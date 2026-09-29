import {
  memo, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState,
  type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type ReactNode, type RefObject,
} from 'react'
import { createPortal } from 'react-dom'
import { Handle, Position, type Edge, type NodeProps } from '@xyflow/react'
import { Sparkles, Wrench } from 'lucide-react'
import clsx from 'clsx'
import { NODE_DEFS, sourceHandles } from './nodeDefs'
import { DENSE_EXITS, NODE_WIDTH } from './routing'
import { LiveClock, stillClock, TeamMatrix, teamBrief, type TeamEnding } from './TeamMatrix'
import { edgeKey, topology } from '../run/derive'
import { api } from '../api/client'
import { isComposing, StatusBadge, toast } from '../components/ui'
import { matchedSource } from '../lib/evidence'
import { explainRunError } from '../lib/explain'
import { formatDuration, formatLapse, formatNumber, formatTokens, NONE, shortId } from '../lib/format'
import { statusMeta } from '../lib/status'
import { claimTally, issuanceLabel, reportStampText, RUN_CLASS_LABEL } from '../lib/terms'
import { useReportStamp, type ReportStamp } from './reportStamp'
import { ApprovalCard } from '../run/RunPanel'
import type { NodeState, NodeTrace } from '../run/trace'
import { useRunClock } from '../run/useRunClock'
import {
  execStart, lastIn, loopSince, loopSpan, openCall, openSince, repairsOf, useNodeFacts, useNodeView, within,
  type NodeFacts, type NodeUsage, type NodeView, type ToolSpan,
} from '../run/useNodeView'
import { useCatalog } from '../store/catalog'
import { configSig, useStudio, type FlowNode } from '../store/studio'
import type { Approval, NodeType, RunEvent, ValidationIssue } from '../types'

/** 卡片正文的一行摘要：不用打开属性面板就能看懂这个节点在干什么。 */
function summarize(type: NodeType, config: Record<string, any>): string {
  const first = (...vals: any[]) => vals.find((v) => typeof v === 'string' && v.trim())?.trim() ?? ''
  switch (type) {
    case 'input':
      return (config.fields ?? []).map((f: any) => f.name).filter(Boolean).join(' · ') || '未定义输入'
    case 'output': {
      const names = (config.fields ?? []).map((f: any) => f.name).filter(Boolean).join(' · ')
      return (config.contract ? '⚖ 出具契约 · ' : '') + (names || '未定义成果')
    }
    case 'llm':
      return first(config.prompt, config.system) || '未填提示'
    case 'agent': {
      const tools = (config.tools ?? []).length
      return `${first(config.prompt, config.system) || '未填任务'}${tools ? ` · ${tools} 个工具` : ''}`
    }
    case 'supervisor':
      return `${(config.agents ?? []).map((a: any) => a.name).join(' / ') || '未配成员'}`
    case 'tool':
      return config.tool ? `${config.tool}()` : '未选择工具'
    case 'code':
      return `${config.language ?? 'python'} · ${(config.code ?? '').split('\n')[0].slice(0, 48) || '空'}`
    case 'branch':
      return (config.cases ?? []).map((c: any) => c.key).filter(Boolean).join(' / ') || '未配分支'
    case 'loop':
      return config.mode === 'while'
        ? `当 ${config.condition || '?'} 时重复`
        : `遍历 ${config.items || '?'}`
    case 'retrieve':
      return `${config.collection || 'default'} · 取 ${config.limit ?? 5} 条`
    case 'memory':
      return `${{ recall: '回忆', write: '记住', clear: '清空' }[config.action as string] ?? ''} @ ${config.scope || 'default'}`
    case 'human':
      // 兜底不写「等待人工」：挂在还没跑的卡上读着像一个状态
      return first(config.title) || '人工审批 · 未填标题'
    case 'validate':
      return 'JSON Schema 校验' + (config.repair_with_llm ? ' · 失败自动返工' : '')
    case 'metrics': {
      // 钉住别的工作流里的口径卡：本地那几项不生效，写「0 个指标」会让人以为卡是空的
      const from = config.caliber_from
      if (from && typeof from === 'object') {
        return from.workflow_id && from.workflow_version && from.node_id
          ? `钉住上游口径卡 v${from.workflow_version} · 节点 ${from.node_id}${config.upgrade_policy ? ` · 升版按 ${config.upgrade_policy}` : ''}`
          : '钉住上游口径卡 · 还没选完'
      }
      return `${config.caliber || '口径'}@${config.caliber_version || 'v1'} · ${(config.metrics ?? []).length} 个指标`
    }
    case 'report': {
      const from = Array.isArray(config.metrics_from) ? config.metrics_from.length : 0
      return `${first(config.instructions) || '未填写作要求'} · ${from ? `${from} 张口径卡` : '上游全部口径卡'}`
    }
    case 'transform':
      return first(config.expression, config.template) || '未配置'
    case 'subgraph':
      return config.workflow_id ? '嵌套工作流' : '未选择工作流'
    default:
      return ''
  }
}

// -------------------------------------------------------------------------
// 从 store 里取的几样小东西。选择器都返回原始值或稳定引用：每条事件都会把
// 全部卡片的选择器跑一遍，返回新对象就等于整张画布跟着重渲染
// -------------------------------------------------------------------------

/** 这个节点的校验问题，压成一个字符串（数量 + 第一条），好让选择器返回稳定值 */
function issueKey(issues: ValidationIssue[], id: string): string {
  let errors = 0
  let warnings = 0
  let first = ''
  for (const i of issues) {
    if (i.node_id !== id) continue
    if (i.level === 'error') {
      if (!errors) first = i.message
      errors += 1
    } else {
      if (!errors && !warnings) first = i.message
      warnings += 1
    }
  }
  return errors || warnings ? `${errors}\u0000${warnings}\u0000${first}` : ''
}

/**
 * 出具判定挂在哪个成果节点上、什么时候落下的（毫秒，和航迹同一条时间轴，回放时
 * 游标没走到这一刻就不盖章）。按事件数组缓存：一张图里通常只有一两个成果节点在问
 */
interface IssuanceAt {
  id: string | null
  at: number | null
  /** 事件原样的 data：运行还没结束时 run.output 里还没有 _issuance，悬停说明从这里取 */
  data?: Record<string, any>
}
const issuanceCache = new WeakMap<RunEvent[], IssuanceAt>()
function issuanceOf(events: RunEvent[]): IssuanceAt {
  const hit = issuanceCache.get(events)
  if (hit) return hit
  let found: IssuanceAt = { id: null, at: null }
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const e = events[i]
    if (e.type === 'issuance') {
      found = { id: e.node_id ?? null, at: typeof e.ts === 'number' ? e.ts * 1000 : null, data: e.data ?? undefined }
      break
    }
  }
  issuanceCache.set(events, found)
  return found
}

/** 点印章够不够得着出具横幅：横幅或右栏（助手面板）看得见就行，右栏在对话层时会自己切过去 */
function issuanceReachable(): boolean {
  return ['[data-issuance-banner]', '[data-assistant-panel]'].some((sel) => {
    const el = document.querySelector<HTMLElement>(sel)
    return !!el && el.offsetWidth > 0 && getComputedStyle(el).visibility === 'visible'
  })
}

