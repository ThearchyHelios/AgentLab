// 个人应用的使用面（docs/design/normal-user-apps.md N7、§4.6）的回归检查。
//
// 守的是这些「看着没坏、其实不对」的地方：
//   - 使用者建好的个人应用出现在应用首页单独一组「我的应用」，卡片角标写「个人」、不写「未发布」；共享的应用另列一组；
//   - 个人应用的页上写明是个人应用（只有自己看得见、遮罩、探索运行未经发布审核），印章的来源照服务端写的
//     「探索 · 未经发布审核」，不另编；没有补算面板；
//   - 「刷新」写明今天还剩几次（服务端在详情和刷新回包里给）：点一次发 POST /refresh，剩余跟着回包变；用完了按钮置灰、
//     写「今天的刷新次数已用完」；服务端回 429 时照实说服务端的原话，不当成网络错误；
//   - 「提交审核」：没选人不让提交；选一位搭建者或一个组、附一句说明，POST /submit-review 带 {to, note}；提交后写
//     「审核中」和交给了谁，不再给提交按钮；
//   - 审核状态以服务端为准（详情的 personal，形状同服务端 blueprints/personal.personal_state）：审核待办还在等写「审核中」；
//     结论是已发布写「已有正式版本：去看看」并链到那个应用；
//     详情没给时看蓝图的状态（in_review 写「审核中」）。
// 使用者在蓝图里「存成我的应用」那一步在 check-blueprint 里查。
//
// 页面是真页面（走真的应用外壳），/api/apps/**、/api/blueprints/** 在浏览器层答掉，数据是这里虚构的（「我的退款周报」）；
// 其余 GET 放行到服务端（外壳要它），写请求一律拦下、记在探针里，不碰真实写接口。
//
//   node scripts/check-personal.mjs
// 对别的实例跑时带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-personal.mjs
// CHECK_ONLY=刷新,审核 只跑段名里含这些字的段。
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}
const ONLY = (process.env.CHECK_ONLY ?? '').split(',').map((s) => s.trim()).filter(Boolean)
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

const ago = (ms) => new Date(Date.now() - ms).toISOString()
const USER = { mode: 'local', username: 'zhangsan', display_name: '张三', role: 'user', groups: ['门店'], verified: true }

// ---------------------------------------------------------------- 假数据

const MINE = {
  id: 'app_mine', name: '我的退款周报', description: '每周看自己门店的退款', icon: 'app', status: 'active',
  published_version: null, level: null, needs_upgrade: false, pending_tasks: 0, my_role: 'use', updated_at: ago(600_000),
  scope: 'personal', owner: 'zhangsan', blueprint_id: 'bp_mine',
}
const SHARED = {
  id: 'app_weekly', name: '门店周报', description: '每周一看各门店的销售概况', icon: 'app', status: 'active',
  published_version: 3, level: 'published', needs_upgrade: false, pending_tasks: 0, my_role: 'use', updated_at: ago(3600_000),
  scope: 'shared', owner: null, blueprint_id: null,
}
const STAMP = {
  as_of: ago(3600_000), as_of_text: '截至 2026-10-14 09:05；统计期 上一 ISO 周', pin: 'mine', graph_hash: '9c1e22aa',
  calibers: [], tier: 'formal', tier_text: '完整出具', from: '探索 · 未经发布审核', sealed: true, notes: [],
}
const HOME = {
  format: 'agentlab.view/1',
  app: { id: 'app_mine', name: '我的退款周报', version: 0, level: 'published', needs_upgrade: false },
  page: { id: 'home', title: '本周退款' },
  pages: [{ id: 'home', title: '本周退款' }],
  sources: { main: { kind: 'exploratory', state: 'ok', run_id: 'run_p1', stamp: STAMP } },
  tree: [
    { id: 'stamp', type: 'stamp', props: {}, data: { view: 'main', state: 'ok', stamp: STAMP } },
    { id: 'refund', type: 'metric', props: { label: '退款率' }, data: { state: 'ok', text: '3.2%', name: '退款率' } },
  ],
  forms: {},
  problems: [],
}
const detailOf = (personal) => ({ ...MINE, published: null, ...(personal === undefined ? {} : { personal }) })

