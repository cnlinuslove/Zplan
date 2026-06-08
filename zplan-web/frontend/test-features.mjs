// 精确测试每个功能
import { chromium } from 'playwright';
const BASE = 'http://127.0.0.1:8000';
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
page.on('console', msg => { if (msg.type() === 'error') errors.push(msg.text()); });
page.on('pageerror', err => errors.push(err.message));

try {
  // 1. 首页加载
  await page.goto(BASE, { waitUntil: 'networkidle', timeout: 15000 });
  await page.waitForTimeout(1500);
  console.log('1. 首页:', (await page.textContent('#root')).slice(0, 60));

  // 2. 进入选股榜单
  await page.click('text=选股榜单');
  await page.waitForTimeout(2000);
  const rows = await page.$$('.ant-table-row');
  console.log(`2. 选股榜单: ${rows.length} 行`);

  // 3. 点击第一行 → PickDetailPage
  if (rows.length > 0) {
    await rows[0].click();
    await page.waitForTimeout(3000);
    const url = page.url();
    console.log(`3. 选股详情 URL: ${url}`);
    const content = await page.textContent('#root');
    console.log(`   内容前200字: ${(content || '').slice(0, 200)}`);

    // Check for chart image
    const imgs = await page.$$('img');
    console.log(`   图片数量: ${imgs.length}`);
    for (const img of imgs) {
      const src = await img.getAttribute('src');
      console.log(`   img src: ${src?.slice(0, 80)}`);
    }

    // Check for report
    const hasReport = content.includes('选股分析') || content.includes('深度研报') || content.includes('技术信号');
    console.log(`   有分析报告: ${hasReport}`);
  }

  // 4. 导航回首页，搜索股票
  await page.goto(BASE, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1000);
  await page.click('text=行情/概念');
  await page.waitForTimeout(1500);

  // 搜索 603020
  const searchInput = page.locator('input[placeholder*="代码或名称"]');
  await searchInput.fill('603020');
  await page.waitForTimeout(2000);
  const results = await page.$$('.ant-list-item');
  console.log(`4. 搜索 603020: ${results.length} 结果`);

  if (results.length > 0) {
    await results[0].click();
    await page.waitForTimeout(3000);
    const url = page.url();
    console.log(`5. 个股详情 URL: ${url}`);
    const content = await page.textContent('#root');
    console.log(`   内容前200字: ${(content || '').slice(0, 200)}`);

    const imgs = await page.$$('img');
    console.log(`   图片数量: ${imgs.length}`);
    for (const img of imgs) {
      const src = await img.getAttribute('src');
      console.log(`   img src: ${src?.slice(0, 80)}`);
    }
    const hasChart = content.includes('技术趋势');
    const hasReport = content.includes('选股分析') || content.includes('深度研报');
    console.log(`   有趋势图: ${hasChart}, 有分析: ${hasReport}`);
  }

} finally {
  console.log(`\n错误: ${errors.length}`);
  errors.forEach(e => console.log(' -', e.slice(0, 150)));
  await page.screenshot({ path: 'feature-test.png' });
  console.log('截图: feature-test.png');
  await browser.close();
}
