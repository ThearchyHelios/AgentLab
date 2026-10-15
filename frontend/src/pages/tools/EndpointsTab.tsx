import { useCallback, useEffect, useState } from 'react'
import { Plug, Plus, X } from 'lucide-react'
import { ApiError, api } from '../../api/client'
import {
  DeleteButton, EmptyState, ErrorState, Field, Modal, SectionBar, Skeleton, Spinner, confirmDialog, toast,
} from '../../components/ui'
import { ENDPOINT_TEXT as T } from '../../lib/terms'
import { useAtLeast } from '../../store/auth'
import { useOnReconnect } from '../../store/catalog'
import type { EndpointEnv, EndpointRow } from '../../typesApp'

/*
 * 工具页的「端点」页签（A6、§3.9）：管理员登记的外部系统地址和凭据，分测试、生产两个环境。
 *
 * 认证头加密保存，接口只回掩码；编辑时不动的那一项保持掩码原样提交，服务端认作「保持原值」。认证头按行编辑：名称为空、
 * 不合法、重复的那一行标红，不让保存——存成 {"": ""} 之后这个环境的每个请求都在发出之前失败（服务端同样拒收）。
 * 允许访问内网只有管理员能开，而且只有登记的端点能访问内网（开了也照样拦元数据地址和高危端口）。带 version 保存，
 * 别人刚改过时 409，载入最新的再改。搭建者只读（§3.12）。
 * 测试连接照服务端说的写：2xx 才是成功；能连上但基础地址回 404、401 的是提醒，不说成「连接成功」
 */

type EnvKey = 'test' | 'prod'
const ENVS: EnvKey[] = ['test', 'prod']

const blankEnv = (): EndpointEnv => ({ base_url: '', headers: {}, allow_intranet: false, verify_tls: true })

/** 认证头的一行。按行存而不是按名字存：名字还空着、两行同名时也要能画出来、标出来 */
interface HeaderRow { k: string; v: string }
const rowsOf = (env?: EndpointEnv): HeaderRow[] => Object.entries(env?.headers ?? {}).map(([k, v]) => ({ k, v }))

/** 请求头名称：HTTP 的 token（和服务端 capabilities/secrets.header_problem 同一条规矩，服务端为准） */
const HEADER_NAME = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/
/** 一行认证头的问题。重名只标后来的那一行：前面那一行本身没错，删掉或改掉后加的就好 */
function headerProblem(row: HeaderRow, rows: HeaderRow[], index: number): string | null {
  if (!row.k.trim()) return T.headerEmpty
  if (!HEADER_NAME.test(row.k)) return T.headerInvalid
  if (rows.slice(0, index).some((r) => r.k.toLowerCase() === row.k.toLowerCase())) return T.headerDuplicate
  return null
}

