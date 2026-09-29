// Copyright (c) 2026 Amir Fish. All rights reserved.
// SPDX-License-Identifier: LicenseRef-CCC-Software-License
/**
 * Sidebar "Jobs" tab: scheduled jobs with outcomes (Hermes systemd timers +
 * scheduled laptop launchd agents). Data: GET /api/jobs (server-cached, refreshed
 * in the background). The sidebar list re-renders often and replaces the host
 * element, so app.js calls CCCJobsTab.mount() after each rebuild; mount()
 * refills the fresh host from cached data without a fetch.
 */
(function () {
  'use strict';

  const HOST_KEY = 'ccc-jobs-host';
  const POLL_MS = 60000;
  let _data = null;
  let _lastFetch = 0;
  let _inflight = false;
  let _lastHtml = '';
  let _host = (function () {
    try { const v = localStorage.getItem(HOST_KEY); if (v === 'all' || v === 'hermes' || v === 'laptop') return v; } catch (_) {}
    return 'hermes';
  })();
  const _expanded = new Set();
  const _collapsed = new Set((function () { try { return JSON.parse(localStorage.getItem('ccc-jobs-collapsed') || '[]'); } catch (_) { return []; } })());
  const _logs = new Map(); // id -> text | null (loading)

  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#039;');
  }

  function rel(iso, future) {
    if (!iso) return '';
    const ms = Date.parse(iso);
    if (isNaN(ms)) return '';
    let sec = Math.round((ms - Date.now()) / 1000);
    const isFuture = sec > 0;
    sec = Math.abs(sec);
    let t;
    if (sec < 60) t = sec + 's';
    else if (sec < 3600) t = Math.round(sec / 60) + 'm';
    else if (sec < 86400) t = Math.round(sec / 3600) + 'h';
    else t = Math.round(sec / 86400) + 'd';
    if (future !== undefined && isFuture !== future) return isFuture ? 'in ' + t : t + ' ago';
    return isFuture ? 'in ' + t : t + ' ago';
  }

  function dur(s) {
    if (s == null) return '';
    s = Math.round(s);
    if (s < 60) return s + 's';
    const m = Math.floor(s / 60), r = s % 60;
    if (m < 60) return m + 'm' + (r ? r + 's' : '');
    return Math.floor(m / 60) + 'h' + (m % 60 ? (m % 60) + 'm' : '');
  }

  function shortName(n) {
    return String(n || '').replace(/^com\.amirfish\./, '').replace(/^com\.amir\./, '');
  }

  function isActive() {
    let t = null;
    try { t = localStorage.getItem('ccc-sidebar-tab'); } catch (_) {}
    return t === 'jobs';
  }

  function attentionCount() {
    if (!_data || !_data.summary) return 0;
    const sm = _data.summary[_host === 'all' ? 'all' : _host];
    return (sm && sm.attention) || 0;
  }

  function updateBadge() {
    const btn = document.querySelector('[data-conv-tab="jobs"]');
    if (!btn) return;
    const n = attentionCount();
    let span = btn.querySelector('.conv-tab-count');
    if (n && !span) { span = document.createElement('span'); span.className = 'conv-tab-count'; btn.appendChild(span); }
    if (span) { if (n) span.textContent = String(n); else span.remove(); }
  }

  function visibleJobs() {
    const jobs = (_data && _data.jobs) || [];
    return _host === 'all' ? jobs : jobs.filter(j => j.host === _host);
  }

  // No em-dashes in user copy.
  function nodash(t) { return String(t == null ? '' : t).replace(/ — /g, ': ').replace(/—/g, ':'); }

  function hueOf(str) {
    let h = 0;
    for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) % 360;
    return h;
  }

  function fmtLocal(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return '';
    return d.toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  function headerHtml() {
    const hosts = _data.hosts || {};
    const h = hosts.hermes || {};
    const sum = (_data.summary || {})[_host === 'all' ? 'all' : _host] || {};
    const parts = [];
    if (_host !== 'laptop') {
      if (h.status === 'online') parts.push('Hermes online');
      else if (h.status === 'loading') parts.push('Hermes loading');
      else {
        const age = h.age_s != null ? ' (data ' + rel(new Date(Date.now() - h.age_s * 1000).toISOString()).replace(' ago', '') + ' old)' : '';
        parts.push('Hermes offline' + age);
      }
    } else {
      parts.push('Laptop');
    }
    const active = (sum.total || 0) - (sum.disabled || 0);
    parts.push(active + ' job' + (active === 1 ? '' : 's'));
    if (sum.failed) parts.push(sum.failed + ' failed');
    if (sum.stale) parts.push(sum.stale + ' stale');
    if (sum.disabled) parts.push(sum.disabled + ' disabled');
    const bad = (h.status === 'offline' && _host !== 'laptop') || sum.failed;
    const seg = ['hermes', 'laptop', 'all'].map(k => {
      const label = k === 'all' ? 'All' : k === 'hermes' ? 'Hermes' : 'Laptop';
      return '<button type="button" class="jobs-seg-btn' + (k === _host ? ' is-active' : '') + '" data-jobs-host="' + k + '">' + label + '</button>';
    }).join('');
    return '<div class="jobs-head"><div class="jobs-seg">' + seg + '</div>'
      + '<div class="jobs-summary' + (bad ? ' is-bad' : '') + '">' + esc(parts.join(' · ')) + '</div></div>';
  }

  // 24h strip: ticks at launch times (local), a "now" marker, weekday label for
  // weekly jobs, and a filled band for short intervals.
  function stripHtml(j) {
    const tl = j.timeline || {};
    let ticks = '';
    let label = '';
    if (tl.kind === 'interval' && tl.interval_s) {
      if (tl.interval_s <= 3600) {
        ticks = '<i class="job-band' + (tl.interval_s <= 900 ? ' dense' : '') + '"></i>';
      } else if (tl.interval_s >= 86400) {
        ticks = '<i class="job-tick" style="left:0"></i>';
        label = 'every ' + Math.round(tl.interval_s / 86400) + 'd';
      } else {
        for (let m = 0; m < 1440; m += tl.interval_s / 60) ticks += '<i class="job-tick" style="left:' + (m / 14.4).toFixed(2) + '%"></i>';
      }
    } else if (tl.kind === 'times') {
      ticks = (tl.minutes || []).map(m => '<i class="job-tick" style="left:' + (m / 14.4).toFixed(2) + '%"></i>').join('');
      if (tl.weekdays && tl.weekdays.length) label = tl.weekdays.length > 2 ? tl.weekdays[0] + '+' : tl.weekdays.join(',');
    }
    const n = new Date();
    const nowPct = ((n.getHours() * 60 + n.getMinutes()) / 14.4).toFixed(2);
    const tip = j.schedule + (j.next_run_at ? '\nNext: ' + fmtLocal(j.next_run_at) + ' (' + rel(j.next_run_at) + ')' : '');
    return '<span class="job-strip" title="' + esc(tip) + '"><span class="job-track">' + ticks
      + '<i class="job-now" style="left:' + nowPct + '%"></i></span>'
      + '<span class="job-wd">' + esc(label) + '</span></span>';
  }

  function chipHtml(t) {
    const label = esc(t.ref);
    if (t.kind === 'watchtower') {
      const q = String(t.ref).replace(/-\d+$/, '');
      return '<a role="button" tabindex="0" class="job-chip watchtower-ticket-link" data-watchtower-ticket="' + esc(t.ref)
        + '" data-watchtower-queue="' + esc(q) + '">' + label + '</a>';
    }
    if (t.url) return '<a class="job-chip" href="' + esc(t.url) + '" target="_blank" rel="noopener" title="' + esc(t.repo || '') + '">' + label + '</a>';
    return '<span class="job-chip">' + label + '</span>';
  }

  function chipsHtml(tickets, max) {
    const list = tickets || [];
    const shown = list.slice(0, max);
    return shown.map(chipHtml).join('') + (list.length > max ? '<span class="job-chip-more">+' + (list.length - max) + '</span>' : '');
  }

  function outcomeHtml(j) {
    if (!j.outcome) return '';
    const text = nodash(j.outcome);
    return '<span class="job-outcome' + (j.outcome_kind === 'output' ? ' is-raw' : '') + '" title="' + esc(text) + '">'
      + (j.outcome_kind === 'output' ? '<span class="job-out-label">output </span>' : '') + esc(text) + '</span>';
  }

  function rowHtml(j) {
    const open = _expanded.has(j.id);
    const tipBits = ['Status: ' + j.status];
    if (j.last_run_at) tipBits.push('Last run: ' + fmtLocal(j.last_run_at));
    if (j.last_duration_s != null) tipBits.push('Duration: ' + dur(j.last_duration_s || 0.4));
    if (j.exit_code) tipBits.push('Exit code: ' + j.exit_code);
    const mid = (j.outcome_kind === 'summary' || !(j.tickets && j.tickets.length))
      ? outcomeHtml(j) : '<span class="job-chips">' + chipsHtml(j.tickets, 5) + '</span>';
    let h = '<div class="job-row st-' + esc(j.status) + (open ? ' is-open' : '') + '" data-job-id="' + esc(j.id) + '">'
      + '<div class="job-line1"><span class="job-dot" title="' + esc(j.status) + '"></span>'
      + '<span class="job-name" title="' + esc(j.name) + '">' + esc(shortName(j.name)) + '</span>'
      + (_host === 'all' ? '<span class="job-host">' + (j.host === 'hermes' ? 'Hermes' : 'Laptop') + '</span>' : '')
      + '<span class="job-desc-inline" title="' + esc(nodash(j.description)) + '">' + esc(nodash(j.description)) + '</span></div>'
      + '<div class="job-line2">' + stripHtml(j) + '<span class="job-mid">' + mid + '</span>'
      + '<span class="job-when" title="' + esc(tipBits.join('\n')) + '">' + esc(j.last_run_at ? rel(j.last_run_at) : '') + '</span></div>';
    if (open) {
      const log = _logs.get(j.id);
      const facts = [];
      facts.push(j.schedule);
      if (j.next_run_at) facts.push('next ' + rel(j.next_run_at));
      if (j.last_run_at) facts.push('last ' + fmtLocal(j.last_run_at));
      if (j.last_duration_s != null) facts.push('took ' + dur(j.last_duration_s || 0.4));
      if (j.exit_code) facts.push('exit ' + j.exit_code);
      h += '<div class="job-detail">'
        + (j.description ? '<div class="job-desc">' + esc(nodash(j.description)) + '</div>' : '')
        + '<div class="job-facts">' + esc(facts.join(' · ')) + '</div>'
        + (j.repo_path ? '<div class="job-facts">' + esc(j.repo_path) + '</div>' : '')
        + (j.history && j.history.length ? '<div class="job-facts">7d ' + historyHtml(j.history) + '</div>' : '')
        + (j.outcome ? '<div class="job-outcome-full">' + (j.outcome_kind === 'output' ? '<span class="job-out-label">last output </span>' : '') + esc(nodash(j.outcome)) + '</div>' : '')
        + (j.tickets && j.tickets.length ? '<div class="job-chips">' + chipsHtml(j.tickets, 50) + '</div>' : '')
        + '<pre class="job-log">' + (log == null ? 'Loading log...' : esc(log)) + '</pre></div>';
    }
    return h + '</div>';
  }

  function historyHtml(hist) {
    return '<span class="jobs-hist">' + hist.slice(-14).map(x => '<i class="' + (x.ok ? 'ok' : 'bad') + '" title="' + esc(x.at) + '"></i>').join('') + '</span>';
  }

  function groupsHtml(jobs) {
    const groups = new Map(); // server order = most recent run first, so groups inherit that order
    jobs.forEach(j => {
      const k = j.project || 'Other';
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(j);
    });
    return Array.from(groups.entries()).map(([name, list]) => {
      const collapsed = _collapsed.has(name);
      return '<div class="conv-folder-group jobs-group">'
        + '<div class="conv-folder-group-header" style="--chip-hue:' + hueOf(name) + ';" role="button" tabindex="0" data-jobs-group="' + esc(name) + '">'
        + '<button type="button" class="conv-folder-group-arrow" tabindex="-1">' + (collapsed ? '▸' : '▾') + '</button>'
        + '<span class="conv-folder-group-chip">' + esc(name) + '</span>'
        + '<span class="conv-folder-group-count">' + list.length + '</span></div>'
        + (collapsed ? '' : list.map(rowHtml).join('')) + '</div>';
    }).join('');
  }

  function render() {
    const el = document.getElementById('sidebarJobsHost');
    if (!el) return;
    let html;
    if (!_data) {
      html = '<div class="jobs-empty">Loading jobs...</div>';
    } else {
      const jobs = visibleJobs();
      html = headerHtml()
        + (jobs.length ? '<div class="jobs-list">' + groupsHtml(jobs) + '</div>'
          : '<div class="jobs-empty">No scheduled jobs' + (_host === 'hermes' && (_data.hosts.hermes || {}).status !== 'online' ? ' (Hermes unreachable)' : '') + '.</div>');
    }
    if (html !== _lastHtml || !el.firstChild) {
      const scroll = el.scrollTop;
      el.innerHTML = html;
      el.scrollTop = scroll;
      _lastHtml = html;
    }
  }

  async function loadLog(id) {
    try {
      const res = await (window.__cccBackgroundApiFetch || fetch)('/api/system/scheduled-jobs/log?lines=50&id=' + encodeURIComponent(id), { cache: 'no-store' });
      const d = await res.json();
      _logs.set(id, (d && d.log) ? d.log : '(no log output)');
    } catch (e) {
      _logs.set(id, 'Failed to load log: ' + e);
    }
    render();
    // Newest lines are at the bottom; show them.
    document.querySelectorAll('#sidebarJobsHost [data-job-id]').forEach(function (r) {
      if (r.getAttribute('data-job-id') === id) { const pre = r.querySelector('.job-log'); if (pre) pre.scrollTop = pre.scrollHeight; }
    });
  }

  function onClick(ev) {
    const seg = ev.target.closest('[data-jobs-host]');
    if (seg) {
      _host = seg.getAttribute('data-jobs-host');
      try { localStorage.setItem(HOST_KEY, _host); } catch (_) {}
      updateBadge();
      render();
      return;
    }
    const grp = ev.target.closest('[data-jobs-group]');
    if (grp) {
      const g = grp.getAttribute('data-jobs-group');
      if (_collapsed.has(g)) _collapsed.delete(g); else _collapsed.add(g);
      try { localStorage.setItem('ccc-jobs-collapsed', JSON.stringify(Array.from(_collapsed))); } catch (_) {}
      render();
      return;
    }
    if (ev.target.closest('.job-detail') || ev.target.closest('a.job-chip')) return;
    const row = ev.target.closest('[data-job-id]');
    if (!row) return;
    const id = row.getAttribute('data-job-id');
    if (_expanded.has(id)) { _expanded.delete(id); }
    else { _expanded.add(id); _logs.set(id, null); loadLog(id); }
    render();
  }

  async function poll() {
    if (_inflight) return;
    _inflight = true;
    try {
      const res = await (window.__cccBackgroundApiFetch || fetch)('/api/jobs', { cache: 'no-store' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const d = await res.json();
      if (d && d.ok) {
        _data = d;
        _lastFetch = Date.now();
        updateBadge();
        render();
      }
    } catch (_) {
      // keep last-good data; retry next tick
    } finally {
      _inflight = false;
    }
  }

  function mount() {
    const el = document.getElementById('sidebarJobsHost');
    if (!el) return;
    _lastHtml = '';
    if (!el._jobsWired) { el.addEventListener('click', onClick); el._jobsWired = true; }
    render();
    if (Date.now() - _lastFetch > POLL_MS) poll();
  }

  // One early fetch so the tab badge is populated; then poll only while the
  // Jobs tab is showing and the document is visible.
  setTimeout(poll, 4000);
  setInterval(function () {
    if (document.hidden || !isActive() || !document.getElementById('sidebarJobsHost')) return;
    const loading = _data && _data.hosts && _data.hosts.hermes && _data.hosts.hermes.status === 'loading';
    if (Date.now() - _lastFetch >= (loading ? 5000 : POLL_MS)) poll();
    else render(); // keep relative times fresh
  }, 5000);

  window.CCCJobsTab = { mount: mount, attentionCount: attentionCount };
})();
