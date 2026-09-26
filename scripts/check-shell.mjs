// 外壳：导航、待审批徽标、404、标签页标题与 favicon、后台提醒、⌘K 命令面板、
// ? 快捷键说明、主题快速切换、连接遥测、离线横幅、启动页。
//
// 这些都是每一屏都在的东西，坏了没有哪一页的检查会替它说话：徽标指向一个找不到
// 待办的列表、打错地址看到白板、后端没起时启动页永远「正在连接」、命令面板的
// aria 断了读屏用户就用不了——之前每一样都真发生过。
//
// 待审批用 page.route 伪造（两条，最早的一条等了 8 天），离线用 page.route 直接
// 掐断 /api。所有非 GET 请求（主题切换会 PUT 设置）都在探针里应答，不写库。
// 跑之前前端得起着；对沙箱跑：
//   AGENTLAB_WEB=http://localhost:5373 node scripts/check-shell.mjs
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
// 不用 playwright install：它既下不动也会动到已有缓存。系统 Chrome 就够了。
const CHROME = process.env.CHROME_PATH
  ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}

const ago = (ms) => new Date(Date.now() - ms).toISOString()
const PENDING = [
  {
    id: 'shell-ap-old', run_id: 'shell-run-old', node_id: 'review', mode: 'approve', title: '这条公告可以发吗？',
    payload: {}, status: 'pending', response: {}, created_at: ago(8 * 86400_000 + 3600_000),
    workflow_name: '公告审核', node_label: '人工审批', run_status: 'interrupted', run_class: 'exploratory',
  },
  {
    id: 'shell-ap-new', run_id: 'shell-run-new', node_id: 'review', mode: 'input', title: '补一下预算上限',
    payload: {}, status: 'pending', response: {}, created_at: ago(12 * 60_000),
    workflow_name: '采购比价', node_label: '人工审批', run_status: 'interrupted', run_class: 'exploratory',
  },
]

// 相对亮度与对比度（WCAG）：徽标在两套主题下都要 ≥ 4.5
const lum = (rgb) => {
  const [r, g, b] = rgb.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number).map((v) => {
    const c = v / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}
const contrast = (a, b) => {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p)
  return (x + 0.05) / (y + 0.05)
}

const browser = await chromium.launch({ executablePath: CHROME })

/** 一个带探针的新页面。state 可以在跑的过程中改：换待审批、断网 */
async function open({ theme = 'dark', state }) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 }, colorScheme: theme })
  const page = await ctx.newPage()
  page.errors = []
  page.on('pageerror', (e) => page.errors.push(e.message))
  await page.addInitScript((t) => {
    localStorage.setItem('agentlab.theme', t)
    // 系统通知换成记录器：只记谁在什么时候要了权限、发了什么
    window.__notes = []
    window.__permissionAsks = 0
    class FakeNotification {
      static permission = 'default'
      static async requestPermission() {
        window.__permissionAsks++
        FakeNotification.permission = 'granted'
        return 'granted'
      }
      constructor(title, opts) { window.__notes.push({ title, ...opts }) }
      close() {}
    }
    window.Notification = FakeNotification
  }, theme)
  // 只认路径以 /api/ 开头的：'**/api/**' 也会匹配到 vite 的模块 /src/api/client.ts，
  // 断网时把它一起掐了，页面就直接白了
  await page.route((url) => url.pathname.startsWith('/api/'), (route) => {
    const r = route.request()
    const url = new URL(r.url())
    if (state.offline) return route.abort('connectionrefused')
    // 后端连得上但不回：/health 照常，其余请求一直挂着
    if (state.hang && url.pathname !== '/api/health') return
    if (r.method() !== 'GET') {
      state.writes.push({ method: r.method(), path: url.pathname, body: r.postData() })
      return route.fulfill({ json: {} })
    }
    // 主题以本地为准，别让沙箱库里存的偏好把这次检查的主题改掉
    if (url.pathname === '/api/settings') return route.fulfill({ json: { ui: { theme } } })
    if (url.pathname === '/api/approvals') return route.fulfill({ json: state.approvals })
    if (state.broken === url.pathname) return route.fulfill({ status: 500, json: { detail: '数据库锁住了，稍后重试' } })
    return route.continue()
  })
  return { ctx, page }
}

