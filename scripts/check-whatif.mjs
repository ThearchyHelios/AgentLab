// 数字推演（证据面板里口径卡上的数，run/WhatIf.tsx）的回归检查。
//
// 守的是这些「看着没坏、其实用不了」的地方：横幅没了或者不醒目（推演被当成出具）、边打字边发请求、请求体里的
// 输入路径和行号不对（行号对人从 1 数，对接口从 0 数）、勾选框在不推演时也冒出来、恢复原值之后还留着一行没勾回去、
// 不支持的输入一声不吭、窄屏（390px）被推演区撑出横向滚动、浅色主题下横幅看不清。
//
// 跑在预览页上：/preview.html?syn=whatif（宽屏右侧弹出、窄屏底部抽出）和 ?syn=whatif-rail（360px 画布右栏）。
// 文档是 frontend/src/run/__tests__/evidence-query.json 里后端 compose_doc 真跑出来的（只用通用名）；片段接口
// 按它的 segments 作答，推演接口按后端 engine/recompute.py 的规则在这里算（只覆盖用到的三个指标），非 GET 一律拦掉。
//
//   node scripts/check-whatif.mjs
// 对别的实例跑时带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-whatif.mjs
// WHATIF_SHOTS=<目录> 时亮暗两套各截几张，供人眼复核。
import { mkdirSync, readFileSync } from 'node:fs'
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const SHOTS = process.env.WHATIF_SHOTS ?? ''
const root = new URL('..', import.meta.url).pathname
const fx = JSON.parse(readFileSync(`${root}frontend/src/run/__tests__/evidence-query.json`, 'utf8'))
const RUN = fx.run_id
const BANNER = '推演，非出具：不会写进运行记录，也不会改动报告'

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}
async function section(name, fn) {
  console.log(`\n=== ${name} ===`)
  try {
    await fn()
  } catch (e) {
    check(`${name} 中途出错`, false, String(e?.message ?? e).split('\n')[0])
  }
}

// ---------------------------------------------------------------- 夹具

/** 片段接口的答复：夹具里的原样，s22 改成老运行的容器输入（推演不了的那种） */
function segmentReply(sid) {
  const body = structuredClone(fx.segments[sid])
  if (sid === 's22') {
    body.chain[0] = { ...body.chain[0], expression: 'sum(vars.adjust.items)', substituted: 'sum(vars.adjust.items)' }
    body.chain[1] = { step: 'input', path: 'vars.adjust.items', value: '[4 项]', node_id: 'adjust', via: 'code',
      field: 'items', role: 'compute', status: 'ok' }
  }
  return body
}

const chainOf = (sid) => fx.segments[sid].chain
const inputsOf = (sid) => chainOf(sid).filter((s) => s.step === 'input')
const Q1 = chainOf('s12')[1].artifact
const Q4 = chainOf('s12')[2].artifact
const Q3 = inputsOf('s70')[0].artifact
const thousands1 = (v) => v.toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 })

/** 推演接口在这里按后端的规则算：给了新值的用新值；cell / agent 字段那一格所在的行被排除了，指标排除后无法计算 */
const METRICS = {
  wow: {
    sid: 's12', name: '环比增幅', unit: '%', calc: ([g, p]) => Math.round((g - p) / p * 1000) / 10,
    render: (v) => `${v.toFixed(1)}%`, delta: (d) => `${d > 0 ? '+' : '-'}${Math.abs(d).toFixed(1)} 个百分点`,
  },
  spread: {
    sid: 's70', name: '首末单差额', unit: '元', calc: ([t, l]) => Math.round((t - l) * 10) / 10,
    render: (v) => `${thousands1(v)}元`, delta: (d) => `${d > 0 ? '+' : '-'}${thousands1(Math.abs(d))}元`,
  },
}

