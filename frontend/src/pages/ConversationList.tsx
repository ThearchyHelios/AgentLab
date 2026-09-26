import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Check, MessageSquarePlus, PanelLeftClose, PanelLeftOpen, Pencil, RotateCw, Search, Trash2, X } from 'lucide-react'
import clsx from 'clsx'
import { confirmDialog, isComposing, Skeleton, StatusBadge, toast } from '../components/ui'
import { formatDateTime, formatRelative, parseServerTime } from '../lib/format'
import { statusLabel } from '../lib/status'
import { humanizeError } from '../lib/errors'
import { busyTurn, useChat, type ChatTurn } from '../store/chat'
import { useOnReconnect } from '../store/catalog'
import { useConversations } from '../store/conversations'
import type { Conversation } from '../types'

/**
 * 左侧会话列表。
 *
 * 之前对话只活在内存里，刷新页面就没了——用户那三次提问在界面上再也找不回来，
 * 只剩 runs 表里三条互不相干的记录。这一栏是它们重新变成"一次对话"的入口。
 *
 * 只在问数据页出现。画布右栏的 Copilot 也有自己的会话，但那是依附某张图的
 * "改图指令"，和这里的"问题"不是一回事，混进同一个列表只会让两种都更难找。
 *
 * 历史会话是反复回来取结论的地方，所以每一条要一眼看出三件事：是哪次聊天
 * （标题）、什么时候（相对时间）、现在怎么样（在跑、等审批、挂了）。
 */
