import { useEffect, useId, useMemo, useState } from 'react'
import type { CSSProperties } from 'react'
import { AlertTriangle, Ban, Check, ChevronRight } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { useCatalog } from '../store/catalog'
import { asDoc } from '../store/evidence'
import { useStudio } from '../store/studio'
import { Spinner, useToast } from '../components/ui'
import { segmentState } from '../lib/evidence'
import { NONE } from '../lib/format'
import { CATALOG_TABLE_FIELD_LABEL, DUAL_CHECK_TEXT } from '../lib/terms'
import { REJECT_ROUTE_TEXT, TASK_TEXT, rejectRouteOf } from '../lib/termsApp'
import { patchValueText } from './catalogPatch'
import { RationaleDoc } from './ChallengeSummary'
import type {
  Approval, DualCheckBlindInput, DualCheckCard, DualCheckChoice, DualCheckInput, DualCheckMetric, DualCheckReply,
  DualCheckSummaryItem, EvidenceDocData,
} from '../types'

const T = DUAL_CHECK_TEXT.approval
const CHOICES: DualCheckChoice[] = ['a', 'b', 'neither']
/** 不算具体差异的两种：兜底说明（other）和解析失败（parse）。服务端摘要的 fallback 也是这么算的 */
const NOT_CONCRETE = new Set(['other', 'parse'])
/** 能对上数据目录里一项事实的三种差异：时间列 → 业务日期，筛选条件 → 有效记录条件，聚合 → 去重规则 */
type CatalogAspect = 'time' | 'filter' | 'aggregate'
const CATALOG_ASPECTS = new Set<string>(['time', 'filter', 'aggregate'])

export interface DualCheckApprovalProps {
  /** payload.kind 为 'dual_check' 的审批：双盲复算两路对不上、交给人裁决 */
  approval: Approval
  /**
   * 提交之后谁来重新接上运行的事件流，同 ApprovalCard 的 onResolved：对话页、运行页各自接，没给就走画布的 attachRun。
   * 两处都接会开两条订阅，各收一份事件
   */
  onResolved?: (runId: string) => void | Promise<void>
  /**
   * 待办卡（run/TaskCard）给：裁决交给它提交（走待办接口），不再调审批接口、不刷新审批列表、不接事件流。没给时照旧
   */
  decide?: (approved: boolean, reply: DualCheckReply) => Promise<void>
}

/** 一个不一致的指标在卡上要画的东西：先由 payload.summary 填，完整对照（card_artifact）到了再补全 */
interface MetricView {
  id: string
  name: string
  original: string | null
  blind: string | null
  /** 已乘过 100；null 是原流程值为 0（约定），undefined 是没给 */
  deltaPct: number | null | undefined
  /** 具体差异，一条一句（兜底说明、解析失败不算） */
  items: string[]
  /** 一条具体差异都没有：卡上不列条目，默认展开对照，审核人只能对照原文判断 */
  fallbackOnly: boolean
  /** fallbackOnly 时写不写那句固定的「两条 SQL 的写法不同，未能归纳成具体差异」（一边没有 SQL 时不写，只写原因） */
  fallbackHead: boolean
  /** 兜底、解析失败的原话里固定那句以外的说法（比较超时、解析出错的位置……），附在下面 */
  notes: string[]
  /** 具体差异之外还有未能归纳的部分（服务端那句「另有未能归纳的差异」） */
  rest: string | null
  /** 两条 SQL 等价，差在取用的结果上 */
  equivalent: boolean
  /** 服务端没有预告时用：两边只差一处、又可能对得上数据目录的一项事实，审核人的选择可能被记进数据目录（见 catalogOf） */
  catalog: CatalogAspect | null
  /** 服务端预告的采用哪一边会往数据目录里写什么（见 previewOf）；有它就照它说，不再按 catalog 有条件地说 */
  preview: CatalogPreview | null
  full: DualCheckMetric | null
}

/** 盲算一边的输入，约定之外服务端另带核查 agent 组装的理由文档（期 4；老工件没有） */
type BlindInput = DualCheckBlindInput & { rationale_doc?: unknown }
/** 完整对照，约定之外服务端另带几个指标落到同一项上时每种选择组合的预告（期 4；老工件没有） */
type Card = DualCheckCard & { catalog_joint?: unknown }

/**
 * 写到数据目录的哪一项：数据源、表、目录项（business_date / valid_filter / dedup）。服务端写目录前按这三样归拢同一次
 * 裁决里的几个指标；where 是给人看的说法（「orders」（shop）的业务日期）
 */
interface Target { table: string; source: string | null; item: string; where: string }

/**
 * 服务端预告的一边。写的：写到哪、写成什么（value 是给人看的写法，raw 是原样的取值，和别的指标的说法比是不是同一个时
 * 用）。不写的：reason 是服务端的原话；认出了事实、只是被目录挡下的（已确认、驳回过、和已有条件合不起来）另带 target
 * 和本来要写的 raw——服务端照样会把它和落到同一项上的别的指标归拢，卡上据此认同一项上有谁
 */
type PreviewSide =
  | { write: true; target: Target; value: string; raw: unknown }
  | { write: false; target: Target | null; reason: string | null; raw?: unknown }
interface CatalogPreview { a: PreviewSide; b: PreviewSide }

/**
 * 对照工件里的 catalog_joint（dual_check_facts.preview_joint）：几个指标按各自的选择落到同一项上时，服务端按真写的办法
 * 算好的每个指标的结果。members 是落到这一项上的指标和各自的选择，outcomes 按指标 id 给结果
 */
interface JointOutcome { write: boolean; raw?: unknown; reason?: string | null }
interface JointEntry { source: string | null; table: string; item: string; members: Map<string, DualCheckChoice>; outcomes: Map<string, JointOutcome> }

const str = (v: unknown): string | undefined => (typeof v === 'string' && v ? v : undefined)
const isRecord = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)
const num = (v: unknown): number | undefined => (typeof v === 'number' && Number.isFinite(v) ? v : undefined)
const hasSql = (v: unknown): boolean => typeof v === 'string' && !!v.trim()

/** payload.summary 里认得出的项（老服务端、写坏了的项跳过，不让整张卡崩掉） */
function summaryOf(raw: unknown): DualCheckSummaryItem[] {
  return Array.isArray(raw)
    ? raw.filter((s): s is DualCheckSummaryItem => !!s && typeof s === 'object' && typeof s.metric_id === 'string' && !!s.metric_id)
    : []
}

/** 工件内容像不像一份对照：至少得有 metrics 数组，里面的项带 metric_id */
function cardOf(content: unknown): Card | null {
  if (!content || typeof content !== 'object' || !Array.isArray((content as Card).metrics)) return null
  const card = content as Card
  return { ...card, metrics: card.metrics.filter((m) => !!m && typeof m === 'object' && typeof m.metric_id === 'string') }
}

