// AssistantStream 的渲染回归检查。
//
// 解码器的检查（check-decode.mjs）守的是"翻译对不对"，这里守的是"画出来
// 对不对"——两者能各自通过而合起来是坏的：Step 里字段都对，组件却把它渲染成
// 一屏转义 JSON，或者在 360px 窄栏里把页面撑得横向滚动。
//
// 用真浏览器跑真组件，数据还是 fixtures.json 里那批真实事件。
// 跑之前前端得起着：./scripts/dev.sh
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
// 不用 playwright install：它既下不动也会动到已有缓存。系统 Chrome 就够了。
const CHROME = process.env.CHROME_PATH
  ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const CASES = ['db', 'think', 'human', 'issue', 'failed', 'loop_approve']

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}

const browser = await chromium.launch({ executablePath: CHROME })

for (const kind of CASES) {
  console.log(`\n=== ${kind} ===`)
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
  await page.goto(`${WEB}/preview.html?case=${kind}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(300)

  const text = await page.locator('body').innerText()
  // 404 是 favicon，和组件无关
  const real = errors.filter((e) => !e.includes('404'))
  check('没有运行时报错', real.length === 0, real.join(' | '))
  check('有内容渲染出来', text.length > 40, `${text.length} 字`)
  check('没有把转义 JSON 直接吐出来', !text.includes('\\"columns\\"'))
  check('没有 0ms 噪音', !/\b0ms\b/.test(text))
  check('没有裸事件名', !/\b(node|run|llm|tool)\.(started|finished|start|end)\b/.test(text))

  // 360px 窄栏是画布右栏的真实宽度。这里溢出，右栏就会横向滚动
  const overflow = await page.evaluate(() => {
    const el = document.documentElement
    return el.scrollWidth - el.clientWidth
  })
  check('页面不横向溢出', overflow <= 0, `${overflow}px`)

  await page.close()
}

console.log('\n=== 展开交互 ===')
{
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  await page.goto(`${WEB}/preview.html?case=db`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(300)
  const before = await page.locator('body').innerText()
  check('收起时不显示 SQL', !before.includes('SELECT'))
  await page.locator('button', { hasText: '在 bi 上查询数据' }).first().click()
  await page.waitForTimeout(250)
  const after = await page.locator('body').innerText()
  check('展开后能看到 SQL 原文', after.includes('SELECT') && after.includes('ANALYTICS'))
  check('展开后能看到结果表', after.includes('factory_code'))
  await page.close()
}

console.log('\n=== 空态 ===')
{
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  await page.goto(`${WEB}/preview.html?case=__none__`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(250)
  const text = await page.locator('body').innerText()
  check('没有内容时给的是空态而不是白屏', text.includes('问点什么'))
  await page.close()
}

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 助手流渲染全部通过')
process.exit(failed ? 1 : 0)
