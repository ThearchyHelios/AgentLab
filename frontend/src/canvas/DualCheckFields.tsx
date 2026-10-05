import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { X } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { useStudio } from '../store/studio'
import { modelOptions, providerOfModel, useCatalog } from '../store/catalog'
import { useRadioGroup } from '../components/ui'
import { DUAL_CHECK_TEXT } from '../lib/terms'
import type { VerifySettings } from '../types'
import type { FieldRenderProps, NodeSummaryContext } from './nodeDefs'

/*
 * 双盲复算节点的检查器控件（nodeDefs 里 dual_check 各字段的 render）和节点卡片的摘要。
 *
 * 口径卡、要复算的指标、口径说明都得看画布上别的节点：从 studio store 读，不往检查器的 props 里加字段。
 * 每个控件只写自己那个键（FieldRenderProps.onChange）；写回 undefined 就是「留空 = 跟随默认」，配置里不留空值。
 * 后端认的键和缺省见 types.ts 的 DualCheckConfig
 */

const T = DUAL_CHECK_TEXT

type CanvasNode = NodeSummaryContext['nodes'][number]
type CanvasEdge = NodeSummaryContext['edges'][number]

const isRecord = (v: unknown): v is Record<string, any> => !!v && typeof v === 'object' && !Array.isArray(v)
const ids = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && !!x) : [])

/** 某个节点的全部上游（沿连线往回走），不含它自己 */
function upstreamOf(id: string, edges: readonly CanvasEdge[]): Set<string> {
  const parents = new Map<string, string[]>()
  for (const e of edges) parents.set(e.target, [...(parents.get(e.target) ?? []), e.source])
  const seen = new Set<string>()
  const stack = [...(parents.get(id) ?? [])]
  while (stack.length) {
    const cur = stack.pop()!
    if (seen.has(cur) || cur === id) continue
    seen.add(cur)
    stack.push(...(parents.get(cur) ?? []))
  }
  return seen
}

/** 画布上的口径卡：本节点上游的在前（复算只能复算它们），其余的另列 */
function cardsOf(id: string, nodes: readonly CanvasNode[], edges: readonly CanvasEdge[]) {
  const up = upstreamOf(id, edges)
  const cards = nodes.filter((n) => n.data.nodeType === 'metrics')
  return { upstream: cards.filter((n) => up.has(n.id)), other: cards.filter((n) => !up.has(n.id)) }
}

/**
 * 实际复算哪张卡：写了 card 就是它（不在画布上时 node 为 null）；没写取上游的口径卡，上游只有一张时就是它，
 * 好几张时说不准，node 为 null、由调用方照实说
 */
function targetOf(config: Record<string, any>, id: string, nodes: readonly CanvasNode[], edges: readonly CanvasEdge[]) {
  const { upstream } = cardsOf(id, nodes, edges)
  const card = typeof config.card === 'string' && config.card ? config.card : ''
  if (card) return { node: nodes.find((n) => n.id === card && n.data.nodeType === 'metrics') ?? null, card, upstream }
  return { node: upstream.length === 1 ? upstream[0] : null, card: '', upstream }
}

/** 一张口径卡在摘要里的名字：节点名，没起名的用口径名，再没有用 id */
const cardName = (n: CanvasNode): string =>
  String(n.data.label || n.data.config?.caliber || n.id)

/** 口径卡里定义的指标；引用别的工作流发布的口径（caliber_from）时指标运行时才知道，返回 null */
function metricsOf(n: CanvasNode): { id: string; name: string }[] | null {
  const cfg = n.data.config ?? {}
  if (isRecord(cfg.caliber_from)) return null
  return (Array.isArray(cfg.metrics) ? cfg.metrics : [])
    .filter((m: any) => isRecord(m) && typeof m.id === 'string' && m.id)
    .map((m: any) => ({ id: String(m.id), name: typeof m.name === 'string' ? m.name : '' }))
}

/**
 * 节点卡片的摘要：复算哪张口径卡、几个指标（NodeDef.summary）。NodeCard 在 studio store 的选择器里调它，
 * 每条事件都会跑一遍：只做几次数组查找，返回字符串
 */
