import { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'
import { ArrowUp, BookMarked, Database, Info, MessagesSquare, Minus, Plus, Square, Trash2, X } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api, streamCatalogAssistant } from '../../api/client'
import type { CatalogAssistantEnd } from '../../api/client'
import type { CatalogDetail } from '../../types'
import { Notice, Spinner, confirmDialog, isComposing, toast } from '../../components/ui'
import { useLeaveGuard } from '../../lib/leave'
import { CATALOG_ASSISTANT_TEXT as T, CATALOG_STATUS_LABEL } from '../../lib/terms'
import { Markdown } from '../../run/Markdown'
import { patchValueText } from '../../run/catalogPatch'
import {
  ASSISTANT_MAX_CHARS, ASSISTANT_MAX_SAVE, QUERY_ROWS_SHOWN, applyEvent, applyPreview, assistantChangesOf, blankColumnsOf, changeStateOf,
  changeWhere, dropPending, historyOf, mergeProposal, pendingSubmit, pendingVersion, savedKey,
} from './assistant'
import type { AssistantTurn, ChangeState, PendingChange, Turn, TurnPart } from './assistant'

// ===========================================================================
// 表助手：在表详情里和助手对话完善这一张表的数据目录（POST /catalog/{table}/assistant，事件流）。
//
// 回答流式显示，能停止；助手跑过的只读查询（只有开了数据剖析的源才有）写 SQL 和前几行，默认收起；修改提案逐项写改前、
// 改后、依据，可以逐项去掉、加回。提案并进「待保存的修改」：跨多轮累积，同一项以最后一次提案为准，一键保存（最多 200 项）。
// 保存带待保存的修改里最早那条提案对照的版本：别人在那之后改过就 409，按最新内容重算（预览接口，只读）后停下来让人再看
// 一眼。对话不存服务端：每一轮把前面的对话和待保存的修改一起交上去；关闭、换表、离开页面就没有了，有待保存的修改时先问。
// ===========================================================================

type SaveState =
  | { phase: 'idle'; error?: string | null; saved?: { n: number; version: number } | null }
  | { phase: 'saving' }
  | { phase: 'reloading' }
  | { phase: 'conflict'; problems: string[] }

const charCount = (s: string) => [...s.trim()].length

