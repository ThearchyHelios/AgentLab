// 三个页面的端到端检查：真浏览器、真后端、真数据。
//
// check-decode 守翻译，check-stream 守渲染，这一层守的是"接起来还对不对"——
// 前两层都能通过而这一层坏掉：store 没把事件存进去、tab 切换把状态卸载了、
// 唯一的运行入口被改版顺手删了。
//
// 跑之前前后端都得起着：./scripts/dev.sh
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
const API = process.env.AGENTLAB_API ?? 'http://localhost:8000/api'
const CHROME = process.env.CHROME_PATH
  ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const SHOTS = process.env.SHOT_DIR ?? ''

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}

const browser = await chromium.launch({ executablePath: CHROME })
const ctx = await browser.newContext({ viewport: { width: 1500, height: 940 } })

/** 打开一页并收集真实报错（favicon 的 404 不算） */
async function visit(path) {
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))
  page.on('console', (m) => {
    if (m.type() === 'error' && !m.text().includes('404')) errors.push(m.text())
  })
  await page.goto(`${WEB}${path}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(700)
  return { page, errors }
}
const shot = async (page, name) => {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png` })
}

console.log('=== 画布助手栏 ===')
{
  const { page, errors } = await visit('/studio')
  check('没有运行时报错', errors.length === 0, errors.join(' | '))

  const body = await page.locator('body').innerText()
  check('不再有两层 tab 嵌套',
    !body.includes('时间线') && !body.includes('原始事件'))

  // 用户的原话："没有一个很好的地方引导用户写问题"。空态下输入区就该是主体
  check('空态给出了明确的邀请', body.includes('想让它做什么'))
  check('说清楚它能够到什么', /\d+ 个工具/.test(body), body.match(/\d+ 个工具/)?.[0])
  const examples = await page.locator('aside button').filter({ hasText: /。|，/ }).count()
  check('例句是完整句子而不是截断的 chip', examples >= 2, `${examples} 条`)

  const composer = page.locator('aside textarea').first()
  check('输入框自动聚焦，不用先点一下',
    await composer.evaluate((el) => el === document.activeElement))

  // 改版最容易顺手删掉的东西：唯一的运行入口
  check('发起运行的入口还在（已搬到工具栏）',
    await page.getByRole('button', { name: /^运行/ }).count() > 0)

  // 这是这次重构的核心承诺：属性是盖在助手上的一层，不是把它换掉。
  // 切 tab 会卸载组件，草稿、滚动位置、展开状态全丢——那正是要修的问题
  await composer.fill('测试草稿不要丢')
  await page.locator('.react-flow__node').first().click()
  await page.waitForTimeout(350)
  const sheet = await page.locator('body').innerText()
  check('选中节点滑出属性面板', sheet.includes('节点名称') || sheet.includes('ID:'))
  check('面板上有明确的返回，让人知道底下还有东西',
    await page.getByTitle('返回助手（Esc）').count() > 0)

  await page.keyboard.press('Escape')
  await page.waitForTimeout(350)
  check('Esc 关得掉', await page.getByTitle('返回助手（Esc）').count() === 0)
  check('回来之后输入的字还在', await composer.inputValue() === '测试草稿不要丢',
    await composer.inputValue())
  await composer.fill('')

  const overflow = await page.evaluate(() =>
    document.documentElement.scrollWidth - document.documentElement.clientWidth)
  check('页面不横向溢出', overflow <= 0, `${overflow}px`)
  await shot(page, 'studio')
  await page.close()
}

console.log('\n=== 变量表与模板补全 ===')
{
  const { page, errors } = await visit('/studio')
  check('没有运行时报错', errors.length === 0, errors.join(' | '))

  await page.getByRole('button', { name: /^变量/ }).click()
  await page.waitForTimeout(700)
  const rows = await page.locator('table tbody tr').count()
  check('变量表列出了变量', rows > 0, `${rows} 行`)
  const body = await page.locator('body').innerText()
  check('列出的是可直接粘的写法', body.includes('{{ input.'), '')

  // 抽屉是挤压式的：画布不能被压没，React Flow 容器高度归零会直接报错
  const canvasH = await page.locator('.react-flow').first()
    .evaluate((el) => el.getBoundingClientRect().height)
  check('画布没有被压没', canvasH > 120, `${Math.round(canvasH)}px`)
  await page.getByRole('button', { name: /^变量/ }).click()
  await page.waitForTimeout(400)

  // 补全：模板取不到值会静默渲染成空字符串，补全是从源头消灭这类错误。
  // 默认那张图只有 输入→成果，没有带模板的字段，先从面板拖一个模型调用出来
  await page.locator('text=模型调用').first().click()
  await page.waitForTimeout(700)
  // 必须限定在属性面板里：底下助手栏的 Copilot 输入框也是 aside textarea，
  // 而它被这一层盖着，点它会一直超时
  const field = page.locator('.sheet-in textarea').first()
  await field.click()
  await page.keyboard.type('{{')
  await page.waitForTimeout(400)
  const hasPanel = (await page.locator('body').innerText()).includes('⏎ 插入')
  check('打 {{ 弹出候选', hasPanel)
  if (hasPanel) {
    await page.keyboard.press('Enter')
    await page.waitForTimeout(350)
    const v = await field.inputValue()
    check('选中后插入成完整写法', /\{\{ \S+ \}\}/.test(v), v.slice(0, 40))
    // 完全受控组件，插入后光标会被重置到末尾，必须自己放回去
    await page.keyboard.type('X')
    check('光标停在插入内容之后', (await field.inputValue()).endsWith('X'),
      (await field.inputValue()).slice(-20))
  }
  await shot(page, 'variables')
  await page.close()
}

