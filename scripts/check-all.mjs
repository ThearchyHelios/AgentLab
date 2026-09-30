// 分道并行跑全部检查，汇总成一张表，给一个总的退出码。
//
//   node scripts/check-all.mjs                   全部跑一遍（默认 8 道并行）
//   node scripts/check-all.mjs ui runs           只跑 check-ui、check-runs（名字去掉 check- 前缀）
//   node scripts/check-all.mjs --repeat 2        整套连跑两遍，列出只在某一遍失败的项（偶发）
//   node scripts/check-all.mjs --lanes 1         一个接一个地跑（以前的跑法）；也可以写 CHECK_LANES=1
//   CHECK_DATA_SRC=<数据目录> node scripts/check-all.mjs --stacks 2 --lanes 12
//                                                推荐：从当前工作区另起 2 套专给检查用的「后端 + vite」，
//                                                12 道分在上面跑，边改代码边跑也不受热更新打扰（见下）
//
// 默认连 5273 / 8000。对别的实例（比如一份沙箱拷贝）跑时带上地址，会原样传给每个检查：
//   AGENTLAB_WEB=http://localhost:<前端端口> AGENTLAB_API=http://localhost:<后端端口>/api node scripts/check-all.mjs
// 每个检查的完整输出写进 CHECK_LOGS（默认 /tmp/agentlab-checks/<时间>/），终端只给汇总。
//
// 默认 8 道并行（CHECK_LANES 或 --lanes 改）。各检查对后端只读（几处 POST 都是纯计算的排版接口），
// 截图各有各的目录，时间大多花在固定等待和开关浏览器上，CPU 闲着一大半，所以并行几乎是线性提速。
// 代价是大家挤同一台机器、同一个 vite 开发服务器，有几段还量时间（计时器走没走、动效多少毫秒播完、
// 取景动画落没落地）。三道防线：
// - 最慢的两项（run-states、canvas-fx）按段拆成几份（SPLIT），分到不同的道上。量时间的那几份
//   同一时刻最多跑其中一份；其余的段不量时间，和别的检查一样随便排。汇总表里仍按原检查名合并成一行；
// - 并行时没过的，全部跑完后再单独、一个接一个地重跑一遍（拆开的只重跑没过的那一份）。重跑通过
//   的算通过，但单列成「偶发」：它在挤的时候会挂，迟早也会在别处挂。连跑几遍（--repeat）时不重跑，
//   要的就是原始的偶发率；
// - --stacks N：另起 N 套隔离的「后端 + vite」，各道轮流分到各套上，见下。
//
// 关浏览器：系统 Chrome（154 起）只要开过浏览器上下文，browser.close() 就要干等 4 秒多才退出，
// 而 run-states、canvas-fx 每一段都是开一个浏览器、查完关掉——光这一项，run-states 就从 5 分多钟
// 降到 3 分钟、canvas-fx 从 4 分 40 秒降到 2 分钟。
// 所以默认给各检查的 CHROME_PATH 套一层转发：Playwright 发来「关浏览器」时替 Chrome 应答，直接
// SIGTERM 它（几十毫秒退干净），其余消息原样转发，检查本身一行不改。CHECK_FAST_CLOSE=0 关掉。
//
// --stacks N（或 CHECK_STACKS=N）：从当前工作区另起 N 套「后端 + vite」，只给这一次检查用，跑完
// 连同临时目录一起收掉（出错、Ctrl+C 也收）。好处是边改代码边跑也不受影响：后端不带 --reload，
// vite 不监听文件、不开热更新（AGENTLAB_VITE_NO_WATCH，见 frontend/vite.config.ts），起来后先把
// 页面加载一遍，之后测的就是那一刻的代码；几道分摊到几个 vite 上，也不再挤一个。
// - CHECK_DATA_SRC：数据目录，必填。每套拷一份到 /tmp 下的临时目录（APFS 上是写时复制，一秒内），
//   不碰原目录；
// - CHECK_PYTHON：后端解释器，默认 ~/miniforge3/envs/agentlab/bin/python；
// - 端口由系统分配空闲的，避开 5273 / 8000 / 5373 / 8100；后端去掉模型密钥的环境变量，
//   seed 不会拿会话里的密钥去种 provider；后端、vite 的输出也在 CHECK_LOGS 里。
// 带 --stacks 时 AGENTLAB_WEB / AGENTLAB_API 不用。
//
// 各检查自己的段过滤（CHECK_ONLY、ONLY、THEMES……）不往下传：整套检查就是整套，只跑某几段
// 请直接跑那个脚本。
//
// 退出码：0 全部通过；1 有检查没通过、中途崩了、超时或一项都没跑；2 前后端连不上（或另起的
// 那几套没起来），一项都没跑。
import { spawn } from 'node:child_process'
import {
  chmodSync, constants as fsConstants, cpSync, createWriteStream, existsSync, mkdirSync, mkdtempSync, openSync,
  readFileSync, readdirSync, rmSync, writeFileSync,
} from 'node:fs'
import { createServer } from 'node:net'
import { homedir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = dirname(HERE)
const SYSTEM_CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

// 先跑快的、不开浏览器的，再按页面从外壳往里跑；最后是连真数据的两项
const ORDER = [
  'tokens',          // 令牌、别名、原生对话框：纯静态扫源码
  'copy',            // 界面文案的禁用写法：纯静态扫前后端源码
  'trace',           // 运行态内核的纯函数
  'decode',          // 事件翻译层
  'canvas-layout',   // 走线
  'ui-kit',          // 公共组件和 lib
  'guards',          // 输入法、未知节点、错误边界
  'shell',           // 外壳：导航、徽标、命令面板、离开前确认、启动页
  'stream',          // 回答流的渲染
  'evidence',        // 可点击证据：报告逐段渲染、证据面板、旧契约按位置标记
  'run-states',      // 节点卡的运行态
  'canvas-fx',       // 画布表层的运行态
  'studio',          // 编排页的编辑
  'publish',         // 发布前检查与自动修复：发布弹窗、问题面板
  'chat',            // 问数据
  'runs',            // 记录
  'manage',          // 数据、工具、知识、设置
  'ui',              // 各页面端到端，连真数据
]
// 各项的耗时（秒）。只决定排队顺序：长的先开跑，短的填空，结果不受影响。新加的检查不在表里就按 60 秒算；
// 拆开的两项看 SPLIT 里各份的 est。2026-09-29 八道并行、关浏览器不干等之后的实测
const ESTIMATE = {
  stream: 173, chat: 160, studio: 149, manage: 145, runs: 112, publish: 92, shell: 87,
  evidence: 85, 'ui-kit': 70, ui: 61, guards: 8, 'canvas-layout': 2, copy: 2, tokens: 1, trace: 1, decode: 1,
}
// 最慢的两项按段拆成几份。用的是它们自己的段过滤：最后一份用 *_SKIP 兜住「其余全部」，以后新加的段
// 不用改这里也跑得到（拆出去的那几份要是一段都没对上，那一份会报「一项都没跑」）。
// timing 的几份量时间：计时器走没走、动效多少毫秒内播出来又停掉、取景动画落没落地、减少动效时
// 一帧到位——机器一挤就可能读早了。它们同一时刻最多跑其中一份，这就是以前「run-states、canvas-fx
// 互不重叠」的意思，只是收窄到真正量时间的段；其余的段只看结构和文字，挤不挤都一样
const FX_TIMING = ['运行中：胶囊', '失败：接着跑', '跑完：执行路径', '打开宽图', 'Copilot 搭图', 'Copilot 这一轮删掉', '系统关了动效']
const SPLIT = {
  'run-states': [
    { part: 'themes', env: { RUN_STATES_ONLY: 'themes' }, timing: true, est: 86 },
    { part: 'interact,exits,reduced', env: { RUN_STATES_ONLY: 'interact,exits,reduced' }, timing: true, est: 45 },
    { part: '其余', env: { RUN_STATES_SKIP: 'themes,interact,exits,reduced' }, est: 60 },
  ],
  'canvas-fx': [
    { part: '量时间的段', env: { FX_ONLY: FX_TIMING.join(',') }, timing: true, est: 52 },
    { part: '其余', env: { FX_SKIP: FX_TIMING.join(',') }, est: 72 },
  ],
}
const TIMEOUT_MS = Number(process.env.CHECK_TIMEOUT_MS ?? 15 * 60_000)
// 各检查只跑其中几段、只跑一套主题的开关。壳里留着一个 ONLY=… 就会让整套检查悄悄只跑一小截，
// 却照样报「全部通过」，所以一律不传给子进程
const FILTERS = ['CHECK_ONLY', 'ONLY', 'THEMES', 'FX_ONLY', 'FX_SKIP', 'STUDIO_ONLY', 'RUN_STATES_ONLY', 'RUN_STATES_SKIP',
  'UI_KIT_ONLY', 'CHECK_THEME', 'EVIDENCE_ONLY', 'PUBLISH_ONLY', 'RUNS_ONLY']
// 另起的后端不拿会话里的模型密钥：seed 见到它们会种一个真的 provider
const SECRETS = ['ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'OPENAI_API_KEY']
// 另起的几套不许占这几个口：开发（5273 / 8000）和沙箱（5373 / 8100）
const RESERVED_PORTS = new Set([5273, 8000, 5373, 8100])
// 新加的 check-*.mjs 忘了写进 ORDER 就永远不会跑：列出来提醒
const unlisted = readdirSync(HERE)
  .map((f) => f.match(/^check-(.+)\.mjs$/)?.[1])
  .filter((n) => n && n !== 'all' && !ORDER.includes(n))

const args = process.argv.slice(2)
let repeat = 1
let lanes = Math.max(1, Number(process.env.CHECK_LANES) || 8)
let stackCount = Math.max(0, Number(process.env.CHECK_STACKS) || 0)
const picked = []
for (let i = 0; i < args.length; i++) {
  const [flag, inline] = args[i].split('=')
  const value = () => Number(inline ?? args[++i])
  if (flag === '--repeat') repeat = Math.max(1, value() || 1)
  else if (flag === '--lanes') lanes = Math.max(1, value() || 1)
  else if (flag === '--stacks') stackCount = Math.max(0, value() || 0)
  else picked.push(args[i].replace(/^check-/, '').replace(/\.mjs$/, ''))
}
const unknown = picked.filter((n) => !ORDER.includes(n))
if (unknown.length) {
  console.error(`认不出的检查：${unknown.join('、')}。可选的有：${ORDER.join(' ')}`)
  process.exit(2)
}
const names = picked.length ? ORDER.filter((n) => picked.includes(n)) : ORDER
// 纯静态的几项不用起服务
const STATIC = new Set(['tokens', 'copy'])
const needsServers = names.some((n) => !STATIC.has(n))

// ---------------------------------------------------------------- 收尾
//
// 自己拉起来的东西（另起的几套服务、正在跑的检查、临时目录）登记在这里。正常跑完、出错、
// Ctrl+C、被 kill 都走一遍；进程退出前只能做同步的事，所以这里一律同步

const cleanups = []
let cleaned = false
function cleanup() {
  if (cleaned) return
  cleaned = true
  for (const fn of cleanups.reverse()) {
    try { fn() } catch { /* 收尾时能收多少收多少 */ }
  }
}
process.on('exit', cleanup)
for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
  process.on(sig, () => {
    console.error(`\n收到 ${sig}：停掉检查和另起的服务，删掉临时目录`)
    cleanup()
    process.exit(130)
  })
}
for (const ev of ['uncaughtException', 'unhandledRejection']) {
  process.on(ev, (e) => {
    console.error(e)
    cleanup()
    process.exit(1)
  })
}
const running = new Set()   // 正在跑的检查子进程：被中断时一起停掉
cleanups.push(() => { for (const c of running) c.kill('SIGTERM') })

const TMP = mkdtempSync('/tmp/agentlab-check-')
cleanups.push(() => rmSync(TMP, { recursive: true, force: true }))

const now = new Date()
const pad = (n) => String(n).padStart(2, '0')
const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
const LOGS = process.env.CHECK_LOGS ?? join('/tmp/agentlab-checks', stamp)
mkdirSync(LOGS, { recursive: true })

const baseEnv = { ...process.env }
for (const k of FILTERS) delete baseEnv[k]

// ---------------------------------------------------------------- 关浏览器不干等

/**
 * 套在真 Chrome 外面的转发：Playwright 用 fd 3 发、fd 4 收（--remote-debugging-pipe），消息以 \0
 * 分隔。见到 Browser.close 就不转发，替 Chrome 应答、再 SIGTERM 它——Chrome 收到 Browser.close
 * 之后连 SIGTERM 也一起晾着，一样要等满 4 秒。应答要插在 Chrome 两条消息之间：它正写到一半就
 * 等这条写完再插，不然 Playwright 那头的分帧就乱了。管道用 net.Socket 读写：fs 流读管道会占住
 * 线程池，进程退不掉
 */
const RELAY = `import { spawn } from 'node:child_process'
import { Socket } from 'node:net'
const child = spawn(process.argv[2], process.argv.slice(3), { stdio: ['ignore', 'inherit', 'inherit', 'pipe', 'pipe'] })
const fromPw = new Socket({ fd: 3, readable: true, writable: false })
const toPw = new Socket({ fd: 4, readable: false, writable: true })
for (const s of [fromPw, toPw, child.stdio[3], child.stdio[4]]) s.on('error', () => {})
let tail = ''
let ack = null        // 等着插进去的应答
let sealed = false    // 应答已经发出，Chrome 之后说的都不再转
let between = true    // 转给 Playwright 的最后一个字节是 \\0：正好在两条消息之间
const seal = () => {
  toPw.write(ack)
  sealed = true
  child.kill('SIGTERM')
}
fromPw.on('data', (chunk) => {
  if (ack) return
  const parts = (tail + chunk.toString('latin1')).split('\\0')
  tail = parts.pop()
  for (const m of parts) {
    const hit = m.includes('"method":"Browser.close"') && /"id":(-?\\d+)/.exec(m)
    if (hit) {
      ack = '{"id":' + hit[1] + ',"result":{}}\\0'
      if (between) seal()
      else setTimeout(() => { if (!sealed) seal() }, 1000)   // 兜底：这条迟迟写不完就不等了
      return
    }
    child.stdio[3].write(Buffer.from(m + '\\0', 'latin1'))
  }
})
fromPw.on('end', () => child.stdio[3].end())
child.stdio[4].on('data', (chunk) => {
  if (sealed) return
  if (ack) {
    const i = chunk.indexOf(0)
    if (i < 0) return void toPw.write(chunk)
    toPw.write(chunk.subarray(0, i + 1))
    return seal()
  }
  toPw.write(chunk)
  between = chunk[chunk.length - 1] === 0
})
child.on('exit', (code, sig) => {
  const done = () => process.exit(code ?? (sig ? 0 : 1))
  toPw.end(done)
  setTimeout(done, 500)
})
for (const s of ['SIGTERM', 'SIGINT', 'SIGHUP']) process.on(s, () => child.kill(s))
`
const sq = (s) => `'${String(s).replace(/'/g, `'\\''`)}'`
let chromePath = process.env.CHROME_PATH ?? SYSTEM_CHROME
if (process.env.CHECK_FAST_CLOSE !== '0' && existsSync(chromePath)) {
  writeFileSync(join(TMP, 'chrome-relay.mjs'), RELAY)
  const wrapper = join(TMP, 'chrome')
  writeFileSync(wrapper, `#!/bin/sh\nexec ${sq(process.execPath)} ${sq(join(TMP, 'chrome-relay.mjs'))} ${sq(chromePath)} "$@"\n`)
  chmodSync(wrapper, 0o755)
  chromePath = wrapper
}
baseEnv.CHROME_PATH = chromePath

