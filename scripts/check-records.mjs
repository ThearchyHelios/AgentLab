// 记录列表 records_list（docs/design/normal-user-apps.md N8、§3.7、§4.7）的回归检查。
//
// 守的是这些「看着没坏、其实不对」的地方：
//   - 行级可见在服务端做，界面只照服务端说的写：使用者（mine_only）的标题是「我的记录」、写明只显示自己提交的、不列提交人；
//     查看全部的人标题是「全部记录」、每行写提交人；界面不自己按人过滤（服务端交来几行就列几行）；
//   - 数据取自 GET /api/apps/{id}/records（带 table），每次打开实时取，不走封存；服务端解析页面时给了第一页（node.data）
//     的照它画、不再取一遍；
//   - 按时间排序（序号就是提交先后），默认最新的在前，可以换成最早的在前；按内容或提交人筛选，写明筛出几条；
//   - 每行可以展开看全部字段，附件是可下载的链接（/api/files/{id}）；
//   - 更正不改原行：更正过的行标「已更正」，展开能看更正历史（带 history=true 再取一次，顺着指向往前找），原来那条的
//     值照原样写；
//   - 没写记录表的组件取这个应用的第一张记录表（同服务端），照样列出来；服务端说这个应用还没有记录表（state=empty）的
//     照实说，不报错。
//
// 页面是真页面（走真的应用外壳），/api/apps/** 在浏览器层答掉，数据是这里虚构的（「门店报损」）；其余 GET 放行到服务端
// （外壳要它），写请求一律拦下、记在探针里，不碰真实写接口。
//
//   node scripts/check-records.mjs
// 对别的实例跑时带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-records.mjs
// CHECK_ONLY=我的,更正 只跑段名里含这些字的段。
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

const at = (h) => new Date(Date.UTC(2026, 9, 12, h)).toISOString()
const USER = { mode: 'local', username: 'zhangsan', display_name: '张三', role: 'user', groups: ['门店'], verified: true }
const LEAD = { mode: 'local', username: 'wangwu', display_name: '王五', role: 'builder', groups: ['区域'], verified: true }

// ---------------------------------------------------------------- 假数据

/** 服务端解析页面时给的第一页里的列（和接口给的一样，单独写一份免得引用顺序出错） */
const COLUMNS_FIRST = [{ name: 'store', label: '门店', type: 'string' }]
const APP = {
  id: 'app_loss', name: '门店报损', description: '门店每日报损登记', icon: 'app', status: 'active', published_version: 2,
  level: 'published', needs_upgrade: false, pending_tasks: 0, my_role: 'use', updated_at: at(1), scope: 'shared',
}
const HOME = {
  format: 'agentlab.view/1',
  app: { id: 'app_loss', name: '门店报损', version: 2, level: 'published', needs_upgrade: false },
  page: { id: 'home', title: '报损' },
  pages: [{ id: 'home', title: '报损' }],
  sources: {},
  tree: [
    { id: 'my_records', type: 'records_list', props: { table: 'loss', title: '报损登记' } },
    { id: 'no_table', type: 'records_list', props: {} },
    { id: 'server_page', type: 'records_list', props: { who: 'mine' },
      data: { table: 'loss', version: 1, columns: COLUMNS_FIRST, mine_only: true, items: [], next_cursor: null, state: 'ok' } },
    { id: 'empty_page', type: 'records_list', props: {},
      data: { table: null, version: 0, columns: [], items: [], mine_only: true, state: 'empty', next_cursor: null } },
    // 按草稿预览（服务端 apps/resolve._records_list）：不列真实的记录
    { id: 'preview_page', type: 'records_list', props: {},
      data: { table: null, version: 0, columns: [], items: [], mine_only: false, state: 'preview', next_cursor: null } },
  ],
  forms: {},
  problems: [],
}
const COLUMNS = [
  { name: 'store', label: '门店', type: 'string' },
  { name: 'amount', label: '金额（元）', type: 'number' },
  { name: 'reason', label: '原因', type: 'string' },
  { name: 'photo', label: '照片', type: 'file' },
]
const PHOTO = { file_id: 'f_photo', name: '破损.png', size: 20480, sha256: 'ab'.repeat(32), type: 'image/png' }
// 一行的形状同服务端 records/listing.py：系统列（records.SYSTEM_COLUMNS）加上这一行在第几版表里（_version）
const row = (id, seq, by, values, extra = {}) => ({
  _id: id, _seq: seq, _submitted_by: by, _submitted_at: at(seq), _run_id: `run_${id}`, _supersedes: null, _schema_version: 1,
  _version: 1, superseded: false, values, ...extra,
})
/** zhangsan 的三条：r3 是对 r1 的更正（r1 被更正过，只在历史里） */
const MINE_ROWS = [
  row('r2', 2, 'zhangsan', { store: '南山店', amount: 80, reason: '过期', photo: null }),
  row('r3', 3, 'zhangsan', { store: '南山店', amount: 120, reason: '运输破损', photo: PHOTO }, { _supersedes: 'r1' }),
  row('r5', 5, 'zhangsan', { store: '南山店', amount: 45, reason: '试吃', photo: null }),
]
const R1 = row('r1', 1, 'zhangsan', { store: '南山店', amount: 210, reason: '运输破损', photo: null }, { superseded: true })
const ALL_ROWS = [...MINE_ROWS, row('r4', 4, 'lisi', { store: '福田店', amount: 60, reason: '过期', photo: null })]

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
      state.writes.push({ method: r.method(), path: p })
      return route.fulfill({ status: 503, json: { detail: '检查脚本拦下了写请求' } })
    }
    if (p === '/api/auth/me') return route.fulfill({ json: state.me })
    if (p === '/api/tasks/count') return route.fulfill({ json: { mine: 0, apps: 0, other: 0 } })
    if (p === '/api/apps') return route.fulfill({ json: { items: [APP] } })
    if (p === '/api/apps/app_loss') return route.fulfill({ json: { ...APP, my_role: state.role, published: { version: 2, pages: HOME.pages, entries: [] } } })
    if (p === '/api/apps/app_loss/pages/home') return route.fulfill({ json: HOME })
    if (p === '/api/apps/app_loss/records') {
      state.reads.push(url.search)
      const mine = state.role === 'use'
      const items = mine ? MINE_ROWS : ALL_ROWS
      const history = url.searchParams.get('history') === 'true' || url.searchParams.get('history') === '1'
      return route.fulfill({ json: { table: 'loss', version: 1, columns: COLUMNS, mine_only: mine,
        items: history ? [R1, ...items] : items, next_cursor: null, state: 'ok' } })
    }
    return route.continue()
  })
  return { ctx, page }
}

