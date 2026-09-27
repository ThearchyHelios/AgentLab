/**
 * 离开前确认：页面有没保存的改动时登记一道守卫，站内的每一次换页都先经过它——
 * 左侧导航、页面里的链接、⌘K、⌥ 数字、浏览器前进后退、代码里的 navigate()——
 * 关页和刷新也会让浏览器问一句。
 *
 * 以前设置页自己在捕获阶段拦 <a> 的点击：⌘K 和快捷键直接调 navigate()，浏览器
 * 后退也不走 <a>，改到一半的表单照样丢。路由换成 data router 之后，react-router 的
 * useBlocker 拦得住所有换页；但一个 router 同时只认一个 blocker，所以由外壳挂唯一
 * 那一个（useLeaveBlocker），页面只往这里登记，别自己调 useBlocker。
 *
 *   useLeaveGuard(dirty > 0, () => confirmDialog({ title: `有 ${dirty} 项设置还没保存`, … }))
 */
import { useEffect, useRef } from 'react'
import { NavigationType, parsePath, useBlocker } from 'react-router-dom'
import type { BlockerFunction, NavigateOptions, Path } from 'react-router-dom'

export interface LeaveGuard {
  /**
   * 这一次换页要不要拦。next 为 null 表示去向还没定（canLeave() 不带目标时）。
   * 默认：路径变了就拦；只改 ?query 或 #hash 不算，同一页里换个筛选不会丢表单
   */
  blocks?: (next: Path | null, current: Path) => boolean
  /** 拦下之后怎么问：true 放行，false 留下。抛错按留下算 */
  confirm: (next: Path | null) => Promise<boolean>
  /** 关页、刷新时要不要让浏览器问一句，默认要。浏览器只给通用文案，写不了自己的话 */
  unload?: boolean
}

const guards = new Set<LeaveGuard>()

const pathChanged = (next: Path | null, current: Path) => next == null || next.pathname !== current.pathname

function blocking(next: Path | null, current: Path): LeaveGuard[] {
  return [...guards].filter((g) => {
    try {
      return (g.blocks ?? pathChanged)(next, current)
    } catch {
      return true
    }
  })
}

const onBeforeUnload = (e: BeforeUnloadEvent) => {
  e.preventDefault()
  e.returnValue = ''
}
let unloadOn = false

/** 只在有守卫时挂 beforeunload：常驻的监听会让浏览器的往返缓存失效 */
function syncUnload() {
  const want = [...guards].some((g) => g.unload !== false)
  if (want === unloadOn || typeof window === 'undefined') return
  unloadOn = want
  if (want) window.addEventListener('beforeunload', onBeforeUnload)
  else window.removeEventListener('beforeunload', onBeforeUnload)
}

/** 登记一道守卫，返回撤销函数。组件里用 useLeaveGuard，它管登记和撤销的时机 */
export function registerLeaveGuard(guard: LeaveGuard): () => void {
  guards.add(guard)
  syncUnload()
  return () => {
    guards.delete(guard)
    syncUnload()
  }
}

/** 一道一道地问：同时弹两个确认框，人不知道先答哪个。有一道说留下就留下 */
async function ask(list: LeaveGuard[], next: Path | null): Promise<boolean> {
  for (const g of list) {
    let ok = false
    try {
      ok = await g.confirm(next)
    } catch {
      ok = false
    }
    if (!ok) return false
  }
  return true
}

const here = (): Path => ({ pathname: location.pathname, search: location.search, hash: location.hash })

/**
 * 有没保存的改动时拦住离开。active 为 false 时什么都不登记。
 * confirm、blocks 每次渲染取最新的，不必包 useCallback。
 */
export function useLeaveGuard(
  active: boolean,
  confirm: (next: Path | null) => Promise<boolean>,
  opts?: Pick<LeaveGuard, 'blocks' | 'unload'>,
): void {
  const latest = useRef({ confirm, blocks: opts?.blocks })
  latest.current = { confirm, blocks: opts?.blocks }
  const unload = opts?.unload
  useEffect(() => {
    if (!active) return
    return registerLeaveGuard({
      confirm: (next) => latest.current.confirm(next),
      blocks: (next, current) => (latest.current.blocks ?? pathChanged)(next, current),
      unload,
    })
  }, [active, unload])
}

/**
 * 先问、再做。「新对话」「新建工作流」要先建出东西、建完才跳：等到跳的时候才问，
 * 人点了「留下」，东西已经建出来了。所以先问一遍；问过之后的那次跳转带上
 * leavePass()，不再问第二遍。没有拦着的守卫就直接是 true。
 * to 省略表示去向还没定：只要有守卫在就问。
 */
export function canLeave(to?: string): Promise<boolean> {
  const next = to == null ? null : { pathname: '/', search: '', hash: '', ...parsePath(to) }
  const list = blocking(next, here())
  return list.length ? ask(list, next) : Promise.resolve(true)
}

const PASS = '__leaveConfirmed'

/** 给 canLeave() 问过的那一次跳转放行：navigate(to, leavePass(opts)) */
export function leavePass<T extends NavigateOptions>(opts?: T): T {
  const state = opts?.state && typeof opts.state === 'object' ? opts.state : {}
  return { ...opts, state: { ...state, [PASS]: true } } as T
}

/** 拦下时那几道守卫：问的时候就问它们，不再重新挑（后退时地址已经变过一次了） */
let held: LeaveGuard[] = []

const shouldBlock: BlockerFunction = ({ currentLocation, nextLocation, historyAction }) => {
  // 后退前进不认通行证：它存在历史记录的 state 里，退回那一条时还在
  if (historyAction !== NavigationType.Pop && (nextLocation.state as Record<string, unknown> | null)?.[PASS]) return false
  held = blocking(nextLocation, currentLocation)
  return held.length > 0
}

/**
 * 外壳挂一次（App 里）。一个 router 同时只认一个 blocker：页面别自己调 useBlocker，
 * 用 useLeaveGuard 登记。
 */
export function useLeaveBlocker(): void {
  const blocker = useBlocker(shouldBlock)
  const latest = useRef(blocker)
  latest.current = blocker
  // 正在问的是哪一次：StrictMode 下 effect 会跑两遍，同一次别问两回
  const asking = useRef<string | null>(null)
  useEffect(() => {
    if (blocker.state !== 'blocked') {
      asking.current = null
      return
    }
    const key = blocker.location.key
    if (asking.current === key) return
    asking.current = key
    void ask(held, blocker.location).then((ok) => {
      const b = latest.current
      // 问的时候人又点了别处（比如又按了后退），这一问就作废，归新的那次
      if (b.state !== 'blocked' || b.location.key !== key) return
      if (ok) b.proceed()
      else b.reset()
    })
  }, [blocker])
}

// 开发期挂到 window 上，好让 check-shell 登记一道测试守卫，看各条换页的路都拦得住。
// 生产构建里去掉
if (import.meta.env.DEV && typeof window !== 'undefined') {
  ;(window as any).__leave = { register: registerLeaveGuard, count: () => guards.size }
}
