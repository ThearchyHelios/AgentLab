// 管理页（工具 / 知识 / 数据 / 设置）的交互回归。
//
// 这几页的毛病都不在"渲染不出来"，而在交互的细处：写操作失败一声不响、中文
// 输入法选词的回车把半句话写进长期记忆、原生 confirm 不说后果、测完连接只剩
// 4 秒 toast、Oracle 的 service_name 绑错了字段、传完表格回显一帧都看不到……
// 页面级的 check-ui 走的都是正常路径，这些一个字都不会说。
//
// 不写库：GET 放行到后端（读真实数据），其余一律 page.route 拦下伪造——断言
// 请求体对不对，再回一个像样的响应。所以对哪个后端跑都不会改数据；但照规矩
// 还是只对沙箱跑：
//   AGENTLAB_WEB=http://localhost:5373 AGENTLAB_API=http://localhost:8100/api node scripts/check-manage.mjs
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
 * 开一页：GET 放行，写请求交给 handlers（按「METHOD 路径正则」匹配），没配的一律
 * 拦成 503——既不写库，也顺带检验「写失败有反馈」。所有原生对话框都算失败。
 */
async function open(path, { handlers = [], theme = THEME, viewport = { width: 1280, height: 860 } } = {}) {
  const ctx = await browser.newContext({ viewport, colorScheme: theme, timezoneId: 'Asia/Shanghai' })
  await ctx.addInitScript((t) => {
    try { localStorage.setItem('agentlab.theme', t); localStorage.removeItem('agentlab.health') } catch { /* noop */ }
  }, theme)
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
  return { page, ctx, sent, errors, natives, close: () => ctx.close() }
}
// 页内换地址：App 会拿后端 settings 里的主题再覆盖一次，跟 open() 一样钉回来
const goto = async (page, path) => {
  await page.goto(`${WEB}${path}`, { waitUntil: 'networkidle' })
  await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), THEME)
  await page.waitForTimeout(300)
}
const json = (data, status = 200) => (route) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) })
const delayed = (ms, data, status = 200) => async (route) => { await new Promise((r) => setTimeout(r, ms)); return json(data, status)(route) }
const shot = async (page, name) => { if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png` }) }
const text = (page) => page.locator('main').innerText()
const dialog = (page) => page.locator('[role="dialog"]').last()
const composingEnter = (locator) => locator.evaluate((el) => {
  el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 229, isComposing: true, bubbles: true, cancelable: true }))
})

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

console.log('=== 页面骨架：页头、标签、地址 ===')
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

console.log('\n=== 设置 · 模型接入 ===')
{
  const p0 = providers[0]
  const { page, sent, natives, errors, close } = await open('/settings/providers', {
    handlers: [
      [/^POST \/providers\/[^/]+\/test$/, delayed(300, { ok: true, latency_ms: 128, model: p0?.default_model ?? 'm', reply: 'pong' })],
      [/^POST \/providers\/test$/, json({ ok: false, error: '测试没通过：连不上对方的服务', hint: '核对地址和端口，确认服务已经启动', detail: 'OpenAIConnectionError: Connection error.' })],
      [/^POST \/providers\/models$/, json({ ok: true, models: ['qwen-max', 'qwen-plus'], url: 'x' })],
    ],
  })
  if (p0) {
    const card = page.locator(`[data-provider="${p0.name}"]`)
    check('卡片上的状态点说的是连通，没测过写「未测试」', (await card.locator('[data-health]').getAttribute('data-health')) === 'idle')
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
    check('测完留在卡片上，写明是什么时候测的：已连通 · 128 ms · 刚测过', /已连通.*128 ms.*刚测过/.test(pill), pill)
    await shot(page, 'providers-tested')
    await page.getByRole('tab', { name: '运行环境' }).click()
    await page.waitForTimeout(300)
    await page.getByRole('tab', { name: '模型接入' }).click()
    await page.waitForTimeout(300)
    const again = await page.locator(`[data-provider="${p0.name}"] [data-health]`).innerText()
    check('切走再回来，测试结果还在', again.includes('已连通'), again)
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
  check('测试失败的原因和怎么办留在弹窗里', body.includes('连不上对方的服务') && body.includes('核对地址'))
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
}

console.log('\n=== 设置 · 偏好 ===')
{
  const settings = await get('/settings')
  const { page, sent, natives, close } = await open('/settings/prefs', {
    handlers: [[/^PUT \/settings$/, (route, { body }) => json({ ...settings, ...body.values })(route)]],
  })
  // 选一个和这次要截图的主题一致的值（浅色跑选「跟随系统」，上下文的 colorScheme
  // 是浅色）；它已经是选中的就先绕一下别的，点选中项不会发请求
  const [label, value] = THEME === 'dark' ? ['深色', 'dark'] : ['跟随系统', 'system']
  const radio = page.getByRole('radio', { name: label })
  if ((await radio.getAttribute('aria-checked')) === 'true') {
    await page.getByRole('radio', { name: '浅色' }).click()
    await page.waitForTimeout(300)
  }
  await radio.click()
  await page.waitForTimeout(400)
  const put = sent.filter((s) => s.key === 'PUT /settings').at(-1)
  check('主题选中即保存，只 PUT ui 一组', !!put && Object.keys(put.body?.values ?? {}).join() === 'ui' && put.body.values.ui.theme === value,
        JSON.stringify(put?.body ?? {}).slice(0, 100))
  check('主题旁显示「已保存」', (await page.locator('[data-theme-save]').innerText()).includes('已保存'))
  await page.waitForTimeout(1600)
  check('……约 1.6 秒后淡出，不一直挂着', await page.getByText('已保存', { exact: true }).count() === 0
        && await page.locator('[data-theme-save="saved"]').count() === 0)
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
  await dialog(page).getByRole('button', { name: '留下来保存' }).click()
  await page.waitForTimeout(200)
  check('选「留下」就还在偏好页', page.url().endsWith('/settings/prefs'), page.url())

  await page.getByRole('button', { name: /保存设置/ }).click()
  await page.waitForTimeout(400)
  const put2 = sent.filter((s) => s.key === 'PUT /settings').at(-1)
  check('保存设置只 PUT run 一组', Object.keys(put2?.body?.values ?? {}).join() === 'run', JSON.stringify(put2?.body ?? {}).slice(0, 100))
  check('保存后说「已保存」', (await page.locator('[data-prefs-bar]').innerText().catch(() => '')).includes('已保存'))
  check('署名跟着保存写进本机', await page.evaluate(() => localStorage.getItem('agentlab_actor')) === '检查脚本')
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  await close()

  // 保存失败要看得见（以前 pageerror 一条，界面毫无变化）
  const failing = await open('/settings/prefs')
  await failing.page.locator('#pref-actor').fill('x')
  await failing.page.getByRole('button', { name: /保存设置/ }).click()
  await failing.page.waitForTimeout(600)
  check('保存失败：条上写「没存上」', (await failing.page.locator('[data-prefs-bar]').innerText()).includes('没存上'))
  check('保存失败不抛未捕获异常', failing.errors.length === 0, failing.errors[0] ?? '')
  await failing.close()
}

console.log('\n=== 数据 · 数据库 ===')
{
  const dbs = sources.filter((s) => !/[\\/]uploads[\\/]tables[\\/]/.test(s.database ?? ''))
  const first = dbs.find((s) => s.table_count > 0) ?? dbs[0]
  const oracle = dbs.find((s) => s.kind === 'oracle')
  // 一个配置里的 schema 探不出对象的假库：只有这种库才给「换个 schema 探查」。
  // 顺带是个免密库（has_password:false），验证密码不再拦保存
  const empty = {
    id: 'check-manage-empty', name: 'zz_probe', kind: 'postgres', host: '10.0.0.9', port: null, database: 'mes',
    username: 'reader', options: { schema: 'ods' }, readonly: true, description: '检查脚本的假库', enabled: true,
    password_masked: '', has_password: false, table_count: 0, schema_synced_at: null,
    schema_error: 'NoSuchTableError: ods', available_schemas: ['mes', 'ods', 'public'], tools: ['db_query__zz_probe'],
  }
  const probed = { ...empty, table_count: 3, schema_error: '', schema_synced_at: new Date().toISOString() }
  const { page, sent, natives, errors, close } = await open('/data/databases', {
    handlers: [
      [/^GET \/datasources$/, json([...sources, empty])],
      [/^GET \/datasources\/check-manage-empty\/schema$/, json({ tables: ['mes.work_order', 'mes.line', 'mes.shift'], summary: '', synced_at: probed.schema_synced_at })],
      [/^POST \/datasources\/check-manage-empty\/introspect$/, (route, { url }) =>
        json(url.searchParams.get('schema') === 'mes' ? probed : empty)(route)],
      [/^PATCH \/datasources\/check-manage-empty$/, (route, { body }) => json({ ...empty, ...body, options: body?.options ?? empty.options })(route)],
      [/^POST \/datasources\/[^/]+\/test$/, delayed(250, { ok: true, elapsed_ms: 42, url: 'x' })],
      [/^POST \/datasources\/test$/, json({ ok: false, error: '连不上：认证失败', hint: '核对用户名和密码', detail: 'ORA-01017' })],
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
    const addrs = await page.locator('article[data-source] .mono').allInnerTexts()
    check('地址不留尾巴冒号', !addrs.some((a) => /:\s*$/.test(a)), addrs.join(' | ').slice(0, 160))
    await card.getByRole('button', { name: /测连接/ }).click()
    await page.waitForTimeout(40)
    const spoken0 = await card.locator('[role="status"]').first().innerText().catch(() => '')
    await page.waitForTimeout(160)
    const spoken1 = await card.locator('[role="status"]').first().innerText().catch(() => '')
    check('测连接期间播报区只说一句「正在测连接」，不跟着计时一跳一跳地念',
          spoken0 === '正在测连接' && spoken1 === spoken0, `${spoken0} / ${spoken1}`)
    check('……看得见的胶囊不是播报区（里面有计时）',
          (await card.locator('[data-health]').first().getAttribute('aria-hidden')) === 'true'
          && (await card.locator('[data-health]').first().getAttribute('role')) === null)
    await page.waitForTimeout(400)
    const pill = await card.locator('[data-health]').first().innerText()
    check('测连接的结果留在卡片上：已连通 · 42 ms', /已连通.*42 ms/.test(pill), pill)
    const spoken = await card.locator('[role="status"]').first().innerText()
    check('……播报区说结果和钟点，不说「几分钟前」', /^已连通 42 ms，\d{1,2}:\d{2} 测的$/.test(spoken), spoken)
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
      check('点表名懒加载出逐列信息', cols > 0, `${cols} 列`)
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

    // 好好的库不给「换个 schema 探查」：后端探查会落进缓存，换着探就把助手看到的结构换掉了
    check('结构正常的库不给「换个 schema 探查」', await card.locator('[data-probe-schema]').count() === 0)

    await card.getByRole('button', { name: `删除数据源 ${first.name}` }).click()
    const del = dialog(page)
    const delText = await del.innerText()
    check('删除数据源：说明后果（工具、工作流）', delText.includes('工具') && delText.includes('工作流'))
    check('删除数据源：要照抄名字才能确认', await del.getByRole('button', { name: '删除数据源' }).isDisabled())
    await del.getByRole('button', { name: '取消' }).click()
    check('取消后没有发删除', !sent.some((s) => s.key.startsWith('DELETE')))
  }

  {
    // 配置里的 schema 探不出东西：才给「换个 schema 探查」
    const ec = page.locator(`[data-source="${empty.name}"]`)
    check('探查失败的库给「换个 schema 探查」', await ec.locator('[data-probe-schema]').count() === 1)
    await ec.locator('[data-probe-schema]').click()
    const pd = dialog(page)
    const pdText = await pd.innerText()
    check('……先说清楚：探到的结构会换掉缓存，不写进配置就换回来', pdText.includes('换掉') && pdText.includes('换回「ods」'), pdText.slice(0, 120))
    await pd.locator('input').fill('mes')
    await pd.getByRole('button', { name: '探查', exact: true }).click()
    await page.waitForTimeout(500)
    const probeReq = sent.filter((s) => s.key.endsWith('/introspect')).at(-1)
    check('……请求带上指定的 schema', !!probeReq?.url.includes('schema=mes'), probeReq?.url ?? '')
    const ask = dialog(page)
    const askText = await ask.innerText().catch(() => '')
    check('……探到了再问要不要写进配置，并举出探到的表名', askText.includes('mes') && askText.includes('work_order'), askText.slice(0, 160))
    await shot(page, 'datasource-probe-schema')
    const before = sent.filter((s) => s.key.endsWith('/introspect')).length
    await ask.getByRole('button', { name: '不改，换回 ods' }).click()
    await page.waitForTimeout(500)
    const restore = sent.filter((s) => s.key.endsWith('/introspect')).slice(before)
    check('……不改：马上按配置里的 schema 重探一次，缓存和配置对得上',
          restore.length === 1 && !restore[0].url.includes('schema='), restore.map((r) => r.url).join(' | '))
    check('……不改就不发 PATCH', !sent.some((s) => s.key.startsWith('PATCH /datasources')))
    check('……卡片回到配置里那份（探查失败、0 个对象）', (await ec.innerText()).includes('上次探查失败'))

    // 失败框里的候选 chip：探到了选「改」就写进配置
    await ec.getByRole('button', { name: '用 mes 探查' }).click()
    await page.waitForTimeout(500)
    await dialog(page).getByRole('button', { name: '改成 mes' }).click()
    await page.waitForTimeout(400)
    const patch = sent.filter((s) => s.key === 'PATCH /datasources/check-manage-empty').at(-1)
    check('……选「改成 mes」：PATCH 写进 options.schema', patch?.body?.options?.schema === 'mes', JSON.stringify(patch?.body ?? {}))

    // 免密库：密码不拦保存
    await ec.getByRole('button', { name: '编辑' }).click()
    const ed = dialog(page)
    await ed.getByLabel('说明', { exact: true }).fill('检查脚本的假库（改过说明）')
    const saveBtn = ed.getByRole('button', { name: '保存' })
    check('免密库改说明：保存可点，不再要「还缺：密码」',
          await saveBtn.isEnabled() && !((await saveBtn.getAttribute('title')) ?? '').includes('密码'),
          (await saveBtn.getAttribute('title')) ?? '')
    check('……密码框下软提示「只有免密登录的库才能这样连」', (await ed.innerText()).includes('免密登录'))
    await shot(page, 'datasource-nopw-edit')
    if (await saveBtn.isEnabled()) await saveBtn.click()
    else await ed.getByRole('button', { name: '取消' }).click()
    await page.waitForTimeout(400)
    const saved = sent.filter((s) => s.key === 'PATCH /datasources/check-manage-empty').at(-1)
    check('……保存时不带 password（不动它）', !!saved && !('password' in (saved.body ?? {})) && saved.body?.description?.includes('改过说明'),
          JSON.stringify(saved?.body ?? {}).slice(0, 120))
  }

  if (oracle) {
    await page.locator(`[data-source="${oracle.name}"]`).getByRole('button', { name: '编辑' }).click()
    const dlg = dialog(page)
    const target = dlg.locator('#ds-oracle-target')
    const svc = oracle.options?.service_name ?? oracle.database ?? ''
    check('Oracle 编辑框回显 service_name（以前绑在 database 上，显示为空）', (await target.inputValue()) === svc, `「${await target.inputValue()}」应为「${svc}」`)
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
    check('弹窗里先测连接：带 id、只写 options.sid、不写 database',
          req?.body?.id === oracle.id && req?.body?.options?.sid === 'ORCL' && !('service_name' in (req?.body?.options ?? {})) && !('database' in (req?.body ?? {})),
          JSON.stringify(req?.body ?? {}).slice(0, 160))
    check('测试失败的原因留在弹窗里', (await dlg.innerText()).includes('核对用户名和密码'))
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
}

console.log('\n=== 数据 · 表格 ===')
{
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
    handlers: [
      [/^POST \/datasources\/upload$/, delayed(400, fake, 201)],
      // 传上来的表只有一两张，卡片会自动摊开结构：假的那张也得有结构可取
      [/^GET \/datasources\/check-manage-fake\/schema$/, (route, { url }) => json(url.searchParams.get('table')
        ? { table: 'sales_demo', detail: '表 sales_demo\n  2026-01  REAL\n  region  TEXT\n  Unnamed: 2  TEXT' }
        : { tables: ['sales_demo'], summary: '', synced_at: fake.source.schema_synced_at })(route)],
    ],
  })
  await page.getByRole('button', { name: /传表格/ }).first().click()
  const dlg = dialog(page)
  await dlg.locator('input[type="file"]').setInputFiles({ name: 'sales_demo.csv', mimeType: 'text/csv', buffer: Buffer.from('2026-01,region\n1,east\n') })
  check('文件名自动变成数据源名', (await dlg.locator('input.mono').first().inputValue()) === 'sales_demo')
  await dlg.getByRole('button', { name: /导入/ }).click()
  await page.waitForTimeout(100)
  check('导入中按钮写已用时间', /导入中/.test(await dlg.getByRole('button', { name: /导入中/ }).innerText().catch(() => '')))
  await page.waitForTimeout(700)
  const res = await dialog(page).innerText()
  check('导入完弹窗不关，停在列名和类型上', res.includes('region') && res.includes('TEXT'))
  check('像数据的列名标黄并提示改表头行号', await dialog(page).locator('[data-suspicious]').count() === 2 && res.includes('第 2 行'))
  check('两个出口：列名对了 · 完成 / 表头不对 · 改行号重传',
        await dialog(page).getByRole('button', { name: '列名对了 · 完成' }).count() === 1
        && await dialog(page).getByRole('button', { name: /表头不对/ }).count() === 1)
  check('后台列表里已经有了这张表', await page.locator('[data-source="sales_demo"]').count() === 1)
  await shot(page, 'table-upload-result')
  await dialog(page).getByRole('button', { name: /表头不对/ }).click()
  await page.waitForTimeout(200)
  check('改行号重传：回到表单，行号 +1', (await dialog(page).locator('input[type="number"]').inputValue()) === '2')
  await dialog(page).getByRole('button', { name: '取消' }).click()
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('上传只发了一次', sent.filter((s) => s.key === 'POST /datasources/upload').length === 1)
  await close()
}

console.log('\n=== 知识库 ===')
{
  const { page, sent, natives, errors, close } = await open('/knowledge/kb', {
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

  await page.locator('input[data-kb-upload]').setInputFiles({ name: 'note.md', mimeType: 'text/markdown', buffer: Buffer.from('# hi') })
  await page.waitForTimeout(250)
  check('上传中列表顶上有占位行', await page.locator('[data-uploading]').count() === 1)
  check('上传按钮写着上传中', (await page.getByRole('button', { name: /上传中/ }).count()) === 1)
  await shot(page, 'kb-uploading')
  await page.waitForTimeout(1300)
  check('传完占位行消失', await page.locator('[data-uploading]').count() === 0)

  await page.locator('input[data-kb-upload]').setInputFiles({ name: 'data.csv', mimeType: 'text/csv', buffer: Buffer.from('a,b\n1,2') })
  await page.waitForTimeout(300)
  const toData = page.getByRole('button', { name: '去数据源传表格' })
  check('表格被拦下，toast 给「去数据源传表格」', await toData.count() === 1)
  check('……表格不会发到知识库', sent.filter((s) => s.key === 'POST /kb/upload').length === 1)
  await toData.click()
  await page.waitForTimeout(300)
  check('……点了就到 /data/tables', page.url().endsWith('/data/tables'), page.url())
  await goto(page, '/knowledge/kb')

  await page.getByRole('button', { name: '换一个' }).click()
  await page.getByRole('button', { name: /本地哈希向量/ }).click()
  const dlg = dialog(page)
  const body = await dlg.innerText().catch(() => '')
  check('切本地哈希先确认，并写出影响范围', body.includes('段知识') && body.includes('条记忆'))
  check('……要照抄「本地哈希」才能确认', await dlg.getByRole('button', { name: '切到本地哈希' }).isDisabled())
  await page.waitForTimeout(300)
  await shot(page, 'kb-local-confirm')
  await dlg.getByRole('button', { name: '取消' }).click()
  check('……取消了就什么都没发', !sent.some((s) => s.key === 'PUT /kb/embedding'))
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
}

console.log('\n=== 长期记忆 ===')
{
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

  await page.getByLabel('回忆测试').fill('出勤')
  await page.getByRole('button', { name: /回忆/ }).click()
  await page.waitForTimeout(300)
  const recall = sent.find((s) => s.key === 'GET /memory/search')
  check('回忆测试带 peek=true，不改召回计数', recall?.url.includes('peek=true'), recall?.url ?? '')

  if (withRun) {
    const href = await page.locator(`[data-memory="${withRun.id}"] a[data-memory-source]`).getAttribute('href')
    check('记忆来源可点，跳到那次运行', href === `/runs/${withRun.source.run_id}`, href ?? '')
  }
  if (target) {
    const row = page.locator(`[data-memory="${target.id}"]`)
    check('写着「记下于」', (await row.innerText()).includes('记下于'))
    await row.getByRole('button', { name: '修改这条记忆' }).click()
    await row.getByLabel('修改记忆内容').fill(`${target.content}（已核对）`)
    await row.getByRole('button', { name: '保存' }).click()
    await page.waitForTimeout(400)
    const patch = sent.find((s) => s.key.startsWith('PATCH /memory/'))
    check('原地修改走 PATCH', patch?.body?.content?.endsWith('（已核对）'), JSON.stringify(patch?.body ?? {}).slice(-40))

    await page.locator(`[data-memory="${target.id}"]`).getByRole('button', { name: '删除这条记忆' }).click()
    const d = dialog(page)
    check('删除记忆先确认，并说后果', (await d.innerText()).includes('召回不到'))
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
}

console.log('\n=== Skill ===')
{
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
}

console.log('\n=== 工具 ===')
{
  const danger = tools.find((t) => t.runtime_approval)
  const safe = tools.find((t) => t.source === 'builtin' && !t.dangerous)
  let calls = 0
  const { page, sent, natives, errors, close } = await open(danger ? `/tools/library/${danger.id}` : '/tools', {
    handlers: [
      [/^POST \/tools\/[^/]+\/run$/, (route, { body }) => {
        calls++
        return body?.confirm
          ? json({ ok: true, result: 'done', duration_ms: 5 })(route)
          : json({ detail: '会在 playground 工作目录里写入「x.txt」，同名文件会被覆盖。在工具库里执行不经过审批，确认后才会执行。' }, 409)(route)
      }],
      [/^POST \/mcp\/refresh$/, (route) => route.abort()],
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
  }
  await page.getByRole('tab', { name: '自定义工具' }).click()
  await page.waitForTimeout(400)
  await page.getByRole('button', { name: /新建工具/ }).first().click()
  const params = await dialog(page).locator('textarea').nth(1).inputValue()
  check('新建工具的参数给了能跑的示例，不是 {}', params.includes('"query"'), params.slice(0, 60))
  check('编辑器里有试跑区（保存后可用）', (await dialog(page).locator('[data-tool-trial]').innerText()).includes('先保存'))
  await dialog(page).getByRole('button', { name: '取消' }).click()

  await page.getByRole('tab', { name: 'MCP 接入' }).click()
  await page.waitForTimeout(400)
  await page.getByRole('button', { name: /重新加载/ }).click()
  await page.waitForTimeout(700)
  check('写操作失败有 error toast（以前一声不响）', await page.locator('[role="alert"][aria-live="assertive"] > *').count() > 0)
  check('没有原生对话框', natives.length === 0, natives.join(' | '))
  check('没有运行时报错', errors.length === 0, errors[0] ?? '')
  await close()
}

console.log('\n=== 自定义工具 · 已有工具 ===')
{
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
}

console.log('\n=== 知识库 · 撤销窗口里的轮询 ===')
{
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
}

console.log('\n=== 知识库 · 切块地图 ===')
{
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
}

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
