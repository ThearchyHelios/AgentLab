import type { ReactNode } from 'react'
import {
  Bot, Braces, Brain, ChartColumnStacked, CheckCircle2, Code2, Combine, Database, FileInput, FileOutput, FileText,
  Gauge, GitBranch, GitCompareArrows, Hand, Repeat, Search, ShieldAlert, Shuffle, Users, Wrench,
} from 'lucide-react'
import {
  APPROVAL_POLICY_LABEL, CHALLENGE_TEXT, DUAL_CHECK_TEXT, MERGE_TEXT, NODE_TYPE_LABEL, REJECTED_TEXT, UPGRADE_POLICY_LABEL,
} from '../lib/terms'
import { ANALYSIS_FIELD_LABEL, ANALYSIS_OPTION_LABEL, ANALYSIS_TEXT } from '../lib/termsAnalysis'
import type { NodeType } from '../types'
import { dualCheckFields, dualCheckSummary } from './DualCheckFields'
import { HUMAN_PROCESS_FIELDS, TOOL_WRITE_FIELDS } from './humanFields'
import { nodePickField, triStateField, upstreamModel, verifyModelFields } from './verifyFields'

export type FieldType =
  | 'text' | 'textarea' | 'prompt' | 'code' | 'number' | 'select' | 'switch'
  | 'json' | 'model' | 'tools' | 'skills' | 'collection'
  | 'ioFields' | 'cases' | 'agents' | 'metricsList' | 'nodeRefs' | 'caliberFrom' | 'judge' | 'mergeInputs'
  | 'analysisItems' | 'analysisOverrides'

/**
 * 这个字段里写的是什么语法。
 *
 * 模板和表达式长得像、错法相反：模板里写 `{{ vars.x }}`，取不到值渲染成空字符串、
 * 不报错；表达式里写裸的 `vars.x == 1`，套上 `{{ }}` 就是另一种错。以前两种输入框
 * 外观一模一样，只能靠 placeholder 暗示，后端专门加了「{{ }} 是多余的」这条告警，
 * 说明这类错误很常见。检查器按它挂徽标、补全和高亮。
 *
 * 不写时按类型推：prompt / textarea / code 是模板，其余是普通文本。
 * 显式写 'plain' 的是长得像模板、后端却不渲染的字段（成员的 system）。
 */
export type FieldSyntax = 'template' | 'expression' | 'plain'

export interface FieldDef {
  key: string
  label: string
  type: FieldType
  syntax?: FieldSyntax
  placeholder?: string
  help?: string
  options?: { value: string; label: string }[]
  min?: number
  max?: number
  step?: number
  /** 只在满足条件时显示，避免面板一次糊一屏用不上的选项 */
  when?: (config: Record<string, any>) => boolean
  /**
   * 看得见但暂时用不了：返回一句为什么（「先写 Schema 才能开」），返回 null 就能用。
   * 和 when 的区别：藏起来的人不知道有这个选项，禁用的知道、也知道差什么
   */
  disabled?: (config: Record<string, any>) => string | null
  /** nodeRefs：只能选这一类的上游节点（报告的 metrics_from 只收口径卡） */
  refType?: NodeType
  /**
   * select：config 里写着一个不在选项里的值（手写的、Copilot 写的、后续版本才支持的）时，
   * 下拉里照实多列一项、写明为什么不认。不给的话浏览器会把它显示成第一个选项——看着像默认值
   */
  unknownLabel?: (value: string) => string
  advanced?: boolean
  /**
   * 现有字段类型画不了的配置（选画布上某一类节点、三态开关、接入加模型）交给它画。
   * 给了 render 就不再按 type 渲染；type 仍用于默认值和校验
   */
  render?: (p: FieldRenderProps) => ReactNode
}

/** FieldDef.render 拿到的：当前值、写回、所在节点和配置，以及无障碍属性 */
export interface FieldRenderProps {
  id: string
  nodeId: string
  value: any
  config: Record<string, any>
  invalid: boolean
  describedBy?: string
  disabled?: boolean
  onChange: (v: any) => void
}

/**
 * 写没写结构化输出 Schema：对象形状（type: object 或带 properties），画布里存成 JSON 文本的也认，
 * 和后端 llm._output_schema 的认法一致
 */
export function hasOutputSchema(schema: unknown): boolean {
  let value = schema
  if (typeof value === 'string') {
    if (!value.trim()) return false
    try { value = JSON.parse(value) } catch { return false }
  }
  return !!value && typeof value === 'object' && !Array.isArray(value)
    && ((value as any).type === 'object' || 'properties' in (value as any))
}

/** 字段实际的语法：显式声明优先，否则按类型推 */
export function syntaxOf(field: Pick<FieldDef, 'type' | 'syntax'>): FieldSyntax {
  if (field.syntax) return field.syntax
  return field.type === 'prompt' || field.type === 'textarea' || field.type === 'code'
    ? 'template' : 'plain'
}

export interface HandleDef {
  id: string
  label: string
  color?: string
}

export interface NodeDef {
  type: NodeType
  label: string
  category: string
  description: string
  icon: typeof Bot
  hasTarget: boolean
  /** 静态出口；branch/loop 这类由配置动态生成的返回 null */
  sources: HandleDef[] | null
  fields: FieldDef[]
  defaults: Record<string, any>
  /**
   * 卡片正文那一行摘要要提到画布上别的节点时给它（双盲复算复算的是哪张口径卡、几个指标），没给的照旧走
   * NodeCard 的 summarize。NodeCard 在 studio store 的选择器里调它、每条事件都会跑：要快、没有副作用、只返回字符串
   */
  summary?: (ctx: NodeSummaryContext) => string
}

/** NodeDef.summary 拿到的：这个节点的 id 和配置，以及画布上的全部节点和连线 */
export interface NodeSummaryContext {
  id: string
  config: Record<string, any>
  nodes: readonly { id: string; data: { nodeType: NodeType; label?: string; config?: Record<string, any> } }[]
  edges: readonly { source: string; target: string }[]
}

