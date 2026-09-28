import { createContext, Fragment, useContext, useState, type ReactNode } from 'react'
import { Check, Copy } from 'lucide-react'
import clsx from 'clsx'
import { toast } from '../components/ui'

/**
 * 模型输出的 Markdown 渲染。
 *
 * 在此之前成果区是 whitespace-pre-wrap 的纯文本，于是「1+1 等于 **2**」就
 * 原样带着星号显示——模型几乎总是用 Markdown 组织回答，不渲染等于把它的
 * 结构全丢了。
 *
 * **手写而不是装库。** 依赖只有七个，全是必需的；react-markdown 那一串
 * （unified / mdast / micromark）比这个文件大一个数量级，而实际要渲染的
 * 东西是有限的——统计了库里 252 段真实输出，出现过的构造只有下面这些：
 *
 *     有序列表 40% · 无序列表 15% · 粗体 13% · 标题 3% · 行内代码 3%
 *     表格 3% · 分隔线 2% · 代码块 1% · 引用 1% · 斜体 1% · 链接 0.8%
 *
 * 所以这里只做这些，**不声称支持完整 CommonMark**。认不出来的语法原样当
 * 文本显示——那正是现在的行为，所以下限不会比今天差；能认出来的就渲染对。
 *
 * 全程构造 React 元素，一处 dangerouslySetInnerHTML 都没有。模型输出是不
 * 可信内容，走 innerHTML 就等于给自己开了个 XSS 口子。
 */
export function Markdown({ text, dense, marks }: {
  text: string
  dense?: boolean
  /** 要在正文里标出来的词：出具时无法回指的数字、能回指到口径卡的数字 */
  marks?: MarkSpec[]
}) {
  if (!text?.trim()) return null
  return (
    <MarksContext.Provider value={marks?.length ? marks : null}>
      <div className={clsx('space-y-2 leading-relaxed', dense ? 'text-[11.5px]' : 'text-sm')}>
        {parseBlocks(text).map((b, i) => <Block key={i} block={b} dense={dense} />)}
      </div>
    </MarksContext.Provider>
  )
}

/**
 * 正文里要标出来的一个词。
 *
 * 出具校验认得出「哪些数字回指不到口径卡」，但以前只在横幅里列一串孤零零的
 * token——读的人得自己回正文里找「6」是哪句话里的那个 6。直接在原句上画出来，
 * 悬停说明原因，证据才落到读的地方。
 */
export interface MarkSpec {
  token: string
  title: string
  tone: 'warn' | 'ok'
}

const MarksContext = createContext<MarkSpec[] | null>(null)

