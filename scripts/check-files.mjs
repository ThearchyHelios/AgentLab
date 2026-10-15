// 文件控件（docs/design/normal-user-apps.md N9、§3.8、§4.7）的回归检查。
//
// 守的是这些「看着没坏、其实不对」的地方：
//   - 表单里 x-widget 为 file 的字段画成文件控件：拖放或点选，写明允许的类型和大小（来自 x-file）、谁能下载附件；
//   - 两步走：先 POST /api/files（multipart，带上下文——入口是 {app_id, entry}，待办是 {task_id}），拿回引用；引用就是
//     这个字段的值，发起时 values 里原样带着（只有引用的五个键）；
//   - 上传中写文件名、大小、进度，可以取消；服务端回包慢也不把进度冒充完成；
//   - 超过大小、类型不在允许范围内的先在这里拦下，不发请求，说清原因；服务端拒收的（415 / 413）照实写服务端的原话；
//   - 必填的没上传，本地拦下、挂在字段下面（规则和服务端同一份：必填，形状是文件引用）；
//   - 这次上传的移除时顺手删掉（DELETE /api/files/{id}）；
//   - 已提交的引用（待办里只读的表单、实例页上的字段）显示成可下载的链接（/api/files/{id}）；
//   - 不在入口、待办、试运行里的表单（页面的筛选表单）没有上传的上下文：照实说这里不能上传。
//
// 页面是真页面（走真的应用外壳），/api/apps/**、/api/files/**、/api/tasks/** 在浏览器层答掉，数据是这里虚构的（「报销审批」）；
// 其余 GET 放行到服务端（外壳要它），别的写请求一律拦下、记在探针里，不碰真实写接口。
//
//   node scripts/check-files.mjs
// 对别的实例跑时带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-files.mjs
// CHECK_ONLY=上传,下载 只跑段名里含这些字的段。
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

// ---------------------------------------------------------------- 假数据

const FILE_FIELD = { type: 'object', title: '发票', 'x-widget': 'file', 'x-file': { accept: ['application/pdf', 'image/png'], max_mb: 1 } }
const FORM = {
  format: 'agentlab.form/1', type: 'object', required: ['amount', 'invoice'], 'x-order': ['amount', 'invoice'],
  properties: { amount: { type: 'number', title: '金额（元）', minimum: 0 }, invoice: FILE_FIELD },
}
const FILTER_FORM = {
  format: 'agentlab.form/1', type: 'object', required: [], properties: { scan: { type: 'object', title: '扫描件', 'x-widget': 'file' } },
}
const APP = {
  id: 'app_claim', name: '报销审批', description: '员工发起报销', icon: 'app', status: 'active', published_version: 2,
  level: 'published', needs_upgrade: false, pending_tasks: 0, my_role: 'use', updated_at: ago(3600_000), scope: 'shared',
}
const HOME = {
  format: 'agentlab.view/1',
  app: { id: 'app_claim', name: '报销审批', version: 2, level: 'published', needs_upgrade: false },
  page: { id: 'home', title: '报销' },
  pages: [{ id: 'home', title: '报销' }],
  sources: {},
  tree: [
    { id: 'new', type: 'entry_button', props: { entry: 'apply' }, data: { entry: 'apply', label: '发起报销', disabled: false } },
    { id: 'filter', type: 'form', props: { form: 'filter' }, data: { form: 'filter' } },
  ],
  forms: { apply: FORM, filter: FILTER_FORM },
  problems: [],
}
const SUBMITTED = { file_id: 'f_inv', name: '发票-1001.pdf', size: 52_314, sha256: 'cd'.repeat(32), type: 'application/pdf' }
/** 财务的待办：表单里带着发起人交的发票（只读：不是这一位处理） */
const TASK = {
  id: 'task_fin', approval_id: 'ap_fin', run_id: 'run_claim_1', node_id: 'finance', app_id: 'app_claim', app_name: '报销审批',
  app_version: 2, workflow_name: '报销流程', instance_title: '张三 的报销', kind: 'input', title: '补充发票',
  form: { format: 'agentlab.form/1', type: 'object', required: ['invoice'], properties: { invoice: { ...FILE_FIELD, default: SUBMITTED } } },
  policy: { kind: 'any' }, status: 'pending', due_at: null, escalation_level: 0,
  payload: { kind: 'human_node', message: '请补充发票' }, assignments: [], can_respond: false, outcome: null,
  resolved_by: null, resolved_at: null, created_at: ago(600_000),
}
/** 一张能处理的待办：上传的上下文是 {task_id} */
const TASK_OPEN = { ...TASK, id: 'task_open', form: { ...TASK.form, properties: { invoice: FILE_FIELD } }, can_respond: true }

