import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { GitBranch } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { humanizeError } from '../lib/errors'
import { CATALOG_IMPACT_TEXT as T, WORKFLOW_STATUS_LABEL } from '../lib/terms'
import type { CatalogImpact, CatalogImpactNode, CatalogImpactTemplate } from '../types'
import { Skeleton } from './ui'

// ===========================================================================
// 影响面：哪些已发布、受管的模板引用这张表（GET /catalog/{table}/impact）。数据目录的表详情里一栏，助手的
// 「建议更新数据目录」卡片保存之后也列一遍——改了目录，人要知道哪些正式出具的数可能跟着变。
//
// 「直接引用」和「可能涉及」分开写：前者 SQL 里写着这张表，后者是 Agent 运行时自己写 SQL，静态看不出。
// 合并查询写明经由哪个输入。模板名点进去是画布（定位到那个节点）。
// ===========================================================================

const SHOWN = 5

export function CatalogImpactList({ sourceId, table, refreshKey, compact = false, onLoad }: {
  sourceId: string
  table: string
  /** 变了就重新统计（表详情传目录版本：保存之后刷新） */
  refreshKey?: number | string
  /** 卡片里的紧凑版：只列前几个模板，节点收成一行 */
  compact?: boolean
  /** 统计好了：数据目录页据此在保存之后说「N 个已发布模板引用这张表」 */
  onLoad?: (d: CatalogImpact) => void
}) {
  const [data, setData] = useState<CatalogImpact | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [all, setAll] = useState(false)
  const seq = useRef(0)
  const onLoadRef = useRef(onLoad)
  onLoadRef.current = onLoad

  const load = useCallback(() => {
    const mine = ++seq.current
    setError(null)
    api.dataCatalog.impact(sourceId, table)
      .then((d) => { if (mine === seq.current) { setData(d); onLoadRef.current?.(d) } })
      .catch((e) => { if (mine === seq.current) setError(e) })
  }, [sourceId, table])

  useEffect(() => {
    setData(null)
    setAll(false)
    load()
  }, [load, refreshKey])

  if (error && !data) {
    return (
      <p className="flex flex-wrap items-center gap-2 text-2xs text-[var(--err)]" data-catalog-impact="error">
        {T.error}：{humanizeError(error).title}
        <button type="button" className="rounded px-1 text-dim hover:bg-hover hover:text-fg" onClick={load}>{T.retry}</button>
      </p>
    )
  }
  if (!data) return <div data-catalog-impact="loading"><Skeleton rows={compact ? 1 : 2} height={compact ? 12 : 20} gap={6} /></div>

  const list = data.templates
  if (!list.length) {
    return (
      <p className={clsx('text-faint', compact ? 'text-2xs' : 'rounded-lg border px-3 py-4 text-xs')} data-catalog-impact="0">
        {compact ? T.afterSaveNone : T.none}
      </p>
    )
  }
  const shown = all || !compact ? list : list.slice(0, SHOWN)
  return (
    <div data-catalog-impact={list.length}>
      {compact && <p className="mb-1 text-2xs text-dim">{T.afterSave(list.length)}</p>}
      <ul className={clsx(compact ? 'space-y-0.5' : 'divide-y divide-[var(--hairline)] rounded-lg border bg-panel')}>
        {shown.map((t) => <TemplateRow key={t.workflow_id} t={t} compact={compact} />)}
      </ul>
      {compact && list.length > shown.length && (
        <button type="button" className="mt-0.5 rounded px-1 text-2xs text-dim hover:bg-hover hover:text-fg" onClick={() => setAll(true)}>
          {T.more(list.length - shown.length)}
        </button>
      )}
    </div>
  )
}

function ImpactBadge({ impact }: { impact: 'direct' | 'possible' }) {
  return (
    <span className={clsx('chip shrink-0', impact === 'direct' ? 'text-fg' : 'text-faint')} title={T.impactHint[impact]}
          style={impact === 'direct' ? { borderColor: 'var(--st-waiting)', color: 'var(--st-waiting)' } : undefined}
          data-impact={impact}>
      {T.impact[impact]}
    </span>
  )
}

function nodeText(n: CatalogImpactNode): string {
  const who = n.member ? `「${n.label}」${T.member(n.member)}` : `「${n.label}」`
  const via = n.via?.length ? `（${T.via(n.via.map((v) => `「${v.label}」`).join('、'))}）` : ''
  return `${who}${via}`
}

function TemplateRow({ t, compact }: { t: CatalogImpactTemplate; compact: boolean }) {
  const first = t.nodes.find((n) => n.impact === t.impact) ?? t.nodes[0]
  const href = `/studio/${encodeURIComponent(t.workflow_id)}${first ? `?focus=${encodeURIComponent(first.node_id)}` : ''}`
  return (
    <li className={clsx('min-w-0', compact ? 'text-2xs' : 'px-3 py-2 text-xs')} data-impact-template={t.workflow_id} data-impact-level={t.impact}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
        <GitBranch size={compact ? 10 : 11} className="shrink-0 text-faint" aria-hidden />
        <Link to={href} className="min-w-0 truncate font-medium hover:underline">{t.name}</Link>
        <span className="mono shrink-0 text-2xs text-faint">{T.version(t.version)}</span>
        <span className="shrink-0 text-2xs text-faint">{WORKFLOW_STATUS_LABEL[t.level] ?? t.level}</span>
        <ImpactBadge impact={t.impact} />
      </div>
      {!compact && (
        <ul className="mt-1 space-y-0.5 pl-[19px] text-2xs text-dim">
          {t.nodes.map((n) => (
            <li key={`${n.node_id}:${n.member ?? ''}`} className="flex min-w-0 items-baseline gap-1.5" data-impact-node={n.node_id}>
              <span className="min-w-0 break-words">{nodeText(n)}</span>
              <span className={clsx('shrink-0', n.impact === 'direct' ? 'text-dim' : 'text-faint')}>{T.impact[n.impact]}</span>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}
