// 各页面的端到端检查：真浏览器、真后端、真数据。
//
// check-decode 守翻译，check-stream 守渲染，这一层守的是"接起来还对不对"——
// 前两层都能通过而这一层坏掉：store 没把事件存进去、tab 切换把状态卸载了、
// 唯一的运行入口被改版顺手删了、组件类把工具类盖掉了。
//
// 跑之前前后端都得起着（./scripts/dev.sh）。默认连 5273 / 8000；对别的实例（比如一份
// 沙箱拷贝）跑时带上地址：
//   AGENTLAB_WEB=http://localhost:<前端端口> AGENTLAB_API=http://localhost:<后端端口>/api node scripts/check-ui.mjs
// 只跑其中几节：CHECK_ONLY=运行历史,样式层叠（按节名包含匹配）。SHOT_DIR=<目录> 时各节存一张截图。
//
// 读的是真数据，写一律拦在浏览器里：非 GET 的接口请求只放行纯计算的校验和变量分析，
// 其余的要么由那一节自己伪造回应，要么直接掐掉。所以这份检查对哪个库跑都不留痕迹。
import { readFileSync } from 'node:fs'
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const API = process.env.AGENTLAB_API ?? 'http://localhost:8000/api'
const CHROME = process.env.CHROME_PATH
  ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const SHOTS = process.env.SHOT_DIR ?? ''
const ONLY = process.env.CHECK_ONLY?.split(',').map((x) => x.trim()).filter(Boolean)

// 仓库是公开的，这份输出会被贴进报告：跑的是真数据，对话标题、工作流名、节点名、
// 文档标题、数据源名一律换成占位符再打印。本脚本源码里自己写着的串（夹具）本来就公开，不换
const ownSource = readFileSync(new URL(import.meta.url), 'utf8')
const listOf = async (path) => {
  try { const r = await fetch(API + path); return r.ok ? await r.json() : [] } catch { return [] }
}
const secrets = []
for (const c of await listOf('/conversations?kind=chat&include_archived=true')) secrets.push([c.title, '‹对话标题›'])
for (const w of await listOf('/workflows')) {
  secrets.push([w.name, '‹工作流名›'])
  for (const n of w.graph?.nodes ?? []) secrets.push([n.data?.label ?? n.label, '‹节点名›'])
}
for (const d of await listOf('/kb/documents')) secrets.push([d.title, '‹文档标题›'])
for (const d of await listOf('/datasources')) secrets.push([d.name, '‹数据源名›'], [d.description, '‹数据源描述›'])
const maskList = [...new Map(secrets
  .filter(([v]) => typeof v === 'string' && v.trim().length >= 2 && !ownSource.includes(v))).entries()]
  .sort((a, b) => b[0].length - a[0].length)
const masked = (t) => maskList.reduce((out, [v, tag]) => out.split(v).join(tag), String(t))

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${masked(detail)}` : ''}`)
  if (!cond) failed++
}

const browser = await chromium.launch({ executablePath: CHROME })
const ctx = await browser.newContext({ viewport: { width: 1500, height: 940 } })

// 写操作的闸门。各节自己用 page.route 伪造的接口优先于这里（页面级路由先匹配），
// 漏到这里的非 GET 请求记下来再掐掉：逛页面本身会触发哪些写，一眼看得见
const COMPUTE_ONLY = [/\/api\/workflows\/(validate|variables)$/]
const blocked = []
const blockedUrls = new Set()
await ctx.route('**/api/**', (route) => {
  const r = route.request()
  const path = new URL(r.url()).pathname
  if (['GET', 'HEAD'].includes(r.method()) || COMPUTE_ONLY.some((re) => re.test(path))) return route.continue()
  blocked.push(`${r.method()} ${path}`)
  blockedUrls.add(r.url())
  return route.abort('blockedbyclient')
})

/**
 * 一节一节地跑：某一节里元素找不到、等待超时，只记成这一节失败，关掉它开的页面，
 * 接着跑下一节——不让一处卡住把后面几十项一起吞掉
 */
const opened = new Set()
async function section(name, fn) {
  if (ONLY?.length && !ONLY.some((k) => name.includes(k))) return
  console.log(`\n=== ${name} ===`)
  const mark = blocked.length
  try {
    await fn()
  } catch (e) {
    check(`${name} 中途出错`, false, String(e?.message ?? e).split('\n')[0])
  } finally {
    for (const p of opened) await p.close().catch(() => {})
    opened.clear()
  }
  // 页面自己发出、被闸门掐掉的写请求：逛页面就想写库的地方都在这里，列出来备查，不算失败
  const writes = [...new Set(blocked.slice(mark).map((w) => w.replace(/[0-9a-f]{32}/g, '…')))]
  if (writes.length) console.log(`  · 拦下的写请求：${writes.join('、')}`)
}