/**
 * 印章 → 右栏运行视图里的出具横幅。和「去审批」同一个约定：先广播，右栏在对话层时
 * 自己切过去；接手了就 preventDefault。没人接手时这里等几帧找横幅滚过去，还是找不到
 * 就说一声，不让印章像是点坏了
 */
function gotoIssuance(runId: string | undefined): void {
  const handled = !window.dispatchEvent(new CustomEvent('agentlab:goto-issuance', {
    detail: { runId }, cancelable: true,
  }))
  if (handled) return
  let tries = 0
  const find = () => {
    const banner = document.querySelector<HTMLElement>('[data-issuance-banner]')
    if (banner && banner.offsetWidth > 0 && getComputedStyle(banner).visibility === 'visible') {
      const reduced = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches
      banner.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'center' })
      return
    }
    if (tries++ < 12) requestAnimationFrame(find)
    else toast.info('右栏收起来了：展开助手栏，在运行视图的成果区看完整判定', { key: 'card:issuance' })
  }
  requestAnimationFrame(find)
}

/**
 * 回指上了哪些数字、各按哪个口径核的：「12.4% → 毛利率（月度口径 @ v2）」。
 * 口径带版本，同一个指标换过口径时一眼看得出这次按的是哪一版。悬停里只放前几条
 */
function matchedLines(matched: unknown): string {
  if (!Array.isArray(matched) || !matched.length) return ''
  const lines = matched.slice(0, 5).map((m: any) => {
    const token = String(m?.token ?? '').trim()
    // 出处不唯一的（metric 为 null、带候选）照实写，不能写成空箭头
    const source = matchedSource(m).text
    const caliber = typeof m?.caliber === 'string' && m.caliber.trim() ? `（${m.caliber.trim()}）` : ''
    return token ? `  ${token}${source ? ` → ${source}` : ''}${caliber}` : ''
  }).filter(Boolean)
  if (!lines.length) return ''
  const more = matched.length > lines.length ? `\n  …另有 ${matched.length - lines.length} 个` : ''
  return `回指明细：\n${lines.join('\n')}${more}`
}

/** stats 里的裁判句数（claims 为 judge 时才有）→「结论 4 句（支持 1 · 不支持 1 · 未裁判 2）」 */
function claimsLine(st: ReportStamp['stats']): string {
  const n = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : 0)
  const c = { supported: n(st.supported), partial: n(st.partial), unsupported: n(st.unsupported), unjudged: n(st.unjudged), uncited: 0 }
  return claimTally({ ...c, total: c.supported + c.partial + c.unsupported + c.unjudged })
}

/** 报告节点章的悬停说明：两个数怎么来的，缺口里各有什么 */
function reportStampTitle(r: ReportStamp): string {
  const st = r.stats
  const n = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : 0)
  const uncited = n(st.uncited_claims)
  return [
    `报告核对（这张图最近一次运行${r.runId ? ` ${shortId(r.runId)}` : ''}）：${reportStampText(r.cited, r.none)}`,
    typeof st.numbers === 'number' ? `数字 ${formatNumber(n(st.numbers_cited))}/${formatNumber(st.numbers)} 有出处` : '',
    n(st.entities) || n(st.quotes) ? `表名字段名 ${formatNumber(n(st.entities))} 处、引文 ${formatNumber(n(st.quotes))} 处有出处` : '',
    n(st.unresolved) ? `引用解析不了 ${formatNumber(n(st.unresolved))} 处` : '',
    n(st.unknown_entities) ? `可能是编造的名字 ${formatNumber(n(st.unknown_entities))} 个` : '',
    n(st.unverified_entities) ? `核对不了的名字 ${formatNumber(n(st.unverified_entities))} 个（只标注，不计入）` : '',
    uncited ? `没挂依据的结论句 ${formatNumber(uncited)} 句${r.claims === 'require_citation' || r.claims === 'judge'
      ? '（计入缺口）' : '（结论句策略不要求，不计入）'}` : '',
    // 结论句裁判（四期）：模型的判断，不计入章上的两个数
    r.claims === 'judge' && claimsLine(st) ? `${claimsLine(st)}（模型判断，非确定，不计入）` : '',
    '完整清单在记录页这次运行的「证据」页签',
  ].filter(Boolean).join('\n')
}

/**
 * 每个节点有几路上游（不算循环的回边）。排队中的节点据此说清在等什么：
 * 汇合点是「等其余上游」，单入口的只是轮到它之前那一拍。按拓扑缓存
 */
const fanInCache = new WeakMap<object, Record<string, number>>()
function fanInOf(nodes: FlowNode[], edges: Edge[]): Record<string, number> {
  const topo = topology({ nodes, edges })
  const hit = fanInCache.get(topo)
  if (hit) return hit
  const sources: Record<string, Set<string>> = {}
  for (const e of edges) {
    if (topo.back.has(edgeKey(e))) continue
    ;(sources[e.target] ??= new Set()).add(e.source)
  }
  const out = Object.fromEntries(Object.entries(sources).map(([id, set]) => [id, set.size]))
  fanInCache.set(topo, out)
  return out
}

/** 报错的第一句。后端的报错是「发生了什么 + 原因 + 怎么办」，槽里只放得下第一句 */
function firstSentence(text: string | undefined): string {
  const t = (text ?? '').trim()
  if (!t) return ''
  const line = t.split('\n')[0]
  const cut = line.search(/[。；;]/)
  return cut > 0 ? line.slice(0, cut) : line
}

/** 回放到某一刻时，最近一次跑完走的是哪个出口（那一刻还没跑完的不算） */
function takenAt(n: NodeTrace | undefined, at: number): string | undefined {
  let handle: string | undefined
  for (const s of n?.segments ?? []) {
    if (s.kind === 'run' && s.end != null && s.end <= at && s.handle) handle = s.handle
  }
  return handle
}

/**
 * 失败摘要。「模型没真调工具」「团队用完轮数」「修复想凑数」「提示词点名的工具没绑」
 * 这几类是图本身有缺口，后端的原话很长、槽里只截得下半句：换成 lib/explain 的短标题，
 * 原因和下一步放悬停。别的失败照原话的第一句——它们本来就是写给人看的，
 * 再归一次类会把「工艺员 30 秒没有回应」这种具体的话抹成「等待超时」
 */
function failureOf(error: string): { note: string; title: string } {
  if (!error) return { note: '没有给出原因', title: '' }
  const ex = explainRunError(error)
  if (ex.continuable || ex.fix !== 'canvas') return { note: firstSentence(error), title: error }
  return {
    note: ex.title,
    title: [ex.title, ex.reason, ex.action ? `怎么办：${ex.action}` : ''].filter(Boolean).join('\n'),
  }
}

/** 协作团队是不是因为轮数用完而失败的（后端的报错首句「协作团队用完 N 轮仍未完成」） */
const EXHAUSTED_ERROR = /用完\s*\d+\s*轮(?:仍|还)?未完成/

/**
 * 「协作团队用完 N 轮仍未完成：<理由>。一次都没被派到的成员：A、B。…」里的轮数、理由
 * 和没派到的人。判失败的报错和降档的那条日志是同一个开头；产出里的 never_dispatched
 * 进事件时被缩成了「[1 项]」，成员名只能从这句话里认。只认这一次执行的那句话——
 * 右栏泳道那份结局是整次运行攒下来的，接着跑成功之后还留着上一次的
 */
