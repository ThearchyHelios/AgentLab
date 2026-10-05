import { useCallback, useEffect, useId, useMemo, useRef, useState, type Ref } from 'react'
import { ChevronRight, ShieldCheck } from 'lucide-react'
import { EVIDENCE_STATE, claimCountsOf, claimProblems, statsTally } from '../lib/evidence'
import { formatNumber } from '../lib/format'
import { APPROVAL_TEXT, EVIDENCE_TEXT, JUDGE_TEXT, evidenceTally, runClassLabel } from '../lib/terms'
import { useEvidence } from '../store/evidence'
import type { ApprovalReport, EvidenceSegment } from '../types'
import { EvidenceDoc, type EvidenceDocHandle } from './EvidenceDoc'
import { ReportInquiry, reportSegmentFor, type InquiryAccess } from './Inquiry'

type RunClass = 'formal' | 'exploratory' | null | undefined

/**
 * 人工审批这次是探索运行还是正式运行，批了会怎样。
 *
 * 真实运行里审批说明写着「批准后才会正式出具」，那却是一次探索运行：出具只发生在成果节点配了出具契约、从已发布
 * 版本发起的正式运行里。审批卡自己标明运行类别，不靠模型写的说明
 */
export function ApprovalRunClass({ runClass, approve }: { runClass: RunClass; approve: boolean }) {
  const formal = runClass === 'formal'
  return (
    <div className="mb-2 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-2xs leading-relaxed"
         data-approval-run-class={formal ? 'formal' : 'exploratory'}>
      <span className="chip shrink-0" style={formal ? { color: 'var(--accent)' } : undefined}>{runClassLabel(runClass)}</span>
      <span className="text-dim">{APPROVAL_TEXT.outcome(runClass, approve)}</span>
    </div>
  )
}

/** 结论句那一句：节点里判过的说判了几句、几句相矛盾；没判过的说尚未核对，并说什么时候能核对 */
function claimsLine(report: ApprovalReport, runClass: RunClass): { text: string; sub?: string; warn: boolean } {
  const judge = report.judge
  if (judge?.mode === 'inline' && judge.counts && typeof judge.counts === 'object') {
    const c = claimCountsOf(judge.counts)
    if (!c.total) return { text: APPROVAL_TEXT.claimsNone, warn: false }
    const contradicted = c.contradicted + c.unsupported
    return {
      text: APPROVAL_TEXT.claimsJudged(c.total - c.unjudged, contradicted, c.unjudged),
      warn: claimProblems(c) > 0 || c.unjudged > 0,
    }
  }
  return {
    text: APPROVAL_TEXT.claimsPending,
    sub: runClass === 'formal' ? (report.claims === 'judge' ? undefined : APPROVAL_TEXT.claimsOff)
      // 开了裁判却没在节点里判的，只会是这条规则之前的探索运行（现在交给审批的报告探索运行也当场判）
      : report.claims === 'judge' ? APPROVAL_TEXT.claimsAfterRun : APPROVAL_TEXT.claimsLater,
    warn: true,
  }
}

/**
 * 审批审的报告的核对概况：几个数字有出处（同证据条的说法），结论句判没判过、几句证据相矛盾，再给一个查看逐段证据
 * 的入口——点开是和成果里同一份可逐段点开的报告文档，点数字、句末徽标打开证据面板。
 *
 * 真实运行里报告有一句结论和证据相矛盾，审批人批准时完全不知道：卡上只有正文
 */
export function ApprovalReports({ reports, runId, runClass, approvalId }: {
  reports: ApprovalReport[]
  runId: string
  runClass: RunClass
  /**
   * 运行停在的这张审批（待处理的才给）。有它，审批人可以就报告提出质询：报告上的句子、划选的原文挂「质询」，
   * 核对概况下面是问答串和提问框（run/Inquiry.tsx）
   */
  approvalId?: string
}) {
  const access = useMemo<InquiryAccess | null>(() => (approvalId ? { approvalId } : null), [approvalId])
  if (!reports.length) return null
  return (
    <>
      {reports.map((r) => <ReportCheck key={r.node_id} report={r} runId={runId} runClass={runClass} access={access} />)}
    </>
  )
}

