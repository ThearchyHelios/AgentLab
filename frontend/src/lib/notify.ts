/**
 * 标签页在后台时的三路提醒：标题前缀、favicon 状态点、系统通知。
 *
 * 多 Agent 协作一跑就是一两分钟，审批更是要等人——用户切去别的标签页是常态。
 * 之前标题写死、favicon 写死、也没有通知，一个待签批能被晾上几天没人发现，
 * 只能不停切回来看。
 *
 * 这里只管浏览器那一层（document.title、<link rel=icon>、Notification），
 * 不读任何 store：什么时候该提醒，由外壳（App）从运行态和待审批推出来。
 * 这样页面也能用 usePageTitle 报自己的名字，不必知道前缀是怎么拼的。
 */
import { useEffect } from 'react'
import { create } from 'zustand'

export type NotifyPermission = 'granted' | 'denied' | 'default' | 'unsupported'

/** 回来之前一直挂在标题和 favicon 上的那件事：后台时结束的运行 */
export interface Attention {
  tone: 'ok' | 'err' | 'warn'
  /** 标题前缀，比如「✕ 运行失败」 */
  text: string
}

interface SignalState {
  /** 用户开没开后台提醒（且浏览器已授权） */
  notify: boolean
  permission: NotifyPermission
  hidden: boolean
  attention: Attention | null
  /** 页面自己报的标题，盖过外壳按路径推的那个。404 页用它 */
  pageTitle: string | null
}

const PREF_KEY = 'agentlab.notify'

export const notifySupported = (): boolean =>
  typeof window !== 'undefined' && 'Notification' in window

function readPermission(): NotifyPermission {
  return notifySupported() ? Notification.permission : 'unsupported'
}

function readPref(): boolean {
  try {
    return localStorage.getItem(PREF_KEY) === '1'
  } catch {
    return false
  }
}

function writePref(on: boolean) {
  try {
    localStorage.setItem(PREF_KEY, on ? '1' : '0')
  } catch {
    // 隐私模式：这次会话里照样生效，只是下次打开要重新开
  }
}

export const useSignals = create<SignalState>(() => ({
  notify: readPref() && readPermission() === 'granted',
  permission: readPermission(),
  hidden: typeof document !== 'undefined' ? document.hidden : false,
  attention: null,
  pageTitle: null,
}))

if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    const hidden = document.hidden
    // 人回来了，「在你离开时结束了」这件事就算传达到了。权限也可能是在浏览器
    // 设置里改的，回来时顺手对一次
    const permission = readPermission()
    useSignals.setState((s) => ({
      hidden,
      permission,
      notify: s.notify && permission === 'granted',
      ...(hidden ? {} : { attention: null }),
    }))
  })
}

/**
 * 打开后台提醒。只在用户点开关的那一刻调用：浏览器的权限弹窗是一次性的，
 * 进页面就弹，多数人会顺手点「拒绝」，之后就再也要不回来了。
 */
export async function enableNotify(): Promise<NotifyPermission> {
  if (!notifySupported()) return 'unsupported'
  let permission = Notification.permission as NotifyPermission
  if (permission === 'default') {
    try {
      permission = (await Notification.requestPermission()) as NotifyPermission
    } catch {
      permission = Notification.permission as NotifyPermission
    }
  }
  const on = permission === 'granted'
  writePref(on)
  useSignals.setState({ permission, notify: on })
  return permission
}

export function disableNotify(): void {
  writePref(false)
  useSignals.setState({ notify: false })
}

/**
 * 页面在后台时发一条系统通知；页面在前台、没开提醒、没授权都不发。
 * 点通知回到这个标签页并执行 onClick（通常是跳到对应的运行）。
 * tag 相同的通知互相替换，同一条运行不会叠出一串。
 */
export function notifyInBackground(opts: { title: string; body?: string; tag?: string; onClick?: () => void }): boolean {
  if (typeof document === 'undefined' || !document.hidden) return false
  if (!useSignals.getState().notify || readPermission() !== 'granted') return false
  try {
    const n = new Notification(opts.title, { body: opts.body, tag: opts.tag, icon: '/favicon.svg', lang: 'zh-CN' })
    n.onclick = () => {
      window.focus()
      opts.onClick?.()
      n.close()
    }
    return true
  } catch {
    // 安卓 Chrome 之类只允许经 Service Worker 发通知，直接 new 会抛
    return false
  }
}

/** 后台时结束的运行：挂到标题和 favicon 上，直到人切回这个标签页 */
export function flagAttention(attention: Attention): void {
  if (typeof document === 'undefined' || !document.hidden) return
  useSignals.setState({ attention })
}

/**
 * 页面报自己的标题（「页面不存在」这类外壳按路径推不出来的）。卸载时撤掉。
 * 前缀（待审批、运行中）照样由外壳加。
 */
export function usePageTitle(name: string | null): void {
  useEffect(() => {
    useSignals.setState({ pageTitle: name })
    return () => {
      if (useSignals.getState().pageTitle === name) useSignals.setState({ pageTitle: null })
    }
  }, [name])
}

