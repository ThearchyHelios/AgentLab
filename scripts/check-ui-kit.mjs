// 基础组件（components/ui.tsx）、前端工具库（lib/*）和连接状态（store/catalog.ts）
// 的回归检查。
//
// 这几样是全站的地基，坏了不会在哪一页上报错，只会悄悄变样：弹窗的焦点漏到页面
// 上、组字时按 Esc 把半屏草稿关掉、toast 压住工具栏按钮、后端一抖整站说「还没有
// 工作流」、恢复以后页面自己的列表还停在假的空态上。页面级的 check-ui 喂的都是
// 正常路径，对这些一个字都不会说。
//
// 跑在预览页 /ui-harness.html 上（只有 dev server 有，不进生产包）。lib 用的是
// 预览页挂在 window.__ui.lib 上的那一份：和组件同一个模块实例，instanceof ApiError
// 才靠得住——从页面里另外 import 一次，vite 热更新过的模块会带 ?t= 成为第二个实例。
// 断网用 page.route 拦 /api，不停后端；非 GET 一律拦掉，不写库。
// 时间格式按 Asia/Shanghai 断言，浏览器上下文钉了这个时区，和本机设置无关。
//
// 跑之前前端得起着：./scripts/dev.sh
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const API = process.env.AGENTLAB_API ?? 'http://localhost:8000/api'
// 不用 playwright install：它既下不动也会动到已有缓存。系统 Chrome 就够了。
const CHROME = process.env.CHROME_PATH
  ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}

const browser = await chromium.launch({ executablePath: CHROME })
const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, timezoneId: 'Asia/Shanghai' })
const page = await ctx.newPage()
const isApi = (u) => new URL(u).pathname.startsWith('/api/')
await page.route(isApi, (r) => (r.request().method() === 'GET' ? r.continue() : r.abort()))
const errors = []
page.on('pageerror', (e) => errors.push(e.message))

try {
  await page.goto(`${WEB}/ui-harness.html`, { waitUntil: 'networkidle' })
  await page.waitForSelector('#badges-color', { timeout: 8000 })
} catch (e) {
  console.error(`✗ 打不开预览页（${WEB}/ui-harness.html）——前端没起？先跑 ./scripts/dev.sh\n  ${e.message}`)
  await browser.close()
  process.exit(1)
}

// 别的改动让 vite 整页刷新时，预览页的状态（心跳、catalog）全没了，后面的断言会
// 莫名其妙地挂。记下来，失败时说清楚是这个原因
let reloads = 0
page.on('load', () => { reloads++ })

const active = () => page.evaluate(() => {
  const el = document.activeElement
  return el ? `${el.tagName}${el.id ? '#' + el.id : ''}:${(el.textContent || el.getAttribute('placeholder') || '').trim().slice(0, 12)}` : 'null'
})
const dialogOpen = () => page.locator('[role="dialog"]').count()
const asking = () => page.getByText('有未保存的修改').count()
const state = () => page.evaluate(() => {
  const s = window.__ui.useCatalog.getState()
  return { backend: s.backend, err: s.backendError, retryIn: s.retryAt ? s.retryAt - Date.now() : null, reconnects: s.reconnects }
})

