// 双盲复算与反方审稿的回归检查（期 3）。
//
// 双盲复算这一半守的是这些「看着没坏、其实用不了」的地方：
// - 审批卡：两路对不上时，卡上要先按摘要画出来（完整对照还在路上时不能一片空白），再按工件补全；具体差异逐条列，
//   只有兜底说明（或只有解析失败）时写明「未能归纳成具体差异」并默认展开对照；对照两条 SQL 时左右并排（窄栏上下排），
//   原文按行比出的不同行高亮，规范化后不同的行（text_diff）分两栏高亮——夹具里的差异说明是服务端的真实输出
//   （backend/tests/test_dual_check_fixture.py 核对），规范化的行和原文对不上，高亮不能指望两边逐行相等；
//   「记入数据目录」不能说得比服务端宽：服务端在摘要里预告了采用哪一边写什么（payload.summary[].catalog）时照它说，
//   两边都不写就不提，当前选的那一句加重；同一次裁决里别的指标按当前的选择落到同一张表的同一项上时，照服务端写目录前
//   的归拢说——业务日期、去重规则说法不同的改说都不记，有效记录条件说合成一句；老服务端没有预告时只在可能写的形状下
//   （恰好一条具体差异，且是时间列、筛选条件或聚合）说必要条件，只有一边按时间筛、多出的是指标自己范围的条件、采用
//   不去重的一边这几种不写的照实说——夹具里每个变体都带着服务端对这一对 SQL 的真实预告，几种组合带着服务端真写一遍的
//   结果，两种说法都按它核对；
//   三个选择缺一个都不能提交，提交的回复是 {choices, note}；卡上不能再冒出通用的输入框和「提交 / 驳回并终止」
//   （mode 是 input，通用那套会把一个字符串当 value 发出去），认不出指标时另给「驳回并终止」；后果按运行类别和节点的
//   「未采用原流程时」说（payload 带了实际生效的处置就照它说，老服务端从运行的图快照里读），探索运行不提出具，读不到
//   节点设置时有条件地说；
// - 审批卡（期 4）：预告逐条说「会记为……」或「不会记入：原因」，原因是服务端的原话；被目录挡下的一边照样说写到哪；
//   几个指标落到同一项上时照完整对照里服务端算好的合并预告（catalog_joint）说，和真写的一字不差（夹具的 merges 是服务端
//   真写一遍的结果）；没有合并预告时按其余指标当前的选择推，被目录挡下的同项指标也算进来（说法不同就说都不记，说不准的
//   只说可能），结尾换成以时间线上的记录为准；盲算的理由按核查 agent 组装的理由文档画，有出处的数字画实线、裸数字醒目
//   标出，老工件照旧纯文本；
// - 出具缺口：证据页（真的 EvidencePane）和成果节点印章的悬停说明把双盲复算、反方审稿报上来的缺口另起「核查未通过」
//   「核查未完成」两组，不放在「校验未全部完成」下；
// - 记录页的工件列表：双盲复算对照、反方审稿记录两种工件有名字和说明，不露键名；
// - 检查器：口径卡、指标、口径说明、盲算模型、相对误差、未采用原流程时、启用，每一项都真的写进节点配置；字段的
//   叫法和服务端校验报错里的（engine/labels.py 的 DUAL_CHECK_FIELD_LABEL）一字不差；选口径卡、盲算模型、启用三态用的是
//   和反方审稿共用的控件（verifyFields.tsx）；盲算模型不藏在高级选项里
//   （时间线「模型相同」那一行要能定位过来）；上游有好几张口径卡时写明必须选；「跟随设置」的当前值每次打开都重取；
//   节点卡片的摘要写出复算哪张口径卡、几个指标；
// - 设置页「核查」一节：读回各项，改了之后整组写 verify（不限存 null），叫法和服务端 VERIFY_SETTING_LABEL 一字不差；
// - 390px 窄屏不横向溢出，深浅色都读得清，键盘能操作。
//
// 页面都跑在预览页（/preview.html?syn=…，用例在 frontend/src/dev/fixtures/dual_check.tsx，夹具 dual_check.json 两边
// 共用）和设置页上，接口一律 page.route 伪造：完整对照的工件、审批回复、设置的读写都在浏览器里答，别的非 GET 拦掉，
// 不碰真实的写接口。文案对照那一节不开浏览器：读 backend/app/api/settings.py、backend/app/engine/labels.py，terms.ts 借 vite
// 转译后直接 import。
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

await section('双盲复算检查器的叫法和服务端校验报错一致', async () => {
  // 校验问题挂在字段下方、出现在问题面板里时，报错引用的是 labels.py 的 DUAL_CHECK_FIELD_LABEL（期 4 从 schema.py 挪过去，
  // field_label 按节点类型也说这一套）：检查器上得是同一个名字，人才找得到是哪一项。处置的取值：报错里用短说法，下拉的
  // 选项以它开头、后面可以带一句用途（labels.py 的约定）
  const { DUAL_CHECK_TEXT: D } = await import(await load('/src/lib/terms.ts'))
  const KEYS = ['card', 'metrics', 'definitions', 'provider', 'model', 'tolerance', 'on_reject', 'enabled']
  check('检查器的字段名按配置键放在 DUAL_CHECK_TEXT.field', KEYS.every((k) => typeof D.field?.[k] === 'string' && D.field[k]),
    KEYS.filter((k) => !D.field?.[k]).join('、'))
  check('……模型的两项照「裁判模型 · 接入」「裁判模型」的叫法', D.field.provider === '盲算模型 · 接入' && D.field.model === '盲算模型',
    `${D.field.provider} / ${D.field.model}`)
  const py = readFileSync(`${root}backend/app/engine/labels.py`, 'utf8')
  const block = py.match(/^DUAL_CHECK_FIELD_LABEL: dict\[str, str\] = \{([\s\S]*?)^\}/m)?.[1]
  check('从 labels.py 读出了 DUAL_CHECK_FIELD_LABEL', block != null)
  const labels = Object.fromEntries([...(block ?? '').matchAll(/"([^"]+)":\s*"([^"]+)"/g)].map((m) => [m[1], m[2]]))
  const wrong = KEYS.filter((k) => labels[k] !== D.field[k])
  check('每个字段的叫法和 DUAL_CHECK_FIELD_LABEL 一字不差', !wrong.length && Object.keys(labels).length === KEYS.length,
    wrong.map((k) => `${k}：${D.field[k]} ≠ ${labels[k]}`).join('；'))
  const opts = py.match(/^DUAL_CHECK_ON_REJECT_LABEL: dict\[str, str\] = \{([^}]*)\}/m)?.[1] ?? ''
  const short = Object.fromEntries([...opts.matchAll(/"([^"]+)":\s*"([^"]+)"/g)].map((m) => [m[1], m[2]]))
  check('「未采用原流程时」的选项以服务端的短说法开头', Object.keys(short).length === 2
    && Object.entries(short).every(([k, v]) => D.onReject?.[k]?.startsWith(v)), JSON.stringify(short))
})

// ---------------------------------------------------------------- 页面

const browser = await chromium.launch({ executablePath: CHROME })

/**
 * 开一个预览用例。完整对照的工件按 artifact 答（默认夹具里的那份；给函数可以延迟、出错），这次运行的图快照按 graph
 * 答（默认夹具里的图：双盲复算节点没写「未采用原流程时」，按缺省的计入缺口），审批回复记下请求体、回一个伪造的运行；
 * 审批列表回空；别的非 GET 一律拦掉，别的 GET 放行。query 接在预览页地址后面（用例认的参数见 dual_check.tsx）
 */
async function preview(syn, { width = 1280, height = 900, theme = 'dark', artifact = null, graph = null, extra = null, query = '' } = {}) {
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
    if (url.pathname === `/api/runs/${AP.run_id}/graph`) {
      const out = graph ? await graph() : { json: { graph: fx.graph, workflow_id: null, version: null } }
      return route.fulfill({ status: out.status ?? 200, json: out.json })
    }
    if (url.pathname === '/api/approvals') return route.fulfill({ json: [] })
    return route.continue()
  })
  await page.goto(`${WEB}/preview.html?syn=${syn}&theme=${theme}&actor=张工${query ? `&${query}` : ''}`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector(`[data-fixture="${syn}"]`)
  return { ctx, page, sent, errors }
}

const metric = (page, id) => page.locator(`[data-dual-metric="${id}"]`)
/** 弹出的提示（toast 的播报区）里的文字：卡上的后果说明里也有「运行继续」，不能拿整页的文字比 */
const toastOf = (page) => text(page.locator('[role="status"][aria-live="polite"]'))

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
  check('……兜底的原话不再列成一条「具体差异」，也不另附一遍', await rr.locator('[data-dual-diff-item]').count() === 0
    && await rr.locator('[data-dual-fallback-note]').count() === 0)
  // 销售额只差按哪一列筛时间（一条 time）：夹具的 payload 只有约定里的几项（没有服务端的预告），卡上只看得到差异的
  // 方面——服务端会把采用的一边记成该表的业务日期，可同样一条 time 也可能是只有一边按时间筛（服务端不记），只说必要条件
  const note = await text(gmv.locator('[data-dual-catalog]'))
  check('只差在时间列的指标：说明两边按不同的列筛时间才会记成该表的业务日期，只说必要条件', note.startsWith('仅当')
    && note.includes('业务日期') && note.includes('记入数据目录') && note.includes('只有一边按时间筛时不记')
    && note.includes('两边都不对') && note.includes('不覆盖') && await rr.locator('[data-dual-catalog]').count() === 0, note)
  await page.waitForSelector('[data-dual-consequence][data-on-reject="degrade"]')
  const after = await text(page.locator('[data-dual-consequence]'))
  check('正式运行：后果写明报告里的数仍取自原流程、采用盲算或都不对会让出具降档', after.includes('报告中的数仍取自原流程') && after.includes('出具降档'), after)
  check('……节点按缺省（计入缺口）处置：不再挂「判为失败会终止」那句', !after.includes('判为失败') && !after.includes('终止'), after)
  check('没有发出任何写请求', sent.length === 0, JSON.stringify(sent))
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await shot(page, 'approval-dark')
})

/** 一块里高亮的行：原文两栏（data-dual-sql）和规范化后的两栏（data-dual-norm）各自的 <mark> 文字 */
const marksIn = (loc) => loc.evaluate((el) => {
  const pick = (sel) => [...el.querySelectorAll(sel)].map((m) => m.textContent.trim())
  return {
    del: pick('[data-dual-sql="original"] [data-diff-line="del"]'), add: pick('[data-dual-sql="blind"] [data-diff-line="add"]'),
    normDel: pick('[data-dual-norm="original"] [data-diff-line="del"]'), normAdd: pick('[data-dual-norm="blind"] [data-diff-line="add"]'),
  }
})
/** text_diff 里两边各有哪几行（去掉「- 」「+ 」和首尾空白） */
const sidesOf = (diff) => ({
  del: diff.text_diff.filter((l) => l.startsWith('- ')).map((l) => l.slice(2).trim()),
  add: diff.text_diff.filter((l) => l.startsWith('+ ')).map((l) => l.slice(2).trim()),
})

