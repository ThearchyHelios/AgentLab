import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { FileBox, History } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import { EmptyState, ErrorState, Skeleton, StatusBadge } from '../../components/ui'
import { formatBytes, formatClock, formatDateTime, formatNumber, formatTime, parseServerTime } from '../../lib/format'
import { nodeTypeLabel } from '../../lib/terms'
import { ArtifactViewer } from '../../run/AssistantStream'
import { topology } from '../../run/derive'
import type { Trace } from '../../run/trace'
import type { GraphSpec } from '../../types'
import { artifactDescription, artifactKindLabel, isEvidence, type RunArtifact } from './model'

// -------------------------------------------------------------------------
// 数据
// -------------------------------------------------------------------------

export interface ArtifactList {
  /** null = 还没取回来过 */
  items: RunArtifact[] | null
  state: 'loading' | 'ok' | 'error'
  error: unknown
  reload: () => void
}

/** 跑着的运行每完成一个节点都会多几件：最多两秒取一次，不跟着每条事件敲 */
const REFRESH_EVERY_MS = 2000

/**
 * 这次运行的工件清单。页签上要写件数，所以在详情一打开就取；refreshKey 变了
 * （又有节点跑完、运行收尾）再取，节流到两秒一次。刷新失败保留原来的清单。
 */
export function useRunArtifacts(runId: string, refreshKey: string): ArtifactList {
  const [items, setItems] = useState<RunArtifact[] | null>(null)
  const [state, setState] = useState<ArtifactList['state']>('loading')
  const [error, setError] = useState<unknown>(null)
  const epoch = useRef(0)
  const lastAt = useRef(0)

  const load = useCallback(async () => {
    const my = ++epoch.current
    lastAt.current = Date.now()
    try {
      const list = await api.runs.artifacts(runId)
      if (my !== epoch.current) return
      setItems(Array.isArray(list) ? (list as RunArtifact[]) : [])
      setError(null)
      setState('ok')
    } catch (e) {
      if (my !== epoch.current) return
      setError(e)
      setState((s) => (s === 'ok' ? s : 'error'))
    }
  }, [runId])

  useEffect(() => {
    const t = window.setTimeout(() => void load(), Math.max(0, lastAt.current + REFRESH_EVERY_MS - Date.now()))
    return () => window.clearTimeout(t)
  }, [load, refreshKey])

  return { items, state, error, reload: () => void load() }
}

// -------------------------------------------------------------------------
// 页签
// -------------------------------------------------------------------------

type KindFilter = 'all' | 'evidence' | 'output'
const FILTER_LABEL: Record<KindFilter, string> = { all: '全部', evidence: '证据快照', output: '节点产出' }
const FILTER_HINT: Record<KindFilter, string> = {
  all: '',
  evidence: '查询、工具调用、知识检索的原始结果：结论里的数字从这里来',
  output: '每个节点每执行一次的产出：循环里每一轮都有一件',
}
/** 一次循环能有上千件：先列这么多，其余点了再展开 */
const FIRST_ROWS = 300

/**
 * 工件：这次运行按内容哈希存下来的每一件东西——查询和工具调用的原始结果、每个
 * 节点每一次的产出。审阅出具物时从这里下钻到证据；打开时按哈希复验。
 *
 * 按节点分组、按执行顺序排，和航迹的泳道同一个顺序。能对上时刻的，一键跳到
 * 航迹里它产出的那一刻。
 */