/**
 * 兜底、解析失败的原话 → standard：其中有那句固定的兜底说明（几个输入合并时前面带「输入「…」：」）；notes：其余的
 * 说法，去掉句末的「请对照原文」（对照就在下面），去重
 */
function notesOf(texts: string[]): { standard: boolean; notes: string[] } {
  const plain = [...new Set(texts.map((t) => t.replace(/，?请对照原文。?$/, '').trim()).filter(Boolean))]
  const isStandard = (t: string) => t.replace(/^输入「[^」]*」：/, '') === T.fallbackOnly
  return { standard: plain.some(isStandard), notes: plain.filter((t) => !isStandard(t)) }
}

/**
 * 服务端没有预告时（老服务端），审核人的选择可能被记进数据目录的形状：照服务端写目录的门槛（dual_check_facts.derive）
 * ——两条 SQL 不等价、没用兜底、差异恰好一条，而且是时间列、筛选条件或聚合。几个输入都对不上时节点把各自的差异合成
 * 一份（每个输入至少一条），所以「恰好一条」也就是只有一个输入对不上。过了这道门槛也不一定写：只有一边按时间筛、
 * 多出的条件是指标自己的范围、采用了不去重的一边都不写，同一次裁决里几个指标给同一张表记的业务日期、去重规则不同时
 * 一项都不写，这些卡上看不出来（差异条目只有方面和原话，没有预告也认不出写到哪张表），所以只说必要条件，并把不写的
 * 几种照实说出来（DUAL_CHECK_TEXT.approval.catalog）
 */
function catalogOf(metric: DualCheckMetric, items: { aspect: string; text: string }[]): CatalogAspect | null {
  if (metric.diff?.equivalent === true || metric.diff?.fallback === true || items.length !== 1) return null
  return CATALOG_ASPECTS.has(items[0].aspect) ? items[0].aspect as CatalogAspect : null
}

/** 预告里写到哪：得有表、认得出的目录项；认不出为 null */
function targetOf(s: Record<string, unknown>): Target | null {
  const table = str(s.table)
  const item = str(s.item)
  if (!table || !item || !Object.hasOwn(CATALOG_TABLE_FIELD_LABEL, item)) return null
  const source = str(s.source) ?? null
  const label = CATALOG_TABLE_FIELD_LABEL[item as keyof typeof CATALOG_TABLE_FIELD_LABEL]
  return { table, source, item, where: DUAL_CHECK_TEXT.log.catalogItem(table, source ?? undefined, label) }
}

/** 预告的一边：写的得有写到哪和取值；不写的有没有写到哪都认（老服务端只给原因）；认不出返回 undefined */
function previewSideOf(raw: unknown): PreviewSide | undefined {
  if (!isRecord(raw)) return undefined
  if (raw.write === false) return { write: false, target: targetOf(raw), reason: str(raw.reason) ?? null, raw: raw.value }
  const target = targetOf(raw)
  if (raw.write !== true || !target || raw.value == null || raw.value === '') return undefined
  return { write: true, target, value: patchValueText(target.item, raw.value), raw: raw.value }
}

/**
 * payload.summary[].catalog（约定之外、服务端另带的预告）：采用原流程、采用盲算各会往数据目录里写什么，和裁决后真写的
 * 是同一套判断。两边有一边认不出就整份不认，卡上回到按差异形状有条件地说（catalogOf）——宁可少说，不替服务端编
 */
function previewOf(raw: unknown): CatalogPreview | null {
  if (!raw || typeof raw !== 'object') return null
  const a = previewSideOf((raw as Record<string, unknown>).a)
  const b = previewSideOf((raw as Record<string, unknown>).b)
  return a && b ? { a, b } : null
}

/** 是不是同一个数据源、同一张表的同一项（服务端写目录前按这三样归拢） */
const sameTarget = (a: Pick<Target, 'table' | 'source' | 'item'> | null | undefined,
  b: Pick<Target, 'table' | 'source' | 'item'> | null | undefined): boolean =>
  !!a && !!b && a.table === b.table && a.source === b.source && a.item === b.item

/**
 * 对照工件里的 catalog_joint：认不出的条目跳过（宁可这一项回到有条件地说，不替服务端编）。每条要有写到哪、至少两个
 * 成员、每个成员都有结果
 */
function jointOf(card: Card | null): JointEntry[] {
  const raw = card?.catalog_joint
  if (!Array.isArray(raw)) return []
  const out: JointEntry[] = []
  for (const e of raw) {
    if (!isRecord(e) || !Array.isArray(e.members) || !isRecord(e.outcomes)) continue
    const table = str(e.table)
    const item = str(e.item)
    if (!table || !item) continue
    const members = new Map<string, DualCheckChoice>()
    for (const m of e.members) {
      if (isRecord(m) && str(m.metric_id) && (m.choice === 'a' || m.choice === 'b')) members.set(m.metric_id as string, m.choice)
    }
    const outcomes = new Map<string, JointOutcome>()
    for (const [id, o] of Object.entries(e.outcomes)) {
      if (isRecord(o) && typeof o.write === 'boolean') outcomes.set(id, { write: o.write, raw: o.value, reason: str(o.reason) ?? null })
    }
    if (members.size < 2 || [...members.keys()].some((id) => !outcomes.has(id))) continue
    out.push({ source: str(e.source) ?? null, table, item, members, outcomes })
  }
  return out
}
/** 同一项的两个说法是不是同一个（同服务端的 _same_fact）：业务日期只看哪一列，其余是文字、压掉多余的空白比 */
function sameFact(item: string, a: unknown, b: unknown): boolean {
  if (item === 'business_date') return isRecord(a) && isRecord(b) && a.column === b.column
  const flat = (s: string) => s.split(/\s+/).filter(Boolean).join(' ')
  return typeof a === 'string' && typeof b === 'string' && flat(a) === flat(b)
}

/** 预告里的一条：说什么、这一选会不会写，以及是怎么说出来的（data-* 标记，检查脚本据此核对） */
interface Clause {
  side: DualCheckChoice
  text: string
  write: boolean
  /** 句子里写明了是哪张表的哪一项：同一项的下一条可以省掉 */
  where: boolean
  /** 服务端说不写、给了原因 */
  reason: boolean
  /** 照服务端算好的合并结果说（catalog_joint） */
  joint: boolean
  /** 合并结果里和别的指标一起记入 */
  together: boolean
  /**
   * 没有算好的合并结果、按其余指标当前的选择推的：说法不同都不记（conflict）、有效记录条件合写（joined）、同一项上有被
   * 目录挡下的指标，写不写说不准、只说可能（pending）。inferred：这一条是推出来的（以上几种，以及推的路上照单个指标的
   * 预告说的），结尾换成「以时间线上的记录为准」的一般说法
   */
  conflict: boolean
  joined: boolean
  pending: boolean
  inferred: boolean
}