console.log('=== lib：格式、状态、快捷键、术语、报错 ===')
for (const [name, ok, detail] of await page.evaluate(() => {
  const { format: f, status: st, keys: k, terms: t, errors: e } = window.__ui.lib
  const { ApiError } = window.__ui
  const out = []
  const eq = (name, got, want) => out.push([name, got === want, got === want ? '' : `得到 ${JSON.stringify(got)}，应为 ${JSON.stringify(want)}`])
  const ok = (name, cond, detail = '') => out.push([name, !!cond, cond ? '' : detail])

  // 耗时：先按目标精度取整再判档（旧实现 119600 显示成「1m60s」）
  eq('820 ms', f.formatDuration(820), '820 ms')
  eq('999.6 ms 进位到秒档', f.formatDuration(999.6), '1.0 s')
  eq('7.6 s', f.formatDuration(7600), '7.6 s')
  eq('59.96 s 进位到分钟档', f.formatDuration(59_960), '1 分 00 秒')
  eq('1 分 14 秒', f.formatDuration(74_000), '1 分 14 秒')
  eq('119600 → 2 分 00 秒', f.formatDuration(119_600), '2 分 00 秒')
  eq('1 小时 02 分', f.formatDuration(3_720_000), '1 小时 02 分')
  eq('拿不到的耗时写 —', f.formatDuration(undefined), '—')
  eq('0 是 0 ms 不是 —', f.formatDuration(0), '0 ms')
  eq('计时器 mm:ss.s', f.formatClock(74_310), '01:14.3')
  eq('计时器过小时', f.formatClock(3_723_400), '1:02:03.4')
  eq('tokens 千分位', f.formatTokens(56034), '56,034 tokens')
  eq('tokens 紧凑', f.formatTokens(56034, { compact: true }), '56.0k tok')
  eq('tokens 紧凑进位到 M', f.formatTokens(999_950, { compact: true }), '1.0M tok')
  eq('成本', f.formatCost(0.0312), '$0.031')
  eq('成本不足 0.001', f.formatCost(0.0004), '<$0.001')
  eq('成本为 0', f.formatCost(0), '$0')
  eq('成本拿不到', f.formatCost(null), '—')

  // 时间：不带时区的服务器时间按 UTC
  eq('不带时区按 UTC', f.parseServerTime('2026-09-26T01:11:19.891698')?.toISOString(), '2026-09-26T01:11:19.891Z')
  eq('空格分隔也按 UTC', f.parseServerTime('2026-09-26 01:11:19')?.toISOString(), '2026-09-26T01:11:19.000Z')
  eq('小于 1e12 的数当秒', f.parseServerTime(1790385079.89)?.toISOString(), '2026-09-26T01:11:19.890Z')
  eq('解析不了是 null', f.parseServerTime('garbage'), null)
  const now = new Date('2026-09-26T02:00:00Z')   // 上海 10:00
  eq('今天只写时分', f.formatTime('2026-09-26T01:11:19', now), '09:11')
  eq('更早写月/日', f.formatTime('2026-09-25T04:52:00Z', now), '9/25 12:52')
  eq('跨年带年份', f.formatTime('2025-09-25T04:52:00Z', now), '2025/9/25 12:52')
  eq('完整时间带时区', f.formatDateTime('2026-09-26T01:11:19.891698'), '2026-09-26 09:11:19 (UTC+08:00)')
  eq('今天 / 昨天', `${f.formatDay('2026-09-26T01:11:19Z', now)}/${f.formatDay('2026-09-25T01:11:19Z', now)}`, '今天/昨天')
  const ago = (ms) => f.formatRelative(new Date(now.getTime() - ms), now)
  eq('相对时间：刚刚', ago(30_000), '刚刚')
  eq('相对时间：52 分钟前', ago(52 * 60_000), '52 分钟前')
  eq('相对时间：59.6 分钟进位到小时档', ago(59.6 * 60_000), '1 小时前')
  ok('相对时间：23.6 小时不写「24 小时前」', !/24 小时前/.test(ago(23.6 * 3_600_000)), ago(23.6 * 3_600_000))
  eq('短 id', f.shortId('66a5a6ff0011'), '#66a5a6')

  // 状态
  eq('succeeded = 已完成', st.statusLabel('succeeded'), '已完成')
  eq('interrupted + 有审批 = 等待审批', st.statusLabel('interrupted', { pendingApproval: true }), '等待审批')
  eq('interrupted + 没审批 = 已挂起', st.statusLabel('interrupted', { pendingApproval: false }), '已挂起 · 可续跑')
  eq('suspended', st.statusLabel('suspended'), '已中断（服务重启）')
  eq('未知状态原样写', st.statusLabel('weird'), 'weird')
  const shapes = new Set(['running', 'queued', 'done', 'waiting', 'failed', 'skipped', 'cancelled', 'blocked', 'unreached'].map((s) => st.statusMeta(s).shape))
  eq('九种剪影各不相同', shapes.size, 9)
  // 显示码不能直接当查询参数：waiting / held 在后端都是 interrupted
  eq('waiting → interrupted', st.serverStatusOf('waiting'), 'interrupted')
  eq('held → interrupted', st.serverStatusOf('held'), 'interrupted')
  eq('done → succeeded', st.serverStatusOf('done'), 'succeeded')
  eq('节点状态不是运行状态', st.serverStatusOf('skipped'), null)
  ok('筛选分段的每一档都查得到', st.RUN_STATUS_ORDER.every((c) => st.serverStatusOf(c)),
    st.RUN_STATUS_ORDER.filter((c) => !st.serverStatusOf(c)).join(','))

  // 快捷键：按本机平台断言
  const mac = k.isMac
  eq('Mod+Enter 的显示', k.formatShortcut('Mod+Enter'), mac ? '⌘⏎' : 'Ctrl+Enter')
  eq('Alt+V 的显示', k.formatShortcut('Alt+V'), mac ? '⌥V' : 'Alt+V')
  eq('aria-keyshortcuts：Mod 按平台', k.ariaShortcut('Mod+K'), mac ? 'Meta+K' : 'Control+K')
  eq('aria-keyshortcuts：Ctrl 写成 Control', k.ariaShortcut('Ctrl+Shift+Enter'), 'Control+Shift+Enter')
  const ev = (o) => ({ key: '', code: '', metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...o })
  ok('⌥V 的 e.key 是 √ 也认', k.matchShortcut(ev({ key: '√', code: 'KeyV', altKey: true }), 'Alt+V'))
  ok('多了 Shift 不算', !k.matchShortcut(ev({ key: 's', code: 'KeyS', [k.modKey]: true, shiftKey: true }), 'Mod+S'))
  ok('? 不强求 Shift 一致', k.matchShortcut(ev({ key: '?', code: 'Slash', shiftKey: true }), '?'))

  // 术语
  eq('human = 人工审批', t.nodeTypeLabel('human'), '人工审批')
  eq('正式运行 v3', t.runClassLabel('formal', 3), '正式运行 v3')
  eq('完整出具', t.issuanceLabel('formal'), '完整出具')

  // 报错
  const net = e.humanizeError(new ApiError(0, '连不上后端服务（可能没启动或正在重启），稍后重试', { kind: 'network', raw: 'TypeError: Failed to fetch' }))
  ok('网络失败：连不上后端服务 + 怎么办', net.title === '连不上后端服务' && net.kind === 'network' && net.action, JSON.stringify(net))
  eq('TypeError: Failed to fetch 翻成人话', e.humanizeError(new TypeError('Failed to fetch')).title, '连不上后端服务')
  ok('errorMessage 不露 Failed to fetch', !e.errorMessage(new TypeError('Failed to fetch')).includes('Failed to fetch'))
  eq('exit undefined', e.humanizeError('✕ 失败 (exit undefined)').title, '请求没到沙箱')
  const pyd = e.humanizeError("2 validation errors for GraphSpec\nnodes.1.type\n  Input should be 'input'")
  ok('pydantic 原文翻成人话，原文进 raw', pyd.title === '生成的工作流里有节点类型不认识' && pyd.raw.includes('GraphSpec'), pyd.title)
  ok('Python 异常类名不进标题', !e.humanizeError("KeyError: 'rows'").title.includes('KeyError'))
  const obj = e.humanizeError({ ok: false, error: '连不上对方的服务', hint: '核对地址和端口', detail: 'ConnectError: [Errno 61]' })
  ok('{error, hint, detail}', obj.title === '连不上对方的服务' && obj.action === '核对地址和端口' && obj.raw === 'ConnectError: [Errno 61]', JSON.stringify(obj))
  eq('超时和连不上分开说', e.humanizeError(new ApiError(0, 'x', { kind: 'network', timeoutMs: 5000 })).title, '后端没有响应')
  eq('取消不是错误', e.humanizeError(new DOMException('aborted', 'AbortError')).title, '已取消')
  // 后端回了话，话里提到 fetch failed，说的是它够不着的下游，不是我们连不上它
  const down = e.humanizeError(new ApiError(502, '模型服务连不上：httpx.ConnectError: fetch failed'))
  ok('HTTP 错误里的网络字眼不算连不上后端', down.kind === 'http' && down.title !== '连不上后端服务' && down.status === 502, JSON.stringify(down))
  ok('isNetworkError 对 HTTP 错误为假', !e.isNetworkError(new ApiError(500, 'NetworkError when attempting to fetch resource')))
  ok('测连接结果里的 ECONNREFUSED 不算连不上后端',
    e.humanizeError({ ok: false, error: 'connect ECONNREFUSED 10.0.0.5:5432' }).title !== '连不上后端服务')
  return out
})) check(name, ok, detail)

