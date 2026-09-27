/**
 * 本机署名：发布、审批、放弃运行、正式运行都记在这个名下。
 *
 * 署名只存在这台浏览器的 localStorage 里，请求头 X-Actor 读的也是它。之前
 * App、RunPanel、RunDetailView、PublishDialog、client 各读一份，有的 trim 有的
 * 不 trim，有的写完不通知——在发布弹窗里就地署名后，导航底部的首字要等下一次
 * 获得焦点才跟上。读写都走这里。
 */
import { useSyncExternalStore } from 'react'

export const ACTOR_KEY = 'agentlab_actor'

/** 同一个标签页里写 localStorage 不触发 storage 事件，写完派发这个 */
export const ACTOR_EVENT = 'agentlab:actor'

/** 读署名；没填、只填了空白、或者读不到（隐私模式、被禁用）都返回 null */
export function localActor(): string | null {
  try {
    const v = localStorage.getItem(ACTOR_KEY)?.trim()
    return v ? v : null
  } catch {
    return null
  }
}

/**
 * 写署名；空串或 null 表示清掉。写不进去（隐私模式）时静默失败，返回 false：
 * 调用方可以决定这一次会话里先用内存里的值。
 */
export function setLocalActor(name: string | null): boolean {
  const v = name?.trim() ?? ''
  let ok = true
  try {
    if (v) localStorage.setItem(ACTOR_KEY, v)
    else localStorage.removeItem(ACTOR_KEY)
  } catch {
    ok = false
  }
  window.dispatchEvent(new Event(ACTOR_EVENT))
  return ok
}

/** 署名变化的订阅：别的标签页（storage）、本标签页（ACTOR_EVENT）、切回来时（focus） */
export function subscribeActor(cb: () => void): () => void {
  const events = ['storage', ACTOR_EVENT, 'focus']
  events.forEach((e) => window.addEventListener(e, cb))
  return () => events.forEach((e) => window.removeEventListener(e, cb))
}

/** 组件里读署名并跟着变 */
export function useLocalActor(): string | null {
  return useSyncExternalStore(subscribeActor, localActor, () => null)
}
