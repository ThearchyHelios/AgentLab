import { useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle, ChevronDown, Crosshair, Database, Hash, Package, RotateCw, Variable as VarIcon,
} from 'lucide-react'
import clsx from 'clsx'
import { useStudio } from '../store/studio'
import { Spinner } from '../components/ui'
import type { Variable, VarIssue } from '../types'

/**
 * 变量：这张图里有哪些变量、谁产出、谁引用、最近一次运行的值。
 *
 * 这套编排靠 `{{ }}` 串数据流，但"这张图里到底有哪些变量"此前在界面上
 * 根本无处可查——只能靠翻每个节点的 assign_to 自己拼。拼错一个名字还不会
 * 报错（模板取不到值渲染成空字符串），于是排查只能靠猜。
 *
 * 悬停一行，画布上亮出它的血缘：谁产出、谁引用（store.lineage，由画布和卡片画出来）。
 * 排查「为什么这里取到空值」时，不用再在抽屉、检查器、画布三处之间来回对照。
 *
 * 它是画布底部停靠栏（CanvasDock）的一页，和「问题」同一个位置。
 */
export function VariablesPane() {
  const variables = useStudio((s) => s.variables)
  const analyzeNow = useStudio((s) => s.analyzeNow)
  const traceVariable = useStudio((s) => s.traceVariable)
  const [picked, setPicked] = useState<string | null>(null)

  // 打开时立刻算一次：变量表是按结构签名跳过的，可能已经很久没刷新了
  useEffect(() => { void analyzeNow() }, [analyzeNow])
  // 收起时把画布上的血缘高亮一起收掉
  useEffect(() => () => traceVariable(null), [traceVariable])

  const selected = variables.find((v) => v.path === picked) ?? null

  return (
    <div className="flex min-h-0 flex-1">
      <div className="min-w-0 flex-1 overflow-y-auto"
           // 离开表格时回到「选中的那个」：点开详情的变量一直亮着，悬停别的只是临时看一眼
           onMouseLeave={() => traceVariable(picked)}>
        <VarTable variables={variables} picked={picked}
                  onPick={(p) => { setPicked(p); traceVariable(p) }}
                  onHover={traceVariable} />
      </div>
      {selected && (
        <div className="w-[300px] shrink-0 overflow-y-auto border-l">
          <VarDetail variable={selected} onClose={() => { setPicked(null); traceVariable(null) }} />
        </div>
      )}
    </div>
  )
}

/** 停靠栏页签上的计数 */
export function VarIssueChips({ issues }: { issues: VarIssue[] }) {
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
  loop: { icon: VarIcon, text: '循环变量' },
  node: { icon: Package, text: '节点输出' },
  builtin: { icon: Hash, text: '内置' },
}

