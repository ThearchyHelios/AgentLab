import { useMemo } from 'react'
import type { Edge } from '@xyflow/react'
import clsx from 'clsx'
import { useRadioGroup } from '../components/ui'
import { modelOptions, providerOfModel, useCatalog } from '../store/catalog'
import { useStudio, type FlowNode } from '../store/studio'
import type { NodeType } from '../types'
import type { FieldDef, FieldRenderProps } from './nodeDefs'

/**
 * 核查类节点（反方审稿、双盲复算）检查器里的几样通用控件，nodeDefs 用下面的工厂函数拼进字段表：
 * - nodePickField：从画布上挑本节点上游的某一类节点（要审的报告、要复算的口径卡），留空按缺省取上游的那一个；
 * - triStateField：跟随设置 / 开启 / 关闭三态（配置里没有这一项、true、false）；
 * - verifyModelFields：核查模型的接入加模型，写法同结论句裁判的模型选择。
 *
 * 文案一律由调用方传进来（各节点自己那一块的 *_TEXT），控件里不写死任何一句话：两个节点说法不同（反方模型 / 盲算模型），
 * 控件是同一个。读画布节点用 studio store，不往 FieldRenderProps 里加字段。
 */

/** 节点在下拉里的叫法：名字和 id 不同时把 id 带上（同 NodeRefsInput） */
function nodeLabel(n: FlowNode): string {
  const label = String(n.data?.label || n.id)
  return label === n.id ? label : `${label}（${n.id}）`
}

/** 本节点的全部上游（沿连线往回找）。只认上游：下游的节点运行到这里时还没有产出 */
function upstreamOf(nodeId: string, edges: Edge[]): Set<string> {
  const parents = new Map<string, string[]>()
  for (const e of edges) parents.set(e.target, [...(parents.get(e.target) ?? []), e.source])
  const seen = new Set<string>()
  const stack = [...(parents.get(nodeId) ?? [])]
  while (stack.length) {
    const id = stack.pop()!
    if (seen.has(id) || id === nodeId) continue
    seen.add(id)
    stack.push(...(parents.get(id) ?? []))
  }
  return seen
}

/**
 * 这个节点实际要核查的是哪一个：配置里写了就是它（还在画布上的话），没写时上游恰好有一个就是那一个；
 * 上游有好几个、一个都没有时说不准，返回 undefined
 */
export function targetNode(nodeId: string, chosen: unknown, type: NodeType, nodes: FlowNode[], edges: Edge[]): FlowNode | undefined {
  if (typeof chosen === 'string' && chosen) return nodes.find((n) => n.id === chosen)
  const up = upstreamOf(nodeId, edges)
  const candidates = nodes.filter((n) => up.has(n.id) && n.data?.nodeType === type)
  return candidates.length === 1 ? candidates[0] : undefined
}

// ---------------------------------------------------------------- 选画布上的节点

export interface NodePickText {
  /** 留空那一项：上游恰好一个时给出它的名字 */
  auto: (only: string | null) => string
  /** 上游一个都没有 */
  none: string
  /** 上游不止一个、又没有选 */
  many: (n: number) => string
  /** 选中的节点已不在画布上（下拉里那一项的叫法） */
  missing: (id: string) => string
  /** 选中的节点不在上游（下拉里那一项的叫法） */
  notUpstream: (label: string) => string
  missingHint: string
  notUpstreamHint: string
}

function NodePick({ p, nodeType, text }: { p: FieldRenderProps; nodeType: NodeType; text: NodePickText }) {
  const nodes = useStudio((s) => s.nodes)
  const edges = useStudio((s) => s.edges)
  const options = useMemo(() => {
    const up = upstreamOf(p.nodeId, edges)
    return nodes.filter((n) => up.has(n.id) && n.data?.nodeType === nodeType)
  }, [nodes, edges, p.nodeId, nodeType])
  const value = typeof p.value === 'string' ? p.value : ''
  const chosen = value ? nodes.find((n) => n.id === value) : undefined
  // 选中的节点被删了、挪到了下游：下拉里照实多列一项（选不回去），不让它看着像「未选择」
  const stale = value && !options.some((n) => n.id === value) ? (chosen ? 'notUpstream' : 'missing') : null
  const hint = stale === 'missing' ? text.missingHint
    : stale === 'notUpstream' ? text.notUpstreamHint
    : !options.length ? text.none
    : !value && options.length > 1 ? text.many(options.length) : ''
  const hintId = `${p.id}-hint`
  const only = options.length === 1 ? String(options[0].data?.label || options[0].id) : null
  return (
    <>
      <select id={p.id} className="field" value={value} disabled={p.disabled} aria-invalid={p.invalid || undefined}
              aria-describedby={[p.describedBy, hint ? hintId : ''].filter(Boolean).join(' ') || undefined}
              style={p.invalid || stale ? { borderColor: p.invalid ? 'var(--err)' : 'var(--warn)' } : undefined}
              onChange={(e) => p.onChange(e.target.value || undefined)}>
        <option value="">{text.auto(only)}</option>
        {options.map((n) => <option key={n.id} value={n.id}>{nodeLabel(n)}</option>)}
        {stale && (
          <option value={value} disabled>{stale === 'missing' ? text.missing(value) : text.notUpstream(nodeLabel(chosen!))}</option>
        )}
      </select>
      {hint && (
        <div id={hintId} className="mt-1 text-2xs leading-snug" data-node-pick-hint={stale ?? (options.length ? 'many' : 'none')}
             style={{ color: 'var(--st-waiting)' }}>
          {hint}
        </div>
      )}
    </>
  )
}

