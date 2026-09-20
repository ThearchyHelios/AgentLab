import type { RunEvent } from '../types'

/**
 * 事件 → 人能看懂的步骤。
 *
 * 这是全站唯一的翻译层：画布时间线、助手栏、运行页都用它。分两套翻译的话，
 * 同一次运行在不同地方会读出不同的故事，而用户没法判断哪个是真的。
 *
 * 三条原则：
 *
 * 1. **不回写事件**。事件表是审计凭证——run 终态时对全部已落库事件算
 *    manifest_hash，事后增删改会对不上。翻译只发生在前端内存里。
 * 2. **成对事件合并成一行**。tool.start + tool.end 是同一件事的两个时刻，
 *    拆成两行等于把"查询数据"和"查完了"当成两件事讲。
 * 3. **节点是天然的分组单位**。一个图节点内部可能有好几轮模型调用和工具
 *    调用，它们属于同一个"步骤"——这也正是用户说的"简要思考节点"：
 *    节点本身就是那个节点，里面的动作是它的过程。
 */

export type StepKind =
  | 'node'      // 一个图节点的执行（可能带子步骤）
  | 'think'     // 模型的思考
  | 'llm'       // 模型调用
  | 'query'     // 数据库查询
  | 'schema'    // 看表结构
  | 'tool'      // 其他工具
  | 'code'      // 沙箱代码
  | 'branch'    // 分支走向
  | 'human'     // 人工介入
  | 'issuance'  // 出具判定
  | 'note'      // 日志/提示
  | 'error'
  | 'lifecycle' // 开始/结束

export type StepStatus = 'running' | 'done' | 'failed' | 'waiting'

export interface Step {
  id: string
  seq: number
  kind: StepKind
  /** 一句人话，不带技术黑话 */
  title: string
  /** 展开后看的东西：SQL 原文、工具参数、结果预览、思考全文 */
  detail?: string
  /** 行尾的次要信息：耗时、行数 */
  meta?: string
  status?: StepStatus
  level?: 'info' | 'warn' | 'error'
  nodeId?: string
  /** 工件 id，可下钻到完整证据 */
  artifact?: string
  /** 工具/查询的返回。和 detail 分开：查询的 detail 是 SQL 原文，
   *  两者要同时展示（问什么 + 查到什么），合成一个字段就只能二选一 */
  result?: string
  children?: Step[]
  /** 协作团队的泳道数据。只有 supervisor 节点的顶层 Step 会有 */
  team?: TeamRun
}

/**
 * 协作团队一轮里的一个人。
 *
 * ms 是**各自量出来**的耗时，不是拿整轮墙钟摊的——界面要显示"并行省了多少"，
 * 那个数按 N×墙钟 推算会把快的那个也记成最慢那条，省下的时间就被夸大了。
 */
export interface TeamMember {
  agent: string
  instruction: string
  ms: number
  status: StepStatus
  result?: string
}

export interface TeamRound {
  round: number
  /** 这一轮同时派了几个人。1 就是串行的一步 */
  parallel: number
  /** 这一轮实际花的时间：并发时是最慢那个 */
  wallMs: number
  /** 各人耗时之和：并发时它大于 wallMs，差值就是省下的 */
  sumMs: number
  reason?: string
  members: TeamMember[]
}

export interface TeamRun {
  /** 花名册，按第一次出现的顺序——泳道的行顺序 */
  members: string[]
  rounds: TeamRound[]
  /** 并行一共省下多少毫秒。全程串行则为 0 */
  savedMs: number
  /** 协作是不是已经收尾了 */
  finished: boolean
}

// 这两个是流式增量，后端根本不落库（_EPHEMERAL）。单次运行的 delta 量级会
// 把列表淹掉，而且刷新页面后它们不会回来——UI 不能建立在它们之上。
const EPHEMERAL = new Set(['llm.token', 'llm.thinking.delta'])

// 节点类型的中文名。刻意内联而不是 import canvas/nodeDefs：解码器是纯粹的
// 翻译层，画布只是它的消费者之一，反过来依赖画布会让运行页也被迫加载
// 整套节点定义（含图标、表单 schema）。这张表只有 16 行，重复得起。
const TYPE_LABEL: Record<string, string> = {
  input: '输入', output: '成果', llm: '模型调用', agent: 'Agent',
  supervisor: '多 Agent 协作', tool: '调用工具', code: '沙箱代码',
  branch: '条件分支', loop: '循环', subgraph: '子工作流',
  memory: '长期记忆', retrieve: '知识检索', transform: '数据整形',
  human: '人工介入', validate: '结构校验', metrics: '口径卡',
}

// 只对开发者有意义、对使用者是噪音的。不是丢弃事件本身（原始事件另有视图），
// 只是不进这条给人看的流。
const SILENT = new Set(['usage', 'node.skipped'])

const num = (v: unknown): number | undefined =>
  typeof v === 'number' && Number.isFinite(v) ? v : undefined

/**
 * 耗时，没有可显示内容时给 undefined 而不是空串。
 *
 * 差别不是洁癖：Step.meta 是可选字段，赋成 `''` 之后"没测到耗时"和
 * "测到了但不值一提"就分不开了，任何想据此判断的地方都得先猜。
 */
const dur = (ms?: number): string | undefined => formatDuration(ms) || undefined

