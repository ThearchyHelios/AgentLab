import {
  useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent,
} from 'react'
import { Link } from 'react-router-dom'
import { ArrowRight, ChevronRight, Download, FileText, Sparkles } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import { isUpgradeAdvice } from '../../canvas/issues'
import { ErrorState, Skeleton, Spinner, StatusBadge, rovingTarget, toast, useRadioGroup } from '../../components/ui'
import {
  EVIDENCE_STATE, PROBLEM_GROUPS, auditCsv, auditFromApi, auditRows, groupRows, guessRows, guessesOf, issuanceMarks,
  segmentState, type AuditFilter, type AuditGroup, type AuditRow,
} from '../../lib/evidence'
import { formatNumber } from '../../lib/format'
import { EVIDENCE_AUDIT_TEXT as T, UPGRADE_TEXT } from '../../lib/terms'
import { EvidenceDoc, type EvidenceDocHandle } from '../../run/EvidenceDoc'
import { EvidenceGuessView } from '../../run/EvidenceGuess'
import { Markdown, inlineStyles } from '../../run/Markdown'
import { useEvidence } from '../../store/evidence'
import { useCatalog } from '../../store/catalog'
import { EDIT_LOCK_TEXT, editLockOf, useStudio } from '../../store/studio'
import type { EvidenceAudit, EvidenceDocData, EvidenceGraph, GraphSpec, Run } from '../../types'

/**
 * 记录页的「证据」页签（方案 6.4）：左边报告，右边常驻的证据面板，报告下面是整张审计表。
 *
 * - 报告按证据图里封存的那几份画（片段接口也只认它们）：文档按工件 id 取、取回时后端复验哈希；
 *   证据图说哈希对不上的，不画成可点的报告，醒目地说它疑似被改过。
 * - 审计表按状态分组（无证据、可疑实体在前），可以只看有问题的两组，可以导出 JSON / CSV
 *   （后端 /evidence/audit?format=…；老后端没有这个接口时按页面上的清单导出，并照实说）。
 *   键盘用户在表里能看全每一处：一个 Tab 位，↑/↓ 逐行走，回车在右边打开那一段。
 * - 没有报告的运行照实说是哪种：没有证据（none）、旧版出具按数值匹配（legacy_contract）、
 *   没有契约按数值猜的候选（legacy_text，默认收起，写明不能当证据）。
 *
 * 窄的时候（详情栏不到 880px）不留右栏：面板从底部抽出，和问数据页一样。
 */
