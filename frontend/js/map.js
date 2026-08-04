/**
 * map.js — Leaflet map component
 *
 * Renders:
 *   - Poles as circle markers, coloured by status
 *   - Network edges as polylines (span=solid, inferred=dashed, dim)
 *   - Fault edges highlighted red
 *   - Popup on pole click: id, DT, status, last heartbeat
 *   - "Fly to fault" when a ticket is selected
 *
 * All state (poles, edges) comes from HumbugPoller subscriptions set up by
 * app.js; map.js exposes a HumbugMap object with update methods.
 */

const HumbugMap = (() => {
  let _map = null;
  let _poleLayer = null;      // L.LayerGroup for pole circles
  let _edgeLayer = null;      // L.LayerGroup for topology edges
  let _faultLayer = null;     // L.LayerGroup for fault-highlighted edges/markers

  const _poleMarkers = {};    // pole_id -> L.CircleMarker
  const _edgeLines   = {};    // `${from}-${to}` -> L.Polyline

  // Colours match theme.css
  const COLORS = {
    normal:       '#22c55e',
    fault:        '#ef4444',
    load_shedding:'#6b7280',
    unknown:      '#374151',
  };

  function init(containerId) {
    _map = L.map(containerId, {
      center: [12.9716, 77.5946],  // Bangalore city centre
      zoom: 13,
      zoomControl: true,
      attributionControl: true,
    });

    // CartoDB dark_matter — free, keyless, matches our dark theme
    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_matter/{z}/{x}/{y}{r}.png', {
      attribution: '© <a href="https://www.openstreetmap.org/">OSM</a> © <a href="https://carto.com/">CARTO</a>',
      subdomains: 'abcd',
      maxZoom: 19,
    }).addTo(_map);

    _edgeLayer  = L.layerGroup().addTo(_map);
    _faultLayer = L.layerGroup().addTo(_map);
    _poleLayer  = L.layerGroup().addTo(_map);

    return _map;
  }

  // ─── Pole rendering ──────────────────────────────────────────────────

  function _poleStyle(status) {
    const base = { weight: 1, opacity: 0.9, fillOpacity: 0.85 };
    switch (status) {
      case 'normal':
        return { ...base, radius: 4, color: '#15803d', fillColor: COLORS.normal };
      case 'fault':
        return { ...base, radius: 5.5, color: '#991b1b', fillColor: COLORS.fault,
                 fillOpacity: 1, weight: 1.5 };
      case 'load_shedding':
        return { ...base, radius: 4, color: '#4b5563', fillColor: COLORS.load_shedding };
      case 'unknown':
      default:
        return { ...base, radius: 3.5, color: '#4b5563', fillColor: 'transparent',
                 fillOpacity: 0, dashArray: '3 2' };
    }
  }

  function _polePopupHtml(pole) {
    const statusClass = `status-${pole.status}`;
    const lastSeen = pole.last_seen ? fmtAge(pole.last_seen) : 'never';
    const statusText = pole.status === 'load_shedding' ? 'Load shedding'
                     : pole.status === 'unknown' ? 'No device' : pole.status;
    return `<div class="pole-popup">
      <div class="pp-id">${pole.pole_id}</div>
      <table>
        <tr><td>Status</td><td class="${statusClass}">${statusText.toUpperCase()}</td></tr>
        <tr><td>DT</td><td>${pole.dt_id || '—'}</td></tr>
        <tr><td>Feeder</td><td>${pole.feeder_id || '—'}</td></tr>
        <tr><td>Ward</td><td>${pole.ward || '—'}</td></tr>
        <tr><td>Pincode</td><td>${pole.pincode || '—'}</td></tr>
        <tr><td>Last seen</td><td>${lastSeen}</td></tr>
        ${pole.seq_on_line ? `<tr><td>Seq on line</td><td>${pole.seq_on_line}</td></tr>` : ''}
      </table>
    </div>`;
  }

  function updatePoles(poles) {
    const incoming = new Set(poles.map(p => p.pole_id));

    // Remove stale markers
    for (const pid of Object.keys(_poleMarkers)) {
      if (!incoming.has(pid)) {
        _poleMarkers[pid].remove();
        delete _poleMarkers[pid];
      }
    }

    for (const pole of poles) {
      const style = _poleStyle(pole.status);
      if (_poleMarkers[pole.pole_id]) {
        const marker = _poleMarkers[pole.pole_id];
        marker.setStyle(style);
        if (pole.status === 'fault') {
          // Pulsing glow effect for fault poles via the DOM
          marker.getElement() && marker.getElement().classList.add('fault-pulse');
        } else {
          marker.getElement() && marker.getElement().classList.remove('fault-pulse');
        }
        // Update popup if open
        marker._popup && marker.setPopupContent(_polePopupHtml(pole));
      } else {
        const marker = L.circleMarker([pole.lat, pole.lon], style);
        marker.bindPopup(_polePopupHtml(pole), { maxWidth: 220, className: 'humbug-popup' });
        marker.on('click', () => marker.openPopup());
        marker.addTo(_poleLayer);
        _poleMarkers[pole.pole_id] = marker;
      }
    }

    // Fit bounds on first load only
    if (Object.keys(_poleMarkers).length > 0 && !_map._hasFitBounds) {
      const coords = poles.map(p => [p.lat, p.lon]);
      if (coords.length > 0) {
        _map.fitBounds(L.latLngBounds(coords).pad(0.08));
        _map._hasFitBounds = true;
      }
    }
  }

  // ─── Edge rendering ──────────────────────────────────────────────────

  function updateEdges(edges) {
    // Clear existing edge lines
    _edgeLayer.clearLayers();
    Object.keys(_edgeLines).forEach(k => delete _edgeLines[k]);

    for (const edge of edges) {
      const fromNode = _poleMarkers[edge.from_id] ||
                       _findTransformerLatLon(edge.from_id);
      const toNode   = _poleMarkers[edge.to_id];

      if (!fromNode || !toNode) continue;

      const fromLatLng = fromNode.getLatLng ? fromNode.getLatLng()
                       : L.latLng(fromNode.lat, fromNode.lon);
      const toLatLng   = toNode.getLatLng();

      const isInferred = edge.edge_type === 'inferred';
      const line = L.polyline([fromLatLng, toLatLng], {
        color:     isInferred ? '#4b5563' : 'rgba(56,139,212,0.35)',
        weight:    isInferred ? 1 : 1.5,
        opacity:   isInferred ? 0.5 : 0.7,
        dashArray: isInferred ? '4 4' : null,
      });

      const tooltip = isInferred
        ? 'Topology inferred (not surveyed)'
        : `Span: ${edge.from_id} → ${edge.to_id}`;
      line.bindTooltip(tooltip, { className: 'leaflet-tooltip' });
      line.addTo(_edgeLayer);

      const key = `${edge.from_id}-${edge.to_id}`;
      _edgeLines[key] = line;
    }
  }

  // ─── Fault highlight ─────────────────────────────────────────────────
  /**
   * When tickets update, highlight the fault spans/poles in red.
   * We highlight the edges connected to fault poles and add a pulsing
   * overlay circle on the centroid of each open ticket.
   */
  function updateFaultHighlights(tickets, poles) {
    _faultLayer.clearLayers();

    const faultPoleIds = new Set();
    const openTickets = tickets.filter(t =>
      !['verified','closed'].includes(t.status)
    );

    for (const ticket of openTickets) {
      (ticket.affected_poles || []).forEach(pid => faultPoleIds.add(pid));

      if (ticket.lat && ticket.lon) {
        // Pulsing ring at fault centroid
        const ring = L.circle([ticket.lat, ticket.lon], {
          radius: 80,
          color: '#ef4444',
          fillColor: '#ef4444',
          fillOpacity: 0.06,
          weight: 2,
          dashArray: '6 4',
          className: 'fault-ring',
        });
        ring.bindTooltip(
          `${ticket.id} — ${ticket.affected_pole_count || '?'} poles affected`,
          { sticky: true }
        );
        ring.on('click', () => {
          window.HumbugTickets && window.HumbugTickets.openDrawer(ticket.id);
        });
        ring.addTo(_faultLayer);
      }
    }

    // Highlight edges between fault poles
    for (const [key, line] of Object.entries(_edgeLines)) {
      const [fromId, toId] = key.split('-');
      if (faultPoleIds.has(toId)) {
        line.setStyle({ color: '#ef4444', weight: 2.5, opacity: 0.9 });
      }
    }
  }

  // ─── Fly to fault ─────────────────────────────────────────────────────
  function flyToTicket(ticket) {
    if (!_map) return;
    if (ticket.lat && ticket.lon) {
      _map.flyTo([ticket.lat, ticket.lon], 16, { duration: 1.2 });
    } else if (ticket.affected_poles && ticket.affected_poles.length > 0) {
      const pids = ticket.affected_poles;
      const markers = pids.map(pid => _poleMarkers[pid]).filter(Boolean);
      if (markers.length > 0) {
        const bounds = L.latLngBounds(markers.map(m => m.getLatLng()));
        _map.flyToBounds(bounds.pad(0.15), { duration: 1.2 });
      }
    }
  }

  // ─── DT lat/lon lookup for edge drawing ─────────────────────────────
  const _dtPositions = {};   // dt_id -> {lat, lon}
  function registerTransformer(dt_id, lat, lon) {
    _dtPositions[dt_id] = { lat, lon };
  }
  function _findTransformerLatLon(id) {
    return _dtPositions[id] || null;
  }

  return { init, updatePoles, updateEdges, updateFaultHighlights, flyToTicket, registerTransformer };
})();
