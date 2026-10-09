// Terminating Puppeteer check. All API calls are fixtures; no account is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const os = require('node:os');
const { execFileSync } = require('node:child_process');
const root = path.join(__dirname, '..');
const puppeteer = require(process.env.CCC_TEST_PUPPETEER_MODULE || '../require-puppeteer.js');
const browserConfigRoot = process.env.CCC_TEST_PUPPETEER_MODULE
  ? path.dirname(process.env.CCC_TEST_PUPPETEER_MODULE) : root;
const { findChromePath } = require(path.join(browserConfigRoot, 'puppeteer-browser-config.js'));
const output = process.env.CCC_VERIFY_OUT || fs.mkdtempSync(path.join(os.tmpdir(), 'ccc-text-tools-'));
fs.mkdirSync(output, { recursive: true });
const checks = [], errors = [], writes = [], spellBodies = [];
const cache = new Map();
let held = null, holdNext = false, failNext = false, backendAvailable = true;
execFileSync(process.execPath, ['--test', path.join(root, 'tests/text-tools.test.cjs')], { stdio: 'inherit' });

function asset(relative) {
  if (cache.has(relative)) return cache.get(relative);
  let bytes;
  try { bytes = fs.readFileSync(path.join(root, relative)); }
  catch (error) {
    // Optional preview of a patch prepared from upstream Git objects.
    if (!process.env.CCC_TEST_ASSET_GIT) throw error;
    bytes = execFileSync('git', ['-C', process.env.CCC_TEST_ASSET_GIT, 'show',
      (process.env.CCC_TEST_ASSET_REF || 'HEAD') + ':' + relative], { maxBuffer: 8 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
  }
  cache.set(relative, bytes);
  return bytes;
}
const server = http.createServer((request, response) => {
  const pathname = new URL(request.url, 'http://localhost').pathname;
  const relative = pathname === '/' ? 'static/index.html' : pathname.slice(1);
  if (relative.split('/').includes('..')) { response.writeHead(404); response.end(); return; }
  try {
    const type = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
      '.svg': 'image/svg+xml', '.png': 'image/png' }[path.extname(relative)] || 'application/octet-stream';
    const bytes = asset(relative);
    response.writeHead(200, { 'Content-Type': type }); response.end(bytes);
  } catch (_) { response.writeHead(404); response.end(); }
});
const row = { id: 'example-chat', session_id: 'example-chat', display_name: 'Example conversation',
  first_message: 'An example conversation', source: 'codex', engine: 'codex',
  repo_path: '/projects/example', folder_path: '/projects/example', session_cwd: '/projects/example',
  session_cwd_exists: true, modified: Date.now() / 1000, state: 'idle', is_live: false, archived: false,
  spawned_via: 'ui', spawned_lane: 'coding' };

(async () => {
  let browser, page;
  let phase = 'setup';
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const base = 'http://127.0.0.1:' + server.address().port;
    browser = await puppeteer.launch({ executablePath: findChromePath(), args: ['--no-sandbox', '--disable-dev-shm-usage'] });
    page = await browser.newPage();
    page.setDefaultTimeout(30000);
    await page.setViewport({ width: 1440, height: 960 });
    await page.evaluateOnNewDocument(() => {
      localStorage.setItem('ccc-tour-done', '1');
      localStorage.setItem('ccc-pwa-install-dismissed', String(Date.now()));
      localStorage.setItem('ccc-sidebar-tab', 'coding');
      localStorage.setItem('ccc-archive-window', '7d');
      localStorage.setItem('ccc-separate-tabs', 'off');
      localStorage.setItem('ccc-spawn-cwd', '/projects/example');
      localStorage.setItem('ccc-session-view', 'list');
    });
    page.on('pageerror', error => errors.push(error.message));
    await page.setRequestInterception(true);
    page.on('request', request => {
      const url = new URL(request.url());
      const json = (data, status = 200) => request.respond({ status, contentType: 'application/json', body: JSON.stringify(data) });
      if (url.origin !== base) return request.abort();
      if (!url.pathname.startsWith('/api/')) return request.continue();
      if (url.pathname === '/api/text-tools') {
        if (request.method() === 'GET') return json({ ok: true, available: backendAvailable,
          backend: backendAvailable ? 'Configured command' : 'Spelling command unavailable.', max_chars: 20000 });
        const body = JSON.parse(request.postData());
        spellBodies.push(body);
        const finish = () => json({ ok: true, result: body.text.replace(/teh/g, 'the') });
        if (holdNext) { holdNext = false; held = finish; return; }
        if (failNext) { failNext = false; return json({ ok: false, error: 'Spelling correction timed out.' }, 504); }
        return finish();
      }
      if (!['GET', 'HEAD'].includes(request.method())) {
        writes.push(url.pathname); return json({ ok: true });
      }
      if (['/api/conversations/list', '/api/conversations/all'].includes(url.pathname)) return json({ ok: true, conversations: [row], count: 1, window: '7d' });
      if (['/api/sessions', '/api/conversations', '/api/sessions/spawned'].includes(url.pathname)) return json([]);
      if (url.pathname === '/api/objects') return json({ ok: true, objects: [] });
      if (url.pathname === '/api/repo/list') return json({ repos: [{ path: '/projects/example', label: 'Example' }], recent: [], rankings: [], by_kind: {} });
      if (url.pathname === '/api/spawn-defaults') return json({ ok: true, engine: 'codex' });
      if (url.pathname === '/api/project-memory') return json({ ok: true, projects: [] });
      if (url.pathname === '/api/handoff-sessions') return json({ ok: true, sessions: [] });
      if (url.pathname.startsWith('/api/group-chats/')) return json({ chats: [] });
      if (url.pathname === '/api/ux-fixes/health') return json({ queues: [], worker_session_ids: [] });
      if (url.pathname === '/api/wt/workers') return json({ workers: [] });
      if (url.pathname === '/api/composer/commands') return json({ ok: true, commands: [] });
      if (url.pathname === '/api/spawn-subagents') return json({ ok: true, subagents: [], modes: [] });
      if (url.pathname === '/api/spawn-mcps') return json({ ok: true, mcps: [] });
      if (url.pathname === '/api/modes' || url.pathname === '/api/spawn-modes') return json({ ok: true, modes: [] });
      if (url.pathname === '/api/session-status') return json({ state: 'idle', engine: 'codex', pending_steers: [] });
      if (url.pathname === '/api/conversations/events') return json({ events: [], count: 0 });
      if ((request.headers().accept || '').includes('text/event-stream') || url.pathname.endsWith('/stream')) {
        return request.respond({ status: 200, contentType: 'text/event-stream', body: ': fixture\n\n' });
      }
      return json({ ok: true });
    });
    await page.goto(base, { waitUntil: 'domcontentloaded', timeout: 60000 });
    phase = 'settings';
    assert.equal(await page.$$eval('.composer-text-tool', buttons => buttons.every(btn => btn.hidden)), true);
    assert.equal(spellBodies.length, 0);
    checks.push('tools default off and load without a model call');
    await page.click('#settingsBtn');
    await page.click('#settingsRailTab-tools');
    await page.click('[data-text-tool-toggle="layout"]');
    await page.click('[data-text-tool-toggle="spell"]');
    await page.waitForFunction(() => !document.querySelector('[data-text-tool="spell"]').disabled);
    assert.equal(await page.$$eval('[data-text-tool-toggle]', buttons =>
      buttons.every(btn => btn.getAttribute('aria-checked') === 'true')), true);
    await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 150)));
    await page.screenshot({ path: path.join(output, 'settings-desktop.png') });
    checks.push('Settings → Tools toggles are visible, labelled and persisted');
    await page.click('#settingsModalClose');
    await page.click('#sidebarNewBtn');
    await page.waitForFunction(() => document.querySelector('#convInputBar').getBoundingClientRect().height > 0);
    const layout = '#convInputBar [data-text-tool="layout"]';
    const spell = '#convInputBar [data-text-tool="spell"]';
    const draft = async (value, start = value.length, end = start) => page.$eval('#convInput', (el, target) => {
      el.value = target.value; el.focus(); el.setSelectionRange(target.start, target.end);
      el.dispatchEvent(new Event('input', { bubbles: true }));
    }, { value, start, end });
    const value = () => page.$eval('#convInput', el => el.value);
    const waitValue = expected => page.waitForFunction(expected => document.querySelector('#convInput').value === expected, {}, expected);

    phase = 'layout and undo';
    await draft('akuo'); await page.click(layout); await waitValue('שלום');
    assert.equal(spellBodies.length, 0);
    await page.keyboard.down('Control'); await page.keyboard.press('z'); await page.keyboard.up('Control');
    await waitValue('akuo');
    await draft('Keep akuo here', 5, 9); await page.click(layout); await waitValue('Keep שלום here');
    checks.push('offline layout fixes whole draft or selection, with native Undo');
    await page.evaluate(() => { document.execCommand = () => false; });
    await draft('akuo'); await page.click(layout); await waitValue('שלום');
    await page.keyboard.down('Control'); await page.keyboard.press('z'); await page.keyboard.up('Control');
    await waitValue('akuo');
    checks.push('Undo also works when native insertText is unavailable');

    phase = 'spell selection';
    const countBeforeWhitespace = spellBodies.length;
    await draft('Keep teh text', 4, 5); await page.click(spell);
    assert.equal(spellBodies.length, countBeforeWhitespace);
    assert.equal(await value(), 'Keep teh text');
    checks.push('a whitespace-only selection never sends the whole draft');
    await draft('Keep teh first; fix teh second', 16, 30);
    await page.click(spell); await waitValue('Keep teh first; fix the second');
    assert.equal(spellBodies.at(-1).text, 'fix teh second');
    checks.push('only the spelling selection is sent and replaced');
    await draft('teh whole draft'); await page.click(spell); await waitValue('the whole draft');
    checks.push('unselected spelling fixes the whole draft');
    await page.screenshot({ path: path.join(output, 'composer-desktop.png') });

    phase = 'stale draft';
    holdNext = true;
    await draft('teh draft'); await page.click(spell);
    await page.waitForFunction(() => document.querySelector('#convInputBar [data-text-tool="spell"]').disabled);
    await draft('newer draft');
    assert(held); await held(); held = null;
    await page.waitForFunction(() => !document.querySelector('#convInputBar [data-text-tool="spell"]').disabled);
    assert.equal(await value(), 'newer draft');
    checks.push('in-flight correction cannot overwrite an edited draft');

    phase = 'failure';
    failNext = true; await draft('teh original'); await page.click(spell);
    await page.waitForFunction(() => document.querySelector('#convInputBar .composer-text-tools-status').textContent.includes('timed out'));
    assert.equal(await value(), 'teh original');
    checks.push('provider failure is visible and retains the original draft');

    phase = 'navigation with an identical draft';
    holdNext = true;
    await draft('teh same'); await page.click(spell);
    await page.evaluate(() => window.cccOpenSession('example-chat'));
    await draft('teh same');
    assert(held); await held().catch(() => {}); held = null;
    await page.waitForFunction(() => !document.querySelector('#convInputBar [data-text-tool="spell"]').disabled);
    assert.equal(await value(), 'teh same');
    checks.push('changing conversations discards a result even if the new draft is identical');
    await page.click('#sidebarNewBtn'); await draft('teh original');

    phase = 'cloned pane';
    await page.evaluate(() => {
      const clone = document.querySelector('#convInputBar').cloneNode(true);
      clone.id = 'testClone'; clone.querySelectorAll('[id]').forEach(el => el.removeAttribute('id'));
      clone.style.cssText = 'display:block;position:fixed;left:500px;top:120px;width:500px;z-index:100;';
      document.body.appendChild(clone);
      const input = clone.querySelector('textarea'); input.value = 'akuo'; input.setSelectionRange(4, 4);
    });
    await page.click('#testClone [data-text-tool="layout"]');
    assert.equal(await page.$eval('#testClone textarea', el => el.value), 'שלום');
    assert.equal(await value(), 'teh original');
    checks.push('ID-free split-pane clones correct their own textarea');
    await page.$eval('#testClone', el => el.remove());

    phase = 'phone';
    await page.setViewport({ width: 393, height: 851 });
    await draft('akuo'); await page.click(layout); await waitValue('שלום');
    const boxes = await page.$$eval('#convInputBar .composer-text-tool', buttons => buttons.map(btn => {
      const r = btn.getBoundingClientRect(); return { left: r.left, right: r.right, width: r.width, height: r.height };
    }));
    assert(boxes.every(r => r.left >= 0 && r.right <= 393 && r.width >= 32 && r.height >= 32));
    await page.screenshot({ path: path.join(output, 'composer-phone.png') });
    checks.push('phone buttons remain visible and usable without horizontal overflow');

    phase = 'Simple Home';
    await page.evaluate(() => {
      window.cccSetUiMode('simple');
      document.querySelector('#mobileBackBtn').click();
      const section = document.querySelector('#simpleHome [data-simple-section="composer"]');
      if (section.classList.contains('simple-section-collapsed')) {
        section.querySelector('.simple-section-toggle').click();
      }
      const input = document.querySelector('#simpleComposerInput');
      input.value = 'akuo'; input.focus(); input.setSelectionRange(4, 4);
    });
    await page.click('#simpleHome [data-text-tool="layout"]');
    assert.equal(await page.$eval('#simpleComposerInput', el => el.value), 'שלום');
    await page.screenshot({ path: path.join(output, 'simple-phone.png') });
    checks.push('Simple Home corrections operate on its own composer on a phone');
    await page.evaluate(() => window.cccSetUiMode('advanced'));

    phase = 'unavailable and off';
    // Open the existing desktop Settings entry, then exercise its phone layout.
    // The advanced sidebar footer is covered by the phone navigation bar.
    await page.setViewport({ width: 1440, height: 960 });
    backendAvailable = false;
    await page.click('#settingsBtn'); await page.click('#settingsRailTab-tools');
    await page.waitForFunction(() => document.querySelector('#settingsSpellingStatus').textContent.includes('unavailable'));
    await page.setViewport({ width: 393, height: 851 });
    await page.screenshot({ path: path.join(output, 'settings-phone.png') });
    assert.equal(await page.$eval(spell, el => el.disabled), true);
    await page.click('[data-text-tool-toggle="layout"]'); await page.click('[data-text-tool-toggle="spell"]');
    await page.click('#settingsModalClose');
    assert.equal(await page.$$eval('.composer-text-tool', buttons => buttons.every(btn => btn.hidden)), true);
    await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 });
    assert.equal(await page.$$eval('.composer-text-tool', buttons => buttons.every(btn => btn.hidden)), true);
    checks.push('missing backend is explained; disabling tools survives reload');
    assert.deepEqual(errors, []);
    assert(!writes.some(url => /inject-input|sessions\/spawn|sessions\/resume/.test(url)));
    checks.push('no browser errors, coding-session writes or real model calls');
    fs.writeFileSync(path.join(output, 'browser.json'), JSON.stringify({ checks, errors, writes, spellBodies }, null, 2));
    console.log(JSON.stringify({ checks: checks.length, errors, output }));
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(output, 'failed.png') }).catch(() => {});
    fs.writeFileSync(path.join(output, 'browser.json'), JSON.stringify({ phase, error: error.message, checks, errors, writes, spellBodies }, null, 2));
    throw error;
  } finally {
    if (browser) await browser.close();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
})();
