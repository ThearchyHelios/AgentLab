import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { AlertTriangle, ArrowLeft, ChevronLeft, ChevronRight, ListChecks, Lock, Pencil, Plus, RotateCw, Search } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import type {
  CatalogBusinessDate, CatalogCardinality, CatalogDetail, CatalogItem, CatalogRelation, CatalogReviewAction, CatalogTableKind,
  CatalogTableRow,
} from '../../types'
import { DeleteButton, EmptyState, ErrorState, Notice, Skeleton, Spinner, confirmDialog, toast } from '../../components/ui'
import { useLeaveGuard } from '../../lib/leave'
import { formatDateTime, formatNumber, formatTime } from '../../lib/format'
import {
  CATALOG_CARDINALITY_LABEL, CATALOG_IMPACT_TEXT, CATALOG_KIND_HINT, CATALOG_KIND_LABEL, CATALOG_TABLE_FIELD_HINT,
  CATALOG_TABLE_FIELD_LABEL, CATALOG_TEXT as CT, CATALOG_UI_TEXT as UT, CODES_TEXT as KT,
} from '../../lib/terms'
import { CatalogImpactList } from '../../components/CatalogImpact'
import { CountBadges, ItemMark, StatusChip, StatusLegend, SystemNotesNotice } from './parts'
import type { ReviewTarget } from './parts'
import { ColumnTable, Value, pendingCodes } from './ColumnTable'
import type { ColumnModel } from './ColumnTable'
import { CodesDialog } from './CodesDialog'
import { coverageText } from './profile'
import {
  COLUMN_FIELDS, DATE_KEYS, TABLE_FIELDS, buildNotes, changeCount, columnPath, confirmProposed, countsOf, formFromNotes,
  relationDraft, relationPath, relationRemoval, tablePath, unknownKeys, validateForm,
} from './model'
import type { CodesItem, ColumnField, EditForm, RelationDraft, TableField, TableKey } from './model'

// ===========================================================================
// 单表详情：表级各项、列表格、关联关系。每一项标出来源和状态，点状态标识做单项确认 / 驳回 / 恢复（审阅接口）；
// 「确认本表全部推断」「确认本列推断」和直接编辑都是整份提交（编辑即确认：改过的项服务端记为人工填写、已确认）。
//
// 写操作都带读到的版本。409 说明别人刚改过：不静默覆盖，横幅写明并给「重新载入」；编辑中的修改留着，重新载入前
// 先问一句。编辑中换表、离开页面走全站的离开前确认。
// ===========================================================================

const KINDS = Object.keys(CATALOG_KIND_LABEL) as CatalogTableKind[]
const CARDINALITIES = Object.keys(CATALOG_CARDINALITY_LABEL) as CatalogCardinality[]

const PROBLEM_TEXT = {
  tooLong: CT.tooLong,
  codesInvalid: CT.codesInvalid,
  codesDuplicate: CT.codesDuplicate,
  dateColumnRequired: CT.dateColumnRequired,
  relationIncomplete: CT.relationIncomplete,
  relationMismatch: CT.relationMismatch,
  relationDuplicate: CT.relationDuplicate,
}

type Editing = { initial: EditForm; form: EditForm }
type Conflict = 'edit' | 'review'