function whatifReply(body) {
  const overrides = body?.overrides ?? {}
  const inputs = overrides.inputs ?? {}
  const exclude = overrides.exclude_rows ?? {}
  const base = { run_id: RUN, card: { node_id: 'caliber', artifact: chainOf('s12')[0].artifact, caliber: '周报口径', version: 'v3' },
    report: body?.report ?? null, sealed: true, seal: { sealed: true, ok: true }, affected_cards: [] }
  if (body?.metric === 'adj') {
    return { ...base, metrics: [{ id: 'adj', name: '调整后销售额', unit: '元', old: 44000, new: 44000, delta: 0,
      rendered_old: '44,000.0元', rendered_new: '44,000.0元', rendered_delta: null, changed: false, status: 'unchanged',
      reason: null, substituted: 'sum(vars.adjust.items)',
      inputs: [{ path: 'vars.adjust.items', via: 'code', node_id: 'adjust', kind: 'container', editable: false,
        old: '[4 项]', new: '[4 项]', changed: false }] }],
    segments: [{ report: 'write', unit: 'u4', sid: 's22', metric: 'adj', rendered_old: '44,000.0元', rendered_new: '44,000.0元', changed: false }],
    unsupported: [{ metric: 'adj', input: 'vars.adjust.items', reason: '无法推演：当时没有保存这个输入的完整值' }] }
  }
  if (body?.metric === 'fx') {
    // 空态：原式里没有引用任何输入（写死的常数）
    return { ...base, metrics: [{ id: 'fx', name: '汇率', unit: '', old: 7.12, new: 7.12, delta: 0, rendered_old: '7.12',
      rendered_new: '7.12', rendered_delta: null, changed: false, status: 'unchanged', reason: null, substituted: '7.12', inputs: [] }],
    segments: [], unsupported: [] }
  }
  const def = METRICS[body?.metric]
  const step = chainOf(def.sid)[0]
  const views = inputsOf(def.sid).map((inp) => {
    const row = inp.locator?.row
    const view = { path: inp.path, via: inp.via, node_id: inp.node_id, artifact: inp.artifact, locator: inp.locator,
      kind: 'number', editable: true, old: inp.value }
    if (inp.path in inputs) return { ...view, new: inputs[inp.path], changed: inputs[inp.path] !== inp.value, touched: true }
    if ((exclude[inp.artifact] ?? []).includes(row)) {
      return { ...view, new: null, changed: true, excluded_rows: [row], touched: true, gone: true }
    }
    return { ...view, new: inp.value, changed: false }
  })
  const touched = views.some((v) => v.touched)
  const gone = views.find((v) => v.gone)
  let status = 'unchanged'; let value = step.value; let reason = null
  if (touched && gone) {
    status = 'excluded'; value = null; reason = `排除后无法计算：${gone.path} 所在的第 ${gone.locator.row + 1} 行已排除`
  } else if (touched) {
    status = 'ok'; value = def.calc(views.map((v) => v.new))
  }
  const delta = value == null ? null : Math.round((value - step.value) * 10) / 10
  const rendered = value == null ? '—' : def.render(value)
  const metric = { id: step.metric, name: def.name, unit: def.unit, old: step.value, new: value, delta,
    rendered_old: step.rendered, rendered_new: rendered, rendered_delta: delta ? def.delta(delta) : null,
    changed: value !== step.value, status, reason, substituted: status === 'ok' ? `（推演代入式 ${views.map((v) => v.new).join('、')}）` : step.substituted,
    inputs: views.map(({ touched: _t, gone: _g, ...v }) => v) }
  return { ...base, metrics: [metric], unsupported: [],
    segments: [{ report: 'write', unit: fx.segments[def.sid].unit.id, sid: def.sid, metric: step.metric,
      rendered_old: step.rendered, rendered_new: rendered, changed: rendered !== step.rendered }] }
}

// ---------------------------------------------------------------- 页面

const browser = await chromium.launch({ executablePath: CHROME })

/**
 * 开一页：片段接口按夹具答，推演接口按 whatifReply 答（reply 给了就由它改：延迟、出错、未封存），记下每次推演的
 * 请求体和时刻。别的非 GET 一律拦掉，别的 GET 放行
 */