export function TableAssistant({ sourceId, table, tableLabel, detail, profileEnabled, lastBlank, editing, onSaved, onClose }: {
  sourceId: string
  table: string
  /** 表的中文名（没有就是表名）：标题、提示里用 */
  tableLabel: string
  /** 页面上这张表最新的目录：留空的列、保存后更新 */
  detail: CatalogDetail
  /** 数据源开了数据剖析：助手才能发只读查询 */
  profileEnabled: boolean
  /** 上次起草模型拿不准、留空的列数：开场引导先问这几列 */
  lastBlank: number
  /** 正在编辑这张表：先不让保存（同一张表两处同时改，保存会撞上版本冲突） */
  editing: boolean
  /** 保存成功：页面换上返回的目录 */
  onSaved: (d: CatalogDetail) => void
  /** 关上面板（有待保存的修改时这里先问过了） */
  onClose: () => void
}) {
  const [turns, setTurns] = useState<Turn[]>([])
  const [pending, setPending] = useState<PendingChange[]>([])
  const [saved, setSaved] = useState<ReadonlySet<string>>(() => new Set())
  const [text, setText] = useState('')
  const [save, setSave] = useState<SaveState>({ phase: 'idle' })
  const [trayOpen, setTrayOpen] = useState(true)
  const turnsRef = useRef(turns)
  turnsRef.current = turns
  const pendingRef = useRef(pending)
  pendingRef.current = pending
  const stopRef = useRef<(() => void) | null>(null)
  const seq = useRef(0)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const logRef = useRef<HTMLDivElement>(null)
  const titleId = useId()
  const streaming = turns.some((t) => t.role === 'assistant' && t.state === 'streaming')
  const dirty = pending.length > 0 || streaming

  // 打开时焦点给输入框；卸下时停掉进行中的回答
  useEffect(() => { inputRef.current?.focus() }, [])
  useEffect(() => () => { stopRef.current?.() }, [])

  useLeaveGuard(pending.length > 0, () => confirmDialog({
    title: T.leaveTitle, consequences: T.leaveConsequences, confirmLabel: T.leaveConfirm, danger: true,
  }))

  // 新的内容到了：滚到底（人往上翻着看的时候不打扰）
  const lastLen = useRef(0)
  useEffect(() => {
    const el = logRef.current
    if (!el) return
    const size = turns.reduce((n, t) => n + (t.role === 'user' ? 1 : t.parts.length + 1), 0)
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 160
    if (size !== lastLen.current || near) el.scrollTop = el.scrollHeight
    lastLen.current = size
  }, [turns])

  const blankCols = useMemo(() => (lastBlank > 0 ? blankColumnsOf(detail) : []), [lastBlank, detail])
  const hasProposed = useMemo(() => {
    const n = detail.notes
    const items = [n.label, n.description, n.grain, n.keys, n.kind, n.business_date, n.valid_filter, n.dedup,
      ...Object.values(n.columns ?? {}).flatMap((c) => Object.values(c ?? {})), ...(n.relations ?? [])]
    return items.some((it) => !!it && (it as { status?: string }).status === 'proposed')
  }, [detail])

  const updateTurn = useCallback((id: string, fn: (t: AssistantTurn) => AssistantTurn) => {
    setTurns((ts) => ts.map((t) => (t.id === id && t.role === 'assistant' ? fn(t) : t)))
  }, [])

  const send = useCallback((raw: string) => {
    const question = raw.trim()
    if (!question || charCount(question) > ASSISTANT_MAX_CHARS) return
    if (turnsRef.current.some((t) => t.role === 'assistant' && t.state === 'streaming')) return
    const n = ++seq.current
    const userTurn: Turn = { id: `u${n}`, role: 'user', text: question }
    const reply: AssistantTurn = { id: `a${n}`, role: 'assistant', parts: [], state: 'streaming' }
    const messages = historyOf(turnsRef.current, question)
    setTurns((ts) => [...ts, userTurn, reply])
    setText('')
    let proposals = 0
    // 这一轮已经收尾（done、error）：之后再来的事件不认
    let ended = false
    stopRef.current = streamCatalogAssistant(sourceId, table, { messages, pending: pendingSubmit(pendingRef.current) }, (ev) => {
      if (ended) return
      if (ev.type === 'done' || ev.type === 'error') ended = true
      if (ev.type === 'proposal') {
        const pid = `${reply.id}.p${++proposals}`
        const changes = assistantChangesOf(ev.changes)
        updateTurn(reply.id, (t) => applyEvent(t, ev, pid))
        if (changes.length) {
          const version = typeof ev.version === 'number' && Number.isFinite(ev.version) ? ev.version : detail.version
          setPending((cur) => mergeProposal(cur, changes, pid, version))
          // 待保存的修改变了：上一次保存的结果、冲突的说明都不再对
          setSave({ phase: 'idle' })
        }
        return
      }
      updateTurn(reply.id, (t) => applyEvent(t, ev, ''))
    }, (end: CatalogAssistantEnd) => {
      stopRef.current = null
      updateTurn(reply.id, (t) => {
        if (t.state !== 'streaming') return t
        if (end.aborted) return { ...t, state: 'stopped' }
        if (end.error) return { ...t, state: 'error', error: end.error, code: end.code ?? null }
        // 流结束了却没有 done：这一轮没答完
        return { ...t, state: 'error', error: T.interrupted }
      })
    })
  }, [sourceId, table, detail.version, updateTurn])

  const stop = () => stopRef.current?.()

  const onKeyDown = (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== 'Enter' || e.shiftKey || isComposing(e)) return
    e.preventDefault()
    if (!streaming) send(text)
  }

  // 两个回调保持不变：流式的正文每来一段只重画正在回答的那一轮
  const drop = useCallback((path: string) => setPending((cur) => dropPending(cur, path)), [])
  const restore = useCallback((part: Extract<TurnPart, { kind: 'proposal' }>, path: string) => {
    const change = part.changes.find((c) => c.path === path)
    if (change) setPending((cur) => mergeProposal(cur, [change], part.id, part.version))
  }, [])
  const clearAll = async () => {
    if (!pendingRef.current.length) return
    if (!(await confirmDialog({ title: T.clearTitle(pendingRef.current.length), confirmLabel: T.clearConfirm, danger: true }))) return
    setPending([])
    setSave({ phase: 'idle' })
  }

  const doSave = async () => {
    const list = pendingRef.current
    if (!list.length || list.length > ASSISTANT_MAX_SAVE || editing) return
    setSave({ phase: 'saving' })
    try {
      const d = await api.dataCatalog.patch(sourceId, table, pendingSubmit(list), pendingVersion(list))
      setSaved((cur) => new Set([...cur, ...list.map((p) => savedKey(p.origin, p.path))]))
      setPending((cur) => cur.filter((p) => !list.includes(p)))
      setSave({ phase: 'idle', saved: { n: list.length, version: d.version } })
      onSaved(d)
      toast.ok(T.savedToast(list.length, tableLabel))
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // 别人刚改过：按最新内容重算（只读），停下来让人再看一眼
        setSave({ phase: 'reloading' })
        try {
          const p = await api.dataCatalog.patchPreview(sourceId, table, pendingSubmit(list))
          setPending((cur) => applyPreview(cur, p))
          setSave({ phase: 'conflict', problems: p.problems })
        } catch {
          setSave({ phase: 'idle', error: T.conflictFailed })
        }
      } else if (e instanceof ApiError && e.kind === 'http' && (e.status === 422 || e.status === 404)) {
        setSave({ phase: 'idle', error: e.message })
      } else {
        setSave({ phase: 'idle' })
        toast.error(e)
      }
    }
  }

  const close = async () => {
    if (dirty && !(await confirmDialog({ title: T.closeTitle, consequences: T.closeConsequences, confirmLabel: T.closeConfirm, danger: true }))) return
    stopRef.current?.()
    onClose()
  }

  const onPanelKey = (e: ReactKeyboardEvent<HTMLElement>) => {
    if (e.key !== 'Escape' || isComposing(e) || e.defaultPrevented) return
    e.preventDefault()
    void close()
  }

  const fresh = turns.length === 0
  const len = charCount(text)
  const over = len > ASSISTANT_MAX_CHARS
  const lastAssistant = [...turns].reverse().find((t): t is AssistantTurn => t.role === 'assistant')
  const status = streaming ? T.answering : lastAssistant?.state === 'stopped' ? T.stopped : lastAssistant ? T.answered : ''
  const sampleCols = blankCols.slice(0, Math.min(Math.max(lastBlank, 1), 5))

  return (
    <aside
      // 贴着详情的右边：窄一些的屏幕盖在详情上面（手机上铺满）；宽屏（xl 起）详情让出这一栏的宽度，不带阴影
      className={clsx('absolute inset-y-0 right-0 z-30 flex w-full min-h-0 flex-col border-l bg-panel shadow-elev-2 sm:w-[400px]',
        'xl:z-10 xl:w-[380px] xl:shadow-none 2xl:w-[420px]')}
      aria-labelledby={titleId}
      onKeyDown={onPanelKey}
      data-table-assistant={table}
      data-streaming={streaming ? 'true' : 'false'}
    >
      <header className="flex items-start gap-2 border-b px-3 py-2.5">
        <MessagesSquare size={14} className="mt-0.5 shrink-0" style={{ color: 'var(--accent)' }} aria-hidden />
        <div className="min-w-0 flex-1">
          <h3 id={titleId} className="text-xs font-semibold">{T.title}</h3>
          <p className="truncate text-2xs text-faint" title={T.subtitle(tableLabel)}>{T.subtitle(tableLabel)}</p>
        </div>
        <button type="button" className="shrink-0 rounded p-1 text-faint outline-none hover:bg-hover hover:text-fg focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                onClick={() => void close()} aria-label={T.close} title={T.close} data-assistant-close="">
          <X size={13} aria-hidden />
        </button>
      </header>

      <div className="border-b px-3 py-2 text-2xs leading-relaxed">
        {profileEnabled
          ? (
            <p className="flex items-start gap-1.5 text-faint" data-assistant-profile-on="">
              <Database size={11} className="mt-0.5 shrink-0" aria-hidden /> <span className="min-w-0">{T.profileOn}</span>
            </p>
          )
          : (
            <p className="flex items-start gap-1.5 text-dim" data-assistant-no-profile="">
              <Info size={11} className="mt-0.5 shrink-0" style={{ color: 'var(--st-waiting)' }} aria-hidden /> <span className="min-w-0">{T.noProfile}</span>
            </p>
          )}
      </div>

      <div ref={logRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 py-3" role="log" aria-live="polite" aria-relevant="additions"
           aria-busy={streaming} aria-label={T.logLabel} data-assistant-log="">
        <div className="space-y-2 rounded-lg border bg-bg px-3 py-2.5 text-xs leading-relaxed" data-assistant-msg="intro">
          <p className="text-dim" data-assistant-greeting="">{T.greeting}</p>
          {lastBlank > 0 && <p className="text-dim" data-assistant-blank="">{T.blankLead(lastBlank, sampleCols)}</p>}
          <p className="text-2xs text-faint">{T.notStored}</p>
          {fresh && (
            <div className="space-y-1 pt-0.5">
              <p className="text-2xs text-faint">{T.suggestLabel}</p>
              <div className="flex flex-wrap gap-1.5">
                {lastBlank > 0 && blankCols.length > 0 && (
                  <SuggestButton kind="blank" label={T.suggestBlank(lastBlank)}
                                 onClick={() => send(T.suggestBlankText(blankCols.slice(0, 20), blankCols.length))} />
                )}
                <SuggestButton kind="facts" label={T.suggestFacts} onClick={() => send(T.suggestFactsText)} />
                {hasProposed && <SuggestButton kind="pending" label={T.suggestPending} onClick={() => send(T.suggestPendingText)} />}
              </div>
            </div>
          )}
        </div>
        {turns.map((t) => (t.role === 'user'
          ? (
            <div key={t.id} className="flex justify-end" data-assistant-msg="user">
              <span className="sr-only">{T.you}：</span>
              <p className="max-w-[88%] whitespace-pre-wrap break-words rounded-lg bg-accent-soft px-3 py-2 text-xs leading-relaxed"
                 data-assistant-user-text="">
                {t.text}
              </p>
            </div>
          )
          : <AssistantTurnView key={t.id} turn={t} pending={pending} saved={saved} onDrop={drop} onRestore={restore} />))}
      </div>
      <p className="sr-only" role="status" data-assistant-status="">{status}</p>

      <SaveNotices save={save} pendingCount={pending.length} />
      {pending.length > 0 && (
        <PendingTray pending={pending} open={trayOpen} onToggle={() => setTrayOpen((v) => !v)} save={save} editing={editing}
                     onDrop={drop} onClear={() => void clearAll()} onSave={() => void doSave()} />
      )}

      <div className="border-t px-3 py-2.5">
        <div className="flex items-end gap-2">
          <textarea
            ref={inputRef}
            className="field min-h-[2.75rem] flex-1 resize-none text-xs leading-relaxed"
            rows={2}
            value={text}
            placeholder={T.inputPlaceholder}
            aria-label={T.inputLabel}
            aria-invalid={over ? true : undefined}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            data-assistant-input=""
          />
          {streaming
            ? (
              <button type="button" className="btn btn-sm shrink-0" onClick={stop} aria-label={T.stop} title={T.stop} data-assistant-stop="">
                <Square size={11} aria-hidden /> <span className="hidden sm:inline">{T.stop}</span>
              </button>
            )
            : (
              <button type="button" className="btn btn-sm btn-primary shrink-0" onClick={() => send(text)} disabled={!text.trim() || over}
                      aria-label={T.send} title={T.inputHint} data-assistant-send="">
                <ArrowUp size={12} aria-hidden /> <span className="hidden sm:inline">{T.send}</span>
              </button>
            )}
        </div>
        <div className="mt-1 flex items-center gap-2 text-2xs text-faint">
          <span className="min-w-0 flex-1 truncate">{over ? <span className="text-[var(--err)]" role="alert">{T.tooLong(ASSISTANT_MAX_CHARS)}</span> : T.inputHint}</span>
          <span className={clsx('tnum shrink-0', over && 'text-[var(--err)]')} aria-hidden>{T.count(len, ASSISTANT_MAX_CHARS)}</span>
        </div>
      </div>
    </aside>
  )
}

