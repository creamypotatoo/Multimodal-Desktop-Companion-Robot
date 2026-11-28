// Canvas preview of Eli's eyes. Geometry and poses mirror display/config.py
// (EyeConfig) and display/expressions.py so the preview matches the TFT.
import { prefersReducedMotion } from './ui.js';

const W = 320, H = 240;
const GEOM = { eyeW: 78, eyeH: 96, spacing: 64, radius: 22, offsetY: -6, gazeX: 34, gazeY: 22, closed: 6 };
const BASE = { sx: 1, sy: 1, oy: 0, open: 1, lidTop: 0, tilt: 0, lidBottom: 0 };
const pose = (p) => ({ ...BASE, ...p });
const both = (p) => ({ left: pose(p), right: pose(p) });

// name -> {left, right, gaze:[x,y], sparkle, bounce, hop, breathe, hz, blink:[lo,hi]|null, wander}
const EXPRESSIONS = {
  idle: { ...both({ sy: 0.96 }), blink: [2.5, 6], wander: 0.35 },
  listening: { ...both({ sx: 1.08, sy: 1.12, oy: -0.03 }), blink: [3, 7], wander: 0.12 },
  thinking: { ...both({ sx: 0.96, sy: 0.92, lidTop: 0.16 }), gaze: [0, -0.35], blink: [3, 6], wander: 0.2,
    points: [[-0.75, -0.5], [0.75, -0.5], [0, -0.8], [0.55, -0.7]] },
  speaking: { ...both({ sx: 1.03, sy: 1.02, lidBottom: 0.18 }), bounce: 0.05, hz: 2.2, blink: [2.5, 5], wander: 0.2 },
  happy: { ...both({ sx: 1.06, oy: -0.04, lidBottom: 0.5 }), hop: 0.015, hz: 1.2, blink: [3, 6], wander: 0.12 },
  excited: { ...both({ sx: 1.16, sy: 1.2, oy: -0.05, lidBottom: 0.12 }), sparkle: 1, hop: 0.035, hz: 2.6, blink: [3, 6.5], wander: 0.15 },
  sad: { ...both({ sx: 0.98, sy: 0.84, oy: 0.14, lidTop: 0.22, tilt: 0.5 }), gaze: [0, 0.45], blink: [3.5, 7], wander: 0.12 },
  confused: { left: pose({ sx: 1.06, sy: 1.1, oy: -0.05 }), right: pose({ sx: 0.9, sy: 0.8, oy: 0.04, lidTop: 0.3, tilt: -0.25 }),
    gaze: [0.15, -0.1], blink: [2.5, 5], wander: 0.18 },
  sleepy: { ...both({ sy: 0.92, oy: 0.06, lidTop: 0.5, tilt: 0.12 }), gaze: [0, 0.3], blink: [3, 6], wander: 0.08 },
  sleeping: { ...both({ sx: 1.04, oy: 0.12, open: 0 }), breathe: 0.035, gaze: [0, 0.25], blink: null, wander: 0 },
};
const KEYS = Object.keys(BASE);
const rand = (lo, hi) => lo + Math.random() * (hi - lo);

export class EyePreview {
  constructor(canvas, { expression = 'idle', color = '#00E5FF', speed = 1 } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.label = canvas.parentElement?.querySelector('[data-eye-label]');
    this.color = color;
    this.speed = speed;
    this.reduced = prefersReducedMotion();
    this.cur = { left: pose({}), right: pose({}) };
    this.gaze = [0, 0];
    this.gazeTarget = [0, 0];
    this.blinkAt = 0; this.blinkStart = -1; this.wanderAt = 0;
    this.last = performance.now() / 1000;
    this.setExpression(expression);
    this.cur = structuredClone({ left: this.spec.left, right: this.spec.right });
    this._fit();
    window.addEventListener('resize', () => this._fit());
    this._loop = this._loop.bind(this);
    requestAnimationFrame(this._loop);
  }

