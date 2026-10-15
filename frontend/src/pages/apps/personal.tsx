/**
 * 个人应用的使用面（docs/design/normal-user-apps.md N7、§4.6）：应用页顶上的一条——说清这是个人应用（只有自己看得见、
 * 结果遮罩、探索运行未经发布审核），「刷新」写明今天还剩几次，「提交审核」交给一位搭建者或一个组。
 *
 * 印章上的「探索 · 未经发布审核」由服务端写，这里不另编（P1）。剩余次数以服务端为准：详情里给了（AppDetail.personal，
 * 服务端 blueprints/personal.personal_state 的形状，typesApp 还没有它、契约待补）就写，刷新的回包里也带；不知道的不猜，
 * 只说有上限。超了上限服务端回 429，照实说用完了。蓝图改过、还没重新存的（graph_changed），照实说刷新要先重新存。
 *
 * 审核状态同样以服务端为准：最近一条审核待办还在等（pending）写「审核中」；搭建者发布了（decision=published）写
 * 「已有正式版本：去看看」，链到发布的那个应用；退回了写退回和原因，可以再提交。详情没给时看蓝图的状态（in_review）。
 */
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { FileCheck2, Hourglass, RotateCw, Send, UserRound } from 'lucide-react'
import { ApiError, api } from '../../api/client'
import { PrincipalPicker, type Principal } from '../../components/PrincipalPicker'
import { Modal, Notice, Spinner, toast } from '../../components/ui'
import { PERSONAL_TEXT as T } from '../../lib/termsApp'
import type { AppDetail } from '../../typesApp'
import { blueprintPath } from '../blueprints/shared'
import { appPath } from './shared'

/** 最近一条审核待办（服务端 personal_state.review）：status 是待办的状态，decision 是结论（published / reject） */
export interface PersonalReview {
  task_id: string
  status: 'pending' | 'resolving' | 'resolved' | 'cancelled' | 'expired'
  decision?: 'published' | 'reject' | null
  note?: string
  /** 发布出来的正式应用（decision=published 时） */
  app_id?: string | null
  to?: { kind: 'user' | 'group'; name: string }[]
  resolved_at?: string | null
}

/** 个人应用在详情里多给的（服务端 blueprints/personal.personal_state）：今天还能刷新几次、蓝图改过没有、审核到哪了 */
export interface PersonalInfo {
  remaining_today?: number | null
  daily_limit?: number | null
  blueprint_status?: string | null
  graph_changed?: boolean
  review?: PersonalReview | null
}

/** 界面上的审核状态：在等、发布了、退回了；还没提交过（或审核取消、过期）为 null */
type ReviewView =
  | { state: 'in_review'; to?: { kind: 'user' | 'group'; name: string }[] }
  | { state: 'published'; appId?: string | null }
  | { state: 'rejected'; note?: string }

function reviewView(r: PersonalReview | null | undefined): ReviewView | null {
  if (!r) return null
  if (r.status === 'pending' || r.status === 'resolving') return { state: 'in_review', to: r.to }
  if (r.decision === 'published') return { state: 'published', appId: r.app_id }
  if (r.decision === 'reject') return { state: 'rejected', note: r.note }
  return null
}

export const personalOf = (d: AppDetail | undefined): PersonalInfo | null =>
  ((d as (AppDetail & { personal?: PersonalInfo }) | undefined)?.personal) ?? null

