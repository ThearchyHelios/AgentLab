import { useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Crosshair, ListTree, TriangleAlert, X } from 'lucide-react'
import clsx from 'clsx'
import '../../canvas/surface.css'
import {
  MIN_H, RunTimeline, dockHeightFor, lastStampOf, projectSettled, waitedMs, type DockUi,
} from '../../canvas/RunTimeline'
import { EmptyState, StatusBadge, StatusPill } from '../../components/ui'
import { explainRunError } from '../../lib/explain'
import {
  NONE, formatCost, formatDateTime, formatNumber, formatOffset, formatSpan, formatTokens, shortId,
} from '../../lib/format'
import { runClassLabel, nodeTypeLabel } from '../../lib/terms'
import { topology, type GraphLike } from '../../run/derive'
import {
  isActivePhase, isSettled, liveAt, project, type NodeState, type Projection, type RunPhase, type Trace,
} from '../../run/trace'
import {
  execStart, factsOf, lastIn, openCall, projectCached, replayTraceCached, type FactMark, type NodeFacts,
} from '../../run/useNodeView'
import { useRunClock } from '../../run/useRunClock'
import type { StatusCode } from '../../lib/status'
import type { GraphSpec, Run, RunEvent } from '../../types'
import { cancelReason } from './model'

const EMPTY_GRAPH: GraphSpec = { nodes: [], edges: [] }

const DOCK_KEY = 'agentlab.runs.dock'
function readDock(): Pick<DockUi, 'open' | 'compress'> {
  try {
    const raw = JSON.parse(localStorage.getItem(DOCK_KEY) ?? 'null')
    if (raw && typeof raw === 'object') {
      return { open: raw.open !== false, compress: raw.compress !== false }
    }
  } catch { /* 隐私窗口、禁用存储：用默认值 */ }
  return { open: true, compress: true }
}
function saveDock(ui: DockUi): void {
  try { localStorage.setItem(DOCK_KEY, JSON.stringify({ open: ui.open, compress: ui.compress })) } catch { /* 记不住就算了 */ }
}

/**
 * 航迹页签：和画布底部同一个航迹坞（canvas/RunTimeline），上面换成「游标读数」——
 * 画布上拖游标，卡片回到那一刻；这里没有画布，拖到哪一刻，就读出那一刻的相位、
 * 三种时长、用量、在跑和在等的节点，点一条泳道看那个节点那一刻的样子。
 *
 * 用的是运行时的图快照（GET /runs/{id}/graph），不是工作流现在的样子：未保存的
 * 工作流、改过结构的工作流，也只有这里能看到当时的全貌。
 */
