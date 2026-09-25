import { useMemo, useState } from 'react'
import { ChevronLeft, Copy, Plus, Settings2, Trash2, X } from 'lucide-react'
import clsx from 'clsx'
import { NODE_DEFS, type FieldDef } from './nodeDefs'
import { useStudio } from '../store/studio'
import { useCatalog, modelOptions } from '../store/catalog'
import { JsonInput } from '../components/ui'
import { TemplateText } from './TemplateText'

/** 属性面板。所有字段都由 nodeDefs 的声明驱动渲染，加节点类型不用改这里。 */
export function Inspector() {
  const selectedId = useStudio((s) => s.selectedId)
  const node = useStudio((s) => s.nodes.find((n) => n.id === s.selectedId))
  const allIssues = useStudio((s) => s.issues)
  const issues = useMemo(
    () => allIssues.filter((i) => i.node_id === selectedId),
    [allIssues, selectedId],
  )
  const runtime = useStudio((s) => (selectedId ? s.runtime[selectedId] : undefined))
  const { updateNode, removeNode, duplicateNode, select } = useStudio()
  const [showAdvanced, setShowAdvanced] = useState(false)

  if (!node) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center">
        <Settings2 size={22} className="text-faint opacity-40" />
        <div className="text-xs text-faint">选中一个节点来编辑它</div>
      </div>
    )
  }

  const def = NODE_DEFS[node.data.nodeType]
  if (!def) {
    // 这个界面版本不认识的类型：给出实情和唯一能做的事，而不是读 def.fields 崩掉整页
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center">
        <div className="text-xs">不认识的节点类型「{node.data.nodeType}」</div>
        <div className="text-[11px] text-faint">这个版本的界面编辑不了它，运行时后端也会拒绝这张图。</div>
        <button className="btn btn-sm" onClick={() => removeNode(node.id)}>删除这个节点</button>
      </div>
    )
  }
  const config = node.data.config ?? {}
  const setConfig = (key: string, value: any) =>
    updateNode(node.id, { config: { ...config, [key]: value } })

  const visible = def.fields.filter((f) => !f.when || f.when(config))
  const basic = visible.filter((f) => !f.advanced)
  const advanced = visible.filter((f) => f.advanced)

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-1.5 border-b px-2 py-2">
        {/* 这块是盖在助手栏上的一层。不给一个明确的"回去"，用户就不知道
            底下还有东西——只看到一个 ×，会以为关掉之后什么都没有了 */}
        <button
          className="flex items-center gap-0.5 rounded-md px-1 py-1 text-faint transition-colors hover:bg-hover hover:text-fg"
          title="返回助手（Esc）"
          onClick={() => select(null)}
        >
          <ChevronLeft size={14} />
          <span className="text-[10px]">助手</span>
        </button>
        <span className="mx-0.5 h-3.5 w-px shrink-0" style={{ background: 'var(--border)' }} />
        <def.icon size={13} style={{ color: 'var(--nt)' }} className={`nt-${node.data.nodeType}`} />
        <span className="flex-1 truncate text-xs font-semibold">{def.label}</span>
        <button className="btn btn-ghost btn-sm" title="复制节点" onClick={() => duplicateNode(node.id)}>
          <Copy size={12} />
        </button>
        <button className="btn btn-ghost btn-sm" title="删除节点" onClick={() => removeNode(node.id)}>
          <Trash2 size={12} className="text-[var(--err)]" />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-3">
        <div className="mb-3 text-[11px] leading-relaxed text-faint">{def.description}</div>

        {!!issues.length && (
          <div className="mb-3 space-y-1">
            {issues.map((issue, i) => (
              <div
                key={i}
                className="rounded border px-2 py-1.5 text-[11px] leading-snug"
                style={{
                  borderColor: issue.level === 'error' ? 'var(--err)' : 'var(--warn)',
                  color: issue.level === 'error' ? 'var(--err)' : 'var(--warn)',
                }}
              >
                {issue.message}
              </div>
            ))}
          </div>
        )}

        <div className="mb-3">
          <label className="label">节点名称</label>
          <input
            className="field"
            value={node.data.label ?? ''}
            onChange={(e) => updateNode(node.id, { label: e.target.value })}
            placeholder={def.label}
          />
          <div className="mt-1 text-[10px] text-faint">
            ID: <code className="mono">{node.id}</code> · 下游用 {`{{ nodes.${node.id}.text }}`} 引用
          </div>
        </div>

        {basic.map((field) => (
          <Field key={field.key} field={field} value={config[field.key]} onChange={(v) => setConfig(field.key, v)} />
        ))}

        {!!advanced.length && (
          <div className="mt-3 border-t pt-3">
            <button
              className="mb-2 text-[11px] text-faint hover:text-dim"
              onClick={() => setShowAdvanced(!showAdvanced)}
            >
              {showAdvanced ? '▾' : '▸'} 高级选项（{advanced.length}）
            </button>
            {showAdvanced &&
              advanced.map((field) => (
                <Field key={field.key} field={field} value={config[field.key]} onChange={(v) => setConfig(field.key, v)} />
              ))}
          </div>
        )}

        {runtime?.preview != null && (
          <div className="mt-4 border-t pt-3">
            <div className="label">上次运行输出</div>
            <pre className="mono max-h-60 overflow-auto rounded border bg-bg p-2 text-[10.5px] leading-relaxed whitespace-pre-wrap break-words">
              {typeof runtime.preview === 'string'
                ? runtime.preview
                : JSON.stringify(runtime.preview, null, 2)}
            </pre>
          </div>
        )}
      </div>
    </div>
  )
}

