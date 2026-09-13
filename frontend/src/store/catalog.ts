import { create } from 'zustand'
import { api } from '../api/client'
import type { Approval, Provider, Skill, ToolInfo, Workflow } from '../types'

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
}

export const useCatalog = create<CatalogState>((set) => ({
  providers: [],
  tools: [],
  skills: [],
  collections: [],
  workflows: [],
  approvals: [],
  loaded: false,

  refresh: async () => {
    // 单项失败不该让整个面板空掉，各自兜底
    const [providers, tools, skills, collections, workflows, approvals] = await Promise.all([
      api.providers.list().catch(() => []),
      api.tools.list().catch(() => []),
      api.skills.list().catch(() => []),
      api.kb.collections().catch(() => []),
      api.workflows.list().catch(() => []),
      api.approvals.list().catch(() => []),
    ])
    set({ providers, tools, skills, collections, workflows, approvals, loaded: true })
  },

  refreshApprovals: async () => {
    set({ approvals: await api.approvals.list().catch(() => []) })
  },
}))

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
