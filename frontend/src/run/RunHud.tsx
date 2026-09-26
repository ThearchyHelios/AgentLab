import { useEffect, useMemo, useRef, useState, type ButtonHTMLAttributes, type ReactNode } from 'react'
import {
  ChevronDown, Crosshair, Eraser, Hand, History, LocateFixed, Radio, RotateCw, Square,
} from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { Kbd, Spinner, StatusBadge, toast } from '../components/ui'
import { formatClock, formatCost, formatDuration, formatTokens, NONE } from '../lib/format'
import { isTypingTarget, matchShortcut } from '../lib/keys'
import { statusMeta, STATUS, type StatusCode } from '../lib/status'
import { issuanceLabel, runClassLabel } from '../lib/terms'
import { lastStampOf, projectSettled, useDock, waitedMs } from '../canvas/RunTimeline'
import { structureSig, useStudio } from '../store/studio'
import { topology } from './derive'
import {
  isActivePhase, isSettled, isTerminal, liveAt, project, type NodeState, type RunPhase, type Trace,
} from './trace'
import { useRunClock } from './useRunClock'

/**
 * 运行胶囊与遥测面板。
 *
 * 以前运行时工具栏只剩一个「停止」：跑了多久、跑到哪、卡在哪、花了多少，
 * 要么翻右栏时间线去拼，要么不知道。这里把它们收成一枚胶囊，点开是一块
 * 遥测面板，数字全部来自航迹（run/trace.ts）——画布、时间轴、这里讲同一个故事。
 *
 * 规矩：
 * - 只显示诚实的量。拿不到写「—」；不画确定百分比的进度条（分支图里总有节点
 *   走不到，循环会让同一节点跑好几遍，百分比要么停在 6/8 要么超过 100%）。
 * - 正常态安静：运行中只有一圈转动的徽标；等人、失败、挂起才上底色。
 * - 主动作随相位变：在跑能停止，等人去审批，失败定位 + 接着跑，结束了回放 + 清除。
 *   等审批时后端已经不在执行，停止必然 409，所以那时不给停止。
 */

// -------------------------------------------------------------------------
// 时限：GET /api/system 的 limits，全站取一次
// -------------------------------------------------------------------------

let limitCache: number | null | undefined
let limitLoading: Promise<void> | null = null

function useRunLimit(): number | null {
  const [ms, setMs] = useState<number | null>(limitCache ?? null)
  useEffect(() => {
    if (limitCache !== undefined) return
    limitLoading ??= api.system()
      .then((s) => {
        const sec = Number(s?.limits?.max_run_seconds)
        limitCache = Number.isFinite(sec) && sec > 0 ? sec * 1000 : null
      })
      .catch(() => { limitCache = null })
    void limitLoading.then(() => setMs(limitCache ?? null))
  }, [])
  return ms
}

// -------------------------------------------------------------------------
// 一次运行此刻的样子。胶囊、面板、属性面板底部那条都读它
// -------------------------------------------------------------------------

export interface Attention {
  kind: 'failed' | 'waiting' | 'budget'
  nodeId?: string
  title: string
  detail?: string
}

export interface RunGlance {
  phase: RunPhase
  /** 显示用的状态码（lib/status） */
  code: StatusCode
  label: string
  replay: boolean
  /** 正在看的时刻（毫秒） */
  at: number
  elapsedMs: number
  activeMs: number
  waitMs: number
  /** 一句话：此刻在跑谁 / 停在谁 / 失败于谁 */
  headline: string
  executing: boolean
  /** 拓扑序的每个节点此刻的状态 */
  cells: { id: string; label: string; state: NodeState }[]
  done: number
  visited: number
  parallelNow: number
  series: [number, number][]
  tokens: number | null
  cost: number | null
  /** 这一段执行还剩多少时间；null = 不知道上限或眼下不在计时 */
  remainingMs: number | null
  limitMs: number | null
  attention: Attention[]
  failedNodeId?: string
  waitingNodeId?: string
}

const firstLine = (s: string | undefined | null): string => (s ?? '').trim().split('\n')[0].slice(0, 90)

/** 相位的叫法。挂起在窄处写短名「已中断」，完整说法放 title */
export function phaseLabel(phase: RunPhase): string {
  if (phase === 'suspended') return STATUS.suspended.short
  return statusMeta(phase).label
}

