// Countdown timer, stopwatch and Pomodoro. Pure client-side; nothing is sent
// to the robot. Timing uses absolute deadlines so background tabs stay accurate.
import { startShell } from '../shell.js';
import { $, $$, toast } from '../ui.js';

const CIRC = 2 * Math.PI * 54;
const pad = (n) => String(n).padStart(2, '0');
const mmss = (s) => { s = Math.max(0, Math.ceil(s)); return `${pad(Math.floor(s / 60))}:${pad(s % 60)}`; };

function chime() {
  try {
    const ctx = new AudioContext();
    [0, 0.25, 0.5].forEach((t) => {
      const osc = ctx.createOscillator(), gain = ctx.createGain();
      osc.frequency.value = 880;
      gain.gain.setValueAtTime(0.0001, ctx.currentTime + t);
      gain.gain.exponentialRampToValueAtTime(0.2, ctx.currentTime + t + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + t + 0.2);
      osc.connect(gain).connect(ctx.destination);
      osc.start(ctx.currentTime + t); osc.stop(ctx.currentTime + t + 0.22);
    });
    setTimeout(() => ctx.close(), 1200);
  } catch { /* audio not allowed: the toast still shows */ }
}

function setToggle(button, running) {
  $('use', button).setAttribute('href', running ? '#i-pause' : '#i-play');
  $('span', button).textContent = running ? 'Pause' : (button.dataset.resume === '1' ? 'Resume' : 'Start');
}

function setRing(ring, fraction) {
  $('.ring__bar', ring).style.strokeDashoffset = String(CIRC * (1 - Math.max(0, Math.min(1, fraction))));
}

// ---------- tabs ----------
const tabs = $$('[role="tab"]');
function selectTab(tab) {
  tabs.forEach((t) => {
    const on = t === tab;
    t.classList.toggle('is-active', on);
    t.setAttribute('aria-selected', String(on));
    t.tabIndex = on ? 0 : -1;
    $(`#${t.getAttribute('aria-controls')}`).hidden = !on;
  });
}
tabs.forEach((t, i) => {
  t.addEventListener('click', () => selectTab(t));
  t.addEventListener('keydown', (e) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
    if (!step) return;
    const next = tabs[(i + step + tabs.length) % tabs.length];
    next.focus(); selectTab(next);
  });
});

// ---------- countdown ----------
const timer = { total: 300, remaining: 300, deadline: null, raf: 0 };
const timerRing = $('[data-ring="timer"]'), timerDisplay = $('[data-display="timer"]');
const timerToggle = $('[data-timer="toggle"]');

function drawTimer() {
  if (timer.deadline) timer.remaining = (timer.deadline - performance.now()) / 1000;
  timerDisplay.textContent = mmss(timer.remaining);
  setRing(timerRing, timer.remaining / timer.total);
  if (timer.deadline && timer.remaining <= 0) {
    timer.deadline = null; timer.remaining = 0;
    timerToggle.dataset.resume = '0';
    setToggle(timerToggle, false);
    timerRing.classList.add('is-done');
    timerDisplay.textContent = '00:00';
    document.title = 'Timer done · Eli Control';
    chime(); toast("Time's up!");
    return;
  }
  if (timer.deadline) timer.raf = requestAnimationFrame(drawTimer);
}
function setTimer(seconds) {
  cancelAnimationFrame(timer.raf);
  timer.total = timer.remaining = Math.max(1, Math.min(seconds, 180 * 60));
  timer.deadline = null;
  timerToggle.dataset.resume = '0';
  timerRing.classList.remove('is-done');
  setToggle(timerToggle, false);
  drawTimer();
}
timerToggle.addEventListener('click', () => {
  if (timer.deadline) {
    cancelAnimationFrame(timer.raf);
    drawTimer(); timer.deadline = null;
    timerToggle.dataset.resume = '1';
    setToggle(timerToggle, false);
  } else {
    if (timer.remaining <= 0) setTimer(timer.total);
    timerRing.classList.remove('is-done');
    timer.deadline = performance.now() + timer.remaining * 1000;
    setToggle(timerToggle, true);
    drawTimer();
  }
});
$('[data-timer="reset"]').addEventListener('click', () => { setTimer(timer.total); document.title = 'Focus · Eli Control'; });
$$('[data-preset]').forEach((b) => b.addEventListener('click', () => {
  $$('[data-preset]').forEach((x) => x.classList.toggle('is-active', x === b));
  setTimer(Number(b.dataset.preset));
}));
$('[data-timer="set"]').addEventListener('click', () => {
  const m = Math.max(0, Math.min(180, Number($('[data-custom="min"]').value) || 0));
  const s = Math.max(0, Math.min(59, Number($('[data-custom="sec"]').value) || 0));
  if (m * 60 + s < 1) { toast('Enter a time of at least 1 second', 'error'); return; }
  $$('[data-preset]').forEach((x) => x.classList.remove('is-active'));
  setTimer(m * 60 + s);
});