await section('审批卡：并排对照与高亮（服务端真实的差异输出）', async () => {
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
  // 夹具的 text_diff 是 explain_sql_diff 的真实输出：规范化后列名带表名、两个条件并成一行，和原文的任何一行都对不上
  const real = sidesOf(fx.card.metrics[0].diff)
  const rawLines = new Set(fx.card.metrics[0].original.inputs[0].sql.split('\n').map((l) => l.trim()))
  check('（夹具）text_diff 是规范化后的行，和原文逐行对不上', real.del.length > 0 && !real.del.some((l) => rawLines.has(l)), real.del.join(' | '))
  const marks = await marksIn(gmv)
  check('原文按行比：不同的行在原流程那边高亮', marks.del.join('|') === "WHERE paid_at >= '2026-09-01'|AND paid_at < '2026-09-08'", marks.del.join('|'))
  check('……在盲算那边也高亮', marks.add.join('|') === "WHERE created_at >= '2026-09-01'|AND created_at < '2026-09-08'", marks.add.join('|'))
  check('……相同的行不高亮', !(await orig.locator('[data-diff-line]').allInnerTexts()).some((t) => t.includes('FROM orders')))
  check('text_diff 的行分两栏高亮：原流程一栏是「- 」行', marks.normDel.join('|') === real.del.join('|'), marks.normDel.join('|'))
  check('……盲算一栏是「+ 」行', marks.normAdd.join('|') === real.add.join('|'), marks.normAdd.join('|'))
  const hl = await gmv.locator('[data-dual-norm] [data-diff-line]').first().evaluate((el) => getComputedStyle(el).backgroundColor)
  check('……高亮行有底色', !!hl && hl !== 'rgba(0, 0, 0, 0)' && hl !== 'transparent', hl)
  const o = await orig.boundingBox()
  const b = await blind.boundingBox()
  check('宽栏里左右并排：同一行、盲算在右边', !!o && !!b && Math.abs(o.y - b.y) < 2 && b.x >= o.x + o.width - 1, JSON.stringify({ o, b }))
  const no = await gmv.locator('[data-dual-norm="original"]').boundingBox()
  const nb = await gmv.locator('[data-dual-norm="blind"]').boundingBox()
  check('……规范化后的两栏也左右并排', !!no && !!nb && Math.abs(no.y - nb.y) < 2 && nb.x >= no.x + no.width - 1, JSON.stringify({ no, nb }))
  // 盲算的说明按核查 agent 组装的理由文档画（夹具里是 compose_doc 的真实输出）：有出处的数字照证据画实线、挂盲算自己的
  // 查询编号，裸数字画点状线、句末挂「无证据」，下面再醒目地说一句；退款率的输入是老工件，没有理由文档，照旧纯文本
  const why = gmv.locator('[data-dual-rationale="doc"]')
  check('盲算的说明按理由文档画', await why.count() === 1 && (await text(why)).includes('销售额按下单时间 created_at 归属到当周')
    && (await text(why)).includes('51,388.3'), await text(why))
  const cite = why.locator('[data-challenge-cite="Q1"]')
  check('……有出处的数字画实线、挂查询编号，不是按钮（没有可跳的查询）', await cite.count() === 1
    && (await cite.getAttribute('data-ev-line')) === 'solid' && (await cite.evaluate((el) => el.tagName)) === 'SPAN'
    && (await cite.locator('sup').innerText()).trim() === 'Q1' && (await cite.getAttribute('title') ?? '').includes('Q1'), await cite.getAttribute('title'))
  const bare = await why.locator('.ev-mark[data-ev-state="none"]').allInnerTexts()
  check('……裸数字画点状线、挂「无证据」', JSON.stringify(bare.map((t) => t.replace(/\s*（无证据）$/, ''))) === '["1200"]'
    && (await why.locator('[data-ev-tag="none"]').innerText()).includes('无证据'), JSON.stringify(bare))
  check('……下面醒目地说一句没有出处', (await text(why.locator('[data-dual-rationale-bare]'))) === '理由中标出的数字没有引用盲算自己的查询结果，无法核对出处')
  const plain = metric(page, 'refund_rate').locator('[data-dual-rationale="text"]')
  check('老工件没有理由文档：照旧显示纯文本', await plain.count() === 1 && (await text(plain)) === '盲算的说明：退款笔数：订单表中能找到对应订单的退款记录数',
    await plain.count() ? await text(plain) : '没有')
  check('输入取值：路径、两边的值', (await text(gmv.locator('[data-dual-input="vars.kpi.gmv"]'))).includes('45,678.5')
    && (await text(gmv.locator('[data-dual-input="vars.kpi.gmv"]'))).includes('51,388.3'))
  const rr = metric(page, 'refund_rate')
  check('两个输入、只有一个不同：只对照不同的那一个', (await rr.locator('[data-dual-pair]').evaluateAll((els) => els.map((e) => e.dataset.dualPair))).join(',')
    === 'vars.kpi.refunds')
  check('……输入取值两项都列，不同的那项标出来', await rr.locator('[data-dual-input]').count() === 2
    && await rr.locator('[data-dual-input="vars.kpi.refunds"][data-differs]').count() === 1
    && await rr.locator('[data-dual-input="vars.kpi.orders"][data-differs]').count() === 0)
  // 退款这一项两边写法差得多（EXISTS 对 IN）：原文只有前两行相同，其余各自高亮；规范化后的行照样分两栏
  const rm = await marksIn(rr)
  const rreal = sidesOf(fx.card.metrics[1].diff)
  check('……原文按行比：相同的前两行不标，其余各自高亮', rm.del.length === 4 && rm.add.join('|') === 'WHERE r.order_id IN (SELECT id FROM orders)'
    && !rm.del.concat(rm.add).some((t) => t.startsWith('SELECT COUNT') || t.startsWith('FROM refunds')), `${rm.del.join('|')} ‖ ${rm.add.join('|')}`)
  check('……规范化后的行两栏各自高亮（「- 」「+ 」交错给的也分得开）', rm.normDel.join('|') === rreal.del.join('|') && rm.normAdd.join('|') === rreal.add.join('|'),
    `${rm.normDel.join('|')} ‖ ${rm.normAdd.join('|')}`)
  await page.keyboard.press('Enter')
  check('再按一次回车收起', (await toggle.getAttribute('aria-expanded')) === 'false' && await gmv.locator('[data-dual-compare]').count() === 0)
  check('页面没有报错', errors.length === 0, errors.join('；'))
})

/**
 * 把「销售额」换成 variants 里的一对 SQL 及其真实的差异说明（取值照旧不同），别的指标原样；diff 给了就用给的
 * （拼几个输入的、兜底的原话）。peer 给了就把「退款率」对不上的那个输入同样换成 peers 里的一对 SQL
 */
function cardWith(name, patch = {}) {
  const v = fx.variants[name]
  const card = JSON.parse(JSON.stringify(fx.card))
  const gmv = card.metrics[0]
  gmv.original.inputs[0].sql = v?.sql[0] ?? gmv.original.inputs[0].sql
  gmv.blind.inputs[0].sql = v?.sql[1] ?? gmv.blind.inputs[0].sql
  gmv.diff = JSON.parse(JSON.stringify(v?.diff ?? gmv.diff))
  Object.assign(gmv, patch.metric ?? {})
  if (patch.diff) gmv.diff = patch.diff
  if (patch.peer) {
    const p = fx.peers[patch.peer]
    const rr = card.metrics[1]
    const k = rr.original.inputs.findIndex((i, n) => i.value !== rr.blind.inputs[n].value)
    rr.original.inputs[k].sql = p.sql[0]
    rr.blind.inputs[k].sql = p.sql[1]
    rr.diff = JSON.parse(JSON.stringify(p.diff))
  }
  return async () => ({ json: { id: CARD_ID, content: card } })
}

/** 预告里写进去的说法在卡上的写法：业务日期写列名，其余原样（同 catalogPatch 的 patchValueText） */
const previewValue = (v) => (typeof v === 'string' ? v : v?.column)

await section('审批卡：没有预告时，「记入数据目录」只说必要条件，服务端不写的几种照实说', async () => {
  // 老服务端的 payload 没有预告（payload.summary[].catalog），卡上只看得到差异的方面和原话。可能写目录的形状同
  // dual_check_facts.derive 的门槛（只有一个输入对不上、恰好一条具体差异、没有兜底，而且是时间列、筛选条件或聚合），
  // 过了门槛服务端也不一定写：只有一边按时间筛、多出的条件是指标自己的范围（不是排除无效记录）、采用了不去重的一边都
  // 不写。卡上不能说得比服务端宽——每句只说必要条件（「仅当」开头），这几种不写的照实说，免得审核人为了不让目录被写错
  // 而避开本来对的一边。变体的 catalog 是服务端对这一对 SQL 的真实预告，按它核对
  const { CATALOG_TABLE_FIELD_LABEL: LABEL } = await import(await load('/src/lib/terms.ts'))
  // [变体, 说明, 服务端不写的那一种卡上照实说的那句（只在卡上提数据目录时有）]。变体挑的是收尾版差异说明下各有代表性
  // 的几种（backend/tests/test_dual_check_fixture.py 的模块说明）：同一列上的时间范围不同只给兜底（fallback）
  const cases = [
    ['fallback', '说不清的（同一列上的时间范围不同，只给兜底）'],
    ['two_filters', '差两个筛选条件'],
    ['join', '差在关联方式'],
    ['parse', '一边解析失败'],
    ['time_column', '换了时间列（两列都声明为日期类型）', '只有一边按时间筛时不记'],
    ['time_one_side', '只有一边按时间筛', '只有一边按时间筛时不记'],
    ['filter_extra', '只多一个排除测试单的条件', '只看某个地区、某个渠道这类限定指标范围的条件不记'],
    ['filter_scope', '只多一个渠道条件', '只看某个地区、某个渠道这类限定指标范围的条件不记'],
    ['dedup', '只差去不去重', '采用不去重的一边时不记'],
  ]
  for (const [name, what, unwritten] of cases) {
    const { page, errors } = await preview('dual-check-approval', { artifact: cardWith(name) })
    await page.waitForSelector('[data-dual-check][data-state="ready"]')
    const gmv = metric(page, 'gmv')
    const note = await text(gmv.locator('[data-dual-catalog]'))
    const expect = fx.variants[name].catalog
    const writes = ['a', 'b'].filter((s) => expect[s].write)
    if (!unwritten) {
      // 差异的形状就写不了目录（服务端两边都不写）：卡上不提
      check(`${what}：不说会记入数据目录（服务端两边都不写）`, !note && !writes.length, note || '（无）')
    } else {
      check(`${what}：只说必要条件（「仅当」开头），服务端不写的那一种照实说`, note.startsWith('仅当') && note.includes(unwritten)
        && note.includes('记入数据目录') && note.includes('两边都不对') && note.includes('不覆盖') && note.includes('时间线'), note)
      // 服务端会写的那一边，卡上的必要条件里得说到写的是哪一项
      const items = writes.map((s) => LABEL[expect[s].item])
      check(`……服务端会写的（${items.join('、') || '无'}）卡上说到了`, items.every((i) => note.includes(i)), note)
      // 「不记」后面紧跟一句「记为人工确认的事实」，读起来像在说不记的那一项
      check('……「人工确认」说的是写入的项', note.includes('写入的项记为人工确认的事实') && !note.includes('不记。记为'), note)
    }
    if (name === 'time_column' || name === 'time_one_side' || name === 'dedup') {
      // 同一次裁决里几个指标给同一张表记的业务日期、去重规则不同时，服务端一项都不写（有效记录条件是合成一句写）。
      // 没有预告时卡上看不出别的指标落到哪一项上，照实说这一条
      check('……同一次裁决里几个指标给同一张表记的说法不同时都不记', note.includes('同一次裁决里') && note.includes('不同时都不记'), note)
    }
    if (name === 'time_column') {
      // 服务端两边都会写（各记被采纳那一边的列）：卡上的必要条件说的就是按不同的列筛时间
      check('……两边按不同的列筛时间，采用的一边所用的时间列才记为业务日期', writes.length === 2
        && note.includes('按不同的列筛时间') && note.includes('采用的一边所用的时间列'), note)
    }
    if (name === 'fallback') {
      // 收尾版差异说明归纳不了同一列上的时间范围：卡上写那句固定的兜底说明，默认展开对照，审核人逐行对照两条 SQL
      check('……写那句固定的兜底说明，不当成一条具体差异', (await text(gmv.locator('[data-dual-fallback]'))) === '两条 SQL 的写法不同，未能归纳成具体差异'
        && await gmv.locator('[data-dual-diff-item]').count() === 0, await text(gmv.locator('[data-dual-fallback]')))
      check('……默认展开对照', (await gmv.locator('[data-dual-compare-toggle]').getAttribute('aria-expanded')) === 'true')
    }
    if (name === 'time_one_side') {
      // 正上方那条差异说「盲算没有按时间筛选」：卡上不能再说两边只差在按哪一列筛时间
      check('……不和正上方那条差异自相矛盾', (await text(gmv.locator('[data-dual-diff-item]'))).includes('没有按时间筛选')
        && !note.includes('两边只差') && !note.includes('按哪一列'), note)
    }
    if (name === 'filter_extra' || name === 'filter_scope') {
      check('……多出的条件得只在一边、是在排除无效记录，采用有它的一边才记', note.includes('只在一边')
        && note.includes('排除退款、作废、测试这类无效记录') && note.includes('采用有它的一边'), note)
    }
    if (name === 'dedup') {
      // 服务端：采用去重的原流程记去重规则，采用不去重的盲算不记。两种选择卡上都说清
      check('……采用去重的一边（这里是原流程）才记', expect.a.write && note.includes('采用的是去重的一边'), note)
      check('……采用不去重的一边（这里是盲算）不记', !expect.b.write && note.includes('采用不去重的一边时不记'), note)
    }
    if (name === 'two_filters') {
      check('……两条差异都列出来', await gmv.locator('[data-dual-diff-item]').count() === 2)
    }
    const diff = fx.variants[name].diff
    if (diff.text_diff.length) {
      // 只有兜底说明时对照默认就展开着：再点一下就收起来了
      const toggle = gmv.locator('[data-dual-compare-toggle]')
      if ((await toggle.getAttribute('aria-expanded')) !== 'true') await toggle.click()
      const m = await marksIn(gmv)
      const real = sidesOf(diff)
      check(`……规范化后的行按真实输出分两栏高亮（${real.del.length} + ${real.add.length} 行）`,
        m.normDel.join('|') === real.del.join('|') && m.normAdd.join('|') === real.add.join('|'), `${m.normDel.join('|')} ‖ ${m.normAdd.join('|')}`)
      if (!real.add.length) {
        check('……一边没有不同的行时照实说', (await text(gmv.locator('[data-dual-norm="blind"]'))).includes('没有不同的行'))
      }
    }
    check(`${what}：页面没有报错`, errors.length === 0, errors.join('；'))
  }

  // 几个输入都差在时间列：节点把两处的差异合成一份（每条前面写明是哪个输入），不止一条，不写目录
  const t = fx.variants.time_column.diff.items[0]
  const merged = {
    equivalent: false, fallback: false, text_diff: fx.card.metrics[0].diff.text_diff,
    items: [{ aspect: 'time', text: `输入「gmv_web」：${fx.card.metrics[0].diff.items[0].text}` }, { aspect: 'time', text: `输入「gmv_app」：${t.text}` }],
  }
  const two = await preview('dual-check-approval', { artifact: cardWith(null, { diff: merged }) })
  await two.page.waitForSelector('[data-dual-check][data-state="ready"]')
  check('几个输入都差在同一方面（合并后两条）：不说会记入数据目录', await metric(two.page, 'gmv').locator('[data-dual-catalog]').count() === 0)
})

