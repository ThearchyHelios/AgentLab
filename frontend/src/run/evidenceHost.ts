import { createContext, useContext } from 'react'

/**
 * 证据面板所在的地方能做什么。
 *
 * 同一个面板在问数据页、记录页、画布右栏里各出现一次，只有画布右栏旁边有一张图：
 * 点开片段时要把证据路径画到图上，面板里的节点名点一下要选中并对准那个节点。
 * 这些靠上下文交给面板，不顺着 AssistantStream → Output → EvidenceDoc 一层层传 props；
 * 别处不提供，面板就照旧（节点名是文字，不画路径）。
 */
export interface EvidenceTrace {
  /** 画布上血缘层的标签：「证据 8.7%」 */
  label: string
  /** 产出证据的节点：查询、口径卡、检索 */
  producers: string[]
  /** 用到它的节点：报告 */
  consumers: string[]
}

export interface EvidenceHost {
  /** 片段打开时交出证据路径，关掉时交 null */
  onTrace?: (trace: EvidenceTrace | null) => void
  /** 面板里的节点名点一下：选中并对准画布上的这个节点 */
  onNode?: (nodeId: string) => void
  /** 节点 id → 画布上的名字；画布上没有这个节点时返回 undefined（不给按钮，点了会落空） */
  nodeLabel?: (nodeId: string) => string | undefined
}

export const EvidenceHostContext = createContext<EvidenceHost>({})

export const useEvidenceHost = (): EvidenceHost => useContext(EvidenceHostContext)
