import { useEffect, useRef, useState, type ReactNode } from 'react'
import {
  Brain, ChevronRight, CircleCheck, CircleDot, Database,
  FileCode, GitBranch, Hand, Sparkles, Table2, Terminal, Wrench, XCircle,
} from 'lucide-react'
import clsx from 'clsx'
import { Spinner } from '../components/ui'
import {
  parseQueryResult, type ResultTable as Table, type Step, type StepKind,
} from './decode'
import { Markdown } from './Markdown'

/**
 * 助手流：一轮轮「你问什么 → 它做了什么 → 结果」。
 *
 * 纯展示，不碰 store。画布右栏（360px）和问数据页（宽屏）用的是同一个组件，
 * 差别只在 dense 这个开关上——而不是做成"一个组件三种停靠模式"：那要求同一个
 * 组件实例出现在三个不同的 DOM 父节点里，React 里除非用 portal 否则不成立，
 * 硬做就会变成跨路由 unmount/remount，输入框草稿和展开状态全丢。
 *
 * 步骤来自 decode.ts，这里只负责怎么画。两边读同一个解码结果，所以同一次运行
 * 在画布和问数据页讲的是同一个故事。
 */

export interface StreamTurn {
  id: string
  /** 用户说了什么。画布上的 Copilot 指令也走这个字段 */
  question?: string
  phase: 'running' | 'waiting' | 'done' | 'error'
  /** 一句话进度，顶在最前面 */
  status?: string
  steps: Step[]
  /** 还在流的思考（尚未成为 Step）。只有 Anthropic 系模型会有 */
  thinking?: string
  output?: Record<string, any> | null
  error?: string
  runId?: string
  /** formal / exploratory。出具横幅要据此标注"不进正式归档" */
  runClass?: string
  /** 这一轮建出来的图，可展开看、可放到画布 */
  graph?: any
  graphNote?: string
}

const ICONS: Record<StepKind, typeof Wrench> = {
  node: CircleDot, think: Brain, llm: Sparkles, query: Database, schema: Table2,
  tool: Wrench, code: Terminal, branch: GitBranch, human: Hand,
  issuance: FileCode, note: CircleDot, error: XCircle, lifecycle: CircleCheck,
}

export function AssistantStream({
  turns, dense = false, empty, approvalsFor, onOpenGraph, footer,
}: {
  turns: StreamTurn[]
  /** 窄栏模式：更紧的排版、表格少列 */
  dense?: boolean
  /** 没有任何一轮时显示什么 */
  empty?: ReactNode
  /** 这一轮要不要插审批卡——插槽而不是内建，因为不同页面接流的方式不同 */
  approvalsFor?: (turn: StreamTurn) => ReactNode
  onOpenGraph?: (graph: any) => void
  footer?: ReactNode
}) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const lastPhase = turns[turns.length - 1]?.phase
  const lastCount = turns[turns.length - 1]?.steps.length

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [turns.length, lastPhase, lastCount])

  if (!turns.length && empty) {
    return <div className="flex h-full flex-col">{empty}{footer}</div>
  }

  return (
    <div className="flex h-full flex-col">
      <div className={clsx('min-h-0 flex-1 overflow-y-auto', dense ? 'px-2.5 py-3' : 'px-4 py-4')}>
        <div className={clsx('space-y-4', !dense && 'mx-auto max-w-3xl')}>
          {turns.map((turn) => (
            <TurnCard key={turn.id} turn={turn} dense={dense}
                      approvals={approvalsFor?.(turn)} onOpenGraph={onOpenGraph} />
          ))}
        </div>
        <div ref={bottomRef} />
      </div>
      {footer}
    </div>
  )
}

