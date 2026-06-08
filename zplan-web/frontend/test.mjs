// 用 headless Chrome 自测前端，不用麻烦用户
import { chromium } from 'playwright';

const URL = 'http://127.0.0.1:5173';
const errors = [];

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();

// 收集所有 console 错误
page.on('console', msg => {
  if (msg.type() === 'error') errors.push(msg.text());
  console.log(`  [${msg.type()}] ${msg.text().slice(0, 120)}`);
});
page.on('pageerror', err => {
  errors.push(err.message);
  console.log(`  [PAGE ERROR] ${err.message}`);
});

try {
  console.log(`Loading ${URL}...`);
  await page.goto(URL, { timeout: 15000, waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2000); // 等 React 渲染完

  // 检查页面内容
  const text = await page.textContent('#root');
  console.log(`\nPage content: "${text?.slice(0, 200) || '(empty)'}"`);

  // 截图保存
  await page.screenshot({ path: 'test-screenshot.png' });
  console.log('Screenshot saved: test-screenshot.png');

  if (errors.length > 0) {
    console.log(`\n❌ FAILED: ${errors.length} error(s):`);
    errors.forEach(e => console.log(`  - ${e}`));
    process.exit(1);
  } else {
    console.log('\n✅ PASSED: No JS errors');
  }
} catch (err) {
  console.log(`\n❌ PAGE LOAD ERROR: ${err.message}`);
  process.exit(1);
} finally {
  await browser.close();
}
