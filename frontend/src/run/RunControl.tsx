import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ChevronDown, Play } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { NODE_DEFS } from '../canvas/nodeDefs'
import { hintOf } from '../canvas/shortcuts'
import { Kbd, Skeleton, Spinner, toast } from '../components/ui'
import { SchemaForm } from '../components/SchemaForm'
import { useLocalActor } from '../lib/actor'
import { errorMessage } from '../lib/errors'
import { explainStartError } from '../lib/explain'
import { runClassLabel } from '../lib/terms'
import { useStudio, type FlowNode } from '../store/studio'
import type { ValidationIssue, WorkflowVersion } from '../types'
import type { FormSchema } from '../typesApp'
import { inputPayload, inputSchemaOf, type InputField } from './inputSchema'
import { ClassDot, escapeRun, RunCapsule } from './RunHud'
import { isActivePhase } from './trace'

/**
 * 发起就被拒。绑定的工具在本机不存在（run_tool_missing）时给直达入口：数据源工具去数据页接入，
 * 自定义、MCP 工具去工具页——这次运行还没开始，没有「接着跑」可给。别的照通用的出错提示
 */
function startFailed(e: unknown, navigate: (to: string) => void) {
  const x = explainStartError(e)
  if (!x?.fixTo) {
    toast.error(e)
    return
  }
  const to = x.fixTo
  toast.error(`${x.title}：${x.reason ?? ''}${x.action ?? ''}`, {
    detail: x.raw, key: 'studio:run-tool-missing',
    action: { label: x.fixLabel ?? '去接入', onClick: () => navigate(to) },
  })
}

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

function fieldsOfGraph(graph: WorkflowVersion['graph']): InputField[] {
  const entry = graph?.nodes?.find((n) => n.type === 'input')
  return ((entry?.data?.config?.fields ?? []) as InputField[]).filter((f) => f?.name)
}

/** 已发布版本的发起表单，按 (工作流, 版本) 缓存：发布之后那一版就不会再变 */
const versionForms = new Map<string, FormSchema>()

/**
 * 发起表单。X2：发起表单、人工节点、待办、应用入口共用一个表单渲染器（SchemaForm）——以前这里每个字段都是一个
 * 文本框，入口字段声明了类型、可选值也看不出来。Mod+Enter 直接发起
 */
function LaunchForm({ schema, values, onChange, onSubmit, idPrefix }: {
  schema: FormSchema
  values: Record<string, unknown>
  onChange: (v: Record<string, unknown>) => void
  onSubmit: () => void
  idPrefix: string
}) {
  if (!Object.keys(schema.properties).length) return null
  return (
    <div className="mb-1" data-launch-form="">
      <SchemaForm schema={schema} value={values} onChange={onChange} onSubmit={onSubmit} idPrefix={idPrefix} />
    </div>
  )
}

/**
 * 运行被拦时按钮上的说明：点出是哪个节点。后端的原话只说节点类型（「调用工具」节点还没选工具），
 * 图里有两个工具节点就分不清是哪一个；全部问题在问题面板里，点一条能定位过去
 */
