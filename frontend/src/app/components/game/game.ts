import { CommonModule } from '@angular/common';
import {
    Component,
    ElementRef,
    HostListener,
    inject,
    signal,
    ViewChild,
    OnInit,
    NgZone,
    Host,
} from '@angular/core';
import { Socket } from 'ngx-socket-io';

interface Point {
    x: number;
    y: number;
}
interface VisualPlayer {
    x: number;
    y: number;
    angle: number;
    body: Point[];
}

@Component({
    selector: 'app-game',
    standalone: true,
    imports: [CommonModule],
    templateUrl: './game.html',
    styleUrl: './game.scss',
})
export class Game implements OnInit {
    private socketWrapper = inject(Socket);
    private ngZone = inject(NgZone);
    private get socket() {
        return this.socketWrapper.ioSocket;
    }

    @ViewChild('gameCanvas', { static: true }) canvasRef!: ElementRef<HTMLCanvasElement>;
    private ctx!: CanvasRenderingContext2D;

    isConnected = signal(false);
    myId = signal('');
    isDead = signal(false);
    finalScore = signal(0);
    playerCount = signal(0);
    myScore = signal(0);
    spectateMode = signal(false);

    private players: any = {};
    private food: any = {};
    private leaderboard: any[] = [];

    private visualPlayers: { [id: string]: VisualPlayer } = {};

    private camX = 0;
    private camY = 0;
    private currentScale = 1;
    private targetScale = 1;
    private lastTime = 0;

    private driftAngle = 0;

    // Defaults until config loads
    config: any = {
        MAP_SIZE: 5000,
        DEBUG_MODE: false,
        ZOOM_BASE: 1.8,
        ZOOM_DAMPENER: 2500,
    };

    ngOnInit() {
        this.ctx = this.canvasRef.nativeElement.getContext('2d')!;
        this.resizeCanvas();

        this.socketWrapper.fromEvent('connect').subscribe(() => {
            this.isConnected.set(true);
            if (this.socket.id) this.myId.set(this.socket.id);
        });

        this.socketWrapper.fromEvent('init_config').subscribe((cfg: any) => (this.config = cfg));

        this.socketWrapper.fromEvent('init_food').subscribe((fullFood: any) => {
            this.food = fullFood;
        });

        this.socketWrapper.fromEvent('death').subscribe((data: any) => {
            this.isDead.set(true);
            this.finalScore.set(data.score);
            this.targetScale = 0.4;
            this.driftAngle = Math.random() * Math.PI * 2;
        });

        this.socketWrapper.fromEvent('init_player').subscribe((data: any) => {
            this.isDead.set(false);
            if (data.id) this.myId.set(data.id);
            this.targetScale = 1.0;
        });

        this.socketWrapper.fromEvent('disconnect').subscribe(() => this.isConnected.set(false));

        this.socketWrapper.fromEvent('game_tick').subscribe((gameState: any) => {
            this.players = gameState.players;
            this.leaderboard = gameState.leaderboard || [];
            this.playerCount.set(Object.keys(this.players).length);

            if (gameState.food_diff) {
                if (gameState.food_diff.removed) {
                    gameState.food_diff.removed.forEach((fid: string) => {
                        delete this.food[fid];
                    });
                }
                if (gameState.food_diff.added) {
                    Object.assign(this.food, gameState.food_diff.added);
                }
            }

            if (!this.myId() && this.socket.id) this.myId.set(this.socket.id);

            const me = this.players[this.myId()];
            if (me && !this.isDead()) {
                this.myScore.set(Math.floor(me.length));
                // UPDATE: Uses config values for zoom calculation
                const base = this.config.ZOOM_BASE || 1.8;
                const damp = this.config.ZOOM_DAMPENER || 2500;
                this.targetScale = Math.max(0.4, base / (1 + me.length / damp));
            }
        });

        this.lastTime = performance.now();
        this.ngZone.runOutsideAngular(() => this.renderLoop());
    }

