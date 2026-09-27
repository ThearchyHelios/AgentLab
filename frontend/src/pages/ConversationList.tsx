import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useNavigate, type NavigateFunction } from 'react-router-dom'
import {
  ArchiveRestore, ArrowLeft, Check, MessageSquarePlus, PanelLeftClose, PanelLeftOpen, Pencil, RotateCw, Search,
  Trash2, X,
} from 'lucide-react'
import clsx from 'clsx'
import { confirmDialog, IconButton, isComposing, Skeleton, StatusBadge, toast, useTicker } from '../components/ui'
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
 *
 * 删除先进回收站（归档），回收站在这一栏底部：撤销只有几秒，误删一个聊了七轮的
 * 对话，过了那几秒就该还有地方找回来。真正的删除只在回收站里做，而且要确认。
 */
export function ConversationList() {
  const { list, trash, showTrash, currentId, loading, error, load, create, rename, archive, restore, setShowTrash } =
    useConversations()
  const byConversation = useChat((s) => s.byConversation)
  const forget = useChat((s) => s.forget)
  const navigate = useNavigate()
  // 相对时间和分组一分钟重算一次就够：「刚刚」「3 分钟前」不需要更细
  const now = useTicker(60_000)

  const narrow = useNarrow()
  const [collapsed, setCollapsed] = useState(narrow)
  // 跨过断点时跟着变；窄屏上挑了一个对话（或新建、从回收站点开）就把抽屉收回去
  useEffect(() => { setCollapsed(narrow) }, [narrow])
  useEffect(() => { if (narrow) setCollapsed(true) }, [currentId, narrow])

  // 切会话 = 换地址，不是改 store。store 那个 currentId 是 URL 的派生缓存，
  // 在这里直接 set 的话，地址栏不动、刷新就跳回旧的那个，前进后退也全失效
  const open = (id: string) => {
    navigate(`/chat/${id}`)
    // 窄屏上点的就是当前这个时地址不变，抽屉也得收
    if (narrow) setCollapsed(true)
  }

  const startNew = async () => {
    try {
      const id = await create()
      setShowTrash(false)
      navigate(`/chat/${id}`)
    } catch (e) {
      toast.error(e)
    }
  }

  // 不再先弹确认：回收站和撤销就是后悔药，每删一个都打断一次反而让人不看就点确定
  const drop = async (c: Conversation) => {
    const index = list.findIndex((x) => x.id === c.id)
    const wasCurrent = c.id === currentId
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
    toast.ok(`「${c.title || '新对话'}」已移到回收站`, {
      duration: 8000,
      action: {
        label: '撤销',
        onClick: () => {
          void restore(c, index).then(
            () => { if (wasCurrent) navigate(`/chat/${c.id}`) },
            (e) => toast.error(e),
          )
        },
      },
    })
  }
  const [editing, setEditing] = useState<string | null>(null)
  const [query, setQuery] = useState('')

  useEffect(() => { void load() }, [load])
  // 断开期间列表可能取失败了；连回来之后自己补上，不用人去刷新
  useOnReconnect(load)

  // 这里**不**自动建空会话。试过，代价是每次进页面都留下一条"新对话"：
  // create 是异步的，StrictMode 把 effect 跑两遍，两次都看到空列表于是各建
  // 一条，列表很快被空壳淹掉。真正需要会话的时刻是用户开口那一下，
  // ChatPage 的 send 会在那时按需建——在此之前空列表配空态提示就是对的。

  // 同名的会话不少（同一句「上月销量」「orders 里…」常常问好几回），光靠翻很难找；
  // 最后一问也算进去，标题常常只是第一句话
  const needle = query.trim().toLowerCase()
  const shown = useMemo(() => (needle
    ? list.filter((c) => `${c.title} ${c.last_question ?? ''}`.toLowerCase().includes(needle))
    : list), [list, needle])
  const groups = useMemo(() => groupByDay(shown, now), [shown, now])

  const listPanel = (
    <aside className="flex w-60 shrink-0 flex-col border-r bg-panel" aria-label="对话列表">
      {/* 和右边的页头同高（48px）、同一条底线：两栏顶边对齐 */}
      <div className="flex h-12 shrink-0 items-center gap-1 border-b px-2">
        <button className="btn btn-sm btn-ghost flex-1 justify-start gap-1.5 text-xs"
                onClick={() => void startNew()}>
          <MessageSquarePlus size={13} /> 新对话
        </button>
        <button className="btn btn-sm btn-ghost" title="收起" aria-label="收起对话列表"
                onClick={() => setCollapsed(true)}>
          <PanelLeftClose size={13} />
        </button>
      </div>

      {/* 有筛选词时一直留着：删掉一条、列表不够 8 条时框跟着消失的话，筛选词还在生效，
          剩下的对话全被藏住，只有刷新页面才清得掉 */}
      {(list.length >= 8 || !!query) && (
        <div className="relative px-2 pb-1 pt-2">
          <Search size={12} aria-hidden
                  className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 mt-0.5 text-faint" />
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
        {!list.length && !loading && !error && !query && (
          <p className="px-2 py-4 text-2xs text-faint">还没有对话。在右边问第一句，就会出现在这里。</p>
        )}
        {!shown.length && !!query.trim() && (
          <p className="px-2 py-4 text-2xs leading-relaxed text-faint">
            没有标题或问题里带「{query.trim()}」的对话
          </p>
        )}
        {groups.map((g) => (
          <section key={g.label} aria-label={g.label}>
            <div className="px-2 pb-0.5 pt-2.5 text-2xs text-faint">{g.label}</div>
            {g.items.map((c) => {
              const turns = byConversation[c.id]
              // 取回内存的以内存为准（它是这一刻的）；没打开过的看列表接口带来的最后一轮状态
              const live = turns ? liveStatus(turns) : listStatus(c)
              const running = stillRunning(c, turns)
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
                          {live && <StatusText code={live} tail={c.turn_count > 0} />}
                          {c.turn_count > 0 ? `${c.turn_count} 轮` : live ? '' : '还没问过'}
                        </div>
                      </button>
                      {/* 操作浮在右侧，悬停或键盘聚焦时才出现，不占标题的宽度 */}
                      <RowActions>
                        <IconButton size="md" className="btn-xs" label={`重命名「${c.title}」`} title="重命名"
                                    icon={<Pencil size={11} />} onClick={() => setEditing(c.id)} />
                        {/* 正在跑的不给删：底下还挂着一条事件流和一个真在跑、在计费的运行 */}
                        <IconButton size="md" className="btn-xs" label={`删除「${c.title}」`}
                                    title={running ? RUNNING_NO_DELETE : '删除（先放进回收站）'}
                                    disabled={running} icon={<Trash2 size={11} />} onClick={() => void drop(c)} />
                      </RowActions>
                    </>
                  )}
                </div>
              )
            })}
          </section>
        ))}
      </div>

      <div className="shrink-0 border-t px-1.5 py-1.5">
        <button
          className="flex w-full items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs text-faint transition-colors hover:bg-hover hover:text-dim"
          title="删掉的对话在这里，可以恢复"
          aria-label={`打开回收站${trash.length ? `（${trash.length} 个对话）` : ''}`}
          onClick={() => setShowTrash(true)}
        >
          <Trash2 size={12} aria-hidden />
          <span className="flex-1 text-left">回收站</span>
          {trash.length > 0 && <span className="tnum text-2xs">{trash.length}</span>}
        </button>
      </div>
    </aside>
  )

  const strip = (
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
  if (collapsed) return strip

  // 相对时间跟着这一层每分钟重渲染
  const panel = showTrash
    ? <TrashView items={trash} currentId={currentId} onBack={() => setShowTrash(false)} />
    : listPanel
  if (!narrow) return panel
  // 窄屏上 240px 的列表和正文抢宽度，正文被挤成一字一行、整页横向溢出：列表改成浮在
  // 正文上面的抽屉，收起条留在原处占位，开合时正文不跟着跳
  return (
    <>
      {strip}
      <div className="absolute inset-0 z-20 bg-black/40" aria-hidden onClick={() => setCollapsed(true)} />
      <div className="absolute inset-y-0 left-0 z-30 flex" style={{ boxShadow: 'var(--elev-3)' }} data-conversation-drawer="">
        {panel}
      </div>
    </>
  )
}

