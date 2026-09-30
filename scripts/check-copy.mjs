// 界面文案防回潮：静态扫描前后端会上界面的字符串，不开浏览器、不连后端。
//
//   node scripts/check-copy.mjs            扫一遍，命中禁用写法（且不在白名单里）的报出来
//   node scripts/check-copy.mjs --list     列出全部命中，含被白名单放过的（调白名单时用）
//
// 守的是 2026-09 文案整改定下的写法（术语表和语体规范见 frontend/src/lib/terms.ts 开头）：
//   - 「跑」作动词（工作流写「运行」，节点、工具写「执行」）、后端（写「服务端」）、多半、照样、眼下、压根、
//     取不到、没取回来、读不懂、连不上、写坏了（写「无法 / 未能 / 失败」）、
//     令牌 / tok / tokens（写「token」）、钉住（写「固定版本」「引用版本」）、智能体（写「Agent」）、
//     供应商（写「模型接入」）、界面上的 Copilot（写「助手」）；
//   - 句尾语气词（吧、呢、哦……）；
//   - 句子里裸写的配置键名和枚举值（report_from、require_citation、cells: true……），要写界面上的中文标签；
//   - 中文句子里的半角标点、英文引号和「...」（用全角标点、「」和「…」）。
//
// 扫哪些字符串：
//   - 前端：frontend/src 下的 .ts / .tsx，不含 __tests__、dev/、.d.ts。用 vite 自带的解析器（rolldown 的
//     oxc）读成语法树，取字符串字面量、模板字符串和 JSX 文本，注释天然不在里面。按原文匹配用的字符串
//     （includes / startsWith / === / new RegExp 的参数、case 标签、对象的键、import 路径、类型里的字面量、
//     console 输出）不算界面文案，跳过。
//   - 后端：backend/app 下的 .py，用 Python 的 ast 读，取带中文的字符串和 f-string（docstring、日志、
//     按原文比较和正则匹配用的跳过）。写给模型的提示词不上界面，按常量名、函数名排除（见 MODEL_ONLY）。
//
// 确实该保留的写在 scripts/copy-allow.json：每条写明文件、原文片段、规则和理由。白名单里有一条
// 已经对不上任何命中（原文改掉了、挪走了），也算不通过：白名单只收现在还需要的例外。
import { spawnSync } from 'node:child_process'
import { existsSync, readdirSync, readFileSync, realpathSync, statSync } from 'node:fs'
import { createRequire } from 'node:module'
import { homedir } from 'node:os'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = dirname(HERE)
const FRONT = join(ROOT, 'frontend/src')
const BACK = join(ROOT, 'backend/app')
const LIST = process.argv.includes('--list')