console.log('\n=== 动效对前庭敏感者可关 ===')
{
  // 这套界面里动的东西不少（边在流动、节点在脉冲、面板在滑）。
  // 系统里关了动效还照播，对前庭敏感的人是实打实的难受
  const page = await ctx.newPage()
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto(`${WEB}/studio`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(500)
  const durations = await page.evaluate(() => {
    const probe = document.createElement('div')
    probe.className = 'rise-in'
    document.body.appendChild(probe)
    const d = getComputedStyle(probe).animationDuration
    probe.remove()
    return d
  })
  check('关掉动效后动画确实停了', parseFloat(durations) < 0.01, durations)
  await page.close()
}

console.log('\n=== 运行历史 ===')
{
  const { page, errors } = await visit('/runs')
  check('没有运行时报错', errors.length === 0, errors.join(' | '))

  const rows = page.locator('button').filter({ hasText: /\d{4}\/\d{1,2}\/\d{1,2}/ })
  const n = await rows.count()
  check('列出了运行记录', n > 0, `${n} 条`)

  if (n > 0) {
    // 列表原本每行只有"临时图 · 1.2s · 340 tok"，十条长得一模一样
    const texts = await rows.allInnerTexts()
    const withSummary = texts.filter((t) => t.trim().split('\n').length >= 3).length
    check('行里有内容摘要而不只是耗时和 token', withSummary > 0,
      `${withSummary}/${texts.length} 行带摘要`)

    await rows.first().click()
    await page.waitForTimeout(900)
    const body = await page.locator('body').innerText()
    check('详情走的是可读视图，不是事件表',
      !/\bnode\.(started|finished)\b/.test(body) && !/\brun\.started\b/.test(body))
    check('详情有内容', body.length > 300, `${body.length} 字`)

    // 原始事件不是常驻 tab，但必须还能看到
    await page.locator('button[title*="原始事件"]').first().click()
    await page.waitForTimeout(400)
    const rawBody = await page.locator('body').innerText()
    check('切得到原始事件', /node\.(started|finished)|run\.started/.test(rawBody))
    await page.locator('button[title*="可读视图"]').first().click()
    await page.waitForTimeout(300)
    check('切得回来', !/\bnode\.started\b/.test(await page.locator('body').innerText()))
  }
  await shot(page, 'runs')
  await page.close()
}

console.log('\n=== 问数据 ===')
{
  const { page, errors } = await visit('/chat')
  check('没有运行时报错', errors.length === 0, errors.join(' | '))
  const body = await page.locator('body').innerText()
  check('有空态或历史，不是白屏', body.length > 60, `${body.length} 字`)
  check('有输入框', await page.locator('textarea').count() > 0)
  const overflow = await page.evaluate(() =>
    document.documentElement.scrollWidth - document.documentElement.clientWidth)
  check('页面不横向溢出', overflow <= 0, `${overflow}px`)
  await shot(page, 'chat')
  await page.close()
}

console.log('\n=== 向量模型设置 ===')
{
  // 以前这里只有三个写死的选项（本地 / OpenAI 3-small / 3-large），接不了
  // 本机起的服务。而本地哈希向量没有语义能力这件事，界面上也得说出来。
  const { page, errors } = await visit('/knowledge')
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))

  let body = await page.locator('body').innerText()
  check('显示当前用的是什么、几维', /·\s*\d+\s*维/.test(body),
        body.match(/[\w:.-]+ · \d+ 维/)?.[0])
  if (body.includes('local-hashing')) {
    check('本地哈希要标出没有语义能力', body.includes('没有语义能力'))
  }

  await page.getByRole('button', { name: '换一个' }).first().click()
  await page.waitForTimeout(400)
  body = await page.locator('body').innerText()
  check('能配自定义端点', body.includes('自定义端点'))
  check('也留着本地那一项', body.includes('本地哈希向量'))
  // 模型名手填太容易错，必须能探
  check('给得出探测入口', body.includes('看看有哪些模型'))

  const overflow = await page.evaluate(() =>
    document.documentElement.scrollWidth - document.documentElement.clientWidth)
  check('页面不横向溢出', overflow <= 0, `${overflow}px`)
  await shot(page, 'embedding')
  await page.close()
}