function TurnCard({ turn, dense, approvals, onOpenGraph }: {
  turn: StreamTurn; dense: boolean; approvals?: ReactNode
  onOpenGraph?: (graph: any) => void
}) {
  const running = turn.phase === 'running'

  return (
    <div className="fade-up">
      {turn.question && (
        // 宽屏走聊天气泡（右对齐、实心），窄栏里气泡会挤成一条竖带，
        // 改用左侧强调条——同样能一眼看出"这句是你说的"，但不吃宽度
        <div className={clsx('mb-2 flex', dense ? 'justify-start' : 'justify-end')}>
          <div
            className={clsx(
              'leading-relaxed',
              dense
                ? 'w-full border-l-2 py-0.5 pl-2 text-[11.5px]'
                : 'max-w-[80%] rounded-lg rounded-br-sm px-3 py-1.5 text-[12.5px]',
            )}
            style={dense
              ? { borderColor: 'var(--accent)', color: 'var(--text)' }
              : { background: 'var(--accent)', color: '#fff' }}
          >
            {turn.question}
          </div>
        </div>
      )}

      <div className={clsx('rounded-lg border bg-panel', dense ? 'p-2.5' : 'p-3')}>
        <div className="flex items-center gap-2 text-[11.5px]">
          {running && <Spinner size={12} />}
          {turn.phase === 'waiting' && <Hand size={12} style={{ color: 'var(--warn)' }} />}
          {turn.phase === 'error' && <XCircle size={12} style={{ color: 'var(--err)' }} />}
          <span className={clsx('min-w-0 flex-1 truncate',
            turn.phase === 'error' && 'text-[var(--err)]')}>
            {turn.phase === 'error' ? turn.error : turn.status}
          </span>
          {turn.runId && (
            <span className="mono shrink-0 text-[10px] text-faint">#{turn.runId.slice(0, 6)}</span>
          )}
        </div>

        {/* 还在流的思考。这是等待期间唯一能看的东西——但只有 Anthropic 系
            模型会产出，所以它是增强，主干是下面的步骤 */}
        {running && turn.thinking && <ThinkingTail text={turn.thinking} />}

        {!!turn.steps.length && (
          <div className="mt-2">
            <StepList steps={turn.steps} dense={dense} />
          </div>
        )}

        {approvals}

        {turn.output && (
          <Output output={turn.output} dense={dense} runClass={turn.runClass} />
        )}

        {turn.graph && (
          <GraphPeek graph={turn.graph} note={turn.graphNote}
                     dense={dense} onOpen={onOpenGraph} />
        )}
      </div>
    </div>
  )
}

/** 思考的尾窗：只露最后几行并跟着滚，不然一大坨会把步骤挤到屏幕外。 */
function ThinkingTail({ text }: { text: string }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight
  }, [text])
  return (
    <div ref={ref}
         className="mt-2 max-h-20 overflow-y-auto whitespace-pre-wrap rounded bg-bg px-2 py-1.5 text-[10.5px] leading-relaxed text-faint">
      {text}
    </div>
  )
}

function StepList({ steps, dense, depth = 0 }: {
  steps: Step[]; dense: boolean; depth?: number
}) {
  return (
    <div className={clsx(depth > 0 && 'ml-3 border-l pl-2.5')}>
      {steps.map((step) => <StepRow key={step.id} step={step} dense={dense} depth={depth} />)}
    </div>
  )
}

function StepRow({ step, dense, depth }: { step: Step; dense: boolean; depth: number }) {
  const [open, setOpen] = useState(false)
  const Icon = ICONS[step.kind] ?? CircleDot
  const result = step.result
  const expandable = !!(step.detail || result)
  const table = result ? parseQueryResult(result) : null

  // 失败要压过 kind：一条 lifecycle 跑挂了还画成打勾的"完成"图标，
  // 是把失败说成了成功
  const failed = step.status === 'failed' || step.level === 'error'
  // "等你确认"不是"正在忙"。转圈说的是"你等着"，而这里正相反
  const waiting = step.status === 'waiting'
  const Glyph = failed ? XCircle : waiting ? Hand : Icon
  const color = failed ? 'var(--err)'
    : waiting ? 'var(--warn)'
    : step.level === 'warn' ? 'var(--warn)'
    : step.kind === 'think' ? 'var(--text-faint)'
    : step.status === 'running' ? 'var(--accent)'
    // 顶层节点是主干，子步骤是它的过程——靠明度分层，不然一屏全是同一个灰
    : depth === 0 && (step.kind === 'node' || step.kind === 'lifecycle') ? 'var(--text)'
    : 'var(--text-dim)'

  return (
    <div className="py-[3px]">
      <button
        className={clsx('flex w-full items-start gap-1.5 text-left',
          expandable && 'hover:opacity-80')}
        style={{ color }}
        onClick={() => expandable && setOpen((v) => !v)}
        disabled={!expandable}
      >
        <span className="mt-[2px] shrink-0">
          {step.status === 'running'
            ? <Spinner size={10} />
            : <Glyph size={11} />}
        </span>
        <span className={clsx('min-w-0 flex-1 leading-relaxed',
          dense ? 'text-[11px]' : 'text-[11.5px]',
          step.kind === 'think' && 'italic',
          // 一道光扫过文字。比转圈多说一件事——它在出东西，不只是在等
          step.status === 'running' && !step.children?.length && 'shimmer')}>
          {step.title}
        </span>
        {step.meta && (
          <span className="mono mt-[1px] shrink-0 text-[9.5px] text-faint">{step.meta}</span>
        )}
        {expandable && (
          <ChevronRight size={10} className="mt-[2px] shrink-0 opacity-50"
            style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }} />
        )}
      </button>

      {open && (
        <div className="mb-1 mt-1 space-y-1.5 pl-[18px]">
          {step.detail && (
            <pre className="mono max-h-40 overflow-auto whitespace-pre-wrap rounded bg-bg px-2 py-1.5 text-[10px] leading-relaxed text-dim">
              {step.detail}
            </pre>
          )}
          {table
            ? <ResultTable table={table} dense={dense} />
            : result && (
              <pre className="mono max-h-40 overflow-auto whitespace-pre-wrap rounded bg-bg px-2 py-1.5 text-[10px] leading-relaxed text-dim">
                {result.slice(0, 2000)}
              </pre>
            )}
        </div>
      )}

      {!!step.children?.length && (
        <StepList steps={step.children} dense={dense} depth={depth + 1} />
      )}
    </div>
  )
}

