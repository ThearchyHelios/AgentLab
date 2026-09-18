import { useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle, ChevronDown, Crosshair, Database, Hash, Info, Package, Variable as VarIcon, X,
} from 'lucide-react'
import clsx from 'clsx'
import { useStudio } from '../store/studio'
import type { Variable, VarIssue } from '../types'

/**
 * 画布底部的变量抽屉。
 *
 * 这套编排靠 `{{ }}` 串数据流，但"这张图里到底有哪些变量"此前在界面上
 * 根本无处可查——只能靠翻每个节点的 assign_to 自己拼。拼错一个名字还不会
 * 报错（模板取不到值渲染成空字符串），于是排查只能靠猜。
 *
 * 做成挤压式而不是浮层：React Flow 的 Controls 和 MiniMap 是绝对定位在
 * 画布容器里的，浮层会把它俩埋掉；挤压会把它俩顶上去。代价是画布底部被
 * 裁掉一截（React Flow v12 的 ResizeObserver 只更新尺寸，不会自动重新取景），
 * 所以要限高，并且保证画布至少剩一截——高度归零时 React Flow 会直接报错。
 */
const MIN_CANVAS = 200
const DEFAULT_HEIGHT = 260

export function VariablesDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const variables = useStudio((s) => s.variables)
  const varIssues = useStudio((s) => s.varIssues)
  const analyzeNow = useStudio((s) => s.analyzeNow)
  const [picked, setPicked] = useState<string | null>(null)
  const [height, setHeight] = useState(DEFAULT_HEIGHT)

  // 打开时立刻算一次：变量表是按结构签名跳过的，可能已经很久没刷新了
  useEffect(() => { if (open) void analyzeNow() }, [open, analyzeNow])

  if (!open) return null

  const selected = variables.find((v) => v.path === picked) ?? null

  return (
    <div
      className="relative z-10 flex shrink-0 flex-col border-t bg-panel"
      style={{ height, maxHeight: `max(${MIN_CANVAS}px, 60vh)` }}
    >
      <ResizeHandle height={height} onResize={setHeight} />

      <div className="flex shrink-0 items-center gap-2 border-b px-3 py-1.5">
        <VarIcon size={12} style={{ color: 'var(--accent)' }} />
        <span className="text-[11.5px] font-semibold">变量</span>
        <span className="text-[10px] text-faint">{variables.length} 个</span>
        <IssueChips issues={varIssues} />
        <span className="flex-1" />
        <button className="rounded p-1 text-faint transition-colors hover:bg-hover"
                title="收起（⌥V）" onClick={onClose}>
          <X size={12} />
        </button>
      </div>

      <div className="flex min-h-0 flex-1">
        <div className="min-w-0 flex-1 overflow-y-auto">
          <VarTable variables={variables} picked={picked} onPick={setPicked} />
        </div>
        {selected && (
          <div className="w-[300px] shrink-0 overflow-y-auto border-l">
            <VarDetail variable={selected} onClose={() => setPicked(null)} />
          </div>
        )}
      </div>
    </div>
  )
}

/** 拖拽改高。上下都要夹住：高度归零会让 React Flow 直接报错 004。 */
function ResizeHandle({ height, onResize }: {
  height: number; onResize: (h: number) => void
}) {
  return (
    <div
      className="absolute inset-x-0 -top-1 z-10 h-2 cursor-row-resize"
      onPointerDown={(e) => {
        e.preventDefault()
        const startY = e.clientY
        const startH = height
        const max = Math.max(MIN_CANVAS, window.innerHeight * 0.6)
        const move = (ev: PointerEvent) => {
          onResize(Math.min(max, Math.max(120, startH - (ev.clientY - startY))))
        }
        const up = () => {
          window.removeEventListener('pointermove', move)
          window.removeEventListener('pointerup', up)
        }
        window.addEventListener('pointermove', move)
        window.addEventListener('pointerup', up)
      }}
    />
  )
}

function IssueChips({ issues }: { issues: VarIssue[] }) {
  const errors = issues.filter((i) => i.level === 'error').length
  const warns = issues.filter((i) => i.level === 'warning').length
  const infos = issues.filter((i) => i.level === 'info').length
  return (
    <>
      {errors > 0 && (
        <span className="chip" style={{ color: 'var(--err)', borderColor: 'var(--err)' }}>
          <AlertTriangle size={9} /> {errors} 个引用不到
        </span>
      )}
      {warns > 0 && (
        <span className="chip" style={{ color: 'var(--warn)' }}>{warns} 个取值时机不对</span>
      )}
      {infos > 0 && <span className="chip text-faint">{infos} 个没人用</span>}
    </>
  )
}

const KIND_META: Record<string, { icon: typeof Hash; text: string }> = {
  input: { icon: Database, text: '入口输入' },
  var: { icon: VarIcon, text: '节点写入' },
  node: { icon: Package, text: '节点输出' },
  builtin: { icon: Hash, text: '内置' },
}