function SuggestButton({ kind, label, onClick }: { kind: string; label: string; onClick: () => void }) {
  return (
    <button type="button" className="btn btn-sm !text-2xs" onClick={onClick} data-assistant-suggest={kind}>{label}</button>
  )
}

// ---------------------------------------------------------------------------
// 一轮回答：正文、只读查询、修改提案按到来的先后排；末尾写这一轮的结局（出错、停止、用量）
// ---------------------------------------------------------------------------

const AssistantTurnView = memo(function AssistantTurnView({ turn, pending, saved, onDrop, onRestore }: {
  turn: AssistantTurn
  pending: PendingChange[]
  saved: ReadonlySet<string>
  onDrop: (path: string) => void
  onRestore: (part: Extract<TurnPart, { kind: 'proposal' }>, path: string) => void
}) {
  const usage = turn.usage
  return (
    <div className="space-y-2" data-assistant-msg="assistant" data-turn-state={turn.state}>
      <span className="sr-only">{T.assistant}：</span>
      {turn.parts.map((p, i) => {
        if (p.kind === 'text') {
          return (
            <div key={i} className="min-w-0 break-words text-xs" data-assistant-part="text" data-assistant-text="">
              <Markdown text={p.text} dense />
            </div>
          )
        }
        if (p.kind === 'query') return <QueryBlock key={i} part={p} />
        return <ProposalCard key={p.id} part={p} pending={pending} saved={saved} onDrop={onDrop} onRestore={onRestore} />
      })}
      {turn.state === 'streaming' && (
        <p className="flex items-center gap-1.5 text-2xs text-faint" aria-hidden><Spinner size={11} /> {T.answering}</p>
      )}
      {turn.state === 'error' && (
        <Notice tone="err" attr={{ 'data-assistant-error': '' }}>
          {turn.error ?? T.interrupted}
        </Notice>
      )}
      {turn.state === 'stopped' && (
        <p className="text-2xs text-faint" data-assistant-turn-status="stopped">{T.stopped}</p>
      )}
      {turn.state === 'done' && (!!usage || (turn.queries ?? 0) > 0) && (
        <p className="tnum text-2xs text-faint" data-assistant-turn-status="done">
          {[usage ? T.usage(usage.input_tokens, usage.output_tokens) : '', turn.queries ? T.queries(turn.queries) : ''].filter(Boolean).join(' · ')}
        </p>
      )}
    </div>
  )
})