console.log('\n=== Modal：焦点、Esc、dirty、遮罩 ===')
await page.click('#open-dirty')
await page.waitForSelector('[role="dialog"]')
await page.waitForTimeout(80)
const dlg = page.locator('[role="dialog"]')
check('role=dialog + aria-modal', (await dlg.getAttribute('aria-modal')) === 'true')
const labelledby = await dlg.getAttribute('aria-labelledby')
check('aria-labelledby 指向标题', !!labelledby && (await page.locator(`[id="${labelledby}"]`).innerText()) === '编辑 Skill')
check('打开时聚焦第一个输入框', (await active()).startsWith('INPUT'), await active())
check('× 按钮有名字', (await page.locator('[role="dialog"] button[aria-label="关闭"]').count()) === 1)
let escaped = false
for (const key of [...Array(12).fill('Tab'), ...Array(6).fill('Shift+Tab')]) {
  await page.keyboard.press(key)
  if (!(await page.evaluate(() => !!document.activeElement?.closest('[role="dialog"]')))) escaped = true
}
check('Tab / Shift+Tab 焦点困在弹窗里', !escaped)
await page.keyboard.press('Escape')
await page.waitForTimeout(80)
check('没改动时 Esc 直接关', (await dialogOpen()) === 0)
check('关闭后焦点回到打开按钮', (await active()).startsWith('BUTTON#open-dirty'), await active())

