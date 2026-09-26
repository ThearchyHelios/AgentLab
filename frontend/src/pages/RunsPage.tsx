import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { History, Inbox, RefreshCw } from 'lucide-react'
import { EmptyState, IconButton, StatusBadge } from '../components/ui'
import { STATUS, type StatusCode } from '../lib/status'
import { useCatalog, useOnReconnect } from '../store/catalog'
import type { Approval, Run } from '../types'
import {
  ApprovalRows, FilterBar, RunRows, StatusSegments,
} from './runs/RunList'
import { RunDetailView } from './runs/RunDetailView'
import {
  RUNS_TABS, TAB_CODES, TAB_LABEL, ageMs, asTab, duplicateNames, formatSpan, isLiveRun, matchesCodes,
  parseQuery, runCode, stripStatusWords, type RunsTab,
} from './runs/model'
import { RunTabs, useNow, type TabItem } from './runs/parts'
import { useApprovalQueue, useRunList, useStatusCounts } from './runs/useRunsData'
import './runs/runs.css'

/**
 * 记录：每次运行的完整事件流都落了库，所以这里能完整回放、排错、处理待办。
 *
 * 地址就是状态：/runs?tab=approvals|running|failed|all，外加 status / class / wf / q
 * 几个筛选；/runs/:runId 打开某一条，筛选原样保留在查询串里。导航上的待审批
 * 徽标指向 ?tab=approvals——这页必须能处理待办，否则刷新后卡在审批上的运行在
 * 整个界面里没有任何入口可以推进。
 */
