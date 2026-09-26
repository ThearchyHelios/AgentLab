import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Ban, Check, Hand, PenLine, Wrench } from 'lucide-react'
import { api } from '../api/client'
import { useStudio } from '../store/studio'
import { useCatalog } from '../store/catalog'
import { NODE_DEFS } from '../canvas/nodeDefs'
import { StatusDot, useToast } from '../components/ui'
import { formatDateTime, formatTime, parseServerTime } from '../lib/format'
import { runClassLabel } from '../lib/terms'
import { useRunClock } from './useRunClock'
import { Markdown } from './Markdown'
import type { Approval } from '../types'

// 发起运行的表单搬去了 run/RunControl.tsx 的工具栏控件里。它原先常驻在
// 助手栏顶部，和 Copilot 输入框两个"主要动作"互相压着——而它们是两种不同的
// 意图（跑这张图 / 改这张图），不该争同一块地方。运行是对整张图的动作，
// 和保存、发布同类，属于工具栏。

/** 本机填的署名（设置 → 偏好设置），随 X-Actor 发给后端，写进审批留痕 */
function localActor(): string {
  try { return (localStorage.getItem('agentlab_actor') ?? '').trim() } catch { return '' }
}

/** 等了多久：「12 分钟」「5 小时」「8 天」。审批按天积压是常态，精确到秒没有意义 */
function waited(ms: number): string {
  if (ms < 3_600_000) return `${Math.max(1, Math.round(ms / 60_000))} 分钟`
  if (ms < 48 * 3_600_000) return `${Math.floor(ms / 3_600_000)} 小时`
  return `${Math.floor(ms / 86_400_000)} 天`
}

/**
 * 审批卡。
 *
 * 卡头交代上下文：哪个工作流、挂在哪个节点、什么时候发起、已经等了多久（超过
 * 一天用琥珀色）；按钮下面常显"批了会怎样"，以前藏在驳回按钮的 hover 里；
 * 底部写明"将以谁的名义签批"——审批留痕记的是设置里的署名，没署名时静默记空，
 * 事后审计才发现没有人名。
 *
 * onResolved 是给非画布场景用的：这个组件被对话页和运行页复用，而默认的
 * "恢复后重新接上事件流"走的是 studio store——在对话页里用就会同时开两条
 * 订阅（一条 chat 的、一条 studio 的），两边各收一份事件。谁用谁负责重新
 * 接流，是唯一不会打架的分工。
 */
