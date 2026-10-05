// 双盲复算与反方审稿的回归检查（期 3）。
//
// 双盲复算这一半守的是这些「看着没坏、其实用不了」的地方：
// - 审批卡：两路对不上时，卡上要先按摘要画出来（完整对照还在路上时不能一片空白），再按工件补全；具体差异逐条列，
//   只有兜底说明时写明「未能归纳成具体差异」并默认展开对照；对照两条 SQL 时左右并排（窄栏上下排），规范化后不同的
//   行高亮；三个选择缺一个都不能提交，提交的回复是 {choices, note}；卡上不能再冒出通用的输入框和「提交 / 驳回并终止」
//   （mode 是 input，通用那套会把一个字符串当 value 发出去）；后果按运行类别说，探索运行不提出具；
// - 检查器：口径卡、指标、口径说明、盲算模型、相对误差、不采用原流程时、启用，每一项都真的写进节点配置；
//   节点卡片的摘要写出复算哪张口径卡、几个指标；
// - 设置页「核查」一节：读回各项，改了之后整组写 verify（不限存 null），叫法和服务端 VERIFY_SETTING_LABEL 一字不差；
// - 390px 窄屏不横向溢出，深浅色都读得清，键盘能操作。
//
// 页面都跑在预览页（/preview.html?syn=…，用例在 frontend/src/dev/fixtures/dual_check.tsx，夹具 dual_check.json 两边
// 共用）和设置页上，接口一律 page.route 伪造：完整对照的工件、审批回复、设置的读写都在浏览器里答，别的非 GET 拦掉，
// 不碰真实的写接口。文案对照那一节不开浏览器：读 backend/app/api/settings.py，terms.ts 借 vite 转译后直接 import。
//
//   node scripts/check-verify.mjs
// 对别的实例跑时带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-verify.mjs
// VERIFY_SHOTS=<目录> 时亮暗两套各截几张，供人眼复核。
import { mkdirSync, readFileSync } from 'node:fs'
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const SHOTS = process.env.VERIFY_SHOTS ?? ''
const root = new URL('..', import.meta.url).pathname
const fx = JSON.parse(readFileSync(`${root}frontend/src/dev/fixtures/dual_check.json`, 'utf8'))
const AP = fx.approval
const CARD_ID = AP.payload.card_artifact