/** 查询结果画成表格。一屏 JSON 谁也看不出名堂，表格能一眼看到形状。 */
function ResultTable({ table, dense }: { table: Table; dense: boolean }) {
  const maxCols = dense ? 4 : 8
  const maxRows = 12
  const cols = table.columns.slice(0, maxCols)
  const hiddenCols = table.columns.length - cols.length
  const rows = table.rows.slice(0, maxRows)

  return (
    <div className="overflow-x-auto rounded border">
      <table className="w-full text-[10px]">
        <thead>
          <tr className="border-b bg-elev">
            {cols.map((c) => (
              <th key={c} className="mono px-1.5 py-1 text-left font-medium text-faint">{c}</th>
            ))}
            {hiddenCols > 0 && <th className="px-1.5 py-1 text-faint">+{hiddenCols}</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-b last:border-0">
              {row.slice(0, maxCols).map((cell, j) => (
                <td key={j} className="mono max-w-[140px] truncate px-1.5 py-1 text-dim"
                    title={cell == null ? '' : String(cell)}>
                  {cell == null ? <span className="opacity-40">null</span> : String(cell)}
                </td>
              ))}
              {hiddenCols > 0 && <td className="px-1.5 py-1 text-faint">…</td>}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="border-t px-1.5 py-1 text-[9.5px] leading-relaxed text-faint">
        {[
          rows.length < table.rows.length
            ? `显示前 ${rows.length} / ${table.rows.length} 行`
            : `${table.rows.length} 行`,
          hiddenCols > 0 ? `显示前 ${cols.length} / ${table.columns.length} 列` : null,
          // 两种"不全"性质不同：一个是 guard 主动限的，一个是存预览时切的。
          // 混成一句"已截断"，用户没法判断该去调查询还是去看完整工件。
          table.truncated ? '查询撞了行数上限，库里还有更多' : null,
          table.clipped ? '这里只是预览，实际取回的行数更多' : null,
        ].filter(Boolean).join(' · ')}
      </div>
    </div>
  )
}

function Output({ output, dense, runClass }: {
  output: Record<string, any>; dense: boolean; runClass?: string
}) {
  // _issuance 这类下划线开头的是内部字段，不是给人看的成果。
  // 空值也要滤掉：output 里留一个 {"result": ""} 很常见（图跑通了但出口
  // 没接上），它会渲染出一条什么都没有的分隔线——用户只会以为界面坏了
  const entries = Object.entries(output)
    .filter(([k, v]) => !k.startsWith('_') && !isBlank(v))
  const issuance = (output as any)._issuance
  if (!entries.length && !issuance) return null

  return (
    <div className="mt-2 space-y-2 border-t pt-2">
      {issuance && <IssuanceBanner issuance={issuance} runClass={runClass} />}
      {entries.map(([key, value]) => (
        <div key={key}>
          {entries.length > 1 && (
            <div className="mb-0.5 text-[10.5px] font-semibold text-faint">{key}</div>
          )}
          <OutputValue value={value} dense={dense} />
        </div>
      ))}
    </div>
  )
}

const TEXT_CAP = 3000

/**
 * 折叠长文本的切点。
 *
 * 不能按字符硬切：模型的报告几乎总是 Markdown，切点一旦落在表格中间，
 * 前面的行渲染成表格、最后半行留成原始的 `| 1 | ThearchyHelios | …`，
 * 看上去就是一份被咬掉一口的报告——而它其实是完整的，只是被折叠了。
 *
 * 所以只在行边界切；而且切点落进表格里时整块表格都不要，宁可少显示一段，
 * 也不要显示一张半截的表。
 */
function cutForPreview(text: string, cap: number): string {
  const head = text.slice(0, cap)
  const atLine = head.lastIndexOf('\n')
  let cut = atLine > cap * 0.5 ? head.slice(0, atLine) : head

  // 收尾的连续表格行（含分隔行）一起去掉——只剩表头的表格比没有更难读
  const lines = cut.split('\n')
  while (lines.length && /^\s*\|/.test(lines[lines.length - 1])) lines.pop()
  const withoutTable = lines.join('\n')
  // 整段都是表格时别把内容清空，那还不如原样折叠
  if (withoutTable.trim()) cut = withoutTable

  return cut.trimEnd()
}

function isBlank(v: unknown): boolean {
  if (v == null) return true
  if (typeof v === 'string') return !v.trim()
  if (Array.isArray(v)) return !v.length
  if (typeof v === 'object') return !Object.keys(v as object).length
  return false
}

/**
 * 一个成果值该怎么显示。
 *
 * 取数流程的最终产物十有八九就是结果集本身，而它在 output 里是一个
 * JSON 字符串（还常常是被再编码一层的）。直接当文本贴出来就是满屏
 * `\"attribute01\", \"attribute02\"`——技术上没错，但没人能从里面看出
 * "查到了什么"。能认出结果集就画成表格，认不出才退回文本。
 */
function OutputValue({ value, dense }: { value: unknown; dense: boolean }) {
  const [full, setFull] = useState(false)

  const table = typeof value === 'string'
    ? parseQueryResult(value)
    : (value && typeof value === 'object'
        && Array.isArray((value as any).columns) && Array.isArray((value as any).rows)
        ? { columns: (value as any).columns, rows: (value as any).rows,
            truncated: !!(value as any).truncated }
        : null)
  if (table) return <ResultTable table={table} dense={dense} />

  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2)
  // 不设上限的话，一次没走查询工具、直接把几千行塞进 output 的运行会
  // 把整条流卡住——列表本来就没做虚拟化
  const long = text.length > TEXT_CAP
  const shown = long && !full ? cutForPreview(text, TEXT_CAP) : text

  return (
    <div>
      {/* 模型几乎总是用 Markdown 组织回答。当纯文本显示的话，「1+1 等于 **2**」
          就原样带着星号——它的结构全丢了。只有字符串才渲染：JSON.stringify
          出来的东西按 Markdown 解会被 * 和 _ 搅乱 */}
      {typeof value === 'string'
        ? <Markdown text={shown} dense={dense} />
        : (
          <pre className={clsx('mono overflow-x-auto whitespace-pre-wrap leading-relaxed',
            dense ? 'text-[10.5px]' : 'text-[11px]')}>{shown}</pre>
        )}
      {long && (
        <div className="mt-1 flex items-center gap-2 text-[10.5px]">
          {!full && (
            // 光有一个"展开"按钮不够：用户看到的是一份读起来完整的报告，
            // 不会想到它下面还有。得先说"这里断了"
            <span className="text-faint">…后面还有，这里先折叠了</span>
          )}
          <button className="text-[var(--accent)] hover:underline"
                  onClick={() => setFull((v) => !v)}>
            {full ? '收起' : `展开全部（${text.length} 字）`}
          </button>
        </div>
      )}
    </div>
  )
}

