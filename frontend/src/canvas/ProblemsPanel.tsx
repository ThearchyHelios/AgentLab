import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle, ArrowRight, Check, CheckCircle2, CornerDownRight, ListChecks, Plus, RotateCw, ShieldCheck, Sparkles,
  Trash2, Unlink, Wand2, Workflow, XCircle,
} from 'lucide-react'
import clsx from 'clsx'
import { NODE_DEFS } from './nodeDefs'
import {
  fixFieldLabel, fixValueLines, fixValueText, isBareUpgradeAdvice, isUpgradeAdvice, problemsOf, unboundToolOf, upgradeChangeKind,
  upgradeChangesFromDiff, upgradeEdgeText, upgradeGroups, upgradeNodeText, upgradeRuleText, upgradeStepText, upgradeSummary,
  upgradeTypeText, withToolBound, type FieldRef, type NodeNameOf, type Problem,
} from './issues'
import { PublishPreflightPane } from './PublishDialog'
import { humanizeError } from '../lib/errors'
import { formatShortcut } from '../lib/keys'
import { PUBLISH_FIX_TEXT, UPGRADE_TEXT } from '../lib/terms'
import { useCatalog } from '../store/catalog'
import { EDIT_LOCK_TEXT, upgradeUnsaved, useEditLock, useStudio, type UpgradeFlow } from '../store/studio'
import { Spinner, useRadioGroup } from '../components/ui'
import type { UpgradeChange, ValidationIssue } from '../types'

/**
 * 问题面板：图级、节点、边上的 error 和 warning，一条一行，点一下定位。
 *
 * 以前只有工具栏上一个点不动的「2 个错误」：图级的（找不到入口）只体现在计数里，
 * 边上的根本没地方看，有 error 时 warning 的计数还被藏起来——而 warning 恰恰是
 * 「取到空值」这类运行期静默出错的那一类。现在按节点分组（图级在最前），
 * 点一行选中节点、镜头对过去、检查器翻到出问题的那个字段。F8 / ⇧F8 在问题之间跳。
 */
export function ProblemsPane({ problems, activeId, onLocate, onDeleteEdge }: {
  problems: Problem[]
  activeId: string | null
  onLocate: (p: Problem) => void
  onDeleteEdge: (edgeId: string) => void
}) {
  // 校验：画布上随改随查的那一份。发布前检查：按选定的发布等级另查一遍门禁，带修法——
  // 在画布上就能提前发现、提前修，不用等到点发布那一刻
  const [mode, setMode] = useState<'lint' | 'publish'>('lint')
  const radio = useRadioGroup(MODES, mode, setMode)
  const hasWorkflow = useStudio((s) => !!s.workflow)
  // 又开了一次升级预览（记录页的横幅跳过来、点了快速修复）：它画在「校验」这一页，停在发布前检查上就看不见
  const upgradeSeq = useStudio((s) => s.upgrade?.seq)
  useEffect(() => { if (upgradeSeq != null) setMode('lint') }, [upgradeSeq])
  const errors = problems.filter((p) => p.level === 'error').length
  return (
    <>
      {hasWorkflow && (
        <div className="flex shrink-0 items-center gap-1 px-2 pt-1.5" role="radiogroup" aria-label="问题面板视图">
          {MODES.map((m) => (
            <button key={m} type="button" {...radio(m)} data-problems-mode={m}
                    onClick={() => setMode(m)}
                    className={clsx('flex items-center gap-1 rounded px-2 py-0.5 text-2xs transition-colors',
                      mode === m ? 'bg-hover text-fg' : 'text-faint hover:text-dim')}>
              {m === 'lint'
                ? <><ListChecks size={11} aria-hidden /> {PUBLISH_FIX_TEXT.lint}
                    {errors > 0 && <span className="tnum" style={{ color: 'var(--err)' }}>{errors}</span>}</>
                : <><ShieldCheck size={11} aria-hidden /> {PUBLISH_FIX_TEXT.section}</>}
            </button>
          ))}
        </div>
      )}
      {mode === 'publish' && hasWorkflow
        ? <PublishPreflightPane onLocate={(issue) => {
            const { nodes } = useStudio.getState()
            const [p] = problemsOf([issue], nodes)
            if (p) onLocate(p)
          }} />
        : <LintPane problems={problems} activeId={activeId} onLocate={onLocate} onDeleteEdge={onDeleteEdge} />}
    </>
  )
}

