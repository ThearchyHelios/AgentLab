// 基础组件（components/ui.tsx）、前端工具库（lib/*）、连接状态（store/catalog.ts）
// 和 api/client 的上传进度的回归检查。管理页共用件（页头、连通胶囊、单选组、删除
// 后撤销）和失败说明（lib/explain）也在这里：它们从页面挪进了公共层，页面级检查
// 只走得到其中一两种情况。
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
// 跑之前前后端都得起着（./scripts/dev.sh），默认连 5273 / 8000。对别的实例（比如一份
// 沙箱拷贝）跑时带上地址：
//   AGENTLAB_WEB=http://localhost:<前端端口> AGENTLAB_API=http://localhost:<后端端口>/api node scripts/check-ui-kit.mjs
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

/**
 * 一节一节地跑：某一节里元素找不到、等待超时，只记成这一节失败，接着跑下一节，
 * 不让一处卡住把后面的检查一起吞掉。页面是各节共用的：
 * 出错那一节停在哪，下一节就从哪接着
 */
async function section(name, fn) {
  console.log(`\n=== ${name} ===`)
  try {
    await fn()
  } catch (e) {
    check(`${name} 中途出错`, false, String(e?.message ?? e).split('\n')[0])
  }
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

await section('lib：格式、状态、快捷键、术语、报错', async () => {
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
    // 跨天的跨度和相对时刻：记录页、时间轴、HUD 共用这一份（以前在 pages/runs/model 里）
    eq('formatSpan 一天以内同 formatDuration', f.formatSpan(74_000), '1 分 14 秒')
    eq('formatSpan 跨天：8 天 23 小时', f.formatSpan(8 * 86_400_000 + 23 * 3_600_000 + 5 * 60_000), '8 天 23 小时')
    eq('formatSpan 整天不写 00 小时', f.formatSpan(2 * 86_400_000 + 1000), '2 天')
    eq('formatSpan 粗粒度：8 天', f.formatSpan(8 * 86_400_000 + 23 * 3_600_000, { coarse: true }), '8 天')
    eq('formatSpan 粗粒度：3 小时', f.formatSpan(3 * 3_600_000 + 59 * 60_000, { coarse: true }), '3 小时')
    eq('formatSpan 粗粒度：不到 1 分钟', f.formatSpan(40_000, { coarse: true }), '不到 1 分钟')
    eq('formatSpan 拿不到写 —', f.formatSpan(null), '—')
    // 已等、等人的共用写法：一小时以内是钟面，一小时以上按 formatSpan（3C REQ-19）
    eq('formatLapse 一小时以内是钟面', f.formatLapse?.(1_500_300), '25:00.3')
    eq('formatLapse 不要十分位', f.formatLapse?.(1_500_300, false), '25:00')
    eq('formatLapse 过了一小时按时长写', f.formatLapse?.(3 * 3_600_000 + 5 * 60_000), '3 小时 05 分')
    eq('formatLapse 跨天：9 天 03 小时', f.formatLapse?.(9 * 86_400_000 + 3 * 3_600_000 + 34 * 60_000), '9 天 03 小时')
    eq('formatLapse 拿不到写 —', f.formatLapse?.(Number.NaN), '—')
    eq('formatOffset 一天以内是秒表读数', f.formatOffset(303_400), '05:03.4')
    eq('formatOffset 跨天：9 天 00:38:26', f.formatOffset(9 * 86_400_000 + 38 * 60_000 + 26_200), '9 天 00:38:26')
    eq('formatOffset 负数写 —', f.formatOffset(-1), '—')
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
    eq('文件大小：字节', f.formatBytes(820), '820 B')
    eq('文件大小：KB', f.formatBytes(12_700), '12.4 KB')
    eq('文件大小：MB', f.formatBytes(3.1 * 1024 * 1024), '3.1 MB')
    eq('文件大小拿不到写 —', f.formatBytes(undefined), '—')
    eq('短名去掉括号里的补充', f.shortLabel('OpenAI 兼容（DeepSeek、通义…）'), 'OpenAI 兼容')

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
    // 没挂在工作流上的运行：老数据叫「临时图」，界面上一律「未保存的工作流」（3C 返工 D3）
    eq('老数据的「临时图」叫未保存的工作流', t.runName?.({ workflow_id: null, workflow_name: '临时图' }), '未保存的工作流')
    eq('新后端的名字原样认得', t.isUnsaved?.({ workflow_id: null, workflow_name: '未保存的工作流' }), true)
    eq('没有名字也算未保存', t.runName?.({ workflow_id: null, workflow_name: '' }), '未保存的工作流')
    eq('工作流删了留下的运行：还叫原来的名字', t.runName?.({ workflow_id: null, workflow_name: 'sales_daily' }), 'sales_daily')
    eq('挂在工作流上的，名字叫临时图也照写', t.runName?.({ workflow_id: 'w1', workflow_name: '临时图' }), '临时图')

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
    // 已经是「标题：原因」的中文人话，不再套一个「操作超时」、把整句当原因（标题说两遍）
    const zh = e.humanizeError(new ApiError(504, '数据库查询超时：超过 30 秒没有返回，已中断。缩小查询范围后重试。'))
    ok('中文人话里的「超时」不套「操作超时」', zh.title !== '操作超时' && !(zh.reason ?? '').includes('操作超时')
      && !(zh.reason && zh.reason.includes(zh.title)), JSON.stringify(zh))
    eq('英文 timeout 原文照样翻成「操作超时」', e.humanizeError('ReadTimeout: timed out').title, '操作超时')
    ok('……原因里不带异常类名', !/ReadTimeout/.test(e.humanizeError('ReadTimeout: timed out').reason ?? ''))

    // 失败说明（lib/explain）：记录页、助手流、问数据页共用一份
    const x = window.__ui.lib.explain
    const leak = x.explainRunError('模型输出了工具调用的原始标记，但没有真正调用工具。常见原因：节点没有绑定工具，或者模型、服务不支持工具调用。')
    ok('工具调用标记泄漏：说人话、不给接着跑、指到画布', leak.title === '模型没有真正调用工具' && !leak.continuable && leak.fix === 'canvas', JSON.stringify(leak))
    const tired = x.explainRunError('NodeError: 协作团队用完 4 轮仍未完成：还缺汇总员的结论')
    ok('团队轮数用完：标题带轮数，原因是调度者最后的理由', tired.title === '协作团队用完 4 轮仍未完成'
      && tired.reason === '还缺汇总员的结论' && !tired.continuable, JSON.stringify(tired))
    const made = x.explainRunError('校验修复无效：修复时出现了原文没有的值：total_count=0')
    ok('校验修复编造数值：点出是哪个值、不给接着跑', made.reason.includes('total_count=0') && !made.continuable, JSON.stringify(made))
    eq('提示词点名的工具没绑定', x.explainRunError('提示词要求用 db_query__shop，但节点没有绑定它').title,
      '提示词要求用「db_query__shop」，但节点没有绑定它')
    eq('老运行的鉴权失败', x.explainRunError('NodeError: AuthenticationError: Error code: 401 - invalid_api_key').title, '模型鉴权没通过')
    const miss = x.explainRunError('缺少必填输入：region')
    ok('缺必填输入：给出是哪一项、重新发起', miss.missingInput === 'region' && miss.fix === 'rerun' && !miss.continuable)
    eq('没留原因', x.explainRunError(null).title, '运行失败，但没有留下原因')
    ok('兜底：句中的异常类名也去掉', !/ValueError/.test(x.explainRunError('结构不对：ValueError: bad').title))
    // 团队轮数用完：后端原话把「先看成员…调大最多轮数」也写进了冒号后面，原因只要理由和
    // 没派到的成员，建议交给 action——否则同样的话在报错里说两遍
    const tiredFull = x.explainRunError('NodeError: 协作团队用完 4 轮仍未完成：还没有查到任何订单数据。一次都没被派到的成员：汇总员。'
      + '先看成员有没有绑定要用的工具，再调大「最多轮数」；也可以把「用完轮数时」改成降档交付')
    eq('团队轮数用完：原因在「。先看成员」处截断', tiredFull.reason, '还没有查到任何订单数据。一次都没被派到的成员：汇总员')
    ok('……action 里有最多轮数，原因里没有', /最多轮数/.test(tiredFull.action) && !/最多轮数|降档交付/.test(tiredFull.reason), JSON.stringify(tiredFull))
    eq('团队按降档交付的原话：原因在「。按降档交付」处截断',
      x.explainRunError('协作团队用完 3 轮仍未完成：还缺结论。按降档交付：成果是成员最后的原话，不是调度者认可的结论').reason, '还缺结论')
    // 超时：后端已经给了具体原因和建议的中文原话，不能换成笼统的「下游服务没在限定时间内响应」
    const qto = x.explainRunError('KeyError: 查询超时，数据库没有在 30 秒内返回')
    ok('中文超时原话：原因照原样留下', qto.reason === '查询超时，数据库没有在 30 秒内返回'
      && !/下游服务/.test(`${qto.reason}${qto.action}`), JSON.stringify(qto))
    const qlong = x.explainRunError('查询超过 30s 没有返回，已放弃等待（数据库那边可能还在跑，连接会在后台收回）。加上 WHERE 条件或 LIMIT 缩小范围再查')
    ok('中文超时原话：后端的建议进 action，原因里不再重复', /WHERE 条件或 LIMIT/.test(qlong.action ?? '')
      && /查询超过 30s 没有返回/.test(qlong.reason ?? '') && !/WHERE/.test(qlong.reason ?? ''), JSON.stringify(qlong))
    const unified = x.explainRunError('等待超时：对方没有在限定时间内响应')
    ok('「标题：原因」式的中文超时：拆成标题和原因', unified.title === '等待超时' && unified.reason === '对方没有在限定时间内响应'
      && unified.continuable, JSON.stringify(unified))
    const en = x.explainRunError('ReadTimeout: timed out')
    ok('英文超时原文照旧给通用说法', en.title === '等待超时' && /限定时间/.test(en.reason ?? '') && !/ReadTimeout/.test(en.reason ?? ''), JSON.stringify(en))
    // 自定义工具的参数定义写坏了：先去工具页改好（直达那一项），改好之后接着跑能过
    const broken = x.explainRunError('自定义工具「lookup_order」的参数定义格式不对：参数 store 要写成 {"type": "string"} 这样的对象，'
      + '不能直接写 "string"。到「工具」页把它的参数定义改好再运行')
    ok('坏参数定义：点名是哪个工具、fix 指到工具页', broken.fix === 'tools' && broken.title.includes('lookup_order')
      && /参数 store/.test(broken.reason ?? ''), JSON.stringify(broken))
    ok('……主按钮先去修（fixFirst），直达这一项的编辑', broken.fixFirst === true && broken.continuable
      && broken.fixTo === '/tools/custom?edit=lookup_order', JSON.stringify(broken))
    ok('……不和「找不到工具」混为一谈', !/找不到/.test(broken.title))
    const brokenObj = x.explainRunError('自定义工具「lookup_order」的参数定义要是一个 JSON 对象。到「工具」页把它的参数定义改好再运行')
    ok('坏参数定义（不是对象）也认得', brokenObj.fix === 'tools' && brokenObj.fixFirst === true, JSON.stringify(brokenObj))
    // 后端把几个坏工具用「；」连成一句（tools/custom.build_tools）；单个工具的原因里自己也可能带「；」
    const typeProblem = '参数定义格式不对：参数 n 的类型「int」认不出来，是不是想写 integer；只能是 string、integer、number、boolean、array、object 里的一个'
    const brokenOne = x.explainRunError(`自定义工具「sales_daily_api」的${typeProblem}。到「工具」页把它的参数定义改好再运行`)
    ok('坏参数定义：原因里自带的「；」不把后半句切丢', brokenOne.title.includes('sales_daily_api') && /只能是 string/.test(brokenOne.reason ?? ''),
      JSON.stringify(brokenOne))
    const brokenTwo = x.explainRunError(`ToolBuildError: 自定义工具「lookup_order」的参数定义要是一个 JSON 对象。到「工具」页把它的参数定义改好再运行；`
      + `自定义工具「sales_daily_api」的${typeProblem}。到「工具」页把它的参数定义改好再运行`)
    ok('两个坏工具：标题说有几个，不只认第一个', brokenTwo.title === '2 个自定义工具的参数定义写坏了', JSON.stringify(brokenTwo))
    ok('……原因里两个都点名，各自的原因都在', /lookup_order/.test(brokenTwo.reason ?? '') && /sales_daily_api/.test(brokenTwo.reason ?? '')
      && /JSON 对象/.test(brokenTwo.reason ?? '') && /只能是 string/.test(brokenTwo.reason ?? '') && !/到「工具」页/.test(brokenTwo.reason ?? ''),
      JSON.stringify(brokenTwo))
    ok('……怎么办里也点名两个，直达第一个的编辑', /lookup_order/.test(brokenTwo.action ?? '') && /sales_daily_api/.test(brokenTwo.action ?? '')
      && brokenTwo.fixTo === '/tools/custom?edit=lookup_order' && brokenTwo.fix === 'tools' && brokenTwo.fixFirst === true && brokenTwo.continuable,
      JSON.stringify(brokenTwo))
    // 超时原话按「；」拆开时，原因的末尾不能挂着半个分号
    const semi = x.explainRunError('调用模型超时（60 秒），对方没有响应；换个时段再试')
    ok('中文超时按「；」拆：原因以句号收尾，不挂「；」', semi.reason === '调用模型超时（60 秒），对方没有响应。' && semi.action === '换个时段再试。',
      JSON.stringify(semi))
    // 3b 起查询的时限归数据源管（查询时限 query_timeout_s），不在画布的节点上
    ok('查询超时、后端没给建议：指到「数据」页的查询时限，不再说调画布里节点的超时',
      /查询时限/.test(qto.action ?? '') && /「数据」页/.test(qto.action ?? '') && /WHERE/.test(qto.action ?? '') && !/画布/.test(qto.action ?? ''),
      JSON.stringify(qto))
    return out
  })) check(name, ok, detail)
})