await page.click('#open-dirty')
await page.waitForSelector('[role="dialog"]')
const ta = page.locator('[role="dialog"] textarea')
await ta.fill('多行指令\n第二行')
await ta.focus()
const fireKey = (loc, init) => loc.evaluate((el, init) =>
  el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init })), init)
await fireKey(ta, { key: 'Escape', isComposing: true })
await page.waitForTimeout(100)
check('组字中的 Esc（isComposing）不关也不问', (await dialogOpen()) === 1 && (await asking()) === 0)
await fireKey(ta, { key: 'Escape', keyCode: 229 })
await page.waitForTimeout(100)
check('Safari 式 keyCode=229 的 Esc 不关也不问', (await dialogOpen()) === 1 && (await asking()) === 0)
await page.keyboard.press('Escape')
await page.waitForTimeout(100)
check('dirty 时 Esc 先问', (await dialogOpen()) === 1 && (await asking()) === 1)
check('询问时焦点在「继续编辑」', (await active()).includes('继续编辑'), await active())
await page.keyboard.press('Escape')
await page.waitForTimeout(120)
check('再按 Esc = 继续编辑', (await asking()) === 0 && (await dialogOpen()) === 1)
check('焦点回到刚才的输入框，内容还在', (await active()).startsWith('TEXTAREA') && (await ta.inputValue()) === '多行指令\n第二行', await active())

// 点遮罩：按下和松开都在遮罩上才算。两个方向的拖动，click 都会派给公共祖先（遮罩）
const drag = async (from, to) => {
  await page.mouse.move(from.x, from.y)
  await page.mouse.down()
  await page.mouse.move(to.x, to.y, { steps: 4 })
  await page.mouse.up()
  await page.waitForTimeout(100)
}
const panelBox = await dlg.boundingBox()
const inPanel = { x: panelBox.x + panelBox.width / 2, y: panelBox.y + 20 }
const onBackdrop = { x: 10, y: 10 }
const taBox = await ta.boundingBox()
await drag({ x: taBox.x + 20, y: taBox.y + 10 }, onBackdrop)
check('从输入框拖到遮罩松开：不关不问', (await dialogOpen()) === 1 && (await asking()) === 0)
await drag(onBackdrop, inPanel)
check('从遮罩按下、拖进面板松开：不关不问', (await dialogOpen()) === 1 && (await asking()) === 0)
await page.mouse.click(onBackdrop.x, onBackdrop.y)
await page.waitForTimeout(100)
check('dirty 时点遮罩先问', (await asking()) === 1)
await page.getByRole('button', { name: '继续编辑' }).click()
await page.locator('[role="dialog"] button[aria-label="关闭"]').click()
await page.waitForTimeout(100)
check('dirty 时点 × 先问', (await asking()) === 1)
await page.getByRole('button', { name: '放弃修改' }).click()
await page.waitForTimeout(100)
check('放弃修改后关闭，焦点回到打开按钮', (await dialogOpen()) === 0 && (await active()).startsWith('BUTTON#open-dirty'), await active())