const MODES = ['lint', 'publish'] as const

function LintPane({ problems, activeId, onLocate, onDeleteEdge }: {
  problems: Problem[]
  activeId: string | null
  onLocate: (p: Problem) => void
  onDeleteEdge: (edgeId: string) => void
}) {
  const nodes = useStudio((s) => s.nodes)
  const analysis = useStudio((s) => s.analysis)
  const analysisError = useStudio((s) => s.analysisError)
  const analyzeNow = useStudio((s) => s.analyzeNow)
  const offline = useCatalog((s) => s.backend === 'down')
  const list = useRef<HTMLDivElement>(null)
  // 「可以升级为可追溯结构」：校验给的建议，不是问题。预览开着时建议可能已经没了（又校验过一次），预览照样留着
  const upgradeAdvice = useStudio((s) => s.advice.find(isUpgradeAdvice))
  const upgrading = useStudio((s) => !!s.upgrade)
  const upgrade = upgradeAdvice || upgrading ? <UpgradeBlock advice={upgradeAdvice} /> : null

  // F8 跳到的那一条要在视野里
  useEffect(() => {
    if (!activeId) return
    list.current?.querySelector(`[data-problem="${CSS.escape(activeId)}"]`)
      ?.scrollIntoView({ block: 'nearest' })
  }, [activeId])

  // 只有后端整个断了，恢复连接时才会自动重跑（useOnReconnect）；校验接口自己报错
  // 不会有人替你再试，这时候承诺「会自动重新校验」就是在让人干等
  const retryHint = offline ? '与服务端恢复连接后将自动重新校验' : '点击「重试」重新校验'
  if (analysis === 'failed' && !problems.length) {
    const failed = (
      <div className={clsx('flex flex-col items-center justify-center gap-2 px-6 text-center text-2xs', upgrade ? 'py-3' : 'flex-1')}
           data-analysis-failed="">
        <span style={{ color: 'var(--warn)' }}>分析失败{analysisError ? `：${analysisError}` : ''}</span>
        <span className="text-faint">暂时无法获取问题清单，画布仍可正常编辑。{retryHint}</span>
        <button type="button" className="btn btn-sm" onClick={() => void analyzeNow()}>
          <RotateCw size={11} /> 重试
        </button>
      </div>
    )
    // 升级块照样摆着：后端断开时保存和校验一起失败，这时候最要紧的正是「没存上 → 重试保存」
    if (!upgrade) return failed
    return (
      <div className="min-h-0 flex-1 overflow-y-auto py-1">
        {upgrade}
        {failed}
      </div>
    )
  }
  if (!problems.length) {
    const idle = (
      <div className={clsx('flex items-center justify-center gap-1.5 text-2xs text-faint', upgrade ? 'py-3' : 'flex-1')}>
        {analysis === 'pending'
          ? <><Spinner size={11} /> 正在校验…</>
          : <><CheckCircle2 size={12} style={{ color: 'var(--ok)' }} /> 未发现问题</>}
      </div>
    )
    if (!upgrade) return idle
    return (
      <div className="min-h-0 flex-1 overflow-y-auto py-1">
        {upgrade}
        {idle}
      </div>
    )
  }

  const byNode = new Map(nodes.map((n) => [n.id, n]))
  const groups: { key: string; title: React.ReactNode; items: Problem[] }[] = []
  for (const p of problems) {
    const key = p.scope === 'node' ? `n:${p.nodeId}` : p.scope
    let g = groups.find((x) => x.key === key)
    if (!g) {
      const node = p.nodeId ? byNode.get(p.nodeId) : undefined
      const def = node ? NODE_DEFS[node.data.nodeType] : undefined
      g = {
        key,
        title: p.scope === 'graph'
          ? <><Workflow size={11} className="shrink-0 text-faint" /> 整个工作流</>
          : p.scope === 'edge'
            ? <><Unlink size={11} className="shrink-0 text-faint" /> 连线</>
            : <>
                {def
                  ? <def.icon size={11} className={`nt-${node!.data.nodeType} shrink-0`} style={{ color: 'var(--nt)' }} />
                  : <span className="h-2.5 w-2.5 shrink-0 rounded-sm border" />}
                <span className="min-w-0 truncate text-fg">{node?.data.label || p.nodeId}</span>
                {def && <span className="shrink-0 text-faint">· {def.label}</span>}
              </>,
        items: [],
      }
      groups.push(g)
    }
    g.items.push(p)
  }

  return (
    <>
      {/* 这次没分析成：清单还是上一次校验的，照样列出来（多半仍然有效），但说清楚它可能过时 */}
      {analysis === 'failed' && (
        <div className="mx-2 mt-1.5 flex shrink-0 items-center gap-2 rounded-md border px-2.5 py-1.5 text-2xs"
             style={{ borderColor: 'var(--warn)' }}>
          <AlertTriangle size={11} className="shrink-0" style={{ color: 'var(--warn)' }} aria-hidden />
          <span className="min-w-0 flex-1">
            <span style={{ color: 'var(--warn)' }}>分析失败{analysisError ? `：${analysisError}` : ''}</span>
            <span className="text-faint"> · 以下为上一次校验的结果，可能已过时。{retryHint}</span>
          </span>
          <button type="button" className="btn btn-xs shrink-0" onClick={() => void analyzeNow()}>
            <RotateCw size={10} /> 重试
          </button>
        </div>
      )}
      <div ref={list} className="min-h-0 flex-1 overflow-y-auto py-1">
        {upgrade}
        <div role="list" aria-label="问题">
          {groups.map((g) => (
            <div key={g.key} className="mb-1">
              <div className="sticky top-0 z-[1] flex items-center gap-1.5 bg-panel px-3 py-1 text-2xs font-medium">
                {g.title}
                <span className="tnum ml-auto shrink-0 text-faint">{g.items.length}</span>
              </div>
              {g.items.map((p) => (
                <ProblemRow key={p.id} problem={p} active={p.id === activeId}
                            onLocate={() => onLocate(p)} onDeleteEdge={onDeleteEdge} />
              ))}
            </div>
          ))}
        </div>
        <div className="px-3 pb-1 pt-0.5 text-2xs text-faint">
          {formatShortcut('F8')} 下一个 · {formatShortcut('Shift+F8')} 上一个
        </div>
      </div>
    </>
  )
}

