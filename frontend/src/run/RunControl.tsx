import { useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown, Play, RotateCw, ShieldCheck, Square } from 'lucide-react'
import { useStudio } from '../store/studio'
import { Spinner, useToast } from '../components/ui'

/**
 * 工具栏上的运行控件。
 *
 * 从右栏顶部搬上来的。原先它和 Copilot 输入框一起常驻在助手栏里，两个都是
 * "主要动作"，互相压着——而它们其实是两种不同的意图（改这张图 / 跑这张图），
 * 不该争同一块地方。运行是对整个文档的动作，和保存、发布是一类，属于工具栏。
 *
 * 有输入字段就弹出表单填了再跑；没有字段就一键直接跑，不为了统一而多一次点击。
 */
export function RunControl() {
  const nodes = useStudio((s) => s.nodes)
  const run = useStudio((s) => s.run)
  const streaming = useStudio((s) => s.streaming)
  const issues = useStudio((s) => s.issues)
  const workflow = useStudio((s) => s.workflow)
  const dirty = useStudio((s) => s.dirty)
  const { startRun, startFormalRun, stopRun, continueRun } = useStudio()
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const [values, setValues] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const wrap = useRef<HTMLDivElement>(null)

  const inputFields = useMemo(() => {
    const entry = nodes.find((n) => n.data.nodeType === 'input')
    return (entry?.data.config?.fields ?? []) as any[]
  }, [nodes])

  const errors = issues.filter((i) => i.level === 'error')
  const canFormal = !!workflow
    && (workflow.status === 'published' || workflow.status === 'governed')
    && !!workflow.published_version
  const running = streaming || run?.status === 'running'

  // 点外面关掉。浮层不能只靠再点一次按钮关——那是很容易漏掉的死路
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open])

  const launch = async (formal = false) => {
    setBusy(true)
    try {
      const payload: Record<string, any> = {}
      for (const field of inputFields) {
        if (!field.name) continue
        const raw = values[field.name] ?? field.default ?? ''
        // 看起来像 JSON 就按 JSON 传，让数组/对象类型的输入能用
        if (typeof raw === 'string' && /^\s*[[{]/.test(raw)) {
          try { payload[field.name] = JSON.parse(raw) } catch { payload[field.name] = raw }
        } else {
          payload[field.name] = raw
        }
      }
      if (formal) await startFormalRun(payload)
      else await startRun(payload)
      setOpen(false)
    } catch (e: any) {
      toast(e.message ?? '启动失败', 'error')
    } finally {
      setBusy(false)
    }
  }

  if (running) {
    return (
      <button className="btn btn-danger" onClick={stopRun}>
        <Square size={11} fill="currentColor" /> 停止
      </button>
    )
  }

  const blocked = !!errors.length || !nodes.length

  const resume = async () => {
    setBusy(true)
    try {
      await continueRun()
      toast('从断点接着跑，前面跑过的节点不重来', 'ok')
    } catch (e: any) {
      toast(e.message ?? '接着跑失败', 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="relative flex items-center gap-1.5" ref={wrap}>
      {/* 图挂在半路时，"接着跑"才是用户接下来最想做的事：他刚在画布上把那个
          写错的模型 id / 循环条件改好，重跑一遍意味着前面查过的表、跑过的 SQL
          全部重来。断点一直在 checkpoint 里躺着，只是以前没有入口 */}
      {run?.status === 'failed' && !blocked && (
        <button className="btn gap-1.5" disabled={busy} onClick={resume}
                title="从失败的那个节点接着跑，前面跑过的节点不重来。只能改节点配置，改了结构要重新发起">
          {busy ? <Spinner size={11} /> : <RotateCw size={11} />}
          接着跑
        </button>
      )}
      <button
        className="btn btn-primary gap-1.5"
        disabled={busy || blocked}
        title={blocked
          ? (errors.length ? `${errors.length} 个问题会阻止运行：${errors[0].message}` : '画布是空的')
          : '用画布当前内容跑一次，结果标探索性'}
        onClick={() => {
          // 没有输入字段就没什么可填的，多弹一层只是多一次点击
          if (!inputFields.length && !canFormal) void launch(false)
          else setOpen((v) => !v)
        }}
      >
        {busy ? <Spinner size={11} /> : <Play size={11} fill="currentColor" />}
        运行
        {(inputFields.length > 0 || canFormal) && (
          <ChevronDown size={10} className="opacity-60 transition-transform"
                       style={{ transform: open ? 'rotate(180deg)' : 'none' }} />
        )}
      </button>

      {open && (
        <div className="sheet-in absolute right-0 top-full z-50 mt-1.5 w-[340px] rounded-xl border bg-panel p-3 shadow-xl">
          {inputFields.map((field) => (
            <div key={field.name} className="mb-2.5">
              <label className="label">
                {field.name}
                {field.required && <span className="ml-1 text-[var(--err)]">*</span>}
                {field.description && (
                  <span className="ml-1.5 font-normal text-faint">{field.description}</span>
                )}
              </label>
              <textarea
                className="field"
                rows={2}
                autoFocus={field === inputFields[0]}
                value={values[field.name] ?? (typeof field.default === 'string'
                  ? field.default
                  : field.default ? JSON.stringify(field.default) : '')}
                placeholder={field.required ? '必填' : '可留空'}
                onChange={(e) => setValues({ ...values, [field.name]: e.target.value })}
                onKeyDown={(e) => {
                  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') void launch(false)
                }}
              />
            </div>
          ))}
          {!inputFields.length && (
            <div className="mb-2.5 text-[11px] text-faint">这张图没有输入节点，将直接运行</div>
          )}

          <div className="flex gap-2">
            <button className="btn btn-primary flex-1 justify-center" disabled={busy}
                    onClick={() => launch(false)}>
              <Play size={11} fill="currentColor" /> 试运行
            </button>
            {canFormal && (
              <button
                className="btn flex-1 justify-center"
                style={{ borderColor: 'var(--ok)', color: 'var(--ok)' }}
                disabled={busy || dirty}
                title={dirty
                  ? '有未保存改动，正式运行只跑已发布版本'
                  : `从已发布的 v${workflow?.published_version} 不可变版本发起`}
                onClick={() => launch(true)}
              >
                <ShieldCheck size={11} /> 正式 v{workflow?.published_version}
              </button>
            )}
          </div>
          {canFormal && dirty && (
            <div className="mt-1.5 text-[10px] leading-relaxed text-faint">
              画布有未保存改动；正式运行永远执行已发布的不可变版本
            </div>
          )}
          {!!inputFields.length && (
            <div className="mt-1.5 text-[10px] text-faint">⌘⏎ 直接运行</div>
          )}
        </div>
      )}
    </div>
  )
}