let failed = 0
const check = (name, cond, detail = '') => {
  const d = String(detail ?? '').replace(/\s+/g, ' ').slice(0, 160)
  console.log(`  ${cond ? '✓' : '✗'} ${name}${d ? ` — ${d}` : ''}`)
  if (!cond) failed++
}
/** 一节一节地跑：某一节里等待超时、元素找不到，只记成这一节失败，收掉它开的页面，接着跑下一节 */
const opened = new Set()
async function section(name, fn) {
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

// terms.ts 借 vite 转译成 data: URL 再 import（同 check-decode）：依赖也各自转好替换进去
const loaded = new Map()
async function load(path) {
  if (loaded.has(path)) return loaded.get(path)
  const res = await fetch(`${WEB}${path}?t=${Date.now()}`)
  if (!res.ok) throw new Error(`${path} → HTTP ${res.status}`)
  let code = await res.text()
  const deps = new Set([...code.matchAll(/from\s+["'](\/src\/[^"'?]+)(?:\?[^"']*)?["']/g)].map((m) => m[1]))
  for (const dep of deps) {
    const url = await load(dep)
    const pattern = new RegExp(`(["'])${dep.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(\\?[^"']*)?\\1`, 'g')
    code = code.replace(pattern, JSON.stringify(url))
  }
  const url = 'data:text/javascript;base64,' + Buffer.from(code).toString('base64')
  loaded.set(path, url)
  return url
}

const shot = async (page, name) => {
  if (!SHOTS) return
  mkdirSync(SHOTS, { recursive: true })
  await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true })
}
const text = async (loc) => (await loc.first().innerText().catch(() => '')).replace(/\s+/g, ' ').trim()

/** 两个颜色的对比度（WCAG）：颜色是 getComputedStyle 给的 rgb / rgba / color(srgb …)，半透明的叠在底色上 */
function contrast(fg, bg, base) {
  const parse = (c) => {
    if (typeof c !== 'string') return null
    const m = c.match(/color\(srgb ([\d.]+) ([\d.]+) ([\d.]+)(?: \/ ([\d.]+))?\)/)
    if (m) return [m[1] * 255, m[2] * 255, m[3] * 255, m[4] == null ? 1 : Number(m[4])]
    const n = c.match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/)
    return n ? [Number(n[1]), Number(n[2]), Number(n[3]), n[4] == null ? 1 : Number(n[4])] : null
  }
  const over = (top, under) => top.slice(0, 3).map((v, i) => v * top[3] + under[i] * (1 - top[3]))
  const lum = ([r, g, b]) => {
    const f = (v) => { const s = v / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4 }
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
  }
  const b0 = parse(base); const b1 = parse(bg); const f0 = parse(fg)
  if (!b0 || !b1 || !f0) return 0
  const under = over(b1, b0.slice(0, 3))
  const a = lum(over(f0, under)); const b = lum(under)
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
}

// ---------------------------------------------------------------- 文案对照（不开浏览器）

await section('核查设置的叫法和服务端一字不差', async () => {
  // 设置页保存时 422 的报错写的是 VERIFY_SETTING_LABEL 里的叫法：界面上的字段名得是同一套，人才找得到是哪一项
  const py = readFileSync(`${root}backend/app/api/settings.py`, 'utf8')
  const block = py.match(/^VERIFY_SETTING_LABEL = \{([\s\S]*?)^\}/m)?.[1] ?? ''
  const labels = Object.fromEntries([...block.matchAll(/"([^"]+)":\s*"([^"]+)"/g)].map((m) => [m[1], m[2]]))
  check('从 settings.py 读出了 VERIFY_SETTING_LABEL', Object.keys(labels).length >= 10, `${Object.keys(labels).length} 项`)
  const { VERIFY_SETTING_TEXT: T } = await import(await load('/src/lib/terms.ts'))
  const keys = Object.keys(T).filter((k) => k !== 'section')
  const missing = Object.keys(labels).filter((k) => !(k in T))
  const extra = keys.filter((k) => !(k in labels))
  check('VERIFY_SETTING_TEXT 顶层的键和服务端完全相同（section 除外）', !missing.length && !extra.length,
    `缺 ${missing.join('、') || '无'}；多 ${extra.join('、') || '无'}`)
  const wrong = Object.entries(labels).filter(([k, v]) => T[k] !== v)
  check('……每一项的叫法一字不差', !wrong.length, wrong.map(([k, v]) => `${k}：${T[k]} ≠ ${v}`).join('；'))
  check('……这一节其余的文案放在 section 里', !!T.section?.title && typeof T.section.spend === 'function')
})

// ---------------------------------------------------------------- 页面

const browser = await chromium.launch({ executablePath: CHROME })

/**
 * 开一个预览用例。完整对照的工件按 artifact 答（默认夹具里的那份；给函数可以延迟、出错），审批回复记下请求体、
 * 回一个伪造的运行；审批列表回空；别的非 GET 一律拦掉，别的 GET 放行
 */
async function preview(syn, { width = 1280, height = 900, theme = 'dark', artifact = null, extra = null } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: theme, timezoneId: 'Asia/Shanghai' })
  opened.add(ctx)
  ctx.setDefaultTimeout(6000)
  const page = await ctx.newPage()
  const sent = []
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('dialog', (d) => { errors.push(`原生对话框：${d.message()}`); void d.dismiss() })
  await page.route((u) => new URL(u).pathname.startsWith('/api/'), async (route) => {
    const req = route.request()
    const url = new URL(req.url())
    if (extra) {
      const out = await extra(req, url)
      if (out) return route.fulfill({ status: out.status ?? 200, json: out.json })
    }
    if (req.method() === 'POST' && url.pathname === `/api/approvals/${AP.id}/decide`) {
      sent.push({ path: url.pathname, body: req.postDataJSON() })
      return route.fulfill({ json: { id: AP.run_id, status: 'running', run_class: AP.run_class, input: {}, output: {}, usage: {} } })
    }
    if (req.method() !== 'GET') {
      sent.push({ path: url.pathname, blocked: true, method: req.method() })
      return route.abort()
    }
    if (url.pathname === `/api/artifacts/${CARD_ID}`) {
      const out = artifact ? await artifact() : { json: { id: CARD_ID, content: fx.card } }
      return route.fulfill({ status: out.status ?? 200, json: out.json })
    }
    if (url.pathname === '/api/approvals') return route.fulfill({ json: [] })
    return route.continue()
  })
  await page.goto(`${WEB}/preview.html?syn=${syn}&theme=${theme}&actor=张工`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector(`[data-fixture="${syn}"]`)
  return { ctx, page, sent, errors }
}

const metric = (page, id) => page.locator(`[data-dual-metric="${id}"]`)