/**
 * 一块里预告的三条：{a, b, neither} → {text, write, current, reason, joint, together, conflict, joined, pending, inferred}。
 * reason：不写、照服务端的原话说原因；joint：照完整对照里服务端算好的合并预告说；together：和别的指标一起记入；
 * conflict、joined、pending：没有合并预告时按其余指标当前的选择推的（说法不同都不记、有效记录条件合成一句、同项上有
 * 被挡下的指标说不准只说可能）；inferred：这一条是推出来的
 */
const previewClauses = (loc) => loc.evaluate((el) => Object.fromEntries([...el.querySelectorAll('[data-dual-catalog="preview"] [data-dual-catalog-side]')]
  .map((s) => [s.dataset.dualCatalogSide, {
    text: s.textContent.trim(), write: s.dataset.write != null, current: s.dataset.current != null, weight: Number(getComputedStyle(s).fontWeight),
    reason: s.dataset.reason != null, joint: s.dataset.joint != null, together: s.dataset.together != null,
    conflict: s.dataset.conflict != null, joined: s.dataset.joined != null, pending: s.dataset.pending != null,
    inferred: s.dataset.inferred != null,
  }])))
/** 一块预告的结尾：{kind: exact（照服务端算好的说）| inferred（有推出来的）, text} */
const previewTail = async (loc) => {
  const tail = loc.locator('[data-dual-catalog-tail]')
  return { kind: await tail.getAttribute('data-dual-catalog-tail'), text: await text(tail) }
}
/** 不写的一条：选择的叫法加服务端的原话 */
const blockedText = (label, reason) => `${label}时不会记入：${reason}`
const CHOICE_LABEL = { a: '采用原流程的结果', b: '采用盲算的结果', neither: '两边都不对' }

await section('审批卡：服务端预告了写目录时照它说，当前选的那一句加重', async () => {
  // payload.summary[].catalog（约定之外、服务端另带的）：采用原流程、采用盲算各会往数据目录里写什么，和裁决后真写的
  // 是同一套判断（dual_check_facts.preview）。有它就照它说：两边都不写不提数据目录，写的那一边说清哪张表的哪一项记成
  // 什么。夹具里的预告是服务端对这几对 SQL 的真实输出（test_dual_check_fixture.py 核对）
  const { CATALOG_TABLE_FIELD_LABEL: LABEL } = await import(await load('/src/lib/terms.ts'))

  // 口径卡原样：销售额两边换了时间列（选哪边记哪一列），退款率只有兜底（两边都不写）。预告跟着摘要来，完整对照没到也说
  let release
  const gate = new Promise((r) => { release = r })
  const base = await preview('dual-check-approval', {
    query: 'catalog=1', artifact: async () => { await gate; return { json: { id: CARD_ID, content: fx.card } } },
  })
  await base.page.waitForSelector('[data-dual-metric="gmv"] [data-dual-catalog="preview"]')
  const gmv = metric(base.page, 'gmv')
  check('完整对照还没到：已照预告说', (await base.page.locator('[data-dual-check]').getAttribute('data-state')) === 'loading')
  let c = await previewClauses(gmv)
  check('两边换了时间列：采用原流程会记为原流程那一列', c.a?.text === '采用原流程的结果时，「orders」（shop）的业务日期会记为「paid_at」' && c.a.write, c.a?.text)
  check('……采用盲算会记为盲算那一列（同一项，省掉哪张表的哪一项）', c.b?.text === '采用盲算的结果时会记为「created_at」' && c.b.write, c.b?.text)
  check('……两边都不对时不记', c.neither?.text === '两边都不对时不记' && !c.neither.write, c.neither?.text)
  check('……三个选择逐条分行列出', await gmv.locator('[data-dual-catalog="preview"] li[data-dual-catalog-side]').count() === 3)
  const said = await text(gmv.locator('[data-dual-catalog]'))
  check('……开头说记入数据目录，结尾说以时间线为准，不再用有条件的说法', said.startsWith('记入数据目录：') && said.includes('人工确认')
    && said.includes('时间线') && !said.includes('仅当'), said)
  check('退款率（服务端两边都不写）：不提数据目录', await metric(base.page, 'refund_rate').locator('[data-dual-catalog]').count() === 0)
  release()
  await base.page.waitForSelector('[data-dual-check][data-state="ready"]')
  check('完整对照到了：照样按预告说，不换成有条件的说法', await gmv.locator('[data-dual-catalog="preview"]').count() === 1
    && await base.page.locator('[data-dual-catalog="time"]').count() === 0)
  check('没选之前三句都不加重', !c.a.current && !c.b.current && !c.neither.current)
  for (const [choice, label] of [['a', '采用原流程的结果'], ['b', '采用盲算的结果'], ['neither', '两边都不对']]) {
    await gmv.locator('label', { hasText: label }).click()
    c = await previewClauses(gmv)
    const others = ['a', 'b', 'neither'].filter((s) => s !== choice)
    check(`选「${label}」：这一句加重，另两句不加重`, c[choice].current && c[choice].weight >= 500
      && others.every((s) => !c[s].current && c[s].weight < 500), JSON.stringify(c))
  }
  await shot(base.page, 'approval-preview-dark')

  // 几个变体：预告说两边都不写的，卡上就不提（没有预告时这几种会有条件地提）；写的那一边说清写到哪、写成什么
  for (const [name, what] of [['time_column', '换了时间列'], ['time_one_side', '只有一边按时间筛'], ['filter_scope', '只多一个渠道条件'],
    ['filter_extra', '只多一个排除测试单的条件'], ['dedup', '只差去不去重'], ['fallback', '说不清的（只给兜底）']]) {
    const { page, errors } = await preview('dual-check-approval', { query: `variant=${name}&catalog=1`, artifact: cardWith(name) })
    await page.waitForSelector('[data-dual-check][data-state="ready"]')
    const m = metric(page, 'gmv')
    const expect = fx.variants[name].catalog
    const writes = ['a', 'b'].filter((s) => expect[s].write)
    if (fx.variants[name].diff.items.some((i) => i.aspect !== 'other' && i.aspect !== 'parse')) {
      check(`${what}（变体）：摘要换成这一对 SQL 的差异`, (await text(m.locator('[data-dual-diff-item]'))) === fx.variants[name].diff.items[0].text,
        await text(m.locator('[data-dual-diff-item]')))
    } else {
      check(`${what}（变体）：摘要换成兜底说明，不当成一条具体差异`, (await text(m.locator('[data-dual-fallback]'))) === '两条 SQL 的写法不同，未能归纳成具体差异'
        && await m.locator('[data-dual-diff-item]').count() === 0, await text(m.locator('[data-dual-fallback]')))
    }
    if (!writes.length) {
      check(`${what}：服务端两边都不写，卡上不提数据目录`, await m.locator('[data-dual-catalog]').count() === 0, await text(m.locator('[data-dual-catalog]')))
    } else {
      const got = await previewClauses(m)
      // 两边都写、写的是同一张表的同一项（换了时间列：同一张表的业务日期）时，后一条省掉哪张表的哪一项
      const same = writes.length === 2 && ['source', 'table', 'item'].every((k) => expect.a[k] === expect.b[k])
      const ok = ['a', 'b'].every((s) => (expect[s].write
        ? got[s]?.write && got[s].text.includes(`会记为「${previewValue(expect[s].value)}」`)
          && (s === 'b' && same ? got[s].text === `${CHOICE_LABEL.b}时会记为「${previewValue(expect.b.value)}」` : got[s].text.includes(LABEL[expect[s].item]))
        : !got[s]?.write && got[s]?.reason && got[s].text === blockedText(CHOICE_LABEL[s], expect[s].reason)))
      check(`${what}：写的一边说清写到哪、会记为什么，不写的一边照服务端的原话说不会记入的原因`, ok, `${got.a?.text} ‖ ${got.b?.text}`)
      if (name === 'time_column') {
        // 两列都声明为日期类型：选哪一边都记被采纳那一边的列，加重的那一条跟着选择走
        await m.locator('label', { hasText: '采用盲算的结果' }).click()
        const cur = await previewClauses(m)
        check('换了时间列，选盲算：加重的那一条说业务日期会记为盲算那一列', cur.b?.current && cur.b.write && !cur.a.current
          && cur.b.text === `采用盲算的结果时会记为「${previewValue(expect.b.value)}」`
          && cur.a.text === `采用原流程的结果时，「orders」（shop）的业务日期会记为「${previewValue(expect.a.value)}」`, `${cur.a?.text} ‖ ${cur.b?.text}`)
      }
      if (name === 'dedup') {
        // 采用去重的原流程记去重规则，采用不去重的盲算不记：两种选择分别看加重的那一句
        await m.locator('label', { hasText: '采用原流程的结果' }).click()
        let cur = await previewClauses(m)
        check('只差去不去重，选原流程（去重）：加重的那一条说会记为去重规则', cur.a.current
          && cur.a.text === '采用原流程的结果时，「orders」（shop）的去重规则会记为「对 user_id 计数时去重」', cur.a.text)
        await m.locator('label', { hasText: '采用盲算的结果' }).click()
        cur = await previewClauses(m)
        check('……选盲算（不去重）：加重的那一条说不会记入、为什么', cur.b.current && cur.b.reason
          && cur.b.text === blockedText('采用盲算的结果', expect.b.reason) && cur.b.text.includes('不去重是这个指标自己的算法') && !cur.a.current, cur.b.text)
      }
    }
    check(`${what}：页面没有报错`, errors.length === 0, errors.join('；'))
  }

  // 预告认不出（老版本、写坏了）：不照它说，回到按差异形状有条件地说
  const bad = await preview('dual-check-approval', { query: 'catalog=bad' })
  await bad.page.waitForSelector('[data-dual-check][data-state="ready"]')
  const fallback = await text(metric(bad.page, 'gmv').locator('[data-dual-catalog]'))
  check('预告认不出：回到有条件的说法', (await metric(bad.page, 'gmv').locator('[data-dual-catalog]').getAttribute('data-dual-catalog')) === 'time'
    && fallback.startsWith('仅当'), fallback)

  // 完整对照取不到：预告跟着摘要来，照样说
  const lost = await preview('dual-check-approval', { query: 'catalog=1', artifact: async () => ({ status: 404, json: { detail: '工件不存在' } }) })
  await lost.page.waitForSelector('[data-dual-load-failed]')
  check('完整对照取不到：照预告说', (await previewClauses(metric(lost.page, 'gmv'))).a?.write === true)

  // 窄屏、深浅色：预告里的条件、取值很长也不撑宽；平常那几句和加重的那一句都读得清
  for (const theme of ['dark', 'light']) {
    const narrow = await preview('dual-check-approval', {
      query: 'variant=filter_extra&catalog=1', artifact: cardWith('filter_extra'), width: 390, height: 844, theme,
    })
    await narrow.page.waitForSelector('[data-dual-check][data-state="ready"]')
    await metric(narrow.page, 'gmv').locator('label', { hasText: '采用原流程的结果' }).click()
    const w = await narrow.page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    check(`390px（${theme}）：预告不横向溢出`, w <= 0, `${w}px`)
    const c = await narrow.page.evaluate(() => {
      const base = getComputedStyle(document.body).backgroundColor
      const bgOf = (el) => {
        for (let n = el; n && n !== document.documentElement; n = n.parentElement) {
          const v = getComputedStyle(n).backgroundColor
          if (v !== 'rgba(0, 0, 0, 0)' && v !== 'transparent') return v
        }
        return 'rgba(0, 0, 0, 0)'
      }
      const pick = (sel) => { const el = document.querySelector(sel); return el && { fg: getComputedStyle(el).color, bg: bgOf(el) } }
      return { base, note: pick('[data-dual-catalog="preview"]'), current: pick('[data-dual-catalog-side][data-current]') }
    })
    for (const key of ['note', 'current']) {
      const v = c[key]
      const ratio = v ? contrast(v.fg, v.bg, c.base) : 0
      check(`${theme}：${key === 'note' ? '预告' : '预告里加重的那一句'}对比度 ≥ 4.5`, ratio >= 4.5, `${ratio.toFixed(2)}（${v?.fg} on ${v?.bg}）`)
    }
    await shot(narrow.page, `approval-preview-narrow-${theme}`)
  }
})

