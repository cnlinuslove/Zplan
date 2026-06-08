// 端到端测试每个功能
import { chromium } from 'playwright';

const BASE = 'http://127.0.0.1:8000';
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];

page.on('console', msg => { if (msg.type() === 'error') errors.push(msg.text()); });
page.on('pageerror', err => errors.push(err.message));

async function test(name, fn) {
  console.log(`\n--- ${name} ---`);
  try { await fn(); console.log(`  ✅ PASSED`); }
  catch(e) { console.log(`  ❌ FAILED: ${e.message}`); errors.push(`${name}: ${e.message}`); }
}

try {
  // 1. 首页加载
  await test('首页加载', async () => {
    await page.goto(BASE, { waitUntil: 'networkidle', timeout: 15000 });
    await page.waitForTimeout(1500);
    const content = await page.textContent('#root');
    if (!content.includes('对话')) throw new Error('首页未渲染');
  });

  // 2. 导航到选股榜单
  await test('选股榜单加载', async () => {
    await page.click('text=选股榜单');
    await page.waitForTimeout(1500);
    const t = await page.textContent('.ant-card');
    if (!t.includes('选股榜单')) throw new Error('选股页未加载');
    // 看有多少行
    const rows = await page.$$('.ant-table-row');
    console.log(`  榜单行数: ${rows.length}`);
    if (rows.length === 0) throw new Error('榜单为空');
  });

  // 3. 切换 TOP50
  await test('切换 TOP50', async () => {
    await page.click('.ant-segmented-item:nth-child(3)'); // Top 50
    await page.waitForTimeout(2500);
    const rows = await page.$$('.ant-table-row');
    console.log(`  TOP50 行数: ${rows.length}`);
    if (rows.length < 50) throw new Error(`只有 ${rows.length} 条，期望 >= 50`);
  });

  // 4. 搜索筛选
  await test('搜索筛选', async () => {
    const input = page.locator('input[placeholder*="筛选"]');
    await input.fill('920');
    await page.waitForTimeout(500);
    const rows = await page.$$('.ant-table-row');
    console.log(`  筛选结果: ${rows.length}`);
  });

  // 5. 导航到行情概念
  await test('行情概念加载', async () => {
    await page.click('text=行情/概念');
    await page.waitForTimeout(1500);
    const t = await page.textContent('.ant-card');
    if (!t.includes('股票搜索')) throw new Error('行情页未加载');
  });

  // 6. 概念列表
  await test('概念列表加载', async () => {
    await page.click('text=概念板块');
    await page.waitForTimeout(2000);
    const tags = await page.$$('.ant-tag-purple');
    console.log(`  概念标签数: ${tags.length}`);
  });

  // 7. 点击概念
  if ((await page.$$('.ant-tag-purple')).length > 0) {
    await test('点击概念标签', async () => {
      await page.click('.ant-tag-purple');
      await page.waitForTimeout(1500);
      const modal = await page.textContent('.ant-modal');
      console.log(`  弹窗内容: ${modal?.slice(0, 80) || '(无)'}`);
      if (!modal || !modal.includes('成份股')) throw new Error('概念弹窗异常');
      // 关闭弹窗
      await page.click('.ant-modal-close');
      await page.waitForTimeout(500);
    });
  }

  // 8. 股票搜索
  await test('股票搜索', async () => {
    await page.click('#rc-tabs-0-tab-stocks');
    await page.waitForTimeout(500);
    const input = page.locator('input[placeholder*="代码或名称"]');
    await input.fill('600519');
    await page.waitForTimeout(2000);
    const items = await page.$$('.ant-list-item');
    console.log(`  搜索结果: ${items.length} 条`);
    if (items.length === 0) throw new Error('搜索 600519 无结果');
    const text = await items[0].textContent();
    console.log(`  第一条: ${text?.slice(0, 60)}`);
    if (!text?.includes('茅台')) throw new Error('未搜索到茅台');
  });

  // 9. 自选股页
  await test('自选股页面', async () => {
    await page.click('text=自选股');
    await page.waitForTimeout(1500);
    const t = await page.textContent('.ant-card');
    console.log(`  自选页: ${t?.slice(0, 80)}`);
  });

  // 10. 打开添加弹窗
  await test('添加自选股弹窗', async () => {
    await page.click('text=添加');
    await page.waitForTimeout(500);
    const modal = await page.textContent('.ant-modal');
    console.log(`  弹窗: ${modal?.slice(0, 100)}`);
    // 关闭弹窗
    await page.click('text=取消');
    await page.waitForTimeout(300);
  });

} finally {
  const ss = await page.screenshot({ path: 'e2e-screenshot.png' });
  console.log(`\n截图: e2e-screenshot.png, 错误数: ${errors.length}`);
  if (errors.length) { console.log('❌ 错误:'); errors.forEach(e => console.log(`  - ${e}`)); }
  else console.log('✅ 全部通过');
  await browser.close();
}