export function EndpointsTab() {
  const admin = useAtLeast('admin')
  const [rows, setRows] = useState<EndpointRow[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [editing, setEditing] = useState<EndpointRow | 'new' | null>(null)
  const [testing, setTesting] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const { items } = await api.endpoints.list()
      setRows(items)
      setError(null)
    } catch (e) {
      setError(e)
    }
  }, [])
  useEffect(() => { void load() }, [load])
  useOnReconnect(load)

  const test = async (row: EndpointRow, env: EnvKey) => {
    setTesting(`${row.id}/${env}`)
    try {
      // 服务端按状态码照实说：2xx 才是成功；连上了但回 404、401 的，和连不上的一样是提醒
      const res = await api.endpoints.test(row.id, env)
      const text = `${row.name} · ${T.envs[env]}：${res.message}`
      if (res.ok) toast.ok(text)
      else toast.warn(text)
    } catch (e) {
      toast.error(e)
    } finally {
      setTesting(null)
    }
  }

  const remove = async (row: EndpointRow) => {
    if (!(await confirmDialog({ title: T.removeTitle(row.name), body: T.removeBody, danger: true, confirmLabel: '删除' }))) return
    try {
      await api.endpoints.remove(row.id)
      await load()
    } catch (e) {
      toast.error(e)
    }
  }

  return (
    <div className="mx-auto max-w-4xl p-4" data-endpoints="">
      <SectionBar title={T.title} hint={T.hint}>
        {admin && (
          <button className="btn btn-primary btn-sm" onClick={() => setEditing('new')} data-endpoint-create="">
            <Plus size={12} aria-hidden /> {T.create}
          </button>
        )}
      </SectionBar>
      {!admin && <p className="mb-2 text-2xs text-faint">{T.readOnlyHint}</p>}
      {rows === null ? (
        error ? <ErrorState error={error} onRetry={() => void load()} /> : <Skeleton rows={3} height={52} gap={8} />
      ) : !rows.length ? (
        <EmptyState icon={<Plug size={22} />} title={T.empty} body={T.emptyBody} />
      ) : (
        <div className="space-y-2">
          {rows.map((row) => (
            <div key={row.id} className="rounded-lg border bg-panel px-3 py-2.5" data-endpoint={row.name}>
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium">{row.name}</span>
                <span className="tnum text-2xs text-faint">v{row.version}</span>
                <span className="flex-1" />
                {admin && <button className="btn btn-sm btn-ghost" onClick={() => setEditing(row)}>编辑</button>}
                {admin && <DeleteButton label={`删除端点 ${row.name}`} onClick={() => void remove(row)} />}
              </div>
              {row.description && <div className="text-xs text-faint">{row.description}</div>}
              <div className="mt-1.5 grid grid-cols-2 gap-2">
                {ENVS.map((env) => {
                  const e = row.environments[env]
                  return (
                    <div key={env} className="min-w-0 rounded border px-2 py-1.5 text-2xs" data-endpoint-env={env}>
                      <div className="flex items-center gap-1.5">
                        <span className="font-medium">{T.envs[env]}</span>
                        {e?.allow_intranet && <span className="chip">{T.allowIntranet}</span>}
                        <span className="flex-1" />
                        {e && (
                          <button className="btn btn-xs" disabled={testing === `${row.id}/${env}`}
                                  onClick={() => void test(row, env)} data-endpoint-test={env}>
                            {testing === `${row.id}/${env}` ? <Spinner size={10} /> : null} {T.test('')}
                          </button>
                        )}
                      </div>
                      {e ? (
                        <>
                          <div className="mono mt-0.5 truncate text-dim" title={e.base_url}>{e.base_url}</div>
                          {Object.keys(e.headers).length > 0 && (
                            <div className="mono mt-0.5 truncate text-faint" data-endpoint-headers="">
                              {Object.entries(e.headers).map(([k, v]) => `${k}: ${v}`).join(' · ')}
                            </div>
                          )}
                        </>
                      ) : (
                        <div className="mt-0.5 text-faint">{T.envMissing}{env === 'prod' ? ` · ${T.noProd}` : ''}</div>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      )}
      {editing && (
        <EndpointEditor row={editing === 'new' ? null : editing} onClose={() => setEditing(null)}
                        onSaved={async (saved) => { setEditing(null); toast.ok(T.saved(saved.name)); await load() }}
                        onConflict={async () => { setEditing(null); toast.warn(T.conflict); await load() }} />
      )}
    </div>
  )
}

function EndpointEditor({ row, onClose, onSaved, onConflict }: {
  row: EndpointRow | null
  onClose: () => void
  onSaved: (row: EndpointRow) => void
  onConflict: () => void
}) {
  const [name, setName] = useState(row?.name ?? '')
  const [description, setDescription] = useState(row?.description ?? '')
  const [envs, setEnvs] = useState<{ test?: EndpointEnv; prod?: EndpointEnv }>(
    () => row?.environments ?? { prod: blankEnv() })
  const [headers, setHeaders] = useState<Record<EnvKey, HeaderRow[]>>(
    () => ({ test: rowsOf(row?.environments.test), prod: rowsOf(row?.environments.prod) }))
  const [idem, setIdem] = useState(row?.idempotency_header ?? 'Idempotency-Key')
  const [timeout, setTimeoutS] = useState(row?.timeout_s ?? 20)
  const [busy, setBusy] = useState(false)
  // 配了的环境里有标红的请求头（名称空、不合法、重复）：不让保存
  const headerBlocked = ENVS.some((env) => envs[env] && headers[env].some((r, i) => headerProblem(r, headers[env], i)))
  const idemProblem = HEADER_NAME.test(idem) ? null : T.headerInvalid
  const setEnv = (env: EnvKey, patch: Partial<EndpointEnv> | null) =>
    setEnvs((cur) => {
      const next = { ...cur }
      if (patch == null) delete next[env]
      else next[env] = { ...(cur[env] ?? blankEnv()), ...patch }
      return next
    })

  const save = async () => {
    setBusy(true)
    const environments = Object.fromEntries(ENVS.filter((env) => envs[env]).map((env) => [env, {
      ...envs[env]!, headers: Object.fromEntries(headers[env].map((r) => [r.k, r.v])),
    }]))
    const body = { name: name.trim(), description, environments, idempotency_header: idem, timeout_s: timeout }
    try {
      const saved = row ? await api.endpoints.update(row.id, { ...body, version: row.version }) : await api.endpoints.create(body)
      onSaved(saved)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && row) onConflict()
      else toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open onClose={onClose} title={row ? T.edit(row.name) : T.create} width={680} dirty
           footer={<>
             <button className="btn" onClick={onClose}>取消</button>
             <button className="btn btn-primary" disabled={busy || !name.trim() || !Object.keys(envs).length || headerBlocked || !!idemProblem}
                     title={headerBlocked ? T.headerBlocked : undefined} onClick={() => void save()} data-endpoint-save="">
               {busy ? <Spinner size={11} /> : null} 保存
             </button>
           </>}>
      <div className="space-y-3" data-endpoint-editor="">
        <div className="grid grid-cols-2 gap-3">
          <Field label={T.name} required>
            {(p) => <input {...p} className="field" value={name} onChange={(e) => setName(e.target.value)} />}
          </Field>
          <Field label={T.description}>
            {(p) => <input {...p} className="field" value={description} onChange={(e) => setDescription(e.target.value)} />}
          </Field>
          <Field label={T.idempotency} error={idemProblem ?? undefined}>
            {(p) => <input {...p} className="field mono text-xs" value={idem} onChange={(e) => setIdem(e.target.value)} />}
          </Field>
          <Field label={T.timeout}>
            {(p) => <input {...p} className="field tnum" type="number" min={1} max={300} value={timeout}
                           onChange={(e) => setTimeoutS(Math.max(1, Number(e.target.value) || 1))} />}
          </Field>
        </div>
        {ENVS.map((env) => (
          <EnvEditor key={env} env={env} value={envs[env]} onChange={(patch) => setEnv(env, patch)}
                     headers={headers[env]} onHeaders={(next) => setHeaders((cur) => ({ ...cur, [env]: next }))} />
        ))}
      </div>
    </Modal>
  )
}

/** 一个环境：基础地址、认证头（掩码，按行）、允许访问内网、校验证书。没配置时只给「添加」 */
function EnvEditor({ env, value, onChange, headers, onHeaders }: {
  env: EnvKey; value?: EndpointEnv; onChange: (patch: Partial<EndpointEnv> | null) => void
  headers: HeaderRow[]; onHeaders: (rows: HeaderRow[]) => void
}) {
  const admin = useAtLeast('admin')
  if (!value) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-dashed px-3 py-2 text-xs" data-endpoint-env-editor={env}>
        <span className="font-medium">{T.envs[env]}</span>
        <span className="flex-1 text-2xs text-faint">{T.envMissing}{env === 'prod' ? ` · ${T.noProd}` : ''}</span>
        <button type="button" className="btn btn-xs" onClick={() => onChange({})}><Plus size={10} aria-hidden /> 添加</button>
      </div>
    )
  }
  const setHeader = (i: number, patch: Partial<HeaderRow>) =>
    onHeaders(headers.map((r, j) => (j === i ? { ...r, ...patch } : r)))
  return (
    <fieldset className="space-y-2 rounded-lg border px-3 py-2" data-endpoint-env-editor={env}>
      <legend className="px-1 text-xs font-medium">{T.envs[env]}</legend>
      <div className="flex items-center gap-2">
        <input className="field mono min-w-0 flex-1 text-xs" placeholder="https://erp.example.com/api" aria-label={T.baseUrl}
               value={value.base_url} onChange={(e) => onChange({ base_url: e.target.value })} />
        <button type="button" className="btn btn-xs btn-ghost" onClick={() => onChange(null)} aria-label={`移除${T.envs[env]}`}>
          <X size={11} aria-hidden />
        </button>
      </div>
      <div>
        <div className="mb-1 text-2xs text-faint">{T.headers} · {T.headersHint}</div>
        {headers.map((r, i) => {
          const problem = headerProblem(r, headers, i)
          const errorId = `endpoint-${env}-header-${i}-error`
          return (
            <div key={i} className="mb-1" data-endpoint-header={i}>
              <div className="flex gap-1.5">
                <input className="field mono w-44 shrink-0 text-xs" value={r.k} aria-label={T.headerName}
                       aria-invalid={problem ? true : undefined} aria-describedby={problem ? errorId : undefined}
                       onChange={(e) => setHeader(i, { k: e.target.value })} />
                <input className="field mono min-w-0 flex-1 text-xs" value={r.v} aria-label={T.headerValue}
                       onChange={(e) => setHeader(i, { v: e.target.value })} />
                <button type="button" className="btn btn-xs btn-ghost" aria-label={T.removeHeader(r.k)}
                        onClick={() => onHeaders(headers.filter((_, j) => j !== i))}>
                  <X size={11} aria-hidden />
                </button>
              </div>
              {problem && (
                <p id={errorId} className="mt-0.5 text-2xs" style={{ color: 'var(--err)' }} data-endpoint-header-error="">{problem}</p>
              )}
            </div>
          )
        })}
        <button type="button" className="btn btn-xs" onClick={() => onHeaders([...headers, { k: '', v: '' }])}>
          <Plus size={10} aria-hidden /> {T.addHeader}
        </button>
      </div>
      <div className="flex flex-wrap gap-4 text-xs">
        <label className="flex items-center gap-1.5" title={admin ? T.allowIntranetHint : T.readOnlyHint}>
          <input type="checkbox" className="accent-[var(--accent)]" checked={value.allow_intranet} disabled={!admin}
                 onChange={(e) => onChange({ allow_intranet: e.target.checked })} data-endpoint-intranet={env} />
          {T.allowIntranet}
        </label>
        <label className="flex items-center gap-1.5">
          <input type="checkbox" className="accent-[var(--accent)]" checked={value.verify_tls}
                 onChange={(e) => onChange({ verify_tls: e.target.checked })} />
          {T.verifyTls}
        </label>
      </div>
      <div className="text-2xs text-faint">{T.allowIntranetHint}</div>
    </fieldset>
  )
}