/**
 * 从画布上挑本节点上游的某一类节点。配置里存节点 id；留空（写回 undefined，不留空字符串）时服务端按缺省取上游的那一个
 */
export function nodePickField(def: {
  key: string; label: string; nodeType: NodeType; text: NodePickText; help?: string; advanced?: boolean
}): FieldDef {
  return {
    key: def.key, label: def.label, type: 'select', help: def.help, advanced: def.advanced,
    render: (p) => <NodePick p={p} nodeType={def.nodeType} text={def.text} />,
  }
}

// ---------------------------------------------------------------- 三态开关

export interface TriStateText {
  follow: string
  on: string
  off: string
  /** 选中各态时下面那句说明：跟随的是哪一项设置、关掉之后会怎样 */
  hint: { follow: string; on: string; off: string }
}

type Tri = 'follow' | 'on' | 'off'
const TRI: readonly Tri[] = ['follow', 'on', 'off']

function TriState({ p, label, text }: { p: FieldRenderProps; label: string; text: TriStateText }) {
  // 只认真正的布尔：手写的 "false"、0 不当成关闭，按跟随设置显示（服务端校验会报出来）
  const state: Tri = p.value === true ? 'on' : p.value === false ? 'off' : 'follow'
  // 跟随设置写回 undefined：配置里不留这一项，和没配过的节点一样
  const set = (t: Tri) => p.onChange(t === 'on' ? true : t === 'off' ? false : undefined)
  const radio = useRadioGroup(TRI, state, set)
  const hintId = `${p.id}-hint`
  return (
    <>
      {/* 字段标签的 for 指不到一组按钮，组名另写在 aria-label 上 */}
      <div id={p.id} role="radiogroup" aria-label={label} data-tri-state={state}
           aria-describedby={[hintId, p.describedBy].filter(Boolean).join(' ')}
           className="inline-flex max-w-full flex-wrap rounded-md border p-px">
        {TRI.map((t) => (
          <button key={t} type="button" {...radio(t)} disabled={p.disabled} data-tri={t} onClick={() => set(t)}
                  className={clsx('rounded px-2 py-0.5 text-2xs', state === t ? 'bg-accent-soft text-fg' : 'text-faint hover:text-dim')}>
            {text[t]}
          </button>
        ))}
      </div>
      <div id={hintId} className="mt-1 text-2xs leading-snug text-faint" data-tri-hint={state}>{text.hint[state]}</div>
    </>
  )
}

/** 跟随设置 / 开启 / 关闭：配置里没有这一项（或 null）/ true / false */
export function triStateField(def: { key: string; label: string; text: TriStateText; advanced?: boolean }): FieldDef {
  return {
    key: def.key, label: def.label, type: 'select', advanced: def.advanced,
    render: (p) => <TriState p={p} label={def.label} text={def.text} />,
  }
}

// ---------------------------------------------------------------- 核查模型

export interface VerifyModelText {
  /** 接入、模型都留空 */
  providerFollow: string
  /** 接入留空、模型填了：服务端按模型名找接入，不再跟随设置 */
  providerFromModel: string
  providerMissing: (name: string) => string
  /** 接在已停用的接入名后面 */
  providerDisabled: string
  /** 模型框的占位：接入也没选 / 选了接入 */
  modelFollow: string
  modelDefault: string
  /** 取值链的说明，常显在模型框下面 */
  help: string
  byModel: (model: string, found: { name: string; enabled: boolean } | null | undefined, fallback: string | null | undefined) => string
  /** 和被核查的模型是同一个 */
  same: (model: string) => string
}

/** 被核查的那个模型：节点上明确写着的模型名；说不准（没写、跟随默认）时为 null，不提醒 */
export type SubjectModel = (nodeId: string, config: Record<string, any>, graph: { nodes: FlowNode[]; edges: Edge[] }) => string | null

