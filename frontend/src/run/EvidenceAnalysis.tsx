import { useState, type ReactNode } from 'react'
import { ChevronRight } from 'lucide-react'
import { evidenceValue as valueText } from '../lib/evidence'
import { NONE, shortId } from '../lib/format'
import { EVIDENCE_TEXT } from '../lib/terms'
import { ANALYSIS_TEXT } from '../lib/termsAnalysis'
import type {
  AnalysisProbe, AnalysisScope, AnalysisScopeItem, EvidenceAnalysisStep, EvidenceComparability, EvidenceSeal, EvidenceViolation,
} from '../types'
import { ResultTable } from './AssistantStream'
import { useEvidenceHost } from './evidenceHost'

/*
 * 证据面板、正文里分析结论的几块（docs/design/analysis-round/00-总方案.md §3.3、§3.6）：
 *
 * - 分析结论的出处链：判定（判定语、判定标准、点名的段、附带说明、算不了的原因、这一处引用的是哪一项、谁做的、Agent 写的假设）
 *   → 方法（方法与版本、参数、实际用的阈值、现查结果）→ 计算（关键数的代入式、具名的数、结果表）→ 输入（每份一步：角色、落到哪个
 *   Q、用到的列和行、总数那一格和闭合核对、下钻的上一层）→ 复算 → 封存。步骤、说法照导出页（backend/app/report_export/runtime.js
 *   的 analysisPart…analysisSealPart）：同一条链两处说法一样。输入快照的查询步骤由证据面板照常画（QueryPart），这里只画分析的几步
 * - 口径卡指标上的两期可比性：判定、判定语、原因，展开看那份可比性结论的整条链
 * - 正文里的说法违规：判断词未挂分析结论、判断与分析结论不符、因果说法没有依据，原话照服务端
 * - 报告末尾系统写的「分析范围」：现象、拆过、排除、算不了、没拆、检验，编号点得开
 *
 * 界面层不放逻辑：判定语、判定标准、原因、附带说明、代入式、闭合核对的数、复算的那一句都是服务端写好的字，这里只排版。
 */

const E = ANALYSIS_TEXT.evidence

/** 面板里的一节：小标题 + 内容（同 EvidencePanel 的 Part） */
function Part({ title, children, ...rest }: { title: ReactNode; children: ReactNode } & Record<`data-${string}`, string>) {
  return (
    <div {...rest}>
      <div className="mb-1 text-2xs font-medium text-faint">{title}</div>
      {children}
    </div>
  )
}

/** 一组「名称 · 值」 */
function Rows({ rows }: { rows: ([string, ReactNode] | null | false | undefined)[] }) {
  const shown = rows.filter((r): r is [string, ReactNode] => !!r)
  if (!shown.length) return null
  return (
    <dl className="space-y-1">
      {shown.map(([k, v]) => (
        <div key={k} className="flex gap-2">
          <dt className="w-20 shrink-0 text-2xs text-faint">{k}</dt>
          <dd className="min-w-0 flex-1 [overflow-wrap:anywhere]">{v}</dd>
        </div>
      ))}
    </dl>
  )
}

/** 逐项复核出来的问题：正常时不说，出问题才醒目（同 EvidencePanel 的 IntegrityList） */
function Problems({ code, items }: { code: string; items: string[] }) {
  if (!items.length) return null
  return (
    <ul className="mt-1.5 space-y-0.5 rounded border px-2 py-1" data-ev-integrity={code}
        style={{ color: 'var(--st-failed)', borderColor: 'var(--st-failed)', background: 'var(--st-failed-soft)' }}>
      {items.map((t) => <li key={t}>{t}</li>)}
    </ul>
  )
}

const subjectsText = (subjects: unknown): string => (Array.isArray(subjects)
  ? subjects.map((s) => (Array.isArray(s) ? s.map((x) => valueText(x)).join(' · ') : valueText(s))).join('、') : '')

