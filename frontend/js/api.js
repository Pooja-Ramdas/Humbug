/**
 * api.js — Humbug API client
 *
 * All backend communication lives here. Components call these functions;
 * nothing else touches fetch directly.
 *
 * Live-update strategy: short-interval polling (3s for poles+tickets,
 * 10s for edges). The backend is REST-only; polling gives us 40× headroom
 * against the 120s p95 detection-to-UI target.
 */

const API_BASE = window.HUMBUG_API_BASE || (() => {
  if (window.location.protocol === 'file:') return 'http://localhost:8000';
  // If served via Nginx proxy (e.g. port 80 or standard reverse proxy setup)
  if (window.location.port === '' || window.location.port === '80' || window.location.port === '443') {
    return window.location.origin + '/api';
  }
  return 'http://localhost:8000';
})();

// ─── Internal fetch wrapper ───────────────────────────────────────────────

async function apiFetch(path, options = {}) {
  const url = `${API_BASE}${path}`;
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    let detail = text;
    try { detail = JSON.parse(text).detail || text; } catch (_) {}
    throw new Error(`${res.status} ${detail}`);
  }
  return res.json();
}

// ─── Network / poles ─────────────────────────────────────────────────────

const Api = {
  health:       () => apiFetch('/health'),
  stats:        () => apiFetch('/stats'),

  getPoles:     () => apiFetch('/poles'),
  getPole:      (id) => apiFetch(`/poles/${id}`),

  getEdges:     () => apiFetch('/network/edges'),
  getTransformers: () => apiFetch('/transformers'),
  getFeeders:   () => apiFetch('/feeders'),
  getSubstations: () => apiFetch('/substations'),

  // ─── Tickets ───────────────────────────────────────────────────────────
  getTickets:   (status) => apiFetch(`/tickets${status ? `?status=${status}` : ''}`),
  getTicket:    (id) => apiFetch(`/tickets/${id}`),
  acknowledgeTicket: (id) => apiFetch(`/tickets/${id}/acknowledge`, { method: 'POST', body: '{}' }),
  assignCrew:   (id) => apiFetch(`/tickets/${id}/assign-crew`,  { method: 'POST', body: '{}' }),
  resolveTicket:(id) => apiFetch(`/tickets/${id}/resolve`,     { method: 'POST', body: '{}' }),

  // ─── Simulator ─────────────────────────────────────────────────────────
  simulateFault: (type, target_id) =>
    apiFetch('/simulate/fault', { method: 'POST', body: JSON.stringify({ type, target_id }) }),

  simulateRestore: (type, target_id) =>
    apiFetch('/simulate/restore', { method: 'POST', body: JSON.stringify({ type, target_id }) }),

  simulateNoise: (noise_type, target_id, scope, duration_minutes) =>
    apiFetch('/simulate/noise', {
      method: 'POST',
      body: JSON.stringify({ noise_type, target_id, scope, duration_minutes }),
    }),

  getActiveFaults: () => apiFetch('/simulate/active'),

  // ─── Scheduled outages / Load Shedding ──────────────────────────────────
  getScheduledOutages: () => apiFetch('/scheduled-outages'),
  createScheduledOutage: (scope, target_id, start_ts, end_ts, reason) =>
    apiFetch('/scheduled-outages', {
      method: 'POST',
      body: JSON.stringify({ scope, target_id, start_ts, end_ts, reason }),
    }),
  simulateLoadShed: (scope, target_id, duration_minutes) =>
    apiFetch('/api/simulate-load-shed', {
      method: 'POST',
      body: JSON.stringify({ scope, target_id, duration_minutes }),
    }),
  getActiveLoadShed: () => apiFetch('/api/active-load-shed'),
  endLoadShed: (id) => apiFetch(`/api/end-load-shed/${id}`, { method: 'POST', body: '{}' }),

  // ─── Manual detection trigger ─────────────────────────────────────────
  triggerDetection: () => apiFetch('/detect', { method: 'POST' }),
};

