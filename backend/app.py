import asyncio
import math
import random
import time
import socketio
from flask import Flask

sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
app = Flask(__name__)
app_asgi = socketio.ASGIApp(sio, app)

# --- CONFIG ---
GAME_CONFIG = {
    'MAP_SIZE': 5000,
    'GRID_CELL_SIZE': 400,
    'FOOD_COUNT': 1000,         
    'MAX_FOOD': 2000,           
    'BOT_COUNT': 50,
    'BASE_SPEED': 7,
    'BOOST_SPEED': 14,
    'INITIAL_LENGTH': 10,
    
    # --- TUNING ---
    'DEBUG_MODE': False,            
    'BOT_TURN_MULTIPLIER': 1.2,     
    'BOT_CENTER_BIAS': 0.001,       
    'BOT_WANDER_CHANCE': 0.05,      
    'BODY_RESOLUTION': 12,      

    # --- PHYSICS ---
    'TURN_DECAY_FACTOR': 300,
    'INITIAL_TURN_SPEED': 0.5,
    'MIN_TURN_SPEED': 0.08,
    
    'FOOD_PICKUP_RATIO': 0.8,   
    'FOOD_PICKUP_EXTRA': 10,
    'FOOD_DROP_RATIO': 0.5,
    'LOOT_EXPIRY_MS': 15000,
    'GARBAGE_COLLECT_TICKS': 60,
    'RESPAWN_DELAY': 3.0,
    'LOG_INTERVAL_SEC': 2.0 
}

players = {}
food = {} 
spatial_grid = {} 
respawn_queue = [] 

FOOD_LIST_CACHE = []
FOOD_CACHE_DIRTY = True

# --- HELPERS ---
def normalize_angle(angle):
    while angle > math.pi: angle -= 2 * math.pi
    while angle < -math.pi: angle += 2 * math.pi
    return angle

def get_dist_sq_point_to_segment(px, py, x1, y1, x2, y2):
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return (px - x1)**2 + (py - y1)**2
    t = ((px - x1) * dx + (py - y1) * dy) / (dx*dx + dy*dy)
    t = max(0, min(1, t))
    nx = x1 + t * dx
    ny = y1 + t * dy
    return (px - nx)**2 + (py - ny)**2

def get_angle_difference(source, target):
    diff = target - source
    return normalize_angle(diff)

def generate_name():
    ADJECTIVES = ["Happy", "Speedy", "Slippery", "Neon", "Angry", "Brave", "Lucky", "Wild", "Sneaky", "Hyper", "Robo", "Cyber", "Giant", "Tiny"]
    NOUNS = ["Python", "Viper", "Noodle", "Cobra", "Worm", "Boa", "Mamba", "Slider", "Glider", "Serpent", "Bot", "Droid", "Dragon", "Basilisk"]
    return f"{random.choice(ADJECTIVES)} {random.choice(NOUNS)}"

# --- NEW PHYSICS: DYNAMIC HITBOX ---
def get_radius(length):
    """Visual Radius"""
    return 6 + min(28, int(length / 15))

def get_hitbox_radius(length):
    """
    Collision Radius.
    Small snakes need BIG hitboxes (0.9 ratio) to feel solid.
    Big snakes need TINY hitboxes (0.4 ratio) to allow maneuvering.
    """
    vis_rad = get_radius(length)
    if length < 50:
        return vis_rad * 0.9 
    elif length < 200:
        return vis_rad * 0.7 
    else:
        return vis_rad * 0.4 

def get_turn_speed(length):
    decay = GAME_CONFIG['TURN_DECAY_FACTOR']
    factor = 1 / (1 + length / decay)
    return max(GAME_CONFIG['MIN_TURN_SPEED'], GAME_CONFIG['INITIAL_TURN_SPEED'] * factor)

