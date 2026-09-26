import { useEffect } from 'react'
import { ChevronLeft } from 'lucide-react'
import clsx from 'clsx'
import { useStudio } from '../store/studio'
import { StatusBadge } from '../components/ui'
import { formatClock } from '../lib/format'
import { isTypingTarget } from '../lib/keys'
import { statusMeta } from '../lib/status'
import { useRunGlance } from '../run/RunHud'
import { isActivePhase } from '../run/trace'
import { Inspector } from './Inspector'

/**
 * 属性面板：盖在助手栏上的一层，不是和它并列的 tab。
 *
 * 之前两者是 [助手|属性] 两个 tab。tab 是互斥的，而这两件事在时间上不互斥——
 * 跑图跑到一半想点开某个节点看配置，整条执行过程就从眼前消失了；切回来时
 * 滚动位置、展开的步骤、输入框里打了一半的字全没（tab 切换会卸载组件）。
 *
 * 改成"盖上去"之后：助手一直在下面活着，属性是临时造访者，关掉就回到原处。
 * 选中节点即滑入，Esc 或点画布空白处即滑出——不需要专门去找一个 tab。
 */
export function InspectorSheet() {
  const selectedId = useStudio((s) => s.selectedId)
  const select = useStudio((s) => s.select)

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

  if (!selectedId) return null

  return (
    // key 带上 selectedId：换一个节点时重播一次滑入，让人知道内容换了。
    // 不重播的话，点另一个节点看起来像什么都没发生
    <div key={selectedId}
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
      title={`${g.label} · ${g.headline}\n回到助手看完整过程`}
      data-run-phase={g.phase}
      onClick={onBack}
    >
      <StatusBadge status={g.code} size={12} animate={g.executing} decorative />
      <span className="shrink-0 font-medium" style={{ color: alert ? meta.color : undefined }}>{g.label}</span>
      <span className={clsx('min-w-0 flex-1 truncate', alert ? 'text-fg' : 'text-dim')}>{g.headline}</span>
      {isActivePhase(g.phase) && (
        <span className="shrink-0 tnum text-faint">{formatClock(g.elapsedMs)}</span>
      )}
      <ChevronLeft size={11} className="shrink-0 rotate-180 text-faint" />
    </button>
  )
}
