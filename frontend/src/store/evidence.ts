import { useEffect, useMemo } from 'react'
import { create } from 'zustand'
import { ApiError, api } from '../api/client'
import { graphVerdicts, judgedVerdicts } from '../lib/evidence'
import type {
  EvidenceDocData, EvidenceGraph, EvidenceJudgeResult, EvidenceSegmentDetail, EvidenceStats, EvidenceVerdict,
} from '../types'

/**
 * 可点击证据的缓存：报告文档、证据图、点开过的片段。
 *
 * - 文档按工件 id 缓存，永久：工件内容寻址，同一个 id 永远是同一份内容，取回时后端
 *   还复验了哈希。同一份报告在问数据页、画布右栏、记录页各出现一次也只取一次。
 * - 片段按 `runId:segId` 缓存：点开时才取，同一个片段来回点不重复请求。
 * - 证据图按运行缓存：面板里封存状态的兜底来源。
 *
 * 出错的不缓存成定论：下次点开再取一次（后端刚重启、接口还没部署到这个版本都会是
 * 一时的）。正在取的不重复发。
 *
 * 四期：封存之后按需追加的结论句判定按 `runId@报告` 记在 judged 里（按需裁判接口、片段接口叠上的
 * 判定都记进来），正文的句末徽标、横幅的结论句计数据此盖过文档里的「未裁判 · 按需」。封存的文档本身
 * 一个字不改。同一句正在判的不重复发；判过的（判定不是未裁判）不再给按钮，也就不会再发
 */

export interface Slot<T> {
  status: 'loading' | 'ok' | 'error'
  data?: T
  error?: unknown
}

/** 一张图最近一次运行里每个报告节点的核对统计（画布上报告节点卡的章用） */
export interface LatestReports {
  runId: string | null
  reports: Record<string, { stats?: EvidenceStats; claims?: string | null }>
}

interface EvidenceState {
  /**
   * 哪一份报告的面板开着。同一屏上可能有好几份报告（问数据页的几轮、改写前的原件），
   * 侧边面板只有一个位置：新打开的认领它，别的自己收起
   */
  owner: string | null
  claim: (owner: string) => void
  docs: Record<string, Slot<EvidenceDocData>>
  graphs: Record<string, Slot<EvidenceGraph>>
  segments: Record<string, Slot<EvidenceSegmentDetail>>
  /** 按工作流：最近一次运行的报告统计。画布没挂着运行时，报告节点卡的章从这里取 */
  latest: Record<string, Slot<LatestReports>>
  loadDoc: (artifact: string) => Promise<void>
  loadGraph: (runId: string) => Promise<void>
  /** 证据图重新取一次：运行还在往前走（记录页接着流）时，封存状态和报告都还会变 */
  reloadGraph: (runId: string) => Promise<void>
  loadSegment: (runId: string, segmentId: string, report?: string) => Promise<void>
  loadLatest: (workflowId: string) => Promise<void>
  /**
   * 这张图的「最近一次运行」作废：画布上挂着这张图的一次运行（刚跑的、回放的）时，之前取的那份
   * 不一定还是最近的。清掉以后，运行摘下来（「清除」、换一张图再回来）时 loadLatest 重新取
   */
  dropLatest: (workflowId: string) => void
  /** 封存之后追加的判定：`runId@报告` → {unit: 判定} */
  judged: Record<string, Record<string, EvidenceVerdict>>
  /** 按需裁判的请求：`runId@报告:unit` → 这一次的答复（触顶、失败说什么靠它） */
  asks: Record<string, Slot<EvidenceJudgeResult>>
  /** 运行类别（formal / exploratory）：没开裁判的文档靠它认探索运行，按需取一次 */
  runClasses: Record<string, Slot<string | null>>
  /** 记下几句封存后追加的判定（片段接口叠上的、证据图带来的） */
  noteVerdicts: (runId: string, report: string | undefined, verdicts: Record<string, EvidenceVerdict>) => void
  /** 请模型判断这几句（探索运行）。判定记进 judged；答复（含触顶、出错）记进 asks */
  judge: (runId: string, report: string | undefined, units: string[]) => Promise<void>
  loadRunClass: (runId: string) => Promise<void>
}

