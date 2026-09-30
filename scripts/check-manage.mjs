// 管理页（工具 / 知识 / 数据 / 设置）的交互回归。
//
// 这几页的毛病都不在"渲染不出来"，而在交互的细处：写操作失败一声不响、中文
// 输入法选词的回车把半句话写进长期记忆、原生 confirm 不说后果、测完连接只剩
// 4 秒 toast、Oracle 的 service_name 绑错了字段、传完表格回显一帧都看不到……
// 页面级的 check-ui 走的都是正常路径，这些一个字都不会说。
//
// 不写库：GET 放行到后端（读真实数据），其余一律 page.route 拦下伪造——断言
// 请求体对不对，再回一个像样的响应。所以对哪个后端跑都不会改数据。
// 跑之前前后端都得起着（./scripts/dev.sh），默认连 5273 / 8000。对别的实例（比如一份
// 沙箱拷贝）跑时带上地址：
//   AGENTLAB_WEB=http://localhost:<前端端口> AGENTLAB_API=http://localhost:<后端端口>/api node scripts/check-manage.mjs
// 截图：CHECK_SHOTS=/某个目录 时把关键状态存下来；CHECK_THEME=light 换浅色跑一遍。
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const API = process.env.AGENTLAB_API ?? 'http://localhost:8000/api'
const SHOTS = process.env.CHECK_SHOTS ?? ''
// 交互检查跑在哪套主题上（截图用）：dark / light
const THEME = process.env.CHECK_THEME === 'light' ? 'light' : 'dark'
// 不用 playwright install：它既下不动也会动到已有缓存。系统 Chrome 就够了。
const CHROME = process.env.CHROME_PATH
  ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}
const get = (p) => fetch(`${API}${p}`).then((r) => r.json())

let providers, sources, tools, formats, embedding, memories, collections
try {
  ;[providers, sources, tools, formats, embedding, memories, collections] = await Promise.all([
    get('/providers'), get('/datasources'), get('/tools'), get('/kb/formats'), get('/kb/embedding'),
    get('/memory?scope=default'), get('/kb/collections'),
  ])
} catch (e) {
  console.error(`✗ 连不上后端（${API}）——先起沙箱后端\n  ${e.message}`)
  process.exit(1)
}

const browser = await chromium.launch({ executablePath: CHROME })

/**
 * 上传进度靠浏览器的 xhr.upload 事件，而 page.route 拦下的请求 Chrome 一个进度事件
 * 都不报（真请求会报，已实测）。所以在页面里替 XHR 报：发 /upload 时先报 40%，
 * 0.7 秒后报发完，再真正 send（被 page.route 接住）。发之前 abort 的，照浏览器的
 * 样子报一次 abort。只测界面怎么接这些事件，不碰后端
 */
function fakeUploadProgress() {
  const open = XMLHttpRequest.prototype.open
  const send = XMLHttpRequest.prototype.send
  const abort = XMLHttpRequest.prototype.abort
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__url = String(url)
    return open.call(this, method, url, ...rest)
  }
  XMLHttpRequest.prototype.abort = function () {
    if (this.__pending) {
      this.__pending = false
      this.dispatchEvent(new ProgressEvent('abort'))
      return
    }
    return abort.call(this)
  }
  XMLHttpRequest.prototype.send = function (body) {
    if (!/\/upload(\?|$)/.test(this.__url ?? '')) return send.call(this, body)
    const total = 2_000_000
    const up = this.upload
    this.__pending = true
    const fire = (type, loaded) => up.dispatchEvent(new ProgressEvent(type, { lengthComputable: true, loaded, total }))
    setTimeout(() => { if (this.__pending) fire('progress', 800_000) }, 50)
    setTimeout(() => {
      if (!this.__pending) return
      this.__pending = false
      fire('progress', total)
      fire('load', total)
      send.call(this, body)
    }, 700)
  }
}

/**
 * 开一页：GET 放行，写请求交给 handlers（按「METHOD 路径正则」匹配），没配的一律
 * 拦成 503——既不写库，也顺带检验「写失败有反馈」。所有原生对话框都算失败。
 */
