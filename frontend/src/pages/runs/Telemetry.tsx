import { useState, type ReactNode } from 'react'
import { ChevronDown, Shield, ShieldAlert, ShieldCheck } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../../api/client'
import { StatusPill, toast } from '../../components/ui'
import {
  NONE, formatClock, formatCost, formatDateTime, formatNumber, formatTime, formatTokens, shortId,
} from '../../lib/format'
import { type StatusCode } from '../../lib/status'
import { isExecuting, liveAt, project, type Projection, type RunPhase, type Trace } from '../../run/trace'
import { useRunClock } from '../../run/useRunClock'
import type { Approval, Run } from '../../types'
import {
  LONG_WAIT_MS, UNSAVED_HINT, ageMs, formatSpan, isUnsaved, runName, runScope, runTiming, type RunTiming,
} from './model'
import { CLOCK_MAX_MS, CopyValue, useNow } from './parts'

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

// -------------------------------------------------------------------------
// 遥测条：状态 · 墙钟 · 执行 · 等人 · 节点 · 用量
// -------------------------------------------------------------------------

export interface RunClocks {
  wall: number | null
  active: number | null
  wait: number | null
  proj: Projection | null
  /** usage：后端分段计时；trace：事件时间戳投影；stamps / last / none：老数据的退路 */
  source: 'usage' | 'trace' | RunTiming['source']
}

/**
 * 三种时长从哪来：跑完了以后端的 usage 为准；进行中、或者老数据没有 wall_ms，用
 * 航迹（事件时间戳）投影到 now；连事件时间都没有，退回 runTiming 的老口径。
 * 详情头和时间线的轮次头都读它，两处的数不会对不上。
 */
export function runClocks(run: Run, trace: Trace, phase: RunPhase, streaming: boolean, now: number): RunClocks {
  const usage = run.usage ?? {}
  const settled = !streaming && (phase === 'succeeded' || phase === 'failed' || phase === 'cancelled')
  const proj = trace.timed && trace.startedAt != null ? project(trace, liveAt(trace, now)) : null
  if (settled && isNum(usage.wall_ms)) {
    return {
      wall: usage.wall_ms,
      active: isNum(usage.active_ms) ? usage.active_ms : isNum(usage.duration_ms) ? usage.duration_ms : null,
      wait: isNum(usage.wait_ms) ? usage.wait_ms : 0,
      proj,
      source: 'usage',
    }
  }
  if (proj) return { wall: proj.elapsedMs, active: proj.activeMs, wait: proj.waitMs, proj, source: 'trace' }
  const t = runTiming(run)
  return { wall: t.wallMs, active: t.activeMs, wait: t.waitMs, proj: null, source: t.source }
}

/**
 * 详情头的一排读数。和画布 HUD 同一种语汇：小标签、大号等宽数字、细线分隔。
 *
 * 三种时长分开写：墙钟是从第一次开始到结束，执行是各段之和，等人是审批挂着
 * 的时间。以前只有一个 usage.duration_ms，审批恢复过的运行只剩最后一段——
 * 87 秒的运行写成 22 ms，和节点上的 1 分 14 秒同屏矛盾。
 *
 * 数从哪来见 runClocks。都拿不到就写「—」，不猜。
 */