/** 现查结果的一条：有 expr 的是按运行当天现算的界，其余是一列的实际范围；现查到的范围最早、最晚都空是这一列一个值都没有 */
function probeText(p: AnalysisProbe): string {
  const how = `（${E.probeBasis[String(p.basis ?? '')] ?? p.basis ?? NONE}${p.at ? ` · ${p.at}` : ''}）`
  if (typeof p.expr === 'string' && p.expr) return `${E.probeBound(p.expr, p.value || NONE)}${how}`
  const where = `${p.table || NONE}.${p.column || NONE}`
  const empty = p.basis === 'live' && !p.min && !p.max
  return `${where}：${empty ? E.probeEmpty : E.probeRange(p.min || NONE, p.max || NONE)}${how}`
}

function NodeName({ id, label }: { id?: string | null; label?: string | null }) {
  const host = useEvidenceHost()
  if (!id) return null
  const known = host.nodeLabel?.(id)
  if (host.onNode && known !== undefined) {
    return (
      <button type="button" className="chip max-w-full truncate transition-colors hover:bg-hover" data-ev-node={id}
              title={`在画布上选中并对准「${known || id}」`} onClick={() => host.onNode!(id)}>
        {known || label || id}
      </button>
    )
  }
  return <span data-ev-node={id}>{label || id}</span>
}

/** 判定：判定语和它的依据，谁做的，复算、封存的结论（细节在后面几步） */
function JudgedPart({ step, recompute, seal, overall }: {
  step: EvidenceAnalysisStep; recompute?: EvidenceAnalysisStep; seal?: EvidenceAnalysisStep; overall?: EvidenceSeal
}) {
  const verdict = step.verdict && typeof step.verdict === 'object' ? step.verdict : null
  const cited = step.cited ?? {}
  const citedText = cited.what === 'value' ? E.citedValue(String(cited.key ?? ''))
    // 结果表的格写列名（服务端照结果表记的 labels 给），不把列 id 当叫法；老接口没给列名的照旧写 id
    : cited.what === 'cell' ? E.citedCell(Number(cited.row) || 0, String(cited.column_label || cited.column || '')) : E.citedPhrase
  const refused = step.status === 'refused'
  const trusted = !!overall?.sealed && overall.ok !== false
  const problems = [
    step.eid_ok === false ? EVIDENCE_TEXT.integrity.eid : '',
    step.render_ok === false && step.hash_ok !== false ? E.renderBad : '',
    step.note ?? '',
    trusted && seal && seal.sealed === false && seal.note ? seal.note : '',
  ].filter(Boolean)
  const recomputeTone = recompute?.status === 'same' ? 'ok' : recompute?.status === 'different' ? 'bad' : 'none'
  return (
    <Part title={E.titleOf(step.alias ?? '', step.method_label ?? '')} data-ev-analysis={step.alias ?? ''}>
      {step.phrase && (
        <p className="font-medium" data-ev-analysis-phrase="">{step.phrase}</p>
      )}
      <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs text-dim">
        {refused && (
          <span className="chip" style={{ color: 'var(--st-waiting)', borderColor: 'var(--st-waiting)' }} data-ev-analysis-refused="">
            {E.refused}
          </span>
        )}
        <span data-ev-analysis-origin={step.origin ?? ''}>{E.origin[String(step.origin ?? '')] ?? NONE}</span>
        <NodeName id={step.node_id} label={step.label} />
        {step.artifact && <span className="mono text-faint" title={`${E.artifact} ${step.artifact}`}>{E.artifact} {shortId(step.artifact)}</span>}
      </div>
      <Problems code="analysis" items={problems} />
      <div className="mt-2">
        <Rows rows={[
          step.criterion ? [E.criterion, step.criterion] : null,
          verdict?.subjects?.length ? [E.subjects, subjectsText(verdict.subjects)] : null,
          verdict?.direction ? [E.direction, E.directionLabel[verdict.direction] ?? verdict.direction] : null,
          refused && step.reason ? [E.reason, <span key="r" style={{ color: 'var(--st-waiting)' }}>{step.reason}</span>] : null,
          [E.citedPart, <span key="c" data-ev-analysis-cited={cited.what ?? 'phrase'}>{citedText}</span>],
          step.caveats?.length ? [E.caveats, (
            <ul key="cv" className="space-y-0.5" data-ev-analysis-caveats="">
              {step.caveats.map((c, i) => <li key={`${c.code ?? ''}-${i}`}>{c.text}</li>)}
            </ul>
          )] : null,
          step.hypothesis ? [step.hypothesis_label || E.hypothesis, (
            <span key="h" data-ev-analysis-hypothesis="">
              {step.hypothesis}
              {step.hypothesis_note && <span className="mt-0.5 block text-2xs text-faint">{step.hypothesis_note}</span>}
            </span>
          )] : null,
          recompute ? [E.recompute, (
            <span key="rc" data-ev-analysis-recompute={recomputeTone}
                  style={{ color: recomputeTone === 'ok' ? 'var(--st-done)' : recomputeTone === 'bad' ? 'var(--st-failed)' : 'var(--st-cancelled)' }}>
              {recompute.text || NONE}
            </span>
          )] : null,
          seal ? [E.seal, (
            <span key="s" data-ev-analysis-seal={seal.via === 'metric_set' ? 'card' : seal.ledger ? 'ledger' : 'none'}>
              {seal.via === 'metric_set' ? E.viaCard : seal.ledger ? E.inLedger : E.notInLedger}
            </span>
          )] : null,
        ]} />
      </div>
    </Part>
  )
}

