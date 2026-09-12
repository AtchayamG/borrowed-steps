// Borrowed Steps live demo capture. Uses the public Vercel release and CDP frames.
const puppeteer = require('D:/Work/Codex/Hackathon Projects/LumaLoad/node_modules/puppeteer');
const fs = require('fs');
const path = require('path');

const OUT = path.join(__dirname, 'frames');
const BASE = 'https://borrowed-steps.vercel.app';
const W = 1920, H = 1080;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const frames = [], marks = [];
let n = 0, t0 = 0;
const mark = name => { marks.push({ name, t: Date.now() - t0 }); console.log('MARK', name); };

async function smoothScroll(page, y, ms) {
  await page.evaluate(async (target, duration) => {
    const start = window.scrollY, delta = target - start, begin = performance.now();
    await new Promise(resolve => {
      const step = now => {
        const p = Math.min(1, (now - begin) / duration);
        const eased = p < .5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
        window.scrollTo(0, start + delta * eased);
        p < 1 ? requestAnimationFrame(step) : resolve();
      };
      requestAnimationFrame(step);
    });
  }, y, ms);
}

async function clickButton(page, text, timeout = 8000, last = false) {
  await page.waitForFunction(t => [...document.querySelectorAll('button')]
    .some(button => button.offsetParent !== null && button.innerText.includes(t)), { timeout }, text);
  const buttons = await page.$$('button');
  const matches = [];
  for (const button of buttons) {
    const visible = await button.evaluate(e => e.offsetParent !== null).catch(() => false);
    const label = await button.evaluate(e => e.innerText || '').catch(() => '');
    if (visible && label.includes(text)) matches.push(button);
  }
  const button = matches[last ? matches.length - 1 : 0];
  await button.evaluate(e => e.scrollIntoView({ block: 'center' }));
  await sleep(400);
  await button.click();
}

(async () => {
  fs.rmSync(OUT, { recursive: true, force: true });
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({
    headless: 'new',
    defaultViewport: { width: W, height: H },
    args: ['--hide-scrollbars', '--force-device-scale-factor=1', '--window-size=1920,1080']
  });
  const page = await browser.newPage();
  const client = await page.target().createCDPSession();
  t0 = Date.now();
  client.on('Page.screencastFrame', async ({ data, sessionId }) => {
    const f = `f${String(++n).padStart(6, '0')}.jpg`;
    fs.writeFileSync(path.join(OUT, f), Buffer.from(data, 'base64'));
    frames.push({ f, t: Date.now() - t0 });
    try { await client.send('Page.screencastFrameAck', { sessionId }); } catch {}
  });

  await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 60000 });
  await client.send('Page.startScreencast', { format: 'jpeg', quality: 90, maxWidth: W, maxHeight: H, everyNthFrame: 1 });
  await sleep(2800); mark('landing');
  await clickButton(page, 'Start Synthetic Workspace');
  await sleep(5000); mark('workspace');
  await smoothScroll(page, 430, 1400); await sleep(2200); mark('inventory');
  await smoothScroll(page, 840, 1400); await sleep(2200); mark('assistant');
  await smoothScroll(page, 0, 1200); await sleep(1200);

  // Register a deterministic synthetic request using the live form.
  await page.$eval('input[type="datetime-local"]', e => {
    e.value = '2026-09-19T13:30';
    e.dispatchEvent(new Event('input', { bubbles: true }));
    e.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await clickButton(page, 'Register Structured Request');
  await sleep(4200); mark('request_created');
  await smoothScroll(page, 900, 1400); await sleep(1800);

  try {
    await clickButton(page, 'Allocate Equipment');
    await sleep(1000); mark('allocation_review');
    const checkbox = await page.$('input[type="checkbox"]');
    if (checkbox) await checkbox.click();
    await clickButton(page, 'Confirm Allocation', 8000, true);
    await sleep(4000); mark('reserved');
  } catch (error) { console.log('allocation skipped', error.message); }

  try {
    await clickButton(page, 'Confirm Pickup');
    await sleep(800);
    const checkbox = await page.$('input[type="checkbox"]');
    if (checkbox) await checkbox.click();
    await clickButton(page, 'Confirm Pickup', 8000, true);
    await sleep(4000); mark('picked_up');
  } catch (error) { console.log('pickup skipped', error.message); }

  await smoothScroll(page, 1500, 1200); await sleep(2200); mark('audit');
  await client.send('Page.stopScreencast');
  await sleep(700);
  fs.writeFileSync(path.join(__dirname, 'manifest.json'), JSON.stringify({ frames, marks, total: Date.now() - t0 }, null, 2));
  console.log('FRAMES', frames.length, 'TOTAL_MS', Date.now() - t0);
  await browser.close();
})().catch(error => { console.error(error.stack || error); process.exit(1); });