console.log('\n=== 会话 ===')
{
  // 对话以前只活在内存里，刷新就没了。这一组守的是它真的落了库：
  // 列表能列出来、切过去内容跟着变、刷新还在。
  //
  // 最后一项守的是一个真踩过的坑：进页面时自动建一条空会话，而 create 是
  // 异步的、StrictMode 又把 effect 跑两遍，于是每次访问都留下两三条"新对话"。
  const before = await (await fetch(`${API}/conversations`)).json()

  const { page, errors } = await visit('/chat')
  check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
  check('有"新对话"按钮', await page.getByRole('button', { name: /新对话/ }).first().isVisible())

  const seeded = before.find((c) => c.turn_count > 0)
  if (seeded) {
    await page.locator('aside button[title]').filter({ hasText: seeded.title }).first().click()
    await page.waitForTimeout(700)
    check('切过去能看到那次问的话', (await page.locator('main').innerText()).includes(seeded.title))
    await page.reload({ waitUntil: 'networkidle' })
    await page.waitForTimeout(900)
    check('刷新后还在', (await page.locator('main').innerText()).includes(seeded.title))
  } else {
    check('（跳过：库里还没有带轮次的会话）', true)
  }

  const after = await (await fetch(`${API}/conversations`)).json()
  check('逛一圈没有凭空多出空会话', after.length <= before.length,
        `${before.length} → ${after.length}`)
  await shot(page, 'conversations')
  await page.close()
}

