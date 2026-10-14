import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { confirmDialog, promptDialog, toast } from '../components/ui'
import { formatDateTime } from '../lib/format'
import { VERSION_USAGE_TEXT as T } from '../lib/terms'

/*
 * 工作流版本的使用情况与退役（A1、L4）：发布弹窗、版本历史、退役确认三处共用。
 *
 * 使用情况取账本的只读接口（/api/ledger/workflow-versions/{wf}/{v}）：被哪些应用固定、生效记录数、正式运行数。
 * 退役只给管理员（Q8）；关登录时所有人等同管理员。当前发布版不能退役，服务端回 409，这里先不给按钮
 */

export type VersionUsage = Awaited<ReturnType<typeof api.ledger.version>>

/** 一个版本的使用情况。version 为空时不取；取不到（老服务端、网络）时是 null，界面不画这一行 */
export function useVersionUsage(workflowId: string, version: number | null, epoch = 0): VersionUsage | null {
  const [usage, setUsage] = useState<VersionUsage | null>(null)
  useEffect(() => {
    setUsage(null)
    if (version == null) return
    let alive = true
    api.ledger.version(workflowId, version).then((u) => { if (alive) setUsage(u) }).catch(() => undefined)
    return () => { alive = false }
  }, [workflowId, version, epoch])
  return usage
}

/** 「被 2 个应用固定：报销审批、周报看板 · 生效记录 3 条 · 正式运行 14 次」，应用名可点开编辑 */
export function VersionUsageLine({ usage }: { usage: VersionUsage }) {
  return (
    <span className="text-dim" data-version-usage="">
      {T.apps(usage.apps.length)}
      {usage.apps.length > 0 && (
        <>
          {'：'}
          {usage.apps.map((a, i) => (
            <span key={a.app_id}>
              {i > 0 && '、'}
              <Link to={`/apps/${encodeURIComponent(a.app_id)}/edit`} className="underline-offset-2 hover:underline">
                {a.name}
              </Link>
              <span className="text-faint"> v{a.app_version}</span>
            </span>
          ))}
        </>
      )}
      <span className="text-faint"> · {T.effects(usage.effects)} · {T.formalRuns(usage.formal_runs)}</span>
    </span>
  )
}

/** 退役这个按钮现在能不能点：不能时返回为什么（给按钮的 title 和下面那一行） */
export function retireBlocked(opts: { current: boolean; publishedLevel: boolean; retired: boolean; admin: boolean }): string | null {
  if (opts.retired) return T.retired
  if (!opts.publishedLevel) return T.retireDraft
  if (opts.current) return T.retireCurrent
  if (!opts.admin) return T.retireAdmin
  return null
}

/**
 * 退役一个版本：先确认（写明受影响的应用和后果），再问一句可选的说明。成功返回 true。
 * 服务端的拒绝（当前发布版、没有权限）照它的原话弹出
 */
export async function retireVersion(workflowId: string, version: number, usage: VersionUsage | null): Promise<boolean> {
  const ok = await confirmDialog({
    title: T.retireTitle(version),
    body: T.retireBody,
    consequences: usage?.apps.length ? [T.retireAffected(usage.apps.map((a) => `${a.name} v${a.app_version}`))] : undefined,
    confirmLabel: T.retire,
    danger: true,
  })
  if (!ok) return false
  const note = await promptDialog({ title: T.retireTitle(version), label: T.retireNote, allowEmpty: true, confirmLabel: T.retire })
  if (note == null) return false
  try {
    await api.workflows.retire(workflowId, version, note.trim() || undefined)
    toast.ok(T.retired_ok(version))
    return true
  } catch (e) {
    toast.error(e)
    return false
  }
}

/** 已退役的版本那一行：谁、什么时候退的，说明 */
export function RetiredNote({ by, at, note }: { by?: string | null; at: string; note?: string | null }) {
  return (
    <span className="text-2xs" style={{ color: 'var(--st-waiting)' }} data-version-retired="">
      {T.retiredBy(by, formatDateTime(at))}{note ? ` · ${note}` : ''}
    </span>
  )
}