export function TracePane({
  run, events, trace, graph, code, replayAt, onReplayAt, selected, onSelect, labelOf, nodeHref, goneFromCanvas, onRevealStep,
}: {
  run: Run
  /** 航迹不存的几件事（在调哪个工具、模型把工具调用写成了文字…）要从事件里捞，和画布卡片同一份 */
  events: RunEvent[]
  trace: Trace
  graph: GraphSpec | null
  /** 详情头的显示码：没拖游标时状态格和它说同一句 */
  code: StatusCode
  replayAt: number | null
  onReplayAt: (at: number | null) => void
  selected: string | null
  onSelect: (id: string | null) => void
  labelOf: (id?: string | null) => string | undefined
  /** 在画布里看这个节点（带 run 和 focus）；回不到画布的返回 null */
  nodeHref: (id: string) => string | null
  /** 工作流之后改过结构，这个节点在现在的图里已经没有了（节点卡说明为什么没有「在画布中看」） */
  goneFromCanvas?: (id: string) => boolean
  /** 切回时间线并描出这个节点的那几步 */
  onRevealStep: (id: string) => void
}) {
  const g = graph ?? EMPTY_GRAPH
  const [hovered, setHovered] = useState<string | null>(null)

  // ---- 坞：开合、压缩空闲记在本机；高度按读数区和泳道分，拖过就听人的 ----
  const box = useRef<HTMLDivElement>(null)
  const [ui, setUi] = useState<DockUi>(() => ({ ...readDock(), height: 0, playing: false, speed: 1 }))
  const sized = useRef(false)
  // 泳道一条不裁时坞要多高：排版的几个常量只在 RunTimeline 里有一份，这里不再抄
  const fullDock = useMemo(() => dockHeightFor(trace, g), [trace, g])
  // 读数区先拿够它要的（八格读数，加上此刻、节点卡、关键时刻），坞拿剩下的：屏幕矮、
  // 节点多时，按泳道要的高度给坞会把读数区挤得只剩八格，默认选中的失败节点卡、关键
  // 时刻全落在视线外。坞最矮到能拖到的最矮（MIN_H），泳道在坞里滚。只在面板尺寸、泳道数变了时
  // 重分：拖游标、点节点时读数区的高度在变，坞跟着跳的话，手底下的泳道会挪走
  const empty = !Object.keys(trace.nodes).length && trace.startedAt == null
  useLayoutEffect(() => {
    const el = box.current
    if (!el) return
    const fit = () => {
      const h = el.clientHeight
      const cap = Math.max(MIN_H, h * 0.7)
      const read = el.querySelector<HTMLElement>('[data-trace-readout]')
      const need = read ? [...read.children].reduce((n, c) => n + c.getBoundingClientRect().height, 0) : 0
      setUi((cur) => {
        const want = sized.current ? cur.height : Math.min(fullDock, h - Math.ceil(need))
        const height = Math.round(Math.min(cap, Math.max(MIN_H, want)))
        return height === cur.height ? cur : { ...cur, height }
      })
    }
    fit()
    const ro = new ResizeObserver(fit)
    ro.observe(el)
    return () => ro.disconnect()
  }, [fullDock, empty])
  const patchUi = (p: Partial<DockUi>) => {
    if ('height' in p) sized.current = true
    setUi((cur) => {
      const next = { ...cur, ...p }
      if ('open' in p || 'compress' in p) saveDock(next)
      return next
    })
  }

  // ---- 游标那一刻 ----
  // 停在审批上的也算「还在走」：等人的时长在涨，和下面坞里的游标同一拍走
  const live = replayAt == null
  const active = isActivePhase(trace.phase)
  const now = useRunClock(live && active)
  const t0 = trace.startedAt
  const at = replayAt ?? (active ? liveAt(trace, now) : lastStampOf(trace))
  const proj: Projection = !live ? projectCached(trace, replayAt!) : active ? project(trace, liveAt(trace, now)) : projectSettled(trace)
  // 回放时的节点状态要按图再推一遍（排队、阻断、未到达），和画布卡片读同一份
  const view = useMemo(() => (live ? trace : replayTraceCached(trace, replayAt!, g as GraphLike)), [live, trace, replayAt, g])
  const order = useMemo(() => {
    const ids = topology(g as GraphLike).order
    return [...ids, ...Object.keys(trace.nodes).filter((id) => !ids.includes(id))]
  }, [g, trace.nodes])
  const stateOf = (id: string): NodeState => view.nodes[id]?.state ?? 'idle'
  const facts = useMemo(() => factsOf(events), [events])
  const moments = useMemo(
    () => momentsOf(trace, facts, order, labelOf, code, run.error),
    [trace, facts, order, labelOf, code, run.error],
  )

  if (empty) {
    return (
      <EmptyState
        icon={<ListTree size={22} />}
        title="本次运行没有节点执行记录"
        body="运行在开始前就已停止，或该记录缺少节点事件：航迹需要节点的开始和结束事件才能展开。"
        className="h-full"
      />
    )
  }

  const title = `${runClassLabel(run.run_class ?? trace.runClass ?? 'exploratory', run.version)} ${shortId(run.id)}`
  const pick = (id: string | null) => onSelect(id === selected ? null : id)

  return (
    <div ref={box} className="flex h-full min-h-0 flex-col" data-run-trace="">
      <Readout
        trace={trace} proj={proj} at={at} t0={t0} live={live} active={active} code={code}
        order={order} stateOf={stateOf} labelOf={labelOf} hovered={hovered} onHover={setHovered}
        selected={selected} onPick={pick} graph={g} nodeHref={nodeHref} goneFromCanvas={goneFromCanvas}
        onRevealStep={onRevealStep} facts={facts} moments={moments} onReplayAt={onReplayAt}
      />
      {ui.height > 0 && (
        <RunTimeline
          trace={trace}
          graph={g}
          title={title}
          replayAt={replayAt}
          onReplayAt={onReplayAt}
          hoveredNodeId={hovered ?? selected}
          onHoverNode={setHovered}
          onFocusNode={(id) => onSelect(id)}
          ui={ui}
          onUi={patchUi}
        />
      )}
    </div>
  )
}

// -------------------------------------------------------------------------
// 游标读数
// -------------------------------------------------------------------------

