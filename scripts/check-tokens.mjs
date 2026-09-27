// 设计令牌的静态检查：只读扫 frontend/src，不启浏览器、不连后端。
//
//   node scripts/check-tokens.mjs
//
// 守的是这几件事：
//   1. 源码里用到的每个 var(--x)，都得有地方定义它——index.css（以及 src 下其余 .css）
//      里的 `--x:`，或者运行时写进去的（style 里的 '--x'、setProperty('--x', …)）。
//      写错一个字母，浏览器不报错，只是那条规则静悄悄失效：颜色掉回继承值、间距变 0；
//   2. 兼容别名 --hover、--canvas-dot 已经删掉，不许再有人用；
//   3. 原生 confirm / prompt / alert 只留在 components/ui.tsx 里没挂 DialogHost 时的兜底。
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

const root = new URL('../frontend/src/', import.meta.url).pathname

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(css|tsx?|mjs)$/.test(name)) out.push(p)
  }
  return out
}

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}

const files = walk(root)
// 注释里提到的 var(--x) 不算用法（说明文字常常写「以前用 var(--hover)」）
// 换行留着，报出来的行号才对得上
const blank = (m) => m.replace(/[^\n]/g, '')
const stripComments = (src, css) => (css
  ? src.replace(/\/\*[\s\S]*?\*\//g, blank)
  : src.replace(/\/\*[\s\S]*?\*\//g, blank).replace(/(^|[^:'"`\\])\/\/.*$/gm, '$1'))

const defined = new Set()
const uses = new Map() // name -> [{file, line, fallback}]
const families = new Map() // var(--nt-${type}) 这种按前缀拼出来的：前缀 -> [位置]
for (const file of files) {
  const css = file.endsWith('.css')
  const src = stripComments(readFileSync(file, 'utf8'), css)
  const rel = relative(root, file)
  if (css) {
    for (const m of src.matchAll(/(--[\w-]+)\s*:/g)) defined.add(m[1])
  } else {
    // 运行时写上去的：style={{ '--rank': … }}、el.style.setProperty('--zoom', …)
    for (const m of src.matchAll(/['"`](--[\w-]+)['"`]\s*(?:as\s+string\s*)?[:,)\]]/g)) defined.add(m[1])
  }
  const lines = src.split('\n')
  lines.forEach((line, i) => {
    for (const m of line.matchAll(/var\(\s*(--[\w-]+)(\$\{)?\s*(,)?/g)) {
      const at = { file: rel, line: i + 1, fallback: !!m[3] }
      const book = m[2] ? families : uses
      if (!book.has(m[1])) book.set(m[1], [])
      book.get(m[1]).push(at)
    }
  })
}

console.log('=== 用到的 var(--x) 都有定义 ===')
const missing = [...uses.entries()].filter(([name]) => !defined.has(name))
const hard = missing.filter(([, at]) => at.some((u) => !u.fallback))
const soft = missing.filter(([, at]) => at.every((u) => u.fallback))
check(`${uses.size} 个令牌，没有一处引用了未定义的（不带回落值）`, hard.length === 0,
  hard.map(([n, at]) => `${n} @ ${at.filter((u) => !u.fallback).map((u) => `${u.file}:${u.line}`).join(', ')}`).join(' ｜ '))
check('带回落值的也都有定义（回落值只是保险，不该成为唯一的来源）', soft.length === 0,
  soft.map(([n, at]) => `${n} @ ${at.map((u) => `${u.file}:${u.line}`).join(', ')}`).join(' ｜ '))

const orphan = [...families.entries()].filter(([prefix]) => ![...defined].some((d) => d.startsWith(prefix)))
check(`按前缀拼出来的 ${families.size} 族（${[...families.keys()].join('、')}）都有定义`, orphan.length === 0,
  orphan.map(([p, at]) => `${p}* @ ${at.map((u) => `${u.file}:${u.line}`).join(', ')}`).join(' ｜ '))
// 节点类型色一种都不能少：少一个，那种卡片的类型条就掉回继承色
const nodeTypes = [...readFileSync(join(root, 'types.ts'), 'utf8')
  .match(/export type NodeType =([^]*?)\n\n/)?.[1].matchAll(/'(\w+)'/g) ?? []].map((m) => m[1])
const noColor = nodeTypes.filter((t) => !defined.has(`--nt-${t}`))
check(`${nodeTypes.length} 种节点类型都有 --nt-<类型>`, nodeTypes.length > 10 && noColor.length === 0, noColor.join('、'))

console.log('\n=== 兼容别名已经删掉，没人再用 ===')
for (const alias of ['--hover', '--canvas-dot']) {
  const at = uses.get(alias) ?? []
  check(`${alias} 没有调用方`, at.length === 0, at.map((u) => `${u.file}:${u.line}`).join(', '))
  check(`${alias} 不再定义`, !defined.has(alias))
}

console.log('\n=== 原生对话框只留在 ui.tsx 的兜底里 ===')
const native = []
for (const file of files) {
  if (file.endsWith('.css')) continue
  const rel = relative(root, file)
  const src = stripComments(readFileSync(file, 'utf8'), false)
  // 文件里自己声明的同名函数（DialogHost 里的 const confirm = …）调的是它，不是原生的
  const own = new Set([...src.matchAll(/(?:function|const|let)\s+(confirm|prompt|alert)\b/g)].map((m) => m[1]))
  src.split('\n').forEach((line, i) => {
    // 字符串里的（预览页的 XSS 样例 '<script>alert(1)</script>'）不算调用
    const code = line.replace(/(['"`])(?:\\.|(?!\1).)*\1/g, '""')
    for (const m of code.matchAll(/(^|[^\w.$])(window\.)?(confirm|prompt|alert)\s*\(/g)) {
      if (!m[2] && own.has(m[3])) continue
      if (/(?:function|const|let)\s+$/.test(code.slice(0, m.index + m[1].length))) continue
      native.push({ rel, line: i + 1, code: code.trim() })
    }
  })
}
const outside = native.filter((n) => !(n.rel === 'components/ui.tsx' && /window\.(confirm|prompt)\(/.test(n.code)))
check('components/ui.tsx 之外没有 confirm / prompt / alert', outside.length === 0,
  outside.map((n) => `${n.rel}:${n.line}`).join(', '))

console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 令牌全部通过')
process.exit(failed ? 1 : 0)
