/**
 * 记录页的纯函数：页签、筛选词、状态落点、时长口径、同名工作流的区分。
 *
 * 不依赖 React：check-runs 和将来的单测可以直接喂数据。状态的文字和外观一律
 * 从 lib/status 取，这里只决定"哪条运行算哪个状态"和"筛选词指的是哪个状态"。
 */

import type { Approval, Run, RunStatus, RunUsage } from '../../types'
import {
  RUN_STATUS_ORDER, STATUS, resolveStatus, serverStatusOf, type StatusCode,
} from '../../lib/status'
import { formatSpan, parseServerTime } from '../../lib/format'
import { hasPendingApproval } from '../../store/catalog'

// -------------------------------------------------------------------------
// 页签
// -------------------------------------------------------------------------

/**
 * 页签是工作队列，不是状态筛选的另一种写法：「待审批」列的是审批（另一种
 * 东西，带节点和等待时长），「运行中」「失败」是最常来处理的两类运行；
 * 完整的状态维度在「全部」里用分段筛。
 */
export type RunsTab = 'all' | 'approvals' | 'running' | 'failed'
export const RUNS_TABS: readonly RunsTab[] = ['all', 'approvals', 'running', 'failed']
export const TAB_LABEL: Record<RunsTab, string> = {
  all: '全部', approvals: '待审批', running: '运行中', failed: '失败',
}
/** 这两个页签固定的状态范围（显示码） */
export const TAB_CODES: Record<'running' | 'failed', StatusCode[]> = {
  running: ['running', 'queued'],
  failed: ['failed'],
}

export const asTab = (v: string | null | undefined): RunsTab =>
  (RUNS_TABS as readonly string[]).includes(v ?? '') ? (v as RunsTab) : 'all'

/** 「全部」页签里的状态分段，顺序同 lib/status 的 RUN_STATUS_ORDER */
export const SEGMENT_CODES: StatusCode[] = RUN_STATUS_ORDER

// -------------------------------------------------------------------------
// 状态落点
// -------------------------------------------------------------------------

/**
 * 一条运行此刻显示成哪个状态。后端的 interrupted 有两种：停在审批上、服务重启
 * 打断后没有审批——要看待审批列表才分得开。approvals 还没加载时传 null，按
 * 等待审批算（最常见的来源），不会在加载的那一瞬间闪成「已挂起」。
 */
export function runCode(run: Pick<Run, 'id' | 'status'>, approvals: Approval[] | null): StatusCode {
  if (run.status !== 'interrupted') return resolveStatus(run.status)
  return resolveStatus(run.status, {
    pendingApproval: approvals == null ? undefined : hasPendingApproval(approvals, run.id),
  })
}

/** 还会自己往前走、会有新事件的运行：列表要跟着刷，详情要接流 */
export const isLiveRun = (status: RunStatus | string | null | undefined): boolean =>
  status === 'running' || status === 'queued'

// -------------------------------------------------------------------------
// 搜索框里的状态词
// -------------------------------------------------------------------------

/**
 * 屏幕上写的是中文状态，人就会照着中文去搜。以前筛选只认英文枚举：输入「失败」
 * 只命中两条内容里恰好有"失败"二字的成功运行，真正失败的一条都不出来。
 *
 * 词表从 lib/status 的 label / short 生成，另加几个口头说法；显示文字改了这里
 * 自动跟上。
 */
const WORDS = new Map<string, StatusCode[]>()
const addWord = (word: string, codes: StatusCode[]) => {
  const key = norm(word)
  if (!key) return
  const prev = WORDS.get(key) ?? []
  WORDS.set(key, [...new Set([...prev, ...codes])])
}
function norm(s: string): string {
  return s.toLowerCase().replace(/[\s·・()（）]+/g, '')
}
for (const code of RUN_STATUS_ORDER) {
  addWord(STATUS[code].label, [code])
  addWord(STATUS[code].short, [code])
  addWord(code, [code])
}
addWord(STATUS.suspended.label, ['held'])
for (const [word, codes] of [
  ['完成', ['succeeded']], ['成功', ['succeeded']], ['success', ['succeeded']],
  ['待审批', ['waiting']], ['等待', ['waiting']], ['审批', ['waiting']], ['等待审批', ['waiting']],
  ['挂起', ['held']], ['中断', ['held']], ['已中断', ['held']], ['可继续运行', ['held']],
  ['interrupted', ['waiting', 'held']],
  ['取消', ['cancelled']], ['canceled', ['cancelled']],
  ['出错', ['failed']], ['报错', ['failed']], ['error', ['failed']],
  ['排队', ['queued']], ['执行中', ['running']], ['进行中', ['running']],
] as [string, StatusCode[]][]) addWord(word, codes)

