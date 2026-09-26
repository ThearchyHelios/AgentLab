import { create } from 'zustand'
import { api } from '../api/client'
import type { Conversation } from '../types'

/**
 * 会话列表的状态。
 *
 * 只管"有哪些对话、当前在看哪个"。对话里的内容归 chat store —— 两者分开是
 * 因为列表要常驻（切换时不能闪），而内容是按会话缓存的一大坨东西。
 *
 * **当前在看哪个由 URL 说了算**（/chat/:conversationId），这里的 currentId 只是
 * 它的派生缓存——十几处在读这个字段，但写它的只有 ChatPage 的那个同步 effect。
 * 两边都能写就一定会打架：点一下列表，store 改了、地址栏没改，刷新又回到旧的。
 *
 * localStorage 留着，但它回答的是另一个问题：**打开不带 id 的 /chat 该去哪**。
 * URL 里有 id 时一律以 URL 为准。
 */

const LAST_KEY = 'agentlab.lastConversation'

/** 上次看的是哪个。只在 /chat 不带 id 时用来决定落到哪 */
export const lastVisited = (): string | null => {
  try {
    return localStorage.getItem(LAST_KEY)
  } catch {
    return null   // 隐私模式下 localStorage 会抛
  }
}

interface ConversationState {
  list: Conversation[]
  currentId: string | null
  loading: boolean
  /** 列表取失败了。和「一个对话都没有」要分开说 */
  error: unknown

  load: () => Promise<void>
  /** 新建（或复用一条空的），返回它的 id。**不负责切过去**——那是导航的事 */
  create: () => Promise<string>
  /** 由 URL 回流调用，把「在看哪个」记下来。不要在别处直接调它 */
  select: (id: string | null) => void
  rename: (id: string, title: string) => Promise<void>
  /** 删掉，返回接下来该去哪个（列表空了就是 null），由调用方导航 */
  remove: (id: string) => Promise<string | null>
  /**
   * 从列表里拿掉（归档），返回接下来该去哪个。撤销就是 restore。
   *
   * 用归档而不是「先藏起来、过几秒再真删」：延迟删除在关掉标签页、切走页面时
   * 要么没删成、要么撤销不了。归档是一次就落库的状态，撤不撤都不会丢
   */
  archive: (id: string) => Promise<string | null>
  /** 撤销归档，放回原来的位置 */
  restore: (item: Conversation, index: number) => Promise<void>
  /** 有人在这个会话里说话了，把它顶到列表最前并刷新副标题 */
  touch: (id: string, lastQuestion?: string) => void
}

export const useConversations = create<ConversationState>((set, get) => ({
  list: [],
  currentId: null,   // 由 URL 填，见 ChatPage 的同步 effect
  loading: false,
  error: null,

  load: async () => {
    // 只管把列表取回来。「当前在看哪个」以前也在这里决定，现在归 URL 管——
    // 两边都能定就会互相覆盖：深链接进来，列表一加载完又被拽回上次那个
    set({ loading: true })
    try {
      set({ list: await api.conversations.list('chat'), loading: false, error: null })
    } catch (e) {
      set({ loading: false, error: e })
    }
  },

  create: async () => {
    // 已经有一条空会话就直接用它。连点几次"新对话"不该留下一串一模一样的
    // 空壳——用户的意思是"我要从头开始说"，不是"我要三个空对话"
    const blank = get().list.find((c) => c.turn_count === 0)
    if (blank) return blank.id
    const created = await api.conversations.create({ kind: 'chat' })
    set((s) => ({ list: [created, ...s.list] }))
    return created.id
  },

  select: (id) => {
    try {
      if (id) localStorage.setItem(LAST_KEY, id)
      else localStorage.removeItem(LAST_KEY)
    } catch { /* 隐私模式，记不住就算了，不影响这次 */ }
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
    const list = get().list.filter((c) => c.id !== id)
    set({ list })
    // 删的不是当前这个就原地不动；删的是当前这个才需要换地方
    return get().currentId === id ? (list[0]?.id ?? null) : get().currentId
  },

  archive: async (id) => {
    const before = get().list
    const list = before.filter((c) => c.id !== id)
    // 先从列表里拿掉再发请求：删除该是即时的，失败了再放回去
    set({ list })
    try {
      await api.conversations.update(id, { archived: true })
    } catch (e) {
      set({ list: before })
      throw e
    }
    return get().currentId === id ? (list[0]?.id ?? null) : get().currentId
  },

  restore: async (item, index) => {
    await api.conversations.update(item.id, { archived: false })
    set((s) => {
      if (s.list.some((c) => c.id === item.id)) return s
      const list = [...s.list]
      list.splice(Math.min(index, list.length), 0, { ...item, archived: false })
      return { list }
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
        last_active_at: new Date().toISOString(),
      }
      return { list: [updated, ...s.list.filter((c) => c.id !== id)] }
    })
  },
}))