export function EvidencePane({ run, output, labelOf, refreshKey }: {
  run: Run
  /** 这次运行的成果（旧契约按位置标数字时要取那个字段的原文） */
  output: Record<string, any> | null
  labelOf: (id?: string | null) => string | undefined
  /** 运行又往前走了（事件数、状态变了）：证据图和审计表重取 */
  refreshKey: string
}) {
  const graphSlot = useEvidence((s) => s.graphs[run.id])
  const loadGraph = useEvidence((s) => s.loadGraph)
  const reloadGraph = useEvidence((s) => s.reloadGraph)
  const first = useRef(true)
  useEffect(() => {
    if (first.current) { first.current = false; void loadGraph(run.id); return }
    void reloadGraph(run.id)
  }, [run.id, refreshKey, loadGraph, reloadGraph])
  const audit = useAudit(run.id, refreshKey)
  const graph = graphSlot?.data

  const [root, setRoot] = useState<HTMLDivElement | null>(null)
  const wide = useWide(root, 880)
  const [dock, setDock] = useState<HTMLElement | null>(null)
  const [opened, setOpened] = useState<Set<string>>(() => new Set())
  const handles = useRef(new Map<string, EvidenceDocHandle | null>())

  // 报告文档：证据图里封存的那几份，按工件 id 取（store 永久缓存）
  const reports = useMemo(() => (graph?.mode === 'cited' ? graph.reports ?? [] : [])
    .filter((r) => typeof r?.doc_artifact === 'string' && !!r.doc_artifact && r.hash_ok !== false), [graph])
  const docs = useEvidence((s) => s.docs)
  const loadDoc = useEvidence((s) => s.loadDoc)
  useEffect(() => { for (const r of reports) void loadDoc(r.doc_artifact!) }, [reports, loadDoc])
  const docOf = useCallback((node?: string): EvidenceDocData | undefined => {
    const r = reports.find((x) => x.node_id === node)
    const slot = r?.doc_artifact ? docs[r.doc_artifact] : undefined
    return slot?.status === 'ok' ? slot.data : undefined
  }, [reports, docs])
  const guesses = useMemo(() => guessesOf(graph), [graph])

  // 审计表的行：后端的审计接口优先（每行的封存核对是后端做的）；老后端没有它时按页面上的文档、猜测自己拼
  const fallback = audit.status === 'unsupported' || audit.status === 'error'
  const rows = useMemo<AuditRow[]>(() => {
    if (audit.status === 'ok') return auditFromApi(audit.data)
    if (!fallback || !graph) return []
    if (graph.mode === 'legacy_text') return guesses.flatMap(guessRows)
    return reports.flatMap((r) => {
      const doc = docOf(r.node_id)
      return doc ? auditRows(doc, { report: r.node_id, graph }) : []
    })
  }, [audit, fallback, graph, guesses, reports, docOf])
  const [filter, setFilter] = useState<AuditFilter>('all')

  // 哪些行能在正文里打开：`报告:片段` 的集合，一份文档算一次（长报告几千个片段，不能每行各扫一遍）。
  // 只收有状态的片段，和 EvidenceDoc 能打开的是同一批：违规行指着的文字片段（粗体、链接里的可疑名字）、
  // 结构片段（列表序号 100. 里的数字）在正文里没有能点的地方，画成按钮就是点了没反应
  const openable = useMemo(() => {
    const keys = new Set<string>()
    for (const r of reports) {
      const doc = docOf(r.node_id)
      for (const b of doc?.blocks ?? []) for (const u of b.units ?? []) for (const seg of u.segments ?? []) {
        if (segmentState(seg)) keys.add(`${r.node_id}:${seg.id}`)
      }
    }
    return keys
  }, [reports, docOf])
  const canOpen = useCallback((row: AuditRow) => !!row.seg && openable.has(`${row.report}:${row.seg}`), [openable])
  const openRow = useCallback((row: AuditRow) => {
    if (row.seg) handles.current.get(row.report)?.open(row.seg)
  }, [])

  const onView = useCallback((report: string, open: boolean) => {
    setOpened((prev) => {
      if (prev.has(report) === open) return prev
      const next = new Set(prev)
      if (open) next.add(report)
      else next.delete(report)
      return next
    })
  }, [])

  const mode = graph?.mode ?? audit.data?.mode
  const seal = audit.data?.seal ?? graph?.seal

  if (!graph && graphSlot?.status !== 'error') {
    return (
      <div className="p-4" aria-busy="true" data-evidence-pane="loading">
        <Skeleton rows={6} height={16} />
      </div>
    )
  }
  if (!graph) {
    return <ErrorState error={graphSlot?.error} onRetry={() => void reloadGraph(run.id)} className="flex-1" />
  }

  return (
    <div ref={setRoot} className="flex h-full min-h-0 min-w-0" data-evidence-pane={mode ?? ''} data-evidence-wide={wide ? '1' : '0'}>
      <div className="min-h-0 min-w-0 flex-1 overflow-y-auto" data-evidence-main="">
        <div className="space-y-4 px-4 py-3">
          <SummaryBar mode={mode} seal={seal} message={audit.data?.seal?.message}
                      reports={graph.reports ?? []} labelOf={labelOf} />
          <UpgradeBanner run={run} mode={mode} />

          {mode === 'cited' && reports.map((r) => {
            const doc = docOf(r.node_id)
            const slot = r.doc_artifact ? docs[r.doc_artifact] : undefined
            return (
              <section key={r.node_id} className="rounded-lg border bg-panel p-3" data-evidence-report={r.node_id}>
                <header className="mb-2 flex min-w-0 items-center gap-1.5 text-2xs text-faint">
                  <FileText size={11} aria-hidden />
                  <span className="min-w-0 truncate">
                    报告「{labelOf(r.node_id) ?? r.node_id}」{r.fields?.length ? ` · 成果字段 ${r.fields.join('、')}` : ''}
                  </span>
                </header>
                {doc ? (
                  <EvidenceDoc ref={(h) => { handles.current.set(r.node_id!, h) }} doc={doc} artifact={r.doc_artifact}
                               runId={run.id} runClass={run.run_class} label={r.fields?.join('、') || r.node_id} tally
                               panel={wide ? 'dock' : 'auto'} dock={dock} onView={(o) => onView(r.node_id!, o)} />
                ) : slot?.status === 'error' ? (
                  <p className="text-2xs" style={{ color: 'var(--st-failed)' }}>{T.docGone(labelOf(r.node_id) ?? r.node_id ?? '')}</p>
                ) : <Skeleton rows={4} height={14} />}
              </section>
            )
          })}
          {/* 证据图说哈希对不上的报告：不画成可点的，醒目地说 */}
          {mode === 'cited' && (graph.reports ?? []).filter((r) => r.hash_ok === false).map((r) => (
            <p key={r.node_id} className="rounded border px-2 py-1.5 text-2xs" data-evidence-report-bad={r.node_id}
               style={{ color: 'var(--st-failed)', borderColor: 'var(--st-failed)', background: 'var(--st-failed-soft)' }}>
              {T.docBad(labelOf(r.node_id) ?? r.node_id ?? '')}
            </p>
          ))}

          {mode !== 'cited' && (
            <ModeNote mode={mode} graph={graph} output={output} guesses={guesses} />
          )}

          <AuditTable runId={run.id} rows={rows} filter={filter} onFilter={setFilter} canOpen={canOpen} onOpen={openRow}
                      loading={audit.status === 'loading' && !rows.length} fallback={fallback && rows.length > 0}
                      unsupported={audit.status === 'unsupported'} error={audit.status === 'error' ? audit.error : undefined}
                      multiReport={reports.length > 1} labelOf={labelOf} mode={mode} />
        </div>
      </div>
      {wide && (
        <aside ref={setDock} className="relative flex w-[380px] min-w-0 shrink-0 flex-col border-l" data-evidence-dock=""
               aria-label="证据面板">
          {!opened.size && (
            <p className="m-auto max-w-[260px] px-4 text-center text-2xs leading-relaxed text-faint" data-evidence-dock-idle="">
              {mode === 'cited' ? T.panelIdle : T.mode[mode ?? 'none'] ?? T.mode.none}
            </p>
          )}
        </aside>
      )}
    </div>
  )
}

