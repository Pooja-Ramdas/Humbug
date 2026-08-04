/**
 * simulator.js — Fault Simulator panel + inject dialog
 *
 * Renders:
 *   - "Inject Fault" button → dialog with fault type + searchable target combobox
 *   - Active injected faults list with per-fault "Repair" action
 *   - Noise injection section (device death, load shed, duplicate burst)
 *
 * Uses React.createElement (no JSX).
 * Exposes: window.HumbugSimulator = { render }
 */

window.HumbugSimulator = (() => {
  const { createElement: h, useState, useEffect, useRef, useCallback } = React;

  // ─── Combobox ────────────────────────────────────────────────────────
  function Combobox({ options, value, onChange, placeholder }) {
    const [query, setQuery] = useState('');
    const [open, setOpen] = useState(false);
    const [highlighted, setHighlighted] = useState(0);
    const inputRef = useRef(null);

    const filtered = options.filter(o =>
      !query || o.id.toLowerCase().includes(query.toLowerCase()) ||
      (o.sub && o.sub.toLowerCase().includes(query.toLowerCase()))
    ).slice(0, 40);

    function select(opt) {
      onChange(opt.id);
      setQuery(opt.id);
      setOpen(false);
    }

    function onKeyDown(e) {
      if (!open) { setOpen(true); return; }
      if (e.key === 'ArrowDown') { e.preventDefault(); setHighlighted(h => Math.min(h+1, filtered.length-1)); }
      if (e.key === 'ArrowUp')   { e.preventDefault(); setHighlighted(h => Math.max(h-1, 0)); }
      if (e.key === 'Enter') { e.preventDefault(); filtered[highlighted] && select(filtered[highlighted]); }
      if (e.key === 'Escape') setOpen(false);
    }

    return h('div', { className: 'combobox-wrap' },
      h('input', {
        ref: inputRef,
        className: 'form-input',
        placeholder: placeholder || 'Type to search…',
        value: query,
        onChange: e => { setQuery(e.target.value); setOpen(true); onChange(''); },
        onFocus: () => setOpen(true),
        onBlur: () => setTimeout(() => setOpen(false), 150),
        onKeyDown,
        autoComplete: 'off',
      }),
      h('div', { className: `combobox-dropdown ${open && filtered.length > 0 ? '' : 'hidden'}` },
        filtered.map((opt, i) =>
          h('div', {
            key: opt.id,
            className: `combo-option ${i === highlighted ? 'highlighted' : ''}`,
            onMouseDown: e => { e.preventDefault(); select(opt); },
            onMouseEnter: () => setHighlighted(i),
          },
            h('div', { className: 'combo-option-id' }, opt.id),
            opt.sub && h('div', { className: 'combo-option-sub' }, opt.sub)
          )
        )
      )
    );
  }

  // ─── InjectDialog ────────────────────────────────────────────────────
  function InjectDialog({ poles, transformers, feeders, onClose, onSubmit }) {
    const [faultType, setFaultType] = useState('dt');
    const [targetId, setTargetId] = useState('');
    const [submitting, setSubmitting] = useState(false);

    // Build combobox options based on fault type
    const options = (() => {
      if (faultType === 'feeder') {
        return feeders.map(f => ({
          id: f.feeder_id,
          sub: `Substation ${f.substation_id} · ${f.lat?.toFixed(4)}°N ${f.lon?.toFixed(4)}°E`,
        }));
      }
      if (faultType === 'dt') {
        return transformers.map(t => ({
          id: t.dt_id,
          sub: `${t.feeder_id} · ${t.capacity_kva}kVA · ${t.households_served} households`
            + (t.topology_known ? '' : ' · topology unknown'),
        }));
      }
      // span — target is a pole
      return poles
        .filter(p => p.parent_pole_id || p.seq_on_line)  // only poles with known position
        .map(p => ({
          id: p.pole_id,
          sub: `${p.dt_id} · seq ${p.seq_on_line || '?'} · ${p.lat?.toFixed(5)}°N`,
        }));
    })();

    async function handleSubmit(e) {
      e.preventDefault();
      if (!targetId) { showToast('Select a target first', 'warn'); return; }
      setSubmitting(true);
      try {
        await onSubmit(faultType, targetId);
        onClose();
      } finally {
        setSubmitting(false);
      }
    }

    const typeLabels = { span: 'Span fault (pole + downstream)', dt: 'DT fault (whole transformer)', feeder: 'Feeder fault (all DTs on feeder)' };

    return h('form', { onSubmit: handleSubmit },
      h('div', { className: 'form-group' },
        h('label', { className: 'form-label' }, 'Fault Type'),
        h('select', {
          className: 'form-select',
          value: faultType,
          onChange: e => { setFaultType(e.target.value); setTargetId(''); },
        },
          Object.entries(typeLabels).map(([val, label]) =>
            h('option', { key: val, value: val }, label)
          )
        )
      ),

      h('div', { className: 'form-group' },
        h('label', { className: 'form-label' },
          faultType === 'span' ? 'Target Pole (fault starts here)'
          : faultType === 'dt' ? 'Target Distribution Transformer'
          : 'Target Feeder'
        ),
        h(Combobox, {
          options,
          value: targetId,
          onChange: setTargetId,
          placeholder: `Search ${faultType === 'feeder' ? 'feeder' : faultType === 'dt' ? 'DT' : 'pole'} ID…`,
        })
      ),

      faultType === 'span' && h('div', { className: 'text-xs text-dim mt-2' },
        'Span fault darkens the selected pole and all poles electrically downstream of it.'
      ),
      faultType === 'dt' && h('div', { className: 'text-xs text-dim mt-2' },
        'DT fault darkens every pole under this transformer simultaneously.'
      ),
      faultType === 'feeder' && h('div', { className: 'text-xs text-dim mt-2' },
        'Feeder fault darkens all poles under every DT on this feeder. Expect multiple tickets.'
      ),

      h('div', { className: 'dialog-footer', style: { padding: '16px 0 0 0', marginTop: '16px', borderTop: '1px solid var(--border-dim)' } },
        h('button', { type: 'button', className: 'btn btn-ghost', onClick: onClose }, 'Cancel'),
        h('button', {
          type: 'submit',
          className: 'btn btn-danger',
          disabled: !targetId || submitting,
        }, submitting ? h('span', { className: 'spinner' }) : '⚡ Inject Fault')
      )
    );
  }

  // ─── ActiveFaultItem ─────────────────────────────────────────────────
  function ActiveFaultItem({ fault, onRepair }) {
    const [repairing, setRepairing] = useState(false);
    const age = fmtAge(fault.injected_at);

    async function handleRepair(e) {
      e.stopPropagation();
      setRepairing(true);
      try { await onRepair(fault); }
      finally { setRepairing(false); }
    }

    return h('div', { className: 'active-fault-item' },
      h('div', null,
        h('div', { className: 'active-fault-id' }, `${fault.fault_type.toUpperCase()} · ${fault.target_id}`),
        h('div', { className: 'active-fault-meta' }, `Injected ${age}`)
      ),
      h('button', {
        className: 'btn btn-success',
        style: { fontSize: '11px', padding: '4px 10px' },
        disabled: repairing,
        onClick: handleRepair,
      }, repairing ? h('span', { className: 'spinner' }) : '✓ Repair')
    );
  }

  // ─── SimulatorPanel ──────────────────────────────────────────────────
  function SimulatorPanel() {
    const [poles, setPoles] = useState([]);
    const [transformers, setTransformers] = useState([]);
    const [feeders, setFeeders] = useState([]);
    const [activeFaults, setActiveFaults] = useState([]);
    const [dialogOpen, setDialogOpen] = useState(false);
    const [collapsed, setCollapsed] = useState(false);
    const [noiseTarget, setNoiseTarget] = useState('');

    // Load static data once
    useEffect(() => {
      Api.getPoles().then(setPoles).catch(() => {});
      Api.getTransformers().then(setTransformers).catch(() => {});
      Api.getFeeders().then(setFeeders).catch(() => {});
    }, []);

    // Active faults from poller
    useEffect(() => {
      HumbugPoller.subscribe('activeFaults', setActiveFaults);
      return () => HumbugPoller.unsubscribe('activeFaults', setActiveFaults);
    }, []);

    async function handleInject(faultType, targetId) {
      try {
        const res = await Api.simulateFault(faultType, targetId);
        showToast(
          `Fault injected: ${res.affected_pole_count} poles affected, ` +
          `${res.messages_generated} msgs sent, ${res.messages_lost} lost`,
          'warn', 6000
        );
        await HumbugPoller.refresh();
      } catch (e) {
        showToast(`Inject failed: ${e.message}`, 'err');
        throw e;
      }
    }

    async function handleRepair(fault) {
      try {
        const res = await Api.simulateRestore(fault.fault_type, fault.target_id);
        showToast(
          `Repair telemetry sent: ${res.messages_generated} messages. ` +
          'Awaiting auto-verification from telemetry.',
          'ok', 5000
        );
        await HumbugPoller.refresh();
      } catch (e) {
        showToast(`Repair failed: ${e.message}`, 'err');
      }
    }

    async function injectNoise(noiseType, extraArgs = {}) {
      try {
        const res = await Api.simulateNoise(noiseType, noiseTarget || null, extraArgs.scope, extraArgs.duration);
        showToast(`Noise injected: ${noiseType}`, 'info');
        await HumbugPoller.refresh();
      } catch (e) {
        showToast(`Noise inject failed: ${e.message}`, 'err');
      }
    }

    // Sync collapse button state
    useEffect(() => {
      const btn = document.getElementById('sim-collapse-btn');
      if (btn) btn.textContent = collapsed ? '▼' : '▲';
      const body = document.getElementById('simulator-body');
      if (body) body.style.display = collapsed ? 'none' : '';
    }, [collapsed]);

    // Wire collapse button
    useEffect(() => {
      const btn = document.getElementById('sim-collapse-btn');
      if (btn) btn.onclick = () => setCollapsed(c => !c);
    }, []);

    return h(React.Fragment, null,
      h('div', { className: 'sim-actions' },
        h('button', {
          className: 'btn btn-danger w-full',
          onClick: () => setDialogOpen(true),
          style: { flex: 1 },
        }, '⚡ Inject Fault')
      ),

      activeFaults.length > 0 && h('div', { className: 'active-faults-list' },
        h('div', { className: 'text-xs text-dim', style: { marginBottom: '8px', letterSpacing: '0.08em' } },
          `${activeFaults.length} ACTIVE INJECTED FAULT${activeFaults.length > 1 ? 'S' : ''}`),
        activeFaults.map(f =>
          h(ActiveFaultItem, { key: f.id, fault: f, onRepair: handleRepair })
        )
      ),

      activeFaults.length === 0 && h('div', { className: 'text-xs text-dim', style: { marginTop: '8px' } },
        'No active injected faults. Inject one above.'
      ),

      // Noise section — clearly separated from real fault injection
      h('div', { className: 'noise-section' },
        h('div', { className: 'noise-section-title' }, 'Independent Noise (not faults)'),
        h('div', { className: 'form-group', style: { marginBottom: '8px' } },
          h('input', {
            className: 'form-input',
            placeholder: 'Pole/DT/Feeder ID (optional)',
            value: noiseTarget,
            onChange: e => setNoiseTarget(e.target.value),
            style: { fontSize: '11px' },
          })
        ),
        h('div', { className: 'noise-actions' },
          h('button', {
            className: 'btn btn-ghost',
            style: { fontSize: '11px', padding: '4px 8px' },
            title: 'Simulate a device modem dying while power is fine — should NOT generate a fault ticket',
            onClick: () => injectNoise('device_death'),
          }, '📡 Dead device'),
          h('button', {
            className: 'btn btn-ghost',
            style: { fontSize: '11px', padding: '4px 8px' },
            title: 'Register a scheduled outage window so dark poles are suppressed',
            onClick: () => injectNoise('load_shed', { scope: 'dt', duration: 60 }),
          }, '🕐 Load shed'),
          h('button', {
            className: 'btn btn-ghost',
            style: { fontSize: '11px', padding: '4px 8px' },
            title: 'Send duplicate heartbeat messages — tests dedup logic',
            onClick: () => injectNoise('duplicate_burst'),
          }, '♻ Duplicate burst'),
        )
      ),

      // Inject dialog — rendered into the stable dialog root (created once in render())
      dialogOpen && (() => {
        const overlay = document.getElementById('inject-dialog');
        const dialogRoot = window.HumbugSimulator && window.HumbugSimulator.getDialogRoot
          ? window.HumbugSimulator.getDialogRoot()
          : null;
        if (overlay) overlay.classList.remove('dialog-hidden');
        if (dialogRoot) {
          dialogRoot.render(
            h(InjectDialog, {
              poles, transformers, feeders,
              onClose: () => {
                if (overlay) overlay.classList.add('dialog-hidden');
                setDialogOpen(false);
              },
              onSubmit: handleInject,
            })
          );
        }
        return null;
      })()
    );
  }

  // ─── Wire dialog close button ─────────────────────────────────────────
  document.getElementById('inject-close-btn')?.addEventListener('click', () => {
    document.getElementById('inject-dialog')?.classList.add('dialog-hidden');
  });

  // ─── Public API ──────────────────────────────────────────────────────
  let _root = null;
  let _dialogRoot = null;  // Stable root for the inject dialog — never recreated

  function render(containerId) {
    const el = document.getElementById(containerId);
    if (!el) return;
    _root = ReactDOM.createRoot(el);
    _root.render(h(SimulatorPanel));

    // Pre-create the dialog root once so we can call .render() on it
    // without leaking new roots on every dialog open.
    const dialogEl = document.getElementById('inject-dialog-body');
    if (dialogEl) {
      _dialogRoot = ReactDOM.createRoot(dialogEl);
    }
  }

  // Expose so SimulatorPanel can reuse the stable dialog root
  function getDialogRoot() { return _dialogRoot; }

  return { render, getDialogRoot };
})();
