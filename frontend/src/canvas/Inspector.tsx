import { useEffect, useId, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { AlertTriangle, ChevronLeft, Copy, Maximize2, Plus, Trash2, X, XCircle } from 'lucide-react'
import clsx from 'clsx'
import { NODE_DEFS, syntaxOf, type FieldDef, type FieldSyntax } from './nodeDefs'
import { fieldOfIssue, type FieldRef } from './issues'
import { hintOf } from './shortcuts'
import { useStudio } from '../store/studio'
import { useCatalog, modelOptions } from '../store/catalog'
import { api } from '../api/client'
import { IconButton, JsonInput, Modal, isComposing } from '../components/ui'
import { formatShortcut } from '../lib/keys'
import { TemplateText } from './TemplateText'
import type { ValidationIssue, WorkflowVersion } from '../types'

/** 检查器里一条落到字段上的问题 */
type FieldIssue = ValidationIssue & { at: FieldRef | null }

/**
 * 运行默认值（设置 · 运行默认值）。检查器里「留空 = 跟随默认」的字段要把实际会用
 * 哪个写出来，否则「留空」是个黑箱。整个会话取一次就够：这两项很少改
 */
let runDefaults: Promise<{ default_collection?: string; default_memory_scope?: string }> | null = null
function useRunDefaults() {
  const [value, setValue] = useState<{ default_collection?: string; default_memory_scope?: string }>({})
  useEffect(() => {
    runDefaults ??= api.settings.get().then((s) => s?.run ?? {}).catch(() => {
      runDefaults = null   // 这次没取到，下次打开再试
      return {}
    })
    let alive = true
    void runDefaults.then((v) => { if (alive) setValue(v) })
    return () => { alive = false }
  }, [])
  return value
}

/** 属性面板。所有字段都由 nodeDefs 的声明驱动渲染，加节点类型不用改这里。 */
export function Inspector() {
  const selectedId = useStudio((s) => s.selectedId)
  const node = useStudio((s) => s.nodes.find((n) => n.id === s.selectedId))
  const allIssues = useStudio((s) => s.issues)
  const locked = useStudio((s) => s.copilot.active)
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

  const visible = def.fields.filter((f) => !f.when || f.when(config))
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
        <IconButton label="删除节点" title={`${hintOf('删除节点', 'delete')} · 可撤销`} disabled={locked}
                    onClick={() => removeNode(node.id)} icon={<Trash2 size={12} className="text-[var(--err)]" />} />
      </Header>

      {/* 助手改图期间整块只读：这时候改的东西会被它的最终结果悄悄覆盖 */}
      <fieldset disabled={locked} className="min-h-0 min-w-0 flex-1 overflow-y-auto p-3">
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

/** 一条问题。字段下面用行内版，面板顶部用带框的版本 */
function IssueLine({ issue, boxed }: { issue: Pick<ValidationIssue, 'level' | 'message'>; boxed?: boolean }) {
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
      <span className="min-w-0 flex-1">{issue.message}</span>
    </div>
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
    || field.type === 'metricsList'
  // 复合字段自己把问题落到第几项；落不到具体某一项的，和普通字段一样挂在下面
  const own = composite ? issues.filter((i) => i.at?.index == null) : issues
  const bad = own.some((i) => i.level === 'error')
  const errorId = own.length ? `${id}-issues` : undefined
  // 长文本在 360px 宽的栏里没法写：提示词、代码可以展开到大编辑器里（同一份值，边写边存）
  const expandable = (field.type === 'prompt' || field.type === 'code' || field.type === 'textarea') && syntax !== 'plain'
  const [wide, setWide] = useState(false)
  return (
    <div className="mb-3" data-field={field.key}>
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
                  invalid={bad} describedBy={errorId} issues={issues} onChange={onChange} />
      {field.help && field.type !== 'switch' && (
        <div className="mt-1 text-2xs leading-snug text-faint">{field.help}</div>
      )}
      {!!own.length && (
        <div id={errorId} className="mt-1 space-y-0.5">
          {own.map((i, k) => <IssueLine key={k} issue={i} />)}
        </div>
      )}
    </div>
  )
}

function FieldInput({ field, id, nodeId, syntax, value, config, invalid, describedBy, issues, onChange }: {
  field: FieldDef; id: string; nodeId: string; syntax: FieldSyntax; value: any
  config: Record<string, any>; invalid: boolean; describedBy?: string
  issues: FieldIssue[]; onChange: (v: any) => void
}) {
  const catalog = useCatalog()
  const defaults = useRunDefaults()
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
        </select>
      )

    case 'switch':
      return (
        <label className="flex cursor-pointer items-center gap-2 py-0.5">
          <input id={id} type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)}
                 className="accent-[var(--accent)]" />
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
      const options = modelOptions(catalog.providers)
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
            {catalog.collections.map((c) => (
              <option key={c.collection} value={c.collection}>{`${c.documents} 篇文档`}</option>
            ))}
          </datalist>
        </>
      )

    case 'skills':
      return (
        <MultiPick
          options={catalog.skills.filter((s) => s.enabled).map((s) => ({ value: s.name, label: s.name, hint: s.description }))}
          value={Array.isArray(value) ? value : []}
          onChange={onChange}
          empty="还没有 Skill，去「方法论」页创建"
        />
      )

    case 'tools': {
      const options = catalog.tools.map((t) => ({
        value: t.id, label: t.name, hint: t.description, danger: t.dangerous, group: t.category,
      }))
      // tool 节点只选一个，agent 节点可以多选
      if (field.help?.includes('只能选一个')) {
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
      return (
        <MultiPick options={options} value={Array.isArray(value) ? value : []} onChange={onChange}
                   empty="没有可用工具" />
      )
    }

    case 'ioFields':
      return <IoFieldList nodeId={nodeId} value={value ?? []} issues={issues} onChange={onChange} />

    case 'cases':
      return <CaseList nodeId={nodeId} mode={config.mode} value={value ?? []} issues={issues} onChange={onChange} />

    case 'metricsList':
      return <MetricList nodeId={nodeId} value={value ?? []} issues={issues} onChange={onChange} />

    case 'agents':
      return <AgentList value={value ?? []} onChange={onChange} />

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
    else delete config.workflow_version
    updateNode(node.id, { config })
  }
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
        </div>
      )}
    </div>
  )
}