export function formatDuration(ms?: number): string {
  if (ms == null) return ''
  // 低于 10ms 不显示。一屏全是"0ms"看着像每步都被精确计时了，实际上只是
  // 这些步骤（输入、成果这类纯赋值节点）根本没花时间——把没有信息量的数字
  // 摆出来，反而把真正慢的那一步淹没了
  if (ms < 10) return ''
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60_000)}m${Math.round((ms % 60_000) / 1000)}s`
}

/**
 * 这次运行是不是正停在人工介入上。
 *
 * 从步骤推，不要去查审批列表——那是 4 秒轮询一次的，而 run.interrupted
 * 事件是即时到的。用列表的话，中断后会有几秒钟界面说"这次运行已结束"，
 * 而实际上它正等着你点通过。
 */
export function isAwaitingHuman(steps: Step[]): boolean {
  return steps.some(function walk(s): boolean {
    return s.status === 'waiting' || (s.children?.some(walk) ?? false)
  })
}

/** run 到终态时，所有还挂着"进行中"的生命周期行都要收尾。 */
function closeLifecycles(out: Step[], status: StepStatus) {
  out.forEach((s) => {
    if (s.kind === 'lifecycle' && s.status === 'running') s.status = status
  })
}

/** 从 db_query 的结果预览里抠出行数——用户关心的是"查到多少"，不是 JSON。 */
function rowCountOf(preview: string): number | undefined {
  const m = preview.match(/"row_count":\s*(\d+)/)
  return m ? Number(m[1]) : undefined
}

export interface ResultTable {
  columns: string[]
  rows: unknown[][]
  /** 查询本身撞了行数上限——数据库里还有更多，是 guard 没让它全取回来 */
  truncated: boolean
  /** 这份预览被按字符数切断了——取回来的行数比这里显示的多，只是没存下来 */
  clipped?: boolean
}

/**
 * 把查询结果预览转成小表格能用的形状；解析不了就算了，原样展示。
 *
 * 必须容忍**被截断的 JSON**。后端的预览是按字符数硬切的（成果字段 2000、
 * 工具结果 4000），切点落在 JSON 中间是常态而不是例外——严格 JSON.parse
 * 对这类值一律失败，于是最典型的一次取数运行，最终成果会以满屏
 * `\"attribute01\", \"attribute02\"` 的形式呈现。那不是"降级展示"，那是
 * 什么都没展示。所以解析失败时回退到"截到最后一条完整记录"再解析。
 */
export function parseQueryResult(preview: string): ResultTable | null {
  const text = preview.trim()
  if (!text) return null

  // 值常常被 JSON 编码过一层（字符串里套字符串），截断后连外层引号都收不了口
  const unwrapped = text.startsWith('"') ? unescapeJsonString(text) : text
  if (!unwrapped.includes('"columns"')) return null

  const direct = tryTable(unwrapped)
  if (direct) return direct

  const repaired = cutAtLastCompleteRow(unwrapped)
  if (repaired) {
    const table = tryTable(repaired)
    if (table) return { ...table, clipped: true }
  }
  return null
}

function tryTable(text: string): ResultTable | null {
  try {
    const data = JSON.parse(text)
    if (Array.isArray(data?.columns) && Array.isArray(data?.rows)) {
      return { columns: data.columns, rows: data.rows, truncated: !!data.truncated }
    }
  } catch {
    /* 交给调用方决定要不要修 */
  }
  return null
}

/** 去掉外层 JSON 字符串的引号和转义。截断的串 JSON.parse 不了，只能手工拆。 */
function unescapeJsonString(text: string): string {
  const body = text.slice(1)
  return body.replace(/\\(u[0-9a-fA-F]{4}|.)/g, (_, c: string) => {
    if (c[0] === 'u') return String.fromCharCode(parseInt(c.slice(1), 16))
    return { n: '\n', t: '\t', r: '\r', b: '\b', f: '\f' }[c] ?? c
  })
}

/**
 * 把截断的 `{"columns":[...],"rows":[[...],[...],[...`
 * 砍到最后一条完整记录后补上收尾括号。
 */
function cutAtLastCompleteRow(text: string): string | null {
  const start = text.indexOf('"rows"')
  if (start < 0) return null
  const open = text.indexOf('[', start)
  if (open < 0) return null

  let depth = 0
  let inString = false
  let escaped = false
  let lastRowEnd = -1

  for (let i = open; i < text.length; i += 1) {
    const ch = text[i]
    if (escaped) { escaped = false; continue }
    if (ch === '\\') { escaped = true; continue }
    if (ch === '"') { inString = !inString; continue }
    if (inString) continue
    if (ch === '[' || ch === '{') depth += 1
    else if (ch === ']' || ch === '}') {
      depth -= 1
      // depth 回到 1 表示一条记录刚闭合（0 是 rows 数组本身）
      if (depth === 1) lastRowEnd = i + 1
      else if (depth === 0) return text.slice(0, i + 1) + '}'   // rows 其实是完整的
    }
  }
  if (lastRowEnd < 0) return null
  return text.slice(0, lastRowEnd) + ']}'
}

/**
 * 一次运行的一句话摘要，给列表用。
 *
 * 运行列表原本每行只有"临时图 · 1.2s · 340 tok"——三条运行长得一模一样，
 * 要分清哪条是哪条只能挨个点开。人记得住的是内容（问了什么、出了什么），
 * 不是耗时。
 *
 * 优先成果：跑完了的话，"查到多少"比"问了什么"更能认出这一条。
 */
export function summarizeRun(
  input?: Record<string, any> | null,
  output?: Record<string, any> | null,
): string {
  const pick = (obj?: Record<string, any> | null) => {
    if (!obj) return ''
    for (const [k, v] of Object.entries(obj)) {
      if (k.startsWith('_')) continue
      if (v == null || v === '') continue
      if (typeof v === 'string') {
        const table = parseQueryResult(v)
        if (table) {
          return `${table.rows.length}${table.clipped ? '+' : ''} 行 × ${table.columns.length} 列`
        }
        return v.replace(/\s+/g, ' ').slice(0, 70)
      }
      if (typeof v === 'object') {
        const table = parseQueryResult(JSON.stringify(v))
        if (table) return `${table.rows.length} 行 × ${table.columns.length} 列`
        const text = JSON.stringify(v)
        if (text !== '{}' && text !== '[]') return text.slice(0, 70)
        continue
      }
      return String(v).slice(0, 70)
    }
    return ''
  }
  return pick(output) || pick(input)
}

/**
 * agent 步骤的配对键。
 *
 * 光用 agent 名不够：supervisor 会在多轮里反复派给同一个 agent，第二轮的
 * end 会错配到第一轮的 start 上。round 才是区分轮次的那一维。
 */
function agentKey(nodeId: string | undefined, d: any): string {
  return [nodeId ?? '_', String(d?.agent ?? ''), String(d?.round ?? '')].join('|')
}

