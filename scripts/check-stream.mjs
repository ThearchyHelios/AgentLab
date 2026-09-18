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
const CASES = ['db', 'think', 'human', 'issue', 'failed', 'loop_approve', 'supervisor']

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
  check('展开后能看到 SQL 原文', after.includes('SELECT') && after.includes('OWODS'))
  check('展开后能看到结果表', after.includes('factory_code'))
  await page.close()
}

console.log('\n=== Markdown 渲染 ===')
{
  // 用库里导出的**真实输出**，不是编的样本：模型写什么才是要渲染的，
  // 按 CommonMark 规范挑测例只会测到用不上的角落
  const page = await browser.newPage({ viewport: { width: 1100, height: 900 } })
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  await page.goto(`${WEB}/preview.html?md=1`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(600)
  check('没有运行时报错', errors.length === 0, errors.join(' | '))

  const text = await page.locator('body').innerText()
  // 「1+1 等于 **2**」原样带着星号显示——这就是改之前的样子
  const stray = [...text.matchAll(/\*\*[^*\n]{1,40}\*\*|(?<!\w)`[^`\n]{1,40}`/g)].map((m) => m[0])
  check('没有残留的 markdown 标记', stray.length === 0, stray.slice(0, 4).join(' '))

  const el = await page.evaluate(() => ({
    strong: document.querySelectorAll('strong').length,
    code: document.querySelectorAll('code').length,
    li: document.querySelectorAll('li').length,
    table: document.querySelectorAll('table').length,
    quote: document.querySelectorAll('blockquote').length,
    // 模型很爱写 **`table_name`**。粗体在扫描顺序上先命中，强调内部不再
    // 解析的话，那对反引号会原样显示出来
    codeInStrong: document.querySelectorAll('strong code').length,
    // 模型输出不可信，链接必须挡住 javascript:
    badHref: [...document.querySelectorAll('a')]
      .filter((a) => !/^(https?:|mailto:)/i.test(a.getAttribute('href') || '')).length,
    // 只数 markdown 渲染出来的，不数整页——dev server 自己就注入 HMR 脚本
    rawHtml: [...document.querySelectorAll('main, .space-y-2')]
      .reduce((n, el) => n + el.querySelectorAll('script,iframe,object,embed,img').length, 0),
    goodHref: [...document.querySelectorAll('a')]
      .filter((a) => /^https?:/i.test(a.getAttribute('href') || '')).length,
    pwned: window.__pwned === 1,
  }))
  check('粗体渲染出来了', el.strong > 0, `${el.strong} 处`)
  check('行内代码渲染出来了', el.code > 0, `${el.code} 处`)
  check('列表渲染出来了', el.li > 0, `${el.li} 条`)
  check('表格渲染出来了', el.table > 0, `${el.table} 张`)
  check('粗体里的代码也解析', el.codeInStrong > 0, `${el.codeInStrong} 处`)
  // 样本里混了一段构造的恶意输入（真实输出不会自带攻击，但模型输出是不可信
  // 内容，渲染层要么天生免疫，要么就是个 XSS 口子）
  check('javascript: 链接没被渲染成可点的', el.badHref === 0, `${el.badHref} 个`)
  check('正常链接照常可点', el.goodHref > 0, `${el.goodHref} 个`)
  check('原始 HTML 没有变成真标签', el.rawHtml === 0, `${el.rawHtml} 个`)
  check('注入的脚本没有执行', el.pwned === false)
  check('原始 HTML 当文本显示出来了', text.includes('<script>'))
  await page.close()
}

console.log('\n=== 长报告的折叠 ===')
{
  // 折叠以前是按字符硬切的，切点落进表格中间：前面几行渲染成表格、最后半行
  // 留成原始的 `| 1 | ThearchyHelios | …`。用户看到的是一份被咬掉一口的报告。
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  await page.goto(`${WEB}/preview.html?long=1`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(300)

  const folded = await page.locator('body').innerText()
  check('折叠时说破了"后面还有"', folded.includes('后面还有'))
  check('给得出展开入口', folded.includes('展开全部'))
  // 关键一条：折叠后不能留下没渲染成表格的原始 markdown 行
  const rawRow = folded.split('\n').find((l) => /^\s*\|.*\|/.test(l))
  check('没有半截的原始表格行', !rawRow, rawRow ? `残留：${rawRow.slice(0, 48)}` : '')
  check('开头的正文还在', folded.includes('总结'))

  await page.locator('button', { hasText: '展开全部' }).first().click()
  await page.waitForTimeout(250)
  const full = await page.locator('body').innerText()
  check('展开后拿得到结尾', full.includes('身兼管理员与商家双重身份'))
  check('展开后表格渲染完整', full.includes('user_39'))
  await page.close()
}

console.log('\n=== 没查库的那一轮 ===')
{
  // 「涉及数据必须真查」是这条路径上最硬的约定，放开直接回答之后，用户得能
  // 一眼分清哪些结论背后真的动了库
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  await page.goto(`${WEB}/preview.html?noquery=1`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(300)
  const body = await page.locator('body').innerText()
  check('说破了这一条没有查库', body.includes('没有查库'))
  check('声明排在结论前面',
        body.indexOf('没有查库') < body.indexOf('role.level'),
        `声明@${body.indexOf('没有查库')} 结论@${body.indexOf('role.level')}`)
  check('答案本身照常渲染', body.includes('口径'))
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