    renderLoop() {
        const now = performance.now();
        const dt = (now - this.lastTime) / 1000;
        this.lastTime = now;

        this.updateVisuals(dt);
        this.render();
        requestAnimationFrame(() => this.renderLoop());
    }

    updateVisuals(dt: number) {
        const lerpSpeed = 10.0 * dt;

        for (let pid in this.visualPlayers) {
            if (!this.players[pid] && pid !== this.myId()) delete this.visualPlayers[pid];
        }

        for (let pid in this.players) {
            const p = this.players[pid];
            if (!p.body || p.body.length === 0) continue;

            if (!this.visualPlayers[pid]) {
                const bodyCopy = p.body.map((b: any) => ({ x: b.x, y: b.y }));
                this.visualPlayers[pid] = {
                    x: p.body[0].x,
                    y: p.body[0].y,
                    angle: p.angle,
                    body: bodyCopy,
                };
            }

            const vis = this.visualPlayers[pid];
            while (vis.body.length < p.body.length) vis.body.push({ ...vis.body[vis.body.length - 1] });
            while (vis.body.length > p.body.length) vis.body.pop();

            const targetHead = p.body[0];
            const dist = Math.hypot(targetHead.x - vis.x, targetHead.y - vis.y);

            if (dist > 500) {
                vis.x = targetHead.x;
                vis.y = targetHead.y;
                vis.angle = p.angle;
                vis.body = p.body.map((b: any) => ({ x: b.x, y: b.y }));
            } else {
                vis.x += (targetHead.x - vis.x) * lerpSpeed;
                vis.y += (targetHead.y - vis.y) * lerpSpeed;

                let diff = p.angle - vis.angle;
                while (diff > Math.PI) diff -= Math.PI * 2;
                while (diff < -Math.PI) diff += Math.PI * 2;
                vis.angle += diff * lerpSpeed;

                for (let i = 0; i < p.body.length; i++) {
                    vis.body[i].x += (p.body[i].x - vis.body[i].x) * lerpSpeed;
                    vis.body[i].y += (p.body[i].y - vis.body[i].y) * lerpSpeed;
                }
            }
        }

        if (this.spectateMode()) {
            this.updateSpectatorMovement(dt);
            this.currentScale += (this.targetScale - this.currentScale) * (5.0 * dt);
        } 
        else if (this.isDead()) {
            this.camX += Math.cos(this.driftAngle) * (20 * dt);
            this.camY += Math.sin(this.driftAngle) * (20 * dt);
            this.currentScale += (this.targetScale - this.currentScale) * (1.0 * dt);
        } 
        else {
            const myId = this.myId();
            if (this.visualPlayers[myId]) {
                const me = this.visualPlayers[myId];
                if (Math.hypot(me.x - this.camX, me.y - this.camY) > 1000) {
                    this.camX = me.x; this.camY = me.y;
                } else {
                    this.camX += (me.x - this.camX) * (10.0 * dt); 
                    this.camY += (me.y - this.camY) * (10.0 * dt);
                }
            }
            this.currentScale += (this.targetScale - this.currentScale) * (2.0 * dt);
        }
    }