function Readout({
  trace, proj, at, t0, live, active, code, order, stateOf, labelOf, hovered, onHover, selected, onPick, graph,
  nodeHref, goneFromCanvas, onRevealStep, facts, moments, onReplayAt,
}: {
  trace: Trace; proj: Projection; at: number; t0?: number; live: boolean; active: boolean; code: StatusCode
  order: string[]; stateOf: (id: string) => NodeState; labelOf: (id?: string | null) => string | undefined
  hovered: string | null; onHover: (id: string | null) => void
  selected: string | null; onPick: (id: string | null) => void; graph: GraphSpec
  nodeHref: (id: string) => string | null; goneFromCanvas?: (id: string) => boolean
  onRevealStep: (id: string) => void
  facts: Record<string, NodeFacts>; moments: Moment[]; onReplayAt: (at: number | null) => void
}) {
  // 时刻是秒级浮点换算来的，差的那一丝不该让 2.1 s 显示成 00:02.0：先取整到毫秒
  const offset = t0 != null && trace.timed ? Math.max(0, Math.round(at - t0)) : null
  const mode = !live ? '回放' : active ? '实时' : '终态'
  const phase: string = live ? code : proj.phase
  // 整次运行一次模型都没调过：和详情头一样写「—」，不写「0 tok / $0」
  const metered = trace.usageSeries.length > 0 || trace.tokensIn + trace.tokensOut > 0 || trace.costUsd > 0
  const tokens = metered && proj.tokensIn != null && proj.tokensOut != null ? proj.tokensIn + proj.tokensOut : undefined

  // 下面还有没露出来的（节点卡、关键时刻）：底边淡出一截，看得出能往下滚
  const scroller = useRef<HTMLDivElement>(null)
  const [more, setMore] = useState(false)
  useLayoutEffect(() => {
    const el = scroller.current
    if (!el) return
    const check = () => setMore(el.scrollHeight - el.scrollTop - el.clientHeight > 4)
    check()
    el.addEventListener('scroll', check, { passive: true })
    const ro = new ResizeObserver(check)
    ro.observe(el)
    for (const c of el.children) ro.observe(c)
    return () => {
      el.removeEventListener('scroll', check)
      ro.disconnect()
    }
  }, [])

  return (
    <div ref={scroller} className="runs-trace-read min-h-0 flex-1 overflow-y-auto" data-trace-readout="" data-mode={mode}
         data-more={more ? '' : undefined}>
      <div className="runs-trace-grid border-b">
        {/* 实时、回放、终态写在标签那一行：数值那一行要留给跨天的时刻 */}
        <Metric label="游标" data="at" title={trace.timed ? formatDateTime(at) : '该记录没有时间戳，只能按先后顺序回放'}
                tag={<span className={clsx(!live && 'text-[var(--accent)]')} data-readout-mode="">{mode}</span>}>
          {offset != null ? `T+${formatOffset(offset)}` : NONE}
        </Metric>
        <Metric label="状态" data="phase">
          {/* 格子窄，两种挂起写短名（「已挂起 · 可续跑」会被截成「已挂起 · 可续…」）；全称在详情头 */}
          <StatusPill status={phase} short={phase === 'held' || phase === 'suspended'} className="-ml-1.5" />
        </Metric>
        <Metric label="总时长" data="wall" title="从首次开始到游标所在时刻的总时长">{span(proj.elapsedMs, trace.timed)}</Metric>
        <Metric label="执行时长" data="active" title="截至游标所在时刻，各段执行时长之和">{span(proj.activeMs, true)}</Metric>
        <Metric label="等待审批" data="wait" title="截至游标所在时刻，等待人工审批的时长"
                tone={proj.phase === 'waiting' ? 'var(--st-waiting)' : undefined}>
          {span(proj.waitMs, trace.timed)}
        </Metric>
        <Metric label="节点" data="nodes" title="已完成（含跳过）/ 工作流里的节点总数"
                sub={proj.parallelNow > 1 ? `${proj.parallelNow} 个并行` : undefined}>
          {proj.nodesTotal ? <>{proj.nodesDone}<span className="text-faint">/{proj.nodesTotal}</span></> : NONE}
        </Metric>
        <Metric label="用量" data="tokens"
                title={!metered ? '本次运行未调用模型'
                  : tokens == null ? '各次模型调用的用量之和与服务端总数不一致（部分调用未上报用量），无法给出此刻的准确读数'
                  : '截至游标所在时刻的 token 用量'}
                sub={tokens ? `输入 ${formatNumber(proj.tokensIn)} · 输出 ${formatNumber(proj.tokensOut)}` : undefined}>
          {tokens == null ? NONE : formatTokens(tokens, { compact: true })}
        </Metric>
        <Metric label="成本" data="cost">{!metered || proj.costUsd == null ? NONE : formatCost(proj.costUsd)}</Metric>
      </div>

      <div className="runs-trace-cols">
        <NowList trace={trace} proj={proj} at={at} order={order} stateOf={stateOf} labelOf={labelOf}
                 hovered={hovered} onHover={onHover} selected={selected} onPick={onPick} />
        {selected
          ? <NodeCard id={selected} trace={trace} proj={proj} state={stateOf(selected)} graph={graph}
                      facts={facts[selected]} at={live ? null : at}
                      labelOf={labelOf} href={nodeHref(selected)} gone={!!goneFromCanvas?.(selected)}
                      onClose={() => onPick(null)} onRevealStep={onRevealStep} />
          : <Slowest trace={trace} order={order} labelOf={labelOf} activeMs={projectSettledActive(trace)}
                     onHover={onHover} onPick={onPick} />}
        <Moments moments={moments} at={at} live={live} t0={trace.timed ? t0 : undefined}
                 onHover={onHover} onJump={(m) => onReplayAt(m.end ? null : m.at)} />
      </div>
    </div>
  )
}