function headlineOf(t: Trace, phase: RunPhase, states: Record<string, NodeState>, at: number,
                    label: (id: string) => string, errorText: string | null | undefined): string {
  const running = Object.keys(states).filter((id) => states[id] === 'running')
  switch (phase) {
    case 'queued':
      return '排队中：等空出执行名额'
    case 'running': {
      const exec = running.filter((id) => !t.nodes[id]?.looping)
      if (!exec.length) {
        const loop = running.find((id) => t.nodes[id]?.looping)
        return loop ? `「${label(loop)}」第 ${t.nodes[loop]?.iteration ?? 1} 轮之间` : '调度下一步…'
      }
      // 协作团队：几个人同时在干
      const team = exec.map((id) => {
        const members = t.nodes[id]?.segments.filter((s) => s.kind === 'member' && s.start <= at
          && (s.end == null || s.end > at)).length ?? 0
        return members > 1 ? `${label(id)} · ${members} 人并行` : label(id)
      })
      return team.slice(0, 2).join(' // ') + (team.length > 2 ? ` 等 ${team.length} 个` : '')
    }
    case 'waiting': {
      // 相位已经写着「等待审批」，这一句说停在谁、等了多久（和坞头、泳道同一个数，见 waitedMs）
      const id = t.waitingNodeId ?? Object.keys(states).find((k) => states[k] === 'waiting')
      if (!id) return '等人处理审批卡'
      const waited = waitedMs(t, id, at)
      return `停在「${label(id)}」${waited != null ? ` · 已等 ${formatClock(waited)}` : ''}`
    }
    case 'failed': {
      const id = t.failedNodeId
      const why = firstLine(t.nodes[id ?? '']?.error || errorText)
      return `${id ? `失败于「${label(id)}」` : '运行失败'}${why ? `：${why}` : ''}`
    }
    case 'succeeded':
      return t.issuance?.tier ? `执行完成 · ${issuanceLabel(t.issuance.tier)}` : '执行完成'
    case 'cancelled': {
      const stopped = Object.keys(states).filter((id) => states[id] === 'cancelled')
      return stopped.length ? `停在「${label(stopped[0])}」` : '已取消'
    }
    case 'suspended':
      return '服务重启时挂起，可从断点接着跑'
    default:
      return ''
  }
}