const MODEL_FIELDS: FieldDef[] = [
  { key: 'model', label: '模型', type: 'model', help: '留空则使用默认模型接入' },
  {
    key: 'thinking', label: '思考模式', type: 'select', advanced: true,
    options: [
      { value: '', label: '跟随模型默认' },
      { value: 'summarized', label: '开启并显示摘要' },
      { value: 'adaptive', label: '开启但不显示' },
      { value: 'off', label: '关闭' },
    ],
    help: 'Claude 4.6 及以上版本默认先思考再回答，思考也会消耗 token',
  },
  {
    key: 'effort', label: '投入程度', type: 'select', advanced: true,
    options: [
      { value: '', label: '默认' }, { value: 'low', label: '低' },
      { value: 'medium', label: '中' }, { value: 'high', label: '高' },
      { value: 'xhigh', label: '很高' }, { value: 'max', label: '最高' },
    ],
  },
  {
    key: 'temperature', label: '温度', type: 'number', min: 0, max: 2, step: 0.1, advanced: true,
    help: 'Claude 4.6 之后的模型不支持此项，填写后将被自动忽略',
  },
  { key: 'max_tokens', label: '最大输出 token', type: 'number', min: 1, advanced: true },
]

/**
 * 审批策略。留空 = 跟随设置里的「危险工具默认需要人工确认」。
 *
 * 以前界面新建的节点在 defaults 里写死了 approval:'dangerous'，于是每个节点都带着
 * 显式配置，全局设置对它们永远不起作用；后端把空值当成「跟随全局」
 * （NodeContext.approval_mode），这里的默认就该是空。
 */
const APPROVAL_FOLLOW = { value: '', label: '跟随全局设置' }
const APPROVAL_OPTIONS = [
  APPROVAL_FOLLOW,
  { value: 'dangerous', label: APPROVAL_POLICY_LABEL.dangerous },
  { value: 'always', label: APPROVAL_POLICY_LABEL.always },
  { value: 'never', label: APPROVAL_POLICY_LABEL.never },
]

const COMMON_TAIL: FieldDef[] = [
  {
    key: 'assign_to', label: '结果存为变量', type: 'text', placeholder: '例如 result',
    help: '下游用 {{ vars.变量名 }} 引用',
  },
  {
    key: 'skip_if', label: '跳过条件', type: 'text', syntax: 'expression', advanced: true,
    placeholder: 'vars.count == 0',
  },
  { key: 'retries', label: '失败重试次数', type: 'number', min: 0, max: 5, advanced: true },
  {
    key: 'on_error', label: '出错时', type: 'select', advanced: true,
    options: [{ value: '', label: '中断整个运行' }, { value: 'continue', label: '记录错误并继续' }],
  },
]

