import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ArrowDown, Ban, BookMarked, ChartGantt, Copy, Crosshair, Play, RotateCcw, Settings2, Wrench, X } from 'lucide-react'
import { CopyButton, Spinner, StatusBadge } from '../../components/ui'
import { formatDateTime, formatSpan, formatTime } from '../../lib/format'
import type { Approval, CatalogDriftTable } from '../../types'
import { CATALOG_DRIFT_TEXT, CATALOG_IMPACT_TEXT } from '../../lib/terms'
import { catalogDriftLine } from '../../run/decode'
import type { RunErrorExplain } from '../../lib/explain'
import { LONG_WAIT_MS, ageMs } from './model'
import { copyText } from './parts'

/** 横幅外壳：左侧状态色条 + 淡底。只有异常态用它 */
function Shell({ color, children, data }: { color: string; children: ReactNode; data: string }) {
  return (
    <section
      className="fade-up relative shrink-0 border-t px-4 py-2.5"
      style={{ background: `color-mix(in srgb, ${color} 7%, var(--bg-panel))` }}
      data-run-banner={data}
    >
      <span aria-hidden className="absolute inset-y-0 left-0 w-0.5" style={{ background: color }} />
      {children}
    </section>
  )
}

// -------------------------------------------------------------------------
// 发布之后数据目录有变化（catalog.drift）
// -------------------------------------------------------------------------

/**
 * 从发布版本发起的正式运行，开始时发现这一版用到的表的数据目录在发布之后改过。运行照常（目录是说明，
 * 不是运行的输入），但看结果的人要知道：「在园人数」在发布之后被改成了存量，这一版的 SQL 可能还在跨天相加。
 * SQL 里写着的表在前；Agent 可能查询的表（Agent 运行时自己写 SQL）另起一组、带小标题，和直接引用分开。
 * 每张表给到数据目录的入口；每组表多时只列前几张
 */
export function CatalogDriftBanner({ drift }: {
  drift: { title: string; direct: CatalogDriftTable[]; possible: CatalogDriftTable[] }
}) {
  return (
    <Shell color="var(--st-waiting)" data="catalog-drift">
      <div className="flex items-start gap-2.5">
        <BookMarked size={14} className="mt-0.5 shrink-0" style={{ color: 'var(--st-waiting)' }} aria-hidden />
        <div className="min-w-0 flex-1 text-xs">
          <p className="font-semibold" data-drift-title="">{drift.title}</p>
          <p className="mt-0.5 text-2xs text-dim">{CATALOG_DRIFT_TEXT.sub}</p>
          {drift.direct.length > 0 && <DriftTables tables={drift.direct} />}
          {drift.possible.length > 0 && (
            <div className="mt-1.5" data-drift-possible={drift.possible.length}>
              <p className="text-2xs font-medium text-dim">{CATALOG_DRIFT_TEXT.possibleHead}</p>
              <p className="text-2xs text-faint">{CATALOG_DRIFT_TEXT.possibleHint}</p>
              <DriftTables tables={drift.possible} />
            </div>
          )}
        </div>
      </div>
    </Shell>
  )
}

function DriftTables({ tables }: { tables: CatalogDriftTable[] }) {
  const shown = tables.slice(0, DRIFT_SHOWN)
  return (
    <ul className="mt-1 space-y-0.5 text-2xs text-dim">
      {shown.map((t) => (
        <li key={`${t.source}/${t.table}`} className="flex min-w-0 flex-wrap items-baseline gap-x-2" data-drift-table={t.table}
            data-drift-impact={t.impact}>
          <span className="min-w-0 break-words">{catalogDriftLine(t)}</span>
          {t.source_id && (
            <Link className="shrink-0 text-[var(--accent)] hover:underline" data-drift-open={t.table}
                  to={`/data/catalog/${encodeURIComponent(t.source_id)}/${encodeURIComponent(t.table)}`}>
              {CATALOG_DRIFT_TEXT.open}
            </Link>
          )}
        </li>
      ))}
      {tables.length > shown.length && <li className="text-faint">{CATALOG_IMPACT_TEXT.more(tables.length - shown.length)}</li>}
    </ul>
  )
}