/** 封存后追加的判定按哪份报告记：一次运行里有好几份报告时 unit id 会撞（每份都从 u0 数起） */
export const docKey = (runId: string, report?: string) => `${runId}@${report ?? ''}`
export const askKey = (runId: string, report: string | undefined, unit: string) => `${docKey(runId, report)}:${unit}`

/**
 * 片段的缓存键：`runId:segId`。一次运行里有好几份报告时片段 id 会撞（每份都从 s0 数起），
 * 带上报告节点：`runId:segId@report`
 */
export const segmentKey = (runId: string, segmentId: string, report?: string) =>
  report ? `${runId}:${segmentId}@${report}` : `${runId}:${segmentId}`

/** 工件内容是不是一份报告文档：至少得有块。别的工件（指标集、查询快照）点进来不能当文档画 */
export function asDoc(content: unknown): EvidenceDocData | null {
  if (!content || typeof content !== 'object') return null
  const doc = content as EvidenceDocData
  return Array.isArray(doc.blocks) ? doc : null
}

export class NotADocError extends Error {
  constructor() {
    super('这件工件不是报告文档')
    this.name = 'NotADocError'
  }
}

export const useEvidence = create<EvidenceState>((set, get) => {
  /**
   * 取一次、记进 table[key]；正在取或已经取到的不再发。取的途中这一格被清掉或换了（dropLatest 之后
   * 又起了一次），回来的结果作废：不能让早先发出的请求把后来那份盖掉
   */
  async function fill<K extends 'docs' | 'graphs' | 'segments' | 'latest'>(
    table: K, key: string, fetcher: () => Promise<NonNullable<EvidenceState[K][string]['data']>>, force = false,
  ) {
    const cur = get()[table][key]
    if (cur && (cur.status === 'loading' || (cur.status === 'ok' && !force))) return
    // 重取时留着上一份：界面不闪回「正在取」
    const pending = (force && cur?.data ? { ...cur, status: 'loading' } : { status: 'loading' }) as Slot<unknown>
    set((s) => ({ [table]: { ...s[table], [key]: pending } }) as Partial<EvidenceState>)
    const settle = (slot: Slot<unknown>) => set((s) => (s[table][key] === pending
      ? { [table]: { ...s[table], [key]: slot } } as Partial<EvidenceState> : s))
    try {
      settle({ status: 'ok', data: await fetcher() })
    } catch (error) {
      settle({ status: 'error', error })
    }
  }

  return {
    owner: null,
    claim: (owner) => { if (get().owner !== owner) set({ owner }) },
    docs: {},
    graphs: {},
    segments: {},
    latest: {},
    loadDoc: (artifact) => fill('docs', artifact, async () => {
      const res = await api.artifact(artifact)
      const doc = asDoc(res?.content)
      if (!doc) throw new NotADocError()
      return doc
    }),
    loadGraph: (runId) => fill('graphs', runId, () => api.evidence.graph(runId)),
    reloadGraph: (runId) => fill('graphs', runId, () => api.evidence.graph(runId), true),
    loadSegment: (runId, segmentId, report) =>
      fill('segments', segmentKey(runId, segmentId, report), () => api.evidence.segment(runId, segmentId, { report })),
    // 最近一次运行 → 它封存范围内的报告核对（report.checked）。审计接口的 reports 带着结论句策略（claims，
    // 章上「无证据」要不要算没挂依据的结论句靠它）；只要 reports，所以只取最小的「可疑实体」那一组行。
    // 老后端没有审计接口时退回证据图（没有 claims，没挂依据的结论句不计入）。没有运行、没有报告都是空表，不画章
    loadLatest: (workflowId) => fill('latest', workflowId, async () => {
      const [run] = await api.runs.list({ workflow_id: workflowId, limit: 1 })
      if (!run) return { runId: null, reports: {} }
      const list = await api.evidence.audit(run.id, { groups: ['suspicious'] }).then((a) => a.reports ?? [], async (e) => {
        if (!(e instanceof ApiError && (e.status === 404 || e.status === 405) && !e.code)) throw e
        await get().loadGraph(run.id)
        return get().graphs[run.id]?.data?.reports ?? []
      })
      const reports: LatestReports['reports'] = {}
      for (const r of list) if (r?.node_id) reports[r.node_id] = { stats: r.stats, claims: r.claims }
      return { runId: run.id, reports }
    }),
    dropLatest: (workflowId) => {
      if (!(workflowId in get().latest)) return
      set((s) => {
        const latest = { ...s.latest }
        delete latest[workflowId]
        return { latest }
      })
    },
    judged: {},
    asks: {},
    runClasses: {},
    noteVerdicts: (runId, report, verdicts) => {
      const key = docKey(runId, report)
      const cur = get().judged[key] ?? {}
      // 内容一样就不换引用：正文按它重算徽标，片段接口每次答同样的判定不该让整份报告重渲染
      const fresh = Object.entries(verdicts).filter(([u, v]) => JSON.stringify(cur[u]) !== JSON.stringify(v))
      if (!fresh.length) return
      set((s) => ({ judged: { ...s.judged, [key]: { ...(s.judged[key] ?? {}), ...Object.fromEntries(fresh) } } }))
    },
    judge: async (runId, report, units) => {
      const keys = units.map((u) => askKey(runId, report, u))
      if (!units.length || keys.some((k) => get().asks[k]?.status === 'loading')) return
      const mark = (slot: Slot<EvidenceJudgeResult>) =>
        set((s) => ({ asks: { ...s.asks, ...Object.fromEntries(keys.map((k) => [k, slot])) } }))
      mark({ status: 'loading' })
      try {
        const res = await api.evidence.judge(runId, { units, ...(report ? { report } : {}) })
        get().noteVerdicts(runId, report, judgedVerdicts(res))
        mark({ status: 'ok', data: res })
      } catch (error) {
        mark({ status: 'error', error })
      }
    },
    loadRunClass: (runId) => {
      const cur = get().runClasses[runId]
      if (cur && cur.status !== 'error') return Promise.resolve()
      set((s) => ({ runClasses: { ...s.runClasses, [runId]: { status: 'loading' } } }))
      return api.runs.get(runId).then(
        (run) => set((s) => ({ runClasses: { ...s.runClasses, [runId]: { status: 'ok', data: run?.run_class ?? null } } })),
        (error) => set((s) => ({ runClasses: { ...s.runClasses, [runId]: { status: 'error', error } } })),
      )
    },
  }
})

