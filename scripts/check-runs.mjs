// 记录页（/runs）的检查：页签、状态筛选、分页、时间、待审批、删除保护、运行中接流、
// 失败的排错路径、封存凭证、提取模板。
//
// 守的是审查里抓到的几条死路：待审批的运行排在第 145 位、列表只取 100 条，点了
// 徽标也找不到；输入「失败」筛出来的是两条成功的运行；时间整体慢 8 小时；正在跑
// 的运行一动不动、却能删；封存过的正式运行一键就删掉了。
//
// 不写库：探针拦下所有非 GET 的 /api 请求，按场景回假响应。库里没有的形态（正在
// 跑的、封存过的正式运行）用 page.route 伪造 GET，用 routeWebSocket 伪造实时流。
// 跑之前前端和后端都得起着，并且指向同一份数据：
//   AGENTLAB_WEB=http://localhost:5373 AGENTLAB_API=http://localhost:8100/api node scripts/check-runs.mjs
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const API = process.env.AGENTLAB_API ?? 'http://localhost:8000/api'
const CHROME = process.env.CHROME_PATH
  ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}
const getJson = async (path) => (await fetch(API + path)).json()

const browser = await chromium.launch({ executablePath: CHROME })
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await ctx.newPage()
const errors = []
page.on('pageerror', (e) => errors.push(e.message))

// ---- 探针：非 GET 一律不放行，按场景回假响应 ----
const writes = []
const fakes = new Map()   // `${METHOD} ${pathname}` → (url) => {status, json}
let offline = false       // 断网：所有 /api 请求都够不着
let healthAborted = 0
await page.route('**/api/**', async (route) => {
  const r = route.request()
  const url = new URL(r.url())
  if (offline) {
    if (url.pathname === '/api/health') healthAborted++
    return route.abort('internetdisconnected')
  }
  if (r.method() === 'GET') {
    const fake = fakes.get(`GET ${url.pathname}`)
    if (fake) return route.fulfill(fake(url))
    return route.continue()
  }
  writes.push(`${r.method()} ${url.pathname}${url.search}`)
  const fake = fakes.get(`${r.method()} ${url.pathname}`)
  if (fake) return route.fulfill(fake(url, r))
  return route.abort()
})

const rowsOf = () => page.locator('[data-runs-list] [data-run-id]')
const settle = async () => {
  await page.waitForLoadState('networkidle')
  await page.waitForTimeout(350)
}
const statuses = () => rowsOf().evaluateAll((els) => els.map((e) => e.getAttribute('data-status')))
/** 做一个会触发列表重查的动作，等到那一次列表请求（limit=50）回来、列表画完 */
const listAfter = async (act, match) => {
  const done = page.waitForResponse((r) => {
    const u = new URL(r.url())
    return u.pathname === '/api/runs' && u.searchParams.get('limit') === '50' && match(u.searchParams)
  }, { timeout: 8000 }).catch(() => null)
  await act()
  await done
  await page.waitForTimeout(250)
}
const search = (text, match) => listAfter(() => page.locator('[data-runs-search]').fill(text), match)
const ids = () => rowsOf().evaluateAll((els) => els.map((e) => e.getAttribute('data-run-id')))
/** 页内记下详情 data-run-code 的每一次变化：一闪而过的中间态定时采样会漏掉 */
const recordCodes = () => page.evaluate(() => {
  const el = document.querySelector('[data-run-detail]')
  const seen = [el?.getAttribute('data-run-code')]
  window.__runCodes = seen
  if (el) new MutationObserver(() => seen.push(el.getAttribute('data-run-code')))
    .observe(el, { attributes: true, attributeFilter: ['data-run-code'] })
})
const recordedCodes = () => page.evaluate(() => window.__runCodes ?? [])

// ------------------------------------------------------------------ 页签

console.log('=== 页签与地址 ===')
await page.goto(`${WEB}/runs`, { waitUntil: 'networkidle' })
await rowsOf().first().waitFor({ timeout: 10000 })
check('默认是「全部」页签', await page.locator('[role=tab][data-tab=all]').getAttribute('aria-selected') === 'true')
check('页头写着「记录」', (await page.locator('[data-runs-page] header h1').innerText()).includes('记录'))
const headerH = await page.locator('[data-runs-page] > header').evaluate((el) => el.getBoundingClientRect().height)
check('页头和管理页一样高（48px，toast 不压工具栏）', headerH === 48, `${headerH}px`)

// 键盘：↓ 在行之间移动焦点，不打开
await rowsOf().first().focus()
await page.keyboard.press('ArrowDown')
const focused = await page.evaluate(() => document.activeElement?.getAttribute('data-run-id'))
check('↓ 把焦点移到下一行', focused === (await ids())[1] && !/\/runs\/./.test(new URL(page.url()).pathname))

await page.locator('[role=tab][data-tab=failed]').click()
await settle()
check('点「失败」地址变成 ?tab=failed', new URL(page.url()).searchParams.get('tab') === 'failed')
const failedStatuses = await statuses()
check('「失败」页签里全是失败的运行', failedStatuses.length > 0 && failedStatuses.every((s) => s === 'failed'),
  `${failedStatuses.length} 行`)
check('失败行的第二行是原因，不是输入', await page.locator('[data-runs-list] [data-run-reason]').count() === failedStatuses.length)

const runningReq = page.waitForRequest((r) => new URL(r.url()).pathname === '/api/runs'
  && new URL(r.url()).searchParams.get('status') === 'running,queued')
await page.locator('[role=tab][data-tab=running]').click()
check('「运行中」页签按 status=running,queued 查服务端', await runningReq.then(() => true, () => false))
await settle()
check('「运行中」页签里只有在跑的', (await statuses()).every((s) => s === 'running' || s === 'queued'))

await page.goto(`${WEB}/runs?tab=bogus`, { waitUntil: 'networkidle' })
await page.waitForTimeout(300)
check('认不出的页签落回「全部」并纠正地址', !new URL(page.url()).searchParams.has('tab'))

// ------------------------------------------------------------------ 待审批