await section('审批卡：先按摘要画，再取完整对照', async () => {
  // 完整对照晚一点到：这段时间里卡上照样有两个值、相差多少、摘要里的差异，选择也能先做
  let release
  const gate = new Promise((r) => { release = r })
  const { page, sent, errors } = await preview('dual-check-approval', {
    artifact: async () => { await gate; return { json: { id: CARD_ID, content: fx.card } } },
  })
  await page.waitForSelector('[data-dual-check]')
  const card = page.locator('[data-approval]')
  check('卡头是审批标题', (await text(card)).includes('双盲复算：2 个指标不一致'))
  check('开头一句说几个指标不一致、要逐个选', (await text(page.locator('[data-dual-intro]'))) === '盲算与原流程有 2 个指标不一致，请逐个选择采用哪一边的结果')
  check('每个不一致的指标一块，按摘要的顺序', (await page.locator('[data-dual-metric]').evaluateAll((els) => els.map((e) => e.dataset.dualMetric))).join(',')
    === 'gmv,refund_rate')
  const gmv = metric(page, 'gmv')
  check('指标名、原流程值、盲算值', (await text(gmv.locator('[data-dual-name]'))) === '销售额'
    && (await text(gmv.locator('[data-dual-value="original"]'))) === '45,678.5元' && (await text(gmv.locator('[data-dual-value="blind"]'))) === '51,388.3元')
  check('……两个值各标是哪一边', (await text(gmv.locator('[data-dual-values]'))).includes('原流程') && (await text(gmv.locator('[data-dual-values]'))).includes('盲算'))
  check('相差百分比带正负号', (await text(gmv.locator('[data-dual-delta]'))) === '相差 +12.5%' && (await text(metric(page, 'refund_rate').locator('[data-dual-delta]'))) === '相差 +31.91%',
    `${await text(gmv.locator('[data-dual-delta]'))} / ${await text(metric(page, 'refund_rate').locator('[data-dual-delta]'))}`)
  check('完整对照还没到：先列摘要里的具体差异', (await text(gmv.locator('[data-dual-diff-item]'))) === AP.payload.summary[0].diff[0])
  check('……只有兜底的那个指标写明未能归纳', (await text(metric(page, 'refund_rate').locator('[data-dual-fallback]'))) === '两条 SQL 的写法不同，未能归纳成具体差异')
  check('……说明正在加载完整对照', (await page.locator('[data-dual-loading]').count()) > 0)
  check('卡上没有通用的输入框和备注', await card.locator('textarea[aria-label="你的输入"], input[aria-label="备注"]').count() === 0)
  check('……也没有通用的「提交」「驳回并终止」', await card.getByRole('button', { name: '提交', exact: true }).count() === 0
    && await card.getByRole('button', { name: /驳回/ }).count() === 0)
  check('……通用那句「提交：以你填写的内容继续运行」也不在', !(await text(card)).includes('以你填写的内容继续运行'))
  check('署名那一行还在', (await text(card)).includes('将以「张工」签批'))
  release()
  await page.waitForSelector('[data-dual-check][data-state="ready"]')
  check('完整对照到了：写出盲算模型', (await text(page.locator('[data-dual-model]'))) === '盲算模型：blind-model-x')
  check('……具体差异按工件逐条列', (await gmv.locator('[data-dual-diff-item]').count()) === 1
    && (await text(gmv.locator('[data-dual-diff-item]'))) === fx.card.metrics[0].diff.items[0].text)
  check('……具体差异的那个指标默认收着对照，给「对照两条 SQL」', (await gmv.locator('[data-dual-compare-toggle]').getAttribute('aria-expanded')) === 'false'
    && (await text(gmv.locator('[data-dual-compare-toggle]'))) === '对照两条 SQL' && await gmv.locator('[data-dual-compare]').count() === 0)
  const rr = metric(page, 'refund_rate')
  check('……只有兜底的那个指标默认展开对照', (await rr.locator('[data-dual-compare-toggle]').getAttribute('aria-expanded')) === 'true'
    && await rr.locator('[data-dual-compare]').count() === 1)
  check('……兜底的原话不再列成一条「具体差异」', await rr.locator('[data-dual-diff-item]').count() === 0)
  check('只差一个方面的指标：说明选择会记入数据目录', (await text(gmv.locator('[data-dual-catalog]'))).includes('记入数据目录')
    && await rr.locator('[data-dual-catalog]').count() === 0)
  const after = await text(page.locator('[data-dual-consequence]'))
  check('正式运行：后果写明报告里的数仍取自原流程、采用盲算或都不对会让出具降档', after.includes('报告中的数仍取自原流程') && after.includes('出具降档'), after)
  check('没有发出任何写请求', sent.length === 0, JSON.stringify(sent))
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await shot(page, 'approval-dark')
})