export function useRunGlance(active: boolean): RunGlance {
  const trace = useStudio((s) => s.trace)
  const phaseLive = useStudio((s) => s.runPhase)
  const replayAt = useStudio((s) => s.replayAt)
  const usage = useStudio((s) => s.usageLive)
  const runUsage = useStudio((s) => s.run?.usage)
  const runError = useStudio((s) => s.run?.error)
  const nodes = useStudio((s) => s.nodes)
  const edges = useStudio((s) => s.edges)
  const limitMs = useRunLimit()
  const ticking = active && replayAt == null && isActivePhase(phaseLive)
  const now = useRunClock(ticking)

  const order = useMemo(() => topology({ nodes, edges }).order, [nodes, edges])
  const labels = useMemo(() => new Map(nodes.map((n) => [n.id, n.data.label || n.id])), [nodes])
  const label = (id: string) => labels.get(id) ?? id

  const live = isActivePhase(phaseLive)
  const at = replayAt ?? (live ? liveAt(trace, now) : lastStampOf(trace))
  // 停下之后按「最后一刻之后」投影：节点状态含推导出的阻断 / 未到达，时长用后端给的
  const proj = replayAt == null && !live ? projectSettled(trace) : project(trace, at)
  const phase = replayAt != null ? proj.phase : phaseLive
  const states: Record<string, NodeState> = {}
  for (const id of order) states[id] = proj.nodes[id]?.state ?? 'idle'
  for (const id of Object.keys(proj.nodes)) states[id] ??= proj.nodes[id].state

  const cells = order.map((id) => ({ id, label: label(id), state: states[id] }))
  const done = cells.filter((c) => c.state === 'done' || c.state === 'skipped').length
  const visited = cells.filter((c) => (trace.nodes[c.id]?.count ?? 0) > 0 || c.state === 'skipped').length

  // 用量：跑的时候看实时累加，终态用后端累计校正（老后端的 agent / 协作调用不发 llm.end）。
  // 还没收到任何一次模型调用的用量时写「—」，不写「0 tok」：那是不知道，不是零
  const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null)
  const settledUsage = isTerminal(phaseLive) && runUsage
  const backendTok = settledUsage && (num(runUsage.input_tokens) != null || num(runUsage.output_tokens) != null)
    ? (num(runUsage.input_tokens) ?? 0) + (num(runUsage.output_tokens) ?? 0)
    : settledUsage ? num(runUsage.total_tokens) : null
  const backendCost = settledUsage ? num(runUsage.cost_usd) : null
  const seen = usage.tokensIn + usage.tokensOut > 0 || usage.costUsd > 0
  const tokens = replayAt != null ? null : backendTok ?? (seen ? usage.tokensIn + usage.tokensOut : null)
  const cost = replayAt != null ? null : backendCost ?? (seen ? usage.costUsd : null)

  // 时限按"这一段执行"计：等人工审批时执行已经结束，那段不算（和后端一致）
  const drive = trace.drives[trace.drives.length - 1]
  const remainingMs = limitMs != null && phase === 'running' && drive && drive[1] == null
    ? Math.max(0, limitMs - (at - drive[0])) : null

  const attention: Attention[] = []
  for (const c of cells) {
    if (c.state !== 'failed') continue
    attention.push({ kind: 'failed', nodeId: c.id, title: `失败 · ${c.label}`, detail: firstLine(trace.nodes[c.id]?.error) || undefined })
  }
  for (const c of cells) {
    if (c.state !== 'waiting') continue
    const waited = waitedMs(trace, c.id, at)
    attention.push({
      kind: 'waiting', nodeId: c.id, title: `等待审批 · ${c.label}`,
      detail: waited != null ? `已等 ${formatClock(waited)}` : undefined,
    })
  }
  if (remainingMs != null && limitMs != null && remainingMs < Math.max(60_000, limitMs * 0.2)) {
    const running = cells.find((c) => c.state === 'running')
    attention.push({
      kind: 'budget', nodeId: running?.id, title: '时限将尽',
      detail: `这一段执行还剩 ${formatClock(remainingMs).replace(/\.\d$/, '')}，上限 ${formatDuration(limitMs)}`,
    })
  }

  return {
    phase, code: phase, label: phaseLabel(phase), replay: replayAt != null, at,
    elapsedMs: proj.elapsedMs, activeMs: proj.activeMs, waitMs: proj.waitMs,
    headline: headlineOf(trace, phase, states, at, label, runError),
    executing: phase === 'running' && cells.some((c) => c.state === 'running' && !trace.nodes[c.id]?.looping),
    cells, done, visited, parallelNow: proj.parallelNow, series: trace.parallelSeries,
    tokens, cost, remainingMs, limitMs, attention,
    failedNodeId: trace.failedNodeId, waitingNodeId: trace.waitingNodeId,
  }
}

// -------------------------------------------------------------------------
// 动作
// -------------------------------------------------------------------------

/**
 * 去审批：不在画布上另做审批控件，只把人带到右栏那张审批卡——同一个组件、同一套
 * 治理口径（只认明确的同意，带备注）。右栏归助手面板：先广播一声，让它在对话层时
 * 切到运行层；再等几帧找卡片，找到就滚过去、描一圈、把焦点放进去。
 */
function gotoApproval(nodeId: string | undefined): void {
  const s = useStudio.getState()
  // 属性面板盖在右栏上：先让开，审批卡在右栏里
  s.select(null)
  if (nodeId) s.focusNode(nodeId)
  window.dispatchEvent(new CustomEvent('agentlab:goto-approval', { detail: { runId: s.run?.id, nodeId } }))
  let tries = 0
  const find = () => {
    const slot = document.querySelector<HTMLElement>('[data-approval-slot]:not(:empty)')
    if (!slot) {
      if (tries++ < 12) requestAnimationFrame(find)
      // 审批列表几秒轮询一次：刚停下来时卡片可能还没到，说实话，别让按钮像是坏了
      else toast.info('审批卡还没出现在右栏：审批列表每几秒刷新一次，稍等再点', { key: 'hud:approval' })
      return
    }
    const card = slot.querySelector<HTMLElement>('[data-approval]') ?? slot
    card.scrollIntoView({ block: 'center', behavior: reducedMotion() ? 'auto' : 'smooth' })
    card.classList.add('sf-flash')
    setTimeout(() => card.classList.remove('sf-flash'), 1600)
    card.querySelector<HTMLElement>('textarea, input, button')?.focus({ preventScroll: true })
  }
  requestAnimationFrame(find)
}

