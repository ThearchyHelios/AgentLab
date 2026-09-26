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
  check('没有 0 ms 噪音', !/(^|[^\d.])0 ms/.test(text))
  check('没有旧的耗时写法（1m14s / 2min / 12345ms）', !/\d(ms|min)\b|\dm\d+s/.test(text))
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
  // 查询标题从 SQL 派生：以前五条查询都叫「在 warehouse 上查询数据」
  await page.locator('button', { hasText: '查询 v_device_kpi' }).first().click()
  await page.waitForTimeout(250)
  const after = await page.locator('body').innerText()
  check('展开后能看到 SQL 原文', after.includes('SELECT') && after.includes('ANALYTICS'))
  check('展开后能看到结果表', after.includes('factory_code'))
  check('展开区写明数据源', after.includes('数据源 warehouse'))
  check('SQL 可以一键复制', await page.locator('button[aria-label="复制 SQL"]').count() > 0)
  await page.close()
}

console.log('\n=== Markdown 渲染 ===')
{
  // 样本取自库里导出的**真实输出**，不是按 CommonMark 规范挑的测例：模型
  // 实际会写成什么样，才是要渲染的东西。涉及业务数据的那几条已换成同构的
  // 合成内容——换的是领域，markdown 构造（标题层级、表格、引用块、inline
  // code 密度）逐项对齐，覆盖面没有变窄
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

console.log('\n=== 复核说明 ===')
{
  // 「跑完了」和「答得对」是两回事。复核说明必须排在成果**上方**——排在下面
  // 等于让人读完整个结论、信了，才知道它是在什么条件下得出的
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  await page.goto(`${WEB}/preview.html?review=1`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(300)
  const body = await page.locator('body').innerText()

  check('说破了取数没跑通', body.includes('取数没跑通'))
  check('说明排在结论前面',
        body.indexOf('取数没跑通') < body.indexOf('大约 5 个'),
        `说明@${body.indexOf('取数没跑通')} 结论@${body.indexOf('大约 5 个')}`)
  check('答案本身照常渲染', body.includes('大约 5 个'))
  check('异常清单默认收起', !body.includes('no such table'))
  check('数得出有几处异常', body.includes('检测到 2 处异常'))

  // broken 用报警色，degraded 用普通边框。混成一个样子，用户就学会了一概忽略
  const colors = await page.evaluate(() => {
    const err = getComputedStyle(document.documentElement).getPropertyValue('--err').trim()
    const hit = [...document.querySelectorAll('div[style*="border-color"]')]
      .map((el) => getComputedStyle(el).borderTopColor)
    return { err, hit }
  })
  check('两档复核长得不一样', new Set(colors.hit).size > 1, colors.hit.join(' / '))

  // 改写是有损的，原件得留着能对照
  check('改写前的原文可以展开', body.includes('看改写前的原文'))
  check('原文默认不占地方', !body.includes('平台管理员可以修改他人权限。\n'))

  const overflow = await page.evaluate(() =>
    document.documentElement.scrollWidth - document.documentElement.clientWidth)
  check('页面不横向溢出', overflow <= 0, `${overflow}px`)
  await page.close()
}

console.log('\n=== 并行分支 ===')
{
  // fan-out 一直是真并发（实测 3 个 sleep 2 秒的节点墙钟 2.6 秒），但时间线上
  // 原来只是穿插出现的几行，省下的时间一个字都没说
  for (const [w, dense, label] of [[1000, '0', '宽栏'], [380, '1', '窄栏']]) {
    const page = await browser.newPage({ viewport: { width: w, height: 700 } })
    const errors = []
    page.on('pageerror', (e) => errors.push(e.message))
    await page.goto(`${WEB}/preview.html?fanout=1&dense=${dense}`, { waitUntil: 'networkidle' })
    await page.waitForTimeout(400)
    const body = await page.locator('body').innerText()
    check(`${label}：没有运行时报错`, errors.length === 0, errors.join(' | '))
    check(`${label}：说破了这是三路并行`, body.includes('3 路并行'))
    check(`${label}：省下多少写出来了`, /合计 6\.\d s，实际 3\.\d s/.test(body),
          body.slice(body.indexOf('3 路并行'), body.indexOf('3 路并行') + 40).replace(/\n/g, ' '))
    check(`${label}：三路都还看得见`,
          ['查销售库', '查库存库', '查客户库'].every((n) => body.includes(n)))
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth)
    check(`${label}：不横向溢出`, overflow <= 0, `${overflow}px`)
    await page.close()
  }
}

console.log('\n=== 协作团队的泳道 ===')
{
  // 这个节点以前在界面上是一条扁平的步骤序列：看得出"谁回了什么"，
  // 看不出"谁和谁是同时干的"——而一轮同时派几个人正是它相对单 agent 的
  // 全部优势，不画出来等于没有
  for (const [w, dense, label] of [[1000, '0', '宽栏'], [380, '1', '窄栏']]) {
    const page = await browser.newPage({ viewport: { width: w, height: 700 } })
    const errors = []
    page.on('pageerror', (e) => errors.push(e.message))
    await page.goto(`${WEB}/preview.html?team=1&dense=${dense}`, { waitUntil: 'networkidle' })
    await page.waitForTimeout(400)
    const body = await page.locator('body').innerText()

    check(`${label}：没有运行时报错`, errors.length === 0, errors.join(' | '))
    check(`${label}：三个人各占一行`,
          ['researcher', 'analyst', 'writer'].every((n) => body.includes(n)))
    // 说法和画布上的协作矩阵同一套：「并行省下 X」
    check(`${label}：说清楚并行省了多少`, /并行省下 11\.8 s（2 轮并行）/.test(body),
          body.slice(0, 90).replace(/\n/g, ' '))
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth)
    check(`${label}：不横向溢出`, overflow <= 0, `${overflow}px`)

    if (dense === '0') {
      // 条形宽度按耗时归一化，一眼看得出谁是这一轮的瓶颈。
      // 都画一样长的话，"并行"就只剩一句口号
      const widths = await page.evaluate(() =>
        [...document.querySelectorAll('button[title*="第 1 轮"]')]
          .map((b) => b.getBoundingClientRect().width))
      check('同一轮里慢的那个条更长', widths.length === 2 && widths[0] > widths[1] * 1.2,
            widths.map((x) => Math.round(x)).join(' vs '))

      await page.locator('button[title*="第 1 轮"]').first().click()
      await page.waitForTimeout(300)
      const opened = await page.locator('body').innerText()
      check('点开看得到这个人的任务和回复',
            opened.includes('查 Milvus 与 Qdrant') && opened.includes('内置 jieba'))
      // 空格子要有底轨：以前写的 var(--hover) 没定义，底色是透明的，泳道的网格断了
      const empty = await page.evaluate(() => {
        const cell = [...document.querySelectorAll('button[title*="第 3 轮"]')][0]?.closest('.flex-1')
          ?.parentElement?.parentElement?.previousElementSibling?.querySelector('.flex-1 > .flex-1:last-child')
        const bg = cell ? getComputedStyle(cell).backgroundColor : ''
        return bg
      })
      check('没派活的格子也有底轨', !!empty && empty !== 'rgba(0, 0, 0, 0)', empty)
      // 完成条和画布上协作矩阵同一个颜色（--st-done）
      const bar = await page.evaluate(() => {
        const b = document.querySelector('button[title*="第 1 轮"]')
        const probe = document.createElement('span')
        probe.style.color = 'var(--st-done)'
        document.body.append(probe)
        const want = getComputedStyle(probe).color
        probe.remove()
        return { got: b ? getComputedStyle(b).backgroundColor : '', want }
      })
      check('完成条是完成色，和协作矩阵一致', bar.got === bar.want, `${bar.got} vs ${bar.want}`)
    }
    await page.close()
  }
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

// ---------------------------------------------------------------- 第二波
const SHOTS = process.env.STREAM_SHOTS
/** 打开一个预览场景。shots 目录给了就亮暗各截一张，供人眼复核 */
async function open(query, { w = 1100, h = 800, reduced = false, name } = {}) {
  const page = await browser.newPage({ viewport: { width: w, height: h },
    ...(reduced ? { reducedMotion: 'reduce' } : {}) })
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  await page.goto(`${WEB}/preview.html?${query}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(400)
  if (SHOTS && name) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(150)
      await page.screenshot({ path: `${SHOTS}/stream-${name}-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
  return { page, errors }
}
const scrollState = (page) => page.evaluate(() => {
  const el = document.querySelector('[data-stream-scroll]')
  return { top: Math.round(el.scrollTop), max: Math.round(el.scrollHeight - el.clientHeight) }
})

console.log('\n=== 跟随：只在贴底时跟 ===')
{
  // 以前每来一个节点就 scrollIntoView 到底：往上翻着看某一步时被一次次拽回去
  const { page, errors } = await open('grow=1&dense=1', { w: 380, h: 560, name: 'follow' })
  const first = await scrollState(page)
  check('打开时停在最新处', first.max > 0 && first.max - first.top < 64, `${first.top}/${first.max}`)
  await page.evaluate(() => {
    const el = document.querySelector('[data-stream-scroll]')
    el.scrollTop = 0
    el.dispatchEvent(new Event('scroll'))
  })
  await page.waitForTimeout(120)
  await page.evaluate(() => window.__grow(60))
  await page.waitForTimeout(400)
  const away = await scrollState(page)
  check('往上翻着看时，新步骤到来不拽人', away.top < 40, `scrollTop=${away.top}`)
  const pill = page.locator('[data-jump-latest]')
  check('底下浮出「跳到最新」', await pill.count() === 1 && /\d+ 条新进展/.test(await pill.innerText()),
    await pill.innerText().catch(() => ''))
  await pill.click()
  await page.waitForTimeout(700)
  const back = await scrollState(page)
  check('点了回到最新处', back.max - back.top < 64, `${back.top}/${back.max}`)
  check('回到底部后胶囊消失', await pill.count() === 0)
  await page.evaluate(() => window.__grow(40))
  await page.waitForTimeout(400)
  const again = await scrollState(page)
  check('回到底部后恢复跟随', again.max - again.top < 64, `${again.top}/${again.max}`)
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
  await page.close()
}

console.log('\n=== 回看历史运行：停在失败处 ===')
{
  const { page } = await open('syn=mixed', { w: 1100, h: 560, name: 'history-failed' })
  const s1 = await scrollState(page)
  const where = await page.evaluate(() => {
    const box = document.querySelector('[data-stream-scroll]').getBoundingClientRect()
    const row = document.querySelector('[data-step-status="failed"]')
    const r = row?.getBoundingClientRect()
    const probe = document.createElement('span')
    probe.style.color = 'var(--st-failed)'
    document.body.append(probe)
    const want = getComputedStyle(probe).color
    probe.remove()
    // 行上有 transition-colors：刚切过主题（截图时）读到的是过渡中的颜色，量终值
    if (row) row.style.transition = 'none'
    const outline = row ? getComputedStyle(row).outlineColor : ''
    if (row) row.style.transition = ''
    return { inView: !!r && r.top >= box.top && r.bottom <= box.bottom, flash: row?.dataset.flash, outline, want }
  })
  // 以前历史运行打开就滚到最底，摘要和失败节点都在视口外
  check('第一处失败在视口里', where.inView, JSON.stringify(s1))
  check('不是滚到了最底', s1.top < s1.max, `${s1.top}/${s1.max}`)
  check('定位到的那一行描了一下边，用失败色', where.flash === 'failed' && where.outline === where.want,
    `${where.flash} ${where.outline}`)
  const body = await page.locator('body').innerText()
  check('报错写全，不截成一行', body.includes('推送失败：企业微信机器人地址没配'))
  check('技术细节默认收着', !body.includes('ConnectError') && body.includes('技术细节'))
  check('动作槽渲染出页面给的按钮', body.includes('重试这一轮'))
  check('跳过的节点在时间线上留痕', body.includes('跳过「背景检索」') && body.includes('skip_if 成立'))
  check('#runId 可以点去运行记录', await page.locator('a[href="/runs/syn-mixed"]').count() === 1)
  // 「开始执行」「完成」和头部说的是同一件事，不再单列；「继续执行」是分段线，
  // 失败的运行里它以前是一个红叉，读起来像续跑本身出了错
  check('和头部重复的生命周期行不再单列', !body.includes('开始执行（6 个节点）'))
  const mark = await page.locator('[data-phase-mark]').allInnerTexts()
  check('续跑是一条分段线，说清谁发起的', mark.some((t) => t.includes('继续执行') && t.includes('张工')), mark.join(' | '))
  check('分段线不画成失败', await page.locator('[data-phase-mark] [data-status="failed"]').count() === 0)
  const live = await page.locator('[role="status"][aria-live="polite"]').allInnerTexts()
  check('状态变化有读屏播报区', live.some((t) => t.includes('失败')), live.join(' | '))
  await page.close()
}

console.log('\n=== 运行中的时间感 ===')
{
  const { page } = await open('syn=live', { w: 1100, h: 760, name: 'live' })
  const clock = () => page.evaluate(() => [...document.querySelectorAll('[aria-busy="true"] .mono')]
    .map((e) => e.textContent).find((t) => /\d\d:\d\d\.\d/.test(t ?? '')))
  const a = await clock()
  await page.waitForTimeout(700)
  const b = await clock()
  check('进行中的步骤有实时秒表', !!a && /\d\d:\d\d\.\d/.test(a), a)
  check('秒表在走', !!a && !!b && a !== b, `${a} → ${b}`)
  const head = await page.locator('[data-turn]').first().innerText()
  check('头部说到第几个节点了', /\d+\/6 节点/.test(head), head.split('\n').slice(0, 3).join(' '))
  check('头部说此刻在做什么', head.includes('正在：'))
  // 节点轨：一格一个节点，按状态着色；还没轮到的是空格，不猜
  const rail = await page.evaluate(() => [...document.querySelectorAll('[data-node-rail] > span')].map((e) => e.title))
  check('节点轨一格一个节点', rail.length === 6, rail.join(' | '))
  check('节点轨标出跳过的、正在跑的、还没轮到的',
    rail.some((t) => t.includes('已跳过')) && rail.some((t) => t.includes('运行中')) && rail.includes('还没轮到'),
    rail.join(' | '))
  // 还没轮到的格子以前填 --bg-hover，和栏底只差 1.1:1：5 格的轨看着像 4 格。亮暗两套都量
  for (const theme of ['dark', 'light']) {
    await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
    await page.waitForTimeout(100)
    const pend = await page.evaluate(() => {
      const lum = (c) => {
        const [r, g, b] = (c.match(/\d+(\.\d+)?/g) ?? []).slice(0, 3).map(Number).map((v) => {
          const x = v / 255
          return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4
        })
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
      }
      const cell = [...document.querySelectorAll('[data-node-rail] > span')].find((e) => e.title === '还没轮到')
      if (!cell) return null
      const cs = getComputedStyle(cell)
      const mark = cs.backgroundColor !== 'rgba(0, 0, 0, 0)' ? cs.backgroundColor : (cs.boxShadow.match(/rgba?\([^)]+\)/) ?? [''])[0]
      let el = cell.parentElement
      while (el && getComputedStyle(el).backgroundColor === 'rgba(0, 0, 0, 0)') el = el.parentElement
      const bg = el ? getComputedStyle(el).backgroundColor : 'rgb(0,0,0)'
      const [a, b] = [lum(mark), lum(bg)].sort((x, y) => y - x)
      return { mark, bg, ratio: +((a + 0.05) / (b + 0.05)).toFixed(2) }
    })
    check(`${theme}：还没轮到的格子看得见（≥ 3:1）`, !!pend && pend.ratio >= 3, JSON.stringify(pend))
  }
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  check('进行中的行标了 aria-busy', await page.locator('[aria-busy="true"]').count() > 0)
  check('正常态有扫光', await page.locator('.shimmer').count() > 0)
  await page.close()

  // 减少动效：扫光和转圈停住后，"在跑"换成一道竖线加「进行中」
  const r = await open('syn=live', { w: 1100, h: 760, reduced: true, name: 'live-reduced' })
  const busy = await r.page.evaluate(() => {
    const row = document.querySelector('[aria-busy="true"]')
    return { text: row?.innerText ?? '', border: row ? getComputedStyle(row).borderLeftWidth : '' }
  })
  check('减少动效时不画扫光', await r.page.locator('.shimmer').count() === 0)
  check('减少动效时写「进行中」', busy.text.includes('进行中'), busy.text.slice(0, 40))
  check('减少动效时有左侧竖线', busy.border === '2px', busy.border)
  await r.page.close()
}

console.log('\n=== 长运行：按轮折叠 ===')
{
  // 1100 多条事件、145 拍。以前 5970 个 DOM 节点、14390px 高
  const { page, errors } = await open('syn=long', { w: 1100, h: 800, name: 'long' })
  const dom = await page.evaluate(() => document.querySelector('[data-stream-scroll]').querySelectorAll('*').length)
  check('DOM 节点少于 1000', dom < 1000, `${dom} 个`)
  const s0 = await scrollState(page)
  check('不再是几万像素的长卷', s0.max < 1500, `可滚 ${s0.max}px`)
  const body = await page.locator('body').innerText()
  check('收起的轮次说有几轮', body.includes('另外 144 轮'))
  check('说出最慢的是哪一次', /最慢 65 ms（第 37 次）/.test(body))
  check('有提醒的轮次数出来', body.includes('14 轮有提醒'))
  // 摘要里点名的轮次要够得着：以前「最慢（第 37 次）」「14 轮有提醒」只是字，
  // 展开全部又只画前 20 轮，第 37 次、第 30 次以后有提醒的几轮根本翻不到
  const box = page.locator('[data-exec-groups="poll"]')
  const execs = () => box.evaluate((b) => [...b.querySelectorAll(':scope > [data-exec]')].map((e) => +e.dataset.exec))
  await box.locator('button', { hasText: '最慢 65 ms（第 37 次）' }).click()
  await page.waitForTimeout(200)
  const r37 = await box.evaluate((b) => {
    const e = b.querySelector('[data-exec="37"]')
    const s = document.querySelector('[data-stream-scroll]').getBoundingClientRect()
    const r = e?.getBoundingClientRect()
    const probe = document.createElement('span')
    probe.style.color = 'var(--st-failed)'
    document.body.append(probe)
    const failed = getComputedStyle(probe).color
    probe.remove()
    return { inView: !!r && r.top >= s.top && r.bottom <= s.bottom, open: !!e?.querySelector('[data-step-status]'),
      flash: e?.dataset.flash, outline: e ? getComputedStyle(e).outlineColor : '', failed }
  })
  check('点「最慢」就列出第 37 次并滚到眼前', r37.inView && r37.open, JSON.stringify(await execs()))
  check('点名定位描的是强调色，不是失败色', r37.flash === 'focus' && r37.outline !== r37.failed, `${r37.flash} ${r37.outline}`)
  await box.locator('button', { hasText: '14 轮有提醒' }).click()
  await page.waitForTimeout(200)
  const warned = await execs()
  check('点「有提醒」列出那 14 轮', [10, 30, 70, 140].every((n) => warned.includes(n)), JSON.stringify(warned))
  await box.locator('button', { hasText: '另外' }).click()
  await page.waitForTimeout(300)
  const dom2 = await page.evaluate(() => document.querySelector('[data-stream-scroll]').querySelectorAll('*').length)
  check('展开全部也分批画', dom2 < 2500, `${dom2} 个`)
  const paged = await execs()
  check('展开全部后最后一轮、第 37 次、第 30 次都还在', [145, 37, 30].every((n) => paged.includes(n)), JSON.stringify(paged))
  const more = await box.locator('[data-exec-more]').innerText().catch(() => '')
  check('没列出的那一截说清还有多少、怎么继续', /还有 \d+ 轮没列出/.test(more) && more.includes('再列') && more.includes('全部列出'),
    more.replace(/\n/g, ' '))
  check('不再说「已在上面展开」的假话', !(await page.locator('body').innerText()).includes('已在上面展开'))
  await box.locator('[data-exec-more] button', { hasText: '全部列出' }).click()
  await page.waitForTimeout(300)
  const every = await execs()
  check('全部列出后 145 轮一轮不少、不重复', every.length === 145 && new Set(every).size === 145, `${every.length} 行`)
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
  await page.close()
}

console.log('\n=== 几轮审批：分轮但不收起 ===')
{
  // 驳回两次再放行：只执行了三次的节点按轮分组、全部摊开——收起来就把那两次驳回藏了
  const { page } = await open('case=loop_approve', { w: 1100, h: 900 })
  const body = await page.locator('body').innerText()
  check('三轮审批都看得见（宽窄两栏各三条）', (body.match(/这条公告可以发吗/g) ?? []).length === 6)
  check('轮次边界画出来了', body.includes('第 3 次'))
  check('老数据里光秃秃的「继续执行」不单列', !body.includes('继续执行'))
  await page.close()
}

console.log('\n=== 协作团队：运行中的说法 ===')
{
  const { page } = await open('syn=team-live&dense=1', { w: 380, h: 760, name: 'team-live' })
  const body = await page.locator('body').innerText()
  check('运行中写「N 人并行中」', body.includes('3 人并行中'))
  check('运行中不报「省下 0」', !/省下 0/.test(body))
  check('进行中的轮次不写 0 ms', body.includes('进行中') && !/第1轮 · 0/.test(body))
  check('调度这一行说人话', body.includes('第 1 轮：交给 采购员、质检员、物流员（并行）'))
  check('运行中的说法和协作矩阵一致（本轮 N 人并行中）', body.includes('本轮 3 人并行中'))
  check('没有原始事件名', !/agent\.(route|step)/.test(body))
  await page.close()
  const r = await open('syn=team-routing&dense=1', { w: 380, h: 760, name: 'team-routing' })
  check('调度者在想时有一行进行中', (await r.page.locator('body').innerText()).includes('调度者在想下一步'))
  await r.page.close()
}

console.log('\n=== 出具横幅、答案操作、证据下钻 ===')
{
  const { page } = await open('syn=issued', { w: 1100, h: 900, name: 'issued' })
  const body = await page.locator('body').innerText()
  // gaps 以前一条都不显示：横幅说「请对照下方声明」，下方什么都没有
  check('横幅列出校验没跑全的原因', body.includes('校验没跑全') && body.includes('叙述模板渲染为空'))
  check('无法回指的数字带上下文', body.includes('晚班比上周低 6 个百分点'))
  check('正文里画出了那个数字', await page.locator('[data-mark="warn"]').count() === 1
    && (await page.locator('[data-mark="warn"]').innerText()) === '6')
  check('说清数字回指到哪张口径卡', body.includes('回指上的 2 个数字都来自这张卡'))
  check('探索运行标注不进正式归档', body.includes('探索运行 · 不进正式归档'))
  check('头部和复核结论一致', body.includes('有缺口'))
  check('吸顶的头上挂着出具档位', (await page.locator('[data-turn] .sticky').first().innerText()).includes('降档出具'))
  await page.locator('.group\\/answer').first().hover()
  check('答案可以复制', await page.locator('button[aria-label="复制"]').count() > 0)
  // 复制 / 导出以前贴着整块的右上角浮出，正好压在横幅的「回指 N 个数字」上
  const clash = await page.evaluate(() => {
    const a = document.querySelector('.group\\/answer')
    const act = a?.querySelector('button[aria-label="复制"]')?.parentElement
    const banner = a?.firstElementChild
    if (!act || !banner || banner.contains(act)) return 'no-banner'
    const x = act.getBoundingClientRect()
    const y = banner.getBoundingClientRect()
    return x.top < y.bottom && x.bottom > y.top && x.left < y.right && x.right > y.left ? 'overlap' : 'clear'
  })
  check('答案操作不压在出具横幅上', clash === 'clear', clash)
  const [dl] = await Promise.all([page.waitForEvent('download', { timeout: 5000 }).catch(() => null),
    page.locator('button', { hasText: '导出' }).first().click()])
  const md = dl ? await (async () => {
    const chunks = []
    for await (const c of await dl.createReadStream()) chunks.push(c)
    return Buffer.concat(chunks).toString('utf8')
  })() : ''
  check('导出 Markdown：带着问题、答案和出具档位', md.includes('> 9 月 19 日各班次出勤率')
    && md.includes('94.23%') && md.includes('降档出具'), `${dl?.suggestedFilename() ?? '没有下载'} ${md.length} 字`)
  await page.close()

  // 证据下钻：工件取回时后端复验哈希，界面上要能打开。形状照真实的工具快照：
  // {tool, args: {sql}, result: "<JSON 字符串>"}——result 是一段字符串，不是现成的表
  for (const theme of ['dark', 'light']) {
    const p2 = await browser.newPage({ viewport: { width: 1100, height: 800 }, colorScheme: theme })
    await p2.route(/\/api\/artifacts\/.+/, (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ id: 'x', content: {
        tool: 'db_query__warehouse',
        args: { sql: 'SELECT shift_name, COUNT(*) AS n FROM v_device_kpi GROUP BY shift_name', limit: null },
        result: JSON.stringify({ columns: ['shift_name', 'n', 'rate'],
          rows: Array.from({ length: 30 }, (_, i) => [`班次${i}`, 40 + i, 0.9]), row_count: 30 }),
      } }),
    }))
    await p2.goto(`${WEB}/preview.html?syn=issued&theme=${theme}`, { waitUntil: 'networkidle' })
    await p2.waitForTimeout(300)
    await p2.locator('button', { hasText: '完整结果' }).first().click()
    await p2.waitForTimeout(500)
    const modal = await p2.locator('[role="dialog"]').innerText().catch(() => '')
    if (theme === 'dark') {
      check('打开完整结果，结果集画成表', modal.includes('班次29')
        && await p2.locator('[role="dialog"] table').count() === 1, modal.slice(0, 60))
      check('证据连着问的是哪条 SQL', modal.includes('FROM v_device_kpi'))
      check('说明哈希已校验', modal.includes('哈希已校验'))
      check('完整结果可以导出 CSV', modal.includes('CSV'))
    }
    if (SHOTS) await p2.screenshot({ path: `${SHOTS}/stream-artifact-${theme}.png` })
    await p2.close()
  }
}

