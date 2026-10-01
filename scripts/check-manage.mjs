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
import { readFileSync } from 'node:fs'
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
 * 上传表格时表单里的文本字段（mixed、raw_mode、header_row……）逐次记到 window.__uploadForms。
 * page.route 拿到的 multipart 请求体按原文切太脆（文件那一段 Chrome 也不一定给），在页面里
 * 直接读 FormData 最稳。和 fakeUploadProgress 谁先装都行：两边都只是包一层 send
 */
function recordUploadForms() {
  const open = XMLHttpRequest.prototype.open
  const send = XMLHttpRequest.prototype.send
  window.__uploadForms = []
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__formUrl = String(url)
    return open.call(this, method, url, ...rest)
  }
  window.__importForms = []
  XMLHttpRequest.prototype.send = function (body) {
    const entries = () => Object.fromEntries([...body.entries()]
      .map(([k, v]) => [k, typeof v === 'string' ? v : `<file ${v.name}>`]))
    if (/\/datasources\/upload(\?|$)/.test(this.__formUrl ?? '') && body instanceof FormData) {
      window.__uploadForms.push(entries())
    }
    // 按配方导入的两个上传口（暂存、上传新一期）另记一份：不挤占上面那份的下标
    if (/\/datasources\/(imports\/stage|[^/]+\/reupload)(\?|$)/.test(this.__formUrl ?? '') && body instanceof FormData) {
      window.__importForms.push({ url: this.__formUrl, ...entries() })
    }
    return send.call(this, body)
  }
}
const uploadForms = (page) => page.evaluate(() => window.__uploadForms ?? [])
const importForms = (page) => page.evaluate(() => window.__importForms ?? [])

/**
 * 开一页：GET 放行，写请求交给 handlers（按「METHOD 路径正则」匹配），没配的一律
 * 拦成 503——既不写库，也顺带检验「写失败有反馈」。所有原生对话框都算失败。
 */
async function open(path, {
  handlers = [], theme = THEME, viewport = { width: 1280, height: 860 }, uploadProgress = false, recordForms = false,
} = {}) {
  const ctx = await browser.newContext({ viewport, colorScheme: theme, timezoneId: 'Asia/Shanghai' })
  opened.add(ctx)
  // 找不到元素就早点失败：默认 30 秒一项，一处点不到能拖住整节半分钟
  ctx.setDefaultTimeout(6000)
  ctx.setDefaultNavigationTimeout(30000)
  await ctx.addInitScript((t) => {
    try { localStorage.setItem('agentlab.theme', t); localStorage.removeItem('agentlab.health') } catch { /* noop */ }
  }, theme)
  if (uploadProgress) await ctx.addInitScript(fakeUploadProgress)
  if (recordForms) await ctx.addInitScript(recordUploadForms)
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
  // 裁判拆档：接入和模型都没填时，平常那句建议换成醒目的「未单独配置」提示（建议写在提示里）；填了再换回来（见下）
  check('裁判模型：接入和模型，提示建议和写作模型不同', await page.locator('#pref-judge-provider').count() === 1
    && await page.locator('#pref-judge-model').count() === 1 && all.includes('建议配置一个与撰写报告的模型不同、能力更强的模型'))
  check('……接入留空时跟随助手的模型', (await page.locator('#pref-judge-provider option').first().innerText()).includes('跟随助手的模型'))
  const notSet = page.locator('[data-judge-model] [data-judge-not-set]')
  check('没配裁判模型（接入、模型都是 null）：提示「未单独配置，将使用助手的模型」',
    (await notSet.locator('[data-judge-not-set-text]').innerText().catch(() => '')) === '未单独配置，将使用助手的模型',
    await notSet.innerText().catch(() => '（没有这条提示）'))
  const advice = await notSet.locator('[data-judge-not-set-advice]').innerText().catch(() => '')
  check('……并建议配一个和写作模型不同、能力更强的模型', advice.includes('与撰写报告的模型不同') && advice.includes('能力更强'), advice)
  const noteLook = await notSet.evaluate((el) => {
    const probe = document.createElement('span')
    probe.style.color = 'var(--st-waiting)'
    document.body.appendChild(probe)
    const waiting = getComputedStyle(probe).color
    probe.remove()
    const cs = getComputedStyle(el)
    return { border: cs.borderLeftColor, width: cs.borderLeftWidth, icon: getComputedStyle(el.querySelector('svg')).color, waiting,
             described: document.getElementById(el.closest('[data-judge-model]').getAttribute('aria-describedby'))?.hasAttribute('data-judge-not-set') }
  }).catch(() => null)
  check('……醒目：左侧提醒色竖线、提醒色图标', !!noteLook && noteLook.border === noteLook.waiting && noteLook.width === '2px'
    && noteLook.icon === noteLook.waiting, JSON.stringify(noteLook))
  check('……读屏：模型那一组的说明就是这条提示（aria-describedby）', noteLook?.described === true)
  if (SHOTS) {
    await box.scrollIntoViewIfNeeded()
    await page.waitForTimeout(200)
    await box.screenshot({ path: `${SHOTS}/prefs-judge-not-set-${THEME}.png` })
  }
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
  check('填了裁判模型：「未单独配置」的提示收起，换回平常那句建议', await notSet.count() === 0
    && (await box.innerText().catch(() => '')).includes('建议与撰写报告的模型不同'))
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

  // 只填了接入（模型留空）：也算单独配置了，不提示「未单独配置」
  const byProvider = { ...stored, judge: { ...stored.judge, provider: 'judge-provider-x', model: null } }
  const pv = await open('/settings/prefs', { handlers: [[/^GET \/settings$/, (route) => json(byProvider)(route)],
    [/^GET \/settings\/judge\/spend$/, (route) => json({ date: '2026-09-29', usd: 0, calls: 0, unpriced_calls: 0 })(route)]] })
  await pv.page.waitForSelector('[data-judge-settings]', { timeout: 6000 }).catch(() => {})
  check('只填了接入：不提示「未单独配置」', await pv.page.locator('[data-judge-settings]').count() === 1
    && await pv.page.locator('[data-judge-not-set]').count() === 0)
  await pv.close()

  // 老后端没有 judge 这一组：不显示，也不写
  const legacy = { ...stored }
  delete legacy.judge
  const old = await open('/settings/prefs', { handlers: [[/^GET \/settings$/, (route) => json(legacy)(route)]] })
  check('老后端没有裁判设置：这一组不出现', await old.page.locator('[data-judge-settings]').count() === 0)
  await old.close()
})