    render() {
        const canvas = this.canvasRef.nativeElement;
        const width = canvas.width;
        const height = canvas.height;

        this.ctx.fillStyle = '#171717';
        this.ctx.fillRect(0, 0, width, height);

        this.ctx.save();
        this.ctx.translate(width / 2, height / 2);
        this.ctx.scale(this.currentScale, this.currentScale);
        this.ctx.translate(-this.camX, -this.camY);

        this.ctx.strokeStyle = '#2a2a2a';
        this.ctx.lineWidth = 10;
        this.ctx.strokeRect(0, 0, this.config.MAP_SIZE, this.config.MAP_SIZE);
        this.drawGrid();

        // Food
        for (let fid in this.food) {
            const f = this.food[fid];
            if (
                Math.abs(f.x - this.camX) > width / this.currentScale ||
                Math.abs(f.y - this.camY) > height / this.currentScale
            )
                continue;

            this.ctx.beginPath();
            const radius = f.is_loot ? 6 + Math.sin(Date.now() / 200) * 1 : 3;
            this.ctx.arc(f.x, f.y, radius, 0, 2 * Math.PI);
            this.ctx.fillStyle = f.color;
            this.ctx.shadowBlur = f.is_loot ? 20 : 0;
            this.ctx.shadowColor = f.color;
            this.ctx.fill();
            this.ctx.shadowBlur = 0;
        }

        // Snakes
        for (let pid in this.players) {
            if (this.isDead() && pid === this.myId()) continue;

            const p = this.players[pid];
            const vis = this.visualPlayers[pid];
            if (!vis || !vis.body || vis.body.length === 0) continue;

            if (Math.abs(vis.x - this.camX) > width / this.currentScale + 200) continue;

            const radius = p.radius || 10 + Math.min(25, Math.floor(p.length / 20));
            this.ctx.lineCap = 'round';
            this.ctx.lineJoin = 'round';
            this.ctx.lineWidth = radius * 2;
            this.ctx.strokeStyle = p.color;

            if (p.boosting) {
                this.ctx.shadowBlur = radius + 5;
                this.ctx.shadowColor = p.color;
            }

            // --- CHANGED: Hide Gap Filling in Debug Mode ---
            if (!this.config.DEBUG_MODE) {
                // NORMAL RENDERING (Smooth Curves/Lines)
                if (p.skin === 'stripe') {
                    // ... (stripe logic) ...
                    this.ctx.beginPath();
                    this.ctx.moveTo(vis.body[0].x, vis.body[0].y);
                    for (let i = 1; i < vis.body.length; i++) this.ctx.lineTo(vis.body[i].x, vis.body[i].y);
                    this.ctx.stroke();

                    this.ctx.save();
                    this.ctx.strokeStyle = 'rgba(0,0,0,0.2)';
                    this.ctx.beginPath();
                    for (let i = 0; i < vis.body.length; i += 2) {
                        if (i + 1 < vis.body.length) {
                            this.ctx.moveTo(vis.body[i].x, vis.body[i].y);
                            this.ctx.lineTo(vis.body[i + 1].x, vis.body[i + 1].y);
                        }
                    }
                    this.ctx.stroke();
                    this.ctx.restore();
                } else if (p.skin === 'spot') {
                   // ... (spot logic) ...
                   this.ctx.beginPath();
                   this.ctx.moveTo(vis.body[0].x, vis.body[0].y);
                   for (let i = 1; i < vis.body.length; i++) this.ctx.lineTo(vis.body[i].x, vis.body[i].y);
                   this.ctx.stroke();

                   this.ctx.fillStyle = 'rgba(255,255,255,0.3)';
                   for (let i = 2; i < vis.body.length; i += 3) {
                       this.ctx.beginPath();
                       this.ctx.arc(vis.body[i].x, vis.body[i].y, radius * 0.6, 0, Math.PI * 2);
                       this.ctx.fill();
                   }
                } else {
                    // Standard Curve
                    this.ctx.beginPath();
                    this.ctx.moveTo(vis.body[0].x, vis.body[0].y);
                    if (vis.body.length > 2) {
                        let i;
                        for (i = 1; i < vis.body.length - 2; i++) {
                            const xc = (vis.body[i].x + vis.body[i + 1].x) / 2;
                            const yc = (vis.body[i].y + vis.body[i + 1].y) / 2;
                            this.ctx.quadraticCurveTo(vis.body[i].x, vis.body[i].y, xc, yc);
                        }
                        this.ctx.quadraticCurveTo(vis.body[i].x, vis.body[i].y, vis.body[i + 1].x, vis.body[i + 1].y);
                    } else {
                        for (let i = 1; i < vis.body.length; i++) this.ctx.lineTo(vis.body[i].x, vis.body[i].y);
                    }
                    this.ctx.stroke();
                }
            } else {
                // DEBUG RENDERING: RAW CIRCLES
                // This reveals the actual gap between collision points
                this.ctx.fillStyle = p.color;
                for(let b of vis.body) {
                    this.ctx.beginPath();
                    this.ctx.arc(b.x, b.y, radius, 0, Math.PI*2);
                    this.ctx.fill();
                }
            }

            this.ctx.shadowBlur = 0;

            if (this.config.DEBUG_MODE) {
                // Hitbox Overlay
                this.ctx.beginPath();
                this.ctx.arc(vis.x, vis.y, radius * 0.6, 0, Math.PI * 2);
                this.ctx.strokeStyle = 'rgba(255, 255, 0, 0.8)';
                this.ctx.lineWidth = 1;
                this.ctx.stroke();
            }

            this.ctx.lineWidth = 2;
            this.ctx.strokeStyle = pid === this.myId() ? 'rgba(0,0,0,0.5)' : 'rgba(255,255,255,0.4)';
            this.ctx.stroke();

            this.drawEyes(vis, vis.angle, radius);
            this.drawName(vis, p.name, radius, p.state);

            if (this.config.DEBUG_MODE && p.debug_lines) {
                this.ctx.save();
                this.ctx.lineCap = 'round';
                for (let line of p.debug_lines) {
                    this.ctx.beginPath();
                    this.ctx.moveTo(line.x, line.y);
                    this.ctx.lineTo(line.tx, line.ty);
                    this.ctx.lineWidth = line.color === 'white' ? 3 : line.color === 'cyan' ? 2 : 1;
                    this.ctx.strokeStyle = line.color;
                    this.ctx.stroke();
                }
                this.ctx.restore();
            }
        }

        this.ctx.restore();
        this.drawMinimap(width, height);
        this.drawLeaderboard(width);
        
        if (this.isDead() && !this.spectateMode()) {
            this.ctx.fillStyle = 'rgba(0, 0, 0, 0.4)';
            this.ctx.fillRect(0, 0, width, height);
            
            this.ctx.save();
            this.ctx.shadowBlur = 20;
            this.ctx.shadowColor = 'red';
            this.ctx.fillStyle = '#ff3333';
            this.ctx.font = 'bold 80px Arial';
            this.ctx.textAlign = 'center';
            this.ctx.fillText("YOU DIED", width / 2, height / 2 - 20);
            
            this.ctx.shadowBlur = 0;
            this.ctx.fillStyle = 'white';
            this.ctx.font = 'bold 30px Arial';
            this.ctx.fillText(`Final Length: ${this.finalScore()}`, width / 2, height / 2 + 50);
            
            // Changed text to inform user about Spectator Mode
            this.ctx.font = '20px Arial';
            this.ctx.fillStyle = '#ccc';
            this.ctx.fillText("Press 'S' to Spectate", width / 2, height / 2 + 90);
            this.ctx.restore();
        }
    }

