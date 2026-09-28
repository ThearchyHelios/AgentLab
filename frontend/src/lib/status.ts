/**
 * 运行状态与节点状态的唯一一份文案和外观。
 *
 * 之前有五张映射表：StatusDot 写「成功 / 等待人工」，运行页轮次头写「完成 /
 * 等待人工介入」，问数据写「完成 / 出错了」，运行条写「等你确认」……同一屏上
 * 「成功」和「完成」并存，人会以为是两个状态。加一个状态就得去五处各补一遍。
 *
 * 每个状态同时有剪影（shape）、颜色和文字三条通道：去掉颜色还能靠剪影认出来，
 * 去掉剪影还能读字。颜色走 index.css 的状态令牌 --st-*；令牌还没定义时回落到
 * ok / warn / err / accent 这几个基础色。
 */

import type { RunStatus } from '../types'

/** 所有能被显示的状态。运行层和节点层共用一张表，其中几个只出现在一层 */
export type StatusCode =
  | 'idle'        // 节点：还没跑过
  | 'queued'      // 运行：排队；节点：上游都交付了、自己还没开始（推导）
  | 'running'
  | 'waiting'     // 等人审批
  | 'done'        // 节点跑完
  | 'succeeded'   // 运行跑完
  | 'failed'
  | 'cancelled'
  | 'suspended'   // 服务重启后挂起，可续跑
  | 'held'        // 后端的 interrupted 但没有待审批：已挂起 · 可续跑
  | 'skipped'     // 被 skip_if 跳过
  | 'blocked'     // 失败节点的下游，这次再也跑不到了（推导）
  | 'unreached'   // 运行结束时一次都没轮到，比如分支没走的那一侧（推导）

/**
 * 九种剪影，外加挂起用的暂停符和未运行用的小空心点。
 * 形状是给"去掉颜色也能认"准备的：灰度打印、色弱、投影仪偏色时都要分得开。
 */
export type StatusShape =
  | 'ring'          // 圆环：运行中
  | 'dashed-ring'   // 虚线环：排队
  | 'square'        // 实心圆角方（带勾）：完成
  | 'diamond'       // 菱形：等人
  | 'triangle'      // 三角：失败
  | 'slashed'       // 斜杠圆：跳过
  | 'stop'          // 实心小方：取消
  | 'dashed-x'      // 虚线方加 ×：阻断
  | 'bar'           // 横杠：未到达
  | 'pause'         // 两竖条：挂起
  | 'dot'           // 小空心点：未运行

export interface StatusMeta {
  code: StatusCode
  /** 完整叫法，状态标签和 tooltip 用 */
  label: string
  /** 窄处用的短叫法（节点卡的运行槽、HUD 计数） */
  short: string
  shape: StatusShape
  /** 前景色，CSS 颜色表达式 */
  color: string
  /** 淡底色，CSS 颜色表达式 */
  soft: string
  /** 是不是异常态。正常态安静、异常态醒目：只有它们该用底色和强调 */
  alert: boolean
  /** 是不是终态（这次运行里不会再变了） */
  terminal: boolean
  /** 推导出来的状态：tooltip 里要写明"推导"，别让人以为是后端说的 */
  derived?: boolean
  /** 一句话解释，放 title */
  hint?: string
}

const tok = (name: string, fallback: string) => ({
  color: `var(--st-${name}, ${fallback})`,
  soft: `var(--st-${name}-soft, color-mix(in srgb, ${fallback} 14%, transparent))`,
})

