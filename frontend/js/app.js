/**
 * app.js — App shell wiring
 *
 * Bootstraps all components, starts the poller, and wires up:
 *   - Header clock + health badge + stats
 *   - Map (poles, edges, fault highlights)
 *   - Ticket list + drawer
 *   - Simulator panel
 *
 * No JSX. Plain ES5/ES6 module-style code.
 */

(function () {
  'use strict';

  // ─── Configure API base from meta tag or env ──────────────────────────
  // In production, nginx proxies /api/* to the backend. In dev, backend is :8000.
  // We read from a <meta name="api-base"> tag in index.html if present,
  // otherwise fall back to same-origin /api, then localhost:8000.
  const metaBase = document.querySelector('meta[name="api-base"]');
  if (metaBase) {
    window.HUMBUG_API_BASE = metaBase.getAttribute('content');
  } else if (window.location.port === '80' || window.location.port === '') {
    // Served by nginx on standard HTTP port — API is proxied at /api
    window.HUMBUG_API_BASE = '/api';
  } else if (window.location.port !== '8000') {
    // Local dev server (e.g. python -m http.server 3000) — backend is on :8000
    window.HUMBUG_API_BASE = 'http://localhost:8000';
  }
  // port === '8000': already defaulted to http://localhost:8000 in api.js

  // ─── Clock ─────────────────────────────────────────────────────────────
  function tickClock() {
    const el = document.getElementById('app-clock');
    if (el) {
      const now = new Date();
      el.textContent = now.toLocaleTimeString('en-IN', { hour12: false });
    }
  }
  tickClock();
  setInterval(tickClock, 1000);

  // ─── Health badge ──────────────────────────────────────────────────────
  function updateHealthBadge(openTickets, connected) {
    const badge = document.getElementById('system-health');
    const label = badge && badge.querySelector('.health-label');
    if (!badge || !label) return;

    badge.className = 'health-badge';
    if (!connected) {
      badge.classList.add('health-warn');
      label.textContent = 'BACKEND OFFLINE';
      return;
    }
    if (openTickets === 0) {
      badge.classList.add('health-ok');
      label.textContent = 'ALL CLEAR';
    } else if (openTickets <= 3) {
      badge.classList.add('health-warn');
      label.textContent = `${openTickets} ACTIVE FAULT${openTickets > 1 ? 'S' : ''}`;
    } else {
      badge.classList.add('health-crit');
      label.textContent = `${openTickets} ACTIVE FAULTS`;
    }
  }

  // ─── Header stats ──────────────────────────────────────────────────────
  function updateHeaderStats(stats) {
    const faultEl = document.getElementById('hdr-open-tickets');
    const poles   = document.getElementById('hdr-pole-count');
    // Display active fault locations count (not red pole count)
    const activeFaults = stats.active_fault_count ?? stats.open_tickets ?? 0;
    if (faultEl) faultEl.textContent = activeFaults;
    if (poles)   poles.textContent   = stats.pole_count ?? '\u2014';
    updateHealthBadge(activeFaults, true);
  }

  // ─── Tickets badge ─────────────────────────────────────────────────────
  function updateTicketsBadge(tickets) {
    const badge = document.getElementById('tickets-badge');
    if (!badge) return;
    const open = tickets.filter(t => !['verified','closed'].includes(t.status)).length;
    badge.textContent = open;
    badge.classList.toggle('zero', open === 0);
  }

  // ─── Boot sequence ─────────────────────────────────────────────────
  async function boot() {
    // 1. Mount map
    HumbugMap.init('map-container', { preferCanvas: true });

    // Load transformers so we can register their positions for edge drawing
    try {
      const transformers = await Api.getTransformers();
      transformers.forEach(t => HumbugMap.registerTransformer(t.dt_id, t.lat, t.lon));
    } catch (e) {
      console.warn('Could not load transformers:', e.message);
    }

    // 2. Load static topology ONCE — positions, DT/feeder/ward/pincode never change.
    //    This initialises all 2700+ markers; subsequent polls only update colors.
    try {
      const topology = await Api.getNetworkTopology();
      HumbugMap.initTopology(topology);
    } catch (e) {
      console.warn('Could not load topology; falling back to /poles:', e.message);
      // Fallback: use the old /poles endpoint so map still renders if new endpoint missing
      try {
        const poles = await Api.getPoles();
        HumbugMap.updatePoles(poles);
      } catch (e2) {
        console.warn('Could not load poles either:', e2.message);
      }
    }

    // 3. Mount simulator panel
    HumbugSimulator.render('simulator-body');

    // 4. Mount ticket list + drawer
    HumbugTickets.render('tickets-list-body', 'drawer-body');

    // 5. Subscribe to poller channels
    //    poleStatuses is a lightweight {pole_id: status} dict polled every 3s —
    //    map.js only calls setStyle on markers whose status changed, not re-render all.
    HumbugPoller.subscribe('poleStatuses', (statuses) => {
      HumbugMap.updatePoleStatuses(statuses);
    });

    HumbugPoller.subscribe('edges', (edges) => {
      HumbugMap.updateEdges(edges);
    });

    HumbugPoller.subscribe('activeLoadShed', (outages) => {
      HumbugMap.updateActiveLoadShed(outages);
    });

    HumbugPoller.subscribe('tickets', (tickets) => {
      updateTicketsBadge(tickets);
      HumbugMap.updateFaultHighlights(tickets, []);
    });

    HumbugPoller.subscribe('stats', (stats) => {
      updateHeaderStats(stats);
      updateHealthBadge(stats.open_tickets, true);
    });

    HumbugPoller.subscribe('connected', (connected) => {
      if (!connected) {
        updateHealthBadge(0, false);
      }
    });

    // 6. Start polling
    HumbugPoller.start();

    // Initial edge load
    try {
      const edges = await Api.getEdges();
      HumbugMap.updateEdges(edges);
    } catch (e) {
      console.warn('Could not load network edges:', e.message);
    }

    console.log('[Humbug] Boot complete. Polling started.');
  }


  // Wait for DOM
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
