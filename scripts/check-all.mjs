// 分道并行跑全部检查，汇总成一张表，给一个总的退出码。
//
//   node scripts/check-all.mjs                   全部跑一遍
//   node scripts/check-all.mjs ui runs           只跑 check-ui、check-runs（名字去掉 check- 前缀）
//   node scripts/check-all.mjs --repeat 2        整套连跑两遍，列出只在某一遍失败的项（偶发）
//   node scripts/check-all.mjs --lanes 1         一个接一个地跑（以前的跑法）；也可以写 CHECK_LANES=1
//
// 默认连 5273 / 8000。对别的实例（比如一份沙箱拷贝）跑时带上地址，会原样传给每个检查：
//   AGENTLAB_WEB=http://localhost:<前端端口> AGENTLAB_API=http://localhost:<后端端口>/api node scripts/check-all.mjs
// 每个检查的完整输出写进 CHECK_LOGS（默认 /tmp/agentlab-checks/<时间>/），终端只给汇总。
//
// 默认 4 道并行。各检查对后端只读（几处 POST 都是纯计算的排版接口），截图各有各的目录，
// 时间大多花在 waitForTimeout 的固定等待上，所以并行几乎是线性提速。代价是共用同一个
// vite 开发服务器，有几项还量时间（审批卡多少毫秒出现、计时器走没走）。两道防线：
// - 量时间最多的几项（EXCLUSIVE）互相不重叠，同一时刻最多跑其中一项；
// - 并行时没过的，全部跑完后再单独、一个接一个地重跑一遍。重跑通过的算通过，但单列成
//   「偶发」：它在挤的时候会挂，迟早也会在别处挂。连跑几遍（--repeat）时不重跑，要的就是
//   原始的偶发率。
//
// 各检查自己的段过滤（CHECK_ONLY、ONLY、THEMES……）不往下传：整套检查就是整套，只跑某几段
// 请直接跑那个脚本。
//
// 退出码：0 全部通过；1 有检查没通过、中途崩了、超时或一项都没跑；2 前后端连不上，一项都没跑。
import { spawn } from 'node:child_process'
import { createWriteStream, mkdirSync, readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const API = process.env.AGENTLAB_API ?? 'http://localhost:8000/api'
const HERE = dirname(fileURLToPath(import.meta.url))

// 先跑快的、不开浏览器的，再按页面从外壳往里跑；最后是连真数据的两项
const ORDER = [
  'tokens',          // 令牌、别名、原生对话框：纯静态扫源码
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
// 最近一次全量里各项的耗时（秒）。只决定排队顺序：长的先开跑，短的填空，结果不受影响。
// 新加的检查不在表里就按 60 秒算
const ESTIMATE = {
  'run-states': 220, 'canvas-fx': 181, stream: 162, studio: 125, chat: 124, manage: 115, runs: 91,
  shell: 84, 'ui-kit': 69, ui: 61, evidence: 56, publish: 31, guards: 10, 'canvas-layout': 4, tokens: 1, trace: 1, decode: 1,
}
// 量时间最多、以前并行最容易挂的：同一时刻最多跑其中一项
const EXCLUSIVE = new Set(['run-states', 'canvas-fx'])
const TIMEOUT_MS = Number(process.env.CHECK_TIMEOUT_MS ?? 15 * 60_000)
// 各检查只跑其中几段、只跑一套主题的开关。壳里留着一个 ONLY=… 就会让整套检查悄悄只跑一小截，
// 却照样报「全部通过」，所以一律不传给子进程
const FILTERS = ['CHECK_ONLY', 'ONLY', 'THEMES', 'FX_ONLY', 'STUDIO_ONLY', 'RUN_STATES_ONLY', 'UI_KIT_ONLY', 'CHECK_THEME',
  'EVIDENCE_ONLY', 'PUBLISH_ONLY']
// 新加的 check-*.mjs 忘了写进 ORDER 就永远不会跑：列出来提醒
const unlisted = readdirSync(HERE)
  .map((f) => f.match(/^check-(.+)\.mjs$/)?.[1])
  .filter((n) => n && n !== 'all' && !ORDER.includes(n))

const args = process.argv.slice(2)
let repeat = 1
let lanes = Math.max(1, Number(process.env.CHECK_LANES) || 4)
const picked = []
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--repeat') repeat = Math.max(1, Number(args[++i]) || 1)
  else if (args[i].startsWith('--repeat=')) repeat = Math.max(1, Number(args[i].slice(9)) || 1)
  else if (args[i] === '--lanes') lanes = Math.max(1, Number(args[++i]) || 1)
  else if (args[i].startsWith('--lanes=')) lanes = Math.max(1, Number(args[i].slice(8)) || 1)
  else picked.push(args[i].replace(/^check-/, '').replace(/\.mjs$/, ''))
}
const unknown = picked.filter((n) => !ORDER.includes(n))
if (unknown.length) {
  console.error(`认不出的检查：${unknown.join('、')}。可选的有：${ORDER.join(' ')}`)
  process.exit(2)
}
const names = picked.length ? ORDER.filter((n) => picked.includes(n)) : ORDER

// 连不上就别跑：十几个检查挨个报「打不开页面」只会淹没真正的原因
const reach = async (url) => {
  try {
    const r = await fetch(url, { signal: AbortSignal.timeout(5000) })
    return r.ok ? '' : `HTTP ${r.status}`
  } catch (e) {
    return e.cause?.code ?? e.name ?? String(e)
  }
}
const needsServers = names.some((n) => n !== 'tokens')
if (needsServers) {
  const [web, api] = await Promise.all([reach(WEB), reach(`${API}/health`)])
  if (web || api) {
    if (web) console.error(`✗ 前端连不上：${WEB}（${web}）`)
    if (api) console.error(`✗ 后端连不上：${API}/health（${api}）`)
    console.error('  先把前后端起来（./scripts/dev.sh），或者用 AGENTLAB_WEB / AGENTLAB_API 指到要检查的那一份')
    process.exit(2)
  }
}

const now = new Date()
const pad = (n) => String(n).padStart(2, '0')
const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
const LOGS = process.env.CHECK_LOGS ?? join('/tmp/agentlab-checks', stamp)
mkdirSync(LOGS, { recursive: true })
console.log(`前端 ${WEB} · 后端 ${API}`)
const dropped = FILTERS.filter((k) => process.env[k])
if (dropped.length) console.log(`没传给各检查的段过滤：${dropped.map((k) => `${k}=${process.env[k]}`).join(' ')}（只跑某几段请直接跑那个脚本）`)
lanes = Math.min(lanes, names.length)
console.log(`${names.length} 项检查${repeat > 1 ? ` × ${repeat} 遍` : ''}，${lanes > 1 ? `${lanes} 道并行` : '一个接一个'}，完整输出在 ${LOGS}/\n`)

const childEnv = { ...process.env, AGENTLAB_WEB: WEB, AGENTLAB_API: API }
for (const k of FILTERS) delete childEnv[k]

/**
 * 跑一个检查，输出落盘，数 ✓ / ✗。退出码非 0 却一个 ✗ 都没有的，是中途崩了；
 * 退出码 0 却一个 ✓ 都没有的，是什么都没查——都不算通过
 */
function runOne(name, pass, suffix = '') {
  const file = join(LOGS, `${name}${repeat > 1 ? `.${pass}` : ''}${suffix}.log`)
  const out = createWriteStream(file)
  const started = Date.now()
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [join(HERE, `check-${name}.mjs`)], {
      env: childEnv,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
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
        resolve({ name, pass, code, ok, bad, secs, timedOut, crashed, empty, passed, tail, file })
      })
    })
  })
}