function VarTable({ variables, picked, onPick }: {
  variables: Variable[]; picked: string | null; onPick: (p: string | null) => void
}) {
  const runtime = useRuntimeValues()

  if (!variables.length) {
    return (
      <div className="flex h-full items-center justify-center text-[11px] text-faint">
        画布上还没有节点
      </div>
    )
  }

  return (
    <table className="w-full text-[11px]">
      <thead className="sticky top-0 bg-panel">
        <tr className="border-b text-left text-[10px] text-faint">
          <th className="px-3 py-1.5 font-medium">变量</th>
          <th className="px-2 py-1.5 font-medium">来自</th>
          <th className="px-2 py-1.5 font-medium">被引用</th>
          <th className="px-2 py-1.5 font-medium">最近一次运行的值</th>
        </tr>
      </thead>
      <tbody>
        {variables.map((v) => {
          const meta = KIND_META[v.kind] ?? KIND_META.builtin
          const value = runtime[v.path]
          return (
            <tr
              key={v.path}
              className={clsx('cursor-pointer border-b last:border-0 hover:bg-hover',
                picked === v.path && 'bg-hover')}
              onClick={() => onPick(picked === v.path ? null : v.path)}
            >
              <td className="px-3 py-1.5">
                <span className="mono" style={{ color: 'var(--accent)' }}>
                  {`{{ ${v.path} }}`}
                </span>
              </td>
              <td className="px-2 py-1.5 text-dim">
                <span className="flex items-center gap-1">
                  <meta.icon size={10} className="shrink-0 text-faint" />
                  <span className="truncate">{v.produced_by_label || meta.text}</span>
                </span>
              </td>
              <td className="px-2 py-1.5">
                {v.refs.length
                  ? <span className="text-dim">{v.refs.length} 处</span>
                  : <span className="text-faint">—</span>}
              </td>
              <td className="max-w-0 px-2 py-1.5">
                {value === undefined
                  ? <span className="text-faint">—</span>
                  : <span className="mono block truncate text-dim" title={value}>{value}</span>}
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function VarDetail({ variable, onClose }: { variable: Variable; onClose: () => void }) {
  const select = useStudio((s) => s.select)
  const runtime = useRuntimeValues()
  const value = runtime[variable.path]

  return (
    <div className="p-2.5">
      <div className="mb-1.5 flex items-start gap-2">
        <span className="mono min-w-0 flex-1 break-all text-[11.5px]"
              style={{ color: 'var(--accent)' }}>
          {`{{ ${variable.path} }}`}
        </span>
        <button className="rounded p-0.5 text-faint hover:bg-hover" onClick={onClose}>
          <ChevronDown size={12} className="-rotate-90" />
        </button>
      </div>
      <div className="mb-2 text-[10.5px] leading-relaxed text-faint">{variable.label}</div>
      {variable.description && (
        <div className="mb-2 rounded bg-bg px-2 py-1.5 text-[10.5px] leading-relaxed text-dim">
          {variable.description}
        </div>
      )}

      {variable.produced_by && (
        <Section title="谁产出">
          <button className="flex w-full items-center gap-1.5 rounded px-1 py-1 text-left text-[10.5px] text-dim hover:bg-hover"
                  onClick={() => select(variable.produced_by!)}>
            <Crosshair size={10} className="shrink-0 text-faint" />
            <span className="min-w-0 flex-1 truncate">{variable.produced_by_label}</span>
          </button>
        </Section>
      )}

      <Section title={`被引用 ${variable.refs.length} 处`}>
        {variable.refs.length ? variable.refs.map((r, i) => (
          <button key={i}
                  className="flex w-full items-center gap-1.5 rounded px-1 py-1 text-left text-[10.5px] hover:bg-hover"
                  onClick={() => select(r.node_id)}>
            <Crosshair size={10} className="shrink-0 text-faint" />
            <span className="min-w-0 flex-1 truncate text-dim">{r.node_label}</span>
            <span className="mono shrink-0 text-[9.5px] text-faint">{r.field}</span>
          </button>
        )) : (
          <div className="px-1 py-1 text-[10.5px] text-faint">
            没有任何地方引用它。改图改了一半的话这很正常
          </div>
        )}
      </Section>

      <Section title="最近一次运行的值">
        {value === undefined ? (
          <div className="px-1 text-[10.5px] leading-relaxed text-faint">
            这次运行里没有它的记录。跑一次就能看到
          </div>
        ) : (
          <pre className="mono max-h-40 overflow-auto whitespace-pre-wrap rounded bg-bg px-2 py-1.5 text-[10px] leading-relaxed text-dim">
            {value}
          </pre>
        )}
      </Section>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-2">
      <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wide text-faint">
        {title}
      </div>
      {children}
    </div>
  )
}

/**
 * 每个变量在最近一次运行里的实际取值。
 *
 * 从事件里读，不从别处猜：node.finished 现在带 vars（那一步往变量池写了
 * 什么）和 preview（那一步自己的输出）。以前 vars 不在任何事件里，只能从
 * preview 反推 data ?? text ?? output——那个推断大多数时候对，而一个
 * "大多数时候对"的调试工具比没有更糟。
 */
function useRuntimeValues(): Record<string, string> {
  const events = useStudio((s) => s.events)
  return useMemo(() => {
    const out: Record<string, string> = {}
    const show = (v: unknown) =>
      typeof v === 'string' ? v : JSON.stringify(v, null, 2) ?? String(v)
    for (const e of events) {
      if (e.type !== 'node.finished') continue
      const d: any = e.data ?? {}
      for (const [k, v] of Object.entries(d.vars ?? {})) {
        out[`vars.${k}`] = show(v)
        // 入口节点写进 vars 的那几个同时也是 input.*
        if (e.node_id && d.preview && typeof d.preview === 'object' && k in d.preview) {
          out[`input.${k}`] = show(v)
        }
      }
      if (e.node_id && d.preview !== undefined && d.preview !== null) {
        out[`nodes.${e.node_id}`] = show(d.preview)
      }
    }
    return out
  }, [events])
}
