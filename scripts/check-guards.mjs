// 几道"坏了就是整页事故"的护栏：输入法回车、未知节点类型、页面级和外壳级的
// 错误边界、全局快捷键不抢输入框、未知地址不是白板。
//
// 出事时的样子都很重：中文用户每选一个词就把半句话发去建图、跑图；
// Copilot 编出一个不存在的节点类型，整站白屏、没保存的编辑一起丢；任何组件渲染
// 抛错，React 卸掉整棵树，连导航栏都没了；外壳的快捷键一旦抢了输入框，问题里
// 打个问号就弹出一层面板。而 decode / stream / ui 三层检查对它们一个字都不会
// 说——它们喂的都是合法数据。
//
// 探针拦掉所有非 GET 的 /api 请求：不写库、不调模型、不花钱，随时可以跑。
// 往画布 store 里注入节点走的是 dev 构建挂在 window.__studio 上的那一份。
// 跑之前前端得起着（./scripts/dev.sh），默认连 5273。对别的实例（比如一份沙箱拷贝）跑时
// 带上地址：AGENTLAB_WEB=http://localhost:<前端端口> node scripts/check-guards.mjs
import { chromium } from '../frontend/node_modules/playwright-core/index.mjs'

const WEB = process.env.AGENTLAB_WEB ?? 'http://localhost:5273'
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
const page = await browser.newPage({ viewport: { width: 1280, height: 860 } })
const writes = []
await page.route('**/api/**', (route) => {
  const r = route.request()
  if (r.method() === 'GET') return route.continue()
  writes.push(`${r.method()} ${new URL(r.url()).pathname}`)
  return route.abort()
})
const errors = []
page.on('pageerror', (e) => errors.push(e.message))

// 等到页面上出现某段文字为止。只在"应该发生"的断言上用；"不应该发生"的
// 只能等一个窗口——那是它的本性，不是偷懒
const shows = (text) => page.getByText(text).first().waitFor({ timeout: 8000 }).then(() => true, () => false)

await section('问数据页：输入法组字时的回车', async () => {
  await page.goto(`${WEB}/chat`, { waitUntil: 'networkidle' })
  const box = page.locator('textarea').first()
  await box.fill('帮我查一下本周')
  const fire = (init) => box.evaluate((el, init) => el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init })), init)
  writes.length = 0
  await fire({ key: 'Enter', isComposing: true })
  await page.waitForTimeout(400)
  check('isComposing 的回车不发送', writes.length === 0, writes.join(', '))
  await fire({ key: 'Enter', keyCode: 229 })
  await page.waitForTimeout(400)
  check('Safari 式 keyCode=229 的回车不发送', writes.length === 0, writes.join(', '))
  check('草稿还在输入框里', (await box.inputValue()) === '帮我查一下本周')
  const sent = page.waitForRequest((r) => r.method() !== 'GET' && r.url().includes('/api/'), { timeout: 8000 })
    .then(() => true, () => false)
  await fire({ key: 'Enter' })
  await sent
  check('普通回车照常发送（请求被探针拦下）', writes.length > 0, writes.join(', '))
})

await section('画布：未知节点类型不再白屏', async () => {
  await page.goto(`${WEB}/studio`, { waitUntil: 'networkidle' })
  // 画布会自己跳到第一张图，等它真的把节点装进 store 再注入
  await page.waitForFunction(() => window.__studio?.getState().nodes.length > 0, null, { timeout: 8000 })
  await page.evaluate(() => {
    const s = window.__studio.getState()
    window.__studio.setState({ nodes: [...s.nodes, { id: 'ghost', type: 'card', position: { x: 60, y: 60 },
      data: { nodeType: 'no_such_type', label: '幽灵节点', config: {} } }] })
    window.__studio.getState().select('ghost')
  })
  check('属性面板说清楚是未知类型', await shows('无法识别的节点类型'))
  check('画布还在（没有白屏）', (await page.locator('body').innerText()).includes('幽灵节点'))
  check('没有未捕获的运行时错误', errors.length === 0, errors[0] ?? '')
})

await section('错误边界：渲染真出错时只坏这一页', async () => {
  errors.length = 0
  await page.evaluate(() => {
    const s = window.__studio.getState()
    window.__studio.setState({ nodes: [...s.nodes, { id: 'broken', type: 'card', position: { x: 0, y: 0 }, data: null }] })
  })
  check('出错时给出错误页而不是白屏', await shows('页面发生错误'))
  check('错误页标明是页面这一层', await page.locator('[data-error-scope="page"]').count() === 1)
  // 读屏只该把标题和那句说明当警报念：按钮、技术细节（堆栈摘要）不在 role=alert 里
  const alertBox = page.locator('[data-error-scope="page"] [role="alert"]')
  check('role=alert 只包标题和说明，不含按钮和技术细节', (await page.locator('[data-error-scope="page"]').getAttribute('role')) === null
    && await alertBox.count() === 1 && (await alertBox.innerText()).includes('页面发生错误')
    && await alertBox.locator('button, details').count() === 0)
  const after = await page.locator('body').innerText()
  check('导航栏还在', after.includes('问数据') && after.includes('编排'))
  await page.evaluate(() => {
    const s = window.__studio.getState()
    window.__studio.setState({ nodes: s.nodes.filter((n) => n.id !== 'broken' && n.id !== 'ghost') })
  })
  await page.getByText('问数据', { exact: true }).first().click()
  await page.waitForURL(/\/chat/)
  check('换一页就恢复', await page.getByText('页面发生错误').waitFor({ state: 'detached', timeout: 8000 })
    .then(() => true, () => false))
})