await section('审批卡：被数据目录挡下的一边照样说写到哪、不会记入的原因照服务端的原话说', async () => {
  // 目录里已有人工确认的业务日期（夹具 catalog_blocked：服务端按那时的目录算的真实预告）：两边都认出了事实、都被目录
  // 挡下。以前预告只给 {write: false}，卡上一句「不记」，看不出是因为目录里已经有了；现在逐条说「不会记入：原因」，
  // 原因和裁决后时间线上跳过的原因一字不差（test_dual_check_node 的图级测试核对）
  const said = fx.catalog_blocked.catalog
  check('夹具：两边都被挡下，各带写到哪和原因', ['a', 'b'].every((s) => said[s].write === false && said[s].table === 'orders'
    && said[s].item === 'business_date' && !!said[s].reason), JSON.stringify(said))
  const { page, errors } = await preview('dual-check-approval', { query: 'catalog=blocked' })
  await page.waitForSelector('[data-dual-check][data-state="ready"]')
  const gmv = metric(page, 'gmv')
  const c = await previewClauses(gmv)
  check('采用原流程：说不会记入，原因是服务端的原话（已是人工确认过的同一列）', c.a?.text === blockedText(CHOICE_LABEL.a, said.a.reason)
    && c.a.reason && !c.a.write && c.a.text.includes('无需重复写入'), c.a?.text)
  check('……采用盲算：同样照原话说（已有不同的说法，未覆盖）', c.b?.text === blockedText(CHOICE_LABEL.b, said.b.reason)
    && c.b.reason && !c.b.write && c.b.text.includes('未覆盖'), c.b?.text)
  check('……两边都不对时不记', c.neither?.text === '两边都不对时不记' && !c.neither.write, c.neither?.text)
  check('……三条都没有「会记为」', !Object.values(c).some((x) => x.text.includes('会记为')), JSON.stringify(c))
  await gmv.locator('label', { hasText: '采用盲算的结果' }).click()
  const cur = await previewClauses(gmv)
  check('选盲算：加重的是「不会记入」那一条', cur.b?.current && cur.b.weight >= 500 && !cur.a.current, JSON.stringify(cur.b))
  check('页面没有报错', errors.length === 0, errors.join('；'))
  for (const theme of ['dark', 'light']) {
    const narrow = await preview('dual-check-approval', { query: 'catalog=blocked', width: 390, height: 844, theme })
    await narrow.page.waitForSelector('[data-dual-check][data-state="ready"]')
    const w = await narrow.page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    check(`390px（${theme}）：原因很长也不横向溢出`, w <= 0, `${w}px`)
    await shot(narrow.page, `approval-blocked-narrow-${theme}`)
  }
})

await section('审批卡：几个指标落到同一项上、工件里没有合并预告（老服务端）时，按其余指标当前的选择推', async () => {
  // 预告是每个指标单独算的，服务端写目录前却把同一次裁决里落到同一张表同一项上的说法归拢（dual_check_facts._merged）：
  // 业务日期、去重规则说法不同的一项都不写，有效记录条件用 AND 合成一句写。两个指标分开选时卡上要是各自加粗说
  // 「记为……」，审核人会以为同一张表要记两个业务日期，可能为了避开它把本该分开的选择改成一致（2026-10 第 3 轮复核）。
  // 卡上按其余指标当前的选择说，选择变了说法跟着变。夹具 merges 是这几种组合在服务端真写一遍的结果
  // （test_dual_check_fixture.py 用 record_choices 核对），peers 换掉「退款率」，和「销售额」落到同一项上。这一节的工件
  // 不带服务端算好的合并预告（catalog_joint，期 4 起才有）：卡上照旧规矩推；带了的见下一节
  const LABEL = { a: '采用原流程的结果', b: '采用盲算的结果', neither: '两边都不对' }
  const open = async (name, opts = {}) => {
    const m = fx.merges[name]
    const query = ['catalog=1', `peer=${m.refund_rate}`, ...(m.gmv ? [`variant=${m.gmv}`] : [])].join('&')
    const out = await preview('dual-check-approval', { query, artifact: cardWith(m.gmv, { peer: m.refund_rate }), ...opts })
    await out.page.waitForSelector('[data-dual-check][data-state="ready"]')
    return out
  }
  const choose = (page, id, c) => metric(page, id).locator('label', { hasText: LABEL[c] }).click()
  const chooseAll = async (page, name) => {
    for (const [id, c] of Object.entries(fx.merges[name].choices)) await choose(page, id, c)
  }
  /** 两个指标当前选中的那一句 */
  const current = async (page) => Object.fromEntries(await Promise.all(['gmv', 'refund_rate'].map(async (id) => (
    [id, Object.values(await previewClauses(metric(page, id))).find((x) => x.current) ?? null]))))

  // 业务日期：销售额采用原流程（paid_at）、退款率采用盲算（created_at）
  check('夹具：分开选时服务端一项都不写（record_choices 的真实结果）', fx.merges.time_split.written.length === 0)
  const { page, errors } = await open('time_split')
  let g = await previewClauses(metric(page, 'gmv'))
  check('还没选：退款率没有选择，销售额照自己的预告说', g.a?.text === '采用原流程的结果时，「orders」（shop）的业务日期会记为「paid_at」'
    && g.a.write && !g.a.conflict, g.a?.text)
  await choose(page, 'gmv', 'a')
  let r = await previewClauses(metric(page, 'refund_rate'))
  check('销售额选了原流程：退款率选原流程（同一列）照常记', r.a?.text === '采用原流程的结果时，「orders」（shop）的业务日期会记为「paid_at」'
    && r.a.write && !r.a.conflict, r.a?.text)
  check('……退款率选盲算（另一列）说清和销售额说法不同、都不记，不再说「记为」', !!r.b?.conflict && !r.b.write
    && r.b.text.includes('「销售额」') && r.b.text.endsWith('都不记') && !r.b.text.includes('记为'), r.b?.text)
  await chooseAll(page, 'time_split')
  let cur = await current(page)
  check('分开选：两边加粗的那一句都说都不记，和服务端一致', ['gmv', 'refund_rate'].every((id) => cur[id]?.conflict && !cur[id].write
    && cur[id].weight >= 500), JSON.stringify(cur))
  check('……销售额那一句写明是哪张表的哪一项、和哪个指标说法不同',
    cur.gmv?.text === '采用原流程的结果时，「orders」（shop）的业务日期与「退款率」的选择说法不同，都不记', cur.gmv?.text)
  check('……退款率那一句是同一项，不再重复写表名', cur.refund_rate?.text === '采用盲算的结果时与「销售额」的选择说法不同，都不记',
    cur.refund_rate?.text)
  g = await previewClauses(metric(page, 'gmv'))
  check('……销售额那一块里，改选盲算（和退款率同一列）的那一句照常说记', g.b?.text === '采用盲算的结果时会记为「created_at」'
    && g.b.write && !g.b.conflict, g.b?.text)
  const note = await text(metric(page, 'gmv').locator('[data-dual-catalog]'))
  check('……结尾说清预告按各指标当前的选择算，「人工确认」说的是写入的项', note.includes('各指标当前的选择')
    && note.includes('写入的项记为人工确认的事实') && !note.includes('不记。记为'), note)
  const tail = await previewTail(metric(page, 'gmv'))
  check('……推出来的：结尾不说只有目录被改了才会不一致，说实际写入以时间线上的记录为准', tail.kind === 'inferred'
    && tail.text.includes('推断') && tail.text.endsWith('实际写入以时间线上的记录为准') && !tail.text.includes('审批期间数据目录有改动时'),
    `${tail.kind} / ${tail.text}`)
  // 退款率改选两边都不对：不再落到同一项上，销售额照常记
  await choose(page, 'refund_rate', 'neither')
  g = await previewClauses(metric(page, 'gmv'))
  check('退款率改选两边都不对：销售额加粗的那一句回到记为「paid_at」', g.a?.current && g.a.write && !g.a.conflict
    && g.a.text.endsWith('记为「paid_at」'), g.a?.text)
  check('页面没有报错', errors.length === 0, errors.join('；'))

  // 选同一边、落到同一列：服务端写一次
  const same = await open('time_same')
  await chooseAll(same.page, 'time_same')
  cur = await current(same.page)
  const sameValue = previewValue(fx.merges.time_same.written[0]?.value)
  check('选同一列：两边加粗的那一句都说记为这一列，和服务端写的一致', fx.merges.time_same.written.length === 1
    && ['gmv', 'refund_rate'].every((id) => cur[id]?.write && !cur[id].conflict && cur[id].text.endsWith(`记为「${sameValue}」`)), JSON.stringify(cur))

  // 去重规则：两个指标都采用去重的一边，去重的列不同（user_id、id）
  const dd = await open('dedup_columns')
  await chooseAll(dd.page, 'dedup_columns')
  cur = await current(dd.page)
  check('去重的列不同：服务端一项都不写，两边加粗的那一句都说都不记', fx.merges.dedup_columns.written.length === 0
    && cur.gmv?.text === '采用原流程的结果时，「orders」（shop）的去重规则与「退款率」的选择说法不同，都不记'
    && cur.refund_rate?.text === '采用原流程的结果时，「orders」（shop）的去重规则与「销售额」的选择说法不同，都不记'
    && !cur.gmv.write && !cur.refund_rate.write, `${cur.gmv?.text} ‖ ${cur.refund_rate?.text}`)

  // 有效记录条件：两个指标各自多出的条件不同（status <> 'test'、status <> 'cancelled'），服务端合成一句写
  const ff = await open('filters')
  await chooseAll(ff.page, 'filters')
  cur = await current(ff.page)
  const own = { gmv: fx.variants.filter_extra.catalog.a.value, refund_rate: fx.peers.filter.catalog.a.value }
  const merged = fx.merges.filters.written[0]?.value ?? ''
  check('夹具：服务端把两边的条件合成一句写', own.gmv !== own.refund_rate && merged.includes(own.gmv) && merged.includes(own.refund_rate), merged)
  check('有效记录条件不同：两边加粗的那一句都说记、并和另一个指标采用的条件合成一句',
    cur.gmv?.text === `采用原流程的结果时，「orders」（shop）的有效记录条件会记为「${own.gmv}」，与「退款率」采用的条件用 AND 合成一句`
    && cur.refund_rate?.text === `采用原流程的结果时，「orders」（shop）的有效记录条件会记为「${own.refund_rate}」，与「销售额」采用的条件用 AND 合成一句`
    && cur.gmv.write && cur.gmv.joined && cur.refund_rate.write && cur.refund_rate.joined, `${cur.gmv?.text} ‖ ${cur.refund_rate?.text}`)

  // 被目录挡下的同项指标也算进来（2026-10 复核）：目录里人工驳回过业务日期 created_at（夹具 time_rejected）。销售额采用
  // 原流程（paid_at，单独看会写），退款率采用盲算（created_at，单独看被驳回挡下，但带着写到哪）。服务端先归拢：说法不同，
  // 一项都不写，轮不到对目录。以前只看会写的同伴，销售额照样加粗说「会记为 paid_at」
  const tr = fx.merges.time_rejected
  check('夹具：退款率那一边被驳回挡下、带着写到哪，销售额那一边单独看会写，服务端一项都不写',
    tr.catalog.refund_rate.b.write === false && tr.catalog.refund_rate.b.table === 'orders' && tr.catalog.gmv.a.write === true
    && tr.written.length === 0, JSON.stringify(tr.catalog))
  const rj = await preview('dual-check-approval', {
    query: `merge=time_rejected&peer=${tr.refund_rate}`, artifact: cardWith(tr.gmv, { peer: tr.refund_rate }),
  })
  await rj.page.waitForSelector('[data-dual-check][data-state="ready"]')
  await chooseAll(rj.page, 'time_rejected')
  cur = await current(rj.page)
  check('……两边加粗的那一句都说说法不同、都不记，销售额不再说「会记为」', ['gmv', 'refund_rate'].every((id) => cur[id]?.conflict
    && !cur[id].write && cur[id].inferred && !cur[id].text.includes('会记为')), JSON.stringify(cur))
  check('……退款率被挡下的那一句照归拢的结果说（和销售额说法不同），不照它单独的驳回原因说',
    cur.refund_rate?.text === '采用盲算的结果时与「销售额」的选择说法不同，都不记', cur.refund_rate?.text)
  const rjTail = await previewTail(metric(rj.page, 'gmv'))
  check('……结尾换成实际写入以时间线上的记录为准', rjTail.kind === 'inferred' && rjTail.text.endsWith('实际写入以时间线上的记录为准'), rjTail.text)

  // 有效记录条件、同项上有被挡下的指标（夹具 filters_rejected：销售额采纳的条件曾被驳回）：挡下它的若是驳回，服务端
  // 拿掉它、退款率照写；若是别的原因（和已有条件合不起来），退款率也一起挡下。卡上认不出是哪一种，退款率那一句只说可能
  const fr = fx.merges.filters_rejected
  const fp = await preview('dual-check-approval', {
    query: `merge=filters_rejected&peer=${fr.refund_rate}&variant=${fr.gmv}`, artifact: cardWith(fr.gmv, { peer: fr.refund_rate }),
  })
  await fp.page.waitForSelector('[data-dual-check][data-state="ready"]')
  await chooseAll(fp.page, 'filters_rejected')
  cur = await current(fp.page)
  check('有效记录条件、同项上有被挡下的：退款率说可能记为自己的条件、也可能不记，不说定了「会记为」', !!cur.refund_rate?.pending
    && !cur.refund_rate.write && cur.refund_rate.inferred && cur.refund_rate.text === '采用原流程的结果时，「orders」（shop）的有效记录条件'
      + `可能记为「${fr.catalog.refund_rate.a.value}」，也可能不记：要与「销售额」的选择合起来才能确定`, cur.refund_rate?.text)
  check('……销售额（被驳回挡下）照它的原因说不会记入，和服务端一致（确实没写）', !!cur.gmv?.reason && !cur.gmv.write
    && cur.gmv.text === blockedText(CHOICE_LABEL.a, fr.catalog.gmv.a.reason)
    && !fr.written.some((w) => w.value === fr.catalog.gmv.a.value), cur.gmv?.text)

  // 窄屏：说法不同的那一句更长，也不撑宽
  for (const theme of ['dark', 'light']) {
    const narrow = await open('time_split', { width: 390, height: 844, theme })
    await chooseAll(narrow.page, 'time_split')
    const w = await narrow.page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    check(`390px（${theme}）：说法不同的那一句不横向溢出`, w <= 0, `${w}px`)
    await shot(narrow.page, `approval-merge-narrow-${theme}`)
  }
})

