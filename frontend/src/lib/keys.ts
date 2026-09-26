/**
 * 快捷键：平台判断、显示格式、事件匹配。
 *
 * 提示文字和处理器必须出自同一份描述——之前界面上写着 ⌥V，全仓却没有一个
 * 监听处理 V；RunControl 的处理器接受 Ctrl+Enter，提示却只写 ⌘⏎，Windows
 * 用户照着按不出来。组合键一律写成 'Mod+Enter' 这样的串，显示走
 * formatShortcut，判定走 matchShortcut。
 *
 * Mod 在 Mac 上是 ⌘，其他平台是 Ctrl。
 */

export const isMac: boolean = (() => {
  if (typeof navigator === 'undefined') return false
  const nav = navigator as Navigator & { userAgentData?: { platform?: string } }
  const platform = nav.userAgentData?.platform || navigator.platform || navigator.userAgent
  return /mac|iphone|ipad|ipod/i.test(platform)
})()

/** 这个平台上"主修饰键"对应事件上的哪个字段：e[modKey] */
export const modKey: 'metaKey' | 'ctrlKey' = isMac ? 'metaKey' : 'ctrlKey'

/** 主修饰键的显示名 */
export const modLabel: string = isMac ? '⌘' : 'Ctrl'

const MAC: Record<string, string> = {
  mod: '⌘', meta: '⌘', cmd: '⌘', ctrl: '⌃', alt: '⌥', option: '⌥', shift: '⇧',
  enter: '⏎', return: '⏎', backspace: '⌫', delete: '⌦', tab: '⇥', esc: 'Esc', escape: 'Esc',
  up: '↑', down: '↓', left: '←', right: '→', space: '空格',
}

const OTHER: Record<string, string> = {
  mod: 'Ctrl', meta: 'Win', cmd: 'Ctrl', ctrl: 'Ctrl', alt: 'Alt', option: 'Alt', shift: 'Shift',
  enter: 'Enter', return: 'Enter', backspace: 'Backspace', delete: 'Del', tab: 'Tab',
  esc: 'Esc', escape: 'Esc', up: '↑', down: '↓', left: '←', right: '→', space: '空格',
}

function split(combo: string): string[] {
  // 'Mod++' 这种以加号本身为键的写法：最后一段是空串时补回 '+'
  const parts = combo.split('+').map((p) => p.trim())
  if (parts.length > 1 && parts[parts.length - 1] === '') parts[parts.length - 1] = '+'
  return parts.filter(Boolean)
}

/** 组合键拆成一个个键帽的文字：'Mod+Shift+K' → ['⌘','⇧','K'] 或 ['Ctrl','Shift','K'] */
export function shortcutParts(combo: string): string[] {
  const table = isMac ? MAC : OTHER
  return split(combo).map((p) => table[p.toLowerCase()] ?? (p.length === 1 ? p.toUpperCase() : p))
}

/**
 * 给人看的组合键。Mac 上符号连写（⌘⏎、⌥V），其他平台用加号（Ctrl+Enter、Alt+V），
 * 这是两边各自系统菜单的写法。
 */
export function formatShortcut(combo: string): string {
  return shortcutParts(combo).join(isMac ? '' : '+')
}

/** aria-keyshortcuts 的键名：修饰键和功能键按 UI Events 的 key 值写，不是给人看的简写 */
const ARIA: Record<string, string> = {
  meta: 'Meta', cmd: 'Meta', ctrl: 'Control', alt: 'Alt', option: 'Alt', shift: 'Shift',
  enter: 'Enter', return: 'Enter', esc: 'Escape', escape: 'Escape', up: 'ArrowUp', down: 'ArrowDown',
  left: 'ArrowLeft', right: 'ArrowRight', space: 'Space', tab: 'Tab', backspace: 'Backspace', delete: 'Delete',
}

/**
 * 写进 aria-keyshortcuts 的串：'Mod+Enter' 在 Mac 上是 'Meta+Enter'，其他平台是
 * 'Control+Enter'。读屏照这个念，Windows 上念成 Meta 就是在教人按错键。
 */
export function ariaShortcut(combo: string): string {
  return split(combo).map((raw) => {
    const p = raw.toLowerCase()
    if (p === 'mod') return isMac ? 'Meta' : 'Control'
    return ARIA[p] ?? (raw.length === 1 ? raw.toUpperCase() : raw)
  }).join('+')
}

/**
 * 键盘事件是否就是这个组合键。
 *
 * 字母和数字按 e.code 比：Mac 上 Option+V 产生的 e.key 是「√」，按 e.key 比
 * ⌥V 永远按不中。修饰键要求完全一致，免得 ⌘⇧S 也触发 ⌘S。
 * Mod 在 Mac 上只认 ⌘：Mac 的 Ctrl+字母另有系统含义（比如 Ctrl+A 回行首）。
 */
export function matchShortcut(e: KeyboardEvent | { key: string; code?: string; metaKey: boolean; ctrlKey: boolean; altKey: boolean; shiftKey: boolean }, combo: string): boolean {
  const want = { meta: false, ctrl: false, alt: false, shift: false }
  let key = ''
  for (const raw of split(combo)) {
    const p = raw.toLowerCase()
    if (p === 'mod') want[isMac ? 'meta' : 'ctrl'] = true
    else if (p === 'meta' || p === 'cmd') want.meta = true
    else if (p === 'ctrl') want.ctrl = true
    else if (p === 'alt' || p === 'option') want.alt = true
    else if (p === 'shift') want.shift = true
    else key = p
  }
  if (e.metaKey !== want.meta || e.ctrlKey !== want.ctrl || e.altKey !== want.alt) return false
  // '?' 这类本身就要按 Shift 才打得出的键，不强求 shift 一致
  const shiftNeutral = key.length === 1 && !/[a-z0-9]/.test(key)
  if (!shiftNeutral && e.shiftKey !== want.shift) return false
  if (/^[a-z]$/.test(key)) return e.code === `Key${key.toUpperCase()}` || e.key.toLowerCase() === key
  if (/^[0-9]$/.test(key)) return e.code === `Digit${key}` || e.key === key
  const alias: Record<string, string> = {
    enter: 'Enter', return: 'Enter', esc: 'Escape', escape: 'Escape', up: 'ArrowUp',
    down: 'ArrowDown', left: 'ArrowLeft', right: 'ArrowRight', space: ' ', tab: 'Tab',
    backspace: 'Backspace', delete: 'Delete',
  }
  return e.key === (alias[key] ?? key)
}

/**
 * 焦点是不是在一个能打字的地方。全局快捷键遇到它要让路：用户在输入框里按
 * Esc 是给输入法的，按 ⌥V 是要打字符的。
 */
export function isTypingTarget(target: EventTarget | null = typeof document !== 'undefined' ? document.activeElement : null): boolean {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  const tag = target.tagName
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true
  if (tag !== 'INPUT') return false
  const type = (target as HTMLInputElement).type
  // 复选框、按钮之类的 input 不接收文字
  return !['checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'color', 'file'].includes(type)
}
