import { useEffect, useId, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { AlertTriangle, ChevronRight, FileText } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../api/client'
import { ErrorState, Modal, Skeleton } from '../components/ui'
import { NONE, formatDateTime } from '../lib/format'
import { EVIDENCE_TEXT, RAW_STATE_LABEL, VERSIONS_TEXT } from '../lib/terms'
import { ManifestBody } from '../pages/import/ManifestBody'
import { segmentKey, useEvidence } from '../store/evidence'
import type {
  EvidenceProvenance as Provenance, ManifestOut, ProvenanceCellSource, ProvenanceCheck, ProvenanceFromCell, ProvenancePart,
  ProvenanceVersion,
} from '../types'
import { CopyChip } from './Markdown'

/**
 * 证据面板里的「数据版本」「推断的来源」「相关核对」三节（Excel 导入期 4，P4-SPEC 4.1–4.3）。
 *
 * 上传表格的单元格：这次查询查的是哪一版数据（各期的统计期、文件、原件状态、写过的接受理由），这一格来自原表的
 * 哪个工作表、哪一格（日期、年份、行标签、分段标题各取自哪里），以及这一行、这一格的核对结果。
 *
 * 数据都来自推断来源接口（按需算，面板打开那个片段时才算）。调用方只在片段是单元格引用、且片段接口的查询步骤
 * 带 provenance: true 时挂这个组件，所以期 4 之前的运行、手工源、指标片段一个请求都不多发。
 *
 * 三节分两种性质，界面分开标注：
 * - 数据版本来自封存链上的导入清单（内容寻址），不加「推断」标注；
 * - 推断的来源、相关核对是先按主键认出这一行再推出来的，挂「推断，不属于已封存的证据」，用虚线左边框和
 *   别的几节区分。封存那一行（verdict）不受它影响，推断的来源不计入「已封存 · 核对一致」。
 *
 * 标题用 <h4>（读屏按标题跳得到），视觉照抄面板里 Part 的小标题；Part 本身不动——改它会改变期 4 之前所有面板的
 * DOM。标红的提示用自己的属性 data-ev-prov-alert，不用 data-ev-integrity：现有断言数过后者的个数。
 * 折叠、展开不加动效（P4-SPEC 4.3）。
 */

const P = EVIDENCE_TEXT.provenance

/** 小标题的样式：同 EvidencePanel 的 Part（mb-1 text-2xs font-medium text-faint），只是换成真正的标题元素 */
const HEAD = 'text-2xs font-medium text-faint'

/** 期数多于这个数时折叠：只展开这一格所在的一期和最近一期 */
const FOLD_AT = 3

/** 原件 sha256 显示前几位（P4-SPEC 4.1：前 12 位） */
const SHA_PREFIX = 12

/**
 * 导入、清除、作废记录的时间。没记（契约里 at 可以是 null）或认不出时给空串，由调用方略去这一项：formatDateTime
 * 拿不到时间时返回「—」，它是真值，filter(Boolean) 去不掉，会在「原件已清除 · — · 理由：…」里留下一个孤立的「—」
 */
function stamp(at: string | null | undefined): string {
  const text = formatDateTime(at)
  return text === NONE ? '' : text
}

export function EvidenceProvenance({ runId, segId, report }: { runId: string; segId: string; report?: string }) {
  const key = segmentKey(runId, segId, report)
  const slot = useEvidence((s) => s.provenance[key])
  const load = useEvidence((s) => s.loadProvenance)
  // 槽没了就再取：面板开着时不会被清（清缓存只在最后一个面板关掉以后），这里只是不让一节永远停在「正在推断」
  const missing = !slot
  useEffect(() => {
    if (missing) void load(runId, segId, report)
  }, [missing, runId, segId, report, load])

  if (!slot || slot.status === 'loading') {
    // 取的过程中只画这一节的加载态，不先画三节空壳
    return (
      <SourceShell state="loading">
        <p className="text-dim" data-ev-prov-loading="">{P.loading}</p>
      </SourceShell>
    )
  }
  if (slot.status === 'error' || !slot.data || typeof slot.data !== 'object') {
    // 老服务端没有这个接口（404、405 且没有机读码）：三节都不画，和期 4 之前一样
    if (oldServer(slot.error)) return null
    return (
      <SourceShell state="error">
        <p className="text-dim" data-ev-prov-failed="">{P.failed}</p>
      </SourceShell>
    )
  }
  // 按片段换实例：展开的「技术细节」「另有 n 期」是这一格的状态，点到别的格子时收起
  return <Answer key={key} d={slot.data} />
}

function oldServer(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 405) && !error.code
}