function ProblemRow({ problem: p, active, onLocate, onDeleteEdge }: {
  problem: Problem; active: boolean; onLocate: () => void; onDeleteEdge: (id: string) => void
}) {
  const node = useStudio((s) => (p.nodeId ? s.nodes.find((n) => n.id === p.nodeId) : undefined))
  const updateNode = useStudio((s) => s.updateNode)
  const locked = useEditLock() != null
  const err = p.level === 'error'
  const Icon = err ? XCircle : AlertTriangle
  const where = fieldLabel(p.field, node?.data.nodeType, node?.data.config)
  const locatable = p.scope === 'node' && !!node
  // 提示词点了名的工具没绑：就地绑上，和检查器里那个按钮是同一个修法
  const tool = unboundToolOf(p.message)
  const bound = node && tool ? withToolBound(node.data.config ?? {}, p.field, tool) : null
  return (
    <div role="listitem" data-problem={p.id}
         className={clsx('group flex items-start gap-2 pl-6 pr-2', active && 'bg-hover')}>
      <button
        type="button"
        disabled={!locatable}
        onClick={onLocate}
        title={locatable ? '选中该节点并定位到问题位置' : undefined}
        className={clsx('flex min-w-0 flex-1 items-start gap-2 rounded py-1 text-left text-2xs leading-snug',
          locatable ? 'cursor-pointer hover:text-fg' : 'cursor-default')}
      >
        <Icon size={11} className="mt-px shrink-0" style={{ color: err ? 'var(--err)' : 'var(--warn)' }}
              aria-label={err ? '错误' : '提示'} />
        <span className={clsx('min-w-0 flex-1', err ? 'text-fg' : 'text-dim')}>{p.message}</span>
        {where && (
          <span className="flex shrink-0 items-center gap-0.5 text-faint">
            <CornerDownRight size={9} aria-hidden />{where}
          </span>
        )}
      </button>
      {bound && tool && (
        <button type="button" className="btn btn-xs my-0.5 shrink-0" disabled={locked}
                title={`将 ${tool} 添加到${where || '该节点的工具'}`}
                onClick={() => updateNode(node!.id, { config: bound })}>
          <Plus size={10} aria-hidden /> 绑定 {tool}
        </button>
      )}
      {/* 悬空边（指向不存在的节点）：React Flow 不画它，画布上看不见、点不到，只能在这儿删 */}
      {p.scope === 'edge' && p.edgeId && (
        <button type="button" className="btn btn-xs my-0.5 shrink-0" onClick={() => onDeleteEdge(p.edgeId!)}>
          <Trash2 size={10} /> 删除这条无效连线
        </button>
      )}
    </div>
  )
}