export const NODE_DEFS: Record<NodeType, NodeDef> = {
  input: {
    type: 'input', label: NODE_TYPE_LABEL.input, category: '起止', icon: FileInput,
    description: '工作流入口，声明需要哪些输入',
    hasTarget: false, sources: [{ id: 'out', label: '' }],
    fields: [{ key: 'fields', label: '输入字段', type: 'ioFields' }],
    defaults: { fields: [{ name: 'question', required: true }] },
  },
  output: {
    type: 'output', label: NODE_TYPE_LABEL.output, category: '起止', icon: FileOutput,
    description: '收集结构化成果；配置出具契约后按三档出具（完整 / 降档 / 不予出具）',
    hasTarget: true, sources: [],
    fields: [
      { key: 'fields', label: '成果字段', type: 'ioFields' },
      {
        key: 'contract', label: '出具契约', type: 'json', syntax: 'template', advanced: true,
        help: '用 JSON 声明，可包含 metrics_from（口径卡节点 ID）、narrative（叙述）、required（必需指标）、'
          + 'expected（期望指标）、allow_numbers（允许无出处的数字）、strict（严格模式）。'
          + '配置后，叙述中的每个数字都必须能追溯到指标集，否则降档或不予出具',
      },
    ],
    defaults: { fields: [{ name: '结果', value: '{{ last_message }}' }] },
  },
  llm: {
    type: 'llm', label: NODE_TYPE_LABEL.llm, category: '模型', icon: Brain,
    description: '单次 LLM 调用，可要求结构化输出',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      { key: 'system', label: '系统提示', type: 'textarea', placeholder: '你是…' },
      { key: 'prompt', label: '用户提示', type: 'prompt', placeholder: '{{ input.question }}' },
      { key: 'skills', label: '挂载 Skill', type: 'skills' },
      ...MODEL_FIELDS,
      {
        key: 'output_schema', label: '结构化输出 Schema', type: 'json', advanced: true,
        help: '填写后，模型必须按此 JSON Schema 返回',
      },
      { key: 'use_history', label: '带上对话历史', type: 'switch', advanced: true },
      ...COMMON_TAIL,
    ],
    defaults: { system: '', prompt: '{{ input.question }}' },
  },
  agent: {
    type: 'agent', label: NODE_TYPE_LABEL.agent, category: '模型', icon: Bot,
    description: '循环调用工具，由模型自主决定调用哪些工具',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      { key: 'system', label: '系统提示', type: 'textarea' },
      { key: 'prompt', label: '任务', type: 'prompt' },
      { key: 'tools', label: '可用工具', type: 'tools' },
      { key: 'skills', label: '挂载 Skill', type: 'skills' },
      {
        key: 'max_steps', label: '最大步数', type: 'number', min: 1, max: 100, placeholder: '跟随设置',
        // 早期界面新建的 Agent 节点在 defaults 里写死了 max_steps: 12，后端把 12 当成留空处理（跟随运行默认值）
        help: '步数上限仅作保护。出现重复调用、连续多步没有新信息、预算用尽或上下文将满时，'
          + 'Agent 会基于已获取的信息提前收尾。留空则使用「设置 → 运行默认值」',
      },
      {
        key: 'budget_tokens', label: 'token 预算', type: 'number', min: 1000, step: 10000, advanced: true,
        placeholder: '跟随设置',
        help: '本节点最多使用的 token 数（输入加输出），用尽后按已获取的信息收尾。留空则使用设置中的值，设置中可改为不限',
      },
      {
        key: 'budget_usd', label: '金额预算（美元）', type: 'number', min: 0.01, step: 0.1, advanced: true,
        placeholder: '跟随设置',
        help: '按模型目录中的价格估算。目录中未收录价格的模型无法估算金额，只能依靠 token 预算',
      },
      {
        key: 'approval', label: '审批策略', type: 'select', options: APPROVAL_OPTIONS,
        help: '需要审批时运行将暂停，请在运行面板或记录页的审批卡上处理',
      },
      {
        key: 'parallel_tools', label: '并行执行工具', type: 'switch', advanced: true,
        help: '默认关闭：每轮只调用一个工具，根据结果决定下一步。开启后每轮可同时调用多个工具，速度更快，'
          + '但同一批调用之间不会重新推理',
      },
      {
        key: 'output_schema', label: '结构化输出 Schema', type: 'json', advanced: true,
        help: '需要将查询结果交给口径卡时填写。填写后还需开启下方的「按出处核对字段」才会生效；'
          + '只填写 Schema 而不开启时，输出仍为纯文本',
      },
      {
        key: 'cite_fields', label: '按出处核对字段', type: 'switch', advanced: true,
        // 已经开着的不锁：开了之后又清掉 Schema，得还能把它关上
        disabled: (c) => (c.cite_fields || hasOutputSchema(c.output_schema) ? null : '需先填写上方的「结构化输出 Schema」'),
        help: '运行结束后额外进行一次抽取调用：模型为 Schema 中的每个字段标注来源查询及单元格，系统按查询快照取值并核对，'
          + '不一致时以快照为准；无法取得的记为空值，不以 0 代替。下游口径卡通过 vars.变量名.字段 读取',
      },
      ...MODEL_FIELDS,
      ...COMMON_TAIL,
    ],
    defaults: { tools: [], parallel_tools: false },
  },
  supervisor: {
    type: 'supervisor', label: NODE_TYPE_LABEL.supervisor, category: '模型', icon: Users,
    description: '调度者按进展将任务分派给多个团队成员',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      { key: 'goal', label: '团队目标', type: 'prompt' },
      { key: 'agents', label: '团队成员', type: 'agents' },
      // 叫「最多轮数」：团队用完轮数的报错和右栏都说「调大『最多轮数』」，字段名得对得上
      { key: 'max_rounds', label: '最多轮数', type: 'number', min: 1, max: 25 },
      {
        // 以前轮数用完就把成员最后的原话当成功结果交出去：成员一直没查到数、只会输出
        // 工具调用的原始标记，运行照样显示「已完成」。默认改成判失败，降档要自己选
        key: 'on_exhausted', label: '用完轮数时', type: 'select',
        // 没写和写 fail 是一回事（后端只认 degrade）。选项用 fail：没写时下拉框落在第一项上
        options: [
          { value: 'fail', label: '判为失败（默认）' },
          { value: 'degrade', label: '降档交付' },
        ],
        help: '最后一轮结束时调度者仍未判定完成的处理方式。判为失败：节点报错并停止运行，同时列出从未被分派任务的成员；'
          + '降档交付：将成员最后的输出交给下游并标记「未完成」，出具和复核随之降档。'
          + '需要产出数据或报告的工作流，建议保留「判为失败」',
      },
      {
        key: 'max_parallel', label: '每轮最大并行成员数', type: 'number', min: 1, max: 6,
        help: '调度者可将互不依赖的任务放在同一轮并行执行，本轮耗时取决于最慢的成员。设为 1 则严格串行。'
          + '同一轮的成员看到的是同一份进展快照，有依赖关系的任务若被同时分派，'
          + '成员会基于相同的旧信息重复工作',
      },
      {
        key: 'approval', label: '审批策略', type: 'select', advanced: true,
        options: [
          APPROVAL_FOLLOW,
          { value: 'dangerous', label: '直接拦截需要审批的调用' },
          { value: 'never', label: APPROVAL_POLICY_LABEL.never },
        ],
        help: '团队成员在同一节点内并行执行，无法暂停等待审批。默认拦截危险工具及可写数据源上的写操作，'
          + '并提示成员改用其他方式；需要逐次审批的操作请交给团队外的 Agent 节点',
      },
      ...MODEL_FIELDS,
      ...COMMON_TAIL,
    ],
    defaults: { max_rounds: 6, max_parallel: 3, agents: [] },
  },
  tool: {
    type: 'tool', label: NODE_TYPE_LABEL.tool, category: '执行', icon: Wrench,
    description: '直接调用一个指定工具',
    // 出口由配置决定：会请人审批（工具调用审批、写入前的动作确认）时多一个「驳回」出口，见 sourceHandles
    hasTarget: true, sources: null,
    fields: [
      { key: 'tool', label: '工具', type: 'tools', help: '只能选一个' },
      { key: 'args', label: '参数', type: 'json', syntax: 'template', help: '值中可用 {{ }} 引用上游数据' },
      { key: 'approval', label: '审批策略', type: 'select', options: APPROVAL_OPTIONS },
      // 写外部系统只能放在调用工具节点里（A5）：确认卡在节点开头、发送之前
      ...TOOL_WRITE_FIELDS,
      ...COMMON_TAIL,
    ],
    defaults: { args: {} },
  },
  merge: {
    type: 'merge', label: NODE_TYPE_LABEL.merge, category: '执行', icon: Combine,
    description: '把不同数据库的查询结果按合并键合并成一张表，报告和口径卡照常引用、可查看出处',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      // 后端 config.inputs 是 {别名: 节点 id}：别名就是内存库里的表名（engine/merge_query.alias_problem 校验）
      { key: 'inputs', label: '输入', type: 'mergeInputs', help: MERGE_TEXT.inputsHint },
      {
        key: 'sql', label: '合并 SQL', type: 'code',
        placeholder: 'SELECT s.day, s.store, s.orders, v.visitors FROM s JOIN v ON s.day = v.day AND s.store = v.store',
        help: '一条 SQLite 的 SELECT 或 WITH 查询，表名用上面的别名。同一个库的数据请直接写成一条查询；'
          + '比率、增幅等派生计算请写在口径卡中。直接选取输入的列（可用 AS 改名）时，报告中的数字可以追到输入的那一格',
      },
      ...COMMON_TAIL,
    ],
    defaults: { inputs: {}, sql: '' },
  },
  code: {
    type: 'code', label: NODE_TYPE_LABEL.code, category: '执行', icon: Code2,
    description: '在 microVM 或系统沙箱中执行代码',
    // 审批策略设成每次审批时多一个「驳回」出口，见 sourceHandles
    hasTarget: true, sources: null,
    fields: [
      {
        key: 'language', label: '语言', type: 'select',
        options: [
          { value: 'python', label: 'Python' }, { value: 'bash', label: 'Bash' },
          { value: 'node', label: 'Node.js' },
        ],
      },
      { key: 'code', label: '代码', type: 'code', help: '代码中可用 {{ }} 插入变量' },
      { key: 'timeout', label: '超时（秒）', type: 'number', min: 1, max: 300 },
      { key: 'memory_mb', label: '内存上限（MB）', type: 'number', min: 64, max: 4096, advanced: true },
      { key: 'network', label: '允许联网', type: 'switch', help: '默认禁止联网' },
      {
        key: 'isolation', label: '隔离档位', type: 'select',
        options: [
          { value: '', label: '跟随系统默认' },
          { value: 'strict', label: '严格：microVM（独立内核，可严格限制内存）' },
          { value: 'fast', label: '快速：系统沙箱（延迟低，无法限制内存）' },
        ],
        help: '不可信代码请使用「严格」；自行编写或已审核的代码可使用「快速」。两档均无法阻止 DNS 出站请求，'
          + '防止数据外泄需依靠网络层隔离。所选档位不可用时将回退到默认档位，并在时间线上告警',
      },
      {
        key: 'approval', label: '审批策略', type: 'select', advanced: true,
        // 代码节点不跟全局设置走（后端缺省就是 never），所以没有「跟随全局」
        options: [
          { value: 'never', label: APPROVAL_POLICY_LABEL.never },
          { value: 'always', label: `${APPROVAL_POLICY_LABEL.always}（审批时可修改代码）` },
        ],
      },
      { key: 'fail_fast', label: '执行失败即中断', type: 'switch', advanced: true },
      {
        key: 'evidence_role', label: '证据角色', type: 'select', advanced: true,
        options: [{ value: '', label: '计算（默认）' }, { value: 'source', label: '取数' }],
        help: '报告不能直接引用代码节点的输出，沙箱中计算的数值需经口径卡引用。仅原样获取外部数据'
          + '（调用接口、读取文件）的，选择「取数」；包含业务计算的保留「计算」，口径卡引用其数值时会给出提醒',
      },
      ...COMMON_TAIL,
    ],
    defaults: { language: 'python', code: 'print("hello")', timeout: 30, network: false, fail_fast: true },
  },
  branch: {
    type: 'branch', label: NODE_TYPE_LABEL.branch, category: '控制', icon: GitBranch,
    description: '按条件表达式或模型分类进入不同分支',
    hasTarget: true, sources: null,
    fields: [
      {
        key: 'mode', label: '判断方式', type: 'select',
        options: [
          { value: 'expression', label: '表达式判断' },
          { value: 'llm', label: '让模型分类' },
        ],
      },
      { key: 'cases', label: '分支', type: 'cases' },
      {
        key: 'input', label: '待分类内容', type: 'prompt',
        when: (c) => c.mode === 'llm', placeholder: '{{ last_message }}',
      },
      { key: 'instruction', label: '分类说明', type: 'textarea', when: (c) => c.mode === 'llm' },
      ...MODEL_FIELDS.filter((f) => f.key === 'model').map((f) => ({
        ...f, when: (c: any) => c.mode === 'llm',
      })),
      ...COMMON_TAIL.filter((f) => f.key !== 'assign_to'),
    ],
    defaults: { mode: 'expression', cases: [{ key: 'yes', condition: '', label: '' }] },
  },
  loop: {
    type: 'loop', label: NODE_TYPE_LABEL.loop, category: '控制', icon: Repeat,
    description: '遍历列表或按条件重复执行',
    hasTarget: true, sources: null,
    fields: [
      {
        key: 'mode', label: '循环方式', type: 'select',
        options: [
          { value: 'foreach', label: '遍历列表' },
          { value: 'while', label: '条件成立时重复' },
        ],
      },
      {
        // 后端按模板渲染（control.py 的 ctx.render），以前定义成普通文本，没有补全
        key: 'items', label: '列表来源', type: 'text', syntax: 'template',
        when: (c) => c.mode !== 'while', placeholder: '{{ input.items }}',
      },
      { key: 'item_var', label: '当前项变量名', type: 'text', when: (c) => c.mode !== 'while' },
      {
        key: 'condition', label: '继续条件', type: 'text', syntax: 'expression',
        when: (c) => c.mode === 'while', placeholder: 'vars.done != true',
      },
      { key: 'max_iterations', label: '最大迭代次数', type: 'number', min: 1, max: 100 },
    ],
    defaults: { mode: 'foreach', item_var: 'item', max_iterations: 10 },
  },
  subgraph: {
    type: 'subgraph', label: NODE_TYPE_LABEL.subgraph, category: '控制', icon: Braces,
    description: '将另一个工作流作为单个节点嵌入',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      { key: 'workflow_id', label: '工作流', type: 'select', options: [] },
      { key: 'input', label: '传入参数', type: 'json', syntax: 'template' },
      ...COMMON_TAIL,
    ],
    defaults: { input: {} },
  },
  memory: {
    type: 'memory', label: NODE_TYPE_LABEL.memory, category: '上下文', icon: Database,
    description: '读取或写入跨运行的长期记忆',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      {
        key: 'action', label: '操作', type: 'select',
        options: [
          { value: 'recall', label: '回忆（检索）' },
          { value: 'write', label: '记住（写入）' },
          { value: 'clear', label: '清空该作用域' },
        ],
      },
      {
        key: 'scope', label: '作用域', type: 'text', syntax: 'template',
        help: '留空则使用本次运行的默认作用域（「设置 → 运行默认值」）',
      },
      { key: 'query', label: '检索内容', type: 'prompt', when: (c) => c.action !== 'write' && c.action !== 'clear' },
      { key: 'limit', label: '返回条数', type: 'number', min: 1, max: 20, when: (c) => c.action !== 'write' },
      { key: 'content', label: '记住的内容', type: 'prompt', when: (c) => c.action === 'write' },
      {
        key: 'kind', label: '记忆类型', type: 'select', when: (c) => c.action === 'write',
        options: [
          { value: 'fact', label: '事实' }, { value: 'preference', label: '偏好' },
          { value: 'episode', label: '事件' },
        ],
      },
      { key: 'importance', label: '重要程度', type: 'number', min: 0, max: 1, step: 0.1, when: (c) => c.action === 'write' },
      ...COMMON_TAIL,
    ],
    // scope 不写死：留空才会跟着这次运行的默认作用域走（后端 ctx.run.memory_scope），
    // 写死 default 等于让设置里的「默认记忆作用域」对界面新建的节点永远不起作用
    defaults: { action: 'recall', limit: 5, query: '{{ last_message }}' },
  },
  retrieve: {
    type: 'retrieve', label: NODE_TYPE_LABEL.retrieve, category: '上下文', icon: Search,
    description: '从知识库里混合检索相关片段',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      { key: 'query', label: '检索问题', type: 'prompt' },
      {
        key: 'collection', label: '知识库', type: 'collection',
        help: '留空则使用本次运行的默认知识库（「设置 → 运行默认值」）',
      },
      { key: 'limit', label: '返回片段数', type: 'number', min: 1, max: 20 },
      {
        key: 'rerank', label: '重排', type: 'select', advanced: true,
        options: [
          { value: 'off', label: '不重排' },
          { value: 'model', label: '让模型重排（更准确，增加一次调用）' },
        ],
        help: '开启后先召回更多候选片段，再由模型按「对回答问题的帮助程度」重新排序',
      },
      {
        key: 'alpha', label: '向量与关键词权重', type: 'number', min: 0, max: 1, step: 0.1,
        help: '1 表示纯语义相似，0 表示纯关键词匹配。留空则根据向量模型的能力自动选择；'
          + '向量模型不具备语义能力时，提高该值反而会降低命中率',
      },
      { key: 'min_score', label: '最低分数', type: 'number', min: 0, max: 1, step: 0.05, advanced: true },
      ...COMMON_TAIL,
    ],
    // alpha 不写死：写死 0.5 等于给一个没有语义能力的信号一半权重，
    // 实测会把 hit@1 从 88% 拉到 81%（backend/tests/test_retrieval_quality.py）
    // collection 同理：留空跟着运行默认值走，正式运行会把实际用的那个写进封存
    defaults: { query: '{{ last_message }}', limit: 5, rerank: 'off' },
  },
  transform: {
    type: 'transform', label: NODE_TYPE_LABEL.transform, category: '上下文', icon: Shuffle,
    description: '不调用模型，直接将数据转换为下游所需的格式',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      {
        key: 'mode', label: '方式', type: 'select',
        options: [
          { value: 'template', label: '文本模板' },
          { value: 'expression', label: '表达式' },
          { value: 'json', label: 'JSON 模板' },
        ],
      },
      { key: 'template', label: '模板', type: 'textarea', when: (c) => c.mode !== 'expression' },
      {
        key: 'expression', label: '表达式', type: 'text', syntax: 'expression',
        when: (c) => c.mode === 'expression', placeholder: 'len(vars.items)',
      },
      ...COMMON_TAIL,
    ],
    defaults: { mode: 'template', template: '{{ last_message }}' },
  },
  human: {
    type: 'human', label: NODE_TYPE_LABEL.human, category: '把关', icon: Hand,
    description: '暂停运行，等待人工审批、补充信息或修改草稿',
    hasTarget: true, sources: null,
    fields: [
      {
        key: 'mode', label: '审批方式', type: 'select',
        options: [
          { value: 'approve', label: '批准 / 驳回' },
          { value: 'input', label: '补充输入' },
          { value: 'edit', label: '编辑草稿' },
        ],
      },
      { key: 'title', label: '标题', type: 'text', syntax: 'template' },
      { key: 'message', label: '展示给审批人的内容', type: 'prompt' },
      { key: 'draft', label: '草稿内容', type: 'prompt', when: (c) => c.mode === 'edit' },
      { key: 'stop_on_reject', label: '驳回即终止运行', type: 'switch', when: (c) => c.mode === 'approve' },
      // 流程语义（§3.7）：谁处理、几个人怎么算、多久、超时怎么办。没有应用绑定的运行派给任何人，和以前一样
      ...HUMAN_PROCESS_FIELDS,
      ...COMMON_TAIL,
    ],
    defaults: { mode: 'approve', title: '待审批', message: '{{ last_message }}' },
  },
  validate: {
    type: 'validate', label: NODE_TYPE_LABEL.validate, category: '把关', icon: CheckCircle2,
    description: '按 JSON Schema 校验，不合格时可由模型自动返工',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      { key: 'source', label: '待校验内容', type: 'prompt', placeholder: '{{ last_message }}' },
      { key: 'schema', label: 'JSON Schema', type: 'json' },
      { key: 'max_retries', label: '最多返工次数', type: 'number', min: 0, max: 5 },
      { key: 'repair_with_llm', label: '让模型修复', type: 'switch' },
      { key: 'fail_fast', label: '校验失败即中断', type: 'switch' },
      ...COMMON_TAIL,
    ],
    defaults: {
      source: '{{ last_message }}', max_retries: 2, repair_with_llm: true, fail_fast: true,
      schema: { type: 'object', properties: {}, required: [] },
    },
  },
  report: {
    type: 'report', label: NODE_TYPE_LABEL.report, category: '模型', icon: FileText,
    description: '生成带引用的报告：模型写引用标记，系统从口径卡取值渲染，引用的数字可查看出处',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      {
        key: 'instructions', label: '写作要求', type: 'prompt', placeholder: '为 {{ input.week }} 写周报，先总后分',
        help: '写作内容与要求。上游口径卡的指标和运行输入会自动整理成证据目录交给模型，无需在此手动填写',
      },
      {
        key: 'metrics_from', label: '指标来自', type: 'nodeRefs', refType: 'metrics',
        help: '留空则使用上游全部口径卡。报告中的数字只能引用这些口径卡中的指标',
      },
      {
        // 后端只认 strict / off（report.py 的 NUMBERS）
        key: 'numbers', label: '未引用的数字', type: 'select',
        options: [
          { value: 'strict', label: '计为违规，要求重写（默认）' },
          { value: 'off', label: '仅标注，不要求重写' },
        ],
        help: '报告中的每个数字都应写成引用标记，由系统取值渲染。模型自行写出的数字无法核对出处',
      },
      {
        // 留空不是「不处理」：后端按运行类别取默认，探索运行照常产出、正式运行判失败
        key: 'on_violation', label: '重写后仍有违规时', type: 'select',
        options: [
          { value: '', label: '按运行类别：探索运行正常产出，正式运行判为失败' },
          { value: 'flag', label: '正常产出，并标注违规之处' },
          { value: 'fail', label: '判为失败，报告不再交给下游' },
        ],
      },
      {
        key: 'max_repairs', label: '最多重写次数', type: 'number', min: 0, max: 3, placeholder: '1',
        help: '存在违规时，将违规清单交回模型重写。每次重写都会重新生成全文，增加次数通常难以明显改善结果',
      },
      {
        // 后端认 off / require_citation / judge（schema.CLAIMS_VALUES）。没写等于 off：select 显示第一项，正好是默认
        key: 'claims', label: '未附依据的结论句', type: 'select',
        options: [
          { value: 'off', label: '不处理（默认）：不参与出具判档' },
          { value: 'require_citation', label: '计入缺口：出具按档位降档' },
          { value: 'judge', label: '计入缺口，并由模型逐句判断证据是否支持' },
        ],
        help: '结论句指含数字、趋势词或因果词的句子。选择「计入缺口」时，未附依据的结论句计为缺口，出具随之降档'
          + '（系统自动链接的表名、字段名不算依据）。选择「由模型逐句判断」时，另一个模型按证据判断每句结论：'
          + '正式运行中当场判断并随报告封存，探索运行中按需逐句判断；结果为模型判断，并非系统核对。'
          + '受管级别发布须选择后两项之一；选择由模型逐句判断时，还需在高级选项中填写金额上限（或选择不限）',
        unknownLabel: (v) => `${v}：无法识别的值，请从上方选项中选择`,
      },
      {
        // claims 为 judge 时的子配置（schema.JUDGE_KEYS）。没写的上限用设置里「证据裁判」的默认值；写 null 是不限
        key: 'judge', label: '结论句裁判', type: 'judge', advanced: true,
        when: (c) => c.claims === 'judge' || (!!c.judge && typeof c.judge === 'object'),
        help: '未填写的上限使用「设置 → 证据裁判」中的默认值；勾选「不限」表示该项不设上限，仅受其余上限约束。建议裁判模型与撰写报告的模型不同',
      },
      {
        // 后端只认 link / off（report.py 的 ENTITIES）
        key: 'entities', label: '表名、字段名', type: 'select', advanced: true,
        options: [
          { value: 'link', label: '核对并链接匹配的名称（默认）' },
          { value: 'off', label: '不核对' },
        ],
        help: '报告中的表名、字段名按本次运行的表结构快照和查询核对：匹配的名称可点击查看出处，'
          + '反引号中无从核对的名称标为「疑似不存在的名称」',
        unknownLabel: (v) => `${v}：无法识别的值，请从上方选项中选择`,
      },
      {
        // 后端认 off / check / strict（schema.ANALYSIS_CLAIMS_VALUES），没写按 check：缺省放第一项，没写时下拉正好落在它上面。
        // 选项以 engine/labels.py 的短说法开头
        key: 'analysis_claims', label: ANALYSIS_FIELD_LABEL.analysis_claims, type: 'select',
        options: [
          { value: 'check', label: `${ANALYSIS_OPTION_LABEL.analysis_claims.check}（默认）：仍有的在报告中标注，不影响出具档位` },
          { value: 'strict', label: `${ANALYSIS_OPTION_LABEL.analysis_claims.strict}：按最多重写次数重写，出具随之降档` },
          { value: 'off', label: ANALYSIS_OPTION_LABEL.analysis_claims.off },
        ],
        help: '「主要来自」「超出常规波动」「持续上升」这类判断要挂对应的分析结论且判定对得上；「由于」「导致」这类原因要有'
          + '知识库原话或运行输入作依据，否则写成待核实的推测。受管级别发布须选择「重写后仍有的计入缺口」',
        unknownLabel: (v) => `${v}：无法识别的值，请从上方选项中选择`,
      },
      {
        // 后端认 auto / off（schema.ANALYSIS_SCOPE_VALUES），没写按 auto
        key: 'analysis_scope', label: ANALYSIS_FIELD_LABEL.analysis_scope, type: 'select', advanced: true,
        options: [
          { value: 'auto', label: `${ANALYSIS_OPTION_LABEL.analysis_scope.auto}（默认）` },
          { value: 'off', label: ANALYSIS_OPTION_LABEL.analysis_scope.off },
        ],
        help: '报告末尾由系统写一段：现象、拆过、排除、没拆、检验，写作者改不了，随报告封存',
        unknownLabel: (v) => `${v}：无法识别的值，请从上方选项中选择`,
      },
      ...MODEL_FIELDS,
      { key: 'system', label: '系统提示', type: 'textarea', advanced: true, placeholder: '你是…' },
      ...COMMON_TAIL,
    ],
    defaults: { instructions: '', numbers: 'strict', max_repairs: 1 },
  },
  metrics: {
    type: 'metrics', label: NODE_TYPE_LABEL.metrics, category: '把关', icon: Gauge,
    description: '受控指标集：用确定性表达式计算指标，结果可复算，供报告引用',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      { key: 'caliber_from', label: '口径卡来源', type: 'caliberFrom' },
      {
        key: 'caliber', label: '口径名称', type: 'text', syntax: 'template', placeholder: '周报口径',
        when: (c) => !c.caliber_from,
      },
      { key: 'caliber_version', label: '口径版本', type: 'text', placeholder: 'v1', when: (c) => !c.caliber_from },
      { key: 'metrics', label: '指标定义', type: 'metricsList', when: (c) => !c.caliber_from },
      {
        key: 'upgrade_policy', label: '上游发布新版本时', type: 'select', when: (c) => !!c.caliber_from,
        options: [
          { value: '', label: '未声明：上游有新版本时阻止正式运行' },
          { value: 'recompute', label: UPGRADE_POLICY_LABEL.recompute },
          { value: 'dual', label: UPGRADE_POLICY_LABEL.dual },
          { value: 'incomparable', label: UPGRADE_POLICY_LABEL.incomparable },
        ],
        help: '所固定的版本之后上游又发布了新版本时，正式运行前必须声明处置方式，规则与子工作流的升版处置相同；'
          + '声明后按声明执行，并记入运行记录',
      },
      {
        key: 'on_missing', label: '缺输入时', type: 'select', advanced: true,
        options: [{ value: '', label: '整个节点失败' }, { value: 'null', label: '记为空值，交给出具契约判档' }],
        help: '上游未提供某个指标所需的数值时的处理方式。记为空值的指标在报告中显示「—」，出具契约按必需 / 期望项降档',
      },
      {
        key: 'assign_to', label: '结果存为变量', type: 'text',
        help: '报告撰写节点可用 {{ nodes.节点ID.text }} 引用指标清单',
      },
    ],
    defaults: {
      caliber: '', caliber_version: 'v1',
      metrics: [{ id: 'total', name: '', unit: '', expression: '' }],
    },
  },
  analysis: {
    type: 'analysis', label: NODE_TYPE_LABEL.analysis, category: '把关', icon: ChartColumnStacked,
    description: ANALYSIS_TEXT.node.description,
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    // 配置的键照服务端 engine/schema.analysis_issues：items（每项 id、op、mode、from、params、hypothesis）、overrides（阈值覆盖，
    // 键和范围同设置里的分析一组）。产出 nodes.<卡>.results.<项> = {status, verdict, direction, values, phrase, artifact}
    fields: [
      { key: 'items', label: ANALYSIS_FIELD_LABEL.items, type: 'analysisItems', help: ANALYSIS_TEXT.node.itemsHint },
      { key: 'overrides', label: ANALYSIS_FIELD_LABEL.overrides, type: 'analysisOverrides', help: ANALYSIS_TEXT.node.overridesHint },
      ...COMMON_TAIL,
    ],
    // 带一项空的：拖进画布就看得到一项要填什么（同口径卡带一个空指标）
    defaults: { items: [{ id: 'a1', params: {} }] },
  },
  dual_check: {
    type: 'dual_check', label: NODE_TYPE_LABEL.dual_check, category: '把关', icon: GitCompareArrows,
    description: DUAL_CHECK_TEXT.description,
    // 两路不一致时请人裁决，裁决可以驳回：普通出口之外有一个「驳回」出口，见 sourceHandles
    hasTarget: true, sources: null,
    // 配置的键和缺省见 types.ts 的 DualCheckConfig（期 3 约定）。口径卡、指标、口径说明要看画布上别的节点，
    // 盲算模型是接入加模型，启用是三态，都交给 DualCheckFields 画；type 只管默认值和校验时当什么看。
    // 盲算模型不放进高级选项：时间线上「模型相同」那一行的「打开设置」要定位到它，高级选项收着时定位不到
    fields: [
      { key: 'card', label: DUAL_CHECK_TEXT.field.card, type: 'select', help: DUAL_CHECK_TEXT.help.card, render: dualCheckFields.card },
      {
        key: 'metrics', label: DUAL_CHECK_TEXT.field.metrics, type: 'json', help: DUAL_CHECK_TEXT.help.metrics,
        render: dualCheckFields.metrics,
      },
      {
        key: 'definitions', label: DUAL_CHECK_TEXT.field.definitions, type: 'json', help: DUAL_CHECK_TEXT.help.definitions,
        render: dualCheckFields.definitions,
      },
      {
        key: 'enabled', label: DUAL_CHECK_TEXT.field.enabled, type: 'select', help: DUAL_CHECK_TEXT.help.enabled,
        render: dualCheckFields.enabled,
      },
      {
        // 没写和写 degrade 是一回事：选项从 degrade 排起，没写时下拉框落在它上面，正好是缺省
        key: 'on_reject', label: DUAL_CHECK_TEXT.field.on_reject, type: 'select', help: DUAL_CHECK_TEXT.help.on_reject,
        options: [
          { value: 'degrade', label: DUAL_CHECK_TEXT.onReject.degrade },
          { value: 'fail', label: DUAL_CHECK_TEXT.onReject.fail },
        ],
        unknownLabel: DUAL_CHECK_TEXT.onRejectUnknown,
      },
      { key: 'provider', label: DUAL_CHECK_TEXT.field.provider, type: 'select', render: dualCheckFields.provider },
      {
        key: 'model', label: DUAL_CHECK_TEXT.field.model, type: 'text', help: DUAL_CHECK_TEXT.help.model,
        render: dualCheckFields.model,
      },
      {
        key: 'tolerance', label: DUAL_CHECK_TEXT.field.tolerance, type: 'number', min: 0, advanced: true,
        help: DUAL_CHECK_TEXT.help.tolerance, render: dualCheckFields.tolerance,
      },
    ],
    defaults: {},
    summary: dualCheckSummary,
  },
  challenge: {
    type: 'challenge', label: NODE_TYPE_LABEL.challenge, category: '把关', icon: ShieldAlert,
    description: CHALLENGE_TEXT.description,
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    // 上限、模型、开关都可以不写：服务端按「设置 → 偏好设置 → 核查」里反方审稿那一组取（verify_settings）。
    // 没写的项不往配置里落默认值，设置改了，没单独配过的节点跟着变
    fields: [
      // 留空时先认直接连着的报告（「初稿 → 终稿 → 反方审稿」审终稿），同服务端 schema.challenge_report
      nodePickField({
        key: 'report', label: CHALLENGE_TEXT.field.report, nodeType: 'report', rule: 'direct', text: CHALLENGE_TEXT.reportPick,
      }),
      {
        key: 'max_claims', label: CHALLENGE_TEXT.field.max_claims, type: 'number', min: 1,
        placeholder: CHALLENGE_TEXT.followSettings, help: CHALLENGE_TEXT.help.max_claims,
      },
      // 和撰写报告的模型相同时提醒：同一个模型审自己写的结论，较难发现错误
      ...verifyModelFields({
        labels: { provider: CHALLENGE_TEXT.field.provider, model: CHALLENGE_TEXT.field.model },
        text: CHALLENGE_TEXT.model, subject: upstreamModel('report', 'report', 'direct'),
      }),
      {
        // 没写等于 degrade：降档放第一项，没写时下拉框正好落在它上面
        key: 'on_refuted', label: CHALLENGE_TEXT.field.on_refuted, type: 'select',
        options: [
          { value: 'degrade', label: CHALLENGE_TEXT.onRefuted.degrade },
          { value: 'flag', label: CHALLENGE_TEXT.onRefuted.flag },
          { value: 'fail', label: CHALLENGE_TEXT.onRefuted.fail },
        ],
        help: CHALLENGE_TEXT.help.on_refuted, unknownLabel: CHALLENGE_TEXT.unknownValue,
      },
      triStateField({ key: 'enabled', label: CHALLENGE_TEXT.field.enabled, text: CHALLENGE_TEXT.enabled }),
      {
        key: 'max_queries', label: CHALLENGE_TEXT.field.max_queries, type: 'number', min: 1, advanced: true,
        placeholder: CHALLENGE_TEXT.followSettings, help: CHALLENGE_TEXT.help.max_queries,
      },
      {
        key: 'timeout_s', label: CHALLENGE_TEXT.field.timeout_s, type: 'number', min: 1, advanced: true,
        placeholder: CHALLENGE_TEXT.followSettings, help: CHALLENGE_TEXT.help.timeout_s,
      },
    ],
    defaults: {},
  },
}