async function open(syn, { width = 1280, height = 900, theme = 'dark', reply = null } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height }, colorScheme: theme, timezoneId: 'Asia/Shanghai' })
  ctx.setDefaultTimeout(6000)
  const page = await ctx.newPage()
  const sent = []
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('dialog', (d) => { errors.push(`原生对话框：${d.message()}`); void d.dismiss() })
  await page.route((u) => new URL(u).pathname.startsWith('/api/'), async (route) => {
    const req = route.request()
    const url = new URL(req.url())
    if (req.method() === 'POST' && url.pathname === `/api/runs/${RUN}/evidence/whatif`) {
      const body = req.postDataJSON()
      sent.push({ body, at: Date.now() })
      const out = reply ? await reply(body, sent.length) : { json: whatifReply(body) }
      return route.fulfill({ status: out.status ?? 200, json: out.json })
    }
    if (req.method() !== 'GET') return route.abort()
    const seg = url.pathname.match(new RegExp(`/api/runs/${RUN}/evidence/segments/([^/]+)$`))
    if (seg) return route.fulfill({ json: segmentReply(decodeURIComponent(seg[1])) })
    return route.continue()
  })
  await page.goto(`${WEB}/preview.html?syn=${syn}&theme=${theme}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-evidence-doc]')
  return { ctx, page, sent, errors }
}

/** 点开一个片段，等指标步骤和「推演」开关出来 */
async function openMetric(page, sid) {
  await page.click(`[data-seg="${sid}"]`)
  await page.waitForSelector('[data-ev-metric] [data-whatif-toggle]')
}

/** 展开推演，等第一份答复画出来 */
async function openWhatIf(page) {
  await page.click('[data-whatif-toggle]')
  await page.waitForSelector('[data-whatif-result="unchanged"], [data-whatif-result="ok"]')
}

const settle = (page, ms = 700) => page.waitForTimeout(ms)
const text = (page, sel) => page.locator(sel).first().innerText()
const shot = async (page, name) => {
  if (!SHOTS) return
  mkdirSync(SHOTS, { recursive: true })
  await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: false })
}

await section('打开推演', async () => {
  const { ctx, page, sent, errors } = await open('whatif')
  await openMetric(page, 's12')
  const toggle = page.locator('[data-whatif-toggle]')
  check('指标步骤上有「推演」，默认收起', (await toggle.getAttribute('aria-expanded')) === 'false')
  check('不推演时查询步骤里没有勾选框', await page.locator('[data-whatif-rows], [data-whatif-row] input').count() === 0)
  await openWhatIf(page)
  check('展开后开关标为展开、指向推演区', (await toggle.getAttribute('aria-expanded')) === 'true'
    && (await page.locator(`#${await toggle.getAttribute('aria-controls')}[data-whatif]`).count()) === 1)
  check('顶上醒目写着「推演，非出具」', (await text(page, '[data-whatif-banner]')).trim() === BANNER)
  check('第一次请求不带改动', JSON.stringify(sent[0]?.body) === JSON.stringify(
    { report: 'write', card: 'caliber', metric: 'wow', overrides: { inputs: {}, exclude_rows: {} } }), JSON.stringify(sent[0]?.body))
  check('原值 → 原值，标「无变化」', (await text(page, '[data-whatif-new]')) === '8.7%'
    && (await text(page, '[data-whatif-delta]')) === '无变化')
  const fields = await page.locator('[data-whatif-field]').count()
  check('两个数值输入都能改，框里是原值', fields === 2
    && await page.locator('[data-whatif-input="vars.kpi.gmv"] [data-whatif-field]').inputValue() === '45678.5')
  check('取自查询的输入给「去勾选要排除的行」', await page.locator('[data-whatif-pick]').count() === 2)
  check('输入落在的两个查询步骤里出现勾选框', await page.locator('[data-whatif-rows]').count() === 2
    && await page.locator('[data-whatif-row] input[type="checkbox"]').count() === 2)
  check('报告里引用它的一处不受影响', (await text(page, '[data-whatif-segments-none]')).includes('1 处不受影响'))
  check('「恢复原值」在没有改动时不可点', await page.locator('[data-whatif-reset]').isDisabled())
  await shot(page, 'open-dark')
  check('页面没有报错', errors.length === 0, errors.join('；'))
  await ctx.close()
})

