import { useId, useMemo, useState, type KeyboardEvent } from 'react'
import clsx from 'clsx'
import { isComposing } from '../../components/ui'
import { SCHEDULE_TEXT as T, WORKFLOW_STATUS_LABEL } from '../../lib/terms'
import type { Workflow } from '../../types'

/** 工作流在下拉里的状态：「已发布 v3」「受管 v2」「草稿」 */
export function workflowStatusText(w: Workflow): string {
  if (!w.published_version || !w.status || w.status === 'draft') return T.workflowDraft
  return T.workflowVersion(WORKFLOW_STATUS_LABEL[w.status], w.published_version)
}

/**
 * 可搜索的工作流选择（combobox + listbox）。聚焦就列出全部，打字按名称和说明筛；↑↓ 移动、回车选中、Esc 收起。
 * 没展开时输入框里写选中的那个的名字。选项按下时不抢焦点，点选不会先触发输入框失焦把列表收掉
 */
export function WorkflowPicker({ id, value, workflows, onChange, onBlur, fallbackLabel, invalid, describedBy }: {
  id: string
  value: string
  workflows: Workflow[]
  onChange: (w: Workflow) => void
  /** 选中的工作流不在列表里（列表还没取回来、或者已被删除）时，输入框里写的名字 */
  fallbackLabel?: string
  onBlur?: () => void
  invalid?: boolean
  describedBy?: string
}) {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const listId = `${id}-list`
  const optionId = useId().replace(/[^a-zA-Z0-9_-]/g, '')
  const selected = workflows.find((w) => w.id === value)

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase()
    const sorted = [...workflows].sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))
    return q ? sorted.filter((w) => w.name.toLowerCase().includes(q) || (w.description ?? '').toLowerCase().includes(q)) : sorted
  }, [workflows, query])

  const pick = (w: Workflow) => {
    onChange(w)
    setOpen(false)
    setQuery('')
  }
  const show = () => {
    setOpen(true)
    setQuery('')
    setActive(Math.max(0, matches.findIndex((w) => w.id === value)))
  }
  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (isComposing(e)) return
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      if (!open) { show(); return }
      const step = e.key === 'ArrowDown' ? 1 : -1
      setActive((i) => Math.min(Math.max(0, i + step), Math.max(0, matches.length - 1)))
    } else if (e.key === 'Enter') {
      // 展开着时回车是选中，不是提交表单
      if (open && matches[active]) {
        e.preventDefault()
        pick(matches[active])
      }
    } else if (e.key === 'Escape' && open) {
      // 只收起列表，不让外层（弹窗、页面）跟着处理 Esc
      e.preventDefault()
      e.stopPropagation()
      setOpen(false)
      setQuery('')
    }
  }

  return (
    <div className="relative" data-workflow-picker="">
      <input
        id={id}
        className="field"
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={open && matches[active] ? `${optionId}-${active}` : undefined}
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
        value={open ? query : selected?.name ?? (value ? fallbackLabel ?? '' : '')}
        placeholder={T.workflowPlaceholder}
        autoComplete="off"
        spellCheck={false}
        onFocus={show}
        onClick={() => { if (!open) show() }}
        onChange={(e) => {
          setQuery(e.target.value)
          setOpen(true)
          setActive(0)
        }}
        onBlur={() => {
          setOpen(false)
          setQuery('')
          onBlur?.()
        }}
        onKeyDown={onKeyDown}
      />
      {open && (
        <ul id={listId} role="listbox" aria-label={T.workflow}
            className="absolute inset-x-0 top-full z-20 mt-1 max-h-60 overflow-y-auto rounded-md border bg-panel py-1 shadow-elev-2">
          {matches.length ? matches.map((w, i) => (
            <li
              key={w.id}
              id={`${optionId}-${i}`}
              role="option"
              aria-selected={w.id === value}
              data-workflow-option={w.id}
              onMouseDown={(e) => e.preventDefault()}
              onMouseEnter={() => setActive(i)}
              onClick={() => pick(w)}
              className={clsx('flex cursor-pointer items-center gap-2 px-2.5 py-1.5 text-xs', i === active && 'bg-hover')}
            >
              <span className={clsx('min-w-0 flex-1 truncate', w.id === value && 'font-medium')}>{w.name}</span>
              <span className="chip shrink-0">{workflowStatusText(w)}</span>
            </li>
          )) : <li className="px-2.5 py-1.5 text-xs text-faint" role="presentation">{T.workflowNone}</li>}
        </ul>
      )}
    </div>
  )
}