export function dualCheckSummary({ id, config, nodes, edges }: NodeSummaryContext): string {
  const S = T.summary
  const { node, card, upstream } = targetOf(config, id, nodes, edges)
  const picked = ids(config.metrics)
  let text: string
  if (node) {
    const all = metricsOf(node)
    text = S.card(cardName(node), picked.length ? picked.length : null, all ? all.length : null)
  } else if (card) {
    text = S.card(card, picked.length ? picked.length : null, null)
  } else {
    text = upstream.length > 1 ? S.many(upstream.length) : S.noCard
  }
  return config.enabled === false ? `${text} · ${S.off}` : text
}

// ---------------------------------------------------------------- 设置里的默认值

/**
 * 设置里核查一组的值：相对误差、启用的「跟随设置」要写出现在是多少。每次打开检查器都重取一次——去设置页关掉双盲
 * 复算、改了误差再回到画布，这里得是新值（不同于检查器的运行默认值，核查开关是常改的）。先用上次取到的画，免得
 * 闪一下；同一次打开里的几个字段共用一个请求；取不到就沿用上次的（没有就不写），不挡编辑
 */
let verifyPending: Promise<VerifySettings | null> | null = null
let verifyValue: VerifySettings | null | undefined
function fetchVerify(): Promise<VerifySettings | null> {
  verifyPending ??= api.settings.get().then(
    (s) => (verifyValue = isRecord(s?.verify) ? s.verify as VerifySettings : null),
    () => verifyValue ?? null,
  ).finally(() => { verifyPending = null })
  return verifyPending
}
function useVerifyDefaults(): VerifySettings | null {
  const [value, setValue] = useState<VerifySettings | null>(verifyValue ?? null)
  useEffect(() => {
    let alive = true
    void fetchVerify().then((v) => { if (alive) setValue(v) })
    return () => { alive = false }
  }, [])
  return value
}

/** 字段下面的一行提醒（所选口径卡不在上游之类），提醒色 */
function Warn({ children, attr }: { children: ReactNode; attr: string }) {
  return (
    <div className="mt-1 text-2xs leading-snug" style={{ color: 'var(--st-waiting)' }} {...{ [attr]: '' }}>{children}</div>
  )
}

// ---------------------------------------------------------------- 各字段

/** 要复算的口径卡：下拉选画布上的口径卡，上游的一组、其余的一组；留空取上游的口径卡 */
function CardField({ id, nodeId, value, invalid, describedBy, onChange }: FieldRenderProps) {
  const nodes = useStudio((s) => s.nodes)
  const edges = useStudio((s) => s.edges)
  const { upstream, other } = useMemo(() => cardsOf(nodeId, nodes, edges), [nodeId, nodes, edges])
  const current = typeof value === 'string' ? value : ''
  const known = [...upstream, ...other].some((n) => n.id === current)
  const option = (n: CanvasNode) => (
    <option key={n.id} value={n.id}>{T.cardOption(String(n.data.label || n.id), n.data.config?.caliber)}</option>
  )
  return (
    <>
      <select id={id} className="field" value={current} aria-invalid={invalid || undefined} aria-describedby={describedBy}
              data-dual-field="card" onChange={(e) => onChange(e.target.value || undefined)}>
        <option value="">{T.cardDefault(upstream.length === 1 ? cardName(upstream[0]) : null, upstream.length)}</option>
        {upstream.length > 0 && <optgroup label={T.cardUpstream}>{upstream.map(option)}</optgroup>}
        {other.length > 0 && <optgroup label={T.cardOther}>{other.map(option)}</optgroup>}
        {/* 写着的卡已经不在画布上：照实列出来（不让它看着像「默认」），下面再说一句 */}
        {current && !known && <option value={current}>{T.cardGone(current)}</option>}
      </select>
      {/* 上游有好几张又没选：服务端校验会挡住发起运行，用提醒色写明要选 */}
      {!current && upstream.length > 1 && <Warn attr="data-dual-card-warn">{T.cardChoose(upstream.length)}</Warn>}
      {current && known && !upstream.some((n) => n.id === current) && <Warn attr="data-dual-card-warn">{T.cardNotUpstream}</Warn>}
      {current && !known && <Warn attr="data-dual-card-warn">{T.cardMissing(current)}</Warn>}
    </>
  )
}

