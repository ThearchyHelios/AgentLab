import { createContext, useContext } from 'react'
import type { AppDetailOut } from '../../../api/apps'
import type { AppBindings, AppSpec, ComponentCatalog } from '../../../typesApp'

/** 应用编辑器各节共用的状态：草稿、岗位绑定、组件目录、保存、选中的组件、切到哪一节 */
export interface EditorState {
  appId: string
  /** 应用详情（manage）：带着草稿、岗位绑定和能按新口径补算的视图 */
  detail: AppDetailOut
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
  /**
   * 切到某一节。componentId：选中这个组件（页面一节）；path：检查问题的 path，落在入口、表单、待办外观上时，
   * 「入口与表单」一节按它在 JSON 里定位
   */
  goto: (section: string, componentId?: string, path?: string) => void
  /** 最近一次定位到的 path（入口与表单一节读它，定位完清掉） */
  focusPath: string | null
  clearFocus: () => void
}

export const EditorContext = createContext<EditorState | null>(null)

export function useEditor(): EditorState {
  const ctx = useContext(EditorContext)
  if (!ctx) throw new Error('useEditor 只能在应用编辑器里用')
  return ctx
}