export function RunsPage() {
  const { runId } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  const [params, setParams] = useSearchParams()

  const tab = asTab(params.get('tab'))
  const text = params.get('q') ?? ''
  const runClass = (['formal', 'exploratory'].includes(params.get('class') ?? '') ? params.get('class') : '') as
    '' | 'formal' | 'exploratory'
  const workflowId = params.get('wf') ?? ''
  const segment = (params.get('status') ?? '').split(',').filter((c): c is StatusCode => c in STATUS)

  const patchParams = useCallback((patch: Record<string, string | null>) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev)
      for (const [k, v] of Object.entries(patch)) {
        if (v == null || v === '') next.delete(k)
        else next.set(k, v)
      }
      return next
    }, { replace: true })
  }, [setParams])

  // 认不出的页签（链接过期、手输错）落回「全部」，并把地址一起纠正
  const rawTab = params.get('tab')
  useEffect(() => {
    if (rawTab && rawTab !== tab) patchParams({ tab: tab === 'all' ? null : tab })
  }, [rawTab, tab, patchParams])

  // 搜索框：输入即时显示，停手 250ms 再写进地址、发请求
  const [draft, setDraft] = useState(text)
  const typing = useRef(false)
  useEffect(() => { if (!typing.current) setDraft(text) }, [text])
  useEffect(() => {
    if (draft === text) { typing.current = false; return }
    const t = setTimeout(() => { typing.current = false; patchParams({ q: draft || null }) }, 250)
    return () => clearTimeout(t)
  }, [draft, text, patchParams])

  const parsed = useMemo(() => parseQuery(text), [text])
  const approvals = useCatalog((s) => s.approvals)
  const catalogLoaded = useCatalog((s) => s.loaded)
  const workflows = useCatalog((s) => s.workflows)

  // epoch：手动刷新、断网恢复、删除之后，列表以外的几份（计数、待审批）一起重拉；
  // tick：轮询时只刷计数，待审批跟着 catalog 的轮询走，不另敲一遍
  const [epoch, setEpoch] = useState(0)
  const [tick, setTick] = useState(0)
  const queue = useApprovalQueue(epoch)
  // 判「等待审批 / 已挂起」用哪份待审批：页面自己那份上限高（500 条），没拿到
  // 之前用 catalog 的；两份都没有时是 null，按等待审批算，不闪成「已挂起」
  const known: Approval[] | null = queue.state === 'ok' ? queue.items : catalogLoaded ? approvals : null

  // 实际查询的状态：页签定死的范围和文字里的状态词取交集；「全部」里文字优先于分段
  const tabCodes = tab === 'running' || tab === 'failed' ? TAB_CODES[tab] : null
  const codes: StatusCode[] = tabCodes
    ? (parsed.codes.length ? tabCodes.filter((c) => parsed.codes.includes(c)) : tabCodes)
    : (parsed.codes.length ? parsed.codes : segment)
  const impossible = !!tabCodes && parsed.codes.length > 0 && codes.length === 0

  const filters = { codes, runClass, workflowId, q: parsed.q }
  const list = useRunList(filters, tab !== 'approvals' && !impossible)
  // 计数两份：页签和分段上的跟着当前的类别、工作流、名称条件走；右侧总览说的
  // 是全局——筛掉了不代表事情没了
  const narrowed = !!(runClass || workflowId || parsed.q)
  const countKey = epoch * 100_000 + tick
  const globalCounts = useStatusCounts({ runClass: '', workflowId: '', q: '' }, known, countKey)
  const narrowCounts = useStatusCounts({ runClass, workflowId, q: parsed.q }, known, countKey, narrowed)
  const counts = narrowed ? narrowCounts : globalCounts

  const { reload: reloadList, patchRow, removeRow } = list
  const refreshAll = useCallback(() => {
    void reloadList()
    setEpoch((e) => e + 1)
  }, [reloadList])
  // 断网恢复：列表、计数、待审批一起重拉，不停在「还没有运行记录」的假空态上
  useOnReconnect(refreshAll)

  // 有在跑的：列表每 4 秒原地刷新（页面不可见时不刷）；「运行中」页签一直刷，
  // 新发起的运行会自己出现
  const hasLive = list.rows.some((r) => isLiveRun(r.status))
  useEffect(() => {
    if (tab === 'approvals' || (tab !== 'running' && !hasLive)) return
    const t = setInterval(() => {
      if (document.hidden) return
      void reloadList({ silent: true })
      setTick((n) => n + 1)
    }, 4000)
    return () => clearInterval(t)
  }, [tab, hasLive, reloadList])

  // waiting / held 在后端都是 interrupted：查回来之后按审批列表再分一次
  const codeSig = codes.join(',')
  const rows = useMemo(
    () => (impossible ? [] : list.rows.filter((r) => matchesCodes(r, codes, known))),
    [list.rows, codeSig, known, impossible], // codes 每次渲染都是新数组，按内容比
  )
  const shownList = impossible ? { ...list, rows: [], state: 'ok' as const, hasMore: false } : { ...list, rows }

  const dupNames = useMemo(() => duplicateNames([
    ...workflows.map((w) => ({ id: w.id, name: w.name })),
    ...list.rows.map((r) => ({ id: r.workflow_id, name: r.workflow_name })),
  ]), [workflows, list.rows])

  // 待审批页签也吃同样的类别、工作流、名称条件
  const approvalFilter = useCallback((a: Approval) => {
    if (runClass && a.run_class !== runClass) return false
    if (workflowId && a.workflow_id !== workflowId) return false
    if (parsed.q && !(a.workflow_name ?? '').toLowerCase().includes(parsed.q.toLowerCase())) return false
    return true
  }, [runClass, workflowId, parsed.q])

  const pendingOf = useCallback(
    (id: string) => known?.find((a) => a.run_id === id && a.status === 'pending'),
    [known],
  )
  const codeOf = useCallback((r: Run) => runCode(r, known), [known])

  const open = (id: string) => navigate({ pathname: `/runs/${id}`, search: location.search })

  // 详情里那一条变了：原地换掉列表里的那一行；状态变了顺带刷新计数
  const lastStatus = useRef(new Map<string, string>())
  const onRunChange = useCallback((run: Run) => {
    patchRow(run)
    const before = lastStatus.current.get(run.id)
    lastStatus.current.set(run.id, run.status)
    if (before && before !== run.status) setTick((n) => n + 1)
  }, [patchRow])

  const onDeleted = useCallback((id: string) => {
    removeRow(id)
    setEpoch((e) => e + 1)
    navigate({ pathname: '/runs', search: location.search }, { replace: true })
  }, [removeRow, navigate, location.search])

  const now = useNow(60_000)
  const queueItems = queue.items.filter(approvalFilter)
  const oldest = oldestAge(queueItems, now)
  const running = (counts.counts.running ?? 0) + (counts.counts.queued ?? 0)
  const failed = counts.counts.failed ?? 0
  const plus = counts.saturated ? '+' : ''
  const allRunning = (globalCounts.counts.running ?? 0) + (globalCounts.counts.queued ?? 0)
  const allFailed = globalCounts.counts.failed ?? 0
  const allPlus = globalCounts.saturated ? '+' : ''
  const allPending = queue.items.length
  const allOldest = oldestAge(queue.items, now)
  const tabs: TabItem<RunsTab>[] = RUNS_TABS.map((key) => {
    if (key === 'approvals') {
      return {
        key, label: TAB_LABEL[key], count: queueItems.length, tone: 'alert',
        title: queueItems.length
          ? `${queueItems.length} 条待审批${oldest != null ? `，最久已等 ${formatSpan(oldest, { coarse: true })}` : ''}`
          : '没有待审批',
      }
    }
    if (key === 'running') return { key, label: TAB_LABEL[key], count: running, countLabel: `${running}${plus}`, tone: 'live' }
    if (key === 'failed') return { key, label: TAB_LABEL[key], count: failed, countLabel: `${failed}${plus}`, tone: 'quiet' }
    return { key, label: TAB_LABEL[key] }
  })

  const setTab = (k: RunsTab) => patchParams({ tab: k === 'all' ? null : k })
  const setText = (v: string) => { typing.current = true; setDraft(v) }
  const pickSegment = (code: StatusCode | null) => {
    // 点了分段，文字里原来的状态词就不该再和它打架
    const rest = stripStatusWords(draft)
    typing.current = false
    setDraft(rest)
    patchParams({ status: code, q: rest || null })
  }
  const clearFilters = () => {
    typing.current = false
    setDraft('')
    patchParams({ q: null, status: null, class: null, wf: null })
  }

  const filterNote = describeFilters({ tab, parsed, segment, runClass, workflowId, workflows })

  return (
    <div className="flex h-full flex-col" data-runs-page="">
      {/* 页头和工具、知识、数据、设置几页同一个样子：48px 高、图标框、标题、一句
          说明。待审批的数不在这里重复——紧下面的页签上就有 */}
      <header className="flex h-12 shrink-0 items-center gap-2.5 border-b bg-panel px-4">
        <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md border bg-elev text-dim" aria-hidden>
          <History size={13} />
        </span>
        <h1 className="shrink-0 text-sm font-semibold">记录</h1>
        <p className="min-w-0 truncate text-xs text-faint">每次运行的完整轨迹、待处理的审批和封存凭证</p>
        <span className="flex-1" />
        <IconButton label="刷新" icon={<RefreshCw size={12} aria-hidden />} onClick={refreshAll} data-runs-refresh="" />
      </header>

      <div className="flex min-h-0 flex-1">
        <aside className="flex w-[360px] shrink-0 flex-col border-r bg-panel xl:w-[400px]" aria-label="运行列表">
          <RunTabs tabs={tabs} active={tab} onChange={setTab} label="记录分类" idPrefix="runs" />
          <FilterBar
            text={draft} onText={setText} parsed={parsed}
            runClass={runClass} onRunClass={(v) => patchParams({ class: v || null })}
            workflowId={workflowId} onWorkflow={(id) => patchParams({ wf: id || null })}
            workflows={workflows} dupNames={dupNames}
          />
          {tab === 'all'
            ? <StatusSegments active={codes} onPick={pickSegment} counts={counts} />
            : <div className="h-2 shrink-0" />}
          <div
            className="min-h-0 flex-1 overflow-y-auto border-t"
            role="tabpanel"
            id="runs-panel"
            aria-labelledby={`runs-tab-${tab}`}
            data-runs-panel={tab}
            onKeyDown={moveInList}
          >
            {tab === 'approvals' ? (
              <ApprovalRows queue={queue} filter={approvalFilter} onOpen={open} selectedId={runId} dupNames={dupNames} />
            ) : (
              <RunRows
                list={shownList}
                codeOf={codeOf}
                pendingOf={pendingOf}
                selectedId={runId}
                onOpen={open}
                dupNames={dupNames}
                emptyTitle={tab === 'running' ? '现在没有在跑的运行' : tab === 'failed' ? '没有失败的运行' : '还没有运行记录'}
                emptyBody={tab === 'all' ? '在问数据或画布上发起一次运行，完整轨迹会记在这里。' : undefined}
                filtered={filterNote}
                onClear={clearFilters}
              />
            )}
          </div>
        </aside>

        {/* 外壳已经有 <main>，这里再套一个就是两个主区域地标 */}
        <section className="flex min-h-0 min-w-0 flex-1 flex-col" aria-label="运行详情">
          {runId
            ? <RunDetailView key={runId} runId={runId} onChange={onRunChange} onDeleted={onDeleted} />
            : <Overview pending={allPending} oldest={allOldest} running={allRunning} failed={allFailed} plus={allPlus}
                        onTab={setTab} />}
        </section>
      </div>
    </div>
  )
}

