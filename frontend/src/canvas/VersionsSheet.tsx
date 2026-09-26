import { useEffect, useMemo, useState } from 'react'
import { ChevronLeft, History, RotateCcw, ShieldCheck } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { diffGraphs, toFlow, useStudio, type FlowNode, type GraphDiff } from '../store/studio'
import { ErrorState, Skeleton, toast } from '../components/ui'
import { formatDateTime, formatTime } from '../lib/format'
import { WORKFLOW_STATUS_LABEL } from '../lib/terms'
import { hintOf } from './shortcuts'
import type { Edge } from '@xyflow/react'
import type { GraphSpec, Workflow, WorkflowVersion } from '../types'

/**
 * 版本历史。盖在右栏上的一层，和属性面板同一种「临时造访」。
 *
 * 后端每次保存都认真记了一版、还带着说明（「叙述 prompt 禁止元话语数字」），界面上
 * 却无处可看；误删一个节点、助手一次改坏，只能靠「不保存再刷新」挽回。
 *
 * 选一版就拿它和画布比：缩略图上标出恢复之后会多出来的、会被拿掉的、会被改掉的
 * 节点。「恢复这一版」不调后端的 restore——那个接口不会把受管 / 已发布退回草稿，
 * 一张受管工作流恢复成未过闸的旧图，工具栏会照样显示「受管」。这里把旧版放上画布，
 * 作为一次可撤销的改动；保存时走普通的保存，状态按规矩退回草稿，说明写「回滚到 vN」。
 */