// ---------------------------------------------------------------- 另起的几套服务

const reach = async (url, want) => {
  try {
    const r = await fetch(url, { signal: AbortSignal.timeout(5000) })
    if (!r.ok) return `HTTP ${r.status}`
    return want && !(await r.text()).includes(want) ? '答话的不是 agentlab' : ''
  } catch (e) {
    return e.cause?.code ?? e.name ?? String(e)
  }
}
const takenPorts = new Set()
function freePort() {
  return new Promise((resolvePort, reject) => {
    const srv = createServer()
    srv.on('error', reject)
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address()
      srv.close(() => {
        if (RESERVED_PORTS.has(port) || takenPorts.has(port)) freePort().then(resolvePort, reject)
        else { takenPorts.add(port); resolvePort(port) }
      })
    })
  })
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const tailOf = (file, n = 6) => {
  try { return readFileSync(file, 'utf8').split('\n').filter(Boolean).slice(-n).join('\n    ') } catch { return '' }
}

/** 起一个服务：自成一个进程组，收尾时整组杀掉（uvicorn、vite 自己再拉的子进程也在组里） */
function launch(cmd, argv, { cwd, env, log }) {
  const fd = openSync(log, 'a')
  const child = spawn(cmd, argv, { cwd, env, detached: true, stdio: ['ignore', fd, fd] })
  child.exited = new Promise((r) => child.on('exit', r))
  let dead = false
  child.on('exit', () => { dead = true })
  child.isDead = () => dead
  const kill = (sig) => { if (!dead) { try { process.kill(-child.pid, sig) } catch { /* 已经没了 */ } } }
  child.stop = async () => {
    kill('SIGTERM')
    await Promise.race([child.exited, sleep(5000)])
    kill('SIGKILL')
  }
  cleanups.push(() => kill('SIGKILL'))
  return child
}