function ReportCheck({ report, runId, runClass, access }: {
  report: ApprovalReport; runId: string; runClass: RunClass; access: InquiryAccess | null
}) {
  const [open, setOpen] = useState(false)
  // 回答里引用了报告已有的出处：在逐段证据里打开那一处的证据面板。逐段证据收着的先展开，挂上之后再打开
  const docRef = useRef<EvidenceDocHandle>(null)
  const opening = useRef<string | null>(null)
  const docSlot = useEvidence((s) => (report.doc_artifact ? s.docs[report.doc_artifact] : undefined))
  const locate = useCallback((seg: EvidenceSegment) => {
    const id = reportSegmentFor(docSlot?.status === 'ok' ? docSlot.data : null, seg)
    if (!id) return false
    if (open && docRef.current) return docRef.current.open(id, { focus: true })
    opening.current = id
    setOpen(true)
    return true
  }, [docSlot, open])
  useEffect(() => {
    if (!open || !opening.current || !docRef.current) return
    docRef.current.open(opening.current, { focus: true })
    opening.current = null
  })
  const docId = useId()
  const counts = statsTally(report.stats)
  const suspect = typeof report.stats?.unknown_entities === 'number' ? report.stats.unknown_entities : 0
  const numbers = counts ? evidenceTally(counts.cited, counts.total, counts.other) : ''
  const claims = claimsLine(report, runClass)
  const gaps = !!counts && (counts.none > 0 || counts.other > 0)
  return (
    <div className="mb-2 rounded border bg-bg p-2" data-approval-report={report.node_id}>
      <div className="mb-1 flex items-center gap-1 text-2xs font-medium text-faint">
        <ShieldCheck size={11} aria-hidden /> {APPROVAL_TEXT.reportHead(report.label || report.node_id)}
      </div>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs">
        <span className="tnum" data-approval-report-numbers=""
              style={{ color: !counts ? 'var(--text-dim)' : gaps ? 'var(--st-waiting)' : 'var(--st-done)' }}>
          {numbers || APPROVAL_TEXT.noNumbers}
        </span>
        {suspect > 0 && (
          <span className="tnum" style={{ color: 'var(--st-waiting)' }}>
            · {EVIDENCE_STATE.suspect.glyph} {EVIDENCE_TEXT.suspectTag} {formatNumber(suspect)}
          </span>
        )}
        {/* 判定是模型给的：全都有依据也不用确定性的绿，有问题、没核对的用提醒色 */}
        <span className="tnum" data-approval-report-claims="" title={report.judge?.mode === 'inline' ? JUDGE_TEXT.badge(null) : undefined}
              style={{ color: claims.warn ? 'var(--st-waiting)' : 'var(--text-dim)' }}>
          · {claims.text}
        </span>
      </div>
      {claims.sub && <div className="mt-0.5 text-2xs leading-relaxed text-dim">{claims.sub}</div>}
      {report.doc_artifact && (
        <>
          <button type="button" className="btn btn-xs mt-1.5" data-approval-evidence="" aria-expanded={open}
                  aria-controls={docId} onClick={() => setOpen((v) => !v)}>
            <ChevronRight size={11} aria-hidden style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }} />
            {open ? APPROVAL_TEXT.closeEvidence : APPROVAL_TEXT.openEvidence}
          </button>
          {open && (
            <div id={docId} className="mt-1.5">
              <ReportDoc artifact={report.doc_artifact} runId={runId} runClass={runClass} inquiry={access} handle={docRef} />
            </div>
          )}
        </>
      )}
      {/* 审批质询：问答串（进入审批卡时按列表接口恢复）、提问框和「就整份报告提问」 */}
      <ReportInquiry runId={runId} report={report.node_id} docArtifact={report.doc_artifact} access={access} onLocate={locate} />
    </div>
  )
}

/** 报告文档：和成果里同一份可逐段点开的文档，自带证据条（定位下一处、违规清单），点开的证据面板在卡内展开 */
function ReportDoc({ artifact, runId, runClass, inquiry, handle }: {
  artifact: string; runId: string; runClass: RunClass
  /** 审批质询：句子、划选的原文上挂「质询」 */
  inquiry: InquiryAccess | null
  handle: Ref<EvidenceDocHandle>
}) {
  const slot = useEvidence((s) => s.docs[artifact])
  const loadDoc = useEvidence((s) => s.loadDoc)
  useEffect(() => { void loadDoc(artifact) }, [artifact, loadDoc])
  if (slot?.status === 'error') return <div className="text-2xs text-faint">{APPROVAL_TEXT.docMissing}</div>
  const doc = slot?.status === 'ok' ? slot.data : undefined
  if (!doc) return <div className="text-2xs text-faint">{APPROVAL_TEXT.docLoading}</div>
  return (
    <div className="max-h-96 overflow-y-auto rounded border p-2" data-approval-evidence-doc="">
      <EvidenceDoc ref={handle} doc={doc} artifact={artifact} runId={runId} dense tally runClass={runClass ?? undefined}
                   inquiry={inquiry} />
    </div>
  )
}