console.log('\n=== 待审批页签 ===')
const pending = await getJson('/approvals?status=pending&limit=500')
// 「别处批掉了」要用的实时流：routeWebSocket 只管注册之后加载的页面，先挂上
let resumeSocket = false
if (pending.length) {
  const target = pending[0]
  const lastSeq = (await getJson(`/runs/${target.run_id}/events`)).at(-1)?.seq ?? 0
  await page.routeWebSocket(new RegExp(`/api/runs/${target.run_id}/stream`), (ws) => {
    resumeSocket = true
    const t = Date.now() / 1000
    ws.send(JSON.stringify({ seq: lastSeq + 1, type: 'run.resumed', node_id: target.node_id, ts: t, data: { actor: '张工' } }))
    ws.send(JSON.stringify({ seq: lastSeq + 2, type: 'node.started', node_id: target.node_id, ts: t + 0.05, data: { resumed: true } }))
  })
}
await page.goto(`${WEB}/runs?tab=approvals`, { waitUntil: 'networkidle' })
await page.waitForTimeout(400)
const approvalRows = page.locator('[data-approvals-list] [data-approval-id]')
const shownApprovals = await approvalRows.evaluateAll((els) => els.map((e) => e.getAttribute('data-approval-id')))
check('沙箱里的待审批都在页签里', pending.length > 0 && pending.every((a) => shownApprovals.includes(a.id)),
  `API ${pending.length} 条 / 页面 ${shownApprovals.length} 条`)
const allRuns = await getJson('/runs?limit=200')
const deep = pending.filter((a) => allRuns.findIndex((r) => r.id === a.run_id) >= 100)
check('排在最近 100 条之外的待审批也看得到（原来的死路）', deep.every((a) => shownApprovals.includes(a.id)),
  `${deep.length} 条在第 100 名之后`)
check('每条都写着等了多久', await page.locator('[data-approvals-list] [data-approval-age]').count() === shownApprovals.length)
if (pending.length) {
  const rowText = await page.locator(`[data-approval-id="${pending[0].id}"]`).innerText()
  check('每条写着工作流名和节点', rowText.includes(pending[0].workflow_name ?? '') && rowText.includes(pending[0].node_label ?? pending[0].node_id),
    rowText.replace(/\s+/g, ' ').slice(0, 80))
  check('每条有处理入口', rowText.includes('去处理'))
}
const badge = await page.locator('[role=tab][data-tab=approvals]').innerText()
check('页签上的数和待审批条数一致', badge.includes(String(pending.length)), badge.replace(/\s+/g, ' '))
if (pending.length) {
  const target = pending[0]
  await page.locator(`[data-approval-id="${target.id}"]`).click()
  await page.waitForURL(`**/runs/${target.run_id}**`)
  await page.locator('[data-run-detail]').waitFor()
  await page.waitForTimeout(500)
  check('点进去是那次运行，地址保留 ?tab=approvals', new URL(page.url()).searchParams.get('tab') === 'approvals')
  check('详情显示等审批的横幅', await page.locator('[data-run-banner=waiting]').count() === 1)
  check('审批卡在时间线里', await page.locator(`[data-approval-card="${target.id}"]`).count() === 1)
  check('详情头的状态是「等待审批」', await page.locator('[data-run-detail]').getAttribute('data-run-code') === 'waiting')
  const docScroll = await page.evaluate(() => document.scrollingElement.scrollHeight - innerHeight)
  check('整页不滚动：滚的是时间线自己', docScroll <= 0, `溢出 ${docScroll}px`)

  // 别处（画布、问数据、另一个标签页）把这张批掉了：catalog 轮询发现审批没了。
  // 这一刻 run 还没重查的话是「interrupted、没有审批」，会被错当成已挂起——
  // 要一起重查，看到它接着跑了，并接上实时流
  // 后端先把审批记成已回复、再把运行改成 running：第一次重查故意落在这个空档里
  // （运行还是 interrupted、审批已经没了），不能就此判成挂起
  const full = await getJson(`/runs/${target.run_id}`)
  let runReads = 0
  fakes.set('GET /api/approvals', () => ({ status: 200, json: [] }))
  fakes.set(`GET /api/runs/${target.run_id}`, () => ({
    status: 200, json: { ...full, status: runReads++ === 0 ? 'interrupted' : 'running' },
  }))
  await recordCodes()
  const resumed = await page.waitForFunction(
    () => document.querySelector('[data-run-detail]')?.getAttribute('data-run-code') === 'running',
    null, { timeout: 9000 },
  ).then(() => true, () => false)
  await page.waitForTimeout(300)
  const codes = await recordedCodes()
  check('别处批掉后：接上实时流、状态变成运行中', resumed && resumeSocket, codes.at(-1) ?? '')
  check('中间没有闪成「已挂起」', !codes.includes('held'), [...new Set(codes)].join(' → '))
  fakes.delete('GET /api/approvals')
  fakes.delete(`GET /api/runs/${target.run_id}`)

  // 别处批掉，剩下的几步在下一轮轮询之前就跑完了（审批靠近末尾的都这样）：
  // 重查时运行已经是已完成。头上跟着变不够，别处发生的事件要补进时间线——
  // 原来只在「还在跑」时接流，这种就停在审批卡上，连「放行」那一行都没有
  const other = pending[1] ?? pending[0]
  const otherRun = await getJson(`/runs/${other.run_id}`)
  const otherEvents = await getJson(`/runs/${other.run_id}/events`)
  const lastOther = otherEvents.at(-1)?.seq ?? 0
  const tf = Date.now() / 1000
  const tail = [
    { seq: lastOther + 1, type: 'human.resolved', node_id: other.node_id, ts: tf, data: { response: { approved: true }, actor: '张工' } },
    { seq: lastOther + 2, type: 'run.resumed', node_id: null, ts: tf, data: { actor: '张工' } },
    { seq: lastOther + 3, type: 'node.finished', node_id: other.node_id, ts: tf + 0.01, data: { duration_ms: 1 } },
    { seq: lastOther + 4, type: 'run.finished', node_id: null, ts: tf + 0.02,
      data: { output: { answer: '已发布' }, usage: {}, timing: { wall_ms: 1000, active_ms: 30, wait_ms: 970 } } },
  ]
  await page.goto(`${WEB}/runs/${other.run_id}`, { waitUntil: 'networkidle' })
  await page.locator('[data-run-detail][data-run-code=waiting]').waitFor({ timeout: 8000 }).catch(() => {})
  const restPending = pending.filter((a) => a.id !== other.id)
  fakes.set('GET /api/approvals', (url) => ({
    status: 200,
    json: restPending.filter((a) => !url.searchParams.get('run_id') || a.run_id === url.searchParams.get('run_id')),
  }))
  fakes.set(`GET /api/runs/${other.run_id}`, () => ({
    status: 200,
    json: { ...otherRun, status: 'succeeded', output: { answer: '已发布' }, finished_at: new Date().toISOString() },
  }))
  fakes.set(`GET /api/runs/${other.run_id}/events`, () => ({ status: 200, json: [...otherEvents, ...tail] }))
  await recordCodes()
  const finishedFast = await page.waitForFunction(
    () => document.querySelector('[data-run-detail]')?.getAttribute('data-run-code') === 'succeeded',
    null, { timeout: 12000 },
  ).then(() => true, () => false)
  await page.waitForTimeout(400)
  const fastCodes = await recordedCodes()
  check('别处批掉、已经跑完：状态变成已完成', finishedFast, [...new Set(fastCodes)].join(' → '))
  check('跑完之前没有闪成「已挂起」', !fastCodes.includes('held'), fastCodes.join(' → '))
  const fastStream = (await page.locator('[data-run-detail] > div').nth(0).innerText()).replace(/\s+/g, ' ')
  check('别处发生的事件补进了时间线（「张工 放行了」）', fastStream.includes('张工 放行了'), fastStream.slice(0, 120))
  const fastCount = (await page.locator('[data-run-detail] > header').innerText()).match(/([\d,]+) 条事件/)?.[1]
  check('详情头的事件数跟着补齐', fastCount === (otherEvents.length + tail.length).toLocaleString('en-US'),
    `${fastCount} / 应为 ${otherEvents.length + tail.length}`)
  fakes.delete('GET /api/approvals')
  fakes.delete(`GET /api/runs/${other.run_id}`)
  fakes.delete(`GET /api/runs/${other.run_id}/events`)

  // 断开期间别处批掉了：恢复那一刻重拉，同样可能撞上「审批已回复、运行还没改」
  // 的空档。恢复走的是另一条路（useOnReconnect），也不能就此判成挂起
  await page.goto(`${WEB}/runs/${target.run_id}`, { waitUntil: 'networkidle' })
  await page.locator('[data-run-detail][data-run-code=waiting]').waitFor({ timeout: 8000 }).catch(() => {})
  await recordCodes()
  healthAborted = 0
  offline = true
  // 等 catalog 的心跳失败、探活也失败，判成断开（之后 4 秒重试 /health）
  for (let i = 0; i < 60 && !healthAborted; i++) await page.waitForTimeout(200)
  await page.waitForTimeout(200)
  let gapReads = 0
  fakes.set('GET /api/approvals', () => ({ status: 200, json: [] }))
  fakes.set(`GET /api/runs/${target.run_id}`, () => ({
    status: 200, json: { ...full, status: gapReads++ === 0 ? 'interrupted' : 'running' },
  }))
  offline = false
  const back = await page.waitForFunction(
    () => document.querySelector('[data-run-detail]')?.getAttribute('data-run-code') === 'running',
    null, { timeout: 15000 },
  ).then(() => true, () => false)
  await page.waitForTimeout(300)
  const backCodes = await recordedCodes()
  check('断网恢复时撞上空档：认出它已经接着跑了', healthAborted > 0 && back, `探活失败 ${healthAborted} 次`)
  check('断网恢复时撞上空档：也没有闪成「已挂起」', !backCodes.includes('held'), backCodes.join(' → '))
  fakes.delete('GET /api/approvals')
  fakes.delete(`GET /api/runs/${target.run_id}`)
}

