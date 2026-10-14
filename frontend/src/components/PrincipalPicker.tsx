/**
 * 选人或组：岗位绑定、应用授权、改派、表单里的 principal 控件共用。
 *
 * 开了登录时从用户表、组表取候选（下拉提示，照样可以手填：取候选要管理员权限，搭建者取不到时退回手填）；
 * 关登录时没有用户表，只能手填名字——签批记的也是各自填的署名，这里不假装能核对。
 */
import { useEffect, useId, useState } from 'react'
import { Plus, Users, UserRound, X } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { PRINCIPAL_TEXT } from '../lib/termsApp'
import { useLoginOn } from '../store/auth'
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

/** 候选只取一次，同一页上几个选择器共用 */
let candidates: Promise<{ users: string[]; groups: string[] }> | null = null
function loadCandidates() {
  candidates ??= Promise.all([
    api.identity.users().then((r) => r.items.filter((u) => !u.disabled).map((u) => u.username), () => [] as string[]),
    api.identity.groups().then((r) => r.items.map((g) => g.name), () => [] as string[]),
  ]).then(([users, groups]) => ({ users, groups }))
  return candidates
}

export function PrincipalPicker({ value, onChange, allowEveryone = false, single = false, label }: PrincipalPickerProps) {
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, '')
  const loginOn = useLoginOn()
  const [kind, setKind] = useState<'user' | 'group'>('user')
  const [text, setText] = useState('')
  const [known, setKnown] = useState<{ users: string[]; groups: string[] }>({ users: [], groups: [] })
  useEffect(() => {
    if (!loginOn) return
    let alive = true
    void loadCandidates().then((c) => { if (alive) setKnown(c) })
    return () => { alive = false }
  }, [loginOn])

  const has = (p: Principal) => value.some((x) => x.kind === p.kind && x.name === p.name)
  const add = (p: Principal) => {
    if (!p.name.trim() || has(p)) return
    const item = { ...p, name: p.name.trim() }
    onChange(single ? [item] : [...value, item])
    setText('')
  }
  const remove = (p: Principal) => onChange(value.filter((x) => !(x.kind === p.kind && x.name === p.name)))
  const listId = `${uid}-list`
  const options = kind === 'user' ? known.users : known.groups

  return (
    <div className="min-w-0" data-principal-picker="">
      {value.length > 0 && (
        <ul className="mb-1.5 flex flex-wrap gap-1" aria-label={label}>
          {value.map((p) => (
            <li key={`${p.kind}:${p.name}`} className="chip flex items-center gap-1" data-principal={p.kind}>
              {p.kind === 'group' ? <Users size={11} aria-hidden /> : p.kind === 'user' ? <UserRound size={11} aria-hidden /> : null}
              <span>{p.kind === 'everyone' ? PRINCIPAL_TEXT.everyone : p.name}</span>
              <span className="sr-only">（{PRINCIPAL_TEXT[p.kind]}）</span>
              <button type="button" className="-mr-0.5 rounded text-faint hover:text-fg" aria-label={PRINCIPAL_TEXT.remove(p.kind === 'everyone' ? PRINCIPAL_TEXT.everyone : p.name)}
                      onClick={() => remove(p)}>
                <X size={11} aria-hidden />
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex items-center gap-1.5">
        <select className="field w-auto shrink-0" aria-label={PRINCIPAL_TEXT.kind} value={kind}
                onChange={(e) => setKind(e.target.value as 'user' | 'group')}>
          <option value="user">{PRINCIPAL_TEXT.user}</option>
          <option value="group">{PRINCIPAL_TEXT.group}</option>
        </select>
        <input className="field min-w-0 flex-1" aria-label={label ?? PRINCIPAL_TEXT.placeholder}
               placeholder={PRINCIPAL_TEXT.placeholder} list={options.length ? listId : undefined} value={text}
               onChange={(e) => setText(e.target.value)}
               onKeyDown={(e) => {
                 if (e.key !== 'Enter' || isComposing(e)) return
                 e.preventDefault()
                 add({ kind, name: text })
               }}
               // 单选时填完就算选好，不用再点「添加」：表单里的选人控件和普通输入框一样，失焦即生效
               onBlur={() => { if (single && text.trim()) add({ kind, name: text }) }} />
        {options.length > 0 && (
          <datalist id={listId}>{options.map((o) => <option key={o} value={o} />)}</datalist>
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
      {!loginOn && <div className="mt-1 text-2xs text-faint">{PRINCIPAL_TEXT.manualHint}</div>}
    </div>
  )
}