const limit = TIMEOUT_MS >= 60_000 ? `${Math.round(TIMEOUT_MS / 60_000)} 分钟` : `${Math.round(TIMEOUT_MS / 1000)} 秒`
const verdict = (r) => r.timedOut ? `超时（${limit}）`
  : r.crashed ? '中途崩了'
  : r.empty ? '一项都没跑'
  : r.passed ? '通过'
  : `${r.bad.length} 项没过`
let spent = 0   // 各项累计的秒数，重跑的那次也算
const report = (r) => {
  spent += r.secs
  const mark = r.passed ? '✓' : '✗'
  console.log(`${mark} ${`check-${r.name}`.padEnd(20)} ${verdict(r).padEnd(10)} ${String(r.ok).padStart(4)} 项通过 · ${r.secs}s`)
  for (const l of r.bad.slice(0, 5)) console.log(`    ${l}`)
  if (r.bad.length > 5) console.log(`    ……另有 ${r.bad.length - 5} 项，见 ${r.file}`)
  if (r.tail) console.log(`    ${r.tail}`)
}

/**
 * 一遍：lanes 道同时从队里取。长的排前面；EXCLUSIVE 里的一项在跑时，另一项先让给队里
 * 后面的。结果按跑完的先后打印，返回时按 ORDER 排好
 */