// -------------------------------------------------------------------------
// 复合编辑器
// -------------------------------------------------------------------------

function MultiPick({ options, value, onChange, empty }: {
  options: { value: string; label: string; hint?: string; danger?: boolean; group?: string }[]
  value: string[]; onChange: (v: string[]) => void; empty: string
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
      <button type="button" className="btn btn-sm w-full justify-center" onClick={() => setOpen(!open)}>
        <Plus size={11} /> {open ? '收起' : '添加'}
      </button>
      {open && (
        <div className="mt-1.5 rounded border bg-bg">
          <input className="field rounded-b-none border-0 border-b" placeholder="搜索…"
                 value={query} onChange={(e) => setQuery(e.target.value)} autoFocus />
          <div className="max-h-52 overflow-y-auto p-1">
            {!filtered.length && <div className="px-2 py-3 text-center text-2xs text-faint">{empty}</div>}
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

function Row({ children, onRemove, removeLabel, tone }: {
  children: React.ReactNode; onRemove: () => void; removeLabel: string
  tone?: 'error' | 'warning'
}) {
  return (
    <div className="mb-1.5 rounded border bg-bg p-2"
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
function Sub({ label, htmlFor, children, syntax, className }: {
  label: string; htmlFor: string; children: React.ReactNode; syntax?: FieldSyntax; className?: string
}) {
  return (
    <div className={className}>
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
          <Row key={i} removeLabel={`删除字段 ${field.name || i + 1}`}
               tone={own.some((x) => x.level === 'error') ? 'error' : own.length ? 'warning' : undefined}
               onRemove={() => onChange(value.filter((_, idx) => idx !== i))}>
            <Sub label="字段名" htmlFor={`${uid}-${i}-name`}>
              <input id={`${uid}-${i}-name`} className="field" value={field.name ?? ''}
                     onChange={(e) => update(i, { name: e.target.value })} />
            </Sub>
            {isOutput ? (
              // 成果字段是写 {{ }} 最多的地方，以前却是普通输入框，敲 {{ 没有任何弹出
              <Sub label="取值" htmlFor={`${uid}-${i}-value`} syntax="template">
                <TemplateText id={`${uid}-${i}-value`} multiline={false} nodeId={nodeId}
                              className="mono text-xs" placeholder="{{ vars.xxx }}"
                              value={field.value ?? ''} onChange={(v) => update(i, { value: v })} />
              </Sub>
            ) : (
              <>
                <Sub label="说明（可选）" htmlFor={`${uid}-${i}-desc`}>
                  <input id={`${uid}-${i}-desc`} className="field" value={field.description ?? ''}
                         onChange={(e) => update(i, { description: e.target.value })} />
                </Sub>
                <div className="flex items-end gap-3">
                  <label className="flex items-center gap-1.5 pb-1.5 text-2xs">
                    <input type="checkbox" checked={!!field.required} className="accent-[var(--accent)]"
                           onChange={(e) => update(i, { required: e.target.checked })} />
                    必填
                  </label>
                  <Sub label="默认值" htmlFor={`${uid}-${i}-def`} className="flex-1">
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
          <Row key={i} removeLabel={`删除分支 ${c.label || c.key || i + 1}`} tone={tone}
               onRemove={() => onChange(value.filter((_, idx) => idx !== i))}>
            <div className="flex gap-1.5">
              <Sub label="标识（连线出口）" htmlFor={`${uid}-${i}-key`} className="w-[42%] shrink-0">
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
              <Sub label={byModel ? '类别说明（给模型看）' : '说明'} htmlFor={`${uid}-${i}-label`} className="min-w-0 flex-1">
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
              <Sub label="条件" htmlFor={`${uid}-${i}-cond`} syntax="expression">
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
          <Row key={i} removeLabel={`删除指标 ${m.id || i + 1}`}
               tone={own.some((x) => x.level === 'error') ? 'error' : own.length ? 'warning' : undefined}
               onRemove={() => onChange(value.filter((_, idx) => idx !== i))}>
            <div className="flex gap-1.5">
              <Sub label="指标 id（英文）" htmlFor={`${uid}-${i}-id`} className="min-w-0 flex-1">
                <input id={`${uid}-${i}-id`} className="field mono text-xs" value={m.id ?? ''}
                       onChange={(e) => update(i, { id: e.target.value })} />
              </Sub>
              <Sub label="名称" htmlFor={`${uid}-${i}-name`} className="min-w-0 flex-1">
                <input id={`${uid}-${i}-name`} className="field" value={m.name ?? ''}
                       onChange={(e) => update(i, { name: e.target.value })} />
              </Sub>
              <Sub label="单位" htmlFor={`${uid}-${i}-unit`} className="w-16 shrink-0">
                <input id={`${uid}-${i}-unit`} className="field" value={m.unit ?? ''}
                       onChange={(e) => update(i, { unit: e.target.value })} />
              </Sub>
            </div>
            <Sub label="计算" htmlFor={`${uid}-${i}-expr`} syntax="expression">
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

function AgentList({ value, onChange }: { value: any[]; onChange: (v: any[]) => void }) {
  const catalog = useCatalog()
  const uid = useId()
  const update = (i: number, patch: any) =>
    onChange(value.map((a, idx) => (idx === i ? { ...a, ...patch } : a)))
  return (
    <div>
      {value.map((agent, i) => (
        <Row key={i} removeLabel={`删除成员 ${agent.name || i + 1}`}
             onRemove={() => onChange(value.filter((_, idx) => idx !== i))}>
          <div className="flex gap-1.5">
            <Sub label="成员名（英文）" htmlFor={`${uid}-${i}-name`} className="min-w-0 flex-1">
              <input id={`${uid}-${i}-name`} className="field mono text-xs" placeholder="researcher"
                     value={agent.name ?? ''} onChange={(e) => update(i, { name: e.target.value })} />
            </Sub>
            <Sub label="最多几步" htmlFor={`${uid}-${i}-steps`} className="w-20 shrink-0">
              <input id={`${uid}-${i}-steps`} className="field tnum" type="number" min={1} max={30} placeholder="4"
                     value={agent.max_steps ?? ''}
                     onChange={(e) => update(i, { max_steps: e.target.value === '' ? undefined : Number(e.target.value) })} />
            </Sub>
          </div>
          <Sub label="职责（调度者据此分派）" htmlFor={`${uid}-${i}-desc`}>
            <input id={`${uid}-${i}-desc`} className="field" value={agent.description ?? ''}
                   onChange={(e) => update(i, { description: e.target.value })} />
          </Sub>
          {/* 成员的 system 后端直接取值、不过模板渲染：这里不挂补全，免得教人写 {{ }} */}
          <Sub label="角色设定（system，原样发给模型）" htmlFor={`${uid}-${i}-sys`}>
            <textarea id={`${uid}-${i}-sys`} className="field" rows={2}
                      value={agent.system ?? ''} onChange={(e) => update(i, { system: e.target.value })} />
          </Sub>
          <div>
            <div className="mb-0.5 text-2xs text-faint">可用工具</div>
            <MultiPick
              options={catalog.tools.map((t) => ({ value: t.id, label: t.name, hint: t.description, group: t.category }))}
              value={agent.tools ?? []} onChange={(v) => update(i, { tools: v })} empty="没有可用工具"
            />
          </div>
        </Row>
      ))}
      <button type="button" className="btn btn-sm w-full justify-center"
              onClick={() => onChange([...value, { name: '', description: '', system: '', tools: [] }])}>
        <Plus size={11} /> 添加成员
      </button>
    </div>
  )
}