const TIER_META: Record<string, { label: string; color: string; hint: string }> = {
  formal: { label: '正式出具', color: 'var(--ok)', hint: '指标齐全，叙述中所有数字均可回指口径卡' },
  degraded: { label: '降档出具', color: 'var(--warn)', hint: '存在缺口，结论请对照下方声明使用' },
  withheld: { label: '不予出具', color: 'var(--err)', hint: '必需指标缺失或数字无法溯源，本期结论不作数' },
}

/**
 * 出具档位。全站唯一一份——画布助手栏、问数据页、运行详情都用它。
 *
 * 数据取自 output._issuance 而不是 issuance 事件：事件里只有 tier /
 * missing_* / unmatched(计数)，而 output 里那份还带着 matched_numbers、
 * metrics_checked、calibers、unmatched_numbers[].token。拿事件当数据源，
 * 横幅会静默退化成"核对 0 个指标"，比不显示更糟。
 */
export function IssuanceBanner({ issuance, runClass }: {
  issuance: any; runClass?: string
}) {
  const tier = String(issuance?.tier ?? '')
  if (!tier) return null
  const meta = TIER_META[tier] ?? { label: tier, color: 'var(--warn)', hint: '' }
  const unmatched: any[] = issuance?.unmatched_numbers ?? []

  return (
    <div className="rounded-lg border p-2" style={{ borderColor: meta.color }}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="text-[11.5px] font-semibold" style={{ color: meta.color }}>
          ⚖ {meta.label}
        </span>
        {runClass === 'exploratory' && (
          <span className="chip" style={{ color: 'var(--warn)' }}>探索性 · 不进正式归档</span>
        )}
        <span className="ml-auto text-[10px] text-faint">
          回指 {issuance.matched_numbers ?? 0} 个数字 / 核对 {issuance.metrics_checked ?? 0} 个指标
        </span>
      </div>
      {meta.hint && <div className="mt-1 text-[10.5px] leading-relaxed text-faint">{meta.hint}</div>}
      {(issuance.calibers ?? []).map((c: any) => (
        <div key={c.node ?? c.caliber} className="mt-1 text-[10.5px] text-dim">
          口径：{c.caliber} @ {c.version}
        </div>
      ))}
      {!!issuance.missing_required?.length && (
        <div className="mt-1 text-[10.5px]" style={{ color: 'var(--err)' }}>
          缺必需指标：{issuance.missing_required.join('、')}
        </div>
      )}
      {!!issuance.missing_expected?.length && (
        <div className="mt-1 text-[10.5px]" style={{ color: 'var(--warn)' }}>
          缺数据声明：{issuance.missing_expected.join('、')} 本期缺失
        </div>
      )}
      {!!unmatched.length && (
        <div className="mt-1 text-[10.5px]" style={{ color: 'var(--warn)' }}>
          无法回指的数字：{unmatched.map((u: any) => u.token ?? u).join('、')}
        </div>
      )}
    </div>
  )
}