async function waitUntil(label, url, want, child, log, ms) {
  const end = Date.now() + ms
  for (;;) {
    if (child.isDead()) throw new Error(`${label}没起来，进程退出了：\n    ${tailOf(log)}`)
    if (!(await reach(url, want))) return
    if (Date.now() > end) throw new Error(`${label} ${Math.round(ms / 1000)} 秒内没就绪（${url}）：\n    ${tailOf(log)}`)
    await sleep(300)
  }
}

async function startStack(i, dataSrc, python) {
  const dir = join(TMP, `stack-${i}`)
  // 写时复制地拷一份数据目录。-shm 是正在用它的进程的共享内存索引，不拷：打开拷贝时按 -wal 重建
  cpSync(dataSrc, join(dir, 'data'), { recursive: true, mode: fsConstants.COPYFILE_FICLONE, filter: (p) => !p.endsWith('-shm') })
  const [apiPort, webPort] = [await freePort(), await freePort()]
  const WEB = `http://127.0.0.1:${webPort}`
  const API = `http://127.0.0.1:${apiPort}/api`
  const env = { ...baseEnv }
  for (const k of SECRETS) delete env[k]

  // 后端照 scripts/dev.sh 的起法，只是不带 --reload
  const backendLog = join(LOGS, `stack-${i}.backend.log`)
  const backend = launch(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(apiPort)], {
    cwd: join(ROOT, 'backend'), log: backendLog,
    env: {
      ...env, PYTHONUNBUFFERED: '1', AGENTLAB_DATA_DIR: join(dir, 'data'), AGENTLAB_HOST: '127.0.0.1', AGENTLAB_PORT: String(apiPort),
      AGENTLAB_CORS_ORIGINS: JSON.stringify([WEB, `http://localhost:${webPort}`]),
    },
  })
  await waitUntil(`第 ${i} 套后端`, `${API}/health`, '"agentlab"', backend, backendLog, 90_000)

  const viteLog = join(LOGS, `stack-${i}.vite.log`)
  const vite = launch(process.execPath, [join(ROOT, 'frontend', 'node_modules', 'vite', 'bin', 'vite.js'),
    '--port', String(webPort), '--strictPort', '--host', '127.0.0.1'], {
    cwd: join(ROOT, 'frontend'), log: viteLog,
    env: { ...env, AGENTLAB_PORT: String(apiPort), AGENTLAB_VITE_NO_WATCH: '1', AGENTLAB_VITE_CACHE_DIR: join(dir, 'vite') },
  })
  await waitUntil(`第 ${i} 套 vite`, `${WEB}/`, '', vite, viteLog, 60_000)
  return { i, WEB, API, backend, vite }
}

