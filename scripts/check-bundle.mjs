// 可验证报告的检查：真服务端导出、断网打开核对、篡改变红；虚构样本页上的各种形状；外加记录页上的导出按钮。
//
// 跑之前前后端都得起着（check-all 另起的那套，或者 ./scripts/dev.sh）。默认连 5273 / 8000；对别的实例跑时带上地址：
//   AGENTLAB_WEB=http://localhost:<前端端口> AGENTLAB_API=http://localhost:<后端端口>/api node scripts/check-bundle.mjs
// 样本页用后端解释器生成（CHECK_PYTHON，默认 ~/miniforge3/envs/agentlab/bin/python），从这份工作区的 backend 导入。
//
// 分几段：
// - 导出：从服务端找一份已封存、封存核对一致、有报告文档的运行（GET /api/runs 找成功结束的，再看 /verify 和证据接口），
//   下载 bundle.html：状态码、类型、附件名（RFC 5987）、页面本身零外链。找不到可导出的运行时照实报失败并说明原因；
// - 断网打开：拦掉一切网络请求，从本地文件打开：各项核对没有不通过、总览通过（有无法核对的项时是中性的「可核对的项全部
//   通过，另有 N 项无法核对」，下面醒目地写明真伪要去系统里核对封存）、没有发出任何请求，数字点开看得到出处；
//   数字与取值的显示文字都核对得了（没有「无法核对显示文字」：移植的显示规则漏了真实写法时这里先报）；
// - 去掉 crypto.subtle：页面加载前删掉它，照常全部通过，页脚写明用的是本页内置的实现；
// - 篡改：① 一条带规范字符串的事件内容、② 一份嵌入工件的一个字节、③ 元信息里的封存哈希，各自对应的核对项不通过、
//   总览显示不通过；另做两种「哈希全部重算」的伪造：改报告里一个数字的引用值，或者引用值不动、只改它的显示文字（数字改
//   一位；有引用快照文字的取值时也改一个），文档、事件、清单、页面顶部的封存哈希都重算一遍，①–④ 都对得上，只有 ⑤ 拿
//   口径卡、快照对出来（显示文字按显示规则重新渲染后逐字比对）不通过，原因点名正文显示的字和引用的值，报告标题旁的徽标
//   跟着不通过；
// - 深浅色、窄屏：390 宽不横向溢出，点开的数字面板在窄屏上从底部抽出，深色下底是深的、字是浅的；
// - 样本页：tests/bundle_samples.py 用虚构数据真实生成的几份页面（遮罩快照、两份报告、早期运行、失败结束、先失败后
//   经继续运行跑到成功的早期运行、两条出具
//   判定、服务端缺文件、裸 NaN、别的运行的文档、口径卡有输入取自遮罩列（卡的节点完成事件只给哈希）、同一张卡配上证据
//   目录里写着号码的老文档（文档只给哈希、页面写明原因）、设了遮罩的数据源上按地区汇总的查询、每种显示
//   格式各引用一处的经营月报、单位带尾随空白的经营月报、引用旁边是写作者自己写的普通文字的经营月报（「统一」「——」
//   「SKU-」「共…家」，原样导出全绿）），逐份断言各项结果和原因文案；有一项无法核对的样本页总览是中性的，不是绿的；
//   经营月报上只改文字取值、数字、换算成万的口径卡引用的显示文字并重算全部哈希，各自变红并点名；核对过的字不动、改它
//   旁边的（链接地址藏起数字、整段改成分隔线、文字片段冒用数字的 id；数字前面补一位数、加负号（半角、数学减号、负号
//   前面是汉字的）、后面接「0 万」，隔着显示时不占位置的控制字符补一位数，这几种变红并点名「正文里紧挨着这个数的字改变了
//   读者看到的数」；后面接量级单位（万、亿元、%、k，订单号后面接万）、画上删除线另写一个数、长得像数字的字母（l、O）、
//   长得像负号的连字符、叠在空格上的横线、从右往左书写的字母把一位数挪到数字前面判无法核对、写明原因，总览不再是绿的；
//   紧挨着数字头尾的字改成白名单：别的文字系统里长得像 I、O、M、K 的字母、数学字母、隔着细空格的一位数、隔着换行和 em
//   空格的「万」、「（万元）」、弯撇号当千分位补的一位数、偏旁「刂」和扩展 B 区里像竖线的字、后面接「×10⁴」「bn」
//   「 billion」、前面紧挨着 en dash 判无法核对，负号前面是别的文字系统的字母、后面接 e 和数字判不通过），
//   读的人看到的数要就是核对过的那个；列表缩进写成十万级或负数、
//   表格的列号写成十万，核对通过的数字照样留在屏幕上（宽屏、窄屏都不横向溢出）；在上面篡改 ④
//   引用链（换文档、拿掉报告、多一份没人引用的工件）、⑥ 运行信息（运行结果、类别、出具档位）、拿掉被引用的口径卡、把
//   报告文档标成遮罩（总览不是绿的）、把遮罩卡标成服务端的文件与 id 不一致或解析不出来（服务端对被改过、解析不了的遮罩卡
//   就这么标）、
//   遮罩源上随文件附带的汇总快照改一个字节、它的数字改引用值并重算全部哈希、
//   只改页面顶部的封存哈希；宽屏点开表格里右对齐的数字不被面板盖住；不执行脚本时封存哈希、运行 ID 照样看得到，状态写
//   「尚未核对」；
// - 记录页的导出按钮（预览用例 ?syn=bundle-provenance-*，page.route 假装导出接口）：已封存时可点、下载的文件名同服务端；
//   未封存时不可点并说明原因（左边写着「已封存」却不能点时原因写在屏幕上）；服务端回 409 时提示原因；失败结束的运行
//   提示之后继续运行会重新封存。
//
// 读的是真数据：输出里不打印工作流名、报告内容和数字，只打印计数和运行号前 8 位。下载的文件写在临时目录，跑完删掉。
// 导出接口只读（不追加事件、不改运行），页面检查里的写接口一律是假的。
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { homedir, tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const API = process.env.AGENTLAB_API ?? 'http://localhost:8000/api'
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const BACKEND = join(dirname(dirname(fileURLToPath(import.meta.url))), 'backend')
const PYTHON = [process.env.CHECK_PYTHON, join(homedir(), 'miniforge3/envs/agentlab/bin/python')]
  .find((p) => p && existsSync(p))

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}
const note = (text) => console.log(`  · ${text}`)
/** 等一处元素出现：locator.isVisible() 不等待，提示、按钮恢复要等一会儿才到 */
const appears = (locator, timeout = 5000) => locator.first().waitFor({ state: 'visible', timeout }).then(() => true, () => false)

const TMP = mkdtempSync(join(tmpdir(), 'agentlab-bundle-'))
// 导出的页面带着真数据：中途崩了也要删掉
process.on('exit', () => rmSync(TMP, { recursive: true, force: true }))
const browser = await chromium.launch({ executablePath: CHROME })
const cleanup = async () => {
  await browser.close().catch(() => {})
  rmSync(TMP, { recursive: true, force: true })
}

/** 一节一节地跑：某一节里元素找不到、等待超时，只记成这一节失败，接着跑下一节 */
async function section(name, fn) {
  console.log(`\n=== ${name} ===`)
  try {
    await fn()
  } catch (e) {
    check(`${name} 中途出错`, false, String(e?.message ?? e).split('\n')[0])
  }
}

// ---------------------------------------------------------------- 页面数据的读写（篡改用）