/** 整次运行的执行时长：最耗时那一栏算占比用，和游标无关 */
function projectSettledActive(t: Trace): number {
  return t.timing?.activeMs ?? t.drives.reduce((n, [a, b]) => n + Math.max(0, (b ?? a) - a), 0)
}

/** 没有时间戳的老数据，墙钟和等人都不可知 */
const span = (ms: number | undefined, known: boolean) => (known && ms != null ? formatSpan(ms) : NONE)

function Metric({ label, tag, data, title, sub, tone, children }: {
  label: string; tag?: ReactNode; data: string; title?: string; sub?: string; tone?: string; children: ReactNode
}) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 px-3 py-1.5" title={title}>
      <div className="flex min-w-0 gap-1.5 truncate text-2xs text-faint">{label}{tag}</div>
      <div className="mono tnum flex min-w-0 items-center truncate text-sm leading-5 text-fg"
           style={tone ? { color: tone } : undefined} data-readout={data}>
        {children}
      </div>
      {sub && <div className="tnum truncate text-2xs text-faint">{sub}</div>}
    </div>
  )
}

// -------------------------------------------------------------------------
// 此刻在做什么
// -------------------------------------------------------------------------

function NowList({ trace, proj, at, order, stateOf, labelOf, hovered, onHover, selected, onPick }: {
  trace: Trace; proj: Projection; at: number; order: string[]; stateOf: (id: string) => NodeState
  labelOf: (id?: string | null) => string | undefined
  hovered: string | null; onHover: (id: string | null) => void
  selected: string | null; onPick: (id: string | null) => void
}) {
  // 循环容器在两轮之间也是 running，真正在跑的是循环体：不列它
  const running = order.filter((id) => stateOf(id) === 'running' && !trace.nodes[id]?.looping)
  const waiting = order.filter((id) => stateOf(id) === 'waiting')
  const settled = proj.phase === 'succeeded' || proj.phase === 'failed' || proj.phase === 'cancelled' || proj.phase === 'suspended'

  let empty: ReactNode = null
  if (!running.length && !waiting.length) {
    if (proj.phase === 'idle' || proj.phase === 'queued' || (trace.startedAt != null && at < trace.startedAt)) {
      empty = '尚未开始'
    } else if (settled) {
      const tally = new Map<NodeState, number>()
      for (const id of order) tally.set(stateOf(id), (tally.get(stateOf(id)) ?? 0) + 1)
      const parts = ([['done', '完成'], ['failed', '失败'], ['skipped', '跳过'], ['blocked', '阻断'], ['unreached', '未到达'],
        ['cancelled', '取消']] as [NodeState, string][])
        .filter(([s]) => tally.get(s)).map(([s, l]) => `${l} ${tally.get(s)}`)
      empty = `已停止：${parts.join(' · ') || '没有节点执行'}`
    } else {
      empty = '此刻没有节点在执行：处于两步之间，或正在等待下游汇合'
    }
  }

  const chip = (id: string, state: NodeState, text: string) => (
    <button
      key={id}
      type="button"
      className={clsx(
        'inline-flex max-w-full items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs transition-colors hover:bg-hover',
        (hovered === id || selected === id) && 'bg-hover',
      )}
      style={{ borderColor: `color-mix(in srgb, ${state === 'waiting' ? 'var(--st-waiting)' : 'var(--st-running)'} 45%, var(--border))` }}
      onMouseEnter={() => onHover(id)}
      onMouseLeave={() => onHover(null)}
      onFocus={() => onHover(id)}
      onBlur={() => onHover(null)}
      onClick={() => onPick(id)}
      aria-pressed={selected === id}
      data-now-node={id}
    >
      <StatusBadge status={state} size={11} animate={false} decorative />
      <span className="min-w-0 truncate text-fg">{labelOf(id) ?? id}</span>
      <span className="tnum shrink-0 text-2xs text-faint">{text}</span>
    </button>
  )

  return (
    // 标题和内容写在同一行：多半只有一两个节点或一句话，单占一行的标题白占高度
    <section className="runs-trace-now min-w-0 px-4 py-2" aria-label="游标所在时刻" data-trace-now="">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
        <h4 className="text-2xs font-medium text-faint">此刻</h4>
        {empty
          ? <p className="text-xs text-dim">{empty}</p>
          : (
            <>
              {running.map((id) => chip(id, 'running', `已执行 ${formatSpan(proj.nodes[id]?.elapsedMs)}`))}
              {waiting.map((id) => {
                const w = waitedMs(trace, id, at)
                return chip(id, 'waiting', w != null ? `等待审批 ${formatSpan(w)}` : '等待审批')
              })}
            </>
          )}
      </div>
    </section>
  )
}