const list = (page) => page.locator('[data-node="my_records"] [data-component="records_list"]')
const ids = (page) => list(page).locator('[data-record]').evaluateAll((els) => els.map((e) => e.getAttribute('data-record')))

// ---------------------------------------------------------------- 我的记录

await section('我的记录：使用者只看到自己的、标题写「我的记录」、按时间排序、筛选', async () => {
  const state = { writes: [], reads: [], me: USER, role: 'use' }
  const { page } = await open({ state })
  await page.goto(`${WEB}/apps/app_loss`)
  await list(page).locator('[data-record]').first().waitFor({ timeout: 15000 })
  check('数据取自记录接口、带上记录表', state.reads.some((q) => /[?&]table=loss/.test(q)), state.reads.join(' | '))
  check('使用者的标题是「我的记录」', (await list(page).locator('[data-records-title]').innerText()) === '我的记录')
  check('……写明只显示自己提交的（服务端 mine_only）', (await list(page).locator('[data-records-hint]').innerText()).includes('只显示你自己提交的记录')
    && await list(page).getAttribute('data-records-scope') === 'mine')
  check('……不列提交人', !(await list(page).innerText()).includes('由「zhangsan」提交'))
  check('服务端交来几行就列几行（界面不自己按人过滤）', (await ids(page)).length === 3)
  check('默认最新的在前', (await ids(page)).join(',') === 'r5,r3,r2', (await ids(page)).join(','))
  await list(page).locator('[data-records-sort]').selectOption('oldest')
  check('换成最早的在前', (await ids(page)).join(',') === 'r2,r3,r5', (await ids(page)).join(','))
  await list(page).locator('[data-records-filter]').fill('破损')
  check('按内容筛选，写明筛出几条', (await ids(page)).join(',') === 'r3'
    && (await list(page).locator('[data-records-count]').innerText()) === '1 / 3 条', await list(page).locator('[data-records-count]').innerText())
  await list(page).locator('[data-records-filter]').fill('没有这样的')
  check('筛不出来时照实说', (await list(page).innerText()).includes('没有符合筛选条件的记录'))
  await list(page).locator('[data-records-filter]').fill('')

  // 展开看全部字段和附件
  await list(page).locator('[data-record="r3"] > button').click()
  const detail = list(page).locator('[data-record="r3"] [data-record-detail]')
  check('展开看全部字段', await detail.locator('[data-record-field]').count() === 4
    && (await detail.locator('[data-record-field="reason"]').innerText()).includes('运输破损'))
  const link = detail.locator('[data-record-field="photo"] a[data-file-link]')
  check('附件是可下载的链接', await link.getAttribute('href') === '/api/files/f_photo' && (await link.innerText()).includes('破损.png'),
    await link.getAttribute('href').catch(() => ''))

  check('没写记录表的组件取第一张记录表，照样列出来', await page.locator('[data-node="no_table"] [data-record]').count() === 3
    && state.reads.some((q) => !/[?&]table=/.test(q)), state.reads.join(' | '))
  check('服务端给了第一页的照它画（这里是空的，写「你还没有提交过记录」）',
    (await page.locator('[data-node="server_page"]').innerText()).includes('你还没有提交过记录'))
  check('服务端说还没有记录表的照实说', (await page.locator('[data-node="empty_page"]').innerText()).includes('还没有记录表'))
  const preview = page.locator('[data-node="preview_page"]')
  check('按草稿预览（state=preview）：照实说不列真实的记录，不说成「还没有记录表」，也不给重新读取',
    await preview.locator('[data-records-preview]').count() === 1 && !(await preview.innerText()).includes('还没有记录表')
    && await preview.getByRole('button', { name: '重新读取' }).count() === 0, (await preview.innerText()).replace(/\s+/g, ' '))
  check('只读：没有发出写请求', state.writes.length === 0, state.writes.map((w) => w.path).join(','))
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

// ---------------------------------------------------------------- 更正

await section('更正：更正过的行标「已更正」，展开能看更正历史、原来那条照原样', async () => {
  const state = { writes: [], reads: [], me: USER, role: 'use' }
  const { page } = await open({ state })
  await page.goto(`${WEB}/apps/app_loss`)
  await list(page).locator('[data-record]').first().waitFor({ timeout: 15000 })
  const r3 = list(page).locator('[data-record="r3"]')
  check('更正过的行标「已更正」', await r3.getAttribute('data-record-corrected') === '' && (await r3.innerText()).includes('已更正'))
  check('没更正过的不标', !(await list(page).locator('[data-record="r2"]').innerText()).includes('已更正'))
  check('被更正的原行默认不列（只列当前有效的）', await list(page).locator('[data-record="r1"]').count() === 0)
  await r3.locator('> button').click()
  await r3.locator('[data-record-history-toggle]').click()
  const hist = r3.locator('[data-record-history]')
  check('展开更正历史：带 history 再取一次', await hist.waitFor({ timeout: 5000 }).then(() => true, () => false)
    && state.reads.some((q) => /[?&]history=(true|1)/.test(q)), state.reads.join(' | '))
  check('……列出被更正的那一条，值照原样', await hist.locator('[data-record-history-item="r1"]').count() === 1
    && (await hist.locator('[data-record-history-item="r1"] [data-record-field="amount"]').innerText()).includes('210'))
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

// ---------------------------------------------------------------- 全部记录

await section('全部记录：查看全部的人看到所有人的、每行写提交人', async () => {
  const state = { writes: [], reads: [], me: LEAD, role: 'observe' }
  const { page } = await open({ state })
  await page.goto(`${WEB}/apps/app_loss`)
  await list(page).locator('[data-record]').first().waitFor({ timeout: 15000 })
  check('标题写组件的标题（不是「我的记录」）', (await list(page).locator('[data-records-title]').innerText()) === '报损登记')
  check('……写明能查看全部记录', (await list(page).locator('[data-records-hint]').innerText()).includes('全部记录')
    && await list(page).getAttribute('data-records-scope') === 'all')
  check('列出别人提交的', (await ids(page)).includes('r4'))
  check('每行写提交人', (await list(page).locator('[data-record="r4"]').innerText()).includes('由「lisi」提交'))
  await list(page).locator('[data-records-filter]').fill('lisi')
  check('按提交人筛选', (await ids(page)).join(',') === 'r4', (await ids(page)).join(','))
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

await section('窄屏：390px 不出横向滚动', async () => {
  const state = { writes: [], reads: [], me: USER, role: 'use' }
  const { page } = await open({ state, width: 390, height: 844 })
  await page.goto(`${WEB}/apps/app_loss`)
  await list(page).locator('[data-record]').first().waitFor({ timeout: 15000 })
  await list(page).locator('[data-record="r3"] > button').click()
  check('展开一行后没有横向滚动', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
})

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 全部通过')
process.exit(failed ? 1 : 0)