await section('lib/evidence：证据状态的四通道元数据、报告节点的叫法、复核守卫', async () => {
  for (const [name, ok, detail] of await page.evaluate(() => {
    const { evidence: ev, terms: t } = window.__ui.lib
    const out = []
    const eq = (name, got, want) => out.push([name, got === want, got === want ? '' : `得到 ${JSON.stringify(got)}，应为 ${JSON.stringify(want)}`])
    const ok = (name, cond, detail = '') => out.push([name, !!cond, cond ? '' : detail])
    const codes = ev.EVIDENCE_STATES
    // 方案 6.1 的八种：确定性、概率性四种、无证据、连接性、旧运行候选
    ok('八种状态一种不少', ['deterministic', 'supported', 'partial', 'unsupported', 'unjudged', 'none', 'connective', 'candidate']
      .every((c) => codes.includes(c)) && codes.length === 8, codes.join(','))
    const lines = new Set(['solid', 'dotted', 'badge', 'none'])
    const bad = codes.filter((c) => {
      const m = ev.EVIDENCE_STATE[c]
      return !m || m.code !== c || !m.label || !lines.has(m.line) || typeof m.glyph !== 'string'
        || !/^var\(--st-[\w-]+\)$/.test(m.color) || !m.decoration || !/^var\(--st-[\w-]+\)$/.test(m.soft) || !m.hint
    })
    ok('每种都有线型、字形、颜色、文字（颜色只用 --st-*）', !bad.length, bad.join(','))
    ok('下划线颜色也只取 --st-*（或透明）', codes.every((c) => {
      const d = ev.EVIDENCE_STATE[c].decoration
      return d === 'transparent' || /var\(--st-[\w-]+\)/.test(d)
    }), codes.map((c) => ev.EVIDENCE_STATE[c].decoration).join(' | '))
    const pairs = codes.map((c) => `${ev.EVIDENCE_STATE[c].line}/${ev.EVIDENCE_STATE[c].glyph}`)
    ok('去掉颜色也分得开：线型 + 字形两两不同', new Set(pairs).size === codes.length, pairs.join(' '))
    const labels = codes.map((c) => ev.EVIDENCE_STATE[c].label)
    ok('文字两两不同', new Set(labels).size === codes.length, labels.join(' '))
    eq('确定性：细实线、有出处', `${ev.EVIDENCE_STATE.deterministic.line}/${ev.EVIDENCE_STATE.deterministic.label}`, 'solid/有出处')
    eq('无证据：点状线、?、无证据', `${ev.EVIDENCE_STATE.none.line}/${ev.EVIDENCE_STATE.none.glyph}/${ev.EVIDENCE_STATE.none.label}`, 'dotted/?/无证据')
    ok('概率性的「有依据」不用确定性的绿', ev.EVIDENCE_STATE.supported.color !== ev.EVIDENCE_STATE.deterministic.color)
    eq('本期只会出现确定性和无证据', codes.filter((c) => ev.EVIDENCE_STATE[c].phase === 1).join(','), 'deterministic,none')
    ok('只有异常态醒目（进 n / N 的跳转）', !ev.EVIDENCE_STATE.deterministic.alert && ev.EVIDENCE_STATE.none.alert)
    eq('片段状态：deterministic', ev.segmentState({ kind: 'number', state: 'deterministic' }), 'deterministic')
    eq('片段状态：结构片段不画', ev.segmentState({ kind: 'structural', state: 'none' }), null)
    eq('片段状态：文字不画', ev.segmentState({ kind: 'text', state: 'neutral' }), null)
    eq('片段状态：probabilistic 裁判前按未裁判画', ev.segmentState({ kind: 'text', state: 'probabilistic' }), 'unjudged')

    eq('报告节点叫「报告撰写」', t.nodeTypeLabel('report'), '报告撰写')
    eq('计数的说法：无证据 = 总数 − 有出处，算得平', t.evidenceTally(7, 12), '7/12 数字有出处 · 无证据 5')
    eq('不是数字的解析不了的引用另起一句', t.evidenceTally(7, 12, 1), '7/12 数字有出处 · 无证据 5 · 另有 1 处引用解析不了')
    eq('数字全有出处', t.evidenceTally(1234, 1234, 0), '1,234 个数字都有出处')
    eq('数字全有出处、另有引用解析不了', t.evidenceTally(3, 3, 2), '3 个数字都有出处 · 另有 2 处引用解析不了')
    eq('一个数字都没有、只有解析不了的引用', t.evidenceTally(0, 0, 1), '1 处引用解析不了')
    eq('什么都没有时给空串', t.evidenceTally(0, 0, 0), '')

    const out1 = { 周报: 'x', _evidence: { report_node: 'write', doc_artifact: 'abc', fields: ['周报'],
      others: [{ report_node: 'w2', doc_artifact: 'def', fields: ['附录'] }] } }
    const f = ev.evidenceFields(out1)
    ok('_evidence 的字段和 others 都认', f.get('周报')?.artifact === 'abc' && f.get('附录')?.report === 'w2', JSON.stringify([...f]))
    eq('缺 doc_artifact 的标注当没有', ev.evidenceFields({ _evidence: { fields: ['a'] } }).size, 0)
    const review = { verdict: 'rewritten', note: '有缺口', answer: '改写后的', original: '原文', retry: false, severity: 'degraded', signals: [] }
    const guarded = ev.guardReview(review, out1)
    ok('成果带证据：复核只加说明，不改写', guarded.answer === null && guarded.original === null && guarded.verdict === 'annotated'
      && guarded.note === '有缺口', JSON.stringify(guarded))
    ok('成果不带证据：复核照旧可以改写', ev.guardReview(review, { answer: 'x' }).answer === '改写后的')
    eq('出处不唯一的说法', ev.matchedSource({ token: '0', metric: null, candidates: ['a', 'b'], ambiguous: true }).text, '出处不唯一：候选 a、b')
    eq('出处唯一的照写指标', ev.matchedSource({ token: '12', metric: 'gmv' }).text, 'gmv')
    const cp = ev.codePointIndex('📦 15 单')
    eq('码点偏移换成 UTF-16：emoji 后面差一位', cp(2), 3)
    eq('没有 emoji 时原样', ev.codePointIndex('abc')(2), 2)
    const doc = window.__ui.evidenceFixture.doc
    const tally = ev.docTally(doc)
    const k = (x) => `${x.cited}/${x.total}/${x.none}/${x.other}/${x.hidden}/${x.structural}/${x.noSegment}`
    eq('文档计数（有出处/总数/无证据/非数字引用/画不了线/其中结构/其中没对应字）', k(tally), '7/12/5/1/1/1/0')
    const { stats: _s, ...bare } = doc
    eq('没有 stats 时从片段和违规清单数，口径一样', k(ev.docTally(bare)), '7/12/5/1/1/1/0')
    const { violations: _v, ...noList } = doc
    eq('有 stats、没有违规清单：非数字引用由 stats 反推', `${ev.docTally(noList).none}/${ev.docTally(noList).other}`, '5/1')
    eq('只有 stats（report.checked 的载荷）：同一种算法', JSON.stringify(ev.statsTally(doc.stats)),
      JSON.stringify({ total: 12, cited: 7, none: 5, other: 1 }))
    eq('载荷缺字段：不给计数', ev.statsTally({ uncited_numbers: 1 }), null)
    // 句末依据里写错的引用：违规不指任何片段，和列表序号里的数字分开数、分开说
    const withSee = { ...doc, stats: { ...doc.stats, unresolved: 4, violations: 7 },
      violations: [...doc.violations, { code: 'unresolved_ref', message: '依据 [[see:m:nope]] 解析不了', ref: 'm:nope', unit: 'u1' }] }
    const t2 = ev.docTally(withSee)
    eq('句末依据的引用：算进非数字引用、算进「没对应字」', k(t2), '7/12/5/2/2/1/1')
    const sum = ev.tallySummary(t2, true)
    ok('读屏摘要：两种画不了线的分开说在哪', sum.includes('其中 1 个数字在列表序号') && sum.includes('1 处在正文里没有对应的字')
      && sum.includes('句末依据'), sum)
    ok('读屏摘要：只有结构片段时不提「句末依据」', !ev.tallySummary(tally, true).includes('句末依据'), ev.tallySummary(tally, true))

    // 封存那一行：顺序是 正文不是封存那份 → 没拿到 → 没封存 → 核对失败 → 不在范围 → 通过
    const sv = (seal, opts) => { const v = ev.sealVerdict(seal, opts); return `${v.status}:${v.label}` }
    eq('封存被改过（covered 必然也是 false）：失败，不是「不在封存范围内」', sv({ sealed: true, ok: false, covered: false }),
      'failed:已封存 · 核对不一致')
    eq('没封存：灰「尚未封存」', sv({ sealed: false, ok: null, covered: false }), 'idle:尚未封存')
    eq('封存完好、这件不在台账里：琥珀', sv({ sealed: true, ok: true, covered: false }), 'waiting:不在封存范围内')
    eq('都过了：绿', sv({ sealed: true, ok: true, covered: true }), 'done:已封存 · 核对一致')
    ok('正文不是封存的那份：再完好的封存也判失败', sv({ sealed: true, ok: true, covered: true }, { foreign: true }).startsWith('failed:'))
    ok('没有证据的片段：落到报告文档上', sv({ sealed: true, ok: true, covered: true }, { docOnly: true }).includes('报告文档已封存'))
    ok('链没取到、封存状态从证据图查的：只说文档封存了', sv({ sealed: true, ok: true }, { viaGraph: true }).includes('证据链这次没取到'))
    eq('逐项复核：没封存时不报「不在封存范围内」', ev.integrityFailures({ sealed: false, hash_ok: true }, { sealed: false }).join(','), '')
    eq('逐项复核：封存被改过时也不报', ev.integrityFailures({ sealed: false }, { sealed: true, ok: false }).join(','), '')
    eq('逐项复核：封存完好时照报', ev.integrityFailures({ sealed: false, hash_ok: false }, { sealed: true, ok: true }).join(','), 'hash,sealed')

    // 证据接口答的是不是正文这份
    const seg = { id: 's8', text: '8.7%' }
    const mine = { artifact: 'aaa', node: 'write', seg }
    eq('同一份：不算外来', ev.docForeign({ report: { doc_artifact: 'aaa', node_id: 'write' }, segment: { id: 's8', text: '8.7%' } }, mine), null)
    ok('文档工件 id 不同：外来', !!ev.docForeign({ report: { doc_artifact: 'bbb' }, segment: { id: 's8', text: '8.7%' } }, mine))
    eq('这个位置的字不同：外来，并记下封存那份写的是什么',
      ev.docForeign({ report: { doc_artifact: 'aaa' }, segment: { id: 's8', text: '9.9%' } }, mine)?.sealedText, '9.9%')
    eq('接口没报文档 id（老后端）：只比字', ev.docForeign({ segment: { text: '8.7%' } }, mine), null)
    eq('证据图兜底：封存的报告里有正文这份', ev.graphDoc({ reports: [{ doc_artifact: 'aaa' }] }, 'aaa'), 'ok')
    eq('证据图兜底：没有正文这份', ev.graphDoc({ reports: [{ doc_artifact: 'bbb' }] }, 'aaa'), 'foreign')
    eq('证据图兜底：有，但哈希对不上', ev.graphDoc({ reports: [{ doc_artifact: 'aaa', hash_ok: false }] }, 'aaa'), 'tampered')
    return out
  })) check(name, ok, detail)
})

