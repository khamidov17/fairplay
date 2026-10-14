// Full click-through crawl of the local Classroom dev server (agent E).
// usage: (manage.py shell < gen_urls.py > urls.json) then: PORT=8000 node crawl.js <out.json> <role> [role ...]
// roles: anon student student_hw student_exam ta teacher admin
const { chromium } = require('/opt/node22/lib/node_modules/playwright');
const fs = require('fs');
const path = require('path');

const BASE = 'http://127.0.0.1:' + (process.env.PORT || '8105');
const PASSWORD = 'fairplay-dev';
const ROLES = {
  anon: null, student: 'ab101', student_hw: 'ab101', student_exam: 'ab101', ta: 'ta_wang', teacher: 'prof_li',
  admin: 'admin',
};
const JOIN = { student_hw: 'cs101_hw1', student_exam: 'cs101_exam1' };
const ROUTES = JSON.parse(fs.readFileSync(path.join(__dirname, 'urls.json'))).urls.map((u) => u.url);
// ONLY=/a,/b visits just those pages (no link following): for reproducing one finding.
const ONLY = process.env.ONLY ? process.env.ONLY.split(',') : null;
const SEEDS = ONLY || [...new Set([
  '/', '/problems/', '/contests/', '/organizations/', '/users/', '/submissions/', '/user',
  '/contest/cs101_hw1', '/contest/cs101_exam1', '/contest/cs101_quiz1', '/contest/cs101_quiz2',
  '/contest/math201_hw1', '/organization/2-cs101', '/organization/2-cs101/class/1-cs101-001',
  ...ROUTES,
])];
// Never GET these: session changes, logout, heavy exports, things that could change data on GET.
const SKIP_URL = /(logout|\/i18n\/|\/impersonate\/|\/static\/|\/media\/|export-pdf|export-word|\/pdf(\/|$)|test_data\/init|\/ai-(grade|hint)|\/admin\/(jsi18n|logout)|\/admin\/.*\/(delete|history)\/|\/leave$|\/accounts\/(two_factor|webauthn)|\/edit\/(webauthn|totp)|\/login\/|\/complete\/|\/disconnect\/)/i;
// These answer with a file, not a page: check the status only.
const FILE_URL = /(download|\/export\/|\/raw$|\.zip|\.ics$|\/feed\/|\/tester$|\/file$|sitemap\.xml|manifest\.json|browserconfig\.xml|\.png$|\.svg$|\/data\/)/i;
const SKIP_CLICK = /(delete|remove|leave|log ?out|sign ?out|finali|release|submit|save|reset|clear|regrade|rejudge|abort|ban|disqualif|grade|\bai\b|hint|run\b|upload|import|create|add\b|send|post|vote|follow|subscribe|join|publish|unpublish|generate|archive|kick|approve|reject|close|reopen|transfer|unlink|disable|enable|revoke|confirm|apply|restore|lock|unlock|mark|exit|stop|start|end\b|harvest|recompute|rescore|refresh|retry|duplicate|copy to|move|impersonat)/i;
const PER_PATTERN = parseInt(process.env.PER_PATTERN || '3', 10);
const MAX_CLICKS = 30;
const TEXT_BAD = /(\{\{|\{%|%\}|\}\}|Traceback \(most recent|\[object Object\]|\bNaN\b|\bundefined\b|TemplateSyntaxError|jinja2\.exceptions|gettext\(|_\(['"])/;

function pattern(url) {
  const u = new URL(url);
  // Admin change-list filters combine without end: one pattern for "some filter".
  if (u.pathname.startsWith('/admin/')) return u.pathname.replace(/\d+/g, 'N') + (u.search ? '?Q' : '');
  return u.pathname.replace(/\d+/g, 'N') + (u.search ? '?' + [...u.searchParams.keys()].sort().join('&') : '');
}

async function login(page, user) {
  await page.goto(BASE + '/accounts/login/');
  await page.fill('#id_username', user);
  await page.fill('#id_password', PASSWORD);
  await Promise.all([page.waitForNavigation({ waitUntil: 'domcontentloaded' }), page.click('form.form-area button[type=submit]')]);
  if (page.url().includes('/accounts/login')) throw new Error('login failed for ' + user);
}

async function csrf(context) {
  const c = (await context.cookies()).find((x) => x.name === 'csrftoken');
  return c ? c.value : '';
}

async function post(context, url) {
  const r = await context.request.post(BASE + url, {
    form: { csrfmiddlewaretoken: await csrf(context) }, headers: { Referer: BASE + url }, maxRedirects: 0,
  });
  return r.status();
}

async function crawlRole(browser, role, user) {
  const context = await browser.newContext({ viewport: { width: 1366, height: 900 } });
  const external = {};
  await context.route('**/*', (route) => {
    const u = route.request().url();
    if (u.startsWith(BASE) || u.startsWith('data:') || u.startsWith('blob:')) return route.continue();
    const host = new URL(u).host;
    external[host] = external[host] || [];
    if (external[host].length < 5) external[host].push(u.slice(0, 200) + '  [on ' + current + ']');
    return route.abort();
  });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  const findings = [];
  let current = '';
  page.on('pageerror', (err) => findings.push({ role, url: current, kind: 'js-exception', detail: String(err.message || err).slice(0, 400) }));
  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      const text = msg.text();
      if (/Failed to load resource/.test(text)) return; // reported via response handler
      findings.push({ role, url: current, kind: 'console-error', detail: text.slice(0, 400) });
    }
  });
  page.on('response', (resp) => {
    const u = resp.url();
    if (!u.startsWith(BASE)) return;
    if (resp.status() >= 400 && resp.request().resourceType() !== 'document') {
      findings.push({ role, url: current, kind: 'subrequest-' + resp.status(), detail: u.replace(BASE, '') });
    }
  });
  page.on('requestfailed', (req) => {
    const u = req.url();
    if (!u.startsWith(BASE)) return;
    const f = req.failure() ? req.failure().errorText : '';
    if (/ERR_ABORTED/.test(f)) return; // navigation away mid-load
    findings.push({ role, url: current, kind: 'request-failed', detail: u.replace(BASE, '') + ' ' + f });
  });
  page.on('dialog', (d) => d.dismiss().catch(() => {}));
  if (user) await login(page, user);
  if (JOIN[role]) {
    const st = await post(context, '/contest/' + JOIN[role] + '/join');
    console.log(role, 'join', JOIN[role], st);
  }

  const queue = SEEDS.map((p) => BASE + p);
  const seen = new Set();
  const patterns = {};
  const clickedPattern = new Set();
  const visited = [];
  const status = {}; // url -> status (pages visited or links checked)
  const linkedFrom = {}; // url -> first page showing a link to it
  const seedSet = new Set(SEEDS.map((p) => BASE + p));
  while (queue.length) {
    const url = queue.shift().split('#')[0];
    if (seen.has(url)) continue;
    seen.add(url);
    const rel = url.replace(BASE, '');
    if (!url.startsWith(BASE) || SKIP_URL.test(rel)) continue;
    const pat = pattern(url);
    patterns[pat] = (patterns[pat] || 0) + 1;
    if (!seedSet.has(url) && patterns[pat] > PER_PATTERN) continue;
    current = rel;
    if (FILE_URL.test(rel)) {
      try {
        const r = await context.request.get(url, { maxRedirects: 5, timeout: 30000 });
        status[rel] = r.status();
        visited.push({ url: rel, status: r.status(), final: r.url().replace(BASE, ''), file: true });
        if (r.status() >= 500) findings.push({ role, url: rel, kind: 'http-' + r.status(), detail: 'file' });
      } catch (e) {
        findings.push({ role, url: rel, kind: 'navigation-failed', detail: String(e.message).slice(0, 300) });
      }
      continue;
    }
    let st = 0;
    let ctype = '';
    try {
      const resp = await page.goto(url, { waitUntil: 'domcontentloaded' });
      st = resp ? resp.status() : 0;
      ctype = resp ? (resp.headers()['content-type'] || '') : '';
      await page.waitForLoadState('load', { timeout: 8000 }).catch(() => {});
      await page.waitForTimeout(200);
    } catch (e) {
      if (/Download is starting/.test(e.message)) {
        status[rel] = 200;
        visited.push({ url: rel, status: 200, final: rel, file: true });
        continue;
      }
      findings.push({ role, url: current, kind: 'navigation-failed', detail: String(e.message).slice(0, 300) });
      continue;
    }
    status[rel] = st;
    const final = page.url().replace(BASE, '');
    visited.push({ url: rel, status: st, final, seed: seedSet.has(url) });
    if (st >= 500) {
      const body = await page.evaluate(() => document.body ? document.body.innerText.slice(0, 300) : '').catch(() => '');
      findings.push({ role, url: current, kind: 'http-' + st, detail: ((await page.title().catch(() => '')) + ' | ' + body).slice(0, 400) });
    }
    if (st >= 400 || !page.url().startsWith(BASE)) continue;
    if (/json/.test(ctype) && linkedFrom[url]) findings.push({ role, url: current, kind: 'raw-json-page', detail: 'linked from ' + linkedFrom[url] });
    if (!/html/.test(ctype)) continue;

    // Page text checks + horizontal overflow.
    const check = await page.evaluate((re) => {
      const r = new RegExp(re);
      const t = document.body ? document.body.innerText : '';
      const m = t.match(r);
      const se = document.scrollingElement;
      return {
        bad: m ? t.slice(Math.max(0, m.index - 80), m.index + 80).replace(/\s+/g, ' ') : '',
        overflow: se ? se.scrollWidth - se.clientWidth : 0,
      };
    }, TEXT_BAD.source).catch(() => ({}));
    if (check.bad) findings.push({ role, url: current, kind: 'suspicious-text', detail: check.bad });
    if (check.overflow > 2) {
      // Widgets (select2) settle after load: only report overflow that is still there a second later.
      await page.waitForTimeout(1000);
      check.overflow = await page.evaluate(() => document.scrollingElement.scrollWidth - document.scrollingElement.clientWidth).catch(() => 0);
    }
    if (check.overflow > 2) findings.push({ role, url: current, kind: 'h-overflow', detail: check.overflow + 'px' });

    // Collect links.
    const links = await page.$$eval('a[href]', (as) => as.map((a) => a.href)).catch(() => []);
    for (const l0 of links) {
      const l = l0.split('#')[0];
      if (!l.startsWith(BASE)) continue;
      if (!linkedFrom[l]) linkedFrom[l] = current;
      if (!seen.has(l) && !ONLY) queue.push(l);
    }

    // Click non-destructive controls on the first page of each pattern.
    if (clickedPattern.has(pat)) continue;
    clickedPattern.add(pat);
    const handles = await page.$$('button[type=button], [role=tab], a[href="#"], a[href^="javascript"], summary, [data-toggle], .tab, .tabs a, .dropdown-toggle, label.switch');
    let clicks = 0;
    for (const h of handles) {
      if (clicks >= MAX_CLICKS) break;
      let label = '';
      try {
        if (!(await h.isVisible())) continue;
        label = ((await h.innerText().catch(() => '')) || (await h.getAttribute('title')) || (await h.getAttribute('aria-label')) || (await h.getAttribute('id')) || '').trim().slice(0, 60);
        const cls = (await h.getAttribute('class')) || '';
        if (SKIP_CLICK.test(label) || SKIP_CLICK.test(cls) || /danger|delete/.test(cls)) continue;
        const before = findings.length;
        const beforeUrl = page.url();
        await h.click({ timeout: 2500 });
        clicks++; if (ONLY) console.log("click", label, findings.length);
        await page.waitForTimeout(200);
        for (let i = before; i < findings.length; i++) findings[i].detail += '  [after clicking "' + label + '"]';
        if (page.url() !== beforeUrl) {
          await page.goto(url, { waitUntil: 'domcontentloaded' }).catch(() => {});
          break;
        }
        await page.keyboard.press('Escape').catch(() => {});
      } catch (e) {
        if (!/detached|not visible|outside of the viewport|intercepts pointer|Timeout|not attached|not stable/.test(e.message)) {
          findings.push({ role, url: current, kind: 'click-failed', detail: label + ': ' + String(e.message).slice(0, 200) });
        }
      }
    }
  }

  // Every link shown to this role: check the ones not visited (pattern cap) with a plain GET.
  for (const [l, from] of Object.entries(linkedFrom)) {
    const rel = l.replace(BASE, '');
    if (SKIP_URL.test(rel) || rel in status) continue;
    try {
      const r = await context.request.get(l, { maxRedirects: 5, timeout: 30000 });
      status[rel] = r.status();
    } catch (e) {
      status[rel] = -1;
    }
  }
  for (const [l, from] of Object.entries(linkedFrom)) {
    const rel = l.replace(BASE, '');
    if (status[rel] >= 400 || status[rel] === -1) findings.push({ role, url: from, kind: 'broken-link-' + status[rel], detail: rel });
  }
  if (JOIN[role]) console.log(role, 'leave', await post(context, '/contest/' + JOIN[role] + '/leave'));
  await context.close();
  return { visited, findings, external, linksChecked: Object.keys(linkedFrom).length };
}

(async () => {
  const out = process.argv[2];
  const roles = process.argv.slice(3);
  const browser = await chromium.launch();
  const report = {};
  for (const role of roles) {
    const t = Date.now();
    report[role] = await crawlRole(browser, role, ROLES[role]);
    console.log(role, 'pages', report[role].visited.length, 'links', report[role].linksChecked, 'findings',
      report[role].findings.length, ((Date.now() - t) / 1000).toFixed(0) + 's');
    fs.writeFileSync(out, JSON.stringify(report, null, 1));
  }
  await browser.close();
})();