const pdf = (name, bytes) => ({ name, mimeType: 'application/pdf', buffer: Buffer.alloc(bytes, 0x25) })

// ---------------------------------------------------------------- 浏览器

const browser = await chromium.launch({ executablePath: CHROME })

async function open({ state, width = 1280, height = 900 }) {
  const ctx = await browser.newContext({ viewport: { width, height } })
  opened.add(ctx)
  const page = await ctx.newPage()
  page.errors = []
  page.on('pageerror', (e) => page.errors.push(e.message))
  await page.route((url) => url.pathname.startsWith('/api/'), async (route) => {
    const r = route.request()
    const url = new URL(r.url())
    const p = url.pathname
    if (r.method() !== 'GET') {
      const raw = r.postDataBuffer()?.toString('utf8') ?? ''
      state.writes.push({ method: r.method(), path: p, body: raw })
      if (r.method() === 'POST' && p === '/api/files') {
        // 服务端回包慢一点：上传中的样子要看得到。取消了的请求再答会抛错，吞掉
        const id = `f_${state.writes.length}`
        await new Promise((res) => setTimeout(res, state.uploadDelay ?? 600))
        if (state.reject) return route.fulfill({ status: state.reject.status, json: { detail: state.reject.detail } }).catch(() => {})
        const name = /filename="([^"]+)"/.exec(raw)?.[1] ?? 'file'
        return route.fulfill({ status: 201, json: { file_id: id, name, size: 2048, sha256: 'ef'.repeat(32),
          type: 'application/pdf', uploaded_at: ago(0) } }).catch(() => {})
      }
      if (r.method() === 'DELETE' && p.startsWith('/api/files/')) return route.fulfill({ status: 204, body: '' })
      if (p === '/api/apps/app_claim/entries/apply') return route.fulfill({ status: 201, json: { run_id: 'run_claim_9', title: '报销' } })
      return route.fulfill({ status: 503, json: { detail: '检查脚本拦下了写请求' } })
    }
    if (p === '/api/auth/me') return route.fulfill({ json: { mode: 'off', username: null, display_name: null, role: 'admin', groups: [], verified: false } })
    if (p === '/api/tasks/count') return route.fulfill({ json: { mine: 0, apps: 0, other: 0 } })
    if (p === '/api/apps') return route.fulfill({ json: { items: [APP] } })
    if (p === '/api/apps/app_claim') return route.fulfill({ json: { ...APP, published: { version: 2, pages: HOME.pages, entries: [{ id: 'apply', label: '发起报销' }] } } })
    if (p === '/api/apps/app_claim/pages/home') return route.fulfill({ json: HOME })
    if (p === '/api/apps/app_claim/instances/run_claim_9') return route.fulfill({ status: 404, json: { detail: '检查脚本不画实例页' } })
    if (p === '/api/tasks/task_fin') return route.fulfill({ json: TASK })
    if (p === '/api/tasks/task_open') return route.fulfill({ json: TASK_OPEN })
    return route.continue()
  })
  return { ctx, page }
}

