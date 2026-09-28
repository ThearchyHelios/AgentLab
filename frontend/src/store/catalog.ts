import { useEffect, useRef } from 'react'
import { create } from 'zustand'
import { ApiError, api, onConnectivity } from '../api/client'
import type { Approval, DataSource, Provider, Skill, ToolInfo, Workflow } from '../types'

/**
 * 后端连没连上。
 *
 * 之前 refresh 对每个请求 `.catch(() => [])`，然后照样 loaded:true——后端挂了，
 * 整站显示「还没有工作流 / 还没有接入数据源」并引导新建。在工业场景里这等于告诉
 * 用户"你的数据没了"，还会诱发重复配置。空和断必须分得开。
 */
export type BackendState = 'checking' | 'ok' | 'down'

/** catalog 里的一张表 */
export type CatalogKey = 'providers' | 'tools' | 'skills' | 'collections' | 'workflows' | 'approvals' | 'datasources'

/** 启动清单里的一行：catalog.refresh 的一个真实请求 */
export interface CatalogCheck {
  key: CatalogKey
  label: string
  state: 'pending' | 'ok' | 'error'
  ms?: number
  status?: number
  error?: string
}

/** 各张表的中文名。启动清单、「正在读取工作流」这类空态用 */
export const CATALOG_LABELS: Record<CatalogKey, string> = {
  providers: '模型接入', tools: '工具', skills: 'Skill',
  collections: '知识库', workflows: '工作流', approvals: '待审批', datasources: '数据源',
}

const CATALOG_KEYS = Object.keys(CATALOG_LABELS) as CatalogKey[]

/**
 * refresh 里每个请求最多等多久。一个接口卡住（MCP 服务不应答时的 /tools）不能
 * 让它那张表永远停在「加载中」；到点掐断、记成出错，别的表不受影响
 */
export const CATALOG_TIMEOUT_MS = 15_000

/** 心跳间隔：连着时复用 4 秒一次的待审批轮询，不另起请求 */
const HEARTBEAT_MS = 4000
/** 断开后的重试退避 */
const BACKOFF_MS = [4000, 8000, 16000]

/** 全局参照数据：属性面板的各种下拉都从这里取，只加载一次。 */
export interface CatalogState {
  providers: Provider[]
  tools: ToolInfo[]
  skills: Skill[]
  collections: { collection: string; documents: number; chunks: number }[]
  workflows: Workflow[]
  approvals: Approval[]
  /**
   * 数据源，停用的也在（各行 enabled 区分）。它们的查询工具 db_query__<源> / db_schema__<源>
   * 不在 /api/tools 里，挑工具、问数据页的范围、助手的数据源提示都从这里取一份
   */
  datasources: DataSource[]
  /**
   * 启动后的第一次 refresh 已经落定（每个请求都回来了或超时了）。只从 false 变 true，
   * 之后的刷新不会把它打回去——所以它不能回答「这张表是真的空还是没取到」，那个
   * 看 loadedAt / catalogListState
   */
  loaded: boolean
  /** 每张表最近一次成功取回的时刻（ms）。没有这一项 = 从没取到过，空列表不能当「没有」 */
  loadedAt: Partial<Record<CatalogKey, number>>
  /**
   * 重拉全部六张表，谁先回来先填谁：一张表卡住不拖累别的表。每个请求最多等
   * CATALOG_TIMEOUT_MS，超时记成那一项出错
   */
  refresh: () => Promise<void>
  refreshApprovals: () => Promise<void>
  /**
   * 只重拉一张表。页面改了它（数据页增删改了数据源）就调一次，别处的下拉立刻跟上；
   * 失败时列表保持原样。不动启动清单
   */
  reload: (key: CatalogKey) => Promise<void>

  backend: BackendState
  /** 最近一次成功请求的往返毫秒。没测到是 null */
  latencyMs: number | null
  /** 最后一次确认连通的时刻（ms） */
  lastOkAt: number | null
  /** 断开的原因，给横幅和遥测点的悬停说明 */
  backendError: string | null
  /** 断开时下一次自动重试的时刻（ms），横幅倒计时用 */
  retryAt: number | null
  /** 最近一次 refresh 的逐项结果，启动页的清单用 */
  checks: CatalogCheck[]
  /** 断开后又连上的次数。页面用 useOnReconnect 订阅它，重拉自己的列表 */
  reconnects: number
  /** 立刻探一次 /health。「立即重试」按钮调它 */
  checkBackend: () => Promise<boolean>
  /**
   * 开始心跳：连着时每 4 秒拉一次待审批（顺便测延迟）。断开后的重试不归它管，
   * catalog 自己按 4→8→16 秒退避探 /health，恢复后自动 refresh。返回停止函数。
   * 重复调用只会有一个心跳在跑。
   */
  startHeartbeat: () => () => void
}

