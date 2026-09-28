import {
  useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'
import { ArrowLeft, CornerDownRight, ListChecks, ShieldCheck, X } from 'lucide-react'
import clsx from 'clsx'
import { StatusBadge, isComposing } from '../components/ui'
import {
  EVIDENCE_STATE, caliberSourceText, caliberUpgradeText, docForeign, evidenceValue as valueText, graphDoc, inputSource,
  integrityFailures, locatable, locatorText, notableInput, queryOf, queryWindow, reasonOf, sealVerdict, segmentState,
  sourceOf, type SealStatus, type SourceTone,
} from '../lib/evidence'
import { formatNumber, NONE, shortId } from '../lib/format'
import { EVIDENCE_TEXT } from '../lib/terms'
import { useCatalog } from '../store/catalog'
import { segmentKey, useEvidence } from '../store/evidence'
import type {
  EvidenceDocData, EvidenceInput, EvidenceSeal, EvidenceSegment, EvidenceStep, EvidenceUnit, EvidenceViolation,
} from '../types'
import { ArtifactViewer, ResultTable } from './AssistantStream'
import { CopyChip } from './Markdown'

/**
 * 证据面板：点开报告里的一个片段，看它从哪来。
 *
 * 展示：片段和它所在的句子；指标步骤——名称、值、口径与版本（钉在别的工作流上的写明来源
 * 和升版处置）、原式、代入式（每个输入是「值 ← 路径」的小标签，点一下跳到对应的查询）、
 * 复算结果；输入来源——agent 字段、cell() 取数和快照核对得上没有，代码节点的数醒目提醒；
 * 查询步骤——SQL、被引用的行和格高亮的结果窗口、打开完整快照、遮罩；封存状态。没有证据
 * 的片段说清为什么（裸数字、引用解析不了），另有一份违规清单：列表序号、代码块标签里的数字
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
            ? <SegmentBody panelId={id} doc={doc} artifact={artifact} runId={runId} seg={found.seg} unit={found.unit}
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

/** 定位到的那一步描一下边（CSS 的 [data-flash]，减少动效时由全局规则停掉） */
function flashOnce(el: HTMLElement) {
  el.dataset.flash = 'focus'
  setTimeout(() => { if (el.dataset.flash === 'focus') delete el.dataset.flash }, 1600)
}

function SegmentBody({ panelId, doc, artifact, runId, seg, unit, onViolations }: {
  panelId: string
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
  const chain: EvidenceStep[] = Array.isArray(detail?.chain) ? detail.chain.filter((s) => s && typeof s === 'object') : []
  const metricStep = chain.find((s) => s.step === 'metric')
  const inputStep = chain.find((s) => s.step === 'run_input')
  const queries = chain.filter((s) => s.step === 'query')
  const isMetric = cite?.kind === 'metric' || entry?.kind === 'metric' || !!metricStep
  const isInput = cite?.kind === 'input' || entry?.kind === 'input' || !!inputStep
  // 报告直接引用的查询单元格（[[v:Q3.r5.amount]]、整表里的一格）：证据链就是那一次查询
  const isCell = !isMetric && !isInput && (cite?.kind === 'cell' || entry?.kind === 'query' || !!queries.length)
  const inputs: EvidenceInput[] = metricStep?.inputs?.length
    ? metricStep.inputs
    : chain.filter((s) => s.step === 'input').map((s) => s as EvidenceInput)
  const masked = (Array.isArray(detail?.redacted?.columns) ? detail.redacted.columns : []).map(String).filter(Boolean)
  const workflows = useCatalog((s) => s.workflows)
  const workflowName = (id?: string) => (id ? workflows.find((w) => w.id === id)?.name : undefined)
  // 输入标签、输入来源那一行点一下：焦点跳到对应的查询步骤，描一下边
  const queryId = (i: number) => `${panelId}-q${i}`
  const jumpTo = useCallback((i: number) => {
    const part = document.getElementById(`${panelId}-q${i}`)
    const head = part?.querySelector<HTMLElement>('[data-ev-query-head]')
    if (!part || !head) return
    head.focus({ preventScroll: true })
    head.scrollIntoView({ block: 'nearest' })
    flashOnce(part)
  }, [panelId])
  const linkOf = (inp: EvidenceInput) => {
    const i = queryOf(inp, queries)
    return i >= 0 ? { index: i, alias: queries[i].alias ?? '', go: () => jumpTo(i) } : undefined
  }
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
        <MetricPart step={metricStep} entry={entry} inputs={inputs} runId={runId} seal={seal}
                    pending={!!runId && !docIssue && (!slot || slot.status === 'loading')}
                    failed={chainFailed}
                    resolved={state === 'deterministic'}
                    linkOf={linkOf}
                    workflowName={workflowName(pinnedWorkflow(metricStep))} />
      )}

      {isMetric && inputs.some(notableInput) && (
        <SourcesPart inputs={inputs.filter(notableInput)} linkOf={linkOf} />
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

      {isCell && !queries.length && state === 'deterministic' && (
        // 接口没给查询步骤（取不到、老后端、正在取）：只说文档目录里记着的那次查询，不画假的行。
        // 解析不了的单元格引用（行越界、没有这一列、受管没声明 cells）接口本来就不给链，上面已经说了原因，
        // 再摆一块「这一次没取到」就像是接口出了错
        <QueryFallback entry={entry} alias={cite?.alias} where={locatorText(cite?.locator)}
                       pending={!!runId && !docIssue && (!slot || slot.status === 'loading')}
                       failed={chainFailed} />
      )}

      {queries.map((q, i) => (
        <QueryPart key={`${q.artifact ?? ''}-${i}`} id={queryId(i)} step={q} seal={seal} masked={masked} />
      ))}

      {state === 'deterministic' && !isMetric && !isInput && !isCell && (
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

      {!!masked.length && (
        // 用户拍板：界面上写明遮罩不是安全边界。放在最后，读完证据再看到这句
        <p className="border-t pt-2 text-2xs leading-relaxed text-faint" data-ev-mask-note="">
          {EVIDENCE_TEXT.maskNote(masked)}
        </p>
      )}
    </>
  )
}

type InputLink = { index: number; alias: string; go: () => void } | undefined

/** 口径卡钉住的上游工作流 id：方案写在 caliber_from，接口给在 source 上 */
function pinnedWorkflow(step?: EvidenceStep): string | undefined {
  const from = step?.caliber_from ?? (step?.source && typeof step.source === 'object' ? step.source : undefined)
  return from?.workflow_id || undefined
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

/** 指标步骤：名称、值、口径与版本（来源、升版处置）、原式、代入式（输入标签）、复算 */
function MetricPart({ step, entry, inputs, runId, seal, pending, failed, resolved, linkOf, workflowName }: {
  step?: EvidenceStep
  entry?: Record<string, any>
  inputs: EvidenceInput[]
  runId?: string
  /** 封存状态：逐项复核里「在不在封存范围内」只在封存完好时单独报 */
  seal?: EvidenceSeal
  pending: boolean
  /** 证据链为什么没有：没有运行 id、接口取不到、接口答的是另一份报告 */
  failed: string
  /** 片段的引用解析成功了（有出处）；解析不了的也画这一节，好说清缺的是哪个指标 */
  resolved: boolean
  /** 输入对应的查询步骤：有的话输入标签是按钮，点一下跳过去 */
  linkOf: (input: EvidenceInput) => InputLink
  /** 钉住的上游工作流在目录里的名字（接口没给名字时用） */
  workflowName?: string
}) {
  const name = step?.name ?? entry?.name ?? step?.metric ?? entry?.locator?.metric ?? NONE
  const value = step?.value !== undefined ? step.value : entry?.value
  const rendered = step?.rendered ?? entry?.rendered
  const caliber = step?.caliber ?? entry?.caliber
  const version = step?.version ?? entry?.version
  const artifact = step?.artifact ?? entry?.artifact
  // 钉在别的工作流上的口径卡：写明来自哪一版，上游有新版本时写处置
  const pinned = step?.caliber_from ?? (step?.source && typeof step.source === 'object' ? step.source : undefined)
    ?? entry?.caliber_from ?? entry?.source
  const from = caliberSourceText(caliber, version, typeof pinned === 'object' ? pinned : undefined, workflowName)
  const upgrade = caliberUpgradeText(step?.caliber_upgrade ?? entry?.caliber_upgrade)
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
      {(caliber || version || artifact || from) && (
        <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-2xs text-dim">
          {from
            ? <span data-ev-caliber-from="">{from}</span>
            : (caliber || version) && <span>口径卡「{caliber || NONE}」<span className="mono">{version}</span></span>}
          {artifact && <span className="mono text-faint" title={`口径卡工件 ${artifact}`}>工件 {shortId(artifact)}</span>}
        </div>
      )}
      {upgrade && (
        <p className="mt-0.5 text-2xs" style={{ color: 'var(--st-waiting)' }} data-ev-caliber-upgrade="">{upgrade}</p>
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
                  {inputs.map((inp, i) => <InputChip key={`${inp.path}-${i}`} input={inp} link={linkOf(inp)} />)}
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

/**
 * 「45,678.5 ← vars.kpi.gmv」：值从哪条路径取的。缺的写「—」、标成提醒色。
 * 能对到链里某一次查询的（agent 字段、cell() 取数）是按钮：点一下跳到那个查询步骤
 */
function InputChip({ input, link }: { input: EvidenceInput; link?: InputLink }) {
  const missing = input.status === 'missing' || input.value == null
  const from = [input.node_id && `来自节点 ${input.node_id}`, input.via && `（${input.via}）`, input.role && ` · ${input.role}`]
    .filter(Boolean).join('')
  const style = missing ? { color: 'var(--st-waiting)', borderColor: 'var(--st-waiting)' } : { color: 'var(--text)' }
  const body = <span className="truncate">{valueText(input.value)} ← {input.path ?? NONE}{missing ? '（缺）' : ''}</span>
  return (
    <li className="max-w-full">
      {link ? (
        <button type="button" className="chip mono tnum max-w-full transition-colors hover:bg-hover" style={style}
                data-ev-input={input.path ?? ''} title={`${from ? `${from} · ` : ''}${EVIDENCE_TEXT.gotoQuery(link.alias)}`}
                aria-label={`${valueText(input.value)} ← ${input.path ?? NONE}，${EVIDENCE_TEXT.gotoQuery(link.alias)}`}
                onClick={link.go}>
          {body}<CornerDownRight size={9} className="shrink-0 opacity-60" aria-hidden />
        </button>
      ) : (
        <span className="chip mono tnum max-w-full" data-ev-input={input.path ?? ''} style={style} title={from || undefined}>
          {body}
        </span>
      )}
    </li>
  )
}

const TONE: Record<SourceTone, { color: string; border: string; background?: string }> = {
  ok: { color: 'var(--st-done)', border: 'var(--border)' },
  warn: { color: 'var(--st-waiting)', border: 'var(--border)' },
  // 计算角色的代码节点：沙箱里的算术绕开了口径卡，比别的提醒都醒目
  alert: { color: 'var(--st-failed)', border: 'var(--st-failed)', background: 'var(--st-failed-soft)' },
  muted: { color: 'var(--text-dim)', border: 'var(--border)' },
}

/**
 * 输入来源：每个能核对到快照的输入（agent 字段、cell() 取数）、代码节点的输入、缺的输入各一行——
 * 路径、从哪个节点哪一格来、核对结果。能对到查询步骤的给「看查询 Qn」
 */
function SourcesPart({ inputs, linkOf }: { inputs: EvidenceInput[]; linkOf: (input: EvidenceInput) => InputLink }) {
  return (
    <Part title={EVIDENCE_TEXT.sources} data-ev-sources="">
      <ul className="space-y-1.5">
        {inputs.map((inp, i) => {
          const said = inputSource(inp)
          const tone = TONE[said.tone]
          const link = linkOf(inp)
          // 查询编号写报告目录里的全局编号（接口补的 query、对上的查询步骤）；agent 字段自己的 ref 是
          // 那个节点内部的编号，和面板里的「查询 Qn」不是一回事，不拿来写
          const alias = link?.alias || inp.query || (typeof inp.cell === 'string' ? inp.cell.split('.')[0] : '')
          const where = [
            inp.node_id && `节点 ${inp.node_id}`,
            inp.field && (inp.via === 'agent_field' ? `字段 ${inp.field}` : inp.field),
            [alias, locatorText(inp.locator)].filter(Boolean).join(' · '),
          ].filter(Boolean).join(' · ')
          return (
            <li key={`${inp.path}-${i}`} className="rounded border px-2 py-1"
                style={{ borderColor: tone.border, background: tone.background }}
                data-ev-source={inp.path ?? ''} data-ev-source-status={inp.status ?? ''}
                data-ev-source-role={inp.via === 'code' ? inp.role || 'compute' : undefined}>
              <div className="flex items-baseline gap-2">
                <span className="mono min-w-0 flex-1 truncate" title={inp.path}>{inp.path ?? NONE}</span>
                {link && (
                  <button type="button" className="btn btn-xs btn-ghost -mr-1 shrink-0" onClick={link.go} data-ev-goto="">
                    <CornerDownRight size={10} aria-hidden /> {EVIDENCE_TEXT.gotoQuery(link.alias)}
                  </button>
                )}
              </div>
              {where && <div className="mono text-2xs text-faint [overflow-wrap:anywhere]">{where}</div>}
              {said.text && <p className="mt-0.5" style={{ color: tone.color }}>{said.text}</p>}
              {said.reason && <p className="mt-0.5 text-2xs text-dim">{said.reason}</p>}
            </li>
          )
        })}
      </ul>
    </Part>
  )
}

/**
 * 查询步骤：SQL（带复制）、结果窗口（被引用的行和格高亮）、窗口说明、打开完整快照、复核。
 *
 * 接口只给被引用的行加前后各 2 行，窗口没盖住整份快照时写明；完整快照走工件接口（取回时
 * 复验哈希），复用工件查看。被遮罩的列在表里写「已遮罩」。查询快照哈希对不上时用失败色写出来：
 * 这时候这些行不能当证据，比「有出处」重要得多
 */
function QueryPart({ id, step, seal, masked }: { id: string; step: EvidenceStep; seal?: EvidenceSeal; masked: string[] }) {
  const win = useMemo(() => queryWindow(step), [step])
  const [open, setOpen] = useState(false)
  const scroller = useRef<HTMLDivElement>(null)
  const alias = step.alias ?? ''
  const hidden = [...new Set([...masked, ...(Array.isArray(step.masked) ? step.masked.map(String) : [])])]
    .filter((c) => win.columns.includes(c))
  const bad = integrityFailures(step, seal)
  // 完整快照只在这一步自己画得出行、复核也都过了时给：快照不在封存范围里（接口一行不给，工件 id 照样带着）、
  // 哈希对不上时，按钮一点就绕过证据接口把整份快照打开，还挂着「哈希已校验」
  const snapshot = !!step.artifact && step.sealed !== false && step.hash_ok !== false && !bad.length && win.rows.length > 0
  // 知道每一行在快照里是第几行时，最前面加一列行号：窗口不连续（被引用的行隔得远）也看得出跳过了哪几行
  const table = useMemo(() => (win.index
    ? { columns: [ROW_NO, ...win.columns], rows: win.rows.map((r, i) => [win.index![i] + 1, ...r]), truncated: false }
    : { columns: win.columns, rows: win.rows, truncated: false }), [win])
  // 被引用的格可能在右边几列：窄栏里表格横向滚到它，不让人以为没高亮
  useLayoutEffect(() => {
    const box = scroller.current
    const cell = box?.querySelector<HTMLElement>('td[data-highlight="cell"]')
    const inner = cell?.closest<HTMLElement>('.overflow-x-auto')
    if (!cell || !inner) return
    const left = cell.offsetLeft
    if (left < inner.scrollLeft || left + cell.offsetWidth > inner.scrollLeft + inner.clientWidth) {
      inner.scrollLeft = Math.max(0, left - 8)
    }
  }, [win])
  return (
    // 从输入标签跳过来时描一下边（flashOnce 设 data-flash）：静态描边，减少动效时照样看得见
    <div id={id} data-ev-query={step.artifact ?? ''} data-ev-query-alias={alias}
         className="rounded data-[flash]:outline data-[flash]:outline-solid data-[flash]:outline-2 data-[flash]:outline-offset-2 data-[flash=focus]:outline-[color:var(--accent)]">
      <div className="mb-1 flex items-center gap-2">
        {/* 键盘跳过来时焦点落在这里：得看得见落在哪（不能 outline-none） */}
        <span tabIndex={-1} data-ev-query-head=""
              className="min-w-0 flex-1 truncate rounded-sm text-2xs font-medium text-faint focus:outline-none focus-visible:outline focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[color:var(--accent)]">
          {EVIDENCE_TEXT.query(alias)}
          {step.tool && <span className="mono ml-1.5 font-normal">{step.tool}</span>}
        </span>
        {step.sql && <CopyChip label={EVIDENCE_TEXT.copySql} text={() => step.sql ?? ''} />}
      </div>
      {step.sql && (
        <pre className="mono mb-1.5 max-h-28 overflow-auto whitespace-pre-wrap rounded bg-bg px-1.5 py-1 text-2xs leading-relaxed text-dim [overflow-wrap:anywhere]"
             data-ev-sql="">
          {step.sql}
        </pre>
      )}
      {bad.length > 0 && (
        <IntegrityList code={bad.map((k) => (k === 'hash' ? 'query-hash' : k)).join(' ')}
                       items={bad.map((k) => (k === 'hash' ? EVIDENCE_TEXT.queryHash : EVIDENCE_TEXT.integrity[k]))} />
      )}
      {win.columns.length > 0 && win.rows.length > 0 && (
        <div ref={scroller} className={clsx(bad.length > 0 && 'mt-1.5')}>
          <ResultTable table={table} full title={EVIDENCE_TEXT.query(alias)}
                       highlight={win.marks.length || win.cells?.length ? { rows: win.marks, cols: win.cols, cells: win.cells } : undefined}
                       masked={hidden} />
        </div>
      )}
      {!win.rows.length && (
        // 没有行：快照不在封存范围里、哈希对不上、取不回来。接口说了原因就照原话
        <p className="text-2xs text-dim" data-ev-query-note="">{step.note || EVIDENCE_TEXT.queryMissing}</p>
      )}
      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs text-faint">
        {win.windowed && win.total != null && (
          <span data-ev-window="">
            {EVIDENCE_TEXT.windowed}（{win.span}，共 {formatNumber(win.total)} 行）
          </span>
        )}
        {step.window_truncated && <span style={{ color: 'var(--st-waiting)' }}>{EVIDENCE_TEXT.windowCut(win.rows.length)}</span>}
        {step.truncated && <span style={{ color: 'var(--st-waiting)' }}>{EVIDENCE_TEXT.truncatedSnapshot}</span>}
        {/* 数据源改名或删掉了：遮罩按查询当时记下的那份处理，行照样给。这句要看得见——
            现在的数据源设置已经管不到这份快照了 */}
        {step.mask_note && (
          <span className="[overflow-wrap:anywhere]" style={{ color: 'var(--st-waiting)' }} data-ev-query-mask-note="">{step.mask_note}</span>
        )}
        <span className="flex-1" />
        {snapshot && (
          <button type="button" className="inline-flex items-center gap-1 rounded px-1 transition-colors hover:bg-hover hover:text-fg"
                  onClick={() => setOpen(true)} data-ev-snapshot="">
            <ShieldCheck size={10} aria-hidden /> {EVIDENCE_TEXT.openSnapshot}
          </button>
        )}
      </div>
      {/* 弹窗挂到 body 上：侧边面板是 fixed 的一层，里面的遮罩盖不住整页 */}
      {/* 完整快照里同样遮掉这几列：工件接口还是原值（面板底部写明了），面板自己的入口不多暴露 */}
      {open && snapshot && step.artifact && createPortal(
        <ArtifactViewer id={step.artifact} title={EVIDENCE_TEXT.snapshotTitle(alias)} masked={hidden} onClose={() => setOpen(false)} />,
        document.body,
      )}
    </div>
  )
}

/** 窗口表格最前面那一列：快照里的行号，从 1 数 */
const ROW_NO = '#'

/** 接口没给查询步骤时：文档目录里记着的那次查询（编号、工具、行数），并照实说行没取到 */
function QueryFallback({ entry, alias, where, pending, failed }: {
  entry?: Record<string, any>; alias?: string; where: string; pending: boolean; failed: string
}) {
  const rows = typeof entry?.rows === 'number' ? `${formatNumber(entry.rows)} 行` : ''
  return (
    <Part title={EVIDENCE_TEXT.query(alias ?? entry?.alias ?? '')} data-ev-query-missing={pending ? 'loading' : 'missing'}>
      <p className="mono text-2xs text-dim [overflow-wrap:anywhere]">
        {[entry?.tool ?? entry?.source, rows, where].filter(Boolean).join(' · ') || NONE}
      </p>
      <p className="mt-1 text-2xs text-faint">
        {pending ? EVIDENCE_TEXT.queryPending : failed === EVIDENCE_TEXT.chainMissing ? EVIDENCE_TEXT.queryMissing : failed || EVIDENCE_TEXT.queryMissing}
      </p>
    </Part>
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
