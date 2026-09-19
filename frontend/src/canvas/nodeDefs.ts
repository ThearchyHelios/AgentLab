import {
  Bot, Braces, Brain, CheckCircle2, Code2, Database, FileInput, FileOutput,
  Gauge, GitBranch, Hand, Repeat, Search, Shuffle, Users, Wrench,
} from 'lucide-react'
import type { NodeType } from '../types'

export type FieldType =
  | 'text' | 'textarea' | 'prompt' | 'code' | 'number' | 'select' | 'switch'
  | 'json' | 'model' | 'tools' | 'skills' | 'collection'
  | 'ioFields' | 'cases' | 'agents' | 'metricsList'

export interface FieldDef {
  key: string
  label: string
  type: FieldType
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

const COMMON_TAIL: FieldDef[] = [
  {
    key: 'assign_to', label: '结果存为变量', type: 'text', placeholder: '例如 result',
    help: '下游用 {{ vars.变量名 }} 引用',
  },
  { key: 'skip_if', label: '跳过条件', type: 'text', advanced: true, placeholder: 'vars.count == 0' },
  { key: 'retries', label: '失败重试次数', type: 'number', min: 0, max: 5, advanced: true },
  {
    key: 'on_error', label: '出错时', type: 'select', advanced: true,
    options: [{ value: '', label: '中断整个运行' }, { value: 'continue', label: '记录错误并继续' }],
  },
]

export const NODE_DEFS: Record<NodeType, NodeDef> = {
  input: {
    type: 'input', label: '输入', category: '起止', icon: FileInput,
    description: '工作流入口，声明需要哪些输入',
    hasTarget: false, sources: [{ id: 'out', label: '' }],
    fields: [{ key: 'fields', label: '输入字段', type: 'ioFields' }],
    defaults: { fields: [{ name: 'question', required: true }] },
  },
  output: {
    type: 'output', label: '成果 / 出具', category: '起止', icon: FileOutput,
    description: '收集结构化成果；配出具契约后升级为三档出具',
    hasTarget: true, sources: [],
    fields: [
      { key: 'fields', label: '成果字段', type: 'ioFields' },
      {
        key: 'contract', label: '出具契约', type: 'json', advanced: true,
        help: '{"metrics_from":["口径卡节点id"],"narrative":"{{ vars.report }}",'
          + '"required":[…],"expected":[…],"allow_numbers":[…],"strict":false}。'
          + '配了之后叙述里的每个数字必须能回指指标集，否则降档或不予出具',
      },
    ],
    defaults: { fields: [{ name: '结果', value: '{{ last_message }}' }] },
  },
  llm: {
    type: 'llm', label: '模型调用', category: '模型', icon: Brain,
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
    type: 'agent', label: 'Agent', category: '模型', icon: Bot,
    description: '带工具循环，自己决定调什么工具',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      { key: 'system', label: '角色设定', type: 'textarea' },
      { key: 'prompt', label: '任务', type: 'prompt' },
      { key: 'tools', label: '可用工具', type: 'tools' },
      { key: 'skills', label: '挂载 Skill', type: 'skills' },
      { key: 'max_steps', label: '最大步数', type: 'number', min: 1, max: 25 },
      {
        key: 'approval', label: '工具审批', type: 'select',
        options: [
          { value: 'dangerous', label: '仅危险工具需要确认' },
          { value: 'always', label: '每次调用都确认' },
          { value: 'never', label: '全部自动放行' },
        ],
        help: '需要确认时运行会暂停，等你在审批面板放行',
      },
      {
        key: 'parallel_tools', label: '并行执行工具', type: 'switch', advanced: true,
        help: '默认关闭：一轮只调一个工具，看到结果再想下一步。开启后一轮可发多个，更快，'
          + '但这一批中间没有新的思考',
      },
      ...MODEL_FIELDS,
      ...COMMON_TAIL,
    ],
    defaults: { max_steps: 12, approval: 'dangerous', tools: [], parallel_tools: false },
  },
  supervisor: {
    type: 'supervisor', label: '多 Agent 协作', category: '模型', icon: Users,
    description: '调度者按进展把任务分派给多个专家',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      { key: 'goal', label: '团队目标', type: 'prompt' },
      { key: 'agents', label: '团队成员', type: 'agents' },
      { key: 'max_rounds', label: '最大轮次', type: 'number', min: 1, max: 25 },
      ...MODEL_FIELDS,
      ...COMMON_TAIL,
    ],
    defaults: { max_rounds: 6, agents: [] },
  },
  tool: {
    type: 'tool', label: '调用工具', category: '执行', icon: Wrench,
    description: '直接调用一个指定工具',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      { key: 'tool', label: '工具', type: 'tools', help: '只能选一个' },
      { key: 'args', label: '参数', type: 'json', help: '值里可以用 {{ }} 引用上游' },
      {
        key: 'approval', label: '执行前确认', type: 'select',
        options: [
          { value: 'dangerous', label: '危险工具需要确认' },
          { value: 'always', label: '总是确认' },
          { value: 'never', label: '不确认' },
        ],
      },
      ...COMMON_TAIL,
    ],
    defaults: { args: {}, approval: 'dangerous' },
  },
  code: {
    type: 'code', label: '沙箱代码', category: '执行', icon: Code2,
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
        key: 'approval', label: '执行前确认', type: 'select', advanced: true,
        options: [{ value: 'never', label: '不确认' }, { value: 'always', label: '总是确认（可改代码）' }],
      },
      { key: 'fail_fast', label: '执行失败即中断', type: 'switch', advanced: true },
      ...COMMON_TAIL,
    ],
    defaults: { language: 'python', code: 'print("hello")', timeout: 30, network: false, fail_fast: true },
  },
  branch: {
    type: 'branch', label: '条件分支', category: '控制', icon: GitBranch,
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
    type: 'loop', label: '循环', category: '控制', icon: Repeat,
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
        key: 'items', label: '列表来源', type: 'text', when: (c) => c.mode !== 'while',
        placeholder: '{{ input.items }}',
      },
      { key: 'item_var', label: '当前项变量名', type: 'text', when: (c) => c.mode !== 'while' },
      {
        key: 'condition', label: '继续条件', type: 'text', when: (c) => c.mode === 'while',
        placeholder: 'vars.done != true',
      },
      { key: 'max_iterations', label: '最大迭代次数', type: 'number', min: 1, max: 100 },
    ],
    defaults: { mode: 'foreach', item_var: 'item', max_iterations: 10 },
  },
  subgraph: {
    type: 'subgraph', label: '子工作流', category: '控制', icon: Braces,
    description: '把另一张工作流当成一个节点嵌进来',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      { key: 'workflow_id', label: '工作流', type: 'select', options: [] },
      { key: 'input', label: '传入参数', type: 'json' },
      ...COMMON_TAIL,
    ],
    defaults: { input: {} },
  },
  memory: {
    type: 'memory', label: '长期记忆', category: '上下文', icon: Database,
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
      { key: 'scope', label: '作用域', type: 'text', placeholder: 'default' },
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
    defaults: { action: 'recall', scope: 'default', limit: 5, query: '{{ last_message }}' },
  },
  retrieve: {
    type: 'retrieve', label: '知识检索', category: '上下文', icon: Search,
    description: '从知识库里混合检索相关片段',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      { key: 'query', label: '检索问题', type: 'prompt' },
      { key: 'collection', label: '知识库', type: 'collection' },
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
    defaults: { query: '{{ last_message }}', collection: 'default', limit: 5, rerank: 'off' },
  },
  transform: {
    type: 'transform', label: '数据整形', category: '上下文', icon: Shuffle,
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
        key: 'expression', label: '表达式', type: 'text', when: (c) => c.mode === 'expression',
        placeholder: "len(vars.items)",
      },
      ...COMMON_TAIL,
    ],
    defaults: { mode: 'template', template: '{{ last_message }}' },
  },
  human: {
    type: 'human', label: '人工介入', category: '把关', icon: Hand,
    description: '暂停运行，等人确认或补充信息',
    hasTarget: true, sources: null,
    fields: [
      {
        key: 'mode', label: '介入方式', type: 'select',
        options: [
          { value: 'approve', label: '批准 / 驳回' },
          { value: 'input', label: '补充输入' },
          { value: 'edit', label: '编辑草稿' },
        ],
      },
      { key: 'title', label: '标题', type: 'text' },
      { key: 'message', label: '给人看的内容', type: 'prompt' },
      { key: 'draft', label: '草稿内容', type: 'prompt', when: (c) => c.mode === 'edit' },
      { key: 'stop_on_reject', label: '驳回即终止运行', type: 'switch', when: (c) => c.mode === 'approve' },
      ...COMMON_TAIL,
    ],
    defaults: { mode: 'approve', title: '需要你确认', message: '{{ last_message }}' },
  },
  validate: {
    type: 'validate', label: '结构校验', category: '把关', icon: CheckCircle2,
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
    type: 'metrics', label: '口径卡', category: '把关', icon: Gauge,
    description: '受控指标集：所有算术在这里发生，叙述层只能引用',
    hasTarget: true, sources: [{ id: 'out', label: '' }],
    fields: [
      { key: 'caliber', label: '口径名称', type: 'text', placeholder: '周报口径' },
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
  if (def.sources) return def.sources
  if (type === 'branch') {
    const cases = (config.cases ?? []) as { key?: string; label?: string }[]
    return [
      ...cases
        .filter((c) => c.key)
        .map((c) => ({ id: c.key!, label: c.label || c.key!, color: 'var(--ok)' })),
      { id: 'default', label: '其他', color: 'var(--text-faint)' },
    ]
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