let failed = 0
const check = (name, cond, detail = '') => {
  console.log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ` — ${detail}` : ''}`)
  if (!cond) failed++
}

// ---------------------------------------------------------------- 规则

const CJK = /[㐀-鿿]/
const H = '[\\u3400-\\u9fff]'
// 句子里不该裸写的配置键名和枚举值（配置界面字段旁边附的键名是单独的字符串，不带中文，不会命中）
const KEYS = ['report_from', 'require_citation', 'on_violation', 'on_uncited', 'on_unsupported', 'on_exhausted',
  'allow_numbers', 'caliber_from', 'metrics_from', 'cite_fields', 'upgrade_policy', 'evidence_role', 'max_repairs',
  'max_rounds', 'skip_if', 'budget_tokens', 'workflow_version', 'output_schema']
// 前面是「{」的是 str.format 的占位符（{budget_tokens:,}），不算
const KEY_RE = new RegExp(`(?<![A-Za-z0-9_.${'$'}{])(?:${KEYS.join('|')})(?![A-Za-z0-9_])`
  + '|cells\\s*:\\s*true|numbers\\s*:\\s*(?:strict|off|flag)\\b|claims\\s*:\\s*(?:off|judge|require_citation)\\b')

/**
 * 每条规则：id（白名单按它对）、说明、test(text, prose) → 命中的片段或 null。
 * prose：这段文字带中文，或者是 JSX 文本——只有这样的才按「句子」查英文写法。
 */
const RULES = [
  { id: '跑', what: '「跑」作动词：工作流写「运行」，节点、工具写「执行」', test: (t) => t.match(/.{0,6}跑.{0,6}/)?.[0] },
  { id: '后端', what: '「后端」：写「服务端」，连接类写「无法连接服务」', test: (t) => t.match(/.{0,6}后端.{0,6}/)?.[0] },
  {
    id: '口语', what: '口语副词和补语：多半、照样、眼下、压根；取不到、没取回来、读不懂、连不上、写坏了',
    test: (t) => t.match(/.{0,6}(?:多半|照样|眼下|压根|取不到|没取回来|读不懂|连不上|写坏了).{0,6}/)?.[0],
  },
  { id: '令牌', what: '「令牌」：写「token」', test: (t) => t.match(/.{0,6}令牌.{0,6}/)?.[0] },
  {
    id: 'tok', what: '「tok」「tokens」：写「token」（如「2.8k token」）',
    test: (t, prose) => (prose
      ? t.match(/.{0,8}(?<![A-Za-z_])(?:toks?|tokens)(?![A-Za-z_]).{0,4}/)?.[0]
      : t.match(/(?:\d|\{x\})\s*(?:toks?|tokens)(?![A-Za-z_])/)?.[0]),
  },
  { id: '钉', what: '「钉住」「钉」：写「固定版本」「引用版本」', test: (t) => t.match(/.{0,6}钉.{0,6}/)?.[0] },
  { id: '智能体', what: '「智能体」：写「Agent」', test: (t) => t.match(/.{0,6}智能体.{0,6}/)?.[0] },
  { id: '供应商', what: '「供应商」：写「模型接入」', test: (t) => t.match(/.{0,6}供应商.{0,6}/)?.[0] },
  {
    id: 'Copilot', what: '界面上的「Copilot」：写「助手」',
    test: (t, prose) => (prose ? t.match(/.{0,6}copilot.{0,6}/i)?.[0] : t.match(/.{0,6}Copilot.{0,6}/)?.[0]),
  },
  {
    id: '语气词', what: '句尾语气词（吧、呢、哦、啊、呀、嘛、啦）',
    test: (t) => t.match(new RegExp(`.{0,6}${H}[吧呢哦啊呀嘛啦](?=$|[\\s。！？!?，,；;：:」』）)\\]…—])`))?.[0],
  },
  {
    id: '键名', what: '句子里裸写配置键名或枚举值：写界面上的中文标签（如「报告来自」「单元格引用」）',
    test: (t, prose) => (prose && CJK.test(t) ? t.match(KEY_RE)?.[0] : null),
  },
  {
    id: '半角标点', what: '中文句子里的半角标点、英文引号或「...」：用全角标点、「」和「…」',
    test: (t, prose) => {
      if (!prose) return null
      const m = t.match(new RegExp(`.{0,4}(?:${H}[,;!?]|[,;!?]${H}|${H}:(?![/\\d])|:${H}|${H}\\(|\\)${H}`
        + `|${H}"|"${H}|[“”]|\\.\\.\\.).{0,4}`))
      return m?.[0] ?? null
    },
  },
]

/** 一段文字命中了哪些规则 → [{ rule, snippet }] */
function lint(text, prose) {
  const out = []
  for (const r of RULES) {
    const hit = r.test(text, prose)
    if (hit) out.push({ rule: r.id, snippet: hit })
  }
  return out
}

// ---------------------------------------------------------------- 前端：取界面字符串

// pnpm 的依赖是软链：从 vite 的真实路径出发才解析得到它的依赖 rolldown
const require = createRequire(realpathSync(join(ROOT, 'frontend/node_modules/vite/package.json')))
const { parseAst } = await import(pathToFileURL(require.resolve('rolldown/parseAst')).href)

// 这些方法的参数是「拿来匹配的原文」，不是显示给人的
const MATCH_METHODS = new Set(['includes', 'startsWith', 'endsWith', 'indexOf', 'lastIndexOf', 'match', 'matchAll',
  'search', 'split', 'test'])
const FIRST_ARG_METHODS = new Set(['replace', 'replaceAll'])
// 不上界面的 JSX 属性
const HIDDEN_ATTR = /^(?:className|class|style|key|id|role|type|href|to|src|htmlFor|name|value|d|viewBox|fill|stroke|data-.+|aria-hidden|aria-controls|aria-labelledby|aria-describedby)$/

function memberName(n) {
  if (!n) return ''
  if (n.type === 'Identifier') return n.name
  if (n.type === 'MemberExpression' && !n.computed) return `${memberName(n.object)}.${n.property.name}`
  return ''
}

/** 这个字面量是不是「按原文匹配 / 代码里的键」而不是界面文字 */
function skipLiteral(node, parent, key) {
  if (!parent) return false
  switch (parent.type) {
    case 'ImportDeclaration': case 'ExportNamedDeclaration': case 'ExportAllDeclaration': case 'ImportExpression':
    case 'TSLiteralType': case 'TSExternalModuleReference': case 'TSImportType':
      return true
    case 'Property': case 'PropertyDefinition': case 'MethodDefinition': case 'TSPropertySignature':
      return key === 'key' && !parent.computed
    case 'BinaryExpression':
      return ['===', '!==', '==', '!=', 'in'].includes(parent.operator)
    case 'SwitchCase':
      return key === 'test'
    case 'JSXAttribute':
      return HIDDEN_ATTR.test(parent.name?.name ?? '')
    case 'NewExpression':
    case 'CallExpression': {
      if (key !== 'arguments') return false
      const callee = parent.callee
      if (callee.type === 'Identifier' && callee.name === 'RegExp') return true
      if (callee.type === 'MemberExpression' && !callee.computed) {
        const m = callee.property.name
        if (MATCH_METHODS.has(m)) return true
        if (FIRST_ARG_METHODS.has(m) && parent.arguments[0] === node) return true
        if (memberName(callee.object) === 'console') return true
      }
      return false
    }
    default:
      return false
  }
}

function lineOf(src) {
  const starts = [0]
  for (let i = 0; i < src.length; i++) if (src.charCodeAt(i) === 10) starts.push(i + 1)
  return (pos) => {
    let lo = 0, hi = starts.length - 1
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1
      if (starts[mid] <= pos) lo = mid; else hi = mid - 1
    }
    return lo + 1
  }
}

/** 一段 TS / TSX 源码里会上界面的字符串 → [{ line, text, prose }] */
export function frontStrings(src, lang = 'tsx') {
  const ast = parseAst(src, { lang })
  const at = lineOf(src)
  const out = []
  const visit = (node, parent, key) => {
    if (!node || typeof node.type !== 'string') return
    if (node.type === 'Literal' && typeof node.value === 'string') {
      if (!skipLiteral(node, parent, key)) out.push({ line: at(node.start), text: node.value, prose: CJK.test(node.value) })
      return
    }
    if (node.type === 'TemplateLiteral') {
      if (parent?.type !== 'TaggedTemplateExpression' && !skipLiteral(node, parent, key)) {
        const text = node.quasis.map((q) => q.value.cooked ?? q.value.raw).join('{x}')
        out.push({ line: at(node.start), text, prose: CJK.test(text) })
      }
      for (const e of node.expressions) visit(e, node, 'expressions')
      return
    }
    if (node.type === 'JSXText') {
      const text = node.value.replace(/\s+/g, ' ').trim()
      if (text) out.push({ line: at(node.start), text, prose: true })
      return
    }
    for (const k of Object.keys(node)) {
      if (k === 'parent') continue
      const v = node[k]
      if (Array.isArray(v)) for (const c of v) visit(c, node, k)
      else if (v && typeof v === 'object' && typeof v.type === 'string') visit(v, node, k)
    }
  }
  visit(ast, null, null)
  return out
}

function walk(dir, keep, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, keep, out)
    else if (keep(p)) out.push(p)
  }
  return out
}

const frontFiles = walk(FRONT, (p) => /\.tsx?$/.test(p) && !/\.d\.ts$/.test(p)
  && !/\/__tests__\//.test(p) && !/\/dev\//.test(p) && !/\.test\.tsx?$/.test(p))

// ---------------------------------------------------------------- 后端：取界面字符串

// 写给模型的提示词：不上界面（backend-surfaced 分片按要求未改）。按赋值的常量名、所在函数名排除。
// 既给人看又交回模型的，给人看的那句单独存在（for_model、_UNKNOWN_ENTITY_FOR_MODEL 是模型那份）
const MODEL_ONLY = {
  // 模块级常量：系统提示、输出协议、写作 / 裁判 / 复核规则、交给模型的 JSON Schema、正则片段
  names: new Set([
    'NODE_REFERENCE', 'ROLE', 'MARKUP_NUDGE', 'JUDGE_RULES', 'JUDGE_ROLE', 'JUDGE_RULE', 'REVIEW_SYSTEM', 'MARKER_RULES',
    'GATE_SYSTEM', 'TOOL_MARKUP_NUDGE', '_UNKNOWN_ENTITY_FOR_MODEL', 'NODE_FIELD_FIX', '_FIX_TAIL',
    '_STREAM_PROTOCOL', '_RULES_HEAD', '_NO_LOWERING', '_ASK_HUMAN', '_UPGRADE_TASK', 'CELL_RULES', 'ENTITY_RULES',
    'EXTRACT_SYSTEM', 'GRAPH_SCHEMA', 'REVIEW_SCHEMA', '_CN_N',
  ]),
  // 拼提示词的函数：生成 / 修正 / 升级请求、抽取消息、交给抽取模型的输出结构
  funcs: new Set(['_user_message', '_repair_request', '_publish_fix_request', '_upgrade_assist_request',
    '_extract_messages', '_from_rows', 'catalog_prompt']),
  // 第二个参数是交回模型改写的指令（第一个是给人看的那句）：engine/evidence.py 的 _Reason(text, fix)、fail(text, fix)
  fixArg: new Set(['_Reason', 'fail']),
  names_re: /(?:^|_)(?:SYSTEM|PROMPT|NUDGE)(?:_|$)/,
}

const PY_EXTRACT = String.raw`
import ast, json, os, re, sys
CJK = re.compile(r'[㐀-鿿]')
root = sys.argv[1]
only = sys.argv[2:]  # 给了就只读这几个文件（自检用）
out = []
def dotted(n):
    if isinstance(n, ast.Name): return n.id
    if isinstance(n, ast.Attribute):
        d = dotted(n.value)
        return (d + '.' if d else '') + n.attr
    return ''
