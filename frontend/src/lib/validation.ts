/**
 * FastAPI 的 422（请求没过 pydantic 校验）翻成人话。
 *
 * 422 的 detail 是 [{type, loc, msg, input, ctx}]：msg 是 pydantic 的英文原文，loc 是
 * 请求体里的内部键名。原样拼出来就是「name：Field required」——表单大多先在前端
 * 校验过，但 Copilot、画布这类只靠后端校验的路径会直接撞上。
 *
 * 这里按 type 翻成中文，loc 换成表单上的叫法；查不到叫法就写字段路径，没见过的
 * type 写笼统说法。英文原文不丢：ApiError 的 detail 和 raw 里都在，界面收进「详情」。
 * 输入值（input）一律不回显：密码、密钥也可能在里面。
 */

export const VALIDATION_TITLE = '提交的内容不符合要求'

export interface ValidationItem {
  type?: string
  loc?: (string | number)[]
  msg?: string
  ctx?: Record<string, unknown>
}

/** 多数表单上一样的叫法 */
const COMMON_LABELS: Record<string, string> = {
  name: '名称',
  description: '说明',
  kind: '类型',
  enabled: '启用',
  title: '标题',
  content: '内容',
  tags: '标签',
}

/**
 * 按接口覆盖：同一个键在不同表单上叫法不同（数据源的 name 是「标识」，
 * 自定义工具的 description 是「描述」）。和页面上 <Field label> 的写法保持一致，
 * 先匹配到的算数，所以更具体的路径写在前面
 */
const LABELS_BY_PATH: [RegExp, Record<string, string>][] = [
  [/^\/(settings\/)?providers/, { base_url: 'Base URL', api_key: 'API Key', default_model: '默认模型', models: '可选模型' }],
  [/^\/datasources\/upload/, { name: '数据源名', header_row: '表头在第几行', file: '文件' }],
  [/^\/datasources/, {
    name: '标识', host: '主机', port: '端口', database: '数据库', username: '用户名', password: '密码',
    readonly: '只读', options: '高级参数',
  }],
  [/^\/custom-tools/, { description: '描述', parameters: '参数 JSON Schema', config: '配置' }],
  [/^\/mcp/, { transport: '传输方式', command: '命令', args: '参数', env: '环境变量', url: 'URL' }],
  [/^\/tools\//, { args: '参数', sandbox_session: '沙箱会话' }],
  [/^\/memory/, { importance: '重要度', scope: '作用域' }],
  [/^\/kb/, { collection: '知识库', file: '文件' }],
  [/^\/skills/, { description: '一句话说明', instructions: '指令内容', examples: '示例', suggested_tools: '建议工具' }],
  [/^\/workflows/, { graph: '工作流', note: '版本说明', level: '发布级别', version: '版本' }],
  [/^\/copilot/, { instruction: '输入的内容', base_graph: '当前工作流', datasource_ids: '圈定的数据源', question: '问题' }],
  [/^\/runs/, {
    input: '输入', run_class: '运行类别', version: '版本', memory_scope: '记忆作用域', collection: '知识库',
    workflow_id: '工作流', graph: '工作流',
  }],
  [/^\/conversations/, { question: '问题', archived: '归档' }],
  [/^\/settings/, { values: '设置项' }],
]

function labelOf(key: string, path?: string): string | undefined {
  const own = path ? LABELS_BY_PATH.find(([re]) => re.test(path))?.[1] : undefined
  return own?.[key] ?? COMMON_LABELS[key]
}

/**
 * loc → 「字段」。每一段都查得到叫法才换（数组下标写成「第 N 项」），有一段查不到
 * 就整条写字段路径：半中半英的「工作流 › nodes › type」比路径还难认
 */
function fieldOf(loc: (string | number)[], path?: string): string {
  const [where, ...rest] = loc
  const segs = where === 'body' || where === 'query' || where === 'path' || where === 'header' || where === 'cookie'
    ? rest : loc
  if (!segs.length) return ''
  const names = segs.map((s) => (typeof s === 'number' ? `第 ${s + 1} 项` : labelOf(s, path)))
  const field = names.every(Boolean) && typeof segs[0] === 'string'
    ? `「${names[0]}」${names.slice(1).join('')}`
    : `「${segs.join('.')}」`
  if (where === 'query') return `参数${field}`
  if (where === 'path') return `地址里的${field}`
  return field
}

const num = (v: unknown) => (typeof v === 'number' ? String(v) : String(v ?? ''))

/** ctx.expected：「'a', 'b' or 'c'」或「1, 2 or 3」→ ['a', 'b', 'c'] */
function choicesOf(expected: unknown): string[] {
  if (typeof expected !== 'string' || !expected.trim()) return []
  const quoted = [...expected.matchAll(/'((?:[^'\\]|\\.)*)'/g)].map((m) => m[1])
  return quoted.length ? quoted : expected.split(/,\s*|\s+or\s+/).map((s) => s.trim()).filter(Boolean)
}