/** 开一页并收集真实报错。favicon 的 404 不算，被上面的闸门掐掉的写请求也不算 */
async function newPage() {
  const page = await ctx.newPage()
  opened.add(page)
  const errors = []
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))
  page.on('console', (m) => {
    if (m.type() !== 'error' || m.text().includes('404')) return
    if (blockedUrls.has(m.location()?.url)) return
    errors.push(m.text())
  })
  return { page, errors }
}
async function visit(path) {
  const { page, errors } = await newPage()
  await page.goto(`${WEB}${path}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(700)
  return { page, errors }
}
const shot = async (page, name) => {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png` })
}
const overflowOf = (page) => page.evaluate(() =>
  document.documentElement.scrollWidth - document.documentElement.clientWidth)
/** 等一句提示出现（toast 约 4 秒就走，固定等一段再数会错过），再留一小段看它有没有重复 */
async function countNotice(page, text, settle = 1200) {
  const seen = await page.getByText(text).first().waitFor({ timeout: 8000 }).then(() => true, () => false)
  if (!seen) return 0
  await page.waitForTimeout(settle)
  return ((await page.locator('body').innerText()).match(new RegExp(text, 'g')) ?? []).length
}

await section('画布助手栏', async () => {
  const { page, errors } = await visit('/studio')
  check('没有运行时报错', errors.length === 0, errors.join(' | '))

  const body = await page.locator('body').innerText()
  check('不再有两层 tab 嵌套',
    !body.includes('时间线') && !body.includes('原始事件'))

  // 用户的原话："没有一个很好的地方引导用户写问题"。空态下输入区就该是主体
  check('空态给出了明确的邀请', body.includes('需要助手做什么'))
  check('说清楚它能够到什么', /\d+ 个工具/.test(body), body.match(/\d+ 个工具/)?.[0])
  const examples = await page.locator('aside button').filter({ hasText: /。|，/ }).count()
  check('例句是完整句子而不是截断的 chip', examples >= 2, `${examples} 条`)

  const composer = page.locator('aside textarea').first()
  check('输入框自动聚焦，不用先点一下',
    await composer.evaluate((el) => el === document.activeElement))

  // 改版最容易顺手删掉的东西：唯一的运行入口
  check('发起运行的入口还在（已搬到工具栏）',
    await page.getByRole('button', { name: /^运行/ }).count() > 0)

  // 这是这次重构的核心承诺：属性是盖在助手上的一层，不是把它换掉。
  // 切 tab 会卸载组件，草稿、滚动位置、展开状态全丢——那正是要修的问题
  await composer.fill('测试草稿不要丢')
  await page.locator('.react-flow__node').first().click()
  await page.waitForTimeout(350)
  const sheet = await page.locator('body').innerText()
  check('选中节点滑出属性面板', sheet.includes('节点名称') || sheet.includes('ID:'))
  check('面板上有明确的返回，让人知道底下还有东西',
    await page.getByTitle('返回助手（Esc）').count() > 0)

  await page.keyboard.press('Escape')
  await page.waitForTimeout(350)
  check('Esc 关得掉', await page.getByTitle('返回助手（Esc）').count() === 0)
  check('回来之后输入的字还在', await composer.inputValue() === '测试草稿不要丢',
    await composer.inputValue())
  await composer.fill('')

  const overflow = await overflowOf(page)
  check('页面不横向溢出', overflow <= 0, `${overflow}px`)
  await shot(page, 'studio')
})

await section('变量表与模板补全', async () => {
  const { page, errors } = await visit('/studio')
  check('没有运行时报错', errors.length === 0, errors.join(' | '))

  await page.getByRole('button', { name: /^变量/ }).click()
  await page.waitForTimeout(700)
  const rows = await page.locator('table tbody tr').count()
  check('变量表列出了变量', rows > 0, `${rows} 行`)
  const body = await page.locator('body').innerText()
  check('列出的是可直接粘的写法', body.includes('{{ input.'), '')

  // 抽屉是挤压式的：画布不能被压没，React Flow 容器高度归零会直接报错
  const canvasH = await page.locator('.react-flow').first()
    .evaluate((el) => el.getBoundingClientRect().height)
  check('画布没有被压没', canvasH > 120, `${Math.round(canvasH)}px`)
  await page.getByRole('button', { name: /^变量/ }).click()
  await page.waitForTimeout(400)

  // 补全：模板取不到值会静默渲染成空字符串，补全是从源头消灭这类错误。
  // 默认那张图只有 输入→成果，没有带模板的字段，先从面板拖一个模型调用出来
  await page.locator('text=模型调用').first().click()
  await page.waitForTimeout(700)
  // 必须限定在属性面板里：底下助手栏的 Copilot 输入框也是 aside textarea，
  // 而它被这一层盖着，点它会一直超时
  const field = page.locator('.sheet-in textarea').first()
  await field.click()
  await page.keyboard.type('{{')
  await page.waitForTimeout(400)
  const hasPanel = (await page.locator('body').innerText()).includes('⏎ 插入')
  check('打 {{ 弹出候选', hasPanel)
  if (hasPanel) {
    await page.keyboard.press('Enter')
    await page.waitForTimeout(350)
    const v = await field.inputValue()
    check('选中后插入成完整写法', /\{\{ \S+ \}\}/.test(v), v.slice(0, 40))
    // 完全受控组件，插入后光标会被重置到末尾，必须自己放回去
    await page.keyboard.type('X')
    check('光标停在插入内容之后', (await field.inputValue()).endsWith('X'),
      (await field.inputValue()).slice(-20))
  }
  await shot(page, 'variables')
})