const reducedMotion = (): boolean =>
  typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches

async function stop(): Promise<void> {
  try {
    await useStudio.getState().stopRun()
  } catch (e) {
    toast.error(e)
  }
}

function startReplay(trace: Trace): void {
  if (trace.startedAt == null) return
  useDock.getState().set({ open: true, playing: true })
  useStudio.getState().setReplayAt(trace.startedAt)
}

function clear(): void {
  useDock.getState().set({ playing: false })
  useStudio.getState().clearRun()
}

/** 接着跑：失败的从失败节点续；服务重启挂起的从断点恢复（和记录页同一条路） */
function useResume(phase: RunPhase) {
  const [busy, setBusy] = useState(false)
  const snapshot = useStudio((s) => s.runSnapshot?.structure)
  const nodes = useStudio((s) => s.nodes)
  const edges = useStudio((s) => s.edges)
  const errors = useStudio((s) => s.issues.filter((i) => i.level === 'error').length)
  // 骨架签名要排序，按图算一次；别写进选择器里——那样每个 token 事件都会重排一遍
  const structure = useMemo(
    () => (phase === 'failed' && snapshot != null ? structureSig(nodes, edges) : null),
    [phase, snapshot, nodes, edges],
  )
  const structureChanged = structure != null && snapshot !== structure
  const blockedBy = structureChanged
    ? '增删过节点或连线：接着跑只接受结构不变的图，需要重新运行'
    : phase === 'failed' && errors ? `画布上有 ${errors} 个问题要先改掉` : ''
  const resume = async () => {
    setBusy(true)
    try {
      const s = useStudio.getState()
      if (phase === 'suspended' && s.run) {
        await api.runs.resume(s.run.id, null)
        await s.attachRun(s.run.id)
      } else {
        await s.continueRun()
      }
      toast.ok('从断点接着跑，前面跑过的节点不重来')
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }
  return { busy, resume, blockedBy }
}

// -------------------------------------------------------------------------
// 小件
// -------------------------------------------------------------------------

/** 并行度的迷你阶梯线 */
function Spark({ series, at, width = 64, height = 16 }: { series: [number, number][]; at: number; width?: number; height?: number }) {
  if (series.length < 1) return <span className="sf-dim">{NONE}</span>
  const t0 = series[0][0]
  const span = Math.max(1, at - t0)
  const peak = Math.max(1, ...series.map(([, v]) => v))
  const x = (t: number) => ((Math.min(t, at) - t0) / span) * width
  const y = (v: number) => height - 1 - (v / peak) * (height - 3)
  let d = `M0 ${y(0)}`
  for (const [ts, v] of series) {
    if (ts > at) break
    d += ` H${x(ts).toFixed(1)} V${y(v).toFixed(1)}`
  }
  d += ` H${width}`
  return (
    <svg className="sf-spark" width={width} height={height} aria-hidden>
      <path d={`${d} V${height} H0 Z`} className="sf-spark-area" />
      <path d={d} className="sf-spark-line" />
    </svg>
  )
}

/** 时限余量的小圆环：静态的，不转 */
function Budget({ remaining, limit }: { remaining: number; limit: number }) {
  const r = 7
  const c = 2 * Math.PI * r
  const f = Math.max(0, Math.min(1, remaining / limit))
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" className="sf-budget" aria-hidden>
      <circle cx="9" cy="9" r={r} className="sf-budget-track" />
      <circle cx="9" cy="9" r={r} className="sf-budget-fill" strokeDasharray={`${c * f} ${c}`}
              transform="rotate(-90 9 9)" />
    </svg>
  )
}