await section('审批卡：并排对照与高亮', async () => {
  const { page, errors } = await preview('dual-check-approval')
  await page.waitForSelector('[data-dual-check][data-state="ready"]')
  const gmv = metric(page, 'gmv')
  const toggle = gmv.locator('[data-dual-compare-toggle]')
  // 键盘：Tab 到按钮上回车展开
  await toggle.focus()
  await page.keyboard.press('Enter')
  check('键盘回车展开对照，按钮标为展开、指向对照区', (await toggle.getAttribute('aria-expanded')) === 'true'
    && await page.locator(`#${await toggle.getAttribute('aria-controls')}[data-dual-compare]`).count() === 1)
  check('……按钮改说「收起对照」', (await text(toggle)) === '收起对照')
  const orig = gmv.locator('[data-dual-sql="original"]')
  const blind = gmv.locator('[data-dual-sql="blind"]')
  check('两条 SQL 都在，各标是哪一边', (await text(orig)).includes('paid_at') && (await text(blind)).includes('created_at')
    && (await text(gmv.locator('[data-dual-pair]'))).includes('原流程的 SQL') && (await text(gmv.locator('[data-dual-pair]'))).includes('盲算的 SQL'))
  const marks = await gmv.evaluate((el) => ({
    del: [...el.querySelectorAll('[data-dual-sql="original"] [data-diff-line="del"]')].map((m) => m.textContent.trim()),
    add: [...el.querySelectorAll('[data-dual-sql="blind"] [data-diff-line="add"]')].map((m) => m.textContent.trim()),
  }))
  check('text_diff 的行在原流程那边高亮', marks.del.join('|') === "WHERE paid_at >= '2026-09-01'|AND paid_at < '2026-09-08'", marks.del.join('|'))
  check('……在盲算那边也高亮', marks.add.join('|') === "WHERE created_at >= '2026-09-01'|AND created_at < '2026-09-08'", marks.add.join('|'))
  check('……相同的行不高亮', !(await orig.locator('[data-diff-line]').allInnerTexts()).some((t) => t.includes('FROM orders')))
  const o = await orig.boundingBox()
  const b = await blind.boundingBox()
  check('宽栏里左右并排：同一行、盲算在右边', !!o && !!b && Math.abs(o.y - b.y) < 2 && b.x >= o.x + o.width - 1, JSON.stringify({ o, b }))
  check('盲算的说明', (await text(gmv.locator('[data-dual-rationale]'))).includes('销售额按下单时间统计本周全部订单金额之和'))
  check('输入取值：路径、两边的值', (await text(gmv.locator('[data-dual-input="vars.kpi.gmv"]'))).includes('45,678.5')
    && (await text(gmv.locator('[data-dual-input="vars.kpi.gmv"]'))).includes('51,388.3'))
  const lines = await gmv.locator('[data-dual-text-diff] [data-diff-side]').evaluateAll((els) => els.map((e) => e.dataset.diffSide))
  check('规范化后不同的行另列一份，每行标是哪一边', lines.join(',') === 'original,original,blind,blind', lines.join(','))
  const rr = metric(page, 'refund_rate')
  check('两个输入、只有一个不同：只对照不同的那一个', (await rr.locator('[data-dual-pair]').evaluateAll((els) => els.map((e) => e.dataset.dualPair))).join(',')
    === 'vars.kpi.refunds')
  check('……输入取值两项都列，不同的那项标出来', await rr.locator('[data-dual-input]').count() === 2
    && await rr.locator('[data-dual-input="vars.kpi.refunds"][data-differs]').count() === 1
    && await rr.locator('[data-dual-input="vars.kpi.orders"][data-differs]').count() === 0)
  // 原流程那条规范化后多了 AS，和原文对不上：不乱标；盲算那条规范化后和原文一字不差：照样高亮
  check('……一行写完的 SQL：规范化后对不上原文的不乱标，对得上的照样高亮', await rr.locator('[data-dual-sql="original"] [data-diff-line]').count() === 0
    && await rr.locator('[data-dual-sql="blind"] [data-diff-line="add"]').count() === 1)
  check('……规范化后不同的行照样另列', await rr.locator('[data-dual-text-diff] [data-diff-side]').count() === 2)
  await page.keyboard.press('Enter')
  check('再按一次回车收起', (await toggle.getAttribute('aria-expanded')) === 'false' && await gmv.locator('[data-dual-compare]').count() === 0)
  check('页面没有报错', errors.length === 0, errors.join('；'))
})

await section('审批卡：不选全不能提交，提交的回复是 {choices, note}', async () => {
  const { page, sent, errors } = await preview('dual-check-approval')
  await page.waitForSelector('[data-dual-check][data-state="ready"]')
  const submit = page.locator('[data-dual-submit]')
  check('三个选项必选：一个都没选时不能提交', await submit.isDisabled() && (await text(page.locator('[data-dual-pending]'))) === '还有 2 个指标未选择')
  const gmv = metric(page, 'gmv')
  const opts = await gmv.locator('[data-dual-choice] label').allInnerTexts()
  check('三个选项的说法', opts.map((t) => t.trim()).join('|') === '采用原流程的结果|采用盲算的结果|两边都不对', opts.join('|'))
  check('……每组有读屏名：哪个指标采用哪一边', (await text(gmv.locator('[data-dual-choice] legend'))) === '「销售额」采用哪一边的结果')
  await gmv.locator('label', { hasText: '采用原流程的结果' }).click()
  check('选了一个：还差一个，仍不能提交', await submit.isDisabled() && (await text(page.locator('[data-dual-pending]'))) === '还有 1 个指标未选择')
  // 键盘：方向键在一组里换选项
  const rr = metric(page, 'refund_rate')
  await rr.locator('input[value="a"]').focus()
  await page.keyboard.press('ArrowRight')
  await page.keyboard.press('ArrowRight')
  check('键盘方向键在一组里换选项', await rr.locator('input[value="neither"]').isChecked() && !(await rr.locator('input[value="b"]').isChecked()))
  check('全选了：可以提交、不再提示未选', !(await submit.isDisabled()) && await page.locator('[data-dual-pending]').count() === 0)
  const note = '销售额以支付时间为准；退款率两边都没有剔除部分退款'
  await page.locator('[data-dual-note]').fill(note)
  await submit.click()
  await page.waitForFunction(() => window.__dualResolved != null)
  const posts = sent.filter((s) => s.path === `/api/approvals/${AP.id}/decide`)
  check('走审批回复接口，只发一次', posts.length === 1, String(posts.length))
  const body = posts[0]?.body ?? {}
  check('请求体：value 是 {choices, note}', JSON.stringify(body.value) === JSON.stringify({ choices: { gmv: 'a', refund_rate: 'neither' }, note }),
    JSON.stringify(body.value))
  check('……照 input 模式的写法：approved 为真、备注同时放在外层', body.approved === true && body.note === note, JSON.stringify(body))
  check('提交后交给调用方重新接上运行', await page.evaluate(() => window.__dualResolved) === AP.run_id)
  check('……提示已提交、运行继续', (await text(page.locator('body'))).includes('已提交裁决，运行继续'))
  check('没有别的写请求', !sent.some((s) => s.blocked), JSON.stringify(sent.filter((s) => s.blocked)))
  check('页面没有报错', errors.length === 0, errors.join('；'))
})