// -------------------------------------------------------------------------
// 关键时刻
// -------------------------------------------------------------------------

interface Moment {
  key: string
  at: number
  /** 徽标：运行或节点的状态；warn 是引擎判了降档、失败的那几种警告 */
  status: string | 'warn'
  text: string
  sub?: string
  nodeId?: string
  /** 结局那一条：跳过去就是终态（推导出的阻断、未到达都在），不是按段重建的那一刻 */
  end?: boolean
}

/** 列太多就不是「关键」了：循环里几十次重试时只列前面这些 */
const MOMENTS_MAX = 40

/**
 * 一次运行里值得停下来看的几个时刻：开始、停下等审批、审批处理完、失败、重试、
 * 接着跑、引擎的警告、结局。都来自航迹（和坞里的泳道同一份）和节点上的那几件事，
 * 不另读事件。长时间等人被压缩的空档、几百条事件的循环里，拖游标很难正好停在
 * 那一下——这里点一下就到
 */
function momentsOf(
  trace: Trace, facts: Record<string, NodeFacts>, order: string[],
  labelOf: (id?: string | null) => string | undefined, code: StatusCode, error: string | null | undefined,
): Moment[] {
  const out: Moment[] = []
  const name = (id: string) => `「${labelOf(id) ?? id}」`
  if (trace.startedAt != null) out.push({ key: 'start', at: trace.startedAt, status: 'running', text: '开始运行' })
  const ended = isSettled(trace.phase) && trace.endedAt != null
  for (const id of order) {
    const n = trace.nodes[id]
    if (!n) continue
    const lastFail = n.segments.reduce((k, s, i) => (s.kind === 'run' && s.status === 'failed' ? i : k), -1)
    n.segments.forEach((s, i) => {
      if (s.kind === 'wait') {
        out.push({ key: `${id}:w${i}`, at: s.start, status: 'waiting', text: `${name(id)}等待审批`, nodeId: id })
        // 以放弃、挂起收场的等待，结局那一条会说
        if (s.end != null && s.status !== 'cancelled' && s.status !== 'suspended') {
          out.push({
            key: `${id}:r${i}`, at: s.end, status: 'done', text: `${name(id)}审批已处理`,
            sub: `等了 ${formatSpan(s.end - s.start)}`, nodeId: id,
          })
        }
      } else if (s.kind === 'retry') {
        out.push({ key: `${id}:t${i}`, at: s.start, status: 'running', text: `${name(id)}重试`, nodeId: id })
      } else if (s.kind === 'run' && s.status === 'failed' && s.end != null) {
        // 让整次运行失败的那一下由结局那一条说，这里只列失败了还接着走下去的
        if (ended && trace.phase === 'failed' && id === trace.failedNodeId && i === lastFail) return
        out.push({
          key: `${id}:f${i}`, at: s.end, status: 'failed', text: `${name(id)}失败`, nodeId: id,
          sub: i === lastFail && n.error ? explainRunError(n.error).title : undefined,
        })
      }
    })
    // 引擎的警告一次执行一件：同一个节点跑了几次，每次的都各算一个时刻，说法和节点卡同一份。
    // 只取整次运行最后那一下的话，前几次的从列表里消失，列表和节点卡就对不上了。
    // 最后一件的 key 不带序号，老链接和检查照旧认得
    const f = facts[id]
    const each = <T extends FactMark>(list: T[] | undefined, tag: string, say: (m: T) => Pick<Moment, 'text' | 'sub'>) =>
      (list ?? []).forEach((m, i, all) => out.push({
        key: i === all.length - 1 ? `${id}:${tag}` : `${id}:${tag}${i}`, at: m.at, status: 'warn', nodeId: id, ...say(m),
      }))
    each(f?.markups, 'markup', (m) => (m.settle
      ? { text: `${name(id)}收尾时仍试图调用工具`, sub: '结果可能不完整' }
      : { text: `${name(id)}以文本形式输出了工具调用`, sub: '已提醒并重新作答' }))
    each(f?.exhausts, 'exhausted', () => ({ text: `${name(id)}协作轮数已用完，降档交付` }))
    each(f?.inventions, 'invented', () => ({ text: `${name(id)}校验修复时生成了原文中不存在的值` }))
  }
  // 失败、挂起之后又跑起来：接着跑。等审批之后的恢复已经由「审批已处理」说了
  let prev: RunPhase | null = null
  trace.phases.forEach(([at, phase], i) => {
    if (phase === 'running' && (prev === 'failed' || prev === 'suspended')) {
      out.push({ key: `go${i}`, at, status: 'running', text: prev === 'failed' ? '从失败处继续运行' : '从断点继续运行' })
    }
    prev = phase
  })
  if (ended) {
    const failed = trace.phase === 'failed' ? trace.failedNodeId : undefined
    const why = trace.phase === 'failed'
      ? explainRunError((failed && trace.nodes[failed]?.error) || error).title
      : trace.phase === 'cancelled' ? cancelReason(error) ?? undefined
      : trace.phase === 'suspended' ? error?.trim() || undefined : undefined
    out.push({
      key: 'end', at: trace.endedAt!, end: true,
      // 挂起分两种（服务重启、可续跑），详情头已经分好了，照它的说
      status: trace.phase === 'suspended' ? code : trace.phase,
      text: trace.phase === 'succeeded' ? '运行完成'
        : failed ? `失败于${name(failed)}`
        : trace.phase === 'failed' ? '运行失败'
        : trace.phase === 'cancelled' ? '运行取消' : '运行挂起',
      sub: why,
    })
  }
  return out.sort((a, b) => a.at - b.at || (a.end ? 1 : 0) - (b.end ? 1 : 0))
}