export function Telemetry({ run, trace, phase, code, streaming, pending, labelOf }: {
  run: Run
  trace: Trace
  phase: RunPhase
  code: StatusCode
  streaming: boolean
  pending: Approval[] | null
  labelOf: (id?: string | null) => string | undefined
}) {
  const executing = streaming && (phase === 'running' || phase === 'queued')
  const clock = useRunClock(executing)
  // 等审批的时候数字按分钟涨就够了
  const slow = useNow(30_000, phase === 'waiting')
  const now = executing ? clock : Math.max(slow, Date.now())

  const usage = run.usage ?? {}
  const settled = !streaming && (phase === 'succeeded' || phase === 'failed' || phase === 'cancelled')
  const { wall, active, wait, proj, source } = runClocks(run, trace, phase, streaming, now)

  const tokensSettled = (usage.total_tokens as number | undefined)
    ?? (isNum(usage.input_tokens) || isNum(usage.output_tokens)
      ? (usage.input_tokens ?? 0) + (usage.output_tokens ?? 0) : undefined)
  const liveTokens = trace.tokensIn + trace.tokensOut
  // 进行中只看得到已经回报的那部分：写「≥」，终态再换成后端的权威总数
  const tokens = settled || !streaming ? tokensSettled ?? (liveTokens || undefined) : liveTokens
  const cost = settled || !streaming ? (isNum(usage.cost_usd) ? usage.cost_usd : trace.costUsd || undefined) : trace.costUsd
  const approx = streaming && !settled

  const running = Object.values(trace.nodes).filter(isExecuting)
  const waitingId = trace.waitingNodeId ?? pending?.[0]?.node_id
  const waitedMs = trace.waitingSince != null ? liveAt(trace, now) - trace.waitingSince
    : ageMs(pending?.[0]?.created_at, now)

  // 纯文字：窄屏时格子会截断，完整的一句放进 title
  let sub: string | null = null
  if (code === 'running' && running.length) {
    sub = `当前「${labelOf(running[0].id)}」${running.length > 1 ? ` 等 ${running.length} 个` : ''}`
  } else if (code === 'queued') {
    sub = '等待开始'
  } else if (code === 'waiting' && waitingId) {
    sub = `等「${labelOf(waitingId)}」${waitedMs != null ? ` · 已等 ${formatSpan(waitedMs, { coarse: true })}` : ''}`
  } else if (code === 'failed') {
    const id = run.error_node_id ?? trace.failedNodeId
    sub = id ? `失败于「${labelOf(id)}」` : '没定位到节点'
  } else if (code === 'held' || code === 'suspended') {
    sub = '断点还在，可接着跑'
  } else if (code === 'cancelled') {
    sub = run.error && run.error !== '用户取消' ? run.error : '用户取消'
  }

  const nodesTotal = proj?.nodesTotal ?? 0
  const drives = trace.drives.length
  const waits = trace.waits.length
  // 秒表只给一小时以内、没停下等过人的。墙钟从第一次开始算：等了八天的审批批掉
  // 之后接着跑，秒表读成「213:46:45.6」，谁也读不出那是多久——改写跨度，按分钟变
  const wallTicks = executing && isNum(wall) && wall < CLOCK_MAX_MS && !waits
  const activeTicks = executing && isNum(active) && active < CLOCK_MAX_MS

  return (
    <div
      className="grid border-t"
      style={{ gridTemplateColumns: 'minmax(140px, 1.5fr) repeat(5, minmax(76px, 1fr))' }}
      data-run-telemetry=""
    >
      <Cell label="状态" first>
        <StatusPill status={code} className="-ml-1.5 self-start" />
        {sub && <Sub title={sub}>{sub}</Sub>}
      </Cell>
      <Cell label="墙钟" title="从第一次开始到结束（或此刻）">
        <Value data="wall">{wallTicks ? formatClock(wall) : <Span ms={wall} />}</Value>
        <Sub>
          {executing ? (waits ? '至今，含等人' : '计时中')
            : phase === 'waiting' ? '至今，还在等审批'
            : code === 'held' || code === 'suspended' ? '到挂起为止'
            : source === 'stamps' ? '按起止时间推算' : '发起到结束'}
        </Sub>
      </Cell>
      <Cell label="执行" title="各段执行时长之和：审批恢复、接着跑的每一段都算">
        <Value data="active">{activeTicks ? formatClock(active) : <Span ms={active} />}</Value>
        <Sub>{drives > 1 ? `分 ${drives} 段执行` : ' '}</Sub>
      </Cell>
      <Cell label="等人" title="挂在人工审批上的总时长">
        <Value data="wait" tone={phase === 'waiting' && (waitedMs ?? 0) >= LONG_WAIT_MS ? 'var(--st-waiting)' : undefined}>
          <Span ms={wait} />
        </Value>
        <Sub>{waits ? `审批 ${waits} 次` : ' '}</Sub>
      </Cell>
      <Cell label="节点" title="已完成（含跳过）/ 图上的节点总数">
        <Value data="nodes">{nodesTotal ? <>{proj!.nodesDone}<span className="text-faint">/{nodesTotal}</span></> : NONE}</Value>
        <Sub>{executing && proj && proj.parallelNow > 1 ? `${proj.parallelNow} 个并行` : ' '}</Sub>
      </Cell>
      <Cell label="用量" title={approx ? '进行中：只含已经回报的调用，跑完后换成后端的权威总数' : 'tokens 与成本'}>
        <Value data="tokens">
          {tokens ? <>{approx && '≥ '}{formatTokens(tokens, { compact: true })}</> : NONE}
        </Value>
        <Sub>{isNum(cost) && cost > 0 ? `${approx ? '≥ ' : ''}${formatCost(cost)}` : isNum(cost) ? formatCost(cost) : ' '}</Sub>
      </Cell>
    </div>
  )
}

function Cell({ label, title, first, children }: { label: string; title?: string; first?: boolean; children: ReactNode }) {
  return (
    <div className={clsx('flex min-w-0 flex-col gap-0.5 px-3 py-2 xl:px-4', !first && 'border-l')} title={title}>
      <div className="text-2xs text-faint">{label}</div>
      {children}
    </div>
  )
}

/**
 * 跨天的时长：宽屏写全「8 天 09 小时」，窄屏只写「8 天」，完整的放 title。
 * 格子在 1024 宽下只有七十来像素，写全就只剩「8 天 09…」
 */