/**
 * 每个指标三个选择各一条预告。预告是每个指标单独算的，服务端写目录前却把同一次裁决里落到同一张表同一项上的说法归拢成
 * 一项（dual_check_facts._merged、_screen、_plan）。按其余指标当前的选择，找出这一选落到同一项上的指标（选了 a、b，
 * 那一边认出了写到哪的；被目录挡下的也算——服务端照样并进来）：
 * - 只有它自己：照它自己的预告说，会写的说会记为什么，不写的说原因；
 * - 还有别的：照对照工件里服务端为这几个成员算好的那一条（catalog_joint）说，会写的是合起来的值、写明和谁一起记，
 *   不写的照原因说；工件里没有（老服务端、组合太多、工件还没取到）时照服务端归拢的先后推（inferred），被目录挡下的
 *   同项指标也算进来——服务端先归拢、后对目录：
 *   - 业务日期、去重规则：有一个说法不同（不论它会不会被目录挡下），归拢就失败，都不记；说法都一样时结果和单个指标
 *     一样（同一个说法对同一份目录），照自己的预告说；
 *   - 有效记录条件：自己被挡下的，合起来也写不进去（驳回过的会被拿掉，别的原因连合起来的整句一起挡下），照自己的原因说；
 *     同项上有别的指标被挡下时说不准（挡下它的若是驳回，拿掉它照写；若是别的原因，这一边也一起挡下），只说可能；
 *     都会写、条件不同的说合成一句
 *   推出来的不是服务端算好的结果，结尾换成以时间线上的记录为准的一般说法
 */
function clausesOf(view: MetricView, views: MetricView[], choices: Record<string, DualCheckChoice>, joint: JointEntry[]): Clause[] {
  const P = T.catalogPreview
  const blank = {
    write: false, where: false, reason: false, joint: false, together: false, conflict: false, joined: false, pending: false, inferred: false,
  }
  const skip = (side: DualCheckChoice): Clause => ({ side, text: P.skip(T.choices[side]), ...blank })
  const out: Clause[] = []
  for (const side of ['a', 'b'] as const) {
    const mine = view.preview?.[side]
    if (!mine) { out.push(skip(side)); continue }
    const blocked = (reason: string | null | undefined): Clause =>
      (reason ? { side, text: P.blocked(T.choices[side], reason), ...blank, reason: true } : skip(side))
    // 前一条已经写明了是哪张表的哪一项，这一条省掉
    const short = side === 'b' && !!out[0]?.where && sameTarget(view.preview?.a.target, mine.target)
    const writing = (value: string, extra: Partial<Clause> = {}, tail = ''): Clause => ({
      side, ...blank, write: true, where: !short, ...extra,
      text: (short ? P.writeSame(T.choices[side], value) : P.write(T.choices[side], mine.target!.where, value)) + tail,
    })
    const peers = mine.target ? views.filter((o) => {
      const c = choices[o.id]
      return o.id !== view.id && (c === 'a' || c === 'b') && sameTarget(o.preview?.[c]?.target, mine.target)
    }) : []
    if (!peers.length) {
      out.push(mine.write ? writing(mine.value) : blocked(mine.reason))
      continue
    }
    const members = new Map<string, DualCheckChoice>([[view.id, side], ...peers.map((o) => [o.id, choices[o.id]] as [string, DualCheckChoice])])
    const entry = joint.find((e) => sameTarget(e, mine.target)
      && e.members.size === members.size && [...members].every(([id, c]) => e.members.get(id) === c))
    const outcome = entry?.outcomes.get(view.id)
    if (entry && outcome) {
      if (!outcome.write || outcome.raw == null || outcome.raw === '') {
        out.push({ ...blocked(outcome.reason), joint: true })
        continue
      }
      // 一起记入的：结果也是写的那几个成员（拿掉驳回的、没写进去的不算）
      const with_ = peers.filter((o) => entry.outcomes.get(o.id)?.write).map((o) => o.name)
      out.push(writing(patchValueText(entry.item, outcome.raw), { joint: true, together: with_.length > 0 },
        with_.length ? P.together(P.metricNames(with_)) : ''))
      continue
    }
    // 没有算好的合并结果：照服务端归拢的先后推。同项上的指标不论会写还是被挡下都带着本来要写的说法（raw），一起算
    const item = mine.target!.item
    const theirs = peers.map((o) => ({ name: o.name, side: o.preview![choices[o.id] as 'a' | 'b']! }))
    // 认不出本来要写什么（没带 raw）的不算说法不同：说不准，落到下面的「可能」里
    const differ = [...new Set(theirs.filter((t) => t.side.raw != null && !sameFact(item, t.side.raw, mine.raw)).map((t) => t.name))]
    const held = [...new Set(theirs.filter((t) => !t.side.write).map((t) => t.name))]
    const inferred = { inferred: true }
    if (differ.length && item !== 'valid_filter') {
      // 业务日期、去重规则说法不同：归拢这一步就不写了，轮不到对目录，挡下的那个也一样
      const names = P.metricNames(differ)
      out.push({ side, ...blank, ...inferred, conflict: true, where: !short,
        text: short ? P.conflictSame(T.choices.b, names) : P.conflict(T.choices[side], mine.target!.where, names) })
      continue
    }
    if (!mine.write) { out.push({ ...blocked(mine.reason), ...inferred }); continue }
    if (held.length) {
      const names = P.metricNames(held)
      out.push({ side, ...blank, ...inferred, pending: true, where: !short,
        text: short ? P.pendingSame(T.choices[side], mine.value, names) : P.pending(T.choices[side], mine.target!.where, mine.value, names) })
      continue
    }
    out.push(differ.length ? writing(mine.value, { ...inferred, joined: true }, P.joined(P.metricNames(differ))) : writing(mine.value, inferred))
  }
  out.push(skip('neither'))
  return out
}

