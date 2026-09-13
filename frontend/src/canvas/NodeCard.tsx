import { memo } from 'react'
import { Handle, Position, type NodeProps } from '@xyflow/react'
import { AlertTriangle, Check, Hand, Loader2, Sparkles, Wrench } from 'lucide-react'
import clsx from 'clsx'
import { NODE_DEFS, sourceHandles } from './nodeDefs'
import { useStudio, type FlowNode } from '../store/studio'
import type { NodeType } from '../types'

/** 卡片正文的一行摘要：不用打开属性面板就能看懂这个节点在干什么。 */
function summarize(type: NodeType, config: Record<string, any>): string {
  const first = (...vals: any[]) => vals.find((v) => typeof v === 'string' && v.trim())?.trim() ?? ''
  switch (type) {
    case 'input':
      return (config.fields ?? []).map((f: any) => f.name).filter(Boolean).join(' · ') || '未定义输入'
    case 'output':
      return (config.fields ?? []).map((f: any) => f.name).filter(Boolean).join(' · ') || '未定义成果'
    case 'llm':
      return first(config.prompt, config.system) || '未填提示'
    case 'agent': {
      const tools = (config.tools ?? []).length
      return `${first(config.prompt, config.system) || '未填任务'}${tools ? ` · ${tools} 个工具` : ''}`
    }
    case 'supervisor':
      return `${(config.agents ?? []).map((a: any) => a.name).join(' / ') || '未配成员'}`
    case 'tool':
      return config.tool ? `${config.tool}()` : '未选择工具'
    case 'code':
      return `${config.language ?? 'python'} · ${(config.code ?? '').split('\n')[0].slice(0, 48) || '空'}`
    case 'branch':
      return (config.cases ?? []).map((c: any) => c.key).filter(Boolean).join(' / ') || '未配分支'
    case 'loop':
      return config.mode === 'while'
        ? `当 ${config.condition || '?'} 时重复`
        : `遍历 ${config.items || '?'}`
    case 'retrieve':
      return `${config.collection || 'default'} · 取 ${config.limit ?? 5} 条`
    case 'memory':
      return `${{ recall: '回忆', write: '记住', clear: '清空' }[config.action as string] ?? ''} @ ${config.scope || 'default'}`
    case 'human':
      return first(config.title) || '等待人工'
    case 'validate':
      return 'JSON Schema 校验' + (config.repair_with_llm ? ' · 失败自动返工' : '')
    case 'transform':
      return first(config.expression, config.template) || '未配置'
    case 'subgraph':
      return config.workflow_id ? '嵌套工作流' : '未选择工作流'
    default:
      return ''
  }
}

function NodeCardImpl({ id, data, selected }: NodeProps<FlowNode>) {
  const def = NODE_DEFS[data.nodeType]
  const runtime = useStudio((s) => s.runtime[id])
  // selector 必须返回稳定引用：filter() 每次都造新数组，会触发无限重渲染
  const hasError = useStudio((s) => s.issues.some((i) => i.node_id === id && i.level === 'error'))
  const Icon = def?.icon ?? Sparkles
  const handles = sourceHandles(data.nodeType, data.config)
  const status = runtime?.status ?? 'idle'

  const streamed = runtime?.tokens || runtime?.thinking
  const summary = summarize(data.nodeType, data.config)

  return (
    <div
      className={clsx(
        `nt-${data.nodeType} group relative rounded-lg border bg-elev transition-shadow`,
        status === 'running' && 'node-running',
        status === 'done' && 'node-done',
        status === 'failed' && 'node-failed',
        status === 'waiting' && 'node-waiting',
        selected && 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg)]',
      )}
      style={{ width: 238, borderColor: selected ? 'var(--accent)' : undefined }}
    >
      {def?.hasTarget && (
        <Handle type="target" position={Position.Left} style={{ left: -5 }} />
      )}

      {/* 标题栏 */}
      <div className="flex items-center gap-2 rounded-t-lg px-2.5 py-2"
           style={{ background: 'color-mix(in srgb, var(--nt) 9%, transparent)' }}>
        <div
          className="flex h-5 w-5 shrink-0 items-center justify-center rounded"
          style={{ background: 'color-mix(in srgb, var(--nt) 20%, transparent)', color: 'var(--nt)' }}
        >
          <Icon size={12} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[12px] font-medium leading-tight">
            {data.label || def?.label}
          </div>
        </div>
        {status === 'running' && <Loader2 size={12} className="animate-spin" style={{ color: 'var(--nt)' }} />}
        {status === 'done' && <Check size={12} className="text-[var(--ok)]" />}
        {status === 'waiting' && <Hand size={12} className="text-[var(--warn)]" />}
        {status === 'failed' && <AlertTriangle size={12} className="text-[var(--err)]" />}
        {status === 'idle' && hasError && (
          <AlertTriangle size={12} className="text-[var(--err)] opacity-70" />
        )}
      </div>

      {/* 摘要 */}
      {summary && (
        <div className="px-2.5 pb-2 pt-1.5 text-[11px] leading-snug text-dim">
          <div className="line-clamp-2 break-words">{summary}</div>
        </div>
      )}

      {/* 运行态：实时流出来的内容 */}
      {streamed && status !== 'idle' && (
        <div className="mx-2 mb-2 max-h-20 overflow-hidden rounded border bg-bg px-2 py-1.5">
          {runtime?.thinking && !runtime?.tokens && (
            <div className="mb-1 text-[9.5px] uppercase tracking-wide text-faint">思考中</div>
          )}
          <div className="mono whitespace-pre-wrap break-words text-[10px] leading-relaxed text-dim">
            {(runtime?.tokens || runtime?.thinking || '').slice(-180)}
          </div>
        </div>
      )}

      {/* 工具调用 */}
      {!!runtime?.toolCalls?.length && (
        <div className="mx-2 mb-2 flex flex-wrap gap-1">
          {runtime.toolCalls.slice(-4).map((call, i) => (
            <span
              key={i}
              className="chip"
              style={{
                borderColor: call.ok === false ? 'var(--err)' : undefined,
                color: call.ok === undefined ? 'var(--accent)' : undefined,
              }}
              title={call.result?.slice(0, 300)}
            >
              <Wrench size={8} />
              {call.tool}
            </span>
          ))}
        </div>
      )}

      {/* 底部状态条 */}
      {(runtime?.durationMs != null || runtime?.error) && (
        <div className="flex items-center gap-2 border-t px-2.5 py-1">
          {runtime.durationMs != null && (
            <span className="text-[10px] text-faint">{formatMs(runtime.durationMs)}</span>
          )}
          {runtime.error && (
            <span className="truncate text-[10px] text-[var(--err)]" title={runtime.error}>
              {runtime.error}
            </span>
          )}
        </div>
      )}

      {/* 出口：有名字的分支把标签显示出来，不用点开就知道哪条是哪条 */}
      {handles.map((handle, i) => {
        const top = handles.length === 1 ? '50%' : `${((i + 1) / (handles.length + 1)) * 100}%`
        return (
          <div key={handle.id}>
            <Handle
              id={handle.id}
              type="source"
              position={Position.Right}
              style={{ top, right: -5, borderColor: handle.color }}
            />
            {handle.label && (
              <span
                className="pointer-events-none absolute left-full ml-2 -translate-y-1/2 whitespace-nowrap text-[9.5px]"
                style={{ top, color: handle.color ?? 'var(--text-faint)' }}
              >
                {handle.label}
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}

export function formatMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60_000)}m${Math.round((ms % 60_000) / 1000)}s`
}

export const NodeCard = memo(NodeCardImpl)
