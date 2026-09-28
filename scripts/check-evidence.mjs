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

/** 伪造证据相关的 GET；片段请求记账（缓存：同一个片段点两次只取一次） */
async function routed(page) {
  const hits = []
  await page.route((u) => new URL(u).pathname.startsWith('/api/'), (r) => {
    const req = r.request()
    const url = new URL(req.url())
    if (req.method() !== 'GET') return r.abort()
    const seg = url.pathname.match(/\/api\/runs\/[^/]+\/evidence\/segments\/([^/]+)$/)
    if (seg) {
      hits.push(decodeURIComponent(seg[1]))
      const body = fx.segments[decodeURIComponent(seg[1])]
      return body ? r.fulfill({ json: body })
        : r.fulfill({ status: 404, json: { detail: '报告里没有这个片段', code: 'evidence_segment_not_found' } })
    }
    if (/\/api\/runs\/[^/]+\/evidence$/.test(url.pathname)) return r.fulfill({ json: fx.graph })
    if (url.pathname === `/api/artifacts/${fx.doc_artifact}`) return r.fulfill({ json: { id: fx.doc_artifact, content: fx.doc } })
    return r.continue()
  })
  return hits
}

async function open(url, { w = 1280, h = 1000, reduced = false } = {}) {
  const ctx = await browser.newContext({ viewport: { width: w, height: h }, ...(reduced ? { reducedMotion: 'reduce' } : {}) })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  const hits = await routed(page)
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
  check('顶部有一段读屏摘要，说清计数和按键', summary.includes('7/12 数字有出处 · 无证据 5 · 另有 1 处引用解析不了') && summary.includes('方向键'), summary)
  check('读屏摘要说清画不了线的在哪：列表序号里的数字，不混说成「句末依据」', summary.includes('其中 1 个数字在列表序号')
    && !summary.includes('句末依据'), summary)
  const sr = await page.evaluate((sel) => {
    const el = document.querySelector(`${sel} [data-evidence-summary]`)
    const r = el?.getBoundingClientRect()
    return !!r && r.width <= 1 && r.height <= 1
  }, wide)
  check('读屏摘要不占视觉位置（sr-only）', sr)
  const tally = await page.locator(`${wide} [data-evidence-tally]`).innerText().catch(() => '')
  check('没有出具横幅时文档自己给出计数条', tally.includes('7/12 数字有出处 · 无证据 5 · 另有 1 处引用解析不了') && tally.includes('定位下一处'), tally)
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
  check('解析不了的引用说出原因', text.includes('缺输入'), text.slice(0, 80))
  check('value 为 null：写「缺输入」', await panel(page).locator('[data-ev-missing]').count() === 1
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
  check('裸数字说清没写引用标记', text.includes('没有写成引用标记') && text.includes('要写成引用标记'), text.slice(0, 120))

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
  check('出具横幅多一行「N/N 数字有出处 · 无证据 M」和「定位下一处」', line.includes('7/12 数字有出处 · 无证据 5 · 另有 1 处引用解析不了') && line.includes('定位下一处'), line)
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
  check('执行过程里有「核对报告」那一行', row.includes('核对报告：7/12 数字有出处 · 无证据 5 · 另有 1 处引用解析不了'))
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
             fold: document.querySelector(sel).innerText.includes('后面还有，这里先折叠了') }
  }, box)
  check('超过 40 块的报告：每块 content-visibility: auto', info.cv.length > 0 && info.cv.every((v) => v === 'auto'), info.cv.slice(0, 3).join(','))
  check('超过 3000 字先折叠，说清「后面还有」', info.fold && info.n < 128, `${info.n} 块`)
  const tally = await page.locator(`${box} [data-evidence-tally]`).innerText()
  check('没有 stats、没有违规清单时从片段自己数（16 份：数字 4 处 + 非数字引用 1 处）',
    tally.includes('112/176 数字有出处 · 无证据 64 · 另有 16 处引用解析不了'), tally)
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
  check('工件哈希对不上、不在封存范围内：用失败色逐条写出', said.includes('哈希对不上') && said.includes('不在封存范围内'), said)
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
    && got.text.includes('证据链这次没取到'), `${got.state} ${got.text.replace(/\s+/g, ' ').slice(-80)}`)
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
    check(`${name}：${late ? '单独报「不在封存范围内」' : '不说「事后补进来的」（封存那一行已经说清了）'}`,
      got.text.includes('事后补进来') === late, got.text.replace(/\s+/g, ' ').slice(0, 120))
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
    && (await r.page.locator('#fx-field-other-run').innerText()).includes('不是这次运行写的')
    && await r.page.locator('#fx-field-other-run [data-seg]').count() === 0, String(other))
  const bare = await r.page.locator('#fx-field-no-markdown [data-evidence-fallback]').getAttribute('data-evidence-fallback').catch(() => null)
  check('文档没记正文（缺 markdown）：当对不上处理，不画', bare === 'mismatch'
    && await r.page.locator('#fx-field-no-markdown [data-seg]').count() === 0, String(bare))
  await r.ctx.close()
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
  check('违规时的默认写明按运行类别', onViolation[0]?.includes('探索运行照常产出'), onViolation.join(' | '))
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
check('没有运行时报错（组件预览）', harness.errors.length === 0, harness.errors.join(' | '))
await harness.ctx.close()
await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 可点击证据全部通过')
process.exit(failed ? 1 : 0)