export const NODE_CATEGORIES = ['起止', '模型', '执行', '控制', '上下文', '把关']

/** branch / loop / human 的出口由配置决定，这里统一算出来。 */
export function sourceHandles(type: NodeType, config: Record<string, any>): HandleDef[] {
  const def = NODE_DEFS[type]
  // 不认识的类型给一个普通出口，连线还能接上。以前这里直接读 def.sources，
  // 一个未知类型就让画布连带整站白屏
  if (!def) return [{ id: 'out', label: '' }]
  if (def.sources) return def.sources
  if (type === 'branch') {
    const cases = (config.cases ?? []) as { key?: string; label?: string }[]
    // 出口颜色是中性的：ok 色只用来说「完成了」，哪条被走过由卡片按 taken 再点亮。
    // 同名出口只留第一个：key 重复时 React Flow 会出两个同 id 的 handle，跑完两条
    // 一起亮；重名本身由检查器和校验报错。key 用了保留名 default 的，和兜底出口
    // 合并成一个——运行时它们本来就是同一个出口（edge.taken 的 branch 都是 default）
    const seen = new Set<string>()
    const handles: HandleDef[] = []
    for (const c of cases) {
      const key = (c.key ?? '').trim()
      if (!key || seen.has(key)) continue
      seen.add(key)
      handles.push(key === 'default'
        ? { id: 'default', label: `${c.label || '其他'}（默认）`, color: 'var(--text-dim)' }
        : { id: key, label: c.label || key, color: 'var(--text-dim)' })
    }
    if (!seen.has('default')) handles.push({ id: 'default', label: '其他', color: 'var(--text-faint)' })
    return handles
  }
  if (type === 'loop') {
    return [
      { id: 'body', label: '循环体', color: 'var(--nt)' },
      { id: 'done', label: '结束', color: 'var(--text-faint)' },
    ]
  }
  if (type === 'human') {
    if (config.mode === 'approve') {
      return [
        { id: 'approved', label: '批准', color: 'var(--ok)' },
        REJECT_EXIT,
      ]
    }
    return [{ id: 'out', label: '' }, REJECT_EXIT]
  }
  if (canReject(type, config)) return [{ id: 'out', label: '' }, REJECT_EXIT]
  return [{ id: 'out', label: '' }]
}