/** 纯文本段里的标记词包一层。数字按词边界认：「6」不能命中「94.26」里的那个 6 */
function markText(text: string, marks: MarkSpec[] | null, keyBase: number): ReactNode[] {
  if (!marks || !text) return [text]
  const alts = [...marks].sort((a, b) => b.token.length - a.token.length)
    .map((m) => m.token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  const re = new RegExp(`(?<![\\d.])(?:${alts.join('|')})(?![\\d]|\\.\\d)`, 'g')
  const out: ReactNode[] = []
  let last = 0
  for (let m = re.exec(text); m; m = re.exec(text)) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const spec = marks.find((x) => x.token === m![0])!
    out.push(
      <span key={`mk${keyBase}-${m.index}`} title={spec.title}
            className="cursor-help underline decoration-dotted underline-offset-[3px]"
            style={{ textDecorationColor: spec.tone === 'warn' ? 'var(--st-waiting)' : 'var(--st-done)' }}
            data-mark={spec.tone}>
        {m[0]}
      </span>,
    )
    last = m.index + m[0].length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

// -------------------------------------------------------------------------
// 块级
// -------------------------------------------------------------------------

type Block =
  | { kind: 'p'; text: string }
  | { kind: 'h'; level: number; text: string }
  | { kind: 'code'; lang: string; code: string }
  | { kind: 'quote'; text: string }
  | { kind: 'hr' }
  | { kind: 'list'; ordered: boolean; start?: number; items: { text: string; depth: number }[] }
  | { kind: 'table'; head: string[]; rows: string[][] }

const FENCE = /^\s*```(\w*)\s*$/
const HEADING = /^(#{1,6})\s+(.*)$/
const HR = /^\s*(-{3,}|\*{3,}|_{3,})\s*$/
const BULLET = /^(\s*)[-*+]\s+(.*)$/
const ORDERED = /^(\s*)(\d+)[.)]\s+(.*)$/
const QUOTE = /^\s*>\s?(.*)$/
const TABLE_ROW = /^\s*\|(.+)\|\s*$/
const TABLE_SEP = /^\s*\|[\s:|-]+\|\s*$/

function parseBlocks(text: string): Block[] {
  const lines = text.replace(/\r\n?/g, '\n').split('\n')
  const out: Block[] = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i]

    if (!line.trim()) { i += 1; continue }

    // 代码块优先：里面的一切都不当语法看
    const fence = line.match(FENCE)
    if (fence) {
      const body: string[] = []
      i += 1
      while (i < lines.length && !FENCE.test(lines[i])) { body.push(lines[i]); i += 1 }
      i += 1   // 吃掉收尾的 ```
      out.push({ kind: 'code', lang: fence[1] || '', code: body.join('\n') })
      continue
    }

    // 分隔线要在列表之前判：--- 也能匹配 BULLET
    if (HR.test(line)) { out.push({ kind: 'hr' }); i += 1; continue }

    const heading = line.match(HEADING)
    if (heading) {
      out.push({ kind: 'h', level: heading[1].length, text: heading[2] })
      i += 1
      continue
    }

    // 表格：一行 | ... | 后面紧跟一行分隔行，两者都有才算
    if (TABLE_ROW.test(line) && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      const head = splitRow(line)
      const rows: string[][] = []
      i += 2
      while (i < lines.length && TABLE_ROW.test(lines[i])) { rows.push(splitRow(lines[i])); i += 1 }
      out.push({ kind: 'table', head, rows })
      continue
    }

    const quote = line.match(QUOTE)
    if (quote) {
      const body = [quote[1]]
      i += 1
      while (i < lines.length && QUOTE.test(lines[i])) {
        body.push(lines[i].match(QUOTE)![1]); i += 1
      }
      out.push({ kind: 'quote', text: body.join('\n') })
      continue
    }

    const bullet = line.match(BULLET)
    const ordered = line.match(ORDERED)
    if (bullet || ordered) {
      // 一段连续的列表算一块。有序和无序混排时按第一条定性——模型不常这么写，
      // 真遇上了也比拆成两块好看
      const isOrdered = !!ordered
      const items: { text: string; depth: number }[] = []
      while (i < lines.length) {
        const b = lines[i].match(BULLET)
        const o = b ? null : lines[i].match(ORDERED)
        if (b || o) {
          const indent = (b ?? o)![1]
          items.push({ text: b ? b[2] : o![3], depth: Math.floor(indent.replace(/\t/g, '  ').length / 2) })
          i += 1
        } else if (lines[i].trim() && !isBlockStart(lines[i]) && items.length) {
          // 悬挂缩进的续行接到上一条上，不要另起一段
          items[items.length - 1].text += '\n' + lines[i].trim()
          i += 1
        } else break
      }
      // 模型常把有序列表写成被空行隔开的几段，每段各自一块。不记起始序号的话
      // 三段都从 1 开始，「1. 1. 1.」读起来像三条并列的第一条
      out.push({ kind: 'list', ordered: isOrdered, items,
                 ...(ordered ? { start: Number(ordered[2]) } : {}) })
      continue
    }

    // 普通段落：一直吃到空行或下一个块开头
    const para = [line]
    i += 1
    while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i])) {
      para.push(lines[i]); i += 1
    }
    out.push({ kind: 'p', text: para.join('\n') })
  }
  return out
}

function isBlockStart(line: string): boolean {
  return FENCE.test(line) || HR.test(line) || HEADING.test(line)
    || BULLET.test(line) || ORDERED.test(line) || QUOTE.test(line) || TABLE_ROW.test(line)
}

function splitRow(line: string): string[] {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim())
}

