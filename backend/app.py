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
    'FOOD_COUNT': 2000,         
    'MAX_FOOD': 5000,           
    'BOT_COUNT': 50,
    'BASE_SPEED': 7,
    'BOOST_SPEED': 14,
    'INITIAL_LENGTH': 10,
    
    # --- VISUAL / ZOOM TUNING ---
    'RADIUS_STEP': 100,    #lower values = faster growth    
    'MAX_RADIUS': 120,        
    'ZOOM_BASE': 1.8,        
    'ZOOM_DAMPENER': 2500,   

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
food_grid = {}
spatial_grid = {} 
respawn_queue = [] 

FOOD_LIST_CACHE = []
FOOD_CACHE_DIRTY = True

# --- HELPERS ---
def get_grid_key(x, y):
    return int(x // GAME_CONFIG['GRID_CELL_SIZE']), int(y // GAME_CONFIG['GRID_CELL_SIZE'])

def add_food_to_grid(fid, f):
    key = get_grid_key(f['x'], f['y'])
    if key not in food_grid: food_grid[key] = set()
    food_grid[key].add(fid)

def remove_food_from_grid(fid, f):
    key = get_grid_key(f['x'], f['y'])
    if key in food_grid:
        food_grid[key].discard(fid)
        if not food_grid[key]: del food_grid[key]

def update_player_bbox(p):
    # Calculate min/max x/y for the whole snake to fast-fail collisions
    xs = [b['x'] for b in p['body']]
    ys = [b['y'] for b in p['body']]
    margin = get_hitbox_radius(p['length']) # Add radius margin
    p['bbox'] = (min(xs)-margin, min(ys)-margin, max(xs)+margin, max(ys)+margin)

def bbox_overlap(b1, b2):
    return not (b1[2] < b2[0] or b1[0] > b2[2] or b1[3] < b2[1] or b1[1] > b2[3])

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
    """Visual Radius: Uses new Config values"""
    return 6 + min(GAME_CONFIG['MAX_RADIUS'], int(length / GAME_CONFIG['RADIUS_STEP']))

def get_hitbox_radius(length):
    """
    Collision Radius.
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
        # Optimization: Use int() instead of round() for speed, or raw floats
        opt_p = {
            'x': int(p.get('x', 0)),
            'y': int(p.get('y', 0)),
            'angle': round(p['angle'], 2),
            'length': int(p['length']),
            'radius': get_radius(p['length']), 
            'color': p['color'],
            'skin': p.get('skin', 'solid'),
            'name': p['name'],
            'boosting': p.get('boosting', False),
            # Optimization: slicing body to send fewer points if bandwidth is an issue
            # For now, just using int() to speed up serialization
            'body': [{'x': int(b['x']), 'y': int(b['y'])} for b in p['body']]
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
    add_food_to_grid(fid, f_obj) # <--- ADDED
    FOOD_CACHE_DIRTY = True
    return fid, f_obj

def respawn_player(pid, is_bot=False):
    if pid not in players: players[pid] = {'id': pid}
    
    spawn_margin = 400
    ms = GAME_CONFIG['MAP_SIZE']
    
    # ... (Random Position Logic remains the same) ...
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
        'debug_lines': [], 'state': 'SPAWN', 'wander_angle': start_angle, 'skin': random.choice(skin_types),
        'step_size': 0 # <--- NEW: Tracks sub-frame movement
    })
    players[pid]['body'] = [{'x': players[pid]['x'], 'y': players[pid]['y']}]

def update_bot_ai(pid):
    bot = players[pid]
    if not bot.get('body'): return
    head = bot['body'][0]
    angle = bot['angle']
    bot['debug_lines'] = []; bot['state'] = 'THINK'

    # 1. WANDER MOMENTUM
    if random.random() < GAME_CONFIG['BOT_WANDER_CHANCE']: 
        bot['wander_angle'] = normalize_angle(bot['wander_angle'] + random.uniform(-0.8, 0.8))

    look_radius = 500 + (bot['length'] * 0.5) 
    look_radius_sq = look_radius**2
    
    # 2. IDENTIFY THREATS
    nearby_ids = get_nearby_players(pid)
    threats = [players[o] for o in nearby_ids if o != pid and o in players and players[o].get('body')]
    
    # 3. IDENTIFY FOOD
    visible_loot = []
    visible_food = []
    
    gx, gy = get_grid_key(head['x'], head['y'])
    
    for dx in [-1, 0, 1]:
        for dy in [-1, 0, 1]:
            key = (gx + dx, gy + dy)
            if key in food_grid:
                for fid in food_grid[key]:
                    if fid not in food: continue
                    f = food[fid]
                    
                    dist_x = f['x'] - head['x']
                    dist_y = f['y'] - head['y']
                    dist_sq = dist_x**2 + dist_y**2
                    
                    if dist_sq < look_radius_sq:
                        dist = math.sqrt(dist_sq)
                        item_angle = math.atan2(dist_y, dist_x)
                        data = {'dist': dist, 'angle': item_angle, 'val': f['value']}
                        
                        if f['is_loot']: 
                            visible_loot.append(data)
                        elif dist < 300: 
                            visible_food.append(data)

    # 4. EVALUATE SECTORS
    best_score = -float('inf')
    best_angle = angle 

    for i in range(16):
        sector = normalize_angle(angle + (i * (math.pi * 2 / 16)))
        tx = head['x'] + math.cos(sector) * look_radius
        ty = head['y'] + math.sin(sector) * look_radius
        
        score = 0
        blocked = False
        
        # A. Wall Avoidance
        if tx < 80 or tx > GAME_CONFIG['MAP_SIZE']-80 or ty < 80 or ty > GAME_CONFIG['MAP_SIZE']-80: 
            blocked = True
        
        # B. Snake Avoidance (Standard)
        if not blocked:
            for t in threats:
                if (tx - t['body'][0]['x'])**2 + (ty - t['body'][0]['y'])**2 < 22500: 
                    blocked = True; break
                
                if (head['x'] - t['body'][0]['x'])**2 + (head['y'] - t['body'][0]['y'])**2 > (look_radius + 300)**2: 
                    continue

                t_rad = get_hitbox_radius(t['length']) 
                safe_dist_sq = (t_rad + 40)**2 
                
                stride = max(1, int(t['length'] / 20))
                for j in range(0, len(t['body'])-1, stride):
                    if get_dist_sq_point_to_segment(t['body'][j]['x'], t['body'][j]['y'], head['x'], head['y'], tx, ty) < safe_dist_sq: 
                        blocked=True; break
                if blocked: break

        if blocked: 
            score = -999999
        else:
            # Base Scores
            score -= abs(get_angle_difference(angle, sector)) * 5.0
            score -= abs(get_angle_difference(bot['wander_angle'], sector)) * 2.0
            
            center_dist = math.hypot(GAME_CONFIG['MAP_SIZE']/2-tx, GAME_CONFIG['MAP_SIZE']/2-ty)
            score -= center_dist * GAME_CONFIG['BOT_CENTER_BIAS']
            
            # C. Dynamic Crowd Penalty
            # If this sector points towards a cluster of OTHER snakes, reduce attractiveness
            # This helps prevent the "ball of death"
            for t in threats:
                 t_angle = math.atan2(t['body'][0]['y'] - head['y'], t['body'][0]['x'] - head['x'])
                 if abs(get_angle_difference(sector, t_angle)) < 0.5:
                     score -= 2000  # Penalty for turning towards another head

            # D. Food Attraction (TUNED DOWN)
            has_loot = False
            for l in visible_loot:
                diff = abs(get_angle_difference(sector, l['angle']))
                if diff < 0.5: 
                    # Enough to chase, but not enough to ignore walls/death
                    score += (10000 * l['val']) / (l['dist'] + 10) 
                    has_loot = True
            
            if not has_loot:
                for f in visible_food:
                    diff = abs(get_angle_difference(sector, f['angle']))
                    if diff < 0.6:
                        score += 500 / (f['dist'] + 1)

        if GAME_CONFIG['DEBUG_MODE']:
            c = 'rgba(255,0,0,0.3)' if blocked else ('cyan' if has_loot else 'rgba(0,255,0,0.05)')
            if i % 2 == 0:
                bot['debug_lines'].append({'x': int(head['x']), 'y': int(head['y']), 'tx': int(tx), 'ty': int(ty), 'color': c})
        
        if score > best_score: 
            best_score = score
            best_angle = sector

    bot['target_angle'] = best_angle
    
    # State Logic
    if best_score > 3000:
        bot['state'] = "FEAST"
        bot['boosting'] = True 
    elif best_score > 500: 
        bot['state'] = "GRAZE"
        bot['boosting'] = False
    else: 
        bot['state'] = "WANDER"
        bot['boosting'] = False


async def game_loop():
    print("Game Loop Started")
    tick_count = 0
    last_log_time = time.time()
    perf_frame_count = 0
    perf_total_calc_time = 0
    time_stats = {'ai': 0, 'physics': 0, 'collision': 0, 'broadcast': 0}
    
    global FOOD_LIST_CACHE, FOOD_CACHE_DIRTY
    global food_grid
    
    # Init food grid
    food_grid = {}
    for fid, f in food.items(): add_food_to_grid(fid, f)
    
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

        if tick_count % 5 == 0: rebuild_spatial_grid()
        
        # Garbage Collect
        if tick_count % GAME_CONFIG['GARBAGE_COLLECT_TICKS'] == 0:
            now_ms = current_time * 1000
            expired = [fid for fid, f in food.items() if f['is_loot'] and (now_ms - f['born'] > GAME_CONFIG['LOOT_EXPIRY_MS'])]
            for fid in expired: 
                remove_food_from_grid(fid, food[fid]) 
                del food[fid]
                removed_food_ids.append(fid)
                FOOD_CACHE_DIRTY = True

        player_ids = list(players.keys())
        dead_players = set()

        # AI
        t_ai = time.perf_counter()
        for pid in player_ids:
            if pid in players and players[pid].get('is_bot'): 
                if (tick_count + hash(pid)) % 4 == 0: update_bot_ai(pid)
        time_stats['ai'] += (time.perf_counter() - t_ai)

        # Physics
        t_phys = time.perf_counter()
        for pid in player_ids:
            if pid not in players: continue
            p = players[pid]
            if 'target_angle' not in p: p['target_angle'] = p['angle']
            if not p.get('body'): continue

            # Movement
            turn_speed = get_turn_speed(p['length'])
            if p.get('is_bot'): turn_speed *= GAME_CONFIG['BOT_TURN_MULTIPLIER']
            
            diff = get_angle_difference(p['angle'], p['target_angle'])
            if abs(diff) < turn_speed: p['angle'] = p['target_angle']
            else: p['angle'] += turn_speed if diff > 0 else -turn_speed
            p['angle'] = normalize_angle(p['angle'])
            
            speed_bonus = 1.3 * (1 - (p['length'] / 200)) if p['length'] < 200 else 0
            speed = (GAME_CONFIG['BOOST_SPEED'] + speed_bonus) if p.get('boosting') else (GAME_CONFIG['BASE_SPEED'] + speed_bonus)

            if p.get('boosting'):
                if p['length'] > 10:
                    if tick_count % 3 == 0: 
                        p['length'] -= 1
                        res = spawn_food(p['body'][-1]['x'], p['body'][-1]['y'], 1, 5)
                        if res: added_food[res[0]] = serialize_single_food(res[1])
                else: speed = GAME_CONFIG['BASE_SPEED']; p['boosting'] = False

            # --- MOVEMENT FIX: ACCUMULATOR ---
            head = p['body'][0]
            dx = math.cos(p['angle']) * speed
            dy = math.sin(p['angle']) * speed
            
            # Update position
            head['x'] += dx
            head['y'] += dy
            
            # Accumulate distance
            p.setdefault('step_size', 0)
            p['step_size'] += speed
            
            # Only add segment if we've moved enough (prevents gaps at high speed)
            if p['step_size'] >= GAME_CONFIG['BODY_RESOLUTION']:
                p['step_size'] = 0
                p['body'].insert(1, {'x': head['x'], 'y': head['y']})
                
                # Maintain length
                target_segs = int(p['length'] * (GAME_CONFIG['BASE_SPEED']/GAME_CONFIG['BODY_RESOLUTION'])) + 5
                while len(p['body']) > target_segs: p['body'].pop()
            
            update_player_bbox(p)

            # --- EATING ---
            rad = get_radius(p['length'])
            pickup_sq = (rad * GAME_CONFIG['FOOD_PICKUP_RATIO'] + GAME_CONFIG['FOOD_PICKUP_EXTRA'])**2
            eaten = []
            
            gx, gy = get_grid_key(head['x'], head['y'])
            nearby_food_ids = set()
            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    key = (gx+dx, gy+dy)
                    if key in food_grid: nearby_food_ids.update(food_grid[key])
            
            for fid in nearby_food_ids:
                if fid not in food: continue 
                f = food[fid]
                if (head['x'] - f['x'])**2 + (head['y'] - f['y'])**2 < pickup_sq:
                    p['length'] += f['value']; eaten.append(fid)
            
            for fid in eaten:
                if fid in food:
                    if food[fid]['value'] <= 1: 
                        res = spawn_food()
                        if res: added_food[res[0]] = serialize_single_food(res[1])
                    remove_food_from_grid(fid, food[fid]) 
                    del food[fid]
                    removed_food_ids.append(fid)
                    FOOD_CACHE_DIRTY = True
        time_stats['physics'] += (time.perf_counter() - t_phys)

        # --- COLLISIONS ---
        t_col = time.perf_counter()
        for pid in player_ids:
            if pid not in players: continue
            p = players[pid]; head = p['body'][0]; 
            r1 = get_hitbox_radius(p['length']) 
            
            # Wall
            if head['x'] < r1 or head['x'] > GAME_CONFIG['MAP_SIZE']-r1 or head['y'] < r1 or head['y'] > GAME_CONFIG['MAP_SIZE']-r1:
                dead_players.add(pid); continue

            p_bbox = (head['x']-r1, head['y']-r1, head['x']+r1, head['y']+r1) 
            
            for opid in get_nearby_players(pid):
                if pid == opid or opid not in players: continue
                o = players[opid]; 
                if not o.get('body'): continue
                
                # Fast Fail
                if 'bbox' in o and not bbox_overlap(p_bbox, o['bbox']):
                    continue

                r2 = get_hitbox_radius(o['length']) 
                hit_threshold = r1 + r2
                hit_threshold_sq = hit_threshold**2

                # 1. Head-on-Head
                if (head['x'] - o['body'][0]['x'])**2 + (head['y'] - o['body'][0]['y'])**2 < hit_threshold_sq:
                    if p['length'] <= o['length']: dead_players.add(pid)
                    continue 
                
                # 2. Body Segments
                collided = False
                
                # We iterate all segments to ensure no tunneling. BBox check makes this cheap.
                for i in range(1, len(o['body'])):
                    seg = o['body'][i]
                    
                    # Segment BBox optimization
                    if seg['x'] + r2 < p_bbox[0] or seg['x'] - r2 > p_bbox[2] or \
                       seg['y'] + r2 < p_bbox[1] or seg['y'] - r2 > p_bbox[3]:
                        continue

                    if (head['x'] - seg['x'])**2 + (head['y'] - seg['y'])**2 < hit_threshold_sq:
                        collided = True; break
                
                if collided: dead_players.add(pid); break
        time_stats['collision'] += (time.perf_counter() - t_col)

        # Deaths
        for pid in dead_players:
            if pid in players:
                p = players[pid]
                if p.get('body'):
                    drops = []
                    # Drop logic
                    step = max(1, int(len(p['body']) / 30)) 
                    for i in range(0, len(p['body']), step):
                        s = p['body'][i]
                        if random.random() < GAME_CONFIG['FOOD_DROP_RATIO']: drops.append(s)
                    
                    slots_needed = len(food) + len(drops) - GAME_CONFIG['MAX_FOOD']
                    if slots_needed > 0:
                        keys_to_remove = []
                        for fid, f in food.items():
                            if f['value'] == 1 and not f['is_loot']:
                                keys_to_remove.append(fid)
                                if len(keys_to_remove) >= slots_needed: break
                        for fid in keys_to_remove:
                            remove_food_from_grid(fid, food[fid])
                            del food[fid]
                            removed_food_ids.append(fid)

                    for s in drops:
                        res = spawn_food(s['x'], s['y'], 5, 12, force=True)
                        if res: added_food[res[0]] = serialize_single_food(res[1])
                
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
async def cheat_boost(sid, data):
    if sid in players and not players[sid].get('is_bot'):
        mass_increase = data.get('mass', 0)
        if mass_increase > 0:
            players[sid]['length'] += mass_increase
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
    

@sio.event
async def enter_spectator(sid):
    """Kill the player immediately and remove them for spectator mode"""
    if sid in players:
        p = players[sid]
        
        # 1. Drop Food (simulate death)
        if p.get('body'):
            drops = []
            for s in p['body']:
                if random.random() < GAME_CONFIG['FOOD_DROP_RATIO']: 
                    drops.append(s)
            
            # Spawn the loot
            for s in drops:
                # spawn_food updates the global 'food' dict automatically
                spawn_food(s['x'], s['y'], 5, 12, force=True)

        # 2. Remove Player completely
        del players[sid]
        
        # 3. Ensure they don't auto-respawn
        global respawn_queue
        respawn_queue = [r for r in respawn_queue if r['sid'] != sid]

@sio.event
async def request_respawn(sid):
    """Manual respawn when exiting spectator mode"""
    if sid not in players:
        respawn_player(sid)
        await sio.emit('init_player', {'id': sid}, to=sid)

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