let probing: Promise<boolean> | null = null
let retryTimer: ReturnType<typeof setTimeout> | undefined
let heartbeatOwner: symbol | null = null
let markUpRef: (latencyMs?: number) => void = () => {}
/** 第几次 refresh。启动清单只归最新一次 refresh 写 */
let refreshEpoch = 0
/**
 * 每个请求发出时的序号。同一张表的两个请求交叠时（refresh、reload、心跳），
 * 晚发的那次的结果不能被早发、晚到的覆盖
 */
let fetchSeq = 0
const appliedSeq: Partial<Record<CatalogKey, number>> = {}
/** 这次结果还能不能写：同一张表已经有更晚发出的请求填过了就作废 */
const claim = (key: CatalogKey, seq: number): boolean => {
  if ((appliedSeq[key] ?? 0) > seq) return false
  appliedSeq[key] = seq
  return true
}

const catalogOpts = { timeoutMs: CATALOG_TIMEOUT_MS }
const FETCHERS: Record<CatalogKey, () => Promise<unknown[]>> = {
  providers: () => api.providers.list(catalogOpts),
  tools: () => api.tools.list(catalogOpts),
  skills: () => api.skills.list(catalogOpts),
  collections: () => api.kb.collections(catalogOpts),
  workflows: () => api.workflows.list(catalogOpts),
  approvals: () => api.approvals.list('pending', catalogOpts),
  datasources: () => api.datasources.list(catalogOpts),
}

