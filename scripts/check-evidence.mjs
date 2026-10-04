// 可点击证据（第一期：数字层）的回归检查：报告文档逐段渲染、证据面板、键盘、读屏、
// 减少动效、窄栏，以及旧契约「按位置标记」和没有 _evidence 的旧回答。
//
// 守的是这些「看着没坏、其实用不了」的地方：两种状态的下划线长得一样（去掉颜色就分不
// 出有没有出处）、整份报告每个数字各占一个 Tab 位、面板关了焦点丢到页首、减少动效时面板
// 照样滑进来、360px 的画布右栏被面板撑出横向滚动、日期里的 15 被当成回指不上的 15、
// 旧运行的答案被新组件改了样子。
//
// 跑在预览页上：/ui-harness.html?evidence=1（组件）和 /preview.html?syn=evidence（问数据页
// 的答案形态）。文档是后端 compose_doc 真跑出来的夹具（frontend/src/run/__tests__/
// evidence-doc.json，只用通用名）；片段接口、证据图、报告工件都用 page.route 伪造，非 GET
// 一律拦掉，不写库。
//
// 跑之前前端得起着（./scripts/dev.sh），默认连 5273。对别的实例（比如一份沙箱拷贝）跑时
// 带上地址：
//   AGENTLAB_WEB=http://localhost:<前端端口> AGENTLAB_API=http://localhost:<后端端口>/api node scripts/check-evidence.mjs
// EVIDENCE_SHOTS=<目录> 时亮暗两套各截几张，供人眼复核。
import { readFileSync } from 'node:fs'
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const CHROME = process.env.CHROME_PATH
  ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const SHOTS = process.env.EVIDENCE_SHOTS
const root = new URL('..', import.meta.url).pathname
const fx = JSON.parse(readFileSync(`${root}frontend/src/run/__tests__/evidence-doc.json`, 'utf8'))
// 第二期：报告直接引用查询单元格、整表，口径卡的输入核对到 agent 字段和查询快照。文档同样是
// compose_doc 真跑出来的（查询快照走 loader），片段接口的答复按方案第 5 节和 NOTES-A2 的形状拼
const fxq = JSON.parse(readFileSync(`${root}frontend/src/run/__tests__/evidence-query.json`, 'utf8'))
// 第三期：表名字段名（反引号里的、标记的、自动链接的）、可疑实体、核对不了的名字、逐字引文，
// 以及没有契约的旧运行按数值猜的候选。文档和猜测都是后端真跑出来的（compose_doc / guess_sources）
const fxe = JSON.parse(readFileSync(`${root}frontend/src/run/__tests__/evidence-entity.json`, 'utf8'))
// 第四期：结论句裁判。文档由 compose_doc 真跑，候选句由 judge.prepare 真跑，判定是剧本（模拟裁判模型），
// 经 judge 的 summarize / apply_judgement 写回：正式运行（节点里判好的）、探索运行（按需）、没开裁判的探索运行
const fxj = JSON.parse(readFileSync(`${root}frontend/src/run/__tests__/evidence-judge.json`, 'utf8'))
// 裁判拆档（JR）：证据相矛盾、证据不足（写明缺什么）、讲方法的句子、裁判模型和写作模型相同、模型不在价格表里。
// 文档由 compose_doc 真跑、证据链由 api/evidence.py 的 _chain 真跑；判定和表名实体步骤的 fields 照前后端接口约定写
const fxv = JSON.parse(readFileSync(`${root}frontend/src/run/__tests__/evidence-verdict.json`, 'utf8'))
const JUDGE_SETS = [fxj.formal, fxj.explore, fxj.plain, fxv.formal, fxv.explore]
// Excel 导入期 4：推断的来源。文档由 compose_doc 真跑（带文档标记），片段答复由 _chain 真跑，推断来源的答复按契约
// （backend/app/data/provenance_types.py）拼；cases 写明每个场景是哪个片段、片段文字、覆盖 P4-SPEC 4.4 的哪几条
const fxp = JSON.parse(readFileSync(`${root}frontend/src/run/__tests__/evidence-provenance.json`, 'utf8'))
/** 推断来源夹具里各期导入清单的工件 id：夹具只收了两份清单，其余的按「工件不存在」答，不落到真服务端 */
const FXP_MANIFESTS = new Set(Object.values(fxp.provenance).flatMap((a) => a.version?.parts ?? []).map((p) => p.manifest))
/** 夹具里某段文字是哪个片段：检查按文字认片段，重新生成夹具时编号变了也不用改 */
const segOf = (doc, text) => doc.blocks.flatMap((b) => b.units.flatMap((u) => u.segments)).find((s) => s.text === text)?.id

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}
// EVIDENCE_ONLY=look,keys 只跑这几节（改坏验证时省时间）；check-all 不会把它传下来
const ONLY = (process.env.EVIDENCE_ONLY ?? '').split(',').filter(Boolean)
async function section(key, name, fn) {
  if (ONLY.length && !ONLY.includes(key)) return
  console.log(`\n=== ${name} ===`)
  try {
    await fn()
  } catch (e) {
    check(`${name} 中途出错`, false, String(e?.message ?? e).split('\n')[0])
  }
}

const browser = await chromium.launch({ executablePath: CHROME })

/**
 * 按需裁判的伪造答复：evidence.judged 的载荷形状（NOTES-A4 §8、E4-node 的 POST …/evidence/judge）。
 * 夹具里按句子备好了答复的用夹具的；别的句子给一条「有依据」。正式运行回 409
 */
function judgeReply(set, body) {
  if (set === fxj.formal || set === fxv.formal) return { status: 409, json: { detail: '正式运行的结论句已在报告撰写节点内完成裁判，结果随报告一起封存，不能再按需裁判', code: 'evidence_judge_formal' } }
  const unit = body?.units?.[0]
  const named = Object.entries(set.units).find(([, u]) => u === unit)?.[0]
  if (named && set.judge?.[named]) return { json: set.judge[named] }
  return { json: { report: { node_id: 'write', doc_artifact: set.doc_artifact }, verdicts: { [unit]: { status: 'supported',
    rationale: '引用的证据支持这句话', judge: fxj.model, post_seal: true, used: [] } }, model: fxj.model, limits_hit: [], post_seal: true } }
}

/**
 * 伪造证据相关的 GET；片段请求记账（缓存：同一个片段点两次只取一次）。四期：按需裁判的 POST 也在这里认
 * （记进 hits 的 judge:<运行>:<句子>），别的非 GET 一律拦掉。judge 给了就用它答（改坏验证、触顶、出错）；
 * segPatch 给了就改片段接口的答复（比如 on_demand 说运行还没封存）。
 *
 * Excel 导入期 4：推断来源接口（…/segments/<id>/provenance）在片段正则之前单独认，不分运行、不分夹具，一律记成
 * prov:<运行>:<片段>——片段正则以 $ 结尾认不出它，落到 r.continue() 会得到一个没有机读码的 404，界面按「老服务端」
 * 三节都不画，只看 DOM 的断言不论前端发没发请求都会过。推断来源夹具的运行按 provenance[<片段>] 作答，其余一律回
 * 可识别的哨兵（418，harness_unexpected_provenance），界面会画出「未能获取推断的来源」。prov 给了就由它改答复
 * （prov(运行, 片段, 默认答复) → {status?, json} 或它的 Promise：延迟、出错、老服务端）
 */
async function routed(page, { judge = null, segPatch = null, prov = null } = {}) {
  const hits = []
  const judged = new Set()
  await page.route((u) => new URL(u).pathname.startsWith('/api/'), async (r) => {
    const req = r.request()
    const url = new URL(req.url())
    const jset = JUDGE_SETS.find((x) => url.pathname.includes(`/runs/${x.run_id}/`) || url.pathname.endsWith(`/runs/${x.run_id}`))
    if (jset && req.method() === 'POST' && /\/evidence\/judge$/.test(url.pathname)) {
      const body = req.postDataJSON()
      hits.push(`judge:${jset.run_id}:${(body?.units ?? []).join(',')}:${body?.report ?? ''}`)
      const reply = judge ? judge(jset, body) : judgeReply(jset, body)
      if (reply.status == null || reply.status < 300) for (const u of body?.units ?? []) judged.add(`${jset.run_id}:${u}`)
      return r.fulfill({ status: reply.status ?? 200, json: reply.json })
    }
    if (req.method() !== 'GET') return r.abort()
    const pm = url.pathname.match(/\/api\/runs\/([^/]+)\/evidence\/segments\/([^/]+)\/provenance$/)
    if (pm) {
      const [run, sid] = [decodeURIComponent(pm[1]), decodeURIComponent(pm[2])]
      hits.push(`prov:${run}:${sid}`)
      const own = run === fxp.run_id && fxp.provenance[sid]
      let reply = own ? { json: structuredClone(own) } : fxp.errors.unexpected
      if (prov) reply = await prov(run, sid, reply)
      return r.fulfill({ status: reply.status ?? 200, json: reply.json })
    }
    if (url.pathname.includes(`/runs/${fxp.run_id}/`)) {
      const seg = url.pathname.match(/\/evidence\/segments\/([^/]+)$/)
      if (seg) {
        const id = decodeURIComponent(seg[1])
        hits.push(`p:${id}`)
        const body = fxp.segments[id]
        return body ? r.fulfill({ json: structuredClone(body) })
          : r.fulfill({ status: 404, json: { detail: '报告中没有这个片段', code: 'evidence_segment_not_found' } })
      }
      if (/\/evidence$/.test(url.pathname)) return r.fulfill({ json: fxp.graph })
    }
    // 「查看导入清单」按清单的工件 id 取（内容寻址）
    const art = url.pathname.match(/^\/api\/artifacts\/([^/]+)$/)?.[1]
    if (art && fxp.artifacts[art]) {
      hits.push(`artifact:P:${art}`)
      return r.fulfill({ json: fxp.artifacts[art] })
    }
    if (art && FXP_MANIFESTS.has(art)) return r.fulfill({ status: 404, json: { detail: '工件不存在' } })
    // 四期的报告工件：问数据页的答案（Output → EvidenceField）按工件 id 取文档
    const jart = JUDGE_SETS.find((x) => url.pathname === `/api/artifacts/${x.doc_artifact}`)
    if (jart) return r.fulfill({ json: { id: jart.doc_artifact, content: jart.doc } })
    // 裁判拆档：讲方法的句子在「挂的依据」下列出 SQL，取的是查询快照工件
    const vsnap = Object.entries(fxv.snapshots).find(([, x]) => url.pathname === `/api/artifacts/${x.artifact}`)
    if (vsnap) {
      hits.push(`artifact:V:${vsnap[0]}`)
      return r.fulfill({ json: { id: vsnap[1].artifact, content: vsnap[1].content } })
    }
    if (jset) {
      const seg = url.pathname.match(/\/evidence\/segments\/([^/]+)$/)
      if (seg) {
        const id = decodeURIComponent(seg[1])
        hits.push(`j:${id}`)
        // 判过之后片段接口按最新的 evidence.judged 叠上判定（封存后追加）：夹具里备着判过那一句的答复
        const trigger = jset.units.bad ?? jset.units.method
        const after = jset.segments_after && judged.has(`${jset.run_id}:${trigger}`) ? jset.segments_after : jset.segments
        let body = structuredClone(after[id])
        if (body && segPatch) body = segPatch(jset, body)
        return body ? r.fulfill({ json: body })
          : r.fulfill({ status: 404, json: { detail: '报告中没有这个片段', code: 'evidence_segment_not_found' } })
      }
      if (/\/evidence$/.test(url.pathname)) return r.fulfill({ json: jset.graph })
      if (url.pathname.endsWith(`/runs/${jset.run_id}`)) {
        hits.push(`run:${jset.run_id}`)
        return r.fulfill({ json: { id: jset.run_id, run_class: jset.run_class, status: 'succeeded' } })
      }
    }
    const seg = url.pathname.match(/\/api\/runs\/([^/]+)\/evidence\/segments\/([^/]+)$/)
    // 按运行认夹具：第一期、第二期（查询链）、第三期（实体、引文）各有自己的片段
    const set = url.pathname.includes(`/runs/${fxq.run_id}/`) ? fxq : url.pathname.includes(`/runs/${fxe.run_id}/`) ? fxe : fx
    if (seg) {
      hits.push(`${set === fxq ? 'q:' : set === fxe ? 'e:' : ''}${decodeURIComponent(seg[2])}`)
      const body = set.segments[decodeURIComponent(seg[2])]
      return body ? r.fulfill({ json: body })
        : r.fulfill({ status: 404, json: { detail: '报告中没有这个片段', code: 'evidence_segment_not_found' } })
    }
    if (/\/api\/runs\/[^/]+\/evidence$/.test(url.pathname)) return r.fulfill({ json: set.graph })
    if (url.pathname === `/api/artifacts/${fx.doc_artifact}`) return r.fulfill({ json: { id: fx.doc_artifact, content: fx.doc } })
    if (url.pathname === `/api/artifacts/${fxq.snapshots.Q3.artifact}`) {
      hits.push('artifact:Q3')
      // 工件接口给的是原值（遮罩只在证据接口里换）：夹具里存的是换过的，这里换回「原值」
      const content = structuredClone(fxq.snapshots.Q3.content)
      const j = content.columns.indexOf('phone')
      content.rows.forEach((row, i) => { row[j] = `RAW-PHONE-${i}` })
      return r.fulfill({ json: { id: fxq.snapshots.Q3.artifact, content } })
    }
    return r.continue()
  })
  return hits
}