/** 三节画不画、推断的来源那一节画什么（P4-SPEC 4.1 的渲染矩阵） */
interface Plan {
  version: boolean
  source: 'none' | 'reason' | 'alert' | 'cell'
  checks: boolean
}

const NOTHING: Plan = { version: false, source: 'none', checks: false }

/**
 * 渲染矩阵。表外的组合不该出现（服务端的契约检查会拦），出现时按「table_only、无 alert」画：给出数据版本（有的话）
 * 和一句原因。alert 只在矩阵里列出的组合下画成红色
 */
function planOf(d: Provenance): Plan {
  const code = d.reason?.code
  const alert = d.alert?.code
  if (d.status === 'none') {
    if (!alert && (code === 'legacy_doc' || code === 'not_upload' || code === 'not_cell')) return NOTHING
    if (!alert && (code === 'not_sealed' || code === 'simple_upload')) return { version: false, source: 'reason', checks: false }
    if (alert === 'manifest_unreadable' || alert === 'chain_mismatch') return { version: false, source: 'alert', checks: false }
  } else if (d.status === 'table_only') {
    if (alert === 'db_tampered' || alert === 'chain_mismatch') return { version: true, source: 'alert', checks: false }
    if (!alert) return { version: true, source: 'reason', checks: false }
  } else if (d.status === 'inferred') {
    if (!alert && d.cell_source) return { version: true, source: 'cell', checks: true }
  }
  return { version: true, source: 'reason', checks: false }
}

function Answer({ d }: { d: Provenance }) {
  const plan = planOf(d)
  const version = plan.version && d.version ? d.version : null
  const [manifest, setManifest] = useState<ProvenancePart | null>(null)
  if (plan.source === 'none') return null
  return (
    <>
      {version && <VersionPart v={version} sealed={d.sealed !== false} onManifest={setManifest} />}
      <SourceShell state={d.status}>
        {plan.source === 'alert' && d.alert && (
          // 标红：样式同 IntegrityList（失败色的框），放在这一节最前面；有它时不再画 reason.text
          <p className="flex items-start gap-1.5 rounded border px-2 py-1 [overflow-wrap:anywhere]" data-ev-prov-alert={d.alert.code}
             style={{ color: 'var(--st-failed)', borderColor: 'var(--st-failed)', background: 'var(--st-failed-soft)' }}>
            <AlertTriangle size={12} aria-hidden className="mt-0.5 shrink-0" />
            <span>{d.alert.text}</span>
          </p>
        )}
        {plan.source === 'reason' && d.reason?.text && (
          <p className="text-dim [overflow-wrap:anywhere]" data-ev-prov-reason={d.reason.code}>{d.reason.text}</p>
        )}
        {plan.source === 'cell' && d.cell_source && <CellPart cs={d.cell_source} version={d.version} />}
      </SourceShell>
      {plan.checks && Array.isArray(d.checks) && d.checks.length > 0 && <ChecksPart checks={d.checks} />}
      {manifest && createPortal(
        // 弹窗挂到 body 上：侧边面板是 fixed 的一层，里面的遮罩盖不住整页（同「打开完整快照」）
        <ManifestDialog part={manifest} onClose={() => setManifest(null)} />,
        document.body,
      )}
    </>
  )
}

/** 「推断，不属于已封存的证据」：推断的来源、相关核对两节的标题旁都挂 */
function Badge() {
  return (
    <span className="chip shrink-0 text-faint" data-ev-prov-badge=""
          style={{ borderColor: 'var(--border-strong)', borderStyle: 'dashed' }}>
      {P.badge}
    </span>
  )
}

/** 推断出来的一节：虚线左边框，标题旁挂「推断」标签 */
function Inferred({ title, children, ...rest }: { title: string; children: ReactNode } & Record<`data-${string}`, string>) {
  return (
    <div className="border-l-2 border-dashed pl-2" style={{ borderColor: 'var(--border-strong)' }} {...rest}>
      <div className="mb-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
        <h4 className={HEAD}>{title}</h4>
        <Badge />
      </div>
      {children}
    </div>
  )
}