/**
 * 标签页标题：「(2) 待审批 · ● 运行中 3/8 · 编排 · 多 Agent 协作 — AgentLab」。
 *
 * 待审批数放最前：它是要人去做的事，标签页窄到只剩几个字时也得看得见。
 * 后台时结束的运行（attention）盖过「运行中」——它已经不在跑了。
 */
export function composeTitle(p: {
  pending: number
  activity?: string | null
  attention?: Attention | null
  page: string
  object?: string | null
}): string {
  const lead: string[] = []
  if (p.pending > 0) lead.push(`(${p.pending}) 待审批`)
  const state = p.attention?.text ?? p.activity
  if (state) lead.push(state)
  const base = p.object ? `${p.page} · ${p.object}` : p.page
  return `${[...lead, base].join(' · ')} — AgentLab`
}

// -------------------------------------------------------------------------
// favicon 状态点
// -------------------------------------------------------------------------

export type FaviconDot = 'running' | 'waiting' | 'failed' | 'done'

/**
 * 标签栏跟着系统配色走，不跟页面主题，所以这里按系统深浅挑色，而不是读页面
 * 的 CSS 变量。取值与 public/favicon.svg、index.css 的状态色一致。
 */
const FAVICON = {
  light: {
    tile: '#eef3fe', tileStroke: '#c3d3f3', mark: '#2563eb',
    dot: { running: '#2563eb', waiting: '#d29922', failed: '#cf222e', done: '#1f883d' },
  },
  dark: {
    tile: '#141926', tileStroke: '#333c52', mark: '#4f8cff',
    dot: { running: '#4f8cff', waiting: '#d29922', failed: '#f85149', done: '#3fb950' },
  },
} as const

/** 与 components/Logo.tsx、public/favicon.svg 同一个图形 */
const EDGES = 'M8.5 12H12V6h3.5M12 12v6h3.5'

let originalHref: string | null = null
let painted: string | null = null

function iconLink(): HTMLLinkElement | null {
  return document.querySelector<HTMLLinkElement>('link[rel~="icon"]')
}

function systemScheme(): 'light' | 'dark' {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath()
  ctx.moveTo(x + r, y)
  ctx.arcTo(x + w, y, x + w, y + h, r)
  ctx.arcTo(x + w, y + h, x, y + h, r)
  ctx.arcTo(x, y + h, x, y, r)
  ctx.arcTo(x, y, x + w, y, r)
  ctx.closePath()
}

function draw(dot: FaviconDot, scheme: 'light' | 'dark'): string | null {
  const c = FAVICON[scheme]
  const canvas = document.createElement('canvas')
  canvas.width = 64
  canvas.height = 64
  const ctx = canvas.getContext('2d')
  if (!ctx) return null
  ctx.scale(2, 2)
  roundRect(ctx, 0.5, 0.5, 31, 31, 7)
  ctx.fillStyle = c.tile
  ctx.fill()
  ctx.strokeStyle = c.tileStroke
  ctx.lineWidth = 1
  ctx.stroke()

  ctx.save()
  ctx.translate(4, 4)
  ctx.strokeStyle = c.mark
  ctx.lineWidth = 2.25
  ctx.lineCap = 'round'
  ctx.lineJoin = 'round'
  ctx.stroke(new Path2D(EDGES))
  roundRect(ctx, 2.5, 9, 6, 6, 1.5)
  ctx.fillStyle = c.mark
  ctx.fill()
  ctx.stroke()
  roundRect(ctx, 15.5, 3, 6, 6, 1.5)
  ctx.stroke()
  roundRect(ctx, 15.5, 15, 6, 6, 1.5)
  ctx.stroke()
  ctx.restore()

  // 右下角的状态点，外面一圈底板色把它从字标上"抠"出来，16px 下也分得清
  ctx.beginPath()
  ctx.arc(24.5, 24.5, 7.5, 0, Math.PI * 2)
  ctx.fillStyle = c.tile
  ctx.fill()
  ctx.beginPath()
  ctx.arc(24.5, 24.5, 5.5, 0, Math.PI * 2)
  ctx.fillStyle = c.dot[dot]
  ctx.fill()
  return canvas.toDataURL('image/png')
}

/**
 * 换 favicon：有状态就画一个带状态点的，没有就还原成 /favicon.svg。
 * 静态，不闪不转：标签页里闪烁的图标只会让人烦，而且减少动效的用户也看得到。
 */
export function paintFavicon(dot: FaviconDot | null): void {
  if (typeof document === 'undefined') return
  const link = iconLink()
  if (!link) return
  if (originalHref == null) originalHref = link.getAttribute('href') ?? '/favicon.svg'
  const key = dot ? `${dot}:${systemScheme()}` : null
  if (key === painted) return
  painted = key
  if (!dot) {
    link.type = 'image/svg+xml'
    link.href = originalHref
    link.removeAttribute('data-status')
    return
  }
  const url = draw(dot, systemScheme())
  if (!url) return
  link.type = 'image/png'
  link.href = url
  link.setAttribute('data-status', dot)
}

if (typeof window !== 'undefined' && window.matchMedia) {
  // 系统换了深浅，底板色要跟着换；只在有状态点时才需要重画
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', () => {
    const dot = painted?.split(':')[0] as FaviconDot | undefined
    painted = null
    if (dot) paintFavicon(dot)
  })
}