await section('动效对前庭敏感者可关', async () => {
  // 这套界面里动的东西不少（边在流动、节点在脉冲、面板在滑）。
  // 系统里关了动效还照播，对前庭敏感的人是实打实的难受
  const { page } = await newPage()
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto(`${WEB}/studio`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(500)
  const durations = await page.evaluate(() => {
    const probe = document.createElement('div')
    probe.className = 'rise-in'
    document.body.appendChild(probe)
    const d = getComputedStyle(probe).animationDuration
    probe.remove()
    return d
  })
  check('关掉动效后动画确实停了', parseFloat(durations) < 0.01, durations)
})

await section('运行历史', async () => {
  const { page, errors } = await visit('/runs')
  check('没有运行时报错', errors.length === 0, errors.join(' | '))

  // 行是按天分组的按钮，时间只写「11:36」「9/25 20:52」，所以按结构认，不按日期格式认
  const rows = page.locator('[data-runs-list] [data-run-id]')
  await rows.first().waitFor({ timeout: 10000 }).catch(() => {})
  const n = await rows.count()
  if (!n && !(await listOf('/runs?limit=1')).length) {
    check('（跳过：库里还没有运行记录）', true)
    return
  }
  check('列出了运行记录', n > 0, `${n} 条`)
  if (!n) return

  // 列表原本每行只有"临时图 · 1.2s · 340 tok"，十条长得一模一样。现在第二行是这一条
  // 自己的事：成功的写摘要，失败的写原因，等人的写等谁
  const withContext = await rows.evaluateAll((els) =>
    els.filter((el) => el.querySelectorAll(':scope > div').length >= 3).length)
  check('行里有这一条自己的内容，不只是耗时和 token', withContext > 0, `${withContext}/${n} 行带第二行`)

  const first = rows.first()
  const id = await first.getAttribute('data-run-id')
  await first.click()
  await page.waitForURL(`**/runs/${id}**`, { timeout: 5000 }).catch(() => {})
  check('点开一条，地址跟着换成它', new URL(page.url()).pathname === `/runs/${id}`, page.url())
  check('列表里标着正在看的是哪一条', await page.locator(`[data-runs-list] [data-run-id="${id}"][aria-current="true"]`)
    .waitFor({ timeout: 4000 }).then(() => true, () => false))
  const pane = page.locator('[data-view-pane="stream"]')
  await pane.waitFor({ timeout: 8000 }).catch(() => {})
  await pane.locator('[data-turn]').first().waitFor({ timeout: 6000 }).catch(() => {})
  await page.waitForTimeout(400)
  const body = await pane.innerText().catch(() => '')
  check('详情走的是可读视图，不是事件表',
    !/\bnode\.(started|finished)\b/.test(body) && !/\brun\.started\b/.test(body))
  // 按结构认，不按字数：最新一条可能只是个「hi」，正确的视图也就三十几个字。
  // 有轮次、轮次头说得出状态、跑过节点的就列得出节点行
  const view = await pane.evaluate((el) => ({
    turns: el.querySelectorAll('[data-turn]').length,
    status: [...el.querySelectorAll('[data-turn-status]')].map((s) => s.textContent.trim()),
    nodes: el.querySelectorAll('[data-node-id]').length,
  })).catch(() => ({ turns: 0, status: [], nodes: 0 }))
  const ran = (await listOf(`/runs/${id}/events?after=0`)).some((e) => e.type === 'node.started')
  check('详情有内容：轮次、状态、节点行都在',
    view.turns > 0 && view.status.length > 0 && view.status.every(Boolean) && (!ran || view.nodes > 0),
    `${view.turns} 轮 · 状态「${view.status.join('、')}」· ${view.nodes} 个节点行${ran ? '' : '（这条没跑到节点）'}`)

  // 原始事件不是常驻 tab，但必须还能看到
  const raw = page.locator('[data-action="raw"]')
  await raw.click()
  const rawShown = await page.locator('[data-raw-events]').waitFor({ timeout: 4000 }).then(() => true, () => false)
  check('切得到原始事件', rawShown && /node\.(started|finished)|run\.started/.test(await pane.innerText()))
  check('开关说得出现在是开着的', await raw.getAttribute('aria-pressed') === 'true')
  await raw.click()
  await page.waitForTimeout(300)
  check('切得回来', await page.locator('[data-raw-events]').count() === 0
    && !/\bnode\.started\b/.test(await pane.innerText()))
  await shot(page, 'runs')
})

await section('问数据', async () => {
  const { page, errors } = await visit('/chat')
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
  const body = await page.locator('body').innerText()
  check('有空态或历史，不是白屏', body.length > 60, `${body.length} 字`)
  check('有输入框', await page.locator('textarea').count() > 0)
  const overflow = await overflowOf(page)
  check('页面不横向溢出', overflow <= 0, `${overflow}px`)
  await shot(page, 'chat')
})

await section('向量模型设置', async () => {
  // 以前这里只有三个写死的选项（本地 / OpenAI 3-small / 3-large），接不了
  // 本机起的服务。而本地哈希向量不支持语义检索这件事，界面上也得说出来。
  const { page, errors } = await visit('/knowledge')
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))

  let body = await page.locator('body').innerText()
  check('显示当前用的是什么、几维', /·\s*\d+\s*维/.test(body),
        body.match(/[\w:.-]+ · \d+ 维/)?.[0])
  if (body.includes('local-hashing')) {
    check('本地哈希要标出不支持语义检索', body.includes('不支持语义检索'))
  }

  await page.getByRole('button', { name: '更换' }).first().click()
  await page.waitForTimeout(400)
  body = await page.locator('body').innerText()
  check('能配自定义端点', body.includes('自定义端点'))
  check('也留着本地那一项', body.includes('本地哈希向量'))
  // 模型名手填太容易错，必须能探
  check('给得出探测入口', body.includes('获取模型列表'))

  const overflow = await overflowOf(page)
  check('页面不横向溢出', overflow <= 0, `${overflow}px`)
  await shot(page, 'embedding')
})