// -------------------------------------------------------------------------
// 一键升级为可追溯结构：校验给了建议（evidence.upgrade_available）才有入口。老后端不给这条建议，
// 入口也就不出现。和发布前修复同一套规矩：先出预览，人点「应用」才走现有的保存存成草稿
// -------------------------------------------------------------------------

const reasonOf = (e: unknown) => {
  const h = humanizeError(e)
  return h.reason ? `${h.title}：${h.reason}` : h.title
}

/** 建议那一行加上预览。画布锁着（正式运行在跑、助手在改）不给入口，说清为什么 */
function UpgradeBlock({ advice }: { advice?: ValidationIssue }) {
  const upgrade = useStudio((s) => s.upgrade)
  const previewUpgrade = useStudio((s) => s.previewUpgrade)
  const lock = useEditLock()
  // 预览出来、换了状态（算完、出错）时滚到眼前：连同上面那句建议一起，别只露出半截预览
  const ref = useRef<HTMLElement>(null)
  const seq = upgrade?.seq
  const status = upgrade?.status
  useEffect(() => {
    if (seq != null) ref.current?.scrollIntoView({ block: 'nearest' })
  }, [seq, status])
  return (
    <section ref={ref} className="mx-2 mb-1.5 mt-0.5 rounded-md border px-2.5 py-1.5 text-2xs" aria-label={UPGRADE_TEXT.advice}
             style={{ borderColor: 'color-mix(in srgb, var(--accent) 35%, var(--border))' }} data-upgrade="">
      <div className="flex flex-wrap items-start gap-x-2 gap-y-1">
        <Sparkles size={11} className="mt-px shrink-0" style={{ color: 'var(--accent)' }} aria-hidden />
        <span className="min-w-0 flex-1 leading-snug">
          <span className="text-fg" data-upgrade-advice="">{advice?.message || UPGRADE_TEXT.advice}</span>
          {/* 后端的建议多半已经说了要改哪里、先预览；只有一句光秃秃的标题时才补这句说明 */}
          {(!advice?.message || isBareUpgradeAdvice(advice.message)) && (
            <span className="block text-faint">{UPGRADE_TEXT.adviceHint}</span>
          )}
        </span>
        {!lock && !upgrade && (
          <button type="button" className="btn btn-xs shrink-0" data-upgrade-action="" onClick={() => void previewUpgrade()}>
            <Wand2 size={10} aria-hidden /> {UPGRADE_TEXT.action}
          </button>
        )}
      </div>
      {lock && <div className="mt-1 text-faint" data-upgrade-locked="">{UPGRADE_TEXT.locked(EDIT_LOCK_TEXT[lock])}</div>}
      {upgrade && <UpgradePreview flow={upgrade} locked={!!lock} />}
    </section>
  )
}

/** 预览里念节点和字段要用的几样：名字、升级前后的类型、升级前的配置 */
interface UpgradeNames {
  nameOf: NodeNameOf
  /** 升级后的类型（新插入的节点也有） */
  typeOf: (id?: string | null) => string | undefined
  /** 升级前的类型：换成报告撰写之后去掉的那几项（用户提示、角色设定）按原来的叫法念 */
  wasTypeOf: (id?: string | null) => string | undefined
  configOf: (id?: string | null) => Record<string, any> | undefined
}

/**
 * 节点 id → 名字和类型：先认画布上的，再认升级后的图里的（新插入的报告节点只在那里）。跟着 issues
 * 和预览换新，不订阅 nodes：拖一下节点这一块不必跟着重渲染
 */
