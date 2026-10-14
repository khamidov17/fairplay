// mprobe.js <user> <url> -- at 390px, list the innermost elements that stick out past the viewport
const { chromium, devices } = require('/opt/node22/lib/node_modules/playwright');
const BASE = 'http://127.0.0.1:' + (process.env.PORT || '8105');
(async () => {
  const [user, url] = process.argv.slice(2);
  const browser = await chromium.launch();
  const context = await browser.newContext({ ...devices['iPhone 13'], viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  if (user !== 'anon') {
    await page.goto(BASE + '/accounts/login/');
    await page.fill('#id_username', user);
    await page.fill('#id_password', 'fairplay-dev');
    await Promise.all([page.waitForNavigation(), page.click('form.form-area button[type=submit]')]);
  }
  await page.goto(BASE + url, { waitUntil: 'load' });
  if (process.env.CSS) await page.addStyleTag({ content: process.env.CSS });
  await page.waitForTimeout(800);
  if (process.env.SHOT) await page.screenshot({ path: process.env.SHOT, fullPage: true });
  if (process.env.CHAIN) console.log(await page.evaluate((s) => { const o = []; let e = document.querySelector(s); while (e && e !== document.documentElement) { const cs = getComputedStyle(e); o.push(e.tagName + '.' + e.className + ' w=' + Math.round(e.getBoundingClientRect().width) + ' disp=' + cs.display + ' fd=' + cs.flexDirection + ' minw=' + cs.minWidth + ' width=' + cs.width + ' gtc=' + cs.gridTemplateColumns); e = e.parentElement; } return o.join('\n'); }, process.env.CHAIN));
  const r = await page.evaluate(() => {
    const vw = document.documentElement.clientWidth;
    const name = (el) => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\s+/).join('.') : '');
    const path = (el) => { const p = []; let e = el; for (let i = 0; e && e !== document.body && i < 6; i++, e = e.parentElement) p.unshift(name(e)); return p.join(' > '); };
    const out = [];
    for (const el of document.querySelectorAll('main *, #content *')) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.right <= vw + 2) continue;
      const cs = getComputedStyle(el);
      // skip things inside a horizontally scrollable box
      let p = el.parentElement; let scroll = false;
      while (p && p !== document.body) { const o = getComputedStyle(p).overflowX; if (o === 'auto' || o === 'scroll' || o === 'hidden') { scroll = true; break; } p = p.parentElement; }
      if (scroll) continue;
      const kids = [...el.children].filter((c) => c.getBoundingClientRect().right > vw + 2);
      if (kids.length) continue;
      out.push(path(el) + ' | w=' + Math.round(r.width) + ' R=' + Math.round(r.right) + ' ws=' + cs.whiteSpace + ' minw=' + cs.minWidth + ' w=' + cs.width);
    }
    return { sw: document.scrollingElement.scrollWidth, bodyW: document.body.getBoundingClientRect().width, out: out.slice(0, 25) };
  });
  console.log(JSON.stringify(r, null, 1));
  await browser.close();
})();
