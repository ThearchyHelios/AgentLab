// 三个页面的端到端检查：真浏览器、真后端、真数据。
//
// check-decode 守翻译，check-stream 守渲染，这一层守的是"接起来还对不对"——
// 前两层都能通过而这一层坏掉：store 没把事件存进去、tab 切换把状态卸载了、
// 唯一的运行入口被改版顺手删了。
//
// 跑之前前后端都得起着：./scripts/dev.sh
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH
  ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const SHOTS = process.env.SHOT_DIR ?? ''

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}

const browser = await chromium.launch({ executablePath: CHROME })
const ctx = await browser.newContext({ viewport: { width: 1500, height: 940 } })

/** 打开一页并收集真实报错（favicon 的 404 不算） */
async function visit(path) {
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))
  page.on('console', (m) => {
    if (m.type() === 'error' && !m.text().includes('404')) errors.push(m.text())
  })
  await page.goto(`${WEB}${path}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(700)
  return { page, errors }
}
const shot = async (page, name) => {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png` })
}

console.log('=== 画布助手栏 ===')
{
  const { page, errors } = await visit('/studio')
  check('没有运行时报错', errors.length === 0, errors.join(' | '))

  const tabs = await page.locator('nav, [role=tablist], button').allInnerTexts()
  const flat = tabs.join(' ')
  check('右栏是「助手 / 属性」两个 tab', flat.includes('助手') && flat.includes('属性'))
  check('不再有「时间线 / 成果 / 事件」的第二层嵌套',
    !flat.includes('时间线') && !flat.includes('原始事件'))

  // 改版最容易顺手删掉的两个东西
  check('发起运行的入口还在', await page.getByText('试运行').count() > 0)
  check('Copilot 输入框就在栏里（不再是弹窗）',
    await page.locator('textarea[placeholder*="告诉它"], textarea[placeholder*="描述你要"]').count() > 0)

  // 切到属性再切回来，输入框里的草稿不该没
  const composer = page.locator('textarea[placeholder*="告诉它"], textarea[placeholder*="描述你要"]').first()
  await composer.fill('测试草稿不要丢')
  await page.getByRole('button', { name: '属性' }).click()
  await page.waitForTimeout(200)
  await page.getByRole('button', { name: '助手' }).click()
  await page.waitForTimeout(200)
  check('切走再切回来，输入的字还在', await composer.inputValue() === '测试草稿不要丢',
    await composer.inputValue())
  await composer.fill('')

  const overflow = await page.evaluate(() =>
    document.documentElement.scrollWidth - document.documentElement.clientWidth)
  check('页面不横向溢出', overflow <= 0, `${overflow}px`)
  await shot(page, 'studio')
  await page.close()
}

console.log('\n=== 运行历史 ===')
{
  const { page, errors } = await visit('/runs')
  check('没有运行时报错', errors.length === 0, errors.join(' | '))

  const rows = page.locator('button').filter({ hasText: /\d{4}\/\d{1,2}\/\d{1,2}/ })
  const n = await rows.count()
  check('列出了运行记录', n > 0, `${n} 条`)

  if (n > 0) {
    // 列表原本每行只有"临时图 · 1.2s · 340 tok"，十条长得一模一样
    const texts = await rows.allInnerTexts()
    const withSummary = texts.filter((t) => t.trim().split('\n').length >= 3).length
    check('行里有内容摘要而不只是耗时和 token', withSummary > 0,
      `${withSummary}/${texts.length} 行带摘要`)

    await rows.first().click()
    await page.waitForTimeout(900)
    const body = await page.locator('body').innerText()
    check('详情走的是可读视图，不是事件表',
      !/\bnode\.(started|finished)\b/.test(body) && !/\brun\.started\b/.test(body))
    check('详情有内容', body.length > 300, `${body.length} 字`)

    // 原始事件不是常驻 tab，但必须还能看到
    await page.locator('button[title*="原始事件"]').first().click()
    await page.waitForTimeout(400)
    const rawBody = await page.locator('body').innerText()
    check('切得到原始事件', /node\.(started|finished)|run\.started/.test(rawBody))
    await page.locator('button[title*="可读视图"]').first().click()
    await page.waitForTimeout(300)
    check('切得回来', !/\bnode\.started\b/.test(await page.locator('body').innerText()))
  }
  await shot(page, 'runs')
  await page.close()
}

console.log('\n=== 问数据 ===')
{
  const { page, errors } = await visit('/chat')
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
  const body = await page.locator('body').innerText()
  check('有空态或历史，不是白屏', body.length > 60, `${body.length} 字`)
  check('有输入框', await page.locator('textarea').count() > 0)
  const overflow = await page.evaluate(() =>
    document.documentElement.scrollWidth - document.documentElement.clientWidth)
  check('页面不横向溢出', overflow <= 0, `${overflow}px`)
  await shot(page, 'chat')
  await page.close()
}

console.log('\n=== 三处讲的是同一个故事 ===')
{
  // 同一次运行，运行页详情和 preview 里的 dense 渲染应该给出同一批步骤。
  // 这是"全站一个翻译层"的实际含义——分两套的话用户没法判断哪个是真的。
  const a = await ctx.newPage()
  await a.goto(`${WEB}/preview.html?case=loop_approve`, { waitUntil: 'networkidle' })
  await a.waitForTimeout(400)
  const wide = await a.locator('body').innerText()
  const asks = (wide.match(/这条公告可以发吗/g) ?? []).length
  // 宽栏和窄栏各渲染一遍，所以是 3×2
  check('宽窄两栏各三轮审批，一轮不少', asks === 6, `${asks} 处`)
  check('决定折进了同一行而不是另起一行',
    !/^你放行了$/m.test(wide) && !/^你驳回了$/m.test(wide))
  await a.close()
}

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 界面端到端全部通过')
process.exit(failed ? 1 : 0)