function useUpgradeNames(flow: UpgradeFlow): UpgradeNames {
  const tick = useStudio((s) => s.issues)
  const after = flow.status === 'ready' ? flow.result.graph : null
  const map = useMemo(() => {
    const m = new Map<string, { label: string; type: string }>()
    for (const n of after?.nodes ?? []) m.set(n.id, { label: n.data?.label || n.id, type: n.type })
    // 画布上的名字优先，类型取升级后的：改的是「写报告」这个节点，字段按报告撰写的叫法念
    for (const n of useStudio.getState().nodes) {
      m.set(n.id, { label: n.data.label || n.id, type: m.get(n.id)?.type ?? n.data.nodeType })
    }
    return m
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick, after])
  const nameOf = useCallback<NodeNameOf>((id) => map.get(id)?.label, [map])
  const typeOf = useCallback((id?: string | null) => (id ? map.get(id)?.type : undefined), [map])
  // 列表项（成果字段第几项）按改之前的配置认名字：改的正是它
  const base = flow.status === 'ready' ? flow.base : null
  const before = useCallback((id?: string | null) => (id ? base?.nodes.find((n) => n.id === id) : undefined), [base])
  const configOf = useCallback((id?: string | null) => before(id)?.data?.config, [before])
  const wasTypeOf = useCallback((id?: string | null) => before(id)?.type, [before])
  return { nameOf, typeOf, wasTypeOf, configOf }
}