/** 拓扑序小格条。格子可以有七十多个：整条只占一个 Tab 位，左右键在格子间走 */
function Cells({ cells, onPick }: { cells: RunGlance['cells']; onPick: (id: string) => void }) {
  const [cur, setCur] = useState(0)
  const box = useRef<HTMLDivElement>(null)
  const at = Math.min(cur, Math.max(0, cells.length - 1))
  const onKeyDown = (e: React.KeyboardEvent) => {
    const next = e.key === 'ArrowRight' ? at + 1 : e.key === 'ArrowLeft' ? at - 1
      : e.key === 'Home' ? 0 : e.key === 'End' ? cells.length - 1 : null
    if (next == null) return
    e.preventDefault()
    const i = Math.max(0, Math.min(cells.length - 1, next))
    setCur(i)
    box.current?.querySelectorAll<HTMLButtonElement>('.sf-cell')[i]?.focus()
  }
  return (
    <div className="sf-cells" role="list" aria-label="各节点状态（按拓扑序，左右键切换）" ref={box} onKeyDown={onKeyDown}>
      {cells.map((c, i) => {
        const name = `${c.label} · ${statusMeta(c.state).label}`
        return (
          <div key={c.id} role="listitem" className="sf-cell-slot">
            <button type="button" className={`sf-cell sf-st-${c.state}`} tabIndex={i === at ? 0 : -1}
                    title={name} aria-label={`${name}，在画布上定位`}
                    onFocus={() => setCur(i)} onClick={() => onPick(c.id)} />
          </div>
        )
      })}
    </div>
  )
}

const COUNT_ORDER: NodeState[] = [
  'running', 'waiting', 'failed', 'suspended', 'done', 'queued', 'blocked', 'unreached', 'skipped', 'cancelled',
]

// -------------------------------------------------------------------------
// 胶囊（工具栏里）+ 展开的面板
// -------------------------------------------------------------------------