export const STATUS: Record<StatusCode, StatusMeta> = {
  idle: {
    code: 'idle', label: '未运行', short: '未运行', shape: 'dot', alert: false, terminal: false,
    ...tok('idle', 'var(--text-faint)'),
  },
  queued: {
    code: 'queued', label: '排队中', short: '排队', shape: 'dashed-ring', alert: false, terminal: false,
    hint: '上游已经交付，等同一步里别的节点跑完', ...tok('queued', 'var(--text-dim)'),
  },
  running: {
    code: 'running', label: '运行中', short: '运行中', shape: 'ring', alert: false, terminal: false,
    ...tok('running', 'var(--accent)'),
  },
  waiting: {
    code: 'waiting', label: '等待审批', short: '待审批', shape: 'diamond', alert: true, terminal: false,
    hint: '停在人工审批上，处理之后接着跑', ...tok('waiting', 'var(--warn)'),
  },
  done: {
    code: 'done', label: '已完成', short: '完成', shape: 'square', alert: false, terminal: true,
    ...tok('done', 'var(--ok)'),
  },
  succeeded: {
    code: 'succeeded', label: '已完成', short: '完成', shape: 'square', alert: false, terminal: true,
    hint: '执行层面跑完了。结论是否可用看出具档位和复核', ...tok('done', 'var(--ok)'),
  },
  failed: {
    code: 'failed', label: '失败', short: '失败', shape: 'triangle', alert: true, terminal: true,
    ...tok('failed', 'var(--err)'),
  },
  cancelled: {
    code: 'cancelled', label: '已取消', short: '已取消', shape: 'stop', alert: false, terminal: true,
    ...tok('cancelled', 'var(--text-faint)'),
  },
  suspended: {
    code: 'suspended', label: '已中断（服务重启）', short: '已中断', shape: 'pause', alert: true, terminal: true,
    hint: '服务重启时这次运行还没结束，已从断点挂起，可以接着跑', ...tok('suspended', 'var(--warn)'),
  },
  held: {
    code: 'held', label: '已挂起 · 可续跑', short: '已挂起', shape: 'pause', alert: true, terminal: true,
    hint: '运行停在断点上，但没有待处理的审批——多半是服务重启打断的，可以接着跑',
    ...tok('suspended', 'var(--warn)'),
  },
  skipped: {
    code: 'skipped', label: '已跳过', short: '跳过', shape: 'slashed', alert: false, terminal: true,
    hint: '跳过条件成立，这一步没有执行', ...tok('skipped', 'var(--text-faint)'),
  },
  blocked: {
    code: 'blocked', label: '已阻断', short: '阻断', shape: 'dashed-x', alert: true, terminal: true,
    derived: true, hint: '推导：上游失败，这次运行再也走不到这里', ...tok('blocked', 'var(--err)'),
  },
  unreached: {
    code: 'unreached', label: '未到达', short: '未到达', shape: 'bar', alert: false, terminal: true,
    derived: true, hint: '推导：运行结束时一次都没轮到，比如分支没走的那一侧', ...tok('unreached', 'var(--text-faint)'),
  },
}

/** 旧代码和后端里的别名。未知状态码原样落到 idle 的外观上，文字照写原码 */
const ALIAS: Record<string, StatusCode> = {
  success: 'succeeded', ok: 'succeeded', completed: 'succeeded', finished: 'succeeded',
  error: 'failed', canceled: 'cancelled', pending: 'queued', interrupted: 'waiting',
}

/**
 * 把后端的运行状态落到显示用的状态上。
 *
 * 后端的 interrupted 有两种：停在审批上（等待审批），和服务重启打断后没有
 * 待审批（已挂起 · 可续跑）。二者要看审批列表里有没有这条运行的 pending
 * 才分得开——传 pendingApproval。不知道时（没传）按等待审批处理，这是
 * interrupted 最常见的来源，也是改造前的说法，不会比原来更错。
 */
export function resolveStatus(status: string | null | undefined, opts?: { pendingApproval?: boolean }): StatusCode {
  if (!status) return 'idle'
  if (status === 'interrupted') return opts?.pendingApproval === false ? 'held' : 'waiting'
  if (status in STATUS) return status as StatusCode
  return ALIAS[status] ?? 'idle'
}

/** 状态的完整外观。未知状态码回落到 idle 的外观，文字写原码，免得静默吞掉 */
export function statusMeta(status: string | null | undefined, opts?: { pendingApproval?: boolean }): StatusMeta {
  const code = resolveStatus(status, opts)
  const meta = STATUS[code]
  if (status && code === 'idle' && status !== 'idle' && !(status in ALIAS)) {
    return { ...meta, label: status, short: status }
  }
  return meta
}

/** 只要文字 */
export function statusLabel(status: string | null | undefined, opts?: { pendingApproval?: boolean; short?: boolean }): string {
  const meta = statusMeta(status, opts)
  return opts?.short ? meta.short : meta.label
}

/**
 * 运行列表的状态筛选用：按显示顺序排好的运行状态。
 * 这是显示用的码，不能直接当查询参数——waiting / held 在后端都是 interrupted，
 * 查询前用 serverStatusOf 换回去。
 */
export const RUN_STATUS_ORDER: StatusCode[] = [
  'running', 'waiting', 'held', 'failed', 'succeeded', 'cancelled', 'queued',
]

/**
 * 显示用的状态码换回后端 runs.status 认的值，给 api.runs.list({status}) 用。
 *
 * waiting、held、suspended 在后端都是 interrupted：按它查回来的是两种混在一起，
 * 要再用 resolveStatus(run.status, {pendingApproval: hasPendingApproval(approvals,
 * run.id)}) 在前端分开。节点才有的状态（idle / skipped / blocked / unreached）
 * 不是运行状态，返回 null。
 */
export function serverStatusOf(code: StatusCode): RunStatus | null {
  switch (code) {
    case 'waiting': case 'held': case 'suspended': return 'interrupted'
    case 'done': case 'succeeded': return 'succeeded'
    case 'queued': case 'running': case 'failed': case 'cancelled': return code
    default: return null
  }
}

/**
 * 运行是不是还"活着"（会有新事件）。held / suspended 虽然后端写的是
 * interrupted，但不会自己动，不算。
 */
export function isLiveStatus(status: string | null | undefined, opts?: { pendingApproval?: boolean }): boolean {
  const code = resolveStatus(status, opts)
  return code === 'queued' || code === 'running' || code === 'waiting'
}