await section('审批卡：完整对照取不到时，按摘要照样能裁决', async () => {
  const { page, sent } = await preview('dual-check-approval', {
    artifact: async () => ({ status: 404, json: { detail: '工件不存在' } }),
  })
  await page.waitForSelector('[data-dual-load-failed]')
  check('写明无法加载完整对照、以下仅为摘要', (await text(page.locator('[data-dual-load-failed]'))) === '无法加载完整对照，以下仅为摘要')
  check('……摘要照样在：值、相差、差异', (await text(metric(page, 'gmv').locator('[data-dual-value="blind"]'))) === '51,388.3元'
    && await metric(page, 'gmv').locator('[data-dual-diff-item]').count() === 1)
  check('……没有可对照的 SQL，不给「对照两条 SQL」', await page.locator('[data-dual-compare-toggle]').count() === 0)
  for (const id of ['gmv', 'refund_rate']) await metric(page, id).locator('label', { hasText: '采用盲算的结果' }).click()
  await page.locator('[data-dual-submit]').click()
  await page.waitForFunction(() => window.__dualResolved != null)
  const body = sent.find((s) => s.path === `/api/approvals/${AP.id}/decide`)?.body
  check('……照样能提交', JSON.stringify(body?.value?.choices) === JSON.stringify({ gmv: 'b', refund_rate: 'b' }), JSON.stringify(body))
})

await section('审批卡：探索运行不提出具', async () => {
  const { page } = await preview('dual-check-approval-exploratory')
  await page.waitForSelector('[data-dual-check][data-state="ready"]')
  const said = await text(page.locator('[data-dual-consequence]'))
  check('后果只说记为缺口，不写任何出具相关的话', said.includes('记为缺口') && !said.includes('出具'), said)
})

