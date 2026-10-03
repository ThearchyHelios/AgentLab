import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { Code2, Copy, GitFork, Link2, Rewind, Square, SquareArrowOutUpRight, Trash2 } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../../api/client'
import {
  ErrorState, IconButton, Skeleton, Spinner, confirmDialog, promptDialog, toast, useTicker,
} from '../../components/ui'
import { formatDateTime, formatNumber, formatTime, shortId } from '../../lib/format'
import { resolveStatus, statusLabel, type StatusCode } from '../../lib/status'
import { AssistantStream, type StreamTurn, type TurnFailure } from '../../run/AssistantStream'
import { catalogDrift, decodePhase, decodeRun, exitLabels, summarizeRun, type RunFinal } from '../../run/decode'
import { sourceHandles } from '../../canvas/nodeDefs'
import { ApprovalCard } from '../../run/RunPanel'
import { runStatusOf, type RunPhase } from '../../run/trace'
import { useCatalog } from '../../store/catalog'
import type { Approval, Run, RunEvent } from '../../types'
import { CatalogDriftBanner, FailedBanner, FeedbackStrip, HeldBanner, WaitingBanner, type Feedback } from './Banners'
import { localActor } from '../../lib/actor'
import { explainRunError, explainStartError } from '../../lib/explain'
import { canLeave, leavePass } from '../../lib/leave'
import { UNSAVED_HINT, isUnsaved, runName } from '../../lib/terms'
import {
  asView, duplicateNames, graphShape, idTail, isLiveRun, runScope, type DetailView,
} from './model'
import { ArtifactsPane, useRunArtifacts } from './ArtifactsPane'
import { EvidencePane } from './EvidencePane'
import { EVIDENCE_AUDIT_TEXT } from '../../lib/terms'
import { ClassChip, MoreMenu, RunTabs, TierChip, copyText, type MenuItem, type TabItem } from './parts'
import { ProvenanceBar, Telemetry, runClocks } from './Telemetry'
import { TracePane } from './TracePane'
import { useRunDetail } from './useRunDetail'

/**
 * 一次运行的详情：头部读数、凭证、排错路径，下面分三个视图——
 *
 * - 时间线：和画布助手栏、问数据页同一个解码器、同一个组件画的逐步叙述。三处各写
 *   一套的话，同一次运行会被讲成三个不同的故事，而用户没法判断哪个是真的；
 * - 航迹：和画布底部同一个航迹坞，按时间摊开，能拖到任意一刻回放；
 * - 工件：这次运行按内容哈希存下的每一件证据和产出。
 *
 * 视图记在地址的 ?view= 里，换一条运行时留在同一个视图；?at= 是航迹上的时刻
 * （相对开始的毫秒），从工件、分享的链接直接落到那一刻。
 */