console.log('\n=== confirmDialog / promptDialog ===')
const result = () => page.locator('#dialog-result').innerText()
await page.click('#open-danger')
await page.waitForSelector('[role="dialog"]')
await page.waitForTimeout(80)
check('危险确认：初始焦点在「取消」', (await active()).includes('取消'), await active())
check('危险确认：列出后果', (await page.getByText('连同 4 条运行记录一起删除').count()) === 1)
await page.keyboard.press('Enter')
await page.waitForTimeout(100)
check('危险确认：回车 = 取消', (await result()) === 'false')
check('关闭后焦点回到触发按钮', (await active()).startsWith('BUTTON#open-danger'), await active())

await page.click('#open-confirm')
await page.waitForSelector('[role="dialog"]')
await page.waitForTimeout(80)
check('普通确认：初始焦点在确认按钮', (await active()).includes('发布 v4'), await active())
const confirmBox = await page.locator('[role="dialog"]').boundingBox()
await drag(onBackdrop, { x: confirmBox.x + confirmBox.width / 2, y: confirmBox.y + 20 })
check('普通确认：从遮罩拖进面板松开不关', (await dialogOpen()) === 1)
await page.keyboard.press('Enter')
await page.waitForTimeout(100)
check('回车确认', (await result()) === 'true')

await page.click('#open-require')
await page.waitForSelector('[role="dialog"]')
await page.waitForTimeout(80)
const requireBtn = page.getByRole('button', { name: '删除凭证' })
check('照抄确认：没输入时禁用、焦点在输入框', (await requireBtn.isDisabled()) && (await active()).startsWith('INPUT'), await active())
await page.keyboard.type('a3f9c')
check('照抄确认：输错仍禁用', await requireBtn.isDisabled())
await page.keyboard.type('2')
await page.keyboard.press('Enter')
await page.waitForTimeout(100)
check('照抄确认：抄对后回车确认', (await result()) === 'true')

await page.click('#open-prompt')
await page.waitForSelector('[role="dialog"]')
await page.waitForTimeout(80)
const input = page.locator('[role="dialog"] input')
check('输入框：初值带上并全选', (await input.inputValue()) === '未命名工作流 09-26 10:05'
  && await input.evaluate((el) => el.selectionStart === 0 && el.selectionEnd === el.value.length))
await input.fill('新工作流')
await page.waitForTimeout(50)
check('validate 不通过时说原因并禁用', (await page.getByText('已经有一个同名的了').count()) === 1
  && await page.getByRole('button', { name: '新建', exact: true }).isDisabled())
await input.fill('  月度经营分析 ')
await fireKey(input, { key: 'Enter', isComposing: true })
await fireKey(input, { key: 'Escape', keyCode: 229 })
await page.waitForTimeout(100)
check('组字中的回车、Esc 都不动', (await dialogOpen()) === 1)
await input.press('Enter')
await page.waitForTimeout(100)
check('回车提交，返回去掉首尾空白的名字', (await result()) === '"月度经营分析"')

// 嵌套：弹窗里再弹确认，Esc 只关一层
await page.click('#open-dirty')
await page.waitForSelector('[role="dialog"]')
await page.locator('[role="dialog"] textarea').fill('x')
await page.evaluate(() => { void window.__ui.confirmDialog({ title: '里面那层', danger: true }).then((v) => { window.__inner = v }) })
await page.waitForTimeout(150)
await page.keyboard.press('Escape')
await page.waitForTimeout(120)
check('嵌套时 Esc 只关上面那层，下面那层没被问', (await dialogOpen()) === 1
  && (await page.evaluate(() => window.__inner)) === false && (await asking()) === 0)