async function open(path, { handlers = [], theme = THEME, viewport = { width: 1280, height: 860 }, uploadProgress = false } = {}) {
  const ctx = await browser.newContext({ viewport, colorScheme: theme, timezoneId: 'Asia/Shanghai' })
  opened.add(ctx)
  // 找不到元素就早点失败：默认 30 秒一项，一处点不到能拖住整节半分钟
  ctx.setDefaultTimeout(6000)
  ctx.setDefaultNavigationTimeout(30000)
  await ctx.addInitScript((t) => {
    try { localStorage.setItem('agentlab.theme', t); localStorage.removeItem('agentlab.health') } catch { /* noop */ }
  }, theme)
  if (uploadProgress) await ctx.addInitScript(fakeUploadProgress)
  const page = await ctx.newPage()
  const sent = []
  const errors = []
  const natives = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('dialog', (d) => { natives.push(`${d.type()}: ${d.message().slice(0, 40)}`); void d.dismiss() })
  await page.route((u) => new URL(u).pathname.startsWith('/api/'), async (route) => {
    const r = route.request()
    const url = new URL(r.url())
    const key = `${r.method()} ${url.pathname.replace(/^\/api/, '')}`
    for (const [pattern, handle] of handlers) {
      if (pattern.test(key)) {
        let body = null
        try { body = r.postDataJSON() } catch { body = r.postData() }
        sent.push({ key, url: url.pathname + url.search, body })
        return handle(route, { body, url, key })
      }
    }
    if (r.method() === 'GET') return route.continue()
    sent.push({ key, url: url.pathname + url.search, body: null, blocked: true })
    return route.fulfill({ status: 503, body: '' })
  })
  await page.goto(`${WEB}${path}`, { waitUntil: 'networkidle' })
  // App 会拿后端 settings 里的主题覆盖一次：钉回来，截图才是想看的那一套
  await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
  await page.waitForTimeout(400)
  return { page, ctx, sent, errors, natives, close: () => { opened.delete(ctx); return ctx.close() } }
}
// 页内换地址：App 会拿后端 settings 里的主题再覆盖一次，跟 open() 一样钉回来
const goto = async (page, path) => {
  await page.goto(`${WEB}${path}`, { waitUntil: 'networkidle' })
  await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), THEME)
  await page.waitForTimeout(300)
}
const json = (data, status = 200) => (route) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) })
/** 多久之前的服务器时间（后端列表里的 last_checked_at 就是这种带时区的 ISO） */
const ago = (ms) => new Date(Date.now() - ms).toISOString()
/** 等到条件成立或超时，返回最后一次的值 */
const until = async (fn, ms = 8000, step = 200) => {
  const end = Date.now() + ms
  let v = await fn()
  while (!v && Date.now() < end) { await new Promise((r) => setTimeout(r, step)); v = await fn() }
  return v
}
const delayed = (ms, data, status = 200) => async (route) => { await new Promise((r) => setTimeout(r, ms)); return json(data, status)(route) }
const shot = async (page, name) => { if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png` }) }
const text = (page) => page.locator('main').innerText()
const dialog = (page) => page.locator('[role="dialog"]').last()
/** 元素正中那一点最上层的就是它自己：在视口里、没被滚走也没被盖住 */
const inView = (locator) => locator.evaluate((el) => {
  const r = el.getBoundingClientRect()
  const hit = document.elementFromPoint(r.left + Math.min(24, r.width / 2), r.top + r.height / 2)
  return !!hit && el.contains(hit)
}).catch(() => false)
const composingEnter = (locator) => locator.evaluate((el) => {
  el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 229, isComposing: true, bubbles: true, cancelable: true }))
})

/**
 * 一节一节地跑。某个元素找不到（点击超时）以前会抛出未捕获的异常，整个脚本就停在
 * 那里，后面各节一项都不跑——变异测试也就证明不了后面那些检查抓得住回归。现在
 * 这一节记一项失败、关掉它开的页面，接着跑下一节。
 * 只跑其中几节：CHECK_ONLY=知识库,工具 node scripts/check-manage.mjs（按节名包含匹配）
 */
const ONLY = process.env.CHECK_ONLY?.split(',').map((x) => x.trim()).filter(Boolean)
const opened = new Set()
async function section(name, fn) {
  if (ONLY?.length && !ONLY.some((k) => name.includes(k))) return
  console.log(`\n=== ${name} ===`)
  try {
    await fn()
  } catch (e) {
    check(`${name} 中途出错`, false, String(e?.message ?? e).split('\n')[0])
  } finally {
    for (const c of opened) await c.close().catch(() => {})
    opened.clear()
  }
}

try {
  const probe = await open('/data')
  const overlay = await probe.page.locator('vite-error-overlay').count()
  await probe.close()
  if (overlay) {
    console.error('✗ 页面上是 vite 的报错层——有文件正在改，等一会儿再跑')
    process.exit(1)
  }
} catch (e) {
  console.error(`✗ 打不开前端（${WEB}）——先起沙箱前端\n  ${e.message}`)
  process.exit(1)
}

await section('页面骨架：页头、标签、地址', async () => {
  for (const [path, title] of [['/tools', '工具'], ['/knowledge', '知识'], ['/data', '数据'], ['/settings', '设置']]) {
    const { page, errors, close } = await open(path)
    const h1 = await page.locator('main h1').first().innerText().catch(() => '')
    const box = await page.locator('main header').first().boundingBox()
    check(`${path} 有页头「${title}」`, h1 === title, h1)
    check(`${path} 页头不高于 48px（toast 从 56px 起）`, !!box && box.height <= 48, box ? `${box.height}px` : '没有页头')
    check(`${path} 标签页有 tablist 语义`, await page.locator('main [role="tablist"]').count() === 1)
    check(`${path} 没有运行时报错`, errors.length === 0, errors[0] ?? '')
    await close()
  }
  {
    const { page, close } = await open('/settings')
    const tabs = await page.locator('main [role="tab"]').allInnerTexts()
    check('设置页不再有数据源标签', !tabs.some((t) => t.includes('数据源')), tabs.join(' / '))
    await close()
    const old = await open('/settings/datasources')
    check('/settings/datasources 跳到 /data', new URL(old.page.url()).pathname.startsWith('/data'), old.page.url())
    await old.close()
    const data = await open('/data')
    const dtabs = await data.page.locator('main [role="tab"]').allInnerTexts()
    check('/data 有「数据库」「表格」两个标签', dtabs.join('|') === '数据库|表格', dtabs.join(' / '))
    check('/data 落到 /data/databases', data.page.url().endsWith('/data/databases'), data.page.url())
    await data.close()
  }
})

await section('设置 · 模型接入', async () => {
  const p0 = providers[0]
  // 后端记着上次测连接的结果（last_check_*）：第一张卡 3 小时前测过、没通过，其余没测过
  const listed = providers.map((p, i) => (i === 0 ? {
    ...p, last_checked_at: ago(3 * 3600_000), last_check_ok: false, last_latency_ms: null,
    last_error: '鉴权失败（401）：服务方拒绝了当前 API Key',
  } : { ...p, last_checked_at: null, last_check_ok: null, last_latency_ms: null, last_error: null }))
  const { page, sent, natives, errors, close } = await open('/settings/providers', {
    handlers: [
      [/^GET \/providers$/, json(listed)],
      [/^POST \/providers\/[^/]+\/test$/, delayed(300, { ok: true, latency_ms: 128, model: p0?.default_model ?? 'm', reply: 'pong' })],
      [/^POST \/providers\/test$/, json({ ok: false, error: '测试未通过：无法连接服务：网络不通或服务未启动', hint: '请检查地址和端口，确认服务已启动且本机可以访问', detail: 'OpenAIConnectionError: Connection error.' })],
      [/^POST \/providers\/models$/, json({ ok: true, models: ['qwen-max', 'qwen-plus'], url: 'x' })],
    ],
  })
  if (p0) {
    const card = page.locator(`[data-provider="${p0.name}"]`)
    const first = await card.locator('[data-health]').innerText()
    check('卡片初值是后端记着的上次结果：连接失败 · 3 小时前测试（换了浏览器也在）', /连接失败.*3 小时前测试/.test(first), first)
    check('……原因也写在卡片上', (await card.innerText()).includes('服务方拒绝了当前 API Key'))
    const p1 = providers[1]
    if (p1) {
      check('后端没测过的卡片写「未测试」',
            (await page.locator(`[data-provider="${p1.name}"] [data-health]`).getAttribute('data-health')) === 'idle')
    }
    const disabled = providers.find((p) => !p.enabled)
    if (disabled) {
      check('停用单独标「已停用」，不再借圆点表达',
            (await page.locator(`[data-provider="${disabled.name}"]`).innerText()).includes('已停用'))
    }
    await card.getByRole('button', { name: /测试/ }).click()
    await page.waitForTimeout(80)
    check('测试中状态点在转、写已用时间', (await card.locator('[data-health]').getAttribute('data-health')) === 'checking')
    await page.waitForTimeout(600)
    const pill = await card.locator('[data-health]').innerText()
    check('本机刚测的比后端记的新，用本机的：连接成功 · 128 ms · 刚刚测试', /连接成功.*128 ms.*刚刚测试/.test(pill), pill)
    await shot(page, 'providers-tested')
    await page.getByRole('tab', { name: '运行环境' }).click()
    await page.waitForTimeout(300)
    await page.getByRole('tab', { name: '模型接入' }).click()
    await page.waitForTimeout(300)
    const again = await page.locator(`[data-provider="${p0.name}"] [data-health]`).innerText()
    check('切走再回来，测试结果还在', again.includes('连接成功'), again)
  }

  await page.getByRole('button', { name: /添加接入/ }).first().click()
  const dlg = dialog(page)
  await page.waitForTimeout(200)
  const kinds = dlg.locator('[role="radiogroup"] [role="radio"]')
  const checkedAt = await kinds.evaluateAll((els) => els.findIndex((el) => el.getAttribute('aria-checked') === 'true'))
  check('类型单选组只占一个 Tab 位（落在选中项上）',
        (await kinds.evaluateAll((els) => els.filter((el) => el.tabIndex === 0).length)) === 1
        && (await kinds.nth(checkedAt).getAttribute('tabindex')) === '0')
  await kinds.nth(checkedAt).focus()
  await page.keyboard.press('ArrowRight')
  const movedTo = await kinds.evaluateAll((els) => els.findIndex((el) => el.getAttribute('aria-checked') === 'true'))
  check('……→ 选中下一种，焦点跟过去', movedTo === (checkedAt + 1) % await kinds.count()
        && await kinds.nth(movedTo).evaluate((el) => el === document.activeElement), `${checkedAt} → ${movedTo}`)
  await dlg.getByRole('radio', { name: /OpenAI 兼容/ }).click()
  check('选中的类型卡 aria-checked，且带勾', (await dlg.getByRole('radio', { name: /OpenAI 兼容/ }).getAttribute('aria-checked')) === 'true'
        && await dlg.locator('[role="radio"][aria-checked="true"] svg').count() > 0)
  const name = await dlg.locator('input').first().inputValue()
  check('自动名称用短名，不带一长串括号', name === 'OpenAI 兼容', name)
  const saveBtn = dlg.getByRole('button', { name: '保存' })
  check('Base URL 没填时保存不可点，并说缺什么', await saveBtn.isDisabled() && ((await saveBtn.getAttribute('title')) ?? '').includes('Base URL'))
  const urlInput = dlg.getByLabel(/Base URL/)
  check('Base URL 的占位只放示例地址，「必填」交给标签上的 *', !((await urlInput.getAttribute('placeholder')) ?? '').includes('必填'))
  await urlInput.fill('http://127.0.0.1:9/v1')
  await dlg.getByRole('button', { name: /测试连接/ }).click()
  await page.waitForTimeout(400)
  const testReq = sent.find((s) => s.key === 'POST /providers/test')
  check('弹窗里测的是未保存的草稿（providers.testConfig）', testReq?.body?.kind === 'openai_compatible' && testReq?.body?.base_url === 'http://127.0.0.1:9/v1', JSON.stringify(testReq?.body ?? {}).slice(0, 120))
  const body = await dlg.innerText()
  check('测试失败的原因和怎么办留在弹窗里', body.includes('无法连接服务') && body.includes('请检查地址'))
  await dlg.getByRole('button', { name: /从端点拉取模型/ }).click()
  await page.waitForTimeout(300)
  await dlg.locator('[data-fetched-models] button', { hasText: 'qwen-max' }).click()
  check('拉到的模型点一下就加进可选模型', (await dlg.getByLabel('去掉模型 qwen-max').count()) === 1)
  const manual = dlg.getByLabel('手动添加模型 id')
  await manual.fill('pinyin')
  await composingEnter(manual)
  check('模型 id 框：输入法组字时的回车不提交', (await dlg.getByLabel('去掉模型 pinyin').count()) === 0)
  await shot(page, 'provider-editor')
  await page.keyboard.press('Escape')
  await page.waitForTimeout(200)
  check('填了一半按 Esc 先问「放弃修改」', (await dlg.innerText()).includes('有未保存的修改'))
  await dlg.getByRole('button', { name: '放弃修改' }).click()
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('设置 · 模型接入：后端校验没过', async () => {
  // FastAPI 的 422 是 pydantic 的英文原文 + 内部键名。弹窗上说中文、按表单叫法点字段，原文收进「详情」（验收 NEW）
  const detail = [{ type: 'missing', loc: ['body', 'name'], msg: 'Field required', input: {} }]
  const { page, sent, errors, close } = await open('/settings/providers', {
    handlers: [[/^POST \/providers$/, json({ detail }, 422)]],
  })
  await page.getByRole('button', { name: /添加接入/ }).first().click()
  const dlg = dialog(page)
  await dlg.getByRole('radio', { name: /演示模型/ }).click()
  await dlg.getByRole('button', { name: '保存' }).click()
  const alert = page.locator('[role="alert"][aria-live="assertive"]').first()
  const said = await until(async () => { const t = await alert.innerText().catch(() => ''); return t.includes('不符合要求') ? t : '' }, 4000)
  check('保存发出去了', sent.some((s) => s.key === 'POST /providers'))
  check('toast 说中文：哪个字段、怎么了', said.includes('提交的内容不符合要求') && said.includes('「名称」为必填项'), said.replace(/\s+/g, ' '))
  const summary = said.split('详情')[0]
  check('……英文原文和内部键名不在正文里', !/Field required|\bname\b/.test(summary), summary.replace(/\s+/g, ' '))
  await alert.getByText('详情').first().click()
  const raw = await alert.locator('details pre').first().innerText().catch(() => '')
  check('……英文原文收在「详情」里，给维护者复制', raw.includes('Field required'), raw.slice(0, 80))
  check('弹窗还开着，填的东西没丢', await dlg.isVisible())
  await shot(page, 'provider-422')
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('设置 · 偏好', async () => {
  // 设置读写都走假的一份：PUT 写进去，GET 读回来（主题的「已保存」是读回来核对过才写的）
  let stored = await get('/settings')
  const { page, sent, natives, close } = await open('/settings/prefs', {
    handlers: [
      [/^GET \/settings$/, (route) => json(stored)(route)],
      [/^PUT \/settings$/, (route, { body }) => { stored = { ...stored, ...body.values }; return json(stored)(route) }],
    ],
  })
  const themeRadio = (label) => page.getByRole('radiogroup', { name: '主题' }).getByRole('radio', { name: label })
  const checkedTheme = () => page.getByRole('radiogroup', { name: '主题' }).locator('[aria-checked="true"]').innerText()
  // 选一个和这次要截图的主题一致的值（浅色跑选「跟随系统」，上下文的 colorScheme
  // 是浅色）；它已经是选中的就先绕一下别的，点选中项不会发请求
  const [label, value] = THEME === 'dark' ? ['深色', 'dark'] : ['跟随系统', 'system']
  if ((await themeRadio(label).getAttribute('aria-checked')) === 'true') {
    await themeRadio('浅色').click()
    await page.waitForTimeout(400)
  }
  await themeRadio(label).click()
  await page.waitForTimeout(500)
  const put = sent.filter((s) => s.key === 'PUT /settings').at(-1)
  // 细节只报组名和主题：settings 里别的组存着沙箱的真实配置，日志常被贴进报告
  check('主题选中即保存，只 PUT ui 一组', !!put && Object.keys(put.body?.values ?? {}).join() === 'ui' && put.body.values.ui.theme === value,
        `组：${Object.keys(put?.body?.values ?? {}).join(',') || '（没发）'} · theme=${put?.body?.values?.ui?.theme ?? '—'}`)
  check('主题旁显示「已保存」（读回来核对过）', (await page.locator('[data-theme-save]').innerText()).includes('已保存'))
  await page.waitForTimeout(1600)
  check('……约 1.6 秒后淡出，不一直挂着', await page.getByText('已保存', { exact: true }).count() === 0
        && await page.locator('[data-theme-save="saved"]').count() === 0)

  // REQ：从导航（或 ⌘K）换主题时偏好页开着，选项要跟着变，不能还显示旧值
  const resolvedBefore = await page.evaluate(() => {
    const a = document.documentElement.getAttribute('data-theme')
    return a === 'light' || a === 'dark' ? a : (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark')
  })
  await page.getByRole('button', { name: /^切换到(浅色|深色)主题$/ }).click()
  await page.waitForTimeout(500)
  const synced = await checkedTheme()
  check('从导航换主题：偏好页的主题选项跟着变', synced.includes(resolvedBefore === 'dark' ? '浅色' : '深色'), synced)
  await themeRadio(label).click()
  await page.waitForTimeout(500)

  check('默认知识库是下拉，选项来自已有知识库',
        await page.locator('#pref-collection').evaluate((el) => el.tagName) === 'SELECT'
        && (await page.locator('#pref-collection option').allInnerTexts()).some((t) => t.startsWith(collections[0]?.collection ?? 'default')))
  check('危险工具开关写明只影响探索运行', (await text(page)).includes('只影响探索运行'))
  check('没改时没有保存条', await page.locator('[data-prefs-bar]').count() === 0)
  await page.locator('#pref-actor').fill('检查脚本')
  check('改了署名：底部出现「有 1 项未保存」', (await page.locator('[data-prefs-bar]').innerText()).includes('有 1 项未保存'))
  await shot(page, 'prefs-dirty')

  // 左侧导航离开：先问
  await page.locator('nav a[href="/tools"]').first().click()
  await page.waitForTimeout(300)
  check('有未保存的改动时点导航先问', (await dialog(page).innerText().catch(() => '')).includes('还没保存'))
  await dialog(page).getByRole('button', { name: '返回保存' }).click()
  await page.waitForTimeout(200)
  check('选「留下」就还在偏好页', page.url().endsWith('/settings/prefs'), page.url())

  // 切标签只问一次：以前 change() 先问一遍，守卫在地址变化时又问一遍
  const asking = () => page.locator('[role="dialog"]').filter({ hasText: '还没保存' }).count()
  await page.getByRole('tab', { name: '模型接入' }).click()
  await page.waitForTimeout(400)
  check('有改动时切标签：只弹一个确认框', await asking() === 1, `${await asking()} 个`)
  await dialog(page).getByRole('button', { name: '返回保存' }).click()
  await page.waitForTimeout(400)
  check('……选「留下」：标签和地址都不变，也没有第二个框冒出来', page.url().endsWith('/settings/prefs')
        && (await page.getByRole('tab', { name: '偏好设置' }).getAttribute('aria-selected')) === 'true' && await asking() === 0, page.url())
  // ⌘K 和 ⌥ 数字不走 <a>：以前页面自己在捕获阶段拦 <a> 的点击，这两条路拦不住
  const mod = process.platform === 'darwin' ? 'Meta' : 'Control'
  await page.keyboard.press(`${mod}+KeyK`)
  await page.getByRole('dialog', { name: '命令面板' }).getByRole('combobox').fill('工具')
  await page.keyboard.press('Enter')
  await page.waitForTimeout(400)
  check('⌘K 跳页也先问', await asking() === 1 && page.url().endsWith('/settings/prefs'), page.url())
  await dialog(page).getByRole('button', { name: '返回保存' }).click()
  await page.waitForTimeout(300)
  await page.evaluate(() => document.activeElement?.blur())
  await page.keyboard.press('Alt+Digit1')
  await page.waitForTimeout(400)
  check('⌥1 切页也先问', await asking() === 1 && page.url().endsWith('/settings/prefs'), page.url())
  await dialog(page).getByRole('button', { name: '返回保存' }).click()
  await page.waitForTimeout(300)
  check('……都选「留下」：改动还在', (await page.locator('#pref-actor').inputValue()) === '检查脚本')

  const putsBefore = sent.filter((s) => s.key === 'PUT /settings').length
  await page.getByRole('button', { name: /保存设置/ }).click()
  await page.waitForTimeout(250)
  check('只改署名：存在本机就行，不发 PUT', sent.filter((s) => s.key === 'PUT /settings').length === putsBefore)
  check('保存后说「已保存」', (await page.locator('[data-prefs-bar]').innerText().catch(() => '')).includes('已保存'))
  check('署名写进本机', await page.evaluate(() => localStorage.getItem('agentlab_actor')) === '检查脚本')
  const initial = await page.locator('nav a[href="/settings/prefs"]').first().innerText().catch(() => '')
  check('……导航底部的署名首字立刻换成「检」（同一个标签页也收得到）', initial.trim() === '检', initial)

  await page.locator('#pref-confirm-hint').locator('xpath=ancestor::label//input').click()
  await page.getByRole('button', { name: /保存设置/ }).click()
  await page.waitForTimeout(400)
  const put2 = sent.filter((s) => s.key === 'PUT /settings').at(-1)
  // run 组里是默认知识库、记忆作用域这些沙箱里的真实名字：只报组名
  check('改了运行默认值：保存只 PUT run 一组', Object.keys(put2?.body?.values ?? {}).join() === 'run',
        `组：${Object.keys(put2?.body?.values ?? {}).join(',') || '（没发）'}`)

  // 有改动时切标签选「放弃修改」：一问就走，不再补问第二遍
  await page.locator('#pref-actor').fill('再改一次')
  await page.getByRole('tab', { name: '运行环境' }).click()
  await page.waitForTimeout(400)
  await dialog(page).getByRole('button', { name: '放弃修改' }).click()
  await page.waitForTimeout(500)
  check('切标签选「放弃修改」：直接到运行环境，不再问第二遍', page.url().endsWith('/settings/system') && await asking() === 0,
        `${page.url()} · 还开着 ${await asking()} 个`)
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  await close()

  // 保存失败要看得见（以前 pageerror 一条，界面毫无变化）
  const failing = await open('/settings/prefs')
  await failing.page.locator('#pref-actor').fill('x')
  await failing.page.locator('#pref-confirm-hint').locator('xpath=ancestor::label//input').click()
  await failing.page.getByRole('button', { name: /保存设置/ }).click()
  await failing.page.waitForTimeout(600)
  const bar = await failing.page.locator('[data-prefs-bar]').innerText()
  check('保存失败：条上写「保存失败」', bar.includes('保存失败'))
  check('……署名只在本机，已经存上了；条上只剩危险工具的默认审批策略一项', bar.includes('有 1 项未保存') && bar.includes('危险工具的默认审批策略') && !bar.includes('署名'), bar.replace(/\s+/g, ' '))
  check('保存失败不抛未捕获异常', failing.errors.length === 0, failing.errors[0] ?? '')
  await failing.close()

  // 断线时在偏好页换的主题：先在本机生效，恢复后自动补存，而不是被服务端的旧值翻回去。
  // 断线要断得像真的：/health 也够不着，catalog 才会判断开、恢复时才算「重连」
  let offline = false
  let puts = 0
  let stored2 = await get('/settings')
  const cut = (route) => route.abort('internetdisconnected')
  const off = await open('/settings/prefs', {
    handlers: [
      [/^GET \/settings$/, (route) => (offline ? cut(route) : json(stored2)(route))],
      [/^PUT \/settings$/, (route, { body }) => {
        puts++
        if (offline) return cut(route)
        stored2 = { ...stored2, ...body.values }
        return json(stored2)(route)
      }],
      [/^GET \//, (route) => (offline ? cut(route) : route.continue())],
    ],
  })
  const offRadio = (l) => off.page.getByRole('radiogroup', { name: '主题' }).getByRole('radio', { name: l })
  const target = (await offRadio('浅色').getAttribute('aria-checked')) === 'true' ? ['深色', 'dark'] : ['浅色', 'light']
  offline = true
  await offRadio(target[0]).click()
  await off.page.waitForTimeout(800)
  check('断线时换主题：说清先在本机生效、连上后自动存', (await off.page.locator('body').innerText()).includes('连接服务端后将自动保存到设置'))
  check('……选项和页面都已经是新主题', (await offRadio(target[0]).getAttribute('aria-checked')) === 'true'
        && await off.page.evaluate(() => document.documentElement.getAttribute('data-theme')) === target[1])
  const down = await until(() => off.page.locator('[data-offline-banner]').count().then((n) => n > 0), 6000)
  check('……整站也判了断开（横幅出来了）', !!down)
  const putsOffline = puts
  offline = false
  await until(() => stored2?.ui?.theme === target[1], 20000)
  await off.page.waitForTimeout(400)
  check('……后端恢复后自动补存这个主题', puts > putsOffline && stored2?.ui?.theme === target[1], `PUT ${puts} 次，存的是 ${stored2?.ui?.theme}`)
  check('……没有被服务端的旧值翻回去', await off.page.evaluate(() => document.documentElement.getAttribute('data-theme')) === target[1]
        && (await offRadio(target[0]).getAttribute('aria-checked')) === 'true')
  await off.close()
})

await section('设置 · 门控模型', async () => {
  // 设置读写都走假的一份；接入列表也换成假的两家（日志里只出现这些通用名）
  let stored = await get('/settings')
  stored = { ...stored, run: { ...(stored.run ?? {}), tool_gate_provider: null, tool_gate_model: null } }
  const scope0 = stored.run.default_memory_scope ?? 'default'
  const PROVIDERS = [
    { id: 'check-gate-p1', name: 'demo-main', kind: 'openai', base_url: 'https://example.com/v1', default_model: 'big-model',
      models: [{ id: 'big-model' }], enabled: true, extra: {}, api_key_masked: '', has_key: true },
    { id: 'check-gate-p2', name: 'demo-small', kind: 'openai', base_url: 'https://example.com/v1', default_model: 'tiny-1',
      models: [{ id: 'tiny-1' }, { id: 'tiny-2' }], enabled: true, extra: {}, api_key_masked: '', has_key: true },
  ]
  const { page, sent, errors, close } = await open('/settings/prefs', {
    handlers: [
      [/^GET \/settings$/, (route) => json(stored)(route)],
      [/^PUT \/settings$/, (route, { body }) => { stored = { ...stored, ...body.values }; return json(stored)(route) }],
      [/^GET \/providers$/, json(PROVIDERS)],
    ],
  })
  const box = page.locator('[data-gate-model]')
  const hint = await box.innerText().catch(() => '')
  check('运行默认值里有「门控模型」，提示留空用默认、建议选小模型', hint.includes('门控模型') && hint.includes('留空') && hint.includes('小模型'),
    hint.replace(/\s+/g, ' ').slice(0, 100))
  const provider = page.getByLabel('门控模型 · 接入')
  const model = page.getByLabel('门控模型 · 模型')
  check('……留空时写明默认是哪家（第一个启用的接入）', (await provider.locator('option').first().innerText()).includes('demo-main'))
  await provider.selectOption('demo-small')
  await model.fill('tiny-2')
  const bar = await page.locator('[data-prefs-bar]').innerText().catch(() => '')
  check('……改了两项：保存条写「门控模型的接入、门控模型」', bar.includes('有 2 项未保存') && bar.includes('门控模型的接入') && bar.includes('门控模型'),
    bar.replace(/\s+/g, ' '))
  await page.getByRole('button', { name: /保存设置/ }).click()
  await page.waitForTimeout(500)
  const put = sent.filter((s) => s.key === 'PUT /settings').at(-1)
  const run = put?.body?.values?.run ?? {}
  // run 组里别的键是沙箱的真实配置：只报门控这两项
  check('保存只 PUT run 一组，写 tool_gate_provider / tool_gate_model', Object.keys(put?.body?.values ?? {}).join() === 'run'
    && run.tool_gate_provider === 'demo-small' && run.tool_gate_model === 'tiny-2',
    `provider=${run.tool_gate_provider ?? '—'} · model=${run.tool_gate_model ?? '—'}`)
  check('……其余运行默认值原样带着', run.default_memory_scope === scope0 && 'confirm_dangerous_tools' in run)
  await shot(page, 'prefs-gate-model')

  // 换接入：原来填的模型不在新接入里就清掉；都清空保存成 null（= 用默认接入的默认模型）
  await provider.selectOption('demo-main')
  check('换了接入，旧接入的模型清掉', await model.inputValue() === '', await model.inputValue())
  await provider.selectOption('')
  await page.getByRole('button', { name: /保存设置/ }).click()
  await page.waitForTimeout(500)
  const run2 = sent.filter((s) => s.key === 'PUT /settings').at(-1)?.body?.values?.run ?? {}
  check('……两项都留空：存成 null', run2.tool_gate_provider === null && run2.tool_gate_model === null,
    `provider=${JSON.stringify(run2.tool_gate_provider)} · model=${JSON.stringify(run2.tool_gate_model)}`)
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()

  // 老后端：run 组里没有这两项，读进来就是空，不算改动
  let legacy = await get('/settings')
  legacy = { ...legacy, run: Object.fromEntries(Object.entries(legacy.run ?? {}).filter(([k]) => !k.startsWith('tool_gate_'))) }
  const old = await open('/settings/prefs', { handlers: [[/^GET \/settings$/, (route) => json(legacy)(route)], [/^GET \/providers$/, json(PROVIDERS)]] })
  check('老后端没有这两项：显示空、没有保存条', await old.page.getByLabel('门控模型 · 模型').inputValue() === ''
    && await old.page.locator('[data-prefs-bar]').count() === 0)
  await old.close()
})

await section('设置 · 智能体护栏', async () => {
  // 默认步数、token 预算、金额预算（后端 engine/guards.py）。预算留空 = 不限，存 null
  let stored = await get('/settings')
  stored = { ...stored, run: { ...(stored.run ?? {}), agent_max_steps: 100, agent_budget_tokens: 2000000, agent_budget_usd: null },
    limits: { ...(stored.limits ?? {}), max_agent_steps: 100 } }
  const { page, sent, errors, close } = await open('/settings/prefs', {
    handlers: [
      [/^GET \/settings$/, (route) => json(stored)(route)],
      [/^PUT \/settings$/, (route, { body }) => { stored = { ...stored, ...body.values }; return json(stored)(route) }],
    ],
  })
  const box = page.locator('[data-agent-guard]')
  const text = (await box.innerText().catch(() => '')).replace(/\s+/g, ' ')
  check('运行默认值里有「Agent 护栏」，说清不再被固定步数掐断', text.includes('Agent 护栏') && text.includes('基于已获取的信息收尾'),
    text.slice(0, 100))
  const steps = page.getByLabel('默认最大步数')
  const tokens = page.getByLabel('token 预算（每个节点）')
  const usd = page.getByLabel('金额预算（美元）')
  check('……读回设置里的值：100 步、200 万 token、金额不限', await steps.inputValue() === '100'
    && await tokens.inputValue() === '2000000' && await usd.inputValue() === '')
  check('……金额留空写明「不限」', text.includes('不限：费用仅受最大步数和上下文长度约束'))

  await steps.fill('500')
  const save = page.getByRole('button', { name: /保存设置/ })
  check('步数超过硬上限：标红、保存按钮不可用', await save.isDisabled()
    && (await box.innerText()).includes('请填写 1 到 100 之间的整数'))
  await steps.fill('60')
  await tokens.fill('')
  await usd.fill('1.5')
  const bar = (await page.locator('[data-prefs-bar]').innerText().catch(() => '')).replace(/\s+/g, ' ')
  check('……改了三项：保存条写明是哪三项', bar.includes('有 3 项未保存') && bar.includes('默认最大步数')
    && bar.includes('token 预算') && bar.includes('金额预算'), bar)
  await save.click()
  await page.waitForTimeout(500)
  const run = sent.filter((s) => s.key === 'PUT /settings').at(-1)?.body?.values?.run ?? {}
  check('保存：步数写数字，token 留空存 null（不限），金额写数字', run.agent_max_steps === 60
    && run.agent_budget_tokens === null && run.agent_budget_usd === 1.5,
    `steps=${JSON.stringify(run.agent_max_steps)} tokens=${JSON.stringify(run.agent_budget_tokens)} usd=${JSON.stringify(run.agent_budget_usd)}`)
  await tokens.fill('500')
  check('token 预算少于 1000：标红、不能保存', await save.isDisabled())
  await shot(page, 'prefs-agent-guard')
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()

  // 存着的步数比硬上限大（硬上限后来调小了）：没动它就不挡别的设置保存，后端发起运行时自己会截
  const tight = { ...stored, run: { ...stored.run, agent_max_steps: 100 }, limits: { ...stored.limits, max_agent_steps: 25 } }
  const t = await open('/settings/prefs', { handlers: [[/^GET \/settings$/, (route) => json(tight)(route)],
    [/^PUT \/settings$/, (route) => json(tight)(route)]] })
  await t.page.getByLabel('金额预算（美元）').fill('2')
  check('存着的步数超过硬上限、这次没改它：只改金额照样能保存',
    !(await t.page.getByRole('button', { name: /保存设置/ }).isDisabled()))
  await t.close()
})

await section('设置 · 证据裁判', async () => {
  // 结论句裁判的模型和各项上限（后端 engine/judge.py）：每一项都能设成不限（存 null），不限时写明还受什么约束。
  // 读写照运行默认值：GET 读、PUT 整组写。写请求在浏览器层拦下，不落库
  let stored = await get('/settings')
  stored = { ...stored, judge: { provider: null, model: null, report_max_claims: 40, report_max_cost_usd: null, report_timeout_s: 30,
    click_max_cost_usd: 0.01, daily_max_usd: 2 } }
  let spendGets = 0
  const { page, sent, errors, close } = await open('/settings/prefs', {
    handlers: [
      [/^GET \/settings$/, (route) => json(stored)(route)],
      [/^PUT \/settings$/, (route, { body }) => { stored = { ...stored, ...body.values }; return json(stored)(route) }],
      [/^GET \/settings\/judge\/spend$/, (route) => { spendGets += 1; return json({ date: '2026-09-29', usd: 0.0123, calls: 7, unpriced_calls: 2, daily_max_usd: 2 })(route) }],
    ],
  })
  const box = page.locator('[data-judge-settings]')
  const all = (await box.innerText().catch(() => '')).replace(/\s+/g, ' ')
  check('偏好里有「证据裁判」一组，写明判断是模型给的、不是系统核对', all.includes('证据裁判') && all.includes('非确定'), all.slice(0, 80))
  check('裁判模型：接入和模型，提示建议和写作模型不同', await page.locator('#pref-judge-provider').count() === 1
    && await page.locator('#pref-judge-model').count() === 1 && all.includes('建议与撰写报告的模型不同'))
  check('……接入留空时跟随助手的模型', (await page.locator('#pref-judge-provider option').first().innerText()).includes('跟随助手的模型'))
  const val = (id) => page.locator(`#${id}`).inputValue()
  check('读回各项上限：40 句、30 秒、每次点击 $0.01、每日 $2', await val('pref-judge-claims') === '40' && await val('pref-judge-timeout') === '30'
    && await val('pref-judge-click') === '0.01' && await val('pref-judge-daily') === '2')
  const cost = page.locator('[data-judge-limit="report_max_cost_usd"]')
  check('存着 null 的每份报告金额：勾着「不限」、框变灰', await cost.locator('[data-judge-unlimited-toggle]').isChecked()
    && await page.locator('#pref-judge-cost').isDisabled())
  const costNote = await cost.locator('[data-judge-unlimited]').innerText().catch(() => '')
  check('……写明「不设上限，费用仅受……约束」', costNote === '不设上限，费用仅受句数上限、时长上限、每日上限约束', costNote)
  const spend = await page.locator('[data-judge-spend]').innerText().catch(() => '')
  check('每日上限旁边写今天花了多少', spend.includes('今日已花费 $0.0123（7 次调用）'), spend)
  check('……估不出金额的调用另说', spend.includes('另有 2 次调用无法估算金额') && await page.locator('[data-judge-unpriced]').count() === 1, spend)

  const daily = page.locator('[data-judge-limit="daily_max_usd"]')
  await daily.locator('[data-judge-unlimited-toggle]').check()
  const dailyNote = await daily.locator('[data-judge-unlimited]').innerText().catch(() => '')
  check('每日上限设成不限：写明每日费用还受什么约束', dailyNote.startsWith('不设上限，每日费用仅受') && dailyNote.includes('每次点击的金额上限'), dailyNote)
  const costNote2 = await cost.locator('[data-judge-unlimited]').innerText().catch(() => '')
  check('……每日也不限了：每份报告金额那句不再说「每日上限」', !costNote2.includes('每日') && costNote2.startsWith('不设上限'), costNote2)
  await page.locator('#pref-judge-click').fill('0')
  const save = page.getByRole('button', { name: /保存设置/ })
  check('每次点击填 0：标红、保存按钮不可用', await save.isDisabled()
    && (await page.locator('[data-judge-limit="click_max_cost_usd"]').innerText()).includes('请填写大于 0 的金额，或勾选「不限」'))
  await page.locator('#pref-judge-click').fill('0.02')
  check('……接入留空时模型框的占位也写跟随助手的模型', await page.locator('#pref-judge-model').getAttribute('placeholder') === '留空：跟随助手的模型')
  await page.locator('#pref-judge-model').fill('judge-model-b')
  // 只填了模型：设置这一级整组生效（judge_model_spec 不跨级拼），后端按模型名找接入——已经不跟随助手的模型
  const provFirst = await page.locator('#pref-judge-provider option').first().innerText().catch(() => '')
  check('只填了模型：接入留空那一项不再叫「跟随助手的模型」，改叫「按模型名找接入」', provFirst === '按模型名找接入', provFirst)
  const byModel = page.locator('[data-judge-by-model]')
  const byText = await byModel.innerText().catch(() => '')
  // 说明里有沙箱的接入名：细节只报认没认出来，不打印原文
  check('……写明按模型名找接入（认不出就去调默认接入），「跟随助手的模型」只在两项都留空时生效',
    await byModel.getAttribute('data-judge-by-model').catch(() => '') === 'default' && byText.includes('只填了模型')
    && byText.includes('默认接入') && byText.includes('「跟随助手的模型」仅在接入和模型都留空时生效'),
    `data-judge-by-model=${await byModel.getAttribute('data-judge-by-model').catch(() => '（没有）')}`)
  const bar = (await page.locator('[data-prefs-bar]').innerText().catch(() => '')).replace(/\s+/g, ' ')
  check('保存条写明改了哪几项', bar.includes('有 3 项未保存') && bar.includes('每日金额上限') && bar.includes('每次点击的金额上限')
    && bar.includes('裁判模型'), bar)
  const gets = spendGets
  await save.click()
  await page.waitForTimeout(500)
  const put = sent.filter((s) => s.key === 'PUT /settings').at(-1)?.body?.values ?? {}
  const j = put.judge ?? {}
  check('保存：整组写 judge（七项都在），不限存 null', Object.keys(j).sort().join(',')
    === 'click_max_cost_usd,daily_max_usd,model,provider,report_max_claims,report_max_cost_usd,report_timeout_s'
    && j.daily_max_usd === null && j.report_max_cost_usd === null && j.click_max_cost_usd === 0.02 && j.report_max_claims === 40
    && j.report_timeout_s === 30 && j.model === 'judge-model-b' && j.provider === null, JSON.stringify(j))
  check('……只改了裁判：不碰运行默认值那一组', !('run' in put), Object.keys(put).join(','))
  check('……存完再取一次今天的花费（每日上限变了）', spendGets > gets, `${gets} → ${spendGets}`)
  check('……存完显示「已保存」', (await page.locator('[data-prefs-bar]').innerText().catch(() => '')).includes('已保存'))
  await daily.locator('[data-judge-unlimited-toggle]').uncheck()
  check('勾掉「不限」：填回原来的数', await val('pref-judge-daily') === '2')
  await daily.locator('[data-judge-unlimited-toggle]').check()
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  if (SHOTS) {
    await box.scrollIntoViewIfNeeded()
    await page.waitForTimeout(200)
    await box.screenshot({ path: `${SHOTS}/prefs-judge-${THEME}.png` })
  }
  await close()

  // 老后端没有 judge 这一组：不显示，也不写
  const legacy = { ...stored }
  delete legacy.judge
  const old = await open('/settings/prefs', { handlers: [[/^GET \/settings$/, (route) => json(legacy)(route)]] })
  check('老后端没有裁判设置：这一组不出现', await old.page.locator('[data-judge-settings]').count() === 0)
  await old.close()
})

