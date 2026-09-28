import { create } from 'zustand'
import { api } from '../api/client'
import type { EvidenceDocData, EvidenceGraph, EvidenceSegmentDetail } from '../types'

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
 */

export interface Slot<T> {
  status: 'loading' | 'ok' | 'error'
  data?: T
  error?: unknown
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
  loadDoc: (artifact: string) => Promise<void>
  loadGraph: (runId: string) => Promise<void>
  loadSegment: (runId: string, segmentId: string, report?: string) => Promise<void>
}

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
  /** 取一次、记进 table[key]；正在取或已经取到的不再发 */
  async function fill<K extends 'docs' | 'graphs' | 'segments'>(
    table: K, key: string, fetcher: () => Promise<NonNullable<EvidenceState[K][string]['data']>>,
  ) {
    const cur = get()[table][key]
    if (cur && cur.status !== 'error') return
    set((s) => ({ [table]: { ...s[table], [key]: { status: 'loading' } } }) as Partial<EvidenceState>)
    try {
      const data = await fetcher()
      set((s) => ({ [table]: { ...s[table], [key]: { status: 'ok', data } } }) as Partial<EvidenceState>)
    } catch (error) {
      set((s) => ({ [table]: { ...s[table], [key]: { status: 'error', error } } }) as Partial<EvidenceState>)
    }
  }

  return {
    owner: null,
    claim: (owner) => { if (get().owner !== owner) set({ owner }) },
    docs: {},
    graphs: {},
    segments: {},
    loadDoc: (artifact) => fill('docs', artifact, async () => {
      const res = await api.artifact(artifact)
      const doc = asDoc(res?.content)
      if (!doc) throw new NotADocError()
      return doc
    }),
    loadGraph: (runId) => fill('graphs', runId, () => api.evidence.graph(runId)),
    loadSegment: (runId, segmentId, report) =>
      fill('segments', segmentKey(runId, segmentId, report), () => api.evidence.segment(runId, segmentId, { report })),
  }
})