const nav = (page) => page.locator('nav[aria-label="主导航"]')
const waitNav = (page) => nav(page).waitFor({ timeout: 15000 })
const isMac = process.platform === 'darwin'
const MOD = isMac ? 'Meta' : 'Control'

// ---------------------------------------------------------------------------
console.log('=== 导航 ===')
{
  const state = { approvals: PENDING, offline: false, writes: [] }
  const { ctx, page } = await open({ state })
  await page.goto(`${WEB}/chat`)
  await waitNav(page)

  const box = await nav(page).boundingBox()
  check('导航宽 64px（w-16）', Math.round(box.width) === 64, `${box.width}`)
  const labels = await nav(page).locator('li > a[aria-keyshortcuts] > span:not([aria-hidden])').allInnerTexts()
  check('导航项：问数据、编排、记录、数据、工具、知识、设置',
    labels.join(',') === '问数据,编排,记录,数据,工具,知识,设置', labels.join(','))
  const fontSize = await nav(page).locator('li > a[aria-keyshortcuts] > span:not([aria-hidden])').first()
    .evaluate((el) => getComputedStyle(el).fontSize)
  check('导航标签 11px', fontSize === '11px', fontSize)

  const active = nav(page).locator('a[aria-current="page"]')
  check('当前页有 aria-current，且只有一个', (await active.count()) === 1 && (await active.innerText()).startsWith('问数据'))
  const accent = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim())
  const [iconColor, barColor] = await active.evaluate((el) => [
    getComputedStyle(el.querySelector('svg')).color,
    getComputedStyle(el.querySelector('span[aria-hidden]')).backgroundColor,
  ])
  const hex = (rgb) => '#' + rgb.match(/\d+/g).slice(0, 3).map((v) => Number(v).toString(16).padStart(2, '0')).join('')
  check('选中项：图标是强调色', hex(iconColor) === accent.toLowerCase(), `${hex(iconColor)} vs ${accent}`)
  check('选中项：左缘有强调色竖条', hex(barColor) === accent.toLowerCase(), hex(barColor))

  await page.getByRole('link', { name: 'AgentLab 首页' }).waitFor({ timeout: 3000 }).then(() => check('品牌标是回首页的链接', true), () => check('品牌标是回首页的链接', false))

  await nav(page).locator('a[href="/data"]').hover()
  const tip = nav(page).locator('a[href="/data"] > span[aria-hidden]').last()
  await page.waitForTimeout(600)
  check('悬停出提示：页面名 + 快捷键', (await tip.isVisible()) && /数据/.test(await tip.innerText()) && /(⌥4|Alt\+4)/.test(await tip.innerText()),
    (await tip.innerText()).replace(/\n/g, ' '))

  await page.locator('main').click({ position: { x: 600, y: 300 } }).catch(() => {})
  await page.keyboard.press('Alt+Digit3')
  check('⌥3 切到记录页', await page.waitForURL(/\/runs(\?|$)/, { timeout: 5000 }).then(() => true, () => false), page.url())

  await page.goto(`${WEB}/settings/datasources`)
  check('旧地址 /settings/datasources 重定向到 /data', await page.waitForURL(/\/data(\/|$)/, { timeout: 8000 }).then(() => true, () => false), page.url())
  check('数据页点亮「数据」', (await nav(page).locator('a[aria-current="page"]').innerText()).startsWith('数据'))

  // 矮窗口：导航按高度收紧，最底下的遥测点和署名不能被切掉
  const bottomFits = () => page.evaluate(() => {
    const n = document.querySelector('nav[aria-label="主导航"]')
    const t = document.querySelector('[data-telemetry]').getBoundingClientRect()
    return { ok: n.scrollHeight <= innerHeight && t.bottom <= innerHeight, tel: Math.round(t.bottom), vh: innerHeight }
  })
  for (const h of [600, 480]) {
    await page.setViewportSize({ width: 1280, height: h })
    await page.waitForTimeout(150)
    const fit = await bottomFits()
    check(`窗口高 ${h}px：遥测点整个露在视口里，导航不溢出`, fit.ok, JSON.stringify(fit))
  }
  await page.setViewportSize({ width: 1280, height: 860 })
  await page.waitForTimeout(150)
  const itemH = await nav(page).locator('a[href="/runs"]').evaluate((el) => el.getBoundingClientRect().height)
  check('窗口够高时导航项仍是 48px', itemH === 48, `${itemH}`)

  // 底部三样：署名、主题、遥测
  await page.goto(`${WEB}/chat`)
  await waitNav(page)
  const actor = nav(page).locator('a[href="/settings/prefs"]')
  check('底部有署名入口，点开去设置', (await actor.count()) === 1, await actor.getAttribute('aria-label'))
  const telemetry = nav(page).locator('[data-telemetry]')
  await page.waitForFunction(() => document.querySelector('[data-telemetry]')?.getAttribute('data-telemetry') === 'ok', null, { timeout: 10000 }).catch(() => {})
  check('底部遥测点显示在线和延迟', /\d+ms/.test(await telemetry.innerText()), await telemetry.innerText())
  await telemetry.click()
  const panel = page.getByRole('dialog', { name: '后端连接' })
  check('点开遥测点：地址、延迟、加载清单', (await panel.isVisible()) && /\/api/.test(await panel.innerText()) && /模型接入/.test(await panel.innerText()))
  await page.keyboard.press('Escape')
  check('Esc 关掉遥测浮层', await panel.waitFor({ state: 'detached', timeout: 3000 }).then(() => true, () => false))

  const before = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
  await nav(page).getByRole('button', { name: /切换到.+主题/ }).click()
  const after = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
  check('主题快速切换：深色 → 浅色', before === 'dark' && after === 'light', `${before} → ${after}`)
  await page.waitForTimeout(300)
  const put = state.writes.find((w) => w.method === 'PUT' && w.path === '/api/settings')
  check('主题存进设置（PUT ui.theme，被探针拦下）', !!put && JSON.parse(put.body).values.ui.theme === 'light', put?.body ?? '没有请求')
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
  await ctx.close()
}