const DATA_RE = /(<script type="application\/json" id="bundle-data">)([\s\S]*?)(<\/script>)/
const SEAL_RE = /(<code id="seal-hash" class="hash">)([^<]*)(<\/code>)/
const dataOf = (html) => JSON.parse(DATA_RE.exec(html)[2])
// 和服务端 bundle_json 一样转义 < > & 和 U+2028 / U+2029，改过的数据照样截不断数据块
const escapeJson = (obj) => JSON.stringify(obj)
  .replace(/[<>&\u2028\u2029]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`)
const withData = (html, obj) => html.replace(DATA_RE, (_, open, __, close) => open + escapeJson(obj) + close)
/** 只改页面顶部静态写出的封存哈希（不执行脚本也看得到的那一份） */
const withSeal = (html, hash) => html.replace(SEAL_RE, (_, open, __, close) => open + hash + close)
const sha = (data) => createHash('sha256').update(data).digest('hex')

function writePage(name, html) {
  const file = join(TMP, name)
  writeFileSync(file, html)
  return file
}

/**
 * 断网打开一份本地文件：除了 file:// 本身，任何请求都拦掉并记下来。等页面核对完（html[data-verified]）再交回。
 * noSubtle：页面脚本跑之前删掉 crypto.subtle（Crypto.prototype 上的 getter），逼它走内置实现
 */
async function openFile(file, { noSubtle = false, colorScheme = 'light', width = 1280 } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 }, colorScheme })
  const requests = []
  await ctx.route('**/*', (route) => {
    const url = route.request().url()
    if (url.startsWith('file:')) return route.continue()
    requests.push(url)
    return route.abort('blockedbyclient')
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
  if (noSubtle) {
    await page.addInitScript(() => {
      Object.defineProperty(Crypto.prototype, 'subtle', { get: () => undefined, configurable: true })
    })
  }
  await page.goto(pathToFileURL(file).href)
  await page.waitForSelector('html[data-verified]', { timeout: 30_000 })
  return { ctx, page, requests, errors }
}

const stateOf = (page) => page.evaluate(() => ({
  overall: document.getElementById('overall')?.getAttribute('data-overall'),
  text: document.getElementById('overall-text')?.textContent ?? '',
  checks: Object.fromEntries([...document.querySelectorAll('[data-check]')].map((c) => [c.dataset.check, c.dataset.result])),
  sums: Object.fromEntries([...document.querySelectorAll('[data-check]')].map((c) => [c.dataset.check,
    c.querySelector('.check-sum')?.textContent ?? ''])),
  failsIn: Object.fromEntries([...document.querySelectorAll('[data-check]')].map((c) => [c.dataset.check,
    new Set([...c.querySelectorAll('li[data-result="fail"]')].map((li) => li.textContent)).size])),
  // 每一条核对：在哪一块、结果、文字（含原因）、是不是收在「展开全部」里
  items: [...document.querySelectorAll('[data-check] li[data-result]')].map((li) => ({
    check: li.closest('[data-check]').dataset.check, result: li.dataset.result, text: li.textContent,
    folded: !!li.closest('details'),
  })),
  reports: [...document.querySelectorAll('article.report')].map((a) => ({
    node: a.dataset.report, result: a.dataset.result, parts: a.querySelector('[data-report-parts]')?.textContent ?? '',
    warn: a.querySelector('.warn')?.textContent ?? '', why: a.querySelector('.warn')?.dataset.why ?? '',
  })),
  meta: Object.fromEntries([...document.querySelectorAll('[data-meta]')].map((m) => [m.dataset.meta,
    { result: m.dataset.result, text: m.textContent }])),
  numbers: document.querySelectorAll('button.num').length,
  numberStates: [...document.querySelectorAll('button.num')].reduce((acc, b) => {
    acc[b.dataset.result] = (acc[b.dataset.result] ?? 0) + 1
    return acc
  }, {}),
  // 正文里标成不通过的数字：读屏念的那一句
  failedButtons: [...document.querySelectorAll('button.num[data-result="fail"]')].map((b) => b.getAttribute('aria-label') ?? ''),
  // 正文里每个可点的数字：哪一处、结果、读的人看到的字
  buttons: [...document.querySelectorAll('button.num')].map((b) => ({ key: b.dataset.num, result: b.dataset.result,
    text: b.textContent })),
  sha: document.querySelector('[data-sha]')?.dataset.sha ?? null,
  // 总览不是绿色的通过时下面醒目写着的那句（真伪要去系统里核对封存），没显示是空串
  hint: document.getElementById('overall-hint')?.hidden === false ? document.getElementById('overall-hint').textContent : '',
  seal: document.getElementById('seal-hash')?.textContent ?? '',
  overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
}))

const describe = (s) => Object.entries(s.checks).map(([k, v]) => `${k}=${v}`).join(' ')
/** 查询快照只给哈希时核对页写的原因（verifier.js 的 T.maskedQuery） */
const MASKED_QUERY = '这条查询用到或可能用到设了遮罩的列，快照未随文件附带，只能核对到哈希'
/** 有通过、没有不通过：「部分无法核对」也算（遮罩快照、没有出处的数字是正常的无法核对） */
const noFail = (r) => r === 'pass' || r === 'partial'
/**
 * 中性的总览：可核对的项都通过，另有几项无法核对——不是绿色的通过（无法核对的项里可能藏着读者看到的另一个数）。下面醒目地
 * 写明真伪要去系统里核对封存：运行 ID、/verify 接口、定时任务通知里的清单哈希前 12 位
 */
const neutral = (s) => s.overall === 'partial' && /^可核对的项全部通过，另有 \d+ 项无法核对$/.test(s.text)
  && s.hint.includes('/verify') && s.hint.includes('清单哈希前 12 位')
/** 总览通过：一项无法核对都没有的是绿色的「全部通过」，有的是中性的（neutral） */
const passed = (s) => (s.overall === 'pass' && s.text === '全部通过' && s.hint === '') || neutral(s)
const hasItem = (s, check, result, text) => s.items.some((i) => i.check === check && i.result === result
  && (!text || i.text.includes(text)))

/**
 * 「哈希全部重算」的伪造：报告里挑一个能核对的数字或取值（引用口径卡或随文件附带的快照，pick(片段) 为真的第一个），
 * edit(片段) 改它，再把文档、引用它的事件、清单哈希、页面顶部的封存哈希全部重算。返回改了哪一处的说明（what、所在报告
 * node、改过的片段 seg），找不到可改的返回 null
 */
function rewrite(data, pick, edit) {
  const embedded = new Set(data.artifacts.filter((a) => typeof a.b64 === 'string').map((a) => a.id))
  return rewriteDoc(data, (doc) => {
    let hit = null
    for (const block of doc.blocks ?? []) for (const unit of block.units ?? []) for (const seg of unit.segments ?? []) {
      const c = seg.cite
      if (!hit && c?.status === 'resolved' && ['metric', 'cell'].includes(c.kind)
          && embedded.has(doc.catalog?.[c.alias]?.artifact) && pick(seg)) hit = seg
    }
    if (!hit) return null
    edit(hit)
    return { what: `片段 ${hit.id}（${hit.cite.kind}）`, seg: hit }
  })
}

/**
 * change(文档) 就地改报告文档（返回 { what, seg? } 说明改了哪里，找不到可改的返回 null），再把文档、引用它的事件、
 * 清单哈希全部重算（页面顶部的封存哈希由 tamper 的 seal 换）。返回说明，另带所在报告 node
 */
function rewriteDoc(data, change) {
  const docArt = data.artifacts.find((a) => a.kind === 'report_doc' && typeof a.b64 === 'string')
  if (!docArt) return null
  const doc = JSON.parse(Buffer.from(docArt.b64, 'base64').toString('utf8'))
  const done = change(doc)
  if (!done) return null
  const bytes = Buffer.from(JSON.stringify(doc), 'utf8')
  const oldId = docArt.id
  const newId = sha(bytes)
  docArt.id = newId
  docArt.b64 = bytes.toString('base64')
  for (const r of data.reports) if (r.doc_artifact === oldId) r.doc_artifact = newId
  for (const ev of data.events) {
    if (typeof ev.canonical === 'string' && ev.canonical.includes(oldId)) {
      ev.canonical = ev.canonical.split(oldId).join(newId)
      ev.data_sha256 = sha(ev.canonical)
    }
  }
  const lines = data.events.map((e, i) => [e, i]).sort((x, y) => x[0].seq - y[0].seq || x[1] - y[1])
    .map(([e]) => `${e.seq}|${e.type}|${e.node_id ?? ''}|${e.data_sha256}`)
  data.meta.manifest_hash = sha(lines.join('\n'))
  return { ...done, node: data.reports.find((r) => r.doc_artifact === newId)?.node_id }
}

/** 经营月报样本里按 id 找片段（改它两边的文字做排版上的伪造） */
const segById = (doc, id) => doc.blocks.flatMap((b) => b.units).flatMap((u) => u.segments).find((s) => s.id === id)

/** 改一个数字的引用值（显示文字不动），重算全部哈希：⑤ 拿口径卡、快照对出来 */
const forge = (data) => rewrite(data, (seg) => typeof seg.cite.value === 'number', (seg) => { seg.cite.value += 1 })

/**
 * 只改显示文字的伪造（验收里的 T4、T5）：引用值不动，正文显示的字换成 change(原来的字)，cite.rendered 跟着改，重算全部
 * 哈希。pick 挑哪一个片段（默认第一个能核对的）
 */
const forgeShown = (change, pick = () => true) => (data) => rewrite(data, pick, (seg) => {
  seg.text = change(seg.text)
  seg.cite.rendered = seg.text
})

/** 显示文字里第一个数字加一（9 变 0）：74,613.65元 → 84,613.65元 */
const bumpDigit = (text) => text.replace(/\d/, (d) => String((Number(d) + 1) % 10))

/** 引用快照里文字的取值（华南、门店名这类） */
const isTextValue = (seg) => seg.kind === 'value' && typeof seg.cite.value === 'string' && seg.cite.value.trim() !== ''

/**
 * 只改显示文字的伪造该有的样子：⑤ 里只有那一项不通过，原因点名正文显示的字和引用的值，正文里那个数字标成不通过、
 * 读屏读作「不通过」；①–④ 照样通过（伪造重算了全部哈希）。quiet：读的是真数据，原因只断言、不打印
 */
const namesShown = ({ quiet = false } = {}) => (s, forged) => {
  // 条目多时不通过的列在外面一份、收起的全部里又一份：只数外面的
  const failed = s.items.filter((i) => i.check === 'numbers' && i.result === 'fail' && !i.folded)
  const named = `正文显示「${forged.seg.text}」，引用的值是「${forged.seg.cite.value}」`
  return [
    ...turnsRed('numbers', ['events', 'manifest', 'artifacts', 'chain'])(s),
    ['⑤ 里只有这一项不通过', failed.length === 1, `${failed.length} 项`],
    ['原因点名正文显示的字和引用的值', failed.length === 1 && failed[0].text.includes(named),
      quiet ? '' : failed[0]?.text ?? '没有不通过的项'],
    ['正文里那个数字标成不通过，读屏读作「不通过」', s.failedButtons.length === 1
      && s.failedButtons[0].includes(`${forged.seg.text}，不通过，查看出处`), quiet ? '' : s.failedButtons.join(' | ')],
    ['那份报告标题旁的徽标跟着不通过', s.reports.find((r) => r.node === forged.node)?.result === 'fail',
      JSON.stringify(s.reports.map((r) => [r.node, r.result]))],
  ]
}

/** 改一份嵌入工件的一个字节（优先改数字），返回说明 */
function flipByte(art) {
  const bytes = Buffer.from(art.b64, 'base64')
  const at = bytes.findIndex((b) => b >= 0x30 && b <= 0x39)
  const index = at >= 0 ? at : bytes.length - 2
  bytes[index] = at >= 0 ? (bytes[index] === 0x39 ? 0x30 : bytes[index] + 1) : bytes[index] ^ 0x01
  art.b64 = bytes.toString('base64')
  return `${art.kind} ${art.id.slice(0, 8)} 的第 ${index} 字节`
}

// ---------------------------------------------------------------- 导出

async function getJson(path) {
  const r = await fetch(API + path, { signal: AbortSignal.timeout(20_000) })
  if (!r.ok) throw new Error(`${path} 返回 HTTP ${r.status}`)
  return r.json()
}

/** 找一份可导出的运行：已封存、核对一致、证据图里有哈希复验通过的报告文档。优先有带出处的数字的那份 */
async function findRun() {
  let seen = 0
  let sealedCount = 0
  let fallback = null
  let before = ''
  for (let page = 0; page < 5; page++) {
    let runs
    try {
      runs = await getJson(`/runs?status=succeeded&limit=200${before ? `&before=${encodeURIComponent(before)}` : ''}`)
    } catch (e) {
      return { why: `无法读取运行列表：${e.message}` }
    }
    for (const run of runs) {
      seen++
      if (!run.manifest_hash || run.manifest_seq == null) continue
      sealedCount++
      const verdict = await getJson(`/runs/${run.id}/verify`).catch(() => null)
      if (verdict?.ok !== true) continue
      const graph = await getJson(`/runs/${run.id}/evidence`).catch(() => null)
      const reports = (graph?.mode === 'cited' ? graph.reports ?? [] : []).filter((r) => r.doc_artifact && r.hash_ok === true)
      if (!reports.length) continue
      const cited = reports.reduce((n, r) => n + (r.stats?.numbers_cited ?? 0), 0)
      if (cited > 0) return { run, cited, reports: reports.length }
      fallback ??= { run, cited, reports: reports.length }
    }
    if (runs.length < 200) break
    before = runs[runs.length - 1].created_at
  }
  if (fallback) return fallback
  if (!seen) return { why: '服务端里没有成功结束的运行' }
  return { why: `${seen} 次成功结束的运行里 ${sealedCount} 次已封存，没有一次封存核对一致且带报告文档` }
}

let picked = null
let html = ''

await section('导出', async () => {
  picked = await findRun()
  if (!picked.run) {
    check('找到一份已封存、核对一致、有报告文档的运行', false, picked.why)
    return
  }
  const { run } = picked
  check('找到一份已封存、核对一致、有报告文档的运行', true,
    `运行 ${run.id.slice(0, 8)}，${picked.reports} 份报告、${picked.cited} 个带出处的数字`)
  const before = await getJson(`/runs/${run.id}/events`).catch(() => null)
  const resp = await fetch(`${API}/runs/${run.id}/bundle.html`)
  const body = await resp.text()
  const ok = resp.status === 200 && /^text\/html/.test(resp.headers.get('content-type') ?? '')
  check('导出接口返回 200 和 text/html', ok, `HTTP ${resp.status}，${Math.round(Buffer.byteLength(body) / 1024)} KB`)
  // 只有真拿到了页面，后面几节才有东西可查；没拿到的原因上面已经报了，后面几节跳过，不重复报
  html = ok && DATA_RE.test(body) ? body : ''
  const disposition = resp.headers.get('content-disposition') ?? ''
  const star = /filename\*=UTF-8''([^;]+)/.exec(disposition)?.[1]
  let name = ''
  try { name = star ? decodeURIComponent(star) : '' } catch { name = '' }
  check('附件名按 RFC 5987 编码，以「-可验证报告.html」结尾', disposition.startsWith('attachment') && name.endsWith('-可验证报告.html'))
  check('另带一个纯 ASCII 的文件名给老客户端', /filename="[\x20-\x7e]+"/.test(disposition))
  // 页面本身（去掉数据块）不许有任何 http(s):// 引用；数据块里的字是数据，浏览器不会去取
  const shell = html.replace(DATA_RE, '$1$3')
  check('页面零外链：没有 http(s)://、外部脚本、样式表、图片和字体', !/https?:\/\//i.test(shell)
    && !/<script[^>]*\ssrc\s*=|<link\b|<img\b|@import|url\s*\(/i.test(shell))
  check('内容安全策略默认不许连任何地方', /default-src 'none'/.test(shell))
  check('数据块里没有裸的 < > &（截不断）', !/[<>&]/.test(DATA_RE.exec(html)?.[2] ?? '<'))
  check('封存哈希直接写在页面上（不执行脚本也看得到）', SEAL_RE.exec(html)?.[2] === run.manifest_hash)
  const after = await getJson(`/runs/${run.id}/events`).catch(() => null)
  check('导出不追加事件', !!before && !!after && before.length === after.length, before && after ? `${before.length} → ${after.length} 条` : '')
  // 没封存的，或者还没结束的（失败后接着跑、早年停在审批时封存过的运行留着旧的封存）：都该回 409
  const unfinished = await getJson('/runs?status=queued,running,interrupted,suspended&limit=5').catch(() => [])
  const recent = await getJson('/runs?limit=200').catch(() => [])
  const unsealed = [...unfinished, ...recent].find((r) => !r.manifest_hash || !['succeeded', 'failed', 'cancelled'].includes(r.status))
  if (unsealed) {
    const refused = await fetch(`${API}/runs/${unsealed.id}/bundle.html`)
    const body = await refused.json().catch(() => ({}))
    check('未封存或未结束的运行回 409 并说明原因', refused.status === 409 && body.code === 'bundle_unsealed' && !!body.detail,
      `运行 ${unsealed.id.slice(0, 8)}（${unsealed.manifest_hash ? '留着旧封存' : '未封存'}）HTTP ${refused.status}`)
  } else {
    note('数据里没有未封存或未结束的运行，409 由服务端单测覆盖')
  }
})

// ---------------------------------------------------------------- 断网打开

let tamperable = null   // 篡改要用：原始数据

const skipped = () => {
  if (html) return false
  note('没有拿到导出的页面（原因见上一节），这一节跳过')
  return true
}

await section('断网打开', async () => {
  if (skipped()) return
  tamperable = dataOf(html)
  const { ctx, page, requests, errors } = await openFile(writePage('bundle.html', html))
  try {
    const s = await stateOf(page)
    check('总览通过（有无法核对的项时是中性的「可核对的项全部通过，另有 N 项无法核对」）', passed(s), `${s.overall}：${s.text}`)
    check('总览不是绿色的通过时，醒目地写明去系统里核对封存（运行 ID、清单哈希前 12 位）', s.overall === 'pass'
      || (s.hint.includes(tamperable.meta.run_id) && s.hint.includes(tamperable.meta.manifest_hash.slice(0, 12))), s.hint)
    for (const key of ['events', 'manifest', 'chain']) check(`核对项「${key}」通过`, s.checks[key] === 'pass', s.checks[key])
    // 遮罩快照、服务端已没有文件的工件是正常的无法核对；没有出处的数字同理
    check('核对项「artifacts」没有不通过', noFail(s.checks.artifacts), s.checks.artifacts)
    check('数字与取值没有不通过的', noFail(s.checks.numbers) || s.checks.numbers === 'na',
      `${s.checks.numbers}：${JSON.stringify(s.numberStates)}`)
    // 显示文字在真数据上也要核得了：移植的显示规则漏了某种真实写法（或者真报告的排版被判成紧挨着）时，数字会大批变成
    // 「无法核对显示文字」，上面那条「没有不通过」看不出来。Chrome 有 BigInt，正常导出的都该核得了。读的是真数据：
    // 只打印条数
    const unshown = new Set(s.items.filter((i) => i.check === 'numbers' && i.result === 'na'
      && i.text.includes('无法核对显示文字')).map((i) => i.text)).size
    check('数字与取值的显示文字都核对得了（没有「无法核对显示文字」）', unshown === 0, `${unshown} 项`)
    // 早期运行的运行结果、升级前的运行类别核对不了（无法核对），只要求没有对不上的
    check('运行信息没有与已核对的事件对不上的', s.checks.meta !== 'fail', `${s.checks.meta}：${JSON.stringify(s.meta)}`)
    check('没有发出任何网络请求', requests.length === 0, requests.slice(0, 3).join('、'))
    check('页面没有报错', errors.length === 0, errors.slice(0, 2).join(' | '))
    check('用的是浏览器内置的 SHA-256', s.sha === 'subtle', String(s.sha))
    check('封存哈希醒目地写在页面上', s.seal === tamperable.meta.manifest_hash)
    // 页面列出的清单原文：拿出去用别的工具（这里是 node 的 crypto）算，等于封存哈希——读的人可以不信页面上的脚本
    const lines = await page.locator('[data-manifest-lines]').textContent()
    check('页面列出的清单原文在页面之外重算，等于封存哈希', sha(lines ?? '') === tamperable.meta.manifest_hash)
    const sealNote = await page.locator('#seal-note').innerText()
    check('页面写明只证明内部一致、真伪要去系统里对照', /内部一致/.test(sealNote) && /\/api\/runs\/[^/]+\/verify/.test(sealNote))
    check('页面写明哪几项取自运行记录、不在封存范围内', /取自运行记录/.test(await page.locator('#record-note').innerText()))
    if (s.numbers) {
      const first = page.locator('button.num[data-kind="number"][data-result="pass"]').first()
      const target = (await first.count()) ? first : page.locator('button.num').first()
      const key = await target.getAttribute('data-num')
      await target.click()
      await page.locator('#detail').waitFor({ state: 'visible', timeout: 5000 })
      const text = await page.locator('#detail').innerText()
      const cited = await target.getAttribute('data-kind') === 'number'
      check('点开数字看得到出处和核对结果', /出处/.test(text) && /核对结果/.test(text)
        && (!cited || /与按显示规则渲染的结果一致/.test(text)) && await page.locator('#detail').getAttribute('data-open') === key)
      await page.keyboard.press('Escape')
      check('Esc 关掉出处面板', await page.locator('#detail').isHidden())
    } else {
      check('报告里有可点的数字', picked?.cited === 0, '证据图说有带出处的数字，页面上却一个都没有')
    }
  } finally {
    await ctx.close()
  }
})

await section('去掉 crypto.subtle', async () => {
  if (skipped()) return
  const { ctx, page, requests, errors } = await openFile(join(TMP, 'bundle.html'), { noSubtle: true })
  try {
    const s = await stateOf(page)
    check('照常全部通过', passed(s) && !Object.values(s.checks).includes('fail'), describe(s))
    check('页脚写明用的是本页内置的实现', s.sha === 'js', String(s.sha))
    check('没有发出任何网络请求、没有报错', !requests.length && !errors.length, [...requests, ...errors].slice(0, 2).join(' | '))
  } finally {
    await ctx.close()
  }
})

// ---------------------------------------------------------------- 篡改

/**
 * 在一份页面上篡改后断网打开。mutate(data) 改数据、返回说明（找不到可改的返回 null）；expect(state, 说明, page) 返回
 * [[检查名, 条件, 说明]…]（要点开面板的可以是异步的）。seal：同时把页面顶部的封存哈希换成改过的数据里的那一份
 * （重算全部哈希的伪造会这么做）
 */
async function tamper(source, label, mutate, expect, { seal = false } = {}) {
  const data = structuredClone(dataOf(source))
  const what = mutate(data)
  if (!what) {
    check(`篡改：${label}`, false, '这份页面里找不到可以篡改的对象')
    return
  }
  let page = withData(source, data)
  if (seal) page = withSeal(page, data.meta.manifest_hash)
  const opened = await openFile(writePage(`tampered-${label.replace(/[^\w一-鿿]+/g, '_')}.html`, page))
  try {
    const s = await stateOf(opened.page)
    for (const [name, cond, detail] of await expect(s, what, opened.page)) {
      check(`篡改${label}：${name}`, cond, detail ?? `${typeof what === 'string' ? what : what.what}；${describe(s)}`)
    }
  } finally {
    await opened.ctx.close()
  }
}

/** 一项变红、总览不通过，另几项仍通过 */
const turnsRed = (key, keepPass = []) => (s) => [
  [`「${key}」不通过、总览不通过`, s.checks[key] === 'fail' && s.overall === 'fail' && s.failsIn[key] >= 1],
  ...keepPass.map((k) => [`「${k}」仍没有不通过`, noFail(s.checks[k]), s.checks[k]]),
]

await section('篡改', async () => {
  if (skipped() || !tamperable) return
  // ① 改一条带规范字符串的事件内容（不动它记的哈希）：只有 ① 变红，清单照样对得上
  await tamper(html, '一条事件的内容', (data) => {
    const ev = data.events.find((e) => e.type === 'run.started' && typeof e.canonical === 'string')
      ?? data.events.find((e) => typeof e.canonical === 'string')
    if (!ev) return null
    ev.canonical = /\d/.test(ev.canonical) ? ev.canonical.replace(/\d/, (d) => String((Number(d) + 1) % 10)) : `${ev.canonical} `
    return `第 ${ev.seq} 条（${ev.type}）`
  }, turnsRed('events', ['manifest']))
  // ② 改一份嵌入工件的一个字节：优先口径卡、快照，没有就改报告文档
  await tamper(html, '一份工件的一个字节', (data) => {
    const embedded = data.artifacts.filter((a) => typeof a.b64 === 'string')
    const art = embedded.find((a) => a.kind !== 'report_doc') ?? embedded[0]
    return art ? flipByte(art) : null
  }, turnsRed('artifacts', ['events', 'manifest']))
  // 伪造：改报告里一个数字的引用值，再把文档、引用它的事件、清单、页面顶部的封存哈希全部重算
  await tamper(html, '一个数字的引用值并重算全部哈希', forge, (s, forged) => [
    ...turnsRed('numbers', ['events', 'manifest', 'artifacts', 'chain'])(s),
    ['那份报告标题旁的徽标跟着不通过', s.reports.find((r) => r.node === forged.node)?.result === 'fail',
      JSON.stringify(s.reports.map((r) => [r.node, r.result]))],
  ], { seal: true })
  // 只改显示文字（验收里的 T4）：引用值不动，一个数字的显示文字改一位，重算全部哈希。⑤ 按显示规则重新渲染对出来，点名
  // 正文显示的字和引用的值。读的是真数据：原因只断言、不打印
  await tamper(html, '一个数字的显示文字并重算全部哈希',
    forgeShown(bumpDigit, (seg) => typeof seg.cite.value === 'number' && /\d/.test(seg.text ?? '')),
    namesShown({ quiet: true }), { seal: true })
  // 文字取值（验收里的 T5）：导出的报告里有引用快照文字的取值才做；样本页 formats 上一定做
  if (rewrite(structuredClone(tamperable), isTextValue, () => {})) {
    await tamper(html, '一个文字取值的显示并重算全部哈希', forgeShown((t) => `${t}（改）`, isTextValue),
      namesShown({ quiet: true }), { seal: true })
  } else {
    note('导出的报告里没有引用快照文字的取值，文字取值的篡改在样本页 formats 上做')
  }
  // ③ 改元信息里的封存哈希
  await tamper(html, '元信息里的封存哈希', (data) => {
    const h = data.meta.manifest_hash
    if (typeof h !== 'string' || !h) return null
    data.meta.manifest_hash = h.slice(0, -1) + (h.endsWith('0') ? '1' : '0')
    return '最后一位'
  }, turnsRed('manifest', ['events', 'artifacts']))
})

// ---------------------------------------------------------------- 深浅色、窄屏

const luminance = (rgb) => {
  const [r, g, b] = (rgb.match(/\d+(\.\d+)?/g) ?? ['0', '0', '0']).slice(0, 3).map(Number).map((v) => {
    const c = v / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

await section('深浅色与窄屏', async () => {
  if (skipped()) return
  for (const colorScheme of ['light', 'dark']) {
    const { ctx, page } = await openFile(join(TMP, 'bundle.html'), { colorScheme, width: 390 })
    try {
      const s = await stateOf(page)
      check(`${colorScheme === 'dark' ? '深色' : '浅色'} 390 宽：不横向溢出`, s.overflow <= 0, `溢出 ${s.overflow}px`)
      const colors = await page.evaluate(() => {
        const body = getComputedStyle(document.body)
        const doc = document.querySelector('.doc') ?? document.body
        return { bg: body.backgroundColor, fg: getComputedStyle(doc).color }
      })
      const [bg, fg] = [luminance(colors.bg), luminance(colors.fg)]
      const ratio = (Math.max(bg, fg) + 0.05) / (Math.min(bg, fg) + 0.05)
      check(`${colorScheme === 'dark' ? '深色' : '浅色'}：底色${colorScheme === 'dark' ? '深' : '浅'}、正文对比度够`,
        (colorScheme === 'dark' ? bg < 0.1 : bg > 0.8) && ratio >= 7, `对比度 ${ratio.toFixed(1)}`)
      if (s.numbers) {
        await page.locator('button.num').first().click()
        const box = await page.locator('#detail').boundingBox()
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
        check(`${colorScheme === 'dark' ? '深色' : '浅色'} 390 宽：出处面板从底部抽出、不溢出`,
          !!box && box.width <= 390 && box.y > 0 && overflow <= 0, box ? `面板 ${Math.round(box.width)}×${Math.round(box.height)}` : '')
      }
    } finally {
      await ctx.close()
    }
  }
})

// ---------------------------------------------------------------- 样本页

/** tests/bundle_samples.py 生成的页面：{名字: HTML}。生成不了时抛出，原因写清 */
function samplePages() {
  if (!PYTHON) throw new Error('找不到后端解释器，用 CHECK_PYTHON 指定')
  const out = join(TMP, 'samples')
  const r = spawnSync(PYTHON, ['-m', 'tests.bundle_samples', out], {
    cwd: BACKEND, encoding: 'utf8', timeout: 120_000,
    // 从这份工作区的 backend 导入；导入时会建数据目录，指到临时目录里
    env: { ...process.env, PYTHONPATH: BACKEND, AGENTLAB_DATA_DIR: join(TMP, 'py-data'), AGENTLAB_SECRET_KEY: 'check-bundle-samples' },
  })
  if (r.status !== 0) throw new Error(`生成样本页失败：${(r.stderr || String(r.error ?? '')).trim().split('\n').pop()}`)
  return Object.fromEntries(readdirSync(out).filter((f) => f.endsWith('.html'))
    .map((f) => [f.replace(/\.html$/, ''), readFileSync(join(out, f), 'utf8')]))
}

let samples = null

/** 断网打开一份样本页，交给 fn(state, page) 断言 */
async function sample(name, fn, opts = {}) {
  const { ctx, page, requests, errors } = await openFile(writePage(`sample-${name}.html`, samples[name]), opts)
  try {
    const s = await stateOf(page)
    if (requests.length || errors.length) check(`${name}：没有发出请求、没有报错`, false, [...requests, ...errors].slice(0, 2).join(' | '))
    await fn(s, page)
  } finally {
    await ctx.close()
  }
}

/** 点开正文里一个数字，等出处面板出来，交回面板里的全部文字（看完按 Esc 关掉） */
async function detailText(page, locator) {
  await locator.click()
  await page.locator('#detail').waitFor({ state: 'visible', timeout: 5000 })
  const text = await page.locator('#detail').innerText()
  await page.keyboard.press('Escape')
  return text
}

await section('样本页：各种形状', async () => {
  try {
    samples = samplePages()
  } catch (e) {
    check('生成样本页（tests/bundle_samples.py）', false, String(e?.message ?? e))
    return
  }
  check('生成样本页（tests/bundle_samples.py）', Object.keys(samples).length === 17, Object.keys(samples).join('、'))

  await sample('base', async (s, page) => {
    check('base：有无法核对的项，总览是中性的「可核对的项全部通过，另有 N 项无法核对」，不是绿色的通过', neutral(s),
      `${s.overall}：${s.text}`)
    const meta = dataOf(samples.base).meta
    check('base：总览下面醒目地写明真伪要去系统里核对封存，带着运行 ID 和清单哈希前 12 位',
      s.hint.includes(meta.run_id) && s.hint.includes(meta.manifest_hash.slice(0, 12)) && s.hint.includes('判断不了真伪')
      && await page.locator('#overall-hint').isVisible(), s.hint)
    check('base：总览框不是通过的绿底', await page.locator('#overall').evaluate((el) => {
      const probe = document.body.appendChild(document.createElement('div'))
      probe.style.background = 'var(--ok-soft)'
      const ok = getComputedStyle(probe).backgroundColor
      probe.remove()
      return getComputedStyle(el).backgroundColor !== ok
    }))
    check('base：①②④⑥ 通过，③⑤ 部分无法核对', ['events', 'manifest', 'chain', 'meta'].every((k) => s.checks[k] === 'pass')
      && s.checks.artifacts === 'partial' && s.checks.numbers === 'partial', describe(s))
    check(`base：选出了遮罩列的快照在 ③ 无法核对，写明「${MASKED_QUERY}」`, hasItem(s, 'artifacts', 'na', MASKED_QUERY))
    check('base：引用这份快照的数字无法核对，原因同上', hasItem(s, 'numbers', 'na', MASKED_QUERY))
    check('base：没有出处的数字写明未使用引用标记', hasItem(s, 'numbers', 'na', '该数字未使用引用标记，无法核对其来源'))
    const outside = s.items.filter((i) => i.check === 'numbers' && !i.folded)
    check('base：⑤ 超过 20 项收起来时，无法核对的列在外面', outside.length === 2 && outside.every((i) => i.result === 'na')
      && s.items.some((i) => i.check === 'numbers' && i.folded), `外面 ${outside.length} 条`)
    check('base：⑤ 分开数数字和取值，写明分项', s.sums.numbers.includes('数字 13 个 · 取值 8 个') && s.sums.numbers.includes('无法核对 2'),
      s.sums.numbers)
    check('base：报告徽标连同数字一起算（部分无法核对），旁边写分项', s.reports[0]?.result === 'partial'
      && s.reports[0].parts.includes('文档 通过') && s.reports[0].parts.includes('无法核对 2'), JSON.stringify(s.reports[0]))
    check('base：运行结果、类别、出具档位标着已核对', ['status', 'class', 'tier'].every((k) => s.meta[k]?.result === 'pass'),
      JSON.stringify(s.meta))
    check('base：封存之后追加的事件单列，标明不在封存范围内',
      (await page.locator('[data-appended]').allTextContents()).some((t) => t.includes('封存之后追加，不在封存范围内')))
    check('base：斜体渲染成斜体', (await page.locator('.doc em').allTextContents()).includes('按发生周统计'))
    const cell = page.locator('td.n button.num')
    check('base：表格里数字列右对齐', await cell.count() === 8)
    // 「与按显示规则渲染的结果一致」只附在显示文字核对过的数字和取值上：模型自己写的数没人重新渲染过
    const detailOf = async (locator) => {
      await locator.click()
      await page.locator('#detail').waitFor({ state: 'visible', timeout: 5000 })
      const text = await page.locator('#detail').innerText()
      await page.keyboard.press('Escape')
      return text
    }
    const cellNumber = page.locator('td.n button.num[data-kind="number"]').first()
    const value = page.locator('button.num[data-kind="value"]').first()
    const bare = page.locator('button.num[data-kind="number"][data-result="na"]').filter({ hasText: /^42$/ })
    check('base：带出处的数字注明显示文字与按显示规则渲染的结果一致', /与按显示规则渲染的结果一致/.test(await detailOf(cellNumber)))
    check('base：表格里的文字取值同样核对显示文字', /与按显示规则渲染的结果一致/.test(await detailOf(value)))
    check('base：没有出处的数字不注明', !/按显示规则/.test(await detailOf(bare)))
    check('base：读屏把文字取值读作「取值」、数字读作「数字」', (await value.getAttribute('aria-label')).startsWith('取值 ')
      && (await cellNumber.getAttribute('aria-label')).startsWith('数字 '))
    // 点开引用口径卡指标的数字：口径、计算方式（原式、代入式）、输入、复算，和输入指向的查询快照
    const metric = page.locator('.doc p button.num[data-kind="number"][data-result="pass"]').first()
    const mdetail = await detailOf(metric)
    check('base：指标数字的出处面板写明指标、口径与版本、原式、代入式、输入、复算',
      mdetail.includes('销售额（gmv）') && mdetail.includes('周报口径 v2')
        && mdetail.includes('原式（计算方式）') && mdetail.includes('代入式（代入输入后的算式）')
        && mdetail.includes('输入') && mdetail.includes('复算一致'), mdetail.slice(0, 400))
    check('base：每个输入带着取值和出处（哪次查询的哪一行哪一列）',
      mdetail.includes("← cell(nodes.fetch, 0, 'amount')") && mdetail.includes('取自 查询 Q1 · 第 1 行 · amount'),
      mdetail.slice(0, 400))
    check('base：输入指向的查询快照随文件附带，看得到 SQL 和被引用的那一行',
      mdetail.includes('SELECT store, amount, ratio FROM orders') && mdetail.includes('快照第 1 行'), mdetail.slice(0, 400))
  })

  // 宽屏：出处面板停在右侧，正文让出这一栏。点开表格里右对齐的数字，它和同列的数字都不被面板盖住
  for (const width of [1280, 1440]) {
    await sample('base', async (s, page) => {
      const cells = page.locator('td.n button.num')
      await cells.nth(2).click()
      await page.locator('#detail').waitFor({ state: 'visible', timeout: 5000 })
      const covered = await cells.nth(2).evaluate((el) => {
        const r = el.getBoundingClientRect()
        const at = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
        return !!at && !!at.closest('#detail')
      })
      const panel = await page.locator('#detail').boundingBox()
      const spot = await cells.nth(2).boundingBox()
      check(`${width} 宽：点开的数字不被出处面板盖住`, !covered && !!panel && !!spot && spot.x + spot.width <= panel.x,
        spot && panel ? `数字 ${Math.round(spot.x)}–${Math.round(spot.x + spot.width)}，面板从 ${Math.round(panel.x)} 起` : '')
      const next = await cells.nth(4).click({ timeout: 3000 }).then(() => true, () => false)
      check(`${width} 宽：面板开着时同列别的数字照样点得到`, next
        && await page.locator('#detail').getAttribute('data-open') === await cells.nth(4).getAttribute('data-num'))
      check(`${width} 宽：不横向溢出`, await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth))
    }, { width })
  }

  await sample('two-reports', async (s) => {
    check('two-reports：两份报告各自显示，引用链都通过', s.reports.length === 2 && s.checks.chain === 'pass'
      && s.items.filter((i) => i.check === 'chain' && i.text.includes('的文档')).length === 2, JSON.stringify(s.reports.map((r) => r.node)))
    check('two-reports：月报的数字全部通过', s.reports.find((r) => r.node === 'monthly')?.result === 'pass')
  })
  // 只引用指标的报告：正文没引用门店流水，快照从口径卡的输入这一条路随文件附带，④ 认得这条引用
  await sample('metric-only', async (s, page) => {
    check('metric-only：总览通过', s.overall === 'pass', `${s.overall}：${s.text}`)
    check('metric-only：引用链写明从已核对的口径卡的输入认这份快照',
      s.checks.chain === 'pass' && hasItem(s, 'chain', 'pass', '口径卡「周报口径」的输入（指标 gmv）'), describe(s))
    check('metric-only：只嵌报告文档、口径卡和口径卡的输入指向的快照（会员消费谁都没引用，不嵌）',
      s.items.filter((i) => i.check === 'artifacts').length === 3
        && !s.items.some((i) => i.check === 'artifacts' && i.text.includes('members')),
      String(s.items.filter((i) => i.check === 'artifacts').length))
    const detail = await detailText(page, page.locator('button.num[data-kind="number"]').first())
    check('metric-only：点开数字看得到指标、口径与版本、原式、代入式、输入和复算',
      detail.includes('销售额（gmv）') && detail.includes('周报口径 v2') && detail.includes('原式（计算方式）')
        && detail.includes('代入式（代入输入后的算式）') && detail.includes('复算一致'), detail.slice(0, 400))
    check('metric-only：输入指向的查询快照看得到 SQL 和被引用的那一行',
      detail.includes('SELECT store, amount, ratio FROM orders') && detail.includes('快照第 1 行'), detail.slice(0, 400))
  })
  await sample('legacy', async (s, page) => {
    check('legacy：总览中性（运行结果无法核对），范围写明按结束事件推算', neutral(s)
      && (await page.locator('#meta').innerText()).includes('早期运行，范围按结束事件推算'), s.text)
    check('legacy：运行结果无法核对，写明早期运行的结束事件不在封存范围内', s.meta.status?.result === 'na'
      && hasItem(s, 'meta', 'na', '早期运行在结束事件落库之前封存'), JSON.stringify(s.meta.status))
    check('legacy：运行结束事件列在封存之后', (await page.locator('[data-appended]').allTextContents()).some((t) => t.includes('运行结束')))
  })
  await sample('failed', async (s, page) => {
    check('failed：运行结果「已失败」与运行失败事件对上', s.meta.status?.result === 'pass' && s.meta.status.text.includes('已失败'))
    check('failed：封存说明写明之后继续运行会重新封存', await page.locator('#seal-failed').isVisible()
      && (await page.locator('#seal-failed').innerText()).includes('重新封存'))
  })
  await sample('base', async (_, page) => {
    check('成功结束的运行不提重新封存', await page.locator('#seal-failed').isHidden())
  })
  // 早期运行先失败、经继续运行跑到成功：范围里那条运行失败之后运行又继续了，它不是运行的结束；成功结束的那条在范围外
  await sample('continued', async (s) => {
    check('continued：总览中性，运行结果无法核对（成功结束的那条不在封存范围内），不拿中途那条失败去对',
      neutral(s) && s.meta.status?.result === 'na' && hasItem(s, 'meta', 'na', '早期运行在结束事件落库之前封存'),
      JSON.stringify(s.meta.status))
  })
  await sample('degraded', async (s) => {
    check('degraded：出具档位认最后一条出具判定（降档出具）', s.meta.tier?.result === 'pass' && s.meta.tier.text.includes('降档出具'),
      JSON.stringify(s.meta.tier))
  })
  await sample('missing-doc', async (s, page) => {
    check('missing-doc：总览不是绿色的通过，写明有报告未能核对', s.overall === 'incomplete' && s.text.includes('份报告未能核对'),
      `${s.overall}：${s.text}`)
    check('missing-doc：报告处写明导出时服务端已没有这份报告文档', s.reports[0]?.why === 'missing'
      && s.reports[0].warn.includes('导出时服务端已没有这份报告文档'), s.reports[0]?.warn)
    check('missing-doc：③ ④ 无法核对，原因写明', hasItem(s, 'artifacts', 'na', '导出时服务端已没有这份工件的文件')
      && hasItem(s, 'chain', 'na', '导出时服务端已没有这份报告文档'))
    check('missing-doc：总览框不是通过的绿底', await page.locator('#overall').evaluate((el) => {
      const probe = document.body.appendChild(document.createElement('div'))
      probe.style.background = 'var(--ok-soft)'
      const ok = getComputedStyle(probe).backgroundColor
      probe.remove()
      return getComputedStyle(el).backgroundColor !== ok
    }))
  })
  await sample('missing-card', async (s) => {
    check('missing-card：引用口径卡的数字无法核对，写明服务端已没有文件', hasItem(s, 'numbers', 'na', '导出时服务端已没有这份工件的文件'))
    check('missing-card：服务端缺文件是正常的无法核对，没有不通过，总览中性', neutral(s), `${s.overall}：${s.text}`)
  })
  // 口径卡上有输入取自遮罩列（会员号码）：卡上记着号码原值和代入式，整张卡只给 id。这是正常的无法核对，不是被改过
  await sample('masked-card', async (s, page) => {
    const card = s.items.find((i) => i.check === 'artifacts' && i.text.includes('口径卡'))
    check('masked-card：口径卡在 ③ 无法核对，写明「含遮罩列，未随文件附带，只能核对到哈希」，不判不通过',
      card?.result === 'na' && card.text.includes('含遮罩列，未随文件附带，只能核对到哈希'), card?.text)
    const metricNumber = s.items.find((i) => i.check === 'numbers' && i.text.includes('销售额'))
    check('masked-card：引用这张卡的数字无法核对，原因同上', metricNumber?.result === 'na'
      && metricNumber.text.includes('含遮罩列'), metricNumber?.text)
    check('masked-card：总览中性（可核对的项全部通过，另有几项无法核对），没有一项不通过', neutral(s)
      && !s.items.some((i) => i.result === 'fail'), `${s.overall}：${s.text}`)
    // 卡的节点完成事件预览里逐条写着「指标（名称）= 值」，号码那个指标也在里面：只给哈希，① 里不列它
    const data = dataOf(samples['masked-card'])
    const cardEvent = data.events.find((e) => e.type === 'node.finished' && e.node_id === 'card')
    check('masked-card：口径卡的节点完成事件只给哈希，① 里不列它', !!cardEvent && !('canonical' in cardEvent)
      && !s.items.some((i) => i.check === 'events' && i.text.includes(`第 ${cardEvent.seq} 条`)), cardEvent?.seq)
    // 报告文档是现在的报告撰写节点写的：证据目录里号码那个指标写「已遮罩」，文档照常随文件附带、正文照常显示
    check('masked-card：报告文档照常随文件附带，正文照常显示', s.reports[0]?.why === '' && s.reports[0]?.parts.includes('文档 通过'),
      JSON.stringify(s.reports[0]))
    const html = await page.content()
    check('masked-card：页面里找不到卡上那个遮罩列的号码', !html.includes('000-0001') && !html.includes('000-0002'))
  })
  // 同一张卡，报告文档是之前的报告撰写节点写的（证据目录里写着号码）：整份只给哈希，页面写明原因；这份报告算未能核对，
  // 总览不是绿的，但没有一项不通过（这是导出时就这样，不是被改过）
  await sample('masked-old-doc', async (s, page) => {
    const why = '证据目录里写着取自遮罩列的指标的值'
    check('masked-old-doc：总览不是绿色的通过，写明有报告未能核对，没有一项不通过', s.overall === 'incomplete'
      && s.text.includes('份报告未能核对') && !s.items.some((i) => i.result === 'fail'), `${s.overall}：${s.text}`)
    check('masked-old-doc：报告处写明证据目录里写着取自遮罩列的指标的值、未随文件附带（中性色）', s.reports[0]?.why === 'masked'
      && s.reports[0].warn.includes(why) && s.reports[0].warn.includes('未随文件附带'), s.reports[0]?.warn)
    check('masked-old-doc：③ 报告文档无法核对、④ 无法确认它记下的运行和节点，原因写明', hasItem(s, 'artifacts', 'na', why)
      && hasItem(s, 'chain', 'na', why))
    check('masked-old-doc：报告文档不随文件附带，它引用的口径卡、快照也不附带', dataOf(samples['masked-old-doc']).artifacts
      .every((a) => a.kind === 'report_doc' && a.masked === true && !('b64' in a)))
    const html = await page.content()
    check('masked-old-doc：页面里找不到号码', !html.includes('000-0001') && !html.includes('000-0002'))
  })
  // 会员库设了号码遮罩（查询当时记下了，数据源现在也设着）：按地区汇总的查询没碰号码列，快照随文件附带、数字核对得过；
  // 选出了号码的那份只给哈希，原因写明这条查询用到或可能用到设了遮罩的列
  await sample('masked-source', async (s, page) => {
    const regions = s.items.find((i) => i.check === 'artifacts' && i.text.includes('会员地区汇总'))
    check('masked-source：按地区汇总的快照随文件附带，③ 通过', regions?.result === 'pass', regions?.text)
    const cited = s.items.filter((i) => i.check === 'numbers' && i.text.includes('查询 Q3'))
    check('masked-source：引用地区汇总的 2 个数字都核对通过', cited.length === 2 && cited.every((i) => i.result === 'pass'),
      cited.map((i) => `${i.result}：${i.text}`).join(' | '))
    const members = s.items.find((i) => i.check === 'artifacts' && i.text.includes('会员消费'))
    check('masked-source：选出了号码的快照只给哈希，写明这条查询用到或可能用到设了遮罩的列',
      members?.result === 'na' && members.text.includes(MASKED_QUERY), members?.text)
    const phoneCited = s.items.find((i) => i.check === 'numbers' && i.text.includes('查询 Q2'))
    check('masked-source：引用它的数字无法核对，原因同上', phoneCited?.result === 'na' && phoneCited.text.includes(MASKED_QUERY),
      phoneCited?.text)
    check('masked-source：总览中性（可核对的项全部通过，另有几项无法核对），没有一项不通过', neutral(s)
      && !s.items.some((i) => i.result === 'fail'), `${s.overall}：${s.text}`)
    // 点开地区汇总的第一个数字：出处面板里有这条查询的 SQL 和被引用的那一行。先看有没有核对通过的数字，没有就照实记一笔、
    // 不去点：点空了要等满时限才抛错，同一节后面的样本（连同下面这一项）就都不跑了
    const passed = page.locator('button.num[data-result="pass"]')
    let detail = '页面上没有核对通过的数字'
    if (await passed.count()) {
      await passed.first().click({ timeout: 5000 })
      detail = await appears(page.locator('#detail')) ? await page.locator('#detail').innerText() : '点开后没有出处面板'
    }
    check('masked-source：点开数字看得到汇总查询的 SQL 和被引用的那一行', detail.includes('SUM(spend)')
      && detail.includes('快照第 1 行') && detail.includes('华东'), detail.replace(/\s+/g, ' ').slice(0, 120))
    const html = await page.content()
    check('masked-source：页面里找不到号码', !html.includes('000-0001') && !html.includes('000-0002'))
  })
  await sample('nan', async (s) => {
    check('nan：内容里有裸 NaN 的事件和工件照常通过', passed(s) && s.checks.events === 'pass'
      && !s.items.some((i) => i.result === 'fail'), describe(s))
  })
  await sample('stray', async (s) => {
    check('stray：另一次运行的文档，④ 不通过并写明', s.overall === 'fail' && hasItem(s, 'chain', 'fail', '文档记下的运行或节点与这次不符'))
    check('stray：文档没通过核对时数字不予核对', s.checks.numbers === 'na' && hasItem(s, 'numbers', 'na', '所在的报告文档没有通过核对')
      && s.reports[0]?.warn.includes('其中的数字不予核对'), s.checks.numbers)
  })
  // 经营月报：每种显示格式各引用一处（货币单位、万 / 亿、比率、负数、不分组、小数位、pct、DECIMAL 文本、超过 2^53 的
  // 订单号、日期时间、文字取值），原样导出全部通过：每个数字和取值的显示文字都按显示规则重新渲染、逐字对上
  await sample('formats', async (s, page) => {
    check('formats：总览「全部通过」，没有一项无法核对，不另写去系统里核对的提示', s.overall === 'pass' && s.text === '全部通过'
      && !s.items.some((i) => i.result !== 'pass') && s.hint === '' && await page.locator('#overall-hint').isHidden(),
      `${s.overall}：${s.text}`)
    check('formats：⑤ 数字 16 个、取值 3 个全部通过', s.checks.numbers === 'pass'
      && s.sums.numbers.includes('数字 16 个 · 取值 3 个') && s.numberStates.pass === 19, s.sums.numbers)
    const wan = page.locator('button.num').filter({ hasText: /^7\.46万元$/ })
    let detail = '找不到「7.46万元」'
    if (await wan.count()) {
      await wan.first().click()
      detail = await appears(page.locator('#detail')) ? await page.locator('#detail').innerText() : '点开后没有出处面板'
      await page.keyboard.press('Escape')
    }
    check('formats：点开换算成万的数字，引用的值、口径卡里的值照原值写，注明与按显示规则渲染的结果一致',
      detail.includes('74613.65') && detail.includes('与按显示规则渲染的结果一致') && !detail.includes('按显示规则应为'),
      detail.replace(/\s+/g, ' ').slice(0, 160))
    const order = page.locator('button.num').filter({ hasText: '202,609,071,234,567,890' })
    detail = '找不到订单号'
    let rowCell = ''
    if (await order.count()) {
      await order.first().click()
      detail = await appears(page.locator('#detail')) ? await page.locator('#detail').innerText() : '点开后没有出处面板'
      rowCell = await page.locator('#detail td[data-cell="order_no"]').textContent().catch(() => '') ?? ''
      await page.keyboard.press('Escape')
    }
    // 引用的值、快照里的值、快照第 1 行那一格都按 JSON 原文写：读成 double 的 202609071234567900 一处都不能有
    check('formats：超过 2^53 的订单号核对通过，出处面板里的值（连同快照那一行）一位不差',
      detail.includes('202609071234567890') && !detail.includes('202609071234567900')
      && rowCell === '202609071234567890' && detail.includes('与按显示规则渲染的结果一致'),
      `${detail.replace(/\s+/g, ' ').slice(0, 160)} | 那一行的格：${rowCell}`)
  })
  // 口径卡单位带尾随空白（「元 」）：服务端照原样接在数后面，正文是「74,613.65元 」，两边都只去首尾空白，原样导出照样
  // 全部通过，不会被说成文件被改过
  await sample('formats-unit', async (s, page) => {
    check('formats-unit：口径卡单位带尾随空白时原样导出「全部通过」', s.overall === 'pass' && s.text === '全部通过'
      && s.numberStates.pass === 19, `${s.overall}：${s.text}；${JSON.stringify(s.numberStates)}`)
    const gmv = page.locator('button.num').filter({ hasText: /^74,613\.65元\s*$/ })
    let detail = '找不到销售额'
    if (await gmv.count()) {
      await gmv.first().click()
      detail = await appears(page.locator('#detail')) ? await page.locator('#detail').innerText() : '点开后没有出处面板'
      await page.keyboard.press('Escape')
    }
    check('formats-unit：点开销售额，注明与按显示规则渲染的结果一致，不另列「按显示规则应为」',
      detail.includes('与按显示规则渲染的结果一致') && !detail.includes('按显示规则应为'), detail.replace(/\s+/g, ' ').slice(0, 160))
  })
  // 引用旁边是写作者自己写的普通文字（「运费统一10元」「合计——7,036.50元」「SKU-202…」「共12840单家」、破折号、段内软换行、
  // 金额前面的「￥」「¥」、全角括号、「NO.」、假名长音、制表符横线、粗体斜体包着的数）：服务端组装的文档原样导出，全部通过，
  // 总览是绿的
  await sample('prose', async (s) => {
    check('prose：普通写法紧挨着核对过的数，原样导出「全部通过」，没有一项无法核对', s.overall === 'pass'
      && s.text === '全部通过' && !s.items.some((i) => i.result !== 'pass') && s.hint === '', `${s.overall}：${s.text}`)
    check('prose：17 个数字和取值都显示成可点的数字、都通过', s.numberStates.pass === 17 && s.numbers === 17,
      JSON.stringify(s.numberStates))
  })

  // 不执行脚本（附件预览、快速查看）：封存哈希、运行 ID、工作流名照样看得到，状态写「尚未核对」，不是一直「正在核对」
  {
    const data = dataOf(samples.base)
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, javaScriptEnabled: false })
    try {
      const page = await ctx.newPage()
      await page.goto(pathToFileURL(writePage('sample-nojs.html', samples.base)).href)
      check('不执行脚本：封存哈希写在页面上', await page.locator('#seal-hash').innerText() === data.meta.manifest_hash)
      check('不执行脚本：运行 ID、工作流名写在页面上', (await page.locator('#seal-note').innerText()).includes(data.meta.run_id)
        && await page.locator('#title').innerText() === data.meta.workflow_name)
      check('不执行脚本：状态写「尚未核对」，不显示「正在核对」', await page.locator('.no-js').isVisible()
        && (await page.locator('#overall').innerText()).includes('尚未核对') && !(await page.locator('#overall').innerText()).includes('正在核对'))
    } finally {
      await ctx.close()
    }
  }
})

await section('样本页：篡改', async () => {
  if (!samples) {
    note('没有样本页（原因见上一节），这一节跳过')
    return
  }
  const base = samples.base
  const byKind = (data, kind) => data.artifacts.find((a) => a.kind === kind)
  // ④ 引用链
  await tamper(base, '报告记录指向另一份工件', (data) => {
    data.reports[0].doc_artifact = byKind(data, 'metric_set').id
    return '报告的文档换成口径卡'
  }, (s) => [...turnsRed('chain')(s), ['原因写明', hasItem(s, 'chain', 'fail', '这个节点最后一次报告自查记下的不是这份文档')]])
  await tamper(base, '拿掉报告记录', (data) => {
    data.reports = []
    return '清空 reports'
  }, (s) => [...turnsRed('chain')(s), ['原因写明', hasItem(s, 'chain', 'fail', '运行记录里有这个报告节点的自查事件，文件里却没有附带它的文档')]])
  await tamper(base, '多一份没人引用的工件', (data) => {
    const bytes = Buffer.from(JSON.stringify({ columns: ['n'], rows: [[7]], source: 'shop', sql: 'SELECT 7 AS n' }), 'utf8')
    data.artifacts.push({ id: sha(bytes), kind: 'query', node_id: 'fetch', b64: bytes.toString('base64') })
    return '追加一份哈希正确的快照'
  }, (s) => [...turnsRed('chain', ['artifacts'])(s), ['原因写明', hasItem(s, 'chain', 'fail', '已核对的事件和报告文档里都没有引用这份工件')]])
  // 服务端不会给出的状态：只能是改过
  await tamper(base, '拿掉被引用的口径卡', (data) => {
    data.artifacts = data.artifacts.filter((a) => a.kind !== 'metric_set')
    return '删掉口径卡条目'
  }, (s) => [...turnsRed('numbers')(s), ['原因写明', hasItem(s, 'numbers', 'fail', '引用的工件不在文件的工件清单里')]])
  // 只引用指标的报告：拿掉口径卡，数字没了核对的对象（⑤ 变红）；它的输入指向的快照还被已核对的
  // tool.end 事件引用着，④ 照样通过——事件本身就是一条合法的引用
  await tamper(samples['metric-only'], '拿掉被引用的口径卡（只引用指标的报告）', (data) => {
    data.artifacts = data.artifacts.filter((a) => a.kind !== 'metric_set')
    return '删掉口径卡条目'
  }, (s) => [...turnsRed('numbers', ['events', 'manifest', 'artifacts', 'chain'])(s),
    ['数字无法核对，写明引用的工件不在清单里', hasItem(s, 'numbers', 'fail', '引用的工件不在文件的工件清单里')]])
  // 证据目录里写着取自遮罩列的指标的值的老文档，服务端只给哈希、不附带它引用的工件（见样本页 masked-old-doc）：伪造者把
  // 一份好好的文档也标成遮罩，页面照服务端的说法写原因、正文不显示，可它引用的那几份工件还列着，已核对的事件和文档里却
  // 没人引用它们，判不通过——藏不起一份报告还显示通过
  await tamper(base, '报告文档标成遮罩', (data) => {
    const doc = byKind(data, 'report_doc')
    delete doc.b64
    doc.masked = true
    return '报告文档去掉内容、标遮罩'
  }, (s) => [
    ['总览不通过', s.overall === 'fail', `${s.overall}：${s.text}`],
    ['④ 它引用的工件没人引用，判不通过', hasItem(s, 'chain', 'fail', '已核对的事件和报告文档里都没有引用这份工件')],
    ['报告处写明未随文件附带，正文不显示', s.reports[0]?.why === 'masked' && s.numbers === 0, JSON.stringify(s.reports[0])],
  ])
  // 只留报告文档、拿掉它引用的工件（和服务端只给哈希时一模一样）：没有正文可核，这份报告算未能核对，总览不是绿的
  await tamper(base, '报告文档标成遮罩并拿掉它引用的工件', (data) => {
    const doc = byKind(data, 'report_doc')
    delete doc.b64
    doc.masked = true
    data.artifacts = [doc]
    return '报告文档去掉内容、标遮罩，只留它一份'
  }, (s) => [
    ['总览不是绿色的通过，写明有报告未能核对', s.overall === 'incomplete' && s.text.includes('份报告未能核对'),
      `${s.overall}：${s.text}`],
    ['报告处写明未随文件附带，正文不显示', s.reports[0]?.why === 'masked' && s.numbers === 0, JSON.stringify(s.reports[0])],
  ])
  // 检索快照这类别的工件服务端从来不会因遮罩不随文件附带：标成遮罩只能是改过
  await tamper(base, '查询快照以外的工件标成遮罩', (data) => {
    const card = byKind(data, 'metric_set')
    data.artifacts.push({ id: card.id.replace(/^./, (c) => (c === '0' ? '1' : '0')), kind: 'retrieval', masked: true })
    return '多一份标着遮罩的检索快照'
  }, (s) => [...turnsRed('artifacts')(s),
    ['原因写明', hasItem(s, 'artifacts', 'fail', '只有查询快照、口径卡和报告文档会因遮罩不随文件附带')]])
  // 遮罩卡在服务端被改过时，服务端照样只给 id、另标 mismatch（tests/test_bundle_masked_cards.py 守着服务端这一边）：
  // 不能因为不嵌内容就和正常的遮罩卡一样「可核对的项全部通过」
  await tamper(samples['masked-card'], '遮罩卡标成服务端的文件与 id 不一致', (data) => {
    byKind(data, 'metric_set').mismatch = true
    return '口径卡加 mismatch'
  }, (s) => {
    const card = s.items.find((i) => i.check === 'artifacts' && i.text.includes('口径卡'))
    const metricNumber = s.items.find((i) => i.check === 'numbers' && i.text.includes('销售额'))
    return [
      ...turnsRed('artifacts', ['events', 'manifest', 'chain'])(s),
      ['口径卡写明导出时服务端的文件与 id 不一致、可能被改过、未随文件附带', card?.result === 'fail'
        && card.text.includes('导出时服务端的这份文件与工件 id（哈希）不一致，可能被改过') && card.text.includes('未随文件附带'),
      card?.text],
      ['引用这张卡的数字无法核对，写明引用的工件没有通过核对', metricNumber?.result === 'na'
        && metricNumber.text.includes('引用的工件没有通过核对'), metricNumber?.text],
    ]
  })
  // 遮罩源上随文件附带的汇总快照照样逐字节核对：改一个字节 ③ 变红；改它的数字的引用值、哈希全部重算，⑤ 拿快照对出来变红
  await tamper(samples['masked-source'], '遮罩源上汇总快照的一个字节', (data) => {
    const art = data.artifacts.find((a) => a.kind === 'query' && typeof a.b64 === 'string')
    return art ? flipByte(art) : null
  }, turnsRed('artifacts', ['events', 'manifest']))
  await tamper(samples['masked-source'], '遮罩源上汇总查询的数字的引用值并重算全部哈希', forge,
    turnsRed('numbers', ['events', 'manifest', 'artifacts', 'chain']), { seal: true })
  // 服务端标了解析不出来、哈希又对得上的遮罩卡：照实说认不出是否含遮罩列，是正常的无法核对（同哈希一致却解析不了的
  // 嵌入工件），不说成「含遮罩列」
  await tamper(samples['masked-card'], '遮罩卡标成服务端解析不出来', (data) => {
    byKind(data, 'metric_set').unparsable = true
    return '口径卡加 unparsable'
  }, (s) => {
    const card = s.items.find((i) => i.check === 'artifacts' && i.text.includes('口径卡'))
    const metricNumber = s.items.find((i) => i.check === 'numbers' && i.text.includes('销售额'))
    return [
      ['口径卡无法核对，写明服务端的文件无法解析、认不出是否含遮罩列', card?.result === 'na'
        && card.text.includes('导出时服务端的这份文件无法解析，认不出是否含遮罩列'), card?.text],
      ['引用它的数字原因同上', metricNumber?.result === 'na' && metricNumber.text.includes('认不出是否含遮罩列'),
        metricNumber?.text],
      ['总览中性，没有不通过', neutral(s), `${s.overall}：${s.text}`],
    ]
  })
  await tamper(base, '报告文档的一个字节', (data) => flipByte(byKind(data, 'report_doc')), (s) => [
    ...turnsRed('artifacts')(s),
    ['数字不予核对', s.checks.numbers === 'na' && hasItem(s, 'numbers', 'na', '所在的报告文档没有通过核对')],
    ['报告处写明没有通过核对', s.reports[0]?.result === 'fail' && s.reports[0].why === 'untrusted'],
  ])
  // ⑥ 运行信息：改元信息不动事件，清单照样对得上，只有 ⑥ 变红，页面上显示的是事件里的值
  await tamper(base, '运行结果', (data) => {
    data.meta.status = 'failed'
    return 'succeeded → failed'
  }, (s) => [...turnsRed('meta', ['events', 'manifest'])(s),
    ['页面显示事件里的「已完成」并标明文件里写的不同', s.meta.status?.result === 'fail' && s.meta.status.text.includes('已完成')
      && s.meta.status.text.includes('已失败'), JSON.stringify(s.meta.status)]])
  // 先失败、经继续运行跑到成功的早期运行，运行结果改成「已失败」：范围里那条运行失败之后运行又继续了，它不是运行的
  // 结束，不能拿它把「已失败」标成已核对
  for (const [status, label] of [['failed', '已失败'], ['cancelled', '已取消']]) {
    await tamper(samples.continued, `继续运行过的早期运行的运行结果改成${label}`, (data) => {
      data.meta.status = status
      return `succeeded → ${status}`
    }, (s) => [...turnsRed('meta', ['events', 'manifest'])(s),
      ['运行结果不再标已核对，写明那条失败之后运行又继续了', s.meta.status?.result === 'fail'
        && hasItem(s, 'meta', 'fail', '之后运行又继续执行'), JSON.stringify(s.meta.status)]])
  }
  await tamper(base, '运行类别', (data) => {
    data.meta.run_class = 'exploratory'
    return 'formal → exploratory'
  }, (s) => [...turnsRed('meta')(s), ['原因写明', hasItem(s, 'meta', 'fail', '记下的运行参数不符')]])
  await tamper(base, '去掉出具档位', (data) => {
    data.meta.tier = null
    return 'tier → null'
  }, (s) => [...turnsRed('meta')(s), ['页面显示事件里的档位', s.meta.tier?.result === 'fail' && s.meta.tier.text.includes('完整出具')]])
  await tamper(samples.degraded, '出具档位改指早先那条', (data) => {
    const first = data.events.find((e) => e.type === 'issuance')
    data.meta.tier = { value: 'formal', seq: first.seq }
    return `指向第 ${first.seq} 条（完整出具）`
  }, (s) => [...turnsRed('meta')(s),
    ['页面显示最后一条的降档出具', s.meta.tier?.result === 'fail' && s.meta.tier.text.includes('降档出具'), JSON.stringify(s.meta.tier)]])
  // 只改页面顶部静态写出的封存哈希：数据里的没动，② 的清单照样对得上，但顶上那份对不上，核对后换成核对用的
  {
    const data = dataOf(base)
    const fake = data.meta.manifest_hash.replace(/^./, (c) => (c === '0' ? '1' : '0'))
    const opened = await openFile(writePage('tampered-seal.html', withSeal(base, fake)))
    try {
      const s = await stateOf(opened.page)
      check('篡改页面顶部的封存哈希：② 不通过、总览不通过', s.checks.manifest === 'fail' && s.overall === 'fail'
        && hasItem(s, 'manifest', 'fail', '页面被改过'), describe(s))
      check('篡改页面顶部的封存哈希：核对后顶上显示核对用的那一份', s.seal === data.meta.manifest_hash)
    } finally {
      await opened.ctx.close()
    }
  }
  // 重算全部哈希的伪造在样本页上同样露馅：⑤ 不通过，报告徽标跟着不通过
  await tamper(base, '样本页上重算全部哈希的伪造', forge, (s) => [
    ...turnsRed('numbers', ['events', 'manifest', 'artifacts', 'chain', 'meta'])(s),
    ['报告徽标不通过', s.reports[0]?.result === 'fail' && s.reports[0].parts.includes('不通过 1'), JSON.stringify(s.reports[0])],
  ], { seal: true })
  // 只改显示文字、引用值不动、重算全部哈希（验收里的 T4、T5）：经营月报上改文字取值、千分位加货币单位的数字、换算成万的
  // 口径卡引用，各自只有那一项不通过。原因点名正文显示的字和引用的值，数字另写按显示规则应显示的字
  const shownForgeries = [
    ['文字取值的显示', 'v:Q1.r0.region', '华东', null],
    ['数字的显示', 'm:gmv', '84,613.65元', '74,613.65元'],
    ['换算成万的口径卡引用的显示', 'm:gmv|万', '8.46万元', '7.46万元'],
  ]
  for (const [label, ref, text, expected] of shownForgeries) {
    await tamper(samples.formats, `${label}并重算全部哈希`, forgeShown(() => text, (seg) => seg.ref === ref), (s, forged) => {
      const reason = s.items.find((i) => i.check === 'numbers' && i.result === 'fail')?.text ?? ''
      return [
        ...namesShown()(s, forged),
        expected ? ['原因另写按显示规则应显示的字', reason.includes(`按显示规则应显示为「${expected}」`), reason]
          : ['文字取值的原因不另写应显示的字', !reason.includes('按显示规则应显示为'), reason],
      ]
    }, { seal: true })
  }
  // 点开改过的那个数字：出处面板并排列出按显示规则应为的字和正文显示的字
  {
    const data = structuredClone(dataOf(samples.formats))
    const forged = forgeShown(() => '8.46万元', (seg) => seg.ref === 'm:gmv|万')(data)
    const opened = await openFile(writePage('tampered-shown-detail.html', withSeal(withData(samples.formats, data),
      data.meta.manifest_hash)))
    try {
      await opened.page.locator(`button.num[data-num="${forged.node}|${forged.seg.id}"]`).click()
      const detail = await appears(opened.page.locator('#detail')) ? await opened.page.locator('#detail').innerText() : ''
      check('篡改换算成万的口径卡引用的显示：出处面板写明不通过，列出按显示规则应为「7.46万元」、正文显示「8.46万元」',
        detail.includes('不通过') && /按显示规则应为\s*7\.46万元/.test(detail) && /正文显示\s*8\.46万元/.test(detail),
        detail.replace(/\s+/g, ' ').slice(0, 200))
    } finally {
      await opened.ctx.close()
    }
  }
  // 核对过的字不动、改它旁边的（经营月报，重算全部哈希）：读的人看到的数要就是核对过的那个
  const buttonOf = (s, forged) => s.buttons.find((b) => b.key === `${forged.node}|${forged.seg.id}`)
  // 伪造的链接把销售额塞进链接地址（链接只显示文字），链接文字写成 84,613.65元：这一句改按原文显示，核对过的字露出来
  await tamper(samples.formats, '用链接地址藏起核对通过的数字', (data) => rewriteDoc(data, (doc) => {
    segById(doc, 's3').text += '[84,613.65元](x '
    segById(doc, 's5').text = `)${segById(doc, 's5').text}`
    return { what: '销售额塞进链接地址', seg: segById(doc, 's4') }
  }), async (s, forged, page) => {
    const btn = buttonOf(s, forged)
    const sentence = await page.locator('.doc p').first().innerText()
    return [
      ['销售额照样显示成可点的数字，字一个不少', btn?.text === '74,613.65元' && btn.result === 'pass', JSON.stringify(btn)],
      ['这一句按原文显示，伪造的链接写法露在外面', sentence.includes('[84,613.65元](x 74,613.65元)'), sentence.slice(0, 80)],
    ]
  }, { seal: true })
  // 核对过的那段字不动，只改紧挨着它的普通正文，重算全部哈希（验收第二轮的绕过）：前面补一位数读作 174,613.65元，前面加
  // 负号读作 -2.35%，后面接「0 万」读作 45,678.50 万。读者看到的是另一个数：那个数变红、点名原因，总览不通过。以前只判
  // 无法核对，总览照样是绿的「可核对的项全部通过」
  const GLUED = '正文里紧挨着这个数的字改变了读者看到的数'
  const failsBecause = (why) => (s, forged) => {
    const btn = buttonOf(s, forged)
    const item = s.items.find((i) => i.check === 'numbers' && i.result === 'fail' && !i.folded && i.text.includes(why))
    return [
      ['那个数字变红，不再标通过', btn?.result === 'fail', JSON.stringify(btn)],
      [`⑤ 点名「${why}」`, !!item, item?.text ?? '没有这一项'],
      ['⑤ 里只有这一项不通过', s.items.filter((i) => i.check === 'numbers' && i.result === 'fail' && !i.folded).length === 1],
      ['总览不通过', s.overall === 'fail' && s.checks.numbers === 'fail', `${s.overall}：${s.text}`],
      ['①–④ 照样通过（伪造重算了全部哈希）', ['events', 'manifest', 'artifacts', 'chain'].every((k) => noFail(s.checks[k])),
        describe(s)],
    ]
  }
  const glued = [
    ['核对通过的数字前面补一位数', 's3', (t) => `${t}1`, 's4', '本月销售额 174,613.65元'],
    ['核对通过的数字前面加负号', 's12', (t) => `${t}-`, 's13', '转化率 -2.35%'],
    ['核对通过的数字前面加数学减号', 's12', (t) => `${t}\u2212`, 's13', '转化率 −2.35%'],
    ['核对通过的数字前面紧挨着汉字加负号', 's12', () => '转化率-', 's13', '转化率-2.35%'],
    ['核对通过的数字后面接「0 万」', 's34', (t) => `0 万${t}`, 's33', '流水 45,678.50 万'],
  ]
  for (const [label, at, change, target, what] of glued) {
    await tamper(samples.formats, label, (data) => rewriteDoc(data, (doc) => {
      segById(doc, at).text = change(segById(doc, at).text)
      return { what, seg: segById(doc, target) }
    }), failsBecause(GLUED), { seal: true })
  }
  // 一段改成分隔线：分隔线不显示句子，这一段里核对通过的数字读的人看不到，导出时不会出现
  await tamper(samples.formats, '把一段改成分隔线', (data) => rewriteDoc(data, (doc) => {
    doc.blocks[2].type = 'hr'
    return { what: '第二段改成分隔线' }
  }), (s) => [
    ...turnsRed('numbers', ['events', 'manifest', 'artifacts', 'chain'])(s),
    ['原因写明正文里没有显示这一处', hasItem(s, 'numbers', 'fail', '正文里没有显示这一处：导出时不会出现这种情况')],
  ], { seal: true })
  // 文字片段冒用销售额的 id：页面按片段本身找核对结果，冒用的文字显示不成可点的数字、借不到「通过」
  await tamper(samples.formats, '文字片段冒用核对通过的数字的 id', (data) => rewriteDoc(data, (doc) => {
    const gmv = segById(doc, 's4')
    doc.blocks[2].units.at(-1).segments.push({ kind: 'text', id: gmv.id, text: '84,613.65元', state: 'neutral' })
    return { what: '第二段末尾加一段冒用 id 的文字', seg: gmv }
  }), (s, forged) => {
    const same = s.buttons.filter((b) => b.key === `${forged.node}|${forged.seg.id}`)
    return [
      ['冒用 id 的文字不显示成可点的数字', same.length === 1 && same[0].text === '74,613.65元'
        && !s.buttons.some((b) => b.text.includes('84,613.65')), JSON.stringify(same)],
    ]
  }, { seal: true })
  // 紧挨着的字（经营月报，重算全部哈希）：隔着显示时不占位置的控制字符补一位数，读者看到的是另一个数，判不通过；长得像
  // 负号的连字符、量级单位、删除线、长得像数字的字母、叠在空格上的横线、从右往左书写的字母把一位数挪到数字前面，读起来
  // 可能不是核对过的那个数，判无法核对，总览随之不是绿的（中性），下面写明真伪去系统里核对。原因各自写明。字一律按码点写，
  // 脚本里不出现看不见、改变方向的字
  const cp = (...points) => String.fromCodePoint(...points)
  const naBecause = (why) => (s, forged) => {
    const btn = buttonOf(s, forged)
    const item = s.items.find((i) => i.check === 'numbers' && i.result === 'na' && i.text.includes(why))
    return [
      ['那个数字判无法核对，不再标通过', btn?.result === 'na', JSON.stringify(btn)],
      [`⑤ 写明「${why}」`, !!item, item?.text ?? '没有这一项'],
      ['总览不是绿色的通过：中性，并写明真伪去系统里核对', neutral(s) && s.checks.numbers === 'partial', `${s.overall}：${s.text}`],
      ['①–④ 照样通过（伪造重算了全部哈希）', ['events', 'manifest', 'artifacts', 'chain'].every((k) => noFail(s.checks[k])),
        describe(s)],
    ]
  }
  await tamper(samples.formats, '隔着显示时不占位置的控制字符补一位数', (data) => rewriteDoc(data, (doc) => {
    segById(doc, 's3').text += `1${cp(0x7f)}`
    return { what: '销售额前面补「1」和一个 DEL', seg: segById(doc, 's4') }
  }), failsBecause(GLUED), { seal: true })
  // 长得像负号的连字符平时不当负号用：不判不通过，读起来像负数，判无法核对
  await tamper(samples.formats, '前面加一个长得像负号的连字符', (data) => rewriteDoc(data, (doc) => {
    segById(doc, 's12').text += cp(0x2010)
    return { what: '转化率前面加 U+2010', seg: segById(doc, 's13') }
  }), naBecause('前面紧挨着一道像负号的短横'), { seal: true })
  // 后面接量级单位：读者看到的数差了量级（45,678.5万 读作四亿多）。系统显示一个数时单位、换算都在核对过的字里，判无法
  // 核对、写明原因，总览不再是绿的。以前连无法核对都不是，总览「全部通过」
  for (const [label, at, change, target] of [
    ['核对通过的数字后面接「万」', 's34', (t) => `万${t}`, 's33'],
    ['核对通过的数字后面隔着空格接「万」', 's34', (t) => ` 万${t}`, 's33'],
    ['核对通过的数字后面接「亿元」', 's34', (t) => `亿元${t}`, 's33'],
    ['核对通过的数字后面接「%」', 's34', (t) => `%${t}`, 's33'],
    ['核对通过的数字后面接「k」', 's34', (t) => `k${t}`, 's33'],
    ['订单号后面接「万」', 's43', (t) => `万${t}`, 's42'],
  ]) {
    await tamper(samples.formats, label, (data) => rewriteDoc(data, (doc) => {
      segById(doc, at).text = change(segById(doc, at).text)
      return { what: label, seg: segById(doc, target) }
    }), naBecause('后面接着量级单位'), { seal: true })
  }
  // 删除线：核对过的数画上删除线、旁边另写一个数（读的人以为它已作废、以旁边的为准）
  await tamper(samples.formats, '核对通过的数字画上删除线、旁边另写一个数', (data) => rewriteDoc(data, (doc) => {
    segById(doc, 's3').text += '~~'
    segById(doc, 's5').text = `~~ 84,613.65元${segById(doc, 's5').text}`
    return { what: '本月销售额 ~~74,613.65元~~ 84,613.65元', seg: segById(doc, 's4') }
  }), naBecause('这个数画着删除线'), { seal: true })
  // 长得像数字的字母：l74,613.65元 读作 174,613.65元，45,678.5O 读作 45,678.50
  for (const [label, at, change, target] of [
    ['核对通过的数字前面加小写 L', 's3', (t) => `${t}l`, 's4'],
    ['核对通过的数字后面接大写 O', 's34', (t) => `O${t}`, 's33'],
  ]) {
    await tamper(samples.formats, label, (data) => rewriteDoc(data, (doc) => {
      segById(doc, at).text = change(segById(doc, at).text)
      return { what: label, seg: segById(doc, target) }
    }), naBecause('紧挨着长得像数字的字母或符号'), { seal: true })
  }
  await tamper(samples.formats, '叠在空格上的横线', (data) => rewriteDoc(data, (doc) => {
    segById(doc, 's3').text += cp(0x336)
    return { what: '销售额前面的空格上叠一道长横线（U+0336）', seg: segById(doc, 's4') }
  }), naBecause('紧挨着的正文里有看不见或在不同设备上显示不同的字'), { seal: true })
  // 希伯来字母：逻辑上在销售额后面的「5」显示在销售额紧前面（读作 574,613.65元）。先量一下页面上确实这样显示，
  // 说明这种伪造能把数读错
  await tamper(samples.formats, '从右往左书写的字母把一位数挪到数字前面', (data) => rewriteDoc(data, (doc) => {
    segById(doc, 's3').text += cp(0x5d0)
    segById(doc, 's5').text = `5${segById(doc, 's5').text}`
    return { what: '销售额前面加一个希伯来字母、后面加「5」', seg: segById(doc, 's4') }
  }), async (s, forged, page) => {
    const gap = await page.evaluate((key) => {
      const btn = document.querySelector(`button.num[data-num="${key}"]`)
      const text = btn?.nextSibling?.firstChild
      if (!btn || !text || text.nodeType !== 3 || !text.data.startsWith('5')) return null
      const range = document.createRange()
      range.setStart(text, 0)
      range.setEnd(text, 1)
      return Math.round(btn.getBoundingClientRect().left - range.getBoundingClientRect().right)
    }, `${forged.node}|${forged.seg.id}`)
    return [
      ['页面上「5」显示在销售额紧前面', gap !== null && Math.abs(gap) <= 2, `间距 ${gap}px`],
      ...naBecause('这一句里有从右往左书写的文字')(s, forged),
    ]
  }, { seal: true })
  // 紧挨着数字头尾的那一个字改成白名单（复核第 2 轮的伪造写法：换一个易混字、换一种空白就能绕开按字认的规则，以前全是
  // 绿色的「全部通过」、提示也不显示）：长得像数字的字母、名单以外的字、隔着各种空白和左括号的量级单位判无法核对，总览中性
  // 并写明去系统里核对；负号前面是别的文字系统里长得像标点的字母照旧算负号、后面接 e 和数字算科学记数法，判不通过
  for (const [label, at, change, target, why] of [
    ['数字前面加长得像 I 的西里尔字母', 's3', (t) => `${t}${cp(0x406)}`, 's4', '紧挨着长得像数字的字母或符号'],
    ['数字前面加汉字「丨」', 's3', (t) => `${t}${cp(0x4e28)}`, 's4', '紧挨着长得像数字的字母或符号'],
    ['数字前面加数学粗体 l', 's3', (t) => `${t}${cp(0x1d425)}`, 's4', '紧挨着这个数的字不在常见写法里'],
    ['数字前面隔着细空格补一位数', 's3', (t) => `${t}1${cp(0x2009)}`, 's4', '紧挨着这个数的字不在常见写法里'],
    ['数字后面接长得像 M 的西里尔字母', 's34', (t) => `${cp(0x41c)}${t}`, 's33', '紧挨着这个数的字不在常见写法里'],
    ['数字后面接开尔文符号', 's34', (t) => `${cp(0x212a)}${t}`, 's33', '紧挨着这个数的字不在常见写法里'],
    ['数字后面隔着换行接「万」', 's34', (t) => `\n万${t}`, 's33', '后面接着量级单位'],
    ['数字后面隔着 em 空格接「万」', 's34', (t) => `${cp(0x2003)}万${t}`, 's33', '后面接着量级单位'],
    ['数字后面接「（万元）」', 's34', (t) => `（万元）${t}`, 's33', '后面接着量级单位'],
    // 复核第 3 轮点名的几类：弯撇号当千分位补一位数、偏旁形的汉字和扩展 B 区里像竖线的字、乘号乘方再接数字、英文的量级写法、
    // en dash 当负号
    ['数字前面加「1」和弯撇号', 's3', (t) => `${t}1${cp(0x2019)}`, 's4', '紧挨着这个数的字不在常见写法里'],
    ['数字前面加偏旁「刂」', 's3', (t) => `${t}${cp(0x5202)}`, 's4', '紧挨着这个数的字不在常见写法里'],
    ['数字前面加扩展 B 区像竖线的字', 's3', (t) => `${t}${cp(0x2010c)}`, 's4', '紧挨着这个数的字不在常见写法里'],
    ['数字后面接「×10⁴」', 's34', (t) => `${cp(0xd7)}10${cp(0x2074)}${t}`, 's33', '后面紧接着乘号或乘方和数字'],
    ['数字后面接「bn」', 's34', (t) => `bn${t}`, 's33', '后面接着量级单位'],
    ['数字后面隔着空格接「billion」', 's34', (t) => ` billion${t}`, 's33', '后面接着量级单位'],
    ['数字前面紧挨着 en dash', 's12', () => `转化率${cp(0x2013)}`, 's13', '前面紧挨着一道像负号的短横'],
  ]) {
    await tamper(samples.formats, label, (data) => rewriteDoc(data, (doc) => {
      segById(doc, at).text = change(segById(doc, at).text)
      return { what: label, seg: segById(doc, target) }
    }), naBecause(why), { seal: true })
  }
  for (const [label, at, change, target] of [
    ['负号前面是拉丁文里长得像撇号的字母', 's12', () => `转化率${cp(0xa78c)}-`, 's13'],
    ['数字后面接 e 和数字', 's34', (t) => `e3${t}`, 's33'],
  ]) {
    await tamper(samples.formats, label, (data) => rewriteDoc(data, (doc) => {
      segById(doc, at).text = change(segById(doc, at).text)
      return { what: label, seg: segById(doc, target) }
    }), failsBecause(GLUED), { seal: true })
  }
  // 列表缩进写得极大或写成负数（服务端按原文的缩进算级数、没有上限）：最多缩进 8 级，列表里核对通过的数字都留在屏幕上、
  // 照常通过，宽屏、窄屏都不横向溢出
  for (const [label, depths] of [['列表缩进写成十万级', [100000, 3]], ['列表缩进写成负数', [-100000, -3]]]) {
    const data = structuredClone(dataOf(samples.formats))
    rewriteDoc(data, (doc) => {
      const block = doc.blocks[2]
      block.type = 'list'
      block.units.forEach((unit, i) => {
        unit.depth = depths[i]
        if (i > 0) unit.segments.unshift({ kind: 'structural', id: `li${i}`, text: '\n- ' })
      })
      return { what: label }
    })
    for (const width of [1280, 390]) {
      const opened = await openFile(writePage(`tampered-depth-${depths[0]}-${width}.html`,
        withSeal(withData(samples.formats, data), data.meta.manifest_hash)), { width })
      try {
        const s = await stateOf(opened.page)
        const boxes = await opened.page.evaluate(() => [...document.querySelectorAll('.doc li button.num')].map((b) => {
          const r = b.getBoundingClientRect()
          return [Math.round(r.left), Math.round(r.right)]
        }))
        check(`篡改${label}（${width} 宽）：列表里的数字都在屏幕上、照常通过，页面不横向溢出`,
          boxes.length === 7 && boxes.every(([l, r]) => l >= 0 && r <= width) && s.overflow <= 0
            && s.overall === 'pass' && s.text === '全部通过',
          `${JSON.stringify(boxes)}；溢出 ${s.overflow}px；${s.overall}：${s.text}`)
      } finally {
        await opened.ctx.close()
      }
    }
  }
  // 表格里一格的列号写成十万：只排有格的列，那一格紧接着前一列显示，不会被几万个空列挤到表格右边，也不建几十万个空格
  {
    const data = structuredClone(dataOf(samples.base))
    const done = rewriteDoc(data, (doc) => {
      const table = doc.blocks.find((b) => b.type === 'table')
      const unit = table?.units.find((u) => u.loc?.row === 0 && u.loc?.col === 1)
      const seg = unit?.segments.find((x) => x.kind === 'number' || x.kind === 'value')
      if (!seg) return null
      unit.loc = { row: 0, col: 100000 }
      return { what: '表格第一行第二格的列号改成 100000', seg }
    })
    if (!done) {
      check('篡改表格的列号：样本页 base 里找不到表格第一行第二格的数字', false)
    } else {
      const opened = await openFile(writePage('tampered-table-col.html', withSeal(withData(samples.base, data),
        data.meta.manifest_hash)))
      try {
        const got = await opened.page.evaluate((key) => {
          const btn = document.querySelector(`button.num[data-num="${key}"]`)
          const wrap = btn?.closest('.table-wrap')
          if (!btn || !wrap) return null
          const b = btn.getBoundingClientRect()
          const w = wrap.getBoundingClientRect()
          return { result: btn.dataset.result, visible: b.left >= w.left && b.right <= w.left + wrap.clientWidth,
            cols: wrap.querySelectorAll('thead th').length, cells: wrap.querySelectorAll('td').length }
        }, `${done.node}|${done.seg.id}`)
        check('篡改表格的列号：那一格紧接着前一列显示、照常通过，表格只有 3 列',
          got?.result === 'pass' && got.visible && got.cols === 3 && got.cells === 24, JSON.stringify(got))
      } finally {
        await opened.ctx.close()
      }
    }
  }
})

// ---------------------------------------------------------------- 记录页的导出按钮

const SAMPLE_NAME = '门店周报-可验证报告.html'
// 和服务端 api/bundle.py 的 SEAL_BROKEN 一字不差（服务端单测断言那一边）：提示里原样显示
const SEAL_BROKEN = '封存核对不一致：封存后有事件被修改、删除或插入，不能导出可验证报告。点「核对」可查看核对结果'

async function preview(syn, { width = 1280, bundle } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height: 700 }, acceptDownloads: true })
  const page = await ctx.newPage()
  const hits = []
  // 后注册的路由先匹配：兜底的先注册，假装导出接口的后注册，导出请求才落到假接口上。
  // 别的写接口一律掐掉：预览用例只该请求导出接口
  await page.route('**/api/**', (route) => (route.request().method() === 'GET' ? route.continue() : route.abort()))
  await page.route('**/api/runs/*/bundle.html', (route) => {
    hits.push(new URL(route.request().url()).pathname)
    return route.fulfill(bundle ?? {
      status: 200,
      headers: { 'content-type': 'text/html; charset=utf-8',
        'content-disposition': `attachment; filename="agentlab-f4b0f4b0-bundle.html"; filename*=UTF-8''${encodeURIComponent(SAMPLE_NAME)}` },
      body: '<!doctype html><title>bundle</title>',
    })
  })
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  await page.goto(`${WEB}/preview.html?syn=${syn}`, { waitUntil: 'networkidle' })
  await page.locator('[data-fixture="bundle-provenance"]').waitFor({ timeout: 15_000 })
  return { ctx, page, hits, errors }
}

await section('记录页的导出按钮', async () => {
  {
    const { ctx, page, hits, errors } = await preview('bundle-provenance-sealed')
    try {
      const button = page.locator('[data-bundle-export]')
      check('已封存：导出按钮可点', await button.isEnabled() && await button.getAttribute('data-bundle-export') === 'ready')
      check('按钮写着「导出可验证报告」', (await button.innerText()).includes('导出可验证报告'))
      const [download] = await Promise.all([page.waitForEvent('download', { timeout: 10_000 }), button.click()])
      check('下载的文件名同服务端（RFC 5987 的中文名）', download.suggestedFilename() === SAMPLE_NAME, download.suggestedFilename())
      check('请求的是这次运行的导出接口', hits.length === 1 && hits[0] === '/api/runs/f4b0f4b0f4b0f4b0f4b0f4b0f4b0f4b0/bundle.html',
        hits.join('、'))
      check('导出后提示文件名', await appears(page.getByText(`已导出「${SAMPLE_NAME}」`)))
      check('页面没有报错', !errors.length, errors.slice(0, 2).join(' | '))
    } finally {
      await ctx.close()
    }
  }
  {
    const refusal = { status: 409, contentType: 'application/json', body: JSON.stringify({ detail: SEAL_BROKEN, code: 'bundle_seal_broken' }) }
    const { ctx, page } = await preview('bundle-provenance-sealed', { bundle: refusal })
    try {
      await page.locator('[data-bundle-export]').click()
      check('服务端回 409 时原样提示原因', await appears(page.getByText(SEAL_BROKEN)))
      check('提示之后按钮恢复可点', await appears(page.locator('[data-bundle-export="ready"]')))
    } finally {
      await ctx.close()
    }
  }
  {
    // 失败结束的运行：导出前后都说明之后继续运行会重新封存
    const { ctx, page } = await preview('bundle-provenance-failed')
    try {
      const button = page.locator('[data-bundle-export]')
      const hover = await button.locator('xpath=..').getAttribute('title')
      check('失败结束：可以导出，悬停提示写明继续运行会重新封存', await button.isEnabled() && (hover ?? '').includes('重新封存'), hover ?? '')
      const [download] = await Promise.all([page.waitForEvent('download', { timeout: 10_000 }), button.click()])
      check('失败结束：导出后的提示写明继续运行会重新封存', !!download
        && await appears(page.getByText(`已导出「${SAMPLE_NAME}」。之后若继续运行，系统会重新封存`, { exact: false })))
    } finally {
      await ctx.close()
    }
  }
  for (const [syn, reason, shown] of [['bundle-provenance-running', '运行结束并封存后才能导出', false],
    ['bundle-provenance-waiting', '等待审批中', false], ['bundle-provenance-unsealed', '没有封存清单', false],
    ['bundle-provenance-again', '运行结束并封存后才能导出', true]]) {
    const { ctx, page, hits } = await preview(syn)
    const label = syn.replace('bundle-provenance-', '')
    try {
      const button = page.locator('[data-bundle-export]')
      const described = await button.getAttribute('aria-describedby')
      const spoken = described ? await page.locator(`[id="${described}"]`).innerText() : ''
      const hover = await page.locator('[data-bundle-export]').locator('xpath=..').getAttribute('title')
      check(`${label}：按钮不可点，说明原因`, await button.isDisabled()
        && spoken.includes(reason) && (hover ?? '').includes(reason), spoken || '没有说明')
      if (shown) {
        // 左边写着「已封存」，按钮却不能点：原因要写在屏幕上，触屏看不到悬停提示
        const visible = page.locator('[data-bundle-blocked="shown"]')
        const box = await visible.boundingBox()
        check(`${label}：左边写着「已封存」时，原因直接写在按钮旁`, await page.locator('[data-seal="sealed"]').isVisible()
          && await visible.isVisible() && !!box && box.width > 20 && (await visible.innerText()).includes(reason))
      }
      await button.click({ force: true }).catch(() => {})
      check(`${label}：点了也不请求导出接口`, hits.length === 0)
    } finally {
      await ctx.close()
    }
  }
  for (const syn of ['bundle-provenance-sealed', 'bundle-provenance-again']) {
    const { ctx, page } = await preview(syn, { width: 390 })
    try {
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
      const box = await page.locator('[data-bundle-export]').boundingBox()
      check(`390 宽（${syn.replace('bundle-provenance-', '')}）：凭证条不横向溢出，导出按钮在屏幕内`,
        overflow <= 0 && !!box && box.x >= 0 && box.x + box.width <= 390, `溢出 ${overflow}px`)
    } finally {
      await ctx.close()
    }
  }
})

await cleanup()
if (failed) {
  console.log(`\n✗ 可验证报告：${failed} 项未通过`)
  process.exit(1)
}
console.log('\n✓ 可验证报告：全部通过')
