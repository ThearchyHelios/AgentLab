import type {
  CatalogAssistantChange, CatalogAssistantEvent, CatalogAssistantRequest, CatalogDetail, CatalogPatchChange, CatalogPatchPreview,
  CatalogPatchSubmit,
} from '../../types'
import { patchChangesOf, patchWhere } from '../../run/catalogPatch'

// ===========================================================================
// 表助手的纯函数：一轮回答怎么由事件拼起来、交给服务端的对话怎么截、待保存的修改怎么累积（同一项以最后一次提案为准）、
// 保存遇到 409 后怎么按预览重算。界面在 TableAssistant.tsx。
//
// 对话不存服务端：每一轮把前面的对话（最多 40 条、每条最多 4000 字）和界面上还没保存的修改一起交上去，服务端据此
// 知道已经提过什么、用户说过什么。
// ===========================================================================

/** 每一轮最多带几条对话（服务端同一个上限） */
export const ASSISTANT_MAX_MESSAGES = 40
/** 每条对话最多几个字（服务端同一个上限） */
export const ASSISTANT_MAX_CHARS = 4000
/** 一次最多保存几项（服务端 PATCH_MAX_CHANGES） */
export const ASSISTANT_MAX_SAVE = 200
/** 每一轮最多交几项待保存的修改（服务端同一个上限，多了整个请求 422）：多出来的只交最近的这么多项 */
export const ASSISTANT_MAX_PENDING = 200
/** 查询结果在界面上显示前几行（服务端最多给 10 行） */
export const QUERY_ROWS_SHOWN = 10

/** 一轮回答里按先后到来的几块：正文、它跑的只读查询、修改提案 */
export type TurnPart =
  | { kind: 'text'; text: string }
  /** rowCount 是取回的行数；truncated 为真时后面还有行（取回的只是前一截） */
  | { kind: 'query'; sql: string; columns: string[]; rows: unknown[][]; rowCount: number; truncated: boolean; error: string | null }
  | { kind: 'proposal'; id: string; version: number; changes: CatalogAssistantChange[] }

export interface UserTurn { id: string; role: 'user'; text: string }

export interface AssistantTurn {
  id: string
  role: 'assistant'
  parts: TurnPart[]
  /** streaming 还在回答；done 答完；stopped 点了停止；error 出错（开流前被拒、流里的 error 事件、连接中断） */
  state: 'streaming' | 'done' | 'stopped' | 'error'
  error?: string | null
  code?: string | null
  usage?: { input_tokens: number; output_tokens: number } | null
  queries?: number
}

export type Turn = UserTurn | AssistantTurn

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)
const num = (v: unknown, fallback = 0): number => (typeof v === 'number' && Number.isFinite(v) ? v : fallback)

/** 提案里的各项：形状同助手流里的 catalog_patch（认不出的项跳过），多一个给人看的位置 */
export function assistantChangesOf(raw: unknown): CatalogAssistantChange[] {
  const list = Array.isArray(raw) ? raw : []
  const parsed = patchChangesOf(list)
  return parsed.map((c) => {
    const src = list.find((x) => isObj(x) && x.path === c.path) as Record<string, unknown> | undefined
    const label = typeof src?.label === 'string' && src.label.trim() ? src.label.trim() : undefined
    return label ? { ...c, label } : c
  })
}

/** 一项给人看的位置：服务端写了就用它，没写按路径说 */
export const changeWhere = (c: CatalogAssistantChange): string => c.label ?? patchWhere(c)

/** 一个事件并进这一轮回答（返回新的一轮，不改入参）。proposalId：提案的编号（卡片、待保存的修改按它认出处） */
export function applyEvent(turn: AssistantTurn, ev: CatalogAssistantEvent, proposalId: string): AssistantTurn {
  const parts = [...turn.parts]
  switch (ev.type) {
    case 'text': {
      const delta = typeof ev.delta === 'string' ? ev.delta : ''
      if (!delta) return turn
      const last = parts.at(-1)
      if (last?.kind === 'text') parts[parts.length - 1] = { kind: 'text', text: last.text + delta }
      else parts.push({ kind: 'text', text: delta })
      return { ...turn, parts }
    }
    case 'query': {
      const columns = Array.isArray(ev.columns) ? ev.columns.map(String) : []
      const rows = Array.isArray(ev.rows) ? ev.rows.filter(Array.isArray).slice(0, QUERY_ROWS_SHOWN) as unknown[][] : []
      parts.push({
        kind: 'query', sql: typeof ev.sql === 'string' ? ev.sql : '', columns, rows, rowCount: num(ev.row_count, rows.length),
        truncated: ev.truncated === true, error: typeof ev.error === 'string' && ev.error.trim() ? ev.error : null,
      })
      return { ...turn, parts }
    }
    case 'proposal': {
      const changes = assistantChangesOf(ev.changes)
      if (!changes.length) return turn
      parts.push({ kind: 'proposal', id: proposalId, version: num(ev.version), changes })
      return { ...turn, parts }
    }
    case 'done': {
      const usage = isObj(ev.usage) ? { input_tokens: num(ev.usage.input_tokens), output_tokens: num(ev.usage.output_tokens) } : null
      return { ...turn, state: 'done', usage, queries: num(ev.queries) }
    }
    case 'error':
      return { ...turn, state: 'error', error: typeof ev.message === 'string' && ev.message ? ev.message : null, code: ev.code ?? null }
    default:
      return turn
  }
}