await section('改一个输入', async () => {
  const { ctx, page, sent } = await open('whatif')
  await openMetric(page, 's12')
  await openWhatIf(page)
  const field = page.locator('[data-whatif-input="vars.kpi.gmv"] [data-whatif-field]')
  await field.fill('')
  const before = sent.length
  await field.pressSequentially('50000', { delay: 40 })
  await page.waitForTimeout(150)
  check('边打字不发请求', sent.length === before)
  await page.waitForSelector('[data-whatif-result="ok"]')
  check('停顿之后只发一次', sent.length === before + 1, `${sent.length - before} 次`)
  check('请求体带着改动的输入', JSON.stringify(sent.at(-1).body.overrides) === JSON.stringify({ inputs: { 'vars.kpi.gmv': 50000 }, exclude_rows: {} }),
    JSON.stringify(sent.at(-1).body.overrides))
  check('显示新值和变化量', (await text(page, '[data-whatif-new]')) === '19.0%'
    && (await text(page, '[data-whatif-delta]')) === '变化 +10.3 个百分点', await text(page, '[data-whatif-result]'))
  check('报告里会变的那一处：8.7% → 19.0%，可定位', (await text(page, '[data-whatif-segment="s12"]')).includes('19.0%')
    && await page.locator('[data-whatif-locate="s12"]').count() === 1)
  check('横幅一直在', (await text(page, '[data-whatif-banner]')).trim() === BANNER)

  // 回车立刻提交，不等停顿
  await field.fill('60000')
  const t0 = Date.now()
  await field.press('Enter')
  await page.waitForFunction(() => document.querySelector('[data-whatif-new]')?.textContent === '42.8%')
  check('回车立刻提交', sent.at(-1).body.overrides.inputs['vars.kpi.gmv'] === 60000 && sent.at(-1).at - t0 < 300,
    `${sent.at(-1).at - t0}ms`)
  // 写的不是数：提示，保留上一次的结果
  await field.fill('六万')
  await settle(page)
  check('不是数：提示「请输入数值」、不发请求', (await page.locator('[data-whatif-input="vars.kpi.gmv"]').innerText()).includes('请输入数值')
    && sent.at(-1).body.overrides.inputs['vars.kpi.gmv'] === 60000)
  // Esc 退回已提交的值，面板不关
  await field.press('Escape')
  check('Esc 退回已提交的值，面板还开着', await field.inputValue() === '60000' && await page.locator('[data-evidence-panel]').count() === 1)

  // 定位：焦点回到正文里那一处，推演不收起
  await page.click('[data-whatif-locate="s12"]')
  await page.waitForTimeout(200)
  check('定位到正文里那一处，推演照旧展开', await page.evaluate(() => document.activeElement?.getAttribute('data-seg')) === 's12'
    && await page.locator('[data-whatif]').count() === 1 && (await text(page, '[data-whatif-new]')) === '42.8%')
  await ctx.close()
})

await section('勾掉一行', async () => {
  const { ctx, page, sent } = await open('whatif')
  await openMetric(page, 's12')
  await openWhatIf(page)
  const box = page.locator(`[data-ev-query="${Q4}"] [data-whatif-row="0"] input[type="checkbox"]`)
  check('勾选框的读屏名从 1 数', (await box.getAttribute('aria-label')) === '排除第 1 行')
  await box.check()
  await page.waitForSelector('[data-whatif-result="excluded"]')
  check('请求体带 exclude_rows（快照工件 id → 从 0 数的行号）',
    JSON.stringify(sent.at(-1).body.overrides) === JSON.stringify({ inputs: {}, exclude_rows: { [Q4]: [0] } }), JSON.stringify(sent.at(-1).body.overrides))
  check('这一行划掉', await page.locator(`[data-ev-query="${Q4}"] [data-whatif-row="0"][data-excluded]`).count() === 1)
  check('排除后无法计算：写明原因、新值是「—」', (await text(page, '[data-whatif-reason]')).startsWith('排除后无法计算')
    && (await text(page, '[data-whatif-new]')) === '—')
  check('输入下写明排除了哪一行', (await text(page, `[data-whatif-input="cell(nodes.pull, 0, 'gmv')"] [data-whatif-excluded]`)) === '已排除第 1 行')
  check('结果窗口下写明已排除几行', (await text(page, `[data-ev-query="${Q4}"] [data-whatif-excluded-count]`)) === '已排除 1 行')
  await shot(page, 'excluded-dark')

  // 恢复原值：输入框、勾选框、结果都回去
  await page.locator('[data-whatif-input="vars.kpi.gmv"] [data-whatif-field]').fill('50000')
  await page.locator('[data-whatif-input="vars.kpi.gmv"] [data-whatif-field]').press('Enter')
  await settle(page, 300)
  await page.click('[data-whatif-reset]')
  await page.waitForSelector('[data-whatif-result="unchanged"]')
  check('恢复原值：结果回到原值', (await text(page, '[data-whatif-new]')) === '8.7%')
  check('恢复原值：输入框回到原值、勾选框都放开',
    await page.locator('[data-whatif-input="vars.kpi.gmv"] [data-whatif-field]').inputValue() === '45678.5'
    && !(await box.isChecked()) && await page.locator('[data-whatif-row][data-excluded]').count() === 0)
  check('恢复原值后按钮不可点', await page.locator('[data-whatif-reset]').isDisabled())

  // 收起：勾选框跟着收起，结果窗口换回原来的表格
  await page.click('[data-whatif-toggle]')
  check('收起后推演区和勾选框都没了，查询结果照常显示', await page.locator('[data-whatif], [data-whatif-rows]').count() === 0
    && await page.locator(`[data-ev-query="${Q4}"] table`).count() === 1)
  await ctx.close()
})

