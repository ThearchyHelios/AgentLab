/**
 * 两条 SQL 逐词对比：并排显示时标出各自独有的部分（结构变了的那条查询，差在哪一眼能看到）。
 *
 * 按词（名字、关键字、数、字符串、运算符、注释）而不是按字符比：按字符比会把 created_at 和 paid_at 里碰巧相同的「_at」
 * 当成没变，读起来支离破碎；按行比又太粗，模型写的 SQL 常常一整条写在一行里。关键字和不加引号的名字不分大小写
 * （服务端的规范化也不算它们不同），字符串、加引号的名字照原样比。空白不参与比较，只是两个变了的词之间的空白也
 * 跟着标，免得一段新加的条件被切成好几截。
 *
 * 先去掉两头相同的部分，中间再求最长公共子序列。中间太长（两边词数之积超过 CELL_LIMIT）时不逐词算，整段标出——
 * 页面不能为一条超长的 SQL 卡住，coarse 为 true，界面照实说只标了大致范围。
 */

export interface DiffPart {
  text: string
  /** 这一段只在自己这边有 */
  changed: boolean
}

export interface SqlDiff {
  base: DiffPart[]
  target: DiffPart[]
  /** 中间太长、没有逐词比，整段标出 */
  coarse: boolean
}

/** 逐词比较时动态规划表的格数上限（Uint32Array，4 MB） */
const CELL_LIMIT = 1_000_000

// 空白、注释（-- 到行尾、/* … */，没收尾的到末尾）、字符串（'' 转义）、加引号的名字（"…"、`…`、[…]）、名字和关键字、
// 数、两个字符的运算符、其余单个字符。注释整段算一个词，而且排在字符串前面：模型写的英文注释里常有 it's、user's，
// 把里面的撇号当成字符串的开头，会一直吞到下一个引号，后面没改的语句全被标成不同
const TOKEN = /\s+|--[^\n]*|\/\*[\s\S]*?(?:\*\/|$)|'(?:[^']|'')*'?|"(?:[^"]|"")*"?|`[^`]*`?|\[[^\]\s]*\]?|[\p{L}_][\p{L}\p{N}_$]*|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|<=|>=|<>|!=|\|\||::|[\s\S]/gu

interface Token {
  text: string
  space: boolean
  /** 比较用的写法 */
  key: string
}

export function tokenize(sql: string): Token[] {
  const out: Token[] = []
  for (const m of sql.matchAll(TOKEN)) {
    const text = m[0]
    const space = /^\s+$/.test(text)
    const word = /^[\p{L}_]/u.test(text)
    out.push({ text, space, key: word ? text.toLowerCase() : text })
  }
  return out
}

/** 非空白的词里，哪些和对面配上了（最长公共子序列）。返回两个布尔数组：true 是配上了 */
function matchWords(a: string[], b: string[]): { a: boolean[]; b: boolean[]; coarse: boolean } {
  const ma = a.map(() => false)
  const mb = b.map(() => false)
  let lo = 0
  while (lo < a.length && lo < b.length && a[lo] === b[lo]) { ma[lo] = true; mb[lo] = true; lo++ }
  let ea = a.length
  let eb = b.length
  while (ea > lo && eb > lo && a[ea - 1] === b[eb - 1]) { ea--; eb--; ma[ea] = true; mb[eb] = true }
  const n = ea - lo
  const m = eb - lo
  if (n === 0 || m === 0) return { a: ma, b: mb, coarse: false }
  if ((n + 1) * (m + 1) > CELL_LIMIT) return { a: ma, b: mb, coarse: true }
  // dp[i][j]：a 从 lo+i、b 从 lo+j 起到中段末尾的最长公共子序列长度，从后往前填，回溯时从前往后走
  const w = m + 1
  const dp = new Uint32Array((n + 1) * w)
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i * w + j] = a[lo + i] === b[lo + j]
        ? dp[(i + 1) * w + j + 1] + 1
        : Math.max(dp[(i + 1) * w + j], dp[i * w + j + 1])
    }
  }
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (a[lo + i] === b[lo + j]) {
      ma[lo + i] = true
      mb[lo + j] = true
      i++
      j++
    } else if (dp[(i + 1) * w + j] >= dp[i * w + j + 1]) {
      i++
    } else {
      j++
    }
  }
  return { a: ma, b: mb, coarse: false }
}

/** 词和配对结果 → 一段段文字。夹在两个变了的词之间的空白也算变了，相邻同类的段并成一段 */
function parts(tokens: Token[], matched: boolean[]): DiffPart[] {
  const flags: (boolean | null)[] = []
  let w = 0
  for (const t of tokens) flags.push(t.space ? null : !matched[w++])
  // 每个位置往后最近的一个词变没变（空白看前后两个词），从后往前扫一遍，不在每个空白处再各找一次
  const next: boolean[] = new Array(tokens.length).fill(false)
  let ahead = false
  for (let i = tokens.length - 1; i >= 0; i--) {
    next[i] = ahead
    if (flags[i] !== null) ahead = flags[i] as boolean
  }
  const out: DiffPart[] = []
  let behind = false
  tokens.forEach((t, i) => {
    const flag = flags[i]
    const changed = flag ?? (behind && next[i])
    if (flag !== null) behind = flag
    const last = out[out.length - 1]
    if (last && last.changed === changed) last.text += t.text
    else out.push({ text: t.text, changed })
  })
  return out
}

export function diffSql(base: string, target: string): SqlDiff {
  const tb = tokenize(base)
  const tt = tokenize(target)
  const words = (ts: Token[]) => ts.filter((t) => !t.space).map((t) => t.key)
  const { a, b, coarse } = matchWords(words(tb), words(tt))
  return { base: parts(tb, a), target: parts(tt, b), coarse }
}