await section('lib/actor：本机署名', async () => {
  for (const [name, ok, detail] of await page.evaluate(() => {
    const a = window.__ui.lib.actor
    const out = []
    const ok = (name, cond, detail = '') => out.push([name, !!cond, cond ? '' : detail])
    const saved = localStorage.getItem(a.ACTOR_KEY)
    let heard = 0
    const stop = a.subscribeActor(() => { heard++ })
    try {
      localStorage.removeItem(a.ACTOR_KEY)
      ok('没署名是 null', a.localActor() === null, JSON.stringify(a.localActor()))
      localStorage.setItem(a.ACTOR_KEY, '   ')
      ok('只有空白也算没署名', a.localActor() === null, JSON.stringify(a.localActor()))
      a.setLocalActor('  张工 ')
      ok('写入时去掉首尾空白，读回来一致', a.localActor() === '张工' && localStorage.getItem(a.ACTOR_KEY) === '张工')
      ok('写入会通知同一标签页的订阅者', heard === 1, `通知了 ${heard} 次`)
      a.setLocalActor('')
      ok('写空串就是清掉', a.localActor() === null && localStorage.getItem(a.ACTOR_KEY) === null)
    } finally {
      stop()
      if (saved == null) localStorage.removeItem(a.ACTOR_KEY); else localStorage.setItem(a.ACTOR_KEY, saved)
    }
    return out
  })) check(name, ok, detail)

  if (process.env.UI_KIT_ONLY === 'lib') {
    await browser.close()
    console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ lib 全部通过（UI_KIT_ONLY=lib，只跑了这一段）')
    process.exit(failed ? 1 : 0)
  }
})