console.log('\n=== 复核判了不可信 ===')
{
  const { page } = await open('review=1', { w: 1100, h: 800, name: 'review' })
  const body = await page.locator('body').innerText()
  // 以前 broken 时头部仍是普通字重的「完成」
  check('头部写「结论不可用」', body.includes('结论不可用'))
  // 运行确实跑完了：徽标照常是完成的样子和颜色，红色只落在结论那几个字上。
  // 以前把完成的勾染成红色——形状说成功、颜色说失败
  const head = await page.evaluate(() => {
    const color = (v) => {
      const probe = document.createElement('span')
      probe.style.color = v
      document.body.append(probe)
      const c = getComputedStyle(probe).color
      probe.remove()
      return c
    }
    const h = document.querySelector('[data-turn] .sticky')
    const badge = h?.querySelector('[data-status]')
    const verdict = h?.querySelector('[data-verdict]')
    return { status: badge?.getAttribute('data-status'), badge: badge ? getComputedStyle(badge).color : '',
      verdict: verdict ? getComputedStyle(verdict).color : '', done: color('var(--st-done)'), failed: color('var(--st-failed)') }
  })
  check('结论不可用时徽标不染红', head.badge !== head.failed && head.badge === head.done, JSON.stringify(head))
  check('红色落在结论上', head.verdict === head.failed, head.verdict)
  check('答案上方固定一句「不能当结论用」', body.includes('以下内容不能当结论用'))
  const faint = await page.evaluate(() => {
    const el = [...document.querySelectorAll('summary')].find((x) => x.textContent?.includes('处异常'))
    return el ? getComputedStyle(el).fontSize : ''
  })
  check('可信度相关的字不小于 11px', parseFloat(faint) >= 11, faint)
  await page.close()
}

