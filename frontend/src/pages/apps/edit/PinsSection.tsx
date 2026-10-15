import { useEffect, useState } from 'react'
import { Plus, RefreshCw } from 'lucide-react'
import { api } from '../../../api/client'
import { DeleteButton, EmptyState, Field, SectionBar, confirmDialog, toast } from '../../../components/ui'
import { APP_EDITOR_TEXT as T, APP_TERM_TEXT, WORKFLOW_STATUS_LABEL } from '../../../lib/terms'
import { useCatalog } from '../../../store/catalog'
import type { WorkflowVersion } from '../../../types'
import type { AppPin, PinUse } from '../../../typesApp'
import { useEditor } from './context'

/*
 * 固定版本（A1）：应用引用的工作流版本，每一项 {alias, workflow_id, version, graph_hash, level, use}。
 *
 * 只能固定「已发布且未退役」的版本（服务端 published_versions 是唯一的说法，这里按版本列表上的 level、retired_at
 * 过滤，发布检查 app.pin_unpublished / app.pin_retired 兜底）。graph_hash 从版本详情取：正式运行按它发起，
 * 对不上就拒绝（run_version_hash_mismatch）
 */

const USES: PinUse[] = ['view', 'entry', 'process']

/** 一个工作流「已发布且未退役」的版本；老服务端的版本列表没有 level，只认当前发布版 */
function usable(v: WorkflowVersion, current?: number | null): boolean {
  if (v.retired_at) return false
  return v.level != null ? true : v.published === true || v.version === current
}

/** 选一个已发布且未退役的版本。value 是版本号；选中后回调版本详情（带 graph_hash、level） */
export function PublishedVersionPicker({ workflowId, value, onChange, compact = false }: {
  workflowId: string
  value: number | null
  onChange: (v: WorkflowVersion | null) => void
  compact?: boolean
}) {
  const current = useCatalog((s) => s.workflows.find((w) => w.id === workflowId)?.published_version)
  const [list, setList] = useState<WorkflowVersion[] | null>(null)
  useEffect(() => {
    let alive = true
    setList(null)
    api.workflows.versions(workflowId).then((vs) => { if (alive) setList(vs) }).catch(() => { if (alive) setList([]) })
    return () => { alive = false }
  }, [workflowId])
  const options = (list ?? []).filter((v) => usable(v, current)).sort((a, b) => b.version - a.version)
  const pick = async (n: number | null) => {
    if (n == null) { onChange(null); return }
    try {
      onChange(await api.workflows.version(workflowId, n))
    } catch (e) {
      toast.error(e)
    }
  }
  const select = (
    <select className="field" value={value ?? ''} disabled={!list} aria-label={T.pickVersion}
            onChange={(e) => void pick(e.target.value ? Number(e.target.value) : null)}>
      {!compact && <option value="">{list && !options.length ? T.noPublished : '—'}</option>}
      {options.map((v) => (
        <option key={v.version} value={v.version}>
          v{v.version}{v.level ? ` · ${WORKFLOW_STATUS_LABEL[v.level]}` : ''}{v.note ? ` · ${v.note}` : ''}
        </option>
      ))}
      {value != null && list && !options.some((v) => v.version === value) && (
        <option value={value} disabled>v{value} · {T.pinRetired}</option>
      )}
    </select>
  )
  if (compact) return select
  return <Field label={T.pickVersion} hint={list && !options.length ? T.noPublished : undefined}>{() => select}</Field>
}

