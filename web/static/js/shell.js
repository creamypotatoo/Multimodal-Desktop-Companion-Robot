// Shared page shell: polls /status, drives the connection and mode pills and
// notifies page modules. Polling pauses while the tab is hidden.
import { api } from './api.js';
import { $ } from './ui.js';

const POLL_MS = 4000;
const listeners = new Set();
let latest = null;
let timer = null;

function setConnection(state, text) {
  const pill = $('#conn-pill');
  if (!pill) return;
  pill.dataset.state = state;
  $('.pill__text', pill).textContent = text;
}

export function updateMode(mode) {
  const pill = $('#mode-pill');
  if (pill && mode) {
    $('.pill__text', pill).textContent = mode;
    pill.title = `Operating mode: ${mode} (simulated)`;
  }
}

export async function refresh() {
  try {
    latest = await api.get('/status');
    setConnection('online', `Online · ${latest.backend}`);
    updateMode(latest.mode);
    listeners.forEach((fn) => fn(latest));
  } catch (err) {
    if (err.status !== 401) setConnection('offline', 'Offline');
  }
  return latest;
}

function schedule() {
  clearTimeout(timer);
  if (!document.hidden) timer = setTimeout(async () => { await refresh(); schedule(); }, POLL_MS);
}

export function onStatus(fn) {
  listeners.add(fn);
  if (latest) fn(latest);
}

export function startShell() {
  refresh().then(schedule);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refresh().then(schedule);
    else clearTimeout(timer);
  });
}