export function RunCapsule() {
  const [open, setOpen] = useState(false)
  const g = useRunGlance(true)
  const run = useStudio((s) => s.run)
  const trace = useStudio((s) => s.trace)
  const follow = useStudio((s) => s.follow)
  const phaseLive = useStudio((s) => s.runPhase)
  const { busy, resume, blockedBy } = useResume(g.phase)
  const wrap = useRef<HTMLDivElement>(null)
  const cycle = useRef(0)
  const active = isActivePhase(g.phase)
  const runClass = run?.run_class ?? trace.runClass ?? 'exploratory'
  const focus = (id?: string) => { if (id) useStudio.getState().focusNode(id) }

  // 点外面、按 Esc 关掉面板
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => { if (!wrap.current?.contains(e.target as Node)) setOpen(false) }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.isComposing || isTypingTarget(e.target)) return
      e.preventDefault()
      setOpen(false)
    }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open])

  // F：在「需要处理」里逐个定位（失败 > 等人 > 时限将尽）
  const attentionRef = useRef(g.attention)
  attentionRef.current = g.attention
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || isTypingTarget(e.target) || !matchShortcut(e, 'F')) return
      // 有选中的节点时 F 是"对准选中的"（编排页的快捷键表）。两个监听谁先注册说不准，这里自己让路
      if (useStudio.getState().selectedId) return
      const items = attentionRef.current.filter((a) => a.nodeId)
      if (!items.length) return
      e.preventDefault()
      const item = items[cycle.current % items.length]
      cycle.current += 1
      focus(item.nodeId)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const alert = g.phase === 'waiting' || g.phase === 'failed' || g.phase === 'suspended'
  const terminal = isSettled(g.phase)
  // 运行本身结束了（回放时 g.phase 是游标那一刻的相位）：工具栏上还要放发起按钮，胶囊收窄
  const narrow = isSettled(phaseLive)
  const tokens = g.tokens != null ? formatTokens(g.tokens, { compact: true }) : NONE
  const nodesText = terminal && !g.replay ? `${g.visited}/${g.cells.length}` : `${g.done}/${g.cells.length}`

  // compact = 胶囊里：结束之后发起按钮也要挤进工具栏，次要动作在窄屏上只留图标
  const actions = (compact: boolean) => {
    if (g.replay) {
      return (
        <Act icon={<Radio size={11} />} text="回到实时" title="游标回到现在"
             onClick={() => { useDock.getState().set({ playing: false }); useStudio.getState().setReplayAt(null) }} />
      )
    }
    switch (g.phase) {
      case 'queued':
      case 'running':
        return (
          <Act className="btn-danger" icon={<Square size={10} fill="currentColor" />} text="停止"
               title="停止这次运行" onClick={() => void stop()} />
        )
      case 'waiting':
        return (
          <Act className="sf-btn-warn" icon={<Hand size={11} />} text="去审批"
               title="到右栏的审批卡上处理。等审批时运行已经停在断点上，停止不了"
               onClick={() => gotoApproval(g.waitingNodeId ?? g.attention.find((a) => a.kind === 'waiting')?.nodeId)} />
        )
      case 'failed':
        return (
          <>
            {g.failedNodeId && (
              <Act icon={<LocateFixed size={11} />} text="定位" iconOnly={compact}
                   title="画布取景到失败的节点" onClick={() => focus(g.failedNodeId)} />
            )}
            <Act className="btn-primary" disabled={busy || !!blockedBy} text="接着跑"
                 icon={busy ? <Spinner size={10} /> : <RotateCw size={11} />}
                 title={blockedBy || '从失败的那个节点接着跑，前面跑过的节点不重来。只能改节点配置'}
                 onClick={() => void resume()} />
            {!compact && <ClearButton kbd />}
          </>
        )
      case 'suspended':
        return (
          <>
            <Act className="btn-primary" disabled={busy} text="接着跑"
                 icon={busy ? <Spinner size={10} /> : <RotateCw size={11} />}
                 title="服务重启打断了这次运行，checkpoint 完好，从断点恢复"
                 onClick={() => void resume()} />
            {!compact && <ClearButton kbd />}
          </>
        )
      case 'succeeded':
      case 'cancelled':
        return (
          <>
            {/* 结束之后工具栏上还有两个发起按钮：1536 以下这两个只留图标，面板里有全名 */}
            <Act icon={<History size={11} />} text="回放" textFrom={compact ? '2xl' : undefined}
                 disabled={trace.startedAt == null}
                 title="在画布上按时间重放这次运行" onClick={() => startReplay(trace)} />
            <ClearButton kbd={!compact} textFrom={compact ? '2xl' : undefined} />
          </>
        )
      default:
        return null
    }
  }


  return (
    <div className="relative" ref={wrap}>
      <div className={clsx('sf-capsule', alert && 'is-alert')} data-phase={g.phase} data-replay={g.replay ? '1' : undefined}>
        <button type="button" className="sf-cap-main" onClick={() => setOpen((v) => !v)}
                aria-expanded={open} aria-haspopup="dialog"
                title={open ? '收起运行遥测' : `${g.label} · ${g.headline}\n点开看遥测`}>
          <StatusBadge status={g.code} size={14} animate={g.executing} decorative />
          <span className="sf-cap-label">{g.replay ? `回放 · ${g.label}` : g.label}</span>
          {/* 结束之后工具栏还要放发起按钮：窄屏上计时收进面板和航迹里 */}
          <span className={clsx('sf-cap-sep', narrow && 'hidden xl:block')} />
          <span className={clsx('sf-cap-clock tnum', narrow && 'hidden xl:inline')} data-clock>
            {formatClock(g.elapsedMs)}
          </span>
          {/* 窄处只留相位和计时；节点数、用量在面板里。结束之后胶囊让出地方给发起按钮 */}
          {!narrow && !g.replay && (
            <>
              <span className="sf-cap-sep hidden lg:block" />
              <span className="sf-cap-nodes tnum hidden lg:inline" title="完成的节点 / 全部节点">{nodesText}</span>
              <span className="sf-cap-sep hidden xl:block" />
              <span className="sf-cap-usage tnum hidden xl:inline">
                {tokens}{g.cost ? ` · ${formatCost(g.cost)}` : ''}
              </span>
            </>
          )}
          <ChevronDown size={11} className={clsx('sf-cap-chev', narrow && 'hidden xl:block')}
                       style={{ transform: open ? 'rotate(180deg)' : undefined }} />
        </button>
        <div className="sf-cap-actions">{actions(true)}</div>
      </div>

      {open && (
        <div className="sf-hud sheet-in" role="dialog" aria-label="运行遥测" data-phase={g.phase} data-esc-layer>
          <div className="sf-hud-top">
            <StatusBadge status={g.code} size={18} animate={g.executing} decorative />
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="sf-hud-phase">{g.replay ? `回放 · ${g.label}` : g.label}</span>
                <span className={clsx('sf-class', runClass === 'formal' && 'is-formal')}>
                  {runClassLabel(runClass, run?.version)}{runClass === 'formal' && active ? ' · 画布只读' : ''}
                </span>
              </div>
              <div className="sf-hud-line" title={g.headline}>{g.headline || NONE}</div>
            </div>
            {/* 跟随只对还在跑的运行有意义：结束了、在回放时画布不会自己动 */}
            {isActivePhase(phaseLive) && !g.replay && (
              <label className="sf-follow-toggle" title="跟着正在执行的节点平移画布；手动平移后暂停 10 秒">
                <input type="checkbox" checked={follow} onChange={(e) => useStudio.getState().setFollow(e.target.checked)} />
                <Crosshair size={11} /> 跟随
              </label>
            )}
          </div>

          <div className="sf-hud-grid">
            <div className="sf-metric">
              <div className="sf-metric-k">墙钟</div>
              <div className="sf-metric-v tnum">T+{formatClock(g.elapsedMs)}</div>
              {/* 执行和等人分两行：挤在一行时后半截被截掉，恰好是等人那段 */}
              <div className="sf-metric-s tnum">执行 {formatClock(g.activeMs)}</div>
              {(g.waitMs > 0 || g.phase === 'waiting') && (
                <div className={clsx('sf-metric-s tnum', g.phase === 'waiting' && 'sf-warn')}>等人 {formatClock(g.waitMs)}</div>
              )}
            </div>
            <div className="sf-metric sf-metric-wide">
              <div className="sf-metric-k">节点</div>
              <div className="sf-metric-v tnum">
                {nodesText}
                <span className="sf-metric-u">{terminal && !g.replay ? ' 经过' : ' 完成'}</span>
              </div>
              <Cells cells={g.cells} onPick={focus} />
            </div>
            <div className="sf-metric">
              <div className="sf-metric-k">并行</div>
              <div className="sf-metric-v tnum">{active || g.replay ? `${g.parallelNow} 路` : NONE}</div>
              <Spark series={g.series} at={g.at} />
            </div>
            <div className="sf-metric">
              <div className="sf-metric-k">用量</div>
              <div className="sf-metric-v tnum">{tokens}</div>
              <div className="sf-metric-s tnum">{g.cost != null ? `≈ ${formatCost(g.cost)}` : NONE}</div>
            </div>
            <div className="sf-metric">
              <div className="sf-metric-k">时限</div>
              {g.remainingMs != null && g.limitMs != null ? (
                <>
                  <div className={clsx('sf-metric-v tnum flex items-center gap-1.5',
                    g.remainingMs < g.limitMs * 0.2 && 'sf-warn')}>
                    <Budget remaining={g.remainingMs} limit={g.limitMs} />
                    余 {formatClock(g.remainingMs).replace(/\.\d$/, '')}
                  </div>
                  <div className="sf-metric-s" title="每一段执行各算各的：审批恢复、接着跑之后重新计">
                    上限 {formatDuration(g.limitMs)}
                  </div>
                </>
              ) : (
                <>
                  <div className="sf-metric-v">{g.phase === 'waiting' ? '暂停计时' : NONE}</div>
                  <div className="sf-metric-s" title={g.phase === 'waiting' ? '等人审批不算执行时间，时限不走' : undefined}>
                    {g.phase === 'waiting' ? '等人不计时' : g.limitMs ? `上限 ${formatDuration(g.limitMs)}` : ''}
                  </div>
                </>
              )}
            </div>
          </div>

          <div className="sf-hud-counts">
            {COUNT_ORDER.map((st) => {
              const ids = g.cells.filter((c) => c.state === st).map((c) => c.id)
              if (!ids.length) return null
              const meta = statusMeta(st)
              return (
                <button key={st} type="button" className="sf-count" title={`${meta.label} ${ids.length} 个 · 点一下逐个定位`}
                        onClick={() => { focus(ids[cycle.current % ids.length]); cycle.current += 1 }}>
                  <StatusBadge status={st} size={12} animate={false} decorative />
                  <span className="tnum">{ids.length}</span>
                  <span className="sf-dim">{meta.short}</span>
                </button>
              )
            })}
          </div>

          {g.attention.length > 0 && (
            <div className="sf-queue">
              <div className="sf-queue-head">
                <span>需要处理</span>
                <span className="tnum sf-dim">{g.attention.length}</span>
                <span className="flex-1" />
                <span className="sf-dim">逐个定位</span> <Kbd combo="F" />
              </div>
              {g.attention.map((a, i) => (
                <div key={`${a.kind}${a.nodeId ?? i}`} className={clsx('sf-queue-item', `is-${a.kind}`)}>
                  <StatusBadge status={a.kind === 'budget' ? 'waiting' : a.kind} size={12} animate={false} decorative />
                  <div className="min-w-0 flex-1">
                    <div className="truncate">{a.title}</div>
                    {a.detail && <div className="sf-dim truncate tnum">{a.detail}</div>}
                  </div>
                  {a.kind === 'waiting' ? (
                    <button type="button" className="btn btn-xs" onClick={() => gotoApproval(a.nodeId)}>去审批</button>
                  ) : a.nodeId ? (
                    <button type="button" className="btn btn-xs" onClick={() => focus(a.nodeId)}>定位</button>
                  ) : null}
                </div>
              ))}
            </div>
          )}

          <div className="sf-hud-actions">
            {g.phase === 'failed' && blockedBy && <span className="sf-dim flex-1 truncate" title={blockedBy}>{blockedBy}</span>}
            <span className="flex-1" />
            {actions(false)}
          </div>
        </div>
      )}
    </div>
  )
}

