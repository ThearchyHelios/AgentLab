/**
 * 文件控件（docs/design/normal-user-apps.md N9、§3.8、§4.7）：表单里 x-widget 为 file 的字段。
 *
 * 两步走：先上传（POST /api/files，带上下文：应用的入口、待办或蓝图），拿回引用 {file_id, name, size, sha256, type}，
 * 引用就是这个字段的值，随表单一起提交；服务端提交时核对引用属于提交人、没被别的提交用过，再绑到这次运行。
 * 上下文由用它的地方给（SchemaForm 的 fileContext）：不在入口、待办、试运行里的表单（画布的发起表单、页面的筛选表单）
 * 没有上下文，照实说这里不能上传。
 *
 * 拖放或点选；写明文件名、大小、真实的上传进度（XHR 的字节进度，发完之后是服务端在核对，不把条拉满冒充完成）、允许的
 * 类型和大小。类型和大小先在这里按 x-file 拦一道（省一次白传），服务端照样核对、以服务端为准；被拒的照实说原因。
 * 这次上传、还没提交的文件，换掉或移除时顺手删掉（只能删自己没绑定的，删不掉的服务端 24 小时后清理）。
 * 只读（已提交）时显示成可下载的链接（GET /api/files/{id}，服务端按绑定的运行或记录核对读权限）。
 */
import { useEffect, useRef, useState } from 'react'
import type { DragEvent as ReactDragEvent } from 'react'
import { FileUp, Paperclip, RefreshCw, Upload, X } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import type { UploadProgress } from '../api/http'
import { formatBytes } from '../lib/format'
import { humanizeError } from '../lib/errors'
import { FILE_TEXT } from '../lib/termsApp'
import { isFileRef, toFileRef } from '../lib/schemaForm'
import type { FileLimits } from '../typesApp'
import type { FileRef } from '../typesBlueprint'
import { Spinner } from './ui'

/** 允许的类型写给人看：MIME 换成常见的叫法，扩展名照写；同名的只写一次 */
export function typesText(accept: string[]): string {
  const labels = accept.map((a) => FILE_TEXT.typeLabels[a.toLowerCase()] ?? (a.startsWith('.') ? a.slice(1).toUpperCase() : a))
  return [...new Set(labels)].join('、')
}

/** 这个文件在不在允许的类型里：扩展名、MIME、MIME 通配（image/*）。浏览器认不出类型、又只给了 MIME 的，交给服务端判 */
export function accepts(accept: string[], file: { name: string; type: string }): boolean {
  if (!accept.length) return true
  const name = file.name.toLowerCase()
  const type = (file.type || '').toLowerCase()
  if (!type && !accept.some((a) => a.startsWith('.'))) return true
  return accept.some((raw) => {
    const a = raw.trim().toLowerCase()
    if (a.startsWith('.')) return name.endsWith(a)
    if (a.endsWith('/*')) return type.startsWith(a.slice(0, -1))
    return type === a
  })
}

/** 已提交的文件：可下载的链接（服务端按绑定的运行或记录核对读权限），写文件名和大小。记录列表、字段组件也用它 */
export function FileLink({ file, id, describedBy }: { file: FileRef; id?: string; describedBy?: string }) {
  return (
    <a href={api.files.url(file.file_id)} download={file.name} id={id} aria-describedby={describedBy} title={FILE_TEXT.download}
       className="inline-flex max-w-full items-center gap-1.5 text-xs underline-offset-2 hover:underline" data-file-link={file.file_id}>
      <Paperclip size={12} aria-hidden className="shrink-0 text-faint" />
      <span className="truncate">{file.name}</span>
      <span className="tnum shrink-0 text-2xs text-faint">{formatBytes(file.size)}</span>
    </a>
  )
}

export interface FileInputProps {
  id: string
  /** 控件的名字（字段标题），读屏和取消按钮用 */
  label: string
  value: unknown
  onChange: (v: FileRef | undefined) => void
  readOnly: boolean
  /** 上传的上下文：{app_id, entry} | {task_id} | {blueprint_id}；没有就不能上传 */
  context?: Record<string, string>
  limits: FileLimits
  invalid?: boolean
  describedBy?: string
}

interface Sending { name: string; size: number; progress: UploadProgress | null }