await section('向量模型：配了语义模型却退回本地', async () => {
  // 配的是本机 LM Studio 上的模型，服务启动时它没起，进程就一直用本地哈希。
  // 以前页面按"配的是不是 local"判断：不说没有语义能力，还高亮"重建索引"——
  // 那些"对不上"的向量正是那个模型建好的，一点就被哈希覆盖。真实环境撞不撞得上
  // 这个状态看运气，所以这里伪造状态接口，几种情况各看一遍；写请求一律拦下
  const embeddingPage = async (initial, onPut) => {
    let status = initial
    const { page, errors } = await newPage()
    const sent = []
    await page.route('**/api/**', (route) => {
      const r = route.request()
      if (new URL(r.url()).pathname.endsWith('/api/kb/embedding')) {
        if (r.method() === 'GET') return route.fulfill({ json: status })
        if (r.method() === 'PUT') {
          sent.push(r.postDataJSON())
          status = onPut(status)
          return route.fulfill({ json: {
            embedder: status.embedder, dim: status.dim,
            stale_chunks: status.stale_chunks, stale_memories: status.stale_memories } })
        }
      }
      return r.method() === 'GET' ? route.continue() : route.abort()
    })
    await page.goto(`${WEB}/knowledge`, { waitUntil: 'networkidle' })
    await page.waitForTimeout(500)
    return { page, errors, sent, body: () => page.locator('body').innerText() }
  }
  const down = {
    embedder: 'local-hashing', dim: 512, configured: true, kind: 'openai',
    model: 'text-embedding-qwen3-embedding-4b', base_url: 'http://127.0.0.1:1234/v1',
    stale_chunks: 68, stale_memories: 5, unindexed_chunks: 0,
    has_semantics: false, default_alpha: 0, fallback: true,
    fallback_reason: '无法使用 http://127.0.0.1:1234/v1 上的向量模型 '
      + 'text-embedding-qwen3-embedding-4b：APIConnectionError: Connection error.。请检查服务是否可用、模型名是否正确；使用官方接口时还需检查 OPENAI_API_KEY。',
  }
  const up = {
    ...down, embedder: 'openai:text-embedding-qwen3-embedding-4b', dim: 2560,
    stale_chunks: 0, stale_memories: 0, has_semantics: true, default_alpha: 0.5,
    fallback: false, fallback_reason: '',
  }

  const a = await embeddingPage(down, () => up)
  let body = await a.body()
  check('退回本地时标出不支持语义检索', body.includes('不支持语义检索'))
  check('说清配的是哪个、为什么没用上',
        body.includes('text-embedding-qwen3-embedding-4b') && body.includes('但连接失败'))
  check('没连上的原因原样给出来', body.includes('APIConnectionError'))
  check('不再劝人重建（会把建好的向量覆盖掉）',
        await a.page.getByRole('button', { name: /重建索引/ }).count() === 0)
  check('存量提示改口成暂缓重建', body.includes('请暂缓重建'))

  const reconnect = a.page.getByRole('button', { name: '重新连接' })
  check('给得出「重新连接」', await reconnect.count() === 1)
  if (await reconnect.count()) {
    await reconnect.click()
    await a.page.waitForTimeout(800)
  }
  const req = a.sent[0]
  check('重新连接发的是保存着的那份配置',
        req?.kind === 'openai' && req.model === down.model && req.base_url === down.base_url,
        JSON.stringify(req))
  body = await a.body()
  check('连上了说一声', body.includes('已重新连接'))
  check('连上之后提示消失', !body.includes('不支持语义检索') && !body.includes('连接失败'))
  check('没有运行时报错', !a.errors.some((e) => e.startsWith('pageerror')), a.errors.slice(0, 2).join(' | '))
  await a.page.close()

  // 明确选了本地哈希是正常情况：照旧标不支持语义检索，重建按钮也还在
  const b = await embeddingPage(
    { ...down, kind: 'local', model: '', base_url: '', fallback: false, fallback_reason: '' },
    (s) => s)
  body = await b.body()
  check('明确选了本地哈希：照旧标不支持语义检索', body.includes('不支持语义检索'))
  check('……不冒出"连接失败"', !body.includes('连接失败'))
  check('……重建按钮还在，正常情况别一起藏了',
        await b.page.getByRole('button', { name: /重建索引/ }).count() === 1)
})

