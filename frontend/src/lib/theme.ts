/**
 * 主题偏好。
 *
 * 真源是后端 settings 的 ui.theme；localStorage 只是它的首帧缓存。index.html
 * 里那段内联脚本要在 React 起来之前就决定画深色还是浅色——等 /api/settings
 * 回来再切，浅色用户每次打开都会先看到一整帧深色。所以每次应用主题都顺手写回
 * 缓存，键名和取值必须和 index.html 那段脚本一致。
 *
 * 「跟随系统」就是不写 data-theme：index.css 的 prefers-color-scheme 分支接管，
 * 系统切换时页面跟着变，不需要这边监听 matchMedia。
 */

export type ThemePref = 'system' | 'light' | 'dark'

export const THEME_STORAGE_KEY = 'agentlab.theme'

/** 后端和缓存里的值都可能是旧的或手改坏的，认不出来的一律当「跟随系统」 */
export function normalizeTheme(value: unknown): ThemePref {
  return value === 'light' || value === 'dark' ? value : 'system'
}

export function readThemePref(): ThemePref {
  try {
    return normalizeTheme(localStorage.getItem(THEME_STORAGE_KEY))
  } catch {
    return 'system'
  }
}

export function applyTheme(pref: ThemePref): void {
  const root = document.documentElement
  // 切换这一帧关掉过渡：带 transition 的按钮、输入框会各自花 120ms 慢慢变色，
  // 整页看着像在闪。先让新配色在无过渡的状态下落定（读一次样式逼它重算），
  // 下一帧再把过渡还回去
  root.classList.add('theme-switching')
  if (pref === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', pref)
  void getComputedStyle(root).backgroundColor
  requestAnimationFrame(() => root.classList.remove('theme-switching'))
  try {
    localStorage.setItem(THEME_STORAGE_KEY, pref)
  } catch {
    // 隐私模式下写不进去：只是下次首帧跟随系统，等设置接口回来再纠正
  }
}

/** 眼下实际画的是哪一套。「跟随系统」时要问系统，给主题切换按钮挑图标用 */
export function resolvedTheme(): 'light' | 'dark' {
  const attr = document.documentElement.getAttribute('data-theme')
  if (attr === 'light' || attr === 'dark') return attr
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
}
