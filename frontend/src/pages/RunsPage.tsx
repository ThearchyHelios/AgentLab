import { useEffect, useMemo, useState } from 'react'
import { Code2, GitFork, History, RefreshCw, Trash2 } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { Empty, Spinner, StatusDot, useToast } from '../components/ui'
import { ApprovalCard } from '../run/RunPanel'
import { AssistantStream, type StreamTurn } from '../run/AssistantStream'
import { decodeRun, formatDuration, summarizeRun } from '../run/decode'
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
  const [raw, setRaw] = useState(false)
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
    setEvents([])
    setEvents(await api.runs.events(run.id).catch(() => []))
  }

  const shown = runs.filter(
    (r) => !filter || r.workflow_name?.includes(filter) || r.status === filter
      || summarizeRun(r.input, r.output).includes(filter),
  )

  // 详情走和画布助手栏、问数据页同一个解码器和同一个组件：三处各写一套的话，
  // 同一次运行会被讲成三个不同的故事，而用户没法判断哪个是真的
  const turn = useMemo<StreamTurn[]>(() => {
    if (!selected) return []
    const finished = [...events].reverse().find((e) => e.type === 'run.finished')
    const failedEvent = [...events].reverse().find((e) => e.type === 'run.failed')
    return [{
      id: selected.id,
      phase: failedEvent || selected.status === 'failed' ? 'error'
        : pendingHere.length ? 'waiting'
        : selected.status === 'running' ? 'running' : 'done',
      status: STATUS_TEXT[selected.status] ?? selected.status,
      steps: decodeRun(events),
      // run 对象是启动时的快照，成果在 run.finished 事件里；历史记录两者
      // 都已落库，取事件那份更接近"当时真的输出了什么"
      output: finished?.data?.output ?? selected.output ?? null,
      error: failedEvent ? String(failedEvent.data?.error ?? '') : selected.error || undefined,
      runClass: selected.run_class,
    }]
  }, [selected, events, pendingHere.length])

  return (
    <div className="flex h-full">
      <div className="flex w-[380px] shrink-0 flex-col border-r">
        <div className="flex items-center gap-2 border-b p-2">
          <input className="field" placeholder="按名称、状态或内容筛选…" value={filter}
                 onChange={(e) => setFilter(e.target.value)} />
          <button className="btn btn-sm" onClick={load}><RefreshCw size={11} /></button>
        </div>
        <div className="flex-1 overflow-y-auto">
          {loading && <div className="p-4"><Spinner /></div>}
          {!loading && !shown.length && <Empty icon={<History size={22} />} title="还没有运行记录" />}
          {shown.map((run) => {
            // 一行只有"临时图 · 1.2s · 340 tok"的话，十条运行长得一模一样，
            // 要分清哪条是哪条只能挨个点开
            const summary = summarizeRun(run.input, run.output)
            return (
              <button
                key={run.id}
                onClick={() => open(run)}
                className={clsx(
                  'flex w-full flex-col gap-0.5 border-b px-3 py-2 text-left hover:bg-hover',
                  selected?.id === run.id && 'bg-hover',
                )}
              >
                <div className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate text-[12px]">
                    {run.workflow_name || '临时图'}
                  </span>
                  {run.run_class === 'formal' && (
                    <span className="chip" style={{ color: 'var(--ok)', borderColor: 'var(--ok)' }}>正式</span>
                  )}
                  {(run.output as any)?._issuance?.tier === 'withheld' && (
                    <span className="chip" style={{ color: 'var(--err)' }}>不予出具</span>
                  )}
                  <StatusDot status={run.status} />
                </div>
                {summary && (
                  <div className="truncate text-[11px] text-dim" title={summary}>{summary}</div>
                )}
                <div className="flex gap-2 text-[10px] text-faint">
                  <span>{run.created_at ? new Date(run.created_at).toLocaleString('zh-CN') : ''}</span>
                  {run.usage?.duration_ms ? <span>{formatDuration(run.usage.duration_ms)}</span> : null}
                  {run.usage?.total_tokens ? <span>{run.usage.total_tokens} tok</span> : null}
                  {run.usage?.cost_usd ? <span>${Number(run.usage.cost_usd).toFixed(4)}</span> : null}
                </div>
              </button>
            )
          })}
        </div>
      </div>

      <div className="flex min-w-0 flex-1 flex-col">
        {!selected && <Empty title="选一条运行记录" hint="左侧点击可以查看完整的执行轨迹、输入输出和用量。" />}
        {selected && (
          <>
            <div className="flex shrink-0 items-start gap-3 border-b px-4 py-2.5">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 text-sm font-semibold">
                  {selected.workflow_name || '临时图'}
                  {selected.run_class === 'formal'
                    ? <span className="chip" style={{ color: 'var(--ok)', borderColor: 'var(--ok)' }}>正式 v{selected.version}</span>
                    : <span className="chip">探索性</span>}
                </div>
                <div className="mono truncate text-[10.5px] text-faint">
                  {selected.id}
                  {selected.version_hash && ` · 版本 ${selected.version_hash.slice(0, 10)}`}
                  {selected.manifest_hash && ` · 清单 ${selected.manifest_hash.slice(0, 10)}`}
                  {selected.started_by && ` · by ${selected.started_by}`}
                </div>
              </div>
              <StatusDot status={selected.status} />
              <button
                className={clsx('btn btn-sm btn-ghost', raw && 'text-[var(--accent)]')}
                title={raw ? '回到可读视图' : `看原始事件（${events.length} 条）`}
                onClick={() => setRaw((v) => !v)}
              >
                <Code2 size={11} />
              </button>
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
              <div className="shrink-0 border-b" style={{ borderColor: 'var(--warn)' }}>
                {pendingHere.map((a) => (
                  <ApprovalCard key={a.id} approval={a} onResolved={() => open(selected)} />
                ))}
              </div>
            )}

            <div className="min-h-0 flex-1">
              {raw ? <RawEvents events={events} /> : <AssistantStream turns={turn} />}
            </div>

            <div className="flex shrink-0 items-center gap-3 border-t px-4 py-1.5 text-[10px] text-faint">
              <span>{events.length} 条事件</span>
              {selected.usage?.total_tokens ? <span>{selected.usage.total_tokens} tok</span> : null}
              {selected.usage?.cost_usd ? <span>${Number(selected.usage.cost_usd).toFixed(4)}</span> : null}
              {selected.usage?.duration_ms ? <span>{formatDuration(selected.usage.duration_ms)}</span> : null}
              <span className="flex-1" />
              {selected.input && !!Object.keys(selected.input).length && (
                <span className="truncate" title={JSON.stringify(selected.input, null, 2)}>
                  输入：{summarizeRun(selected.input)}
                </span>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

const STATUS_TEXT: Record<string, string> = {
  succeeded: '完成', failed: '失败', running: '执行中',
  interrupted: '等待人工介入', cancelled: '已取消', queued: '排队中',
}

/** 原始事件。翻译层出问题时用来对照。 */
function RawEvents({ events }: { events: RunEvent[] }) {
  if (!events.length) {
    return <div className="p-3 text-center text-[11px] text-faint">没有事件记录</div>
  }
  return (
    <div className="h-full overflow-y-auto">
      {events.map((e) => (
        <div key={e.seq} className="flex gap-2 border-b px-3 py-1 text-[10.5px] last:border-0">
          <span className="w-10 shrink-0 text-faint">#{e.seq}</span>
          <span className="w-32 shrink-0 text-[var(--accent)]">{e.type}</span>
          <span className="w-24 shrink-0 truncate text-dim">{e.node_id ?? ''}</span>
          <span className="min-w-0 flex-1 break-all text-faint">
            {JSON.stringify(e.data).slice(0, 240)}
          </span>
        </div>
      ))}
    </div>
  )
}