export const useCatalog = create<CatalogState>((set, get) => {
  let backoffStep = 0

  const markDown = (error: string) => {
    // 已经断着再失败一次，就把下一次重试往后推一档。所以"发现断开"的各条路
    // （refresh、refreshApprovals、onConnectivity）都只在还没判 down 时才探活，
    // 否则同一次断开记两笔，第一次就从 8 秒起跳
    backoffStep = get().backend === 'down' ? Math.min(backoffStep + 1, BACKOFF_MS.length - 1) : 0
    const delay = BACKOFF_MS[backoffStep]
    set({ backend: 'down', backendError: error, latencyMs: null, retryAt: Date.now() + delay })
    // 重试自己排，不搭调用方轮询的车：App 里 4 秒一拍的轮询会把「4 秒后重试」
    // 拖成 6、7 秒，横幅倒计时走到 0 还得再干等
    clearTimeout(retryTimer)
    retryTimer = setTimeout(() => { retryTimer = undefined; void get().checkBackend() }, delay)
  }

  const markUp = (latencyMs?: number) => {
    const wasDown = get().backend === 'down'
    backoffStep = 0
    clearTimeout(retryTimer)
    retryTimer = undefined
    set((s) => ({
      backend: 'ok', backendError: null, retryAt: null, lastOkAt: Date.now(),
      ...(latencyMs != null ? { latencyMs } : {}),
      ...(wasDown ? { reconnects: s.reconnects + 1 } : {}),
    }))
    // 恢复了就把断开期间的空列表换回真数据，用户不用手动刷新。这里只管 catalog
    // 自己的几张表；页面自己拉的列表靠 useOnReconnect
    if (wasDown) void get().refresh()
  }
  markUpRef = markUp

  return {
    providers: [],
    tools: [],
    skills: [],
    collections: [],
    workflows: [],
    approvals: [],
    datasources: [],
    loaded: false,
    loadedAt: {},

    backend: 'checking',
    latencyMs: null,
    lastOkAt: null,
    backendError: null,
    retryAt: null,
    checks: [],
    reconnects: 0,

    refresh: async () => {
      const gen = ++refreshEpoch
      const keys = CATALOG_KEYS
      set({ checks: keys.map((key) => ({ key, label: CATALOG_LABELS[key], state: 'pending' })) })
      // 清单只归最新一次 refresh 写：交叠时早发的那次别把新清单的行改回去
      const update = (key: CatalogKey, patch: Partial<CatalogCheck>) => {
        if (gen !== refreshEpoch) return
        set((s) => ({ checks: s.checks.map((c) => (c.key === key ? { ...c, ...patch } : c)) }))
      }
      let networkFailures = 0
      // 各张表回来一张填一张：以前是 Promise.all 全到齐才一起写，/tools 卡着不回，
      // 工作流也跟着一直是空的，编排页就说「还没有工作流」。单项失败各自兜底，
      // 列表保持原样（不清成 []）；但要记下是不是"够不着"，六个全是网络失败就是
      // 后端断了，不是"什么都没有"
      const track = async (key: CatalogKey) => {
        const t0 = performance.now()
        const seq = ++fetchSeq
        try {
          const rows = await FETCHERS[key]()
          update(key, { state: 'ok', ms: Math.round(performance.now() - t0) })
          if (!claim(key, seq)) return
          set((s) => ({ [key]: rows, loadedAt: { ...s.loadedAt, [key]: Date.now() } }) as Partial<CatalogState>)
        } catch (e) {
          const err = e instanceof ApiError ? e : null
          if (err?.kind === 'network') networkFailures++
          update(key, {
            state: 'error', ms: Math.round(performance.now() - t0),
            status: err?.status || undefined,
            error: err?.message ?? String((e as any)?.message ?? e),
          })
        }
      }
      const t0 = performance.now()
      await Promise.all(keys.map(track))
      if (!get().loaded) set({ loaded: true })
      if (networkFailures === keys.length) {
        // 第一个失败的请求已经通过 onConnectivity 起了一次探活；这里并进同一次，
        // 不另记一笔——两笔会让第一次断开就跳到退避的第二档（8 秒而不是 4 秒）
        if (get().backend !== 'down') await get().checkBackend()
      } else if (networkFailures === 0) {
        markUp(Math.round(performance.now() - t0))
      }
    },

    refreshApprovals: async () => {
      // 断开期间不跟着调用方的 4 秒轮询一起敲，重试按 markDown 排的退避走——
      // 否则横幅上「16 秒后重试」是假的。过了点还没探（定时器被节流之类）才补一次
      const { backend, retryAt } = get()
      if (backend === 'down') {
        if (!retryAt || Date.now() >= retryAt) await get().checkBackend()
        return
      }
      const t0 = performance.now()
      try {
        const seq = ++fetchSeq
        const approvals = await api.approvals.list('pending', catalogOpts)
        // 这一拍在路上时又发起了 refresh、而且那次已经填过了：以那次为准
        if (!claim('approvals', seq)) return
        set((s) => ({
          approvals, latencyMs: Math.round(performance.now() - t0), lastOkAt: Date.now(),
          loadedAt: { ...s.loadedAt, approvals: Date.now() },
        }))
      } catch (e) {
        // 后端回了错误码说明它还活着，列表保持原样；够不着才去确认是否断开
        if (e instanceof ApiError && e.kind === 'network' && get().backend !== 'down') await get().checkBackend()
      }
    },

    reload: async (key) => {
      const seq = ++fetchSeq
      try {
        const rows = await FETCHERS[key]()
        if (!claim(key, seq)) return
        set((s) => ({ [key]: rows, loadedAt: { ...s.loadedAt, [key]: Date.now() } }) as Partial<CatalogState>)
      } catch (e) {
        if (e instanceof ApiError && e.kind === 'network' && get().backend !== 'down') await get().checkBackend()
      }
    },

    checkBackend: () => {
      if (probing) return probing
      probing = (async () => {
        const t0 = performance.now()
        try {
          await api.health()
          markUp(Math.round(performance.now() - t0))
          return true
        } catch (e) {
          markDown(e instanceof ApiError ? e.message : String((e as any)?.message ?? e))
          return false
        } finally {
          probing = null
        }
      })()
      return probing
    },

    startHeartbeat: () => {
      const me = Symbol('heartbeat')
      heartbeatOwner = me
      let timer: ReturnType<typeof setTimeout> | undefined
      // 链式 setTimeout 而不是 setInterval：后端慢的时候上一拍没回来，不叠下一拍
      const tick = async () => {
        if (heartbeatOwner !== me) return
        await get().refreshApprovals()
        if (heartbeatOwner !== me) return
        timer = setTimeout(() => void tick(), HEARTBEAT_MS)
      }
      timer = setTimeout(() => void tick(), HEARTBEAT_MS)
      return () => {
        if (heartbeatOwner === me) heartbeatOwner = null
        if (timer) clearTimeout(timer)
      }
    },
  }
})

// 任何一个请求的成败都是连接状态的证据：页面上的保存、加载失败于网络时立刻去
// 确认一次，不必等下一拍心跳；断开期间任何请求成功了就算恢复
onConnectivity((reachable) => {
  const s = useCatalog.getState()
  if (reachable) {
    if (s.backend !== 'ok') markUpRef()
  } else if (s.backend !== 'down') {
    // 一次失败先别急着宣布断开：再探一次 /health，两次都够不着才算——一次代理
    // 抖动不该让整站横幅闪一下
    void s.checkBackend()
  }
})

