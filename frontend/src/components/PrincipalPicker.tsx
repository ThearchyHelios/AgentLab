/**
 * 选人或组：岗位绑定、应用授权、改派、表单里的 principal 控件共用。
 *
 * 开了登录、搭建者及以上：从名录（GET /api/directory，只读：用户名、显示名、是否停用、组名）取候选，填的名字逐个核对——
 * 名录里没有的、停用了的不让添加，说清是哪一个（账号密码登录时人和组都在本系统里建，名录里没有就是填错了）；单点登录的
 * 人和组第一次登录后才进名录，名录里没有的照实提醒，点「仍然添加」才加上。已选的里头名录里没有的、停用了的在标签上
 * 标出来，不静默放着。
 * 使用者读不到名录（只读与最小可见）：给了 source 的（表单里的选人控件、个人应用提交审核）边输入边按开头取候选
 * （GET /api/directory/candidates，05-已知遗留 (b)），只有这件事能选的人、只有名字；加的时候按同一个池问一次服务端，不在池里的
 * 当场说清。提交时服务端照同一个池核对（不在池里 422），这里只是提前说。没给 source 的照填，服务端按名字认。
 * 关登录时没有用户表，只能手填名字——签批记的也是各自填的署名，这里不假装能核对。
 */
import { useEffect, useId, useRef, useState } from 'react'
import { Plus, Users, UserRound, X } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { PRINCIPAL_TEXT } from '../lib/termsApp'
import { useAtLeast, useAuth, useLoginOn } from '../store/auth'
import type { CandidateSource, Candidates, Directory } from '../typesApp'
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
  /**
   * 使用者取候选的池（服务端按用途算）：表单里的选人控件（入口、待办）、个人应用提交审核。搭建者及以上照旧用整份名录，
   * 不看它
   */
  source?: CandidateSource
  /**
   * 能不能选服务账号（docs/design/next-round/04-对外集成与触达.md A1）：应用授权能（外部系统按授权读应用）；岗位绑定、改派
   * 不能（服务账号只持有访问密钥，回复不了待办）——缺省不能，名录里的服务账号不列、填了说清是服务账号
   */
  allowService?: boolean
}

/** 边输入边取候选：停手这么久再去问，不是每敲一个字问一次 */
const ASK_DELAY_MS = 250

/** 名录只取一次，同一页上几个选择器共用；没取到（断线）下次打开再取 */
let directory: Promise<Directory | null> | null = null
function loadDirectory(): Promise<Directory | null> {
  directory ??= api.identity.directory().catch(() => { directory = null; return null })
  return directory
}

/**
 * 这一项在名录里对不上的地方：没有这个人或组（missing）、人停用了（disabled）、是服务账号而这里不收（service）。名录没取到、
 * 选的是所有人时不判
 */
function problemOf(p: Principal, known: Directory | null, allowService: boolean): 'missing' | 'disabled' | 'service' | null {
  if (!known || p.kind === 'everyone') return null
  if (p.kind === 'group') return known.groups.some((g) => g.name === p.name) ? null : 'missing'
  const user = known.users.find((u) => u.username === p.name)
  return !user ? 'missing' : user.disabled ? 'disabled' : user.service && !allowService ? 'service' : null
}