function VarTable({ variables, picked, onPick, onHover }: {
  variables: Variable[]; picked: string | null
  onPick: (p: string | null) => void; onHover: (p: string | null) => void
}) {
  const runtime = useRuntimeValues()
  const nodeCount = useStudio((s) => s.nodes.length)
  const analysis = useStudio((s) => s.analysis)
  const analysisError = useStudio((s) => s.analysisError)
  const analyzeNow = useStudio((s) => s.analyzeNow)

  if (!variables.length) {
    // 三种空各说各的：以前分析请求失败时也写「画布上还没有节点」，而画布上明明有 8 个
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center text-2xs text-faint">
        {!nodeCount ? '画布上还没有节点'
          : analysis === 'failed' ? (
            <>
              <span style={{ color: 'var(--warn)' }}>变量分析失败{analysisError ? `：${analysisError}` : ''}</span>
              <button type="button" className="btn btn-sm" onClick={() => void analyzeNow()}>
                <RotateCw size={11} /> 重试
              </button>
            </>
          )
          : <span className="flex items-center gap-1.5"><Spinner size={11} /> 正在分析变量…</span>}
      </div>
    )
  }

  return (
    <table className="w-full text-2xs">
      <thead className="sticky top-0 z-[1] bg-panel">
        <tr className="border-b text-left text-faint">
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
              data-var={v.path}
              tabIndex={0}
              aria-selected={picked === v.path}
              className={clsx('cursor-pointer border-b last:border-0 outline-none hover:bg-hover focus-visible:bg-hover',
                picked === v.path && 'bg-hover')}
              onClick={() => onPick(picked === v.path ? null : v.path)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onPick(picked === v.path ? null : v.path) }
              }}
              onMouseEnter={() => onHover(v.path)}
              onFocus={() => onHover(v.path)}
            >
              <td className="px-3 py-1.5">
                <span className="mono text-fg">{`{{ ${v.path} }}`}</span>
              </td>
              <td className="px-2 py-1.5 text-dim">
                <span className="flex items-center gap-1">
                  <meta.icon size={10} className="shrink-0 text-faint" />
                  <span className="truncate">{v.produced_by_label || meta.text}</span>
                </span>
              </td>
              <td className="px-2 py-1.5">
                {v.refs.length
                  ? <span className="tnum text-dim">{v.refs.length} 处</span>
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
  const focusNode = useStudio((s) => s.focusNode)
  const runtime = useRuntimeValues()
  const value = runtime[variable.path]
  // 以前只 select 不平移：节点在视野外时点了等于没点，画布上连选中环都不会移过去
  const go = (id: string) => { select(id); focusNode(id) }

  return (
    <div className="p-2.5">
      <div className="mb-1.5 flex items-start gap-2">
        <span className="mono min-w-0 flex-1 break-all text-xs text-fg">
          {`{{ ${variable.path} }}`}
        </span>
        <button type="button" className="rounded p-0.5 text-faint hover:bg-hover" onClick={onClose}
                aria-label="收起详情" title="收起详情">
          <ChevronDown size={12} className="-rotate-90" />
        </button>
      </div>
      <div className="mb-2 text-2xs leading-relaxed text-faint">{variable.label}</div>
      {variable.description && (
        <div className="mb-2 rounded bg-bg px-2 py-1.5 text-2xs leading-relaxed text-dim">
          {variable.description}
        </div>
      )}

      {variable.produced_by && (
        <Section title="谁产出">
          <button type="button" className="flex w-full items-center gap-1.5 rounded px-1 py-1 text-left text-2xs text-dim hover:bg-hover"
                  title="在画布上定位" onClick={() => go(variable.produced_by!)}>
            <Crosshair size={10} className="shrink-0 text-faint" />
            <span className="min-w-0 flex-1 truncate">{variable.produced_by_label}</span>
          </button>
        </Section>
      )}

      <Section title={`被引用 ${variable.refs.length} 处`}>
        {variable.refs.length ? variable.refs.map((r, i) => (
          <button type="button" key={i}
                  className="flex w-full items-center gap-1.5 rounded px-1 py-1 text-left text-2xs hover:bg-hover"
                  title="在画布上定位" onClick={() => go(r.node_id)}>
            <Crosshair size={10} className="shrink-0 text-faint" />
            <span className="min-w-0 flex-1 truncate text-dim">{r.node_label}</span>
            <span className="mono shrink-0 text-2xs text-faint">{r.field}</span>
          </button>
        )) : (
          <div className="px-1 py-1 text-2xs text-faint">
            没有任何地方引用它。改图改了一半的话这很正常
          </div>
        )}
      </Section>

      <Section title="最近一次运行的值">
        {value === undefined ? (
          <div className="px-1 text-2xs leading-relaxed text-faint">
            这次运行里没有它的记录。跑一次就能看到
          </div>
        ) : (
          <pre className="mono max-h-40 overflow-auto whitespace-pre-wrap rounded bg-bg px-2 py-1.5 text-2xs leading-relaxed text-dim">
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
      <div className="mb-0.5 text-2xs font-semibold text-faint">
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
