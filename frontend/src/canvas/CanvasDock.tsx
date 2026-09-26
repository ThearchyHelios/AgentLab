import { useState } from 'react'
import { AlertTriangle, ListChecks, Variable as VarIcon, X, XCircle } from 'lucide-react'
import clsx from 'clsx'
import { ProblemsPane } from './ProblemsPanel'
import { VariablesPane, VarIssueChips } from './VariablesDrawer'
import type { Problem } from './issues'
import { hintOf } from './shortcuts'
import { useStudio } from '../store/studio'
import { IconButton } from '../components/ui'

export type DockTab = 'problems' | 'variables'

/**
 * 画布底部的停靠栏：「问题」和「变量」两页，同一个位置。
 *
 * 做成挤压式而不是浮层：React Flow 的 Controls 和 MiniMap 是绝对定位在画布容器里
 * 的，浮层会把它俩埋掉；挤压会把它俩顶上去。代价是画布底部被裁掉一截，所以要限高，
 * 并且保证画布至少剩一截——高度归零时 React Flow 会直接报错 004。
 */
const MIN_CANVAS = 200
const DEFAULT_HEIGHT = 240
const HEIGHT_KEY = 'agentlab.studio.dockHeight'

const readHeight = () => {
  try {
    const v = Number(localStorage.getItem(HEIGHT_KEY))
    return Number.isFinite(v) && v >= 120 ? v : DEFAULT_HEIGHT
  } catch {
    return DEFAULT_HEIGHT
  }
}

export function CanvasDock({ tab, onTab, onClose, problems, activeProblem, onLocate }: {
  tab: DockTab
  onTab: (t: DockTab) => void
  onClose: () => void
  problems: Problem[]
  activeProblem: string | null
  onLocate: (p: Problem) => void
}) {
  const [height, setHeight] = useState(readHeight)
  const variables = useStudio((s) => s.variables)
  const varIssues = useStudio((s) => s.varIssues)
  const onEdgesChange = useStudio((s) => s.onEdgesChange)
  const errors = problems.filter((p) => p.level === 'error').length
  const warns = problems.length - errors

  return (
    <section
      className="relative z-10 flex shrink-0 flex-col border-t bg-panel"
      style={{ height, maxHeight: `max(${MIN_CANVAS}px, 60vh)` }}
      aria-label={tab === 'problems' ? '问题' : '变量'}
    >
      <ResizeHandle height={height} onResize={setHeight} />

      <div className="flex shrink-0 items-center gap-1 border-b px-2 py-1" role="tablist" aria-label="画布停靠栏">
        <DockTabButton active={tab === 'problems'} onClick={() => onTab('problems')}
                       title={hintOf('问题', 'problems')} controls="dock-problems">
          <ListChecks size={12} /> 问题
          {errors > 0 && (
            <span className="tnum flex items-center gap-0.5" style={{ color: 'var(--err)' }}>
              <XCircle size={10} />{errors}
            </span>
          )}
          {warns > 0 && (
            <span className="tnum flex items-center gap-0.5" style={{ color: 'var(--warn)' }}>
              <AlertTriangle size={10} />{warns}
            </span>
          )}
        </DockTabButton>
        <DockTabButton active={tab === 'variables'} onClick={() => onTab('variables')}
                       title={hintOf('变量', 'variables')} controls="dock-variables">
          <VarIcon size={12} /> 变量
          <span className="tnum text-faint">{variables.length}</span>
        </DockTabButton>
        {tab === 'variables' && <span className="ml-1 flex items-center gap-1"><VarIssueChips issues={varIssues} /></span>}
        <span className="flex-1" />
        <IconButton label="收起" title={hintOf('收起', tab === 'problems' ? 'problems' : 'variables')}
                    onClick={onClose} icon={<X size={12} />} />
      </div>

      <div id={tab === 'problems' ? 'dock-problems' : 'dock-variables'} role="tabpanel"
           className="flex min-h-0 flex-1 flex-col">
        {tab === 'problems'
          ? <ProblemsPane problems={problems} activeId={activeProblem} onLocate={onLocate}
                          onDeleteEdge={(id) => onEdgesChange([{ type: 'remove', id }])} />
          : <VariablesPane />}
      </div>
    </section>
  )
}

function DockTabButton({ active, onClick, title, controls, children }: {
  active: boolean; onClick: () => void; title: string; controls: string; children: React.ReactNode
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      aria-controls={controls}
      title={title}
      onClick={onClick}
      className={clsx(
        'relative flex items-center gap-1.5 rounded-md px-2 py-1 text-2xs font-medium transition-colors',
        active ? 'bg-hover text-fg' : 'text-faint hover:bg-hover hover:text-dim',
      )}
    >
      {children}
    </button>
  )
}

/** 拖拽改高。上下都要夹住：高度归零会让 React Flow 直接报错 004。 */
function ResizeHandle({ height, onResize }: {
  height: number; onResize: (h: number) => void
}) {
  return (
    <div
      className="absolute inset-x-0 -top-1 z-10 h-2 cursor-row-resize"
      role="separator"
      aria-orientation="horizontal"
      aria-label="拖动调整高度"
      onPointerDown={(e) => {
        e.preventDefault()
        const startY = e.clientY
        const startH = height
        const max = Math.max(MIN_CANVAS, window.innerHeight * 0.6)
        let last = startH
        const move = (ev: PointerEvent) => {
          last = Math.min(max, Math.max(120, startH - (ev.clientY - startY)))
          onResize(last)
        }
        const up = () => {
          window.removeEventListener('pointermove', move)
          window.removeEventListener('pointerup', up)
          try { localStorage.setItem(HEIGHT_KEY, String(Math.round(last))) } catch { /* 存不下就下次用默认高度 */ }
        }
        window.addEventListener('pointermove', move)
        window.addEventListener('pointerup', up)
      }}
    />
  )
}