/** 已经收尾的运行：还在跑、停在审批上的，报告可能还没写，说「这次的报告没有逐段证据」太早 */
const SETTLED = new Set(['succeeded', 'failed', 'cancelled'])

/**
 * 工作流现在的图能不能一键升级：拿它去校验，看有没有 evidence.upgrade_available 这条建议。老后端不给
 * 这条建议，横幅也就不出现（它也没有升级接口）。按工作流 id 和版本记住答案：同一张图的几条运行不重复问；
 * 问失败了不记，下次打开再问
 */
const upgradable = new Map<string, Promise<boolean>>()
function useUpgradable(workflowId: string | null, version: number | undefined, graph: GraphSpec | undefined, enabled: boolean) {
  const [yes, setYes] = useState(false)
  useEffect(() => {
    setYes(false)
    if (!enabled || !workflowId || !graph?.nodes?.length) return
    let alive = true
    const key = `${workflowId}@${version ?? ''}`
    let ask = upgradable.get(key)
    if (!ask) {
      ask = api.workflows.validate(graph).then((r) => (r.issues ?? []).some(isUpgradeAdvice))
      ask.catch(() => { if (upgradable.get(key) === ask) upgradable.delete(key) })
      upgradable.set(key, ask)
    }
    ask.then((v) => { if (alive) setYes(v) }, () => undefined)
    return () => { alive = false }
    // graph 跟着版本走：同一个版本的图不会变
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowId, version, enabled])
  return yes
}

/**
 * 没有逐段证据的运行（none、旧版出具、没有契约的旧运行）：工作流现在的图可以升级的，给一条横幅，
 * 跳到编排页打开升级预览。画布正锁着这张工作流（正式运行在跑、助手在改）时不给入口，说清为什么
 */