/** 方法：方法与版本、参数（规范化后的）、实际用的阈值（只列这个算子用到的）、现查结果 */
function MethodPart({ step }: { step: EvidenceAnalysisStep }) {
  const params = step.params && typeof step.params === 'object' && Object.keys(step.params).length ? step.params : null
  return (
    <Part title={E.method} data-ev-analysis-method="">
      <Rows rows={[
        [E.methodVersion, <span key="m">{step.method_label || NONE} · <span className="mono">{step.method || NONE}</span></span>],
        step.settings?.length ? [E.settings, (
          <ul key="s" className="space-y-0.5" data-ev-analysis-settings="">
            {step.settings.map((x) => (
              <li key={x.key}>{x.label || x.key} <span className="mono tnum">{valueText(x.value)}</span></li>
            ))}
          </ul>
        )] : null,
        step.probes?.length ? [E.probes, (
          <ul key="p" className="space-y-0.5" data-ev-analysis-probes="">
            {step.probes.map((p, i) => <li key={i}>{probeText(p)}</li>)}
          </ul>
        )] : null,
      ]} />
      {params && (
        <div className="mt-1.5">
          <div className="mb-0.5 text-2xs text-faint">{E.params}</div>
          <pre className="mono max-h-36 overflow-auto whitespace-pre-wrap rounded bg-bg px-1.5 py-1 text-2xs leading-relaxed text-dim [overflow-wrap:anywhere]"
               data-ev-analysis-params="">
            {JSON.stringify(params, null, 2)}
          </pre>
        </div>
      )}
    </Part>
  )
}