/** 搜索框里被认成状态的词不再当名称去搜；说明给占位符和提示用 */
export const SEARCH_PLACEHOLDER = '搜索工作流名称或状态（如「失败」）'

export interface ParsedQuery {
  /** 从文字里认出来的状态（显示码），没有就是空 */
  codes: StatusCode[]
  /** 认出来的那几个词，原样（提示「按状态「失败」筛选」用） */
  words: string[]
  /** 剩下的交给后端按工作流名匹配 */
  q: string
}

/** 「失败 新工作流」→ 状态 failed + 名称「新工作流」 */
export function parseQuery(text: string): ParsedQuery {
  const whole = WORDS.get(norm(text))
  if (whole) return { codes: whole, words: [text.trim()], q: '' }
  const codes: StatusCode[] = []
  const words: string[] = []
  const rest: string[] = []
  for (const token of text.split(/\s+/).filter(Boolean)) {
    const hit = WORDS.get(norm(token))
    if (hit) {
      codes.push(...hit)
      words.push(token)
    } else {
      rest.push(token)
    }
  }
  return { codes: [...new Set(codes)], words, q: rest.join(' ') }
}

/** 把文字里的状态词去掉（点了状态分段之后，文字里的旧状态词不该再和它打架） */
export function stripStatusWords(text: string): string {
  if (WORDS.has(norm(text))) return ''
  return text.split(/\s+/).filter((t) => t && !WORDS.has(norm(t))).join(' ')
}

/**
 * 显示码 → api.runs.list 的 status 参数。waiting / held 都查 interrupted，
 * 回来之后再用 runCode 在前端分开（matchesCodes）。
 */
export function serverStatuses(codes: StatusCode[]): RunStatus[] {
  const out = new Set<RunStatus>()
  for (const c of codes) {
    const s = serverStatusOf(c)
    if (s) out.add(s)
  }
  return [...out]
}

export function matchesCodes(run: Run, codes: StatusCode[], approvals: Approval[] | null): boolean {
  if (!codes.length) return true
  return codes.includes(runCode(run, approvals))
}

// -------------------------------------------------------------------------
// 时长
// -------------------------------------------------------------------------

export interface RunTiming {
  /** 墙钟：第一次开始到结束 */
  wallMs: number | null
  /** 执行：各段执行之和 */
  activeMs: number | null
  /** 等人审批 */
  waitMs: number | null
  /**
   * 怎么来的：usage 是后端分段计时；stamps 是老数据，用创建和结束时间推的墙钟；
   * last 是老数据连结束时间都没有，只剩「最后一段」的 duration_ms
   */
  source: 'usage' | 'stamps' | 'last' | 'none'
}

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/**
 * 列表和详情头的三种时长。
 *
 * 老数据的 usage.duration_ms 在审批恢复后只剩最后一段（87 秒的运行记成 22 ms），
 * 拿它当"耗时"会和节点上的 1 分 14 秒同屏打架。所以老数据优先用结束时间减
 * 创建时间推墙钟，执行和等人说不清就写「—」，不拿最后一段冒充。
 */
export function runTiming(run: Pick<Run, 'usage' | 'created_at' | 'started_at' | 'finished_at'>): RunTiming {
  const u: RunUsage = run.usage ?? {}
  if (isNum(u.wall_ms)) {
    return {
      wallMs: u.wall_ms,
      activeMs: isNum(u.active_ms) ? u.active_ms : isNum(u.duration_ms) ? u.duration_ms : null,
      waitMs: isNum(u.wait_ms) ? u.wait_ms : 0,
      source: 'usage',
    }
  }
  const start = parseServerTime(run.created_at ?? run.started_at ?? null)
  const end = parseServerTime(run.finished_at ?? null)
  if (start && end && end.getTime() >= start.getTime()) {
    const wall = end.getTime() - start.getTime()
    // 和最后一段差不多长（没经过审批）时，那一段就是全部执行
    const active = isNum(u.duration_ms) && wall - u.duration_ms < 1500 ? u.duration_ms : null
    return { wallMs: wall, activeMs: active, waitMs: active != null ? 0 : null, source: 'stamps' }
  }
  if (isNum(u.duration_ms)) return { wallMs: null, activeMs: u.duration_ms, waitMs: null, source: 'last' }
  return { wallMs: null, activeMs: null, waitMs: null, source: 'none' }
}

/** 列表里那一个数：优先墙钟 */
export const headlineMs = (t: RunTiming): number | null => t.wallMs ?? t.activeMs

// -------------------------------------------------------------------------
// 列表行的缩略条
// -------------------------------------------------------------------------

/**
 * active：执行；wait：等人审批；other：墙钟里既不算执行也不算等人的部分（排队、
 * 两段之间的空当）；unknown：老数据，只知道总长、分不出执行和等人；live：还在跑
 */