/** 窄于 768px 算窄屏：会话列表默认收起，展开时是抽屉 */
function useNarrow(): boolean {
  const query = '(max-width: 767px)'
  const [narrow, setNarrow] = useState(() => typeof matchMedia === 'function' && matchMedia(query).matches)
  useEffect(() => {
    if (typeof matchMedia !== 'function') return
    const mq = matchMedia(query)
    const on = () => setNarrow(mq.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])
  return narrow
}

/** 行尾的浮层按钮：悬停或键盘聚焦到这一行才出现，渐隐背景盖住标题尾巴而不挤占宽度 */
function RowActions({ children }: { children: ReactNode }) {
  return (
    <div
      className="pointer-events-none absolute inset-y-0 right-0 flex items-center gap-0.5 rounded-r-lg pl-6 pr-1 opacity-0 transition-opacity group-focus-within:pointer-events-auto group-focus-within:opacity-100 group-hover:pointer-events-auto group-hover:opacity-100"
      style={{ background: 'linear-gradient(to right, transparent, var(--bg-hover) 34%)' }}
    >
      {children}
    </div>
  )
}

/**
 * 回收站。点开一条是预览（问数据页会说明它在回收站里、不能接着问）；恢复放回列表，
 * 彻底删除要确认——这一下之后就真没了。正在跑的（点开预览时发现运行还活着、接回去了）
 * 和列表里一样不给删
 */
function TrashView({ items, currentId, onBack }: {
  items: Conversation[]
  currentId: string | null
  onBack: () => void
}) {
  const navigate = useNavigate()
  const emptyTrash = useConversations((s) => s.emptyTrash)
  const byConversation = useChat((s) => s.byConversation)
  const sorted = useMemo(() => [...items].sort((a, b) => stamp(b) - stamp(a)), [items])
  const idle = items.filter((c) => !stillRunning(c, byConversation[c.id]))

  const emptyAll = async () => {
    // 确认框开着的时候谁又跑起来了，以点确认那一刻为准，所以挑两次
    const pick = () => {
      const mem = useChat.getState().byConversation
      const trash = useConversations.getState().trash
      return {
        doomed: trash.filter((c) => !stillRunning(c, mem[c.id])),
        kept: trash.filter((c) => stillRunning(c, mem[c.id])),
      }
    }
    const first = pick()
    if (!first.doomed.length) return
    const rounds = first.doomed.reduce((n, c) => n + (c.turn_count || 0), 0)
    const ok = await confirmDialog({
      title: '清空回收站？',
      body: `里面的 ${first.doomed.length} 个对话会被彻底删除，删除后找不回来。`,
      consequences: [
        ...(rounds ? [`一共 ${rounds} 轮问答和它们的结论`] : []),
        ...(first.kept.length ? [`${titles(first.kept)}正在运行，这次不删，留在回收站里`] : []),
        '跑过的运行记录不受影响，还在「记录」页',
      ],
      confirmLabel: '全部删除',
      danger: true,
    })
    if (!ok) return
    const { doomed, kept } = pick()
    const ids = doomed.map((c) => c.id)
    const failed = ids.length ? await emptyTrash(ids) : 0
    const left = new Set(useConversations.getState().trash.map((c) => c.id))
    for (const id of ids) if (!left.has(id)) useChat.getState().forget(id)
    if (currentId && ids.includes(currentId) && !left.has(currentId)) navigate('/chat', { replace: true })
    if (failed) toast.error(`有 ${failed} 个对话没删掉，稍后再试一次`)
    else if (kept.length) toast.info(`删掉了 ${ids.length} 个；${titles(kept)}正在运行，留在回收站里，停下来之后再删`)
    else toast.ok('回收站清空了')
  }

  return (
    <aside className="flex w-60 shrink-0 flex-col border-r bg-panel" aria-label="回收站">
      <div className="flex h-12 shrink-0 items-center gap-1 border-b px-2">
        <IconButton label="返回对话列表" icon={<ArrowLeft size={13} />} onClick={onBack} />
        <h2 className="min-w-0 flex-1 truncate text-xs font-semibold">回收站</h2>
        {items.length > 0 && (
          <button className="btn btn-xs btn-ghost text-[var(--err)]" aria-label="清空回收站"
                  disabled={!idle.length} title={idle.length ? undefined : '回收站里的对话正在运行，先停下来再删'}
                  onClick={() => void emptyAll()}>
            清空
          </button>
        )}
      </div>
      <p className="px-3 pb-1 pt-2 text-2xs leading-relaxed text-faint">
        删掉的对话先放在这里。恢复后回到列表；彻底删除后就找不回来了。
      </p>
      <div className="min-h-0 flex-1 overflow-y-auto px-1.5 pb-2">
        {!items.length && (
          <p className="px-2 py-4 text-2xs leading-relaxed text-faint">回收站是空的。</p>
        )}
        {sorted.map((c) => {
          const active = c.id === currentId
          const title = c.title || '新对话'
          const when = c.last_active_at ?? c.created_at
          const turns = byConversation[c.id]
          const live = turns ? liveStatus(turns) : listStatus(c)
          const running = stillRunning(c, turns)
          return (
            <div key={c.id}
                 className={clsx('group relative mb-0.5 rounded-lg text-xs',
                   active ? 'bg-hover text-fg' : 'text-dim hover:bg-hover')}>
              <button
                className="block w-full rounded-lg px-2 py-1.5 text-left"
                onClick={() => navigate(`/chat/${c.id}`)}
                aria-current={active ? 'page' : undefined}
                title={[title, c.last_question && c.last_question !== c.title ? `最后一问：${c.last_question}` : '',
                  when ? `最后活跃 ${formatDateTime(when)}` : ''].filter(Boolean).join('\n')}
              >
                <div className="flex items-center gap-1.5">
                  {live && <StatusBadge status={live} size={11} decorative />}
                  <span className="min-w-0 flex-1 truncate">{title}</span>
                  <span className="tnum shrink-0 text-2xs text-faint">{when ? formatRelative(when) : ''}</span>
                </div>
                <div className="mt-px truncate text-2xs text-faint">
                  {live && <StatusText code={live} tail={c.turn_count > 0} />}
                  {c.turn_count > 0 ? `${c.turn_count} 轮` : live ? '' : '还没问过'}
                </div>
              </button>
              <RowActions>
                <IconButton size="md" className="btn-xs" label={`恢复「${title}」`} title="恢复到列表"
                            icon={<ArchiveRestore size={11} />}
                            onClick={() => void restoreConversation(c, navigate)} />
                <IconButton size="md" className="btn-xs hover:text-[var(--err)]" label={`彻底删除「${title}」`}
                            title={running ? RUNNING_NO_DELETE : '彻底删除'} disabled={running}
                            icon={<Trash2 size={11} />}
                            onClick={() => void purgeConversation(c, navigate)} />
              </RowActions>
            </div>
          )
        })}
      </div>
    </aside>
  )
}

/** 从回收站拿回来。不是正在看的那个，就在提示里给一个「打开」 */
export async function restoreConversation(c: Conversation, navigate: NavigateFunction): Promise<void> {
  const st = useConversations.getState()
  try {
    await st.restore(c)
  } catch (e) {
    toast.error(e)
    return
  }
  const title = c.title || '新对话'
  if (st.currentId === c.id) {
    // 正在看的那个恢复了：左栏回到列表，它就在里面，可以接着问
    st.setShowTrash(false)
    toast.ok(`「${title}」已恢复到列表`)
  } else {
    toast.ok(`「${title}」已恢复到列表`, { action: { label: '打开', onClick: () => navigate(`/chat/${c.id}`) } })
  }
}

/**
 * 彻底删除：先确认，确认了才发 DELETE。删的是正在看的那个就离开它。删掉了返回 true。
 * 正在跑的不删，确认之后再看一次：确认框开着的时候，核对可能刚把一次还活着的运行接回来
 */
export async function purgeConversation(c: Conversation, navigate: NavigateFunction): Promise<boolean> {
  const title = c.title || '新对话'
  const running = () => stillRunning(c, useChat.getState().byConversation[c.id])
  const refuse = () => { toast.info(`「${title}」正在运行，先停下来再删`); return false }
  if (running()) return refuse()
  const ok = await confirmDialog({
    title: `彻底删除「${title}」？`,
    body: '删除后找不回来。',
    consequences: [
      ...(c.turn_count ? [`${c.turn_count} 轮问答和它们的结论一起删除`] : []),
      '跑过的运行记录不受影响，还在「记录」页',
    ],
    confirmLabel: '彻底删除',
    danger: true,
  })
  if (!ok) return false
  if (running()) return refuse()
  const st = useConversations.getState()
  try {
    await st.purge(c.id)
  } catch (e) {
    toast.error(e)
    return false
  }
  useChat.getState().forget(c.id)
  if (st.currentId === c.id) navigate('/chat', { replace: true })
  toast.ok(`已彻底删除「${title}」`)
  return true
}

/**
 * 这个会话此刻值得标出来的状态（已经取回内存的）。正常完成的不标：正常态安静，
 * 异常态才醒目。
 */
function liveStatus(turns: ChatTurn[]): string | null {
  if (!turns.length) return null
  if (busyTurn(turns)) return 'running'
  if (turns.some((t) => t.phase === 'waiting')) return 'waiting'
  const last = turns[turns.length - 1]
  if (last.phase === 'error') return 'failed'
  if (last.phase === 'suspended') return 'suspended'
  return null
}

/** 没打开过的会话：列表接口带来的最后一轮状态。完成、取消的不标，同上 */
const LIST_STATUS: Partial<Record<NonNullable<Conversation['last_status']>, string>> = {
  running: 'running', waiting: 'waiting', error: 'failed', suspended: 'suspended',
}
const listStatus = (c: Conversation): string | null => (c.last_status && LIST_STATUS[c.last_status]) || null

/**
 * 这个会话底下有没有正在跑的东西。取回内存的看轮次的相位，没打开过的看列表接口的
 * last_status。正在跑的不给删：删掉只关得掉这一头的事件流，后端的运行照样跑、照样计费，
 * 从此没有哪一页还看得见它
 */
export function stillRunning(c: Conversation, turns: ChatTurn[] | undefined): boolean {
  return turns ? !!busyTurn(turns) : c.last_status === 'running'
}

export const RUNNING_NO_DELETE = '这个对话正在运行，先停下来再删'

const titles = (list: Conversation[]) => list.map((c) => `「${c.title || '新对话'}」`).join('、')

/** 行里第二行开头那个状态词：去掉颜色、关掉动效时也认得出这条在跑 */
function StatusText({ code, tail }: { code: string; tail: boolean }) {
  return (
    <span style={{ color: code === 'running' ? 'var(--st-running)' : undefined }}
          className={clsx(code !== 'running' && statusTone(code))}>
      {statusLabel(code, { short: true })}
      {tail ? ' · ' : ''}
    </span>
  )
}

const statusTone = (code: string) =>
  code === 'failed' ? 'text-[var(--st-failed)]' : 'text-[var(--st-waiting)]'

const stamp = (c: Conversation) => parseServerTime(c.last_active_at ?? c.created_at ?? '')?.getTime() ?? 0

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
    const i = buckets.findIndex(([, hit]) => hit(stamp(c)))
    out[i].items.push(c)
  }
  return out.filter((g) => g.items.length)
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
