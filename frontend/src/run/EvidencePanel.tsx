import { useEffect, useMemo, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from 'react'
import { ArrowLeft, ListChecks, X } from 'lucide-react'
import clsx from 'clsx'
import { StatusBadge, isComposing } from '../components/ui'
import {
  EVIDENCE_STATE, docForeign, graphDoc, integrityFailures, locatable, reasonOf, sealVerdict, segmentState, sourceOf,
  type SealStatus,
} from '../lib/evidence'
import { formatNumber, NONE, shortId } from '../lib/format'
import { EVIDENCE_TEXT } from '../lib/terms'
import { segmentKey, useEvidence } from '../store/evidence'
import type {
  EvidenceDocData, EvidenceInput, EvidenceSeal, EvidenceSegment, EvidenceStep, EvidenceUnit, EvidenceViolation,
} from '../types'

/**
 * 证据面板：点开报告里的一个片段，看它从哪来。
 *
 * 本期（数字层）展示：片段和它所在的句子；指标步骤——名称、值、口径与版本、原式、
 * 代入式（每个输入是「值 ← 路径」的小标签）、复算结果；封存状态。没有证据的片段说清
 * 为什么（裸数字、引用解析不了），另有一份违规清单：列表序号、代码块标签里的数字
 * 在正文里画不了线，只能在清单里找到。
 *
 * 数据两层来源：文档自己记着的出处（doc.catalog，打开就有），和证据接口的证据链
 * （原式、代入式、输入、封存，点开才取）。接口取不到（老后端、没有运行 id 的预览）
 * 就只显示第一层，并照实说缺了什么。
 *
 * 只信封存链：正文是按成果上的 _evidence.doc_artifact 取的（成果可以改写、工件表可以
 * 事后插行），接口是从封存范围内的事件找的文档。两边不是同一份时，接口的封存状态和
 * 证据链说的是另一份报告——不能挂到正文上画成绿的，要醒目地说正文不是封存的那一份。
 *
 * 三种摆法：side 从右侧弹出（宽屏），drawer 从底部抽出（窄屏），inline 在 360px 的
 * 画布右栏里直接栏内展开。都不是模态的：开着面板照样能在正文里走。
 */

export type PanelMode = 'side' | 'drawer' | 'inline'
export type PanelView = { kind: 'seg'; id: string } | { kind: 'violations' }

export function EvidencePanel({ id, mode, doc, artifact, runId, view, onClose, onLocate, onViolations }: {
  id: string
  mode: PanelMode
  doc: EvidenceDocData
  /** 正文这份文档的工件 id：和证据接口报的封存文档比对，不是同一份就不信接口给的链 */
  artifact?: string
  runId?: string
  view: PanelView
  onClose: () => void
  /** 跳到正文里的某个片段（违规清单里的「定位」） */
  onLocate: (segId: string) => void
  onViolations: () => void
}) {
  const titleId = `${id}-title`
  const found = useMemo(() => (view.kind === 'seg' ? findSeg(doc, view.id) : null), [doc, view])
  const state = found ? segmentState(found.seg) : null
  const meta = state ? EVIDENCE_STATE[state] : null

  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (e.key !== 'Escape' || isComposing(e) || e.defaultPrevented) return
    e.preventDefault()
    // 画布上在 window 上听 Esc 的（收起检查器之类）不该跟着动
    e.stopPropagation()
    onClose()
  }

  const title = view.kind === 'violations' ? EVIDENCE_TEXT.violations : found?.seg.text ?? EVIDENCE_TEXT.panelTitle
  const frame = mode === 'side'
    ? 'ev-panel-side fixed bottom-0 right-0 top-0 z-40 flex flex-col border-l bg-panel shadow-elev-3'
    : mode === 'drawer'
      ? 'ev-panel-rise fixed bottom-0 left-0 right-0 z-40 flex flex-col rounded-t-lg border-t bg-panel shadow-elev-3'
      : 'ev-panel-rise my-2 flex flex-col rounded-lg border bg-panel'

  return (
    <section
      id={id}
      role="region"
      aria-labelledby={titleId}
      data-evidence-panel={mode}
      className={clsx(frame, 'text-xs')}
      style={mode === 'side' ? { width: 'min(380px, 100vw)' } : mode === 'drawer' ? { maxHeight: '65vh' } : undefined}
      onKeyDown={onKeyDown}
    >
      <header className="flex items-center gap-2 border-b px-3 py-2">
        {mode === 'inline' && (
          <button type="button" className="btn btn-xs btn-ghost -ml-1" onClick={onClose} aria-label={EVIDENCE_TEXT.back}>
            <ArrowLeft size={12} aria-hidden /> {EVIDENCE_TEXT.back}
          </button>
        )}
        {meta && (
          <span className="chip shrink-0" style={{ color: meta.color, borderColor: meta.color }} data-ev-badge={meta.code}>
            {meta.glyph && <span aria-hidden>{meta.glyph}</span>}{meta.label}
          </span>
        )}
        <h3 id={titleId} tabIndex={-1} className="mono min-w-0 flex-1 truncate text-sm font-semibold outline-none">
          {title}
        </h3>
        {mode !== 'inline' && (
          <button type="button" className="btn btn-xs btn-ghost -mr-1" onClick={onClose} aria-label={EVIDENCE_TEXT.close}
                  title={`${EVIDENCE_TEXT.close}（Esc）`}>
            <X size={13} aria-hidden />
          </button>
        )}
      </header>
      <div className={clsx('space-y-3 px-3 py-2.5 leading-relaxed', mode !== 'inline' && 'min-h-0 flex-1 overflow-y-auto')}>
        {view.kind === 'violations'
          ? <ViolationList doc={doc} onLocate={onLocate} />
          : found
            ? <SegmentBody doc={doc} artifact={artifact} runId={runId} seg={found.seg} unit={found.unit}
                           onViolations={onViolations} />
            : <p className="text-dim">{NONE}</p>}
      </div>
    </section>
  )
}

