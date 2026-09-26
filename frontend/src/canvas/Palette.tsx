import { useState } from 'react'
import { createPortal } from 'react-dom'
import { PanelLeftClose, PanelLeftOpen } from 'lucide-react'
import clsx from 'clsx'
import { NODE_CATEGORIES, NODE_DEFS, type NodeDef } from './nodeDefs'
import { hintOf } from './shortcuts'
import { useStudio } from '../store/studio'
import { IconButton } from '../components/ui'
import type { NodeType } from '../types'

/** 节点库搜索框的 id：「/」要把焦点送过来 */
export const PALETTE_SEARCH_ID = 'studio-palette-search'

const startDrag = (e: React.DragEvent, def: NodeDef) => {
  e.dataTransfer.setData('application/agentlab-node', def.type)
  e.dataTransfer.effectAllowed = 'move'
}

/**
 * 左侧节点库。拖到画布，或点一下放在视野中间的空地上（新节点自动选中）。
 *
 * 可以收成 48px 的图标轨：画布才是主角。1024 宽时三栏固定占掉 624px，画布只剩约
 * 400px，一张八个节点的图被缩成一条线。收起后悬停图标浮出名字和说明，拖拽照常。
 */
export function Palette({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const [query, setQuery] = useState('')
  const addNode = useStudio((s) => s.addNode)
  const locked = useStudio((s) => s.copilot.active)

  if (collapsed) return <Rail onToggle={onToggle} onAdd={(t) => addNode(t)} locked={locked} />

  const q = query.trim().toLowerCase()
  const defs = Object.values(NODE_DEFS).filter(
    (d) => !q || d.label.toLowerCase().includes(q) || d.description.includes(q) || d.type.includes(q),
  )

  return (
    <div className="flex h-full flex-col" aria-label="节点库">
      <div className="flex items-center gap-1 border-b p-2">
        <input
          id={PALETTE_SEARCH_ID}
          className="field"
          placeholder="搜索节点…（/）"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            // 回车直接加第一个：搜索 → 添加一气呵成，不用再去点
            if (e.key === 'Enter' && defs[0] && !locked && !e.nativeEvent.isComposing) {
              e.preventDefault()
              addNode(defs[0].type as NodeType)
            } else if (e.key === 'Escape') {
              setQuery('')
              ;(e.target as HTMLInputElement).blur()
            }
          }}
        />
        <IconButton label="收起节点库" title={hintOf('收起节点库', 'palette')} onClick={onToggle}
                    icon={<PanelLeftClose size={13} />} />
      </div>
      <div className={clsx('flex-1 overflow-y-auto p-2', locked && 'pointer-events-none opacity-50')}
           aria-disabled={locked || undefined}>
        {NODE_CATEGORIES.map((category) => {
          const items = defs.filter((d) => d.category === category)
          if (!items.length) return null
          return (
            <div key={category} className="mb-3">
              <div className="mb-1.5 px-1 text-2xs font-semibold tracking-wider text-faint">
                {category}
              </div>
              <div className="space-y-1">
                {items.map((def, i) => (
                  <button
                    type="button"
                    key={def.type}
                    draggable={!locked}
                    onDragStart={(e) => startDrag(e, def)}
                    onClick={() => addNode(def.type as NodeType)}
                    className={clsx(
                      `nt-${def.type} group flex w-full cursor-grab items-start gap-2 rounded-md border border-transparent px-2 py-1.5 text-left hover:border-[var(--border)] hover:bg-hover active:cursor-grabbing`,
                      q && category === defs[0]?.category && i === 0 && 'border-[var(--border)] bg-hover',
                    )}
                    title={`${def.description}\n点一下放在视野中间，或拖到画布上`}
                  >
                    <TypeIcon def={def} />
                    <div className="min-w-0">
                      <div className="text-xs font-medium leading-tight">{def.label}</div>
                      <div className="truncate text-2xs leading-tight text-faint">
                        {def.description}
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            </div>
          )
        })}
        {!defs.length && (
          <div className="px-2 py-6 text-center text-2xs text-faint">没有匹配的节点</div>
        )}
      </div>
      <div className="border-t px-3 py-2 text-2xs leading-relaxed text-faint">
        拖到画布，或点击放在视野中间。<br />
        从节点右侧圆点拖到另一个节点左侧即可连线。
      </div>
    </div>
  )
}

function TypeIcon({ def, size = 20 }: { def: NodeDef; size?: number }) {
  return (
    <div
      className="mt-0.5 flex shrink-0 items-center justify-center rounded"
      style={{
        width: size, height: size,
        background: 'color-mix(in srgb, var(--nt) 18%, transparent)',
        color: 'var(--nt)',
      }}
    >
      <def.icon size={Math.round(size * 0.6)} />
    </div>
  )
}

/**
 * 收起后的图标轨。说明不用原生 title：原生提示要停一秒多才出来，而且只能一行灰字；
 * 这里悬停即浮出名字和一句话说明。浮层挂到 body 上，不被这一栏的滚动裁掉
 */
function Rail({ onToggle, onAdd, locked }: {
  onToggle: () => void; onAdd: (t: NodeType) => void; locked: boolean
}) {
  const [tip, setTip] = useState<{ def: NodeDef; top: number; left: number } | null>(null)
  const show = (def: NodeDef, el: HTMLElement) => {
    const r = el.getBoundingClientRect()
    setTip({ def, top: r.top + r.height / 2, left: r.right + 8 })
  }
  return (
    <div className="flex h-full flex-col items-center" aria-label="节点库（已收起）">
      <div className="flex w-full justify-center border-b py-2">
        <IconButton label="展开节点库" title={hintOf('展开节点库', 'palette')} onClick={onToggle}
                    icon={<PanelLeftOpen size={13} />} />
      </div>
      <div className={clsx('flex w-full flex-1 flex-col items-center gap-0.5 overflow-y-auto py-1.5',
        locked && 'pointer-events-none opacity-50')}
           onMouseLeave={() => setTip(null)}>
        {NODE_CATEGORIES.map((category, ci) => (
          <div key={category} className="flex w-full flex-col items-center gap-0.5">
            {ci > 0 && <span className="my-1 h-px w-5" style={{ background: 'var(--border)' }} aria-hidden />}
            {Object.values(NODE_DEFS).filter((d) => d.category === category).map((def) => (
              <button
                type="button"
                key={def.type}
                aria-label={`添加${def.label}`}
                draggable={!locked}
                onDragStart={(e) => { setTip(null); startDrag(e, def) }}
                onClick={() => onAdd(def.type as NodeType)}
                onMouseEnter={(e) => show(def, e.currentTarget)}
                onFocus={(e) => show(def, e.currentTarget)}
                onBlur={() => setTip(null)}
                className={`nt-${def.type} flex h-8 w-8 cursor-grab items-center justify-center rounded-md hover:bg-hover active:cursor-grabbing`}
              >
                <TypeIcon def={def} size={22} />
              </button>
            ))}
          </div>
        ))}
      </div>
      {tip && createPortal(
        // 居中和入场分两层：fade-up 的关键帧也写 transform，放在同一层上会在动画期间
        // 顶掉 translateY(-50%)，浮层先落低半个身位、动画一完再跳上去
        <div role="tooltip" className="pointer-events-none fixed z-50 w-52"
             style={{ top: tip.top, left: tip.left, transform: 'translateY(-50%)' }}>
          <div className="fade-up rounded-md border bg-panel px-2.5 py-2 shadow-elev-2">
            <div className="text-xs font-medium">{tip.def.label}</div>
            <div className="mt-0.5 text-2xs leading-snug text-faint">{tip.def.description}</div>
            <div className="mt-1 text-2xs text-faint">点一下放在视野中间 · 或拖到画布上</div>
          </div>
        </div>,
        document.body,
      )}
    </div>
  )
}
