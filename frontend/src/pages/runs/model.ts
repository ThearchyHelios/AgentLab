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
import { NONE, formatDuration, parseServerTime } from '../../lib/format'
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
  ['待审批', ['waiting']], ['等待', ['waiting']], ['审批', ['waiting']], ['等人', ['waiting']],
  ['挂起', ['held']], ['中断', ['held']], ['已中断', ['held']], ['可续跑', ['held']],
  ['interrupted', ['waiting', 'held']],
  ['取消', ['cancelled']], ['canceled', ['cancelled']],
  ['出错', ['failed']], ['报错', ['failed']], ['error', ['failed']],
  ['排队', ['queued']], ['执行中', ['running']], ['进行中', ['running']],
] as [string, StatusCode[]][]) addWord(word, codes)

/** 搜索框里被认成状态的词不再当名称去搜；说明给占位符和提示用 */
export const SEARCH_PLACEHOLDER = '搜工作流名称，或输入状态（如 失败）'

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

/** 三种时长写进 title：「墙钟 1 分 27 秒 · 执行 7.6 s · 等人 1 分 14 秒」 */
export function timingTitle(t: RunTiming): string {
  const parts = [
    `墙钟 ${formatSpan(t.wallMs)}`,
    `执行 ${formatSpan(t.activeMs)}`,
    `等人 ${formatSpan(t.waitMs)}`,
  ]
  const note = t.source === 'stamps'
    ? '（老数据：墙钟按创建和结束时间推算，没有分段计时）'
    : t.source === 'last'
      ? '（老数据：只记了最后一段执行时长）'
      : ''
  return parts.join(' · ') + note
}

const pad2 = (n: number) => String(n).padStart(2, '0')

/**
 * 耗时，能跨天。一天以内同 formatDuration；一天以上写「8 天 02 小时」——审批
 * 挂了一周的运行，「192 小时 05 分」没人会去换算。coarse 只要最大的单位：
 * 「已等 8 天」。
 */
export function formatSpan(ms: number | null | undefined, opts?: { coarse?: boolean }): string {
  if (!isNum(ms) || ms < 0) return NONE
  const DAY = 86_400_000
  if (ms >= DAY) {
    const d = Math.floor(ms / DAY)
    const h = Math.floor((ms % DAY) / 3_600_000)
    return opts?.coarse || h === 0 ? `${d} 天` : `${d} 天 ${pad2(h)} 小时`
  }
  if (opts?.coarse) {
    if (ms >= 3_600_000) return `${Math.floor(ms / 3_600_000)} 小时`
    if (ms >= 60_000) return `${Math.floor(ms / 60_000)} 分钟`
    return '不到 1 分钟'
  }
  return formatDuration(ms)
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

/**
 * 没有 workflow_id、名字是后端默认的「临时图」：画布或问数据当场建的图，从没
 * 存成工作流。界面上按术语表叫它「未保存的工作流」——「图」只在技术细节里出现。
 */
export const UNSAVED_NAME = '未保存的工作流'
export const UNSAVED_HINT = '这次运行跑的是画布或问数据当场建的图，没有存成工作流，所以回不到画布里'
export const isUnsaved = (run: Pick<Run, 'workflow_id' | 'workflow_name'>): boolean =>
  !run.workflow_id && (!run.workflow_name || run.workflow_name === '临时图')
export const runName = (run: Pick<Run, 'workflow_id' | 'workflow_name'>): string =>
  isUnsaved(run) ? UNSAVED_NAME : run.workflow_name

/**
 * 这次运行实际用的记忆域和知识库。后端 RunOut 已经带着，types.ts 的 Run 还没
 * 声明（第二波不能改 types），先在这里收窄一次，别处不再写 as any。
 */
export function runScope(run: Run): { memory_scope?: string; collection?: string } {
  const r = run as Run & { memory_scope?: string | null; collection?: string | null }
  return { memory_scope: r.memory_scope ?? undefined, collection: r.collection ?? undefined }
}