const field = (page) => page.locator('[data-field="invoice"]')
const uploads = (state) => state.writes.filter((w) => w.method === 'POST' && w.path === '/api/files')

// ---------------------------------------------------------------- 上传

await section('入口：上传、进度、引用随表单提交、移除时删掉', async () => {
  const state = { writes: [], uploadDelay: 1200 }
  const { page } = await open({ state })
  await page.goto(`${WEB}/apps/app_claim/new/apply`)
  await field(page).waitFor({ timeout: 15000 })
  check('画成文件控件', await field(page).getAttribute('data-widget') === 'file'
    && await field(page).locator('[data-file-input="ready"] [data-file-drop]').count() === 1)
  const limits = await field(page).locator('[data-file-limits]').innerText()
  check('写明允许的类型和大小（来自 x-file）', limits.includes('PDF') && limits.includes('PNG 图片') && limits.includes('1 MB'), limits)
  check('写明谁能下载附件', limits.includes('都能下载附件'), limits)

  // 必填没上传：本地拦下
  await page.locator('#entry-apply-amount').fill('1200')
  await page.locator('[data-entry-submit]').click()
  check('必填的文件没上传：本地拦下、挂在字段下面', (await field(page).locator('[data-field-error]').innerText().catch(() => '')) === '必填'
    && !state.writes.some((w) => w.path.endsWith('/entries/apply')))

  await field(page).locator('[data-file-picker]').setInputFiles(pdf('发票-2001.pdf', 2048))
  const sending = field(page).locator('[data-file-sending]')
  check('上传中写文件名、大小', await sending.waitFor({ timeout: 3000 }).then(() => true, () => false)
    && (await sending.innerText()).includes('发票-2001.pdf') && (await sending.innerText()).includes('2.0 KB'),
    await sending.innerText().catch(() => ''))
  check('……写进度、不冒充完成', /正在上传|上传完成，正在等服务端核对/.test(await sending.innerText().catch(() => '')))
  const value = field(page).locator('[data-file-value]')
  await value.waitFor({ timeout: 5000 })
  const up = uploads(state)[0]
  check('先 POST /api/files，带上下文 {app_id, entry}', !!up && up.body.includes('"app_id":"app_claim"') && up.body.includes('"entry":"apply"'),
    (up?.body ?? '').slice(0, 120))
  check('上传完写文件名，给「换一个」「移除」', (await value.innerText()).includes('发票-2001.pdf')
    && await value.locator('[data-file-replace]').count() === 1 && await value.locator('[data-file-remove]').count() === 1)

  await page.locator('[data-entry-submit]').click()
  await page.waitForTimeout(500)
  const start = state.writes.find((w) => w.path === '/api/apps/app_claim/entries/apply')
  const values = start ? JSON.parse(start.body || '{}').values : null
  check('发起时 values 里带着引用（只有五个键）', !!values && values.invoice?.file_id === 'f_1'
    && Object.keys(values.invoice).sort().join(',') === 'file_id,name,sha256,size,type', JSON.stringify(values))
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')

  // 移除：这次上传的顺手删掉
  const s2 = { writes: [], uploadDelay: 100 }
  const two = await open({ state: s2 })
  await two.page.goto(`${WEB}/apps/app_claim/new/apply`)
  await field(two.page).locator('[data-file-picker]').setInputFiles(pdf('发票-3001.pdf', 1024))
  await field(two.page).locator('[data-file-value]').waitFor({ timeout: 5000 })
  await field(two.page).locator('[data-file-remove]').click()
  await two.page.waitForTimeout(300)
  check('移除这次上传的：DELETE /api/files/{id}', s2.writes.some((w) => w.method === 'DELETE' && w.path === '/api/files/f_1'))
  check('……控件回到可以上传的样子', await field(two.page).locator('[data-file-drop]').count() === 1)
})

