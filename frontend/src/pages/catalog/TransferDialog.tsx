import { useState } from 'react'
import type { ChangeEvent } from 'react'
import { AlertTriangle, ArrowRight, FileJson, Upload } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import type { CatalogImportReport, CatalogImportTable } from '../../types'
import { Modal, Notice, Spinner } from '../../components/ui'
import { errorMessage } from '../../lib/errors'
import { formatDateTime, formatNumber } from '../../lib/format'
import { CATALOG_TRANSFER_TEXT as T } from '../../lib/terms'
import { patchTarget, patchValueText, patchWhere } from '../../run/catalogPatch'

// ===========================================================================
// 导入数据目录（数据目录页的「导入目录」）：选文件 → 预览（只读：对上了哪些表，各有几项新增、更新、保留、冲突、跳过，
// 冲突逐条列出）→ 确认导入 → 结果（逐表写入后的版本）。规矩由服务端定（data/catalog_transfer.py），这里照预览写出来，
// 写入前让人看清楚哪些会动、哪些以这里为准。
//
// 导出不在这里：页头的「导出目录」直接下载文件（CatalogPage）。
// ===========================================================================

type Step = 'choose' | 'preview' | 'done'

interface Chosen {
  name: string
  content: unknown
  /** 文件里写的来源数据源和导出时间（认得出才有） */
  source: string | null
  exportedAt: string | null
}

const COUNT_KEYS = ['add', 'update', 'keep', 'conflicts', 'skipped'] as const

/** 跳过的那一项写成界面上的说法（「列 gate_code 的中文名」）；关联关系只有编号，照原样写 */
const pathText = (path: string) => {
  const t = patchTarget(path)
  return t && t.kind !== 'relation' ? patchWhere({ path, before: null, after: null }) : path
}

/**
 * 导入真正写进去的表（文件中的表名）：没出错、有新增或更新的项、版本比预览时前进了。服务端合计里的 tables 是对上的表数，
 * 文件中的项这里都有了的表也算在里面，不能拿它说「写入 N 张表」。preview 是确认导入之前那次预览（版本是导入前的）；
 * 拿不到某张表导入前的版本、或者服务端没给版本时只看新增和更新
 */
export function writtenTables(result: CatalogImportReport, preview: CatalogImportReport | null): Set<string> {
  const before = new Map((preview?.tables ?? []).map((t) => [t.table_name, t.version]))
  return new Set(result.tables.filter((t) => {
    if (t.error || t.add + t.update <= 0) return false
    const was = before.get(t.table_name)
    return typeof t.version !== 'number' || typeof was !== 'number' || t.version > was
  }).map((t) => t.table_name))
}