export function ApprovalCard({ approval, onResolved, showWorkflow = true }: {
  approval: Approval | any
  onResolved?: (runId: string) => void | Promise<void>
  /** 画布上工作流就是眼前这张，卡头不用再说一遍 */
  showWorkflow?: boolean
}) {
  const toast = useToast()
  const refreshApprovals = useCatalog((s) => s.refreshApprovals)
  const attachRun = useStudio((s) => s.attachRun)
  const [note, setNote] = useState('')
  const [value, setValue] = useState(() =>
    approval.payload?.draft != null ? String(approval.payload.draft) : '',
  )
  const [busy, setBusy] = useState(false)
  const payload = approval.payload ?? {}
  const actor = localActor()
  // 等待时长一分钟级就够，不订阅 100ms 的时钟；卡片因别的原因重画时顺带更新
  const now = useRunClock(false)
  const created = parseServerTime(approval.created_at)
  const age = created ? now - created.getTime() : undefined
  const stale = age != null && age > 24 * 3_600_000
  const approveMode = approval.mode === 'approve'

  const decide = async (approved: boolean) => {
    setBusy(true)
    try {
      await api.approvals.decide(approval.id, {
        approved,
        note,
        ...(!approveMode ? { value } : {}),
      })
      await refreshApprovals()
      // 恢复后重新接上事件流：调用方给了 onResolved 就听它的
      if (onResolved) await onResolved(approval.run_id)
      else await attachRun(approval.run_id)
      // 驳回在"补充输入/编辑草稿"模式下会终止整个运行（图上没有 rejected 那条
      // 出口边），说清楚比笼统一句"已驳回"诚实
      toast(
        approved
          ? '已放行，运行继续'
          : approveMode ? '已驳回，走驳回那条出口' : '已驳回，本次运行终止',
        'ok',
      )
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  const where = approval.node_label || approval.node_id
  return (
    <div className="fade-up border-b p-3 last:border-b-0" data-approval={approval.id}
         style={{ background: 'color-mix(in srgb, var(--warn) 7%, transparent)' }}>
      <div className="mb-1 flex items-center gap-1.5 text-xs font-semibold" style={{ color: 'var(--warn)' }}>
        <Hand size={13} aria-hidden /> {approval.title || '需要你确认'}
      </div>
      <div className="tnum mb-2 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-2xs text-dim">
        <span>人工审批</span>
        {showWorkflow && approval.workflow_name && <><span aria-hidden>·</span><span>{approval.workflow_name}</span></>}
        {where && <><span aria-hidden>·</span><span>节点「{where}」</span></>}
        {approval.run_class && <><span aria-hidden>·</span><span>{runClassLabel(approval.run_class)}</span></>}
        {created && (
          <>
            <span aria-hidden>·</span>
            <span title={formatDateTime(created)}>{formatTime(created)} 发起</span>
          </>
        )}
        {age != null && age >= 60_000 && (
          <>
            <span aria-hidden>·</span>
            <span style={stale ? { color: 'var(--st-waiting)', fontWeight: 600 } : undefined}>
              已等待 {waited(age)}
            </span>
          </>
        )}
      </div>

      {payload.message && (
        // 待审的往往是一整段模型写的文案，同样是 Markdown。要人家判断"能不能
        // 发"，却让他对着一堆星号看，是在给审批这件事添难度
        <div className="mb-2 max-h-40 overflow-y-auto rounded border bg-bg p-2">
          <Markdown text={String(payload.message)} dense />
        </div>
      )}

      {payload.tool && (
        <div className="mb-2 rounded border bg-bg p-2">
          <div className="mb-1 flex items-center gap-1.5 text-2xs">
            <Wrench size={11} aria-hidden /> <code className="mono">{payload.tool}</code>
          </div>
          <pre className="mono max-h-32 overflow-auto text-2xs leading-relaxed text-dim">
            {JSON.stringify(payload.args ?? {}, null, 2)}
          </pre>
        </div>
      )}

      {payload.code && (
        <pre className="mono mb-2 max-h-40 overflow-auto rounded border bg-bg p-2 text-2xs leading-relaxed">
          {payload.code}
        </pre>
      )}

      {!approveMode && (
        <textarea
          className="field mb-2"
          rows={4}
          value={value}
          aria-label={approval.mode === 'edit' ? '修改后的内容' : '你的输入'}
          placeholder={approval.mode === 'edit' ? '修改后的内容' : '你的输入'}
          onChange={(e) => setValue(e.target.value)}
        />
      )}

      <input
        className="field mb-2"
        aria-label="备注"
        placeholder="备注（会传给后续节点）"
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />

      <div className="flex gap-2">
        <button className="btn btn-primary flex-1 justify-center" disabled={busy} onClick={() => decide(true)}>
          <Check size={12} aria-hidden /> {approveMode ? '通过' : '提交'}
        </button>
        <button className="btn btn-danger flex-1 justify-center" disabled={busy} onClick={() => decide(false)}>
          <Ban size={12} aria-hidden /> {approveMode ? '驳回' : '驳回并终止'}
        </button>
      </div>
      {/* 批了会怎样，常显而不是藏在 hover 里：审批人得在按下去之前知道 */}
      <div className="mt-1.5 text-2xs leading-relaxed text-dim">
        {approveMode
          ? '通过 → 接着往下跑；驳回 → 走「驳回」那条出口'
          : '提交 → 用你填的内容接着跑；驳回 → 这次运行到此终止'}
      </div>
      <div className="mt-1 flex items-center gap-1 text-2xs leading-relaxed"
           style={actor ? { color: 'var(--text-dim)' } : { color: 'var(--st-waiting)' }}>
        <PenLine size={10} aria-hidden className="shrink-0" />
        {actor
          ? <span>将以「{actor}」签批，写进审批留痕</span>
          : (
            <span>
              未署名：这次签批不会记录审批人 ·{' '}
              <Link to="/settings/prefs" className="underline underline-offset-2 hover:text-fg">去设置署名</Link>
            </span>
          )}
      </div>
    </div>
  )
}

// Timeline / OutputView / describe / EVENT_META / IssuanceBanner / RawEvents
// 都搬走了：
//   - "事件 → 人话"归 run/decode.ts，这里曾经有一套平行的 EVENT_META + describe()，
//     和解码器各译各的。留着两套，同一次运行在画布和问数据页会读出不同的故事，
//     而用户没法判断哪个是真的。
//   - 怎么画归 run/AssistantStream.tsx（含出具横幅），怎么组装归 run/AssistantPanel.tsx。
// 这个文件现在只剩两样东西：发起运行，和处理审批。

export { StatusDot, NODE_DEFS }
