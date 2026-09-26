import { useEffect, useRef } from 'react'
import { AlertTriangle, CheckCircle2, CornerDownRight, RotateCw, Trash2, Unlink, Workflow, XCircle } from 'lucide-react'
import clsx from 'clsx'
import { NODE_DEFS } from './nodeDefs'
import type { FieldRef, Problem } from './issues'
import { formatShortcut } from '../lib/keys'
import { useCatalog } from '../store/catalog'
import { useStudio } from '../store/studio'
import { Spinner } from '../components/ui'

/**
 * 问题面板：图级、节点、边上的 error 和 warning，一条一行，点一下定位。
 *
 * 以前只有工具栏上一个点不动的「2 个错误」：图级的（找不到入口）只体现在计数里，
 * 边上的根本没地方看，有 error 时 warning 的计数还被藏起来——而 warning 恰恰是
 * 「取到空值」这类运行期静默出错的那一类。现在按节点分组（图级在最前），
 * 点一行选中节点、镜头对过去、检查器翻到出问题的那个字段。F8 / ⇧F8 在问题之间跳。
 */
export function ProblemsPane({ problems, activeId, onLocate, onDeleteEdge }: {
  problems: Problem[]
  activeId: string | null
  onLocate: (p: Problem) => void
  onDeleteEdge: (edgeId: string) => void
}) {
  const nodes = useStudio((s) => s.nodes)
  const analysis = useStudio((s) => s.analysis)
  const analysisError = useStudio((s) => s.analysisError)
  const analyzeNow = useStudio((s) => s.analyzeNow)
  const offline = useCatalog((s) => s.backend === 'down')
  const list = useRef<HTMLDivElement>(null)

  // F8 跳到的那一条要在视野里
  useEffect(() => {
    if (!activeId) return
    list.current?.querySelector(`[data-problem="${CSS.escape(activeId)}"]`)
      ?.scrollIntoView({ block: 'nearest' })
  }, [activeId])

  // 只有后端整个断了，恢复连接时才会自动重跑（useOnReconnect）；校验接口自己报错
  // 不会有人替你再试，这时候承诺「会自动重新校验」就是在让人干等
  const retryHint = offline ? '连上后端之后会自动重新校验' : '点「重试」再校验一次'
  if (analysis === 'failed' && !problems.length) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center text-2xs">
        <span style={{ color: 'var(--warn)' }}>分析失败{analysisError ? `：${analysisError}` : ''}</span>
        <span className="text-faint">问题清单暂时拿不到，画布照常能编辑。{retryHint}</span>
        <button type="button" className="btn btn-sm" onClick={() => void analyzeNow()}>
          <RotateCw size={11} /> 重试
        </button>
      </div>
    )
  }
  if (!problems.length) {
    return (
      <div className="flex flex-1 items-center justify-center gap-1.5 text-2xs text-faint">
        {analysis === 'pending'
          ? <><Spinner size={11} /> 正在校验…</>
          : <><CheckCircle2 size={12} style={{ color: 'var(--ok)' }} /> 没有发现问题</>}
      </div>
    )
  }

  const byNode = new Map(nodes.map((n) => [n.id, n]))
  const groups: { key: string; title: React.ReactNode; items: Problem[] }[] = []
  for (const p of problems) {
    const key = p.scope === 'node' ? `n:${p.nodeId}` : p.scope
    let g = groups.find((x) => x.key === key)
    if (!g) {
      const node = p.nodeId ? byNode.get(p.nodeId) : undefined
      const def = node ? NODE_DEFS[node.data.nodeType] : undefined
      g = {
        key,
        title: p.scope === 'graph'
          ? <><Workflow size={11} className="shrink-0 text-faint" /> 整张工作流</>
          : p.scope === 'edge'
            ? <><Unlink size={11} className="shrink-0 text-faint" /> 连线</>
            : <>
                {def
                  ? <def.icon size={11} className={`nt-${node!.data.nodeType} shrink-0`} style={{ color: 'var(--nt)' }} />
                  : <span className="h-2.5 w-2.5 shrink-0 rounded-sm border" />}
                <span className="min-w-0 truncate text-fg">{node?.data.label || p.nodeId}</span>
                {def && <span className="shrink-0 text-faint">· {def.label}</span>}
              </>,
        items: [],
      }
      groups.push(g)
    }
    g.items.push(p)
  }

  return (
    <>
      {/* 这次没分析成：清单还是上一次校验的，照样列出来（多半仍然有效），但说清楚它可能过时 */}
      {analysis === 'failed' && (
        <div className="mx-2 mt-1.5 flex shrink-0 items-center gap-2 rounded-md border px-2.5 py-1.5 text-2xs"
             style={{ borderColor: 'var(--warn)' }}>
          <AlertTriangle size={11} className="shrink-0" style={{ color: 'var(--warn)' }} aria-hidden />
          <span className="min-w-0 flex-1">
            <span style={{ color: 'var(--warn)' }}>分析失败{analysisError ? `：${analysisError}` : ''}</span>
            <span className="text-faint"> · 下面是上一次校验的结果，可能已经过时。{retryHint}</span>
          </span>
          <button type="button" className="btn btn-xs shrink-0" onClick={() => void analyzeNow()}>
            <RotateCw size={10} /> 重试
          </button>
        </div>
      )}
      <div ref={list} className="min-h-0 flex-1 overflow-y-auto py-1" role="list" aria-label="问题">
        {groups.map((g) => (
          <div key={g.key} className="mb-1">
            <div className="sticky top-0 z-[1] flex items-center gap-1.5 bg-panel px-3 py-1 text-2xs font-medium">
              {g.title}
              <span className="tnum ml-auto shrink-0 text-faint">{g.items.length}</span>
            </div>
            {g.items.map((p) => (
              <ProblemRow key={p.id} problem={p} active={p.id === activeId}
                          onLocate={() => onLocate(p)} onDeleteEdge={onDeleteEdge} />
            ))}
          </div>
        ))}
        <div className="px-3 pb-1 pt-0.5 text-2xs text-faint">
          {formatShortcut('F8')} 下一个 · {formatShortcut('Shift+F8')} 上一个
        </div>
      </div>
    </>
  )
}