function viewsOf(summary: DualCheckSummaryItem[], card: DualCheckCard | null): MetricView[] {
  const full = new Map((card?.metrics ?? []).map((m) => [m.metric_id, m]))
  // 先按摘要的顺序，工件里另有的（约定里不该有，有就照实列出来）跟在后面
  const ids = [...new Set([...summary.map((s) => s.metric_id), ...full.keys()])]
  return ids.map((id) => {
    const s = summary.find((x) => x.metric_id === id)
    const f = full.get(id) ?? null
    const base = {
      id,
      name: str(s?.name) ?? str(f?.name) ?? id,
      original: str(s?.original_rendered) ?? str(f?.original?.rendered) ?? null,
      blind: str(s?.blind_rendered) ?? str(f?.blind?.rendered) ?? null,
      deltaPct: s && 'delta_pct' in s ? num(s.delta_pct) ?? null : f?.delta_pct === null ? null : num(f?.delta_pct),
      // 预告跟着摘要来：完整对照还没到、或者取不到时也照它说
      preview: previewOf(s?.catalog),
    }
    if (f) {
      const items = (Array.isArray(f.diff?.items) ? f.diff.items : [])
        .filter((i) => !!i && typeof i.text === 'string' && !!i.text && typeof i.aspect === 'string')
      const concrete = items.filter((i) => !NOT_CONCRETE.has(i.aspect))
      const other = items.filter((i) => i.aspect === 'other')
      const equivalent = f.diff?.equivalent === true
      const fallbackOnly = !equivalent && !concrete.length
      const { standard, notes } = notesOf(items.filter((i) => NOT_CONCRETE.has(i.aspect)).map((i) => i.text))
      // 对不上的输入里没有一个两边都有 SQL 的（节点的原话「有一边没有可比较的 SQL」）：不说「两条 SQL 的写法不同」
      const comparable = pairsOf(f).some((p) => p.differs && hasSql(p.original?.sql) && hasSql(p.blind?.sql))
      return {
        ...base,
        items: concrete.map((i) => i.text),
        fallbackOnly,
        fallbackHead: !notes.length || standard || comparable,
        notes: fallbackOnly ? notes : [],
        rest: concrete.length && other.length ? other.map((i) => i.text).join('；') : null,
        equivalent,
        catalog: catalogOf(f, items),
        full: f,
      }
    }
    // 只有摘要：服务端把兜底和解析失败都算作 fallback，diff 里是前几条差异的原话
    const texts = Array.isArray(s?.diff) ? s.diff.filter((t): t is string => typeof t === 'string' && !!t) : []
    const fallbackOnly = s?.fallback === true
    return {
      ...base,
      items: fallbackOnly ? [] : texts,
      fallbackOnly,
      fallbackHead: true,
      notes: fallbackOnly ? notesOf(texts).notes : [],
      rest: null,
      equivalent: false,
      catalog: null,
      full: null,
    }
  })
}

/** 相差比例：服务端给的已乘过 100、保留两位（12.5 是相差 12.5%），带正负号；小到两位小数写不出来的写「<0.01%」 */
function deltaText(pct: number | null | undefined): string | null {
  if (pct === undefined) return null
  if (pct === null) return T.deltaZero
  const abs = Math.abs(pct)
  const sign = pct > 0 ? '+' : pct < 0 ? '-' : ''
  return T.delta(abs < 0.01 ? `${sign}<0.01%` : `${sign}${abs.toLocaleString('en-US', { maximumFractionDigits: 2 })}%`)
}

/**
 * 这次运行里节点的「未采用原流程时」。服务端在 payload 里带了实际生效的处置（given，约定之外另带的）就照它说；
 * 老服务端不带，从运行的图快照（GET /runs/{id}/graph）里按节点 id 读：没写、写了认不出的按缺省的 degrade（节点执行时
 * 同样这么认）；图里找不到这个节点（子工作流里的）、图快照取不到时是 unknown，卡上有条件地说
 */
type OnReject = 'degrade' | 'fail' | 'unknown' | 'loading'
function useOnReject(runId: string, nodeId: string | undefined, given: unknown): OnReject {
  const known = given === 'fail' || given === 'degrade' ? given : null
  const key = `${runId}\n${nodeId ?? ''}`
  const [got, setGot] = useState<{ key: string; value: OnReject } | null>(null)
  useEffect(() => {
    if (known || !runId || !nodeId) return
    let alive = true
    api.runs.graph(runId).then(
      (res) => {
        const node = Array.isArray(res?.graph?.nodes) ? res.graph.nodes.find((n) => n?.id === nodeId) : undefined
        const value: OnReject = !node ? 'unknown' : node.data?.config?.on_reject === 'fail' ? 'fail' : 'degrade'
        if (alive) setGot({ key, value })
      },
      () => { if (alive) setGot({ key, value: 'unknown' }) },
    )
    return () => { alive = false }
  }, [known, runId, nodeId, key])
  if (known) return known
  if (!runId || !nodeId) return 'unknown'
  return got?.key === key ? got.value : 'loading'
}

/**
 * 审批卡里双盲复算那一块（RunPanel 的 ApprovalCard 在 payload.kind === 'dual_check' 时渲染它，通用的输入框、备注和
 * 「提交 / 驳回并终止」不画——mode 是 input，通用那套会把一个字符串当 value 发出去）。
 *
 * 先按 payload.summary 画：两个值、相差多少、摘要里的差异，选择也能先做；完整对照（card_artifact）到了再补上盲算模型、
 * 全部具体差异和「对照两条 SQL」。取不到完整对照时照实说，按摘要照样能裁决。每个指标三选一、缺一个都不能提交，
 * 回复走审批接口：value 为 {choices, note}。摘要和工件里都认不出指标时没法裁决，只留「驳回并终止」（approved 为假，
 * 节点按人工驳回失败），免得运行一直挂着
 */
