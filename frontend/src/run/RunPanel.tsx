import { useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle, Ban, Check, ChevronRight, CircleDot, Hand, Play, ShieldCheck, Square, Wrench,
} from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { useStudio } from '../store/studio'
import { useCatalog } from '../store/catalog'
import { NODE_DEFS } from '../canvas/nodeDefs'
import { formatMs } from '../canvas/NodeCard'
import { Empty, StatusDot, Tabs, useToast } from '../components/ui'
import type { RunEvent } from '../types'

export function RunPanel() {
  const [tab, setTab] = useState('timeline')
  const run = useStudio((s) => s.run)
  const streaming = useStudio((s) => s.streaming)
  const approvals = useCatalog((s) => s.approvals)
  const pending = approvals.filter((a) => a.run_id === run?.id && a.status === 'pending')

  return (
    <div className="flex h-full flex-col">
      <RunLauncher />
      {pending.map((approval) => (
        <ApprovalCard key={approval.id} approval={approval} />
      ))}
      <Tabs
        tabs={[
          { key: 'timeline', label: '时间线' },
          { key: 'output', label: '成果' },
          { key: 'raw', label: '事件' },
        ]}
        active={tab}
        onChange={setTab}
      />
      <div className="min-h-0 flex-1 overflow-y-auto">
        {tab === 'timeline' && <Timeline />}
        {tab === 'output' && <OutputView />}
        {tab === 'raw' && <RawEvents />}
      </div>
      {run && (
        <div className="flex items-center justify-between gap-2 border-t px-3 py-1.5 text-[10.5px] text-faint">
          <span className="mono truncate" title={run.id}>#{run.id.slice(0, 8)}</span>
          {run.run_class === 'formal'
            ? <span className="chip" style={{ color: 'var(--ok)', borderColor: 'var(--ok)' }}>正式 v{run.version}</span>
            : <span className="chip">探索</span>}
          <span className="flex items-center gap-2">
            {streaming && <span className="text-[var(--accent)]">● 实时</span>}
            {!!run.usage?.total_tokens && <span>{run.usage.total_tokens} tok</span>}
            {run.usage?.cost_usd ? <span>${Number(run.usage.cost_usd).toFixed(4)}</span> : null}
            {run.usage?.duration_ms ? <span>{formatMs(run.usage.duration_ms)}</span> : null}
          </span>
        </div>
      )}
    </div>
  )
}

// -------------------------------------------------------------------------