console.log('\n=== URL 指得到 ===')
{
  // 在此之前「在看哪个对话/哪次运行/哪一屏」全在 store 和 localStorage 里：
  // 一次对话没有地址，发给同事只能发截图；刷新靠 localStorage，换台机器就丢；
  // 浏览器后退会直接离开整个页面而不是回到上一个对话。
  const convs = await (await fetch(`${API}/conversations`)).json()
  const seeded = convs.filter((c) => c.turn_count > 0)

  if (seeded.length >= 2) {
    const [a, b] = seeded
    const { page, errors } = await visit(`/chat/${a.id}`)
    check('没有运行时报错', errors.length === 0, errors.slice(0, 2).join(' | '))
    check('深链接直达那个对话', (await page.locator('main').innerText()).includes(a.title),
          `想要「${a.title}」`)
    check('地址里就是那个 chatID', page.url().endsWith(`/chat/${a.id}`), page.url())

    // 点另一个：地址要跟着走，而不是只改 store
    await page.locator('aside button[title]').filter({ hasText: b.title }).first().click()
    await page.waitForTimeout(600)
    check('切换对话会改地址', page.url().endsWith(`/chat/${b.id}`), page.url())

    // 刷新靠的是 URL，不是 localStorage
    await page.reload({ waitUntil: 'networkidle' })
    await page.waitForTimeout(800)
    check('刷新后还在同一个对话', page.url().endsWith(`/chat/${b.id}`)
          && (await page.locator('main').innerText()).includes(b.title))

    // 后退回到上一个对话，而不是甩出整个页面——这条以前是彻底失效的
    await page.goBack({ waitUntil: 'networkidle' })
    await page.waitForTimeout(700)
    check('后退回到上一个对话', page.url().endsWith(`/chat/${a.id}`), page.url())
    await shot(page, 'url-chat')
    await page.close()
  } else {
    check('（跳过：库里不足两个带轮次的会话）', true)
  }

  {
    // 链接过期/对话被删：不能白屏，也不能一声不响地跳走
    const { page } = await visit('/chat/这个id根本不存在')
    const body = await page.locator('body').innerText()
    check('指向不存在的对话时不白屏', body.length > 40, `${body.length} 字`)
    check('落到了一个真实对话', /\/chat\/[0-9a-f]{8,}$/.test(page.url()), page.url())
    // 认整句提示，不认"不在了"三个字——会话标题里碰巧有这仨字就会把它蒙对
    const notice = body.includes('那个对话不在了')
    check('说清楚了为什么换了地方', notice,
          notice ? '' : body.slice(-200).replace(/\n/g, ' '))
    await page.close()
  }

  {
    // tab 也得能指：/settings 永远落在「模型接入」，以前指不到数据源那一屏
    const { page } = await visit('/settings/datasources')
    check('深链接直达数据源那一屏', (await page.locator('body').innerText()).includes('数据源'))
    await page.close()
    const bare = await visit('/settings')
    check('不带 tab 时落到第一屏并纠正地址',
          bare.page.url().endsWith('/settings/providers'), bare.page.url())
    await bare.page.close()
    const bogus = await visit('/knowledge/没这个tab')
    check('认不出的 tab 名回落而不是空白',
          bogus.page.url().endsWith('/knowledge/kb'), bogus.page.url())
    await bogus.page.close()
  }

  {
    // 画布也该能指。这条路最容易出事：前进/后退会绕过选择器里那道
    // "未保存改动"的确认直接换图
    const wfs = await (await fetch(`${API}/workflows`)).json()
    if (wfs.length >= 2) {
      const bare = await visit('/studio')
      check('/studio 落到第一张图并纠正地址',
            bare.page.url().endsWith(`/studio/${wfs[0].id}`), bare.page.url())
      await bare.page.close()

      const { page } = await visit(`/studio/${wfs[1].id}`)
      check('深链接直达那张图', (await page.locator('body').innerText()).includes(wfs[1].name),
            `想要「${wfs[1].name}」`)
      await page.close()
    } else {
      check(`（跳过：库里只有 ${wfs.length} 张工作流）`, true)
    }

    const { page } = await visit('/studio/根本没这张图')
    const body = await page.locator('body').innerText()
    // 说一次就够。这里真弹过三次——effect 在请求回来之前又跑了两遍
    const times = (body.match(/那张工作流不在了/g) || []).length
    check('指向不存在的图时说一次并让开', times === 1 && /\/studio\/[0-9a-f]{8,}$/.test(page.url()),
          `提示 ${times} 次，落在 ${page.url()}`)
    await page.close()
  }

  {
    // 问数据页的「在画布里打开」：建成一张新工作流、送去它自己的地址。
    // 以前只是把节点塞进画布，store 里的 workflow 还是上一张图，⌘S 就把那张
    // 整张覆盖了。要守的是：送到的是新图的地址、图没被别的工作流盖掉、
    // 没弹莫名其妙的"有未保存的改动"。
    //
    // 这个按钮会写库。检查不该在库里留东西（以前按旧断言跑一次就多一张
    // 「问数据：…」），所以拦下建图那次 POST，拿它自己发出去的图回一个
    // "已建好"，之后对这张图的读取也由这里答；校验、变量分析只算不写，放行
    const DRAFT = 'c0ffee00c0ffee00c0ffee00c0ffee00'
    const convs = await (await fetch(`${API}/conversations`)).json()
    let seed = null
    for (const c of convs.slice(0, 8)) {
      const d = await (await fetch(`${API}/conversations/${c.id}`)).json()
      const t = (d.turns || []).find((x) => x.graph?.nodes?.length)
      if (t) { seed = { id: c.id, want: t.graph.nodes.length }; break }
    }
    if (seed) {
      const before = await (await fetch(`${API}/workflows`)).json()
      const page = await ctx.newPage()
      let dialogs = 0
      page.on('dialog', async (d) => { dialogs++; await d.dismiss() })
      let created = null
      await page.route('**/api/workflows**', (route) => {
        const r = route.request()
        const path = new URL(r.url()).pathname
        if (r.method() === 'POST' && path.endsWith('/api/workflows')) {
          const body = r.postDataJSON()
          const now = new Date().toISOString().replace('Z', '')
          created = { id: DRAFT, name: body.name, description: '', graph: body.graph, tags: [],
                      version: 1, is_template: false, status: 'draft', published_version: null,
                      created_at: now, updated_at: now, run_count: 0 }
          return route.fulfill({ status: 201, json: created })
        }
        if (path.includes(`/workflows/${DRAFT}`)) {
          if (path.endsWith('/versions')) return route.fulfill({ json: [] })
          if (r.method() === 'GET' && created) return route.fulfill({ json: created })
        }
        if (r.method() === 'GET' || /\/workflows\/(validate|variables)$/.test(path)) {
          return route.continue()
        }
        return route.abort()   // 其余写操作一律不放
      })
      await page.goto(`${WEB}/chat/${seed.id}`, { waitUntil: 'networkidle' })
      await page.waitForTimeout(900)
      const btn = page.getByRole('button', { name: '在画布里打开' }).first()
      if (await btn.count()) {
        await btn.click()
        await page.waitForURL(`**/studio/${DRAFT}`, { timeout: 5000 }).catch(() => {})
        await page.waitForTimeout(1200)
        check('「在画布里打开」建了新图、送到它自己的地址',
              !!created && page.url().endsWith(`/studio/${DRAFT}`), page.url())
        check('建的是这一轮的图，名字带着问题',
              created?.graph?.nodes?.length === seed.want && created.name.startsWith('问数据：'),
              created?.name)
        check('送过去的草稿图没被第一张工作流盖掉',
              (await page.locator('.react-flow__node').count()) === seed.want,
              `${await page.locator('.react-flow__node').count()} / ${seed.want} 个节点`)
        check('没有弹莫名其妙的"未保存改动"', dialogs === 0, `弹了 ${dialogs} 次`)
        const after = await (await fetch(`${API}/workflows`)).json()
        check('检查本身没往库里写图', after.length === before.length,
              `${before.length} → ${after.length}`)
      } else {
        check('（跳过：这一轮没有「在画布里打开」）', true)
      }
      await page.close()
    } else {
      check('（跳过：前 8 个会话里没有带图的轮次）', true)
    }
  }

  {
    // 单个文档 / 单个工具也要能指。文档那边原来连详情视图都没有——
    // 列表里只有标题和片段数，点不开，而切块结果（重叠、表头续接）
    // 是检索不准时第一个该看的东西
    const docs = await (await fetch(`${API}/kb/documents`)).json()
    const ready = docs.find((d) => d.status !== 'processing' && d.chunk_count > 0)
    if (ready) {
      const { page } = await visit(`/knowledge/kb/${ready.id}`)
      const body = await page.locator('body').innerText()
      check('深链接直达那份文档', body.includes(ready.title), ready.title)
      check('看得到切块结果', /片段 \d+/.test(body) && /\d+ 字/.test(body),
            body.slice(0, 120).replace(/\n/g, ' '))
      check('回得去列表', (await page.getByRole('button', { name: /回到知识库/ }).count()) === 1)
      await page.close()

      const gone = await visit('/knowledge/kb/根本没这份文档')
      const goneBody = await gone.page.locator('body').innerText()
      check('文档不在了要说出来而不是白屏', goneBody.includes('不在了'),
            goneBody.slice(-120).replace(/\n/g, ' '))
      await gone.page.close()
    } else {
      check('（跳过：库里没有切完块的文档）', true)
    }

    const tools = await (await fetch(`${API}/tools`)).json()
    if (tools.length) {
      const t = tools[0]
      const { page } = await visit(`/tools/library/${t.id}`)
      const body = await page.locator('body').innerText()
      check('深链接直达那个工具', body.includes(t.name), t.name)
      check('参数说明跟着出来', body.includes('参数'), '')
      await page.close()
    }
  }

  {
    // 运行列表只取前 100 条，而一条老运行的链接必须也能打开——
    // 所以详情是按 id 直接取的，不是在列表里找
    const all = await (await fetch(`${API}/runs?limit=200`)).json()
    if (all.length > 100) {
      const old = all[all.length - 1]
      const { page } = await visit(`/runs/${old.id}`)
      const body = await page.locator('body').innerText()
      check('第 100 条之后的老运行也打得开', body.includes(old.id.slice(0, 6)),
            `run ${old.id.slice(0, 8)}`)
      await page.close()
    } else {
      check(`（跳过：库里只有 ${all.length} 条运行，不足以验证越过列表上限）`, true)
    }
  }
}

console.log('\n=== 三处讲的是同一个故事 ===')
{
  // 同一次运行，运行页详情和 preview 里的 dense 渲染应该给出同一批步骤。
  // 这是"全站一个翻译层"的实际含义——分两套的话用户没法判断哪个是真的。
  const a = await ctx.newPage()
  await a.goto(`${WEB}/preview.html?case=loop_approve`, { waitUntil: 'networkidle' })
  await a.waitForTimeout(400)
  const wide = await a.locator('body').innerText()
  const asks = (wide.match(/这条公告可以发吗/g) ?? []).length
  // 宽栏和窄栏各渲染一遍，所以是 3×2
  check('宽窄两栏各三轮审批，一轮不少', asks === 6, `${asks} 处`)
  check('决定折进了同一行而不是另起一行',
    !/^你放行了$/m.test(wide) && !/^你驳回了$/m.test(wide))
  await a.close()
}

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 界面端到端全部通过')
process.exit(failed ? 1 : 0)