// ------------------------------------------------------------------ 筛选

console.log('\n=== 状态筛选 ===')
await page.goto(`${WEB}/runs`, { waitUntil: 'networkidle' })
await rowsOf().first().waitFor()
const listRequests = []
const onReq = (r) => { if (new URL(r.url()).pathname === '/api/runs') listRequests.push(new URL(r.url()).searchParams) }
page.on('request', onReq)
await search('失败', (p) => p.get('status') === 'failed')
const zh = await statuses()
check('输入「失败」只出失败的运行', zh.length > 0 && zh.every((s) => s === 'failed'), `${zh.length} 行`)
check('状态走服务端参数 status=failed，不当名称搜',
  listRequests.some((p) => p.get('status') === 'failed' && !p.get('q')))
check('提示按状态筛选', (await page.locator('[data-runs-hint]').innerText()).includes('失败'))
check('「全部」分段里「失败」亮着', await page.locator('[data-segment=failed]').getAttribute('aria-pressed') === 'true')

await search('已完成', (p) => p.get('status') === 'succeeded')
const done = await statuses()
check('输入「已完成」只出已完成的', done.length > 0 && done.every((s) => s === 'succeeded'))

await search('等待审批', (p) => p.get('status') === 'interrupted')
const waitingIds = await ids()
check('输入「等待审批」能找到所有停在审批上的运行',
  pending.every((a) => waitingIds.includes(a.run_id)) && (await statuses()).every((s) => s === 'waiting'),
  `${waitingIds.length} 行`)

await search('', (p) => !p.get('status'))
await listAfter(() => page.locator('[data-segment=cancelled]').click(), (p) => p.get('status') === 'cancelled')
const cancelled = await statuses()
check('点「已取消」分段只出已取消的', cancelled.length > 0 && cancelled.every((s) => s === 'cancelled'))
check('分段写进地址 ?status=cancelled', new URL(page.url()).searchParams.get('status') === 'cancelled')
await page.locator('[data-segment=all]').click()
await settle()

