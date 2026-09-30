// 记录页（/runs）的检查：页签、状态筛选、分页、时间、待审批、删除保护、运行中接流、
// 失败的排错路径、封存凭证、提取模板。
//
// 守的是审查里抓到的几条死路：待审批的运行排在第 145 位、列表只取 100 条，点了
// 徽标也找不到；输入「失败」筛出来的是两条成功的运行；时间整体慢 8 小时；正在跑
// 的运行一动不动、却能删；封存过的正式运行一键就删掉了。
//
// 不写库：探针拦下所有非 GET 的 /api 请求，按场景回假响应。库里没有的形态（正在
// 跑的、封存过的正式运行）用 page.route 伪造 GET，用 routeWebSocket 伪造实时流。
// 前端和后端要指向同一份数据（前端的代理连的就是这个后端）。
// 跑之前前后端都得起着（./scripts/dev.sh），默认连 5273 / 8000。对别的实例（比如一份
// 沙箱拷贝）跑时带上地址：
//   AGENTLAB_WEB=http://localhost:<前端端口> AGENTLAB_API=http://localhost:<后端端口>/api node scripts/check-runs.mjs
import { readFileSync } from 'node:fs'
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const API = process.env.AGENTLAB_API ?? 'http://localhost:8000/api'
// RUNS_SHOTS=<目录>：证据页签亮暗各截几张，供人眼复核
const SHOTS = process.env.RUNS_SHOTS ?? ''
const CHROME = process.env.CHROME_PATH
  ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

const getJson = async (path) => (await fetch(API + path)).json()

// 仓库是公开的，这份输出会被贴进报告：沙箱库是用户开发库的拷贝，里面的工作流名、
// 节点名一律换成占位符再打印。本脚本源码里自己写着的串（夹具）本来就公开，不换。
const ownSource = readFileSync(new URL(import.meta.url), 'utf8')
const secrets = []
for (const w of await getJson('/workflows').catch(() => [])) {
  secrets.push([w.name, '‹工作流名›'])
  for (const n of w.graph?.nodes ?? []) secrets.push([n.data?.label ?? n.label, '‹节点名›'])
}
for (const r of await getJson('/runs?limit=200').catch(() => [])) secrets.push([r.workflow_name, '‹工作流名›'])
const maskList = [...new Map(secrets
  .filter(([v]) => typeof v === 'string' && v.trim().length >= 2 && !ownSource.includes(v))
  .map(([v, tag]) => [v, tag])).entries()]
  .sort((a, b) => b[0].length - a[0].length)
const masked = (t) => maskList.reduce((s, [v, tag]) => s.split(v).join(tag), String(t))

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${masked(detail)}` : ''}`)
  if (!cond) failed++
}

/**
 * 一节一节地跑：某一节里元素找不到、等待超时，只记成这一节失败，接着跑下一节，
 * 不让一处卡住把后面的检查一起吞掉。页面是各节共用的：
 * 出错那一节停在哪，下一节就从哪接着
 */
// RUNS_ONLY=证据页签 只跑段名里含这些字的段（逗号分隔；改坏验证时省时间）。前面几段给后面留的数据
// （航迹那一节的时刻、工件）只在整本跑时有，单跑依赖它们的段会报「中途出错」；check-all 不传它
const ONLY = (process.env.RUNS_ONLY ?? '').split(',').filter(Boolean)
async function section(name, fn) {
  if (ONLY.length && !ONLY.some((k) => name.includes(k)) && name !== '收尾') return
  console.log(`\n=== ${name} ===`)
  try {
    await fn()
  } catch (e) {
    check(`${name} 中途出错`, false, String(e?.message ?? e).split('\n')[0])
  }
}

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

// 各节共用的真数据，只读，在所有节之前取好。放在某一节里取的话，那一节的界面一出错，
// 后面十几节都拿着 undefined 报「中途出错」，各自真正的状态就看不见了
const pending = await getJson('/approvals?status=pending&limit=500')
const allRuns = await getJson('/runs?limit=200')
const listTop = await getJson('/runs?limit=50')
const wfHost = (await getJson('/workflows'))[0]
const base = allRuns.find((r) => r.status === 'succeeded' && !r.workflow_id)

// ------------------------------------------------------------------ 页签

await section('页签与地址', async () => {
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
})

// ------------------------------------------------------------------ 待审批