function RunLauncher() {
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

  // 入口节点声明了什么字段，这里就渲染什么表单
  const inputFields = useMemo(() => {
    const entry = nodes.find((n) => n.data.nodeType === 'input')
    return (entry?.data.config?.fields ?? []) as any[]
  }, [nodes])

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

  return (
    <div className="border-b p-3">
      {inputFields.map((field) => (
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
      {!inputFields.length && (
        <div className="mb-2 text-[11px] text-faint">
          这张图没有输入节点，将直接运行
        </div>
      )}

      {!!errors.length && (
        <div className="mb-2 rounded border px-2 py-1.5 text-[10.5px] leading-snug"
             style={{ borderColor: 'var(--err)', color: 'var(--err)' }}>
          {errors.length} 个问题会阻止运行：{errors[0].message}
        </div>
      )}

      <div className="flex gap-2">
        {streaming || run?.status === 'running' ? (
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

// -------------------------------------------------------------------------

const HIDDEN_TYPES = new Set(['llm.token', 'llm.thinking.delta'])

function Timeline() {
  const events = useStudio((s) => s.events)
  const nodes = useStudio((s) => s.nodes)
  const select = useStudio((s) => s.select)
  const bottom = useRef<HTMLDivElement>(null)
  const [expanded, setExpanded] = useState<Record<number, boolean>>({})

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [events.length])

  const visible = events.filter((e) => !HIDDEN_TYPES.has(e.type))
  if (!visible.length) {
    return <Empty icon={<CircleDot size={22} />} title="还没有运行" hint="填好输入点「运行」，这里会实时显示每一步。" />
  }

  const labelOf = (id: string | null) =>
    nodes.find((n) => n.id === id)?.data.label ?? id ?? ''

  return (
    <div className="p-2">
      {visible.map((event) => {
        const meta = EVENT_META[event.type] ?? { text: event.type, tone: 'dim' as const }
        const detail = describe(event)
        const open = expanded[event.seq]
        return (
          <div key={event.seq} className="fade-up group flex gap-2 px-1 py-1">
            <span
              className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full"
              style={{ background: TONE[meta.tone] }}
            />
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline gap-1.5">
                <span className="text-[11.5px]" style={{ color: TONE[meta.tone] }}>
                  {meta.text}
                </span>
                {event.node_id && (
                  <button
                    className="truncate text-[11px] text-dim hover:text-fg hover:underline"
                    onClick={() => select(event.node_id)}
                  >
                    {labelOf(event.node_id)}
                  </button>
                )}
                {event.data?.duration_ms != null && (
                  <span className="ml-auto shrink-0 text-[10px] text-faint">
                    {formatMs(event.data.duration_ms)}
                  </span>
                )}
              </div>
              {detail && (
                <div
                  className={clsx(
                    'mt-0.5 text-[10.5px] leading-snug text-faint',
                    !open && 'line-clamp-2',
                  )}
                >
                  {detail}
                </div>
              )}
              {detail && detail.length > 90 && (
                <button
                  className="mt-0.5 flex items-center gap-0.5 text-[10px] text-faint opacity-0 transition-opacity group-hover:opacity-100"
                  onClick={() => setExpanded({ ...expanded, [event.seq]: !open })}
                >
                  <ChevronRight size={9} className={open ? 'rotate-90' : ''} />
                  {open ? '收起' : '展开'}
                </button>
              )}
            </div>
          </div>
        )
      })}
      <div ref={bottom} />
    </div>
  )
}

const TONE = {
  ok: 'var(--ok)', err: 'var(--err)', warn: 'var(--warn)',
  accent: 'var(--accent)', dim: 'var(--text-faint)',
}

const EVENT_META: Record<string, { text: string; tone: keyof typeof TONE }> = {
  'run.started': { text: '开始运行', tone: 'accent' },
  'run.finished': { text: '运行完成', tone: 'ok' },
  'run.failed': { text: '运行失败', tone: 'err' },
  'run.cancelled': { text: '已取消', tone: 'warn' },
  'run.interrupted': { text: '等待人工介入', tone: 'warn' },
  'run.resumed': { text: '已恢复', tone: 'accent' },
  'node.started': { text: '▶', tone: 'accent' },
  'node.finished': { text: '✓', tone: 'ok' },
  'node.failed': { text: '✕', tone: 'err' },
  'node.skipped': { text: '跳过', tone: 'dim' },
  'edge.taken': { text: '分支', tone: 'warn' },
  'llm.start': { text: '调用模型', tone: 'dim' },
  'llm.thinking': { text: '思考', tone: 'dim' },
  'llm.end': { text: '模型返回', tone: 'dim' },
  'tool.start': { text: '调用工具', tone: 'dim' },
  'tool.end': { text: '工具完成', tone: 'ok' },
  'tool.error': { text: '工具失败', tone: 'err' },
  'sandbox.start': { text: '沙箱启动', tone: 'dim' },
  'sandbox.end': { text: '沙箱结束', tone: 'ok' },
  'agent.step.start': { text: 'Agent 接手', tone: 'accent' },
  'agent.step.end': { text: 'Agent 交付', tone: 'ok' },
  'human.requested': { text: '请求人工', tone: 'warn' },
  'human.resolved': { text: '人工已回复', tone: 'ok' },
  'log': { text: '日志', tone: 'dim' },
  'issuance': { text: '出具判定', tone: 'warn' },
  'caliber.upgrade': { text: '口径升版', tone: 'warn' },
}

function describe(event: RunEvent): string {
  const d = event.data ?? {}
  switch (event.type) {
    case 'edge.taken':
      return `走 ${d.branch}${d.reason ? ` · ${d.reason}` : ''}${d.iteration != null ? ` · 第 ${d.iteration + 1} 轮` : ''}`
    case 'tool.start':
      return `${d.tool}(${JSON.stringify(d.args ?? {}).slice(0, 120)})`
    case 'tool.end':
    case 'tool.error':
      return String(d.preview ?? '').slice(0, 300)
    case 'llm.thinking':
      return String(d.text ?? '').slice(0, 300)
    case 'llm.end':
      return `${d.model ?? ''} · ${d.input_tokens ?? 0}+${d.output_tokens ?? 0} tok${d.cost_usd ? ` · $${Number(d.cost_usd).toFixed(4)}` : ''}`
    case 'sandbox.end':
      return `exit=${d.exit_code} · ${d.backend}${d.stdout ? `\n${String(d.stdout).slice(0, 300)}` : ''}`
    case 'agent.step.start':
      return `${d.agent}：${String(d.instruction ?? '').slice(0, 160)}`
    case 'agent.step.end':
      return `${d.agent} → ${String(d.preview ?? '').slice(0, 200)}`
    case 'node.failed':
    case 'run.failed':
      return String(d.error ?? '')
    case 'log':
      return String(d.message ?? '')
    case 'human.requested':
      return String(d.title ?? d.tool ?? '')
    case 'issuance': {
      const tier = { formal: '正式出具', degraded: '降档出具', withheld: '不予出具' }[d.tier as string] ?? d.tier
      const parts = [tier]
      if (d.missing_required?.length) parts.push(`缺必需指标 ${d.missing_required.join(',')}`)
      if (d.unmatched) parts.push(`${d.unmatched} 个数字无法回指`)
      return parts.join(' · ')
    }
    case 'caliber.upgrade':
      return `方法卡 v${d.pinned} → v${d.latest} 已有新版，处置：${d.policy_label ?? d.policy}`
    case 'node.finished': {
      if (!d.preview) return ''
      const text = typeof d.preview === 'string' ? d.preview : JSON.stringify(d.preview)
      return text.slice(0, 300)
    }
    default:
      return ''
  }
}

// -------------------------------------------------------------------------

function OutputView() {
  const run = useStudio((s) => s.run)
  const events = useStudio((s) => s.events)

  // run 对象是启动时拿到的快照，完成事件里才带最终成果
  const finished = [...events].reverse().find((e) => e.type === 'run.finished')
  const output = finished?.data?.output ?? run?.output
  const failed = [...events].reverse().find((e) => e.type === 'run.failed')

  if (failed) {
    return (
      <div className="p-3">
        <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-[var(--err)]">
          <AlertTriangle size={13} /> 运行失败
        </div>
        <pre className="mono whitespace-pre-wrap rounded border bg-bg p-2 text-[11px] leading-relaxed text-[var(--err)]">
          {failed.data?.error}
        </pre>
      </div>
    )
  }

  if (!output || !Object.keys(output).length) {
    return <Empty title="还没有成果" hint="运行完成后，output 节点收集的结构化结果会显示在这里。" />
  }

  const issuance = (output as any)._issuance
  return (
    <div className="space-y-3 p-3">
      {issuance && <IssuanceBanner issuance={issuance} runClass={run?.run_class} />}
      {Object.entries(output).filter(([k]) => k !== '_issuance').map(([key, value]) => (
        <div key={key}>
          <div className="label">{key}</div>
          <div className="whitespace-pre-wrap break-words rounded border bg-bg p-2 text-[11.5px] leading-relaxed">
            {typeof value === 'string' ? value : JSON.stringify(value, null, 2)}
          </div>
        </div>
      ))}
    </div>
  )
}

const TIER_META: Record<string, { label: string; color: string; hint: string }> = {
  formal: { label: '正式出具', color: 'var(--ok)', hint: '指标齐全，叙述中所有数字均可回指口径卡' },
  degraded: { label: '降档出具', color: 'var(--warn)', hint: '存在缺口，结论请对照下方声明使用' },
  withheld: { label: '不予出具', color: 'var(--err)', hint: '必需指标缺失或数字无法溯源，本期结论不作数' },
}

function IssuanceBanner({ issuance, runClass }: { issuance: any; runClass?: string }) {
  const meta = TIER_META[issuance.tier] ?? TIER_META.degraded
  return (
    <div className="rounded-lg border p-2.5" style={{ borderColor: meta.color }}>
      <div className="flex items-center gap-2">
        <span className="text-[12px] font-semibold" style={{ color: meta.color }}>
          ⚖ {meta.label}
        </span>
        {runClass === 'exploratory' && (
          <span className="chip" style={{ color: 'var(--warn)' }}>探索性运行 · 不进正式归档</span>
        )}
        <span className="ml-auto text-[10px] text-faint">
          回指 {issuance.matched_numbers ?? 0} 个数字 / 核对 {issuance.metrics_checked ?? 0} 个指标
        </span>
      </div>
      <div className="mt-1 text-[10.5px] text-faint">{meta.hint}</div>
      {(issuance.calibers ?? []).map((c: any) => (
        <div key={c.node} className="mt-1 text-[10.5px] text-dim">
          口径：{c.caliber} @ {c.version}
        </div>
      ))}
      {!!issuance.missing_required?.length && (
        <div className="mt-1 text-[10.5px]" style={{ color: 'var(--err)' }}>
          缺必需指标：{issuance.missing_required.join('、')}
        </div>
      )}
      {!!issuance.missing_expected?.length && (
        <div className="mt-1 text-[10.5px]" style={{ color: 'var(--warn)' }}>
          缺数据声明：{issuance.missing_expected.join('、')} 本期缺失
        </div>
      )}
      {!!issuance.unmatched_numbers?.length && (
        <div className="mt-1 text-[10.5px]" style={{ color: 'var(--warn)' }}>
          无法回指的数字：{issuance.unmatched_numbers.map((u: any) => u.token).join('、')}
        </div>
      )}
    </div>
  )
}

function RawEvents() {
  const events = useStudio((s) => s.events)
  if (!events.length) return <Empty title="暂无事件" />
  return (
    <div className="p-2">
      {events.map((e) => (
        <div key={e.seq} className="mono border-b px-1 py-1 text-[10px] leading-relaxed last:border-0">
          <span className="text-faint">#{e.seq}</span>{' '}
          <span className="text-[var(--accent)]">{e.type}</span>{' '}
          {e.node_id && <span className="text-dim">{e.node_id}</span>}
          <div className="break-all text-faint">{JSON.stringify(e.data).slice(0, 500)}</div>
        </div>
      ))}
    </div>
  )
}

export { StatusDot, NODE_DEFS }
