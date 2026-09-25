import { useEffect, useRef, useState } from 'react'
import clsx from 'clsx'
import { isComposing } from '../components/ui'
import { useStudio } from '../store/studio'
import type { Variable } from '../types'

/**
 * 会写 `{{ }}` 的输入框，打 `{{` 弹出可用变量。
 *
 * 为什么值得做：模板取不到值时渲染成空字符串、不报错，所以拼错一个变量名
 * 在运行时是完全静默的。补全是从源头上消灭这一整类错误——比事后校验更管用。
 *
 * **只能挂在真正过模板渲染的字段上。** 这套里有五个字段长得像但走的是
 * 表达式求值（skip_if、loop.condition、cases[].condition、transform.expression、
 * metrics[].expression），那里要写 `vars.count == 0` 这样的裸路径，不带大
 * 括号。在那些字段上挂 `{{` 触发器等于手把手教用户写错。挂哪些由 nodeDefs
 * 的 template 标记决定，不在这里猜。
 */
export function TemplateText({
  value, onChange, rows, className, placeholder, multiline = true, spellCheck,
}: {
  value: string
  onChange: (v: string) => void
  rows?: number
  className?: string
  placeholder?: string
  multiline?: boolean
  spellCheck?: boolean
}) {
  const variables = useStudio((s) => s.variables)
  const ref = useRef<HTMLTextAreaElement & HTMLInputElement>(null)
  const [query, setQuery] = useState<{ start: number; text: string } | null>(null)
  const [active, setActive] = useState(0)

  const matches = query ? filterVars(variables, query.text).slice(0, 8) : []
  const open = !!query && matches.length > 0

  useEffect(() => { setActive(0) }, [query?.text])

  /** 光标前刚打完 `{{` 或正在写路径时，把待补全的片段找出来 */
  const detect = (text: string, caret: number) => {
    const before = text.slice(0, caret)
    const at = before.lastIndexOf('{{')
    if (at < 0) return null
    const frag = before.slice(at + 2)
    // 已经收口了，或者已经在写过滤器/下标了，就别再弹——那不是在选变量
    if (frag.includes('}}') || frag.includes('|') || frag.includes('[')) return null
    if (frag.length > 40 || /[\n]/.test(frag)) return null
    return { start: at, text: frag.trim() }
  }

  const insert = (path: string) => {
    if (!query) return
    const el = ref.current
    if (!el) return
    const caret = el.selectionStart ?? value.length
    const after = value.slice(caret)
    // 全站的写法都是两边带空格：{{ vars.x }}。后端 strip 后匹配，怎么写都行，
    // 但和既有内容保持一致，不然同一个文件里两种风格
    const closed = after.startsWith('}}') ? '' : ' }}'
    const next = `${value.slice(0, query.start)}{{ ${path}${closed}${after}`
    const pos = query.start + 3 + path.length + closed.length
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
    if (!open) return
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

  const common = {
    ref: ref as any,
    className: clsx('field', className),
    value: value ?? '',
    placeholder,
    spellCheck,
    onKeyDown,
    onBlur: () => setTimeout(() => setQuery(null), 120),   // 等点击先落地
    onChange: (e: any) => {
      onChange(e.target.value)
      setQuery(detect(e.target.value, e.target.selectionStart ?? 0))
    },
    onClick: (e: any) => setQuery(detect(e.target.value, e.target.selectionStart ?? 0)),
  }

  return (
    <div className="relative">
      {multiline
        ? <textarea {...common} rows={rows} />
        : <input {...common} />}

      {open && (
        // 普通文档流里的块，不是绝对定位的浮层：属性面板是 overflow-y-auto、
        // 外面 InspectorSheet 是 overflow-hidden，浮层到边缘就被切掉。
        // 这也和 MultiPick 的既有做法一致
        <div className="fade-up mt-1 overflow-hidden rounded-lg border bg-bg">
          {matches.map((v, i) => (
            <button
              key={v.path}
              className={clsx('flex w-full items-center gap-2 px-2 py-1.5 text-left',
                i === active ? 'bg-hover' : 'hover:bg-hover')}
              // onMouseDown 而不是 onClick：onClick 在 blur 之后才触发，
              // 那时候弹层已经关了
              onMouseDown={(e) => { e.preventDefault(); insert(v.path) }}
              onMouseEnter={() => setActive(i)}
            >
              <span className="mono shrink-0 text-[11px]" style={{ color: 'var(--accent)' }}>
                {v.path}
              </span>
              <span className="min-w-0 flex-1 truncate text-[10px] text-faint">
                {v.description || v.label}
              </span>
            </button>
          ))}
          <div className="border-t px-2 py-1 text-[9.5px] text-faint">
            ↑↓ 选择 · ⏎ 插入 · esc 关掉
          </div>
        </div>
      )}
    </div>
  )
}

/** 排序：前缀命中优先，然后按 input → vars → nodes → 内置 的既有顺序 */
function filterVars(variables: Variable[], q: string): Variable[] {
  const query = q.toLowerCase()
  if (!query) return variables
  const hit = variables.filter((v) => v.path.toLowerCase().includes(query))
  return hit.sort((a, b) => {
    const ap = a.path.toLowerCase().startsWith(query) ? 0 : 1
    const bp = b.path.toLowerCase().startsWith(query) ? 0 : 1
    return ap - bp
  })
}