    drawGrid() {
        this.ctx.strokeStyle = '#222';
        this.ctx.lineWidth = 2;
        this.ctx.beginPath();
        const ms = this.config.MAP_SIZE;
        for (let x = 0; x <= ms; x += 100) {
            this.ctx.moveTo(x, 0);
            this.ctx.lineTo(x, ms);
        }
        for (let y = 0; y <= ms; y += 100) {
            this.ctx.moveTo(0, y);
            this.ctx.lineTo(ms, y);
        }
        this.ctx.stroke();
    }

    drawEyes(pos: { x: number; y: number }, angle: number, radius: number) {
        this.ctx.save();
        this.ctx.translate(pos.x, pos.y);
        this.ctx.rotate(angle);
        const scale = radius / 15;
        this.ctx.fillStyle = 'white';
        this.ctx.beginPath();
        this.ctx.arc(8 * scale, -6 * scale, 5 * scale, 0, Math.PI * 2);
        this.ctx.arc(8 * scale, 6 * scale, 5 * scale, 0, Math.PI * 2);
        this.ctx.fill();
        this.ctx.fillStyle = 'black';
        this.ctx.beginPath();
        this.ctx.arc(9 * scale, -6 * scale, 2.5 * scale, 0, Math.PI * 2);
        this.ctx.arc(9 * scale, 6 * scale, 2.5 * scale, 0, Math.PI * 2);
        this.ctx.fill();
        this.ctx.restore();
    }