export function DualCheckApproval({ approval, onResolved, decide }: DualCheckApprovalProps) {
  const toast = useToast()
  const refreshApprovals = useCatalog((s) => s.refreshApprovals)
  const attachRun = useStudio((s) => s.attachRun)
  const uid = useId()
  const payload = approval.payload ?? {}
  const summary = useMemo(() => summaryOf(payload.summary), [payload.summary])
  const cardId = str(payload.card_artifact) ?? ''
  const onReject = useOnReject(approval.run_id, str(payload.node_id) ?? str(approval.node_id), payload.on_reject)
  // 驳回之后去哪（中断内容的 reject_route）：接了驳回出口的，驳回后转到那一步，不是「本次运行终止」
  const route = rejectRouteOf(payload)
  const routed = route && 'to' in route ? REJECT_ROUTE_TEXT.goes(route) : null

  const [card, setCard] = useState<{ id: string; data?: Card | null } | null>(null)
  useEffect(() => {
    if (!cardId) return
    let alive = true
    setCard({ id: cardId })
    api.artifact(cardId).then(
      (res) => { if (alive) setCard({ id: cardId, data: cardOf(res?.content) }) },
      // 取不到（工件被清理、网络断了）和取回来认不出一样处理：按摘要裁决
      () => { if (alive) setCard({ id: cardId, data: null }) },
    )
    return () => { alive = false }
  }, [cardId])
  const slot = card?.id === cardId ? card : null
  const data = slot?.data ?? null
  const loading = !!cardId && slot?.data === undefined
  const lost = !cardId || slot?.data === null

  const views = useMemo(() => viewsOf(summary, data), [summary, data])
  const [choices, setChoices] = useState<Record<string, DualCheckChoice>>({})
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const pending = views.filter((v) => !choices[v.id]).length
  // 预告要按其余指标当前的选择算：几个指标落到同一项上时照完整对照里服务端算好的合并结果说，选择一变，各块跟着变
  const joint = useMemo(() => jointOf(data), [data])
  const clauses = useMemo(() => new Map(views.map((v) => [v.id, clausesOf(v, views, choices, joint)])),
    [views, choices, joint])

  /** 提交裁决（approved 为真，value 是 {choices, note}）或驳回（认不出指标时的出口） */
  const send = async (approved: boolean) => {
    if (busy || (approved && (pending || !views.length))) return
    setBusy(true)
    try {
      const value: DualCheckReply = {
        choices: Object.fromEntries(views.map((v) => [v.id, choices[v.id]])),
        note: note.trim(),
      }
      // 照 ApprovalCard 里 mode 为 input 时的写法：备注外层也放一份，节点拿到的是 {approved, note, value}
      if (decide) await decide(approved, value)
      else {
        await api.approvals.decide(approval.id, approved ? { approved: true, note: value.note, value } : { approved: false, note: value.note })
        await refreshApprovals()
        if (onResolved) await onResolved(approval.run_id)
        else await attachRun(approval.run_id)
      }
      // 采用盲算、两边都不对时，节点设为「判为失败」就会让运行终止：提示照节点的设置说，读不到设置时不替它下结论
      const rejected = Object.values(value.choices).some((c) => c !== 'a')
      toast(!approved ? (routed ? TASK_TEXT.doneRejectRoute(routed) : T.rejected) : !rejected || onReject === 'degrade' ? T.done : onReject === 'fail' ? T.doneFail : T.doneUnknown, 'ok')
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  const noteField = (
    <div>
      <label className="label" htmlFor={`${uid}-note`}>{T.note}</label>
      <textarea id={`${uid}-note`} className="field" rows={2} value={note} placeholder={T.notePlaceholder}
                data-dual-note="" onChange={(e) => setNote(e.target.value)} />
    </div>
  )

  return (
    <div className="@container mb-2 space-y-2" data-dual-check="" data-state={loading ? 'loading' : lost ? 'summary' : 'ready'}>
      {views.length > 0 && <p className="text-xs leading-relaxed" data-dual-intro="">{T.intro(views.length)}</p>}
      {loading && (
        <div className="flex items-center gap-1.5 text-2xs text-faint" role="status" data-dual-loading="">
          <Spinner size={10} /> {T.loading}
        </div>
      )}
      {lost && views.length > 0 && (
        <div className="text-2xs leading-relaxed" style={{ color: 'var(--st-waiting)' }} data-dual-load-failed="">{T.loadFailed}</div>
      )}
      {data && (str(data.model) || data.same_model) && (
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs">
          {str(data.model) && <span className="mono text-dim [overflow-wrap:anywhere]" data-dual-model="">{T.model(data.model!)}</span>}
          {data.same_model && (
            <span className="flex items-center gap-1" style={{ color: 'var(--st-waiting)' }} data-dual-same-model="">
              <AlertTriangle size={11} aria-hidden className="shrink-0" /> {T.sameModel}
            </span>
          )}
        </div>
      )}

      {!views.length ? (
        !loading && (
          // 认不出任何指标：没法裁决，留一个驳回的出口（RunPanel 在双盲复算的审批上不画通用的按钮行）
          <>
            <p className="text-2xs leading-relaxed text-dim" data-dual-empty="">{T.empty}</p>
            {noteField}
            <button type="button" className="btn btn-danger w-full justify-center" disabled={busy} aria-describedby={`${uid}-reject`}
                    data-dual-reject="" onClick={() => void send(false)}>
              {busy ? <Spinner size={11} /> : <Ban size={12} aria-hidden />} {routed ? TASK_TEXT.reject : T.reject}
            </button>
            <div id={`${uid}-reject`} className="text-2xs leading-relaxed text-dim">
              {routed ? T.emptyRejectRoute(routed) : T.emptyReject}
            </div>
          </>
        )
      ) : (
        <>
          {views.map((v) => (
            <MetricBlock key={v.id} view={v} name={`${uid}-${v.id}`} choice={choices[v.id]} clauses={clauses.get(v.id) ?? []}
                         onChoose={(c) => setChoices((cur) => ({ ...cur, [v.id]: c }))} />
          ))}

          {/* 选了会怎样，常显在提交按钮上面：审核人得在提交之前知道「采用盲算」并不替换报告里的数，以及运行会不会终止 */}
          <div className="text-2xs leading-relaxed text-dim" data-dual-consequence="" data-on-reject={onReject}>
            {onReject === 'fail' ? T.consequenceFail : T.consequence(approval.run_class)}
            {(onReject === 'unknown' || onReject === 'loading') && <span className="mt-0.5 block">{T.consequenceMaybeFail}</span>}
          </div>
          {noteField}
          {pending > 0 && (
            <div id={`${uid}-pending`} className="text-2xs" style={{ color: 'var(--st-waiting)' }} data-dual-pending="">
              {T.pending(pending)}
            </div>
          )}
          <button type="button" className="btn btn-primary w-full justify-center" disabled={busy || pending > 0}
                  aria-describedby={pending > 0 ? `${uid}-pending` : undefined} data-dual-submit="" onClick={() => void send(true)}>
            {busy ? <Spinner size={11} /> : <Check size={12} aria-hidden />} {T.submit}
          </button>
        </>
      )}
    </div>
  )
}

/** 一个不一致的指标：名字、两个值、相差多少、差异说明、对照两条 SQL、三选一 */
function MetricBlock({ view, name, choice, clauses, onChoose }: {
  view: MetricView; name: string; choice?: DualCheckChoice; clauses: Clause[]; onChoose: (c: DualCheckChoice) => void
}) {
  const uid = useId()
  // 只有兜底说明（或只有解析失败）时默认展开：一条具体说法都没有，审核人只能对照原文判断
  const [open, setOpen] = useState<boolean | null>(null)
  const expanded = !!view.full && (open ?? view.fallbackOnly)
  const delta = deltaText(view.deltaPct)
  // 一边没有 SQL 时不写固定那句，原因顶上来用提醒色
  const [lead, ...more] = view.fallbackHead ? [T.fallbackOnly, ...view.notes] : view.notes
  return (
    <div className="rounded border bg-bg p-2" role="group" aria-labelledby={`${uid}-name`} data-dual-metric={view.id}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span id={`${uid}-name`} className="min-w-0 text-xs font-medium [overflow-wrap:anywhere]" data-dual-name="">{view.name}</span>
        {delta && (
          <span className="tnum ml-auto shrink-0 text-2xs font-medium" style={{ color: 'var(--st-waiting)' }} title={T.deltaHint}
                data-dual-delta="">{delta}</span>
        )}
      </div>
      <dl className="mt-1 grid grid-cols-2 gap-2" data-dual-values="">
        <div className="min-w-0">
          <dt className="text-2xs text-faint">{DUAL_CHECK_TEXT.original}</dt>
          <dd className="tnum m-0 text-sm font-medium [overflow-wrap:anywhere]" data-dual-value="original">{view.original ?? NONE}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-2xs text-faint">{DUAL_CHECK_TEXT.blind}</dt>
          <dd className="tnum m-0 text-sm font-medium [overflow-wrap:anywhere]" data-dual-value="blind">{view.blind ?? NONE}</dd>
        </div>
      </dl>

      <div className="mt-1.5" data-dual-diff="">
        <div className="text-2xs text-faint">{T.diffHead}</div>
        {view.equivalent ? (
          <p className="text-2xs leading-relaxed text-dim" data-dual-equivalent="">{T.equivalent}</p>
        ) : view.fallbackOnly ? (
          <>
            {lead && (
              <p className="text-2xs leading-relaxed [overflow-wrap:anywhere]" style={{ color: 'var(--st-waiting)' }} data-dual-fallback="">{lead}</p>
            )}
            {more.map((t, i) => (
              <p key={i} className="mt-0.5 text-2xs leading-relaxed text-dim [overflow-wrap:anywhere]" data-dual-fallback-note="">{t}</p>
            ))}
          </>
        ) : view.items.length > 0 && (
          <ul className="list-disc space-y-0.5 pl-4 text-2xs leading-relaxed [overflow-wrap:anywhere]">
            {view.items.map((t, i) => <li key={i} data-dual-diff-item="">{t}</li>)}
          </ul>
        )}
        {view.rest && <p className="mt-0.5 text-2xs leading-relaxed text-faint" data-dual-diff-rest="">{view.rest}</p>}
      </div>

      {view.full && (
        <button type="button" className="btn btn-xs mt-1.5" aria-expanded={expanded} aria-controls={expanded ? `${uid}-cmp` : undefined}
                data-dual-compare-toggle="" onClick={() => setOpen(!expanded)}>
          <ChevronRight size={11} aria-hidden style={{ transform: expanded ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }} />
          {expanded ? T.compareClose : T.compare}
        </button>
      )}
      {expanded && view.full && <Compare id={`${uid}-cmp`} metric={view.full} />}
      {view.preview ? (
        // 服务端预告了：两边都不写就不提数据目录
        // 服务端预告了：两边都认不出可记的事实（都没有写到哪）就不提数据目录
        (view.preview.a.target || view.preview.b.target) && <CatalogPreviewNote clauses={clauses} choice={choice} />
      ) : view.catalog && (
        <p className="mt-1.5 text-2xs leading-relaxed text-dim" data-dual-catalog={view.catalog}>{T.catalog[view.catalog]}{T.catalog.tail}</p>
      )}

      {/* 原生单选：Tab 进组、方向键换选项，读屏念得出是哪个指标的哪一项 */}
      <fieldset className="mt-2 min-w-0 border-0 p-0" data-dual-choice={view.id}>
        <legend className="sr-only">{T.choiceGroup(view.name)}</legend>
        <div className="flex flex-wrap gap-1.5">
          {CHOICES.map((c) => (
            <label key={c}
                   className={clsx('flex cursor-pointer items-center gap-1.5 rounded border px-2 py-1 text-2xs transition-colors hover:bg-hover',
                     choice === c ? 'font-medium' : 'text-dim')}
                   style={choice === c ? { borderColor: 'var(--accent)', background: 'var(--accent-soft)', color: 'var(--text)' } : undefined}>
              <input type="radio" className="accent-[var(--accent)]" name={name} value={c} checked={choice === c}
                     onChange={() => onChoose(c)} />
              {T.choices[c]}
            </label>
          ))}
        </div>
      </fieldset>
    </div>
  )
}

/**
 * 服务端预告的「记入数据目录」：三个选择逐条一句（clausesOf），审核人当前选的那一条加重——选之前三条都看得到，选了之后
 * 一眼看出这一选会往目录里写什么、不写的为什么。不写的原因是服务端的原话，往往不短，逐条分行写，不挤在一句里。有一条
 * 是推出来的（没有算好的合并结果）时，结尾换成「实际写入以时间线上的记录为准」的一般说法
 */
function CatalogPreviewNote({ clauses, choice }: { clauses: Clause[]; choice?: DualCheckChoice }) {
  const P = T.catalogPreview
  const inferred = clauses.some((c) => c.inferred)
  return (
    <div className="mt-1.5 text-2xs leading-relaxed text-dim [overflow-wrap:anywhere]" data-dual-catalog="preview">
      <div>{P.head}</div>
      <ul className="mt-0.5 list-disc space-y-0.5 pl-4">
        {clauses.map((c) => (
          <li key={c.side} className={clsx(choice === c.side && 'font-medium')} style={choice === c.side ? { color: 'var(--text)' } : undefined}
              data-dual-catalog-side={c.side} data-write={c.write ? '' : undefined} data-current={choice === c.side ? '' : undefined}
              data-reason={c.reason ? '' : undefined} data-joint={c.joint ? '' : undefined} data-together={c.together ? '' : undefined}
              data-conflict={c.conflict ? '' : undefined} data-joined={c.joined ? '' : undefined}
              data-pending={c.pending ? '' : undefined} data-inferred={c.inferred ? '' : undefined}>
            {c.text}
          </li>
        ))}
      </ul>
      <p className="mt-0.5" data-dual-catalog-tail={inferred ? 'inferred' : 'exact'}>{inferred ? P.tailInferred : P.tail}</p>
    </div>
  )
}

/** 一个输入在两边的取值和 SQL */
interface Pair {
  path: string
  original?: DualCheckInput
  blind?: BlindInput
  source: string | null
  /** SQL 或取到的值不同 */
  differs: boolean
}

/** 比 SQL 时不看空白和大小写：原样写成一行和换了行的是同一条 */
const normSql = (sql: unknown): string => (typeof sql === 'string' ? sql.replace(/\s+/g, ' ').trim().toLowerCase() : '')
/** 比一行时再去掉行尾的逗号、分号：只是换了个地方断行的两行算同一行 */
const normLine = (line: string): string => line.replace(/\s+/g, ' ').trim().replace(/[,;]+$/, '').trim().toLowerCase()
const sameValue = (a: unknown, b: unknown): boolean => {
  if (typeof a === 'number' && typeof b === 'number') return a === b
  try { return JSON.stringify(a) === JSON.stringify(b) } catch { return false }
}

function pairsOf(metric: DualCheckMetric): Pair[] {
  const byPath = new Map<string, { original?: DualCheckInput; blind?: BlindInput }>()
  const valid = (i: unknown): i is DualCheckInput => !!i && typeof i === 'object' && typeof (i as DualCheckInput).path === 'string'
  for (const i of (Array.isArray(metric.original?.inputs) ? metric.original.inputs : []).filter(valid)) {
    byPath.set(i.path, { ...byPath.get(i.path), original: i })
  }
  for (const i of (Array.isArray(metric.blind?.inputs) ? metric.blind.inputs : []).filter(valid)) {
    byPath.set(i.path, { ...byPath.get(i.path), blind: i })
  }
  return [...byPath].map(([path, p]) => ({
    path, ...p,
    source: str(p.original?.source) ?? str(p.blind?.source) ?? null,
    differs: normSql(p.original?.sql) !== normSql(p.blind?.sql) || !sameValue(p.original?.value, p.blind?.value),
  }))
}

/** 按行数算 LCS 的上限：再长的 SQL 不在浏览器里逐行比，原文不标（规范化后不同的行照样列） */
const MAX_LCS_CELLS = 250_000

/**
 * 两段原文按行比（不看空白、大小写和行尾的逗号分号），返回两边各自不在最长公共子序列里的行号。
 *
 * 不拿 text_diff 去对原文：服务端的 text_diff 是规范化之后的行（列名带表名、每个条件一行），和原文的行几乎从来
 * 对不上。一行都对不上（写法完全不同、各写成一行）时返回 null：整段都标出来没有信息量，差在哪看规范化后的行
 */
function lineDiff(a: string[], b: string[]): { a: Set<number>; b: Set<number> } | null {
  const na = a.map(normLine)
  const nb = b.map(normLine)
  const n = na.length
  const m = nb.length
  if (!n || !m || n * m > MAX_LCS_CELLS) return null
  // lcs[i][j]：na[i..] 和 nb[j..] 的最长公共子序列有几行
  const lcs = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) lcs[i][j] = na[i] === nb[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1])
  }
  if (!lcs[0][0]) return null
  const da = new Set<number>()
  const db = new Set<number>()
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (na[i] === nb[j]) { i++; j++ } else if (lcs[i + 1][j] >= lcs[i][j + 1]) da.add(i++)
    else db.add(j++)
  }
  while (i < n) da.add(i++)
  while (j < m) db.add(j++)
  return { a: da, b: db }
}