export type ShapeKind = 'active' | 'wait' | 'other' | 'unknown' | 'live'

export interface RunShape {
  /** 各段占墙钟的比例，加起来是 1；空数组 = 连墙钟都不知道 */
  segs: { kind: ShapeKind; frac: number; open?: boolean }[]
  /** 收在哪种结局上：只给要注意的几种，已完成不标 */
  end: 'failed' | 'cancelled' | 'suspended' | null
  title: string
}

/**
 * 一条运行的墙钟里执行、等人各占多少。只用列表里就有的 usage 和审批，不为每一
 * 行去拉事件——所以它是构成，不是时间顺序：几段审批各在哪里，要进详情看航迹。
 * 唯一的例外是正在等的那一段，它一定在最后。
 *
 * 分不出来的（老数据没有分段计时）画成虚的一整条，不按比例猜。
 */
export function runShape(
  run: Pick<Run, 'usage' | 'created_at' | 'started_at' | 'finished_at' | 'status'>,
  code: StatusCode,
  opts: { waitingSince?: string | null; now?: number } = {},
): RunShape {
  const now = opts.now ?? Date.now()
  if (code === 'running' || code === 'queued') {
    return { segs: [{ kind: 'live', frac: 1 }], end: null, title: code === 'queued' ? '排队中' : '运行中：结束后才能区分执行时长与等待审批时长' }
  }
  const t = runTiming(run)
  const pieces =(wall: number, active: number | null, wait: number, openWait = 0) => {
    const f = (ms: number) => Math.max(0, Math.min(1, ms / wall))
    const segs: RunShape['segs'] = []
    if (active != null) segs.push({ kind: 'active', frac: f(active) })
    if (wait > 0) segs.push({ kind: 'wait', frac: f(wait) })
    const used = segs.reduce((n, s) => n + s.frac, 0) + f(openWait)
    const rest = Math.max(0, 1 - used)
    if (rest > 0.005) segs.push({ kind: active == null ? 'unknown' : 'other', frac: rest })
    if (openWait > 0) segs.push({ kind: 'wait', frac: f(openWait), open: true })
    return segs
  }

  if (code === 'waiting') {
    const start = parseServerTime(run.started_at ?? run.created_at ?? null)?.getTime()
    const since = parseServerTime(opts.waitingSince ?? null)?.getTime()
    if (start == null || since == null || now <= start) return { segs: [], end: null, title: '等待审批' }
    const wall = now - start
    const open = Math.max(0, now - since)
    const active = t.source === 'usage' ? t.activeMs : null
    const before = t.source === 'usage' ? t.waitMs ?? 0 : 0
    return {
      segs: pieces(wall, active, before, open),
      end: null,
      title: `总时长 ${formatSpan(wall)}（至今） · 执行时长 ${formatSpan(active)} · 等待审批 ${formatSpan(before + open)}（仍在等待）`,
    }
  }

  const end: RunShape['end'] = code === 'failed' ? 'failed' : code === 'cancelled' ? 'cancelled'
    : code === 'held' || code === 'suspended' ? 'suspended' : null
  if (t.wallMs == null || t.wallMs <= 0) {
    return { segs: [], end, title: t.activeMs != null ? timingTitle(t) : '没有时长记录' }
  }
  return { segs: pieces(t.wallMs, t.activeMs, t.waitMs ?? 0), end, title: timingTitle(t) }
}

// -------------------------------------------------------------------------
// 取消
// -------------------------------------------------------------------------

/**
 * 取消的原因，去掉后端统一加的「用户取消：」前缀；只有那四个字的（跑着时点了
 * 停止）没有别的原因，返回 null。列表写「已取消 · 在等审批时放弃了这次运行」，
 * 不写「已取消 · 用户取消：在等审批时…」
 */
export function cancelReason(error: string | null | undefined): string | null {
  const text = (error ?? '').trim()
  if (!text || text === '用户取消') return null
  return text.replace(/^用户取消\s*[:：]\s*/, '') || null
}

// -------------------------------------------------------------------------
// 详情的几个视图
// -------------------------------------------------------------------------

/**
 * stream：时间线（逐步的文字）；trace：航迹（按时间摊开、可回放）；evidence：证据（报告、常驻面板、
 * 审计表）；artifacts：工件
 */
export type DetailView = 'stream' | 'trace' | 'evidence' | 'artifacts'
export const DETAIL_VIEWS: readonly DetailView[] = ['stream', 'trace', 'evidence', 'artifacts']
export const asView = (v: string | null | undefined): DetailView =>
  (DETAIL_VIEWS as readonly string[]).includes(v ?? '') ? (v as DetailView) : 'stream'