await section('审批卡：390px 不横向溢出、窄栏里上下排、深浅色可读', async () => {
  for (const theme of ['dark', 'light']) {
    const { page, errors } = await preview('dual-check-approval', { width: 390, height: 844, theme })
    await page.waitForSelector('[data-dual-check][data-state="ready"]')
    await metric(page, 'gmv').locator('[data-dual-compare-toggle]').click()
    const w = await page.evaluate(() => ({
      page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      card: (() => { const c = document.querySelector('[data-dual-check]'); return c.scrollWidth - c.clientWidth })(),
    }))
    check(`390px（${theme}）：两边都展开后不横向溢出`, w.page <= 0 && w.card <= 1, JSON.stringify(w))
    const o = await metric(page, 'gmv').locator('[data-dual-sql="original"]').boundingBox()
    const b = await metric(page, 'gmv').locator('[data-dual-sql="blind"]').boundingBox()
    check(`390px（${theme}）：两条 SQL 上下排`, !!o && !!b && b.y >= o.y + o.height - 1, JSON.stringify({ o, b }))
    const c = await page.evaluate(() => {
      const card = document.querySelector('[data-approval]')
      const base = getComputedStyle(document.body).backgroundColor
      const pick = (sel) => {
        const el = document.querySelector(sel)
        if (!el) return null
        // 往上找第一层不透明（或半透明）的底色
        let bg = 'rgba(0, 0, 0, 0)'
        for (let n = el; n && n !== document.documentElement; n = n.parentElement) {
          const v = getComputedStyle(n).backgroundColor
          if (v !== 'rgba(0, 0, 0, 0)' && v !== 'transparent') { bg = v; break }
        }
        return { fg: getComputedStyle(el).color, bg }
      }
      return { base, card: getComputedStyle(card).backgroundColor, delta: pick('[data-dual-delta]'),
        consequence: pick('[data-dual-consequence]'), mark: pick('[data-diff-line="del"]'), fallback: pick('[data-dual-fallback]') }
    })
    for (const key of ['delta', 'consequence', 'mark', 'fallback']) {
      const v = c[key]
      const ratio = v ? contrast(v.fg, v.bg, c.base) : 0
      check(`${theme}：${({ delta: '相差比例', consequence: '后果说明', mark: '高亮的行', fallback: '兜底说明' })[key]}对比度 ≥ 4.5`, ratio >= 4.5,
        `${ratio.toFixed(2)}（${v?.fg} on ${v?.bg}）`)
    }
    await shot(page, `approval-narrow-${theme}`)
    check(`390px（${theme}）：页面没有报错`, errors.length === 0, errors.join('；'))
  }
  const rail = await preview('dual-check-approval-rail', { width: 1280, height: 900 })
  await rail.page.waitForSelector('[data-dual-check][data-state="ready"]')
  await metric(rail.page, 'gmv').locator('[data-dual-compare-toggle]').click()
  const r = await rail.page.evaluate(() => {
    const box = document.querySelector('[data-fixture="dual-check-approval-rail"]')
    return box.scrollWidth - box.clientWidth
  })
  check('360px 画布右栏：展开对照也不撑宽右栏', r <= 0, `${r}px`)
  const o = await metric(rail.page, 'gmv').locator('[data-dual-sql="original"]').boundingBox()
  const b = await metric(rail.page, 'gmv').locator('[data-dual-sql="blind"]').boundingBox()
  check('360px 画布右栏：宽屏下也按卡片的宽度上下排', !!o && !!b && b.y >= o.y + o.height - 1, JSON.stringify({ o, b }))
  await shot(rail.page, 'approval-rail-dark')
})

// ---------------------------------------------------------------- 检查器与节点卡片

/**
 * 检查器用例：设置（相对误差、启用的「跟随设置」要写出眼下的值）按夹具答，改配置触发的校验、变量分析回空，
 * 节点配置直接从 studio store 读（window.__studio，开发服务器才挂）
 */
async function inspector(opts = {}) {
  const out = await preview('dual-check-inspector', {
    ...opts,
    extra: (req, url) => {
      if (req.method() === 'GET' && url.pathname === '/api/settings') return { json: { run: {}, verify: fx.verify } }
      if (req.method() === 'POST' && url.pathname === '/api/workflows/validate') return { json: { issues: [] } }
      if (req.method() === 'POST' && url.pathname === '/api/workflows/variables') return { json: { variables: [], issues: [] } }
      return null
    },
  })
  await out.page.waitForSelector('[data-field="card"] select')
  return out
}
const config = (page) => page.evaluate(() => window.__studio.getState().nodes.find((n) => n.id === 'dc').data.config)
const summaryOf = (page) => text(page.locator('.react-flow__node[data-id="dc"] .nc-summary'))
/** 画布上的节点是 React Flow 在下一拍才按新配置重画的：等摘要变成想要的那句（最多 3 秒），返回最后看到的 */
async function summaryBecomes(page, want) {
  const ok = await page.waitForFunction((w) => {
    const el = document.querySelector('.react-flow__node[data-id="dc"] .nc-summary')
    return !!el && (typeof w === 'string' ? el.textContent.trim() === w : el.textContent.trim().endsWith(w.suffix))
  }, want, { timeout: 3000 }).then(() => true, () => false)
  return { ok, seen: await summaryOf(page) }
}