function ProblemRow({ problem: p, active, onLocate, onDeleteEdge }: {
  problem: Problem; active: boolean; onLocate: () => void; onDeleteEdge: (id: string) => void
}) {
  const node = useStudio((s) => (p.nodeId ? s.nodes.find((n) => n.id === p.nodeId) : undefined))
  const err = p.level === 'error'
  const Icon = err ? XCircle : AlertTriangle
  const where = fieldLabel(p.field, node?.data.nodeType)
  const locatable = p.scope === 'node' && !!node
  return (
    <div role="listitem" data-problem={p.id}
         className={clsx('group flex items-start gap-2 pl-6 pr-2', active && 'bg-hover')}>
      <button
        type="button"
        disabled={!locatable}
        onClick={onLocate}
        title={locatable ? '选中这个节点并定位到出问题的地方' : undefined}
        className={clsx('flex min-w-0 flex-1 items-start gap-2 rounded py-1 text-left text-2xs leading-snug',
          locatable ? 'cursor-pointer hover:text-fg' : 'cursor-default')}
      >
        <Icon size={11} className="mt-px shrink-0" style={{ color: err ? 'var(--err)' : 'var(--warn)' }}
              aria-label={err ? '错误' : '提示'} />
        <span className={clsx('min-w-0 flex-1', err ? 'text-fg' : 'text-dim')}>{p.message}</span>
        {where && (
          <span className="flex shrink-0 items-center gap-0.5 text-faint">
            <CornerDownRight size={9} aria-hidden />{where}
          </span>
        )}
      </button>
      {/* 悬空边（指向不存在的节点）：React Flow 不画它，画布上看不见、点不到，只能在这儿删 */}
      {p.scope === 'edge' && p.edgeId && (
        <button type="button" className="btn btn-xs my-0.5 shrink-0" onClick={() => onDeleteEdge(p.edgeId!)}>
          <Trash2 size={10} /> 删除这条悬空边
        </button>
      )}
    </div>
  )
}

function fieldLabel(field: FieldRef | null | undefined, type?: string): string {
  if (!field) return ''
  if (field.key === 'label') return '节点名称'
  const def = type ? NODE_DEFS[type as keyof typeof NODE_DEFS] : undefined
  const base = def?.fields.find((f) => f.key === field.key)?.label ?? field.key
  if (field.index == null) return base
  const item = ({ cases: '分支', fields: '字段', metrics: '指标', agents: '成员' } as Record<string, string>)[field.key]
  const sub = field.sub === 'condition' ? '的条件' : field.sub === 'key' ? '的标识' : ''
  return item ? `第 ${field.index + 1} 个${item}${sub}` : `${base} · 第 ${field.index + 1} 项`
}