await section('待审批页签', async () => {
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
  const deep = pending.filter((a) => allRuns.findIndex((r) => r.id === a.run_id) >= 100)
  check('排在最近 100 条之外的待审批也看得到（原来的死路）', deep.every((a) => shownApprovals.includes(a.id)),
    `${deep.length} 条在第 100 名之后`)
  check('每条都写着等了多久', await page.locator('[data-approvals-list] [data-approval-age]').count() === shownApprovals.length)
  if (pending.length) {
    const rowText = await page.locator(`[data-approval-id="${pending[0].id}"]`).innerText()
    const hasWf = rowText.includes(pending[0].workflow_name ?? '')
    const hasNode = rowText.includes(pending[0].node_label ?? pending[0].node_id)
    // 名字来自沙箱库，只报在不在
    check('每条写着工作流名和节点', hasWf && hasNode, `工作流名 ${hasWf ? '在' : '缺'} · 节点 ${hasNode ? '在' : '缺'}`)
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
    // 原来只在「还在跑」时接流，这种就停在审批卡上，连「批准」那一行都没有
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
    // 时间线里是沙箱真实运行的内容（节点名、SQL、预览），只报字数，不打原文
    check('别处发生的事件补进了时间线（「张工 已批准」）', fastStream.includes('张工 已批准'), `时间线 ${fastStream.length} 字`)
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
})

// ------------------------------------------------------------------ 筛选

await section('状态筛选', async () => {
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
})

// ------------------------------------------------------------------ 分页

await section('分页', async () => {
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
})

// ------------------------------------------------------------------ 时间

await section('时间不再慢 8 小时', async () => {
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
})

// ------------------------------------------------------------------ 失败的排错路径

await section('失败：指到节点、原因、下一步', async () => {
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
    check('有「继续运行」', await page.locator('[data-run-banner=failed] [data-action=continue]').count() === 1)
    check('有「去模型接入」', await page.locator('[data-run-banner=failed] [data-action=settings]').count() === 1)
    check('有「复制错误」', await page.locator('[data-run-banner=failed] [data-action=copy-error]').count() === 1)
    // 时间线里的报错照原样画横幅那份拆好的失败（3C REQ-14）：以前只交一个标题字符串，流里拿它
    // 再讲一遍，原因、怎么办丢了，「去模型接入」的直达入口也没有
    const bannerTitle = (await page.locator('[data-failed-title]').innerText().catch(() => '')).trim()
    // 横幅标题那一行下面紧跟的一行是原因（没有原因时是「下一步：…」，不算）
    const bannerReason = await page.evaluate(() => {
      const next = document.querySelector('[data-run-banner=failed] [data-failed-title]')?.parentElement?.nextElementSibling
      const text = next?.textContent?.trim() ?? ''
      return text.startsWith('下一步') ? '' : text
    })
    const turnError = page.locator('[data-view-pane=stream] [data-turn-error]')
    const turnErrorText = (await turnError.innerText().catch(() => '')).replace(/\s+/g, ' ')
    const sameTitle = turnErrorText.includes(bannerTitle)
    const sameReason = !bannerReason || turnErrorText.includes(bannerReason.replace(/\s+/g, ' '))
    const fixLink = await turnError.locator('[data-fix=settings]').count() === 1
    const onceTitle = (await turnError.locator('.font-medium').first().innerText().catch(() => '')).trim() === bannerTitle
    // 报错原文来自沙箱里的真实运行，只报各项对不对得上
    check('时间线里的报错：标题、原因和横幅同一句，出错的节点只说一遍，给「去模型接入」',
      sameTitle && sameReason && fixLink && onceTitle,
      `标题 ${sameTitle ? '同' : '异'} · 原因 ${sameReason ? '同' : '异'} · 直达入口 ${fixLink ? '有' : '无'} · 标题行 ${onceTitle ? '同' : '异'}`)
    check('失败的运行不给「提取模板」', await page.locator('[data-action=extract]').count() === 0)
    // 横幅里的定位是排错的主路，详情头上的回放是看全程：两处都得够得着画布。失败的节点
    // 在工作流现在的图里已经没了的话（之后改过结构），对准它会落空：横幅改给「在航迹中看」，
    // 详情头照样回放、只是不带 focus。两种情形下面的航迹夹具各测一遍，这里按库里的实情测
    const authWf = (await getJson('/workflows')).find((w) => w.id === authFail.workflow_id)
    const stillThere = !!authWf?.graph?.nodes?.some((n) => n.id === full.error_node_id)
    const canvasBase = `/studio/${authFail.workflow_id}?run=${authFail.id}`
    const headLink = page.locator('[data-run-detail] > header [data-action=open-canvas]')
    const headHref = await headLink.getAttribute('href').catch(() => null)
    const headText = await headLink.innerText().catch(() => '')
    if (stillThere) {
      const href = await page.locator('[data-run-banner=failed] [data-action=locate]').getAttribute('href').catch(() => null)
      check('「在画布中定位」带 run 和 focus', href === `${canvasBase}&focus=${full.error_node_id}`, href ?? '')
      check('失败的运行详情头上也有「在画布中回放」，对准失败的节点',
        headText.includes('在画布中回放') && headHref === href, headHref ?? '没有这个按钮')
    } else {
      check('失败的节点在工作流现在的图里没了：不给落空的「在画布中定位」，改给「在航迹中看」',
        await page.locator('[data-run-banner=failed] [data-action=locate]').count() === 0
          && await page.locator('[data-run-banner=failed] [data-action=locate-trace]').count() === 1)
      check('失败的运行详情头上照样「在画布中回放」，不带对不上的 focus',
        headText.includes('在画布中回放') && headHref === canvasBase, headHref ?? '没有这个按钮')
    }

    // 继续运行：POST 伪造成功，实时流在上面已经伪造好
    fakes.set(`POST /api/runs/${authFail.id}/continue`, () => ({
      status: 200, json: { ...full, status: 'running', error: null, finished_at: null },
    }))
    await page.locator('[data-run-banner=failed] [data-action=continue]').click()
    await page.locator('[data-run-feedback=continued]').waitFor({ timeout: 5000 }).catch(() => {})
    check('继续运行发的是 POST /continue', writes.includes(`POST /api/runs/${authFail.id}/continue`))
    check('继续运行之后留下回执', await page.locator('[data-run-feedback=continued]').count() === 1)
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
    check('缺输入的失败不给「继续运行」（原样继续运行还会失败）',
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

    // 补上之后发起被拒：绑定的工具不存在（422 run_tool_missing）。和画布上发起被拒同一套，
    // 报错里给直达入口（数据源工具去数据页），不是只有一句 toast
    await page.goto(`${WEB}/runs/${missingInput.id}`, { waitUntil: 'networkidle' })
    await page.locator('[data-run-banner=failed] [data-action=rerun]').waitFor()
    fakes.set('POST /api/runs', () => ({ status: 422, json: {
      detail: '绑定的工具不存在：「查数」（调用工具）绑定的 db_query__nope。请到「数据」页接入，或在节点中重新选择', code: 'run_tool_missing' } }))
    await page.locator('[data-run-banner=failed] [data-action=rerun]').click()
    const refused = page.getByRole('dialog')
    await refused.waitFor()
    await refused.locator('input, textarea').first().fill('检查用的主题')
    await refused.getByRole('button', { name: '重新运行' }).click()
    const toData = page.getByRole('button', { name: '前往「数据」页接入' })
    await toData.waitFor({ timeout: 5000 }).catch(() => {})
    check('补上重新运行被拒（工具不在本机）：报错里有「前往「数据」页接入」', await toData.count() === 1)
    const said = await page.getByText('绑定的工具「db_query__nope」不存在').first().innerText().catch(() => '')
    check('……点名那个工具，说清这次运行没有发起', said.includes('db_query__nope') && said.includes('本次运行未启动'), said.slice(0, 120))
    check('……人还在这条运行上', new URL(page.url()).pathname === `/runs/${missingInput.id}`, page.url())
    await toData.click().catch(() => {})
    await page.waitForURL((u) => u.pathname.startsWith('/data'), { timeout: 5000 }).catch(() => {})
    check('……点了去数据页', new URL(page.url()).pathname.startsWith('/data'), page.url())
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

  // 条件表达式写错了：这一页的继续运行不带图，跑的还是那条写错的条件，只会再失败
  // 一次。不给继续运行，回画布定位是主路
  const syntaxFail = failedRuns.find((r) => /表达式语法错误|invalid syntax/.test(r.error ?? '') && r.workflow_id)
  if (syntaxFail) {
    await page.goto(`${WEB}/runs/${syntaxFail.id}`, { waitUntil: 'networkidle' })
    await page.locator('[data-run-banner=failed]').waitFor()
    check('表达式写错的失败不给「继续运行」', await page.locator('[data-run-banner=failed] [data-action=continue]').count() === 0)
    const locate = page.locator('[data-run-banner=failed] [data-action=locate]')
    // 失败的节点在工作流现在的图里没了的话，定位落空，主路改成去航迹看（定位作主按钮的
    // 情形由后面「模型没有真正调用工具」的夹具测）
    const synNode = (await getJson(`/runs/${syntaxFail.id}`)).error_node_id
    const synWf = (await getJson('/workflows')).find((w) => w.id === syntaxFail.workflow_id)
    if (synWf?.graph?.nodes?.some((n) => n.id === synNode)) {
      check('「在画布中定位」成了主按钮', await locate.count() === 1 && /\bbtn-primary\b/.test(await locate.getAttribute('class') ?? ''))
    } else {
      check('失败的节点在工作流现在的图里没了：不给落空的定位，给「在航迹中看」',
        await locate.count() === 0 && await page.locator('[data-run-banner=failed] [data-action=locate-trace]').count() === 1)
    }
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
    check('挂起横幅带「继续运行」', await page.locator('[data-run-banner=held] [data-action=resume]').count() === 1)
    // 挂起的没人会再去接：不放弃的话它在记录里永远挂着「可继续运行」
    check('挂起横幅也能放弃这次运行', await page.locator('[data-run-banner=held] [data-action=abandon]').count() === 1)
  }
})

// ------------------------------------------------------------------ 封存凭证

await section('封存凭证常驻', async () => {
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
})

// ------------------------------------------------------------------ 时长

let timed
await section('三种时长', async () => {
  timed = allRuns.find((r) => r.usage?.wall_ms != null && r.usage?.wait_ms > 0)
  if (timed) {
    await page.goto(`${WEB}/runs/${timed.id}`, { waitUntil: 'networkidle' })
    await page.locator('[data-run-telemetry]').waitFor()
    const wall = await page.locator('[data-telemetry=wall]').innerText()
    const active = await page.locator('[data-telemetry=active]').innerText()
    const wait = await page.locator('[data-telemetry=wait]').innerText()
    check('总时长、执行时长、等待审批三项都有数', ![wall, active, wait].includes('—'), `${wall} / ${active} / ${wait}`)
    check('列表那一行的 title 分项写了三种时长',
      /总时长 .+ · 执行时长 .+ · 等待审批 .+/.test(await page.locator(`[data-run-id="${timed.id}"] [data-run-duration]`).getAttribute('title') ?? ''))
  }
})

// ------------------------------------------------------------------ 提取模板

await section('提取模板', async () => {
  check('已完成的探索运行有「提取模板」', await page.locator('[data-action=extract]').count() === 1)
  const wfs = await getJson('/workflows')
  fakes.set('POST /api/copilot/from-run', () => ({
    status: 200, json: { workflow_id: wfs[0].id, name: '检查用草稿', nodes: 3, edges: 2, dropped_nodes: 1, source_run: timed?.id },
  }))
  // 跳进画布后，画布先找这张图的助手对话，没有就建一条（POST，会被探针拦下、算成没伪造的写请求）。
  // 沙箱里 wfs[0] 有没有对话要看之前谁跑过什么：给一条现成的，和升级横幅那一节同一个做法
  fakes.set('GET /api/conversations', () => ({ status: 200, json: [{ id: 'fake-conv-extract', title: '', kind: 'canvas', archived: false, turn_count: 0 }] }))
  // 画布上还有没保存的改动（store 里的画布离开编排页也还在）：先问要不要放弃，再建草稿。
  // 以前建完才问，点了「取消」库里就多出一张没人要的草稿（3C REQ-15）
  const extractPosts = () => writes.filter((w) => w === 'POST /api/copilot/from-run').length
  const discardAsk = page.getByRole('dialog').filter({ hasText: '未保存的改动' })
  await page.evaluate(() => window.__studio.setState({ dirty: true,
    workflow: { ...(window.__studio.getState().workflow ?? {}), id: 'fake0dirtycanvas', name: '没存的那张' } }))
  const postsBefore = extractPosts()
  await page.locator('[data-action=extract]').click()
  const asked = await discardAsk.waitFor({ timeout: 3000 }).then(() => true, () => false)
  check('画布有没保存的改动：先问要不要放弃，还没建草稿', asked && extractPosts() === postsBefore,
    `${asked ? '问了' : '没问'} · 已发 ${extractPosts() - postsBefore} 次提取`)
  await discardAsk.getByRole('button', { name: '取消' }).click().catch(() => {})
  await page.waitForTimeout(400)
  check('说了取消：不建草稿，留在记录页', extractPosts() === postsBefore && new URL(page.url()).pathname.startsWith('/runs'))
  const countAsks = () => page.evaluate(() => {
    window.__asks = 0
    new MutationObserver(() => {
      const d = [...document.querySelectorAll('[role=dialog]')].filter((e) => e.textContent?.includes('未保存的改动'))
      if (d.length && !window.__askOpen) window.__asks += 1
      window.__askOpen = d.length > 0
    }).observe(document.body, { childList: true, subtree: true })
  })
  await countAsks()
  await page.locator('[data-action=extract]').click()
  await discardAsk.waitFor({ timeout: 3000 }).catch(() => {})
  await discardAsk.getByRole('button', { name: '放弃并切换' }).click().catch(() => {})
  await page.waitForURL(`**/studio/${wfs[0].id}`, { timeout: 5000 }).catch(() => {})
  await page.waitForTimeout(400)
  const asks = await page.evaluate(() => window.__asks ?? -1)
  check('说了放弃：建一次草稿、跳过去，整条路只问这一遍', extractPosts() === postsBefore + 1 && asks === 1
    && new URL(page.url()).pathname === `/studio/${wfs[0].id}`, `提取 ${extractPosts() - postsBefore} 次 · 问了 ${asks} 遍`)
  check('提取成功后跳到新草稿', new URL(page.url()).pathname === `/studio/${wfs[0].id}`)
  fakes.delete('POST /api/copilot/from-run')
  fakes.delete('GET /api/conversations')
})

// ------------------------------------------------------------------ 运行中：接流、停止、不能删

const FAKE = 'fake0running0000000000000000000'
let t0, liveRun, liveEvents
await section('运行中：实时接流、停止、不能删', async () => {
  t0 = Date.now() / 1000 - 3
  liveRun = {
    ...base, id: FAKE, status: 'running', output: {}, usage: {}, manifest_hash: null, manifest_seq: null,
    created_at: new Date(t0 * 1000).toISOString(), started_at: new Date(t0 * 1000).toISOString(), finished_at: null,
  }
  const graph = { nodes: [
    { id: 'a', type: 'input', position: { x: 0, y: 0 }, data: { label: '输入', config: {} } },
    { id: 'b', type: 'llm', position: { x: 200, y: 0 }, data: { label: '总结', config: {} } },
  ], edges: [{ source: 'a', target: 'b' }] }
  liveEvents = [
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
  check('总时长在走（mm:ss.s）', /^\d\d:\d\d\.\d$/.test(clockB) && clockA !== clockB, `${clockA} → ${clockB}`)
  check('状态格写着当前节点', (await page.locator('[data-run-telemetry]').innerText()).includes('当前「总结」'))
  // 在跑的运行看航迹：游标跟着现在走，「此刻」列出在跑的节点
  await page.locator('[data-run-detail] [role=tab][data-tab=trace]').click()
  await page.locator('[data-trace-readout]').waitFor({ timeout: 5000 }).catch(() => {})
  const liveAtA = await page.locator('[data-trace-readout] [data-readout=at]').innerText().catch(() => '')
  await page.waitForTimeout(500)
  const liveAtB = await page.locator('[data-trace-readout] [data-readout=at]').innerText().catch(() => '')
  const liveMode = await page.locator('[data-trace-readout]').getAttribute('data-mode').catch(() => '')
  check('航迹上游标是实时的，而且在走', liveMode === '实时' && liveAtA !== liveAtB, `${liveMode} ${liveAtA} → ${liveAtB}`.replace(/\s+/g, ' '))
  check('「此刻」列出在跑的「总结」', await page.locator('[data-trace-now] [data-now-node=b]').count() === 1)
  await page.locator('[data-run-detail] [role=tab][data-tab=stream]').click()
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
})

// ------------------------------------------------------------------ 等了很久之后接着跑

await section('等了几天的审批批掉之后：计时读得懂', async () => {
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
  check('总时长写跨度（8 天），不是秒表', /8 天/.test(longWall) && !HMS.test(longWall), longWall)
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
})

// ------------------------------------------------------------------ 删除保护

await section('删除保护', async () => {
  const FORMAL = 'fake0formal00000000000000000000'
  const formalRun = { ...base, id: FORMAL, run_class: 'formal', version: 3, version_hash: 'f'.repeat(64) }
  fakes.set(`GET /api/runs/${FORMAL}`, () => ({ status: 200, json: formalRun }))
  fakes.set(`GET /api/runs/${FORMAL}/events`, () => ({ status: 200, json: liveEvents.slice(0, 3) }))
  let deleteCalls = []
  fakes.set(`DELETE /api/runs/${FORMAL}`, (url) => {
    deleteCalls.push(url.search)
    return url.searchParams.get('force') === 'true'
      ? { status: 204, body: '' }
      : { status: 409, json: { detail: '这是一次已封存的正式运行，是出具结果的追溯凭证。删除后其事件和工件将一并删除，封存清单无法再核对。如仍要删除，请选择「强制删除」' } }
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

  // 普通 409（比如后端说还在进行）：原话提示，不静默
  const BUSY = 'fake0busy0000000000000000000000'
  fakes.set(`GET /api/runs/${BUSY}`, () => ({ status: 200, json: { ...base, id: BUSY } }))
  fakes.set(`GET /api/runs/${BUSY}/events`, () => ({ status: 200, json: liveEvents.slice(0, 3) }))
  fakes.set(`DELETE /api/runs/${BUSY}`, () => ({ status: 409, json: { detail: '这次运行仍在进行，现在删除会留下无人管理的后台任务。请先停止运行，再删除' } }))
  await page.goto(`${WEB}/runs/${BUSY}`, { waitUntil: 'networkidle' })
  await page.locator('[data-run-detail]').waitFor()
  await page.getByRole('button', { name: '更多操作' }).click()
  await page.locator('[data-menu=delete]').click()
  await page.getByRole('dialog').getByRole('button', { name: '删除记录' }).click()
  check('409 时把后端的话原样告诉人', await page.getByText('请先停止运行，再删除').first().waitFor({ timeout: 4000 }).then(() => true, () => false))
  check('409 时停在原地', new URL(page.url()).pathname === `/runs/${BUSY}`)
})

// ------------------------------------------------------------------ 航迹

const iso = (s) => new Date(s * 1000).toISOString()
const TRACE = 'fake0trace00000000000000000000'
const WAIT_S = 300
const traceGraph = { nodes: [
  { id: 'in', type: 'input', position: { x: 0, y: 0 }, data: { label: '输入', config: {} } },
  { id: 'query', type: 'agent', position: { x: 200, y: 0 }, data: { label: '查询订单', config: { tools: ['db_query__shop'] } } },
  { id: 'gate', type: 'human', position: { x: 400, y: 0 }, data: { label: '人工审批', config: {} } },
  { id: 'sum', type: 'llm', position: { x: 600, y: 0 }, data: { label: '汇总', config: {} } },
], edges: [{ source: 'in', target: 'query' }, { source: 'query', target: 'gate' }, { source: 'gate', target: 'sum' }] }
const traceEvents = (usageIn = 2000) => [
  { seq: 1, type: 'run.started', node_id: null, ts: tt, data: { nodes: 4 } },
  { seq: 2, type: 'node.started', node_id: 'in', ts: tt + 0.1, data: { node_type: 'input', label: '输入' } },
  { seq: 3, type: 'node.finished', node_id: 'in', ts: tt + 0.2, data: { duration_ms: 100 } },
  { seq: 4, type: 'node.started', node_id: 'query', ts: tt + 0.3, data: { node_type: 'agent', label: '查询订单' } },
  { seq: 5, type: 'tool.start', node_id: 'query', ts: tt + 0.5,
    data: { tool: 'db_query__shop', call_id: 'c1', args: { sql: 'select count(*) as n from orders' } } },
  { seq: 6, type: 'tool.end', node_id: 'query', ts: tt + 2.5,
    data: { tool: 'db_query__shop', call_id: 'c1', duration_ms: 2000, preview: '{"columns":["n"],"rows":[[42]]}' } },
  { seq: 7, type: 'llm.end', node_id: 'query', ts: tt + 3.0,
    data: { agent: '查询订单', model: 'demo-model', input_tokens: 1200, output_tokens: 300, cost_usd: 0.004 } },
  { seq: 8, type: 'node.finished', node_id: 'query', ts: tt + 3.1, data: { duration_ms: 2800, preview: '共 42 单' } },
  { seq: 9, type: 'node.started', node_id: 'gate', ts: tt + 3.2, data: { node_type: 'human', label: '人工审批' } },
  { seq: 10, type: 'human.requested', node_id: 'gate', ts: tt + 3.3, data: { mode: 'approve', prompt: '放行吗？' } },
  { seq: 11, type: 'run.interrupted', node_id: null, ts: tt + 3.4, data: {} },
  { seq: 12, type: 'human.resolved', node_id: 'gate', ts: tt + 3.4 + WAIT_S, data: { response: { approved: true }, actor: '张工' } },
  { seq: 13, type: 'run.resumed', node_id: null, ts: tt + 3.4 + WAIT_S, data: { actor: '张工' } },
  { seq: 14, type: 'node.finished', node_id: 'gate', ts: tt + 3.5 + WAIT_S, data: { duration_ms: 1 } },
  { seq: 15, type: 'node.started', node_id: 'sum', ts: tt + 3.6 + WAIT_S, data: { node_type: 'llm', label: '汇总' } },
  { seq: 16, type: 'llm.end', node_id: 'sum', ts: tt + 6.0 + WAIT_S,
    data: { model: 'demo-model', input_tokens: 800, output_tokens: 500, cost_usd: 0.003 } },
  { seq: 17, type: 'node.finished', node_id: 'sum', ts: tt + 6.1 + WAIT_S, data: { duration_ms: 2500 } },
  { seq: 18, type: 'run.finished', node_id: null, ts: tt + 6.2 + WAIT_S, data: {
    output: { answer: '共 42 单' }, usage: { input_tokens: usageIn, output_tokens: 800, cost_usd: 0.007 },
    timing: { wall_ms: (6.2 + WAIT_S) * 1000, active_ms: 6200, wait_ms: WAIT_S * 1000 } } },
]
const traceRun = (id, usageIn = 2000) => ({
  ...base, id, workflow_id: wfHost.id, workflow_name: wfHost.name, status: 'succeeded', run_class: 'exploratory',
  version: null, version_hash: null, manifest_hash: null, manifest_seq: null, error: null, error_node_id: null,
  input: { q: '上周的订单' }, output: { answer: '共 42 单' },
  usage: { input_tokens: usageIn, output_tokens: 800, cost_usd: 0.007, duration_ms: 6200,
           wall_ms: (6.2 + WAIT_S) * 1000, active_ms: 6200, wait_ms: WAIT_S * 1000 },
  created_at: iso(tt), started_at: iso(tt), finished_at: iso(tt + 6.2 + WAIT_S),
})
const viewTab = (k) => page.locator(`[data-run-detail] [role=tab][data-tab=${k}]`)
const readout = (k) => page.locator(`[data-trace-readout] [data-readout=${k}]`).innerText().catch(() => '')
// 实时、回放、终态写在游标那一格的标签行，数值那一行只有时刻
const mode = () => page.locator('[data-trace-readout]').getAttribute('data-mode').catch(() => '')
let tt, ART_Q
await section('航迹：按时间摊开，拖到哪一刻读到哪一刻', async () => {
  // 一次等过 5 分钟审批的运行：查询 → 审批 → 汇总。名字都是编的通用示例
  const TRACE_OFF = 'fake0traceoff00000000000000000'
  tt = Date.now() / 1000 - 3600
  ART_Q = 'a1'.repeat(32)
  const ART_QOUT = 'b2'.repeat(32)
  const ART_SUM = 'c3'.repeat(32)
  const traceArtifacts = [
    { id: ART_Q, kind: 'query_snapshot', node_id: 'query', size: 2048, meta: {}, created_at: iso(tt + 2.5) },
    { id: ART_QOUT, kind: 'node_output', node_id: 'query', size: 120, meta: { type: 'agent', attempt: 1 }, created_at: iso(tt + 3.1) },
    { id: ART_SUM, kind: 'node_output', node_id: 'sum', size: 300, meta: { type: 'llm', attempt: 1 }, created_at: iso(tt + 6.1 + WAIT_S) },
  ]
  for (const [id, usageIn] of [[TRACE, 2000], [TRACE_OFF, 5000]]) {
    fakes.set(`GET /api/runs/${id}`, () => ({ status: 200, json: traceRun(id, usageIn) }))
    fakes.set(`GET /api/runs/${id}/events`, () => ({ status: 200, json: traceEvents(usageIn) }))
    fakes.set(`GET /api/runs/${id}/graph`, () => ({ status: 200, json: { graph: traceGraph, workflow_id: wfHost.id, version: null } }))
    fakes.set(`GET /api/runs/${id}/artifacts`, () => ({ status: 200, json: traceArtifacts }))
  }
  fakes.set(`GET /api/artifacts/${ART_Q}`, () => ({ status: 200, json: { id: ART_Q, content: {
    tool: 'db_query__shop', args: { sql: 'select count(*) as n from orders' }, result: '{"columns":["n"],"rows":[[42]]}',
  } } }))
  // 工作流「现在」的图：这些运行之后改过结构——「汇总」删了，换成「通知」；「查询订单」还在。
  // 画布上还对得准的节点给「在画布中看这一步」，已经没了的不给（给了也对不到任何东西）
  const wfNow = (await getJson('/workflows')).map((w) => (w.id !== wfHost.id ? w : { ...w, graph: {
    nodes: [...traceGraph.nodes.filter((n) => n.id !== 'sum'),
      { id: 'notify', type: 'output', position: { x: 600, y: 0 }, data: { label: '通知', config: {} } }],
    edges: [...traceGraph.edges.filter((e) => e.target !== 'sum'), { source: 'gate', target: 'notify' }],
  } }))
  fakes.set('GET /api/workflows', () => ({ status: 200, json: wfNow }))

  await page.goto(`${WEB}/runs/${TRACE}`, { waitUntil: 'networkidle' })
  await page.locator('[data-run-detail]').waitFor()
  check('详情分「时间线 / 航迹 / 工件」三个页签',
    await viewTab('stream').count() === 1 && await viewTab('trace').count() === 1 && await viewTab('artifacts').count() === 1)
  check('默认停在时间线', await viewTab('stream').getAttribute('aria-selected').catch(() => null) === 'true')
  check('工件页签上写着有几件', (await viewTab('artifacts').innerText().catch(() => '')).includes('3'))
  check('读屏念的是「3 件」，不是「3 条」',
    await viewTab('artifacts').locator('[aria-label]').getAttribute('aria-label').catch(() => null) === '3 件',
    await viewTab('artifacts').locator('[aria-label]').getAttribute('aria-label').catch(() => '') ?? '')
  await viewTab('trace').click().catch(() => {})
  await page.locator('[data-run-trace] section[aria-label="航迹"]').waitFor({ timeout: 5000 }).catch(() => {})
  check('点「航迹」地址记下 ?view=trace', new URL(page.url()).searchParams.get('view') === 'trace')
  const lanes = await page.locator('[data-run-trace] [data-lane]').evaluateAll((els) => els.map((e) => e.getAttribute('data-lane')))
  check('每个节点一条泳道，按执行顺序排', lanes.join(',') === 'in,query,gate,sum', lanes.join(',') || '没有泳道')
  check('没拖游标时读的是终态：已完成、2.8k token',
    (await readout('phase')).includes('已完成') && (await readout('tokens')).includes('2.8k'),
    `${await readout('phase')} / ${await readout('tokens')}`)
  check('没选节点时列出最耗时的节点（慢在哪）',
    (await page.locator('[data-trace-slowest] [data-slow-node]').first().getAttribute('data-slow-node').catch(() => null)) === 'query')

  // 地址里带着时刻：从工件、分享的链接直接落到那一刻
  await page.goto(`${WEB}/runs/${TRACE}?view=trace&at=2500`, { waitUntil: 'networkidle' })
  await page.locator('[data-trace-readout]').waitFor({ timeout: 5000 }).catch(() => {})
  check('?at=2500 落到 T+00:02.5', (await readout('at')).includes('00:02.5'), await readout('at'))
  check('游标在时间轴上也在回放', (await page.locator('[data-run-trace] [role=slider]').getAttribute('aria-valuenow').catch(() => null)) === '2500')
  check('那一刻在跑的是「查询订单」', await page.locator('[data-trace-now] [data-now-node=query]').count() === 1)
  check('那一刻整次运行还没用 token（模型 3.0 s 才回）', /^0\b/.test((await readout('tokens')).trim()), await readout('tokens'))
  check('进来之后地址里的时刻摘掉（拖动时不再和它对不上）', !new URL(page.url()).searchParams.has('at'))

  // 慢在哪：那一刻卡在哪个工具上（查询 0.5 s 开始、2.5 s 返回）
  await page.goto(`${WEB}/runs/${TRACE}?view=trace&at=2000`, { waitUntil: 'networkidle' })
  await page.locator('[data-trace-readout]').waitFor({ timeout: 5000 }).catch(() => {})
  await page.locator('[data-run-trace] [data-lane=query] .tl-label').click().catch(() => {})
  const callText = await page.locator('[data-trace-node=query] [data-node-field=call]').innerText().catch(() => '')
  check('那一刻在跑的节点，卡片写着正在调哪个工具、调了多久', /db_query__shop.*已 1\.5 s/.test(callText), callText || '没有这一行')
  check('点泳道名只是选中节点，游标不动', (await readout('at')).includes('00:02.0'), await readout('at'))

  // 拖动：在坞的刻度行上按下、拖过去，读数跟着游标走
  const track = page.locator('[data-run-trace] .tl-scrub').first()
  const box = await track.boundingBox().catch(() => null)
  if (box) {
    await page.mouse.move(box.x + box.width * 0.05, box.y + box.height / 2)
    await page.mouse.down()
    await page.mouse.move(box.x + box.width * 0.15, box.y + box.height / 2, { steps: 4 })
    await page.waitForTimeout(120)
    const mid = `${await mode()} ${await readout('at')}`
    await page.mouse.move(box.x + box.width * 0.3, box.y + box.height / 2, { steps: 4 })
    await page.mouse.up()
    await page.waitForTimeout(150)
    const after = `${await mode()} ${await readout('at')}`
    check('拖动游标：读数跟着走，并标成回放', mid.includes('回放') && after.includes('回放') && mid !== after, `${mid} → ${after}`)
  } else {
    check('拖动游标：读数跟着走，并标成回放', false, '找不到坞的刻度行')
  }
  const slider = page.locator('[data-run-trace] [role=slider]')
  await slider.focus().catch(() => {})
  await page.keyboard.press('Home')
  await page.waitForTimeout(100)
  check('键盘 Home：游标回到开始', (await readout('at')).includes('00:00.0'), await readout('at'))
  await page.keyboard.press('End')
  await page.waitForTimeout(100)
  check('键盘 End：回到终态读数', await mode() === '终态', await mode())

  // 关键时刻：长时间等人的空档被压缩了，拖游标很难正好停在那一下
  const moments = page.locator('[data-trace-moments] [data-moment]')
  const momentText = (await moments.allInnerTexts().catch(() => [])).map((t) => t.replace(/\s+/g, ' '))
  check('列出关键时刻：开始、等待审批、审批已处理（等了多久）、结局',
    momentText.length === 4 && momentText[0].includes('开始运行') && momentText[1].includes('「人工审批」等待审批')
      && momentText[2].includes('审批已处理') && momentText[2].includes('5 分 00 秒') && momentText[3].includes('运行完成'),
    momentText.join(' | '))
  check('没拖游标时，当前落在结局那一条', await moments.nth(3).getAttribute('aria-current').catch(() => null) === 'step')
  await moments.nth(2).click().catch(() => {})
  await page.waitForTimeout(150)
  check('点「审批已处理」：游标跳到那一刻（T+05:03.4）', await mode() === '回放' && (await readout('at')).includes('05:03.4'),
    `${await mode()} ${await readout('at')}`)
  check('跳过去之后它成了当前那一条', await moments.nth(2).getAttribute('aria-current').catch(() => null) === 'step')
  check('坞上的游标也跟着过去', (await slider.getAttribute('aria-valuenow').catch(() => null)) === '303400',
    await slider.getAttribute('aria-valuenow').catch(() => '') ?? '')
  await moments.nth(3).click().catch(() => {})
  await page.waitForTimeout(150)
  check('点结局那一条回到终态', await mode() === '终态', await mode())

  await page.goto(`${WEB}/runs/${TRACE}?view=trace&at=30000`, { waitUntil: 'networkidle' })
  await page.locator('[data-trace-readout]').waitFor({ timeout: 5000 }).catch(() => {})
  check('拖到等审批那一段：状态是等待审批', (await readout('phase')).includes('等待审批'), await readout('phase'))
  check('那一刻等审批的是「人工审批」', await page.locator('[data-trace-now] [data-now-node=gate]').count() === 1)
  check('用量只算到那一刻：1.5k', (await readout('tokens')).includes('1.5k'), await readout('tokens'))
  await page.locator('[data-run-trace] [data-lane=query]').click().catch(() => {})
  const nodeCard = page.locator('[data-trace-node=query]')
  check('点泳道看这个节点在那一刻的样子', await nodeCard.count() === 1)
  check('节点的用量、工具数取自那一刻',
    (await nodeCard.locator('[data-node-field=tokens]').innerText().catch(() => '')).includes('1.5k')
      && (await nodeCard.locator('[data-node-field=tools]').innerText().catch(() => '')).includes('1'))
  check('节点卡能回画布看这一步',
    (await nodeCard.locator('[data-action=node-canvas]').getAttribute('href').catch(() => null)) === `/studio/${wfHost.id}?run=${TRACE}&focus=query`)
  await page.locator('[data-run-trace] [data-lane=sum] .tl-label').click().catch(() => {})
  const goneCard = page.locator('[data-trace-node=sum]')
  check('工作流现在的图里已经没有的节点：节点卡不给落空的「在画布中看这一步」，说明为什么',
    await goneCard.count() === 1 && await goneCard.locator('[data-action=node-canvas]').count() === 0
      && await goneCard.locator('[data-node-gone]').count() === 1)
  await page.locator('[data-run-trace] [data-lane=query] .tl-label').click().catch(() => {})
  await nodeCard.locator('[data-action=node-stream]').click().catch(() => {})
  await page.waitForTimeout(300)
  check('「在时间线里看」切回时间线', await viewTab('stream').getAttribute('aria-selected').catch(() => null) === 'true')
  check('并且把那一步描出来', await page.locator('[data-run-detail] [data-node-id="query"][data-flash]').count() >= 1)

  await page.goto(`${WEB}/runs/${TRACE_OFF}?view=trace&at=30000`, { waitUntil: 'networkidle' })
  await page.locator('[data-trace-readout]').waitFor({ timeout: 5000 }).catch(() => {})
  check('各次调用加起来对不上后端总数时，那一刻的用量写「—」，不拿终值冒充', (await readout('tokens')).trim().startsWith('—'), await readout('tokens'))
})

// 模型把工具调用写成了文字、一次都没真正查（引擎判失败）：结局是失败，而且说得出为什么
await section('航迹：模型没有真正调用工具', async () => {
  const LEAK = 'fake0leak000000000000000000000'
  // run.error 和下面那条日志是老后端的原话（库里老运行存的就是这样），留作「前端认得旧原文」的样本；
  // 后端现在的原文（toolcalls.TOOL_MARKUP_ERROR）在 check-ui-kit 的 explain 单元核对里
  const LEAK_ERROR = '模型输出了工具调用的原始标记，但没有真正调用工具，这一步一次都没查到数据。常见原因：节点没有绑定工具，或者模型、服务不支持工具调用。到画布里给这个节点绑定要用的工具；绑定了还这样，就换一个支持工具调用的模型'
  fakes.set(`GET /api/runs/${LEAK}`, () => ({ status: 200, json: {
    ...traceRun(LEAK), status: 'failed', output: null, error: LEAK_ERROR, error_node_id: 'query',
    usage: { input_tokens: 900, output_tokens: 400, cost_usd: 0.002, duration_ms: 4200, wall_ms: 4200, active_ms: 4200, wait_ms: 0 },
    finished_at: iso(tt + 4.2),
  } }))
  fakes.set(`GET /api/runs/${LEAK}/events`, () => ({ status: 200, json: [
    ...traceEvents().slice(0, 4),
    { seq: 5, type: 'llm.end', node_id: 'query', ts: tt + 2.0,
      data: { agent: '查询订单', model: 'demo-model', input_tokens: 450, output_tokens: 200, cost_usd: 0.001 } },
    { seq: 6, type: 'log', node_id: 'query', ts: tt + 2.1, data: {
      level: 'warn', code: 'tool_markup_leak', message: '模型把工具调用写成了文字（<｜｜DSML｜｜invoke name="db_query__shop"…），没有真正调用工具' } },
    { seq: 7, type: 'llm.end', node_id: 'query', ts: tt + 4.0,
      data: { agent: '查询订单', model: 'demo-model', input_tokens: 450, output_tokens: 200, cost_usd: 0.001 } },
    { seq: 8, type: 'node.failed', node_id: 'query', ts: tt + 4.1, data: { error: LEAK_ERROR, duration_ms: 3800 } },
    { seq: 9, type: 'run.failed', node_id: null, ts: tt + 4.2, data: {
      error: LEAK_ERROR, node_id: 'query', timing: { wall_ms: 4200, active_ms: 4200, wait_ms: 0 } } },
  ] }))
  fakes.set(`GET /api/runs/${LEAK}/graph`, () => ({ status: 200, json: { graph: traceGraph, workflow_id: wfHost.id, version: null } }))
  fakes.set(`GET /api/runs/${LEAK}/artifacts`, () => ({ status: 200, json: [] }))
  await page.goto(`${WEB}/runs/${LEAK}?view=trace`, { waitUntil: 'networkidle' })
  await page.locator('[data-trace-readout]').waitFor({ timeout: 5000 }).catch(() => {})
  const leakBanner = (await page.locator('[data-run-banner=failed]').innerText().catch(() => '')).replace(/\s+/g, ' ')
  check('失败横幅说清是模型没有真正调用工具', leakBanner.includes('模型未实际调用工具'), leakBanner.slice(0, 60))
  const leakLocate = page.locator('[data-run-banner=failed] [data-action=locate]')
  check('失败的节点还在工作流现在的图里：「在画布中定位」对准它；继续运行过不去，它就是主按钮',
    await leakLocate.getAttribute('href').catch(() => null) === `/studio/${wfHost.id}?run=${LEAK}&focus=query`
      && /\bbtn-primary\b/.test(await leakLocate.getAttribute('class').catch(() => '') ?? '')
      && await page.locator('[data-run-banner=failed] [data-action=locate-trace]').count() === 0)
  const leakMoments = (await page.locator('[data-trace-moments] [data-moment]').allInnerTexts().catch(() => [])).map((t) => t.replace(/\s+/g, ' '))
  check('关键时刻里有那一下警告，结局写失败于哪个节点',
    leakMoments.some((t) => t.includes('「查询订单」以文本形式输出了工具调用'))
      && (leakMoments.at(-1) ?? '').includes('失败于「查询订单」') && (leakMoments.at(-1) ?? '').includes('模型未实际调用工具'),
    leakMoments.join(' | '))
  const leakCard = (await page.locator('[data-trace-node=query]').innerText().catch(() => '')).replace(/\s+/g, ' ')
  check('失败的节点默认选中，卡片上写着警告和原因',
    leakCard.includes('以文本形式输出了工具调用') && leakCard.includes('模型未实际调用工具') && leakCard.includes('失败'),
    leakCard.slice(0, 80))
  await page.locator('[data-trace-moments] [data-moment="query:markup"]').click().catch(() => {})
  await page.waitForTimeout(150)
  check('跳到警告那一刻：节点还在跑，卡片上已经有警告',
    (await readout('at')).includes('00:02.1') && (await page.locator('[data-trace-node=query] [data-node-warn]').count()) === 1,
    await readout('at'))
})

// 工具库里的参数定义写坏了：要改的东西不在运行快照里，先去改、再接着跑。以前「接着跑」是主按钮，
// 人先点它，同样的错再来一遍；「去工具库」只到列表，还得自己找是哪一个（3C REQ-1 / REQ-5）
await section('失败：自定义工具的参数定义写坏了，先去改', async () => {
  const BROKEN = 'fake0brokentool000000000000000'
  // 老后端的原话（库里老运行的 run.error 存的是它），留作「前端认得旧原文」的样本；这一节末尾再用后端现在的原文走一遍
  const BROKEN_ERROR = '自定义工具「lookup_order」的参数定义格式不对：参数 store 要写成 {"type": "string"} 这样的对象，不能直接写 "string"。到「工具」页把它的参数定义改好再运行'
    + '；自定义工具「fetch_rate」的参数定义格式不对：类型「int」认不出来，是不是想写 integer；只能是 string、number、integer、boolean、array、object。到「工具」页把它的参数定义改好再运行'
  fakes.set(`GET /api/runs/${BROKEN}`, () => ({ status: 200, json: {
    ...traceRun(BROKEN), status: 'failed', output: null, error: BROKEN_ERROR, error_node_id: 'query',
    usage: { input_tokens: 0, output_tokens: 0, cost_usd: 0, duration_ms: 400, wall_ms: 400, active_ms: 400, wait_ms: 0 },
    finished_at: iso(tt + 0.4),
  } }))
  fakes.set(`GET /api/runs/${BROKEN}/events`, () => ({ status: 200, json: [
    ...traceEvents().slice(0, 4),
    { seq: 5, type: 'node.failed', node_id: 'query', ts: tt + 0.35, data: { error: BROKEN_ERROR, duration_ms: 50 } },
    { seq: 6, type: 'run.failed', node_id: null, ts: tt + 0.4, data: {
      error: BROKEN_ERROR, node_id: 'query', timing: { wall_ms: 400, active_ms: 400, wait_ms: 0 } } },
  ] }))
  fakes.set(`GET /api/runs/${BROKEN}/graph`, () => ({ status: 200, json: { graph: traceGraph, workflow_id: wfHost.id, version: null } }))
  fakes.set(`GET /api/runs/${BROKEN}/artifacts`, () => ({ status: 200, json: [] }))
  await page.goto(`${WEB}/runs/${BROKEN}?view=stream`, { waitUntil: 'networkidle' })
  await page.locator('[data-run-banner=failed]').waitFor({ timeout: 5000 }).catch(() => {})
  const brokenFix = page.locator('[data-run-banner=failed] [data-action=tools]')
  const brokenGo = page.locator('[data-run-banner=failed] [data-action=continue]')
  check('标题说有几个工具坏了，不只认第一个',
    (await page.locator('[data-failed-title]').innerText().catch(() => '')).trim() === '2 个自定义工具的参数定义有误')
  check('主按钮是「去改参数定义」，直达第一个坏工具的编辑框',
    (await brokenFix.innerText().catch(() => '')).includes('去改参数定义')
      && /\bbtn-primary\b/.test(await brokenFix.getAttribute('class').catch(() => '') ?? '')
      && await brokenFix.getAttribute('href').catch(() => null) === '/tools/custom?edit=lookup_order',
    await brokenFix.getAttribute('href').catch(() => '没有这个按钮') ?? '')
  check('「继续运行」还在（改好之后继续运行能过），但退成次要、排在后面',
    await brokenGo.count() === 1 && !/\bbtn-primary\b/.test(await brokenGo.getAttribute('class').catch(() => '') ?? '')
      && await page.evaluate(() => {
        const fix = document.querySelector('[data-run-banner=failed] [data-action=tools]')
        const go = document.querySelector('[data-run-banner=failed] [data-action=continue]')
        return !!fix && !!go && !!(fix.compareDocumentPosition(go) & Node.DOCUMENT_POSITION_FOLLOWING)
      }))
  const streamFix = page.locator('[data-view-pane=stream] [data-turn-error] [data-fix=tools]')
  check('时间线里的报错也直达那个工具，写的是「去改参数定义」',
    await streamFix.getAttribute('href').catch(() => null) === '/tools/custom?edit=lookup_order'
      && (await streamFix.innerText().catch(() => '')).includes('去改参数定义'),
    await streamFix.getAttribute('href').catch(() => '没有这个入口') ?? '')
  await brokenFix.click().catch(() => {})
  await page.waitForURL((u) => u.pathname === '/tools/custom', { timeout: 5000 }).catch(() => {})
  check('点了打开工具页的自定义工具', new URL(page.url()).pathname === '/tools/custom')

  // 后端现在的原文（tools/custom.py：前缀「格式有误」、结尾「到「工具」页修改参数定义后再运行」，几个坏工具仍用「；」连成一句）
  const BROKEN_NOW = 'fake0brokennow0000000000000000'
  const BROKEN_NOW_ERROR = '自定义工具「lookup_order」的参数定义格式有误：参数 store 应写成 {"type": "string"} 这样的对象，不能直接写 "string"。到「工具」页修改参数定义后再运行'
    + '；自定义工具「fetch_rate」的参数定义格式有误：无法识别参数 n 的类型「int」，是否应为 integer；只能是 string、integer、number、boolean、array、object 中的一个。到「工具」页修改参数定义后再运行'
  fakes.set(`GET /api/runs/${BROKEN_NOW}`, () => ({ status: 200, json: {
    ...traceRun(BROKEN_NOW), status: 'failed', output: null, error: BROKEN_NOW_ERROR, error_node_id: 'query',
    usage: { input_tokens: 0, output_tokens: 0, cost_usd: 0, duration_ms: 400, wall_ms: 400, active_ms: 400, wait_ms: 0 },
    finished_at: iso(tt + 0.4),
  } }))
  fakes.set(`GET /api/runs/${BROKEN_NOW}/events`, () => ({ status: 200, json: [
    ...traceEvents().slice(0, 4),
    { seq: 5, type: 'node.failed', node_id: 'query', ts: tt + 0.35, data: { error: BROKEN_NOW_ERROR, duration_ms: 50 } },
    { seq: 6, type: 'run.failed', node_id: null, ts: tt + 0.4, data: {
      error: BROKEN_NOW_ERROR, node_id: 'query', timing: { wall_ms: 400, active_ms: 400, wait_ms: 0 } } },
  ] }))
  fakes.set(`GET /api/runs/${BROKEN_NOW}/graph`, () => ({ status: 200, json: { graph: traceGraph, workflow_id: wfHost.id, version: null } }))
  fakes.set(`GET /api/runs/${BROKEN_NOW}/artifacts`, () => ({ status: 200, json: [] }))
  await page.goto(`${WEB}/runs/${BROKEN_NOW}?view=stream`, { waitUntil: 'networkidle' })
  await page.locator('[data-run-banner=failed]').waitFor({ timeout: 5000 }).catch(() => {})
  const nowFix = page.locator('[data-run-banner=failed] [data-action=tools]')
  check('后端现在的说法：标题同样说有几个工具有误',
    (await page.locator('[data-failed-title]').innerText().catch(() => '')).trim() === '2 个自定义工具的参数定义有误')
  check('……主按钮同样是「去改参数定义」，直达第一个坏工具的编辑框，「继续运行」退成次要',
    (await nowFix.innerText().catch(() => '')).includes('去改参数定义')
      && /\bbtn-primary\b/.test(await nowFix.getAttribute('class').catch(() => '') ?? '')
      && await nowFix.getAttribute('href').catch(() => null) === '/tools/custom?edit=lookup_order'
      && await page.locator('[data-run-banner=failed] [data-action=continue]').count() === 1,
    await nowFix.getAttribute('href').catch(() => '没有这个按钮') ?? '')
  for (const k of ['', '/events', '/graph', '/artifacts']) fakes.delete(`GET /api/runs/${BROKEN_NOW}${k}`)

  // 归不了类的失败：标题就是原话。时间线里的报错不再在「技术细节」里把同一句重复一遍（3C REQ-25）
  const PLAIN = 'fake0plainfail0000000000000000'
  const PLAIN_ERROR = '汇总节点交回的内容是空的'
  fakes.set(`GET /api/runs/${PLAIN}`, () => ({ status: 200, json: {
    ...traceRun(PLAIN), status: 'failed', output: null, error: PLAIN_ERROR, error_node_id: 'query', finished_at: iso(tt + 0.4),
  } }))
  fakes.set(`GET /api/runs/${PLAIN}/events`, () => ({ status: 200, json: [
    ...traceEvents().slice(0, 4),
    { seq: 5, type: 'node.failed', node_id: 'query', ts: tt + 0.35, data: { error: PLAIN_ERROR, duration_ms: 50 } },
    { seq: 6, type: 'run.failed', node_id: null, ts: tt + 0.4, data: { error: PLAIN_ERROR, node_id: 'query', timing: { wall_ms: 400, active_ms: 400, wait_ms: 0 } } },
  ] }))
  fakes.set(`GET /api/runs/${PLAIN}/graph`, () => ({ status: 200, json: { graph: traceGraph, workflow_id: wfHost.id, version: null } }))
  fakes.set(`GET /api/runs/${PLAIN}/artifacts`, () => ({ status: 200, json: [] }))
  await page.goto(`${WEB}/runs/${PLAIN}?view=stream`, { waitUntil: 'networkidle' })
  await page.locator('[data-view-pane=stream] [data-turn-error]').waitFor({ timeout: 5000 }).catch(() => {})
  const plainErr = page.locator('[data-view-pane=stream] [data-turn-error]')
  check('原话就是标题时，时间线的报错不再收一份一模一样的技术细节',
    (await plainErr.innerText().catch(() => '')).includes(PLAIN_ERROR) && await plainErr.locator('details').count() === 0,
    `${await plainErr.locator('details').count()} 个技术细节`)
  for (const k of ['', '/events', '/graph', '/artifacts']) fakes.delete(`GET /api/runs/${PLAIN}${k}`)
})

// 等审批时实时流断了，重连上来后端回 stream.end{interrupted}；这一刻审批正好在别处批掉、
// 运行还没改状态（后端先记审批、再改运行）。以前流收尾时运行和审批分两头查，拼出
// 「interrupted、没有审批」就判成已挂起，而且再没人重查，一直挂着「可续跑」（3C REQ-3）
const GAP = 'fake0gapstream0000000000000000'
let gapApproval
await section('等审批时流断了又重连，撞上审批刚批掉的空档', async () => {
  let gapPhase = 'live'
  let gapReads = 0
  const gapSockets = []
  fakes.set(`GET /api/runs/${GAP}`, () => ({ status: 200, json: {
    ...traceRun(GAP), status: gapPhase === 'live' ? 'running' : gapReads++ === 0 ? 'interrupted' : 'running',
    output: null, finished_at: null,
  } }))
  fakes.set(`GET /api/runs/${GAP}/events`, () => ({ status: 200, json: traceEvents().slice(0, gapPhase === 'live' ? 9 : 11) }))
  fakes.set(`GET /api/runs/${GAP}/graph`, () => ({ status: 200, json: { graph: traceGraph, workflow_id: wfHost.id, version: null } }))
  fakes.set(`GET /api/runs/${GAP}/artifacts`, () => ({ status: 200, json: [] }))
  gapApproval = { id: 'fake0gapapproval', run_id: GAP, node_id: 'gate', status: 'pending', mode: 'approve',
    prompt: '放行吗？', created_at: iso(tt + 3.3), workflow_id: wfHost.id, workflow_name: '订单日报', node_label: '人工审批' }
  fakes.set('GET /api/approvals', (url) => ({ status: 200, json: gapPhase === 'live'
    && (!url.searchParams.get('run_id') || url.searchParams.get('run_id') === GAP) ? [gapApproval] : [] }))
  await page.routeWebSocket(new RegExp(`/api/runs/${GAP}/stream`), (ws) => {
    gapSockets.push(ws)
    // 第二条连接：重连上来，后端看运行还是 interrupted，按协议回结束标记
    if (gapSockets.length === 2) ws.send(JSON.stringify({ type: 'stream.end', status: 'interrupted', data: { status: 'interrupted' } }))
    // 再往后是确认它在跑之后接回来的：补上别处批掉之后的事件
    if (gapSockets.length >= 3) {
      const t = Date.now() / 1000
      ws.send(JSON.stringify({ seq: 12, type: 'human.resolved', node_id: 'gate', ts: t, data: { response: { approved: true }, actor: '张工' } }))
      ws.send(JSON.stringify({ seq: 13, type: 'run.resumed', node_id: null, ts: t, data: { actor: '张工' } }))
    }
  })
  await page.goto(`${WEB}/runs/${GAP}`, { waitUntil: 'networkidle' })
  await page.locator('[data-run-detail][data-run-code=running]').waitFor({ timeout: 5000 }).catch(() => {})
  const gapT = Date.now() / 1000
  gapSockets[0]?.send(JSON.stringify({ seq: 10, type: 'human.requested', node_id: 'gate', ts: gapT, data: { mode: 'approve', prompt: '放行吗？' } }))
  gapSockets[0]?.send(JSON.stringify({ seq: 11, type: 'run.interrupted', node_id: null, ts: gapT, data: {} }))
  await page.locator('[data-run-detail][data-run-code=waiting]').waitFor({ timeout: 5000 }).catch(() => {})
  await recordCodes()
  gapPhase = 'gap'
  await gapSockets[0]?.close()
  const gapBack = await page.waitForFunction(
    () => document.querySelector('[data-run-detail]')?.getAttribute('data-run-code') === 'running', null, { timeout: 9000 },
  ).then(() => true, () => false)
  const gapCodes = await recordedCodes()
  check('空档里没有判成「已挂起」', !gapCodes.includes('held'), gapCodes.join(' → '))
  check('再看一次发现在跑：状态回到运行中，接回实时流', gapBack && gapSockets.length >= 3, `${gapSockets.length} 条连接`)
  for (const k of ['', '/events', '/graph', '/artifacts']) fakes.delete(`GET /api/runs/${GAP}${k}`)
  fakes.delete('GET /api/approvals')
})

// 同一个空档，但别处批掉之后运行转眼就跑完了：再看一次已经是 succeeded。流停在审批上，
// 之后的事件这里一条也没收到，只把头上换成已完成的话，时间线永远停在审批卡上：没有
// 「放行」、没有汇总那一步、没有收尾（3C 返工 D1）
await section('等审批时流断了又重连，空档之后运行已经跑完', async () => {
  const GAPEND = 'fake0gapfinish0000000000000000'
  let gapEndPhase = 'live'
  let gapEndReads = 0
  const gapEndSockets = []
  fakes.set(`GET /api/runs/${GAPEND}`, () => {
    if (gapEndPhase === 'live') return { status: 200, json: { ...traceRun(GAPEND), status: 'running', output: null, finished_at: null } }
    return { status: 200, json: gapEndReads++ === 0
      ? { ...traceRun(GAPEND), status: 'interrupted', output: null, finished_at: null }
      : traceRun(GAPEND) }
  })
  fakes.set(`GET /api/runs/${GAPEND}/events`, () => ({ status: 200, json: gapEndPhase === 'live' ? traceEvents().slice(0, 9) : traceEvents() }))
  fakes.set(`GET /api/runs/${GAPEND}/graph`, () => ({ status: 200, json: { graph: traceGraph, workflow_id: wfHost.id, version: null } }))
  fakes.set(`GET /api/runs/${GAPEND}/artifacts`, () => ({ status: 200, json: [] }))
  const gapEndApproval = { ...gapApproval, id: 'fake0gapendapproval', run_id: GAPEND }
  fakes.set('GET /api/approvals', (url) => ({ status: 200, json: gapEndPhase === 'live'
    && (!url.searchParams.get('run_id') || url.searchParams.get('run_id') === GAPEND) ? [gapEndApproval] : [] }))
  await page.routeWebSocket(new RegExp(`/api/runs/${GAPEND}/stream`), (ws) => {
    gapEndSockets.push(ws)
    if (gapEndSockets.length === 2) ws.send(JSON.stringify({ type: 'stream.end', status: 'interrupted', data: { status: 'interrupted' } }))
  })
  await page.goto(`${WEB}/runs/${GAPEND}`, { waitUntil: 'networkidle' })
  await page.locator('[data-run-detail][data-run-code=running]').waitFor({ timeout: 5000 }).catch(() => {})
  const gapEndT = Date.now() / 1000
  gapEndSockets[0]?.send(JSON.stringify({ seq: 10, type: 'human.requested', node_id: 'gate', ts: gapEndT, data: { mode: 'approve', prompt: '放行吗？' } }))
  gapEndSockets[0]?.send(JSON.stringify({ seq: 11, type: 'run.interrupted', node_id: null, ts: gapEndT, data: {} }))
  await page.locator('[data-run-detail][data-run-code=waiting]').waitFor({ timeout: 5000 }).catch(() => {})
  await recordCodes()
  gapEndPhase = 'gap'
  await gapEndSockets[0]?.close()
  const gapEndDone = await page.waitForFunction(
    () => document.querySelector('[data-run-detail]')?.getAttribute('data-run-code') === 'succeeded', null, { timeout: 9000 },
  ).then(() => true, () => false)
  const gapEndSum = page.locator('[data-view-pane=stream] [data-step-status][data-node-id=sum]')
  await gapEndSum.first().waitFor({ timeout: 4000 }).catch(() => {})
  const gapEndCodes = await recordedCodes()
  const gapEndCount = Number(((await page.locator('[data-run-detail] > header').innerText().catch(() => ''))
    .match(/([\d,]+) 条事件/)?.[1] ?? '0').replace(/,/g, ''))
  check('空档之后已经跑完：状态落到已完成，中间没有判成「已挂起」', gapEndDone && !gapEndCodes.includes('held'), gapEndCodes.join(' → '))
  check('时间线补上了审批之后的事：汇总那一步在', await gapEndSum.count() > 0, `${await gapEndSum.count()} 行`)
  check('事件整条补齐，条数和后端一致', gapEndCount === traceEvents().length, `${gapEndCount} / ${traceEvents().length} 条`)
  for (const k of ['', '/events', '/graph', '/artifacts']) fakes.delete(`GET /api/runs/${GAPEND}${k}`)
  fakes.delete('GET /api/approvals')
})

// 同一个节点执行了三次：第一次写成文字被提醒、重答了；第二次收尾轮还想调工具；第三次干干净净。
// 节点卡片说的是游标那一刻所在的那一次，别把上一次的事挂到这一次上，也别因为后面还有一件就看不到这一次的
await section('航迹：节点卡片按这一次执行说事（3C REQ-13）', async () => {
  const REEXEC = 'fake0reexec0000000000000000000'
  fakes.set(`GET /api/runs/${REEXEC}`, () => ({ status: 200, json: {
    ...traceRun(REEXEC), output: { answer: '共 42 单' },
    usage: { input_tokens: 900, output_tokens: 400, cost_usd: 0.002, duration_ms: 3200, wall_ms: 3200, active_ms: 3200, wait_ms: 0 },
    finished_at: iso(tt + 3.2),
  } }))
  // 两条 tool_markup_leak 日志是老后端的原话（库里老运行存的是它们），留作「前端认得旧原文」的样本
  fakes.set(`GET /api/runs/${REEXEC}/events`, () => ({ status: 200, json: [
    ...traceEvents().slice(0, 4),
    { seq: 5, type: 'log', node_id: 'query', ts: tt + 1.0, data: {
      level: 'warn', code: 'tool_markup_leak', message: '查询订单把工具调用写成了文字（<｜｜DSML｜｜invoke name="db_query__shop"…），没有真正调用工具，已提醒它重试一次' } },
    { seq: 6, type: 'node.finished', node_id: 'query', ts: tt + 1.5, data: { duration_ms: 1200, preview: '共 40 单' } },
    { seq: 7, type: 'node.started', node_id: 'query', ts: tt + 2.0, data: { node_type: 'agent', label: '查询订单' } },
    { seq: 8, type: 'log', node_id: 'query', ts: tt + 2.5, data: {
      level: 'warn', code: 'tool_markup_leak', message: '查询订单收尾轮仍想调用工具（<｜｜DSML｜｜invoke name="db_query__shop"…），交出前面写的内容' } },
    { seq: 9, type: 'node.finished', node_id: 'query', ts: tt + 3.0, data: { duration_ms: 1000, preview: '共 41 单' } },
    { seq: 10, type: 'node.started', node_id: 'query', ts: tt + 3.05, data: { node_type: 'agent', label: '查询订单' } },
    { seq: 11, type: 'node.finished', node_id: 'query', ts: tt + 3.15, data: { duration_ms: 100, preview: '共 42 单' } },
    { seq: 12, type: 'run.finished', node_id: null, ts: tt + 3.2, data: {
      output: { answer: '共 42 单' }, usage: { input_tokens: 900, output_tokens: 400, cost_usd: 0.002 },
      timing: { wall_ms: 3200, active_ms: 3200, wait_ms: 0 } } },
  ] }))
  fakes.set(`GET /api/runs/${REEXEC}/graph`, () => ({ status: 200, json: { graph: traceGraph, workflow_id: wfHost.id, version: null } }))
  fakes.set(`GET /api/runs/${REEXEC}/artifacts`, () => ({ status: 200, json: [] }))
  await page.goto(`${WEB}/runs/${REEXEC}?view=trace&at=1200`, { waitUntil: 'networkidle' })
  await page.locator('[data-trace-readout]').waitFor({ timeout: 5000 }).catch(() => {})
  await page.locator('[data-trace-now] [data-now-node=query]').click({ timeout: 5000 }).catch(() => {})
  await page.waitForTimeout(150)
  const warnsOf = async () => (await page.locator('[data-trace-node=query] [data-node-warn]').allInnerTexts().catch(() => [])).join(' | ')
  const firstExec = await warnsOf()
  check('回放到第一次执行：说它被提醒后重答了，看不到第二次才有的收尾轮那件事',
    firstExec.includes('已提醒并重新作答') && !firstExec.includes('收尾时'), firstExec || '（没有提醒）')
  await page.goto(`${WEB}/runs/${REEXEC}?view=trace&at=2700`, { waitUntil: 'networkidle' })
  await page.locator('[data-trace-readout]').waitFor({ timeout: 5000 }).catch(() => {})
  await page.locator('[data-trace-now] [data-now-node=query]').click({ timeout: 5000 }).catch(() => {})
  await page.waitForTimeout(150)
  const secondExec = await warnsOf()
  check('回放到第二次执行：说收尾轮还想调工具、交出来的可能不完整，不说「重新作答」',
    secondExec.includes('收尾时仍试图调用工具') && !secondExec.includes('重新作答'), secondExec || '（没有提醒）')
  await page.locator('[data-run-trace] [role=slider]').focus().catch(() => {})
  await page.keyboard.press('End')
  await page.waitForTimeout(150)
  check('回到终态（第三次执行，干干净净）：不挂前两次的提醒',
    await mode() === '终态' && await page.locator('[data-trace-node=query]').count() === 1 && !(await warnsOf()),
    `${await mode()} ${await warnsOf() || '（没有提醒）'}`)
  // 关键时刻和节点卡同一个说法：两次执行各一个时刻，各用各的措辞。以前只取整次运行最后一次，
  // 第一次那下从列表里消失，收尾轮那下又被写成「以文本形式输出了工具调用」（3C REQ-23）
  const reMoments = await page.locator('[data-trace-moments] [data-moment*=":markup"]').evaluateAll((els) =>
    els.map((e) => ({ key: e.getAttribute('data-moment'), text: (e.textContent ?? '').replace(/\s+/g, ' ') })))
  const nudgeM = reMoments.find((m) => m.key === 'query:markup0')
  const settleM = reMoments.find((m) => m.key === 'query:markup')
  check('两次执行各有一个时刻：前一次带序号，最后一次沿用 query:markup', reMoments.length === 2 && !!nudgeM && !!settleM,
    reMoments.map((m) => m.key).join(', '))
  check('第一次写「以文本形式输出了工具调用、已提醒并重新作答」，第二次写「收尾时仍试图调用工具」',
    !!nudgeM?.text.includes('以文本形式输出了工具调用') && !!nudgeM?.text.includes('重新作答') && !nudgeM?.text.includes('收尾时')
      && !!settleM?.text.includes('收尾时仍试图调用工具') && !settleM?.text.includes('以文本形式输出'),
    reMoments.map((m) => m.text).join(' | '))
})

// 失败在「汇总」上，而工作流之后把「汇总」删了：画布上对准它什么也对不到
await section('失败的节点在工作流现在的图里已经没了', async () => {
  const GONE = 'fake0gonefail00000000000000000'
  const GONE_ERROR = '节点「汇总」失败：模型服务出错了（HTTP 500）'
  fakes.set(`GET /api/runs/${GONE}`, () => ({ status: 200, json: {
    ...traceRun(GONE), status: 'failed', output: null, error: GONE_ERROR, error_node_id: 'sum', finished_at: iso(tt + 6.2 + WAIT_S),
  } }))
  fakes.set(`GET /api/runs/${GONE}/events`, () => ({ status: 200, json: [
    ...traceEvents().slice(0, 15),
    { seq: 16, type: 'node.failed', node_id: 'sum', ts: tt + 6.0 + WAIT_S, data: { error: GONE_ERROR, duration_ms: 2400 } },
    { seq: 17, type: 'run.failed', node_id: null, ts: tt + 6.1 + WAIT_S, data: {
      error: GONE_ERROR, node_id: 'sum', timing: { wall_ms: (6.1 + WAIT_S) * 1000, active_ms: 6100, wait_ms: WAIT_S * 1000 } } },
  ] }))
  fakes.set(`GET /api/runs/${GONE}/graph`, () => ({ status: 200, json: { graph: traceGraph, workflow_id: wfHost.id, version: null } }))
  fakes.set(`GET /api/runs/${GONE}/artifacts`, () => ({ status: 200, json: [] }))
  await page.goto(`${WEB}/runs/${GONE}`, { waitUntil: 'networkidle' })
  await page.locator('[data-run-banner=failed]').waitFor({ timeout: 5000 }).catch(() => {})
  check('横幅不给落空的「在画布中定位」，改给「在航迹中看」',
    await page.locator('[data-run-banner=failed] [data-action=locate]').count() === 0
      && await page.locator('[data-run-banner=failed] [data-action=locate-trace]').count() === 1)
  const goneHead = page.locator('[data-run-detail] > header [data-action=open-canvas]')
  check('详情头照样能在画布中回放，只是不带对不上的 focus',
    await goneHead.getAttribute('href').catch(() => null) === `/studio/${wfHost.id}?run=${GONE}`
      && !/定位到/.test(await goneHead.getAttribute('title').catch(() => '') ?? ''),
    await goneHead.getAttribute('href').catch(() => '没有这个按钮') ?? '')
  await page.locator('[data-run-banner=failed] [data-action=locate-trace]').click().catch(() => {})
  await page.locator('[data-trace-readout]').waitFor({ timeout: 5000 }).catch(() => {})
  check('「在航迹中看」：切到航迹，选中失败的那个节点，停在终态',
    await viewTab('trace').getAttribute('aria-selected').catch(() => null) === 'true'
      && await page.locator('[data-trace-node=sum]').count() === 1 && await mode() === '终态')
  check('那张节点卡也说清画布上已经没有它', await page.locator('[data-trace-node=sum] [data-node-gone]').count() === 1)
})

// ------------------------------------------------------------------ 工件

await section('工件：按节点分组，就地打开', async () => {
  await page.goto(`${WEB}/runs/${TRACE}?view=artifacts`, { waitUntil: 'networkidle' })
  await page.locator('[data-run-artifacts]').waitFor({ timeout: 5000 }).catch(() => {})
  const groups = await page.locator('[data-run-artifacts] [data-artifact-group]').evaluateAll((els) => els.map((e) => e.getAttribute('data-artifact-group')))
  check('按节点分组，按执行顺序', groups.join(',') === 'query,sum', groups.join(',') || '没有分组')
  const qText = await page.locator(`[data-artifact-id="${ART_Q}"]`).innerText().catch(() => '')
  check('每件写着类型和大小', qText.includes('查询快照') && qText.includes('2.0 KB'), qText.replace(/\s+/g, ' '))
  await page.locator(`[data-artifact-id="${ART_Q}"] [data-action=artifact-open]`).click().catch(() => {})
  const artDialog = page.getByRole('dialog')
  // 弹窗先出来、内容取回来之后才画表：等到解开的那一块再读
  await artDialog.locator('[data-artifact]').waitFor({ timeout: 5000 }).catch(() => {})
  const artText = await artDialog.innerText().catch(() => '')
  check('用证据查看器打开：带节点名，解开 SQL 和结果表',
    artText.includes('查询订单') && artText.includes('select count(*)') && artText.includes('42'), artText.replace(/\s+/g, ' ').slice(0, 90))
  await page.keyboard.press('Escape')
  await page.locator(`[data-artifact-id="${ART_Q}"] [data-action=artifact-moment]`).click().catch(() => {})
  await page.waitForTimeout(400)
  check('「在航迹里看这一刻」跳到航迹、定位到它产出的时刻',
    await viewTab('trace').getAttribute('aria-selected').catch(() => null) === 'true' && (await readout('at')).includes('00:02.5'),
    await readout('at'))
  const realWithArt = []
  for (const r of allRuns.slice(0, 40)) {
    const a = await getJson(`/runs/${r.id}/artifacts`)
    if (a.length) { realWithArt.push([r, a]); break }
  }
  if (realWithArt.length) {
    const [r, a] = realWithArt[0]
    await page.goto(`${WEB}/runs/${r.id}`, { waitUntil: 'networkidle' })
    await page.locator('[data-run-detail]').waitFor()
    await page.waitForTimeout(300)
    check('真实运行：工件页签的数和接口一致', (await viewTab('artifacts').innerText().catch(() => '')).includes(String(a.length)),
      `接口 ${a.length} 件`)
  }
})

// ------------------------------------------------------------------ 回画布

await section('在画布中回放', async () => {
  await page.goto(`${WEB}/runs/${TRACE}`, { waitUntil: 'networkidle' })
  await page.locator('[data-run-detail]').waitFor()
  const replayLink = page.locator('[data-run-detail] > header [data-action=open-canvas]')
  check('跑完的运行，详情头是「在画布中回放」',
    (await replayLink.innerText().catch(() => '')).includes('在画布中回放')
      && await replayLink.getAttribute('href').catch(() => null) === `/studio/${wfHost.id}?run=${TRACE}`,
    await replayLink.getAttribute('href').catch(() => '') ?? '')
  check('工作流之后改过结构：链接说明画布上是现在的图',
    await replayLink.getAttribute('data-graph-drift').catch(() => null) === '1'
      && /改过/.test(await replayLink.getAttribute('title').catch(() => '') ?? ''))
  fakes.delete('GET /api/workflows')
})

// ------------------------------------------------------------------ 列表：缩略条、取消原因

await section('列表：每行一条缩略条', async () => {
  const FAILED_ROW = 'fake0failedrow0000000000000000'
  const CANCEL_ROW = 'fake0cancelrow0000000000000000'
  const failedRow = { ...traceRun(FAILED_ROW), status: 'failed', error: '节点「汇总」失败：模型服务出错了（HTTP 500）', error_node_id: 'sum',
    usage: { wall_ms: 4000, active_ms: 4000, wait_ms: 0, duration_ms: 4000 }, finished_at: iso(tt + 4) }
  const cancelRow = { ...traceRun(CANCEL_ROW), status: 'cancelled', error: '用户取消：等待审批时放弃了这次运行',
    usage: { wall_ms: 90000, active_ms: 3000, wait_ms: 87000, duration_ms: 3000 }, finished_at: iso(tt + 90) }
  fakes.set('GET /api/runs', () => ({ status: 200, json: [traceRun(TRACE), failedRow, cancelRow, ...listTop] }))
  await page.goto(`${WEB}/runs`, { waitUntil: 'networkidle' })
  await rowsOf().first().waitFor()
  const shapeCount = await page.locator('[data-runs-list] [data-run-id] [data-run-shape]').count()
  check('每一行都有一条缩略条', shapeCount === await rowsOf().count() && shapeCount > 0, `${shapeCount} 条`)
  const waitFrac = await page.locator(`[data-run-id="${TRACE}"] [data-shape-seg=wait]`).evaluate(
    (el) => el.getBoundingClientRect().width / el.parentElement.getBoundingClientRect().width).catch(() => 0)
  check('等待审批占了九成八的总时长，缩略条上等待那段也占大半', waitFrac > 0.9, waitFrac.toFixed(2))
  check('失败的那一行收在失败色上', await page.locator(`[data-run-id="${FAILED_ROW}"] [data-shape-end=failed]`).count() === 1)
  check('缩略条的 title 写着执行时长和等待审批',
    /执行时长 .+ · 等待审批 .+/.test(await page.locator(`[data-run-id="${TRACE}"] [data-run-shape]`).getAttribute('title').catch(() => '') ?? ''))
  const cancelText = (await page.locator(`[data-run-id="${CANCEL_ROW}"]`).innerText().catch(() => '')).replace(/\s+/g, ' ')
  const cancelWhy = cancelText.includes('等待审批时放弃了这次运行')
  const cancelDup = cancelText.includes('用户取消')
  // 这一行的工作流名借的是沙箱里的真实工作流，只报两项判断
  check('已取消写原因，不重复「用户取消」', cancelWhy && !cancelDup, `原因 ${cancelWhy ? '在' : '缺'} · 「用户取消」${cancelDup ? '重复了' : '没重复'}`)
  fakes.delete('GET /api/runs')
})

await section('工作流筛选：未保存的工作流', async () => {
  await page.goto(`${WEB}/runs`, { waitUntil: 'networkidle' })
  await rowsOf().first().waitFor()
  const wfOptions = await page.locator('[data-runs-workflow] option').evaluateAll((els) => els.map((e) => e.value))
  check('工作流下拉里有「未保存的工作流」', wfOptions.includes('__none__'))
  if (wfOptions.includes('__none__')) {
    await listAfter(() => page.locator('[data-runs-workflow]').selectOption('__none__'), (p) => p.get('workflow_id') === '__none__')
    check('地址记下 ?wf=__none__', new URL(page.url()).searchParams.get('wf') === '__none__')
    const names = await page.locator('[data-runs-list] [data-run-id] > div:first-of-type').evaluateAll((els) => els.map((e) => e.textContent ?? ''))
    check('筛出来的都是未保存的工作流', names.length > 0 && names.every((n) => n.includes('未保存的工作流')), `${names.length} 行`)
    await page.locator('[data-runs-workflow]').selectOption('')
    await settle()
  }

  // ------------------------------------------------------------------ 放弃

  if (pending.length) {
    console.log('\n=== 等审批的运行就地放弃 ===')
    const target = pending[0]
    const full = await getJson(`/runs/${target.run_id}`)
    const evs = await getJson(`/runs/${target.run_id}/events`)
    await page.goto(`${WEB}/runs/${target.run_id}`, { waitUntil: 'networkidle' })
    await page.locator('[data-run-detail][data-run-code=waiting]').waitFor({ timeout: 8000 }).catch(() => {})
    const abandon = page.locator('[data-run-banner=waiting] [data-action=abandon]')
    check('等审批的横幅上有「放弃这次运行」', await abandon.count() === 1)
    if (await abandon.count()) {
      let cancelCalls = 0
      fakes.set(`POST /api/runs/${target.run_id}/cancel`, () => { cancelCalls++; return { status: 200, json: { ok: true, status: 'cancelled' } } })
      await abandon.click()
      const ask = page.getByRole('dialog')
      await ask.waitFor({ timeout: 5000 }).catch(() => {})
      check('放弃前要确认，并写清待审批一并关闭', (await ask.innerText().catch(() => '')).includes('待审批'))
      const tc = Date.now() / 1000
      fakes.set(`GET /api/runs/${target.run_id}`, () => ({ status: 200, json: {
        ...full, status: 'cancelled', error: '用户取消：等待审批时放弃了这次运行', finished_at: iso(tc),
      } }))
      fakes.set(`GET /api/runs/${target.run_id}/events`, () => ({ status: 200, json: [...evs, {
        seq: (evs.at(-1)?.seq ?? 0) + 1, type: 'run.cancelled', node_id: null, ts: tc,
        data: { actor: '张工', message: '放弃了这次运行，1 条待审批一并关闭', timing: { wall_ms: 1000, active_ms: 10, wait_ms: 990 } },
      }] }))
      fakes.set('GET /api/approvals', (url) => ({
        status: 200, json: pending.filter((a) => a.id !== target.id && (!url.searchParams.get('run_id') || a.run_id === url.searchParams.get('run_id'))),
      }))
      await ask.getByRole('button', { name: '放弃这次运行' }).click({ timeout: 5000 }).catch(() => {})
      const gone = await page.waitForFunction(
        () => document.querySelector('[data-run-detail]')?.getAttribute('data-run-code') === 'cancelled',
        null, { timeout: 6000 },
      ).then(() => true, () => false)
      check('发的是 POST /cancel', cancelCalls === 1)
      check('状态变成已取消', gone)
      const status = (await page.locator('[data-run-telemetry]').innerText().catch(() => '')).replace(/\s+/g, ' ')
      check('状态格写着谁放弃的', status.includes('张工') && status.includes('放弃'), status.slice(0, 60))
      fakes.delete(`POST /api/runs/${target.run_id}/cancel`)
      fakes.delete(`GET /api/runs/${target.run_id}`)
      fakes.delete(`GET /api/runs/${target.run_id}/events`)
      fakes.delete('GET /api/approvals')
    }
  }
})

// ------------------------------------------------------------------ 等了几天、窄屏

// 停在审批上十天多：等人要写「10 天 05 小时」，而不是被截成「245 小时 3…」；游标写「T+10 天 05:30:07」，
// 不写「T+245:30:07.3」（同一行的墙钟、等人写的是「10 天」，而且放不下）。等人的时长在涨，游标要走
const STALE = 'fake0stalewait0000000000000000'
const fits = (sel) => page.locator(sel).evaluateAll((els) => els.length > 0 && els.every((e) => e.scrollWidth <= e.clientWidth + 0.5))
let tw, longApproval
await section('等了几天的审批、1024 宽', async () => {
  tw = Date.now() / 1000 - 10 * 86400 - 5.5 * 3600
  longApproval = {
    id: 'fake0longappr0000000000000000', run_id: STALE, node_id: 'gate', mode: 'approve', title: '放行吗？',
    payload: { kind: 'human_node', node_id: 'gate', mode: 'approve', title: '放行吗？', message: '', schema: {}, context: {} },
    status: 'pending', response: {}, created_at: iso(tw + 3.3), resolved_by: null, resolved_at: null,
    workflow_id: wfHost.id, workflow_name: '订单日报', node_label: '人工审批', run_status: 'interrupted', run_class: 'exploratory',
  }
  fakes.set(`GET /api/runs/${STALE}`, () => ({ status: 200, json: {
    ...traceRun(STALE), status: 'interrupted', output: null, finished_at: null, created_at: iso(tw), started_at: iso(tw),
    usage: { input_tokens: 1200, output_tokens: 300, cost_usd: 0.004 },
  } }))
  fakes.set(`GET /api/runs/${STALE}/events`, () => ({ status: 200, json: traceEvents().slice(0, 11).map((e) => ({ ...e, ts: e.ts - tt + tw })) }))
  fakes.set(`GET /api/runs/${STALE}/graph`, () => ({ status: 200, json: { graph: traceGraph, workflow_id: wfHost.id, version: null } }))
  fakes.set(`GET /api/runs/${STALE}/artifacts`, () => ({ status: 200, json: [] }))
  fakes.set('GET /api/approvals', (url) => ({
    status: 200, json: !url.searchParams.get('run_id') || url.searchParams.get('run_id') === STALE ? [longApproval] : [],
  }))
  const DAY_AT = /^T\+10 天 05:3\d:\d\d$/
  // 1300 宽：详情区 836px，排成一行的话一格只剩 78px，「10 天 05 小时」放不下，得排两行
  for (const [w, h] of [[1440, 900], [1300, 860], [1024, 768]]) {
    await page.setViewportSize({ width: w, height: h })
    await page.goto(`${WEB}/runs/${STALE}?view=trace`, { waitUntil: 'networkidle' })
    await page.locator('[data-trace-readout]').waitFor({ timeout: 5000 }).catch(() => {})
    const waitRead = await readout('wait')
    check(`${w} 宽：等待审批写成跨天的跨度（10 天 05 小时），不被截断`,
      waitRead.startsWith('10 天 05 小时') && await fits('[data-trace-readout] [data-readout=wait]'), waitRead)
    const atRead = (await readout('at')).trim()
    check(`${w} 宽：游标写成跨天的时刻（T+10 天 05:30:xx），不被截断`,
      DAY_AT.test(atRead) && await fits('[data-trace-readout] [data-readout=at]'), atRead)
  }
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`${WEB}/runs/${STALE}?view=trace`, { waitUntil: 'networkidle' })
  await page.locator('[data-trace-readout]').waitFor({ timeout: 5000 }).catch(() => {})
  check('「此刻」的等审批也写跨天', (await page.locator('[data-trace-now] [data-now-node=gate]').innerText().catch(() => '')).includes('10 天 05 小时'))
  const waitAtA = await readout('at')
  // 跨天的时刻只写到秒：隔一秒多再读
  await page.waitForTimeout(1100)
  const waitAtB = await readout('at')
  check('停在审批上时游标照样跟着现在走（和坞里同一拍）', await mode() === '实时' && waitAtA !== waitAtB, `${waitAtA} → ${waitAtB}`.replace(/\s+/g, ' '))
})

// 去审批卡：审批卡在时间线里。从航迹、工件、原始事件点过来，要切回时间线、描一下卡片，
// 并且焦点进到卡片里——键盘和读屏用户才接得着。视图经地址切，提交比点击晚好几帧
await section('去审批卡：从哪个视图点都能把焦点送进卡片', async () => {
  for (const [from, label] of [['trace', '航迹'], ['artifacts', '工件'], ['raw', '原始事件'], ['stream', '时间线']]) {
    await page.goto(`${WEB}/runs/${STALE}${from === 'trace' || from === 'artifacts' ? `?view=${from}` : ''}`, { waitUntil: 'networkidle' })
    await page.locator('[data-run-banner=waiting] [data-action=jump-approval]').waitFor({ timeout: 5000 }).catch(() => {})
    if (from === 'raw') {
      await page.locator('[data-action=raw]').click().catch(() => {})
      await page.locator('[data-raw-events]').waitFor({ timeout: 3000 }).catch(() => {})
    }
    await page.locator('[data-run-banner=waiting] [data-action=jump-approval]').click().catch(() => {})
    const into = await page.waitForFunction(() => !!document.activeElement?.closest('[data-approval-card]'), null, { timeout: 3000 })
      .then(() => true, () => false)
    const ringed = await page.locator(`[data-approval-card="${longApproval.id}"].runs-focus-ring`).count()
    const back = await viewTab('stream').getAttribute('aria-selected').catch(() => null) === 'true'
    check(`从「${label}」点「去审批卡」：回到时间线，焦点进了审批卡，卡片描了一下边`, into && ringed === 1 && back,
      `时间线 ${back ? '是' : '否'} · 描边 ${ringed} · 焦点在 ${await page.evaluate(() => {
        const a = document.activeElement
        return `${a?.tagName}${a?.getAttribute('data-action') ? `[${a.getAttribute('data-action')}]` : ''}`
      })}`)
  }
})

// 批过的十天等待：「审批已处理」「运行完成」都在 T+10 天之后。关键时刻里时刻那一栏按最宽的
// 一条排齐，不压到后面的徽标上；终态的游标放得下
await section('跨天的关键时刻', async () => {
  const LONGDONE = 'fake0longdone00000000000000000'
  const td = Date.now() / 1000 - 11 * 86400
  const LONG_S = 10 * 86400
  const longTiming = { wall_ms: (6.2 + LONG_S) * 1000, active_ms: 6200, wait_ms: LONG_S * 1000 }
  fakes.set(`GET /api/runs/${LONGDONE}`, () => ({ status: 200, json: {
    ...traceRun(LONGDONE), created_at: iso(td), started_at: iso(td), finished_at: iso(td + 6.2 + LONG_S),
    usage: { input_tokens: 2000, output_tokens: 800, cost_usd: 0.007, duration_ms: 6200, ...longTiming },
  } }))
  fakes.set(`GET /api/runs/${LONGDONE}/events`, () => ({ status: 200, json: traceEvents().map((e) => ({
    ...e,
    ts: e.ts - tt + td + (e.seq >= 12 ? LONG_S - WAIT_S : 0),
    ...(e.type === 'run.finished' ? { data: { ...e.data, timing: longTiming } } : {}),
  })) }))
  fakes.set(`GET /api/runs/${LONGDONE}/graph`, () => ({ status: 200, json: { graph: traceGraph, workflow_id: wfHost.id, version: null } }))
  fakes.set(`GET /api/runs/${LONGDONE}/artifacts`, () => ({ status: 200, json: [] }))
  const momentsAligned = () => page.locator('[data-trace-moments] [data-moment]').evaluateAll((els) => {
    const cols = els.map((b) => {
      const at = b.querySelector('[data-moment-at]')
      return { at: at.getBoundingClientRect(), fit: at.scrollWidth <= at.clientWidth + 0.5, mark: at.nextElementSibling.getBoundingClientRect().left }
    })
    return cols.length > 0 && cols.every((c) => c.fit && c.at.right <= c.mark + 0.5 && Math.abs(c.mark - cols[0].mark) < 0.5)
  })
  for (const [w, h] of [[1440, 900], [1024, 768]]) {
    await page.setViewportSize({ width: w, height: h })
    await page.goto(`${WEB}/runs/${LONGDONE}?view=trace`, { waitUntil: 'networkidle' })
    await page.locator('[data-trace-moments]').waitFor({ timeout: 5000 }).catch(() => {})
    const texts = (await page.locator('[data-trace-moments] [data-moment]').allInnerTexts().catch(() => [])).map((t) => t.replace(/\s+/g, ' '))
    check(`${w} 宽：跨天的关键时刻写「T+10 天 00:00:03」，时刻一栏排齐、不压到徽标上`,
      texts.some((t) => t.includes('T+10 天 00:00:03') && t.includes('审批已处理') && t.includes('等了 10 天')) && await momentsAligned(),
      texts.join(' | '))
    check(`${w} 宽：终态的游标写「T+10 天 00:00:06」，放得下`,
      (await readout('at')).trim() === 'T+10 天 00:00:06' && await fits('[data-trace-readout] [data-readout=at]'), await readout('at'))
  }
})

// 屏幕矮、节点多：九个节点的失败运行，横幅占去一截。读数区先拿够它要的，默认选中的失败
// 节点卡、关键时刻都得在首屏；坞拿剩下的，不矮过能拖到的最矮，泳道在坞里滚
await section('屏幕矮、节点多：读数区不被航迹坞压住', async () => {
  const FAN = 'fake0fanfail000000000000000000'
  const FAN_ERROR = '节点「查询订单」失败：模型服务出错了（HTTP 500）'
  const FAN_STEPS = ['check', 'score', 'route', 'publish', 'archive']
  const fanGraph = {
    nodes: [...traceGraph.nodes, ...FAN_STEPS.map((id, i) => ({
      id, type: 'llm', position: { x: 800 + i * 200, y: 0 }, data: { label: `步骤 ${i + 1}`, config: {} },
    }))],
    edges: [...traceGraph.edges, ...FAN_STEPS.map((id, i) => ({ source: i ? FAN_STEPS[i - 1] : 'sum', target: id }))],
  }
  fakes.set(`GET /api/runs/${FAN}`, () => ({ status: 200, json: {
    ...traceRun(FAN), status: 'failed', output: null, error: FAN_ERROR, error_node_id: 'query',
    usage: { input_tokens: 0, output_tokens: 0, cost_usd: 0, duration_ms: 1200, wall_ms: 1200, active_ms: 1200, wait_ms: 0 },
    finished_at: iso(tt + 1.2),
  } }))
  fakes.set(`GET /api/runs/${FAN}/events`, () => ({ status: 200, json: [
    ...traceEvents().slice(0, 4),
    { seq: 5, type: 'node.failed', node_id: 'query', ts: tt + 1.1, data: { error: FAN_ERROR, duration_ms: 800 } },
    { seq: 6, type: 'run.failed', node_id: null, ts: tt + 1.2, data: {
      error: FAN_ERROR, node_id: 'query', timing: { wall_ms: 1200, active_ms: 1200, wait_ms: 0 } } },
  ] }))
  fakes.set(`GET /api/runs/${FAN}/graph`, () => ({ status: 200, json: { graph: fanGraph, workflow_id: wfHost.id, version: null } }))
  fakes.set(`GET /api/runs/${FAN}/artifacts`, () => ({ status: 200, json: [] }))
  const firstScreen = () => page.evaluate(() => {
    const read = document.querySelector('[data-trace-readout]')?.getBoundingClientRect()
    const whole = (sel) => {
      const b = document.querySelector(sel)?.getBoundingClientRect()
      return !!b && !!read && b.top >= read.top - 0.5 && b.bottom <= read.bottom + 1
    }
    return {
      card: whole('[data-trace-node=query]'), moments: whole('[data-trace-moments]'),
      dock: Math.round(document.querySelector('[data-run-trace] section[aria-label="航迹"]')?.getBoundingClientRect().height ?? 0),
    }
  })
  // 屏幕够高：坞按 RunTimeline.dockHeightFor 给，泳道一行不裁、不用滚，也不多留一截空白
  // （以前这里自己抄了一份排版常量，再加 10px 余量糊住少算的上边框，3C REQ-8 / REQ-13）
  await page.setViewportSize({ width: 1440, height: 1500 })
  await page.goto(`${WEB}/runs/${FAN}?view=trace`, { waitUntil: 'networkidle' })
  await page.locator('[data-run-trace] .tl-lane').first().waitFor({ timeout: 5000 }).catch(() => {})
  await page.waitForTimeout(200)
  const tallDock = await page.evaluate(() => {
    const sc = document.querySelector('[data-run-trace] .tl-scroll')
    const lanes = [...document.querySelectorAll('[data-run-trace] .tl-lane')]
    const last = lanes.at(-1)?.getBoundingClientRect()
    const b = sc?.getBoundingClientRect()
    return { lanes: lanes.length, over: sc ? sc.scrollHeight - sc.clientHeight : null,
             slack: last && b ? Math.round(b.bottom - last.bottom) : null }
  })
  check('屏幕够高：坞正好装下每一条泳道，不滚、不多留空白', tallDock.lanes >= 9 && tallDock.over === 0
    && tallDock.slack != null && tallDock.slack >= 0 && tallDock.slack <= 1, JSON.stringify(tallDock))
  for (const [w, h] of [[1440, 900], [1180, 800], [1024, 768]]) {
    await page.setViewportSize({ width: w, height: h })
    await page.goto(`${WEB}/runs/${FAN}?view=trace`, { waitUntil: 'networkidle' })
    await page.locator('[data-trace-node=query]').waitFor({ timeout: 5000 }).catch(() => {})
    await page.waitForTimeout(150)
    const seen = await firstScreen()
    check(`${w}×${h}：失败节点卡、关键时刻整块都在首屏，坞不矮过 132px`, seen.card && seen.moments && seen.dock >= 132, JSON.stringify(seen))
  }
  // 再矮（或者横幅更高）就放不下了：坞不再让，读数区底边淡出，看得出下面还有
  await page.setViewportSize({ width: 1024, height: 640 })
  await page.waitForTimeout(300)
  check('1024×640：读数区放不下时底边淡出，提示能往下滚', await page.locator('[data-trace-readout][data-more]').count() === 1)

  // 1024 宽：详情区只有 600px，读数一格不到 70px 的字宽
  await page.setViewportSize({ width: 1024, height: 768 })
  fakes.set('GET /api/runs', () => ({ status: 200, json: [traceRun(TRACE), ...listTop] }))
  await page.goto(`${WEB}/runs/${TRACE}`, { waitUntil: 'networkidle' })
  await page.locator('[data-run-telemetry]').waitFor({ timeout: 5000 }).catch(() => {})
  const cut = await page.locator('[data-run-telemetry] [data-telemetry]').evaluateAll((els) =>
    els.filter((e) => e.scrollWidth > e.clientWidth + 0.5).map((e) => `${e.getAttribute('data-telemetry')}「${e.textContent}」`))
  check('1024 宽：详情头的读数没有一格被截成省略号', cut.length === 0, cut.join('、') || '全部放得下')
  const footH = await page.locator(`[data-runs-list] [data-run-id="${TRACE}"] [data-run-duration]`).evaluate(
    (el) => el.parentElement.getBoundingClientRect().height).catch(() => 99)
  check('1024 宽：列表行底下那一行不折行（时长太长就截断，用量不被拆开）', footH <= 18, `${footH}px`)
  await page.setViewportSize({ width: 1440, height: 900 })
  fakes.delete('GET /api/runs')
  fakes.delete('GET /api/approvals')
})

// ------------------------------------------------------------------ 证据页签（可点击证据三期）

// 夹具是后端真跑出来的（compose_doc、guess_sources、api/evidence.py 的审计函数），只用通用名
const fxe = JSON.parse(readFileSync(new URL('../frontend/src/run/__tests__/evidence-entity.json', import.meta.url), 'utf8'))
const EV = 'fake0evidence0000000000000000'
const EV_LEGACY = 'fake0evlegacy0000000000000000'
const EV_NONE = 'fake0evnone000000000000000000'
const EV_OLD = 'fake0evoldback000000000000000'
const evGraph = { nodes: [
  { id: 'in', type: 'input', position: { x: 0, y: 0 }, data: { label: '输入', config: {} } },
  { id: 'fetch', type: 'agent', position: { x: 200, y: 0 }, data: { label: '取数', config: { tools: ['db_query__shop'] } } },
  { id: 'manual', type: 'retrieve', position: { x: 200, y: 150 }, data: { label: '查手册', config: {} } },
  { id: 'write', type: 'report', position: { x: 400, y: 0 }, data: { label: '写周报', config: {} } },
  { id: 'out', type: 'output', position: { x: 600, y: 0 }, data: { label: '成果', config: {} } },
], edges: [{ source: 'in', target: 'fetch' }, { source: 'fetch', target: 'write' }, { source: 'manual', target: 'write' },
  { source: 'write', target: 'out' }] }
const evRun = (id, output) => ({
  ...base, id, workflow_id: null, workflow_name: '证据检查', status: 'succeeded', run_class: 'formal',
  version: 3, version_hash: null, manifest_hash: 'f'.repeat(64), manifest_seq: 88, error: null, error_node_id: null,
  input: { week: '2026-W37' }, output, usage: { input_tokens: 10, output_tokens: 10, cost_usd: 0, wall_ms: 4000, active_ms: 4000, wait_ms: 0 },
  created_at: new Date(Date.now() - 7200e3).toISOString(), started_at: new Date(Date.now() - 7200e3).toISOString(),
  finished_at: new Date(Date.now() - 7196e3).toISOString(),
})
const evEvents = (output, report = true) => {
  const t0 = Date.now() / 1000 - 7200
  return [
    { seq: 1, type: 'run.started', node_id: null, ts: t0, data: { nodes: 5 } },
    { seq: 2, type: 'node.started', node_id: 'write', ts: t0 + 1, data: { node_type: 'report', label: '写周报' } },
    ...(report ? [{ seq: 3, type: 'report.checked', node_id: 'write', ts: t0 + 3, data: fxe.report_checked }] : []),
    { seq: 4, type: 'node.finished', node_id: 'write', ts: t0 + 3.1, data: { duration_ms: 2100 } },
    { seq: 5, type: 'run.finished', node_id: null, ts: t0 + 4, data: { output, usage: {}, timing: { wall_ms: 4000, active_ms: 4000, wait_ms: 0 } } },
  ]
}
/** 证据相关的请求（审计、导出、片段）记下来：导出发出的请求要带对 format 和 groups */
const evRequests = []
page.on('request', (r) => {
  const u = new URL(r.url())
  if (/\/api\/runs\/fake0ev[^/]*\/evidence/.test(u.pathname)) evRequests.push(`${u.pathname}${u.search}`)
})
const auditFake = (body, csv) => (url) => {
  const format = url.searchParams.get('format')
  const groups = url.searchParams.get('groups')?.split(',')
  const pick = groups ? { ...body, groups: body.groups.filter((g) => groups.includes(g.key)) } : body
  if (format === 'csv') {
    return { status: 200, body: csv, headers: { 'Content-Type': 'text/csv; charset=utf-8',
      'Content-Disposition': `attachment; filename="evidence-${body.run_id.slice(0, 8)}.csv"` } }
  }
  if (format === 'json') {
    return { status: 200, body: JSON.stringify(pick), headers: { 'Content-Type': 'application/json',
      'Content-Disposition': `attachment; filename="evidence-${body.run_id.slice(0, 8)}.json"` } }
  }
  return { status: 200, json: pick }
}
const evFakes = (id, { output, graph, audit, csv, report = true }) => {
  fakes.set(`GET /api/runs/${id}`, () => ({ status: 200, json: evRun(id, output) }))
  fakes.set(`GET /api/runs/${id}/events`, () => ({ status: 200, json: evEvents(output, report) }))
  fakes.set(`GET /api/runs/${id}/graph`, () => ({ status: 200, json: { graph: evGraph, workflow_id: null, version: 3 } }))
  fakes.set(`GET /api/runs/${id}/artifacts`, () => ({ status: 200, json: [] }))
  fakes.set(`GET /api/runs/${id}/evidence`, () => ({ status: 200, json: { ...graph, run_id: id } }))
  fakes.set(`GET /api/runs/${id}/evidence/audit`, audit ? auditFake({ ...audit, run_id: id }, csv ?? '')
    : () => ({ status: 404, json: { detail: 'Not Found' } }))
}
const openEvidence = async (id) => {
  await page.goto(`${WEB}/runs/${id}?view=evidence`, { waitUntil: 'networkidle' })
  await page.locator('[data-view-pane=evidence] [data-evidence-pane]:not([data-evidence-pane=loading])').waitFor({ timeout: 8000 })
  await page.waitForTimeout(300)
}
const auditGroups = () => page.locator('[data-audit-table] tbody[data-audit-group]').evaluateAll((els) => els.map((e) => e.getAttribute('data-audit-group')))
const auditRowsShown = () => page.locator('[data-audit-table] tr[data-audit-row]').count()

await section('证据页签：左边报告、右边常驻面板、下方审计表', async () => {
  evFakes(EV, { output: fxe.output, graph: fxe.graph, audit: fxe.audit, csv: fxe.audit_csv })
  fakes.set(`GET /api/artifacts/${fxe.doc_artifact}`, () => ({ status: 200, json: { id: fxe.doc_artifact, content: fxe.doc } }))
  fakes.set(`GET /api/runs/${EV}/evidence/segments/`, () => ({ status: 404, json: {} }))
  await page.route(new RegExp(`/api/runs/${EV}/evidence/segments/`), (r) => {
    const sid = decodeURIComponent(new URL(r.request().url()).pathname.split('/').pop())
    return fxe.segments[sid] ? r.fulfill({ json: fxe.segments[sid] }) : r.fulfill({ status: 404, json: { detail: '没有这个片段' } })
  })
  await page.goto(`${WEB}/runs/${EV}`, { waitUntil: 'networkidle' })
  await page.locator('[data-run-detail]').waitFor()
  check('详情多了「证据」页签（在航迹和工件之间）', await viewTab('evidence').count() === 1
    && (await page.locator('[data-run-detail] [role=tab]').evaluateAll((els) => els.map((e) => e.getAttribute('data-tab')))).join(',') === 'stream,trace,evidence,artifacts')
  check('没打开证据页签之前不取证据图和审计表', !evRequests.some((u) => u.includes(EV)), evRequests.join(' '))
  await viewTab('evidence').click()
  await page.locator('[data-view-pane=evidence] [data-evidence-pane=cited]').waitFor({ timeout: 8000 }).catch(() => {})
  await page.waitForTimeout(400)
  check('证据页签能打开，地址记着 ?view=evidence', await viewTab('evidence').getAttribute('aria-selected') === 'true'
    && new URL(page.url()).searchParams.get('view') === 'evidence')
  check('左边是报告（逐段可点的文档）', await page.locator('[data-evidence-report=write] [data-evidence-doc] [data-seg]').count() > 5)
  check('右边是常驻面板的位置，没点片段时说怎么用', await page.locator('[data-evidence-dock] [data-evidence-dock-idle]').count() === 1)
  const seal = await page.locator('[data-evidence-seal]').innerText().catch(() => '')
  check('显示封存核对的结果', await page.locator('[data-evidence-seal]').getAttribute('data-evidence-seal') === 'done'
    && seal.includes('已封存 · 核对一致') && seal.includes('88'), seal)
  check('……也说报告文档的哈希一致', (await page.locator('[data-evidence-doc-hash=ok]').innerText().catch(() => '')).includes('写周报'))
  check('审计表按状态分组：无证据、可疑名称在前，有出处在后', (await auditGroups()).join(',') === 'none,suspicious,cited',
    (await auditGroups()).join(','))
  const counts = await page.locator('[data-audit-group-toggle]').allInnerTexts()
  check('每组写着条数（无证据 3、可疑名称 3、有出处 10）', counts.join('|').includes('无证据\n3') || (counts[0].includes('3') && counts[1].includes('3') && counts[2].includes('10')),
    counts.join(' | ').replace(/\n/g, ' '))
  check('一共 16 行', await auditRowsShown() === 16, String(await auditRowsShown()))
  // 这张图的出口没有出具契约：后端说「要求了，但没有契约按它判档」，不说「出具时计入缺口」。表里照后端的原话写
  const claim = page.locator('[data-audit-table] tbody[data-audit-group=none] tr[data-audit-row]', { hasText: '另外参考了' })
  const claimNote = fxe.audit.groups.find((g) => g.key === 'none')?.rows.find((r) => r.kind === 'claim')?.note ?? '（夹具里没有结论句那行）'
  check('没挂依据的结论句也列在无证据里，原因照后端的原话（要求结论句附依据、没有出具契约据此判档）', await claim.count() === 1
    && claimNote.includes('要求结论句附依据') && claimNote.includes('没有出具契约据此判档') && (await claim.innerText()).includes(claimNote), claimNote)
  check('可疑名称那组有「疑似不存在的名称」和「无法核实」', (await page.locator('[data-audit-table] tbody[data-audit-group=suspicious]').innerText())
    .includes('疑似不存在的名称') && (await page.locator('[data-audit-table] tbody[data-audit-group=suspicious]').innerText()).includes('无法核实'))
  check('有出处的行写着在封存范围内', await page.locator('[data-audit-table] tbody[data-audit-group=cited] [data-audit-sealed=yes]').count() === 10)
  check('反引号里的名字在表里不带反引号', !(await page.locator('[data-audit-table]').innerText()).includes('`'))

  // 只看无证据 / 可疑名称
  await page.locator('[data-audit-filter-option=problems]').click()
  await page.waitForTimeout(200)
  check('筛选「只看无证据 / 可疑名称」：只剩这两组', (await auditGroups()).join(',') === 'none,suspicious' && await auditRowsShown() === 6,
    `${(await auditGroups()).join(',')} ${await auditRowsShown()}`)
  check('筛选是一组单选（radio），选中的那项 aria-checked', await page.locator('[data-audit-filter-option=problems]').getAttribute('aria-checked') === 'true')

  // 导出：发出的请求要带对 format，筛选了就带 groups
  evRequests.length = 0
  const dl1 = page.waitForEvent('download', { timeout: 5000 }).catch(() => null)
  await page.locator('[data-audit-export=csv]').click()
  const d1 = await dl1
  check('导出 CSV：请求 /evidence/audit?format=csv，只要筛出来的两组',
    evRequests.some((u) => u === `/api/runs/${EV}/evidence/audit?format=csv&groups=none%2Csuspicious`), evRequests.join(' '))
  check('……下载的文件名是后端给的', d1?.suggestedFilename() === `evidence-${EV.slice(0, 8)}.csv`, d1?.suggestedFilename() ?? '没有下载')
  await page.locator('[data-audit-filter-option=all]').click()
  evRequests.length = 0
  const dl2 = page.waitForEvent('download', { timeout: 5000 }).catch(() => null)
  await page.locator('[data-audit-export=json]').click()
  const d2 = await dl2
  check('导出 JSON：请求 ?format=json，不筛就不带 groups', evRequests.some((u) => u === `/api/runs/${EV}/evidence/audit?format=json`),
    evRequests.join(' '))
  check('……下载了 .json', d2?.suggestedFilename()?.endsWith('.json'), d2?.suggestedFilename() ?? '没有下载')

  // 键盘：整张表一个 Tab 位，↑/↓ 逐行走完每一行，回车在右边打开
  const focusables = await page.locator('[data-audit-table] [data-audit-focus]').evaluateAll((els) => els.map((e) => e.tabIndex))
  check('表里只有一个 Tab 位（roving）', focusables.filter((t) => t === 0).length === 1 && focusables.length === 16, focusables.join(','))
  await page.locator('[data-audit-table] [data-audit-focus][tabindex="0"]').focus()
  const seen = [await page.evaluate(() => document.activeElement?.getAttribute('data-audit-focus'))]
  for (let i = 0; i < 20; i++) {
    await page.keyboard.press('ArrowDown')
    seen.push(await page.evaluate(() => document.activeElement?.getAttribute('data-audit-focus')))
  }
  const uniq = [...new Set(seen.filter(Boolean))]
  check('↓ 能逐行走完全部 16 行（包括没挂依据的结论句这种打不开的行）', uniq.length === 16, `${uniq.length} 行`)
  check('走到底停住，不首尾相接', seen[seen.length - 1] === seen[seen.length - 2])
  await page.keyboard.press('Home')
  const home = await page.evaluate(() => document.activeElement?.getAttribute('data-audit-focus'))
  await page.keyboard.press('End')
  const end = await page.evaluate(() => document.activeElement?.getAttribute('data-audit-focus'))
  check('Home / End 到第一行、最后一行', home === uniq[0] && end === uniq[uniq.length - 1], `${home} / ${end}`)
  // 找到「orders.week」那一行，回车打开
  const target = await page.locator('[data-audit-table] [data-audit-open]', { hasText: /^orders\.week$/ }).getAttribute('data-audit-focus')
  await page.keyboard.press('Home')
  for (let i = 0; i < 20; i++) {
    if (await page.evaluate(() => document.activeElement?.getAttribute('data-audit-focus')) === target) break
    await page.keyboard.press('ArrowDown')
  }
  await page.keyboard.press('Enter')
  await page.locator('[data-evidence-dock] [data-evidence-panel=dock] [data-ev-entity-type]').waitFor({ timeout: 5000 }).catch(() => {})
  check('回车在右边的常驻面板里打开这一段：实体步骤、字段类型', (await page.locator('[data-evidence-dock] [data-evidence-panel] h3').innerText().catch(() => '')) === 'orders.week'
    && (await page.locator('[data-evidence-dock] [data-ev-entity-type]').innerText().catch(() => '')).includes('VARCHAR'))
  check('打开之后焦点还在表里（接着往下走）', await page.evaluate(() => document.activeElement?.getAttribute('data-audit-focus')) === target)
  check('面板开着时空位提示收起', await page.locator('[data-evidence-dock] [data-evidence-dock-idle]').count() === 0)
  check('正文里对应的片段标着展开', await page.locator('[data-evidence-report=write] [data-seg][aria-expanded=true]').innerText().catch(() => '') === 'orders.week')
  // 点正文里的片段：面板跟着换
  await page.locator('[data-evidence-report=write] [data-seg]', { hasText: '退款金额以财务确认日为准' }).click()
  await page.locator('[data-evidence-dock] [data-ev-quote-hit]').waitFor({ timeout: 5000 }).catch(() => {})
  check('点正文里的引文：右边换成引文步骤，原文里高亮', (await page.locator('[data-evidence-dock] [data-ev-quote-hit]').innerText().catch(() => ''))
    .includes('退款金额以财务确认日为准'))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(250)
      await page.screenshot({ path: `${SHOTS}/runs-evidence-${theme}.png` })
      await page.locator('[data-evidence-audit]').scrollIntoViewIfNeeded()
      await page.waitForTimeout(150)
      await page.screenshot({ path: `${SHOTS}/runs-evidence-audit-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.removeAttribute('data-theme'))
  }
  await page.keyboard.press('Escape')
})

await section('证据页签：同一段文字里好几条同样的违规、正文里点不开的行', async () => {
  // 夹具同样是后端真跑的：粗体、链接里反引号写的可疑名字只记违规、不切片段——同一段文字上两条 unknown_entity、
  // 两条 unverified_entity（片段和 code 都相同），列表序号 100. 是结构片段上的裸数字。这几行正文里都点不开
  const dup = fxe.dup
  const EV_DUP = 'fake0evdupviol000000000000000'
  evFakes(EV_DUP, { output: dup.output, graph: dup.graph, audit: dup.audit })
  fakes.set(`GET /api/artifacts/${dup.doc_artifact}`, () => ({ status: 200, json: { id: dup.doc_artifact, content: dup.doc } }))
  await page.route(new RegExp(`/api/runs/${EV_DUP}/evidence/segments/`), (r) => {
    const sid = decodeURIComponent(new URL(r.request().url()).pathname.split('/').pop())
    return dup.segments[sid] ? r.fulfill({ json: dup.segments[sid] }) : r.fulfill({ status: 404, json: { detail: '没有这个片段' } })
  })
  const consoleErrors = []
  const onConsole = (m) => { if (m.type() === 'error') consoleErrors.push(m.text()) }
  page.on('console', onConsole)
  try {
    await openEvidence(EV_DUP)
    await page.locator('[data-audit-table]').waitFor({ timeout: 5000 }).catch(() => {})
    const texts = await page.locator('[data-audit-table] [data-audit-focus]').allInnerTexts()
    const keys = await page.locator('[data-audit-table] tr[data-audit-row]').evaluateAll((els) => els.map((e) => e.getAttribute('data-audit-row')))
    const total = dup.audit.groups.reduce((n, g) => n + g.rows.length, 0)
    check(`每条违规一行（${total} 行），同一片段同一种问题的几行键各不相同`, keys.length === total && new Set(keys).size === keys.length,
      keys.join(' '))
    check('React 没有报「两个子元素键相同」', !consoleErrors.some((t) => /same key/i.test(t)), consoleErrors.find((t) => /same key/i.test(t))?.slice(0, 120) ?? '')

    await page.locator('[data-audit-table] [data-audit-focus][tabindex="0"]').focus()
    const seen = [await page.evaluate(() => document.activeElement?.getAttribute('data-audit-focus'))]
    for (let i = 0; i < total + 2; i++) {
      await page.keyboard.press('ArrowDown')
      seen.push(await page.evaluate(() => document.activeElement?.getAttribute('data-audit-focus')))
    }
    const walked = [...new Set(seen.filter(Boolean))]
    check(`↓ 逐行走完全部 ${total} 行，不卡在同一片段的第一条违规上`, walked.length === total
      && seen.slice(0, total - 1).every((k, i) => k !== seen[i + 1]), `${walked.length} 行：${seen.join(' → ')}`)
    const reached = await page.evaluate((ks) => ks.map((k) => document.querySelector(`[data-audit-focus="${CSS.escape(k)}"]`)?.textContent), walked)
    check('……粗体、链接里的四个可疑名字都走得到', ['orders.coupon_id', 'orders.promo_code', 'campaign_tags', 'promo_rules'].every((n) => reached.includes(n)),
      reached.join('、'))

    // 点不开的行不画成按钮：只有有状态的片段（这里是那个有出处的数字）能在右边打开
    const openers = await page.locator('[data-audit-table] [data-audit-open]').evaluateAll((els) => els.map((e) => e.getAttribute('data-audit-open')))
    check('只有正文里点得开的片段画成按钮（文字片段、结构片段上的违规不是）', openers.join(',') === 's1', openers.join(',') || '一个都没有')
    const hidden = page.locator('[data-audit-table] tr[data-audit-row]:has([data-audit-hidden])')
    check('……点不开的五行都说清只在表里列出', await hidden.count() === 5
      && (await hidden.first().innerText()).includes('仅在此处列出'), String(await hidden.count()))
    const deadKey = keys.find((k, i) => texts[i] === 'orders.promo_code')
    await page.locator(`[data-audit-table] [data-audit-focus="${deadKey}"]`).focus()
    await page.keyboard.press('Enter')
    await page.waitForTimeout(300)
    check('……在点不开的行上按回车：右边不开一个空面板，也不报错', await page.locator('[data-evidence-dock] [data-evidence-dock-idle]').count() === 1)
    await page.locator('[data-audit-table] [data-audit-open="s1"]').click()
    await page.locator('[data-evidence-dock] [data-evidence-panel]').waitFor({ timeout: 5000 }).catch(() => {})
    check('点得开的那行：右边打开这一段', (await page.locator('[data-evidence-dock] [data-evidence-panel] h3').innerText().catch(() => '')) === '45,678.5')
    await page.keyboard.press('Escape')
  } finally {
    page.off('console', onConsole)
    for (const k of [...fakes.keys()]) if (k.includes(EV_DUP)) fakes.delete(k)
    fakes.delete(`GET /api/artifacts/${dup.doc_artifact}`)
  }
})

await section('证据页签：裁判拆档——证据相矛盾、证据不足的结论句各一行，点开是这一句的「模型的解释」', async () => {
  // 夹具是后端（JR-back 之后）真算的：审计行由 api/evidence.py 的 _audit_doc 给出，判定行 issue 为
  // contradicted_claim / insufficient_claim，带着 unit 和 verdict
  const fxv = JSON.parse(readFileSync(new URL('../frontend/src/run/__tests__/evidence-verdict.json', import.meta.url), 'utf8'))
  const v = fxv.formal
  const EV_V = 'fake0evverdict000000000000000'
  const output = { answer: v.doc.markdown, _evidence: { report_node: 'write', doc_artifact: v.doc_artifact, fields: ['answer'] } }
  evFakes(EV_V, { output, graph: v.graph, audit: v.audit })
  fakes.set(`GET /api/artifacts/${v.doc_artifact}`, () => ({ status: 200, json: { id: v.doc_artifact, content: v.doc } }))
  for (const snap of Object.values(fxv.snapshots)) {
    fakes.set(`GET /api/artifacts/${snap.artifact}`, () => ({ status: 200, json: { id: snap.artifact, content: snap.content } }))
  }
  await page.route(new RegExp(`/api/runs/${EV_V}/evidence/segments/`), (r) => {
    const sid = decodeURIComponent(new URL(r.request().url()).pathname.split('/').pop())
    return v.segments[sid] ? r.fulfill({ json: v.segments[sid] }) : r.fulfill({ status: 404, json: { detail: '没有这个片段' } })
  })
  try {
    await openEvidence(EV_V)
    await page.locator('[data-audit-table]').waitFor({ timeout: 5000 }).catch(() => {})
    const claimRows = await page.locator('[data-audit-table] tbody[data-audit-group=none] tr[data-audit-row]').evaluateAll((els) =>
      els.map((e) => ({ state: e.getAttribute('data-audit-state'), open: e.querySelector('[data-audit-open]')?.getAttribute('data-audit-open') ?? null,
                        stateText: e.children[1]?.textContent ?? '', source: e.querySelector('[data-audit-source]')?.textContent ?? '' })))
    const byState = (st) => claimRows.filter((r) => r.state === st)
    check('审计表：证据相矛盾的结论句两行、证据不足一行，列在「无证据」一组', byState('contradicted').length === 2 && byState('insufficient').length === 1,
      JSON.stringify(claimRows.map((r) => r.state)))
    check('……状态照句末徽标的字形和叫法写', byState('contradicted').every((r) => r.stateText === '!模型判断：证据相矛盾')
      && byState('insufficient')[0]?.stateText === '○模型判断：证据不足', JSON.stringify(claimRows.map((r) => r.stateText)))
    const note = v.audit.groups.flatMap((g) => g.rows).find((r) => r.issue === 'insufficient_claim')?.note ?? '（夹具里没有这一行）'
    check('……原因照后端的原话（缺什么、理由）', byState('insufficient')[0]?.source === note && note.includes(`缺少：${fxv.missing}`), note)
    check('……判定行点得开，打开的是这一句的句末徽标（claim:<句子>）', byState('insufficient')[0]?.open === `claim:${v.units.insuff}`
      && byState('contradicted').map((r) => r.open).join(',') === [v.units.contra, v.units.method].map((u) => `claim:${u}`).join(','),
    JSON.stringify(claimRows.map((r) => r.open)))
    await page.locator(`[data-audit-table] [data-audit-open="claim:${v.units.insuff}"]`).click()
    await page.locator('[data-evidence-dock] [data-evidence-panel] [data-ev-judge="insufficient"]').waitFor({ timeout: 5000 }).catch(() => {})
    const dock = page.locator('[data-evidence-dock] [data-evidence-panel]')
    check('点开证据不足那一行：右边打开这一句的「模型的解释」，写明缺什么', (await dock.locator('[data-ev-missing]').innerText().catch(() => ''))
      === `缺少：${fxv.missing}`)
    check('……裁判模型和写作模型相同、不在价格表里：两条提醒都在', await dock.locator('[data-ev-judge-same]').count() === 1
      && await dock.locator('[data-ev-judge-unpriced]').count() === 1)
    const tally = await page.locator(`[data-evidence-report=write] [data-evidence-claims]`).innerText().catch(() => '')
    check('报告上方的证据条：结论句计数带上证据相矛盾、证据不足', tally.includes('结论 6 句（有依据 1 · 部分有依据 1 · 证据相矛盾 2 · 证据不足 1 · 未裁判 1）'), tally)
    await page.keyboard.press('Escape')
  } finally {
    for (const k of [...fakes.keys()]) if (k.includes(EV_V) || k.includes(v.doc_artifact)) fakes.delete(k)
    for (const snap of Object.values(fxv.snapshots)) fakes.delete(`GET /api/artifacts/${snap.artifact}`)
  }
})

await section('证据页签：没有报告的运行照实说明（none / 旧运行猜测 / 老后端）', async () => {
  evFakes(EV_LEGACY, { output: fxe.legacy.output, graph: fxe.legacy.graph, audit: fxe.legacy.audit, report: false })
  await openEvidence(EV_LEGACY)
  check('没有契约的旧运行：mode 是 legacy_text，照实说', await page.locator('[data-evidence-pane=legacy_text]').count() === 1
    && (await page.locator('[data-evidence-mode-note=legacy_text]').innerText()).includes('没有出具契约'))
  check('猜测默认收起', await page.locator('[data-evidence-legacy-text] [data-ev-guess]').getAttribute('data-open') === 'false')
  check('审计表里「按数值猜测」那组默认收起', await page.locator('tbody[data-audit-group=candidate]').getAttribute('data-audit-collapsed') === '1'
    && await page.locator('tbody[data-audit-group=candidate] tr[data-audit-row]').count() === 0)
  await page.locator('[data-evidence-legacy-text] [data-ev-guess-toggle]').click()
  check('展开猜测写明「猜测的来源，不能当证据」', (await page.locator('[data-evidence-legacy-text] [data-ev-guess-note]').innerText().catch(() => ''))
    .includes('猜测的来源，不能当证据'))
  await page.locator('[data-audit-group-toggle=candidate]').click()
  check('展开那一组：有候选的 4 个数字，线型是候选那一档', await page.locator('tbody[data-audit-group=candidate] tr[data-audit-row]').count() === 4
    && (await page.locator('tbody[data-audit-group=candidate] tr[data-audit-row]').evaluateAll((els) => els.every((e) => e.getAttribute('data-audit-state') === 'candidate'))))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(200)
      await page.screenshot({ path: `${SHOTS}/runs-evidence-legacy-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.removeAttribute('data-theme'))
  }

  evFakes(EV_NONE, { output: { answer: '这次没有数字' }, report: false,
    graph: { schema: 'agentlab.evidence/1', mode: 'none', note: '本次运行没有报告文档，也没有出具契约，没有可展示的证据', seal: { sealed: true, ok: true },
             reports: [], evidence: [], edges: [] },
    audit: { schema: 'agentlab.evidence.audit/1', mode: 'none', seal: { sealed: true, ok: true }, reports: [], groups: [], counts: {}, total: 0 } })
  await openEvidence(EV_NONE)
  check('mode 为 none：照实说没有证据', await page.locator('[data-evidence-pane=none] [data-evidence-mode-note=none]').count() === 1
    && (await page.locator('[data-evidence-mode-note=none]').innerText()).includes('没有'))

  // 老后端：审计接口不存在（404 没有机读码）——清单按页面上的报告自己拼，导出也按页面上的
  evFakes(EV_OLD, { output: fxe.output, graph: fxe.graph, audit: null })
  await openEvidence(EV_OLD)
  await page.locator('[data-audit-fallback]').waitFor({ timeout: 5000 }).catch(() => {})
  check('老后端没有审计接口：照实说，清单按页面上的报告拼', await page.locator('[data-audit-fallback=unsupported]').count() === 1
    && await auditRowsShown() > 10, String(await auditRowsShown()))
  const dl = page.waitForEvent('download', { timeout: 5000 }).catch(() => null)
  await page.locator('[data-audit-export=csv]').click()
  const d = await dl
  check('……导出退回按页面上的清单，也照实说', !!d && (await page.locator('[data-toast], [role=status], [role=alert]').allInnerTexts()).join(' ').includes('当前服务版本不支持导出'))
  for (const id of [EV, EV_LEGACY, EV_NONE, EV_OLD]) {
    for (const k of [...fakes.keys()]) if (k.includes(id)) fakes.delete(k)
  }
  fakes.delete(`GET /api/artifacts/${fxe.doc_artifact}`)
})

// ------------------------------------------------------------------ 没有逐段证据 → 升级这张图（可点击证据五期）

await section('证据页签：没有逐段证据的运行给升级横幅，跳到编排页打开升级预览', async () => {
  const UP = 'fake0evupgrade000000000000000'
  const UP_OLD = 'fake0evupgradeold00000000000'
  const UP_LEGACY = 'fake0evupgradelegacy00000000'
  const UP_NOWF = 'fake0evupgradenowf0000000000'
  const node = (id, type, x, label, config = {}) => ({ id, type, position: { x, y: 120 }, data: { label, config } })
  const wfOf = (id, name, graph) => ({ id, name, description: '检查脚本伪造的工作流', graph, tags: [], version: 5, is_template: false,
    status: 'published', published_version: 5, published_by: null, run_count: 1,
    created_at: '2026-09-20T02:00:00Z', updated_at: '2026-09-26T02:00:00Z' })
  // 旧结构：模型调用写的文字进了成果（可以升级）；另一张已经是报告撰写（校验不给建议）
  const legacyGraph = { nodes: [node('in', 'input', 0, '入口'), node('fetch', 'agent', 240, '取数', { tools: ['db_query__shop'] }),
    node('story', 'llm', 480, '写周报', { prompt: '写周报' }), node('done', 'output', 720, '成果', { fields: [{ name: 'report', value: '{{ nodes.story.text }}' }] })],
  edges: [{ id: 'e1', source: 'in', target: 'fetch' }, { id: 'e2', source: 'fetch', target: 'story' }, { id: 'e3', source: 'story', target: 'done' }] }
  const cleanGraph = { ...legacyGraph, nodes: legacyGraph.nodes.map((n) => (n.id === 'story' ? { ...n, type: 'report', data: { label: '写周报', config: { instructions: '写周报' } } } : n)) }
  const WF = wfOf('fake-wf-upgrade', '升级检查', legacyGraph)
  const WF_CLEAN = wfOf('fake-wf-upgraded', '升级检查（已升级）', cleanGraph)
  const real = await getJson('/workflows').catch(() => [])
  fakes.set('GET /api/workflows', () => ({ status: 200, json: [...real, WF, WF_CLEAN] }))
  fakes.set(`GET /api/workflows/${WF.id}`, () => ({ status: 200, json: WF }))
  fakes.set('GET /api/conversations', () => ({ status: 200, json: [{ id: 'fake-conv-upgrade', title: '', kind: 'canvas', archived: false, turn_count: 0 }] }))
  const validated = []
  fakes.set('POST /api/workflows/validate', (url, r) => {
    const graph = r.postDataJSON()?.graph ?? { nodes: [] }
    validated.push(graph.nodes.map((n) => n.id).join(','))
    const old = graph.nodes.some((n) => n.type === 'llm')
    return { status: 200, json: { ok: true, issues: old ? [{ level: 'info', code: 'evidence.upgrade_available', node_id: null, field: null, message: '可以升级为可追溯结构' }] : [] } }
  })
  fakes.set('POST /api/workflows/variables', () => ({ status: 200, json: { variables: [], issues: [] } }))
  const upgrades = []
  fakes.set('POST /api/copilot/upgrade-evidence', (url, r) => {
    const body = r.postDataJSON()
    upgrades.push(body)
    const graph = JSON.parse(JSON.stringify(body.graph))
    const story = graph.nodes.find((n) => n.id === 'story')
    if (story) { story.type = 'report'; story.data.config = { instructions: '写周报' } }
    return { status: 200, json: { graph, ops: [], notes: [], issues: [], applied: ['R2:story'], rejected: [], assist: null, ok: true,
      changes: [{ fix_id: 'R2:story', rule: 'R2', label: '把「写周报」换成报告撰写', node_id: 'story', node_title: '写周报', field: 'type', before: 'llm', after: 'report' }] } }
  })
  const none = { schema: 'agentlab.evidence/1', mode: 'none', note: '', seal: { sealed: true, ok: true }, reports: [], evidence: [], edges: [] }
  const noneAudit = { schema: 'agentlab.evidence.audit/1', mode: 'none', seal: { sealed: true, ok: true }, reports: [], groups: [], counts: {}, total: 0 }
  const wire = (id, wf, graph) => {
    evFakes(id, { output: { report: '本周销售额 45678' }, report: false, graph, audit: { ...noneAudit, mode: graph.mode } })
    fakes.set(`GET /api/runs/${id}`, () => ({ status: 200, json: { ...evRun(id, { report: '本周销售额 45678' }), workflow_id: wf.id, workflow_name: wf.name } }))
  }
  wire(UP, WF, none)
  wire(UP_OLD, WF_CLEAN, none)
  wire(UP_LEGACY, WF, { ...none, mode: 'legacy_text', legacy: { note: '猜测', fields: [] } })
  evFakes(UP_NOWF, { output: { report: '本周销售额 45678' }, report: false, graph: none, audit: noneAudit })
  const banner = () => page.locator('[data-view-pane=evidence] [data-upgrade-banner]')

  await openEvidence(UP)
  await banner().waitFor({ timeout: 5000 }).catch(() => {})
  const text = await banner().innerText().catch(() => '')
  check('没有逐段证据（none）、图能升级：横幅「本次报告没有逐段证据 → 升级此工作流」', text.includes('本次报告没有逐段证据')
    && (await banner().locator('[data-upgrade-link]').innerText().catch(() => '')).trim() === '升级此工作流', text)
  check('……拿工作流现在的图去校验、按那条建议认', validated.includes('in,fetch,story,done'), validated.join(' | '))
  check('……正式运行：说清升级之后要重新发布', text.includes('需要重新发布'), text)
  const href = await banner().locator('[data-upgrade-link]').getAttribute('href').catch(() => '')
  check('跳转地址：编排页，带 upgrade=1', href === `/studio/${WF.id}?upgrade=1`, href)
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(200)
      await page.screenshot({ path: `${SHOTS}/runs-upgrade-banner-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.removeAttribute('data-theme'))
  }
  // 画布正锁着这张工作流（正式运行在跑）：不给入口，说清为什么
  await page.evaluate((wf) => {
    const s = window.__studio.getState()
    window.__studio.setState({ workflow: wf, runPhase: 'running', trace: { ...s.trace, runClass: 'formal' } })
  }, WF)
  await page.waitForTimeout(200)
  check('画布锁着这张工作流：横幅不给跳转，说清为什么', await banner().locator('[data-upgrade-link]').count() === 0
    && (await banner().locator('[data-upgrade-banner-locked]').innerText().catch(() => '')).includes('正式运行进行中'))

  await openEvidence(UP_LEGACY)
  await banner().waitFor({ timeout: 5000 }).catch(() => {})
  check('旧运行猜测（legacy_*）也给横幅', await banner().count() === 1)

  const before = validated.length
  await openEvidence(UP_OLD)
  await page.waitForTimeout(500)
  check('图已经是可追溯结构（校验不给建议，老后端同样不给）：没有横幅', await banner().count() === 0 && validated.length === before + 1)

  await openEvidence(UP_NOWF)
  await page.waitForTimeout(400)
  check('没有工作流的运行（未保存的图）：没有横幅，也不去校验', await banner().count() === 0 && validated.length === before + 1)

  await openEvidence(UP)
  await banner().locator('[data-upgrade-link]').click()
  await page.waitForURL((u) => u.pathname === `/studio/${WF.id}`, { timeout: 8000 }).catch(() => {})
  await page.locator('#dock-problems [data-upgrade-preview="ready"]').waitFor({ timeout: 8000 }).catch(() => {})
  check('跳到编排页：打开问题面板、升级预览摆着', await page.locator('#dock-problems [data-upgrade-preview="ready"]').count() === 1
    && (await page.locator('#dock-problems [data-upgrade-change="type"]').innerText().catch(() => '')).includes('报告撰写'))
  check('……请求的是这张工作流的图，地址摘掉了 upgrade', upgrades.length === 1 && upgrades[0]?.graph?.nodes?.length === 4
    && new URL(page.url()).search === '', `${upgrades.length} 次 · ${page.url()}`)
  check('……只出预览，没保存', !writes.some((w) => w.startsWith('PATCH ')))

  for (const k of [...fakes.keys()]) {
    if ([UP, UP_OLD, UP_LEGACY, UP_NOWF].some((id) => k.includes(id)) || k.includes(WF.id)) fakes.delete(k)
  }
  for (const k of ['GET /api/workflows', 'GET /api/conversations', 'POST /api/workflows/validate', 'POST /api/workflows/variables',
    'POST /api/copilot/upgrade-evidence']) fakes.delete(k)
})

await section('收尾', async () => {
  // 提取模板之后会跳进画布：画布一打开就拿图去做校验、变量分析，这两个是带图的只读 POST
  // （同样被探针拦下，不落库）。赶上它们发出来之前就离开了画布的话就没有
  check('只有伪造过的写请求', writes.every((w) => /\/continue$|\/cancel$|\/copilot\/from-run$|DELETE \/api\/runs\/fake0|^POST \/api\/runs$|^POST \/api\/workflows\/(validate|variables)$|^POST \/api\/copilot\/upgrade-evidence$/.test(w)),
    writes.join(', '))
  check('没有未捕获的运行时错误', errors.length === 0, errors[0] ?? '')
})

await browser.close()
console.log(failed ? `\n${failed} 项没过` : '\n全部通过')
process.exit(failed ? 1 : 0)