// ---------- stopwatch ----------
const sw = { elapsed: 0, started: null, raf: 0, laps: [] };
const swDisplay = $('[data-display="stopwatch"]'), swToggle = $('[data-sw="toggle"]'), swLap = $('[data-sw="lap"]');
const swNow = () => sw.elapsed + (sw.started ? performance.now() - sw.started : 0);
const fmtSw = (ms) => `${pad(Math.floor(ms / 60000))}:${pad(Math.floor(ms / 1000) % 60)}.${pad(Math.floor(ms / 10) % 100)}`;
function drawSw() { swDisplay.textContent = fmtSw(swNow()); if (sw.started) sw.raf = requestAnimationFrame(drawSw); }
swToggle.addEventListener('click', () => {
  if (sw.started) { sw.elapsed = swNow(); sw.started = null; cancelAnimationFrame(sw.raf); swToggle.dataset.resume = '1'; }
  else { sw.started = performance.now(); drawSw(); }
  setToggle(swToggle, Boolean(sw.started));
  swLap.disabled = !sw.started;
});
swLap.addEventListener('click', () => {
  const total = swNow(), prev = sw.laps.at(-1) ?? 0;
  sw.laps.push(total);
  const li = document.createElement('li');
  li.innerHTML = '<span></span><span></span>';
  li.children[0].textContent = `Lap ${sw.laps.length}`;
  li.children[1].textContent = `${fmtSw(total - prev)} · ${fmtSw(total)}`;
  $('[data-laps]').prepend(li);
});
$('[data-sw="reset"]').addEventListener('click', () => {
  cancelAnimationFrame(sw.raf);
  Object.assign(sw, { elapsed: 0, started: null, laps: [] });
  swToggle.dataset.resume = '0';
  setToggle(swToggle, false); swLap.disabled = true;
  $('[data-laps]').replaceChildren(); drawSw();
});

// ---------- pomodoro ----------
const PHASES = { focus: 25 * 60, short: 5 * 60, long: 15 * 60 };
const LABEL = { focus: 'Focus', short: 'Short break', long: 'Long break' };
const today = new Date().toDateString();
const stored = JSON.parse(localStorage.getItem('eli.pomodoro') || '{}');
const pomo = { phase: 'focus', round: 1, done: stored.day === today ? stored.done || 0 : 0,
  remaining: PHASES.focus, deadline: null, raf: 0 };
const pomoRing = $('[data-ring="pomodoro"]'), pomoDisplay = $('[data-display="pomodoro"]');
const pomoToggle = $('[data-pomo-btn="toggle"]');

function drawPomoStatic() {
  $('[data-pomo="phase"]').textContent = LABEL[pomo.phase];
  $('[data-pomo="phase"]').classList.toggle('is-break', pomo.phase !== 'focus');
  pomoRing.classList.toggle('ring--break', pomo.phase !== 'focus');
  $('[data-pomo="round"]').textContent = pomo.round;
  $('[data-pomo="done"]').textContent = pomo.done;
}
function nextPhase(completed) {
  if (pomo.phase === 'focus') {
    if (completed) { pomo.done += 1; localStorage.setItem('eli.pomodoro', JSON.stringify({ day: today, done: pomo.done })); }
    pomo.phase = pomo.round >= 4 ? 'long' : 'short';
  } else {
    pomo.round = pomo.phase === 'long' ? 1 : pomo.round + 1;
    pomo.phase = 'focus';
  }
  pomo.remaining = PHASES[pomo.phase];
  drawPomoStatic();
}
function drawPomo() {
  if (pomo.deadline) pomo.remaining = (pomo.deadline - performance.now()) / 1000;
  if (pomo.deadline && pomo.remaining <= 0) {
    const finished = LABEL[pomo.phase];
    nextPhase(true);
    chime(); toast(`${finished} complete. ${LABEL[pomo.phase]} next.`);
    pomo.deadline = performance.now() + pomo.remaining * 1000; // auto-continue
  }
  pomoDisplay.textContent = mmss(pomo.remaining);
  setRing(pomoRing, pomo.remaining / PHASES[pomo.phase]);
  if (pomo.deadline) pomo.raf = requestAnimationFrame(drawPomo);
}
function pausePomo() { cancelAnimationFrame(pomo.raf); drawPomo(); pomo.deadline = null; }
pomoToggle.addEventListener('click', () => {
  if (pomo.deadline) { pausePomo(); pomoToggle.dataset.resume = '1'; }
  else { pomo.deadline = performance.now() + pomo.remaining * 1000; drawPomo(); }
  setToggle(pomoToggle, Boolean(pomo.deadline));
});
$('[data-pomo-btn="skip"]').addEventListener('click', () => {
  const running = Boolean(pomo.deadline);
  pausePomo(); nextPhase(false);
  if (running) pomo.deadline = performance.now() + pomo.remaining * 1000;
  drawPomo();
});
$('[data-pomo-btn="reset"]').addEventListener('click', () => {
  pausePomo();
  Object.assign(pomo, { phase: 'focus', round: 1, remaining: PHASES.focus });
  pomoToggle.dataset.resume = '0';
  setToggle(pomoToggle, false); drawPomoStatic(); drawPomo();
});

setTimer(300);
drawSw();
drawPomoStatic();
drawPomo();
startShell();
