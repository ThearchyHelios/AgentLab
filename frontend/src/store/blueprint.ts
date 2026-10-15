import { create } from 'zustand'
import { api } from '../api/client'
import { streamBlueprint, streamFillJoints } from '../api/blueprintCopilot'
import { COPILOT_PHASE_TEXT, copilotLook, type CopilotOp } from '../run/decode'
import { BLUEPRINT_TEXT, LOOK_TEXT } from '../lib/terms'
import type { ConversationTurn } from '../types'
import type { AppSpec, FormSchema } from '../typesApp'
import type { Blueprint, BpAsk, BpOp, JointState, PlainIssue, TrialStart } from '../typesBlueprint'

/**
 * 蓝图页的状态（docs/design/normal-user-apps.md §2.2、§2.11、§4.2）：每个蓝图一桶——蓝图本身、对话的轮次、填关节的进度。
 *
 * 写法照 store/chat.ts：这里**只存原始流**（助手的操作），翻译归 run/decode.ts（decodeCopilot 的 blueprint 语境）；
 * 忙不忙按蓝图算、由轮次的相位推出来，取消句柄记在模块里按蓝图分开，切走再回来不会关错别人的流。
 *
 * 蓝图以服务端为准（P7）：助手的 bp_* 操作由服务端 BlueprintApplier 落地，流里的 final 带落地后的整份蓝图；人在卡片上的修改
 * 也是 bp_* 操作，经 PATCH /api/blueprints/{id} 落地、来源记成 user。前端不在本地模拟落地——流里的操作只用来讲过程，
 * 没收到 final 时流一结束就重取一次。
 *
 * 对话的每一轮由服务端落进蓝图对话（kind=blueprint 的 Conversation，blueprint.conversation_id，没有时第一轮建）：问题、
 * 回答或说明、助手问回来的话（meta.asked）、查看记录（meta.looks）；用户这一轮要是在回答上一轮的反问，服务端先把回答记在
 * 那个问题上（/asks/<i>/answer，来源记成你）再交给助手。前端不另记，免得一轮落两条。之前的轮次进页面时取回来，只显示问题、
 * 查看记录和回答——当时的改动已经落在卡片上，不重放。
 */

export interface BpTurn {
  id: string
  question: string
  /** 助手的原始操作流。thinking 在写入时合并 */
  ops: CopilotOp[]
  phase: 'running' | 'done' | 'error'
  /** 一句话进度 */
  status: string
  error?: string
  /** 这一句在回答哪个问题 */
  askId?: string | null
  startedAt: number
  endedAt?: number
}

export interface FillGroup {
  id: string
  joints: string[]
  status: 'running' | 'done' | 'failed'
  /** 这一组眼下在做什么（查看了什么、正在想） */
  line?: string
  message?: string
}

export interface FillState {
  phase: 'running' | 'done' | 'error'
  /** 按到达顺序；组之间并行 */
  groups: FillGroup[]
  /** 流里报来的关节状态（final 之前先显示） */
  joints: Record<string, Pick<JointState, 'status' | 'note'>>
  /** final 带来的实例化问题（带白话）：没填上的关节、骨架被改、图的校验 */
  problems?: PlainIssue[]
  error?: string
}

export interface BpEntry {
  blueprint: Blueprint | null
  loadState: 'loading' | 'ready' | 'error'
  loadError?: unknown
  /** 落过库的轮次（蓝图对话） */
  past: ConversationTurn[]
  historyFailed?: boolean
  turns: BpTurn[]
  fill: FillState | null
  /** 这一页里回答过的问题 id：服务端还没把回答记进蓝图之前，不再把它当成在等 */
  answered: string[]
  /** 最近一次按卡片生成的结果：应用骨架（试运行的输入表单从它的入口取）和检查出的问题 */
  instantiated?: { app_spec: AppSpec; problems: PlainIssue[] } | null
  /** 这一页里发起的最近一次试运行：用了什么输入、输入表单（服务端给了的话） */
  lastTrial?: { run_id: string; inputs?: Record<string, unknown> | null; form?: FormSchema | null } | null
}

