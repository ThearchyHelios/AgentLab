import { useEffect, useState } from 'react'
import { Modal, Notice, Spinner, toast } from '../../components/ui'
import { api } from '../../api/client'
import { errorMessage } from '../../lib/errors'
import { EVAL_TEXT } from '../../lib/termsEval'
import type { EvalImportPlan } from '../../typesEval'

const T = EVAL_TEXT.import

/**
 * 导入题集：选一个导出的题集文件 → 预览（哪些题对得上、跳过的写原因，服务端只读、不查业务库）→ 起名 → 导入（服务端逐道
 * 试算，过了的导进来，没过的跳过、写原因）。导入的是一个新题集。
 */
export function ImportDialog({ open, onClose, onImported }: {
  open: boolean; onClose: () => void; onImported: (suiteId: string) => void
}) {
  const [content, setContent] = useState<unknown>(null)
  const [plan, setPlan] = useState<EvalImportPlan | null>(null)
  const [name, setName] = useState('')
  const [busy, setBusy] = useState<'read' | 'import' | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (open) return
    setContent(null)
    setPlan(null)
    setName('')
    setError(null)
    setBusy(null)
  }, [open])

  const pick = async (file: File | undefined) => {
    if (!file) return
    setBusy('read')
    setError(null)
    setPlan(null)
    try {
      let parsed: unknown
      try {
        parsed = JSON.parse(await file.text())
      } catch {
        setError(T.notJson)
        return
      }
      const p = await api.evals.importPreview(parsed)
      setContent(parsed)
      setPlan(p)
      setName(p.suite.name ?? '')
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setBusy(null)
    }
  }

  const submit = async () => {
    if (!plan || !content) return
    setBusy('import')
    setError(null)
    try {
      const r = await api.evals.importSuite(content, name.trim() || undefined)
      toast.ok(T.done(r.suite.name, r.imported, r.skipped.length))
      onClose()
      onImported(r.suite.id)
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setBusy(null)
    }
  }

  const sources = Object.entries(plan?.sources ?? {})
  const skipped = (plan?.cases ?? []).filter((c) => !c.ok)
  return (
    <Modal open={open} onClose={onClose} title={T.title} width={620}
           footer={(
             <>
               <button type="button" className="btn" onClick={onClose}>{EVAL_TEXT.editor.cancel}</button>
               <button type="button" className="btn btn-primary" disabled={!plan || !plan.importable || !!busy} onClick={() => void submit()}
                       data-eval-import-confirm="">
                 {busy === 'import' && <Spinner size={11} />} {busy === 'import' ? T.importing : T.confirm}
               </button>
             </>
           )}>
      <div className="space-y-3" data-eval-import="">
        <div>
          <label className="label" htmlFor="eval-import-file">{T.file}</label>
          <input id="eval-import-file" type="file" accept=".json,application/json" className="text-xs"
                 onChange={(e) => void pick(e.target.files?.[0])} data-eval-import-file="" />
          <p className="mt-1 text-2xs leading-relaxed text-faint">{T.fileHint}</p>
        </div>
        {busy === 'read' && <p className="flex items-center gap-1.5 text-xs text-dim"><Spinner size={11} /> {T.reading}</p>}
        {error && <Notice tone="err" attr={{ 'data-eval-import-error': '' }}>{error}</Notice>}
        {plan && (
          <div className="space-y-3" data-eval-import-plan={`${plan.importable}/${plan.skipped}`}>
            <p className="text-xs">{T.preview(plan.importable, plan.skipped)}</p>
            <p className="text-2xs text-faint">{T.previewHint}</p>
            <div>
              <label className="label" htmlFor="eval-import-name">{T.name}</label>
              <input id="eval-import-name" className="field" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />
            </div>
            {!!sources.length && (
              <div>
                <div className="label">{T.sources}</div>
                <ul className="space-y-0.5 text-xs">
                  {sources.map(([src, id]) => (
                    <li key={src} className="flex gap-2">
                      <span className="mono">{src}</span>
                      {id ? <span className="text-faint">✓</span> : <span className="text-[var(--warn)]">{T.sourceMissing}</span>}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {!!skipped.length && (
              <div>
                <div className="label">{T.skipped}</div>
                <ul className="space-y-1 text-xs">
                  {skipped.map((c, i) => (
                    <li key={i} className="text-dim">
                      {c.seq != null && <span className="tnum">{EVAL_TEXT.caseRow.seq(c.seq)}：</span>}{c.reason}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </div>
    </Modal>
  )
}