function Block({ block, dense }: { block: Block; dense?: boolean }) {
  switch (block.kind) {
    case 'h': {
      // 侧栏只有 360px 宽，一级标题按 h1 的字号会大得不像话。
      // 按层级递减但整体压扁，保住层次感就够了
      const size = dense
        ? ['text-[13px]', 'text-[12px]', 'text-[11.5px]'][Math.min(block.level, 3) - 1]
        : ['text-base', 'text-sm', 'text-sm'][Math.min(block.level, 3) - 1]
      return (
        <div className={clsx('mt-3 font-semibold first:mt-0', size)}>
          <Inline text={block.text} />
        </div>
      )
    }
    case 'hr':
      return <hr className="my-2 border-t" />
    case 'code':
      return <CodeBlock lang={block.lang} code={block.code} dense={dense} />
    case 'quote':
      return (
        <blockquote className="border-l-2 pl-2.5 text-dim" style={{ borderColor: 'var(--accent)' }}>
          <Inline text={block.text} />
        </blockquote>
      )
    case 'list': {
      const Tag = block.ordered ? 'ol' : 'ul'
      return (
        <Tag start={block.ordered && block.start && block.start !== 1 ? block.start : undefined}
             className={clsx('space-y-1', block.ordered ? 'list-decimal' : 'list-disc',
               'ml-4 marker:text-faint')}>
          {block.items.map((it, i) => (
            <li key={i} style={{ marginLeft: it.depth * 14 }}>
              <Inline text={it.text} />
            </li>
          ))}
        </Tag>
      )
    }
    case 'table': {
      // 数值列右对齐、等宽数字：一列金额左对齐时小数点对不上，没法一眼比大小
      const numeric = block.head.map((h, j) => {
        if (CODE_COLUMN.test(h.replace(/[*`]/g, '').trim())) return false
        const cells = block.rows.map((r) => (r[j] ?? '').replace(/[*`]/g, '').trim()).filter(Boolean)
        return cells.length > 0 && cells.every((c) => NUMERIC_CELL.test(c) && !LEADING_ZERO.test(c))
      })
      return (
        <div className="group/table relative overflow-x-auto rounded-lg border">
          <CopyChip label="复制为 CSV" text={() => toCsv(block.head, block.rows)}
                    className="absolute right-1 top-1 opacity-0 group-hover/table:opacity-100 focus-visible:opacity-100" />
          <table className={clsx('w-full', dense ? 'text-[10.5px]' : 'text-xs')}>
            <thead>
              <tr className="border-b bg-elev">
                {block.head.map((h, i) => (
                  <th key={i} className={clsx('px-2 py-1 font-medium text-dim',
                    numeric[i] ? 'text-right' : 'text-left')}>
                    <Inline text={h} />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, i) => (
                <tr key={i} className="border-b last:border-0">
                  {/* 用表头的列数对齐：模型偶尔会少写一格，少的补空，
                      多的截掉——否则整张表会错位 */}
                  {block.head.map((_, j) => (
                    <td key={j} className={clsx('px-2 py-1 align-top text-dim',
                      numeric[j] && 'tnum text-right')}>
                      <Inline text={row[j] ?? ''} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    }
    default:
      return <p className="whitespace-pre-wrap"><Inline text={block.text} /></p>
  }
}

// -------------------------------------------------------------------------
// 行内
// -------------------------------------------------------------------------

// 顺序重要：行内代码最先，它里面的一切都不再当语法看；然后链接、粗体、
// 斜体。斜体放最后且要求两侧不是 *，否则 **粗体** 会被它先拆掉。
// 不带 g：每次用的时候按 source 新建一个带 g 的，避免 lastIndex 被共享
const INLINE = new RegExp([
  '(`+)([^`]+?)\\1',                       // `code`
  '\\[([^\\]]+)\\]\\(([^)\\s]+)[^)]*\\)',  // [text](url)
  '\\*\\*([^*]+?)\\*\\*',                  // **bold**
  '__([^_]+?)__',                          // __bold__
  '(?<!\\*)\\*([^*\\n]+?)\\*(?!\\*)',      // *italic*
  '~~([^~]+?)~~',                          // ~~strike~~
].join('|'))

function Inline({ text, depth = 0 }: { text: string; depth?: number }): ReactNode {
  if (!text) return null
  const out: ReactNode[] = []
  let last = 0
  let key = 0
  // 模块级正则带 g 标志，lastIndex 是有状态的。这里会递归调用自己（粗体
  // 里可以套行内代码），不每次重置就会从上一层留下的位置接着扫
  const re = new RegExp(INLINE.source, 'g')

  const marks = useContext(MarksContext)
  const plain = (t: string) => out.push(...markText(t, marks, key++))

  for (let m = re.exec(text); m; m = re.exec(text)) {
    if (m.index > last) plain(text.slice(last, m.index))
    const [, , code, linkText, href, bold1, bold2, italic, strike] = m

    // 强调的内容要再解析一层。模型很爱写 **`table_name`**（粗体里套代码），
    // 而粗体在扫描顺序上先命中，不递归的话那对反引号就原样显示出来了。
    // 深度有限：每层的文本都严格更短，且这里也卡了三层
    const sub = (t: string) =>
      depth < 3 ? <Inline text={t} depth={depth + 1} /> : t

    if (code !== undefined) {
      // 代码内部不再解析——这正是代码的含义
      out.push(
        <code key={key++} className="mono rounded bg-bg px-1 py-px text-[0.92em]">{code}</code>,
      )
    } else if (linkText !== undefined) {
      out.push(<Link key={key++} href={href} text={linkText} />)
    } else if (bold1 !== undefined || bold2 !== undefined) {
      out.push(<strong key={key++} className="font-semibold">{sub(bold1 ?? bold2)}</strong>)
    } else if (italic !== undefined) {
      out.push(<em key={key++}>{sub(italic)}</em>)
    } else if (strike !== undefined) {
      out.push(<s key={key++} className="text-faint">{sub(strike)}</s>)
    }
    last = m.index + m[0].length
  }
  if (last < text.length) plain(text.slice(last))
  return <>{out.map((n, i) => <Fragment key={i}>{n}</Fragment>)}</>
}

/**
 * 链接。
 *
 * 只放行 http/https/mailto。模型输出是不可信内容，`javascript:` 开头的
 * href 点一下就是脚本执行——这里不认的协议就退回纯文本显示，宁可少一个
 * 可点链接，也不留这个口子。
 */
function Link({ href, text }: { href: string; text: string }) {
  const safe = /^(https?:|mailto:)/i.test(href.trim())
  if (!safe) return <>{text}</>
  return (
    <a href={href} target="_blank" rel="noopener noreferrer"
       className="underline decoration-dotted underline-offset-2"
       style={{ color: 'var(--accent)' }}>
      {text}
    </a>
  )
}

// -------------------------------------------------------------------------
// 代码块、复制
// -------------------------------------------------------------------------

/** 「1,234」「-3.5」「94.23%」「$0.031」「12 ms」这类都算数值格 */
const NUMERIC_CELL = /^[-+]?[$¥€]?\d[\d,]*(\.\d+)?\s*(%|‰|ms|s|k|万|亿)?$/

/** 0 打头的整数串（'001'、'0086'）是编号不是数：右对齐、拿来排序都没有意义 */
export const LEADING_ZERO = /^[-+]?0\d/

/**
 * 列名看得出是编号、代码、分组、类型的：值再像数也不当"量"。factory_code 的 1063、
 * attribute_group 的 001 按数右对齐、给出「按 attribute_group 从高到低排」都说不通
 */
export const CODE_COLUMN =
  /(^id$|_id$|^id_|code|_no$|(^|_)(group|type|status|kind|class|category)$|编号|编码|代码|序号|分组|类型|状态)/i

function toCsv(head: string[], rows: string[][]): string {
  const cell = (v: string) => {
    const t = v.replace(/\*\*|__|`/g, '')
    return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t
  }
  return [head, ...rows.map((r) => head.map((_, j) => r[j] ?? ''))]
    .map((r) => r.map(cell).join(',')).join('\n')
}

/**
 * 代码块带语言标签和复制按钮。模型给的 SQL、脚本十有八九要被拿去别处跑，
 * 手动框选容易漏掉首尾一行
 */
function CodeBlock({ lang, code, dense }: { lang: string; code: string; dense?: boolean }) {
  return (
    <div className="group/code relative overflow-hidden rounded-lg border bg-bg">
      <div className="flex items-center gap-2 border-b px-2.5 py-1 text-2xs text-faint">
        <span className="mono">{lang || '代码'}</span>
        <span className="flex-1" />
        <CopyChip label="复制" text={() => code} />
      </div>
      <pre className={clsx('mono overflow-x-auto px-2.5 py-2 leading-relaxed', dense ? 'text-[10.5px]' : 'text-xs')}>
        <code>{code}</code>
      </pre>
    </div>
  )
}

/** 小号的复制键：点了变成对勾一秒半，失败走 toast */
export function CopyChip({ label, text, className }: {
  label: string; text: () => string; className?: string
}) {
  const [done, setDone] = useState(false)
  return (
    <button
      type="button"
      className={clsx('inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-2xs text-dim transition-colors hover:bg-hover hover:text-fg',
        className)}
      style={{ background: 'var(--bg-panel)' }}
      aria-label={label}
      title={label}
      onClick={(e) => {
        e.stopPropagation()
        const value = text()
        if (!navigator.clipboard) { toast.error('复制失败：浏览器没有给剪贴板权限'); return }
        void navigator.clipboard.writeText(value).then(() => {
          setDone(true)
          setTimeout(() => setDone(false), 1500)
        }, () => toast.error('复制失败：浏览器没有给剪贴板权限'))
      }}
    >
      {done ? <Check size={11} aria-hidden /> : <Copy size={11} aria-hidden />}
      {done ? '已复制' : label}
    </button>
  )
}