await section('多行快照与遮罩', async () => {
  const { ctx, page, sent } = await open('whatif')
  await openMetric(page, 's70')
  await openWhatIf(page)
  const rows = page.locator(`[data-ev-query="${Q3}"] [data-whatif-row]`)
  check('窗口里每行一个勾选框', await rows.count() === chainOf('s70').find((s) => s.step === 'query').rows.length)
  check('遮罩列照旧写「已遮罩」', await page.locator(`[data-ev-query="${Q3}"] [data-whatif-rows] td[data-masked]`).count() > 0
    && (await page.locator(`[data-ev-query="${Q3}"] [data-whatif-rows] td[data-masked]`).first().innerText()).trim() === '已遮罩')
  await page.locator(`[data-ev-query="${Q3}"] [data-whatif-row="5"] input`).check()
  await page.waitForSelector('[data-whatif-result="excluded"]')
  check('勾掉第 6 行：请求里是行号 5', JSON.stringify(sent.at(-1).body.overrides.exclude_rows) === JSON.stringify({ [Q3]: [5] }))
  await ctx.close()
})

await section('不支持的输入', async () => {
  const { ctx, page } = await open('whatif')
  await openMetric(page, 's22')
  await openWhatIf(page)
  check('不支持推演的输入列出原因', (await text(page, '[data-whatif-unsupported-input="vars.adjust.items"]'))
    .includes('无法推演：当时没有保存这个输入的完整值'))
  check('容器输入不给输入框', await page.locator('[data-whatif-field]').count() === 0
    && (await text(page, '[data-whatif-input="vars.adjust.items"] [data-whatif-value]')).includes('[4 项]'))
  await ctx.close()
})

await section('空态', async () => {
  const { ctx, page } = await open('whatif')
  await openMetric(page, 's24')
  await openWhatIf(page)
  check('原式没有引用输入：说清无可推演，不画空列表', (await text(page, '[data-whatif-empty]')).includes('无可推演')
    && await page.locator('[data-whatif-input]').count() === 0)
  check('没有报告引用时不画「报告中会变的地方」', await page.locator('[data-whatif-segments]').count() === 0)
  await ctx.close()
})

await section('加载、失败、未封存', async () => {
  let release
  const gate = new Promise((r) => { release = r })
  const slow = await open('whatif', { reply: async (body, n) => {
    if (n === 1) await gate
    return { json: whatifReply(body) }
  } })
  await openMetric(slow.page, 's12')
  await slow.page.click('[data-whatif-toggle]')
  await slow.page.waitForSelector('[data-whatif-loading]')
  check('答复回来之前显示「推演中」，横幅已经在', (await text(slow.page, '[data-whatif-loading]')).includes('推演中')
    && (await text(slow.page, '[data-whatif-banner]')).trim() === BANNER)
  release()
  await slow.page.waitForSelector('[data-whatif-result="unchanged"]')
  check('答复回来后加载提示消失', await slow.page.locator('[data-whatif-loading]').count() === 0)
  await slow.ctx.close()

  const flaky = await open('whatif', { reply: async (body, n) => (n === 1
    ? { status: 500, json: { detail: '推演服务暂时不可用' } } : { json: whatifReply(body) }) })
  await openMetric(flaky.page, 's12')
  await flaky.page.click('[data-whatif-toggle]')
  await flaky.page.waitForSelector('[data-whatif] [role="alert"]')
  check('失败时给出原因和「重试」', (await text(flaky.page, '[data-whatif] [role="alert"]')).includes('重试'))
  await flaky.page.locator('[data-whatif] [role="alert"]').getByRole('button', { name: '重试' }).click()
  await flaky.page.waitForSelector('[data-whatif-result="unchanged"]')
  check('重试之后正常显示', await flaky.page.locator('[data-whatif] [role="alert"]').count() === 0)
  await flaky.ctx.close()

  const open_ = await open('whatif', { reply: async (body) => ({ json: { ...whatifReply(body), sealed: false, seal: { sealed: false, ok: null } } }) })
  await openMetric(open_.page, 's12')
  await openWhatIf(open_.page)
  check('运行未封存时提示数据可能还会变', (await text(open_.page, '[data-whatif-unsealed]')) === '运行未封存，数据可能还会变')
  await open_.ctx.close()
})