const fireKey = (loc, init) => loc.evaluate((el, init) =>
  el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init })), init)
// 点遮罩：按下和松开都在遮罩上才算。两个方向的拖动，click 都会派给公共祖先（遮罩）
const drag = async (from, to) => {
  await page.mouse.move(from.x, from.y)
  await page.mouse.down()
  await page.mouse.move(to.x, to.y, { steps: 4 })
  await page.mouse.up()
  await page.waitForTimeout(100)
}
const onBackdrop = { x: 10, y: 10 }
await section('Modal：焦点、Esc、dirty、遮罩', async () => {
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

  const panelBox = await dlg.boundingBox()
  const inPanel = { x: panelBox.x + panelBox.width / 2, y: panelBox.y + 20 }
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
})

await section('confirmDialog / promptDialog', async () => {
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
})

await section('toast', async () => {
  await page.evaluate(() => window.__ui.toast.dismiss())
  await page.click('#toast-error')
  await page.getByRole('button', { name: '普通' }).click()
  await page.waitForTimeout(100)
  check('出错的在 assertive 区', (await page.locator('[role="alert"][aria-live="assertive"]').innerText()).includes('保存失败'))
  check('普通的在 polite 区', (await page.locator('[role="status"][aria-live="polite"]').first().innerText()).includes('已复制到剪贴板'))
  check('出错的能复制', (await page.getByRole('button', { name: '复制详情' }).count()) >= 1)
  // 位置：内容区（让出 64px 的导航，App 里的 w-16）居中，顶边在 48px 工具栏之下
  const NAV_W = 64
  const stackBox = await page.locator('[role="alert"][aria-live="assertive"]').boundingBox()
  const vw = page.viewportSize().width
  check('toast 在工具栏下沿之下（≥ 56px）', stackBox.y >= 56, `top=${Math.round(stackBox.y)}`)
  check('toast 在内容区里居中', Math.abs(stackBox.x + stackBox.width / 2 - (NAV_W + vw) / 2) < 2,
    `中线 ${Math.round(stackBox.x + stackBox.width / 2)}，内容区中线 ${(NAV_W + vw) / 2}`)
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
})

