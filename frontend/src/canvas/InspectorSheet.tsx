import { useEffect } from 'react'
import { ChevronLeft, Hand } from 'lucide-react'
import clsx from 'clsx'
import { useCatalog } from '../store/catalog'
import { useStudio } from '../store/studio'
import { decodeRun, type Step } from '../run/decode'
import { Spinner } from '../components/ui'
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
      if (e.key !== 'Escape') return
      // 正在输入框里打字时 Esc 是给输入法用的，别抢
      const el = document.activeElement
      const typing = el instanceof HTMLElement
        && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)
      if (typing) return
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
 */
function LiveStrip() {
  const streaming = useStudio((s) => s.streaming)
  const events = useStudio((s) => s.events)
  const select = useStudio((s) => s.select)
  const approvals = useCatalog((s) => s.approvals)
  const run = useStudio((s) => s.run)
  const waiting = approvals.some((a) => a.run_id === run?.id && a.status === 'pending')

  if (!streaming && !waiting) return null

  // 只取最后一步说给人听，不在这里重演整条流——这是一条提示，不是第二个时间线
  const steps = decodeRun(events)
  const last = flattenLast(steps)

  return (
    <button
      className="fade-up flex w-full shrink-0 items-center gap-2 border-t px-2.5 py-1.5 text-left text-[10.5px] transition-colors hover:bg-hover"
      style={waiting ? { background: 'color-mix(in srgb, var(--warn) 10%, transparent)' } : undefined}
      title="回到助手看完整过程"
      onClick={() => select(null)}
    >
      {waiting
        ? <Hand size={11} className="shrink-0" style={{ color: 'var(--warn)' }} />
        : <Spinner size={10} />}
      <span className={clsx('min-w-0 flex-1 truncate', waiting && 'text-[var(--warn)]')}>
        {waiting ? '等你确认' : last?.title || '正在执行…'}
      </span>
      <ChevronLeft size={11} className="shrink-0 rotate-180 text-faint" />
    </button>
  )
}

/** 最后一条有意义的步骤：优先取最深的子步骤，那才是"此刻在干什么"。 */
function flattenLast(steps: Step[]): Step | undefined {
  const flat: Step[] = []
  const walk = (list: Step[]) => list.forEach((s) => { flat.push(s); if (s.children) walk(s.children) })
  walk(steps)
  return [...flat].reverse().find((s) => s.kind !== 'lifecycle') ?? flat[flat.length - 1]
}