export function ImportDialog({ sourceId, onClose, onImported }: {
  sourceId: string
  onClose: () => void
  /** 导入写进了这些表（这个数据源里的表名）：页面重取清单、正在看的表重新载入 */
  onImported: (tables: string[]) => void
}) {
  const [step, setStep] = useState<Step>('choose')
  const [file, setFile] = useState<Chosen | null>(null)
  const [includeGuidance, setIncludeGuidance] = useState(true)
  const [report, setReport] = useState<CatalogImportReport | null>(null)
  const [result, setResult] = useState<CatalogImportReport | null>(null)
  const [busy, setBusy] = useState<'preview' | 'import' | null>(null)
  const [error, setError] = useState<string | null>(null)

  const choose = async (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]
    e.target.value = ''
    if (!f) return
    setError(null)
    setFile(null)
    let text: string
    try {
      text = await f.text()
    } catch {
      setError(T.readFailed)
      return
    }
    let content: unknown
    try {
      content = JSON.parse(text)
    } catch {
      setError(T.notJson)
      return
    }
    const meta = content && typeof content === 'object' ? content as { source?: { name?: unknown }; exported_at?: unknown } : {}
    setFile({
      name: f.name, content,
      source: typeof meta.source?.name === 'string' ? meta.source.name : null,
      exportedAt: typeof meta.exported_at === 'string' ? meta.exported_at : null,
    })
  }

  const preview = async () => {
    if (!file || busy) return
    setBusy('preview')
    setError(null)
    try {
      const r = await api.dataCatalog.importPreview(sourceId, { content: file.content, include_guidance: includeGuidance })
      setReport(r)
      setStep('preview')
    } catch (e) {
      // 文件格式不对（422）、没有表结构……服务端那句话照原样写，留在这一步
      setError(errorMessage(e))
    } finally {
      setBusy(null)
    }
  }

  const doImport = async () => {
    if (!file || busy) return
    setBusy('import')
    setError(null)
    try {
      const r = await api.dataCatalog.importCatalog(sourceId, { content: file.content, include_guidance: includeGuidance })
      setResult(r)
      setStep('done')
      const written = writtenTables(r, report)
      onImported(r.tables.filter((t) => written.has(t.table_name)).map((t) => t.matched_as ?? t.table_name))
    } catch (e) {
      // 有起草任务在进行（409）、别的原因：写服务端那句话，留在预览
      setError(errorMessage(e))
    } finally {
      setBusy(null)
    }
  }

  const back = () => {
    setStep('choose')
    setReport(null)
    setError(null)
  }

  const importable = report ? report.tables.length : 0
  const footer = step === 'choose'
    ? (
      <>
        <button type="button" className="btn" onClick={onClose}>{T.cancel}</button>
        <button type="button" className="btn btn-primary" onClick={() => void preview()} disabled={!file || !!busy} data-import-preview="">
          {busy === 'preview' ? <><Spinner size={11} /> {T.previewing}</> : T.preview}
        </button>
      </>
    )
    : step === 'preview'
      ? (
        <>
          <button type="button" className="btn" onClick={back} disabled={!!busy} data-import-back="">{T.back}</button>
          <button type="button" className="btn btn-primary" onClick={() => void doImport()} disabled={!importable || !!busy} data-import-confirm="">
            {busy === 'import' ? <><Spinner size={11} /> {T.importing}</> : T.confirm(importable)}
          </button>
        </>
      )
      : <button type="button" className="btn btn-primary" onClick={onClose} data-autofocus data-import-close="">{T.close}</button>

  return (
    <Modal open onClose={onClose} title={T.title} width={640} footer={footer}>
      <div className="min-w-0 space-y-3 text-xs" data-import-dialog="" data-step={step}>
        {step === 'choose' && (
          <>
            <div className="space-y-1.5">
              <p className="label">{T.chooseLabel}</p>
              <div className="flex flex-wrap items-center gap-2">
                <label className="btn btn-sm cursor-pointer focus-within:ring-2 focus-within:ring-[var(--accent)]">
                  <Upload size={11} aria-hidden /> {T.choose}
                  <input type="file" accept=".json,application/json" className="sr-only" onChange={(e) => void choose(e)}
                         aria-label={T.chooseLabel} data-import-file="" />
                </label>
                <span className="text-2xs text-faint">{T.chooseHint}</span>
              </div>
              {file && (
                <p className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs text-dim" data-import-file-meta="">
                  <FileJson size={11} className="shrink-0" aria-hidden />
                  <span className="min-w-0 break-all">{T.chosen(file.name)}</span>
                  {file.source && <span className="text-faint">{T.fileMeta(file.source, file.exportedAt ? formatDateTime(file.exportedAt) : '—')}</span>}
                </p>
              )}
            </div>
            <label className="flex cursor-pointer items-start gap-2 rounded-md border px-3 py-2" data-import-guidance="">
              <input type="checkbox" className="mt-0.5" checked={includeGuidance} onChange={(e) => setIncludeGuidance(e.target.checked)} />
              <span className="min-w-0">
                <span className="font-medium">{T.includeGuidance}</span>
                <span className="mt-0.5 block text-2xs text-faint">{T.includeGuidanceHint}</span>
              </span>
            </label>
            <div className="rounded-md border bg-bg px-3 py-2">
              <p className="text-2xs font-medium text-dim">{T.rulesTitle}</p>
              <ul className="mt-1 list-disc space-y-0.5 pl-4 text-2xs leading-relaxed text-faint" data-import-rules="">
                {T.rules.map((r) => <li key={r}>{r}</li>)}
              </ul>
            </div>
          </>
        )}

        {step === 'preview' && report && <ReportView report={report} />}

        {step === 'done' && result && <DoneView result={result} preview={report} />}

        {error && <Notice tone="err" attr={{ 'data-import-error': '' }}>{error}</Notice>}
      </div>
    </Modal>
  )
}

/** 导入结果：合计只数真正写进去的表，一张都没写进去时说清没有需要写入的内容；逐表写第几版或没有变化 */
function DoneView({ result, preview }: { result: CatalogImportReport; preview: CatalogImportReport | null }) {
  const written = writtenTables(result, preview)
  const failed = result.tables.filter((t) => !!t.error).length
  return (
    <>
      <Notice tone="info" attr={{ 'data-import-done': '', 'data-import-written': String(written.size) }}>
        <p className="font-medium">{T.done}</p>
        <p className="mt-0.5 text-dim">{T.doneTotals(written.size, result.totals, failed, result.guidance.action === 'replace')}</p>
      </Notice>
      <ReportView report={result} done written={written} />
    </>
  )
}

/**
 * 预览、导入结果：合计、逐表的计数（导入后写第几版，没写进去的写没有变化）、对不上的表、冲突逐条、跳过的原因、起草要求
 * 怎么处理。written：导入结果里真正写进去的表
 */