export function VersionsSheet({ workflow, onClose }: { workflow: Workflow; onClose: () => void }) {
  const [list, setList] = useState<WorkflowVersion[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [picked, setPicked] = useState<number | null>(null)
  const [detail, setDetail] = useState<{ v: number; graph: GraphSpec } | { v: number; error: unknown } | null>(null)
  const nodes = useStudio((s) => s.nodes)
  const edges = useStudio((s) => s.edges)
  const dirty = useStudio((s) => s.dirty)
  const locked = useStudio((s) => s.copilot.active)
  const restoreVersion = useStudio((s) => s.restoreVersion)
  const undo = useStudio((s) => s.undo)

  const load = () => {
    setError(null)
    setList(null)
    api.workflows.versions(workflow.id).then(setList).catch(setError)
  }
  // 保存一次就多一版：跟着 workflow.version 重新取
  useEffect(load, [workflow.id, workflow.version])

  // Esc 收起，和属性面板一样
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      const el = document.activeElement
      if (el instanceof HTMLElement && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) return
      onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  useEffect(() => {
    if (picked == null) return
    let alive = true
    setDetail(null)
    api.workflows.version(workflow.id, picked)
      .then((v) => { if (alive) setDetail({ v: picked, graph: v.graph ?? { nodes: [], edges: [] } }) })
      .catch((e) => { if (alive) setDetail({ v: picked, error: e }) })
    return () => { alive = false }
  }, [workflow.id, picked])

  const graph = detail && 'graph' in detail ? detail.graph : null
  const target = useMemo(() => (graph ? toFlow(graph) : null), [graph])
  const diff = useMemo(() => (target ? diffGraphs({ nodes, edges }, target) : null), [target, nodes, edges])

  const restore = () => {
    if (!graph || picked == null) return
    restoreVersion(picked, graph)
    toast(`已把 v${picked} 放上画布，保存后才生效`, 'ok', {
      key: 'studio:restore', duration: 8000, action: { label: '撤销', onClick: () => undo() },
    })
    onClose()
  }

  return (
    <div className="sheet-in absolute inset-0 z-30 flex flex-col bg-panel"
         style={{ boxShadow: 'var(--elev-3)' }} role="dialog" aria-label="版本历史">
      <div className="flex items-center gap-1.5 border-b px-2 py-2">
        <button
          type="button"
          className="flex items-center gap-0.5 rounded-md px-1 py-1 text-faint transition-colors hover:bg-hover hover:text-fg"
          title="返回（Esc）" onClick={onClose}
        >
          <ChevronLeft size={14} />
          <span className="text-2xs">助手</span>
        </button>
        <span className="mx-0.5 h-3.5 w-px shrink-0" style={{ background: 'var(--border)' }} />
        <History size={13} className="shrink-0 text-faint" />
        <span className="flex-1 truncate text-xs font-semibold" title={hintOf('版本历史', 'history')}>版本历史</span>
        <span className="tnum text-2xs text-faint">{list ? `${list.length} 版` : ''}</span>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {error ? <ErrorState error={error} onRetry={load} compact className="m-3" />
          : !list ? <div className="p-3"><Skeleton rows={4} height={30} /></div>
          : (
            <ol className="p-1.5" aria-label="版本">
              {list.map((v) => {
                const current = v.version === workflow.version
                const published = v.version === workflow.published_version
                const open = picked === v.version
                return (
                  <li key={v.id} className="mb-0.5">
                    <button
                      type="button"
                      aria-expanded={open}
                      onClick={() => setPicked(open ? null : v.version)}
                      className={clsx('flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left transition-colors',
                        open ? 'bg-hover' : 'hover:bg-hover')}
                    >
                      <span className="mono tnum mt-px w-7 shrink-0 text-xs font-semibold">v{v.version}</span>
                      <span className="min-w-0 flex-1">
                        <span className={clsx('block truncate text-xs', v.note ? 'text-fg' : 'text-faint')}>
                          {v.note || '（没写说明）'}
                        </span>
                        <span className="mt-0.5 flex flex-wrap items-center gap-1 text-2xs text-faint">
                          <span title={formatDateTime(v.created_at)}>{formatTime(v.created_at)}</span>
                          {current && <span className="chip">{dirty ? '画布基于这一版' : '画布上就是这一版'}</span>}
                          {published && (
                            <span className="chip" style={{ color: 'var(--ok)', borderColor: 'var(--ok)' }}>
                              <ShieldCheck size={9} />
                              {WORKFLOW_STATUS_LABEL[workflow.status === 'governed' ? 'governed' : 'published']}
                            </span>
                          )}
                        </span>
                      </span>
                    </button>
                    {open && (
                      <div className="fade-up mx-2 mb-2 mt-1 rounded-md border bg-bg p-2">
                        {!detail ? <Skeleton rows={3} height={12} />
                          : 'error' in detail ? <ErrorState error={detail.error} compact />
                          : target && diff && (
                            <VersionPreview version={v.version} target={target} diff={diff}
                                            locked={locked} onRestore={restore} />
                          )}
                      </div>
                    )}
                  </li>
                )
              })}
            </ol>
          )}
      </div>
      <div className="border-t px-3 py-2 text-2xs leading-relaxed text-faint">
        恢复只是把旧版放上画布，可以撤销；保存之后才会成为新的一版。
        {workflow.published_version != null && '已发布的版本不受影响，正式运行照旧跑它。'}
      </div>
    </div>
  )
}

function VersionPreview({ version, target, diff, locked, onRestore }: {
  version: number; target: { nodes: FlowNode[]; edges: Edge[] }; diff: GraphDiff
  locked: boolean; onRestore: () => void
}) {
  const current = useStudio((s) => s.nodes)
  const same = diff.total === 0
  const label = (id: string, pool: FlowNode[]) => pool.find((n) => n.id === id)?.data.label || id
  const lines = [
    ...diff.added.map((id) => ({ kind: 'add' as const, text: label(id, target.nodes) })),
    ...diff.removed.map((id) => ({ kind: 'del' as const, text: label(id, current) })),
    ...diff.changed.map((id) => ({ kind: 'mod' as const, text: label(id, target.nodes) })),
  ]
  return (
    <div>
      <MiniGraph target={target} current={current} diff={diff} />
      {same ? (
        <div className="mt-2 text-2xs text-faint">和画布上的一样（位置可能不同）</div>
      ) : (
        <>
          <div className="mt-2 text-2xs text-dim">恢复后，和画布相比：</div>
          <ul className="mt-1 space-y-0.5 text-2xs">
            {lines.slice(0, 8).map((l, i) => (
              <li key={i} className="flex items-center gap-1.5">
                <span className="mono w-3 shrink-0 text-center" style={{ color: DIFF_COLOR[l.kind] }}>
                  {l.kind === 'add' ? '+' : l.kind === 'del' ? '−' : '~'}
                </span>
                <span className="min-w-0 flex-1 truncate">{l.text}</span>
                <span className="shrink-0 text-faint">{l.kind === 'add' ? '回来' : l.kind === 'del' ? '拿掉' : '配置不同'}</span>
              </li>
            ))}
            {lines.length > 8 && <li className="text-faint">还有 {lines.length - 8} 处…</li>}
            {(diff.edgesAdded > 0 || diff.edgesRemoved > 0) && (
              <li className="text-faint">
                连线 {diff.edgesAdded ? `+${diff.edgesAdded}` : ''}{diff.edgesAdded && diff.edgesRemoved ? ' ' : ''}{diff.edgesRemoved ? `−${diff.edgesRemoved}` : ''}
              </li>
            )}
          </ul>
        </>
      )}
      <button type="button" className="btn btn-sm mt-2 w-full justify-center" disabled={same || locked}
              title={locked ? '助手正在改这张工作流，等它做完' : undefined} onClick={onRestore}>
        <RotateCcw size={11} /> 恢复 v{version} 到画布
      </button>
    </div>
  )
}

const DIFF_COLOR = { add: 'var(--ok)', del: 'var(--err)', mod: 'var(--warn)' } as const

/**
 * 缩略图：这一版的节点按真实坐标缩进一个小框，类型色填充；恢复后会被拿掉的
 * （只在画布上有）画成红色虚框叠在同一个坐标系里，改过配置的描琥珀边
 */
function MiniGraph({ target, current, diff }: {
  target: { nodes: FlowNode[]; edges: Edge[] }; current: FlowNode[]; diff: GraphDiff
}) {
  const W = 280
  const H = 120
  const ghosts = current.filter((n) => diff.removed.includes(n.id))
  const all = [...target.nodes, ...ghosts]
  if (!all.length) return <div className="py-4 text-center text-2xs text-faint">这一版是空的</div>
  const nw = 238
  const nh = 80
  const x0 = Math.min(...all.map((n) => n.position.x))
  const y0 = Math.min(...all.map((n) => n.position.y))
  const x1 = Math.max(...all.map((n) => n.position.x + nw))
  const y1 = Math.max(...all.map((n) => n.position.y + nh))
  const k = Math.min((W - 8) / Math.max(1, x1 - x0), (H - 8) / Math.max(1, y1 - y0))
  const ox = (W - (x1 - x0) * k) / 2
  const oy = (H - (y1 - y0) * k) / 2
  const pos = (n: FlowNode) => ({ x: ox + (n.position.x - x0) * k, y: oy + (n.position.y - y0) * k })
  const byId = new Map(target.nodes.map((n) => [n.id, n]))
  const added = new Set(diff.added)
  const changed = new Set(diff.changed)
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="block w-full" role="img"
         aria-label={`缩略图：${target.nodes.length} 个节点`} style={{ background: 'var(--bg-panel)', borderRadius: 4 }}>
      {target.edges.map((e, i) => {
        const a = byId.get(e.source)
        const b = byId.get(e.target)
        if (!a || !b) return null
        const p = pos(a)
        const q = pos(b)
        const sx = p.x + nw * k
        const sy = p.y + (nh * k) / 2
        const tx = q.x
        const ty = q.y + (nh * k) / 2
        const mx = (sx + tx) / 2
        return <path key={i} d={`M${sx},${sy} H${mx} V${ty} H${tx}`} fill="none" strokeWidth={0.8}
                     style={{ stroke: 'var(--border-strong)' }} />
      })}
      {target.nodes.map((n) => {
        const p = pos(n)
        const tone = added.has(n.id) ? DIFF_COLOR.add : changed.has(n.id) ? DIFF_COLOR.mod : null
        return (
          // 颜色走 style：SVG 的呈现属性不认 var()
          <rect key={n.id} x={p.x} y={p.y} width={nw * k} height={nh * k} rx={2}
                className={`nt-${n.data.nodeType}`} strokeWidth={tone ? 1.4 : 0.6}
                style={{ fill: 'color-mix(in srgb, var(--nt) 30%, var(--bg))',
                         stroke: tone ?? 'color-mix(in srgb, var(--nt) 60%, transparent)' }}>
            <title>{n.data.label || n.id}</title>
          </rect>
        )
      })}
      {ghosts.map((n) => {
        const p = pos(n)
        return (
          <rect key={`g-${n.id}`} x={p.x} y={p.y} width={nw * k} height={nh * k} rx={2}
                fill="none" strokeWidth={1.2} strokeDasharray="3 2" style={{ stroke: DIFF_COLOR.del }}>
            <title>{`恢复后会拿掉：${n.data.label || n.id}`}</title>
          </rect>
        )
      })}
    </svg>
  )
}
