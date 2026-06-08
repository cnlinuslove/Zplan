// 完整截图测试 — 验证所有功能正常
import { chromium } from 'playwright';
const BASE = 'http://127.0.0.1:8000';
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

try {
  // 1. 选股榜单 → 第一个股票详情（含图表+分析）
  await page.goto(BASE + '/picks/1190', { waitUntil: 'networkidle', timeout: 20000 });
  await page.waitForTimeout(3000); // 等图表生成
  await page.screenshot({ path: 's1-pick-detail.png', fullPage: false });
  const c1 = await page.textContent('#root');
  const hasChart = c1.includes('技术趋势');
  const hasReport = c1.includes('选股分析') || c1.includes('价格预测');
  console.log(`1. 选股详情: 图表=${hasChart} 分析=${hasReport} (截图 s1-pick-detail.png)`);

  // 2. 个股详情页（搜索 603020）
  await page.goto(BASE + '/market/603020', { waitUntil: 'networkidle', timeout: 20000 });
  await page.waitForTimeout(3000);
  await page.screenshot({ path: 's2-stock-detail.png', fullPage: false });
  const c2 = await page.textContent('#root');
  const hasChart2 = c2.includes('技术趋势');
  console.log(`2. 个股详情: 图表=${hasChart2} (截图 s2-stock-detail.png)`);

  // 3. 选股榜单
  await page.goto(BASE + '/picks', { waitUntil: 'networkidle', timeout: 15000 });
  await page.waitForTimeout(2000);
  await page.screenshot({ path: 's3-picks-list.png', fullPage: false });
  const rows = await page.$$('.ant-table-row');
  console.log(`3. 选股榜单: ${rows.length} 行 (截图 s3-picks-list.png)`);

  // 4. 行情/概念
  await page.goto(BASE + '/market', { waitUntil: 'networkidle', timeout: 15000 });
  await page.waitForTimeout(2000);
  await page.screenshot({ path: 's4-market.png', fullPage: false });
  console.log('4. 行情页 (截图 s4-market.png)');

  console.log('\n✅ 全部截图已保存到 frontend/ 目录');
} finally {
  await browser.close();
}