export function PrincipalPicker({
  value, onChange, allowEveryone = false, single = false, label, source, allowService = false,
}: PrincipalPickerProps) {
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, '')
  const loginOn = useLoginOn()
  // 名录给搭建者及以上（使用者读不到，不去取：取了也是 403）
  const listable = useAtLeast('builder') && loginOn
  // 使用者：有 source 就按池取候选
  const asking = !listable && loginOn && !!source
  const sourceKey = source ? JSON.stringify(source) : ''
  const oidc = useAuth((s) => s.me?.mode === 'oidc')
  const [kind, setKind] = useState<'user' | 'group'>('user')
  const [text, setText] = useState('')
  const [known, setKnown] = useState<Directory | null>(null)
  const [pool, setPool] = useState<Candidates | null>(null)
  /** 取到过的候选：用户名 → 显示名（标签上写显示名，悬停给用户名） */
  const shown = useRef(new Map<string, string>())
  /** 刚才要加、名录里对不上的那一项：soft 是单点登录下名录里还没有（可以「仍然添加」），其余不让加 */
  const [refused, setRefused] = useState<{ item: Principal; message: string; soft: boolean } | null>(null)
  useEffect(() => {
    if (!listable) return
    let alive = true
    void loadDirectory().then((d) => { if (alive) setKnown(d) })
    return () => { alive = false }
  }, [listable])
  useEffect(() => {
    if (!asking || !source) return
    let alive = true
    const timer = window.setTimeout(() => {
      api.identity.candidates(source, text.trim())
        .then((c) => {
          if (!alive) return
          for (const u of c.users) shown.current.set(u.username, u.display_name)
          setPool(c)
        })
        .catch(() => { if (alive) setPool(null) })
    }, ASK_DELAY_MS)
    return () => { alive = false; window.clearTimeout(timer) }
    // sourceKey 代替 source：调用方每次渲染都给一个新对象
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [asking, sourceKey, text])

  const has = (p: Principal) => value.some((x) => x.kind === p.kind && x.name === p.name)
  const add = async (p: Principal, anyway = false) => {
    const item = { ...p, name: p.name.trim() }
    if (!item.name) return
    if (has(item)) { setText(''); setRefused(null); return }
    if (asking && source && item.kind !== 'everyone') {
      // 按同一个池问一次服务端：不在池里的当场说清（提交时服务端照样拒）。问不到（断线）就先收下，提交时由服务端判
      const found = await api.identity.candidates(source, item.name, 50).catch(() => null)
      const listed = found && (item.kind === 'group' ? found.groups.some((g) => g.name === item.name)
        : found.users.some((u) => u.username === item.name))
      if (found && !listed) {
        setRefused({ item, soft: false, message: PRINCIPAL_TEXT.notCandidate(item.name) })
        return
      }
    }
    const problem = problemOf(item, known, allowService)
    const soft = problem === 'missing' && oidc
    if (problem && !(soft && anyway)) {
      setRefused({
        item, soft,
        message: problem === 'service' ? PRINCIPAL_TEXT.serviceUser(item.name)
          : problem === 'disabled' ? PRINCIPAL_TEXT.disabledUser(item.name)
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
  const options = asking
    ? (!pool ? [] : kind === 'user' ? pool.users.map((u) => ({ value: u.username, label: u.display_name }))
      : pool.groups.map((g) => ({ value: g.name, label: g.label })))
    : !known ? [] : kind === 'user'
      ? known.users.filter((u) => !u.disabled && (allowService || !u.service))
        .map((u) => ({ value: u.username, label: u.display_name }))
      : known.groups.map((g) => ({ value: g.name, label: g.label }))
  /** 候选池要先输入开头才列（应用授权给了所有人）、列不全（超过上限） */
  const poolHint = asking && pool
    ? (pool.needs_query && !text.trim() ? PRINCIPAL_TEXT.typeToSearch : pool.truncated ? PRINCIPAL_TEXT.truncated : null)
    : null

  return (
    <div className="min-w-0" data-principal-picker="">
      {value.length > 0 && (
        <ul className="mb-1.5 flex flex-wrap gap-1" aria-label={label}>
          {value.map((p) => {
            const problem = problemOf(p, known, allowService)
            return (
              <li key={`${p.kind}:${p.name}`} className="chip flex items-center gap-1" data-principal={p.kind}
                  data-principal-problem={problem ?? undefined}
                  style={problem ? { color: 'var(--st-waiting)', borderColor: 'var(--st-waiting)' } : undefined}>
                {p.kind === 'group' ? <Users size={11} aria-hidden /> : p.kind === 'user' ? <UserRound size={11} aria-hidden /> : null}
                <span title={p.kind === 'user' && shown.current.get(p.name) ? p.name : undefined}>
                  {p.kind === 'everyone' ? PRINCIPAL_TEXT.everyone : shown.current.get(p.name) ?? p.name}
                </span>
                <span className="sr-only">（{PRINCIPAL_TEXT[p.kind]}）</span>
                {problem && <span className="text-2xs">· {problem === 'disabled' ? PRINCIPAL_TEXT.disabledTag
                  : problem === 'service' ? PRINCIPAL_TEXT.serviceTag : PRINCIPAL_TEXT.missingTag}</span>}
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
                 void add({ kind, name: text })
               }}
               // 单选时填完就算选好，不用再点「添加」：表单里的选人控件和普通输入框一样，失焦即生效
               onBlur={() => { if (single && text.trim()) void add({ kind, name: text }) }} />
        {options.length > 0 && (
          <datalist id={listId}>
            {options.map((o) => <option key={o.value} value={o.value}>{o.label !== o.value ? o.label : undefined}</option>)}
          </datalist>
        )}
        <button type="button" className="btn btn-sm shrink-0" disabled={!text.trim()} onClick={() => void add({ kind, name: text })}>
          <Plus size={12} aria-hidden /> {PRINCIPAL_TEXT.add}
        </button>
        {allowEveryone && (
          <button type="button" className={clsx('btn btn-sm shrink-0', has({ kind: 'everyone', name: '*' }) && 'opacity-50')}
                  disabled={has({ kind: 'everyone', name: '*' })} onClick={() => void add({ kind: 'everyone', name: '*' })}>
            {PRINCIPAL_TEXT.everyone}
          </button>
        )}
      </div>
      {refused && (
        <div id={errorId} role="alert" className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs leading-relaxed"
             style={{ color: refused.soft ? 'var(--st-waiting)' : 'var(--err)' }} data-principal-refused={refused.soft ? 'soft' : 'hard'}>
          <span>{refused.message}</span>
          {refused.soft && (
            <button type="button" className="btn btn-xs" onClick={() => void add(refused.item, true)} data-principal-add-anyway="">
              {PRINCIPAL_TEXT.addAnyway}
            </button>
          )}
        </div>
      )}
      {poolHint && <div className="mt-1 text-2xs text-faint" data-principal-pool-hint="">{poolHint}</div>}
      {!loginOn && <div className="mt-1 text-2xs text-faint">{PRINCIPAL_TEXT.manualHint}</div>}
    </div>
  )
}
