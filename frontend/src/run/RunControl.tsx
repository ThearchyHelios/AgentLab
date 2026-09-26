import { useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown, Play, ShieldCheck } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { isComposing, Kbd, Skeleton, Spinner, toast } from '../components/ui'
import { errorMessage } from '../lib/errors'
import { matchShortcut } from '../lib/keys'
import { runClassLabel } from '../lib/terms'
import { useStudio } from '../store/studio'
import type { WorkflowVersion } from '../types'
import { escapeRun, RunCapsule } from './RunHud'
import { isActivePhase } from './trace'

/**
 * 工具栏上的运行控件。
 *
 * 从右栏顶部搬上来的。原先它和 Copilot 输入框一起常驻在助手栏里，两个都是
 * "主要动作"，互相压着——而它们其实是两种不同的意图（改这张图 / 跑这张图），
 * 不该争同一块地方。运行是对整个文档的动作，和保存、发布是一类，属于工具栏。
 *
 * 有了运行之后，这里多一枚运行胶囊（run/RunHud.tsx）：跑的时候它就是全部——
 * 计时、进度、用量、停止都在里面；结束了它收成结果，旁边再露出发起按钮。
 * 相位一律读 store 的 runPhase（由事件推出），不再读启动时那份 run 快照：
 * 那份快照的 status 永远是 queued，失败后「接着跑」出不来、续跑完「停止」卡死。
 *
 * 两个入口互不牵连：
 * - 「运行」用画布当前内容发起探索运行，画布有错误时不能点；
 * - 「正式运行 vN」跑已发布的不可变版本，只看 published_version，和画布上的
 *   错误、未保存改动都无关（改了只提示「不含画布改动」），表单字段取那一版的。
 */

type Field = { name: string; required?: boolean; description?: string; default?: unknown; example?: unknown }

/** 已发布版本的入口字段，按 (工作流, 版本) 缓存：发布之后那一版就不会再变 */
const versionFields = new Map<string, Field[]>()

function fieldsOfGraph(graph: WorkflowVersion['graph']): Field[] {
  const entry = graph?.nodes?.find((n) => n.type === 'input')
  return ((entry?.data?.config?.fields ?? []) as Field[]).filter((f) => f?.name)
}

const shown = (v: unknown): string => (typeof v === 'string' ? v : v == null ? '' : JSON.stringify(v))

function payloadOf(fields: Field[], values: Record<string, string>): Record<string, any> {
  const payload: Record<string, any> = {}
  for (const field of fields) {
    // 留空就用默认值。占位符里写着「默认：…」，照它说的办
    const typed = values[field.name]
    const raw = typed != null && typed.trim() !== '' ? typed : field.default ?? ''
    // 看起来像 JSON 就按 JSON 传，让数组/对象类型的输入能用
    if (typeof raw === 'string' && /^\s*[[{]/.test(raw)) {
      try { payload[field.name] = JSON.parse(raw) } catch { payload[field.name] = raw }
    } else {
      payload[field.name] = raw
    }
  }
  return payload
}

function FieldsForm({ fields, values, onChange, onSubmit }: {
  fields: Field[]
  values: Record<string, string>
  onChange: (v: Record<string, string>) => void
  onSubmit: () => void
}) {
  return (
    <>
      {fields.map((field, i) => (
        <div key={field.name} className="mb-2.5">
          <label className="label" htmlFor={`run-field-${field.name}`}>
            {field.name}
            {field.required && <span className="ml-1 text-[var(--err)]" aria-label="必填">*</span>}
            {field.description && (
              <span className="ml-1.5 font-normal text-faint">{field.description}</span>
            )}
          </label>
          <textarea
            id={`run-field-${field.name}`}
            className="field"
            rows={2}
            autoFocus={i === 0}
            value={values[field.name] ?? ''}
            // 占位符不复用 description（标签旁已经写着）：有默认值说默认值，有示例给示例，都没有就空着
            placeholder={field.default != null && shown(field.default) !== ''
              ? `默认：${shown(field.default)}`
              : field.example != null ? `例如：${shown(field.example)}` : ''}
            onChange={(e) => onChange({ ...values, [field.name]: e.target.value })}
            onKeyDown={(e) => {
              if (!isComposing(e) && matchShortcut(e.nativeEvent, 'Mod+Enter')) {
                e.preventDefault()
                onSubmit()
              }
            }}
          />
        </div>
      ))}
    </>
  )
}

/** 点外面、按 Esc 关掉浮层。浮层不能只靠再点一次按钮关——那是很容易漏掉的死路 */
function useDismiss(open: boolean, close: () => void, wrap: React.RefObject<HTMLDivElement | null>) {
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) close()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.isComposing) return
      e.preventDefault()
      close()
    }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open, close, wrap])
}