function GraphPeek({ graph, note, dense, onOpen }: {
  graph: any; note?: string; dense: boolean; onOpen?: (g: any) => void
}) {
  const [open, setOpen] = useState(false)
  const nodes = graph?.nodes ?? []

  return (
    <div className="mt-2 border-t pt-2">
      <div className="flex items-center gap-2">
        <button className="flex items-center gap-1 text-[10.5px] text-faint hover:text-dim"
                onClick={() => setOpen((v) => !v)}>
          <ChevronRight size={10}
            style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }} />
          它是怎么做的（{nodes.length} 步）
        </button>
        <span className="flex-1" />
        {onOpen && (
          <button className="btn btn-sm btn-ghost text-[10.5px]"
                  title="把这张图放到画布上继续改" onClick={() => onOpen(graph)}>
            在画布里打开
          </button>
        )}
      </div>
      {open && (
        <div className="mt-1.5 space-y-1">
          {note && (
            <div className={clsx('rounded bg-bg px-2 py-1.5 leading-relaxed text-dim',
              dense ? 'text-[10.5px]' : 'text-[11px]')}>
              {note}
            </div>
          )}
          {nodes.map((n: any, i: number) => (
            <div key={n.id ?? i} className="flex items-center gap-2 text-[10.5px] text-faint">
              <span className="mono w-4 shrink-0 text-right">{i + 1}</span>
              <span className="chip shrink-0">{n.type}</span>
              <span className="truncate">{n.data?.label || n.id}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/** 供各页面复用的空态骨架。 */
export function StreamEmpty({ icon, title, hint, children }: {
  icon: ReactNode; title: string; hint?: string; children?: ReactNode
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col items-center justify-center px-6 text-center">
      <div className="mb-2 text-faint">{icon}</div>
      <div className="text-[13px] font-medium">{title}</div>
      {hint && <div className="mt-1 max-w-sm text-[11.5px] leading-relaxed text-faint">{hint}</div>}
      {children && <div className="mt-3 w-full max-w-md">{children}</div>}
    </div>
  )
}
