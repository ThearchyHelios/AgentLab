/**
 * 登录页（/login，不在外壳里）：开了登录时，账号密码表单或单点登录按钮——登录方式看 /auth/me 的 401 里说的。
 *
 * 关登录时没有登录这回事，直接回首页；已经登录了回到原处（next）。登录成功后整页跳回原处：外壳的各个 store 按这个人
 * 重新加载。next 只认站内路径（以 / 开头、不是 //），不做开放跳转。单点登录回调失败时服务端把原因放在 error 里带回来。
 * 单点登录模式下本地账号仍可登录（找回用的管理员账号），收在下面。
 *
 * 账号密码登录的新部署（环境变量直接指定了 local、库里还没有能登录的管理员：/auth/me 的 401 带 can_bootstrap）：先给
 * 「建第一个管理员」的表单，建好即以他登录、回到原处；已有别的账号的人可以展开登录表单。
 */
import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { Navigate, useSearchParams } from 'react-router-dom'
import { KeyRound, LogIn, UserPlus } from 'lucide-react'
import { ApiError, api } from '../api/client'
import { Logo } from '../components/Logo'
import { Notice, Spinner } from '../components/ui'
import { useAuth } from '../store/auth'
import { AUTH_MODE_LABEL, AUTH_TEXT } from '../lib/terms'

/** 站内路径才跳：/apps/x?tab=… 可以，//evil.example 和 https://… 不行 */
function safeNext(raw: string | null): string {
  return raw && raw.startsWith('/') && !raw.startsWith('//') && !raw.startsWith('/login') ? raw : '/'
}

export function LoginPage() {
  const [search] = useSearchParams()
  const next = safeNext(search.get('next'))
  const me = useAuth((s) => s.me)
  const ready = useAuth((s) => s.ready)
  const canBootstrap = useAuth((s) => s.canBootstrap)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(search.get('error'))
  const [local, setLocal] = useState(false)

  // 已经登录了：回原处（整页，同登录成功）
  const signedIn = ready && !!me?.verified && me.mode !== 'off'
  useEffect(() => { if (signedIn) window.location.assign(next) }, [signedIn, next])
  if (ready && me?.mode === 'off') return <Navigate to="/" replace />
  if (signedIn) return null

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (busy || !username.trim() || !password) return
    setBusy(true)
    setError(null)
    try {
      await api.auth.login({ username: username.trim(), password })
      window.location.assign(next)
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? AUTH_TEXT.failed
        : err instanceof Error ? err.message : AUTH_TEXT.failed)
      setBusy(false)
    }
  }

  const oidc = me?.mode === 'oidc'
  const form = (
    <form className="space-y-3" onSubmit={(e) => void submit(e)} data-login-form="">
      <div>
        <label className="label" htmlFor="login-username">{AUTH_TEXT.username}</label>
        <input id="login-username" className="field" autoComplete="username" autoFocus={!oidc} value={username}
               onChange={(e) => setUsername(e.target.value)} />
      </div>
      <div>
        <label className="label" htmlFor="login-password">{AUTH_TEXT.password}</label>
        <input id="login-password" type="password" className="field" autoComplete="current-password" value={password}
               onChange={(e) => setPassword(e.target.value)} />
      </div>
      <button type="submit" className={oidc ? 'btn w-full justify-center' : 'btn btn-primary w-full justify-center'}
              disabled={busy || !username.trim() || !password}>
        {busy ? <Spinner size={12} /> : <LogIn size={12} aria-hidden />} {busy ? AUTH_TEXT.loggingIn : AUTH_TEXT.login}
      </button>
    </form>
  )

  // 新部署：先建第一个管理员（「已有账号」展开的是同一个登录表单）
  const bootstrapping = canBootstrap && !local

  return (
    <div className="flex h-full items-center justify-center overflow-auto bg-bg p-6" data-login-page={me?.mode ?? ''}>
      <div className="w-full max-w-[360px]">
        <div className="flex items-center gap-3">
          <Logo size={34} className="text-accent" title="AgentLab" />
          <div>
            <h1 className="text-lg font-semibold leading-tight text-fg">{AUTH_TEXT.title}</h1>
            <div className="text-2xs text-faint">{AUTH_TEXT.subtitle}{me ? ` · ${AUTH_MODE_LABEL[me.mode]}` : ''}</div>
          </div>
        </div>
        <div className="mt-6 space-y-3 rounded-lg border bg-panel p-4 shadow-elev-1">
          {!ready ? (
            <div className="flex items-center gap-2 text-xs text-dim" role="status"><Spinner size={12} /> {AUTH_TEXT.checking}</div>
          ) : (
            <>
              {error && <Notice tone="err" attr={{ 'data-login-error': '' }}>{error}</Notice>}
              {bootstrapping ? (
                <>
                  <BootstrapForm next={next} onError={setError} />
                  <button type="button" className="w-full text-center text-2xs text-dim underline-offset-2 hover:text-fg hover:underline"
                          onClick={() => { setError(null); setLocal(true) }} data-login-have-account="">
                    {AUTH_TEXT.haveAccount}
                  </button>
                </>
              ) : oidc ? (
                <>
                  <a href={api.auth.oidcStartUrl(next)} className="btn btn-primary w-full justify-center" data-login-oidc="">
                    <KeyRound size={12} aria-hidden /> {AUTH_TEXT.oidc}
                  </a>
                  {local ? form : (
                    <button type="button" className="w-full text-center text-2xs text-dim underline-offset-2 hover:text-fg hover:underline"
                            onClick={() => setLocal(true)}>
                      {AUTH_TEXT.useLocal}
                    </button>
                  )}
                </>
              ) : form}
              {!bootstrapping && <p className="text-2xs text-faint">{AUTH_TEXT.forgot}</p>}
            </>
          )}
        </div>
      </div>
    </div>
  )
}