/** 页面和面板正文都没有横向滚动（表格自己的横向滚动条不算：那是表格容器里的） */
async function overflow(page) {
  return page.evaluate(() => {
    const doc = document.documentElement
    const body = document.querySelector('[data-ev-body]')
    const box = document.querySelector('[data-whatif]')
    return { page: doc.scrollWidth - doc.clientWidth, body: body ? body.scrollWidth - body.clientWidth : 0,
      whatif: box ? box.scrollWidth - box.clientWidth : 0 }
  })
}

await section('窄屏', async () => {
  for (const theme of ['dark', 'light']) {
    const { ctx, page, errors } = await open('whatif', { width: 390, height: 844, theme })
    await openMetric(page, 's12')
    check(`390px（${theme}）：面板从底部抽出`, await page.locator('[data-evidence-panel="drawer"]').count() === 1)
    await openWhatIf(page)
    await page.locator('[data-whatif-input="vars.kpi.gmv"] [data-whatif-field]').fill('50000')
    await page.locator('[data-whatif-input="vars.kpi.gmv"] [data-whatif-field]').press('Enter')
    await page.waitForSelector('[data-whatif-result="ok"]')
    await page.locator(`[data-ev-query="${Q4}"] [data-whatif-row="0"] input`).check()
    await page.waitForSelector('[data-whatif-result="excluded"]')
    const o = await overflow(page)
    check(`390px（${theme}）：推演展开、勾了行也没有横向溢出`, o.page <= 0 && o.body <= 1 && o.whatif <= 1, JSON.stringify(o))
    await shot(page, `narrow-${theme}`)
    check(`390px（${theme}）：页面没有报错`, errors.length === 0, errors.join('；'))
    await ctx.close()
  }
  const rail = await open('whatif-rail', { width: 1100, height: 900 })
  await openMetric(rail.page, 's70')
  check('360px 画布右栏：面板栏内展开', await rail.page.locator('[data-evidence-panel="inline"]').count() === 1)
  await openWhatIf(rail.page)
  const o = await rail.page.evaluate(() => {
    const box = document.querySelector('[data-fixture="whatif-rail"]')
    const panel = document.querySelector('[data-whatif]')
    return { rail: box.scrollWidth - box.clientWidth, whatif: panel.scrollWidth - panel.clientWidth }
  })
  check('360px 画布右栏：推演区不撑宽右栏', o.rail <= 0 && o.whatif <= 1, JSON.stringify(o))
  await shot(rail.page, 'rail-dark')
  await rail.ctx.close()
})

/** 两个颜色的对比度（WCAG）：颜色是 getComputedStyle 给的 rgb / rgba / color(srgb …)，半透明的叠在底色上 */
function contrast(fg, bg, base) {
  const parse = (c) => {
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

await section('深浅色可读', async () => {
  for (const theme of ['dark', 'light']) {
    const { ctx, page } = await open('whatif', { theme })
    await openMetric(page, 's12')
    await openWhatIf(page)
    const c = await page.evaluate(() => {
      const banner = document.querySelector('[data-whatif-banner]')
      const panel = document.querySelector('[data-evidence-panel]')
      const s = getComputedStyle(banner)
      return { fg: s.color, bg: s.backgroundColor, base: getComputedStyle(panel).backgroundColor,
        weight: Number(s.fontWeight) }
    })
    const ratio = contrast(c.fg, c.bg, c.base)
    check(`${theme}：横幅文字对比度 ≥ 4.5`, ratio >= 4.5, `${ratio.toFixed(2)}（${c.fg} on ${c.bg}）`)
    check(`${theme}：横幅加粗`, c.weight >= 500, String(c.weight))
    await shot(page, `open-${theme}`)
    await ctx.close()
  }
})

await browser.close()
console.log(failed ? `\n✗ 数字推演：${failed} 项未通过` : '\n✓ 数字推演：全部通过')
process.exit(failed ? 1 : 0)
