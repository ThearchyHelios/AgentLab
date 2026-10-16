/**
 * 口径召回在画布上的两处（docs/design/next-round/02-口径账本.md 做法 7、8）：
 * - 发布弹窗的召回预告（RecallPreviewRow）：把这一版发布成新版本之后，旧版本被多少问数回答、导出、定时任务、应用用过，
 *   预计自动重算几组，超出上限的写明（GET /api/workflows/{id}/recall-preview）。和发布之后真的召回走同一份计划，规模一致；
 * - 版本历史里口径升到这一版之后的召回批次（RecallBatches）：每组的结论（数变了 / 没变 / 无法重算：原因 / 重算失败 / 正在
 *   重算 / 未重算），「另有 M 组未重算」和「接着算」（GET / POST /api/ledger/recall/{wf}/{v}）。之后又发布了更新的版本的，
 *   旧的那一版不再给「接着算」，写服务端给的原因（continue_reason）。
 *
 * 界面层不放逻辑：几组、能不能重算、为什么不能、能不能接着算都由服务端给。老服务端没有这两个接口（404）时不画。
 */
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ExternalLink, Play } from 'lucide-react'
import { api } from '../api/client'
import { Spinner, toast } from '../components/ui'
import { formatDateTime } from '../lib/format'
import { RECALL_TEXT } from '../lib/termsRecall'
import type { RecallBatches as Batches, RecallGroup, RecallPreview } from '../typesRecall'

const P = RECALL_TEXT.preview
const B = RECALL_TEXT.batches

/** 发布弹窗「召回」那一行。旧版本里没有口径、也没有用过的答案时不画（发布不牵涉召回） */
export function RecallPreviewRow({ workflowId, version }: { workflowId: string; version: number }) {
  const [preview, setPreview] = useState<RecallPreview | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let alive = true
    setPreview(null)
    setFailed(false)
    api.recall.preview(workflowId, version)
      .then((p) => { if (alive) setPreview(p) })
      .catch(() => { if (alive) setFailed(true) })
    return () => { alive = false }
  }, [workflowId, version])
  if (failed || !preview) {
    return failed ? null : (
      <div className="flex gap-2" data-publish-recall="loading">
        <dt className="w-14 shrink-0 text-faint">{P.label}</dt>
        <dd className="min-w-0 flex-1 text-faint">{P.loading}</dd>
      </div>
    )
  }
  if (!preview.caliber && !preview.groups) return null
  const uses = preview.versions.reduce((acc, v) => ({
    answers: acc.answers + v.answers, exports: acc.exports + v.exports, schedules: acc.schedules + v.schedules,
    apps: acc.apps + v.apps, formal: acc.formal + v.formal, pinned: acc.pinned + v.pinned,
  }), { answers: 0, exports: 0, schedules: 0, apps: 0, formal: 0, pinned: 0 })
  return (
    <div className="flex gap-2" data-publish-recall={preview.groups ? 'groups' : 'none'}>
      <dt className="w-14 shrink-0 text-faint">{P.label}</dt>
      <dd className="min-w-0 flex-1 space-y-0.5">
        {!preview.groups ? <div className="text-faint">{P.none}</div> : (
          <>
            <div data-publish-recall-uses="">{P.uses(uses)}</div>
            <div data-publish-recall-groups={preview.groups} data-publish-recall-will={preview.will_recompute}>
              {P.groups(preview.groups, preview.will_recompute)}
              {preview.already > 0 && <span className="text-faint">（{P.already(preview.already)}）</span>}
            </div>
            {preview.limit === 0 && <div style={{ color: 'var(--st-waiting)' }} data-publish-recall-zero="">{P.zero}</div>}
            {preview.limit > 0 && preview.over_limit > 0 && (
              <div style={{ color: 'var(--st-waiting)' }} data-publish-recall-over={preview.over_limit}>
                {P.over(preview.over_limit, preview.limit)}
              </div>
            )}
            {preview.not_recomputable > 0 && (
              <div className="text-dim" data-publish-recall-unrecomputable={preview.not_recomputable}>
                {P.notRecomputable(preview.not_recomputable)}：{preview.reasons.map((r) => r.reason).join('；')}
              </div>
            )}
            {preview.truncated && <div className="text-faint">{P.truncated}</div>}
            <div className="text-faint">{P.hint}</div>
          </>
        )}
      </dd>
    </div>
  )
}

const STATE_COLOR: Record<string, string | undefined> = {
  changed: 'var(--st-waiting)', failed: 'var(--err)', not_recomputable: 'var(--text-faint)', unchanged: 'var(--ok)',
}
/** 列表先列几组，其余收起 */
const SHOWN = 6

