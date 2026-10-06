import { useMemo } from 'react'
import type { ReactNode } from 'react'
import { X } from 'lucide-react'
import clsx from 'clsx'
import { useStudio } from '../store/studio'
import { DUAL_CHECK_TEXT } from '../lib/terms'
import type { FieldRenderProps, NodeSummaryContext } from './nodeDefs'
import {
  ModelInput, NodePick, ProviderPick, TriState, resolvePick, upstreamOf, useVerifyDefaults,
  type CanvasEdge, type CanvasNode, type NodePickText, type TriStateText, type VerifyModelText,
} from './verifyFields'

/*
 * 双盲复算节点的检查器控件（nodeDefs 里 dual_check 各字段的 render）和节点卡片的摘要。
 *
 * 选口径卡、盲算模型、启用三态用核查类节点共用的控件（verifyFields.tsx，反方审稿也用它们），这里只给文案；双盲复算
 * 特有的选指标、口径说明、相对误差在这里。口径卡、要复算的指标、口径说明都得看画布上别的节点：从 studio store 读，
 * 不往检查器的 props 里加字段。每个控件只写自己那个键（FieldRenderProps.onChange）；写回 undefined 就是「留空 = 跟随
 * 默认」，配置里不留空值。后端认的键和缺省见 types.ts 的 DualCheckConfig
 */

const T = DUAL_CHECK_TEXT

const isRecord = (v: unknown): v is Record<string, any> => !!v && typeof v === 'object' && !Array.isArray(v)
const ids = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && !!x) : [])

/**
 * 实际复算哪张卡：写了 card 就是它（不在画布上、不是口径卡时 node 为 null；不在上游也照样是它，指标照列，选卡的地方
 * 另外提醒）；没写取上游的口径卡（schema.dual_check_card 的认法），上游只有一张时就是它，好几张时说不准，node 为 null、
 * 由调用方照实说
 */
function targetOf(config: Record<string, any>, id: string, nodes: readonly CanvasNode[], edges: readonly CanvasEdge[]) {
  const up = upstreamOf(id, edges)
  const upstream = nodes.filter((n) => up.has(n.id) && n.data.nodeType === 'metrics')
  const card = typeof config.card === 'string' && config.card ? config.card : ''
  const pick = resolvePick(id, card, 'metrics', 'upstream', nodes, edges)
  const node = pick.kind === 'target' || pick.kind === 'notUpstream' ? pick.node : null
  return { node, card, upstream }
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

/** 字段下面的一行提醒（写了说明的指标不在复算范围里之类），提醒色 */
function Warn({ children, attr }: { children: ReactNode; attr: string }) {
  return (
    <div className="mt-1 text-2xs leading-snug" style={{ color: 'var(--st-waiting)' }} {...{ [attr]: '' }}>{children}</div>
  )
}

// ---------------------------------------------------------------- 各字段

/**
 * 要复算的口径卡（共用的 NodePick）：上游的一组、其余的一组（选了不在上游的，提醒把它连到上游）；留空取上游的口径卡，
 * 上游有好几张又没选时写明要选（服务端校验会挡住发起运行）；写着的卡不在画布上、不是口径卡时照实列出来、说一句
 */
const CARD_TEXT: NodePickText = {
  auto: (only, count) => T.cardDefault(only, count),
  many: (n) => T.cardChoose(n),
  missing: (id) => T.cardGone(id),
  notUpstream: (label) => label,
  wrongType: (label) => T.cardWrongType(label),
  missingHint: T.cardMissingHint,
  notUpstreamHint: T.cardNotUpstream,
  wrongTypeHint: (type) => T.cardWrongTypeHint(type),
  groups: { upstream: T.cardUpstream, other: T.cardOther },
}
/** 一张口径卡在下拉里的叫法：节点名，口径名不同时附在后面 */
const cardOption = (n: CanvasNode) => T.cardOption(String(n.data.label || n.id), n.data.config?.caliber)

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
 * 盲算模型（共用的 ProviderPick、ModelInput）：留空跟随设置；只填了模型时接入按模型名找，留空那一项改叫「按模型名找
 * 接入」，并写明实际用哪个接入（verify_model_spec 哪一级写了就整组用哪一级，不跨级拼）。取值链的说明在字段的帮助里
 */
const MODEL_TEXT: VerifyModelText = {
  providerFollow: T.providerFollow, providerFromModel: T.providerFromModel, providerMissing: T.providerGone,
  providerDisabled: T.providerOff, modelFollow: T.modelFollow, modelDefault: T.modelOfProvider, byModel: T.byModel,
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

/**
 * 启用（共用的 TriState）：跟随设置 / 开启 / 关闭三态（null 或不写、true、false）。跟随设置时写回 undefined，不在配置里
 * 留 null；「跟随设置」后面写出设置里现在是开是关（每次打开检查器都重取）。各态的说明在字段的帮助里
 */
const ENABLED_TEXT: TriStateText = {
  follow: T.enabled.follow, on: T.enabled.on, off: T.enabled.off, followNow: (on) => T.enabledFollow(on),
}

/** nodeDefs 里 dual_check 各字段的 render */
export const dualCheckFields = {
  card: (p: FieldRenderProps) => (
    <NodePick p={p} fieldKey="card" nodeType="metrics" rule="upstream" text={CARD_TEXT} optionLabel={cardOption} nameOf={cardName} />
  ),
  metrics: (p: FieldRenderProps) => <MetricsField {...p} />,
  definitions: (p: FieldRenderProps) => <DefinitionsField {...p} />,
  provider: (p: FieldRenderProps) => <ProviderPick p={p} text={MODEL_TEXT} />,
  model: (p: FieldRenderProps) => <ModelInput p={p} fieldKey="model" text={MODEL_TEXT} />,
  tolerance: (p: FieldRenderProps) => <ToleranceField {...p} />,
  enabled: (p: FieldRenderProps) => (
    <TriState p={p} label={T.field.enabled} text={ENABLED_TEXT} setting={(v) => v.dual_check?.enabled} />
  ),
}