/**
 * 复算的指标：所选口径卡的指标逐个勾。没写（全部勾着）是缺省——复算输入来自查询的全部指标；全部勾回来时写回
 * undefined，配置里不留一份和缺省等价的清单。最后一个不让取消：一个都不勾和缺省分不清
 */
function MetricsField({ id, nodeId, value, config, describedBy, onChange }: FieldRenderProps) {
  const nodes = useStudio((s) => s.nodes)
  const edges = useStudio((s) => s.edges)
  const target = useMemo(() => targetOf(config, nodeId, nodes, edges), [config, nodeId, nodes, edges])
  const picked = ids(value)
  const metrics = target.node ? metricsOf(target.node) : undefined
  if (!target.node || metrics === undefined) {
    return (
      <div id={id} className="text-2xs leading-snug text-faint" aria-describedby={describedBy} data-dual-metrics="none">
        {target.upstream.length > 1 && !target.card ? T.cardChoose(target.upstream.length) : T.metricsNoCard}
      </div>
    )
  }
  if (metrics === null || !metrics.length) {
    return (
      <div id={id} aria-describedby={describedBy} data-dual-metrics={metrics === null ? 'pinned' : 'empty'}>
        <div className="text-2xs leading-snug text-faint">{metrics === null ? T.metricsPinned : T.metricsNone}</div>
        {picked.length > 0 && (
          <button type="button" className="btn btn-xs mt-1" onClick={() => onChange(undefined)}>{T.metricsClear}</button>
        )}
      </div>
    )
  }
  const known = new Set(metrics.map((m) => m.id))
  const valid = picked.filter((p) => known.has(p))
  const stale = picked.filter((p) => !known.has(p))
  const all = !picked.length
  const checked = new Set(all ? metrics.map((m) => m.id) : valid)
  const write = (next: Set<string>) => {
    const ordered = metrics.map((m) => m.id).filter((m) => next.has(m))
    onChange(ordered.length === metrics.length ? undefined : ordered)
  }
  return (
    <div id={id} role="group" aria-label={T.field.metrics} aria-describedby={describedBy} data-dual-metrics="list">
      <div className="space-y-0.5 rounded border px-2 py-1.5">
        {metrics.map((m) => {
          const on = checked.has(m.id)
          const last = on && checked.size === 1
          return (
            <label key={m.id} className={clsx('flex items-center gap-2 py-0.5 text-xs', last ? 'cursor-not-allowed' : 'cursor-pointer')}
                   title={last ? T.metricsLast : undefined} data-dual-metric-option={m.id}>
              <input type="checkbox" className="accent-[var(--accent)]" checked={on} disabled={last}
                     onChange={(e) => {
                       const next = new Set(checked)
                       if (e.target.checked) next.add(m.id)
                       else next.delete(m.id)
                       write(next)
                     }} />
              <span className="min-w-0 flex-1 truncate">{T.metricOption(m.name, m.id)}</span>
            </label>
          )
        })}
      </div>
      <div className="mt-1 text-2xs leading-snug text-faint" data-dual-metrics-state={all ? 'default' : 'picked'}>
        {all ? T.metricsDefault : T.metricsPicked(checked.size, metrics.length)}
      </div>
      {stale.length > 0 && (
        <Warn attr="data-dual-metrics-stale">
          {T.metricsStale(stale)}
          <button type="button" className="btn btn-xs ml-1 align-baseline" onClick={() => onChange(valid.length ? valid : undefined)}>
            {T.metricsClear}
          </button>
        </Warn>
      )}
    </div>
  )
}

/**
 * 口径说明：要复算的每个指标一段，给盲算模型看的业务定义（它看不到原流程的 SQL）。存成 {指标 id: 说明}，
 * 清空的那一项去掉，全清空写回 undefined。已经写了、指标却不在复算范围里的照样列出来，好让人看见并删掉
 */