export function ConversationList() {
  const { list, currentId, loading, error, load, create, rename, archive, restore } = useConversations()
  const byConversation = useChat((s) => s.byConversation)
  const forget = useChat((s) => s.forget)
  const navigate = useNavigate()
  // 相对时间和分组每分钟重算一次；格式化时取当下，刚问过的那条才是「刚刚」
  const now = useMinute()

  // 切会话 = 换地址，不是改 store。store 那个 currentId 是 URL 的派生缓存，
  // 在这里直接 set 的话，地址栏不动、刷新就跳回旧的那个，前进后退也全失效
  const open = (id: string) => navigate(`/chat/${id}`)

  const startNew = async () => {
    try {
      const id = await create()
      navigate(`/chat/${id}`)
    } catch (e) {
      toast.error(e)
    }
  }

  const drop = async (c: Conversation) => {
    const index = list.findIndex((x) => x.id === c.id)
    const ok = await confirmDialog({
      title: `删除「${c.title || '新对话'}」？`,
      body: '它会从列表里移走。删除后几秒内可以撤销。',
      consequences: c.turn_count ? [`${c.turn_count} 轮问答和它们的结论一起移走`] : undefined,
      confirmLabel: '删除',
      danger: true,
    })
    if (!ok) return
    let next: string | null
    try {
      next = await archive(c.id)
    } catch (e) {
      toast.error(e)
      return
    }
    forget(c.id)
    // 删的是当前这个才需要换地方；archive 会把该去哪告诉我们
    if (next !== currentId) navigate(next ? `/chat/${next}` : '/chat', { replace: true })
    toast.ok(`已删除「${c.title || '新对话'}」`, {
      duration: 8000,
      action: {
        label: '撤销',
        onClick: () => {
          void restore(c, index).then(
            () => navigate(`/chat/${c.id}`),
            (e) => toast.error(e),
          )
        },
      },
    })
  }
  const [collapsed, setCollapsed] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  const [query, setQuery] = useState('')

  useEffect(() => { void load() }, [load])
  // 断开期间列表可能取失败了；连回来之后自己补上，不用人去刷新
  useOnReconnect(load)

  // 这里**不**自动建空会话。试过，代价是每次进页面都留下一条"新对话"：
  // create 是异步的，StrictMode 把 effect 跑两遍，两次都看到空列表于是各建
  // 一条，列表很快被空壳淹掉。真正需要会话的时刻是用户开口那一下，
  // ChatPage 的 send 会在那时按需建——在此之前空列表配空态提示就是对的。

  // 同名的会话不少（「我是谁」「shop 里…」各有好几条），光靠翻很难找；
  // 最后一问也算进去，标题常常只是第一句话
  const needle = query.trim().toLowerCase()
  const shown = useMemo(() => (needle
    ? list.filter((c) => `${c.title} ${c.last_question ?? ''}`.toLowerCase().includes(needle))
    : list), [list, needle])
  const groups = useMemo(() => groupByDay(shown, now), [shown, now])

  if (collapsed) {
    return (
      <div className="flex w-9 shrink-0 flex-col items-center gap-1 border-r bg-panel py-2">
        <button className="btn btn-sm btn-ghost" title="展开对话列表" aria-label="展开对话列表"
                onClick={() => setCollapsed(false)}>
          <PanelLeftOpen size={13} />
        </button>
        <button className="btn btn-sm btn-ghost" title="新对话" aria-label="新对话" onClick={() => void startNew()}>
          <MessageSquarePlus size={13} />
        </button>
      </div>
    )
  }

  return (
    <aside className="flex w-60 shrink-0 flex-col border-r bg-panel" aria-label="对话列表">
      <div className="flex items-center gap-1 px-2 py-2">
        <button className="btn btn-sm btn-ghost flex-1 justify-start gap-1.5 text-xs"
                onClick={() => void startNew()}>
          <MessageSquarePlus size={13} /> 新对话
        </button>
        <button className="btn btn-sm btn-ghost" title="收起" aria-label="收起对话列表"
                onClick={() => setCollapsed(true)}>
          <PanelLeftClose size={13} />
        </button>
      </div>

      {list.length >= 8 && (
        <div className="relative px-2 pb-1">
          <Search size={12} aria-hidden
                  className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 -mt-0.5 text-faint" />
          <input
            type="search"
            className="field h-7 w-full pl-7 text-xs"
            placeholder="找对话…"
            aria-label="按标题或问过的话找对话"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Escape' && !isComposing(e)) setQuery('') }}
          />
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto px-1.5 pb-2">
        {!list.length && loading && <Skeleton rows={6} height={30} gap={8} className="px-1.5 pt-1" />}
        {!list.length && !loading && !!error && (
          <div className="px-2 py-4 text-2xs leading-relaxed text-faint" role="alert">
            <div className="text-xs text-dim">对话列表没取回来</div>
            <div className="mt-0.5">{humanizeError(error).title}</div>
            <button className="btn btn-xs mt-2" onClick={() => void load()}>
              <RotateCw size={11} /> 重试
            </button>
          </div>
        )}
        {!list.length && !loading && !error && (
          <p className="px-2 py-4 text-2xs text-faint">还没有对话。在右边问第一句，就会出现在这里。</p>
        )}
        {!!list.length && !shown.length && (
          <p className="px-2 py-4 text-2xs leading-relaxed text-faint">
            没有标题或问题里带「{query.trim()}」的对话
          </p>
        )}
        {groups.map((g) => (
          <section key={g.label} aria-label={g.label}>
            <div className="px-2 pb-0.5 pt-2.5 text-2xs text-faint">{g.label}</div>
            {g.items.map((c) => {
              const turns = byConversation[c.id]
              const live = liveStatus(turns)
              const running = !!busyTurn(turns)
              const active = c.id === currentId
              const when = c.last_active_at ?? c.created_at
              return (
                <div
                  key={c.id}
                  className={clsx(
                    'group relative mb-0.5 rounded-lg text-xs',
                    active ? 'bg-hover text-fg' : 'text-dim hover:bg-hover',
                  )}
                >
                  {editing === c.id ? (
                    <div className="px-2 py-1.5">
                      <RenameField
                        initial={c.title}
                        onCancel={() => setEditing(null)}
                        onSubmit={(title) => { void rename(c.id, title); setEditing(null) }}
                      />
                    </div>
                  ) : (
                    <>
                      <button
                        className="block w-full rounded-lg px-2 py-1.5 text-left"
                        onClick={() => open(c.id)}
                        aria-current={active ? 'page' : undefined}
                        title={[
                          c.title || '新对话',
                          c.last_question && c.last_question !== c.title ? `最后一问：${c.last_question}` : '',
                          when ? formatDateTime(when) : '',
                        ].filter(Boolean).join('\n')}
                      >
                        <div className="flex items-center gap-1.5">
                          {live && <StatusBadge status={live} size={11} decorative />}
                          <span className="min-w-0 flex-1 truncate">{c.title || '新对话'}</span>
                          <span className="tnum shrink-0 text-2xs text-faint">
                            {when ? formatRelative(when) : ''}
                          </span>
                        </div>
                        <div className="mt-px truncate text-2xs text-faint">
                          {/* 状态写成字：去掉颜色、关掉动效时也认得出这条在跑 */}
                          {live && (
                            <span style={{ color: live === 'running' ? 'var(--st-running)' : undefined }}
                                  className={clsx(live !== 'running' && statusTone(live))}>
                              {statusLabel(live, { short: true })}
                              {c.turn_count > 0 ? ' · ' : ''}
                            </span>
                          )}
                          {c.turn_count > 0 ? `${c.turn_count} 轮` : live ? '' : '还没问过'}
                        </div>
                      </button>
                      {/* 操作浮在右侧，悬停或键盘聚焦时才出现，不占标题的宽度 */}
                      <div
                        className="pointer-events-none absolute inset-y-0 right-0 flex items-center gap-0.5 rounded-r-lg pl-6 pr-1 opacity-0 transition-opacity group-focus-within:pointer-events-auto group-focus-within:opacity-100 group-hover:pointer-events-auto group-hover:opacity-100"
                        style={{ background: 'linear-gradient(to right, transparent, var(--bg-hover) 34%)' }}
                      >
                        <button className="btn btn-xs btn-ghost" title="重命名" aria-label={`重命名「${c.title}」`}
                                onClick={() => setEditing(c.id)}>
                          <Pencil size={11} />
                        </button>
                        {/* 正在跑的不给删：底下还挂着一条事件流和一个真在跑、在计费的运行 */}
                        <button
                          className="btn btn-xs btn-ghost"
                          title={running ? '这个对话正在运行，先停下来再删' : '删除'}
                          aria-label={`删除「${c.title}」`}
                          disabled={running}
                          onClick={() => void drop(c)}
                        >
                          <Trash2 size={11} />
                        </button>
                      </div>
                    </>
                  )}
                </div>
              )
            })}
          </section>
        ))}
      </div>
    </aside>
  )
}

/**
 * 这个会话此刻值得标出来的状态。只看已经取回内存的会话——没打开过的，列表接口
 * 不带轮次状态，宁可不标也不猜。正常完成的不标：正常态安静，异常态才醒目。
 */
function liveStatus(turns: ChatTurn[] | undefined): string | null {
  if (!turns?.length) return null
  if (busyTurn(turns)) return 'running'
  if (turns.some((t) => t.phase === 'waiting')) return 'waiting'
  const last = turns[turns.length - 1]
  if (last.phase === 'error') return 'failed'
  if (last.phase === 'suspended') return 'suspended'
  return null
}

const statusTone = (code: string) =>
  code === 'failed' ? 'text-[var(--st-failed)]' : 'text-[var(--st-waiting)]'

/** 今天 / 昨天 / 近 7 天 / 更早：历史会话按「多久以前」找，比按字母找快 */
function groupByDay(list: Conversation[], now: number): { label: string; items: Conversation[] }[] {
  const start = new Date(now)
  start.setHours(0, 0, 0, 0)
  const today = start.getTime()
  const day = 86_400_000
  const buckets: [string, (t: number) => boolean][] = [
    ['今天', (t) => t >= today],
    ['昨天', (t) => t >= today - day],
    ['近 7 天', (t) => t >= today - 6 * day],
    ['更早', () => true],
  ]
  const out = buckets.map(([label]) => ({ label, items: [] as Conversation[] }))
  for (const c of list) {
    const t = parseServerTime(c.last_active_at ?? c.created_at)?.getTime() ?? 0
    const i = buckets.findIndex(([, hit]) => hit(t))
    out[i].items.push(c)
  }
  return out.filter((g) => g.items.length)
}

/** 相对时间一分钟刷一次就够：「刚刚」「3 分钟前」不需要更细 */
function useMinute(): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 60_000)
    return () => clearInterval(t)
  }, [])
  return now
}

function RenameField({ initial, onSubmit, onCancel }: {
  initial: string
  onSubmit: (title: string) => void
  onCancel: () => void
}) {
  const [text, setText] = useState(initial)
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => { ref.current?.select() }, [])

  return (
    <div className="flex items-center gap-0.5">
      <input
        ref={ref}
        className="field h-6 min-w-0 flex-1 px-1 text-xs"
        aria-label="对话标题"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !isComposing(e)) onSubmit(text)
          if (e.key === 'Escape' && !isComposing(e)) onCancel()
        }}
        autoFocus
      />
      <button className="btn btn-xs btn-ghost" aria-label="保存标题" onClick={() => onSubmit(text)}><Check size={11} /></button>
      <button className="btn btn-xs btn-ghost" aria-label="取消" onClick={onCancel}><X size={11} /></button>
    </div>
  )
}