export function TableDetail({ sourceId, table, rows, drafted, fillCodes, onFillDone, prev, next, onOpen, onBack, onDetail }: {
  sourceId: string
  table: string
  /** 全部表：关联关系的目标表能点就跳过去，编辑时给目标表做候选 */
  rows: CatalogTableRow[]
  /** 起草刚写进了这些表：正在看的表在里面、又没在编辑，就重新载入 */
  drafted: { seq: number; tables: Set<string> }
  /** 从剖析报告点「填写含义」过来：载入后打开这一列的码值弹窗（seq 让同一列连点两次也会再开） */
  fillCodes?: { table: string; column: string; seq: number } | null
  /**
   * 从剖析报告打开的码值弹窗关上了（保存了、取消了），或者打不开（正在编辑这张表）：页面据此回到报告，接着处理下一列。
   * pending 是保存后这一列还有几个含义待填写；取消、打不开为 null
   */
  onFillDone?: (seq: number, pending: number | null) => void
  prev: string | null
  next: string | null
  onOpen: (table: string) => void
  /** 窄屏：回到表清单 */
  onBack: () => void
  /** 载入或写入之后的单表目录：清单据此更新这一行 */
  onDetail: (d: CatalogDetail) => void
}) {
  const [detail, setDetail] = useState<CatalogDetail | null>(null)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [busy, setBusy] = useState('')
  const [conflict, setConflict] = useState<Conflict | null>(null)
  const [writeError, setWriteError] = useState<string | null>(null)
  const [edit, setEdit] = useState<Editing | null>(null)
  const [live, setLive] = useState('')
  const [colQuery, setColQuery] = useState('')
  const [onlyPending, setOnlyPending] = useState(false)
  /** 正在填写哪一列的码值含义 */
  const [codesFor, setCodesFor] = useState<string | null>(null)
  /** 码值弹窗是从剖析报告的「填写含义」打开的：那一次的 seq。弹窗关上时据此回到报告 */
  const fillOpen = useRef<number | null>(null)
  const onFillDoneRef = useRef(onFillDone)
  onFillDoneRef.current = onFillDone
  /** 收起码值弹窗；是从剖析报告打开的就告诉页面（回到报告）。back=false：不回报告（别人刚改过，先处理冲突横幅） */
  const closeCodes = useCallback((pending: number | null, back = true) => {
    const seq = fillOpen.current
    fillOpen.current = null
    setCodesFor(null)
    if (seq != null && back) onFillDoneRef.current?.(seq, pending)
  }, [])
  const loadSeq = useRef(0)
  const onDetailRef = useRef(onDetail)
  onDetailRef.current = onDetail

  const load = useCallback(async () => {
    const seq = ++loadSeq.current
    try {
      const d = await api.dataCatalog.get(sourceId, table)
      if (seq !== loadSeq.current) return
      setDetail(d)
      setLoadError(null)
      setConflict(null)
      onDetailRef.current(d)
    } catch (e) {
      if (seq !== loadSeq.current) return
      setLoadError(e)
    }
  }, [sourceId, table])

  useEffect(() => {
    setDetail(null)
    setLoadError(null)
    setEdit(null)
    setConflict(null)
    setWriteError(null)
    setColQuery('')
    setOnlyPending(false)
    setLive('')
    setCodesFor(null)
    fillOpen.current = null
    void load()
  }, [load])

  // 起草写进了这张表：没在编辑就重新载入；在编辑就不动，保存时服务端按版本号拦下，横幅里再重新载入
  const editingRef = useRef(false)
  editingRef.current = !!edit
  // 只认新的一次起草：换表时上面那个 effect 已经载入过，不再重复取
  const draftSeen = useRef(drafted.seq)
  useEffect(() => {
    if (drafted.seq === draftSeen.current) return
    draftSeen.current = drafted.seq
    if (drafted.tables.has(table) && !editingRef.current) void load()
  }, [drafted, table, load])

  // 剖析报告里点了「填写含义」：这张表载入后打开那一列的码值弹窗。编辑中不打开，免得同一项两处同时改
  const fillSeen = useRef(0)
  useEffect(() => {
    if (!fillCodes || fillCodes.table !== table || fillCodes.seq === fillSeen.current) return
    if (!detail || detail.table_name !== table) return
    fillSeen.current = fillCodes.seq
    if (!editingRef.current && detail.notes.columns?.[fillCodes.column]?.codes) {
      fillOpen.current = fillCodes.seq
      setCodesFor(fillCodes.column)
      return
    }
    // 打不开（正在编辑这张表，或者这一列的码值已经没了）：说一声，回到报告
    if (editingRef.current) toast.warn(UT.fillWhileEditing)
    onFillDoneRef.current?.(fillCodes.seq, null)
  }, [fillCodes, table, detail])

  const changes = edit ? changeCount(edit.initial, edit.form) : 0
  const problems = useMemo(() => (edit ? validateForm(edit.form, PROBLEM_TEXT) : {}), [edit])
  const problemCount = Object.keys(problems).length

  useLeaveGuard(changes > 0, () => confirmDialog({
    title: CT.leaveTitle, consequences: CT.leaveConsequences, confirmLabel: CT.leaveConfirm, danger: true,
  }))

  /** 写操作：成功换上返回的目录；409 挂冲突横幅，422 显示服务端原话，其余弹提示。只用 ref 和 setState，自身不变 */
  const write = useCallback(async (key: string, call: () => Promise<CatalogDetail>): Promise<CatalogDetail | null> => {
    setBusy(key)
    setWriteError(null)
    try {
      const d = await call()
      setDetail(d)
      setConflict(null)
      onDetailRef.current(d)
      return d
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setConflict(editingRef.current ? 'edit' : 'review')
      else if (e instanceof ApiError && e.kind === 'http' && (e.status === 422 || e.status === 404)) setWriteError(e.message)
      else toast.error(e)
      return null
    } finally {
      setBusy('')
    }
  }, [])

  const detailRef = useRef(detail)
  detailRef.current = detail

  const review = useCallback(async (t: ReviewTarget, action: CatalogReviewAction) => {
    const d = detailRef.current
    if (!d) return
    const done = await write(t.path, () => api.dataCatalog.review(sourceId, table, { path: t.path, action, if_version: d.version }))
    if (done) setLive(action === 'reset' && t.source === 'human' ? CT.removed(t.where) : CT.reviewed[action](t.where))
  }, [sourceId, table, write])

  const confirmColumn = useCallback(async (col: string) => {
    const d = detailRef.current
    if (!d) return
    const paths = new Set(COLUMN_FIELDS.map((f) => columnPath(col, f)))
    const { notes, n } = confirmProposed(d.notes, (p) => paths.has(p))
    if (!n) return
    const done = await write(`column:${col}`, () => api.dataCatalog.put(sourceId, table, notes, d.version))
    if (done) setLive(CT.confirmedN(n))
  }, [sourceId, table, write])

  const fillCodesOf = useCallback((col: string) => setCodesFor(col), [])

  /**
   * 填好的码值含义（和「已列出全部取值」）整份提交：只换这一列码值的值，服务端记为人工填写、已确认。别人刚改过（409）时收起弹窗、挂冲突横幅；
   * 其余失败弹窗留着，填的字不丢
   */
  const saveCodes = useCallback(async (col: string, value: Record<string, string>, complete: boolean) => {
    const d = detailRef.current
    const item = d?.notes.columns?.[col]?.codes
    if (!d || !item) return
    const notes = structuredClone(d.notes)
    // 「已列出全部取值」跟着交：取消勾选交 false（服务端不留这个键），只改勾选也算人工改动
    notes.columns![col] = { ...notes.columns![col], codes: { ...item, value, complete } as CodesItem }
    setBusy(`codes:${col}`)
    setWriteError(null)
    try {
      const next = await api.dataCatalog.put(sourceId, table, notes, d.version)
      setDetail(next)
      setConflict(null)
      onDetailRef.current(next)
      closeCodes(pendingCodes(value))
      const filled = Math.max(0, pendingCodes(item.value) - pendingCodes(value))
      toast.ok(KT.saved(filled))
      setLive(KT.saved(filled))
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        closeCodes(null, false)
        setConflict('review')
      } else {
        toast.error(e)
      }
    } finally {
      setBusy('')
    }
  }, [sourceId, table, closeCodes])

  const setColumn = useCallback((col: string, f: ColumnField, v: string) => {
    setEdit((e) => e && ({ ...e, form: { ...e.form, columns: { ...e.form.columns, [col]: { ...e.form.columns[col], [f]: v } } } }))
  }, [])
  const setCodesComplete = useCallback((col: string, v: boolean) => {
    setEdit((e) => e && ({ ...e, form: { ...e.form, columns: { ...e.form.columns, [col]: { ...e.form.columns[col], complete: v } } } }))
  }, [])
  const setTable = (k: TableKey, v: string) => setEdit((e) => e && ({ ...e, form: { ...e.form, table: { ...e.form.table, [k]: v } } }))
  const setRelations = (fn: (rs: RelationDraft[]) => RelationDraft[]) =>
    setEdit((e) => e && ({ ...e, form: { ...e.form, relations: fn(e.form.relations) } }))

  const columns: ColumnModel[] = useMemo(() => {
    if (!detail) return []
    const st = detail.structure?.columns ?? []
    const known = new Set(st.map((c) => c.name))
    return [
      ...st.map((c) => ({ name: c.name, structure: c, items: detail.notes.columns?.[c.name] })),
      ...Object.keys(detail.notes.columns ?? {}).filter((n) => !known.has(n))
        .map((n) => ({ name: n, structure: null, items: detail.notes.columns?.[n] })),
    ]
  }, [detail])

  const shownColumns = useMemo(() => {
    const q = colQuery.trim().toLowerCase()
    return columns.filter((c) => {
      if (onlyPending && !COLUMN_FIELDS.some((f) => c.items?.[f]?.status === 'proposed')) return false
      if (!q) return true
      const label = c.items?.label?.value ?? ''
      return c.name.toLowerCase().includes(q) || label.toLowerCase().includes(q)
    })
  }, [columns, colQuery, onlyPending])

  if (!detail) {
    return (
      <div className="min-h-0 flex-1 overflow-y-auto p-4" data-catalog-detail={table} data-loading="">
        <button type="button" className="btn btn-sm mb-3 lg:hidden" onClick={onBack}><ArrowLeft size={12} aria-hidden /> {CT.backToList}</button>
        {loadError
          ? (loadError instanceof ApiError && loadError.status === 404
            ? <EmptyState title={loadError.message} offline={false} />
            : <ErrorState error={loadError} onRetry={() => void load()} />)
          : <Skeleton rows={6} height={28} gap={10} />}
      </div>
    )
  }

  const notes = detail.notes
  const counts = countsOf(notes)
  const label = notes.label && notes.label.status !== 'rejected' ? notes.label.value : null
  const kind = notes.kind && notes.kind.status !== 'rejected' ? notes.kind.value : null
  const st = detail.structure
  const systemComment = detail.system_notes ? st?.comment?.trim() : ''

  const startEdit = () => {
    const f = formFromNotes(notes, st?.columns ?? [])
    setEdit({ initial: f, form: f })
    setWriteError(null)
  }
  const cancelEdit = async () => {
    if (changes > 0 && !(await confirmDialog({ title: CT.cancelEditTitle, confirmLabel: CT.cancelEditAction, danger: true }))) return
    setEdit(null)
    setConflict(null)
    setWriteError(null)
  }
  const save = async () => {
    if (!edit || problemCount || !changes) return
    const body = buildNotes(notes, edit.initial, edit.form)
    const done = await write('save', () => api.dataCatalog.put(sourceId, table, body, detail.version))
    if (done) {
      setEdit(null)
      toast.ok(CT.saved)
    }
  }
  const confirmAll = async () => {
    const { notes: body, n } = confirmProposed(notes)
    if (!n) return
    const ok = await confirmDialog({
      title: CT.confirmAllTitle(n, label ?? table), consequences: CT.confirmAllConsequences, confirmLabel: CT.confirmAllAction(n),
    })
    if (!ok) return
    const done = await write('confirm-all', () => api.dataCatalog.put(sourceId, table, body, detail.version))
    if (done) toast.ok(CT.confirmedN(n))
  }
  const reload = async () => {
    if (changes > 0 && !(await confirmDialog({ title: CT.reloadDiscardTitle, confirmLabel: CT.reloadDiscardAction, danger: true }))) return
    setEdit(null)
    setWriteError(null)
    await load()
  }

  const targetOf = (name: string) => {
    const lower = name.toLowerCase()
    return rows.find((r) => r.table_name === name || r.qualified === name)
      ?? rows.find((r) => r.table_name.toLowerCase() === lower || r.qualified.toLowerCase() === lower)
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-catalog-detail={table} data-version={detail.version}
         data-editing={edit ? 'true' : 'false'}>
      <header className="space-y-2 border-b bg-panel px-4 py-3">
        <div className="flex items-start gap-2">
          <button type="button" className="btn btn-sm btn-ghost -ml-1 shrink-0 lg:hidden" onClick={onBack} aria-label={CT.backToList} data-catalog-detail-back="">
            <ArrowLeft size={12} aria-hidden />
          </button>
          <div className="min-w-0 flex-1">
            <h2 className="flex flex-wrap items-center gap-2 text-sm font-semibold" data-detail-title="">
              <span className={clsx('break-all', !label && 'mono')}>{label ?? detail.table_name}</span>
              {kind && <span className="chip" title={CATALOG_KIND_HINT[kind]}>{CATALOG_KIND_LABEL[kind]}</span>}
              {st?.is_view && <span className="chip">{CT.view}</span>}
            </h2>
            <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs text-faint">
              {label && <span className="mono break-all text-dim">{st?.qualified ?? detail.table_name}</span>}
              <span className="tnum" title={CT.usageHint}>{CT.usage(detail.usage)}</span>
              {/* 时间写全站统一的短格式（今天「10:05」、今年「9/27 15:21」），完整时间放在悬停里 */}
              <span className="tnum" title={detail.updated_at ? formatDateTime(detail.updated_at) : undefined} data-detail-updated="">
                {detail.updated_at
                  ? (detail.updated_by ? CT.updatedBy(detail.updated_by, formatTime(detail.updated_at)) : CT.updatedAt(formatTime(detail.updated_at)))
                  : CT.notStarted}
              </span>
              <CountBadges counts={counts} />
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <button type="button" className="btn btn-sm btn-ghost" disabled={!prev} onClick={() => prev && onOpen(prev)}
                    aria-label={CT.prev} title={prev ? `${CT.prev}：${prev}` : CT.prev} data-catalog-prev="">
              <ChevronLeft size={13} aria-hidden /><span className="hidden xl:inline">{CT.prev}</span>
            </button>
            <button type="button" className="btn btn-sm btn-ghost" disabled={!next} onClick={() => next && onOpen(next)}
                    aria-label={CT.next} title={next ? `${CT.next}：${next}` : CT.next} data-catalog-next="">
              <span className="hidden xl:inline">{CT.next}</span><ChevronRight size={13} aria-hidden />
            </button>
          </div>
        </div>
        {!edit && (
          <div className="flex flex-wrap items-center gap-1.5">
            {counts.proposed > 0 && (
              <button type="button" className="btn btn-sm" disabled={!!busy} onClick={() => void confirmAll()} data-catalog-confirm-all={counts.proposed}>
                {busy === 'confirm-all' ? <Spinner size={11} /> : <ListChecks size={12} aria-hidden />} {CT.confirmAll(counts.proposed)}
              </button>
            )}
            <button type="button" className="btn btn-sm" disabled={!!busy} onClick={startEdit} data-catalog-edit="">
              <Pencil size={11} aria-hidden /> {CT.edit}
            </button>
            <span className="text-2xs text-faint">{CT.editHint}</span>
          </div>
        )}
      </header>

      <div className="relative min-h-0 flex-1 overflow-y-auto">
        <div className="space-y-5 p-4">
          <p className="sr-only" aria-live="polite" data-catalog-live="">{live}</p>
          {conflict && (
            <Notice tone="err" attr={{ 'data-catalog-conflict': conflict }}>
              <p className="font-medium">{CT.conflictTitle}</p>
              <p className="mt-0.5 text-dim">{conflict === 'edit' ? CT.conflictEditing : CT.conflictReview}</p>
              <button type="button" className="btn btn-sm mt-2" onClick={() => void reload()} data-catalog-reload="">
                <RotateCw size={11} aria-hidden /> {CT.reload}
              </button>
            </Notice>
          )}
          {writeError && <Notice tone="err" attr={{ 'data-catalog-write-error': '' }}>{writeError}</Notice>}
          {!detail.in_schema && (
            <Notice tone="warn" attr={{ 'data-catalog-missing': '' }}>{CT.missingBody}</Notice>
          )}
          {detail.system_notes && <SystemNotesNotice />}
          {edit && <Notice tone="info">{CT.editHint}。{CT.clearHint}。</Notice>}

          <section aria-labelledby="catalog-sec-table" data-catalog-section="table">
            <div className="mb-2 flex items-center gap-2">
              <h3 id="catalog-sec-table" className="text-xs font-semibold">{CT.sectionTable}</h3>
              <span className="flex-1" />
              {!edit && <StatusLegend />}
            </div>
            <div className="rounded-lg border bg-panel">
              {systemComment && (
                <div className="flex items-start gap-2 border-b px-3 py-2.5 text-xs" data-system-table-note="">
                  <Lock size={12} className="mt-0.5 shrink-0 text-faint" aria-hidden />
                  <div className="min-w-0">
                    <div className="text-2xs text-faint">{CT.systemTableNote} · {CT.systemTag}</div>
                    <p className="mt-0.5 whitespace-pre-wrap break-words leading-relaxed text-dim">{systemComment}</p>
                  </div>
                </div>
              )}
              <dl className="divide-y divide-[var(--hairline)]">
                {TABLE_FIELDS.map((f) => (
                  <TableFieldRow key={f} field={f} item={notes[f] as CatalogItem | undefined} edit={edit} busy={!!busy}
                                 problems={problems} columns={st?.columns ?? []} onChange={setTable} onReview={review} />
                ))}
              </dl>
            </div>
          </section>

          <section aria-labelledby="catalog-sec-columns" data-catalog-section="columns">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <h3 id="catalog-sec-columns" className="text-xs font-semibold">{CT.sectionColumns}</h3>
              <span className="tnum text-2xs text-faint" data-column-count="">{CT.columnCount(shownColumns.length, columns.length)}</span>
              <span className="flex-1" />
              <div className="relative">
                <Search size={11} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-faint" aria-hidden />
                <input type="search" className="field !w-48 !py-1 pl-6 !text-2xs" placeholder={CT.columnFilter} aria-label={CT.columnFilter}
                       value={colQuery} onChange={(e) => setColQuery(e.target.value)} data-column-filter="" />
              </div>
              <label className="inline-flex items-center gap-1.5 text-2xs text-dim">
                <input type="checkbox" checked={onlyPending} onChange={(e) => setOnlyPending(e.target.checked)} data-only-pending="" />
                {CT.onlyPending}
              </label>
            </div>
            {shownColumns.length
              ? (
                <ColumnTable columns={shownColumns} form={edit?.form.columns ?? null} initial={edit?.initial.columns ?? null}
                             problems={problems} busy={!!busy} systemNotes={detail.system_notes}
                             onReview={review} onConfirmColumn={confirmColumn} onFillCodes={fillCodesOf} onChange={setColumn}
                             onCodesComplete={setCodesComplete} />
              )
              : <p className="rounded-lg border px-3 py-6 text-center text-xs text-faint">{CT.noColumns}</p>}
          </section>

          <section aria-labelledby="catalog-sec-relations" data-catalog-section="relations">
            <div className="mb-2 flex items-center gap-2">
              <h3 id="catalog-sec-relations" className="text-xs font-semibold">{CT.sectionRelations}</h3>
              <span className="tnum text-2xs text-faint">{formatNumber((edit ? edit.form.relations : notes.relations ?? []).length)}</span>
            </div>
            {edit
              ? <RelationEditor drafts={edit.form.relations} initial={edit.initial.relations} problems={problems} rows={rows}
                                onChange={setRelations} />
              : <RelationList relations={notes.relations ?? []} busy={!!busy} targetOf={targetOf} onOpen={onOpen} onReview={review} />}
          </section>

          <section aria-labelledby="catalog-sec-impact" data-catalog-section="impact">
            <div className="mb-2 flex flex-wrap items-baseline gap-2">
              <h3 id="catalog-sec-impact" className="text-xs font-semibold">{CATALOG_IMPACT_TEXT.title}</h3>
              <span className="text-2xs text-faint">{CATALOG_IMPACT_TEXT.hint}</span>
            </div>
            <CatalogImpactList sourceId={sourceId} table={detail.table_name} refreshKey={detail.version} />
          </section>
        </div>
      </div>

      {codesFor && notes.columns?.[codesFor]?.codes && (
        <CodesDialog column={codesFor} item={notes.columns[codesFor]!.codes!} saving={busy === `codes:${codesFor}`}
                     onSave={(v, complete) => void saveCodes(codesFor, v, complete)} onClose={() => closeCodes(null)} />
      )}

      {edit && (
        <footer className="flex flex-wrap items-center gap-2 border-t bg-panel px-4 py-2.5" data-catalog-edit-bar="">
          <span className="tnum text-xs text-dim" data-catalog-changes={changes}>{changes ? CT.changes(changes) : CT.noChanges}</span>
          {problemCount > 0 && (
            <span className="tnum inline-flex items-center gap-1 text-xs text-[var(--err)]" data-catalog-problems={problemCount}>
              <AlertTriangle size={11} aria-hidden /> {CT.invalidCount(problemCount)}
            </span>
          )}
          <span className="flex-1" />
          <button type="button" className="btn btn-sm" onClick={() => void cancelEdit()} disabled={busy === 'save'} data-catalog-cancel="">
            {CT.cancelEdit}
          </button>
          <button type="button" className="btn btn-sm btn-primary" onClick={() => void save()}
                  disabled={!changes || problemCount > 0 || busy === 'save'} data-catalog-save="">
            {busy === 'save' ? <><Spinner size={11} /> {CT.saving}</> : CT.save}
          </button>
        </footer>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 表级各项
// ---------------------------------------------------------------------------

function TableFieldRow({ field, item, edit, busy, problems, columns, onChange, onReview }: {
  field: TableField
  item: CatalogItem | undefined
  edit: Editing | null
  busy: boolean
  problems: Record<string, string>
  /** 表结构里的列：业务日期的候选、业务主键的核对 */
  columns: { name: string }[]
  onChange: (k: TableKey, v: string) => void
  onReview: (t: ReviewTarget, a: CatalogReviewAction) => void
}) {
  const where = CT.whereTable(CATALOG_TABLE_FIELD_LABEL[field])
  const keys: TableKey[] = field === 'business_date' ? [...DATE_KEYS] : [field as TableKey]
  const changed = !!edit && keys.some((k) => (edit.form.table[k] ?? '') !== (edit.initial.table[k] ?? ''))
  const problem = keys.map((k) => problems[`t:${k}`]).find(Boolean)
  const inputId = `catalog-field-${field}`
  const rejected = item?.status === 'rejected'
  const placeholder = rejected ? CT.rejectedPlaceholder(plainValue(field, item?.value)) : CATALOG_TABLE_FIELD_HINT[field]

  let input: ReactNode = null
  if (edit) {
    const v = (k: TableKey) => edit.form.table[k] ?? ''
    if (field === 'kind') {
      input = (
        <select id={inputId} className="field" value={v('kind')} onChange={(e) => onChange('kind', e.target.value)} data-input="kind">
          <option value="">{CT.kindNone}</option>
          {KINDS.map((k) => <option key={k} value={k}>{CATALOG_KIND_LABEL[k]}（{CATALOG_KIND_HINT[k]}）</option>)}
        </select>
      )
    } else if (field === 'business_date') {
      input = (
        <div className="grid gap-2 sm:grid-cols-[1fr_1.4fr_1fr]">
          <div>
            <label className="label" htmlFor={inputId}>{CT.dateColumn}</label>
            <input id={inputId} className="field mono" list="catalog-columns" value={v('business_date.column')}
                   aria-invalid={problems['t:business_date.column'] ? true : undefined}
                   onChange={(e) => onChange('business_date.column', e.target.value)} data-input="business_date.column" />
          </div>
          <div>
            <label className="label" htmlFor={`${inputId}-rule`}>{CT.dateRule}</label>
            <input id={`${inputId}-rule`} className="field" value={v('business_date.rule')}
                   onChange={(e) => onChange('business_date.rule', e.target.value)} data-input="business_date.rule" />
          </div>
          <div>
            <label className="label" htmlFor={`${inputId}-tz`}>{CT.dateTimezone}</label>
            <input id={`${inputId}-tz`} className="field" value={v('business_date.timezone')} placeholder="Asia/Shanghai"
                   onChange={(e) => onChange('business_date.timezone', e.target.value)} data-input="business_date.timezone" />
          </div>
          <datalist id="catalog-columns">{columns.map((c) => <option key={c.name} value={c.name} />)}</datalist>
        </div>
      )
    } else if (field === 'description') {
      input = (
        <textarea id={inputId} className="field" rows={3} value={v('description')} placeholder={placeholder}
                  aria-invalid={problem ? true : undefined} onChange={(e) => onChange('description', e.target.value)} data-input="description" />
      )
    } else {
      const k = field as TableKey
      input = (
        <input id={inputId} className={clsx('field', (field === 'valid_filter' || field === 'keys') && 'mono')} value={v(k)}
               placeholder={field === 'keys' && !rejected ? CT.keysPlaceholder : placeholder}
               aria-invalid={problem ? true : undefined} onChange={(e) => onChange(k, e.target.value)} data-input={field} />
      )
    }
  }
  const unknown = edit && field === 'keys' ? unknownKeys(edit.form.table.keys ?? '', columns) : []

  return (
    <div className="grid gap-x-4 gap-y-1 px-3 py-2.5 sm:grid-cols-[7.5rem_minmax(0,1fr)]" data-field={field} data-status={item?.status}>
      <dt className="pt-0.5 text-2xs text-faint" title={CATALOG_TABLE_FIELD_HINT[field]}>
        {edit && field !== 'business_date'
          ? <label htmlFor={inputId}>{CATALOG_TABLE_FIELD_LABEL[field]}</label>
          : CATALOG_TABLE_FIELD_LABEL[field]}
      </dt>
      <dd className="min-w-0">
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1 text-xs">{edit ? input : <FieldValue field={field} item={item} />}</div>
          {item && !changed && (
            <ItemMark target={{ path: tablePath(field), where, source: item.source, status: item.status, note: item.note, updated_at: item.updated_at }}
                      disabled={busy || !!edit} disabledHint={edit ? CT.reviewWhileEditing : undefined}
                      onReview={(a) => onReview({ path: tablePath(field), where, source: item.source, status: item.status }, a)} />
          )}
          {changed && <span className="chip shrink-0 !text-2xs" style={{ color: 'var(--accent)' }} data-changed="">{CT.changedMark}</span>}
        </div>
        {problem && <div className="mt-1 text-2xs text-[var(--err)]" role="alert">{problem}</div>}
        {!problem && unknown.length > 0 && (
          <div className="mt-1 text-2xs text-[var(--warn)]" data-keys-unknown="">{CT.keysUnknown(unknown.join('、'))}</div>
        )}
      </dd>
    </div>
  )
}

function plainValue(field: TableField, v: unknown): string {
  if (v == null) return ''
  if (field === 'keys' && Array.isArray(v)) return v.join('、')
  if (field === 'kind') return CATALOG_KIND_LABEL[v as CatalogTableKind] ?? String(v)
  if (field === 'business_date' && typeof v === 'object') return (v as CatalogBusinessDate).column
  return String(v)
}

function FieldValue({ field, item }: { field: TableField; item: CatalogItem | undefined }) {
  return (
    <Value item={item} render={(v: any) => {
      if (field === 'keys') {
        return (
          <span className="flex flex-wrap gap-1">
            {(v as string[]).map((k) => <span key={k} className="mono rounded border bg-bg px-1 text-2xs">{k}</span>)}
          </span>
        )
      }
      if (field === 'kind') {
        return <span>{CATALOG_KIND_LABEL[v as CatalogTableKind] ?? v}<span className="ml-1.5 text-2xs text-faint">{CATALOG_KIND_HINT[v as CatalogTableKind]}</span></span>
      }
      if (field === 'business_date') {
        const d = v as CatalogBusinessDate
        return (
          <span className="flex flex-wrap items-center gap-x-2">
            <span className="mono">{d.column}</span>
            {d.rule && <span className="text-dim">{CT.dateRule}：{d.rule}</span>}
            {d.timezone && <span className="text-dim">{CT.dateTimezone}：{d.timezone}</span>}
          </span>
        )
      }
      if (field === 'valid_filter') return <code className="mono break-all rounded bg-bg px-1 text-2xs">{String(v)}</code>
      return <span className="whitespace-pre-wrap break-words leading-relaxed">{String(v)}</span>
    }} />
  )
}

// ---------------------------------------------------------------------------
// 关联关系
// ---------------------------------------------------------------------------

function RelationList({ relations, busy, targetOf, onOpen, onReview }: {
  relations: CatalogRelation[]
  busy: boolean
  targetOf: (name: string) => CatalogTableRow | undefined
  onOpen: (table: string) => void
  onReview: (t: ReviewTarget, a: CatalogReviewAction) => void
}) {
  if (!relations.length) return <p className="rounded-lg border px-3 py-4 text-xs text-faint" data-relations-empty="">{CT.noRelations}</p>
  return (
    <div className="relative overflow-x-auto rounded-lg border" data-catalog-relations="">
      <table className="w-full min-w-[640px] border-collapse text-xs">
        <thead>
          <tr className="border-b bg-elev text-left text-2xs text-faint">
            <th scope="col" className="px-3 py-2 font-medium">{CT.relationHead.from}</th>
            <th scope="col" className="px-2 py-2 font-medium">{CT.relationHead.to}</th>
            <th scope="col" className="px-2 py-2 font-medium">{CT.relationHead.toColumns}</th>
            <th scope="col" className="px-2 py-2 font-medium">{CT.relationHead.cardinality}</th>
            <th scope="col" className="px-2 py-2 font-medium">{CT.relationHead.coverage}</th>
            <th scope="col" className="px-2 py-2 font-medium">{CT.relationHead.source} / {CT.relationHead.status}</th>
          </tr>
        </thead>
        <tbody>
          {relations.map((r) => {
            const target = targetOf(r.to_table)
            const where = CT.whereRelation(r.to_table)
            const rejected = r.status === 'rejected'
            return (
              <tr key={r.id} className="border-b border-hairline last:border-b-0" data-relation={r.id} data-status={r.status}>
                <td className={clsx('mono px-3 py-1.5 align-top', rejected && 'text-faint line-through')}>{r.columns.join(', ')}</td>
                <td className="px-2 py-1.5 align-top">
                  {target
                    ? (
                      <button type="button" className={clsx('mono text-left underline decoration-dotted underline-offset-2 hover:text-[var(--accent)]', rejected && 'text-faint line-through')}
                              onClick={() => onOpen(target.table_name)} title={CT.openTable(target.label ?? target.table_name)} data-relation-target={target.table_name}>
                        {r.to_table}
                      </button>
                    )
                    : <span className={clsx('mono', rejected && 'text-faint line-through')}>{r.to_table}</span>}
                  {target?.label && <div className="text-2xs text-faint">{target.label}</div>}
                </td>
                <td className={clsx('mono px-2 py-1.5 align-top', rejected && 'text-faint line-through')}>{r.to_columns.join(', ')}</td>
                <td className="px-2 py-1.5 align-top">{r.cardinality ? CATALOG_CARDINALITY_LABEL[r.cardinality] : <span className="text-faint">{CT.cardinalityNone}</span>}</td>
                <td className="tnum px-2 py-1.5 align-top">
                  {typeof r.coverage === 'number'
                    ? <span data-relation-coverage={r.coverage}>{coverageText(r.coverage)}</span>
                    : <span className="text-faint">—</span>}
                </td>
                <td className="px-2 py-1.5 align-top">
                  <ItemMark target={{ path: relationPath(r.id), where, source: r.source, status: r.status, note: r.note, updated_at: r.updated_at }}
                            disabled={busy} onReview={(a) => onReview({ path: relationPath(r.id), where, source: r.source, status: r.status }, a)} />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function RelationEditor({ drafts, initial, problems, rows, onChange }: {
  drafts: RelationDraft[]
  initial: RelationDraft[]
  problems: Record<string, string>
  rows: CatalogTableRow[]
  onChange: (fn: (rs: RelationDraft[]) => RelationDraft[]) => void
}) {
  const before = new Map(initial.map((r) => [r.key, r]))
  const patch = (key: string, p: Partial<RelationDraft>) => onChange((rs) => rs.map((r) => (r.key === key ? { ...r, ...p } : r)))
  return (
    <div className="space-y-2" data-relation-editor="">
      <p className="text-2xs leading-relaxed text-faint" data-relation-remove-hint="">{CT.relationRemoveHint}</p>
      {drafts.map((r) => {
        const b = before.get(r.key)
        const changed = !b || b.columns !== r.columns || b.to_table !== r.to_table || b.to_columns !== r.to_columns || b.cardinality !== r.cardinality
        const problem = problems[`r:${r.key}`]
        // 人工添加的才能删除；外键约束、命名推断、数据剖析得出的只能驳回（删掉会被起草和现推带回来）
        const removal = relationRemoval(r.orig)
        const rejecting = !!r.reject
        const locked = rejecting ? 'text-faint line-through' : undefined
        return (
          <div key={r.key} className="rounded-lg border bg-panel p-2.5" data-relation-draft={r.key} data-rejecting={rejecting ? 'true' : undefined}>
            <div className="grid items-end gap-2 sm:grid-cols-[1fr_1fr_1fr_8rem_auto]">
              <label className="min-w-0">
                <span className="label">{CT.relationHead.from}</span>
                <input className={clsx('field mono', locked)} value={r.columns} placeholder={CT.relationColumns} disabled={rejecting}
                       onChange={(e) => patch(r.key, { columns: e.target.value })} data-input="columns" />
              </label>
              <label className="min-w-0">
                <span className="label">{CT.relationTarget}</span>
                <input className={clsx('field mono', locked)} list="catalog-tables" value={r.to_table} disabled={rejecting}
                       onChange={(e) => patch(r.key, { to_table: e.target.value })} data-input="to_table" />
              </label>
              <label className="min-w-0">
                <span className="label">{CT.relationHead.toColumns}</span>
                <input className={clsx('field mono', locked)} value={r.to_columns} placeholder={CT.relationToColumns} disabled={rejecting}
                       onChange={(e) => patch(r.key, { to_columns: e.target.value })} data-input="to_columns" />
              </label>
              <label className="min-w-0">
                <span className="label">{CT.relationHead.cardinality}</span>
                <select className={clsx('field', locked)} value={r.cardinality} disabled={rejecting}
                        onChange={(e) => patch(r.key, { cardinality: e.target.value })} data-input="cardinality">
                  <option value="">{CT.cardinalityNone}</option>
                  {CARDINALITIES.map((c) => <option key={c} value={c}>{CATALOG_CARDINALITY_LABEL[c]}</option>)}
                </select>
              </label>
              <div className="flex items-center gap-1.5 pb-1">
                {rejecting
                  ? (
                    <>
                      <span className="chip !text-2xs" style={{ color: 'var(--accent)' }} data-rejecting-mark="">{CT.rejectingMark}</span>
                      <button type="button" className="btn btn-sm btn-ghost !px-1.5" onClick={() => patch(r.key, { reject: false })} data-relation-undo-reject="">
                        {CT.undoRejectRelation}
                      </button>
                    </>
                  )
                  : (
                    <>
                      {r.orig && !changed && <StatusChip status={r.orig.status} source={r.orig.source} />}
                      {changed && <span className="chip !text-2xs" style={{ color: 'var(--accent)' }}>{CT.changedMark}</span>}
                      {removal === 'delete' && (
                        <DeleteButton label={CT.removeRelation} onClick={() => onChange((rs) => rs.filter((x) => x.key !== r.key))} />
                      )}
                      {removal === 'reject' && (
                        // 驳回时两端和基数回到原样：驳回的是原来那条关系，不是改过的
                        <button type="button" className="btn btn-sm btn-ghost !px-1.5" aria-label={CT.rejectRelationLabel} title={CT.rejectRelationLabel}
                                onClick={() => patch(r.key, { ...(b ?? {}), reject: true })} data-relation-reject="">
                          {CT.rejectRelation}
                        </button>
                      )}
                    </>
                  )}
              </div>
            </div>
            {problem && !rejecting && <div className="mt-1.5 text-2xs text-[var(--err)]" role="alert">{problem}</div>}
          </div>
        )
      })}
      <datalist id="catalog-tables">{rows.map((t) => <option key={t.table_name} value={t.table_name}>{t.label ?? ''}</option>)}</datalist>
      <button type="button" className="btn btn-sm" onClick={() => onChange((rs) => [...rs, relationDraft(null)])} data-add-relation="">
        <Plus size={11} aria-hidden /> {CT.addRelation}
      </button>
    </div>
  )
}