function toolStep(seq: number, tool: string, args: Record<string, any>): Step {
  const base = { id: `tool-${seq}`, seq, status: 'running' as StepStatus }
  // 数据库工具单独认：它是这个产品最主要的取数方式，"调用 db_query__warehouse"
  // 这种说法等于没翻译
  if (tool.startsWith('db_query')) {
    const source = tool.replace(/^db_query__/, '')
    return { ...base, kind: 'query', title: `在 ${source} 上查询数据`,
             detail: String(args?.sql ?? '') }
  }
  if (tool.startsWith('db_schema')) {
    const table = String(args?.table ?? '')
    return { ...base, kind: 'schema',
             title: table ? `查看 ${table} 的字段` : '列出有哪些表' }
  }
  const argText = Object.keys(args ?? {}).length
    ? JSON.stringify(args, null, 2) : ''
  return { ...base, kind: 'tool', title: `调用 ${tool}`, detail: argText }
}

/** 低于这个毫秒数的节点不参与并行分组：瞬时节点谁跟谁都"重叠"，是噪音不是信息 */
const CONCURRENT_MIN_MS = 30
/** 开始时刻相差在这个数以内算"同一批出发"。实测 fan-out 的几路相差不到 1ms */
const SAME_WAVE_MS = 250

/**
 * 认出哪几个节点是**同时**跑的，把它们收进一个分组里。
 *
 * 图上一个节点连出多条边就是 fan-out，LangGraph 会在同一个 superstep 里并发
 * 执行它们——这是真并发（实测 3 个各 sleep 2 秒的节点，全部 +0.00s 开始、
 * +2.37s 结束，墙钟 2.6 秒）。但在时间线上它们只是穿插出现的几行，
 * "这三路是同时跑的、因此省下了 3.6 秒"这件事一个字都没说。
 *
 * 判定用时间跨度重叠，而不是看事件顺序——顺序只说明"先后发出"，
 * 说明不了"同时在跑"。
 *
 * 判据是"同一批出发"：fan-out 的几路由 LangGraph 在同一个 superstep 里启动，
 * 开始时刻相差以毫秒计。**不能只看跨度重叠**——三路同时开始但先后结束时，
 * 长的那条在时间上确实"包含"短的，按包含关系去排会把先跑完的那路漏掉
 * （实测 leg0 [0.05,2.21] / leg1 [0.05,3.01] / leg2 [0.05,1.51]，
 * 漏的就是 leg2）。
 *
 * 子工作流那种真嵌套则是父节点明显更早开始、更晚结束，出发时刻对不上。
 */
function groupConcurrent(
  out: Step[], spans: Map<string, { start: number; end: number; ms: number }>,
): void {
  const idx = new Map<Step, number>()
  out.forEach((s, i) => idx.set(s, i))

  const candidates = out.filter((s) => {
    const sp = s.nodeId ? spans.get(s.nodeId) : undefined
    return !!sp && sp.ms >= CONCURRENT_MIN_MS
  })
  if (candidates.length < 2) return

  const together = (a: Step, b: Step): boolean => {
    const x = spans.get(a.nodeId!)!
    const y = spans.get(b.nodeId!)!
    if (x.start >= y.end || y.start >= x.end) return false      // 压根没重叠
    return Math.abs(x.start - y.start) * 1000 <= SAME_WAVE_MS   // 同一批出发
  }

  const used = new Set<Step>()
  const groups: Step[][] = []
  for (const a of candidates) {
    if (used.has(a)) continue
    const g = [a]
    for (const b of candidates) {
      if (b === a || used.has(b)) continue
      // 和组里每一个都重叠才算同一批，否则 A|B 重叠、B|C 重叠但 A|C 不重叠
      // 也会被串成一组，而那三个并不是同时在跑
      if (g.every((m) => together(m, b))) g.push(b)
    }
    if (g.length > 1) { g.forEach((m) => used.add(m)); groups.push(g) }
  }

  for (const g of groups) {
    g.sort((a, b) => (idx.get(a) ?? 0) - (idx.get(b) ?? 0))
    const sp = g.map((m) => spans.get(m.nodeId!)!)
    const wallMs = Math.round(
      (Math.max(...sp.map((x) => x.end)) - Math.min(...sp.map((x) => x.start))) * 1000)
    const sumMs = sp.reduce((acc, x) => acc + x.ms, 0)
    const at = Math.min(...g.map((m) => out.indexOf(m)))
    const first = g[0]
    for (const m of g) out.splice(out.indexOf(m), 1)
    out.splice(at, 0, {
      id: `par-${first.id}`, seq: first.seq, kind: 'branch', status: 'done',
      title: `${g.length} 路并行`,
      // 省下多少是这一组存在的理由，放 meta 里一眼看得到
      meta: sumMs > wallMs
        ? `合计 ${dur(sumMs)}，实际 ${dur(wallMs)}`
        : dur(wallMs),
      children: g,
    })
  }
}

/**
 * 把一次运行的事件解码成步骤树。
 *
 * 顶层是节点，节点内部的模型/工具调用是子步骤。没有 node_id 的事件
 * （run.*、issuance 这类）留在顶层，按时间顺序穿插。
 */
