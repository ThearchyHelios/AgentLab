import { useCallback, useEffect, useState } from 'react'
import { ScrollText } from 'lucide-react'
import { api } from '../../api/client'
import { EmptyState, ErrorState, SectionBar, Skeleton } from '../../components/ui'
import { formatDateTime } from '../../lib/format'
import { IDENTITY_TEXT as T } from '../../lib/terms'
import { useAtLeast } from '../../store/auth'
import type { AuditRow } from '../../typesApp'

/*
 * 设置 → 审计（A7 第 5 条）：管理类操作的留痕，只读、只给管理员。谁做的照记录写，开了登录的写「已登录」，
 * 关登录时的是自报署名——审计不替人核实，照实标出来（P1）
 */

const KINDS: Record<string, string> = {
  auth: '登录方式', user: '用户', group: '组', app: '应用', grant: '授权', endpoint: '端点', capability: '能力',
  workflow_version: '工作流版本', channel: '通知渠道',
}

export function AuditTab() {
  const admin = useAtLeast('admin')
  const [rows, setRows] = useState<AuditRow[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [kind, setKind] = useState('')
  const load = useCallback(() => {
    setError(null)
    api.identity.audit({ subject_kind: kind || undefined, limit: 200 }).then(({ items }) => setRows(items)).catch(setError)
  }, [kind])
  useEffect(load, [load])
  if (!admin) return <p className="text-xs text-faint">{T.adminOnly}</p>
  return (
    <section data-audit="">
      <SectionBar title={T.audit} hint={T.auditHint}>
        <select className="field h-7 w-36 py-0 text-xs" value={kind} aria-label={T.auditFilter} onChange={(e) => setKind(e.target.value)}>
          <option value="">{T.auditFilter}</option>
          {Object.entries(KINDS).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
        </select>
      </SectionBar>
      {error ? <ErrorState error={error} onRetry={load} /> : !rows ? <Skeleton rows={5} height={28} /> : !rows.length ? (
        <EmptyState icon={<ScrollText size={22} />} title={T.auditEmpty} />
      ) : (
        <div className="divide-y rounded-lg border text-2xs">
          {rows.map((r) => (
            <details key={r.id} data-audit-row={r.action}>
              <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-1.5">
                <time className="tnum w-36 shrink-0 text-faint">{formatDateTime(r.at)}</time>
                <span className="w-28 shrink-0 truncate">{r.actor || '未署名'}</span>
                <span className="chip shrink-0" style={r.verified ? undefined : { color: 'var(--st-waiting)' }}>
                  {r.verified ? T.auditVerified : T.auditUnverified}
                </span>
                <span className="mono min-w-0 flex-1 truncate text-dim">{r.action}</span>
                <span className="shrink-0 text-faint">{KINDS[r.subject_kind] ?? r.subject_kind}{r.subject_id ? ` · ${r.subject_id}` : ''}</span>
              </summary>
              <pre className="mono max-h-48 overflow-auto border-t bg-bg px-3 py-2">{JSON.stringify(r.data, null, 2)}</pre>
            </details>
          ))}
        </div>
      )}
    </section>
  )
}