export function ArtifactsPane({ list, graph, trace, labelOf, onMoment }: {
  list: ArtifactList
  graph: GraphSpec | null
  trace: Trace
  labelOf: (id?: string | null) => string | undefined
  /** 在航迹里看这件工件产出的那一刻（毫秒时间戳，和航迹同一个钟） */
  onMoment?: (at: number, nodeId: string | null) => void
}) {
  const [filter, setFilter] = useState<KindFilter>('all')
  const [all, setAll] = useState(false)
  const [open, setOpen] = useState<{ id: string; title: string } | null>(null)
  const items = list.items

  const counts = useMemo(() => {
    const c: Record<KindFilter, number> = { all: 0, evidence: 0, output: 0 }
    for (const a of items ?? []) {
      c.all += 1
      c[isEvidence(a.kind) ? 'evidence' : 'output'] += 1
    }
    return c
  }, [items])
  const bytes = useMemo(() => (items ?? []).reduce((n, a) => n + (a.size ?? 0), 0), [items])

  const groups = useMemo(() => {
    const shown = (items ?? []).filter((a) => filter === 'all' || (filter === 'evidence') === isEvidence(a.kind))
    const order = graph ? topology(graph).order : []
    const rank = new Map(order.map((id, i) => [id, i]))
    const byNode = new Map<string, RunArtifact[]>()
    for (const a of shown) {
      const key = a.node_id ?? ''
      const g = byNode.get(key)
      if (g) g.push(a)
      else byNode.set(key, [a])
    }
    // 图里有的按拓扑序；图里没有的（快照取不到、子工作流里的）按第一件出现的先后排在后面
    return [...byNode].sort(([a], [b]) => (rank.get(a) ?? Infinity) - (rank.get(b) ?? Infinity))
  }, [items, filter, graph])

  if (!items) {
    if (list.state === 'error') return <ErrorState error={list.error} onRetry={list.reload} className="h-full" />
    return <div className="p-4" aria-busy="true"><Skeleton rows={6} height={22} /></div>
  }
  if (!items.length) {
    return (
      <EmptyState
        icon={<FileBox size={22} />}
        title="本次运行没有工件"
        body="节点每次执行的产出、查询和工具调用的原始结果，都会按内容校验值存为工件；尚无节点执行完成的运行没有工件。"
        className="h-full"
      />
    )
  }

  const t0 = trace.timed ? trace.startedAt : undefined
  let budget = all ? Infinity : FIRST_ROWS
  const hidden = Math.max(0, groups.reduce((n, [, g]) => n + g.length, 0) - FIRST_ROWS)

  return (
    <div className="flex h-full min-h-0 flex-col" data-run-artifacts="">
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-1.5 text-2xs text-faint">
        <div className="flex gap-0.5" role="group" aria-label="按类型筛选工件">
          {(Object.keys(FILTER_LABEL) as KindFilter[]).map((k) => (
            <button
              key={k}
              type="button"
              aria-pressed={filter === k}
              disabled={k !== 'all' && !counts[k]}
              title={FILTER_HINT[k] || undefined}
              onClick={() => setFilter(k)}
              data-artifact-kind={k}
              className={clsx(
                'inline-flex h-6 items-center gap-1 rounded-full border px-2 transition-colors disabled:opacity-45',
                filter === k ? 'border-[var(--border-strong)] bg-hover text-fg' : 'border-transparent hover:bg-hover hover:text-dim',
              )}
            >
              {FILTER_LABEL[k]} <span className="tnum">{counts[k]}</span>
            </button>
          ))}
        </div>
        <span className="flex-1" />
        <span className="tnum" title="按内容校验值存放：打开时重新计算，与运行时记录的一致才会显示">
          合计 {formatBytes(bytes)} · 打开时重新校验
        </span>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {groups.map(([nodeId, rows]) => {
          if (budget <= 0) return null
          const take = rows.slice(0, budget)
          budget -= take.length
          return (
            <Group key={nodeId || '—'} nodeId={nodeId} rows={take} total={rows.length} graph={graph} trace={trace}
                   labelOf={labelOf} t0={t0} onOpen={setOpen} onMoment={onMoment} />
          )
        })}
        {!all && hidden > 0 && (
          <div className="flex items-center justify-center gap-2 px-4 py-3 text-2xs text-faint">
            <span className="tnum">还有 {formatNumber(hidden)} 件未列出</span>
            <button type="button" className="btn btn-sm" onClick={() => setAll(true)}>全部列出</button>
          </div>
        )}
      </div>

      <ArtifactViewer id={open?.id ?? null} title={open?.title} onClose={() => setOpen(null)} />
    </div>
  )
}