/** SQL 原文按行切（去掉末尾的空白行） */
const linesOf = (sql: unknown): string[] | null => (hasSql(sql) ? (sql as string).replace(/\s+$/, '').split('\n') : null)

/** 规范化后的一行：context 是服务端带上的上级行（「  」开头、两边相同），只为标出不同的行在哪个子句里，不高亮 */
interface NormLine { text: string; context: boolean }

/** 行首的空格数：服务端的规范化写法用空格缩进，子句的内容缩在子句名下面 */
const depthOf = (line: string): number => line.length - line.trimStart().length

/**
 * text_diff 里一栏的行：这一边（「- 」原流程、「+ 」盲算）不同的行，加上它们用得着的上级行。服务端在不同的行前面带上
 * 各级上级行（「  」开头：条件在哪个 WHERE、哪个 JOIN 的 ON、哪层子查询里），两边共用一份；这里只留这一边的不同行
 * 往上数用得着的——排在前面、缩进比它浅的最近一行，一层层往上——另一边才用得着的不放进这一栏（一边多出一个 WHERE 时，
 * 另一边关联条件上面那一行 JOIN 和它无关）。这一边没有不同的行时一行都不留，栏里照实说没有
 */
function normSide(lines: string[], mark: '- ' | '+ '): NormLine[] {
  const own = lines.filter((l) => l.startsWith(mark) || l.startsWith('  '))
    .map((l) => ({ text: l.slice(2), context: l.startsWith('  ') }))
  if (!own.some((l) => !l.context)) return []
  const keep = new Set<number>()
  own.forEach((line, i) => {
    if (line.context) return
    let depth = depthOf(line.text)
    for (let k = i - 1; k >= 0 && depth > 0; k--) {
      const above = own[k]
      if (!above.text.trim() || depthOf(above.text) >= depth) continue
      if (above.context) keep.add(k)
      depth = depthOf(above.text)
    }
  })
  return own.filter((line, i) => !line.context || keep.has(i))
}