const joinChoices = (xs: string[]) => (xs.length > 1 ? `${xs.slice(0, -1).join('、')} 或 ${xs[xs.length - 1]}` : xs[0])

/** 自定义校验器（value_error / assertion_error）的话是后端写的：写的是中文就照用 */
function customMessage(msg: string | undefined): string | undefined {
  const text = (msg ?? '').replace(/^(?:Value error|Assertion failed),\s*/, '').trim()
  return /[一-鿿]/.test(text) ? text : undefined
}

/** 一条：「字段」+ 怎么了。field 为空（整个请求体）时各自单说 */
function describeOne(d: ValidationItem, path?: string): string {
  const type = d.type ?? ''
  const ctx = d.ctx ?? {}
  if (type === 'json_invalid') return '提交的内容不是合法的 JSON'
  const field = fieldOf(Array.isArray(d.loc) ? d.loc : [], path)
  if (!field && type === 'missing') return '未收到提交的内容'
  const f = field || '提交的内容'
  switch (type) {
    case 'missing':
      return `${f}为必填项`
    case 'string_too_short':
      return Number(ctx.min_length) <= 1 ? `${f}不能为空` : `${f}过短，至少 ${num(ctx.min_length)} 个字符`
    case 'string_too_long':
      return `${f}过长，最多 ${num(ctx.max_length)} 个字符`
    case 'too_short':
      return `${f}至少需要 ${num(ctx.min_length)} 项`
    case 'too_long':
      return `${f}最多 ${num(ctx.max_length)} 项`
    case 'string_pattern_mismatch':
      return `${f}格式不正确`
    case 'string_type':
      return `${f}应为文本`
    case 'int_parsing':
    case 'int_type':
    case 'int_from_float':
      return `${f}应为整数`
    case 'float_parsing':
    case 'float_type':
    case 'decimal_parsing':
    case 'decimal_type':
      return `${f}应为数字`
    case 'bool_parsing':
    case 'bool_type':
      return `${f}只能是「是」或「否」`
    case 'greater_than':
      return `${f}应大于 ${num(ctx.gt)}`
    case 'greater_than_equal':
      return `${f}不能小于 ${num(ctx.ge)}`
    case 'less_than':
      return `${f}应小于 ${num(ctx.lt)}`
    case 'less_than_equal':
      return `${f}不能大于 ${num(ctx.le)}`
    case 'literal_error':
    case 'enum': {
      const xs = choicesOf(ctx.expected)
      // 列太长就不列了：一长串内部枚举值对填表的人没有帮助
      return xs.length && xs.length <= 6 ? `${f}只能取 ${joinChoices(xs)}` : `${f}不在允许的取值范围内`
    }
    case 'list_type':
    case 'tuple_type':
    case 'set_type':
      return `${f}应为列表`
    case 'dict_type':
    case 'model_type':
    case 'model_attributes_type':
      return `${f}应为键值对象`
    case 'url_parsing':
    case 'url_type':
    case 'url_scheme':
      return `${f}不是合法的网址`
    case 'date_parsing':
    case 'date_type':
    case 'datetime_parsing':
    case 'datetime_type':
    case 'time_parsing':
      return `${f}不是合法的日期或时间`
    case 'extra_forbidden':
      return `${f}不是可接受的字段`
    case 'value_error':
    case 'assertion_error': {
      const said = customMessage(d.msg)
      return said ? (field ? `${field}：${said}` : said) : `${f}取值无效`
    }
    default:
      return `${f}不符合要求`
  }
}

/** 422 的 detail 数组 → 一条条中文。path 是请求路径（不带 /api），用来查表单叫法 */
export function describeValidation(items: unknown[], path?: string): string[] {
  return items.map((d) => (d && typeof d === 'object' ? describeOne(d as ValidationItem, path) : '有一项不符合要求'))
}