/**
 * 把几个入口页各加载一遍：vite 按需转译，第一个打开页面的检查不用替大家等转译。每套的依赖在
 * 自己的缓存目录里从头预构建（关了热更新，指纹和平时的开发服务器不一样），预构建赶上页面加载
 * 引起的整页重载也发生在这里，不落在检查中途
 */
async function warmUp(stacks) {
  const { chromium } = await import(join(ROOT, 'frontend', 'node_modules', 'playwright-core', 'index.mjs'))
  const browser = await chromium.launch({ executablePath: chromePath })
  try {
    await Promise.all(stacks.map(async (s) => {
      const page = await browser.newPage()
      for (const path of ['/', '/studio', '/preview.html', '/ui-harness.html']) {
        await page.goto(`${s.WEB}${path}`, { waitUntil: 'networkidle', timeout: 60_000 })
      }
      await page.close()
    }))
  } finally {
    await browser.close()
  }
}

let stacks = []
if (stackCount > 0 && needsServers) {
  const dataSrc = process.env.CHECK_DATA_SRC && resolve(process.env.CHECK_DATA_SRC)
  const python = process.env.CHECK_PYTHON ?? join(homedir(), 'miniforge3', 'envs', 'agentlab', 'bin', 'python')
  const problems = []
  if (!dataSrc) problems.push('--stacks 要知道拿哪份数据：用 CHECK_DATA_SRC=<数据目录> 指定（里面有 agentlab.db 的那一层）')
  else if (!existsSync(join(dataSrc, 'agentlab.db'))) problems.push(`CHECK_DATA_SRC=${dataSrc} 里没有 agentlab.db`)
  if (!existsSync(python)) problems.push(`找不到后端解释器 ${python}，用 CHECK_PYTHON 指定`)
  if (problems.length) {
    for (const p of problems) console.error(`✗ ${p}`)
    process.exit(2)
  }
  const t0 = Date.now()
  console.log(`另起 ${stackCount} 套后端 + vite（数据拷自 ${dataSrc}）……`)
  try {
    stacks = await Promise.all(Array.from({ length: stackCount }, (_, k) => startStack(k + 1, dataSrc, python)))
    await warmUp(stacks)
  } catch (e) {
    console.error(`✗ ${String(e?.message ?? e)}`)
    process.exit(2)
  }
  for (const s of stacks) console.log(`  第 ${s.i} 套：前端 ${s.WEB} · 后端 ${s.API}`)
  console.log(`  ${Math.round((Date.now() - t0) / 1000)} 秒就绪，日志 ${LOGS}/stack-*.log`)
} else {
  const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
  const API = process.env.AGENTLAB_API ?? 'http://localhost:8000/api'
  // 连不上就别跑：十几个检查挨个报「打不开页面」只会淹没真正的原因
  if (needsServers) {
    const [web, api] = await Promise.all([reach(WEB), reach(`${API}/health`)])
    if (web || api) {
      if (web) console.error(`✗ 前端连不上：${WEB}（${web}）`)
      if (api) console.error(`✗ 后端连不上：${API}/health（${api}）`)
      console.error('  先把前后端起来（./scripts/dev.sh），或者用 AGENTLAB_WEB / AGENTLAB_API 指到要检查的那一份，或者带上 --stacks')
      process.exit(2)
    }
  }
  stacks = [{ i: 0, WEB, API }]
  console.log(`前端 ${WEB} · 后端 ${API}`)
}
const dropped = FILTERS.filter((k) => process.env[k])
if (dropped.length) console.log(`没传给各检查的段过滤：${dropped.map((k) => `${k}=${process.env[k]}`).join(' ')}（只跑某几段请直接跑那个脚本）`)