function valueText(v: unknown): string {
  if (v == null) return NONE
  if (typeof v === 'number') return Number.isFinite(v) ? v.toLocaleString('en-US', { maximumFractionDigits: 6 }) : String(v)
  if (typeof v === 'string' || typeof v === 'boolean') return String(v)
  try {
    const s = JSON.stringify(v)
    return s.length > 80 ? `${s.slice(0, 79)}…` : s
  } catch {
    return String(v)
  }
}

/** 高亮的行：两边都用同一种提醒底色——谁对谁错由审核人判断，不用红绿暗示哪边是「删掉的」 */
const MARK: CSSProperties = {
  background: 'color-mix(in srgb, var(--warn) 16%, transparent)', color: 'var(--text)', boxShadow: 'inset 2px 0 0 var(--warn)',
}
/** 代码框：折行显示、不横向撑开，太长时框里纵向滚动，框能用键盘聚焦 */
const CODE_BOX = 'overflow-y-auto whitespace-pre-wrap rounded border bg-panel px-1 py-1.5 text-2xs leading-relaxed [overflow-wrap:anywhere] '
  + 'focus-visible:outline-2 focus-visible:outline-[color:var(--accent)]'

/**
 * 对照两条 SQL：先列每个输入两边各取到多少；再把不同的输入的两条 SQL 原文左右并排（卡片窄于 32rem 时上下排，按卡片
 * 自己的宽度算，画布右栏里也是上下排），两段原文按行比出的不同行高亮；最后是规范化后不同的行（服务端的 text_diff），
 * 同样分原流程、盲算两栏，不同的行高亮，前面淡色的是它所在子句的那几行（见 normSide）——原文格式差得太远、按行比不出
 * 东西时，差在哪就看这一段
 */