// ---------------------------------------------------------------------------
console.log('\n=== 待审批徽标 ===')
for (const theme of ['dark', 'light']) {
  const state = { approvals: PENDING, offline: false, writes: [] }
  const { ctx, page } = await open({ theme, state })
  await page.goto(`${WEB}/chat`)
  await waitNav(page)
  const badge = page.locator('[data-approval-badge]')
  await badge.waitFor({ timeout: 8000 })
  const b = await badge.boundingBox()
  const icon = await nav(page).locator('a[href="/runs"] svg').boundingBox()
  const [fg, bg, nums] = await badge.evaluate((el) => {
    const s = getComputedStyle(el)
    return [s.color, s.backgroundColor, s.fontVariantNumeric]
  })
  if (theme === 'dark') {
    check('徽标是链接，指向 /runs?tab=approvals', (await badge.getAttribute('href')) === '/runs?tab=approvals')
    check('导航项「记录」本身仍然进 /runs', (await nav(page).locator('a[aria-keyshortcuts]').nth(2).getAttribute('href')) === '/runs')
    const label = await badge.getAttribute('aria-label')
    check('徽标读屏文字：条数 + 最久等了多久', /2 项待审批/.test(label) && /最久已等 8 天/.test(label), label)
    check('徽标至少 16×16', b.width >= 16 && b.height >= 16, `${b.width}×${b.height}`)
    check('徽标数字等宽', /tabular-nums/.test(nums), nums)
    check('徽标挂在图标右上角外侧，不压图标主体', b.x + b.width / 2 > icon.x + icon.width && b.y < icon.y,
      `badge(${b.x},${b.y}) icon(${icon.x},${icon.y},${icon.width})`)
  }
  const ratio = contrast(fg, bg)
  check(`徽标对比度 ≥ 4.5（${theme === 'dark' ? '深色' : '浅色'}）`, ratio >= 4.5, ratio.toFixed(2))
  if (theme === 'dark') {
    await badge.hover()
    await page.waitForTimeout(400)
    const tip = badge.locator('span[aria-hidden]')
    check('悬停徽标有提示', (await tip.isVisible()) && /最久已等/.test(await tip.innerText()))
    await badge.click()
    check('点徽标进待审批页签', await page.waitForURL(/\/runs\?tab=approvals/, { timeout: 5000 }).then(() => true, () => false), page.url())
  }
  await ctx.close()
}

