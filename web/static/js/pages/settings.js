import { api } from '../api.js';
import { mountEyes } from '../eyes.js';
import { refresh, startShell, updateMode } from '../shell.js';
import { $, $$, bindRange, capitalize, setText, toast } from '../ui.js';

const eyes = mountEyes();
const MODE_HINTS = {
  companion: 'Conversations can be started. Movement is locked.',
  standby: 'Normal eyes. Conversation and movement are idle.',
  movement: 'Allows manual movement when safety checks pass.',
  sleep: 'Motion stops and the eyes sleep. Expression changes are blocked.',
};

const fail = (err) => toast(err.message, 'error');

const volume = bindRange($('[data-input="volume"]'), (value) =>
  api.put('/volume', { volume: value }).catch(fail), { format: (v) => `${v}%` });

const animSpeed = bindRange($('[data-input="animation_speed"]'), (value) =>
  api.patch('/settings', { animation_speed: value }).then(apply).catch(fail),
{ format: (v) => `${Number(v).toFixed(1)}×` });
$('[data-input="animation_speed"]').addEventListener('input', (e) => eyes?.setSpeed(Number(e.target.value)));

function radioGroup(host, values, current, onPick) {
  host.replaceChildren(...values.map((value) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = host.classList.contains('chips') ? 'chip' : '';
    b.setAttribute('role', 'radio');
    b.setAttribute('aria-checked', String(value === current));
    b.tabIndex = value === current ? 0 : -1;
    b.textContent = value;
    b.dataset.value = value;
    b.addEventListener('click', () => onPick(value));
    return b;
  }));
  host.onkeydown = (e) => { // roving focus for arrow keys
    const items = $$('[role="radio"]', host);
    const i = items.indexOf(document.activeElement);
    if (i < 0) return;
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
    if (!step) return;
    e.preventDefault();
    items[(i + step + items.length) % items.length].focus();
  };
}

function check(host, current) {
  $$('[role="radio"]', host).forEach((b) => {
    const on = b.dataset.value === current;
    b.setAttribute('aria-checked', String(on));
    b.tabIndex = on ? 0 : -1;
  });
}

function markColor(hex) {
  $$('[data-color]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.color.toUpperCase() === hex)));
  $('[data-input="eye_color"]').value = hex.toLowerCase();
}

function apply(s) {
  eyes?.setColor(s.eye_color);
  eyes?.setSpeed(s.animation_speed);
  eyes?.setExpression(s.expression);
  markColor(s.eye_color);
  animSpeed.set(s.animation_speed);
  volume.set(s.volume);
  check($('[data-expressions]'), s.expression);
  check($('[data-modes]'), s.mode);
  setText('[data-field="mode-hint"]', MODE_HINTS[s.mode] || '');
  updateMode(s.mode);
}

async function setColor(hex) {
  eyes?.setColor(hex);
  try { apply(await api.patch('/settings', { eye_color: hex })); } catch (err) { fail(err); }
}

$$('[data-color]').forEach((b) => b.addEventListener('click', () => setColor(b.dataset.color)));
const picker = $('[data-input="eye_color"]');
picker.addEventListener('input', () => eyes?.setColor(picker.value));
picker.addEventListener('change', () => setColor(picker.value.toUpperCase()));

async function init() {
  const [settings, expr, modes] = await Promise.all([api.get('/settings'), api.get('/expression'), api.get('/mode')]);
  radioGroup($('[data-expressions]'), expr.expressions, settings.expression, async (value) => {
    try {
      await api.put('/expression', { expression: value });
      apply(await api.get('/settings'));
    } catch (err) { fail(err); }
  });
  radioGroup($('[data-modes]'), modes.modes, settings.mode, async (value) => {
    try {
      const r = await api.put('/mode', { mode: value });
      apply(await api.get('/settings'));
      if (r.mode !== r.previous) toast(`${capitalize(r.mode)} mode (simulated)`);
      refresh();
    } catch (err) { fail(err); }
  });
  apply(settings);
}
init().catch(fail);
startShell();