function Group({ nodeId, rows, total, graph, trace, labelOf, t0, onOpen, onMoment }: {
  nodeId: string
  rows: RunArtifact[]
  total: number
  graph: GraphSpec | null
  trace: Trace
  labelOf: (id?: string | null) => string | undefined
  t0?: number
  onOpen: (v: { id: string; title: string }) => void
  onMoment?: (at: number, nodeId: string | null) => void
}) {
  const node = graph?.nodes.find((n) => n.id === nodeId)
  const label = nodeId ? labelOf(nodeId) ?? nodeId : '不属于某个节点'
  const state = nodeId ? trace.nodes[nodeId]?.state : undefined
  // 同一个节点同一类的第几件：循环里几十件「节点产出」要能分得开；只有一件的不编号
  const ordinal = new Map<string, number>()
  const perKind = new Map<string, number>()
  for (const a of rows) perKind.set(a.kind, (perKind.get(a.kind) ?? 0) + 1)
  return (
    <section aria-label={label} data-artifact-group={nodeId}>
      <h3 className="sticky top-0 z-10 flex items-center gap-2 border-b bg-panel px-4 py-1 text-2xs">
        <span aria-hidden className="h-3 w-[3px] shrink-0 rounded-sm"
              style={{ background: node ? `var(--nt-${node.type}, var(--text-faint))` : 'var(--text-faint)' }} />
        <span className="min-w-0 truncate font-medium text-fg">{label}</span>
        {node && <span className="shrink-0 text-faint">{nodeTypeLabel(node.type)}</span>}
        {nodeId && label !== nodeId && <span className="mono shrink-0 text-faint">{nodeId}</span>}
        {state && <StatusBadge status={state} size={11} animate={false} />}
        <span className="flex-1" />
        <span className="tnum shrink-0 text-faint">{total} 件</span>
      </h3>
      {rows.map((a) => {
        const n = (ordinal.get(a.kind) ?? 0) + 1
        ordinal.set(a.kind, n)
        const kind = artifactKindLabel(a.kind)
        const attempt = Number(a.meta?.attempt)
        const created = parseServerTime(a.created_at ?? null)?.getTime()
        const at = t0 != null && created != null && created >= t0 ? created : null
        const numbered = (perKind.get(a.kind) ?? 0) > 1
        const title = `${label} · ${kind}${numbered ? ` #${n}` : ''}`
        return (
          <div key={a.id} className="flex items-center gap-2 border-b px-4 py-1 last:border-0 hover:bg-hover"
               data-artifact-id={a.id}>
            <button
              type="button"
              className="flex min-w-0 flex-1 items-center gap-2 rounded py-0.5 text-left outline-none focus-visible:ring-1 focus-visible:ring-[var(--accent)]"
              onClick={() => onOpen({ id: a.id, title })}
              title={`打开${kind}（读取时按内容校验值重新校验）`}
              data-action="artifact-open"
            >
              <span className={clsx('chip shrink-0', isEvidence(a.kind) && 'text-fg')}
                    style={isEvidence(a.kind) ? { borderColor: 'var(--border-strong)' } : undefined}>
                {kind}
              </span>
              <span className="min-w-0 truncate text-xs text-dim">
                {artifactDescription(a.kind, a.meta)}
                {numbered && <span className="tnum text-faint"> #{n}</span>}
                {attempt > 1 && <span className="text-faint"> · 第 {attempt} 次尝试</span>}
              </span>
              <span className="flex-1" />
              <span className="mono shrink-0 text-2xs text-faint" title={`内容校验值 ${a.id}`}>{a.id.slice(0, 8)}</span>
              <span className="tnum w-16 shrink-0 text-right text-2xs text-faint">{formatBytes(a.size)}</span>
            </button>
            {at != null && onMoment ? (
              <button type="button" className="btn btn-xs btn-ghost tnum w-[86px] shrink-0 justify-end"
                      onClick={() => onMoment(at, a.node_id)} data-action="artifact-moment"
                      title={`在航迹中查看其产出时刻（${formatDateTime(a.created_at ?? null)}）`}>
                <History size={11} aria-hidden /> T+{formatClock(Math.round(at - t0!))}
              </button>
            ) : (
              <span className="tnum w-[86px] shrink-0 text-right text-2xs text-faint" title={formatDateTime(a.created_at ?? null)}>
                {formatTime(a.created_at ?? null)}
              </span>
            )}
          </div>
        )
      })}
    </section>
  )
}
