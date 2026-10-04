// AssistantStream 的渲染回归检查。
//
// 解码器的检查（check-decode.mjs）守的是"翻译对不对"，这里守的是"画出来
// 对不对"——两者能各自通过而合起来是坏的：Step 里字段都对，组件却把它渲染成
// 一屏转义 JSON，或者在 360px 窄栏里把页面撑得横向滚动。
//
// 用真浏览器跑真组件，数据还是 fixtures.json 里那批真实事件。
// 跑之前前端得起着（./scripts/dev.sh），默认连 5273。对别的实例（比如一份沙箱拷贝）跑时
// 带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-stream.mjs
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

/**
 * 一节一节地跑：某一节里元素找不到、等待超时，只记成这一节失败，接着跑下一节，
 * 不让一处卡住把后面的检查一起吞掉。各节自己开页面
 */
async function section(name, fn) {
  console.log(`\n=== ${name} ===`)
  try {
    await fn()
  } catch (e) {
    check(`${name} 中途出错`, false, String(e?.message ?? e).split('\n')[0])
  }
}

const browser = await chromium.launch({ executablePath: CHROME })

for (const kind of CASES) await section(kind, async () => {
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
})

await section('展开交互', async () => {
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
})

await section('Markdown 渲染', async () => {
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
})

await section('长报告的折叠', async () => {
  // 折叠以前是按字符硬切的，切点落进表格中间：前面几行渲染成表格、最后半行
  // 留成原始的 `| 1 | ThearchyHelios | …`。用户看到的是一份被咬掉一口的报告。
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  await page.goto(`${WEB}/preview.html?long=1`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(300)

  const folded = await page.locator('body').innerText()
  check('折叠时说明「以下内容已折叠」', folded.includes('以下内容已折叠'))
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
})

await section('没查库的那一轮', async () => {
  // 「涉及数据必须真查」是这条路径上最硬的约定，放开直接回答之后，用户得能
  // 一眼分清哪些结论背后真的动了库
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  await page.goto(`${WEB}/preview.html?noquery=1`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(300)
  const body = await page.locator('body').innerText()
  check('说明本条未查询数据库', body.includes('未查询数据库'))
  check('声明排在结论前面',
        body.indexOf('未查询数据库') < body.indexOf('role.level'),
        `声明@${body.indexOf('未查询数据库')} 结论@${body.indexOf('role.level')}`)
  check('答案本身照常渲染', body.includes('口径'))
  await page.close()
})

await section('复核说明', async () => {
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
  check('改写前的原文可以展开', body.includes('查看改写前的原文'))
  check('原文默认不占地方', !body.includes('平台管理员可以修改他人权限。\n'))

  const overflow = await page.evaluate(() =>
    document.documentElement.scrollWidth - document.documentElement.clientWidth)
  check('页面不横向溢出', overflow <= 0, `${overflow}px`)
  await page.close()
})

await section('并行分支', async () => {
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
})

await section('协作团队的泳道', async () => {
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
    // 说法和画布上的协作矩阵同一套：「并行节省 X」
    check(`${label}：说清并行节省了多少`, /并行节省 11\.8 s（2 轮并行）/.test(body),
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
})

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
// 先等滚动容器挂上：open() 只固定等 400ms，并行跑、机器忙的时候组件还没渲染出来
// （check-all 分道并行后实测挂过一次：读 null 的 scrollTop）
const scrollState = async (page) => {
  await page.waitForSelector('[data-stream-scroll]', { timeout: 10_000 })
  return page.evaluate(() => {
    const el = document.querySelector('[data-stream-scroll]')
    return { top: Math.round(el.scrollTop), max: Math.round(el.scrollHeight - el.clientHeight) }
  })
}
await section('空态', async () => {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  await page.goto(`${WEB}/preview.html?case=__none__`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(250)
  const text = await page.locator('body').innerText()
  check('没有内容时给的是空态而不是白屏', text.includes('问你的数据'))
  await page.close()

})

await section('跟随：只在贴底时跟', async () => {
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
})

await section('点开已完成轮次的执行过程：不算新进展、不拽人（终验 NEW）', async () => {
  // 按需取回的历史步骤以前算进「N 条新进展」：没有任何运行在跑，底下却浮出「24 条新进展」
  for (const [n, pick, label] of [[1, 0, '单轮停在顶部'], [6, 2, '6 轮点开中间一轮']]) {
    const { page, errors } = await open(`expand=${n}`, { w: 1100, h: 700, name: `expand-${n}` })
    await page.waitForSelector('[data-stream-scroll]', { timeout: 10_000 })
    const at = await page.evaluate((i) => {
      const el = document.querySelector('[data-stream-scroll]')
      const turn = document.querySelectorAll('[data-turn]')[i]
      el.scrollTop = i === 0 ? 0 : Math.max(0, el.scrollTop + turn.getBoundingClientRect().top - el.getBoundingClientRect().top - 40)
      el.dispatchEvent(new Event('scroll'))
      return Math.round(el.scrollTop)
    }, pick)
    await page.waitForTimeout(150)
    await page.evaluate((i) => window.__expand(i), pick)
    await page.waitForTimeout(400)
    const after = await scrollState(page)
    const pill = await page.locator('[data-jump-latest]').innerText().catch(() => '')
    check(`${label}：点开之后不浮出「条新进展」`, !/条新进展/.test(pill), pill)
    check(`${label}：没被拽到底部`, Math.abs(after.top - at) < 8, `${at} → ${after.top}/${after.max}`)
    check(`${label}：没有运行时报错`, errors.length === 0, errors.join(' | '))
    await page.close()
  }
})

await section('回看历史运行：停在失败处', async () => {
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
  check('报错写全，不截成一行', body.includes('推送失败：通知机器人地址为空'))
  check('技术细节默认收着', !body.includes('ConnectError') && body.includes('技术细节'))
  check('动作槽渲染出页面给的按钮', body.includes('重试本轮'))
  check('跳过的节点在时间线上留痕', body.includes('跳过「背景检索」') && body.includes('满足跳过条件'))
  check('#runId 可以点去运行记录', await page.locator('a[href="/runs/syn-mixed"]').count() === 1)
  // 「开始运行」「完成」和头部说的是同一件事，不再单列；「继续运行」是分段线，
  // 失败的运行里它以前是一个红叉，读起来像续跑本身出了错
  check('和头部重复的生命周期行不再单列', !body.includes('开始运行（6 个节点）'))
  const mark = await page.locator('[data-phase-mark]').allInnerTexts()
  check('继续运行是一条分段线，说清谁发起的', mark.some((t) => t.includes('继续运行') && t.includes('张工')), mark.join(' | '))
  check('分段线不画成失败', await page.locator('[data-phase-mark] [data-status="failed"]').count() === 0)
  const live = await page.locator('[role="status"][aria-live="polite"]').allInnerTexts()
  check('状态变化有读屏播报区', live.some((t) => t.includes('失败')), live.join(' | '))
  await page.close()
})

await section('运行中的时间感', async () => {
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
  check('节点轨标出已跳过、运行中、尚未开始的',
    rail.some((t) => t.includes('已跳过')) && rail.some((t) => t.includes('运行中')) && rail.includes('尚未开始'),
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
      const cell = [...document.querySelectorAll('[data-node-rail] > span')].find((e) => e.title === '尚未开始')
      if (!cell) return null
      const cs = getComputedStyle(cell)
      const mark = cs.backgroundColor !== 'rgba(0, 0, 0, 0)' ? cs.backgroundColor : (cs.boxShadow.match(/rgba?\([^)]+\)/) ?? [''])[0]
      let el = cell.parentElement
      while (el && getComputedStyle(el).backgroundColor === 'rgba(0, 0, 0, 0)') el = el.parentElement
      const bg = el ? getComputedStyle(el).backgroundColor : 'rgb(0,0,0)'
      const [a, b] = [lum(mark), lum(bg)].sort((x, y) => y - x)
      return { mark, bg, ratio: +((a + 0.05) / (b + 0.05)).toFixed(2) }
    })
    check(`${theme}：尚未开始的格子看得见（≥ 3:1）`, !!pend && pend.ratio >= 3, JSON.stringify(pend))
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
})

await section('长运行：按轮折叠', async () => {
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
  check('没列出的那一截说清还有多少、怎么继续', /还有 \d+ 轮未列出/.test(more) && more.includes('再列') && more.includes('全部列出'),
    more.replace(/\n/g, ' '))
  check('不再说「已在上面展开」的假话', !(await page.locator('body').innerText()).includes('已在上面展开'))
  await box.locator('[data-exec-more] button', { hasText: '全部列出' }).click()
  await page.waitForTimeout(300)
  const every = await execs()
  check('全部列出后 145 轮一轮不少、不重复', every.length === 145 && new Set(every).size === 145, `${every.length} 行`)
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
  await page.close()
})

await section('几轮审批：分轮但不收起', async () => {
  // 驳回两次再放行：只执行了三次的节点按轮分组、全部摊开——收起来就把那两次驳回藏了
  const { page } = await open('case=loop_approve', { w: 1100, h: 900 })
  const body = await page.locator('body').innerText()
  check('三轮审批都看得见（宽窄两栏各三条）', (body.match(/这条公告可以发吗/g) ?? []).length === 6)
  check('轮次边界画出来了', body.includes('第 3 次'))
  check('老数据里光秃秃的「继续运行」不单列', !body.includes('继续运行'))
  await page.close()
})

await section('协作团队：运行中的说法', async () => {
  const { page } = await open('syn=team-live&dense=1', { w: 380, h: 760, name: 'team-live' })
  const body = await page.locator('body').innerText()
  check('运行中写「N 名成员并行中」', body.includes('3 名成员并行中'))
  check('运行中不报「节省 0」', !/节省 0/.test(body))
  check('进行中的轮次不写 0 ms', body.includes('进行中') && !/第1轮 · 0/.test(body))
  check('调度这一行说人话', body.includes('第 1 轮：交给 采购员、质检员、物流员（并行）'))
  check('运行中的说法和协作矩阵一致（本轮 N 名成员并行）', body.includes('本轮 3 名成员并行中'))
  check('没有原始事件名', !/agent\.(route|step)/.test(body))
  await page.close()
  const r = await open('syn=team-routing&dense=1', { w: 380, h: 760, name: 'team-routing' })
  check('调度者规划下一步时有一行进行中', (await r.page.locator('body').innerText()).includes('调度者正在规划下一步'))
  await r.page.close()
  // 一轮里三人都还在跑时被取消（runfx-17 终验）：那一轮不知道本来要多久，不写「0 ms」
  for (const [w, dense] of [[380, '1'], [1100, '0']]) {
    const c = await open(`syn=team-cancelled&dense=${dense}`, { w, h: 760, name: `team-cancelled-${dense === '1' ? 'dense' : 'wide'}` })
    const lanes = (await c.page.locator('[data-team-lanes]').innerText().catch(() => '')).replace(/\n/g, ' ')
    check(`${dense === '1' ? '窄栏' : '宽栏'}：取消在一轮中途，轮次脚注不写 0 ms`, !!lanes && !/第\s?1\s?轮[^第]*· 0 ms/.test(lanes) && !/(^|[^\d.])0 ms/.test(lanes), lanes.slice(-80))
    check(`${dense === '1' ? '窄栏' : '宽栏'}：轮次脚注说这一轮被取消了`, /第\s?1\s?轮[^第]*已取消/.test(lanes), lanes.slice(-80))
    await c.page.close()
  }
})

await section('出具横幅、答案操作、证据下钻', async () => {
  const { page } = await open('syn=issued', { w: 1100, h: 900, name: 'issued' })
  const body = await page.locator('body').innerText()
  // gaps 以前一条都不显示：横幅说「请对照下方声明」，下方什么都没有
  check('横幅列出校验未全部完成的原因', body.includes('校验未全部完成') && body.includes('叙述模板渲染为空'))
  check('无法追溯的数字带上下文', body.includes('晚班比上周低 6 个百分点'))
  check('正文里画出了那个数字', await page.locator('[data-mark="warn"]').count() === 1
    && (await page.locator('[data-mark="warn"]').innerText()) === '6')
  check('说清数字追溯到哪张口径卡', body.includes('可追溯的 2 个数字均来自此口径卡'))
  check('探索运行标注不进正式归档', body.includes('探索运行 · 不进正式归档'))
  check('头部和复核结论一致', body.includes('存在缺口'))
  check('吸顶的头上挂着出具档位', (await page.locator('[data-turn] .sticky').first().innerText()).includes('降档出具'))
  await page.locator('.group\\/answer').first().hover()
  check('答案可以复制', await page.locator('button[aria-label="复制"]').count() > 0)
  // 复制 / 导出以前贴着整块的右上角浮出，正好压在横幅的「可追溯 N 个数字」上
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
      check('说明已校验通过', modal.includes('校验通过'))
      check('完整结果可以导出 CSV', modal.includes('CSV'))
    }
    if (SHOTS) await p2.screenshot({ path: `${SHOTS}/stream-artifact-${theme}.png` })
    await p2.close()
  }
})