function DefinitionsField({ id, nodeId, value, config, describedBy, onChange }: FieldRenderProps) {
  const nodes = useStudio((s) => s.nodes)
  const edges = useStudio((s) => s.edges)
  const target = useMemo(() => targetOf(config, nodeId, nodes, edges), [config, nodeId, nodes, edges])
  const defs: Record<string, string> = isRecord(value)
    ? Object.fromEntries(Object.entries(value).filter(([, v]) => typeof v === 'string')) as Record<string, string> : {}
  const metrics = target.node ? metricsOf(target.node) : undefined
  const picked = ids(config.metrics)
  const rows = metrics ? (picked.length ? metrics.filter((m) => picked.includes(m.id)) : metrics) : []
  const gone = Object.keys(defs).filter((k) => !rows.some((r) => r.id === k))
  const set = (metric: string, text: string | null) => {
    const next = { ...defs }
    if (text != null && text.trim()) next[metric] = text
    else delete next[metric]
    onChange(Object.keys(next).length ? next : undefined)
  }
  if (!rows.length && !gone.length) {
    return (
      <div id={id} className="text-2xs leading-snug text-faint" aria-describedby={describedBy} data-dual-definitions="none">
        {metrics === null ? T.definitionsPinned : T.definitionsNone}
      </div>
    )
  }
  return (
    <div id={id} role="group" aria-label={T.field.definitions} aria-describedby={describedBy} className="space-y-1.5"
         data-dual-definitions="list">
      {rows.map((m) => (
        <div key={m.id} data-dual-definition={m.id}>
          <label className="mb-0.5 block truncate text-2xs text-dim" htmlFor={`${id}-${m.id}`}>{T.metricOption(m.name, m.id)}</label>
          <textarea id={`${id}-${m.id}`} className="field text-xs" rows={2} value={defs[m.id] ?? ''}
                    placeholder={T.definitionPlaceholder(m.name || m.id)} onChange={(e) => set(m.id, e.target.value)} />
        </div>
      ))}
      {gone.map((k) => (
        <Warn key={k} attr="data-dual-definition-gone">
          <span className="flex items-start gap-1">
            <span className="min-w-0 flex-1">{T.definitionGone(k)}</span>
            <button type="button" className="shrink-0 rounded p-0.5 hover:bg-hover" aria-label={`${T.definitionRemove}：${k}`}
                    title={T.definitionRemove} onClick={() => set(k, null)}>
              <X size={11} aria-hidden />
            </button>
          </span>
        </Warn>
      ))}
    </div>
  )
}

/**
 * 盲算模型 · 接入。照报告节点「结论句裁判」的写法：留空跟随设置；只填了模型时接入按模型名找，留空那一项改叫
 * 「按模型名找接入」（verify_model_spec 哪一级写了就整组用哪一级，不跨级拼）
 */
function ProviderField({ id, value, config, invalid, describedBy, onChange }: FieldRenderProps) {
  const providers = useCatalog((s) => s.providers)
  const provider = typeof value === 'string' ? value : ''
  const known = providers.some((p) => p.name === provider)
  const byModel = !provider && typeof config.model === 'string' && !!config.model.trim()
  return (
    <select id={id} className="field" value={provider} aria-invalid={invalid || undefined} aria-describedby={describedBy}
            data-dual-field="provider" onChange={(e) => onChange(e.target.value || undefined)}>
      <option value="">{byModel ? T.providerFromModel : T.providerFollow}</option>
      {provider && !known && providers.length > 0 && <option value={provider}>{T.providerGone(provider)}</option>}
      {providers.map((p) => <option key={p.id} value={p.name}>{p.name}{p.enabled ? '' : T.providerOff}</option>)}
    </select>
  )
}

/** 盲算模型 · 模型：可以手填，下拉给所选接入（没选接入时给全部启用接入）的模型；只填了模型时写明实际用哪个接入 */
function ModelField({ id, value, config, invalid, describedBy, onChange }: FieldRenderProps) {
  const providers = useCatalog((s) => s.providers)
  const provider = typeof config.provider === 'string' ? config.provider : ''
  const pv = provider ? providers.find((p) => p.name === provider) : undefined
  const model = typeof value === 'string' ? value : ''
  const byModel = !provider && !!model.trim()
  const owner = byModel ? providerOfModel(providers, model.trim()) : undefined
  const enabled = providers.filter((p) => p.enabled)
  const fallback = enabled.find((p) => p.kind !== 'mock') ?? enabled[0]
  const hint = byModel && providers.length > 0
    ? T.byModel(model.trim(), owner && { name: owner.provider.name, enabled: owner.enabled }, fallback?.name) : ''
  const models = pv ? (pv.models ?? []).map((m) => m.id) : [...new Set(modelOptions(providers).map((o) => o.value))]
  return (
    <>
      <input id={id} className="field mono text-xs" list={`${id}-models`} value={model} spellCheck={false}
             placeholder={provider ? T.modelOfProvider : T.modelFollow} aria-invalid={invalid || undefined}
             aria-describedby={describedBy} data-dual-field="model"
             onChange={(e) => onChange(e.target.value.trim() ? e.target.value : undefined)} />
      <datalist id={`${id}-models`}>{models.map((m) => <option key={m} value={m} />)}</datalist>
      {hint && (
        <div className="mt-1 text-2xs leading-snug" data-dual-by-model={owner ? (owner.enabled ? 'found' : 'disabled') : 'default'}
             style={owner?.enabled ? undefined : { color: 'var(--st-waiting)' }}>
          <span className={owner?.enabled ? 'text-faint' : undefined}>{hint}</span>
        </div>
      )}
    </>
  )
}

