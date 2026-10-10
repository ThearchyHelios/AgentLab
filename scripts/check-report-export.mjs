// 报告导出的检查：真服务端导出、断网打开点数字、不执行脚本也读得到正文；外加记录页对话框里那份导出按钮。
//
// 跑之前前后端都得起着（check-all 另起的那套，或者 ./scripts/dev.sh）。默认连 5273 / 8000；对别的实例跑时带上地址：
//   AGENTLAB_WEB=http://localhost:<前端端口> AGENTLAB_API=http://localhost:<后端端口>/api node scripts/check-report-export.mjs
//
// 分几段：
// - 导出：从服务端找一份成功结束、有报告文档的运行（GET /api/runs 找成功结束的，再看证据接口），下载 report.html：
//   状态码、类型、附件名（RFC 5987）、页面本身零外链、封存哈希不执行脚本也看得到、导出只读不追加事件。
//   找不到带报告文档的运行时照实报失败并说明原因；
// - 断网打开：拦掉一切网络请求，从本地文件打开：正文（服务端渲染的 HTML）读得到，头部写明报告节点、运行、封存状态；
//   点开带出处的数字看得到出处链——口径卡指标（口径、原式、代入式、输入、复算）、查询（SQL、被引用的行）、
//   数据源遮罩列写「已遮罩」；Esc 关掉面板；没有发出任何请求；
// - 不执行脚本：正文照样读得到（附件预览常常不执行脚本），封存哈希和运行 ID 静态写着；
// - 深浅色、窄屏：390 宽不横向溢出，点开的面板从底部抽出，深色下底是深的、字是浅的。
//
// 读的是真数据：输出里不打印工作流名、报告内容和数字，只打印计数和运行号前 8 位。下载的文件写在临时目录，跑完删掉。
// 导出接口只读（不追加事件、不改运行）。找不到能点开指标数字的运行时，只查单元格引用的那条链，原因写明。
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { homedir, tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)))
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

const TMP = mkdtempSync(join(tmpdir(), 'agentlab-report-'))
// 导出的页面带着真数据：中途崩了也要删掉
process.on('exit', () => rmSync(TMP, { recursive: true, force: true }))
const browser = await chromium.launch({ executablePath: CHROME })
const cleanup = async () => { await browser.close().catch(() => {}); rmSync(TMP, { recursive: true, force: true }) }

async function section(name, fn) {
  console.log(`\n=== ${name} ===`)
  try {
    await fn()
  } catch (e) {
    check(`${name} 中途出错`, false, String(e?.message ?? e).split('\n')[0])
  }
}

const getJson = async (path) => {
  const res = await fetch(`${API}${path}`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

const DATA_RE = /(<script type="application\/json" id="report-data">)([\s\S]*?)(<\/script>)/
const dataOf = (html) => JSON.parse(DATA_RE.exec(html)[2])

function writePage(name, html) {
  const file = join(TMP, name)
  writeFileSync(file, html)
  return file
}

/** 断网打开一份本地文件：除了 file:// 本身，任何请求都拦掉并记下来 */
async function openFile(file, { colorScheme = 'light', width = 1280, javaScript = true } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 }, colorScheme, javaScriptEnabled: javaScript })
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
  await page.goto(pathToFileURL(file).href)
  return { ctx, page, requests, errors }
}

// ---------------------------------------------------------------- 找一份可导出的运行