/**
 * 图的骨架：节点 id 和连线。运行时的快照和工作流现在的样子骨架不同，画布上回放
 * 就是把这次的事件套在另一张图上——对不上的节点不会亮，要先说清
 */
export function graphShape(g: { nodes?: { id: string }[]; edges?: { source: string; target: string; sourceHandle?: string | null }[] } | null | undefined): string | null {
  if (!g?.nodes) return null
  const ids = g.nodes.map((n) => n.id).sort().join(',')
  const links = (g.edges ?? []).map((e) => `${e.source}>${e.target}:${e.sourceHandle ?? ''}`).sort().join(',')
  return `${ids}|${links}`
}

// -------------------------------------------------------------------------
// 工件
// -------------------------------------------------------------------------

export interface RunArtifact {
  id: string
  kind: string
  node_id: string | null
  size: number | null
  meta: Record<string, any> | null
  created_at: string | null
}

export const ARTIFACT_KIND_LABEL: Record<string, string> = {
  query_snapshot: '查询快照',
  tool_snapshot: '工具快照',
  retrieval_snapshot: '检索快照',
  node_output: '节点产出',
  metric_set: '口径卡指标集',
  report_doc: '报告文档',
  schema_snapshot: '表结构快照',
}
export const artifactKindLabel = (kind: string): string => ARTIFACT_KIND_LABEL[kind] ?? kind

/** 工件行上的一句说明：这件工件里装的是什么 */
export function artifactDescription(kind: string, meta?: Record<string, any> | null): string {
  switch (kind) {
    case 'query_snapshot': return '查询：SQL 和结果集'
    case 'tool_snapshot': return '工具调用：参数和返回'
    case 'retrieval_snapshot': return '知识检索：命中的片段'
    case 'node_output': return '本次执行的产出'
    // 口径卡每跑一次落一件：哪张卡、哪一版（工件行的 meta 里有 {caliber, version}）
    case 'metric_set': return meta?.caliber
      ? `口径卡「${meta.caliber}」${meta.version ?? ''}` : '口径卡的指标、算式和输入'
    case 'report_doc': return '报告：逐段的片段和每个数字的出处'
    // 查库时冻结的表结构（工件行的 meta 里有 {source, tables: 表数量}）：报告里的表名、字段名按它核对
    case 'schema_snapshot': return meta?.source
      ? `表结构：数据源「${meta.source}」${typeof meta.tables === 'number' ? ` · ${meta.tables} 张表` : ''}` : '查询时保存的表结构'
    default: return '工件'
  }
}

/** 证据：查询、工具、检索的快照。节点产出是过程里的中间值，一次循环就是几十件 */
export const isEvidence = (kind: string): boolean => kind !== 'node_output'

/** 三种时长写进 title：「总时长 1 分 27 秒 · 执行时长 7.6 s · 等待审批 1 分 14 秒」 */
export function timingTitle(t: RunTiming): string {
  const parts = [
    `总时长 ${formatSpan(t.wallMs)}`,
    `执行时长 ${formatSpan(t.activeMs)}`,
    `等待审批 ${formatSpan(t.waitMs)}`,
  ]
  const note = t.source === 'stamps'
    ? '（总时长按创建与结束时间推算，无分段计时）'
    : t.source === 'last'
      ? '（仅记录了最后一段执行时长）'
      : ''
  return parts.join(' · ') + note
}

/** 等了多久算"久"：超过一天就该有人管了 */
export const LONG_WAIT_MS = 86_400_000

export function ageMs(v: string | null | undefined, now = Date.now()): number | null {
  const d = parseServerTime(v ?? null)
  return d ? Math.max(0, now - d.getTime()) : null
}

// -------------------------------------------------------------------------
// 工作流名
// -------------------------------------------------------------------------

/**
 * 同名工作流的区分标记。库里有三个都叫「新工作流」的，列表只写名字就分不清
 * 哪条运行属于哪一个。名字重复时带上 id 尾号（「…d373」），不重复不加。
 */
export function duplicateNames(items: { id: string | null; name: string }[]): Set<string> {
  const ids = new Map<string, Set<string>>()
  for (const it of items) {
    if (!it.id) continue
    const set = ids.get(it.name) ?? new Set<string>()
    set.add(it.id)
    ids.set(it.name, set)
  }
  return new Set([...ids].filter(([, s]) => s.size > 1).map(([name]) => name))
}

export const idTail = (id: string | null | undefined, len = 4): string =>
  id ? `…${id.slice(-len)}` : ''

/** 这次运行实际用的记忆域和知识库，null 收成 undefined：重新发起时原样带上，没有就不带 */
export function runScope(run: Run): { memory_scope?: string; collection?: string } {
  return { memory_scope: run.memory_scope ?? undefined, collection: run.collection ?? undefined }
}