const DRIFT_SHOWN = 6

// -------------------------------------------------------------------------
// 失败
// -------------------------------------------------------------------------

/**
 * 失败的排错路径：哪个节点、为什么、下一步点哪里。以前这里只有一句截断的原始
 * 异常，用户得自己读懂、自己去编排页找那张图、找那个节点，再整张重跑——后端的
 * 断点续跑在这一页用不上。
 */
export function FailedBanner({
  explain, nodeId, nodeLabel, canvasHref, onShowInTrace, onContinue, onRerun, busy,
}: {
  explain: RunErrorExplain
  nodeId?: string | null
  nodeLabel?: string
  /** 有工作流才能回画布定位；未保存的图没有地方可回，不放一个点了没用的按钮 */
  canvasHref?: string | null
  /** 工作流之后改过结构、失败的节点已经不在了：画布上定位不到，改去航迹看它当时的样子 */
  onShowInTrace?: () => void
  onContinue: () => void
  /** 缺输入的失败：补上那一项，用同一张图重新发起 */
  onRerun?: (field: string) => void
  busy: boolean
}) {
  // 要改的东西不在运行快照里（工具库里的参数定义）：先改好再接着跑，主按钮给去改的那一处，
  // 「接着跑」退到后面但不藏——改好之后原样接着跑就能过
  const fixFirst = !!explain.fixFirst && (explain.fix === 'settings' || explain.fix === 'tools')
  const fixCls = `btn btn-sm${fixFirst ? ' btn-primary' : ''}`
  const fixLink = explain.fix === 'settings'
    ? (
      <Link className={fixCls} to={explain.fixTo ?? '/settings/providers'} data-action="settings">
        <Settings2 size={11} aria-hidden /> 去模型接入
      </Link>
    )
    : explain.fix === 'tools'
      ? (
        <Link className={fixCls} to={explain.fixTo ?? '/tools'} data-action="tools"
              title={explain.fixTo ? '打开需要修改的工具的编辑框' : undefined}>
          <Wrench size={11} aria-hidden /> {fixFirst ? '去改参数定义' : '去工具库'}
        </Link>
      )
      : null
  return (
    <Shell color="var(--st-failed)" data="failed">
      <div className="flex items-start gap-2.5">
        <StatusBadge status="failed" size={15} className="mt-0.5" />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 text-xs">
            {nodeId && (
              <span className="shrink-0 text-dim" data-failed-node={nodeId}>
                失败于「<span className="font-medium text-fg">{nodeLabel ?? nodeId}</span>」
                {nodeLabel && nodeLabel !== nodeId && <span className="mono ml-1 text-2xs text-faint">{nodeId}</span>}
              </span>
            )}
            <span className="min-w-0 font-semibold" style={{ color: 'var(--st-failed)' }} data-failed-title="">
              {explain.title}
            </span>
          </div>
          {explain.reason && <div className="mt-0.5 text-xs leading-relaxed text-dim">{explain.reason}</div>}
          {explain.action && (
            <div className="mt-0.5 text-xs leading-relaxed text-faint">
              <span className="text-dim">下一步：</span>{explain.action}
            </div>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            {fixFirst && fixLink}
            {explain.continuable && (
              <button type="button" className={`btn btn-sm${fixFirst ? '' : ' btn-primary'}`} disabled={busy} onClick={onContinue}
                      title={fixFirst ? '请先按上面的提示修改，再继续运行：原样继续运行仍会在同一处失败。已完成的节点不会重新执行'
                        : '从失败的节点继续运行，已完成的节点不会重新执行。如需修改节点配置，请前往画布'}
                      data-action="continue">
                {busy ? <Spinner size={11} /> : <Play size={11} aria-hidden />} 继续运行
              </button>
            )}
            {explain.fix === 'rerun' && explain.missingInput && onRerun && (
              <button type="button" className="btn btn-sm btn-primary" disabled={busy}
                      onClick={() => onRerun(explain.missingInput!)} data-action="rerun"
                      title="使用本次运行时的工作流快照和其余输入，补上这一项后发起新的运行；这条失败记录会保留">
                {busy ? <Spinner size={11} /> : <RotateCcw size={11} aria-hidden />} 补上「{explain.missingInput}」重新运行
              </button>
            )}
            {!fixFirst && fixLink}
            {canvasHref && (
              // 这里接着跑过不去、得回画布改的，定位就是主路
              <Link className={`btn btn-sm${!explain.continuable && explain.fix === 'canvas' ? ' btn-primary' : ''}`}
                    to={canvasHref} data-action="locate"
                    title={nodeId ? '打开该工作流，并将画布定位到失败的节点' : '打开该工作流'}>
                <Crosshair size={11} aria-hidden /> 在画布中定位
              </Link>
            )}
            {!canvasHref && onShowInTrace && (
              <button type="button" className="btn btn-sm" onClick={onShowInTrace} data-action="locate-trace"
                      title={'工作流在本次运行后修改过结构，当前工作流中已没有该节点，无法在画布上定位。\n航迹使用运行时的快照，可查看该节点当时的状态'}>
                <ChartGantt size={11} aria-hidden /> 在航迹中查看
              </button>
            )}
            <button type="button" className="btn btn-sm btn-ghost" data-action="copy-error"
                    onClick={() => void copyText(explain.raw, '报错原文')}>
              <Copy size={11} aria-hidden /> 复制错误
            </button>
          </div>
          {explain.raw && explain.raw !== explain.title && (
            <details className="mt-1.5 text-2xs text-faint">
              <summary className="cursor-pointer select-none hover:text-dim">技术细节</summary>
              <div className="mt-1 flex items-start gap-1.5">
                <pre className="mono max-h-40 min-w-0 flex-1 overflow-auto whitespace-pre-wrap break-all rounded-md border bg-[var(--bg)] p-2 leading-relaxed text-dim">
                  {explain.raw}
                </pre>
                <CopyButton text={explain.raw} />
              </div>
            </details>
          )}
        </div>
      </div>
    </Shell>
  )
}

// -------------------------------------------------------------------------
// 挂起
// -------------------------------------------------------------------------

/**
 * 中断了却没有待审批：服务重启时停下的，断点还在。以前卡头写「等待人工介入」，却没有任何东西可以点。
 * 不打算再跑的就放弃：挂起的运行没人会再去接，不放弃就在记录里永远挂着「可续跑」
 */
export function HeldBanner({ reason, onContinue, onAbandon, busy }: {
  reason?: string | null; onContinue: () => void; onAbandon: () => void; busy: boolean
}) {
  return (
    <Shell color="var(--st-suspended)" data="held">
      <div className="flex items-center gap-2.5 text-xs">
        <StatusBadge status="held" size={15} />
        <span className="min-w-0 flex-1">
          <span className="font-medium text-fg">已挂起，可继续运行</span>
          <span className="text-dim"> · {reason || '本次运行停在断点上，没有待处理的审批'}。已完成的节点不会重新执行。</span>
        </span>
        <AbandonButton onClick={onAbandon} disabled={busy} />
        <button type="button" className="btn btn-sm btn-primary" disabled={busy} onClick={onContinue} data-action="resume">
          {busy ? <Spinner size={11} /> : <Play size={11} aria-hidden />} 继续运行
        </button>
      </div>
    </Shell>
  )
}

/** 放弃是低频、不可逆的：安静的次级按钮，放在主动作左边，点了还要确认一次 */
function AbandonButton({ onClick, disabled }: { onClick: () => void; disabled: boolean }) {
  return (
    <button type="button" className="btn btn-sm btn-ghost shrink-0" disabled={disabled} onClick={onClick}
            data-action="abandon" title="终止运行并记为已取消，待审批一并关闭；已完成的节点和产出保留">
      <Ban size={11} aria-hidden /> 放弃这次运行
    </button>
  )
}

// -------------------------------------------------------------------------
// 等审批
// -------------------------------------------------------------------------

/**
 * 停在审批上：说清楚卡在哪、等了多久，审批卡本身在时间线里（和它要确认的
 * 那件事挨着）。按钮把人带过去。
 */
export function WaitingBanner({ approvals, labelOf, onJump, onAbandon, busy, now }: {
  approvals: Approval[]; labelOf: (id?: string | null) => string | undefined; onJump: (id: string) => void
  onAbandon: () => void; busy: boolean; now: number
}) {
  const first = approvals[0]
  const waited = ageMs(first.created_at, now)
  const long = waited != null && waited >= LONG_WAIT_MS
  return (
    <Shell color="var(--st-waiting)" data="waiting">
      <div className="flex items-center gap-2.5 text-xs">
        <StatusBadge status="waiting" size={15} />
        <span className="min-w-0 flex-1 truncate">
          <span className="font-medium text-fg">在「{first.node_label ?? labelOf(first.node_id)}」等待审批</span>
          {waited != null && (
            <span className="tnum" style={{ color: long ? 'var(--st-waiting)' : undefined }}>
              {' '}· 已等待 {formatSpan(waited, { coarse: true })}
            </span>
          )}
          <span className="text-faint" title={formatDateTime(first.created_at ?? null)}>
            {' '}· {formatTime(first.created_at ?? null)} 发起
          </span>
          {approvals.length > 1 && <span className="text-faint"> · 共 {approvals.length} 张审批卡</span>}
        </span>
        <AbandonButton onClick={onAbandon} disabled={busy} />
        <button type="button" className="btn btn-sm" onClick={() => onJump(first.id)} data-action="jump-approval">
          <ArrowDown size={11} aria-hidden /> 去审批卡
        </button>
      </div>
    </Shell>
  )
}

// -------------------------------------------------------------------------
// 处理之后的回执
// -------------------------------------------------------------------------

export type Feedback =
  | { kind: 'decided'; approved: boolean | null; node: string; by?: string | null; at?: string | null; terminates?: boolean }
  | { kind: 'continued'; from?: string; resume: boolean; at: number }

/**
 * 做完动作之后留下的回执：批了什么、谁批的、什么时候。以前只有一个 4 秒的
 * toast，人一走神就不知道刚才那一下有没有生效。它留到关掉或换一条运行为止。
 */
export function FeedbackStrip({ feedback, onDismiss }: { feedback: Feedback; onDismiss: () => void }) {
  let text: ReactNode
  let color = 'var(--st-done)'
  if (feedback.kind === 'decided') {
    const verb = feedback.approved === false ? '驳回了' : '批准了'
    if (feedback.approved === false) color = 'var(--text-dim)'
    text = (
      <>
        <span className="font-medium">{feedback.by || '未署名'}</span> {verb}「{feedback.node}」
        {feedback.at && <span className="tnum text-faint"> · {formatTime(feedback.at)}</span>}
        <span className="text-dim">
          {' '}· {feedback.approved === false && feedback.terminates ? '本次运行终止' : '运行已继续，时间线将持续更新'}
        </span>
      </>
    )
  } else {
    text = (
      <>
        <span className="font-medium">已{feedback.resume ? '从断点' : feedback.from ? `从「${feedback.from}」` : ''}继续运行</span>
        <span className="tnum text-faint"> · {formatTime(feedback.at)}</span>
        <span className="text-dim"> · 已完成的节点不会重新执行</span>
      </>
    )
  }
  return (
    <div
      role="status"
      className="fade-up flex shrink-0 items-center gap-2 border-t px-4 py-1.5 text-2xs"
      style={{ color }}
      data-run-feedback={feedback.kind}
    >
      <span className="min-w-0 flex-1 truncate">{text}</span>
      <button type="button" className="rounded p-0.5 text-faint hover:bg-hover hover:text-dim" onClick={onDismiss}
              aria-label="关闭回执" title="关闭">
        <X size={11} aria-hidden />
      </button>
    </div>
  )
}
