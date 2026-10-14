/**
 * 应用页上点一个数看出处（应用平台 §3.4、§3.6）。复用运行里那一套出处链，不另画一份：
 * - 指标（kind=metric）：按 GET /runs/{id}/evidence/metrics/{node}/{metric} 取指标链（指标 → 输入 → 查询快照，封存
 *   状态、遮罩），交给证据面板（run/EvidencePanel）画——面板认的是「报告里的一个片段」，这里按这个指标拼一份只有一句话
 *   的文档，把取回的链先放进证据缓存，面板打开时直接用它，不再去请求报告片段接口；
 * - 单元格、整表（cell / query）：打开那份查询快照（ArtifactViewer，取回时复验哈希，遮罩列照样写「已遮罩」）；
 * - 报告（report，逐字取自报告的成果字段）：打开那份报告；
 * - 输入（input）：写明取自本次运行的输入，没有更上游的出处。
 *
 * 报告块里的数字由 EvidenceDoc 自己处理，不走这里。
 */
import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { X } from 'lucide-react'
import { ApiError, api } from '../api/client'
import { EvidencePanel } from '../run/EvidencePanel'
import { ArtifactViewer } from '../run/AssistantStream'
import { segmentKey, useEvidence } from '../store/evidence'
import { ErrorState, Modal, Spinner, isComposing } from '../components/ui'
import { VIEW_TEXT } from '../lib/termsApp'
import type { EvidenceDocData } from '../types'
import type { ViewEvidence } from '../typesApp'
import { ReportDoc } from './components/display'

export interface EvidenceTarget {
  evidence: ViewEvidence
  label: string
  text?: string
}

/** 片段 id 带上节点和指标：同一次运行里几个指标块各自一份缓存，也不会和报告的片段（s0、s1…）撞 */
const segIdOf = (ev: ViewEvidence) => `app:${ev.node ?? ''}.${ev.metric ?? ''}`

/** 只有一句话的文档：「指标名 数」，数是一个引用指标的片段 */
function metricDoc(ev: ViewEvidence, label: string, text: string): EvidenceDocData {
  const head = `${label} `
  const shown = text || '—'
  const end = head.length + shown.length
  return {
    run_id: ev.run_id,
    node_id: '',
    markdown: `${head}${shown}`,
    catalog: { m: { kind: 'metric', node_id: ev.node, locator: { metric: ev.metric }, eid: ev.eid } },
    blocks: [{
      id: 'b0', type: 'paragraph',
      units: [{
        id: 'u0', kind: 'connective', span: [0, end],
        segments: [
          { id: 'app:label', kind: 'text', text: head, span: [0, head.length], state: 'neutral' },
          {
            id: segIdOf(ev), kind: 'number', text: shown, span: [head.length, end], state: 'deterministic',
            cite: { alias: 'm', kind: 'metric', status: 'resolved', eid: ev.eid, locator: { metric: ev.metric } },
          },
        ],
      }],
    }],
  }
}

export function EvidenceDrawer({ target, onClose }: { target: EvidenceTarget | null; onClose: () => void }) {
  if (!target) return null
  const ev = target.evidence
  if (ev.kind === 'metric' && ev.node && ev.metric) return <MetricChain target={target} onClose={onClose} />
  if ((ev.kind === 'cell' || ev.kind === 'query') && ev.artifact) {
    return <ArtifactViewer id={ev.artifact} title={VIEW_TEXT.evidenceTitle(target.label)} onClose={onClose} />
  }
  if (ev.kind === 'report' && ev.artifact) {
    // 逐字取自报告的成果字段：打开那份报告，报告里的数字照样逐段点开
    return (
      <Modal open onClose={onClose} width={760} title={VIEW_TEXT.evidenceTitle(target.label)}>
        <ReportDoc artifact={ev.artifact} runId={ev.run_id} label={target.label} />
      </Modal>
    )
  }
  return (
    <Shell title={VIEW_TEXT.evidenceTitle(target.label)} onClose={onClose}>
      <p className="text-xs leading-relaxed text-dim">
        {ev.kind === 'input' ? VIEW_TEXT.inputSource(target.text ?? '') : VIEW_TEXT.noEvidence}
      </p>
    </Shell>
  )
}

function MetricChain({ target, onClose }: { target: EvidenceTarget; onClose: () => void }) {
  const ev = target.evidence
  const segId = segIdOf(ev)
  const key = segmentKey(ev.run_id, segId)
  const seeded = useEvidence((s) => s.segments[key]?.status === 'ok')
  const [error, setError] = useState<unknown>(null)
  const [attempt, setAttempt] = useState(0)
  const doc = useMemo(() => metricDoc(ev, target.label, target.text ?? ''), [ev, target.label, target.text])

  useEffect(() => {
    if (seeded) return
    let alive = true
    setError(null)
    api.apps.metricEvidence(ev.run_id, ev.node!, ev.metric!).then(
      (detail) => {
        if (!alive) return
        // 接口答的是这个指标，不是报告里的片段：片段的说法换成这一份文档里的，面板才不会当成「另一份报告」
        const data = { ...detail, report: undefined, segment: { id: segId, text: target.text || '—' } }
        useEvidence.setState((s) => ({ segments: { ...s.segments, [key]: { status: 'ok', data } } }))
      },
      (e) => { if (alive) setError(e) },
    )
    return () => { alive = false }
  }, [ev.run_id, ev.node, ev.metric, key, segId, seeded, target.text, attempt])

  if (!seeded) {
    return (
      <Shell title={VIEW_TEXT.evidenceTitle(target.label)} onClose={onClose}>
        {error
          ? <ErrorState compact error={error instanceof ApiError && error.status === 404 && !error.code
              ? new ApiError(404, VIEW_TEXT.evidenceFailed) : error}
                        onRetry={() => setAttempt((n) => n + 1)} />
          : <div className="flex items-center gap-2 text-xs text-dim" role="status"><Spinner size={12} /> {VIEW_TEXT.evidenceLoading}</div>}
      </Shell>
    )
  }
  const narrow = typeof window !== 'undefined' && window.innerWidth < 768
  return (
    <EvidencePanel id="app-evidence" mode={narrow ? 'drawer' : 'side'} doc={doc} runId={ev.run_id} runClass="formal"
                   view={{ kind: 'seg', id: segId }} onClose={onClose} onLocate={() => {}} onViolations={() => {}} />
  )
}

/** 取链之前、取不到时的那一块：和证据面板同一个位置、同样的关法 */
function Shell({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <section role="region" aria-label={title} data-evidence-panel="app"
             className="ev-panel-side fixed bottom-0 right-0 top-0 z-40 flex flex-col border-l bg-panel text-xs shadow-elev-3"
             style={{ width: 'min(380px, 100vw)' }}
             onKeyDown={(e) => { if (e.key === 'Escape' && !isComposing(e)) { e.stopPropagation(); onClose() } }}>
      <header className="flex items-center gap-2 border-b px-3 py-2">
        <h3 className="min-w-0 flex-1 truncate text-sm font-semibold">{title}</h3>
        <button type="button" className="btn btn-xs btn-ghost -mr-1" onClick={onClose} aria-label={VIEW_TEXT.close}>
          <X size={13} aria-hidden />
        </button>
      </header>
      <div className="px-3 py-2.5">{children}</div>
    </section>
  )
}