function exhaustedText(text: string): { rounds?: number; reason?: string; never: string[] } | undefined {
  const m = text.match(/用完\s*(\d+)\s*轮(?:仍|还)?未完成[：:]\s*([\s\S]*?)(?:。一次都没被派到的成员|。先看成员|。按降档交付|$)/)
  if (!m) return undefined
  const never = text.match(/一次都没被派到的成员[：:]\s*([^。]+)/)?.[1]
    .split('、').map((s) => s.trim()).filter(Boolean) ?? []
  return { rounds: Number(m[1]) || undefined, reason: m[2].trim() || undefined, never }
}

/** 完成之后的产出量：模型出了多少 token、查到几行、取回几条 */
function outputOf(u: NodeUsage, preview: any): string {
  if (u.tokensOut > 0) return formatTokens(u.tokensOut, { compact: true })
  if (Array.isArray(preview)) return `${formatNumber(preview.length)} 条`
  if (preview && typeof preview === 'object') {
    if (typeof preview.row_count === 'number') return `${formatNumber(preview.row_count)} 行`
    for (const v of Object.values(preview)) {
      if (Array.isArray(v)) return `${formatNumber(v.length)} 条`
    }
  }
  return ''
}

// -------------------------------------------------------------------------
// 遥测槽：固定 22px，一行说完状态、计时和产出
// -------------------------------------------------------------------------

interface Reading {
  /** 主读数：计时、耗时、进度。远景档精简卡上只剩它 */
  main: ReactNode
  /** 主读数之后的补充：理由、错误摘要、产出量 */
  note?: ReactNode
  noteTitle?: string
  /** 跑完了但有要留意的事（降档交付、收尾时还想调工具）：note 用琥珀色 */
  noteWarn?: boolean
  /** 靠右的一组小读数：token、工具 */
  side?: ReactNode
}

interface ReadingInput {
  state: NodeState
  view: NodeView
  type: NodeType
  config: Record<string, any>
  skewMs: number
  timed: boolean
  /** 上游有几路：排队时说清在等什么 */
  fanIn: number
  facts?: NodeFacts
  /** 卡片在说的那一次执行的起点（execStart）：事件里捞的事只算这之后的 */
  since: number
  /** 这一次执行的报错 */
  error: string
  ending?: TeamEnding
}

/**
 * 循环节点的分段进度：foreach 按总项数分段，while 按上限分段。
 * phase：live 在跑（当前那格亮运行色）、done 走了 done 出口、stopped 半路停下
 * （失败、取消、挂起：当前那格不亮，停在哪一格一眼看得出）
 */
function LoopProgress({ n, iteration = 0, config, phase }: {
  n: NodeTrace | undefined
  /** 当前第几轮：实时取航迹，回放取那一刻的投影 */
  iteration?: number
  config: Record<string, any>
  phase: 'live' | 'done' | 'stopped'
}) {
  const done = phase === 'done'
  const cap = Number(config.max_iterations) || 0
  const total = n?.iterTotal != null ? (cap ? Math.min(n.iterTotal, cap) : n.iterTotal) : undefined
  const finished = done ? iteration : Math.max(0, iteration - 1)
  const slots = total ?? cap
  // 分段超过 12 格就看不清了，改成一条连续的进度
  const segs = slots > 0 && slots <= 12 ? Array.from({ length: slots }, (_, i) => i) : null
  const label = config.mode === 'while'
    ? done ? `共 ${iteration} 轮` : iteration ? `第 ${iteration} 轮${cap ? ` · 上限 ${cap}` : ''}` : NONE
    : total != null
      ? done ? `共 ${total} 项` : `${Math.min(iteration, total)}/${total}`
      : iteration ? `第 ${iteration} 项` : NONE
  return (
    <>
      {segs ? (
        <span className="nc-seg" aria-hidden>
          {segs.map((i) => (
            <i key={i} className={clsx(i < finished && 'is-done', phase === 'live' && i === finished && iteration > 0 && 'is-cur')} />
          ))}
        </span>
      ) : slots > 0 ? (
        <span className="nc-seg nc-seg-bar" aria-hidden>
          <i style={{ width: `${Math.min(100, (finished / slots) * 100)}%` }} />
        </span>
      ) : null}
      <span className="tnum">{label}</span>
    </>
  )
}

/**
 * 在跑的那次工具调用。后端声明了时限（tool.start.timeout_s）的，超出之后直说
 * 「已超出 30s 上限」，不再写一个看不出等了多久的「⋯」。没超出时照常安静
 */
function ToolTag({ call, at, skewMs }: { call: ToolSpan; at: number | null; skewMs: number }) {
  // 老事件没有时间戳时起点是 0，算不出等了多久，就不判超没超
  if (!call.limitS || !call.start) return <ToolName tool={call.tool} />
  if (at != null) return <ToolTagText call={call} elapsed={at - call.start} />
  return <LiveToolTag call={call} skewMs={skewMs} />
}

function LiveToolTag({ call, skewMs }: { call: ToolSpan; skewMs: number }) {
  const now = useRunClock(true)
  return <ToolTagText call={call} elapsed={now - skewMs - call.start} />
}

/**
 * 超出之后槽里只放得下一件事：说超时，不再写工具名（扳手已经说了是工具，名字在悬停里）。
 * db_query__shop 这种名字加上「已超出 30s 上限」，比卡片还宽
 */
function ToolTagText({ call, elapsed }: { call: ToolSpan; elapsed: number }) {
  if (elapsed <= call.limitS! * 1000) return <ToolName tool={call.tool} />
  return <span className="nc-over">已超出 {call.limitS}s 上限</span>
}

/** 在跑的工具名：名字长时截掉，省略号前后都留着「⋯」这个在跑的记号 */
const ToolName = ({ tool }: { tool: string }) => <><span className="nc-tool-name">{tool}</span> ⋯</>

/** 工具明细（放悬停）：谁调的（成员首字）、调了什么、成没成、有没有超时 */
function toolLines(calls: { tool: string; agent?: string; ok?: boolean; timedOut?: boolean; limitS?: number }[]): string {
  return calls.slice(-8).map((c) => `${c.agent ? `[${c.agent.slice(0, 1)}] ` : ''}${c.tool} ${
    c.timedOut ? `⏱ 超时${c.limitS ? `（上限 ${c.limitS}s）` : ''}`
      : c.ok === false ? '✗ 失败' : c.ok ? '✓' : `⋯ 进行中${c.limitS ? `（上限 ${c.limitS}s）` : ''}`}`).join('\n')
}

/**
 * 跑完之后那半句：平常是产出量；有要留意的事时换成那件事——降档交付、
 * 收尾时还想调工具、纠正过工具调用、校验靠修复才过。正常态安静，只在这些时候上琥珀色。
 * 只看这一次执行（since 之后）：上一轮循环、接着跑之前的事不挂到这一次的结果上
 */