function findSeg(doc: EvidenceDocData, id: string): { seg: EvidenceSegment; unit: EvidenceUnit } | null {
  for (const block of doc.blocks ?? []) {
    for (const unit of block.units ?? []) {
      const seg = (unit.segments ?? []).find((s) => s.id === id)
      if (seg) return { seg, unit }
    }
  }
  return null
}

/** 面板里的一节：小标题 + 内容 */
function Part({ title, children, ...rest }: { title: string; children: ReactNode } & Record<`data-${string}`, string>) {
  return (
    <div {...rest}>
      <div className="mb-1 text-2xs font-medium text-faint">{title}</div>
      {children}
    </div>
  )
}

/** 值怎么写：数带千分位、不丢小数；拿不到写「—」 */
function valueText(v: unknown): string {
  if (v == null || v === '') return NONE
  if (typeof v === 'number') return Number.isFinite(v) ? v.toLocaleString('en-US', { maximumFractionDigits: 20 }) : String(v)
  if (typeof v === 'boolean') return v ? '是' : '否'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

function SegmentBody({ doc, artifact, runId, seg, unit, onViolations }: {
  doc: EvidenceDocData; artifact?: string; runId?: string; seg: EvidenceSegment; unit: EvidenceUnit
  onViolations: () => void
}) {
  const report = doc.node_id || undefined
  const key = runId ? segmentKey(runId, seg.id, report) : ''
  const slot = useEvidence((s) => (key ? s.segments[key] : undefined))
  const graph = useEvidence((s) => (runId ? s.graphs[runId] : undefined))
  const loadSegment = useEvidence((s) => s.loadSegment)
  const loadGraph = useEvidence((s) => s.loadGraph)
  useEffect(() => {
    if (runId) void loadSegment(runId, seg.id, report)
  }, [runId, seg.id, report, loadSegment])
  const answered = slot?.status === 'ok' ? slot.data : undefined
  // 接口答的是另一份报告（封存的那份不是正文这份）：它的链和封存状态一概不用
  const foreign = docForeign(answered, { artifact, node: report, seg })
  const detail = foreign ? undefined : answered
  // 片段接口没给封存状态（或者取不到）时，退回整次运行的证据图
  const needGraph = !!runId && !foreign && (slot?.status === 'error' || (slot?.status === 'ok' && !answered?.seal))
  useEffect(() => {
    if (needGraph && runId) void loadGraph(runId)
  }, [needGraph, runId, loadGraph])
  const graphData = needGraph && graph?.status === 'ok' ? graph.data : undefined
  // 证据图兜底时也得先认出正文这份文档在不在封存的报告里
  const inGraph = graphDoc(graphData, artifact)
  const docIssue: 'foreign' | 'tampered' | null = foreign || inGraph === 'foreign' ? 'foreign'
    : inGraph === 'tampered' ? 'tampered' : null

  const state = segmentState(seg)
  const cite = seg.cite
  const entry = cite?.alias ? doc.catalog?.[cite.alias] : undefined
  const chain: EvidenceStep[] = Array.isArray(detail?.chain) ? detail.chain : []
  const metricStep = chain.find((s) => s.step === 'metric')
  const inputStep = chain.find((s) => s.step === 'run_input')
  const isMetric = cite?.kind === 'metric' || entry?.kind === 'metric' || !!metricStep
  const isInput = cite?.kind === 'input' || entry?.kind === 'input' || !!inputStep
  const violations = (doc.violations ?? []).filter((v) => v.segment === seg.id)
  const seal: EvidenceSeal | undefined = docIssue ? undefined : detail?.seal ?? graphData?.seal
  const reason = reasonOf(seg) || detail?.note || ''
  const verdict = sealVerdict(seal, {
    foreign: !!docIssue,
    pending: !!runId && !seal && (slot?.status === 'loading' || graph?.status === 'loading'),
    noRun: !runId,
    docOnly: state !== 'deterministic',
    viaGraph: slot?.status === 'error',
  })
  const chainFailed = !runId ? EVIDENCE_TEXT.noRun
    : docIssue ? EVIDENCE_TEXT.chainForeign
    : slot?.status === 'error' ? EVIDENCE_TEXT.chainMissing : ''

  return (
    <>
      {docIssue && (
        // 正文不是封存的那一份：比「有出处」三个字重要得多，放在最前面
        <IntegrityList code="doc" items={[
          docIssue === 'tampered' ? EVIDENCE_TEXT.integrity.docHash : EVIDENCE_TEXT.integrity.doc,
          ...(foreign?.sealedText != null ? [EVIDENCE_TEXT.integrity.sealedText(foreign.sealedText)] : []),
        ]} />
      )}

      <Part title={EVIDENCE_TEXT.sentence} data-ev-sentence="">
        <Sentence unit={unit} active={seg.id} />
      </Part>

      {state === 'none' && (
        <Part title="为什么没有证据" data-ev-reason="">
          <p style={{ color: 'var(--st-waiting)' }}>{reason}</p>
          {/* 违规的原话多半是「引用 … 解析不了：<原因>」，和上面那句重复的不再列；裸数字那条说的是怎么改，留着 */}
          {violations.filter((v) => v.message && !(reason && v.message.includes(reason))).map((v, i) => (
            <p key={i} className="mt-1 text-dim">{v.message}</p>
          ))}
          {seg.ref && <p className="mono mt-1 text-2xs text-faint">[[{seg.ref}]]</p>}
        </Part>
      )}

      {isMetric && (
        <MetricPart step={metricStep} entry={entry} chain={chain} runId={runId} seal={seal}
                    pending={!!runId && !docIssue && (!slot || slot.status === 'loading')}
                    failed={chainFailed}
                    resolved={state === 'deterministic'} />
      )}

      {isInput && !isMetric && (
        <Part title={EVIDENCE_TEXT.input} data-ev-input-step="">
          <p className="mono">
            {inputStep?.field ?? entry?.locator?.field ?? cite?.locator?.field ?? cite?.ref} ={' '}
            <span className="tnum">{valueText(inputStep?.value ?? entry?.value ?? cite?.value)}</span>
          </p>
          <Integrity step={inputStep} seal={seal} />
        </Part>
      )}

      {state === 'deterministic' && !isMetric && !isInput && (
        <p className="text-dim">{sourceOf(seg, doc) || detail?.note}</p>
      )}

      <Part title={EVIDENCE_TEXT.seal} data-ev-seal="">
        <SealLine status={verdict.status} label={verdict.label} />
      </Part>

      {!!doc.violations?.length && (
        <button type="button" className="btn btn-xs btn-ghost -ml-1" onClick={onViolations} data-ev-open-violations="">
          <ListChecks size={11} aria-hidden /> {EVIDENCE_TEXT.violations}（{formatNumber(doc.violations.length)}）
        </button>
      )}
    </>
  )
}

/** 所在的句子：点开的那一段描底，句子里的数字加粗 */
function Sentence({ unit, active }: { unit: EvidenceUnit; active: string }) {
  return (
    <p className="leading-relaxed">
      {(unit.segments ?? []).filter((s) => s.kind !== 'structural').map((s) => {
        const state = segmentState(s)
        const on = s.id === active
        const number = s.kind === 'number' || s.kind === 'value'
        return (
          <span key={s.id}
                className={clsx(number && 'font-semibold tnum', on && 'rounded-sm px-0.5')}
                style={on && state ? { background: EVIDENCE_STATE[state].soft, color: 'var(--text)' } : undefined}
                data-ev-here={on ? '' : undefined}>
            {s.text}
          </span>
        )
      })}
    </p>
  )
}

/** 指标步骤：名称、值、口径与版本、原式、代入式（输入标签）、复算 */
function MetricPart({ step, entry, chain, runId, seal, pending, failed, resolved }: {
  step?: EvidenceStep
  entry?: Record<string, any>
  chain: EvidenceStep[]
  runId?: string
  /** 封存状态：逐项复核里「在不在封存范围内」只在封存完好时单独报 */
  seal?: EvidenceSeal
  pending: boolean
  /** 证据链为什么没有：没有运行 id、接口取不到、接口答的是另一份报告 */
  failed: string
  /** 片段的引用解析成功了（有出处）；解析不了的也画这一节，好说清缺的是哪个指标 */
  resolved: boolean
}) {
  const name = step?.name ?? entry?.name ?? step?.metric ?? entry?.locator?.metric ?? NONE
  const value = step?.value !== undefined ? step.value : entry?.value
  const rendered = step?.rendered ?? entry?.rendered
  const caliber = step?.caliber ?? entry?.caliber
  const version = step?.version ?? entry?.version
  const artifact = step?.artifact ?? entry?.artifact
  const inputs: EvidenceInput[] = step?.inputs?.length
    ? step.inputs
    : chain.filter((s) => s.step === 'input').map((s) => s as EvidenceInput)
  // 口径卡里 rendered 为「—」有两种：value 为 null 是缺输入，不为 null 是有值但按格式显示不出来
  const dash = rendered === NONE || entry?.rendered === NONE
  const missing = entry?.status === 'missing_input' || (value == null && (dash || !resolved))
  const unshowable = !missing && dash && value != null

  return (
    <Part title={EVIDENCE_TEXT.metric} data-ev-metric="">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="font-medium">{name}</span>
        {resolved && rendered && <span className="mono tnum text-sm" data-ev-value="">{rendered}</span>}
      </div>
      {(caliber || version || artifact) && (
        <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-2xs text-dim">
          {(caliber || version) && <span>口径卡「{caliber || NONE}」<span className="mono">{version}</span></span>}
          {artifact && <span className="mono text-faint" title={`口径卡工件 ${artifact}`}>工件 {shortId(artifact)}</span>}
        </div>
      )}
      <Integrity step={step} seal={seal} />
      {missing && <p className="mt-1" style={{ color: 'var(--st-waiting)' }} data-ev-missing="">{EVIDENCE_TEXT.missingValue}</p>}
      {unshowable && (
        <p className="mt-1" style={{ color: 'var(--st-waiting)' }} data-ev-unshowable="">
          {EVIDENCE_TEXT.unshowable(valueText(value))}
        </p>
      )}

      {step ? (
        <dl className="mt-2 space-y-1.5">
          <div>
            <dt className="text-2xs text-faint">{EVIDENCE_TEXT.expression}</dt>
            <dd><code className="mono block whitespace-pre-wrap break-all rounded bg-bg px-1.5 py-1 text-2xs" data-ev-expression="">
              {step.expression || NONE}
            </code></dd>
          </div>
          <div>
            <dt className="text-2xs text-faint">{EVIDENCE_TEXT.substituted}</dt>
            <dd>
              <code className="mono block whitespace-pre-wrap break-all rounded bg-bg px-1.5 py-1 text-2xs" data-ev-substituted="">
                {step.substituted || NONE}
              </code>
              {!!inputs.length && (
                <ul className="mt-1 flex flex-wrap gap-1" aria-label={EVIDENCE_TEXT.inputs}>
                  {inputs.map((inp, i) => <InputChip key={`${inp.path}-${i}`} input={inp} />)}
                </ul>
              )}
            </dd>
          </div>
          <div>
            <dt className="text-2xs text-faint">{EVIDENCE_TEXT.recompute}</dt>
            <dd data-ev-recompute={step.recompute_ok == null ? 'none' : step.recompute_ok ? 'ok' : 'bad'}
                style={{ color: step.recompute_ok == null ? 'var(--st-cancelled)'
                  : step.recompute_ok ? 'var(--st-done)' : 'var(--st-failed)' }}>
              {step.recompute_ok == null ? EVIDENCE_TEXT.recomputeNone
                : step.recompute_ok ? EVIDENCE_TEXT.recomputeOk : EVIDENCE_TEXT.recomputeBad}
            </dd>
          </div>
        </dl>
      ) : resolved && (
        <p className="mt-1.5 text-2xs text-faint" data-ev-chain={pending ? 'loading' : 'missing'}>
          {pending ? EVIDENCE_TEXT.chainPending : failed || (runId ? EVIDENCE_TEXT.chainMissing : EVIDENCE_TEXT.noRun)}
        </p>
      )}
    </Part>
  )
}

/**
 * 证据接口逐项复核的结果：eid 能重算、工件哈希复验、重新渲染一致、在封存范围内。
 * 全过的时候不说（正常态安静）；哪一项明确没过就用失败色写出来——这时候这件证据
 * 不能信，比「有出处」三个字重要得多。「不在封存范围内」只在封存完好时报：没封存、
 * 封存核对失败时后端把每一步都记成 sealed=false，那由封存那一行说，不能说成「事后补进来的」
 */
function Integrity({ step, seal }: { step?: EvidenceStep; seal?: EvidenceSeal }) {
  const bad = integrityFailures(step, seal)
  if (!bad.length) return null
  return <IntegrityList code={bad.join(' ')} items={bad.map((k) => EVIDENCE_TEXT.integrity[k])} />
}

function IntegrityList({ code, items }: { code: string; items: string[] }) {
  return (
    <ul className="mt-1.5 space-y-0.5 rounded border px-2 py-1" data-ev-integrity={code}
        style={{ color: 'var(--st-failed)', borderColor: 'var(--st-failed)', background: 'var(--st-failed-soft)' }}>
      {items.map((t) => <li key={t}>{t}</li>)}
    </ul>
  )
}

/** 「45,678.5 ← vars.kpi.gmv」：值从哪条路径取的。缺的写「—」、标成提醒色 */
function InputChip({ input }: { input: EvidenceInput }) {
  const missing = input.status === 'missing' || input.value == null
  const from = [input.node_id && `来自节点 ${input.node_id}`, input.via && `（${input.via}）`, input.role && ` · ${input.role}`]
    .filter(Boolean).join('')
  return (
    <li className="chip mono tnum max-w-full" data-ev-input={input.path ?? ''}
        style={missing ? { color: 'var(--st-waiting)', borderColor: 'var(--st-waiting)' } : { color: 'var(--text)' }}
        title={from || undefined}>
      <span className="truncate">{valueText(input.value)} ← {input.path ?? NONE}{missing ? '（缺）' : ''}</span>
    </li>
  )
}

/**
 * 封存状态：StatusBadge 的剪影 + 一句话。说什么由 lib/evidence 的 sealVerdict 定（顺序、
 * 正文不是封存那份时的失败态都在那里）；没有证据的片段封存的只是报告文档本身，
 * 不能说成「这个数已封存」
 */
function SealLine({ status, label }: { status: SealStatus; label: string }) {
  return (
    <p className="flex items-center gap-1.5" data-ev-seal-state={status}>
      <StatusBadge status={status} size={12} decorative animate={false} />
      <span className={status === 'idle' ? 'text-dim' : undefined}>{label}</span>
    </p>
  )
}

const CODE_LABEL: Record<string, string> = {
  uncited_number: '裸数字',
  unresolved_ref: '引用解析不了',
}

/** 违规清单：报告撰写节点核对出来的全部问题，画得出线的能定位，画不出的说清在哪 */
function ViolationList({ doc, onLocate }: { doc: EvidenceDocData; onLocate: (segId: string) => void }) {
  const list: EvidenceViolation[] = doc.violations ?? []
  if (!list.length) return <p className="text-dim">{EVIDENCE_TEXT.allCited(docNumbers(doc))}</p>
  return (
    <>
      <p className="text-2xs text-faint">{EVIDENCE_TEXT.violationsHint}</p>
      <ul className="space-y-2" data-ev-violations="">
        {list.map((v, i) => {
          const seg = locatable(doc, v)
          return (
            <li key={i} className="rounded border px-2 py-1.5" data-ev-violation={v.code}
                data-ev-locatable={seg ? 'yes' : 'no'}>
              <div className="flex items-center gap-2">
                <span className="mono font-semibold">{v.text ?? (v.ref ? `[[${v.ref}]]` : NONE)}</span>
                <span className="chip" style={{ color: 'var(--st-waiting)' }}>{CODE_LABEL[v.code] ?? '文档核对没通过'}</span>
                <span className="flex-1" />
                {seg && (
                  <button type="button" className="btn btn-xs" onClick={() => onLocate(seg.id)}>定位</button>
                )}
              </div>
              {v.message && <p className="mt-1 text-dim">{v.message}</p>}
              {v.context && <p className="mono mt-0.5 text-2xs text-faint [overflow-wrap:anywhere]">「{v.context.trim()}」</p>}
              {!seg && (
                <p className="mt-0.5 text-2xs" style={{ color: 'var(--st-waiting)' }}>
                  {v.segment ? EVIDENCE_TEXT.structural : EVIDENCE_TEXT.noSegment}
                </p>
              )}
            </li>
          )
        })}
      </ul>
    </>
  )
}

const docNumbers = (doc: EvidenceDocData): number => doc.stats?.numbers ?? 0
