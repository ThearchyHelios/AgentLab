import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import clsx from 'clsx'
import type { Edge } from '@xyflow/react'
import { isComposing } from '../components/ui'
import { formatShortcut } from '../lib/keys'
import { useStudio } from '../store/studio'
import type { Variable, VarIssue } from '../types'

/**
 * 会写变量引用的输入框：补全、高亮、悬停看血缘。两种语法：
 *
 * - template：`{{ vars.x }}`，打 `{{` 弹出可用变量。模板取不到值时渲染成空字符串、
 *   不报错，所以拼错一个变量名在运行时是完全静默的——补全和高亮是从源头上消灭这类错误。
 * - expression：裸路径 `vars.x == 1`，打标识符就补全，插入的也是裸路径。这里敲 `{{`
 *   就地提示「表达式不需要 {{ }}」——后端专门有一条告警说的就是这个。
 *
 * **挂哪种由 nodeDefs 的 syntax 决定，不在这里猜。** 有几个字段长得像模板、走的却是
 * 表达式求值（skip_if、loop.condition、cases[].condition…），在那儿挂 `{{` 触发器等于
 * 手把手教用户写错。
 *
 * 补全只给「这一步能取到」的：上游节点的产出、入口输入、内置变量。本节点自己和下游
 * 的产出、并行支路上的产出，置灰列在后面并写明为什么取不到，不能插入。
 *
 * 高亮是一层和输入框同字体、同内边距的镜像，垫在透明的输入框下面：每个引用画成
 * 一枚底色胶囊；引用不到的画红色波浪线，这里还取不到的画琥珀色。纯前端，值变了就重算。
 */