function Span({ ms }: { ms: number | null }) {
  const full = formatSpan(ms)
  if (!isNum(ms) || ms < 86_400_000) return <>{full}</>
  return (
    <span title={full}>
      <span className="xl:hidden">{formatSpan(ms, { coarse: true })}</span>
      <span className="hidden xl:inline">{full}</span>
    </span>
  )
}

function Value({ children, tone, data }: { children: ReactNode; tone?: string; data?: string }) {
  return (
    <div className="mono tnum truncate text-base leading-6 text-fg" style={tone ? { color: tone } : undefined}
         data-telemetry={data}>
      {children}
    </div>
  )
}

function Sub({ children, title }: { children: ReactNode; title?: string }) {
  return <div className="truncate text-2xs text-faint" title={title}>{children}</div>
}

// -------------------------------------------------------------------------
// 封存凭证
// -------------------------------------------------------------------------

interface VerifyResult {
  sealed: boolean
  ok: boolean | null
  message: string
  events?: number
  sealed_at?: number
  legacy?: boolean
  /** 什么时候核对的（ms） */
  at: number
}

const VERIFY_KEY = (id: string) => `agentlab.verify.${id}`

function readVerify(id: string): VerifyResult | null {
  try {
    const raw = sessionStorage.getItem(VERIFY_KEY(id))
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

function writeVerify(id: string, v: VerifyResult) {
  try { sessionStorage.setItem(VERIFY_KEY(id), JSON.stringify(v)) } catch { /* 存不下就只在这一屏有效 */ }
}

/**
 * 封存凭证。「事件表是审计凭证、终态时封存清单」是这个产品和普通 agent 平台的
 * 分界线，以前却只是一个没字的盾牌图标，核对结果 4 秒 toast 就没了，截图给同事
 * 看时拿不出证据。现在凭证常驻在详情头下面，核对结果留在上面（按运行记在
 * sessionStorage，切回来还在），展开能看到完整哈希、封存范围和运行环境。
 */
export function ProvenanceBar({ run, eventCount, phase }: { run: Run; eventCount: number; phase: RunPhase }) {
  const [verify, setVerify] = useState<VerifyResult | null>(() => readVerify(run.id))
  const [verifying, setVerifying] = useState(false)
  const [fresh, setFresh] = useState(false)
  const [open, setOpen] = useState(false)
  const sealed = !!run.manifest_hash

  const doVerify = async () => {
    setVerifying(true)
    setFresh(false)
    try {
      const res = await api.runs.verify(run.id) as Omit<VerifyResult, 'at'>
      const v = { ...res, at: Date.now() }
      setVerify(v)
      writeVerify(run.id, v)
      setFresh(true)
    } catch (e) {
      toast.error(e)
    } finally {
      setVerifying(false)
    }
  }

  const unsealedWhy = phase === 'running' || phase === 'queued'
    ? '跑完才封存清单'
    : phase === 'waiting'
      ? '停在审批上，跑完才封存'
      : '这次运行没有封存清单'

  // 扫描线的时长按事件条数估：重算的就是这些事件
  const scanMs = Math.min(2400, Math.max(600, eventCount * 0.8))

  return (
    <div className="border-t" data-run-provenance="">
      <div className="relative flex min-h-8 flex-wrap items-center gap-x-2 gap-y-1 px-4 py-1 text-2xs">
        {!sealed ? (
          <span className="flex items-center gap-1.5 text-faint" data-seal="unsealed">
            <Shield size={12} aria-hidden /> 未封存 · {unsealedWhy}
          </span>
        ) : (
          <>
            <span className="flex items-center gap-1.5 text-dim" data-seal="sealed">
              <ShieldCheck size={12} aria-hidden /> 已封存
            </span>
            <span className="text-faint">清单</span>
            <CopyValue value={run.manifest_hash!} label="清单哈希" display={`${run.manifest_hash!.slice(0, 10)}…`}
                       className="text-dim" />
            {run.manifest_seq != null && (
              <span className="tnum text-faint">· 封存到第 {formatNumber(run.manifest_seq)} 条事件</span>
            )}
          </>
        )}
        {run.version != null && (
          <>
            <span className="text-faint">· 版本 v{run.version}</span>
            {run.version_hash && (
              <CopyValue value={run.version_hash} label="版本哈希" display={`${run.version_hash.slice(0, 8)}…`}
                         className="text-faint" />
            )}
          </>
        )}
        <span className="flex-1" />
        {sealed && <VerifyState verify={verify} verifying={verifying} fresh={fresh} />}
        {sealed && (
          <button type="button" className="btn btn-xs btn-ghost" disabled={verifying} onClick={() => void doVerify()}
                  title="重算封存范围内的事件哈希，和封存时记下的清单对一下" data-verify-btn="">
            {verify ? '重新核对' : '核对'}
          </button>
        )}
        <button
          type="button"
          className="btn btn-xs btn-ghost"
          aria-expanded={open}
          aria-controls={`prov-${run.id}`}
          onClick={() => setOpen((v) => !v)}
          data-prov-toggle=""
        >
          凭证详情
          <ChevronDown size={11} aria-hidden style={{ transform: open ? 'rotate(180deg)' : undefined, transition: 'transform var(--dur-fast)' }} />
        </button>
        {verifying && <span className="runs-verify-scan" style={{ ['--scan-ms' as string]: `${scanMs}ms` }} aria-hidden />}
      </div>
      {open && <ProvenanceDetail id={`prov-${run.id}`} run={run} verify={verify} eventCount={eventCount} />}
    </div>
  )
}

function VerifyState({ verify, verifying, fresh }: { verify: VerifyResult | null; verifying: boolean; fresh: boolean }) {
  if (verifying) {
    return <span className="text-dim" role="status" data-verify="running">核对中…</span>
  }
  if (!verify) return <span className="text-faint" data-verify="none">还没核对过</span>
  const good = verify.ok === true
  const Icon = good ? ShieldCheck : ShieldAlert
  return (
    <span
      role="status"
      className="flex min-w-0 items-center gap-1"
      style={{ color: good ? 'var(--st-done)' : 'var(--st-failed)' }}
      title={`${verify.message}（${formatDateTime(verify.at)} 核对${verify.legacy ? '，按老口径还原封存范围' : ''}）`}
      data-verify={good ? 'ok' : 'mismatch'}
    >
      <Icon size={12} className={fresh ? 'runs-stamp' : undefined} aria-hidden />
      <span className="font-medium">{good ? '已核验' : '不一致'}</span>
      <span className="tnum truncate opacity-80">
        {formatTime(verify.at)}
        {good && isNum(verify.events) ? ` · ${formatNumber(verify.events)} 条事件与清单一致` : ` · ${verify.message}`}
      </span>
    </span>
  )
}

function ProvenanceDetail({ id, run, verify, eventCount }: {
  id: string; run: Run; verify: VerifyResult | null; eventCount: number
}) {
  const rows: [string, ReactNode][] = [
    ['运行 ID', <CopyValue value={run.id} label="运行 ID" />],
    ['工作流', isUnsaved(run)
      ? <span className="text-faint" title={UNSAVED_HINT}>{runName(run)}</span>
      : <span className="flex min-w-0 items-center gap-1">{run.workflow_name}
          {run.workflow_id && <CopyValue value={run.workflow_id} label="工作流 ID" display={shortId(run.workflow_id, 8)} className="text-faint" />}
        </span>],
    ['版本', run.version != null
      ? <span className="flex min-w-0 items-center gap-1">v{run.version}
          {run.version_hash && <CopyValue value={run.version_hash} label="版本哈希" className="text-faint" />}
        </span>
      : <span className="text-faint">探索运行不绑定版本</span>],
    ['清单哈希', run.manifest_hash ? <CopyValue value={run.manifest_hash} label="清单哈希" /> : <span className="text-faint">未封存</span>],
    ['封存范围', run.manifest_seq != null
      ? <span className="tnum">第 1–{formatNumber(run.manifest_seq)} 条事件（现有 {formatNumber(eventCount)} 条）</span>
      : <span className="tnum text-faint">{run.manifest_hash ? '老运行：封存时没记范围，核对时按当时口径还原' : NONE}（现有 {formatNumber(eventCount)} 条）</span>],
    ['核对', verify
      ? <span style={{ color: verify.ok ? 'var(--st-done)' : 'var(--st-failed)' }}>
          {verify.message} · {formatDateTime(verify.at)}{verify.legacy ? ' · 老口径' : ''}
        </span>
      : <span className="text-faint">还没核对过</span>],
    ['记忆域 / 知识库', <span className="mono">{runScope(run).memory_scope ?? NONE} / {runScope(run).collection ?? NONE}</span>],
    ['发起', <span className="tnum">{formatDateTime(run.created_at ?? null)} · {run.started_by || '未署名'}</span>],
    ['开始 / 结束', <span className="tnum">{formatDateTime(run.started_at ?? null)} → {formatDateTime(run.finished_at ?? null)}</span>],
  ]
  return (
    <dl id={id} className="fade-up grid gap-x-4 gap-y-1 border-t bg-elev px-4 py-2.5 text-2xs"
        style={{ gridTemplateColumns: 'max-content minmax(0, 1fr)' }} data-prov-detail="">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-faint">{k}</dt>
          <dd className="min-w-0 truncate text-dim">{v}</dd>
        </div>
      ))}
    </dl>
  )
}