// -------------------------------------------------------------------------

function Field({ field, value, onChange }: {
  field: FieldDef; value: any; onChange: (v: any) => void
}) {
  return (
    <div className="mb-3">
      {field.type !== 'switch' && <label className="label">{field.label}</label>}
      <FieldInput field={field} value={value} onChange={onChange} />
      {field.help && field.type !== 'switch' && (
        <div className="mt-1 text-[10px] leading-snug text-faint">{field.help}</div>
      )}
    </div>
  )
}

function FieldInput({ field, value, onChange }: {
  field: FieldDef; value: any; onChange: (v: any) => void
}) {
  const catalog = useCatalog()

  switch (field.type) {
    case 'text':
      return (
        <input className="field" value={value ?? ''} placeholder={field.placeholder}
               onChange={(e) => onChange(e.target.value)} />
      )

    case 'number':
      return (
        <input
          className="field" type="number" value={value ?? ''} min={field.min} max={field.max}
          step={field.step ?? 1} placeholder={field.placeholder}
          onChange={(e) => onChange(e.target.value === '' ? undefined : Number(e.target.value))}
        />
      )

    // 这三种默认都过模板渲染，所以带 {{ }} 补全。
    // 注意 field.template === false 是显式关闭——supervisor 的 agents[].system
    // 长得和 llm.system 一模一样，但后端是 cfg.get("system") 直接取值，不渲染，
    // 在那儿弹补全会是个谎
    case 'textarea':
    case 'prompt':
      return (
        <TemplateText
          className={clsx(field.type === 'prompt' && 'mono text-[11.5px]')}
          rows={field.type === 'prompt' ? 4 : 3}
          value={value ?? ''} placeholder={field.placeholder}
          onChange={onChange}
        />
      )

    case 'code':
      return (
        <TemplateText
          className="mono text-[11px]" rows={10} spellCheck={false}
          value={value ?? ''} placeholder={field.placeholder}
          onChange={onChange}
        />
      )

    case 'select':
      return (
        <select className="field" value={value ?? ''} onChange={(e) => onChange(e.target.value)}>
          {(field.options ?? []).map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      )

    case 'switch':
      return (
        <label className="flex cursor-pointer items-center gap-2 py-0.5">
          <input type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)}
                 className="accent-[var(--accent)]" />
          <span className="text-xs">{field.label}</span>
          {field.help && <span className="text-[10px] text-faint">· {field.help}</span>}
        </label>
      )

    case 'json':
      return <JsonInput value={value} onChange={onChange} placeholder={field.placeholder} />

    case 'model': {
      const options = modelOptions(catalog.providers)
      const groups = [...new Set(options.map((o) => o.group))]
      return (
        <select className="field" value={value ?? ''} onChange={(e) => onChange(e.target.value)}>
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
        <input className="field" list="kb-collections" value={value ?? ''}
               placeholder="default" onChange={(e) => onChange(e.target.value)} />
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
          <select className="field" value={value ?? ''} onChange={(e) => onChange(e.target.value)}>
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
      return <IoFieldList value={value ?? []} onChange={onChange} />

    case 'cases':
      return <CaseList value={value ?? []} onChange={onChange} />

    case 'metricsList':
      return <MetricList value={value ?? []} onChange={onChange} />

    case 'agents':
      return <AgentList value={value ?? []} onChange={onChange} />

    default:
      return <input className="field" value={value ?? ''} onChange={(e) => onChange(e.target.value)} />
  }
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
            <span key={v} className="chip" style={{ borderColor: 'var(--accent)', color: 'var(--accent)' }}>
              {opt?.label ?? v}
              <button onClick={() => toggle(v)} className="ml-0.5 hover:opacity-60"><X size={9} /></button>
            </span>
          )
        })}
        {!value.length && <span className="text-[10.5px] text-faint">未选择</span>}
      </div>
      <button className="btn btn-sm w-full justify-center" onClick={() => setOpen(!open)}>
        <Plus size={11} /> {open ? '收起' : '添加'}
      </button>
      {open && (
        <div className="mt-1.5 rounded border bg-bg">
          <input className="field rounded-b-none border-0 border-b" placeholder="搜索…"
                 value={query} onChange={(e) => setQuery(e.target.value)} autoFocus />
          <div className="max-h-52 overflow-y-auto p-1">
            {!filtered.length && <div className="px-2 py-3 text-center text-[10.5px] text-faint">{empty}</div>}
            {filtered.map((o) => (
              <button
                key={o.value}
                onClick={() => toggle(o.value)}
                className={clsx(
                  'flex w-full items-start gap-2 rounded px-2 py-1.5 text-left hover:bg-hover',
                  value.includes(o.value) && 'bg-hover',
                )}
              >
                <input type="checkbox" readOnly checked={value.includes(o.value)}
                       className="mt-0.5 accent-[var(--accent)]" />
                <span className="min-w-0 flex-1">
                  <span className="block text-[11.5px]">
                    {o.label}
                    {o.danger && <span className="ml-1 text-[9px] text-[var(--warn)]">需确认</span>}
                  </span>
                  {o.hint && <span className="block truncate text-[10px] text-faint">{o.hint}</span>}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function Row({ children, onRemove }: { children: React.ReactNode; onRemove: () => void }) {
  return (
    <div className="mb-1.5 rounded border bg-bg p-2">
      <div className="flex items-start gap-1.5">
        <div className="min-w-0 flex-1 space-y-1.5">{children}</div>
        <button className="btn btn-ghost btn-sm shrink-0" onClick={onRemove}>
          <Trash2 size={11} className="text-[var(--err)]" />
        </button>
      </div>
    </div>
  )
}

function IoFieldList({ value, onChange }: { value: any[]; onChange: (v: any[]) => void }) {
  const update = (i: number, patch: any) =>
    onChange(value.map((f, idx) => (idx === i ? { ...f, ...patch } : f)))
  // 有 value 的是成果字段（output），有 required 的是输入字段
  const isOutput = value.some((f) => 'value' in f)

  return (
    <div>
      {value.map((field, i) => (
        <Row key={i} onRemove={() => onChange(value.filter((_, idx) => idx !== i))}>
          <input className="field" placeholder="字段名" value={field.name ?? ''}
                 onChange={(e) => update(i, { name: e.target.value })} />
          {isOutput ? (
            <input className="field mono text-[11px]" placeholder="{{ vars.xxx }}"
                   value={field.value ?? ''} onChange={(e) => update(i, { value: e.target.value })} />
          ) : (
            <>
              <input className="field" placeholder="说明（可选）" value={field.description ?? ''}
                     onChange={(e) => update(i, { description: e.target.value })} />
              <div className="flex items-center gap-3">
                <label className="flex items-center gap-1.5 text-[11px]">
                  <input type="checkbox" checked={!!field.required} className="accent-[var(--accent)]"
                         onChange={(e) => update(i, { required: e.target.checked })} />
                  必填
                </label>
                <input className="field flex-1" placeholder="默认值" value={field.default ?? ''}
                       onChange={(e) => update(i, { default: e.target.value })} />
              </div>
            </>
          )}
        </Row>
      ))}
      <button className="btn btn-sm w-full justify-center"
              onClick={() => onChange([...value, isOutput ? { name: '', value: '' } : { name: '', required: false }])}>
        <Plus size={11} /> 添加字段
      </button>
    </div>
  )
}

function CaseList({ value, onChange }: { value: any[]; onChange: (v: any[]) => void }) {
  const update = (i: number, patch: any) =>
    onChange(value.map((c, idx) => (idx === i ? { ...c, ...patch } : c)))
  return (
    <div>
      {value.map((c, i) => (
        <Row key={i} onRemove={() => onChange(value.filter((_, idx) => idx !== i))}>
          <input className="field" placeholder="分支标识（会成为连线出口）" value={c.key ?? ''}
                 onChange={(e) => update(i, { key: e.target.value })} />
          <input className="field mono text-[11px]" placeholder="条件，如 len(vars.text) > 100"
                 value={c.condition ?? ''} onChange={(e) => update(i, { condition: e.target.value })} />
          <input className="field" placeholder="说明（模型分类时作为类别描述）" value={c.label ?? ''}
                 onChange={(e) => update(i, { label: e.target.value })} />
        </Row>
      ))}
      <button className="btn btn-sm w-full justify-center"
              onClick={() => onChange([...value, { key: '', condition: '', label: '' }])}>
        <Plus size={11} /> 添加分支
      </button>
      <div className="mt-1.5 text-[10px] leading-snug text-faint">
        所有条件都不满足时走 default 出口，记得连一条
      </div>
    </div>
  )
}

function MetricList({ value, onChange }: { value: any[]; onChange: (v: any[]) => void }) {
  const update = (i: number, patch: any) =>
    onChange(value.map((m, idx) => (idx === i ? { ...m, ...patch } : m)))
  return (
    <div>
      {value.map((m, i) => (
        <Row key={i} onRemove={() => onChange(value.filter((_, idx) => idx !== i))}>
          <div className="flex gap-1.5">
            <input className="field" placeholder="指标 id（英文）" value={m.id ?? ''}
                   onChange={(e) => update(i, { id: e.target.value })} />
            <input className="field" placeholder="名称" value={m.name ?? ''}
                   onChange={(e) => update(i, { name: e.target.value })} />
            <input className="field w-16 shrink-0" placeholder="单位" value={m.unit ?? ''}
                   onChange={(e) => update(i, { unit: e.target.value })} />
          </div>
          <input className="field mono text-[11px]"
                 placeholder="表达式，如 round(vars.agg.amount / vars.agg.orders, 2)"
                 value={m.expression ?? ''}
                 onChange={(e) => update(i, { expression: e.target.value })} />
        </Row>
      ))}
      <button className="btn btn-sm w-full justify-center"
              onClick={() => onChange([...value, { id: '', name: '', unit: '', expression: '' }])}>
        <Plus size={11} /> 添加指标
      </button>
      <div className="mt-1.5 text-[10px] leading-snug text-faint">
        所有算术都发生在这里（确定性、可复算）。叙述节点只许引用这份清单里的数，
        出具时会逐个数字回指校验。
      </div>
    </div>
  )
}

function AgentList({ value, onChange }: { value: any[]; onChange: (v: any[]) => void }) {
  const catalog = useCatalog()
  const update = (i: number, patch: any) =>
    onChange(value.map((a, idx) => (idx === i ? { ...a, ...patch } : a)))
  return (
    <div>
      {value.map((agent, i) => (
        <Row key={i} onRemove={() => onChange(value.filter((_, idx) => idx !== i))}>
          <input className="field" placeholder="成员名（英文，如 researcher）" value={agent.name ?? ''}
                 onChange={(e) => update(i, { name: e.target.value })} />
          <input className="field" placeholder="职责一句话（调度者据此分派）" value={agent.description ?? ''}
                 onChange={(e) => update(i, { description: e.target.value })} />
          <textarea className="field" rows={2} placeholder="角色设定 system prompt"
                    value={agent.system ?? ''} onChange={(e) => update(i, { system: e.target.value })} />
          <MultiPick
            options={catalog.tools.map((t) => ({ value: t.id, label: t.name, hint: t.description, group: t.category }))}
            value={agent.tools ?? []} onChange={(v) => update(i, { tools: v })} empty="没有可用工具"
          />
        </Row>
      ))}
      <button className="btn btn-sm w-full justify-center"
              onClick={() => onChange([...value, { name: '', description: '', system: '', tools: [] }])}>
        <Plus size={11} /> 添加成员
      </button>
    </div>
  )
}
