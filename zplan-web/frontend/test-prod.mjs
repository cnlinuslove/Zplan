import { chromium } from 'playwright';
const URL = 'http://127.0.0.1:8000';
const errors = [];
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
page.on('console', msg => { if (msg.type() === 'error') errors.push(msg.text()); });
page.on('pageerror', err => errors.push(err.message));
try {
  console.log(`Loading ${URL}...`);
  await page.goto(URL, { timeout: 15000, waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);
  const text = await page.textContent('#root');
  console.log(`Content: "${text?.slice(0, 200) || '(empty)'}"`);
  await page.screenshot({ path: 'prod-screenshot.png' });
  if (errors.length) { console.log(`\n❌ ${errors.length} errors:`); errors.forEach(e => console.log(' -', e)); process.exit(1); }
  console.log('\n✅ PASSED');
} catch(err) { console.log(`\n❌ ${err.message}`); process.exit(1); }
finally { await browser.close(); }