function blockedTitle(errors: ValidationIssue[], nodes: FlowNode[]): string {
  const first = errors[0]
  const node = first.node_id ? nodes.find((n) => n.id === first.node_id) : undefined
  const name = node ? node.data.label || NODE_DEFS[node.data.nodeType]?.label || node.id : ''
  const text = name && !first.message.includes(`「${name}」`) ? `「${name}」· ${first.message}` : first.message
  const head = errors.length > 1 ? `${errors.length} 个问题会阻止运行，第一个：` : '此问题会阻止运行：'
  return `${head}${text}。${hintOf('所有问题均列在问题面板中，点击可定位到节点', 'problems')}`
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
  const [values, setValues] = useState<Record<string, unknown>>({})
  const [busy, setBusy] = useState(false)
  const wrap = useRef<HTMLDivElement>(null)
  useDismiss(open, () => setOpen(false), wrap)

  // 画布上的入口字段按和服务端同一套规则转成表单（run/inputSchema）：试运行的是画布，不是已发布版本
  const entryFields = useMemo(() => {
    const entry = nodes.find((n) => n.data.nodeType === 'input')
    return ((entry?.data.config?.fields ?? []) as InputField[]).filter((f) => f?.name)
  }, [nodes])
  const schema = useMemo(() => inputSchemaOf(entryFields), [entryFields])
  const fields = entryFields
  const errors = issues.filter((i) => i.level === 'error')
  const blocked = !!errors.length || !nodes.length

  const navigate = useNavigate()
  const launch = async () => {
    setBusy(true)
    try {
      await startRun(inputPayload(schema, values))
      setOpen(false)
    } catch (e) {
      startFailed(e, navigate)
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
          ? (errors.length ? blockedTitle(errors, nodes) : '画布为空')
          : `使用画布当前内容发起一次${runClassLabel('exploratory')}`}
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
            <ClassDot formal={false} />
            <span>{runClassLabel('exploratory')}</span>
            <span className="sf-dim">· 使用画布当前内容，结果不归档</span>
          </div>
          <LaunchForm schema={schema} values={values} onChange={setValues} onSubmit={() => void launch()} idPrefix="run-explore" />
          <button type="button" className="btn btn-primary w-full justify-center" disabled={busy}
                  onClick={() => void launch()}>
            {busy ? <Spinner size={11} /> : <Play size={11} fill="currentColor" />} {runClassLabel('exploratory')}
          </button>
          <div className="mt-1.5 flex items-center gap-1 text-2xs text-faint">
            <Kbd combo="Mod+Enter" /> 在输入框中直接发起{runClassLabel('exploratory')}
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
  // 正式运行记在署名下（请求头 X-Actor）：和发布、审批一样写明以谁的名义
  const actor = useLocalActor()
  const key = `${workflowId}@${version}`
  const [open, setOpen] = useState(false)
  const [schema, setSchema] = useState<FormSchema | null>(versionForms.get(key) ?? null)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [values, setValues] = useState<Record<string, unknown>>({})
  const [busy, setBusy] = useState(false)
  const wrap = useRef<HTMLDivElement>(null)
  useDismiss(open, () => setOpen(false), wrap)

  useEffect(() => {
    setSchema(versionForms.get(key) ?? null)
    setValues({})
  }, [key])

  useEffect(() => {
    if (!open || versionForms.has(key)) return
    let alive = true
    setLoadError(null)
    api.workflows.version(workflowId, version)
      .then((v) => {
        // 服务端按版本快照转好的表单（input_schema）为准；老服务端没有，按这一版的入口字段同规则转
        const form = v.input_schema ?? inputSchemaOf((v.input_fields?.length ? v.input_fields : fieldsOfGraph(v.graph)) as InputField[])
        versionForms.set(key, form)
        if (alive) setSchema(form)
      })
      .catch((e) => { if (alive) setLoadError(e) })
    return () => { alive = false }
  }, [open, key, workflowId, version])
  const fields = schema ? Object.keys(schema.properties) : null

  // 画布和已发布版本不是同一份：有未保存的改动，或者保存过但没重新发布
  const ahead = dirty || (canvasVersion != null && canvasVersion !== version)
  const label = runClassLabel('formal', version)

  const navigate = useNavigate()
  const launch = async () => {
    setBusy(true)
    try {
      await startFormalRun(schema ? inputPayload(schema, values) : {})
      setOpen(false)
    } catch (e) {
      startFailed(e, navigate)
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
        {/* 实心圆点是正式运行的记号（探索运行空心）；盾牌只留给发布等级，不再两处混用 */}
        {busy ? <Spinner size={11} /> : <ClassDot formal />}
        {/* 窄屏上工具栏放不下全名：留圆点和版本号，全名在 aria-label 和 title 里 */}
        <span aria-hidden><span className="hidden xl:inline">{runClassLabel('formal')} </span>v{version}</span>
        <ChevronDown size={10} className="hidden opacity-60 transition-transform xl:block"
                     style={{ transform: open ? 'rotate(180deg)' : 'none' }} />
      </button>

      {open && (
        <div className="sf-pop sheet-in" role="dialog" aria-label={label} data-esc-layer>
          <div className="sf-pop-head">
            <ClassDot formal />
            <span>{label}</span>
            <span className="sf-dim">· 已发布的不可变版本，结果归档</span>
          </div>
          {ahead && (
            <div className="sf-pop-note">
              不含画布改动：{dirty ? '画布上有未保存的改动' : `画布为 v${canvasVersion}，尚未发布`}，本次运行使用 v{version}。
            </div>
          )}
          {fields == null && !loadError && <Skeleton rows={2} height={28} className="mb-2.5" />}
          {loadError != null && (
            <div className="sf-pop-note is-err">
              无法获取 v{version} 的输入字段：{errorMessage(loadError)}。
              <button type="button" className="underline" onClick={() => { setLoadError(null); setOpen(false); setTimeout(() => setOpen(true)) }}>
                重试
              </button>
            </div>
          )}
          {fields && !fields.length && (
            <div className="mb-2.5 text-2xs text-faint">v{version} 没有输入字段，将直接运行</div>
          )}
          {schema && (
            <LaunchForm schema={schema} values={values} onChange={setValues} onSubmit={() => void launch()} idPrefix="run-formal" />
          )}
          {actor ? (
            <div className="sf-pop-sign" data-signer="signed">
              将以「<b>{actor}</b>」的名义发起，并记入运行记录
            </div>
          ) : (
            <div className="sf-pop-sign is-unsigned" data-signer="unsigned">
              未署名：本次运行不会记录发起人 · <Link to="/settings/prefs">去设置署名</Link>
            </div>
          )}
          <button type="button" className="btn w-full justify-center sf-formal-go"
                  disabled={busy || schema == null} onClick={() => void launch()}>
            {busy ? <Spinner size={11} /> : <ClassDot formal />} {label}
          </button>
          {!!fields?.length && (
            <div className="mt-1.5 flex items-center gap-1 text-2xs text-faint">
              <Kbd combo="Mod+Enter" /> 在输入框中直接发起{runClassLabel('formal')}
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