await section('数据 · 数据库', async () => {
  const dbs = sources.filter((s) => !/[\\/]uploads[\\/]tables[\\/]/.test(s.database ?? ''))
  const first = dbs.find((s) => s.table_count > 0) ?? dbs[0]
  const oracle = dbs.find((s) => s.kind === 'oracle')
  const noCheck = { last_checked_at: null, last_check_ok: null, last_latency_ms: null, last_error: null }
  // 假库一：配置里的 schema 探不出对象；后端记着 2 小时前测连接没通过。顺带是个免密库
  // （has_password:false），验证密码不再拦保存
  const empty = {
    id: 'check-manage-empty', name: 'zz_probe', kind: 'postgres', host: '10.0.0.9', port: null, database: 'mes',
    username: 'reader', options: { schema: 'ods', sslmode: 'disable' }, readonly: true, description: '检查脚本的假库', enabled: true,
    password_masked: '', has_password: false, table_count: 0, schema_synced_at: null, cached_schema: 'ods',
    schema_error: 'NoSuchTableError: ods', available_schemas: ['mes', 'ods', 'public'], tools: ['db_query__zz_probe'],
    last_checked_at: ago(2 * 3600_000), last_check_ok: false, last_latency_ms: null, last_error: '连接失败：用户名或密码错误',
  }
  // 假库二：配置改成了 sales，缓存还是按 legacy 探的——助手看到的和配置对不上
  const drifted = {
    ...empty, ...noCheck, id: 'check-manage-drift', name: 'zz_drift', options: { schema: 'sales' }, cached_schema: 'legacy',
    table_count: 2, schema_error: '', available_schemas: [], schema_synced_at: ago(3600_000), tools: ['db_query__zz_drift'],
  }
  let cur = empty
  let drift = drifted
  const { page, sent, natives, errors, close } = await open('/data/databases', {
    handlers: [
      [/^GET \/datasources$/, (route) => json([...sources, cur, drift])(route)],
      [/^GET \/datasources\/check-manage-empty\/schema$/, json({ tables: ['mes.work_order', 'mes.line', 'mes.shift'], summary: '', synced_at: ago(0) })],
      // 取单张表时回老后端的样子：只有 detail 文本，没有 found、没有 columns
      [/^GET \/datasources\/check-manage-drift\/schema$/, (route, { url }) => json(url.searchParams.get('table')
        ? { table: url.searchParams.get('table'), detail: '表 sales.orders\n  order_id  INTEGER\n  amount  REAL' }
        : { tables: ['sales.orders', 'sales.sales_daily'], summary: '', synced_at: ago(0) })(route)],
      [/^POST \/datasources$/, json({ detail: '查询时限需要填写 1 到 600 之间的秒数，例如 60；当前为「0」' }, 422)],
      [/^POST \/datasources\/check-manage-empty\/introspect$/, (route, { url }) => {
        const schema = url.searchParams.get('schema')
        if (url.searchParams.get('dry_run') === 'true') {
          return json(schema === 'mes'
            ? { dry_run: true, schema: 'mes', table_count: 3, tables: ['mes.work_order', 'mes.line', 'mes.shift'], truncated: false, total: 3, schema_error: '', available_schemas: empty.available_schemas }
            : { dry_run: true, schema, table_count: 0, tables: [], truncated: false, total: 0, schema_error: '', available_schemas: empty.available_schemas })(route)
        }
        const hit = cur.options.schema === 'mes'
        cur = { ...cur, cached_schema: cur.options.schema ?? '', table_count: hit ? 3 : 0, schema_error: hit ? '' : empty.schema_error,
                schema_synced_at: hit ? ago(0) : cur.schema_synced_at }
        return json(cur)(route)
      }],
      [/^POST \/datasources\/check-manage-drift\/introspect$/, (route) => {
        drift = { ...drift, cached_schema: drift.options.schema, schema_synced_at: ago(0) }
        return json(drift)(route)
      }],
      // 后端的规矩：连接配置（地址、账号、库）改了，上次测连接的结果作废
      [/^PATCH \/datasources\/check-manage-empty$/, (route, { body }) => {
        const reconnect = ['host', 'port', 'database', 'username'].some((k) => k in (body ?? {}) && body[k] !== cur[k]) || 'password' in (body ?? {})
        cur = { ...cur, ...body, options: body?.options ?? cur.options, ...(reconnect ? noCheck : {}) }
        return json(cur)(route)
      }],
      // 回得慢一点：「测连接期间只说一句」要在结果回来之前读两次播报区。250ms 在机器
      // 负载高时第一次读就已经是结果了，这项时过时不过
      [/^POST \/datasources\/[^/]+\/test$/, delayed(1500, { ok: true, elapsed_ms: 42, url: 'x' })],
      [/^POST \/datasources\/test$/, json({ ok: false, error: '连接失败：用户名或密码错误', hint: '请核对「用户名」和「密码」；编辑时密码留空表示沿用已保存的密码', detail: 'ORA-01017' })],
      [/^POST \/datasources\/[^/]+\/introspect/, (route, { url }) => {
        const id = url.pathname.split('/')[3]
        const row = sources.find((s) => s.id === id)
        return json({ ...row, schema_synced_at: new Date().toISOString() })(route)
      }],
    ],
  })
  if (!first) {
    check('（跳过：沙箱里没有数据库）', true)
  } else {
    const card = page.locator(`[data-source="${first.name}"]`)
    // 地址是沙箱库里真实的连接串（主机、账号、库名）：日志常被贴进报告，只报出问题的条数
    const addrs = await page.locator('article[data-source] .mono').allInnerTexts()
    const tails = addrs.filter((a) => /:\s*$/.test(a))
    check('地址不留尾巴冒号', !tails.length, tails.length ? `${tails.length} 条（内容不打出来）` : '')
    await card.getByRole('button', { name: /测试连接/ }).click()
    // 先等到真在测，再隔 1 秒读两次：计时每 100ms 一跳，播报区要是跟着念就对不上
    await card.locator('[data-health="checking"]').first().waitFor({ timeout: 3000 })
    const spoken0 = await card.locator('[role="status"]').first().innerText().catch(() => '')
    await page.waitForTimeout(700)
    const spoken1 = await card.locator('[role="status"]').first().innerText().catch(() => '')
    check('测连接期间播报区只说一句「正在测试连接」，不跟着计时一跳一跳地念',
          spoken0 === '正在测试连接' && spoken1 === spoken0, `${spoken0} / ${spoken1}`)
    check('……看得见的胶囊不是播报区（里面有计时）',
          (await card.locator('[data-health]').first().getAttribute('aria-hidden')) === 'true'
          && (await card.locator('[data-health]').first().getAttribute('role')) === null)
    await until(() => card.locator('[data-health]').first().getAttribute('data-health').then((v) => v !== 'checking'), 5000)
    const pill = await card.locator('[data-health]').first().innerText()
    check('测连接的结果留在卡片上：连接成功 · 42 ms', /连接成功.*42 ms/.test(pill), pill)
    const spoken = await card.locator('[role="status"]').first().innerText()
    check('……播报区说结果和钟点，不说「几分钟前」', /^连接成功 42 ms，测试于 \d{1,2}:\d{2}$/.test(spoken), spoken)
    check('卡片上写着结构同步于何时', (await card.innerText()).includes('结构同步于'))
    if (first.table_count > 0) {
      await card.getByRole('button', { name: /个对象/ }).click()
      await page.waitForTimeout(700)
      const rows = card.locator('[data-schema-browser] button[aria-expanded]')
      const n = await rows.count()
      check('表清单一行一张表', n === Math.min(first.table_count, 300), `${n} 行 / ${first.table_count} 个对象`)
      await rows.first().click()
      await page.waitForTimeout(800)
      const cols = await card.locator('[data-schema-browser] tbody tr').count()
      check('点表名懒加载出逐列信息（后端给的结构化列）', cols > 0, `${cols} 列`)
      const scroller = card.locator('[data-schema-browser] .overflow-y-auto')
      await scroller.evaluate((el) => { el.scrollTop = 60 })
      const before = await scroller.evaluate((el) => el.scrollTop)
      await card.getByRole('button', { name: /探查结构/ }).click()
      await page.waitForTimeout(900)
      check('探查完不整页 Spinner：结构浏览器还开着', await card.locator('[data-schema-browser]').count() === 1)
      check('……展开的那张表也还开着', await card.locator('[data-schema-browser] button[aria-expanded="true"]').count() >= 1)
      const after = await card.locator('[data-schema-browser] .overflow-y-auto').evaluate((el) => el.scrollTop).catch(() => -1)
      check('……滚动位置没跳回顶部', Math.abs(after - before) < 5, `${before} → ${after}`)
      await shot(page, 'datasource-schema')
    }

    // 「预览其他 schema」只看不存，好好的库也给；单文件的 SQLite 没有 schema 可换
    check(first.kind === 'sqlite' ? 'SQLite 不给「预览其他 schema」' : '结构正常的库也能「预览其他 schema」（只看不存，没有风险）',
          await card.locator('[data-probe-schema]').count() === (first.kind === 'sqlite' ? 0 : 1))

    await card.getByRole('button', { name: `删除数据源 ${first.name}` }).click()
    const del = dialog(page)
    const delText = await del.innerText()
    check('删除数据源：说明后果（工具、工作流）', delText.includes('工具') && delText.includes('工作流'))
    check('删除数据源：要照抄名字才能确认', await del.getByRole('button', { name: '删除数据源' }).isDisabled())
    await del.getByRole('button', { name: '取消' }).click()
    check('取消后没有发删除', !sent.some((s) => s.key.startsWith('DELETE')))
  }

  {
    const ec = page.locator(`[data-source="${empty.name}"]`)
    // 全局数据源目录（store/catalog）每重拉一次就是一个 GET /datasources；页面自己的列表只在进页时拉
    const lists = () => sent.filter((s) => s.key === 'GET /datasources').length
    const pill0 = await ec.locator('[data-health]').innerText()
    check('卡片初值是后端记着的上次测连接：连接失败 · 2 小时前测试', /连接失败.*2 小时前测试/.test(pill0), pill0)
    check('……原因写在卡片上', (await ec.innerText()).includes('用户名或密码错误'))

    // 预览其他 schema：只看不存（dry_run），缓存和配置都不动
    check('探查失败的库给「预览其他 schema」', await ec.locator('[data-probe-schema]').count() === 1)
    check('失败框的候选里不含配置里现有的那个（ods），只列别的', await ec.getByRole('button', { name: '预览 ods' }).count() === 0
          && await ec.getByRole('button', { name: '预览 mes' }).count() === 1)
    await ec.locator('[data-probe-schema]').click()
    const pd = dialog(page)
    const pdText = await pd.innerText()
    check('……先说清楚：只看不存，缓存和配置都不动', pdText.includes('仅预览') && pdText.includes('不修改缓存和配置'), pdText.slice(0, 120))
    await pd.locator('input').fill('mes')
    await pd.getByRole('button', { name: '预览', exact: true }).click()
    await page.waitForTimeout(500)
    const probeReq = sent.filter((s) => s.key.endsWith('/introspect')).at(-1)
    check('……请求带上 schema 和 dry_run=true（不写缓存）', !!probeReq?.url.includes('schema=mes') && probeReq.url.includes('dry_run=true'), probeReq?.url ?? '')
    const ask = dialog(page)
    const askText = await ask.innerText().catch(() => '')
    check('……探到了再问要不要写进配置，并举出探到的表名', askText.includes('mes') && askText.includes('work_order'), askText.slice(0, 160))
    check('……「不改」说的是什么都不动，不再「换回去」', askText.includes('不做任何变更') && !askText.includes('换回'), askText.slice(0, 200))
    await shot(page, 'datasource-probe-schema')
    const before = sent.length
    await ask.getByRole('button', { name: '不改', exact: true }).click()
    await page.waitForTimeout(500)
    check('……不改：一个请求都不再发（以前要再探一次把缓存换回来）', sent.length === before, sent.slice(before).map((r) => r.url).join(' | '))
    check('……卡片还是配置里那份（探查失败、0 个对象）', (await ec.innerText()).includes('上次探查失败'))

    // 失败框里的候选 chip：探到了选「改」就写进配置，再按新配置真探一次
    await ec.getByRole('button', { name: '预览 mes' }).click()
    await page.waitForTimeout(500)
    const listsBeforeSwitch = lists()
    await dialog(page).getByRole('button', { name: '改成 mes' }).click()
    await page.waitForTimeout(600)
    check('……卡片上换了 schema、重探了：全局的数据源目录也跟着重拉（检查器、问数据读它的 schema 和结构）',
          lists() > listsBeforeSwitch, `${listsBeforeSwitch} → ${lists()}`)
    const patch = sent.filter((s) => s.key === 'PATCH /datasources/check-manage-empty').at(-1)
    check('……选「改成 mes」：PATCH 写进 options.schema，别的连接参数原样带上',
          patch?.body?.options?.schema === 'mes' && patch?.body?.options?.sslmode === 'disable', JSON.stringify(patch?.body ?? {}))
    const real = sent.filter((s) => s.key.endsWith('check-manage-empty/introspect')).at(-1)
    check('……再按新配置真探一次（不带 dry_run）', !!real && !real.url.includes('dry_run') && !real.url.includes('schema='), real?.url ?? '')
    const ecText = await ec.innerText()
    check('……卡片换成新 schema 的结构', /3\s*个对象/.test(ecText) && !ecText.includes('上次探查失败'), ecText.replace(/\s+/g, ' ').slice(0, 160))

    // 本机刚测的比后端记的新；只改说明不动它，改了连接配置就作废
    await ec.getByRole('button', { name: /测试连接/ }).click()
    const tested = await until(async () => {
      const t = await ec.locator('[data-health]').innerText()
      return /连接成功.*42 ms/.test(t) ? t : ''
    }, 5000)
    check('本机刚测过：卡片换成本机的结果', !!tested)
    await ec.getByRole('button', { name: '编辑' }).click()
    const ed = dialog(page)
    await ed.getByLabel('说明', { exact: true }).fill('检查脚本的假库（改过说明）')
    const saveBtn = ed.getByRole('button', { name: '保存' })
    check('免密库改说明：保存可点，不再要「还缺：密码」',
          await saveBtn.isEnabled() && !((await saveBtn.getAttribute('title')) ?? '').includes('密码'),
          (await saveBtn.getAttribute('title')) ?? '')
    check('……密码框下软提示「只有免密登录的库才能这样连」', (await ed.innerText()).includes('免密登录'))
    await shot(page, 'datasource-nopw-edit')
    const listsBefore = lists()
    if (await saveBtn.isEnabled()) await saveBtn.click()
    else await ed.getByRole('button', { name: '取消' }).click()
    await page.waitForTimeout(400)
    check('……保存后全局的数据源目录跟着重拉（检查器挑工具、问数据的范围读它）', lists() > listsBefore, `${listsBefore} → ${lists()}`)
    const saved = sent.filter((s) => s.key === 'PATCH /datasources/check-manage-empty').at(-1)
    check('……保存时不带 password（不动它）', !!saved && !('password' in (saved.body ?? {})) && saved.body?.description?.includes('改过说明'),
          JSON.stringify(saved?.body ?? {}).slice(0, 120))
    check('……只改说明：刚测的结果还作数', /连接成功/.test(await ec.locator('[data-health]').innerText()))
    await ec.getByRole('button', { name: '编辑' }).click()
    await dialog(page).getByLabel(/主机/).fill('10.0.0.10')
    await dialog(page).getByRole('button', { name: '保存' }).click()
    await page.waitForTimeout(400)
    const after = await ec.locator('[data-health]').innerText()
    check('……换了主机：之前测的不再代表它，回到「未测试」', after.includes('未测试'), after)

    // 缓存和配置对不上：常驻一行 warn，一键按配置重探
    const dc = page.locator(`[data-source="${drifted.name}"]`)
    const driftText = await dc.locator('[data-schema-drift]').innerText().catch(() => '')
    check('缓存按 legacy 探、配置写着 sales：卡片上常驻一行说对不上', driftText.includes('legacy') && driftText.includes('sales'), driftText)
    await shot(page, 'datasource-drift')
    await dc.getByRole('button', { name: '按配置重新探查' }).click()
    await page.waitForTimeout(500)
    const re = sent.filter((s) => s.key === 'POST /datasources/check-manage-drift/introspect').at(-1)
    check('……点「按配置重新探查」：按配置真探（不带 schema、不带 dry_run）', !!re && !re.url.includes('?'), re?.url ?? '')
    check('……探完那行 warn 就消失', await dc.locator('[data-schema-drift]').count() === 0)

    // 老后端取单张表只给 detail 文本（没有 found）：原样显示，不能误报成「缓存里没有这张表」
    await dc.getByRole('button', { name: /个对象/ }).click()
    await page.waitForTimeout(500)
    await dc.locator('[data-schema-browser] button[aria-expanded]').first().click()
    await page.waitForTimeout(500)
    const oldCols = await dc.locator('[data-schema-browser]').innerText()
    check('老后端的列信息：照原文显示，不说「缓存中已没有这张表」', oldCols.includes('amount') && oldCols.includes('REAL')
          && !oldCols.includes('缓存中已没有这张表'), oldCols.replace(/\s+/g, ' ').slice(0, 160))
  }

  {
    // SQLite 也有高级连接参数（查询时限）；填错被 422 拒掉时写在那一项下面，不只弹 toast
    await page.getByRole('button', { name: /接入数据库/ }).first().click()
    const nd = dialog(page)
    await nd.getByLabel('类型').selectOption('sqlite')
    await nd.getByLabel(/^标识/).fill('zz_sqlite_demo')
    await nd.getByLabel(/数据库文件路径/).fill('/tmp/zz_sqlite_demo.db')
    const timeout = nd.locator('#ds-adv-query_timeout_s')
    check('SQLite 表单有「查询时限（秒）」', await timeout.count() === 1)
    if (await timeout.count()) {
      if (!(await timeout.isVisible())) await nd.getByText('高级连接参数').click()
      await timeout.fill('0')
      await nd.getByRole('button', { name: '保存' }).click()
      await page.waitForTimeout(500)
      const err = await nd.locator('#ds-adv-query_timeout_s-error').innerText().catch(() => '')
      check('……查询时限填错：后端的那句话写在这一项下面', err.includes('1 到 600'), err)
      check('……输入框标成出错，焦点落在它上面', (await timeout.getAttribute('aria-invalid')) === 'true'
            && await timeout.evaluate((el) => el === document.activeElement))
      check('……不再只弹 toast', !(await page.locator('[aria-live="assertive"]').innerText().catch(() => '')).includes('1 到 600'))
      await shot(page, 'datasource-sqlite-timeout-error')
      // 照人的改法：退格删掉 0 再敲 30。删空的那一下高级参数一项都不剩，以前 <details> 跟着
      // 收起，焦点掉到 body，后面敲的字落空（fill 一步到位，不经过空值，抓不到）
      await timeout.press('Backspace')
      await page.waitForTimeout(150)
      check('……改了这一项，出错提示就收起', await nd.locator('#ds-adv-query_timeout_s-error').count() === 0)
      const advOpen = () => timeout.evaluate((el) => !!el.closest('details')?.open)
      check('……删空那一下：高级连接参数不收起，焦点还在这一项上', await advOpen()
            && await timeout.evaluate((el) => el === document.activeElement),
            `${await advOpen() ? '开着' : '收起了'} · 焦点在 ${await page.evaluate(() => document.activeElement?.id || document.activeElement?.tagName)}`)
      await page.keyboard.type('30')
      check('……接着敲 30 就填进这一项', (await timeout.inputValue()) === '30', `框里是「${await timeout.inputValue()}」`)
    }
    await nd.getByRole('button', { name: '取消' }).click()
    await page.waitForTimeout(200)
    const discard = dialog(page).getByRole('button', { name: '放弃修改' })
    if (await discard.count()) await discard.click()
  }

  if (oracle) {
    await page.locator(`[data-source="${oracle.name}"]`).getByRole('button', { name: '编辑' }).click()
    const dlg = dialog(page)
    const target = dlg.locator('#ds-oracle-target')
    const svc = oracle.options?.service_name ?? oracle.database ?? ''
    // 回显的是沙箱里真实的服务名：对不上也只说对不上，不把两边的值打出来
    check('Oracle 编辑框回显 service_name（以前绑在 database 上，显示为空）', (await target.inputValue()) === svc,
          (await target.inputValue()) === svc ? '' : '回显和配置不一致（内容不打出来）')
    check('类型提示不再指向表单上没有的 options', !(await dlg.innerText()).includes('options.'))
    const svcRadio = dlg.getByRole('radio', { name: 'service_name' })
    check('service_name / SID 单选组只占一个 Tab 位',
          (await svcRadio.getAttribute('tabindex')) === '0' && (await dlg.getByRole('radio', { name: 'SID' }).getAttribute('tabindex')) === '-1')
    await svcRadio.focus()
    await page.keyboard.press('ArrowRight')
    check('……→ 切到 SID，焦点跟过去', (await dlg.getByRole('radio', { name: 'SID' }).getAttribute('aria-checked')) === 'true'
          && await dlg.getByRole('radio', { name: 'SID' }).evaluate((el) => el === document.activeElement))
    check('切到 SID：框里是 SID 的值（空）', (await target.inputValue()) === '')
    check('SID 为空时保存不可点', await dlg.getByRole('button', { name: '保存' }).isDisabled())
    await target.fill('ORCL')
    await dlg.getByRole('button', { name: /测试连接/ }).click()
    await page.waitForTimeout(400)
    const req = sent.find((s) => s.key === 'POST /datasources/test')
    // 请求体里是沙箱库真实的主机、账号、schema、说明：只报键名和判断用到的几个真假
    const reqOk = req?.body?.id === oracle.id && req?.body?.options?.sid === 'ORCL'
      && !('service_name' in (req?.body?.options ?? {})) && !('database' in (req?.body ?? {}))
    check('弹窗里先测连接：带 id、只写 options.sid、不写 database', reqOk, reqOk ? '' : !req ? '没发 POST /datasources/test'
          : `键：${Object.keys(req.body ?? {}).join(',')} · options 键：${Object.keys(req.body?.options ?? {}).join(',')}`
            + ` · id 对=${req.body?.id === oracle.id} · sid=ORCL:${req.body?.options?.sid === 'ORCL'}`)
    check('测试失败的原因留在弹窗里', (await dlg.innerText()).includes('请核对「用户名」和「密码」'))
    await dlg.getByLabel(/主机/).fill('')
    const title = (await dlg.getByRole('button', { name: '保存' }).getAttribute('title')) ?? ''
    check('缺必填项时保存不可点，悬停说缺什么', await dlg.getByRole('button', { name: '保存' }).isDisabled() && title.includes('主机'), title)
    await shot(page, 'datasource-oracle-edit')
    await dlg.getByRole('button', { name: '取消' }).click()
  } else {
    check('（跳过：沙箱里没有 Oracle 数据源）', true)
  }
  // 术语表：右栏那位叫「助手」，「Copilot」只能出现在按钮提示里
  check('正文里不写「Copilot」，统一叫「助手」', !(await text(page)).includes('Copilot'))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据 · 遮罩列（证据面板的 mask_columns）', async () => {
  // 假库：两张表，phone 已经遮上了。挑列从已探查的结构里读；写请求全部在浏览器里接住
  const base = {
    id: 'check-manage-mask', name: 'zz_mask', kind: 'postgres', host: '10.0.0.7', port: null, database: 'shop',
    username: 'reader', options: { schema: 'sales', mask_columns: ['phone'] }, readonly: true, description: '检查脚本的假库',
    enabled: true, password_masked: '', has_password: false, table_count: 2, schema_synced_at: ago(3600_000), cached_schema: 'sales',
    schema_error: '', available_schemas: [], tools: ['db_query__zz_mask'],
    last_checked_at: null, last_check_ok: null, last_latency_ms: null, last_error: null,
  }
  const upload = {
    ...base, id: 'check-manage-upmask', name: 'zz_upload_mask', kind: 'sqlite', host: null, username: null,
    database: '/tmp/agentlab/uploads/tables/zz_upload_mask.db', options: { query_timeout_s: '30' }, table_count: 1,
    tools: ['db_query__zz_upload_mask'],
  }
  let cur = base
  let up = upload
  const columns = {
    'sales.orders': ['order_id', 'amount', 'phone'],
    'sales.customers': ['id', 'customer_id', 'email', 'phone'],
    orders_upload: ['order_id', 'phone'],
  }
  const tableJson = (t) => ({ table: t, found: true, kind: 'table', comment: null, detail: '',
    columns: (columns[t] ?? []).map((name) => ({ name, type: 'TEXT' })) })
  const { page, sent, errors, natives, close } = await open('/data/databases', {
    handlers: [
      [/^GET \/datasources$/, (route) => json([...sources, cur, up])(route)],
      [/^GET \/datasources\/check-manage-mask\/schema$/, (route, { url }) => json(url.searchParams.get('table')
        ? tableJson(url.searchParams.get('table')) : { tables: ['sales.orders', 'sales.customers'], summary: '', synced_at: ago(0) })(route)],
      [/^GET \/datasources\/check-manage-upmask\/schema$/, (route, { url }) => json(url.searchParams.get('table')
        ? tableJson(url.searchParams.get('table')) : { tables: ['orders_upload'], summary: '', synced_at: ago(0) })(route)],
      [/^POST \/datasources\/check-manage-mask\/test$/, delayed(200, { ok: true, elapsed_ms: 42, url: 'x' })],
      [/^PATCH \/datasources\/check-manage-mask$/, (route, { body }) => { cur = { ...cur, ...body }; return json(cur)(route) }],
      [/^PATCH \/datasources\/check-manage-upmask$/, (route, { body }) => { up = { ...up, ...body }; return json(up)(route) }],
    ],
  })
  const card = page.locator(`[data-source="${base.name}"]`)
  check('卡片上写着遮罩了几列', (await card.locator('[data-mask-columns]').innerText().catch(() => '')).includes('遮罩 1 列')
    && await card.locator('[data-mask-columns]').getAttribute('data-mask-columns') === 'phone')
  await card.getByRole('button', { name: /测试连接/ }).click()
  await until(() => card.locator('[data-health]').first().innerText().then((t) => /连接成功/.test(t)), 5000)
  await card.getByRole('button', { name: '编辑' }).click()
  const ed = dialog(page)
  const field = ed.locator('[data-field="mask_columns"]')
  check('编辑框里有「遮罩的列」，已遮的 phone 是一个标签', await field.count() === 1
    && await field.locator('[data-mask-chip="phone"]').count() === 1)
  const boundary = await field.locator('[data-mask-boundary]').innerText().catch(() => '')
  check('写明遮罩只减少暴露、不是安全边界', boundary.includes('遮罩只减少暴露，不是安全边界'), boundary)
  check('高级连接参数里不再另摆一个遮罩的文本框（只有这一处）', await ed.locator('#ds-adv-mask_columns').count() === 0)
  const before = sent.filter((r) => r.key === 'GET /datasources/check-manage-mask/schema').length
  await ed.locator('#ds-mask-columns').focus()
  const listed = await until(async () => {
    const v = await ed.locator('#ds-mask-columns-list option').evaluateAll((els) => els.map((e) => e.value))
    return v.length ? v : null
  }, 5000)
  const reads = sent.filter((r) => r.key === 'GET /datasources/check-manage-mask/schema').length - before
  check('聚焦输入框才去读已探查的结构，候选是各表的列（已遮的 phone 不再列）', !!listed && listed.includes('email')
    && listed.includes('amount') && !listed.includes('phone') && reads === 3, `${JSON.stringify(listed)} · ${reads} 个请求`)
  const hint = await field.innerText()
  check('……说清是从已探查的几张表里挑的', hint.includes('从已探查的 2 张表中选择'), hint.replace(/\s+/g, ' ').slice(0, 160))
  await ed.locator('#ds-mask-columns').fill('email')
  await ed.locator('#ds-mask-columns').press('Enter')
  await ed.locator('#ds-mask-columns').pressSequentially('id_card')
  check('逐字敲 id_card 途中碰上列名 id 不会提前加成标签', await field.locator('[data-mask-chip="id"]').count() === 0
    && (await ed.locator('#ds-mask-columns').inputValue()) === 'id_card')
  await ed.locator('#ds-mask-columns').press('Enter')
  await ed.locator('#ds-mask-columns').fill('EMAIL')
  await ed.locator('#ds-mask-columns').press('Enter')
  await field.getByRole('button', { name: '不再遮罩 phone' }).click()
  const chips = await field.locator('[data-mask-chip]').evaluateAll((els) => els.map((e) => e.getAttribute('data-mask-chip')))
  check('回车加标签、× 去掉、大小写不同的同名列不重复加', chips.join(',') === 'email,id_card', chips.join(','))
  if (SHOTS) await shot(page, 'datasource-mask-columns')
  await ed.getByRole('button', { name: '保存' }).click()
  await page.waitForTimeout(500)
  const saved = sent.filter((r) => r.key === 'PATCH /datasources/check-manage-mask').at(-1)
  check('保存：options.mask_columns 是列名列表，schema 原样带上', JSON.stringify(saved?.body?.options?.mask_columns) === '["email","id_card"]'
    && saved?.body?.options?.schema === 'sales', JSON.stringify(saved?.body?.options ?? {}))
  check('……改遮罩列不算改连接：刚测的结果还作数', /连接成功/.test(await card.locator('[data-health]').first().innerText()))
  check('……卡片跟着变成遮罩 2 列', (await card.locator('[data-mask-columns]').innerText().catch(() => '')).includes('遮罩 2 列'))
  await card.getByRole('button', { name: '编辑' }).click()
  await dialog(page).locator('[data-field="mask_columns"]').getByRole('button', { name: '不再遮罩 email' }).click()
  await dialog(page).locator('[data-field="mask_columns"]').getByRole('button', { name: '不再遮罩 id_card' }).click()
  await dialog(page).getByRole('button', { name: '保存' }).click()
  await page.waitForTimeout(400)
  const cleared = sent.filter((r) => r.key === 'PATCH /datasources/check-manage-mask').at(-1)
  check('全部去掉：options 里不留 mask_columns 这个键', !!cleared && !('mask_columns' in (cleared.body?.options ?? {}))
    && await card.locator('[data-mask-columns]').count() === 0, JSON.stringify(cleared?.body?.options ?? {}))

  await goto(page, '/data/tables')
  const uc = page.locator(`[data-source="${upload.name}"]`)
  await uc.locator('[data-edit-mask]').click()
  const ud = dialog(page)
  await ud.locator('#ds-mask-columns').fill('phone')
  await ud.locator('#ds-mask-columns').press('Enter')
  await ud.getByRole('button', { name: '保存' }).click()
  await page.waitForTimeout(400)
  const upSaved = sent.filter((r) => r.key === 'PATCH /datasources/check-manage-upmask').at(-1)
  check('传上来的表没有编辑框：卡片上「设遮罩列」单独改，其余 options 原样带上',
    JSON.stringify(upSaved?.body) === JSON.stringify({ options: { query_timeout_s: '30', mask_columns: ['phone'] } }), JSON.stringify(upSaved?.body ?? {}))
  check('……卡片写着遮罩 1 列', (await uc.locator('[data-mask-columns]').innerText().catch(() => '')).includes('遮罩 1 列'))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据 · 表格', async () => {
  const fake = {
    source: {
      id: 'check-manage-fake', name: 'sales_demo', kind: 'sqlite', host: null, port: null,
      database: '/tmp/x/uploads/tables/sales_demo.db', username: null, options: {}, readonly: true,
      description: '', enabled: true, password_masked: '', has_password: false, table_count: 1,
      schema_synced_at: new Date().toISOString(), schema_error: '', available_schemas: [], tools: ['db_query__sales_demo'],
    },
    replaced: false,
    tables: [{ name: 'sales_demo', sheet: 'sales_demo', rows: 12, columns: [
      { name: '2026-01', type: 'REAL' }, { name: 'region', type: 'TEXT' }, { name: 'Unnamed: 2', type: 'TEXT' },
    ] }],
  }
  const { page, sent, natives, close } = await open('/data/tables', {
    uploadProgress: true,
    handlers: [
      [/^POST \/datasources\/upload$/, delayed(400, fake, 201)],
      // 传上来的表只有一两张，卡片会自动摊开结构：假的那张也得有结构可取
      // 列注释里带两个空格：以前解析 detail 文本（按两个以上空格切列），「见附表」会被切丢
      [/^GET \/datasources\/check-manage-fake\/schema$/, (route, { url }) => json(url.searchParams.get('table')
        ? {
            table: 'sales_demo', found: true, qualified: 'sales_demo', kind: 'table', comment: '按月的销售明细',
            detail: '表 sales_demo（按月的销售明细）\n  2026-01  REAL\n  region  TEXT  主键、非空、销售区域  见附表\n  Unnamed: 2  TEXT',
            columns: [
              { name: '2026-01', type: 'REAL', pk: false, not_null: false, comment: null },
              { name: 'region', type: 'TEXT', pk: true, not_null: true, comment: '销售区域  见附表' },
              { name: 'Unnamed: 2', type: 'TEXT', pk: false, not_null: false, comment: null },
            ],
          }
        : { tables: ['sales_demo'], summary: '', synced_at: fake.source.schema_synced_at })(route)],
    ],
  })
  await page.getByRole('button', { name: /传表格/ }).first().click()
  const dlg = dialog(page)
  await dlg.locator('input[type="file"]').setInputFiles({ name: 'sales_demo.csv', mimeType: 'text/csv', buffer: Buffer.from('2026-01,region\n1,east\n') })
  check('文件名自动变成数据源名', (await dlg.locator('input.mono').first().inputValue()) === 'sales_demo')
  await dlg.getByRole('button', { name: /导入/ }).click()
  await page.waitForTimeout(250)
  check('导入中按钮写已用时间', /导入中/.test(await dlg.getByRole('button', { name: /导入中/ }).innerText().catch(() => '')))
  const sending = await dlg.locator('[data-upload-progress]').innerText().catch(() => '')
  check('上传中画真实的字节进度：已传多少 / 总共多少 · 百分比', sending.includes('已传') && sending.includes('40%')
        && (await dlg.locator('[data-upload-progress] [role="progressbar"]').getAttribute('aria-valuenow').catch(() => '')) === '40',
        sending.replace(/\s+/g, ' '))
  check('……字节没发完之前可以取消', await dlg.getByRole('button', { name: '取消上传' }).count() === 1)
  await shot(page, 'table-uploading')
  await page.waitForTimeout(650)
  const processing = await dlg.locator('[data-upload-progress]').innerText().catch(() => '')
  check('……发完写「正在读表」，不把条拉满冒充完成', processing.includes('正在读表')
        && await dlg.locator('[data-upload-progress] [role="progressbar"]').count() === 0, processing.replace(/\s+/g, ' '))
  const cancel = dlg.getByRole('button', { name: '取消', exact: true })
  check('……发完之后不能取消（后端已经在建表），悬停说明为什么',
        await cancel.isDisabled() && ((await cancel.getAttribute('title')) ?? '').includes('传完'))
  await page.waitForTimeout(700)
  const res = await dialog(page).innerText()
  check('导入完弹窗不关，停在列名和类型上', res.includes('region') && res.includes('TEXT'))
  check('像数据的列名标黄并提示改表头行号', await dialog(page).locator('[data-suspicious]').count() === 2 && res.includes('第 2 行'))
  check('两个出口：列名无误 · 完成 / 表头有误 · 改行号重传',
        await dialog(page).getByRole('button', { name: '列名无误 · 完成' }).count() === 1
        && await dialog(page).getByRole('button', { name: /表头有误/ }).count() === 1)
  check('后台列表里已经有了这张表', await page.locator('[data-source="sales_demo"]').count() === 1)
  await shot(page, 'table-upload-result')
  await dialog(page).getByRole('button', { name: /表头有误/ }).click()
  await page.waitForTimeout(200)
  check('改行号重传：回到表单，行号 +1', (await dialog(page).locator('input[type="number"]').inputValue()) === '2')
  // 这次发到一半取消：后端收不全，什么都不会建
  await dialog(page).getByRole('button', { name: /导入/ }).click()
  await page.waitForTimeout(250)
  await dialog(page).getByRole('button', { name: '取消上传' }).click()
  await page.waitForTimeout(900)
  check('发到一半取消：说清什么都没建，弹窗留在表单上', (await page.locator('body').innerText()).includes('上传已取消')
        && await dialog(page).getByRole('button', { name: /导入/ }).isEnabled())
  await dialog(page).getByRole('button', { name: '取消' }).click()
  await page.waitForTimeout(300)
  // 结构浏览器的列清单用后端给的结构化列，不再解析 detail 文本
  const tcard = page.locator('[data-source="sales_demo"]')
  await tcard.locator('[data-schema-browser] button[aria-expanded]').first().click()
  await page.waitForTimeout(500)
  const region = tcard.locator('[data-schema-browser] tbody tr', { hasText: 'region' })
  const cells = await region.locator('td').allInnerTexts()
  check('列清单：类型、主键、非空、说明各归各的格子', cells.length === 3 && cells[1] === 'TEXT'
        && await region.locator('[aria-label="主键"]').count() === 1 && cells[2].includes('非空'), cells.join(' | '))
  check('……列注释里的两个空格不再把说明切丢（「见附表」还在）', cells[2]?.includes('销售区域') && cells[2]?.includes('见附表'), cells[2] ?? '')
  check('……表注释写在列表上方', (await tcard.locator('[data-schema-browser]').innerText()).includes('按月的销售明细'))
  await shot(page, 'table-columns')
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('上传只发了一次', sent.filter((s) => s.key === 'POST /datasources/upload').length === 1)
  await close()
})