await section('会话', async () => {
  // 对话以前只活在内存里，刷新就没了。这一组守的是它真的落了库：
  // 列表能列出来、切过去内容跟着变、刷新还在。
  //
  // 最后一项守的是一个真踩过的坑：进页面时自动建一条空会话，而 create 是
  // 异步的、StrictMode 又把 effect 跑两遍，于是每次访问都留下两三条"新对话"。
  // 写请求被闸门掐掉了，库里的条数不会变，所以直接数页面有没有想去建
  const before = await listOf('/conversations')
  const mark = blocked.length

  const { page, errors } = await visit('/chat')
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
  check('有"新对话"按钮', await page.getByRole('button', { name: /新对话/ }).first().isVisible())

  const seeded = before.find((c) => c.turn_count > 0)
  if (seeded) {
    await page.locator('aside button[title]').filter({ hasText: seeded.title }).first().click()
    await page.waitForTimeout(700)
    check('切过去能看到那次问的话', (await page.locator('main').innerText()).includes(seeded.title))
    await page.reload({ waitUntil: 'networkidle' })
    await page.waitForTimeout(900)
    check('刷新后还在', (await page.locator('main').innerText()).includes(seeded.title))
  } else {
    check('（跳过：库里还没有带轮次的会话）', true)
  }

  const creates = blocked.slice(mark).filter((w) => /^POST \/api\/conversations$/.test(w))
  check('逛一圈没有想凭空建会话', creates.length === 0, `${creates.length} 次`)
  await shot(page, 'conversations')
})

