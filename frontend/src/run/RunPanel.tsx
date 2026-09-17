import { useState } from 'react'
import { Ban, Check, Hand, Wrench } from 'lucide-react'
import { api } from '../api/client'
import { useStudio } from '../store/studio'
import { useCatalog } from '../store/catalog'
import { NODE_DEFS } from '../canvas/nodeDefs'
import { StatusDot, useToast } from '../components/ui'

// 发起运行的表单搬去了 run/RunControl.tsx 的工具栏控件里。它原先常驻在
// 助手栏顶部，和 Copilot 输入框两个"主要动作"互相压着——而它们是两种不同的
// 意图（跑这张图 / 改这张图），不该争同一块地方。运行是对整张图的动作，
// 和保存、发布同类，属于工具栏。

/**
 * 人工介入卡片。
 *
 * onResolved 是给非画布场景用的：这个组件被对话页和运行页复用，而默认的
 * "恢复后重新接上事件流"走的是 studio store——在对话页里用就会同时开两条
 * 订阅（一条 chat 的、一条 studio 的），两边各收一份事件。谁用谁负责重新
 * 接流，是唯一不会打架的分工。
 */
export function ApprovalCard({ approval, onResolved }: {
  approval: any
  onResolved?: (runId: string) => void | Promise<void>
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

  const decide = async (approved: boolean) => {
    setBusy(true)
    try {
      await api.approvals.decide(approval.id, {
        approved,
        note,
        ...(approval.mode !== 'approve' ? { value } : {}),
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
          : approval.mode === 'approve'
            ? '已驳回'
            : '已驳回，本次运行终止',
        'ok',
      )
    } catch (e: any) {
      toast(e.message ?? '操作失败', 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fade-up border-b p-3" style={{ background: 'color-mix(in srgb, var(--warn) 7%, transparent)' }}>
      <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold" style={{ color: 'var(--warn)' }}>
        <Hand size={13} /> {approval.title || '需要你确认'}
      </div>

      {payload.message && (
        <div className="mb-2 max-h-40 overflow-y-auto whitespace-pre-wrap rounded border bg-bg p-2 text-[11px] leading-relaxed">
          {payload.message}
        </div>
      )}

      {payload.tool && (
        <div className="mb-2 rounded border bg-bg p-2">
          <div className="mb-1 flex items-center gap-1.5 text-[11px]">
            <Wrench size={11} /> <code className="mono">{payload.tool}</code>
          </div>
          <pre className="mono max-h-32 overflow-auto text-[10px] leading-relaxed text-dim">
            {JSON.stringify(payload.args ?? {}, null, 2)}
          </pre>
        </div>
      )}

      {payload.code && (
        <pre className="mono mb-2 max-h-40 overflow-auto rounded border bg-bg p-2 text-[10px] leading-relaxed">
          {payload.code}
        </pre>
      )}

      {approval.mode !== 'approve' && (
        <textarea
          className="field mb-2"
          rows={4}
          value={value}
          placeholder={approval.mode === 'edit' ? '修改后的内容' : '你的输入'}
          onChange={(e) => setValue(e.target.value)}
        />
      )}

      <input
        className="field mb-2"
        placeholder="备注（会传给后续节点）"
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />

      <div className="flex gap-2">
        <button className="btn btn-primary flex-1 justify-center" disabled={busy} onClick={() => decide(true)}>
          <Check size={12} /> {approval.mode === 'approve' ? '通过' : '提交'}
        </button>
        <button
          className="btn btn-danger flex-1 justify-center"
          disabled={busy}
          onClick={() => decide(false)}
          title={approval.mode === 'approve' ? '不放行，走 rejected 分支' : '驳回并终止这次运行'}
        >
          <Ban size={12} /> {approval.mode === 'approve' ? '驳回' : '驳回并终止'}
        </button>
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