    drawName(pos: { x: number; y: number }, name: string, radius: number, state?: string) {
        if (!name) return;
        this.ctx.save();
        this.ctx.fillStyle = 'white';
        this.ctx.strokeStyle = 'black';
        this.ctx.lineWidth = 3;
        this.ctx.textAlign = 'center';

        this.ctx.font = 'bold 8px Arial';
        this.ctx.strokeText(name, pos.x, pos.y + radius + 25);
        this.ctx.fillText(name, pos.x, pos.y + radius + 25);

        if (this.config.DEBUG_MODE && state) {
            this.ctx.font = 'bold 10px Monospace';
            this.ctx.fillStyle = state === 'FLEE' ? '#ff3333' : state === 'LOOT' ? 'cyan' : '#aaa';
            this.ctx.fillText(`[${state}]`, pos.x, pos.y - radius - 15);
        }
        this.ctx.restore();
    }
    keysPressed: { [key: string]: boolean } = {};

    updateSpectatorMovement(dt: number) {
        const speed = 1500 * dt / this.currentScale; // Faster when zoomed out
        if (this.keysPressed['w'] || this.keysPressed['arrowup']) this.camY -= speed;
        if (this.keysPressed['s'] && !this.spectateMode()) { /* S is toggle, handled in keydown */ }
        if (this.keysPressed['arrowdown']) this.camY += speed; // S is occupied by toggle, use ArrowDown or map S differently if needed. 
        // Actually, let's allow S for movement if we are IN spectator mode, but we need to debounce the toggle.
        // For simplicity here, I'll map movement to Arrows + WASD, assuming S toggle is distinct.
        if (this.keysPressed['s'] || this.keysPressed['arrowdown']) this.camY += speed;

        if (this.keysPressed['a'] || this.keysPressed['arrowleft']) this.camX -= speed;
        if (this.keysPressed['d'] || this.keysPressed['arrowright']) this.camX += speed;
    }

    // --- NEW MINIMAP WITH TRAILS ---
    drawMinimap(w: number, h: number) {
        const size = 150;
        const margin = 20;
        const mapX = w - size - margin;
        const mapY = h - size - margin;
        this.ctx.save();
        this.ctx.translate(mapX, mapY);
        this.ctx.fillStyle = 'rgba(0, 0, 0, 0.6)';
        this.ctx.fillRect(0, 0, size, size);
        this.ctx.strokeStyle = '#555';
        this.ctx.lineWidth = 2;
        this.ctx.strokeRect(0, 0, size, size);

        const ms = this.config.MAP_SIZE;

        for (let pid in this.players) {
            if (this.isDead() && pid === this.myId()) continue;
            const p = this.players[pid];
            const vis = this.visualPlayers[pid];
            if (!p.body || p.body.length === 0 || !vis) continue;

            const isMe = pid === this.myId();

            // Draw Trail on Minimap
            this.ctx.beginPath();
            const startX = (vis.body[0].x / ms) * size;
            const startY = (vis.body[0].y / ms) * size;
            this.ctx.moveTo(startX, startY);

            // Skip some points for performance on minimap
            for (let i = 1; i < vis.body.length; i += 2) {
                const bx = (vis.body[i].x / ms) * size;
                const by = (vis.body[i].y / ms) * size;
                this.ctx.lineTo(bx, by);
            }

            this.ctx.lineWidth = isMe ? 2 : 1;
            this.ctx.strokeStyle = isMe ? 'rgba(0, 255, 255, 0.8)' : 'rgba(255, 50, 50, 0.5)';
            this.ctx.stroke();

            // Draw Head Dot
            const mx = (vis.x / ms) * size;
            const my = (vis.y / ms) * size;
            this.ctx.beginPath();
            this.ctx.arc(mx, my, isMe ? 3 : 2, 0, Math.PI * 2);
            this.ctx.fillStyle = isMe ? '#ffffff' : '#ff3333';
            this.ctx.fill();
        }
        this.ctx.restore();
    }