/**
 * 「驳回」出口（docs/design/orchestration-round/00-总方案.md §3）：接了它，驳回（含超时驳回）时这一步正常结束、沿它往下走；
 * 没接，驳回让运行结束。和后端 engine/schema.REJECT_HANDLE 同一个 id。颜色是中性的：驳回是人的决定，不是故障
 */
export const REJECT_HANDLE = 'rejected'
const REJECT_EXIT: HandleDef = { id: REJECT_HANDLE, label: REJECTED_TEXT.exit, color: 'var(--text-dim)' }

/** 数据源的查询、看表结构：只读，不会请人审批（除非节点上明说了每次都审批） */
const READ_ONLY_TOOL = /^db_(?:query|schema)__/

/**
 * 这个节点会不会被人驳回，画布上给不给「驳回」出口（后端认的六类关卡：engine/schema.GATE_TYPES）。人工节点、双盲复算一律
 * 给（审批、补充输入、编辑稿件、裁决都能驳回）；沙箱代码只在审批策略是每次审批时问人；调用工具选了工具、审批策略不是
 * 从不审批时可能问人（工具调用审批、写入前的动作确认），数据源的查询只读，只在明说了每次审批时问。不给出口的节点上连着的
 * 驳回边，改配置时由 studio 收掉（出口跟着配置算）
 */
export function canReject(type: NodeType, config: Record<string, any>): boolean {
  if (type === 'human' || type === 'dual_check') return true
  if (type === 'code') return config.approval === 'always'
  if (type === 'tool') {
    if (config.approval === 'always') return true
    const tool = String(config.tool ?? '')
    return config.approval !== 'never' && !!tool && !READ_ONLY_TOOL.test(tool)
  }
  return false
}