function Moments({ moments, at, live, t0, onHover, onJump }: {
  moments: Moment[]; at: number; live: boolean; t0?: number
  onHover: (id: string | null) => void; onJump: (m: Moment) => void
}) {
  if (moments.length < 2) return null
  // 游标落在哪一段里：最后一个不晚于它的时刻。停下的运行没拖游标时就是结局
  const settledEnd = live && moments[moments.length - 1].end
  let current = -1
  if (settledEnd) current = moments.length - 1
  else for (let i = 0; i < moments.length; i += 1) if (moments[i].at <= at) current = i
  const shown = moments.length > MOMENTS_MAX
    ? [...moments.slice(0, MOMENTS_MAX - 1), moments[moments.length - 1]]
    : moments
  const hidden = moments.length - shown.length

  return (
    <section className="runs-trace-moments min-w-0 px-4 py-2.5" aria-label="关键时刻" data-trace-moments="">
      <h4 className="mb-1 flex items-center gap-2 text-2xs font-medium text-faint">
        关键时刻
        <span className="font-normal">· 点击可将游标定位到该时刻</span>
      </h4>
      <ol className="runs-moments -mx-1">
        {shown.map((m) => {
          const i = moments.indexOf(m)
          const here = i === current
          return (
            <li key={m.key}>
              <button
                type="button"
                className={clsx(
                  'items-center rounded px-1 py-0.5 text-left text-xs transition-colors hover:bg-hover',
                  here && 'bg-hover',
                )}
                aria-current={here ? 'step' : undefined}
                onClick={() => onJump(m)}
                onMouseEnter={() => m.nodeId && onHover(m.nodeId)}
                onMouseLeave={() => m.nodeId && onHover(null)}
                title={m.end ? '回到终态' : '将游标定位到该时刻'}
                data-moment={m.key}
              >
                <span className={clsx('mono tnum text-2xs', here ? 'text-fg' : 'text-faint')} data-moment-at="">
                  {t0 != null ? `T+${formatOffset(Math.max(0, Math.round(m.at - t0)))}` : NONE}
                </span>
                {m.status === 'warn'
                  ? <TriangleAlert size={11} aria-hidden style={{ color: 'var(--st-waiting)' }} />
                  : <StatusBadge status={m.status} size={11} animate={false} decorative />}
                <span className="min-w-0 truncate">
                  <span className={here ? 'text-fg' : 'text-dim'}>{m.text}</span>
                  {m.sub && <span className="text-faint"> · {m.sub}</span>}
                </span>
              </button>
            </li>
          )
        })}
      </ol>
      {hidden > 0 && <p className="mt-1 px-1 text-2xs text-faint">另有 {hidden} 个时刻未列出，可在下方航迹上拖动游标查看</p>}
    </section>
  )
}

// -------------------------------------------------------------------------
// 最耗时的节点（没选节点时）
// -------------------------------------------------------------------------