await section('审批卡：几个指标落到同一项上时，照完整对照里服务端算好的合并预告说，和真写的一字不差', async () => {
  // 期 4：服务端把同一次裁决里落到同一项上的每种选择组合按真写的办法算好（dual_check_facts.preview_joint：归拢、拿掉
  // 驳回过的、对目录），放进完整对照的 catalog_joint。卡上按各指标当前的选择取对应的那一条：会写的说合起来的值、和哪几个
  // 指标一起记入，不写的照服务端的原话说原因。夹具 merges 的 catalog、joint 是服务端按那一组合的目录算的真实输出，
  // written 是服务端按那一组选择真写一遍（record_choices）的结果，test_dual_check_fixture.py 核对两者逐字一致
  const open = async (name, opts = {}) => {
    const m = fx.merges[name]
    const query = [`merge=${name}`, `peer=${m.refund_rate}`, ...(m.gmv ? [`variant=${m.gmv}`] : [])].join('&')
    const base = cardWith(m.gmv, { peer: m.refund_rate })
    const artifact = async () => {
      const out = await base()
      out.json.content.catalog_joint = m.joint
      return out
    }
    const out = await preview('dual-check-approval', { query, artifact, ...opts })
    await out.page.waitForSelector('[data-dual-check][data-state="ready"]')
    return out
  }
  const choose = (page, id, c) => metric(page, id).locator('label', { hasText: CHOICE_LABEL[c] }).click()
  const NAME = { gmv: '销售额', refund_rate: '退款率' }
  const current = async (page) => Object.fromEntries(await Promise.all(['gmv', 'refund_rate'].map(async (id) => (
    [id, Object.values(await previewClauses(metric(page, id))).find((x) => x.current) ?? null]))))
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b)

  for (const [name, m] of Object.entries(fx.merges)) {
    const { page, errors } = await open(name)
    for (const [id, c] of Object.entries(m.choices)) await choose(page, id, c)
    const cur = await current(page)
    const [entry] = m.joint.filter((e) => e.members.length === 2 && e.members.every((x) => m.choices[x.metric_id] === x.choice))
    const wrong = []
    for (const id of ['gmv', 'refund_rate']) {
      const said = entry?.outcomes?.[id]
      const c = cur[id]
      const other = NAME[id === 'gmv' ? 'refund_rate' : 'gmv']
      const ok = !!said && !!c?.joint && !c.conflict && !c.joined && c.weight >= 500 && (said.write
        // 会写的：值是合起来的那个，就是真写进去的；另一个指标也写时说一起记入
        ? c.write && c.text.includes(`会记为「${previewValue(said.value)}」`)
          && m.written.some((w) => w.item === entry.item && same(w.value, said.value))
          && (entry.outcomes[id === 'gmv' ? 'refund_rate' : 'gmv'].write
            ? c.together && c.text.endsWith(`，与「${other}」的选择一起记入`) : !c.together)
        // 不写的：照服务端的原话说原因，也确实没写进去
        : !c.write && c.reason && c.text === blockedText(CHOICE_LABEL[m.choices[id]], said.reason)
          && !m.written.some((w) => w.item === entry.item && same(w.value, said.value)))
      if (!ok) wrong.push(`${id}：${c?.text}（服务端 ${JSON.stringify(said)}）`)
    }
    check(`${name}：两个指标当前的那一条和服务端算好的合并结果、真写的结果一致`, !!entry && !wrong.length, wrong.join('；'))
    // 反过来：真写进去的每一项，卡上都有指标说「会记为」它
    const told = Object.values(cur).filter((c) => c?.write).map((c) => c.text)
    check(`${name}：真写进去的每一项卡上都说了会记为什么`, m.written.every((w) => told.some((t) => t.includes(`会记为「${previewValue(w.value)}」`))),
      `${JSON.stringify(m.written)} ‖ ${told.join(' ‖ ')}`)
    check(`${name}：页面没有报错`, errors.length === 0, errors.join('；'))
  }

  // 分开选（业务日期说法不同）：两边都照服务端的原话说不会记入；选择一变，说法跟着换成那一组合的结果
  const { page, errors } = await open('time_split')
  for (const [id, c] of Object.entries(fx.merges.time_split.choices)) await choose(page, id, c)
  let cur = await current(page)
  const exact = await previewTail(metric(page, 'gmv'))
  check('照服务端算好的合并结果说：结尾只说审批期间目录有改动时以时间线为准，不说是推断的', exact.kind === 'exact'
    && exact.text.includes('审批期间数据目录有改动时') && !exact.text.includes('推断')
    && !Object.values(cur).some((c) => c?.inferred), `${exact.kind} / ${exact.text}`)
  check('分开选：两边都说不会记入、写明给出了不同的说法', ['gmv', 'refund_rate'].every((id) => cur[id]?.reason
    && cur[id].text.includes('给出了不同的说法') && cur[id].text.includes('都未写入数据目录')), JSON.stringify(cur))
  await choose(page, 'refund_rate', 'a')
  cur = await current(page)
  check('退款率改选原流程（同一列）：两边都说会记为「paid_at」，并和对方一起记入',
    cur.gmv?.text === '采用原流程的结果时，「orders」（shop）的业务日期会记为「paid_at」，与「退款率」的选择一起记入'
    && cur.refund_rate?.text === '采用原流程的结果时，「orders」（shop）的业务日期会记为「paid_at」，与「销售额」的选择一起记入',
    `${cur.gmv?.text} ‖ ${cur.refund_rate?.text}`)
  const all = await previewClauses(metric(page, 'gmv'))
  check('……没选的那一条（销售额采用盲算）按那一组合说：和退款率说法不同，不会记入', all.b?.joint && all.b.reason && !all.b.write
    && all.b.text.startsWith('采用盲算的结果时不会记入：') && all.b.text.includes('给出了不同的说法'), all.b?.text)
  await choose(page, 'refund_rate', 'neither')
  cur = await current(page)
  check('退款率改选两边都不对：不再落到同一项上，销售额回到自己的预告', cur.gmv?.write && !cur.gmv.joint && !cur.gmv.together
    && cur.gmv.text === '采用原流程的结果时，「orders」（shop）的业务日期会记为「paid_at」', cur.gmv?.text)
  check('页面没有报错', errors.length === 0, errors.join('；'))

  // 驳回过的说法：拿出来不写，另一个指标照写，各自照服务端的结果说
  const rej = await open('filters_rejected')
  for (const [id, c] of Object.entries(fx.merges.filters_rejected.choices)) await choose(rej.page, id, c)
  cur = await current(rej.page)
  check('驳回过的条件：销售额说不会记入（曾被人工驳回），退款率说会记为自己的条件、不说一起记入',
    !!cur.gmv?.reason && cur.gmv.text.includes('曾被人工驳回') && cur.refund_rate?.write && !cur.refund_rate.together
    && cur.refund_rate.text === `采用原流程的结果时，「orders」（shop）的有效记录条件会记为「${fx.merges.filters_rejected.written[0]?.value}」`,
    `${cur.gmv?.text} ‖ ${cur.refund_rate?.text}`)

  // 工件里的合并预告认不出：这一项回到按其余指标的选择推，不替服务端编
  const broken = await preview('dual-check-approval', {
    query: `merge=time_split&peer=${fx.merges.time_split.refund_rate}`,
    artifact: async () => {
      const out = await cardWith(null, { peer: fx.merges.time_split.refund_rate })()
      out.json.content.catalog_joint = [{ table: 'orders', item: 'business_date', members: [{ metric_id: 'gmv' }], outcomes: 'x' }]
      return out
    },
  })
  await broken.page.waitForSelector('[data-dual-check][data-state="ready"]')
  for (const [id, c] of Object.entries(fx.merges.time_split.choices)) await choose(broken.page, id, c)
  cur = await current(broken.page)
  check('合并预告认不出：回到按其余指标的选择推（说法不同、都不记）', !!cur.gmv?.conflict && !cur.gmv.joint && !!cur.refund_rate?.conflict,
    `${cur.gmv?.text} ‖ ${cur.refund_rate?.text}`)
  check('……页面没有报错', broken.errors.length === 0, broken.errors.join('；'))

  for (const theme of ['dark', 'light']) {
    const narrow = await open('time_split', { width: 390, height: 844, theme })
    for (const [id, c] of Object.entries(fx.merges.time_split.choices)) await choose(narrow.page, id, c)
    const w = await narrow.page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    check(`390px（${theme}）：合并预告的原因很长也不横向溢出`, w <= 0, `${w}px`)
    await shot(narrow.page, `approval-joint-narrow-${theme}`)
  }
})

await section('审批卡：只有解析失败、兜底原话另有原因时', async () => {
  // 只有解析失败：和摘要阶段（服务端把它算作兜底）说法一致，默认展开对照，解析的原话附在下面
  const { page } = await preview('dual-check-approval', { artifact: cardWith('parse') })
  await page.waitForSelector('[data-dual-check][data-state="ready"]')
  const gmv = metric(page, 'gmv')
  check('只有解析失败：写那句固定的兜底说明，不当成一条具体差异', (await text(gmv.locator('[data-dual-fallback]'))) === '两条 SQL 的写法不同，未能归纳成具体差异'
    && await gmv.locator('[data-dual-diff-item]').count() === 0)
  check('……解析的原话附在下面', (await text(gmv.locator('[data-dual-fallback-note]'))).includes('盲算：SQL 无法解析'))
  check('……默认展开对照', (await gmv.locator('[data-dual-compare-toggle]').getAttribute('aria-expanded')) === 'true')

  // 有一边没有 SQL（节点的原话「有一边没有可比较的 SQL」）：不能再说「两条 SQL 的写法不同」
  const noSql = { equivalent: false, fallback: true, text_diff: [], items: [{ aspect: 'other', text: '有一边没有可比较的 SQL，请对照原文' }] }
  const card = cardWith(null, { diff: noSql })
  const lone = await preview('dual-check-approval', {
    artifact: async () => {
      const out = await card()
      out.json.content.metrics[0].blind.inputs[0].sql = null
      return out
    },
  })
  await lone.page.waitForSelector('[data-dual-check][data-state="ready"]')
  const g2 = metric(lone.page, 'gmv')
  const said = await text(g2.locator('[data-dual-fallback]'))
  check('一边没有 SQL：照节点的原话说，不写「两条 SQL 的写法不同」', said === '有一边没有可比较的 SQL' && !(await text(g2)).includes('写法不同'), said)
  check('……盲算那一栏写没有记录 SQL', (await text(g2.locator('[data-dual-sql="blind"]'))) === '没有记录 SQL')

  // 比较超时：固定那句之外附上原因
  const slow = { equivalent: false, fallback: true, text_diff: fx.card.metrics[0].diff.text_diff,
    items: [{ aspect: 'other', text: '两条 SQL 比较超时，未能归纳成具体差异，请对照原文' }] }
  const late = await preview('dual-check-approval', { artifact: cardWith(null, { diff: slow }) })
  await late.page.waitForSelector('[data-dual-check][data-state="ready"]')
  const g3 = metric(late.page, 'gmv')
  check('比较超时：固定那句之外附上原因（去掉「请对照原文」）', (await text(g3.locator('[data-dual-fallback]'))) === '两条 SQL 的写法不同，未能归纳成具体差异'
    && (await text(g3.locator('[data-dual-fallback-note]'))) === '两条 SQL 比较超时，未能归纳成具体差异', await text(g3.locator('[data-dual-fallback-note]')))
})