await section('检查器：每一项都写进节点配置，节点卡片的摘要跟着变', async () => {
  const { page, errors, sent } = await inspector()
  const panel = page.locator('[data-dual-inspector]')
  check('说明里不再写「功能开发中」', !(await text(panel)).includes('功能开发中') && (await text(panel)).includes('另起一路独立计算'))
  check('节点卡片的摘要：复算哪张口径卡、全部几个指标', await summaryOf(page) === '复算「周报口径卡」 · 全部 3 个指标', await summaryOf(page))

  // 口径卡：下拉选画布上的口径卡，上游的一组、其余的一组；留空取上游的那张
  const card = page.locator('[data-field="card"] select')
  const groups = await card.evaluate((el) => [...el.querySelectorAll('optgroup')].map((g) => `${g.label}:${[...g.querySelectorAll('option')].map((o) => o.value).join('+')}`))
  check('口径卡：上游的一组、其余的一组', groups.join('|') === '上游的口径卡:card|其他口径卡（需连接到本节点上游）:card2', groups.join('|'))
  check('……留空那一项写明默认取上游的哪一张', (await card.locator('option').first().innerText()) === '默认：上游的口径卡「周报口径卡」')
  check('……标签指向这个下拉', await page.locator('[data-field="card"] label').getAttribute('for') === await card.getAttribute('id'))
  await card.selectOption('card2')
  check('选另一张：写进 card', (await config(page)).card === 'card2', JSON.stringify(await config(page)))
  check('……不在上游的照实提醒', (await text(page.locator('[data-dual-card-warn]'))).includes('不在本节点上游'))
  const swapped = await summaryBecomes(page, '复算「月报口径卡」 · 全部 1 个指标')
  check('……摘要换成这张卡', swapped.ok, swapped.seen)
  await card.selectOption('')
  check('选回默认：card 去掉，不留空串', !('card' in (await config(page))) || (await config(page)).card === undefined, JSON.stringify(await config(page)))

  // 指标：逐个勾；全部勾着是缺省，不写；最后一个不让取消
  const opts = page.locator('[data-dual-metric-option]')
  check('指标：列出这张卡的三个指标，默认全勾、说明是缺省', await opts.count() === 3
    && await page.locator('[data-dual-metric-option] input:checked').count() === 3
    && (await text(page.locator('[data-dual-metrics-state]'))) === '未指定：复算全部输入来自查询的指标')
  check('……一组有读屏名', await page.locator('[data-dual-metrics="list"]').getAttribute('aria-label') === '复算的指标')
  await page.locator('[data-dual-metric-option="aov"] input').uncheck()
  check('取消一个：写进 metrics（按卡里的顺序）', JSON.stringify((await config(page)).metrics) === '["gmv","orders"]', JSON.stringify((await config(page)).metrics))
  const two = await summaryBecomes(page, '复算「周报口径卡」 · 2 个指标')
  check('……摘要写几个指标', two.ok, two.seen)
  check('……说明写已选几个', (await text(page.locator('[data-dual-metrics-state]'))) === '已选 2 / 3 个指标')
  await page.locator('[data-dual-metric-option="orders"] input').uncheck()
  check('只剩一个：不让取消（一个都不勾和缺省分不清）', await page.locator('[data-dual-metric-option="gmv"] input').isDisabled()
    && JSON.stringify((await config(page)).metrics) === '["gmv"]')

  // 口径说明：只列要复算的指标；写了的存成 {指标 id: 说明}
  const defs = page.locator('[data-dual-definition]')
  check('口径说明：只列要复算的指标', (await defs.evaluateAll((els) => els.map((e) => e.dataset.dualDefinition))).join(',') === 'gmv')
  const words = '销售额按支付时间统计，不含退款'
  await page.locator('[data-dual-definition="gmv"] textarea').fill(words)
  check('写一段：存成 {指标 id: 说明}', JSON.stringify((await config(page)).definitions) === JSON.stringify({ gmv: words }),
    JSON.stringify((await config(page)).definitions))
  check('……每段有自己的标签（指标名）', (await text(page.locator('[data-dual-definition="gmv"] label'))) === '销售额（gmv）')
  for (const id of ['orders', 'aov']) await page.locator(`[data-dual-metric-option="${id}"] input`).check()
  check('全部勾回来：metrics 去掉（缺省）', (await config(page)).metrics === undefined, JSON.stringify((await config(page)).metrics))
  await page.locator('[data-dual-definition="orders"] textarea').fill('订单数按支付成功的订单计')
  await page.locator('[data-dual-metric-option="orders"] input').uncheck()
  check('写了说明的指标被取消：照实提醒、能删掉', (await text(page.locator('[data-dual-definition-gone]'))).includes('「orders」'))
  await page.locator('[data-dual-definition-gone] button').click()
  check('……删掉后只剩还在复算的那段', JSON.stringify((await config(page)).definitions) === JSON.stringify({ gmv: words }))
  await page.locator('[data-dual-definition="gmv"] textarea').fill('')
  check('清空最后一段：definitions 去掉', (await config(page)).definitions === undefined)
  await page.locator('[data-dual-metric-option="orders"] input').check()

  // 启用：跟随设置 / 开启 / 关闭
  const enabled = page.locator('[data-field="enabled"] [role="radiogroup"]')
  check('启用：三态，跟随设置写出设置里眼下是开着的', (await enabled.locator('[role="radio"]').allInnerTexts()).join('|') === '跟随设置（当前开启）|开启|关闭'
    && await enabled.locator('[role="radio"][aria-checked="true"]').getAttribute('data-value') === 'follow')
  await enabled.locator('[data-value="off"]').click()
  const off = await summaryBecomes(page, { suffix: ' · 已关闭' })
  check('关闭：enabled 为 false，摘要标「已关闭」', (await config(page)).enabled === false && off.ok, off.seen)
  await enabled.locator('[data-value="off"]').focus()
  await page.keyboard.press('ArrowLeft')
  check('键盘方向键换选项：开启是 true', (await config(page)).enabled === true
    && await page.evaluate(() => document.activeElement?.getAttribute('data-value')) === 'on')
  await enabled.locator('[data-value="follow"]').click()
  check('跟随设置：enabled 去掉，不留 null', (await config(page)).enabled === undefined, JSON.stringify(await config(page)))

  // 不采用原流程时
  const reject = page.locator('[data-field="on_reject"] select')
  check('不采用原流程时：默认落在「记为出具缺口」上', (await reject.locator('option:checked').innerText()) === '记为出具缺口，出具降档（默认）')
  await reject.selectOption('fail')
  check('……选判为失败：on_reject 为 fail', (await config(page)).on_reject === 'fail')

  // 高级：盲算模型、相对误差
  await page.locator('button', { hasText: '高级选项' }).click()
  const provider = page.locator('[data-field="provider"] select')
  check('盲算模型 · 接入：留空跟随设置，停用的接入照实标出', (await provider.locator('option').allInnerTexts()).join('|') === '跟随设置|云端接入|备用接入（已停用）',
    (await provider.locator('option').allInnerTexts()).join('|'))
  await provider.selectOption('云端接入')
  const model = page.locator('[data-field="model"] input')
  check('选了接入：provider 写进去，模型的占位换成接入的默认模型', (await config(page)).provider === '云端接入'
    && await model.getAttribute('placeholder') === '留空则使用该接入的默认模型')
  await model.fill('model-b')
  check('模型：写进 model', (await config(page)).model === 'model-b')
  await provider.selectOption('')
  check('只填了模型：接入留空那一项改叫「按模型名找接入」，写明按模型名用哪个接入', (await provider.locator('option').first().innerText()) === '按模型名找接入'
    && (await text(page.locator('[data-dual-by-model]'))).includes('将按模型名使用接入「云端接入」') && (await config(page)).provider === undefined)
  const tol = page.locator('[data-field="tolerance"] input')
  check('允许的相对误差：占位写出设置里的值', await tol.getAttribute('placeholder') === '跟随设置（0.000001）', await tol.getAttribute('placeholder'))
  await tol.fill('0.001')
  check('……填了写成数', (await config(page)).tolerance === 0.001)
  await tol.fill('-1')
  check('……负数标出来', (await text(page.locator('[data-field="tolerance"]'))).includes('请填写不小于 0 的数') && await tol.getAttribute('aria-invalid') === 'true')
  await tol.fill('')
  check('……清空回到跟随设置', (await config(page)).tolerance === undefined)
  const KEYS = ['card', 'metrics', 'definitions', 'provider', 'model', 'tolerance', 'on_reject', 'enabled']
  const keys = Object.entries(await config(page)).filter(([, v]) => v !== undefined).map(([k]) => k)
  check('配置里只有约定的键，留空的项不留空值', keys.every((k) => KEYS.includes(k)) && keys.sort().join(',') === 'model,on_reject', keys.join(','))
  check('没有写库的请求', !sent.some((s) => s.blocked), JSON.stringify(sent.filter((s) => s.blocked)))
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await shot(page, 'inspector-dark')
})