function doneNote(type: NodeType, u: NodeUsage, preview: any, facts: NodeFacts | undefined,
                  ending: TeamEnding | undefined, at: number | null, since: number, maxRounds: number,
): Pick<Reading, 'note' | 'noteTitle' | 'noteWarn'> {
  if (ending?.exhausted === 'degrade') {
    const rounds = ending.rounds ?? (maxRounds || '全部')
    return {
      note: `用完 ${rounds} 轮未完成 · 降档交付`,
      noteTitle: [
        `协作团队用完 ${rounds} 轮，调度者始终没有判定完成`,
        ending.reason ? `理由：${ending.reason}` : '',
        '按降档交付：成果是成员最后的原话，不是调度者认可的结论。下游的复核、出具会跟着降档',
      ].filter(Boolean).join('\n'),
      noteWarn: true,
    }
  }
  const marks = within(facts?.markups, at, since)
  const settle = marks.filter((m) => m.settle).pop()
  if (settle) {
    return {
      note: '收尾时仍想调用工具',
      noteTitle: `${settle.message}\n步数用完之后模型还在要工具：交出来的是它前面写的内容，可能不完整`,
      noteWarn: true,
    }
  }
  // 写成文字、被提醒之后重答了：成果是重答的那一次，不是不完整。复核仍按降档算，所以还是琥珀。
  // 协作成员提醒之后还这样的，这一步记失败（矩阵里那一格是红的），不能说成重答了
  const fails = type === 'supervisor' ? within(facts?.memberErrors, at, since) : []
  const nudged = marks.filter((m) => !m.settle && !fails.some((f) => f.at >= m.at && m.message.startsWith(f.agent)))
  if (nudged.length) {
    return {
      note: nudged.length > 1 ? `纠正过 ${nudged.length} 次工具调用` : '纠正过一次工具调用',
      noteTitle: [
        ...nudged.slice(-3).map((m) => m.message),
        `${type === 'supervisor' ? '成员' : '模型'}把工具调用写成了文字，提醒之后重答了，成果是重答的那一次；复核按降档算`,
      ].join('\n'),
      noteWarn: true,
    }
  }
  const repairs = type === 'validate' ? repairsOf(facts, at, since) : 0
  if (repairs) {
    // 有一次修复想拿原文没有的值凑数、被作废了：最后虽然过了，复核照样按降档算
    const invented = lastIn(facts?.inventions, at, since)
    return {
      note: `修复 ${repairs} 次后通过`,
      noteTitle: invented
        ? `第一次没过校验。其中一次修复出现了原文没有的值，已作废：${invented.message}\n最后通过的那次只调整了格式，复核按降档算`
        : '第一次没过校验，模型只调整了格式，没有补数据',
      noteWarn: !!invented,
    }
  }
  return { note: outputOf(u, preview) }
}

function readingOf({ state, view, type, config, skewMs, timed, fanIn, facts, since, error, ending }: ReadingInput): Reading {
  const n = view.trace
  const runtime = view.runtime
  const at = view.at
  const u = view.usage
  // 回放时计时是定值：游标减去那一段的起点。实时的交给 LiveClock 自己走
  const clock = (from: number | undefined, coarse = false): ReactNode => {
    if (at != null) return stillClock(from != null ? at - from : undefined)
    return timed && from != null ? <LiveClock from={from} skewMs={skewMs} coarse={coarse} /> : NONE
  }
  const loop = (phase: 'live' | 'done' | 'stopped') => (
    <LoopProgress n={n} iteration={view.iteration} config={config} phase={phase} />
  )
  const tokens = u.tokensIn + u.tokensOut
  // 工具调用：事件里捞出来的那份带起止和时限，回放能按游标截；没有时退回 runtime。
  // 只数这一次执行的：上一轮失败的调用不能让这一轮的扳手变红
  const calls = facts
    ? facts.calls.filter((c) => c.start >= since && (at == null || c.start <= at))
      .map((c) => (at != null && c.end != null && c.end > at ? { ...c, ok: undefined, timedOut: false } : c))
    : runtime?.toolCalls ?? []
  const failedTools = calls.filter((c) => c.ok === false).length
  const open = u.toolsRunning ? openCall(facts, at, since) : undefined
  const runningTool = u.toolsRunning
    ? open?.tool ?? [...(runtime?.toolCalls ?? [])].reverse().find((c) => c.ok === undefined)?.tool
    : undefined
  const toolTitle = toolLines(calls)
  const side = (
    <>
      {tokens > 0 && (
        <span className="tnum" title={`输入 ${formatNumber(u.tokensIn)} · 输出 ${formatNumber(u.tokensOut)} tokens`}>
          {formatTokens(tokens, { compact: true })}
        </span>
      )}
      {!!u.tools && (
        <span
          className={clsx('nc-tools tnum', runningTool && 'is-live', failedTools && 'is-bad')}
          title={toolTitle}
        >
          <Wrench size={9} aria-hidden />
          {open ? <ToolTag call={open} at={at} skewMs={skewMs} /> : runningTool ? <ToolName tool={runningTool} /> : u.tools}
        </span>
      )}
    </>
  )

  // 预览（查到几行、取回几条）只有最后一次执行的：回放到更早的一次执行时不拿它冒充那一刻。
  // token、工具次数不受这个限制，投影给的就是那一刻的读数
  const lastExec = at == null || view.count === (n?.count ?? 0)
  const preview = lastExec ? runtime?.preview : undefined

  switch (state) {
    case 'running':
      if (type === 'loop') {
        return { main: loop('live'), side: clock(loopSince(n, at), true) }
      }
      return { main: clock(openSince(n, 'run', at)), side }
    case 'waiting':
      return { main: <>已等 {clock(openSince(n, 'wait', at), true)}</> }
    case 'done':
      if (type === 'loop') {
        return { main: loop('done'), side: <span className="tnum">{formatDuration(timed ? loopSpan(n, at) ?? n?.lastDurationMs : n?.lastDurationMs)}</span> }
      }
      return {
        main: formatDuration(at != null ? view.replayElapsedMs : n?.lastDurationMs ?? runtime?.durationMs),
        ...doneNote(type, u, preview, facts, ending, at, since, Number(config.max_rounds) || 0),
        // 跑完了只留工具次数：token 已经在产出量里说过（输出多少），再挂一个总数是两套数
        side: u.tools ? (
          <span className={clsx('nc-tools tnum', failedTools && 'is-bad')} title={toolTitle}>
            <Wrench size={9} aria-hidden />{u.tools}
          </span>
        ) : undefined,
      }
    case 'failed': {
      const why = failureOf(error)
      if (type === 'loop') {
        return { main: loop('stopped'), note: error ? why.note : '', noteTitle: why.title }
      }
      return {
        main: formatDuration(n?.lastDurationMs ?? runtime?.durationMs),
        note: why.note,
        noteTitle: why.title,
      }
    }
    case 'queued':
      return { main: '', note: fanIn > 1 ? '等其余上游汇合' : '上游已交付，等待开始' }
    case 'skipped':
      return { main: '', note: n?.skippedReason || '跳过条件成立', noteTitle: n?.skippedReason }
    case 'blocked':
      return { main: '', note: '上游失败，走不到这里' }
    case 'unreached':
      return { main: '', note: '本次未执行' }
    case 'cancelled':
      // 循环容器停下时要说的是做到第几项，不是这一轮跑了多久
      if (type === 'loop') {
        return { main: loop('stopped'), side: <span className="tnum">{formatDuration(timed ? loopSpan(n, at) : undefined)}</span> }
      }
      return {
        main: n?.startedAt != null && n.endedAt != null && timed ? `停在 ${formatLapse(n.endedAt - n.startedAt)}` : '',
        note: n?.count ? '' : '没有开始',
      }
    case 'suspended':
      if (type === 'loop') return { main: loop('stopped'), note: '可接着跑' }
      return { main: '', note: '服务重启打断，可接着跑' }
    default:
      return { main: '' }
  }
}