/** 版本历史里这一版的召回。还没有任何一组（旧版本没被用过、或者这是第一版）时不画 */
export function RecallBatches({ workflowId, version }: { workflowId: string; version: number }) {
  const [data, setData] = useState<Batches | null>(null)
  const [failed, setFailed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [all, setAll] = useState(false)
  useEffect(() => {
    let alive = true
    setData(null)
    setFailed(false)
    api.recall.batches(workflowId, version)
      .then((d) => { if (alive) setData(d) })
      .catch(() => { if (alive) setFailed(true) })
    return () => { alive = false }
  }, [workflowId, version])

  const continueBatch = async () => {
    if (busy) return
    setBusy(true)
    try {
      const next = await api.recall.continueBatch(workflowId, version)
      setData(next)
      toast.ok(B.continued(next.launched?.length ?? 0))
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  if (failed) return null
  if (!data) return <div className="mt-3 border-t pt-2 text-2xs text-faint" data-version-recall="loading">{B.loading}</div>
  if (!data.groups.length) return null
  const shown = all ? data.groups : data.groups.slice(0, SHOWN)
  const summary = B.summary(data.counts)
  return (
    <div className="mt-3 border-t pt-2 text-2xs" data-version-recall={data.groups.length}>
      <div className="font-medium text-dim">{B.title}</div>
      {summary && <div className="mt-1 text-dim" data-version-recall-summary="">{summary}</div>}
      <ul className="mt-1 space-y-1">
        {shown.map((g) => <GroupRow key={g.id} group={g} />)}
      </ul>
      {!all && data.groups.length > SHOWN && (
        <button type="button" className="mt-1 text-faint underline-offset-2 hover:underline" onClick={() => setAll(true)}>
          {B.more(data.groups.length - SHOWN)}
        </button>
      )}
      {data.remaining > 0 && (
        <div className="mt-1.5" data-version-recall-remaining={data.remaining}>
          <div style={{ color: 'var(--st-waiting)' }}>{B.remaining(data.remaining)}</div>
          {data.can_continue && (
            <button type="button" className="btn btn-sm mt-1 w-full justify-center" disabled={busy}
                    data-version-recall-continue="" onClick={() => void continueBatch()}>
              {busy ? <Spinner size={11} /> : <Play size={11} aria-hidden />} {busy ? B.continuing : B.continue(data.batch_size)}
            </button>
          )}
          {!data.can_continue && data.continue_reason && (
            <div className="mt-0.5 text-dim" data-version-recall-continue-reason="">{B.cannotContinue(data.continue_reason)}</div>
          )}
        </div>
      )}
      {data.truncated && <div className="mt-1 text-faint">{B.truncated}</div>}
    </div>
  )
}

function GroupRow({ group }: { group: RecallGroup }) {
  return (
    <li className="rounded border bg-bg px-2 py-1" data-version-recall-group={group.id} data-state={group.state}>
      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
        <span className="chip" style={STATE_COLOR[group.state] ? { color: STATE_COLOR[group.state] } : undefined}>
          {B.state[group.state] ?? group.state}
        </span>
        <span className="text-dim">{B.from(group.from_version)}</span>
        <span className="text-faint">{B.uses(group.uses)}</span>
        {group.latest && <span className="text-faint">· {formatDateTime(group.latest)}</span>}
      </div>
      <div className="mt-0.5 flex flex-wrap gap-1">
        {group.binding.length
          ? group.binding.map((b) => <span key={b.field} className="chip mono">{b.field}={String(b.value ?? '')}</span>)
          : <span className="text-faint">{B.noBinding}</span>}
      </div>
      {group.reason && (
        <div className="mt-0.5" style={{ color: group.state === 'pending' ? 'var(--st-waiting)' : 'var(--text-dim)' }}
             data-version-recall-reason="">
          {B.reason(group.state, group.reason)}
        </div>
      )}
      {!!group.changed_metrics?.length && <div className="mt-0.5 text-dim">{B.changedMetrics(group.changed_metrics)}</div>}
      <div className="mt-0.5 flex flex-wrap gap-x-3">
        {!!group.tasks && <span className="text-faint">{B.tasks(group.tasks)}</span>}
        {group.run_id && (
          <Link to={`/runs/${encodeURIComponent(group.run_id)}`} data-version-recall-run={group.run_id}
                className="inline-flex items-center gap-1 text-dim underline-offset-2 hover:text-fg hover:underline">
            <ExternalLink size={10} aria-hidden /> {B.openRun}
          </Link>
        )}
      </div>
    </li>
  )
}