function Slowest({ trace, order, labelOf, activeMs, onHover, onPick }: {
  trace: Trace; order: string[]; labelOf: (id?: string | null) => string | undefined; activeMs: number
  onHover: (id: string | null) => void; onPick: (id: string | null) => void
}) {
  const end = lastStampOf(trace)
  const rows = order
    .map((id) => {
      const n = trace.nodes[id]
      const total = (n?.segments ?? []).reduce(
        (sum, s) => (s.kind === 'run' ? sum + Math.max(0, (s.end ?? end) - s.start) : sum), 0)
      return { id, total, count: n?.count ?? 0 }
    })
    .filter((r) => r.total > 0)
    .sort((a, b) => b.total - a.total)
    .slice(0, 3)
  const top = rows[0]?.total ?? 0
  return (
    <section className="runs-trace-side min-w-0 px-4 py-2.5" aria-label="最耗时的节点" data-trace-slowest="">
      <h4 className="mb-1.5 flex items-center gap-2 text-2xs font-medium text-faint">
        本次运行中最耗时的节点
        <span className="font-normal">· 点击泳道可查看该节点在游标所在时刻的状态</span>
      </h4>
      {!rows.length ? <p className="text-xs text-dim">没有节点执行过</p> : (
        <div className="space-y-1">
          {rows.map((r) => (
            <button
              key={r.id}
              type="button"
              className="grid w-full grid-cols-[minmax(0,1fr)_72px_auto] items-center gap-2 rounded px-1 py-0.5 text-left text-xs hover:bg-hover"
              onMouseEnter={() => onHover(r.id)}
              onMouseLeave={() => onHover(null)}
              onClick={() => onPick(r.id)}
              data-slow-node={r.id}
              title={activeMs > 0 ? `占执行时长的 ${Math.round((r.total / activeMs) * 100)}%` : undefined}
            >
              <span className="min-w-0 truncate text-fg">
                {labelOf(r.id) ?? r.id}
                {r.count > 1 && <span className="tnum text-2xs text-faint"> ×{r.count}</span>}
              </span>
              <span className="h-1.5 overflow-hidden rounded-full bg-hover" aria-hidden>
                <span className="block h-full rounded-full" style={{
                  width: `${Math.max(4, (r.total / Math.max(1, top)) * 100)}%`,
                  background: 'color-mix(in srgb, var(--text-faint) 75%, transparent)',
                }} />
              </span>
              <span className="tnum text-right text-2xs text-dim">{formatSpan(r.total)}</span>
            </button>
          ))}
        </div>
      )}
    </section>
  )
}

// -------------------------------------------------------------------------
// 选中的节点在游标那一刻
// -------------------------------------------------------------------------