function UpgradeBanner({ run, mode }: { run: Run; mode?: string }) {
  const wf = useCatalog((s) => (run.workflow_id ? s.workflows.find((w) => w.id === run.workflow_id) : undefined))
  const legacy = mode === 'none' || !!mode?.startsWith('legacy')
  const yes = useUpgradable(wf?.id ?? null, wf?.version, wf?.graph, legacy && SETTLED.has(run.status))
  const lock = useStudio((s) => (wf && s.workflow?.id === wf.id ? editLockOf(s) : null))
  if (!yes || !wf) return null
  return (
    <section className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-lg border px-3 py-2" data-upgrade-banner=""
             aria-label={UPGRADE_TEXT.runBanner}
             style={{ borderColor: 'color-mix(in srgb, var(--accent) 35%, var(--border))',
                      background: 'color-mix(in srgb, var(--accent) 5%, var(--bg-panel))' }}>
      <Sparkles size={13} className="shrink-0" style={{ color: 'var(--accent)' }} aria-hidden />
      <span className="min-w-0 flex-1 text-xs">
        <span className="font-medium text-fg">{UPGRADE_TEXT.runBanner}</span>
        <span className="mt-0.5 block text-2xs leading-relaxed text-faint">
          {UPGRADE_TEXT.runHint}{run.run_class === 'formal' ? `。${UPGRADE_TEXT.runFormal}` : ''}
        </span>
      </span>
      {lock ? (
        <span className="text-2xs" style={{ color: 'var(--st-waiting)' }} data-upgrade-banner-locked="">
          {UPGRADE_TEXT.runLocked(EDIT_LOCK_TEXT[lock])}
        </span>
      ) : (
        <Link className="btn btn-sm shrink-0" to={`/studio/${encodeURIComponent(wf.id)}?upgrade=1`} data-upgrade-link=""
              title={UPGRADE_TEXT.runHint}>
          <ArrowRight size={11} aria-hidden /> {UPGRADE_TEXT.runAction}
        </Link>
      )}
    </section>
  )
}

/** 详情栏宽到能放下右栏吗：按容器量，不按屏幕（左边还有运行列表） */
function useWide(el: HTMLElement | null, min: number): boolean {
  const [wide, setWide] = useState(true)
  useLayoutEffect(() => {
    if (!el || typeof ResizeObserver !== 'function') return
    const on = () => setWide(el.getBoundingClientRect().width >= min)
    on()
    const ro = new ResizeObserver(on)
    ro.observe(el)
    return () => ro.disconnect()
  }, [el, min])
  return wide
}

type AuditState =
  | { status: 'loading'; data?: EvidenceAudit; error?: undefined }
  | { status: 'ok'; data: EvidenceAudit; error?: undefined }
  | { status: 'unsupported'; data?: undefined; error?: undefined }
  | { status: 'error'; data?: undefined; error: unknown }

/** 审计表的数据。接口不存在（老后端：404 且没有机读码）算「不支持」，退回页面自己拼 */
function useAudit(runId: string, key: string): AuditState {
  const [state, setState] = useState<AuditState>({ status: 'loading' })
  useEffect(() => {
    const ctrl = new AbortController()
    setState((s) => (s.status === 'ok' ? { status: 'loading', data: s.data } : { status: 'loading' }))
    api.evidence.audit(runId, { signal: ctrl.signal }).then(
      (data) => setState({ status: 'ok', data }),
      (error) => {
        if (ctrl.signal.aborted) return
        const missing = error instanceof ApiError && (error.status === 404 || error.status === 405) && !error.code
        setState(missing ? { status: 'unsupported' } : { status: 'error', error })
      },
    )
    return () => ctrl.abort()
  }, [runId, key])
  return state
}