// ---------------------------------------------------------------- 浏览器

const browser = await chromium.launch({ executablePath: CHROME })

async function open({ state, width = 1280, height = 900 }) {
  const ctx = await browser.newContext({ viewport: { width, height } })
  opened.add(ctx)
  const page = await ctx.newPage()
  page.errors = []
  page.on('pageerror', (e) => page.errors.push(e.message))
  await page.route((url) => url.pathname.startsWith('/api/'), (route) => {
    const r = route.request()
    const url = new URL(r.url())
    const p = url.pathname
    if (r.method() !== 'GET') {
      state.writes.push({ method: r.method(), path: p, body: r.postData() })
      if (p === '/api/apps/app_mine/refresh') {
        const next = state.refreshes.shift()
        if (!next) return route.fulfill({ status: 503, json: { detail: '检查脚本没有准备更多的刷新回包' } })
        return next.status ? route.fulfill({ status: next.status, json: next.json }) : route.fulfill({ json: next })
      }
      if (p === '/api/apps/app_mine/submit-review') return route.fulfill({ json: { task_id: 'task_review_1' } })
      return route.fulfill({ status: 503, json: { detail: '检查脚本拦下了写请求' } })
    }
    if (p === '/api/auth/me') return route.fulfill({ json: state.me ?? USER })
    if (p === '/api/tasks/count') return route.fulfill({ json: { mine: 0, apps: 0, other: 0 } })
    if (p === '/api/tasks') return route.fulfill({ json: { items: [] } })
    if (p === '/api/apps') return route.fulfill({ json: { items: [MINE, SHARED] } })
    if (p === '/api/apps/app_mine') return route.fulfill({ json: state.detail })
    if (p === '/api/apps/app_mine/pages/home') {
      state.pageHits = (state.pageHits ?? 0) + 1
      return route.fulfill({ json: HOME })
    }
    if (/^\/api\/apps\/[^/]+\/instances$/.test(p)) return route.fulfill({ json: { items: [] } })
    if (p === '/api/blueprints/bp_mine') {
      state.blueprintHits = (state.blueprintHits ?? 0) + 1
      return route.fulfill({ json: { id: 'bp_mine', name: '我的退款周报', status: state.blueprintStatus ?? 'personal', scope: 'personal' } })
    }
    return route.continue()
  })
  return { ctx, page }
}

const bar = (page) => page.locator('[data-personal-bar]')

// ---------------------------------------------------------------- 首页

