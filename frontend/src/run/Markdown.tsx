import { Fragment, type ReactNode } from 'react'
import clsx from 'clsx'

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
export function Markdown({ text, dense }: { text: string; dense?: boolean }) {
  if (!text?.trim()) return null
  return (
    <div className={clsx('space-y-2 leading-relaxed', dense ? 'text-[11.5px]' : 'text-[12.5px]')}>
      {parseBlocks(text).map((b, i) => <Block key={i} block={b} dense={dense} />)}
    </div>
  )
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
  | { kind: 'list'; ordered: boolean; items: { text: string; depth: number }[] }
  | { kind: 'table'; head: string[]; rows: string[][] }

const FENCE = /^\s*```(\w*)\s*$/
const HEADING = /^(#{1,6})\s+(.*)$/
const HR = /^\s*(-{3,}|\*{3,}|_{3,})\s*$/
const BULLET = /^(\s*)[-*+]\s+(.*)$/
const ORDERED = /^(\s*)\d+[.)]\s+(.*)$/
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
        const m = lines[i].match(BULLET) || lines[i].match(ORDERED)
        if (m) {
          items.push({ text: m[2], depth: Math.floor(m[1].replace(/\t/g, '  ').length / 2) })
          i += 1
        } else if (lines[i].trim() && !isBlockStart(lines[i]) && items.length) {
          // 悬挂缩进的续行接到上一条上，不要另起一段
          items[items.length - 1].text += '\n' + lines[i].trim()
          i += 1
        } else break
      }
      out.push({ kind: 'list', ordered: isOrdered, items })
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
      const size = ['text-[14px]', 'text-[13px]', 'text-[12.5px]'][Math.min(block.level, 3) - 1]
      return (
        <div className={clsx('mt-3 font-semibold first:mt-0', size)}>
          <Inline text={block.text} />
        </div>
      )
    }
    case 'hr':
      return <hr className="my-2 border-t" />
    case 'code':
      return (
        <pre className="mono overflow-x-auto rounded-lg border bg-bg px-2.5 py-2 text-[10.5px] leading-relaxed">
          <code>{block.code}</code>
        </pre>
      )
    case 'quote':
      return (
        <blockquote className="border-l-2 pl-2.5 text-dim" style={{ borderColor: 'var(--accent)' }}>
          <Inline text={block.text} />
        </blockquote>
      )
    case 'list': {
      const Tag = block.ordered ? 'ol' : 'ul'
      return (
        <Tag className={clsx('space-y-1', block.ordered ? 'list-decimal' : 'list-disc',
          'ml-4 marker:text-faint')}>
          {block.items.map((it, i) => (
            <li key={i} style={{ marginLeft: it.depth * 14 }}>
              <Inline text={it.text} />
            </li>
          ))}
        </Tag>
      )
    }
    case 'table':
      return (
        <div className="overflow-x-auto rounded-lg border">
          <table className={clsx('w-full', dense ? 'text-[10.5px]' : 'text-[11px]')}>
            <thead>
              <tr className="border-b bg-elev">
                {block.head.map((h, i) => (
                  <th key={i} className="px-2 py-1 text-left font-medium text-faint">
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
                    <td key={j} className="px-2 py-1 align-top text-dim">
                      <Inline text={row[j] ?? ''} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
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

  for (let m = re.exec(text); m; m = re.exec(text)) {
    if (m.index > last) out.push(text.slice(last, m.index))
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
  if (last < text.length) out.push(text.slice(last))
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
