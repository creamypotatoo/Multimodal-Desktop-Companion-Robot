import { api } from '../api.js';
import { mountEyes } from '../eyes.js';
import { onStatus, startShell } from '../shell.js';
import { $, formatDuration, setText } from '../ui.js';

const eyes = mountEyes();
api.get('/settings').then((s) => { eyes?.setColor(s.eye_color); eyes?.setSpeed(s.animation_speed); }).catch(() => {});

function describe(entry) {
  const { command, at, ...rest } = entry;
  const details = Object.entries(rest).map(([k, v]) => `${k.replace('_', ' ')}: ${v}`).join(', ');
  return details ? `${command} · ${details}` : command;
}

function renderHistory(history) {
  const list = $('[data-activity]');
  if (!history.length) return;
  list.replaceChildren(...history.slice(0, 8).map((entry) => {
    const li = document.createElement('li');
    const text = document.createElement('span');
    text.textContent = describe(entry);
    const time = document.createElement('time');
    const date = new Date(entry.at * 1000);
    time.dateTime = date.toISOString();
    time.textContent = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    li.append(text, time);
    return li;
  }));
}

onStatus((s) => {
  eyes?.setExpression(s.expression);
  setText('[data-field="state-label"]', s.conversation_active ? 'listening' : s.mode === 'sleep' ? 'asleep' : 'idle');
  setText('[data-field="mode"]', s.mode);
  setText('[data-field="expression"]', s.expression);
  setText('[data-field="volume"]', s.volume);
  setText('[data-field="uptime"]', formatDuration(s.uptime_s));
  renderHistory(s.history);
});
startShell();