/** 文字从多宽开始露出来。类名要写全，Tailwind 按字面扫描 */
const TEXT_FROM = { xl: 'hidden xl:inline', '2xl': 'hidden 2xl:inline' } as const

/**
 * 胶囊和面板里的动作按钮。iconOnly 只留图标；textFrom 在那个宽度以下只留图标。
 * 文字始终在 aria-label 里，读屏和检查脚本按名字找得到
 */
function Act({ icon, text, className, iconOnly = false, textFrom, children, ...rest }:
  ButtonHTMLAttributes<HTMLButtonElement> & {
    icon: ReactNode; text: string; iconOnly?: boolean; textFrom?: keyof typeof TEXT_FROM
  }) {
  return (
    <button type="button" className={clsx('btn btn-sm', className)} aria-label={text} {...rest}>
      {icon}
      {!iconOnly && <span className={textFrom ? TEXT_FROM[textFrom] : undefined}>{text}</span>}
      {children}
    </button>
  )
}

function ClearButton({ kbd = false, textFrom }: { kbd?: boolean; textFrom?: keyof typeof TEXT_FROM }) {
  return (
    <Act className="btn-ghost" icon={<Eraser size={11} />} text="清除" textFrom={textFrom} onClick={clear}
         title="清除这次运行在画布上的结果，回到编辑态（Esc）">
      {kbd && <Kbd combo="Esc" className="ml-0.5" />}
    </Act>
  )
}