/** 顶上一行：这次运行按哪种方式展示证据、封存核对的结果、每份报告文档的哈希 */
function SummaryBar({ mode, seal, message, reports, labelOf }: {
  mode?: string
  seal?: EvidenceGraph['seal'] & { events?: number; message?: string }
  message?: string
  reports: NonNullable<EvidenceGraph['reports']>
  labelOf: (id?: string | null) => string | undefined
}) {
  const verdict: { status: 'done' | 'failed' | 'idle'; text: string } = !seal
    ? { status: 'idle', text: T.sealUnknown }
    : !seal.sealed ? { status: 'idle', text: T.sealOpen }
    : seal.ok === false ? { status: 'failed', text: T.sealBad }
    : { status: 'done', text: T.sealOk(seal.manifest_seq ?? seal.events) }
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-2xs" data-evidence-summary-bar="">
      <span className="chip" data-evidence-mode={mode ?? ''}>{T.modeLabel[mode ?? 'none'] ?? mode}</span>
      <span className="flex min-w-0 items-center gap-1.5" data-evidence-seal={verdict.status} title={message}>
        <StatusBadge status={verdict.status} size={12} decorative animate={false} />
        <span className="text-faint">{T.sealTitle}：</span>
        <span className={verdict.status === 'failed' ? undefined : 'text-dim'}
              style={verdict.status === 'failed' ? { color: 'var(--st-failed)' } : undefined}>{verdict.text}</span>
      </span>
      {reports.map((r) => {
        const name = labelOf(r.node_id) ?? r.node_id ?? ''
        const bad = r.hash_ok === false
        const gone = r.hash_ok == null
        return (
          <span key={r.node_id} className="min-w-0" data-evidence-doc-hash={bad ? 'bad' : gone ? 'gone' : 'ok'}
                style={{ color: bad ? 'var(--st-failed)' : gone ? 'var(--st-waiting)' : 'var(--text-faint)' }}>
            {bad ? T.docBad(name) : gone ? T.docGone(name) : T.docOk(name)}
          </span>
        )
      })}
    </div>
  )
}

/** 没有逐段引用的运行：照实说是哪一种，旧契约把回指的数字按位置标在原文上，没有契约的给收起的猜测 */
function ModeNote({ mode, graph, output, guesses }: {
  mode?: string; graph: EvidenceGraph; output: Record<string, any> | null; guesses: ReturnType<typeof guessesOf>
}) {
  const legacy = graph.mode === 'legacy_contract' && graph.legacy && !Array.isArray(graph.legacy) ? graph.legacy : null
  const field = typeof legacy?.field === 'string' ? legacy.field : null
  const text = field && typeof output?.[field] === 'string' ? output[field] as string : null
  const matched: any[] = Array.isArray(legacy?.matched) ? legacy!.matched : []
  const unmatched: any[] = Array.isArray(legacy?.unmatched) ? legacy!.unmatched : []
  return (
    <section className="space-y-2" data-evidence-mode-note={mode ?? 'none'}>
      <p className="rounded border px-2.5 py-1.5 text-xs leading-relaxed text-dim">
        {T.mode[mode ?? 'none'] ?? T.mode.none}
        {graph.note && graph.note !== T.mode[mode ?? 'none'] && <span className="mt-0.5 block text-2xs text-faint">{graph.note}</span>}
      </p>
      {legacy && (
        <div className="rounded-lg border bg-panel p-3" data-evidence-legacy-contract="">
          <div className="mb-1.5 flex flex-wrap gap-x-2 text-2xs text-faint">
            <span>{legacy.note ?? T.mode.legacy_contract}</span>
            <span>· {T.legacyMatched(matched.length)}</span>
            {unmatched.length > 0 && <span style={{ color: 'var(--st-waiting)' }}>· {T.legacyUnmatched(unmatched.length)}</span>}
          </div>
          {text
            ? <Markdown text={text} marks={issuanceMarks({ matched, unmatched_numbers: unmatched })} />
            : <p className="text-2xs text-dim">{T.legacyNoField}</p>}
        </div>
      )}
      {mode === 'legacy_text' && guesses.length > 0 && (
        <div className="rounded-lg border bg-panel p-3" data-evidence-legacy-text="">
          <EvidenceGuessView guesses={guesses} />
        </div>
      )}
    </section>
  )
}

/**
 * 审计表：按状态分组，一组一个 tbody。「旧运行猜测」那组默认收起（猜的，不能当证据）。
 * 键盘：整张表一个 Tab 位（roving），↑/↓ 逐行走、Home/End 到头尾，回车或点击在右边打开那一段
 */
