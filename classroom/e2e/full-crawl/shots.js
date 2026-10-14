// Screenshots + layout checks of the main pages per role, desktop and mobile (agent E).
// usage: node shots.js <outdir> <role:mode> ...   mode = desktop | mobile
const { chromium, devices } = require('/opt/node22/lib/node_modules/playwright');
const fs = require('fs');

const BASE = 'http://127.0.0.1:' + (process.env.PORT || '8105');
const USERS = { anon: null, student: 'ab101', ta: 'ta_wang', teacher: 'prof_li', admin: 'admin' };
const CLS = '/organization/2-cs101/class/1-cs101-001';
const PAGES = {
  anon: ['/', '/problems/', '/contests/', '/organizations/', '/users/', '/submissions/', '/accounts/login/',
    '/accounts/register/', '/blog/', '/post/1-welcome-cs101', '/about/', '/status/', '/problem/aplusb',
    '/stats/language/', '/runtimes/'],
  student: ['/', '/organizations/', '/organization/2-cs101', CLS, '/contests/', '/contest/cs101_hw1',
    '/contest/cs101_exam1', '/contest/cs101_quiz1', '/contest/cs101_quiz1/ranking/', '/problem/cs_sum',
    '/problem/cs_mcq_loop', '/problems/', '/submissions/', '/user', '/edit/profile/'],
  ta: ['/', '/organization/2-cs101', CLS, CLS + '/edit', '/contest/cs101_hw1', '/contest/cs101_hw1/grade',
    '/contest/cs101_exam1/ranking/', '/contest/cs101_quiz1/analytics', '/problems/', '/problem/cs_sum',
    '/submissions/', '/tickets/', '/user', '/organization/2-cs101/users', '/contests/'],
  teacher: ['/', '/organization/2-cs101', CLS, '/contests/create/', '/contest/cs101_hw1',
    '/contests/cs101_hw1/edit/', '/contest/cs101_hw1/grade', '/contest/cs101_quiz1/analytics',
    '/contest/cs101_exam1/ranking/', '/problems/new/', '/problem/cs_sum', '/problem/cs_sum/test_data',
    '/problems/', '/organization/2-cs101/edit', '/organizations/notifications/'],
  admin: ['/admin/', '/admin/judge/problem/', '/admin/judge/problem/1/change/', '/admin/judge/contest/add/',
    '/admin/judge/organization/', '/admin/judge/profile/', '/admin/auth/user/1/change/', '/admin/judge/submission/',
    '/organizations/instructor-requests/', '/organizations/requests/', '/tickets/', '/ticket/1', '/contests/',
    '/problems/', '/submissions/'],
};

async function login(page, user) {
  await page.goto(BASE + '/accounts/login/');
  await page.fill('#id_username', user);
  await page.fill('#id_password', 'fairplay-dev');
  await Promise.all([page.waitForNavigation({ waitUntil: 'domcontentloaded' }), page.click('form.form-area button[type=submit]')]);
}

function layoutCheck() {
  const vw = document.documentElement.clientWidth;
  const vh = window.innerHeight;
  const se = document.scrollingElement;
  const out = { overflow: se.scrollWidth - se.clientWidth, wide: [], fixed: [], overlaps: [] };
  const name = (el) => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
    (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.') : '');
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (r.right > vw + 2 && out.wide.length < 6) {
      // only report the outermost offender
      let p = el.parentElement; let parentWide = false;
      while (p && p !== document.body) { if (p.getBoundingClientRect().right > vw + 2) { parentWide = true; break; } p = p.parentElement; }
      if (!parentWide) out.wide.push(name(el) + ' right=' + Math.round(r.right));
    }
    if ((cs.position === 'fixed' || cs.position === 'sticky') && r.height > 0) out.fixed.push({ n: name(el), r: [r.left, r.top, r.right, r.bottom] });
  }
  const fx = out.fixed.filter((f) => f.r[3] > 0 && f.r[1] < vh);
  for (let i = 0; i < fx.length; i++) {
    for (let j = i + 1; j < fx.length; j++) {
      const a = fx[i].r; const b = fx[j].r;
      const ix = Math.min(a[2], b[2]) - Math.max(a[0], b[0]);
      const iy = Math.min(a[3], b[3]) - Math.max(a[1], b[1]);
      if (ix > 4 && iy > 4) out.overlaps.push(fx[i].n + ' x ' + fx[j].n);
    }
  }
  out.fixedTall = fx.filter((f) => (f.r[3] - Math.max(0, f.r[1])) > vh * 0.4 && (f.r[2] - f.r[0]) > vw * 0.8).map((f) => f.n);
  out.fixed = fx.map((f) => f.n + ' ' + f.r.map(Math.round).join(','));
  return out;
}

(async () => {
  const outdir = process.argv[2];
  fs.mkdirSync(outdir, { recursive: true });
  const browser = await chromium.launch();
  const report = {};
  for (const spec of process.argv.slice(3)) {
    const [role, mode] = spec.split(':');
    const opts = mode === 'mobile' ? { ...devices['iPhone 13'], viewport: { width: 390, height: 844 } } : { viewport: { width: 1366, height: 900 } };
    const context = await browser.newContext(opts);
    await context.route('**/*', (r) => (r.request().url().startsWith(BASE) || /^(data|blob):/.test(r.request().url()) ? r.continue() : r.abort()));
    const page = await context.newPage();
    page.on('dialog', (d) => d.dismiss().catch(() => {}));
    if (USERS[role]) await login(page, USERS[role]);
    for (const [i, p] of PAGES[role].entries()) {
      const resp = await page.goto(BASE + p, { waitUntil: 'load' }).catch((e) => null);
      await page.waitForTimeout(500);
      const file = `${outdir}/${role}-${mode}-${String(i).padStart(2, '0')}.png`;
      if (mode === 'mobile') await page.screenshot({ path: file, fullPage: true, clip: { x: 0, y: 0, width: 390, height: 1700 } }).catch(() => page.screenshot({ path: file }));
      else await page.screenshot({ path: file });
      const lc = await page.evaluate(layoutCheck).catch((e) => ({ err: String(e) }));
      report[`${role}-${mode}-${i}`] = { url: p, status: resp ? resp.status() : 0, final: page.url().replace(BASE, ''), ...lc };
    }
    await context.close();
  }
  await browser.close();
  fs.writeFileSync(outdir + '/layout.json', JSON.stringify(report, null, 1));
})();