# --- DATA OPTIMIZATION ---
def smart_serialize_players():
    optimized = {}
    for pid, p in players.items():
        opt_p = {
            'x': round(p.get('x', 0), 1),
            'y': round(p.get('y', 0), 1),
            'angle': round(p['angle'], 2),
            'length': int(p['length']),
            'radius': get_radius(p['length']), 
            'color': p['color'],
            'skin': p.get('skin', 'solid'),
            'name': p['name'],
            'boosting': p.get('boosting', False),
            'body': [{'x': round(b['x'], 1), 'y': round(b['y'], 1)} for b in p['body']]
        }
        if GAME_CONFIG['DEBUG_MODE'] and p.get('debug_lines'):
             opt_p['debug_lines'] = p['debug_lines']
             opt_p['state'] = p.get('state', '')
        optimized[pid] = opt_p
    return optimized

def serialize_single_food(f):
    return { 'x': int(f['x']), 'y': int(f['y']), 'color': f['color'], 'value': f['value'], 'is_loot': f['is_loot'] }

# --- SPATIAL GRID ---
def rebuild_spatial_grid():
    global spatial_grid
    spatial_grid = {}
    cell_size = GAME_CONFIG['GRID_CELL_SIZE']
    
    for pid, p in players.items():
        if not p.get('body'): continue
        points = [p['body'][0]]
        if len(p['body']) > 5: points.append(p['body'][len(p['body'])//2])
        
        for pt in points:
            gx = int(pt['x'] // cell_size)
            gy = int(pt['y'] // cell_size)
            key = (gx, gy)
            if key not in spatial_grid: spatial_grid[key] = []
            if pid not in spatial_grid[key]: spatial_grid[key].append(pid)

def get_nearby_players(pid):
    if pid not in players or not players[pid].get('body'): return []
    head = players[pid]['body'][0]
    cell_size = GAME_CONFIG['GRID_CELL_SIZE']
    gx = int(head['x'] // cell_size)
    gy = int(head['y'] // cell_size)
    
    candidates = set()
    for dx in [-1, 0, 1]:
        for dy in [-1, 0, 1]:
            key = (gx + dx, gy + dy)
            if key in spatial_grid:
                candidates.update(spatial_grid[key])
    return list(candidates)

# --- GAME LOGIC ---
def spawn_food(x=None, y=None, value=1, scatter=25, force=False): 
    global FOOD_CACHE_DIRTY
    
    # <--- MODIFIED: Allow forcing spawn even if full
    if not force and len(food) >= GAME_CONFIG['MAX_FOOD']: 
        return None

    fid = f"f_{random.randint(0, 999999999)}"
    if x is not None:
        x += random.uniform(-scatter, scatter)
        y += random.uniform(-scatter, scatter)
    else:
        x = random.randint(0, GAME_CONFIG['MAP_SIZE'])
        y = random.randint(0, GAME_CONFIG['MAP_SIZE'])
        
    f_obj = {
        'x': x, 'y': y, 
        'color': f'#{random.randint(0, 0xFFFFFF):06x}', 
        'value': value, 
        'is_loot': value > 1,
        'born': time.time() * 1000 
    }
    food[fid] = f_obj
    FOOD_CACHE_DIRTY = True
    return fid, f_obj

def respawn_player(pid, is_bot=False):
    if pid not in players: players[pid] = {'id': pid}
    
    spawn_margin = 400
    ms = GAME_CONFIG['MAP_SIZE']
    
    if random.random() < 0.5:
        x = random.randint(100, ms - 100)
        y = random.randint(100, spawn_margin) if random.random() < 0.5 else random.randint(ms - spawn_margin, ms - 100)
    else:
        x = random.randint(100, spawn_margin) if random.random() < 0.5 else random.randint(ms - spawn_margin, ms - 100)
        y = random.randint(100, ms - 100)

    center_x, center_y = ms / 2, ms / 2
    angle_to_center = math.atan2(center_y - y, center_x - x)
    start_angle = normalize_angle(angle_to_center + random.uniform(-0.5, 0.5))
    skin_types = ['solid', 'stripe', 'spot']
    
    players[pid].update({
        'x': x, 'y': y, 'length': GAME_CONFIG['INITIAL_LENGTH'],
        'angle': start_angle, 'target_angle': start_angle,
        'boosting': False, 'name': generate_name(),
        'color': f'#{random.randint(0, 0xFFFFFF):06x}', 
        'is_bot': is_bot, 
        'debug_lines': [], 'state': 'SPAWN', 'wander_angle': start_angle, 'skin': random.choice(skin_types)
    })
    players[pid]['body'] = [{'x': players[pid]['x'], 'y': players[pid]['y']}]

# --- AI ---
def update_bot_ai(pid):
    bot = players[pid]
    if not bot.get('body'): return
    head = bot['body'][0]
    angle = bot['angle']
    bot['debug_lines'] = []; bot['state'] = 'THINK'

    if random.random() < GAME_CONFIG['BOT_WANDER_CHANCE']: 
        bot['wander_angle'] = normalize_angle(bot['wander_angle'] + random.uniform(-0.8, 0.8))

    look_radius = 400 + (bot['length'] * 0.5) 
    look_radius_sq = look_radius**2
    
    nearby_ids = get_nearby_players(pid)
    threats = [players[o] for o in nearby_ids if o != pid and o in players and players[o].get('body')]
    
    visible_loot = []
    visible_food = []
    
    global FOOD_LIST_CACHE
    sample_size = 40
    food_sample = random.sample(FOOD_LIST_CACHE, min(len(FOOD_LIST_CACHE), sample_size))
    
    for f in food_sample:
        dx = f['x'] - head['x']
        dy = f['y'] - head['y']
        dist_sq = dx*dx + dy*dy
        
        if dist_sq < look_radius_sq:
            item_angle = math.atan2(dy, dx)
            dist = math.sqrt(dist_sq) 
            data = {'dist': dist, 'angle': item_angle}
            if f['is_loot']: visible_loot.append(data)
            elif dist < 300: visible_food.append(data)

    best_score = -999999; best_angle = angle 

    for i in range(16):
        sector = normalize_angle(angle + (i * (math.pi * 2 / 16)))
        tx = head['x'] + math.cos(sector) * look_radius; ty = head['y'] + math.sin(sector) * look_radius
        
        score = 0; blocked = False; loot = False
        if tx < 80 or tx > GAME_CONFIG['MAP_SIZE']-80 or ty < 80 or ty > GAME_CONFIG['MAP_SIZE']-80: blocked = True
        
        if not blocked:
            for t in threats:
                if (tx - t['body'][0]['x'])**2 + (ty - t['body'][0]['y'])**2 < 22500: blocked = True; break
                if (head['x'] - t['body'][0]['x'])**2 + (head['y'] - t['body'][0]['y'])**2 > (look_radius + 200)**2: continue

                t_rad = get_hitbox_radius(t['length']) 
                safe_dist_sq = (t_rad + 30)**2
                for j in range(0, len(t['body'])-1, 3):
                    if get_dist_sq_point_to_segment(t['body'][j]['x'], t['body'][j]['y'], head['x'], head['y'], tx, ty) < safe_dist_sq: 
                        blocked=True; break
                if blocked: break

        if blocked: score = -999999
        else:
            score -= abs(get_angle_difference(angle, sector)) * 2.0
            score -= abs(get_angle_difference(bot['wander_angle'], sector)) * 1.5
            center_dist = math.hypot(GAME_CONFIG['MAP_SIZE']/2-tx, GAME_CONFIG['MAP_SIZE']/2-ty)
            score -= center_dist * GAME_CONFIG['BOT_CENTER_BIAS']
            
            for l in visible_loot:
                if abs(get_angle_difference(sector, l['angle'])) < 0.4: score += 50000 / (l['dist']*0.1 + 1); loot = True
            
            if not loot:
                for f in visible_food:
                    if abs(get_angle_difference(sector, f['angle'])) < 0.5: score += 30 / (f['dist'] * 0.01 + 1)

        if GAME_CONFIG['DEBUG_MODE']:
            c = 'rgba(255,0,0,0.3)' if blocked else ('cyan' if loot else 'rgba(0,255,0,0.05)')
            bot['debug_lines'].append({'x': head['x'], 'y': head['y'], 'tx': tx, 'ty': ty, 'color': c})
        
        if score > best_score: best_score = score; best_angle = sector

    bot['target_angle'] = best_angle
    if best_score > 10000: bot['state']="LOOT"; bot['boosting']=True
    elif best_score > 50: bot['state']="GRAZE"; bot['boosting']=False
    else: bot['state']="WANDER"; bot['boosting']=False

async def game_loop():
    print("Game Loop Started")
    tick_count = 0
    last_log_time = time.time()
    perf_frame_count = 0
    perf_total_calc_time = 0
    time_stats = {'ai': 0, 'physics': 0, 'collision': 0, 'broadcast': 0}
    
    global FOOD_LIST_CACHE, FOOD_CACHE_DIRTY
    FOOD_LIST_CACHE = list(food.values())
    
    while True:
        t_start = time.perf_counter()
        await asyncio.sleep(1/30) 
        tick_count += 1
        current_time = time.time()
        
        if FOOD_CACHE_DIRTY:
            FOOD_LIST_CACHE = list(food.values())
            FOOD_CACHE_DIRTY = False

        added_food = {}
        removed_food_ids = []

        # 1. Respawn
        for r in respawn_queue[:]:
            if current_time > r['time']:
                respawn_player(r['sid'])
                await sio.emit('init_player', {'id': r['sid']}, to=r['sid'])
                respawn_queue.remove(r)

        if tick_count % 2 == 0: rebuild_spatial_grid()
        
        if tick_count % GAME_CONFIG['GARBAGE_COLLECT_TICKS'] == 0:
            now_ms = current_time * 1000
            expired = [fid for fid, f in food.items() if f['is_loot'] and (now_ms - f['born'] > GAME_CONFIG['LOOT_EXPIRY_MS'])]
            for fid in expired: 
                del food[fid]
                removed_food_ids.append(fid)
                FOOD_CACHE_DIRTY = True

        player_ids = list(players.keys())
        dead_players = set()

        # AI
        t_ai = time.perf_counter()
        for pid in player_ids:
            if pid in players and players[pid].get('is_bot'): 
                if (tick_count + hash(pid)) % 3 == 0: update_bot_ai(pid)
        time_stats['ai'] += (time.perf_counter() - t_ai)

        # Physics
        t_phys = time.perf_counter()
        for pid in player_ids:
            if pid not in players: continue
            p = players[pid]
            if 'target_angle' not in p: p['target_angle'] = p['angle']
            if not p.get('body'): continue

            turn_speed = get_turn_speed(p['length'])
            if p.get('is_bot'): turn_speed *= GAME_CONFIG['BOT_TURN_MULTIPLIER']
            
            diff = get_angle_difference(p['angle'], p['target_angle'])
            if abs(diff) < turn_speed: p['angle'] = p['target_angle']
            else: p['angle'] += turn_speed if diff > 0 else -turn_speed
            p['angle'] = normalize_angle(p['angle'])

            speed_bonus = 3.0 * (1 - (p['length'] / 200)) if p['length'] < 200 else 0
            speed = (GAME_CONFIG['BOOST_SPEED'] + speed_bonus) if p.get('boosting') else (GAME_CONFIG['BASE_SPEED'] + speed_bonus)
            
            if p.get('boosting'):
                if p['length'] > 10:
                    if tick_count % 3 == 0: 
                        p['length'] -= 1
                        res = spawn_food(p['body'][-1]['x'], p['body'][-1]['y'], 1, 5)
                        if res: added_food[res[0]] = serialize_single_food(res[1])
                else: speed = GAME_CONFIG['BASE_SPEED']; p['boosting'] = False

            head = p['body'][0]
            new_x = head['x'] + math.cos(p['angle']) * speed
            new_y = head['y'] + math.sin(p['angle']) * speed
            p['body'][0]['x'] = new_x; p['body'][0]['y'] = new_y
            
            if len(p['body']) == 1: p['body'].append({'x': new_x, 'y': new_y})
            else:
                dist_sq = (new_x - p['body'][1]['x'])**2 + (new_y - p['body'][1]['y'])**2
                if dist_sq > GAME_CONFIG['BODY_RESOLUTION']**2:
                    p['body'].insert(1, {'x': new_x, 'y': new_y})
                    target_segs = int(p['length'] * (GAME_CONFIG['BASE_SPEED']/GAME_CONFIG['BODY_RESOLUTION'])) + 2
                    while len(p['body']) > target_segs: p['body'].pop()

            # Eating
            rad = get_radius(p['length'])
            pickup_sq = (rad * GAME_CONFIG['FOOD_PICKUP_RATIO'] + GAME_CONFIG['FOOD_PICKUP_EXTRA'])**2
            eaten = []
            
            check_rad = rad + 20
            for fid, f in food.items():
                if abs(new_x - f['x']) > check_rad or abs(new_y - f['y']) > check_rad: continue
                if (new_x - f['x'])**2 + (new_y - f['y'])**2 < pickup_sq:
                    p['length'] += f['value']; eaten.append(fid)
            
            for fid in eaten:
                if food[fid]['value'] <= 1: 
                    res = spawn_food()
                    if res: added_food[res[0]] = serialize_single_food(res[1])
                del food[fid]
                removed_food_ids.append(fid)
                FOOD_CACHE_DIRTY = True
        time_stats['physics'] += (time.perf_counter() - t_phys)

        # Collisions (Dynamic Hitbox)
        t_col = time.perf_counter()
        for pid in player_ids:
            if pid not in players: continue
            p = players[pid]; head = p['body'][0]; 
            r1 = get_hitbox_radius(p['length']) 
            
            if head['x'] < r1 or head['x'] > GAME_CONFIG['MAP_SIZE']-r1 or head['y'] < r1 or head['y'] > GAME_CONFIG['MAP_SIZE']-r1:
                dead_players.add(pid); continue

            for opid in get_nearby_players(pid):
                if pid == opid or opid not in players: continue
                o = players[opid]; 
                if not o.get('body'): continue
                
                r2 = get_hitbox_radius(o['length']) 
                hit_threshold_sq = (r1 + r2)**2

                if (head['x'] - o['body'][0]['x'])**2 + (head['y'] - o['body'][0]['y'])**2 < hit_threshold_sq:
                    dead_players.add(pid); continue

                collided = False
                for i in range(1, len(o['body'])-1):
                    p1, p2 = o['body'][i], o['body'][i+1]
                    hit_dist = r1 + r2
                    if min(p1['x'], p2['x'])-hit_dist < head['x'] < max(p1['x'], p2['x'])+hit_dist and \
                       min(p1['y'], p2['y'])-hit_dist < head['y'] < max(p1['y'], p2['y'])+hit_dist:
                        if get_dist_sq_point_to_segment(head['x'], head['y'], p1['x'], p1['y'], p2['x'], p2['y']) < hit_threshold_sq:
                            collided = True; break
                if collided: dead_players.add(pid); break
        time_stats['collision'] += (time.perf_counter() - t_col)

        # Deaths
        for pid in dead_players:
            if pid in players:
                p = players[pid]
                if p.get('body'):
                    # <--- NEW DEATH LOGIC START
                    drops_to_spawn = []
                    
                    # 1. Calculate how many drops we WANT to create
                    for s in p['body']:
                        if random.random() < GAME_CONFIG['FOOD_DROP_RATIO']:
                             drops_to_spawn.append(s)
                    
                    # 2. Check how many slots we need vs how many we have
                    slots_needed = len(food) + len(drops_to_spawn) - GAME_CONFIG['MAX_FOOD']
                    
                    # 3. If over capacity, delete small ambient food (value=1) to make room
                    if slots_needed > 0:
                        keys_to_remove = []
                        for fid, f in food.items():
                            if f['value'] == 1 and not f['is_loot']:
                                keys_to_remove.append(fid)
                                if len(keys_to_remove) >= slots_needed:
                                    break
                        
                        # Delete the victims and tell clients
                        for fid in keys_to_remove:
                            del food[fid]
                            removed_food_ids.append(fid)
                            FOOD_CACHE_DIRTY = True

                    # 4. Spawn the loot (Force = True ensures we spawn even if slightly over limit)
                    for s in drops_to_spawn:
                        res = spawn_food(s['x'], s['y'], 5, 12, force=True)
                        if res: added_food[res[0]] = serialize_single_food(res[1])
                    # <--- NEW DEATH LOGIC END
                
                if p.get('is_bot'): respawn_player(pid, is_bot=True)
                else:
                    try: asyncio.create_task(sio.emit('death', {'score': int(p['length'])}, to=pid))
                    except: pass
                    del players[pid]
                    respawn_queue.append({'sid': pid, 'time': current_time + GAME_CONFIG['RESPAWN_DELAY']})

        # Broadcast
        t_cast = time.perf_counter()
        opt_players = smart_serialize_players()
        food_diff = {'added': added_food, 'removed': removed_food_ids}
        
        sorted_players = sorted(players.values(), key=lambda x: x['length'], reverse=True)[:5]
        leaderboard = [{'name': p['name'], 'score': int(p['length'])} for p in sorted_players]
        
        await sio.emit('game_tick', {'players': opt_players, 'food_diff': food_diff, 'leaderboard': leaderboard})
        time_stats['broadcast'] += (time.perf_counter() - t_cast)
        
        # Logging
        dt = time.perf_counter() - t_start
        perf_total_calc_time += dt
        perf_frame_count += 1
        
        if current_time - last_log_time > GAME_CONFIG['LOG_INTERVAL_SEC']:
            avg_load_ms = (perf_total_calc_time / perf_frame_count) * 1000
            tps = perf_frame_count / (current_time - last_log_time)
            
            avg_ai = (time_stats['ai'] / perf_frame_count) * 1000
            avg_phys = (time_stats['physics'] / perf_frame_count) * 1000
            avg_col = (time_stats['collision'] / perf_frame_count) * 1000
            avg_cast = (time_stats['broadcast'] / perf_frame_count) * 1000
            
            print(f"[STATUS] TPS: {tps:.1f} | Load: {avg_load_ms:.2f}ms | AI: {avg_ai:.1f}ms | Phys: {avg_phys:.1f}ms | Col: {avg_col:.1f}ms | Cast: {avg_cast:.1f}ms | Food: {len(food)}")
            
            last_log_time = current_time
            perf_frame_count = 0
            perf_total_calc_time = 0
            time_stats = {'ai': 0, 'physics': 0, 'collision': 0, 'broadcast': 0}

@sio.event
async def connect(sid, environ):
    respawn_player(sid, is_bot=False)
    await sio.emit('init_config', GAME_CONFIG, to=sid)
    full_food = {fid: serialize_single_food(f) for fid, f in food.items()}
    await sio.emit('init_food', full_food, to=sid)
    await sio.emit('init_player', {'id': sid}, to=sid)

@sio.event
async def disconnect(sid):
    if sid in players: del players[sid]
    global respawn_queue
    respawn_queue = [r for r in respawn_queue if r['sid'] != sid]

@sio.event
async def input_update(sid, data):
    if sid in players: players[sid]['target_angle'] = data['angle']

@sio.event
async def boost_update(sid, data):
    if sid in players: players[sid]['boosting'] = data['boosting']

if __name__ == '__main__':
    import uvicorn
    for _ in range(GAME_CONFIG['FOOD_COUNT']): spawn_food()
    for i in range(GAME_CONFIG['BOT_COUNT']): respawn_player(f"bot_{i}", is_bot=True)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(game_loop())
    config = uvicorn.Config(app_asgi, host='0.0.0.0', port=5001, loop=loop)
    server = uvicorn.Server(config)
    loop.run_until_complete(server.serve())