/** 推断的来源一节的外壳：data-ev-provenance 写这一节的状态（inferred / table_only / none，取的过程中 loading，出错 error） */
function SourceShell({ state, children }: { state: string; children: ReactNode }) {
  return <Inferred title={P.title} data-ev-provenance={state}>{children}</Inferred>
}

// ---------------------------------------------------------------------------
// 数据版本
// ---------------------------------------------------------------------------

function VersionPart({ v, sealed, onManifest }: {
  v: ProvenanceVersion; sealed: boolean; onManifest: (part: ProvenancePart) => void
}) {
  const [open, setOpen] = useState(false)
  const moreId = useId()
  const parts = Array.isArray(v.parts) ? v.parts : []
  // 多于 3 期：这一格所在的一期和最近一期展开，其余收进「另有 n 期」
  const fold = parts.length > FOLD_AT
  const latest = parts[parts.length - 1]
  const keep = parts.filter((p) => p.has_row || p === latest)
  const folded = fold ? parts.length - keep.length : 0
  const shown = fold && !open ? keep : parts
  const tables = (Array.isArray(v.tables) ? v.tables : []).filter((t) => t && t.name)
  return (
    <div data-ev-prov-version="">
      <h4 className={clsx('mb-1', HEAD)}>{P.version}</h4>
      {!sealed && (
        // 封存那一行已经照实说了状态，这里只是不让「来自封存链」的说法越界：dim 色，不标红
        <p className="mb-1 text-2xs text-dim" data-ev-prov-unsealed="">{P.unsealed}</p>
      )}
      <p className="[overflow-wrap:anywhere]" data-ev-prov-head="">{P.versionHead(v.source, v.mode, parts.length)}</p>
      {tables.length > 0 && (
        <p className="mt-0.5 text-2xs text-dim [overflow-wrap:anywhere]" data-ev-prov-tables="">
          {P.tables(tables.map((t) => (t.kind === 'reported_total' ? P.totalTable(t.name) : t.name)))}
        </p>
      )}
      <ul className="mt-1 space-y-1.5" id={moreId}>
        {shown.map((p) => (
          <PartRow key={`${p.import_id}-${p.seq}`} p={p} manifest={v.manifest_view !== false} onManifest={onManifest} />
        ))}
      </ul>
      {fold && folded > 0 && (
        // 展开后按钮还在、焦点留在它上面（同一个元素），再按一次收起
        <button type="button" className="btn btn-xs btn-ghost -ml-1 mt-1" aria-expanded={open} aria-controls={moreId}
                data-ev-prov-more="" onClick={() => setOpen((x) => !x)}>
          <ChevronRight size={11} aria-hidden className={clsx(open && 'rotate-90')} />
          {P.morePeriods(folded)}
        </button>
      )}
      {v.manifest_view === false && (
        // 清单里有核对细节里的数、标签原文、区域外文字：数据源设了遮罩时面板自己的入口不多暴露
        <p className="mt-1 text-2xs text-dim" data-ev-prov-manifest-masked="">{P.manifestMasked}</p>
      )}
    </div>
  )
}

/** 当前状态（取自导入记录，不在封存范围内）：作废、清除后面加这枚小字 */
function Current() {
  return <span className="chip ml-1.5 text-faint" data-ev-prov-current="">{P.current}</span>
}