async function open(url, { w = 1280, h = 1000, reduced = false, patchQuery = null, judge = null, segPatch = null, prov = null } = {}) {
  const ctx = await browser.newContext({ viewport: { width: w, height: h }, ...(reduced ? { reducedMotion: 'reduce' } : {}) })
  const page = await ctx.newPage()
  // 找不到元素时 8 秒就报，不等默认的 30 秒：改坏验证时一节里十几处都找不到
  page.setDefaultTimeout(8000)
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  const hits = await routed(page, { judge, segPatch, prov })
  // 预览页直接 import 第二期夹具（vite 把 JSON 当模块给）：要一份改过的文档时把那个模块换掉
  if (patchQuery) {
    await page.route((u) => new URL(u).pathname.endsWith('/run/__tests__/evidence-query.json'), (r) =>
      r.fulfill({ contentType: 'application/javascript', body: `export default ${JSON.stringify(patchQuery(structuredClone(fxq)))}` }))
  }
  await page.goto(`${WEB}${url}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-evidence-doc], [data-turn]', { timeout: 8000 })
  return { ctx, page, errors, hits }
}

const active = (page) => page.evaluate(() => document.activeElement?.getAttribute('data-seg')
  ?? (document.activeElement?.closest('[data-evidence-panel]') ? 'panel' : document.activeElement?.tagName ?? null))
const panel = (page) => page.locator('[data-evidence-panel]')

let harness
try {
  harness = await open('/ui-harness.html?evidence=1')
} catch (e) {
  console.error(`✗ 打不开预览页（${WEB}/ui-harness.html?evidence=1）——前端没起？先跑 ./scripts/dev.sh\n  ${e.message}`)
  await browser.close()
  process.exit(1)
}
const { page } = harness
const wide = '#evidence-wide'

await section('look', '两种状态四通道都分得开：线型、字形、颜色、文字', async () => {
  const look = await page.evaluate((sel) => {
    const pick = (state) => {
      const el = document.querySelector(`${sel} [data-ev-state="${state}"]`)
      if (!el) return null
      const cs = getComputedStyle(el)
      return { line: cs.textDecorationLine, style: cs.textDecorationStyle, color: cs.textDecorationColor,
               tag: el.tagName, type: el.getAttribute('type'), font: cs.fontSize, fg: cs.color }
    }
    const body = getComputedStyle(document.querySelector(`${sel} [data-evidence-doc] p`)).color
    return { det: pick('deterministic'), none: pick('none'), body }
  }, wide)
  check('确定性画实线下划线', look.det?.line.includes('underline') && look.det.style === 'solid', JSON.stringify(look.det))
  check('无证据画点状下划线', look.none?.line.includes('underline') && look.none.style === 'dotted', JSON.stringify(look.none))
  check('两种线型不同（读计算后的 text-decoration-style）', look.det?.style !== look.none?.style)
  check('两种下划线颜色也不同', look.det?.color !== look.none?.color, `${look.det?.color} / ${look.none?.color}`)
  check('片段是原生 button type=button', look.det?.tag === 'BUTTON' && look.det.type === 'button')
  check('片段外观和正文一样：字色、字号继承正文', look.det?.fg === look.body, `${look.det?.fg} vs ${look.body}`)
  const labels = await page.evaluate((sel) => Object.fromEntries(['s8', 's16', 's33', 's4'].map((id) =>
    [id, document.querySelector(`${sel} [data-seg="${id}"]`)?.getAttribute('aria-label') ?? ''])), wide)
  check('aria-label 写了状态和出处（有出处）', labels.s8 === '8.7%，有出处：口径卡指标 环比增幅', labels.s8)
  check('aria-label 写了状态和原因（裸数字）', labels.s16.startsWith('12，无证据：'), labels.s16)
  check('aria-label 写了解析不了的原因', labels.s33.includes('无证据') && labels.s33.includes('显示为 0'), labels.s33)
  check('运行输入也写明出处', labels.s4 === '2026-W37，有出处：运行输入 week', labels.s4)
  const tags = await page.locator(`${wide} .ev-tag`).allInnerTexts()
  check('含无证据的句子末尾挂「?无证据」（字形 + 文字），表格格子里不挂', tags.length === 5 && tags.every((t) => t === '?无证据'),
    tags.join('|'))
  const summary = await page.locator(`${wide} [data-evidence-summary]`).innerText().catch(() => '')
  check('顶部有一段读屏摘要，说清计数和按键', summary.includes('7/12 数字有出处 · 无证据 5 · 另有 1 处引用无法解析') && summary.includes('方向键'), summary)
  check('读屏摘要说清画不了线的在哪：列表序号里的数字，不混说成「句末依据」', summary.includes('其中 1 个数字位于列表序号')
    && !summary.includes('句末依据'), summary)
  const sr = await page.evaluate((sel) => {
    const el = document.querySelector(`${sel} [data-evidence-summary]`)
    const r = el?.getBoundingClientRect()
    return !!r && r.width <= 1 && r.height <= 1
  }, wide)
  check('读屏摘要不占视觉位置（sr-only）', sr)
  const tally = await page.locator(`${wide} [data-evidence-tally]`).innerText().catch(() => '')
  check('没有出具横幅时文档自己给出计数条', tally.includes('7/12 数字有出处 · 无证据 5 · 另有 1 处引用无法解析') && tally.includes('定位下一处'), tally)
  const [cited, total, gaps] = (tally.match(/(\d+)\/(\d+) 数字有出处 · 无证据 (\d+)/) ?? []).slice(1).map(Number)
  check('计数算得平：无证据 = 总数 − 有出处', total - cited === gaps, tally)
})

await section('keys', '键盘：整份报告一个 Tab 位，←/→、↑/↓、n/N', async () => {
  const stops = await page.locator(`${wide} [data-seg][tabindex="0"]`).count()
  const segs = await page.locator(`${wide} [data-seg]`).count()
  check('只有一个片段在 Tab 顺序里（roving tabindex）', stops === 1 && segs > 5, `${stops}/${segs}`)
  await page.locator(`${wide} [data-evidence-list]`).focus()
  await page.keyboard.press('Tab')
  check('Tab 进报告落在第一个片段', await active(page) === 's4', await active(page))
  await page.keyboard.press('Tab')
  // 下一个 Tab 位是表格的「复制为 CSV」之类的控件，不能是另一个片段
  const next = await active(page)
  check('再按一次 Tab 不会落到另一个片段上', !/^s\d+$/.test(String(next)), String(next))
  await page.locator(`${wide} [data-seg="s4"]`).focus()
  const walk = []
  for (const key of ['ArrowRight', 'ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp']) {
    await page.keyboard.press(key)
    walk.push(await active(page))
  }
  check('→ → ← 在片段间走', walk.slice(0, 3).join(',') === 's6,s8,s6', walk.join(','))
  check('↓ 到下一句的第一个片段，↑ 回来', walk[3] === 's16' && walk[4] === 's4', walk.join(','))
  const stop0 = await page.locator(`${wide} [data-seg][tabindex="0"]`).getAttribute('data-seg')
  check('Tab 位跟着焦点走', stop0 === 's4', stop0)
  const jumps = []
  for (const key of ['n', 'n', 'Shift+N', 'N']) {
    await page.keyboard.press(key)
    jumps.push(await active(page))
  }
  check('n 跳到下一处无证据、N / Shift+N 回上一处', jumps.join(',') === 's16,s26,s16,s36', jumps.join(','))
  await page.keyboard.press('End')
  check('End 到最后一个片段', await active(page) === 's49', await active(page))
  await page.keyboard.press('ArrowRight')
  check('→ 首尾相接', await active(page) === 's4', await active(page))
  check('只是走动不打开面板', await panel(page).count() === 0)
})

await section('panel', '面板：回车打开、能看到代入式、Esc 关掉焦点回原片段', async () => {
  await page.locator(`${wide} [data-seg="s8"]`).focus()
  await page.keyboard.press('Enter')
  await page.waitForSelector('[data-evidence-panel] [data-ev-substituted]', { timeout: 4000 })
  const p = panel(page)
  check('宽屏从侧边弹出', await p.getAttribute('data-evidence-panel') === 'side')
  const sub = await p.locator('[data-ev-substituted]').innerText()
  check('代入式', sub === 'round((45678.5 - 42010.0) / 42010.0 * 100, 1)', sub)
  check('原式', (await p.locator('[data-ev-expression]').innerText()).includes('vars.kpi.gmv_prev'))
  const chips = await p.locator('[data-ev-input]').allInnerTexts()
  check('每个输入是「值 ← 路径」的小标签', chips.join('|') === '45,678.5 ← vars.kpi.gmv|42,010 ← vars.kpi.gmv_prev', chips.join('|'))
  const text = await p.innerText()
  check('指标名称、值、口径与版本', text.includes('环比增幅') && text.includes('8.7%') && text.includes('口径卡「周报口径」') && text.includes('v2'))
  check('复算结果', await p.locator('[data-ev-recompute]').getAttribute('data-ev-recompute') === 'ok' && text.includes('复算一致'))
  check('封存状态用状态徽标', await p.locator('[data-ev-seal] svg[data-status="done"]').count() === 1 && text.includes('已封存 · 核对一致'))
  check('所在的句子里点开的那段描底', (await p.locator('[data-ev-sentence] [data-ev-here]').innerText()) === '8.7%')
  check('片段标着展开了哪个面板', await page.locator(`${wide} [data-seg="s8"]`).getAttribute('aria-expanded') === 'true'
    && !!(await page.locator(`${wide} [data-seg="s8"]`).getAttribute('aria-controls')))
  check('焦点留在片段上，读屏从 live region 听到标题', await active(page) === 's8'
    && (await page.locator(`${wide} [aria-live="polite"]`).innerText()).includes('8.7%，有出处'))

  await page.keyboard.press('ArrowRight')
  await page.waitForTimeout(150)
  check('面板开着时方向键走到哪，面板跟到哪', (await p.locator('h3').innerText()) === '1,234单', await p.locator('h3').innerText())
  await page.keyboard.press('Escape')
  await page.waitForTimeout(150)
  check('Esc 关面板，焦点仍在原片段', await panel(page).count() === 0 && await active(page) === 's10', await active(page))

  await page.keyboard.press('Enter')
  await page.waitForSelector('[data-evidence-panel]')
  await page.keyboard.press('Tab')
  check('Tab 从片段直接进面板', await active(page) === 'panel', await active(page))
  await page.keyboard.press('Escape')
  await page.waitForTimeout(200)
  check('在面板里按 Esc：关掉并把焦点还回原片段', await panel(page).count() === 0 && await active(page) === 's10', await active(page))

  await page.locator(`${wide} [data-seg="s8"]`).click()
  await page.waitForSelector('[data-evidence-panel] [data-ev-substituted]')
  const again = harness.hits.filter((h) => h === 's8').length
  check('同一个片段再点开不重复请求（按 runId:segId 缓存）', again === 1, `请求了 ${again} 次`)
  await page.locator('[data-evidence-panel] button[aria-label="关闭证据"]').click()
  check('关闭按钮', await panel(page).count() === 0)
})

await section('none', '没有证据的地方：原因、缺输入 / 显示不出来、违规清单', async () => {
  await page.locator(`${wide} [data-seg="s26"]`).click()
  await page.waitForSelector('[data-evidence-panel] [data-ev-seal-state="done"]')
  let text = await panel(page).innerText()
  check('无法解析的引用说出原因', text.includes('缺少输入'), text.slice(0, 80))
  check('value 为 null：写「缺少输入」', await panel(page).locator('[data-ev-missing]').count() === 1
    && await panel(page).locator('[data-ev-unshowable]').count() === 0)
  check('没有证据的片段不说「这个数已封存」', text.includes('报告文档已封存'))
  await page.locator(`${wide} [data-seg="s33"]`).click()
  await page.waitForTimeout(200)
  text = await panel(page).innerText()
  check('value 不为 null、rendered 为「—」：写「有值但显示不出来」，和缺输入分开',
    (await panel(page).locator('[data-ev-unshowable]').innerText().catch(() => '')).includes('有值（29）')
    && await panel(page).locator('[data-ev-missing]').count() === 0, text.slice(0, 120))
  await page.locator(`${wide} [data-seg="s16"]`).click()
  await page.waitForTimeout(200)
  text = await panel(page).innerText()
  check('裸数字说清没写引用标记', text.includes('未使用引用标记') && text.includes('报告中直接写出了数字'), text.slice(0, 120))

  await page.locator('[data-evidence-panel] [data-ev-open-violations]').click()
  await page.waitForSelector('[data-ev-violations]')
  const items = page.locator('[data-ev-violation]')
  check('违规清单列全 6 条', await items.count() === 6, String(await items.count()))
  const hundred = items.filter({ hasText: '100' })
  check('列表序号里的 100 在清单里找得到，并说清为什么正文里没画线',
    await hundred.getAttribute('data-ev-locatable') === 'no' && (await hundred.innerText()).includes('列表序号'),
    await hundred.innerText().catch(() => ''))
  await items.filter({ hasText: '数字「3」' }).getByRole('button', { name: '定位' }).click()
  await page.waitForTimeout(250)
  check('清单里「定位」跳到正文那一处并打开它的证据', await active(page) === 's36'
    && (await panel(page).locator('h3').innerText()) === '3', await active(page))
  await page.keyboard.press('Escape')

  await page.locator(`${wide} [data-evidence-next]`).click()
  await page.waitForTimeout(250)
  const first = await active(page)
  const flash = await page.locator(`${wide} [data-seg="s16"]`).getAttribute('data-flash')
  await page.locator(`${wide} [data-evidence-next]`).click()
  await page.waitForTimeout(250)
  check('「定位下一处」依次跳到无证据的片段、描一下边、打开面板', first === 's16' && flash === 'focus'
    && await active(page) === 's26' && await panel(page).count() === 1, `${first} ${flash} ${await active(page)}`)
  await page.keyboard.press('Escape')
})

await section('narrow', '360px：画布右栏里栏内展开，不出横向滚动', async () => {
  const narrow = '#evidence-narrow'
  const overflow = () => page.evaluate((sel) => {
    const el = document.querySelector(sel)
    return { sw: el.scrollWidth, cw: el.clientWidth, w: el.getBoundingClientRect().width }
  }, narrow)
  const before = await overflow()
  await page.locator(`${narrow} [data-seg="s8"]`).click()
  await page.waitForSelector(`${narrow} [data-evidence-panel="inline"] [data-ev-substituted]`)
  const after = await overflow()
  check('窄栏宽 360px', Math.round(before.w) === 360, String(before.w))
  check('窄栏里没有横向滚动（面板打开前后）', before.sw <= before.cw && after.sw <= after.cw, JSON.stringify({ before, after }))
  check('窄栏里的面板在栏内、紧跟着片段所在的块', await page.evaluate((sel) => {
    const p = document.querySelector(`${sel} [data-evidence-panel]`)
    return p?.previousElementSibling?.getAttribute('data-block') === 'b1'
  }, narrow))
  check('栏内面板带「回到正文」', await page.locator(`${narrow} [data-evidence-panel] button`, { hasText: '回到正文' }).count() === 1)
  await page.locator(`${narrow} [data-evidence-panel] button`, { hasText: '回到正文' }).click()
  await page.waitForTimeout(150)
  check('回到正文：面板收起，焦点回片段', await page.locator(`${narrow} [data-evidence-panel]`).count() === 0 && await active(page) === 's8')

  const small = await open('/ui-harness.html?evidence=1', { w: 360, h: 780 })
  const scroll = () => small.page.evaluate(() => ({ sw: document.documentElement.scrollWidth, vw: innerWidth }))
  const a = await scroll()
  await small.page.locator('#evidence-wide [data-seg="s8"]').click()
  await small.page.waitForSelector('[data-evidence-panel] [data-ev-substituted]')
  const b = await scroll()
  check('360px 宽的屏幕：面板从底部抽出', await small.page.locator('[data-evidence-panel]').getAttribute('data-evidence-panel') === 'drawer')
  check('360px 宽的屏幕：整页没有横向滚动（面板打开前后）', a.sw <= a.vw && b.sw <= b.vw, JSON.stringify({ a, b }))
  const box = await small.page.locator('[data-evidence-panel]').boundingBox()
  check('抽屉不超出屏幕', !!box && box.x >= 0 && box.x + box.width <= 361, JSON.stringify(box))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await small.page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await small.page.waitForTimeout(250)
      await small.page.screenshot({ path: `${SHOTS}/evidence-360-drawer-${theme}.png` })
    }
  }
  check('没有运行时报错（360px）', small.errors.length === 0, small.errors.join(' | '))
  await small.ctx.close()
})

await section('motion', '减少动效：面板直接出现，没有动画', async () => {
  // 先证明这条检查抓得住：正常情况下面板是滑进来的
  // 全局的减少动效规则把过渡压到 0.01ms（主题色切换那种），那不算动画；动画和更长的过渡才算
  const running = () => {
    const p = document.querySelector('[data-evidence-panel]')
    if (!p) return -1
    return p.getAnimations({ subtree: true })
      .filter((a) => !(a instanceof CSSTransition && Number(a.effect?.getTiming().duration) <= 0.01)).length
  }
  await page.locator(`${wide} [data-seg="s8"]`).click()
  const moving = await page.evaluate(running)
  check('正常情况下面板有入场动画（对照）', moving > 0, String(moving))
  await page.keyboard.press('Escape')
  const r = await open('/ui-harness.html?evidence=1', { reduced: true })
  await r.page.locator('#evidence-wide [data-seg="s8"]').click()
  await r.page.waitForSelector('[data-evidence-panel] [data-ev-substituted]').catch(() => {})
  const still = await r.page.evaluate((fn) => ({
    n: new Function(`return (${fn})()`)(),
    name: getComputedStyle(document.querySelector('[data-evidence-panel]')).animationName,
  }), running.toString())
  check('减少动效时面板没有任何动画', still.n === 0 && still.name === 'none', JSON.stringify(still))
  await r.page.locator('#evidence-narrow [data-seg="s8"]').click()
  const inline = await r.page.evaluate(() => document.querySelector('#evidence-narrow [data-evidence-panel]')
    ?.getAnimations({ subtree: true })
    .filter((a) => !(a instanceof CSSTransition && Number(a.effect?.getTiming().duration) <= 0.01)).length ?? -1)
  check('减少动效时栏内面板也不动', inline === 0, String(inline))
  await r.ctx.close()
})

await section('legacy', '旧契约按位置标记（REQ-A-2）', async () => {
  const pos = await page.evaluate(() => {
    const box = document.querySelector('#legacy-marks')
    const marks = [...box.querySelectorAll('[data-mark]')]
    return marks.map((m) => ({ tone: m.dataset.mark, text: m.textContent, title: m.title,
      before: (m.previousSibling?.textContent ?? '').slice(-3) }))
  })
  const warn = pos.filter((m) => m.tone === 'warn')
  check('按位置：只标正文里回指不上的那个 15，日期 2026-09-15 里的不标', warn.length === 1 && warn[0].text === '15'
    && !warn[0].before.includes('-'), JSON.stringify(warn))
  const amb = pos.filter((m) => m.tone === 'ambiguous')
  check('出处不唯一写「出处不唯一：候选 a、b」', amb.length === 2 && amb.every((m) => m.title === '出处不唯一：候选 refund_cnt、reship_cnt'),
    JSON.stringify(amb))
  check('不再出现「来自口径卡指标「」」', !pos.some((m) => m.title.includes('指标「」')))
  check('回指上的照常标出来源', pos.some((m) => m.tone === 'ok' && m.text === '1,234' && m.title.includes('orders')))
  const loose = await page.locator('#legacy-loose [data-mark="warn"]').allInnerTexts()
  check('老运行没有位置：退回按字符串标（日期里的 15 也会被标，和以前一样）', loose.length === 2 && loose.every((t) => t === '15'),
    loose.join('|'))
})

await section('chat', '问数据页的答案：带 _evidence 的换成证据文档，没有的和以前一样', async () => {
  const chat = await open('/preview.html?syn=evidence')
  const p = chat.page
  await p.waitForSelector('[data-turn="evidence"] [data-evidence-doc]', { timeout: 6000 })
  const line = await p.locator('[data-turn="evidence"] [data-evidence-line]').innerText().catch(() => '')
  check('出具横幅多一行「N/N 数字有出处 · 无证据 M」和「定位下一处」', line.includes('7/12 数字有出处 · 无证据 5 · 另有 1 处引用无法解析') && line.includes('定位下一处'), line)
  const note = await p.locator('[data-turn="evidence"] [data-unmatched-note]').innerText().catch(() => '')
  check('横幅不再说「正文里已用虚线标出」（列表序号里的 100 画不了线），改指违规清单', !note.includes('虚线')
    && note.includes('违规清单'), note)
  await p.locator('[data-turn="evidence"] [data-issuance-banner] [data-evidence-list]').click()
  await p.waitForSelector('[data-evidence-panel] [data-ev-violations]', { timeout: 3000 }).catch(() => {})
  check('横幅上的「违规清单」打开报告的违规清单，100 在里面', (await p.locator('[data-evidence-panel] [data-ev-violation]')
    .filter({ hasText: '100' }).count()) === 1)
  await p.keyboard.press('Escape')
  await p.locator('[data-evidence-panel] button[aria-label="关闭证据"]').click().catch(() => {})
  check('有横幅时文档不再重复计数条', await p.locator('[data-turn="evidence"] [data-evidence-tally]').count() === 0)
  await p.locator('[data-turn="evidence"] [data-evidence-next]').click()
  await p.waitForSelector('[data-evidence-panel]')
  check('横幅的「定位下一处」跳到第一处无证据并打开面板', await active(p) === 's16')
  await p.keyboard.press('Escape')
  const legacy = await p.evaluate(() => {
    const turn = document.querySelector('[data-turn="evidence-legacy"]')
    const md = turn?.querySelector('div.space-y-2.leading-relaxed')
    const base = document.querySelector('#md-baseline > div')
    return { segs: turn?.querySelectorAll('[data-seg], [data-evidence-doc]').length ?? -1,
             same: !!md && !!base && md.outerHTML === base.outerHTML, md: !!md, base: !!base }
  })
  check('没有 _evidence 的回答不出现证据片段', legacy.segs === 0, String(legacy.segs))
  check('没有 _evidence 的回答渲染和普通 Markdown 一字不差', legacy.same, JSON.stringify(legacy))
  const row = await p.locator('[data-turn="evidence"]').innerText()
  check('执行过程里有「核对报告」那一行', row.includes('核对报告：7/12 数字有出处 · 无证据 5 · 另有 1 处引用无法解析'))
  const legacyNote = await p.locator('[data-turn="evidence-legacy"] [data-unmatched-note]').count()
  check('没有 _evidence 的旧回答不挂横幅也不挂证据说法', legacyNote === 0)
  check('没有运行时报错（问数据页）', chat.errors.length === 0, chat.errors.join(' | '))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await p.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await p.locator('[data-turn="evidence"] [data-seg="s8"]').click()
      await p.waitForSelector('[data-evidence-panel] [data-ev-substituted]')
      await p.waitForTimeout(350)
      await p.screenshot({ path: `${SHOTS}/evidence-chat-${theme}.png` })
      await p.keyboard.press('Escape')
    }
  }
  await chat.ctx.close()
})

await section('artifacts', '记录页的工件：口径卡指标集、报告文档有名字（REQ-A-1）', async () => {
  // 按页面自己加载的那一份取（热更新过的模块带 ?t=，另 import 一份也是同一套代码，这里只读纯函数）
  const got = await page.evaluate(async () => {
    const m = await import('/src/pages/runs/model.ts')
    return {
      metric: m.artifactKindLabel('metric_set'), report: m.artifactKindLabel('report_doc'),
      card: m.artifactDescription('metric_set', { caliber: '周报口径', version: 'v2' }),
      bare: m.artifactDescription('metric_set', null), doc: m.artifactDescription('report_doc', {}),
      evidence: m.isEvidence('metric_set') && m.isEvidence('report_doc'),
    }
  })
  check('工件种类：口径卡指标集、报告文档', got.metric === '口径卡指标集' && got.report === '报告文档', JSON.stringify(got))
  check('口径卡那件写成「口径卡「x」v2」', got.card === '口径卡「周报口径」v2', got.card)
  check('没有 meta 的老数据也有一句说明', got.bare === '口径卡的指标、算式和输入' && got.doc.includes('报告'))
  check('两种都算证据（列表里醒目）', got.evidence)
})

await section('long', '长报告：块级 content-visibility、折叠、跳进折叠区自动展开', async () => {
  const box = '#evidence-long'
  const info = await page.evaluate((sel) => {
    const blocks = [...document.querySelectorAll(`${sel} [data-block]`)]
    return { n: blocks.length, cv: blocks.map((b) => getComputedStyle(b).contentVisibility),
             fold: document.querySelector(sel).innerText.includes('以下内容已折叠') }
  }, box)
  check('超过 40 块的报告：每块 content-visibility: auto', info.cv.length > 0 && info.cv.every((v) => v === 'auto'), info.cv.slice(0, 3).join(','))
  check('超过 3000 字先折叠，说清「后面还有」', info.fold && info.n < 128, `${info.n} 块`)
  const tally = await page.locator(`${box} [data-evidence-tally]`).innerText()
  check('没有 stats、没有违规清单时从片段自己数（16 份：数字 4 处 + 非数字引用 1 处）',
    tally.includes('112/176 数字有出处 · 无证据 64 · 另有 16 处引用无法解析'), tally)
  await page.locator(`${box} [data-seg="s4-0"]`).focus()
  await page.keyboard.press('End')
  await page.waitForTimeout(250)
  const after = await page.evaluate((sel) => ({ n: document.querySelectorAll(`${sel} [data-block]`).length,
    active: document.activeElement?.getAttribute('data-seg') }), box)
  check('跳到折叠区里的片段：先展开再聚焦', after.n === 128 && after.active === 's49-15', JSON.stringify(after))
  const stops = await page.locator(`${box} [data-seg][tabindex="0"]`).count()
  check('长报告也只占一个 Tab 位', stops === 1, String(stops))
})

await section('integrity', '证据接口复核出问题：醒目写出来，不说「有出处」就完事', async () => {
  const bad = await open('/ui-harness.html?evidence=1')
  await bad.page.unroute((u) => new URL(u).pathname.startsWith('/api/'))
  await bad.page.route((u) => new URL(u).pathname.startsWith('/api/'), (r) => {
    const m = new URL(r.request().url()).pathname.match(/\/evidence\/segments\/([^/]+)$/)
    if (!m) return r.request().method() === 'GET' ? r.continue() : r.abort()
    const body = structuredClone(fx.segments[m[1]])
    body.chain[0] = { ...body.chain[0], hash_ok: false, sealed: false }
    return r.fulfill({ json: body })
  })
  await bad.page.locator('#evidence-wide [data-seg="s8"]').click()
  await bad.page.waitForSelector('[data-evidence-panel] [data-ev-integrity]', { timeout: 4000 }).catch(() => {})
  const said = await bad.page.locator('[data-evidence-panel] [data-ev-integrity]').innerText().catch(() => '')
  check('工件哈希校验失败、不在封存范围内：用失败色逐条写出', said.includes('哈希校验失败') && said.includes('不在封存范围内'), said)
  await bad.page.keyboard.press('Escape')
  await page.locator(`${wide} [data-seg="s8"]`).click()
  await page.waitForSelector('[data-evidence-panel] [data-ev-substituted]')
  check('全部复核通过时不多说一句', await page.locator('[data-evidence-panel] [data-ev-integrity]').count() === 0)
  await page.keyboard.press('Escape')
  await bad.ctx.close()
})

/**
 * 组件预览页，片段接口按 answer(segId, 夹具里那一份的拷贝) 回：返回对象就当 200，返回数字
 * 就当那个状态码。证据图按 graph(夹具拷贝) 回
 */
async function probe(answer, { graph = (g) => g } = {}) {
  const r = await open('/ui-harness.html?evidence=1')
  await r.page.unroute((u) => new URL(u).pathname.startsWith('/api/'))
  await r.page.route((u) => new URL(u).pathname.startsWith('/api/'), (route) => {
    const req = route.request()
    if (req.method() !== 'GET') return route.abort()
    const path = new URL(req.url()).pathname
    const m = path.match(/\/evidence\/segments\/([^/]+)$/)
    if (m) {
      const body = answer(m[1], structuredClone(fx.segments[m[1]] ?? {}))
      return typeof body === 'number'
        ? route.fulfill({ status: body, json: { detail: '伪造的错误', code: 'evidence_report_not_found' } })
        : route.fulfill({ json: body })
    }
    if (/\/evidence$/.test(path)) return route.fulfill({ json: graph(structuredClone(fx.graph)) })
    return route.continue()
  })
  return r
}
const sealOf = async (p) => ({
  state: await p.locator('[data-evidence-panel] [data-ev-seal-state]').getAttribute('data-ev-seal-state').catch(() => null),
  text: await p.locator('[data-evidence-panel]').innerText().catch(() => ''),
})
async function openSeg(p, id = 's8', wait = '[data-ev-seal-state]:not([data-ev-seal-state="idle"])') {
  await p.locator(`#evidence-wide [data-seg="${id}"]`).click()
  await p.waitForSelector(`[data-evidence-panel] ${wait}`, { timeout: 4000 }).catch(() => {})
  await p.waitForTimeout(100)
}

await section('trust', '只信封存链：证据接口答的是另一份报告时，不能画成绿的', async () => {
  // 正文是按成果上的 doc_artifact 取的（可改写），接口从封存的事件找文档。两边不是同一份：
  // 接口报的文档工件 id 不同、这个位置的字也不同（封存的那份写的是 9.9%）
  const other = await probe((id, body) => (id === 's8' ? {
    ...body, report: { ...body.report, doc_artifact: 'f'.repeat(64) },
    segment: { ...body.segment, text: '9.9%' },
    chain: [{ ...body.chain[0], value: 9.9, rendered: '9.9%' }, ...body.chain.slice(1)],
  } : body))
  await openSeg(other.page)
  let got = await sealOf(other.page)
  const integrity = await other.page.locator('[data-evidence-panel] [data-ev-integrity="doc"]').innerText().catch(() => '')
  check('封存文档不是正文这份：封存那一行不是绿的「核对一致」，是失败', got.state === 'failed' && !got.text.includes('核对一致'),
    `${got.state} ${got.text.replace(/\s+/g, ' ').slice(0, 120)}`)
  check('醒目写出「正文不是封存的那一份」，并说封存的那份这里写的是 9.9%', integrity.includes('不是封存范围内的那一份')
    && integrity.includes('9.9%'), integrity.replace(/\s+/g, ' '))
  check('另一份报告的算式和值不挂到正文上', await other.page.locator('[data-evidence-panel] [data-ev-substituted]').count() === 0
    && !(await other.page.locator('[data-evidence-panel] [data-ev-metric]').innerText()).includes('9.9%'))
  check('标题还是正文上的字', (await other.page.locator('[data-evidence-panel] h3').innerText()) === '8.7%')
  await other.ctx.close()

  // 只有文档工件 id 不同（字碰巧一样）也不行
  const idOnly = await probe((id, body) => ({ ...body, report: { ...body.report, doc_artifact: 'e'.repeat(64) } }))
  await openSeg(idOnly.page)
  got = await sealOf(idOnly.page)
  check('只是文档工件 id 不同：一样判失败', got.state === 'failed'
    && await idOnly.page.locator('[data-evidence-panel] [data-ev-integrity="doc"]').count() === 1, got.state)
  await idOnly.ctx.close()

  // 片段接口取不到，退回证据图：证据图里封存的报告没有正文这份 → 失败；有 → 只说文档封存了
  const noSeg = await probe(() => 500, { graph: (g) => ({ ...g, reports: g.reports.map((r) => ({ ...r, doc_artifact: 'd'.repeat(64) })) }) })
  await openSeg(noSeg.page)
  got = await sealOf(noSeg.page)
  check('证据图兜底：封存的报告里没有正文这份，判失败', got.state === 'failed'
    && await noSeg.page.locator('[data-evidence-panel] [data-ev-integrity="doc"]').count() === 1, got.state)
  await noSeg.ctx.close()
  const viaGraph = await probe(() => 500)
  await openSeg(viaGraph.page)
  got = await sealOf(viaGraph.page)
  check('证据图兜底、正文这份在封存的报告里：只说文档封存了，这个数的链没逐项核对', got.state === 'done'
    && got.text.includes('未能获取本段的证据链'), `${got.state} ${got.text.replace(/\s+/g, ' ').slice(-80)}`)
  await viaGraph.ctx.close()

  // 对照：接口答的就是正文这份时照常是绿的（上面那几条不是因为什么都判失败才过的）
  await page.locator(`${wide} [data-seg="s8"]`).click()
  await page.waitForSelector('[data-evidence-panel] [data-ev-seal-state="done"]', { timeout: 4000 }).catch(() => {})
  got = await sealOf(page)
  check('对照：同一份文档照常「已封存 · 核对一致」', got.state === 'done' && got.text.includes('已封存 · 核对一致')
    && await page.locator('[data-evidence-panel] [data-ev-integrity]').count() === 0, got.state)
  await page.keyboard.press('Escape')
})

await section('seal', '封存状态：被改过的封存画成失败，没封存不说「事后补进来」', async () => {
  // 后端 covered = 封存核对通过 && 每步在台账里：核对失败时 covered 必然 false、每步 sealed 也是 false
  const cases = [
    ['封存被改过（sealed, ok=false）', { sealed: true, ok: false, covered: false }, 'failed', '已封存 · 核对不一致', false],
    ['还没封存', { sealed: false, ok: null, covered: false }, 'idle', '尚未封存', false],
    ['封存完好、这件证据不在台账里', { sealed: true, ok: true, covered: false }, 'waiting', '不在封存范围内', true],
  ]
  for (const [name, seal, state, label, late] of cases) {
    const r = await probe((id, body) => ({ ...body, seal, chain: body.chain?.map((st) => ('sealed' in st ? { ...st, sealed: false } : st)) }))
    await openSeg(r.page, 's8', '[data-ev-substituted]')
    const got = await sealOf(r.page)
    check(`${name}：封存那一行是 ${state}「${label}」`, got.state === state && got.text.includes(label), `${got.state}`)
    check(`${name}：${late ? '单独报「不在封存范围内」' : '不说「事后补充」（封存那一行已经说清了）'}`,
      got.text.includes('事后补充') === late, got.text.replace(/\s+/g, ' ').slice(0, 120))
    await r.ctx.close()
  }
})

await section('fields', '成果字段取文档：别的运行写的、没记正文的都不画', async () => {
  const r = await open('/ui-harness.html?evidence=1')
  await r.page.route(/\/api\/artifacts\/fx-field-/, (route) => {
    const id = new URL(route.request().url()).pathname.split('/').pop()
    const doc = id === 'fx-field-other-run' ? { ...fx.doc, run_id: 'run-someone-else' }
      : id === 'fx-field-no-markdown' ? (({ markdown: _m, ...d }) => d)(fx.doc) : fx.doc
    return route.fulfill({ json: { id, content: doc } })
  })
  await r.page.reload({ waitUntil: 'networkidle' })
  await r.page.waitForSelector('#fx-field-ok [data-evidence-doc]', { timeout: 5000 }).catch(() => {})
  check('对照：正文这份照常逐段可点', await r.page.locator('#fx-field-ok [data-evidence-doc] [data-seg]').count() > 0)
  const other = await r.page.locator('#fx-field-other-run [data-evidence-fallback]').getAttribute('data-evidence-fallback').catch(() => null)
  check('文档的 run_id 和成果的运行不同：退回普通文本并说明', other === 'other-run'
    && (await r.page.locator('#fx-field-other-run').innerText()).includes('不属于本次运行')
    && await r.page.locator('#fx-field-other-run [data-seg]').count() === 0, String(other))
  const bare = await r.page.locator('#fx-field-no-markdown [data-evidence-fallback]').getAttribute('data-evidence-fallback').catch(() => null)
  check('文档没记正文（缺 markdown）：当对不上处理，不画', bare === 'mismatch'
    && await r.page.locator('#fx-field-no-markdown [data-seg]').count() === 0, String(bare))
  await r.ctx.close()
})

// ---------------------------------------------------------------------------
// 第二期：查询步骤、输入来源、整表、口径卡来源（夹具 evidence-query.json）
// ---------------------------------------------------------------------------
const Q = '#evidence-query'
const QN = '#evidence-query-narrow'
/** 夹具里的片段 id：按文字认，免得重新生成夹具后编号挪了这里全错 */
const qseg = (text, nth = 0) => Object.values(fxq.segments).filter((d) => d.segment.text === text)[nth]?.segment.id
const Q3 = fxq.snapshots.Q3.artifact
const qsteps = (text) => fxq.segments[qseg(text)].chain.filter((st) => st.step === 'query')
/** 第二期的组件预览：片段接口按 answer(segId, 夹具拷贝) 回（数字当状态码），其余照 routed */
async function probeQ(answer, opts = {}) {
  const r = await open('/ui-harness.html?evidence=1', opts)
  await r.page.route((u) => new URL(u).pathname.includes(`/runs/${fxq.run_id}/evidence/segments/`), (route) => {
    const id = decodeURIComponent(new URL(route.request().url()).pathname.split('/').pop())
    const body = answer(id, structuredClone(fxq.segments[id] ?? {}))
    return typeof body === 'number'
      ? route.fulfill({ status: body, json: { detail: '伪造的错误', code: 'evidence_report_not_found' } })
      : route.fulfill({ json: body })
  })
  return r
}
async function openQ(p, text, { box = Q, nth = 0, wait = '[data-ev-query]' } = {}) {
  await p.locator(`${box} [data-seg="${qseg(text, nth)}"]`).click()
  await p.waitForSelector(`[data-evidence-panel] ${wait}`, { timeout: 4000 }).catch(() => {})
  await p.waitForTimeout(120)
}
/** 令牌算出来的颜色：拿一个探针元素量，不在脚本里写死色值 */
const tokenColor = (p, prop, value) => p.evaluate(([prop, value]) => {
  const el = document.createElement('div')
  el.style[prop] = value
  document.body.appendChild(el)
  const got = getComputedStyle(el)[prop]
  el.remove()
  return got
}, [prop, value])

await section('query', '查询步骤：SQL 带复制、被引用的行和格高亮、窗口说明、完整快照、遮罩', async () => {
  await openQ(page, '1,288')
  const part = panel(page).locator(`[data-ev-query="${Q3}"]`)
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(250)
      await page.screenshot({ path: `${SHOTS}/evidence-query-step-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
  check('点开单元格：面板里有这次查询的步骤', await part.count() === 1, String(await part.count()))
  check('标题写查询编号和工具', (await part.innerText()).includes('Q3') && (await part.innerText()).includes('db_query__shop'))
  check('SQL 原样摆出来', (await part.locator('[data-ev-sql]').innerText()).startsWith('SELECT order_id, region, amount, phone'))
  check('SQL 带复制按钮', await part.locator('button', { hasText: '复制 SQL' }).count() === 1)
  const rows = part.locator('tbody tr[data-highlight="row"]')
  check('被引用的行加 data-highlight="row"，只有这一行', await rows.count() === 1
    && (await rows.first().innerText()).includes('10006'), await rows.first().innerText().catch(() => ''))
  const cell = part.locator('td[data-highlight="cell"]')
  check('被引用的格加 data-highlight="cell"，在 amount 列', await cell.count() === 1 && (await cell.innerText()) === '1288'
    && await cell.evaluate((td) => td.closest('table').querySelectorAll('th')[td.cellIndex]?.textContent === 'amount'),
    await cell.innerText().catch(() => ''))
  const [rowBg, cellLine] = await Promise.all([
    rows.first().evaluate((el) => getComputedStyle(el).backgroundColor),
    cell.evaluate((el) => getComputedStyle(el).outlineColor),
  ])
  check('高亮只用令牌：行底是 --st-done-soft', rowBg === await tokenColor(page, 'backgroundColor', 'var(--st-done-soft)'), rowBg)
  check('高亮只用令牌：格的描边是 --st-done', cellLine === await tokenColor(page, 'outlineColor', 'var(--st-done)'), cellLine)
  check('只显示窗口：被引用的第 6 行加前后各 2 行，共 5 行', await part.locator('tbody tr').count() === 5)
  const win = await part.locator('[data-ev-window]').innerText().catch(() => '')
  check('窗口外的行写明「仅显示被引用的行及前后各 2 行」和位置', win.includes('仅显示被引用的行及前后各 2 行')
    && win.includes('第 4–8 行') && win.includes('共 12 行'), win)
  const masked = await part.locator('td[data-masked]').allInnerTexts()
  check('被遮罩的列（phone）每格写「已遮罩」', masked.length === 5 && masked.every((t) => t === '已遮罩'), masked.join('|'))
  const note = await panel(page).locator('[data-ev-mask-note]').innerText().catch(() => '')
  check('面板底部说明遮罩：列名 + 不是安全边界', note.includes('phone') && note.includes('遮罩只减少暴露，不是安全边界'), note)
  check('遮罩说明在面板最后（封存那一行之后）', await panel(page).evaluate((p) => {
    const note = p.querySelector('[data-ev-mask-note]')
    const seal = p.querySelector('[data-ev-seal]')
    return !!note && !!seal && !!(seal.compareDocumentPosition(note) & Node.DOCUMENT_POSITION_FOLLOWING)
  }))
  check('查询快照复验通过时不多说一句', await panel(page).locator('[data-ev-integrity]').count() === 0)
  // Excel 导入期 4 之前的报告：查询步骤不带推断来源的提示，前端一个请求都不多发、三节都不画（P4-SPEC 4.4 第 14 条）。
  // 断言请求记账，不只看 DOM：老服务端那种 404 也会让三节不画
  check('期 4 之前的报告（查询步骤没有提示）：不请求推断的来源，面板里没有数据版本、推断的来源',
    !harness.hits.some((h) => h.startsWith('prov:'))
    && await panel(page).locator('[data-ev-provenance], [data-ev-prov-version], [data-ev-prov-checks]').count() === 0,
    harness.hits.filter((h) => h.startsWith('prov:')).join(','))
  const aria = await page.locator(`${Q} [data-seg="${qseg('1,288')}"]`).getAttribute('aria-label')
  check('单元格的 aria-label 写明查询、第几行、哪一列', aria === '1,288，有出处：查询 Q3 · 第 6 行 · amount', aria)

  const before = harness.hits.filter((h) => h === 'artifact:Q3').length
  await part.locator('[data-ev-snapshot]').click()
  await page.waitForSelector('[role="dialog"] [data-artifact] tbody tr', { timeout: 4000 }).catch(() => {})
  const full = await page.locator('[role="dialog"] [data-artifact] tbody tr').count()
  check('「打开完整快照」复用工件查看，12 行全在', full === 12 && harness.hits.filter((h) => h === 'artifact:Q3').length === before + 1, `${full} 行`)
  const fullText = await page.locator('[role="dialog"] [data-artifact]').innerText().catch(() => '')
  check('……完整快照里遮罩列同样写「已遮罩」，工件接口给的原值不画出来', !fullText.includes('RAW-PHONE')
    && await page.locator('[role="dialog"] [data-artifact] td[data-masked]').count() === 12,
    fullText.includes('RAW-PHONE') ? '原值漏出来了' : String(await page.locator('[role="dialog"] [data-artifact] td[data-masked]').count()))
  await page.keyboard.press('Escape')
  await page.waitForTimeout(200)
  check('在完整快照里按 Esc 只关快照，证据面板还开着', await page.locator('[role="dialog"]').count() === 0 && await panel(page).count() === 1)

  await openQ(page, '华东')
  const v = panel(page).locator('[data-ev-query]')
  check('值片段（华东）也给查询步骤，高亮第 1 行 region 格', (await v.locator('td[data-highlight="cell"]').innerText().catch(() => '')) === '华东'
    && await v.locator('tbody tr').first().getAttribute('data-highlight') === 'row')
  check('第 1 行：窗口是第 1–3 行（前面没有行可补），照实写', (await v.locator('[data-ev-window]').innerText().catch(() => ''))
    .includes('第 1–3 行，共 4 行'))
  check('没有遮罩列时不写遮罩说明', await panel(page).locator('[data-ev-mask-note]').count() === 0)
  await page.keyboard.press('Escape')
  await openQ(page, '45,678.5元')
  const one = panel(page).locator('[data-ev-query]')
  check('整份快照都在窗口里（1 行）：不写窗口说明，完整快照照样能开', await one.count() === 1
    && await one.locator('[data-ev-window]').count() === 0 && await one.locator('[data-ev-snapshot]').count() === 1)
  await page.keyboard.press('Escape')

  // 被引用的两格隔得远（第 6、12 行）：窗口不连续，行号那一列看得出跳过了哪几行，只标那两格
  await openQ(page, '672.5元')
  const gap = panel(page).locator(`[data-ev-query="${Q3}"]`)
  const nums = await gap.locator('tbody tr td:first-child').allInnerTexts()
  check('窗口不连续：最前面一列是快照里的行号（4–8、10–12）', nums.join(',') === '4,5,6,7,8,10,11,12', nums.join(','))
  check('……窗口说明写成「第 4–8、10–12 行，共 12 行」', (await gap.locator('[data-ev-window]').innerText().catch(() => ''))
    .includes('第 4–8、10–12 行，共 12 行'))
  const hit2 = await gap.locator('td[data-highlight="cell"]').allInnerTexts()
  check('……两格都高亮（1288、615.5）', hit2.join(',') === '1288,615.5', hit2.join(','))
  await page.keyboard.press('Escape')
  const exact = await probeQ((id, body) => (id === qseg('1,288') ? { ...body, chain: body.chain.map((st) => (st.step === 'query'
    ? { ...st, highlight: { rows: [5, 6], cols: ['amount', 'region'], cells: [[5, 'amount'], [6, 'region']] } } : st)) } : body))
  await openQ(exact.page, '1,288')
  const exactCells = await exact.page.locator('[data-evidence-panel] td[data-highlight="cell"]').allInnerTexts()
  check('highlight 给了 cells：只标那几格，不按行 × 列全标', exactCells.join(',') === '1288,华北', exactCells.join(','))
  await exact.ctx.close()
  const unsealed = await probeQ((id, body) => (id === qseg('1,288') ? { ...body, chain: body.chain.map((st) => (st.step === 'query'
    ? { step: 'query', alias: st.alias, artifact: st.artifact, tool: st.tool, columns: [], rows: [], hash_ok: null, sealed: false,
        highlight: st.highlight, note: '这份查询快照不属于封存范围内的任何事件（封存前没有任何查询返回过它），不能作为证据展示' } : st)) } : body))
  await openQ(unsealed.page, '1,288')
  const un = await unsealed.page.locator('[data-evidence-panel] [data-ev-query]').innerText().catch(() => '')
  check('查询快照不在封存范围里：一行都不画，照接口原话说，并用失败色写「不在封存范围内」', un.includes('不能作为证据展示')
    && await unsealed.page.locator('[data-evidence-panel] [data-ev-query] [data-ev-integrity~="sealed"]').count() === 1
    && await unsealed.page.locator('[data-evidence-panel] [data-ev-query] tbody tr').count() === 0,
    un.replace(/\s+/g, ' ').slice(0, 200))
  // 后端这时照样带着工件 id：按钮一点就绕过证据接口把整份快照打开，还挂着「哈希已校验」
  check('……不给「打开完整快照」（接口不给行，面板也不能开整份快照）',
    await unsealed.page.locator('[data-evidence-panel] [data-ev-snapshot]').count() === 0)
  await unsealed.ctx.close()

  // 接口把遮罩列的原值带回来了（老接口、改错了的后端）：列在遮罩里就一律不画原值
  const leak = await probeQ((id, body) => (id === qseg('1,288') ? { ...body, chain: body.chain.map((st) => (st.step === 'query'
    ? { ...st, rows: st.rows.map((r) => r.map((v, j) => (st.columns[j] === 'phone' ? `RAW-PHONE-${j}` : v))) } : st)) } : body))
  await openQ(leak.page, '1,288')
  const leaked = await leak.page.locator('[data-evidence-panel]').innerText().catch(() => '')
  check('遮罩列带回了原值也不画：表里一律写「已遮罩」', !leaked.includes('RAW-PHONE')
    && await leak.page.locator('[data-evidence-panel] td[data-masked]').count() === 5, leaked.includes('RAW-PHONE') ? '原值漏出来了' : '')
  await leak.ctx.close()

  const bad = await probeQ((id, body) => (id === qseg('1,288')
    ? { ...body, chain: body.chain.map((st) => (st.step === 'query' ? { ...st, hash_ok: false } : st)) } : body))
  await openQ(bad.page, '1,288')
  const said = await bad.page.locator('[data-evidence-panel] [data-ev-integrity]').innerText().catch(() => '')
  const color = await bad.page.locator('[data-evidence-panel] [data-ev-integrity]').evaluate((el) => getComputedStyle(el).color).catch(() => '')
  check('查询快照哈希校验失败：用失败色写出来', said.includes('查询快照') && said.includes('哈希校验失败')
    && color === await tokenColor(bad.page, 'color', 'var(--st-failed)'), said)
  check('……哈希校验失败也不给「打开完整快照」', await bad.page.locator('[data-evidence-panel] [data-ev-snapshot]').count() === 0)
  await bad.ctx.close()
})

await section('sources', '输入来源：与快照一致、模型报 X 快照是 Y、没查到记为空、代码节点、点标签跳到查询', async () => {
  await openQ(page, '37.0元', { wait: '[data-ev-sources]' })
  const src = (path) => panel(page).locator(`[data-ev-source="${path}"]`)
  const gmv = await src('vars.kpi.gmv').innerText().catch(() => '')
  check('agent 字段 verified：写「与快照一致」', gmv.includes('与快照一致')
    && await src('vars.kpi.gmv').getAttribute('data-ev-source-status') === 'verified', gmv)
  const ord = await src('vars.kpi.order_cnt').innerText().catch(() => '')
  check('mismatch：写「模型给出 1,240，快照值为 1,234，已采用快照值」', ord.includes('模型给出 1,240，快照值为 1,234，已采用快照值'), ord)
  check('写明从哪个节点的哪个字段、哪一格来', gmv.includes('fetch') && gmv.includes('Q1') && gmv.includes('第 1 行'), gmv)
  await page.keyboard.press('Escape')

  await openQ(page, '—', { wait: '[data-ev-sources]' })
  const refund = await src('vars.kpi.refund_cnt').innerText().catch(() => '')
  check('from 为 null（missing）：写「未查询到，记为空值（未按 0 处理）」', refund.includes('未查询到，记为空值（未按 0 处理）'), refund)
  check('缺输入的指标照样给出输入来源，说得清缺的是哪一个', await panel(page).locator('[data-ev-missing]').count() === 1
    && await panel(page).locator('[data-ev-sources] [data-ev-source]').count() === 2)
  await page.keyboard.press('Escape')
  await openQ(page, '—', { nth: 1, wait: '[data-ev-sources]' })
  const nu = await src('vars.kpi.new_users').innerText().catch(() => '')
  check('unresolved：同样写「未查询到，记为空值（未按 0 处理）」，并带原因', nu.includes('未查询到，记为空值（未按 0 处理）')
    && nu.includes('没有列「new_users」'), nu)
  await page.keyboard.press('Escape')

  await openQ(page, '44,000.0元', { wait: '[data-ev-sources]' })
  const compute = src('nodes.adjust.total')
  const ct = await compute.innerText().catch(() => '')
  check('来自 code 节点（计算）：写警示语，说要进口径卡', ct.includes('代码节点') && ct.includes('口径卡')
    && await compute.getAttribute('data-ev-source-role') === 'compute', ct)
  const computeColor = await compute.evaluate((el) => getComputedStyle(el).borderColor).catch(() => '')
  await page.keyboard.press('Escape')
  await openQ(page, '7.12', { wait: '[data-ev-sources]' })
  const source = src('nodes.rate.value')
  const st = await source.innerText().catch(() => '')
  check('来自 code 节点（取数）：写明标为取数、无法与快照核对', st.includes('取数') && st.includes('无法与查询快照核对')
    && await source.getAttribute('data-ev-source-role') === 'source', st)
  const sourceColor = await source.evaluate((el) => getComputedStyle(el).borderColor).catch(() => '')
  check('计算角色比取数角色醒目：失败色描边', computeColor === await tokenColor(page, 'borderColor', 'var(--st-failed)')
    && sourceColor !== computeColor, `${computeColor} / ${sourceColor}`)
  await page.keyboard.press('Escape')

  await openQ(page, '8.7%', { wait: '[data-ev-sources]' })
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(250)
      await page.screenshot({ path: `${SHOTS}/evidence-sources-${theme}.png` })
      await panel(page).evaluate((p) => { p.querySelector('.overflow-y-auto').scrollTop = 99999 })
      await page.waitForTimeout(100)
      await page.screenshot({ path: `${SHOTS}/evidence-sources-queries-${theme}.png` })
      await panel(page).evaluate((p) => { p.querySelector('.overflow-y-auto').scrollTop = 0 })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
  const prev = qsteps('8.7%').find((q) => q.alias === 'Q4')
  const chip = panel(page).locator(`button[data-ev-input="cell(nodes.pull, 0, 'gmv')"]`)
  check('代入式里的输入标签是按钮', await chip.count() === 1)
  await chip.click()
  await page.waitForTimeout(200)
  const landed = await page.evaluate((a) => {
    const part = document.querySelector(`[data-evidence-panel] [data-ev-query="${a}"]`)
    const css = part ? getComputedStyle(part) : null
    return { inside: !!part?.contains(document.activeElement), flash: part?.hasAttribute('data-flash') ?? false,
             style: css?.outlineStyle, width: css?.outlineWidth, color: css?.outlineColor }
  }, prev?.artifact)
  // 只有属性没有样式等于没描：目标步骤已经在视野里时，点完什么都看不出来变了
  check('点输入标签：焦点跳到对应的查询步骤（Q4），并描一下（看得见的强调色描边）', landed.inside && landed.flash
    && landed.style !== 'none' && parseFloat(landed.width) >= 1 && landed.color === await tokenColor(page, 'outlineColor', 'var(--accent)'),
    JSON.stringify(landed))
  // 键盘：焦点落在查询步骤的标题上，得有看得见的焦点框（WCAG 2.4.7）
  await page.waitForTimeout(1700)
  await page.keyboard.press('Shift')
  await chip.focus()
  await page.keyboard.press('Enter')
  await page.waitForTimeout(150)
  const ring = await page.evaluate(() => {
    const el = document.activeElement
    const css = el ? getComputedStyle(el) : null
    return { head: el?.hasAttribute('data-ev-query-head') ?? false, style: css?.outlineStyle, width: css?.outlineWidth }
  })
  check('……键盘按回车跳过去：焦点落在查询步骤标题上，焦点框看得见', ring.head && ring.style !== 'none' && parseFloat(ring.width) >= 1,
    JSON.stringify(ring))
  const go = panel(page).locator('[data-ev-source="vars.kpi.gmv"] button[data-ev-goto]')
  check('输入来源那一行也能跳到查询', await go.count() === 1)
  await go.click()
  await page.waitForTimeout(200)
  check('……跳到 Q1 那一步', await page.evaluate((a) => !!document.querySelector(`[data-evidence-panel] [data-ev-query="${a}"]`)
    ?.contains(document.activeElement), qsteps('8.7%').find((q) => q.alias === 'Q1')?.artifact))
  await page.keyboard.press('Escape')
  await page.waitForTimeout(150)
  check('Esc 关掉面板，焦点回到原片段', await panel(page).count() === 0 && await active(page) === qseg('8.7%'), await active(page))
})

await section('table', '整表：每格是可点的片段，方向键按行列走，值和数同一套线型', async () => {
  const cells = page.locator(`${Q} table td [data-seg]`)
  check('整表每个数据格都是可点的片段（3 行 × 3 列）', await cells.count() === 9, String(await cells.count()))
  check('表头不是片段', await page.locator(`${Q} table th [data-seg]`).count() === 0)
  const [val, num] = await Promise.all([qseg('华东', 1), qseg('18,230.5', 1)].map((id) => page.locator(`${Q} [data-seg="${id}"]`)
    .evaluate((el) => { const cs = getComputedStyle(el); return `${cs.textDecorationLine}/${cs.textDecorationStyle}/${cs.textDecorationColor}` })))
  check('值片段（华东）和数字片段（18,230.5）同一套线型', val === num && val.includes('solid'), `${val} vs ${num}`)
  await page.locator(`${Q} [data-seg="${qseg('18,230.5', 1)}"]`).focus()
  const walk = []
  for (const key of ['ArrowDown', 'ArrowDown', 'ArrowUp', 'ArrowRight', 'ArrowDown', 'ArrowDown']) {
    await page.keyboard.press(key)
    walk.push(await active(page))
  }
  const want = [qseg('12,004', 1), qseg('9,876'), qseg('12,004', 1), qseg('311'), qseg('298'), qseg('1,288')]
  check('表格里 ↓/↑ 按列走到下一行 / 上一行，→ 走到右边一格，出了最后一行接着往下', walk.join(',') === want.join(','),
    `${walk.join(',')} ≠ ${want.join(',')}`)
  await page.locator(`${Q} [data-seg="${qseg('华东', 1)}"]`).focus()
  await page.keyboard.press('ArrowUp')
  check('第一行按 ↑ 回到表格前面那一句', await active(page) === qseg('华东', 0), await active(page))
  await page.locator(`${Q} [data-seg="${qseg('311')}"]`).focus()
  await page.keyboard.press('Enter')
  await page.waitForSelector('[data-evidence-panel] [data-ev-query]', { timeout: 4000 }).catch(() => {})
  const hl = await panel(page).locator('td[data-highlight="cell"]').innerText().catch(() => '')
  check('回车打开这一格的查询步骤，高亮 311 这一格', hl === '311', hl)
  await page.keyboard.press('Escape')
  const stops = await page.locator(`${Q} [data-seg][tabindex="0"]`).count()
  check('整份报告（含表格）仍只占一个 Tab 位', stops === 1, String(stops))
})

await section('caliber', '口径卡来源和升版处置', async () => {
  await openQ(page, '45,678.5元', { wait: '[data-ev-caliber-from]' })
  const from = await panel(page).locator('[data-ev-caliber-from]').innerText().catch(() => '')
  check('指标步骤写「口径卡「周报口径」v3，来自「销售周报」v5」', from.includes('口径卡「周报口径」v3，来自「销售周报」v5'), from)
  const up = await panel(page).locator('[data-ev-caliber-upgrade]').innerText().catch(() => '')
  check('有升版处置时写「上游已有 v6，按『…』处置」', up.includes('上游已有 v6') && up.includes('新旧口径并列展示') && up.includes('处置'), up)
  await page.keyboard.press('Escape')
  // 方案第 5 节的写法（caliber_from）也认；没给工作流名、没给 policy_label 时不出 undefined
  const bare = await probeQ((id, body) => ({ ...body, chain: (body.chain ?? []).map((st) => (st.step === 'metric'
    ? { ...st, source: undefined, caliber_from: { workflow_id: 'wf-demo-weekly', workflow_version: 5 },
        caliber_upgrade: { policy: 'recompute', latest: 6 } } : st)) }))
  await openQ(bare.page, '45,678.5元', { wait: '[data-ev-caliber-from]' })
  const t = await bare.page.locator('[data-evidence-panel] [data-ev-metric]').innerText()
  check('没给工作流名：退回工作流 id，不出 undefined', t.includes('来自') && t.includes('wf-demo') && !/undefined|null/.test(t), t.replace(/\s+/g, ' ').slice(0, 160))
  check('没给 policy_label：按策略代码说人话', t.includes('用新口径回算历史'), t.replace(/\s+/g, ' ').slice(0, 200))
  await bare.ctx.close()
  const none = await probeQ((id, body) => ({ ...body, chain: (body.chain ?? []).map((st) => (st.step === 'metric'
    ? { ...st, source: null, caliber_from: undefined, caliber_upgrade: null } : st)) }))
  await openQ(none.page, '45,678.5元', { wait: '[data-ev-expression]' })
  check('本地定义的口径卡、没有升版：两行都不出', await none.page.locator('[data-evidence-panel] [data-ev-caliber-from], [data-evidence-panel] [data-ev-caliber-upgrade]').count() === 0)
  await none.ctx.close()
})

await section('incomplete', '截断的查询结果整组算出来的指标：标「结果不完整」并写明原因，完整的指标不标', async () => {
  await openQ(page, '45,678.5元', { wait: '[data-ev-expression]' })
  check('完整的指标不出不完整标识', await panel(page).locator('[data-ev-incomplete], [data-ev-incomplete-reason]').count() === 0)
  check('……输入也不标「已截断」', !(await panel(page).locator('[data-ev-input]').allInnerTexts()).some((t) => t.includes('已截断')))
  await page.keyboard.press('Escape')
  // 接口的形状：backend/app/api/evidence.py 的指标步骤带 incomplete / incomplete_reason，输入带 Agent 字段证据里的 truncated
  const REASON = '基于被截断的查询结果计算（只取回了前 1000 行），结果不完整'
  const cut = await probeQ((id, body) => ({ ...body, chain: (body.chain ?? []).map((st) => (st.step === 'metric'
    ? { ...st, incomplete: true, incomplete_reason: REASON } : st.step === 'input' ? { ...st, truncated: true } : st)) }))
  await openQ(cut.page, '45,678.5元', { wait: '[data-ev-incomplete]' })
  const p = cut.page.locator('[data-evidence-panel]')
  const chip = p.locator('[data-ev-incomplete]')
  check('指标名旁有「结果不完整」标识，悬停看原因', (await chip.innerText().catch(() => '')) === '结果不完整'
    && await chip.getAttribute('title') === REASON, await chip.innerText().catch(() => ''))
  const why = await p.locator('[data-ev-incomplete-reason]').innerText().catch(() => '')
  check('原因写在指标下面', why === REASON, why)
  const warn = await tokenColor(cut.page, 'color', 'var(--st-waiting)')
  check('用提醒色（不是失败色：值是真算出来的，只是不全）', await chip.evaluate((el) => getComputedStyle(el).color) === warn)
  const inputs = await p.locator('[data-ev-input]').allInnerTexts()
  check('被截断切开的 Agent 字段输入标「已截断」', inputs.some((t) => t.includes('（已截断）')), inputs.join('|'))
  const source = await p.locator('[data-ev-sources]').innerText().catch(() => '')
  check('……输入来源写明与快照一致、但只含取回的部分行', source.includes('与快照一致；查询结果已截断，该字段只含取回的部分行'),
    source.replace(/\s+/g, ' ').slice(0, 160))
  await cut.ctx.close()
})

await section('sqlcheck', 'SQL 检查（数据目录阶段 4B）：查询步骤列出级别、规则名、说明、表和列、SQL 片段；指标未通过时标出来并写明原因', async () => {
  await openQ(page, '1,288')
  check('没查出问题的查询步骤不出 SQL 检查', await panel(page).locator('[data-ev-sql-checks]').count() === 0)
  check('没有冻结目录的查询步骤不写数据目录版本', await panel(page).locator('[data-ev-catalog]').count() === 0)
  await page.keyboard.press('Escape')
  await openQ(page, '45,678.5元', { wait: '[data-ev-expression]' })
  check('通过的指标不出「SQL 检查未通过」', await panel(page).locator('[data-ev-sql-check-failed], [data-ev-sql-check-reason]').count() === 0)
  await page.keyboard.press('Escape')
  // 接口的形状：backend/app/api/evidence.py 的查询步骤带 checks（去掉了给模型的那句），指标步骤带 sql_check_failed / sql_check_reason
  const CHECKS = [
    { code: 'join_unconfirmed', level: 'info', message: '「订单」与「地区」按推断的关系关联（o.region = r.code），这条关系尚未确认。', table: 'orders' },
    { code: 'fanout_sum', level: 'error', table: 'orders', column: 'amount', sql_excerpt: 'SUM(o.amount)',
      message: '「订单」关联「订单明细」是一对多，对「订单」的「订单金额」求和会重复计算。请先按订单汇总明细再关联。' },
    { code: 'missing_valid_filter', level: 'warning', message: '「订单」定义了有效记录条件「status <> 9」，查询中没有按它筛选。', table: 'orders' },
  ]
  const REASON = '所依据的查询未通过 SQL 检查（「订单」关联「订单明细」是一对多，对「订单」的「订单金额」求和会重复计算），结果不可靠'
  // 查询步骤带着这次查询对照的数据目录版本（_catalog_of：表结构快照里冻结的那份）
  const CATALOG = [{ table: 'orders', label: '订单', version: 3 }, { table: 'order_items', version: 1 }]
  const bad = await probeQ((id, body) => ({ ...body, chain: (body.chain ?? []).map((st) => (st.step === 'query'
    ? { ...st, checks: CHECKS, schema_artifact: 'c'.repeat(64), catalog: CATALOG }
    : st.step === 'metric' ? { ...st, sql_check_failed: true, sql_check_reason: REASON } : st)) }))
  await openQ(bad.page, '1,288', { wait: '[data-ev-sql-checks]' })
  const p = bad.page.locator('[data-evidence-panel]')
  const cat = await p.locator('[data-ev-catalog]').innerText().catch(() => '')
  check('查询步骤写明对照的数据目录版本：有中文名写中文名，没有写表名', cat === '数据目录：订单 第 3 版、order_items 第 1 版', cat)
  const items = p.locator('[data-ev-sql-checks] [data-sql-check]')
  const levels = await items.evaluateAll((els) => els.map((e) => e.getAttribute('data-level')))
  check('三条都列在 SQL 下面，错误在前、提示在后', levels.join(',') === 'error,warning,info', levels.join(','))
  const first = await items.first().innerText().catch(() => '')
  check('一条里有级别、中文规则名、说明、涉及的表和列、SQL 片段', first.includes('错误') && first.includes('一对多关联后重复计算')
    && first.includes('会重复计算') && first.includes('orders.amount') && first.includes('SUM(o.amount)'), first.replace(/\s+/g, ' '))
  check('级别四通道：图标 + 文字，三档文字各不相同', (await items.locator('[data-sql-level]').allInnerTexts()).map((t) => t.trim()).join(',')
    === '错误,提醒,提示' && await items.locator('[data-sql-level] svg').count() === 3)
  const tones = await items.locator('[data-sql-level]').evaluateAll((els) => els.map((e) => getComputedStyle(e).color))
  const want = [await tokenColor(bad.page, 'color', 'var(--st-failed)'), await tokenColor(bad.page, 'color', 'var(--st-waiting)'),
    await tokenColor(bad.page, 'color', 'var(--accent)')]
  check('级别的颜色走令牌：错误 --st-failed、提醒 --st-waiting、提示 --accent', tones.join('|') === want.join('|'), tones.join('|'))
  check('悬停写明依据的确证程度', (await items.first().locator('[data-sql-level]').getAttribute('title')) === '依据已核实，结果很可能有误，需要修改')
  check('不露规则编号', !/fanout_sum|missing_valid_filter|join_unconfirmed/.test(await p.innerText()))
  await bad.page.keyboard.press('Escape')
  await openQ(bad.page, '45,678.5元', { wait: '[data-ev-sql-check-failed]' })
  const chip = p.locator('[data-ev-sql-check-failed]')
  check('指标名旁有「SQL 检查未通过」标识，悬停看原因', (await chip.innerText().catch(() => '')) === 'SQL 检查未通过'
    && await chip.getAttribute('title') === REASON, await chip.innerText().catch(() => ''))
  check('原因写在指标下面', (await p.locator('[data-ev-sql-check-reason]').innerText().catch(() => '')) === REASON)
  check('和「结果不完整」同一种样式（提醒色的标识）', await chip.evaluate((el) => getComputedStyle(el).color)
    === await tokenColor(bad.page, 'color', 'var(--st-waiting)') && (await chip.getAttribute('class'))?.includes('chip'))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await bad.page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await bad.page.waitForTimeout(200)
      await bad.page.screenshot({ path: `${SHOTS}/evidence-sqlcheck-metric-${theme}.png` })
    }
  }
  check('没有运行时报错', bad.errors.length === 0, bad.errors[0] ?? '')
  await bad.ctx.close()
})

await section('q-narrow', '查询步骤在 360px：栏内展开、底部抽屉都不横向滚动；减少动效', async () => {
  const overflow = () => page.evaluate((sel) => {
    const el = document.querySelector(sel)
    return { sw: el.scrollWidth, cw: el.clientWidth }
  }, QN)
  await openQ(page, '1,288', { box: QN })
  const inline = await page.locator(`${QN} [data-evidence-panel="inline"] [data-ev-query]`).count()
  const o = await overflow()
  check('窄栏里查询步骤在栏内展开', inline === 1)
  check('窄栏里没有横向滚动（表格自己在框里滚）', o.sw <= o.cw, JSON.stringify(o))
  const hl = await page.locator(`${QN} td[data-highlight="cell"]`).evaluate((td) => {
    const box = td.closest('.overflow-x-auto')
    const a = td.getBoundingClientRect()
    const b = box.getBoundingClientRect()
    return a.left >= b.left - 1 && a.right <= b.right + 1
  }).catch(() => false)
  check('被引用的格滚到看得见的位置', hl)
  await page.locator(`${QN} [data-evidence-panel] button`, { hasText: '回到正文' }).click()

  const small = await open('/ui-harness.html?evidence=1', { w: 360, h: 780 })
  await small.page.locator(`${Q} [data-seg="${qseg('1,288')}"]`).click()
  await small.page.waitForSelector('[data-evidence-panel] [data-ev-query]', { timeout: 4000 }).catch(() => {})
  const s = await small.page.evaluate(() => ({ sw: document.documentElement.scrollWidth, vw: innerWidth }))
  check('360px 的屏幕：底部抽屉里有查询步骤', await small.page.locator('[data-evidence-panel="drawer"] [data-ev-query]').count() === 1)
  check('360px 的屏幕：整页没有横向滚动', s.sw <= s.vw, JSON.stringify(s))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await small.page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await small.page.waitForTimeout(250)
      await small.page.screenshot({ path: `${SHOTS}/evidence-query-360-${theme}.png` })
    }
  }
  check('没有运行时报错（360px）', small.errors.length === 0, small.errors.join(' | '))
  await small.ctx.close()

  const r = await open('/ui-harness.html?evidence=1', { reduced: true })
  await r.page.locator(`${Q} [data-seg="${qseg('8.7%')}"]`).click()
  await r.page.waitForSelector('[data-evidence-panel] [data-ev-sources]', { timeout: 4000 }).catch(() => {})
  await r.page.locator(`[data-evidence-panel] button[data-ev-input="cell(nodes.pull, 0, 'gmv')"]`).click().catch(() => {})
  await r.page.waitForTimeout(100)
  const moving = await r.page.evaluate(() => [...(document.querySelector('[data-evidence-panel]')?.getAnimations({ subtree: true }) ?? [])]
    .filter((a) => !(a instanceof CSSTransition && Number(a.effect?.getTiming().duration) <= 0.01)).length)
  check('减少动效：面板和跳转都没有动画', moving === 0, String(moving))
  await r.ctx.close()
})

await section('q-tolerant', '接口缺字段、取不到：降级显示，不白屏', async () => {
  const miss = await probeQ(() => 404)
  await openQ(miss.page, '1,288', { wait: '[data-ev-query-missing]' })
  const t = await miss.page.locator('[data-evidence-panel]').innerText()
  check('片段接口取不到：只显示文档里记的出处（Q3 · db_query__shop · 12 行），照实说取不到', t.includes('Q3')
    && t.includes('db_query__shop') && t.includes('12 行') && t.includes('未能获取查询结果的行'), t.replace(/\s+/g, ' ').slice(0, 200))
  check('……没有假装高亮了什么', await miss.page.locator('[data-evidence-panel] [data-highlight]').count() === 0)
  check('……没有运行时报错', miss.errors.length === 0, miss.errors.join(' | '))
  await miss.ctx.close()
  const thin = await probeQ((id, body) => ({ ...body, redacted: undefined,
    chain: (body.chain ?? []).map((st) => (st.step === 'query' ? { step: 'query', alias: st.alias, columns: st.columns, rows: st.rows } : st)) }))
  await openQ(thin.page, '1,288')
  const part = thin.page.locator('[data-evidence-panel] [data-ev-query]')
  check('查询步骤只有列和行（老形状）：照样画表，不高亮、不写窗口位置', await part.locator('tbody tr').count() === 5
    && await part.locator('[data-highlight]').count() === 0 && !(await part.innerText()).includes('undefined'))
  check('……没有工件 id 就不给「打开完整快照」', await part.locator('[data-ev-snapshot]').count() === 0)
  const inRange = await probeQ((id, body) => ({ ...body, chain: (body.chain ?? []).map((st) => (st.step === 'query'
    ? { ...st, highlight: { rows: [2], cols: ['amount'] } } : st)) }))
  await openQ(inRange.page, '1,288')
  check('highlight 的行号落在窗口里时按窗口内下标认（row_offset 之前的号）', (await inRange.page.locator('[data-evidence-panel] td[data-highlight="cell"]').innerText().catch(() => '')) === '1288')
  check('没有 mask_note 就不出那一句', await inRange.page.locator('[data-evidence-panel] [data-ev-query-mask-note]').count() === 0)
  check('没有运行时报错（缺字段）', thin.errors.length === 0 && inRange.errors.length === 0, [...thin.errors, ...inRange.errors].join(' | '))
  await thin.ctx.close()
  await inRange.ctx.close()

  // 查询之后数据源改名或删掉了（E2W-6）：后端按查询当时记下的遮罩处理，行照样给，另带一句 mask_note。
  // 这句写在窗口说明那一行、琥珀色：现在的数据源设置已经管不到这份快照了
  const MASK_NOTE = '数据源「shop」已不存在（可能已改名或删除），按查询时记录的遮罩处理'
  const gone = await probeQ((id, body) => ({ ...body, chain: (body.chain ?? []).map((st) => (st.step === 'query'
    ? { ...st, mask_note: MASK_NOTE } : st)) }))
  await openQ(gone.page, '1,288')
  const goneNote = gone.page.locator('[data-evidence-panel] [data-ev-query] [data-ev-query-mask-note]')
  const [goneText, goneColor, amber, sameLine] = await Promise.all([
    goneNote.innerText().catch(() => ''),
    goneNote.evaluate((el) => getComputedStyle(el).color).catch(() => ''),
    tokenColor(gone.page, 'color', 'var(--st-waiting)'),
    goneNote.evaluate((el) => el.parentElement === el.closest('[data-ev-query]')?.querySelector('[data-ev-window]')?.parentElement).catch(() => false),
  ])
  check('数据源找不到了：mask_note 原话写出来', goneText === MASK_NOTE, goneText)
  check('……琥珀色（--st-waiting），和窗口说明同一行', goneColor === amber && sameLine, `${goneColor} / ${amber} · 同一行 ${sameLine}`)
  check('……行照样画、按记下的遮罩照样写「已遮罩」', await gone.page.locator('[data-evidence-panel] [data-ev-query] tbody tr').count() === 5
    && await gone.page.locator('[data-evidence-panel] [data-ev-query] td[data-masked]').count() === 5)
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await gone.page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await gone.page.waitForTimeout(200)
      await gone.page.locator('[data-evidence-panel]').screenshot({ path: `${SHOTS}/evidence-mask-note-${theme}.png` }).catch(() => {})
    }
  }
  check('没有运行时报错（mask_note）', gone.errors.length === 0, gone.errors.join(' | '))
  await gone.ctx.close()

  // 窗口被截到 50 行（window_truncated）：被引用的另一格（第 41 行）不在窗口里，窗口里那一格照样标
  const cut = await probeQ((id, body) => ({ ...body, chain: (body.chain ?? []).map((st) => (st.step === 'query'
    ? { ...st, window_truncated: true, total_rows: 60,
        highlight: { rows: [5, 40], cols: ['amount'], cells: [[5, 'amount'], [40, 'amount']] } } : st)) }))
  await openQ(cut.page, '1,288')
  const cutCells = await cut.page.locator('[data-evidence-panel] td[data-highlight="cell"]').allInnerTexts()
  check('窗口截断、有的被引用行不在窗口里：窗口里的那一格照样高亮，不是一格都不标', cutCells.join(',') === '1288'
    && await cut.page.locator('[data-evidence-panel] tr[data-highlight="row"]').count() === 1, cutCells.join(',') || '一格都没标')
  const cutRows = await probeQ((id, body) => ({ ...body, chain: (body.chain ?? []).map((st) => (st.step === 'query'
    ? { ...st, window_truncated: true, highlight: { rows: [5, 40], cols: ['amount'] } } : st)) }))
  await openQ(cutRows.page, '1,288')
  check('……只给 rows × cols 时同样只标窗口里的那一行', (await cutRows.page.locator('[data-evidence-panel] td[data-highlight="cell"]').allInnerTexts()).join(',') === '1288')
  await cut.ctx.close()
  await cutRows.ctx.close()

  // 解析不了的单元格引用（行越界）：后端不给链。上面已经说了原因，不能再摆一块「查询的行这一次没取到」
  const REASON = 'Q3 请求第 99 行，但查询结果只有 12 行（行号从 0 开始）'
  const broken = (seg) => ({ ...seg, text: '⟦?v:Q3.r99.amount⟧', state: 'none', issue: 'unresolved_ref', ref: 'v:Q3.r99.amount',
    cite: { ref: 'Q3.r99.amount', alias: 'Q3', kind: 'cell', role: 'value', status: 'unresolved', reason: REASON } })
  const target = qseg('1,288')
  const bad = await probeQ((id, body) => (id === target
    ? { ...body, segment: broken(body.segment), chain: [], note: `引用无法解析：${REASON}` } : body), {
    patchQuery: (f) => {
      for (const b of f.doc.blocks) for (const u of b.units ?? []) u.segments = (u.segments ?? []).map((sg) => (sg.id === target ? broken(sg) : sg))
      return f
    },
  })
  await bad.page.locator(`${Q} [data-seg="${target}"]`).click()
  await bad.page.waitForSelector('[data-evidence-panel] [data-ev-reason]', { timeout: 4000 }).catch(() => {})
  await bad.page.waitForTimeout(150)
  const said = await bad.page.locator('[data-evidence-panel]').innerText().catch(() => '')
  check('无法解析的单元格引用：说清原因（行越界）', said.includes(REASON), said.replace(/\s+/g, ' ').slice(0, 200))
  check('……不再摆「查询 Q3 · 未能获取」，那像是接口出了错', await bad.page.locator('[data-evidence-panel] [data-ev-query-missing]').count() === 0
    && !said.includes('未能获取'), said.replace(/\s+/g, ' ').slice(0, 200))
  check('没有运行时报错（截断、解析不了）', cut.errors.length === 0 && bad.errors.length === 0, [...cut.errors, ...bad.errors].join(' | '))
  await bad.ctx.close()
})

await section('pinned', '没有 _evidence 的旧回答：和 Markdown.tsx 改动前逐字一样', async () => {
  // markdown-pinned.json 里的 html 是改动前（HEAD b418c0d）的 Markdown.tsx 渲染的。重新生成：把
  // `git show <旧提交>:frontend/src/run/Markdown.tsx` 临时放进 src/run/，另起一个页面逐个渲染
  // samples、取 outerHTML 写回 html 字段，然后删掉临时文件。唯一有意的改动列在 intended 里
  const pinned = JSON.parse(readFileSync(`${root}frontend/src/run/__tests__/markdown-pinned.json`, 'utf8'))
  const now = await page.evaluate(() => Object.fromEntries([...document.querySelectorAll('#md-pinned [data-pin]')]
    .map((el) => [el.dataset.pin, el.firstElementChild?.outerHTML ?? ''])))
  for (const sample of pinned.samples) {
    let before = sample.html
    for (const d of pinned.intended) before = before.split(d.from).join(d.to)
    const cur = now[sample.id] ?? ''
    let at = 0
    while (at < before.length && before[at] === cur[at]) at++
    check(`「${sample.id}」和改动前逐字一样（只差列表缩进 ml-4 → pl-4）`, !!cur && cur === before,
      cur === before ? '' : `第 ${at} 个字起不同：改动前「${before.slice(at, at + 60)}」现在「${cur.slice(at, at + 60)}」`)
  }
  // ml-4 → pl-4：量一下列表项的位置和宽度，把类名换回 ml-4 再量一遍，必须一样
  const moved = await page.evaluate(() => {
    const out = []
    for (const list of document.querySelectorAll('#md-pinned ul, #md-pinned ol')) {
      const rects = () => [...list.children].map((li) => {
        const r = li.getBoundingClientRect()
        return [r.x, r.y, r.width, r.height].map((v) => Math.round(v * 10) / 10).join(',')
      }).join('|')
      // 类名得真是 pl-4 才有得换：换不了就算没量（别的缩进一律不认）
      if (!list.classList.contains('pl-4') || list.classList.contains('ml-4')) { out.push(`类名是「${list.className}」`); continue }
      const a = rects()
      list.classList.replace('pl-4', 'ml-4')
      const b = rects()
      list.classList.replace('ml-4', 'pl-4')
      if (a !== b) out.push(`${a} ≠ ${b}`)
    }
    return { lists: document.querySelectorAll('#md-pinned ul, #md-pinned ol').length, moved: out }
  })
  check('列表缩进 pl-4 和以前的 ml-4 看起来一样：每个列表项的位置、宽度都没变', moved.lists >= 6 && !moved.moved.length,
    `${moved.lists} 个列表 ${moved.moved.join(' ; ')}`)
})

await section('studio', '报告撰写节点：画布认得、检查器能配（REQ-A-3）', async () => {
  // 假的工作流，读写都在浏览器层拦下：GET 回假的，写一律 409，不落库
  const at = (x, y) => ({ x, y })
  const node = (id, type, x, label, config = {}) => ({ id, type, position: at(x, 80), data: { label, config } })
  const graph = {
    nodes: [
      node('in', 'input', 0, '入口', { fields: [{ name: 'week', required: true }] }),
      node('caliber', 'metrics', 260, '周报口径', { caliber: '周报口径', caliber_version: 'v2',
        metrics: [{ id: 'gmv', name: '销售额', unit: '元', expression: 'input.week' }] }),
      node('write', 'report', 520, '写周报', { instructions: '为 {{ input.week }} 写周报', numbers: 'strict', max_repairs: 1 }),
      node('late', 'metrics', 780, '事后口径', { caliber: '事后', metrics: [{ id: 'x', expression: '1' }] }),
      node('out', 'output', 1040, '成果', { fields: [{ name: '周报', value: '{{ nodes.write.text }}' }] }),
    ],
    edges: [['in', 'caliber'], ['caliber', 'write'], ['write', 'late'], ['late', 'out']]
      .map(([source, target]) => ({ id: `${source}-${target}`, source, target })),
  }
  const wf = { id: 'fx-report', name: '报告节点检查', description: '', graph, tags: [], version: 1, is_template: false,
    status: 'draft', published_version: null, run_count: 0, created_at: '2026-09-26T00:00:00Z', updated_at: '2026-09-26T00:00:00Z' }
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const p = await ctx.newPage()
  const errors = []
  p.on('pageerror', (e) => errors.push(e.message))
  const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  await p.route(/\/api\/workflows(\?.*)?$/, (r) => (r.request().method() === 'GET' ? json(r, [wf]) : json(r, { detail: '检查脚本不写库' }, 409)))
  await p.route(/\/api\/workflows\/fx-report(\/.*)?(\?.*)?$/, (r) =>
    (r.request().method() === 'GET' && !new URL(r.request().url()).pathname.endsWith('/versions') ? json(r, wf) : json(r, [])))
  await p.route(/\/api\/conversations(\/.*)?(\?.*)?$/, (r) =>
    (r.request().method() === 'GET' ? json(r, []) : json(r, { id: 'fx-conv', kind: 'canvas', title: '', turns: [] })))
  await p.route(/\/api\/runs(\/.*)?(\?.*)?$/, (r) => (r.request().method() === 'GET' ? r.continue() : json(r, { detail: '不发起运行' }, 409)))
  await p.goto(`${WEB}/studio/fx-report`, { waitUntil: 'networkidle' })
  await p.waitForFunction(() => window.__studio?.getState().workflow?.id === 'fx-report', null, { timeout: 15000 })
  await p.waitForTimeout(600)
  const card = await p.locator('.react-flow__node[data-id="write"]').innerText().catch(() => '')
  check('画布上的报告节点有摘要（写作要求、指标从哪来），不是未知类型', card.includes('为 {{ input.week }} 写周报')
    && card.includes('上游全部口径卡'), card.replace(/\n/g, ' ').slice(0, 120))
  const tint = await p.evaluate(() => {
    const el = document.querySelector('.react-flow__node[data-id="write"] .nt-report')
    return el ? getComputedStyle(el).getPropertyValue('--nt').trim() : ''
  })
  check('报告节点有自己的类型色（--nt-report）', !!tint, tint)
  await p.evaluate(() => window.__studio.getState().select('write'))
  await p.waitForSelector('[data-field="metrics_from"]', { timeout: 5000 })
  check('检查器标题写「报告撰写」', (await p.locator('body').innerText()).includes('报告撰写'))
  const fields = await p.evaluate(() => [...document.querySelectorAll('[data-field]')].map((el) => el.getAttribute('data-field')))
  check('检查器的字段：写作要求、指标来自、裸数字、违规时、重写次数、模型',
    ['instructions', 'metrics_from', 'numbers', 'on_violation', 'max_repairs', 'model'].every((f) => fields.includes(f)), fields.join(','))
  await p.locator('[data-field="metrics_from"] button', { hasText: '选择口径卡' }).click()
  const opts = await p.locator('[data-field="metrics_from"] .max-h-52 button').allInnerTexts()
  check('「指标来自」只列上游的口径卡（下游那张不列）', opts.length === 1 && opts[0].includes('周报口径'), opts.join(' | '))
  const onViolation = await p.locator('[data-field="on_violation"] option').allInnerTexts()
  check('违规时的默认写明按运行类别', onViolation[0]?.includes('探索运行正常产出'), onViolation.join(' | '))
  // 结论句策略 claims（off / require_citation / judge，四期放开了 judge）和表名字段名核对 entities
  // 表名字段名核对收在「高级选项」里（很少要改），先展开
  await p.locator('button[aria-expanded]', { hasText: '高级选项' }).click().catch(() => {})
  await p.waitForTimeout(100)
  const all = await p.evaluate(() => [...document.querySelectorAll('[data-field]')].map((el) => el.getAttribute('data-field')))
  check('检查器有结论句策略、表名字段名核对（高级选项里）两项', fields.includes('claims') && all.includes('entities')
    && !fields.includes('entities'), all.join(','))
  const claimVals = await p.locator('[data-field="claims"] option').evaluateAll((els) => els.map((e) => e.value))
  check('结论句策略三档：off / require_citation / judge（四期放开了模型裁判）', claimVals.join(',') === 'off,require_citation,judge',
    claimVals.join(','))
  const claimsLabel = await p.locator('[data-field="claims"] label').first().innerText().catch(() => '')
  check('结论句策略有中文标签（发布前修复的预览也用它）', /结论句/.test(claimsLabel) && !/claims/.test(claimsLabel), claimsLabel)
  const claimsHelp = await p.locator('[data-field="claims"]').innerText()
  check('……说明写明模型逐句判断、受管级别要显式选并写金额上限，不再说「后续版本」', claimsHelp.includes('由模型逐句判断')
    && claimsHelp.includes('受管') && claimsHelp.includes('金额上限') && !claimsHelp.includes('后续版本'),
    claimsHelp.replace(/\s+/g, ' ').slice(0, 200))
  check('没写 claims 时显示默认的 off', await p.locator('[data-field="claims"] select').inputValue() === 'off'
    || (await p.locator('[data-field="claims"] select').evaluate((el) => el.options[el.selectedIndex]?.value)) === 'off')
  const entityVals = await p.locator('[data-field="entities"] option').evaluateAll((els) => els.map((e) => e.value))
  check('表名字段名核对：link / off', entityVals.join(',') === 'link,off', entityVals.join(','))
  await p.locator('[data-field="claims"] select').selectOption('require_citation')
  const cfgOf = () => p.evaluate(() => window.__studio.getState().nodes.find((n) => n.id === 'write')?.data.config)
  check('选「要求挂依据」写进 config.claims', (await cfgOf())?.claims === 'require_citation')
  // 手写或别处写进来的不认识的值：不能悄悄显示成 off，要照实写出来
  await p.evaluate(() => {
    const st = window.__studio.getState()
    const n = st.nodes.find((x) => x.id === 'write')
    st.updateNode('write', { config: { ...n.data.config, claims: 'sometimes' } })
  })
  await p.waitForTimeout(150)
  const shown = await p.locator('[data-field="claims"] select').evaluate((el) => el.options[el.selectedIndex]?.textContent ?? '')
  check('config 里写着无法识别的值：下拉照实显示，不装成 off', shown.includes('sometimes') && shown.includes('无法识别'), shown)

  // 四期：选模型裁判，高级选项里出现结论句裁判的子配置（裁判模型、三项上限各自能设不限、改写一次、证据相矛盾时）
  check('没选模型裁判时不出结论句裁判那一组', await p.locator('[data-field="judge"]').count() === 0)
  await p.locator('[data-field="claims"] select').selectOption('judge')
  check('选「请模型逐句判断」写进 config.claims = judge', (await cfgOf())?.claims === 'judge')
  await p.waitForSelector('[data-field="judge"]', { timeout: 3000 }).catch(() => {})
  const jf = p.locator('[data-field="judge"]')
  const jlabel = await jf.locator('label').first().innerText().catch(() => '')
  check('高级选项里出现「结论句裁判」一组（中文标签）', await jf.count() === 1 && jlabel.includes('结论句裁判'), jlabel)
  const keys = await jf.locator('[data-judge-key]').evaluateAll((els) => els.map((e) => e.getAttribute('data-judge-key')))
  check('……裁判模型、三项上限、改写一次、证据相矛盾时', keys.join(',') === 'model,max_claims,max_cost_usd,timeout_s,rewrite_once,on_unsupported',
    keys.join(','))
  const labels = await jf.innerText()
  // 细节只报缺了哪几个：整段文字里有沙箱的接入名（下拉的选项），日志常被贴进报告。裁判拆档后 on_unsupported
  // 管的是证据相矛盾（键名不变），标签跟着改
  const missingLabels = ['裁判模型', '最多裁判句数', '金额上限（美元）', '时长上限（秒）', '退回改写一次', '证据相矛盾时'].filter((t) => !labels.includes(t))
  check('……每一项都有中文标签', !missingLabels.length, `缺：${missingLabels.join('、')}`)
  const onHint = await jf.locator('[data-judge-key="on_unsupported"] [data-judge-hint]').innerText().catch(() => '')
  check('……「证据相矛盾时」下面写明证据不足的句子最多降档、不会因此不予出具', onHint.includes('证据不足的句子最多降档')
    && onHint.includes('不会因此不予出具'), onHint)
  check('……没写的上限说明跟着设置里的默认值', (await jf.locator('[data-judge-key="max_cost_usd"] [data-judge-note]').innerText()).includes('默认值'))
  const cost = jf.locator('[data-judge-key="max_cost_usd"]')
  await cost.locator('input[type="checkbox"]').click()
  check('金额上限勾「不限」：写 null（不是删掉，也不是 0）', (await cfgOf())?.judge && (await cfgOf()).judge.max_cost_usd === null
    && 'max_cost_usd' in (await cfgOf()).judge, JSON.stringify((await cfgOf())?.judge))
  const off = await cost.locator('[data-judge-note]').innerText().catch(() => '')
  check('……写明「不设上限，费用只受……约束」，数字框变灰', off.includes('不设上限') && off.includes('约束')
    && await cost.locator('input[type="number"]').isDisabled(), off)
  await cost.locator('input[type="checkbox"]').click()
  check('勾掉「不限」：回到没写（跟着设置里的默认值）', !((await cfgOf())?.judge && 'max_cost_usd' in (await cfgOf()).judge),
    JSON.stringify((await cfgOf())?.judge))
  await cost.locator('input[type="number"]').fill('0.2')
  await jf.locator('[data-judge-key="max_claims"] input[type="number"]').fill('20')
  await jf.locator('[data-judge-key="timeout_s"] input[type="checkbox"]').click()
  await jf.locator('[data-judge-key="rewrite_once"] input[type="checkbox"]').click()
  await jf.locator('[data-judge-key="on_unsupported"] select').selectOption('withhold')
  const j = (await cfgOf())?.judge ?? {}
  check('写数就是数、时长不限写 null、改写一次写 true、不支持时写 withhold',
    j.max_cost_usd === 0.2 && j.max_claims === 20 && j.timeout_s === null && j.rewrite_once === true && j.on_unsupported === 'withhold',
    JSON.stringify(j))
  const provFirst = () => jf.locator('[data-judge-key="model"] select').evaluate((el) => el.options[0]?.textContent ?? '').catch(() => '')
  check('接入和模型都没写：接入留空那一项叫「跟随设置」', await provFirst() === '跟随设置' && await jf.locator('[data-judge-by-model]').count() === 0)
  await jf.locator('[data-judge-key="model"] input').fill('judge-model-b')
  check('裁判模型写进 judge.model', (await cfgOf())?.judge?.model === 'judge-model-b')
  // 节点上只写了模型：节点这一级整组生效（judge_model_spec 不跨级拼），按模型名找接入——已经不是「跟随设置」
  const pf = await provFirst()
  check('只写了模型：接入留空那一项不再叫「跟随设置」，改叫「按模型名找接入」', pf === '按模型名找接入', pf)
  const byModel = jf.locator('[data-judge-by-model]')
  const byText = await byModel.innerText().catch(() => '')
  // 说明里有沙箱的接入名：细节只报认没认出来，不打印原文
  check('……写明按模型名找接入、认不出就去调默认接入，两项都没写才跟随设置', await byModel.getAttribute('data-judge-by-model').catch(() => '') === 'default'
    && byText.includes('只填了模型') && byText.includes('默认接入') && byText.includes('接入和模型都未填写时，才跟随设置'),
    `data-judge-by-model=${await byModel.getAttribute('data-judge-by-model').catch(() => '（没有）')}`)
  await jf.locator('[data-judge-key="model"] input').fill('')
  check('……模型清空：又回到「跟随设置」，说明也收起', await provFirst() === '跟随设置' && await byModel.count() === 0
    && !('model' in ((await cfgOf())?.judge ?? {})))
  await jf.locator('[data-judge-key="model"] input').fill('judge-model-b')
  const previews = await p.evaluate(async () => {
    const url = performance.getEntriesByType('resource').map((e) => e.name)
      .find((n) => { try { return new URL(n).pathname === '/src/canvas/issues.ts' } catch { return false } })
    const m = await import(url ?? '/src/canvas/issues.ts')
    return [m.fixFieldLabel('judge.max_cost_usd', 'report'), m.fixValueText(null, 'judge.max_cost_usd'),
            m.fixValueText(0.05, 'judge.max_cost_usd'), (m.fixValueLines({ max_cost_usd: null, model: 'x' }, 'judge') ?? []).join('；'),
            m.fixValueText('withhold', 'judge.on_unsupported'), m.fixFieldLabel('defaults.model', 'report')]
  }).catch((e) => [String(e)])
  check('发布前修复的预览：judge.max_cost_usd 写成「结论句裁判 · 金额上限（美元）」', previews[0] === '结论句裁判 · 金额上限（美元）', previews[0])
  check('……null 写「不限」（没写才是「（空）」）、金额带单位', previews[1] === '不限' && previews[2] === '0.05 美元', JSON.stringify(previews.slice(1, 3)))
  check('……补预算按整份 judge 记时一键一行', previews[3] === '裁判模型：x；金额上限（美元）：不限', previews[3])
  check('……证据相矛盾时写选项文字', previews[4] === '不予出具', previews[4])
  check('……别处的 model 不借用裁判的叫法', previews[5] === '工作流默认设置 · model', previews[5])
  if (SHOTS) {
    await jf.scrollIntoViewIfNeeded()
    for (const theme of ['dark', 'light']) {
      await p.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await p.waitForTimeout(250)
      await p.screenshot({ path: `${SHOTS}/evidence4-inspector-judge-${theme}.png` })
    }
  }
  const fixLabel = await p.evaluate(async () => {
    // 按页面自己加载时的地址 import：改过的模块地址带 ?t=，直接写 /src/… 会拿到另一份实例
    const url = performance.getEntriesByType('resource').map((e) => e.name)
      .find((n) => { try { return new URL(n).pathname === '/src/canvas/issues.ts' } catch { return false } })
    const m = await import(url ?? '/src/canvas/issues.ts')
    return [m.fixFieldLabel('claims', 'report'), m.fixFieldLabel('contract.claims.on_uncited', 'output'),
            m.fixValueText('ignore', 'contract.claims.on_uncited'), m.fixValueText('degrade', 'contract.claims.on_uncited')]
  }).catch((e) => [String(e)])
  check('发布前修复预览里 claims 这个字段写中文名', fixLabel[0] === claimsLabel.trim() && !/claims/.test(fixLabel[0]), fixLabel[0])
  check('契约 claims 的 on_uncited（ignore 改成 degrade 那条修复）：字段和值都写中文',
    !/claims|on_uncited/.test(fixLabel[1] ?? 'claims') && fixLabel[2] === '仅标注，不计入缺口' && fixLabel[3] === '计入缺口、出具降档',
    JSON.stringify(fixLabel.slice(1)))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await p.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await p.waitForTimeout(250)
      await p.screenshot({ path: `${SHOTS}/evidence-studio-report-${theme}.png` })
    }
  }
  check('没有运行时报错（画布）', errors.length === 0, errors.join(' | '))
  await ctx.close()
})

await section('config', '配置项：agent 的 cite_fields、code 的证据角色、口径卡钉住上游（caliber_from + upgrade_policy）', async () => {
  // 假的工作流和上游，读写都在浏览器层拦下：GET 回假的，写一律 409，不落库
  const node = (id, type, x, label, config = {}) => ({ id, type, position: { x, y: 80 }, data: { label, config } })
  const graph = {
    nodes: [
      node('in', 'input', 0, '入口', { fields: [{ name: 'week', required: true }] }),
      node('fetch', 'agent', 240, '查数', { prompt: '查本周销售额', tools: ['db_query__shop'] }),
      node('calc', 'code', 480, '换算', { language: 'python', code: 'print(1)' }),
      node('caliber', 'metrics', 720, '周报口径', { caliber: '周报口径', caliber_version: 'v2',
        metrics: [{ id: 'gmv', name: '销售额', unit: '元', expression: 'vars.kpi.gmv' }] }),
      node('sub', 'subgraph', 960, '方法卡', { workflow_id: 'fx-weekly', input: {} }),
    ],
    edges: [['in', 'fetch'], ['fetch', 'calc'], ['calc', 'caliber'], ['caliber', 'sub']].map(([source, target]) => ({ id: `${source}-${target}`, source, target })),
  }
  const stamp = { tags: [], is_template: false, status: 'draft', published_version: null, run_count: 0,
    created_at: '2026-09-26T00:00:00Z', updated_at: '2026-09-26T00:00:00Z', description: '' }
  const wf = { id: 'fx-config', name: '配置项检查', graph, version: 1, ...stamp }
  const upstream = { id: 'fx-weekly', name: '销售周报', graph: { nodes: [], edges: [] }, version: 6, ...stamp, status: 'published', published_version: 6 }
  const pinnedGraph = { nodes: [
    node('caliber', 'metrics', 0, '周报口径', { caliber: '周报口径', caliber_version: 'v3',
      metrics: [{ id: 'gmv', name: '销售额', expression: 'vars.kpi.gmv' }, { id: 'aov', name: '客单价', expression: 'vars.kpi.gmv / vars.kpi.order_cnt' }] }),
    node('write', 'report', 240, '写周报', {}),
  ], edges: [] }
  const versions = [5, 6].map((v) => ({ id: `v${v}`, version: v, note: v === 6 ? '改了客单价口径' : '', published: v === 6 }))
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const p = await ctx.newPage()
  p.setDefaultTimeout(8000)
  const errors = []
  p.on('pageerror', (e) => errors.push(e.message))
  const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  await p.route(/\/api\/workflows(\?.*)?$/, (r) => (r.request().method() === 'GET' ? json(r, [wf, upstream]) : json(r, { detail: '检查脚本不写库' }, 409)))
  await p.route(/\/api\/workflows\/fx-config(\/.*)?(\?.*)?$/, (r) =>
    (r.request().method() === 'GET' && !new URL(r.request().url()).pathname.endsWith('/versions') ? json(r, wf) : json(r, [])))
  await p.route(/\/api\/workflows\/fx-weekly\/versions(\/\d+)?$/, (r) => {
    const m = new URL(r.request().url()).pathname.match(/\/versions\/(\d+)$/)
    return m ? json(r, { ...versions.find((v) => v.version === Number(m[1])), workflow_id: 'fx-weekly', graph: pinnedGraph }) : json(r, versions)
  })
  await p.route(/\/api\/conversations(\/.*)?(\?.*)?$/, (r) =>
    (r.request().method() === 'GET' ? json(r, []) : json(r, { id: 'fx-conv', kind: 'canvas', title: '', turns: [] })))
  await p.route(/\/api\/runs(\/.*)?(\?.*)?$/, (r) => (r.request().method() === 'GET' ? r.continue() : json(r, { detail: '不发起运行' }, 409)))
  await p.goto(`${WEB}/studio/fx-config`, { waitUntil: 'networkidle' })
  await p.waitForFunction(() => window.__studio?.getState().workflow?.id === 'fx-config', null, { timeout: 15000 })
  await p.waitForTimeout(500)
  const cfg = (id) => p.evaluate((id) => window.__studio.getState().nodes.find((n) => n.id === id)?.data.config, id)
  const advanced = async () => {
    const t = p.getByRole('button', { name: /高级选项/ })
    if ((await t.getAttribute('aria-expanded')) !== 'true') await t.click()
  }

  await p.evaluate(() => window.__studio.getState().select('fetch'))
  await advanced()
  const cite = p.locator('[data-field="cite_fields"]')
  check('agent 有「按出处核对字段」开关，没写 Schema 时用不了并说清差什么', await cite.locator('input[type="checkbox"]').isDisabled()
    && (await cite.innerText()).includes('需先填写上方的「结构化输出 Schema」'), (await cite.innerText()).replace(/\s+/g, ' ').slice(0, 120))
  check('……说明会额外进行一次抽取调用', (await cite.innerText()).includes('额外进行一次抽取调用'))
  check('output_schema 的说明写明要开 cite_fields 才生效', (await p.locator('[data-field="output_schema"]').innerText()).includes('按出处核对字段'))
  await p.evaluate(() => {
    const s = window.__studio.getState()
    const n = s.nodes.find((x) => x.id === 'fetch')
    s.updateNode('fetch', { config: { ...n.data.config, output_schema: { type: 'object', properties: { gmv: { type: 'number' } } } } })
  })
  await p.waitForTimeout(150)
  check('写了 Schema：开关可用', !(await cite.locator('input[type="checkbox"]').isDisabled()) && await cite.locator('[id$="-off"]').count() === 0)
  await cite.locator('input[type="checkbox"]').check()
  check('……打开后写进 config.cite_fields', (await cfg('fetch'))?.cite_fields === true)
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await p.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await p.waitForTimeout(200)
      await p.screenshot({ path: `${SHOTS}/evidence-config-agent-${theme}.png` })
    }
  }
  // 开着开关又把 Schema 清掉：不能锁死在「开」上，得还能关
  await p.evaluate(() => {
    const s = window.__studio.getState()
    const { output_schema: _drop, ...rest } = s.nodes.find((x) => x.id === 'fetch').data.config
    s.updateNode('fetch', { config: rest })
  })
  await p.waitForTimeout(150)
  check('开着又清掉 Schema：开关不锁死，还能关', !(await cite.locator('input[type="checkbox"]').isDisabled())
    && await cite.getAttribute('data-disabled') === null && await cite.locator('[id$="-off"]').count() === 0)
  await cite.locator('input[type="checkbox"]').uncheck({ timeout: 2000 }).catch(() => {})
  check('……关掉后 config.cite_fields 不再为真；关掉之后才锁上', !(await cfg('fetch'))?.cite_fields
    && await cite.locator('input[type="checkbox"]').isDisabled())

  await p.evaluate(() => window.__studio.getState().select('calc'))
  await advanced()
  const role = p.locator('[data-field="evidence_role"]')
  const opts = await role.locator('option').allInnerTexts()
  check('code 有「证据角色」下拉：计算（默认）/ 取数', opts.join('|') === '计算（默认）|取数', opts.join('|'))
  check('……说明报告不能直接引用代码节点的输出', (await role.innerText()).includes('报告不能直接引用代码节点的输出'))
  await role.locator('select').selectOption('source')
  check('……选「取数」写进 config.evidence_role', (await cfg('calc'))?.evidence_role === 'source')

  await p.evaluate(() => window.__studio.getState().select('caliber'))
  const from = p.locator('[data-field="caliber_from"]')
  check('口径卡默认「在此定义」，本地的名称、版本、指标都在', await from.locator('[data-caliber-from="local"]').count() === 1
    && await p.locator('[data-field="metrics"]').count() === 1 && await p.locator('[data-field="upgrade_policy"]').count() === 0)
  await from.getByRole('radio', { name: '引用其他工作流的口径卡' }).click()
  await p.waitForTimeout(150)
  check('切到引用：本地定义收起（后端也说它们不生效），升版处置出来', await p.locator('[data-field="metrics"]').count() === 0
    && await p.locator('[data-field="caliber"]').count() === 0 && await p.locator('[data-field="upgrade_policy"]').count() === 1)
  const wfOpts = await from.locator('select').first().locator('option').allInnerTexts()
  check('工作流下拉复用目录，不列自己', wfOpts.some((o) => o.includes('销售周报')) && !wfOpts.some((o) => o.includes('配置项检查')), wfOpts.join('|'))
  await from.locator('select').first().selectOption('fx-weekly')
  await p.waitForSelector('[data-field="caliber_from"] select[id$="-version"] option[value="5"]', { timeout: 4000 }).catch(() => {})
  await from.locator('select[id$="-version"]').selectOption('5')
  await p.waitForSelector('[data-field="caliber_from"] [data-caliber-preview]', { timeout: 4000 }).catch(() => {})
  const c = await cfg('caliber')
  check('选了工作流和版本：那一版只有一张口径卡时直接引用，写成 {workflow_id, workflow_version（数）, node_id}',
    JSON.stringify(c?.caliber_from) === JSON.stringify({ workflow_id: 'fx-weekly', workflow_version: 5, node_id: 'caliber' }), JSON.stringify(c?.caliber_from))
  check('……本地的 metrics、caliber 不留在配置里', !('metrics' in (c ?? {})) && !('caliber' in (c ?? {})), JSON.stringify(Object.keys(c ?? {})))
  const preview = await from.locator('[data-caliber-preview]').innerText().catch(() => '')
  check('……预览引用的那张卡：口径、版本、几个指标', preview.includes('周报口径') && preview.includes('v3') && preview.includes('2 个指标')
    && preview.includes('aov'), preview.replace(/\s+/g, ' '))
  const newer = await from.locator('[data-caliber-newer]').innerText().catch(() => '')
  check('上游已有更新的版本：提醒正式运行前要声明升版处置', newer.includes('上游已有 v6') && newer.includes('升版处置'), newer)
  const pol = await p.locator('[data-field="upgrade_policy"] option').allInnerTexts()
  check('升版处置三选一，和子工作流同一套说法', pol.length === 4 && pol[0].includes('阻止正式运行') && pol.some((o) => o.includes('新旧口径并列展示')), pol.join('|'))
  await p.locator('[data-field="upgrade_policy"] select').selectOption('dual')
  check('……写进 config.upgrade_policy', (await cfg('caliber'))?.upgrade_policy === 'dual')
  const card = await p.locator('.react-flow__node[data-id="caliber"]').innerText().catch(() => '')
  check('画布卡片写「引用上游口径卡 v5」，不再写「0 个指标」', card.includes('引用上游口径卡 v5') && !card.includes('0 个指标'), card.replace(/\s+/g, ' ').slice(0, 120))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await p.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await p.waitForTimeout(200)
      await p.screenshot({ path: `${SHOTS}/evidence-config-caliber-${theme}.png` })
    }
  }
  await from.getByRole('radio', { name: '在此定义' }).click()
  await p.waitForTimeout(150)
  const back = await cfg('caliber')
  check('切回「在此定义」：去掉 caliber_from 和 upgrade_policy，给回一条空指标', !('caliber_from' in back) && !('upgrade_policy' in back)
    && Array.isArray(back.metrics) && back.metrics.length === 1 && await p.locator('[data-field="metrics"]').count() === 1, JSON.stringify(back))

  // 子工作流钉住版本后的升版处置（E2W-7）：和口径卡的是同一个下拉、同一套文案
  await p.evaluate(() => window.__studio.getState().select('sub'))
  await p.waitForTimeout(150)
  check('子工作流没固定版本（跟随最新）：不出升版处置', await p.locator('[data-field="upgrade_policy"]').count() === 0)
  const subVersion = p.locator('[data-field="workflow_id"] select[id$="-version"]')
  await p.waitForSelector('[data-field="workflow_id"] select[id$="-version"] option[value="5"]', { timeout: 4000 }).catch(() => {})
  await subVersion.selectOption('5')
  await p.waitForSelector('[data-field="upgrade_policy"]', { timeout: 3000 }).catch(() => {})
  const subPol = await p.locator('[data-field="upgrade_policy"] option').allInnerTexts()
  check('固定 v5：升版处置出来，选项和口径卡的那个一字不差', subPol.length === 4 && JSON.stringify(subPol) === JSON.stringify(pol), subPol.join('|'))
  check('……紧跟在「工作流」后面', await p.evaluate(() => {
    const fields = [...document.querySelectorAll('[data-field]')].map((el) => el.getAttribute('data-field'))
    return fields[fields.indexOf('workflow_id') + 1] === 'upgrade_policy'
  }))
  const subHelp = await p.locator('[data-field="upgrade_policy"]').innerText()
  check('……说明换成子工作流这一侧：规则与引用其他工作流的口径卡相同', subHelp.includes('规则与引用其他工作流的口径卡相同')
    && !subHelp.includes('规则与子工作流的升版处置相同'), subHelp.replace(/\s+/g, ' ').slice(0, 120))
  const subNewer = await p.locator('[data-field="workflow_id"] [data-subgraph-newer]').innerText().catch(() => '')
  check('上游已有 v6：提醒要声明升版处置，和口径卡同一句', subNewer === newer, subNewer)
  await p.locator('[data-field="upgrade_policy"] select').selectOption('recompute')
  check('……写进子工作流的 config.upgrade_policy', (await cfg('sub'))?.upgrade_policy === 'recompute')
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await p.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await p.waitForTimeout(200)
      await p.screenshot({ path: `${SHOTS}/evidence-config-subgraph-${theme}.png` })
    }
  }
  await subVersion.selectOption('')
  await p.waitForTimeout(150)
  const unpinned = await cfg('sub')
  check('改回跟随最新：upgrade_policy 一起拿掉、下拉收起', !('upgrade_policy' in unpinned) && !('workflow_version' in unpinned)
    && await p.locator('[data-field="upgrade_policy"]').count() === 0, JSON.stringify(unpinned))
  check('没有运行时报错（配置项）', errors.length === 0, errors.join(' | '))
  await ctx.close()
})

// ------------------------------------------------------------------ 第三期：实体、引文、旧运行的猜测

const E = {
  num: segOf(fxe.doc, '45,678.5'), orders: segOf(fxe.doc, 'orders'), week: segOf(fxe.doc, 'orders.week'),
  refunds: segOf(fxe.doc, '`refunds`'), column: segOf(fxe.doc, '`refunds.refund_amount`'),
  coupon: segOf(fxe.doc, '`orders.coupon_code`'), discount: segOf(fxe.doc, 'orders.discount'),
  promo: segOf(fxe.doc, '`promo_events`'), customers: segOf(fxe.doc, 'customers'), bare: segOf(fxe.doc, '30'),
  quote: segOf(fxe.doc, '退款金额以财务确认日为准，未确认的退款不计入当周'), miss: segOf(fxe.doc, '周报一律按自然周统计'),
}
const ent = '#evidence-entity'

await section('entity', '三期：表名字段名、可疑实体、逐字引文——四通道分得开', async () => {
  check('夹具里认得出每一种片段', Object.values(E).every(Boolean), JSON.stringify(E))
  const look = await page.evaluate(([sel, E]) => Object.fromEntries(Object.entries(E).map(([k, id]) => {
    const el = document.querySelector(`${sel} [data-seg="${id}"]`)
    if (!el) return [k, null]
    const cs = getComputedStyle(el)
    return [k, { style: cs.textDecorationStyle, color: cs.textDecorationColor, state: el.getAttribute('data-ev-state'),
                 kind: el.getAttribute('data-ev-kind'), label: el.getAttribute('aria-label') ?? '', text: el.innerText,
                 code: !!el.querySelector('code'), before: getComputedStyle(el, '::before').content }]
  })), [ent, E])
  check('有出处的实体和数字同一套「有出处」线型：实线、同一个颜色', look.orders?.state === 'deterministic'
    && look.orders.style === 'solid' && look.orders.color === look.num?.color, JSON.stringify([look.orders, look.num?.color]))
  check('可疑实体用「无证据」的线型（点状），和有出处的实体线型不同', look.coupon?.state === 'suspect'
    && look.coupon.style === 'dotted' && look.coupon.style !== look.orders?.style, JSON.stringify(look.coupon))
  check('标记写的可疑实体（[[c:…]] 编造的字段）同样标成可疑', look.discount?.state === 'suspect' && look.discount.style === 'dotted',
    JSON.stringify(look.discount))
  check('可疑实体的 aria-label 写「疑似不存在的名称」，名字不带反引号', look.coupon?.label.startsWith('orders.coupon_code，')
    && look.coupon.label.includes('疑似不存在的名称'), look.coupon?.label)
  check('无法核实的名字：点状线、自己的说法，不说「疑似不存在」', look.promo?.state === 'unverified' && look.promo.style === 'dotted'
    && look.promo.label.includes('无法核实') && !look.promo.label.includes('疑似不存在'), look.promo?.label)
  check('核对不了和可疑实体颜色不同（它只是标注，不醒目）', look.promo?.color !== look.coupon?.color, `${look.promo?.color} / ${look.coupon?.color}`)
  check('反引号里的实体按行内代码画，不露反引号', look.refunds?.code && !look.refunds.text.includes('`')
    && look.column?.code, JSON.stringify([look.refunds, look.column?.text]))
  check('自动链接的名字（正文里的已知表名）也是有出处的实体', look.customers?.state === 'deterministic' && look.customers.kind === 'entity')
  check('aria-label：实体说清是表还是字段、出现在哪几次查询', look.orders?.label === 'orders，有出处：表 orders · 出现在查询 Q1、Q2',
    look.orders?.label)
  check('引文片段用引用样式：前面挂引号、实线', look.quote?.kind === 'quote' && look.quote.before.includes('“')
    && look.quote.style === 'solid' && look.quote.state === 'deterministic', JSON.stringify(look.quote))
  check('原话对不上的引文照样是引用样式，但线型换成点状（无证据）', look.miss?.kind === 'quote' && look.miss.before.includes('“')
    && look.miss.style === 'dotted' && look.miss.state === 'none', JSON.stringify(look.miss))
  check('引文的 aria-label 写出出处', look.quote?.label.includes('有出处') && look.quote.label.includes('运营手册 · 退款'), look.quote?.label)
  const tags = await page.locator(`${ent} .ev-tag`).allInnerTexts()
  check('句末小标签：可疑名称挂「?!可疑名称」，和「?无证据」分开', tags.includes('?!可疑名称') && tags.includes('?无证据'), tags.join('|'))
  const tally = await page.locator(`${ent} [data-evidence-tally]`).innerText().catch(() => '')
  check('证据条另说可疑名称的个数', tally.includes('可疑名称 2'), tally)
  const summary = await page.locator(`${ent} [data-evidence-summary]`).innerText().catch(() => '')
  check('读屏摘要写出可疑实体、无法核实的个数', summary.includes('2 个疑似不存在的名称') && summary.includes('1 个名称无法核实'), summary)

  // n / N：可疑实体进跳转（异常态），核对不了的不进（只是标注）
  await page.locator(`${ent} [data-seg="${E.num}"]`).focus()
  const jumps = []
  for (let i = 0; i < 5; i++) {
    await page.keyboard.press('n')
    jumps.push(await active(page))
  }
  check('n 依次跳到裸数字、可疑实体、原话对不上的引文，跳过核对不了的名字',
    jumps.join(',') === [E.bare, E.coupon, E.discount, E.miss, E.bare].join(','), jumps.join(','))
})

await section('e-panel', '三期：面板里的实体步骤、可疑实体的提示、引文步骤', async () => {
  const p = panel(page)
  await page.locator(`${ent} [data-seg="${E.week}"]`).click()
  await page.waitForSelector('[data-evidence-panel] [data-ev-entity-type]', { timeout: 4000 }).catch(() => {})
  check('面板标题是名字本身', (await p.locator('h3').innerText().catch(() => '')) === 'orders.week')
  check('实体步骤写字段类型', (await p.locator('[data-ev-entity-type]').innerText().catch(() => '')).includes('VARCHAR(8)'))
  check('实体步骤写表结构快照的同步时间', (await p.locator('[data-ev-entity-synced]').innerText().catch(() => '')).includes('2026-09-20'),
    await p.locator('[data-ev-entity-synced]').innerText().catch(() => ''))
  check('快照不全时照实说只包含部分表', (await p.innerText()).includes('表结构快照仅包含部分表'))
  check('实体的来历：表结构快照', (await p.locator('[data-ev-entity-source]').allInnerTexts()).some((t) => t.includes('表结构快照')))
  await page.locator(`${ent} [data-seg="${E.orders}"]`).click()
  await page.waitForTimeout(250)
  const queries = await p.locator('[data-ev-entity-queries]').innerText().catch(() => '')
  check('表：出现在哪几次查询里', queries.includes('Q1') && queries.includes('Q2'), queries)
  const srcs = await p.locator('[data-ev-entity-source]').allInnerTexts()
  check('表的来历逐条列：表结构快照、查询 SQL', srcs.some((t) => t.includes('查询 Q2') && t.includes('SQL')), srcs.join(' | '))
  check('实体步骤取自证据接口（按 runId:segId 取了一次）', harness.hits.includes(`e:${E.orders}`), harness.hits.filter((h) => h.startsWith('e:')).join(','))

  await page.locator(`${ent} [data-seg="${E.discount}"]`).click()
  // 提示要等片段接口回来（先显示「正在取」）：等到名字出来再读
  await page.waitForSelector('[data-evidence-panel] [data-ev-closest-name]', { timeout: 4000 }).catch(() => {})
  const reason = await p.locator('[data-ev-reason]').innerText().catch(() => '')
  check('可疑实体说清为什么：本次运行里哪里都没有这个名字', reason.includes('疑似不存在的名称'), reason.slice(0, 80))
  const closest = await p.locator('[data-ev-closest-name]').allInnerTexts()
  check('可疑实体给出最接近的已知名字（最多 3 个，不带 c: 前缀）', closest.length >= 1 && closest.length <= 3
    && closest.includes('orders.amount') && closest.every((n) => !n.startsWith('c:')), closest.join(','))
  check('可疑实体的面板徽标：?! 疑似不存在的名称', (await p.locator('[data-ev-badge]').innerText().catch(() => '')).includes('疑似不存在的名称'))

  await page.locator(`${ent} [data-seg="${E.promo}"]`).click()
  await page.waitForTimeout(250)
  const promo = await p.innerText()
  check('无法核实的名字：说快照不全、无法核实，不说疑似不存在', promo.includes('无法核实') && !promo.includes('疑似不存在'), promo.slice(0, 120))

  await page.locator(`${ent} [data-seg="${E.quote}"]`).click()
  await page.waitForSelector('[data-evidence-panel] [data-ev-quote-hit]', { timeout: 4000 }).catch(() => {})
  const src = await p.locator('[data-ev-quote-source]').innerText().catch(() => '')
  check('引文步骤：原文所在的文档和片段', src.includes('运营手册 · 退款') && src.includes('第 3 段'), src)
  const hit = await p.locator('[data-ev-quote-hit]').innerText().catch(() => '')
  check('引文在原文里的位置高亮出来，就是那句原话', hit === '退款金额以财务确认日为准，未确认的退款不计入当周', hit)
  const ctx = await p.locator('[data-ev-quote]').innerText().catch(() => '')
  check('高亮前后带着原文的上下文', ctx.includes('第三章 退款口径') && ctx.includes('部分退款'), ctx.slice(0, 80))
  await page.locator(`${ent} [data-seg="${E.miss}"]`).click()
  await page.waitForTimeout(250)
  const miss = await p.innerText()
  check('原话对不上的引文：说清找不到这段引文的原文', miss.includes('找不到这段引文的原文'), miss.slice(0, 120))
  await page.keyboard.press('Escape')
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      for (const [name, id] of [['entity', E.orders], ['suspect', E.discount], ['quote', E.quote]]) {
        await page.locator(`${ent} [data-seg="${id}"]`).click()
        await page.waitForTimeout(350)
        await page.screenshot({ path: `${SHOTS}/evidence3-${name}-${theme}.png` })
      }
      await page.keyboard.press('Escape')
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
})

await section('e-narrow', '三期在 360px：栏内展开不横向滚动；减少动效', async () => {
  const narrow = '#evidence-entity-narrow'
  const overflow = () => page.evaluate((sel) => {
    const el = document.querySelector(sel)
    return { sw: el.scrollWidth, cw: el.clientWidth }
  }, narrow)
  for (const id of [E.quote, E.discount, E.orders]) {
    await page.locator(`${narrow} [data-seg="${id}"]`).click()
    await page.waitForSelector(`${narrow} [data-evidence-panel="inline"]`, { timeout: 4000 }).catch(() => {})
    await page.waitForTimeout(250)
    const o = await overflow()
    check(`360px 栏内面板（${id}）不出横向滚动`, o.sw <= o.cw, JSON.stringify(o))
  }
  await page.keyboard.press('Escape')
  const r = await open('/ui-harness.html?evidence=1', { reduced: true, w: 360, h: 780 })
  const scroll = () => r.page.evaluate(() => ({ sw: document.documentElement.scrollWidth, vw: innerWidth }))
  await r.page.locator(`${ent} [data-seg="${E.quote}"]`).click()
  await r.page.waitForSelector('[data-evidence-panel] [data-ev-quote]', { timeout: 4000 }).catch(() => {})
  const s1 = await scroll()
  check('360px 屏幕：引文面板（底部抽屉）整页不横向滚动', s1.sw <= s1.vw, JSON.stringify(s1))
  const anim = await r.page.evaluate(() => document.querySelector('[data-evidence-panel]')?.getAnimations({ subtree: true })
    .filter((a) => !(a instanceof CSSTransition && Number(a.effect?.getTiming().duration) <= 0.01)).length ?? -1)
  check('减少动效：引文面板没有动画', anim === 0, String(anim))
  await r.page.keyboard.press('Escape')
  await r.page.locator('#evidence-guess [data-ev-guess-toggle]').click().catch(() => {})
  await r.page.waitForTimeout(150)
  const s2 = await scroll()
  check('360px 屏幕：展开猜测之后整页不横向滚动', s2.sw <= s2.vw
    && await r.page.locator('#evidence-guess [data-ev-guess]').getAttribute('data-open').catch(() => null) === 'true', JSON.stringify(s2))
  const guessAnim = await r.page.evaluate(() => document.querySelector('#evidence-guess [data-ev-guess]')?.getAnimations({ subtree: true })
    .filter((a) => !(a instanceof CSSTransition && Number(a.effect?.getTiming().duration) <= 0.01)).length ?? -1)
  check('减少动效：展开猜测没有动画', guessAnim === 0, String(guessAnim))
  check('没有运行时报错（三期 360px）', r.errors.length === 0, r.errors.join(' | '))
  await r.ctx.close()
})

await section('banner', '出具横幅那一行：数字都有出处时，可疑名字、没挂依据的结论句也写出来', async () => {
  const at = (key) => page.locator(`#issuance-line [data-issuance-case="${key}"]`)
  const line = await at('suspect').locator('[data-evidence-line]').innerText().catch(() => '')
  check('数字那半句照旧：3 个数字都有出处', line.includes('3 个数字都有出处'), line)
  check('可疑名称另起一句：?! 可疑名称 2', await at('suspect').locator('[data-evidence-line-suspect]').count() === 1
    && line.includes('?! 可疑名称 2'), line)
  check('未附依据的结论句另起一句：未附依据的结论句 1', await at('suspect').locator('[data-evidence-line-claims]').count() === 1
    && line.includes('未附依据的结论句 1'), line)
  const next = at('suspect').locator('[data-evidence-next]')
  check('数字都有出处、只有可疑名称时也给「定位下一处」', await next.count() === 1)
  await page.evaluate(() => { window.__issuanceNext = 0 })
  await next.click()
  check('点了它就去找下一处', await page.evaluate(() => window.__issuanceNext) === 1)
  const clean = await at('clean').locator('[data-evidence-line]').innerText().catch(() => '')
  check('都干净的那一份：只有数字那半句，不给「定位下一处」', clean.includes('3 个数字都有出处')
    && await at('clean').locator('[data-evidence-line-suspect], [data-evidence-line-claims], [data-evidence-next]').count() === 0, clean)
  const names = await at('names').locator('[data-evidence-line]').innerText().catch(() => '')
  check('一个数字都没有、只有可疑名称：这一行照样出来，开头不带「·」，也给「定位下一处」',
    names.trim().startsWith('?! 可疑名称 1') && await at('names').locator('[data-evidence-next]').count() === 1, names)
})

await section('guess', '旧运行按数值猜的候选：默认折叠，展开写明「猜测」，淡点状线', async () => {
  const g = '#evidence-guess'
  const box = page.locator(`${g} [data-ev-guess]`)
  check('默认折叠', await box.getAttribute('data-open').catch(() => null) === 'false')
  check('折叠时看不到候选（不在页面上，不只是藏起来）', await page.locator(`${g} [data-ev-state="candidate"]`).count() === 0)
  const head = await page.locator(`${g} [data-ev-guess-toggle]`).innerText().catch(() => '')
  check('折叠的标题就写明「猜测」', head.includes('猜测'), head)
  const toggle = page.locator(`${g} [data-ev-guess-toggle]`)
  check('展开按钮是 button，带 aria-expanded=false', await toggle.evaluate((el) => el.tagName).catch(() => '') === 'BUTTON'
    && await toggle.getAttribute('aria-expanded').catch(() => null) === 'false')
  const answer = await page.locator(`${g}`).innerText().catch(() => '')
  check('折叠时答案本身照常显示', answer.includes('本周销售额 45,678.5 元'), answer.slice(0, 60))
  await toggle.click()
  await page.waitForTimeout(150)
  check('展开后 aria-expanded=true', await toggle.getAttribute('aria-expanded').catch(() => null) === 'true')
  const note = await page.locator(`${g} [data-ev-guess-note]`).innerText().catch(() => '')
  check('展开后写明「猜测的来源，不能当证据」', note.includes('猜测的来源，不能当证据'), note)
  const cands = await page.evaluate((sel) => [...document.querySelectorAll(`${sel} [data-ev-state="candidate"]`)].map((el) => {
    const cs = getComputedStyle(el)
    // 候选是 Markdown 里的行内标记（保留旧答案的版式），读屏靠标记里那句 sr-only 的「猜测的来源：…」
    return { style: cs.textDecorationStyle, color: cs.textDecorationColor, line: el.getAttribute('data-ev-line'),
             label: el.textContent ?? '', title: el.getAttribute('title') ?? '', text: el.firstChild?.textContent ?? '' }
  }), g)
  check('有候选的数字画成候选（4 个）', cands.length === 4, cands.map((c) => c.text).join(','))
  check('候选用淡点状线，不用确定性的实线', cands.length > 0 && cands.every((c) => c.style === 'dotted' && c.line === 'dotted'),
    JSON.stringify(cands[0]))
  const detColor = await page.evaluate((sel) => {
    const el = document.querySelector(`${sel} [data-ev-state="deterministic"]`)
    return el ? getComputedStyle(el).textDecorationColor : ''
  }, ent)
  check('候选的线和「有出处」不是一个颜色', cands.length > 0 && cands.every((c) => c.color !== detColor), `${cands[0]?.color} / ${detColor}`)
  check('候选给读屏写「猜测的来源」和候选出处，悬停也看得到', cands[0]?.label.includes('猜测的来源') && cands[0].label.includes('Q1')
    && cands[0].title.includes('Q1'), cands[0]?.label)
  const list = await page.locator(`${g} [data-ev-candidates]`).innerText().catch(() => '')
  check('候选清单：每个数字可能来自哪一格', list.includes('查询 Q1') && list.includes('gmv'), list.slice(0, 120))
  const plain = await page.evaluate((sel) => [...document.querySelectorAll(`${sel} [data-ev-guess-body] [data-ev-state]`)]
    .map((el) => el.firstChild?.textContent ?? ''), g)
  check('没猜到候选的数字不画线（40%、50,000）', !plain.includes('40%') && !plain.includes('50,000'), plain.join(','))
  await toggle.click()
  await page.waitForTimeout(100)
  check('再点一次收起', await box.getAttribute('data-open').catch(() => null) === 'false')
  if (SHOTS) {
    await toggle.click()
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(200)
      await page.locator(g).screenshot({ path: `${SHOTS}/evidence3-guess-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
    await toggle.click()
  }
})

// ---------------------------------------------------------------------------
// 第四期：结论句裁判
// ---------------------------------------------------------------------------

const J = '#evidence-judge'
const JX = '#evidence-judge-explore'
const JP = '#evidence-judge-plain'
const badge = (pg, sel, unit) => pg.locator(`${sel} [data-ev-claim="${unit}"]`)
/** 令牌算出来的颜色（借查询步骤那节的 tokenColor 探针），和徽标的计算色比 */
const colorOf = (pg, token) => tokenColor(pg, 'color', `var(${token})`)
const activeKey = (pg) => pg.evaluate(() => document.activeElement?.getAttribute('data-seg')
  ?? (document.activeElement?.getAttribute('data-ev-claim') ? `claim:${document.activeElement.getAttribute('data-ev-claim')}` : null)
  ?? (document.activeElement?.closest('[data-evidence-panel]') ? 'panel' : document.activeElement?.tagName ?? null))

await section('judge', '四期：结论句的句末徽标——四通道、不用确定性的绿、整句不画线、n / N 也跳到证据不支持的句子', async () => {
  const u = fxj.formal.units
  const look = await page.evaluate(({ sel, units }) => Object.fromEntries(Object.entries(units).map(([k, id]) => {
    const el = document.querySelector(`${sel} [data-ev-claim="${id}"]`)
    if (!el) return [k, null]
    const cs = getComputedStyle(el)
    return [k, { verdict: el.getAttribute('data-ev-verdict'), glyph: el.textContent, color: cs.color, tag: el.tagName,
                 type: el.getAttribute('type'), label: el.getAttribute('aria-label'), deco: cs.textDecorationLine,
                 align: cs.verticalAlign }]
  })), { sel: J, units: u })
  check('有判定的结论句句末都挂徽标（有依据、证据不支持、部分有依据、未裁判）',
    look.ok?.verdict === 'supported' && look.bad?.verdict === 'unsupported' && look.partial?.verdict === 'partial'
    && look.limit?.verdict === 'unjudged', JSON.stringify(Object.fromEntries(Object.entries(look).map(([k, v]) => [k, v?.verdict]))))
  check('判成「不是结论句」的不挂徽标', look.not === null)
  check('字形：◆ 有依据、◇ 部分、! 不支持、? 未裁判', [look.ok, look.partial, look.bad, look.limit].map((x) => x?.glyph).join('') === '◆◇!?',
    [look.ok, look.partial, look.bad, look.limit].map((x) => x?.glyph).join(''))
  const [accent, waiting, failed, dim, done] = await Promise.all(['--accent', '--st-waiting', '--st-failed', '--text-dim', '--st-done']
    .map((t) => colorOf(page, t)))
  check('颜色令牌：有依据 --accent、部分 --st-waiting、不支持 --st-failed、未裁判 --text-dim',
    look.ok?.color === accent && look.partial?.color === waiting && look.bad?.color === failed && look.limit?.color === dim,
    JSON.stringify({ got: [look.ok?.color, look.partial?.color, look.bad?.color, look.limit?.color], want: [accent, waiting, failed, dim] }))
  check('概率性的判定一个都不用确定性的绿', [look.ok, look.partial, look.bad, look.limit].every((x) => x && x.color !== done), done)
  check('徽标是原生 button type=button、上标、不画线', [look.ok, look.bad].every((x) => x?.tag === 'BUTTON' && x.type === 'button'
    && x.align === 'super' && x.deco === 'none'), JSON.stringify(look.ok))
  check('aria-label 写全：句子和判定', look.bad?.label === '结论句「增长主要来自新客首单，老客复购持平。」，模型判断：证据不支持', look.bad?.label)
  check('……未裁判（到上限）的说「已达上限」', look.limit?.label?.endsWith('未裁判：已达上限'), look.limit?.label)
  const titles = await page.evaluate(({ a, b, c }) => [document.querySelector(a)?.getAttribute('title') ?? '',
    document.querySelector(b)?.getAttribute('title') ?? '', document.querySelector(c)?.getAttribute('title') ?? ''],
  { a: `${J} [data-ev-claim="${u.bad}"]`, b: `${JX} [data-ev-claim="${fxj.explore.units.ok}"]`, c: `${J} [data-ev-claim="${u.limit}"]` })
  check('悬停说明：模型判过的写明谁判的、非确定', titles[0] === `模型判断：证据不支持（${fxj.model} · 非确定）`, titles[0])
  check('……还没有模型判过的（探索运行按需）不说谁判的，只说还没判', !titles[1].includes('非确定') && titles[1].startsWith('未裁判'),
    titles[1])
  check('……到上限没判的（记着裁判模型）也不写成「（模型 · 非确定）」，说已达上限、本句未经裁判',
    titles[2].startsWith('未裁判：已达上限') && !titles[2].includes('非确定') && !titles[2].includes(fxj.model), titles[2])
  const shade = await page.evaluate(({ sel, bad, ok }) => {
    const box = document.querySelector(`${sel} [data-ev-claim-shade="${bad}"]`)
    const other = document.querySelector(`${sel} [data-ev-claim-shade="${ok}"]`)
    const probe = document.createElement('span')
    probe.style.background = 'var(--st-failed-soft)'
    document.body.appendChild(probe)
    const soft = getComputedStyle(probe).backgroundColor
    probe.remove()
    if (!box) return { found: false }
    const texts = [...box.querySelectorAll('span')].map((el) => getComputedStyle(el).textDecorationLine)
    return { found: true, bg: getComputedStyle(box).backgroundColor, soft, deco: getComputedStyle(box).textDecorationLine,
             texts, other: !!other, text: box.textContent }
  }, { sel: J, bad: u.bad, ok: u.ok })
  check('证据不支持的整句铺浅底（--st-failed-soft）', shade.found && shade.bg === shade.soft && shade.bg !== 'rgba(0, 0, 0, 0)',
    JSON.stringify(shade))
  check('……只有不支持的那句铺底', shade.found && !shade.other)
  check('整句的文字不画下划线（浅底里的字、别的结论句的字都没有线）', shade.found && shade.deco === 'none'
    && shade.texts.every((d) => d === 'none'), JSON.stringify(shade.texts))
  const plainText = await page.evaluate(({ sel }) => {
    const b = document.querySelector(`${sel} [data-ev-claim]`)
    const sentence = b?.previousElementSibling
    return sentence ? getComputedStyle(sentence).textDecorationLine : ''
  }, { sel: J })
  check('……有依据的那句同样不画线（只有数字自己的线）', plainText === 'none' || plainText === '', plainText)

  // 键盘：徽标在 roving 顺序里，句尾就是它；n / N 跳到证据不支持、部分有依据的句子
  const stops = await page.locator(`${J} [data-seg][tabindex="0"], ${J} [data-ev-claim][tabindex="0"]`).count()
  check('整份报告仍只有一个 Tab 位（徽标也在 roving 里）', stops === 1, String(stops))
  const firstNum = fxj.formal.doc.blocks.flatMap((b) => b.units).find((x) => x.id === u.ok).segments.filter((s) => s.kind === 'number').at(-1).id
  await page.locator(`${J} [data-seg="${firstNum}"]`).focus()
  await page.keyboard.press('ArrowRight')
  check('→ 从句子最后一个片段走到句末徽标', await activeKey(page) === `claim:${u.ok}`, await activeKey(page))
  const jumps = []
  for (const key of ['n', 'n', 'N']) {
    await page.keyboard.press(key)
    jumps.push(await activeKey(page))
  }
  check('n 跳到证据不支持、再到部分有依据的句子，N 回来', jumps.join(',') === `claim:${u.bad},claim:${u.partial},claim:${u.bad}`, jumps.join(','))
  check('只是走动不打开面板', await panel(page).count() === 0)
  await page.keyboard.press('Enter')
  await page.waitForSelector('[data-evidence-panel] [data-ev-judge]', { timeout: 4000 })
  check('回车打开这一句的「模型的解释」', (await panel(page).locator('[data-ev-judge]').getAttribute('data-ev-judge')) === 'unsupported')
  check('徽标标着展开了面板', await badge(page, J, u.bad).getAttribute('aria-expanded') === 'true')
  check('读屏从 live region 听到这一句的判定', (await page.locator(`${J} [aria-live="polite"]`).innerText()).includes('模型判断：证据不支持'))
  await page.keyboard.press('Escape')
  await page.waitForTimeout(150)
  check('Esc 关掉，焦点回到徽标', await panel(page).count() === 0 && await activeKey(page) === `claim:${u.bad}`, await activeKey(page))
  const tally = await page.locator(`${J} [data-evidence-claims]`).innerText().catch(() => '')
  check('证据条接着数结论句，和数字那一段同一种说法', tally.includes('结论 4 句（有依据 1 · 部分有依据 1 · 证据不支持 1 · 未裁判 1）'), tally)
  const summary = await page.locator(`${J} [data-evidence-summary]`).innerText()
  check('读屏摘要也说结论句（写明是模型判断）', summary.includes('结论 4 句') && summary.includes('模型判断'), summary)
  check('对照：一期的报告（没有判定）一个徽标都不挂', await page.locator(`${wide} [data-ev-claim], ${wide} [data-ev-claim-shade]`).count() === 0)
})

await section('j-panel', '四期：面板里「模型的解释」、「请模型判断这句」只在探索运行没判过时出现、触顶、封存后追加', async () => {
  const r = await open('/ui-harness.html?evidence=1')
  const pg = r.page
  const p = pg.locator('[data-evidence-panel]')
  const u = fxj.formal.units
  await badge(pg, J, u.bad).click()
  await pg.waitForSelector('[data-evidence-panel] [data-ev-judge="unsupported"]')
  const chip = await p.locator('[data-ev-judge-badge]').innerText().catch(() => '')
  check('带「模型判断 · 模型名 · 非确定」徽标', chip.trim() === `模型判断 · ${fxj.model} · 非确定`, chip)
  const why = await p.locator('[data-ev-rationale]').evaluate((el) => ({ text: el.textContent, style: getComputedStyle(el).fontStyle }))
    .catch(() => null)
  check('理由用斜体', why?.style === 'italic' && why.text.includes('看不出新客、老客'), JSON.stringify(why))
  const head = await p.locator('header [data-ev-badge]').evaluate((el) => ({ code: el.getAttribute('data-ev-badge'),
    style: getComputedStyle(el).borderTopStyle, text: el.textContent })).catch(() => null)
  check('面板标题的徽标：证据不支持、虚线框（概率性）', head?.code === 'unsupported' && head.style === 'dashed', JSON.stringify(head))
  check('正式运行节点里判的：不写「封存后追加」，写明随报告封存', await p.locator('[data-ev-post-seal]').count() === 0
    && (await p.locator('[data-ev-judge-sealed]').innerText().catch(() => '')).includes('与报告一起封存'))
  check('正式运行：没有「请模型判断本句」', await p.locator('[data-ev-judge-ask]').count() === 0)
  const cites = await p.locator('[data-ev-cites]').innerText().catch(() => '')
  check('整句的面板列出挂的依据', cites.includes('Q1') && cites.includes('db_query__shop'), cites)
  await badge(pg, J, u.limit).click()
  await pg.waitForSelector('[data-evidence-panel] [data-ev-judge-limit]')
  const lim = await p.locator('[data-ev-judge-limit]').innerText()
  check('到上限没判：写明「已达上限」和是哪个上限', lim.includes('已达上限') && lim.includes('这份报告的裁判金额上限 $0.05'), lim)
  check('……写明怎么调（正式运行：报告撰写节点的结论句裁判，可以设为不限）', lim.includes('报告撰写节点') && lim.includes('设为不限'), lim)
  check('……正式运行也不给按钮', await p.locator('[data-ev-judge-ask]').count() === 0)
  // 到上限没判的句子也记着裁判模型（judge 字段），但模型没看过它：不能画成「模型判断 · 模型名 · 非确定」
  await pg.waitForSelector('[data-evidence-panel] [data-ev-seal-state="done"]', { timeout: 3000 }).catch(() => {})
  check('……模型没看过这句：不挂「模型判断 · 模型名 · 非确定」', await p.locator('[data-ev-judge-badge]').count() === 0)
  check('……也不说这条判断「与报告一起封存」', await p.locator('[data-ev-judge-sealed]').count() === 0)
  const limSeal = await p.locator('[data-ev-seal]').innerText().catch(() => '')
  check('……封存那一节只说报告文档封存了', limSeal.includes('报告文档已封存') && !limSeal.includes('这条判断随报告一起封存'), limSeal)
  const limModel = await p.locator('[data-ev-judge-model]').innerText().catch(() => '')
  check('……中性地说一句裁判模型是谁、未裁判本句', limModel.includes(fxj.model) && limModel.includes('未裁判本句'), limModel)
  // 点开结论句里的一个数字：片段面板里也有「模型的解释」（只对结论句）
  const num = fxj.formal.doc.blocks.flatMap((b) => b.units).find((x) => x.id === u.ok).segments.find((s) => s.kind === 'number').id
  await pg.locator(`${J} [data-seg="${num}"]`).click()
  await pg.waitForSelector('[data-evidence-panel] [data-ev-judge="supported"]')
  check('结论句里的片段：面板里有这一句的模型的解释', (await p.locator('[data-ev-verdict]').innerText()).includes('模型判断：有依据'))
  // 一期的报告：没有判定、没开裁判、不知道运行类别也取不到——不画这一节
  await pg.locator(`${wide} [data-seg="s8"]`).click()
  await pg.waitForSelector('[data-evidence-panel] [data-ev-substituted]')
  await pg.waitForTimeout(300)
  check('对照：没有判定的老报告不画「模型的解释」', await p.locator('[data-ev-judge]').count() === 0)
  await pg.keyboard.press('Escape')

  // 探索运行、开了裁判（按需）：还没判过的句子给按钮
  const x = fxj.explore.units
  await badge(pg, JX, x.bad).click()
  await pg.waitForSelector('[data-evidence-panel] [data-ev-judge-ask]')
  check('探索运行、还没判过：有「请模型判断本句」', (await p.locator('[data-ev-judge-ask]').innerText()).includes('请模型判断本句'))
  check('……还没判过：没有模型徽标（还没有模型判过）', await p.locator('[data-ev-judge-badge]').count() === 0)
  await p.locator('[data-ev-judge-ask]').click()
  await pg.waitForSelector('[data-evidence-panel] [data-ev-judge="unsupported"]').catch(() => {})
  const sent = r.hits.filter((h) => h.startsWith(`judge:${fxj.explore.run_id}:`))
  check('请求体带着句子和报告节点', sent.length === 1 && sent[0].endsWith(`:${x.bad}:write`), sent.join(' | '))
  check('判完：写「封存后追加」', (await p.locator('[data-ev-post-seal]').innerText().catch(() => '')).trim() === '封存后追加')
  check('……答复没带话（判成了）：不多写一行', await p.locator('[data-ev-judge-message]').count() === 0)
  check('……封存那一节也写明封存后追加（报告文档仍是封存的）', (await p.locator('[data-ev-seal-late]').innerText().catch(() => '')).includes('封存后追加')
    && (await p.locator('[data-ev-seal]').innerText()).includes('报告文档已封存'))
  check('……判过了就不再给按钮', await p.locator('[data-ev-judge-ask]').count() === 0)
  const after = await badge(pg, JX, x.bad).evaluate((el) => ({ v: el.getAttribute('data-ev-verdict'), late: el.hasAttribute('data-ev-post-seal') }))
  check('正文的句末徽标跟着变：证据不支持、标着封存后追加', after.v === 'unsupported' && after.late, JSON.stringify(after))
  check('同一份报告在窄栏里的那份也跟着变（按运行和报告记）',
    await badge(pg, '#evidence-judge-explore-narrow', x.bad).getAttribute('data-ev-verdict') === 'unsupported')
  check('证据条的结论句计数跟着变', (await pg.locator(`${JX} [data-evidence-claims]`).innerText()).includes('证据不支持 1'))
  await badge(pg, JX, x.bad).click()
  await pg.waitForTimeout(100)
  await badge(pg, JX, x.bad).click()
  await pg.waitForSelector('[data-evidence-panel] [data-ev-judge="unsupported"]')
  check('同一句再点开：直接用已有的判定，不再请求', r.hits.filter((h) => h.startsWith(`judge:${fxj.explore.run_id}:`)).length === 1)
  // 触顶：不报错，说明「已达上限」和怎么调（每次点击的上限），还能再点一次
  await badge(pg, JX, x.limit).click()
  await pg.waitForSelector('[data-evidence-panel] [data-ev-judge-ask]')
  await p.locator('[data-ev-judge-ask]').click()
  await pg.waitForSelector('[data-evidence-panel] [data-ev-judge-limit]')
  const top = await p.locator('[data-ev-judge-limit]').innerText()
  check('按需裁判触顶：写「已达上限」和这次点击的上限', top.includes('已达上限') && top.includes('这次点击的裁判金额上限 $0.01'), top)
  check('……写明怎么调：设置里「每次点击的金额上限」、可以设为不限', top.includes('每次点击的金额上限') && top.includes('设为不限')
    && await p.locator('[data-ev-judge-settings]').count() === 1, top)
  check('……不是报错', await p.locator('[data-ev-judge-error]').count() === 0)
  check('……还能再次请模型判断', (await p.locator('[data-ev-judge-ask]').innerText().catch(() => '')).includes('再次请模型判断'))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await pg.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await badge(pg, JX, x.bad).click()
      await pg.waitForTimeout(100)
      if (await p.count() === 0) await badge(pg, JX, x.bad).click()
      await pg.waitForSelector('[data-evidence-panel] [data-ev-judge="unsupported"]')
      await pg.waitForTimeout(250)
      await pg.screenshot({ path: `${SHOTS}/evidence4-explore-judged-${theme}.png` })
      await badge(pg, J, u.limit).click()
      await pg.waitForSelector('[data-evidence-panel] [data-ev-judge-limit]')
      await pg.waitForTimeout(250)
      await pg.screenshot({ path: `${SHOTS}/evidence4-formal-limit-${theme}.png` })
      await pg.keyboard.press('Escape')
    }
    await pg.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }

  // 没开裁判的探索运行：文档里不记运行类别，面板按运行 id 取一次；结论句照样能请模型判断
  const pu = fxj.plain.doc.blocks.flatMap((b) => b.units).find((x2) => x2.id === 'u3')
  const pnum = pu.segments.find((s) => s.kind === 'number').id
  await pg.locator(`${JP} [data-seg="${pnum}"]`).click()
  await pg.waitForSelector('[data-evidence-panel] [data-ev-judge-ask]').catch(() => {})
  check('没开裁判的报告：按运行 id 认出是探索运行，给按钮', r.hits.includes(`run:${fxj.plain.run_id}`)
    && await p.locator('[data-ev-judge-ask]').count() === 1)
  check('……判之前正文没有徽标', await pg.locator(`${JP} [data-ev-claim]`).count() === 0)
  await p.locator('[data-ev-judge-ask]').click().catch(() => {})
  await pg.waitForSelector(`${JP} [data-ev-claim="u3"]`).catch(() => {})
  check('……判完正文挂上徽标（封存后追加）', await badge(pg, JP, 'u3').getAttribute('data-ev-verdict') === 'supported'
    && await badge(pg, JP, 'u3').getAttribute('data-ev-post-seal') === '')
  const ptally = await pg.locator(`${JP} [data-evidence-claims]`).innerText().catch(() => '')
  check('……证据条多出结论句：挂了依据没判的算未裁判、没挂的算无证据', ptally.includes('结论 5 句（有依据 1 · 未裁判 3 · 无证据 1）'), ptally)
  check('没有运行时报错（裁判面板）', r.errors.length === 0, r.errors.join(' | '))
  await r.ctx.close()

  // 片段接口说暂时判不了（运行还没跑完封存）：不给按钮，照原话说为什么
  const un = await open('/ui-harness.html?evidence=1', { segPatch: (set, body) => (set === fxj.explore
    ? { ...body, unit: { ...body.unit, on_demand: { available: false, reason: 'unsealed', message: '运行尚未结束并封存（仍在运行、停在人工审批或正在继续运行），请在运行结束后再请模型裁判' } } } : body) })
  const tnum = fxj.explore.doc.blocks.flatMap((b) => b.units).find((x2) => x2.id === x.ok).segments.find((s) => s.kind === 'number').id
  await un.page.locator(`${JX} [data-seg="${tnum}"]`).click()
  await un.page.waitForSelector('[data-evidence-panel] [data-ev-judge]')
  await un.page.waitForTimeout(300)
  check('接口说运行还没封存：不给按钮，照原话说', await un.page.locator('[data-ev-judge-ask]').count() === 0
    && (await un.page.locator('[data-ev-judge-blocked]').innerText()).includes('请在运行结束后再请模型裁判'))
  await un.ctx.close()
  // 接口 409（正式运行、封存被改过）：不白屏，照后端原话说
  const e409 = await open('/ui-harness.html?evidence=1', { judge: () => ({ status: 409, json: { detail: '封存核对未通过：封存后有事件被修改、删除或插入，报告和证据已不可信，无法再请模型裁判', code: 'evidence_seal_broken' } }) })
  await badge(e409.page, JX, x.bad).click()
  await e409.page.locator('[data-ev-judge-ask]').click()
  await e409.page.waitForSelector('[data-evidence-panel] [data-ev-judge-error]')
  check('按需裁判回 409：照后端原话说，不白屏', (await e409.page.locator('[data-ev-judge-error]').innerText()).includes('封存核对未通过'))
  check('……徽标还是未裁判', await badge(e409.page, JX, x.bad).getAttribute('data-ev-verdict') === 'unjudged')
  await e409.ctx.close()
  // 答复里的那句话（message）：判定照样交回来，但运行已经接着跑了、没记进运行记录——得照原话写出来，
  // 不能只挂一枚「封存后追加」让人以为记进去了。触顶的那句已经由上限那一框说了，不重复
  const lostText = '运行已继续执行，本次裁判结果未写入运行记录，刷新后将不再显示。请在运行结束并封存后重新裁判'
  const lost = await open('/ui-harness.html?evidence=1', { judge: (set, body) => {
    const unit = body?.units?.[0]
    if (set === fxj.explore && unit === x.limit) {
      return { json: { ...fxj.explore.judge.limit, message: '已达上限（这次点击的裁判金额上限 $0.01）：1 句未裁判，已裁判的结果保留' } }
    }
    return { json: { report: { node_id: 'write', doc_artifact: set.doc_artifact }, model: fxj.model, limits_hit: [], post_seal: true,
      message: lostText, event: null, calls: 1,
      verdicts: { [unit]: { status: 'supported', rationale: '引用的证据支持这句话', judge: fxj.model, post_seal: true, used: [] } } } }
  } })
  const lp = lost.page.locator('[data-evidence-panel]')
  await badge(lost.page, JX, x.ok).click()
  await lp.locator('[data-ev-judge-ask]').click().catch(() => {})
  await lost.page.waitForSelector('[data-evidence-panel] [data-ev-judge="supported"]').catch(() => {})
  const lostMsg = await lp.locator('[data-ev-judge-message]').innerText().catch(() => '')
  check('答复带着「未写入运行记录」：照原话写在判定下面', lostMsg.includes('未写入运行记录') && lostMsg.includes('刷新后将不再显示'), lostMsg || '（没写）')
  check('……判定照样显示（有依据）', (await lp.locator('[data-ev-verdict]').innerText().catch(() => '')).includes('模型判断：有依据'))
  await badge(lost.page, JX, x.limit).click()
  await lp.locator('[data-ev-judge-ask]').click().catch(() => {})
  await lost.page.waitForSelector('[data-evidence-panel] [data-ev-judge-limit]').catch(() => {})
  check('触顶那句话已经由上限那一框说了：不再另写一遍', await lp.locator('[data-ev-judge-limit]').count() === 1
    && await lp.locator('[data-ev-judge-message]').count() === 0)
  check('没有运行时报错（答复里的那句话）', lost.errors.length === 0, lost.errors.join(' | '))
  await lost.ctx.close()
})

await section('j-narrow', '四期在 360px：徽标和「模型的解释」栏内展开不横向滚动；减少动效', async () => {
  const narrow = '#evidence-judge-narrow'
  const overflow = () => page.evaluate((sel) => {
    const el = document.querySelector(sel)
    return { sw: el.scrollWidth, cw: el.clientWidth, w: el.getBoundingClientRect().width }
  }, narrow)
  const before = await overflow()
  await badge(page, narrow, fxj.formal.units.bad).click()
  await page.waitForSelector(`${narrow} [data-evidence-panel="inline"] [data-ev-judge]`)
  const after = await overflow()
  check('窄栏里徽标照样挂在句末', await page.locator(`${narrow} [data-ev-claim]`).count() === 4)
  check('窄栏里没有横向滚动（面板打开前后）', Math.round(before.w) === 360 && before.sw <= before.cw && after.sw <= after.cw,
    JSON.stringify({ before, after }))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(200)
      await page.locator(narrow).screenshot({ path: `${SHOTS}/evidence4-360-inline-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
  await page.locator(`${narrow} [data-evidence-panel] button`, { hasText: '回到正文' }).click()
  await page.waitForTimeout(150)
  const small = await open('/ui-harness.html?evidence=1', { w: 360, h: 780 })
  const scroll = () => small.page.evaluate(() => ({ sw: document.documentElement.scrollWidth, vw: innerWidth }))
  const a = await scroll()
  await badge(small.page, J, fxj.formal.units.bad).click()
  await small.page.waitForSelector('[data-evidence-panel] [data-ev-judge]')
  const b = await scroll()
  check('360px 宽的屏幕：整句的面板从底部抽出、整页没有横向滚动',
    await small.page.locator('[data-evidence-panel]').getAttribute('data-evidence-panel') === 'drawer' && a.sw <= a.vw && b.sw <= b.vw,
    JSON.stringify({ a, b }))
  await small.ctx.close()
  const r = await open('/ui-harness.html?evidence=1', { reduced: true })
  await badge(r.page, J, fxj.formal.units.bad).click()
  await r.page.waitForSelector('[data-evidence-panel] [data-ev-judge]').catch(() => {})
  const still = await r.page.evaluate(() => {
    const p = document.querySelector('[data-evidence-panel]')
    return { n: p ? p.getAnimations({ subtree: true })
      .filter((a2) => !(a2 instanceof CSSTransition && Number(a2.effect?.getTiming().duration) <= 0.01)).length : -1,
             name: p ? getComputedStyle(p).animationName : '' }
  })
  check('减少动效时整句的面板没有任何动画', still.n === 0 && still.name === 'none', JSON.stringify(still))
  await r.ctx.close()
})

await section('j-banner', '四期：出具横幅多一段「结论 N 句（支持 a · 无证据 b）」', async () => {
  const at = (key) => page.locator(`#issuance-claims [data-issuance-case="${key}"]`)
  const judged = await at('judged').locator('[data-evidence-line]').innerText().catch(() => '')
  check('结论句那一段：和数字那一段同一种说法', judged.includes('3 个数字都有出处') && judged.includes('结论 4 句（有依据 1 · 部分有依据 1 · 证据不支持 1 · 未裁判 1）'), judged)
  check('有证据不支持的结论句：给「定位下一处」', await at('judged').locator('[data-evidence-next]').count() === 1)
  await page.evaluate(() => { window.__claimsNext = 0 })
  await at('judged').locator('[data-evidence-next]').click()
  check('……点了就去找下一处', await page.evaluate(() => window.__claimsNext) === 1)
  const cited = await at('cited').locator('[data-evidence-line]').innerText().catch(() => '')
  check('方案的例子：结论 4 句（有依据 3 · 无证据 1）', cited.includes('结论 4 句（有依据 3 · 无证据 1）'), cited)
  check('……没附依据的算进「无证据」，不再另起「未附依据的结论句」', await at('cited').locator('[data-evidence-line-claims]').count() === 0)
  const all = at('all').locator('[data-evidence-claims]')
  const [color, done] = [await all.evaluate((el) => getComputedStyle(el).color).catch(() => ''), await colorOf(page, '--st-done')]
  check('全都支持：不给「定位下一处」，也不用确定性的绿', await at('all').locator('[data-evidence-next]').count() === 0 && !!color && color !== done,
    `${color} / ${done}`)
  check('对照：三期那一份（没有判定）照旧写「未附依据的结论句」', await page.locator('#issuance-line [data-issuance-case="suspect"] [data-evidence-line-claims]').count() === 1)
  // 开了裁判的文档：没挂依据的那句送了裁判、按判定数进了「支持 3」，出具照样按没挂依据记 1 句——不能被吞掉
  const ju = await at('judge-uncited').locator('[data-evidence-line]').innerText().catch(() => '')
  check('开了裁判、出具还记着未附依据的结论句：结论句那一段旁边另起「未附依据的结论句 1」',
    ju.includes('结论 3 句（有依据 3）') && await at('judge-uncited').locator('[data-evidence-line-claims]').count() === 1
    && ju.includes('· 未附依据的结论句 1'), ju)
  const juTitle = await at('judge-uncited').locator('[data-evidence-line-claims]').getAttribute('title').catch(() => '')
  check('……悬停说明：判定不能替代依据，出具照样计缺口', !!juTitle && juTitle.includes('不能替代依据') && juTitle.includes('缺口'), juTitle ?? '')
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(200)
      await page.locator('#issuance-claims').screenshot({ path: `${SHOTS}/evidence4-banner-${theme}.png` })
      await page.locator(J).screenshot({ path: `${SHOTS}/evidence4-doc-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
})

await section('j-stream', '四期：问数据页、画布右栏的横幅（Output 这条路）——按需判过的结论句数进横幅，开了裁判的报告没挂依据的照样写出来', async () => {
  // 按需裁判把「退款 29 单，其中大多数是部分退款」判成证据不支持（这句有数字，点得开）
  const pu = fxj.plain.doc.blocks.flatMap((b) => b.units).find((x2) => x2.id === 'u3')
  const pnum = pu.segments.find((x2) => x2.kind === 'number').id
  const s = await open('/preview.html?syn=evidence-judge', { judge: (set, body) => (set === fxj.plain ? { json: {
    report: { node_id: 'write', doc_artifact: set.doc_artifact }, model: fxj.model, limits_hit: [], post_seal: true,
    verdicts: { [body.units[0]]: { status: 'unsupported', rationale: 'Q2 里部分退款 11 单，说不上「大多数」', judge: fxj.model, post_seal: true, used: ['Q2'] } },
  } } : judgeReply(set, body)) })
  const p = s.page
  const turn = (id) => p.locator(`[data-turn="${id}"]`)
  await p.waitForSelector('[data-turn="judge-plain"] [data-evidence-line]', { timeout: 6000 }).catch(() => {})
  await p.waitForSelector('[data-turn="judge-formal"] [data-evidence-line]', { timeout: 6000 }).catch(() => {})
  const plainLine = () => turn('judge-plain').locator('[data-evidence-line]').innerText().catch(() => '')
  const before = await plainLine()
  check('没开裁判、还没判过：横幅照旧写「未附依据的结论句 1」，没有结论句那一段',
    before.includes('未附依据的结论句 1') && await turn('judge-plain').locator('[data-evidence-claims]').count() === 0, before)
  // 点开那一句的数字，请模型判断（Output 没给运行类别：按运行 id 认出探索运行）
  await turn('judge-plain').locator(`[data-seg="${pnum}"]`).click().catch(() => {})
  const ask = p.locator('[data-evidence-panel] [data-ev-judge-ask]')
  await ask.waitFor({ timeout: 6000 }).catch(() => {})
  await ask.click().catch(() => {})
  await p.waitForSelector('[data-turn="judge-plain"] [data-ev-claim="u3"]', { timeout: 6000 }).catch(() => {})
  await p.waitForSelector('[data-turn="judge-plain"] [data-evidence-line] [data-evidence-claims]', { timeout: 3000 }).catch(() => {})
  check('……请模型判断那一下发出去了（按运行、报告、句子）', s.hits.includes(`judge:${fxj.plain.run_id}:u3:write`),
    s.hits.filter((h) => h.startsWith('judge:')).join(' '))
  const claims = await turn('judge-plain').locator('[data-evidence-line] [data-evidence-claims]').innerText().catch(() => '')
  check('按需判过一句：横幅多出结论句那一段（EvidenceField 叠好判定交出的文档，docTally 照样数）',
    claims.includes('结论 5 句（证据不支持 1 · 未裁判 3 · 无证据 1）'), claims || '（没有这一段）')
  const after = await plainLine()
  check('……没挂依据的那句已经数在「无证据」里，不再另起一句', !!claims && await turn('judge-plain').locator('[data-evidence-line-claims]').count() === 0, after)
  check('……有证据不支持的句子：横幅给「定位下一处」', !!claims && await turn('judge-plain').locator('[data-evidence-line] [data-evidence-next]').count() === 1, after)
  await p.keyboard.press('Escape')
  const formal = await turn('judge-formal').locator('[data-evidence-line]').innerText().catch(() => '')
  check('开了裁判的正式运行：结论句那一段照节点里的判定数', formal.includes('结论 4 句（有依据 1 · 部分有依据 1 · 证据不支持 1 · 未裁判 1）'), formal)
  check('……没附依据的那句（送了裁判、到上限没判）出具照样计缺口：另起「未附依据的结论句 1」',
    await turn('judge-formal').locator('[data-evidence-line-claims]').count() === 1 && formal.includes('未附依据的结论句 1'), formal)
  check('没有运行时报错（问数据页 · 结论句裁判）', s.errors.length === 0, s.errors.join(' | '))
  if (SHOTS) {
    // 侧面板还开着会压住横幅的右半边：先关掉
    await p.locator('[data-evidence-panel] button[aria-label="关闭证据"]').click().catch(() => {})
    for (const theme of ['dark', 'light']) {
      await p.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await p.waitForTimeout(200)
      await turn('judge-plain').locator('[data-issuance-banner]').screenshot({ path: `${SHOTS}/evidence4-stream-plain-${theme}.png` })
      await turn('judge-formal').locator('[data-issuance-banner]').screenshot({ path: `${SHOTS}/evidence4-stream-formal-${theme}.png` })
    }
  }
  await s.ctx.close()
})

// ---------------------------------------------------------------------------
// 裁判拆档（JR）：证据相矛盾 / 证据不足、缺什么、裁判模型的两条提醒、理由 120 字、字段清单、讲方法的句子
// ---------------------------------------------------------------------------

const V = '#evidence-verdict'
const VN = '#evidence-verdict-narrow'
const VX = '#evidence-verdict-explore'
/** 一组句末徽标的样子：判定、字形、计算色、读屏名、悬停说明，外加整句的浅底（没有浅底是 null） */
const badgeLook = (pg, sel, units) => pg.evaluate(({ sel, units }) => Object.fromEntries(Object.entries(units).map(([k, id]) => {
  const el = document.querySelector(`${sel} [data-ev-claim="${id}"]`)
  if (!el) return [k, null]
  const shade = document.querySelector(`${sel} [data-ev-claim-shade="${id}"]`)
  return [k, { verdict: el.getAttribute('data-ev-verdict'), glyph: el.textContent, color: getComputedStyle(el).color,
               label: el.getAttribute('aria-label'), title: el.getAttribute('title'),
               shade: shade ? getComputedStyle(shade).backgroundColor : null }]
})), { sel, units })
const softOf = (pg, token) => tokenColor(pg, 'backgroundColor', `var(${token})`)
const firstNumber = (doc, uid) => doc.blocks.flatMap((b) => b.units).find((x) => x.id === uid).segments.find((s) => s.kind === 'number').id

await section('jr', '裁判拆档：证据相矛盾、证据不足四通道分得开，证据不足不用红、不和未裁判混；n / N 先走相矛盾、再走证据不足', async () => {
  const u = fxv.formal.units
  await page.waitForSelector(`${V} [data-ev-claim="${u.insuff}"]`, { timeout: 4000 }).catch(() => {})
  const look = await badgeLook(page, V, u)
  check('新取值各挂各的徽标：证据相矛盾、证据不足（讲方法的那句也判成证据相矛盾）',
    look.contra?.verdict === 'contradicted' && look.insuff?.verdict === 'insufficient' && look.method?.verdict === 'contradicted',
    JSON.stringify(Object.fromEntries(Object.entries(look).map(([k, v]) => [k, v?.verdict]))))
  check('字形：证据相矛盾 !、证据不足 ○，和未裁判的 ? 分得开', look.contra?.glyph === '!' && look.insuff?.glyph === '○'
    && look.limit?.glyph === '?' && look.insuff.glyph !== look.limit.glyph,
    [look.contra?.glyph, look.insuff?.glyph, look.limit?.glyph].join(' '))
  const [failed, waiting, neutral, done] = await Promise.all(['--st-failed', '--st-waiting', '--st-cancelled', '--st-done']
    .map((t) => colorOf(page, t)))
  check('颜色令牌：证据相矛盾 --st-failed（同原来的证据不支持），证据不足 --st-cancelled（中性）',
    look.contra?.color === failed && look.method?.color === failed && look.insuff?.color === neutral,
    JSON.stringify({ got: [look.contra?.color, look.insuff?.color], want: [failed, neutral] }))
  check('证据不足不用红，也不用提醒色和确定性的绿', !!look.insuff && ![failed, waiting, done].includes(look.insuff.color), look.insuff?.color)
  const soft = await softOf(page, '--st-failed-soft')
  check('证据相矛盾的整句铺浅底（--st-failed-soft）', look.contra?.shade === soft && look.method?.shade === soft && soft !== 'rgba(0, 0, 0, 0)',
    JSON.stringify({ contra: look.contra?.shade, method: look.method?.shade, soft }))
  check('证据不足不铺底（中性样式）', look.insuff?.shade === null, String(look.insuff?.shade))
  check('读屏名：证据相矛盾', look.contra?.label === '结论句「各渠道的订单数相加与总订单数一致。」，模型判断：证据相矛盾', look.contra?.label)
  check('读屏名：证据不足接着说缺什么', look.insuff?.label === `结论句「增长主要来自新客首单，老客复购持平。」，模型判断：证据不足，缺少：${fxv.missing}`,
    look.insuff?.label)
  check('悬停说明：证据不足写明谁判的、非确定、缺什么', look.insuff?.title === `模型判断：证据不足（${fxv.model} · 非确定）。缺少：${fxv.missing}`,
    look.insuff?.title)
  const tally = await page.locator(`${V} [data-evidence-claims]`).innerText().catch(() => '')
  check('证据条的结论句计数带上证据相矛盾、证据不足', tally.includes('结论 6 句（有依据 1 · 部分有依据 1 · 证据相矛盾 2 · 证据不足 1 · 未裁判 1）'), tally)
  const summary = await page.locator(`${V} [data-evidence-summary]`).innerText()
  check('读屏摘要的按键说明：证据有问题的句子 n 也跳到，证据不足的排在最后', summary.includes('证据有问题的句子（证据不足的排在最后）'), summary)

  // n / N：第 1 轮是证据相矛盾、部分有依据（按正文顺序），第 2 轮是证据不足——它在正文里排第二，照样最后才到
  await page.locator(`${V} [data-seg="${fxv.formal.segs.gmv}"]`).focus()
  const jumps = []
  for (let i = 0; i < 5; i++) {
    await page.keyboard.press('n')
    jumps.push(await activeKey(page))
  }
  const want = [u.contra, u.partial, u.method, u.insuff, u.contra].map((x) => `claim:${x}`)
  check('n：先按正文顺序走证据相矛盾、部分有依据，再走证据不足，走完接回开头', jumps.join(',') === want.join(','), jumps.join(','))
  const back = []
  for (let i = 0; i < 2; i++) {
    await page.keyboard.press('N')
    back.push(await activeKey(page))
  }
  check('N：倒着走，从第一处退到证据不足（最后一处），再退到第 1 轮的最后一处', back.join(',') === `claim:${u.insuff},claim:${u.method}`,
    back.join(','))
  check('只是走动不打开面板', await panel(page).count() === 0)

  // 拆档之前的取值：探索运行证据图里的 unsupported 照旧显示「证据不支持」，样式同证据相矛盾
  const x = fxv.explore.units
  await page.waitForSelector(`${VX} [data-ev-claim="${x.contra}"][data-ev-verdict="unsupported"]`, { timeout: 4000 }).catch(() => {})
  const old = (await badgeLook(page, VX, { contra: x.contra, insuff: x.insuff }))
  check('旧取值 unsupported：照旧叫「证据不支持」（读屏名、悬停说明）', old.contra?.verdict === 'unsupported'
    && old.contra.label?.endsWith('，模型判断：证据不支持，封存后追加') && old.contra.title?.startsWith('模型判断：证据不支持 · 封存后追加'),
    JSON.stringify(old.contra))
  check('……样式同证据相矛盾：!、--st-failed、整句浅底', old.contra?.glyph === '!' && old.contra.color === failed && old.contra.shade === soft,
    JSON.stringify(old.contra))
  check('……封存后追加的证据不足同样是 ○、中性色', old.insuff?.verdict === 'insufficient' && old.insuff.glyph === '○' && old.insuff.color === neutral,
    JSON.stringify(old.insuff))
  const xt = await page.locator(`${VX} [data-evidence-claims]`).innerText().catch(() => '')
  check('……计数里旧取值照样算进去，和证据不足并列', xt.includes('结论 7 句（证据不支持 1 · 证据不足 1 · 未裁判 5）'), xt)
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(200)
      await page.locator(V).screenshot({ path: `${SHOTS}/jr-doc-${theme}.png` })
      await page.locator(VX).screenshot({ path: `${SHOTS}/jr-doc-explore-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
})

await section('jr-panel', '裁判拆档：面板写明缺什么、裁判模型的两条提醒、理由 120 字不截断、字段清单、讲方法的句子直接看到 SQL 和字段清单', async () => {
  const r = await open('/ui-harness.html?evidence=1')
  const pg = r.page
  const p = pg.locator('[data-evidence-panel]')
  const u = fxv.formal.units
  const [failed, waiting, neutral] = await Promise.all(['--st-failed', '--st-waiting', '--st-cancelled'].map((t) => colorOf(pg, t)))
  await badge(pg, V, u.insuff).click()
  await pg.waitForSelector('[data-evidence-panel] [data-ev-judge="insufficient"]')
  check('面板：证据不足（字形 ○）', (await p.locator('[data-ev-verdict]').innerText()).trim() === '○模型判断：证据不足',
    await p.locator('[data-ev-verdict]').innerText())
  const head = await p.locator('header [data-ev-badge]').evaluate((el) => ({ code: el.getAttribute('data-ev-badge'), color: getComputedStyle(el).color }))
    .catch(() => null)
  check('……面板标题的徽标是中性色，不用红', head?.code === 'insufficient' && head.color === neutral && head.color !== failed, JSON.stringify(head))
  const missing = await p.locator('[data-ev-judge] [data-ev-missing]').innerText().catch(() => '')
  check('……「模型的解释」里写明缺什么', missing === `缺少：${fxv.missing}`, missing)
  const same = await p.locator('[data-ev-judge] [data-ev-judge-same]').innerText().catch(() => '')
  const unpriced = await p.locator('[data-ev-judge] [data-ev-judge-unpriced]').innerText().catch(() => '')
  check('裁判模型和写作模型相同：「模型的解释」下写「裁判模型和写作模型相同，结果仅供参考」', same === '裁判模型和写作模型相同，结果仅供参考', same)
  check('模型不在价格表里：写「该模型不在价格表中，金额上限不生效」', unpriced === '该模型不在价格表中，金额上限不生效', unpriced)
  const caution = await p.locator('[data-ev-judge-caution]').evaluate((el) => {
    const cs = getComputedStyle(el)
    return { border: cs.borderLeftColor, width: cs.borderLeftWidth, style: cs.borderLeftStyle, color: cs.color, bg: cs.backgroundColor,
             icon: getComputedStyle(el.querySelector('svg')).color }
  }).catch(() => null)
  check('……醒目但不刺眼：左侧一道 2px 提醒色竖线、图标用提醒色，字不用红、不铺底', !!caution && caution.border === waiting
    && caution.width === '2px' && caution.style === 'solid' && caution.icon === waiting && caution.color !== failed
    && caution.bg === 'rgba(0, 0, 0, 0)', JSON.stringify(caution))
  const sameTitle = await p.locator('[data-ev-judge-same]').getAttribute('title').catch(() => '')
  check('……悬停写明是哪个模型', (sameTitle ?? '').includes(fxv.model), sameTitle ?? '')
  // 没判的句子、别的报告不挂
  await badge(pg, V, u.limit).click()
  await pg.waitForSelector('[data-evidence-panel] [data-ev-judge="unjudged"]')
  check('到上限没判的句子不挂这两条提醒（模型没看过这句）', await p.locator('[data-ev-judge-caution]').count() === 0)
  await badge(pg, J, fxj.formal.units.bad).click()
  await pg.waitForSelector('[data-evidence-panel] [data-ev-judge="unsupported"]')
  check('对照：裁判模型和写作模型不同、模型在价格表里的报告不挂提醒', await p.locator('[data-ev-judge-caution]').count() === 0)
  check('……旧取值的面板照旧写「证据不支持」', (await p.locator('[data-ev-verdict]').innerText()).includes('模型判断：证据不支持'))
  // 理由最长 120 字：整句显示，侧边面板里折行、不溢出
  await badge(pg, V, u.contra).click()
  await pg.waitForSelector('[data-evidence-panel] [data-ev-judge="contradicted"]')
  const why = await p.locator('[data-ev-rationale]').evaluate((el) => {
    const cs = getComputedStyle(el)
    return { text: el.textContent, sw: el.scrollWidth, cw: el.clientWidth, ellipsis: cs.textOverflow, clamp: cs.webkitLineClamp,
             overflow: cs.overflow, lines: Math.round(el.getBoundingClientRect().height / parseFloat(cs.lineHeight)) }
  }).catch(() => null)
  check('理由 120 字整句显示，不截断', why?.text === fxv.long_rationale && [...fxv.long_rationale].length === 120, why?.text)
  check('……在侧边面板里折成几行、不溢出、不加省略号', !!why && why.sw <= why.cw && why.ellipsis !== 'ellipsis'
    && (why.clamp === 'none' || why.clamp === '') && why.lines >= 3, JSON.stringify(why))
  check('证据相矛盾：面板写「模型判断：证据相矛盾」，不写缺什么', (await p.locator('[data-ev-verdict]').innerText()).trim() === '!模型判断：证据相矛盾'
    && await p.locator('[data-ev-missing]').count() === 0)
  const orders = fxv.formal.segments[fxv.formal.segs.orders].chain.find((c) => c.step === 'entity')
  const wantFields = orders.fields.map((f) => [f.name, f.type])
  // 讲方法的句子：「挂的依据」下直接列出 SQL 和字段清单（先于点开句中的表名：取数只能来自这一块）
  const hitsBefore = r.hits.length
  check('……此前没取过句中表名的片段', !r.hits.includes(`j:${fxv.formal.segs.orders}`))
  await badge(pg, V, u.method).click()
  await pg.waitForSelector('[data-evidence-panel] [data-ev-cites] [data-ev-basis]')
  // SQL、字段清单没画出来时也往下走：让下面几条断言自己报没通过
  await pg.waitForSelector('[data-evidence-panel] [data-ev-cites] [data-ev-basis] [data-ev-sql]', { timeout: 3000 }).catch(() => {})
  await pg.waitForSelector('[data-evidence-panel] [data-ev-basis-table] [data-ev-field]', { timeout: 3000 }).catch(() => {})
  const basis = p.locator('[data-ev-cites] [data-ev-basis]')
  const sqls = await basis.locator('[data-ev-basis-sql]').evaluateAll((els) => els.map((e) =>
    [e.getAttribute('data-ev-basis-sql'), e.querySelector('[data-ev-sql]')?.textContent ?? '']))
  check('讲方法的句子：「挂的依据」下直接列出 SQL（直接挂的 Q1，再补表名出现过的 Q2）',
    JSON.stringify(sqls) === JSON.stringify([['Q1', fxv.snapshots.Q1.content.sql], ['Q2', fxv.snapshots.Q2.content.sql]]), JSON.stringify(sqls))
  const bf = await basis.locator('[data-ev-basis-table="t:orders"] [data-ev-field]').evaluateAll((els) => els.map((e) => e.getAttribute('data-ev-field')))
  check('……表的字段清单也直接列出', JSON.stringify(bf) === JSON.stringify(wantFields.map(([n]) => n)), JSON.stringify(bf))
  const bhead = await basis.locator('[data-ev-basis-table="t:orders"]').innerText().catch(() => '')
  check('……写明是哪张表的字段清单', bhead.startsWith(`表 orders 的字段清单（${wantFields.length} 个）`), bhead.split('\n')[0])
  const bc = await basis.locator('[data-ev-basis-column]').allInnerTexts()
  check('……挂的字段写类型', bc.some((t) => t.includes('orders.status') && t.includes('VARCHAR(12)'))
    && bc.some((t) => t.includes('orders.created_at') && t.includes('TIMESTAMP')), bc.join(' | '))
  check('……SQL 带复制', await basis.locator('[data-ev-basis-sql="Q1"] button', { hasText: '复制 SQL' }).count() === 1)
  // 这一页只有「挂的依据」会取查询快照工件（完整快照要点按钮）；这之前没点开过句中的表名，字段清单那次片段请求只能是
  // 它自己发的。Q1、Q2 的 SQL 在前面几句（只挂了整份查询）打开时已经取过，缓存着不再取
  const fresh = r.hits.slice(hitsBefore)
  check('……SQL 取自查询快照工件，字段清单取自表名片段的证据链', r.hits.includes('artifact:V:Q1') && r.hits.includes('artifact:V:Q2')
    && fresh.includes(`j:${fxv.formal.segs.orders}`), fresh.join(','))
  check('……判定的理由里提到的 created_at 在字段清单里看得到', bf.includes('created_at'))
  await badge(pg, V, u.insuff).click()
  await pg.waitForSelector('[data-evidence-panel] [data-ev-judge="insufficient"]')
  await pg.waitForSelector('[data-evidence-panel] [data-ev-basis] [data-ev-sql]').catch(() => {})
  const insuffSql = await p.locator('[data-ev-basis] [data-ev-basis-sql]').evaluateAll((els) => els.map((e) => e.getAttribute('data-ev-basis-sql')))
  check('只挂了整份查询的句子（证据不足的那句）：同样列出 SQL，读的人看得出缺的是什么', insuffSql.join(',') === 'Q1', insuffSql.join(','))
  await badge(pg, V, u.ok).click()
  await pg.waitForSelector('[data-evidence-panel] [data-ev-judge="supported"]')
  await pg.waitForTimeout(200)
  check('对照：挂了数字的结论句只列依据的编号，不另列 SQL', await p.locator('[data-ev-basis]').count() === 0
    && (await p.locator('[data-ev-cites]').innerText()).includes('Q1'))
  // 表名实体步骤：字段清单和类型
  await pg.locator(`${V} [data-seg="${fxv.formal.segs.orders}"]`).click()
  await pg.waitForSelector('[data-evidence-panel] [data-ev-entity] [data-ev-entity-queries]')
  // 字段清单没画出来时也往下走：让下面两条断言自己报没通过，而不是整节在等待上出错
  await pg.waitForSelector('[data-evidence-panel] [data-ev-entity] [data-ev-fields]', { timeout: 3000 }).catch(() => {})
  const fields = await p.locator('[data-ev-entity] [data-ev-field]').evaluateAll((els) => els.map((e) =>
    [e.getAttribute('data-ev-field'), e.querySelector('[data-ev-field-type]')?.textContent ?? '']))
  check('表名实体步骤列出字段清单和类型（照证据接口的 fields，顺序不变）', JSON.stringify(fields) === JSON.stringify(wantFields), JSON.stringify(fields))
  const fhead = await p.locator('[data-ev-entity] [data-ev-fields]').innerText().catch(() => '')
  check('……写明一共几个字段；没有多出来的不写「另有」', fhead.startsWith(`字段清单（${wantFields.length} 个）`)
    && await p.locator('[data-ev-fields-more]').count() === 0, fhead.split('\n')[0])
  check('没有运行时报错（裁判拆档 · 面板）', r.errors.length === 0, r.errors.join(' | '))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await pg.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      for (const [name, unit] of [['insufficient', u.insuff], ['contradicted', u.contra], ['method', u.method]]) {
        await badge(pg, V, unit).click()
        await pg.waitForTimeout(100)
        if (await p.count() === 0) await badge(pg, V, unit).click()
        await pg.waitForSelector('[data-evidence-panel] [data-ev-judge]')
        await pg.waitForTimeout(300)
        await pg.screenshot({ path: `${SHOTS}/jr-panel-${name}-${theme}.png` })
      }
      await pg.locator(`${V} [data-seg="${fxv.formal.segs.orders}"]`).click()
      await pg.waitForSelector('[data-evidence-panel] [data-ev-fields]')
      await pg.waitForTimeout(300)
      await pg.screenshot({ path: `${SHOTS}/jr-panel-fields-${theme}.png` })
      await pg.keyboard.press('Escape')
    }
    await pg.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
  await r.ctx.close()

  // 字段多于 60 个、表结构快照只存了部分表
  const more = await open('/ui-harness.html?evidence=1', { segPatch: (set, body) => (set === fxv.formal && body?.segment?.id === fxv.formal.segs.orders
    ? { ...body, chain: body.chain.map((c) => (c.step === 'entity' ? { ...c, fields: c.fields.slice(0, 5), fields_more: 55, snapshot_truncated: true,
      fields_masked: true } : c)) }
    : body) })
  const mp = more.page.locator('[data-evidence-panel]')
  await more.page.locator(`${V} [data-seg="${fxv.formal.segs.orders}"]`).click()
  await more.page.waitForSelector('[data-evidence-panel] [data-ev-fields-more]').catch(() => {})
  check('字段多于 60 个：列出的之外写「另有 N 个」', (await mp.locator('[data-ev-fields-more]').innerText().catch(() => '')) === '另有 55 个字段未列出',
    await mp.locator('[data-ev-fields-more]').innerText().catch(() => '（没写）'))
  check('……总数按列出的加另有的算', (await mp.locator('[data-ev-entity] [data-ev-fields]').innerText().catch(() => '')).startsWith('字段清单（60 个）')
    && await mp.locator('[data-ev-entity] [data-ev-field]').count() === 5)
  check('有列按遮罩没列出（fields_masked）：照实说一句', (await mp.locator('[data-ev-entity] [data-ev-fields-masked]').innerText().catch(() => ''))
    === '部分字段已按数据源的遮罩设置隐藏，未列出')
  await badge(more.page, V, u.method).click()
  await more.page.waitForSelector('[data-evidence-panel] [data-ev-basis-table] [data-ev-fields-partial]').catch(() => {})
  check('讲方法的句子：表结构快照只存了部分表时，字段清单下照实注明', (await mp.locator('[data-ev-basis-table] [data-ev-fields-partial]')
    .innerText().catch(() => '')).includes('表结构快照仅包含部分表'))
  check('……另有的字段也写出来', (await mp.locator('[data-ev-basis-table] [data-ev-fields-more]').innerText().catch(() => '')) === '另有 55 个字段未列出')
  await more.ctx.close()

  // 探索运行：封存后追加的证据不足、按需裁判答复里的 same_model / priced
  const x = fxv.explore.units
  const ex = await open('/ui-harness.html?evidence=1')
  const xp = ex.page.locator('[data-evidence-panel]')
  await ex.page.waitForSelector(`${VX} [data-ev-claim="${x.insuff}"][data-ev-verdict="insufficient"]`, { timeout: 4000 }).catch(() => {})
  await badge(ex.page, VX, x.insuff).click()
  await ex.page.waitForSelector('[data-evidence-panel] [data-ev-judge="insufficient"]')
  await ex.page.waitForTimeout(300)
  check('封存后追加的证据不足：同样写明缺什么', (await xp.locator('[data-ev-missing]').innerText().catch(() => '')) === `缺少：${fxv.missing}`)
  check('……证据图里最近一次按需裁判的模型和写作模型不同、在价格表里：不挂提醒', await xp.locator('[data-ev-judge-caution]').count() === 0)
  await badge(ex.page, VX, x.method).click()
  await xp.locator('[data-ev-judge-ask]').click()
  await ex.page.waitForSelector('[data-evidence-panel] [data-ev-judge="contradicted"]').catch(() => {})
  check('按需裁判回来：讲方法的那句判成证据相矛盾，正文徽标跟着变', await badge(ex.page, VX, x.method).getAttribute('data-ev-verdict') === 'contradicted')
  check('……这一次的答复说裁判模型和写作模型相同、不在价格表里：两条提醒都挂上',
    (await xp.locator('[data-ev-judge-same]').innerText().catch(() => '')) === '裁判模型和写作模型相同，结果仅供参考'
    && (await xp.locator('[data-ev-judge-unpriced]').innerText().catch(() => '')) === '该模型不在价格表中，金额上限不生效')
  await ex.page.keyboard.press('Escape')
  await ex.page.waitForTimeout(150)
  await ex.page.locator(`${VX} [data-seg="${firstNumber(fxv.explore.doc, x.ok)}"]`).focus()
  const xj = []
  for (let i = 0; i < 3; i++) {
    await ex.page.keyboard.press('n')
    xj.push(await activeKey(ex.page))
  }
  check('n：旧的证据不支持、证据相矛盾在前，证据不足在后（它在正文里更靠前也一样）',
    xj.join(',') === [x.contra, x.method, x.insuff].map((k) => `claim:${k}`).join(','), xj.join(','))
  // 封存后追加的旧判定（拆档之前的 unsupported）：片段接口给 on_demand.reason = outdated、available 为真——照常给按钮，
  // 旧判定旁边注明「按旧规则判定」；按当前规则重判之后注明和按钮都收起
  const outdatedMsg = fxv.explore.segments[fxv.explore.doc.blocks.flatMap((b) => b.units).find((u2) => u2.id === x.contra)
    .segments.find((sg) => sg.kind !== 'structural').id].unit.on_demand
  // 面板可能还开着、正停在这一句上（上面 n 跟着走过来）：点一下是收起，没开出来就再点一次
  await badge(ex.page, VX, x.contra).click()
  await ex.page.waitForSelector('[data-evidence-panel] [data-ev-judge="unsupported"]', { timeout: 1500 }).catch(() => {})
  if (await xp.locator('[data-ev-judge="unsupported"]').count() === 0) await badge(ex.page, VX, x.contra).click()
  await ex.page.waitForSelector('[data-evidence-panel] [data-ev-judge="unsupported"]', { timeout: 4000 }).catch(() => {})
  await ex.page.waitForSelector('[data-evidence-panel] [data-ev-judge-outdated]').catch(() => {})
  const chip = xp.locator('[data-ev-judge-outdated]')
  check('旧判定（片段接口说 outdated）：旁边注明「按旧规则判定」，悬停是接口的原话', outdatedMsg?.reason === 'outdated'
    && (await chip.innerText().catch(() => '')) === '按旧规则判定' && await chip.getAttribute('title').catch(() => '') === outdatedMsg.message,
    JSON.stringify(outdatedMsg))
  check('……照常给按钮（再次请模型判断），按钮旁写明为什么', (await xp.locator('[data-ev-judge-ask]').innerText().catch(() => '')).includes('再次请模型判断')
    && (await xp.locator('[data-ev-judge-outdated-why]').innerText().catch(() => '')) === outdatedMsg.message)
  await xp.locator('[data-ev-judge-ask]').click().catch(() => {})
  await ex.page.waitForSelector('[data-evidence-panel] [data-ev-judge="contradicted"]').catch(() => {})
  check('……按当前规则重判：判成证据相矛盾，「按旧规则判定」和按钮都收起', await badge(ex.page, VX, x.contra).getAttribute('data-ev-verdict') === 'contradicted'
    && await xp.locator('[data-ev-judge-outdated]').count() === 0 && await xp.locator('[data-ev-judge-ask]').count() === 0
    && ex.hits.some((h) => h === `judge:${fxv.explore.run_id}:${x.contra}:write`), ex.hits.filter((h) => h.startsWith('judge:')).join(' '))
  if (SHOTS) {
    // 重新开一页截「按旧规则判定」那一刻（上面已经重判掉了）
    const shot = await open('/ui-harness.html?evidence=1')
    await shot.page.waitForSelector(`${VX} [data-ev-claim="${x.contra}"][data-ev-verdict="unsupported"]`).catch(() => {})
    for (const theme of ['dark', 'light']) {
      await shot.page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await badge(shot.page, VX, x.contra).click()
      await shot.page.waitForTimeout(100)
      if (await shot.page.locator('[data-evidence-panel]').count() === 0) await badge(shot.page, VX, x.contra).click()
      await shot.page.waitForSelector('[data-evidence-panel] [data-ev-judge-outdated]').catch(() => {})
      await shot.page.waitForTimeout(300)
      await shot.page.screenshot({ path: `${SHOTS}/jr-panel-outdated-${theme}.png` })
    }
    await shot.ctx.close()
  }
  check('没有运行时报错（裁判拆档 · 探索运行）', ex.errors.length === 0, ex.errors.join(' | '))
  await ex.ctx.close()
})

await section('jr-narrow', '裁判拆档在 360px：理由 120 字、字段清单、SQL 栏内展开不横向滚动；底部抽屉整页不横向滚动', async () => {
  const overflow = () => page.evaluate((sel) => {
    const el = document.querySelector(sel)
    return { sw: el.scrollWidth, cw: el.clientWidth, w: el.getBoundingClientRect().width }
  }, VN)
  const before = await overflow()
  await badge(page, VN, fxv.formal.units.contra).click()
  await page.waitForSelector(`${VN} [data-evidence-panel="inline"] [data-ev-rationale]`)
  const why = await page.locator(`${VN} [data-ev-rationale]`).evaluate((el) => ({ text: el.textContent, sw: el.scrollWidth, cw: el.clientWidth }))
  const after = await overflow()
  check('窄栏里理由 120 字整句显示、在栏内折行', why.text === fxv.long_rationale && why.sw <= why.cw, JSON.stringify({ sw: why.sw, cw: why.cw }))
  check('……面板打开前后都没有横向滚动', Math.round(before.w) === 360 && before.sw <= before.cw && after.sw <= after.cw, JSON.stringify({ before, after }))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(200)
      await page.locator(VN).screenshot({ path: `${SHOTS}/jr-360-rationale-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
  await badge(page, VN, fxv.formal.units.method).click()
  await page.waitForSelector(`${VN} [data-evidence-panel="inline"] [data-ev-basis] [data-ev-sql]`).catch(() => {})
  await page.waitForSelector(`${VN} [data-evidence-panel="inline"] [data-ev-basis-table] [data-ev-field]`).catch(() => {})
  const basis = await overflow()
  const pre = await page.locator(`${VN} [data-ev-basis] [data-ev-sql]`).first().evaluate((el) => ({ sw: el.scrollWidth, cw: el.clientWidth }))
    .catch(() => null)
  check('窄栏里讲方法的句子：SQL、字段清单都在栏内，不横向滚动', basis.sw <= basis.cw && !!pre && pre.sw <= pre.cw
    && await page.locator(`${VN} [data-ev-basis-table] [data-ev-field]`).count() > 0, JSON.stringify({ basis, pre }))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(200)
      await page.locator(VN).screenshot({ path: `${SHOTS}/jr-360-basis-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
  await page.locator(`${VN} [data-evidence-panel] button`, { hasText: '回到正文' }).click()
  await page.waitForTimeout(150)
  const small = await open('/ui-harness.html?evidence=1', { w: 360, h: 780 })
  const scroll = () => small.page.evaluate(() => ({ sw: document.documentElement.scrollWidth, vw: innerWidth }))
  await badge(small.page, V, fxv.formal.units.contra).click()
  await small.page.waitForSelector('[data-evidence-panel] [data-ev-rationale]')
  const d = await scroll()
  const dwhy = await small.page.locator('[data-evidence-panel] [data-ev-rationale]').evaluate((el) => ({ text: el.textContent, sw: el.scrollWidth, cw: el.clientWidth }))
  check('360px 宽的屏幕：底部抽屉里理由整句显示、整页没有横向滚动',
    await small.page.locator('[data-evidence-panel]').getAttribute('data-evidence-panel') === 'drawer' && d.sw <= d.vw
    && dwhy.text === fxv.long_rationale && dwhy.sw <= dwhy.cw, JSON.stringify({ d, sw: dwhy.sw, cw: dwhy.cw }))
  await small.ctx.close()
})

await section('jr-banner', '裁判拆档：出具横幅的结论句计数带上证据相矛盾、证据不足和旧的证据不支持', async () => {
  const at = (key) => page.locator(`#issuance-claims [data-issuance-case="${key}"]`)
  const rework = await at('rework').locator('[data-evidence-line]').innerText().catch(() => '')
  check('计数：证据相矛盾、旧的证据不支持、证据不足各写各的', rework.includes('结论 7 句（有依据 1 · 部分有依据 1 · 证据相矛盾 2 · 证据不支持 1 · 证据不足 1 · 未裁判 1）'),
    rework)
  const [color, waiting] = [await at('rework').locator('[data-evidence-claims]').evaluate((el) => getComputedStyle(el).color).catch(() => ''),
    await colorOf(page, '--st-waiting')]
  check('……有问题的计数用提醒色', color === waiting, `${color} / ${waiting}`)
  check('只有证据不足一种问题：照样给「定位下一处」', await at('insufficient').locator('[data-evidence-next]').count() === 1)
  const only = await at('insufficient').locator('[data-evidence-claims]').evaluate((el) => getComputedStyle(el).color).catch(() => '')
  check('……计数也用提醒色（证据不足也算问题）', only === waiting, only)
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(200)
      await page.locator('#issuance-claims').screenshot({ path: `${SHOTS}/jr-banner-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
})

// ---------------------------------------------------------------------------
// Excel 导入期 4：推断的来源（夹具 evidence-provenance.json，P4-SPEC 4.4）
// ---------------------------------------------------------------------------
const PV = '#evidence-provenance'
const PVN = '#evidence-provenance-narrow'
/** 场景的片段 id：按夹具 cases 里记的片段文字认，重新生成夹具后编号挪了也不用改 */
const pseg = (name) => segOf(fxp.doc, fxp.cases[name].text)
/** 这个场景的片段请求过几次推断来源 */
const provHits = (hits, name) => hits.filter((h) => h === `prov:${fxp.run_id}:${pseg(name)}`).length
/** 点开一个场景，等推断的来源那一节定下来（不再是「正在推断」）；不该有这一节的场景改等查询步骤、指标步骤 */
async function openP(p, name, { box = PV, wait = '[data-ev-provenance]:not([data-ev-provenance="loading"])' } = {}) {
  await p.locator(`${box} [data-seg="${pseg(name)}"]`).click()
  await p.waitForSelector(`[data-evidence-panel] ${wait}`, { timeout: 4000 }).catch(() => {})
  await p.waitForTimeout(120)
}
const pv = (p, sel = '') => p.locator(`[data-evidence-panel] ${sel}`)
/** 元素的文字（空白合成一个空格）；没有这个元素时是空串 */
const textOf = async (loc) => ((await loc.count()) ? (await loc.first().innerText()).replace(/\s+/g, ' ').trim() : '')
const fromOf = (p) => p.locator('[data-evidence-panel] [data-ev-provenance] [data-ev-prov-from] li').allInnerTexts()
const partsOf = (p) => p.locator('[data-evidence-panel] [data-ev-prov-version] [data-ev-prov-part]')
  .evaluateAll((els) => els.map((e) => `${e.getAttribute('data-ev-prov-part')}${e.hasAttribute('data-ev-prov-has-row') ? '*' : ''}`))
/** 只给表级来历的场景：table_only、一句原因、没有格子和相关核对、数据版本照样有 */
async function tableOnly(p, name, want, what) {
  await openP(p, name)
  const reason = await textOf(pv(p, '[data-ev-provenance="table_only"] [data-ev-prov-reason]'))
  check(what, (typeof want === 'string' ? reason === want : want.test(reason))
    && await pv(p, '[data-ev-prov-cell], [data-ev-prov-checks], [data-ev-prov-alert]').count() === 0
    && await pv(p, '[data-ev-prov-version]').count() === 1, reason)
}
const noSections = async (p) => (await pv(p, '[data-ev-provenance], [data-ev-prov-version], [data-ev-prov-checks]').count()) === 0
const activeHas = (p, attr) => p.evaluate((a) => document.activeElement?.hasAttribute(a) ?? false, attr)

await section('provenance', '期 4：推断的来源——数据版本、格子与逐条、相关核对，只在上传表格的单元格上请求', async () => {
  const ids = Object.fromEntries(Object.keys(fxp.cases).map((k) => [k, pseg(k)]))
  check('夹具的场景都能按片段文字认出来（和 cases 记的编号一致）', Object.entries(fxp.cases).every(([k, c]) => ids[k] === c.segment),
    JSON.stringify(ids))
  // 前面各节点开的都是期 4 之前的报告、没有提示的查询步骤：一个推断来源请求都不该有（4.4 第 14 条的全局版）
  const stray = harness.hits.filter((h) => h.startsWith('prov:'))
  check('其他夹具（期 4 之前的报告）点开过的片段一个推断来源请求都没发', stray.length === 0, stray.join(','))

  // ---- 1. 宽表的一格：格子、逐条、相关核对、位置、标题
  await openP(page, 'wide')
  const sec = pv(page, '[data-ev-provenance="inferred"]')
  check('满足条件：有「推断的来源」一节（inferred）', await sec.count() === 1, String(await pv(page, '[data-ev-provenance]').getAttribute('data-ev-provenance').catch(() => null)))
  check('……标题「推断的来源」，旁边标「推断，不属于已封存的证据」', await textOf(sec.locator('h4')) === '推断的来源'
    && await textOf(sec.locator('[data-ev-prov-badge]')) === '推断，不属于已封存的证据')
  const cell = await textOf(sec.locator('[data-ev-prov-cell]'))
  check('……第一行醒目地写格子：工作表「客流汇总」G5', cell === '工作表「客流汇总」G5', cell)
  const items = await fromOf(page)
  check('……逐条：日期取自表头格 G4 → 年份取自统计期 → 行标签 B5（指标名）', items.join('|')
    === '日期取自表头格 G4|年份取自统计期（客流汇总!B2）|行标签 B5「全日客流（人次）」', items.join('|'))
  check('相关核对：R1「这一行成立」', (await textOf(pv(page, '[data-ev-prov-checks] [data-ev-prov-check="R1"]'))).includes('这一行成立'))
  // 规格 4.1 的散文写「口径说明：…」；R2 的标题本身是「口径不同：…」，再套一层冒号读不顺，所以和别的核对同一种
  // 写法「R2 标题 — 口径说明」（口径说明是 4.2 partStatus.info 的原文）。改写法要连同 CheckRow 一起改
  check('……口径不同（R2）只写口径说明，不判行', (await textOf(pv(page, '[data-ev-prov-check="R2"]'))).endsWith('— 口径说明')
    && await pv(page, '[data-ev-prov-check="R2"][data-ev-prov-row]').count() === 0)
  const order = await page.evaluate(() => {
    const p = document.querySelector('[data-evidence-panel]')
    const q = p?.querySelector('[data-ev-query]')
    const seq = ['[data-ev-prov-version]', '[data-ev-provenance]', '[data-ev-prov-checks]', '[data-ev-judge]', '[data-ev-seal]']
      .map((s) => p?.querySelector(s)).filter(Boolean)
    if (!q || seq.length < 4) return `缺：${seq.length}`
    let prev = q
    for (const el of seq) {
      if (!(prev.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)) return `顺序不对：${el.outerHTML.slice(0, 60)}`
      prev = el
    }
    return 'ok'
  })
  check('三节在查询步骤之后、结论句裁判和封存之前：数据版本 → 推断的来源 → 相关核对', order === 'ok', order)
  const heads = await Promise.all(['数据版本', '推断的来源', '相关核对']
    .map((name) => panel(page).getByRole('heading', { name, exact: true }).evaluateAll((els) => els.map((e) => e.tagName).join(','))))
  check('三节的标题都是真正的标题（getByRole heading，h4）', heads.every((t) => t === 'H4'), heads.join(' | '))
  check('推断的来源用虚线左边框，和其余几节区分', await sec.evaluate((el) => getComputedStyle(el).borderLeftStyle) === 'dashed')
  check('相关核对也挂「推断」标签；数据版本来自封存链上的清单，不挂', await pv(page, '[data-ev-prov-checks] [data-ev-prov-badge]').count() === 1
    && await pv(page, '[data-ev-prov-version] [data-ev-prov-badge]').count() === 0)
  check('封存那一行照旧只按片段接口的链算：「已封存 · 核对一致」', (await textOf(pv(page, '[data-ev-seal]'))).includes('已封存 · 核对一致'))
  const integrityBase = await pv(page, '[data-ev-integrity]').count()

  const ver = pv(page, '[data-ev-prov-version]')
  const vtext = await textOf(ver)
  const p1 = fxp.provenance[ids.wide].version.parts[0]
  check('数据版本：首行「数据源「flow_demo」· 每期替换」（每期替换不写期数）', await textOf(ver.locator('[data-ev-prov-head]')) === '数据源「flow_demo」· 每期替换',
    vtext)
  check('……每期一行：第几次导入 · 统计期；文件名、sha256 前 12 位、区域、原件状态、配方第几版',
    vtext.includes('第 1 次导入 · 统计期 2026-08-01 至 2026-08-31') && vtext.includes(`文件「${p1.file_name}」· sha256 ${p1.raw_sha256.slice(0, 12)}`)
    && !vtext.includes(p1.raw_sha256.slice(0, 13)) && vtext.includes('区域 客流汇总!B4:AG30') && vtext.includes('原件已保存')
    && vtext.includes('配方第 1 版') && vtext.includes('涉及的表：日客流'), vtext)
  check('……这一格所在的一期有标记；已封存的运行不写「尚未封存」', (await partsOf(page)).join(',') === '1*'
    && vtext.includes('这一格所在的一期') && await ver.locator('[data-ev-prov-unsealed]').count() === 0)

  const det = sec.locator('[data-ev-prov-details]')
  check('末尾一行小字「已按主键回查数据文件，值一致」，回查 SQL 收在「技术细节」里（默认收起）',
    await textOf(sec.locator('[data-ev-prov-recheck]')) === '已按主键回查数据文件，值一致'
    && await det.getAttribute('aria-expanded') === 'false' && await sec.locator('[data-ev-prov-tech]').isHidden()
    && await det.getAttribute('aria-controls') === await sec.locator('[data-ev-prov-tech]').getAttribute('id'))
  await det.focus()
  await page.keyboard.press('Enter')
  const tech = await textOf(sec.locator('[data-ev-prov-tech]'))
  check('……「技术细节」是按钮，回车展开：回查 SQL 和参数，带复制', await det.getAttribute('aria-expanded') === 'true'
    && await sec.locator('[data-ev-prov-tech]').isVisible()
    && tech.includes('SELECT rowid, "全日客流" FROM "日客流" WHERE "日期" = ?') && tech.includes('2026-08-05')
    && await sec.locator('[data-ev-prov-tech] button', { hasText: '复制 SQL' }).count() === 1, tech)

  // ---- 16、28：键盘打开导入清单，对话框是结构化的清单渲染，Esc 只关对话框
  await pv(page, '[data-ev-query] [data-ev-query-head]').focus()
  let reached = false
  for (let i = 0; i < 30 && !reached; i++) {
    await page.keyboard.press('Tab')
    reached = await activeHas(page, 'data-ev-prov-manifest')
  }
  check('键盘：从查询步骤 Tab 走得到「查看导入清单」', reached)
  const arts = harness.hits.filter((h) => h.startsWith('artifact:P:')).length
  await page.keyboard.press('Enter')
  await page.waitForSelector('[role="dialog"] [data-manifest-block]', { timeout: 4000 }).catch(() => {})
  const dlg = page.locator('[role="dialog"]')
  const dtext = await dlg.innerText().catch(() => '')
  check('回车打开导入清单对话框，按清单的工件 id 经工件接口取', await dlg.count() === 1
    && harness.hits.filter((h) => h.startsWith('artifact:P:')).length === arts + 1 && harness.hits.includes(`artifact:P:${p1.manifest}`)
    && dtext.includes('第 1 次导入的导入清单'), dtext.slice(0, 80))
  check('……正文是版本页的清单渲染（结构化区块），不是整份 JSON（展开的文字里没有键名 "acceptances"）',
    await dlg.locator('[data-manifest-block]').count() >= 5 && !dtext.includes('"acceptances"') && dtext.includes(p1.file_name)
    && await dlg.locator('[data-manifest-raw]').evaluate((el) => !el.open).catch(() => false), String(await dlg.locator('[data-manifest-block]').count()))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(250)
      await page.screenshot({ path: `${SHOTS}/evidence-p4-manifest-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
  await page.keyboard.press('Escape')
  await page.waitForTimeout(200)
  check('Esc 只关清单对话框：证据面板还开着，焦点回到「查看导入清单」', await dlg.count() === 0 && await panel(page).count() === 1
    && await activeHas(page, 'data-ev-prov-manifest'))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(250)
      await panel(page).screenshot({ path: `${SHOTS}/evidence-p4-inferred-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
  await page.keyboard.press('Escape')
  await page.waitForTimeout(200)

  // ---- 18：缓存只在面板这次打开期间有效
  const c0 = provHits(harness.hits, 'wide')
  await openP(page, 'wide')
  await openP(page, 'long')
  await openP(page, 'wide')
  check('同一次打开里同一片段点两次，推断的来源只取一次', provHits(harness.hits, 'wide') === c0 + 1 && provHits(harness.hits, 'long') >= 1,
    `${c0} → ${provHits(harness.hits, 'wide')}`)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(200)
  check('……面板关掉了', await panel(page).count() === 0)
  await openP(page, 'wide')
  check('关闭面板再打开、再点同一片段：重新取（回答里有当前状态）', provHits(harness.hits, 'wide') === c0 + 2, String(provHits(harness.hits, 'wide')))

  // ---- 2、3：长表的分段标题、全角标签的规范写法、合计表
  await openP(page, 'long')
  const longItems = await fromOf(page)
  check('长表一格：分段标题 B9（按配方中的标题「…」定位），不说它是格子原文；行标签 B16「13-14」',
    longItems.includes('分段标题 B9（按配方中的标题「日间时段客流（人次）」定位）') && longItems.includes('行标签 B16「13-14」'), longItems.join('|'))
  await openP(page, 'fullwidth')
  const fw = await fromOf(page)
  check('全角标签那一期的维度列：原文「８－９」，按规范写法存为「8-9」', fw.includes('原文「８－９」，按规范写法存为「8-9」'), fw.join('|'))
  await openP(page, 'total')
  const tItems = await fromOf(page)
  check('合计表一格：合计标签 B28「18-22 时合计」（原文），口径「原表写明的合计：不要彼此相加…」', tItems.includes('合计标签 B28「18-22 时合计」')
    && tItems.includes('原表写明的合计：不要彼此相加，也不要与明细相加') && tItems.includes('原文「18-22 时合计」，按规范写法存为「18-22时合计」'),
    tItems.join('|'))
  check('……相关核对里 K1「这一格一致」', (await textOf(pv(page, '[data-ev-prov-check="K1"]'))).includes('这一格一致'))

  // ---- 4：累积的两期
  await openP(page, 'union')
  const un = fxp.provenance[ids.union]
  check('按期累积：首行写「按期累积 · 共 2 期」，两期都列出，第 2 期标「这一格所在的一期」',
    await textOf(pv(page, '[data-ev-prov-head]')) === `数据源「${un.version.source}」· 按期累积 · 共 2 期`
    && (await partsOf(page)).join(',') === '1,2*', (await partsOf(page)).join(','))
  const ufrom = await textOf(pv(page, '[data-ev-prov-from-part]'))
  check('……格子是第 2 期（9 月）文件的坐标', await textOf(pv(page, '[data-ev-prov-cell]')) === `工作表「${un.cell_source.sheet}」${un.cell_source.cell}`
    && ufrom.includes('第 2 次导入') && ufrom.includes(un.version.parts[1].file_name), ufrom)

  // ---- 5–8：只给表级来历
  await tableOnly(page, 'alias', /改了名/, '改名：table_only，原因写「改了名」，没有格子，数据版本照样有')
  await tableOnly(page, 'selfjoin', /同一张表多次/, '自连接：table_only，原因写「同一张表多次」')
  await tableOnly(page, 'null', '这一格是空值，不推断来源', '空值：table_only，「这一格是空值，不推断来源」')
  await tableOnly(page, 'aggregate', /计算、聚合/, '聚合：table_only，原因写「计算、聚合」')
  check('……聚合的格子数据版本照样列出涉及的表', (await textOf(pv(page, '[data-ev-prov-tables]'))) === '涉及的表：日客流')

  // ---- 9、10、21：标红
  const failedColor = await tokenColor(page, 'color', 'var(--st-failed)')
  const look = (code) => pv(page, `[data-ev-prov-alert="${code}"]`).evaluate((el) => {
    const cs = getComputedStyle(el)
    return { color: cs.color, border: cs.borderLeftColor, text: el.innerText }
  }).catch(() => null)
  await openP(page, 'tampered')
  const tam = await look('db_tampered')
  check('数据文件被改：标红（data-ev-prov-alert="db_tampered"），写「可能被修改过」，边框和字都是 --st-failed',
    !!tam && tam.text.includes('可能被修改过') && tam.color === failedColor && tam.border === failedColor, JSON.stringify({ tam, failedColor }))
  check('……不给格子、不写原因那一句（有 alert 时只画 alert），数据版本照样有', await pv(page, '[data-ev-prov-cell], [data-ev-prov-reason], [data-ev-prov-checks]').count() === 0
    && await pv(page, '[data-ev-prov-version]').count() === 1 && await pv(page, '[data-ev-provenance="table_only"]').count() === 1)
  check('……标红用自己的属性：面板里 data-ev-integrity 的个数和没有 alert 时相同', await pv(page, '[data-ev-integrity]').count() === integrityBase)
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(250)
      await panel(page).screenshot({ path: `${SHOTS}/evidence-p4-tampered-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
  await openP(page, 'chain')
  check('登记哈希与清单不符：table_only，标红 chain_mismatch，数据版本照样有，没有原因那一句',
    await pv(page, '[data-ev-provenance="table_only"] [data-ev-prov-alert="chain_mismatch"]').count() === 1
    && await pv(page, '[data-ev-prov-version]').count() === 1 && await pv(page, '[data-ev-prov-reason]').count() === 0)
  await openP(page, 'unreadable')
  const unr = await look('manifest_unreadable')
  check('清单无法读取：none 里只有红色的 manifest_unreadable，没有数据版本、没有原因那一句',
    await pv(page, '[data-ev-provenance="none"] [data-ev-prov-alert="manifest_unreadable"]').count() === 1 && unr?.color === failedColor
    && await pv(page, '[data-ev-prov-version], [data-ev-prov-reason], [data-ev-prov-cell], [data-ev-prov-checks]').count() === 0, JSON.stringify(unr))

  // ---- 11、12：当前状态
  await openP(page, 'purged')
  const pver = await textOf(pv(page, '[data-ev-prov-version]'))
  const purgedLine = await textOf(pv(page, '[data-ev-prov-purged]'))
  check('原件已清除：数据版本里写「原件已清除 · 清除时间 · 理由 · 署名（未认证）」，标「当前状态」',
    /^原件已清除 · \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \(UTC[+-]\d{2}:\d{2}\) · 理由：合成理由：原件不再保留 · 署名（未认证）：录入员甲/.test(purgedLine)
    && await pv(page, '[data-ev-prov-purged] [data-ev-prov-current]').count() === 1, purgedLine)
  check('……格子照样有，旁边注明「原件已清除：坐标来自导入时的记录，无法再对照原件」', await pv(page, '[data-ev-prov-cell]').count() === 1
    && await textOf(pv(page, '[data-ev-prov-raw]')) === '原件已清除：坐标来自导入时的记录，无法再对照原件')
  await openP(page, 'revoked')
  const rver = await textOf(pv(page, '[data-ev-prov-version]'))
  check('作废接受：数据版本里写「这一期的接受已作废 · 作废时间 · 理由」（当前状态），接受理由照旧列出',
    /^这一期的接受已作废 · \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \(UTC[+-]\d{2}:\d{2}\) · 理由：合成理由：接受依据有误 · 署名（未认证）：未填写/
      .test(await textOf(pv(page, '[data-ev-prov-revoked]')))
    && await pv(page, '[data-ev-prov-revoked] [data-ev-prov-current]').count() === 1
    && rver.includes('已接受：「日客流」：全日客流 = 分区甲 + 分区乙 · 合成理由：分区合计口径调整 · 署名（未认证）：未填写'), rver)

  // ---- 13：简单导入
  await openP(page, 'simple')
  check('简单导入：none 里只有一句「这份表格按表头行直接导入…」，没有数据版本',
    (await textOf(pv(page, '[data-ev-provenance="none"] [data-ev-prov-reason]'))).startsWith('这份表格按表头行直接导入')
    && await pv(page, '[data-ev-prov-version], [data-ev-prov-alert], [data-ev-prov-checks]').count() === 0)

  // ---- 15、20：不该请求的两种
  check('夹具自检：手工源那一格的查询步骤没有提示，指标片段链上的查询步骤误带了提示',
    fxp.segments[ids.manual].chain.every((st) => st.provenance !== true)
    && fxp.segments[ids.metric].chain.some((st) => st.step === 'query' && st.provenance === true))
  await openP(page, 'manual', { wait: '[data-ev-query]' })
  await page.waitForTimeout(250)
  check('手工源（查询步骤不带提示）：不请求推断的来源，三节都不画', provHits(harness.hits, 'manual') === 0 && await noSections(page))
  await openP(page, 'metric', { wait: '[data-ev-metric]' })
  await page.waitForTimeout(250)
  check('指标片段：链上的查询步骤带了提示也不请求（只认单元格引用）', provHits(harness.hits, 'metric') === 0 && await noSections(page)
    && await pv(page, '[data-ev-query]').count() >= 1)

  // ---- 22、23、25、26：回收、遮罩、未封存、本期不成立并接受
  await tableOnly(page, 'gone', /已回收或不存在/, '数据文件已回收：table_only，原因写「已回收或不存在」，不标红')
  await tableOnly(page, 'masked', '这一格涉及设置了遮罩的列，不推断来源', '遮罩：table_only，「这一格涉及设置了遮罩的列，不推断来源」')
  check('……数据源设有遮罩：数据版本里没有「查看导入清单」，改写一句去向', await pv(page, '[data-ev-prov-manifest]').count() === 0
    && await textOf(pv(page, '[data-ev-prov-manifest-masked]')) === '数据源设置了遮罩，导入清单请到数据源卡片上的「版本」查看')
  await openP(page, 'unsealed')
  check('运行未封存：照常推断，数据版本一节写「本次运行尚未封存…」，不标红', await pv(page, '[data-ev-provenance="inferred"]').count() === 1
    && (await textOf(pv(page, '[data-ev-prov-version] [data-ev-prov-unsealed]'))).startsWith('本次运行尚未封存或封存核对未通过')
    && await pv(page, '[data-ev-prov-alert]').count() === 0)
  // 接受理由挂在本期结论上（接受针对的是这一期的核对结果，不是这一行），写法取 4.2 的 accepted 原文「已接受，理由：…」，
  // 不是 4.1 散文里的「已接受：理由」；行级结果单独一行，本期结论另起一行——d26_other 要同时看得到「这一行成立」和
  // 「本期不成立 · 已接受」（4.4 第 26 条）
  await openP(page, 'd26_bad')
  const bad = await textOf(pv(page, '[data-ev-prov-check="R1"]'))
  check('不成立的那一天：R1「这一行不成立」，接着写「本期不成立 · 已接受，理由：…」', bad.includes('这一行不成立')
    && bad.includes('本期不成立 · 已接受，理由：合成理由：分区合计口径调整（署名（未认证）：未填写）'), bad)
  await openP(page, 'd26_other')
  const other = await textOf(pv(page, '[data-ev-prov-check="R1"]'))
  check('同一期的另一天：「这一行成立」，本期结论照样写「本期不成立 · 已接受」', other.includes('这一行成立') && other.includes('本期不成立 · 已接受'), other)

  // ---- 人工录入的统计期、列表
  await openP(page, 'human')
  const hItems = await fromOf(page)
  check('统计期人工录入：年份取自人工录入的统计期（署名（未认证）：录入员甲）', hItems.includes('年份取自人工录入的统计期（署名（未认证）：录入员甲）')
    && (await textOf(pv(page, '[data-ev-prov-version]'))).includes('人工录入（署名（未认证）：录入员甲）'), hItems.join('|'))
  await openP(page, 'list')
  check('列表：格子在列表头下面（列表头 F5），统计期未记录', await textOf(pv(page, '[data-ev-prov-cell]')) === '工作表「月报」F8'
    && (await fromOf(page)).includes('列表头 F5') && (await textOf(pv(page, '[data-ev-prov-version]'))).includes('第 1 次导入 · 统计期未记录'))

  // ---- 27：多于 3 期的折叠
  await openP(page, 'many')
  const more = pv(page, '[data-ev-prov-more]')
  check('四期：首行「按期累积 · 共 4 期」，只展开这一格所在的一期和最近一期，其余收进「另有 2 期」',
    (await textOf(pv(page, '[data-ev-prov-head]'))).endsWith('按期累积 · 共 4 期') && (await partsOf(page)).join(',') === '2*,4'
    && await textOf(more) === '另有 2 期' && await more.getAttribute('aria-expanded') === 'false', (await partsOf(page)).join(','))
  await more.focus()
  await page.keyboard.press('Enter')
  check('……「另有 2 期」是带 aria-expanded 的按钮：回车展开（true），四期按统计期列全，焦点留在按钮上',
    await more.getAttribute('aria-expanded') === 'true' && (await partsOf(page)).join(',') === '1,2*,3,4' && await activeHas(page, 'data-ev-prov-more'),
    (await partsOf(page)).join(','))
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(250)
      await panel(page).screenshot({ path: `${SHOTS}/evidence-p4-many-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
  await page.keyboard.press('Escape')
  await page.waitForTimeout(200)

  // ---- 17：360 宽的画布右栏
  const wrap = async () => page.evaluate((sel) => {
    const box = document.querySelector(sel)
    const p = box?.querySelector('[data-evidence-panel]')
    return { mode: p?.getAttribute('data-evidence-panel') ?? null, box: [box?.scrollWidth, box?.clientWidth], panel: p ? [p.scrollWidth, p.clientWidth] : null }
  }, PVN)
  const fits = (w) => w.mode === 'inline' && w.box[0] <= w.box[1] && !!w.panel && w.panel[0] <= w.panel[1]
  await openP(page, 'wide', { box: PVN })
  await page.locator(`${PVN} [data-ev-prov-details]`).click().catch(() => {})
  const w1 = await wrap()
  await openP(page, 'many', { box: PVN })
  await page.locator(`${PVN} [data-ev-prov-more]`).click().catch(() => {})
  const w2 = await wrap()
  await openP(page, 'tampered', { box: PVN })
  const w3 = await wrap()
  check('360 宽的画布右栏：栏内展开；宽表一格（技术细节展开）、四期（全部展开）、标红三种都不出横向滚动',
    fits(w1) && fits(w2) && fits(w3), JSON.stringify({ w1, w2, w3 }))
  if (SHOTS) {
    await openP(page, 'wide', { box: PVN })
    for (const theme of ['dark', 'light']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await page.waitForTimeout(250)
      await page.locator(PVN).screenshot({ path: `${SHOTS}/evidence-p4-360-${theme}.png` })
    }
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
  await page.locator(`${PVN} [data-evidence-panel] button`, { hasText: '回到正文' }).click().catch(() => {})
  await page.waitForTimeout(150)

  // ---- 24：加载态
  const wideId = ids.wide
  const slow = await open('/ui-harness.html?evidence=1', {
    prov: async (run, sid, reply) => {
      if (sid === wideId) await new Promise((res) => setTimeout(res, 300))
      return reply
    },
  })
  await slow.page.locator(`${PV} [data-seg="${wideId}"]`).click()
  await slow.page.waitForSelector('[data-evidence-panel] [data-ev-prov-loading]', { timeout: 4000 }).catch(() => {})
  const loading = await textOf(pv(slow.page, '[data-ev-provenance="loading"] [data-ev-prov-loading]'))
  check('取的过程中：「推断的来源」一节写「正在推断来源…」，三节不先画空壳', loading === '正在推断来源…'
    && await pv(slow.page, '[data-ev-prov-version], [data-ev-prov-checks], [data-ev-prov-cell]').count() === 0, loading)
  await slow.page.waitForSelector('[data-evidence-panel] [data-ev-provenance="inferred"]', { timeout: 4000 }).catch(() => {})
  check('……答复到了以后加载态消失，三节画出来', await pv(slow.page, '[data-ev-prov-loading]').count() === 0
    && await pv(slow.page, '[data-ev-prov-version]').count() === 1 && await pv(slow.page, '[data-ev-prov-checks]').count() === 1)
  check('没有运行时报错（加载态）', slow.errors.length === 0, slow.errors.join(' | '))
  await slow.ctx.close()

  // ---- 19：出错、老服务端。另守渲染矩阵本身：服务端误把数据版本带在 none · manifest_unreadable、table_only 的答复里
  // 时照样按矩阵画（前者不画数据版本，后者不画格子）——不能只靠夹具里那些字段恰好为空。
  // 清除、作废、导入的时间没记（契约里 at 可以是 null，夹具里恰好都有）：略去这一项，不留孤立的「—」
  const longId = ids.long
  const untimed = (ans) => ({
    ...ans,
    version: {
      ...ans.version,
      parts: ans.version.parts.map((pt) => ({
        ...pt,
        committed_at: null,
        purged: pt.purged && { ...pt.purged, at: null },
        revoked: pt.revoked && { ...pt.revoked, at: null },
      })),
    },
  })
  const sloppy = {
    [ids.unreadable]: { ...fxp.provenance[ids.unreadable], version: fxp.provenance[wideId].version },
    [ids.alias]: { ...fxp.provenance[ids.alias], cell_source: fxp.provenance[wideId].cell_source, checks: fxp.provenance[wideId].checks },
    [ids.purged]: untimed(fxp.provenance[ids.purged]),
    [ids.revoked]: untimed(fxp.provenance[ids.revoked]),
  }
  const broken = await open('/ui-harness.html?evidence=1', {
    prov: (run, sid, reply) => (sid === wideId ? fxp.errors.server : sid === longId ? fxp.errors.old_server
      : sloppy[sid] ? { json: sloppy[sid] } : reply),
  })
  await openP(broken.page, 'wide')
  check('推断来源接口回 500：「推断的来源」一节只写「未能获取推断的来源」', await textOf(pv(broken.page, '[data-ev-provenance="error"] [data-ev-prov-failed]'))
    === '未能获取推断的来源' && await pv(broken.page, '[data-ev-prov-version], [data-ev-prov-checks], [data-ev-prov-cell]').count() === 0)
  check('……其余几节照常：查询步骤、封存都在', await pv(broken.page, '[data-ev-query]').count() === 1
    && (await textOf(pv(broken.page, '[data-ev-seal]'))).includes('已封存 · 核对一致'))
  await openP(broken.page, 'long', { wait: '[data-ev-query]' })
  await broken.page.waitForTimeout(300)
  check('回 404 且没有机读码（老服务端）：请求发了，三节都不画', provHits(broken.hits, 'long') === 1 && await noSections(broken.page),
    String(provHits(broken.hits, 'long')))
  await openP(broken.page, 'unreadable')
  check('渲染矩阵：清单无法读取时，答复里哪怕带着数据版本也不画，只画红色提示',
    await pv(broken.page, '[data-ev-prov-alert="manifest_unreadable"]').count() === 1 && await pv(broken.page, '[data-ev-prov-version]').count() === 0)
  await openP(broken.page, 'alias')
  check('渲染矩阵：只给表级来历时，答复里哪怕带着格子和核对也不画', await pv(broken.page, '[data-ev-prov-reason]').count() === 1
    && await pv(broken.page, '[data-ev-prov-cell], [data-ev-prov-checks]').count() === 0)
  await openP(broken.page, 'purged')
  const purgedUntimed = await textOf(pv(broken.page, '[data-ev-prov-purged]'))
  const verUntimed = await textOf(pv(broken.page, '[data-ev-prov-version]'))
  check('清除时间、导入时间没记：写「原件已清除 · 理由：… · 署名（未认证）：…」，不留孤立的「—」，也不写「导入于」',
    /^原件已清除 · 理由：合成理由：原件不再保留 · 署名（未认证）：录入员甲/.test(purgedUntimed)
    && !verUntimed.includes('—') && !verUntimed.includes('导入于')
    && await pv(broken.page, '[data-ev-prov-purged] [data-ev-prov-current]').count() === 1, verUntimed)
  await openP(broken.page, 'revoked')
  const revokedUntimed = await textOf(pv(broken.page, '[data-ev-prov-revoked]'))
  check('作废时间没记：写「这一期的接受已作废 · 理由：… · 署名（未认证）：未填写」，不留孤立的「—」',
    /^这一期的接受已作废 · 理由：合成理由：接受依据有误 · 署名（未认证）：未填写/.test(revokedUntimed)
    && !(await textOf(pv(broken.page, '[data-ev-prov-version]'))).includes('—'), revokedUntimed)
  check('没有运行时报错（出错、老服务端）', broken.errors.length === 0, broken.errors.join(' | '))
  await broken.ctx.close()
})

if (SHOTS) {
  for (const theme of ['dark', 'light']) {
    await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
    await page.locator(`${wide} [data-seg="s8"]`).click()
    await page.waitForTimeout(350)
    await page.screenshot({ path: `${SHOTS}/evidence-harness-${theme}.png`, fullPage: true })
    await page.locator('#evidence-narrow [data-seg="s26"]').click()
    await page.waitForTimeout(350)
    await page.locator('#evidence-narrow').screenshot({ path: `${SHOTS}/evidence-360-inline-${theme}.png` })
    await page.keyboard.press('Escape')
  }
}
// ---------------------------------------------------------------------------
// 合并查询：片段接口的查询步骤带 merge（表级来历、逐格来历），后面接着追到的输入的查询步骤（merged_into）；
// 推断来源的答复带 merge（经过的每一次合并）。答复按 backend/app/api/evidence.py 的 _merge_view、_query_steps 的形状改
// ---------------------------------------------------------------------------
const MERGE_SQL = 'SELECT o.order_id, o.region, o.amount, t.target FROM o JOIN t ON o.region = t.region ORDER BY o.order_id'
// 合并输入自己的 SQL 检查（_merge_view 的 inputs[].checks，形状同查询步骤的 checks）
const INPUT_CHECKS = [{ code: 'fanout_sum', level: 'error', table: 'orders', column: 'amount',
  message: '「订单」关联「订单明细」是一对多，对「订单」的「订单金额」求和会重复计算。请先按订单汇总明细再关联。' }]
function asMerge(body, traced, { withInput = true, inputChecks = false } = {}) {
  const q = body.chain.find((st) => st.step === 'query')
  const merged = {
    ...q, tool: undefined, source: '合并查询', sql: MERGE_SQL,
    merge: {
      sql: MERGE_SQL,
      inputs: [
        { alias: 'o', node_id: 'fetch', label: '订单明细', query: 'Q1', rows: 12, source: 'shop', artifact: 'a'.repeat(64), sealed: true,
          ...(inputChecks ? { checks: INPUT_CHECKS } : {}) },
        { alias: 't', node_id: 'targets', label: '区域目标', query: null, rows: 4, source: 'plan', artifact: 'b'.repeat(64), sealed: true },
      ],
      warnings: [{ code: 'rows_grew', message: '合并结果有 14 行，多于行数最多的输入「o」（12 行）：合并键可能不唯一，同一行被重复匹配' }],
      traced,
    },
  }
  const input = { ...q, alias: 'Q1', merged_into: q.alias }
  return { ...body, chain: body.chain.flatMap((st) => (st.step === 'query' ? (withInput ? [merged, input] : [merged]) : [st])) }
}

await section('merge', '合并查询：表级来历（输入、合并 SQL、警告）、逐格来历、输入步骤标明是哪次合并的输入', async () => {
  const traced = await probeQ((id, body) => (id === qseg('1,288')
    ? asMerge(body, [{ cell: [5, 'amount'], input: 'o', query: 'Q1', row: 5, column: 'amount' }]) : body))
  await openQ(traced.page, '1,288')
  if (SHOTS) {
    for (const theme of ['dark', 'light']) {
      await traced.page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
      await traced.page.waitForTimeout(250)
      await traced.page.screenshot({ path: `${SHOTS}/evidence-merge-${theme}.png`, fullPage: true })
    }
    await traced.page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'))
  }
  const p = traced.page.locator('[data-evidence-panel]')
  const parts = p.locator('[data-ev-query]')
  check('合并步骤和它追到的输入步骤各一块', await parts.count() === 2, String(await parts.count()))
  const head = await parts.first().locator('[data-ev-query-head]').innerText().catch(() => '')
  check('合并步骤的标题写「合并查询」，不写工具名', head.includes('Q3') && head.includes('合并查询') && !head.includes('db_query__'), head)
  const inputs = p.locator('[data-ev-merge]')
  check('表级来历：合并自 2 个输入', (await inputs.innerText().catch(() => '')).includes('合并自 2 个输入'))
  const o = await p.locator('[data-ev-merge-input="o"]').innerText().catch(() => '')
  check('……每个输入写别名、目录编号、节点名、行数、数据源', ['o', '查询 Q1', '订单明细', '12 行', '数据源 shop'].every((t) => o.includes(t)), o)
  const t = await p.locator('[data-ev-merge-input="t"]').innerText().catch(() => '')
  check('……不在本报告目录里的输入照实说', t.includes('不在本报告的证据目录中'), t)
  check('合并 SQL 有标签、带复制', (await parts.first().innerText()).includes('合并 SQL')
    && await parts.first().locator('button', { hasText: '复制合并 SQL' }).count() === 1)
  const warn = await p.locator('[data-ev-merge-warnings]').innerText().catch(() => '')
  check('执行时的警告写出来', warn.includes('执行时的警告') && warn.includes('不唯一'), warn)
  const line = await p.locator('[data-ev-merge-trace="cell"]').innerText().catch(() => '')
  check('逐格来历：被引用的格追到输入的哪一格（行号从 1 数）', line.includes('Q3 第 6 行「amount」') && line.includes('Q1 第 6 行「amount」'), line)
  const into = await parts.nth(1).locator('[data-ev-merged-into]').innerText().catch(() => '')
  check('输入步骤标明是哪次合并的输入，高亮追到的那一格', into.includes('合并查询 Q3 的输入')
    && await parts.nth(1).locator('td[data-highlight="cell"]').count() === 1, into)
  check('合并查询不是数据源：不说它「已不存在」', await p.locator('[data-ev-query-mask-note]').count() === 0)
  check('输入没查出问题：合并步骤里不出输入的 SQL 检查', await p.locator('[data-ev-merge-input-checks]').count() === 0)
  await traced.ctx.close()

  const flat = await probeQ((id, body) => (id === qseg('1,288') ? asMerge(body, [{
    cell: [5, 'amount'], input: null, query: null, row: null, column: null, note: '没有逐格来历',
  }], { withInput: false, inputChecks: true }) : body))
  await openQ(flat.page, '1,288')
  const none = await flat.page.locator('[data-evidence-panel] [data-ev-merge-trace="none"]').innerText().catch(() => '')
  check('追不到的格：写明没有逐格来历、只有表级来历，不接输入步骤', none.includes('没有逐格来历')
    && none.includes('表级来历') && await flat.page.locator('[data-evidence-panel] [data-ev-query]').count() === 1, none)
  // 没有输入步骤时，输入查询的 SQL 检查结果要在合并步骤里看得到（指标正是因为它标了「存疑」）
  const inChecks = flat.page.locator('[data-evidence-panel] [data-ev-merge-input-checks="o"]')
  const inText = await inChecks.innerText().catch(() => '')
  check('追不到逐格来历时，合并步骤的输入下面列出它自己的 SQL 检查（级别、规则名、说明）', inText.includes('输入 o 的 SQL 检查')
    && inText.includes('错误') && inText.includes('一对多关联后重复计算') && inText.includes('会重复计算')
    && await inChecks.locator('[data-sql-check="fanout_sum"][data-level="error"]').count() === 1, inText.replace(/\s+/g, ' '))
  check('……没查出问题的输入不出', await flat.page.locator('[data-evidence-panel] [data-ev-merge-input-checks="t"]').count() === 0)
  check('……不露规则编号', !/fanout_sum/.test(await flat.page.locator('[data-evidence-panel] [data-ev-merge]').innerText()))
  await flat.ctx.close()

  const wideId = pseg('wide')
  const hop = { alias: 'Q3', node_id: 'merge', input: 'f', query: 'Q1', row: 5, column: '全日客流' }
  const lost = { alias: 'Q3', node_id: 'merge', input: null, query: null, row: null, column: null }
  const noLineage = {
    ...fxp.provenance[wideId], status: 'none', version: null, cell_source: null, checks: [], merge: [lost],
    reason: { code: 'merge_no_lineage', detail: '', text: '这一格来自合并查询，无法确定它对应哪个输入的哪一格' },
  }
  const hops = await open('/ui-harness.html?evidence=1', {
    prov: (run, sid, reply) => (sid === wideId ? { json: { ...reply.json, merge: [hop] } }
      : sid === pseg('long') ? { json: noLineage } : reply),
  })
  await openP(hops.page, 'wide')
  const said = await textOf(pv(hops.page, '[data-ev-prov-merge]'))
  check('推断的来源：先说经过的合并、追到的那一格，下面的原表格子照常', said === '经合并查询 Q3 追到 Q1 第 6 行「全日客流」'
    && await pv(hops.page, '[data-ev-prov-cell]').count() === 1, said)
  await openP(hops.page, 'long')
  check('追不到逐格来历：写原因和没追到的那一跳，不画数据版本和格子', (await textOf(pv(hops.page, '[data-ev-prov-reason="merge_no_lineage"]')))
    .includes('合并查询') && (await textOf(pv(hops.page, '[data-ev-prov-merge-hop="none"]'))).includes('没有逐格来历')
    && await pv(hops.page, '[data-ev-prov-version], [data-ev-prov-cell]').count() === 0)
  await hops.ctx.close()
})

await section('copy-r2','按后端原文匹配的地方：后端现在的原文、整改前的原文都认得（文案整改第二轮）', async () => {
  // 画布问题落到哪一栏（canvas/issues.ts）、检查器的分支标识过滤（Inspector 用 issues.ts 的 isCaseKeyIssue）、
  // 证据面板的裁判触顶（lib/evidence.ts）都按后端原文判断。开发库里历史运行、旧版本存下的文字是整改前的，
  // 新的是整改后的：两种各喂一遍。「新」样本和 backend/app 现在的写法一致（先核对源码片段），
  // 「旧」样本是整改前（提交 68fb353）的原话，只证明旧文字仍认得，不要跟着后端改
  const src = (f) => readFileSync(new URL(`../backend/app/${f}`, import.meta.url), 'utf8')
  const SOURCE = [
    ['engine/schema.py', '条件循环没有设置「继续条件」'],
    ['engine/schema.py', '「数据整形」节点还没有填写表达式'],
    ['engine/schema.py', '「调用工具」节点还没有选择工具'],
    ['engine/schema.py', '「子工作流」节点还没有选择要嵌套的工作流'],
    ['engine/schema.py', '有两个分支的标识都是「{key}」'],
    ['engine/schema.py', '提示词要求用「{tool}」，但节点没有绑定它'],
    ['engine/schema.py', '团队目标要求用「{tool}」，但'],
    ['engine/judge.py', '已达上限（{_limit_label(reason, budget, click=click)}），这句未裁判'],
    ['api/evidence.py', 'f"已达上限（{labels}）：'],
    ['engine/evidence.py', 'UNKNOWN_ENTITY_REASON = "疑似不存在的名称：'],
    ['engine/upgrade.py', 'HINT = "可以升级为可追溯结构"'],
  ]
  const gone = SOURCE.filter(([f, frag]) => !src(f).includes(frag))
  check('「新」样本的片段在后端源码里都还在（后端改了说法就同步这里的样本和前端的正则）', gone.length === 0,
    gone.map(([f, frag]) => `${f}：${frag}`).join(' ｜ '))

  const got = await page.evaluate(async () => {
    const at = (path) => performance.getEntriesByType('resource').map((e) => e.name)
      .find((n) => { try { return new URL(n).pathname === path } catch { return false } }) ?? path
    const issues = await import(at('/src/canvas/issues.ts'))
    const ev = await import(at('/src/lib/evidence.ts'))
    const terms = await import(at('/src/lib/terms.ts'))
    const node = (nodeType, config = {}) => ({ id: 'n', data: { nodeType, config } })
    const field = (nodeType, message, config) => JSON.stringify(issues.fieldOfIssue({ level: 'error', message, node_id: 'n' }, node(nodeType, config)))
    const cases = { cases: [{ key: 'a', condition: 'x > 1' }, { key: 'a', condition: 'x > 2' }] }
    const PAIRS = [
      // [说明, 节点类型, 后端现在的原文, 整改前的原文, 配置]
      ['条件循环没写继续条件 → 循环条件', 'loop', '条件循环没有设置「继续条件」，循环体不会执行', 'while 循环没有写条件，循环体一次都不会跑'],
      ['数据整形没写表达式 → 表达式', 'transform', '「数据整形」节点还没有填写表达式', '整形节点没有填表达式'],
      ['调用工具没选工具 → 工具', 'tool', '「调用工具」节点还没有选择工具', '「调用工具」节点还没选工具'],
      ['子工作流没选工作流 → 工作流', 'subgraph', '「子工作流」节点还没有选择要嵌套的工作流', '「子工作流」节点还没选要嵌套的工作流'],
      ['两个分支标识重复 → 后一个分支的标识', 'branch',
        '「条件分支」中有两个分支的标识都是「a」：路由只按标识区分，后一个分支不会被选中。请换一个不重复的标识',
        "「条件分支」里有两个分支的标识都是 'a'：路由只认标识，后一个永远轮不到。给它换一个不重复的标识", cases],
      ['分支没有连线 → 那个分支', 'branch', '分支「a」还没有连线', "分支 'a' 没有连出去的边", cases],
      ['分支条件有误 → 那个分支的条件', 'branch', '分支「a」的条件有误：语法错误（原文：x >）', '分支「a」的条件写错了：语法错误（原文：x >）', cases],
    ]
    return {
      fields: PAIRS.map(([what, type, now, before, config]) => [what, field(type, now, config), field(type, before, config)]),
      unbound: [
        issues.unboundToolOf('提示词要求用「db_query__shop」，但节点没有绑定它'),
        issues.unboundToolOf('团队目标要求用「crm_lookup」，但没有任何成员绑定该工具：调度者分派的成员无法调用它'),
        issues.unboundToolOf('协作目标要求用「crm_lookup」，但没有哪个成员绑定了它：调度者派出去的人用不了'),
      ],
      bareAdvice: [
        issues.isBareUpgradeAdvice('可以升级为可追溯结构'),
        issues.isBareUpgradeAdvice('可升级为可追溯结构'),
        issues.isBareUpgradeAdvice('可以升级为可追溯结构：「写周报」的数字来自代码节点，先预览改动'),
      ],
      keyIssue: [
        issues.isCaseKeyIssue('分支「default」的标识用了「default」：这是「其他」出口的保留名，两者会合并成一个出口'),
        issues.isCaseKeyIssue("「条件分支」里有两个分支的标识都是 'a'：路由只认标识，后一个永远轮不到。给它换一个不重复的标识"),
        issues.isCaseKeyIssue('分支「a」还没有连线'),
      ],
      limit: [
        ev.isJudgeLimit('已达上限（每次点击的金额上限 $0.01）：1 句未裁判，已裁判的结果保留'),
        ev.isJudgeLimit('已到上限（每次点击的金额上限 $0.01）：1 句没判，记为未裁判；已判的保留'),
        ev.isJudgeLimit('裁判失败：超时'),
        ev.judgeLimitWords('已达上限（这份报告的裁判金额上限 $0.05），这句未裁判'),
        ev.judgeLimitWords('已到上限（这份报告的裁判金额上限 $0.05），这句没判'),
      ],
      settings: [
        Object.values(terms.JUDGE_TEXT.limitHow.click).every((h) => ev.pointsToSettings(h)),
        ev.pointsToSettings(terms.JUDGE_TEXT.limitHow.report.max_cost_usd),
        ev.pointsToSettings('到「设置 → 偏好设置 → 证据裁判」调高每日上限'),
      ],
      suspect: [
        ev.segmentLabel({ kind: 'entity', text: '`orders.coupon_code`', code: true, name: 'orders.coupon_code', state: 'none', issue: 'unknown_entity',
          cite: { reason: '疑似不存在的名称：本次运行的表结构快照、查询用到的表和查询结果列中都没有这个名称' } }),
        ev.segmentLabel({ kind: 'entity', text: '`orders.coupon_code`', code: true, name: 'orders.coupon_code', state: 'none', issue: 'unknown_entity',
          cite: { reason: '本次运行的表结构快照、查询用到的表、查询结果列里都没有这个名字，可能是编造的名字' } }),
      ],
    }
  }).catch((e) => ({ error: String(e) }))
  if (got.error) { check('按原文匹配的函数都拿得到', false, got.error); return }
  const want = {
    '条件循环没写继续条件 → 循环条件': '{"key":"condition"}',
    '数据整形没写表达式 → 表达式': '{"key":"expression"}',
    '调用工具没选工具 → 工具': '{"key":"tool"}',
    '子工作流没选工作流 → 工作流': '{"key":"workflow_id"}',
    '两个分支标识重复 → 后一个分支的标识': '{"key":"cases","index":1,"sub":"key"}',
    '分支没有连线 → 那个分支': '{"key":"cases","index":0}',
    '分支条件有误 → 那个分支的条件': '{"key":"cases","index":0,"sub":"condition"}',
  }
  for (const [what, now, before] of got.fields) {
    check(`画布问题落到字段：${what}（后端现在的原文）`, now === want[what], now)
    check(`画布问题落到字段：${what}（整改前的原文）`, before === want[what], before)
  }
  check('提示词点名的工具：节点、团队目标（新旧两种说法）都取得出工具名',
    got.unbound.join(',') === 'db_query__shop,crm_lookup,crm_lookup', got.unbound.join(','))
  check('问题面板的升级建议只有一句标题时才补说明：后端原话「可以升级…」和界面的「可升级…」都认，带了具体说明的不补',
    got.bareAdvice.join(',') === 'true,true,false', got.bareAdvice.join(','))
  check('检查器不重复列的「标识」问题：保留名、重复标识（新旧）都认得，别的分支问题不误认',
    got.keyIssue.join(',') === 'true,true,false', got.keyIssue.join(','))
  check('证据面板的裁判触顶：「已达上限」「已到上限」都认得，别的失败不误认', got.limit.slice(0, 3).join(',') === 'true,true,false',
    got.limit.slice(0, 3).join(','))
  check('……触顶理由去掉前缀（新旧两种）', got.limit[3] === '这份报告的裁判金额上限 $0.05，这句未裁判'
    && got.limit[4] === '这份报告的裁判金额上限 $0.05，这句没判', `${got.limit[3]} ｜ ${got.limit[4]}`)
  check('……调法指向设置页的才给直达入口（现在写「前往「设置」，以前写「到「设置」）',
    got.settings.join(',') === 'true,false,true', got.settings.join(','))
  check('可疑名称的读屏名：原因里已带状态名的（现在「疑似不存在的名称：」、以前「可能是编造的名字」）不再重复',
    got.suspect[0] === 'orders.coupon_code，疑似不存在的名称：本次运行的表结构快照、查询用到的表和查询结果列中都没有这个名称'
      && got.suspect[1] === 'orders.coupon_code，本次运行的表结构快照、查询用到的表、查询结果列里都没有这个名字，可能是编造的名字',
    got.suspect.join(' ｜ '))
})

check('没有运行时报错（组件预览）', harness.errors.length === 0, harness.errors.join(' | '))
await harness.ctx.close()
await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 可点击证据全部通过')
process.exit(failed ? 1 : 0)