// ---------------------------------------------------------------- 排队与跑

// 一道一次跑一份。并行时最慢的两项拆开；一个接一个地跑时拆开没有好处，整项跑
const split = lanes > 1
const jobs = names.flatMap((name) => (split && SPLIT[name]
  ? SPLIT[name].map((p) => ({ name, part: p.part, env: p.env, timing: !!p.timing, est: p.est ?? 60 }))
  : [{ name, part: '', env: {}, timing: false, est: ESTIMATE[name] ?? 60 }]))
lanes = Math.min(lanes, jobs.length)
console.log(`${names.length} 项检查${jobs.length > names.length ? `（拆成 ${jobs.length} 份）` : ''}${repeat > 1 ? ` × ${repeat} 遍` : ''}，`
  + `${lanes > 1 ? `${lanes} 道并行` : '一个接一个'}${stacks.length > 1 ? `、分在 ${stacks.length} 套服务上` : ''}，完整输出在 ${LOGS}/\n`)

const label = (r) => `check-${r.name}${r.part ? `〔${r.part}〕` : ''}`

/**
 * 跑一份检查，输出落盘，数 ✓ / ✗。退出码非 0 却一个 ✗ 都没有的，是中途崩了；
 * 退出码 0 却一个 ✓ 都没有的，是什么都没查——都不算通过
 */