// -------------------------------------------------------------------------
// 等待节点上的「去审批」浮层：复用右栏同一张审批卡，口径只有一份
// -------------------------------------------------------------------------

/** 浮层里能拿到键盘焦点的元素，按文档顺序 */
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'

function ApprovalPopover({ id, nodeId, runId, anchor, returnTo, onClose }: {
  id: string
  nodeId: string
  runId: string
  anchor: HTMLElement
  /** 收起后焦点回到哪（「去审批」按钮） */
  returnTo: RefObject<HTMLButtonElement | null>
  onClose: () => void
}) {
  const listed = useCatalog((s) => s.approvals.find((a) =>
    a.run_id === runId && a.node_id === nodeId && a.status === 'pending'))
  const [fetched, setFetched] = useState<Approval | null | undefined>(undefined)
  const [pos, setPos] = useState<{ left: number; top: number; above: boolean } | null>(null)
  const box = useRef<HTMLDivElement>(null)

  // 全局列表 4 秒轮询一次，刚停下的那一刻它可能还没有这一条：按运行问一次
  useEffect(() => {
    if (listed) return
    let alive = true
    api.approvals.list({ run_id: runId, status: 'pending' })
      .then((list) => { if (alive) setFetched(list.find((a) => a.node_id === nodeId) ?? null) })
      .catch(() => { if (alive) setFetched(null) })
    return () => { alive = false }
  }, [listed, runId, nodeId])

  useLayoutEffect(() => {
    const r = anchor.getBoundingClientRect()
    const width = 340
    const left = Math.max(8, Math.min(r.left, window.innerWidth - width - 8))
    const below = window.innerHeight - r.bottom
    setPos(below > 320 || below > r.top
      ? { left, top: r.bottom + 8, above: false }
      : { left, top: r.top - 8, above: true })
  }, [anchor])

  // 点外面、按 Esc、滚轮缩放画布都收起：它钉在打开时的屏幕位置上，画布一动就对不上了。
  // 这里的 Esc 只管焦点还在「去审批」按钮上的时候；焦点进了浮层，按键在浮层根上处理
  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      if (box.current?.contains(e.target as Node) || anchor.contains(e.target as Node)) return
      onClose()
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && !isComposing(e)) onClose() }
    const onWheel = (e: WheelEvent) => { if (!box.current?.contains(e.target as Node)) onClose() }
    document.addEventListener('pointerdown', onDown, true)
    document.addEventListener('keydown', onKey)
    window.addEventListener('wheel', onWheel, { passive: true })
    return () => {
      document.removeEventListener('pointerdown', onDown, true)
      document.removeEventListener('keydown', onKey)
      window.removeEventListener('wheel', onWheel)
    }
  }, [anchor, onClose])

  const approval = listed ?? fetched

  // 打开就把焦点送进去：浮层挂在 body 末尾，不送进去的话键盘用户从「去审批」
  // 按 Tab 会跳到下一个节点，永远够不着「通过 / 驳回」。审批卡还在取的时候先
  // 落在浮层本身，取到了再往里送；送进去之后全局列表每轮询一次都换一个对象，
  // 那时不再抢焦点
  const placed = useRef(false)
  useEffect(() => {
    const el = box.current
    if (!pos || !el) return
    if (placed.current && document.activeElement !== el) return
    const first = el.querySelector<HTMLElement>(FOCUSABLE)
    ;(first ?? el).focus({ preventScroll: true })
    placed.current = !!first
  }, [pos, approval])

  // 收起时焦点还在浮层里（或者已经无处可去）就还给「去审批」；批完了按钮也没了，
  // 就还给节点本身。焦点已经去了别处（点了右栏的输入框）的不去抢。卸载时 ref
  // 已经摘掉了，焦点在不在里面靠 focus / blur 记着
  const holding = useRef(false)
  useEffect(() => () => {
    const active = document.activeElement
    if (!holding.current && active && active !== document.body) return
    if (returnTo.current?.isConnected) returnTo.current.focus({ preventScroll: true })
    else anchor.closest<HTMLElement>('.react-flow__node')?.focus({ preventScroll: true })
  }, [anchor, returnTo])

  if (!pos) return null
  // 挂到 body 上：画布是缩放过的，挂在节点里的话 0.4 倍时整张审批卡小得没法点。
  // React 的合成事件仍会顺着组件树冒到 React Flow 的节点上（点一下就选中节点、
  // 打开检查器），所以在浮层根上把它们截住
  const stop = (e: { stopPropagation: () => void }) => e.stopPropagation()
  // 键盘同理要截住（不然 Enter 会冒到节点上去选中它）；截住之前先把 Esc 和
  // Tab 处理掉：Esc 收起，Tab 在浮层里转圈，不从末尾掉出页面
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    e.stopPropagation()
    if (isComposing(e)) return
    if (e.key === 'Escape') {
      e.preventDefault()
      onClose()
      return
    }
    if (e.key !== 'Tab' || !box.current) return
    const items = [...box.current.querySelectorAll<HTMLElement>(FOCUSABLE)]
    if (!items.length) return
    const first = items[0]
    const last = items[items.length - 1]
    const active = document.activeElement
    if (e.shiftKey && (active === first || active === box.current)) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && active === last) {
      e.preventDefault()
      first.focus()
    }
  }
  return createPortal(
    <div
      ref={box}
      id={id}
      className="nc-pop fade-up nodrag nopan nowheel"
      style={{ left: pos.left, top: pos.top, transform: pos.above ? 'translateY(-100%)' : undefined }}
      role="dialog"
      aria-label="审批卡"
      tabIndex={-1}
      onClick={stop}
      onPointerDown={stop}
      onMouseDown={stop}
      onDoubleClick={stop}
      onKeyDown={onKeyDown}
      onFocus={(e) => { holding.current = true; e.stopPropagation() }}
      onBlur={(e) => {
        if (!box.current?.contains(e.relatedTarget as Node | null)) holding.current = false
        e.stopPropagation()
      }}
    >
      {approval ? (
        <ApprovalCard approval={approval} showWorkflow={false} />
      ) : (
        <div className="p-3 text-xs text-dim">
          {approval === null ? '没找到这一步的待审批，可能已经处理过了' : '正在取审批卡…'}
        </div>
      )}
    </div>,
    document.body,
  )
}

// -------------------------------------------------------------------------

