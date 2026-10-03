import { useMemo, useRef, useState } from 'react'
import { Sparkles } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import type { CatalogDraftRow, CatalogTableRow } from '../../types'
import { Modal, Notice, Spinner } from '../../components/ui'
import { errorMessage } from '../../lib/errors'
import { CATALOG_TEXT as CT } from '../../lib/terms'

// ===========================================================================
// 起草数据目录。接口是同步的，一次请求起草完才返回；表多时分批调用，每批回来就更新进度和累计的新增、更新、删除，
// 中途可以停止（当前这一批做完后不再发下一批）。模型整体用不了时第一批就会知道：之后的批次不再请模型，结果里
// 写明原因。已经写进去的批次不回滚——起草只增改推断项，人工确认、驳回过的项服务端不会动。
// ===========================================================================

/** 每批几张表。只按注释、外键和命名起草时只看探查缓存，很快；请模型时服务端每 4 张一批、最多 3 批并发 */
const BATCH_PLAIN = 40
const BATCH_MODEL = 12
/** 服务端不给 tables 时取前 20 张，这里给出同样的范围，进度才算得出来 */
const TOP_N = 20
/** 结果里逐张列出的表最多几张，其余合成一句 */
const LIST_MAX = 8

type Scope = 'selected' | 'filtered' | 'top' | 'all'
type Phase = 'setup' | 'running' | 'done'

interface Outcome {
  done: number
  total: number
  added: number
  updated: number
  removed: number
  tableErrors: CatalogDraftRow[]
  modelErrors: CatalogDraftRow[]
  modelSkipped: string | null
  model: string | null
  failure: string | null
  stopped: boolean
  /** 这一次勾了「用模型起草」 */
  modelAsked: boolean
  /** 模型给了可用内容（model_items > 0）的表有几张；服务端没给 model_items（老服务端）时 modelItemsKnown 为假 */
  modelItems: number
  modelItemsKnown: boolean
}

const fresh = (total: number, modelAsked = false): Outcome => ({
  done: 0, total, added: 0, updated: 0, removed: 0, tableErrors: [], modelErrors: [], modelSkipped: null, model: null,
  failure: null, stopped: false, modelAsked, modelItems: 0, modelItemsKnown: false,
})

/**
 * 一项都没写进去时怎么说。「没有变化」只在确实是「起草出了内容、和现有目录一致」时说：有表没起草、模型在某些表上
 * 没给出内容（model_error），或者勾了用模型、却没有一张表拿到模型的可用内容（模型整体用不了、model_items 都是 0），
 * 都说「目录没有更新」，原因写在下面
 */
function nothingWrittenText(out: Outcome): string {
  const failed = out.modelErrors.length > 0 || out.tableErrors.length > 0
  const modelEmpty = out.modelAsked && (!!out.modelSkipped || (out.modelItemsKnown && out.modelItems === 0))
  return failed || modelEmpty ? CT.draftNothingWritten : CT.draftNoChange
}