await section('离线：横幅、空态', async () => {
  const emptyBlock = page.locator('[data-block="空态 EmptyState（模拟离线时会变）"]')
  await page.click('#toggle-offline')
  await page.waitForTimeout(100)
  check('横幅出现', (await page.getByText('后端未连接').count()) === 1)
  // 横幅早就挂着（连着时什么都不画），计时器是这一刻才走起来的：头一个数得按此刻算
  const firstSecs = Number((await page.getByText(/秒后自动重试/).innerText()).match(/\d+/)?.[0])
  check('刚断开时倒计时从此刻算（8 秒）', firstSecs === 8 || firstSecs === 7, `横幅 ${firstSecs} 秒`)
  check('空态改说拿不到数据', (await emptyBlock.getByText('暂时拿不到数据').count()) >= 1)
  check('离线空态不许诺「会自动刷新」', (await emptyBlock.getByText(/自动刷新/).count()) === 0)
  check('离线时收起「新建」', (await page.getByRole('button', { name: '新建工作流' }).count()) === 0)
  check('本地空态不受影响', (await page.getByText('没有匹配的工具').count()) === 1)
  // role=alert 整块重念：倒计时一秒一跳，放在里面读屏就一秒念一遍整条横幅
  const bannerAlert = page.locator('[data-offline-banner] [role="alert"]')
  check('横幅外层不是 role=alert', (await page.locator('[data-offline-banner]').getAttribute('role')) === null)
  check('横幅的播报区只有不变的那几句', (await bannerAlert.count()) === 1
    && (await bannerAlert.innerText()).includes('后端未连接') && !/秒后自动重试/.test(await bannerAlert.innerText()),
    await bannerAlert.innerText().catch(() => ''))
  check('倒计时对读屏隐藏', (await page.getByText(/秒后自动重试/).getAttribute('aria-hidden')) === 'true')
  // 横幅把工具栏往下推了多少，toast 就跟着让多少
  await page.click('#toast-error')
  await page.waitForTimeout(80)
  const bannerH = (await page.locator('[data-offline-banner]').boundingBox()).height
  const offTop = (await page.locator('[role="alert"][aria-live="assertive"]').boundingBox()).y
  check('离线时 toast 再让出横幅的高度', Math.abs(offTop - (56 + bannerH)) < 2, `top=${Math.round(offTop)}，横幅 ${Math.round(bannerH)}px`)
  await page.evaluate(() => window.__ui.toast.dismiss())
  await page.click('#toggle-offline')
  await page.waitForTimeout(80)
  check('恢复后横幅高度变量清掉', (await page.evaluate(() => document.documentElement.style.getPropertyValue('--offline-banner-h'))) === '')
})

await section('Tabs · Field · IconButton', async () => {
  await page.getByRole('tab', { name: '模型接入' }).focus()
  await page.keyboard.press('ArrowRight')
  check('→ 切到下一个标签', (await page.getByRole('tab', { name: /数据源/ }).getAttribute('aria-selected')) === 'true')
  check('tabpanel 与 tab 互相指认', (await page.locator('[role="tabpanel"]').getAttribute('aria-labelledby')) === 'demo-tab-b')
  check('Field：label 关联输入框', (await page.getByLabel('Base URL').count()) === 1 && (await page.getByLabel('标识').count()) === 1)
  const wantAria = await page.evaluate(() => window.__ui.lib.keys.ariaShortcut('Mod+R'))
  const gotAria = await page.getByRole('button', { name: '刷新列表' }).getAttribute('aria-keyshortcuts')
  check('IconButton 的 aria-keyshortcuts 按平台写', gotAria === wantAria && !/Ctrl|Mod/.test(gotAria), gotAria)
})

await section('管理页共用件：页头、连通胶囊、单选组、删除后撤销', async () => {
  check('页头 48px 高', Math.round((await page.locator('#page-header-demo header').boundingBox()).height) === 48)
  const pills = await page.locator('#health-demo [data-health]').evaluateAll((els) => els.map((el) => el.textContent))
  // 「3 分钟前」是从预览页挂载时算的，跑到这里可能已经过了一分钟
  check('连通胶囊五种说法', pills[0] === '未测试' && /^正在测 · /.test(pills[1]) && /^已连通 · 42 ms · [34] 分钟前测$/.test(pills[2])
    && pills[3] === '连不上 · 1 小时前测' && /已连通 · 380 ms.*配置改过了/.test(pills[4]), pills.join(' | '))
  check('不知道测的时刻：不留一个悬空的「 · 」', pills[5] === '连不上', JSON.stringify(pills[5]))
  const spoken = await page.locator('#health-demo [role="status"]').evaluateAll((els) => els.map((el) => el.textContent))
  check('读屏那一句不带跳动的计时和相对时间', spoken[1] === '正在测连接' && /^已连通 42 ms，\d\d:\d\d 测的$/.test(spoken[2]), spoken.join(' | '))

  const radios = page.locator('#radio-demo [role="radio"]')
  check('单选组只占一个 Tab 位', JSON.stringify(await radios.evaluateAll((els) => els.map((el) => el.tabIndex))) === '[0,-1,-1]')
  const checkedRadio = () => page.locator('#radio-demo [role="radio"][aria-checked="true"]').innerText()
  const focusedRadio = () => page.evaluate(() => document.activeElement?.textContent)
  await radios.first().focus()
  await page.keyboard.press('ArrowRight')
  check('→ 选中下一项并聚焦', (await checkedRadio()) === 'sid' && (await focusedRadio()) === 'sid')
  await page.keyboard.press('End')
  check('End 到最后一项', (await checkedRadio()) === 'dsn')
  await page.keyboard.press('ArrowDown')
  check('↓ 首尾相接', (await checkedRadio()) === 'service_name')
  await page.keyboard.press('ArrowUp')
  await page.keyboard.press('Home')
  check('↑ 往回、Home 到第一项', (await checkedRadio()) === 'service_name' && (await focusedRadio()) === 'service_name')
  await page.keyboard.press('Alt+ArrowRight')
  check('带 Alt 的方向键不接（那是浏览器后退 / 前进）', (await checkedRadio()) === 'service_name')
  await page.getByRole('tab', { name: '模型接入' }).focus()
  await page.keyboard.press('End')
  check('标签页：End 到最后一个', (await page.getByRole('tab', { name: '偏好' }).getAttribute('aria-selected')) === 'true')
  await page.keyboard.press('Home')

  let deletes = 0
  page.on('request', (r) => { if (r.method() === 'DELETE') deletes++ })
  const rowShown = (id) => page.locator(`#defer-demo [data-row="${id}"]`).count()
  const undoBtn = page.locator('[role="status"][aria-live="polite"]').getByRole('button', { name: '撤销' })
  await page.getByRole('button', { name: '删除 orders' }).click()
  await page.waitForTimeout(80)
  check('删除：行先拿掉，toast 给「撤销」', (await rowShown('orders')) === 0
    && (await page.getByText('已删除「orders」').count()) === 1 && (await undoBtn.count()) === 1)
  await page.click('#defer-reload')
  await page.waitForTimeout(50)
  check('撤销窗口里重拉列表，删掉的行不回来', (await rowShown('orders')) === 0)
  await undoBtn.click()
  await page.waitForTimeout(80)
  check('撤销：行回来了，一个 DELETE 都没发', (await rowShown('orders')) === 1 && deletes === 0)
  await page.evaluate(() => window.__ui.toast.dismiss())
})