async function findRun() {
  let seen = 0
  let withDoc = 0
  let fallback = null
  let before = ''
  for (let p = 0; p < 5; p++) {
    let runs
    try {
      runs = await getJson(`/runs?status=succeeded&limit=200${before ? `&before=${encodeURIComponent(before)}` : ''}`)
    } catch (e) {
      return { why: `无法读取运行列表：${e.message}` }
    }
    for (const run of runs) {
      seen++
      const graph = await getJson(`/runs/${run.id}/evidence`).catch(() => null)
      const reports = (graph?.mode === 'cited' ? graph.reports ?? [] : []).filter((r) => r.doc_artifact && r.hash_ok === true)
      if (!reports.length) continue
      withDoc++
      const cited = reports.reduce((n, r) => n + (r.stats?.numbers_cited ?? 0), 0)
      if (cited > 0) return { run, cited, reports: reports.length }
      fallback ??= { run, cited, reports: reports.length }
    }
    if (runs.length < 200) break
    before = runs[runs.length - 1].created_at
  }
  if (fallback) return fallback
  if (!seen) return { why: '服务端里没有成功结束的运行' }
  return { why: `${seen} 次成功结束的运行里 ${withDoc} 次带报告文档，没有一次有带出处的数字` }
}

let picked = null
let html = ''

// ---------------------------------------------------------------- 导出