await page.getByRole('button', { name: '取消' }).click()
await page.waitForTimeout(80)

console.log('\n=== toast ===')
await page.evaluate(() => window.__ui.toast.dismiss())
await page.click('#toast-error')
await page.getByRole('button', { name: '普通' }).click()
await page.waitForTimeout(100)
check('出错的在 assertive 区', (await page.locator('[role="alert"][aria-live="assertive"]').innerText()).includes('保存失败'))
check('普通的在 polite 区', (await page.locator('[role="status"][aria-live="polite"]').first().innerText()).includes('已复制到剪贴板'))
check('出错的能复制', (await page.getByRole('button', { name: '复制详情' }).count()) >= 1)
// 位置：内容区（让出 56px 导航）居中，顶边在 48px 工具栏之下
const stackBox = await page.locator('[role="alert"][aria-live="assertive"]').boundingBox()
const vw = page.viewportSize().width
check('toast 在工具栏下沿之下（≥ 56px）', stackBox.y >= 56, `top=${Math.round(stackBox.y)}`)
check('toast 在内容区里居中', Math.abs(stackBox.x + stackBox.width / 2 - (56 + vw) / 2) < 4,
  `中线 ${Math.round(stackBox.x + stackBox.width / 2)}，内容区中线 ${(56 + vw) / 2}`)
const infoCard = page.getByText('已复制到剪贴板')
await infoCard.hover()
await page.waitForTimeout(4600)
check('悬停时不消失', (await infoCard.count()) === 1)
await page.mouse.move(640, 880)
await page.waitForTimeout(4400)
check('移开后约 4 秒消失', (await infoCard.count()) === 0)
check('出错的常驻', (await page.getByText('保存失败：工作流名称不能为空').count()) === 1)
await page.click('#toast-error')
await page.waitForTimeout(80)
check('同样的内容不叠，计数 ×2', (await page.getByText('保存失败：工作流名称不能为空').count()) === 1 && (await page.getByText('×2').count()) === 1)
await page.getByRole('button', { name: '关闭提示' }).first().click()
await page.waitForTimeout(80)
check('× 关掉', (await page.getByText('保存失败').count()) === 0)
await page.click('#toast-network')
await page.waitForTimeout(80)
const netText = await page.locator('[role="alert"][aria-live="assertive"]').innerText()
check('网络错误不露 Failed to fetch', netText.includes('连不上后端服务') && !netText.includes('Failed to fetch'), netText.replace(/\n/g, ' | '))
await page.getByRole('button', { name: '叠 6 条' }).click()
await page.waitForTimeout(80)
check('最多露 3 条，其余收起', (await page.getByText(/还有 \d+ 条/).count()) === 1)
await page.evaluate(() => window.__ui.toast.dismiss())

console.log('\n=== 离线：横幅、空态 ===')
const emptyBlock = page.locator('[data-block="空态 EmptyState（模拟离线时会变）"]')
await page.click('#toggle-offline')
await page.waitForTimeout(100)
check('横幅出现', (await page.getByText('后端未连接').count()) === 1)
check('空态改说拿不到数据', (await emptyBlock.getByText('暂时拿不到数据').count()) === 1)
check('离线空态不许诺「会自动刷新」', (await emptyBlock.getByText(/自动刷新/).count()) === 0)
check('离线时收起「新建」', (await page.getByRole('button', { name: '新建工作流' }).count()) === 0)
check('本地空态不受影响', (await page.getByText('没有匹配的工具').count()) === 1)
// 横幅把工具栏往下推了多少，toast 就跟着让多少
await page.click('#toast-error')
await page.waitForTimeout(80)
const bannerH = (await page.locator('[role="alert"]', { hasText: '后端未连接' }).boundingBox()).height
const offTop = (await page.locator('[role="alert"][aria-live="assertive"]').boundingBox()).y
check('离线时 toast 再让出横幅的高度', Math.abs(offTop - (56 + bannerH)) < 2, `top=${Math.round(offTop)}，横幅 ${Math.round(bannerH)}px`)
await page.evaluate(() => window.__ui.toast.dismiss())
await page.click('#toggle-offline')
await page.waitForTimeout(80)
check('恢复后横幅高度变量清掉', (await page.evaluate(() => document.documentElement.style.getPropertyValue('--offline-banner-h'))) === '')

