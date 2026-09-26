import { useEffect, useRef } from 'react'
import { create } from 'zustand'
import { ApiError, api, onConnectivity } from '../api/client'
import type { Approval, Provider, Skill, ToolInfo, Workflow } from '../types'

/**
 * 后端连没连上。
 *
 * 之前 refresh 对每个请求 `.catch(() => [])`，然后照样 loaded:true——后端挂了，
 * 整站显示「还没有工作流 / 还没有接入数据源」并引导新建。在工业场景里这等于告诉
 * 用户"你的数据没了"，还会诱发重复配置。空和断必须分得开。
 */
export type BackendState = 'checking' | 'ok' | 'down'

/** 启动清单里的一行：catalog.refresh 的一个真实请求 */
export interface CatalogCheck {
  key: 'providers' | 'tools' | 'skills' | 'collections' | 'workflows' | 'approvals'
  label: string
  state: 'pending' | 'ok' | 'error'
  ms?: number
  status?: number
  error?: string
}

const CHECK_LABELS: Record<CatalogCheck['key'], string> = {
  providers: '模型接入', tools: '工具', skills: 'Skill',
  collections: '知识库', workflows: '工作流', approvals: '待审批',
}

/** 心跳间隔：连着时复用 4 秒一次的待审批轮询，不另起请求 */
const HEARTBEAT_MS = 4000
/** 断开后的重试退避 */
const BACKOFF_MS = [4000, 8000, 16000]

/** 全局参照数据：属性面板的各种下拉都从这里取，只加载一次。 */
interface CatalogState {
  providers: Provider[]
  tools: ToolInfo[]
  skills: Skill[]
  collections: { collection: string; documents: number; chunks: number }[]
  workflows: Workflow[]
  approvals: Approval[]
  loaded: boolean
  refresh: () => Promise<void>
  refreshApprovals: () => Promise<void>

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
    loaded: false,

    backend: 'checking',
    latencyMs: null,
    lastOkAt: null,
    backendError: null,
    retryAt: null,
    checks: [],
    reconnects: 0,

    refresh: async () => {
      const keys = Object.keys(CHECK_LABELS) as CatalogCheck['key'][]
      set({ checks: keys.map((key) => ({ key, label: CHECK_LABELS[key], state: 'pending' })) })
      const update = (key: CatalogCheck['key'], patch: Partial<CatalogCheck>) =>
        set((s) => ({ checks: s.checks.map((c) => (c.key === key ? { ...c, ...patch } : c)) }))

      let networkFailures = 0
      let lastError = ''
      // 单项失败不该让整个面板空掉，各自兜底；但要记下是不是"够不着"，
      // 六个全是网络失败就是后端断了，不是"什么都没有"
      const track = <T>(key: CatalogCheck['key'], p: Promise<T>): Promise<T | []> => {
        const t0 = performance.now()
        return p.then(
          (v) => { update(key, { state: 'ok', ms: Math.round(performance.now() - t0) }); return v },
          (e: unknown) => {
            const err = e instanceof ApiError ? e : null
            if (err?.kind === 'network') networkFailures++
            lastError = err?.message ?? String((e as any)?.message ?? e)
            update(key, {
              state: 'error', ms: Math.round(performance.now() - t0),
              status: err?.status || undefined, error: lastError,
            })
            return []
          },
        )
      }
      const t0 = performance.now()
      const [providers, tools, skills, collections, workflows, approvals] = await Promise.all([
        track('providers', api.providers.list()),
        track('tools', api.tools.list()),
        track('skills', api.skills.list()),
        track('collections', api.kb.collections()),
        track('workflows', api.workflows.list()),
        track('approvals', api.approvals.list()),
      ])
      set({ providers, tools, skills, collections, workflows, approvals, loaded: true })
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
        const approvals = await api.approvals.list()
        set({ approvals, latencyMs: Math.round(performance.now() - t0), lastOkAt: Date.now() })
      } catch (e) {
        // 后端回了错误码说明它还活着，列表保持原样；够不着才去确认是否断开
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