export function PersonalBar({ detail, onRefreshed }: { detail: AppDetail; onRefreshed: () => void }) {
  const info = personalOf(detail)
  const [remaining, setRemaining] = useState<number | null>(info?.remaining_today ?? null)
  const [busy, setBusy] = useState(false)
  const [review, setReview] = useState<ReviewView | null>(reviewView(info?.review))
  const [asking, setAsking] = useState(false)
  // 详情换了（重取）就跟上服务端说的
  useEffect(() => {
    setRemaining(info?.remaining_today ?? null)
    if (info?.review) setReview(reviewView(info.review))
  }, [info?.remaining_today, info?.review])
  // 详情没说审核到哪了（老服务端）：看蓝图的状态
  useEffect(() => {
    if (info || !detail.blueprint_id) return
    let alive = true
    api.blueprints.get(detail.blueprint_id).then(
      (bp) => { if (alive && bp.status === 'in_review') setReview((r) => r ?? { state: 'in_review' }) },
      () => undefined,
    )
    return () => { alive = false }
  }, [detail.blueprint_id, info])

  const refresh = async () => {
    setBusy(true)
    try {
      const res = await api.personal.refresh(detail.id)
      setRemaining(res.remaining)
      toast.ok(T.refreshed(res.remaining))
      onRefreshed()
    } catch (e) {
      if (e instanceof ApiError && e.status === 429) {
        setRemaining(0)
        toast.warn(e.message || T.refreshUsedUp)
      } else toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  const usedUp = remaining === 0
  const names = (review?.state === 'in_review' ? review.to ?? [] : []).map((p) => p.name).join('、')
  return (
    <div className="space-y-2" data-personal-bar={detail.id}>
      <Notice tone="info" attr={{ 'data-personal-banner': '' }}>{T.banner}</Notice>
      {info?.graph_changed && detail.blueprint_id && (
        <Notice tone="warn" attr={{ 'data-personal-graph-changed': '' }}>
          {T.graphChanged}{' '}
          <Link to={blueprintPath(detail.blueprint_id)} className="underline underline-offset-2">{T.graphChangedGo}</Link>
        </Notice>
      )}
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <button type="button" className="btn btn-sm" disabled={busy || usedUp} onClick={() => void refresh()}
                title={usedUp ? T.refreshUsedUp : T.refreshLimitHint} data-personal-refresh="">
          {busy ? <Spinner size={11} /> : <RotateCw size={11} aria-hidden />} {busy ? T.refreshing : T.refresh}
        </button>
        <span className="text-2xs text-dim" data-personal-remaining={remaining ?? ''}>
          {remaining == null ? T.refreshLimitHint : usedUp ? T.refreshUsedUp : T.refreshLeft(remaining)}
        </span>
        <span className="flex-1" />
        {review?.state === 'published' ? (
          <span className="inline-flex items-center gap-1.5" data-personal-review="published">
            <FileCheck2 size={12} aria-hidden style={{ color: 'var(--st-done)' }} />
            {T.formal}
            {review.appId && (
              <Link to={appPath(review.appId)} className="underline underline-offset-2" data-personal-formal="">{T.formalGo}</Link>
            )}
          </span>
        ) : review?.state === 'in_review' ? (
          <span className="inline-flex items-center gap-1.5 text-dim" data-personal-review="in_review">
            <span className="chip" style={{ color: 'var(--st-waiting)' }}><Hourglass size={10} aria-hidden /> {T.inReview}</span>
            {names ? T.inReviewTo(names) : T.inReviewPlain}
          </span>
        ) : (
          <>
            {review?.state === 'rejected' && (
              <span className="text-2xs text-dim" data-personal-review="rejected">
                {review.note ? T.rejectedNote(review.note) : T.rejected}
              </span>
            )}
            <button type="button" className="btn btn-sm" title={T.submitReviewHint} onClick={() => setAsking(true)}
                    data-personal-submit-review="">
              <Send size={11} aria-hidden /> {T.submitReview}
            </button>
          </>
        )}
      </div>
      {asking && (
        <ReviewDialog appId={detail.id} onClose={() => setAsking(false)}
                      onSent={(to) => { setReview({ state: 'in_review', to }); setAsking(false) }} />
      )}
    </div>
  )
}

/** 提交审核：选一位搭建者或一个组，可以附一句说明 */
function ReviewDialog({ appId, onClose, onSent }: {
  appId: string; onClose: () => void; onSent: (to: { kind: 'user' | 'group'; name: string }[]) => void
}) {
  const [to, setTo] = useState<Principal[]>([])
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const picked = to.filter((p): p is Principal & { kind: 'user' | 'group' } => p.kind !== 'everyone')
  const submit = async () => {
    if (!picked.length) { toast.warn(T.reviewPick); return }
    setBusy(true)
    try {
      await api.personal.submitReview(appId, { to: picked.map(({ kind, name }) => ({ kind, name })), note: note.trim() || undefined })
      toast.ok(T.reviewSent)
      onSent(picked.map(({ kind, name }) => ({ kind, name })))
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Modal open onClose={onClose} title={T.reviewTitle} width={520} dirty={picked.length > 0 || !!note.trim()}
           footer={<>
             <button type="button" className="btn" onClick={onClose}>{T.cancel}</button>
             <button type="button" className="btn btn-primary" disabled={busy || !picked.length} title={picked.length ? undefined : T.reviewPick}
                     onClick={() => void submit()} data-personal-review-submit="">
               {busy ? <Spinner size={11} /> : <Send size={11} aria-hidden />} {T.reviewSubmit}
             </button>
           </>}>
      <div className="space-y-3" data-personal-review-dialog="">
        <p className="text-xs leading-relaxed text-dim">{T.reviewIntro}</p>
        <div>
          <div className="label"><UserRound size={11} aria-hidden className="mr-1 inline" />{T.reviewTo}</div>
          <PrincipalPicker single value={to} onChange={setTo} label={T.reviewTo} />
          <p className="mt-1 text-2xs text-faint">{T.reviewToHint}</p>
        </div>
        <div>
          <label className="label" htmlFor="personal-review-note">{T.reviewNote}</label>
          <textarea id="personal-review-note" className="field text-xs" rows={3} value={note} maxLength={500}
                    placeholder={T.reviewNotePlaceholder} onChange={(e) => setNote(e.target.value)} />
        </div>
      </div>
    </Modal>
  )
}
