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
  /** 已见过的 interrupt_id，避免 human.requested 与 run.interrupted 各出一行 */
  const seenInterrupts = new Set<string>()

  const push = (step: Step, nodeId?: string) => {
    const parent = nodeId ? nodeSteps.get(nodeId) : undefined
    if (parent) (parent.children ??= []).push(step)
    else out.push(step)
  }

  for (const event of events) {
    const type = String(event.type)
    if (EPHEMERAL.has(type) || SILENT.has(type)) continue
    const d: any = event.data ?? {}
    const seq = event.seq ?? 0
    const nodeId = event.node_id ?? undefined

    switch (type) {
      case 'run.started':
      case 'run.resumed': {
        const resumed = type === 'run.resumed' || !!d.resumed
        // 恢复一次会同时来 run.resumed 和 run.started(resumed=true)，
        // 说两遍"继续执行"只会让人以为恢复了两次
        if (resumed && out.some((s) => s.kind === 'lifecycle' && s.title === '继续执行')
            && !out.some((s) => s.status === 'waiting')) {
          break
        }
        if (resumed) {
          // 恢复意味着之前那条"等你确认"已经过去了。level 也要一起清：
          // 留着 warn 的话，那条已经处理完的审批会永远是橙色的，看上去
          // 像还有什么没解决
          const settle = (s: Step) => {
            if (s.status === 'waiting') { s.status = 'done'; s.level = undefined }
          }
          out.forEach(settle)
          nodeSteps.forEach((s) => s.children?.forEach(settle))
        }
        out.push({
          id: `s-${seq}`, seq, kind: 'lifecycle', status: 'running',
          title: resumed ? '继续执行' : `开始执行（${d.nodes ?? '?'} 步）`,
        })
        break
      }

      case 'node.started': {
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
          step.detail = body || (d.ok ? '（无输出）' : `退出码 ${d.exit_code}`)
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
        // 同一次中断会来两条事件，而且审批恢复后节点重放还会再来一遍
        // （LangGraph 的重放语义，不是 bug）。三条都指向同一次"等你确认"，
        // 界面上只该有一个。
        //
        // 不能用 interrupt_id 做键：human.requested 压根没这个字段，
        // run.interrupted 才有。两者共有的是 payload 里的 node_id + mode + title，
        // 用它们才能让三条算出同一个键。
        const payload = d.payload ?? d
        const key = [payload.node_id ?? nodeId, payload.mode ?? '', payload.title ?? ''].join('|')
        if (seenInterrupts.has(key)) break
        seenInterrupts.add(key)
        push({
          id: `hm-${seq}`, seq, kind: 'human', nodeId, status: 'waiting', level: 'warn',
          title: String(payload.title || d.title || '等你确认'),
          detail: [payload.message, payload.tool ? `工具：${payload.tool}` : '']
            .filter(Boolean).join('\n') || undefined,
        }, nodeId)
        break
      }

      case 'human.resolved': {
        const r = d.response ?? {}
        const approved = typeof r === 'object' ? r.approved : undefined
        push({
          id: `hr-${seq}`, seq, kind: 'human', nodeId, status: 'done',
          title: approved === false ? '你驳回了' : '你放行了',
          detail: typeof r === 'object' && r.note ? String(r.note) : undefined,
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

      case 'agent.step.start':
        push({
          id: `as-${seq}`, seq, kind: 'note', nodeId, status: 'running',
          title: `${d.agent}：${String(d.instruction ?? '').slice(0, 80)}`,
        }, nodeId)
        break

      case 'agent.step.end':
        push({
          id: `ae-${seq}`, seq, kind: 'note', nodeId, status: 'done',
          title: `${d.agent} 回复`, meta: dur(num(d.duration_ms)),
          detail: String(d.preview ?? '').slice(0, 2000),
        }, nodeId)
        break

      case 'log': {
        const level = String(d.level ?? 'info')
        // info 级日志是给排查用的，不进主流程——但 warn/error 用户必须看到
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
  return out
}

// -------------------------------------------------------------------------
// Copilot 建图阶段：操作流 → 同一套 Step
// -------------------------------------------------------------------------

export interface CopilotOp {
  op: string
  [k: string]: any
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
  for (const op of ops) {
    i += 1
    switch (op.op) {
      case 'thinking': {
        const text = String(op.delta ?? '')
        if (!text.trim()) break
        const last = out[out.length - 1]
        // 思考是连续流，一片 delta 一行会碎成几十条。同一段连续思考并成一条，
        // 标题取开头、详情是全文——这才是"一次思考是一个节点"。
        if (last?.kind === 'think') {
          last.detail = (last.detail ?? '') + text
          last.title = last.detail.replace(/\s+/g, ' ').slice(0, 60)
            + (last.detail.length > 60 ? '…' : '')
        } else {
          out.push({ id: `ct-${i}`, seq: i, kind: 'think', status: 'done',
                     title: text.replace(/\s+/g, ' ').slice(0, 60), detail: text })
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
        out.push({ id: `cd-${i}`, seq: i, kind: 'lifecycle', status: 'done',
                   title: `流程搭好了，共 ${nodeCount} 步`,
                   detail: String(op.explanation ?? '') || undefined })
        break
      case 'error':
        out.push({ id: `ce-${i}`, seq: i, kind: 'error', level: 'error', status: 'failed',
                   title: String(op.message ?? '生成失败') })
        break
      default:
        break
    }
  }
  return out
}