function Compare({ id, metric }: { id: string; metric: DualCheckMetric }) {
  const pairs = useMemo(() => pairsOf(metric), [metric])
  const norm = useMemo(() => {
    const lines = (Array.isArray(metric.diff?.text_diff) ? metric.diff.text_diff : []).filter((l): l is string => typeof l === 'string')
    return { original: normSide(lines, '- '), blind: normSide(lines, '+ ') }
  }, [metric])
  const withContext = norm.original.some((l) => l.context) || norm.blind.some((l) => l.context)
  const differing = pairs.filter((p) => p.differs)
  return (
    <div id={id} className="mt-1.5 min-w-0 space-y-2" data-dual-compare="">
      {pairs.length > 0 && (
        <div>
          <div className="mb-0.5 text-2xs text-faint">{T.inputs}</div>
          <ul className="space-y-0.5">
            {pairs.map((p) => (
              <li key={p.path} className="flex min-w-0 flex-wrap items-baseline gap-x-2 text-2xs" data-dual-input={p.path}
                  data-differs={p.differs ? '' : undefined}>
                <code className="mono min-w-0 [overflow-wrap:anywhere]">{p.path}</code>
                <span className="tnum text-dim">{DUAL_CHECK_TEXT.original} {valueText(p.original?.value)}</span>
                <span className={clsx('tnum', !p.differs && 'text-dim')} style={p.differs ? { color: 'var(--st-waiting)' } : undefined}>
                  {DUAL_CHECK_TEXT.blind} {valueText(p.blind?.value)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {(differing.length ? differing : pairs).map((p) => <PairSql key={p.path} pair={p} />)}
      {(norm.original.length > 0 || norm.blind.length > 0) && (
        <div data-dual-text-diff="">
          <div className="mb-0.5 text-2xs text-faint">
            {T.textDiff}{withContext && <span data-dual-text-diff-hint="">{` · ${T.textDiffContext}`}</span>}
          </div>
          <div className="grid grid-cols-1 gap-2 @lg:grid-cols-2">
            <NormPane side="original" lines={norm.original} />
            <NormPane side="blind" lines={norm.blind} />
          </div>
        </div>
      )}
    </div>
  )
}

/** 一个对不上的输入：两条 SQL 原文并排，按行比出的不同行高亮，盲算的说明跟在下面 */
function PairSql({ pair }: { pair: Pair }) {
  const a = useMemo(() => linesOf(pair.original?.sql), [pair.original?.sql])
  const b = useMemo(() => linesOf(pair.blind?.sql), [pair.blind?.sql])
  const diff = useMemo(() => (a && b ? lineDiff(a, b) : null), [a, b])
  return (
    <div className="min-w-0" data-dual-pair={pair.path}>
      <div className="mb-0.5 text-2xs text-faint [overflow-wrap:anywhere]">
        <code className="mono">{pair.path}</code>{pair.source ? ` · ${T.source(pair.source)}` : ''}
      </div>
      <div className="grid grid-cols-1 gap-2 @lg:grid-cols-2">
        <SqlPane side="original" label={T.sqlOriginal} lines={a} marks={diff?.a} />
        <SqlPane side="blind" label={T.sqlBlind} lines={b} marks={diff?.b} />
      </div>
      <BlindRationale input={pair.blind} />
    </div>
  )
}

/** 理由文档里有没有没出处的数字（evidence.compose_doc 标出的裸数字，状态是「无证据」） */
const hasBare = (doc: EvidenceDocData): boolean =>
  (doc.blocks ?? []).some((b) => (b.units ?? []).some((u) => (u.segments ?? []).some((seg) => segmentState(seg) === 'none')))

/**
 * 盲算的说明。工件带了核查 agent 组装的理由文档（rationale_doc）时按文档画，和反方审稿的理由同一种画法：数字有出处的
 * 画实线、挂盲算自己的查询编号（悬停看出处），裸数字画点状线、句末挂「无证据」，下面再醒目地说一句。老工件只有渲染后的
 * 正文，照旧显示纯文本
 */
function BlindRationale({ input }: { input?: BlindInput }) {
  const doc = useMemo(() => {
    const raw = input?.rationale_doc
    const parsed = raw && typeof raw === 'object' ? asDoc(raw) : null
    return parsed && (parsed.blocks ?? []).length ? parsed : null
  }, [input?.rationale_doc])
  if (doc) {
    return (
      <div className="mt-1 min-w-0 text-2xs leading-relaxed text-dim [overflow-wrap:anywhere]" data-dual-rationale="doc">
        <div className="text-faint">{T.rationale}</div>
        <RationaleDoc doc={doc} />
        {hasBare(doc) && (
          <p className="mt-0.5 flex items-start gap-1 font-medium" style={{ color: 'var(--st-waiting)' }} data-dual-rationale-bare="">
            <AlertTriangle size={11} aria-hidden className="mt-[3px] shrink-0" /> {T.rationaleBare}
          </p>
        )}
      </div>
    )
  }
  if (!str(input?.rationale)) return null
  return (
    <p className="mt-1 text-2xs leading-relaxed text-dim [overflow-wrap:anywhere]" data-dual-rationale="text">
      <span className="text-faint">{T.rationale}：</span>{input!.rationale}
    </p>
  )
}

/** 一条 SQL 原文：逐行画，marks 里的行（按行比出的不同行）高亮；空白行不标 */
function SqlPane({ side, label, lines, marks }: { side: 'original' | 'blind'; label: string; lines: string[] | null; marks?: Set<number> }) {
  return (
    <figure className="m-0 min-w-0">
      <figcaption className="mb-0.5 text-2xs text-faint">{label}</figcaption>
      <pre className={clsx('max-h-56', CODE_BOX, lines ? 'mono text-dim' : 'font-sans text-faint')} tabIndex={0} aria-label={label} data-dual-sql={side}>
        {lines
          ? lines.map((line, i) => (marks?.has(i) && line.trim()
            ? <mark key={i} className="block rounded-sm px-1" style={MARK} data-diff-line={side === 'original' ? 'del' : 'add'}>{line}</mark>
            : <span key={i} className="block px-1">{line || ' '}</span>))
          : <span className="px-1">{T.sqlMissing}</span>}
      </pre>
    </figure>
  )
}

/**
 * 规范化后不同的行的一栏：这一边的「- 」（原流程）或「+ 」（盲算）行，每行高亮；前面带着的上级行（两边相同，标出在
 * 哪个子句里）不高亮、用淡一级的字色，同原文栏里没标的行；这一边一行都没有时照实说
 */
function NormPane({ side, lines }: { side: 'original' | 'blind'; lines: NormLine[] }) {
  const label = side === 'original' ? DUAL_CHECK_TEXT.original : DUAL_CHECK_TEXT.blind
  return (
    <figure className="m-0 min-w-0" data-dual-norm={side}>
      <figcaption className="mb-0.5 text-2xs text-faint">{label}</figcaption>
      <div className={clsx('max-h-48', CODE_BOX, lines.length ? 'mono' : 'font-sans text-faint')} tabIndex={0} role="group"
           aria-label={`${T.textDiff} · ${label}`}>
        {lines.length
          ? lines.map((line, i) => (line.context
            ? (
              <span key={i} className="mb-px block px-1 text-dim last:mb-0" title={T.textDiffContext} data-diff-context="">
                {line.text || ' '}
              </span>
            )
            : (
              <mark key={i} className="mb-px block rounded-sm px-1 last:mb-0" style={MARK} data-diff-line={side === 'original' ? 'del' : 'add'}>
                {line.text || ' '}
              </mark>
            )))
          : <span className="px-1">{T.textDiffNone}</span>}
      </div>
    </figure>
  )
}
