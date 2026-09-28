// Federated sidebar: sessions from paired peers (other machines) shown in the
// conversation list, opened embedded from the peer's own CCC.
//
// Peer rows come from GET /api/sessions?federated=1&peers_only=1, fetched on
// its own timer so the local list never waits on the network (the server caches
// one call per peer per TTL). Each row is identified by its global ref
// (<node_id>:<session_id>). Clicking a row embeds the peer's single-conversation
// popout (<web_url>/?ccc_popout=conversation&conv=<session_id>) over the main
// pane. A peer without a browser-reachable address shows why and is not openable.
(function () {
  'use strict';
  var params = new URLSearchParams(window.location.search || '');
  if (params.get('ccc_popout') || params.get('popout')) return; // never nest inside a popout frame

  var POLL_MS = 15000;      // matches the server's ~10s per-peer cache
  var IDLE_POLL_MS = 60000; // nothing paired: keep the interval long
  var PER_NODE_CAP = 5;
  var FETCH_TIMEOUT_MS = 25000;
  var ROW_BG_KEY = 'ccc-fed-row-bg';
  var ROW_BG_DEFAULT = '#271923';
  var COLLAPSE_KEY = 'ccc-fed-collapsed';

  var state = { nodes: [], rows: [] };
  var expanded = false;
  var collapsed = false;
  try { collapsed = localStorage.getItem(COLLAPSE_KEY) === '1'; } catch (_) {}
  var openRef = '';
  var lastSig = null;
  var pollTimer = null;
  var polling = false;

  function $(id) { return document.getElementById(id); }

  function rowEpoch(r) {
    var t = r && r.timestamp;
    if (typeof t === 'number') return t;
    if (typeof t === 'string' && t) {
      var n = Number(t);
      if (isFinite(n)) return n;
      var d = Date.parse(t);
      if (isFinite(d)) return d / 1000;
    }
    return 0;
  }

  function ago(epoch) {
    if (!epoch) return '';
    var s = Math.max(0, Date.now() / 1000 - epoch);
    if (s < 90) return 'now';
    if (s < 3600) return Math.round(s / 60) + 'm';
    if (s < 86400) return Math.round(s / 3600) + 'h';
    return Math.round(s / 86400) + 'd';
  }

  function nodeById(id) {
    for (var i = 0; i < state.nodes.length; i++) if (state.nodes[i].node_id === id) return state.nodes[i];
    return null;
  }

  // Why a row can't be opened, or '' when it can.
  function blockedReason(node) {
    if (!node) return 'unknown machine';
    var st = node.web_url_state;
    if (st === 'ok' && node.web_url) return '';
    if (st === 'pin') return 'PIN-protected: cannot be embedded';
    if (st === 'unknown') return 'looking up web address…';
    return 'no web address for this machine';
  }

  function nodeNote(node) {
    if (!node) return '';
    if (node.ok === false) return 'offline';
    if (node.stale) return 'stale';
    return '';
  }

  function buildEmbedUrl(node, row) {
    var u = new URL(node.web_url);
    u.searchParams.set('ccc_popout', 'conversation');
    u.searchParams.set('conv', row.session_id);
    return u.toString();
  }

  function searchValue() {
    var el = $('convSearch');
    return ((el && el.value) || '').trim().toLowerCase();
  }

  function sidebarTabIsCoding() {
    try {
      var t = localStorage.getItem('ccc-sidebar-tab');
      return !t || t === 'coding';
    } catch (_) { return true; }
  }

  function rowTitle(r) {
    return r.display_name || r.first_message || String(r.session_id || '').slice(0, 8);
  }

  function visibleRows() {
    var q = searchValue();
    var rows = state.rows.filter(function (r) {
      if (!r.ref || !r.session_id) return false;
      if (!q) return true;
      return (rowTitle(r) + ' ' + (r.first_message || '') + ' ' + (r.cwd || '') + ' '
        + (r.node_name || '') + ' ' + r.session_id).toLowerCase().indexOf(q) !== -1;
    });
    if (expanded || q) return rows;
    var seen = {};
    return rows.filter(function (r) {
      seen[r.node_id] = (seen[r.node_id] || 0) + 1;
      return seen[r.node_id] <= PER_NODE_CAP;
    });
  }

  // Peer rows get their own background so they never read as local sessions;
  // the swatch in the section header picks it, persisted per browser.
  function rowBg() {
    try {
      var c = localStorage.getItem(ROW_BG_KEY);
      if (c && /^#[0-9a-f]{6}$/i.test(c)) return c;
    } catch (_) {}
    return ROW_BG_DEFAULT;
  }

  function applyRowBg(c) {
    document.documentElement.style.setProperty('--fed-row-bg', c);
  }

  function buildHead(count) {
    var head = el('div', 'fed-head');
    head.setAttribute('data-role', 'fed-toggle');
    head.setAttribute('role', 'button');
    head.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    head.tabIndex = 0;
    head.title = collapsed ? 'Show sessions on other machines' : 'Hide sessions on other machines';
    head.appendChild(el('span', 'fed-caret', collapsed ? '\u25b8' : '\u25be'));
    head.appendChild(el('span', 'fed-head-label', 'Other machines'));
    if (collapsed && count) head.appendChild(el('span', 'fed-head-count', String(count)));
    var pick = el('input', 'fed-color');
    pick.type = 'color';
    pick.value = rowBg();
    pick.title = 'Background color for sessions on other machines';
    pick.setAttribute('aria-label', pick.title);
    pick.addEventListener('input', function () {
      applyRowBg(pick.value);
      try { localStorage.setItem(ROW_BG_KEY, pick.value); } catch (_) {}
    });
    head.appendChild(pick);
    return head;
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function buildRow(r) {
    var node = nodeById(r.node_id);
    var reason = blockedReason(node);
    var note = nodeNote(node);
    var row = el('div', 'fed-row' + (note ? ' is-offline' : '') + (reason ? ' is-blocked' : '')
      + (openRef === r.ref ? ' active' : ''));
    row.setAttribute('data-id', r.ref);
    row.setAttribute('data-node-id', r.node_id || '');
    row.setAttribute('role', 'button');
    row.tabIndex = 0;
    row.title = reason || ('Open on ' + (r.node_name || 'peer'));
    row.appendChild(el('span', 'fed-dot' + (r.is_live ? ' is-live' : '')));
    var main = el('div', 'fed-main');
    main.appendChild(el('div', 'fed-title', rowTitle(r)));
    var sub = el('div', 'fed-sub');
    sub.appendChild(el('span', 'fed-chip', r.node_name || (node && node.name) || 'peer'));
    if (note) sub.appendChild(el('span', 'fed-note', note));
    if (reason) sub.appendChild(el('span', 'fed-note', reason));
    else if (r.cwd) sub.appendChild(el('span', 'fed-cwd', String(r.cwd).replace(/\/+$/, '').split('/').pop()));
    main.appendChild(sub);
    row.appendChild(main);
    row.appendChild(el('span', 'fed-time', ago(rowEpoch(r))));
    return row;
  }

  // A peer that answered with no sessions, or is down, still says so.
  function buildNodeRow(node) {
    var reason = node.ok === false ? 'offline' : '';
    if (!reason) return null;
    var row = el('div', 'fed-row is-offline is-blocked');
    row.setAttribute('data-id', 'node:' + node.node_id);
    row.appendChild(el('span', 'fed-dot'));
    var main = el('div', 'fed-main');
    main.appendChild(el('div', 'fed-title', 'No sessions available'));
    var sub = el('div', 'fed-sub');
    sub.appendChild(el('span', 'fed-chip', node.name || 'peer'));
    sub.appendChild(el('span', 'fed-note', reason));
    main.appendChild(sub);
    row.appendChild(main);
    return row;
  }

  function render() {
    var list = $('convList');
    if (!list) return;
    var section = list.querySelector(':scope > .fed-peer-section');
    var peers = state.nodes.filter(function (n) { return !n.self; });
    var rows = (peers.length && sidebarTabIsCoding()) ? visibleRows() : [];
    var offlineOnly = peers.filter(function (n) {
      return n.ok === false && !state.rows.some(function (r) { return r.node_id === n.node_id; });
    });
    var hasContent = rows.length || (offlineOnly.length && !searchValue());
    if (!hasContent) {
      if (section) section.remove();
      lastSig = null;
      return;
    }
    var totalHidden = state.rows.length - rows.length;
    var sig = JSON.stringify([
      rows.map(function (r) { return [r.ref, rowTitle(r), ago(rowEpoch(r)), !!r.is_live, r.node_name, r.cwd]; }),
      peers.map(function (n) { return [n.node_id, n.ok, n.stale, n.web_url_state, n.web_url]; }),
      openRef, expanded, collapsed, totalHidden > 0 && !searchValue()]);
    var tabBar = list.querySelector(':scope > .conv-tab-bar');
    var placed = section && (tabBar ? section.previousElementSibling === tabBar : list.firstElementChild === section);
    if (section && placed && sig === lastSig) return;
    lastSig = sig;
    var fresh = el('div', 'fed-peer-section');
    fresh.setAttribute('data-role', 'fed-peer-section');
    fresh.appendChild(buildHead(state.rows.length));
    if (!collapsed) rows.forEach(function (r) { fresh.appendChild(buildRow(r)); });
    if (!collapsed && !searchValue()) {
      offlineOnly.forEach(function (n) { var b = buildNodeRow(n); if (b) fresh.appendChild(b); });
    }
    if (!collapsed && !searchValue() && (totalHidden > 0 || expanded) && state.rows.length > PER_NODE_CAP) {
      var more = el('button', 'fed-more', expanded ? 'Show fewer' : 'Show ' + totalHidden + ' more');
      more.type = 'button';
      more.setAttribute('data-role', 'fed-more');
      fresh.appendChild(more);
    }
    if (section) section.remove();
    if (tabBar) tabBar.insertAdjacentElement('afterend', fresh);
    else list.insertBefore(fresh, list.firstChild);
  }

  // ---- embed ---------------------------------------------------------------

  function closeEmbed() {
    var host = $('fedEmbed');
    if (host) host.remove();
    var main = document.querySelector('.main');
    if (main) main.classList.remove('fed-embed-host');
    openRef = '';
    render();
  }

  function openRow(ref) {
    var row = null;
    for (var i = 0; i < state.rows.length; i++) if (state.rows[i].ref === ref) row = state.rows[i];
    if (!row) return;
    var node = nodeById(row.node_id);
    if (blockedReason(node)) return;
    var main = document.querySelector('.main');
    if (!main) return;
    var host = $('fedEmbed');
    if (!host) {
      host = el('div', 'fed-embed');
      host.id = 'fedEmbed';
      var bar = el('div', 'fed-embed-bar');
      bar.appendChild(el('span', 'fed-chip'));
      bar.appendChild(el('span', 'fed-embed-title'));
      var close = el('button', 'fed-embed-close', 'Close');
      close.type = 'button';
      close.setAttribute('data-role', 'fed-embed-close');
      close.addEventListener('click', closeEmbed);
      bar.appendChild(close);
      host.appendChild(bar);
      var frame = document.createElement('iframe');
      frame.className = 'fed-embed-frame';
      frame.setAttribute('allow', 'clipboard-read; clipboard-write');
      host.appendChild(frame);
      main.appendChild(host);
      main.classList.add('fed-embed-host');
    }
    host.querySelector('.fed-chip').textContent = row.node_name || node.name || 'peer';
    host.querySelector('.fed-embed-title').textContent = rowTitle(row);
    var f = host.querySelector('.fed-embed-frame');
    f.title = 'Conversation on ' + (row.node_name || 'peer');
    var url = buildEmbedUrl(node, row);
    if (f.getAttribute('src') !== url) f.setAttribute('src', url);
    openRef = row.ref;
    render();
  }

  function toggleCollapsed() {
    collapsed = !collapsed;
    try { localStorage.setItem(COLLAPSE_KEY, collapsed ? '1' : '0'); } catch (_) {}
    render();
  }

  function wire() {
    var list = $('convList');
    if (!list) return;
    list.addEventListener('click', function (ev) {
      if (ev.target.closest && ev.target.closest('.fed-color')) return;
      if (ev.target.closest && ev.target.closest('[data-role="fed-toggle"]')) { toggleCollapsed(); return; }
      var more = ev.target.closest && ev.target.closest('[data-role="fed-more"]');
      if (more) { expanded = !expanded; render(); return; }
      var row = ev.target.closest && ev.target.closest('.fed-row[data-node-id]');
      if (row) openRow(row.getAttribute('data-id'));
    });
    list.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter' && ev.key !== ' ') return;
      if (ev.target.getAttribute && ev.target.getAttribute('data-role') === 'fed-toggle') {
        ev.preventDefault(); toggleCollapsed(); return;
      }
      var row = ev.target.closest && ev.target.closest('.fed-row[data-node-id]');
      if (row) { ev.preventDefault(); openRow(row.getAttribute('data-id')); }
    });
    // The local list re-renders wholesale (innerHTML); put the section back.
    new MutationObserver(function () { render(); }).observe(list, { childList: true });
    var search = $('convSearch');
    if (search) search.addEventListener('input', render);
    // Opening any local conversation dismisses the embed.
    window.addEventListener('ccc:conversation-selected', function () { if ($('fedEmbed')) closeEmbed(); });
  }

  // ---- polling -------------------------------------------------------------

  function schedule(ms) {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(poll, ms);
  }

  function poll() {
    if (polling) return;
    if (document.hidden) { schedule(POLL_MS); return; }
    polling = true;
    var ctl = new AbortController();
    var to = setTimeout(function () { ctl.abort(); }, FETCH_TIMEOUT_MS);
    fetch('/api/sessions?federated=1&peers_only=1&limit=60', { cache: 'no-store', signal: ctl.signal })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (d && Array.isArray(d.nodes)) {
          state.nodes = d.nodes;
          // Peer rows only: the local list already carries this machine's own.
          state.rows = (d.sessions || []).filter(function (r) {
            var n = nodeById(r.node_id);
            return n && !n.self;
          });
          render();
        }
        var hasPeers = state.nodes.some(function (n) { return !n.self; });
        schedule(hasPeers ? POLL_MS : IDLE_POLL_MS);
      })
      .catch(function () { schedule(POLL_MS); })
      .then(function () { clearTimeout(to); polling = false; });
  }

  function boot() {
    applyRowBg(rowBg());
    wire();
    poll();
    document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
    window.addEventListener('storage', function (e) { if (e.key === 'ccc-sidebar-tab') render(); });
    window.cccFederatedSidebar = { render: render, state: function () { return state; }, open: openRow, close: closeEmbed };
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