/** 试运行的回包：服务端可以多给这次用的输入（按「多久更新」算出的最近一期）和输入表单 */
export type TrialStartOut = TrialStart & { inputs?: Record<string, unknown> | null; form?: FormSchema | null }

interface BlueprintState {
  byId: Record<string, BpEntry>
  /** force：手上有也再取一次 */
  load: (id: string, opts?: { force?: boolean }) => Promise<void>
  /** 新建或别处拿到的整份蓝图（新建、实例化、试运行、卡片修改的回包） */
  adopt: (bp: Blueprint) => void
  /** 对助手说一句；发出去了返回 true。蓝图没取到、或这个蓝图正有一轮在进行时不发 */
  send: (id: string, text: string, opts?: { askId?: string | null }) => boolean
  stop: (id: string) => void
  /** 卡片上的修改（bp_* 操作，来源记成 user）。成功交回落地后的蓝图；被拒抛出服务端的原因 */
  edit: (id: string, body: { ops?: BpOp[]; name?: string }) => Promise<Blueprint>
  /** 按卡片生成工作流和应用骨架（确定性关节落地，生成关节标待填）。被拒抛出服务端的原因 */
  instantiate: (id: string) => Promise<{ app_spec: AppSpec; problems: PlainIssue[] }>
  /** 用真数试一次：交回这次试运行的 id。inputs 不给 = 服务端按「多久更新」算最近一期 */
  trial: (id: string, inputs?: Record<string, unknown> | null) => Promise<string>
  /** 让助手填生成关节：groups 不给 = 还有没填上的关节的那几组（服务端按模板分组，组之间并行） */
  fill: (id: string, groups?: string[]) => boolean
  stopFill: (id: string) => void
}

const EMPTY_ENTRY: BpEntry = { blueprint: null, loadState: 'loading', past: [], turns: [], fill: null, answered: [] }

/** 各蓝图手上的流：对话一条、填关节一条。放模块里：store 里存函数会跟着每次 set 复制 */
const cancels = new Map<string, () => void>()

let seq = 0
const newId = () => `bt${Date.now().toString(36)}${seq++}`

/** 并进一条操作：thinking 连着的合成一条，免得一轮几百条增量 */
function pushOp(ops: CopilotOp[], op: CopilotOp): CopilotOp[] {
  const last = ops[ops.length - 1]
  if (op.op === 'thinking' && last?.op === 'thinking') {
    return [...ops.slice(0, -1), { ...last, delta: String(last.delta ?? '') + String(op.delta ?? '') }]
  }
  return [...ops, op]
}

const BP_EDIT_OPS = new Set(['bp_set', 'bp_add', 'bp_remove', 'bp_template'])

/** 蓝图对象形状对不对得上（流里的 final、接口回包） */
const isBlueprint = (v: unknown): v is Blueprint =>
  !!v && typeof v === 'object' && typeof (v as Blueprint).id === 'string' && !!(v as Blueprint).spec