function NodeCardImpl({ id, data, selected, isConnectable }: NodeProps<FlowNode>) {
  const def = NODE_DEFS[data.nodeType]
  const type = data.nodeType
  const view = useNodeView(id)
  const facts = useNodeFacts(id)
  const { state, runtime, trace: nt } = view

  const runActive = useStudio((s) => s.runPhase !== 'idle')
  const runId = useStudio((s) => s.run?.id ?? null)
  const skewMs = useStudio((s) => s.trace.skewMs ?? 0)
  const timed = useStudio((s) => s.trace.timed)
  const lastReplay = useStudio((s) => s.trace.lastReplay)
  const runClass = useStudio((s) => s.trace.runClass ?? s.run?.run_class)
  const isSelected = useStudio((s) => s.selectedId === id)
  const hasSelection = useStudio((s) => s.selectedId != null)
  const hovered = useStudio((s) => s.hoveredNodeId === id)
  const dimmed = useStudio((s) => s.hoveredNodeId != null && s.hoveredNodeId !== id)
  const focusSeq = useStudio((s) => (s.focusRequest?.id === id ? s.focusRequest.seq : 0))
  const copilotNew = useStudio((s) => s.copilotNew.includes(id))
  const issues = useStudio((s) => issueKey(s.issues, id))
  const snapSig = useStudio((s) => s.runSnapshot?.configs[id])
  const issuance = useStudio((s) =>
    (type === 'output' && issuanceOf(s.events).id === id ? s.trace.issuance : undefined))
  const issuedAt = useStudio((s) => (type === 'output' ? issuanceOf(s.events).at : null))
  const issuanceDetail = useStudio((s) =>
    (type === 'output' ? (s.run?.output as any)?._issuance as Record<string, any> | undefined
      ?? (issuanceOf(s.events).id === id ? issuanceOf(s.events).data : undefined) : undefined))
  // 报告节点：这张图最近一次运行的核对统计，画成卡上的章（没有就不画）
  const reportStamp = useReportStamp(id, type === 'report')
  // 只有排队中的卡片要知道自己有几路上游，别的卡片不必为改图重跑这一趟
  const fanIn = useStudio((s) => (state === 'queued' ? fanInOf(s.nodes, s.edges)[id] ?? 0 : 0))

  const Icon = def?.icon ?? Sparkles
  const handles = sourceHandles(type, data.config)
  const summary = summarize(type, data.config)
  const meta = statusMeta(state)
  // 循环容器在两轮之间也是 running，但在跑的是循环体：不转圈、不挂光弧。
  // 回放时取游标那一刻的（航迹上的 looping 是最后的值）
  const executing = state === 'running' && !view.looping
  const card = useRef<HTMLDivElement>(null)
  const approveBtn = useRef<HTMLButtonElement>(null)
  const popId = useId()
  const [approving, setApproving] = useState(false)
  const closeApproval = useCallback(() => setApproving(false), [])
  // 印章能不能点：右栏的出具横幅在不在页面上。悬停时再看——横幅跟着右栏的
  // 视图切换挂上摘下，渲染时看一眼作不了准
  const [stampLink, setStampLink] = useState(false)

  // 结果是不是拿改动前的配置跑出来的：发起运行时记了一份签名，和现在的比
  const stale = useMemo(
    () => snapSig != null && snapSig !== configSig(data.config),
    [snapSig, data.config],
  )

  // 进入某个状态的那一下（抖一下、扫一遍、落章）只在实时事件里播。回放、打开
  // 一条历史运行时几十个节点同时落到终态，要是每张都扫一遍就成了烟花
  const prev = useRef<NodeState>(state)
  const entered = useRef<NodeState | null>(null)
  if (prev.current !== state) {
    entered.current = !view.replay && !lastReplay ? state : null
    prev.current = state
  }
  const liveEntry = entered.current === state

  // 印章同理：跟着实时的出具判定落下来才有落章的动作，否则直接在那儿
  const stampLive = useRef<boolean | null>(null)
  if (issuance?.tier && stampLive.current == null) stampLive.current = !view.replay && !lastReplay
  if (!issuance?.tier) stampLive.current = null

  // 离开等待（批了、驳了、运行被取消）就把浮层收掉
  useEffect(() => {
    if (state !== 'waiting' || view.replay) setApproving(false)
  }, [state, view.replay])

  // 协作矩阵从编辑态起就在（那时就是花名册），跑起来只换内容，卡片不长个子
  const agents = data.config.agents as { name?: string; description?: string }[] | undefined
  const roster = useMemo(
    () => (agents ?? []).filter((a) => a.name).map((a) => ({ name: a.name!, description: a.description })),
    [agents],
  )
  const isTeam = type === 'supervisor' && roster.length > 0

  // 流式正文只在真的在跑时替换摘要位。取消了、跑完了就换回摘要——
  // 正文在右栏和检查器里，卡片上留着 80px 的尾巴只会压住下面的邻居
  const streamText = executing && !view.replay ? (runtime?.tokens || runtime?.thinking || '') : ''
  const thinkingOnly = !!streamText && !runtime?.tokens

  // 这张卡在说哪一次执行：循环的下一轮、失败后接着跑都是新的一次，事件里捞的事只算
  // 这一次的。不截的话，调大轮数接着跑成功了，表头还挂着上一次的「判定未完成」
  const since = execStart(nt, view.at, facts)
  const lastExec = view.at == null || view.count === (nt?.count ?? 0)
  // 报错：航迹、runtime 上的是最后一次执行的；回放到更早的一次失败时取那一次的原话
  const failure = view.at != null ? lastIn(facts?.failures, view.at, since) : undefined
  const error = failure?.message || (lastExec ? nt?.error || runtime?.error || '' : '')
  const preview = lastExec ? runtime?.preview : undefined

  // 协作团队怎么收场：收尾判定、用完轮数之后是失败还是降档。卡片和矩阵说同一件事
  const verdictMark = type === 'supervisor' ? lastIn(facts?.closings, view.at, since) : undefined
  // 判定开始了、结论还没到（回放时按游标：那一刻结论还没出来也算在判）
  const verdictOpen = !!verdictMark && (verdictMark.end == null || (view.at != null && verdictMark.end > view.at))
  const exhaustedLog = type === 'supervisor' ? lastIn(facts?.exhausts, view.at, since) : undefined
  const failedOut = type === 'supervisor' && state === 'failed' && EXHAUSTED_ERROR.test(error)
  const degraded = type === 'supervisor' && state === 'done' && (preview?.exhausted === true || !!exhaustedLog)
  const told = failedOut ? exhaustedText(error) : degraded && exhaustedLog ? exhaustedText(exhaustedLog.message) : undefined
  const exhaustedReason = typeof preview?.exhausted_reason === 'string' ? preview.exhausted_reason : told?.reason ?? ''
  // 产出进事件时数组会被缩成「[1 项]」，是数组才用；否则用报错、日志原话里认出来的
  const neverSig = failedOut || degraded
    ? (Array.isArray(preview?.never_dispatched) ? preview.never_dispatched.map(String) : told?.never ?? []).join('\u0000')
    : ''
  const exhaustedRounds = told?.rounds
  const ending = useMemo<TeamEnding | undefined>(() => {
    if (!verdictMark && !failedOut && !degraded) return undefined
    const verdict = verdictMark && {
      at: verdictMark.at,
      done: !verdictOpen && verdictMark.done,
      reason: verdictOpen ? '' : verdictMark.reason,
      open: verdictOpen,
    }
    return {
      verdict,
      exhausted: failedOut ? 'fail' : degraded ? 'degrade' : undefined,
      reason: verdict?.reason || exhaustedReason,
      never: neverSig ? neverSig.split('\u0000') : undefined,
      rounds: exhaustedRounds,
    }
  }, [verdictMark, verdictOpen, failedOut, degraded, exhaustedReason, neverSig, exhaustedRounds])
  // 成员失败的原因也只要这一次执行的
  const memberErrors = useMemo(() => {
    const out: Record<string, string> = {}
    for (const f of within(facts?.memberErrors, view.at, since)) out[f.agent] = f.error
    return out
  }, [facts?.memberErrors, view.at, since])

  const reading = readingOf({ state, view, type, config: data.config, skewMs, timed, fanIn, facts, since, error, ending })

  // 编辑态（没有运行）槽里放校验问题，有运行时放遥测
  const [errCount, warnCount, firstIssue] = issues ? issues.split('\u0000') : ['0', '0', '']
  const hasErrors = Number(errCount) > 0
  const hasWarnings = Number(warnCount) > 0
  const editing = !runActive && state === 'idle'

  // 头部小徽标：执行了几次、重试了几次、配置改过了
  const count = type === 'loop' ? 0 : view.count
  // 回放时只数游标之前的重试（node_retry 日志在航迹里是一段 retry）
  const retries = view.at != null
    ? (nt?.segments ?? []).filter((x) => x.kind === 'retry' && x.start <= view.at!).length
    : Math.max(0, (nt?.attempt ?? 1) - 1)
  const taken = view.at != null ? takenAt(nt, view.at) : nt?.takenHandle ?? runtime?.takenHandle
  // 模型名放悬停：槽里放不下 claude-sonnet-4-5 这么长的名字，也不是一眼要看的东西
  const teleTitle = !editing && nt ? [
    nt.model ? `模型 ${nt.model}` : '',
    view.usage.tokensIn + view.usage.tokensOut > 0
      ? `输入 ${formatNumber(view.usage.tokensIn)} · 输出 ${formatNumber(view.usage.tokensOut)} tokens` : '',
    retries > 0 ? `失败后重试了 ${retries} 次` : '',
  ].filter(Boolean).join('\n') || undefined : undefined

  const pin = state === 'failed' || state === 'waiting'
  const tier = view.at != null && issuedAt != null && view.at < issuedAt ? undefined : issuance?.tier
  const tierClass = tier === 'formal' ? 'is-formal' : tier === 'degraded' ? 'is-degraded' : tier === 'withheld' ? 'is-withheld' : ''
  const detail = issuanceDetail?.tier ? issuanceDetail : undefined
  const gaps: string[] = Array.isArray(detail?.gaps) ? detail.gaps.map(String) : []
  const stampTitle = tier ? [
    `出具判定：${issuanceLabel(tier)}`,
    detail ? `回指 ${detail.matched_numbers ?? 0} 个数字 / 核对 ${detail.metrics_checked ?? 0} 个指标` : '',
    // 降档常常不是数字对不上，而是校验根本没跑全（叙述模板渲染为空、指标集为空）：
    // 不写出来，悬停在一个「降档出具」上看不到任何理由
    gaps.length ? `校验没跑全：${gaps.join('；')}` : '',
    (detail?.missing_required ?? issuance?.missingRequired ?? []).length
      ? `缺必需指标：${(detail?.missing_required ?? issuance?.missingRequired).join('、')}` : '',
    (detail?.unmatched_numbers ?? issuance?.unmatched ?? []).length
      ? `无法回指的数字：${(detail?.unmatched_numbers ?? issuance?.unmatched).map((u: any) => u?.token ?? u).join('、')}`
      : issuance?.unmatchedCount ? `无法回指的数字 ${issuance.unmatchedCount} 个` : '',
    (detail?.missing_expected ?? []).length ? `缺数据声明：${detail!.missing_expected.join('、')} 本期缺失` : '',
    matchedLines(detail?.matched),
    runClass === 'exploratory' ? '探索运行的结论不进正式归档' : '',
    '完整判定在右栏运行视图的成果区',
  ].filter(Boolean).join('\n') : ''

  const rank = typeof (data as any).rank === 'number' ? (data as any).rank : undefined
  // 画布算出来的：走廊挪不出地方时，出口标签最多多宽（见 routing 的 Route.exit）
  const exitRoom = (data as { exitRoom?: Record<string, number> }).exitRoom
  const style: CSSProperties & Record<string, any> = { width: NODE_WIDTH }
  if (rank != null) style['--rank'] = rank

  return (
    <div
      ref={card}
      className={clsx(
        `nc nt-${type} node-${state}`,
        liveEntry && 'nc-enter',
        isSelected && 'nc-selected',
        selected && !isSelected && !hasSelection && 'nc-selected',
        hovered && 'nc-hover',
        dimmed && 'nc-dim',
        copilotNew && 'node-copilot-new',
        // 出口一多（92px 高的卡上 5 个起，间距不到 16px），画在线上方的标签会压住
        // 上一条出口的线：改成标签骑在自己那条线上，谁是谁的不会看错
        handles.length >= DENSE_EXITS && 'nc-exits-dense',
      )}
      data-state={state}
      data-type={type}
      style={style}
    >
      {/* 左侧状态槽：线型 / 纹理是颜色之外的第二条通道（色弱、灰度投影下也分得开） */}
      <div className="nc-slot" aria-hidden />

      {/* 光效层。独立一层，见 index.css 里 .node-fx 的说明 */}
      <div className="node-fx" aria-hidden>
        {executing && (
          <>
            <div className="fx-halo" />
            <div className="fx-clip"><div className="fx-scan" /></div>
          </>
        )}
        <div className="fx-clip">
          <div className="fx-sweep" />
          {liveEntry && state === 'done' && <div className="fx-done" />}
        </div>
        <div className="fx-moment" />
        {focusSeq > 0 && <div key={focusSeq} className="fx-focus" />}
      </div>

      {def?.hasTarget && (
        <Handle type="target" position={Position.Left} isConnectable={isConnectable} style={{ left: -5 }} />
      )}

      <div className="nc-content">
        {/* 标题栏：类型色只在图标块和极淡的底色里，边框、光弧一律是状态色 */}
        <div className="nc-head">
          <div className="nc-icon"><Icon size={12} /></div>
          <div className="nc-title">{data.label || def?.label}</div>
          {stale && (
            <span className="nc-chip" title="画布上的配置在这次运行之后改过，这张卡上的结果来自改动前的配置">旧配置</span>
          )}
          {count > 1 && (
            <span className="nc-chip tnum" title={`这次运行里执行了 ${count} 次`}>×{count}</span>
          )}
          {retries > 0 && (
            <span className="nc-chip nc-chip-warn tnum" title={`失败后重试了 ${retries} 次`}>↻{retries}</span>
          )}
          {ending?.exhausted === 'degrade' && (
            <span className="nc-chip nc-chip-warn" title="轮数用完仍未完成，按降档交付">降档</span>
          )}
          {(runActive || state !== 'idle') && (
            <StatusBadge status={state} size={14} animate={executing} className="nc-badge" />
          )}
        </div>

        {isTeam ? (
          <TeamMatrix
            roster={roster}
            team={runtime?.team}
            trace={nt}
            live={state === 'running'}
            replay={view.replay}
            at={view.at}
            maxRounds={Number(data.config.max_rounds) || 0}
            maxParallel={Number(data.config.max_parallel) || 0}
            skewMs={skewMs}
            ending={ending}
            memberErrors={memberErrors}
          />
        ) : (
          <div className={clsx('nc-body', streamText && 'is-stream')}>
            {streamText ? (
              <div className="nc-stream mono">
                {thinkingOnly && <span className="nc-stream-tag">思考中 · </span>}
                {streamText.slice(-160)}
              </div>
            ) : (
              <div className="nc-summary">{summary}</div>
            )}
          </div>
        )}

        {/* 遥测槽：固定高度。状态文字是第四条通道（剪影、线型、颜色之外） */}
        <div className="nc-tele" title={teleTitle}>
          {editing ? (
            hasErrors || hasWarnings ? (
              <span className={clsx('nc-tele-issue', hasErrors ? 'is-error' : 'is-warning')} title={firstIssue}>
                {firstIssue}
              </span>
            ) : (
              <span className="nc-tele-dash">{NONE}</span>
            )
          ) : (
            <>
              <span className="nc-tele-state" style={{ color: meta.color }}>{meta.short}</span>
              {reading.main !== '' && <span className="nc-tele-main tnum">{reading.main}</span>}
              {reading.note && (
                <span className={clsx('nc-tele-note', reading.noteWarn && 'is-warn')} title={reading.noteTitle}>
                  {reading.note}
                </span>
              )}
              <span className="flex-1" />
              {reading.side && <span className="nc-tele-side">{reading.side}</span>}
              {state === 'waiting' && !view.replay && runId && (
                <button
                  ref={approveBtn}
                  type="button"
                  className="nc-approve nodrag nokey"
                  onPointerDown={(e) => e.stopPropagation()}
                  onClick={(e) => { e.stopPropagation(); setApproving((v) => !v) }}
                  aria-haspopup="dialog"
                  aria-expanded={approving}
                  aria-controls={approving ? popId : undefined}
                >
                  去审批
                </button>
              )}
            </>
          )}
        </div>
      </div>

      {/* 精简档（缩放 0.35–0.6）：盒子不变，内容换成大字的标题 + 一个主读数 */}
      <div className="nc-lod" aria-hidden>
        <div className="nc-lod-title">
          {(runActive || state !== 'idle') && <StatusBadge status={state} size={18} animate={false} decorative />}
          <span>{data.label || def?.label}</span>
        </div>
        {/* 这一档只放得下一个读数：状态已经有标题前的剪影、左侧槽和颜色三条通道，
            这里不再写状态字，把位置让给计时、进度；没有读数的状态才写状态字 */}
        <div className="nc-lod-read tnum" style={{ color: state === 'idle' ? undefined : meta.color }}>
          {isTeam && state === 'running'
            ? teamBrief(runtime?.team, nt, true, view.at, ending?.verdict)
            : state === 'idle' ? (runActive ? NONE : '') : reading.main !== '' ? reading.main : meta.short}
        </div>
      </div>

      {/* 信号档（< 0.35）：整张卡成了一块状态色，中间留一枚大号剪影；
          异常节点另挂一枚反向缩放的牌子，任何缩放下都是正常字号 */}
      {(runActive || state !== 'idle') && (
        <div className="nc-sig" aria-hidden>
          <StatusBadge status={state} size={40} animate={false} decorative />
        </div>
      )}
      {pin && (
        <div className="nc-pin" aria-hidden>
          <StatusBadge status={state} size={12} animate={false} decorative />
          <b>{meta.label}</b>
          <span>{data.label || def?.label}</span>
          {state === 'waiting' ? <em className="tnum">{reading.main}</em> : null}
        </div>
      )}

      {/* 校验问题的角标：error 和 warning 同样醒目，谁也不挡谁。只在编辑时挂——运行中、
          结果还留在画布上时，卡片上要读的是状态：实心琥珀的「! 1」和等待审批同一个色相，
          远景里又跟状态牌一样大，满屏的「没指定模型」会把真正的异常淹掉。问题清单照样在
          工具栏的 chip 和问题面板里 */}
      {editing && (hasErrors || hasWarnings) && (
        <div className="nc-issue" title={firstIssue}>
          {hasErrors && <span className="is-error tnum">✕ {errCount}</span>}
          {hasWarnings && <span className="is-warning tnum">! {warnCount}</span>}
        </div>
      )}

      {/* 出具印章：成果节点上，跟着 issuance 事件落下。它是一枚章不是按钮：
          点它是去右栏看完整判定的捷径（悬停时看一眼够不够得着，届时才有手形光标），
          够不着就是一张带悬停说明的图，不装作能点。右栏停在对话层时横幅不在页面上，
          由右栏听 agentlab:goto-issuance 自己切到运行层 */}
      {tier && (
        <span
          role="img"
          aria-label={stampTitle}
          className={clsx('nc-stamp', tierClass, stampLive.current && 'nc-stamp-in', stampLink && 'is-link nodrag')}
          title={stampTitle}
          onPointerEnter={() => setStampLink(issuanceReachable())}
          onPointerDown={stampLink ? (e) => e.stopPropagation() : undefined}
          onClick={stampLink ? (e) => {
            e.stopPropagation()
            gotoIssuance(useStudio.getState().run?.id)
          } : undefined}
        >
          <StatusBadge status={tier === 'formal' ? 'done' : tier === 'withheld' ? 'failed' : 'waiting'} size={11} animate={false} decorative />
          <span aria-hidden>{issuanceLabel(tier)}</span>
          {runClass === 'exploratory' && <small aria-hidden>{RUN_CLASS_LABEL.exploratory} · 不归档</small>}
        </span>
      )}

      {/* 报告节点的章：「引用 N · 无证据 M」，这张图最近一次运行的 report.checked。回放时游标还没走到
          那一刻不画。章不是按钮：完整清单在记录页的「证据」页签 */}
      {reportStamp && (reportStamp.at == null || view.at == null || view.at >= reportStamp.at) && (
        <span role="img" aria-label={reportStampTitle(reportStamp)} title={reportStampTitle(reportStamp)}
              className={clsx('nc-stamp is-evidence', reportStamp.none ? 'is-degraded' : 'is-formal')}
              data-report-stamp="" data-stamp-cited={reportStamp.cited} data-stamp-none={reportStamp.none}>
          <StatusBadge status={reportStamp.none ? 'waiting' : 'done'} size={11} animate={false} decorative />
          <span aria-hidden className="tnum">{reportStampText(reportStamp.cited, reportStamp.none)}</span>
        </span>
      )}

      {/* 出口：标签画在出线第一段的上方，不压在线上。跑过之后命中的那条亮起来、
          落空的压暗——六出口的分支节点跑完，不这样的话谁也说不清它到底选了哪条。
          运行前一律中性色：绿色的「通过」挂在还没跑的分支上，读起来像已经通过了 */}
      {handles.map((handle, i) => {
        const top = handles.length === 1 ? '50%' : `${((i + 1) / (handles.length + 1)) * 100}%`
        const hit = taken ? taken === handle.id : null
        return (
          <div key={handle.id}>
            <Handle
              id={handle.id}
              type="source"
              position={Position.Right}
              isConnectable={isConnectable}
              className={clsx(hit === true && 'handle-taken', hit === false && 'handle-idle')}
              style={{ top, right: -5 }}
            />
            {handle.label && (
              <span
                className={clsx('nc-exit', hit === true && 'is-hit', hit === false && 'is-miss')}
                style={exitRoom?.[handle.id] != null
                  // 截短时往拐弯前收：拐弯从字里穿过去，比少几个字难认得多
                  ? { top, maxWidth: exitRoom[handle.id], overflow: 'hidden', textOverflow: 'ellipsis' }
                  : { top }}
              >
                {handle.label}
              </span>
            )}
          </div>
        )
      })}

      {approving && runId && card.current && (
        <ApprovalPopover
          id={popId}
          nodeId={id}
          runId={runId}
          anchor={card.current}
          returnTo={approveBtn}
          onClose={closeApproval}
        />
      )}
    </div>
  )
}

export const NodeCard = memo(NodeCardImpl)