/** 探索运行：画布当前这张图。tight：旁边有运行胶囊，窄屏上只留图标 */
function ExploreLauncher({ tight }: { tight: boolean }) {
  const nodes = useStudio((s) => s.nodes)
  const issues = useStudio((s) => s.issues)
  const startRun = useStudio((s) => s.startRun)
  const [open, setOpen] = useState(false)
  const [values, setValues] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const wrap = useRef<HTMLDivElement>(null)
  useDismiss(open, () => setOpen(false), wrap)

  const fields = useMemo(() => {
    const entry = nodes.find((n) => n.data.nodeType === 'input')
    return ((entry?.data.config?.fields ?? []) as Field[]).filter((f) => f?.name)
  }, [nodes])
  const errors = issues.filter((i) => i.level === 'error')
  const blocked = !!errors.length || !nodes.length

  const launch = async () => {
    setBusy(true)
    try {
      await startRun(payloadOf(fields, values))
      setOpen(false)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="relative" ref={wrap}>
      <button
        type="button"
        className="btn btn-primary gap-1.5"
        disabled={busy || blocked}
        aria-label="运行"
        aria-expanded={fields.length ? open : undefined}
        title={blocked
          ? (errors.length ? `${errors.length} 个问题会阻止运行：${errors[0].message}` : '画布是空的')
          : `用画布当前内容发起一次${runClassLabel('exploratory')}`}
        onClick={() => {
          // 没有输入字段就没什么可填的，多弹一层只是多一次点击
          if (!fields.length) void launch()
          else setOpen((v) => !v)
        }}
      >
        {busy ? <Spinner size={11} /> : <Play size={11} fill="currentColor" />}
        <span className={tight ? 'hidden xl:inline' : undefined}>运行</span>
        {fields.length > 0 && (
          <ChevronDown size={10} className={clsx('opacity-60 transition-transform', tight && 'hidden xl:block')}
                       style={{ transform: open ? 'rotate(180deg)' : 'none' }} />
        )}
      </button>

      {open && (
        <div className="sf-pop sheet-in" role="dialog" aria-label={runClassLabel('exploratory')} data-esc-layer>
          <div className="sf-pop-head">
            <Play size={11} fill="currentColor" className="text-[var(--accent)]" />
            <span>{runClassLabel('exploratory')}</span>
            <span className="sf-dim">· 用画布当前内容，结果不归档</span>
          </div>
          <FieldsForm fields={fields} values={values} onChange={setValues} onSubmit={() => void launch()} />
          <button type="button" className="btn btn-primary w-full justify-center" disabled={busy}
                  onClick={() => void launch()}>
            {busy ? <Spinner size={11} /> : <Play size={11} fill="currentColor" />} {runClassLabel('exploratory')}
          </button>
          <div className="mt-1.5 flex items-center gap-1 text-2xs text-faint">
            <Kbd combo="Mod+Enter" /> 在输入框里直接{runClassLabel('exploratory')}
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * 正式运行：已发布的不可变版本。
 *
 * 以前它要求工作流状态是已发布 / 受管——可保存一次就退回草稿，入口跟着消失，
 * 而后端只看 published_version。它也不受画布上的错误和未保存改动影响：跑的
 * 根本不是画布。表单字段必须取那一版的入口：画布改过字段名时照画布填，
 * 传过去的就是错的参数。
 */
function FormalLauncher({ workflowId, version }: { workflowId: string; version: number }) {
  const dirty = useStudio((s) => s.dirty)
  const canvasVersion = useStudio((s) => s.workflow?.version)
  const startFormalRun = useStudio((s) => s.startFormalRun)
  const key = `${workflowId}@${version}`
  const [open, setOpen] = useState(false)
  const [fields, setFields] = useState<Field[] | null>(versionFields.get(key) ?? null)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [values, setValues] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const wrap = useRef<HTMLDivElement>(null)
  useDismiss(open, () => setOpen(false), wrap)

  useEffect(() => {
    setFields(versionFields.get(key) ?? null)
    setValues({})
  }, [key])

  useEffect(() => {
    if (!open || versionFields.has(key)) return
    let alive = true
    setLoadError(null)
    api.workflows.version(workflowId, version)
      .then((v) => {
        const list = (v.input_fields?.length ? v.input_fields : fieldsOfGraph(v.graph)) as Field[]
        versionFields.set(key, list)
        if (alive) setFields(list)
      })
      .catch((e) => { if (alive) setLoadError(e) })
    return () => { alive = false }
  }, [open, key, workflowId, version])

  // 画布和已发布版本不是同一份：有未保存的改动，或者保存过但没重新发布
  const ahead = dirty || (canvasVersion != null && canvasVersion !== version)
  const label = runClassLabel('formal', version)

  const launch = async () => {
    setBusy(true)
    try {
      await startFormalRun(payloadOf(fields ?? [], values))
      setOpen(false)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="relative" ref={wrap}>
      <button
        type="button"
        className={clsx('btn gap-1.5', ahead && 'sf-ahead')}
        disabled={busy}
        aria-label={label}
        aria-expanded={open}
        title={`从已发布的 v${version} 发起${runClassLabel('formal')}，结果归档、可审计${ahead ? '。不含画布上的改动' : ''}`}
        onClick={() => setOpen((v) => !v)}
      >
        {busy ? <Spinner size={11} /> : <ShieldCheck size={12} />}
        {/* 窄屏上工具栏放不下全名：留盾牌和版本号，全名在 aria-label 和 title 里 */}
        <span aria-hidden><span className="hidden xl:inline">{runClassLabel('formal')} </span>v{version}</span>
        <ChevronDown size={10} className="hidden opacity-60 transition-transform xl:block"
                     style={{ transform: open ? 'rotate(180deg)' : 'none' }} />
      </button>

      {open && (
        <div className="sf-pop sheet-in" role="dialog" aria-label={label} data-esc-layer>
          <div className="sf-pop-head">
            <ShieldCheck size={12} />
            <span>{label}</span>
            <span className="sf-dim">· 已发布的不可变版本，结果归档</span>
          </div>
          {ahead && (
            <div className="sf-pop-note">
              不含画布改动：{dirty ? '画布上有未保存的改动' : `画布是 v${canvasVersion}，还没发布`}，这次执行的是 v{version}。
            </div>
          )}
          {fields == null && !loadError && <Skeleton rows={2} height={28} className="mb-2.5" />}
          {loadError != null && (
            <div className="sf-pop-note is-err">
              取不到 v{version} 的输入字段：{errorMessage(loadError)}。
              <button type="button" className="underline" onClick={() => { setLoadError(null); setOpen(false); setTimeout(() => setOpen(true)) }}>
                重试
              </button>
            </div>
          )}
          {fields && !fields.length && (
            <div className="mb-2.5 text-2xs text-faint">v{version} 没有输入字段，将直接运行</div>
          )}
          {fields && (
            <FieldsForm fields={fields} values={values} onChange={setValues} onSubmit={() => void launch()} />
          )}
          <button type="button" className="btn w-full justify-center sf-formal-go"
                  disabled={busy || fields == null} onClick={() => void launch()}>
            {busy ? <Spinner size={11} /> : <ShieldCheck size={12} />} {label}
          </button>
          {!!fields?.length && (
            <div className="mt-1.5 flex items-center gap-1 text-2xs text-faint">
              <Kbd combo="Mod+Enter" /> 在输入框里直接{runClassLabel('formal')}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export function RunControl() {
  const phase = useStudio((s) => s.runPhase)
  const workflowId = useStudio((s) => s.workflow?.id)
  const published = useStudio((s) => s.workflow?.published_version)
  const active = isActivePhase(phase)

  // Esc：回放中先回到实时，运行结束后清掉画布上的结果。别的层（输入框、弹窗、
  // 属性面板、发起浮层）先处理 Esc 时这里让路
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (escapeRun(e)) e.preventDefault() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <div className="flex items-center gap-1.5" data-run-control>
      {phase !== 'idle' && <RunCapsule />}
      {/* 运行中只留胶囊：同一张画布同一时刻只跟一次运行 */}
      {!active && (
        <>
          <ExploreLauncher tight={phase !== 'idle'} />
          {workflowId && published ? <FormalLauncher workflowId={workflowId} version={published} /> : null}
        </>
      )}
    </div>
  )
}

