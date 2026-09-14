import { useEffect, useState } from 'react'
import { GitFork, History, RefreshCw, Trash2 } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { formatMs } from '../canvas/NodeCard'
import { Empty, Spinner, StatusDot, useToast } from '../components/ui'
import { ApprovalCard } from '../run/RunPanel'
import { useCatalog } from '../store/catalog'
import type { Run, RunEvent } from '../types'

/** 运行历史。每次运行的完整事件流都落了库，所以这里能完整回放。 */
export function RunsPage() {
  const toast = useToast()
  const [runs, setRuns] = useState<Run[]>([])
  const [selected, setSelected] = useState<Run | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('')
  // 导航上的待办徽标指向这一页，所以这页必须能处理待办
  const approvals = useCatalog((s) => s.approvals)
  const pendingHere = approvals.filter(
    (a) => a.run_id === selected?.id && a.status === 'pending',
  )

  const load = async () => {
    setLoading(true)
    setRuns(await api.runs.list({ limit: 100 }).catch(() => []))
    setLoading(false)
  }
  useEffect(() => { void load() }, [])

  const open = async (run: Run) => {
    setSelected(run)
    setEvents(await api.runs.events(run.id).catch(() => []))
  }

  const shown = runs.filter(
    (r) => !filter || r.workflow_name.includes(filter) || r.status === filter,
  )

  return (
    <div className="flex h-full">
      <div className="flex w-[380px] shrink-0 flex-col border-r">
        <div className="flex items-center gap-2 border-b p-2">
          <input className="field" placeholder="按名称或状态筛选…" value={filter}
                 onChange={(e) => setFilter(e.target.value)} />
          <button className="btn btn-sm" onClick={load}><RefreshCw size={11} /></button>
        </div>
        <div className="flex-1 overflow-y-auto">
          {loading && <div className="p-4"><Spinner /></div>}
          {!loading && !shown.length && <Empty icon={<History size={22} />} title="还没有运行记录" />}
          {shown.map((run) => (
            <button
              key={run.id}
              onClick={() => open(run)}
              className={clsx(
                'flex w-full flex-col gap-1 border-b px-3 py-2 text-left hover:bg-hover',
                selected?.id === run.id && 'bg-hover',
              )}
            >
              <div className="flex items-center gap-2">
                <span className="min-w-0 flex-1 truncate text-[12px]">{run.workflow_name || '临时图'}</span>
                {run.run_class === 'formal' && (
                  <span className="chip" style={{ color: 'var(--ok)', borderColor: 'var(--ok)' }}>正式</span>
                )}
                {(run.output as any)?._issuance?.tier === 'withheld' && (
                  <span className="chip" style={{ color: 'var(--err)' }}>不予出具</span>
                )}
                <StatusDot status={run.status} />
              </div>
              <div className="flex gap-2 text-[10px] text-faint">
                <span>{run.created_at ? new Date(run.created_at).toLocaleString('zh-CN') : ''}</span>
                {run.usage?.duration_ms ? <span>{formatMs(run.usage.duration_ms)}</span> : null}
                {run.usage?.total_tokens ? <span>{run.usage.total_tokens} tok</span> : null}
                {run.usage?.cost_usd ? <span>${Number(run.usage.cost_usd).toFixed(4)}</span> : null}
              </div>
            </button>
          ))}
        </div>
      </div>

      <div className="min-w-0 flex-1 overflow-y-auto">
        {!selected && <Empty title="选一条运行记录" hint="左侧点击可以查看完整的执行轨迹、输入输出和用量。" />}
        {selected && (
          <div className="p-4">
            <div className="mb-3 flex items-start gap-3">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 text-sm font-semibold">
                  {selected.workflow_name || '临时图'}
                  {selected.run_class === 'formal'
                    ? <span className="chip" style={{ color: 'var(--ok)', borderColor: 'var(--ok)' }}>正式 v{selected.version}</span>
                    : <span className="chip">探索性</span>}
                </div>
                <div className="mono text-[10.5px] text-faint">
                  {selected.id}
                  {selected.version_hash && ` · 版本 ${selected.version_hash.slice(0, 10)}`}
                  {selected.manifest_hash && ` · 清单 ${selected.manifest_hash.slice(0, 10)}`}
                  {selected.started_by && ` · by ${selected.started_by}`}
                </div>
              </div>
              <StatusDot status={selected.status} />
              {selected.run_class !== 'formal' && (
                <button
                  className="btn btn-sm"
                  title="把这次实际走过的路径提取成草稿模板（探索层 → 模板层）"
                  onClick={async () => {
                    try {
                      const res = await api.copilot.fromRun(selected.id)
                      toast(`已提取为草稿「${res.name}」（${res.nodes} 节点，剪掉 ${res.dropped_nodes} 个未走节点）`, 'ok')
                    } catch (e: any) { toast(e.message ?? '提取失败', 'error') }
                  }}
                >
                  <GitFork size={11} /> 提取模板
                </button>
              )}
              <button
                className="btn btn-sm btn-ghost"
                onClick={async () => {
                  if (!confirm('删除这条运行记录？')) return
                  await api.runs.remove(selected.id)
                  setSelected(null)
                  await load()
                  toast('已删除', 'ok')
                }}
              >
                <Trash2 size={11} className="text-[var(--err)]" />
              </button>
            </div>

            {/* 等待人工的运行要能就地处理。导航上的待办徽标指向这一页，
                而这里以前只有只读的事件列表——刷新页面后那些卡在审批上的
                运行在整个界面里没有任何入口可以推进。 */}
            {pendingHere.length > 0 && (
              <div className="mb-3 overflow-hidden rounded-lg border" style={{ borderColor: 'var(--warn)' }}>
                {pendingHere.map((a) => <ApprovalCard key={a.id} approval={a} />)}
              </div>
            )}

            {selected.error && (
              <pre className="mono mb-3 whitespace-pre-wrap rounded border p-2 text-[11px] text-[var(--err)]"
                   style={{ borderColor: 'var(--err)' }}>
                {selected.error}
              </pre>
            )}

            <Block title="输入" data={selected.input} />
            <Block title="成果" data={selected.output} />
            <Block title="用量" data={selected.usage} />

            <div className="mb-1.5 mt-4 text-[11px] font-semibold uppercase tracking-wide text-faint">
              执行轨迹（{events.length} 条事件）
            </div>
            <div className="rounded border">
              {events.map((e) => (
                <div key={e.seq} className="flex gap-2 border-b px-2 py-1 text-[10.5px] last:border-0">
                  <span className="w-10 shrink-0 text-faint">#{e.seq}</span>
                  <span className="w-32 shrink-0 text-[var(--accent)]">{e.type}</span>
                  <span className="w-24 shrink-0 truncate text-dim">{e.node_id ?? ''}</span>
                  <span className="min-w-0 flex-1 break-all text-faint">
                    {JSON.stringify(e.data).slice(0, 240)}
                  </span>
                </div>
              ))}
              {!events.length && <div className="p-3 text-center text-[11px] text-faint">没有事件记录</div>}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function Block({ title, data }: { title: string; data: any }) {
  if (!data || !Object.keys(data).length) return null
  return (
    <div className="mb-3">
      <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-faint">{title}</div>
      <pre className="mono max-h-64 overflow-auto whitespace-pre-wrap break-words rounded border bg-panel p-2 text-[11px] leading-relaxed">
        {JSON.stringify(data, null, 2)}
      </pre>
    </div>
  )
}
