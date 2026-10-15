/**
 * 选人或组：岗位绑定、应用授权、改派、表单里的 principal 控件共用。
 *
 * 开了登录、搭建者及以上：从名录（GET /api/directory，只读：用户名、显示名、是否停用、组名）取候选，填的名字逐个核对——
 * 名录里没有的、停用了的不让添加，说清是哪一个（账号密码登录时人和组都在本系统里建，名录里没有就是填错了）；单点登录的
 * 人和组第一次登录后才进名录，名录里没有的照实提醒，点「仍然添加」才加上。已选的里头名录里没有的、停用了的在标签上
 * 标出来，不静默放着。
 * 使用者读不到名录：照填，服务端按名字认。关登录时没有用户表，只能手填名字——签批记的也是各自填的署名，这里不假装能核对。
 */
import { useEffect, useId, useState } from 'react'
import { Plus, Users, UserRound, X } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { PRINCIPAL_TEXT } from '../lib/termsApp'
import { useAtLeast, useAuth, useLoginOn } from '../store/auth'
import type { Directory } from '../typesApp'
import { isComposing } from './ui'

export interface Principal {
  kind: 'user' | 'group' | 'everyone'
  name: string
}

export interface PrincipalPickerProps {
  value: Principal[]
  onChange: (value: Principal[]) => void
  /** 允许选「所有人」（应用授权用） */
  allowEveryone?: boolean
  /** 只能选一个 */
  single?: boolean
  label?: string
}

/** 名录只取一次，同一页上几个选择器共用；没取到（断线）下次打开再取 */
let directory: Promise<Directory | null> | null = null
function loadDirectory(): Promise<Directory | null> {
  directory ??= api.identity.directory().catch(() => { directory = null; return null })
  return directory
}

/** 这一项在名录里对不上的地方：没有这个人或组（missing）、人停用了（disabled）。名录没取到、选的是所有人时不判 */
function problemOf(p: Principal, known: Directory | null): 'missing' | 'disabled' | null {
  if (!known || p.kind === 'everyone') return null
  if (p.kind === 'group') return known.groups.some((g) => g.name === p.name) ? null : 'missing'
  const user = known.users.find((u) => u.username === p.name)
  return !user ? 'missing' : user.disabled ? 'disabled' : null
}

