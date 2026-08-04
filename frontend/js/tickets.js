/**
 * tickets.js — Ticket list + detail drawer
 *
 * Renders the right-column tickets list and the slide-in detail drawer.
 * Uses React.createElement (no JSX) for component logic.
 *
 * Exposes: window.HumbugTickets = { render, openDrawer }
 */

window.HumbugTickets = (() => {
  const { createElement: h, useState, useEffect, useCallback } = React;

  // ─── Lifecycle steps config ─────────────────────────────────────────
  const LIFECYCLE_STEPS = [
    { key: 'detected',      label: 'DETECTED' },
    { key: 'acknowledged',  label: 'ACK' },
    { key: 'crew_assigned', label: 'CREW SENT' },
    { key: 'resolved',      label: 'RESOLVED' },
    { key: 'verified',      label: 'VERIFIED' },
    { key: 'closed',        label: 'CLOSED' },
  ];

  function stepIndex(status) {
    return LIFECYCLE_STEPS.findIndex(s => s.key === status);
  }

  // ─── LifecycleStepper ───────────────────────────────────────────────
  function LifecycleStepper({ status }) {
    const current = stepIndex(status);
    return h('div', { className: 'lifecycle-stepper' },
      LIFECYCLE_STEPS.map((step, i) => {
        const done    = i < current;
        const active  = i === current;
        const cls = ['step', done ? 'step-done' : '', active ? 'step-current' : ''].join(' ');
        return h('div', { key: step.key, className: cls },
          h('div', { className: 'step-dot' }, done ? '✓' : null),
          h('div', { className: 'step-label' }, step.label)
        );
      })
    );
  }

  // ─── ConfidenceBar ──────────────────────────────────────────────────
  function ConfidenceBar({ value }) {
    const pct = Math.round((value || 0) * 100);
    const cls = confidenceClass(value || 0);
    return h('div', { className: 'confidence-bar-wrap' },
      h('div', { className: 'confidence-bar' },
        h('div', { className: `confidence-fill ${cls}`, style: { width: `${pct}%` } })
      ),
      h('span', { className: 'text-xs text-dim' }, `${pct}%`)
    );
  }

  // ─── TicketCard ─────────────────────────────────────────────────────
  function TicketCard({ ticket, onOpen, onFlyTo }) {
    const age = fmtAge(ticket.created_at);
    const poleCount = ticket.affected_pole_count || (ticket.affected_poles || []).length;

    function handleClick(e) {
      e.preventDefault();
      onOpen(ticket.id);
    }
    function handleMapClick(e) {
      e.stopPropagation();
      onFlyTo(ticket);
    }

    return h('div', {
      className: `ticket-card status-${ticket.status}`,
      onClick: handleClick,
      title: 'Click to view details',
    },
      h('div', { className: 'ticket-card-top' },
        h('span', { className: 'ticket-id' }, ticket.id),
        h('span', { className: `badge badge-status badge-${ticket.status}` },
          statusLabel(ticket.status))
      ),
      h('div', { className: 'ticket-card-body' },
        h('div', { className: 'ticket-target' },
          ticket.scope === 'dt' ? `DT ${ticket.target_id}` : ticket.target_id),
        h('div', { className: 'ticket-meta' },
          h('span', null, `${poleCount} pole${poleCount !== 1 ? 's' : ''}`),
          ticket.pincode && h('span', null, ticket.pincode),
          ticket.feeder_id && h('span', null, ticket.feeder_id)
        )
      ),
      h('div', { className: 'ticket-card-footer' },
        h('div', { className: 'confidence-bar-wrap' },
          h('div', { className: 'confidence-bar' },
            h('div', {
              className: `confidence-fill ${confidenceClass(ticket.confidence || 0)}`,
              style: { width: `${Math.round((ticket.confidence || 0) * 100)}%` }
            })
          ),
          h('span', { className: 'text-xs text-dim' },
            `${Math.round((ticket.confidence || 0) * 100)}% conf`)
        ),
        h('div', { style: { display: 'flex', gap: '8px', alignItems: 'center' } },
          h('span', { className: 'text-dim text-xs' }, age),
          h('button', {
            className: 'btn-icon', title: 'Zoom to fault on map',
            onClick: handleMapClick, style: { fontSize: '12px' }
          }, '◎')
        )
      )
    );
  }

  // ─── TicketList ─────────────────────────────────────────────────────
  function TicketList({ tickets, onOpen, onFlyTo }) {
    const open = tickets.filter(t => !['verified','closed'].includes(t.status));

    if (open.length === 0) {
      return h('div', { className: 'ticket-list-empty' },
        h('span', { className: 'empty-icon' }, '✔'),
        h('div', null, 'No active faults'),
        h('div', { className: 'text-xs mt-2' }, 'System nominal')
      );
    }

    return h('div', null,
      open.map(t =>
        h(TicketCard, { key: t.id, ticket: t, onOpen, onFlyTo })
      )
    );
  }

  // ─── DetailRow ──────────────────────────────────────────────────────
  function DetailRow({ label, value, valueClass }) {
    return h(React.Fragment, null,
      h('div', { className: 'detail-label' }, label),
      h('div', { className: `detail-value ${valueClass || ''}` }, value || '—')
    );
  }

  // ─── TicketDetail (drawer content) ──────────────────────────────────
  function TicketDetail({ ticketId, onClose }) {
    const [ticket, setTicket] = useState(null);
    const [loading, setLoading] = useState(true);
    const [acting, setActing] = useState(false);

    const load = useCallback(async () => {
      try {
        const t = await Api.getTicket(ticketId);
        setTicket(t);
      } catch (e) {
        showToast(`Failed to load ticket: ${e.message}`, 'err');
      } finally {
        setLoading(false);
      }
    }, [ticketId]);

    useEffect(() => { load(); }, [load]);

    // Refresh whenever poller fires new tickets
    useEffect(() => {
      function onTickets(tickets) {
        const updated = tickets.find(t => t.id === ticketId);
        if (updated) setTicket(updated);
      }
      HumbugPoller.subscribe('tickets', onTickets);
      return () => HumbugPoller.unsubscribe('tickets', onTickets);
    }, [ticketId]);

    async function doAction(actionFn, successMsg) {
      setActing(true);
      try {
        await actionFn();
        showToast(successMsg, 'ok');
        await load();
        await HumbugPoller.refresh();
      } catch (e) {
        showToast(e.message, 'err');
      } finally {
        setActing(false);
      }
    }

    if (loading) {
      return h('div', { style: { padding: '32px', textAlign: 'center' } },
        h('span', { className: 'spinner' })
      );
    }
    if (!ticket) return null;

    const status = ticket.status;
    const poleCount = ticket.affected_pole_count || (ticket.affected_poles || []).length;
    const isVerified = ['verified','closed'].includes(status);
    const isRestorationPending = ['resolved'].includes(status);

    const verificationCallout = () => {
      if (isVerified) {
        return h('div', { className: 'verification-callout callout-verified' },
          `✓ Restoration auto-verified from telemetry at ${fmtTime(ticket.verified_at)}`
        );
      }
      if (isRestorationPending) {
        return h('div', { className: 'verification-callout callout-pending' },
          '⏳ Awaiting telemetry confirmation — poles must come back live for auto-verification'
        );
      }
      return h('div', { className: 'verification-callout callout-info' },
        'Restoration will be verified automatically from pole telemetry. ' +
        'The system will not accept a manual "verified" click.'
      );
    };

    return h('div', null,
      // Lifecycle stepper
      h('div', { className: 'detail-section' },
        h(LifecycleStepper, { status })
      ),

      // Verification callout — this is a core trust feature
      verificationCallout(),

      // Fault summary
      h('div', { className: 'detail-section' },
        h('div', { className: 'detail-section-title' }, 'Fault Summary'),
        h('div', { className: 'detail-grid' },
          h(DetailRow, { label: 'Ticket', value: ticket.id }),
          h(DetailRow, { label: 'Scope', value: ticket.scope === 'dt'
            ? `DT-level (${ticket.scope})` : ticket.scope }),
          h(DetailRow, { label: 'Target', value: ticket.target_id, valueClass: 'value-fault' }),
          h(DetailRow, { label: 'Poles affected', value: `${poleCount}` }),
          h(DetailRow, { label: 'Feeder', value: ticket.feeder_id }),
          h(DetailRow, { label: 'Pincode', value: ticket.pincode }),
          h(DetailRow, {
            label: 'Coordinates',
            value: ticket.lat && ticket.lon
              ? `${ticket.lat.toFixed(5)}°N ${ticket.lon.toFixed(5)}°E`
              : 'Unknown',
            valueClass: ticket.lat ? 'value-accent' : ''
          }),
        )
      ),

      // Confidence
      h('div', { className: 'detail-section' },
        h('div', { className: 'detail-section-title' }, 'Confidence'),
        h('div', { className: 'detail-grid' },
          h('div', { className: 'detail-label' }, 'Score'),
          h('div', { className: 'detail-value', style: { display: 'flex', alignItems: 'center', gap: '12px' } },
            h(ConfidenceBar, { value: ticket.confidence }),
          ),
          h('div', { className: 'detail-label' }, 'Reason'),
          h('div', { className: 'detail-value text-sm', style: { color: 'var(--text-secondary)', gridColumn: '2' } },
            ticket.confidence_reason || 'Not computed yet')
        )
      ),

      // Timeline
      h('div', { className: 'detail-section' },
        h('div', { className: 'detail-section-title' }, 'Timeline'),
        h('div', { className: 'detail-grid' },
          h(DetailRow, { label: 'Detected',     value: fmtDateTime(ticket.created_at) }),
          h(DetailRow, { label: 'Acknowledged', value: fmtDateTime(ticket.acknowledged_at) }),
          h(DetailRow, { label: 'Crew assigned',value: fmtDateTime(ticket.crew_assigned_at) }),
          h(DetailRow, { label: 'Resolved',     value: fmtDateTime(ticket.resolved_at) }),
          h(DetailRow, { label: 'Verified',     value: fmtDateTime(ticket.verified_at) }),
          h(DetailRow, { label: 'Closed',       value: fmtDateTime(ticket.closed_at) }),
        )
      ),

      // Actions — only the transitions that make sense for current state
      h('div', { className: 'ticket-actions' },
        status === 'detected' && h('button', {
          className: 'btn btn-warn',
          disabled: acting,
          onClick: () => doAction(() => Api.acknowledgeTicket(ticket.id), 'Ticket acknowledged')
        }, acting ? h('span', { className: 'spinner' }) : 'Acknowledge'),

        status === 'acknowledged' && h('button', {
          className: 'btn btn-primary',
          disabled: acting,
          onClick: () => doAction(() => Api.assignCrew(ticket.id), 'Crew assigned')
        }, acting ? h('span', { className: 'spinner' }) : 'Assign Crew'),

        status === 'crew_assigned' && h('button', {
          className: 'btn btn-ghost',
          disabled: acting,
          onClick: () => doAction(() => Api.resolveTicket(ticket.id), 'Marked resolved — awaiting telemetry verification')
        }, acting ? h('span', { className: 'spinner' }) : 'Mark Resolved'),

        // Explicitly disabled verified/close — telemetry-only
        (status === 'resolved') && h('button', {
          className: 'btn btn-ghost',
          disabled: true,
          title: 'Verification is automatic from telemetry — cannot be done manually'
        }, '🔒 Auto-verify only'),

        h('button', {
          className: 'btn btn-ghost',
          onClick: () => {
            HumbugMap.flyToTicket(ticket);
            onClose();
          }
        }, '◎ Zoom to fault')
      )
    );
  }

  // ─── Drawer component ────────────────────────────────────────────────
  let _drawerRoot = null;
  let _drawerSetTicket = null;

  function DrawerApp() {
    const [ticketId, setTicketId] = useState(null);

    useEffect(() => {
      // Expose setter for external callers
      _drawerSetTicket = setTicketId;
    }, []);

    function close() {
      setTicketId(null);
      document.getElementById('ticket-drawer').classList.replace('drawer-open','drawer-closed');
    }

    // sync DOM class
    useEffect(() => {
      const el = document.getElementById('ticket-drawer');
      if (!el) return;
      if (ticketId) {
        el.classList.replace('drawer-closed','drawer-open');
      }
    }, [ticketId]);

    const drawerTitle = ticketId || 'Ticket Detail';

    return h(React.Fragment, null,
      // Update DOM title
      ticketId && document.getElementById('drawer-title') &&
        Object.assign(document.getElementById('drawer-title'), { textContent: ticketId }),

      ticketId && h(TicketDetail, { ticketId, onClose: close })
    );
  }

  // ─── List renderer (not React — keeps it simple for the right column) 
  let _listRoot = null;

  function ListApp({ initialTickets }) {
    const [tickets, setTickets] = useState(initialTickets || []);

    useEffect(() => {
      HumbugPoller.subscribe('tickets', setTickets);
      return () => HumbugPoller.unsubscribe('tickets', setTickets);
    }, []);

    function handleFlyTo(ticket) {
      HumbugMap.flyToTicket(ticket);
    }

    return h(TicketList, {
      tickets,
      onOpen: (id) => openDrawer(id),
      onFlyTo: handleFlyTo,
    });
  }

  // ─── Public API ──────────────────────────────────────────────────────
  function render(listContainerId, drawerBodyId) {
    // Mount ticket list
    const listEl = document.getElementById(listContainerId);
    if (listEl) {
      _listRoot = ReactDOM.createRoot(listEl);
      _listRoot.render(h(ListApp, { initialTickets: [] }));
    }

    // Mount drawer body
    const drawerEl = document.getElementById(drawerBodyId);
    if (drawerEl) {
      _drawerRoot = ReactDOM.createRoot(drawerEl);
      _drawerRoot.render(h(DrawerApp, null));
    }

    // Backdrop closes drawer
    document.getElementById('drawer-backdrop')?.addEventListener('click', closeDrawer);
    document.getElementById('drawer-close-btn')?.addEventListener('click', closeDrawer);
  }

  function openDrawer(ticketId) {
    if (_drawerSetTicket) _drawerSetTicket(ticketId);
    // title update
    const el = document.getElementById('drawer-title');
    if (el) el.textContent = ticketId;
  }

  function closeDrawer() {
    if (_drawerSetTicket) _drawerSetTicket(null);
    document.getElementById('ticket-drawer')?.classList.replace('drawer-open','drawer-closed');
  }

  return { render, openDrawer, closeDrawer };
})();