await section('检查器：360px 栏、390px 窄屏不横向溢出，深浅色可读', async () => {
  for (const [width, theme] of [[1280, 'dark'], [390, 'light']]) {
    const { page, errors } = await inspector({ width, height: 900, theme })
    await page.locator('button', { hasText: '高级选项' }).click()
    await page.locator('[data-field="model"] input').fill('model-zzz')
    const w = await page.evaluate(() => {
      const panel = document.querySelector('[data-dual-inspector]')
      const body = panel.querySelector('fieldset')
      return { page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        panel: panel.scrollWidth - panel.clientWidth, body: body ? body.scrollWidth - body.clientWidth : 0 }
    })
    check(`${width}px（${theme}）：检查器不横向溢出`, w.page <= 0 && w.panel <= 0 && w.body <= 0, JSON.stringify(w))
    const c = await page.evaluate(() => {
      const base = getComputedStyle(document.querySelector('[data-dual-inspector]')).backgroundColor
      const el = document.querySelector('[data-dual-by-model]')
      const radio = document.querySelector('[data-field="enabled"] [role="radio"][aria-checked="true"]')
      return { base, hint: el && getComputedStyle(el).color, radio: radio && getComputedStyle(radio).color,
        radioBg: radio && getComputedStyle(radio).backgroundColor }
    })
    const hint = contrast(c.hint, c.base, c.base)
    check(`${theme}：只填了模型的提醒对比度 ≥ 4.5`, hint >= 4.5, `${hint.toFixed(2)}（${c.hint} on ${c.base}）`)
    const radio = contrast(c.radio, c.radioBg, c.base)
    check(`${theme}：启用里选中那一项对比度 ≥ 4.5`, radio >= 4.5, `${radio.toFixed(2)}（${c.radio} on ${c.radioBg}）`)
    await shot(page, `inspector-${width}-${theme}`)
    check(`${width}px（${theme}）：页面没有报错`, errors.length === 0, errors.join('；'))
  }
})

await browser.close()
console.log(failed ? `\n✗ 双盲复算与反方审稿：${failed} 项未通过` : '\n✓ 双盲复算与反方审稿：全部通过')
process.exit(failed ? 1 : 0)
