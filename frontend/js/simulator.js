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
      return poles.map(p => ({
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
        }, submitting ? h('span', { className: 'spinner' }) : '\u26A1 Inject Fault')
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
      }, repairing ? h('span', { className: 'spinner' }) : '\u2713 Repair')
    );
  }

  // ─── ActiveLoadShedItem ──────────────────────────────────────────────
  function ActiveLoadShedItem({ outage, onEnd }) {
    const [ending, setEnding] = useState(false);
    const [timeLeft, setTimeLeft] = useState(Math.max(0, Math.round(outage.end_ts - Date.now() / 1000)));

    // Tick the countdown timer every second
    useEffect(() => {
      const timer = setInterval(() => {
        const remaining = Math.max(0, Math.round(outage.end_ts - Date.now() / 1000));
        setTimeLeft(remaining);
        if (remaining <= 0) {
          clearInterval(timer);
        }
      }, 1000);
      return () => clearInterval(timer);
    }, [outage.end_ts]);

    async function handleEnd(e) {
      e.stopPropagation();
      setEnding(true);
      try { await onEnd(outage); }
      finally { setEnding(false); }
    }

    const mins = Math.floor(timeLeft / 60);
    const secs = timeLeft % 60;
    const timeStr = `${mins}:${secs.toString().padStart(2, '0')}`;

    return h('div', { className: 'active-fault-item' },
      h('div', null,
        h('div', { className: 'active-fault-id', style: { color: 'var(--accent-yellow)', textShadow: '0 0 4px rgba(255, 230, 0, 0.3)' } }, `LOAD SHED · ${outage.target_id}`),
        h('div', { className: 'active-fault-meta' }, `Time remaining: ${timeStr}`)
      ),
      h('button', {
        className: 'btn btn-warn',
        style: { fontSize: '11px', padding: '4px 10px' },
        disabled: ending,
        onClick: handleEnd,
      }, ending ? h('span', { className: 'spinner' }) : '\u25A0 End Early')
    );
  }

  // ─── LoadShedDialog ──────────────────────────────────────────────────
  function LoadShedDialog({ poles, transformers, feeders, onClose, onSubmit }) {
    const [scope, setScope] = useState('dt');
    const [targetId, setTargetId] = useState('');
    const [duration, setDuration] = useState('60');
    const [startType, setStartType] = useState('instant');
    const [startOffset, setStartOffset] = useState('10');
    const [submitting, setSubmitting] = useState(false);

    useEffect(() => {
      const el = document.querySelector('.dialog-title');
      if (el) el.textContent = 'SCHEDULE LOAD SHEDDING';
    }, []);

    const options = (() => {
      if (scope === 'feeder') {
        return feeders.map(f => ({
          id: f.feeder_id,
          sub: `Substation ${f.substation_id} \u00b7 ${f.lat?.toFixed(4)}\u00b0N ${f.lon?.toFixed(4)}\u00b0E`,
        }));
      }
      // dt scope
      return transformers.map(t => ({
        id: t.dt_id,
        sub: `${t.feeder_id} \u00b7 ${t.capacity_kva}kVA \u00b7 ${t.households_served} households`,
      }));
    })();

    async function handleSubmit(e) {
      e.preventDefault();
      if (!targetId) { showToast('Select a target first', 'warn'); return; }
      setSubmitting(true);
      const delay = startType === 'scheduled' ? parseInt(startOffset) : 0;
      try {
        await Api.simulateLoadShed(scope, targetId, parseInt(duration), delay);
        const msg = delay > 0 
          ? `Load shedding scheduled in ${delay} mins for ${targetId}`
          : `Load shedding started for ${targetId}`;
        showToast(msg, 'success');
        
        // Trigger manual detection refresh
        await Api.triggerDetection().catch(() => {});
        if (onSubmit) onSubmit();
        onClose();
      } catch (err) {
        showToast(err.message, 'error');
      } finally {
        setSubmitting(false);
      }
    }

    // Load shedding is feeder/DT level only — individual pole level is not physically valid.
    // Real-world load shedding is always controlled at feeder breakers or substation level.
    const scopeLabels = { dt: 'Whole Transformer (DT)', feeder: 'Feeder Line' };

    return h('form', { onSubmit: handleSubmit },
      h('div', { className: 'form-group' },
        h('label', { className: 'form-label' }, 'Outage Scope'),
        h('select', {
          className: 'form-select',
          value: scope,
          onChange: e => { setScope(e.target.value); setTargetId(''); },
        },
          Object.entries(scopeLabels).map(([val, label]) =>
            h('option', { key: val, value: val }, label)
          )
        )
      ),

      h('div', { className: 'form-group' },
        h('label', { className: 'form-label' }, 'Target ID'),
        h(Combobox, {
          options,
          value: targetId,
          onChange: setTargetId,
          placeholder: `Search ${scope === 'feeder' ? 'feeder' : scope === 'dt' ? 'DT' : 'pole'} ID…`,
        })
      ),

      h('div', { className: 'form-group' },
        h('label', { className: 'form-label' }, 'Duration (minutes)'),
        h('input', {
          type: 'number',
          className: 'form-input',
          min: '1',
          value: duration,
          onChange: e => setDuration(e.target.value),
          placeholder: 'Enter duration in minutes...',
          required: true,
        })
      ),

      h('div', { className: 'form-group' },
        h('label', { className: 'form-label' }, 'Timing'),
        h('select', {
          className: 'form-select',
          value: startType,
          onChange: e => setStartType(e.target.value),
        },
          h('option', { value: 'instant' }, 'Start now'),
          h('option', { value: 'scheduled' }, 'Schedule for later')
        )
      ),

      startType === 'scheduled' && h('div', { className: 'form-group' },
        h('label', { className: 'form-label' }, 'Start Offset (minutes from now)'),
        h('input', {
          type: 'number',
          className: 'form-input',
          min: '1',
          value: startOffset,
          onChange: e => setStartOffset(e.target.value),
          placeholder: 'Enter start offset in minutes...',
          required: true,
        })
      ),

      h('div', { className: 'dialog-footer', style: { padding: '16px 0 0 0', marginTop: '16px', borderTop: '1px solid var(--border-dim)' } },
        h('button', { type: 'button', className: 'btn btn-ghost', onClick: onClose }, 'Cancel'),
        h('button', {
          type: 'submit',
          className: 'btn btn-warn',
          disabled: !targetId || submitting,
        }, submitting ? h('span', { className: 'spinner' }) : '\u29D6 Confirm Outage')
      )
    );
  }

  // ─── NoiseDialog ─────────────────────────────────────────────────────
  function NoiseDialog({ poles, transformers, feeders, onClose, onSubmit }) {
    const [noiseType, setNoiseType] = useState('device_death');
    const [targetId, setTargetId] = useState('');
    const [submitting, setSubmitting] = useState(false);

    useEffect(() => {
      const el = document.querySelector('.dialog-title');
      if (el) el.textContent = 'INJECT TELEMETRY NOISE';
    }, []);

    const options = (() => {
      return poles.map(p => ({
        id: p.pole_id,
        sub: `${p.dt_id} · seq ${p.seq_on_line || '?'} · ${p.lat?.toFixed(5)}°N`,
      }));
    })();

    async function handleSubmit(e) {
      e.preventDefault();
      setSubmitting(true);
      try {
        await Api.simulateNoise(noiseType, targetId || null, 'pole', 60);
        showToast(`Noise injected successfully`, 'success');
        
        // Trigger manual detection refresh
        await Api.triggerDetection().catch(() => {});
        if (onSubmit) onSubmit();
        onClose();
      } catch (err) {
        showToast(err.message, 'error');
      } finally {
        setSubmitting(false);
      }
    }

    const noiseLabels = {
      device_death:    '[x] Dead device modem (loss of heartbeat)',
      duplicate_burst: '[~] Duplicate burst (at-least-once retry storm)',
    };

    return h('form', { onSubmit: handleSubmit },
      h('div', { className: 'form-group' },
        h('label', { className: 'form-label' }, 'Noise Type'),
        h('select', {
          className: 'form-select',
          value: noiseType,
          onChange: e => { setNoiseType(e.target.value); setTargetId(''); },
        },
          Object.entries(noiseLabels).map(([val, label]) =>
            h('option', { key: val, value: val }, label)
          )
        )
      ),

      h('div', { className: 'form-group' },
        h('label', { className: 'form-label' }, 'Target Pole ID (Optional)'),
        h(Combobox, {
          options,
          value: targetId,
          onChange: setTargetId,
          placeholder: 'Search/Select specific pole (blank for random)…',
        })
      ),

      h('div', { className: 'dialog-footer', style: { padding: '16px 0 0 0', marginTop: '16px', borderTop: '1px solid var(--border-dim)' } },
        h('button', { type: 'button', className: 'btn btn-ghost', onClick: onClose }, 'Cancel'),
        h('button', {
          type: 'submit',
          className: 'btn btn-primary',
          disabled: submitting,
        }, submitting ? h('span', { className: 'spinner' }) : '\u2248 Inject Noise')
      )
    );
  }

  // ─── SimulatorPanel ──────────────────────────────────────────────────
  function SimulatorPanel() {
    const [poles, setPoles] = useState([]);
    const [transformers, setTransformers] = useState([]);
    const [feeders, setFeeders] = useState([]);
    const [activeFaults, setActiveFaults] = useState([]);
    const [activeDialog, setActiveDialog] = useState(null); // 'fault' | 'load_shed' | 'noise' | null
    const [collapsed, setCollapsed] = useState(false);

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

    // Active load shedding from poller
    const [activeLoadShed, setActiveLoadShed] = useState([]);
    useEffect(() => {
      HumbugPoller.subscribe('activeLoadShed', setActiveLoadShed);
      return () => HumbugPoller.unsubscribe('activeLoadShed', setActiveLoadShed);
    }, []);

    async function handleEndLoadShed(outage) {
      try {
        await Api.endLoadShed(outage.id);
        showToast(`Load shedding ended: ${outage.target_id}`, 'ok');
        await HumbugPoller.refresh();
      } catch (e) {
        showToast(`End load shed failed: ${e.message}`, 'err');
      }
    }

    // Sync HTML Close button with React state
    useEffect(() => {
      const closeBtn = document.getElementById('inject-close-btn');
      if (closeBtn) {
        closeBtn.onclick = () => {
          const overlay = document.getElementById('inject-dialog');
          if (overlay) overlay.classList.add('dialog-hidden');
          setActiveDialog(null);
        };
      }
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
      h('div', { className: 'sim-actions-grid', style: { display: 'flex', flexDirection: 'column', gap: '8px', marginBottom: '12px' } },
        h('button', {
          className: 'btn btn-danger w-full',
          onClick: () => setActiveDialog('fault'),
        }, '\u26A1 Inject Fault'),
        h('button', {
          className: 'btn btn-warn w-full',
          onClick: () => setActiveDialog('load_shed'),
        }, '\u29D6 Schedule Load Shedding'),
        h('button', {
          className: 'btn btn-primary w-full',
          onClick: () => setActiveDialog('noise'),
        }, '\u2248 Inject Telemetry Noise')
      ),

      activeFaults.length > 0 && h('div', { className: 'active-faults-list' },
        h('div', { className: 'text-xs text-dim', style: { marginBottom: '8px', letterSpacing: '0.08em' } },
          `${activeFaults.length} ACTIVE INJECTED FAULT${activeFaults.length > 1 ? 'S' : ''}`),
        activeFaults.map(f =>
          h(ActiveFaultItem, { key: f.id, fault: f, onRepair: handleRepair })
        )
      ),

      activeFaults.length === 0 && h('div', { className: 'text-xs text-dim', style: { marginTop: '8px', marginBottom: '8px', textAlign: 'center' } },
        'No active injected faults. Inject one above.'
      ),

      activeLoadShed.length > 0 && h('div', { className: 'active-faults-list', style: { marginTop: '8px' } },
        h('div', { className: 'text-xs text-dim', style: { marginBottom: '8px', letterSpacing: '0.08em' } },
          `${activeLoadShed.length} ACTIVE LOAD SHEDDING EVENT${activeLoadShed.length > 1 ? 'S' : ''}`),
        activeLoadShed.map(o =>
          h(ActiveLoadShedItem, { key: o.id, outage: o, onEnd: handleEndLoadShed })
        )
      ),

      // Dialog renderer
      activeDialog && (() => {
        const overlay = document.getElementById('inject-dialog');
        const dialogRoot = window.HumbugSimulator && window.HumbugSimulator.getDialogRoot
          ? window.HumbugSimulator.getDialogRoot()
          : null;
        if (overlay) overlay.classList.remove('dialog-hidden');
        if (dialogRoot) {
          let comp = null;
          const onClose = () => {
            if (overlay) overlay.classList.add('dialog-hidden');
            setActiveDialog(null);
          };
          if (activeDialog === 'fault') {
            comp = h(InjectDialog, { poles, transformers, feeders, onClose, onSubmit: handleInject });
          } else if (activeDialog === 'load_shed') {
            comp = h(LoadShedDialog, { poles, transformers, feeders, onClose, onSubmit: () => HumbugPoller.refresh() });
          } else if (activeDialog === 'noise') {
            comp = h(NoiseDialog, { poles, transformers, feeders, onClose, onSubmit: () => HumbugPoller.refresh() });
          }
          if (comp) {
            dialogRoot.render(comp);
          }
        }
        return null;
      })()
    );
  }

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
