/**
 * 布局组件：page、section、columns、tabs、divider（组件目录 v1 §3.5）。只摆放子组件，不碰数据。
 */
import { useId, useState } from 'react'
import { Tabs, TabPanel } from '../../components/ui'
import { str, useView, type NodeProps } from '../context'

export function PageBlock({ node }: NodeProps) {
  const { render } = useView()
  const title = str(node.props.title)
  return (
    <section className="space-y-3" data-component="page">
      {title && <h2 className="text-sm font-semibold">{title}</h2>}
      {render(node.children)}
    </section>
  )
}

export function SectionBlock({ node }: NodeProps) {
  const { render } = useView()
  const title = str(node.props.title)
  const description = str(node.props.description)
  return (
    <section className="space-y-2.5 rounded-lg border bg-panel p-3" data-component="section">
      {(title || description) && (
        <header>
          {title && <h3 className="text-xs font-semibold text-fg">{title}</h3>}
          {description && <p className="mt-0.5 text-2xs leading-relaxed text-faint">{description}</p>}
        </header>
      )}
      {render(node.children)}
    </section>
  )
}

// Tailwind 只认写死的类名：几栏各写一条。窄屏一律单栏
const COLS: Record<number, string> = {
  1: 'grid-cols-1',
  2: 'grid-cols-1 sm:grid-cols-2',
  3: 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-3',
  4: 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-4',
}

export function ColumnsBlock({ node }: NodeProps) {
  const { render } = useView()
  const children = node.children ?? []
  const count = Math.min(4, Math.max(1, Number(node.props.count) || children.length || 1))
  return (
    <div className={`grid gap-3 ${COLS[count]}`} data-component="columns">
      {children.map((c) => <div key={c.id} className="min-w-0">{render([c])}</div>)}
    </div>
  )
}

/** 页签：第 i 个标签对应第 i 个子组件；标签少了用子组件的 id 补 */
export function TabsBlock({ node }: NodeProps) {
  const { render } = useView()
  const children = node.children ?? []
  const labels: unknown[] = Array.isArray(node.props.labels) ? node.props.labels : []
  const tabs = children.map((c, i) => ({ key: c.id, label: str(labels[i]) || c.id }))
  const [active, setActive] = useState(tabs[0]?.key ?? '')
  const prefix = `tabs-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`
  const current = tabs.some((t) => t.key === active) ? active : tabs[0]?.key ?? ''
  return (
    <div className="rounded-lg border bg-panel" data-component="tabs">
      <Tabs tabs={tabs} active={current} onChange={setActive} idPrefix={prefix} />
      {children.filter((c) => c.id === current).map((c) => (
        <TabPanel key={c.id} idPrefix={prefix} tabKey={c.id} className="p-3">{render([c])}</TabPanel>
      ))}
    </div>
  )
}

export function DividerBlock() {
  return <hr className="border-hairline" data-component="divider" />
}