export function PinsSection() {
  const { draft, setDraft } = useEditor()
  const workflows = useCatalog((s) => s.workflows)
  const nameOf = (id: string) => workflows.find((w) => w.id === id)?.name ?? id
  const [adding, setAdding] = useState(false)

  const update = (i: number, patch: Partial<AppPin>) =>
    setDraft((s) => ({ ...s, pins: s.pins.map((p, j) => (j === i ? { ...p, ...patch } : p)) }))

  const rederive = async (pin: AppPin) => {
    // 还没发布的（试运行派生的草稿）、个人应用的钉住项没有工作流版本可派生
    if (!pin.workflow_id || pin.version == null) return
    const { workflow_id: wid, version } = pin
    if (!(await confirmDialog({ title: T.rederiveTitle, body: T.rederiveBody, confirmLabel: T.rederive, danger: true }))) return
    try {
      const { spec } = await api.apps.derive({ workflow_id: wid, version, name: draft.name })
      setDraft(() => ({ ...spec, name: draft.name, description: draft.description || spec.description }))
      toast.ok(T.rederiveDone)
    } catch (e) {
      toast.error(e)
    }
  }

  return (
    <div className="mx-auto max-w-4xl p-4" data-app-pins={draft.pins.length}>
      <SectionBar title={T.sections.pins} hint={T.pinsHint}>
        <button type="button" className="btn btn-sm" onClick={() => setAdding(true)} data-pin-add="">
          <Plus size={12} aria-hidden /> {T.pinAdd}
        </button>
      </SectionBar>
      {/* 这一节里两个要先懂的词：固定版本、处置（上面那句提到的） */}
      <div className="-mt-1 mb-3 space-y-0.5 text-2xs leading-relaxed text-dim" data-app-terms="">
        <p data-app-term="pin">{APP_TERM_TEXT.pin}</p>
        <p data-app-term="disposition">{APP_TERM_TEXT.disposition}</p>
      </div>
      {!draft.pins.length && !adding && <EmptyState title={T.pinsEmpty} body={T.newIntro} />}
      <div className="space-y-2">
        {draft.pins.map((pin, i) => (
          <div key={`${pin.alias}-${i}`} className="grid grid-cols-[140px_1fr_170px_110px_auto] items-end gap-2 rounded-lg border bg-panel px-3 py-2"
               data-pin={pin.alias}>
            <Field label={T.pinAlias}>
              {(p) => <input {...p} className="field mono text-xs" value={pin.alias} onChange={(e) => update(i, { alias: e.target.value })} />}
            </Field>
            <div className="min-w-0 pb-1.5 text-xs">
              <div className="truncate font-medium">{pin.workflow_id ? nameOf(pin.workflow_id) : '—'}</div>
              <div className="mono text-2xs text-faint">{T.pinHash(pin.graph_hash || '')}</div>
            </div>
            <Field label={T.pickVersion}>
              {() => (
                <PublishedVersionPicker compact workflowId={pin.workflow_id ?? ''} value={pin.version}
                                        onChange={(v) => v && update(i, { version: v.version, graph_hash: v.graph_hash ?? '',
                                                                           level: v.level ?? null })} />
              )}
            </Field>
            <Field label={T.pinUse} hint={pin.use === 'process' ? T.pinUseHint : undefined}>
              {(p) => (
                <select {...p} className="field" value={pin.use} onChange={(e) => update(i, { use: e.target.value as PinUse })}>
                  {USES.map((u) => <option key={u} value={u}>{T.pinUses[u]}</option>)}
                </select>
              )}
            </Field>
            <div className="flex items-center gap-1 pb-1">
              <button type="button" className="btn btn-xs btn-ghost" title={T.rederive} aria-label={T.rederive}
                      onClick={() => void rederive(pin)} data-pin-rederive="">
                <RefreshCw size={11} aria-hidden />
              </button>
              <DeleteButton label={T.pinRemove(pin.alias)}
                            onClick={() => setDraft((s) => ({ ...s, pins: s.pins.filter((_, j) => j !== i) }))} />
            </div>
          </div>
        ))}
      </div>
      {adding && <PinAdder onDone={() => setAdding(false)} />}
    </div>
  )
}

function PinAdder({ onDone }: { onDone: () => void }) {
  const { draft, setDraft } = useEditor()
  const workflows = useCatalog((s) => s.workflows)
  const [workflowId, setWorkflowId] = useState('')
  const [version, setVersion] = useState<WorkflowVersion | null>(null)
  const [alias, setAlias] = useState('')
  const [use, setUse] = useState<PinUse>('view')
  const taken = new Set(draft.pins.map((p) => p.alias))
  const add = () => {
    if (!version) return
    let name = alias.trim() || `p${draft.pins.length + 1}`
    for (let n = 2; taken.has(name); n++) name = `${alias.trim() || 'p'}_${n}`
    setDraft((s) => ({ ...s, pins: [...s.pins, {
      alias: name, workflow_id: workflowId, version: version.version, graph_hash: version.graph_hash ?? '',
      level: version.level ?? null, use,
    }] }))
    onDone()
  }
  return (
    <div className="mt-3 space-y-2 rounded-lg border border-dashed px-3 py-3" data-pin-adder="">
      <div className="grid grid-cols-2 gap-2">
        <Field label={T.pickWorkflow}>
          {(p) => (
            <select {...p} className="field" value={workflowId} onChange={(e) => { setWorkflowId(e.target.value); setVersion(null) }}>
              <option value="">{T.pickWorkflowFirst}</option>
              {workflows.filter((w) => w.published_version != null).map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
            </select>
          )}
        </Field>
        {workflowId
          ? <PublishedVersionPicker workflowId={workflowId} value={version?.version ?? null} onChange={setVersion} />
          : <div />}
        <Field label={T.pinAlias}>
          {(p) => <input {...p} className="field mono text-xs" value={alias} placeholder="weekly" onChange={(e) => setAlias(e.target.value)} />}
        </Field>
        <Field label={T.pinUse}>
          {(p) => (
            <select {...p} className="field" value={use} onChange={(e) => setUse(e.target.value as PinUse)}>
              {USES.map((u) => <option key={u} value={u}>{T.pinUses[u]}</option>)}
            </select>
          )}
        </Field>
      </div>
      <div className="flex justify-end gap-2">
        <button type="button" className="btn btn-sm" onClick={onDone}>取消</button>
        <button type="button" className="btn btn-sm btn-primary" disabled={!version} onClick={add}>{T.pinAdd}</button>
      </div>
    </div>
  )
}