/**
 * Esc 的分层：有别的东西在处理 Esc（输入框、弹窗、属性面板、胶囊面板）时让路；
 * 在回放就先回到实时；运行已经结束才清掉结果。返回 true 表示这一下被用掉了。
 */
export function escapeRun(e: KeyboardEvent): boolean {
  if (e.key !== 'Escape' || e.defaultPrevented || e.isComposing || isTypingTarget(e.target)) return false
  // 弹窗、发起浮层、遥测面板开着时，Esc 是关它们的
  if (document.querySelector('[role="dialog"][aria-modal="true"], [data-esc-layer]')) return false
  // 只认画布这一片（画布、航迹坞、工具栏的运行控件）或焦点哪儿都不在。焦点在右栏的
  // 步骤行、别的面板上时按 Esc 是想收起那一处，把整次运行的结果清掉就太意外了
  const focus = document.activeElement
  if (focus && focus !== document.body && !focus.closest('.react-flow, .tl, [data-run-control]')) return false
  const s = useStudio.getState()
  if (s.selectedId) return false
  if (s.replayAt != null) {
    useDock.getState().set({ playing: false })
    s.setReplayAt(null)
    return true
  }
  if (s.run && isSettled(s.runPhase)) {
    clear()
    return true
  }
  return false
}