console.log('\n=== 长表名、Copilot 结局、问数据的分段 ===')
{
  const { page } = await open('syn=schema&dense=1', { w: 380, h: 700, name: 'schema' })
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  check('十几张表的标题不撑破窄栏', overflow <= 0, `${overflow}px`)
  check('标题摘要成「N 张表」', (await page.locator('body').innerText()).includes('查看 12 张表的字段'))
  await page.close()

  const c = await open('syn=copilot&dense=1', { w: 380, h: 800, name: 'copilot' })
  const body = await c.page.locator('body').innerText()
  check('自查没修好的说法是画布语境', body.includes('没能自动修好') && !body.includes('没有自动运行'))
  check('少了一步说出来', body.includes('少了一步') && body.includes('excel_export'))
  check('修正写明第几轮', body.includes('第 1/2 轮'))
  check('收尾后的阶段行不再是「正在…」', !body.includes('正在理解需求'))
  check('收尾后没有还在跑的行', await c.page.locator('[aria-busy="true"]').count() === 0)
  await c.page.close()

  const q = await open('syn=chat', { w: 1100, h: 800, name: 'chat' })
  const before = await q.page.locator('body').innerText()
  check('执行开始后「规划」收成一行', before.includes('规划') && !before.includes('取数 → 汇总 → 出结论'))
  await q.page.locator('button', { hasText: '规划' }).first().click()
  await q.page.waitForTimeout(200)
  check('点开能看规划过程', (await q.page.locator('body').innerText()).includes('取数 → 汇总 → 出结论'))
  check('给出追问', before.includes('接着问'))
  await q.page.locator('button.chip').first().click()
  check('点追问把话交给页面', !!(await q.page.evaluate(() => window.__followUp)))
  await q.page.close()
}