export const useBlueprint = create<BlueprintState>((set, get) => {
  const entry = (id: string): BpEntry => get().byId[id] ?? EMPTY_ENTRY
  const patch = (id: string, fn: (e: BpEntry) => Partial<BpEntry>) =>
    set((s) => ({ byId: { ...s.byId, [id]: { ...(s.byId[id] ?? EMPTY_ENTRY), ...fn(s.byId[id] ?? EMPTY_ENTRY) } } }))
  const patchTurn = (id: string, turnId: string, fn: (t: BpTurn) => Partial<BpTurn>) =>
    patch(id, (e) => ({ turns: e.turns.map((t) => (t.id === turnId ? { ...t, ...fn(t) } : t)) }))
  const patchFill = (id: string, fn: (f: FillState) => Partial<FillState>) =>
    patch(id, (e) => (e.fill ? { fill: { ...e.fill, ...fn(e.fill) } } : {}))

  /** 不打扰地重取蓝图：流没带 final 时、填完关节之后。失败就留着手上那份 */
  const refresh = async (id: string) => {
    try {
      const bp = await api.blueprints.get(id)
      patch(id, () => ({ blueprint: bp, loadState: 'ready' }))
    } catch { /* 手上那份照用 */ }
  }

  /** 取回蓝图对话之前的轮次 */
  const loadHistory = async (id: string, conversationId: string | null) => {
    if (!conversationId) { patch(id, () => ({ past: [], historyFailed: false })); return }
    try {
      // 按蓝图取（权限跟着蓝图走），不走问数据的对话接口：没开问数据的使用者也读得到自己蓝图的对话
      const detail = await api.blueprints.conversation(id)
      patch(id, () => ({ past: (detail.turns ?? []).filter((t) => t.status !== 'running'), historyFailed: false }))
    } catch {
      patch(id, () => ({ historyFailed: true }))
    }
  }

  return {
    byId: {},

    load: async (id, opts) => {
      const cur = get().byId[id]
      if (cur?.blueprint && !opts?.force) return
      if (!cur?.blueprint) patch(id, () => ({ loadState: 'loading', loadError: undefined }))
      try {
        const bp = await api.blueprints.get(id)
        patch(id, () => ({ blueprint: bp, loadState: 'ready', loadError: undefined }))
        // 之前的轮次只在第一次取：重取蓝图（发布完、断线恢复）时这一页里的轮次已经在手上，再取一遍就重复了
        if (!cur?.blueprint) await loadHistory(id, bp.conversation_id)
      } catch (e) {
        // 手上有就留着（重取失败不该把页面清空）
        patch(id, (en) => (en.blueprint ? {} : { loadState: 'error', loadError: e }))
      }
    },

    adopt: (bp) => {
      const known = !!get().byId[bp.id]?.blueprint
      patch(bp.id, () => ({ blueprint: bp, loadState: 'ready', loadError: undefined }))
      if (!known) void loadHistory(bp.id, bp.conversation_id)
    },

    send: (id, text, opts) => {
      const e = entry(id)
      const instruction = text.trim()
      if (!instruction || !e.blueprint || e.turns.some((t) => t.phase === 'running')) return false
      const turnId = newId()
      // 回答的是哪个问题：只用来马上收起「在等你回答」；回答记在问题上由服务端做（它认最近一个没回答的反问）
      const askId = opts?.askId ?? null
      patch(id, (en) => ({
        turns: [...en.turns, {
          id: turnId, question: instruction, ops: [], phase: 'running', status: COPILOT_PHASE_TEXT.connecting,
          askId, startedAt: Date.now(),
        }],
        answered: askId && !en.answered.includes(askId) ? [...en.answered, askId] : en.answered,
      }))
      const got: CopilotOp[] = []
      let finalSeen = false
      const key = `${id}:chat`
      const stop = streamBlueprint(
        { blueprint_id: id, instruction },
        (op: CopilotOp) => {
          got.push(op)
          patchTurn(id, turnId, (t) => ({ ops: pushOp(t.ops, op) }))
          switch (op.op) {
            case 'heartbeat':
              patchTurn(id, turnId, () => ({
                status: op.phase === 'looking' ? LOOK_TEXT.running : COPILOT_PHASE_TEXT[String(op.phase)] ?? COPILOT_PHASE_TEXT.planning,
              }))
              break
            case 'look': {
              const look = copilotLook(op)
              if (look) patchTurn(id, turnId, () => ({ status: look.title }))
              break
            }
            case 'bp_ask':
            case 'ask':
              // 模型写成通用的 ask 也认（服务端存进蓝图时换成 bp_ask）
              patchTurn(id, turnId, () => ({ status: BLUEPRINT_TEXT.turnAsked }))
              break
            case 'final':
              // 服务端落地后的整份蓝图为准
              if (isBlueprint(op.blueprint)) { finalSeen = true; patch(id, () => ({ blueprint: op.blueprint as Blueprint })) }
              break
            case 'error':
              patchTurn(id, turnId, () => ({ phase: 'error', error: String(op.message ?? BLUEPRINT_TEXT.turnFailed) }))
              break
            default:
              if (BP_EDIT_OPS.has(op.op)) patchTurn(id, turnId, () => ({ status: BLUEPRINT_TEXT.turnRunning }))
          }
        },
        (error, info) => {
          if (cancels.get(key) === stop) cancels.delete(key)
          const message = info?.aborted ? BLUEPRINT_TEXT.turnStopped : error
          patchTurn(id, turnId, (t) => (t.phase === 'error' ? { endedAt: Date.now() }
            : message ? { phase: 'error', error: message, endedAt: Date.now() }
            : { phase: 'done', status: '', endedAt: Date.now() }))
          // 改动在服务端落地了，流里却没带整份蓝图（断流、老服务端）：重取一次
          if (!finalSeen && got.some((o) => BP_EDIT_OPS.has(o.op) || o.op === 'bp_ask' || o.op === 'ask')) void refresh(id)
        },
      )
      cancels.set(key, stop)
      return true
    },

    stop: (id) => {
      const key = `${id}:chat`
      cancels.get(key)?.()
      cancels.delete(key)
    },

    edit: async (id, body) => {
      const bp = await api.blueprints.patch(id, body)
      patch(id, () => ({ blueprint: bp, loadState: 'ready' }))
      return bp
    },

    instantiate: async (id) => {
      const res = await api.blueprints.instantiate(id)
      const instantiated = { app_spec: res.app_spec, problems: res.problems ?? [] }
      patch(id, () => ({ blueprint: res.blueprint, loadState: 'ready', instantiated }))
      return instantiated
    },

    trial: async (id, inputs) => {
      const res = await api.blueprints.trial(id, inputs ? { inputs } : {}) as TrialStartOut
      patch(id, () => ({
        blueprint: res.blueprint, loadState: 'ready',
        lastTrial: { run_id: res.run_id, inputs: res.inputs ?? inputs ?? null, form: res.form ?? null },
      }))
      return res.run_id
    },

    fill: (id, groups) => {
      const e = entry(id)
      if (!e.blueprint || e.fill?.phase === 'running') return false
      patch(id, () => ({ fill: { phase: 'running', groups: [], joints: {} } }))
      let finalSeen = false
      const key = `${id}:fill`
      /** 这一组：没见过就按到达顺序加上 */
      const group = (gid: string, fn: (g: FillGroup) => Partial<FillGroup>) => patchFill(id, (f) => {
        const known = f.groups.some((g) => g.id === gid)
        const base: FillGroup = { id: gid, joints: [], status: 'running' }
        return {
          groups: known ? f.groups.map((g) => (g.id === gid ? { ...g, ...fn(g) } : g)) : [...f.groups, { ...base, ...fn(base) }],
        }
      })
      const stop = streamFillJoints(
        { blueprint_id: id, ...(groups?.length ? { groups } : {}) },
        (op: CopilotOp) => {
          const gid = typeof op.group === 'string' && op.group ? op.group : null
          const joint = typeof op.joint === 'string' ? op.joint : null
          switch (op.op) {
            case 'group':
              // 组的结局：started / retrying（并行出错退回串行再试）是在填，filled 填好，failed 没填上（带原因或没过的检查）
              if (gid) {
                const issue = Array.isArray(op.issues) ? op.issues.find((i: any) => i?.message) : null
                group(gid, (g) => ({
                  joints: Array.isArray(op.joints) ? op.joints.map(String) : g.joints,
                  status: op.status === 'filled' ? 'done' : op.status === 'failed' ? 'failed' : 'running',
                  line: op.status === 'retrying' ? BLUEPRINT_TEXT.groupRetrying : g.line,
                  ...(op.message || issue ? { message: String(op.message ?? issue.message) } : {}),
                }))
              }
              break
            case 'plan':
              if (gid && op.summary) group(gid, () => ({ line: String(op.summary) }))
              break
            case 'check':
              if (gid) group(gid, () => ({ line: op.status === 'repairing' ? COPILOT_PHASE_TEXT.repairing : BLUEPRINT_TEXT.groupChecked }))
              break
            case 'fill_joint':
              if (joint) patchFill(id, (f) => ({ joints: { ...f.joints, [joint]: { status: 'filled' } } }))
              if (gid && joint) group(gid, (g) => ({ joints: g.joints.includes(joint) ? g.joints : [...g.joints, joint] }))
              break
            case 'heartbeat':
              if (gid) group(gid, () => ({ line: op.phase === 'looking' ? LOOK_TEXT.running : COPILOT_PHASE_TEXT[String(op.phase)] }))
              break
            case 'look': {
              const look = copilotLook(op)
              if (gid && look) group(gid, () => ({ line: look.title }))
              break
            }
            case 'final':
              if (isBlueprint(op.blueprint)) { finalSeen = true; patch(id, () => ({ blueprint: op.blueprint as Blueprint })) }
              if (Array.isArray(op.problems)) patchFill(id, () => ({ problems: op.problems as PlainIssue[] }))
              // 各组的结局以 final 为准（卡片在填的时候又改过的组，服务端判它不算数）
              for (const [g, r] of Object.entries((op.groups ?? {}) as Record<string, { status?: string }>)) {
                group(g, () => ({ status: r?.status === 'filled' ? 'done' : 'failed' }))
              }
              break
            case 'error':
              if (gid) group(gid, () => ({ status: 'failed', message: String(op.message ?? BLUEPRINT_TEXT.groupFailed) }))
              else patchFill(id, () => ({ error: String(op.message ?? BLUEPRINT_TEXT.fillFailed) }))
              break
          }
        },
        (error, info) => {
          if (cancels.get(key) === stop) cancels.delete(key)
          const message = info?.aborted ? BLUEPRINT_TEXT.turnStopped : error
          patchFill(id, (f) => {
            // 流结束了还标着在填的组：没等到它的结局，照实标没填上
            const groups = f.groups.map((g) => (g.status === 'running'
              ? { ...g, status: 'failed' as const, message: g.message ?? BLUEPRINT_TEXT.groupUnknown } : g))
            const bad = !!message || !!f.error || groups.some((g) => g.status === 'failed')
            return { phase: bad ? 'error' : 'done', groups, ...(message ? { error: message } : {}) }
          })
          if (!finalSeen) void refresh(id)
        },
      )
      cancels.set(key, stop)
      return true
    },

    stopFill: (id) => {
      const key = `${id}:fill`
      cancels.get(key)?.()
      cancels.delete(key)
    },
  }
})