function AuditTable({
  runId, rows, filter, onFilter, canOpen, onOpen, loading, fallback, unsupported, error, multiReport, labelOf, mode,
}: {
  runId: string
  rows: AuditRow[]
  filter: AuditFilter
  onFilter: (f: AuditFilter) => void
  canOpen: (row: AuditRow) => boolean
  onOpen: (row: AuditRow) => void
  loading: boolean
  /** 行是页面自己拼的（审计接口不支持或没取到） */
  fallback: boolean
  unsupported: boolean
  error?: unknown
  multiReport: boolean
  labelOf: (id?: string | null) => string | undefined
  mode?: string
}) {
  const groups = useMemo(() => groupRows(rows, filter), [rows, filter])
  const [collapsed, setCollapsed] = useState<Set<AuditGroup>>(() => new Set(['candidate']))
  const visible = useMemo(() => groups.flatMap((g) => (collapsed.has(g.group) ? [] : g.rows)), [groups, collapsed])
  const [current, setCurrent] = useState<string | null>(null)
  const cur = current && visible.some((r) => r.key === current) ? current : visible[0]?.key ?? null
  const tableRef = useRef<HTMLTableElement>(null)
  const keysId = useId()
  const radio = useRadioGroup<AuditFilter>(['all', 'problems'], filter, onFilter)
  const [exporting, setExporting] = useState<null | 'json' | 'csv'>(null)
  const problems = rows.filter((r) => r.group === 'none' || r.group === 'suspicious').length

  // 焦点当场挪过去（每一行都画着，只是 tabIndex=-1）：等下一帧再挪的话，连按几下方向键会落在同一行上
  const focusRow = (key: string) => {
    setCurrent(key)
    tableRef.current?.querySelector<HTMLElement>(`[data-audit-focus="${CSS.escape(key)}"]`)?.focus()
  }
  const onKey = (e: ReactKeyboardEvent, key: string) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp' && e.key !== 'Home' && e.key !== 'End') return
    const i = visible.findIndex((r) => r.key === key)
    // ↑/↓ 当作一维走（rovingTarget 按 both 认上下），到头停住、不首尾相接：表格读到底就是底
    const to = e.key === 'ArrowDown' ? Math.min(visible.length - 1, i + 1)
      : e.key === 'ArrowUp' ? Math.max(0, i - 1)
      : rovingTarget(e, i, visible.length, 'both')
    if (to < 0 || to === i) { e.preventDefault(); return }
    e.preventDefault()
    focusRow(visible[to].key)
  }

  const exportAs = async (format: 'json' | 'csv') => {
    setExporting(format)
    const wanted = filter === 'problems' ? [...PROBLEM_GROUPS] : undefined
    try {
      const { blob, name } = await api.evidence.auditExport(runId, format, wanted)
      save(name, blob)
      toast.ok(T.exported(name))
    } catch (e) {
      // 老后端没有导出接口：按页面上这张清单导出，并照实说没经过后端的封存核对
      if (e instanceof ApiError && (e.status === 404 || e.status === 405) && !e.code) {
        const shown = groupRows(rows, filter).flatMap((g) => g.rows)
        const name = `evidence-${runId.slice(0, 8)}.${format}`
        save(name, new Blob([format === 'csv' ? `﻿${auditCsv(shown)}` : JSON.stringify({
          run_id: runId, mode, source: 'page', groups: groupRows(rows, filter).map((g) => ({ key: g.group, rows: g.rows })),
        }, null, 2)], { type: format === 'csv' ? 'text/csv;charset=utf-8' : 'application/json' }))
        toast.info(T.exportLocal)
      } else {
        toast.error(e, { key: 'evidence:export' })
      }
    } finally {
      setExporting(null)
    }
  }

  return (
    <section className="rounded-lg border bg-panel" data-evidence-audit="" aria-labelledby={`${keysId}-title`}>
      <header className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
        <h3 id={`${keysId}-title`} className="text-xs font-semibold">{T.title}</h3>
        <span className="tnum text-2xs text-faint">{formatNumber(rows.length)} 处</span>
        <div role="radiogroup" aria-label={T.filterLabel} className="inline-flex rounded-md border p-px text-2xs" data-audit-filter={filter}>
          {(['all', 'problems'] as const).map((f) => (
            <button key={f} type="button" {...radio(f)} onClick={() => onFilter(f)} data-audit-filter-option={f}
                    className={clsx('rounded px-2 py-0.5', filter === f ? 'bg-accent-soft text-fg' : 'text-faint hover:text-dim')}>
              {f === 'all' ? T.filterAll : `${T.filterProblems}（${formatNumber(problems)}）`}
            </button>
          ))}
        </div>
        <span className="flex-1" />
        {(['json', 'csv'] as const).map((f) => (
          <button key={f} type="button" className="btn btn-xs" disabled={!!exporting} onClick={() => void exportAs(f)}
                  data-audit-export={f} title={filter === 'problems' ? `只导出${T.filterProblems.replace('只看', '')}` : undefined}>
            {exporting === f ? <Spinner size={10} /> : <Download size={10} aria-hidden />} {f === 'json' ? T.exportJson : T.exportCsv}
          </button>
        ))}
      </header>
      {fallback && (
        <p className="border-b px-3 py-1 text-2xs text-faint" data-audit-fallback={unsupported ? 'unsupported' : 'error'}>
          {unsupported ? T.fallbackUnsupported : T.fallbackError(error instanceof Error ? error.message : '未知原因')}
        </p>
      )}
      <p id={keysId} className="sr-only">{T.keys}</p>
      {loading ? <div className="p-3"><Skeleton rows={4} height={14} /></div>
        : !groups.length ? (
          <p className="px-3 py-3 text-2xs text-faint" data-audit-empty="">{filter === 'problems' ? T.emptyProblems : T.empty}</p>
        ) : (
          <div className="overflow-x-auto">
            <table ref={tableRef} className="w-full min-w-[560px] table-fixed border-collapse text-xs" aria-describedby={keysId}
                   aria-labelledby={`${keysId}-title`} data-audit-table="">
              {/* 定宽：出处是长文字，按内容分宽的话会被挤成一个字宽。所在的句子放在出处下面一行，不另占一列 */}
              <colgroup>
                <col style={{ width: '24%' }} />
                <col style={{ width: '7rem' }} />
                <col />
                {multiReport && <col style={{ width: '6rem' }} />}
                <col style={{ width: '5.5rem' }} />
              </colgroup>
              <thead>
                <tr className="text-left text-2xs text-faint">
                  <th scope="col" className="px-3 py-1.5 font-medium">{T.cols.text}</th>
                  <th scope="col" className="px-2 py-1.5 font-medium">{T.cols.state}</th>
                  <th scope="col" className="px-2 py-1.5 font-medium">{T.cols.source}<span className="font-normal">（{T.cols.sentence}）</span></th>
                  {multiReport && <th scope="col" className="px-2 py-1.5 font-medium">{T.cols.report}</th>}
                  <th scope="col" className="px-3 py-1.5 text-right font-medium">{T.cols.seal}</th>
                </tr>
              </thead>
              {groups.map((g) => {
                const shut = collapsed.has(g.group)
                return (
                  <tbody key={g.group} data-audit-group={g.group} data-audit-collapsed={shut ? '1' : '0'}>
                    <tr className="border-t">
                      <th scope="colgroup" colSpan={multiReport ? 5 : 4} className="px-2 py-1 text-left">
                        <button type="button" className="inline-flex items-center gap-1.5 rounded px-1 py-0.5 text-2xs transition-colors hover:bg-hover"
                                aria-expanded={!shut} data-audit-group-toggle={g.group}
                                onClick={() => setCollapsed((prev) => {
                                  const next = new Set(prev)
                                  if (shut) next.delete(g.group)
                                  else next.add(g.group)
                                  return next
                                })}>
                          <ChevronRight size={11} aria-hidden className={clsx('text-faint', !shut && 'rotate-90')} />
                          <span className="font-semibold" style={{ color: groupColor(g.group) }}>{T.groups[g.group]}</span>
                          <span className="tnum text-faint">{formatNumber(g.rows.length)}</span>
                          <span className="font-normal text-faint">· {T.groupHint[g.group]}</span>
                        </button>
                      </th>
                    </tr>
                    {!shut && g.rows.map((r) => (
                      <AuditLine key={r.key} row={r} current={cur === r.key} openable={canOpen(r)} multiReport={multiReport}
                                 labelOf={labelOf} onOpen={() => { setCurrent(r.key); onOpen(r) }}
                                 onFocus={() => setCurrent(r.key)} onKey={(e) => onKey(e, r.key)} />
                    ))}
                  </tbody>
                )
              })}
            </table>
          </div>
        )}
    </section>
  )
}

