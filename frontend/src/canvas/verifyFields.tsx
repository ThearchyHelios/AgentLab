import { useEffect, useMemo, useState } from 'react'
import clsx from 'clsx'
import { api } from '../api/client'
import { useRadioGroup } from '../components/ui'
import { nodeTypeLabel } from '../lib/terms'
import { modelOptions, providerOfModel, useCatalog } from '../store/catalog'
import { useStudio } from '../store/studio'
import type { NodeType, VerifySettings } from '../types'
import { fieldOfIssue } from './issues'
import type { FieldDef, FieldRenderProps } from './nodeDefs'

/**
 * 核查类节点（反方审稿、双盲复算）检查器里的几样通用控件，两个节点共用这一份（期 3 两个工作包各写过一份，期 4 合并）：
 * - NodePick / nodePickField：从画布上挑本节点上游的某一类节点（要审的报告、要复算的口径卡），留空按缺省取上游的那一个；
 * - TriState / triStateField：跟随设置 / 开启 / 关闭三态（配置里没有这一项、true、false）；
 * - ProviderPick、ModelInput / verifyModelFields：核查模型的接入加模型，写法同结论句裁判的模型选择；
 * - useVerifyDefaults：设置里「核查」一组的当前值（「跟随设置」后面写出现在是多少）。
 * 双盲复算特有的（选指标、口径说明、相对误差）留在 DualCheckFields.tsx。
 *
 * 文案一律由调用方传进来（各节点自己那一块的 *_TEXT），控件里不写死任何一句话：两个节点说法不同（反方模型 / 盲算模型），
 * 控件是同一个。读画布节点用 studio store，不往 FieldRenderProps 里加字段。
 *
 * 本地提示（上游有好几个、模型相同……）只在画布校验还没在这一项上报问题时画：校验的话 Inspector 会挂在字段下面，
 * 再画一句就是同一件事说两遍（同结论句裁判「校验也会报，报了就不重复说」）。校验没回来、校验失败时照画。
 */

/** 画布上的节点、连线只用得到这几项：检查器给的是 FlowNode / Edge，节点卡片的摘要给的是 NodeSummaryContext 里的 */
export interface CanvasNode { id: string; data: { nodeType: NodeType; label?: string; config?: Record<string, any> } }
export interface CanvasEdge { source: string; target: string }

/**
 * 画布校验在这个节点的这一项上报了问题没有。认法和 Inspector 把问题挂到字段下面时一样（fieldOfIssue），
 * 两边说的「这一项有问题」不会对不上
 */
function useReported(nodeId: string, key: string): boolean {
  return useStudio((s) => {
    const node = s.nodes.find((n) => n.id === nodeId)
    return s.issues.some((i) => i.node_id === nodeId && fieldOfIssue(i, node)?.key === key)
  })
}

/** 节点在下拉里的叫法：名字和 id 不同时把 id 带上（同 NodeRefsInput） */
function nodeLabel(n: CanvasNode): string {
  const label = String(n.data?.label || n.id)
  return label === n.id ? label : `${label}（${n.id}）`
}

