import { createContext, useContext } from 'react'
import type { AppBindings, AppDetail, AppSpec, ComponentCatalog } from '../../../typesApp'

/** 应用编辑器各节共用的状态：草稿、岗位绑定、组件目录、保存、选中的组件、切到哪一节 */
export interface EditorState {
  appId: string
  detail: AppDetail
  draft: AppSpec
  /** 改草稿：传函数拿到最新的那一份（助手的操作和人的编辑可能挨着到） */
  setDraft: (fn: (spec: AppSpec) => AppSpec) => void
  bindings: AppBindings
  setBindings: (fn: (b: AppBindings) => AppBindings) => void
  catalog: ComponentCatalog | null
  dirty: boolean
  /** 有未保存的修改时先存；返回存没存上 */
  save: () => Promise<boolean>
  selected: string | null
  select: (componentId: string | null) => void
  goto: (section: string, componentId?: string) => void
}

export const EditorContext = createContext<EditorState | null>(null)

export function useEditor(): EditorState {
  const ctx = useContext(EditorContext)
  if (!ctx) throw new Error('useEditor 只能在应用编辑器里用')
  return ctx
}
