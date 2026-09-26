import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ArrowDown, Copy, Crosshair, Play, RotateCcw, Settings2, Wrench, X } from 'lucide-react'
import { CopyButton, Spinner, StatusBadge } from '../../components/ui'
import { formatDateTime, formatTime } from '../../lib/format'
import type { Approval } from '../../types'
import type { RunErrorExplain } from './explain'
import { LONG_WAIT_MS, ageMs, formatSpan } from './model'
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
// 失败
// -------------------------------------------------------------------------

/**
 * 失败的排错路径：哪个节点、为什么、下一步点哪里。以前这里只有一句截断的原始
 * 异常，用户得自己读懂、自己去编排页找那张图、找那个节点，再整张重跑——后端的
 * 断点续跑在这一页用不上。
 */
export function FailedBanner({
  explain, nodeId, nodeLabel, canvasHref, onContinue, onRerun, busy,
}: {
  explain: RunErrorExplain
  nodeId?: string | null
  nodeLabel?: string
  /** 有工作流才能回画布定位；未保存的图没有地方可回，不放一个点了没用的按钮 */
  canvasHref?: string | null
  onContinue: () => void
  /** 缺输入的失败：补上那一项，用同一张图重新发起 */
  onRerun?: (field: string) => void
  busy: boolean
}) {
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
            {explain.continuable && (
              <button type="button" className="btn btn-sm btn-primary" disabled={busy} onClick={onContinue}
                      title="从失败的节点接着跑，前面跑完的不重跑。要改节点配置请到画布" data-action="continue">
                {busy ? <Spinner size={11} /> : <Play size={11} aria-hidden />} 接着跑
              </button>
            )}
            {explain.fix === 'rerun' && explain.missingInput && onRerun && (
              <button type="button" className="btn btn-sm btn-primary" disabled={busy}
                      onClick={() => onRerun(explain.missingInput!)} data-action="rerun"
                      title="用这次运行的同一张图和其余输入，补上这一项，发起一次新的运行；这条失败记录保留">
                {busy ? <Spinner size={11} /> : <RotateCcw size={11} aria-hidden />} 补上「{explain.missingInput}」重新运行
              </button>
            )}
            {explain.fix === 'settings' && (
              <Link className="btn btn-sm" to="/settings/providers" data-action="settings">
                <Settings2 size={11} aria-hidden /> 去模型接入
              </Link>
            )}
            {explain.fix === 'tools' && (
              <Link className="btn btn-sm" to="/tools" data-action="tools">
                <Wrench size={11} aria-hidden /> 去工具库
              </Link>
            )}
            {canvasHref && (
              // 这里接着跑过不去、得回画布改的，定位就是主路
              <Link className={`btn btn-sm${!explain.continuable && explain.fix === 'canvas' ? ' btn-primary' : ''}`}
                    to={canvasHref} data-action="locate"
                    title={nodeId ? '打开这张工作流，并把画布对准失败的节点' : '打开这张工作流'}>
                <Crosshair size={11} aria-hidden /> 在画布中定位
              </Link>
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

/** 中断了却没有待审批：服务重启时停下的，断点还在。以前卡头写「等待人工介入」，却没有任何东西可以点 */
export function HeldBanner({ reason, onContinue, busy }: { reason?: string | null; onContinue: () => void; busy: boolean }) {
  return (
    <Shell color="var(--st-suspended)" data="held">
      <div className="flex items-center gap-2.5 text-xs">
        <StatusBadge status="held" size={15} />
        <span className="min-w-0 flex-1">
          <span className="font-medium text-fg">已挂起，可以接着跑</span>
          <span className="text-dim"> · {reason || '这次运行停在断点上，没有待处理的审批'}。前面跑完的节点不会重跑。</span>
        </span>
        <button type="button" className="btn btn-sm btn-primary" disabled={busy} onClick={onContinue} data-action="resume">
          {busy ? <Spinner size={11} /> : <Play size={11} aria-hidden />} 接着跑
        </button>
      </div>
    </Shell>
  )
}

// -------------------------------------------------------------------------
// 等审批
// -------------------------------------------------------------------------

/**
 * 停在审批上：说清楚卡在哪、等了多久，审批卡本身在时间线里（和它要确认的
 * 那件事挨着）。按钮把人带过去。
 */
export function WaitingBanner({ approvals, labelOf, onJump, now }: {
  approvals: Approval[]; labelOf: (id?: string | null) => string | undefined; onJump: (id: string) => void; now: number
}) {
  const first = approvals[0]
  const waited = ageMs(first.created_at, now)
  const long = waited != null && waited >= LONG_WAIT_MS
  return (
    <Shell color="var(--st-waiting)" data="waiting">
      <div className="flex items-center gap-2.5 text-xs">
        <StatusBadge status="waiting" size={15} />
        <span className="min-w-0 flex-1 truncate">
          <span className="font-medium text-fg">停在「{first.node_label ?? labelOf(first.node_id)}」等审批</span>
          {waited != null && (
            <span className="tnum" style={{ color: long ? 'var(--st-waiting)' : undefined }}>
              {' '}· 已等 {formatSpan(waited, { coarse: true })}
            </span>
          )}
          <span className="text-faint" title={formatDateTime(first.created_at ?? null)}>
            {' '}· {formatTime(first.created_at ?? null)} 发起
          </span>
          {approvals.length > 1 && <span className="text-faint"> · 共 {approvals.length} 张审批卡</span>}
        </span>
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
    const verb = feedback.approved === false ? '驳回了' : '放行了'
    if (feedback.approved === false) color = 'var(--text-dim)'
    text = (
      <>
        <span className="font-medium">{feedback.by || '未署名'}</span> {verb}「{feedback.node}」
        {feedback.at && <span className="tnum text-faint"> · {formatTime(feedback.at)}</span>}
        <span className="text-dim">
          {' '}· {feedback.approved === false && feedback.terminates ? '本次运行终止' : '运行继续，下面的时间线会接着长'}
        </span>
      </>
    )
  } else {
    text = (
      <>
        <span className="font-medium">已{feedback.resume ? '从断点' : feedback.from ? `从「${feedback.from}」` : ''}接着跑</span>
        <span className="tnum text-faint"> · {formatTime(feedback.at)}</span>
        <span className="text-dim"> · 前面跑完的节点不会重跑</span>
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