await section('复核判了不可信', async () => {
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
  check('答案上方固定一句「不可作为结论使用」', body.includes('以下内容不可作为结论使用'))
  const faint = await page.evaluate(() => {
    const el = [...document.querySelectorAll('summary')].find((x) => x.textContent?.includes('处异常'))
    return el ? getComputedStyle(el).fontSize : ''
  })
  check('可信度相关的字不小于 11px', parseFloat(faint) >= 11, faint)
  await page.close()
})

await section('长表名、Copilot 结局、问数据的分段', async () => {
  const { page } = await open('syn=schema&dense=1', { w: 380, h: 700, name: 'schema' })
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  check('十几张表的标题不撑破窄栏', overflow <= 0, `${overflow}px`)
  check('标题摘要成「N 张表」', (await page.locator('body').innerText()).includes('查看 12 张表的字段'))
  await page.close()

  const c = await open('syn=copilot&dense=1', { w: 380, h: 800, name: 'copilot' })
  const body = await c.page.locator('body').innerText()
  check('自查没修好的说法是画布语境', body.includes('未能自动修正') && !body.includes('未自动运行'))
  check('跳过的步骤说出来', body.includes('已跳过一个步骤') && body.includes('excel_export'))
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
  check('给出追问', before.includes('继续提问'))
  await q.page.locator('button.chip').first().click()
  check('点追问把话交给页面', !!(await q.page.evaluate(() => window.__followUp)))
  await q.page.close()
})

await section('合并查询：输入、合并 SQL、结果预览，警告各占一行并给出下一步', async () => {
  const { page, errors } = await open('syn=merge', { w: 1100, h: 900, name: 'merge' })
  const row = page.locator('[data-step-code="merge"]').first()
  const line = (await row.innerText().catch(() => '')).replace(/\s+/g, ' ')
  check('合并那一行说合并的是哪几个输入，行尾写行数', line.includes('合并 s、v 的查询结果') && line.includes('8 行'), line)
  check('有警告：这一行标成警告、说有几条、指到下方', line.includes('2 条警告，详见本节点下方'), line)
  await row.locator('button').first().click()
  await page.waitForTimeout(200)
  const merge = row.locator('[data-step-merge]')
  const inputs = (await merge.innerText().catch(() => '')).replace(/\s+/g, ' ')
  check('展开：合并自 2 个输入，每个写别名、节点名、行数、数据源', inputs.includes('合并自 2 个输入')
    && ['s', '门店销售', 'v', '到店人数', '4 行', '数据源 stores', '数据源 members'].every((t) => inputs.includes(t)), inputs)
  check('合并 SQL 有标签、带复制', (await row.innerText()).includes('合并 SQL')
    && await row.getByRole('button', { name: '复制合并 SQL' }).count() === 1)
  check('结果预览画成表格（列是合并结果的列）', (await row.locator('table th').allInnerTexts()).some((t) => t.includes('到店人数')))
  // 合并结果 8 行、预览只存了 5 行：说「此处仅为预览」和「显示前 5 / 8 行」，不说「查询已达行数上限」（A3）
  const foot = (await row.innerText().catch(() => '')).replace(/\s+/g, ' ')
  check('预览只存了前几行：写「显示前 5 / 8 行」和「仅为预览」，不说查询已达行数上限', foot.includes('显示前 5 / 8 行')
    && foot.includes('此处仅为预览') && !foot.includes('查询已达行数上限'), foot.slice(-120))
  check('输入写在合并 SQL 前面：先看合并的是什么，再看怎么合并', await row.evaluate((el) => {
    const m = el.querySelector('[data-step-merge]')
    const pre = el.querySelector('pre')
    return !!m && !!pre && !!(m.compareDocumentPosition(pre) & Node.DOCUMENT_POSITION_FOLLOWING)
  }))
  const key = page.locator('[data-step-code="merge_key_type"]').first()
  check('键类型不一致：标题点名两列，下一步不用展开就看得到', (await key.innerText().catch(() => '')).includes('合并键类型不一致：s.门店 与 v.门店')
    && (await key.locator('[data-step-next]').innerText().catch(() => '')).includes('CAST'))
  const grew = page.locator('[data-step-code="merge_rows_grew"]').first()
  check('行数放大：说合并键可能不唯一，下一步说怎么改', (await grew.innerText().catch(() => '')).includes('合并键可能不唯一')
    && (await grew.locator('[data-step-next]').innerText().catch(() => '')).includes('聚合到相同粒度'))
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
  await page.close()

  const narrow = await open('syn=merge&dense=1', { w: 380, h: 900 })
  await narrow.page.locator('[data-step-code="merge"] button').first().click()
  await narrow.page.waitForTimeout(200)
  const overflow = await narrow.page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  check('窄栏展开合并那一行：输入清单、合并 SQL、预览都不撑出横向滚动', overflow <= 0, `${overflow}px`)
  await narrow.page.close()
})

await section('并行查询各配各的；查询的 SQL 检查；指标的问题指到来源查询的 SQL（A1、B2）', async () => {
  const { page, errors } = await open('syn=parallel&link=1', { w: 1100, h: 900, name: 'parallel' })
  const qa = page.locator('[data-node-id="q_a"]').filter({ hasText: 'SQL 检查：1 处错误' }).last()
  check('出问题的那条查询：行上就说「SQL 检查：1 处错误」', await qa.count() === 1)
  await qa.locator('button').first().click()
  await page.waitForTimeout(200)
  const list = qa.locator('[data-step-sql-checks] [data-sql-check="fanout_sum"][data-level="error"]')
  const text = (await qa.locator('[data-step-sql-checks]').innerText().catch(() => '')).replace(/\s+/g, ' ')
  check('展开：用 SQL 检查清单列出级别、规则名、说明、涉及的表和列', await list.count() === 1 && text.includes('错误')
    && text.includes('一对多关联后重复计算') && text.includes('orders.total_amount') && !text.includes('fanout_sum'), text)
  const qb = (await page.locator('[data-node-id="q_b"]').last().innerText().catch(() => '')).replace(/\s+/g, ' ')
  check('另一条查询没有检查结果，也没被配上别人的结果', !qb.includes('SQL 检查') && !qb.includes('119'), qb)
  const metric = page.locator('[data-step-code="metric_sql_check"]')
  check('两个指标出自同一条查询：时间线一行', await metric.count() === 1, String(await metric.count()))
  const fix = metric.locator('[data-fix="canvas"]')
  check('「打开设置」落到来源查询节点的 SQL：写「打开「订单金额查询」的 SQL」', (await fix.innerText().catch(() => '')).trim()
    === '打开「订单金额查询」的 SQL' && await fix.getAttribute('data-fix-node') === 'q_a'
    && await fix.getAttribute('data-fix-field') === 'args.sql', await fix.innerText().catch(() => ''))
  await fix.click()
  const opened = await page.evaluate(() => window.__opened ?? [])
  check('……点了打开的是来源查询节点的 args.sql，不是口径卡', JSON.stringify(opened) === JSON.stringify([['q_a', 'args.sql']]),
    JSON.stringify(opened))
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
  await page.close()
})

await section('参考了哪些表：画布右栏和问数据页（context 操作）', async () => {
  const c = await open('syn=copilot-context&dense=1', { w: 380, h: 800, name: 'copilot-context' })
  const row = c.page.locator('[data-step-code="copilot_context"]')
  check('画布右栏：一行说参考了几张表', await row.count() === 1 && (await row.innerText()).includes('参考了 5 张表'),
    await row.count() ? await row.innerText() : '没有这一行')
  check('表名收在展开区，不铺满窄栏', !(await row.innerText()).includes('channel_visits'))
  await row.locator('button').first().click()
  await c.page.waitForTimeout(150)
  const expanded = await row.innerText()
  check('展开看到按数据源分组的表名', expanded.includes('「scenic」按需求从 51 张表中挑出 3 张')
    && expanded.includes('channel_visits') && expanded.includes('「shop」全部 2 张表'), expanded.slice(0, 160))
  const overflow = await c.page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  check('展开后不撑破 380px 窄栏', overflow <= 0, `${overflow}px`)
  check('没有运行时报错', c.errors.length === 0, c.errors.join(' | '))
  await c.page.close()

  const f = await open('syn=copilot-fallback&dense=1', { w: 380, h: 800, name: 'copilot-fallback' })
  const fb = f.page.locator('[data-step-code="copilot_context"]')
  check('挑表失败：写明只给了表名', await fb.count() === 1 && (await fb.innerText()).includes('未能按需求挑选，已提供全部表名'))
  await fb.locator('button').first().click()
  await f.page.waitForTimeout(150)
  check('挑表失败：展开看到原因', (await fb.innerText()).includes('原因：挑选数据表超过 20 秒未完成'))
  await f.page.close()

  const q = await open('syn=chat', { w: 1100, h: 800, name: 'chat-context' })
  check('问数据页：执行开始后这一行随「规划」收起', !(await q.page.locator('body').innerText()).includes('参考了 2 张表'))
  await q.page.locator('button', { hasText: '规划' }).first().click()
  await q.page.waitForTimeout(200)
  const qrow = q.page.locator('[data-step-code="copilot_context"]')
  check('问数据页：点开「规划」看到参考了几张表', await qrow.count() === 1 && (await qrow.innerText()).includes('参考了 2 张表'))
  await qrow.locator('button').first().click()
  await q.page.waitForTimeout(150)
  check('问数据页：展开看到表名', (await qrow.innerText()).includes('v_device_kpi、dim_device'))
  check('问数据页：没有运行时报错', q.errors.length === 0, q.errors.join(' | '))
  await q.page.close()
})

await section('建议更新数据目录：卡片的保存、忽略和 409（catalog_patch 操作）', async () => {
  // 保存、预览都在浏览器层答掉：预览页没有真的数据源，检查也不许写库
  const detail = (version) => ({ table_name: 'visits', in_schema: true, notes: {}, version, updated_at: null, updated_by: '检查脚本',
    structure: null, system_notes: false, usage: 0 })
  const c = await open('syn=catalog-patch&dense=1', { w: 380, h: 900, name: 'catalog-patch' })
  const sent = []
  let conflictOnce = true
  await c.page.route('**/api/datasources/src-scenic/catalog/visits/patch**', async (route) => {
    const req = route.request()
    const body = req.postDataJSON()
    sent.push({ url: req.url(), body, actor: req.headers()['x-actor'] })
    if (req.url().endsWith('/patch/preview')) {
      // 重新载入：别人刚把码值补了一个 8，改前改后按最新的算
      return route.fulfill({ json: { table_name: 'visits', version: 4, problems: [], changes: [
        { path: 'columns.status.codes', before: { 1: '有效', 0: '作废', 8: '退票' }, before_status: 'confirmed',
          after: { 1: '有效', 0: '作废', 8: '退票', 9: '作废' }, value: { 9: '作废' }, reason: '用户说明 status=9 表示作废', state: 'change' },
        { path: 'valid_filter', before: 'status = 1', before_status: 'proposed', after: 'status = 1 AND status <> 9',
          value: 'status = 1 AND status <> 9', reason: '统计时要排除作废记录', state: 'change' },
      ] } })
    }
    if (conflictOnce) {
      conflictOnce = false
      return route.fulfill({ status: 409, json: { detail: '表「visits」的数据目录刚被修改过，请重新载入后再提交' } })
    }
    return route.fulfill({ json: detail(5) })
  })
  await c.page.route('**/api/datasources/src-scenic/catalog/visits/impact', (route) =>
    route.fulfill({ json: { table: 'visits', templates: [
      { workflow_id: 'wf-daily', name: '入园日报', version: 4, level: 'governed', impact: 'direct',
        nodes: [{ node_id: 'q', label: '查询入园人数', type: 'tool', impact: 'direct' }] },
      { workflow_id: 'wf-ask', name: '客流分析', version: 2, level: 'published', impact: 'possible',
        nodes: [{ node_id: 'ask', label: '分析客流', type: 'agent', impact: 'possible' }] },
    ] } }))
  const card = c.page.locator('[data-catalog-patch="visits"]')
  check('卡片在：标题写「建议更新数据目录」', await card.count() === 1 && (await card.innerText()).includes('建议更新数据目录'))
  const codes = card.locator('[data-patch-change="columns.status.codes"]')
  check('逐项写改到哪儿：「列 status 的码值」', (await codes.innerText()).includes('列 status 的码值'))
  check('改前、改后、理由都在', (await codes.locator('[data-patch-before]').innerText()).includes('0=作废、1=有效')
    && (await codes.locator('[data-patch-after]').innerText()).includes('9=作废')
    && (await codes.locator('[data-patch-reason]').innerText()).includes('status=9'))
  check('新补的码值加重显示', await codes.locator('[data-code-changed="9"]').count() === 1
    && await codes.locator('[data-code-changed="1"]').count() === 0)
  check('值不变的项写明保存即确认', (await card.locator('[data-patch-state="confirm"]').innerText()).includes('值不变，保存即确认'))
  check('改前写状态（推断）', (await card.locator('[data-patch-change="valid_filter"] [data-patch-before]').innerText()).includes('（推断）'))
  check('过程里也记了一行', (await c.page.locator('[data-step-code="catalog_patch"]').innerText()).includes('建议更新「入园记录」的数据目录（3 项）'))
  const overflow = await c.page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  check('卡片不撑破 380px 窄栏', overflow <= 0, `${overflow}px`)

  // 第一次保存撞上 409：写明「这张表刚被修改过」，给「重新载入」。署名在设置页填，这里直接写进本机
  await c.page.evaluate(() => localStorage.setItem('agentlab_actor', '检查脚本'))
  await card.locator('[data-patch-save]').click()
  await card.locator('[data-patch-conflict]').waitFor({ timeout: 3000 })
  check('409：提示这张表刚被修改过', (await card.innerText()).includes('这张表刚被修改过'))
  check('保存带着提案对照的版本、原样取值和署名', sent[0]?.body?.if_version === 3
    && JSON.stringify(sent[0]?.body?.changes?.[0]?.value) === '{"9":"作废"}' && !!sent[0]?.actor, JSON.stringify(sent[0]))
  await card.locator('[data-patch-reload]').click()
  await c.page.waitForFunction(() => document.querySelector('[data-catalog-patch="visits"]')?.getAttribute('data-patch-version') === '4',
    null, { timeout: 3000 })
  check('重新载入交回的是助手原来的提案', sent[1]?.url.endsWith('/patch/preview')
    && JSON.stringify(sent[1]?.body?.changes?.[0]?.value) === '{"9":"作废"}', JSON.stringify(sent[1]))
  check('重新载入后改前按最新的目录写', (await codes.locator('[data-patch-before]').innerText()).includes('8=退票'))
  await card.locator('[data-patch-save]').click()
  await card.locator('[data-patch-done]').waitFor({ timeout: 3000 })
  check('再保存带新版本', sent[2]?.body?.if_version === 4, JSON.stringify(sent[2]?.body))
  check('保存成功：写明第几版，给到数据目录的入口', (await card.locator('[data-patch-done]').innerText()).includes('已保存到数据目录（第 5 版）')
    && (await card.locator('[data-patch-open]').getAttribute('href')) === '/data/catalog/src-scenic/visits')
  check('保存后不再有保存按钮', await card.locator('[data-patch-save]').count() === 0)
  await card.locator('[data-catalog-impact="2"]').waitFor({ timeout: 3000 })
  const impact = await card.locator('[data-catalog-impact]').innerText()
  check('保存后列出受影响的模板：直接引用、可能涉及', impact.includes('2 个已发布模板引用这张表') && impact.includes('入园日报')
    && impact.includes('直接引用') && impact.includes('可能涉及'), impact.replace(/\s+/g, ' '))
  check('没有运行时报错', c.errors.length === 0, c.errors.join(' | '))
  await c.page.close()

  // 忽略：收成一行，可以重新查看；不发任何请求
  const g = await open('syn=catalog-patch&dense=1', { w: 380, h: 900 })
  let writes = 0
  await g.page.route('**/api/datasources/**', (route) => { writes++; return route.abort() })
  const gc = g.page.locator('[data-catalog-patch="visits"]')
  await gc.locator('[data-patch-ignore]').click()
  check('忽略：收成一行说明', (await gc.getAttribute('data-patch-phase')) === 'ignored'
    && (await gc.innerText()).includes('已忽略这条建议'))
  await gc.locator('[data-patch-unignore]').click()
  check('重新查看：卡片回来', await gc.locator('[data-patch-save]').count() === 1)
  check('忽略不发请求', writes === 0, `${writes}`)
  await g.page.close()
})

