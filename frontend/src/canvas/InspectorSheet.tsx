import { useEffect } from 'react'
import { ChevronLeft } from 'lucide-react'
import clsx from 'clsx'
import { create } from 'zustand'
import { useStudio } from '../store/studio'
import { StatusBadge } from '../components/ui'
import { formatOffset } from '../lib/format'
import { isTypingTarget } from '../lib/keys'
import { statusMeta } from '../lib/status'
import { useRunGlance } from '../run/RunHud'
import { isActivePhase } from '../run/trace'
import { Inspector } from './Inspector'
import { parseFieldPath, type FieldRef } from './issues'

type StudioSnapshot = ReturnType<typeof useStudio.getState>

/**
 * 属性面板此刻为哪个节点开着。选中节点不再一律等于打开：看运行时点了跑过的节点，
 * 右栏要留给那个节点的步骤（runfx-11），面板等人明确要了才盖上来。
 */
export const useSheet = create<{ open: string | null }>(() => ({ open: null }))

/**
 * 点这个节点是不是应该先看它的步骤：在看运行（进行中，或者在回放），而它这次
 * 跑过——没跑过的节点在右栏里没有步骤可看，那就照常打开属性面板
 */
export function stepsFirst(id: string, s: StudioSnapshot = useStudio.getState()): boolean {
  if (!isActivePhase(s.runPhase) && s.replayAt == null) return false
  const n = s.trace?.nodes[id]
  return !!n && (n.count > 0 || n.state !== 'idle')
}

/** 明确要看配置（双击节点、画布上的「…的配置」）：运行中也打开 */
export function openInspector(id: string): void {
  // 先记下再选中：选中触发的订阅看到它已经点名要开，就不再按「先看步骤」收回去
  useSheet.setState({ open: id })
  useStudio.getState().select(id)
}

/**
 * 光标落到哪：先找能填的——输入框、下拉，或控件自己点名的入口（data-reveal-focus，比如工具
 * 多选的「添加」）——按文档顺序取第一个；一个都没有才退到按钮，而且不落在「移除」「删除」上。
 * 从「工具被去掉了」点定位过来的人是要把工具加回去的：光标停在已绑工具芯片的 × 上，
 * 顺手一个回车又解绑一个
 */
const FILLABLE = '[data-reveal-focus]:not([disabled]), input:not([type="hidden"]):not([disabled]):not([tabindex="-1"]), textarea:not([disabled]), select:not([disabled])'
const PRESSABLE = 'button:not([disabled]):not([aria-label^="移除"]):not([aria-label^="删除"])'

/**
 * 落到检查器里的某一栏：选中节点、画布取景、打开属性面板、滚到那一栏；focus 时把光标放进去。
 * 问题面板的定位、助手自查问题的「定位」都走这里——只选中节点的话，人还得在十几个字段里
 * 自己找是第几个分支的条件、哪个成员的工具。
 *
 * field 是后端给的路径（'cases[1].condition'、'agents[0].tools'）或解析好的 FieldRef。
 * 能落到第几项、项里的哪一栏就落到那儿，落不到就退一层；认不出来（老后端没给 field）
 * 就只选中、取景，不乱抢焦点。运行中也打开面板：要改的就是那一栏
 */
export function revealField(nodeId: string, field?: FieldRef | string | null, opts: { focus?: boolean } = {}): void {
  const s = useStudio.getState()
  if (!s.nodes.some((n) => n.id === nodeId)) return
  openInspector(nodeId)
  s.focusNode(nodeId)
  const at = typeof field === 'string' ? parseFieldPath(field) : field ?? null
  if (!at) return
  // 面板跟着选中挂上：等它渲染出来再找
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const sheet = document.querySelector('[data-inspector-sheet]') ?? document
    const box = sheet.querySelector<HTMLElement>(`[data-field="${CSS.escape(at.key)}"]`)
    const item = at.index != null ? box?.querySelector<HTMLElement>(`[data-item="${at.index}"]`) : null
    // 只有子键没有序号的（合并查询的 inputs.<别名>）：在整个字段里按子键找那一行
    const part = at.sub ? (item ?? box)?.querySelector<HTMLElement>(`[data-sub="${CSS.escape(at.sub)}"]`) : null
    const target = part ?? item ?? box
    if (!target) return
    const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    target.scrollIntoView({ block: 'center', behavior: still ? 'auto' : 'smooth' })
    if (opts.focus) {
      const to = target.querySelector<HTMLElement>(FILLABLE) ?? target.querySelector<HTMLElement>(PRESSABLE)
      to?.focus({ preventScroll: true })
    }
    // 子键没有自己的一行、字段是一段 JSON（调用工具的「参数」，问题落在 args.sql 上）：在文字里找到那个键，
    // 框内滚到那一行，聚焦时光标放在它的值开头
    if (!part && at.sub) revealJsonKey(target, at.sub, !!opts.focus)
  }))
}

