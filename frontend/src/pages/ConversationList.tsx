import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Check, MessageSquarePlus, PanelLeftClose, PanelLeftOpen, Pencil, Trash2, X } from 'lucide-react'
import clsx from 'clsx'
import { useChat } from '../store/chat'
import { useConversations } from '../store/conversations'

/**
 * 左侧会话列表。
 *
 * 之前对话只活在内存里，刷新页面就没了——用户那三次提问在界面上再也找不回来，
 * 只剩 runs 表里三条互不相干的记录。这一栏是它们重新变成"一次对话"的入口。
 *
 * 只在问数据页出现。画布右栏的 Copilot 也有自己的会话，但那是依附某张图的
 * "改图指令"，和这里的"问题"不是一回事，混进同一个列表只会让两种都更难找。
 */
export function ConversationList() {
  const { list, currentId, loading, load, create, rename, remove } = useConversations()
  const forget = useChat((s) => s.forget)
  const busy = useChat((s) => s.busy)
  const navigate = useNavigate()

  // 切会话 = 换地址，不是改 store。store 那个 currentId 是 URL 的派生缓存，
  // 在这里直接 set 的话，地址栏不动、刷新就跳回旧的那个，前进后退也全失效
  const open = (id: string) => navigate(`/chat/${id}`)

  const startNew = async () => {
    const id = await create()
    navigate(`/chat/${id}`)
  }

  const drop = async (id: string) => {
    forget(id)
    const next = await remove(id)
    // 删的是当前这个才需要换地方；remove 会把该去哪告诉我们
    if (next !== currentId) navigate(next ? `/chat/${next}` : '/chat', { replace: true })
  }
  const [collapsed, setCollapsed] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)

  useEffect(() => { void load() }, [load])

  // 这里**不**自动建空会话。试过，代价是每次进页面都留下一条"新对话"：
  // create 是异步的，StrictMode 把 effect 跑两遍，两次都看到空列表于是各建
  // 一条，列表很快被空壳淹掉。真正需要会话的时刻是用户开口那一下，
  // ChatPage 的 send 会在那时按需建——在此之前空列表配空态提示就是对的。

  if (collapsed) {
    return (
      <div className="flex w-9 shrink-0 flex-col items-center gap-1 border-r bg-panel py-2">
        <button className="btn btn-sm btn-ghost" title="展开对话列表"
                onClick={() => setCollapsed(false)}>
          <PanelLeftOpen size={13} />
        </button>
        <button className="btn btn-sm btn-ghost" title="新对话" onClick={() => void startNew()}>
          <MessageSquarePlus size={13} />
        </button>
      </div>
    )
  }

  return (
    <aside className="flex w-52 shrink-0 flex-col border-r bg-panel">
      <div className="flex items-center gap-1 px-2 py-2">
        <button className="btn btn-sm btn-ghost flex-1 justify-start gap-1.5 text-[12px]"
                onClick={() => void startNew()}>
          <MessageSquarePlus size={13} /> 新对话
        </button>
        <button className="btn btn-sm btn-ghost" title="收起" onClick={() => setCollapsed(true)}>
          <PanelLeftClose size={13} />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-1.5 pb-2">
        {!list.length && !loading && (
          <p className="px-2 py-4 text-[11px] text-faint">还没有对话</p>
        )}
        {list.map((c) => (
          <div
            key={c.id}
            className={clsx(
              'group mb-0.5 rounded-lg px-2 py-1.5 text-[12px]',
              c.id === currentId ? 'bg-hover text-fg' : 'text-dim hover:bg-hover',
            )}
          >
            {editing === c.id ? (
              <RenameField
                initial={c.title}
                onCancel={() => setEditing(null)}
                onSubmit={(title) => { void rename(c.id, title); setEditing(null) }}
              />
            ) : (
              <div className="flex items-center gap-1">
                <button
                  className="min-w-0 flex-1 text-left"
                  onClick={() => open(c.id)}
                  title={c.last_question || c.title}
                >
                  <div className="truncate">{c.title || '新对话'}</div>
                  {c.turn_count > 0 && (
                    <div className="truncate text-[10px] text-faint">{c.turn_count} 轮</div>
                  )}
                </button>
                {/* 正在跑的时候不给删：底下还挂着一条 WebSocket 和一个真在跑的 run */}
                <div className="flex shrink-0 gap-0.5 opacity-0 group-hover:opacity-100">
                  <button className="btn btn-xs btn-ghost" title="重命名"
                          onClick={() => setEditing(c.id)}>
                    <Pencil size={11} />
                  </button>
                  <button
                    className="btn btn-xs btn-ghost"
                    title={busy && c.id === currentId ? '正在运行，先停下来再删' : '删除'}
                    disabled={busy && c.id === currentId}
                    onClick={() => void drop(c.id)}
                  >
                    <Trash2 size={11} />
                  </button>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </aside>
  )
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
        className="field h-6 min-w-0 flex-1 px-1 text-[12px]"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') onSubmit(text)
          if (e.key === 'Escape') onCancel()
        }}
        autoFocus
      />
      <button className="btn btn-xs btn-ghost" onClick={() => onSubmit(text)}><Check size={11} /></button>
      <button className="btn btn-xs btn-ghost" onClick={onCancel}><X size={11} /></button>
    </div>
  )
}