function PartRow({ p, manifest, onManifest }: {
  p: ProvenancePart; manifest: boolean; onManifest: (part: ProvenancePart) => void
}) {
  const period = p.period && p.period.start && p.period.end ? p.period : null
  const purged = p.raw_state === 'purged' || !!p.purged
  const sha = typeof p.raw_sha256 === 'string' ? p.raw_sha256.slice(0, SHA_PREFIX) : ''
  const committed = stamp(p.committed_at)
  const facts = [
    p.file_name ? P.file(p.file_name, sha) : '',
    p.region ? P.region(p.region, p.excluded_rows) : p.excluded_rows ? P.excluded(p.excluded_rows) : '',
    period?.source === 'human' ? `${VERSIONS_TEXT.periodHuman}（${VERSIONS_TEXT.signedBy(period.signed_by || VERSIONS_TEXT.unsigned)}）` : '',
    // 清除过的另起一行写清除记录和「当前状态」，这里不重复
    !purged && p.raw_state ? RAW_STATE_LABEL[p.raw_state] ?? '' : '',
    committed ? P.committedAt(committed) : '',
    p.recipe_seq != null ? VERSIONS_TEXT.recipeSeq(p.recipe_seq) : '',
    p.signed_by ? VERSIONS_TEXT.signedBy(p.signed_by) : '',
  ].filter(Boolean)
  const accepts = Array.isArray(p.acceptances) ? p.acceptances : []
  return (
    <li className="min-w-0" data-ev-prov-part={String(p.seq)} data-ev-prov-has-row={p.has_row ? '' : undefined}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="tnum font-medium">{period ? P.part(p.seq, period.start, period.end) : P.partNoPeriod(p.seq)}</span>
        {p.has_row && (
          <span className="chip" style={{ color: 'var(--accent)', borderColor: 'var(--accent)' }} data-ev-prov-here="">{P.hasRow}</span>
        )}
      </div>
      {facts.length > 0 && <p className="text-2xs text-dim [overflow-wrap:anywhere]">{facts.join(' · ')}</p>}
      {purged && (
        <p className="mt-0.5 text-2xs text-dim [overflow-wrap:anywhere]" data-ev-prov-purged="">
          {RAW_STATE_LABEL.purged}
          {p.purged ? ` · ${P.stateDetail(stamp(p.purged.at), p.purged.reason, p.purged.signed_by)}` : ''}
          <Current />
        </p>
      )}
      {p.revoked && (
        <p className="mt-0.5 text-2xs [overflow-wrap:anywhere]" style={{ color: 'var(--st-waiting)' }} data-ev-prov-revoked="">
          {P.revoked} · {P.stateDetail(stamp(p.revoked.at), p.revoked.reason, p.revoked.signed_by)}
          <Current />
        </p>
      )}
      {accepts.length > 0 && (
        // 接受理由在清单里，是当时的事实：作废之后照旧列出
        <ul className="ml-3 mt-0.5 space-y-0.5 text-2xs text-dim">
          {accepts.map((a, i) => (
            <li key={`${a.check_id}-${i}`} className="[overflow-wrap:anywhere]" data-ev-prov-acceptance={a.check_id}>
              {P.acceptedLine(a.title || a.check_id, a.reason, a.signed_by)}
            </li>
          ))}
        </ul>
      )}
      {manifest && p.manifest && (
        <button type="button" className="btn btn-xs btn-ghost -ml-1 mt-0.5" data-ev-prov-manifest={p.manifest}
                onClick={() => onManifest(p)}>
          <FileText size={11} aria-hidden /> {P.manifest}
        </button>
      )}
    </li>
  )
}

/**
 * 查看导入清单：版本页的清单渲染（ManifestBody），内容按清单的工件 id 经工件接口取（内容寻址，取回时复验哈希）。
 * 不用 ArtifactViewer：它对非表格内容整份 JSON 输出，遮罩不起作用，64 KB 的清单也没法读
 */
function ManifestDialog({ part, onClose }: { part: ProvenancePart; onClose: () => void }) {
  const [state, setState] = useState<{ id: string; content?: unknown; error?: unknown } | null>(null)
  useEffect(() => {
    let alive = true
    setState({ id: part.manifest })
    api.artifact(part.manifest).then(
      (res) => { if (alive) setState({ id: part.manifest, content: res?.content }) },
      (error) => { if (alive) setState({ id: part.manifest, error }) },
    )
    return () => { alive = false }
  }, [part.manifest])
  const mine = state?.id === part.manifest ? state : null
  const m: ManifestOut | null = mine?.content && typeof mine.content === 'object'
    ? { artifact_id: part.manifest, verified: true, kind: 'import_manifest', content: mine.content as Record<string, any> }
    : null
  return (
    <Modal open onClose={onClose} width={920} title={VERSIONS_TEXT.manifestTitle(part.seq)}>
      {mine?.error
        ? <ErrorState error={mine.error} />
        : !m
          ? <Skeleton rows={5} height={36} gap={8} />
          : (
            <div data-ev-prov-manifest-view={part.manifest}>
              <ManifestBody m={m} fileName={part.file_name} rawState={part.raw_state} recipeSeq={part.recipe_seq} />
            </div>
          )}
    </Modal>
  )
}

// ---------------------------------------------------------------------------
// 推断的来源：格子与逐条
// ---------------------------------------------------------------------------

