import { useId } from 'react'
import { RUN_COMPARE_TEXT as T } from '../../../lib/terms'
import type { RunCompareTemplate } from '../../../types'
import { Block } from './parts'

const CHANGE_COLOR: Record<string, string> = { added: 'var(--accent)', removed: 'var(--text-dim)', config: 'var(--warn)' }

/**
 * 模板区：两次运行各用的版本，执行图变了的话列出变化的节点（新增、移除、配置变更）和改了的配置项。配置项写的是节点
 * 配置里的键：同一个键在不同类型的节点里叫法不同，接口又不给节点类型，照原样写比猜一个中文名可靠
 */
export function TemplateSection({ template }: { template: RunCompareTemplate }) {
  const id = useId()
  return (
    <Block id={`${id}-template`} title={T.template} attr={{ 'data-compare-template': '' }}>
      <p className="tnum text-xs text-dim" data-compare-versions="">
        {T.versions(T.version(template.base_version), T.version(template.target_version))}
      </p>
      {template.same_graph ? (
        <p className="mt-1.5 text-xs text-faint" data-compare-same-graph="">{T.sameGraph}</p>
      ) : !template.nodes.length ? (
        <p className="mt-1.5 text-xs text-faint">{T.nodesNone}</p>
      ) : (
        <ul className="mt-2 divide-y rounded border">
          {template.nodes.map((n) => {
            const color = CHANGE_COLOR[n.change] ?? 'var(--text-dim)'
            return (
              <li key={`${n.node_id}:${n.change}`} className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 px-2.5 py-1.5 text-xs"
                  data-compare-node={n.node_id} data-change={n.change}>
                <span className="min-w-0 font-medium [overflow-wrap:anywhere]" title={n.node_id}>{n.title || n.node_id}</span>
                <span className="chip shrink-0"
                      style={{ color, borderColor: `color-mix(in srgb, ${color} 40%, transparent)` }}>
                  {T.nodeChange[n.change] ?? n.change}
                </span>
                {!!n.keys.length && (
                  <span className="flex min-w-0 flex-wrap items-center gap-1 text-2xs text-faint">
                    <span>{T.keys}</span>
                    {n.keys.map((k) => (
                      <code key={k} className="mono rounded bg-bg px-1 text-dim [overflow-wrap:anywhere]" data-compare-key="">{k}</code>
                    ))}
                  </span>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </Block>
  )
}

/** 服务端给的说明（截断、缺快照这类会影响读数的事），照原样列出；没有就不画这一节 */
export function NotesSection({ notes }: { notes: string[] }) {
  const id = useId()
  if (!notes.length) return null
  return (
    <Block id={`${id}-notes`} title={T.notes} attr={{ 'data-compare-notes': '' }}>
      <ul className="list-disc space-y-0.5 pl-4 text-xs leading-relaxed text-dim [overflow-wrap:anywhere]">
        {notes.map((n, i) => <li key={i}>{n}</li>)}
      </ul>
    </Block>
  )
}