console.log('\n=== 步骤行和画布联动 ===')
{
  const { page } = await open('syn=mixed&link=1', { w: 1100, h: 800 })
  const row = page.locator('[data-node-id="agent"]').first()
  check('步骤行带 data-node-id', await row.count() === 1)
  await row.locator('button').first().hover()
  check('悬停告诉画布是哪个节点', await page.evaluate(() => window.__hovered) === 'agent')
  // 从节点行移进它下面的查询行：人还指着这个节点，高亮不能被子行的 leave 清掉
  await row.locator('[data-node-id="agent"] button', { hasText: '查询 v_device_kpi' }).first().hover()
  check('移进子步骤时高亮还在', await page.evaluate(() => window.__hovered) === 'agent')
  await page.mouse.move(2, 2)
  check('移出步骤流就清掉', await page.evaluate(() => window.__hovered) === null)
  await row.locator('button').first().click()
  check('点击请画布取景到它', (await page.evaluate(() => window.__linked)).includes('agent'))
  await page.close()
}

console.log('\n=== 审批卡的上下文 ===')
{
  const { page } = await open('approval=1&actor=', { w: 820, h: 500, name: 'approval-unsigned' })
  const body = await page.locator('body').innerText()
  check('说出挂在哪个节点', body.includes('节点「班长复核」'))
  check('说出等了多久', body.includes('已等待 8 天'))
  check('常显批了会怎样', body.includes('驳回 → 走「驳回」那条出口'))
  check('没署名时提醒并给去处', body.includes('未署名') && await page.locator('a[href="/settings/prefs"]').count() === 1)
  await page.close()
  const s = await open('approval=1&actor=张工', { w: 820, h: 500, name: 'approval-signed' })
  check('署了名就写明以谁的名义', (await s.page.locator('body').innerText()).includes('将以「张工」签批'))
  await s.page.close()
}