function CellPart({ cs, version }: { cs: ProvenanceCellSource; version: ProvenanceVersion | null }) {
  const [tech, setTech] = useState(false)
  const techId = useId()
  const parts = version?.parts ?? []
  const part = parts.find((p) => p.has_row) ?? parts.find((p) => p.seq === cs.part_seq)
  const items = fromItems(cs)
  const recheck = cs.recheck
  const sql = recheck?.sql ? (recheck.params?.length ? `${recheck.sql}\n-- ${JSON.stringify(recheck.params)}` : recheck.sql) : ''
  return (
    <>
      <p className="text-sm font-semibold [overflow-wrap:anywhere]" data-ev-prov-cell="">{P.cell(cs.sheet, cs.cell)}</p>
      {parts.length > 1 && (
        // 多期时坐标是哪一期原件的坐标：同一个位置在别的期的文件里是另一个数
        <p className="text-2xs text-dim [overflow-wrap:anywhere]" data-ev-prov-from-part="">
          {P.fromPart(cs.part_seq, part?.file_name ?? null)}
        </p>
      )}
      {items.length > 0 && (
        <ul className="mt-1 space-y-0.5 [overflow-wrap:anywhere]" data-ev-prov-from="">
          {items.map((it) => <li key={it.key} data-ev-prov-item={it.kind}>{it.text}</li>)}
        </ul>
      )}
      {cs.merged_fill && (
        <p className="mt-1 text-2xs [overflow-wrap:anywhere]" style={{ color: 'var(--st-waiting)' }} data-ev-prov-merged="">{P.mergedFill}</p>
      )}
      {cs.raw_purged && (
        <p className="mt-1 text-2xs [overflow-wrap:anywhere]" style={{ color: 'var(--st-waiting)' }} data-ev-prov-raw="">{P.rawPurged}</p>
      )}
      {(recheck?.ok || sql) && (
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs text-faint">
          {recheck?.ok && <span data-ev-prov-recheck="">{P.recheck}</span>}
          {sql && (
            <button type="button" className="inline-flex items-center gap-0.5 rounded px-1 transition-colors hover:bg-hover hover:text-fg"
                    aria-expanded={tech} aria-controls={techId} data-ev-prov-details="" onClick={() => setTech((x) => !x)}>
              <ChevronRight size={10} aria-hidden className={clsx(tech && 'rotate-90')} />
              {P.details}
            </button>
          )}
        </div>
      )}
      {sql && (
        // 收起时留在 DOM 里（hidden）：按钮的 aria-controls 始终指得到它
        <div className="mt-1" id={techId} hidden={!tech} data-ev-prov-tech="">
          <div className="mb-0.5 flex items-center gap-2">
            <span className="min-w-0 flex-1 text-2xs font-medium text-faint">{P.recheckSql}</span>
            <CopyChip label={EVIDENCE_TEXT.copySql} text={() => sql} />
          </div>
          <pre className="mono max-h-28 overflow-auto whitespace-pre-wrap rounded bg-bg px-1.5 py-1 text-2xs leading-relaxed text-dim [overflow-wrap:anywhere]">
            {sql}
          </pre>
        </div>
      )}
    </>
  )
}

interface Item { key: string; kind: string; text: string }

/**
 * 逐条：这一行其余键列各取自哪一格，加上年份、规范写法、合计口径。顺序照 4.1：日期 → 年份 → 行标签 → 分段标题 →
 * 规范写法。坐标和格子同一张工作表时只写坐标，不同时带上工作表名。列表的多行表头合成一条
 */
function fromItems(cs: ProvenanceCellSource): Item[] {
  const from: ProvenanceFromCell[] = Array.isArray(cs.from) ? cs.from.filter((f) => f && f.cell) : []
  const ref = (f: ProvenanceFromCell) => (f.sheet && f.sheet !== cs.sheet ? `${f.sheet}!${f.cell}` : f.cell)
  const year = yearItem(cs)
  const heads = from.filter((f) => f.role === 'col_header')
  const out: Item[] = []
  let yearDone = false
  from.forEach((f, i) => {
    const key = `${f.role}-${i}`
    if (f.role === 'axis_header') {
      out.push({ key, kind: f.role, text: P.axisHeader(ref(f)) })
      if (year && !yearDone) { out.push(year); yearDone = true }
    } else if (f.role === 'row_label') {
      out.push({ key, kind: f.role, text: P.rowLabel(ref(f), f.text) })
    } else if (f.role === 'section_title') {
      out.push({ key, kind: f.role, text: P.sectionTitle(ref(f), f.locate_title) })
    } else if (f.role === 'total_label') {
      out.push({ key, kind: f.role, text: P.totalLabel(ref(f), f.text) })
    } else if (f.role === 'col_header' && f === heads[0]) {
      out.push({ key, kind: f.role, text: P.colHeader(heads.map(ref).join('、')) })
    }
  })
  if (year && !yearDone) out.push(year)
  if (cs.canonical?.raw && cs.canonical.canonical) {
    out.push({ key: 'canonical', kind: 'canonical', text: P.canonical(cs.canonical.raw, cs.canonical.canonical) })
  }
  if (cs.kind === 'reported_total') out.push({ key: 'total', kind: 'reported_total', text: P.reportedTotal })
  return out
}