await section('外壳自己出错：落在中文错误页，不是路由库的英文报错页', async () => {
  // 导航、后台信号不在页面那一层错误边界里。路由换成 data router 之后，它们一出错，
  // 没人接的话 react-router 会画它自带的「Unexpected Application Error!」和一屏堆栈。
  // 导航上的「有运行在跑」要读画布的 trace：跑着却没有 trace，导航渲染就会抛
  await page.evaluate(() => {
    window.__savedTrace = window.__studio.getState().trace
    window.__studio.setState({ runPhase: 'running', trace: null })
  })
  check('给出中文的错误页，说的是整个界面而不是「这一页」', await shows('界面发生错误')
    && (await page.getByText('页面发生错误').count()) === 0)
  check('错误页标明是外壳这一层', await page.locator('[data-error-scope="shell"]').count() === 1)
  check('不是 react-router 的英文报错页', !(await page.locator('body').innerText()).includes('Unexpected Application Error'))
  await page.evaluate(() => window.__studio.setState({ runPhase: 'idle', trace: window.__savedTrace }))
  await page.getByRole('button', { name: '重试' }).first().click()
  check('点「重试」外壳就回来', await page.locator('nav[aria-label="主导航"]').waitFor({ timeout: 8000 }).then(() => true, () => false))

  // 同样的注入落在编排页上：画布自己订阅了 store，trace 为空时订阅回调不能在 setState 里抛
  // （抛出来的是调用 setState 的那一方，谁注入谁崩，3C REQ-16）
  await page.goto(`${WEB}/studio`, { waitUntil: 'networkidle' })
  await page.locator('.react-flow').first().waitFor({ timeout: 8000 }).catch(() => {})
  const studioInject = await page.evaluate(() => {
    const saved = window.__studio.getState().trace
    try {
      window.__studio.setState({ runPhase: 'running', trace: null })
      return 'ok'
    } catch (e) {
      return String(e?.message ?? e).split('\n')[0]
    } finally {
      window.__studio.setState({ runPhase: 'idle', trace: saved })
    }
  })
  check('编排页上注入 trace:null：画布的订阅不在 setState 里抛', studioInject === 'ok', studioInject)
  await page.getByRole('button', { name: '重试' }).first().click({ timeout: 1500 }).catch(() => {})
})

await section('全局快捷键：不抢输入框', async () => {
  // 外壳在 window 的捕获阶段听键盘，比谁都先拿到。判错一次，问题框里打个问号就
  // 弹出一层面板、⌥3 直接把人带离写了一半的问题
  await page.goto(`${WEB}/chat`, { waitUntil: 'networkidle' })
  const draft = page.locator('textarea').first()
  await draft.fill('本月出勤率')
  await draft.press('?')
  await page.waitForTimeout(300)
  check('输入框里的 ? 是问号，不弹快捷键说明', (await draft.inputValue()).endsWith('?')
    && (await page.getByRole('dialog', { name: /键盘快捷键/ }).count()) === 0, await draft.inputValue())
  await draft.press('Alt+Digit3')
  await page.waitForTimeout(300)
  check('输入框里的 ⌥3 不切页', new URL(page.url()).pathname.startsWith('/chat'), page.url())
  const mod = process.platform === 'darwin' ? { metaKey: true } : { ctrlKey: true }
  await draft.evaluate((el, mod) => el.dispatchEvent(new KeyboardEvent('keydown',
    { bubbles: true, cancelable: true, key: 'k', code: 'KeyK', isComposing: true, ...mod })), mod)
  await page.waitForTimeout(300)
  check('输入法组字时的 ⌘K 不开命令面板', (await page.getByRole('dialog', { name: '命令面板' }).count()) === 0)
  await draft.press(process.platform === 'darwin' ? 'Meta+KeyK' : 'Control+KeyK')
  check('⌘K 在输入框里照样能开命令面板', await page.getByRole('dialog', { name: '命令面板' }).waitFor({ timeout: 3000 })
    .then(() => true, () => false))
  await page.keyboard.press('Escape')
  await page.waitForTimeout(200)
  check('Esc 关掉面板，草稿还在、焦点回到输入框',
    (await draft.inputValue()).startsWith('本月出勤率') && await draft.evaluate((el) => el === document.activeElement))
})

await section('未知地址不是白板', async () => {
  await page.goto(`${WEB}/no-such-page`, { waitUntil: 'networkidle' })
  check('说清楚「这个地址不存在」', await shows('这个地址不存在'))
  check('导航栏还在', (await page.locator('nav').innerText()).includes('问数据'))
})

await browser.close()
console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 护栏全部通过')
process.exit(failed ? 1 : 0)