console.log('\n=== Tabs · Field · IconButton ===')
await page.getByRole('tab', { name: '模型接入' }).focus()
await page.keyboard.press('ArrowRight')
check('→ 切到下一个标签', (await page.getByRole('tab', { name: /数据源/ }).getAttribute('aria-selected')) === 'true')
check('tabpanel 与 tab 互相指认', (await page.locator('[role="tabpanel"]').getAttribute('aria-labelledby')) === 'demo-tab-b')
check('Field：label 关联输入框', (await page.getByLabel('Base URL').count()) === 1 && (await page.getByLabel('标识').count()) === 1)
const wantAria = await page.evaluate(() => window.__ui.lib.keys.ariaShortcut('Mod+R'))
const gotAria = await page.getByRole('button', { name: '刷新列表' }).getAttribute('aria-keyshortcuts')
check('IconButton 的 aria-keyshortcuts 按平台写', gotAria === wantAria && !/Ctrl|Mod/.test(gotAria), gotAria)

console.log('\n=== 连接状态：心跳、退避、恢复 ===')
const expectRuns = await fetch(`${API}/runs?limit=3`).then((r) => r.json()).then((r) => r.length, () => null)
await page.evaluate(() => window.__ui.useCatalog.getState().refresh())
let s = await page.evaluate(() => { const s = window.__ui.useCatalog.getState(); return { backend: s.backend, latency: s.latencyMs, checks: s.checks.map((c) => c.state) } })
check('refresh 成功：backend=ok、测到延迟、6 项清单都 ok', s.backend === 'ok' && s.latency != null && s.checks.length === 6 && s.checks.every((c) => c === 'ok'), JSON.stringify(s))
const reconnects0 = (await state()).reconnects
const loads = async () => Number(await page.locator('#local-list-loads').innerText())
const count = () => page.locator('#local-list-count').innerText()

let probes = 0
await page.unroute(isApi)
await page.route(isApi, (r) => {
  if (new URL(r.request().url()).pathname === '/api/health') probes++
  return r.abort('connectionrefused')
})
await page.evaluate(() => { window.__stopHeartbeat = window.__ui.useCatalog.getState().startHeartbeat() })
// 第一次断开：refresh 六个全失败，第一个失败已经起了一次探活——只能记一笔
await page.evaluate(() => window.__ui.useCatalog.getState().refresh())
s = await state()
check('断开判成 down，原因是人话', s.backend === 'down' && s.err && !/failed to fetch/i.test(s.err), JSON.stringify(s))
check('第一次断开只探活一次', probes === 1, `探了 ${probes} 次`)
check('第一次重试在 4 秒后（不是 8 秒）', s.retryIn > 3000 && s.retryIn <= 4100, `${s.retryIn}ms`)
// 页面自己拉的列表在断开期间拿到 []
await page.click('#local-list-reload')
await page.waitForTimeout(300)
check('断开期间页面自己的列表是空的', (await count()) === '0')
await page.waitForTimeout(s.retryIn + 700)
s = await state()
check('第二次重试在 8 秒后', s.backend === 'down' && s.retryIn > 5500 && s.retryIn <= 8100 && probes === 2, `${s.retryIn}ms，探了 ${probes} 次`)
// 横幅每秒刷一次，读到的可能是上一秒的数
const shown = Number((await page.getByText(/秒后自动重试/).innerText()).match(/\d+/)?.[0])
const secs = Math.ceil(s.retryIn / 1000)
check('横幅倒计时和实际重试一致', shown === secs || shown === secs + 1, `横幅 ${shown} 秒，实际 ${secs} 秒`)
await page.waitForTimeout(s.retryIn + 700)
s = await state()
check('第三次重试在 16 秒后', s.backend === 'down' && s.retryIn > 12000 && s.retryIn <= 16100 && probes === 3, `${s.retryIn}ms，探了 ${probes} 次`)

