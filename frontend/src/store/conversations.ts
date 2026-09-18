import { create } from 'zustand'
import { api } from '../api/client'
import type { Conversation } from '../types'

/**
 * 会话列表的状态。
 *
 * 只管"有哪些对话、当前在看哪个"。对话里的内容归 chat store —— 两者分开是
 * 因为列表要常驻（切换时不能闪），而内容是按会话缓存的一大坨东西。
 *
 * 当前会话 id 存进 localStorage：刷新后回到刚才那个对话，而不是甩回空白页。
 * 这是"对话持久化"里用户最先感知到的一半——另一半（内容本身）在 chat store。
 */

const LAST_KEY = 'agentlab.lastConversation'

interface ConversationState {
  list: Conversation[]
  currentId: string | null
  loading: boolean

  load: () => Promise<void>
  create: () => Promise<string>
  select: (id: string | null) => void
  rename: (id: string, title: string) => Promise<void>
  remove: (id: string) => Promise<void>
  /** 有人在这个会话里说话了，把它顶到列表最前并刷新副标题 */
  touch: (id: string, lastQuestion?: string) => void
}

export const useConversations = create<ConversationState>((set, get) => ({
  list: [],
  currentId: localStorage.getItem(LAST_KEY),
  loading: false,

  load: async () => {
    set({ loading: true })
    try {
      const list = await api.conversations.list('chat')
      // 记着的那个可能已经被删了（另一个标签页里删的）。指着一个不存在的
      // 会话会让页面停在空白，这时候退回最近一条，而不是让人对着空屏幕
      const current = get().currentId
      const alive = current && list.some((c) => c.id === current)
      const next = alive ? current : (list[0]?.id ?? null)
      if (next !== current) {
        if (next) localStorage.setItem(LAST_KEY, next)
        else localStorage.removeItem(LAST_KEY)
      }
      set({ list, currentId: next, loading: false })
    } catch {
      set({ loading: false })
    }
  },

  create: async () => {
    // 已经有一条空会话就直接用它。连点几次"新对话"不该留下一串一模一样的
    // 空壳——用户的意思是"我要从头开始说"，不是"我要三个空对话"
    const blank = get().list.find((c) => c.turn_count === 0)
    if (blank) {
      get().select(blank.id)
      return blank.id
    }
    const created = await api.conversations.create({ kind: 'chat' })
    localStorage.setItem(LAST_KEY, created.id)
    set((s) => ({ list: [created, ...s.list], currentId: created.id }))
    return created.id
  },

  select: (id) => {
    if (id) localStorage.setItem(LAST_KEY, id)
    else localStorage.removeItem(LAST_KEY)
    set({ currentId: id })
  },

  rename: async (id, title) => {
    const clean = title.trim()
    if (!clean) return
    // 先改本地再发请求：改名是个不该等一次往返的动作
    set((s) => ({ list: s.list.map((c) => (c.id === id ? { ...c, title: clean } : c)) }))
    try {
      await api.conversations.update(id, { title: clean })
    } catch {
      void get().load()   // 没改成就回源，别让界面上留着一个假名字
    }
  },

  remove: async (id) => {
    await api.conversations.remove(id)
    set((s) => {
      const list = s.list.filter((c) => c.id !== id)
      const currentId = s.currentId === id ? (list[0]?.id ?? null) : s.currentId
      if (currentId) localStorage.setItem(LAST_KEY, currentId)
      else localStorage.removeItem(LAST_KEY)
      return { list, currentId }
    })
  },

  touch: (id, lastQuestion) => {
    set((s) => {
      const hit = s.list.find((c) => c.id === id)
      if (!hit) return s
      const updated: Conversation = {
        ...hit,
        last_question: lastQuestion ?? hit.last_question,
        // 第一句话会成为标题，本地先顶上，省得列表里那条还写着"新对话"
        title: hit.title === '新对话' && lastQuestion
          ? (lastQuestion.length > 24 ? lastQuestion.slice(0, 24) + '…' : lastQuestion)
          : hit.title,
        turn_count: hit.turn_count + (lastQuestion ? 1 : 0),
      }
      return { list: [updated, ...s.list.filter((c) => c.id !== id)] }
    })
  },
}))