function ReportView({ report, done = false, written }: { report: CatalogImportReport; done?: boolean; written?: ReadonlySet<string> }) {
  const conflicts = report.tables.flatMap((t) => t.conflicts.map((c) => ({ t, c })))
  const skipped = report.tables.flatMap((t) => t.skipped.map((s) => ({ t, s })))
  return (
    <div className="min-w-0 space-y-3">
      {!done && <p className="tnum text-sm" data-import-totals="">{T.totals(report.totals)}</p>}
      {report.tables.length > 0
        ? (
          <div className="overflow-x-auto rounded-md border">
            <table className="w-full border-collapse text-2xs">
              <thead>
                <tr className="border-b bg-elev text-left text-faint">
                  <th scope="col" className="px-2.5 py-1.5 font-medium">{T.head.table}</th>
                  {COUNT_KEYS.map((k) => <th key={k} scope="col" className="px-2 py-1.5 text-right font-medium">{T.head[k]}</th>)}
                </tr>
              </thead>
              <tbody>
                {report.tables.map((t) => <TableRow key={t.table_name} t={t} done={done} written={!!written?.has(t.table_name)} />)}
              </tbody>
            </table>
          </div>
        )
        : <p className="text-faint" data-import-nothing="">{T.nothing}</p>}
      {report.unmatched_tables.length > 0 && (
        <p className="break-words text-2xs leading-relaxed text-dim" data-import-unmatched="">
          {T.unmatched(report.unmatched_tables.length)}
          <span className="mono">{report.unmatched_tables.join('、')}</span>
        </p>
      )}
      {conflicts.length > 0 && (
        <section aria-label={T.conflictsTitle(conflicts.length)}>
          <p className="mb-1 flex items-center gap-1 text-2xs font-medium text-[var(--warn)]">
            <AlertTriangle size={11} aria-hidden /> {T.conflictsTitle(conflicts.length)}
          </p>
          <ul className="divide-y divide-[var(--hairline)] rounded-md border">
            {conflicts.map(({ t, c }) => (
              <li key={`${t.table_name}:${c.path}`} className="px-2.5 py-1.5 text-2xs leading-relaxed" data-import-conflict={`${t.table_name}:${c.path}`}>
                <p className="font-medium text-fg"><span className="mono">{t.table_name}</span> · {c.label || c.path}</p>
                <p className="flex flex-wrap items-center gap-x-1.5 text-dim">
                  <span><span className="text-faint">{T.conflictFile}：</span>{patchValueText(c.path, c.file_value)}</span>
                  <ArrowRight size={10} className="text-faint" aria-hidden />
                  <span><span className="text-faint">{T.conflictTarget}：</span>{patchValueText(c.path, c.target_value)}</span>
                </p>
                {c.why && <p className="text-faint">{c.why}</p>}
              </li>
            ))}
          </ul>
        </section>
      )}
      {skipped.length > 0 && (
        <section aria-label={T.skippedTitle(skipped.length)}>
          <p className="mb-1 text-2xs font-medium text-dim">{T.skippedTitle(skipped.length)}</p>
          <ul className="divide-y divide-[var(--hairline)] rounded-md border">
            {skipped.map(({ t, s }) => (
              <li key={`${t.table_name}:${s.path}`} className="px-2.5 py-1.5 text-2xs leading-relaxed text-dim" data-import-skipped={`${t.table_name}:${s.path}`}>
                <span className="text-faint"><span className="mono">{t.table_name}</span> · {pathText(s.path)}</span>：{s.why}
              </li>
            ))}
          </ul>
        </section>
      )}
      <p className="text-2xs text-dim" data-import-guidance-action={report.guidance.action}>{T.guidance[report.guidance.action] ?? T.guidance.none}</p>
    </div>
  )
}

function TableRow({ t, done, written }: { t: CatalogImportTable; done: boolean; written: boolean }) {
  const renamed = t.matched_as && t.matched_as !== t.table_name ? t.matched_as : null
  const n = (k: typeof COUNT_KEYS[number]) => (k === 'conflicts' || k === 'skipped' ? t[k].length : t[k])
  return (
    <tr className="border-b border-hairline last:border-b-0" data-import-table={t.table_name} data-import-written={done ? String(written) : undefined}>
      <th scope="row" className="px-2.5 py-1.5 text-left align-top font-normal">
        <span className="mono break-all text-fg">{t.table_name}</span>
        {renamed && <span className="ml-1.5 text-faint">{T.matchedAs(renamed)}</span>}
        {done && t.error && <span className="block text-[var(--err)]">{T.tableFailed(t.error)}</span>}
        {done && !t.error && (written
          ? typeof t.version === 'number' && <span className="block text-faint">{T.version(t.version)}</span>
          : <span className="block text-faint">{T.unchanged}</span>)}
      </th>
      {COUNT_KEYS.map((k) => (
        <td key={k} className={clsx('tnum px-2 py-1.5 text-right align-top', k === 'conflicts' && n(k) > 0 ? 'text-[var(--warn)]' : 'text-dim')}
            data-import-count={k}>
          {formatNumber(n(k))}
        </td>
      ))}
    </tr>
  )
}
