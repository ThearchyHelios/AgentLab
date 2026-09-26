import {
  Bot, Braces, Brain, CheckCircle2, Code2, Database, FileInput, FileOutput,
  Gauge, GitBranch, Hand, Repeat, Search, Shuffle, Users, Wrench,
} from 'lucide-react'
import { APPROVAL_POLICY_LABEL, NODE_TYPE_LABEL } from '../lib/terms'
import type { NodeType } from '../types'

export type FieldType =
  | 'text' | 'textarea' | 'prompt' | 'code' | 'number' | 'select' | 'switch'
  | 'json' | 'model' | 'tools' | 'skills' | 'collection'
  | 'ioFields' | 'cases' | 'agents' | 'metricsList'

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
  advanced?: boolean
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
}

const MODEL_FIELDS: FieldDef[] = [
  { key: 'model', label: '模型', type: 'model', help: '留空则用默认 provider' },
  {
    key: 'thinking', label: '思考模式', type: 'select', advanced: true,
    options: [
      { value: '', label: '跟随模型默认' },
      { value: 'summarized', label: '开启并显示摘要' },
      { value: 'adaptive', label: '开启但不显示' },
      { value: 'off', label: '关闭' },
    ],
    help: 'Claude 4.6+ 默认会先思考再回答，思考也消耗 token',
  },
  {
    key: 'effort', label: '投入程度', type: 'select', advanced: true,
    options: [
      { value: '', label: '默认' }, { value: 'low', label: 'low' },
      { value: 'medium', label: 'medium' }, { value: 'high', label: 'high' },
      { value: 'xhigh', label: 'xhigh' }, { value: 'max', label: 'max' },
    ],
  },
  {
    key: 'temperature', label: '温度', type: 'number', min: 0, max: 2, step: 0.1, advanced: true,
    help: 'Claude 4.6 之后的模型不支持，填了也会被自动忽略',
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
    description: '收集结构化成果；配出具契约后升级为三档出具（完整 / 降档 / 不予出具）',
    hasTarget: true, sources: [],
    fields: [
      { key: 'fields', label: '成果字段', type: 'ioFields' },
      {
        key: 'contract', label: '出具契约', type: 'json', syntax: 'template', advanced: true,
        help: '{"metrics_from":["口径卡节点id"],"narrative":"{{ vars.report }}",'
          + '"required":[…],"expected":[…],"allow_numbers":[…],"strict":false}。'
          + '配了之后叙述里的每个数字必须能回指指标集，否则降档或不予出具',
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
        help: '填了就强制模型按此 JSON Schema 返回',
      },
      { key: 'use_history', label: '带上对话历史', type: 'switch', advanced: true },
      ...COMMON_TAIL,
    ],
    defaults: { system: '', prompt: '{{ input.question }}' },
  },
  agent: {
    type: 'agent', label: NODE_TYPE_LABEL.agent, category: '模型', icon: Bot,
    description: '带工具循环，自己决定调什么工具',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      { key: 'system', label: '角色设定', type: 'textarea' },
      { key: 'prompt', label: '任务', type: 'prompt' },
      { key: 'tools', label: '可用工具', type: 'tools' },
      { key: 'skills', label: '挂载 Skill', type: 'skills' },
      { key: 'max_steps', label: '最大步数', type: 'number', min: 1, max: 100 },
      {
        key: 'approval', label: '审批策略', type: 'select', options: APPROVAL_OPTIONS,
        help: '需要审批时运行会暂停，等你在运行面板或记录页的审批卡上处理',
      },
      {
        key: 'parallel_tools', label: '并行执行工具', type: 'switch', advanced: true,
        help: '默认关闭：一轮只调一个工具，看到结果再想下一步。开启后一轮可发多个，更快，'
          + '但这一批中间没有新的思考',
      },
      ...MODEL_FIELDS,
      ...COMMON_TAIL,
    ],
    defaults: { max_steps: 12, tools: [], parallel_tools: false },
  },
  supervisor: {
    type: 'supervisor', label: NODE_TYPE_LABEL.supervisor, category: '模型', icon: Users,
    description: '调度者按进展把任务分派给多个专家',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      { key: 'goal', label: '团队目标', type: 'prompt' },
      { key: 'agents', label: '团队成员', type: 'agents' },
      { key: 'max_rounds', label: '最大轮次', type: 'number', min: 1, max: 25 },
      {
        key: 'max_parallel', label: '每轮最多同时派几人', type: 'number', min: 1, max: 6,
        help: '互不依赖的任务调度者可以放在同一轮同时执行，总耗时按最慢的那个算。'
          + '设为 1 即退回严格串行——并发的成员看到的是同一份进展快照，'
          + '任务其实有依赖却被同时派出去，两个人会基于一样的旧信息重复劳动',
      },
      {
        key: 'approval', label: '审批策略', type: 'select', advanced: true,
        options: [
          APPROVAL_FOLLOW,
          { value: 'dangerous', label: '需要审批的调用直接拦下' },
          { value: 'never', label: APPROVAL_POLICY_LABEL.never },
        ],
        help: '成员们并行跑在一个节点里，停不下来等人审批。默认把危险工具和可写库上的写操作'
          + '挡下来，并告诉成员换一种做法；需要逐次审批的事交给团队外的 agent 节点',
      },
      ...MODEL_FIELDS,
      ...COMMON_TAIL,
    ],
    defaults: { max_rounds: 6, max_parallel: 3, agents: [] },
  },
  tool: {
    type: 'tool', label: NODE_TYPE_LABEL.tool, category: '执行', icon: Wrench,
    description: '直接调用一个指定工具',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      { key: 'tool', label: '工具', type: 'tools', help: '只能选一个' },
      { key: 'args', label: '参数', type: 'json', syntax: 'template', help: '值里可以用 {{ }} 引用上游' },
      { key: 'approval', label: '审批策略', type: 'select', options: APPROVAL_OPTIONS },
      ...COMMON_TAIL,
    ],
    defaults: { args: {} },
  },
  code: {
    type: 'code', label: NODE_TYPE_LABEL.code, category: '执行', icon: Code2,
    description: '在隔离容器里执行代码',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      {
        key: 'language', label: '语言', type: 'select',
        options: [
          { value: 'python', label: 'Python' }, { value: 'bash', label: 'Bash' },
          { value: 'node', label: 'Node.js' },
        ],
      },
      { key: 'code', label: '代码', type: 'code', help: '代码里可以用 {{ }} 插值' },
      { key: 'timeout', label: '超时（秒）', type: 'number', min: 1, max: 300 },
      { key: 'memory_mb', label: '内存上限 MB', type: 'number', min: 64, max: 4096, advanced: true },
      { key: 'network', label: '允许联网', type: 'switch', help: '默认断网' },
      {
        key: 'isolation', label: '隔离档位', type: 'select',
        options: [
          { value: '', label: '跟随整机默认' },
          { value: 'strict', label: 'strict：microVM（独立内核，内存真限得住）' },
          { value: 'fast', label: 'fast：系统沙箱（低延迟，内存限不住）' },
        ],
        help: '不可信代码用 strict；自己写的、已审核的用 fast。两档都拦不住 DNS 出网，'
          + '真要防数据外泄得靠网络层。要的档位不可用时会退回默认并在时间线上告警',
      },
      {
        key: 'approval', label: '审批策略', type: 'select', advanced: true,
        // 代码节点不跟全局设置走（后端缺省就是 never），所以没有「跟随全局」
        options: [
          { value: 'never', label: APPROVAL_POLICY_LABEL.never },
          { value: 'always', label: `${APPROVAL_POLICY_LABEL.always}（审批时可改代码）` },
        ],
      },
      { key: 'fail_fast', label: '执行失败即中断', type: 'switch', advanced: true },
      ...COMMON_TAIL,
    ],
    defaults: { language: 'python', code: 'print("hello")', timeout: 30, network: false, fail_fast: true },
  },
  branch: {
    type: 'branch', label: NODE_TYPE_LABEL.branch, category: '控制', icon: GitBranch,
    description: '按条件或语义分类走不同的路',
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
    description: '把另一张工作流当成一个节点嵌进来',
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
        help: '留空 = 跟随这次运行的默认作用域（设置 · 运行默认值）',
      },
      { key: 'query', label: '回忆什么', type: 'prompt', when: (c) => c.action !== 'write' && c.action !== 'clear' },
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
        help: '留空 = 跟随这次运行的默认知识库（设置 · 运行默认值）',
      },
      { key: 'limit', label: '返回片段数', type: 'number', min: 1, max: 20 },
      {
        key: 'rerank', label: '重排', type: 'select', advanced: true,
        options: [
          { value: 'off', label: '不重排' },
          { value: 'model', label: '让模型重排（更准，多一次调用）' },
        ],
        help: '开了会先多捞一些候选，再让模型按"对回答这个问题有多大帮助"重新排序',
      },
      {
        key: 'alpha', label: '向量 vs 关键词', type: 'number', min: 0, max: 1, step: 0.1,
        help: '1 = 纯语义相似，0 = 纯关键词匹配。留空则跟着向量模型的能力走'
          + '——本地哈希向量没有语义泛化，给它权重反而会让命中率下降',
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
    description: '不调模型，直接把数据揉成下游要的形状',
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
    description: '暂停运行，等人审批、补充信息或改草稿',
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
      { key: 'message', label: '给人看的内容', type: 'prompt' },
      { key: 'draft', label: '草稿内容', type: 'prompt', when: (c) => c.mode === 'edit' },
      { key: 'stop_on_reject', label: '驳回即终止运行', type: 'switch', when: (c) => c.mode === 'approve' },
      ...COMMON_TAIL,
    ],
    defaults: { mode: 'approve', title: '需要你审批', message: '{{ last_message }}' },
  },
  validate: {
    type: 'validate', label: NODE_TYPE_LABEL.validate, category: '把关', icon: CheckCircle2,
    description: '按 JSON Schema 校验，不合格可让模型自动返工',
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
  metrics: {
    type: 'metrics', label: NODE_TYPE_LABEL.metrics, category: '把关', icon: Gauge,
    description: '受控指标集：所有算术在这里发生，叙述层只能引用',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      { key: 'caliber', label: '口径名称', type: 'text', syntax: 'template', placeholder: '周报口径' },
      { key: 'caliber_version', label: '口径版本', type: 'text', placeholder: 'v1' },
      { key: 'metrics', label: '指标定义', type: 'metricsList' },
      {
        key: 'assign_to', label: '结果存为变量', type: 'text',
        help: '叙述节点用 {{ nodes.节点id.text }} 引用指标清单',
      },
    ],
    defaults: {
      caliber: '', caliber_version: 'v1',
      metrics: [{ id: 'total', name: '', unit: '', expression: '' }],
    },
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
        ? { id: 'default', label: `${c.label || '其他'}（兜底）`, color: 'var(--text-dim)' }
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
        { id: 'approved', label: '通过', color: 'var(--ok)' },
        { id: 'rejected', label: '驳回', color: 'var(--err)' },
      ]
    }
    return [{ id: 'out', label: '' }]
  }
  return [{ id: 'out', label: '' }]
}