/** 一批待审批里等得最久的那条等了多久；空的是 null */
function oldestAge(items: Approval[], now: number): number | null {
  return items.reduce<number | null>((m, a) => {
    const age = ageMs(a.created_at, now)
    return age != null && (m == null || age > m) ? age : m
  }, null)
}

/** 列表里 ↑↓ 在行之间移动焦点，回车打开（行本身是按钮）。不在移动时就打开：每按一下就拉一次详情太吵 */
function moveInList(e: KeyboardEvent<HTMLDivElement>) {
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
  const rows = [...e.currentTarget.querySelectorAll<HTMLElement>('[data-run-id][type=button], [data-approval-id]')]
  const i = rows.indexOf(document.activeElement as HTMLElement)
  if (i < 0) return
  const next = rows[e.key === 'ArrowDown' ? Math.min(rows.length - 1, i + 1) : Math.max(0, i - 1)]
  if (!next || next === rows[i]) return
  e.preventDefault()
  next.focus()
  next.scrollIntoView({ block: 'nearest' })
}

/** 空的「没有匹配」要说清是被哪些条件筛空的；没有任何条件时返回 null */
function describeFilters({ tab, parsed, segment, runClass, workflowId, workflows }: {
  tab: RunsTab; parsed: ReturnType<typeof parseQuery>; segment: StatusCode[]
  runClass: string; workflowId: string; workflows: { id: string; name: string }[]
}): string | null {
  const parts: string[] = []
  if (parsed.words.length) parts.push(`状态「${parsed.words.join('、')}」`)
  else if (tab === 'all' && segment.length) parts.push(`状态「${segment.map((c) => STATUS[c].short).join('、')}」`)
  if (parsed.q) parts.push(`名称含「${parsed.q}」`)
  if (runClass) parts.push(runClass === 'formal' ? '正式运行' : '探索运行')
  if (workflowId) parts.push(`工作流「${workflows.find((w) => w.id === workflowId)?.name ?? '已删除的工作流'}」`)
  if (!parts.length) return null
  return `筛选条件：${parts.join(' · ')}${tab !== 'all' ? `（在「${TAB_LABEL[tab]}」里）` : ''}`
}