/** 这一轮回答的正文（各段正文接起来） */
export const turnText = (turn: AssistantTurn): string =>
  turn.parts.filter((p): p is Extract<TurnPart, { kind: 'text' }> => p.kind === 'text').map((p) => p.text).join('')

/** 这一轮里的提案 */
export const turnProposals = (turn: AssistantTurn) =>
  turn.parts.filter((p): p is Extract<TurnPart, { kind: 'proposal' }> => p.kind === 'proposal')

/**
 * 没有正文的一轮回答交给服务端时写成什么（只提了修改、只查了数据、没答完就停了）：保持一问一答交替。这几句只给模型看
 */
function placeholderOf(turn: AssistantTurn): string {
  const proposals = turnProposals(turn).reduce((n, p) => n + p.changes.length, 0)
  if (proposals) return `（提出了 ${proposals} 项修改提案）`
  if (turn.parts.some((p) => p.kind === 'query')) return '（查询了数据，没有写出回答）'
  return '（没有回答）'
}

/** 截到上限：每条最多 4000 字 */
const clip = (s: string) => ([...s].length > ASSISTANT_MAX_CHARS ? [...s].slice(0, ASSISTANT_MAX_CHARS).join('') : s)

/**
 * 交给服务端的对话：前面的对话加上这一条新问题，最多 40 条、每条最多 4000 字。超出时去掉最早的几条，并且总从用户的话
 * 开始（去掉打头的助手回答）
 */
export function historyOf(turns: Turn[], next: string): CatalogAssistantRequest['messages'] {
  const out: CatalogAssistantRequest['messages'] = []
  for (const t of turns) {
    if (t.role === 'user') out.push({ role: 'user', content: clip(t.text) })
    else out.push({ role: 'assistant', content: clip(turnText(t).trim() || placeholderOf(t)) })
  }
  out.push({ role: 'user', content: clip(next) })
  let tail = out.slice(-ASSISTANT_MAX_MESSAGES)
  while (tail.length > 1 && tail[0].role !== 'user') tail = tail.slice(1)
  return tail
}

// ---------------------------------------------------------------------------
// 待保存的修改：跨多轮累积，同一项（路径）以最后一次提案为准；可以逐项去掉、加回。保存成功后清空
// ---------------------------------------------------------------------------

export interface PendingChange extends CatalogAssistantChange {
  /** 来自哪一条提案 */
  origin: string
  /** 提案对照的目录版本：保存时带最早的那个，别人在那之后改过就 409，按最新内容重算后再保存 */
  version: number
}

/** 一条提案并进待保存的修改：同一路径的换成这一条，位置不动；已经是这个值且已确认的（same）不收 */
export function mergeProposal(pending: PendingChange[], changes: CatalogAssistantChange[], origin: string, version: number): PendingChange[] {
  const out = [...pending]
  for (const c of changes) {
    if (c.state === 'same') continue
    const next: PendingChange = { ...c, origin, version }
    const i = out.findIndex((p) => p.path === c.path)
    if (i >= 0) out[i] = next
    else out.push(next)
  }
  return out
}

export const dropPending = (pending: PendingChange[], path: string): PendingChange[] => pending.filter((p) => p.path !== path)

/** 交回保存、交给助手的那一份：路径、原样取值、理由 */
export function pendingSubmit(pending: PendingChange[]): CatalogPatchSubmit[] {
  return pending.map((p) => ({ path: p.path, value: p.value, ...(p.reason ? { reason: p.reason } : {}) }))
}

/**
 * 交给助手的待保存的修改：最多 ASSISTANT_MAX_PENDING 项（服务端同一个上限，多了整个请求 422）。超出时交最近的那些
 * （列表按头一次提出的先后排，后面的是新近提出的）；没交上去的模型不知道，再提同一项时以新的提案为准，不会丢
 */
export const pendingForAssistant = (pending: PendingChange[]): CatalogPatchSubmit[] =>
  pendingSubmit(pending.length > ASSISTANT_MAX_PENDING ? pending.slice(-ASSISTANT_MAX_PENDING) : pending)

/** 保存时带的版本：待保存的修改里最早的那条提案对照的版本 */
export const pendingVersion = (pending: PendingChange[]): number =>
  (pending.length ? Math.min(...pending.map((p) => p.version)) : 0)

