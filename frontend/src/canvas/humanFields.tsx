import { HUMAN_NODE_TEXT as T, TOOL_WRITE_TEXT } from '../lib/terms'
import type { FieldDef, FieldRenderProps } from './nodeDefs'

/*
 * 人工节点的新配置（docs/design/app-platform-and-look.md §3.7）和调用工具节点写入确认派给谁（§3.8），给 nodeDefs 用。
 *
 * 写在图里的是流程语义：派给谁（岗位、发起人、表达式、外部系统，外部系统带关联键）、多人策略、时限、超时处置、表单、关联键。
 * 图里只写岗位名，不写具体的人——岗位绑定到人或组在应用里做；没有应用的运行派给任何人，行为和以前一样。
 * 每个控件只写自己那个键；写回 undefined 就是不配置（配置里不留空对象）。合不合规由服务端的发布检查说了算
 * （human.due_invalid、human.policy_mode、human.escalate_slot），问题挂在对应字段下面
 */

const isRecord = (v: unknown): v is Record<string, any> => !!v && typeof v === 'object' && !Array.isArray(v)

const ASSIGNEE_KINDS = ['', 'slot', 'initiator', 'expression', 'hook'] as const

/** 处理人：{kind:'slot', slot} / {kind:'initiator'} / {kind:'expression', expr} / {kind:'hook'}；不配置就是任何人 */
function AssigneeInput({ id, value, onChange, invalid, describedBy, disabled }: FieldRenderProps) {
  const cur = isRecord(value) ? value : {}
  const kind = typeof cur.kind === 'string' ? cur.kind : ''
  return (
    <div className="space-y-1.5" data-human-assignee={kind || 'anyone'}>
      <select id={id} className="field" value={kind} disabled={disabled} aria-invalid={invalid || undefined}
              aria-describedby={describedBy}
              onChange={(e) => {
                const k = e.target.value
                onChange(k === '' ? undefined
                  : k === 'slot' ? { kind: k, slot: typeof cur.slot === 'string' ? cur.slot : '' }
                  : k === 'expression' ? { kind: k, expr: typeof cur.expr === 'string' ? cur.expr : '' }
                  : k === 'hook' ? { kind: k, correlation: typeof cur.correlation === 'string' ? cur.correlation : '' }
                  : { kind: k })
              }}>
        {ASSIGNEE_KINDS.map((k) => <option key={k} value={k}>{T.assigneeKind[k]}</option>)}
        {kind && !(ASSIGNEE_KINDS as readonly string[]).includes(kind) && (
          <option value={kind} disabled>{kind}：无法识别的处理人，请从上方选项中选择</option>
        )}
      </select>
      {kind === 'slot' && (
        <div>
          <label className="label" htmlFor={`${id}-slot`}>{T.slot}</label>
          <input id={`${id}-slot`} className="field mono text-xs" value={cur.slot ?? ''} placeholder={T.slotPlaceholder}
                 spellCheck={false} disabled={disabled} onChange={(e) => onChange({ kind, slot: e.target.value })} />
        </div>
      )}
      {kind === 'expression' && (
        <div>
          <label className="label" htmlFor={`${id}-expr`}>{T.expression}</label>
          <input id={`${id}-expr`} className="field mono text-xs" value={cur.expr ?? ''} placeholder="input.manager"
                 spellCheck={false} disabled={disabled} onChange={(e) => onChange({ kind, expr: e.target.value })} />
          <div className="mt-1 text-2xs leading-snug text-faint">{T.expressionHint}</div>
        </div>
      )}
      {kind === 'hook' && (
        <div>
          <label className="label" htmlFor={`${id}-corr`}>{T.correlation}</label>
          <input id={`${id}-corr`} className="field mono text-xs" value={cur.correlation ?? ''}
                 placeholder="{{ input.order_id }}" spellCheck={false} disabled={disabled}
                 onChange={(e) => onChange({ kind, correlation: e.target.value })} />
          <div className="mt-1 text-2xs leading-snug text-faint">{T.correlationHint}</div>
        </div>
      )}
    </div>
  )
}