/** 建第一个管理员：用户名、显示名（选填）、密码、确认密码。建好服务端就以他登录，整页回原处（同登录成功） */
function BootstrapForm({ next, onError }: { next: string; onError: (message: string | null) => void }) {
  const [form, setForm] = useState({ username: '', display_name: '', password: '', confirm: '' })
  const [busy, setBusy] = useState(false)
  const mismatch = !!form.confirm && form.confirm !== form.password
  const ready = !!form.username.trim() && !!form.password && form.confirm === form.password
  const set = (patch: Partial<typeof form>) => setForm((f) => ({ ...f, ...patch }))
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (busy || !ready) return
    setBusy(true)
    onError(null)
    try {
      await api.auth.bootstrap({ username: form.username.trim(), password: form.password,
        ...(form.display_name.trim() ? { display_name: form.display_name.trim() } : {}) })
      window.location.assign(next)
    } catch (err) {
      // 密码太短、用户名重复、别人刚建好了管理员：服务端的原话已经说清怎么办
      onError(err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }
  return (
    <form className="space-y-3" onSubmit={(e) => void submit(e)} data-login-bootstrap="">
      <div>
        <div className="flex items-center gap-1.5 text-sm font-medium text-fg">
          <UserPlus size={14} aria-hidden /> {AUTH_TEXT.bootstrapTitle}
        </div>
        <p className="mt-1 text-2xs leading-relaxed text-dim">{AUTH_TEXT.bootstrapHint}</p>
      </div>
      <div>
        <label className="label" htmlFor="boot-username">{AUTH_TEXT.username}</label>
        <input id="boot-username" className="field" autoComplete="username" autoFocus value={form.username}
               onChange={(e) => set({ username: e.target.value })} />
      </div>
      <div>
        <label className="label" htmlFor="boot-display">{AUTH_TEXT.displayName}</label>
        <input id="boot-display" className="field" autoComplete="name" value={form.display_name}
               aria-describedby="boot-display-hint" onChange={(e) => set({ display_name: e.target.value })} />
        <p id="boot-display-hint" className="mt-1 text-2xs text-faint">{AUTH_TEXT.displayNameHint}</p>
      </div>
      <div>
        <label className="label" htmlFor="boot-password">{AUTH_TEXT.password}</label>
        <input id="boot-password" type="password" className="field" autoComplete="new-password" value={form.password}
               onChange={(e) => set({ password: e.target.value })} />
      </div>
      <div>
        <label className="label" htmlFor="boot-confirm">{AUTH_TEXT.confirmPassword}</label>
        <input id="boot-confirm" type="password" className="field" autoComplete="new-password" value={form.confirm}
               aria-invalid={mismatch || undefined} aria-describedby={mismatch ? 'boot-confirm-error' : undefined}
               onChange={(e) => set({ confirm: e.target.value })} />
        {mismatch && (
          <p id="boot-confirm-error" className="mt-1 text-2xs" style={{ color: 'var(--err)' }} data-login-mismatch="">
            {AUTH_TEXT.passwordMismatch}
          </p>
        )}
      </div>
      <button type="submit" className="btn btn-primary w-full justify-center" disabled={busy || !ready}>
        {busy ? <Spinner size={12} /> : <UserPlus size={12} aria-hidden />} {busy ? AUTH_TEXT.bootstrapping : AUTH_TEXT.bootstrap}
      </button>
    </form>
  )
}