/** 表格里的字按正文显示的样子写：去掉行内 Markdown 的语法字符（反引号、星号、链接的括号） */
function plain(text: string): string {
  if (!/[`*_~[]/.test(text)) return text
  const { hide } = inlineStyles(text)
  let out = ''
  for (let i = 0; i < text.length; i++) if (!hide[i]) out += text[i]
  return out
}

const groupColor = (g: AuditGroup): string =>
  g === 'none' || g === 'suspicious' ? 'var(--st-waiting)' : g === 'cited' ? 'var(--st-done)' : EVIDENCE_STATE.candidate.color

function AuditLine({ row, current, openable, multiReport, labelOf, onOpen, onFocus, onKey }: {
  row: AuditRow; current: boolean; openable: boolean; multiReport: boolean
  labelOf: (id?: string | null) => string | undefined
  onOpen: () => void; onFocus: () => void; onKey: (e: ReactKeyboardEvent) => void
}) {
  const meta = EVIDENCE_STATE[row.state]
  const text = plain(row.text)
  const sentence = plain(row.sentence)
  const label = `${text}，${meta.label}${row.source ? `：${row.source}` : ''}`
  const common = {
    tabIndex: current ? 0 : -1, 'data-audit-focus': row.key, onFocus, onKeyDown: onKey,
    className: clsx('mono max-w-full truncate rounded-sm text-left outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[color:var(--accent)]',
      row.kind === 'claim' && 'font-sans'),
  }
  return (
    <tr className="border-t align-top hover:bg-hover" data-audit-row={row.key} data-audit-state={row.state}
        data-audit-seg={row.seg ?? ''}>
      <td className="px-3 py-1.5">
        {openable ? (
          <button type="button" {...common} aria-label={label} title={label} onClick={onOpen} data-audit-open={row.seg}
                  className={clsx(common.className, 'block underline decoration-dotted underline-offset-2 hover:text-[var(--accent)]')}>
            {text}
          </button>
        ) : (
          <span {...common} className={clsx(common.className, 'block')} title={text}>{text}</span>
        )}
      </td>
      <td className="px-2 py-1.5" style={{ color: meta.color }}>
        {meta.glyph && <span aria-hidden className="mr-0.5">{meta.glyph}</span>}{meta.label}
      </td>
      <td className="px-2 py-1.5">
        <span className="block text-dim [overflow-wrap:anywhere]" data-audit-source="">{row.source || '—'}</span>
        {/* 打不开的违规行（列表序号里的数字、粗体和链接里的可疑名字）：说清为什么只在表里 */}
        {row.kind === 'violation' && !openable && (
          <span className="mt-0.5 block text-2xs text-faint" title={T.hiddenWhy} data-audit-hidden="">{T.hidden}</span>
        )}
        {sentence && sentence !== text && (
          <span className="mt-0.5 block text-2xs text-faint" title={sentence} data-audit-sentence=""
                style={{ display: '-webkit-box', WebkitLineClamp: 1, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>
            「{sentence}」
          </span>
        )}
      </td>
      {multiReport && <td className="whitespace-nowrap px-2 py-1.5 text-faint">{labelOf(row.report) ?? row.report}</td>}
      <td className="whitespace-nowrap px-3 py-1.5 text-right text-2xs" data-audit-sealed={row.sealed == null ? 'na' : row.sealed ? 'yes' : 'no'}>
        {row.sealed == null ? <span className="text-faint">{T.sealNa}</span>
          : row.sealed ? <span style={{ color: 'var(--st-done)' }}>{T.sealIn}</span>
          : <span style={{ color: 'var(--st-waiting)' }}>{T.sealOut}</span>}
      </td>
    </tr>
  )
}

/** 存成文件：同 AssistantStream 的下载，CSV 由后端带了 BOM（Excel 按 GBK 猜编码，不带的话中文列名乱码） */
function save(name: string, blob: Blob) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
