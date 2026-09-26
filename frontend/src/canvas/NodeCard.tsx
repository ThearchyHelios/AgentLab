import {
  memo, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState,
  type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type ReactNode, type RefObject,
} from 'react'
import { createPortal } from 'react-dom'
import { Handle, Position, type Edge, type NodeProps } from '@xyflow/react'
import { Sparkles, Wrench } from 'lucide-react'
import clsx from 'clsx'
import { NODE_DEFS, sourceHandles } from './nodeDefs'
import { NODE_WIDTH } from './routing'
import { LiveClock, stillClock, TeamMatrix, teamBrief } from './TeamMatrix'
import { edgeKey, topology } from '../run/derive'
import { api } from '../api/client'
import { isComposing, StatusBadge } from '../components/ui'
import { formatClock, formatDuration, formatNumber, formatTokens, NONE } from '../lib/format'
import { statusMeta } from '../lib/status'
import { issuanceLabel, RUN_CLASS_LABEL } from '../lib/terms'
import { ApprovalCard } from '../run/RunPanel'
import type { NodeState, NodeTrace } from '../run/trace'
import { loopSince, loopSpan, openSince, useNodeView, type NodeView } from '../run/useNodeView'
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
      return first(config.title) || '等待人工'
    case 'validate':
      return 'JSON Schema 校验' + (config.repair_with_llm ? ' · 失败自动返工' : '')
    case 'metrics':
      return `${config.caliber || '口径'}@${config.caliber_version || 'v1'} · ${(config.metrics ?? []).length} 个指标`
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
const issuanceCache = new WeakMap<RunEvent[], { id: string | null; at: number | null }>()
function issuanceOf(events: RunEvent[]): { id: string | null; at: number | null } {
  const hit = issuanceCache.get(events)
  if (hit) return hit
  let found = { id: null as string | null, at: null as number | null }
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const e = events[i]
    if (e.type === 'issuance') {
      found = { id: e.node_id ?? null, at: typeof e.ts === 'number' ? e.ts * 1000 : null }
      break
    }
  }
  issuanceCache.set(events, found)
  return found
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