await section('数据 · 数据库', async () => {
  // 上传的表格按来源标记认（origin），不按路径：上传库的路径随版本变
  const dbs = sources.filter((s) => s.origin !== 'upload')
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
    origin: 'manual', current_snapshot: null,
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
    origin: 'manual', current_snapshot: null,
  }
  const upload = {
    ...base, id: 'check-manage-upmask', name: 'zz_upload_mask', kind: 'sqlite', host: null, username: null,
    database: `/tmp/agentlab/uploads/tables/check-manage-upmask/builds/${'a'.repeat(64)}.db`,
    options: { query_timeout_s: '30' }, table_count: 1, tools: ['db_query__zz_upload_mask'],
    origin: 'upload', current_snapshot: { id: '1'.repeat(64), created_at: ago(3600_000), file_name: 'orders.xlsx', raw_state: 'kept' },
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
  // 新库路径按版本存放：uploads/tables/<源 id>/builds/<构建 id>.db。卡片认的是 origin，不看路径
  const fake = {
    source: {
      id: 'check-manage-fake', name: 'sales_demo', kind: 'sqlite', host: null, port: null,
      database: `/tmp/x/uploads/tables/check-manage-fake/builds/${'b'.repeat(64)}.db`, username: null, options: {}, readonly: true,
      description: '', enabled: true, password_masked: '', has_password: false, table_count: 1,
      schema_synced_at: new Date().toISOString(), schema_error: '', available_schemas: [], tools: ['db_query__sales_demo'],
      origin: 'upload',
      current_snapshot: { id: 'c'.repeat(64), created_at: new Date().toISOString(), file_name: 'sales_demo.csv', raw_state: 'kept' },
    },
    replaced: false,
    import_id: 'check-manage-import', snapshot_id: 'c'.repeat(64), build_reused: false,
    skipped_sheets: [], conversions: [], warnings: [],
    // name 是 SQL 列名，header 是原表头：像不像一行数据按原表头判断（「2026-01」清洗成 c_2026_01 就看不出来了）
    tables: [{ name: 'sales_demo', sheet: 'sales_demo', rows: 12, region: 'A1:C13', unshaped: false, blank_rows_skipped: 0,
      columns_trimmed: [], columns: [
        { name: 'c_2026_01', type: 'REAL', header: '2026-01' }, { name: 'region', type: 'TEXT', header: 'region' },
        { name: 'col_3', type: 'TEXT', header: '' },
      ] }],
  }
  const { page, sent, natives, close } = await open('/data/tables', {
    uploadProgress: true,
    recordForms: true,
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
  const notice = await dlg.locator('[data-raw-notice]').innerText().catch(() => '')
  check('上传之前告知：原件（含隐藏工作表）保存在服务端，用于核对和追溯，之后可以清除',
        ['原始文件', '隐藏工作表', '服务端', '核对和追溯', '清除'].every((w) => notice.includes(w)), notice)
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
  check('像数据的表头标黄并提示改表头行号（按原表头判断，不按清洗后的列名）',
        await dialog(page).locator('[data-suspicious]').count() === 2 && res.includes('第 2 行'))
  const firstForm = (await uploadForms(page))[0] ?? {}
  check('第一次上传不替用户做决定：数字列混入非数字默认拒收，不按原样导入',
        firstForm.mixed === 'reject' && firstForm.raw_mode === 'false' && firstForm.header_row === '1', JSON.stringify(firstForm))
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

await section('数据 · 表格：导入前的决定与回执', async () => {
  // 一张仿客流表的交叉表（标签都是假名）：服务端先说结构不规整（shape），选了按原样导入之后又说
  // 数字列混进了占位符（mixed），选了存为空值才导入成功。服务端的回答排在 replies 里，一次取一个
  const src = {
    id: 'check-manage-decide', name: 'zz_flow', kind: 'sqlite', host: null, port: null,
    database: `/tmp/x/uploads/tables/check-manage-decide/builds/${'d'.repeat(64)}.db`, username: null, options: {},
    readonly: true, description: '', enabled: true, password_masked: '', has_password: false, table_count: 1,
    schema_synced_at: ago(0), cached_schema: '', schema_error: '', available_schemas: [], tools: ['db_query__zz_flow'],
    last_checked_at: null, last_check_ok: null, last_latency_ms: null, last_error: null,
    origin: 'upload', current_snapshot: { id: 'e'.repeat(64), created_at: ago(0), file_name: 'zz_flow.csv', raw_state: 'kept' },
  }
  // detail 是服务端那句原话：界面画的是选择页，不该把它当报错弹出来
  const shape = {
    detail: '检查脚本的 422 原话：结构不规整',
    decision: { kind: 'shape', details: { reasons: [
      { sheet: '时段客流', kind: 'date_header', cells: ['B1:H1'],
        message: '第 1 行（表头）有 7 个日期样式的单元格（B1:H1），疑似日期横排的交叉表' },
      { sheet: '时段客流', kind: 'section_title', cells: ['A6', 'A11'],
        message: '第 6、11 行只有一个单元格有文字、其余列为空（A6「分区乙」、A11「分区丙」），疑似分段标题或备注' },
    ] } },
  }
  const mixed = {
    detail: '检查脚本的 422 原话：数字列混入非数字',
    decision: { kind: 'mixed', details: { columns: [
      { sheet: '时段客流', table: '时段客流', column: 'c_8月1日', header: '8月1日', numeric: 14, nonnumeric: 4,
        values: [{ value: '·', count: 3 }, { value: 'N/A', count: 1 }] },
    ] } },
  }
  const receipt = {
    source: src, replaced: false, import_id: 'check-manage-imp', snapshot_id: 'e'.repeat(64), build_reused: false,
    skipped_sheets: [
      { sheet: '备注', state: 'hidden', reason: 'hidden' },
      { sheet: '口令', state: 'veryHidden', reason: 'hidden' },
      { sheet: 'Sheet3', state: 'visible', reason: 'empty' },
    ],
    conversions: [
      { table: '时段客流', column: 'c_8月1日', kind: 'nonnumeric_to_null', count: 4, examples: ['·', 'N/A'] },
      { table: '时段客流', column: '合计', kind: 'thousands_separator', count: 2, examples: ['1,234', '2,345'] },
    ],
    warnings: ['表「时段客流」按原样导入、未经规整（表头是横排的日期、表内有分段标题行）：同一列里混有不同口径的行，不能直接对列求和'],
    tables: [{
      name: '时段客流', sheet: '时段客流', rows: 18, region: 'A1:I20', unshaped: true, blank_rows_skipped: 2,
      columns_trimmed: ['J'],
      columns: [
        { name: '分区', type: 'TEXT', header: '分区' }, { name: 'c_8月1日', type: 'REAL', header: '8月1日' },
        { name: '合计', type: 'INTEGER', header: '合计' },
      ],
    }],
  }
  const replies = []
  const { page, sent, natives, errors, close } = await open('/data/tables', {
    recordForms: true,
    handlers: [
      [/^POST \/datasources\/upload$/, (route) => {
        const [body, status] = replies.shift() ?? [{ detail: '检查脚本没有准备这次的回答' }, 500]
        return json(body, status)(route)
      }],
      [/^GET \/datasources\/check-manage-decide\/schema$/, json({ tables: ['时段客流'], summary: '', synced_at: ago(0) })],
    ],
  })
  const posts = () => sent.filter((s) => s.key === 'POST /datasources/upload').length
  const body = () => page.locator('body').innerText()
  await page.getByRole('button', { name: /传表格/ }).first().click()
  let dlg = dialog(page)
  await dlg.locator('input[type="file"]').setInputFiles({ name: 'zz_flow.csv', mimeType: 'text/csv', buffer: Buffer.from('分区,8月1日\n分区甲,12\n') })

  // ---- 结构不规整：逐条原因带坐标，说明要按配方导入，「按原样导入」旁边写后果
  replies.push([shape, 422])
  await dlg.getByRole('button', { name: /导入/ }).click()
  const shapeView = page.locator('[data-upload-decision="shape"]')
  await shapeView.waitFor({ timeout: 5000 }).catch(() => {})
  dlg = dialog(page)
  const shapeText = await shapeView.innerText().catch(() => '')
  check('结构不规整（422 shape）：弹窗换成选择页，不弹报错', await shapeView.count() === 1
        && !(await body()).includes('检查脚本的 422 原话'), shapeText.slice(0, 80))
  check('……逐条列出原因，带坐标', await shapeView.locator('[data-shape-reason]').count() === 2
        && shapeText.includes('表头是横排的日期') && shapeText.includes('表内有分段标题行')
        && (await shapeView.locator('[data-shape-cells]').first().innerText()).includes('B1:H1')
        && shapeText.includes('A6') && shapeText.includes('A11'), shapeText.replace(/\s+/g, ' ').slice(0, 200))
  check('……说明这类表要按配方导入', (await shapeView.locator('[data-shape-recipe]').innerText().catch(() => '')).includes('按配方导入'))
  const rawBtn = dlg.getByRole('button', { name: '按原样导入（未规整）' })
  const consequence = dlg.locator('[data-raw-consequence]')
  const consequenceText = await consequence.innerText().catch(() => '')
  const adjacent = await rawBtn.evaluate((el) => {
    const id = el.getAttribute('aria-describedby')
    const note = id && document.getElementById(id)
    return !!note && note.parentElement === el.parentElement && note.hasAttribute('data-raw-consequence')
  }).catch(() => false)
  check('……「按原样导入（未规整）」旁边写明后果：同一列混有不同口径的行，不能直接对列求和',
        await rawBtn.count() === 1 && adjacent && consequenceText.includes('不同口径') && consequenceText.includes('不能直接对列求和'),
        consequenceText)
  const firstForm = (await uploadForms(page))[0] ?? {}
  check('……第一次上传默认拒收：mixed=reject、raw_mode=false',
        firstForm.mixed === 'reject' && firstForm.raw_mode === 'false', JSON.stringify(firstForm))
  await shot(page, 'table-decision-shape')

  // ---- 决定页上按 Esc、点 ×：文件还在，先问一句，不直接关掉丢了文件
  const asking = () => dialog(page).locator('[role="alert"]', { hasText: '有未保存的修改' })
  await page.keyboard.press('Escape')
  await page.waitForTimeout(150)
  check('决定页按 Esc：先问「放弃 / 继续编辑」，弹窗和选择页都还在', await asking().count() === 1
        && await shapeView.count() === 1 && await dialog(page).getByRole('button', { name: '放弃修改' }).count() === 1)
  await dialog(page).getByRole('button', { name: '继续编辑' }).click()
  await page.waitForTimeout(150)
  check('……「继续编辑」：询问收起，仍在选择页', await asking().count() === 0 && await shapeView.count() === 1)
  await dialog(page).getByRole('button', { name: '关闭' }).click()
  await page.waitForTimeout(150)
  check('……点 × 同样先问', await asking().count() === 1 && await shapeView.count() === 1)
  await dialog(page).getByRole('button', { name: '继续编辑' }).click()
  await page.waitForTimeout(150)
  check('……这几下都没有发请求', posts() === 1 && await asking().count() === 0, String(posts()))
  dlg = dialog(page)

  // ---- 取消：什么都不发，回到表单，文件还在
  await dlg.getByRole('button', { name: '取消', exact: true }).click()
  await page.waitForTimeout(200)
  dlg = dialog(page)
  check('「取消」回到表单：不再发请求，文件和名字都还在', posts() === 1 && await page.locator('[data-upload-decision]').count() === 0
        && (await dlg.innerText()).includes('zz_flow.csv') && (await dlg.locator('input.mono').first().inputValue()) === 'zz_flow')
  check('……没有选过任何处理方式', await dlg.locator('[data-upload-choices]').count() === 0)

  // ---- 再导入：选按原样导入 → 服务端又问数字列 → 选存为空值 → 成功
  replies.push([shape, 422], [mixed, 422], [receipt, 201])
  await dlg.getByRole('button', { name: /导入/ }).click()
  await shapeView.waitFor({ timeout: 5000 }).catch(() => {})
  await dialog(page).getByRole('button', { name: '按原样导入（未规整）' }).click()
  const mixedView = page.locator('[data-upload-decision="mixed"]')
  await mixedView.waitFor({ timeout: 5000 }).catch(() => {})
  dlg = dialog(page)
  const forms = await uploadForms(page)
  check('选「按原样导入」：带 raw_mode=true 重新上传，数字列的处理仍是拒收',
        forms[2]?.raw_mode === 'true' && forms[2]?.mixed === 'reject', JSON.stringify(forms[2] ?? {}))
  const col = mixedView.locator('[data-mixed-column="c_8月1日"]')
  const colText = await col.innerText().catch(() => '')
  check('数字列混入非数字（422 mixed）：列出每一列和其中的非数字取值及个数',
        await mixedView.count() === 1 && colText.includes('8月1日') && colText.includes('数字 14 个，非数字 4 个')
        && (await col.locator('[data-mixed-value="·"]').innerText().catch(() => '')).includes('3 个')
        && (await col.locator('[data-mixed-value="N/A"]').innerText().catch(() => '')).includes('1 个'),
        colText.replace(/\s+/g, ' '))
  check('……写明已选的「按原样导入（未规整）」', (await mixedView.locator('[data-upload-choices]').innerText().catch(() => '')).includes('按原样导入'))
  check('……两个出口：存为空值按数字导入 / 取消',
        await dlg.getByRole('button', { name: '把这些值存为空值，按数字导入' }).count() === 1
        && await dlg.getByRole('button', { name: '取消', exact: true }).count() === 1)
  check('……不弹报错', !(await body()).includes('检查脚本的 422 原话'))
  await shot(page, 'table-decision-mixed')
  await dlg.getByRole('button', { name: '把这些值存为空值，按数字导入' }).click()
  const result = page.locator('[data-upload-result]')
  await result.waitFor({ timeout: 5000 }).catch(() => {})
  const third = (await uploadForms(page))[3] ?? {}
  check('选「存为空值」：带 mixed=null 重新上传，先前选的按原样导入一并带上',
        third.mixed === 'null' && third.raw_mode === 'true', JSON.stringify(third))
  check('一共发了 4 次（取消那次没发）', posts() === 4, String(posts()))

  // ---- 回执
  const table = result.locator('[data-upload-table="时段客流"]')
  const tableText = await table.innerText().catch(() => '')
  check('回执：未规整的表打上标记', (await table.locator('[data-unshaped]').innerText().catch(() => '')).includes('未规整'))
  const mappedChip = await table.locator('[data-header="8月1日"]').innerText().catch(() => '')
  check('……原表头和列名不同时并排写出（8月1日 → c_8月1日），相同的只写一次',
        mappedChip.includes('8月1日') && mappedChip.includes('c_8月1日') && await table.locator('[data-header]').count() === 1
        && (await result.innerText()).includes('箭头左侧为原表头'), mappedChip.replace(/\s+/g, ' '))
  const housekeeping = await table.locator('[data-upload-housekeeping]').innerText().catch(() => '')
  check('……去掉的空行和空列', housekeeping.includes('已跳过 2 个空行') && housekeeping.includes('J'), housekeeping)
  check('……导入区域', tableText.includes('A1:I20'))
  const hidden = await result.locator('[data-skipped-hidden]').innerText().catch(() => '')
  check('……跳过的隐藏工作表（深度隐藏的注明）', hidden.includes('2 个隐藏工作表') && hidden.includes('「备注」')
        && hidden.includes('「口令」（深度隐藏）'), hidden)
  check('……没有内容的工作表另写一行', (await result.locator('[data-skipped-empty]').innerText().catch(() => '')).includes('Sheet3'))
  const toNull = await result.locator('[data-conversion="nonnumeric_to_null"]').innerText().catch(() => '')
  const thousands = await result.locator('[data-conversion="thousands_separator"]').innerText().catch(() => '')
  check('……类型转换：哪一列、怎么转、多少个、举例', toNull.includes('c_8月1日') && toNull.includes('非数字的值已存为空值')
        && toNull.includes('共 4 个') && toNull.includes('「·」') && thousands.includes('千分位') && thousands.includes('「1,234」'),
        `${toNull} | ${thousands}`)
  const warnings = result.locator('[data-upload-warnings] li')
  check('……警告逐条列出', await warnings.count() === 1 && (await warnings.first().innerText()).includes('不能直接对列求和'))
  check('……列表里有了这张表（按来源分到「表格」）', await page.locator('[data-source="zz_flow"]').count() === 1)
  await shot(page, 'table-upload-receipt')

  // ---- 改表头行号：上次的回答作废，重新问
  replies.push([mixed, 422])
  await dialog(page).getByRole('button', { name: /表头有误/ }).click()
  await page.waitForTimeout(200)
  check('改表头行号重传：已选的处理方式清空', await dialog(page).locator('[data-upload-choices]').count() === 0)
  await dialog(page).getByRole('button', { name: /导入/ }).click()
  await mixedView.waitFor({ timeout: 5000 }).catch(() => {})
  const fifth = (await uploadForms(page))[4] ?? {}
  check('……换了表头行，又按默认拒收发出（不沿用上次的回答）',
        fifth.header_row === '2' && fifth.mixed === 'reject' && fifth.raw_mode === 'false', JSON.stringify(fifth))

  // ---- 认不出的决定：当普通报错弹出服务端那句话，不画空的选择页
  await dialog(page).getByRole('button', { name: '取消', exact: true }).click()
  await page.waitForTimeout(200)
  replies.push([{ detail: '检查脚本：认不出的决定', decision: { kind: 'other', details: {} } }, 422])
  await dialog(page).getByRole('button', { name: /导入/ }).click()
  await page.waitForTimeout(600)
  check('认不出的决定按普通报错显示服务端的原话，弹窗留在表单上',
        (await body()).includes('检查脚本：认不出的决定') && await page.locator('[data-upload-decision]').count() === 0
        && await dialog(page).getByRole('button', { name: /导入/ }).isEnabled())
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据 · 表格：卡片按来源区分、显示当前版本', async () => {
  const blank = {
    kind: 'sqlite', host: null, port: null, username: null, options: {}, readonly: true, description: '', enabled: true,
    password_masked: '', has_password: false, cached_schema: '', schema_error: '', available_schemas: [],
    last_checked_at: null, last_check_ok: null, last_latency_ms: null, last_error: null,
  }
  const up = {
    ...blank, id: 'check-manage-ver', name: 'zz_versioned', table_count: 2, tools: ['db_query__zz_versioned'],
    database: `/tmp/x/uploads/tables/check-manage-ver/builds/${'f'.repeat(64)}.db`, schema_synced_at: ago(30 * 24 * 3600_000),
    origin: 'upload',
    current_snapshot: { id: '2'.repeat(64), created_at: ago(3 * 3600_000), file_name: '时段客流.xlsx', raw_state: 'kept' },
  }
  // 迁移前上传的老版本：没有记下文件名，也没有原件。照真实后端的样子造：迁移补建的快照时间是迁移那一刻
  // （升级后服务启动时，这里是两分钟前），原来的同步时间（schema_synced_at，40 天前）迁移时原样保留
  const legacy = {
    ...up, id: 'check-manage-legacy', name: 'zz_legacy', table_count: 1, tools: ['db_query__zz_legacy'],
    database: '/tmp/x/uploads/tables/zz_legacy.db', schema_synced_at: ago(40 * 24 * 3600_000),
    current_snapshot: { id: '4'.repeat(64), created_at: ago(2 * 60_000), file_name: '', raw_state: 'absent' },
  }
  // 没迁移成的早期上传（数据文件缺失、迁移失败）：来源还是 manual，路径是老的 uploads/tables/<名>.db。
  // 服务端允许同名重传把它变成上传的表格，前端不知道上传目录在哪，不能拦
  let unmigrated = {
    ...blank, id: 'check-manage-unmigrated', name: 'zz_unmigrated', table_count: 0, tools: ['db_query__zz_unmigrated'],
    database: '/tmp/x/uploads/tables/zz_unmigrated.db', schema_synced_at: null, origin: 'manual', current_snapshot: null,
  }
  // 手工登记的其他库：名字照样在前端就拦住
  const pg = {
    ...blank, id: 'check-manage-pg', name: 'zz_pgdb', kind: 'postgres', host: '10.0.0.8', port: 5432, database: 'mes',
    username: 'reader', table_count: 0, tools: ['db_query__zz_pgdb'], schema_synced_at: null, origin: 'manual', current_snapshot: null,
  }
  // 手工登记的 SQLite，路径恰好长得像老的上传路径：按来源算，它在「数据库」标签
  const lookalike = {
    ...blank, id: 'check-manage-lookalike', name: 'zz_lookalike', table_count: 0, tools: ['db_query__zz_lookalike'],
    database: '/srv/old/uploads/tables/zz_lookalike.db', schema_synced_at: null, origin: 'manual', current_snapshot: null,
  }
  // 同名重传的回答按次序给（multipart 请求体按原文切太脆，发出的名字用 uploadForms 核对）：
  // 第一次是手工库，回服务端那句 409 原话；第二次是没迁移成的早期上传，就地变成上传的表格
  const replies = [
    () => json({ detail: '已存在名为「zz_lookalike」的数据源（非上传表格），请换一个名称' }, 409),
    () => {
      unmigrated = {
        ...unmigrated, origin: 'upload', table_count: 1, schema_synced_at: ago(0),
        database: `/tmp/x/uploads/tables/check-manage-unmigrated/builds/${'5'.repeat(64)}.db`,
        current_snapshot: { id: '6'.repeat(64), created_at: ago(0), file_name: 'zz_unmigrated.csv', raw_state: 'kept' },
      }
      return json({
        source: unmigrated, replaced: true, import_id: 'check-manage-imp2', snapshot_id: '6'.repeat(64), build_reused: false,
        skipped_sheets: [], conversions: [], warnings: [],
        tables: [{ name: 'zz_unmigrated', sheet: 'zz_unmigrated', rows: 1, region: 'A1:B2', unshaped: false, blank_rows_skipped: 0,
          columns_trimmed: [], columns: [{ name: 'region', type: 'TEXT', header: 'region' }, { name: 'amount', type: 'INTEGER', header: 'amount' }] }],
      }, 201)
    },
  ]
  const reupload = (route) => (replies.shift() ?? (() => json({ detail: '检查脚本没有准备这次的回答' }, 500)))()(route)
  const { page, sent, natives, errors, close } = await open('/data/tables', {
    recordForms: true,
    handlers: [
      [/^GET \/datasources$/, (route) => json([...sources, up, legacy, lookalike, unmigrated, pg])(route)],
      [/^GET \/datasources\/check-manage-ver\/schema$/, json({ tables: ['时段客流', '分区明细'], summary: '', synced_at: ago(0) })],
      [/^GET \/datasources\/check-manage-legacy\/schema$/, json({ tables: ['sales'], summary: '', synced_at: ago(0) })],
      [/^GET \/datasources\/check-manage-unmigrated\/schema$/, json({ tables: ['zz_unmigrated'], summary: '', synced_at: ago(0) })],
      [/^POST \/datasources\/[^/]+\/introspect/, json({ detail: '上传的表格无需重新探查，重新上传即可更新' }, 409)],
      [/^POST \/datasources\/upload$/, reupload],
    ],
  })
  const card = page.locator(`[data-source="${up.name}"]`)
  check('「表格」标签按来源认：上传的在，路径像上传库的手工 SQLite 不在',
        await card.count() === 1 && await page.locator('[data-source="zz_legacy"]').count() === 1
        && await page.locator('[data-source="zz_lookalike"]').count() === 0)
  const version = card.locator('[data-current-version]')
  const versionText = await version.innerText().catch(() => '')
  check('卡片写当前版本的文件名和导入时间', versionText.includes('当前版本') && versionText.includes('时段客流.xlsx')
        && versionText.includes('3 小时前导入') && ((await version.getAttribute('title')) ?? '').includes('导入'), versionText)
  check('……原件已保存时不另标', !versionText.includes('原件'))
  const legacyVersion = page.locator('[data-source="zz_legacy"] [data-current-version]')
  const legacyText = await legacyVersion.innerText().catch(() => '')
  check('……迁移前上传的版本：没有文件名、注明未保存原件', legacyText.includes('早期上传的文件') && legacyText.includes('未保存原件'), legacyText)
  check('……不把迁移那一刻写成导入时间（不写「2 分钟前导入」），悬停说明未记录导入时间',
        !legacyText.includes('导入') && !legacyText.includes('分钟前') && !legacyText.includes('刚刚')
        && ((await legacyVersion.getAttribute('title')) ?? '').includes('未记录') && ((await legacyVersion.getAttribute('title')) ?? '').includes('导入时间'),
        `${legacyText} | ${await legacyVersion.getAttribute('title').catch(() => '')}`)
  const legacyCard = await page.locator('[data-source="zz_legacy"]').innerText().catch(() => '')
  check('……照写原来的同步时间（40 天前的「结构同步于」），不提示结构过期',
        legacyCard.includes('结构同步于') && !legacyCard.includes('分钟前') && !legacyCard.includes('库表可能已变更'),
        legacyCard.replace(/\s+/g, ' '))
  const cardText = await card.innerText()
  check('上传的表格不给「探查结构」「按配置重新探查」（服务端会拒绝）',
        await card.getByRole('button', { name: /探查/ }).count() === 0 && !cardText.includes('探查结构'))
  check('……不写「结构同步于」，也不提示结构过期（版本冻结，导入时间写在版本那一行）',
        !cardText.includes('结构同步于') && !cardText.includes('库表可能已变更'))
  check('……路径按版本变，卡片上不写库文件路径', !cardText.includes('/builds/') && !cardText.includes('.db'))
  await card.getByRole('button', { name: '重新上传' }).click()
  const dlg = dialog(page)
  const nameBox = dlg.locator('input.mono').first()
  const importBtn = dlg.getByRole('button', { name: /导入/ })
  check('同名重新上传：名字已填好，不算被占用', (await nameBox.inputValue()) === up.name
        && !(await dlg.innerText()).includes('已被其他数据库用作标识'))
  await nameBox.fill('zz_pgdb')
  check('……手工登记的其他库的名字不能拿来上传（按来源认，不按路径）',
        (await dlg.innerText()).includes('「zz_pgdb」已被其他数据库用作标识') && await importBtn.isDisabled())
  await dlg.locator('input[type="file"]').setInputFiles({ name: 'zz_unmigrated.csv', mimeType: 'text/csv', buffer: Buffer.from('region,amount\neast,1\n') })
  // 手工登记的 SQLite：可能是没迁移成的早期上传，前端不拦，提示一句，由服务端判断
  await nameBox.fill('zz_lookalike')
  const sqliteHint = await dlg.locator('[data-name-sqlite]').innerText().catch(() => '')
  check('……和手工登记的 SQLite 重名：前端不拦（不知道上传目录在哪），提示早期上传的表格可同名替换、否则将被拒绝',
        !(await dlg.innerText()).includes('已被其他数据库用作标识') && await importBtn.isEnabled()
        && sqliteHint.includes('早期上传的表格可同名替换') && sqliteHint.includes('拒绝'), sqliteHint)
  await importBtn.click()
  await until(async () => (await dlg.innerText()).includes('非上传表格'), 5000)
  await page.waitForTimeout(100)
  const refused = await dlg.innerText()
  check('……照填的名字发出', (await uploadForms(page)).at(-1)?.name === 'zz_lookalike', JSON.stringify((await uploadForms(page)).at(-1) ?? {}))
  check('……服务端以重名拒收（409）：原话写在名字下面，弹窗留在表单上，焦点回到名字',
        refused.includes('已存在名为「zz_lookalike」的数据源（非上传表格），请换一个名称')
        && await page.locator('[data-upload-result]').count() === 0 && await importBtn.isDisabled()
        && await nameBox.evaluate((el) => el === document.activeElement && el.getAttribute('aria-invalid') === 'true'),
        refused.replace(/\s+/g, ' ').slice(0, 160))
  await nameBox.fill('zz_unmigrated')
  check('……改了名字，那句 409 就不再显示', !(await dlg.innerText()).includes('非上传表格') && await importBtn.isEnabled()
        && await dlg.locator('[data-name-sqlite]').count() === 1)
  await importBtn.click()
  const replacedResult = page.locator('[data-upload-result]')
  await replacedResult.waitFor({ timeout: 5000 }).catch(() => {})
  const lastForm = (await uploadForms(page)).at(-1) ?? {}
  check('没迁移成的早期上传可以同名重传：照名字发出，服务端替换后写「已替换」',
        lastForm.name === 'zz_unmigrated' && await replacedResult.count() === 1
        && (await dialog(page).innerText()).includes('已替换「zz_unmigrated」里的数据'), JSON.stringify(lastForm))
  await dialog(page).getByRole('button', { name: '列名无误 · 完成' }).click()
  await page.waitForTimeout(300)
  const revived = page.locator('[data-source="zz_unmigrated"]')
  check('……替换后按来源分到「表格」，写当前版本的文件名和「重新上传」',
        await revived.count() === 1 && (await revived.locator('[data-current-version]').innerText().catch(() => '')).includes('zz_unmigrated.csv')
        && await revived.getByRole('button', { name: '重新上传' }).count() === 1)
  await goto(page, '/data/databases')
  const manual = page.locator('[data-source="zz_lookalike"]')
  check('「数据库」标签里是手工 SQLite（照常能探查结构），没有上传的表格',
        await manual.count() === 1 && await manual.getByRole('button', { name: /探查结构/ }).count() === 1
        && await page.locator(`[data-source="${up.name}"]`).count() === 0
        && await page.locator('[data-source="zz_unmigrated"]').count() === 0)
  check('没有对上传的表格发探查请求', !sent.some((s) => /introspect/.test(s.key)))
  check('上传一共发了 2 次（被拒收一次、替换一次）', sent.filter((s) => s.key === 'POST /datasources/upload').length === 2,
        String(sent.filter((s) => s.key === 'POST /datasources/upload').length))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

// ---------------------------------------------------------------------------
// 按配方导入（期 2）：接口全部伪造。夹具是契约里的参考配方（「结构仿照客流表」，假名）和照它造的
// 假暂存区：网格按 P2-SPEC 9.1 的坐标摆（B2:AG30，非空格 771），另加一行没有去处的 B32:C32
// ---------------------------------------------------------------------------
const FLOW_RECIPE = JSON.parse(readFileSync(new URL('../backend/tests/fixtures/recipes/flow_recipe.json', import.meta.url), 'utf8'))
const RECIPE_UNITS = ['人次', '人', '元', '万元', '千元', '亿元', '千克', '吨', '个', '件', '户', '%']
const RECIPE_CANDIDATES = { '日间时段客流（人次）': ['日间', '日间时段客流'], '夜间时段客流（人次）': ['夜间', '夜间时段客流'] }
// 参考配方没有列表块：另加一张「分区明细」工作表，给配方面板的「数据中间的空行」那一项用
const recipeWithList = () => {
  const r = JSON.parse(JSON.stringify(FLOW_RECIPE))
  r.sheets.push({ id: 's2', match: { name: '分区明细' }, blocks: [{ id: '列表1', layout: 'list', table: '分区明细', columns: [
    { header: '分区', name: '分区', type: 'TEXT' }, { header: '客流（人次）', name: '客流', type: 'INTEGER' },
  ] }] })
  r.tables.push({ name: '分区明细', grain: ['分区'], units: { 客流: '人次' } })
  return r
}
function flowGrid() {
  const col = (n) => { let s = ''; let x = n; while (x > 0) { s = String.fromCharCode(65 + ((x - 1) % 26)) + s; x = Math.floor((x - 1) / 26) } return s }
  const days = 31
  const cells = []
  const formulas = {}
  cells.push([2, 2, '统计时间范围：2026年8月1日至2026年8月31日', 'text'], [3, 2, '客流汇总表', 'text'])
  for (let d = 1; d <= days; d++) cells.push([4, 2 + d, `8月${d}日`, 'text'])
  ;['全日客流（人次）', '分区甲（人次）', '分区乙（人次）'].forEach((l, i) => cells.push([5 + i, 2, l, 'text']))
  for (let d = 1; d <= days; d++) {
    const a = 3000 + ((d * 137) % 6000)
    const b = 2000 + ((d * 91) % 6000)
    cells.push([5, 2 + d, String(a + b), 'number'], [6, 2 + d, String(a), 'number'], [7, 2 + d, String(b), 'number'])
  }
  cells.push([9, 2, '日间时段客流（人次）', 'text'])
  for (let h = 7; h < 18; h++) {
    const r = 10 + h - 7
    cells.push([r, 2, `${h}-${h + 1}`, 'text'])
    for (let d = 1; d <= days; d++) cells.push([r, 2 + d, h === 7 ? '·' : String(50 + ((d * h * 13) % 850)), h === 7 ? 'text' : 'number'])
  }
  cells.push([21, 2, '夜间时段客流（人次）', 'text'])
  for (let h = 18; h < 24; h++) {
    const r = 22 + h - 18
    cells.push([r, 2, `${h}-${h + 1}`, 'text'])
    for (let d = 1; d <= days; d++) cells.push([r, 2 + d, String(50 + ((d * h * 7) % 850)), 'number'])
  }
  ;[['18-22 时合计', 22, 25], ['22-24 时合计', 26, 27], ['18-24 时合计', 22, 27]].forEach(([label, r1, r2], i) => {
    cells.push([28 + i, 2, label, 'text'])
    for (let d = 1; d <= days; d++) {
      cells.push([28 + i, 2 + d, String(1000 + d), 'formula'])
      formulas[`${col(2 + d)}${28 + i}`] = `=SUM(${col(2 + d)}${r1}:${col(2 + d)}${r2})`
    }
  })
  // 表下补录的一行：没有分段认领它（P2-SPEC 9.2 的 D21）
  cells.push([32, 2, '补录（人次）', 'text'], [32, 3, '1,234', 'text'])
  const rows = []; for (let r = 2; r <= 32; r++) rows.push(r)
  const cols = []; for (let c = 2; c <= 33; c++) cols.push(c)
  return {
    sheet: '客流汇总', bounds: 'B2:AG32', total_rows: 31, total_cols: 32, truncated: false, rows, cols, cells, formulas,
    merges: ['B2:AG2', 'B3:AG3', 'B8:AG8', 'B9:AG9', 'B21:AG21'], hidden_rows: [], hidden_cols: [],
  }
}
const FLOW_MARKS = [
  ['context', 'B2'], ['outside_text', 'B3'], ['col_header', 'C4:AG4'], ['row_label', 'B5:B7'], ['value', 'C5:AG7'],
  ['section_title', 'B9'], ['row_label', 'B10:B20'], ['value', 'C10:AG20'], ['section_title', 'B21'], ['row_label', 'B22:B27'],
  ['value', 'C22:AG27'], ['derived_label', 'B28:B30'], ['derived_value', 'C28:AG30'],
].map(([role, ref]) => ({ sheet: '客流汇总', role, ref }))
const FLOW_CARDS = [
  { id: 'axis', title: '第 4 行是日期表头：按交叉表导入', reason: 'C4:AG4 共 31 格都能解析为日期', cells: ['客流汇总!C4:AG4'] },
  { id: 'period', title: '统计期取自 B2', reason: 'B2 能解析出 2026-08-01 至 2026-08-31，没有别处冲突', cells: ['客流汇总!B2'] },
  { id: 'titles', title: '分段标题：B9、B21', reason: '标签列有字、日期列全空的行按分段标题处理', cells: ['客流汇总!B9', '客流汇总!B21'] },
  { id: 'f1', title: '第 5 行 = 第 6 行 + 第 7 行', reason: '31 列中 31 列成立，建议登记为每期核对', cells: ['客流汇总!B5:B7'], question: 'q_relation:F1' },
  { id: 'merge', title: '日间、夜间合进一张表', reason: '两段标签都是时段、互不相交；类别从标题的候选词里选', cells: ['客流汇总!B9', '客流汇总!B21'] },
  { id: 'placeholder', title: '「·」表示无数据吗', reason: '第 10 行整行都是「·」', cells: ['客流汇总!C10:AG10'], question: 'q_placeholder:·' },
  { id: 'derived', title: '第 28–30 行是合计：改作核对并另存', reason: '标签能解析出时段区间，值是汇总上方几行的公式', cells: ['客流汇总!B28:B30'] },
  { id: 'f2', title: '各时段之和与全日客流 31 天中 0 天相等', reason: '口径不同，建议登记为「口径不同」', cells: ['客流汇总!B5'], question: 'q_relation:F2' },
  { id: 'outside', title: 'B3 是区域外文字', reason: '不含数字，只记进回执', cells: ['客流汇总!B3'] },
  { id: 'mode', title: '导入模式：每期替换', reason: '按期累积将在后续版本提供', cells: [] },
]
const relationQuestion = (id, extra = {}) => ({
  id: `q_relation:${id}`, text: `系统发现的关系 ${id} 要登记吗`,
  options: [{ value: 'register', label: '登记', needs_reason: false }, { value: 'dismiss', label: '不登记', needs_reason: true }],
  ...extra,
})
// 前两题带起草器的建议（契约 Question.default）：界面只写「建议：…」，不替人选
const FLOW_QUESTIONS = [
  { id: 'q_placeholder:·', text: '「·」是否表示无数据', default: 'null', options: [
    { value: 'null', label: '是，存为空值', needs_reason: false }, { value: 'reject', label: '不是，不导入', needs_reason: false },
  ] },
  relationQuestion('F1', { default: 'register' }), relationQuestion('F2'),
]
const FLOW_CONFIRMS = [
  ['placeholder:·', '「·」存为空值（31 格）'], ['derived:夜间合计', '第 28–30 行改作核对，原值另存表「时段客流_表内合计」'],
  ['year_from:交叉表', '表头只写月日，年份取自统计期'], ['relation:R1', '每期核对「全日客流 = 分区甲 + 分区乙」'],
  ['relation:R2', '「时段客流」与「日客流」口径不同'], ['unit:日客流.全日客流', '「全日客流（人次）」→ 列「全日客流」，单位 人次'],
  ['unit:日客流.分区甲', '「分区甲（人次）」→ 列「分区甲」，单位 人次'], ['unit:日客流.分区乙', '「分区乙（人次）」→ 列「分区乙」，单位 人次'],
  ['unit:时段客流.客流', '列「客流」，单位 人次'], ['unit:时段客流_表内合计.客流', '列「客流」，单位 人次'], ['mode', '导入模式：每期替换'],
].map(([id, label]) => ({ id, label, detail: '', required: true, source: 'recipe' }))
// 外加一条不必勾的（ConfirmItem.required=false）：检查脚本不勾它，提交的 confirmations 里就不能有它
const OPTIONAL_CONFIRM = { id: 'sheet_extra:说明', label: '另有可见工作表「说明」，不导入', detail: '', required: false, source: 'sheet' }
FLOW_CONFIRMS.push(OPTIONAL_CONFIRM)
const flowCheck = (id, kind, title, status, extra = {}) => ({
  id, kind, title, status, category: status === 'info' ? 'info' : 'structure', checked: 31, failed: 0, unverifiable: 0,
  details: [], cells: [], acceptable: false, reasons: {}, ...extra,
})
function flowTrial(status, extra = {}) {
  const checks = status === 'needs_decision' ? [
    flowCheck('C1', 'context_agree', '统计期多处一致', 'passed', { checked: 1 }),
    flowCheck('K1', 'derived_sum', '夜间合计按时段区间重算', 'unverifiable', { category: 'structure', acceptable: true, unverifiable: 31,
      details: ['明细含无数据占位符：31 格'], reasons: { null_detail: 31 }, cells: ['客流汇总!C28'] }),
    flowCheck('R1', 'relation_sum_eq', '全日客流 = 分区甲 + 分区乙', 'mismatch', { category: 'data_quality', acceptable: true, failed: 2,
      details: ['8 月 3 日：全日客流与分区之和不等'], cells: ['客流汇总!E5'] }),
    flowCheck('R2', 'relation_not_comparable', '「时段客流」与「日客流」口径不同', 'info', { failed: 31, details: ['31 天中 0 天相等'] }),
  ] : [
    flowCheck('C1', 'context_agree', '统计期多处一致', 'passed', { checked: 1 }),
    flowCheck('K1', 'derived_sum', '夜间合计按时段区间重算', 'passed', { checked: 93 }),
    flowCheck('R1', 'relation_sum_eq', '全日客流 = 分区甲 + 分区乙', 'passed'),
    flowCheck('R2', 'relation_not_comparable', '「时段客流」与「日客流」口径不同', 'info', { failed: 31, details: ['31 天中 0 天相等'] }),
  ]
  return {
    trial_id: `trial-${status}-${Math.random().toString(36).slice(2, 8)}`, status,
    receipt: {
      ledger: [{ sheet: '客流汇总', nonempty_scan: 771, nonempty_read: 771, unclaimed: 0, roles: {
        value: 620, derived_value: 93, derived_label: 3, col_header: 31, row_label: 20, section_title: 2, context: 1, outside_text: 1,
      } }],
      tables: [
        { name: '日客流', sheet: '客流汇总', kind: 'data', rows: 31, grain: ['日期'], columns: [
          { name: '日期', type: 'TEXT' }, { name: '全日客流', type: 'INTEGER', unit: '人次' },
          { name: '分区甲', type: 'INTEGER', unit: '人次' }, { name: '分区乙', type: 'INTEGER', unit: '人次' }] },
        { name: '时段客流', sheet: '客流汇总', kind: 'data', rows: 527, grain: ['日期', '时段'], columns: [
          { name: '日期', type: 'TEXT' }, { name: '时段', type: 'TEXT' }, { name: '客流', type: 'INTEGER', unit: '人次' }] },
        { name: '时段客流_表内合计', sheet: '客流汇总', kind: 'reported_total', rows: 93, grain: ['日期', '合计项'], columns: [
          { name: '日期', type: 'TEXT' }, { name: '合计项', type: 'TEXT' }, { name: '客流', type: 'INTEGER', unit: '人次' }] },
      ],
      period: status === 'needs_input' ? null : { start: '2026-08-01', end: '2026-08-31', source: 'cells', cells: ['客流汇总!B2'] },
      placeholders: { '·': 31 }, outside_text: [{ sheet: '客流汇总', cell: '客流汇总!B3', text: '客流汇总表', kind: 'text' }],
      db_sha256: 'a'.repeat(64),
    },
    problems: status === 'needs_input'
      ? [{ code: 'period_missing', category: 'input', message: '未能从表格中解析出统计期，请为本期录入统计期', cells: [] }] : [],
    checks: status === 'needs_input' ? [] : checks,
    confirm_items: FLOW_CONFIRMS,
    acceptable: status === 'needs_decision' ? ['K1', 'R1'] : [],
    notes: { 日客流: { comment: '按日的客流，已逐日核对全日客流等于两个分区之和。', columns: { 全日客流: '单位：人次' } } },
    diff: null, same_as_import: null, base_snapshot_id: null,
    ...extra,
  }
}
function flowStaging(over = {}) {
  return {
    id: 'stg-flow', kind: 'first', status: 'drafting',
    source: { id: 'src-new-flow', name: 'zz_flow_recipe', exists: false, import_mode: null },
    file: { name: '月报导出_2026-08-01_2026-08-31.xlsx', size: 12345, sha256_prefix: 'a1b2c3d4' },
    sheets: [{ name: '客流汇总', state: 'visible', bounds: 'B2:AG32', nonempty: 773, merged: 5, formulas: 93, formulas_uncached: 0, hidden_rows: 0, hidden_cols: 0 }],
    skipped_sheets: [], full_calc_on_load: false, grids: [flowGrid()],
    draft: { recipe: recipeWithList(), complete: true, origin: 'rules', cards: FLOW_CARDS, questions: FLOW_QUESTIONS, failures: [] },
    ai_draft: null, ai: { offered: false, available: true, reason: '', model: '检查脚本模型', provider: '检查脚本接入' }, ai_consents: [],
    recipe: recipeWithList(), recipe_origin: 'rules', recipe_problems: [], answers: {},
    cards: FLOW_CARDS, questions: FLOW_QUESTIONS,
    draft_problems: [{ code: 'row_unclaimed', category: 'structure', message: '第 32 行：标签列有字、日期列有值，没有分段认领这一行', cells: ['客流汇总!B32:C32'] }],
    draft_partial: false, candidates: RECIPE_CANDIDATES, units: RECIPE_UNITS, marks: FLOW_MARKS, trial: null, context_inputs: {},
    created_at: ago(60_000), updated_at: ago(0), expires_at: ago(-7 * 24 * 3600_000),
    ...over,
  }
}
const RECIPE_SRC_BASE = {
  kind: 'sqlite', host: null, port: null, username: null, options: {}, readonly: true, description: '', enabled: true,
  password_masked: '', has_password: false, cached_schema: '', schema_error: '', available_schemas: [],
  last_checked_at: null, last_check_ok: null, last_latency_ms: null, last_error: null, table_count: 3,
  schema_synced_at: ago(0), origin: 'upload', import_mode: 'recipe',
  current_snapshot: { id: '7'.repeat(64), created_at: ago(3600_000), file_name: '月报导出_2026-07-01_2026-07-31.xlsx', raw_state: 'kept' },
  current_recipe: { id: 'rcp-1', seq: 1, origin: 'rules', activated_at: ago(3600_000), signed_by: '检查脚本' },
  open_staging: null,
}
const recipeSource = (id, name, over = {}) => ({
  ...RECIPE_SRC_BASE, id, name, tools: [`db_query__${name}`], database: `/tmp/x/uploads/tables/${id}/builds/${'8'.repeat(64)}.db`, ...over,
})
const coded = (status, code, detail) => json({ detail, code }, status)
/** 确认清单：逐条勾上全部必勾的确认项（不点任何「全选」；标了「可选」的不勾） */
const tickRequired = async (list) => {
  const boxes = list.locator('[data-confirm-item][data-required="true"] input[type="checkbox"]')
  for (let i = 0; i < await boxes.count(); i++) await boxes.nth(i).check()
}

await section('数据 · 表格：按配方导入', async () => {
  // 服务端的回答排队：每个接口按到达顺序取一个；没备好的回 500，免得静默通过
  const replies = { stage: [], answers: [], recipe: [], trial: [], commit: [], get: [], preview: [], draftAi: [] }
  const take = (k) => (route, ctx) => {
    const r = replies[k].shift()
    return r ? r(route, ctx) : json({ detail: `检查脚本没有准备这次的回答（${k}）` }, 500)(route)
  }
  let current = flowStaging()
  const echo = (patch = {}) => (route, { body }) => {
    current = { ...current, ...patch, ...(body?.recipe ? { recipe: body.recipe, answers: {} } : {}) }
    return json(current)(route)
  }
  const shapeReply = {
    detail: '检查脚本的 422 原话：结构不规整',
    decision: { kind: 'shape', details: { reasons: [{ sheet: '客流汇总', kind: 'date_header', cells: ['C4:AG4'], message: '第 4 行有 31 个日期样式的单元格（C4:AG4），疑似日期横排的交叉表' }] } },
  }
  const recipeSrc = recipeSource('check-manage-recipe', 'zz_recipe_card')
  const aiSrc = recipeSource('check-manage-ai', 'zz_ai_offer', { import_mode: 'simple', open_staging: { id: 'stg-ai', kind: 'switch', status: 'drafting', created_at: ago(60_000) } })
  const aiOffSrc = recipeSource('check-manage-ai-off', 'zz_ai_unavailable', { import_mode: 'simple', open_staging: { id: 'stg-ai-off', kind: 'switch', status: 'drafting', created_at: ago(60_000) } })
  const PREVIEW = { text: '## 工作表「客流汇总」 已用区域 B2:AG30，非空格 771\n合并：B2:AG2、B3:AG3\n数字（只有类型）：\n  C5:AG7 整数', chars: 66, sha256: 'f'.repeat(64), model: '检查脚本模型', provider: '检查脚本接入' }
  const { page, sent, natives, errors, close } = await open('/data/tables', {
    recordForms: true,
    handlers: [
      [/^GET \/datasources$/, (route) => json([...sources, recipeSrc, aiSrc, aiOffSrc])(route)],
      [/^GET \/datasources\/check-manage-[^/]+\/schema$/, json({ tables: ['日客流'], summary: '', synced_at: ago(0) })],
      [/^POST \/datasources\/upload$/, json(shapeReply, 422)],
      [/^POST \/datasources\/imports\/stage$/, take('stage')],
      [/^GET \/datasources\/imports\/[^/]+\/draft-ai\/preview$/, take('preview')],
      [/^POST \/datasources\/imports\/[^/]+\/draft-ai$/, take('draftAi')],
      [/^GET \/datasources\/imports\/[^/]+$/, take('get')],
      [/^POST \/datasources\/imports\/[^/]+\/answers$/, take('answers')],
      [/^PUT \/datasources\/imports\/[^/]+\/recipe$/, take('recipe')],
      [/^POST \/datasources\/imports\/[^/]+\/trial$/, take('trial')],
      [/^POST \/datasources\/imports\/[^/]+\/commit$/, take('commit')],
    ],
  })
  const count = (key) => sent.filter((s) => s.key === key || new RegExp(key).test(s.key)).length
  const last = (re) => [...sent].reverse().find((s) => re.test(s.key))
  const wizard = () => dialog(page)

  // ---- 卡片：按配方导入的源换成「上传新一期」「修改配方」，没有「重新上传」
  const rcard = page.locator('[data-source="zz_recipe_card"]')
  check('按配方导入的源：卡片上有「上传新一期」「修改配方」，没有「重新上传」',
        await rcard.getByRole('button', { name: '上传新一期' }).count() === 1
        && await rcard.getByRole('button', { name: '修改配方' }).count() === 1
        && await rcard.getByRole('button', { name: '重新上传' }).count() === 0)
  const recipeLine = await rcard.locator('[data-current-recipe]').innerText().catch(() => '')
  check('……卡片上写当前配方：第几版、起草方式、启用时间、署名（未认证）',
        recipeLine.includes('配方第 1 版') && recipeLine.includes('规则起草') && recipeLine.includes('启用')
        && recipeLine.includes('署名「检查脚本」（未认证）'), recipeLine)
  check('……简单导入的源不写配方信息', await page.locator('[data-source="zz_ai_offer"] [data-current-recipe]').count() === 0)
  check('表格标签页头部有「按配方导入」入口', await page.locator('[data-recipe-entry]').count() === 1)

  // ---- 头部入口：选文件区接得住拖进来的文件；提示的格式与选择框收的一致
  await page.locator('[data-recipe-entry]').click()
  const dropZone = dialog(page).locator('[data-wizard-drop]')
  await dropZone.waitFor({ timeout: 4000 }).catch(() => {})
  const pickHint = await dropZone.innerText().catch(() => '')
  const accept = await dropZone.locator('input[type="file"]').getAttribute('accept').catch(() => '')
  check('选文件区写「拖入文件」，提示的格式与选择框收的一致（.xlsx、.xlsm）', pickHint.includes('拖入文件')
        && accept === '.xlsx,.xlsm' && pickHint.includes('.xlsx') && pickHint.includes('.xlsm'), `${pickHint} | ${accept}`)
  const dropFile = async (name) => {
    const dt = await page.evaluateHandle((n) => {
      const d = new DataTransfer()
      d.items.add(new File(['PK'], n, { type: 'application/octet-stream' }))
      return d
    }, name)
    await dropZone.dispatchEvent('dragover', { dataTransfer: dt })
    await dropZone.dispatchEvent('drop', { dataTransfer: dt })
    await page.waitForTimeout(200)
  }
  await dropFile('drop_flow.csv')
  check('……拖进来的不是 Excel：不选中，说明只支持 .xlsx、.xlsm', await dropZone.locator('[data-wizard-file]').count() === 0
        && (await page.locator('body').innerText()).includes('不是 Excel 文件'))
  await dropFile('drop_flow.xlsx')
  check('……拖进来的文件被选中，名字取自文件名', (await dropZone.locator('[data-wizard-file]').innerText().catch(() => '')).includes('drop_flow.xlsx')
        && (await dialog(page).locator('[data-wizard-name]').inputValue().catch(() => '')) === 'drop_flow')
  await dialog(page).getByRole('button', { name: '取消', exact: true }).click()
  await page.waitForTimeout(200)

  // ---- 交叉表决定页 → 按配方导入：带着同一个文件和名字发 stage
  await page.getByRole('button', { name: /传表格/ }).first().click()
  let dlg = dialog(page)
  await dlg.locator('input[type="file"]').setInputFiles({ name: '月报导出_2026-08-01_2026-08-31.xlsx', mimeType: 'application/octet-stream', buffer: Buffer.from('PK\x03\x04') })
  await dlg.locator('input.mono').first().fill('zz_flow_recipe')
  await dlg.getByRole('button', { name: /导入/ }).click()
  const recipeBtn = page.locator('[data-shape-recipe] button', { hasText: '按配方导入' })
  await recipeBtn.waitFor({ timeout: 5000 }).catch(() => {})
  check('交叉表决定页出现「按配方导入」按钮', await recipeBtn.count() === 1)
  replies.stage.push(json(current, 201))
  await recipeBtn.click()
  const grid = page.locator('[data-sheet-grid]')
  await grid.waitFor({ timeout: 5000 }).catch(() => {})
  const stageForm = (await importForms(page)).at(-1) ?? {}
  check('……点了发 POST /datasources/imports/stage，表单里带同一个文件和名字',
        count('^POST /datasources/imports/stage$') === 1 && /imports\/stage/.test(stageForm.url ?? '')
        && stageForm.name === 'zz_flow_recipe' && stageForm.file === '<file 月报导出_2026-08-01_2026-08-31.xlsx>', JSON.stringify(stageForm))
  check('……上传弹窗让位给向导（只剩一个弹窗）', await page.locator('[role="dialog"]').count() === 1 && await page.locator('[data-upload-decision]').count() === 0)

  // ---- 网格
  check('网格：行号、列字母', await grid.locator('[data-row-header="5"]').count() === 1 && await grid.locator('[data-col-header="C"]').count() === 1)
  check('……B5 按去向着色为行标签', (await grid.locator('[data-cell="B5"]').getAttribute('data-role').catch(() => '')) === 'row_label')
  check('……C28 是合计格，悬停显示公式', (await grid.locator('[data-cell="C28"]').getAttribute('data-role').catch(() => '')) === 'derived_value'
        && ((await grid.locator('[data-cell="C28"]').getAttribute('title').catch(() => '')) ?? '').includes('=SUM(C22:C25)'))
  check('……没有去处的格（B32）标成 unclaimed，图例里有「没有去处」',
        (await grid.locator('[data-cell="B32"]').getAttribute('data-role').catch(() => '')) === 'unclaimed'
        && (await grid.locator('[data-grid-legend]').innerText().catch(() => '')).includes('没有去处'))
  check('……合并区 B2:AG2 画成一格', (await grid.locator('[data-cell="B2"]').getAttribute('colspan').catch(() => '')) === '32'
        && await grid.locator('[data-cell="C2"]').count() === 0)

  // ---- 规则草稿完整：不提供 AI 入口
  check('规则草稿完整（offered=false）：没有 AI 入口', await page.locator('[data-ai-offer]').count() === 0)

  // ---- 建议卡片与问题
  const cards = page.locator('[data-suggestion-card]')
  const reasons = await page.locator('[data-suggestion-card] [data-card-reason]').allInnerTexts()
  check('建议卡片 10 张，每张都有理由', await cards.count() === 10 && reasons.length === 10 && reasons.every((r) => r.trim().length > 0), String(await cards.count()))
  const qPlace = page.locator('[data-question="q_placeholder:·"]')
  const radios = page.locator('[data-question] input[type="radio"]')
  check('问题是单选，没有默认选中', await radios.count() === 6 && await page.locator('[data-question] input[type="radio"]:checked').count() === 0
        && (await qPlace.locator('input[type="radio"]').first().getAttribute('type')) === 'radio')
  check('……带起草建议（default）的问题也不预选，只写「建议：…」',
        (await qPlace.locator('[data-question-suggested]').innerText().catch(() => '')).includes('建议：是，存为空值')
        && (await page.locator('[data-question="q_relation:F1"] [data-question-suggested]').innerText().catch(() => '')).includes('建议：登记')
        && await page.locator('[data-question="q_relation:F2"] [data-question-suggested]').count() === 0)
  replies.answers.push((route, { body }) => { current = { ...current, answers: Object.fromEntries(Object.entries(body.answers).map(([k, v]) => [k, { reason: null, ...v }])) }; return json(current)(route) })
  await qPlace.locator('[data-option="null"] input').check()
  await until(async () => count('^POST /datasources/imports/[^/]+/answers$') === 1, 4000)
  const a1 = last(/answers$/)
  check('选了以后发 POST …/answers，body 的 answers 是 {问题: {value}}',
        a1?.body?.answers?.['q_placeholder:·']?.value === 'null' && !('reason' in (a1?.body?.answers?.['q_placeholder:·'] ?? {})), JSON.stringify(a1?.body ?? {}))
  await page.waitForTimeout(200)
  check('……服务端回的 answers 回显为选中', await qPlace.locator('[data-option="null"] input').isChecked())
  const qF2 = page.locator('[data-question="q_relation:F2"]')
  await qF2.locator('[data-option="dismiss"] input').check()
  await page.waitForTimeout(250)
  const reasonBox = qF2.locator('[data-question-reason] textarea')
  const submitReason = qF2.locator('[data-question-reason] button')
  check('选需要理由的「不登记」：出现理由框，没填理由不发请求',
        await reasonBox.count() === 1 && await submitReason.isDisabled() && count('^POST /datasources/imports/[^/]+/answers$') === 1)
  replies.answers.push((route, { body }) => { current = { ...current, answers: Object.fromEntries(Object.entries(body.answers).map(([k, v]) => [k, { reason: null, ...v }])) }; return json(current)(route) })
  await reasonBox.fill('两段口径本来就不同，检查脚本')
  await submitReason.click()
  await until(async () => count('^POST /datasources/imports/[^/]+/answers$') === 2, 4000)
  const a2 = last(/answers$/)
  check('……写了理由再提交：带理由发出，之前的回答一并带上',
        a2?.body?.answers?.['q_relation:F2']?.value === 'dismiss' && a2?.body?.answers?.['q_relation:F2']?.reason === '两段口径本来就不同，检查脚本'
        && a2?.body?.answers?.['q_placeholder:·']?.value === 'null', JSON.stringify(a2?.body ?? {}))
  await shot(page, 'recipe-drafting')

  // ---- 配方面板
  await page.locator('[data-recipe-drawer-toggle]').click()
  const panel = page.locator('[data-recipe-panel]')
  await panel.waitFor({ timeout: 4000 }).catch(() => {})
  const panelText = await panel.innerText().catch(() => '')
  check('配方面板整段文本里没有「正则」「regex」「pattern」', panelText.length > 100 && !/正则|regex|pattern/i.test(panelText))
  const unitOptions = await panel.locator('[data-field="/tables/0/units/全日客流"] option').evaluateAll((os) => os.map((o) => o.value))
  check('……单位是下拉框，选项恰好是单位词表（外加一个「无单位」）',
        JSON.stringify(unitOptions.filter((v) => v)) === JSON.stringify(RECIPE_UNITS) && unitOptions.filter((v) => !v).length === 1, unitOptions.join(','))
  const constOptions = await panel.locator('[data-field="/sheets/0/blocks/0/segments/1/const/时段类别/pick"] option').evaluateAll((os) => os.map((o) => o.value))
  check('……常量是下拉框，选项恰好是该分段标题的候选词', JSON.stringify(constOptions) === JSON.stringify(RECIPE_CANDIDATES['日间时段客流（人次）']), constOptions.join(','))
  const puts = () => sent.filter((s) => /^PUT \/datasources\/imports\/[^/]+\/recipe$/.test(s.key))
  replies.recipe.push(echo())
  await panel.locator('[data-field="/tables/0/name"]').fill('日客流量')
  await panel.locator('[data-field="/tables/0/name"]').press('Enter')
  await until(async () => puts().length === 1, 4000)
  const p1 = puts()[0]?.body?.recipe
  check('改表名：发一次 PUT …/recipe，表名变了，引用它的分段一并改',
        puts().length === 1 && p1?.tables?.[0]?.name === '日客流量' && p1?.sheets?.[0]?.blocks?.[0]?.segments?.[0]?.table === '日客流量'
        && p1?.relations?.[0]?.table === '日客流量', JSON.stringify(p1?.tables?.[0] ?? {}))
  await page.waitForTimeout(300)
  replies.recipe.push(echo())
  const dayLabels = panel.locator('[data-field-wrap="/sheets/0/blocks/0/segments/1/labels/expect"]')
  await dayLabels.locator('[data-label-input]').fill('6-7')
  await dayLabels.locator('[data-label-add]').click()
  await until(async () => puts().length === 2, 4000)
  const p2 = puts()[1]?.body?.recipe?.sheets?.[0]?.blocks?.[0]?.segments?.[1]?.labels?.expect ?? []
  check('增一个标签：发 PUT，标签集合多了「6-7」', puts().length === 2 && p2.includes('6-7') && p2.length === 12, p2.join(','))
  await page.waitForTimeout(300)
  replies.recipe.push(echo())
  await dayLabels.getByRole('button', { name: '移除标签「7-8」' }).click()
  await until(async () => puts().length === 3, 4000)
  const p3 = puts()[2]?.body?.recipe?.sheets?.[0]?.blocks?.[0]?.segments?.[1]?.labels?.expect ?? []
  check('删一个标签：发 PUT，标签集合少了「7-8」', puts().length === 3 && !p3.includes('7-8') && p3.includes('6-7'), p3.join(','))
  await page.waitForTimeout(300)
  replies.recipe.push(echo())
  const titleBox = panel.locator('[data-field="/sheets/0/blocks/0/segments/2/locate/title"]')
  await titleBox.fill('夜间客流（人次）')
  await titleBox.blur()
  await until(async () => puts().length === 4, 4000)
  check('改分段标题：发 PUT，标题变了', puts()[3]?.body?.recipe?.sheets?.[0]?.blocks?.[0]?.segments?.[2]?.locate?.title === '夜间客流（人次）')
  await page.waitForTimeout(300)
  replies.recipe.push(echo())
  await panel.locator('[data-field="/sheets/1/blocks/0/rows/blank_rows"] input[value="skip"]').check()
  await until(async () => puts().length === 5, 4000)
  check('切换「数据中间的空行」：发 PUT，blank_rows 变成跳过', puts()[4]?.body?.recipe?.sheets?.[1]?.blocks?.[0]?.rows?.blank_rows === 'skip')
  await page.waitForTimeout(300)
  // 有问题的配方：一条对得上字段（单位），一条对不上（挂到面板顶部）
  replies.recipe.push(echo({ recipe_problems: [
    { path: '/tables/0/units/分区甲', code: 'unit_label_conflict', message: '表「日客流量」：列「分区甲」的单位与标签「分区甲（人次）」不一致' },
    { path: '/sheets/0/blocks/0/label_offset', code: 'schema', message: '第 1 个工作表的交叉表：标签列偏移只能是紧挨日期的左侧一列' },
  ] }))
  await panel.locator('[data-field="/tables/0/units/分区甲"]').selectOption('人')
  await until(async () => puts().length === 6, 4000)
  await page.waitForTimeout(300)
  const trialBtn = wizard().locator('[data-trial-run]')
  check('配方有问题时「试运行」禁用', await trialBtn.isDisabled())
  check('……带 path 的问题显示在对应字段旁', (await panel.locator('[data-field-wrap="/tables/0/units/分区甲"] [data-recipe-problem]').innerText().catch(() => '')).includes('不一致'))
  check('……对不到字段的显示在面板顶部', (await panel.locator('[data-recipe-problems-top]').innerText().catch(() => '')).includes('标签列偏移')
        && await panel.locator('[data-recipe-problems-top] [data-recipe-problem]').count() === 1)
  await panel.locator('[data-recipe-json-toggle]').click()
  const jsonText = await panel.locator('[data-recipe-json]').innerText().catch(() => '')
  check('「查看配方 JSON」可见，补全了默认值', jsonText.includes('"recipe_format"') && jsonText.includes('"fallback"') && jsonText.includes('"blank_rows"'))
  replies.recipe.push(echo({ recipe_problems: [] }))
  await panel.locator('[data-recipe-paste-toggle]').click()
  await panel.locator('[data-recipe-paste] textarea').fill(JSON.stringify(FLOW_RECIPE))
  await panel.locator('[data-recipe-paste] button', { hasText: '保存配方' }).click()
  await until(async () => puts().length === 7, 4000)
  check('「粘贴配方」发 PUT，body 就是粘贴的配方', JSON.stringify(puts()[6]?.body?.recipe) === JSON.stringify(FLOW_RECIPE))
  await page.waitForTimeout(300)
  check('……问题清掉以后「试运行」可用', await trialBtn.isEnabled())
  // 连续修改：上一次 PUT 还没回来就接着改。上一次回来时，排在后面的修改不能被服务端那份盖掉，
  // 之后的修改也要以含它的配方为底（服务端每次回复都慢 1.2 秒）
  const slowEcho = (route, ctx) => new Promise((r) => setTimeout(r, 1200)).then(() => echo()(route, ctx))
  replies.recipe.push(slowEcho, slowEcho, slowEcho)
  const unitSel = (t, c) => panel.locator(`[data-field="/tables/${t}/units/${c}"]`)
  const putsBefore = puts().length
  await unitSel(1, '客流').selectOption('人')
  await unitSel(2, '客流').selectOption('人')
  await until(async () => puts().length === putsBefore + 2, 6000)
  await page.waitForTimeout(150)
  const midUi = await unitSel(2, '客流').inputValue().catch(() => '')
  await unitSel(0, '全日客流').selectOption('元')
  await until(async () => puts().length === putsBefore + 3, 6000)
  await until(async () => !(await panel.locator('[data-recipe-saving]').count()), 6000)
  await page.waitForTimeout(200)
  const lastUnits = (puts().at(-1)?.body?.recipe?.tables ?? []).map((t) => t?.units ?? {})
  const finalUi = [await unitSel(0, '全日客流').inputValue(), await unitSel(1, '客流').inputValue(), await unitSel(2, '客流').inputValue()]
  check('连续改三处（回复慢）：上一次 PUT 回来时，排着的那处修改在界面上不退回', midUi === '人', midUi)
  check('……最后一次 PUT 三处修改都在，界面停在三处都改过的样子',
        puts().length === putsBefore + 3 && lastUnits[0]?.['全日客流'] === '元' && lastUnits[1]?.['客流'] === '人' && lastUnits[2]?.['客流'] === '人'
        && JSON.stringify(finalUi) === JSON.stringify(['元', '人', '人']), `${puts().length - putsBefore} 次：${JSON.stringify(lastUnits)} 界面 ${finalUi.join(',')}`)
  await shot(page, 'recipe-panel')

  // ---- 试运行：先要录入统计期，再出回执
  replies.trial.push((route) => { current = { ...current, status: 'trialed', trial: flowTrial('needs_input') }; return json(current)(route) })
  await trialBtn.click()
  const periodForm = page.locator('[data-period-form]')
  await periodForm.waitFor({ timeout: 4000 }).catch(() => {})
  check('needs_input：出现两个日期框，文件名里的区间作建议', await periodForm.locator('input[type="date"]').count() === 2
        && (await periodForm.innerText()).includes('2026-08-01'))
  check('……needs_input 没有写库：「表与行数」只列表和列，写「未写入」，不显示行数',
        await page.locator('[data-trial-receipt] [data-receipt-tables="unwritten"]').count() === 1
        && await page.locator('[data-trial-receipt] [data-receipt-table][data-rows]').count() === 0
        && !(await page.locator('[data-trial-receipt] [data-receipt-tables]').innerText().catch(() => '')).includes(' 行'))
  replies.trial.push((route) => { current = { ...current, status: 'trialed', trial: flowTrial('needs_decision') }; return json(current)(route) })
  await periodForm.locator('[data-period-start]').fill('2026-08-01')
  await periodForm.locator('[data-period-end]').fill('2026-08-31')
  await periodForm.getByRole('button', { name: '按此统计期试运行' }).click()
  const receipt = page.locator('[data-trial-receipt]')
  await until(async () => (await receipt.locator('[data-check]').count()) > 0, 4000)
  const t2 = sent.filter((s) => /\/trial$/.test(s.key))[1]
  check('……提交 trial 时 body 带 context_inputs', t2?.body?.context_inputs?.['统计期']?.start === '2026-08-01'
        && t2?.body?.context_inputs?.['统计期']?.end === '2026-08-31', JSON.stringify(t2?.body ?? {}))
  check('回执：格子账含 771', (await receipt.locator('[data-ledger]').innerText().catch(() => '')).includes('771'))
  const rows = async (n) => (await receipt.locator(`[data-receipt-table="${n}"]`).getAttribute('data-rows').catch(() => ''))
  check('……日客流 31 行、时段客流 527 行、时段客流_表内合计 93 行',
        await rows('日客流') === '31' && await rows('时段客流') === '527' && await rows('时段客流_表内合计') === '93'
        && (await receipt.locator('[data-receipt-table="时段客流"]').innerText()).includes('527 行'))
  const chip = (st) => receipt.locator(`[data-check][data-status="${st}"] [data-check-status]`).first()
  const look = async (st) => ({ text: await chip(st).innerText().catch(() => ''), color: await chip(st).evaluate((el) => getComputedStyle(el).color).catch(() => '') })
  const [okLook, badLook, unvLook] = [await look('passed'), await look('mismatch'), await look('unverifiable')]
  check('……通过 / 不一致 / 无法核对三种状态各有不同的文字和样式',
        okLook.text.includes('通过') && badLook.text.includes('不一致') && unvLook.text.includes('无法核对')
        && new Set([okLook.color, badLook.color, unvLook.color]).size === 3, JSON.stringify([okLook, badLook, unvLook]))
  const r2 = receipt.locator('[data-check="R2"]')
  check('……口径不同的 R2（说明）不写「不一致 31」：只有细节「31 天中 0 天相等」',
        await r2.count() === 1 && await r2.locator('[data-check-counts]').count() === 0
        && !(await r2.innerText()).includes('不一致') && (await r2.innerText()).includes('31 天中 0 天相等'))
  check('……区域外文字的坐标是「客流汇总!B3」（cell 本来就带工作表名，不再拼一次）',
        await receipt.locator('[data-outside-text="客流汇总!B3"] [data-cell-ref="客流汇总!B3"]').count() === 1)
  await shot(page, 'recipe-receipt')

  // ---- 确认清单
  await wizard().locator('[data-to-confirm]').click()
  const list = page.locator('[data-confirm-list]')
  await list.waitFor({ timeout: 4000 }).catch(() => {})
  const commitBtn = list.locator('[data-commit]')
  check('确认清单：没有「全选」', await list.getByRole('button', { name: /全选/ }).count() === 0 && !(await list.innerText()).includes('全选'))
  check('……逐列的单位确认项（unit:日客流.全日客流 等五条）',
        await list.locator('[data-confirm-item^="unit:"]').count() === 5 && await list.locator('[data-confirm-item="unit:日客流.全日客流"]').count() === 1)
  check('……署名旁写「署名（未认证）」', (await list.locator('[data-sign-note]').innerText().catch(() => '')).includes('署名（未认证）'))
  check('……不必勾的确认项标「可选」', await list.locator(`[data-confirm-item="${OPTIONAL_CONFIRM.id}"] [data-confirm-optional]`).count() === 1
        && await list.locator('[data-confirm-optional]').count() === 1)
  check('……一项没勾时「确认并启用」禁用', await commitBtn.isDisabled() && (await commitBtn.innerText()).includes('确认并启用'))
  await tickRequired(list)
  check('……全勾了、可接受的核对还没写理由：仍禁用', await commitBtn.isDisabled())
  check('……可接受的核对各一个「接受理由（必填）」', await list.locator('[data-acceptance]').count() === 2
        && (await list.locator('[data-acceptance="R1"]').innerText()).includes('接受理由（必填）'))
  await list.locator('[data-acceptance="R1"] textarea').fill('8 月 3 日分区数据补录，检查脚本')
  check('……只写了一条理由：仍禁用', await commitBtn.isDisabled())
  await list.locator('[data-acceptance="K1"] textarea').fill('占位符时段本来无数据，检查脚本')
  check('……全部勾上、理由都写了：可用', await commitBtn.isEnabled())
  replies.commit.push(coded(409, 'base_changed', '在你试运行之后，当前版本已被更新，请重新试运行'))
  replies.get.push((route) => { current = { ...current, status: 'drafting' }; return json(current)(route) })
  await commitBtn.click()
  await until(async () => count('/commit$') === 1, 4000)
  const c1 = last(/commit$/)
  check('提交的 body：confirmations 恰好是勾过的项（没勾的可选项不在里面）、acceptances 带理由、trial_id 对',
        JSON.stringify([...(c1?.body?.confirmations ?? [])].sort()) === JSON.stringify(FLOW_CONFIRMS.filter((x) => x.required).map((x) => x.id).sort())
        && !(c1?.body?.confirmations ?? []).includes(OPTIONAL_CONFIRM.id)
        && c1?.body?.acceptances?.length === 2 && c1.body.acceptances.every((a) => a.reason.includes('检查脚本'))
        && c1?.body?.trial_id === current.trial.trial_id, JSON.stringify(c1?.body ?? {}).slice(0, 300))
  await until(async () => await page.locator('[data-import-wizard][data-view="draft"]').count() > 0, 4000)
  await page.waitForTimeout(300)
  check('提交回 409 base_changed：出现「请重新试运行」，回到配方步骤',
        (await page.locator('[data-wizard-notice]').innerText().catch(() => '')).includes('请重新试运行')
        && await page.locator('[data-import-wizard][data-view="draft"]').count() === 1 && await page.locator('[data-trial-run]').count() === 1)
  // 再试运行一次：这回提交时试运行库已不在（trial_required）
  replies.trial.push((route) => { current = { ...current, status: 'trialed', trial: flowTrial('passed') }; return json(current)(route) })
  await page.locator('[data-trial-run]').click()
  await until(async () => await page.locator('[data-to-confirm]').count() > 0, 4000)
  await page.locator('[data-to-confirm]').click()
  await tickRequired(page.locator('[data-confirm-list]'))
  replies.commit.push(coded(409, 'trial_required', '试运行之后修改过配方，或试运行结果已失效：请重新试运行'))
  replies.get.push((route) => { current = { ...current, status: 'drafting' }; return json(current)(route) })
  await page.locator('[data-confirm-list] [data-commit]').click()
  await until(async () => await page.locator('[data-import-wizard][data-view="draft"]').count() > 0, 4000)
  check('提交回 409 trial_required：同样出现「请重新试运行」并回到配方步骤',
        (await page.locator('[data-wizard-notice]').innerText().catch(() => '')).includes('请重新试运行')
        && await page.locator('[data-import-wizard][data-view="draft"]').count() === 1)
  // 第三次：与第 3 次导入相同，未新建版本
  replies.trial.push((route) => { current = { ...current, status: 'trialed', trial: flowTrial('passed', { same_as_import: { id: 'imp-3', seq: 3 } }) }; return json(current)(route) })
  await page.locator('[data-trial-run]').click()
  await until(async () => await page.locator('[data-to-confirm]').count() > 0, 4000)
  await page.locator('[data-to-confirm]').click()
  await tickRequired(page.locator('[data-confirm-list]'))
  replies.commit.push(json({ source: recipeSource('src-new-flow', 'zz_flow_recipe'), import_id: 'imp-3', snapshot_id: '9'.repeat(64),
    build_id: 'b'.repeat(64), recipe_id: 'rcp-9', build_reused: true, unchanged: true }, 201))
  await page.locator('[data-confirm-list] [data-commit]').click()
  const doneView = page.locator('[data-import-done]')
  await doneView.waitFor({ timeout: 4000 }).catch(() => {})
  check('提交回 unchanged: true：显示「与第 3 次导入相同」', (await doneView.innerText().catch(() => '')).includes('与第 3 次导入相同'))
  check('……启用后卡片列表里有了这个源', await page.locator('[data-source="zz_flow_recipe"]').count() === 1)
  await wizard().getByRole('button', { name: '完成' }).click()
  await page.waitForTimeout(300)

  // ---- AI 起草：从卡片「有未完成的导入」继续（同名的简单导入切换为按配方导入）
  const aiStaging = () => flowStaging({
    id: 'stg-ai', kind: 'switch', source: { id: 'check-manage-ai', name: 'zz_ai_offer', exists: true, import_mode: 'simple' },
    draft: { recipe: null, complete: false, origin: 'rules', cards: [], questions: [], failures: ['第 28 行像合计行，但标签无法确定区间'] },
    recipe: null, cards: FLOW_CARDS.slice(0, 3), questions: [FLOW_QUESTIONS[0]], answers: { 'q_placeholder:·': { value: 'reject', reason: null } },
    ai: { offered: true, available: true, reason: '', model: '检查脚本模型', provider: '检查脚本接入' }, draft_partial: true,
  })
  replies.get.push(json(aiStaging()))
  await page.locator('[data-source="zz_ai_offer"] [data-open-staging]').click()
  const offer = page.locator('[data-ai-offer]')
  await offer.waitFor({ timeout: 4000 }).catch(() => {})
  check('继续未完成的导入：GET imports/{id} 进入起草', count('^GET /datasources/imports/stg-ai$') === 1 && await page.locator('[data-sheet-grid]').count() === 1)
  check('……刷新后已选的回答处于选中状态', await page.locator('[data-question="q_placeholder:·"] [data-option="reject"] input').isChecked())
  check('……部分干跑：可见文字写全「仅检查了前 500 行，完整检查在试运行时进行」',
        (await page.locator('[data-draft-partial]').innerText().catch(() => '')).includes('仅检查了前 500 行，完整检查在试运行时进行'))
  check('offered 且可用：有 AI 入口', await offer.count() === 1 && (await offer.innerText()).includes('让 AI 起草（会把表格结构发给模型）')
        && await offer.getByRole('button').isEnabled())
  replies.preview.push(json(PREVIEW))
  await offer.getByRole('button').click()
  const consent = page.locator('[data-ai-consent]')
  await consent.waitFor({ timeout: 4000 }).catch(() => {})
  const consentText = await consent.innerText().catch(() => '')
  check('点了先发 GET …/draft-ai/preview，再出同意框', count('/draft-ai/preview$') === 1 && await consent.count() === 1)
  check('……同意框写模型名和「不会发送数字单元格的值」', consentText.includes('检查脚本模型') && consentText.includes('不会发送数字单元格的值'), consentText.slice(0, 120))
  check('……[data-ai-preview] 是预览接口返回的原文', (await consent.locator('[data-ai-preview]').innerText().catch(() => '')).trim() === PREVIEW.text.trim())
  await shot(page, 'recipe-ai-consent')
  await dialog(page).getByRole('button', { name: '取消', exact: true }).click()
  await page.waitForTimeout(250)
  check('……点「取消」不发 POST', count('/draft-ai$') === 0 && await consent.count() === 0)
  replies.preview.push(json(PREVIEW))
  replies.draftAi.push((route) => json({ ...aiStaging(), recipe: FLOW_RECIPE, recipe_origin: 'ai',
    ai_draft: { draft: { recipe: FLOW_RECIPE, complete: true, origin: 'ai' }, usage: [], attempts: 2, total_tokens: 4300, cost_usd: 0.012, error: null } })(route))
  await offer.getByRole('button').click()
  await consent.waitFor({ timeout: 4000 }).catch(() => {})
  await dialog(page).getByRole('button', { name: '同意并发送' }).click()
  await until(async () => count('/draft-ai$') === 1, 4000)
  const ai1 = last(/draft-ai$/)
  check('「同意并发送」发 POST …/draft-ai，consent: true，preview_sha256 等于预览返回的',
        ai1?.body?.consent === true && ai1?.body?.preview_sha256 === PREVIEW.sha256, JSON.stringify(ai1?.body ?? {}))
  const usage = page.locator('[data-ai-usage]')
  await usage.waitFor({ timeout: 4000 }).catch(() => {})
  check('……成功后显示用了多少 token', (await usage.innerText().catch(() => '')).includes('4,300 token'))
  replies.preview.push(json(PREVIEW))
  // 没有工作配方（recipe: null）时服务端的下一步是「粘贴一份完整的配方」：界面照写原话，不再另补「请在配方面板中填写」
  replies.draftAi.push(coded(502, 'ai_failed', 'AI 未能起草出合法的配方。可以重试，或在配方面板中粘贴一份完整的配方'))
  await offer.getByRole('button').click()
  await consent.waitFor({ timeout: 4000 }).catch(() => {})
  await dialog(page).getByRole('button', { name: '同意并发送' }).click()
  await page.locator('[data-ai-error]').waitFor({ timeout: 4000 }).catch(() => {})
  const failedText = (await page.locator('[data-ai-error]').innerText().catch(() => '')).trim()
  check('ai_failed：显示服务端原话，下一步与可做的事一致（没有工作配方时不叫人去「填写」面板）',
        failedText === 'AI 未能起草出合法的配方。可以重试，或在配方面板中粘贴一份完整的配方' && !failedText.includes('填写'), failedText)
  replies.preview.push(json(PREVIEW))
  replies.draftAi.push(coded(409, 'ai_preview_stale', '检查脚本：表格或模型设置在预览之后变了'))
  await offer.getByRole('button').click()
  await consent.waitFor({ timeout: 4000 }).catch(() => {})
  await dialog(page).getByRole('button', { name: '同意并发送' }).click()
  await until(async () => (await page.locator('[data-ai-error]').innerText().catch(() => '')).includes('预览之后变了'), 4000)
  const staleText = await page.locator('[data-ai-error]').innerText().catch(() => '')
  check('ai_preview_stale：显示服务端原话，并提示重新查看将要发送的内容', staleText.includes('预览之后变了') && staleText.includes('重新查看'), staleText)
  await wizard().getByRole('button', { name: '稍后继续' }).click()
  await page.waitForTimeout(300)

  // ---- 模型接入不可用：按钮禁用并写出原因
  replies.get.push(json({ ...aiStaging(), id: 'stg-ai-off', source: { id: 'check-manage-ai-off', name: 'zz_ai_unavailable', exists: true, import_mode: 'simple' },
    ai: { offered: true, available: false, reason: '未配置模型接入，无法使用 AI 起草（设置 → 模型接入）', model: '', provider: '' } }))
  await page.locator('[data-source="zz_ai_unavailable"] [data-open-staging]').click()
  await offer.waitFor({ timeout: 4000 }).catch(() => {})
  check('offered 但不可用：按钮禁用并显示原因', await offer.getByRole('button').isDisabled()
        && (await offer.innerText()).includes('未配置模型接入'))
  await wizard().getByRole('button', { name: '稍后继续' }).click()
  await page.waitForTimeout(200)
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据 · 表格：上传新一期', async () => {
  const src = recipeSource('check-manage-reup', 'zz_recipe_reup')
  // 试运行之后又改过配方的未完成导入（status 回到 drafting、trial 还在）
  const staleSrc = recipeSource('check-manage-stale', 'zz_recipe_stale', {
    open_staging: { id: 'stg-stale', kind: 'reupload', status: 'drafting', created_at: ago(60_000) } })
  const replies = { reupload: [], trial: [], commit: [], recipe: [], redraft: [], discard: [] }
  const take = (k) => (route, ctx) => {
    const r = replies[k].shift()
    return r ? r(route, ctx) : json({ detail: `检查脚本没有准备这次的回答（${k}）` }, 500)(route)
  }
  const reupStaging = (trial, over = {}) => flowStaging({
    id: 'stg-reup', kind: 'reupload', status: trial.status === 'rejected' ? 'rejected' : 'trialed',
    source: { id: src.id, name: src.name, exists: true, import_mode: 'recipe' },
    file: { name: '月报导出_2026-09-01_2026-09-30.xlsx', size: 12000, sha256_prefix: 'b2c3d4e5' },
    draft: null, cards: [], questions: [], answers: {}, ai: { offered: false, available: true, reason: '', model: '', provider: '' },
    recipe: FLOW_RECIPE, trial, ...over,
  })
  const diffTrial = flowTrial('passed', {
    diff: [
      { kind: 'period', label: '统计期 2026-07-01 至 2026-07-31 → 2026-09-01 至 2026-09-30', requires_confirm: false },
      { kind: 'rows', label: '时段客流 527 行 → 510 行', requires_confirm: false },
      { kind: 'label_writing', label: '分段「日间」：标签「8-9」现在写作「8－9」', requires_confirm: true, confirm_id: 'diff:label_writing:日间' },
    ],
    confirm_items: [
      { id: 'diff:label_writing:日间', label: '分段「日间」的标签写法变了：「8-9」→「8－9」', required: true, source: 'diff' },
      { id: 'outside_digits:客流汇总!B33', label: '区域外有含数字的文字「注：9月15日闭馆」', required: true, source: 'outside' },
      OPTIONAL_CONFIRM,
    ],
  })
  const rejectedTrial = flowTrial('rejected', {
    checks: [], confirm_items: [],
    problems: [{ code: 'label_missing', category: 'structure', message: '分段「夜间」：期望的标签「23-24」在表格中没有找到', cells: ['客流汇总!B27'] }],
  })
  const { page, sent, natives, errors, close } = await open('/data/tables', {
    recordForms: true,
    handlers: [
      [/^GET \/datasources$/, (route) => json([...sources, src, staleSrc])(route)],
      [/^GET \/datasources\/check-manage-(?:reup|stale)\/schema$/, json({ tables: ['日客流'], summary: '', synced_at: ago(0) })],
      [/^GET \/datasources\/imports\/stg-stale$/, (route) => json(reupStaging(flowTrial('passed'), {
        id: 'stg-stale', status: 'drafting', source: { id: staleSrc.id, name: staleSrc.name, exists: true, import_mode: 'recipe' } }))(route)],
      [/^POST \/datasources\/check-manage-reup\/reupload$/, take('reupload')],
      [/^POST \/datasources\/imports\/[^/]+\/trial$/, take('trial')],
      [/^POST \/datasources\/imports\/[^/]+\/commit$/, take('commit')],
      [/^PUT \/datasources\/imports\/[^/]+\/recipe$/, take('recipe')],
      [/^POST \/datasources\/check-manage-reup\/redraft$/, take('redraft')],
      [/^DELETE \/datasources\/imports\/[^/]+$/, take('discard')],
    ],
  })
  const card = page.locator('[data-source="zz_recipe_reup"]')
  const start = async (name) => {
    await card.getByRole('button', { name: '上传新一期' }).click()
    const dlg = dialog(page)
    await dlg.locator('input[type="file"]').setInputFiles({ name, mimeType: 'application/octet-stream', buffer: Buffer.from('PK\x03\x04') })
    await dlg.locator('[data-wizard-start]').click()
  }

  // ---- 通过：差异卡需确认的在前，并且在确认清单里；「启用」要全勾
  replies.reupload.push(json(reupStaging(diffTrial), 201))
  await start('月报导出_2026-09-01_2026-09-30.xlsx')
  const diffCard = page.locator('[data-reupload-diff]')
  await diffCard.waitFor({ timeout: 5000 }).catch(() => {})
  const form = (await importForms(page)).at(-1) ?? {}
  check('点「上传新一期」发 POST /datasources/{id}/reupload，带上文件',
        sent.filter((s) => s.key === 'POST /datasources/check-manage-reup/reupload').length === 1
        && form.file === '<file 月报导出_2026-09-01_2026-09-30.xlsx>', JSON.stringify(form))
  const diffs = await diffCard.locator('[data-diff]').evaluateAll((els) => els.map((e) => e.hasAttribute('data-requires-confirm')))
  check('结果通过：显示差异卡，需确认的项排在前面', await diffCard.count() === 1 && diffs.length === 3 && diffs[0] === true && !diffs[1] && !diffs[2],
        JSON.stringify(diffs))
  check('……上传新一期没有 AI 入口', await page.locator('[data-ai-offer]').count() === 0)
  await shot(page, 'recipe-reupload-diff')
  await page.locator('[data-to-confirm]').click()
  const list = page.locator('[data-confirm-list]')
  await list.waitFor({ timeout: 4000 }).catch(() => {})
  const enable = list.locator('[data-commit]')
  check('……需确认的差异出现在确认清单里，按钮写「启用」',
        await list.locator('[data-confirm-item="diff:label_writing:日间"]').count() === 1 && (await enable.innerText()).trim() === '启用')
  await list.locator('[data-confirm-item="diff:label_writing:日间"] input').check()
  check('……没有全勾时「启用」禁用', await enable.isDisabled())
  await tickRequired(list)
  check('……全勾以后可用', await enable.isEnabled())
  replies.commit.push(json({ source: src, import_id: 'imp-9', snapshot_id: '6'.repeat(64), build_id: 'c'.repeat(64), recipe_id: 'rcp-1', build_reused: false, unchanged: false }, 201))
  await enable.click()
  await page.locator('[data-import-done]').waitFor({ timeout: 4000 }).catch(() => {})
  const c1 = sent.filter((s) => /\/commit$/.test(s.key)).at(-1)
  check('……启用：confirmations 是勾过的两项（没勾的可选项不在里面）', JSON.stringify([...(c1?.body?.confirmations ?? [])].sort())
        === JSON.stringify(['diff:label_writing:日间', 'outside_digits:客流汇总!B33'].sort()) && await page.locator('[data-import-done="committed"]').count() === 1)
  await dialog(page).getByRole('button', { name: '完成' }).click()
  await page.waitForTimeout(300)

  // ---- 拒收：问题带坐标，「修改配方」回到配方面板，没有「启用」，也没有 AI 入口
  replies.reupload.push(json(reupStaging(rejectedTrial), 201))
  await start('月报导出_2026-10-01_2026-10-31.xlsx')
  const problem = page.locator('[data-problem="label_missing"]')
  await problem.waitFor({ timeout: 5000 }).catch(() => {})
  check('结果拒收：问题列表带坐标', await problem.count() === 1 && await problem.locator('[data-cell-ref="客流汇总!B27"]').count() === 1)
  await problem.locator('[data-cell-ref="客流汇总!B27"]').click()
  await page.waitForTimeout(400)
  check('……点坐标，网格滚到那一格', await inView(page.locator('[data-sheet-grid] [data-cell="B27"]')))
  check('……没有「启用」、没有「下一步：逐条确认」', await page.locator('[data-to-confirm]').count() === 0 && await page.locator('[data-commit]').count() === 0
        && await dialog(page).getByRole('button', { name: '启用' }).count() === 0)
  check('……没有 AI 入口', await page.locator('[data-ai-offer]').count() === 0)
  check('……拒收时服务端不算差异（diff 为 null）：不显示差异卡；「表与行数」写「未写入」，不显示只写了一半的行数',
        await page.locator('[data-reupload-diff]').count() === 0
        && await page.locator('[data-receipt-tables="unwritten"]').count() === 1
        && await page.locator('[data-receipt-table][data-rows]').count() === 0)
  await page.locator('[data-back-to-recipe]').click()
  await page.waitForTimeout(300)
  check('「修改配方」回到配方面板', await page.locator('[data-recipe-panel]').count() === 1 && await page.locator('[data-ai-offer]').count() === 0)
  // 任一写请求遇到 staging_closed：说清楚并关掉向导
  replies.trial.push(coded(409, 'staging_closed', '这次导入已结束（过期或已放弃）'))
  await page.locator('[data-trial-run]').click()
  await until(async () => (await page.locator('body').innerText()).includes('这次导入已结束'), 4000)
  await page.waitForTimeout(300)
  check('写请求回 409 staging_closed：显示「这次导入已结束」并关闭向导',
        (await page.locator('body').innerText()).includes('这次导入已结束') && await page.locator('[role="dialog"]').count() === 0)

  // ---- 差异卡的种类名：7.6 补上的 outside_moved 有中文名；不认识的 kind 写通用名，不露键名
  replies.reupload.push(json(reupStaging(flowTrial('passed', { diff: [
    { kind: 'outside_moved', label: '区域外文字「注：数据为初步统计」从 B32 移到 B33', requires_confirm: false },
    { kind: 'zz_future_kind', label: '检查脚本：将来新增的一种变化', requires_confirm: false },
  ] })), 201))
  await start('月报导出_2026-11-01_2026-11-30.xlsx')
  const kindCard = page.locator('[data-reupload-diff]')
  await kindCard.waitFor({ timeout: 5000 }).catch(() => {})
  const kindText = await kindCard.innerText().catch(() => '')
  check('差异卡：outside_moved 写「区域外文字挪了位置」，不认识的 kind 写「其他变化」，不露键名',
        kindText.includes('区域外文字挪了位置') && kindText.includes('其他变化') && !/outside_moved|zz_future_kind/.test(kindText), kindText)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(300)

  // ---- 试运行之后又改过配方（status 回到 drafting、trial 还在）：不显示「下一步：逐条确认」，写明已失效
  await page.locator('[data-source="zz_recipe_stale"] [data-open-staging]').click().catch(() => {})
  await page.locator('[data-trial-stale]').waitFor({ timeout: 4000 }).catch(() => {})
  check('试运行已失效：写明「上次的试运行已失效」，没有「下一步：逐条确认」',
        await page.locator('[data-trial-stale]').count() === 1 && await page.locator('[data-to-confirm]').count() === 0)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(300)

  // ---- 与某次导入相同
  replies.reupload.push(json(reupStaging(flowTrial('passed', { same_as_import: { id: 'imp-2', seq: 2 }, diff: [] })), 201))
  await start('月报导出_2026-08-01_2026-08-31.xlsx')
  const same = page.locator('[data-same-as-import]')
  await same.waitFor({ timeout: 5000 }).catch(() => {})
  check('same_as_import：显示「与第 2 次导入相同」', (await same.innerText().catch(() => '')).includes('与第 2 次导入相同'))
  // 放弃：先问一句（说清当前版本不受影响），确认后发 DELETE 并关掉向导
  replies.discard.push((route) => route.fulfill({ status: 204, body: '' }))
  await page.locator('[data-wizard-discard]').click()
  const ask = page.locator('[role="dialog"]', { hasText: '放弃这次导入？' })
  await ask.waitFor({ timeout: 4000 }).catch(() => {})
  check('「放弃这次导入」先问一句，写明当前版本不受影响，还没发 DELETE',
        (await ask.innerText().catch(() => '')).includes('当前版本不受影响') && sent.filter((s) => s.key.startsWith('DELETE ')).length === 0)
  await ask.getByRole('button', { name: '放弃这次导入' }).click()
  await until(async () => await page.locator('[data-import-wizard]').count() === 0, 4000)
  check('……确认后发 DELETE /datasources/imports/{id}，向导关闭',
        sent.filter((s) => s.key === 'DELETE /datasources/imports/stg-reup').length === 1 && await page.locator('[role="dialog"]').count() === 0)

  // ---- 配方里的工作表没读到：执行器不给它记账。账是空的、或缺了工作表时，回执不写「全部有去处」「两遍读取一致」
  const base = flowTrial('rejected').receipt
  const noLedger = flowTrial('rejected', {
    checks: [], confirm_items: [],
    receipt: { ...base, ledger: [], tables: [], period: null, placeholders: {}, outside_text: [] },
    problems: [{ code: 'sheet_missing', category: 'structure', message: '没有找到工作表「客流汇总」（工作簿中有内容的可见工作表：「表一」「表二」）', cells: [] }],
  })
  const ledgerOf = async () => {
    const box = page.locator('[data-trial-receipt] [data-ledger]')
    await box.waitFor({ timeout: 5000 }).catch(() => {})
    return box.innerText().catch(() => '')
  }
  replies.reupload.push(json(reupStaging(noLedger), 201))
  await start('月报导出_2026-11-01_2026-11-30.xlsx')
  const noneText = await ledgerOf()
  check('配方里的工作表一张都没读到（账为空）：回执写「未读取到配方中的工作表」，不写「全部有去处」「两遍读取一致」',
        noneText.includes('未读取到配方中的工作表') && !noneText.includes('全部有去处') && !noneText.includes('两遍读取一致'), noneText)
  await page.keyboard.press('Escape')
  await until(async () => await page.locator('[data-import-wizard]').count() === 0, 4000)
  replies.reupload.push(json(reupStaging(flowTrial('rejected', {
    checks: [], confirm_items: [],
    problems: [{ code: 'sheet_missing', category: 'structure', message: '没有找到工作表「分区明细」（工作簿中有内容的可见工作表：「客流汇总」）', cells: [] }],
  }), { recipe: recipeWithList() }), 201))
  await start('月报导出_2026-12-01_2026-12-31.xlsx')
  const partText = await ledgerOf()
  check('……缺了一张工作表：写明有 1 张未读取到，同样不写「全部有去处」「两遍读取一致」',
        partText.includes('771') && partText.includes('配方中有 1 张工作表未读取到') && !partText.includes('全部有去处') && !partText.includes('两遍读取一致'), partText)
  await page.keyboard.press('Escape')
  await until(async () => await page.locator('[data-import-wizard]').count() === 0, 4000)

  // ---- 修改配方：POST redraft 进入起草（以现行配方为工作配方，没有 AI 入口）
  replies.redraft.push(json(reupStaging({ ...flowTrial('passed') }, { kind: 'redraft', status: 'drafting', trial: null }), 201))
  await card.getByRole('button', { name: '修改配方' }).click()
  await page.locator('[data-import-wizard]').waitFor({ timeout: 4000 }).catch(() => {})
  check('「修改配方」发 POST /datasources/{id}/redraft，进入起草', sent.filter((s) => s.key === 'POST /datasources/check-manage-reup/redraft').length === 1
        && await page.locator('[data-import-wizard][data-view="draft"]').count() === 1 && await page.locator('[data-ai-offer]').count() === 0)
  await dialog(page).getByRole('button', { name: '稍后继续' }).click()
  await page.waitForTimeout(200)
  replies.redraft.push(json({ detail: '当前导入的原始文件已清除', code: 'raw_missing' }, 409))
  await card.getByRole('button', { name: '修改配方' }).click()
  await page.locator('[data-wizard-failed]').waitFor({ timeout: 4000 }).catch(() => {})
  check('……409 raw_missing：提示用「上传新一期」带着文件进入修改', (await page.locator('[data-wizard-failed]').innerText().catch(() => '')).includes('上传新一期'))
  await dialog(page).getByRole('button', { name: '关闭', exact: true }).last().click()
  await page.waitForTimeout(200)
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

// ---------------------------------------------------------------------------
// 期 3（P3-SPEC 10.3）：修复按钮、框选转锚点、按期累积、配方对照与重新起草、改配方后回答保留。接口全部伪造，
// 夹具沿用上面的参考配方和假暂存区（假名、手写假数）。新增的界面文字一律不含「快照」「并集」「构建」「锚点」
// 「放行」「钉」，也不露机读码
// ---------------------------------------------------------------------------
const P3_FORBIDDEN = /快照|并集|构建|锚点|放行|钉/
const P3_CODES = /remove_label|ignore_cells|declare_placeholder|edit_members|rename_sheet|label_missing|row_unclaimed|selection_bottom_not_anchorable|period_overlap|q_mode|accumulate_restart|period_replace|replace_period|fact_claim_mismatch|union_tampered|build_conflict|store_unavailable/
/** 向导里可见的文字（不含隐藏元素） */
const wizardText = (page) => page.locator('[data-import-wizard]').innerText().catch(() => '')
const sha = (c) => c.repeat(64)
/** 修复、框选的预览（契约 EditPreview）：没写到的字段按「没有问题、没有变化」 */
const editPreview = (over = {}) => ({
  ok: true, kind: 'fix', key: '', title: '', summary: [], ops: [], anchors: [], notes: [], problems: [],
  recipe_sha256_before: sha('a'), recipe_sha256_after: sha('b'), recipe_problems: [],
  dry_run: { problems: [], partial: false, marks: FLOW_MARKS.map((m) => ({ ...m, block: m.role === 'context' || m.role === 'outside_text' ? null : '交叉表' })) },
  replay: null, breaking: {}, accumulate_change: null, compare: null, expected: null, block: null, seq: null, ...over,
})
const emptyCompare = (over = {}) => ({
  tables: [], segments: [], relations: [], sheets: [], mode: { old: 'replace', new: 'replace' },
  breaking: false, units_changed: [], accumulate: null, ...over,
})
const FX = {
  remove: {
    id: 'fx-0a1b2c3d4e5f', kind: 'remove_label', problem_code: 'label_missing', title: '在分段「日间」的期望标签中去掉「7-8」',
    cells: ['客流汇总!B10'], target: { segment: '日间', labels: ['7-8'] },
    options: [{ value: 'remove', label: '去掉标签「7-8」', detail: '今后各期如果又出现「7-8」，会再次拒收', needs_reason: false, breaking: false }],
    anchor: { kind: 'problem', index: 0, sheet: null },
  },
  ignore: {
    id: 'fx-1b2c3d4e5f6a', kind: 'ignore_cells', problem_code: 'row_unclaimed', title: '按行标签忽略「补录（人次）」这一行',
    cells: ['客流汇总!B32:C32'], target: { block: '交叉表', how: 'rows', labels: ['补录（人次）'] },
    options: [{ value: 'ignore', label: '按行标签忽略「补录（人次）」这一行', detail: '之后各期出现这一行同样忽略', needs_reason: true, breaking: false }],
    anchor: { kind: 'problem', index: 2, sheet: null },
  },
  placeholder: {
    id: 'fx-2c3d4e5f6a7b', kind: 'declare_placeholder', problem_code: 'value_not_number', title: '把「—」声明为占位符',
    cells: ['客流汇总!D12'], target: { block: '交叉表', texts: ['—'] },
    options: [{ value: 'no_data', label: '把「—」也声明为无数据（存为空值）', needs_reason: false, breaking: false },
      { value: 'not_applicable', label: '声明为不适用（存为空值）', needs_reason: false, breaking: false }],
    anchor: { kind: 'problem', index: 3, sheet: null },
  },
  members: {
    id: 'fx-3d4e5f6a7b8c', kind: 'edit_members', problem_code: null, title: '按系统发现更新关系 R1 的成员',
    cells: ['客流汇总!B5:B8'], target: { relation: 'R1', fact: 'F1', total: '全日客流', parts: ['分区甲', '分区乙', '分区丙'] },
    options: [{ value: 'update', label: '按系统发现更新为「全日客流 = 分区甲 + 分区乙 + 分区丙」', needs_reason: false, breaking: false },
      { value: 'dismiss', label: '不再登记这条关系', needs_reason: true, breaking: false }],
    anchor: { kind: 'recipe_problem', index: 0, sheet: null },
  },
  sheet: {
    id: 'fx-4e5f6a7b8c9d', kind: 'rename_sheet', problem_code: null, title: '把配方里的工作表名更新为「客流汇总（新）」',
    cells: [], target: { sheet_id: 's1', name: '客流汇总（新）' },
    options: [{ value: '客流汇总（新）', label: '更新为「客流汇总（新）」', detail: '下一期不再需要确认改名', needs_reason: false, breaking: false }],
    anchor: { kind: 'sheet_renamed', index: null, sheet: '客流汇总（新）' },
  },
  // 同一次另有一张表改名：确认项 sheet_renamed:<sid> 旁只放对应那张表的按钮（按回执 sheets.matched 对上本期表名）
  sheet2: {
    id: 'fx-5f6a7b8c9d0e', kind: 'rename_sheet', problem_code: null, title: '把配方里的工作表名更新为「附表（新）」',
    cells: [], target: { sheet_id: 's2', name: '附表（新）' },
    options: [{ value: '附表（新）', label: '更新为「附表（新）」', detail: '下一期不再需要确认改名', needs_reason: false, breaking: false }],
    anchor: { kind: 'sheet_renamed', index: null, sheet: '附表（新）' },
  },
}
const FIX_PROBLEMS = [
  { code: 'label_missing', category: 'structure', message: '分段「日间」：期望的标签「7-8」在表格中没有找到', cells: ['客流汇总!B10'], fix: 'remove_label', fix_ids: [FX.remove.id] },
  { code: 'cell_unclaimed', category: 'structure', message: '第 31 行有一格数字没有分段认领', cells: ['客流汇总!AG31'], fix: null, fix_ids: [] },
  { code: 'row_unclaimed', category: 'structure', message: '第 32 行：标签列有字、日期列有值，没有分段认领这一行', cells: ['客流汇总!B32:C32'], fix: 'ignore_cells', fix_ids: [FX.ignore.id] },
  { code: 'value_not_number', category: 'structure', message: '数据区有 1 格写着「—」，不是数字', cells: ['客流汇总!D12'], fix: 'declare_placeholder', fix_ids: [FX.placeholder.id] },
]
const fixEdit = (over = {}) => ({
  seq: 1, kind: 'fix', key: 'remove_label:日间:7-8', title: '在分段「日间」的期望标签中去掉「7-8」', at: ago(30_000), signed_by: '检查脚本',
  recipe_sha256_before: sha('a'), recipe_sha256_after: sha('b'), superseded: false, undoable: true, ...over,
})

await section('数据 · 表格：修复按钮', async () => {
  const src = recipeSource('check-manage-fix', 'zz_recipe_fix')
  const d13Src = recipeSource('check-manage-d13', 'zz_recipe_d13', {
    open_staging: { id: 'stg-d13', kind: 'reupload', status: 'drafting', created_at: ago(60_000) } })
  const renameSrc = recipeSource('check-manage-rename', 'zz_recipe_rename', {
    open_staging: { id: 'stg-rename', kind: 'reupload', status: 'trialed', created_at: ago(60_000) } })
  const replies = { reupload: [], preview: [], apply: [], undo: [], trial: [], get: [] }
  const take = (k) => (route, ctx) => {
    const r = replies[k].shift()
    return r ? r(route, ctx) : json({ detail: `检查脚本没有准备这次的回答（${k}）` }, 500)(route)
  }
  const base = (over = {}) => flowStaging({
    id: 'stg-fix', kind: 'reupload', status: 'drafting', source: { id: src.id, name: src.name, exists: true, import_mode: 'recipe' },
    file: { name: '月报导出_2026-09-01_2026-09-30.xlsx', size: 12000, sha256_prefix: 'c3d4e5f6' },
    draft: null, cards: [], questions: [], answers: {}, ai: { offered: false, available: true, reason: '', model: '', provider: '' },
    recipe: FLOW_RECIPE, recipe_sha256: sha('a'), edits: [], answers_dropped: [], ...over,
  })
  const rejected = flowTrial('rejected', { checks: [], confirm_items: [], problems: FIX_PROBLEMS })
  const allFixes = [FX.remove, FX.ignore, FX.placeholder]
  const { page, sent, natives, errors, close } = await open('/data/tables', {
    recordForms: true,
    handlers: [
      [/^GET \/datasources$/, (route) => json([...sources, src, d13Src, renameSrc])(route)],
      [/^GET \/datasources\/check-manage-[^/]+\/schema$/, json({ tables: ['日客流'], summary: '', synced_at: ago(0) })],
      [/^POST \/datasources\/check-manage-fix\/reupload$/, take('reupload')],
      [/^GET \/datasources\/imports\/stg-d13$/, (route) => json(base({
        id: 'stg-d13', source: { id: d13Src.id, name: d13Src.name, exists: true, import_mode: 'recipe' },
        recipe_problems: [{ path: '/relations/0/claims', code: 'fact_claim_mismatch', message: '关系 R1 认领的系统发现与本期不一致：本期第 5 行 = 第 6、7、8 行之和', fix_ids: [FX.members.id] }],
        fixes: [FX.members], edits: [fixEdit({ key: 'add_label:分区丙:分区丙（人次）', title: '在分段「分区」加入标签「分区丙（人次）」' })],
      }))(route)],
      [/^GET \/datasources\/imports\/stg-rename$/, (route) => json(base({
        id: 'stg-rename', status: 'trialed', source: { id: renameSrc.id, name: renameSrc.name, exists: true, import_mode: 'recipe' },
        fixes: [FX.sheet, FX.sheet2],
        trial: flowTrial('passed', {
          receipt: { ...flowTrial('passed').receipt, sheets: {
            matched: { s1: '客流汇总（新）', s2: '附表（新）' }, renamed: { 客流汇总: '客流汇总（新）', 附表: '附表（新）' }, other_visible: [], skipped_hidden: [] } },
          confirm_items: [{ id: 'sheet_renamed:s1', label: '工作表「客流汇总」现在叫「客流汇总（新）」', required: true, source: 'sheet' },
            { id: 'sheet_renamed:s2', label: '工作表「附表」现在叫「附表（新）」', required: true, source: 'sheet' }],
        }),
      }))(route)],
      [/^GET \/datasources\/imports\/[^/]+$/, take('get')],
      [/^POST \/datasources\/imports\/[^/]+\/edits\/preview$/, take('preview')],
      [/^POST \/datasources\/imports\/[^/]+\/edits\/apply$/, take('apply')],
      [/^POST \/datasources\/imports\/[^/]+\/edits\/undo$/, take('undo')],
      [/^POST \/datasources\/imports\/[^/]+\/trial$/, take('trial')],
    ],
  })
  const count = (re) => sent.filter((s) => re.test(s.key)).length
  const last = (re) => [...sent].reverse().find((s) => re.test(s.key))
  const panel = page.locator('[data-fix-panel]')

  // ---- 拒收的上传新一期：问题旁的修复按钮只按 fix_ids 放
  replies.reupload.push(json(base({ status: 'rejected', trial: rejected, draft_problems: FIX_PROBLEMS, fixes: allFixes }), 201))
  await page.locator('[data-source="zz_recipe_fix"]').getByRole('button', { name: '上传新一期' }).click()
  await dialog(page).locator('input[type="file"]').setInputFiles({ name: '月报导出_2026-09-01_2026-09-30.xlsx', mimeType: 'application/octet-stream', buffer: Buffer.from('PK\x03\x04') })
  await dialog(page).locator('[data-wizard-start]').click()
  const fixBtn = page.locator('[data-trial-receipt] [data-problem="label_missing"] [data-fix="remove_label"]')
  await fixBtn.waitFor({ timeout: 5000 }).catch(() => {})
  check('拒收的问题旁有「去掉标签」按钮（[data-fix="remove_label"]）', await fixBtn.count() === 1 && (await fixBtn.innerText()).includes('去掉标签'))
  check('……没有 fix_ids 的问题旁没有修复按钮', await page.locator('[data-problem="cell_unclaimed"] [data-fix]').count() === 0)
  await fixBtn.click()
  await panel.waitFor({ timeout: 4000 }).catch(() => {})
  const masks = await page.evaluate(() => [...document.querySelectorAll('.fixed.inset-0')].length)
  check('点开后出修复面板，在向导右栏内', await page.locator('[data-wizard-side] [data-fix-panel]').count() === 1)
  check('……网格仍然可见、没有被遮罩挡住（只有向导自己一层，没有另起全屏遮罩）',
        await page.locator('[data-sheet-grid]').isVisible() && await inView(page.locator('[data-sheet-grid] [data-cell="B10"]'))
        && masks === 1 && await page.locator('[role="dialog"]').count() === 1, `遮罩 ${masks} 层`)
  const title = await panel.locator('[data-panel-title]').innerText().catch(() => '')
  check('……标题含「日间」「7-8」，选项没有默认选中', title.includes('日间') && title.includes('7-8')
        && await panel.locator('[data-fix-option] input:checked').count() === 0 && await panel.locator('[data-fix-option]').count() === 1, title)
  check('……相关的格在网格上描出来', await page.locator('[data-sheet-grid] [data-fix-cell="B10"]').count() === 1)
  const removeCompare = emptyCompare({ segments: [{ id: '日间', status: 'changed', title: { old: '日间时段客流（人次）', new: '日间时段客流（人次）' },
    labels: { added: [], removed: ['7-8'] }, ignore: { added: [], removed: [] } }], accumulate: { change: 'compatible', added: [], retired_new: [], retired_existing: [] } })
  replies.preview.push((route, { body }) => json(editPreview({ key: 'remove_label:日间:7-8', title: FX.remove.title,
    summary: ['分段「日间」的期望标签去掉「7-8」，剩下 10 个'],
    ops: [{ op: 'replace', path: '/sheets/0/blocks/0/segments/1/labels/expect', value: ['8-9'] }],
    compare: removeCompare, seq: body.seq }))(route))
  await panel.locator('[data-fix-option="remove"] input').check()
  await until(async () => (await panel.locator('[data-fix-summary]').count()) > 0, 4000)
  const p1 = last(/edits\/preview$/)
  check('选中后发 POST …/edits/preview，body 是 {fix: {id, option}, seq}',
        JSON.stringify(p1?.body?.fix) === JSON.stringify({ id: FX.remove.id, option: 'remove' }) && Number.isInteger(p1?.body?.seq)
        && Object.keys(p1?.body ?? {}).sort().join(',') === 'fix,seq', JSON.stringify(p1?.body ?? {}))
  check('……摘要写的是预览返回的 summary', (await panel.locator('[data-fix-summary]').innerText()).includes('剩下 10 个'))
  check('……[data-fix-compare] 显示预览返回的对照', (await panel.locator('[data-fix-compare]').innerText().catch(() => '')).includes('去掉标签：「7-8」'))
  const panelText = await panel.innerText()
  check('……面板文本里没有补丁路径（不含「/sheets/」「/blocks/」）', !panelText.includes('/sheets/') && !panelText.includes('/blocks/'))
  check('……面板文本不含「快照」「并集」「构建」「锚点」「放行」「钉」，不露机读码', !P3_FORBIDDEN.test(panelText) && !P3_CODES.test(panelText), panelText.slice(0, 120))
  await page.locator('[data-preview-toggle] button', { hasText: '修改后' }).click()
  await page.waitForTimeout(150)
  check('……网格切到「修改后」时按预览的干跑着色（[data-preview-mark]）', await page.locator('[data-sheet-grid] [data-preview-mark]').count() > 0)
  replies.apply.push((route) => json(base({ trial: rejected, draft_problems: FIX_PROBLEMS.slice(1), fixes: [FX.ignore, FX.placeholder],
    edits: [fixEdit()], recipe_sha256: sha('b') }))(route))
  await panel.locator('[data-fix-apply]').click()
  const edits = page.locator('[data-edits]')
  await edits.waitFor({ timeout: 4000 }).catch(() => {})
  const a1 = last(/edits\/apply$/)
  check('「应用修复」发 POST …/edits/apply，expected_sha256 等于预览返回的 recipe_sha256_after',
        a1?.body?.expected_sha256 === sha('b') && JSON.stringify(a1?.body?.fix) === JSON.stringify({ id: FX.remove.id, option: 'remove' }),
        JSON.stringify(a1?.body ?? {}))
  check('……返回带 edits 的暂存区：[data-edits] 有一条，有「撤销上一次修改」', await edits.locator('[data-edit]').count() === 1
        && (await edits.locator('[data-edit-undo]').innerText().catch(() => '')).includes('撤销上一次修改'))
  check('……面板关闭，提示重新试运行', await panel.count() === 0
        && (await page.locator('[data-wizard-notice]').innerText().catch(() => '')).includes('请重新试运行'))
  replies.undo.push((route) => json(base({ trial: rejected, draft_problems: FIX_PROBLEMS, fixes: allFixes, edits: [] }))(route))
  await edits.locator('[data-edit-undo]').click()
  await until(async () => count(/edits\/undo$/) === 1, 4000)
  await page.waitForTimeout(200)
  check('点「撤销上一次修改」发 POST …/edits/undo', count(/edits\/undo$/) === 1 && JSON.stringify(last(/edits\/undo$/)?.body) === '{}'
        && await page.locator('[data-edits]').count() === 0)

  // ---- 需要理由的提议：不自动预览；理由改了预览作废；应用发的理由与最后一次预览逐字相同
  await page.locator('[data-wizard-side] [data-problem="row_unclaimed"] [data-fix="ignore_cells"]').click()
  await panel.waitFor({ timeout: 4000 }).catch(() => {})
  const previews0 = count(/edits\/preview$/)
  await panel.locator('[data-fix-option="ignore"] input').check()
  await page.waitForTimeout(300)
  check('需要理由的选项：选中后不自动发预览；理由为空时「预览」「应用修复」都禁用',
        count(/edits\/preview$/) === previews0 && await panel.locator('[data-fix-preview]').isDisabled() && await panel.locator('[data-fix-apply]').isDisabled())
  const ignoreReply = (after) => (route, { body }) => json(editPreview({ key: 'ignore_cells:rows:交叉表:补录（人次）', title: FX.ignore.title,
    summary: [`按行标签忽略「补录（人次）」，理由：${body.fix.reason}`], recipe_sha256_after: after, seq: body.seq }))(route)
  replies.preview.push(ignoreReply(sha('c')))
  await panel.locator('[data-fix-reason]').fill('表下补录的一行，下月起不再出现，检查脚本')
  await panel.locator('[data-fix-preview]').click()
  await until(async () => count(/edits\/preview$/) === previews0 + 1, 4000)
  await page.waitForTimeout(200)
  check('……填理由、点「预览」后发 preview，body 带理由',
        last(/edits\/preview$/)?.body?.fix?.reason === '表下补录的一行，下月起不再出现，检查脚本' && await panel.locator('[data-fix-apply]').isEnabled())
  await panel.locator('[data-fix-reason]').fill('表下补录的一行，检查脚本改过理由')
  await page.waitForTimeout(150)
  check('……预览之后改理由：「应用修复」禁用，提示「理由已修改，请重新预览」',
        await panel.locator('[data-fix-apply]').isDisabled() && (await panel.locator('[data-fix-stale-hint]').innerText().catch(() => '')).includes('理由已修改，请重新预览'))
  replies.preview.push(ignoreReply(sha('d')))
  await panel.locator('[data-fix-preview]').click()
  await until(async () => count(/edits\/preview$/) === previews0 + 2, 4000)
  await page.waitForTimeout(200)
  check('……重新预览后「应用修复」才可用', await panel.locator('[data-fix-apply]').isEnabled() && await panel.locator('[data-fix-stale-hint]').count() === 0)
  replies.apply.push(coded(409, 'edit_stale', '配方在预览之后有变化，请重新预览'))
  await panel.locator('[data-fix-apply]').click()
  await until(async () => count(/edits\/apply$/) === 2, 4000)
  await page.waitForTimeout(200)
  const a2 = last(/edits\/apply$/)
  check('……apply 的理由与最后一次预览逐字相同，expected_sha256 是最后一次预览的',
        a2?.body?.fix?.reason === '表下补录的一行，检查脚本改过理由' && a2?.body?.expected_sha256 === sha('d'), JSON.stringify(a2?.body ?? {}))
  check('返回 409 edit_stale：留在面板，「应用修复」禁用，提示「配方在预览之后有变化，请重新预览」',
        await panel.count() === 1 && await panel.locator('[data-fix-apply]').isDisabled()
        && (await panel.innerText()).includes('配方在预览之后有变化，请重新预览'))
  replies.preview.push(ignoreReply(sha('e')))
  await panel.locator('[data-fix-preview]').click()
  await until(async () => count(/edits\/preview$/) === previews0 + 3, 4000)
  await page.waitForTimeout(200)
  replies.apply.push(coded(409, 'fix_stale', '这条修复建议已不适用'))
  replies.get.push((route) => json(base({ trial: rejected, draft_problems: FIX_PROBLEMS, fixes: allFixes }))(route))
  const gets0 = count(/^GET \/datasources\/imports\/stg-fix$/)
  await panel.locator('[data-fix-apply]').click()
  await until(async () => count(/^GET \/datasources\/imports\/stg-fix$/) === gets0 + 1, 4000)
  await page.waitForTimeout(200)
  check('返回 409 fix_stale：面板关闭、暂存区重新获取，提示问题已变化',
        await panel.count() === 0 && count(/^GET \/datasources\/imports\/stg-fix$/) === gets0 + 1
        && (await page.locator('[data-wizard-notice]').innerText().catch(() => '')).includes('问题已变化'))

  // ---- 乱序回包：连续切换两个选项，第一次的回包晚到，只显示第二次的
  await page.locator('[data-wizard-side] [data-problem="value_not_number"] [data-fix="declare_placeholder"]').click()
  await panel.waitFor({ timeout: 4000 }).catch(() => {})
  replies.preview.push((route, { body }) => delayed(1500, editPreview({ summary: ['检查脚本：第一次预览'], seq: body.seq }))(route))
  replies.preview.push((route, { body }) => json(editPreview({ summary: ['检查脚本：第二次预览'], seq: body.seq }))(route))
  await panel.locator('[data-fix-option="no_data"] input').check()
  await page.waitForTimeout(100)
  await panel.locator('[data-fix-option="not_applicable"] input').check()
  await page.waitForTimeout(2000)
  const orderText = await panel.locator('[data-fix-summary]').innerText().catch(() => '')
  check('乱序回包：第一次预览的回包晚到，界面只显示第二次的 summary',
        orderText.includes('第二次预览') && !orderText.includes('第一次预览'), orderText)

  // ---- 预览 ok=false（200，带 problems）：原因逐条显示、坐标可点，「应用修复」禁用，不能只剩一块空的预览区
  replies.preview.push((route, { body }) => json(editPreview({ ok: false, summary: [], recipe_sha256_after: null, dry_run: null, seq: body.seq,
    problems: [{ code: 'edit_blocked', category: 'recipe', cells: ['客流汇总!D12'], message: '检查脚本：这条修复现在不能应用，原因写在这里' }] }))(route))
  await panel.locator('[data-fix-option="no_data"] input').check()
  await until(async () => (await panel.locator('[data-edit-preview="blocked"]').count()) > 0, 4000)
  await page.waitForTimeout(100)
  check('伪造 ok=false 的修复预览：原因可见（坐标可点），「应用修复」禁用，面板留着',
        (await panel.locator('[data-fix-problems] [data-fix-problem]').innerText().catch(() => '')).includes('原因写在这里')
        && await panel.locator('[data-fix-problem] [data-cell-ref="客流汇总!D12"]').count() === 1
        && await panel.locator('[data-fix-apply]').isDisabled() && await panel.count() === 1)
  // 原因是 edit_not_applicable（提议对应的内容已不在当前配方里）：按错误表处理——刷新暂存区、关掉面板、提示问题已变化
  const goneMsg = '这条修复建议对应的内容已不在当前的配方里：配方在提出建议之后又被改过。请重新查看问题和修复建议'
  replies.preview.push((route, { body }) => json(editPreview({ ok: false, summary: [], recipe_sha256_after: null, dry_run: null, seq: body.seq,
    problems: [{ code: 'edit_not_applicable', category: 'recipe', cells: [], message: goneMsg }] }))(route))
  replies.get.push((route) => json(base({ trial: rejected, draft_problems: FIX_PROBLEMS, fixes: allFixes }))(route))
  const gets1 = count(/^GET \/datasources\/imports\/stg-fix$/)
  await panel.locator('[data-fix-option="not_applicable"] input').check()
  await until(async () => count(/^GET \/datasources\/imports\/stg-fix$/) === gets1 + 1, 4000)
  await page.waitForTimeout(200)
  const goneNotice = await page.locator('[data-wizard-notice]').innerText().catch(() => '')
  check('……ok=false 的原因是 edit_not_applicable：面板关闭、暂存区重新获取，提示问题已变化并附服务端原话',
        await panel.count() === 0 && count(/^GET \/datasources\/imports\/stg-fix$/) === gets1 + 1
        && goneNotice.includes('问题已变化') && goneNotice.includes('已不在当前的配方里'), goneNotice)

  // ---- 确认清单：修复的确认项在「修改」组
  replies.trial.push((route) => json(base({ status: 'trialed', fixes: [], edits: [fixEdit()], trial: flowTrial('passed', {
    confirm_items: [
      { id: 'fix:remove_label:日间:7-8', label: '在分段「日间」的期望标签中去掉「7-8」', required: true, source: 'edit' },
      { id: 'diff:label_writing:日间', label: '分段「日间」的标签写法变了', required: true, source: 'diff' },
      { id: 'unit:日客流.全日客流', label: '「全日客流（人次）」→ 列「全日客流」，单位 人次', required: true, source: 'recipe' },
    ] }) }))(route))
  await page.locator('[data-trial-run]').click()
  await page.locator('[data-to-confirm]').waitFor({ timeout: 4000 }).catch(() => {})
  await page.locator('[data-to-confirm]').click()
  const editGroup = page.locator('[data-confirm-group="edit"]')
  await editGroup.waitFor({ timeout: 4000 }).catch(() => {})
  check('确认清单里有 fix:remove_label:日间:7-8，在「修改」组里',
        await editGroup.locator('[data-confirm-item="fix:remove_label:日间:7-8"]').count() === 1
        && (await editGroup.locator('[data-confirm-group-title]').innerText()).trim() === '修改')
  await page.keyboard.press('Escape')
  await until(async () => await page.locator('[data-import-wizard]').count() === 0, 4000)

  // ---- D13 两步修复：应用 ② 之后静态校验报关系成员对不上；抽屉关着时，试运行按钮上方就有出路
  await page.locator('[data-source="zz_recipe_d13"] [data-open-staging]').click()
  const bar = page.locator('[data-recipe-problems-bar]')
  await bar.waitFor({ timeout: 4000 }).catch(() => {})
  check('D13：配方有问题时，抽屉关着也有 [data-recipe-problems-bar]（在试运行按钮上方）',
        await bar.count() === 1 && await page.locator('[data-recipe-panel]').count() === 0 && (await bar.innerText()).includes('配方有 1 个问题')
        && await page.locator('[data-trial-run]').isDisabled())
  await bar.locator('[data-recipe-problems-toggle]').click()
  check('……展开后有「更新关系成员」（[data-fix="edit_members"]）', await bar.locator('[data-fix="edit_members"]').count() === 1
        && (await bar.locator('[data-fix="edit_members"]').innerText()).includes('更新关系成员'))
  await bar.locator('[data-fix="edit_members"]').click()
  check('……点了打开修复面板', await page.locator('[data-fix-panel="fx-3d4e5f6a7b8c"]').count() === 1)
  await page.keyboard.press('Escape')
  await until(async () => await page.locator('[data-import-wizard]').count() === 0, 4000)

  // ---- 工作表改名：回执的改名说明旁有「更新工作表名」，确认项旁也有
  await page.locator('[data-source="zz_recipe_rename"] [data-open-staging]').click()
  const renamed = page.locator('[data-sheet-renamed]')
  await renamed.waitFor({ timeout: 4000 }).catch(() => {})
  /** 某处旁边的「更新工作表名」按钮各指向哪个提议 */
  const renameIdsIn = (loc) => loc.locator('[data-fix="rename_sheet"]').evaluateAll((els) => els.map((e) => e.getAttribute('data-fix-id')))
  const r1 = await renameIdsIn(renamed.locator('[data-sheet-renamed-item="客流汇总（新）"]'))
  const r2 = await renameIdsIn(renamed.locator('[data-sheet-renamed-item="附表（新）"]'))
  check('工作表改名：回执的每条改名说明旁各有一个 [data-fix="rename_sheet"]，指向这张表的提议',
        JSON.stringify(r1) === JSON.stringify([FX.sheet.id]) && JSON.stringify(r2) === JSON.stringify([FX.sheet2.id])
        && (await renamed.innerText()).includes('客流汇总（新）'), JSON.stringify({ r1, r2 }))
  await page.locator('[data-to-confirm]').click()
  await page.locator('[data-confirm-list]').waitFor({ timeout: 4000 }).catch(() => {})
  const c1 = await renameIdsIn(page.locator('[data-confirm-item="sheet_renamed:s1"]'))
  const c2 = await renameIdsIn(page.locator('[data-confirm-item="sheet_renamed:s2"]'))
  check('……确认项 sheet_renamed:<sid> 旁也有「更新工作表名」，只放这张表的那一个（按回执的 sheets.matched 对上本期表名），不列全部',
        JSON.stringify(c1) === JSON.stringify([FX.sheet.id]) && JSON.stringify(c2) === JSON.stringify([FX.sheet2.id]), JSON.stringify({ c1, c2 }))
  await page.locator('[data-confirm-item="sheet_renamed:s1"] [data-fix="rename_sheet"]').click()
  check('……点了回到起草、打开修复面板', await page.locator('[data-fix-panel="fx-4e5f6a7b8c9d"]').count() === 1
        && await page.locator('[data-import-wizard][data-view="draft"]').count() === 1)
  const allText = await wizardText(page)
  check('整个向导的新增文字不含「快照」「并集」「构建」「锚点」「放行」「钉」', !P3_FORBIDDEN.test(allText), allText.match(P3_FORBIDDEN)?.[0] ?? '')
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

/** 实验室用例 c01（合成、假名）：表头在 C5，上方标题、单位、编制，下方合计行、空行、备注；另有一张「说明」 */
function c01Grids() {
  const cells = [
    [1, 3, '2026年8月 销售月报', 'text'], [2, 3, '单位：万元', 'text'], [3, 3, '编制：财务部    日期：2026-09-05', 'text'],
    [5, 3, '地区', 'text'], [5, 4, '产品', 'text'], [5, 5, '销量', 'text'], [5, 6, '金额', 'text'],
    [6, 3, '分区甲', 'text'], [6, 4, '产品甲', 'text'], [6, 5, '10', 'number'], [6, 6, '100', 'number'],
    [7, 3, '分区乙', 'text'], [7, 4, '产品乙', 'text'], [7, 5, '20', 'number'], [7, 6, '120', 'number'],
    [8, 3, '分区丙', 'text'], [8, 4, '产品丙', 'text'], [8, 5, '15', 'number'], [8, 6, '80', 'number'],
    [9, 3, '合计', 'text'], [9, 5, '45', 'number'], [9, 6, '300', 'number'],
    [11, 3, '注：数据来源于业务系统，金额含税。', 'text'], [12, 3, '制表人：经办甲', 'text'],
  ]
  const rows = []; for (let r = 1; r <= 12; r++) rows.push(r)
  return [
    { sheet: '月报', bounds: 'C1:F12', total_rows: 12, total_cols: 4, truncated: false, rows, cols: [3, 4, 5, 6], cells,
      formulas: {}, merges: ['C1:F1'], hidden_rows: [], hidden_cols: [] },
    { sheet: '说明', bounds: 'A1:A2', total_rows: 2, total_cols: 1, truncated: false, rows: [1, 2], cols: [1],
      cells: [[1, 1, '本表仅供检查脚本使用', 'text'], [2, 1, '数字均为手写假数', 'text']], formulas: {}, merges: [], hidden_rows: [], hidden_cols: [] },
  ]
}
const C01_MARKS = [
  ['outside_text', 'C1', null], ['outside_text', 'C2', null], ['outside_text', 'C3', null], ['col_header', 'C5:F5', '列表1'],
  ['value', 'C6:F8', '列表1'], ['total_label', 'C9', '列表1'], ['total_value', 'E9:F9', '列表1'],
  ['outside_text', 'C11', null], ['outside_text', 'C12', null],
].map(([role, ref, block]) => ({ sheet: '月报', role, ref, block }))
const C01_RECIPE = {
  recipe_format: 'agentlab-recipe/2', sheets: [{ id: 's1', match: { name: '月报' }, blocks: [{ id: '列表1', layout: 'list', table: '月报',
    columns: [{ header: '地区', name: '地区', type: 'TEXT' }, { header: '产品', name: '产品', type: 'TEXT' },
      { header: '销量', name: '销量', type: 'INTEGER' }, { header: '金额', name: '金额', type: 'INTEGER' }],
    rows: { blank_rows: 'stop', total_row: { label_column: '地区', pick: '合计', keep_as: '月报_表内合计' } } }] }],
  tables: [{ name: '月报', grain: ['地区', '产品'], units: { 金额: '万元' } }],
}

await section('数据 · 表格：框选转锚点', async () => {
  const src = recipeSource('check-manage-c01', 'zz_recipe_c01', {
    open_staging: { id: 'stg-c01', kind: 'redraft', status: 'drafting', created_at: ago(60_000) } })
  const c01Staging = (over = {}) => flowStaging({
    id: 'stg-c01', kind: 'redraft', status: 'drafting', source: { id: src.id, name: src.name, exists: true, import_mode: 'recipe' },
    file: { name: 'c01_literal.xlsx', size: 9000, sha256_prefix: 'd4e5f6a7' }, grids: c01Grids(), marks: C01_MARKS,
    draft: null, cards: [], questions: [], answers: {}, ai: { offered: false, available: true, reason: '', model: '', provider: '' },
    recipe: C01_RECIPE, recipe_sha256: sha('a'), candidates: {}, edits: [], answers_dropped: [], fixes: [],
    draft_problems: [{ code: 'outside_number', category: 'confirm', message: '区域外有含数字的文字', cells: ['月报!C1'] },
      { code: 'cell_unclaimed', category: 'structure', message: '检查脚本：第 400 行有一格没有去处', cells: ['月报!C400'], fix_ids: [] }],
    ...over,
  })
  const replies = { preview: [], apply: [] }
  const take = (k) => (route, ctx) => {
    const r = replies[k].shift()
    return r ? r(route, ctx) : json({ detail: `检查脚本没有准备这次的回答（${k}）` }, 500)(route)
  }
  const listPreview = (body, over = {}) => editPreview({
    kind: 'selection', key: '列表1', title: '按框选替换列表「列表1」', summary: ['替换现有的列表「列表1」：表头按文字定位'],
    anchors: ['地区', '产品', '销量', '金额'].map((text, i) => ({ kind: 'header', text, cell: `月报!${'CDEF'[i]}5` })),
    replay: { expected: { header: 'C5:F5', data: 'C6:F8', total: 'C9:F9' }, actual: { header: 'C5:F5', data: 'C6:F8', total: 'C9:F9' }, match: true, diffs: [], window_rows: null },
    expected: { header: 'C5:F5', data: 'C6:F8', total: 'C9:F9' }, block: '列表1', dry_run: { problems: [], partial: false, marks: C01_MARKS },
    compare: emptyCompare(), seq: body.seq, ...over,
  })
  const handlers = [
    [/^GET \/datasources$/, (route) => json([...sources, src])(route)],
    [/^GET \/datasources\/check-manage-c01\/schema$/, json({ tables: ['月报'], summary: '', synced_at: ago(0) })],
    [/^GET \/datasources\/imports\/stg-c01$/, (route) => json(c01Staging())(route)],
    [/^POST \/datasources\/imports\/[^/]+\/edits\/preview$/, take('preview')],
    [/^POST \/datasources\/imports\/[^/]+\/edits\/apply$/, take('apply')],
  ]
  const { page, sent, natives, errors, close } = await open('/data/tables', { handlers })
  const previews = () => sent.filter((s) => /edits\/preview$/.test(s.key))
  await page.locator('[data-source="zz_recipe_c01"] [data-open-staging]').click()
  const grid = page.locator('[data-sheet-grid]')
  await grid.locator('[data-cell="C5"]').waitFor({ timeout: 5000 }).catch(() => {})
  const center = async (cell) => {
    const b = await grid.locator(`[data-cell="${cell}"]`).boundingBox()
    return { x: b.x + b.width / 2, y: b.y + b.height / 2 }
  }
  const dragFromTo = async (a, b, mid) => {
    const [pa, pb] = [await center(a), await center(b)]
    await page.mouse.move(pa.x, pa.y)
    await page.mouse.down()
    let midState = null
    if (mid) {
      const pm = await center(mid)
      await page.mouse.move(pm.x, pm.y, { steps: 4 })
      await page.waitForTimeout(120)
      midState = {
        tds: await grid.locator('td').count(),
        sel: await grid.locator('[data-selection]').getAttribute('data-selection').catch(() => null),
        box: await grid.locator('[data-selection]').boundingBox().catch(() => null),
      }
    }
    await page.mouse.move(pb.x, pb.y, { steps: 4 })
    await page.waitForTimeout(120)
    await page.mouse.up()
    await page.waitForTimeout(150)
    return midState
  }

  // ---- 未开框选：拖动不产生选区，也不发预览
  const tds0 = await grid.locator('td').count()
  await dragFromTo('C5', 'F8')
  check('未开框选时用鼠标拖动：不产生 [data-selection]，不发预览', await grid.locator('[data-selection]').count() === 0 && previews().length === 0)

  // ---- 打开框选，从 C5 拖到 F8
  await grid.locator('[data-select-toggle]').click()
  const mid = await dragFromTo('C5', 'F8', 'E7')
  const selection = grid.locator('[data-selection]')
  const endBox = await selection.boundingBox().catch(() => null)
  check('打开「框选」，从 C5 拖到 F8：出现 [data-selection="C5:F8"]', (await selection.getAttribute('data-selection').catch(() => '')) === 'C5:F8')
  check('……选区栏写「已选 C5:F8」', (await grid.locator('[data-selection-bar]').innerText().catch(() => '')).includes('已选 C5:F8'))
  check('……拖动过程中网格的 td 数量不变，只有覆盖层在变（拖到 E7 时选区是 C5:E7，比松手时窄）',
        mid?.tds === tds0 && (await grid.locator('td').count()) === tds0 && mid?.sel === 'C5:E7'
        && !!mid?.box && !!endBox && mid.box.width < endBox.width, JSON.stringify({ tds0, mid, endBox }))

  // ---- 「框选为…」选「列表（含表头）」
  replies.preview.push((route, { body }) => json(listPreview(body))(route))
  await grid.locator('[data-selection-menu]').selectOption('list')
  const sp = page.locator('[data-selection-preview]')
  await sp.waitFor({ timeout: 4000 }).catch(() => {})
  const pv = previews().at(-1)?.body
  const want = { sheet: '月报', ref: 'C5:F8', as: 'list', options: { header_rows: 1, bottom: 'box' } }
  // 按键排序后逐层比较：键的先后不算差别，嵌套对象的每个键都要对上
  const canon = (v) => (Array.isArray(v) ? v.map(canon)
    : v && typeof v === 'object' ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, canon(v[k])])) : v)
  const sameJson = (a, b) => JSON.stringify(canon(a)) === JSON.stringify(canon(b))
  check('选「列表（含表头）」发 POST …/edits/preview，body 是 {selection: {sheet, ref, as, options: {header_rows: 1, bottom: "box"}}, seq}',
        previews().length === 1 && sameJson(pv?.selection, want) && Number.isInteger(pv?.seq) && Object.keys(pv ?? {}).sort().join(',') === 'selection,seq', JSON.stringify(pv ?? {}))
  const anchors = await sp.locator('[data-anchor]').allInnerTexts()
  check('……预览面板的定位文字依次是地区、产品、销量、金额', JSON.stringify(anchors.map((x) => x.trim())) === JSON.stringify(['地区', '产品', '销量', '金额']), anchors.join(','))
  check('……有 [data-replay-match="true"] 和「重放结果与框选一致」', await sp.locator('[data-replay-match="true"]').count() === 1
        && (await sp.innerText()).includes('重放结果与框选一致'))
  const dashed = await selection.evaluate((el) => getComputedStyle(el).borderTopStyle).catch(() => '')
  const solid = await grid.locator('[data-replay-mark]').first().evaluate((el) => getComputedStyle(el).borderTopStyle).catch(() => '')
  check('……网格上选区画虚线、重放识别的范围画实线（[data-replay-mark]）', dashed === 'dashed' && solid === 'solid'
        && await grid.locator('[data-replay-mark]').count() === 3, `${dashed} / ${solid}`)
  const spText = await page.locator('[data-selection-panel]').innerText()
  check('……面板整段文本不含「正则」「regex」「pattern」，并含「按文字定位」', !/正则|regex|pattern/i.test(spText) && spText.includes('按文字定位'))
  check('……面板文本不含「快照」「并集」「构建」「锚点」「放行」「钉」', !P3_FORBIDDEN.test(spText), spText.match(P3_FORBIDDEN)?.[0] ?? '')

  // ---- 换算不了：「应用」禁用，原因可见
  replies.preview.push((route, { body }) => json(listPreview(body, { ok: false, anchors: [], replay: null, dry_run: null, summary: [],
    recipe_sha256_after: null, problems: [{ code: 'selection_bottom_not_anchorable', category: 'recipe', cells: ['月报!C8:F8'],
      message: '第 8 行还有数据：按文字无法表达「到第 7 行为止」。请把框扩大到数据的实际末尾（第 8 行），改选「下边界按规则推断」，或删掉文件里多出的行' }] }))(route))
  await grid.locator('[data-selection-ref]').fill('C5:F7')
  await grid.locator('[data-selection-ref]').press('Enter')
  await grid.locator('[data-selection-menu]').selectOption('list')
  await until(async () => previews().length === 2 && (await page.locator('[data-selection-preview="blocked"]').count()) === 1, 4000)
  check('伪造 ok=false（下边界无法按文字表达）：「应用」禁用，原因可见',
        await page.locator('[data-selection-apply]').isDisabled()
        && (await page.locator('[data-selection-problem]').innerText().catch(() => '')).includes('第 8 行还有数据')
        && await page.locator('[data-selection-problem] [data-cell-ref="月报!C8:F8"]').count() === 1)

  // ---- 部分干跑：只比对前 500 行
  replies.preview.push((route, { body }) => json(listPreview(body, { replay: {
    expected: { header: 'C5:F5', data: 'C6:F8', total: 'C9:F9' }, actual: { header: 'C5:F5', data: 'C6:F8', total: 'C9:F9' }, match: true, diffs: [], window_rows: 500 } }))(route))
  await page.locator('[data-selection-bottom="auto"] input').check()
  await until(async () => previews().length === 3, 4000)
  await page.waitForTimeout(200)
  check('改选「按规则推断」重新预览（bottom: "auto"）；部分干跑时写「仅比对前 500 行」',
        previews().at(-1)?.body?.selection?.options?.bottom === 'auto'
        && (await page.locator('[data-replay-window]').innerText().catch(() => '')).includes('仅比对前 500 行')
        && await page.locator('[data-selection-apply]').isEnabled())
  replies.apply.push((route) => json(c01Staging({ edits: [fixEdit({ kind: 'selection', key: 'list:列表1', title: '按框选替换列表「列表1」' })] }))(route))
  await page.locator('[data-selection-apply]').click()
  await until(async () => sent.some((s) => /edits\/apply$/.test(s.key)), 4000)
  await page.waitForTimeout(200)
  const ap = [...sent].reverse().find((s) => /edits\/apply$/.test(s.key))?.body
  check('「应用」发 POST …/edits/apply：选区与预览时相同，expected_sha256 是预览返回的；之后选区清掉',
        ap?.selection?.ref === 'C5:F7' && ap?.selection?.options?.bottom === 'auto' && ap?.expected_sha256 === sha('b')
        && await grid.locator('[data-selection]').count() === 0 && await page.locator('[data-selection-panel]').count() === 0, JSON.stringify(ap ?? {}))

  // ---- 键盘：方向键移动当前格，Shift + 方向键扩选，Esc 只取消选区
  await grid.locator('[data-grid-box]').focus()
  await page.waitForTimeout(100)
  // 当前格可能停在上一次拖动、输入的地方：按它离 C5 有几行几列按方向键（c01 的行列在预览里是连续的）
  const start = await grid.locator('[data-grid-cursor]').getAttribute('data-grid-cursor').catch(() => '')
  const m = /^([A-Z]+)(\d+)$/.exec(start ?? '')
  if (m) {
    const dc = 'C'.charCodeAt(0) - m[1].charCodeAt(0)
    const dr = 5 - Number(m[2])
    for (let i = 0; i < Math.abs(dc); i++) await page.keyboard.press(dc > 0 ? 'ArrowRight' : 'ArrowLeft')
    for (let i = 0; i < Math.abs(dr); i++) await page.keyboard.press(dr > 0 ? 'ArrowDown' : 'ArrowUp')
  }
  const cur = await grid.locator('[data-grid-cursor]').getAttribute('data-grid-cursor').catch(() => '')
  check('键盘：框选模式下聚焦网格有当前格（[data-grid-cursor]），方向键把它移到 C5', !!m && cur === 'C5', `${start} → ${cur}`)
  for (let i = 0; i < 3; i++) await page.keyboard.press('Shift+ArrowRight')
  for (let i = 0; i < 3; i++) await page.keyboard.press('Shift+ArrowDown')
  await page.waitForTimeout(100)
  check('……Shift + 右三下、下三下：[data-selection="C5:F8"]', (await grid.locator('[data-selection]').getAttribute('data-selection').catch(() => '')) === 'C5:F8')
  await page.keyboard.press('Escape')
  await page.waitForTimeout(300)
  check('……按 Esc 选区消失，向导仍然开着（没有当成关闭）', await grid.locator('[data-selection]').count() === 0
        && await page.locator('[data-import-wizard]').count() === 1)

  // ---- 区域输入
  await grid.locator('[data-selection-ref]').fill('C5:F8')
  await grid.locator('[data-selection-ref]').press('Enter')
  await page.waitForTimeout(100)
  check('区域输入：在 [data-selection-ref] 里输入「C5:F8」得到同样的选区', (await grid.locator('[data-selection]').getAttribute('data-selection').catch(() => '')) === 'C5:F8')
  await grid.locator('[data-selection-ref]').fill('C5:')
  await grid.locator('[data-selection-ref]').press('Enter')
  check('……输入「C5:」提示写法不对', (await grid.locator('[data-selection-ref-error]').innerText().catch(() => '')).includes('写法不对'))

  // ---- 合并区：c01 的标题 C1:F1 是一个合并格。选区与合并区相交时扩到整个合并区（Excel 的习惯），输入和拖动两条路都守住
  await grid.locator('[data-selection-ref]').fill('D1:E2')
  await grid.locator('[data-selection-ref]').press('Enter')
  await page.waitForTimeout(100)
  const mergedByRef = await grid.locator('[data-selection]').getAttribute('data-selection').catch(() => '')
  check('区域输入「D1:E2」（与合并区 C1:F1 相交）：选区扩到整个合并区，[data-selection="C1:F2"]', mergedByRef === 'C1:F2', mergedByRef)
  await grid.locator('[data-selection-cancel]').click()
  await dragFromTo('D2', 'C1')
  const mergedByDrag = await grid.locator('[data-selection]').getAttribute('data-selection').catch(() => '')
  check('……从 D2 拖到合并格 C1：同样扩到 C1:F2（不是只到 D 列的 C1:D2）', mergedByDrag === 'C1:F2', mergedByDrag)
  await grid.locator('[data-selection-ref]').fill('C5:F8')
  await grid.locator('[data-selection-ref]').press('Enter')
  await grid.getByRole('tab', { name: '说明' }).click()
  await page.waitForTimeout(200)
  check('切换工作表页签后选区清空', await page.locator('[data-sheet-grid] [data-selection]').count() === 0)
  await page.locator('[data-sheet-grid]').getByRole('tab', { name: '月报' }).click()
  await page.waitForTimeout(150)

  // ---- 定位到预览范围之外的格：说清楚，不静默无反应
  await page.locator('[data-wizard-side] [data-cell-ref="月报!C400"]').click()
  await page.waitForTimeout(200)
  check('定位到第 400 行的坐标时，出现「这一格不在预览范围内」的提示',
        (await page.locator('[data-grid-out-of-range]').innerText().catch(() => '')).includes('这一格不在预览范围内'))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()

  // ---- 窄视口（390 宽）：用区域输入完成框选并发出预览；选区栏完整可见，没有横向滚动
  const narrow = await open('/data/tables', { handlers, viewport: { width: 390, height: 844 } })
  const np = narrow.page
  await np.locator('[data-source="zz_recipe_c01"] [data-open-staging]').click()
  const ngrid = np.locator('[data-sheet-grid]')
  await ngrid.locator('[data-select-toggle]').waitFor({ timeout: 5000 }).catch(() => {})
  await ngrid.locator('[data-select-toggle]').click()
  await ngrid.locator('[data-selection-ref]').fill('C5:F8')
  await ngrid.locator('[data-selection-ref]').press('Enter')
  replies.preview.push((route, { body }) => json(listPreview(body))(route))
  await ngrid.locator('[data-selection-menu]').selectOption('list')
  await until(async () => narrow.sent.some((s) => /edits\/preview$/.test(s.key)), 4000)
  const nb = narrow.sent.find((s) => /edits\/preview$/.test(s.key))?.body
  const barBox = await ngrid.locator('[data-selection-bar]').boundingBox().catch(() => null)
  const fits = await ngrid.locator('[data-selection-bar]').evaluate((el) => el.scrollWidth <= el.clientWidth + 1).catch(() => false)
  const bodyScroll = await np.locator('[role="dialog"] [data-modal-body]').evaluate((el) => el.scrollWidth - el.clientWidth).catch(() => 99)
  check('窄视口（390 宽）：用区域输入完成框选并发出预览', nb?.selection?.ref === 'C5:F8' && nb?.selection?.as === 'list', JSON.stringify(nb ?? {}))
  check('……选区栏完整可见（在视口内、没有被裁），向导没有横向滚动',
        !!barBox && barBox.x >= 0 && barBox.x + barBox.width <= 390 && fits && bodyScroll <= 1,
        JSON.stringify({ barBox, fits, bodyScroll }))
  check('……没有运行时报错', narrow.errors.length === 0, narrow.errors[0] ?? '')
  await narrow.close()
})

/** 按期累积的计划（契约 AccumulatePlan）：默认是参考配方 8 月加 9 月（P3-SPEC 2.12 的数字） */
const AUG = { import_id: 'imp-8', seq: 1, start: '2026-08-01', end: '2026-08-31', file_name: '月报导出_2026-08-01_2026-08-31.xlsx', new: false,
  rows: { 日客流: 31, 时段客流: 527, 时段客流_表内合计: 93 }, blockers: [] }
const SEP = { import_id: null, seq: null, start: '2026-09-01', end: '2026-09-30', file_name: '月报导出_2026-09-01_2026-09-30.xlsx', new: true,
  rows: { 日客流: 30, 时段客流: 510, 时段客流_表内合计: 90 }, blockers: [] }
const plan = (over = {}) => ({
  mode: 'accumulate', action: 'append', period: { start: '2026-09-01', end: '2026-09-30' }, parts: [AUG, SEP],
  replaces: null, dropped: [], overlaps: [], gaps: [], backfill: false, change: 'same', added: [], retired_new: [], retired_existing: [],
  semantic: [], label_sets: [], mode_switch: null, reason: null,
  union: { union_id: sha('9'), db_sha256: sha('8'), rows: { 日客流: 61, 时段客流: 1037, 时段客流_表内合计: 183 } }, ...over,
})
const unionChecks = ['union_rows', 'union_pk', 'union_period'].map((kind, i) => flowCheck(`U${i + 1}`, kind,
  ['各表行数等于各期之和', '主键在各期之间没有重复', '各期的日期都在该期统计期内'][i], 'passed'))

await section('数据 · 表格：按期累积', async () => {
  const src = recipeSource('check-manage-acc', 'zz_recipe_acc', {
    open_staging: { id: 'stg-acc', kind: 'redraft', status: 'drafting', created_at: ago(60_000) } })
  const MODE_Q = { id: 'q_mode', text: '这份报表每期怎么更新', default: null, options: [
    { value: 'accumulate', label: '按期累积（建议）', needs_reason: false }, { value: 'replace', label: '每期替换', needs_reason: false }] }
  const MODE_CARD = { id: 'mode', title: '导入模式：按期累积（建议）', reason: '这是按统计期出的报表：每期以统计期为键累积，跨期可以比较；也可以选每期替换', cells: [], question: 'q_mode' }
  let current = flowStaging({
    id: 'stg-acc', kind: 'redraft', status: 'drafting', source: { id: src.id, name: src.name, exists: true, import_mode: 'recipe' },
    draft: null, cards: [...FLOW_CARDS.filter((c) => c.id !== 'mode'), MODE_CARD], questions: [...FLOW_QUESTIONS, MODE_Q], answers: {},
    ai: { offered: false, available: true, reason: '', model: '', provider: '' }, recipe: { ...FLOW_RECIPE, mode: 'accumulate' },
    draft_problems: [], edits: [], fixes: [], answers_dropped: [],
  })
  const replies = { answers: [], trial: [], commit: [], get: [] }
  const take = (k) => (route, ctx) => {
    const r = replies[k].shift()
    return r ? r(route, ctx) : json({ detail: `检查脚本没有准备这次的回答（${k}）` }, 500)(route)
  }
  const trialWith = (status, extra) => (route) => { current = { ...current, status: status === 'rejected' ? 'rejected' : 'trialed', trial: flowTrial(status, extra) }; return json(current)(route) }
  const { page, sent, natives, errors, close } = await open('/data/tables', {
    handlers: [
      [/^GET \/datasources$/, (route) => json([...sources, src])(route)],
      [/^GET \/datasources\/check-manage-acc\/schema$/, json({ tables: ['日客流'], summary: '', synced_at: ago(0) })],
      [/^GET \/datasources\/imports\/stg-acc$/, (route) => (replies.get.length ? take('get')(route) : json(current)(route))],
      [/^POST \/datasources\/imports\/[^/]+\/answers$/, take('answers')],
      [/^POST \/datasources\/imports\/[^/]+\/trial$/, take('trial')],
      [/^POST \/datasources\/imports\/[^/]+\/commit$/, take('commit')],
    ],
  })
  const last = (re) => [...sent].reverse().find((s) => re.test(s.key))
  const reopen = async () => {
    await page.locator('[data-source="zz_recipe_acc"] [data-open-staging]').click()
    await page.locator('[data-import-wizard]').waitFor({ timeout: 5000 }).catch(() => {})
  }
  const back = async () => { await page.locator('[data-back-to-recipe]').click(); await page.waitForTimeout(150) }
  const runTrial = async (reply) => {
    replies.trial.push(reply)
    await page.locator('[data-trial-run]').click()
    await page.locator('[data-trial-receipt]').waitFor({ timeout: 4000 }).catch(() => {})
    await page.waitForTimeout(150)
  }

  // ---- 导入模式的问题：单选、无默认，两个选项下方各一句后果
  await reopen()
  const q = page.locator('[data-question="q_mode"]')
  await q.waitFor({ timeout: 4000 }).catch(() => {})
  const details = await q.locator('[data-option-detail]').allInnerTexts()
  check('q_mode 排在最后：问题单选、没有默认选中', await q.locator('input[type="radio"]').count() === 2
        && await q.locator('input[type="radio"]:checked').count() === 0
        && (await page.locator('[data-question]').last().getAttribute('data-question')) === 'q_mode')
  check('……两个选项下方各有一句后果', details.length === 2 && details[0].includes('部分重叠的文件会被拒收') && details[1].includes('此前各期留在历史版本中'),
        details.join(' | '))
  replies.answers.push((route, { body }) => { current = { ...current, answers: Object.fromEntries(Object.entries(body.answers).map(([k, v]) => [k, { reason: null, ...v }])) }; return json(current)(route) })
  await q.locator('[data-option="accumulate"] input').check()
  await until(async () => !!last(/answers$/), 4000)
  check('……选「按期累积（建议）」发 answers', last(/answers$/)?.body?.answers?.q_mode?.value === 'accumulate', JSON.stringify(last(/answers$/)?.body ?? {}))
  const qText = await page.locator('[data-suggestions]').innerText()
  check('……问题区不露机读码（不写 q_mode）', !qText.includes('q_mode'))

  // ---- 新增一期：两期，本期标「本期」，行数分两栏
  await runTrial(trialWith('passed', {
    accumulate: plan(), union_checks: unionChecks,
    receipt: { ...flowTrial('passed').receipt, rows_excluded: [
      { sheet: '客流汇总', reason: 'ignored_rows', rows: [[32, 32]], cells: 3, anchor: '补录（人次）', block: '交叉表' },
      { sheet: '客流汇总', reason: 'total_not_kept', rows: [[28, 30]], cells: 93, anchor: null, block: '交叉表' },
    ] },
  }))
  const dropped = page.locator('[data-trial-receipt] [data-rows-dropped]')
  check('回执里「排除的行」按原因分组，原因写中文、不露枚举值', await dropped.locator('[data-excluded-group]').count() === 2
        && (await dropped.innerText()).includes('按配方忽略的行') && (await dropped.innerText()).includes('「补录（人次）」')
        && !/ignored_rows|total_not_kept/.test(await dropped.innerText()))
  await dropped.locator('[data-excluded-rows="32-32"]').click()
  await page.waitForTimeout(400)
  check('……行号可以点：网格滚到那一行', await inView(page.locator('[data-sheet-grid] [data-cell="B32"]')))
  const accPlan = page.locator('[data-accumulate-plan]')
  const cellOf = async (t, col) => (await accPlan.locator(`[data-plan-table="${t}"] [data-rows-${col}]`).innerText().catch(() => '')).trim()
  check('action=append：[data-accumulate-plan] 有两期，本期标「本期」', await accPlan.locator('[data-plan-part]').count() === 2
        && (await accPlan.locator('[data-plan-part="2026-09-01~2026-09-30"]').innerText()).includes('本期')
        && !(await accPlan.locator('[data-plan-part="2026-08-01~2026-08-31"]').innerText()).includes('本期'))
  check('……「启用后当前版本」一栏写 61、1037、183，「本期」一栏写 30、510、90',
        (await accPlan.locator('[data-col-after]').innerText()).includes('启用后当前版本') && (await accPlan.locator('[data-col-this]').innerText()).includes('本期')
        && await cellOf('日客流', 'after') === '61' && await cellOf('时段客流', 'after') === '1,037' && await cellOf('时段客流_表内合计', 'after') === '183'
        && await cellOf('日客流', 'this') === '30' && await cellOf('时段客流', 'this') === '510' && await cellOf('时段客流_表内合计', 'this') === '90')
  check('……整体核对 U1–U3 列出', await accPlan.locator('[data-plan-checks] [data-check]').count() === 3)
  const planText = await accPlan.innerText()
  check('……计划里不写「并集」「快照」，行数不叫「并集行数」', !P3_FORBIDDEN.test(planText) && planText.includes('新增一期'), planText.slice(0, 80))

  // ---- 重新开始累积：移出的期标「将不在当前版本中」，确认项写「此前」
  await back()
  await runTrial(trialWith('passed', {
    accumulate: plan({ action: 'restart', parts: [SEP], dropped: [AUG], change: 'semantic', semantic: ['表「日客流」列「全日客流」的单位 人次 → 万人次'], union: null }),
    confirm_items: [{ id: 'accumulate_restart', label: '表结构有不兼容的变化：启用后当前版本只含本期，此前 1 期不再出现在当前版本中', required: true, source: 'accumulate' }],
  }))
  check('action=restart：被移出的期标「将不在当前版本中」',
        (await accPlan.locator('[data-plan-part="2026-08-01~2026-08-31"][data-plan-state="dropped"]').innerText().catch(() => '')).includes('将不在当前版本中'))
  await page.locator('[data-to-confirm]').click()
  await page.locator('[data-confirm-list]').waitFor({ timeout: 4000 }).catch(() => {})
  check('……确认项文案含「此前」', (await page.locator('[data-confirm-item="accumulate_restart"]').innerText().catch(() => '')).includes('此前'))
  await dialog(page).getByRole('button', { name: '返回回执' }).click()
  await page.waitForTimeout(150)

  // ---- 部分重叠：拒收，问题列表有重叠说明，没有「下一步」
  await back()
  await runTrial(trialWith('rejected', {
    checks: [], confirm_items: [],
    accumulate: plan({ action: 'rejected', parts: [AUG], overlaps: [AUG], union: null, period: { start: '2026-08-15', end: '2026-09-14' } }),
    problems: [{ code: 'period_overlap', category: 'structure', cells: [], fix_ids: [],
      message: '本期统计期 2026-08-15 至 2026-09-14 与已有的 2026-08-01 至 2026-08-31 部分重叠：按期累积要求各期互不重叠。请检查文件的统计期；如需重新开始累积，可以在数据源卡片的「版本」中启用更早的版本，或在配方中改为每期替换' }],
  }))
  check('action=rejected（部分重叠）：问题列表有重叠说明，没有「下一步：逐条确认」',
        (await page.locator('[data-problem="period_overlap"]').innerText().catch(() => '')).includes('部分重叠')
        && await page.locator('[data-to-confirm]').count() === 0 && await accPlan.locator('[data-plan-overlaps]').count() === 1)

  // ---- 替换该期：确认项在「累积」组，排在差异项之前，不勾不能启用；差异卡的空缺需确认
  await back()
  await runTrial(trialWith('passed', {
    accumulate: plan({ action: 'replace_period', parts: [AUG, SEP], replaces: { ...SEP, import_id: 'imp-9', seq: 2, new: false } }),
    diff: [{ kind: 'period_gap', label: '2026-08-01 至 2026-08-31 与 2026-10-01 至 2026-10-31 之间有空缺', requires_confirm: true, confirm_id: 'diff:period_gap:2026-09-01~2026-09-30' },
      { kind: 'period_replaced', label: '替换已有的一期 2026-09-01 至 2026-09-30', requires_confirm: false }],
    confirm_items: [
      { id: 'unit:日客流.全日客流', label: '「全日客流（人次）」→ 列「全日客流」，单位 人次', required: true, source: 'recipe' },
      { id: 'diff:period_gap:2026-09-01~2026-09-30', label: '各期之间有空缺：2026-09-01 至 2026-09-30', required: true, source: 'diff' },
      { id: 'period_replace:2026-09-01~2026-09-30', label: '替换已有的一期 2026-09-01 至 2026-09-30（第 2 次导入）：启用后该期以本次为准', required: true, source: 'accumulate' },
    ],
  }))
  const gap = page.locator('[data-reupload-diff] [data-diff="period_gap"]')
  check('差异卡的 period_gap 需确认（「各期之间的空缺」）', (await gap.getAttribute('data-requires-confirm').catch(() => null)) === 'diff:period_gap:2026-09-01~2026-09-30'
        && (await gap.innerText().catch(() => '')).includes('各期之间的空缺'))
  check('……被替换的那一期标「将被替换」', (await accPlan.locator('[data-plan-state="replaced"]').innerText().catch(() => '')).includes('将被替换'))
  await page.locator('[data-to-confirm]').click()
  const list = page.locator('[data-confirm-list]')
  await list.waitFor({ timeout: 4000 }).catch(() => {})
  const order = await list.locator('[data-confirm-item]').evaluateAll((els) => els.map((e) => e.getAttribute('data-confirm-item')))
  const ri = order.indexOf('period_replace:2026-09-01~2026-09-30')
  check('replace_period：确认清单里有 period_replace:2026-09-01~2026-09-30，在「累积」组里，排在差异项之前',
        await list.locator('[data-confirm-group="accumulate"] [data-confirm-item="period_replace:2026-09-01~2026-09-30"]').count() === 1
        && ri >= 0 && order.every((id, i) => !id.startsWith('diff:') || i > ri), order.join(' > '))
  for (const id of order.filter((x) => !x.startsWith('period_replace:'))) await list.locator(`[data-confirm-item="${id}"] input`).check()
  check('……不勾它不能启用', await list.locator('[data-commit]').isDisabled())
  await list.locator('[data-confirm-item="period_replace:2026-09-01~2026-09-30"] input').check()
  check('……勾上之后可以启用', await list.locator('[data-commit]').isEnabled())
  await dialog(page).getByRole('button', { name: '返回回执' }).click()
  await page.waitForTimeout(150)

  // ---- 上次接受的理由：只读显示，理由框不预填
  await back()
  await runTrial(trialWith('needs_decision', {
    accumulate: plan(), prior_acceptances: [{ check_id: 'R1', reason: '九月分区数据补录，检查脚本', signed_by: '检查脚本', at: ago(86400_000), import_seq: 2 }],
  }))
  check('伪造 prior_acceptances：回执里可接受的核对旁有「上次接受的理由」',
        (await page.locator('[data-trial-receipt] [data-check="R1"] [data-prior-acceptance="R1"]').innerText().catch(() => '')).includes('上次接受的理由：九月分区数据补录'))
  await page.locator('[data-to-confirm]').click()
  await list.waitFor({ timeout: 4000 }).catch(() => {})
  const acc = list.locator('[data-acceptance="R1"]')
  check('……确认清单里可接受的核对旁有「上次接受的理由」（第 2 次导入、署名（未认证）），理由框是空的',
        (await acc.innerText().catch(() => '')).includes('上次接受的理由') && (await acc.innerText()).includes('第 2 次导入')
        && (await acc.innerText()).includes('署名（未认证）') && (await acc.locator('textarea').inputValue()) === '')
  await tickRequired(list)
  await list.locator('[data-acceptance="R1"] textarea').fill('本期补录，检查脚本')
  await list.locator('[data-acceptance="K1"] textarea').fill('占位符时段本来无数据，检查脚本')
  replies.commit.push(coded(409, 'union_tampered', '试运行生成的数据文件已被改动，请重新试运行'))
  replies.get.push((route) => { current = { ...current, status: 'drafting' }; return json(current)(route) })
  await list.locator('[data-commit]').click()
  await until(async () => await page.locator('[data-import-wizard][data-view="draft"]').count() > 0, 4000)
  await page.waitForTimeout(200)
  check('commit 返回 409 union_tampered：退回起草并提示重新试运行',
        (await page.locator('[data-wizard-notice]').innerText().catch(() => '')).includes('请重新试运行') && await page.locator('[data-trial-run]').count() === 1)
  await runTrial(trialWith('passed', { accumulate: plan() }))
  await page.locator('[data-to-confirm]').click()
  await tickRequired(page.locator('[data-confirm-list]'))
  replies.commit.push(coded(409, 'build_conflict', '导入失败：服务端已有的同版本数据文件与登记的不一致，且无法用本次上传恢复，当前版本未受影响。请联系管理员检查数据目录'))
  await page.locator('[data-confirm-list] [data-commit]').click()
  await until(async () => (await page.locator('[data-confirm-error]').count()) > 0, 4000)
  await page.waitForTimeout(150)
  check('commit 返回 409 build_conflict：留在确认页，显示原话，「确认并启用」禁用',
        await page.locator('[data-confirm-list]').count() === 1 && await page.locator('[data-confirm-list] [data-commit]').isDisabled()
        && (await page.locator('[data-confirm-error]').innerText()).includes('请联系管理员检查数据目录'))
  await page.keyboard.press('Escape')
  await until(async () => await page.locator('[data-import-wizard]').count() === 0, 4000)

  // ---- 已完成页：服务端的同版本数据文件曾被改动，已用本次上传的文件恢复
  current = { ...current, status: 'trialed', trial: flowTrial('passed', { accumulate: plan() }) }
  await reopen()
  await page.locator('[data-to-confirm]').click()
  await tickRequired(page.locator('[data-confirm-list]'))
  replies.commit.push(json({ source: src, import_id: 'imp-10', snapshot_id: sha('6'), build_id: sha('c'), recipe_id: 'rcp-2',
    build_reused: true, unchanged: false, build_restored: true, parts: 2, snapshot_reused: false }, 201))
  await page.locator('[data-confirm-list] [data-commit]').click()
  await page.locator('[data-import-done]').waitFor({ timeout: 4000 }).catch(() => {})
  check('commit 返回 build_restored=true：已完成页有恢复提示',
        (await page.locator('[data-build-restored]').innerText().catch(() => '')).includes('已用本次上传的文件恢复'))
  await dialog(page).getByRole('button', { name: '完成' }).click()
  await page.waitForTimeout(200)

  // ---- 只读进程：写请求回 503 store_unavailable，显示服务端原话、不自动重试
  current = { ...current, status: 'drafting', trial: null }
  await reopen()
  const trials0 = sent.filter((s) => /\/trial$/.test(s.key)).length
  replies.trial.push(coded(503, 'store_unavailable', '数据目录正被另一个 AgentLab 进程使用：上传表格的版本只支持单进程部署，本进程不写入版本存储'))
  await page.locator('[data-trial-run]').click()
  await until(async () => (await page.locator('[data-wizard-notice]').innerText().catch(() => '')).includes('数据目录正被另一个'), 4000)
  await page.waitForTimeout(1200)
  check('任一写请求返回 503 store_unavailable：显示服务端原话，不自动重试',
        (await page.locator('[data-wizard-notice]').innerText().catch(() => '')).includes('数据目录正被另一个 AgentLab 进程使用')
        && sent.filter((s) => /\/trial$/.test(s.key)).length === trials0 + 1)
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据 · 表格：配方对照与重新起草', async () => {
  const src = recipeSource('check-manage-redraft', 'zz_recipe_redraft', {
    open_staging: { id: 'stg-redraft', kind: 'redraft', status: 'drafting', created_at: ago(60_000) } })
  const ignoreRecipe = JSON.parse(JSON.stringify(FLOW_RECIPE))
  ignoreRecipe.sheets[0].blocks[0].ignore_rows = [{ label: '补录（人次）', reason: '表下补录的一行，检查脚本' }]
  let current = flowStaging({
    id: 'stg-redraft', kind: 'redraft', status: 'drafting', source: { id: src.id, name: src.name, exists: true, import_mode: 'recipe' },
    draft: null, cards: [], questions: [], answers: {}, ai: { offered: false, available: true, reason: '', model: '', provider: '' },
    recipe: ignoreRecipe, draft_problems: [], edits: [], fixes: [], answers_dropped: [],
  })
  const aligned = { ...JSON.parse(JSON.stringify(FLOW_RECIPE)), mode: 'accumulate' }
  // 与服务端 compare_recipes 同一口径：单位变化另在 units_changed（排最前），tables[].breaking 是这张表其余破坏性变化的
  // 原话；占位符含义是表级变化（没有列），只出现在原话里。同一张表既改了单位、又改了类型和占位符含义
  const unitCompare = emptyCompare({
    tables: [{ name: '日客流', status: 'changed', columns: [
      { name: '分区丙', status: 'added', old: null, new: { type: 'INTEGER', unit: '人次', source: '分区丙（人次）' }, changes: [] },
      { name: '全日客流', status: 'changed', old: { type: 'INTEGER', unit: '人次' }, new: { type: 'INTEGER', unit: '万人次' }, changes: ['unit'] },
      { name: '分区甲', status: 'changed', old: { type: 'INTEGER', unit: '人次', source: '分区甲（人次）' },
        new: { type: 'REAL', unit: '人次', source: '分区甲（人次）' }, changes: ['type'] },
    ], grain: { old: ['日期'], new: ['日期'], changed: false }, kind: { old: 'data', new: 'data' },
    breaking: ['列「分区甲」类型 INTEGER → REAL', '值列的占位符「—」含义 无数据 → 不适用'] }],
    // 重新起草换了分段：新增的分段在 labels.added 里给全部标签，去掉的分段在 labels.removed 里给全部标签（compare_recipes
    // 对空的一边自然给出全部，WP-3 评审修复）。界面要把标题和标签写进同一条，不能只剩一个分段 id
    segments: [
      { id: '夜间', status: 'added', title: { old: null, new: '夜间时段客流（人次）' },
        labels: { added: ['22-23', '23-24'], removed: [] }, ignore: { added: [], removed: [] } },
      { id: '旧分区', status: 'removed', title: { old: '旧分区客流（人次）', new: null },
        labels: { added: [], removed: ['分区丁（人次）', '分区戊（人次）'] }, ignore: { added: [], removed: [] } },
    ],
    breaking: true, units_changed: [{ table: '日客流', column: '全日客流', old: '人次', new: '万人次' }],
  })
  const replies = { redraft: [], recipe: [], trial: [] }
  const take = (k) => (route, ctx) => {
    const r = replies[k].shift()
    return r ? r(route, ctx) : json({ detail: `检查脚本没有准备这次的回答（${k}）` }, 500)(route)
  }
  const { page, sent, natives, errors, close } = await open('/data/tables', {
    handlers: [
      [/^GET \/datasources$/, (route) => json([...sources, src])(route)],
      [/^GET \/datasources\/check-manage-redraft\/schema$/, json({ tables: ['日客流'], summary: '', synced_at: ago(0) })],
      [/^GET \/datasources\/imports\/stg-redraft$/, (route) => json(current)(route)],
      [/^POST \/datasources\/imports\/[^/]+\/redraft-rules$/, take('redraft')],
      [/^PUT \/datasources\/imports\/[^/]+\/recipe$/, take('recipe')],
      [/^POST \/datasources\/imports\/[^/]+\/trial$/, take('trial')],
    ],
  })
  const puts = () => sent.filter((s) => /^PUT /.test(s.key))
  await page.locator('[data-source="zz_recipe_redraft"] [data-open-staging]').click()
  await page.locator('[data-recipe-drawer-toggle]').waitFor({ timeout: 5000 }).catch(() => {})
  await page.locator('[data-recipe-drawer-toggle]').click()
  const panel = page.locator('[data-recipe-panel]')
  await panel.waitFor({ timeout: 4000 }).catch(() => {})
  check('RecipePanel 里有「导入模式」单选（按期累积 / 每期替换），选项下方写后果',
        await panel.locator('[data-recipe-mode] [data-mode-option] input[type="radio"]').count() === 2
        && (await panel.locator('[data-recipe-mode]').innerText()).includes('按期累积') && (await panel.locator('[data-recipe-mode]').innerText()).includes('每期替换')
        && await panel.locator('[data-recipe-mode] [data-option-detail]').count() === 2)
  const rule = panel.locator('[data-ignore-rule]')
  check('……带 ignore_rows 的配方：忽略规则以只读列表显示（不是输入框）', await rule.count() === 1
        && (await rule.innerText()).includes('补录（人次）') && await rule.locator('input, textarea').count() === 0)

  // ---- 规则起草不完整：没有完整配方，对照也是 null。只说为什么不能采用，不写「配方没有变化」
  replies.redraft.push(json({ draft: { recipe: null, complete: false, origin: 'rules', cards: [], questions: [],
    failures: ['工作表「客流汇总」：没有找到日期表头（检查脚本）'] }, aligned_recipe: null, alignment: [], compare: null }))
  await page.locator('[data-redraft-rules]').click()
  const rc = page.locator('[data-redraft-compare]')
  await rc.locator('[data-redraft-incomplete]').waitFor({ timeout: 4000 }).catch(() => {})
  const incText = await rc.innerText().catch(() => '')
  check('规则起草不完整（aligned_recipe、compare 都是 null）：写明无法采用和原因，不写「配方没有变化」，「采用」禁用',
        incText.includes('无法采用') && incText.includes('没有找到日期表头') && !incText.includes('配方没有变化')
        && await rc.locator('[data-compare-none]').count() === 0 && await rc.locator('[data-redraft-adopt]').isDisabled(), incText)

  // ---- 按规则重新起草：对照、名字对齐；「采用」先确认，取消不发请求
  replies.redraft.push(json({ draft: { recipe: aligned, complete: true, origin: 'rules', cards: [], questions: [], failures: [] },
    aligned_recipe: aligned, alignment: ['表「客流汇总_按日」对齐为现行配方的「日客流」'], compare: unitCompare }))
  await page.locator('[data-redraft-rules]').click()
  await until(async () => (await rc.innerText().catch(() => '')).includes('对齐为现行配方的「日客流」'), 4000)
  check('redraft 暂存区里点 [data-redraft-rules] 发 POST …/redraft-rules，显示对照和名字对齐',
        sent.filter((s) => /redraft-rules$/.test(s.key)).length === 2 && (await rc.innerText().catch(() => '')).includes('对齐为现行配方的「日客流」'))
  check('……重新起草的对照里，新增分段写出标题和全部标签', (await rc.locator('[data-change-kind="segment_added"]').innerText().catch(() => ''))
        .includes('新增分段「夜间」，标题「夜间时段客流（人次）」，标签：「22-23」、「23-24」'))
  await rc.locator('[data-redraft-adopt]').click()
  const ask = page.locator('[role="dialog"]', { hasText: '采用重新起草的配方？' })
  await ask.waitFor({ timeout: 4000 }).catch(() => {})
  check('「采用」先弹确认框，文案含「将被替换」', (await ask.innerText().catch(() => '')).includes('将被替换'))
  await ask.getByRole('button', { name: '取消' }).click()
  await page.waitForTimeout(250)
  check('……取消则不发请求', puts().length === 0)
  replies.recipe.push((route, { body }) => { current = { ...current, recipe: body.recipe, recipe_origin: 'rules_redraft' }; return json(current)(route) })
  await rc.locator('[data-redraft-adopt]').click()
  await ask.waitFor({ timeout: 4000 }).catch(() => {})
  await ask.getByRole('button', { name: '采用' }).click()
  await until(async () => puts().length === 1, 4000)
  check('……确认后发 PUT …/recipe，body 是 {recipe: aligned_recipe, origin: "rules_redraft"}',
        JSON.stringify(puts()[0]?.body) === JSON.stringify({ recipe: aligned, origin: 'rules_redraft' }), JSON.stringify(puts()[0]?.body ?? {}).slice(0, 160))

  // ---- 采用后的试运行：对照里单位变化排第一；确认清单里有 redraft_adopted
  replies.trial.push((route) => { current = { ...current, status: 'trialed', trial: flowTrial('passed', {
    recipe_compare: unitCompare,
    confirm_items: [...FLOW_CONFIRMS.slice(0, 3), { id: 'redraft_adopted', label: '采用按规则重新起草的配方：表「客流汇总_按日」对齐为「日客流」', required: true, source: 'edit' }],
  }) }; return json(current)(route) })
  await page.locator('[data-trial-run]').click()
  const cmp = page.locator('[data-wizard-side] [data-recipe-compare]')
  await cmp.waitFor({ timeout: 4000 }).catch(() => {})
  const kinds = await cmp.locator('[data-change-kind]').evaluateAll((els) => els.map((e) => e.getAttribute('data-change-kind')))
  check('伪造 recipe_compare（单位变了一列、新增一列）：[data-recipe-compare] 里单位变化排第一',
        kinds[0] === 'unit' && kinds.includes('column_added') && (await cmp.locator('[data-change-kind="unit"]').innerText()).includes('人次 → 万人次'), kinds.join(','))
  const lines = (await cmp.locator('[data-change-kind]').allInnerTexts()).map((x) => x.trim())
  check('……同一张表另有类型变化和占位符含义变化：占位符含义这条破坏性原话照样列出（标破坏性），类型变化、单位变化各只列一次',
        lines.some((x) => x.includes('占位符「—」含义 无数据 → 不适用') && x.includes('日客流'))
        && await cmp.locator('[data-breaking]', { hasText: '占位符「—」含义' }).count() === 1
        && lines.filter((x) => x.includes('分区甲') && x.includes('REAL')).length === 1
        && lines.filter((x) => x.includes('万人次')).length === 1, lines.join(' | '))
  const segAdded = await cmp.locator('[data-change-kind="segment_added"]').allInnerTexts()
  const segRemoved = await cmp.locator('[data-change-kind="segment_removed"]').allInnerTexts()
  check('新增的分段：同一条里写出标题和全部标签（labels.added），不另摊一条「加入标签」',
        segAdded.length === 1 && segAdded[0].includes('新增分段「夜间」') && segAdded[0].includes('标题「夜间时段客流（人次）」')
        && segAdded[0].includes('标签：「22-23」、「23-24」')
        && await cmp.locator('[data-change-kind="labels"]').count() === 0, [...segAdded, ...lines].join(' | '))
  check('……去掉的分段：写出原标题和原有的全部标签（labels.removed）',
        segRemoved.length === 1 && segRemoved[0].includes('去掉分段「旧分区」') && segRemoved[0].includes('原标题「旧分区客流（人次）」')
        && segRemoved[0].includes('原有标签：「分区丁（人次）」、「分区戊（人次）」'), segRemoved.join(' | '))
  await page.locator('[data-to-confirm]').click()
  await page.locator('[data-confirm-list]').waitFor({ timeout: 4000 }).catch(() => {})
  check('伪造采用后的试运行：确认清单里有 redraft_adopted', await page.locator('[data-confirm-list] [data-confirm-item="redraft_adopted"]').count() === 1)
  const text = await wizardText(page)
  check('……新增的文字不含「快照」「并集」「构建」「锚点」「放行」「钉」，不露机读码', !P3_FORBIDDEN.test(text) && !/redraft_adopted|rules_redraft/.test(text))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
})

await section('数据 · 表格：改配方后回答保留', async () => {
  const src = recipeSource('check-manage-carry', 'zz_recipe_carry', {
    open_staging: { id: 'stg-carry', kind: 'redraft', status: 'drafting', created_at: ago(60_000) } })
  let current = flowStaging({
    id: 'stg-carry', kind: 'redraft', status: 'drafting', source: { id: src.id, name: src.name, exists: true, import_mode: 'recipe' },
    ai: { offered: false, available: true, reason: '', model: '', provider: '' }, recipe: FLOW_RECIPE,
    answers: { 'q_placeholder:·': { value: 'null', reason: null } }, draft_problems: [], fixes: [], answers_dropped: [],
    edits: [fixEdit()], recipe_sha256: sha('b'),
  })
  const replies = { recipe: [], trial: [] }
  const take = (k) => (route, ctx) => {
    const r = replies[k].shift()
    return r ? r(route, ctx) : json({ detail: `检查脚本没有准备这次的回答（${k}）` }, 500)(route)
  }
  const { page, sent, natives, errors, close } = await open('/data/tables', {
    handlers: [
      [/^GET \/datasources$/, (route) => json([...sources, src])(route)],
      [/^GET \/datasources\/check-manage-carry\/schema$/, json({ tables: ['日客流'], summary: '', synced_at: ago(0) })],
      [/^GET \/datasources\/imports\/stg-carry$/, (route) => json(current)(route)],
      [/^PUT \/datasources\/imports\/[^/]+\/recipe$/, take('recipe')],
      [/^POST \/datasources\/imports\/[^/]+\/trial$/, take('trial')],
    ],
  })
  const puts = () => sent.filter((s) => /^PUT /.test(s.key))
  await page.locator('[data-source="zz_recipe_carry"] [data-open-staging]').click()
  await page.locator('[data-edits]').waitFor({ timeout: 5000 }).catch(() => {})
  check('有未被覆盖的修改：[data-edits] 有「撤销上一次修改」', await page.locator('[data-edits] [data-edit-undo]').count() === 1)
  await page.locator('[data-recipe-drawer-toggle]').click()
  const panel = page.locator('[data-recipe-panel]')
  await panel.waitFor({ timeout: 4000 }).catch(() => {})
  replies.recipe.push(async (route, { body }) => {
    current = { ...current, recipe: body.recipe, answers: { 'q_placeholder:·': { value: 'null', reason: null } },
      answers_dropped: [{ id: 'q_relation:F2', text: '系统发现的关系 F2 要登记吗', reason: 'changed' },
        { id: 'q_relation:F9', text: '「—」是否表示无数据', reason: 'gone' }],
      edits: [fixEdit({ superseded: true, undoable: false })] }
    // 晚一点返回：好在这次保存还没回来时再改一处（配方面板把它排在后面，等这次返回后接着发）
    await new Promise((r) => setTimeout(r, 1200))
    return json(current)(route)
  })
  replies.recipe.push((route, { body }) => { current = { ...current, recipe: body.recipe }; return json(current)(route) })
  await panel.locator('[data-field="/tables/0/units/分区甲"]').selectOption('人')
  const ask = page.locator('[role="dialog"]', { hasText: '保存对配方的修改？' })
  await ask.waitFor({ timeout: 4000 }).catch(() => {})
  check('PUT 之前有未被覆盖的修改：先确认（写明含已做的 1 项修改、将被替换），还没发 PUT',
        (await ask.innerText().catch(() => '')).includes('含已做的 1 项修改') && (await ask.innerText()).includes('将被替换') && puts().length === 0)
  await ask.getByRole('button', { name: '保存' }).click()
  await until(async () => puts().length === 1, 4000)
  // 这次保存还没返回：再改一处。返回之后修改已被覆盖，排在后面的这一批不该再问一次（再问时点取消，这一处就丢了）
  await panel.locator('[data-field="/tables/0/units/分区乙"]').selectOption('人')
  await until(async () => puts().length === 2 || (await ask.count()) > 0, 6000)
  await page.waitForTimeout(200)
  const askedAgain = await ask.count()
  const put2 = puts()[1]?.body?.recipe?.tables?.[0]?.units ?? {}
  check('保存还没返回时又改了一处：返回后接着保存这一处，不再弹「保存对配方的修改？」，第二次 PUT 带着两处修改',
        askedAgain === 0 && puts().length === 2 && put2.分区乙 === '人' && put2.分区甲 === '人',
        `再次确认 ${askedAgain} 次，PUT ${puts().length} 次，${JSON.stringify(put2)}`)
  if (askedAgain) {
    // 旧行为会再问一次：点「保存」让后面的检查接着走
    await ask.getByRole('button', { name: '保存' }).click()
    await until(async () => puts().length === 2, 4000)
  }
  await page.waitForTimeout(300)
  check('PUT 返回的 answers 里保留了已选项：该选项仍是选中状态',
        await page.locator('[data-question="q_placeholder:·"] [data-option="null"] input').isChecked())
  const dropped = page.locator('[data-answers-dropped]')
  const lines = await dropped.locator('[data-answers-dropped-reason]').allInnerTexts()
  check('返回 answers_dropped 时出现 [data-answers-dropped]，changed 和 gone 分两句，列出问题文字（不露 id）',
        lines.length === 2 && lines[0].includes('需要重新回答') && lines[0].includes('系统发现的关系 F2 要登记吗')
        && lines[1].includes('已不适用') && lines[1].includes('「—」是否表示无数据') && !(await dropped.innerText()).includes('q_relation'), lines.join(' | '))
  const editsBox = page.locator('[data-edits]')
  check('PUT 之后 edits 全部 superseded：[data-edits] 里标「已被覆盖」，没有「撤销上一次修改」',
        (await editsBox.locator('[data-edit-superseded]').innerText().catch(() => '')).includes('已被覆盖') && await editsBox.locator('[data-edit-undo]').count() === 0)
  replies.recipe.push((route, { body }) => { current = { ...current, recipe: body.recipe }; return json(current)(route) })
  await panel.locator('[data-field="/tables/0/units/全日客流"]').selectOption('人')
  await until(async () => puts().length === 3, 4000)
  check('……修改都已被覆盖：之后再改配方不再弹确认', await page.locator('[role="dialog"]', { hasText: '保存对配方的修改？' }).count() === 0
        && puts().length === 3)
  replies.trial.push((route) => { current = { ...current, status: 'trialed', trial: flowTrial('passed', { confirm_items: FLOW_CONFIRMS }) }; return json(current)(route) })
  await page.locator('[data-trial-run]').click()
  await page.locator('[data-to-confirm]').waitFor({ timeout: 4000 }).catch(() => {})
  await page.locator('[data-to-confirm]').click()
  await page.locator('[data-confirm-list]').waitFor({ timeout: 4000 }).catch(() => {})
  check('……确认清单里没有 fix:*', await page.locator('[data-confirm-list] [data-confirm-item^="fix:"]').count() === 0
        && await page.locator('[data-confirm-list] [data-confirm-item]').count() > 0)
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
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