export function decodeRun(events: RunEvent[]): Step[] {
  const out: Step[] = []
  /** node_id → 该节点的顶层 Step，用于把子步骤挂进去 */
  const nodeSteps = new Map<string, Step>()
  /** call_id（或工具名兜底）→ 待配对的工具 Step */
  const pendingTools = new Map<string, Step>()
  /** node_id → 待配对的模型调用 Step */
  const pendingLlm = new Map<string, Step>()
  /** supervisor 里待配对的单个 agent 步骤 */
  const pendingAgents = new Map<string, Step>()
  /** node_id → 协作团队的泳道数据。边解码边攒，最后挂到该节点的顶层 Step 上 */
  const teams = new Map<string, TeamRun>()
  /** node_id → 起止时刻。用来事后认出"哪几个节点是同时跑的" */
  const spans = new Map<string, { start: number; end: number; ms: number }>()

  const teamOf = (nodeId: string | undefined): TeamRun | null => {
    if (!nodeId) return null
    let t = teams.get(nodeId)
    if (!t) {
      t = { members: [], rounds: [], savedMs: 0, finished: false }
      teams.set(nodeId, t)
    }
    return t
  }

  const roundOf = (team: TeamRun, round: number): TeamRound => {
    let r = team.rounds.find((x) => x.round === round)
    if (!r) {
      r = { round, parallel: 1, wallMs: 0, sumMs: 0, members: [] }
      team.rounds.push(r)
      team.rounds.sort((a, b) => a.round - b.round)
    }
    return r
  }
  /**
   * node_id → 这个节点当前那条尚未闭合的审批步骤。
   *
   * 审批是"开→闭"配对的，不能只靠内容去重。同一次中断会发三条事件
   * （human.requested、run.interrupted，恢复后节点重放又来一条
   * human.requested），三条内容完全一样；而一个循环里连续三轮审批
   * （驳回 → 改写 → 再审）内容也完全一样。按内容去重的话，要么把重放
   * 算成新的一轮，要么把真实的第二轮当成重放吞掉——两种都错。
   *
   * 开着就是同一次，闭了才是新一次。
   */
  const openInterrupts = new Map<string, Step>()

  const push = (step: Step, nodeId?: string) => {
    const parent = nodeId ? nodeSteps.get(nodeId) : undefined
    if (parent) (parent.children ??= []).push(step)
    else out.push(step)
  }

  /** 把决定折进那条审批行，而不是另起一行——"问了什么 → 你怎么答的"是一件事 */
  const closeInterrupt = (key: string, approved: unknown, note: string): boolean => {
    const step = openInterrupts.get(key)
    if (!step) return false
    step.status = 'done'
    step.level = undefined
    step.title = `${step.title} → ${approved === false ? '你驳回了' : '你放行了'}`
    if (note) step.detail = [step.detail, `你的备注：${note}`].filter(Boolean).join('\n')
    openInterrupts.delete(key)
    return true
  }

  /** 上一条就是恢复事件——用来认出紧随其后的那条重复的"继续执行" */
  let justResumed = false

  for (const event of events) {
    const type = String(event.type)
    if (EPHEMERAL.has(type) || SILENT.has(type)) continue
    const d: any = event.data ?? {}
    const seq = event.seq ?? 0
    const nodeId = event.node_id ?? undefined
    // 每条事件都会把"刚恢复过"清掉；只有恢复分支会重新点亮它，
    // 所以这个标记只在紧挨着的下一条上为真
    const wasJustResumed = justResumed
    justResumed = false

    switch (type) {
      case 'run.started':
      case 'run.resumed': {
        const resumed = type === 'run.resumed' || !!d.resumed
        // 恢复一次会紧挨着发两条：run.resumed 和 run.started(resumed=true)。
        // 说两遍"继续执行"会让人以为恢复了两次。
        //
        // 判据是"这两条是不是紧挨着的"，不是"界面上还有没有待办"——后者
        // 曾经能用，但只要多一处会产生 waiting 状态的地方（比如把被中断的
        // 节点也标成等待），它就失效了。相邻性是这两条事件的固有性质，
        // 不会因为别处改了展示而变。
        if (resumed && wasJustResumed) break
        if (resumed) {
          justResumed = true
          // 人已经答过了，那条不该再是橙色的"等你确认"。但配对关系要留着：
          // 紧接着 LangGraph 会重放该节点，又发一条一模一样的
          // human.requested，配对还开着才能认出那是重放而不是新一轮。
          // 真正的闭合（把决定折进标题）交给带 node_id 的 human.resolved。
          openInterrupts.forEach((s) => {
            if (s.status === 'waiting') { s.status = 'done'; s.level = undefined }
          })
        }
        out.push({
          id: `s-${seq}`, seq, kind: 'lifecycle', status: 'running',
          title: resumed ? '继续执行' : `开始执行（${d.nodes ?? '?'} 步）`,
        })
        break
      }

      case 'node.started': {
        // 恢复后节点重放，它从"等你"回到"在跑"（下面会复用同一条 Step）
        // data.label 是节点标题**随事件传递的唯一来源**。靠画布 nodes 反查的话，
        // 助手栏和运行页没加载画布，标题会退化成裸 node_id。
        // label 是节点标题随事件传递的唯一来源，但没起过名字的节点它等于
        // 节点 id（后端兜底），"in"/"h"/"out" 对用户毫无意义——退到节点
        // 类型的中文名，至少能看出这步在干嘛
        const label = String(d.label ?? '')
        const typeName = TYPE_LABEL[String(d.node_type ?? '')]
        const title = (label && label !== nodeId ? label : typeName) || nodeId || '执行步骤'
        // 人工审批恢复后 LangGraph 会重放该节点，node.started 因此来第二遍。
        // 那不是"又执行了一个步骤"，是同一步继续——复用原来那条，
        // 否则时间线上每审批一次就多一个同名节点。
        if (nodeId && !spans.has(nodeId)) {
          spans.set(nodeId, { start: event.ts ?? 0, end: event.ts ?? 0, ms: 0 })
        }
        const existing = nodeId ? nodeSteps.get(nodeId) : undefined
        if (existing) {
          existing.status = 'running'
          break
        }
        const step: Step = {
          id: `n-${nodeId ?? seq}`, seq, kind: 'node', nodeId, title, status: 'running',
        }
        if (nodeId) nodeSteps.set(nodeId, step)
        out.push(step)
        break
      }

      case 'node.finished': {
        // 人工节点跑完就说明决定已经生效了。正常路径上 human.resolved 早就
        // 闭合了配对，这里只是兜底：万一那条事件没发出来（旧运行、别的
        // 审批来源），配对不能一直挂着——挂着的话这个节点下一轮真实审批
        // 会被当成重放吞掉
        if (nodeId && openInterrupts.has(nodeId)) {
          const p = d.preview ?? {}
          closeInterrupt(nodeId,
            typeof p === 'object' ? p.approved : undefined,
            typeof p === 'object' ? String(p.note ?? '') : '')
        }
        const span = nodeId ? spans.get(nodeId) : undefined
        if (span) {
          span.end = event.ts ?? span.end
          span.ms = num(d.duration_ms) ?? Math.round((span.end - span.start) * 1000)
        }
        const step = nodeId ? nodeSteps.get(nodeId) : undefined
        if (step) {
          step.status = 'done'
          step.meta = dur(num(d.duration_ms))
          step.artifact = d.artifact || step.artifact
          // 空对象/空串不是"详情"，显示出来只是噪音
          const raw = d.preview
          const empty = raw == null || raw === '' ||
            (typeof raw === 'object' && Object.keys(raw).length === 0)
          if (!empty) {
            const preview = typeof raw === 'string' ? raw : JSON.stringify(raw, null, 2)
            step.detail = preview.slice(0, 4000)
          }
        }
        break
      }

      case 'node.failed': {
        const step = nodeId ? nodeSteps.get(nodeId) : undefined
        if (step) {
          step.status = 'failed'
          step.level = 'error'
          step.detail = String(d.error ?? '')
          step.meta = dur(num(d.duration_ms))
        } else {
          push({ id: `e-${seq}`, seq, kind: 'error', level: 'error',
                 title: String(d.error ?? '这一步失败了'), nodeId }, nodeId)
        }
        break
      }

      case 'llm.start':
        pendingLlm.set(nodeId ?? '_', {
          id: `llm-${seq}`, seq, kind: 'llm', title: '思考并作答',
          status: 'running', nodeId,
        })
        break

      case 'llm.end': {
        const step = pendingLlm.get(nodeId ?? '_')
        if (step) {
          step.status = 'done'
          // 不给 token 数和美元——那是账单视角，不是"它干了什么"。
          // 成本在运行详情的用量区单独看。
          step.meta = dur(num(d.duration_ms))
          push(step, nodeId)
          pendingLlm.delete(nodeId ?? '_')
        }
        break
      }

      case 'llm.thinking': {
        // 只有 Anthropic 系模型会产出（thinking_text 认的是 thinking 块）。
        // 换个 provider 就没有——所以它是增强信息，主干靠上面那些步骤撑着。
        const text = String(d.text ?? d.delta ?? '')
        if (!text.trim()) break
        push({
          id: `th-${seq}`, seq, kind: 'think', nodeId, status: 'done',
          title: text.replace(/\s+/g, ' ').slice(0, 60) + (text.length > 60 ? '…' : ''),
          detail: text + (d.truncated ? '\n\n（已截断）' : ''),
        }, nodeId)
        break
      }

      case 'tool.start': {
        const step = toolStep(seq, String(d.tool ?? ''), d.args ?? {})
        step.nodeId = nodeId
        // call_id 才是可靠的配对键：同一节点并发调同名工具时，按名字配会错位。
        // 独立 Tool 节点不带 call_id，回退到工具名。
        pendingTools.set(String(d.call_id || d.tool || seq), step)
        push(step, nodeId)
        break
      }

      case 'tool.end':
      case 'tool.error': {
        const key = String(d.call_id || d.tool || '')
        const step = pendingTools.get(key)
        const preview = String(d.preview ?? d.error ?? '')
        if (step) {
          const failed = type === 'tool.error' || preview.startsWith('SQL 被拒绝')
            || preview.startsWith('查询失败')
          step.status = failed ? 'failed' : 'done'
          if (failed) step.level = 'error'
          // tool.error 只有 {tool,error}，没有 duration_ms/preview——不能假设统一形状
          const rows = rowCountOf(preview)
          const parts = [
            rows != null ? `${rows} 行` : '',
            formatDuration(num(d.duration_ms)),
          ].filter(Boolean)
          step.meta = parts.join(' · ') || undefined
          step.artifact = d.artifact
          if (step.kind === 'query' || step.kind === 'schema' || failed) {
            // 查询保留 SQL 作为 detail，结果另挂；失败时结果就是错误原因
            step.detail = failed ? `${step.detail ?? ''}\n\n${preview}`.trim() : step.detail
            step.result = preview
          } else {
            step.detail = preview.slice(0, 4000)
          }
          pendingTools.delete(key)
        }
        break
      }

      case 'sandbox.start':
        pendingTools.set(`sandbox-${nodeId ?? seq}`, (() => {
          const step: Step = {
            id: `sb-${seq}`, seq, kind: 'code', nodeId, status: 'running',
            title: `运行${d.language === 'python' ? ' Python' : d.language ? ` ${d.language}` : ''}代码`,
            // 渲染后的代码。编辑器里那份带着 {{ }}，送进沙箱的是替换完的——
            // 值里有引号或换行就会把程序写坏，而报错说的是渲染后的行号。
            // 只有插过值的才值得摆出来，没插值的两份一模一样，显示等于噪音
            detail: d.interpolated && d.code
              ? `# 实际执行的代码（模板已展开）\n${String(d.code)}`
                + (d.code_truncated ? '\n\n（已截断）' : '')
              : undefined,
          }
          push(step, nodeId)
          return step
        })())
        break

      case 'sandbox.end': {
        const step = pendingTools.get(`sandbox-${nodeId ?? seq}`)
        if (step) {
          step.status = d.ok ? 'done' : 'failed'
          if (!d.ok) step.level = 'error'
          step.meta = dur(num(d.duration_ms))
          const body = [d.stdout, d.stderr].filter(Boolean).join('\n').slice(0, 4000)
          // 和查询步骤同一个分工：detail 是"跑的是什么"（渲染后的代码），
          // result 是"跑出了什么"。塞进同一个字段的话，报错时最需要的两样
          // 东西——实际代码和 traceback——只能看见一样
          step.result = body || (d.ok ? '（无输出）' : `退出码 ${d.exit_code}`)
          if (!step.detail) step.detail = step.result
          pendingTools.delete(`sandbox-${nodeId ?? seq}`)
        }
        break
      }

      case 'edge.taken':
        push({
          id: `br-${seq}`, seq, kind: 'branch', nodeId, status: 'done',
          title: `走「${d.branch}」这条路`
            + (d.iteration != null ? `（第 ${Number(d.iteration) + 1} 轮）` : ''),
          detail: d.reason ? String(d.reason) : undefined,
        }, nodeId)
        break

      case 'human.requested':
      case 'run.interrupted': {
        // 同一次中断发三条事件：human.requested、run.interrupted，恢复后
        // 节点重放又来一条 human.requested（LangGraph 的重放语义，不是 bug）。
        // 三条都指向同一次"等你确认"，界面上只该有一条。
        const payload = d.payload ?? d
        const key = String(payload.node_id ?? nodeId ?? '_')
        if (openInterrupts.has(key)) break   // 同一次中断的后续事件
        const step: Step = {
          id: `hm-${seq}`, seq, kind: 'human', nodeId, status: 'waiting', level: 'warn',
          title: String(payload.title || d.title || '等你确认'),
          detail: [payload.message, payload.tool ? `工具：${payload.tool}` : '']
            .filter(Boolean).join('\n') || undefined,
        }
        openInterrupts.set(key, step)
        push(step, nodeId)
        // 承载它的那个节点也不是"在跑"——它停下来等人了。转着蓝圈说的是
        // "在忙，你等着"，而实际情况正相反：它在等你
        const host = nodeId ? nodeSteps.get(nodeId) : undefined
        if (host && host.status === 'running') host.status = 'waiting'
        break
      }

      case 'human.resolved': {
        const r = d.response ?? {}
        const approved = typeof r === 'object' ? r.approved : undefined
        const note = typeof r === 'object' ? String(r.note ?? '') : ''
        if (closeInterrupt(String(nodeId ?? '_'), approved, note)) break
        // 没有对应的待决审批（历史事件不全、或者审批发生在别处）——
        // 还是要把决定说出来，只是没地方折进去
        push({
          id: `hr-${seq}`, seq, kind: 'human', nodeId, status: 'done',
          title: approved === false ? '你驳回了' : '你放行了',
          detail: note || undefined,
        }, nodeId)
        break
      }

      case 'issuance': {
        const tier = String(d.tier ?? '')
        const label = { formal: '正式出具', degraded: '降档出具', withheld: '不予出具' }[tier] ?? tier
        const gaps: string[] = []
        if (d.missing_required?.length) gaps.push(`缺必需指标 ${d.missing_required.join('、')}`)
        if (d.missing_expected?.length) gaps.push(`缺期望指标 ${d.missing_expected.join('、')}`)
        if (num(d.unmatched)) gaps.push(`${d.unmatched} 个数字无法回指指标集`)
        out.push({
          id: `is-${seq}`, seq, kind: 'issuance', status: 'done',
          level: tier === 'formal' ? 'info' : tier === 'withheld' ? 'error' : 'warn',
          title: gaps.length ? `${label}：${gaps.join('；')}` : label,
        })
        break
      }

      case 'caliber.upgrade':
        out.push({
          id: `cu-${seq}`, seq, kind: 'note', level: 'warn', status: 'done',
          title: `口径卡 v${d.pinned} → v${d.latest} 有新版，按「${d.policy_label ?? d.policy}」处置`,
        })
        break

      case 'agent.step.start': {
        // 和 tool.start/tool.end 一样是一件事的两个时刻。拆成两行的话，
        // "派给 researcher 什么任务"和"它回了什么"会变成两条互不相干的记录，
        // 而且 start 那条永远停在转圈——多 agent 节点跑完了它还在转。
        const step: Step = {
          id: `as-${seq}`, seq, kind: 'note', nodeId, status: 'running',
          title: `${d.agent}：${String(d.instruction ?? '').slice(0, 80)}`,
        }
        pendingAgents.set(agentKey(nodeId, d), step)
        push(step, nodeId)

        const team = teamOf(nodeId)
        if (team) {
          const name = String(d.agent ?? '')
          if (name && !team.members.includes(name)) team.members.push(name)
          const r = roundOf(team, num(d.round) ?? 0)
          r.parallel = Math.max(r.parallel, num(d.parallel) ?? 1)
          r.members.push({
            agent: name, instruction: String(d.instruction ?? ''),
            ms: 0, status: 'running',
          })
        }
        break
      }

      case 'agent.step.end': {
        const key = agentKey(nodeId, d)
        const step = pendingAgents.get(key)
        const preview = String(d.preview ?? '').slice(0, 2000)
        if (step) {
          step.status = 'done'
          step.meta = dur(num(d.duration_ms))
          step.result = preview || undefined
          pendingAgents.delete(key)
        } else {
          push({
            id: `ae-${seq}`, seq, kind: 'note', nodeId, status: 'done',
            title: `${d.agent} 回复`, meta: dur(num(d.duration_ms)),
            detail: preview,
          }, nodeId)
        }

        const team = teamOf(nodeId)
        if (team) {
          const r = roundOf(team, num(d.round) ?? 0)
          const name = String(d.agent ?? '')
          const ms = num(d.duration_ms) ?? 0
          const m = r.members.find((x) => x.agent === name && x.status === 'running')
            ?? (r.members.push({ agent: name, instruction: '', ms: 0, status: 'running' }),
                r.members[r.members.length - 1])
          m.ms = ms
          m.status = 'done'
          m.result = preview || undefined
          // 这一轮实际花的是最慢那个，各人之和减去它就是省下的
          r.wallMs = Math.max(...r.members.map((x) => x.ms))
          r.sumMs = r.members.reduce((acc, x) => acc + x.ms, 0)
          team.savedMs = team.rounds.reduce((acc, x) => acc + Math.max(0, x.sumMs - x.wallMs), 0)
        }
        break
      }

      case 'log': {
        const level = String(d.level ?? 'info')
        // supervisor 的调度决策后端标成了 info，但它不是排查用的日志——
        // "为什么派给 researcher"、"为什么只跑一轮就 FINISH"，不显示的话
        // 多 agent 节点在界面上就是一个跑了 31 秒的黑盒。带 round 字段的
        // 就是它，和普通 info 日志区分得开
        if (level === 'info' && d.round != null) {
          const text = String(d.message ?? '')
          const round = (num(d.round) ?? 0) + 1
          // 优先读结构化字段。以前只有一句中文消息串，这里得拿正则去拆
          // 「调度 → X（理由）」——后端文案一改就散架，而文案是会改的。
          // 老运行的事件没有这些字段，正则那条路留着兜底
          const structured = Array.isArray(d.agents)
          const m = text.match(/^调度\s*→\s*([^（(]+)[（(](.*)[）)]\s*$/)
          const legacyTarget = m?.[1].trim() ?? ''
          const agents: string[] = structured
            ? (d.agents as unknown[]).map(String)
            : (legacyTarget && legacyTarget !== 'FINISH' ? [legacyTarget] : [])
          const done = structured ? !!d.done : legacyTarget === 'FINISH'
          const reason = structured ? String(d.reason ?? '') : (m?.[2].trim() ?? '')
          const team = teamOf(nodeId)
          if (team) {
            if (done) team.finished = true
            else if (reason) roundOf(team, num(d.round) ?? 0).reason = reason
          }

          // FINISH 是协议里的收尾标记，不是某个 agent。"交给 FINISH"
          // 会让人以为还有个叫 FINISH 的成员
          const title = done ? `第 ${round} 轮：结束协作`
            : agents.length > 1 ? `第 ${round} 轮：${agents.join('、')} 同时进行`
            : agents.length === 1 ? `第 ${round} 轮：交给 ${agents[0]}`
            : text
          push({
            id: `sv-${seq}`, seq, kind: 'branch', nodeId, status: 'done',
            title, detail: reason || undefined,
          }, nodeId)
          break
        }
        // 其余 info 是给排查用的，不进主流程——但 warn/error 用户必须看到
        if (level === 'info') break
        push({
          id: `lg-${seq}`, seq, kind: 'note', nodeId, status: 'done',
          level: level === 'error' ? 'error' : 'warn',
          title: String(d.message ?? ''),
        }, nodeId)
        break
      }

      case 'run.failed': {
        const msg = String(d.error ?? '运行失败')
        // 节点失败会先报一次，run.failed 往往是同一句话——说两遍不会让人
        // 更明白，只会让人以为出了两个错
        const dup = out.some((s) => s.status === 'failed' &&
          (s.detail === msg || s.title === msg))
        closeLifecycles(out, 'failed')
        if (!dup) {
          out.push({
            id: `rf-${seq}`, seq, kind: 'error', level: 'error', status: 'failed', title: msg,
          })
        }
        break
      }

      case 'run.cancelled':
        closeLifecycles(out, 'failed')
        out.push({ id: `rc-${seq}`, seq, kind: 'lifecycle', status: 'failed', title: '已取消' })
        break

      case 'run.finished': {
        // 开头那条"开始执行"要收尾，否则跑完了还挂着一个转圈的图标。
        // 注意是全部而不是第一条：审批恢复过的运行有"开始执行"+"继续执行"
        // 两条，只收第一条会让"继续执行"永远转圈——明明已经完成了。
        closeLifecycles(out, 'done')
        out.push({
          id: `rd-${seq}`, seq, kind: 'lifecycle', status: 'done',
          title: '完成', meta: dur(num(d.duration_ms)),
        })
        break
      }

      case 'memory.end': {
        // 写入要把**记了什么**原样显示出来。只说"写入 1 条"等于没说——
        // 用户没法判断系统记的是不是他想让它记的，而这东西会影响以后每次对话
        const action = String(d.action ?? '')
        const count = num(d.count) ?? 0
        const content = String(d.content ?? '')
        if (action === 'write') {
          push({
            id: `mw-${seq}`, seq, kind: 'note', nodeId, status: 'done',
            title: content ? `记住了：${content.slice(0, 40)}${content.length > 40 ? '…' : ''}`
                           : '写入了一条记忆',
            detail: content || undefined,
          }, nodeId)
          break
        }
        if (action === 'clear') {
          push({
            id: `mc-${seq}`, seq, kind: 'note', nodeId, status: 'done', level: 'warn',
            title: `清空了记忆域「${String(d.scope ?? '')}」的 ${count} 条`,
          }, nodeId)
          break
        }
        push({
          id: `mr-${seq}`, seq, kind: 'schema', nodeId,
          status: count ? 'done' : 'failed',
          level: count ? undefined : 'warn',
          title: count ? `想起 ${count} 条相关记忆` : '没有想起相关的记忆',
        }, nodeId)
        break
      }

      case 'retrieve.end': {
        // 知识检索。以前只发一条 info 日志，于是"这句结论依据的是哪份文档的
        // 哪一段"在轨迹里查不到——而 SQL 取数那条路早就能下钻到工件
        const count = num(d.count) ?? 0
        const collection = String(d.collection ?? '')
        const top = num(d.top_score)
        push({
          id: `rt-${seq}`, seq, kind: 'schema', nodeId,
          status: count ? 'done' : 'failed',
          level: d.degraded ? 'warn' : count ? undefined : 'warn',
          title: count
            ? `在「${collection}」里检索到 ${count} 段`
            : `在「${collection}」里没检索到内容`,
          detail: String(d.query ?? ''),
          meta: [
            top != null ? `最高分 ${top}` : '',
            // 退回关键词是"少了一半能力"，不标出来用户只会觉得最近搜得不准
            d.degraded ? '已退回关键词' : '',
          ].filter(Boolean).join(' · ') || undefined,
          artifact: d.artifact,
        }, nodeId)
        break
      }

      default:
        // 新增事件类型时不要静默吞掉——宁可显示一条原始的，也比让人觉得
        // "什么都没发生"好。加了新事件记得回来补一条映射。
        push({
          id: `x-${seq}`, seq, kind: 'note', nodeId, status: 'done',
          title: type, detail: JSON.stringify(d, null, 2).slice(0, 1000),
        }, nodeId)
    }
  }

  // 还没收到 end 的工具/模型调用保持 running——它们正在进行，不是丢了
  groupConcurrent(out, spans)

  // 泳道挂到 supervisor 节点的顶层 Step 上。节点内部那些"第N轮交给谁""X：指令"
  // 仍然留着——泳道给的是一眼看清的形状，那些步骤给的是内容，两者不重复
  for (const [nodeId, team] of teams) {
    if (!team.rounds.length) continue
    const step = nodeSteps.get(nodeId)
    if (step) step.team = team
  }

  return out
}

// -------------------------------------------------------------------------
// Copilot 建图阶段：操作流 → 同一套 Step
// -------------------------------------------------------------------------

export interface CopilotOp {
  op: string
  [k: string]: any
}

/**
 * 一段思考在列表里显示哪一句。
 *
 * 还在想的时候取**最后一句**：思考是流式的，第一句往往是"用户想要一个……"
 * 这种复述，之后几十秒里它其实一直在推进（"先看看现有的节点"、"这里需要
 * 一个分支"）。固定显示开头，等于把一段活的叙述冻在起点上，看着就像卡住了。
 *
 * 想完了取**开头**：这时它是一条历史记录，开头那句最接近"这段在想什么"。
 */
function thinkingHeadline(text: string, live: boolean): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  if (!flat) return '正在思考…'
  if (!live) return flat.slice(0, 60) + (flat.length > 60 ? '…' : '')
  // 按中英文句末切；最后一段往往还没说完，取它前面那句更完整
  const parts = flat.split(/(?<=[。！？；.!?;])\s*/).filter(Boolean)
  const tail = parts.length > 1 && parts[parts.length - 1].length < 6
    ? parts[parts.length - 2]
    : parts[parts.length - 1]
  const line = tail ?? flat
  return line.length > 60 ? '…' + line.slice(-60) : line
}