/** 多人处理：'any'（默认，不写）/ 'all' / {quorum: m} */
function PolicyInput({ id, value, onChange, invalid, describedBy, disabled }: FieldRenderProps) {
  const kind = value === 'all' ? 'all' : isRecord(value) && value.quorum != null ? 'quorum' : 'any'
  const m = isRecord(value) && typeof value.quorum === 'number' ? value.quorum : 2
  return (
    <div className="flex items-center gap-2" data-human-policy={kind}>
      <select id={id} className="field min-w-0 flex-1" value={kind} disabled={disabled}
              aria-invalid={invalid || undefined} aria-describedby={describedBy}
              onChange={(e) => onChange(e.target.value === 'any' ? undefined
                : e.target.value === 'all' ? 'all' : { quorum: m })}>
        {(['any', 'all', 'quorum'] as const).map((k) => <option key={k} value={k}>{T.policyKind[k]}</option>)}
      </select>
      {kind === 'quorum' && (
        <input className="field tnum w-20 shrink-0" type="number" min={1} step={1} value={m} disabled={disabled}
               aria-label={T.quorum}
               onChange={(e) => onChange({ quorum: Math.max(1, Math.floor(Number(e.target.value) || 1)) })} />
      )}
    </div>
  )
}

const TIMEOUT_KINDS = ['', 'remind', 'escalate', 'reject'] as const

/** 超时后：{action:'remind', every} / {action:'escalate', to_slot} / {action:'reject'}。没有自动批准 */
function OnTimeoutInput({ id, value, onChange, invalid, describedBy, disabled }: FieldRenderProps) {
  const cur = isRecord(value) ? value : {}
  const action = typeof cur.action === 'string' ? cur.action : ''
  return (
    <div className="space-y-1.5" data-human-timeout={action || 'none'}>
      <select id={id} className="field" value={action} disabled={disabled} aria-invalid={invalid || undefined}
              aria-describedby={describedBy}
              onChange={(e) => {
                const a = e.target.value
                onChange(a === '' ? undefined
                  : a === 'remind' ? { action: a, every: typeof cur.every === 'string' ? cur.every : 'PT1H' }
                  : a === 'escalate' ? { action: a, to_slot: typeof cur.to_slot === 'string' ? cur.to_slot : '' }
                  : { action: a })
              }}>
        {TIMEOUT_KINDS.map((k) => <option key={k} value={k}>{T.onTimeoutKind[k]}</option>)}
      </select>
      {action === 'remind' && (
        <div>
          <label className="label" htmlFor={`${id}-every`}>{T.remindEvery}</label>
          <input id={`${id}-every`} className="field mono text-xs" value={cur.every ?? ''} placeholder="PT1H"
                 disabled={disabled} onChange={(e) => onChange({ action, every: e.target.value })} />
        </div>
      )}
      {action === 'escalate' && (
        <div>
          <label className="label" htmlFor={`${id}-to`}>{T.escalateTo}</label>
          <input id={`${id}-to`} className="field mono text-xs" value={cur.to_slot ?? ''} placeholder="director"
                 spellCheck={false} disabled={disabled} onChange={(e) => onChange({ action, to_slot: e.target.value })} />
        </div>
      )}
    </div>
  )
}

/** 人工节点在「审批方式」之后多出来的几项 */
export const HUMAN_PROCESS_FIELDS: FieldDef[] = [
  { key: 'assignee', label: T.assignee, type: 'select', help: T.assigneeHint, render: (p) => <AssigneeInput {...p} /> },
  {
    key: 'policy', label: T.policy, type: 'select', help: T.policyHint,
    // 全体同意、按人数只用于批准 / 驳回方式（human.policy_mode）：别的方式下不给这一项，存量配置照旧显示，由检查报出来
    when: (c) => (c.mode ?? 'approve') === 'approve' || c.policy != null,
    render: (p) => <PolicyInput {...p} />,
  },
  { key: 'due', label: T.due, type: 'text', help: T.dueHint, placeholder: T.duePlaceholder },
  {
    key: 'on_timeout', label: T.onTimeout, type: 'select', help: T.onTimeoutHint,
    when: (c) => !!c.due || c.on_timeout != null, render: (p) => <OnTimeoutInput {...p} />,
  },
  { key: 'form', label: T.form, type: 'json', help: T.formHint, advanced: true },
  { key: 'form_from', label: T.formFrom, type: 'text', help: T.formFromHint, placeholder: 'nodes.agent1.form', advanced: true },
]

/**
 * 调用工具节点：写入能力的确认派给谁（写法同人工节点）。要不要确认沿用「审批策略」，环境按运行类别定，都写在帮助里；
 * 审批策略是「全部无需审批」时不出确认卡（能力还得允许无人值守写入），这一栏就不给
 */
export const TOOL_WRITE_FIELDS: FieldDef[] = [
  {
    key: 'assignee', label: TOOL_WRITE_TEXT.assignee, type: 'select',
    help: `${TOOL_WRITE_TEXT.assigneeHint}。${TOOL_WRITE_TEXT.envHint}`,
    when: (c) => c.approval !== 'never', render: (p) => <AssigneeInput {...p} />,
  },
]