await section('知识库', async () => {
  const { page, sent, natives, errors, close } = await open('/knowledge/kb', {
    uploadProgress: true,
    handlers: [[/^POST \/kb\/upload$/, delayed(1200, {
      id: 'fake-doc', collection: 'default', title: 'note.md', source: 'note.md', mime: 'text/markdown',
      chunk_count: 0, status: 'processing', meta: { progress: { done: 1, total: 4 } },
    }, 201)]],
  })
  const accept = await page.locator('input[data-kb-upload]').getAttribute('accept')
  check('上传框的 accept 取自 /kb/formats', accept === formats.accept, accept ?? '')
  check('……放行 .pptx，不放 .csv', accept?.includes('.pptx') && !accept?.includes('.csv'))
  const alpha = Number(await page.locator('input[data-alpha]').inputValue())
  check('检索调试台的 α 初值是运行时的 default_alpha', alpha === embedding.default_alpha, `${alpha} vs ${embedding.default_alpha}`)
  await page.locator('input[data-alpha]').fill('0.2')
  await page.waitForTimeout(100)
  check('偏离运行时值要标出来', (await page.locator('[data-alpha-off]').count()) === 1)
  await page.locator('[data-alpha-off] button', { hasText: '重置' }).click()
  check('一键回到运行时值', Number(await page.locator('input[data-alpha]').inputValue()) === embedding.default_alpha)

  // 一次选两份：一次传一个，第二份排队；排队的能取消
  await page.locator('input[data-kb-upload]').setInputFiles([
    { name: 'note.md', mimeType: 'text/markdown', buffer: Buffer.from('# hi') },
    { name: 'more.md', mimeType: 'text/markdown', buffer: Buffer.from('# more') },
  ])
  await page.waitForTimeout(300)
  check('上传中列表顶上有占位行，一份一行', await page.locator('[data-uploading]').count() === 2)
  check('上传按钮写着上传中', (await page.getByRole('button', { name: /上传中/ }).count()) === 1)
  const first = page.locator('[data-uploading]', { hasText: 'note.md' })
  const sendingRow = await first.innerText()
  check('……在传的那份画真实的字节进度', sendingRow.includes('已传') && sendingRow.includes('40%')
        && await first.locator('[role="progressbar"]').count() === 1, sendingRow.replace(/\s+/g, ' '))
  const second = page.locator('[data-uploading]', { hasText: 'more.md' })
  check('……第二份写「排队中」，还能取消', (await second.innerText()).includes('排队中')
        && await second.getByRole('button', { name: '取消上传 more.md' }).count() === 1)
  await shot(page, 'kb-uploading')
  await second.getByRole('button', { name: '取消上传 more.md' }).click()
  await page.waitForTimeout(200)
  check('……排队的取消了立刻从列表拿掉', await page.locator('[data-uploading]', { hasText: 'more.md' }).count() === 0)
  await page.waitForTimeout(500)
  const processingRow = await first.innerText()
  check('……字节发完写「处理中」（正在解析和切块），不把条拉满冒充完成',
        processingRow.includes('正在解析和切块') && await first.locator('[role="progressbar"]').count() === 0,
        processingRow.replace(/\s+/g, ' '))
  check('……发完的那份不给取消（后端已经在切块）', await page.getByRole('button', { name: '取消上传 note.md' }).count() === 0)
  await page.waitForTimeout(1500)
  check('传完占位行消失', await page.locator('[data-uploading]').count() === 0)
  check('……取消的那份没有发出去', sent.filter((s) => s.key === 'POST /kb/upload').length === 1)

  await page.locator('input[data-kb-upload]').setInputFiles({ name: 'data.csv', mimeType: 'text/csv', buffer: Buffer.from('a,b\n1,2') })
  await page.waitForTimeout(300)
  const toData = page.getByRole('button', { name: '去数据源上传表格' })
  check('表格被拦下，toast 给「去数据源上传表格」', await toData.count() === 1)
  check('……表格不会发到知识库', sent.filter((s) => s.key === 'POST /kb/upload').length === 1)
  await toData.click()
  await page.waitForTimeout(300)
  check('……点了就到 /data/tables', page.url().endsWith('/data/tables'), page.url())
  await goto(page, '/knowledge/kb')

  await page.getByRole('button', { name: '更换' }).click()
  await page.getByRole('button', { name: /本地哈希向量/ }).click()
  const dlg = dialog(page)
  const body = await dlg.innerText().catch(() => '')
  check('切本地哈希先确认，并写出影响范围', body.includes('段知识') && body.includes('条记忆'))
  check('……要照抄「本地哈希」才能确认', await dlg.getByRole('button', { name: '切换到本地哈希' }).isDisabled())
  await page.waitForTimeout(300)
  await shot(page, 'kb-local-confirm')
  await dlg.getByRole('button', { name: '取消' }).click()
  check('……取消了就什么都没发', !sent.some((s) => s.key === 'PUT /kb/embedding'))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('知识库 · 检索贡献条与重建进度', async () => {
  const hits = {
    query: '出勤', collection: 'default', degraded: [], alpha: embedding.default_alpha ?? 0.5,
    results: [
      { chunk_id: 'c1', document_id: 'd1', title: 'hr_attendance.md', ordinal: 3, content: '出勤率 = 实际出勤 / 计划出勤',
        score: 0.42, signals: { vector: 0.6, keyword: 3.21 }, contrib: { vector: 0.3, keyword: 0.12 } },
      { chunk_id: 'c2', document_id: 'd1', title: 'hr_attendance.md', ordinal: 4, content: '计划出勤按排班表算',
        score: 0.25, signals: { vector: 0.5 }, contrib: { vector: 0.25, keyword: 0 } },
    ],
  }
  const job = (phase, chunks, memories, state = 'running') => ({
    id: 'check-job', state, phase, collection: 'default', total: 15, done: chunks + memories,
    chunks: { total: 12, done: chunks }, memories: { total: 3, done: memories },
    started_at: ago(4000), finished_at: state === 'running' ? null : ago(0), error: null, hint: null,
    result: state === 'done' ? { reindexed: 12, memories_reindexed: 3, embedder: 'openai:demo-embed' } : null,
  })
  // GET /kb/reindex：点重建之前没在跑（开发模式下进页那一眼会问两次）；之后依次是
  // 片段 → 倒排 → 记忆 → 做完
  const steps = [job('chunks', 6, 0), job('index', 12, 0), job('memories', 12, 2), job('done', 12, 3, 'done')]
  let started = false
  let polled = 0
  let rebuilt = false
  const { page, sent, natives, errors, close } = await open('/knowledge/kb', {
    handlers: [
      [/^GET \/kb\/search$/, json(hits)],
      [/^GET \/kb\/embedding$/, (route) => json({ ...embedding, fallback: false, has_semantics: true, unindexed_chunks: 0,
        stale_chunks: rebuilt ? 0 : 12, stale_memories: rebuilt ? 0 : 3 })(route)],
      [/^POST \/kb\/reindex$/, (route) => { started = true; return json(job('chunks', 0, 0), 202)(route) }],
      [/^GET \/kb\/reindex$/, (route) => {
        if (!started) return json({ state: 'idle' })(route)
        const out = steps[Math.min(polled++, steps.length - 1)]
        if (out.state === 'done') rebuilt = true
        return json(out)(route)
      }],
    ],
  })
  await page.getByLabel('检索内容').fill('出勤')
  await page.getByRole('button', { name: '检索', exact: true }).click()
  await page.waitForTimeout(400)
  const hit = page.locator('[data-hit]').first()
  const contrib = await hit.locator('[data-contrib]').innerText().catch(() => '')
  check('命中拆成两路贡献：语义 +0.300、关键词 +0.120（两段之和就是总分）',
        /语义\s*\+0\.300/.test(contrib) && /关键词\s*\+0\.120/.test(contrib), contrib.replace(/\s+/g, ' '))
  check('……原始分另写成数：余弦、BM25', contrib.includes('余弦') && contrib.includes('0.600') && contrib.includes('3.21'))
  const segs = await hit.locator('span[title^="语义贡献"] > span').count()
  const segs2 = await page.locator('[data-hit]').nth(1).locator('span[title^="语义贡献"] > span').count()
  check('……贡献条两段；只靠语义命中的那条只有一段', segs === 2 && segs2 === 1, `${segs} / ${segs2}`)
  check('……结果上方有图例：总分 = 语义贡献 + 关键词贡献', (await page.locator('[data-contrib-legend]').innerText().catch(() => '')).includes('关键词贡献'))
  await shot(page, 'kb-search-contrib')

  const rebuild = page.getByRole('button', { name: /重建索引（15 段）/ })
  check('向量对不上时给「重建索引（15 段）」', await rebuild.count() === 1,
        (await page.locator('[data-embedder]').innerText().catch(() => '')).replace(/\s+/g, ' ').slice(0, 160))
  await rebuild.click()
  const post = sent.find((s) => s.key === 'POST /kb/reindex')
  check('重建走后台：POST 带 background=true', !!post?.url.includes('background=true'), post?.url ?? '')
  const chunksText = await until(async () => {
    const t = await page.locator('[data-reindex="chunks"]').innerText().catch(() => '')
    return t.includes('6 / 12') ? t : ''
  }, 4000)
  check('进度是后端按批记的真实数：正在重算知识片段的向量 · 6 / 12 段', !!chunksText, chunksText)
  check('……进度条的值就是 done / total', (await page.locator('[data-reindex] [role="progressbar"]').getAttribute('aria-valuenow').catch(() => '')) === '6')
  check('……跑着的时候不能换向量模型', await page.getByRole('button', { name: '更换' }).isDisabled())
  await shot(page, 'kb-reindex-progress')
  const indexText = await until(() => page.locator('[data-reindex="index"]').innerText().catch(() => ''), 4000)
  check('倒排那一段没有分批进度：写明，不去假装它在走', indexText.includes('没有分批进度'), indexText.replace(/\s+/g, ' '))
  const memText = await until(() => page.locator('[data-reindex="memories"]').innerText().catch(() => ''), 4000)
  check('……接着重算记忆：2 / 3 条', memText.includes('2 / 3 条'), memText.replace(/\s+/g, ' '))
  const doneToast = await until(async () => {
    const t = await page.locator('body').innerText()
    return t.includes('重建 12 段知识') ? t : ''
  }, 4000)
  check('做完了报一句：用哪个模型重建了多少', !!doneToast && doneToast.includes('openai:demo-embed') && doneToast.includes('3 条记忆'))
  check('……进度收起，重建按钮也跟着消失（不再有对不上的）',
        await page.locator('[data-reindex]').count() === 0 && await page.getByRole('button', { name: /重建索引/ }).count() === 0)
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()

  // 已经有一次在跑：回 409 就说明，并接着看它的进度，不起第二次
  let other = false
  const busy = await open('/knowledge/kb', {
    handlers: [
      [/^GET \/kb\/embedding$/, json({ ...embedding, fallback: false, has_semantics: true, unindexed_chunks: 0, stale_chunks: 12, stale_memories: 3 })],
      [/^POST \/kb\/reindex$/, (route) => { other = true; return json({ detail: '正在重建索引（全部集合，已完成 3/15），请等待完成后再操作' }, 409)(route) }],
      // 进页那一眼还没在跑；点了之后别处起的那一次才露面
      [/^GET \/kb\/reindex$/, (route) => json(other ? { ...job('chunks', 3, 0), collection: null } : { state: 'idle' })(route)],
    ],
  })
  await busy.page.getByRole('button', { name: /重建索引（15 段）/ }).click()
  await busy.page.waitForTimeout(600)
  check('已经在重建：回 409 时说明已经在跑', (await busy.page.locator('body').innerText()).includes('正在重建索引（全部集合，已完成 3/15）'))
  check('……接着显示那一次的进度', ((await busy.page.locator('[data-reindex]').innerText().catch(() => '')).includes('3 / 12')))
  check('……只发了一次 POST，不起第二次', busy.sent.filter((s) => s.key === 'POST /kb/reindex').length === 1)
  await busy.close()
})

await section('长期记忆', async () => {
  const withRun = memories.find((m) => m.source?.kind === 'run' && m.source?.run_exists)
  const target = memories[0]
  const { page, sent, natives, errors, close } = await open('/knowledge/memory', {
    handlers: [
      [/^POST \/memory$/, (route, { body }) => json({ id: 'fake-mem', scope: 'default', kind: body.kind, content: body.content, importance: 0.5, use_count: 0, meta: {}, source: { kind: 'manual' }, created_at: new Date().toISOString(), last_used_at: null }, 201)(route)],
      [/^PATCH \/memory\//, (route, { body }) => json({ ...target, ...body })(route)],
      [/^GET \/memory\/search$/, json({ results: [], peek: true })],
    ],
  })
  const input = page.locator('[data-memory-input]')
  await input.fill('出勤率 = 实际 / 计划')
  await composingEnter(input)
  await page.waitForTimeout(200)
  check('输入法组字时的回车不写记忆', !sent.some((s) => s.key === 'POST /memory'))
  await input.press('Enter')
  await page.waitForTimeout(400)
  const add = sent.find((s) => s.key === 'POST /memory')
  check('回车写入，内容完整', add?.body?.content === '出勤率 = 实际 / 计划', JSON.stringify(add?.body ?? {}))

  await page.getByLabel('召回测试').fill('出勤')
  await page.getByRole('button', { name: /召回/ }).click()
  await page.waitForTimeout(300)
  const recall = sent.find((s) => s.key === 'GET /memory/search')
  check('召回测试带 peek=true，不改召回计数', recall?.url.includes('peek=true'), recall?.url ?? '')

  if (withRun) {
    const href = await page.locator(`[data-memory="${withRun.id}"] a[data-memory-source]`).getAttribute('href')
    check('记忆来源可点，跳到那次运行', href === `/runs/${withRun.source.run_id}`, href ?? '')
  }
  if (target) {
    const row = page.locator(`[data-memory="${target.id}"]`)
    check('写着「写入于」', (await row.innerText()).includes('写入于'))
    await row.getByRole('button', { name: '修改这条记忆' }).click()
    await row.getByLabel('修改记忆内容').fill(`${target.content}（已核对）`)
    await row.getByRole('button', { name: '保存' }).click()
    await page.waitForTimeout(400)
    const patch = sent.find((s) => s.key.startsWith('PATCH /memory/'))
    // content 是沙箱里一条真实的记忆：只报有没有发、键名
    check('原地修改走 PATCH', patch?.body?.content?.endsWith('（已核对）'),
          patch?.body?.content?.endsWith('（已核对）') ? '' : patch ? `键：${Object.keys(patch.body ?? {}).join(',')}` : '没发 PATCH')

    await page.locator(`[data-memory="${target.id}"]`).getByRole('button', { name: '删除这条记忆' }).click()
    const d = dialog(page)
    check('删除记忆先确认，并说后果', (await d.innerText()).includes('无法再召回'))
    await d.getByRole('button', { name: '删除记忆' }).click()
    await page.waitForTimeout(300)
    check('确认后先从列表拿掉', await page.locator(`[data-memory="${target.id}"]`).count() === 0)
    await shot(page, 'memory-undo')
    await page.getByRole('button', { name: '撤销' }).click()
    await page.waitForTimeout(600)
    check('撤销就回来了', await page.locator(`[data-memory="${target.id}"]`).count() === 1)
    await page.waitForTimeout(5200)
    check('撤销了就不会再发 DELETE', !sent.some((s) => s.key.startsWith('DELETE')))
  }
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('目录没取回来：空态不冒充「还没有」', async () => {
  for (const [path, pattern, label] of [
    ['/settings/providers', /^GET \/providers$/, '模型接入'],
    ['/knowledge/skills', /^GET \/skills$/, 'Skill'],
  ]) {
    const { page, close } = await open(path, { handlers: [[pattern, json({ detail: '服务端内部错误' }, 500)]] })
    await page.waitForTimeout(600)
    const el = page.locator('main [data-empty-unknown]')
    check(`${label}那张表没取回来：不说「还没有」，说没取回来并给「重新读取」`,
          (await el.getAttribute('data-empty-unknown').catch(() => null)) === 'error' && (await el.innerText().catch(() => '')).includes('重新读取'))
    check('……收起「添加 / 新建」这类动作，免得照着建出重复的', await el.getByRole('button', { name: /添加接入|新建 Skill/ }).count() === 0)
    await close()
  }
})

await section('Skill', async () => {
  const { page, sent, natives, close } = await open('/knowledge/skills', {
    handlers: [[/^POST \/skills$/, (route, { body }) => json({ id: 'fake-skill', ...body }, 201)(route)]],
  })
  await page.getByRole('button', { name: /新建 Skill/ }).first().click()
  const dlg = dialog(page)
  await dlg.locator('input').first().fill('检查用 Skill')
  await dlg.locator('#skill-tags').click()
  await page.keyboard.type('质量,工艺，出具')
  const chips = await dlg.getByLabel(/去掉标签/).count()
  check('逐字敲「质量,工艺，出具」：逗号（中英文）能分出标签', chips === 2, `${chips} 个标签 + 框里「${await dlg.locator('#skill-tags').inputValue()}」`)
  await dlg.getByRole('button', { name: '保存' }).click()
  await page.waitForTimeout(400)
  const post = sent.find((s) => s.key === 'POST /skills')
  check('保存时框里没收成标签的那半截也算上', JSON.stringify(post?.body?.tags) === JSON.stringify(['质量', '工艺', '出具']), JSON.stringify(post?.body?.tags))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  await close()
})

await section('工具', async () => {
  const danger = tools.find((t) => t.runtime_approval)
  const safe = tools.find((t) => t.source === 'builtin' && !t.dangerous)
  let calls = 0
  // 库里早就存着的坏参数定义：后端在工具项上给 problem
  const brokenProblem = '参数定义格式有误：参数 store 应写成 {"type": "string"} 这样的对象，不能直接写 "string"'
  const brokenTool = { id: 'broken_demo', name: 'broken_demo', description: '检查脚本的坏工具', category: '自定义 · http',
    source: 'custom', dangerous: true, runtime_approval: false, schema: { properties: { store: 'string' } }, problem: brokenProblem }
  const brokenRow = { id: 'check-manage-broken', name: 'broken_demo', kind: 'http', description: '检查脚本的坏工具',
    parameters: { properties: { store: 'string' } }, config: { method: 'GET', url: 'https://example.com/x' }, enabled: true, problem: brokenProblem }
  const { page, sent, natives, errors, close } = await open(danger ? `/tools/library/${danger.id}` : '/tools', {
    handlers: [
      [/^GET \/tools$/, async (route) => json([...await (await route.fetch()).json(), brokenTool])(route)],
      [/^GET \/custom-tools$/, async (route) => json([...await (await route.fetch()).json(), brokenRow])(route)],
      [/^POST \/tools\/[^/]+\/run$/, (route, { body }) => {
        calls++
        return body?.confirm
          ? json({ ok: true, result: 'done', duration_ms: 5, note: '参数名 cuont 不存在，已按唯一候选 count 执行' })(route)
          : json({ detail: '将在工作目录中写入「x.txt」，同名文件会被覆盖。在工具库中执行不经过审批，确认后才会执行。' }, 409)(route)
      }],
      [/^POST \/mcp\/refresh$/, (route) => route.abort()],
      [/^POST \/custom-tools\/test$/, (route, { body }) => json(body?.config?.url?.includes('fail')
        ? { ok: false, error: '工具执行失败：HTTP 404', hint: '核对接口地址', detail: '', duration_ms: 12 }
        : { ok: true, result: { echo: body?.args ?? {} }, duration_ms: 34, note: '参数名 qurey 不存在，已按唯一候选 query 执行' })(route)],
      // 沙箱里没有 MCP 服务：两台假的，一台后端记着 5 分钟前连上过，一台老数据只有 status、没有测连接记录
      [/^GET \/mcp\/servers$/, json([
        { id: 'check-mcp-ok', name: 'files_demo', transport: 'stdio', command: 'npx', args: ['-y', 'demo-mcp'], env: {}, url: null,
          enabled: true, status: 'ok', last_error: null, tools_cache: ['read', 'list'],
          last_checked_at: ago(5 * 60_000), last_check_ok: true, last_latency_ms: 88 },
        { id: 'check-mcp-old', name: 'legacy_demo', transport: 'http', command: null, args: [], env: {}, url: 'https://example.com/mcp',
          enabled: true, status: 'error', last_error: '无法连接', tools_cache: [],
          last_checked_at: null, last_check_ok: null, last_latency_ms: null },
      ])],
      [/^POST \/mcp\/servers\/check-mcp-ok\/probe$/, delayed(200, { ok: true, tools: [{ name: 'read' }, { name: 'list' }, { name: 'write' }], elapsed_ms: 120 })],
    ],
  })
  if (danger) {
    const listItem = page.locator('nav[aria-label="工具列表"] button', { hasText: danger.name }).first()
    check('会停下来等审批的工具标「运行时需审批」', (await listItem.innerText()).includes('运行时需审批'))
    if (safe) {
      const s = page.locator('nav[aria-label="工具列表"] button', { hasText: safe.name }).first()
      check('没副作用的内置工具不挂审批标签', !(await s.innerText()).includes('审批'))
    }
    check('详情区写明这里直接执行、不经审批', (await text(page)).includes('不经审批'))
    await page.getByRole('button', { name: /^执行$/ }).click()
    await page.waitForTimeout(300)
    const d = dialog(page)
    check('409 后弹确认，把后端说的「会做什么」给人看', (await d.innerText().catch(() => '')).includes('会被覆盖'))
    await shot(page, 'tool-confirm')
    await d.getByRole('button', { name: '确认执行' }).click()
    await page.waitForTimeout(400)
    const runs = sent.filter((s) => /^POST \/tools\/.+\/run$/.test(s.key))
    check('确认后带 confirm:true 重发', runs.length === 2 && runs[1].body?.confirm === true, JSON.stringify(runs.map((r) => r.body?.confirm)))
    check('结果显示成功', (await page.locator('[data-tool-result]').getAttribute('data-tool-result')) === 'ok')
    const note = await page.locator('[data-tool-note]').innerText().catch(() => '')
    check('参数名被纠正过：成功结果下面用 warn 写出来，提醒先改对再抄进工作流', note.includes('cuont') && note.includes('修正参数名称'), note)
    check('……结果和提醒落在看得见的地方（不用自己滚）', await inView(page.locator('[data-tool-note]')))
  }
  {
    const item = page.locator('nav[aria-label="工具列表"] button', { hasText: 'broken_demo' }).first()
    const chip = item.locator('[data-tool-problem]')
    check('工具库：参数定义写坏的自定义工具挂醒目的 chip，悬停看完整原因', await chip.count() === 1
          && (await chip.getAttribute('title')) === brokenProblem, await chip.innerText().catch(() => ''))
    await item.click()
    await page.waitForTimeout(300)
    const alert = page.locator('[data-tool-problem-detail]')
    check('……详情区写明原因和「绑了它的节点一定失败」', (await alert.innerText().catch(() => '')).includes('一定失败'))
    await shot(page, 'tool-broken-detail')
    await alert.getByRole('button', { name: '去改参数定义' }).click()
    const ed0 = page.getByRole('dialog', { name: '编辑工具「broken_demo」' })
    // 等编辑框真的出来再判：并行跑时固定等 700ms 不够，编辑框还没挂上就判成没打开（check-all 里偶发过）
    await ed0.waitFor({ timeout: 5000 }).catch(() => {})
    await page.waitForTimeout(200)
    check('……「去改参数定义」直接打开它的编辑框（/tools/custom?edit=）', await ed0.count() === 1, page.url())
    check('……读完 ?edit= 就从地址里摘掉', !page.url().includes('edit='), page.url())
    const perr = await ed0.locator('[data-params-field]').innerText().catch(() => '')
    check('……编辑框一打开，参数定义下面就写着坏在哪', perr.includes('参数 store'), perr.replace(/\s+/g, ' ').slice(0, 120))
    const row0 = page.locator('div.rounded-lg', { hasText: 'broken_demo' }).last()
    await ed0.getByRole('button', { name: '取消' }).click()
    await page.waitForTimeout(300)
    check('自定义工具列表：坏工具那一行也挂 chip，并写出原因', await row0.locator('[data-tool-problem]').count() === 1
          && (await row0.innerText()).includes('参数 store'))
  }
  await page.getByRole('tab', { name: '自定义工具' }).click()
  await page.waitForTimeout(400)
  await page.getByRole('button', { name: /新建工具/ }).first().click()
  const ed = dialog(page)
  const params = await ed.locator('textarea').nth(1).inputValue()
  check('新建工具的参数给了能跑的示例，不是 {}', params.includes('"query"'), params.slice(0, 60))
  const trialBox = ed.locator('[data-tool-trial]')
  check('新建时不用先保存就能试运行', (await trialBox.innerText()).includes('无需先保存'))
  const tryBtn = trialBox.getByRole('button', { name: /试运行/ }).last()
  check('……URL 还没填时试运行不可点，悬停说缺什么', await tryBtn.isDisabled() && ((await tryBtn.getAttribute('title')) ?? '').includes('URL'))
  await ed.getByLabel(/^URL/).fill('https://api.example.com/search?q={{ query }}')
  await tryBtn.click()
  await page.waitForTimeout(400)
  const draft = sent.filter((s) => s.key === 'POST /custom-tools/test').at(-1)
  check('……试运行的是眼前这份没保存的配置（POST /custom-tools/test）',
        draft?.body?.kind === 'http' && draft?.body?.config?.url?.includes('api.example.com') && 'query' in (draft?.body?.parameters?.properties ?? {})
        && 'args' in (draft?.body ?? {}), JSON.stringify(draft?.body ?? {}).slice(0, 160))
  check('……没有保存（没发 POST /custom-tools）', !sent.some((s) => s.key === 'POST /custom-tools'))
  const trialNote = await trialBox.locator('[data-tool-note]').innerText().catch(() => '')
  check('……试运行结果里参数名被纠正过也写出来', trialNote.includes('qurey'), trialNote)
  check('……试运行结果和提醒自己滚进弹窗的可视范围（试运行区在编辑框最底下）', await inView(trialBox.locator('[data-tool-note]')))
  await shot(page, 'custom-tool-trial')
  await ed.getByLabel(/^URL/).fill('https://api.example.com/fail')
  check('……改了配置，刚才的结果标成旧的', (await trialBox.innerText()).includes('这是修改前的结果'))
  await tryBtn.click()
  await page.waitForTimeout(400)
  check('……失败：原因和怎么办写在试运行区', (await trialBox.innerText()).includes('HTTP 404') && (await trialBox.innerText()).includes('核对接口地址'))
  await ed.getByRole('button', { name: '取消' }).click()
  await page.waitForTimeout(200)
  const discard = dialog(page).getByRole('button', { name: '放弃修改' })
  if (await discard.count()) await discard.click()

  await page.getByRole('tab', { name: 'MCP 接入' }).click()
  await page.waitForTimeout(400)
  const okCard = page.locator('article', { hasText: 'files_demo' })
  const okPill = await okCard.locator('[data-health]').innerText().catch(() => '')
  check('MCP 卡片初值是后端记着的上次探测：连接成功 · 88 ms · 5 分钟前测试', /连接成功.*88 ms.*5 分钟前测试/.test(okPill), okPill)
  check('……只有 status、没有测连接记录的老数据写「未测试」，不再拿 status 猜',
        (await page.locator('article', { hasText: 'legacy_demo' }).locator('[data-health]').getAttribute('data-health')) === 'idle')
  await okCard.getByRole('button', { name: /测试/ }).click()
  await page.waitForTimeout(500)
  const probed = await okCard.locator('[data-health]').innerText()
  check('……测一次：用后端量的耗时（120 ms），不是往返时间', /连接成功.*120 ms.*刚刚测试/.test(probed), probed)
  await shot(page, 'mcp-health')
  await page.getByRole('button', { name: /重新加载/ }).click()
  await page.waitForTimeout(700)
  check('写操作失败有 error toast（以前一声不响）', await page.locator('[role="alert"][aria-live="assertive"] > *').count() > 0)
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('自定义工具 · 已有工具', async () => {
  // 参数是 {} 的已有工具：编辑器要显示存着的样子，只改描述不能把示例参数一起存回去
  const tool = {
    id: 'check-manage-tool', name: 'ping_mes', kind: 'http', description: '旧描述', parameters: {},
    config: { method: 'GET', url: 'https://example.com/ping' }, enabled: true,
  }
  const doomed = { ...tool, id: 'check-manage-doomed', name: 'doomed_tool', description: '要删的' }
  let list = [tool, doomed]
  const { page, sent, natives, errors, close } = await open('/tools/custom', {
    handlers: [
      [/^GET \/custom-tools$/, (route) => json(list)(route)],
      [/^POST \/custom-tools$/, json({ detail: '参数定义格式有误：无法识别参数 n 的类型「int」，是否应为 integer；只能是 string、integer、number、boolean、array、object 中的一个' }, 422)],
      [/^PATCH \/custom-tools\/check-manage-tool$/, (route, { body }) => json({ ...tool, ...body })(route)],
      [/^DELETE \/custom-tools\//, (route) => route.fulfill({ status: 204, body: '' })],
    ],
  })
  const row = page.locator('div.rounded-lg', { hasText: 'ping_mes' }).last()
  await row.getByRole('button', { name: '编辑' }).click()
  const dlg = dialog(page)
  const params = await dlg.locator('textarea').nth(1).inputValue()
  check('已有工具的参数照存着的显示（{}），不塞示例', !params.includes('query'), params.slice(0, 60))
  check('……参数为空时给「插入示例参数」，点了才填', await dlg.getByRole('button', { name: /插入示例参数/ }).count() === 1)
  await shot(page, 'custom-tool-empty-params')
  await dlg.locator('textarea').first().fill('新描述：探一下 MES 在不在')
  await dlg.getByRole('button', { name: '保存' }).click()
  await page.waitForTimeout(500)
  const patch = sent.filter((s) => s.key === 'PATCH /custom-tools/check-manage-tool').at(-1)
  check('只改描述：PATCH 里的参数还是 {}，不带 query', !!patch && !JSON.stringify(patch.body?.parameters ?? {}).includes('query'),
        JSON.stringify(patch?.body?.parameters ?? null))

  await page.locator('div.rounded-lg', { hasText: 'ping_mes' }).last().getByRole('button', { name: '编辑' }).click()
  const insert = dialog(page).getByRole('button', { name: /插入示例参数/ })
  if (await insert.count()) await insert.click()
  check('……点「插入示例参数」才填进 query', (await dialog(page).locator('textarea').nth(1).inputValue()).includes('"query"'))
  await dialog(page).getByRole('button', { name: '取消' }).click()

  // 新建时参数定义写坏了：后端 422 的那句话写在「参数 JSON Schema」下面，不是盖在标题上的 toast
  await page.getByRole('button', { name: /新建工具/ }).first().click()
  const nt = dialog(page)
  await nt.getByLabel(/^名称/).fill('bad_schema_demo')
  await nt.getByLabel(/^URL/).fill('https://example.com/x')
  await nt.getByRole('button', { name: '保存' }).click()
  await page.waitForTimeout(500)
  const pe = await nt.locator('[data-params-field]').innerText().catch(() => '')
  check('新建时保存被 422 拒掉：原因写在「参数 JSON Schema」下面', pe.includes('int') && pe.includes('integer'), pe.replace(/\s+/g, ' ').slice(0, 120))
  check('……不再弹 toast', !(await page.locator('[aria-live="assertive"]').innerText().catch(() => '')).includes('integer'))
  // toast 在 assertive 区里会被念出来；挪进字段之后，要靠焦点落到框上、框标成出错并指向那句话
  const paramsBox = nt.locator('[data-params-field] textarea')
  const described = await paramsBox.getAttribute('aria-describedby').catch(() => null)
  const describedText = described
    ? await page.evaluate((ids) => ids.split(/\s+/).map((id) => document.getElementById(id)?.textContent ?? '').join(' '), described) : ''
  check('……参数框标成出错（aria-invalid），aria-describedby 指向那句原因', (await paramsBox.getAttribute('aria-invalid')) === 'true'
        && describedText.includes('integer'), `aria-invalid=${await paramsBox.getAttribute('aria-invalid')} · describedby=${described ?? '无'}`)
  check('……焦点落在参数框上（保存按钮忙时禁用，以前焦点掉到 body、弹窗外）', await paramsBox.evaluate((el) => el === document.activeElement),
        await page.evaluate(() => document.activeElement?.tagName ?? 'null'))
  check('……参数框描红', await paramsBox.evaluate((el) => getComputedStyle(el).borderTopColor)
        === await page.evaluate(() => { const d = document.createElement('div'); d.style.color = 'var(--err)'; document.body.append(d); const c = getComputedStyle(d).color; d.remove(); return c }))
  check('……弹窗还开着，填的东西都在', await nt.getByLabel(/^名称/).inputValue() === 'bad_schema_demo')
  await shot(page, 'custom-tool-schema-error')
  await nt.getByRole('button', { name: '取消' }).click()
  await page.waitForTimeout(200)
  const discard0 = dialog(page).getByRole('button', { name: '放弃修改' })
  if (await discard0.count()) await discard0.click()

  // 撤销窗口里别的操作重拉列表：删掉的那行不能跟着回来
  await page.getByRole('button', { name: '删除自定义工具 doomed_tool' }).click()
  await page.waitForTimeout(200)
  // 限定 exact：toast「已删除工具「doomed_tool」」里也有这个名字
  const doomedRow = page.locator('main').getByText('doomed_tool', { exact: true })
  check('删除后先从列表拿掉', await doomedRow.count() === 0)
  await page.locator('div.rounded-lg', { hasText: 'ping_mes' }).last().getByRole('button', { name: '编辑' }).click()
  await dialog(page).locator('textarea').first().fill('再改一次描述')
  await dialog(page).getByRole('button', { name: '保存' }).click()
  await page.waitForTimeout(500)
  check('……撤销窗口里保存别的工具、列表重拉，删掉的那行不回来', await doomedRow.count() === 0
        && sent.filter((s) => s.key === 'GET /custom-tools').length >= 2)
  await page.waitForTimeout(5000)
  list = [tool]
  check('……到点只发一次 DELETE', sent.filter((s) => s.key.startsWith('DELETE /custom-tools/')).length === 1)
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('工具 · 信任三档', async () => {
  // 后端的 trust / trust_key 可能还没上线：GET /tools 在真列表后面补三件假的（一个自定义、两个 MCP），
  // PUT /tools/trust 一律拦下来。fail 打开时回 422，看界面退回原样、说出后端给的原因
  const custom = { id: 'crm_lookup', name: 'crm_lookup', description: '按客户编号查客户档案', category: '自定义 · http',
    source: 'custom', dangerous: true, runtime_approval: true, schema: {}, problem: null, trust: 'ask', trust_key: 'crm_lookup' }
  const search = { id: 'mcp:demo/search', name: 'search', server: 'demo', description: '全文检索', category: 'MCP · demo',
    source: 'mcp', dangerous: true, runtime_approval: true, schema: {}, trust: 'gated', trust_key: 'mcp:demo/search' }
  const weather = { id: 'mcp:demo/weather', name: 'weather', server: 'demo', description: '查天气', category: 'MCP · demo',
    source: 'mcp', dangerous: true, runtime_approval: false, schema: {}, trust: 'always', trust_key: 'mcp:demo/weather' }
  const customRow = { id: 'check-trust-crm', name: 'crm_lookup', kind: 'http', description: '按客户编号查客户档案', parameters: {},
    config: { method: 'GET', url: 'https://example.com/crm' }, enabled: true, problem: null }
  const server = { id: 'check-trust-demo', name: 'demo', transport: 'http', command: null, args: [], env: {}, url: 'https://example.com/mcp',
    enabled: true, status: 'ok', last_error: null, tools_cache: ['search', 'weather'], last_checked_at: null, last_check_ok: null, last_latency_ms: null }
  let fail = false
  const builtin = tools.find((t) => t.source === 'builtin')
  const { page, sent, natives, errors, close } = await open('/tools/library/crm_lookup', {
    handlers: [
      [/^GET \/tools$/, async (route) => json([...await (await route.fetch()).json(), custom, search, weather])(route)],
      [/^GET \/custom-tools$/, json([customRow])],
      [/^GET \/mcp\/servers$/, json([server])],
      [/^PUT \/tools\/trust$/, (route, { body }) => (fail
        ? delayed(500, { detail: '信任档位只能是 ask、gated、always，当前为「bogus」' }, 422)(route)
        : delayed(150, { key: body?.key, trust: body?.trust })(route))],
    ],
  })
  const puts = () => sent.filter((s) => s.key === 'PUT /tools/trust')
  const group = (name) => page.getByRole('radiogroup', { name: `${name} 的运行时审批` })
  const checkedIn = (g) => g.locator('[aria-checked="true"]').innerText().catch(() => '')
  // 按名字整段认：search 这种短名会撞上内置工具名里的子串
  const listItem = (name) => page.locator('nav[aria-label="工具列表"] button')
    .filter({ has: page.locator('span.mono', { hasText: new RegExp(`^${name}$`) }) }).first()

  const detail = group('crm_lookup')
  check('自定义工具：详情区有三选一，三档是需审批 / 始终允许 · 门控把关 / 始终允许',
    (await detail.getByRole('radio').allInnerTexts()).join('|') === '需审批|始终允许 · 门控把关|始终允许',
    (await detail.getByRole('radio').allInnerTexts().catch(() => [])).join('|'))
  check('……现在选中的是后端给的「需审批」', await checkedIn(detail) === '需审批', await checkedIn(detail))
  check('……旁边说清三档的差别，写明正式运行不看这个设置', (await page.locator('[data-trust-block]').innerText()).includes('正式运行不受此设置影响'))
  check('徽标按信任档：需审批挂「运行时需审批」', (await listItem('crm_lookup').innerText()).includes('运行时需审批'))
  check('……门控把关挂「门控把关」', (await listItem('search').innerText()).includes('门控把关')
    && !(await listItem('search').innerText()).includes('运行时需审批'))
  const alwaysText = await listItem('weather').innerText()
  check('……始终允许什么都不挂', !alwaysText.includes('审批') && !alwaysText.includes('门控'), alwaysText.replace(/\s+/g, ' '))
  await shot(page, 'tool-trust-library')

  // 改档：乐观更新，PUT 的载荷是 {key, trust}
  await detail.getByRole('radio', { name: '始终允许 · 门控把关' }).click()
  await page.waitForTimeout(40)
  check('改档先改界面（请求还没回来就已经选中、徽标跟着变）', await checkedIn(detail) === '始终允许 · 门控把关'
    && (await listItem('crm_lookup').innerText()).includes('门控把关'), await checkedIn(detail))
  await page.waitForTimeout(300)
  const put = puts().at(-1)
  check('……PUT /tools/trust 的载荷是 {key: crm_lookup, trust: gated}', put?.body?.key === 'crm_lookup' && put?.body?.trust === 'gated'
    && Object.keys(put?.body ?? {}).length === 2, JSON.stringify(put?.body ?? null))
  // 方向键：整组一个 Tab 位，→ 选下一档并保存
  await detail.getByRole('radio', { name: '始终允许 · 门控把关' }).focus()
  await page.keyboard.press('ArrowRight')
  await page.waitForTimeout(300)
  check('……方向键也能改（→ 到「始终允许」，同样发 PUT）', puts().at(-1)?.body?.trust === 'always' && await checkedIn(detail) === '始终允许',
    JSON.stringify(puts().map((p) => p.body?.trust)))
  check('……始终允许之后徽标不挂了', !(await listItem('crm_lookup').innerText()).includes('审批'))

  // 失败：退回原来那一档，并说出后端给的原因
  fail = true
  await detail.getByRole('radio', { name: '需审批' }).click()
  await page.waitForTimeout(120)
  const optimistic = await checkedIn(detail)
  await page.waitForTimeout(700)
  const reverted = await checkedIn(detail)
  const alert = await page.locator('[role="alert"][aria-live="assertive"]').innerText().catch(() => '')
  check('PUT 失败：先按新的显示，回来后退回「始终允许」', optimistic === '需审批' && reverted === '始终允许', `${optimistic} → ${reverted}`)
  check('……徽标也退回去（始终允许不挂）', !(await listItem('crm_lookup').innerText()).includes('审批'))
  check('……出错提示说哪个工具没改成、为什么', alert.includes('crm_lookup') && alert.includes('修改失败') && alert.includes('ask、gated、always'),
    alert.replace(/\s+/g, ' ').slice(0, 120))
  fail = false
  await page.locator('[role="alert"][aria-live="assertive"] button[aria-label*="关闭"]').first().click().catch(() => {})

  // MCP 工具的 id 带斜杠（mcp:demo/search）：以前点它落到 404，详情区里的三选一根本到不了
  await listItem('search').click()
  await page.waitForTimeout(300)
  check('MCP 工具在工具库里点得开，详情区有三选一（门控把关选中）', await checkedIn(group('mcp:demo/search')) === '始终允许 · 门控把关',
    page.url().replace(WEB, ''))

  // 内置工具没有这个控件
  if (builtin) {
    await listItem(builtin.name).click()
    await page.waitForTimeout(300)
    check('内置工具的详情区没有三选一', await page.locator('[data-trust-control]').count() === 0 && await page.locator('[data-trust-block]').count() === 0)
  }

  await page.getByRole('tab', { name: '自定义工具' }).click()
  await page.waitForTimeout(400)
  const row = page.locator('div.rounded-lg', { hasText: 'crm_lookup' }).last()
  check('自定义工具列表：那一行有三选一，跟着刚才改的是「始终允许」', await checkedIn(row.getByRole('radiogroup')) === '始终允许')
  check('……列表上方说清三档的差别', (await page.locator('[data-trust-explain]').innerText().catch(() => '')).includes('门控把关指每次先由小模型检查参数'))
  await shot(page, 'tool-trust-custom')

  await page.getByRole('tab', { name: 'MCP 接入' }).click()
  await page.waitForTimeout(400)
  const card = page.locator('article', { hasText: 'demo' })
  check('MCP 卡片列出这台服务的工具，每个一组三选一', await card.getByRole('radiogroup').count() === 2
    && await checkedIn(group('mcp:demo/search')) === '始终允许 · 门控把关' && await checkedIn(group('mcp:demo/weather')) === '始终允许')
  await group('mcp:demo/weather').getByRole('radio', { name: '需审批' }).click()
  await page.waitForTimeout(300)
  check('……MCP 工具改档，键是 mcp:服务名/工具名', puts().at(-1)?.body?.key === 'mcp:demo/weather' && puts().at(-1)?.body?.trust === 'ask',
    JSON.stringify(puts().at(-1)?.body ?? null))
  await shot(page, 'tool-trust-mcp')
  check('改档只发 PUT /tools/trust，没有别的写请求', sent.filter((s) => s.key !== 'PUT /tools/trust' && !s.key.startsWith('GET ')).length === 0,
    sent.filter((s) => s.key !== 'PUT /tools/trust' && !s.key.startsWith('GET ')).map((s) => s.key).join(', '))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  // 渲染崩了由错误边界接住，不一定有 pageerror：边界的兜底页也算
  check('没有运行时报错', errors.length === 0 && await page.locator('[data-error-scope]').count() === 0, errors[0] ?? '')
  await close()

  // 老后端：工具项上没有 trust / trust_key。界面照旧：没有三选一，自定义 / MCP 写「运行时不审批」
  const strip = ({ trust, trust_key, ...rest }) => ({ ...rest, runtime_approval: false })
  const old = await open('/tools/library/crm_lookup', {
    handlers: [
      [/^GET \/tools$/, async (route) => json([...await (await route.fetch()).json(), strip(custom), strip(search)])(route)],
      [/^GET \/custom-tools$/, json([customRow])],
      [/^GET \/mcp\/servers$/, json([server])],
    ],
  })
  check('老后端：详情区没有三选一', await old.page.locator('[data-trust-control]').count() === 0 && await old.page.locator('[data-trust-block]').count() === 0)
  check('……徽标照旧写「运行时不审批」', (await old.page.locator('nav[aria-label="工具列表"] button', { hasText: 'crm_lookup' }).first()
    .innerText({ timeout: 3000 }).catch(() => '')).includes('运行时不审批'))
  // 页面要是渲染崩了，后面几项照样跑完，最后那条「没有运行时报错」说出原因
  await old.page.getByRole('tab', { name: '自定义工具' }).click({ timeout: 3000 }).catch(() => {})
  await old.page.waitForTimeout(400)
  check('……自定义工具列表也没有，没有那段说明', await old.page.locator('[data-trust-control]').count() === 0 && await old.page.locator('[data-trust-explain]').count() === 0)
  await old.page.getByRole('tab', { name: 'MCP 接入' }).click({ timeout: 3000 }).catch(() => {})
  await old.page.waitForTimeout(400)
  check('……MCP 卡片不列工具', await old.page.locator('[data-trust-control]').count() === 0 && await old.page.locator('article ul').count() === 0)
  check('……没有运行时报错', old.errors.length === 0 && await old.page.locator('[data-error-scope]').count() === 0, old.errors[0] ?? '')
  await old.close()
})

await section('知识库 · 撤销窗口里的轮询', async () => {
  // 有文档在处理时列表 2 秒轮询一次。以前删掉的那行 2.6 秒后又被轮询救回来
  const now = new Date().toISOString()
  const busy = { id: 'check-manage-busy', collection: 'default', title: 'big.pdf', source: 'big.pdf', mime: 'application/pdf',
    chunk_count: 0, status: 'processing', error: '', meta: { progress: { done: 3, total: 40 } }, created_at: now }
  const victim = { ...busy, id: 'check-manage-victim', title: 'victim.docx', source: 'victim.docx', status: 'ready', chunk_count: 2, meta: {} }
  const two = [
    { collection: 'default', documents: 2, chunks: 2 },
    { collection: 'check_second', documents: 0, chunks: 0 },
  ]
  let docs = [busy, victim]
  const { page, sent, natives, errors, close } = await open('/knowledge/kb', {
    handlers: [
      [/^GET \/kb\/collections$/, json(two)],
      [/^GET \/kb\/documents$/, (route) => json(docs)(route)],
      [/^DELETE \/kb\/documents\//, (route) => { docs = [busy]; return route.fulfill({ status: 204, body: '' }) }],
    ],
  })
  // 知识库单选组：只占一个 Tab 位，方向键切换
  const radios = page.locator('[role="radiogroup"][aria-label="知识库"] [role="radio"]')
  check('知识库单选组只占一个 Tab 位', (await radios.evaluateAll((els) => els.map((el) => el.tabIndex).join())) === '0,-1')
  check('……「新知识库」不在单选组里', await page.locator('[role="radiogroup"][aria-label="知识库"]').getByText('新知识库').count() === 0)
  await radios.first().focus()
  await page.keyboard.press('ArrowRight')
  await page.waitForTimeout(300)
  check('……→ 切到下一个知识库，焦点跟过去', (await radios.nth(1).getAttribute('aria-checked')) === 'true'
        && await radios.nth(1).evaluate((el) => el === document.activeElement))
  await page.keyboard.press('ArrowLeft')
  await page.waitForTimeout(600)

  await page.locator('[data-doc="victim.docx"]').getByRole('button', { name: /删除/ }).click()
  await page.waitForTimeout(200)
  check('删文档：先从列表拿掉', await page.locator('[data-doc="victim.docx"]').count() === 0)
  await page.waitForTimeout(2600)
  check('……撤销窗口里轮询刷新了列表，它也不回来', await page.locator('[data-doc="victim.docx"]').count() === 0
        && await page.locator('[data-doc="big.pdf"]').count() === 1)
  await page.waitForTimeout(2800)
  check('……到点只发一次 DELETE', sent.filter((s) => s.key.startsWith('DELETE /kb/documents/')).length === 1)
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('知识库 · 切块地图', async () => {
  const doc = (await get('/kb/documents?collection=default'))[0]
  if (doc) {
    const { page, close } = await open(`/knowledge/kb/${doc.id}`)
    const cells = page.locator('[data-chunk-map] button')
    check('切块地图的每一格是按钮（不是 listitem）', await cells.count() > 0
          && (await cells.evaluateAll((els) => els.every((el) => !el.getAttribute('role')))))
    check('……容器是带名字的 group，没有 list 语义', await page.locator('[data-chunk-map] [role="group"][aria-label="切块地图"]').count() === 1
          && await page.locator('[data-chunk-map] [role="list"], [data-chunk-map] [role="listitem"]').count() === 0)
    await close()
  } else {
    check('（跳过：沙箱知识库里没有文档）', true)
  }
})

if (SHOTS) {
  console.log('\n=== 截图（亮 / 暗） ===')
  for (const theme of ['dark', 'light']) {
    for (const path of ['/data/databases', '/data/tables', '/settings/providers', '/settings/prefs', '/tools/library', '/tools/custom', '/knowledge/kb', '/knowledge/memory', '/knowledge/skills']) {
      const { page, close } = await open(path, { theme })
      await page.screenshot({ path: `${SHOTS}/${path.replace(/\//g, '_').slice(1)}-${theme}.png` })
      await close()
    }
  }
  console.log(`  已存到 ${SHOTS}`)
}

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 管理页全部通过')
process.exit(failed ? 1 : 0)
