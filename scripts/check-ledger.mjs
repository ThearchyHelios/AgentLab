#!/usr/bin/env node
// 口径账本的界面检查（docs/design/next-round/02-口径账本.md 做法 3–6、8 的界面，第 4 波 W4-c）。
//
// 守的是：
//   - 证据面板「口径」一步：比对还没出结果时写「正在比对口径」，按退避再取（不靠实时流：ledger.matched 在事件总线关闭
//     之后才追加）；等满了还没有结论照实说、给「再取一次」，点了重新取、取到就画；三种情况（来自口径 / 相关口径 / 这次的
//     推理）各写各的；
//   - 口径对账：核对参数（推断的参数要先核对）→ 发起（认不出列怎么配时照 409 带回的选项选对照的指标，再发起）→ 结果卡片：
//     这次 / 口径 / 差值 / 相对差四个数各自取值、点开是出处链；请求体逐项核对；做完的卡片上问「差在哪」的说明有没有帮助
//     （说明有用 / 看不懂，统计 E2）：请求体只有 verdict，选过的按下、写各几个人，能改主意；没做完的不问；
//   - 「按口径」那个数转抄口径卡的指标：出处链跨卡接上口径卡那个指标的一步，输入来源里点一下跳到那一步，那一步的输入
//     点一下跳到口径的取数查询；
//   - 可沉淀的问题：工作流列表弹窗里的抽屉，三类来源分开写；「固化成口径草稿」走 from-run 的 freeze（带同组的别的运行）、
//     打开画布；聚类按人挑的那一轮固化；「不是口径」要写理由；
//   - 使用者看到的入口照服务端的动作：不能出具的写原因（请搭建者出具）、不摆出具按钮，能对账的照样对账；出具过的正式运行
//     不给打开运行的链接（使用者读不了它的原始记录），写由搭建者查看；
//   - 设置里口径账本一组：和其余偏好共用保存条，整组写 ledger，值不合规不让存。
//
// 不写库：所有写请求都在浏览器层拦下（伪造的答复），核对请求体；认不出的写请求一律 409 并记账，检查结束时必须是空的。
// 夹具见 scripts/ledger-fixtures.mjs（挂在可点击证据的夹具 evidence-doc.json 上，只用通用名）。
//
// 跑之前前端得起着（./scripts/dev.sh），默认连 5273：
//   AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-ledger.mjs
import { readFileSync } from 'node:fs'
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'
import {
  ISSUE, QUEUE, RECONCILE, RECONCILE_VALUES, WHOLE, WHOLE_LABELS, copiedMetricDetail, ledgerOf, metricDetail, reconciledOf,
} from './ledger-fixtures.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const ONLY = (process.env.CHECK_ONLY ?? '').split(',').map((s) => s.trim()).filter(Boolean)
const fx = JSON.parse(readFileSync(new URL('../frontend/src/run/__tests__/evidence-doc.json', import.meta.url), 'utf8'))
const RUN = fx.run_id