/** 允许的相对误差：留空跟随设置，占位里写出设置里眼下是多少；负数标出来（服务端也不收） */
function ToleranceField({ id, value, invalid, describedBy, onChange }: FieldRenderProps) {
  const settings = useVerifyDefaults()
  const bad = typeof value === 'number' && (!Number.isFinite(value) || value < 0)
  return (
    <>
      <input id={id} className="field tnum" type="number" min={0} step="any" value={value ?? ''}
             placeholder={T.tolerancePlaceholder(settings?.dual_check?.tolerance)}
             aria-invalid={invalid || bad || undefined} aria-describedby={[describedBy, bad ? `${id}-bad` : ''].filter(Boolean).join(' ') || undefined}
             style={bad ? { borderColor: 'var(--err)' } : undefined} data-dual-field="tolerance"
             onChange={(e) => onChange(e.target.value === '' ? undefined : Number(e.target.value))} />
      {bad && <div id={`${id}-bad`} className="mt-1 text-2xs leading-snug" style={{ color: 'var(--err)' }}>{T.toleranceInvalid}</div>}
    </>
  )
}

const ENABLED = ['follow', 'on', 'off'] as const
type Enabled = typeof ENABLED[number]

/** 启用：跟随设置 / 开启 / 关闭三态（null 或不写、true、false）。跟随设置时写回 undefined，不在配置里留 null */
function EnabledField({ id, value, describedBy, onChange }: FieldRenderProps) {
  const settings = useVerifyDefaults()
  const current: Enabled = value === true ? 'on' : value === false ? 'off' : 'follow'
  const pick = (v: Enabled) => onChange(v === 'on' ? true : v === 'off' ? false : undefined)
  const radio = useRadioGroup(ENABLED, current, pick)
  return (
    <div id={id} role="radiogroup" aria-label={T.field.enabled} aria-describedby={describedBy}
         className="flex flex-wrap gap-0.5 rounded border p-0.5" data-dual-field="enabled">
      {ENABLED.map((v) => (
        <button key={v} type="button" {...radio(v)} data-value={v}
                // 按字数分宽度、不折行：「跟随设置（当前开启）」比「开启」长得多，平分会把它挤成两行；太窄时整段换到下一行
                className={clsx('flex-auto whitespace-nowrap rounded px-2 py-1 text-2xs transition-colors',
                  current === v ? 'font-medium text-fg' : 'text-dim hover:bg-hover')}
                style={current === v ? { background: 'var(--accent-soft)', boxShadow: 'inset 0 0 0 1px var(--accent)' } : undefined}
                onClick={() => pick(v)}>
          {v === 'follow' ? T.enabledFollow(settings?.dual_check?.enabled) : T.enabled[v]}
        </button>
      ))}
    </div>
  )
}

/** nodeDefs 里 dual_check 各字段的 render */
export const dualCheckFields = {
  card: (p: FieldRenderProps) => <CardField {...p} />,
  metrics: (p: FieldRenderProps) => <MetricsField {...p} />,
  definitions: (p: FieldRenderProps) => <DefinitionsField {...p} />,
  provider: (p: FieldRenderProps) => <ProviderField {...p} />,
  model: (p: FieldRenderProps) => <ModelField {...p} />,
  tolerance: (p: FieldRenderProps) => <ToleranceField {...p} />,
  enabled: (p: FieldRenderProps) => <EnabledField {...p} />,
}