/**
 * 一份报告封存之后追加的判定：证据图带来的（reports[].judged），叠上这一页里按需判的、片段接口答的
 * （后者新）。没有运行、什么都没有时是 undefined
 */
export function useVerdicts(runId: string | undefined, report: string | undefined):
  Record<string, EvidenceVerdict> | undefined {
  const own = useEvidence((s) => (runId ? s.judged[docKey(runId, report)] : undefined))
  const graph = useEvidence((s) => (runId ? s.graphs[runId]?.data : undefined))
  return useMemo(() => {
    const fromGraph = graphVerdicts(graph, report)
    if (!own && !Object.keys(fromGraph).length) return undefined
    return { ...fromGraph, ...(own ?? {}) }
  }, [own, graph, report])
}

/**
 * 这份报告出自探索运行吗（按需裁判只在探索运行里有）。开了裁判的文档自己记着：on_demand 是探索运行、
 * inline 是正式运行；没开裁判的看调用方给的运行类别，再没有就按运行 id 取一次（enabled 时才取）。
 * 还不知道时是 null：按钮先不出，免得在正式运行里闪一下
 */
export function useExplore(doc: Pick<EvidenceDocData, 'judge'>, runId: string | undefined, runClass: string | undefined,
  enabled: boolean): boolean | null {
  const mode = doc.judge?.mode
  const known = mode === 'on_demand' ? true : mode === 'inline' ? false
    : runClass ? runClass === 'exploratory' : undefined
  const slot = useEvidence((s) => (runId ? s.runClasses[runId] : undefined))
  const loadRunClass = useEvidence((s) => s.loadRunClass)
  const need = known === undefined && enabled && !!runId
  useEffect(() => { if (need && runId) void loadRunClass(runId) }, [need, runId, loadRunClass])
  if (known !== undefined) return known
  if (!runId) return false
  return slot?.status === 'ok' ? slot.data === 'exploratory' : null
}