await section('上传：取消、超过大小和类型不对先拦下、服务端拒收照实说', async () => {
  const state = { writes: [], uploadDelay: 4000 }
  const { page } = await open({ state })
  await page.goto(`${WEB}/apps/app_claim/new/apply`)
  await field(page).waitFor({ timeout: 15000 })

  await field(page).locator('[data-file-picker]').setInputFiles(pdf('大文件.pdf', 2 * 1024 * 1024))
  check('超过大小：不发请求，说清超过几 MB', (await field(page).locator('[data-file-failure]').innerText()).includes('超过 1 MB')
    && uploads(state).length === 0)
  await field(page).locator('[data-file-picker]').setInputFiles({ name: '说明.txt', mimeType: 'text/plain', buffer: Buffer.from('hello') })
  const bad = await field(page).locator('[data-file-failure]').innerText()
  check('类型不在允许范围内：不发请求，写明允许的类型', bad.includes('类型不在允许范围内') && bad.includes('PDF') && uploads(state).length === 0, bad)

  await field(page).locator('[data-file-picker]').setInputFiles(pdf('慢.pdf', 1024))
  await field(page).locator('[data-file-cancel]').click()
  check('上传中可以取消，取消照实说', (await field(page).locator('[data-file-failure]').innerText()).includes('已取消上传'))
  check('……没有留下值', await field(page).locator('[data-file-value]').count() === 0)

  state.uploadDelay = 50
  state.reject = { status: 415, detail: '这种类型的文件不能上传：只收 PDF、Word、Excel、CSV、图片和纯文本' }
  await field(page).locator('[data-file-picker]').setInputFiles({ name: '扫描.png', mimeType: 'image/png', buffer: Buffer.alloc(512, 1) })
  await page.waitForFunction(() => !!document.querySelector('[data-field="invoice"] [data-file-failure]')?.textContent?.includes('上传失败'),
    null, { timeout: 5000 }).catch(() => {})
  const refused = await field(page).locator('[data-file-failure]').innerText()
  check('服务端拒收：照实写服务端的原话', refused.includes('「扫描.png」上传失败') && refused.includes('只收 PDF'), refused)
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

// ---------------------------------------------------------------- 下载、上下文

await section('已提交的引用是下载链接；待办的上传带 {task_id}；筛选表单不能上传', async () => {
  const state = { writes: [], uploadDelay: 100 }
  const { page } = await open({ state })
  await page.goto(`${WEB}/apps/inbox/task_fin`)
  await page.locator('[data-task="task_fin"]').waitFor({ timeout: 15000 })
  const link = page.locator('[data-task="task_fin"] [data-field="invoice"] a[data-file-link]')
  check('只读的表单里，已提交的引用是可下载的链接', await link.getAttribute('href') === '/api/files/f_inv'
    && (await link.innerText()).includes('发票-1001.pdf'), await link.getAttribute('href').catch(() => ''))

  await page.goto(`${WEB}/apps/inbox/task_open`)
  await page.locator('[data-task="task_open"] [data-field="invoice"]').waitFor({ timeout: 15000 })
  await page.locator('[data-task="task_open"] [data-file-picker]').setInputFiles(pdf('补充发票.pdf', 1024))
  await page.locator('[data-task="task_open"] [data-file-value]').waitFor({ timeout: 5000 })
  const up = uploads(state).at(-1)
  check('待办里上传带上下文 {task_id}', !!up && up.body.includes('"task_id":"task_open"'), (up?.body ?? '').slice(0, 120))

  await page.goto(`${WEB}/apps/app_claim`)
  const scan = page.locator('[data-node="filter"] [data-field="scan"]')
  await scan.waitFor({ timeout: 15000 })
  check('页面的筛选表单没有上传的上下文：照实说这里不能上传', await scan.locator('[data-file-input="no-context"]').count() === 1
    && (await scan.innerText()).includes('这里不能上传文件') && await scan.locator('[data-file-drop]').isDisabled())
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
})

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 全部通过')
process.exit(failed ? 1 : 0)