function yearItem(cs: ProvenanceCellSource): Item | null {
  const y = cs.year
  if (!y) return null
  // mixed：清单只记每块的表头形式，逐格判断不了，照实说，不断言这一格属于哪一种
  const text = y.mixed ? P.yearMixed
    : y.source === 'human' ? P.yearHuman(y.signed_by || VERSIONS_TEXT.unsigned)
    : P.yearPeriod((Array.isArray(y.cells) ? y.cells : []).join('、'))
  return { key: 'year', kind: 'year', text }
}

// ---------------------------------------------------------------------------
// 相关核对
// ---------------------------------------------------------------------------

/** 状态的颜色只用令牌：成立 / 一致是 done，不成立是 failed，未能核对、无法确定是 waiting，其余中性 */
const TONE: Record<string, string> = {
  passed: 'var(--st-done)',
  ok: 'var(--st-done)',
  mismatch: 'var(--st-failed)',
  unverifiable: 'var(--st-waiting)',
  unknown: 'var(--st-waiting)',
}

function ChecksPart({ checks }: { checks: ProvenanceCheck[] }) {
  return (
    <Inferred title={P.checks} data-ev-prov-checks="">
      <ul className="space-y-1">
        {checks.map((c, i) => <CheckRow key={`${c.id}-${i}`} c={c} />)}
      </ul>
    </Inferred>
  )
}

/**
 * 一条核对：「编号 标题 — 这一行成立」（关系核对看行级结果，合计看格级结果，其余看本期结论）。本期结论不成立或未能
 * 核对时另起一行写出来，写过理由的接在后面：「本期不成立 · 已接受，理由：…」——行级成立、本期不成立（别的日子不成立、
 * 已接受）两件事都要看得到。口径不同（R2）不判行，本期结论就是「口径说明」，和别的核对同一种写法，扫读时对得齐
 */
function CheckRow({ c }: { c: ProvenanceCheck }) {
  const main: { code: string; text: string } | null = c.row_status
    ? { code: c.row_status, text: P.row[c.row_status] ?? c.row_status }
    : c.cell_status ? { code: c.cell_status, text: P.cellStatus[c.cell_status] ?? c.cell_status }
    : null
  const partText = P.partStatus[c.part_status] ?? c.part_status
  // 没有行级、格级结果的（统计期、口径之外的核对）主行就写本期结论，理由接在同一行
  const lead = main ?? { code: c.part_status, text: partText }
  const accepted = c.acceptance ? P.accepted(c.acceptance.reason, c.acceptance.signed_by) : ''
  const showPart = !!main && (c.part_status === 'mismatch' || c.part_status === 'unverifiable')
  const second = main ? [showPart ? partText : '', accepted].filter(Boolean).join(' · ') : ''
  return (
    <li className="min-w-0 [overflow-wrap:anywhere]" data-ev-prov-check={c.id} data-ev-prov-part-status={c.part_status}
        data-ev-prov-row={c.row_status ?? undefined} data-ev-prov-cell-status={c.cell_status ?? undefined}>
      <p>
        <span className="mono mr-1 text-faint">{c.id}</span>
        <span>{c.title}</span>
        {' — '}
        <span style={{ color: TONE[lead.code] ?? 'var(--text-dim)' }} data-ev-prov-status="">{lead.text}</span>
        {!main && accepted && <span className="text-dim"> · {accepted}</span>}
      </p>
      {second && <p className="text-2xs text-dim" data-ev-prov-period="">{second}</p>}
      {c.detail && <p className="text-2xs text-faint" data-ev-prov-detail="">{c.detail}</p>}
    </li>
  )
}
