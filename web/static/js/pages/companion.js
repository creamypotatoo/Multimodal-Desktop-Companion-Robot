import { api } from '../api.js';
import { mountEyes } from '../eyes.js';
import { onStatus, refresh, startShell } from '../shell.js';
import { $, busy, fillStatusRow, setText, toast } from '../ui.js';

const eyes = mountEyes();
const button = $('[data-action="conversation"]');
let active = false;
let mode = null;

api.get('/settings').then((s) => { eyes?.setColor(s.eye_color); eyes?.setSpeed(s.animation_speed); }).catch(() => {});

function render(data) {
  active = data.conversation.active;
  setText('[data-field="conv-state"]', data.conversation.state);
  button.textContent = active ? 'End conversation' : 'Start conversation';
  button.classList.toggle('is-on', active);
  button.setAttribute('aria-pressed', String(active));
  fillStatusRow('face_recognition', data.face_recognition);
  fillStatusRow('microphone', data.microphone);
  fillStatusRow('ai', data.ai);
}

function renderModeHint() {
  const blocked = mode && mode !== 'companion' && !active;
  button.setAttribute('aria-disabled', String(Boolean(blocked)));
  setText('[data-field="conv-hint"]', blocked
    ? `Conversations need companion mode (currently ${mode}). Change it in Settings.`
    : 'The voice pipeline (main.py) keeps running independently. This button only simulates a session.');
}

button.addEventListener('click', () => busy(button, async () => {
  const data = await api.post('/conversation', { action: active ? 'end' : 'start' });
  render(data);
  toast(data.conversation.active ? 'Simulated conversation started' : 'Conversation ended');
  refresh();
}));

onStatus((s) => {
  mode = s.mode;
  eyes?.setExpression(s.expression);
  renderModeHint();
});
api.get('/companion').then((d) => { render(d); renderModeHint(); }).catch((e) => toast(e.message, 'error'));
startShell();