/** 这个蓝图有没有一轮对话在进行 */
export const chatBusy = (e: BpEntry | undefined): boolean => !!e?.turns.some((t) => t.phase === 'running')

/**
 * 这时在等用户回答的问题：蓝图里最后一个还没回答、这一页也没回答过的；服务端没把问题记进蓝图时，退到最后一轮的
 * bp_ask（它以反问收尾）。有一轮在进行时不算在等
 */
export function openAsk(e: BpEntry | undefined): BpAsk | null {
  if (!e?.blueprint || chatBusy(e)) return null
  const asks = e.blueprint.spec.asks ?? []
  for (let i = asks.length - 1; i >= 0; i -= 1) {
    const a = asks[i]
    if (a.answer == null && !e.answered.includes(a.id)) return a
  }
  const last = e.turns[e.turns.length - 1]
  const op = last?.phase === 'done' ? [...last.ops].reverse().find((o) => o.op === 'bp_ask' || o.op === 'ask') : undefined
  if (!op) return null
  const id = typeof op.id === 'string' && op.id ? op.id : `turn-${last!.id}`
  // 蓝图里记着这个问题（已经回答了）：以蓝图为准
  if (asks.some((a) => a.id === id) || e.answered.includes(id)) return null
  return {
    id, question: String(op.question ?? ''), options: Array.isArray(op.options) ? op.options.map(String) : [],
    path: typeof op.path === 'string' ? op.path : null, answer: null,
  }
}

// 开发期把 store 挂到 window 上，检查脚本直接读状态做断言（同 window.__chat）。生产构建里去掉
if (import.meta.env.DEV) {
  ;(window as any).__blueprint = useBlueprint
}
