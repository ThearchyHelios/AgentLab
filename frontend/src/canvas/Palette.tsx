import { useState } from 'react'
import { NODE_CATEGORIES, NODE_DEFS } from './nodeDefs'
import { useStudio } from '../store/studio'
import type { NodeType } from '../types'

/** 左侧节点库。拖到画布，或点一下放在视野中间。 */
export function Palette() {
  const [query, setQuery] = useState('')
  const addNode = useStudio((s) => s.addNode)

  const defs = Object.values(NODE_DEFS).filter(
    (d) =>
      !query ||
      d.label.includes(query) ||
      d.description.includes(query) ||
      d.type.includes(query.toLowerCase()),
  )

  return (
    <div className="flex h-full flex-col">
      <div className="border-b p-2">
        <input
          className="field"
          placeholder="搜索节点…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      <div className="flex-1 overflow-y-auto p-2">
        {NODE_CATEGORIES.map((category) => {
          const items = defs.filter((d) => d.category === category)
          if (!items.length) return null
          return (
            <div key={category} className="mb-3">
              <div className="mb-1.5 px-1 text-[10px] font-semibold uppercase tracking-wider text-faint">
                {category}
              </div>
              <div className="space-y-1">
                {items.map((def) => (
                  <div
                    key={def.type}
                    draggable
                    onDragStart={(e) => {
                      e.dataTransfer.setData('application/agentlab-node', def.type)
                      e.dataTransfer.effectAllowed = 'move'
                    }}
                    onClick={() =>
                      addNode(def.type as NodeType, {
                        x: 220 + Math.random() * 160,
                        y: 160 + Math.random() * 200,
                      })
                    }
                    className={`nt-${def.type} group flex cursor-grab items-start gap-2 rounded-md border border-transparent px-2 py-1.5 hover:border-[var(--border)] hover:bg-hover active:cursor-grabbing`}
                    title={def.description}
                  >
                    <div
                      className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded"
                      style={{
                        background: 'color-mix(in srgb, var(--nt) 18%, transparent)',
                        color: 'var(--nt)',
                      }}
                    >
                      <def.icon size={12} />
                    </div>
                    <div className="min-w-0">
                      <div className="text-[11.5px] font-medium leading-tight">{def.label}</div>
                      <div className="truncate text-[10px] leading-tight text-faint">
                        {def.description}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )
        })}
        {!defs.length && (
          <div className="px-2 py-6 text-center text-[11px] text-faint">没有匹配的节点</div>
        )}
      </div>
      <div className="border-t px-3 py-2 text-[10px] leading-relaxed text-faint">
        拖到画布，或点击直接添加。<br />
        从节点右侧圆点拖到另一个节点左侧即可连线。
      </div>
    </div>
  )
}