await section('空态：catalog 的表没取回来时不说「还没有」', async () => {
  const sourceEmpty = page.locator('#empty-source-demo')
  await page.evaluate(() => window.__ui.useCatalog.getState().refresh())
  check('取回来了：照常说「还没有工作流」、给新建', (await sourceEmpty.getByText('还没有工作流').count()) === 1
    && (await sourceEmpty.locator('#empty-source-action').count()) === 1)
  const realCatalog = await page.evaluate(() => {
    const s = window.__ui.useCatalog.getState()
    return { loadedAt: s.loadedAt, checks: s.checks }
  })
  await page.evaluate(() => window.__ui.useCatalog.setState({
    loadedAt: {}, checks: [{ key: 'workflows', label: '工作流', state: 'pending' }],
  }))
  await page.waitForTimeout(50)
  check('从没取回来过、还在路上：说「正在读取工作流」、收起新建', (await sourceEmpty.getByText('正在读取工作流').count()) === 1
    && (await sourceEmpty.locator('#empty-source-action').count()) === 0
    && (await sourceEmpty.locator('[data-empty-unknown="loading"]').getAttribute('role')) === 'status')
  await page.evaluate(() => window.__ui.useCatalog.setState({
    checks: [{ key: 'workflows', label: '工作流', state: 'error', error: '后端 15 秒没有响应，稍后重试' }],
  }))
  await page.waitForTimeout(50)
  check('超时或报错：说「工作流没取回来」和原因、给重新读取', (await sourceEmpty.getByText('工作流没取回来').count()) === 1
    && (await sourceEmpty.getByText(/15 秒没有响应.*这里显示为空不代表没有数据/).count()) === 1
    && (await sourceEmpty.getByRole('button', { name: '重新读取' }).count()) === 1
    && (await sourceEmpty.locator('#empty-source-action').count()) === 0)
  await page.evaluate((c) => window.__ui.useCatalog.setState(c), realCatalog)
})

let cat
await section('catalog：一张表卡住不拖累别的表', async () => {
  // /tools 连得上但不回（MCP 服务不应答时就是这样）：以前 Promise.all 等它，工作流也一直空着
  const toolsBefore = await page.evaluate(() => window.__ui.useCatalog.getState().tools.length)
  // unroute 要拿同一个函数引用才摘得掉
  const isTools = (u) => new URL(u).pathname === '/api/tools'
  await page.route(isTools, () => { /* 永远不回 */ })
  const expectWf = await fetch(`${API}/workflows`).then((r) => r.json()).then((l) => l.length, () => null)
  await page.evaluate(() => window.__ui.useCatalog.setState({ workflows: [] }))
  const tHang = Date.now()
  let settled = false
  const hung = page.evaluate(() => window.__ui.useCatalog.getState().refresh()).then(() => { settled = true })
  await page.waitForFunction(() => window.__ui.useCatalog.getState().workflows.length > 0, null, { timeout: 8000 }).catch(() => {})
  cat = await page.evaluate(() => {
    const s = window.__ui.useCatalog.getState()
    return { wf: s.workflows.length, wfAt: s.loadedAt.workflows, tools: s.checks.find((c) => c.key === 'tools')?.state }
  })
  if (expectWf) {
    check('工作流先回来先填，不等卡住的工具', cat.wf === expectWf && cat.wfAt >= tHang && cat.tools === 'pending',
      `${JSON.stringify(cat)}，后端有 ${expectWf} 个工作流`)
  }
  // 最多等 20 秒：没有超时的话 refresh 永远不落定，检查自己也不能跟着挂死
  await Promise.race([hung, new Promise((r) => setTimeout(r, 20_000))])
  const hangMs = Date.now() - tHang
  check('refresh 在超时之后落定', settled, settled ? `${hangMs}ms` : `${hangMs}ms 还没落定`)
  cat = await page.evaluate(() => {
    const s = window.__ui.useCatalog.getState()
    const c = s.checks.find((x) => x.key === 'tools')
    return { state: c?.state, error: c?.error, backend: s.backend, tools: s.tools.length, loaded: s.loaded }
  })
  check('卡住的那张 15 秒超时，记成出错并说清', cat.state === 'error' && /15 秒/.test(cat.error ?? '') && hangMs > 14_500 && hangMs < 18_000,
    `${cat.state} ${cat.error} ${hangMs}ms`)
  check('一张表超时不算后端断开', cat.backend === 'ok', cat.backend)
  check('超时不清空手上已有的那份', cat.tools === toolsBefore, `${toolsBefore} → ${cat.tools}`)
  await page.unroute(isTools)
})

await section('catalog：数据源目录与单表重拉', async () => {
  // 数据源的查询工具不在 /api/tools 里：检查器挑工具、问数据的范围、助手的数据源提示都从
  // catalog 的 datasources 取一份。数据页改了它就 reload('datasources')，别处立刻跟上
  const isSources = (u) => new URL(u).pathname === '/api/datasources'
  const fakeSource = (n) => ({ id: `ui-kit-${n}`, name: `demo_${n}`, kind: 'sqlite', host: null, port: null, database: '/tmp/demo.db',
    username: null, options: {}, readonly: true, description: '', enabled: true, tools: [`db_query__demo_${n}`, `db_schema__demo_${n}`] })
  let sourceRows = [fakeSource(1)]
  let sourceHits = 0
  await page.route(isSources, (r) => { sourceHits++; return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(sourceRows) }) })
  await page.evaluate(() => window.__ui.useCatalog.getState().refresh())
  cat = await page.evaluate(() => {
    const s = window.__ui.useCatalog.getState()
    return { n: s.datasources.length, at: s.loadedAt.datasources, check: s.checks.find((c) => c.key === 'datasources')?.label }
  })
  check('refresh 顺带取回数据源，启动清单里有「数据源」一项', cat.n === 1 && cat.at > 0 && cat.check === '数据源', JSON.stringify(cat))
  sourceRows = [fakeSource(1), fakeSource(2)]
  const hitsBefore = sourceHits
  await page.evaluate(() => window.__ui.useCatalog.getState().reload('datasources'))
  cat = await page.evaluate(() => {
    const s = window.__ui.useCatalog.getState()
    return { names: s.datasources.map((d) => d.name).join(','), checks: s.checks.length }
  })
  check('reload 只重拉这一张表', cat.names === 'demo_1,demo_2' && sourceHits === hitsBefore + 1, `${cat.names} · 请求 ${sourceHits - hitsBefore} 次`)
  await page.unroute(isSources)
  await page.route(isSources, (r) => r.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: '数据库锁住了' }) }))
  await page.evaluate(() => window.__ui.useCatalog.getState().reload('datasources'))
  cat = await page.evaluate(() => {
    const s = window.__ui.useCatalog.getState()
    return { n: s.datasources.length, backend: s.backend }
  })
  check('reload 失败时列表保持原样，也不判后端断开', cat.n === 2 && cat.backend === 'ok', JSON.stringify(cat))
  // 交叠：先发的慢请求后到，不能盖掉后发、先到的新结果
  await page.unroute(isSources)
  let slowFirst = true
  await page.route(isSources, async (r) => {
    const slow = slowFirst
    slowFirst = false
    if (slow) await new Promise((res) => setTimeout(res, 600))
    return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(slow ? [fakeSource(9)] : [fakeSource(3)]) })
  })
  await page.evaluate(() => {
    const s = window.__ui.useCatalog.getState()
    window.__slowReload = s.reload('datasources')
    return new Promise((r) => setTimeout(r, 50)).then(() => s.reload('datasources'))
  })
  await page.evaluate(() => window.__slowReload)
  cat = await page.evaluate(() => window.__ui.useCatalog.getState().datasources.map((d) => d.name).join(','))
  check('两次 reload 交叠：后发的结果不被先发、后到的盖掉', cat === 'demo_3', cat)
  await page.unroute(isSources)
  // useDatasources（检查器、问数据页的输入框挂它）：过期才拉，同一时刻几处挂载只发一个请求；
  // 启动的 refresh 落定前不拉，落定时这张表没取回来就补拉一次——以前只看 maxAge，要等重新挂载
  let hookHits = 0
  await page.route(isSources, (r) => { hookHits++; return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([fakeSource(1)]) }) })
  const mountSources = async (n) => { await page.evaluate((n) => window.__ui.mountDatasources(n), n); await page.waitForTimeout(400) }
  await page.evaluate(() => {
    const c = window.__ui.useCatalog
    c.setState({ loaded: true, loadedAt: { ...c.getState().loadedAt, datasources: Date.now() - 60_000 } })
  })
  hookHits = 0
  await mountSources(2)
  check('useDatasources：目录过期时两处同时挂载，只发一个请求', hookHits === 1, `${hookHits} 次`)
  await mountSources(0)
  hookHits = 0
  await mountSources(2)
  check('……刚取过、没过期：挂载不拉', hookHits === 0, `${hookHits} 次`)
  await mountSources(0)
  await page.evaluate(() => {
    const c = window.__ui.useCatalog
    const { datasources: _, ...rest } = c.getState().loadedAt
    c.setState({ loaded: false, loadedAt: rest })
  })
  hookHits = 0
  await mountSources(2)
  const hitsBeforeLoaded = hookHits
  await page.evaluate(() => window.__ui.useCatalog.setState({ loaded: true }))
  await page.waitForTimeout(400)
  check('……启动的 refresh 落定前不拉；落定时这张表没取回来，已挂着的补拉一次',
    hitsBeforeLoaded === 0 && hookHits === 1, `落定前 ${hitsBeforeLoaded} 次，落定后共 ${hookHits} 次`)
  await mountSources(0)
  await page.unroute(isSources)
  await page.evaluate(() => window.__ui.useCatalog.getState().reload('datasources'))
})