// ---------------------------------------------------------------------------
console.log('\n=== 404 ===')
{
  const state = { approvals: [], offline: false, writes: [] }
  const { ctx, page } = await open({ state })
  await page.goto(`${WEB}/does-not-exist/at-all`)
  await waitNav(page)
  check('未知地址给出说明而不是白板', await page.getByText('这个地址不存在').waitFor({ timeout: 8000 }).then(() => true, () => false))
  check('写出了打错的那个地址', (await page.locator('main').innerText()).includes('/does-not-exist/at-all'))
  check('标题是「页面不存在 — AgentLab」', (await page.title()) === '页面不存在 — AgentLab', await page.title())
  check('给了 ⌘K 入口', await page.getByRole('button', { name: /搜索去处/ }).isVisible())
  await page.getByRole('button', { name: /搜索去处/ }).click()
  check('404 上点搜索打开命令面板', await page.getByRole('dialog', { name: '命令面板' }).isVisible())
  await page.keyboard.press('Escape')
  await page.getByRole('link', { name: '回到问数据' }).click()
  check('「回到问数据」回首页', await page.waitForURL(/\/chat/, { timeout: 5000 }).then(() => true, () => false), page.url())
  await ctx.close()
}

// ---------------------------------------------------------------------------
console.log('\n=== 标签页标题、favicon、后台提醒 ===')
{
  const state = { approvals: [], offline: false, writes: [] }
  const { ctx, page } = await open({ state })
  await page.goto(`${WEB}/runs`)
  await waitNav(page)
  await page.waitForFunction(() => document.title.includes('记录'), null, { timeout: 5000 }).catch(() => {})
  check('每页有自己的标题', (await page.title()) === '记录 — AgentLab', await page.title())
  const favicon = () => page.evaluate(() => {
    const l = document.querySelector('link[rel~="icon"]')
    return { href: l.getAttribute('href'), status: l.getAttribute('data-status') }
  })
  check('没事时 favicon 是原图', (await favicon()).href === '/favicon.svg')

  state.approvals = PENDING
  const titled = await page.waitForFunction(() => document.title.startsWith('(2) 待审批 · '), null, { timeout: 12000 })
    .then(() => true, () => false)
  check('来了待审批：标题加「(2) 待审批 · 」前缀', titled, await page.title())
  const fav = await favicon()
  check('来了待审批：favicon 换成带琥珀点的 PNG', fav.status === 'waiting' && fav.href.startsWith('data:image/png'), fav.status)

  state.approvals = []
  await page.waitForFunction(() => !document.title.includes('待审批'), null, { timeout: 12000 }).catch(() => {})
  await page.goto(`${WEB}/studio`)
  await waitNav(page)
  const hasStudio = await page.waitForFunction(() => window.__studio?.getState().nodes.length > 0, null, { timeout: 12000 })
    .then(() => true, () => false)
  check('画布装好了（dev 构建的 window.__studio）', hasStudio)
  if (hasStudio) {
    const name = await page.evaluate(() => window.__studio.getState().workflow?.name)
    check('编排页标题带工作流名', (await page.title()).startsWith(`编排 · ${name}`), await page.title())

    // 权限只在用户点开关时才要
    check('进页面不自动要通知权限', (await page.evaluate(() => window.__permissionAsks)) === 0)
    await nav(page).getByRole('button', { name: '后台提醒' }).click()
    await page.waitForTimeout(200)
    check('点开关才要权限，开关变为按下', (await page.evaluate(() => window.__permissionAsks)) === 1
      && (await nav(page).getByRole('button', { name: '后台提醒' }).getAttribute('aria-pressed')) === 'true')

    await page.evaluate(() => {
      const s = window.__studio.getState()
      window.__studio.setState({
        run: { id: 'shell-check-run', workflow_id: s.workflow?.id ?? null, workflow_name: s.workflow?.name ?? '', status: 'running',
          input: {}, output: {}, error: null, usage: {} },
        runPhase: 'running',
      })
    })
    await page.waitForFunction(() => document.title.includes('● 运行中'), null, { timeout: 3000 }).catch(() => {})
    // 只认外壳自己的前缀；前面若还挂着别的（比如以后画布自己加的计时），不算错
    check('有运行在跑：标题加「● 运行中 n/N」', /(^|\s)● 运行中 \d+\/\d+ · 编排/.test(await page.title()), await page.title())
    check('有运行在跑：favicon 蓝点', (await favicon()).status === 'running')
    check('有运行在跑：品牌标起点节点亮起', /有运行在跑/.test(await nav(page).locator('a[href="/"]').getAttribute('aria-label')))

    // 切到后台，运行失败
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await page.waitForTimeout(1100)
    await page.evaluate(() => window.__studio.setState({ runPhase: 'failed' }))
    await page.waitForTimeout(200)
    check('后台时失败：标题换成「✕ 运行失败」', /(^|\s)✕ 运行失败 · /.test(await page.title()), await page.title())
    check('后台时失败：favicon 红点', (await favicon()).status === 'failed')
    const notes = await page.evaluate(() => window.__notes)
    check('后台时失败：发了一条系统通知', notes.length === 1 && /运行失败/.test(notes[0].title), JSON.stringify(notes))

    await page.evaluate(() => {
      delete document.hidden
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await page.waitForTimeout(200)
    check('切回来：失败前缀撤掉', !(await page.title()).includes('运行失败'), await page.title())
    check('切回来：favicon 还原', (await favicon()).href === '/favicon.svg')

    // 前台时结束不打扰
    await page.evaluate(() => window.__studio.setState({ runPhase: 'running' }))
    await page.waitForTimeout(1100)
    await page.evaluate(() => window.__studio.setState({ runPhase: 'succeeded' }))
    await page.waitForTimeout(200)
    check('前台时跑完不发通知', (await page.evaluate(() => window.__notes.length)) === 1)
    await page.evaluate(() => window.__studio.getState().clearRun())
  }
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
  await ctx.close()
}

// ---------------------------------------------------------------------------
console.log('\n=== ⌘K 命令面板 ===')
{
  const state = { approvals: PENDING, offline: false, writes: [] }
  const { ctx, page } = await open({ state })
  await page.goto(`${WEB}/runs`)
  await waitNav(page)
  await page.waitForTimeout(500)
  await page.keyboard.press(`${MOD}+KeyK`)
  const dialog = page.getByRole('dialog', { name: '命令面板' })
  check(`${isMac ? '⌘K' : 'Ctrl+K'} 打开命令面板`, await dialog.waitFor({ timeout: 3000 }).then(() => true, () => false))
  const input = dialog.getByRole('combobox')
  check('打开时焦点在搜索框', await input.evaluate((el) => el === document.activeElement))
  const aria = await input.evaluate((el) => ({
    controls: el.getAttribute('aria-controls'), active: el.getAttribute('aria-activedescendant'), expanded: el.getAttribute('aria-expanded'),
  }))
  const listbox = await page.evaluate((id) => document.getElementById(id)?.getAttribute('role'), aria.controls)
  const selected = await page.evaluate((id) => document.getElementById(id)?.getAttribute('aria-selected'), aria.active)
  check('combobox → listbox 接好了，activedescendant 指向选中项', listbox === 'listbox' && selected === 'true' && aria.expanded === 'true', JSON.stringify(aria))
  check('分组有名字（role=group + aria-labelledby）', (await dialog.locator('[role="group"][aria-labelledby]').count()) >= 3)
  check('待审批排在最前', (await dialog.locator('[role="group"]').first().innerText()).startsWith('待审批'))
  await page.keyboard.press('ArrowDown')
  const moved = await input.getAttribute('aria-activedescendant')
  check('↓ 移动选中项', moved !== aria.active)

  await input.fill('设置')
  const first = dialog.locator('[role="option"][aria-selected="true"]')
  check('搜「设置」第一项就是设置页', (await first.innerText()).startsWith('设置'), (await first.innerText()).split('\n')[0])
  await page.keyboard.press('Enter')
  check('回车跳过去、面板关掉', await page.waitForURL(/\/settings/, { timeout: 5000 }).then(() => true, () => false)
    && !(await dialog.isVisible()), page.url())

  const workflows = await (await fetch(`${WEB}/api/workflows`)).json().catch(() => [])
  const wf = workflows.find((w) => !w.is_template && w.name)
  if (wf) {
    await page.keyboard.press(`${MOD}+KeyK`)
    await dialog.getByRole('combobox').fill(wf.name)
    await page.waitForTimeout(100)
    const hit = dialog.locator(`[data-command="wf:${wf.id}"]`)
    check('能搜到工作流', (await hit.count()) === 1, wf.name)
    await hit.click()
    check('点工作流进它的画布', await page.waitForURL(new RegExp(`/studio/${wf.id}`), { timeout: 5000 }).then(() => true, () => false), page.url())
  }

  await page.keyboard.press(`${MOD}+KeyK`)
  await dialog.getByRole('combobox').fill('zzqq没有这个东西')
  check('没结果时说清楚', (await dialog.innerText()).includes('没有匹配'))
  await page.keyboard.press('Escape')
  check('Esc 关掉面板', await dialog.waitFor({ state: 'detached', timeout: 3000 }).then(() => true, () => false))

  // DialogHost 跟着 ToastHost 挂在根上：没挂的话 promptDialog 会退回原生 prompt
  let nativePrompt = false
  page.once('dialog', (d) => { nativePrompt = true; void d.dismiss() })
  await page.keyboard.press(`${MOD}+KeyK`)
  await dialog.getByRole('combobox').fill('新建工作流')
  await page.keyboard.press('Enter')
  const ask = page.getByRole('dialog', { name: '新建工作流' })
  check('「新建工作流」弹的是站内输入框，不是原生 prompt', await ask.waitFor({ timeout: 3000 }).then(() => true, () => false) && !nativePrompt)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(200)
  // 画布自己会发校验、变量分析的 POST，只看有没有建工作流
  check('取消就什么都不建', !state.writes.some((w) => w.method === 'POST' && w.path === '/api/workflows') && (await ask.count()) === 0)

  await page.keyboard.press(`${MOD}+KeyK`)
  await dialog.getByRole('combobox').fill('快捷键')
  await page.keyboard.press('Enter')
  check('面板里的「查看快捷键」打开说明', await page.getByRole('dialog', { name: /键盘快捷键/ }).waitFor({ timeout: 3000 }).then(() => true, () => false))
  await page.keyboard.press('Escape')
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
  await ctx.close()
}

// ---------------------------------------------------------------------------
console.log('\n=== ? 快捷键说明 ===')
{
  const state = { approvals: [], offline: false, writes: [] }
  const { ctx, page } = await open({ state })
  await page.goto(`${WEB}/studio`)
  await waitNav(page)
  await page.waitForTimeout(800)
  // 编排页一进来焦点在助手输入框里，那时的 ? 是打字；先把焦点放回页面
  await page.evaluate(() => document.activeElement?.blur())
  await page.keyboard.press('?')
  const help = page.getByRole('dialog', { name: /键盘快捷键/ })
  check('? 打开快捷键说明', await help.waitFor({ timeout: 3000 }).then(() => true, () => false))
  if (await help.isVisible()) {
    const text = await help.innerText()
    check('有「全局」和「编排页」两组', text.includes('全局') && text.includes('编排页'))
    check('编排页那组标着「当前页」', (await help.locator('section', { hasText: '编排页' }).innerText()).includes('当前页'))
    check(`按平台显示（${isMac ? '⌘K' : 'Ctrl+K'}）`, text.includes(isMac ? '⌘K' : 'Ctrl+K'))
    check('只弹一层', (await page.locator('[role="dialog"][aria-modal="true"]').count()) === 1)
    await page.keyboard.press('Escape')
    check('Esc 关掉', await help.waitFor({ state: 'detached', timeout: 3000 }).then(() => true, () => false))
  }
  await ctx.close()
}

// ---------------------------------------------------------------------------
console.log('\n=== 离线横幅 ===')
{
  const state = { approvals: [], offline: false, writes: [] }
  const { ctx, page } = await open({ state })
  await page.goto(`${WEB}/chat`)
  await waitNav(page)
  await page.waitForTimeout(500)
  state.offline = true
  // 导航遥测点的悬停提示里也有这四个字（隐藏着），只认主区里的横幅
  const banner = page.locator('main').getByText('后端未连接')
  check('断网后出横幅', await banner.waitFor({ timeout: 15000 }).then(() => true, () => false))
  check('横幅是主区第一个孩子（toast 才会跟着下移）',
    await page.evaluate(() => document.querySelector('main')?.firstElementChild?.getAttribute('role') === 'alert'))
  check('遥测点变成离线', /离线/.test(await nav(page).locator('[data-telemetry]').innerText()))
  state.offline = false
  await page.getByRole('button', { name: '立即重试' }).first().click()
  check('恢复后横幅消失', await banner.waitFor({ state: 'detached', timeout: 8000 }).then(() => true, () => false))
  await ctx.close()
}

/**
 * 启动页的播报区：只能有标题那一处，计时器、倒计时这些一秒一跳的字不能在里面，
 * 外面也不能罩着 aria-busy（它会把播报压住）
 */
const bootLiveRegions = (page) => page.evaluate(() => {
  const root = document.querySelector('[data-boot]')
  const live = [...root.querySelectorAll('[role="status"], [role="alert"], [aria-live]')]
  const texts = live.map((el) => el.textContent)
  return {
    ok: live.length === 1 && !/\d/.test(texts[0]) && !root.closest('[aria-busy="true"]') && !root.querySelector('[aria-busy="true"]'),
    texts,
  }
})

// ---------------------------------------------------------------------------
console.log('\n=== 启动页 ===')
for (const theme of ['dark', 'light']) {
  const state = { approvals: [], offline: true, writes: [] }
  const { ctx, page } = await open({ theme, state })
  await page.goto(`${WEB}/chat`)
  const down = await page.getByText('连不上后端服务').first().waitFor({ timeout: 10000 }).then(() => true, () => false)
  if (theme === 'dark') {
    check('后端没起：启动页说清原因，不再一直「正在连接」', down)
    check('给了重试和先进去两条路', await page.getByRole('button', { name: '立即重试' }).isVisible()
      && await page.getByRole('button', { name: '先进去看看' }).isVisible())
    check('清单逐项写出失败', (await page.locator('[data-check][data-state="error"]').count()) >= 6)
    const live = await bootLiveRegions(page)
    check('读屏只播标题那一句：倒计时不在播报区里', live.ok, JSON.stringify(live))
    state.offline = false
    await page.getByRole('button', { name: '立即重试' }).click()
    check('后端起来后点重试就进去', await waitNav(page).then(() => true, () => false))
  } else {
    const bootBg = await page.evaluate(() => getComputedStyle(document.querySelector('[data-boot]')).backgroundColor)
    check('浅色主题下启动页也是浅色', bootBg === 'rgb(246, 247, 249)', bootBg)
    await page.getByRole('button', { name: '先进去看看' }).click()
    check('「先进去看看」进离线模式，横幅挂着', await page.locator('main').getByText('后端未连接').waitFor({ timeout: 5000 }).then(() => true, () => false))
  }
  await ctx.close()
}

{
  // 后端连着、只有一张表报错：照常进，但要说出来，不然那张表对应的下拉就是空的，看着像"没有"
  const state = { approvals: [], offline: false, writes: [], broken: '/api/skills' }
  const { ctx, page } = await open({ state })
  await page.goto(`${WEB}/chat`)
  await waitNav(page)
  const warned = await page.getByText(/有 1 项没加载成功：Skill（500）/).waitFor({ timeout: 5000 }).then(() => true, () => false)
  check('部分请求报错：照常进页面，并说出哪一项没加载', warned)
  const tone = await nav(page).locator('[data-telemetry]').getAttribute('data-tone')
  check('遥测点记为「降级」（琥珀），不是一片绿', tone === '降级', tone)
  await ctx.close()
}

{
  // 后端连得上、请求却卡着不回：「先进去看看」之后不能是一片绿加一屏「还没有工作流」
  const state = { approvals: [], offline: false, writes: [], hang: true }
  const { ctx, page } = await open({ state })
  await page.goto(`${WEB}/studio`)
  const enter = page.getByRole('button', { name: '先进去看看' })
  const slow = await enter.waitFor({ timeout: 12000 }).then(() => true, () => false)
  check('请求卡住：启动页到点改说「比平时慢」', slow && (await page.locator('[data-boot] [role="status"]').innerText()).includes('比平时慢'))
  const live = await bootLiveRegions(page)
  check('读屏只播标题那一句：计时器不在播报区里', live.ok, JSON.stringify(live))
  check('先说清楚进去的代价', (await page.locator('[data-boot]').innerText()).includes('不代表没有数据'))
  await enter.click()
  await waitNav(page)
  const telemetry = nav(page).locator('[data-telemetry]')
  const tone = await telemetry.getAttribute('data-tone')
  check('进去以后遥测点不是「在线」，而是「加载中」', tone === '加载中', `${tone} / ${await telemetry.innerText()}`)
  const warn = page.getByText(/没取回来.*先别新建/)
  check('常驻提示点名还没回来的几项，劝先别新建', await warn.waitFor({ timeout: 3000 }).then(() => true, () => false)
    && /工作流/.test(await warn.innerText()), await warn.innerText().catch(() => ''))
  await page.waitForTimeout(4500)
  check('提示是常驻的，不会自己消失', await warn.isVisible())
  await telemetry.click()
  const panel = page.getByRole('dialog', { name: '后端连接' })
  check('遥测浮层里写出哪几项还没回来', /还没回来/.test(await panel.innerText()))
  await page.keyboard.press('Escape')

  state.hang = false
  await page.locator('[role="status"] > div', { hasText: '先别新建' }).getByRole('button', { name: '重试' }).click()
  const settled = await page.waitForFunction(() => document.querySelector('[data-telemetry]')?.getAttribute('data-tone') === '在线', null, { timeout: 8000 })
    .then(() => true, () => false)
  check('重试后全部回来：遥测点回到「在线」', settled, await telemetry.getAttribute('data-tone'))
  check('提示随之撤掉', await warn.waitFor({ state: 'detached', timeout: 3000 }).then(() => true, () => false))
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
  await ctx.close()
}

{
  // 断线时在导航上换了主题：重连后补存进设置，不能被服务端的旧值翻回去
  const state = { approvals: [], offline: true, writes: [] }
  const { ctx, page } = await open({ state })
  await page.goto(`${WEB}/chat`)
  await page.getByRole('button', { name: '先进去看看' }).click({ timeout: 12000 })
  await waitNav(page)
  await nav(page).getByRole('button', { name: /切换到.+主题/ }).click()
  const theme = () => page.evaluate(() => document.documentElement.getAttribute('data-theme'))
  check('离线时也能换主题（深色 → 浅色）', (await theme()) === 'light')
  check('提示说清楚：先在本机生效，连上后自动存', await page.getByText(/连上后端后自动存进设置/).waitFor({ timeout: 3000 }).then(() => true, () => false))
  state.offline = false
  await page.locator('main').getByRole('button', { name: '立即重试' }).first().click()
  await page.locator('main').getByText('后端未连接').waitFor({ state: 'detached', timeout: 8000 }).catch(() => {})
  await page.waitForTimeout(2500)
  check('重连后主题没被服务端的旧值翻回去', (await theme()) === 'light', await theme())
  const put = state.writes.filter((w) => w.method === 'PUT' && w.path === '/api/settings').at(-1)
  check('重连后补存进设置（PUT ui.theme = light）', !!put && JSON.parse(put.body).values.ui.theme === 'light', put?.body ?? '没有请求')
  check('没有未捕获的运行时错误', page.errors.length === 0, page.errors[0] ?? '')
  await ctx.close()
}

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 外壳检查全部通过')
process.exit(failed ? 1 : 0)