/** 完成之后的产出量：模型出了多少 token、查到几行、取回几条 */
function outputOf(n: NodeTrace | undefined, preview: any): string {
  if (n && n.tokensOut > 0) return formatTokens(n.tokensOut, { compact: true })
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

function readingOf({ state, view, type, config, skewMs, timed, fanIn }: ReadingInput): Reading {
  const n = view.trace
  const runtime = view.runtime
  const at = view.at
  // 回放时计时是定值：游标减去那一段的起点。实时的交给 LiveClock 自己走
  const clock = (from: number | undefined, coarse = false): ReactNode => {
    if (at != null) return stillClock(from != null ? at - from : undefined)
    return timed && from != null ? <LiveClock from={from} skewMs={skewMs} coarse={coarse} /> : NONE
  }
  const loop = (phase: 'live' | 'done' | 'stopped') => (
    <LoopProgress n={n} iteration={view.iteration} config={config} phase={phase} />
  )
  const tokens = (n?.tokensIn ?? 0) + (n?.tokensOut ?? 0)
  const failedTools = (runtime?.toolCalls ?? []).filter((c) => c.ok === false).length
  const runningTool = n?.toolsRunning
    ? [...(runtime?.toolCalls ?? [])].reverse().find((c) => c.ok === undefined)?.tool
    : undefined
  // 工具明细放悬停：谁调的（成员首字）、调了什么、成没成
  const toolTitle = (runtime?.toolCalls ?? []).slice(-8).map((c) =>
    `${c.agent ? `[${c.agent.slice(0, 1)}] ` : ''}${c.tool} ${c.ok === false ? '✗ 失败' : c.ok ? '✓' : '⋯ 进行中'}`).join('\n')
  const side = (
    <>
      {tokens > 0 && (
        <span className="tnum" title={`输入 ${formatNumber(n?.tokensIn)} · 输出 ${formatNumber(n?.tokensOut)} tokens`}>
          {formatTokens(tokens, { compact: true })}
        </span>
      )}
      {!!n?.tools && (
        <span
          className={clsx('nc-tools tnum', runningTool && 'is-live', failedTools && 'is-bad')}
          title={toolTitle}
        >
          <Wrench size={9} aria-hidden />
          {runningTool ? `${runningTool} ⋯` : n.tools}
        </span>
      )}
    </>
  )

  // 航迹里的 token、工具是整次运行的累计，回放到半路时它们还没发生：不拿终值冒充那一刻
  const final = at == null || view.count === (n?.count ?? 0)

  switch (state) {
    case 'running':
      if (type === 'loop') {
        return { main: loop('live'), side: clock(loopSince(n, at), true) }
      }
      return { main: clock(openSince(n, 'run', at)), side: at == null ? side : undefined }
    case 'waiting':
      return { main: <>已等 {clock(openSince(n, 'wait', at), true)}</> }
    case 'done':
      if (type === 'loop') {
        return { main: loop('done'), side: <span className="tnum">{formatDuration(timed ? loopSpan(n, at) ?? n?.lastDurationMs : n?.lastDurationMs)}</span> }
      }
      return {
        main: formatDuration(at != null ? view.replayElapsedMs : n?.lastDurationMs ?? runtime?.durationMs),
        note: final ? outputOf(n, runtime?.preview) : '',
        // 跑完了只留工具次数：token 已经在产出量里说过（输出多少），再挂一个总数是两套数
        side: n?.tools && final ? (
          <span className={clsx('nc-tools tnum', failedTools && 'is-bad')} title={toolTitle}>
            <Wrench size={9} aria-hidden />{n.tools}
          </span>
        ) : undefined,
      }
    case 'failed': {
      const error = n?.error || runtime?.error || ''
      if (type === 'loop') {
        return { main: loop('stopped'), note: firstSentence(error), noteTitle: error }
      }
      return {
        main: formatDuration(n?.lastDurationMs ?? runtime?.durationMs),
        note: firstSentence(error) || '没有给出原因',
        noteTitle: error,
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
        main: n?.startedAt != null && n.endedAt != null && timed ? `停在 ${formatClock(n.endedAt - n.startedAt)}` : '',
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
    (type === 'output' ? (s.run?.output as any)?._issuance as Record<string, any> | undefined : undefined))
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

  const reading = readingOf({ state, view, type, config: data.config, skewMs, timed, fanIn })

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
    nt.tokensIn + nt.tokensOut > 0 ? `输入 ${formatNumber(nt.tokensIn)} · 输出 ${formatNumber(nt.tokensOut)} tokens` : '',
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
    runClass === 'exploratory' ? '探索运行的结论不进正式归档' : '',
    '完整判定在右栏运行视图的成果区',
  ].filter(Boolean).join('\n') : ''

  const rank = typeof (data as any).rank === 'number' ? (data as any).rank : undefined
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
        handles.length >= 5 && 'nc-exits-dense',
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
                <span className="nc-tele-note" title={reading.noteTitle}>{reading.note}</span>
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
            ? teamBrief(runtime?.team, nt, true, view.at)
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

      {/* 校验问题的角标：error 和 warning 同样醒目，谁也不挡谁 */}
      {(hasErrors || hasWarnings) && (
        <div className="nc-issue" title={firstIssue}>
          {hasErrors && <span className="is-error tnum">✕ {errCount}</span>}
          {hasWarnings && <span className="is-warning tnum">! {warnCount}</span>}
        </div>
      )}

      {/* 出具印章：成果节点上，跟着 issuance 事件落下。它是一枚章不是按钮：
          只有右栏的出具横幅在页面上时，点它才滚过去（悬停时看一眼，届时才有
          手形光标）；没有横幅就是一张带悬停说明的图，不装作能点 */}
      {tier && (
        <span
          role="img"
          aria-label={stampTitle}
          className={clsx('nc-stamp', tierClass, stampLive.current && 'nc-stamp-in', stampLink && 'is-link nodrag')}
          title={stampTitle}
          onPointerEnter={() => setStampLink(!!document.querySelector('[data-issuance-banner]'))}
          onPointerDown={stampLink ? (e) => e.stopPropagation() : undefined}
          onClick={stampLink ? (e) => {
            const banner = document.querySelector('[data-issuance-banner]')
            if (!banner) return
            e.stopPropagation()
            banner.scrollIntoView({ behavior: 'smooth', block: 'center' })
          } : undefined}
        >
          <StatusBadge status={tier === 'formal' ? 'done' : tier === 'withheld' ? 'failed' : 'waiting'} size={11} animate={false} decorative />
          <span aria-hidden>{issuanceLabel(tier)}</span>
          {runClass === 'exploratory' && <small aria-hidden>{RUN_CLASS_LABEL.exploratory} · 不归档</small>}
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
                style={{ top }}
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