export function PrincipalPicker({ value, onChange, allowEveryone = false, single = false, label }: PrincipalPickerProps) {
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, '')
  const loginOn = useLoginOn()
  // 名录给搭建者及以上（使用者读不到，不去取：取了也是 403）
  const listable = useAtLeast('builder') && loginOn
  const oidc = useAuth((s) => s.me?.mode === 'oidc')
  const [kind, setKind] = useState<'user' | 'group'>('user')
  const [text, setText] = useState('')
  const [known, setKnown] = useState<Directory | null>(null)
  /** 刚才要加、名录里对不上的那一项：soft 是单点登录下名录里还没有（可以「仍然添加」），其余不让加 */
  const [refused, setRefused] = useState<{ item: Principal; message: string; soft: boolean } | null>(null)
  useEffect(() => {
    if (!listable) return
    let alive = true
    void loadDirectory().then((d) => { if (alive) setKnown(d) })
    return () => { alive = false }
  }, [listable])

  const has = (p: Principal) => value.some((x) => x.kind === p.kind && x.name === p.name)
  const add = (p: Principal, anyway = false) => {
    const item = { ...p, name: p.name.trim() }
    if (!item.name) return
    if (has(item)) { setText(''); setRefused(null); return }
    const problem = problemOf(item, known)
    const soft = problem === 'missing' && oidc
    if (problem && !(soft && anyway)) {
      setRefused({
        item, soft,
        message: problem === 'disabled' ? PRINCIPAL_TEXT.disabledUser(item.name)
          : soft ? PRINCIPAL_TEXT.unknownOidc(item.name)
          : item.kind === 'group' ? PRINCIPAL_TEXT.unknownGroup(item.name) : PRINCIPAL_TEXT.unknownUser(item.name),
      })
      return
    }
    onChange(single ? [item] : [...value, item])
    setText('')
    setRefused(null)
  }
  const remove = (p: Principal) => onChange(value.filter((x) => !(x.kind === p.kind && x.name === p.name)))
  const listId = `${uid}-list`
  const errorId = `${uid}-error`
  const options = !known ? [] : kind === 'user'
    ? known.users.filter((u) => !u.disabled).map((u) => ({ value: u.username, label: u.display_name }))
    : known.groups.map((g) => ({ value: g.name, label: g.label }))

  return (
    <div className="min-w-0" data-principal-picker="">
      {value.length > 0 && (
        <ul className="mb-1.5 flex flex-wrap gap-1" aria-label={label}>
          {value.map((p) => {
            const problem = problemOf(p, known)
            return (
              <li key={`${p.kind}:${p.name}`} className="chip flex items-center gap-1" data-principal={p.kind}
                  data-principal-problem={problem ?? undefined}
                  style={problem ? { color: 'var(--st-waiting)', borderColor: 'var(--st-waiting)' } : undefined}>
                {p.kind === 'group' ? <Users size={11} aria-hidden /> : p.kind === 'user' ? <UserRound size={11} aria-hidden /> : null}
                <span>{p.kind === 'everyone' ? PRINCIPAL_TEXT.everyone : p.name}</span>
                <span className="sr-only">（{PRINCIPAL_TEXT[p.kind]}）</span>
                {problem && <span className="text-2xs">· {problem === 'disabled' ? PRINCIPAL_TEXT.disabledTag : PRINCIPAL_TEXT.missingTag}</span>}
                <button type="button" className="-mr-0.5 rounded text-faint hover:text-fg" aria-label={PRINCIPAL_TEXT.remove(p.kind === 'everyone' ? PRINCIPAL_TEXT.everyone : p.name)}
                        onClick={() => remove(p)}>
                  <X size={11} aria-hidden />
                </button>
              </li>
            )
          })}
        </ul>
      )}
      <div className="flex items-center gap-1.5">
        <select className="field w-auto shrink-0" aria-label={PRINCIPAL_TEXT.kind} value={kind}
                onChange={(e) => { setKind(e.target.value as 'user' | 'group'); setRefused(null) }}>
          <option value="user">{PRINCIPAL_TEXT.user}</option>
          <option value="group">{PRINCIPAL_TEXT.group}</option>
        </select>
        <input className="field min-w-0 flex-1" aria-label={label ?? PRINCIPAL_TEXT.placeholder}
               placeholder={PRINCIPAL_TEXT.placeholder} list={options.length ? listId : undefined} value={text}
               aria-invalid={refused && !refused.soft ? true : undefined} aria-describedby={refused ? errorId : undefined}
               onChange={(e) => { setText(e.target.value); setRefused(null) }}
               onKeyDown={(e) => {
                 if (e.key !== 'Enter' || isComposing(e)) return
                 e.preventDefault()
                 add({ kind, name: text })
               }}
               // 单选时填完就算选好，不用再点「添加」：表单里的选人控件和普通输入框一样，失焦即生效
               onBlur={() => { if (single && text.trim()) add({ kind, name: text }) }} />
        {options.length > 0 && (
          <datalist id={listId}>
            {options.map((o) => <option key={o.value} value={o.value}>{o.label !== o.value ? o.label : undefined}</option>)}
          </datalist>
        )}
        <button type="button" className="btn btn-sm shrink-0" disabled={!text.trim()} onClick={() => add({ kind, name: text })}>
          <Plus size={12} aria-hidden /> {PRINCIPAL_TEXT.add}
        </button>
        {allowEveryone && (
          <button type="button" className={clsx('btn btn-sm shrink-0', has({ kind: 'everyone', name: '*' }) && 'opacity-50')}
                  disabled={has({ kind: 'everyone', name: '*' })} onClick={() => add({ kind: 'everyone', name: '*' })}>
            {PRINCIPAL_TEXT.everyone}
          </button>
        )}
      </div>
      {refused && (
        <div id={errorId} role="alert" className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs leading-relaxed"
             style={{ color: refused.soft ? 'var(--st-waiting)' : 'var(--err)' }} data-principal-refused={refused.soft ? 'soft' : 'hard'}>
          <span>{refused.message}</span>
          {refused.soft && (
            <button type="button" className="btn btn-xs" onClick={() => add(refused.item, true)} data-principal-add-anyway="">
              {PRINCIPAL_TEXT.addAnyway}
            </button>
          )}
        </div>
      )}
      {!loginOn && <div className="mt-1 text-2xs text-faint">{PRINCIPAL_TEXT.manualHint}</div>}
    </div>
  )
}