await section('后端的机读码跟着 ApiError 走', async () => {
  // {detail, code}：detail 是给人看的话、随时会改写，按种类分支要认 code（3C REQ-20）。伪造响应，不碰后端
  const isGen = (u) => new URL(u).pathname === '/api/copilot/generate'
  await page.route(isGen, (r) => r.fulfill({ status: 400, contentType: 'application/json',
    body: JSON.stringify({ detail: '这一轮圈定的库一个也找不到了', code: 'datasource_scope_empty' }) }))
  const coded = await page.evaluate(async () => {
    try { await window.__ui.api.copilot.generate({ instruction: '探针', datasource_ids: ['__gone__'] }); return null }
    catch (e) { return { isApi: e instanceof window.__ui.ApiError, status: e.status, code: e.code ?? null, message: e.message } }
  })
  check('ApiError 带上后端的 code，message 仍是那句话', coded?.isApi && coded.status === 400 && coded.code === 'datasource_scope_empty'
    && coded.message === '这一轮圈定的库一个也找不到了', JSON.stringify(coded))
  await page.unroute(isGen)
})

await section('FastAPI 422：校验错误说中文，英文原文收进详情', async () => {
  // pydantic 的 msg 是英文、loc 是内部键名，原样拼出来就是「name：Field required」（验收 NEW）。
  // 样例照沙箱后端真实回的 422 摘的，伪造响应，不碰后端
  const detailOf = {
    '/api/providers': [{ type: 'missing', loc: ['body', 'name'], msg: 'Field required', input: {} }],
    '/api/datasources': [
      { type: 'string_pattern_mismatch', loc: ['body', 'name'], msg: "String should match pattern '^[a-z][a-z0-9_]{0,40}$'", input: 'X', ctx: { pattern: '^[a-z][a-z0-9_]{0,40}$' } },
      { type: 'int_parsing', loc: ['body', 'port'], msg: 'Input should be a valid integer, unable to parse string as an integer', input: 'abc' },
    ],
    '/api/memory': [{ type: 'less_than_equal', loc: ['body', 'importance'], msg: 'Input should be less than or equal to 1', input: 3, ctx: { le: 1.0 } }],
    '/api/conversations': [{ type: 'literal_error', loc: ['body', 'kind'], msg: "Input should be 'chat' or 'canvas'", input: 'x', ctx: { expected: "'chat' or 'canvas'" } }],
    '/api/workflows': [{ type: 'some_future_type', loc: ['body', 'graph', 'nodes', 0, 'shape'], msg: 'Something went wrong in English', input: 1 }],
    '/api/copilot/generate-stream': [{ type: 'string_too_short', loc: ['body', 'instruction'], msg: 'String should have at least 1 character', input: '', ctx: { min_length: 1 } }],
  }
  const is422 = (u) => Object.hasOwn(detailOf, new URL(u).pathname)
  await page.route(is422, (r) => r.fulfill({ status: 422, contentType: 'application/json',
    body: JSON.stringify({ detail: detailOf[new URL(r.request().url()).pathname] }) }))
  const got = await page.evaluate(async () => {
    const { api, ApiError, streamCopilot } = window.__ui
    const { errors } = window.__ui.lib
    const call = async (fn) => {
      try { await fn(); return null } catch (e) {
        const h = errors.humanizeError(e)
        return { isApi: e instanceof ApiError, status: e.status, message: e.message, raw: e.raw ?? '', arr: Array.isArray(e.detail),
                 title: h.title, reason: h.reason ?? '', hraw: h.raw ?? '' }
      }
    }
    const stream = await new Promise((resolve) => {
      if (!streamCopilot) return resolve(null)
      streamCopilot({ instruction: '' }, () => {}, (error, info) => resolve({ error: error ?? '', status: info?.status }))
    })
    return {
      provider: await call(() => api.providers.create({})),
      source: await call(() => api.datasources.create({ name: 'X', kind: 'sqlite', port: 'abc' })),
      memory: await call(() => api.memory.add({ content: 'x', importance: 3 })),
      conv: await call(() => api.conversations.create({ kind: 'x' })),
      wf: await call(() => api.workflows.create({ name: 'x' })),
      stream,
    }
  })
  await page.unroute(is422)
  // 英文原文里的词（Field required、String should…、Input should…）一个都不能出现在给人看的话里
  const english = /Field required|should|Input|valid|String|Something|went wrong/
  const p = got.provider
  check('缺必填：字段按表单叫法、说「没有填」', p?.message === '提交的内容不符合要求：「名称」没有填', JSON.stringify(p?.message))
  check('……humanizeError 拆成标题和原因，英文原文进 raw（「详情」里看得到）',
    p?.title === '提交的内容不符合要求' && p.reason === '「名称」没有填' && /Field required/.test(p.hraw) && p.arr, JSON.stringify(p))
  const s = got.source
  check('同一个键在不同表单上叫法不同：数据源的 name 是「标识」；格式不对、要填整数逐条说',
    s?.message === '提交的内容不符合要求：「标识」格式不对；「端口」要填整数', JSON.stringify(s?.message))
  check('数值上限：不能大于 1（1.0 不写成 1.0）', got.memory?.message === '提交的内容不符合要求：「重要度」不能大于 1', JSON.stringify(got.memory?.message))
  check('只能取几个值之一：把可选值列出来', got.conv?.message === '提交的内容不符合要求：「类型」只能取 chat 或 canvas', JSON.stringify(got.conv?.message))
  const w = got.wf
  check('没见过的错误类型、查不到叫法的嵌套字段：写字段路径 + 笼统说法，不露英文',
    /「graph\.nodes\.0\.shape」不符合要求/.test(w?.message ?? '') && !english.test(w?.message ?? ''), JSON.stringify(w?.message))
  check('……英文原文还在 raw 里', /Something went wrong in English/.test(w?.raw ?? ''), (w?.raw ?? '').slice(0, 80))
  for (const [k, v] of Object.entries(got)) {
    const said = k === 'stream' ? v?.error : `${v?.message ?? ''} ${v?.title ?? ''} ${v?.reason ?? ''}`
    check(`${k}：给人看的话里没有英文原文`, !!said && !english.test(said), said)
  }
  check('Copilot 流式接口开流前被 422 拒：onEnd 的话同样说中文', got.stream?.status === 422
    && got.stream.error === '提交的内容不符合要求：「输入的内容」不能为空', JSON.stringify(got.stream))

  const unit = await page.evaluate(() => {
    const v = window.__ui.lib.validation
    if (!v) return null
    const d = (type, loc, extra = {}) => ({ type, loc, msg: 'English original', input: null, ...extra })
    const one = (item, path) => v.describeValidation([item], path)[0]
    return {
      query: one(d('int_parsing', ['query', 'limit']), '/runs'),
      pathParam: one(d('missing', ['path', 'run_id']), '/runs/x'),
      json: one(d('json_invalid', ['body', 1], { ctx: { error: 'Expecting value' } }), '/providers'),
      bodyMissing: one(d('missing', ['body']), '/providers'),
      zhValue: one(d('value_error', ['body', 'graph'], { msg: 'Value error, 节点 id 只能包含字母数字、下划线和连字符' }), '/workflows'),
      enValue: one(d('value_error', ['body', 'name'], { msg: 'Value error, bad name' }), '/providers'),
      tooLong: one(d('string_too_long', ['body', 'name'], { ctx: { max_length: 100 } }), '/skills'),
      listItem: one(d('dict_type', ['body', 'examples', 0]), '/skills'),
      gt: one(d('greater_than', ['body', 'importance'], { ctx: { gt: 0 } }), '/memory'),
      manyChoices: one(d('literal_error', ['body', 'run_class'], { ctx: { expected: "'a', 'b', 'c', 'd', 'e', 'f', 'g' or 'h'" } }), '/runs'),
      enumInts: one(d('enum', ['body', 'level'], { ctx: { expected: '1, 2 or 3' } }), '/x'),
      bool: one(d('bool_parsing', ['body', 'readonly']), '/datasources'),
      unknownPath: one(d('missing', ['body', 'foo_bar']), '/nowhere'),
    }
  })
  check('lib/validation 挂在预览页上', !!unit)
  if (unit) {
    const want = {
      query: '参数「limit」要填整数',
      pathParam: '地址里的「run_id」没有填',
      json: '提交的内容不是合法的 JSON',
      bodyMissing: '没有收到提交的内容',
      zhValue: '「工作流」：节点 id 只能包含字母数字、下划线和连字符',
      enValue: '「名称」取值不对',
      tooLong: '「名称」太长，最多 100 个字符',
      listItem: '「示例」第 1 项格式不对，要是一组键值',
      gt: '「重要度」要大于 0',
      manyChoices: '「运行类别」不在允许的取值里',
      enumInts: '「level」只能取 1、2 或 3',
      bool: '「只读」只能是「是」或「否」',
      unknownPath: '「foo_bar」没有填',
    }
    for (const [k, w] of Object.entries(want)) {
      check(`describeValidation · ${k}：${w}`, unit[k] === w, unit[k] === w ? '' : `得到 ${JSON.stringify(unit[k])}`)
    }
  }
})