    drawLeaderboard(w: number) {
        if (this.leaderboard.length === 0) return;
        const boxW = 200;
        const boxX = w - boxW - 20;
        const boxY = 20;
        this.ctx.fillStyle = 'rgba(0,0,0,0.5)';
        this.ctx.fillRect(boxX, boxY, boxW, this.leaderboard.length * 25 + 10);
        this.ctx.fillStyle = 'white';
        this.ctx.font = 'bold 14px Arial';
        this.ctx.textAlign = 'left';
        this.leaderboard.forEach((entry, i) => {
            this.ctx.fillText(`${i + 1}. ${entry.name.substring(0, 12)}`, boxX + 10, boxY + 20 + i * 25);
            this.ctx.textAlign = 'right';
            this.ctx.fillText(entry.score.toString(), boxX + boxW - 10, boxY + 20 + i * 25);
            this.ctx.textAlign = 'left';
        });
    }

    @HostListener('window:resize')
    resizeCanvas() {
        this.canvasRef.nativeElement.width = window.innerWidth;
        this.canvasRef.nativeElement.height = window.innerHeight;
    }

    @HostListener('document:mousemove', ['$event'])
    onMouseMove(e: MouseEvent) {
        if (!this.myId() || this.isDead()) return;
        const center = { x: window.innerWidth / 2, y: window.innerHeight / 2 };
        const angle = Math.atan2(e.clientY - center.y, e.clientX - center.x);
        this.socketWrapper.emit('input_update', { angle });
    }

    @HostListener('document:keydown.space')
    startBoost() {
        if (this.myId() && !this.isDead()) this.socketWrapper.emit('boost_update', { boosting: true });
    }

    @HostListener('document:keyup.space')
    endBoost() {
        if (this.myId()) this.socketWrapper.emit('boost_update', { boosting: false });
    }

    // @HostListener('document:keydown.c')
    cheatBoost() {
        if (this.myId() && !this.isDead()) this.socketWrapper.emit('cheat_boost', { mass: 1000 });
    }

    @HostListener('document:mousedown')
    startBoostMouse() {
        if (this.myId() && !this.isDead()) this.socketWrapper.emit('boost_update', { boosting: true });
    }

    @HostListener('document:mouseup')
    endBoostMouse() {
        if (this.myId()) this.socketWrapper.emit('boost_update', { boosting: false });
    }

    @HostListener('window:keydown', ['$event'])
    onKeyDown(e: KeyboardEvent) {
        this.keysPressed[e.key.toLowerCase()] = true;
        
        if (e.key.toLowerCase() === 's') {
            // Toggle Spectator State
            const isSpectating = !this.spectateMode();
            this.spectateMode.set(isSpectating);
            
            if (isSpectating) {
                // ENTER SPECTATOR: Tell server to kill us, zoom out
                this.socketWrapper.emit('enter_spectator');
                this.targetScale = 0.2; 
                this.isDead.set(true); // Mark as dead locally so we don't try to render our own snake
            } else {
                // EXIT SPECTATOR: Ask to be born again
                this.socketWrapper.emit('request_respawn');
                this.targetScale = 1.0; 
                this.isDead.set(false);
            }
        }
        
        if (e.key.toLowerCase() === 'c') {
            this.cheatBoost();
        }
        
        if (e.code === 'Space') this.startBoost();
    }

    @HostListener('wheel', ['$event'])
    onWheel(e: WheelEvent) {
        if (!this.spectateMode()) return;
        e.preventDefault();
        
        // Increased zoom speed slightly for better control
        const zoomSpeed = 0.002;
        this.targetScale += e.deltaY * -zoomSpeed;
        
        // UPDATED LIMITS: 
        // 0.05 allows viewing ~20,000-40,000 units (entire map + void)
        // 5.0 allows viewing individual segments in extreme detail
        this.targetScale = Math.min(Math.max(0.05, this.targetScale), 5.0);
    }

    @HostListener('window:keyup', ['$event'])
    onKeyUp(e: KeyboardEvent) {
        this.keysPressed[e.key.toLowerCase()] = false;
        if (e.code === 'Space') this.endBoost();
    }
}