/**
 * 后端断开又连上时调用 cb。
 *
 * 断开期间页面自己拉的列表（运行记录、数据源、会话、知识库文档……）拿到的是
 * 空数组；恢复时 catalog 只会刷新它自己那几张表，页面不重拉，就又回到「还没有
 * 运行记录」这种假的空态。首次连上（checking → ok）不算，进页时不会多拉一遍。
 * cb 总是用最新的那一个，调用方不用 useCallback。
 */
export function useOnReconnect(cb: () => void): void {
  const cbRef = useRef(cb)
  useEffect(() => { cbRef.current = cb })
  useEffect(() => useCatalog.subscribe((s, prev) => {
    if (s.reconnects !== prev.reconnects) cbRef.current()
  }), [])
}

/**
 * 某张表眼下能不能当真：
 * - ok：至少成功取回过一次。之后的刷新失败了，手上的也还是真数据；
 * - offline：从没取到过，后端也断着；
 * - loading：从没取到过，请求还在路上（或还没发）；
 * - error：从没取到过，最近一次请求出错或超时了。
 * 后三种时列表是空的，但那不是「没有」。
 */
export type CatalogListState = 'ok' | 'loading' | 'error' | 'offline'

export function catalogListState(
  s: Pick<CatalogState, 'loadedAt' | 'backend' | 'checks'>, key: CatalogKey,
): CatalogListState {
  if (s.loadedAt[key] != null) return 'ok'
  if (s.backend === 'down') return 'offline'
  return s.checks.find((c) => c.key === key)?.state === 'error' ? 'error' : 'loading'
}

/** 同 catalogListState，订阅版。页面判断「空」之前先看它是不是 ok */
export function useCatalogListState(key: CatalogKey): CatalogListState {
  return useCatalog((s) => catalogListState(s, key))
}

/** 超过这么久没取过就在挂载时重拉一次：刚在别的标签页加的源也不能一直看不见 */
export const DATASOURCES_MAX_AGE_MS = 30_000
const reloading: Partial<Record<CatalogKey, Promise<void>>> = {}

/**
 * 数据源目录（订阅版）。list 含停用的，按需自己 filter(enabled)；state 同
 * catalogListState，不是 ok 时空列表不代表「没有数据源」，别劝人去新建。
 *
 * 挂载时如果目录超过 maxAgeMs 没取过就后台重拉一次，先照旧给手上的那份。
 * 同一时刻多处挂载只会发一个请求（reload 自己不去重，这里记着在路上的那一次）。
 * 启动的 refresh 还没落定时先不拉（那次本来就会取）；落定时这张表没取回来，就补拉一次
 */
export function useDatasources(opts?: { maxAgeMs?: number }): { list: DataSource[]; state: CatalogListState } {
  const list = useCatalog((s) => s.datasources)
  const state = useCatalogListState('datasources')
  const loaded = useCatalog((s) => s.loaded)
  const maxAge = opts?.maxAgeMs ?? DATASOURCES_MAX_AGE_MS
  useEffect(() => {
    if (!loaded) return
    const s = useCatalog.getState()
    const at = s.loadedAt.datasources
    if ((at == null || Date.now() - at > maxAge) && !reloading.datasources) {
      reloading.datasources = s.reload('datasources').finally(() => { reloading.datasources = undefined })
    }
  }, [maxAge, loaded])
  return { list, state }
}

/**
 * 数据源给模型的两个工具名。新后端的行里带 tools；老后端没有就按命名规则拼
 * （db_query__<name> / db_schema__<name>，后端 tools/datasource.tool_names）
 */
export function datasourceTools(src: Pick<DataSource, 'name' | 'tools'>): string[] {
  return Array.isArray(src.tools) && src.tools.length ? src.tools : [`db_query__${src.name}`, `db_schema__${src.name}`]
}

/** 这条运行有没有待处理的审批。区分「等待审批」和「已挂起 · 可续跑」要用它 */
export function hasPendingApproval(approvals: Approval[], runId: string | null | undefined): boolean {
  if (!runId) return false
  return approvals.some((a) => a.run_id === runId && a.status === 'pending')
}

/** 把所有 provider 的模型拉平成下拉选项。 */
export function modelOptions(providers: Provider[]): { value: string; label: string; group: string }[] {
  const out: { value: string; label: string; group: string }[] = []
  for (const p of providers) {
    if (!p.enabled) continue
    for (const m of p.models ?? []) {
      out.push({ value: m.id, label: m.label || m.id, group: p.name })
    }
    if (!(p.models ?? []).length && p.default_model) {
      out.push({ value: p.default_model, label: p.default_model, group: p.name })
    }
  }
  return out
}