function runOne(job, pass, stack, suffix = '') {
  const file = join(LOGS, `${job.name}${job.part ? `.${job.part.replace(/[^\w一-鿿-]+/g, '_')}` : ''}${repeat > 1 ? `.${pass}` : ''}${suffix}.log`)
  const out = createWriteStream(file)
  const started = Date.now()
  return new Promise((resolveRun) => {
    const child = spawn(process.execPath, [join(HERE, `check-${job.name}.mjs`)], {
      env: { ...baseEnv, AGENTLAB_WEB: stack.WEB, AGENTLAB_API: stack.API, ...job.env },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    running.add(child)
    child.stdout.pipe(out, { end: false })
    child.stderr.pipe(out, { end: false })
    let timedOut = false
    // 先 SIGTERM：Playwright 收到它会把自己拉起来的 Chrome 一起关掉；直接 SIGKILL 会留下孤儿浏览器
    let hard
    const timer = setTimeout(() => {
      timedOut = true
      child.kill('SIGTERM')
      hard = setTimeout(() => child.kill('SIGKILL'), 10_000)
    }, TIMEOUT_MS)
    child.on('close', (code) => {
      running.delete(child)
      clearTimeout(timer)
      clearTimeout(hard)
      out.end(() => {
        const text = readFileSync(file, 'utf8')
        const lines = text.split('\n')
        // 检查项是缩进的一行；顶格的「✓ 全部通过」「✗ N 项未通过」是脚本自己的总结
        const ok = lines.filter((l) => /^\s+✓ /.test(l)).length
        const bad = lines.filter((l) => /^\s+✗ /.test(l)).map((l) => l.trim())
        const secs = Math.round((Date.now() - started) / 1000)
        const crashed = !timedOut && code !== 0 && !bad.length
        const empty = !timedOut && code === 0 && ok === 0
        const passed = code === 0 && ok > 0
        const tail = crashed || empty ? lines.filter(Boolean).slice(-4).join(' ⏎ ').slice(0, 300) : ''
        resolveRun({ name: job.name, part: job.part, job, pass, code, ok, bad, secs, timedOut, crashed, empty, passed, tail, file })
      })
    })
  })
}

/** 拆开跑的几份合成一行：通过要每份都通过，没过的项前面标上是哪一份 */
function merge(rs) {
  if (rs.length === 1) return rs[0]
  const worst = rs.find((r) => r.timedOut) ?? rs.find((r) => r.crashed) ?? rs.find((r) => r.empty) ?? rs.find((r) => !r.passed)
  return {
    name: rs[0].name, part: '', pass: rs[0].pass, parts: rs,
    ok: rs.reduce((n, r) => n + r.ok, 0),
    bad: rs.flatMap((r) => r.bad.map((l) => `〔${r.part}〕${l}`)),
    secs: rs.reduce((n, r) => n + r.secs, 0),
    timedOut: rs.some((r) => r.timedOut), crashed: rs.some((r) => r.crashed), empty: rs.some((r) => r.empty),
    passed: rs.every((r) => r.passed),
    tail: worst?.tail ? `〔${worst.part}〕${worst.tail}` : '',
    file: worst?.file ?? rs[0].file,
  }
}

const limit = TIMEOUT_MS >= 60_000 ? `${Math.round(TIMEOUT_MS / 60_000)} 分钟` : `${Math.round(TIMEOUT_MS / 1000)} 秒`
const verdict = (r) => r.timedOut ? `超时（${limit}）`
  : r.crashed ? '中途崩了'
  : r.empty ? '一项都没跑'
  : r.passed ? '通过'
  : `${r.bad.length} 项没过`
let spent = 0   // 各份累计的秒数，重跑的那次也算
const report = (r) => {
  const mark = r.passed ? '✓' : '✗'
  const secs = r.parts ? `${r.secs}s（${r.parts.length} 份：${r.parts.map((p) => `${p.secs}s`).join(' / ')}）` : `${r.secs}s`
  console.log(`${mark} ${label(r).padEnd(20)} ${verdict(r).padEnd(10)} ${String(r.ok).padStart(4)} 项通过 · ${secs}`)
  for (const l of r.bad.slice(0, 5)) console.log(`    ${l}`)
  if (r.bad.length > 5) console.log(`    ……另有 ${r.bad.length - 5} 项，见 ${r.parts ? r.parts.filter((p) => !p.passed).map((p) => p.file).join('、') : r.file}`)
  if (r.tail) console.log(`    ${r.tail}`)
}

/**
 * 一遍：lanes 道同时从队里取，第 k 道用第 k % N 套服务。长的排前面；量时间的一份在跑时，
 * 别的量时间的先让给队里后面的。量时间的几份反正一份接一份，合起来就是一条长链，按整条链的
 * 长度排队：开头就起跑，一份跑完下一份马上接上，不被别的长项压到后面。一项的几份都跑完了才
 * 打印这一项（合成一行），返回时按 ORDER 排好
 */
async function runPass(pass) {
  const chain = jobs.filter((j) => j.timing).reduce((n, j) => n + j.est, 0)
  const rank = (j) => (j.timing ? chain : j.est)
  const queue = [...jobs].sort((a, b) => rank(b) - rank(a) || b.est - a.est)
  const done = []
  let timingBusy = false
  let waiting = []   // 等量时间的那一份跑完的几道
  const take = () => {
    const i = queue.findIndex((j) => !(j.timing && timingBusy))
    return i < 0 ? null : queue.splice(i, 1)[0]
  }
  const lane = async (k) => {
    const stack = stacks[k % stacks.length]
    while (queue.length) {
      const job = take()
      if (!job) {
        // 队里只剩量时间的，而另一份正在跑：等它跑完再取
        await new Promise((r) => waiting.push(r))
        continue
      }
      if (job.timing) timingBusy = true
      const r = await runOne(job, pass, stack)
      spent += r.secs
      if (job.timing) {
        timingBusy = false
        const woken = waiting
        waiting = []
        for (const wake of woken) wake()
      }
      done.push(r)
      const mine = jobs.filter((j) => j.name === job.name).map((j) => done.find((d) => d.job === j))
      if (mine.every(Boolean)) report(merge(mine))
    }
  }
  await Promise.all(Array.from({ length: lanes }, (_, k) => lane(k)))
  return done
}

const byName = (done) => names.map((n) => merge(jobs.filter((j) => j.name === n).map((j) => done.find((r) => r.job === j))))

const results = []
const flakyRerun = []   // 并行时没过、单独重跑通过的
const started = Date.now()
for (let pass = 1; pass <= repeat; pass++) {
  if (repeat > 1) console.log(`—— 第 ${pass} 遍 ——`)
  const done = await runPass(pass)
  if (lanes > 1 && repeat === 1 && done.some((r) => !r.passed)) {
    const failed = jobs.map((j) => done.find((r) => r.job === j)).filter((r) => !r.passed)
    console.log(`\n并行时没过的 ${failed.length} 份，单独重跑一遍：`)
    for (const first of failed) {
      const again = await runOne(first.job, pass, stacks[0], '.solo')
      spent += again.secs
      report(again)
      if (again.passed) flakyRerun.push({ first, again })
      done[done.indexOf(first)] = again
    }
  }
  results.push(...byName(done))
  if (repeat > 1) console.log('')
}

if (flakyRerun.length) {
  console.log('\n偶发（并行时没过、单独重跑通过）：')
  for (const { first } of flakyRerun) {
    const why = first.bad.length ? first.bad.slice(0, 3).join('；') : verdict(first)
    console.log(`  ${label(first)}：${why}（见 ${first.file}）`)
  }
}
console.log('')

// 连跑几遍时：同一项只在部分遍数里失败，就是偶发——比稳定失败更该查。按项名认，不带
// 「 — 」后面的读数：每遍都没过、只是毫秒数或像素差不一样的，是稳定失败，不是偶发
if (repeat > 1) {
  const seen = new Map()   // `${name}\t${项名}` → [{ pass, line }]
  for (const r of results) {
    for (const p of r.parts ?? [r]) {
      const lines = p.crashed || p.timedOut || p.empty ? [`${p.part ? `〔${p.part}〕` : ''}${verdict(p)}`]
        : p.bad.map((l) => `${p.part ? `〔${p.part}〕` : ''}${l}`)
      for (const line of lines) {
        const k = `${r.name}\t${line.replace(/ — .*$/, '')}`
        seen.set(k, [...(seen.get(k) ?? []), { pass: r.pass, line }])
      }
    }
  }
  const flaky = [...seen].filter(([, hits]) => new Set(hits.map((h) => h.pass)).size < repeat)
  if (flaky.length) {
    console.log('偶发（只在部分遍数里失败）：')
    for (const [k, hits] of flaky) {
      const name = k.split('\t')[0]
      for (const h of hits) console.log(`  check-${name} 第 ${h.pass} 遍：${h.line}`)
    }
    console.log('')
  }
}

if (unlisted.length && !picked.length) {
  console.log(`没跑：${unlisted.map((n) => `check-${n}`).join('、')} 不在 check-all 的清单里（要算进来就加进 ORDER）\n`)
}

const failedRuns = results.filter((r) => !r.passed)
const total = results.reduce((n, r) => n + r.ok, 0)
const minutes = (secs) => `${Math.floor(secs / 60)} 分 ${String(secs % 60).padStart(2, '0')} 秒`
const wall = Math.round((Date.now() - started) / 1000)
const clock = lanes > 1 ? `用时 ${minutes(wall)}（各份累计 ${minutes(spent)}）` : `共 ${minutes(wall)}`
const flakyNote = flakyRerun.length ? `，其中 ${flakyRerun.length} 份是重跑才过的，见上面「偶发」` : ''
if (failedRuns.length) {
  console.log(`✗ ${failedRuns.length}/${results.length} 次检查没通过（${[...new Set(failedRuns.map((r) => `check-${r.name}`))].join('、')}）· ${clock}`)
} else {
  console.log(`✓ 全部通过：${results.length} 次检查、${total} 项${flakyNote} · ${clock}`)
}

// 另起的几套先好好停（SIGTERM，等它们退），停不下来的由 cleanup 里的 SIGKILL 兜底
await Promise.all(stacks.flatMap((s) => [s.vite?.stop?.(), s.backend?.stop?.()]))
process.exit(failedRuns.length ? 1 : 0)