await search('zz一个不存在的工作流zz', (p) => p.get('q') === 'zz一个不存在的工作流zz')
check('没有匹配时说「没有匹配」，不说「还没有运行记录」',
  await page.getByText('没有匹配的运行').count() === 1 && await page.getByText('还没有运行记录').count() === 0)
await page.getByRole('button', { name: '清除筛选' }).click()
await settle()
check('「清除筛选」之后列表回来了', await rowsOf().count() > 0 && !new URL(page.url()).searchParams.has('q'))
page.off('request', onReq)

// ------------------------------------------------------------------ 分页

console.log('\n=== 分页 ===')
const firstPage = await rowsOf().count()
check('第一页 50 条', firstPage === 50, String(firstPage))
const beforeReq = page.waitForRequest((r) => new URL(r.url()).pathname === '/api/runs' && new URL(r.url()).searchParams.has('before'))
await page.locator('[data-runs-more]').click()
const req = await beforeReq
await settle()
const secondIds = await ids()
check('「加载更多」带 before 游标', !!new URL(req.url()).searchParams.get('before'))
check('加载后变成 100 条', secondIds.length === 100, String(secondIds.length))
check('没有重复的行', new Set(secondIds).size === secondIds.length)
const apiIds = (await getJson('/runs?limit=100')).map((r) => r.id)
check('两页拼起来和后端的前 100 条一致', JSON.stringify(apiIds) === JSON.stringify(secondIds))

// ------------------------------------------------------------------ 时间