await section('首页：「我的应用」单独一组，角标「个人」', async () => {
  const state = { writes: [], refreshes: [], detail: detailOf({ daily_limit: 5, remaining_today: 2, review: null }) }
  const { page } = await open({ state })
  await page.goto(`${WEB}/apps?tab=apps`)
  const group = page.locator('[data-app-group="personal"]')
  check('建好的个人应用出现在「我的应用」一组', await group.waitFor({ timeout: 15000 }).then(() => true, () => false)
    && (await group.locator('h2').innerText()) === '我的应用' && await group.locator('[data-app="app_mine"]').count() === 1)
  const card = group.locator('[data-app="app_mine"]')
  check('卡片角标写「个人」', (await card.locator('[data-app-personal]').innerText()) === '个人')
  check('……不写「未发布」（个人应用没有应用版本）', !(await card.innerText()).includes('未发布'), (await card.innerText()).replace(/\s+/g, ' '))
  check('……没有「编辑」链接', await card.locator('a[href$="/edit"]').count() === 0)
  check('共享的应用另列一组，不混进「我的应用」', await page.locator('[data-app-group="shared"] [data-app="app_weekly"]').count() === 1
    && await group.locator('[data-app="app_weekly"]').count() === 0)
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

// ---------------------------------------------------------------- 应用页

await section('应用页：写明是个人应用、印章照服务端写、刷新写剩余次数', async () => {
  const state = {
    writes: [], detail: detailOf({ daily_limit: 5, remaining_today: 2, review: null }),
    refreshes: [{ run_id: 'run_p2', remaining: 1 }, { run_id: 'run_p3', remaining: 0 }],
  }
  const { page } = await open({ state })
  await page.goto(`${WEB}/apps/app_mine`)
  await bar(page).waitFor({ timeout: 15000 })
  check('页上写明：只有你看得见、未经发布审核', /只有你看得见/.test(await page.locator('[data-personal-banner]').innerText())
    && /未经发布审核/.test(await page.locator('[data-personal-banner]').innerText()))
  check('页头角标写「个人」', (await page.locator('header [data-app-personal]').innerText()) === '个人')
  check('印章的来源照服务端写「探索 · 未经发布审核」', (await page.locator('[data-stamp-row="from"]').innerText()).includes('探索 · 未经发布审核'))
  check('个人应用没有「编辑」（使用者没有管理权）', await page.locator('[data-page-edit-start]').count() === 0)
  const left = page.locator('[data-personal-remaining]')
  check('「刷新」旁写明今天还剩几次（服务端给的）', (await left.innerText()) === '今天还剩 2 次', await left.innerText())

  const hits = state.pageHits ?? 0
  await page.locator('[data-personal-refresh]').click()
  await page.waitForFunction(() => document.querySelector('[data-personal-remaining]')?.getAttribute('data-personal-remaining') === '1',
    null, { timeout: 5000 }).catch(() => {})
  check('点「刷新」发 POST /refresh', state.writes.filter((w) => w.path === '/api/apps/app_mine/refresh').length === 1)
  check('……剩余次数按回包变成 1', (await left.innerText()) === '今天还剩 1 次', await left.innerText())
  check('……刷新后重取页面', (state.pageHits ?? 0) > hits, `${hits} → ${state.pageHits ?? 0}`)
  await page.locator('[data-personal-refresh]').click()
  await page.waitForFunction(() => document.querySelector('[data-personal-remaining]')?.getAttribute('data-personal-remaining') === '0',
    null, { timeout: 5000 }).catch(() => {})
  check('用完了：写「今天的刷新次数已用完」', (await left.innerText()).includes('今天的刷新次数已用完'), await left.innerText())
  check('……按钮置灰，不再发', await page.locator('[data-personal-refresh]').isDisabled())
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

await section('刷新：服务端回 429 时照实说原话、按钮置灰；不知道剩几次时只说有上限', async () => {
  const state = {
    writes: [], detail: detailOf(undefined),
    refreshes: [{ status: 429, json: { detail: '今天的刷新次数已用完（每天 5 次）', code: 'personal_refresh_limit' } }],
  }
  const { page } = await open({ state })
  await page.goto(`${WEB}/apps/app_mine`)
  await bar(page).waitFor({ timeout: 15000 })
  check('详情没给剩余次数：不猜，只说有上限', (await page.locator('[data-personal-remaining]').innerText()).includes('有上限'))
  await page.locator('[data-personal-refresh]').click()
  const toast = page.getByText('今天的刷新次数已用完（每天 5 次）')
  check('429：照实写服务端的原话', await toast.first().waitFor({ timeout: 5000 }).then(() => true, () => false))
  check('……按钮置灰', await page.locator('[data-personal-refresh]').isDisabled())
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

// ---------------------------------------------------------------- 提交审核

await section('提交审核：选人才能交、带说明；提交后写「审核中」', async () => {
  const state = { writes: [], refreshes: [], detail: detailOf({ daily_limit: 5, remaining_today: 5, review: null }) }
  const { page } = await open({ state })
  await page.goto(`${WEB}/apps/app_mine`)
  await bar(page).waitFor({ timeout: 15000 })
  await page.locator('[data-personal-submit-review]').click()
  const dlg = page.getByRole('dialog')
  await dlg.locator('[data-personal-review-dialog]').waitFor({ timeout: 5000 })
  check('写明搭建者在副本上发布、个人应用不会被改动', /副本/.test(await dlg.innerText()) && /不会被改动/.test(await dlg.innerText()))
  check('没选人不让提交', await dlg.locator('[data-personal-review-submit]').isDisabled())
  await dlg.getByRole('textbox', { name: '交给谁审核' }).fill('lisi')
  await dlg.getByRole('button', { name: '添加' }).click()
  await dlg.locator('#personal-review-note').fill('门店店长都想看')
  await dlg.locator('[data-personal-review-submit]').click()
  await page.locator('[data-personal-review="in_review"]').waitFor({ timeout: 5000 }).catch(() => {})
  const sent = state.writes.find((w) => w.path === '/api/apps/app_mine/submit-review')
  const body = sent ? JSON.parse(sent.body || '{}') : {}
  check('POST /submit-review 带 {to, note}', JSON.stringify(body.to) === JSON.stringify([{ kind: 'user', name: 'lisi' }])
    && body.note === '门店店长都想看', JSON.stringify(body))
  const status = page.locator('[data-personal-review="in_review"]')
  check('提交后写「审核中」和交给了谁', /审核中/.test(await status.innerText()) && /lisi/.test(await status.innerText()),
    await status.innerText().catch(() => ''))
  check('……不再给「提交审核」', await page.locator('[data-personal-submit-review]').count() === 0)
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

await section('审核状态：以服务端为准（审核中、已有正式版本、详情没给时看蓝图）', async () => {
  const a = { writes: [], refreshes: [], detail: detailOf({ remaining_today: 3, review: { task_id: 't1', status: 'pending', to: [{ kind: 'group', name: '搭建组' }] } }) }
  const one = await open({ state: a })
  await one.page.goto(`${WEB}/apps/app_mine`)
  await bar(one.page).waitFor({ timeout: 15000 })
  check('详情说审核中：写「审核中」和交给的组', /已提交给 搭建组 审核/.test(await one.page.locator('[data-personal-review="in_review"]').innerText()))
  check('……没有「提交审核」', await one.page.locator('[data-personal-submit-review]').count() === 0)

  const b = { writes: [], refreshes: [], detail: detailOf({ remaining_today: 3,
    review: { task_id: 't1', status: 'resolved', decision: 'published', app_id: 'app_refund' } }) }
  const two = await open({ state: b })
  await two.page.goto(`${WEB}/apps/app_mine`)
  await bar(two.page).waitFor({ timeout: 15000 })
  const pub = two.page.locator('[data-personal-review="published"]')
  check('审核发布后写「已有正式版本」', /已有正式版本/.test(await pub.innerText()), await pub.innerText().catch(() => ''))
  check('……「去看看」链到正式应用', await pub.locator('[data-personal-formal]').getAttribute('href') === '/apps/app_refund')

  const c = { writes: [], refreshes: [], detail: detailOf(undefined), blueprintStatus: 'in_review' }
  const three = await open({ state: c })
  await three.page.goto(`${WEB}/apps/app_mine`)
  await bar(three.page).waitFor({ timeout: 15000 })
  await three.page.locator('[data-personal-review="in_review"]').waitFor({ timeout: 5000 }).catch(() => {})
  check('详情没给审核状态：看蓝图（in_review 写「审核中」）', (c.blueprintHits ?? 0) >= 1
    && await three.page.locator('[data-personal-review="in_review"]').count() === 1)
  check('没有未捕获的运行时错误', [one, two, three].every((x) => x.page.errors.length === 0),
    [one, two, three].map((x) => x.page.errors[0]).filter(Boolean).join(' | '))
})

await section('窄屏：390px 不出横向滚动', async () => {
  const state = { writes: [], refreshes: [], detail: detailOf({ remaining_today: 2, review: null }) }
  const { page } = await open({ state, width: 390, height: 844 })
  for (const path of ['/apps?tab=apps', '/apps/app_mine']) {
    await page.goto(`${WEB}${path}`)
    await page.waitForTimeout(800)
    check(`${path} 没有横向滚动`, await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
  }
})

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 全部通过')
process.exit(failed ? 1 : 0)