await page.unroute(isApi)
await page.route(isApi, (r) => (r.request().method() === 'GET' ? r.continue() : r.abort()))
const loadsBefore = await loads()
await page.getByRole('button', { name: '立即重试' }).first().click()
await page.waitForTimeout(1500)
s = await state()
const checks = await page.evaluate(() => window.__ui.useCatalog.getState().checks.map((c) => c.state))
check('恢复后 backend=ok，catalog 自动 refresh', s.backend === 'ok' && checks.length === 6 && checks.every((c) => c === 'ok'), JSON.stringify(checks))
check('恢复算一次重连', s.reconnects === reconnects0 + 1, `${reconnects0} → ${s.reconnects}`)
check('useOnReconnect：页面自己的列表重拉了一次', (await loads()) === loadsBefore + 1, `${loadsBefore} → ${await loads()}`)
if (expectRuns != null) {
  check('页面自己的列表不再是假的空', (await count()) === String(expectRuns), `${await count()} 条，后端有 ${expectRuns} 条`)
}
check('横幅消失', (await page.getByText('后端未连接').count()) === 0)
await page.evaluate(() => window.__ui.useCatalog.getState().refresh())
check('连着时再 refresh 不算重连', (await state()).reconnects === reconnects0 + 1)

// 后端卡死：连得上但不回
await page.route((u) => new URL(u).pathname === '/api/health', () => { /* 永远不回 */ })
await page.route((u) => new URL(u).pathname.startsWith('/api/approvals'), (r) => r.abort('connectionrefused'))
const t0 = Date.now()
await page.evaluate(() => window.__ui.useCatalog.getState().refreshApprovals())
s = await state()
check('健康检查卡住时 5 秒超时判 down', s.backend === 'down' && Date.now() - t0 < 8000, `${JSON.stringify(s)} ${Date.now() - t0}ms`)
await page.evaluate(() => window.__stopHeartbeat())

const harnessReloaded = reloads > 0
console.log('\n=== 真实页面：toast 不压工具栏 ===')
// 工具栏是各页自己的，谁把它加高了，常驻的出错 toast 就会盖住按钮
await page.unrouteAll({ behavior: 'ignoreErrors' })
await page.route(isApi, (r) => (r.request().method() === 'GET' ? r.continue() : r.abort()))
const wf = await fetch(`${API}/workflows`).then((r) => r.json()).then((l) => l[0]?.id, () => null)
for (const width of [1280, 1180]) {
  await page.setViewportSize({ width, height: 800 })
  for (const path of ['/chat', wf ? `/studio/${wf}` : '/studio', '/runs', '/tools', '/knowledge', '/settings']) {
    await page.goto(`${WEB}${path}`, { waitUntil: 'networkidle' })
    await page.waitForTimeout(300)
    const r = await page.evaluate(() => {
      const stack = document.querySelector('[role="alert"][aria-live="assertive"]')?.parentElement
      if (!stack) return null
      const box = stack.getBoundingClientRect()
      // 从顶部 48px 那一条里起头的可点元素 = 工具栏。下面列表的行被盖住一角是
      // 浮层的本分，不算
      const hits = [...document.querySelectorAll('main button, main a, main input, main select, main [role="tab"]')]
        .map((el) => ({ el, b: el.getBoundingClientRect() }))
        .filter(({ b }) => b.width && b.height && b.top < 48 && b.bottom > box.top && b.right > box.left && b.left < box.right)
        .map(({ el }) => (el.getAttribute('aria-label') || el.textContent || el.tagName).trim().slice(0, 16))
      return { top: Math.round(box.top), hits }
    })
    check(`${width}px ${path.replace(/[0-9a-f]{32}/, ':id')}：toast 区不压工具栏`, r && r.hits.length === 0, r ? (r.hits.join('、') || `top=${r.top}`) : '页面上没有 ToastHost')
  }
}

check('没有未捕获的运行时错误', errors.length === 0, errors[0] ?? '')
if (harnessReloaded) console.log('\n！预览页中途被 vite 整页刷新过（有别的文件在改），上面的失败可能是它引起的，重跑一次')
await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 基础组件全部通过')
process.exit(failed ? 1 : 0)