await section('步骤行和画布联动', async () => {
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
})

await section('审批卡的上下文', async () => {
  const { page } = await open('approval=1&actor=', { w: 820, h: 500, name: 'approval-unsigned' })
  const body = await page.locator('body').innerText()
  check('说出挂在哪个节点', body.includes('节点「班长复核」'))
  check('说出等了多久', body.includes('已等待 8 天'))
  check('常显批了会怎样', body.includes('驳回：转入「驳回」分支'))
  check('没署名时提醒并给去处', body.includes('未署名') && await page.locator('a[href="/settings/prefs"]').count() === 1)
  await page.close()
  const s = await open('approval=1&actor=张工', { w: 820, h: 500, name: 'approval-signed' })
  check('署了名就写明以谁的名义', (await s.page.locator('body').innerText()).includes('将以「张工」签批'))
  await s.page.close()
})

await section('画布右栏：运行视图', async () => {
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
    return req.method() === 'GET' ? route.continue() : json(route, { detail: '这次运行已不在执行中（可能刚结束，或服务重启过），无需停止。请刷新页面查看最新状态' }, 409)
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
    /≥\s?916 token|≥916 token/.test(await panel.locator('[aria-label="这次运行的用量"]').innerText()),
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
  // 以前 streaming 优先：等人时标题写「正在执行…」，工具栏还给一个点了必然 409 的「停止」。
  // 运行中的标题现在写「运行中」，只拦「执行」的话，退回 streaming 优先也拦不住，两个都拦
  check('等待审批时：标题写「等待审批」，不写「正在执行」「运行中」', waitingHead.includes('等待审批') && !waitingHead.includes('执行')
    && !waitingHead.includes('运行中'), waitingHead.replace(/\n/g, ' '))
  check('等待审批时：不给停止，给「去审批」', await panel.getByRole('button', { name: '停止这次运行' }).count() === 0
    && await panel.getByRole('button', { name: '去审批' }).count() === 1)
  // 审批列表 4 秒才轮询一次：卡片得是 run.interrupted 一到就去取的，不能碰运气等下一拍
  const fetched = approvalHits.filter((t) => t >= fedAt && t - fedAt < 300)
  check('等待审批时：一停下来就去取审批卡（不等 4 秒轮询）', fetched.length > 0 && await panel.locator('[data-approval]').count() === 1,
    `停下后 ${fetched.map((t) => t - fedAt).join(',') || '—'} ms 取过`)
  check('等待审批时：卡上写明将以谁的名义签批', (await panel.locator('[data-approval]').innerText()).includes('将以「张工」签批'))
  check('等待审批时：没有还在转的步骤', await panel.locator('[aria-busy="true"]').count() === 0)
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/stream-panel-waiting-dark.png` })

  pending = false
  await feed(FINISH)
  await page.waitForTimeout(600)
  const doneHead = await head()
  const footer = await panel.locator('[aria-label="这次运行的用量"]').innerText()
  check('运行结束：头部写已完成和执行时长', doneHead.includes('已完成') && doneHead.includes('11.0 s'), doneHead.replace(/\n/g, ' '))
  check('运行结束：底栏换成服务端累计，不带 ≥', footer.includes('5.4k token') && footer.includes('$0.032') && !footer.includes('≥'), footer.replace(/\n/g, ' '))
  check('运行结束：执行时长和等待审批分开写', footer.includes('执行 11.0 s') && footer.includes('等待审批 9.5 s'), footer.replace(/\n/g, ' '))
  // 审批列表 4 秒才轮询一次：跑完了卡片不能还挂在那儿等人点
  check('运行结束：审批卡不再挂着', await panel.locator('[data-approval]').count() === 0)
  check('继续运行是谁发起的写在分段线上', (await panel.locator('[data-phase-mark]').allInnerTexts()).some((t) => t.includes('张工')))
  check('审批留痕写签批人，不写「你」', (await panel.innerText()).includes('→ 张工 已批准'))

  // 老后端的历史运行（runfx-3 终验）：usage 里没有 active_ms，duration_ms 只记了审批恢复后的
  // 最后一段。栏头、底栏得和航迹一样按事件算执行时长，不写「22 ms」
  const OLD = [...HEAD, ...WAIT, ...FINISH.slice(0, -1), ev(23, 'run.finished', null,
    { output: { 结论: 'C 供应商交期最短' }, usage: { input_tokens: 4470, output_tokens: 946, cost_usd: 0.0322 }, duration_ms: 22 }, 20.5)]
  await page.evaluate((run) => {
    const s = window.__studio.getState()
    s.clearRun()
    window.__studio.setState({ run: { id: run, workflow_id: 'fx-stream', status: 'queued', input: {}, output: {}, error: null,
      usage: {}, run_class: 'exploratory', version: null }, streaming: true, unsubscribe: () => {} })
  }, RUN)
  await feed(OLD)
  await page.evaluate(() => {
    const s = window.__studio.getState()
    window.__studio.setState({ run: { ...s.run, status: 'succeeded', usage: { duration_ms: 22, input_tokens: 4470, output_tokens: 946, cost_usd: 0.0322 } } })
  })
  await page.waitForTimeout(500)
  const oldHead = (await head()).replace(/\n/g, ' ')
  const oldFoot = (await panel.locator('[aria-label="这次运行的用量"]').innerText()).replace(/\n/g, ' ')
  const active = await page.evaluate(() => {
    const t = window.__studio.getState().trace
    return t.drives.reduce((n, [a, b]) => n + ((b ?? a) - a), 0)
  })
  const said = oldFoot.match(/执行\s*([\d.]+ s)/)?.[1] ?? ''
  check('老运行：栏头、底栏不写最后一段的「22 ms」', !/22 ms/.test(oldHead) && !/22 ms/.test(oldFoot), `${oldHead} / ${oldFoot}`)
  check('老运行：执行时长按事件算（和航迹同一个数）', !!said && Math.abs(parseFloat(said) * 1000 - active) < 100 && oldHead.includes(said),
    `航迹 ${active} ms · 底栏 ${said} · 栏头 ${oldHead}`)
  check('画布右栏没有运行时报错', errors.length === 0, errors.join(' | '))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(200)
      await page.screenshot({ path: `${SHOTS}/stream-panel-finished-${theme}.png` })
    }
  }
  await ctx.close()
})

await section('助手栏：展开之前的轮次', async () => {
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
})

// ---------------------------------------------------------------- 第三波
/** 量一个 CSS 颜色表达式算出来的 rgb，跟元素的实际颜色比 */
const colorOf = (page, v) => page.evaluate((expr) => {
  const probe = document.createElement('span')
  probe.style.color = expr
  document.body.append(probe)
  const c = getComputedStyle(probe).color
  probe.remove()
  return c
}, v)
const count = (text, needle) => text.split(needle).length - 1
await section('编号列、追问、CSV', async () => {
  // attribute_group 的 '001'、factory_code 的 '1063' 长得像数：以前右对齐，还给出
  // 「按 attribute_group 从高到低排」这种说不通的追问
  const { page } = await open('syn=codes', { w: 1100, h: 500, name: 'codes' })
  const chips = await page.$$eval('button.chip', (b) => b.map((x) => x.innerText))
  check('追问拿"量"排序，不拿编号 / 分组', chips.some((c) => c.includes('按 output_qty 从高到低排序'))
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

})

await section('协作团队用完轮数：泳道、头部、报错（NI-5、REQ-3A-3）', async () => {
  for (const [w, dense, label] of [[1100, '0', '宽栏'], [380, '1', '窄栏']]) {
    const { page, errors } = await open(`syn=exhausted&dense=${dense}`, { w, h: 900, name: `exhausted-${dense === '1' ? 'dense' : 'wide'}` })
    const body = await page.locator('body').innerText()
    const lanes = await page.locator('[data-team-lanes]').innerText().catch(() => '')
    check(`${label}：泳道头说「用完 2 轮仍未完成」`, lanes.includes('用完 2 轮仍未完成'), lanes.split('\n')[0])
    check(`${label}：泳道点出未分派的成员`, lanes.includes('汇总员') && lanes.includes('未分派'), lanes.replace(/\n/g, ' ').slice(0, 120))
    check(`${label}：最后那次判定不画成第 3 轮`, !/第\s?3\s?轮/.test(lanes), lanes.replace(/\n/g, ' ').slice(0, 160))
    const cell = await page.evaluate(() => {
      const b = document.querySelector('[data-team-lanes] button[aria-label^="取数员 第 1 轮"]')
      return b ? getComputedStyle(b).backgroundColor : ''
    })
    check(`${label}：失败的成员格子是失败色`, !!cell && cell === await colorOf(page, 'var(--st-failed)'), cell)
    check(`${label}：判定那一行说「轮数用完 · 调度者判定：未完成」`, body.includes('轮数用完 · 调度者判定：未完成'))
    // 报错用 lib/explain 讲清为什么、怎么办，不再原样贴后端那一整句
    const alert = await page.locator('[data-turn] [role="alert"]').first().innerText().catch(() => '')
    check(`${label}：报错标题说人话`, alert.startsWith('协作团队用完 2 轮仍未完成'), alert.split('\n')[0])
    check(`${label}：报错给出怎么办`, alert.includes('最多轮数'), alert.replace(/\n/g, ' ').slice(0, 160))
    check(`${label}：没有运行时报错`, errors.length === 0, errors.join(' | '))
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    check(`${label}：不横向溢出`, overflow <= 0, `${overflow}px`)
    await page.close()
  }
  {
    const { page } = await open('syn=exhausted-degrade', { w: 1100, h: 900, name: 'exhausted-degrade' })
    const head = await page.locator('[data-turn] .sticky').first().innerText()
    check('降档交付：头部不是一个安静的「已完成」', head.includes('协作团队未完成'), head.replace(/\n/g, ' '))
    const lanes = await page.locator('[data-team-lanes]').innerText().catch(() => '')
    check('降档交付：泳道头说按降档交付', lanes.includes('按降档交付'), lanes.split('\n')[0])
    const body = await page.locator('body').innerText()
    check('降档交付：团队那一行说清是降档', body.includes('用完 2 轮仍未完成，按降档交付'))
    check('降档交付：提醒行给出下一步', body.includes('不可作为结论使用'))
    await page.close()
    const r = await open('syn=exhausted-closing&dense=1', { w: 380, h: 800, name: 'exhausted-closing' })
    const rb = await r.page.locator('body').innerText()
    check('最后那次判定进行中：写「调度者正在做最后判定」', rb.includes('轮数用完 · 调度者正在做最后判定'))
    check('最后那次判定进行中：不写「第 3 轮」', !rb.includes('第 3 轮'))
    const rl = await r.page.locator('[data-team-lanes]').innerText().catch(() => '')
    check('最后那次判定进行中：泳道也说正在判定', rl.includes('轮数用完 · 调度者正在判定'), rl.split('\n').slice(0, 2).join(' '))
    await r.page.close()
  }
})

await section('模型把工具调用写成文字、修复凑数、工具超时（NI-3/4）', async () => {
  const { page, errors } = await open('syn=markup&dense=1', { w: 380, h: 900, name: 'markup' })
  const body = await page.locator('body').innerText()
  const alert = await page.locator('[data-turn] [role="alert"]').first().innerText().catch(() => '')
  check('报错标题：模型未实际调用工具', alert.startsWith('模型未实际调用工具'), alert.split('\n')[0])
  check('报错说原因和怎么办', alert.includes('以文本形式输出了工具调用') && alert.includes('绑定'), alert.replace(/\n/g, ' '))
  check('原始标记不出现在正文里', !body.includes('DSML'))
  const row = await page.locator('[data-node-id="query"][data-step-status="failed"]').first().innerText().catch(() => '')
  check('失败的节点行说人话', row.includes('模型未实际调用工具'), row.replace(/\n/g, ' '))
  const warn = page.locator('[data-step-code="tool_markup_leak"]').first()
  const warnText = await warn.innerText().catch(() => '')
  check('提醒行说人话', warnText.includes('模型以文本形式输出了工具调用，未实际执行'), warnText.replace(/\n/g, ' '))
  check('下一步不用展开就看得到', await warn.locator('[data-step-next]').count() === 1
    && (await warn.locator('[data-step-next]').innerText()).includes('绑定'))
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
  await page.close()

  const rp = await open('syn=repair', { w: 1100, h: 900, name: 'repair' })
  const rbody = await rp.page.locator('body').innerText()
  // 两次修复凑出的是同一个值：并成一行「×2」，原因照样写在行上；值不同时分行（见 check-decode）
  check('每次修复都算上、说出凑出来的值', rbody.includes('让模型修复格式') && /×2/.test(rbody) && rbody.includes('total_count=0'),
    rbody.split('\n').filter((l) => l.includes('修复') || l.includes('×')).join(' | '))
  const ralert = await rp.page.locator('[data-turn] [role="alert"]').first().innerText().catch(() => '')
  check('报错：校验修复未采用', ralert.startsWith('校验修复未采用'), ralert.split('\n')[0])
  await rp.page.close()

  const tl = await open('syn=timeout-live&dense=1', { w: 380, h: 700, name: 'timeout-live' })
  const over = await tl.page.evaluate(() => {
    const row = document.querySelector('[aria-busy="true"][data-step-status="running"]')
    const clock = row?.querySelector('[data-over-limit]')
    return { text: clock?.textContent ?? '', color: clock ? getComputedStyle(clock).color : '' }
  })
  check('进行中的查询越过时限要说破', over.text.includes('已超出 30 秒上限'), over.text)
  check('越过时限的秒表是提醒色', over.color === await colorOf(tl.page, 'var(--st-waiting)'), over.color)
  await tl.page.close()
  const td = await open('syn=timeout', { w: 1100, h: 700 })
  check('超时的查询写明超了多少上限', (await td.page.locator('body').innerText()).includes('超过 30 秒上限，已停止等待'))
  await td.page.close()
})

await section('放弃、结构化的报错、恢复的轮次、出具横幅（REQ-1/5/6、REQ-3A-2）', async () => {
  const { page } = await open('syn=abandoned', { w: 1100, h: 600, name: 'abandoned' })
  const mark = await page.locator('[data-phase-mark]').allInnerTexts()
  check('放弃的运行：分段线说清谁放弃的、一并关了什么', mark.some((t) => t.includes('已取消') && t.includes('张工')
    && t.includes('1 条待审批一并关闭')), mark.join(' | '))
  await page.close()

  const s = await open('syn=structured', { w: 1100, h: 600, name: 'structured' })
  const alert = await s.page.locator('[data-turn] [role="alert"]').first().innerText()
  // 以前页面拆好的「操作超时」又被翻译一遍：「操作超时 / 操作超时：查询超时…」
  check('拆好的报错照原样画，标题不重复', count(alert, '操作超时') === 1 && alert.startsWith('操作超时'), alert.replace(/\n/g, ' '))
  check('原因、怎么办各一行', alert.includes('查询超时：数据库 90 秒没有返回') && alert.includes('缩小时间范围'))
  check('原文收进技术细节', !alert.includes('OperationalError') && alert.includes('技术细节'))
  await s.page.close()

  const r = await open('syn=restored', { w: 1100, h: 600 })
  const head = await r.page.locator('[data-turn] .sticky').first().innerText()
  check('恢复的轮次：头部照样写几次查询', head.includes('3 次查询'), head.replace(/\n/g, ' '))
  await r.page.close()

  // 降档是因为查询没通过 SQL 检查（B5）：横幅单列一块写是哪次查询、什么问题、影响了哪些引用；那一句不在其余缺口里重复
  const q = await open('syn=issued-sql', { w: 1100, h: 900, name: 'issued-sql' })
  const banner = q.page.locator('[data-issuance-banner=degraded]')
  const sql = (await banner.locator('[data-issuance-sql-checks]').innerText().catch(() => '')).replace(/\s+/g, ' ')
  check('SQL 检查导致的降档单列一块：写降档原因、哪次查询、问题、受影响的引用、下一步', sql.includes('降档原因：所依据的查询未通过 SQL 检查')
    && sql.includes('查询「订单金额查询」（Q1）') && sql.includes('求和会重复计算') && sql.includes('受影响：指标「订单金额」、Q1 第 1 行「gmv」')
    && sql.includes('修改来源查询的 SQL'), sql)
  const rest = (await banner.locator('[data-issuance-gaps]').innerText().catch(() => '')).replace(/\s+/g, ' ')
  check('……那一句不在「校验未全部完成」里重复，其余缺口照列', !rest.includes('未通过 SQL 检查') && rest.includes('协作团队'), rest)
  await q.page.close()

  const i = await open('syn=issued', { w: 1100, h: 900 })
  check('出具横幅带 data-issuance-banner（画布印章据此滚过来）', await i.page.locator('[data-issuance-banner]').count() === 1)
  const live = await i.page.locator('[data-turn] [role="status"][aria-live="polite"]').first().innerText()
  check('读屏也播报出具档位', live.includes('降档出具'), live)
  await i.page.close()

  const c = await open('syn=tools-dropped&dense=1', { w: 380, h: 700, name: 'tools-dropped' })
  const cb = await c.page.locator('body').innerText()
  check('改图时工具被全部移除要单独说', cb.includes('「数据查询」的工具已全部移除'))
  check('自查通过但工具被移除，自查那行不是安静的通过', cb.includes('自查通过，但有 1 处工具被移除'))
  await c.page.close()

  // 按口径卡清点回指的数字（3C REQ-10）：出处是 matched[].caliber「口径名 @ 版本」，和后端 io.py
  // 同一种拼法。拼法一变，横幅就悄悄只剩「可追溯 3 个数字」，说不出哪个来自哪张卡
  const k = await open('syn=calibers', { w: 1100, h: 800, name: 'calibers' })
  const rows = await k.page.locator('[data-issuance-banner] [data-caliber]').evaluateAll((els) =>
    els.map((e) => [e.getAttribute('data-caliber'), e.textContent.replace(/\s+/g, ' ')]))
  check('两张口径卡各数各的：订单口径可追溯 2 个、退款口径可追溯 1 个',
    rows.length === 2 && rows[0][0] === '订单口径' && rows[0][1].includes('可追溯 2 个数字')
      && rows[1][0] === '退款口径' && rows[1][1].includes('可追溯 1 个数字'), JSON.stringify(rows))
  check('两张卡时不说「均来自此口径卡」', !rows.some(([, t]) => t.includes('均来自此口径卡')))
  await k.page.close()
  const one = await open('syn=caliber-one', { w: 1100, h: 800 })
  const oneRow = await one.page.locator('[data-issuance-banner] [data-caliber]').allInnerTexts()
  check('只有一张卡、逐个出处都指向它：写「均来自此口径卡」', oneRow.length === 1
    && oneRow[0].includes('可追溯的 2 个数字均来自此口径卡'), oneRow.join(' | '))
  await one.page.close()
})

await section('吸顶的轮次头贴着顶边（REQ-8）', async () => {
  for (const [q, w, h, label] of [['syn=mixed', 1024, 768, '宽栏 1024'], ['syn=mixed', 1180, 800, '宽栏 1180'],
                                  ['syn=mixed&dense=1', 380, 700, '窄栏']]) {
    const { page } = await open(q, { w, h })
    const gap = await page.evaluate(() => {
      const sc = document.querySelector('[data-stream-scroll]')
      sc.scrollTop = Math.min(sc.scrollHeight - sc.clientHeight, 260)
      sc.dispatchEvent(new Event('scroll'))
      const head = document.querySelector('[data-turn] .sticky')
      const s = sc.getBoundingClientRect()
      const hd = head.getBoundingClientRect()
      // 顶边往下 1px 那一行像素：落在头上才算没缝
      const hit = document.elementFromPoint(s.left + s.width / 2, s.top + 1)
      return { gap: Math.round(hd.top - s.top), onHead: !!hit && head.contains(hit), scrolled: sc.scrollTop }
    })
    check(`${label}：往下翻后轮次头贴着滚动区顶边，上面不露正文`, gap.scrolled > 0 && gap.gap === 0 && gap.onHead, JSON.stringify(gap))
    await page.close()
  }
})

await section('画布右栏：去审批、放弃、出具横幅、回执（REQ-1/3/4、REQ-3A-2）', async () => {
  const WF_ID = 'fx-stream3'
  const RUN = 'fxstream0003'
  const GRAPH = {
    nodes: [
      { id: 'in', type: 'input', position: { x: 0, y: 0 }, data: { label: '问题', config: { fields: [{ name: 'q' }] } } },
      { id: 'review', type: 'human', position: { x: 300, y: 0 }, data: { label: '主管审批', config: { mode: 'approve' } } },
      { id: 'out', type: 'output', position: { x: 620, y: 0 }, data: { label: '成果', config: {} } },
      { id: 'query', type: 'agent', position: { x: 300, y: 200 }, data: { label: '数据查询', config: { tools: [] } } },
    ],
    edges: [{ id: 'e1', source: 'in', target: 'review' }, { id: 'e2', source: 'review', target: 'out', sourceHandle: 'approved' }],
  }
  const WF = { id: WF_ID, name: '右栏检查三', description: '', graph: GRAPH, tags: [], version: 1, status: 'draft',
    published_version: null, run_count: 0, created_at: '2026-09-26T00:00:00Z', updated_at: '2026-09-26T00:00:00Z' }
  const APPROVAL = { id: 'ap-3', run_id: RUN, node_id: 'review', mode: 'approve', title: '这份报表可以发出吗？',
    payload: { message: '华东 1,204 单' }, status: 'pending', response: {}, created_at: new Date(Date.now() - 60_000).toISOString(),
    workflow_name: '右栏检查三', node_label: '主管审批', run_class: 'exploratory' }
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: 'dark' })
  await ctx.addInitScript(() => { try { localStorage.setItem('agentlab_actor', '张工') } catch { /* 隐私窗口 */ } })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  let pending = true
  let status = 'interrupted'
  const cancels = []
  await page.route(/\/api\/workflows(\?.*)?$/, async (route) => {
    if (route.request().method() !== 'GET') return json(route, { detail: '检查脚本不写库' }, 409)
    const real = await (await route.fetch()).json().catch(() => [])
    return json(route, [WF, ...(Array.isArray(real) ? real : [])])
  })
  await page.route(/\/api\/workflows\/fx-stream3(\/.*)?(\?.*)?$/, (route) =>
    route.request().method() === 'GET' ? json(route, WF) : json(route, { detail: '检查脚本不写库' }, 409))
  await page.route(/\/api\/conversations(\/.*)?(\?.*)?$/, (route) =>
    route.request().method() === 'GET' ? json(route, []) : json(route, { id: 'fx-conv3', kind: 'canvas', title: '', turns: [] }))
  await page.route(/\/api\/approvals(\/.*)?(\?.*)?$/, (route) =>
    route.request().method() !== 'GET' ? json(route, { detail: '检查脚本不写库' }, 409) : json(route, pending ? [APPROVAL] : []))
  await page.route(/\/api\/runs(\/.*)?(\?.*)?$/, (route) => {
    const req = route.request()
    if (req.method() === 'POST' && req.url().includes(`${RUN}/cancel`)) {
      cancels.push(req.headers()['x-actor'] ?? '')
      return json(route, { ok: true, status: 'cancelled' })
    }
    if (req.method() === 'GET' && req.url().includes(RUN)) {
      return json(route, { id: RUN, workflow_id: WF_ID, status, input: {}, output: {}, error: null, usage: {}, run_class: 'exploratory' })
    }
    return req.method() === 'GET' ? route.continue() : json(route, { detail: '检查脚本不写库' }, 409)
  })
  await page.goto(`${WEB}/studio/${WF_ID}`, { waitUntil: 'networkidle' })
  await page.waitForFunction((id) => window.__studio?.getState().workflow?.id === id, WF_ID, { timeout: 15000 })
  const now = Date.now() / 1000
  const ev = (seq, type, node_id, data = {}, t = 0) => ({ seq, type, node_id, data, ts: now - 30 + t })
  const WAIT = [
    ev(1, 'run.started', null, { nodes: 3 }, 0),
    ev(2, 'node.started', 'in', { node_type: 'input', label: '问题' }, 0.1),
    ev(3, 'node.finished', 'in', { duration_ms: 3, preview: { q: 'x' } }, 0.2),
    ev(4, 'node.started', 'review', { node_type: 'human', label: '主管审批' }, 0.3),
    ev(5, 'human.requested', 'review', { kind: 'human_node', node_id: 'review', mode: 'approve', title: '这份报表可以发出吗？' }, 0.4),
    ev(6, 'run.interrupted', 'review', { payload: { node_id: 'review', title: '这份报表可以发出吗？' } }, 0.5),
  ]
  const feed = (list) => page.evaluate((l) => { const s = window.__studio.getState(); for (const e of l) s.applyEvent(e) }, list)
  await page.evaluate((run) => window.__studio.setState({ run: { id: run, workflow_id: 'fx-stream3', status: 'queued', input: {},
    output: {}, error: null, usage: {}, run_class: 'exploratory', version: null }, streaming: true, unsubscribe: () => {} }), RUN)
  await feed(WAIT)
  await page.waitForTimeout(600)
  const panel = page.locator('.sheet-in').first()
  check('等待审批时：栏头「去审批」旁边有「放弃这次运行」', await panel.getByRole('button', { name: '放弃这次运行' }).count() === 1)

  // 胶囊的「去审批」在右栏处于对话层时派发 agentlab:goto-approval：右栏得自己切过去、把焦点放进审批卡
  await panel.getByRole('button', { name: /助手/ }).first().click()
  await page.waitForTimeout(300)
  check('回到对话层后运行层收起', await page.locator('.sheet-in').count() === 0)
  // 右栏条和胶囊同一套说法：停在谁、等了多久
  const strip = await page.locator('[data-run-strip]').innerText().catch(() => '')
  check('对话层的运行条说停在哪个节点', strip.includes('主管审批'), strip.replace(/\n/g, ' '))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(200)
      await page.locator('[data-assistant-panel]').screenshot({ path: `${SHOTS}/stream-panel-strip-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
  // 右栏收起时它还挂着（只是藏起来）：那时不接手，让胶囊自己兜底，别切到一层没人看得见的运行层
  const unhandled = await page.evaluate((run) => {
    const aside = document.querySelector('[data-assistant-panel]').closest('aside')
    aside.style.visibility = 'hidden'
    const passed = window.dispatchEvent(new CustomEvent('agentlab:goto-approval', { detail: { runId: run }, cancelable: true }))
    aside.style.visibility = ''
    return passed
  }, RUN)
  await page.waitForTimeout(200)
  check('右栏收起时不接手 goto-approval', unhandled && await page.locator('.sheet-in').count() === 0)
  await page.evaluate((run) => window.dispatchEvent(new CustomEvent('agentlab:goto-approval', { detail: { runId: run, nodeId: 'review' } })), RUN)
  await page.waitForTimeout(900)
  const focusIn = await page.evaluate(() => !!document.activeElement?.closest('[data-approval]'))
  check('goto-approval：切到运行层', await page.locator('.sheet-in').count() === 1)
  check('goto-approval：焦点落在审批卡里', focusIn)
  check('goto-approval：审批卡描了一圈', await page.locator('[data-approval].sf-flash').count() === 1)
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/stream-panel-goto-approval-dark.png` })

  // 放弃：确认之后调 cancel，带着署名；事件流过来就收成「已取消」，说清是谁放弃的
  await page.locator('.sheet-in').first().getByRole('button', { name: '放弃这次运行' }).click()
  await page.waitForTimeout(300)
  const dialog = await page.locator('[role="dialog"]').innerText().catch(() => '')
  check('放弃前先确认，说清后果', dialog.includes('待审批') && dialog.includes('无法继续运行'), dialog.replace(/\n/g, ' ').slice(0, 160))
  await page.locator('[role="dialog"]').getByRole('button', { name: '放弃这次运行' }).click()
  await page.waitForTimeout(400)
  check('放弃：调了 cancel，带着署名', cancels.length === 1 && decodeURIComponent(cancels[0]) === '张工', JSON.stringify(cancels))
  pending = false
  status = 'cancelled'
  await feed([ev(7, 'run.cancelled', null, { timing: { wall_ms: 30000, active_ms: 500, wait_ms: 29500 }, actor: '张工',
    message: '放弃了这次运行，1 条待审批一并关闭' }, 30), ev(8, 'stream.end', null, { status: 'cancelled' }, 30)])
  await page.waitForTimeout(500)
  const head = await page.locator('.sheet-in [data-turn] .sticky').first().innerText().catch(() => '')
  check('放弃：头部写已取消', head.includes('已取消'), head.replace(/\n/g, ' '))
  const marks = await page.locator('.sheet-in [data-phase-mark]').allInnerTexts()
  check('放弃：分段线写清谁放弃、一并关了什么', marks.some((t) => t.includes('张工') && t.includes('一并关闭')), marks.join(' | '))
  check('放弃之后不再给「去审批」和「放弃」', await page.locator('.sheet-in').getByRole('button', { name: /去审批|放弃这次运行/ }).count() === 0)
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(200)
      await page.screenshot({ path: `${SHOTS}/stream-panel-abandoned-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }

  // 出具横幅：画布上的印章派发 agentlab:goto-issuance；右栏在对话层时先切过去再滚到横幅
  await page.evaluate((run) => {
    const s = window.__studio.getState()
    s.clearRun()
    window.__studio.setState({ run: { id: run, workflow_id: 'fx-stream3', status: 'succeeded', input: {}, output: {
      结论: '华东 1,204 单', _issuance: { tier: 'degraded', calibers: [], metrics_checked: 1, matched_numbers: 0,
        unmatched_numbers: [{ token: '1,204', context: '华东 1,204 单' }], missing_required: [], missing_expected: [], gaps: [] } },
      error: null, usage: {}, run_class: 'exploratory', version: null }, streaming: false, unsubscribe: () => {} })
  }, RUN)
  await feed([ev(1, 'run.started', null, { nodes: 3 }, 0), ev(2, 'node.started', 'out', { node_type: 'output', label: '成果' }, 0.1),
    ev(3, 'issuance', 'out', { tier: 'degraded', unmatched: 1, gaps: [], metrics_checked: 1, matched_numbers: 0 }, 0.2),
    ev(4, 'node.finished', 'out', { duration_ms: 2, preview: {} }, 0.3),
    ev(5, 'run.finished', null, { output: { 结论: '华东 1,204 单', _issuance: { tier: 'degraded', calibers: [], metrics_checked: 1,
      matched_numbers: 0, unmatched_numbers: [{ token: '1,204', context: '华东 1,204 单' }], missing_required: [], missing_expected: [], gaps: [] } },
      usage: {}, duration_ms: 400, timing: { wall_ms: 400, active_ms: 400, wait_ms: 0 } }, 0.4)])
  await page.waitForTimeout(400)
  await page.locator('.sheet-in').first().getByRole('button', { name: /助手/ }).first().click()
  await page.waitForTimeout(300)
  await page.evaluate((run) => window.dispatchEvent(new CustomEvent('agentlab:goto-issuance', { detail: { runId: run } })), RUN)
  await page.waitForTimeout(900)
  const banner = await page.evaluate(() => {
    const b = document.querySelector('.sheet-in [data-issuance-banner]')
    const s = b?.closest('[data-stream-scroll]')?.getBoundingClientRect()
    const r = b?.getBoundingClientRect()
    return { found: !!b, inView: !!r && !!s && r.top >= s.top - 1 && r.top < s.bottom, focused: !!b && document.activeElement === b,
             flash: b?.dataset.flash ?? null, outline: b ? getComputedStyle(b).outlineStyle : null }
  })
  check('goto-issuance：切到运行层、横幅在视野里', banner.found && banner.inView, JSON.stringify(banner))
  // 焦点带过去：读屏和键盘用户从画布那头被带过来，得落在横幅上，而不是留在画布的印章上
  check('goto-issuance：焦点落在出具横幅上', banner.focused)
  // 描边得真的画出来（3C REQ-7）：横幅带 outline-none，data-[flash]:outline 读的是被它设成 none 的变量，
  // 以前 data-flash 挂上了、焦点也到了，算出来却是 outline: none
  check('goto-issuance：横幅那一下描边真的画出来了', banner.flash === 'focus' && banner.outline !== 'none',
    `${banner.flash} ${banner.outline}`)

  // 请求办完就清掉（3C REQ-8）：回到对话层、焦点放进输入框，再点运行条打开运行层，
  // 旧的 goto-issuance 不能再办一遍——又滚走、又抢焦点、又描边
  await page.locator('.sheet-in').first().getByRole('button', { name: /助手/ }).first().click()
  await page.waitForTimeout(300)
  await page.locator('[data-assistant-panel] textarea').first().focus()
  await page.locator('[data-run-strip]').click()
  await page.waitForTimeout(900)
  const again = await page.evaluate(() => {
    const b = document.querySelector('.sheet-in [data-issuance-banner]')
    return { opened: !!document.querySelector('.sheet-in'), stolen: !!b && document.activeElement === b, flash: b?.dataset.flash ?? null }
  })
  check('点运行条重新打开运行层：焦点不被旧请求抢到横幅上，也不再描边', again.opened && !again.stolen && !again.flash,
    JSON.stringify(again))

  // 回执：看过了没改（unchanged）不说「已更新画布」；修过几处数不出来时不写「几 处」
  await page.locator('.sheet-in').first().getByRole('button', { name: /助手/ }).first().click()
  await page.evaluate(() => {
    const s = window.__studio.getState()
    s.clearRun()
    const base = { explanation: '', error: '', phase: 'done' }
    window.__studio.setState({ copilotTurns: [
      { ...base, id: 'u1', instruction: '检查一下有没有问题', outcome: 'unchanged',
        ops: [{ op: 'done', explanation: '' }, { op: 'check', status: 'passed', repaired: 0 }, { op: 'final', graph: { nodes: [{ id: 'in' }] } }] },
      { ...base, id: 'u2', instruction: '把循环条件改对', outcome: 'applied', diff: { added: [], changed: ['review'], removed: [], total: 1 },
        ops: [{ op: 'update_node', id: 'review' }, { op: 'check', status: 'passed', repaired: 1 }, { op: 'final', graph: { nodes: [{ id: 'in' }] } }] },
      { ...base, id: 'u4', instruction: '最后加一步导出表格', outcome: 'unchanged',
        ops: [{ op: 'add_node', node: { id: 'x', type: 'sheet_export', label: '导出表格' } }, { op: 'done', explanation: '' },
          { op: 'final', graph: { nodes: [{ id: 'in' }] }, issues: [{ level: 'warning', node_id: null, code: 'unknown_node_type',
            type: 'sheet_export', message: '模型写了一个不存在的节点类型「sheet_export」，这一步已跳过' }] }] },
    ] })
  })
  await page.waitForTimeout(400)
  const turns = await page.locator('[data-turn]').allInnerTexts()
  check('unchanged：说「已检查，画布无需修改」', turns[0]?.includes('已检查，画布无需修改')
    && !turns[0]?.includes('已更新画布'), turns[0]?.split('\n').slice(0, 3).join(' '))
  check('修过但数不出几处：不写「几 处」', turns[1]?.includes('自查发现的问题已自动修正') && !turns[1]?.includes('几 处'),
    turns[1]?.split('\n').slice(0, 3).join(' '))
  // 只加了认不出类型的节点、全被跳过：画布一处没变，不能说「已应用到画布」（3C REQ-9）
  const skippedHead = await page.locator('[data-turn="u4"] .sticky').innerText().catch(() => '')
  check('全被跳过的轮次：写「画布未修改：有 1 个步骤未能添加」', skippedHead.includes('画布未修改：有 1 个步骤未能添加')
    && !skippedHead.includes('已应用到画布'), skippedHead.replace(/\n/g, ' '))
  // 画布一处没变就没有可撤的：给了「撤销这次生成」，撤掉的是这一轮之前的那一步
  check('画布没变的轮次不给「撤销这次生成」', await page.getByRole('button', { name: '撤销这次生成' }).count() === 0
    && await page.getByRole('button', { name: /让助手补全/ }).count() === 1)

  // 改图时模型漏写了 tools：回执逐项列出去掉了什么；自查是通过的，「让助手再修」不管用，撤销是首选
  const dropped = { level: 'warning', node_id: 'query', code: 'tools_dropped', field: 'tools',
    message: '「数据查询」的工具从 db_query__shop 变为无。本轮要求中没有提到移除工具，请确认是否误删：没有绑定工具时，它无法查询数据库，只能假设调用结果' }
  await page.evaluate((warn) => {
    window.__studio.setState({ copilotTurns: [{ explanation: '', error: '', phase: 'done', id: 'u3', instruction: '只查上月的数据',
      outcome: 'applied', diff: { added: [], changed: ['query'], removed: [], total: 1 },
      ops: [{ op: 'update_node', id: 'query', label: '数据查询', config: { system: '只查上月' } },
        { op: 'check', status: 'passed', repaired: 0, warnings: [warn] },
        { op: 'final', graph: { nodes: [{ id: 'in' }, { id: 'query' }] }, issues: [warn], tool_changes: [{ node_id: 'query',
          label: '数据查询', member: null, field: 'tools', before: ['db_query__shop'], after: [], added: [], removed: ['db_query__shop'] }] }] }] })
  }, dropped)
  await page.waitForTimeout(400)
  const loss = await page.locator('[data-tool-changes]').innerText().catch(() => '')
  check('回执逐项列出被去掉的工具', loss.includes('「数据查询」去掉了') && loss.includes('db_query__shop') && loss.includes('当前没有任何工具'),
    loss.replace(/\n/g, ' '))
  const head3 = await page.locator('[data-turn="u3"] .sticky').innerText().catch(() => '')
  check('回执头部是提醒，写清几处工具被移除', head3.includes('未经要求移除了 1 处工具'), head3.replace(/\n/g, ' '))
  check('只丢了工具时不给「让助手再修」，撤销是主按钮',
    await page.getByRole('button', { name: /让助手/ }).count() === 0
    && await page.locator('button.btn-primary', { hasText: '撤销这次生成' }).count() === 1)
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(200)
      await page.locator('[data-assistant-panel]').screenshot({ path: `${SHOTS}/stream-panel-tools-lost-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }

  // 「定位」被去掉的工具：光标落在那个节点工具选择器的「添加」上，回车就能加回去，
  // 不会落在别的工具的「移除」上（3C REQ-6 / REQ-14）
  await page.locator('[data-tool-changes]').getByRole('button', { name: /定位/ }).first().click()
  await page.waitForTimeout(500)
  const toolFocus = await page.evaluate(() => {
    const a = document.activeElement
    return { sel: window.__studio.getState().selectedId, inSheet: !!a?.closest('[data-inspector-sheet]'),
             field: a?.closest('[data-field]')?.getAttribute('data-field') ?? null,
             text: (a?.getAttribute('aria-label') || a?.textContent || '').trim().slice(0, 20) }
  })
  check('定位被去掉的工具：打开这个节点，光标在工具那一栏的「添加」上',
    toolFocus.sel === 'query' && toolFocus.inSheet && toolFocus.field === 'tools' && /添加/.test(toolFocus.text)
      && !/^(移除|删除)/.test(toolFocus.text), JSON.stringify(toolFocus))
  await page.evaluate(() => window.__studio.getState().select(null))
  await page.waitForTimeout(200)

  // 同一轮里一处是没让删的（后端点了名）、一处是按要求删的：卡片和改图回执一样分两组，
  // 卡头只为没让删的那处提醒（3C REQ-10）
  const unaskedWarn = { level: 'warning', node_id: 'query', code: 'tools_dropped', field: 'tools',
    message: '「数据查询」的工具从 db_query__shop 变为无。本轮要求中没有提到移除 db_query__shop，请确认是否误删：没有绑定工具时，它无法查询数据库，只能假设调用结果' }
  const splitTurn = (id, warnings, changes) => ({ explanation: '', error: '', phase: 'done', id, instruction: '汇总那一步别再查库了',
    outcome: 'applied', diff: { added: [], changed: changes.map((c) => c.node_id), removed: [], total: changes.length },
    ops: [{ op: 'update_node', id: 'query', config: { system: '只查上月' } },
      { op: 'check', status: 'passed', repaired: 0, warnings },
      { op: 'final', graph: { nodes: [{ id: 'in' }, { id: 'query' }] }, issues: warnings, tool_changes: changes }] })
  const lossQuery = { node_id: 'query', label: '数据查询', member: null, field: 'tools', before: ['db_query__shop'], after: [], added: [], removed: ['db_query__shop'] }
  const lossSum = { node_id: 'summary', label: '汇总', member: null, field: 'tools', before: ['db_query__shop', 'python_exec'], after: ['python_exec'], added: [], removed: ['db_query__shop'] }
  await page.evaluate((t) => window.__studio.setState({ copilotTurns: [t] }), splitTurn('u5', [unaskedWarn], [lossQuery, lossSum]))
  await page.waitForTimeout(400)
  const head5 = await page.locator('[data-turn="u5"] .sticky').innerText().catch(() => '')
  const unaskedText = await page.locator('[data-tool-loss="unasked"]').innerText().catch(() => '')
  const askedText = await page.locator('[data-tool-loss="asked"]').innerText().catch(() => '')
  check('卡头只数未经要求移除的那处', head5.includes('未经要求移除了 1 处工具') && !head5.includes('2 处'), head5.replace(/\n/g, ' '))
  // 360px 的卡头放得下「未经要求移除」这几个字；万一截断了，悬停也读得到整句（3C 返工 D5）
  const status5 = await page.locator('[data-turn="u5"] [data-turn-status]').evaluate((el) => {
    // 「未经要求移除」最后一个字的右缘在框内：截断的省略号只能落在它后面
    const node = el.firstChild
    const at = node?.textContent?.indexOf('未经要求移除') ?? -1
    let right = Infinity
    if (at >= 0) {
      const r = document.createRange()
      r.setStart(node, at + 5)
      r.setEnd(node, at + 6)
      right = r.getBoundingClientRect().right
    }
    const box = el.getBoundingClientRect()
    return { title: el.getAttribute('title') ?? '', visible: right <= box.right + 0.5, width: Math.round(box.width) }
  }).catch(() => ({ title: '', visible: false, width: 0 }))
  check('卡头的「未经要求移除」在 360px 里看得见，整句在悬停里', status5.visible && status5.title.includes('未经要求移除了 1 处工具'),
    JSON.stringify(status5))
  check('未经要求删除的一组在前，写明请确认是否误删', unaskedText.includes('未要求删除') && unaskedText.includes('「数据查询」')
    && !unaskedText.includes('「汇总」'), unaskedText.replace(/\n/g, ' '))
  check('按要求删的另列一组', askedText.includes('按要求') && askedText.includes('「汇总」') && !askedText.includes('「数据查询」'),
    askedText.replace(/\n/g, ' '))
  check('有未经要求移除的：撤销仍是主按钮', await page.locator('button.btn-primary', { hasText: '撤销这次生成' }).count() === 1)
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(200)
      await page.locator('[data-assistant-panel]').screenshot({ path: `${SHOTS}/stream-panel-tools-split-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
  // 全是按要求删的：卡头不提醒，撤销退成次要
  await page.evaluate((t) => window.__studio.setState({ copilotTurns: [t] }), splitTurn('u6', [], [lossSum]))
  await page.waitForTimeout(400)
  const head6 = await page.locator('[data-turn="u6"] .sticky').innerText().catch(() => '')
  check('全是按要求删的：卡头不说「未经要求移除」，只列在「按要求」底下', !head6.includes('未经要求移除')
    && await page.locator('[data-tool-loss="asked"]').count() === 1 && await page.locator('[data-tool-loss="unasked"]').count() === 0,
    head6.replace(/\n/g, ' '))
  check('全是按要求删的：撤销不是主按钮', await page.locator('button.btn-primary', { hasText: '撤销这次生成' }).count() === 0
    && await page.getByRole('button', { name: '撤销这次生成' }).count() === 1)

  // 自查没修好的问题带着落点（field）：「定位」把光标放进那一栏的输入框
  await page.evaluate(() => window.__studio.setState({ copilotTurns: [{ explanation: '', error: '', phase: 'done', id: 'u7',
    instruction: '让查询多想几步', outcome: 'applied', diff: { added: [], changed: ['query'], removed: [], total: 1 },
    ops: [{ op: 'update_node', id: 'query', config: { max_steps: 0 } },
      { op: 'check', status: 'failed', issues: [{ level: 'error', node_id: 'query', edge_id: null, field: 'max_steps',
        message: '最大步数要在 1 到 100 之间' }] },
      { op: 'final', graph: { nodes: [{ id: 'in' }, { id: 'query' }] } }] }] }))
  await page.waitForTimeout(400)
  await page.locator('[data-issue-list]').getByRole('button', { name: /定位/ }).first().click()
  await page.waitForTimeout(500)
  const issueFocus = await page.evaluate(() => {
    const a = document.activeElement
    return { sel: window.__studio.getState().selectedId, tag: a?.tagName ?? null,
             field: a?.closest('[data-inspector-sheet] [data-field]')?.getAttribute('data-field') ?? null }
  })
  check('定位自查问题：光标落进出问题的那一栏', issueFocus.sel === 'query' && issueFocus.field === 'max_steps'
    && issueFocus.tag === 'INPUT', JSON.stringify(issueFocus))
  await page.evaluate(() => window.__studio.getState().select(null))
  await page.waitForTimeout(200)

  // 对照数据目录的 SQL 检查（数据目录阶段 4B）：自查没修好的错误、不挡运行的提醒各一组，写中文规则名、级别、表和列、
  // SQL 片段，不露规则编号；「定位」落到调用工具参数里的 SQL，光标放在 sql 的值开头
  const SQL = 'SELECT o.region, SUM(o.amount) AS amt\nFROM orders o JOIN order_items i ON i.order_id = o.id\nGROUP BY o.region'
  await page.evaluate((sql) => {
    const st = window.__studio.getState()
    window.__studio.setState({ nodes: [...st.nodes, { id: 'fetch', type: 'card', position: { x: 620, y: 200 },
      data: { nodeType: 'tool', label: '订单汇总', config: { tool: 'db_query__shop', args: { limit: 100, sql } } } }] })
  }, SQL)
  const sqlErr = { level: 'error', node_id: 'fetch', edge_id: null, field: 'args.sql', code: 'fanout_sum', table: 'orders', column: 'amount',
    sql_excerpt: 'SUM(o.amount)', message: '「订单」关联「订单明细」是一对多，对「订单」的「订单金额」求和会重复计算。请先按订单汇总明细再关联' }
  const sqlWarn = { level: 'warning', node_id: 'fetch', edge_id: null, field: 'args.sql', code: 'missing_valid_filter', table: 'orders',
    message: '「订单」定义了有效记录条件「status <> 9」，查询中没有按它筛选' }
  await page.evaluate(([err, warn]) => window.__studio.setState({ copilotTurns: [{ explanation: '', error: '', phase: 'done', id: 'u8',
    instruction: '按地区汇总订单金额', outcome: 'applied', diff: { added: ['fetch'], changed: [], removed: [], total: 1 },
    ops: [{ op: 'add_node', node: { id: 'fetch', type: 'tool', data: { label: '订单汇总' } } },
      { op: 'check', status: 'repairing', round: 1, issues: [err] },
      { op: 'check', status: 'failed', issues: [err] },
      { op: 'final', graph: { nodes: [{ id: 'in' }, { id: 'fetch' }] }, issues: [err, warn] }] }] }), [sqlErr, sqlWarn])
  await page.waitForTimeout(400)
  const blocking = page.locator('[data-issue-list=""] [data-issue-code="fanout_sum"]')
  const blockingText = await blocking.innerText().catch(() => '')
  check('自查没修好的 SQL 问题：级别、中文规则名、涉及的表和列、说明、SQL 片段', blockingText.includes('错误') && blockingText.includes('一对多关联后重复计算')
    && blockingText.includes('orders.amount') && blockingText.includes('会重复计算') && blockingText.includes('SUM(o.amount)')
    && blockingText.includes('「订单汇总」'), blockingText.replace(/\s+/g, ' '))
  const notes = page.locator('[data-issue-list="sql"]')
  const notesText = await notes.innerText().catch(() => '')
  check('不挡运行的提醒单列一组「SQL 检查」，没修好的错误不重复', notesText.startsWith('SQL 检查') && notesText.includes('提醒')
    && notesText.includes('未筛选有效记录') && !notesText.includes('一对多关联后重复计算'), notesText.replace(/\s+/g, ' '))
  const panelText = await page.locator('[data-assistant-panel]').innerText()
  check('助手面板不露规则编号', !/fanout_sum|missing_valid_filter|args\.sql/.test(panelText))
  const sqlRow = page.locator('[data-turn="u8"] [data-step-code="sql_check"]')
  check('过程里有一行「SQL 检查：1 处提醒」', (await sqlRow.innerText().catch(() => '')).includes('SQL 检查：1 处提醒'),
    await page.locator('[data-turn="u8"]').innerText().then((t) => t.replace(/\s+/g, ' ').slice(0, 240)))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(200)
      await page.locator('[data-assistant-panel]').screenshot({ path: `${SHOTS}/stream-panel-sqlcheck-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
  await blocking.getByRole('button', { name: /定位/ }).click()
  await page.waitForTimeout(600)
  const sqlFocus = await page.evaluate((sql) => {
    const a = document.activeElement
    const at = a instanceof HTMLTextAreaElement ? a.selectionStart : -1
    return { sel: window.__studio.getState().selectedId, tag: a?.tagName ?? null,
             field: a?.closest('[data-inspector-sheet] [data-field]')?.getAttribute('data-field') ?? null,
             atSql: at >= 0 && (a.value.slice(at).startsWith(sql.split('\n')[0]) || a.value.slice(at).startsWith(JSON.stringify(sql).slice(1, 30))) }
  }, SQL)
  check('定位 SQL 检查的问题：打开调用工具的设置，光标落在「参数」里 sql 的值开头', sqlFocus.sel === 'fetch' && sqlFocus.field === 'args'
    && sqlFocus.tag === 'TEXTAREA' && sqlFocus.atSql, JSON.stringify(sqlFocus))
  await page.evaluate(() => {
    const st = window.__studio.getState()
    st.select(null)
    window.__studio.setState({ copilotTurns: [], nodes: st.nodes.filter((n) => n.id !== 'fetch') })
  })
  await page.waitForTimeout(200)

  // 模型把工具调用写成文字、判失败：报错和提醒行给「打开『数据查询』的设置」，点了就是那个节点的属性面板
  const MARKUP_ERROR = '模型以文本形式输出了工具调用的原始标记，未实际调用工具，这一步没有查询到任何数据。常见原因：节点未绑定工具，或模型、服务不支持工具调用。请在画布中为该节点绑定所需工具；如已绑定仍出现此问题，请换用支持工具调用的模型'
  await page.evaluate((run) => {
    window.__studio.setState({ copilotTurns: [] })
    window.__studio.setState({ run: { id: run, workflow_id: 'fx-stream3', status: 'running', input: {}, output: {}, error: null,
      usage: {}, run_class: 'exploratory', version: null }, streaming: true, unsubscribe: () => {} })
  }, RUN)
  await feed([ev(1, 'run.started', null, { nodes: 4 }, 0), ev(2, 'node.started', 'query', { node_type: 'agent', label: '数据查询' }, 0.1),
    ev(3, 'llm.start', 'query', { model: 'demo-chat' }, 0.2),
    ev(4, 'llm.end', 'query', { model: 'demo-chat', duration_ms: 1800, input_tokens: 800, output_tokens: 90, cost_usd: 0.001 }, 2),
    ev(5, 'log', 'query', { level: 'warn', code: 'tool_markup_leak', message: '模型以文本形式输出了工具调用（<｜｜DSML｜｜invoke name="db_query__shop">…），未实际调用工具，已要求模型重试一次' }, 2.1),
    ev(6, 'node.failed', 'query', { error: MARKUP_ERROR, duration_ms: 3900 }, 4),
    ev(7, 'run.failed', null, { error: MARKUP_ERROR, node_id: 'query', label: '数据查询', timing: { wall_ms: 4000, active_ms: 4000, wait_ms: 0 } }, 4)])
  await page.waitForTimeout(500)
  const runPanel = page.locator('.sheet-in').first()
  const alertText = await runPanel.locator('[data-turn-error]').innerText().catch(() => '')
  check('右栏报错说人话：模型未实际调用工具', alertText.startsWith('模型未实际调用工具') && !alertText.includes('DSML'),
    alertText.split('\n')[0])
  check('提醒行的下一步也给直达入口', await runPanel.locator('[data-step-code="tool_markup_leak"] [data-fix="canvas"]').count() === 1)
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(200)
      await page.locator('[data-assistant-panel]').screenshot({ path: `${SHOTS}/stream-panel-markup-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
  const openSettings = runPanel.locator('[data-turn-error]').getByRole('button', { name: '打开「数据查询」的设置' })
  check('报错给「打开『数据查询』的设置」', await openSettings.count() === 1)
  if (await openSettings.count()) {
    await openSettings.click()
    await page.waitForTimeout(300)
    check('点「打开设置」就是那个节点的属性面板', await page.evaluate(() => window.__studio.getState().selectedId) === 'query')
  }

  // 画布上点了跑过的节点（3C REQ-1）：画布派发 agentlab:reveal-step，右栏在对话层也要接手——
  // 切到运行层、滚到这个节点最后一次执行那一行、描一下边；滚过去之后暂停跟随，人滚回底部再恢复
  const RUN2 = 'fxstream0004'
  await page.evaluate((run) => {
    // 上面点「打开设置」开着的属性面板先收起来：它盖在右栏上
    window.__studio.getState().select(null)
    window.__studio.getState().clearRun()
    window.__studio.setState({ run: { id: run, workflow_id: 'fx-stream3', status: 'running', input: {}, output: {}, error: null,
      usage: {}, run_class: 'exploratory', version: null }, streaming: true, unsubscribe: () => {} })
  }, RUN2)
  let seq2 = 0
  const pipe = (from, to) => {
    const out = []
    for (let i = from; i <= to; i += 1) {
      out.push(ev(++seq2, 'node.started', `n${i}`, { node_type: 'tool', label: `取数 ${i}` }, i * 0.2))
      out.push(ev(++seq2, 'node.finished', `n${i}`, { duration_ms: 120, preview: { rows: i } }, i * 0.2 + 0.1))
    }
    return out
  }
  await feed([ev(++seq2, 'run.started', null, { nodes: 40 }, 0), ...pipe(1, 30)])
  await page.waitForTimeout(500)
  await page.locator('.sheet-in').first().getByRole('button', { name: /助手/ }).first().click()
  await page.waitForTimeout(300)
  const notOurs = await page.evaluate(() => window.dispatchEvent(new CustomEvent('agentlab:reveal-step',
    { detail: { runId: 'someone-else', nodeId: 'n3' }, cancelable: true })))
  check('reveal-step：不是这次运行的不接手', notOurs && await page.locator('.sheet-in').count() === 0)
  const taken = await page.evaluate((run) => !window.dispatchEvent(new CustomEvent('agentlab:reveal-step',
    { detail: { runId: run, nodeId: 'n3' }, cancelable: true })), RUN2)
  await page.waitForTimeout(900)
  const revealed = await page.evaluate(() => {
    const row = document.querySelector('.sheet-in [data-step-status][data-node-id="n3"]')
    const box = row?.closest('[data-stream-scroll]')
    const r = row?.getBoundingClientRect()
    const b = box?.getBoundingClientRect()
    return { found: !!row, flash: row?.dataset.flash ?? null, outline: row ? getComputedStyle(row).outlineStyle : null,
             inView: !!r && !!b && r.top >= b.top - 1 && r.bottom <= b.bottom + 1,
             atBottom: !!box && box.scrollHeight - box.scrollTop - box.clientHeight < 64 }
  })
  check('reveal-step：右栏在对话层也接手（preventDefault），切到运行层', taken && await page.locator('.sheet-in').count() === 1)
  check('reveal-step：滚到那个节点的步骤行，描了一下边', revealed.found && revealed.inView && revealed.flash === 'focus'
    && revealed.outline !== 'none', JSON.stringify(revealed))
  check('reveal-step：滚过去之后不在底部', !revealed.atBottom)
  await feed(pipe(31, 33))
  await page.waitForTimeout(500)
  const paused = await page.evaluate(() => {
    const row = document.querySelector('.sheet-in [data-step-status][data-node-id="n3"]')
    const box = row?.closest('[data-stream-scroll]')
    const r = row?.getBoundingClientRect()
    const b = box?.getBoundingClientRect()
    return { inView: !!r && !!b && r.top >= b.top - 1 && r.bottom <= b.bottom + 1, jump: !!document.querySelector('.sheet-in [data-jump-latest]') }
  })
  check('reveal-step：新进展来了不把人拽回底部，给一枚「跳到最新」', paused.inView && paused.jump, JSON.stringify(paused))
  await page.evaluate(() => {
    const box = document.querySelector('.sheet-in [data-stream-scroll]')
    box.scrollTop = box.scrollHeight
    box.dispatchEvent(new Event('scroll'))
  })
  await page.waitForTimeout(200)
  await feed(pipe(34, 36))
  await page.waitForTimeout(500)
  const resumed = await page.evaluate(() => {
    const box = document.querySelector('.sheet-in [data-stream-scroll]')
    return box.scrollHeight - box.scrollTop - box.clientHeight < 64
  })
  check('reveal-step：人滚回底部之后恢复跟随', resumed)
  if (SHOTS) {
    await page.locator('.sheet-in').first().getByRole('button', { name: /助手/ }).first().click()
    await page.waitForTimeout(200)
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.evaluate((run) => window.dispatchEvent(new CustomEvent('agentlab:reveal-step',
        { detail: { runId: run, nodeId: 'n5' }, cancelable: true })), RUN2)
      await page.waitForTimeout(700)
      await page.locator('[data-assistant-panel]').screenshot({ path: `${SHOTS}/stream-panel-reveal-step-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
  // 办过的 reveal-step 同样清掉：回到对话层再点运行条，不再滚回 n3、不再描边
  await page.locator('.sheet-in').first().getByRole('button', { name: /助手/ }).first().click()
  await page.waitForTimeout(300)
  await page.locator('[data-run-strip]').click()
  await page.waitForTimeout(900)
  const reopened = await page.evaluate(() => {
    const box = document.querySelector('.sheet-in [data-stream-scroll]')
    return { flashed: document.querySelectorAll('.sheet-in [data-flash]').length,
             atBottom: !!box && box.scrollHeight - box.scrollTop - box.clientHeight < 64 }
  })
  check('reveal-step 办过之后点运行条重新打开：停在最新处，不再描边', reopened.flashed === 0 && reopened.atBottom,
    JSON.stringify(reopened))
  const hiddenTaken = await page.evaluate((run) => {
    const aside = document.querySelector('[data-assistant-panel]').closest('aside')
    aside.style.visibility = 'hidden'
    const passed = window.dispatchEvent(new CustomEvent('agentlab:reveal-step', { detail: { runId: run, nodeId: 'n3' }, cancelable: true }))
    aside.style.visibility = ''
    return !passed
  }, RUN2)
  check('reveal-step：右栏收起时不接手，让画布自己兜底', !hiddenTaken)

  // 长运行：顶层超过 50 行时前面的收在「展开前面的 N 条」里。点名要看的节点正好在收起的那一截：
  // 先展开、再滚过去描边。以前找不到那一行就什么都不做，却照样报「办完了」（3C REQ-22）
  const RUN3 = 'fxstream0005'
  await page.evaluate((run) => {
    window.__studio.getState().clearRun()
    window.__studio.setState({ run: { id: run, workflow_id: 'fx-stream3', status: 'running', input: {}, output: {}, error: null,
      usage: {}, run_class: 'exploratory', version: null }, streaming: true, unsubscribe: () => {} })
  }, RUN3)
  seq2 = 0
  await feed([ev(++seq2, 'run.started', null, { nodes: 60 }, 0), ...pipe(1, 60)])
  await page.waitForTimeout(600)
  const folded = await page.evaluate(() => [...document.querySelectorAll('.sheet-in button')]
    .some((b) => /展开前面的 \d+ 条/.test(b.textContent ?? '')))
  await page.locator('.sheet-in').first().getByRole('button', { name: /助手/ }).first().click()
  await page.waitForTimeout(300)
  const takenLong = await page.evaluate((run) => !window.dispatchEvent(new CustomEvent('agentlab:reveal-step',
    { detail: { runId: run, nodeId: 'n3' }, cancelable: true })), RUN3)
  await page.waitForTimeout(1000)
  const deep = await page.evaluate(() => {
    const row = document.querySelector('.sheet-in [data-step-status][data-node-id="n3"]')
    const box = row?.closest('[data-stream-scroll]')
    const r = row?.getBoundingClientRect()
    const b = box?.getBoundingClientRect()
    return { found: !!row, flash: row?.dataset.flash ?? null,
             inView: !!r && !!b && r.top >= b.top - 1 && r.bottom <= b.bottom + 1 }
  })
  check('长运行（60 个节点）：点名的节点收在「展开前面的 N 条」里，先展开再滚过去描边',
    folded && takenLong && deep.found && deep.inView && deep.flash === 'focus', JSON.stringify({ folded, takenLong, ...deep }))

  check('画布右栏没有运行时报错', errors.length === 0, errors.join(' | '))
  await ctx.close()
})

await section('逐段证据：带 _evidence 的成果（可点击证据第一期）', async () => {
  // 报告文档和片段接口用夹具伪造（后端 compose_doc 真跑出来的，只用通用名），非 GET 一律拦掉
  const { readFileSync } = await import('node:fs')
  const fx = JSON.parse(readFileSync(new URL('../frontend/src/run/__tests__/evidence-doc.json', import.meta.url), 'utf8'))
  for (const [w, dense] of [[1100, false], [380, true]]) {
    const page = await browser.newPage({ viewport: { width: w, height: 900 } })
    const errors = []
    page.on('pageerror', (e) => errors.push(e.message))
    await page.route((u) => new URL(u).pathname.startsWith('/api/'), (r) => {
      const url = new URL(r.request().url())
      if (r.request().method() !== 'GET') return r.abort()
      const seg = url.pathname.match(/\/evidence\/segments\/([^/]+)$/)
      if (seg) return r.fulfill({ json: fx.segments[seg[1]] ?? {} })
      if (url.pathname === `/api/artifacts/${fx.doc_artifact}`) return r.fulfill({ json: { id: fx.doc_artifact, content: fx.doc } })
      return r.continue()
    })
    const tag = dense ? '窄栏' : '宽栏'
    await page.goto(`${WEB}/preview.html?syn=evidence${dense ? '&dense=1' : ''}`, { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-turn="evidence"] [data-evidence-doc]', { timeout: 6000 })
    const turn = page.locator('[data-turn="evidence"]')
    check(`${tag}：带证据的字段换成逐段文档，数字是可点的片段`, await turn.locator('[data-seg]').count() === 13)
    check(`${tag}：出具横幅多一行计数`, (await turn.locator('[data-evidence-line]').innerText())
      .includes('7/12 数字有出处 · 无证据 5 · 另有 1 处引用无法解析'))
    check(`${tag}：没有 _evidence 的那一轮不变`, await page.locator('[data-turn="evidence-legacy"] [data-seg]').count() === 0)
    await turn.locator('[data-seg="s6"]').click()
    await page.waitForSelector('[data-evidence-panel] [data-ev-substituted]')
    const mode = await page.locator('[data-evidence-panel]').getAttribute('data-evidence-panel')
    check(`${tag}：面板${dense ? '在栏内展开' : '从侧边弹出'}`, mode === (dense ? 'inline' : 'side'), mode)
    const box = await page.evaluate(() => {
      const el = document.querySelector('[data-stream-scroll]')
      return { sw: el.scrollWidth, cw: el.clientWidth }
    })
    check(`${tag}：打开面板后流里没有横向滚动`, box.sw <= box.cw, JSON.stringify(box))
    const copy = await turn.locator('button[aria-label="复制"]').count()
    check(`${tag}：答案的复制 / 导出照常在`, copy > 0)
    check(`${tag}：没有运行时报错`, errors.length === 0, errors.join(' | '))
    if (SHOTS) {
      for (const theme of ['dark', 'light']) {
        await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
        await page.waitForTimeout(250)
        await page.screenshot({ path: `${SHOTS}/stream-evidence-${dense ? 'dense' : 'wide'}-${theme}.png` })
      }
    }
    await page.close()
  }
})

await section('审批卡：始终允许（工具信任三档）', async () => {
  // 探索运行里 MCP / 自定义工具的审批带 trust_key：「批准」旁边多一个「始终允许」，回复走
  // POST /runs/{id}/resume，response 多带 always。不带 trust_key 的审批和以前一模一样。
  // 真页面、真 store，工作流、审批、运行全用 page.route 伪造，写请求只记下来不落库
  const WF_ID = 'fx-trust'
  const RUN = 'fxtrust0001'
  const GRAPH = {
    nodes: [
      { id: 'in', type: 'input', position: { x: 0, y: 0 }, data: { label: '问题', config: { fields: [{ name: 'q' }] } } },
      { id: 'query', type: 'agent', position: { x: 300, y: 0 }, data: { label: '查档案', config: { tools: ['crm_lookup'] } } },
      { id: 'out', type: 'output', position: { x: 620, y: 0 }, data: { label: '成果', config: {} } },
    ],
    edges: [{ id: 'e1', source: 'in', target: 'query' }, { id: 'e2', source: 'query', target: 'out' }],
  }
  const WF = { id: WF_ID, name: '信任检查', description: '', graph: GRAPH, tags: [], version: 1, status: 'draft',
    published_version: null, run_count: 0, created_at: '2026-09-28T00:00:00Z', updated_at: '2026-09-28T00:00:00Z' }
  const approvalOf = (tool, trustKey) => ({ id: `ap-${tool}`, run_id: RUN, node_id: 'query', mode: 'approve', title: `Agent 请求调用工具 ${tool}`,
    payload: { kind: 'tool_approval', node_id: 'query', tool, args: { id: 'C-1' }, title: `是否允许调用 ${tool}？`,
      ...(trustKey ? { trust_key: trustKey } : {}) },
    status: 'pending', response: {}, created_at: new Date(Date.now() - 30_000).toISOString(),
    workflow_name: '信任检查', node_label: '查档案', run_class: 'exploratory' })

  const setup = async (approval) => {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: 'dark' })
    await ctx.addInitScript(() => { try { localStorage.setItem('agentlab_actor', '张工') } catch { /* 隐私窗口 */ } })
    const page = await ctx.newPage()
    const errors = []
    page.on('pageerror', (e) => errors.push(e.message))
    const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    const writes = []
    const toolHits = []
    let pending = true
    await page.route(/\/api\/workflows(\?.*)?$/, async (route) => {
      if (route.request().method() !== 'GET') return json(route, { detail: '检查脚本不写库' }, 409)
      const real = await (await route.fetch()).json().catch(() => [])
      return json(route, [WF, ...(Array.isArray(real) ? real : [])])
    })
    await page.route(/\/api\/workflows\/fx-trust(\/.*)?(\?.*)?$/, (route) =>
      route.request().method() === 'GET' ? json(route, WF) : json(route, { detail: '检查脚本不写库' }, 409))
    await page.route(/\/api\/conversations(\/.*)?(\?.*)?$/, (route) =>
      route.request().method() === 'GET' ? json(route, []) : json(route, { id: 'fx-conv-trust', kind: 'canvas', title: '', turns: [] }))
    await page.route(/\/api\/tools(\?.*)?$/, (route) => { toolHits.push(Date.now()); return route.continue() })
    await page.route(/\/api\/approvals(\/.*)?(\?.*)?$/, (route) => {
      const req = route.request()
      if (req.method() === 'GET') return json(route, pending ? [approval] : [])
      writes.push({ url: new URL(req.url()).pathname, body: req.postDataJSON(), actor: req.headers()['x-actor'] ?? '' })
      return json(route, { detail: '检查脚本不写库' }, 409)
    })
    await page.route(/\/api\/runs(\/.*)?(\?.*)?$/, (route) => {
      const req = route.request()
      const path = new URL(req.url()).pathname
      if (req.method() !== 'GET') {
        writes.push({ url: path, body: req.postDataJSON(), actor: req.headers()['x-actor'] ?? '' })
        if (path.endsWith(`${RUN}/resume`)) {
          pending = false
          return json(route, { id: RUN, workflow_id: WF_ID, status: 'running', input: {}, output: {}, error: null, usage: {}, run_class: 'exploratory' })
        }
        return json(route, { detail: '检查脚本不写库' }, 409)
      }
      if (path.includes(`${RUN}/events`)) return json(route, [])
      if (path.includes(RUN)) {
        return json(route, { id: RUN, workflow_id: WF_ID, status: pending ? 'interrupted' : 'running', input: {}, output: {}, error: null, usage: {}, run_class: 'exploratory' })
      }
      return route.continue()
    })
    await page.goto(`${WEB}/studio/${WF_ID}`, { waitUntil: 'networkidle' })
    await page.waitForFunction((id) => window.__studio?.getState().workflow?.id === id, WF_ID, { timeout: 15000 })
    const now = Date.now() / 1000
    const ev = (seq, type, node_id, data = {}, t = 0) => ({ seq, type, node_id, data, ts: now - 30 + t })
    const tool = approval.payload.tool
    const WAIT = [
      ev(1, 'run.started', null, { nodes: 3 }, 0),
      ev(2, 'node.started', 'in', { node_type: 'input', label: '问题' }, 0.1),
      ev(3, 'node.finished', 'in', { duration_ms: 3, preview: { q: 'x' } }, 0.2),
      ev(4, 'node.started', 'query', { node_type: 'agent', label: '查档案' }, 0.3),
      ...(approval.payload.trust_key
        ? [ev(5, 'tool.gated', 'query', { tool, verdict: 'escalate', reason: '要导出整张客户表', model: 'tiny-1', duration_ms: 640 }, 0.9)] : []),
      ev(6, 'human.requested', 'query', { mode: 'approve', tool, args: { id: 'C-1' }, title: `Agent 请求调用工具 ${tool}`,
        ...(approval.payload.trust_key ? { trust_key: approval.payload.trust_key } : {}) }, 1.0),
      ev(7, 'run.interrupted', 'query', { payload: approval.payload }, 1.1),
    ]
    await page.evaluate((run) => window.__studio.setState({ run: { id: run, workflow_id: 'fx-trust', status: 'queued', input: {},
      output: {}, error: null, usage: {}, run_class: 'exploratory', version: null }, streaming: true, unsubscribe: () => {} }), RUN)
    await page.evaluate((l) => { const s = window.__studio.getState(); for (const e of l) s.applyEvent(e) }, WAIT)
    await page.waitForTimeout(700)
    return { ctx, page, errors, writes, toolHits, card: page.locator('.sheet-in [data-approval]').first() }
  }

  {
    const { ctx, page, errors, writes, toolHits, card } = await setup(approvalOf('crm_lookup', 'crm_lookup'))
    const buttons = await card.locator('button').allInnerTexts()
    check('带 trust_key：「批准」旁边有「始终允许」', buttons.map((b) => b.trim()).join('|').includes('批准|始终允许|驳回'), buttons.join(' | '))
    const always = card.getByRole('button', { name: '始终允许' })
    const title = await always.getAttribute('title')
    check('……悬停说明：批准本次调用，并将工具设为「始终允许 · 门控把关」，可在「工具」页修改',
      !!title?.includes('批准本次调用，并将「crm_lookup」设为「始终允许 · 门控把关」') && title.includes('由门控模型逐次把关') && title.includes('可在「工具」页修改'), title)
    const note = await card.innerText()
    check('……卡上常显同一句旁注，不只藏在悬停里', note.includes('始终允许：批准本次调用') && note.includes('之后的运行将不再请求审批'))
    check('……时间线上写着门控为什么拦截', (await page.locator('.sheet-in').first().innerText()).includes('门控拦截 crm_lookup，转交人工审批：要导出整张客户表'))
    if (SHOTS) {
      for (const theme of ['dark', 'light']) {
        await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
        await page.waitForTimeout(250)
        await card.screenshot({ path: `${SHOTS}/stream-approval-always-${theme}.png` })
        await page.locator('.sheet-in').first().screenshot({ path: `${SHOTS}/stream-approval-always-panel-${theme}.png` })
      }
      await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
    }
    await card.getByLabel('备注').fill('只查一条，放行')
    const hitsBefore = toolHits.length
    await always.click()
    await page.waitForTimeout(700)
    const resume = writes.find((w) => w.url.endsWith(`/runs/${RUN}/resume`))
    check('点了发 POST /runs/{id}/resume，response 是 {approved: true, always: true, note}',
      resume?.body?.response?.approved === true && resume.body.response.always === true && resume.body.response.note === '只查一条，放行',
      JSON.stringify(resume?.body ?? writes.map((w) => w.url)))
    check('……带 approval_id 指明回复的是哪一条', resume?.body?.approval_id === 'ap-crm_lookup', resume?.body?.approval_id)
    check('……带着署名，不另走 /approvals/…/decide', decodeURIComponent(resume?.actor ?? '') === '张工'
      && !writes.some((w) => w.url.includes('/decide')), writes.map((w) => w.url).join(', '))
    const toastText = await page.locator('[aria-live]').allInnerTexts().then((t) => t.join(' ')).catch(() => '')
    check('……回执说运行继续、之后由门控模型把关', toastText.includes('之后由门控模型把关'), toastText.replace(/\s+/g, ' ').slice(0, 120))
    check('……工具目录重拉了一次（工具页跟着显示门控把关）', toolHits.length > hitsBefore, `${hitsBefore} → ${toolHits.length}`)
    check('……审批卡收起来了', await page.locator('.sheet-in [data-approval]').count() === 0)
    check('没有运行时报错', errors.length === 0, errors.join(' | '))
    await ctx.close()
  }

  {
    // 内置工具的审批（不带 trust_key）：和以前一模一样
    const { ctx, errors, writes, card } = await setup(approvalOf('file_write', null))
    const buttons = (await card.locator('button').allInnerTexts()).map((b) => b.trim())
    check('不带 trust_key：没有「始终允许」，按钮还是批准 / 驳回', buttons.join('|').includes('批准|驳回') && !buttons.includes('始终允许'), buttons.join(' | '))
    const text = await card.innerText()
    check('……卡上不提始终允许和门控', !text.includes('始终允许') && !text.includes('门控'), text.replace(/\s+/g, ' ').slice(0, 160))
    check('……批了会怎样还是原来那句', text.includes('批准：继续运行；驳回：转入「驳回」分支'))
    await card.getByRole('button', { name: '批准' }).click()
    await ctx.pages()[0].waitForTimeout(400)
    const decide = writes.find((w) => w.url.includes('/decide'))
    check('……「批准」照旧走 /approvals/…/decide，请求体里没有 always', !!decide && decide.body?.approved === true && !('always' in (decide.body ?? {}))
      && !writes.some((w) => w.url.endsWith('/resume')), JSON.stringify(decide?.body ?? writes.map((w) => w.url)))
    check('没有运行时报错', errors.length === 0, errors.join(' | '))
    await ctx.close()
  }
})

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 助手流渲染全部通过')
process.exit(failed ? 1 : 0)