console.log('\n=== 时间不再慢 8 小时 ===')
const sample = (await getJson('/runs?limit=5'))[0]
const shownTime = await page.locator(`[data-run-id="${sample.id}"] [data-run-time]`).innerText()
const [expected, naive, offset] = await page.evaluate((iso) => {
  const pad = (n) => String(n).padStart(2, '0')
  const hm = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`
  // 带不带 Z 都按 UTC 解析；naive 是以前把 UTC 当本地时间的读法
  const utc = new Date(/Z|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + 'Z')
  const local = new Date(iso.replace(/Z$|[+-]\d\d:?\d\d$/, ''))
  return [hm(utc), hm(local), new Date().getTimezoneOffset()]
}, sample.created_at)
check('列表时间按 UTC 换算成本地', shownTime.includes(expected), `显示 ${shownTime}，应为 ${expected}（created_at=${sample.created_at}）`)
if (offset !== 0) check('不是把 UTC 当本地的那个错读', !shownTime.includes(naive) || naive === expected, `错读会是 ${naive}`)
const title = await page.locator(`[data-run-id="${sample.id}"] [data-run-time]`).getAttribute('title')
check('悬停给完整时间和时区', /\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \(UTC[+-]\d{2}:\d{2}\)/.test(title ?? ''), title ?? '')

// ------------------------------------------------------------------ 失败的排错路径

console.log('\n=== 失败：指到节点、原因、下一步 ===')
const failedRuns = await getJson('/runs?status=failed&limit=200')
const authFail = failedRuns.find((r) => r.workflow_id && /401|Authentication/i.test(r.error ?? ''))
if (authFail) {
  const full = await getJson(`/runs/${authFail.id}`)
  // 实时流伪造成「接着跑之后又动起来」。routeWebSocket 只管注册之后加载的页面，
  // 所以要在打开详情之前挂上
  const last = (await getJson(`/runs/${authFail.id}/events`)).at(-1)
  await page.routeWebSocket(new RegExp(`/api/runs/${authFail.id}/stream`), (ws) => {
    const t = Date.now() / 1000
    ws.send(JSON.stringify({ seq: last.seq + 1, type: 'run.resumed', node_id: null, ts: t, data: { from: full.error_node_id } }))
    ws.send(JSON.stringify({ seq: last.seq + 2, type: 'node.started', node_id: full.error_node_id, ts: t + 0.1, data: { resumed: true } }))
  })
  await page.goto(`${WEB}/runs/${authFail.id}`, { waitUntil: 'networkidle' })
  await page.locator('[data-run-banner=failed]').waitFor()
  check('横幅指到失败的节点', await page.locator(`[data-failed-node="${full.error_node_id}"]`).count() === 1, full.error_node_id)
  check('原始异常翻成人话', (await page.locator('[data-failed-title]').innerText()).includes('鉴权'))
  check('有「接着跑」', await page.locator('[data-run-banner=failed] [data-action=continue]').count() === 1)
  check('有「去模型接入」', await page.locator('[data-run-banner=failed] [data-action=settings]').count() === 1)
  check('有「复制错误」', await page.locator('[data-run-banner=failed] [data-action=copy-error]').count() === 1)
  const href = await page.locator('[data-run-banner=failed] [data-action=locate]').getAttribute('href')
  check('「在画布中定位」带 run 和 focus', href === `/studio/${authFail.workflow_id}?run=${authFail.id}&focus=${full.error_node_id}`, href ?? '')
  check('失败的运行不给「提取模板」', await page.locator('[data-action=extract]').count() === 0)

  // 接着跑：POST 伪造成功，实时流在上面已经伪造好
  fakes.set(`POST /api/runs/${authFail.id}/continue`, () => ({
    status: 200, json: { ...full, status: 'running', error: null, finished_at: null },
  }))
  await page.locator('[data-run-banner=failed] [data-action=continue]').click()
  await page.locator('[data-run-feedback=continued]').waitFor({ timeout: 5000 }).catch(() => {})
  check('接着跑发的是 POST /continue', writes.includes(`POST /api/runs/${authFail.id}/continue`))
  check('接着跑之后留下回执', await page.locator('[data-run-feedback=continued]').count() === 1)
  const flipped = await page.waitForFunction(
    () => document.querySelector('[data-run-detail]')?.getAttribute('data-run-code') === 'running',
    null, { timeout: 5000 },
  ).then(() => true, () => false)
  check('接流后状态变成运行中、出现「停止」', flipped && await page.locator('[data-action=stop]').count() === 1)
  fakes.delete(`POST /api/runs/${authFail.id}/continue`)
} else {
  check('沙箱里有一条有工作流的 401 失败运行', false)
}

const missingInput = failedRuns.find((r) => /缺少必填输入/.test(r.error ?? '') && !r.workflow_id)
if (missingInput) {
  await page.goto(`${WEB}/runs/${missingInput.id}`, { waitUntil: 'networkidle' })
  await page.locator('[data-run-banner=failed]').waitFor()
  check('缺输入的失败不给「接着跑」（原样接着跑还会失败）',
    await page.locator('[data-run-banner=failed] [data-action=continue]').count() === 0)
  check('未保存的工作流不给「在画布中定位」', await page.locator('[data-action=locate]').count() === 0)

  // 补上缺的那一项重新运行：用这次的图快照（未保存的图也能重跑），其余输入照旧
  const field = missingInput.error.match(/缺少必填输入[：:]\s*(\S+)/)[1]
  const rerunBtn = page.locator('[data-run-banner=failed] [data-action=rerun]')
  check(`缺输入的失败给「补上「${field}」重新运行」`, await rerunBtn.count() === 1)
  const RERUN = 'fake0rerun000000000000000000000'
  let startBody = null
  fakes.set('POST /api/runs', (url, req) => {
    startBody = req.postDataJSON()
    return { status: 200, json: { ...missingInput, id: RERUN, status: 'queued', error: null } }
  })
  fakes.set(`GET /api/runs/${RERUN}`, () => ({ status: 200, json: { ...missingInput, id: RERUN, status: 'succeeded', error: null } }))
  fakes.set(`GET /api/runs/${RERUN}/events`, () => ({ status: 200, json: [] }))
  await rerunBtn.click()
  const ask = page.getByRole('dialog')
  await ask.waitFor()
  await ask.locator('input, textarea').first().fill('检查用的主题')
  await ask.getByRole('button', { name: '重新运行' }).click()
  const moved = await page.waitForURL((u) => u.pathname === `/runs/${RERUN}`, { timeout: 5000 }).then(() => true, () => false)
  check('补上之后发起新运行并跳过去', moved, page.url())
  check('新运行带着补上的那一项和原来的其余输入',
    startBody?.input?.[field] === '检查用的主题'
      && Object.keys(missingInput.input ?? {}).every((k) => k === field || k in (startBody?.input ?? {})),
    JSON.stringify(startBody?.input ?? null))
  check('跑的是这次运行时的图快照', Array.isArray(startBody?.graph?.nodes) && startBody.graph.nodes.length > 0 && !startBody.workflow_id)
  fakes.delete('POST /api/runs')

  // 正式运行缺输入：只能跑「当前」发布的版本。之后又发布过新版的话，带着原来
  // 的版本号会被后端 409（「vN 不是当前发布版本」），所以不带，并在弹窗里说清跑哪一版
  const published = (await getJson('/workflows')).find((w) => (w.published_version ?? 0) > 1)
  if (published) {
    const FORMAL = 'fake0formalmissing0000000000000'
    const old = published.published_version - 1
    const formalRun = {
      ...missingInput, id: FORMAL, run_class: 'formal', workflow_id: published.id, workflow_name: published.name,
      version: old, manifest_hash: null, manifest_seq: null,
    }
    const missingEvents = await getJson(`/runs/${missingInput.id}/events`)
    fakes.set(`GET /api/runs/${FORMAL}`, () => ({ status: 200, json: formalRun }))
    fakes.set(`GET /api/runs/${FORMAL}/events`, () => ({ status: 200, json: missingEvents }))
    fakes.set(`GET /api/runs/${FORMAL}/graph`, () => ({ status: 200, json: { graph: published.graph, workflow_id: published.id, version: old } }))
    let formalBody = null
    fakes.set('POST /api/runs', (url, req) => {
      formalBody = req.postDataJSON()
      return { status: 200, json: { ...formalRun, id: RERUN, status: 'queued', error: null, version: published.published_version } }
    })
    await page.goto(`${WEB}/runs/${FORMAL}`, { waitUntil: 'networkidle' })
    await page.locator('[data-run-banner=failed] [data-action=rerun]').click()
    const formalAsk = page.getByRole('dialog')
    await formalAsk.waitFor()
    const askText = (await formalAsk.innerText()).replace(/\s+/g, ' ')
    check('正式运行重跑：弹窗写明跑的是当前发布版本', askText.includes(`v${published.published_version}`) && askText.includes(`v${old}`),
      askText.slice(0, 90))
    await formalAsk.locator('input, textarea').first().fill('检查用的主题')
    await formalAsk.getByRole('button', { name: '重新运行' }).click()
    await page.waitForURL((u) => u.pathname === `/runs/${RERUN}`, { timeout: 5000 }).catch(() => {})
    check('正式运行重跑不带旧版本号（由后端取当前发布版本）',
      formalBody?.run_class === 'formal' && formalBody?.workflow_id === published.id
        && !('version' in (formalBody ?? {})) && !formalBody?.graph && formalBody?.input?.[field] === '检查用的主题',
      JSON.stringify(formalBody ?? null).slice(0, 120))
    fakes.delete('POST /api/runs')
    fakes.delete(`GET /api/runs/${FORMAL}`)
    fakes.delete(`GET /api/runs/${FORMAL}/events`)
    fakes.delete(`GET /api/runs/${FORMAL}/graph`)
  } else {
    check('沙箱里有一个发布过两版以上的工作流', false)
  }
}

// 条件表达式写错了：这一页的接着跑不带图，跑的还是那条写错的条件，只会再失败
// 一次。不给接着跑，回画布定位是主路
const syntaxFail = failedRuns.find((r) => /表达式语法错误|invalid syntax/.test(r.error ?? '') && r.workflow_id)
if (syntaxFail) {
  await page.goto(`${WEB}/runs/${syntaxFail.id}`, { waitUntil: 'networkidle' })
  await page.locator('[data-run-banner=failed]').waitFor()
  check('表达式写错的失败不给「接着跑」', await page.locator('[data-run-banner=failed] [data-action=continue]').count() === 0)
  const locate = page.locator('[data-run-banner=failed] [data-action=locate]')
  check('「在画布中定位」成了主按钮', await locate.count() === 1 && /\bbtn-primary\b/.test(await locate.getAttribute('class') ?? ''))
} else {
  check('沙箱里有一条表达式写错的失败运行', false)
}

// ------------------------------------------------------------------ 挂起

const held = (await getJson('/runs?status=interrupted&limit=200')).find((r) => !pending.some((a) => a.run_id === r.id))
if (held) {
  console.log('\n=== 挂起（没有审批的中断） ===')
  await page.goto(`${WEB}/runs/${held.id}`, { waitUntil: 'networkidle' })
  await page.locator('[data-run-detail]').waitFor()
  await page.waitForTimeout(500)
  check('显示「已挂起」而不是「等待审批」', await page.locator('[data-run-detail]').getAttribute('data-run-code') === 'held')
  check('挂起横幅带「接着跑」', await page.locator('[data-run-banner=held] [data-action=resume]').count() === 1)
}

// ------------------------------------------------------------------ 封存凭证

console.log('\n=== 封存凭证常驻 ===')
const sealed = allRuns.find((r) => r.status === 'succeeded' && r.manifest_hash && !r.workflow_id)
await page.goto(`${WEB}/runs/${sealed.id}`, { waitUntil: 'networkidle' })
await page.locator('[data-run-provenance]').waitFor()
check('凭证条写着「已封存」和清单哈希前缀',
  (await page.locator('[data-run-provenance]').innerText()).includes(sealed.manifest_hash.slice(0, 10)))
await page.locator('[data-verify-btn]').click()
await page.locator('[data-verify=ok], [data-verify=mismatch]').first().waitFor({ timeout: 8000 })
await page.waitForTimeout(4500)
check('核对结果 4 秒后还在（不是 toast）', await page.locator('[data-verify=ok]').count() === 1)
check('写明几条事件与清单一致', /\d+ 条事件与清单一致/.test(await page.locator('[data-verify=ok]').innerText()))
await page.locator('[data-prov-toggle]').click()
check('展开能看到完整清单哈希', (await page.locator('[data-prov-detail]').innerText()).includes(sealed.manifest_hash))
await page.goto(`${WEB}/runs/${allRuns[1].id}`, { waitUntil: 'networkidle' })
await page.goto(`${WEB}/runs/${sealed.id}`, { waitUntil: 'networkidle' })
await page.locator('[data-run-provenance]').waitFor()
check('切走再回来，核对结果还在', await page.locator('[data-verify=ok]').count() === 1)

// ------------------------------------------------------------------ 时长

console.log('\n=== 三种时长 ===')
const timed = allRuns.find((r) => r.usage?.wall_ms != null && r.usage?.wait_ms > 0)
if (timed) {
  await page.goto(`${WEB}/runs/${timed.id}`, { waitUntil: 'networkidle' })
  await page.locator('[data-run-telemetry]').waitFor()
  const wall = await page.locator('[data-telemetry=wall]').innerText()
  const active = await page.locator('[data-telemetry=active]').innerText()
  const wait = await page.locator('[data-telemetry=wait]').innerText()
  check('墙钟、执行、等人三项都有数', ![wall, active, wait].includes('—'), `${wall} / ${active} / ${wait}`)
  check('列表那一行的 title 分项写了三种时长',
    /墙钟 .+ · 执行 .+ · 等人 .+/.test(await page.locator(`[data-run-id="${timed.id}"] [data-run-duration]`).getAttribute('title') ?? ''))
}

// ------------------------------------------------------------------ 提取模板

console.log('\n=== 提取模板 ===')
check('已完成的探索运行有「提取模板」', await page.locator('[data-action=extract]').count() === 1)
const wfs = await getJson('/workflows')
fakes.set('POST /api/copilot/from-run', () => ({
  status: 200, json: { workflow_id: wfs[0].id, name: '检查用草稿', nodes: 3, edges: 2, dropped_nodes: 1, source_run: timed?.id },
}))
await page.locator('[data-action=extract]').click()
await page.waitForURL(`**/studio/${wfs[0].id}`, { timeout: 5000 }).catch(() => {})
check('提取成功后跳到新草稿', new URL(page.url()).pathname === `/studio/${wfs[0].id}`)
fakes.delete('POST /api/copilot/from-run')

// ------------------------------------------------------------------ 运行中：接流、停止、不能删

console.log('\n=== 运行中：实时接流、停止、不能删 ===')
const base = allRuns.find((r) => r.status === 'succeeded' && !r.workflow_id)
const FAKE = 'fake0running0000000000000000000'
const t0 = Date.now() / 1000 - 3
const liveRun = {
  ...base, id: FAKE, status: 'running', output: {}, usage: {}, manifest_hash: null, manifest_seq: null,
  created_at: new Date(t0 * 1000).toISOString(), started_at: new Date(t0 * 1000).toISOString(), finished_at: null,
}
const graph = { nodes: [
  { id: 'a', type: 'input', position: { x: 0, y: 0 }, data: { label: '输入', config: {} } },
  { id: 'b', type: 'llm', position: { x: 200, y: 0 }, data: { label: '总结', config: {} } },
], edges: [{ source: 'a', target: 'b' }] }
const liveEvents = [
  { seq: 1, type: 'run.started', node_id: null, ts: t0, data: { nodes: 2 } },
  { seq: 2, type: 'node.started', node_id: 'a', ts: t0 + 0.1, data: {} },
  { seq: 3, type: 'node.finished', node_id: 'a', ts: t0 + 0.2, data: { duration_ms: 100 } },
  { seq: 4, type: 'node.started', node_id: 'b', ts: t0 + 0.3, data: {} },
]
let liveStatus = 'running'
// 停下之后重读到的是后端收尾过的那一份：带结束时间和用量
fakes.set(`GET /api/runs/${FAKE}`, () => ({
  status: 200,
  json: liveStatus === 'running' ? liveRun
    : { ...liveRun, status: liveStatus, finished_at: new Date().toISOString(), usage: { wall_ms: 3000, active_ms: 3000, wait_ms: 0 } },
}))
fakes.set(`GET /api/runs/${FAKE}/events`, () => ({ status: 200, json: liveEvents }))
fakes.set(`GET /api/runs/${FAKE}/graph`, () => ({ status: 200, json: { graph, workflow_id: null, version: null } }))
fakes.set(`POST /api/runs/${FAKE}/cancel`, () => ({ status: 200, json: { ok: true } }))
// 左边列表里也放上这一条：跑完那一刻它要闪一下，而且闪完要摘掉
const listTop = await getJson('/runs?limit=50')
fakes.set('GET /api/runs', () => ({ status: 200, json: [{ ...liveRun, status: liveStatus }, ...listTop] }))
let socket = null
await page.routeWebSocket(new RegExp(`/api/runs/${FAKE}/stream`), (ws) => { socket = ws })
await page.goto(`${WEB}/runs/${FAKE}`, { waitUntil: 'domcontentloaded' })
await page.locator('[data-run-detail]').waitFor()
await page.waitForTimeout(600)
check('运行中的记录接上了实时流', socket != null)
check('详情头有「停止」', await page.locator('[data-action=stop]').count() === 1)
const clockA = await page.locator('[data-telemetry=wall]').innerText()
await page.waitForTimeout(700)
const clockB = await page.locator('[data-telemetry=wall]').innerText()
check('墙钟在走（mm:ss.s）', /^\d\d:\d\d\.\d$/.test(clockB) && clockA !== clockB, `${clockA} → ${clockB}`)
check('状态格写着当前节点', (await page.locator('[data-run-telemetry]').innerText()).includes('当前「总结」'))
await page.getByRole('button', { name: '更多操作' }).click()
const del = page.locator('[data-menu=delete]')
check('运行中「删除记录」不可点，并说明要先停止', await del.isDisabled() && (await del.innerText()).includes('先停止'))
await page.keyboard.press('Escape')
await page.locator('[data-action=stop]').click()
await page.waitForTimeout(300)
check('停止发的是 POST /cancel', writes.includes(`POST /api/runs/${FAKE}/cancel`))
// 后端的回应经实时流到达：节点收尾、运行取消、流结束
liveStatus = 'cancelled'
const t1 = Date.now() / 1000
socket?.send(JSON.stringify({ seq: 5, type: 'run.cancelled', node_id: null, ts: t1, data: { timing: { wall_ms: 3000, active_ms: 3000, wait_ms: 0 } } }))
// 流结束比取消事件晚一拍到（后端收尾完才发）：相位先变、那一行先改一次
await page.waitForTimeout(300)
socket?.send(JSON.stringify({ type: 'stream.end', status: 'cancelled', data: { status: 'cancelled' } }))
const flashRow = page.locator(`[data-runs-list] [data-run-id="${FAKE}"] .runs-row-changed`)
const flashed = await flashRow.waitFor({ state: 'attached', timeout: 1500 }).then(() => true, () => false)
await page.waitForTimeout(900)
check('流里的事件落进了时间线（状态变成已取消）', await page.locator('[data-run-detail]').getAttribute('data-run-code') === 'cancelled')
check('停下后「停止」消失', await page.locator('[data-action=stop]').count() === 0)
check('停下后不再转圈', await page.locator('[data-run-detail] .animate-spin').count() === 0)
// 相位一变先改一次那一行，紧接着流结束、重读运行又改一次（用量、结束时间）。
// 第二次改动不能把摘掉闪光的计时器清掉，否则这一行一直挂着，下次变状态也闪不起来
check('左边那一行状态变了闪一下', flashed)
await page.waitForTimeout(1600)
check('闪完就摘掉，不一直挂着', await flashRow.count() === 0)
fakes.delete('GET /api/runs')

// ------------------------------------------------------------------ 等了很久之后接着跑

console.log('\n=== 等了几天的审批批掉之后：计时读得懂 ===')
// 第一次开始在 8 天前，挂在审批上 8 天，刚被批掉、正接着跑。从第一次开始算的
// 秒表会读成「192:00:05.3」；墙钟要写跨度，执行和轮次头只数执行的部分
const LONG = 'fake0longwait00000000000000000'
const tl = Date.now() / 1000 - 8 * 86400 - 5
const tr = Date.now() / 1000 - 5
const longRun = {
  ...liveRun, id: LONG, created_at: new Date(tl * 1000).toISOString(), started_at: new Date(tl * 1000).toISOString(),
}
const longGraph = { nodes: [
  { id: 'a', type: 'input', position: { x: 0, y: 0 }, data: { label: '输入', config: {} } },
  { id: 'h', type: 'human', position: { x: 200, y: 0 }, data: { label: '人工把关', config: {} } },
  { id: 'b', type: 'llm', position: { x: 400, y: 0 }, data: { label: '总结', config: {} } },
], edges: [{ source: 'a', target: 'h' }, { source: 'h', target: 'b' }] }
const longEvents = [
  { seq: 1, type: 'run.started', node_id: null, ts: tl, data: { nodes: 3 } },
  { seq: 2, type: 'node.started', node_id: 'a', ts: tl + 0.1, data: {} },
  { seq: 3, type: 'node.finished', node_id: 'a', ts: tl + 0.2, data: { duration_ms: 100 } },
  { seq: 4, type: 'node.started', node_id: 'h', ts: tl + 0.3, data: {} },
  { seq: 5, type: 'human.requested', node_id: 'h', ts: tl + 0.4, data: { mode: 'approve', prompt: '放行吗？' } },
  { seq: 6, type: 'run.interrupted', node_id: null, ts: tl + 0.5, data: {} },
  { seq: 7, type: 'human.resolved', node_id: 'h', ts: tr, data: { response: { approved: true }, actor: '张工' } },
  { seq: 8, type: 'run.resumed', node_id: null, ts: tr, data: { actor: '张工' } },
  { seq: 9, type: 'node.finished', node_id: 'h', ts: tr + 0.1, data: { duration_ms: 1 } },
  { seq: 10, type: 'node.started', node_id: 'b', ts: tr + 0.2, data: {} },
]
fakes.set(`GET /api/runs/${LONG}`, () => ({ status: 200, json: longRun }))
fakes.set(`GET /api/runs/${LONG}/events`, () => ({ status: 200, json: longEvents }))
fakes.set(`GET /api/runs/${LONG}/graph`, () => ({ status: 200, json: { graph: longGraph, workflow_id: null, version: null } }))
fakes.set('GET /api/runs', () => ({ status: 200, json: [longRun, ...listTop] }))
let longSocket = null
await page.routeWebSocket(new RegExp(`/api/runs/${LONG}/stream`), (ws) => { longSocket = ws })
await page.goto(`${WEB}/runs/${LONG}`, { waitUntil: 'domcontentloaded' })
await page.locator('[data-run-detail]').waitFor()
await page.waitForTimeout(800)
check('接上了实时流', longSocket != null)
const HMS = /\d+:\d\d:\d\d\.\d/
const longWall = await page.locator('[data-telemetry=wall]').innerText()
check('墙钟写跨度（8 天），不是秒表', /8 天/.test(longWall) && !HMS.test(longWall), longWall)
const activeA = await page.locator('[data-telemetry=active]').innerText()
await page.waitForTimeout(500)
const activeB = await page.locator('[data-telemetry=active]').innerText()
check('执行仍是走动的秒表，从批掉那一刻数起', /^00:0\d\.\d$/.test(activeB) && activeA !== activeB, `${activeA} → ${activeB}`)
const turnClock = await page.locator('[data-run-detail] > div span[title="已运行"]').first().innerText().catch(() => '')
check('轮次头的秒表只数执行的部分', /^00:0\d\.\d$/.test(turnClock), turnClock)
const rowClock = await page.locator(`[data-runs-list] [data-run-id="${LONG}"] [data-run-duration]`).innerText().catch(() => '')
check('列表那一行写「8 天」，不是秒表', /8 天/.test(rowClock) && !HMS.test(rowClock), rowClock)
fakes.delete('GET /api/runs')
fakes.delete(`GET /api/runs/${LONG}`)
fakes.delete(`GET /api/runs/${LONG}/events`)
fakes.delete(`GET /api/runs/${LONG}/graph`)

// ------------------------------------------------------------------ 删除保护

console.log('\n=== 删除保护 ===')
const FORMAL = 'fake0formal00000000000000000000'
const formalRun = { ...base, id: FORMAL, run_class: 'formal', version: 3, version_hash: 'f'.repeat(64) }
fakes.set(`GET /api/runs/${FORMAL}`, () => ({ status: 200, json: formalRun }))
fakes.set(`GET /api/runs/${FORMAL}/events`, () => ({ status: 200, json: liveEvents.slice(0, 3) }))
let deleteCalls = []
fakes.set(`DELETE /api/runs/${FORMAL}`, (url) => {
  deleteCalls.push(url.search)
  return url.searchParams.get('force') === 'true'
    ? { status: 204, body: '' }
    : { status: 409, json: { detail: '这是一次已封存的正式运行，是出具结果的追溯凭证。删除后它的事件和工件会一并删掉，封存清单再也无法核对。确认要删的话请再确认一次（强制删除）' } }
})
await page.goto(`${WEB}/runs/${FORMAL}`, { waitUntil: 'networkidle' })
await page.locator('[data-run-detail]').waitFor()
check('头部没有常驻的删除按钮', await page.locator('[data-run-detail] header button[title*="删除"]').count() === 0)
await page.getByRole('button', { name: '更多操作' }).click()
await page.locator('[data-menu=delete]').click()
const dialog = page.getByRole('dialog')
await dialog.waitFor()
check('确认框写清后果（清单无法再核对）', (await dialog.innerText()).includes('封存清单再也无法核对'))
await dialog.getByRole('button', { name: '删除记录' }).click()
await page.waitForTimeout(400)
const second = page.getByRole('dialog')
await second.waitFor()
check('后端 409 的 detail 显示在二次确认里', (await second.innerText()).includes('已封存的正式运行'))
const forceBtn = second.getByRole('button', { name: '强制删除' })
check('强制删除要照抄 id 才能点', await forceBtn.isDisabled())
await second.locator('input').fill(FORMAL.slice(0, 6))
await forceBtn.click()
await page.waitForURL((u) => u.pathname === '/runs', { timeout: 5000 }).catch(() => {})
check('先不带 force、确认后再带 force=true', deleteCalls.length === 2 && deleteCalls[0] === '' && deleteCalls[1] === '?force=true',
  JSON.stringify(deleteCalls))
check('删掉后回到列表', new URL(page.url()).pathname === '/runs')

// 普通 409（比如后端说还在执行）：原话提示，不静默
const BUSY = 'fake0busy0000000000000000000000'
fakes.set(`GET /api/runs/${BUSY}`, () => ({ status: 200, json: { ...base, id: BUSY } }))
fakes.set(`GET /api/runs/${BUSY}/events`, () => ({ status: 200, json: liveEvents.slice(0, 3) }))
fakes.set(`DELETE /api/runs/${BUSY}`, () => ({ status: 409, json: { detail: '这次运行还在执行，现在删除会留下没人管的任务。先停止它，再删除' } }))
await page.goto(`${WEB}/runs/${BUSY}`, { waitUntil: 'networkidle' })
await page.locator('[data-run-detail]').waitFor()
await page.getByRole('button', { name: '更多操作' }).click()
await page.locator('[data-menu=delete]').click()
await page.getByRole('dialog').getByRole('button', { name: '删除记录' }).click()
check('409 时把后端的话原样告诉人', await page.getByText('先停止它，再删除').first().waitFor({ timeout: 4000 }).then(() => true, () => false))
check('409 时停在原地', new URL(page.url()).pathname === `/runs/${BUSY}`)

console.log('\n=== 收尾 ===')
check('只有伪造过的写请求', writes.every((w) => /\/continue$|\/cancel$|\/copilot\/from-run$|DELETE \/api\/runs\/fake0|^POST \/api\/runs$/.test(w)),
  writes.join(', '))
check('没有未捕获的运行时错误', errors.length === 0, errors[0] ?? '')

await browser.close()
console.log(failed ? `\n${failed} 项没过` : '\n全部通过')
process.exit(failed ? 1 : 0)