// ─── Poller ───────────────────────────────────────────────────────────────
/**
 * HumbugPoller — coordinates all polling loops.
 * Components subscribe to named channels; the poller batches and dedups calls.
 *
 * Usage:
 *   HumbugPoller.subscribe('poles', (data) => { ... });
 *   HumbugPoller.start();
 */
const HumbugPoller = (() => {
  const FAST_INTERVAL_MS   = 3000;   // poles + tickets
  const SLOW_INTERVAL_MS   = 10000;  // edges (topology rarely changes)

  const subscribers = {};   // channel -> [callback, ...]
  let fastTimer = null;
  let slowTimer = null;
  let running = false;

  function subscribe(channel, cb) {
    if (!subscribers[channel]) subscribers[channel] = [];
    subscribers[channel].push(cb);
  }

  function unsubscribe(channel, cb) {
    if (!subscribers[channel]) return;
    subscribers[channel] = subscribers[channel].filter(fn => fn !== cb);
  }

  function emit(channel, data) {
    (subscribers[channel] || []).forEach(cb => { try { cb(data); } catch (e) { console.error(e); } });
  }

  async function pollFast() {
    const safeFetch = (promise, fallback = null) => promise.catch(err => {
      console.warn('[poller:fast-fetch-error]', err.message);
      return fallback;
    });

    try {
      const [poles, tickets, stats, activeFaults, activeLoadShed] = await Promise.all([
        safeFetch(Api.getPoles()),
        safeFetch(Api.getTickets()),
        safeFetch(Api.stats()),
        safeFetch(Api.getActiveFaults(), []),
        safeFetch(Api.getActiveLoadShed(), []),
      ]);

      if (poles) emit('poles', poles);
      if (tickets) emit('tickets', tickets);
      if (stats) emit('stats', stats);
      if (activeFaults) emit('activeFaults', activeFaults);
      if (activeLoadShed) emit('activeLoadShed', activeLoadShed);
      
      emit('connected', poles !== null);
    } catch (err) {
      console.warn('[poller:fast]', err.message);
      emit('connected', false);
    }
  }

  async function pollSlow() {
    try {
      const edges = await Api.getEdges();
      emit('edges', edges);
    } catch (err) {
      console.warn('[poller:slow]', err.message);
    }
  }

  function start() {
    if (running) return;
    running = true;
    pollFast();
    pollSlow();
    fastTimer = setInterval(pollFast, FAST_INTERVAL_MS);
    slowTimer = setInterval(pollSlow, SLOW_INTERVAL_MS);
  }

  function stop() {
    running = false;
    clearInterval(fastTimer);
    clearInterval(slowTimer);
  }

  // Force immediate refresh (e.g. after simulator action)
  function refresh() { return pollFast(); }

  return { subscribe, unsubscribe, start, stop, refresh };
})();

// ─── Toast helper (used by multiple modules) ─────────────────────────────
function showToast(msg, type = 'info', durationMs = 4000) {
  const icons = { ok: '✓', err: '✗', warn: '⚠', info: 'ℹ' };
  const container = document.getElementById('toast-container');
  if (!container) return;

  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.innerHTML = `<span class="toast-icon">${icons[type] || 'ℹ'}</span><span>${msg}</span>`;
  container.appendChild(el);

  setTimeout(() => {
    el.style.transition = 'opacity 0.3s';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 300);
  }, durationMs);
}

// ─── Format helpers ──────────────────────────────────────────────────────
function fmtAge(ts) {
  if (!ts) return '—';
  const secs = Math.floor(Date.now() / 1000 - ts);
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

function fmtTime(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleTimeString('en-IN', { hour12: false });
}

function fmtDateTime(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString('en-IN', { hour12: false, timeZoneName: 'short' });
}

function confidenceClass(c) {
  if (c >= 0.75) return 'conf-high';
  if (c >= 0.50) return 'conf-medium';
  return 'conf-low';
}

function statusLabel(s) {
  const labels = {
    detected: 'DETECTED', acknowledged: 'ACK', crew_assigned: 'CREW SENT',
    resolved: 'RESOLVED', verified: 'VERIFIED', closed: 'CLOSED',
  };
  return labels[s] || s.toUpperCase();
}