await section('URL 指得到', async () => {
  // 在此之前「在看哪个对话/哪次运行/哪一屏」全在 store 和 localStorage 里：
  // 一次对话没有地址，发给同事只能发截图；刷新靠 localStorage，换台机器就丢；
  // 浏览器后退会直接离开整个页面而不是回到上一个对话。
  const convs = await listOf('/conversations')
  const seeded = convs.filter((c) => c.turn_count > 0)

  if (seeded.length >= 2) {
    const [a, b] = seeded
    const { page, errors } = await visit(`/chat/${a.id}`)
    check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
    check('深链接直达那个对话', (await page.locator('main').innerText()).includes(a.title),
          `想要「${a.title}」`)
    check('地址里就是那个 chatID', page.url().endsWith(`/chat/${a.id}`), page.url())

    // 点另一个：地址要跟着走，而不是只改 store
    await page.locator('aside button[title]').filter({ hasText: b.title }).first().click()
    await page.waitForTimeout(600)
    check('切换对话会改地址', page.url().endsWith(`/chat/${b.id}`), page.url())

    // 刷新靠的是 URL，不是 localStorage
    await page.reload({ waitUntil: 'networkidle' })
    await page.waitForTimeout(800)
    check('刷新后还在同一个对话', page.url().endsWith(`/chat/${b.id}`)
          && (await page.locator('main').innerText()).includes(b.title))

    // 后退回到上一个对话，而不是甩出整个页面——这条以前是彻底失效的
    await page.goBack({ waitUntil: 'networkidle' })
    await page.waitForTimeout(700)
    check('后退回到上一个对话', page.url().endsWith(`/chat/${a.id}`), page.url())
    await shot(page, 'url-chat')
    await page.close()
  } else {
    check('（跳过：库里不足两个带轮次的会话）', true)
  }

  {
    // 链接过期/对话被删：不能白屏，也不能一声不响地跳走。有别的对话就落到最近那个，
    // 一个都没有就回首屏
    const { page } = await newPage()
    await page.goto(`${WEB}/chat/这个id根本不存在`)
    // 认整句提示，不认"不存在"三个字——会话标题里碰巧有这仨字就会把它蒙对
    const times = await countNotice(page, '该对话不存在')
    await page.waitForURL((u) => !u.pathname.includes(encodeURIComponent('这个id')), { timeout: 5000 }).catch(() => {})
    const landed = new URL(page.url()).pathname
    const body = await page.locator('body').innerText()
    check('指向不存在的对话时不白屏', body.length > 40, `${body.length} 字`)
    check(convs.length ? '落到了一个真实对话' : '没有别的对话：回到首屏',
          convs.length ? convs.some((c) => landed === `/chat/${c.id}`) : landed === '/chat', page.url())
    check('说清楚了为什么换了地方，只说一次', times === 1, `提示 ${times} 次`)
    await page.close()
  }

  {
    // 数据源在第二波搬出了设置页，自成一页 /data；老链接 /settings/datasources 重定向过去
    const selected = (page) => page.getByRole('tab', { selected: true }).allInnerTexts()
    const direct = await visit('/data/databases')
    check('深链接直达数据库那一屏',
          new URL(direct.page.url()).pathname === '/data/databases'
          && (await selected(direct.page)).join() === '数据库', direct.page.url())
    await direct.page.close()
    const legacy = await visit('/settings/datasources')
    check('设置页的老链接落到同一屏',
          new URL(legacy.page.url()).pathname === '/data/databases'
          && (await selected(legacy.page)).join() === '数据库', legacy.page.url())
    await legacy.page.close()
    const bare = await visit('/settings')
    check('不带 tab 时落到第一屏并纠正地址',
          bare.page.url().endsWith('/settings/providers'), bare.page.url())
    await bare.page.close()
    const bogus = await visit('/knowledge/没这个tab')
    check('认不出的 tab 名回落而不是空白',
          bogus.page.url().endsWith('/knowledge/kb'), bogus.page.url())
    await bogus.page.close()
  }

  {
    // 画布也该能指。这条路最容易出事：前进/后退会绕过选择器里那道
    // "未保存改动"的确认直接换图
    const wfs = await listOf('/workflows')
    if (wfs.length >= 2) {
      const bare = await visit('/studio')
      check('/studio 落到第一张图并纠正地址',
            bare.page.url().endsWith(`/studio/${wfs[0].id}`), bare.page.url())
      await bare.page.close()

      const { page } = await visit(`/studio/${wfs[1].id}`)
      check('深链接直达那张图', (await page.locator('body').innerText()).includes(wfs[1].name),
            `想要「${wfs[1].name}」`)
      await page.close()
    } else {
      check(`（跳过：库里只有 ${wfs.length} 张工作流）`, true)
    }

    const { page } = await newPage()
    await page.goto(`${WEB}/studio/根本没这张图`)
    // 说一次就够。这里真弹过三次——effect 在请求回来之前又跑了两遍
    const times = await countNotice(page, '该工作流不存在')
    await page.waitForURL(/\/studio\/[0-9a-f]{8,}$/, { timeout: 5000 }).catch(() => {})
    check('指向不存在的图时说一次并让开', times === 1 && /\/studio\/[0-9a-f]{8,}$/.test(page.url()),
          `提示 ${times} 次，落在 ${page.url()}`)
    await page.close()
  }

  {
    // 问数据页的「在画布里打开」：建成一张新工作流、送去它自己的地址。
    // 以前只是把节点塞进画布，store 里的 workflow 还是上一张图，⌘S 就把那张
    // 整张覆盖了。要守的是：送到的是新图的地址、图没被别的工作流盖掉、
    // 没弹莫名其妙的"有未保存的改动"。
    //
    // 这个按钮会写库。拦下建图那次 POST，拿它自己发出去的图回一个"已建好"，
    // 之后对这张图的读取也由这里答；校验、变量分析只算不写，放行
    const DRAFT = 'c0ffee00c0ffee00c0ffee00c0ffee00'
    let seed = null
    for (const c of convs.slice(0, 8)) {
      const d = await (await fetch(`${API}/conversations/${c.id}`)).json()
      const t = (d.turns || []).find((x) => x.graph?.nodes?.length)
      if (t) { seed = { id: c.id, want: t.graph.nodes.length }; break }
    }
    if (seed) {
      const before = await listOf('/workflows')
      const { page } = await newPage()
      let dialogs = 0
      page.on('dialog', async (d) => { dialogs++; await d.dismiss() })
      let created = null
      await page.route('**/api/workflows**', (route) => {
        const r = route.request()
        const path = new URL(r.url()).pathname
        if (r.method() === 'POST' && path.endsWith('/api/workflows')) {
          const body = r.postDataJSON()
          const now = new Date().toISOString().replace('Z', '')
          created = { id: DRAFT, name: body.name, description: '', graph: body.graph, tags: [],
                      version: 1, is_template: false, status: 'draft', published_version: null,
                      created_at: now, updated_at: now, run_count: 0 }
          return route.fulfill({ status: 201, json: created })
        }
        if (path.includes(`/workflows/${DRAFT}`)) {
          if (path.endsWith('/versions')) return route.fulfill({ json: [] })
          if (r.method() === 'GET' && created) return route.fulfill({ json: created })
        }
        return route.fallback()   // 其余交给上下文那道闸门：读放行、写掐掉
      })
      await page.goto(`${WEB}/chat/${seed.id}`, { waitUntil: 'networkidle' })
      await page.waitForTimeout(900)
      const btn = page.getByRole('button', { name: '在画布里打开' }).first()
      if (await btn.count()) {
        await btn.click()
        await page.waitForURL(`**/studio/${DRAFT}`, { timeout: 5000 }).catch(() => {})
        await page.waitForTimeout(1200)
        check('「在画布里打开」建了新图、送到它自己的地址',
              !!created && page.url().endsWith(`/studio/${DRAFT}`), page.url())
        check('建的是这一轮的图，名字带着问题',
              created?.graph?.nodes?.length === seed.want && created.name.startsWith('问数据：'),
              created?.name)
        check('送过去的草稿图没被第一张工作流盖掉',
              (await page.locator('.react-flow__node').count()) === seed.want,
              `${await page.locator('.react-flow__node').count()} / ${seed.want} 个节点`)
        check('没有弹莫名其妙的"未保存改动"', dialogs === 0, `弹了 ${dialogs} 次`)
        const after = await listOf('/workflows')
        check('检查本身没往库里写图', after.length === before.length,
              `${before.length} → ${after.length}`)
      } else {
        check('（跳过：这一轮没有「在画布里打开」）', true)
      }
      await page.close()
    } else {
      check('（跳过：前 8 个会话里没有带图的轮次）', true)
    }
  }

  {
    // 单个文档 / 单个工具也要能指。文档那边原来连详情视图都没有——
    // 列表里只有标题和片段数，点不开，而切块结果（重叠、表头续接）
    // 是检索不准时第一个该看的东西
    const docs = await listOf('/kb/documents')
    const ready = docs.find((d) => d.status !== 'processing' && d.chunk_count > 0)
    if (ready) {
      const { page } = await visit(`/knowledge/kb/${ready.id}`)
      const body = await page.locator('body').innerText()
      check('深链接直达那份文档', body.includes(ready.title), ready.title)
      check('看得到切块结果', /片段 \d+/.test(body) && /\d+ 字/.test(body),
            body.slice(0, 120).replace(/\n/g, ' '))
      check('回得去列表', (await page.getByRole('button', { name: /回到知识库/ }).count()) === 1)
      await page.close()

      const gone = await visit('/knowledge/kb/根本没这份文档')
      const goneBody = await gone.page.locator('body').innerText()
      check('文档不在了要说出来而不是白屏', goneBody.includes('该文档不存在'),
            goneBody.slice(-120).replace(/\n/g, ' '))
      await gone.page.close()
    } else {
      check('（跳过：库里没有切完块的文档）', true)
    }

    const tools = await listOf('/tools')
    if (tools.length) {
      const t = tools[0]
      const { page } = await visit(`/tools/library/${t.id}`)
      const body = await page.locator('body').innerText()
      check('深链接直达那个工具', body.includes(t.name), t.name)
      check('参数说明跟着出来', body.includes('参数'), '')
      await page.close()
    }
  }

  {
    // 列表一页 50 条，往后靠「加载更多」按游标翻；一条老运行的链接必须也能打开——
    // 所以详情是按 id 直接取的，不是在已经取回来的那几页里找
    const PAGE = 50
    const all = await listOf('/runs?limit=200')
    if (all.length > PAGE) {
      const old = all[all.length - 1]
      const { page } = await visit(`/runs/${old.id}`)
      const body = await page.locator('body').innerText()
      check('第一页之外的老运行也打得开', body.includes(old.id.slice(0, 6)),
            `第 ${all.length} 条 · run ${old.id.slice(0, 8)}`)
      await page.close()
    } else {
      check(`（跳过：库里只有 ${all.length} 条运行，不足以越过第一页）`, true)
    }
  }
})