await section('上传进度（kb.upload 走 XHR）', async () => {
  const isUpload = (u) => new URL(u).pathname === '/api/kb/upload'
  const uploadVia = async (handler) => {
    await page.unroute(isUpload)
    await page.route(isUpload, handler)
  }
  const tryUpload = (size) => page.evaluate(async (size) => {
    const events = []
    try {
      const doc = await window.__ui.api.kb.upload(new File([new Uint8Array(size)], 'orders.csv', { type: 'text/csv' }), 'default',
        { onProgress: (p) => events.push(p) })
      return { events, doc }
    } catch (e) {
      return { events, err: { isApi: e instanceof window.__ui.ApiError, status: e.status, kind: e.kind, message: e.message } }
    }
  }, size)
  // 上传进度只有真的走网络才有（page.route 直接 fulfill 时浏览器不发进度事件）。
  // 转给 dev server 上一个不存在的地址：字节真发出去，回 404，什么都不写
  await uploadVia((r) => r.continue({ url: `${WEB}/__upload_probe__` }))
  let up = await tryUpload(300_000)
  const lastUp = up.events.at(-1)
  check('字节发完报 sent，已发 = 总数，且不小于文件大小', !!lastUp?.sent && lastUp.loaded === lastUp.total && lastUp.total >= 300_000,
    JSON.stringify(up.events.slice(-2)))
  check('sent 只在最后', up.events.filter((p) => p.sent).length === 1 && !up.events.slice(0, -1).some((p) => p.sent))
  check('非 JSON 的错误响应：http 类的 ApiError', up.err?.isApi && up.err.kind === 'http' && up.err.status === 404, JSON.stringify(up.err))
  await uploadVia((r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ id: 'doc_demo', collection: 'default', title: 'orders.csv', source: 'upload', mime: 'text/csv', chunk_count: 0, status: 'processing' }),
  }))
  up = await tryUpload(10)
  check('成功：返回值照常解析', up.doc?.id === 'doc_demo', JSON.stringify(up.err ?? up.doc))
  await uploadVia((r) => r.fulfill({
    status: 409, contentType: 'application/json', body: JSON.stringify({ detail: '同名文档已经在处理：等它处理完再传' }),
  }))
  up = await tryUpload(10)
  check('后端拒绝：和 fetch 那条路一样的 ApiError，message 是 detail', up.err?.isApi && up.err.status === 409
    && up.err.kind === 'http' && up.err.message === '同名文档已经在处理：等它处理完再传', JSON.stringify(up.err))
  await uploadVia((r) => r.abort('connectionrefused'))
  up = await tryUpload(10)
  check('连不上：network 类的 ApiError', up.err?.isApi && up.err.kind === 'network' && up.err.status === 0, JSON.stringify(up.err))
  await page.unroute(isUpload)
  // 网络失败会顺手探一次活，等它落定再往下，免得下一段的探活计数多一次
  await page.evaluate(() => window.__ui.useCatalog.getState().checkBackend())
})

const harnessReloaded = reloads > 0
await section('连接状态：心跳、退避、恢复', async () => {
  const expectRuns = await fetch(`${API}/runs?limit=3`).then((r) => r.json()).then((r) => r.length, () => null)
  await page.evaluate(() => window.__ui.useCatalog.getState().refresh())
  let s = await page.evaluate(() => { const s = window.__ui.useCatalog.getState(); return { backend: s.backend, latency: s.latencyMs, checks: s.checks.map((c) => c.state) } })
  check('refresh 成功：backend=ok、测到延迟、7 项清单都 ok', s.backend === 'ok' && s.latency != null && s.checks.length === 7 && s.checks.every((c) => c === 'ok'), JSON.stringify(s))
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
  // 第一次断开：refresh 七个全失败，第一个失败已经起了一次探活——只能记一笔
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
  check('恢复后 backend=ok，catalog 自动 refresh', s.backend === 'ok' && checks.length === 7 && checks.every((c) => c === 'ok'), JSON.stringify(checks))
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

})
await section('真实页面：toast 不压工具栏', async () => {
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
})
await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 基础组件全部通过')
process.exit(failed ? 1 : 0)