export function TemplateText({
  value, onChange, rows, className, placeholder, multiline = true, spellCheck,
  syntax = 'template', nodeId, invalid, id, describedBy,
}: {
  value: string
  onChange: (v: string) => void
  rows?: number
  className?: string
  placeholder?: string
  multiline?: boolean
  spellCheck?: boolean
  syntax?: 'template' | 'expression'
  /** 这个字段属于哪个节点。给了才能判断「上游」 */
  nodeId?: string
  invalid?: boolean
  id?: string
  describedBy?: string
}) {
  const variables = useStudio((s) => s.variables)
  const varIssues = useStudio((s) => s.varIssues)
  const edges = useStudio((s) => s.edges)
  const traceVariable = useStudio((s) => s.traceVariable)
  const ref = useRef<HTMLTextAreaElement & HTMLInputElement>(null)
  const mirror = useRef<HTMLDivElement>(null)
  const [query, setQuery] = useState<{ start: number; end: number; text: string } | null>(null)
  const [active, setActive] = useState(0)
  const [bracesHint, setBracesHint] = useState(false)
  const tracing = useRef<string | null>(null)

  const reach = useMemo(() => reachOf(nodeId, edges), [nodeId, edges])
  const ranked = useMemo(
    () => (query ? rankVars(variables, query.text, nodeId, reach) : { ok: [], blocked: [] }),
    [variables, query, nodeId, reach],
  )
  const matches = ranked.ok.slice(0, 8)
  const blocked = ranked.blocked.slice(0, 4)
  const open = !!query && (matches.length > 0 || blocked.length > 0)

  useEffect(() => { setActive(0) }, [query?.text])

  // 离开的时候把画布上的血缘高亮收掉，不然卸载了它还亮着
  useEffect(() => () => { if (tracing.current) traceVariable(null) }, [traceVariable])

  /** 光标前刚打完 `{{` 或正在写路径时，把待补全的片段找出来 */
  const detect = (text: string, caret: number) => {
    const before = text.slice(0, caret)
    if (syntax === 'expression') {
      // 裸路径：光标前连着的一段标识符（可以带点）
      const m = /[A-Za-z_][\w.]*$/.exec(before)
      if (!m || (m.index > 0 && /[\w.'"]/.test(before[m.index - 1]))) return null
      return { start: m.index, end: caret, text: m[0] }
    }
    const at = before.lastIndexOf('{{')
    if (at < 0) return null
    const frag = before.slice(at + 2)
    // 已经收口了，或者已经在写过滤器/下标了，就别再弹——那不是在选变量
    if (frag.includes('}}') || frag.includes('|') || frag.includes('[')) return null
    if (frag.length > 40 || /[\n]/.test(frag)) return null
    return { start: at, end: caret, text: frag.trim() }
  }

  const insert = (path: string) => {
    if (!query) return
    const el = ref.current
    if (!el) return
    let next: string
    let pos: number
    if (syntax === 'expression') {
      next = `${value.slice(0, query.start)}${path}${value.slice(query.end)}`
      pos = query.start + path.length
    } else {
      const after = value.slice(query.end)
      // 全站的写法都是两边带空格：{{ vars.x }}。后端 strip 后匹配，怎么写都行，
      // 但和既有内容保持一致，不然同一个文件里两种风格
      const closed = after.startsWith('}}') || after.startsWith(' }}') ? '' : ' }}'
      next = `${value.slice(0, query.start)}{{ ${path}${closed}${after}`
      pos = query.start + 3 + path.length + closed.length
    }
    onChange(next)
    setQuery(null)
    // 这是完全受控组件：onChange 会一路走到 store 再把 value 重新灌回来，
    // DOM 的光标会跳到末尾。必须等那一帧过去再自己放回去
    requestAnimationFrame(() => {
      el.focus()
      el.setSelectionRange(pos, pos)
    })
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!open || !matches.length) {
      if (open && e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); setQuery(null) }
      return
    }
    // 弹层开着的时候这几个键有原生行为（换行、移光标、跳焦点），得拦下来
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive((i) => (i + 1) % matches.length) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((i) => (i - 1 + matches.length) % matches.length) }
    else if ((e.key === 'Enter' || e.key === 'Tab') && !isComposing(e)) { e.preventDefault(); insert(matches[active].path) }
    else if (e.key === 'Escape') {
      // 只关弹层，别让它冒泡上去——外面 InspectorSheet 也在听 Escape
      e.preventDefault()
      e.stopPropagation()
      setQuery(null)
    }
  }

  // ---- 镜像高亮层 ----

  /** 这个节点上报出来的变量问题：path → 级别。镜像里据此画波浪线 */
  const flagged = useMemo(() => {
    const out = new Map<string, VarIssue['level']>()
    for (const i of varIssues) {
      if (i.node_id !== nodeId || !i.path || i.level === 'info') continue
      if (out.get(i.path) !== 'error') out.set(i.path, i.level)
    }
    return out
  }, [varIssues, nodeId])
  const known = useMemo(() => new Map(variables.map((v) => [v.path, v])), [variables])
  const tokens = useMemo(() => tokenize(value ?? '', syntax), [value, syntax])

  const toneOf = (path: string): 'ok' | 'error' | 'late' => {
    const flag = flagged.get(path)
    if (flag === 'error') return 'error'
    if (flag === 'warning') return 'late'
    const v = known.get(path)
    // 分析还没回来（变量表是空的）时一律按正常画：宁可少标，不能满屏红
    if (!v) return variables.length && !/^(last_message|input|vars|nodes|run|now|today)$/.test(path.split('.')[0]) ? 'error' : 'ok'
    const why = availability(v, nodeId, reach)
    return why === 'ok' ? 'ok' : 'late'
  }

  // 镜像和输入框的排版必须逐像素一致：字体、内边距、边框、行高都照抄计算样式，
  // 滚动条占掉的宽度也要让出来，否则换行位置不一样，胶囊就错位了
  const sync = useCallback(() => {
    const el = ref.current
    const m = mirror.current
    if (!el || !m) return
    const cs = getComputedStyle(el)
    const props = ['fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'letterSpacing', 'tabSize',
      'paddingTop', 'paddingBottom', 'paddingLeft', 'borderTopWidth', 'borderBottomWidth',
      'borderLeftWidth', 'borderRightWidth', 'borderRadius', 'textIndent', 'wordSpacing'] as const
    for (const p of props) (m.style as any)[p] = cs[p]
    const scrollbar = el.offsetWidth - el.clientWidth
      - parseFloat(cs.borderLeftWidth) - parseFloat(cs.borderRightWidth)
    m.style.paddingRight = `${parseFloat(cs.paddingRight) + Math.max(0, scrollbar)}px`
    m.style.width = `${el.offsetWidth}px`
    m.style.height = `${el.offsetHeight}px`
    m.scrollTop = el.scrollTop
    m.scrollLeft = el.scrollLeft
  }, [])

  useLayoutEffect(() => { sync() }, [sync, value, className, rows])
  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => sync())
    ro.observe(el)
    return () => ro.disconnect()
  }, [sync])

  /** 悬停胶囊：画布上亮出这个变量的来龙去脉。输入框在上层，只能按坐标反查 */
  const hover = (e: React.MouseEvent) => {
    const m = mirror.current
    if (!m) return
    let hit: string | null = null
    for (const chip of m.querySelectorAll<HTMLElement>('[data-path]')) {
      for (const r of chip.getClientRects()) {
        if (e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom) {
          hit = chip.dataset.path ?? null
          break
        }
      }
      if (hit) break
    }
    if (hit === tracing.current) return
    tracing.current = hit
    traceVariable(hit)
  }
  const leave = () => {
    if (!tracing.current) return
    tracing.current = null
    traceVariable(null)
  }

  const common = {
    ref: ref as any,
    id,
    className: clsx('field relative bg-transparent', syntax === 'expression' && 'mono', className),
    value: value ?? '',
    placeholder,
    spellCheck: spellCheck ?? false,
    onKeyDown,
    'aria-invalid': invalid || undefined,
    'aria-describedby': describedBy,
    style: invalid ? { borderColor: 'var(--err)' } : undefined,
    onBlur: () => setTimeout(() => setQuery(null), 120),   // 等点击先落地
    onScroll: sync,
    onMouseMove: hover,
    onMouseLeave: leave,
    onChange: (e: any) => {
      const text = e.target.value
      onChange(text)
      setQuery(detect(text, e.target.selectionStart ?? 0))
      if (syntax === 'expression') setBracesHint(text.includes('{{'))
    },
    onClick: (e: any) => {
      if (syntax === 'template') setQuery(detect(e.target.value, e.target.selectionStart ?? 0))
    },
  }

  return (
    <div className="relative">
      <div
        ref={mirror}
        aria-hidden
        className={clsx('pointer-events-none absolute left-0 top-0 overflow-hidden border-solid border-transparent',
          syntax === 'expression' && 'mono', className)}
        style={{
          background: 'var(--bg)', color: 'transparent',
          whiteSpace: multiline ? 'pre-wrap' : 'pre', overflowWrap: multiline ? 'break-word' : 'normal',
        }}
      >
        {tokens.map((t, i) => {
          if (t.path == null) return <span key={i}>{t.text}</span>
          const tone = t.bad ? 'error' : toneOf(t.path)
          return <span key={i} data-path={t.path} data-tone={tone} style={CHIP[tone]}>{t.text}</span>
        })}
        {/* 末尾换行在 div 里不占一行，textarea 里占：补一个字符撑住 */}
        {' '}
      </div>
      {multiline
        ? <textarea {...common} rows={rows} />
        : <input {...common} />}

      {syntax === 'expression' && bracesHint && /\{\{/.test(value ?? '') && (
        <div className="mt-1 flex items-start gap-1.5 text-2xs leading-snug" style={{ color: 'var(--warn)' }}>
          <span className="mono shrink-0">ƒx</span>
          <span>这里是表达式，不需要 {'{{ }}'}：直接写 <code className="mono">vars.x == 1</code></span>
        </div>
      )}

      {open && (
        // 普通文档流里的块，不是绝对定位的浮层：属性面板是 overflow-y-auto、
        // 外面 InspectorSheet 是 overflow-hidden，浮层到边缘就被切掉。
        // 这也和 MultiPick 的既有做法一致
        <div className="fade-up mt-1 overflow-hidden rounded-lg border bg-bg" role="listbox">
          {matches.map((v, i) => (
            <button
              key={v.path}
              role="option"
              aria-selected={i === active}
              className={clsx('flex w-full items-center gap-2 px-2 py-1.5 text-left',
                i === active ? 'bg-hover' : 'hover:bg-hover')}
              // onMouseDown 而不是 onClick：onClick 在 blur 之后才触发，
              // 那时候弹层已经关了
              onMouseDown={(e) => { e.preventDefault(); insert(v.path) }}
              onMouseEnter={() => setActive(i)}
            >
              <span className="mono shrink-0 text-2xs text-fg">{v.path}</span>
              <span className="min-w-0 flex-1 truncate text-2xs text-faint">
                {v.produced_by_label ? `来自「${v.produced_by_label}」` : v.description || v.label}
              </span>
            </button>
          ))}
          {!!blocked.length && (
            <div className="border-t" style={{ borderColor: 'var(--hairline)' }}>
              {blocked.map(({ v, why }) => (
                <div key={v.path} className="flex items-center gap-2 px-2 py-1 opacity-60"
                     title="这一步还取不到它：运行到这里时它是空的">
                  <span className="mono shrink-0 text-2xs text-faint line-through">{v.path}</span>
                  <span className="min-w-0 flex-1 truncate text-2xs text-faint">{BLOCKED_TEXT[why](v)}</span>
                </div>
              ))}
            </div>
          )}
          <div className="border-t px-2 py-1 text-2xs text-faint">
            {matches.length ? `↑↓ 选择 · ${formatShortcut('Enter')} 插入 · Esc 关掉` : '这一步能取到的变量里没有匹配的'}
          </div>
        </div>
      )}
    </div>
  )
}

// -------------------------------------------------------------------------

/**
 * 胶囊不能加内边距：镜像里多一个像素，后面的字就和输入框里的对不上了。
 * 描边用 inset 阴影，不占排版。正常的引用安静（中性底色），出问题的才醒目
 */
const CHIP: Record<'ok' | 'error' | 'late', React.CSSProperties> = {
  ok: {
    borderRadius: 3,
    background: 'color-mix(in srgb, var(--text) 8%, transparent)',
    boxShadow: 'inset 0 0 0 1px color-mix(in srgb, var(--text) 18%, transparent)',
  },
  error: {
    borderRadius: 3,
    background: 'color-mix(in srgb, var(--err) 14%, transparent)',
    textDecoration: 'underline wavy var(--err)',
    textDecorationSkipInk: 'none',
    textUnderlineOffset: 3,
  },
  late: {
    borderRadius: 3,
    background: 'color-mix(in srgb, var(--warn) 14%, transparent)',
    textDecoration: 'underline wavy var(--warn)',
    textDecorationSkipInk: 'none',
    textUnderlineOffset: 3,
  },
}

type Why = 'ok' | 'self' | 'later' | 'outside'

const BLOCKED_TEXT: Record<Exclude<Why, 'ok'>, (v: Variable) => string> = {
  self: () => '这一步自己的产出，这里还取不到',
  later: (v) => `这里还取不到：「${v.produced_by_label ?? '?'}」不在这一步的上游`,
  outside: (v) => `只在「${v.produced_by_label ?? '循环'}」的循环体里有值`,
}

/** 一个节点能看到谁：它的全部上游，和它所在的循环体 */
interface Reach {
  ancestors: Set<string> | null
  bodies: Map<string, Set<string>>
}

function reachOf(nodeId: string | undefined, edges: Edge[]): Reach {
  if (!nodeId) return { ancestors: null, bodies: new Map() }
  const incoming = new Map<string, string[]>()
  const outgoing = new Map<string, Edge[]>()
  for (const e of edges) {
    incoming.set(e.target, [...(incoming.get(e.target) ?? []), e.source])
    outgoing.set(e.source, [...(outgoing.get(e.source) ?? []), e])
  }
  const ancestors = new Set<string>()
  const stack = [...(incoming.get(nodeId) ?? [])]
  while (stack.length) {
    const id = stack.pop()!
    if (ancestors.has(id) || id === nodeId) continue
    ancestors.add(id)
    stack.push(...(incoming.get(id) ?? []))
  }
  // 循环体：从 body 出口出发、不穿过循环节点本身能走到的节点（和后端 _loop_bodies 同一个口径）
  const bodies = new Map<string, Set<string>>()
  for (const e of edges) {
    if (e.sourceHandle !== 'body') continue
    const body = bodies.get(e.source) ?? new Set<string>()
    const walk = [e.target]
    while (walk.length) {
      const id = walk.pop()!
      if (id === e.source || body.has(id)) continue
      body.add(id)
      for (const o of outgoing.get(id) ?? []) walk.push(o.target)
    }
    bodies.set(e.source, body)
  }
  return { ancestors, bodies }
}

function availability(v: Variable, nodeId: string | undefined, reach: Reach): Why {
  if (!nodeId || !reach.ancestors || !v.produced_by) return 'ok'
  // 入口字段在运行一开始就有，放在哪一步都取得到
  if (v.kind === 'input') return 'ok'
  if (v.produced_by === nodeId) return 'self'
  if (v.kind === 'loop') return reach.bodies.get(v.produced_by)?.has(nodeId) ? 'ok' : 'outside'
  return reach.ancestors.has(v.produced_by) ? 'ok' : 'later'
}

const KIND_RANK: Record<string, number> = { var: 0, loop: 0, input: 1, node: 2, builtin: 3 }

/**
 * 排序：前缀命中优先；然后变量 → 入口 → 节点输出 → 内置；同类里离这一步越近越靠前
 * （order 越大越靠后执行，也就越靠近当前节点）。
 */
function rankVars(
  variables: Variable[], q: string, nodeId: string | undefined, reach: Reach,
): { ok: Variable[]; blocked: { v: Variable; why: Exclude<Why, 'ok'> }[] } {
  const query = q.toLowerCase()
  const hit = query ? variables.filter((v) => v.path.toLowerCase().includes(query)) : variables
  const score = (v: Variable) => (query && !v.path.toLowerCase().startsWith(query) ? 1000 : 0)
    + (KIND_RANK[v.kind] ?? 4) * 100 - Math.min(99, Math.max(0, v.order))
  const ok: Variable[] = []
  const blocked: { v: Variable; why: Exclude<Why, 'ok'> }[] = []
  for (const v of [...hit].sort((a, b) => score(a) - score(b))) {
    const why = availability(v, nodeId, reach)
    if (why === 'ok') ok.push(v)
    else blocked.push({ v, why })
  }
  return { ok, blocked }
}

interface Token { text: string; path?: string; bad?: boolean }

/**
 * 切出引用。模板里是 {{ … }}，表达式里是裸路径 vars.x / input.x / nodes.x。
 * 表达式里的 {{ 本身就是错，整段标红。
 */
function tokenize(text: string, syntax: 'template' | 'expression'): Token[] {
  const out: Token[] = []
  const re = syntax === 'template'
    ? /\{\{\s*([^}]*?)\s*\}\}/g
    : /\{\{[^}]*\}\}|\b(?:vars|input|nodes)(?:\.[A-Za-z_一-龥][\w一-龥]*)+/g
  let last = 0
  for (let m = re.exec(text); m; m = re.exec(text)) {
    if (m.index > last) out.push({ text: text.slice(last, m.index) })
    if (syntax === 'template') {
      // vars.rows[0].name | json → vars.rows.name：和后端 _root_of 同一个口径
      const path = (m[1] ?? '').split('|')[0].replace(/\[[^\]]*\]/g, '').trim()
      out.push({ text: m[0], path: path || m[0] })
    } else if (m[0].startsWith('{{')) {
      out.push({ text: m[0], path: m[0], bad: true })
    } else {
      out.push({ text: m[0], path: m[0] })
    }
    last = m.index + m[0].length
  }
  if (last < text.length) out.push({ text: text.slice(last) })
  return out
}
