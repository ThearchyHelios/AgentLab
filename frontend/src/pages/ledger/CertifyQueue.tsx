import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ChevronLeft, Layers, Sparkles, X } from 'lucide-react'
import { api } from '../../api/client'
import { EmptyState, ErrorState, Skeleton, Spinner, promptDialog, toast } from '../../components/ui'
import { leavePass } from '../../lib/leave'
import { LEDGER_UI, caliberName } from '../../lib/termsLedger'
import { LEDGER_TEXT } from '../../lib/terms'
import { useCatalog } from '../../store/catalog'
import type { CertifyItem, CertifyQueue as Queue, CertifyTurn } from '../../typesLedger'

/**
 * 可沉淀的问题（认证队列，docs/design/next-round/02-口径账本.md 做法 8；服务端 app/ledger/certify.py）：工作流列表页
 * （编排页的「工作流」弹窗）里的一个抽屉。
 *
 * 同一类问题被反复问、Agent 反复写出同一个形状的 SQL，就该沉淀成一份口径：以后再问，比对器认得出「等价已证明」，还能按
 * 口径正式出具。三类来源照实分开写——确定性重复（同样形状的 SQL）、问题聚类（只按问题原文，问法相近不等于口径相同）、
 * 被反复用的答案——门槛、计数都是服务端给的。
 *
 * - 固化成口径草稿：走 /copilot/from-run 的 freeze（报告引用的查询变成固定 SQL 的取数节点，跨轮变化的字面量成为参数），
 *   落成新的草稿工作流，打开画布让人审改、按受管级别发布——提取器是作者，不是发布者。确定性重复的带上同组的别的运行
 *   （认哪些字面量变了）；问题聚类没有共同的 SQL，按人挑的那一轮固化。
 * - 不是口径：要写理由（记一条审计 ledger.certify.dismiss），之后这一项不再进队。
 */

const ORDER: CertifyItem['kind'][] = ['shape', 'cluster', 'reused']

/** 一轮在哪看：有对话的打开对话，没有的打开运行 */
const turnHref = (t: CertifyTurn) => (t.conversation_id ? `/chat/${t.conversation_id}` : t.run_id ? `/runs/${t.run_id}` : null)

