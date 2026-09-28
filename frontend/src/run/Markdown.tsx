import {
  createContext, Fragment, useContext, useMemo, useState, type CSSProperties, type HTMLAttributes, type ReactNode,
} from 'react'
import { Check, Copy } from 'lucide-react'
import clsx from 'clsx'
import { toast } from '../components/ui'
import { EVIDENCE_STATE, codePointIndex } from '../lib/evidence'

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
 *
 * 块的外观（BlockView）单独导出：报告撰写节点的文档（run/EvidenceDoc）是后端切好的
 * 块和片段，不再解析 Markdown，但长得必须和这里一模一样。
 */
export function Markdown({ text, dense, marks }: {
  text: string
  dense?: boolean
  /** 要在正文里标出来的词：出具时无法回指的数字、能回指到口径卡的数字 */
  marks?: MarkSpec[]
}) {
  const ctx = useMemo(() => prepareMarks(text ?? '', marks), [text, marks])
  if (!text?.trim()) return null
  return (
    <MarksContext.Provider value={ctx}>
      <div className={clsx('space-y-2 leading-relaxed', dense ? 'text-[11.5px]' : 'text-sm')}>
        {parseBlocks(text).map((b, i) => <MdBlock key={i} block={b} dense={dense} />)}
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
 *
 * 带 start / end（后端 trace_numbers 给的码点偏移）的只标那一处：按字符串标的话，
 * 同一个数出现几次就画几处，日期「2026-09-15」里的 15 也会被当成回指不上的 15。
 * 老运行没有位置，位置和这段文字对不上（叙述模板在字段外面加了字）时，也退回按字符串标
 */
export interface MarkSpec {
  token: string
  title: string
  /**
   * ambiguous：回指上了，但同一个值对得上好几个指标，出处不唯一；
   * candidate：没有契约的旧运行按数值猜的候选（最淡的点状线，读屏在字后面听到「猜测的来源：…」）
   */
  tone: 'warn' | 'ok' | 'ambiguous' | 'candidate'
  /** 在原文里的码点偏移 [start, end) */
  start?: number
  end?: number
}

interface MarksCtx {
  /** 按字符串认的 */
  loose: MarkSpec[] | null
  /** 按位置认的：UTF-16 起点 → 终点和说明 */
  at: Map<number, { end: number; spec: MarkSpec }> | null
}

const MarksContext = createContext<MarksCtx | null>(null)

/** 位置换成 UTF-16 下标并逐个核对：切出来的字就是 token 才按位置标，否则交给字符串匹配 */
function prepareMarks(text: string, marks: MarkSpec[] | undefined): MarksCtx | null {
  if (!marks?.length) return null
  const loose: MarkSpec[] = []
  const at = new Map<number, { end: number; spec: MarkSpec }>()
  // 换行归一会挪动偏移：带 \r 的文字位置一律不认
  const toUtf16 = text.includes('\r') ? null : codePointIndex(text)
  for (const m of marks) {
    if (toUtf16 && typeof m.start === 'number' && typeof m.end === 'number' && m.end > m.start) {
      const s = toUtf16(m.start)
      const e = toUtf16(m.end)
      if (s >= 0 && e > s && text.slice(s, e) === m.token) {
        at.set(s, { end: e, spec: m })
        continue
      }
    }
    loose.push(m)
  }
  return { loose: loose.length ? loose : null, at: at.size ? at : null }
}

/**
 * 一段文字在原文里的位置：连续的给起点，拼接过的（引用的多行、列表的续行）给逐字的
 * 下标表。只有按位置标记时用得上，其余时候跟着传一下，不做任何事
 */
type At = number | number[]
const offsetAt = (at: At | undefined, i: number): number =>
  at == null ? -1 : typeof at === 'number' ? at + i : at[i] ?? -1
const shiftAt = (at: At | undefined, i: number): At | undefined =>
  at == null ? undefined : typeof at === 'number' ? at + i : at.slice(i)

function markSpan(spec: MarkSpec, text: string, key: string): ReactNode {
  if (spec.tone === 'candidate') {
    // 猜测：外观取证据状态表里「旧运行候选」那一档，绝不用确定性的实线和绿
    const meta = EVIDENCE_STATE.candidate
    return (
      <span key={key} title={spec.title} className="ev-mark cursor-help" data-mark="candidate"
            data-ev-state={meta.code} data-ev-line={meta.line} style={{ '--ev-line': meta.decoration } as CSSProperties}>
        {text}<span className="sr-only">（{meta.label}：{spec.title}）</span>
      </span>
    )
  }
  const color = spec.tone === 'ok' ? 'var(--st-done)' : 'var(--st-waiting)'
  return (
    <span key={key} title={spec.title}
          className="cursor-help underline decoration-dotted underline-offset-[3px]"
          style={{ textDecorationColor: color }}
          data-mark={spec.tone}>
      {text}
    </span>
  )
}

/** 纯文本段里的标记词包一层。数字按词边界认：「6」不能命中「94.26」里的那个 6 */
function markText(text: string, ctx: MarksCtx | null, keyBase: number, at?: At): ReactNode[] {
  if (!ctx || !text) return [text]
  const hits: { start: number; end: number; spec: MarkSpec }[] = []
  // 按位置：这一段里每个字在原文里的下标，撞上某个标记的起点、并且整段都落在这一段里
  if (ctx.at && at != null) {
    for (let i = 0; i < text.length; i++) {
      const hit = ctx.at.get(offsetAt(at, i))
      if (!hit) continue
      // 末字也得正好落在标记的末尾：拼接过的文字（引用的多行）里，字面相同不等于位置相同
      const len = hit.spec.token.length
      if (text.slice(i, i + len) === hit.spec.token && offsetAt(at, i + len - 1) === hit.end - 1) {
        hits.push({ start: i, end: i + len, spec: hit.spec })
        i += len - 1
      }
    }
  }
  if (ctx.loose) {
    const alts = [...ctx.loose].sort((a, b) => b.token.length - a.token.length)
      .map((m) => m.token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
    const re = new RegExp(`(?<![\\d.])(?:${alts.join('|')})(?![\\d]|\\.\\d)`, 'g')
    for (let m = re.exec(text); m; m = re.exec(text)) {
      const start = m.index
      const end = start + m[0].length
      if (hits.some((h) => start < h.end && h.start < end)) continue
      hits.push({ start, end, spec: ctx.loose.find((x) => x.token === m![0])! })
    }
  }
  if (!hits.length) return [text]
  hits.sort((a, b) => a.start - b.start)
  const out: ReactNode[] = []
  let last = 0
  for (const h of hits) {
    if (h.start > last) out.push(text.slice(last, h.start))
    out.push(markSpan(h.spec, text.slice(h.start, h.end), `mk${keyBase}-${h.start}`))
    last = h.end
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

// -------------------------------------------------------------------------
// 块级
// -------------------------------------------------------------------------

/** 带着在原文里位置的一段文字（位置只给按位置标记用） */
interface Src { text: string; at: At }

type Block =
  | { kind: 'p'; text: string; at: At }
  | { kind: 'h'; level: number; text: string; at: At }
  | { kind: 'code'; lang: string; code: string }
  | { kind: 'quote'; text: string; at: At }
  | { kind: 'hr' }
  | { kind: 'list'; ordered: boolean; start?: number; items: { text: string; at: At; depth: number }[] }
  | { kind: 'table'; head: Src[]; rows: Src[][] }

const FENCE = /^\s*```(\w*)\s*$/
const HEADING = /^(#{1,6})\s+(.*)$/
const HR = /^\s*(-{3,}|\*{3,}|_{3,})\s*$/
const BULLET = /^(\s*)[-*+]\s+(.*)$/
const ORDERED = /^(\s*)(\d+)[.)]\s+(.*)$/
const QUOTE = /^\s*>\s?(.*)$/
const TABLE_ROW = /^\s*\|(.+)\|\s*$/
const TABLE_SEP = /^\s*\|[\s:|-]+\|\s*$/

/** 逐字下标表：几段不相连的文字拼成一段时，每个字记着它原来在哪 */
function joinAt(parts: { text: string; at: number }[], sep: string): number[] {
  const out: number[] = []
  parts.forEach((p, k) => {
    if (k > 0) for (let j = 0; j < sep.length; j++) out.push(-1)
    for (let j = 0; j < p.text.length; j++) out.push(p.at + j)
  })
  return out
}

function parseBlocks(text: string): Block[] {
  const lines = text.replace(/\r\n?/g, '\n').split('\n')
  // 每一行在原文里从哪开始
  const starts: number[] = []
  for (let i = 0, pos = 0; i < lines.length; i++) { starts.push(pos); pos += lines[i].length + 1 }
  /** 这一行里 content 这段（它总是顶到行尾）的起点 */
  const tail = (i: number, content: string) => starts[i] + lines[i].length - content.length
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
      out.push({ kind: 'h', level: heading[1].length, text: heading[2], at: tail(i, heading[2]) })
      i += 1
      continue
    }

    // 表格：一行 | ... | 后面紧跟一行分隔行，两者都有才算
    if (TABLE_ROW.test(line) && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      const head = splitRow(line, starts[i])
      const rows: Src[][] = []
      i += 2
      while (i < lines.length && TABLE_ROW.test(lines[i])) { rows.push(splitRow(lines[i], starts[i])); i += 1 }
      out.push({ kind: 'table', head, rows })
      continue
    }

    const quote = line.match(QUOTE)
    if (quote) {
      const body = [{ text: quote[1], at: tail(i, quote[1]) }]
      i += 1
      while (i < lines.length && QUOTE.test(lines[i])) {
        const q = lines[i].match(QUOTE)![1]
        body.push({ text: q, at: tail(i, q) }); i += 1
      }
      out.push({ kind: 'quote', text: body.map((b) => b.text).join('\n'), at: joinAt(body, '\n') })
      continue
    }

    const bullet = line.match(BULLET)
    const ordered = line.match(ORDERED)
    if (bullet || ordered) {
      // 一段连续的列表算一块。有序和无序混排时按第一条定性——模型不常这么写，
      // 真遇上了也比拆成两块好看
      const isOrdered = !!ordered
      const items: { parts: { text: string; at: number }[]; depth: number }[] = []
      while (i < lines.length) {
        const b = lines[i].match(BULLET)
        const o = b ? null : lines[i].match(ORDERED)
        if (b || o) {
          const indent = (b ?? o)![1]
          const body = b ? b[2] : o![3]
          items.push({ parts: [{ text: body, at: tail(i, body) }], depth: Math.floor(indent.replace(/\t/g, '  ').length / 2) })
          i += 1
        } else if (lines[i].trim() && !isBlockStart(lines[i]) && items.length) {
          // 悬挂缩进的续行接到上一条上，不要另起一段
          const t = lines[i].trim()
          items[items.length - 1].parts.push({ text: t, at: starts[i] + lines[i].indexOf(t) })
          i += 1
        } else break
      }
      // 模型常把有序列表写成被空行隔开的几段，每段各自一块。不记起始序号的话
      // 三段都从 1 开始，「1. 1. 1.」读起来像三条并列的第一条
      out.push({
        kind: 'list', ordered: isOrdered,
        items: items.map((it) => ({
          text: it.parts.map((p) => p.text).join('\n'),
          at: it.parts.length === 1 ? it.parts[0].at : joinAt(it.parts, '\n'),
          depth: it.depth,
        })),
        ...(ordered ? { start: Number(ordered[2]) } : {}),
      })
      continue
    }

    // 普通段落：一直吃到空行或下一个块开头。行是原样接起来的，位置连续
    const para = [line]
    const at = starts[i]
    i += 1
    while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i])) {
      para.push(lines[i]); i += 1
    }
    out.push({ kind: 'p', text: para.join('\n'), at })
  }
  return out
}

function isBlockStart(line: string): boolean {
  return FENCE.test(line) || HR.test(line) || HEADING.test(line)
    || BULLET.test(line) || ORDERED.test(line) || QUOTE.test(line) || TABLE_ROW.test(line)
}

/** 表格的一行拆成格：去掉首尾竖线、每格去掉两边空白。记下每格去空白之后从哪开始 */
function splitRow(line: string, lineStart: number): Src[] {
  const lead = line.length - line.trimStart().length
  let body = line.trim()
  let base = lineStart + lead
  if (body.startsWith('|')) { body = body.slice(1); base += 1 }
  if (body.endsWith('|')) body = body.slice(0, -1)
  const out: Src[] = []
  let pos = 0
  for (const raw of body.split('|')) {
    out.push({ text: raw.trim(), at: base + pos + (raw.length - raw.trimStart().length) })
    pos += raw.length + 1
  }
  return out
}

/** 解析出来的块交给 BlockView：每段文字按行内语法渲染 */
function MdBlock({ block, dense }: { block: Block; dense?: boolean }) {
  switch (block.kind) {
    case 'h': return <BlockView dense={dense} shape={{ kind: 'h', level: block.level, body: <Inline text={block.text} at={block.at} /> }} />
    case 'hr': return <BlockView dense={dense} shape={{ kind: 'hr' }} />
    case 'code': return <BlockView dense={dense} shape={{ kind: 'code', lang: block.lang, code: block.code }} />
    case 'quote': return <BlockView dense={dense} shape={{ kind: 'quote', body: <Inline text={block.text} at={block.at} /> }} />
    case 'list':
      return (
        <BlockView dense={dense} shape={{
          kind: 'list', ordered: block.ordered, start: block.start,
          items: block.items.map((it) => ({ depth: it.depth, body: <Inline text={it.text} at={it.at} /> })),
        }} />
      )
    case 'table': {
      const head = block.head.map((h) => h.text)
      const rows = block.rows.map((r) => r.map((c) => c.text))
      return (
        <BlockView dense={dense} shape={{
          kind: 'table',
          head: block.head.map((h, i) => <Inline key={i} text={h.text} at={h.at} />),
          // 用表头的列数对齐：模型偶尔会少写一格，少的补空，多的截掉——否则整张表会错位
          rows: block.rows.map((row) => block.head.map((_, j) => (
            row[j] ? <Inline text={row[j].text} at={row[j].at} /> : null))),
          numeric: numericColumns(head, rows),
          csv: () => toCsv(head, rows),
        }} />
      )
    }
    default:
      return <BlockView dense={dense} shape={{ kind: 'p', body: <Inline text={block.text} at={block.at} /> }} />
  }
}

/**
 * 一个块的外观，内容由调用方给。Markdown 给的是按行内语法渲染的文字，EvidenceDoc
 * 给的是后端切好的片段——两处长得一模一样，改外观只改这里
 */
export type BlockShape =
  | { kind: 'p'; body: ReactNode }
  | { kind: 'h'; level: number; body: ReactNode }
  | { kind: 'code'; lang: ReactNode; code: string; body?: ReactNode }
  | { kind: 'quote'; body: ReactNode }
  | { kind: 'hr' }
  | { kind: 'list'; ordered: boolean; start?: number; items: { body: ReactNode; depth: number; key?: string }[]
      /** 序号按位数留宽（「100.」比 1rem 宽）。普通答案不开，保持原样 */
      fitMarkers?: boolean }
  | { kind: 'table'; head: ReactNode[]; rows: ReactNode[][]; numeric: boolean[]; csv: () => string }

/** 块最外层那个元素上额外要挂的属性（EvidenceDoc 挂 data-block 和 content-visibility） */
type RootProps = HTMLAttributes<HTMLElement> & Record<`data-${string}`, string | undefined>

export function BlockView({ shape, dense, root }: { shape: BlockShape; dense?: boolean; root?: RootProps }) {
  const { className: rootClass, ...rest } = root ?? {}
  switch (shape.kind) {
    case 'h': {
      // 侧栏只有 360px 宽，一级标题按 h1 的字号会大得不像话。
      // 按层级递减但整体压扁，保住层次感就够了
      const size = dense
        ? ['text-[13px]', 'text-[12px]', 'text-[11.5px]'][Math.min(shape.level, 3) - 1]
        : ['text-base', 'text-sm', 'text-sm'][Math.min(shape.level, 3) - 1]
      return (
        <div {...rest} className={clsx('mt-3 font-semibold first:mt-0', size, rootClass)}>
          {shape.body}
        </div>
      )
    }
    case 'hr':
      return <hr {...rest} className={clsx('my-2 border-t', rootClass)} />
    case 'code':
      return <CodeBlock lang={shape.lang} code={shape.code} dense={dense} root={root}>{shape.body}</CodeBlock>
    case 'quote':
      return (
        <blockquote {...rest} className={clsx('border-l-2 pl-2.5 text-dim', rootClass)} style={{ borderColor: 'var(--accent)', ...rest.style }}>
          {shape.body}
        </blockquote>
      )
    case 'list': {
      const Tag = shape.ordered ? 'ol' : 'ul'
      // 缩进用 padding 不用 margin：列表符号画在这一段里，块上加了 content-visibility
      // （长报告）时，画在 margin 里的符号会被裁掉。序号到了两三位（「100.」）时符号比
      // 1rem 宽，fitMarkers 时按位数加宽，不然序号伸到块外面、被裁掉
      const digits = shape.ordered && shape.fitMarkers ? String((shape.start ?? 1) + shape.items.length - 1).length : 1
      return (
        <Tag {...rest} start={shape.ordered && shape.start && shape.start !== 1 ? shape.start : undefined}
             className={clsx('space-y-1', shape.ordered ? 'list-decimal' : 'list-disc',
               'pl-4 marker:text-faint', rootClass)}
             style={digits > 1 ? { paddingLeft: `calc(1rem + ${(digits - 1) * 0.6}em)`, ...rest.style } : rest.style}>
          {shape.items.map((it, i) => (
            <li key={it.key ?? i} style={{ marginLeft: it.depth * 14 }}>
              {it.body}
            </li>
          ))}
        </Tag>
      )
    }
    case 'table':
      // 数值列右对齐、等宽数字：一列金额左对齐时小数点对不上，没法一眼比大小
      return (
        <div {...rest} className={clsx('group/table relative overflow-x-auto rounded-lg border', rootClass)}>
          <CopyChip label="复制为 CSV" text={shape.csv}
                    className="absolute right-1 top-1 opacity-0 group-hover/table:opacity-100 focus-visible:opacity-100" />
          <table className={clsx('w-full', dense ? 'text-[10.5px]' : 'text-xs')}>
            <thead>
              <tr className="border-b bg-elev">
                {shape.head.map((h, i) => (
                  <th key={i} className={clsx('px-2 py-1 font-medium text-dim',
                    shape.numeric[i] ? 'text-right' : 'text-left')}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {shape.rows.map((row, i) => (
                <tr key={i} className="border-b last:border-0">
                  {row.map((cell, j) => (
                    <td key={j} className={clsx('px-2 py-1 align-top text-dim',
                      shape.numeric[j] && 'tnum text-right')}>
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    default:
      return <p {...rest} className={clsx('whitespace-pre-wrap', rootClass)}>{shape.body}</p>
  }
}

/** 哪几列是数值列：列名不像编号 / 分组，非空的格全是数 */
export function numericColumns(head: string[], rows: string[][]): boolean[] {
  return head.map((h, j) => {
    if (CODE_COLUMN.test(h.replace(/[*`]/g, '').trim())) return false
    const cells = rows.map((r) => (r[j] ?? '').replace(/[*`]/g, '').trim()).filter(Boolean)
    return cells.length > 0 && cells.every((c) => NUMERIC_CELL.test(c) && !LEADING_ZERO.test(c))
  })
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

/**
 * 行内语法：代码、链接、粗体、斜体、删除线。EvidenceDoc 的文字片段也走它——后端说
 * 不含标记的粗体、`code`、链接原样留在文字片段里，由前端按行内 Markdown 渲染
 */
export function Inline({ text, depth = 0, at }: { text: string; depth?: number; at?: At }): ReactNode {
  const marks = useContext(MarksContext)
  if (!text) return null
  const out: ReactNode[] = []
  let last = 0
  let key = 0
  // 模块级正则带 g 标志，lastIndex 是有状态的。这里会递归调用自己（粗体
  // 里可以套行内代码），不每次重置就会从上一层留下的位置接着扫
  const re = new RegExp(INLINE.source, 'g')

  const plain = (t: string, from: number) => out.push(...markText(t, marks, key++, shiftAt(at, from)))

  for (let m = re.exec(text); m; m = re.exec(text)) {
    if (m.index > last) plain(text.slice(last, m.index), last)
    const [whole, , code, linkText, href, bold1, bold2, italic, strike] = m

    // 强调的内容要再解析一层。模型很爱写 **`table_name`**（粗体里套代码），
    // 而粗体在扫描顺序上先命中，不递归的话那对反引号就原样显示出来了。
    // 深度有限：每层的文本都严格更短，且这里也卡了三层
    const sub = (t: string) =>
      depth < 3 ? <Inline text={t} depth={depth + 1} at={shiftAt(at, m!.index + whole.indexOf(t))} /> : t

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
    last = m.index + whole.length
  }
  if (last < text.length) plain(text.slice(last), last)
  return <>{out.map((n, i) => <Fragment key={i}>{n}</Fragment>)}</>
}

/** 行内样式位。EvidenceDoc 的片段按它把一段文字切成几截、分别包上 code / strong / em */
export const INLINE_BIT = { code: 1, bold: 2, em: 4, strike: 8, link: 16 } as const

export interface InlineStyles {
  /** 语法字符（反引号、星号、链接的方括号和网址）：不显示 */
  hide: Uint8Array
  /** 每个字的样式位 */
  style: Uint8Array
  /** 链接文字所在的字 → 网址。只收 http/https/mailto，别的协议当纯文本，同 Link */
  href: Map<number, string>
}

/**
 * 一段文字的行内样式，逐字给出。和 Inline 用同一个 INLINE 语法、同样最多套三层。
 *
 * 为什么不直接用 Inline：报告文档里一句话已经被后端切成了好几个片段，行内语法可能
 * 跨片段——`limit 100` 里的 100 是一个裸数字片段，两边的反引号在别的片段里。对整句
 * 算一次样式、每个片段取自己那一截，代码就还是代码，数字还能单独点
 */
export function inlineStyles(text: string): InlineStyles {
  const hide = new Uint8Array(text.length)
  const style = new Uint8Array(text.length)
  const href = new Map<number, string>()
  const walk = (from: number, to: number, inherited: number, depth: number) => {
    const part = text.slice(from, to)
    const re = new RegExp(INLINE.source, 'g')
    let last = 0
    for (let m = re.exec(part); m; m = re.exec(part)) {
      for (let i = last; i < m.index; i++) style[from + i] = inherited
      const [whole, ticks, code, linkText, url, bold1, bold2, italic, strike] = m
      const base = from + m.index
      const inner = code ?? linkText ?? bold1 ?? bold2 ?? italic ?? strike ?? ''
      // 内容紧跟在开头的定界符后面：反引号按实际个数，其余 indexOf 找到的就是第一处
      const off = code !== undefined ? ticks.length : whole.indexOf(inner)
      for (let i = 0; i < whole.length; i++) hide[base + i] = 1
      for (let i = 0; i < inner.length; i++) hide[base + off + i] = 0
      const safe = linkText !== undefined && /^(https?:|mailto:)/i.test(url.trim())
      const bit = code !== undefined ? INLINE_BIT.code
        : linkText !== undefined ? (safe ? INLINE_BIT.link : 0)
        : bold1 !== undefined || bold2 !== undefined ? INLINE_BIT.bold
        : italic !== undefined ? INLINE_BIT.em : INLINE_BIT.strike
      if (code !== undefined || linkText !== undefined || depth >= 3) {
        for (let i = 0; i < inner.length; i++) {
          style[base + off + i] = inherited | bit
          if (safe) href.set(base + off + i, url)
        }
      } else {
        walk(base + off, base + off + inner.length, inherited | bit, depth + 1)
      }
      last = m.index + whole.length
    }
    for (let i = last; i < part.length; i++) style[from + i] = inherited
  }
  walk(0, text.length, 0, 0)
  return { hide, style, href }
}

/**
 * 按 inlineStyles 渲染 [from, to) 这一截。links=false 时链接只画样子不给 <a>：片段本身
 * 是个 button，button 里不能再套可交互的元素
 */
export function StyledSlice({ text, styles, from, to, links = true }: {
  text: string; styles: InlineStyles; from: number; to: number; links?: boolean
}): ReactNode {
  const out: ReactNode[] = []
  let i = from
  while (i < to) {
    if (styles.hide[i]) { i += 1; continue }
    const st = styles.style[i]
    const href = styles.href.get(i)
    let j = i + 1
    while (j < to && !styles.hide[j] && styles.style[j] === st && styles.href.get(j) === href) j += 1
    let node: ReactNode = text.slice(i, j)
    if (st & INLINE_BIT.code) node = <code className="mono rounded bg-bg px-1 py-px text-[0.92em]">{node}</code>
    if (st & INLINE_BIT.link) {
      node = links && href
        ? <a href={href} target="_blank" rel="noopener noreferrer" className="underline decoration-dotted underline-offset-2"
             style={{ color: 'var(--accent)' }}>{node}</a>
        : <span style={{ color: 'var(--accent)' }}>{node}</span>
    }
    if (st & INLINE_BIT.strike) node = <s className="text-faint">{node}</s>
    if (st & INLINE_BIT.em) node = <em>{node}</em>
    if (st & INLINE_BIT.bold) node = <strong className="font-semibold">{node}</strong>
    out.push(<Fragment key={i}>{node}</Fragment>)
    i = j
  }
  return <>{out}</>
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
function CodeBlock({ lang, code, dense, root, children }: {
  lang: ReactNode; code: string; dense?: boolean; root?: RootProps
  /** 要显示的内容，不给就是 code 原文（EvidenceDoc 给的是片段，代码里的裸数字要画线） */
  children?: ReactNode
}) {
  const { className: rootClass, ...rest } = root ?? {}
  return (
    <div {...rest} className={clsx('group/code relative overflow-hidden rounded-lg border bg-bg', rootClass)}>
      <div className="flex items-center gap-2 border-b px-2.5 py-1 text-2xs text-faint">
        <span className="mono">{lang || '代码'}</span>
        <span className="flex-1" />
        <CopyChip label="复制" text={() => code} />
      </div>
      <pre className={clsx('mono overflow-x-auto px-2.5 py-2 leading-relaxed', dense ? 'text-[10.5px]' : 'text-xs')}>
        <code>{children ?? code}</code>
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