/** JSON 文字框里的某个键：滚到它那一行；focus 时把光标放在值的开头（不选中，免得一敲键就把整段 SQL 换掉） */
function revealJsonKey(field: HTMLElement, key: string, focus: boolean): void {
  const box = field.querySelector<HTMLTextAreaElement>('textarea')
  if (!box) return
  const m = new RegExp(`"${key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}"\\s*:\\s*"?`).exec(box.value)
  if (!m) return
  const line = box.value.slice(0, m.index).split('\n').length - 1
  const height = parseFloat(getComputedStyle(box).lineHeight) || 16
  box.scrollTop = Math.max(0, (line - 1) * height)
  if (focus) {
    const at = m.index + m[0].length
    box.focus({ preventScroll: true })
    box.setSelectionRange(at, at)
  }
}

// 选中 → 面板开不开。放在模块级：选中可能来自别处（问题面板定位、发布弹窗、快捷键），
// 规则只写这一份
useStudio.subscribe((s, prev) => {
  if (s.selectedId === prev.selectedId) return
  const id = s.selectedId
  const cur = useSheet.getState().open
  if (!id) {
    if (cur) useSheet.setState({ open: null })
    return
  }
  if (cur === id) return
  useSheet.setState({ open: stepsFirst(id, s) ? null : id })
})

/**
 * 属性面板：盖在助手栏上的一层，不是和它并列的 tab。
 *
 * 之前两者是 [助手|属性] 两个 tab。tab 是互斥的，而这两件事在时间上不互斥——
 * 跑图跑到一半想点开某个节点看配置，整条执行过程就从眼前消失了；切回来时
 * 滚动位置、展开的步骤、输入框里打了一半的字全没（tab 切换会卸载组件）。
 *
 * 改成"盖上去"之后：助手一直在下面活着，属性是临时造访者，关掉就回到原处。
 * 选中节点即滑入，Esc 或点画布空白处即滑出——不需要专门去找一个 tab。
 * 例外是看运行时点了跑过的节点：那时要看的是它的步骤，面板不自动盖上来（见 useSheet）。
 */
export function InspectorSheet() {
  const selectedId = useStudio((s) => s.selectedId)
  const select = useStudio((s) => s.select)
  const open = useSheet((s) => s.open)

  // Esc 关闭。这是"覆盖层"这种东西的通用约定，没有它就只能去找那个 ×
  useEffect(() => {
    if (!selectedId) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      // 正在输入框里打字时 Esc 是给输入法用的，别抢
      if (isTypingTarget()) return
      // 标记用掉了：同一下 Esc 不该再去清运行结果（见 RunHud 的 escapeRun）
      e.preventDefault()
      select(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [selectedId, select])

  if (!selectedId || open !== selectedId) return null

  return (
    // key 带上 selectedId：换一个节点时重播一次滑入，让人知道内容换了。
    // 不重播的话，点另一个节点看起来像什么都没发生
    <div key={selectedId} data-inspector-sheet
         className="sheet-in absolute inset-0 z-20 flex flex-col overflow-hidden bg-panel"
         style={{ boxShadow: '-10px 0 28px -14px rgba(0,0,0,.5)' }}>
      <div className="min-h-0 flex-1">
        <Inspector />
      </div>
      <LiveStrip />
    </div>
  )
}

/**
 * 盖住助手栏期间，底部留一条执行进度。
 *
 * 不留的话，跑图跑到一半点开节点看配置，运行就从眼前彻底消失了——这正是
 * 原来 tab 方案最难受的地方，不能因为换成"盖一层"就原样留着。点一下回去。
 *
 * 相位读 store 的 runPhase（由事件推出），和工具栏胶囊同一份：以前这里看的是
 * 4 秒轮询一次的审批列表，中断后有几秒还在说"正在执行"。失败、挂起这两种要人
 * 处理的结局也留着，别的结局不再占这一行。
 */
function LiveStrip() {
  const phase = useStudio((s) => s.runPhase)
  const select = useStudio((s) => s.select)
  const show = isActivePhase(phase) || phase === 'failed' || phase === 'suspended'
  if (!show) return null
  return <LiveStripBody onBack={() => select(null)} />
}

function LiveStripBody({ onBack }: { onBack: () => void }) {
  const g = useRunGlance(true)
  const meta = statusMeta(g.code)
  const alert = meta.alert
  return (
    <button
      type="button"
      className="fade-up flex w-full shrink-0 items-center gap-2 border-t px-2.5 py-1.5 text-left text-2xs transition-colors hover:bg-hover"
      style={alert ? { background: meta.soft } : undefined}
      title={`${g.label} · ${g.headline}\n返回助手查看完整过程`}
      data-run-phase={g.phase}
      onClick={onBack}
    >
      <StatusBadge status={g.code} size={12} animate={g.executing} decorative />
      <span className="shrink-0 font-medium" style={{ color: alert ? meta.color : undefined }}>{g.label}</span>
      <span className={clsx('min-w-0 flex-1 truncate', alert ? 'text-fg' : 'text-dim')}>{g.headline}</span>
      {isActivePhase(g.phase) && (
        <span className="shrink-0 tnum text-faint">{formatOffset(g.elapsedMs)}</span>
      )}
      <ChevronLeft size={11} className="shrink-0 rotate-180 text-faint" />
    </button>
  )
}