export function CertifyQueue({ onBack, onClose, beforeLeave }: {
  onBack: () => void
  /** 打开了固化出的草稿：关掉外面的弹窗 */
  onClose: () => void
  /** 去画布之前问一句（画布上有没保存的改动时）；返回 false 就不去 */
  beforeLeave: () => Promise<boolean>
}) {
  const navigate = useNavigate()
  const refreshCatalog = useCatalog((s) => s.refresh)
  const workflows = useCatalog((s) => s.workflows)
  const [queue, setQueue] = useState<Queue | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const load = () => {
    setError(null)
    setQueue(null)
    api.caliberLedger.certifyQueue().then(setQueue, setError)
  }
  useEffect(load, [])

  const nameOf = (r: NonNullable<CertifyItem['related']>[number]) => {
    const level = r.level ? (LEDGER_TEXT.level as Record<string, string>)[r.level] ?? '' : ''
    return caliberName(workflows.find((w) => w.id === r.workflow_id)?.name ?? r.workflow_id, r.version, level)
  }

  const freeze = async (item: CertifyItem, turn?: CertifyTurn) => {
    const runId = turn?.run_id ?? item.representative?.run_id
    if (!runId) { toast.warn(LEDGER_UI.certify.noRun); return }
    if (!(await beforeLeave())) return
    const first = turn ?? item.turns.find((t) => t.run_id === runId) ?? item.turns[0]
    setBusy(`${item.id}:${runId}`)
    try {
      const out = await api.caliberLedger.freeze(runId, {
        peers: !turn && item.kind === 'shape' ? item.peers ?? [] : [],
        conversation_id: first?.conversation_id, turn_id: first?.turn_id,
      })
      toast.ok(LEDGER_UI.certify.frozenOk(out.name))
      void refreshCatalog()
      onClose()
      navigate(`/studio/${out.workflow_id}`, leavePass({ state: { discard: true } }))
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  const dismiss = async (item: CertifyItem) => {
    const note = await promptDialog({
      title: LEDGER_UI.certify.dismissTitle, label: LEDGER_UI.certify.dismissLabel,
      placeholder: LEDGER_UI.certify.dismissPlaceholder, confirmLabel: LEDGER_UI.certify.dismissConfirm,
      validate: (v) => (v.trim() ? null : LEDGER_UI.certify.dismissNeed),
    })
    if (note == null || !note.trim()) return
    setBusy(`${item.id}:dismiss`)
    try {
      await api.caliberLedger.dismiss({ item_id: item.id, kind: item.kind, note: note.trim() })
      setQueue((q) => (q ? { ...q, items: q.items.filter((i) => i.id !== item.id) } : q))
      toast.ok(LEDGER_UI.certify.dismissed)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  const C = LEDGER_UI.certify
  const groups = ORDER.map((kind) => ({ kind, items: (queue?.items ?? []).filter((i) => i.kind === kind) }))
    .filter((g) => g.items.length)

  return (
    <div className="sheet-in flex min-h-[52vh] flex-col" data-certify-drawer="" role="region" aria-label={C.title}>
      <div className="mb-2 flex items-center gap-1.5">
        <button type="button" className="btn btn-xs btn-ghost -ml-1" onClick={onBack} data-certify-back="">
          <ChevronLeft size={13} aria-hidden /> {C.back}
        </button>
      </div>
      <p className="mb-2 text-2xs leading-relaxed text-dim">{C.intro}</p>
      {error ? <ErrorState error={error} onRetry={load} compact />
        : !queue ? <Skeleton rows={4} height={40} />
        : !groups.length ? (
          <EmptyState offline={false} icon={<Layers size={20} />} title={C.empty}
                      body={<>{C.emptyBody(queue.considered.runs)}<br />{C.threshold(queue.threshold.runs, queue.threshold.conversations)}</>} />
        ) : (
          <div className="max-h-[52vh] space-y-4 overflow-y-auto pr-0.5">
            {groups.map((g) => (
              <section key={g.kind} data-certify-kind={g.kind}>
                <h4 className="text-xs font-semibold">{C.kinds[g.kind]} <span className="tnum font-normal text-faint">{g.items.length}</span></h4>
                <p className="mb-1.5 mt-0.5 text-2xs leading-relaxed text-faint">{C.kindHint[g.kind]}</p>
                <ul className="space-y-1.5">
                  {g.items.map((item) => (
                    <li key={item.id} className="rounded-lg border px-3 py-2" data-certify-item={item.id}>
                      <div className="flex items-start gap-2">
                        <div className="min-w-0 flex-1">
                          <div className="text-xs font-medium [overflow-wrap:anywhere]">{item.question || '—'}</div>
                          <div className="mt-0.5 text-2xs text-faint" data-certify-stats="">
                            {item.kind === 'shape' && [
                              C.shapeStats(item.runs ?? 0, item.conversations ?? 0),
                              C.params(item.params?.length ?? 0, item.constants ?? 0),
                              item.source ? C.source(item.source) : '',
                            ].filter(Boolean).join(' · ')}
                            {item.kind === 'cluster' && C.clusterStats(item.count ?? item.turns.length)}
                            {item.kind === 'reused' && item.uses && C.uses(item.uses)}
                          </div>
                        </div>
                        <div className="flex shrink-0 items-center gap-1">
                          {item.kind !== 'cluster' && (
                            <button type="button" className="btn btn-xs" disabled={!!busy} data-certify-freeze=""
                                    onClick={() => void freeze(item)}>
                              {busy === `${item.id}:${item.representative?.run_id}` ? <Spinner size={11} /> : <Sparkles size={11} aria-hidden />}
                              {' '}{busy === `${item.id}:${item.representative?.run_id}` ? C.freezeBusy : C.freeze}
                            </button>
                          )}
                          <button type="button" className="btn btn-xs btn-ghost" disabled={!!busy} data-certify-dismiss=""
                                  onClick={() => void dismiss(item)}>
                            <X size={11} aria-hidden /> {C.dismiss}
                          </button>
                        </div>
                      </div>
                      {item.kind === 'shape' && item.shape && (
                        <details className="mt-1.5">
                          <summary className="cursor-pointer text-2xs text-dim">{C.shape}</summary>
                          <pre className="mono mt-1 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-elev p-1.5 text-[11px]"
                               data-certify-shape="">{item.shape}</pre>
                        </details>
                      )}
                      {!!item.related?.length && (
                        <p className="mt-1 text-2xs text-dim" data-certify-related="">
                          {C.related}：{item.related.map(nameOf).join('、')}
                        </p>
                      )}
                      {!!item.frozen_into?.length && (
                        <p className="mt-1 flex flex-wrap items-center gap-1 text-2xs" data-certify-frozen="">
                          <span className="text-faint">{C.frozen}</span>
                          {item.frozen_into.map((w) => (
                            <Link key={w.workflow_id} to={`/studio/${w.workflow_id}`} onClick={onClose}
                                  className="text-[var(--accent)] hover:underline">「{w.name}」</Link>
                          ))}
                        </p>
                      )}
                      {!!item.turns.length && (
                        <details className="mt-1.5" open={item.kind === 'cluster'}>
                          <summary className="cursor-pointer text-2xs text-dim">{C.turns}（{item.turns.length}）</summary>
                          <ul className="mt-1 space-y-0.5">
                            {item.turns.map((t, i) => {
                              const href = turnHref(t)
                              return (
                                <li key={`${t.run_id}-${i}`} className="flex items-center gap-1.5 text-2xs" data-certify-turn={t.run_id ?? ''}>
                                  <span className="min-w-0 flex-1 truncate" title={t.question}>{t.question || '—'}</span>
                                  {href && (
                                    <Link to={href} onClick={onClose} className="shrink-0 text-[var(--accent)] hover:underline">
                                      {t.conversation_id ? C.openTurn : C.openRun}
                                    </Link>
                                  )}
                                  {item.kind === 'cluster' && t.run_id && (
                                    <button type="button" className="btn btn-xs btn-ghost shrink-0" disabled={!!busy}
                                            data-certify-freeze-turn={t.run_id} onClick={() => void freeze(item, t)}>
                                      {busy === `${item.id}:${t.run_id}` ? <Spinner size={11} /> : <Sparkles size={11} aria-hidden />}
                                      {' '}{C.freezeTurn}
                                    </button>
                                  )}
                                </li>
                              )
                            })}
                          </ul>
                        </details>
                      )}
                    </li>
                  ))}
                </ul>
              </section>
            ))}
          </div>
        )}
    </div>
  )
}