await section('导出', async () => {
  picked = await findRun()
  if (!picked.run) {
    check('找到一份成功结束、带报告文档的运行', false, picked.why)
    return
  }
  const { run } = picked
  check('找到一份成功结束、带报告文档的运行', true,
    `运行 ${run.id.slice(0, 8)}，${picked.reports} 份报告、${picked.cited} 个带出处的数字`)
  const before = await getJson(`/runs/${run.id}/events`).catch(() => null)
  const resp = await fetch(`${API}/runs/${run.id}/report.html`)
  const body = await resp.text()
  const ok = resp.status === 200 && /^text\/html/.test(resp.headers.get('content-type') ?? '')
  check('导出接口返回 200 和 text/html', ok, `HTTP ${resp.status}，${Math.round(Buffer.byteLength(body) / 1024)} KB`)
  html = ok && DATA_RE.test(body) ? body : ''
  const disposition = resp.headers.get('content-disposition') ?? ''
  const star = /filename\*=UTF-8''([^;]+)/.exec(disposition)?.[1]
  let name = ''
  try { name = star ? decodeURIComponent(star) : '' } catch { name = '' }
  check('附件名按 RFC 5987 编码，以「-报告.html」结尾', disposition.startsWith('attachment') && name.endsWith('-报告.html'))
  check('另带一个纯 ASCII 的文件名给老客户端', /filename="[\x20-\x7e]+"/.test(disposition))
  check('导出不缓存（no-store）', resp.headers.get('cache-control') === 'no-store')
  // 页面本身（去掉数据块）不许有任何 http(s):// 引用；数据块里的字是数据，浏览器不会去取
  const shell = html.replace(DATA_RE, '$1$3')
  check('页面零外链：没有 http(s)://、外部脚本、样式表、图片和字体', !/https?:\/\//i.test(shell)
    && !/<script[^>]*\ssrc\s*=|<link\b|<img\b|@import|url\s*\(/i.test(shell))
  check('内容安全策略默认不许连任何地方', /default-src 'none'/.test(shell))
  check('数据块里没有裸的 < > &（截不断）', !/[<>&]/.test(DATA_RE.exec(html)?.[2] ?? '<'))
  const after = await getJson(`/runs/${run.id}/events`).catch(() => null)
  check('导出不追加事件', !!before && !!after && before.length === after.length, before && after ? `${before.length} → ${after.length} 条` : '')
  const missing = await fetch(`${API}/runs/run-does-not-exist/report.html`)
  check('不存在的运行回 404', missing.status === 404)
  if (picked.cited === 0) note('这份运行的报告没有带出处的数字（模型没写引用标记），出处面板按无证据数字查')
})

const skipped = () => {
  if (html) return false
  note('没有拿到导出的页面（原因见上一节），这一节跳过')
  return true
}

/** 点开正文里一个数字，等出处面板出来，交回面板里的全部文字（看完按 Esc 关掉） */
async function detailText(page, locator) {
  await locator.click()
  await page.locator('#detail').waitFor({ state: 'visible', timeout: 5000 })
  const text = await page.locator('#detail').innerText()
  await page.keyboard.press('Escape')
  return text
}

// ---------------------------------------------------------------- 断网打开

await section('断网打开', async () => {
  if (skipped()) return
  const data = dataOf(html)
  const { ctx, page, requests, errors } = await openFile(writePage('report.html', html))
  try {
    check('没有发出任何网络请求', requests.length === 0, requests.slice(0, 3).join('、'))
    check('页面没有报错', errors.length === 0, errors.slice(0, 2).join(' | '))
    // 头部：报告节点、运行、封存状态
    const head = await page.locator('header').innerText()
    check('头部写明报告节点和运行 ID', /报告节点「/.test(head) && head.includes(data.meta.run_id.slice(0, 8)), head.slice(0, 120))
    check('封存状态写明（已封存给清单哈希）', /封存/.test(head) && (data.meta.seal?.sealed ? /清单哈希/.test(head) : true))
    // 正文：服务端渲染的 HTML
    const docText = await page.locator('#report').innerText()
    check('正文整份读得到（服务端渲染）', docText.trim().length > 0, `${docText.trim().length} 字`)
    const buttons = page.locator('#report button[data-seg]')
    const count = await buttons.count()
    check('有状态的数字、取值、名称都画成了按钮', count > 0, `${count} 个`)
    const refs = await page.evaluate(() => Object.keys(JSON.parse(document.getElementById('report-data').textContent).refs ?? {}))
    check('每个按钮都点得开一条出处链', count === refs.length || refs.length >= count, `按钮 ${count} 个、链 ${refs.length} 条`)
    // 点开一个带出处的数字
    const cited = page.locator('#report button[data-seg][data-state="deterministic"]').first()
    if (await cited.count()) {
      const key = await cited.getAttribute('data-seg')
      const text = await detailText(page, cited)
      const chains = Object.fromEntries(Object.entries(data.chains ?? {}))
      const chain = chains[data.refs[key] ?? ''] ?? []
      const kinds = chain.map((s) => s.step)
      // 每种步骤在面板里的标题（runtime.js 的 PARTS）：链里有的，面板都得画出一节
      const TITLES = { metric: '口径卡指标', input: '输入', query: '查询', run_input: '运行输入',
        entity: '表或字段', quote: '引文' }
      check('点开数字面板写明类型和状态', /「/.test(text) && /(有出处|无证据|未裁判|猜测)/.test(text))
      check('面板列出出处链的每个步骤', kinds.length > 0
        ? kinds.every((k) => text.includes(TITLES[k] ?? k)) : /没有可展示的出处链/.test(text),
        `${kinds.join('、') || '（空链）'}｜${text.slice(0, 160).replace(/\n/g, ' ')}`)
      if (kinds.includes('metric')) {
        check('指标步骤带口径、计算方式（原式、代入式）、输入和复算',
          /口径卡「/.test(text) && /原式（计算方式）/.test(text) && /代入式/.test(text)
            && (/输入/.test(text) || /缺少输入/.test(text)) && /复算/.test(text), text.slice(0, 300))
      }
      if (kinds.includes('query')) {
        check('查询步骤带 SQL 和被引用的行', /SQL/.test(text) && /被引用的行/.test(text), text.slice(0, 300))
        const masked = chain.filter((s) => s.step === 'query' && Array.isArray(s.masked) && s.masked.length)
        if (masked.length) check('遮罩列的值写「已遮罩」', /已遮罩/.test(text))
      }
      // 面板开着再点另一个：换到那一个，Esc 关掉
      const another = page.locator('#report button[data-seg][data-state="deterministic"]').nth(1)
      if (await another.count()) {
        await another.click()
        await page.locator('#detail').waitFor({ state: 'visible', timeout: 5000 })
        check('面板开着时点另一个数字，换到那一个', await page.locator('#detail').getAttribute('data-open') === await another.getAttribute('data-seg'))
      }
      await page.keyboard.press('Escape')
      check('Esc 关掉出处面板', await page.locator('#detail').isHidden())
    } else {
      check('报告里有可点的数字', picked?.cited === 0, '证据图说有带出处的数字，页面上却一个都没有')
    }
  } finally {
    await ctx.close()
  }
})

// ---------------------------------------------------------------- 不执行脚本

await section('不执行脚本', async () => {
  if (skipped()) return
  const data = dataOf(html)
  const { ctx, page } = await openFile(join(TMP, 'report.html'), { javaScript: false })
  try {
    const docText = await page.locator('#report').innerText()
    check('正文照样整份读得到（附件预览常常不执行脚本）', docText.trim().length > 0, `${docText.trim().length} 字`)
    const head = await page.locator('header').innerText()
    check('运行 ID 和封存哈希静态写着', head.includes(data.meta.run_id.slice(0, 8))
      && (!data.meta.seal?.manifest_hash || head.includes(data.meta.seal.manifest_hash.slice(0, 12))), head.slice(0, 160))
  } finally {
    await ctx.close()
  }
})

// ---------------------------------------------------------------- 深浅色与窄屏

await section('深浅色与窄屏', async () => {
  if (skipped()) return
  const dark = await openFile(join(TMP, 'report.html'), { colorScheme: 'dark' })
  try {
    const deep = await dark.page.evaluate(() => getComputedStyle(document.body).backgroundColor)
    const [r, g, b] = (/\((\d+), (\d+), (\d+)/.exec(deep) ?? []).slice(1).map(Number)
    check('深色下底是深的、字是浅的', r + g + b < 200, deep)
  } finally {
    await dark.ctx.close()
  }
  const narrow = await openFile(join(TMP, 'report.html'), { width: 390 })
  try {
    check('390 宽不横向溢出', await narrow.page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth))
    const first = narrow.page.locator('#report button[data-seg]').first()
    if (await first.count()) {
      await first.click()
      await narrow.page.locator('#detail').waitFor({ state: 'visible', timeout: 5000 })
      const fromBottom = await narrow.page.evaluate(() => {
        const r = document.getElementById('detail').getBoundingClientRect()
        return r.bottom >= window.innerHeight - 2 || r.top > window.innerHeight * 0.2
      })
      check('窄屏上面板从底部抽出', fromBottom)
      await narrow.page.keyboard.press('Escape')
    } else {
      note('正文里没有可点的片段，窄屏面板没查')
    }
  } finally {
    await narrow.ctx.close()
  }
})

// ---------------------------------------------------------------- 问数据成果上的导出按钮

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const SAMPLE_NAME = '门店周报-报告.html'

/** 预览页 ?syn=evidence（带逐段证据的一轮）：报告文档和导出接口由 page.route 伪造，非 GET 一律掐掉 */
async function preview(syn, { reportHtml } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true })
  const page = await ctx.newPage()
  const hits = []
  // 后注册的路由先匹配：兜底的先注册，假装报告文档和导出接口的后注册，请求才落到假接口上
  await page.route('**/api/**', (route) => (route.request().method() === 'GET' ? route.continue() : route.abort()))
  const fx = JSON.parse(readFileSync(join(ROOT, 'frontend/src/run/__tests__/evidence-doc.json'), 'utf8'))
  // 报告文档和导出接口的后注册才落到假接口上。导出接口带查询串（?report=…），模式末尾的 * 把它吞进匹配
  await page.route(`**/api/artifacts/${fx.doc_artifact}`, (route) => route.fulfill({
    contentType: 'application/json', body: JSON.stringify({ id: fx.doc_artifact, content: fx.doc }),
  }))
  await page.route('**/api/runs/*/report.html*', (route) => {
    const url = new URL(route.request().url())
    hits.push(url.pathname + url.search)
    return route.fulfill(reportHtml ?? {
      status: 200,
      headers: { 'content-type': 'text/html; charset=utf-8',
        'content-disposition': `attachment; filename="agentlab-runevid-report.html"; filename*=UTF-8''${encodeURIComponent(SAMPLE_NAME)}` },
      body: '<!doctype html><title>report</title>',
    })
  })
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  await page.goto(`${WEB}/preview.html?syn=${syn}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-turn]', { timeout: 15_000 })
  return { ctx, page, hits, errors, fx }
}

await section('问数据成果上的导出按钮', async () => {
  if (!WEB) {
    note('没有前端地址（AGENTLAB_WEB），这一节跳过')
    return
  }
  {
    const { ctx, page, hits, errors, fx } = await preview('evidence')
    try {
      const html = page.locator('[data-turn="evidence"] [data-answer-export="html"]')
      check('带证据的成果：导出按钮在，悬停写明数字点得开出处', await html.count() === 1
        && (await html.getAttribute('title')).includes('口径、来源和计算方式'))
      // 按钮浮在答案右上角、悬停才显形：先挪上去再点，点击本身才落在按钮上
      await page.locator('[data-turn="evidence"]').hover()
      await html.hover()
      const [download] = await Promise.all([page.waitForEvent('download', { timeout: 10_000 }), html.click()])
      check('下载的文件名同服务端（RFC 5987 的中文名）', download.suggestedFilename() === SAMPLE_NAME, download.suggestedFilename())
      check('请求的是这次运行、点名的报告节点的导出接口', hits.length === 1
        && hits[0] === `/api/runs/${fx.run_id}/report.html?report=write`, hits.join('、'))
      check('导出后提示文件名', await page.getByText(`已导出「${SAMPLE_NAME}」`).first().isVisible())
      check('页面没有报错', !errors.length, errors.slice(0, 2).join(' | '))
      const md = page.locator('[data-turn="evidence"] [data-answer-export="md"]')
      check('Markdown 照旧留着一个小的', await md.count() === 1 && (await md.innerText()).trim() === 'MD')
    } finally {
      await ctx.close()
    }
  }
  {
    // 没有证据的旧答案：只有 Markdown 导出，没有 HTML 按钮
    const { ctx, page, hits } = await preview('evidence')
    try {
      const legacy = page.locator('[data-turn="evidence-legacy"]')
      check('旧运行（没有 _evidence）：没有 HTML 导出按钮', await legacy.locator('[data-answer-export="html"]').count() === 0)
      const md = legacy.locator('button[title="导出为 Markdown 文件"]')
      check('旧运行：照旧只有 Markdown 导出', await md.count() === 1)
      await legacy.hover()
      const [download] = await Promise.all([page.waitForEvent('download', { timeout: 10_000 }), md.click()])
      check('旧运行：下载的是 .md 文件', download.suggestedFilename().endsWith('.md'), download.suggestedFilename())
      check('旧运行：不请求导出接口', hits.length === 0, hits.join('、'))
    } finally {
      await ctx.close()
    }
  }
  {
    // 服务端回 409（文档被改过）：原样提示原因，按钮恢复可点
    const refusal = { status: 409, contentType: 'application/json',
      body: JSON.stringify({ detail: '报告文档与哈希不一致，疑似被修改，不能导出', code: 'evidence_doc_tampered' }) }
    const { ctx, page } = await preview('evidence', { reportHtml: refusal })
    try {
      await page.locator('[data-turn="evidence"]').hover()
      await page.locator('[data-turn="evidence"] [data-answer-export="html"]').click()
      check('服务端回 409 时原样提示原因',
        await appears(page.getByText('报告文档与哈希不一致，疑似被修改，不能导出')))
      check('提示之后按钮恢复可点',
        await appears(page.locator('[data-turn="evidence"] [data-answer-export="html"]:enabled'), 8000))
    } finally {
      await ctx.close()
    }
  }
})

await cleanup()
console.log(failed ? `\n${failed} 项不通过` : '\n全部通过')
process.exit(failed ? 1 : 0)
