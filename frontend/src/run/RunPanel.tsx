import { useEffect, useMemo, useState } from 'react'
import {
  Ban, Check, ChevronRight, Hand, Play, ShieldCheck, Square, Wrench,
} from 'lucide-react'
import { api } from '../api/client'
import { useStudio } from '../store/studio'
import { useCatalog } from '../store/catalog'
import { NODE_DEFS } from '../canvas/nodeDefs'
import { StatusDot, useToast } from '../components/ui'

/**
 * 发起运行。
 *
 * 这是整个界面里**唯一**能发起运行的地方，不能因为改版就顺手删掉——一起没掉的
 * 还有审批入口，那会让卡在人工介入上的运行在界面里彻底没有出口。
 *
 * 跑起来之后自动收起：这时候用户要看的是执行到哪了，不是再填一遍输入。
 */
export function RunLauncher() {
  const nodes = useStudio((s) => s.nodes)
  const run = useStudio((s) => s.run)
  const streaming = useStudio((s) => s.streaming)
  const issues = useStudio((s) => s.issues)
  const workflow = useStudio((s) => s.workflow)
  const dirty = useStudio((s) => s.dirty)
  const { startRun, startFormalRun, stopRun } = useStudio()
  const canFormal = !!workflow && (workflow.status === 'published' || workflow.status === 'governed')
    && !!workflow.published_version
  const toast = useToast()
  const [values, setValues] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState(true)

  // 入口节点声明了什么字段，这里就渲染什么表单
  const inputFields = useMemo(() => {
    const entry = nodes.find((n) => n.data.nodeType === 'input')
    return (entry?.data.config?.fields ?? []) as any[]
  }, [nodes])

  // 跑起来就让位给执行过程；停下来再自己展开
  useEffect(() => { if (streaming) setOpen(false) }, [streaming])

  const errors = issues.filter((i) => i.level === 'error')

  const launch = async (formal = false) => {
    setBusy(true)
    try {
      const payload: Record<string, any> = {}
      for (const field of inputFields) {
        if (!field.name) continue
        const raw = values[field.name] ?? field.default ?? ''
        // 看起来像 JSON 就按 JSON 传，让数组/对象类型的输入能用
        if (typeof raw === 'string' && /^\s*[[{]/.test(raw)) {
          try { payload[field.name] = JSON.parse(raw) } catch { payload[field.name] = raw }
        } else {
          payload[field.name] = raw
        }
      }
      if (formal) await startFormalRun(payload)
      else await startRun(payload)
    } catch (e: any) {
      toast(e.message ?? '启动失败', 'error')
    } finally {
      setBusy(false)
    }
  }

  const running = streaming || run?.status === 'running'

  return (
    <div className="shrink-0 border-b p-2.5">
      {!!inputFields.length && (
        <button className="mb-1.5 flex w-full items-center gap-1 text-[10.5px] text-faint hover:text-dim"
                onClick={() => setOpen((v) => !v)}>
          <ChevronRight size={10}
            style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }} />
          输入（{inputFields.length} 项）
          {!open && (
            <span className="ml-1 min-w-0 flex-1 truncate text-left">
              {inputFields.map((f) => values[f.name] ?? f.default ?? '').filter(Boolean).join(' · ')}
            </span>
          )}
        </button>
      )}

      {open && inputFields.map((field) => (
        <div key={field.name} className="mb-2">
          <label className="label">
            {field.name}
            {field.required && <span className="ml-1 text-[var(--err)]">*</span>}
            {field.description && <span className="ml-1.5 font-normal text-faint">{field.description}</span>}
          </label>
          <textarea
            className="field"
            rows={2}
            value={values[field.name] ?? (typeof field.default === 'string' ? field.default : field.default ? JSON.stringify(field.default) : '')}
            placeholder={field.required ? '必填' : '可留空'}
            onChange={(e) => setValues({ ...values, [field.name]: e.target.value })}
          />
        </div>
      ))}
      {!inputFields.length && !!nodes.length && (
        <div className="mb-2 text-[10.5px] text-faint">这张图没有输入节点，将直接运行</div>
      )}

      {!!errors.length && (
        <div className="mb-2 rounded border px-2 py-1.5 text-[10.5px] leading-snug"
             style={{ borderColor: 'var(--err)', color: 'var(--err)' }}>
          {errors.length} 个问题会阻止运行：{errors[0].message}
        </div>
      )}

      <div className="flex gap-2">
        {running ? (
          <button className="btn btn-danger flex-1 justify-center" onClick={stopRun}>
            <Square size={12} /> 停止
          </button>
        ) : (
          <>
            <button
              className="btn btn-primary flex-1 justify-center"
              onClick={() => launch(false)}
              disabled={busy || !!errors.length || !nodes.length}
              title="用画布当前内容跑，结果标探索性"
            >
              <Play size={12} /> 试运行
            </button>
            {canFormal && (
              <button
                className="btn flex-1 justify-center"
                style={{ borderColor: 'var(--ok)', color: 'var(--ok)' }}
                onClick={() => launch(true)}
                disabled={busy || dirty}
                title={dirty ? '有未保存改动，正式运行只跑已发布版本' : `从已发布的 v${workflow?.published_version} 不可变版本发起`}
              >
                <ShieldCheck size={12} /> 正式运行 v{workflow?.published_version}
              </button>
            )}
          </>
        )}
      </div>
      {canFormal && dirty && (
        <div className="mt-1.5 text-[10px] text-faint">画布有未保存改动；正式运行永远执行已发布的不可变版本</div>
      )}
    </div>
  )
}

// -------------------------------------------------------------------------

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