console.log('\n=== 画布右栏：运行视图 ===')
{
  // 真页面、真 store：往 window.__studio 灌事件（同 check-canvas-fx）。工作流、会话、审批、
  // 运行全用 page.route 伪造，写操作一律回 409——检查脚本不写库
  const WF_ID = 'fx-stream'
  const RUN = 'fxstream0001'
  const GRAPH = {
    nodes: [
      { id: 'in', type: 'input', position: { x: 0, y: 0 }, data: { label: '问题', config: { fields: [{ name: 'q' }] } } },
      { id: 'team', type: 'supervisor', position: { x: 300, y: 0 }, data: { label: '供应链分析团队', config: { agents: [{ name: '采购员' }, { name: '质检员' }, { name: '物流员' }], max_rounds: 3 } } },
      { id: 'review', type: 'human', position: { x: 620, y: 0 }, data: { label: '主管审批', config: { mode: 'approve' } } },
      { id: 'out', type: 'output', position: { x: 920, y: 0 }, data: { label: '成果', config: {} } },
    ],
    edges: [{ id: 'e1', source: 'in', target: 'team' }, { id: 'e2', source: 'team', target: 'review' },
            { id: 'e3', source: 'review', target: 'out', sourceHandle: 'approved' }],
  }
  const WF = { id: WF_ID, name: '右栏检查', description: '', graph: GRAPH, tags: [], version: 1, status: 'draft',
    published_version: null, run_count: 0, created_at: '2026-09-26T00:00:00Z', updated_at: '2026-09-26T00:00:00Z' }
  const APPROVAL = { id: 'ap-x', run_id: RUN, node_id: 'review', mode: 'approve', title: '供应商结论可以发出吗？',
    payload: { message: 'C 交期最短' }, status: 'pending', response: {}, created_at: new Date(Date.now() - 95_000).toISOString(),
    workflow_name: '右栏检查', node_label: '主管审批', run_class: 'exploratory' }
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: 'dark' })
  await ctx.addInitScript(() => { try { localStorage.setItem('agentlab_actor', '张工') } catch { /* 隐私窗口 */ } })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  let pending = false
  await page.route(/\/api\/workflows(\?.*)?$/, async (route) => {
    if (route.request().method() !== 'GET') return json(route, { detail: '检查脚本不写库' }, 409)
    const real = await (await route.fetch()).json().catch(() => [])
    return json(route, [WF, ...(Array.isArray(real) ? real : [])])
  })
  await page.route(/\/api\/workflows\/fx-stream(\/.*)?(\?.*)?$/, (route) =>
    route.request().method() === 'GET' ? json(route, WF) : json(route, { detail: '检查脚本不写库' }, 409))
  await page.route(/\/api\/conversations(\/.*)?(\?.*)?$/, (route) =>
    route.request().method() === 'GET' ? json(route, []) : json(route, { id: 'fx-conv', kind: 'canvas', title: '', turns: [] }))
  const approvalHits = []
  await page.route(/\/api\/approvals(\/.*)?(\?.*)?$/, (route) => {
    if (route.request().method() !== 'GET') return json(route, { detail: '检查脚本不写库' }, 409)
    approvalHits.push(Date.now())
    return json(route, pending ? [APPROVAL] : [])
  })
  await page.route(/\/api\/runs(\/.*)?(\?.*)?$/, (route) => {
    const req = route.request()
    if (req.method() === 'GET' && req.url().includes(RUN)) {
      return json(route, { id: RUN, workflow_id: WF_ID, status: pending ? 'interrupted' : 'running', input: {}, output: {}, error: null, usage: {}, run_class: 'exploratory' })
    }
    return req.method() === 'GET' ? route.continue() : json(route, { detail: '这次运行已经不在执行中' }, 409)
  })
  await page.goto(`${WEB}/studio/${WF_ID}`, { waitUntil: 'networkidle' })
  await page.waitForFunction((id) => window.__studio?.getState().workflow?.id === id, WF_ID, { timeout: 15000 })

  const now = Date.now() / 1000
  const ev = (seq, type, node_id, data = {}, t = 0) => ({ seq, type, node_id, data, ts: now - 30 + t })
  const HEAD = [
    ev(1, 'run.started', null, { nodes: 4 }, 0),
    ev(2, 'node.started', 'in', { node_type: 'input', label: '问题' }, 0.1),
    ev(3, 'node.finished', 'in', { duration_ms: 3, preview: { q: 'x' } }, 0.2),
    ev(4, 'node.started', 'team', { node_type: 'supervisor', label: '供应链分析团队' }, 0.4),
    ev(5, 'agent.route.start', 'team', { round: 0 }, 0.5),
    ev(6, 'llm.end', 'team', { agent: '调度者', model: 'm', duration_ms: 2400, input_tokens: 820, output_tokens: 96, cost_usd: 0.0039 }, 2.9),
    ev(7, 'agent.route.end', 'team', { round: 0, duration_ms: 2400, agents: ['采购员', '质检员'], parallel: 2, done: false, reason: '互不依赖' }, 2.9),
    ev(8, 'agent.step.start', 'team', { agent: '采购员', instruction: '查交期', round: 0, parallel: 2 }, 3),
    ev(9, 'agent.step.start', 'team', { agent: '质检员', instruction: '查不良率', round: 0, parallel: 2 }, 3),
  ]
  const WAIT = [
    ev(10, 'agent.step.end', 'team', { agent: '质检员', duration_ms: 4200, round: 0, parallel: 2, preview: 'B 不良率高' }, 7.2),
    ev(11, 'agent.step.end', 'team', { agent: '采购员', duration_ms: 5600, round: 0, parallel: 2, preview: 'C 交期短' }, 8.6),
    ev(12, 'agent.route.end', 'team', { round: 1, duration_ms: 1900, agents: [], parallel: 0, done: true, reason: '可以收尾' }, 10.6),
    ev(13, 'node.finished', 'team', { duration_ms: 10200, preview: { text: 'C' } }, 10.6),
    ev(14, 'node.started', 'review', { node_type: 'human', label: '主管审批' }, 10.7),
    ev(15, 'human.requested', 'review', { kind: 'human_node', node_id: 'review', mode: 'approve', title: '供应商结论可以发出吗？' }, 10.8),
    ev(16, 'run.interrupted', 'review', { payload: { node_id: 'review', title: '供应商结论可以发出吗？' } }, 10.9),
  ]
  const FINISH = [
    ev(17, 'run.resumed', null, { response: { approved: true }, actor: '张工' }, 20),
    ev(18, 'node.started', 'review', { node_type: 'human', label: '主管审批', resumed: true }, 20.1),
    ev(19, 'human.resolved', 'review', { response: { approved: true }, actor: '张工' }, 20.1),
    ev(20, 'node.finished', 'review', { duration_ms: 0, preview: { approved: true } }, 20.2),
    ev(21, 'node.started', 'out', { node_type: 'output', label: '成果' }, 20.3),
    ev(22, 'node.finished', 'out', { duration_ms: 2, preview: { 结论: 'C' } }, 20.4),
    ev(23, 'run.finished', null, { output: { 结论: 'C 供应商交期最短' }, usage: { input_tokens: 4470, output_tokens: 946, total_tokens: 5416, cost_usd: 0.0322 },
      duration_ms: 11000, timing: { wall_ms: 20500, active_ms: 11000, wait_ms: 9500 } }, 20.5),
  ]
  const feed = (list) => page.evaluate((l) => { const s = window.__studio.getState(); for (const e of l) s.applyEvent(e) }, list)
  await page.evaluate((run) => window.__studio.setState({ run: { id: run, workflow_id: 'fx-stream', status: 'queued', input: {},
    output: {}, error: null, usage: {}, run_class: 'exploratory', version: null }, streaming: true, unsubscribe: () => {} }), RUN)
  await feed(HEAD)
  await page.waitForTimeout(500)
  const panel = page.locator('.sheet-in').first()
  const head = () => panel.locator('[data-turn]').first().locator('.sticky').innerText()
  check('运行中：栏头有停止', await panel.getByRole('button', { name: '停止这次运行' }).count() === 1)
  check('运行中：轮次头写「运行中」并走秒表', /运行中[\s\S]*\d\d:\d\d\.\d/.test(await head()), (await head()).replace(/\n/g, ' '))
  check('运行中：调度者和成员的 llm.end 算进用量（≥ 实时数）',
    /≥\s?916 tok|≥916 tok/.test(await panel.locator('[aria-label="这次运行的用量"]').innerText()),
    await panel.locator('[aria-label="这次运行的用量"]').innerText())
  check('#runId 可以点去运行记录', await panel.locator(`a[href="/runs/${RUN}"]`).count() === 1)

  // 悬停 / 点击步骤行联动画布
  await panel.locator('[data-node-id="team"] button').first().hover()
  check('悬停步骤行，画布知道是哪个节点', await page.evaluate(() => window.__studio.getState().hoveredNodeId) === 'team')
  await panel.locator('[data-node-id="team"] button').first().click()
  check('点击步骤行，请画布取景到它', await page.evaluate(() => window.__studio.getState().focusRequest?.id) === 'team')
  await page.mouse.move(700, 450)
  check('移开后清掉悬停', await page.evaluate(() => window.__studio.getState().hoveredNodeId) === null)

  // 画布上连点两个节点。以前右栏会去描一道红框：换选中时不摘，就一直留在一个好好的
  // 节点上；而且时间线这时被属性面板整个盖着，描了也看不见，只是把它滚离了原处
  const top0 = await panel.locator('[data-stream-scroll]').evaluate((el) => el.scrollTop)
  await page.evaluate(() => window.__studio.getState().select('in'))
  await page.waitForTimeout(200)
  await page.evaluate(() => window.__studio.getState().select('team'))
  await page.waitForTimeout(600)
  await page.evaluate(() => window.__studio.getState().select(null))
  await page.waitForTimeout(300)
  check('画布上连点节点：右栏不留描边', await panel.locator('[data-flash]').count() === 0)
  check('关掉属性面板回到原处', await panel.locator('[data-stream-scroll]').evaluate((el) => el.scrollTop) === top0)

  pending = true
  const fedAt = Date.now()
  await feed(WAIT)
  await page.waitForTimeout(500)
  const waitingHead = await head()
  // 以前 streaming 优先：等人时标题写「正在执行…」，工具栏还给一个点了必然 409 的「停止」
  check('等人：标题写「等待审批」，不写「正在执行」', waitingHead.includes('等待审批') && !waitingHead.includes('执行'), waitingHead.replace(/\n/g, ' '))
  check('等人：不给停止，给「去审批」', await panel.getByRole('button', { name: '停止这次运行' }).count() === 0
    && await panel.getByRole('button', { name: '去审批' }).count() === 1)
  // 审批列表 4 秒才轮询一次：卡片得是 run.interrupted 一到就去取的，不能碰运气等下一拍
  const fetched = approvalHits.filter((t) => t >= fedAt && t - fedAt < 300)
  check('等人：一停下来就去取审批卡（不等 4 秒轮询）', fetched.length > 0 && await panel.locator('[data-approval]').count() === 1,
    `停下后 ${fetched.map((t) => t - fedAt).join(',') || '—'} ms 取过`)
  check('等人：卡上写明将以谁的名义签批', (await panel.locator('[data-approval]').innerText()).includes('将以「张工」签批'))
  check('等人：没有还在转的步骤', await panel.locator('[aria-busy="true"]').count() === 0)
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/stream-panel-waiting-dark.png` })

  pending = false
  await feed(FINISH)
  await page.waitForTimeout(600)
  const doneHead = await head()
  const footer = await panel.locator('[aria-label="这次运行的用量"]').innerText()
  check('跑完：头部写已完成和执行时长', doneHead.includes('已完成') && doneHead.includes('11.0 s'), doneHead.replace(/\n/g, ' '))
  check('跑完：底栏换成后端累计，不带 ≥', footer.includes('5.4k tok') && footer.includes('$0.032') && !footer.includes('≥'), footer.replace(/\n/g, ' '))
  check('跑完：执行和等人分开写', footer.includes('执行 11.0 s') && footer.includes('等人 9.5 s'), footer.replace(/\n/g, ' '))
  // 审批列表 4 秒才轮询一次：跑完了卡片不能还挂在那儿等人点
  check('跑完：审批卡不再挂着', await panel.locator('[data-approval]').count() === 0)
  check('续跑是谁发起的写在分段线上', (await panel.locator('[data-phase-mark]').allInnerTexts()).some((t) => t.includes('张工')))
  check('审批留痕写签批人，不写「你」', (await panel.innerText()).includes('→ 张工 放行了'))
  check('画布右栏没有运行时报错', errors.length === 0, errors.join(' | '))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(200)
      await page.screenshot({ path: `${SHOTS}/stream-panel-finished-${theme}.png` })
    }
  }
  await ctx.close()
}

console.log('\n=== 助手栏：展开之前的轮次 ===')
{
  // 以前展开后补在上面的轮次改了"第一轮是谁"，被当成换了一批、重新定位到最底：
  // 刚补出来的那几轮在视野上方，看着像没点上
  const WF_ID = 'fx-past'
  const WF = { id: WF_ID, name: '记忆', description: '', tags: [], version: 1, status: 'draft', published_version: null,
    run_count: 0, created_at: '2026-09-26T00:00:00Z', updated_at: '2026-09-26T00:00:00Z',
    graph: { nodes: [{ id: 'in', type: 'input', position: { x: 0, y: 0 }, data: { label: '问题', config: {} } }], edges: [] } }
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 700 } })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  await page.route(/\/api\/workflows(\?.*)?$/, (route) =>
    route.request().method() === 'GET' ? json(route, [WF]) : json(route, { detail: '检查脚本不写库' }, 409))
  await page.route(/\/api\/workflows\/fx-past(\/.*)?(\?.*)?$/, (route) =>
    route.request().method() === 'GET' ? json(route, WF) : json(route, { detail: '检查脚本不写库' }, 409))
  await page.route(/\/api\/conversations(\/.*)?(\?.*)?$/, (route) =>
    route.request().method() === 'GET' ? json(route, []) : json(route, { id: 'c', kind: 'canvas', title: '', turns: [] }))
  await page.goto(`${WEB}/studio/${WF_ID}`, { waitUntil: 'networkidle' })
  await page.waitForFunction((id) => window.__studio?.getState().workflow?.id === id, WF_ID, { timeout: 15000 })
  await page.evaluate(() => {
    const long = '这是一段很长的回答。'.repeat(40)
    const past = Array.from({ length: 4 }, (_, i) => ({ id: `p${i}`, question: `之前的问题 ${i + 1}`, answer: long, status: 'done' }))
    const turns = Array.from({ length: 2 }, (_, i) => ({ id: `t${i}`, instruction: `这次的问题 ${i + 1}`, phase: 'done',
      ops: [{ op: 'reply', text: long }], reply: long }))
    window.__studio.setState({ copilotMemory: { past: [...past, ...past.slice(0, 2).map((p, i) => ({ ...p, id: `cur${i}` }))], total: 6, turns: 6 },
      copilotTurns: turns })
  })
  await page.waitForTimeout(400)
  const sc = () => page.evaluate(() => {
    const el = document.querySelector('[data-stream-scroll]')
    return { top: Math.round(el.scrollTop), max: Math.round(el.scrollHeight - el.clientHeight) }
  })
  const s0 = await sc()
  check('打开时停在最新处', s0.max > 0 && s0.max - s0.top < 64, JSON.stringify(s0))
  await page.getByRole('button', { name: /之前的 \d+ 轮/ }).click()
  await page.waitForTimeout(400)
  const s1 = await sc()
  const inView = await page.evaluate(() => {
    const el = document.querySelector('[data-stream-scroll]').getBoundingClientRect()
    return [...document.querySelectorAll('[data-stream-scroll] [data-turn]')]
      .filter((t) => { const b = t.getBoundingClientRect(); return b.bottom > el.top && b.top < el.bottom })
      .map((t) => t.dataset.turn)
  })
  check('展开后从补出来的第一轮读起', s1.top === 0 && inView[0] === 'past-p0', `${JSON.stringify(s1)} ${inView.join(',')}`)
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(150)
      await page.screenshot({ path: `${SHOTS}/studio-past-${theme}.png` })
    }
  }
  await ctx.close()
}

console.log('\n=== 编号列、追问、CSV ===')
{
  // attribute_group 的 '001'、factory_code 的 '1063' 长得像数：以前右对齐，还给出
  // 「按 attribute_group 从高到低排」这种说不通的追问
  const { page } = await open('syn=codes', { w: 1100, h: 500, name: 'codes' })
  const chips = await page.$$eval('button.chip', (b) => b.map((x) => x.innerText))
  check('追问拿"量"排序，不拿编号 / 分组', chips.some((c) => c.includes('按 output_qty 从高到低排'))
    && !chips.some((c) => /attribute_group|factory_code/.test(c)), chips.join(' | '))
  const align = await page.evaluate(() =>
    Object.fromEntries([...document.querySelectorAll('th')].map((th) => [th.innerText, getComputedStyle(th).textAlign])))
  check('编号列不按数右对齐，量照常右对齐',
    align.factory_code === 'left' && align.attribute_group === 'left' && align.output_qty === 'right', JSON.stringify(align))
  // 带 BOM：Excel 按 GBK 猜编码，不带的话中文列名全是乱码
  const [dl] = await Promise.all([page.waitForEvent('download', { timeout: 5000 }).catch(() => null),
    page.locator('button', { hasText: 'CSV' }).first().click()])
  const buf = dl ? await (async () => {
    const chunks = []
    for await (const c of await dl.createReadStream()) chunks.push(c)
    return Buffer.concat(chunks)
  })() : Buffer.alloc(0)
  check('导出的 CSV 以 BOM 开头、内容完整', buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf
    && buf.toString('utf8').includes('1065,三号线,001,980'), `${buf.length} 字节`)
  await page.close()

  // 只有一行：「只看某一个」「从高到低排」都是原样再问一遍
  const q = await open('case=db&follow=1', { w: 1280, h: 700 })
  const one = await q.page.$$eval('button.chip', (b) => b.map((x) => x.innerText))
  check('一行的结果不给排序、筛选的追问', !one.some((c) => /从高到低|只看/.test(c)), one.join(' | '))
  await q.page.close()
}

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 助手流渲染全部通过')
process.exit(failed ? 1 : 0)