/** 提案卡片上一项的处境：待保存（就是这一条）、改用了另一条提案的值、去掉了、已保存 */
export type ChangeState = 'pending' | 'replaced' | 'dropped' | 'saved'

export function changeStateOf(pending: PendingChange[], saved: ReadonlySet<string>, origin: string, path: string): ChangeState {
  if (saved.has(savedKey(origin, path))) return 'saved'
  const p = pending.find((x) => x.path === path)
  if (!p) return 'dropped'
  return p.origin === origin ? 'pending' : 'replaced'
}

export const savedKey = (origin: string, path: string) => `${origin}\u0000${path}`

/**
 * 保存遇到 409（别人刚改过这张表）后按预览重算：预览里有的项换上最新的改前、改后和状态，版本记成预览的；
 * 预览里没有的（已经不合法了，原因在 problems 里）去掉；已经是这个值且已确认的（same）也去掉，不用再存。
 *
 * submitted：交去保存、预览的那几项（同一个对象）。只重算它们：保存、重算期间又到的提案（新的项、同一项换成的新
 * 提案）不在预览里，原样留着，不能当成「已经不合法」悄悄去掉。不给时重算全部
 */
export function applyPreview(pending: PendingChange[], preview: CatalogPatchPreview, submitted?: readonly PendingChange[]): PendingChange[] {
  const byPath = new Map(preview.changes.map((c) => [c.path, c]))
  const mine = submitted ? new Set(submitted) : null
  const out: PendingChange[] = []
  for (const p of pending) {
    if (mine && !mine.has(p)) {
      out.push(p)
      continue
    }
    const c: CatalogPatchChange | undefined = byPath.get(p.path)
    if (!c || c.state === 'same') continue
    out.push({ ...p, before: c.before, before_status: c.before_status, after: c.after, value: c.value, state: c.state,
      ...(c.note ? { note: c.note } : {}), version: preview.version })
  }
  return out
}

// ---------------------------------------------------------------------------
// 开场：上次起草有留空的列时，引导先问这几列
// ---------------------------------------------------------------------------

/** 「补上……」按钮一次最多点名几列（再多的写「等 N 列」） */
export const BLANK_ASK_MAX = 20
/** 开场里最多举几列做例子 */
export const BLANK_EXAMPLES = 5

/**
 * 开场引导的写法。留空的列（模型列出了、中文名和含义却都没给）只有起草时记下的那几个列名（blank_sample，最多 5 个）
 * 靠得住：目录里还没有中文名和含义的列里，还有没写出来的列（部分完成的宽表）、库注释已经说清的列，不能当成留空的列举例。
 * - draft：拿到了起草时记下的列名。例子是其中还没补上的。记全了（列名数不少于留空的列数）时 remaining 是还没补上的
 *   几列，按钮写这个数、点名的正是这几列；没记全时 remaining 为 null，按钮不写数字、只点名这几列（模型那边另有背景）。
 * - catalog：拿不到（最近一次停止了、用的是再之前那次的留空列数；取任务失败；老服务端没有列名）。例子和按钮都改说
 *   「目录里还没有中文名和含义的列」，按钮上的数字就是这些列的总数，点名其中前 BLANK_ASK_MAX 列、再写「等 N 列」。
 * ask 为 null：没有可以点名的列，不给按钮
 */
export interface BlankLead {
  from: 'draft' | 'catalog'
  /** 上次起草留空的列数 */
  n: number
  examples: string[]
  /** 记全了时，留空的列里还没补上的几列；没记全、拿不到列名时为 null */
  remaining: number | null
  /** 目录里还没有中文名和含义的列数 */
  unnamed: number
  ask: { columns: string[]; total: number | null } | null
}

export function blankLeadOf(n: number, sample: readonly string[] | null, unnamed: readonly string[]): BlankLead {
  const named = sample?.filter((c) => !!c) ?? []
  if (named.length) {
    const open = new Set(unnamed)
    const still = named.filter((c) => open.has(c))
    const remaining = named.length >= n ? still.length : null
    return {
      from: 'draft', n, examples: still.slice(0, BLANK_EXAMPLES), remaining, unnamed: unnamed.length,
      ask: still.length ? { columns: still, total: remaining } : null,
    }
  }
  return {
    from: 'catalog', n, examples: unnamed.slice(0, BLANK_EXAMPLES), remaining: null, unnamed: unnamed.length,
    ask: unnamed.length ? { columns: unnamed.slice(0, BLANK_ASK_MAX), total: unnamed.length } : null,
  }
}

/** 还没有中文名和含义的列（表结构里的顺序；被驳回的不算有） */
export function blankColumnsOf(detail: CatalogDetail): string[] {
  const live = (it: { status: string } | undefined) => !!it && it.status !== 'rejected'
  return (detail.structure?.columns ?? []).map((c) => c.name)
    .filter((name) => {
      const items = detail.notes.columns?.[name]
      return !live(items?.label) && !live(items?.meaning)
    })
}