await section('样式层叠：工具类盖得过组件类（F1 回归）', async () => {
  // .field / .btn 以前不在任何 @layer 里，而 Tailwind 的工具类在 utilities 层——层外的
  // 规则永远赢，加上去的工具类全被静默吞掉：受管卡的 border-[var(--accent)] 算出来还是
  // --border，两张卡看不出选了哪个；field w-44 占满整行；btn-xs 压根没定义
  const { page, errors } = await visit('/studio')
  const publish = page.getByRole('button', { name: /^发布$/ })
  const ready = await publish.isEnabled().catch(() => false)
  check('发布入口可用（没有未保存的改动）', ready)
  if (ready) {
    await publish.click()
    const governed = page.getByRole('radio', { name: /^受管/ })
    await governed.click()
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(400)   // 卡片带 transition-colors，等它落定
      const cards = await page.locator('[role=radiogroup][aria-label="发布等级"] [role=radio]').evaluateAll((els) => {
        const probe = document.createElement('i')
        probe.style.borderTop = '1px solid var(--accent)'
        els[0].parentElement.appendChild(probe)
        const accent = getComputedStyle(probe).borderTopColor
        probe.remove()
        return els.map((el) => ({ on: el.getAttribute('aria-checked') === 'true', border: getComputedStyle(el).borderTopColor, accent }))
      })
      const on = cards.find((c) => c.on)
      const off = cards.find((c) => !c.on)
      check(`${theme}：选中「受管」后边框就是 --accent`, !!on && on.border === on.accent,
            `${on?.border} / --accent ${on?.accent}`)
      check(`${theme}：没选中的那张不是 --accent，两张看得出区别`, !!off && off.border !== off.accent, off?.border)
    }
    await page.keyboard.press('Escape')
  }

  const probe = await page.evaluate(() => {
    // 在 utilities 层里声明一个和 Tailwind 的 w-44 一模一样的宽度：守的是层的先后，
    // 不依赖源码里哪一处碰巧用了 w-44
    const style = document.createElement('style')
    style.textContent = '@layer utilities { .check-ui-w-44 { width: 11rem } }'
    document.head.appendChild(style)
    const field = document.createElement('input')
    field.className = 'field check-ui-w-44'
    const xs = document.createElement('button')
    xs.className = 'btn btn-xs'
    xs.textContent = '展开'
    const base = document.createElement('button')
    base.className = 'btn'
    document.body.append(field, xs, base)
    const f = field.getBoundingClientRect().width
    const s = getComputedStyle(xs)
    const out = { field: f, xs: s.fontSize, pad: `${s.paddingTop} ${s.paddingLeft}`, btn: getComputedStyle(base).fontSize }
    field.remove(); xs.remove(); base.remove(); style.remove()
    return out
  })
  check('field 上的 w-44 生效，窄于 200px', probe.field < 200, `${Math.round(probe.field)}px`)
  check('.btn-xs 是 11px', probe.xs === '11px', `${probe.xs}，普通 .btn ${probe.btn}`)
  check('.btn-xs 有自己的内边距，不是按普通按钮渲染', probe.pad === '2px 6px', probe.pad)
  check('没有运行时报错', errors.length === 0, errors.join(' | '))

  // 真页面上挂着宽度工具类的 .field 都按工具类的宽度画（w-N = N × 4px）
  for (const path of ['/tools/sandbox', '/knowledge/memory']) {
    const { page: p } = await visit(path)
    const fields = await p.locator('.field').evaluateAll((els) => els
      .map((el) => ({ w: Number(el.className.match(/(?:^|\s)w-(\d+)(?:\s|$)/)?.[1]), got: el.getBoundingClientRect().width }))
      .filter((x) => x.w))
    const off = fields.filter((x) => Math.abs(x.got - x.w * 4) > 1)
    check(`${path}：.field 上的宽度工具类都生效`, fields.length > 0 && off.length === 0,
          fields.map((x) => `w-${x.w}=${Math.round(x.got)}px`).join(' '))
    await p.close()
  }
})

await section('三处讲的是同一个故事', async () => {
  // 同一次运行，运行页详情和 preview 里的 dense 渲染应该给出同一批步骤。
  // 这是"全站一个翻译层"的实际含义——分两套的话用户没法判断哪个是真的。
  const { page } = await newPage()
  await page.goto(`${WEB}/preview.html?case=loop_approve`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(400)
  const wide = await page.locator('body').innerText()
  const asks = (wide.match(/这条公告可以发吗/g) ?? []).length
  // 宽栏和窄栏各渲染一遍，所以是 3×2
  check('宽窄两栏各三轮审批，一轮不少', asks === 6, `${asks} 处`)
  check('决定折进了同一行而不是另起一行',
    !/^已批准$/m.test(wide) && !/^已驳回$/m.test(wide))
})

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 界面端到端全部通过')
process.exit(failed ? 1 : 0)