/** 某个节点的全部上游（沿连线往回找），不含它自己。只认上游：下游的节点运行到这里时还没有产出 */
export function upstreamOf(nodeId: string, edges: readonly CanvasEdge[]): Set<string> {
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
 * 留空时怎么认，照各节点服务端的认法：
 * - 'upstream'：整个上游里恰好一个才取它（双盲复算的口径卡，schema.dual_check_card）；
 * - 'direct'：先看直接连着的，恰好一个就是它，否则再看整个上游（反方审稿的报告，schema.challenge_report：
 *   「初稿 → 终稿 → 反方审稿」审终稿，不让人在初稿、终稿之间选）
 */
export type PickRule = 'upstream' | 'direct'

/**
 * 配置里那一项按服务端的认法落到哪个节点上：target 是定下来的那一个；其余几种是定不下来的原因——写了的节点不存在、
 * 不是这一类、不在上游（先后同服务端），留空时上游一个都没有、不止一个
 */
export type PickState<N extends CanvasNode = CanvasNode> =
  | { kind: 'target'; node: N }
  | { kind: 'missing' }
  | { kind: 'wrongType'; node: N }
  | { kind: 'notUpstream'; node: N }
  | { kind: 'none' }
  | { kind: 'many'; count: number }

export function resolvePick<N extends CanvasNode>(
  nodeId: string, chosen: unknown, type: NodeType, rule: PickRule, nodes: readonly N[], edges: readonly CanvasEdge[],
): PickState<N> {
  const up = upstreamOf(nodeId, edges)
  if (typeof chosen === 'string' && chosen) {
    const node = nodes.find((n) => n.id === chosen)
    if (!node) return { kind: 'missing' }
    if (node.data?.nodeType !== type) return { kind: 'wrongType', node }
    return up.has(node.id) ? { kind: 'target', node } : { kind: 'notUpstream', node }
  }
  if (rule === 'direct') {
    const direct = new Set(edges.filter((e) => e.target === nodeId).map((e) => e.source))
    const near = nodes.filter((n) => direct.has(n.id) && n.data?.nodeType === type)
    if (near.length === 1) return { kind: 'target', node: near[0] }
  }
  const candidates = nodes.filter((n) => up.has(n.id) && n.data?.nodeType === type)
  return candidates.length === 1 ? { kind: 'target', node: candidates[0] }
    : candidates.length ? { kind: 'many', count: candidates.length } : { kind: 'none' }
}

/** 这个节点实际要核查的是哪一个；定不下来（服务端这时报错、不核查）时返回 undefined */
export function targetNode<N extends CanvasNode>(
  nodeId: string, chosen: unknown, type: NodeType, rule: PickRule, nodes: readonly N[], edges: readonly CanvasEdge[],
): N | undefined {
  const pick = resolvePick(nodeId, chosen, type, rule, nodes, edges)
  return pick.kind === 'target' ? pick.node : undefined
}

// ---------------------------------------------------------------- 设置里的核查一组

/**
 * 设置里核查一组的值：「跟随设置」后面写出现在是开是关、相对误差的占位写出现在是多少。每次打开检查器都重取一次——去
 * 设置页关掉双盲复算、改了误差再回到画布，这里得是新值（不同于检查器的运行默认值，核查开关是常改的）。先用上次取到的
 * 画，免得闪一下；同一次打开里的几个字段共用一个请求；取不到就沿用上次的（没有就不写），不挡编辑
 */
let verifyPending: Promise<VerifySettings | null> | null = null
let verifyValue: VerifySettings | null | undefined
function fetchVerify(): Promise<VerifySettings | null> {
  verifyPending ??= api.settings.get().then(
    (s) => (verifyValue = s?.verify && typeof s.verify === 'object' ? s.verify as VerifySettings : null),
    () => verifyValue ?? null,
  ).finally(() => { verifyPending = null })
  return verifyPending
}
export function useVerifyDefaults(active = true): VerifySettings | null {
  const [value, setValue] = useState<VerifySettings | null>(verifyValue ?? null)
  useEffect(() => {
    // 用不上设置的控件（反方审稿的启用不写当前值）不取，打开检查器不多发一个请求
    if (!active) return
    let alive = true
    void fetchVerify().then((v) => { if (alive) setValue(v) })
    return () => { alive = false }
  }, [active])
  return value
}

// ---------------------------------------------------------------- 选画布上的节点

export interface NodePickText {
  /** 留空那一项：按缺省能认出是哪一个时给出它的名字；count 是上游这一类节点有几个 */
  auto: (only: string | null, count: number) => string
  /** 上游一个都没有时下面那句提示；不给就不提示（留空那一项已经说了） */
  none?: string
  /** 上游不止一个、又没有选 */
  many: (n: number) => string
  /** 选中的节点已不在画布上（下拉里那一项的叫法） */
  missing: (id: string) => string
  /** 选中的节点不在上游（下拉里那一项的叫法） */
  notUpstream: (label: string) => string
  /** 选中的节点不是这一类（下拉里那一项的叫法） */
  wrongType: (label: string) => string
  missingHint: string
  notUpstreamHint: string
  /** type 是选中节点的类型名（「输入」） */
  wrongTypeHint: (type: string) => string
  /**
   * 下拉里把上游的和画布上其余同类节点分两组列时，两组的标题（双盲复算的口径卡：选了不在上游的，提示把它连到上游）。
   * 不给时只列上游的
   */
  groups?: { upstream: string; other: string }
}

export interface NodePickOptions {
  fieldKey: string
  nodeType: NodeType
  rule: PickRule
  text: NodePickText
  /** 下拉里一个节点的叫法，缺省是「名字（id）」 */
  optionLabel?: (n: CanvasNode) => string
  /** 留空那一项里，按缺省认出的那一个的名字，缺省是节点名（没起名的用 id） */
  nameOf?: (n: CanvasNode) => string
}

const nodeName = (n: CanvasNode): string => String(n.data?.label || n.id)

export function NodePick({ p, fieldKey, nodeType, rule, text, optionLabel = nodeLabel, nameOf = nodeName }: NodePickOptions & { p: FieldRenderProps }) {
  const nodes = useStudio((s) => s.nodes)
  const edges = useStudio((s) => s.edges)
  const reported = useReported(p.nodeId, fieldKey)
  const value = typeof p.value === 'string' ? p.value : ''
  const { upstream, other } = useMemo(() => {
    const up = upstreamOf(p.nodeId, edges)
    const same = nodes.filter((n) => n.data?.nodeType === nodeType)
    return { upstream: same.filter((n) => up.has(n.id)), other: same.filter((n) => !up.has(n.id) && n.id !== p.nodeId) }
  }, [nodes, edges, p.nodeId, nodeType])
  const pick = useMemo(
    () => resolvePick(p.nodeId, value, nodeType, rule, nodes, edges), [p.nodeId, value, nodeType, rule, nodes, edges],
  )
  // 选中的节点被删了、不是这一类、挪到了下游：下拉里照实多列一项（选不回去），不让它看着像「未选择」。
  // 分组列出画布上其余同类节点时，不在上游的那个本来就在「其余」一组里，不再另列。校验已经报了的，这一项照样列
  // （看得出选的是什么），只是下面那句提示不再说
  const stale = pick.kind === 'missing' || pick.kind === 'wrongType' || (pick.kind === 'notUpstream' && !text.groups) ? pick : null
  const hint = reported ? ''
    : pick.kind === 'missing' ? text.missingHint
    : pick.kind === 'wrongType' ? text.wrongTypeHint(nodeTypeLabel(pick.node.data?.nodeType))
    : pick.kind === 'notUpstream' ? text.notUpstreamHint
    : pick.kind === 'none' ? text.none ?? ''
    : pick.kind === 'many' ? text.many(pick.count) : ''
  const hintId = `${p.id}-hint`
  const only = !value && pick.kind === 'target' ? nameOf(pick.node) : null
  const option = (n: CanvasNode) => <option key={n.id} value={n.id}>{optionLabel(n)}</option>
  return (
    <>
      <select id={p.id} className="field" value={value} disabled={p.disabled} aria-invalid={p.invalid || undefined}
              aria-describedby={[p.describedBy, hint ? hintId : ''].filter(Boolean).join(' ') || undefined}
              style={p.invalid || stale ? { borderColor: p.invalid ? 'var(--err)' : 'var(--warn)' } : undefined}
              onChange={(e) => p.onChange(e.target.value || undefined)}>
        <option value="">{text.auto(only, upstream.length)}</option>
        {text.groups ? (
          <>
            {upstream.length > 0 && <optgroup label={text.groups.upstream}>{upstream.map(option)}</optgroup>}
            {other.length > 0 && <optgroup label={text.groups.other}>{other.map(option)}</optgroup>}
          </>
        ) : upstream.map(option)}
        {stale && (
          <option value={value} disabled>
            {stale.kind === 'missing' ? text.missing(value)
              : stale.kind === 'wrongType' ? text.wrongType(optionLabel(stale.node)) : text.notUpstream(optionLabel(stale.node))}
          </option>
        )}
      </select>
      {hint && (
        <div id={hintId} className="mt-1 text-2xs leading-snug" data-node-pick-hint={pick.kind} style={{ color: 'var(--st-waiting)' }}>
          {hint}
        </div>
      )}
    </>
  )
}

/**
 * 从画布上挑本节点上游的某一类节点。配置里存节点 id；留空（写回 undefined，不留空字符串）时服务端按 rule 取缺省的那一个
 */
export function nodePickField(def: {
  key: string; label: string; nodeType: NodeType; rule?: PickRule; text: NodePickText; help?: string; advanced?: boolean
  optionLabel?: (n: CanvasNode) => string
}): FieldDef {
  return {
    key: def.key, label: def.label, type: 'select', help: def.help, advanced: def.advanced,
    render: (p) => <NodePick p={p} fieldKey={def.key} nodeType={def.nodeType} rule={def.rule ?? 'upstream'} text={def.text}
                             optionLabel={def.optionLabel} />,
  }
}

// ---------------------------------------------------------------- 三态开关

export interface TriStateText {
  follow: string
  on: string
  off: string
  /** 「跟随设置」那一项附上设置里现在是开是关（设置还没取到时 on 为 null / undefined）。不给就只写 follow */
  followNow?: (on: boolean | null | undefined) => string
  /** 选中各态时下面那句说明：跟随的是哪一项设置、关掉之后会怎样。不给就不写（说明在字段的帮助里） */
  hint?: { follow: string; on: string; off: string }
}

type Tri = 'follow' | 'on' | 'off'
const TRI: readonly Tri[] = ['follow', 'on', 'off']

export function TriState({ p, label, text, setting }: {
  p: FieldRenderProps; label: string; text: TriStateText
  /** 从设置的核查一组里取这一项现在的开关：给了才在「跟随设置」后面写出来 */
  setting?: (v: VerifySettings) => boolean | null | undefined
}) {
  // 只认真正的布尔：手写的 "false"、0 不当成关闭，按跟随设置显示（服务端校验会报出来）
  const state: Tri = p.value === true ? 'on' : p.value === false ? 'off' : 'follow'
  // 跟随设置写回 undefined：配置里不留这一项，和没配过的节点一样
  const set = (t: Tri) => p.onChange(t === 'on' ? true : t === 'off' ? false : undefined)
  const radio = useRadioGroup(TRI, state, set)
  const settings = useVerifyDefaults(!!setting)
  const now = setting && settings ? setting(settings) : undefined
  const hintId = `${p.id}-hint`
  return (
    <>
      {/* 字段标签的 for 指不到一组按钮，组名另写在 aria-label 上 */}
      <div id={p.id} role="radiogroup" aria-label={label} data-tri-state={state}
           aria-describedby={[text.hint ? hintId : '', p.describedBy].filter(Boolean).join(' ') || undefined}
           className="inline-flex max-w-full flex-wrap gap-0.5 rounded-md border p-0.5">
        {TRI.map((t) => (
          <button key={t} type="button" {...radio(t)} disabled={p.disabled} data-tri={t} onClick={() => set(t)}
                  // 不折行：「跟随设置（当前开启）」比「开启」长得多，太窄时整段换到下一行
                  className={clsx('whitespace-nowrap rounded px-2 py-0.5 text-2xs transition-colors',
                    state === t ? 'bg-accent-soft font-medium text-fg' : 'text-dim hover:bg-hover')}>
            {t === 'follow' && text.followNow ? text.followNow(now) : text[t]}
          </button>
        ))}
      </div>
      {text.hint && <div id={hintId} className="mt-1 text-2xs leading-snug text-faint" data-tri-hint={state}>{text.hint[state]}</div>}
    </>
  )
}

/** 跟随设置 / 开启 / 关闭：配置里没有这一项（或 null）/ true / false */
export function triStateField(def: {
  key: string; label: string; text: TriStateText; advanced?: boolean; help?: string
  setting?: (v: VerifySettings) => boolean | null | undefined
}): FieldDef {
  return {
    key: def.key, label: def.label, type: 'select', advanced: def.advanced, help: def.help,
    render: (p) => <TriState p={p} label={def.label} text={def.text} setting={def.setting} />,
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
  /** 取值链的说明，常显在模型框下面。不给就不写（说明在字段的帮助里） */
  help?: string
  byModel: (model: string, found: { name: string; enabled: boolean } | null | undefined, fallback: string | null | undefined) => string
  /** 和被核查的模型是同一个（给了 subject 时用） */
  same?: (model: string) => string
}

/** 被核查的那个模型：节点上明确写着的模型名；说不准（没写、跟随默认）时为 null，不提醒 */
export type SubjectModel = (nodeId: string, config: Record<string, any>, graph: { nodes: readonly CanvasNode[]; edges: readonly CanvasEdge[] }) => string | null

export function ProviderPick({ p, text }: { p: FieldRenderProps; text: VerifyModelText }) {
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

export function ModelInput({ p, fieldKey, text, subject }: {
  p: FieldRenderProps; fieldKey: string; text: VerifyModelText; subject?: SubjectModel
}) {
  const providers = useCatalog((s) => s.providers)
  // 选择器只返回一个字符串：画布上别的节点改动不让这一栏重渲染
  const against = useStudio((s) => (subject ? subject(p.nodeId, p.config, s) : null))
  // 校验也会报模型相同（challenge.same_model 之类），报了就不重复说
  const reported = useReported(p.nodeId, fieldKey)
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
  const same = !!name && !!against && name === against && !reported && !!text.same
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
          {text.same!(name)}
        </div>
      )}
      {text.help && <div className="mt-1 text-2xs leading-snug text-faint">{text.help}</div>}
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
      render: (p) => <ModelInput p={p} fieldKey="model" text={def.text} subject={def.subject} />,
    },
  ]
}

/**
 * 被核查节点上写着的模型：refKey 是本节点配置里指向它的那一项（反方审稿的 report），没写时按 rule 认缺省的那一个
 * （和 nodePickField 用同一个 rule，两处认的是同一个节点）。定不下来时服务端只报选报告的错、不比模型，这里也不提醒
 */
export function upstreamModel(refKey: string, nodeType: NodeType, rule: PickRule = 'upstream'): SubjectModel {
  return (nodeId, config, { nodes, edges }) => {
    const model = targetNode(nodeId, config[refKey], nodeType, rule, nodes, edges)?.data?.config?.model
    return typeof model === 'string' && model.trim() ? model.trim() : null
  }
}