function NodeCard({ id, trace, proj, state, graph, facts, at, labelOf, href, gone, onClose, onRevealStep }: {
  id: string; trace: Trace; proj: Projection; state: NodeState; graph: GraphSpec
  facts?: NodeFacts
  /** 游标那一刻；null = 实时 / 终态 */
  at: number | null
  labelOf: (id?: string | null) => string | undefined; href: string | null
  /** 现在的图里已经没有这个节点 */
  gone: boolean
  onClose: () => void; onRevealStep: (id: string) => void
}) {
  const n = trace.nodes[id]
  const p = proj.nodes[id]
  const node = graph.nodes.find((x) => x.id === id)
  const label = labelOf(id) ?? id
  // 从没调过模型、工具的节点（输入、分支、人工审批）没有这两项，写「—」而不是 0
  const metered = !!n?.marks?.length
  const tokens = p ? p.tokensIn + p.tokensOut : 0
  const error = state === 'failed' && n?.error ? explainRunError(n.error) : null
  const rows: [string, ReactNode][] = []
  if (n?.model) rows.push(['模型', <span className="mono">{n.model}</span>])
  if (n?.takenHandle && (state === 'done' || state === 'failed')) rows.push(['出口', <span className="mono">{n.takenHandle}</span>])
  if (n?.reason) rows.push(['理由', n.reason])
  if (state === 'skipped' && n?.skippedReason) rows.push(['跳过', n.skippedReason])
  // 卡片说的是游标那一刻所在的那一次执行（和画布卡片同一个划分）：循环上一轮、接着跑之前
  // 的事不挂到这一次干净的执行上；回放到更早的一次时，也不因为后面还有一件就看不到这一次的
  const since = execStart(n, at, facts)
  // 那一刻卡在哪个工具上：「慢在哪」往往就是这一次查询。上一次撒手没收尾的调用不算
  const call = state === 'running' ? openCall(facts, at, since) : undefined
  if (call) {
    const since = (at ?? Date.now() - (trace.skewMs ?? 0)) - call.start
    rows.push(['调用中', (
      <span data-node-field="call">
        <span className="mono">{call.tool}</span>
        {call.agent && <span className="text-faint">（{call.agent}）</span>}
        <span className="tnum text-faint"> · 已 {formatSpan(Math.max(0, since))}{call.limitS ? ` / 上限 ${call.limitS} 秒` : ''}</span>
      </span>
    )])
  }
  // 引擎判为降档、失败的几种「没按要求做完」：这一次执行里、到游标那一刻已经发生的才说。
  // 写成文字分两种：收尾轮还想调工具（交出来的是前面写的，可能不完整）；写成文字被提醒、
  // 重答了（成果是重答的那一次）。一律说「没有真正调用工具」，对重答成功的情况不准
  const markup = lastIn(facts?.markups, at, since)
  const warns = [
    markup && (markup.settle ? '收尾时仍试图调用工具，结果可能不完整' : '模型以文本形式输出了工具调用，已提醒并重新作答'),
    lastIn(facts?.exhausts, at, since) && '协作轮数已用完仍未完成，按降档交付',
    lastIn(facts?.inventions, at, since) && '校验修复时生成了原文中不存在的值，修复已作废',
  ].filter((w): w is string => !!w)

  return (
    <section className="runs-trace-side min-w-0 px-4 py-2" aria-label={`节点「${label}」`} data-trace-node={id}>
      <div className="flex min-w-0 items-center gap-2">
        <span aria-hidden className="h-3.5 w-[3px] shrink-0 rounded-sm"
              style={{ background: node ? `var(--nt-${node.type}, var(--text-faint))` : 'var(--text-faint)' }} />
        <span className="min-w-0 truncate text-xs font-semibold text-fg">{label}</span>
        {node && <span className="shrink-0 text-2xs text-faint">{nodeTypeLabel(node.type)}</span>}
        <StatusPill status={state} className="shrink-0" />
        <span className="flex-1" />
        <button type="button" className="rounded p-0.5 text-faint hover:bg-hover hover:text-dim" onClick={onClose}
                aria-label="收起节点" title="收起">
          <X size={11} aria-hidden />
        </button>
      </div>
      <dl className="mt-1.5 grid grid-cols-4 gap-x-3 gap-y-0.5 text-2xs">
        <Fact label="用时" data="elapsed"
              sub={p && p.count > 1 ? `第 ${p.count} 次` : p?.iteration != null ? `第 ${p.iteration} 轮` : undefined}>
          {p?.elapsedMs != null ? formatSpan(p.elapsedMs) : NONE}
        </Fact>
        <Fact label="用量" data="tokens"
              sub={metered && tokens ? `输入 ${formatNumber(p?.tokensIn)} · 输出 ${formatNumber(p?.tokensOut)}` : undefined}>
          {metered ? formatTokens(tokens, { compact: true }) : NONE}
        </Fact>
        <Fact label="成本" data="cost">{metered && p ? formatCost(p.costUsd) : NONE}</Fact>
        <Fact label="工具" data="tools" sub={p?.toolsRunning ? `${p.toolsRunning} 个进行中` : undefined}>
          {metered && p ? `${p.tools} 次` : NONE}
        </Fact>
      </dl>
      {rows.length > 0 && (
        <dl className="mt-1 grid gap-x-2 text-2xs" style={{ gridTemplateColumns: 'max-content minmax(0,1fr)' }}>
          {rows.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-faint">{k}</dt>
              <dd className="min-w-0 truncate text-dim">{v}</dd>
            </div>
          ))}
        </dl>
      )}
      {warns.map((w) => (
        <p key={w} className="mt-1 flex items-center gap-1 truncate text-2xs" style={{ color: 'var(--st-waiting)' }}
           data-node-warn="">
          <TriangleAlert size={11} aria-hidden className="shrink-0" /> {w}
        </p>
      ))}
      {error && (
        <p className="mt-1 truncate text-2xs" style={{ color: 'var(--st-failed)' }} title={error.raw}>{error.title}</p>
      )}
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        <button type="button" className="btn btn-xs" onClick={() => onRevealStep(id)} data-action="node-stream"
                title="切换到时间线，并高亮该节点的相关步骤">
          <ListTree size={11} aria-hidden /> 在时间线中查看
        </button>
        {href ? (
          <Link className="btn btn-xs" to={href} data-action="node-canvas" title="打开该工作流回放本次运行，并定位到该节点">
            <Crosshair size={11} aria-hidden /> 在画布中查看此步骤
          </Link>
        ) : gone && (
          <span className="self-center text-2xs text-faint" data-node-gone=""
                title="工作流在本次运行后修改过结构，当前工作流中已没有该节点，画布上无法显示；此处为运行当时的状态">
            画布上已没有该节点
          </span>
        )}
      </div>
    </section>
  )
}

function Fact({ label, data, sub, children }: { label: string; data: string; sub?: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-faint">{label}</dt>
      <dd className="mono tnum truncate text-xs text-fg" data-node-field={data}>{children}</dd>
      {sub && <dd className="tnum truncate text-faint">{sub}</dd>}
    </div>
  )
}
