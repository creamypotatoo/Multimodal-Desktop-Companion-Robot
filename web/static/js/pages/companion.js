import { api } from '../api.js';
import { mountEyes } from '../eyes.js';
import { onStatus, refresh, startShell } from '../shell.js';
import { $, busy, fillStatusRow, setBadge, setText, toast } from '../ui.js';

const eyes = mountEyes();
const button = $('[data-action="conversation"]');
let active = false;
let mode = null;
let integrated = false;

api.get('/settings').then((s) => { eyes?.setColor(s.eye_color); eyes?.setSpeed(s.animation_speed); }).catch(() => {});

function renderConversation(conversation) {
  active = conversation.active;
  setText('[data-field="conv-state"]', conversation.state);
  setBadge($('[data-conversation-badge]'), conversation.availability);
  button.textContent = active ? 'End conversation' : 'Start conversation';
  button.classList.toggle('is-on', active);
  button.setAttribute('aria-pressed', String(active));
}

function render(data) {
  integrated = data.integrated;
  renderConversation(data.conversation);
  fillStatusRow('face_recognition', data.face_recognition);
  fillStatusRow('microphone', data.microphone);
  fillStatusRow('ai', data.ai);
}

function renderModeHint() {
  const blocked = mode && mode !== 'companion' && !active;
  button.setAttribute('aria-disabled', String(Boolean(blocked)));
  setText('[data-field="conv-hint"]', blocked
    ? `Conversations need companion mode (currently ${mode}). Change it in Settings.`
    : integrated ? 'Starts or ends a session in the unified runtime.'
      : 'This button simulates a session. The legacy voice pipeline runs separately.');
}

button.addEventListener('click', () => busy(button, async () => {
  const data = await api.post('/conversation', { action: active ? 'end' : 'start' });
  render(data);
  toast(data.conversation.active ? 'Conversation requested' : 'Conversation ended');
  refresh();
}));

onStatus((s) => {
  mode = s.mode;
  eyes?.setExpression(s.expression);
  renderConversation({ active: s.conversation_active, state: s.state, availability: s.capabilities.conversation });
  if (s.runtime) {
    integrated = true;
    fillStatusRow('ai', s.runtime.ai);
    fillStatusRow('microphone', s.runtime.microphone);
  }
  renderModeHint();
});
api.get('/companion').then((d) => { render(d); renderModeHint(); }).catch((e) => toast(e.message, 'error'));
startShell();