export function RunDetailView({ runId, onChange, onDeleted }: {
  runId: string
  /** 这条运行变了（状态、用量）：列表原地更新那一行 */
  onChange: (run: Run) => void
  onDeleted: (id: string) => void
}) {
  const navigate = useNavigate()
  const { search } = useLocation()
  const [params, setParams] = useSearchParams()
  const d = useRunDetail(runId, onChange)
  const workflows = useCatalog((s) => s.workflows)
  const catalogLoaded = useCatalog((s) => s.loaded)
  const refreshCatalog = useCatalog((s) => s.refresh)
  const refreshApprovals = useCatalog((s) => s.refreshApprovals)
  const [raw, setRaw] = useState(false)
  const [busy, setBusy] = useState<null | 'stop' | 'continue' | 'rerun' | 'extract' | 'delete' | 'abandon'>(null)
  const [feedback, setFeedback] = useState<Feedback | null>(null)
  const streamBox = useRef<HTMLDivElement>(null)
  const now = useTicker(60_000)

  useEffect(() => {
    if (d.state !== 'missing') return
    toast.info('该运行记录不存在，可能已被删除')
    navigate({ pathname: '/runs', search }, { replace: true })
  }, [d.state, navigate, search])

  const run = d.run
  const pendingFlag = d.pending == null ? undefined : d.pending.length > 0
  // 接着流时事件就是事实；不接流时把 GET 到的状态和有没有待审批交给解码器收尾：
  // 服务被强杀的运行（interrupted、没有审批）这样才会停下，而不是一直转圈
  const finalStatus = !run || d.streaming ? undefined : run.status
  const final = useMemo<RunFinal | undefined>(
    () => (finalStatus ? { status: finalStatus, pending: pendingFlag } : undefined),
    [finalStatus, pendingFlag],
  )
  const phase = useMemo(() => decodePhase(d.events, final), [d.events, final])
  // 分支、循环那一行写运行时快照里出口的说明，不写 case 的 key
  const exitLabelOf = useMemo(() => (d.graph
    ? exitLabels(d.graph.nodes.map((n) => ({ id: n.id, type: n.type, config: n.data?.config })), sourceHandles)
    : undefined), [d.graph])
  const steps = useMemo(() => decodeRun(d.events, final, { exitLabelOf }), [d.events, final, exitLabelOf])
  const code = run ? displayCode(run, phase, pendingFlag) : 'idle'

  // ---- 视图、航迹游标 ----
  const view = asView(params.get('view'))
  const setView = useCallback((v: DetailView) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev)
      if (v === 'stream') next.delete('view')
      else next.set('view', v)
      return next
    }, { replace: true })
  }, [setParams])
  const [replayAt, setReplayAt] = useState<number | null>(null)
  // 航迹上选中的节点；undefined 是还没人点过（点过「收起」就是 null）
  const [traceNode, setTraceNode] = useState<string | null | undefined>(undefined)
  // 地址里的时刻只在进来时读一次，读完摘掉：拖动游标不回写地址（一帧一次会让
  // 整页跟着重渲染），留着就和游标对不上了。等事件到了、知道从哪一刻开始才读
  const atParam = params.get('at')
  const t0 = d.trace.timed ? d.trace.startedAt : undefined
  useEffect(() => {
    if (atParam == null || d.state !== 'ok') return
    const ms = Number(atParam)
    if (t0 != null && Number.isFinite(ms) && ms >= 0) setReplayAt(t0 + ms)
    setParams((prev) => {
      const next = new URLSearchParams(prev)
      next.delete('at')
      return next
    }, { replace: true })
  }, [atParam, d.state, t0, setParams])
  // 失败的、等审批的，航迹上默认就看那个节点。和数据同一次渲染就定下来，不等一个
  // effect：航迹坞按读数区第一次画出来的高度分地方，晚一拍才出现的节点卡会被压住
  const focusDefault = d.trace.failedNodeId ?? d.trace.waitingNodeId ?? null
  const traceSelected = traceNode === undefined ? focusDefault : traceNode
  const pickTraceNode = useCallback((id: string | null) => setTraceNode(id), [])

  // 从航迹、工件、原始事件切回时间线之后要做的定位（去审批卡、描出某个节点那几步）。
  // 视图是经地址切的，提交要晚好几帧：固定等两帧的话，焦点会落进还 inert 着的
  // 时间线，什么也不发生，焦点留在横幅的按钮上。所以等切回来的那次提交再做
  const reveal = useRef<(() => void) | null>(null)
  useEffect(() => {
    const fn = reveal.current
    if (!fn || view !== 'stream' || raw) return
    reveal.current = null
    // inert 已经随这次提交摘掉；再等一帧，让刚换回来的可读视图把审批卡画出来
    requestAnimationFrame(fn)
  }, [view, raw])

  // 工件：页签上要写件数，一打开详情就取；又有节点跑完、运行收尾时再取
  const artifactKey = useMemo(() => {
    let n = 0
    for (const e of d.events) if (e.type === 'node.finished' || e.type === 'tool.end' || e.type === 'tool.error') n += 1
    return `${d.run?.status ?? ''}|${n}`
  }, [d.events, d.run?.status])
  const artifacts = useRunArtifacts(runId, artifactKey)

  // 接流期间状态变了（跑完、停到审批上）：左边那一行跟着变，不用等整表刷新
  const lastPhase = useRef<RunPhase | null>(null)
  useEffect(() => {
    if (!run || !d.streaming || lastPhase.current === phase) return
    lastPhase.current = phase
    const status = runStatusOf(phase)
    if (status && status !== run.status) onChange({ ...run, status })
  }, [phase, d.streaming, run, onChange])

  const labelOf = useCallback((id?: string | null) => {
    if (!id) return undefined
    const n = d.graph?.nodes.find((x) => x.id === id)
    return n?.data?.label || id
  }, [d.graph])

  if (!run) {
    if (d.state === 'error') {
      return <ErrorState error={d.error} onRetry={() => void d.reload()} className="flex-1" />
    }
    return (
      <div className="flex flex-1 flex-col gap-4 p-4" aria-busy="true">
        <Skeleton rows={2} height={14} />
        <Skeleton rows={1} cols={6} height={40} />
        <Skeleton rows={6} height={22} />
      </div>
    )
  }

  const live = d.streaming || isLiveRun(run.status)
  const failedEvent = lastOf(d.events, 'run.failed')
  const failedNode = run.error_node_id ?? d.trace.failedNodeId ?? (failedEvent?.data?.node_id as string | undefined) ?? null
  const failedDetail = (failedEvent?.data?.detail as string | undefined)
    ?? (lastOf(d.events, 'node.failed')?.data?.detail as string | undefined)
  const explain = code === 'failed'
    ? explainRunError(run.error ?? (failedEvent?.data?.error as string | undefined), failedDetail)
    : null
  const workflowGone = !!run.workflow_id && catalogLoaded && !workflows.some((w) => w.id === run.workflow_id)
  const waitingNode = d.trace.waitingNodeId ?? d.pending?.[0]?.node_id
  const focus = code === 'failed' ? failedNode : code === 'waiting' ? waitingNode : null
  // 画布上回放是把这次的事件套在工作流「现在」的图上。之后改过结构的，对不上的
  // 节点不会亮——先说清，并指到航迹：那里用的是运行时的快照
  const current = run.workflow_id ? workflows.find((w) => w.id === run.workflow_id)?.graph : undefined
  const drift = !!d.graph && !!current && graphShape(d.graph) !== graphShape(current)
  // 这个节点在现在的图里已经没有了：画布上对准它，什么也对不到。工作流还没取回来时
  // 不知道，照常给
  const goneFromCanvas = (node?: string | null) => !!node && !!current && !current.nodes.some((n) => n.id === node)
  const canvasBase = run.workflow_id && !workflowGone ? `/studio/${run.workflow_id}?run=${run.id}` : null
  // 对准某个节点的入口（失败横幅的定位、航迹节点卡）：节点不在了就不给，免得放一个
  // 点了落空的按钮
  const hrefFor = (node?: string | null) => (canvasBase && !goneFromCanvas(node)
    ? `${canvasBase}${node ? `&focus=${encodeURIComponent(node)}` : ''}`
    : null)
  // 详情头是回放整次运行，一直给；对准的节点不在了就只打开，不带 focus
  const aimed = focus && !goneFromCanvas(focus) ? focus : null
  const canvasHref = hrefFor(aimed)
  // 还在往前走的叫「打开」（接着看实时的、去处理），停下的叫「回放」
  const replayable = code === 'succeeded' || code === 'failed' || code === 'cancelled'
  const dup = !!run.workflow_id && duplicateNames(workflows.map((w) => ({ id: w.id, name: w.name }))).has(run.workflow_name)

  // ---- 动作 ----

  const stop = async () => {
    setBusy('stop')
    try {
      await api.runs.cancel(run.id)
      toast.info('已发送停止请求，正在收尾…')
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  // 等审批、挂起的运行不再往下跑：收成已取消，待审批一并关闭（后端记下署名）
  const abandon = async () => {
    const n = d.pending?.length ?? 0
    const actor = localActor()
    const ok = await confirmDialog({
      title: '放弃这次运行？',
      body: code === 'waiting'
        ? `「${runName(run)}」正在等待审批。放弃后运行将终止，且无法恢复。`
        : `「${runName(run)}」已在断点处挂起。放弃后将无法继续运行。`,
      consequences: [
        ...(n ? [`${n} 条待审批一并关闭，${actor ? `留痕署名「${actor}」` : '留痕记为未署名（可在「设置 → 偏好设置」中填写署名）'}`] : []),
        '运行记为已取消；已完成的节点、产出和事件仍会保留',
      ],
      danger: true,
      confirmLabel: '放弃这次运行',
    })
    if (!ok) return
    setBusy('abandon')
    try {
      await api.runs.cancel(run.id)
      toast.ok('已放弃这次运行')
      await d.reload()
      void d.refreshPending()
      void refreshApprovals()
    } catch (e) {
      toast.error(e)
      void d.reload()
    } finally {
      setBusy(null)
    }
  }

  const carryOn = async () => {
    setBusy('continue')
    const resume = code !== 'failed'
    try {
      // 失败的走 continue（从失败节点接着跑）；挂起的走 resume(null)（从断点接回）
      const next = resume ? await api.runs.resume(run.id, null) : await api.runs.continue(run.id)
      setFeedback({ kind: 'continued', resume, from: resume ? undefined : labelOf(failedNode), at: Date.now() })
      d.follow(next)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  // 缺了必填输入：接着跑还是同一份输入。补上那一项，用这次运行时的同一张图
  // （快照）重新发起——未保存的图也能重跑。正式运行只能跑「当前」发布的版本：
  // 之后又发布过新版的话，带上原来的版本号会被后端拒掉，所以不带，并说清跑哪一版
  const rerun = async (field: string) => {
    const formal = run.run_class === 'formal' && !!run.workflow_id && !workflowGone
    const wf = formal ? workflows.find((w) => w.id === run.workflow_id) : undefined
    if (wf && !wf.published_version) {
      toast.error(`「${wf.name}」尚无发布版本，无法发起正式运行，请先在画布中发布`, {
        action: canvasHref ? { label: '去画布', onClick: () => navigate(canvasHref) } : undefined,
      })
      return
    }
    const published = wf?.published_version
    const value = await promptDialog({
      title: `补上「${field}」，重新运行`,
      body: formal
        ? `正式运行只使用当前发布的版本${published ? `：本次使用 v${published}${run.version && run.version !== published ? `，而非原运行的 v${run.version}` : ''}` : ''}。其余输入不变，将发起一次新的运行；这条失败记录会保留。`
        : run.run_class === 'formal'
          ? '工作流已删除，无法发起正式运行。本次将以探索运行方式，使用原运行时的工作流快照，其余输入不变；这条失败记录会保留。'
          : '使用原运行时的工作流快照，其余输入不变，发起一次新的运行；这条失败记录会保留。',
      label: field,
      placeholder: `填写 ${field}`,
      confirmLabel: '重新运行',
      validate: (v) => (v.trim() ? null : '此项为必填项'),
    })
    if (value == null) return
    setBusy('rerun')
    try {
      const input = { ...(run.input ?? {}), [field]: value }
      const scope = runScope(run)
      let next: Run
      if (formal) {
        next = await api.runs.start({ workflow_id: run.workflow_id!, run_class: 'formal', input, ...scope })
      } else {
        const graph = d.graph ?? (await api.runs.graph(run.id)).graph
        next = await api.runs.start({
          ...(run.workflow_id && !workflowGone ? { workflow_id: run.workflow_id } : {}),
          graph, input, ...scope,
        })
      }
      toast.ok(`已补上「${field}」并重新发起运行`)
      navigate({ pathname: `/runs/${next.id}`, search })
    } catch (e) {
      // 绑定的工具在本机不存在（422 run_tool_missing）：和画布上发起被拒（run/RunControl）同一套，
      // 报错里给直达入口——数据源工具去数据页，自定义 / MCP 工具去工具页
      const x = explainStartError(e)
      const to = x?.fixTo
      if (x && to) {
        toast.error(`${x.title}：${x.reason ?? ''}${x.action ?? ''}`, {
          detail: x.raw, key: 'runs:run-tool-missing',
          action: { label: x.fixLabel ?? '去接入', onClick: () => navigate(to) },
        })
      } else {
        toast.error(e)
      }
    } finally {
      setBusy(null)
    }
  }

  const onResolved = async (a: Approval) => {
    try {
      const [next, all] = await Promise.all([
        api.runs.get(run.id),
        api.approvals.list({ run_id: run.id, status: 'all' }).catch(() => [] as Approval[]),
      ])
      const done = all.find((x) => x.id === a.id)
      const approved = typeof done?.response?.approved === 'boolean' ? done.response.approved : null
      setFeedback({
        kind: 'decided', approved, node: a.node_label ?? labelOf(a.node_id) ?? a.node_id,
        by: done?.resolved_by, at: done?.resolved_at ?? new Date().toISOString(),
        terminates: approved === false && a.mode !== 'approve',
      })
      // 批完要能看着它往下跑：接上实时流，时间线继续长
      d.follow(next)
    } catch {
      d.follow(null)
    }
    void d.refreshPending()
  }

  // 审批卡、某个节点的几步都在时间线里：别的视图上点过来，先切回时间线，等它
  // 回来了再定位（见上面的 reveal）
  const inStream = (fn: () => void) => {
    if (view === 'stream' && !raw) {
      fn()
      return
    }
    reveal.current = fn
    if (raw) setRaw(false)
    if (view !== 'stream') setView('stream')
  }
  const reduceMotion = () => !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

  const jumpToApproval = (id: string) => inStream(() => {
    const el = streamBox.current?.querySelector<HTMLElement>(`[data-approval-card="${id}"]`)
    if (!el) return
    el.scrollIntoView({ behavior: reduceMotion() ? 'auto' : 'smooth', block: 'center' })
    el.classList.remove('runs-focus-ring')
    void el.offsetWidth
    el.classList.add('runs-focus-ring')
    setTimeout(() => el.classList.remove('runs-focus-ring'), 1500)
    el.querySelector<HTMLElement>('textarea, input, button')?.focus({ preventScroll: true })
  })

  // 航迹上选中的节点，回到时间线里看它那几步：定位到第一步，用强调色描一下
  // （data-flash 是时间线自己的描边样式，和点名定位同一种）
  const revealStep = (nodeId: string) => inStream(() => {
    const rows = [...(streamBox.current?.querySelectorAll<HTMLElement>(`[data-node-id="${CSS.escape(nodeId)}"]`) ?? [])]
    if (!rows.length) {
      toast.info(`时间线中没有「${labelOf(nodeId) ?? nodeId}」的步骤：该节点未执行`)
      return
    }
    rows[0].scrollIntoView({ behavior: reduceMotion() ? 'auto' : 'smooth', block: 'center' })
    for (const el of rows) el.dataset.flash = 'focus'
    setTimeout(() => { for (const el of rows) if (el.dataset.flash === 'focus') delete el.dataset.flash }, 1800)
  })

  // 工件产出的那一刻：去航迹，游标落在那里，选中产出它的节点
  const showMoment = (at: number, nodeId: string | null) => {
    setReplayAt(at)
    if (nodeId) pickTraceNode(nodeId)
    setView('trace')
  }
  // 画布上已经没有的节点：去航迹看它停下时的样子
  const showInTrace = (nodeId: string) => {
    setReplayAt(null)
    pickTraceNode(nodeId)
    setView('trace')
  }

  const extract = async () => {
    // 画布上还有没保存的改动：先问要不要放弃，再建。建完才问的话，人说「取消」，库里就多出
    // 一张没人要的草稿
    if (!(await canLeave('/studio/'))) return
    setBusy('extract')
    try {
      const res = await api.copilot.fromRun(run.id)
      void refreshCatalog()
      toast.ok(
        `已提取为草稿「${res.name}」：${res.nodes} 个节点${res.dropped_nodes ? `，已移除 ${res.dropped_nodes} 个未执行的节点` : ''}`,
      )
      // 提取出来的草稿接下来一定要去画布审改，直接带过去。上面已经问过，这一跳不再问
      navigate(`/studio/${res.workflow_id}`, leavePass())
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  const remove = async () => {
    const sealed = !!run.manifest_hash
    const ok = await confirmDialog({
      title: '删除这条运行记录？',
      body: `「${runName(run)}」${shortId(run.id)}，${formatDateTime(run.created_at ?? null)} 发起。`,
      consequences: [
        '事件流、产出工件和审批留痕一起删除，不可恢复',
        ...(sealed ? ['封存清单再也无法核对，这条运行不能再作为追溯凭证'] : []),
        ...(code === 'waiting' ? ['正在等待审批的步骤也将随之作废'] : []),
      ],
      danger: true,
      confirmLabel: '删除记录',
    })
    if (!ok) return
    setBusy('delete')
    try {
      try {
        await api.runs.remove(run.id)
      } catch (e) {
        // 封存过的正式运行：后端要 force，并在 detail 里写明后果。照它说的再确认一次，
        // 而且要照抄 id 前几位——这是出具结果的追溯凭证，不该一路回车删掉
        if (e instanceof ApiError && e.status === 409 && run.run_class === 'formal' && sealed) {
          const forced = await confirmDialog({
            title: '强制删除已封存的正式运行？',
            body: e.message,
            consequences: ['删除后将无法再核对本次出具的来源'],
            danger: true,
            requireText: run.id.slice(0, 6),
            confirmLabel: '强制删除',
          })
          if (!forced) return
          await api.runs.remove(run.id, { force: true })
        } else {
          throw e
        }
      }
      toast.ok('已删除这条运行记录')
      onDeleted(run.id)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(null)
    }
  }

  const menu: MenuItem[] = [
    { key: 'copy-id', label: '复制运行 ID', icon: <Copy size={12} />, onSelect: () => void copyText(run.id, '运行 ID') },
    {
      key: 'copy-link', label: '复制链接', icon: <Link2 size={12} />,
      onSelect: () => void copyText(`${location.origin}/runs/${run.id}`, '链接'),
    },
    {
      key: 'delete', label: '删除记录…', icon: <Trash2 size={12} />, danger: true, onSelect: () => void remove(),
      disabled: live || busy === 'delete',
      hint: live ? '运行中无法删除，请先停止运行' : undefined,
    },
  ]

  // ---- 时间线 ----

  const finished = lastOf(d.events, 'run.finished')
  const clocks = runClocks(run, d.trace, phase, d.streaming, Date.now())
  const paused = d.trace.waits.length > 0 || d.trace.drives.length > 1
  // statusCode：轮次头的徽标按它画。已取消、已挂起在 StreamTurn 的四种 phase 里
  // 只能算 done，不给它的话轮次头会给一条取消的运行画上「完成」的勾
  const turn: StreamTurn = {
    id: run.id,
    phase: phase === 'running' || phase === 'queued' ? 'running'
      : phase === 'waiting' ? 'waiting'
      : phase === 'failed' ? 'error' : 'done',
    status: statusLabel(code),
    steps,
    // 事件里的成果会被截断（output_truncated），截断了就取 run 上那份完整的
    output: (finished && !finished.data?.output_truncated ? finished.data?.output : null)
      ?? (run.output && Object.keys(run.output).length ? run.output : null),
    // 拆好的交过去（TurnFailure）：只交标题的话，流会拿它再跑一遍 explainRunError，原因和
    // 怎么办就丢了；fix 为 settings / tools 时报错里还有直达入口。标题不再拼节点名：报错块
    // 紧接着一行「出错的节点：「X」」，上面的横幅也写着「失败于「X」」。
    // 归不了类的失败标题就是原话，原文和标题相同时不再收进技术细节里重复一遍
    error: explain ? {
      title: explain.title, reason: explain.reason, hint: explain.action,
      detail: explain.raw && explain.raw !== explain.title ? explain.raw : undefined,
      fix: explain.fix, fixTo: explain.fixTo, fixFirst: explain.fixFirst,
    } satisfies TurnFailure : undefined,
    runClass: run.run_class,
    // 成果里的报告点开片段时要按运行号取证据链；不给 runId（详情头上已经有运行号）
    outputRun: run.id,
    statusCode: code,
    // 轮次头的计时和详情头同一个口径：跑完写墙钟，跑着从航迹的起点实时算。
    // 停下来过的（等过审批、失败后接着跑）例外，只数执行的部分：从第一次开始算，
    // 秒表里会是好几天的等待（「213:46:45.6」）。墙钟和等人在上面的读数里分开写着
    // 等审批的还没结束，不给时长——给了就是一个一直在涨、却被当成"花了多久"的数
    ...(phase === 'running' || phase === 'queued'
      ? (paused && clocks.active != null
        ? { startedAt: Date.now() - (d.trace.skewMs ?? 0) - clocks.active }
        : d.trace.startedAt != null ? { startedAt: d.trace.startedAt } : {})
      : phase !== 'waiting' && clocks.wall != null ? { elapsedMs: paused ? clocks.active ?? clocks.wall : clocks.wall } : {}),
  }

  const cards = d.pending?.length ? (
    <div className="mt-3 space-y-2">
      {d.pending.map((a) => (
        <div key={a.id} data-approval-card={a.id} className="relative overflow-hidden rounded-lg border"
             style={{ borderColor: 'color-mix(in srgb, var(--st-waiting) 45%, var(--border))' }}>
          {/* 详情头已经写着是哪个工作流，卡头不再重复 */}
          <ApprovalCard approval={a} onResolved={() => onResolved(a)} showWorkflow={false} />
        </div>
      ))}
    </div>
  ) : null

  // 发布之后数据目录有变化：正式运行开始时记下的提醒，横幅一直挂着（不拦运行，但看结果的人得知道）。
  // 上面有提前返回，这里不能再用 hook；只解一条事件，直接算
  const catalogEvent = lastOf(d.events, 'catalog.drift')
  const catalogChange = catalogEvent ? catalogDrift(catalogEvent.data ?? {}) : null
  const unsaved = isUnsaved(run)
  const artifactCount = artifacts.items?.length
  const viewTabs: TabItem<DetailView>[] = [
    { key: 'stream', label: '时间线', title: '逐步查看每个节点的执行过程和输出' },
    { key: 'trace', label: '航迹', title: '按时间展开各节点，查看耗时、阻塞和并行情况；可拖动到任意时刻回放' },
    { key: 'evidence', label: EVIDENCE_AUDIT_TEXT.tab, title: EVIDENCE_AUDIT_TEXT.tabTitle },
    {
      key: 'artifacts', label: '工件', count: artifactCount, unit: '件', tone: 'quiet',
      title: artifactCount == null ? '本次运行保存的证据和产出' : `${artifactCount} 件：查询和工具调用的原始结果、每个节点每一次的产出`,
    },
  ]

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col" data-run-detail={run.id} data-run-code={code}>
      <header className="runs-head shrink-0 border-b">
        <div className="flex items-start gap-3 px-4 pb-2 pt-2.5">
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 items-center gap-2">
              <h2 className={clsx('min-w-0 truncate text-sm font-semibold', unsaved && 'text-dim')}
                  title={unsaved ? UNSAVED_HINT : run.workflow_name}>
                {runName(run)}
              </h2>
              {dup && <span className="mono shrink-0 text-2xs text-faint" title={`工作流 id：${run.workflow_id}`}>{idTail(run.workflow_id)}</span>}
              <ClassChip runClass={run.run_class} version={run.version} always />
              <TierChip tier={(run.output as any)?._issuance?.tier} />
            </div>
            <div className="mt-0.5 flex min-w-0 items-center gap-1 text-2xs text-faint">
              <span className="mono">{shortId(run.id)}</span>
              <span>·</span>
              <time className="tnum" title={`发起于 ${formatDateTime(run.created_at ?? null)}`}>
                {formatTime(run.created_at ?? null, new Date(now))}
              </time>
              <span>·</span>
              <span title={run.started_by ? undefined : '发起时未设置署名'}>
                {run.started_by || '未署名'} 发起
              </span>
              <span>·</span>
              <span className="tnum">{formatNumber(d.events.length)} 条事件</span>
              {workflowGone && <span>· 工作流已删除</span>}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {(code === 'running' || code === 'queued') && (
              <button type="button" className="btn btn-sm btn-danger" disabled={busy === 'stop'} onClick={() => void stop()}
                      data-action="stop" title="停止本次运行：已完成的节点保留，执行中的节点记为已取消">
                {busy === 'stop' ? <Spinner size={11} /> : <Square size={10} aria-hidden fill="currentColor" />} 停止
              </button>
            )}
            {canvasHref && (
              <Link className="btn btn-sm relative" to={canvasHref} data-action="open-canvas"
                    data-canvas-mode={replayable ? 'replay' : 'live'} data-graph-drift={drift ? '1' : undefined}
                    title={[
                      replayable
                        ? `在画布上回放本次运行：拖动底部的航迹，节点卡片会回到对应时刻${aimed ? '；并定位到失败的节点' : ''}`
                        : code === 'waiting' && aimed ? '打开该工作流，并定位到等待审批的节点' : '在画布中打开该工作流，继续查看本次运行',
                      drift ? '注意：工作流在本次运行后修改过结构，画布显示的是当前工作流，已变更的节点不会高亮；运行当时的全貌请查看「航迹」' : '',
                    ].filter(Boolean).join('\n')}>
                {replayable ? <Rewind size={11} aria-hidden /> : <SquareArrowOutUpRight size={11} aria-hidden />}
                {replayable ? '在画布中回放' : '在画布中打开'}
                {drift && (
                  <span aria-hidden className="absolute -right-1 -top-1 h-2 w-2 rounded-full border-2"
                        style={{ background: 'var(--st-waiting)', borderColor: 'var(--bg)' }} />
                )}
              </Link>
            )}
            {code === 'succeeded' && run.run_class !== 'formal' && (
              <button type="button" className="btn btn-sm" disabled={busy === 'extract'} onClick={() => void extract()}
                      data-action="extract" title="将本次实际执行的路径提取为草稿工作流（移除未执行的节点），提取后可在画布中审阅修改">
                {busy === 'extract' ? <Spinner size={11} /> : <GitFork size={11} aria-hidden />} 提取为草稿
              </button>
            )}
            <MoreMenu items={menu} />
          </div>
        </div>
        <Telemetry run={run} trace={d.trace} phase={phase} code={code} streaming={d.streaming}
                   pending={d.pending} labelOf={labelOf} cancelled={lastOf(d.events, 'run.cancelled')?.data} />
        <ProvenanceBar run={run} eventCount={d.events.length} phase={phase} />
        {explain && (
          <FailedBanner explain={explain} nodeId={failedNode} nodeLabel={labelOf(failedNode)}
                        canvasHref={hrefFor(failedNode)}
                        onShowInTrace={failedNode && goneFromCanvas(failedNode) ? () => showInTrace(failedNode) : undefined}
                        onContinue={() => void carryOn()}
                        onRerun={(field) => void rerun(field)} busy={busy === 'continue' || busy === 'rerun'} />
        )}
        {(code === 'held' || code === 'suspended') && (
          <HeldBanner reason={run.error} onContinue={() => void carryOn()} onAbandon={() => void abandon()}
                      busy={busy === 'continue' || busy === 'abandon'} />
        )}
        {code === 'waiting' && !!d.pending?.length && (
          <WaitingBanner approvals={d.pending} labelOf={labelOf} onJump={jumpToApproval}
                         onAbandon={() => void abandon()} busy={busy === 'abandon'} now={now} />
        )}
        {catalogChange && <CatalogDriftBanner drift={catalogChange} />}
        {feedback && <FeedbackStrip feedback={feedback} onDismiss={() => setFeedback(null)} />}
        <div className="flex items-center border-t pr-2">
          <RunTabs tabs={viewTabs} active={view} onChange={setView} label="运行详情视图" idPrefix="run-view"
                   className="min-w-0 flex-1" />
          {view === 'stream' && (
            <IconButton
              label={raw ? '回到可读视图' : `查看原始事件（${d.events.length} 条）`}
              aria-pressed={raw}
              className={clsx(raw && 'text-[var(--accent)]')}
              onClick={() => setRaw((v) => !v)}
              icon={<Code2 size={13} aria-hidden />}
              data-action="raw"
            />
          )}
        </div>
      </header>

      {/* 时间线一直挂着（换视图回来还停在原来的位置，也不重新解码），不在看时只是
          不可见、不可聚焦；航迹和工件用到才挂 */}
      <div className="relative min-h-0 flex-1" role="tabpanel" id="run-view-panel" aria-labelledby={`run-view-tab-${view}`}>
        <div ref={streamBox} className={clsx('absolute inset-0', view !== 'stream' && 'invisible')}
             inert={view !== 'stream'} data-view-pane="stream">
          {raw
            ? <RawEvents events={d.events} />
            : (
              // 回看历史停在摘要（第一处失败、审批卡或顶部），在跑的才贴着最新处
              <AssistantStream
                key={run.id}
                turns={[turn]}
                approvalsFor={() => cards}
                landing={live ? 'end' : 'summary'}
                clockSkewMs={d.trace.skewMs ?? 0}
              />
            )}
        </div>
        {view === 'trace' && (
          <div className="absolute inset-0" data-view-pane="trace">
            <TracePane run={run} events={d.events} trace={d.trace} graph={d.graph} code={code}
                       replayAt={replayAt} onReplayAt={setReplayAt} selected={traceSelected} onSelect={pickTraceNode}
                       labelOf={labelOf} nodeHref={hrefFor} goneFromCanvas={goneFromCanvas} onRevealStep={revealStep} />
          </div>
        )}
        {view === 'evidence' && (
          <div className="absolute inset-0" data-view-pane="evidence">
            <EvidencePane run={run} output={turn.output ?? null} labelOf={labelOf} refreshKey={artifactKey} />
          </div>
        )}
        {view === 'artifacts' && (
          <div className="absolute inset-0" data-view-pane="artifacts">
            <ArtifactsPane list={artifacts} graph={d.graph} trace={d.trace} labelOf={labelOf}
                           onMoment={t0 != null ? showMoment : undefined} />
          </div>
        )}
      </div>

      <footer className="flex shrink-0 items-center gap-3 border-t px-4 py-1.5 text-2xs text-faint">
        <span className="tnum">{formatNumber(d.events.length)} 条事件</span>
        {d.streaming && <span style={{ color: 'var(--st-running)' }}>· 实时接收中</span>}
        <span className="flex-1" />
        {run.input && !!Object.keys(run.input).length && (
          <span className="min-w-0 truncate" title={JSON.stringify(run.input, null, 2)}>
            输入：{summarizeRun(run.input)}
          </span>
        )}
      </footer>
    </div>
  )
}

/** 事件之外的事实（查到的状态、有没有审批）和事件推出的相位合成一个显示码 */
function displayCode(run: Run, phase: RunPhase, pending: boolean | undefined): StatusCode {
  switch (phase) {
    case 'running': case 'queued': case 'waiting': case 'succeeded': case 'failed': case 'cancelled':
      return phase
    case 'suspended':
      // 后端写的是 interrupted：服务重启打断、没有审批，叫「已挂起 · 可续跑」
      return run.status === 'suspended' ? 'suspended' : 'held'
    default:
      return resolveStatus(run.status, { pendingApproval: run.status === 'interrupted' ? pending : undefined })
  }
}

function lastOf(events: RunEvent[], type: string): RunEvent | undefined {
  for (let i = events.length - 1; i >= 0; i--) if (events[i].type === type) return events[i]
  return undefined
}

/** 原始事件。翻译层出问题时用来对照；时间写成相对第一条事件的偏移，快慢一眼可比 */
function RawEvents({ events }: { events: RunEvent[] }) {
  if (!events.length) {
    return <div className="p-3 text-center text-2xs text-faint">没有事件记录</div>
  }
  const t0 = events.find((e) => typeof e.ts === 'number' && e.ts > 0)?.ts
  return (
    <div className="h-full overflow-y-auto" data-raw-events="">
      {events.map((e) => (
        <div key={e.seq} className="flex gap-2 border-b px-3 py-1 text-2xs last:border-0">
          <span className="tnum w-10 shrink-0 text-right text-faint">#{e.seq}</span>
          <span className="tnum mono w-16 shrink-0 text-right text-faint"
                title={formatDateTime(e.ts)}>
            {t0 != null && typeof e.ts === 'number' ? `+${((e.ts - t0)).toFixed(2)}s` : '—'}
          </span>
          <span className="mono w-36 shrink-0 truncate text-[var(--accent)]">{e.type}</span>
          <span className="mono w-28 shrink-0 truncate text-dim">{e.node_id ?? ''}</span>
          <span className="mono min-w-0 flex-1 break-all text-faint">
            {JSON.stringify(e.data).slice(0, 240)}
          </span>
        </div>
      ))}
    </div>
  )
}
