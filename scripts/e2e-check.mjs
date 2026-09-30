// 冒烟测试：真开一个浏览器，跑一遍「打开画布 → 选模板 → 运行 → 看高亮」。
// 依赖 frontend 里的 playwright-core，并复用系统已装的 Chrome，不额外下载浏览器。
//
// 和 check-* 不同，这一个是真的发起运行（模型换成演示模型，不花钱），会在库里留下一条
// 运行记录；只打印、截图，不断言。所以它不在 check-all 里，要人看着跑。
// 默认连 5273；对别的实例跑时带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/e2e-check.mjs
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'
const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  args: ['--no-sandbox'] })
const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } })
const errs = []
page.on('pageerror', e => errs.push(e.message))
page.on('console', m => m.type() === 'error' && errs.push(m.text()))

await page.goto(`${WEB}/studio`, { waitUntil: 'networkidle' })
await page.waitForSelector('.react-flow__node', { timeout: 15000 })

// 切到「最小问答」模板，用演示模型运行，验证画布实时高亮
await page.getByRole('button', { name: /批量处理|最小问答|工作流/ }).first().click()
await page.waitForTimeout(600)
const row = page.locator('text=① 最小问答').first()
if (await row.count()) { await row.click(); await page.waitForTimeout(900) }

console.log('当前图节点数:', await page.locator('.react-flow__node').count())

// 选中 llm 节点，把模型改成演示模型，避免这次验证花真钱
await page.locator('.react-flow__node').nth(1).click()
await page.waitForTimeout(500)
// 演示模型的 label 取自后端 providers/catalog.py 的 MOCK_MODELS（seed 时写进接入的 models）。
// 2026-09 前建的库里，这个接入存的还是旧 label「Mock（不花钱，用来试编排）」，对那种库跑时这一下选不中
const modelSel = page.locator('select').filter({ hasText: /默认|演示模型|Claude/ }).first()
if (await modelSel.count()) { await modelSel.selectOption({ label: '演示模型（无需 API Key）' }).catch(()=>{}) }
await page.screenshot({ path: '/tmp/shot-inspector.png' })

// 回到运行面板，填输入并运行
await page.getByRole('button', { name: '运行', exact: true }).first().click()
await page.waitForTimeout(300)
const ta = page.locator('aside textarea').first()
await ta.fill('可视化 agent 编排解决了什么问题？')
await page.getByRole('button', { name: /^运行$/ }).last().click()

// 等到有节点进入运行态，抓一张"正在跑"的画面
await page.waitForSelector('.node-running, .node-done', { timeout: 20000 }).catch(()=>{})
await page.waitForTimeout(1200)
await page.screenshot({ path: '/tmp/shot-running.png' })
console.log('运行中高亮节点:', await page.locator('.node-running').count(), '已完成:', await page.locator('.node-done').count())

await page.waitForTimeout(6000)
await page.screenshot({ path: '/tmp/shot-done.png' })
console.log('完成后 done 节点:', await page.locator('.node-done').count())
const timeline = await page.locator('aside').last().innerText()
console.log('--- 右侧面板文本 ---'); console.log(timeline.slice(0, 700))
console.log('--- 错误 ---'); console.log(errs.slice(0,6).join('\n') || '(无)')
await browser.close()
