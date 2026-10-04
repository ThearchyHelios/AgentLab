// 可验证报告的检查：真服务端导出、断网打开核对、篡改变红，外加记录页上的导出按钮。
//
// 跑之前前后端都得起着（check-all 另起的那套，或者 ./scripts/dev.sh）。默认连 5273 / 8000；对别的实例跑时带上地址：
//   AGENTLAB_WEB=http://localhost:<前端端口> AGENTLAB_API=http://localhost:<后端端口>/api node scripts/check-bundle.mjs
//
// 分几段：
// - 导出：从服务端找一份已封存、封存核对一致、有报告文档的运行（GET /api/runs 找成功结束的，再看 /verify 和证据接口），
//   下载 bundle.html：状态码、类型、附件名（RFC 5987）、页面本身零外链。找不到可导出的运行时照实报失败并说明原因；
// - 断网打开：拦掉一切网络请求，从本地文件打开：五项核对全部通过、总览通过、没有发出任何请求，数字点开看得到出处和核对结果；
// - 去掉 crypto.subtle：页面加载前删掉它，照常全部通过，页脚写明用的是本页内置的实现；
// - 篡改：① 一条带规范字符串的事件内容、② 一份嵌入工件的一个字节、③ 元信息里的封存哈希，各自对应的核对项不通过、
//   总览显示不通过；另做一次「哈希全部重算」的伪造：改报告里一个数字的引用值，文档、事件、清单的哈希都重算一遍，
//   ①–④ 都对得上，只有 ⑤ 拿口径卡、快照对出来不通过（这也是页面要人去系统里对照封存哈希的原因）；
// - 深浅色、窄屏：390 宽不横向溢出，点开的数字面板在窄屏上从底部抽出，深色下底是深的、字是浅的；
// - 记录页的导出按钮（预览用例 ?syn=bundle-provenance-*，page.route 假装导出接口）：已封存时可点、下载的文件名同服务端；
//   未封存时不可点并说明原因；服务端回 409 时提示原因。
//
// 读的是真数据：输出里不打印工作流名、报告内容和数字，只打印计数和运行号前 8 位。下载的文件写在临时目录，跑完删掉。
// 导出接口只读（不追加事件、不改运行），页面检查里的写接口一律是假的。
import { createHash } from 'node:crypto'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const API = process.env.AGENTLAB_API ?? 'http://localhost:8000/api'
const CHROME = process.env.CHROME_PATH ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

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
const dataOf = (html) => JSON.parse(DATA_RE.exec(html)[2])
// 和服务端 bundle_json 一样转义 < > & 和 U+2028 / U+2029，改过的数据照样截不断数据块
const escapeJson = (obj) => JSON.stringify(obj)
  .replace(/[<>&\u2028\u2029]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`)
const withData = (html, obj) => html.replace(DATA_RE, (_, open, __, close) => open + escapeJson(obj) + close)

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
  failsIn: Object.fromEntries([...document.querySelectorAll('[data-check]')].map((c) => [c.dataset.check,
    new Set([...c.querySelectorAll('li[data-result="fail"]')].map((li) => li.textContent)).size])),
  numbers: document.querySelectorAll('button.num').length,
  numberStates: [...document.querySelectorAll('button.num')].reduce((acc, b) => {
    acc[b.dataset.result] = (acc[b.dataset.result] ?? 0) + 1
    return acc
  }, {}),
  sha: document.querySelector('[data-sha]')?.dataset.sha ?? null,
  overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
}))

const describe = (s) => Object.entries(s.checks).map(([k, v]) => `${k}=${v}`).join(' ')

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
    check('总览通过', s.overall === 'pass', `${s.overall}：${s.text}`)
    for (const key of ['events', 'manifest', 'artifacts', 'chain']) {
      check(`核对项「${key}」通过`, s.checks[key] === 'pass', s.checks[key])
    }
    check('数字核对没有不通过的', s.checks.numbers === 'pass' || s.checks.numbers === 'na',
      `${s.checks.numbers}：${JSON.stringify(s.numberStates)}`)
    check('没有发出任何网络请求', requests.length === 0, requests.slice(0, 3).join('、'))
    check('页面没有报错', errors.length === 0, errors.slice(0, 2).join(' | '))
    check('用的是浏览器内置的 SHA-256', s.sha === 'subtle', String(s.sha))
    check('封存哈希醒目地写在页面上', await page.locator('#seal-hash').innerText() === tamperable.meta.manifest_hash)
    // 页面列出的清单原文：拿出去用别的工具（这里是 node 的 crypto）算，等于封存哈希——读的人可以不信页面上的脚本
    const lines = await page.locator('[data-manifest-lines]').textContent()
    check('页面列出的清单原文在页面之外重算，等于封存哈希',
      createHash('sha256').update(lines ?? '', 'utf8').digest('hex') === tamperable.meta.manifest_hash)
    check('页面写明只证明内部一致、真伪要去系统里对照', /内部一致/.test(await page.locator('#seal-note').innerText())
      && /\/api\/runs\/[^/]+\/verify/.test(await page.locator('#seal-note').innerText()))
    if (s.numbers) {
      const first = page.locator('button.num[data-result="pass"]').first()
      const target = (await first.count()) ? first : page.locator('button.num').first()
      const key = await target.getAttribute('data-num')
      await target.click()
      await page.locator('#detail').waitFor({ state: 'visible', timeout: 5000 })
      const text = await page.locator('#detail').innerText()
      check('点开数字看得到出处和核对结果', /出处/.test(text) && /核对结果/.test(text) && /显示格式由服务端渲染/.test(text)
        && await page.locator('#detail').getAttribute('data-open') === key)
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
    check('照常全部通过', s.overall === 'pass' && !Object.values(s.checks).includes('fail'), describe(s))
    check('页脚写明用的是本页内置的实现', s.sha === 'js', String(s.sha))
    check('没有发出任何网络请求、没有报错', !requests.length && !errors.length, [...requests, ...errors].slice(0, 2).join(' | '))
  } finally {
    await ctx.close()
  }
})

// ---------------------------------------------------------------- 篡改

async function tampered(label, mutate, expectFail, keepPass = []) {
  const data = structuredClone(tamperable)
  const what = mutate(data)
  if (!what) {
    check(`篡改：${label}`, false, '这份导出里找不到可以篡改的对象')
    return
  }
  const file = writePage(`tampered-${expectFail}.html`, withData(html, data))
  const { ctx, page } = await openFile(file)
  try {
    const s = await stateOf(page)
    check(`篡改${label}：「${expectFail}」不通过、总览不通过`, s.checks[expectFail] === 'fail' && s.overall === 'fail'
      && s.failsIn[expectFail] >= 1, `${what}；${describe(s)}`)
    for (const key of keepPass) check(`篡改${label}：「${key}」仍通过`, s.checks[key] === 'pass', s.checks[key])
  } finally {
    await ctx.close()
  }
}

await section('篡改', async () => {
  if (skipped() || !tamperable) return
  // ① 改一条带规范字符串的事件内容（不动它记的哈希）：只有 ① 变红，清单照样对得上
  await tampered('一条事件的内容', (data) => {
    const ev = data.events.find((e) => e.type === 'run.started' && typeof e.canonical === 'string')
      ?? data.events.find((e) => typeof e.canonical === 'string')
    if (!ev) return null
    ev.canonical = /\d/.test(ev.canonical) ? ev.canonical.replace(/\d/, (d) => String((Number(d) + 1) % 10)) : `${ev.canonical} `
    return `第 ${ev.seq} 条（${ev.type}）`
  }, 'events', ['manifest'])
  // ② 改一份嵌入工件的一个字节：优先口径卡、快照，没有就改报告文档
  await tampered('一份工件的一个字节', (data) => {
    const embedded = data.artifacts.filter((a) => typeof a.b64 === 'string')
    const art = embedded.find((a) => a.kind !== 'report_doc') ?? embedded[0]
    if (!art) return null
    const bytes = Buffer.from(art.b64, 'base64')
    const at = bytes.findIndex((b) => b >= 0x30 && b <= 0x39)
    const index = at >= 0 ? at : bytes.length - 2
    bytes[index] = at >= 0 ? (bytes[index] === 0x39 ? 0x30 : bytes[index] + 1) : bytes[index] ^ 0x01
    art.b64 = bytes.toString('base64')
    return `${art.kind} ${art.id.slice(0, 8)} 的第 ${index} 字节`
  }, 'artifacts', ['events', 'manifest'])
  // 伪造：改报告里一个数字的引用值，再把文档、引用它的事件、清单的哈希全部重算
  await tampered('一个数字的引用值并重算全部哈希', (data) => {
    const sha = (text) => createHash('sha256').update(text).digest('hex')
    const docArt = data.artifacts.find((a) => a.kind === 'report_doc' && typeof a.b64 === 'string')
    if (!docArt) return null
    const doc = JSON.parse(Buffer.from(docArt.b64, 'base64').toString('utf8'))
    const embedded = new Set(data.artifacts.filter((a) => typeof a.b64 === 'string').map((a) => a.id))
    let hit = null
    for (const block of doc.blocks ?? []) for (const unit of block.units ?? []) for (const seg of unit.segments ?? []) {
      const c = seg.cite
      if (!hit && c?.status === 'resolved' && ['metric', 'cell'].includes(c.kind) && typeof c.value === 'number'
          && embedded.has(doc.catalog?.[c.alias]?.artifact)) hit = seg
    }
    if (!hit) return null
    hit.cite.value += 1
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
    return `片段 ${hit.id}（${hit.cite.kind}）`
  }, 'numbers', ['events', 'manifest', 'artifacts', 'chain'])
  // ③ 改元信息里的封存哈希
  await tampered('元信息里的封存哈希', (data) => {
    const h = data.meta.manifest_hash
    if (typeof h !== 'string' || !h) return null
    data.meta.manifest_hash = h.slice(0, -1) + (h.endsWith('0') ? '1' : '0')
    return '最后一位'
  }, 'manifest', ['events', 'artifacts'])
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

// ---------------------------------------------------------------- 记录页的导出按钮

const SAMPLE_NAME = '门店周报-可验证报告.html'

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
    const refusal = { status: 409, contentType: 'application/json',
      body: JSON.stringify({ detail: '封存核对不一致：事件记录与封存时不一致', code: 'bundle_seal_broken' }) }
    const { ctx, page } = await preview('bundle-provenance-sealed', { bundle: refusal })
    try {
      await page.locator('[data-bundle-export]').click()
      check('服务端回 409 时提示原因', await appears(page.getByText('封存核对不一致：事件记录与封存时不一致')))
      check('提示之后按钮恢复可点', await appears(page.locator('[data-bundle-export="ready"]')))
    } finally {
      await ctx.close()
    }
  }
  for (const [syn, reason] of [['bundle-provenance-running', '运行结束并封存后才能导出'],
    ['bundle-provenance-waiting', '等待审批中'], ['bundle-provenance-unsealed', '没有封存清单'],
    ['bundle-provenance-again', '运行结束并封存后才能导出']]) {
    const { ctx, page, hits } = await preview(syn)
    try {
      const button = page.locator('[data-bundle-export]')
      const described = await button.getAttribute('aria-describedby')
      const spoken = described ? await page.locator(`[id="${described}"]`).innerText() : ''
      const hover = await page.locator('[data-bundle-export]').locator('xpath=..').getAttribute('title')
      check(`${syn.replace('bundle-provenance-', '')}：按钮不可点，说明原因`, await button.isDisabled()
        && spoken.includes(reason) && (hover ?? '').includes(reason), spoken || '没有说明')
      await button.click({ force: true }).catch(() => {})
      check(`${syn.replace('bundle-provenance-', '')}：点了也不请求导出接口`, hits.length === 0)
    } finally {
      await ctx.close()
    }
  }
  {
    const { ctx, page } = await preview('bundle-provenance-sealed', { width: 390 })
    try {
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
      const box = await page.locator('[data-bundle-export]').boundingBox()
      check('390 宽：凭证条不横向溢出，导出按钮在屏幕内', overflow <= 0 && !!box && box.x >= 0 && box.x + box.width <= 390,
        `溢出 ${overflow}px`)
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
