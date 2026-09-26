import { useEffect, useReducer } from 'react'

/**
 * 运行计时用的时钟。全站只有一个 interval，所有在跑的计时器共用同一拍。
 *
 * 各自 setInterval 的话，胶囊、卡片、时间轴三处的秒数会差出几十毫秒，同一次
 * 运行在屏幕上显示三个不同的"已用时间"；而且卡片一多就是几十个定时器。
 *
 * 节拍：有人在看运行中的计时时 100ms 一次（mm:ss.s 的最后一位要走起来）；
 * 系统关了动效、或页面不可见时退到 1s 一次；没有订阅者就停掉。页面重新可见
 * 时立刻补一拍，不让人看到一个停在几分钟前的数字。
 */

type Listener = () => void

const listeners = new Set<Listener>()
let now = Date.now()
let timer: ReturnType<typeof setInterval> | null = null
let period = 0
let watching = false

const reduceMotion = (): boolean =>
  typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches

const hidden = (): boolean =>
  typeof document !== 'undefined' && document.visibilityState === 'hidden'

function tick(): void {
  now = Date.now()
  listeners.forEach((l) => l())
}

function reschedule(): void {
  const want = listeners.size ? (reduceMotion() || hidden() ? 1000 : 100) : 0
  if (want === period) return
  if (timer) clearInterval(timer)
  timer = null
  period = want
  if (want) timer = setInterval(tick, want)
}

/** 可见性、动效偏好一变就换节拍。只挂一次，页面整个生命周期都在 */
function watchEnvironment(): void {
  if (watching || typeof document === 'undefined') return
  watching = true
  document.addEventListener('visibilitychange', () => {
    reschedule()
    if (!hidden() && listeners.size) tick()
  })
  if (typeof matchMedia === 'function') {
    matchMedia('(prefers-reduced-motion: reduce)').addEventListener('change', reschedule)
  }
}

/**
 * 当前时间（毫秒）。active 为 false 时不订阅节拍，只在组件因为别的原因重渲染
 * 时给出当时的时间——终态的数字是定值，用不着每 100ms 重画一遍。
 */
export function useRunClock(active: boolean): number {
  const [, bump] = useReducer((x: number) => x + 1, 0)

  useEffect(() => {
    if (!active) return
    watchEnvironment()
    listeners.add(bump)
    reschedule()
    // 停了一阵的时钟 now 是旧的，接上时立刻补一拍
    tick()
    return () => {
      listeners.delete(bump)
      reschedule()
    }
  }, [active])

  // 没有节拍在走时 now 可能是很久以前的值，直接取当前时间
  if (!timer) now = Date.now()
  return active ? now : Date.now()
}