  _fit() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const rect = this.canvas.getBoundingClientRect();
    const scale = (rect.width || W) / W;
    this.canvas.width = Math.round(W * scale * dpr);
    this.canvas.height = Math.round(H * scale * dpr);
    this.ctx.setTransform(scale * dpr, 0, 0, scale * dpr, 0, 0);
  }

  setExpression(name) {
    const key = EXPRESSIONS[name] ? name : 'idle';
    if (key === this.name) return;
    this.name = key;
    this.spec = EXPRESSIONS[key];
    const now = performance.now() / 1000;
    this.blinkAt = this.spec.blink ? now + rand(...this.spec.blink) / this.speed : Infinity;
    this.wanderAt = now;
    if (this.label) this.label.textContent = key;
  }

  setColor(color) { if (/^#[0-9a-f]{6}$/i.test(color)) this.color = color; }
  setSpeed(speed) { if (speed > 0) this.speed = speed; }
  setGaze(target) {
    if (target == null && this.manualGaze == null) return;
    this.manualGaze = target;
    if (target == null) this.wanderAt = 0;
  }

  _step(now, dt) {
    const s = this.spec, k = 1 - Math.exp(-dt * this.speed / 0.14), kg = 1 - Math.exp(-dt * this.speed / 0.07);
    for (const side of ['left', 'right']) {
      for (const key of KEYS) this.cur[side][key] += (s[side][key] - this.cur[side][key]) * k;
    }
    if (!this.reduced && s.wander && now >= this.wanderAt) {
      const bias = s.gaze || [0, 0];
      const pt = s.points ? s.points[Math.floor(Math.random() * s.points.length)]
        : [bias[0] + rand(-s.wander, s.wander) * 2, bias[1] + rand(-s.wander, s.wander)];
      this.gazeTarget = pt.map((v) => Math.max(-1, Math.min(1, v)));
      this.wanderAt = now + rand(1.5, 4) / this.speed;
    } else if (!s.wander || this.reduced) {
      this.gazeTarget = s.gaze || [0, 0];
    }
    const target = (!this.reduced && this.manualGaze) || this.gazeTarget;
    this.gaze = this.gaze.map((g, i) => g + (target[i] - g) * kg);
    // Blink: 0.09 close, 0.05 hold, 0.14 open (AnimationConfig).
    if (now >= this.blinkAt && this.blinkStart < 0) this.blinkStart = now;
    let blink = 1;
    if (this.blinkStart >= 0) {
      const t = (now - this.blinkStart) * this.speed;
      if (t < 0.09) blink = 1 - t / 0.09;
      else if (t < 0.14) blink = 0;
      else if (t < 0.28) blink = (t - 0.14) / 0.14;
      else { this.blinkStart = -1; this.blinkAt = s.blink ? now + rand(...s.blink) / this.speed : Infinity; }
    }
    return blink;
  }

  _drawEye(cx, cy, p, blink, t, mirror) {
    const ctx = this.ctx, s = this.spec;
    const w = GEOM.eyeW * p.sx;
    let h = GEOM.eyeH * p.sy * (1 + (s.bounce || 0) * Math.sin(t * Math.PI * 2 * (s.hz || 0)));
    const openH = h * Math.max(0, Math.min(1, p.open * blink));
    const y = cy + p.oy * GEOM.eyeH;
    ctx.fillStyle = this.color;
    ctx.shadowColor = this.color;
    ctx.shadowBlur = 18;
    if (openH < GEOM.closed + 1) { // closed: a soft line
      ctx.beginPath();
      ctx.roundRect(cx - w / 2, y - GEOM.closed / 2, w, GEOM.closed, GEOM.closed / 2);
      ctx.fill();
      ctx.shadowBlur = 0;
      return;
    }
    h = openH;
    ctx.beginPath();
    ctx.roundRect(cx - w / 2, y - h / 2, w, h, Math.min(GEOM.radius, h / 2, w / 2));
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.fillStyle = '#000';
    // Lids follow display/renderer.py EyeRenderer.layout().
    const y0 = y - h / 2, y1 = y + h / 2, x0 = cx - w / 2, x1 = cx + w / 2;
    if (p.lidTop > 0.005 || Math.abs(p.tilt) > 0.005) { // straight (tilted) upper lid
      const clamp = (v) => Math.min(Math.max(v, y0 - 3), y1 + 1);
      const inner = clamp(y0 + h * p.lidTop), outer = clamp(y0 + h * (p.lidTop + 0.5 * p.tilt));
      const [leftY, rightY] = mirror ? [outer, inner] : [inner, outer];
      ctx.beginPath();
      ctx.moveTo(x0 - 3, y0 - 3); ctx.lineTo(x1 + 3, y0 - 3);
      ctx.lineTo(x1 + 3, rightY); ctx.lineTo(x0 - 3, leftY);
      ctx.closePath(); ctx.fill();
    }
    if (p.lidBottom > 0.005) { // curved lower cut -> smiling arc
      const spread = Math.max(1, Math.min(w * 0.22, GEOM.spacing / 2 - 2));
      const top = y1 - h * p.lidBottom, bottom = y1 + h * 0.9;
      ctx.beginPath();
      ctx.ellipse(cx, (top + bottom) / 2, w / 2 + spread, (bottom - top) / 2, 0, 0, Math.PI * 2);
      ctx.fill();
    }
    if (s.sparkle) {
      const tw = 0.5 + 0.5 * Math.sin(t * 5 + (mirror ? Math.PI : 0));
      const r = w * 0.1 * s.sparkle * (0.8 + 0.2 * tw);
      const hy = Math.max(y0 + h * 0.3, y0 + h * (p.lidTop + Math.max(0, 0.5 * p.tilt)) + r + 2);
      if (hy + r < y1 - h * p.lidBottom) {
        ctx.fillStyle = '#fff';
        ctx.beginPath(); ctx.arc(x0 + w * 0.3, hy, r, 0, Math.PI * 2); ctx.fill();
        ctx.beginPath(); ctx.arc(x0 + w * 0.47, hy + r * 1.6, Math.max(1, r * 0.45), 0, Math.PI * 2); ctx.fill();
      }
    }
  }

  _loop(ms) {
    const now = ms / 1000, dt = Math.min(0.1, now - this.last);
    this.last = now;
    const blink = this._step(now, dt);
    const ctx = this.ctx, s = this.spec;
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, W, H);
    const t = this.reduced ? 0 : now * this.speed;
    const hop = (s.hop || 0) * GEOM.eyeH * Math.abs(Math.sin(t * Math.PI * (s.hz || 0)));
    const breathe = (s.breathe || 0) * GEOM.eyeH * Math.sin(t * 1.2);
    const cy = H / 2 + GEOM.offsetY + this.gaze[1] * GEOM.gazeY - hop + breathe;
    const dx = GEOM.spacing / 2 + GEOM.eyeW / 2, gx = this.gaze[0] * GEOM.gazeX;
    this._drawEye(W / 2 - dx + gx, cy, this.cur.left, blink, t, true);
    this._drawEye(W / 2 + dx + gx, cy, this.cur.right, blink, t, false);
    requestAnimationFrame(this._loop);
  }
}

export function mountEyes(options) {
  const canvas = document.querySelector('[data-eye-preview]');
  return canvas ? new EyePreview(canvas, options) : null;
}
