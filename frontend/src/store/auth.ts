import { create } from 'zustand'
import { ApiError, api } from '../api/client'
import type { AuthMode, Principal, Role } from '../typesApp'

/**
 * 当前的人（GET /api/auth/me）：登录方式、用户名、角色、组。外壳按它收起导航、换掉署名入口，待办卡按它写「以谁的
 * 名义处理」，表单的 JSON 控件按它决定给不给填。
 *
 * 关登录（off，默认）时 /me 恒为 mode='off'、role='admin'：所有人等同管理员，和加这一层之前的界面一模一样。
 * 服务端没有这个接口（老服务端回 404 / 405）也按 off 算：新前端连老服务端，不能因为一个身份接口把整站拦在门外。
 * 网络失败不下结论（me 留空），外壳照旧走离线那一套，恢复后再取。
 */
export const OFF_PRINCIPAL: Principal = {
  mode: 'off', username: null, display_name: null, role: 'admin', groups: [], verified: false,
}

interface AuthState {
  me: Principal | null
  /** 取过一次（成功或按 off 兜底）。网络失败时保持 false */
  ready: boolean
  /** 取 /me；返回取到的身份，网络失败返回 null */
  load: () => Promise<Principal | null>
  set: (me: Principal) => void
  logout: () => Promise<void>
}

export const useAuth = create<AuthState>((set) => ({
  me: null,
  ready: false,
  load: async () => {
    try {
      const me = await api.auth.me({ timeoutMs: 15_000 })
      set({ me, ready: true })
      return me
    } catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.status === 405) && !e.code) {
        set({ me: OFF_PRINCIPAL, ready: true })
        return OFF_PRINCIPAL
      }
      // 开了登录、还没登录：服务端回 401 并说明登录方式（登录页据此给账号密码表单还是单点登录按钮）
      if (e instanceof ApiError && e.status === 401) {
        const raw = (e.body as { mode?: unknown } | undefined)?.mode
        const mode: AuthMode = raw === 'oidc' ? 'oidc' : 'local'
        const me: Principal = { ...OFF_PRINCIPAL, mode, role: 'user', verified: false }
        set({ me, ready: true })
        return me
      }
      return null
    }
  },
  set: (me) => set({ me, ready: true }),
  logout: async () => {
    await api.auth.logout()
    set({ me: null, ready: false })
  },
}))

const RANK: Record<Role, number> = { user: 1, builder: 2, admin: 3 }

/** 开没开登录：明确是 local / oidc 才算；off 和没取到都算没开（界面照今天的样子） */
export const loginOn = (me: Principal | null | undefined): boolean => me?.mode === 'local' || me?.mode === 'oidc'

/** 至少是这个角色。关登录时所有人等同管理员 */
export function atLeast(me: Principal | null | undefined, role: Role): boolean {
  if (!loginOn(me)) return true
  return RANK[me!.role] >= RANK[role]
}

/** 开了登录、还没登录（/me 回了未认证的身份）：要去登录页 */
export const needsLogin = (me: Principal | null | undefined): boolean => loginOn(me) && !me!.verified

/** 订阅版 */
export const usePrincipal = (): Principal | null => useAuth((s) => s.me)
export const useLoginOn = (): boolean => useAuth((s) => loginOn(s.me))
export const useAtLeast = (role: Role): boolean => useAuth((s) => atLeast(s.me, role))

/** 给人看的名字：显示名（用户名）；关登录时是本机署名，由调用方自己取 */
export function principalName(me: Principal | null | undefined): string {
  if (!me) return ''
  const name = me.display_name?.trim()
  const user = me.username?.trim() ?? ''
  return name && user && name !== user ? `${name}（${user}）` : name || user
}

/** 登录页的地址，带上回来的路：/login?next=/apps/x */
export function loginUrl(next?: string): string {
  const back = next ?? `${window.location.pathname}${window.location.search}`
  return back && back !== '/' && !back.startsWith('/login') ? `/login?next=${encodeURIComponent(back)}` : '/login'
}

/**
 * 等身份认定（/auth/me 回来，成功或按 off 兜底）。启动时设置、主题这类请求和认人同时发出，回来时要先知道是谁才能决定
 * 用不用它的结果。最多等 timeoutMs，到点还没认定按没认定（null）交回
 */
export function authSettled(timeoutMs = 15_000): Promise<Principal | null> {
  const now = useAuth.getState()
  if (now.ready) return Promise.resolve(now.me)
  return new Promise((resolve) => {
    const stop = useAuth.subscribe((s) => {
      if (!s.ready) return
      stop()
      clearTimeout(timer)
      resolve(s.me)
    })
    const timer = setTimeout(() => { stop(); resolve(null) }, timeoutMs)
  })
}

/**
 * 界面偏好（主题）只存本机、不写服务端（D-s）：开了登录、又不是管理员时。服务端设置里的 ui 一组是全站共用的，改它要
 * 管理员；搭建者、使用者换主题只在这台设备上生效，也不拿服务端的值翻回去
 */
export const prefsLocalOnly = (me: Principal | null | undefined): boolean => loginOn(me) && !atLeast(me, 'admin')