/** 升级预览：逐项「节点 · 字段：原值 → 新值」，节点类型的变化写成「模型调用 → 报告撰写」，说明原样列出 */
function UpgradePreview({ flow, locked }: { flow: UpgradeFlow; locked: boolean }) {
  const dirty = useStudio((s) => s.dirty)
  const previewUpgrade = useStudio((s) => s.previewUpgrade)
  const applyUpgrade = useStudio((s) => s.applyUpgrade)
  const retryUpgradeSave = useStudio((s) => s.retryUpgradeSave)
  const discard = useStudio((s) => s.discardUpgrade)
  const names = useUpgradeNames(flow)
  const { nameOf } = names
  const T = UPGRADE_TEXT
  const P = PUBLISH_FIX_TEXT
  const box = 'mt-1.5 rounded-md border px-2.5 py-2'
  const apply = async () => {
    if (await applyUpgrade()) void useCatalog.getState().refresh()
  }
  const retrySave = async () => {
    if (await retryUpgradeSave()) void useCatalog.getState().refresh()
  }

  if (flow.status === 'loading') {
    return (
      <div className={clsx(box, 'flex items-center gap-2')} data-upgrade-preview="loading" aria-live="polite">
        <Spinner size={11} />
        <span className="min-w-0 flex-1 text-dim">{flow.assist ? T.assisting : T.previewing}</span>
        <button type="button" className="btn btn-xs" onClick={discard}>{P.stop}</button>
      </div>
    )
  }
  if (flow.status === 'unsupported' || flow.status === 'error') {
    return (
      <div className={clsx(box, 'flex flex-wrap items-center gap-2')} style={{ borderColor: 'var(--warn)' }}
           data-upgrade-preview={flow.status} role="alert">
        <AlertTriangle size={11} className="shrink-0" style={{ color: 'var(--warn)' }} aria-hidden />
        <span className="min-w-0 flex-1 [overflow-wrap:anywhere]" style={{ color: 'var(--warn)' }}>
          {flow.status === 'unsupported' ? T.unsupported : `${T.failed}：${reasonOf(flow.error)}`}
        </span>
        {flow.status === 'error' && !locked && (
          <button type="button" className="btn btn-xs" onClick={() => void previewUpgrade({ assist: flow.assist })}>
            <RotateCw size={10} aria-hidden /> {P.retry}
          </button>
        )}
        <button type="button" className="btn btn-xs" onClick={discard}>{P.discard}</button>
      </div>
    )
  }

  const { result, base } = flow
  // 后端没给逐项改动时，按前后两张图自己列：照样逐项写，不替它编原因
  const changes = result.changes.length ? result.changes : result.graph ? upgradeChangesFromDiff(base, result.graph) : []
  const summary = result.graph ? upgradeSummary(base, result.graph) : []
  const left = result.issues.filter((i) => i.level === 'error').length
  const usable = !!result.graph && changes.length > 0
  // 没采用的那几步，后端往往也在说明里写了一句「……：没有采用，原因」：说明里已经有这个原因的不再列一遍
  const rejected = result.rejected.filter((r) => !result.notes.some((n) => n.text.includes(r.reason)))
  const a = flow.apply
  const saving = a?.status === 'saving'
  const unsaved = upgradeUnsaved(a)
  return (
    <section className={clsx(box, 'space-y-1.5')} style={{ borderColor: 'var(--accent)' }}
             data-upgrade-preview="ready" aria-label="升级预览">
      <div className="font-medium">{changes.length ? T.title(changes.length) : T.noChange}</div>
      {summary.length > 0 && <div className="text-faint" data-upgrade-summary="">{T.summary(summary)}</div>}
      {changes.length > 0 && (
        <div className="space-y-1.5" data-upgrade-changes="">
          {upgradeGroups(changes).map((g) => (
            <div key={g.key} data-upgrade-step={g.rule ?? ''}>
              {g.label && (
                <div className="flex items-start gap-1 text-dim [overflow-wrap:anywhere]" data-upgrade-step-label="">
                  {g.rule && <RuleTag rule={g.rule} />}
                  <span className="min-w-0">{g.label}</span>
                </div>
              )}
              <ul className={clsx('space-y-0.5', g.label && 'mt-0.5 border-l pl-2.5')}>
                {g.items.map((c, i) => (
                  <UpgradeChangeRow key={i} change={c} names={names} showRule={!g.label} showLabel={c.label !== g.label} />
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
      {result.notes.length > 0 && (
        <div className="rounded border px-2 py-1" style={{ borderColor: 'var(--st-waiting)' }} data-upgrade-notes="">
          <div style={{ color: 'var(--st-waiting)' }}>{T.notes}</div>
          <ul className="list-disc space-y-0.5 pl-4">
            {result.notes.map((n, i) => (
              <li key={i} className="[overflow-wrap:anywhere]" data-upgrade-note={n.rule ?? ''} data-upgrade-note-level={n.level ?? 'info'}
                  style={n.level === 'warning' ? { color: 'var(--warn)' } : undefined}>
                {n.node_id && !n.text.includes(`「${nameOf(n.node_id) ?? n.node_id}」`) && (
                  <b className="font-semibold">「{nameOf(n.node_id) ?? n.node_id}」</b>
                )}
                <span data-upgrade-note-text="">{n.text}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {rejected.length > 0 && (
        <div data-upgrade-rejected="">
          <div className="text-faint">{T.rejected}</div>
          <ul className="space-y-0.5">
            {rejected.map((r, i) => (
              <li key={i} className="flex items-start gap-1 [overflow-wrap:anywhere]" style={{ color: 'var(--warn)' }}>
                <AlertTriangle size={10} className="mt-0.5 shrink-0" aria-hidden />
                <span className="min-w-0">{upgradeStepText(r.fix_id, nameOf)}：{r.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {result.assist && (result.assist.summary || !!result.assist.questions?.length || !!result.assist.warnings?.length) && (
        <div className="space-y-1" data-upgrade-assist-said="">
          {result.assist.summary && (
            <div className="flex items-start gap-1 [overflow-wrap:anywhere]">
              <Sparkles size={10} className="mt-0.5 shrink-0 text-faint" aria-hidden />
              <span className="min-w-0"><span className="text-faint">{P.assistSaid}：</span>{result.assist.summary}</span>
            </div>
          )}
          {!!result.assist.warnings?.length && (
            <div data-upgrade-assist-warnings="">
              <div className="text-faint">{T.assistWarnings}</div>
              <ul className="space-y-0.5">
                {result.assist.warnings.map((w, i) => (
                  <li key={i} className="flex items-start gap-1 [overflow-wrap:anywhere]" style={{ color: 'var(--warn)' }}>
                    <AlertTriangle size={10} className="mt-0.5 shrink-0" aria-hidden /><span className="min-w-0">{w}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {!!result.assist.questions?.length && (
            <div className="rounded border px-2 py-1" style={{ borderColor: 'var(--st-waiting)' }} data-upgrade-questions="">
              <div style={{ color: 'var(--st-waiting)' }}>{P.questions}</div>
              <ul className="list-disc pl-4">
                {result.assist.questions.map((q, i) => <li key={i} className="[overflow-wrap:anywhere]">{q}</li>)}
              </ul>
            </div>
          )}
        </div>
      )}
      {usable && (
        <div style={{ color: left ? 'var(--warn)' : 'var(--ok)' }} data-upgrade-after-check="">
          {left ? T.afterLeft(left) : T.afterOk}
        </div>
      )}

      {a?.status === 'stale' && (
        <div role="alert" className="flex flex-wrap items-center gap-2" style={{ color: 'var(--warn)' }} data-upgrade-stale="">
          <span className="min-w-0 flex-1">{T.stale}</span>
          {!locked && (
            <button type="button" className="btn btn-xs" onClick={() => void previewUpgrade({ assist: flow.assist })}>
              <RotateCw size={10} aria-hidden /> {T.again}
            </button>
          )}
        </div>
      )}
      {a?.status === 'locked' && !a.unsaved && <div role="alert" style={{ color: 'var(--warn)' }}>{T.locked(a.why)}</div>}
      {unsaved && (
        <div role="alert" className="flex flex-wrap items-center gap-2" data-upgrade-save-failed={a?.status === 'failed' ? '' : 'locked'}
             style={{ color: a?.status === 'failed' ? 'var(--err)' : 'var(--warn)' }}>
          <span className="min-w-0 flex-1">
            {a?.status === 'failed' ? `${P.saveFailed}：${reasonOf(a.error)}` : a?.status === 'locked' ? `${P.saveFailed}（${a.why}）` : ''}。
            {T.saveFailedHint}
          </span>
          {/* 放弃只收起这份预览：画布上的升级留着（要退回就撤销），之后按平常的保存来存 */}
          <button type="button" className="btn btn-xs" onClick={discard} title={T.discardUnsavedHint} data-upgrade-discard="unsaved">
            {P.discard}
          </button>
          <button type="button" className="btn btn-xs" onClick={() => void retrySave()}>
            <RotateCw size={10} aria-hidden /> {P.retrySave}
          </button>
        </div>
      )}
      {!unsaved && (
        <div className="flex flex-wrap items-center gap-2 pt-0.5">
          <span className="min-w-0 flex-1 text-faint">
            {saving ? P.saving : locked ? '' : usable ? `${T.applyNote}${dirty ? `。${P.dirtyNote}` : ''}` : ''}
          </span>
          {!flow.assist && !locked && (
            <button type="button" className="btn btn-xs" data-upgrade-assist="" disabled={saving} title={T.assistHint}
                    onClick={() => void previewUpgrade({ assist: true })}>
              <Sparkles size={10} aria-hidden /> {T.assist}
            </button>
          )}
          <button type="button" className="btn btn-xs" onClick={discard} disabled={saving} data-upgrade-discard="">{P.discard}</button>
          {usable && !locked && (
            <button type="button" className="btn btn-xs btn-primary" data-upgrade-apply="" disabled={saving || a?.status === 'stale'}
                    onClick={() => void apply()}>
              {saving ? <Spinner size={10} /> : <Check size={10} aria-hidden />} {T.apply}
            </button>
          )}
        </div>
      )}
    </section>
  )
}

/** 命中的改写规则：R1–R5 或 Copilot */
function RuleTag({ rule }: { rule: string }) {
  return <span className="mono shrink-0 rounded border px-1 text-faint" title="命中的改写规则">{upgradeRuleText(rule)}</span>
}

/** 这个字段按哪种节点的叫法念：升级后的类型认得就用它，认不得（换类型时去掉的用户提示）用原来的 */
function ownerType(field: string | null | undefined, id: string | null | undefined, names: UpgradeNames): string | undefined {
  const now = names.typeOf(id)
  const key = (field ?? '').split('.')[0].replace(/\[\d+\]$/, '')
  const has = (type?: string) => !!type && !!NODE_DEFS[type as keyof typeof NODE_DEFS]?.fields.some((f) => f.key === key)
  if (has(now)) return now
  const was = names.wasTypeOf(id)
  return has(was) ? was : now
}

/** 一项改动。改接的连线、新插入的节点、换了的类型各有写法，其余和发布前修复的预览一样 */
function UpgradeChangeRow({ change: c, names, showRule, showLabel }: {
  change: UpgradeChange; names: UpgradeNames; showRule: boolean; showLabel: boolean
}) {
  const { nameOf } = names
  const kind = upgradeChangeKind(c)
  const T = UPGRADE_TEXT
  const where = c.node_title || (c.node_id ? nameOf(c.node_id) ?? c.node_id : '') || PUBLISH_FIX_TEXT.whole
  const arrow = <ArrowRight size={10} className="mx-1 inline align-[-1px] text-faint" aria-label="改为" />
  let body: React.ReactNode
  if (kind === 'type') {
    body = (
      <>
        <b className="font-semibold">「{where}」</b> · {T.typeField}：
        <span className="text-faint line-through" data-upgrade-before="">{upgradeTypeText(c.before)}</span>
        {arrow}
        <span className="font-medium" data-upgrade-after="">{upgradeTypeText(c.after)}</span>
      </>
    )
  } else if (kind === 'node') {
    body = <>{T.added}：<span className="font-medium" data-upgrade-after="">{upgradeNodeText(c.after)}</span></>
  } else if (kind === 'edge') {
    // 连线本身带一个箭头，改接不再用箭头图标，写「改接为」，免得两种箭头混在一行里
    const from = c.before != null ? upgradeEdgeText(c.before, nameOf) : ''
    const to = c.after != null ? upgradeEdgeText(c.after, nameOf) : ''
    body = from && to
      ? <>{T.edge}：<span className="text-faint line-through" data-upgrade-before="">{from}</span>
          <span className="mx-1 text-faint">{T.rewired}</span><span className="font-medium" data-upgrade-after="">{to}</span></>
      : to ? <>{T.addedEdge}：<span className="font-medium" data-upgrade-after="">{to}</span></>
      : <>{T.removedEdge}：<span className="text-faint line-through" data-upgrade-before="">{from}</span></>
  } else {
    const field = fixFieldLabel(c.field, ownerType(c.field, c.node_id, names), names.configOf(c.node_id))
    body = (
      <>
        <b className="font-semibold">「{where}」</b>{field && ` · ${field}`}：
        <UpgradeValue value={c.before} field={c.field} nameOf={nameOf} className="text-faint line-through" data-upgrade-before="" />
        {arrow}
        <UpgradeValue value={c.after} field={c.field} nameOf={nameOf} className="font-medium" data-upgrade-after="" />
      </>
    )
  }
  return (
    <li className="leading-snug" data-upgrade-change={kind} data-upgrade-rule={c.rule ?? undefined}>
      <span className="[overflow-wrap:anywhere]">
        {showRule && c.rule && <span className="mr-1"><RuleTag rule={c.rule} /></span>}
        {body}
      </span>
      {showLabel && c.label && <span className="block text-faint">{c.label}</span>}
    </li>
  )
}

/** 预览里的一个值：整份契约一键一行，别的压成一行；节点引用写节点名。和发布前修复的写法相同 */
function UpgradeValue({ value, field, nameOf, className, ...data }: {
  value: unknown; field?: string | null; nameOf: NodeNameOf; className?: string
  'data-upgrade-before'?: string; 'data-upgrade-after'?: string
}) {
  const lines = fixValueLines(value, field, nameOf)
  if (!lines) return <span className={className} {...data}>{fixValueText(value, field, nameOf)}</span>
  return (
    <span className={clsx(className, 'block pl-3')} {...data}>
      {lines.map((line, i) => <span key={i} className="block">{line}</span>)}
    </span>
  )
}

const SUB_LABEL: Record<string, string> = { condition: '的条件', key: '的标识', tools: '的工具', system: '的系统提示' }

function fieldLabel(field: FieldRef | null | undefined, type?: string, config?: Record<string, any>): string {
  if (!field) return ''
  if (field.key === 'label') return '节点名称'
  const def = type ? NODE_DEFS[type as keyof typeof NODE_DEFS] : undefined
  const base = def?.fields.find((f) => f.key === field.key)?.label ?? field.key
  if (field.index == null) return base
  const sub = (field.sub && SUB_LABEL[field.sub]) || ''
  // 成员有名字就叫名字：「第 2 个成员」还得数，「成员 writer」一眼就知道是谁
  const name = field.key === 'agents' ? config?.agents?.[field.index]?.name : undefined
  if (name) return `成员「${name}」${sub}`
  const item = ({ cases: '分支', fields: '字段', metrics: '指标', agents: '成员' } as Record<string, string>)[field.key]
  return item ? `第 ${field.index + 1} 个${item}${sub}` : `${base} · 第 ${field.index + 1} 项`
}