export function FileInput({ id, label, value, onChange, readOnly, context, limits, invalid, describedBy }: FileInputProps) {
  const ref = isFileRef(value) ? value : null
  const accept = limits.accept ?? []
  const [dragging, setDragging] = useState(false)
  const [sending, setSending] = useState<Sending | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const ctrl = useRef<AbortController | null>(null)
  const picker = useRef<HTMLInputElement>(null)
  // 这次上传的：换掉、移除时删掉（还没绑定）。默认值、带回来的引用不是这里传的，不删
  const mine = useRef(new Set<string>())
  useEffect(() => () => ctrl.current?.abort(), [])

  if (readOnly) {
    return ref ? <FileLink file={ref} id={id} describedBy={describedBy} />
      : <div className="text-xs text-faint" id={id}>{FILE_TEXT.empty}</div>
  }

  const allowed = accept.length ? typesText(accept) : null
  const limitLine = [allowed ? FILE_TEXT.types(allowed) : null, limits.max_mb ? FILE_TEXT.maxSize(limits.max_mb) : null]
    .filter(Boolean).join('；') || FILE_TEXT.serverChecks

  const drop = (old: FileRef | null) => {
    if (old && mine.current.has(old.file_id)) {
      mine.current.delete(old.file_id)
      void api.files.remove(old.file_id).catch(() => undefined)
    }
  }
  const upload = async (file: File) => {
    setFailure(null)
    if (!context) return
    if (limits.max_mb && file.size > limits.max_mb * 1024 * 1024) { setFailure(FILE_TEXT.tooLarge(file.name, limits.max_mb)); return }
    if (!accepts(accept, file)) { setFailure(FILE_TEXT.badType(file.name, allowed ?? '')); return }
    const c = new AbortController()
    ctrl.current?.abort()
    ctrl.current = c
    setSending({ name: file.name, size: file.size, progress: null })
    try {
      const res = toFileRef(await api.files.upload(file, context, {
        signal: c.signal,
        onProgress: (progress) => setSending((s) => (s ? { ...s, progress } : s)),
      }))
      mine.current.add(res.file_id)
      drop(ref)
      onChange(res)
    } catch (e) {
      if (e instanceof DOMException && e.name === 'AbortError') setFailure(FILE_TEXT.cancelled)
      else {
        const h = humanizeError(e)
        setFailure(FILE_TEXT.failed(file.name, h.reason ? `${h.title}，${h.reason}` : h.title))
      }
    } finally {
      if (ctrl.current === c) ctrl.current = null
      setSending(null)
    }
  }
  const choose = () => picker.current?.click()
  const hasFiles = (e: ReactDragEvent) => Array.from(e.dataTransfer?.types ?? []).includes('Files')
  const pct = sending?.progress?.total ? Math.min(1, sending.progress.loaded / sending.progress.total) : null

  return (
    <div className="space-y-1" data-file-input={context ? 'ready' : 'no-context'}>
      <input ref={picker} type="file" hidden accept={accept.join(',') || undefined} data-file-picker=""
             onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; if (f) void upload(f) }} />
      {sending ? (
        <div className="rounded-md border px-2.5 py-2 text-xs" data-file-sending={sending.progress?.sent ? 'sent' : 'sending'}>
          <div className="flex items-center gap-2">
            <Spinner size={12} />
            <span className="min-w-0 flex-1 truncate">{sending.name}</span>
            <span className="tnum shrink-0 text-2xs text-faint">{formatBytes(sending.size)}</span>
            {!sending.progress?.sent && (
              <button type="button" className="btn btn-xs btn-ghost shrink-0" onClick={() => ctrl.current?.abort()}
                      aria-label={`${FILE_TEXT.cancel} ${sending.name}`} data-file-cancel="">
                <X size={11} aria-hidden /> {FILE_TEXT.cancel}
              </button>
            )}
          </div>
          <div className="mt-1 text-2xs text-dim">
            {sending.progress?.sent ? FILE_TEXT.sent : (
              <span className="tnum">{FILE_TEXT.sending}{pct != null ? ` · ${Math.round(pct * 100)}%` : ''}</span>
            )}
          </div>
          {pct != null && !sending.progress?.sent && (
            <div className="mt-1 h-1 overflow-hidden rounded-full bg-hover" role="progressbar" aria-label={FILE_TEXT.sending}
                 aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct * 100)} data-file-progress={Math.round(pct * 100)}>
              <div className="h-full origin-left rounded-full bg-[var(--st-running)] transition-transform duration-200"
                   style={{ transform: `scaleX(${pct})` }} />
            </div>
          )}
        </div>
      ) : ref ? (
        <div className="flex items-center gap-2 rounded-md border px-2.5 py-2 text-xs" data-file-value={ref.file_id}
             style={invalid ? { borderColor: 'var(--err)' } : undefined}>
          <Paperclip size={12} aria-hidden className="shrink-0 text-faint" />
          <span className="min-w-0 flex-1 truncate" id={id}>{ref.name}</span>
          <span className="tnum shrink-0 text-2xs text-faint">{formatBytes(ref.size)}</span>
          {context && (
            <button type="button" className="btn btn-xs btn-ghost shrink-0" onClick={choose} data-file-replace="">
              <RefreshCw size={10} aria-hidden /> {FILE_TEXT.replace}
            </button>
          )}
          <button type="button" className="btn btn-xs btn-ghost shrink-0" aria-label={`${FILE_TEXT.remove} ${ref.name}`}
                  onClick={() => { drop(ref); onChange(undefined) }} data-file-remove="">
            <X size={10} aria-hidden /> {FILE_TEXT.remove}
          </button>
        </div>
      ) : (
        <button type="button" id={id} disabled={!context} aria-describedby={describedBy} aria-invalid={invalid || undefined}
                aria-label={`${label}：${context ? FILE_TEXT.drop : FILE_TEXT.noContext}`}
                className={clsx('flex w-full items-center justify-center gap-2 rounded-md border border-dashed px-3 py-4 text-xs',
                  context ? 'text-dim hover:border-[var(--accent)] hover:text-fg' : 'cursor-not-allowed text-faint',
                  dragging && 'border-[var(--accent)] bg-accent-soft text-fg')}
                style={invalid ? { borderColor: 'var(--err)' } : undefined}
                onClick={choose}
                onDragOver={(e) => { if (context && hasFiles(e)) { e.preventDefault(); setDragging(true) } }}
                onDragLeave={() => setDragging(false)}
                onDrop={(e) => {
                  if (!context || !hasFiles(e)) return
                  e.preventDefault()
                  setDragging(false)
                  const f = e.dataTransfer.files?.[0]
                  if (f) void upload(f)
                }}
                data-file-drop="">
          {dragging ? <Upload size={14} aria-hidden /> : <FileUp size={14} aria-hidden />}
          {!context ? FILE_TEXT.noContext : dragging ? FILE_TEXT.dropNow : FILE_TEXT.drop}
        </button>
      )}
      {failure && <div role="alert" className="text-2xs leading-relaxed text-[var(--err)]" data-file-failure="">{failure}</div>}
      {context && (
        <div className="text-2xs leading-relaxed text-faint" data-file-limits="">
          {limitLine}。{FILE_TEXT.whoCanDownload}
        </div>
      )}
    </div>
  )
}
