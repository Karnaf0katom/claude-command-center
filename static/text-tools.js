/* Optional composer tools. Layout is offline; spelling is an explicit request. */
(function (root) {
  'use strict';
  const enToHe = {
    q: '/', w: "'", e: 'ק', r: 'ר', t: 'א', y: 'ט', u: 'ו', i: 'ן', o: 'ם', p: 'פ',
    a: 'ש', s: 'ד', d: 'ג', f: 'כ', g: 'ע', h: 'י', j: 'ח', k: 'ל', l: 'ך',
    z: 'ז', x: 'ס', c: 'ב', v: 'ה', b: 'נ', n: 'מ', m: 'צ',
    ';': 'ף', "'": ',', ',': 'ת', '.': 'ץ', '/': '.', '`': ';', '[': ']', ']': '[',
  };
  const heToEn = Object.fromEntries(Object.entries(enToHe).map(([a, b]) => [b, a]));
  function fixLayout(text) {
    const letters = Array.from(text).filter(ch => /\p{L}/u.test(ch));
    if (!letters.length) return text;
    const hebrew = letters.filter(ch => /[א-ת]/u.test(ch)).length >= letters.length / 2;
    const map = hebrew ? heToEn : enToHe;
    return Array.from(text, ch => map[hebrew ? ch : ch.toLowerCase()] || ch).join('');
  }
  function targetFor(el) {
    const value = el.value;
    const start = el.selectionStart || 0, end = el.selectionEnd || 0;
    const selected = end > start;
    return { value, start: selected ? start : 0, end: selected ? end : value.length,
      text: selected ? value.slice(start, end) : value };
  }
  // Export only pure helpers for node's built-in test runner.
  if (typeof module === 'object' && module.exports) {
    module.exports = { fixLayout, targetFor };
    return;
  }
  const doc = root.document;
  const enabled = { layout: false, spell: false };
  const pending = new WeakMap(), revisions = new WeakMap(), fallbackUndo = new WeakMap();
  let status = { available: false, backend: 'Checking spelling availability…', max_chars: 20000 };
  let statusLoading = null;
  function readSettings() {
    for (const tool of Object.keys(enabled)) {
      try { enabled[tool] = root.localStorage.getItem('ccc.textTools.' + tool) === '1'; }
      catch (_) { enabled[tool] = false; }
    }
  }
  function announce(text, source) {
    const bar = source.closest('.conv-input-bar, .simple-composer-card');
    const el = bar && bar.querySelector('.composer-text-tools-status');
    if (el) { el.hidden = false; el.textContent = text; }
  }
  function syncUi() {
    doc.querySelectorAll('[data-text-tool-toggle]').forEach(btn => {
      const on = enabled[btn.dataset.textToolToggle];
      btn.classList.toggle('is-on', on);
      btn.setAttribute('aria-checked', String(on));
    });
    doc.querySelectorAll('.composer-text-tool').forEach(btn => {
      const tool = btn.dataset.textTool;
      btn.hidden = !enabled[tool];
      const el = composerFor(btn);
      btn.disabled = !!(el && pending.has(el)) || (tool === 'spell' && !status.available);
      btn.setAttribute('aria-busy', String(!!(el && pending.has(el))));
      if (tool === 'spell') btn.title = status.available
        ? 'Fix spelling and grammar in the selection or whole draft; uses ' + status.backend
        : status.backend;
    });
    const el = doc.getElementById('settingsSpellingStatus');
    if (el) el.textContent = status.available
      ? 'Uses ' + status.backend + '. Selected text is sent only when you click the correction button.'
      : status.backend;
  }
  async function refreshStatus() {
    if (statusLoading) return statusLoading;
    statusLoading = (async () => {
      try {
        const response = await root.fetch('/api/text-tools');
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error();
        status = data;
      } catch (_) {
        status = { available: false, backend: 'Spelling service unavailable. Reopen Settings to retry.' };
      } finally { syncUi(); statusLoading = null; }
    })();
    return statusLoading;
  }
  function composerFor(btn) {
    const bar = btn.closest('.conv-input-bar, .simple-composer-card');
    return bar && bar.querySelector('textarea, input[type="text"]');
  }
  function apply(el, target, replacement) {
    el.focus();
    el.setSelectionRange(target.start, target.end);
    const expected = target.value.slice(0, target.start) + replacement + target.value.slice(target.end);
    try { doc.execCommand('insertText', false, replacement); } catch (_) {}
    if (el.value !== expected) {
      el.value = expected;
      el.setSelectionRange(target.start + replacement.length, target.start + replacement.length);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      fallbackUndo.set(el, { value: target.value, expected, start: target.start, end: target.end,
        owner: el.id === 'convInput' ? root.currentConversation : null });
    }
  }
  async function run(btn) {
    const el = composerFor(btn), tool = btn.dataset.textTool;
    if (!el || !enabled[tool] || pending.has(el) || btn.disabled) return;
    const say = text => announce(text, btn);
    const target = targetFor(el);
    if (!target.text.trim()) { say('Type or select some text first.'); return; }
    let result;
    if (tool === 'layout') result = fixLayout(target.text);
    else {
      if (target.text.length > (status.max_chars || 20000)) {
        say('Select at most 20,000 characters to correct.'); return;
      }
      const controller = new AbortController();
      const revision = revisions.get(el) || 0;
      const owner = el.id === 'convInput' ? root.currentConversation : null;
      pending.set(el, controller);
      const timer = root.setTimeout(() => controller.abort(), 65000);
      btn.setAttribute('aria-busy', 'true');
      syncUi();
      say('Correcting spelling and grammar…');
      try {
        const response = await root.fetch('/api/text-tools', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'spell', text: target.text }), signal: controller.signal,
        });
        const data = await response.json();
        if (!response.ok || !data.ok || typeof data.result !== 'string' || !data.result.trim()) {
          throw new Error(data.error || 'Spelling correction failed.');
        }
        if (!el.isConnected || el.value !== target.value || (revisions.get(el) || 0) !== revision
            || (el.id === 'convInput' && root.currentConversation !== owner)
            || !enabled.spell || !pending.has(el)) {
          say('Draft changed while correcting; nothing was replaced.'); return;
        }
        result = data.result;
      } catch (error) {
        say(error.name === 'AbortError' ? 'Correction cancelled or timed out; draft kept.'
          : 'Spelling correction failed: ' + error.message);
        return;
      } finally {
        root.clearTimeout(timer); pending.delete(el); btn.removeAttribute('aria-busy'); syncUi();
      }
    }
    if (result === target.text) { say('No change needed.'); return; }
    apply(el, target, result);
    btn.classList.add('applied');
    root.setTimeout(() => btn.classList.remove('applied'), 700);
    say('Draft corrected. Ctrl+Z or Cmd+Z to undo. Review it before sending.');
  }
  // Keep the textarea selection when a mouse, pen or touch opens the tool.
  doc.addEventListener('pointerdown', ev => {
    if (ev.target.closest('.composer-text-tool')) ev.preventDefault();
  });
  doc.addEventListener('input', ev => {
    if (ev.target.matches('.conv-input-bar textarea, .simple-composer-input')) {
      revisions.set(ev.target, (revisions.get(ev.target) || 0) + 1);
      fallbackUndo.delete(ev.target);
    }
  });
  doc.addEventListener('keydown', ev => {
    if (!(ev.ctrlKey || ev.metaKey) || ev.shiftKey || ev.key.toLowerCase() !== 'z') return;
    const undo = fallbackUndo.get(ev.target);
    if (!undo || ev.target.value !== undo.expected
        || (ev.target.id === 'convInput' && root.currentConversation !== undo.owner)) return;
    ev.preventDefault();
    ev.target.value = undo.value;
    ev.target.setSelectionRange(undo.start, undo.end);
    ev.target.dispatchEvent(new Event('input', { bubbles: true }));
  });
  root.addEventListener('ccc:conversation-selected', ev => {
    const pane = ev.detail && ev.detail.paneEl;
    const el = pane && pane.querySelector('.conv-input-bar textarea');
    if (el) {
      revisions.set(el, (revisions.get(el) || 0) + 1);
      fallbackUndo.delete(el);
      const controller = pending.get(el);
      if (controller) controller.abort();
    }
  });
  doc.addEventListener('click', ev => {
    const toggle = ev.target.closest('[data-text-tool-toggle]');
    if (toggle) {
      const tool = toggle.dataset.textToolToggle;
      enabled[tool] = !enabled[tool];
      try { root.localStorage.setItem('ccc.textTools.' + tool, enabled[tool] ? '1' : '0'); } catch (_) {}
      if (tool === 'spell' && !enabled.spell) {
        doc.querySelectorAll('.conv-input-bar textarea, .simple-composer-input').forEach(el => {
          const controller = pending.get(el);
          if (controller) controller.abort();
        });
      }
      syncUi();
      if (tool === 'spell' && enabled.spell) refreshStatus();
      return;
    }
    const btn = ev.target.closest('.composer-text-tool');
    if (btn) { ev.preventDefault(); run(btn); }
    if (ev.target.closest('#settingsBtn, #settingsRailTab-tools')) refreshStatus();
  });
  root.addEventListener('storage', ev => {
    if (!ev.key || ev.key.startsWith('ccc.textTools.')) { readSettings(); syncUi(); }
  });
  // Split panes clone the composer with IDs stripped. Delegated handlers and
  // this child-only observer give future clones the current preference.
  new MutationObserver(records => {
    if (records.some(record => Array.from(record.addedNodes).some(node => node.nodeType === 1
        && (node.matches('.composer-text-tool') || node.querySelector('.composer-text-tool'))))) syncUi();
  }).observe(doc.body, { childList: true, subtree: true });
  readSettings(); syncUi();
  if (enabled.spell) refreshStatus();
})(typeof window === 'undefined' ? globalThis : window);