/** 助手跑过的一条只读查询：摘要写行数（失败写原因），展开看 SQL 和前几行 */
function QueryBlock({ part }: { part: Extract<TurnPart, { kind: 'query' }> }) {
  const failed = !!part.error
  return (
    <details className="group rounded-lg border bg-bg text-2xs" data-assistant-part="query" data-assistant-query={failed ? 'error' : 'ok'}>
      <summary className="flex cursor-pointer select-none items-start gap-1.5 px-2.5 py-1.5 text-dim outline-none hover:text-fg focus-visible:ring-2 focus-visible:ring-[var(--accent)]">
        <Database size={11} className="mt-0.5 shrink-0" style={{ color: failed ? 'var(--err)' : 'var(--accent)' }} aria-hidden />
        <span className="min-w-0 break-words">{failed ? `${T.queryFailed}：${part.error}` : T.query(part.rowCount)}</span>
      </summary>
      <div className="space-y-1.5 border-t px-2.5 py-2">
        <pre className="mono max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-panel px-2 py-1.5 text-2xs" data-assistant-query-sql="">{part.sql}</pre>
        {!failed && (part.rows.length > 0 && part.columns.length > 0
          ? (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-2xs" data-assistant-query-rows="">
                <caption className="pb-1 text-left text-faint">{T.queryRows(Math.min(part.rows.length, QUERY_ROWS_SHOWN), part.rowCount)}</caption>
                <thead>
                  <tr className="border-b text-left text-faint">
                    {part.columns.map((c) => <th key={c} scope="col" className="mono px-1.5 py-1 font-medium">{c}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {part.rows.slice(0, QUERY_ROWS_SHOWN).map((r, i) => (
                    <tr key={i} className="border-b border-hairline last:border-b-0">
                      {part.columns.map((c, j) => <td key={c} className="mono px-1.5 py-1 align-top break-all">{cellText(r[j])}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
          : <p className="text-faint">{part.rowCount > 0 ? T.queryRows(0, part.rowCount) : T.queryEmpty}</p>)}
      </div>
    </details>
  )
}

const cellText = (v: unknown): string => (v == null ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v))

/** 一项的值写成一行（码值写成「1=已入园、9=已作废」）；没有写「未填写」 */
const valueText = (path: string, v: unknown) => patchValueText(path, v)

/** 一条修改提案：逐项写改前、改后、依据；可以逐项去掉、加回。已保存的项不能再加回 */
function ProposalCard({ part, pending, saved, onDrop, onRestore }: {
  part: Extract<TurnPart, { kind: 'proposal' }>
  pending: PendingChange[]
  saved: ReadonlySet<string>
  onDrop: (path: string) => void
  onRestore: (part: Extract<TurnPart, { kind: 'proposal' }>, path: string) => void
}) {
  return (
    <section className="rounded-lg border bg-bg" aria-label={T.proposal(part.changes.length)} data-assistant-part="proposal" data-assistant-proposal={part.id}>
      <header className="flex items-center gap-1.5 border-b px-2.5 py-1.5 text-2xs font-semibold">
        <BookMarked size={11} className="shrink-0" style={{ color: 'var(--accent)' }} aria-hidden />
        {T.proposal(part.changes.length)}
      </header>
      <ol className="divide-y divide-[var(--hairline)]">
        {part.changes.map((c) => {
          const state = changeStateOf(pending, saved, part.id, c.path)
          return (
            <ChangeRow key={c.path} change={c} state={state}
                       onToggle={state === 'saved' ? undefined : () => (state === 'pending' ? onDrop(c.path) : onRestore(part, c.path))} />
          )
        })}
      </ol>
    </section>
  )
}

const STATE_TONE: Record<ChangeState, string> = {
  pending: 'var(--accent)', replaced: 'var(--text-faint)', dropped: 'var(--text-faint)', saved: 'var(--st-done)',
}

function ChangeRow({ change: c, state, onToggle }: {
  change: Parameters<typeof changeWhere>[0]
  state: ChangeState
  onToggle?: () => void
}) {
  const where = changeWhere(c)
  const off = state === 'dropped' || state === 'replaced'
  return (
    <li className="px-2.5 py-2 text-2xs" data-assistant-change={c.path} data-change-state={state}>
      <div className="flex items-start gap-2">
        <span className={clsx('min-w-0 flex-1 break-words text-xs font-medium', off && 'text-faint line-through')}>{where}</span>
        <span className="shrink-0" style={{ color: STATE_TONE[state] }} data-change-state-label="">{T.state[state]}</span>
        {onToggle && (
          <button type="button" className="shrink-0 rounded px-1 text-dim outline-none hover:bg-hover hover:text-fg focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                  onClick={onToggle} aria-label={state === 'pending' ? T.dropLabel(where) : T.restoreLabel(where)} data-assistant-change-toggle="">
            {state === 'pending' ? <Minus size={10} className="mr-0.5 inline" aria-hidden /> : <Plus size={10} className="mr-0.5 inline" aria-hidden />}
            {state === 'pending' ? T.drop : T.restore}
          </button>
        )}
      </div>
      <dl className={clsx('mt-1 grid grid-cols-[auto_minmax(0,1fr)] gap-x-2 gap-y-0.5 leading-relaxed', off && 'opacity-70')}>
        <dt className="text-faint">{T.before}</dt>
        <dd className={clsx('break-words', c.before == null ? 'text-faint' : 'text-dim')} data-change-before="">
          {valueText(c.path, c.before)}
          {c.before != null && c.before_status && <span className="text-faint">（{CATALOG_STATUS_LABEL[c.before_status]}）</span>}
        </dd>
        <dt className="text-faint">{T.after}</dt>
        <dd className="break-words text-fg" data-change-after="">
          {valueText(c.path, c.after)}
          {c.state === 'confirm' && <span className="ml-1 text-faint">{T.stateConfirm}</span>}
        </dd>
        {c.reason && (
          <>
            <dt className="text-faint">{T.reason}</dt>
            <dd className="break-words text-dim" data-change-reason="">{c.reason}</dd>
          </>
        )}
      </dl>
      {c.note && <p className="mt-1 break-words text-faint" data-change-note="">{c.note}</p>}
    </li>
  )
}

// ---------------------------------------------------------------------------
// 待保存的修改：跨多轮累积，可以逐项去掉；一键保存（最多 200 项）
// ---------------------------------------------------------------------------

/**
 * 保存的结果：别人刚改过（409）后按最新内容重算了的说明（已经不合法、去掉了的项列出来）、保存被拒的原话、存上了几项。
 * 不放在「待保存的修改」里：重算之后可能一项都不剩，那一栏就收起了
 */
function SaveNotices({ save, pendingCount }: { save: SaveState; pendingCount: number }) {
  if (save.phase === 'conflict') {
    return (
      <div className="border-t px-3 py-2">
        <Notice tone="warn" attr={{ 'data-assistant-conflict': '' }}>
          <p className="font-medium">{T.conflictTitle}</p>
          <p className="text-dim">{T.conflictBody}</p>
          {save.problems.length > 0 && (
            <div data-assistant-problems={save.problems.length}>
              <p className="mt-1">{T.problems}</p>
              <ul className="list-disc pl-4 text-dim">{save.problems.map((x) => <li key={x}>{x}</li>)}</ul>
            </div>
          )}
        </Notice>
      </div>
    )
  }
  if (save.phase !== 'idle') return null
  if (save.error) return <div className="border-t px-3 py-2"><Notice tone="err" attr={{ 'data-assistant-save-error': '' }}>{save.error}</Notice></div>
  if (save.saved && !pendingCount) {
    return <div className="border-t px-3 py-2"><Notice tone="info" attr={{ 'data-assistant-saved': '' }}>{T.saved(save.saved.n, save.saved.version)}</Notice></div>
  }
  return null
}

function PendingTray({ pending, open, onToggle, save, editing, onDrop, onClear, onSave }: {
  pending: PendingChange[]
  open: boolean
  onToggle: () => void
  save: SaveState
  editing: boolean
  onDrop: (path: string) => void
  onClear: () => void
  onSave: () => void
}) {
  const n = pending.length
  const tooMany = n > ASSISTANT_MAX_SAVE
  const busy = save.phase === 'saving' || save.phase === 'reloading'
  const listId = useId()
  return (
    <section className="border-t bg-elev" aria-label={T.pending(n)} data-assistant-pending={n}>
      <div className="flex items-center gap-2 px-3 pt-2">
        <button type="button" className="min-w-0 flex-1 truncate rounded text-left text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                onClick={onToggle} aria-expanded={open} aria-controls={listId} title={open ? T.pendingHide : T.pendingShow}
                data-assistant-pending-toggle="">
          {T.pending(n)}
        </button>
        <button type="button" className="shrink-0 rounded px-1 text-2xs text-faint outline-none hover:bg-hover hover:text-fg focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                onClick={onClear} disabled={busy} data-assistant-clear="">
          <Trash2 size={10} className="mr-0.5 inline" aria-hidden />{T.clear}
        </button>
      </div>
      {open && (
        <ul id={listId} className="mx-3 mt-1.5 max-h-48 divide-y divide-[var(--hairline)] overflow-y-auto rounded-md border bg-panel">
          {pending.map((p) => {
            const where = changeWhere(p)
            return (
              <li key={p.path} className="flex items-start gap-2 px-2 py-1.5 text-2xs" data-assistant-pending-item={p.path}>
                <div className="min-w-0 flex-1 leading-relaxed">
                  <p className="break-words font-medium text-fg">{where}</p>
                  <p className="break-words text-dim">
                    <span className="text-faint">{T.before} </span>{valueText(p.path, p.before)}
                    <span className="text-faint"> → {T.after} </span><span className="text-fg">{valueText(p.path, p.after)}</span>
                  </p>
                  {p.reason && <p className="break-words text-faint"><span>{T.reason}：</span>{p.reason}</p>}
                </div>
                <button type="button" className="shrink-0 rounded px-1 text-dim outline-none hover:bg-hover hover:text-fg focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                        onClick={() => onDrop(p.path)} disabled={busy} aria-label={T.dropLabel(where)} title={T.dropLabel(where)}
                        data-assistant-pending-remove="">
                  {T.drop}
                </button>
              </li>
            )
          })}
        </ul>
      )}
      <div className="space-y-1.5 px-3 py-2">
        {tooMany && <p className="text-2xs text-[var(--err)]" role="alert" data-assistant-too-many="">{T.tooMany(n, ASSISTANT_MAX_SAVE)}</p>}
        {editing && <p className="text-2xs text-[var(--warn)]" data-assistant-editing="">{T.editing}</p>}
        <div className="flex items-center gap-2">
          <span className="min-w-0 flex-1 text-2xs leading-relaxed text-faint">{T.pendingHint}</span>
          <button type="button" className="btn btn-sm btn-primary shrink-0" onClick={onSave} disabled={busy || tooMany || editing}
                  data-assistant-save="">
            {busy ? <><Spinner size={11} /> {T.saving}</> : T.save(n)}
          </button>
        </div>
      </div>
    </section>
  )
}