async function runPass(pass) {
  const queue = [...names].sort((a, b) => (ESTIMATE[b] ?? 60) - (ESTIMATE[a] ?? 60))
  const done = []
  let exclusiveBusy = false
  let waiting = []   // 等 EXCLUSIVE 那一项跑完的几道
  const take = () => {
    const i = queue.findIndex((n) => !(EXCLUSIVE.has(n) && exclusiveBusy))
    return i < 0 ? null : queue.splice(i, 1)[0]
  }
  const lane = async () => {
    while (queue.length) {
      const name = take()
      if (!name) {
        // 队里只剩 EXCLUSIVE 的，而另一项正在跑：等它跑完再取
        await new Promise((resolve) => waiting.push(resolve))
        continue
      }
      const exclusive = EXCLUSIVE.has(name)
      if (exclusive) exclusiveBusy = true
      const r = await runOne(name, pass)
      if (exclusive) {
        exclusiveBusy = false
        const woken = waiting
        waiting = []
        for (const resolve of woken) resolve()
      }
      done.push(r)
      report(r)
    }
  }
  await Promise.all(Array.from({ length: lanes }, lane))
  return names.map((n) => done.find((r) => r.name === n))
}

const results = []
const flakyRerun = []   // 并行时没过、单独重跑通过的
const started = Date.now()
for (let pass = 1; pass <= repeat; pass++) {
  if (repeat > 1) console.log(`—— 第 ${pass} 遍 ——`)
  const got = await runPass(pass)
  if (lanes > 1 && repeat === 1 && got.some((r) => !r.passed)) {
    const failed = got.filter((r) => !r.passed)
    console.log(`\n并行时没过的 ${failed.length} 项，单独重跑一遍：`)
    for (const first of failed) {
      const again = await runOne(first.name, pass, '.solo')
      report(again)
      if (again.passed) flakyRerun.push({ first, again })
      got[got.indexOf(first)] = again
    }
  }
  results.push(...got)
  if (repeat > 1) console.log('')
}

if (flakyRerun.length) {
  console.log('\n偶发（并行时没过、单独重跑通过）：')
  for (const { first } of flakyRerun) {
    const why = first.bad.length ? first.bad.slice(0, 3).join('；') : verdict(first)
    console.log(`  check-${first.name}：${why}（见 ${first.file}）`)
  }
}
console.log('')

// 连跑几遍时：同一项只在部分遍数里失败，就是偶发——比稳定失败更该查。按项名认，不带
// 「 — 」后面的读数：每遍都没过、只是毫秒数或像素差不一样的，是稳定失败，不是偶发
if (repeat > 1) {
  const seen = new Map()   // `${name}\t${项名}` → [{ pass, line }]
  for (const r of results) {
    const lines = r.crashed || r.timedOut || r.empty ? [verdict(r)] : r.bad
    for (const line of lines) {
      const k = `${r.name}\t${line.replace(/ — .*$/, '')}`
      seen.set(k, [...(seen.get(k) ?? []), { pass: r.pass, line }])
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
const clock = lanes > 1 ? `用时 ${minutes(wall)}（各项累计 ${minutes(spent)}）` : `共 ${minutes(wall)}`
const flakyNote = flakyRerun.length ? `，其中 ${flakyRerun.length} 项是重跑才过的，见上面「偶发」` : ''
if (failedRuns.length) {
  console.log(`✗ ${failedRuns.length}/${results.length} 次检查没通过（${[...new Set(failedRuns.map((r) => `check-${r.name}`))].join('、')}）· ${clock}`)
} else {
  console.log(`✓ 全部通过：${results.length} 次检查、${total} 项${flakyNote} · ${clock}`)
}
process.exit(failedRuns.length ? 1 : 0)