export function DraftDialog({ sourceId, rows, visible, selected, filtered, onClose, onDrafted }: {
  sourceId: string
  /** 全部表（服务端的顺序：使用次数多的在前） */
  rows: CatalogTableRow[]
  /** 当前筛选结果 */
  visible: CatalogTableRow[]
  selected: string[]
  /** 清单上有没有生效的搜索或筛选：有时才给「当前筛选结果」这一项 */
  filtered: boolean
  onClose: () => void
  /** 有批次写进去了：重取清单、刷新正在看的表 */
  onDrafted: (tables: string[]) => void
}) {
  // 表结构里已经没有的表起草不了（服务端会回「表结构里没有」），不算进范围
  const live = useMemo(() => rows.filter((r) => r.in_schema).map((r) => r.table_name), [rows])
  const liveSet = useMemo(() => new Set(live), [live])
  const scopes = useMemo(() => {
    const out: { key: Scope; label: string; tables: string[] }[] = []
    const sel = selected.filter((t) => liveSet.has(t))
    if (sel.length) out.push({ key: 'selected', label: CT.scopeSelected(sel.length), tables: sel })
    const vis = visible.filter((r) => r.in_schema).map((r) => r.table_name)
    if (filtered && vis.length && vis.length !== live.length) out.push({ key: 'filtered', label: CT.scopeFiltered(vis.length), tables: vis })
    if (live.length > TOP_N) out.push({ key: 'top', label: CT.scopeTop(TOP_N), tables: live.slice(0, TOP_N) })
    out.push({ key: 'all', label: CT.scopeAll(live.length), tables: live })
    return out
  }, [selected, visible, filtered, live, liveSet])
  const [scope, setScope] = useState<Scope>(() => scopes[0].key)
  const [useModel, setUseModel] = useState(false)
  const [phase, setPhase] = useState<Phase>('setup')
  const [out, setOut] = useState<Outcome>(() => fresh(0))
  const [stopping, setStopping] = useState(false)
  const stopRef = useRef(false)
  const tables = scopes.find((s) => s.key === scope)?.tables ?? scopes[0].tables

  const run = async () => {
    stopRef.current = false
    setStopping(false)
    setPhase('running')
    const acc = fresh(tables.length, useModel)
    setOut({ ...acc })
    const touched: string[] = []
    let withModel = useModel
    let i = 0
    while (i < tables.length) {
      if (stopRef.current) { acc.stopped = true; break }
      const batch = tables.slice(i, i + (withModel ? BATCH_MODEL : BATCH_PLAIN))
      try {
        const r = await api.dataCatalog.draft(sourceId, { tables: batch, use_model: withModel })
        acc.added += r.total.added
        acc.updated += r.total.updated
        acc.removed += r.total.removed
        acc.tableErrors.push(...r.tables.filter((t) => t.error))
        acc.modelErrors.push(...r.tables.filter((t) => !t.error && t.model_error))
        acc.modelItems += r.tables.filter((t) => !t.error && (t.model_items ?? 0) > 0).length
        if (r.tables.some((t) => t.model_items !== undefined)) acc.modelItemsKnown = true
        if (r.model) acc.model = r.model
        // 模型整体用不了（没配置、已停用……）：后面的批次不再请模型，免得每批都撞一次同样的错
        if (withModel && r.model_error && !r.model_used) { acc.modelSkipped = r.model_error; withModel = false }
        touched.push(...r.tables.filter((t) => !t.error).map((t) => t.table_name))
      } catch (e) {
        acc.failure = errorMessage(e)
        break
      }
      i += batch.length
      acc.done = i
      setOut({ ...acc })
    }
    setOut({ ...acc })
    setPhase('done')
    if (touched.length) onDrafted(touched)
  }

  const stop = () => {
    stopRef.current = true
    setStopping(true)
  }

  // 进行中关窗 = 停止：当前这一批做完再关，结果照常显示
  const requestClose = () => (phase === 'running' ? stop() : onClose())

  const footer = phase === 'setup'
    ? (
      <>
        <button className="btn" onClick={onClose}>{CT.cancel}</button>
        <button className="btn btn-primary" onClick={() => void run()} disabled={!tables.length} data-draft-start="">
          <Sparkles size={12} aria-hidden /> {CT.draftStart(tables.length)}
        </button>
      </>
    )
    : phase === 'running'
      ? <button className="btn" onClick={stop} disabled={stopping} data-draft-stop="">{CT.draftStop}</button>
      : <button className="btn btn-primary" onClick={onClose} data-autofocus data-draft-close="">{CT.draftClose}</button>

  return (
    <Modal open onClose={requestClose} title={CT.draftTitle} width={560} footer={footer}>
      <div className="space-y-4" data-draft-dialog={phase}>
        {phase === 'setup' && (
          <>
            <fieldset>
              <legend className="label">{CT.draftScope}</legend>
              <div className="space-y-1.5" role="radiogroup" aria-label={CT.draftScope}>
                {scopes.map((s) => (
                  <label key={s.key} className={clsx('flex cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-xs',
                    scope === s.key ? 'border-[var(--accent)] bg-accent-soft' : 'hover:bg-hover')} data-draft-scope={s.key}>
                    <input type="radio" name="catalog-draft-scope" checked={scope === s.key} onChange={() => setScope(s.key)} />
                    <span>{s.label}</span>
                  </label>
                ))}
              </div>
            </fieldset>
            <p className="text-2xs leading-relaxed text-faint">{CT.draftBase}</p>
            <label className="flex cursor-pointer items-start gap-2 rounded-md border px-3 py-2.5 text-xs hover:bg-hover" data-draft-model="">
              <input type="checkbox" className="mt-0.5" checked={useModel} onChange={(e) => setUseModel(e.target.checked)} />
              <span className="min-w-0">
                <span className="font-medium">{CT.useModel}</span>
                <span className="mt-0.5 block text-2xs leading-relaxed text-faint">{CT.useModelHint}</span>
              </span>
            </label>
          </>
        )}

        {phase !== 'setup' && (
          <div className="space-y-3" aria-live="polite">
            <div className="flex items-center gap-2 text-xs">
              {phase === 'running' && <Spinner size={13} />}
              <span className="font-medium" data-draft-title="">
                {phase === 'running'
                  ? (stopping ? CT.draftStopping : CT.drafting)
                  : out.failure ? CT.draftFailed : out.stopped ? CT.draftStopped(out.done, out.total) : CT.draftDone}
              </span>
              <span className="flex-1" />
              <span className="tnum text-2xs text-faint" data-draft-progress={`${out.done}/${out.total}`}>{CT.draftProgress(out.done, out.total)}</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-hover" role="progressbar" aria-label={CT.draftProgress(out.done, out.total)}
                 aria-valuemin={0} aria-valuemax={out.total} aria-valuenow={out.done}>
              <div className="h-full rounded-full transition-[width] duration-300"
                   style={{ width: `${out.total ? (out.done / out.total) * 100 : 0}%`, background: out.failure ? 'var(--err)' : 'var(--accent)' }} />
            </div>
            <p className="tnum text-sm" data-draft-summary="">
              {out.added + out.updated + out.removed === 0 && phase === 'done' && !out.failure
                ? nothingWrittenText(out)
                : CT.draftSummary(out.added, out.updated, out.removed)}
            </p>
            {out.model && <p className="text-2xs text-faint">{CT.draftModelUsed(out.model)}</p>}
            {out.modelSkipped && (
              <Notice tone="warn" attr={{ 'data-draft-model-error': '' }}>{CT.draftModelSkipped(out.modelSkipped)}</Notice>
            )}
            {out.failure && <Notice tone="err" attr={{ 'data-draft-failure': '' }}>{out.failure}</Notice>}
            {out.tableErrors.length > 0 && (
              <Notice tone="err" attr={{ 'data-draft-table-errors': String(out.tableErrors.length) }}>
                <p>{CT.draftTableErrors(out.tableErrors.length)}</p>
                <ErrorList rows={out.tableErrors} pick={(r) => r.error ?? ''} />
              </Notice>
            )}
            {out.modelErrors.length > 0 && (
              <Notice tone="warn" attr={{ 'data-draft-table-model-errors': String(out.modelErrors.length) }}>
                <p>{CT.draftModelErrors(out.modelErrors.length)}</p>
                <ErrorList rows={out.modelErrors} pick={(r) => r.model_error ?? ''} />
              </Notice>
            )}
          </div>
        )}
      </div>
    </Modal>
  )
}

function ErrorList({ rows, pick }: { rows: CatalogDraftRow[]; pick: (r: CatalogDraftRow) => string }) {
  return (
    <ul className="mt-1 space-y-0.5 text-dim">
      {rows.slice(0, LIST_MAX).map((r) => (
        <li key={r.table_name} className="break-words"><span className="mono">{r.table_name}</span>：{pick(r)}</li>
      ))}
      {rows.length > LIST_MAX && <li>{CT.moreTables(rows.length - LIST_MAX)}</li>}
    </ul>
  )
}