/** 运行的图快照里双盲复算节点的「未采用原流程时」写成 value（undefined 是没写） */
const graphWith = (value) => async () => {
  const graph = JSON.parse(JSON.stringify(fx.graph))
  const dc = graph.nodes.find((n) => n.id === 'dc')
  dc.data.config = { ...dc.data.config, ...(value === undefined ? {} : { on_reject: value }) }
  return { json: { graph, workflow_id: null, version: null } }
}

await section('审批卡：后果和提交后的提示按节点的「未采用原流程时」说', async () => {
  // 判为失败：采用盲算或都不对会让运行终止，卡上只说这一种，提交后的提示也照实说
  const fail = await preview('dual-check-approval', { graph: graphWith('fail') })
  await fail.page.waitForSelector('[data-dual-consequence][data-on-reject="fail"]')
  const said = await text(fail.page.locator('[data-dual-consequence]'))
  check('判为失败：后果写明采用盲算或都不对时运行将终止，不再说出具降档', said.includes('运行将终止') && !said.includes('降档') && !said.includes('如节点'), said)
  for (const id of ['gmv', 'refund_rate']) await metric(fail.page, id).locator('label', { hasText: '采用原流程的结果' }).click()
  await metric(fail.page, 'gmv').locator('label', { hasText: '采用盲算的结果' }).click()
  await fail.page.locator('[data-dual-submit]').click()
  await fail.page.waitForFunction(() => window.__dualResolved != null)
  await fail.page.waitForFunction(() => document.querySelector('[role="status"][aria-live="polite"]')?.textContent?.includes('已提交裁决'))
  const failToast = await toastOf(fail.page)
  check('……提交后的提示：运行将终止，不说「运行继续」', failToast.includes('已提交裁决，运行将按节点设置终止') && !failToast.includes('运行继续'), failToast)

  // 判为失败但全选原流程：照常继续
  const ok = await preview('dual-check-approval', { graph: graphWith('fail') })
  await ok.page.waitForSelector('[data-dual-consequence][data-on-reject="fail"]')
  for (const id of ['gmv', 'refund_rate']) await metric(ok.page, id).locator('label', { hasText: '采用原流程的结果' }).click()
  await ok.page.locator('[data-dual-submit]').click()
  await ok.page.waitForFunction(() => window.__dualResolved != null)
  await ok.page.waitForFunction(() => document.querySelector('[role="status"][aria-live="polite"]')?.textContent?.includes('已提交裁决'))
  check('……全部采用原流程：提示运行继续', (await toastOf(ok.page)) === '已提交裁决，运行继续', await toastOf(ok.page))

  // 图快照取不到（老运行、接口出错）：两种处置都可能，有条件地说；提交后的提示不说继续还是终止
  const lost = await preview('dual-check-approval', { graph: async () => ({ status: 404, json: { detail: '运行不存在' } }) })
  await lost.page.waitForSelector('[data-dual-consequence][data-on-reject="unknown"]')
  const maybe = await text(lost.page.locator('[data-dual-consequence]'))
  check('取不到节点设置：降档那句之外，有条件地说判为失败会终止', maybe.includes('出具降档') && maybe.includes('如节点的「未采用原流程时」设为「判为失败」'), maybe)
  for (const id of ['gmv', 'refund_rate']) await metric(lost.page, id).locator('label', { hasText: '两边都不对' }).click()
  await lost.page.locator('[data-dual-submit]').click()
  await lost.page.waitForFunction(() => window.__dualResolved != null)
  await lost.page.waitForFunction(() => document.querySelector('[role="status"][aria-live="polite"]')?.textContent?.includes('已提交裁决'))
  const toast = await toastOf(lost.page)
  check('……提交后的提示不替节点下结论', toast === '已提交裁决', toast)

  // 服务端在 payload 里带了实际生效的处置（约定之外另带的）：照它说，不再取图快照——子工作流里的节点图快照里找不到，
  // 有了它也说得准
  let asked = 0
  const given = await preview('dual-check-approval', {
    query: 'on_reject=fail', graph: async () => { asked++; return { json: { graph: fx.graph, workflow_id: null, version: null } } },
  })
  await given.page.waitForSelector('[data-dual-check][data-state="ready"]')
  await given.page.waitForSelector('[data-dual-consequence][data-on-reject="fail"]')
  check('payload 带了「判为失败」：照它说（图快照里是缺省的计入缺口）', (await text(given.page.locator('[data-dual-consequence]'))).includes('运行将终止'))
  check('……不再取运行的图快照', asked === 0, `${asked} 次`)
  const sub = await preview('dual-check-approval', { query: 'on_reject=degrade', graph: async () => ({ json: { graph: { nodes: [], edges: [] } } }) })
  await sub.page.waitForSelector('[data-dual-consequence][data-on-reject="degrade"]')
  check('……图快照里找不到节点（子工作流）也照 payload 说，不挂「如节点设为判为失败」那句',
    !(await text(sub.page.locator('[data-dual-consequence]'))).includes('如节点'))
  // 认不出的值：不照它说，回到图快照
  const odd = await preview('dual-check-approval', { query: 'on_reject=bogus', graph: graphWith('fail') })
  const back = await odd.page.waitForSelector('[data-dual-consequence][data-on-reject="fail"]').then(() => true, () => false)
  check('payload 里的值认不出：回到图快照', back, await odd.page.locator('[data-dual-consequence]').getAttribute('data-on-reject'))
})