function ProviderPick({ p, text }: { p: FieldRenderProps; text: VerifyModelText }) {
  const providers = useCatalog((s) => s.providers)
  const provider = typeof p.value === 'string' ? p.value : ''
  const model = typeof p.config.model === 'string' ? p.config.model.trim() : ''
  const known = provider ? providers.find((x) => x.name === provider) : undefined
  return (
    <select id={p.id} className="field" value={provider} disabled={p.disabled} aria-invalid={p.invalid || undefined}
            aria-describedby={p.describedBy} style={p.invalid ? { borderColor: 'var(--err)' } : undefined}
            onChange={(e) => p.onChange(e.target.value || undefined)}>
      <option value="">{model ? text.providerFromModel : text.providerFollow}</option>
      {/* 接入列表还没取回来时照写名字，免得落到第一项、看着像跟随设置 */}
      {provider && !known && <option value={provider}>{providers.length ? text.providerMissing(provider) : provider}</option>}
      {providers.map((x) => (
        <option key={x.id} value={x.name}>{x.name}{x.enabled ? '' : text.providerDisabled}</option>
      ))}
    </select>
  )
}

function ModelInput({ p, text, subject }: { p: FieldRenderProps; text: VerifyModelText; subject?: SubjectModel }) {
  const providers = useCatalog((s) => s.providers)
  // 选择器只返回一个字符串：画布上别的节点改动不让这一栏重渲染
  const against = useStudio((s) => (subject ? subject(p.nodeId, p.config, s) : null))
  const provider = typeof p.config.provider === 'string' ? p.config.provider : ''
  const model = typeof p.value === 'string' ? p.value : ''
  const name = model.trim()
  const pv = provider ? providers.find((x) => x.name === provider) : undefined
  const models = pv ? (pv.models ?? []).map((m) => m.id) : [...new Set(modelOptions(providers).map((o) => o.value))]
  // 只填了模型：这一级写了就整组用节点上的（verify_model_spec 不跨级拼），服务端按模型名找接入
  const byModel = !provider && !!name
  const owner = byModel ? providerOfModel(providers, name) : undefined
  const enabled = providers.filter((x) => x.enabled)
  const fallback = enabled.find((x) => x.kind !== 'mock') ?? enabled[0]
  const byModelHint = byModel && providers.length > 0
    ? text.byModel(name, owner && { name: owner.provider.name, enabled: owner.enabled }, fallback?.name) : ''
  const same = !!name && !!against && name === against
  return (
    <>
      <input id={p.id} className="field mono text-xs" list={`${p.id}-models`} value={model} spellCheck={false}
             disabled={p.disabled} aria-invalid={p.invalid || undefined} aria-describedby={p.describedBy}
             placeholder={provider ? text.modelDefault : text.modelFollow}
             style={p.invalid ? { borderColor: 'var(--err)' } : undefined}
             onChange={(e) => p.onChange(e.target.value.trim() ? e.target.value : undefined)} />
      <datalist id={`${p.id}-models`}>{models.map((m) => <option key={m} value={m} />)}</datalist>
      {byModelHint && (
        <div className="mt-1 text-2xs leading-snug" data-verify-by-model={owner ? (owner.enabled ? 'found' : 'disabled') : 'default'}
             style={owner?.enabled ? undefined : { color: 'var(--st-waiting)' }}>
          <span className={owner?.enabled ? 'text-faint' : undefined}>{byModelHint}</span>
        </div>
      )}
      {same && (
        <div className="mt-1 text-2xs leading-snug" style={{ color: 'var(--st-waiting)' }} data-verify-same-model="">
          {text.same(name)}
        </div>
      )}
      <div className="mt-1 text-2xs leading-snug text-faint">{text.help}</div>
    </>
  )
}

/**
 * 核查模型：接入、模型两个字段（配置里的 provider、model）。都留空时用设置里的核查模型；写了任一项就整组以节点为准。
 * subject 给出被核查的那个模型，相同时提醒（等于自己审自己）
 */
export function verifyModelFields(def: {
  labels: { provider: string; model: string }; text: VerifyModelText; subject?: SubjectModel; advanced?: boolean
}): FieldDef[] {
  return [
    {
      key: 'provider', label: def.labels.provider, type: 'select', advanced: def.advanced,
      render: (p) => <ProviderPick p={p} text={def.text} />,
    },
    {
      key: 'model', label: def.labels.model, type: 'model', advanced: def.advanced,
      render: (p) => <ModelInput p={p} text={def.text} subject={def.subject} />,
    },
  ]
}

/**
 * 被核查节点上写着的模型：refKey 是本节点配置里指向它的那一项（反方审稿的 report），没写时取上游唯一的那一个 nodeType
 */
export function upstreamModel(refKey: string, nodeType: NodeType): SubjectModel {
  return (nodeId, config, { nodes, edges }) => {
    const model = targetNode(nodeId, config[refKey], nodeType, nodes, edges)?.data?.config?.model
    return typeof model === 'string' && model.trim() ? model.trim() : null
  }
}