const PHASE_LABEL: Record<string, string> = {
  connecting: '正在连接模型',
  planning: '正在理解需求、规划步骤',
  building: '正在搭建流程',
  wiring: '正在连接数据流',
  finalizing: '正在排版和校验',
}

/**
 * Copilot 的操作流解码。
 *
 * 和运行事件是两套完全独立的协议（不共享 seq、node_id、不落库），只在这里
 * 统一成同一种 Step，让用户看到的是一条连续的过程，而不是"建图"和"执行"
 * 两段风格迥异的日志。
 */
export function decodeCopilot(ops: CopilotOp[]): Step[] {
  const out: Step[] = []
  let i = 0
  let nodeCount = 0

  /** 一段思考结束了：不再显示"最新一句"，换成开头那句，收掉转圈 */
  const settleThinking = () => {
    const last = out[out.length - 1]
    if (last?.kind === 'think' && last.status === 'running') {
      last.status = 'done'
      last.title = thinkingHeadline(last.detail ?? '', false)
    }
  }

  for (const op of ops) {
    i += 1
    // 除了继续思考和心跳，任何一条操作都说明它已经想完、开始动手了
    if (op.op !== 'thinking' && op.op !== 'heartbeat') settleThinking()
    switch (op.op) {
      case 'thinking': {
        const text = String(op.delta ?? '')
        if (!text.trim()) break
        const last = out[out.length - 1]
        // 思考是连续流，一片 delta 一行会碎成几十条。同一段连续思考并成一条，
        // 详情是全文——这才是"一次思考是一个节点"。
        if (last?.kind === 'think' && last.status === 'running') {
          last.detail = (last.detail ?? '') + text
          last.title = thinkingHeadline(last.detail, true)
        } else {
          out.push({ id: `ct-${i}`, seq: i, kind: 'think', status: 'running',
                     title: thinkingHeadline(text, true), detail: text })
        }
        break
      }
      case 'heartbeat': {
        // 心跳只更新"还活着"的状态，不该每 3 秒堆一行
        const last = out[out.length - 1]
        const label = PHASE_LABEL[String(op.phase)] ?? '正在处理'
        const elapsed = num(op.elapsed_ms)
        if (last?.kind === 'lifecycle' && last.status === 'running') {
          last.title = label
          last.meta = elapsed ? formatDuration(elapsed) : last.meta
        } else {
          out.push({ id: `hb-${i}`, seq: i, kind: 'lifecycle', status: 'running',
                     title: label, meta: elapsed ? formatDuration(elapsed) : undefined })
        }
        break
      }
      case 'plan':
        out.push({ id: `cp-${i}`, seq: i, kind: 'note', status: 'done',
                   title: String(op.summary ?? '想好了怎么做') })
        break
      case 'add_node':
        nodeCount += 1
        out.push({
          id: `cn-${i}`, seq: i, kind: 'node', status: 'done',
          title: String(op.node?.data?.label || op.node?.label || op.node?.id || '新步骤'),
          meta: String(op.node?.type ?? ''),
        })
        break
      case 'update_node':
        out.push({ id: `cu-${i}`, seq: i, kind: 'note', status: 'done',
                   title: `调整了「${op.id}」` })
        break
      case 'remove_node':
        out.push({ id: `cr-${i}`, seq: i, kind: 'note', status: 'done',
                   title: `去掉了「${op.id}」` })
        break
      // 连线不单独成行：用户关心有哪些步骤，不关心箭头
      case 'add_edge':
      case 'remove_edge':
        break
      case 'done':
        // 心跳那条"正在理解需求…"要收尾，否则生成完了它还在转圈
        closeLifecycles(out, 'done')
        out.push({ id: `cd-${i}`, seq: i, kind: 'lifecycle', status: 'done',
                   title: `流程搭好了，加了 ${nodeCount} 步`,
                   detail: String(op.explanation ?? '') || undefined })
        break
      case 'final': {
        // 后端排版校验后的最终图才知道整张图有几步。nodeCount 只是这一轮
        // 新增的数量——在"改图"场景下说"共 2 步"是错的，图上明明有四个节点
        const total = op.graph?.nodes?.length
        const last = out[out.length - 1]
        if (total && last?.kind === 'lifecycle') {
          last.title = nodeCount && nodeCount < total
            ? `流程搭好了，加了 ${nodeCount} 步，整张图共 ${total} 步`
            : `流程搭好了，共 ${total} 步`
        }
        closeLifecycles(out, 'done')
        break
      }
      case 'error':
        closeLifecycles(out, 'failed')
        out.push({ id: `ce-${i}`, seq: i, kind: 'error', level: 'error', status: 'failed',
                   title: String(op.message ?? '生成失败') })
        break
      default:
        break
    }
  }
  return out
}
