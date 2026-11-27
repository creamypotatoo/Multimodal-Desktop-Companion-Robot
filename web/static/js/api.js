// Small JSON client for /api/v1. Sends the CSRF token on every state change
// and sends the user to the login page when the session has expired.
const BASE = '/api/v1';
const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || '';

export class ApiError extends Error {
  constructor(message, { status = 0, code = 'network_error', field = null } = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.field = field;
  }
}

async function request(method, path, body) {
  const headers = { Accept: 'application/json' };
  const init = { method, headers, credentials: 'same-origin', cache: 'no-store' };
  if (method !== 'GET') {
    headers['X-CSRF-Token'] = csrf();
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
  }
  let response;
  try {
    response = await fetch(BASE + path, init);
  } catch {
    throw new ApiError('Cannot reach Eli. Check the connection.');
  }
  if (response.status === 401) {
    const next = encodeURIComponent(location.pathname);
    location.assign(`/login?next=${next}`);
    throw new ApiError('Session expired', { status: 401, code: 'unauthorized' });
  }
  let payload = null;
  try { payload = await response.json(); } catch { /* non-JSON error */ }
  if (!response.ok || !payload?.ok) {
    const err = payload?.error || {};
    throw new ApiError(err.message || `Request failed (${response.status})`,
      { status: response.status, code: err.code || 'error', field: err.field ?? null });
  }
  return payload.data;
}

export const api = {
  get: (path) => request('GET', path),
  put: (path, body) => request('PUT', path, body),
  post: (path, body) => request('POST', path, body ?? {}),
  patch: (path, body) => request('PATCH', path, body),
};