def scan(path, rel):
    src = open(path, encoding='utf-8').read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._p = node
    skip = set()
    for node in ast.walk(tree):
        # docstring 和当注释用的裸字符串
        if isinstance(node, ast.Expr) and isinstance(node.value, (ast.Constant, ast.JoinedStr)):
            skip.add(id(node.value))
    def ctx(node):
        funcs, assign, call, kw, arg, compare, matcher, logger, dictkey = [], None, None, None, None, False, False, False, None
        cur, prev = node, None
        while hasattr(cur, '_p'):
            prev, cur = cur, cur._p
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                funcs.append(cur.name)
            elif isinstance(cur, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and assign is None:
                targets = cur.targets if isinstance(cur, ast.Assign) else [cur.target]
                for t in targets:
                    name = dotted(t) or (dotted(t.value) if isinstance(t, ast.Subscript) else '')
                    if name:
                        assign = name.split('.')[-1]
                        break
            elif isinstance(cur, ast.Compare) and call is None:
                compare = True
            elif isinstance(cur, ast.Dict) and dictkey is None and prev in cur.values:
                k = cur.keys[cur.values.index(prev)]
                if isinstance(k, ast.Constant) and isinstance(k.value, str): dictkey = k.value
            elif isinstance(cur, ast.keyword) and kw is None and call is None:
                kw = cur.arg
            elif isinstance(cur, ast.Call) and call is None:
                call = dotted(cur.func)
                if prev in cur.args: arg = cur.args.index(prev)
                last = call.split('.')[-1]
                head = call.split('.')[0]
                if last in ('startswith', 'endswith', 'find', 'rfind', 'index', 'count', 'split', 'rsplit',
                            'partition', 'removeprefix', 'removesuffix', 'strip', 'lstrip', 'rstrip') \
                        or (head == 're' and last in ('compile', 'search', 'match', 'fullmatch', 'sub', 'subn',
                                                       'findall', 'finditer', 'split')) \
                        or (last == 'replace' and cur.args and prev is cur.args[0]):
                    matcher = True
                if head in ('logger', 'log', '_log', 'LOG', 'logging', 'warnings') or last in ('debug', 'exception'):
                    logger = True
        return dict(funcs=funcs[::-1], assign=assign, call=call, kw=kw, arg=arg, compare=compare, matcher=matcher,
                    logger=logger, dictkey=dictkey)
    for node in ast.walk(tree):
        if id(node) in skip: continue
        if isinstance(node, ast.JoinedStr):
            text = ''.join(str(v.value) if isinstance(v, ast.Constant) else '{x}' for v in node.values)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if isinstance(getattr(node, '_p', None), (ast.JoinedStr, ast.FormattedValue)): continue
            text = node.value
        else:
            continue
        if not CJK.search(text) and 'Copilot' not in text: continue
        out.append(dict(file=rel, line=node.lineno, text=text, **ctx(node)))
files = []
if only:
    files = [(p, os.path.basename(p)) for p in only]
else:
    for d, _, fs in os.walk(root):
        for f in sorted(fs):
            if f.endswith('.py'):
                p = os.path.join(d, f)
                files.append((p, os.path.relpath(p, root)))
for p, rel in files:
    scan(p, rel)
print(json.dumps(out, ensure_ascii=False))
`

const PYTHON = [process.env.CHECK_PYTHON, join(homedir(), 'miniforge3/envs/agentlab/bin/python'), 'python3']
  .find((p) => p && (p === 'python3' || existsSync(p)))

function backStrings(root, only = []) {
  const r = spawnSync(PYTHON, ['-c', PY_EXTRACT, root, ...only], { encoding: 'utf8', maxBuffer: 256 << 20 })
  if (r.status !== 0) throw new Error(`读后端源码失败：${r.stderr || r.error}`)
  return JSON.parse(r.stdout)
}

/** 后端字符串是不是会上界面的那种（不是日志、不是匹配用的原文、不是写给模型的提示词） */
function backShown(s) {
  if (s.logger || s.matcher || s.compare) return false
  if (s.assign && (MODEL_ONLY.names.has(s.assign) || MODEL_ONLY.names_re.test(s.assign))) return false
  if (s.call && MODEL_ONLY.fixArg.has(s.call.split('.').pop()) && s.arg === 1) return false
  // 接口参数说明只进 OpenAPI 文档，不上界面
  if (s.call && /(?:^|\.)(?:Query|Path|Body|Header)$/.test(s.call) && s.kw === 'description') return false
  if (s.funcs.some((f) => MODEL_ONLY.funcs.has(f))) return false
  if (s.kw === 'fix' || s.kw === 'for_model' || s.dictkey === 'for_model') return false
  return true
}

// ---------------------------------------------------------------- 汇总

const hits = []
for (const file of frontFiles) {
  const rel = `frontend/src/${relative(FRONT, file)}`
  const src = readFileSync(file, 'utf8')
  let strings
  try {
    strings = frontStrings(src, file.endsWith('.tsx') ? 'tsx' : 'ts')
  } catch (e) {
    check(`读得懂 ${rel}`, false, String(e?.message ?? e).slice(0, 200))
    continue
  }
  for (const s of strings) for (const h of lint(s.text, s.prose)) hits.push({ file: rel, line: s.line, text: s.text, ...h })
}
const backAll = backStrings(BACK)
const backShownList = backAll.filter(backShown)
for (const s of backShownList) {
  const where = [s.funcs.join('.'), s.assign && `= ${s.assign}`, s.call && `${s.call}(${s.kw ? `${s.kw}=` : ''})`,
    s.dictkey && `{${s.dictkey}}`].filter(Boolean).join(' ')
  for (const h of lint(s.text, true)) hits.push({ file: `backend/app/${s.file}`, line: s.line, text: s.text, where, ...h })
}

const ALLOW_PATH = join(HERE, 'copy-allow.json')
const allow = JSON.parse(readFileSync(ALLOW_PATH, 'utf8')).entries
const used = new Set()
const allowed = (h) => allow.findIndex((a, i) => {
  const ok = a.file === h.file && (a.rule === h.rule || a.rule === '*') && h.text.includes(a.text)
  if (ok) used.add(i)
  return ok
}) >= 0
const bad = hits.filter((h) => !allowed(h))

console.log('=== 扫描范围 ===')
check(`前端 ${frontFiles.length} 个文件都读得懂`, frontFiles.length > 60)
check(`后端取到 ${backAll.length} 条带中文的字符串，其中 ${backShownList.length} 条按界面文案查`,
  backAll.length > 1000 && backShownList.length > 500)

console.log('\n=== 禁用写法 ===')
for (const r of RULES) {
  const mine = bad.filter((h) => h.rule === r.id)
  check(`${r.what}：没有`, mine.length === 0,
    mine.slice(0, 40).map((h) => `${h.file}:${h.line}「${h.snippet}」`).join(' ｜ ')
      + (mine.length > 40 ? ` ｜ ……另有 ${mine.length - 40} 处` : ''))
}

console.log('\n=== 给模型的改写指令（for_model）不上界面 ===')
// 后端在报告核对的 violations、助手自查的问题里另带 for_model：交回模型用的原话。前端在 api/client.ts 的
// parseBody 里一进来就去掉，别处不该再碰它——原始事件、工件原文这些直接显示 JSON 的地方才不会露出来
{
  const client = join(FRONT, 'api/client.ts')
  const readers = frontFiles.filter((f) => f !== client && readFileSync(f, 'utf8').includes('for_model'))
  check('前端只有 api/client.ts 提到 for_model', readers.length === 0, readers.map((f) => relative(ROOT, f)).join('、'))
  const code = readFileSync(client, 'utf8')
  check('api/client.ts：响应体一律经 parseBody 解析（不直接 res.json()），REST、上传、助手流、运行事件流四个入口都在',
    !/\.json\(\)/.test(code) && (code.match(/parseBody\(/g) ?? []).length >= 5 && /delete[^\n]*MODEL_NOTE/.test(code))
}

console.log('\n=== 白名单 ===')
const bare = allow.filter((a) => !a.file || !a.text || !a.rule || !a.reason || !RULES.some((r) => r.id === a.rule || a.rule === '*'))
check(`${allow.length} 条都写明了文件、原文片段、规则和理由`, bare.length === 0, bare.map((a) => JSON.stringify(a)).join(' ｜ '))
const stale = allow.filter((_, i) => !used.has(i))
check('每条都还对得上一处命中（失效的要删掉）', stale.length === 0,
  stale.map((a) => `${a.file}「${a.text}」(${a.rule})`).join(' ｜ '))

if (LIST) {
  console.log('\n=== 全部命中（--list）===')
  for (const h of hits) {
    console.log(`    ${allowed(h) ? '·' : '!'} [${h.rule}] ${h.file}:${h.line}「${h.snippet}」${h.where ? ` @ ${h.where}` : ''}`
      + ` ← ${JSON.stringify(h.text).slice(0, 120)}`)
  }
}

// ---------------------------------------------------------------- 自检：扫描器自己认得对不对

console.log('\n=== 扫描器自检（拿构造的源码喂一遍）===')
{
  const src = [
    "// 注释里写接着跑不算",
    "/* 块注释：后端 */",
    "const a = <button title=\"接着跑\">接着跑</button>",
    "const b = '后端没有响应'",
    "const c = `已用 ${n} tok`",
    "const d = /跑完/.test(x)",
    "const e = x.includes('收尾轮仍想调用工具，接着跑')",
    "const f = x === '在跑'",
    "const g = { '可续跑': 1 }",
    "const h = <div className=\"跑\" data-x=\"跑\" aria-label=\"不能再跑\" />",
    "const i = '让 Copilot 改'",
    "const j = '好的吧。'",
    "const k = '按 report_from 取'",
    "const l = '提示:请稍后...'",
    "const m = '保存并继续运行'",
    "import z from './跑.ts'",
    "switch (y) { case '跑': break }",
    "console.log('后端')",
  ].join('\n')
  const strings = frontStrings(src)
  const flagged = (needle, rule) => strings.some((s) => s.text.includes(needle) && lint(s.text, s.prose).some((h) => h.rule === rule))
  const seen = (needle) => strings.some((s) => s.text.includes(needle))
  check('JSX 文本、JSX 属性里的「接着跑」报出来', strings.filter((s) => s.text === '接着跑').length === 2 && flagged('接着跑', '跑'))
  check('普通字符串里的「后端」、模板字符串里的「tok」报出来', flagged('后端没有响应', '后端') && flagged('已用 {x} tok', 'tok'))
  check('读屏名（aria-label）按界面文案查', flagged('不能再跑', '跑'))
  check('Copilot、句尾语气词、裸键名、半角标点都报出来', flagged('让 Copilot 改', 'Copilot') && flagged('好的吧', '语气词')
    && flagged('按 report_from 取', '键名') && flagged('提示:请稍后', '半角标点'))
  check('注释、正则、includes / === 的原文、对象键、import 路径、case 标签、className / data-*、console 都不算',
    !seen('注释') && !seen('块注释') && !seen('跑完') && !seen('收尾轮') && !seen('在跑') && !seen('可续跑')
      && !strings.some((s) => s.text === '跑') && !seen('./跑.ts') && !strings.some((s) => s.text === '后端'))
  check('合规的文案不误报', !lint('保存并继续运行', true).length && !lint('56.0k token', false).length
    && !lint('已达上限（3 句），这句未裁判', true).length)

  const py = [
    'from fastapi import HTTPException',
    'NODE_REFERENCE = "给模型：接着跑"',
    'def f(x):',
    '    """docstring：后端"""',
    '    logger.info("后端日志")',
    '    if "接着跑" in x: pass',
    '    if x.startswith("跑"): pass',
    '    raise HTTPException(409, detail="后端还在跑")',
    '    return {"message": f"已用 {x} 令牌", "for_model": "照样重试"}',
  ].join('\n')
  const tmp = join(process.env.TMPDIR ?? '/tmp', `check-copy-selftest-${process.pid}.py`)
  const { writeFileSync, rmSync } = await import('node:fs')
  writeFileSync(tmp, py)
  try {
    const got = backStrings(dirname(tmp), [tmp]).filter(backShown)
    const texts = got.map((s) => s.text)
    check('后端：HTTPException 的 detail、返回给界面的 message 报出来',
      texts.includes('后端还在跑') && texts.includes('已用 {x} 令牌')
        && lint('后端还在跑', true).some((h) => h.rule === '跑') && lint('已用 {x} 令牌', true).some((h) => h.rule === '令牌'))
    check('后端：docstring、日志、in / startswith 的原文、写给模型的常量和 for_model 都不算',
      !texts.some((t) => /docstring|日志|给模型|照样/.test(t)) && !texts.includes('接着跑') && !texts.includes('跑'))
  } finally {
    rmSync(tmp, { force: true })
  }
}

console.log(failed ? `\n✗ ${failed} 项未通过` : '\n✓ 文案全部通过')
process.exit(failed ? 1 : 0)