/**
 * 没选中运行时的右侧：不是一句「选一条」，而是此刻要管的几件事——待审批、在跑的、
 * 失败的，点了就去对应的页签。
 */
function Overview({ pending, oldest, running, failed, plus, onTab }: {
  pending: number; oldest: number | null; running: number; failed: number; plus: string
  onTab: (t: RunsTab) => void
}) {
  const tiles: { key: RunsTab; code: StatusCode; label: string; value: string; sub: string; alert: boolean }[] = [
    {
      key: 'approvals', code: 'waiting', label: '待审批', value: String(pending),
      sub: pending ? `最久已等 ${formatSpan(oldest, { coarse: true })}` : '没有要处理的',
      alert: pending > 0,
    },
    { key: 'running', code: 'running', label: '运行中', value: `${running}${running ? plus : ''}`, sub: running ? '实时看、随时停' : '现在没有在跑的', alert: false },
    { key: 'failed', code: 'failed', label: '失败', value: `${failed}${failed ? plus : ''}`, sub: failed ? '看原因、从断点接着跑' : '没有失败的', alert: false },
  ]
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-6 px-8" data-runs-overview="">
      <div className="grid w-full max-w-xl grid-cols-3 overflow-hidden rounded-lg border bg-panel">
        {tiles.map((t, i) => (
          <button
            key={t.key}
            type="button"
            onClick={() => onTab(t.key)}
            className="flex flex-col gap-1 px-4 py-3 text-left transition-colors hover:bg-hover"
            style={i ? { borderLeft: '1px solid var(--border)' } : undefined}
            data-overview={t.key}
          >
            <span className="flex items-center gap-1.5 text-2xs text-faint">
              <StatusBadge status={t.code} size={11} decorative animate={false} /> {t.label}
            </span>
            <span className="mono tnum text-xl leading-7" style={{ color: t.alert ? STATUS[t.code].color : undefined }}>
              {t.value}
            </span>
            <span className="truncate text-2xs text-faint">{t.sub}</span>
          </button>
        ))}
      </div>
      <EmptyState
        icon={<Inbox size={22} />}
        title="选一条运行记录"
        body="左侧点开一条，可以看完整的执行轨迹、三种时长、封存凭证；失败的能从断点接着跑，等审批的就地处理。"
        className="py-0"
      />
    </div>
  )
}