await section('审批卡：认不出任何指标时留一个出口', async () => {
  const { page, sent, errors } = await preview('dual-check-approval-empty', {
    artifact: async () => ({ json: { id: CARD_ID, content: { metrics: [], model: null, same_model: false } } }),
  })
  await page.waitForSelector('[data-dual-check][data-state="ready"]')
  check('写明没有可以裁决的指标', (await text(page.locator('[data-dual-empty]'))).includes('没有可以裁决的指标'))
  check('……没有提交裁决的按钮', await page.locator('[data-dual-submit]').count() === 0)
  const reject = page.locator('[data-dual-reject]')
  check('……给一个「驳回并终止」，说清后果', (await text(reject)) === '驳回并终止' && (await text(page.locator('[data-dual-check]'))).includes('运行终止'))
  await page.locator('[data-dual-note]').fill('审批内容缺失，重新运行')
  await reject.click()
  await page.waitForFunction(() => window.__dualResolved != null)
  const body = sent.find((s) => s.path === `/api/approvals/${AP.id}/decide`)?.body
  check('……驳回：approved 为假、带上备注，不带 value', body?.approved === false && body.note === '审批内容缺失，重新运行' && !('value' in body), JSON.stringify(body))
  check('……提示已驳回、运行终止', (await text(page.locator('body'))).includes('已驳回，本次运行终止'))
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

await section('出具缺口：证据页、成果节点的印章把核查报上来的缺口另起两组，不放在「校验未全部完成」下', async () => {
  // 双盲复算、反方审稿报上来的缺口（出具契约加了「双盲复算「节点名」：」「反方审稿：」的前缀）以前和「叙述模板渲染为空」
  // 一起列在「校验未全部完成」下。证据页（真的 EvidencePane）在出具横幅下面另起「核查未通过」（两路不一致、结论被推翻）
  // 「核查未完成」（无法复算）两组，横幅里只留校验没做完的；成果节点印章的悬停说明同样分组、同样的先后。用例
  // verify-gaps（dev/fixtures/dual_check.tsx）：缺口的先后照服务端，核查的和别的混在一起；证据图、审计接口在这里伪造
  const RUN = 'run-verify-gaps'
  const FAILED = ['双盲复算「复核周报口径」：指标「销售额」两路结果不一致，审核人选择了盲算结果', '反方审稿：推翻了 1 句结论：「东区增长主要来自新客」']
  const UNFINISHED = ['双盲复算「复核周报口径」：指标「订单数」无法复算：输入来自合并查询，无法单独复算']
  const OTHER = ['叙述模板渲染为空（路径可能有误）']
  const extra = async (req, url) => {
    if (url.pathname === `/api/runs/${RUN}/evidence`) {
      return { json: { run_id: RUN, mode: 'none', note: '这次运行没有报告文档', seal: { sealed: true, ok: true, manifest_seq: 7 } } }
    }
    if (url.pathname === `/api/runs/${RUN}/evidence/audit`) return { status: 404, json: { detail: 'Not Found' } }
    return null
  }
  const items = (els) => els.map((el) => el.textContent.replace(/^·\s*/, '').trim())
  for (const [width, theme] of [[1280, 'dark'], [390, 'dark'], [390, 'light']]) {
    const what = `${width}px（${theme}）`
    const { page, errors } = await preview('verify-gaps', { extra, width, height: 900, theme })
    await page.waitForSelector('[data-evidence-verify-gaps]')
    const groups = await page.evaluate(() => [...document.querySelectorAll('[data-evidence-verify-group]')].map((g) => ({
      kind: g.dataset.evidenceVerifyGroup, head: g.firstElementChild?.textContent.trim(),
      items: [...g.querySelectorAll('li')].map((li) => li.textContent.replace(/^·\s*/, '').trim()),
    })))
    check(`${what} 证据页：横幅下面另起两组，先「核查未通过」（两路不一致、被推翻），再「核查未完成」（无法复算）`,
      JSON.stringify(groups) === JSON.stringify([{ kind: 'failed', head: '核查未通过', items: FAILED },
        { kind: 'unfinished', head: '核查未完成', items: UNFINISHED }]), JSON.stringify(groups))
    const banner = await page.locator('[data-evidence-issuance] [data-issuance-gaps] li').evaluateAll(items)
    check(`${what} ……横幅的「校验未全部完成」里只剩校验没做完的那条`, JSON.stringify(banner) === JSON.stringify(OTHER)
      && (await text(page.locator('[data-evidence-issuance] [data-issuance-gaps]'))).startsWith('校验未全部完成'), JSON.stringify(banner))
    // 成果节点的印章：悬停说明里核查的两组排在「校验未全部完成」前面，各组只列自己的
    const stamp = page.locator('.react-flow__node[data-id="done"] .nc-stamp')
    await stamp.waitFor()
    const lines = ((await stamp.getAttribute('title')) ?? '').split('\n')
    const at = (head) => lines.findIndex((l) => l.startsWith(head))
    check(`${what} 成果节点印章：悬停说明先「核查未通过」、再「核查未完成」、最后「校验未全部完成」，各列各的`,
      lines[at('核查未通过：')] === `核查未通过：${FAILED.join('；')}` && lines[at('核查未完成：')] === `核查未完成：${UNFINISHED.join('；')}`
      && lines[at('校验未全部完成：')] === `校验未全部完成：${OTHER.join('；')}`
      && at('核查未通过：') < at('核查未完成：') && at('核查未完成：') < at('校验未全部完成：'), lines.join(' / '))
    const w = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    check(`${what}：证据页不横向溢出`, w <= 0, `${w}px`)
    const c = await page.evaluate(() => {
      const box = document.querySelector('[data-evidence-verify-gaps]')
      const bgOf = (el) => {
        for (let n = el; n && n !== document.documentElement; n = n.parentElement) {
          const v = getComputedStyle(n).backgroundColor
          if (v !== 'rgba(0, 0, 0, 0)' && v !== 'transparent') return v
        }
        return 'rgba(0, 0, 0, 0)'
      }
      return { fg: getComputedStyle(box).color, bg: bgOf(box), base: getComputedStyle(document.body).backgroundColor }
    })
    const ratio = contrast(c.fg, c.bg, c.base)
    check(`${what}：核查那两组的字对比度 ≥ 4.5`, ratio >= 4.5, `${ratio.toFixed(2)}（${c.fg} on ${c.bg}）`)
    await shot(page, `verify-gaps-${width}-${theme}`)
    check(`${what}：页面没有报错`, errors.length === 0, errors.join('；'))
  }
})

await section('记录页的工件列表：双盲复算对照、反方审稿记录有名字', async () => {
  // 工件列表按 pages/runs/model.ts 的 ARTIFACT_KIND_LABEL 写种类：期 3 新加的两种工件没登记时，列表里露出的是键名
  // dual_check、challenge，说明一栏只有一句「工件」。按页面自己加载的那一份取（同 check-evidence 的工件一节）
  const { page, errors } = await preview('dual-check-approval')
  const got = await page.evaluate(async () => {
    const m = await import('/src/pages/runs/model.ts')
    return {
      dual: m.artifactKindLabel('dual_check'), challenge: m.artifactKindLabel('challenge'),
      dualDesc: m.artifactDescription('dual_check', { metrics: 2 }), dualBare: m.artifactDescription('dual_check', null),
      challengeDesc: m.artifactDescription('challenge', { report: 'report' }),
      evidence: m.isEvidence('dual_check') && m.isEvidence('challenge'),
    }
  })
  check('种类：双盲复算对照、反方审稿记录，不露键名', got.dual === '双盲复算对照' && got.challenge === '反方审稿记录', JSON.stringify(got))
  check('……说明写清装的是什么：双盲复算带指标个数，没有 meta 的老数据也有一句', got.dualDesc === '双盲复算：2 个指标两边的 SQL、取值与差异说明'
    && got.dualBare === '双盲复算：两边的 SQL、取值与差异说明' && got.challengeDesc === '反方审稿：每句结论的判断、理由和反方执行的查询',
  JSON.stringify(got))
  check('……两种都算证据（列表里醒目）', got.evidence)
  check('……页面没有报错', errors.length === 0, errors.join('；'))
})

// ---------------------------------------------------------------- 检查器与节点卡片

/**
 * 检查器用例：设置（相对误差、启用的「跟随设置」要写出眼下的值）按夹具答，改配置触发的校验、变量分析回空，
 * 节点配置直接从 studio store 读（window.__studio，开发服务器才挂）
 */
async function inspector({ verify = () => fx.verify, ...opts } = {}) {
  const out = await preview('dual-check-inspector', {
    ...opts,
    extra: (req, url) => {
      if (req.method() === 'GET' && url.pathname === '/api/settings') return { json: { run: {}, verify: verify() } }
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
  // 字段名就是服务端校验报错里写的叫法（上面「叫法」一节和 schema.py 比过）
  const { DUAL_CHECK_TEXT: D } = await import(await load('/src/lib/terms.ts'))
  const shown = {}
  for (const k of ['card', 'metrics', 'definitions', 'provider', 'model', 'on_reject', 'enabled']) {
    shown[k] = (await page.locator(`[data-field="${k}"] label`).first().innerText()).trim()
  }
  const misnamed = Object.entries(shown).filter(([k, v]) => v !== D.field[k])
  check('检查器上的字段名用 DUAL_CHECK_TEXT.field 的叫法', !misnamed.length, misnamed.map(([k, v]) => `${k}：${v} ≠ ${D.field[k]}`).join('；'))

  // 口径卡：下拉选画布上的口径卡，上游的一组、其余的一组；留空取上游的那张
  const card = page.locator('[data-field="card"] select')
  const groups = await card.evaluate((el) => [...el.querySelectorAll('optgroup')].map((g) => `${g.label}:${[...g.querySelectorAll('option')].map((o) => o.value).join('+')}`))
  check('口径卡：上游的一组、其余的一组', groups.join('|') === '上游的口径卡:card|其他口径卡（需连接到本节点上游）:card2', groups.join('|'))
  check('……留空那一项写明默认取上游的哪一张', (await card.locator('option').first().innerText()) === '默认：上游的口径卡「周报口径卡」')
  check('……标签指向这个下拉', await page.locator('[data-field="card"] label').getAttribute('for') === await card.getAttribute('id'))
  await card.selectOption('card2')
  check('选另一张：写进 card', (await config(page)).card === 'card2', JSON.stringify(await config(page)))
  check('……不在上游的照实提醒', (await text(page.locator('[data-node-pick-hint="notUpstream"]'))).includes('不在本节点上游'))
  const swapped = await summaryBecomes(page, '复算「月报口径卡」 · 全部 1 个指标')
  check('……摘要换成这张卡', swapped.ok, swapped.seen)
  await card.selectOption('')
  check('选回默认：card 去掉，不留空串', !('card' in (await config(page))) || (await config(page)).card === undefined, JSON.stringify(await config(page)))
  // 选口径卡用的是和反方审稿共用的控件（verifyFields.tsx 的 NodePick）：写着的卡被删了、写的不是口径卡（助手改图、导入
  // JSON 会写出这种配置）都照实列在下拉里（选不回去）、下面说一句，不让它看着像「默认」
  const setCard = (value) => page.evaluate((v) => {
    const s = window.__studio.getState()
    const n = s.nodes.find((x) => x.id === 'dc')
    s.updateNode('dc', { config: { ...n.data.config, card: v } })
  }, value)
  const selected = () => card.evaluate((el) => el.options[el.selectedIndex]?.textContent)
  await setCard('ghost')
  await page.waitForTimeout(100)
  check('写着的卡已不在画布上：下拉照实标出，下面说一句', await selected() === 'ghost（不在画布上）'
    && (await text(page.locator('[data-node-pick-hint="missing"]'))) === '所选口径卡已不在画布上，请重新选择', await selected())
  await setCard('fetch')
  await page.waitForTimeout(100)
  const wrongType = page.locator('[data-node-pick-hint="wrongType"]')
  check('写的不是口径卡：下拉标「不是口径卡」，提示说清它是哪类节点，不说成不在画布上', await selected() === '取数（不是口径卡）'
    && await wrongType.count() === 1 && (await text(wrongType)).startsWith('所选节点是「') && (await text(wrongType)).endsWith('」节点，不是口径卡，请重新选择')
    && await page.locator('[data-node-pick-hint="missing"]').count() === 0, `${await selected()} · ${await wrongType.count() ? await text(wrongType) : '没有提示'}`)
  await card.selectOption('')
  check('……选回默认：提示收起、card 去掉', await page.locator('[data-node-pick-hint]').count() === 0 && (await config(page)).card === undefined,
    JSON.stringify(await config(page)))

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
  check('启用用的是和反方审稿共用的三态控件，组名是字段名', (await enabled.getAttribute('data-tri-state')) === 'follow'
    && (await enabled.getAttribute('aria-label')) === D.field.enabled)
  check('启用：三态，跟随设置写出设置里眼下是开着的', (await enabled.locator('[role="radio"]').allInnerTexts()).join('|') === '跟随设置（当前开启）|开启|关闭'
    && await enabled.locator('[role="radio"][aria-checked="true"]').getAttribute('data-tri') === 'follow')
  await enabled.locator('[data-tri="off"]').click()
  const off = await summaryBecomes(page, { suffix: ' · 已关闭' })
  check('关闭：enabled 为 false，摘要标「已关闭」', (await config(page)).enabled === false && off.ok, off.seen)
  await enabled.locator('[data-tri="off"]').focus()
  await page.keyboard.press('ArrowLeft')
  check('键盘方向键换选项：开启是 true', (await config(page)).enabled === true
    && await page.evaluate(() => document.activeElement?.getAttribute('data-tri')) === 'on')
  await enabled.locator('[data-tri="follow"]').click()
  check('跟随设置：enabled 去掉，不留 null', (await config(page)).enabled === undefined, JSON.stringify(await config(page)))

  // 未采用原流程时
  const reject = page.locator('[data-field="on_reject"] select')
  check('未采用原流程时：默认落在「计入缺口、出具降档」上', (await reject.locator('option:checked').innerText()) === '计入缺口、出具降档（默认）')
  await reject.selectOption('fail')
  check('……选判为失败：on_reject 为 fail', (await config(page)).on_reject === 'fail')

  // 盲算模型不在高级选项里：时间线「模型相同」那一行的「打开设置」要能直接定位到它（高级选项收着时定位不到）
  check('盲算模型的接入和模型不用展开高级选项就能看到', await page.locator('[data-field="provider"] select').isVisible()
    && await page.locator('[data-field="model"] input').isVisible() && await page.locator('[data-field="tolerance"]').count() === 0)
  // 高级：相对误差
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
    && (await text(page.locator('[data-verify-by-model]'))).includes('将按模型名使用接入「云端接入」') && (await config(page)).provider === undefined)
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

await section('检查器：上游有几张口径卡时必须选；「跟随设置」的当前值每次打开都重取', async () => {
  let verifyNow = fx.verify
  const { page, errors } = await inspector({ verify: () => verifyNow })
  // 月报口径卡也接到双盲复算上游：服务端校验要求选一张（不选会挡住发起运行），不是「建议」
  await page.evaluate(() => window.__studio.setState((s) => ({
    edges: [...s.edges, { ...s.edges.find((e) => e.id === 'e4'), id: 'e-card2', source: 'card2' }],
  })))
  const card = page.locator('[data-field="card"] select')
  await page.waitForFunction(() => document.querySelector('[data-field="card"] select option')?.textContent?.includes('2 张'))
  check('上游两张：留空那一项写明要选，不说「建议」', (await card.locator('option').first().innerText()) === '请选择要复算的口径卡（上游有 2 张）'
    && !(await text(page.locator('[data-field="card"]'))).includes('建议'), await card.locator('option').first().innerText())
  check('……下面用提醒色写明必须选一张', (await text(page.locator('[data-node-pick-hint="many"]'))) === '上游有 2 张口径卡，请选择要复算的那一张')
  const many = await summaryBecomes(page, '复算上游的口径卡 · 上游有 2 张，请指定')
  check('……节点卡片的摘要也这么说', many.ok, many.seen)
  await card.selectOption('card')
  check('……选了一张：提醒收起', await page.locator('[data-node-pick-hint]').count() === 0 && (await config(page)).card === 'card')

  // 去设置里关掉双盲复算、改了相对误差，再回到画布打开这个节点：「跟随设置」写的是新的值
  const enabled = page.locator('[data-field="enabled"] [role="radio"][data-tri="follow"]')
  check('打开时：跟随设置（当前开启）', (await text(enabled)) === '跟随设置（当前开启）', await text(enabled))
  verifyNow = { ...fx.verify, dual_check: { ...fx.verify.dual_check, enabled: false, tolerance: 0.01 } }
  await page.evaluate(() => window.__studio.setState({ selectedId: null }))
  await page.waitForSelector('[data-field="enabled"]', { state: 'detached' })
  await page.evaluate(() => window.__studio.setState({ selectedId: 'dc' }))
  await page.waitForFunction(() => document.querySelector('[data-field="enabled"] [data-tri="follow"]')?.textContent === '跟随设置（当前关闭）', null, { timeout: 3000 })
    .catch(() => {})
  check('再打开：重取设置，写当前关闭', (await text(enabled)) === '跟随设置（当前关闭）', await text(enabled))
  await page.locator('button', { hasText: '高级选项' }).click()
  check('……相对误差的占位也是新值', await page.locator('[data-field="tolerance"] input').getAttribute('placeholder') === '跟随设置（0.01）',
    await page.locator('[data-field="tolerance"] input').getAttribute('placeholder'))
  check('页面没有报错', errors.length === 0, errors.join('；'))
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
      const el = document.querySelector('[data-verify-by-model]')
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

// ---------------------------------------------------------------- 设置页「核查」一节

/**
 * 设置页（真页面）：设置的读写、今日核查花费、模型接入按夹具答，PUT 记下请求体、合进存着的那份再回给页面；
 * put 给了就由它答（模拟 422）。别的 GET 放行到服务端，别的写请求一律拦掉
 */
async function settingsPage({ width = 1280, height = 900, theme = 'dark', verify = fx.verify, put = null } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: theme, timezoneId: 'Asia/Shanghai' })
  opened.add(ctx)
  ctx.setDefaultTimeout(6000)
  await ctx.addInitScript((t) => {
    try { localStorage.setItem('agentlab.theme', t); localStorage.setItem('agentlab_actor', '张工') } catch { /* 隐私窗口 */ }
  }, theme)
  const page = await ctx.newPage()
  const errors = []
  const sent = []
  let spendGets = 0
  let stored = {
    ui: { theme }, run: { default_memory_scope: 'default', default_collection: 'default', confirm_dangerous_tools: true,
      tool_gate_provider: null, tool_gate_model: null, agent_max_steps: 100, agent_budget_tokens: 2000000, agent_budget_usd: null },
    limits: { max_graph_steps: 200, max_agent_steps: 100, max_run_seconds: 1800 },
    judge: { provider: 'judge-provider-x', model: null, report_max_claims: 40, report_max_cost_usd: 0.05, report_timeout_s: 30,
      click_max_cost_usd: 0.01, daily_max_usd: 2 },
    ...(verify ? { verify } : {}),
  }
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('dialog', (d) => { errors.push(`原生对话框：${d.message()}`); void d.dismiss() })
  await page.route((u) => new URL(u).pathname.startsWith('/api/'), async (route) => {
    const req = route.request()
    const url = new URL(req.url())
    if (url.pathname === '/api/settings' && req.method() === 'GET') return route.fulfill({ json: stored })
    if (url.pathname === '/api/settings' && req.method() === 'PUT') {
      const body = req.postDataJSON()
      sent.push({ path: url.pathname, body })
      if (put) {
        const out = await put(body)
        if (out) return route.fulfill({ status: out.status ?? 200, json: out.json })
      }
      stored = { ...stored, ...body.values }
      return route.fulfill({ json: stored })
    }
    if (url.pathname === '/api/settings/verify/spend') {
      spendGets += 1
      return route.fulfill({ json: { date: '2026-10-05', usd: 0.0456, calls: 12, unpriced_calls: 1, daily_max_usd: stored.verify?.daily_max_usd ?? null } })
    }
    if (url.pathname === '/api/settings/judge/spend') return route.fulfill({ json: { date: '2026-10-05', usd: 0, calls: 0, unpriced_calls: 0 } })
    if (url.pathname === '/api/providers') return route.fulfill({ json: fx.providers })
    if (req.method() !== 'GET') {
      sent.push({ path: url.pathname, blocked: true, method: req.method() })
      return route.abort()
    }
    return route.continue()
  })
  await page.goto(`${WEB}/settings/prefs`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-prefs-bar], [data-judge-settings]', { timeout: 15000 })
  return { ctx, page, errors, sent, spends: () => spendGets }
}

await section('设置页「核查」一节：读回各项、叫法和服务端一致', async () => {
  const { page, errors } = await settingsPage()
  await page.waitForSelector('[data-verify-settings]')
  const box = page.locator('[data-verify-settings]')
  const { VERIFY_SETTING_TEXT: T } = await import(await load('/src/lib/terms.ts'))
  check('偏好设置里有「核查」一节，说明三项共用核查模型和每日金额上限', (await text(box.locator('h2'))) === '核查'
    && (await text(box)).includes('共用核查模型和每日金额上限'))
  const label = async (id) => text(page.locator(`label[for="${id}"]`))
  check('核查模型：接入、模型两项，叫法和服务端一致', await label('pref-verify-provider') === T.provider && await label('pref-verify-model') === T.model)
  check('……接入留空时跟随证据裁判模型，并醒目提示未单独配置', (await page.locator('#pref-verify-provider option').first().innerText()) === '跟随证据裁判模型'
    && (await text(page.locator('[data-verify-not-set]'))).includes('未单独配置，将使用证据裁判的模型'))
  const parts = await page.locator('[data-verify-part]').evaluateAll((els) => els.map((e) => e.dataset.verifyPart))
  check('三项各一组：双盲复算、反方审稿、审批质询', parts.join(',') === 'dual_check,challenge,inquiry', parts.join(','))
  const v = (id) => page.locator(`#${id}`).inputValue()
  check('双盲复算：启用、允许的相对误差、每项最多查询次数、时长上限', await page.locator('[data-verify-toggle="dual_check.enabled"]').isChecked()
    && await v('pref-verify-dual_check-tolerance') === '0.000001' && await v('pref-verify-dual_check-max_queries') === '6'
    && await v('pref-verify-dual_check-timeout_s') === '120')
  check('反方审稿：启用、每份报告最多审几句、每句最多查询次数、时长上限', await page.locator('[data-verify-toggle="challenge.enabled"]').isChecked()
    && await v('pref-verify-challenge-max_claims') === '5' && await v('pref-verify-challenge-max_queries') === '4'
    && await v('pref-verify-challenge-timeout_s') === '90')
  check('审批质询：启用、每次最多查询次数、时长上限', await page.locator('[data-verify-toggle="inquiry.enabled"]').isChecked()
    && await v('pref-verify-inquiry-max_queries') === '6' && await v('pref-verify-inquiry-timeout_s') === '120')
  check('每日金额上限', await v('pref-verify-daily_max_usd') === '2')
  const ids = ['dual_check-tolerance', 'dual_check-max_queries', 'dual_check-timeout_s', 'challenge-max_claims', 'challenge-max_queries',
    'challenge-timeout_s', 'inquiry-max_queries', 'inquiry-timeout_s', 'daily_max_usd']
  const wrong = []
  for (const id of ids) if (await label(`pref-verify-${id}`) !== T[id.replace('-', '.')]) wrong.push(id)
  for (const k of ['dual_check.enabled', 'challenge.enabled', 'inquiry.enabled']) {
    if ((await text(page.locator(`[data-verify-toggle="${k}"]`).locator('xpath=..'))) !== T[k]) wrong.push(k)
  }
  check('……每一项的字段名就是服务端报错时写的叫法', !wrong.length, wrong.join('、'))
  const spend = await text(page.locator('[data-verify-spend]'))
  check('每日上限旁边写今日核查花费', spend.includes('今日已花费 $0.0456（12 次调用）'), spend)
  check('……估不出金额的调用另说', (await text(page.locator('[data-verify-unpriced]'))).includes('另有 1 次调用无法估算金额'))
  check('没有运行时报错', errors.length === 0, errors.join('；'))
  await shot(page, 'settings-dark')
})

await section('设置页「核查」一节：改了之后整组写 verify，不限存 null，错了不让存', async () => {
  const { page, sent, errors, spends } = await settingsPage()
  await page.waitForSelector('[data-verify-settings]')
  const save = page.getByRole('button', { name: /保存设置/ })
  await page.locator('[data-verify-toggle="challenge.enabled"]').uncheck()
  await page.locator('#pref-verify-dual_check-tolerance').fill('0.001')
  // 键盘勾「不限」：Tab 到复选框上按空格
  await page.locator('[data-verify-limit="inquiry.timeout_s"] [data-verify-unlimited-toggle]').focus()
  await page.keyboard.press('Space')
  check('勾了「不限」：框变灰，写明还受什么约束', await page.locator('#pref-verify-inquiry-timeout_s').isDisabled()
    && (await text(page.locator('[data-verify-limit="inquiry.timeout_s"] [data-verify-unlimited]'))) === '不设上限，时长仅受模型接口自身的超时约束')
  await page.locator('#pref-verify-challenge-max_claims').fill('0')
  check('句数填 0：标红、保存不可用', await save.isDisabled()
    && (await text(page.locator('[data-verify-limit="challenge.max_claims"]'))).includes('请填写正整数，或勾选「不限」'))
  await page.locator('#pref-verify-challenge-max_claims').fill('8')
  await page.locator('#pref-verify-dual_check-tolerance').fill('-1')
  check('相对误差填负数：标红、保存不可用', await save.isDisabled()
    && (await text(page.locator('[data-verify-part="dual_check"]'))).includes('请填写不小于 0 的数'))
  await page.locator('#pref-verify-dual_check-tolerance').fill('0.001')
  await page.locator('#pref-verify-provider').selectOption('云端接入')
  await page.locator('#pref-verify-model').fill('model-b')
  check('填了核查模型：「未单独配置」的提示收起', await page.locator('[data-verify-not-set]').count() === 0)
  const bar = (await text(page.locator('[data-prefs-bar]')))
  check('保存条写明改了哪几项（用的是同一套叫法）', bar.includes('有 6 项未保存') && bar.includes('反方审稿 · 启用') && bar.includes('双盲复算 · 允许的相对误差')
    && bar.includes('审批质询 · 每次时长上限（秒）') && bar.includes('核查模型 · 接入'), bar)
  const before = spends()
  await save.click()
  await page.waitForFunction(() => document.querySelector('[data-prefs-bar]')?.textContent?.includes('已保存'))
  const put = sent.filter((s) => s.path === '/api/settings' && !s.blocked).at(-1)?.body?.values ?? {}
  check('保存：只写 verify 一组（运行默认值、裁判没改就不碰）', Object.keys(put).join(',') === 'verify', Object.keys(put).join(','))
  const want = {
    provider: '云端接入', model: 'model-b',
    dual_check: { enabled: true, tolerance: 0.001, max_queries: 6, timeout_s: 120 },
    challenge: { enabled: false, max_claims: 8, max_queries: 4, timeout_s: 90 },
    inquiry: { enabled: true, max_queries: 6, timeout_s: null },
    daily_max_usd: 2,
  }
  check('……整组写（没改的项照原值），不限存 null，数存成数', JSON.stringify(put.verify) === JSON.stringify(want), JSON.stringify(put.verify))
  check('……存完再取一次今日花费', spends() > before, `${before} → ${spends()}`)
  check('……存完读回来的就是刚存的', await page.locator('#pref-verify-model').inputValue() === 'model-b'
    && !(await page.locator('[data-verify-toggle="challenge.enabled"]').isChecked()))
  await page.locator('[data-verify-limit="daily_max_usd"] [data-verify-unlimited-toggle]').check()
  const daily = await text(page.locator('[data-verify-limit="daily_max_usd"] [data-verify-unlimited]'))
  check('每日上限设成不限：写明每日费用还受什么约束', daily.startsWith('不设上限，每日核查费用仅受'), daily)
  await page.locator('[data-verify-limit="daily_max_usd"] [data-verify-unlimited-toggle]').uncheck()
  check('勾掉「不限」：填回原来的数', await page.locator('#pref-verify-daily_max_usd').inputValue() === '2')
  check('没有别的写请求', !sent.some((s) => s.blocked), JSON.stringify(sent.filter((s) => s.blocked)))
  check('没有运行时报错', errors.length === 0, errors.join('；'))
})

await section('设置页「核查」一节：服务端拒收时照实说（报错里的叫法对得上字段名）', async () => {
  const detail = '「反方审稿 · 每份报告最多审几句」需要填写正整数（句数），留空表示不限，当前为「2.5」'
  const { page } = await settingsPage({ put: async () => ({ status: 422, json: { detail } }) })
  await page.waitForSelector('[data-verify-settings]')
  await page.locator('[data-verify-toggle="inquiry.enabled"]').uncheck()
  await page.getByRole('button', { name: /保存设置/ }).click()
  await page.waitForSelector('[data-prefs-bar] [role="alert"]')
  const bar = await text(page.locator('[data-prefs-bar]'))
  check('保存失败：保存条照实说，改动还在', bar.includes('保存失败') && bar.includes('有 1 项未保存')
    && !(await page.locator('[data-verify-toggle="inquiry.enabled"]').isChecked()), bar)
  check('……报错里写的叫法就是页面上的字段名', (await text(page.locator('label[for="pref-verify-challenge-max_claims"]'))) === '反方审稿 · 每份报告最多审几句')
})

await section('设置页「核查」一节：老服务端没有这一组时不显示；390px 不横向溢出、深浅色可读', async () => {
  const old = await settingsPage({ verify: null })
  await old.page.waitForSelector('[data-judge-settings]')
  check('老服务端没有 verify 一组：这一节不出现', await old.page.locator('[data-verify-settings]').count() === 0)
  for (const theme of ['dark', 'light']) {
    const { page, errors } = await settingsPage({ width: 390, height: 844, theme })
    await page.waitForSelector('[data-verify-settings]')
    await page.locator('[data-verify-limit="challenge.max_queries"] [data-verify-unlimited-toggle]').check()
    await page.locator('#pref-verify-model').fill('model-zzz')
    await page.locator('[data-verify-settings]').scrollIntoViewIfNeeded()
    // 设置页的外壳在 390px 下本来就有一处隐形提示条伸出视口（没有这一节时也一样），这里只看这一节自己：
    // 自己不出横向滚动条，里面的每个元素都不越过视口右边
    const w = await page.evaluate(() => {
      const box = document.querySelector('[data-verify-settings]')
      const vw = document.documentElement.clientWidth
      const right = Math.max(...[box, ...box.querySelectorAll('*')].map((e) => e.getBoundingClientRect().right))
      return { box: box.scrollWidth - box.clientWidth, beyond: Math.round(right - vw) }
    })
    check(`390px（${theme}）：「核查」一节不横向溢出`, w.box <= 0 && w.beyond <= 0, JSON.stringify(w))
    const c = await page.evaluate(() => {
      const base = getComputedStyle(document.body).backgroundColor
      const pick = (sel) => { const el = document.querySelector(sel); return el && getComputedStyle(el).color }
      return { base, unlimited: pick('[data-verify-unlimited]'), hint: pick('[data-verify-by-model]'), spend: pick('[data-verify-spend]') }
    })
    for (const key of ['unlimited', 'hint', 'spend']) {
      const ratio = contrast(c[key], c.base, c.base)
      check(`${theme}：${({ unlimited: '不限的说明', hint: '只填了模型的提醒', spend: '今日花费' })[key]}对比度 ≥ 4.5`, ratio >= 4.5,
        `${ratio.toFixed(2)}（${c[key]} on ${c.base}）`)
    }
    await shot(page, `settings-narrow-${theme}`)
    check(`390px（${theme}）：没有运行时报错`, errors.length === 0, errors.join('；'))
  }
})

await browser.close()
console.log(failed ? `\n✗ 双盲复算与反方审稿：${failed} 项未通过` : '\n✓ 双盲复算与反方审稿：全部通过')
process.exit(failed ? 1 : 0)
