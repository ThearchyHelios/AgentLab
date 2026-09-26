/**
 * 编排页的快捷键清单：处理器和提示文字出自这一份。
 *
 * 之前「变量」按钮和抽屉上写着 ⌥V，全仓却没有一个监听处理它；按了没反应，人就连带
 * 不信别的提示了。现在 StudioPage 的键盘处理照这张表分派，按钮的 title 用 hintOf
 * 拼出来，全局的「?」帮助浮层（W-shell）也直接读它——三处不会再各说各的。
 *
 * 组合键的写法和判定都走 lib/keys：'Mod' 在 Mac 上是 ⌘、其他平台是 Ctrl；字母按
 * e.code 判定（Mac 上 ⌥V 的 e.key 是「√」）。
 */
import { formatShortcut, isMac } from '../lib/keys'

export type StudioShortcutId =
  | 'save' | 'saveNote' | 'undo' | 'redo'
  | 'copy' | 'paste' | 'duplicate' | 'selectAll' | 'delete'
  | 'focus' | 'fit' | 'layout' | 'search'
  | 'nextProblem' | 'prevProblem'
  | 'problems' | 'variables' | 'history' | 'palette' | 'assistant' | 'close'

export interface StudioShortcut {
  id: StudioShortcutId
  /** 主组合键，显示用它 */
  combo: string
  /** 同样生效的别的写法（Windows 上的 Ctrl+Y 重做、Delete 删除） */
  alt?: string[]
  label: string
  group: '文件' | '编辑' | '画布' | '面板'
  /** 焦点在输入框里时也生效。只有保存是这样：写到一半按 ⌘S 是最常见的保存方式 */
  inInputs?: boolean
  /**
   * 由别处处理，这里只登记给帮助浮层看：删除由 React Flow 的 deleteKeyCode 处理，
   * Esc 由检查器和各个弹层自己处理
   */
  passive?: boolean
}

export const STUDIO_SHORTCUTS: StudioShortcut[] = [
  { id: 'save', combo: 'Mod+S', label: '保存', group: '文件', inInputs: true },
  { id: 'saveNote', combo: 'Mod+Shift+S', label: '保存并写版本说明', group: '文件', inInputs: true },

  { id: 'undo', combo: 'Mod+Z', label: '撤销', group: '编辑' },
  // Ctrl+Y 是 Windows 上的习惯；Mac 上 ⌘Y 是浏览器的「历史记录」，不抢
  { id: 'redo', combo: 'Mod+Shift+Z', alt: isMac ? [] : ['Mod+Y'], label: '重做', group: '编辑' },
  { id: 'copy', combo: 'Mod+C', label: '复制选中的节点', group: '编辑' },
  { id: 'paste', combo: 'Mod+V', label: '粘贴节点（落在视野中间）', group: '编辑' },
  { id: 'duplicate', combo: 'Mod+D', label: '原地复制一份', group: '编辑' },
  { id: 'selectAll', combo: 'Mod+A', label: '选中全部节点', group: '编辑' },
  { id: 'delete', combo: 'Backspace', alt: ['Delete'], label: '删除选中的节点或连线', group: '编辑', passive: true },

  { id: 'focus', combo: 'F', label: '镜头对准选中的节点（运行中没有选中时：逐个对准要处理的节点）', group: '画布' },
  { id: 'fit', combo: 'Shift+1', label: '缩放到看全所有节点', group: '画布' },
  { id: 'layout', combo: 'Shift+L', label: '自动排版', group: '画布' },
  { id: 'search', combo: '/', label: '搜索节点库', group: '画布' },
  { id: 'nextProblem', combo: 'F8', label: '跳到下一个问题', group: '画布' },
  { id: 'prevProblem', combo: 'Shift+F8', label: '跳到上一个问题', group: '画布' },

  { id: 'problems', combo: 'Alt+P', label: '问题面板', group: '面板' },
  { id: 'variables', combo: 'Alt+V', label: '变量抽屉', group: '面板' },
  { id: 'history', combo: 'Alt+H', label: '版本历史', group: '面板' },
  { id: 'palette', combo: 'Alt+N', label: '收起 / 展开节点库', group: '面板' },
  { id: 'assistant', combo: 'Alt+A', label: '收起 / 展开助手栏', group: '面板' },
  { id: 'close', combo: 'Esc', label: '关掉属性面板', group: '面板', passive: true },
]

export const STUDIO_SHORTCUT_GROUPS: StudioShortcut['group'][] = ['文件', '编辑', '画布', '面板']

const byId = new Map(STUDIO_SHORTCUTS.map((s) => [s.id, s]))

export function studioShortcut(id: StudioShortcutId): StudioShortcut {
  return byId.get(id)!
}

/** 按钮 title 用：「撤销（⌘Z）」。Windows 上是「撤销（Ctrl+Z）」 */
export function hintOf(label: string, id: StudioShortcutId): string {
  return `${label}（${formatShortcut(studioShortcut(id).combo)}）`
}