/** 计算：关键数的代入式、具名的数（被引用的那个描出来）、结果表（被引用的那一格高亮，遮罩的列写「已遮罩」） */
function CalcPart({ step }: { step: EvidenceAnalysisStep }) {
  const table = step.table && Array.isArray(step.table.columns) && Array.isArray(step.table.rows) ? step.table : null
  const labels = table?.labels ?? {}
  const heads = table ? table.columns.map((c) => labels[c] || c) : []
  const index = table ? (Array.isArray(table.row_index) ? table.row_index : table.rows.map((_, i) => i)) : []
  const masked = new Set(table?.masked ?? [])
  // 高亮的格按快照行号给：换成窗口里的下标、列 id 换成表头上的叫法
  const cells = (table?.highlight?.cells ?? [])
    .map(([r, c]) => [index.indexOf(r), labels[c] || c] as [number, string]).filter(([i]) => i >= 0)
  return (
    <Part title={E.calc} data-ev-analysis-calc="">
      {!!step.formulas?.length && (
        <div className="mb-1.5">
          <div className="mb-0.5 text-2xs text-faint">{E.formulas}</div>
          <pre className="mono whitespace-pre-wrap rounded bg-bg px-1.5 py-1 text-2xs leading-relaxed [overflow-wrap:anywhere]"
               data-ev-analysis-formulas="">
            {step.formulas.map((f) => f.text ?? '').filter(Boolean).join('\n')}
          </pre>
        </div>
      )}
      {!!step.values?.length && (
        <div className="mb-1.5">
          <div className="mb-0.5 text-2xs text-faint">{E.values}</div>
          <ul className="flex flex-wrap gap-1" data-ev-analysis-values="">
            {step.values.map((v) => (
              <li key={v.key} className="chip mono" data-ev-analysis-value={v.key}
                  {...(v.cited ? { 'data-ev-cited': '' } : {})}
                  style={v.cited ? { color: 'var(--st-done)', borderColor: 'var(--st-done)' } : undefined}>
                {v.key} <span className="tnum">{v.rendered ?? NONE}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {table && (
        <div data-ev-analysis-table="">
          <div className="mb-0.5 text-2xs text-faint">
            {E.resultTable}（{E.resultRows(table.rows.length, table.total_rows ?? table.rows.length)}）
          </div>
          <ResultTable table={{
            columns: heads,
            rows: table.rows.map((r) => (Array.isArray(r) ? r.map((v, j) => (masked.has(table.columns[j]) ? EVIDENCE_TEXT.masked : v)) : [])),
            truncated: false,
          }} highlight={{ rows: [], cols: [], cells }} masked={[...masked].map((c) => labels[c] || c)} />
          {table.window_truncated && <p className="mt-0.5 text-2xs text-faint">{EVIDENCE_TEXT.windowCut(table.rows.length)}</p>}
        </div>
      )}
    </Part>
  )
}

/** 输入的一份：角色、调用里写的名字、落到哪个 Q（点一下跳到那个查询步骤）、用到的列和行、总数那一格和闭合核对、下钻的上一层 */
function InputPart({ step, onQuery, trusted }: { step: EvidenceAnalysisStep; onQuery?: (alias: string) => void; trusted: boolean }) {
  const closure = step.closure && typeof step.closure === 'object' ? step.closure : null
  const ratio = closure?.mode === 'ratio'
  const closed = closure ? (ratio ? (closure.closed ? E.ratioClosureOk : E.ratioClosureBad)
    : (closure.closed ? E.closureOk : E.closureBad))(closure.headline ?? NONE, closure.total ?? NONE) : null
  const rows = Array.isArray(step.rows) ? E.rowsList(step.rows.filter((r): r is number => typeof r === 'number'))
    : step.rows === 'all' ? E.allRows : null
  const where = step.query || step.alias || ''
  const parent = step.role === 'parent'
  return (
    <Part title={where ? E.inputOf(where) : E.input} data-ev-analysis-input={step.role ?? ''}>
      <Rows rows={[
        [E.role, step.role_label || step.role || NONE],
        step.ref ? [E.ref, <code key="r" className="mono">{step.ref}</code>] : null,
        step.query ? [E.query, onQuery
          ? <button key="q" type="button" className="chip mono hover:bg-hover" data-ev-goto-query={step.query}
                    onClick={() => onQuery(step.query!)}>{EVIDENCE_TEXT.gotoQuery(step.query)}</button>
          : <span key="q" className="mono">{step.query}</span>] : null,
        parent && step.alias ? [E.parent, <span key="p" className="mono">{step.alias}</span>] : null,
        step.columns?.length ? [E.columnsUsed, <span key="c" className="mono">{step.columns.join('、')}</span>] : null,
        rows ? [E.rowsUsed, rows] : null,
        'value' in step && !parent ? [ratio ? E.headlineRatio : E.headline, (
          <span key="v" className="tnum">
            {step.column != null && typeof step.row === 'number' ? `${E.cellAt(step.row, step.column)} = ` : ''}
            {/* 那一格的值服务端渲染好了（千分位、NULL 写「—」，和闭合核对同一个写法），照原样显示；老接口没给的按值本身写 */}
            {step.value_rendered ?? valueText(step.value)}
          </span>
        )] : null,
        closure && closed ? [E.closure, (
          <span key="cl" data-ev-analysis-closure={closure.closed ? 'closed' : 'open'}
                style={{ color: closure.closed ? 'var(--st-done)' : 'var(--st-waiting)' }}>{closed}</span>
        )] : null,
        step.phrase ? [E.phrase, step.phrase] : null,
      ]} />
      <Problems code="sealed" items={trusted && step.sealed === false ? [EVIDENCE_TEXT.integrity.sealed] : []} />
    </Part>
  )
}

/**
 * 分析结论的出处链（查询步骤之外的那几步）。pending、failed 是证据链还没回来、回不来时的说法（同指标一节）；onQuery 点输入里的
 * Q 跳到下面的查询步骤（证据面板给）
 */
export function AnalysisChain({ chain, seal, pending, failed, onQuery }: {
  chain: EvidenceAnalysisStep[]
  seal?: EvidenceSeal
  pending?: boolean
  failed?: string
  onQuery?: (alias: string) => void
}) {
  const judged = chain.find((s) => s.step === 'analysis')
  if (!judged) {
    return (
      <Part title={E.title} data-ev-analysis="">
        <p className="text-2xs text-faint" data-ev-chain={pending ? 'loading' : 'missing'}>
          {pending ? E.chainPending : failed || EVIDENCE_TEXT.chainMissing}
        </p>
      </Part>
    )
  }
  const trusted = !!seal?.sealed && seal.ok !== false
  const method = chain.find((s) => s.step === 'analysis_method')
  const calc = chain.find((s) => s.step === 'analysis_calc')
  const inputs = chain.filter((s) => s.step === 'analysis_input')
  return (
    <>
      <JudgedPart step={judged} recompute={chain.find((s) => s.step === 'analysis_recompute')}
                  seal={chain.find((s) => s.step === 'analysis_seal')} overall={seal} />
      {method && <MethodPart step={method} />}
      {calc && <CalcPart step={calc} />}
      {inputs.map((s, i) => <InputPart key={`${s.ref ?? ''}-${s.role ?? ''}-${i}`} step={s} onQuery={onQuery} trusted={trusted} />)}
    </>
  )
}

/**
 * 指标步骤上的两期可比性（口径卡跨两份同形查询算的环比、同比，执行时自动做的检查）：判定、判定语、原因一句一个，展开看那份
 * 可比性结论的整条链（两份输入快照的查询步骤指标链里已经有了，不再给）。不进台账、随口径卡的指标集封存
 */
export function ComparabilityRow({ comparability, seal }: { comparability: EvidenceComparability; seal?: EvidenceSeal }) {
  const [open, setOpen] = useState(false)
  const verdict = String(comparability.verdict ?? '')
  const tone = verdict === 'comparable' ? 'var(--st-done)' : verdict === 'incomparable' ? 'var(--st-failed)' : 'var(--st-waiting)'
  const steps = Array.isArray(comparability.steps) ? comparability.steps.filter((s) => s && typeof s === 'object') : []
  const trusted = !!seal?.sealed && seal.ok !== false
  const problems = [
    comparability.hash_ok === false ? E.comparabilityHash : '',
    trusted && comparability.sealed === false && comparability.hash_ok !== false ? E.comparabilityUnsealed : '',
  ].filter(Boolean)
  return (
    <div data-ev-comparability={verdict}>
      <dt className="text-2xs text-faint">{E.comparability}</dt>
      <dd>
        <div className="flex flex-wrap items-baseline gap-x-1.5">
          <span className="chip" style={{ color: tone, borderColor: tone }}>{E.comparabilityLabel[verdict] ?? (verdict || NONE)}</span>
          {comparability.phrase && <span data-ev-comparability-phrase="">{comparability.phrase}</span>}
        </div>
        {!!comparability.reasons?.length && (
          <ul className="mt-0.5 space-y-0.5 text-2xs text-dim" data-ev-comparability-reasons="">
            {comparability.reasons.map((r) => <li key={r}>· {r}</li>)}
          </ul>
        )}
        <Problems code="comparability" items={problems} />
        {steps.length > 0 && (
          <button type="button" className="btn btn-xs btn-ghost -ml-1 mt-0.5" aria-expanded={open}
                  data-ev-comparability-toggle="" onClick={() => setOpen((v) => !v)}>
            <ChevronRight size={11} aria-hidden style={{ transform: open ? 'rotate(90deg)' : 'none' }} />
            {open ? E.comparabilityClose : E.comparabilityOpen}
          </button>
        )}
        {open && (
          <div className="mt-1 space-y-3 border-l-2 pl-2" data-ev-comparability-chain="">
            <AnalysisChain chain={steps} seal={seal} />
          </div>
        )}
      </dd>
    </div>
  )
}

/** 说法违规的代码（服务端 engine/analysis_claims.CODES） */
export const CLAIM_CODES = new Set(Object.keys(ANALYSIS_TEXT.claim.label))
export const isClaimViolation = (v: Pick<EvidenceViolation, 'code'>): boolean => CLAIM_CODES.has(v.code)

/** 证据面板：落在这个片段上的说法违规，一处一条：种类、服务端的原话、所挂的分析结论（点一下看那份结论） */
export function ClaimFlags({ violations, onAnalysis }: {
  violations: EvidenceViolation[]
  onAnalysis?: (alias: string) => void
}) {
  if (!violations.length) return null
  return (
    <Part title={ANALYSIS_TEXT.claim.panelTitle} data-ev-claim-flags={String(violations.length)}>
      <ul className="space-y-1.5">
        {violations.map((v, i) => (
          <li key={`${v.code}-${i}`} className="rounded border px-2 py-1" data-ev-claim-flag={v.code}
              style={{ borderColor: 'color-mix(in srgb, var(--st-waiting) 45%, var(--border))' }}>
            <div className="flex flex-wrap items-baseline gap-x-1.5">
              {v.text && <span className="font-semibold">「{v.text}」</span>}
              <span className="chip" style={{ color: 'var(--st-waiting)' }}>{ANALYSIS_TEXT.claim.label[v.code] ?? v.code}</span>
            </div>
            {v.message && <p className="mt-0.5 text-dim">{v.message}</p>}
            <div className="mt-0.5 flex flex-wrap items-center gap-1 text-2xs text-faint">
              {v.analyses?.length ? (
                <>
                  <span>{ANALYSIS_TEXT.claim.analyses}</span>
                  {v.analyses.map((a) => (onAnalysis
                    ? <button key={a} type="button" className="chip mono hover:bg-hover" data-ev-claim-analysis={a}
                              onClick={() => onAnalysis(a)}>{a}</button>
                    : <span key={a} className="mono">{a}</span>))}
                </>
              ) : v.code !== 'causal_unsupported' && <span>{ANALYSIS_TEXT.claim.noAnalyses}</span>}
            </div>
          </li>
        ))}
      </ul>
    </Part>
  )
}

/** 分析范围的一条：编号（点得开）、判定语；拆过的写按哪些维度、下钻自谁；算不了的判定语里已经写了原因 */
function ScopeLine({ item, onOpen }: { item: AnalysisScopeItem; onOpen?: (alias: string) => void }) {
  const alias = item.alias ?? ''
  const extra = [item.dims?.length ? ANALYSIS_TEXT.scope.dims(item.dims) : '', item.parent ? ANALYSIS_TEXT.scope.drill(item.parent) : '']
    .filter(Boolean).join(' · ')
  return (
    <li className="flex items-baseline gap-1.5" data-ev-scope-item={alias}>
      {alias && (onOpen
        ? <button type="button" className="chip mono shrink-0 hover:bg-hover" data-seg={`scope:${alias}`}
                  aria-label={ANALYSIS_TEXT.scope.open(alias)} onClick={() => onOpen(alias)}>{alias}</button>
        : <span className="chip mono shrink-0">{alias}</span>)}
      <span className="min-w-0 [overflow-wrap:anywhere]">
        {item.phrase || NONE}
        {extra && <span className="ml-1 text-2xs text-faint">（{extra}）</span>}
      </span>
    </li>
  )
}

/**
 * 报告末尾系统写的「分析范围」（文档的 analysis_scope，contract.ScopeBlock）：不是写作者写的，随报告封存。几组都照服务端分好的
 * 排：现象（可比性、基线、趋势）、拆过（按下钻链，上一层在前）、排除（分散、差异不足以下结论）、算不了、没拆（候选维度里没拆过的；
 * 核对不了时照实说）、检验（做了几次差异检验、校正过没有）。编号点得开（onOpen 打开那份结论的出处链）
 */
export function ScopeSection({ scope, onOpen }: { scope: AnalysisScope; onOpen?: (alias: string) => void }) {
  const S = ANALYSIS_TEXT.scope
  const groups: [string, string, AnalysisScopeItem[]][] = [
    ['phenomena', S.phenomena, scope.phenomena ?? []],
    ['decomposed', S.decomposed, scope.decomposed ?? []],
    ['excluded', S.excluded, scope.excluded ?? []],
    ['refused', S.refused, scope.refused ?? []],
  ]
  const shown = groups.filter(([, , items]) => items.length)
  const notDecomposed = scope.not_decomposed === null || scope.not_decomposed === undefined
    ? (scope.note || (scope.not_decomposed === null ? S.notDecomposedUnknown : ''))
    : scope.not_decomposed.length ? S.notDecomposedList(scope.not_decomposed) : S.notDecomposedNone
  const tests = typeof scope.tests === 'number' && scope.tests > 0 ? S.testsText(scope.tests, scope.corrected) : ''
  if (!shown.length && !notDecomposed && !tests) return null
  return (
    <section className="mt-3 rounded-lg border px-3 py-2" data-ev-scope="" aria-label={S.title}>
      <div className="flex flex-wrap items-baseline gap-x-2">
        <h4 className="text-xs font-semibold">{S.title}</h4>
        <span className="text-2xs text-faint">{S.hint}</span>
      </div>
      <dl className="mt-1.5 space-y-1.5 text-xs">
        {shown.map(([key, title, items]) => (
          <div key={key} data-ev-scope-group={key}>
            <dt className="text-2xs font-medium text-faint">{title}</dt>
            <dd><ul className="mt-0.5 space-y-0.5">{items.map((it, i) => <ScopeLine key={`${it.alias ?? ''}-${i}`} item={it} onOpen={onOpen} />)}</ul></dd>
          </div>
        ))}
        {notDecomposed && (
          <div data-ev-scope-group="not_decomposed">
            <dt className="text-2xs font-medium text-faint">{S.notDecomposed}</dt>
            <dd className="mt-0.5" data-ev-scope-not-decomposed={scope.not_decomposed == null ? 'unknown' : String(scope.not_decomposed.length)}>
              {notDecomposed}
            </dd>
          </div>
        )}
        {tests && (
          <div data-ev-scope-group="tests">
            <dt className="text-2xs font-medium text-faint">{S.tests}</dt>
            <dd className="mt-0.5">{tests}</dd>
          </div>
        )}
      </dl>
    </section>
  )
}
