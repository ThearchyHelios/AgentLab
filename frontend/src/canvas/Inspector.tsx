import { useEffect, useId, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { Link } from 'react-router-dom'
import { AlertTriangle, ChevronLeft, Copy, Lock, Maximize2, Plus, Trash2, X, XCircle } from 'lucide-react'
import clsx from 'clsx'
import { NODE_DEFS, syntaxOf, type FieldDef, type FieldSyntax, type NodeDef } from './nodeDefs'
import { fieldOfIssue, unboundToolOf, withToolBound, type FieldRef } from './issues'
import { hintOf } from './shortcuts'
import { isActivePhase } from '../run/trace'
import { EDIT_LOCK_TEXT, useEditLock, useStudio } from '../store/studio'
import { datasourceTools, modelOptions, useCatalog, useDatasources } from '../store/catalog'
import { api } from '../api/client'
import { IconButton, JsonInput, Modal, isComposing, useRadioGroup } from '../components/ui'
import { formatShortcut } from '../lib/keys'
import { SUBGRAPH_UPGRADE_HELP, upgradeNewerText } from '../lib/terms'
import { TemplateText } from './TemplateText'
import type { NodeType, ValidationIssue, WorkflowVersion } from '../types'

/** 检查器里一条落到字段上的问题 */
type FieldIssue = ValidationIssue & { at: FieldRef | null }

/**
 * 运行默认值（设置 · 运行默认值）。检查器里「留空 = 跟随默认」的字段要把实际会用
 * 哪个写出来，否则「留空」是个黑箱。整个会话取一次就够：这两项很少改。
 * 只有用得上的字段（记忆范围、知识库）才取：取回来时每个订阅的字段都要重渲染一次，
 * 取回来之后再挂上的直接用缓存
 */
type RunDefaults = { default_collection?: string; default_memory_scope?: string }
let runDefaults: Promise<RunDefaults> | null = null
let runDefaultsValue: RunDefaults | null = null
function useRunDefaults(active: boolean): RunDefaults {
  const [value, setValue] = useState<RunDefaults>(() => runDefaultsValue ?? {})
  useEffect(() => {
    if (!active || runDefaultsValue) return
    runDefaults ??= api.settings.get().then((s) => (runDefaultsValue = s?.run ?? {})).catch(() => {
      runDefaults = null   // 这次没取到，下次打开再试
      return {}
    })
    let alive = true
    void runDefaults.then((v) => { if (alive) setValue(v) })
    return () => { alive = false }
  }, [active])
  return runDefaultsValue ?? value
}

interface ToolOption { value: string; label: string; hint?: string; danger?: boolean; group?: string }

/**
 * 节点能绑的全部工具：目录里的，加上数据源的查询工具（db_query__<源> / db_schema__<源>）。
 * 后者不在 /tools 里，以前检查器里挑不到它们：校验说「提示词要求用 db_query__x，把它加进
 * 工具里」，界面上却做不到。数据源读 catalog 的那一份（问数据页、助手用的也是它），
 * 只有挂着工具选择器的地方订阅——别的字段不跟着目录重渲染
 */
function useToolOptions(): ToolOption[] {
  const tools = useCatalog((s) => s.tools)
  // 超过 30 秒没取过时挂上就后台重拉一次：刚在数据页加的源不能一直看不见
  const { list: sources } = useDatasources()
  return useMemo(() => {
    const known = new Set(tools.map((t) => t.id))
    const fromSources: ToolOption[] = sources
      .filter((r) => r.enabled !== false)
      .flatMap((r) => datasourceTools(r).map((t) => ({
        value: t, label: t, group: '数据源',
        hint: `${t.startsWith('db_schema__') ? '查表结构' : '执行 SQL'} · 数据源「${r.name}」`,
        // 可写的源上，写语句要人确认（运行时的审批关卡认它）
        danger: t.startsWith('db_query__') && r.readonly === false,
      })))
    return [
      ...tools.map((t) => ({ value: t.id, label: t.name, hint: t.description, danger: t.dangerous, group: t.category })),
      ...fromSources.filter((o) => !known.has(o.value)),
    ]
  }, [tools, sources])
}

/**
 * 工具字段。单独一个组件：只有它订阅工具目录和数据源，别的字段（提示词、模型、条件……）
 * 不因为目录刷新、数据源取回来而跟着重渲染
 */
function ToolsInput({ id, single, value, invalid, aria, onChange }: {
  id: string; single: boolean; value: any; invalid: boolean
  aria: { 'aria-invalid'?: boolean; 'aria-describedby'?: string }; onChange: (v: any) => void
}) {
  const options = useToolOptions()
  if (single) {
    return (
      <select id={id} className="field" value={value ?? ''} {...aria}
              style={invalid ? { borderColor: 'var(--err)' } : undefined}
              onChange={(e) => onChange(e.target.value)}>
        <option value="">— 选择工具 —</option>
        {[...new Set(options.map((o) => o.group))].map((g) => (
          <optgroup key={g} label={g}>
            {options.filter((o) => o.group === g).map((o) => (
              <option key={o.value} value={o.value}>{o.label}{o.danger ? ' ⚠' : ''}</option>
            ))}
          </optgroup>
        ))}
      </select>
    )
  }
  return <MultiPick options={options} value={Array.isArray(value) ? value : []} onChange={onChange} empty="没有可用工具" />
}

/** 不需要某张目录表的字段拿到的都是这一个空数组：选择器结果不变，就不重渲染 */
const NO_ROWS: never[] = []

/**
 * 从上游某一类节点里多选（报告的 metrics_from 只收上游口径卡）。
 *
 * 只列上游的：下游的卡在报告跑的时候还没算，选了也是空的（后端 validate 会报 error）。
 * 已经选了、后来被挪到下游或删掉的照样显示成芯片，好让人看见并移掉。
 * 全部移掉写回 undefined 而不是 []：后端两者都当「全部上游」，配置里不留一个空数组
 */
function NodeRefsInput({ nodeId, type, value, onChange }: {
  nodeId: string; type?: string; value: any; onChange: (v: any) => void
}) {
  const nodes = useStudio((s) => s.nodes)
  const edges = useStudio((s) => s.edges)
  const options = useMemo(() => {
    const parents = new Map<string, string[]>()
    for (const e of edges) parents.set(e.target, [...(parents.get(e.target) ?? []), e.source])
    const seen = new Set<string>()
    const stack = [...(parents.get(nodeId) ?? [])]
    while (stack.length) {
      const id = stack.pop()!
      if (seen.has(id) || id === nodeId) continue
      seen.add(id)
      stack.push(...(parents.get(id) ?? []))
    }
    return nodes
      .filter((n) => seen.has(n.id) && (!type || n.data?.nodeType === type))
      .map((n) => {
        const cfg = (n.data?.config ?? {}) as Record<string, any>
        const label = String(n.data?.label || n.id)
        const hint = type === 'metrics'
          ? [cfg.caliber && `${cfg.caliber} ${cfg.caliber_version || ''}`.trim(), `${(cfg.metrics ?? []).length} 个指标`]
            .filter(Boolean).join(' · ')
          : undefined
        return { value: n.id, label: label === n.id ? label : `${label}（${n.id}）`, hint }
      })
  }, [nodes, edges, nodeId, type])
  const list: string[] = Array.isArray(value) ? value.map(String) : typeof value === 'string' && value ? [value] : []
  return (
    <MultiPick
      options={options}
      value={list}
      onChange={(v) => onChange(v.length ? v : undefined)}
      addLabel={type === 'metrics' ? '选择口径卡' : '选择节点'}
      empty={type === 'metrics' ? '上游还没有口径卡：先在这个节点前面接一张口径卡' : '上游没有可选的节点'}
    />
  )
}

/**
 * 子工作流钉住版本后的升版处置：和口径卡的是同一个下拉——直接借口径卡的字段定义，选项、
 * 文案一字不差，只换说明里「和谁同一套」那半句。上游在钉住的那一版之后又发了版时，正式运行
 * 前必须声明，否则挡住（后端 governance.unresolved_caliber_upgrades 两者同一套规则）
 */
const SUBGRAPH_POLICY: FieldDef | null = (() => {
  const base = NODE_DEFS.metrics.fields.find((f) => f.key === 'upgrade_policy')
  return base ? {
    ...base, help: SUBGRAPH_UPGRADE_HELP,
    when: (c: Record<string, any>) => !!c.workflow_id && c.workflow_version != null && c.workflow_version !== '',
  } : null
})()

/** 这个节点要画哪些字段：子工作流在「工作流」后面插上升版处置，其余照 nodeDefs */
function fieldsOf(type: NodeType, def: NodeDef): FieldDef[] {
  if (type !== 'subgraph' || !SUBGRAPH_POLICY || def.fields.some((f) => f.key === 'upgrade_policy')) return def.fields
  const at = def.fields.findIndex((f) => f.key === 'workflow_id')
  return [...def.fields.slice(0, at + 1), SUBGRAPH_POLICY, ...def.fields.slice(at + 1)]
}

/** 属性面板。所有字段都由 nodeDefs 的声明驱动渲染，加节点类型不用改这里。 */
export function Inspector() {
  const selectedId = useStudio((s) => s.selectedId)
  const node = useStudio((s) => s.nodes.find((n) => n.id === s.selectedId))
  const allIssues = useStudio((s) => s.issues)
  // 助手在改、正式运行在跑：整块只读，说清为什么
  const lock = useEditLock()
  const locked = lock != null
  // 运行中（探索运行也算）不删节点：画布那边同样关了删除键，事件会对到一个已经不在的节点上
  const running = useStudio((s) => isActivePhase(s.runPhase))
  const runtime = useStudio((s) => (selectedId ? s.runtime[selectedId] : undefined))
  // 动作逐个取：不带选择器的 useStudio() 订阅整个 store，运行时每来一个 token 这块面板都要重渲染
  const updateNode = useStudio((s) => s.updateNode)
  const removeNode = useStudio((s) => s.removeNode)
  const duplicateNode = useStudio((s) => s.duplicateNode)
  const select = useStudio((s) => s.select)
  const [showAdvanced, setShowAdvanced] = useState(false)

  // 这张面板是盖在助手栏上的一层：节点没了（撤销、被删、换了图）就该自己让开，
  // 以前会剩一块只写着「选中一个节点来编辑它」的空白层盖住助手，还没有返回按钮
  const gone = !!selectedId && !node
  useEffect(() => { if (gone) select(null) }, [gone, select])

  const issues = useMemo<FieldIssue[]>(
    () => (node ? allIssues.filter((i) => i.node_id === node.id).map((i) => ({ ...i, at: fieldOfIssue(i, node) })) : []),
    [allIssues, node],
  )

  if (!node) return null

  const def = NODE_DEFS[node.data.nodeType]
  if (!def) {
    // 这个界面版本不认识的类型：给出实情和唯一能做的事，而不是读 def.fields 崩掉整页
    return (
      <div className="flex h-full flex-col">
        <Header onBack={() => select(null)} />
        <div className="flex flex-1 flex-col items-center justify-center gap-2 p-6 text-center">
          <div className="text-xs">不认识的节点类型「{node.data.nodeType}」</div>
          <div className="text-2xs text-faint">这个版本的界面编辑不了它，运行时后端也会拒绝这张工作流。</div>
          <button className="btn btn-sm" onClick={() => removeNode(node.id)}>删除这个节点</button>
        </div>
      </div>
    )
  }
  const config = node.data.config ?? {}
  const setConfig = (key: string, value: any) =>
    updateNode(node.id, { config: { ...config, [key]: value } })

  const visible = fieldsOf(node.data.nodeType, def).filter((f) => !f.when || f.when(config))
  const keys = new Set(visible.map((f) => f.key))
  const basic = visible.filter((f) => !f.advanced)
  const advanced = visible.filter((f) => f.advanced)
  const issuesOf = (key: string) => issues.filter((i) => i.at?.key === key)
  // 落不到任何一个看得见的字段上的，放在面板顶上；落在高级选项里的，高级选项自动展开
  const loose = issues.filter((i) => !i.at || !keys.has(i.at.key))
  const advancedHit = advanced.some((f) => issuesOf(f.key).length)
  const advancedOpen = showAdvanced || advancedHit

  return (
    <div className="flex h-full flex-col">
      <Header onBack={() => select(null)}>
        <def.icon size={13} style={{ color: 'var(--nt)' }} className={`nt-${node.data.nodeType} shrink-0`} />
        <span className="min-w-0 flex-1 truncate text-xs font-semibold">{def.label}</span>
        <IconButton label="复制节点" title={hintOf('复制一份', 'duplicate')} disabled={locked}
                    onClick={() => duplicateNode(node.id)} icon={<Copy size={12} />} />
        <IconButton label="删除节点" disabled={locked || running}
                    title={running && !locked ? '运行中不能删节点：等它结束或者先停下' : `${hintOf('删除节点', 'delete')} · 可撤销`}
                    onClick={() => removeNode(node.id)} icon={<Trash2 size={12} className="text-[var(--err)]" />} />
      </Header>

      {/* 助手改图、正式运行期间整块只读：前者改的东西会被它的最终结果悄悄覆盖，后者跑的是
          已发布的不可变版本 */}
      <fieldset disabled={locked} className="min-h-0 min-w-0 flex-1 overflow-y-auto p-3">
        {lock && (
          <div className="mb-3 flex items-start gap-1.5 rounded border px-2 py-1.5 text-2xs leading-snug"
               role="note" style={{ borderColor: 'var(--border-strong)', background: 'var(--bg-hover)' }}>
            <Lock size={11} className="mt-px shrink-0 text-dim" aria-hidden />
            <span className="min-w-0 flex-1">
              <span className="font-medium">只能看，不能改</span>
              <span className="text-dim"> · {EDIT_LOCK_TEXT[lock]}</span>
            </span>
          </div>
        )}
        <div className="mb-3 text-2xs leading-relaxed text-faint">{def.description}</div>

        {!!loose.length && (
          <div className="mb-3 space-y-1">
            {loose.map((issue, i) => <IssueLine key={i} issue={issue} boxed />)}
          </div>
        )}

        <div className="mb-3">
          <label className="label" htmlFor={`node-${node.id}-label`}>节点名称</label>
          <input
            id={`node-${node.id}-label`}
            className="field"
            value={node.data.label ?? ''}
            onChange={(e) => updateNode(node.id, { label: e.target.value })}
            placeholder={def.label}
          />
          <div className="mt-1 text-2xs text-faint">
            ID <code className="mono">{node.id}</code> · 下游用 <code className="mono">{`{{ nodes.${node.id}.text }}`}</code> 引用
          </div>
        </div>

        {basic.map((field) => (
          <Field key={field.key} field={field} nodeId={node.id} value={config[field.key]}
                 config={config} issues={issuesOf(field.key)} onChange={(v) => setConfig(field.key, v)} />
        ))}

        {!!advanced.length && (
          <div className="mt-3 border-t pt-3">
            <button
              type="button"
              className="mb-2 text-2xs text-faint hover:text-dim"
              aria-expanded={advancedOpen}
              onClick={() => setShowAdvanced(!advancedOpen)}
            >
              {advancedOpen ? '▾' : '▸'} 高级选项（{advanced.length}）
            </button>
            {advancedOpen &&
              advanced.map((field) => (
                <Field key={field.key} field={field} nodeId={node.id} value={config[field.key]}
                       config={config} issues={issuesOf(field.key)} onChange={(v) => setConfig(field.key, v)} />
              ))}
          </div>
        )}

        {runtime?.preview != null && (
          <div className="mt-4 border-t pt-3">
            <div className="label">上次运行输出</div>
            <pre className="mono max-h-60 overflow-auto rounded border bg-bg p-2 text-2xs leading-relaxed whitespace-pre-wrap break-words">
              {typeof runtime.preview === 'string'
                ? runtime.preview
                : JSON.stringify(runtime.preview, null, 2)}
            </pre>
          </div>
        )}
      </fieldset>
    </div>
  )
}

function Header({ onBack, children }: { onBack: () => void; children?: React.ReactNode }) {
  return (
    <div className="flex items-center gap-1.5 border-b px-2 py-2">
      {/* 这块是盖在助手栏上的一层。不给一个明确的"回去"，用户就不知道
          底下还有东西——只看到一个 ×，会以为关掉之后什么都没有了 */}
      <button
        type="button"
        className="flex items-center gap-0.5 rounded-md px-1 py-1 text-faint transition-colors hover:bg-hover hover:text-fg"
        title="返回助手（Esc）"
        onClick={onBack}
      >
        <ChevronLeft size={14} />
        <span className="text-2xs">助手</span>
      </button>
      <span className="mx-0.5 h-3.5 w-px shrink-0" style={{ background: 'var(--border)' }} />
      {children}
    </div>
  )
}

/** 一条问题。字段下面用行内版，面板顶部用带框的版本。action 是就地修好它的那个按钮 */
function IssueLine({ issue, boxed, action }: {
  issue: Pick<ValidationIssue, 'level' | 'message'>; boxed?: boolean; action?: React.ReactNode
}) {
  const err = issue.level === 'error'
  const Icon = err ? XCircle : AlertTriangle
  return (
    <div
      className={clsx('flex items-start gap-1.5 text-2xs leading-snug', boxed && 'rounded border px-2 py-1.5')}
      style={{
        color: err ? 'var(--err)' : 'var(--warn)',
        ...(boxed ? { borderColor: err ? 'var(--err)' : 'var(--warn)', background: err ? 'var(--st-failed-soft)' : 'var(--st-waiting-soft)' } : {}),
      }}
      role={err ? 'alert' : undefined}
    >
      <Icon size={11} className="mt-px shrink-0" aria-hidden />
      {/* 修法放在话的下面：检查器只有三百来像素宽，并排的话这句要折成七八行 */}
      <span className="min-w-0 flex-1">
        {issue.message}
        {action && <span className="mt-1 flex">{action}</span>}
      </span>
    </div>
  )
}

/**
 * 「提示词要求用 X，但没绑定」的就地修法：把 X 加进这一栏。返回新 config，已经绑了就是 null
 * （按钮不出现）。修法和问题面板那一行的「绑定」是同一个 withToolBound
 */
function bindFix(issue: FieldIssue, config: Record<string, any>, apply: (next: Record<string, any>) => void) {
  const tool = unboundToolOf(issue.message)
  const next = tool ? withToolBound(config, issue.at, tool) : null
  if (!tool || !next) return null
  return (
    <button type="button" className="btn btn-xs shrink-0" onClick={() => apply(next)}>
      <Plus size={10} aria-hidden /> 绑定 {tool}
    </button>
  )
}

// -------------------------------------------------------------------------

/**
 * 模板 / 表达式的标记。两种字段错法相反，以前外观一模一样，只能靠 placeholder 暗示；
 * 后端专门有一条「{{ }} 是多余的——这里是表达式」的告警，说明这种错很常见
 */
function SyntaxBadge({ syntax }: { syntax: FieldSyntax }) {
  if (syntax === 'plain') return null
  const template = syntax === 'template'
  return (
    <span
      className="mono inline-flex shrink-0 items-center gap-1 rounded border px-1 text-2xs leading-4 text-faint"
      style={{ borderColor: 'var(--hairline)' }}
      title={template
        ? '模板：用 {{ vars.x }} 引用上游。取不到值会渲染成空字符串、不报错，所以用补全别手敲'
        : '表达式：直接写 vars.x == 1 这样的裸路径，不要加 {{ }}'}
    >
      {template ? '{{ }}' : 'ƒx'}
      <span className="font-sans">{template ? '模板' : '表达式'}</span>
    </span>
  )
}

function Field({ field, nodeId, value, config, issues, onChange }: {
  field: FieldDef; nodeId: string; value: any; config: Record<string, any>
  issues: FieldIssue[]; onChange: (v: any) => void
}) {
  const id = `f-${nodeId}-${field.key}`
  const syntax = syntaxOf(field)
  const composite = field.type === 'cases' || field.type === 'agents' || field.type === 'ioFields'
    || field.type === 'metricsList' || field.type === 'caliberFrom'
  // 复合字段自己把问题落到第几项；落不到具体某一项的，和普通字段一样挂在下面
  const own = composite ? issues.filter((i) => i.at?.index == null) : issues
  const bad = own.some((i) => i.level === 'error')
  const errorId = own.length ? `${id}-issues` : undefined
  // 长文本在 360px 宽的栏里没法写：提示词、代码可以展开到大编辑器里（同一份值，边写边存）
  const expandable = (field.type === 'prompt' || field.type === 'code' || field.type === 'textarea') && syntax !== 'plain'
  const [wide, setWide] = useState(false)
  // 看得见、暂时用不了：说清差什么（比如先写 Schema 才能开 cite_fields）
  const off = field.disabled?.(config) ?? null
  return (
    <div className="mb-3" data-field={field.key} data-disabled={off ? '' : undefined}>
      {field.type !== 'switch' && (
        <div className="mb-1 flex items-center gap-1.5">
          <label className="label mb-0 min-w-0 flex-1 truncate" htmlFor={composite ? undefined : id}>{field.label}</label>
          <SyntaxBadge syntax={syntax} />
          {expandable && (
            <IconButton label={`展开编辑${field.label}`} title="展开到大编辑器" className="h-5 px-1"
                        onClick={() => setWide(true)} icon={<Maximize2 size={10} />} />
          )}
        </div>
      )}
      {wide && createPortal(
        <Modal open onClose={() => setWide(false)} title={field.label} width={880}
               footer={<button className="btn btn-primary" onClick={() => setWide(false)}>完成</button>}>
          <TemplateText id={`${id}-wide`} nodeId={nodeId} syntax="template" spellCheck={false}
                        className={clsx(field.type !== 'textarea' && 'mono', 'text-xs')} rows={22}
                        value={value ?? ''} placeholder={field.placeholder} onChange={onChange} />
          <div className="mt-2 text-2xs text-faint">改动直接写进画布，{formatShortcut('Mod+Z')} 可以撤回；打 {'{{'} 弹出这一步能取到的变量</div>
        </Modal>,
        document.body,
      )}
      <FieldInput field={field} id={id} nodeId={nodeId} syntax={syntax} value={value} config={config}
                  invalid={bad} describedBy={[errorId, off ? `${id}-off` : ''].filter(Boolean).join(' ') || undefined}
                  disabled={!!off} issues={issues} onChange={onChange} />
      {field.help && field.type !== 'switch' && (
        <div className="mt-1 text-2xs leading-snug text-faint">{field.help}</div>
      )}
      {off && <div id={`${id}-off`} className="mt-1 text-2xs leading-snug" style={{ color: 'var(--st-waiting)' }}>{off}</div>}
      {!!own.length && (
        <div id={errorId} className="mt-1 space-y-0.5">
          {own.map((i, k) => <IssueLine key={k} issue={i} action={bindFix(i, config, (c) => onChange(c[field.key]))} />)}
        </div>
      )}
    </div>
  )
}

function FieldInput({ field, id, nodeId, syntax, value, config, invalid, describedBy, disabled, issues, onChange }: {
  field: FieldDef; id: string; nodeId: string; syntax: FieldSyntax; value: any
  config: Record<string, any>; invalid: boolean; describedBy?: string
  /** 字段的 disabled 条件成立（原因写在字段下面，由 describedBy 接上） */
  disabled?: boolean
  issues: FieldIssue[]; onChange: (v: any) => void
}) {
  // 目录只按这个字段用得上的那一张订阅。以前每个字段都订阅整个 catalog：连接心跳、
  // 待审批轮询每跳一次，检查器里十几个字段全跟着重渲染
  const providers = useCatalog((s) => (field.type === 'model' ? s.providers : NO_ROWS))
  const collections = useCatalog((s) => (field.type === 'collection' ? s.collections : NO_ROWS))
  const skills = useCatalog((s) => (field.type === 'skills' ? s.skills : NO_ROWS))
  const defaults = useRunDefaults(field.key === 'scope' || field.type === 'collection')
  const aria = { 'aria-invalid': invalid || undefined, 'aria-describedby': describedBy }

  switch (field.type) {
    case 'text':
      if (syntax !== 'plain') {
        return (
          <TemplateText id={id} multiline={false} syntax={syntax} nodeId={nodeId}
                        value={value ?? ''} placeholder={field.key === 'scope'
                          ? `跟随运行默认（${defaults.default_memory_scope ?? 'default'}）` : field.placeholder}
                        invalid={invalid} describedBy={describedBy} onChange={onChange} />
        )
      }
      return (
        <input id={id} className="field" value={value ?? ''} placeholder={field.placeholder} {...aria}
               style={invalid ? { borderColor: 'var(--err)' } : undefined}
               onChange={(e) => onChange(e.target.value)} />
      )

    case 'number':
      return (
        <input
          id={id} className="field tnum" type="number" value={value ?? ''} min={field.min} max={field.max}
          step={field.step ?? 1} placeholder={field.placeholder} {...aria}
          onChange={(e) => onChange(e.target.value === '' ? undefined : Number(e.target.value))}
        />
      )

    // 这三种默认都过模板渲染，所以带 {{ }} 补全。syntax 显式写成 plain 的是长得像
    // 模板、后端却不渲染的字段——在那儿弹补全会是个谎
    case 'textarea':
    case 'prompt':
    case 'code':
      if (syntax === 'plain') {
        return <textarea id={id} className="field" rows={3} value={value ?? ''} placeholder={field.placeholder}
                         {...aria} onChange={(e) => onChange(e.target.value)} />
      }
      return (
        <TemplateText
          id={id} nodeId={nodeId} syntax={syntax === 'expression' ? 'expression' : 'template'}
          className={clsx((field.type === 'prompt' || field.type === 'code') && 'mono',
            field.type === 'code' ? 'text-2xs' : field.type === 'prompt' && 'text-xs')}
          rows={field.type === 'code' ? 10 : field.type === 'prompt' ? 4 : 3}
          spellCheck={false} invalid={invalid} describedBy={describedBy}
          value={value ?? ''} placeholder={field.placeholder}
          onChange={onChange}
        />
      )

    case 'select':
      if (field.key === 'workflow_id') {
        return <SubgraphPicker id={id} value={value} version={config.workflow_version} invalid={invalid}
                               describedBy={describedBy} onChange={onChange} />
      }
      return (
        <select id={id} className="field" value={value ?? ''} {...aria} onChange={(e) => onChange(e.target.value)}>
          {(field.options ?? []).map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
          {/* config 里是个不在选项里的值：照实列出来（不能选回去），不让它看着像第一个选项 */}
          {field.unknownLabel && typeof value === 'string' && value && !(field.options ?? []).some((o) => o.value === value) && (
            <option value={value} disabled>{field.unknownLabel(value)}</option>
          )}
        </select>
      )

    case 'switch':
      return (
        <label className={clsx('flex items-center gap-2 py-0.5', disabled ? 'cursor-not-allowed opacity-60' : 'cursor-pointer')}>
          <input id={id} type="checkbox" checked={!!value} disabled={disabled} aria-describedby={describedBy}
                 onChange={(e) => onChange(e.target.checked)} className="accent-[var(--accent)]" />
          <span className="text-xs">{field.label}</span>
          {field.help && <span className="text-2xs text-faint">· {field.help}</span>}
        </label>
      )

    case 'json':
      return syntax === 'template'
        ? <JsonTemplateInput id={id} nodeId={nodeId} value={value} placeholder={field.placeholder}
                             invalid={invalid} describedBy={describedBy} onChange={onChange} />
        : <JsonInput id={id} value={value} onChange={onChange} placeholder={field.placeholder} />

    case 'model': {
      const options = modelOptions(providers)
      const groups = [...new Set(options.map((o) => o.group))]
      return (
        <select id={id} className="field" value={value ?? ''} {...aria} onChange={(e) => onChange(e.target.value)}>
          <option value="">默认（用第一个可用 provider）</option>
          {groups.map((g) => (
            <optgroup key={g} label={g}>
              {options.filter((o) => o.group === g).map((o) => (
                <option key={g + o.value} value={o.value}>{o.label}</option>
              ))}
            </optgroup>
          ))}
        </select>
      )
    }

    case 'collection':
      return (
        <>
          <input id={id} className="field" list={`${id}-list`} value={value ?? ''} {...aria}
                 placeholder={`跟随运行默认（${defaults.default_collection ?? 'default'}）`}
                 onChange={(e) => onChange(e.target.value)} />
          <datalist id={`${id}-list`}>
            {collections.map((c) => (
              <option key={c.collection} value={c.collection}>{`${c.documents} 篇文档`}</option>
            ))}
          </datalist>
        </>
      )

    case 'skills':
      return (
        <MultiPick
          options={skills.filter((s) => s.enabled).map((s) => ({ value: s.name, label: s.name, hint: s.description }))}
          value={Array.isArray(value) ? value : []}
          onChange={onChange}
          addLabel="添加 Skill"
          empty={<>还没有可用的 Skill。<Link to="/knowledge/skills" className="underline underline-offset-2 hover:text-fg">
            去「知识 → 方法论 Skill」创建</Link></>}
        />
      )

    case 'tools':
      // tool 节点只选一个，agent 节点可以多选
      return <ToolsInput id={id} single={!!field.help?.includes('只能选一个')} value={value} invalid={invalid}
                         aria={aria} onChange={onChange} />

    case 'ioFields':
      return <IoFieldList nodeId={nodeId} value={value ?? []} issues={issues} onChange={onChange} />

    case 'cases':
      return <CaseList nodeId={nodeId} mode={config.mode} value={value ?? []} issues={issues} onChange={onChange} />

    case 'metricsList':
      return <MetricList nodeId={nodeId} value={value ?? []} issues={issues} onChange={onChange} />

    case 'nodeRefs':
      return <NodeRefsInput nodeId={nodeId} type={field.refType} value={value} onChange={onChange} />

    case 'caliberFrom':
      return <CaliberFromPicker id={id} value={value} invalid={invalid} describedBy={describedBy} />

    case 'agents':
      return <AgentList value={value ?? []} issues={issues} config={config} onChange={onChange} />

    default:
      return <input id={id} className="field" value={value ?? ''} onChange={(e) => onChange(e.target.value)} />
  }
}

/**
 * 值里能写 {{ }} 的 JSON（工具参数、子工作流入参、出具契约）。
 *
 * 以前是普通的 JsonInput：帮助文字说可以用 {{ }}，打出来却没有补全、没有高亮。
 * 输入过程中允许非法 JSON，合法时才回写——和 JsonInput 同一个规矩
 */
function JsonTemplateInput({ id, nodeId, value, placeholder, invalid, describedBy, onChange }: {
  id: string; nodeId: string; value: any; placeholder?: string; invalid: boolean
  describedBy?: string; onChange: (v: any) => void
}) {
  const [text, setText] = useState(() => (value == null ? '' : JSON.stringify(value, null, 2)))
  const [bad, setBad] = useState(false)
  const [editing, setEditing] = useState(false)
  useEffect(() => {
    if (!editing) setText(value == null ? '' : JSON.stringify(value, null, 2))
  }, [value, editing])
  return (
    <div onFocus={() => setEditing(true)} onBlur={() => setEditing(false)}>
      <TemplateText
        id={id} nodeId={nodeId} className="mono text-2xs" rows={5} spellCheck={false}
        value={text} placeholder={placeholder} invalid={invalid || bad} describedBy={describedBy}
        onChange={(t) => {
          setText(t)
          if (!t.trim()) { setBad(false); onChange(undefined); return }
          try {
            onChange(JSON.parse(t))
            setBad(false)
          } catch {
            setBad(true)
          }
        }}
      />
      {bad && <div className="mt-1 text-2xs text-[var(--err)]">JSON 格式不对，还没保存</div>}
    </div>
  )
}

/**
 * 子工作流：选哪一张、钉哪一版。
 *
 * 以前这是一个选项为空的下拉框，界面上根本选不了；受管门禁又要求子工作流钉住版本
 * （不钉的话口径会随上游最新版漂移），界面上也没地方钉。版本写进同一个节点的
 * workflow_version，留空 = 跟随最新
 */
function SubgraphPicker({ id, value, version, invalid, describedBy, onChange }: {
  id: string; value: any; version: any; invalid: boolean; describedBy?: string
  onChange: (v: any) => void
}) {
  const workflows = useCatalog((s) => s.workflows)
  const self = useStudio((s) => s.workflow?.id)
  const nodeId = useStudio((s) => s.selectedId)
  const updateNode = useStudio((s) => s.updateNode)
  const [versions, setVersions] = useState<WorkflowVersion[] | null>(null)
  useEffect(() => {
    setVersions(null)
    if (!value) return
    let alive = true
    api.workflows.versions(value).then((v) => { if (alive) setVersions(v) }).catch(() => { if (alive) setVersions([]) })
    return () => { alive = false }
  }, [value])
  const setVersion = (v: string) => {
    const node = nodeId ? useStudio.getState().nodes.find((n) => n.id === nodeId) : undefined
    if (!node) return
    const config = { ...node.data.config }
    if (v) config.workflow_version = Number(v)
    else {
      // 跟随最新就谈不上升版：处置跟着钉的版本走，不钉了一起拿掉（撤销能找回）
      delete config.workflow_version
      delete config.upgrade_policy
    }
    updateNode(node.id, { config })
  }
  const pinned = version != null && String(version) !== '' ? Number(version) : null
  const latest = versions?.length ? Math.max(...versions.map((v) => v.version)) : null
  return (
    <div className="space-y-1.5">
      <select id={id} className="field" value={value ?? ''} aria-invalid={invalid || undefined} aria-describedby={describedBy}
              style={invalid ? { borderColor: 'var(--err)' } : undefined}
              onChange={(e) => onChange(e.target.value)}>
        <option value="">— 选择要嵌套的工作流 —</option>
        {workflows.filter((w) => w.id !== self).map((w) => (
          <option key={w.id} value={w.id}>{w.name}{w.is_template ? '（模板）' : ''}</option>
        ))}
      </select>
      {!!value && (
        <div>
          <label className="mb-0.5 block text-2xs text-faint" htmlFor={`${id}-version`}>钉住版本</label>
          <select id={`${id}-version`} className="field" value={version ?? ''} disabled={!versions}
                  onChange={(e) => setVersion(e.target.value)}>
            <option value="">跟随最新（受管工作流不允许）</option>
            {(versions ?? []).map((v) => (
              <option key={v.id} value={v.version}>v{v.version}{v.note ? ` · ${v.note}` : ''}</option>
            ))}
          </select>
          {latest != null && pinned != null && latest > pinned && (
            <div className="mt-1 text-2xs leading-snug" style={{ color: 'var(--st-waiting)' }} data-subgraph-newer="">
              {upgradeNewerText(latest, pinned)}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * 口径卡从哪来：在这里定义，或者钉住另一个工作流某一版里的口径卡（caliber_from）。
 *
 * 钉住时选工作流、版本、那一版图里的口径卡节点，写成 {workflow_id, workflow_version, node_id}；
 * 本地的名称、版本、指标定义这时不生效（后端 validate 也这么说），切过去就清掉——撤销能找回。
 * 版本必须钉：不钉的话口径会跟着上游漂移。上游在钉住的那一版之后又发了版，旁边就提醒
 * 要在下面声明升版处置，否则挡住正式运行（和子工作流同一套）
 */
function CaliberFromPicker({ id, value, invalid, describedBy }: {
  id: string; value: any; invalid: boolean; describedBy?: string
}) {
  const workflows = useCatalog((s) => s.workflows)
  const self = useStudio((s) => s.workflow?.id)
  const nodeId = useStudio((s) => s.selectedId)
  const updateNode = useStudio((s) => s.updateNode)
  const ref = value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, any> : null
  const wfId: string = ref?.workflow_id ? String(ref.workflow_id) : ''
  const version: number | null = ref?.workflow_version != null && String(ref.workflow_version) !== ''
    ? Number(ref.workflow_version) : null
  const [versions, setVersions] = useState<WorkflowVersion[] | null>(null)
  const [graph, setGraph] = useState<{ key: string; nodes: any[] } | 'error' | null>(null)
  useEffect(() => {
    setVersions(null)
    if (!wfId) return
    let alive = true
    api.workflows.versions(wfId).then((v) => { if (alive) setVersions(v) }).catch(() => { if (alive) setVersions([]) })
    return () => { alive = false }
  }, [wfId])
  useEffect(() => {
    setGraph(null)
    if (!wfId || version == null || !Number.isFinite(version)) return
    let alive = true
    api.workflows.version(wfId, version).then(
      (v) => { if (alive) setGraph({ key: `${wfId}@${version}`, nodes: Array.isArray(v?.graph?.nodes) ? v.graph!.nodes : [] }) },
      () => { if (alive) setGraph('error') },
    )
    return () => { alive = false }
  }, [wfId, version])

  /** 改这个节点的配置：钉住的来源和本地定义是互斥的两套 */
  const write = (patch: Record<string, any>, drop: string[] = []) => {
    const node = nodeId ? useStudio.getState().nodes.find((n) => n.id === nodeId) : undefined
    if (!node) return
    const config = { ...node.data.config, ...patch }
    for (const k of drop) delete config[k]
    updateNode(node.id, { config })
  }
  const mode: 'local' | 'pinned' = ref ? 'pinned' : 'local'
  const setMode = (m: 'local' | 'pinned') => {
    if (m === mode) return
    if (m === 'pinned') write({ caliber_from: {} }, ['caliber', 'caliber_version', 'metrics'])
    else write({ caliber_version: 'v1', metrics: [{ id: 'total', name: '', unit: '', expression: '' }] },
      ['caliber_from', 'upgrade_policy'])
  }
  const radio = useRadioGroup(CALIBER_MODES, mode, setMode)
  const cards = graph && graph !== 'error' ? graph.nodes.filter((n) => (n?.type ?? n?.data?.nodeType) === 'metrics') : []
  const cardOf = (n: any) => ({ id: String(n.id), label: n.data?.label || n.title || '', config: n.data?.config ?? n.config ?? {} })
  const chosen = cards.map(cardOf).find((c) => c.id === ref?.node_id)
  const latest = versions?.length ? Math.max(...versions.map((v) => v.version)) : null
  // 那一版里只有一张口径卡：直接钉上，不让人多点一次
  useEffect(() => {
    if (!ref || ref.node_id || cards.length !== 1) return
    const only = cardOf(cards[0])
    if (!only.config.caliber_from) write({ caliber_from: { ...ref, node_id: only.id } })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph])

  return (
    <div className="space-y-1.5" data-caliber-from={mode}>
      <div role="radiogroup" aria-label="口径卡从哪来" className="inline-flex rounded-md border p-px" aria-describedby={describedBy}>
        {CALIBER_MODES.map((m) => (
          <button key={m} type="button" {...radio(m)} onClick={() => setMode(m)}
                  className={clsx('rounded px-2 py-0.5 text-2xs', mode === m ? 'bg-accent-soft text-fg' : 'text-faint hover:text-dim')}>
            {m === 'local' ? '在这里定义' : '钉住别的工作流里的口径卡'}
          </button>
        ))}
      </div>
      {ref && (
        <>
          <select id={id} className="field" value={wfId} aria-label="钉住哪个工作流" aria-invalid={invalid || undefined}
                  style={invalid && !wfId ? { borderColor: 'var(--err)' } : undefined}
                  onChange={(e) => write({ caliber_from: e.target.value ? { workflow_id: e.target.value } : {} })}>
            <option value="">— 选择工作流 —</option>
            {workflows.filter((w) => w.id !== self).map((w) => (
              <option key={w.id} value={w.id}>{w.name}{w.is_template ? '（模板）' : ''}</option>
            ))}
          </select>
          {!!wfId && (
            <div>
              <label className="mb-0.5 block text-2xs text-faint" htmlFor={`${id}-version`}>钉住版本（必须钉：不钉口径会跟着上游漂移）</label>
              <select id={`${id}-version`} className="field" value={version ?? ''} disabled={!versions}
                      onChange={(e) => write({ caliber_from: { workflow_id: wfId, ...(e.target.value ? { workflow_version: Number(e.target.value) } : {}) } })}>
                <option value="">{versions ? (versions.length ? '— 选择版本 —' : '这个工作流还没有保存过版本') : '正在取版本…'}</option>
                {(versions ?? []).map((v) => (
                  <option key={v.id} value={v.version}>v{v.version}{v.published ? '（已发布）' : ''}{v.note ? ` · ${v.note}` : ''}</option>
                ))}
              </select>
            </div>
          )}
          {!!wfId && version != null && (
            <div>
              <label className="mb-0.5 block text-2xs text-faint" htmlFor={`${id}-node`}>那一版里的哪张口径卡</label>
              <select id={`${id}-node`} className="field" value={ref.node_id ?? ''} disabled={!graph || graph === 'error'}
                      onChange={(e) => write({ caliber_from: { workflow_id: wfId, workflow_version: version, ...(e.target.value ? { node_id: e.target.value } : {}) } })}>
                <option value="">{graph === 'error' ? '这一版取不到' : !graph ? '正在取这一版的图…' : cards.length ? '— 选择口径卡 —' : '这一版里没有口径卡'}</option>
                {cards.map(cardOf).map((c) => (
                  // 它自己也是钉住别处的：定义不在这一版里，不能再往下钉
                  <option key={c.id} value={c.id} disabled={!!c.config.caliber_from}>
                    {c.label || c.id} · 口径「{c.config.caliber || '—'}」{c.config.caliber_version ?? ''}
                    {c.config.caliber_from ? '（它也是钉住别处的，不能再钉）' : ''}
                  </option>
                ))}
              </select>
            </div>
          )}
          {chosen && (
            <div className="rounded border bg-bg px-2 py-1.5 text-2xs leading-relaxed" data-caliber-preview="">
              <div>
                口径「{chosen.config.caliber || '—'}」<span className="mono">{chosen.config.caliber_version ?? ''}</span>
                {' · '}{Array.isArray(chosen.config.metrics) ? chosen.config.metrics.length : 0} 个指标
              </div>
              {Array.isArray(chosen.config.metrics) && chosen.config.metrics.length > 0 && (
                <div className="mono text-faint [overflow-wrap:anywhere]">
                  {chosen.config.metrics.slice(0, 8).map((m: any) => m?.id).filter(Boolean).join('、')}
                  {chosen.config.metrics.length > 8 ? ' …' : ''}
                </div>
              )}
            </div>
          )}
          {latest != null && version != null && latest > version && (
            <div className="text-2xs leading-snug" style={{ color: 'var(--st-waiting)' }} data-caliber-newer="">
              {upgradeNewerText(latest, version)}
            </div>
          )}
        </>
      )}
    </div>
  )
}

const CALIBER_MODES = ['local', 'pinned'] as const

// -------------------------------------------------------------------------
// 复合编辑器
// -------------------------------------------------------------------------

function MultiPick({ options, value, onChange, empty, addLabel = '添加工具' }: {
  options: { value: string; label: string; hint?: string; danger?: boolean; group?: string }[]
  value: string[]; onChange: (v: string[]) => void; empty: React.ReactNode
  /** 展开按钮上写添加的是什么：只写「添加」时，一个检查器里有两个多选就分不清 */
  addLabel?: string
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const toggle = (v: string) =>
    onChange(value.includes(v) ? value.filter((x) => x !== v) : [...value, v])

  const filtered = options.filter(
    (o) => !query || o.label.toLowerCase().includes(query.toLowerCase()) ||
      (o.hint ?? '').toLowerCase().includes(query.toLowerCase()),
  )

  return (
    <div>
      <div className="mb-1.5 flex flex-wrap gap-1">
        {value.map((v) => {
          const opt = options.find((o) => o.value === v)
          return (
            <span key={v} className="chip" style={{ borderColor: 'var(--border-strong)', color: 'var(--text)' }}>
              {opt?.label ?? v}
              <button type="button" onClick={() => toggle(v)} className="ml-0.5 hover:opacity-60"
                      aria-label={`移除 ${opt?.label ?? v}`}><X size={9} /></button>
            </span>
          )
        })}
        {!value.length && <span className="text-2xs text-faint">未选择</span>}
      </div>
      {/* 定位（revealField）把光标放在这儿，不放在前面那排芯片的 × 上 */}
      <button type="button" className="btn btn-sm w-full justify-center" onClick={() => setOpen(!open)} data-reveal-focus>
        <Plus size={11} /> {open ? '收起' : addLabel}
      </button>
      {open && (
        <div className="mt-1.5 rounded border bg-bg">
          <input className="field rounded-b-none border-0 border-b" placeholder="搜索…"
                 value={query} onChange={(e) => setQuery(e.target.value)} autoFocus />
          <div className="max-h-52 overflow-y-auto p-1">
            {!filtered.length && (
              <div className="px-2 py-3 text-center text-2xs text-faint">
                {options.length ? `没有匹配「${query.trim()}」的` : empty}
              </div>
            )}
            {filtered.map((o) => (
              <button
                type="button"
                key={o.value}
                onClick={() => toggle(o.value)}
                className={clsx(
                  'flex w-full items-start gap-2 rounded px-2 py-1.5 text-left hover:bg-hover',
                  value.includes(o.value) && 'bg-hover',
                )}
              >
                <input type="checkbox" readOnly checked={value.includes(o.value)} tabIndex={-1}
                       className="mt-0.5 accent-[var(--accent)]" />
                <span className="min-w-0 flex-1">
                  <span className="block text-xs">
                    {o.label}
                    {o.danger && <span className="ml-1 text-2xs text-[var(--warn)]">需审批</span>}
                  </span>
                  {o.hint && <span className="block truncate text-2xs text-faint">{o.hint}</span>}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function Row({ children, onRemove, removeLabel, tone, item }: {
  children: React.ReactNode; onRemove: () => void; removeLabel: string
  tone?: 'error' | 'warning'
  /** 第几项。问题面板定位时按 [data-field] [data-item] 滚到这一项 */
  item?: number
}) {
  return (
    <div className="mb-1.5 rounded border bg-bg p-2" data-item={item}
         style={tone ? { borderColor: tone === 'error' ? 'var(--err)' : 'var(--warn)' } : undefined}>
      <div className="flex items-start gap-1.5">
        <div className="min-w-0 flex-1 space-y-1.5">{children}</div>
        <IconButton label={removeLabel} className="shrink-0" onClick={onRemove}
                    icon={<Trash2 size={11} className="text-[var(--err)]" />} />
      </div>
    </div>
  )
}

/** 行内的小标签：填满之后还分得清哪个框是什么 */
function Sub({ label, htmlFor, children, syntax, className, sub }: {
  label: string; htmlFor: string; children: React.ReactNode; syntax?: FieldSyntax; className?: string
  /** 这一栏在项里的键（condition、value…）：定位按 [data-item] [data-sub] 落到这儿、光标放进来 */
  sub?: string
}) {
  return (
    <div className={className} data-sub={sub}>
      <div className="mb-0.5 flex items-center gap-1.5">
        <label htmlFor={htmlFor} className="min-w-0 flex-1 truncate text-2xs text-faint">{label}</label>
        {syntax && <SyntaxBadge syntax={syntax} />}
      </div>
      {children}
    </div>
  )
}

function IoFieldList({ nodeId, value, issues, onChange }: {
  nodeId: string; value: any[]; issues: FieldIssue[]; onChange: (v: any[]) => void
}) {
  const uid = useId()
  const update = (i: number, patch: any) =>
    onChange(value.map((f, idx) => (idx === i ? { ...f, ...patch } : f)))
  // 有 value 的是成果字段（output），有 required 的是输入字段
  const isOutput = value.some((f) => 'value' in f)

  return (
    <div>
      {value.map((field, i) => {
        const own = issues.filter((x) => x.at?.index === i)
        return (
          <Row key={i} item={i} removeLabel={`删除字段 ${field.name || i + 1}`}
               tone={own.some((x) => x.level === 'error') ? 'error' : own.length ? 'warning' : undefined}
               onRemove={() => onChange(value.filter((_, idx) => idx !== i))}>
            <Sub label="字段名" htmlFor={`${uid}-${i}-name`} sub="name">
              <input id={`${uid}-${i}-name`} className="field" value={field.name ?? ''}
                     onChange={(e) => update(i, { name: e.target.value })} />
            </Sub>
            {isOutput ? (
              // 成果字段是写 {{ }} 最多的地方，以前却是普通输入框，敲 {{ 没有任何弹出
              <Sub label="取值" htmlFor={`${uid}-${i}-value`} syntax="template" sub="value">
                <TemplateText id={`${uid}-${i}-value`} multiline={false} nodeId={nodeId}
                              className="mono text-xs" placeholder="{{ vars.xxx }}"
                              value={field.value ?? ''} onChange={(v) => update(i, { value: v })} />
              </Sub>
            ) : (
              <>
                <Sub label="说明（可选）" htmlFor={`${uid}-${i}-desc`} sub="description">
                  <input id={`${uid}-${i}-desc`} className="field" value={field.description ?? ''}
                         onChange={(e) => update(i, { description: e.target.value })} />
                </Sub>
                <div className="flex items-end gap-3">
                  <label className="flex items-center gap-1.5 pb-1.5 text-2xs">
                    <input type="checkbox" checked={!!field.required} className="accent-[var(--accent)]"
                           onChange={(e) => update(i, { required: e.target.checked })} />
                    必填
                  </label>
                  <Sub label="默认值" htmlFor={`${uid}-${i}-def`} className="flex-1" sub="default">
                    <input id={`${uid}-${i}-def`} className="field" value={field.default ?? ''}
                           onChange={(e) => update(i, { default: e.target.value })} />
                  </Sub>
                </div>
              </>
            )}
            {own.map((x, k) => <IssueLine key={k} issue={x} />)}
          </Row>
        )
      })}
      <button type="button" className="btn btn-sm w-full justify-center"
              onClick={() => onChange([...value, isOutput ? { name: '', value: '' } : { name: '', required: false }])}>
        <Plus size={11} /> 添加字段
      </button>
    </div>
  )
}

/**
 * 分支标识的校验，和后端 schema._check_case_keys 同一套规矩：
 * 空和重复是 error（连不出边 / 永远轮不到）；default 是 warning——它是「其他」兜底
 * 出口的保留名，两者会合并成同一个出口，跑完分不清走的是哪条。
 * 这是给存量数据的；新写的标识在 keyRefusal 那一关就拦下了，落不进画布。
 */
function caseKeyIssue(value: any[], i: number): { level: 'error' | 'warning'; message: string } | null {
  const key = String(value[i]?.key ?? '').trim()
  if (!key) return { level: 'error', message: '还没填标识：连不出边，也走不到它' }
  const first = value.findIndex((c) => String(c?.key ?? '').trim() === key)
  if (first !== i) return { level: 'error', message: `和第 ${first + 1} 个分支的标识重复：路由只认标识，这一个永远轮不到` }
  if (key === 'default') {
    return { level: 'warning', message: 'default 是「其他」兜底出口的保留名：这个分支会和兜底合并成一个出口，跑完分不清走的是哪条' }
  }
  return null
}

/** 新写的标识能不能落进画布。空、default、和别的分支重名都不行 */
function keyRefusal(value: any[], i: number, key: string): string | null {
  if (!key) return '标识不能为空：连不出边，也走不到它'
  if (key === 'default') return 'default 是「其他」兜底出口的保留名，不能拿来当分支标识'
  const other = value.findIndex((c, j) => j !== i && String(c?.key ?? '').trim() === key)
  if (other >= 0) return `和第 ${other + 1} 个分支的标识重复：路由只认标识，这一个永远轮不到`
  return null
}

function CaseList({ nodeId, mode, value, issues, onChange }: {
  nodeId: string; mode?: string; value: any[]; issues: FieldIssue[]; onChange: (v: any[]) => void
}) {
  const uid = useId()
  /**
   * 正在改的那个标识，离开输入框（或回车）才落进画布。
   *
   * 出口是按标识算出来的：写到一半的空标识、重名、default 一旦落进 store，那个出口就没了，
   * 连在它上面的线会被一起删掉（updateNode 不留指向不存在出口的脏边）——想把 team 改成
   * crew，先删光再重打，线就断了。所以中间态只留在输入框里，只有合规的才落地
   */
  const [draft, setDraft] = useState<{ i: number; text: string } | null>(null)
  /** 刚才没落地的那一次：为什么退回去了 */
  const [refused, setRefused] = useState<{ i: number; text: string } | null>(null)
  const update = (i: number, patch: any) =>
    onChange(value.map((c, idx) => (idx === i ? { ...c, ...patch } : c)))
  const byModel = mode === 'llm'
  const used = () => new Set(value.map((c) => String(c?.key ?? '').trim()))
  const nextKey = () => {
    const taken = used()
    let n = value.length + 1
    while (taken.has(`case_${n}`)) n++
    return `case_${n}`
  }
  // 存量数据里的 default 一键改名。⑥ 模板里那个分支就是「协作」，team 最贴切
  const freeKey = () => (used().has('team') ? nextKey() : 'team')
  const commit = (i: number) => {
    if (draft?.i !== i) return
    const key = draft.text.trim()
    setDraft(null)
    if (key === String(value[i]?.key ?? '').trim()) return
    const why = keyRefusal(value, i, key)
    if (why) setRefused({ i, text: `没有改成「${key || '空'}」：${why}` })
    else update(i, { key })
  }
  return (
    <div>
      {value.map((c, i) => {
        const editing = draft?.i === i
        const live = editing ? keyRefusal(value, i, draft.text.trim()) : null
        const note = refused?.i === i ? refused.text : null
        // 输入框里有草稿或者刚退回过时，只说眼下这件事；存量数据的问题等它落定了再说
        const keyIssue = editing || note ? null : caseKeyIssue(value, i)
        // 标识的问题前端即时判（上面这套和后端同口径），后端那几条就不重复列了
        const own = issues.filter((x) => x.at?.index === i && x.at?.sub !== 'key'
          && !/标识/.test(x.message))
        const keyBad = !!live || !!note || keyIssue?.level === 'error'
        const tone = keyBad || own.some((x) => x.level === 'error') ? 'error'
          : keyIssue || own.length ? 'warning' : undefined
        const condBad = own.some((x) => x.at?.sub === 'condition' && x.level === 'error')
        return (
          <Row key={i} item={i} removeLabel={`删除分支 ${c.label || c.key || i + 1}`} tone={tone}
               onRemove={() => onChange(value.filter((_, idx) => idx !== i))}>
            <div className="flex gap-1.5">
              <Sub label="标识（连线出口）" htmlFor={`${uid}-${i}-key`} className="w-[42%] shrink-0" sub="key">
                <input id={`${uid}-${i}-key`} className="field mono text-xs"
                       value={editing ? draft.text : c.key ?? ''}
                       aria-invalid={keyBad || undefined}
                       style={keyBad ? { borderColor: 'var(--err)' } : keyIssue ? { borderColor: 'var(--warn)' } : undefined}
                       onChange={(e) => { setRefused(null); setDraft({ i, text: e.target.value }) }}
                       onBlur={() => commit(i)}
                       onKeyDown={(e) => {
                         if (isComposing(e)) return
                         if (e.key === 'Enter') { e.preventDefault(); commit(i) }
                         else if (e.key === 'Escape' && editing) {
                           // 只撤掉草稿，别让外面的检查器也跟着收起
                           e.preventDefault()
                           e.stopPropagation()
                           setDraft(null)
                         }
                       }} />
              </Sub>
              <Sub label={byModel ? '类别说明（给模型看）' : '说明'} htmlFor={`${uid}-${i}-label`} className="min-w-0 flex-1" sub="label">
                <input id={`${uid}-${i}-label`} className="field" value={c.label ?? ''}
                       onChange={(e) => update(i, { label: e.target.value })} />
              </Sub>
            </div>
            {(live || note) && <IssueLine issue={{ level: 'error', message: live ?? note! }} />}
            {keyIssue && (
              <div className="flex items-start gap-1.5">
                <div className="min-w-0 flex-1"><IssueLine issue={keyIssue} /></div>
                {String(c.key ?? '').trim() === 'default' && (
                  <button type="button" className="btn btn-xs shrink-0"
                          onClick={() => update(i, { key: freeKey() })}>
                    改成 {freeKey()}
                  </button>
                )}
              </div>
            )}
            {/* 让模型分类时条件不参与判断，别让人以为要填 */}
            {!byModel && (
              <Sub label="条件" htmlFor={`${uid}-${i}-cond`} syntax="expression" sub="condition">
                <TemplateText id={`${uid}-${i}-cond`} multiline={false} syntax="expression" nodeId={nodeId}
                              className="text-xs" placeholder="len(vars.text) > 100" invalid={condBad}
                              value={c.condition ?? ''} onChange={(v) => update(i, { condition: v })} />
              </Sub>
            )}
            {own.map((x, k) => <IssueLine key={k} issue={x} />)}
          </Row>
        )
      })}
      <button type="button" className="btn btn-sm w-full justify-center"
              onClick={() => onChange([...value, { key: nextKey(), condition: '', label: '' }])}>
        <Plus size={11} /> 添加分支
      </button>
      <div className="mt-1.5 text-2xs leading-snug text-faint">
        {byModel ? '模型按「类别说明」分类；' : '从上往下判断，第一个成立的条件胜出；'}
        都不满足时走「其他」出口，记得给它连一条。标识改完按回车或点别处才生效
      </div>
    </div>
  )
}

function MetricList({ nodeId, value, issues, onChange }: {
  nodeId: string; value: any[]; issues: FieldIssue[]; onChange: (v: any[]) => void
}) {
  const uid = useId()
  const update = (i: number, patch: any) =>
    onChange(value.map((m, idx) => (idx === i ? { ...m, ...patch } : m)))
  return (
    <div>
      {value.map((m, i) => {
        const own = issues.filter((x) => x.at?.index === i)
        return (
          <Row key={i} item={i} removeLabel={`删除指标 ${m.id || i + 1}`}
               tone={own.some((x) => x.level === 'error') ? 'error' : own.length ? 'warning' : undefined}
               onRemove={() => onChange(value.filter((_, idx) => idx !== i))}>
            <div className="flex gap-1.5">
              <Sub label="指标 id（英文）" htmlFor={`${uid}-${i}-id`} className="min-w-0 flex-1" sub="id">
                <input id={`${uid}-${i}-id`} className="field mono text-xs" value={m.id ?? ''}
                       onChange={(e) => update(i, { id: e.target.value })} />
              </Sub>
              <Sub label="名称" htmlFor={`${uid}-${i}-name`} className="min-w-0 flex-1" sub="name">
                <input id={`${uid}-${i}-name`} className="field" value={m.name ?? ''}
                       onChange={(e) => update(i, { name: e.target.value })} />
              </Sub>
              <Sub label="单位" htmlFor={`${uid}-${i}-unit`} className="w-16 shrink-0" sub="unit">
                <input id={`${uid}-${i}-unit`} className="field" value={m.unit ?? ''}
                       onChange={(e) => update(i, { unit: e.target.value })} />
              </Sub>
            </div>
            <Sub label="计算" htmlFor={`${uid}-${i}-expr`} syntax="expression" sub="expression">
              <TemplateText id={`${uid}-${i}-expr`} multiline={false} syntax="expression" nodeId={nodeId}
                            className="text-xs" placeholder="round(vars.agg.amount / vars.agg.orders, 2)"
                            value={m.expression ?? ''} onChange={(v) => update(i, { expression: v })} />
            </Sub>
            {own.map((x, k) => <IssueLine key={k} issue={x} />)}
          </Row>
        )
      })}
      <button type="button" className="btn btn-sm w-full justify-center"
              onClick={() => onChange([...value, { id: '', name: '', unit: '', expression: '' }])}>
        <Plus size={11} /> 添加指标
      </button>
      <div className="mt-1.5 text-2xs leading-snug text-faint">
        所有算术都发生在这里（确定性、可复算）。叙述节点只许引用这份清单里的数，
        出具时会逐个数字回指校验。
      </div>
    </div>
  )
}

/**
 * 协作成员。成员自己的问题（提示词点名的工具没绑、角色设定写得不对）落在那个成员的那一栏下面：
 * 以前它们既不在成员行里、也不在面板顶上，问题面板说有错，点过来却什么都看不见
 */
function AgentList({ value, issues, config, onChange }: {
  value: any[]; issues: FieldIssue[]; config: Record<string, any>; onChange: (v: any[]) => void
}) {
  const options = useToolOptions()
  const uid = useId()
  const update = (i: number, patch: any) =>
    onChange(value.map((a, idx) => (idx === i ? { ...a, ...patch } : a)))
  const fix = (x: FieldIssue) => bindFix(x, config, (c) => onChange(c.agents))
  return (
    <div>
      {value.map((agent, i) => {
        const own = issues.filter((x) => x.at?.index === i)
        const at = (sub: string) => own.filter((x) => x.at?.sub === sub)
        const rest = own.filter((x) => x.at?.sub !== 'system' && x.at?.sub !== 'tools')
        return (
          <Row key={i} item={i} removeLabel={`删除成员 ${agent.name || i + 1}`}
               tone={own.some((x) => x.level === 'error') ? 'error' : own.length ? 'warning' : undefined}
               onRemove={() => onChange(value.filter((_, idx) => idx !== i))}>
            <div className="flex gap-1.5">
              <Sub label="成员名（英文）" htmlFor={`${uid}-${i}-name`} className="min-w-0 flex-1" sub="name">
                <input id={`${uid}-${i}-name`} className="field mono text-xs" placeholder="researcher"
                       value={agent.name ?? ''} onChange={(e) => update(i, { name: e.target.value })} />
              </Sub>
              <Sub label="最多几步" htmlFor={`${uid}-${i}-steps`} className="w-20 shrink-0" sub="max_steps">
                <input id={`${uid}-${i}-steps`} className="field tnum" type="number" min={1} max={30} placeholder="4"
                       value={agent.max_steps ?? ''}
                       onChange={(e) => update(i, { max_steps: e.target.value === '' ? undefined : Number(e.target.value) })} />
              </Sub>
            </div>
            <Sub label="职责（调度者据此分派）" htmlFor={`${uid}-${i}-desc`} sub="description">
              <input id={`${uid}-${i}-desc`} className="field" value={agent.description ?? ''}
                     onChange={(e) => update(i, { description: e.target.value })} />
            </Sub>
            {/* 成员的 system 后端直接取值、不过模板渲染：这里不挂补全，免得教人写 {{ }} */}
            <div data-sub="system">
              <Sub label="角色设定（system，原样发给模型）" htmlFor={`${uid}-${i}-sys`}>
                <textarea id={`${uid}-${i}-sys`} className="field" rows={2}
                          value={agent.system ?? ''} onChange={(e) => update(i, { system: e.target.value })} />
              </Sub>
              {at('system').map((x, k) => <IssueLine key={k} issue={x} />)}
            </div>
            <div data-sub="tools">
              <div className="mb-0.5 text-2xs text-faint">可用工具</div>
              <MultiPick options={options} value={agent.tools ?? []} onChange={(v) => update(i, { tools: v })}
                         empty="没有可用工具" />
              {!!at('tools').length && (
                <div className="mt-1 space-y-0.5">
                  {at('tools').map((x, k) => <IssueLine key={k} issue={x} action={fix(x)} />)}
                </div>
              )}
            </div>
            {rest.map((x, k) => <IssueLine key={k} issue={x} />)}
          </Row>
        )
      })}
      <button type="button" className="btn btn-sm w-full justify-center"
              onClick={() => onChange([...value, { name: '', description: '', system: '', tools: [] }])}>
        <Plus size={11} /> 添加成员
      </button>
    </div>
  )
}
