import { api } from '../api.js';
import { startShell } from '../shell.js';
import { fillStatusRow, setText, toast } from '../ui.js';

api.get('/perception').then((d) => {
  setText('[data-field="camera-detail"]', d.camera.detail);
  fillStatusRow('recognition', d.recognition);
  setText('[data-field="identity"]', d.recognition.identity ?? 'Not available');
}).catch((e) => toast(e.message, 'error'));
startShell();