let failed = 0
const check = (name, cond, detail = '') => {
  const d = String(detail ?? '').replace(/\s+/g, ' ').slice(0, 200)
  console.log(`  ${cond ? '✓' : '✗'} ${name}${d ? ` — ${d}` : ''}`)
  if (!cond) failed++
}
const opened = new Set()
async function section(name, fn) {
  if (ONLY.length && !ONLY.some((k) => name.includes(k))) return
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
const until = async (fn, ms = 6000, step = 100) => {
  const end = Date.now() + ms
  let v = await fn()
  while (!v && Date.now() < end) {
    await new Promise((r) => setTimeout(r, step))
    v = await fn()
  }
  return v
}
const stray = []
const ADMIN = { mode: 'off', username: null, display_name: null, role: 'admin', groups: [], verified: false }
const USER = { mode: 'local', username: 'carol', display_name: '使用者丙', role: 'user', groups: [], verified: true, user_can_chat: true }

const browser = await chromium.launch({ executablePath: CHROME })

/**
 * 开一页：/api/ 下的请求按 reply 答，记下每个请求（sent）；reply 不认的 GET 交给真服务端，不认的写请求一律 409 并记进
 * stray（检查结束时必须是空的）
 */
async function open(path, { me = ADMIN, reply = () => null, w = 1280, h = 900 } = {}) {
  const ctx = await browser.newContext({ viewport: { width: w, height: h } })
  opened.add(ctx)
  ctx.setDefaultTimeout(8000)
  const page = await ctx.newPage()
  const errors = []
  const sent = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('dialog', (d) => { errors.push(`原生对话框：${d.message()}`); void d.dismiss() })
  await page.route((u) => new URL(u).pathname.startsWith('/api/'), async (route) => {
    const req = route.request()
    const url = new URL(req.url())
    const p = decodeURIComponent(url.pathname.replace(/^\/api/, ''))
    let body = null
    try { body = req.postDataJSON() } catch { body = null }
    sent.push({ key: `${req.method()} ${p}`, body, at: Date.now() })
    if (p === '/auth/me') return route.fulfill({ json: me })
    const out = await reply(req.method(), p, body, url)
    if (out) return route.fulfill({ status: out.status ?? 200, json: out.json ?? {} })
    if (req.method() === 'GET') return route.continue()
    stray.push(`${req.method()} ${p}`)
    return route.fulfill({ status: 409, json: { detail: '检查脚本不写库' } })
  })
  await page.goto(`${WEB}${path}`, { waitUntil: 'domcontentloaded' })
  return { ctx, page, errors, sent }
}

/** 可点击证据的组件预览页：片段接口、证据图按夹具答，账本按 ledger() 答 */
function evidenceReply(ledger, extra = () => null) {
  return async (method, p, body, url) => {
    const more = await extra(method, p, body, url)
    if (more) return more
    if (method !== 'GET') return null
    if (p === `/runs/${RUN}/ledger`) return { json: await ledger() }
    const seg = p.match(new RegExp(`^/runs/${RUN}/evidence/segments/([^/]+)$`))
    if (seg) return fx.segments[seg[1]] ? { json: fx.segments[seg[1]] } : { status: 404, json: { detail: '报告中没有这个片段' } }
    if (p === `/runs/${RUN}/evidence`) return { json: fx.graph }
    // 组件预览页上别的几份夹具：不比对
    const other = p.match(/^\/runs\/([^/]+)\/ledger$/)
    if (other) return { json: { run_id: other[1], state: 'ineligible', reason: '检查脚本的夹具不比对口径' } }
    return null
  }
}

const panelPart = (page) => page.locator('[data-evidence-panel] [data-ev-ledger]')
async function openSeg(page, id) {
  await page.locator(`#evidence-wide [data-seg="${id}"]`).click()
  await page.waitForSelector('[data-evidence-panel]', { timeout: 4000 }).catch(() => {})
}

// ---------------------------------------------------------------- 正在比对、退避、等满了、再取一次

await section('比对还没出结果：正在比对口径、按退避再取、等满了照实说、再取一次', async () => {
  const state = { matched: false, gets: [] }
  const { page, errors } = await open('/ui-harness.html?evidence=1', { reply: evidenceReply(() => {
    state.gets.push(Date.now())
    return state.matched ? ledgerOf(RUN) : { run_id: RUN, state: 'pending', note: '封存后比对，不在封存范围内' }
  }) })
  await page.waitForSelector('[data-evidence-doc]', { timeout: 10000 })
  await openSeg(page, 's6')
  const waiting = await page.locator('[data-evidence-panel] [data-ledger-state="pending"]').waitFor({ timeout: 5000 }).then(() => true, () => false)
  check('「口径」一步写「正在比对口径」', waiting && (await panelPart(page).innerText()).includes('正在比对口径'))
  check('……说明比完会自动更新', (await panelPart(page).innerText()).includes('比完这里会自动更新'))
  check('还没比完时正文上不挂小标记', await page.locator('[data-ev-caliber-of]').count() === 0)
  await until(() => state.gets.length >= 4, 9000)
  const gaps = state.gets.slice(1).map((t, i) => t - state.gets[i])
  check('按退避再取：间隔越来越长（1、2、3 秒……）', state.gets.length >= 4 && gaps[0] >= 700 && gaps[1] >= gaps[0] - 150
    && gaps[2] >= gaps[1] - 150, gaps.map((g) => `${g}ms`).join(', '))
  // 把这一轮等待的起点挪到两分钟以前：下一次取回来还是 pending，就该照实说等满了，不再取
  await page.evaluate((run) => window.__ledger.setState((s) => ({ slots: { ...s.slots, [run]: { ...s.slots[run], since: Date.now() - 200_000 } } })), RUN)
  const timedOut = await page.locator('[data-evidence-panel] [data-ledger-state="timeout"]').waitFor({ timeout: 15000 }).then(() => true, () => false)
  check('等满了还没有结论：照实说，给「再取一次」', timedOut
    && (await panelPart(page).innerText()).includes('比对口径还没有结果') && await page.locator('[data-ledger-retry]').count() >= 1)
  const after = state.gets.length
  await page.waitForTimeout(3500)
  check('……等满了就不再取', state.gets.length === after, `${state.gets.length - after} 次`)
  state.matched = true
  await page.locator('[data-evidence-panel] [data-ledger-retry]').click()
  const drawn = await page.locator('[data-evidence-panel] [data-ev-ledger="caliber"]').waitFor({ timeout: 5000 }).then(() => true, () => false)
  check('点「再取一次」：重新取，比对过了就画出来', drawn && state.gets.length === after + 1, `${state.gets.length - after} 次`)
  check('正文上的小标记跟着出来', await page.locator('#evidence-wide [data-ev-caliber-of="s6"]').count() === 1)
  await page.waitForTimeout(2500)
  check('比对过了、没有要等的：不再取', state.gets.length === after + 1, `${state.gets.length - after} 次`)
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

// ---------------------------------------------------------------- 三种情况

await section('「口径」一步的三种情况：来自口径、相关口径、这次的推理', async () => {
  const { page, errors } = await open('/ui-harness.html?evidence=1', { reply: evidenceReply(() => ledgerOf(RUN, { retro: true })) })
  await page.waitForSelector('#evidence-wide [data-ev-caliber-of="s6"]', { timeout: 10000 })
  await openSeg(page, 's6')
  await page.locator('[data-evidence-panel] [data-ev-ledger="caliber"]').waitFor({ timeout: 5000 })
  const caliber = await panelPart(page).innerText()
  check('来自口径：口径、版本、级别、怎么认的、参数', caliber.includes('来自口径「周报口径」v2（受管）') && caliber.includes('等价已证明')
    && caliber.includes('week = 2026-W37'), caliber)
  check('……事后补比的写「按现在的口径回看」', caliber.includes('封存后比对，不在封存范围内 · 按现在的口径回看'), caliber)
  await openSeg(page, 's8')
  await page.locator('[data-evidence-panel] [data-ev-ledger="related"]').waitFor({ timeout: 5000 })
  const related = await panelPart(page).innerText()
  check('相关口径：差在哪 + 看差多少', related.includes('相关口径「订单口径」v1（已发布）') && related.includes('口径只保留状态为已支付的行')
    && await page.locator('[data-evidence-panel] [data-ledger-reconcile-open]').count() === 1, related)
  check('……这个数本身是这次的推理（照实说）', related.includes('这个数是这次的推理'), related)
  await openSeg(page, 's12')
  await page.locator('[data-evidence-panel] [data-ev-ledger="reasoning"]').waitFor({ timeout: 5000 })
  check('这次的推理：没有相关的已发布口径', (await panelPart(page).innerText()).includes('没有相关的已发布口径'))
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

// ---------------------------------------------------------------- 对账

await section('口径对账：核对参数 → 发起 → 选对照的指标 → 三个数各自点得开', async () => {
  const state = { posts: [], reconciled: [], polls: 0, feedback: [] }
  const extra = (method, p, body) => {
    // 「说明有用 / 看不懂」：服务端交回这次对账现在的反馈（看的人最后一次给的、各几个人；另有一个人之前说过有用）
    if (method === 'POST' && p === `/ledger/answers/${RUN}/reconcile/run-rec-1/feedback`) {
      state.feedback.push(body)
      const fb = { mine: body?.verdict, useful: 1 + (body?.verdict === 'useful' ? 1 : 0), unclear: body?.verdict === 'unclear' ? 1 : 0 }
      state.reconciled = state.reconciled.map((r) => ({ ...r, feedback: fb }))
      return { json: { run_id: 'run-rec-1', feedback: fb } }
    }
    if (method === 'POST' && p === `/ledger/answers/${RUN}/reconcile`) {
      state.posts.push(body)
      if (!body?.pairs) {
        return { status: 409, json: { detail: '认不出这次的哪一列对应口径的哪个指标，请选择要对照的指标', code: 'ledger_reconcile_needs_pairs',
                                      options: { columns: ['orders', 'refunds'], metrics: [{ id: 'orders', name: '订单数', unit: '单' }] } } }
      }
      state.reconciled = [reconciledOf('running')]
      return { status: 201, json: { run_id: 'run-rec-1', reconciled: state.reconciled[0] } }
    }
    const m = p.match(/^\/runs\/run-rec-1\/evidence\/metrics\/reconcile\/([^/]+)$/)
    if (method === 'GET' && m) return { json: metricDetail('run-rec-1', m[1]) }
    return null
  }
  const ledger = () => {
    // 发起之后：第一次取回来对账还在进行中，再取一次就做完了
    if (state.reconciled[0]?.status === 'running') {
      state.polls += 1
      if (state.polls > 1) state.reconciled = [reconciledOf('succeeded')]
    }
    return ledgerOf(RUN, { reconciled: state.reconciled })
  }
  const { page, errors } = await open('/ui-harness.html?evidence=1', { reply: evidenceReply(ledger, extra) })
  await page.waitForSelector('#evidence-wide [data-seg="s8"]', { timeout: 10000 })
  await openSeg(page, 's8')
  await page.locator('[data-evidence-panel] [data-ledger-reconcile-open]').click()
  const dialog = page.locator('[data-ledger-reconcile-dialog]')
  await dialog.waitFor({ timeout: 4000 })
  const text = await dialog.innerText()
  check('对账框：差在哪、写明只读', text.includes('口径只保留状态为已支付的行') && text.includes('只读'), text)
  check('参数是推断的：先核对（表单直接打开、写原因）', await page.locator('[data-ledger-params="edit"]').count() === 1
    && text.includes('参数按这次的 SQL 推断，请核对'))
  check('表单照动作里的入口字段画：推断出的值填好，有缺省值的写明留空用缺省值',
    await page.locator('[data-ledger-param-input="start"]').inputValue() === '2026-09-01'
    && await page.locator('[data-ledger-param-input="store"]').inputValue() === '' && text.includes('留空用口径的缺省值'))
  await page.locator('[data-ledger-param-input="start"]').fill('2026-09-03')
  await page.locator('[data-ledger-reconcile-launch]').click()
  await until(() => state.posts.length === 1)
  check('第一次发起：带核对过的参数（confirm），空着的不送', JSON.stringify(state.posts[0]) === JSON.stringify({
    query: 'q-orders', workflow_id: 'wf-orders', version: 1, inputs: { start: '2026-09-03' }, confirm: true }), JSON.stringify(state.posts[0]))
  const pairs = await page.locator('[data-ledger-pairs]').waitFor({ timeout: 4000 }).then(() => true, () => false)
  check('认不出列怎么配：照服务端给的选项画「选择要对照的指标」', pairs && await page.locator('[data-ledger-pair]').count() === 2
    && (await page.locator('[data-ledger-reconcile-problem]').innerText()).includes('请选择要对照的指标'))
  await page.locator('[data-ledger-reconcile-launch]').click()
  check('一组都没选：不发起，说要至少选一组', state.posts.length === 1
    && (await page.locator('[data-ledger-reconcile-problem]').innerText()).includes('至少选一组对照'))
  await page.locator('[data-ledger-pair="orders"]').selectOption('orders')
  await page.locator('[data-ledger-reconcile-launch]').click()
  await until(() => state.posts.length === 2)
  check('选好再发起：带上对照（这次的列 → 口径卡上的指标）', JSON.stringify(state.posts[1]?.pairs) === JSON.stringify([{ ours: 'orders', metric: 'orders' }])
    && state.posts[1]?.confirm === true, JSON.stringify(state.posts[1]))
  check('发起之后对账框收起', await until(async () => await dialog.count() === 0, 4000))
  const card = page.locator('[data-evidence-panel] [data-ledger-reconciled="run-rec-1"]')
  const running = await card.waitFor({ timeout: 6000 }).then(() => true, () => false)
  check('结果卡片：对账运行还在进行时写「对账进行中」', running && (await card.innerText()).includes('对账进行中'))
  check('……还没做完：不问说明有没有用', await card.locator('[data-ledger-feedback-give]').count() === 0)
  const done = await page.locator('[data-evidence-panel] [data-ledger-reconciled-state="succeeded"]').waitFor({ timeout: 12000 }).then(() => true, () => false)
  check('……再取一次（运行还没结束时接着等），做完了画出每组对照', done)
  await until(async () => (await card.innerText()).includes(RECONCILE_VALUES.diff_0), 5000)
  const cardText = (await card.innerText()).replace(/\s+/g, ' ')
  check('四个数：按你的问法、按口径、差值、相对差', cardText.includes(`按你的问法 ${RECONCILE_VALUES.ours_0}`)
    && cardText.includes(`按口径 ${RECONCILE_VALUES.cal_0}`) && cardText.includes(`差值 ${RECONCILE_VALUES.diff_0}`)
    && cardText.includes(`相对差 ${RECONCILE_VALUES.rel_0}`), cardText)
  check('……写参数从哪来、原答案里写的是什么', cardText.includes('按核对过的参数') && cardText.includes('原答案里写的是 8.7%'), cardText)

  // 「差在哪」的说明有没有帮人看懂（02 做法 10 的 E2）：做完的卡片上问一句，记成对账运行上的封存后事件
  const fbRow = card.locator('[data-ledger-feedback]')
  check('做完的卡片问「差在哪」的说明有没有帮助：说明有用 / 看不懂', (await fbRow.innerText().catch(() => '')).includes('说明有用')
    && await card.locator('[data-ledger-feedback-give]').count() === 2, await fbRow.innerText().catch(() => ''))
  await card.locator('[data-ledger-feedback-give="unclear"]').click()
  await until(() => state.feedback.length === 1)
  check('点「看不懂」：请求体只有 verdict，记在这次答案的这次对账上', JSON.stringify(state.feedback[0]) === JSON.stringify({ verdict: 'unclear' }),
    JSON.stringify(state.feedback[0]))
  const pressed = await until(async () => await card.locator('[data-ledger-feedback-give="unclear"]').getAttribute('aria-pressed') === 'true', 4000)
  const fbText = (await fbRow.innerText().catch(() => '')).replace(/\s+/g, ' ')
  check('……选过的按下，写各几个人（服务端给的数）', pressed && fbText.includes('1 人觉得有用') && fbText.includes('1 人看不懂'), fbText)
  await card.locator('[data-ledger-feedback-give="useful"]').click()
  await until(() => state.feedback.length === 2)
  const changed = await until(async () => await card.locator('[data-ledger-feedback-give="useful"]').getAttribute('aria-pressed') === 'true', 4000)
  check('改主意：再点「说明有用」照样记一条，按下的换过来', changed && JSON.stringify(state.feedback[1]) === JSON.stringify({ verdict: 'useful' })
    && await card.locator('[data-ledger-feedback-give="unclear"]').getAttribute('aria-pressed') === 'false')

  await card.locator('[data-ledger-value="diff_0"]').click()
  const drawer = page.locator('[data-evidence-panel="side"]').last()
  const opened = await until(async () => (await drawer.innerText().catch(() => '')).includes('差值'), 5000)
  check('点差值：打开它的出处链（同应用页点数）', opened, (await drawer.innerText().catch(() => '')).slice(0, 80))
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

// ---------------------------------------------------------------- 「按口径」那个数的出处跨卡追到取数查询

await section('对账结果里「按口径」那个数：出处跨卡追到口径的取数查询', async () => {
  const extra = (method, p) => {
    const m = p.match(/^\/runs\/run-rec-1\/evidence\/metrics\/reconcile\/([^/]+)$/)
    if (method === 'GET' && m) {
      return { json: m[1] === 'cal_0' ? copiedMetricDetail('run-rec-1', m[1]) : metricDetail('run-rec-1', m[1]) }
    }
    return null
  }
  const { page, errors } = await open('/ui-harness.html?evidence=1', {
    reply: evidenceReply(() => ledgerOf(RUN, { reconciled: [reconciledOf('succeeded')] }), extra) })
  await page.waitForSelector('#evidence-wide [data-seg="s8"]', { timeout: 10000 })
  await openSeg(page, 's8')
  const card = page.locator('[data-evidence-panel] [data-ledger-reconciled="run-rec-1"]')
  await card.waitFor({ timeout: 6000 })
  await until(async () => (await card.innerText()).includes(RECONCILE_VALUES.cal_0), 5000)
  await card.locator('[data-ledger-value="cal_0"]').click()
  const drawer = page.locator('[data-evidence-panel="side"]').last()
  const up = drawer.locator('[data-ev-upstream="card"]')
  const shown = await up.waitFor({ timeout: 5000 }).then(() => true, () => false)
  const upText = shown ? (await up.innerText()).replace(/\s+/g, ' ') : ''
  check('点「按口径」：出处链接上口径卡的那个指标（转抄的口径卡指标）', shown && upText.includes('转抄的口径卡指标')
    && upText.includes('取自口径卡「口径卡」的指标「订单数」') && upText.includes('1,180单'), upText)
  check('……写那个指标的原式、它的输入追到了原始数据', upText.includes("cell(nodes.fetch, 0, 'orders')")
    && await up.locator('[data-ev-upstream-origin="traced"]').count() === 1, upText)
  check('指标这一步：输入都追到了原始数据，不挂「未追到原始数据」', await drawer.locator('[data-ev-untraced-chip]').count() === 0
    && await drawer.locator('[data-ev-metric] [data-ev-origin="traced"]').count() === 1)
  const src = drawer.locator('[data-ev-source="nodes.card.metrics[0].value"]')
  const srcText = await src.innerText().catch(() => '')
  check('输入来源：转抄的那一项说清取自另一张口径卡、那个指标的输入都追到了原始数据',
    srcText.includes('取自另一张口径卡的指标，那个指标的输入都追到了原始数据')
    && srcText.includes('查看口径卡「口径卡」的这个指标'), srcText)
  await src.locator('[data-ev-goto]').click()
  const focused = await until(() => page.evaluate(() => document.activeElement?.hasAttribute('data-ev-upstream-head') === true), 3000)
  check('点「查看口径卡「口径卡」的这个指标」：焦点落到那一步、描一下边', focused
    && await up.getAttribute('data-flash') === 'focus')
  await up.locator(`[data-ev-input="cell(nodes.fetch, 0, 'orders')"]`).click()
  const query = drawer.locator('[data-ev-query="q-cal-fetch"]')
  const jumped = await until(async () => await query.getAttribute('data-flash') === 'focus', 3000)
  check('那个指标的输入点一下：跳到口径的取数查询（SQL、被引用的那一格都在）', jumped
    && (await query.innerText()).includes('SELECT COUNT(*) AS orders FROM orders')
    && await query.locator('td[data-highlight="cell"]').count() === 1)
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

// ---------------------------------------------------------------- 可沉淀的问题

const WF = { id: 'wf-led-check', name: '检查用工作流', description: '', version: 1, status: 'draft', tags: [], is_template: false,
  graph: { nodes: [{ id: 'in', type: 'input', position: { x: 0, y: 0 }, data: { label: '入口', config: { fields: [] } } }], edges: [] } }
const FROZEN = { ...WF, id: 'wf-frozen', name: '上周销售额是多少（固化的口径草稿）', tags: ['certified'] }

await section('可沉淀的问题：工作流列表的抽屉，三类来源分开写，固化成口径草稿、不是口径要写理由', async () => {
  const state = { queue: structuredClone(QUEUE), freezes: [], dismisses: [] }
  const reply = (method, p, body) => {
    if (method === 'GET' && p === `/workflows/${WF.id}`) return { json: WF }
    if (method === 'GET' && p === `/workflows/${FROZEN.id}`) return { json: FROZEN }
    if (method === 'POST' && p === '/workflows/validate') return { json: { ok: true, issues: [] } }
    if (method === 'POST' && p === '/workflows/variables') return { json: { variables: [], issues: [] } }
    if (p.startsWith('/conversations')) return { json: method === 'GET' ? [] : { id: 'cv', kind: 'canvas', title: '', turns: [] } }
    if (method === 'GET' && p === '/ledger/certify-queue') return { json: state.queue }
    if (method === 'POST' && p === '/ledger/certify-queue/dismiss') {
      state.dismisses.push(body)
      return { json: { ok: true, item_id: body?.item_id } }
    }
    if (method === 'POST' && p === '/copilot/from-run') {
      state.freezes.push(body)
      return { json: { workflow_id: FROZEN.id, name: FROZEN.name, nodes: 5, edges: 4, params: ['p1'], metrics: ['m1'], sources: [] } }
    }
    return null
  }
  const { page, errors } = await open(`/studio/${WF.id}`, { reply })
  await page.locator('button[title="切换、新建、从模板新建工作流"]').click()
  await page.locator('[data-certify-open]').click()
  const drawer = page.locator('[data-certify-drawer]')
  await drawer.waitFor({ timeout: 5000 })
  await page.locator('[data-certify-item]').first().waitFor({ timeout: 5000 })
  const kinds = await page.locator('[data-certify-kind]').evaluateAll((els) => els.map((e) => e.getAttribute('data-certify-kind')))
  check('三类来源分开列：确定性重复、问题聚类、被反复用的答案', JSON.stringify(kinds) === JSON.stringify(['shape', 'cluster', 'reused']), kinds.join(','))
  const text = (await drawer.innerText()).replace(/\s+/g, ' ')
  check('各类写明是怎么认出来的（聚类不等于口径相同）', text.includes('确定性重复：同样形状的 SQL') && text.includes('问法相近不等于口径相同'), text.slice(0, 200))
  check('确定性重复：几次运行、几段对话、参数候选、已有相关口径', text.includes('4 次运行 · 3 段对话') && text.includes('1 个参数候选 · 1 个口径常量')
    && text.includes('已有相关口径'), text.slice(0, 300))
  check('被反复用的答案：怎么被用的、已固化成哪张草稿', text.includes('导出 2 次 · 在画布里打开过') && text.includes('本月新客数（固化的口径草稿）'))
  const cluster = page.locator('[data-certify-item="cluster:bb22"]')
  check('问题聚类没有共同的 SQL：不给整项固化，按人挑的那一轮固化', await cluster.locator('[data-certify-freeze]').count() === 0
    && await cluster.locator('[data-certify-freeze-turn]').count() === 2)

  await cluster.locator('[data-certify-dismiss]').click()
  const dialog = page.getByRole('dialog', { name: '这不是口径' })
  await dialog.waitFor({ timeout: 4000 })
  check('「不是口径」不写理由：确认键不能点', await dialog.getByRole('button', { name: '确认不是口径' }).isDisabled()
    && state.dismisses.length === 0)
  await dialog.getByRole('textbox').fill('问的是各门店的临时核对')
  await dialog.getByRole('button', { name: '确认不是口径' }).click()
  await until(() => state.dismisses.length === 1)
  check('写了理由才记下：请求带这一项、种类和理由', JSON.stringify(state.dismisses[0]) === JSON.stringify({
    item_id: 'cluster:bb22', kind: 'cluster', note: '问的是各门店的临时核对' }), JSON.stringify(state.dismisses[0]))
  check('……这一项从抽屉里拿掉', await until(async () => await page.locator('[data-certify-item="cluster:bb22"]').count() === 0, 3000))

  await page.locator('[data-certify-item="shape:aa11"] [data-certify-freeze]').click()
  await until(() => state.freezes.length === 1)
  check('固化成口径草稿：走 from-run 的 freeze，带同组的别的运行和来源轮次', JSON.stringify(state.freezes[0]) === JSON.stringify({
    run_id: 'run-s1', freeze: true, peers: ['run-s2', 'run-s3'], conversation_id: 'c-1', turn_id: 't-1' }), JSON.stringify(state.freezes[0]))
  check('……打开画布里的草稿', await until(() => new URL(page.url()).pathname === `/studio/${FROZEN.id}`, 5000), page.url())
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

// ---------------------------------------------------------------- 使用者

await section('使用者看到的入口照服务端的动作：不能出具的写原因，能对账的照样对账，出具的运行不给链接', async () => {
  const EV = JSON.parse(readFileSync(new URL('../frontend/src/run/__tests__/evidence-doc.json', import.meta.url), 'utf8'))
  const RUN_U = 'run-user-led'
  const at = new Date(Date.now() - 86400e3).toISOString()
  const conv = { id: 'c-user-led', title: '问数据：上周的周报', kind: 'chat', workflow_id: null, archived: false, created_at: at,
    last_active_at: at, turn_count: 1, last_question: '上周的周报', last_status: 'done', last_run_id: RUN_U }
  const turn = { id: 'tu1', seq: 0, question: '上周的周报', answer: '（周报原文）', explanation: '', graph: null, run_id: RUN_U,
    status: 'done', error: '', review: null, created_at: at,
    meta: { v: 1, runId: RUN_U, runClass: 'exploratory', runStatus: 'succeeded', evidence: true } }
  const refused = { ...ISSUE, available: false, auto: false, reason: '这份口径没有被你能使用的应用固定，不能由你按口径出具，请搭建者出具' }
  delete refused.auto_reason
  const LED = ledgerOf(RUN_U, { labels: WHOLE_LABELS, whole: WHOLE, actions: [refused, RECONCILE],
    // 出具过的是另一份口径（订单口径）：周报口径这一项还没出具过，照服务端的原因写为什么不能出具
    issued: [{ run_id: 'run-iss-u', status: 'succeeded', workflow_id: 'wf-orders', version: 1, tier: 'degraded', tier_text: '降档出具', auto: true }] })
  const reply = (method, p) => {
    if (method === 'GET' && p === '/conversations') return { json: [conv] }
    if (method === 'GET' && p === `/conversations/${conv.id}`) return { json: { ...conv, turns: [turn] } }
    if (p.startsWith('/conversations')) return { json: {} }
    if (method === 'GET' && p === `/runs/${RUN_U}`) return { json: { id: RUN_U, status: 'succeeded', run_class: 'exploratory', output: EV.output, usage: {} } }
    if (method === 'GET' && p === `/runs/${RUN_U}/events`) return { json: [] }
    if (method === 'GET' && p === `/runs/${RUN_U}/ledger`) return { json: LED }
    if (method === 'GET' && p === `/runs/${RUN_U}/evidence`) return { json: EV.graph }
    if (method === 'GET' && p.startsWith('/artifacts/')) return { json: { id: EV.doc_artifact, content: { ...EV.doc, run_id: RUN_U } } }
    if (method === 'GET' && p.startsWith('/approvals')) return { json: [] }
    return null
  }
  const { page, errors } = await open(`/chat/${conv.id}`, { me: USER, reply })
  const box = page.locator('[data-ledger-turn="matched"]').first()
  const shown = await box.waitFor({ timeout: 12000 }).then(() => true, () => false)
  const text = shown ? (await box.innerText()).replace(/\s+/g, ' ') : ''
  check('使用者也看到摘要', text.includes('3 个数来自口径「周报口径」v2（受管）'), text)
  check('不能出具：照服务端的原因写「请搭建者出具」，不摆出具按钮', text.includes('请搭建者出具')
    && await page.locator('[data-ledger-issue-button]').count() === 0, text)
  check('能对账：照样摆对账按钮', await page.locator('[data-ledger-turn-reconcile]').count() === 1)
  const issued = page.locator('[data-ledger-issued="run-iss-u"]')
  const issuedText = (await issued.innerText().catch(() => '')).replace(/\s+/g, ' ')
  check('自动出具过的正式运行：写「按口径自动出具」、哪份口径和档位', issuedText.includes('按口径自动出具')
    && issuedText.includes('「订单口径」v1（已发布）') && issuedText.includes('降档出具'), issuedText)
  check('……使用者不给打开运行的链接，写由搭建者查看', await issued.locator('a').count() === 0 && issuedText.includes('由搭建者在记录页查看'), issuedText)
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

// ---------------------------------------------------------------- 设置

await section('设置里的口径账本一组：共用保存条，整组写 ledger，值不合规不让存', async () => {
  const state = { puts: [], ledger: { auto_issue: true, issue_daily_limit: 20, match_timeout_s: 8, recall_limit: 50 } }
  const reply = (method, p, body) => {
    if (method === 'GET' && p === '/settings') return { json: { run: {}, limits: { max_agent_steps: 100 }, apps: {}, ledger: state.ledger } }
    if (method === 'PUT' && p === '/settings') {
      state.puts.push(body)
      if (body?.values?.ledger) state.ledger = body.values.ledger
      return { json: { ledger: state.ledger } }
    }
    return null
  }
  const { page, errors } = await open('/settings/prefs', { reply })
  const sec = page.locator('[data-ledger-settings]')
  await sec.waitFor({ timeout: 8000 })
  const text = await sec.innerText()
  check('四项都在：自动出具、每日上限、比对时限、升版后重算几组', ['自动按口径正式出具', '按口径正式出具每日上限', '口径比对时限（秒）',
    '口径升版后每次最多重算几组'].every((t) => text.includes(t)), text)
  check('显示的是服务端生效的值', await page.locator('[data-ledger-setting="issue_daily_limit"]').inputValue() === '20'
    && await page.locator('[data-ledger-setting="recall_limit"]').inputValue() === '50'
    && await page.locator('[data-ledger-setting="auto_issue"]').isChecked())
  await page.locator('[data-ledger-setting="recall_limit"]').fill('5000')
  const bar = page.locator('[data-prefs-bar]')
  check('不合规的值：写明范围，保存键不能点', (await sec.innerText()).includes('请填写 0 到 1,000 之间的整数')
    && await bar.getByRole('button', { name: '保存设置' }).isDisabled())
  await page.locator('[data-ledger-setting="recall_limit"]').fill('30')
  await page.locator('[data-ledger-setting="auto_issue"]').uncheck()
  check('保存条写改了哪几项（服务端的叫法）', (await bar.innerText()).includes('有 2 项未保存')
    && (await bar.innerText()).includes('自动按口径正式出具') && (await bar.innerText()).includes('口径升版后每次最多重算几组'))
  await bar.getByRole('button', { name: '保存设置' }).click()
  await until(() => state.puts.length === 1)
  check('整组写 ledger（没改的项照原值带上，不会被冲回默认）', JSON.stringify(state.puts[0]?.values) === JSON.stringify({
    ledger: { auto_issue: false, issue_daily_limit: 20, match_timeout_s: 8, recall_limit: 30 } }), JSON.stringify(state.puts[0]))
  check('存完：保存条写「已保存」', await until(async () => (await bar.innerText().catch(() => '')).includes('已保存'), 3000))
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
})

check('认不出的写请求一个都没有（都在浏览器层拦下）', stray.length === 0, stray.join(' | '))
await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 口径账本全部通过')
process.exit(failed ? 1 : 0)
