import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  AlertTriangle, ChevronRight, Database, FileSpreadsheet, Info, KeyRound, Lock, Plug, Plus, RefreshCw,
  Search, Table2, Trash2, Upload, X,
} from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { useCatalog, useOnReconnect } from '../store/catalog'
import {
  confirmDialog, EmptyState, ErrorState, Field, IconButton, Modal, promptDialog, Skeleton, Spinner, StatusBadge,
  toast,
} from '../components/ui'
import { humanizeError } from '../lib/errors'
import { formatDateTime, formatDuration, formatNumber, formatRelative, formatTime, parseServerTime } from '../lib/format'
import { useRunClock } from '../run/useRunClock'
import type { Workflow } from '../types'

// ===========================================================================
// 管理页共用件：工具、知识、数据、设置四页都用。
//
// 暂住在这里：这一轮不动 components/ui.tsx，也不另开文件。等 ui.tsx 能改了，
// PageHeader / HealthPill / deferDelete 挪过去。
// ===========================================================================

/**
 * 页头：图标、标题、一句说明、页面级按钮。
 *
 * 高度卡死 48px：toast 从 56px 起，工具栏比这高，常驻的出错 toast 就会压住
 * 按钮。标签页（如果有）紧贴在页头下面、左边缘对齐。
 */
export function PageHeader({ icon, title, subtitle, actions }: {
  icon: ReactNode; title: string; subtitle?: string; actions?: ReactNode
}) {
  return (
    <header className="flex h-12 shrink-0 items-center gap-2.5 border-b bg-panel px-4">
      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md border bg-elev text-dim" aria-hidden>
        {icon}
      </span>
      <h1 className="shrink-0 text-sm font-semibold">{title}</h1>
      {subtitle && <p className="min-w-0 truncate text-xs text-faint" title={subtitle}>{subtitle}</p>}
      <span className="flex-1" />
      {actions}
    </header>
  )
}

/** 标签页里一节的标题行：左边标题和一句说明，右边这一节的按钮 */
export function SectionBar({ title, hint, children }: { title: string; hint?: ReactNode; children?: ReactNode }) {
  return (
    <div className="mb-3 flex flex-wrap items-end gap-x-3 gap-y-2">
      <div className="min-w-0 flex-1">
        <h2 className="text-sm font-semibold">{title}</h2>
        {hint && <p className="mt-0.5 text-xs leading-relaxed text-faint">{hint}</p>}
      </div>
      {children && <div className="flex flex-wrap items-center gap-2">{children}</div>}
    </div>
  )
}

/** 删除类的纯图标按钮：悬停铺 10% 的 err 底，读屏念得出删的是谁 */
export function DeleteButton({ label, onClick, disabled }: { label: string; onClick: () => void; disabled?: boolean }) {
  return (
    <IconButton
      label={label}
      onClick={onClick}
      disabled={disabled}
      className="text-faint hover:bg-st-failed/10 hover:text-[var(--err)]"
      icon={<Trash2 size={12} />}
    />
  )
}

/**
 * 单选组（role=radiogroup）的键盘约定：整组只占一个 Tab 位，落在选中项上；
 * ←→↑↓ 在组内移动并选中，首尾相接。标了 radio 却每项一个 Tab 位、方向键没反应，
 * 等于对读屏用户许了个做不到的诺。
 *
 * 用法：const radio = useRadioGroup(values, value, onChange)，每个选项展开
 * {...radio(v)}，点击照旧自己写 onClick。
 */
export function useRadioGroup<T extends string>(values: readonly T[], value: T | undefined, onChange: (v: T) => void) {
  const refs = useRef(new Map<T, HTMLElement | null>())
  // 选中的值不在选项里（还没加载完之类）：让第一项接住 Tab
  const focusable = value !== undefined && values.includes(value) ? value : values[0]
  return (v: T) => ({
    ref: (el: HTMLElement | null) => { refs.current.set(v, el) },
    role: 'radio' as const,
    'aria-checked': v === value,
    tabIndex: v === focusable ? 0 : -1,
    onKeyDown: (e: ReactKeyboardEvent) => {
      const step = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0
      if (!step || e.altKey || e.metaKey || e.ctrlKey) return
      e.preventDefault()
      const i = values.indexOf(v)
      const next = values[(i + step + values.length) % values.length]
      if (next !== value) onChange(next)
      refs.current.get(next)?.focus()
    },
  })
}

/**
 * 每 ms 毫秒重渲染一次，给「3 分钟前」这类相对时间保鲜。
 * 只负责触发重渲染：算相对时间时要现取 Date.now()——返回值是上一拍的时刻，
 * 比刚到的结果还早，拿它算会把「刚刚」写成一个钟点。
 */
export function useTicker(ms: number, active = true): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const t = setInterval(() => setNow(Date.now()), ms)
    return () => clearInterval(t)
  }, [ms, active])
  return now
}

/** 图里引用了这些名字（工具名、模型 id）的工作流。按 JSON 里的整串值匹配，不做子串 */
export function workflowsMentioning(workflows: Workflow[], tokens: string[]): Workflow[] {
  const needles = tokens.filter(Boolean).map((t) => JSON.stringify(t))
  if (!needles.length) return []
  return workflows.filter((w) => {
    const text = JSON.stringify(w.graph ?? {})
    return needles.some((n) => text.includes(n))
  })
}

/** 「3 个工作流（周报、巡检 等）」 */
export function workflowList(list: Workflow[], max = 3): string {
  const names = list.slice(0, max).map((w) => `「${w.name}」`).join('')
  return `${list.length} 个工作流（${names}${list.length > max ? ' 等' : ''}）`
}

// ---------------------------------------------------------------------------
// 连没连上：统一、持久的状态
// ---------------------------------------------------------------------------

/**
 * 一次「测连接」的结果。按 `种类:id` 存：模型接入 provider:<id>、数据源
 * datasource:<id>、MCP mcp:<id>。
 *
 * 以前测完只弹 4 秒 toast，结果存在组件 state 里，切个标签就没了；卡片上的圆点
 * 又各说各的（模型卡的绿点其实是「启用」）。管理页最核心的问题是「现在能不能
 * 用」，答案得留在对象上，并注明是什么时候测的。
 *
 * 模块级而不是组件 state：切标签、换页都还在。再写一份到 localStorage，刷新
 * 也还在——只是这台浏览器的缓存，后端不记，所以一律带上「几分钟前测的」。
 */
export interface HealthRecord {
  ok: boolean
  /** 往返毫秒 */
  ms?: number | null
  /** 测的时刻（ms） */
  at: number
  error?: string
  hint?: string
  detail?: string
  /** 成功时的补充：用的哪个模型、回了什么 */
  note?: string
}

const HEALTH_KEY = 'agentlab.health'

function readHealth(): Record<string, HealthRecord> {
  try {
    const raw = JSON.parse(localStorage.getItem(HEALTH_KEY) ?? '{}')
    return raw && typeof raw === 'object' ? raw : {}
  } catch {
    return {}
  }
}

let healthSnap: { records: Record<string, HealthRecord>; checking: Record<string, number> } = {
  records: readHealth(), checking: {},
}
const healthListeners = new Set<() => void>()
const subscribeHealth = (l: () => void) => {
  healthListeners.add(l)
  return () => { healthListeners.delete(l) }
}
function emitHealth(next: Partial<typeof healthSnap>, persist = false) {
  healthSnap = { ...healthSnap, ...next }
  if (persist) {
    try { localStorage.setItem(HEALTH_KEY, JSON.stringify(healthSnap.records)) } catch { /* 隐私模式：只是刷新后不记得 */ }
  }
  healthListeners.forEach((l) => l())
}

export function useHealth(key: string): { record?: HealthRecord; checkingSince?: number } {
  const snap = useSyncExternalStore(subscribeHealth, () => healthSnap, () => healthSnap)
  return { record: snap.records[key], checkingSince: snap.checking[key] }
}

export function setHealth(key: string, record: HealthRecord) {
  emitHealth({ records: { ...healthSnap.records, [key]: record } }, true)
}

export function forgetHealth(key: string) {
  const { [key]: _gone, ...rest } = healthSnap.records
  emitHealth({ records: rest }, true)
}

/**
 * 测一次并记下结果。run 返回后端的 {ok, error, hint, detail}；ms 和 note 由调用方
 * 从各自的字段里取（latency_ms / elapsed_ms）。
 *
 * 后端本身够不着时不记：那说的是「我们连不上后端」，不是这个库或模型坏了，
 * 记成它的失败就是冤枉它。只弹 toast。
 */
export async function checkHealth(
  key: string,
  run: () => Promise<Omit<HealthRecord, 'at'>>,
): Promise<HealthRecord | null> {
  if (healthSnap.checking[key]) return null
  emitHealth({ checking: { ...healthSnap.checking, [key]: Date.now() } })
  let record: HealthRecord | null = null
  try {
    record = { ...(await run()), at: Date.now() }
  } catch (e) {
    const h = humanizeError(e)
    if (h.kind === 'network') {
      toast.error(e)
    } else {
      record = {
        ok: false, at: Date.now(),
        error: h.reason ? `${h.title}：${h.reason}` : h.title, hint: h.action, detail: h.raw,
      }
    }
  } finally {
    const { [key]: _done, ...checking } = healthSnap.checking
    emitHealth({
      checking,
      ...(record ? { records: { ...healthSnap.records, [key]: record } } : {}),
    }, !!record)
  }
  return record
}

/**
 * 连通状态胶囊：状态剪影 + 一句话。
 *
 * 和画布运行态同一套语言：进行中是转着的圆环、正常是带勾的方块、失败是三角、
 * 没测过是空心点——去掉颜色也认得出。正常态安静（字是 dim），出错才用 err 色。
 * 结果刚到的那一下，状态点外圈扩散一次；进页时从缓存读出的旧结果不播。
 */
export function HealthPill({ record, checkingSince, labels, className, stale }: {
  record?: HealthRecord | null
  checkingSince?: number
  labels?: { idle?: string; checking?: string; ok?: string; fail?: string }
  className?: string
  /** 结果已经不代表眼前这份配置（改过了），淡出显示并提示重测 */
  stale?: boolean
}) {
  const clock = useRunClock(!!checkingSince)
  useTicker(30_000, !!record && !checkingSince)
  const state = checkingSince ? 'checking' : !record ? 'idle' : record.ok ? 'ok' : 'fail'
  const status = ({ idle: 'idle', checking: 'running', ok: 'done', fail: 'failed' } as const)[state]
  const color = `var(--st-${status})`

  const [ping, setPing] = useState(0)
  const lastAt = useRef(record?.at)
  useEffect(() => {
    const at = record?.at
    if (at && at !== lastAt.current && Date.now() - at < 3000) setPing(at)
    lastAt.current = at
  }, [record?.at])

  // at 为 0：后端记的上次结果，不知道是什么时候测的，就不写时间。带个「测」字：
  // 这是上次测的时刻，不是此刻的状态
  const rel = record?.at ? formatRelative(record.at) : ''
  const when = !rel ? '' : rel === '刚刚' ? '刚测过' : /前$/.test(rel) ? `${rel}测` : `${rel} 测`
  const text = state === 'checking'
    ? `${labels?.checking ?? '正在测'} · ${formatDuration(Math.max(0, clock - (checkingSince ?? clock)))}`
    : state === 'idle'
      ? (labels?.idle ?? '未测试')
      : state === 'ok'
        ? [labels?.ok ?? '已连通', record?.ms != null ? formatDuration(record.ms) : null, when].filter(Boolean).join(' · ')
        : [labels?.fail ?? '连不上', when].join(' · ')
  const tip = record
    ? [
        record.at ? `${formatDateTime(record.at)} 测的` : '上次探测的结果',
        record.ok ? record.note : record.error,
        stale ? '配置改过了，这个结果不代表眼前这份，重测一次' : null,
      ].filter(Boolean).join('\n')
    : undefined
  // 念给读屏的那一句只在状态切换时变：看得见的那句里有 100ms 一跳的计时和
  // 「3 分钟前」，放进播报区的话测连接期间会一直念、之后每分钟每张卡再念一遍。
  // 时刻写成钟点，不写相对时间
  const spoken = state === 'checking'
    ? '正在测连接'
    : state === 'idle' || !record
      ? ''
      : [
          record.ok
            ? [labels?.ok ?? '已连通', record.ms != null ? formatDuration(record.ms) : null].filter(Boolean).join(' ')
            : `${labels?.fail ?? '连不上'}${record.error ? `：${record.error}` : ''}`,
          record.at ? `${formatTime(record.at)} 测的` : null,
          stale ? '配置改过了，这个结果不代表眼前这份' : null,
        ].filter(Boolean).join('，')

  return (
    <>
      <span
        aria-hidden
        data-health={state}
        title={tip}
        className={clsx('inline-flex items-center gap-1.5 whitespace-nowrap text-2xs tnum', stale && 'opacity-55', className)}
        style={{ color: state === 'fail' ? color : state === 'checking' ? 'var(--accent)' : state === 'ok' ? 'var(--text-dim)' : 'var(--text-faint)' }}
      >
        <span className="relative inline-flex">
          {ping > 0 && (
            <span
              key={ping}
              className="absolute inset-0 animate-ping rounded-full"
              style={{ background: color, animationIterationCount: 1, animationFillMode: 'forwards' }}
              onAnimationEnd={() => setPing(0)}
            />
          )}
          <StatusBadge status={status} size={12} decorative />
        </span>
        {text}
        {stale && state !== 'checking' && <span className="text-faint">· 配置改过了</span>}
      </span>
      <span role="status" className="sr-only">{spoken}</span>
    </>
  )
}

// ---------------------------------------------------------------------------
// 删除后可撤销
// ---------------------------------------------------------------------------

const UNDO_MS = 5000
const pendingDeletes = new Map<number, string>()
let pendingSeq = 0
/**
 * 已经点了删除的对象（按 DELETE 的 url 记）。撤销窗口里 DELETE 还没发，这时列表
 * 一刷新（知识库有文档在处理时 2 秒轮询一次、写完记忆、存完工具都会重拉），后端
 * 照样返回它，行就回来了——toast 还写着「已删除」，再点一次删除还会多排一个
 * DELETE，头一个落地后第二个 404。所以各列表的 load 都要过一遍 withoutDeferred。
 *
 * 撤销、或者删失败放回原处时才拿出去；删成功了也不拿：id 不会再出现，而删之前
 * 发出、删之后才回来的那次列表请求里还带着它
 */
const deferredGone = new Set<string>()

/** 滤掉还在撤销窗口里（或已经删掉）的行。base 是 DELETE 地址去掉 id 的那段 */
export function withoutDeferred<T extends { id: string }>(rows: T[], base: string): T[] {
  return deferredGone.size ? rows.filter((r) => !deferredGone.has(`${base}/${r.id}`)) : rows
}

// 关页、刷新时，还在撤销窗口里的删除照样发出去：用户已经点了删除，刷新一下它
// 又回来了，比多等 5 秒更让人糊涂。keepalive 让请求活过页面卸载
if (typeof window !== 'undefined') {
  window.addEventListener('pagehide', () => {
    for (const url of pendingDeletes.values()) {
      void fetch(url, { method: 'DELETE', keepalive: true }).catch(() => {})
    }
    pendingDeletes.clear()
  })
}

/**
 * 低代价的删除：列表里先拿掉，toast 给 5 秒「撤销」，到点才真发 DELETE。
 * 比弹窗确认少一次打断，误删了又救得回来。发失败了放回原处并说明。
 *
 * url 是关页时补发用的（/api/...），commit 是正常路径。url 同时是「已删」的
 * 记号：列表的 load 用 withoutDeferred 按它滤掉这一行。
 */
export function deferDelete({ what, url, hide, restore, commit, done }: {
  what: string
  url: string
  hide: () => void
  restore: () => void
  commit: () => Promise<unknown>
  done?: () => void
}) {
  deferredGone.add(url)
  hide()
  const id = ++pendingSeq
  pendingDeletes.set(id, url)
  // 先拿掉记号再 restore：restore 多半是重拉列表，记号还在的话又被滤掉
  const putBack = () => { deferredGone.delete(url); restore() }
  const timer = setTimeout(async () => {
    if (!pendingDeletes.delete(id)) return
    toast.dismiss(toastId)
    try {
      await commit()
      done?.()
    } catch (e) {
      putBack()
      const h = humanizeError(e)
      toast.error(`没删掉${what}：${h.reason ? `${h.title}，${h.reason}` : h.title}`, { detail: h.raw })
    }
  }, UNDO_MS)
  const toastId = toast(`已删除${what}`, 'info', {
    duration: UNDO_MS,
    key: `undo:${id}`,
    action: {
      label: '撤销',
      onClick: () => {
        if (!pendingDeletes.delete(id)) return
        clearTimeout(timer)
        putBack()
      },
    },
  })
}

// ===========================================================================
// 数据源
// ===========================================================================

/**
 * 数据源管理。/data 页的两个标签（数据库 / 表格）共用这一个组件，view 只决定
 * 显示哪一类——切标签不卸载，列表和卡片上的展开状态都还在。
 *
 * 这里的每一项配置都会影响两件事：Copilot 编排时看得见什么，以及 agent 运行时
 * 能查什么。所以表单上的说明写的是"这项填错会怎样"，而不是字段的字面意思——
 * Oracle 少填一个 schema，探查结果就是空的，而报错信息只会说 ORA-00942。
 */
export type DataView = 'databases' | 'tables'

/** 传上来的表格落在数据目录的 uploads/tables/<name>.db。后端没给来源标记，按路径认 */
export const isUploadedTable = (row: any): boolean =>
  row?.kind === 'sqlite' && /[\\/]uploads[\\/]tables[\\/][^\\/]+\.db$/.test(row?.database ?? '')

const NAME_RE = /^[a-z][a-z0-9_]{0,40}$/

/** 「OpenAI 兼容（DeepSeek / …）」→「OpenAI 兼容」。卡片上的类型标签要短 */
export const shortLabel = (label?: string | null) => (label ?? '').replace(/[（(].*$/, '').trim()

export function DataSourcesTab({ view = 'databases' }: { view?: DataView }) {
  const navigate = useNavigate()
  const [rows, setRows] = useState<any[] | null>(null)
  const [kinds, setKinds] = useState<any[]>([])
  const [loadError, setLoadError] = useState<unknown>(null)
  const [editing, setEditing] = useState<any | null>(null)
  const [uploading, setUploading] = useState<{ name?: string } | null>(null)
  const [kick, setKick] = useState<{ id: string; seq: number } | null>(null)
  const loadSeq = useRef(0)
  const hasRows = useRef(false)

  // 首次加载才画骨架；之后的刷新保留列表、不卸载——展开的表清单、滚动位置、
  // 卡片上的忙碌态都长在卡片上，以前整页换成 Spinner，这些全丢，还跳回顶部
  const load = async () => {
    const seq = ++loadSeq.current
    try {
      const [list, meta] = await Promise.all([api.datasources.list(), api.datasources.kinds()])
      if (seq !== loadSeq.current) return
      hasRows.current = true
      setRows(list)
      setKinds(meta.kinds ?? [])
      setLoadError(null)
    } catch (e) {
      if (seq !== loadSeq.current) return
      if (hasRows.current) toast.error(e)
      else setLoadError(e)
    }
  }
  useEffect(() => { void load() }, [])
  useOnReconnect(load)

  const upsert = (row: any) => setRows((rs) => {
    if (!rs) return [row]
    return rs.some((r) => r.id === row.id)
      ? rs.map((r) => (r.id === row.id ? row : r))
      : [...rs, row].sort((a, b) => a.name.localeCompare(b.name))
  })
  const drop = (id: string) => setRows((rs) => rs && rs.filter((r) => r.id !== id))

  const tables = view === 'tables'
  const shown = rows?.filter((r) => isUploadedTable(r) === tables) ?? []
  const kindOf = (k: string) => kinds.find((x) => x.value === k)

  return (
    <div>
      {tables ? (
        <SectionBar
          title="表格"
          hint="传 Excel / CSV，每个工作表变成一张可以用 SQL 查的表——数字是算出来的，查得到出处。同名重传就地替换，工具名不变。"
        >
          <button className="btn btn-primary btn-sm" onClick={() => setUploading({})}>
            <Upload size={12} /> 传表格
          </button>
        </SectionBar>
      ) : (
        <SectionBar title="数据库" hint="接入后助手编排时看得见这些库的结构，agent 运行时能直接查。卡片右侧是上次测连接的结果和测的时间。">
          <button className="btn btn-primary btn-sm"
                  onClick={() => setEditing({ kind: 'mysql', readonly: true, enabled: true, options: {} })}>
            <Plus size={12} /> 接入数据库
          </button>
        </SectionBar>
      )}

      {rows === null ? (
        loadError
          ? <ErrorState error={loadError} onRetry={() => void load()} />
          : <Skeleton rows={3} height={72} gap={8} />
      ) : !shown.length ? (
        tables ? (
          <EmptyState
            icon={<FileSpreadsheet size={22} />}
            title="还没有传过表格"
            body="Excel（.xlsx）或 CSV / TSV。传上来之后在「问数据」里直接提问，不用自己写 SQL。"
            action={<button className="btn btn-primary btn-sm" onClick={() => setUploading({})}><Upload size={12} /> 传表格</button>}
          />
        ) : (
          <EmptyState
            icon={<Database size={22} />}
            title="还没有接入数据库"
            body="MySQL / PostgreSQL / Oracle / SQLite 都行。只有 Excel？去「表格」标签传上来。"
            action={
              <div className="flex gap-2">
                <button className="btn btn-primary btn-sm"
                        onClick={() => setEditing({ kind: 'mysql', readonly: true, enabled: true, options: {} })}>
                  <Plus size={12} /> 接入数据库
                </button>
                <button className="btn btn-sm" onClick={() => navigate('/data/tables')}>
                  <FileSpreadsheet size={12} /> 传表格
                </button>
              </div>
            }
          />
        )
      ) : (
        <div className="space-y-2.5">
          {shown.map((row) => (
            <SourceCard
              key={row.id}
              row={row}
              meta={kindOf(row.kind)}
              onChange={upsert}
              onRemoved={drop}
              onEdit={() => setEditing(row)}
              onReupload={() => setUploading({ name: row.name })}
              kick={kick && kick.id === row.id ? kick.seq : 0}
            />
          ))}
        </div>
      )}

      {editing && (
        <SourceEditor
          source={editing}
          kinds={kinds}
          onClose={() => setEditing(null)}
          onSaved={(row, tested) => {
            const isNew = !editing.id
            setEditing(null)
            upsert(row)
            if (tested) setHealth(`datasource:${row.id}`, tested)
            if (isNew) {
              toast.ok(`已接入「${row.name}」。下一步：探查结构，助手靠它写 SQL`, {
                action: { label: '探查结构', onClick: () => setKick({ id: row.id, seq: Date.now() }) },
              })
            } else {
              toast.ok(`已保存「${row.name}」`)
            }
          }}
        />
      )}

      {uploading && (
        <TableUploader
          initialName={uploading.name}
          taken={(rows ?? []).filter((r) => !isUploadedTable(r)).map((r) => r.name)}
          onClose={() => setUploading(null)}
          onImported={(row) => {
            upsert(row)
            if (!tables) navigate('/data/tables', { replace: true })
          }}
        />
      )}
    </div>
  )
}

/** 结构同步超过这么久就提示可能过期：库表会变，Copilot 照旧表写 SQL 只会报错 */
const STALE_SCHEMA_MS = 7 * 24 * 3600_000

function SourceCard({ row, meta, onChange, onRemoved, onEdit, onReupload, kick }: {
  row: any; meta?: any
  onChange: (row: any) => void
  onRemoved: (id: string) => void
  onEdit: () => void
  onReupload: () => void
  /** 非 0 时自动点一次「探查结构」（新建后 toast 上的按钮） */
  kick: number
}) {
  const healthKey = `datasource:${row.id}`
  const { record, checkingSince } = useHealth(healthKey)
  const workflows = useCatalog((s) => s.workflows)
  const [busy, setBusy] = useState('')
  const uploaded = isUploadedTable(row)
  // 传上来的表通常只有一两张，直接摊开；库动辄几十上百个对象，默认收着
  const [open, setOpen] = useState(() => uploaded && row.table_count > 0 && row.table_count <= 3)
  const clock = useRunClock(!!busy)
  const busySince = useRef(0)
  useTicker(60_000)
  const now = Date.now()

  const test = () => checkHealth(healthKey, async () => {
    const r = await api.datasources.test(row.id)
    return { ok: !!r.ok, ms: r.elapsed_ms ?? null, error: r.error, hint: r.hint, detail: r.detail }
  })

  const configured: string = row.options?.schema ?? ''
  const configuredLabel = configured || '默认 schema'

  const introspect = async (schema?: string) => {
    if (busy) return
    busySince.current = Date.now()
    setBusy(schema ? `schema:${schema}` : 'introspect')
    try {
      const next = await api.datasources.introspect(row.id, schema)
      onChange(next)
      if (schema && schema !== configured) {
        await settleOther(schema, next)
      } else if (next.table_count) {
        toast.ok(`「${row.name}」探到 ${formatNumber(next.table_count)} 个对象`)
      } else if (!next.schema_error) {
        // 连得上、也没报错，就是这个 schema 下真的没有对象。探查失败的红字
        // 卡片自己会显示，不再叠一条 toast
        toast.warn('连得上，但这个 schema 下没有对象，多半是 schema 填的不对')
      }
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy('')
    }
  }

  /**
   * 用别的 schema 探完之后：要么写进配置，要么马上换回配置里的。
   *
   * 后端的探查没有「只看不存」，结果直接落进缓存——助手和「问数据」此刻看到的
   * 已经是这份。以前「只看看，不改」就停在这儿：卡片上的 schema 还写着原来的，
   * 对象数和结构却是另一个 schema 的，只有一条几秒就消失的 toast 提过一句。
   * 现在不写进配置就重新探一次配置里的，让缓存和配置始终对得上
   */
  const settleOther = async (schema: string, next: any) => {
    if (!next.table_count) {
      await restoreConfigured(schema, `「${schema}」下没探到对象，配置不改`)
      return
    }
    const names: string[] = await api.datasources.schema(row.id).then((s) => s.tables ?? [], () => [])
    const sample = names.slice(0, 6).map((t) => t.slice(t.lastIndexOf('.') + 1))
    const ok = await confirmDialog({
      title: `把 schema 改成「${schema}」？`,
      body: `用 ${schema} 探到了 ${formatNumber(next.table_count)} 个对象${sample.length ? `，比如 ${sample.join('、')}${names.length > sample.length ? ' 等' : ''}` : ''}。`,
      consequences: [
        `改：写进这个数据源的配置，以后探查结构、助手写 SQL 都用 ${schema}`,
        `不改：马上重新探查配置里的「${configuredLabel}」，助手看到的结构跟着换回去`,
      ],
      confirmLabel: `改成 ${schema}`,
      cancelLabel: `不改，换回 ${configured || '默认'}`,
    })
    if (!ok) {
      await restoreConfigured(schema, '没改配置')
      return
    }
    try {
      onChange(await api.datasources.update(row.id, { options: { ...(next.options ?? {}), schema } }))
      toast.ok(`已把「${row.name}」的 schema 改成 ${schema}`)
    } catch (e) {
      const h = humanizeError(e)
      toast.error(`没改成（${h.title}）：配置里还是「${configuredLabel}」，缓存里却是 ${schema} 的结构。再点一次「探查结构」换回去`, { detail: h.raw })
    }
  }

  const restoreConfigured = async (probed: string, lead: string) => {
    busySince.current = Date.now()
    setBusy('introspect')
    try {
      onChange(await api.datasources.introspect(row.id))
      toast.info(`${lead}。已重新探查「${configuredLabel}」，助手看到的结构和配置一致`)
    } catch (e) {
      const h = humanizeError(e)
      toast.error(`${lead}，但没能换回「${configuredLabel}」的结构（${h.title}）：助手眼下看到的还是 ${probed} 的。再点一次「探查结构」`, { detail: h.raw })
    }
  }

  useEffect(() => { if (kick) void introspect() }, [kick])

  // 配置里的 schema 探不出对象（失败或是空的）时，才给「换个 schema」：换着探
  // 会改掉缓存，好好的库没必要冒这个险——要换 schema 走「编辑」
  const synced = parseServerTime(row.schema_synced_at)?.getTime() ?? 0
  const canProbeOther = !uploaded && row.kind !== 'sqlite' && !row.table_count && (!!row.schema_error || synced > 0)
  const introspectOther = async () => {
    const others: string[] = (row.available_schemas ?? []).filter((s: string) => s !== configured).slice(0, 8)
    const schema = await promptDialog({
      title: '用哪个 schema 探查？',
      body: `探到的结构会先换掉眼下缓存的那份；探到对象再问要不要写进配置，不写就马上换回「${configuredLabel}」。${others.length ? `这台服务器上还有：${others.join('、')}` : ''}`,
      label: 'schema',
      placeholder: others[0] ?? (row.kind === 'postgres' ? 'public' : row.kind === 'oracle' ? 'ANALYTICS' : row.database || ''),
      confirmLabel: '探查',
      validate: (v) => (!v ? '填一个 schema 名' : v === configured ? `「${v}」就是配置里的，点「探查结构」就行` : null),
    })
    if (schema) await introspect(schema)
  }

  const remove = async () => {
    const using = workflowsMentioning(workflows, row.tools ?? [])
    const ok = await confirmDialog({
      title: `删除数据源「${row.name}」？`,
      danger: true,
      consequences: [
        row.tools?.length ? `工具 ${row.tools.join('、')} 跟着消失` : '',
        using.length
          ? `${workflowList(using)}用到了这些工具，运行到那一步会失败`
          : '眼下没有工作流直接引用它的工具',
        '助手和「问数据」都看不到这个库了',
        uploaded ? '传上来的这几张表不能再查了，要用得重新传' : '连接信息（含密码）一并删除，不可恢复',
      ].filter(Boolean),
      requireText: row.name,
      confirmLabel: '删除数据源',
    })
    if (!ok) return
    try {
      await api.datasources.remove(row.id)
      forgetHealth(healthKey)
      onRemoved(row.id)
      toast.ok(`已删除数据源「${row.name}」`)
    } catch (e) {
      toast.error(e)
    }
  }

  // 数量变了（刚探查完）才播一次入场，首次渲染不播
  const lastCount = useRef(row.table_count)
  const countChanged = lastCount.current !== row.table_count
  useEffect(() => { lastCount.current = row.table_count }, [row.table_count])

  const staleSchema = !!synced && now - synced > STALE_SCHEMA_MS
  const failed = record && !record.ok && !checkingSince

  return (
    <article className="rounded-lg border bg-panel" data-source={row.name}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 px-3 pt-2.5">
        {uploaded
          ? <FileSpreadsheet size={14} className={row.enabled ? 'text-dim' : 'text-faint'} aria-hidden />
          : <Database size={14} className={row.enabled ? 'text-dim' : 'text-faint'} aria-hidden />}
        <span className="mono text-sm font-medium">{row.name}</span>
        {!uploaded && <span className="chip">{shortLabel(meta?.label) || row.kind}</span>}
        {row.readonly
          ? <span className="chip" title="只能 SELECT，模型写的 UPDATE / DELETE 会被拦下"><Lock size={10} aria-hidden /> 只读</span>
          : <span className="chip" style={{ color: 'var(--warn)', borderColor: 'color-mix(in srgb, var(--warn) 45%, transparent)' }}
                  title="模型生成的 UPDATE / DELETE 会真的执行">
              <AlertTriangle size={10} aria-hidden /> 可写
            </span>}
        {!row.enabled && <span className="chip" title="助手和 agent 都看不到它">已停用</span>}
        <span className="flex-1" />
        <HealthPill record={record} checkingSince={checkingSince} />
        <div className="flex items-center gap-1">
          <button className="btn btn-sm" disabled={!!checkingSince} onClick={() => void test()}>
            <Plug size={11} aria-hidden /> 测连接
          </button>
          <button className="btn btn-sm tnum" disabled={!!busy} onClick={() => void introspect()}
                  title="读取表结构并缓存，助手靠它写 SQL">
            {busy === 'introspect'
              ? <><Spinner size={11} /> 探查中 {formatDuration(clock - busySince.current)}</>
              : <><RefreshCw size={11} aria-hidden /> 探查结构</>}
          </button>
          {uploaded
            ? <button className="btn btn-sm btn-ghost" onClick={onReupload} title="同名重传：就地替换里面的数据，工具名不变">重传</button>
            : <button className="btn btn-sm btn-ghost" onClick={onEdit}>编辑</button>}
          <DeleteButton label={`删除数据源 ${row.name}`} onClick={() => void remove()} />
        </div>
      </div>

      {row.description && <p className="px-3 pt-1 text-xs leading-relaxed text-dim">{row.description}</p>}

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 pb-2.5 pt-1.5 text-2xs text-faint">
        <Address row={row} meta={meta} uploaded={uploaded} />
        {row.options?.schema && <span className="chip">schema {row.options.schema}</span>}
        {!!row.tools?.length && <span className="mono">{row.tools.join(' · ')}</span>}
      </div>

      {failed && (
        <div className="px-3 pb-3">
          <ErrorState compact error={record} onRetry={() => void test()} />
        </div>
      )}

      {row.schema_error && (
        <div
          role="alert"
          className="mx-3 mb-3 rounded-lg border px-3 py-2 text-xs leading-relaxed"
          style={{
            borderColor: 'color-mix(in srgb, var(--err) 35%, var(--border))',
            background: 'color-mix(in srgb, var(--err) 6%, transparent)',
          }}
        >
          <div className="font-medium text-[var(--err)]">上次探查失败</div>
          <div className="mt-0.5 break-all text-dim">{row.schema_error}</div>
          {!!row.available_schemas?.length && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <span className="text-faint">
                当前是「{row.options?.schema || row.database || '默认'}」。这台服务器上还有，点一个换它探查：
              </span>
              {row.available_schemas.slice(0, 12).map((s: string) => (
                <button key={s} className="chip hover:border-[var(--accent)] hover:text-fg" disabled={!!busy}
                        onClick={() => void introspect(s)}>
                  {busy === `schema:${s}` ? <Spinner size={10} /> : <Search size={10} aria-hidden />}
                  用 {s} 探查
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="flex items-center gap-2 border-t px-3 py-1.5 text-2xs">
        {row.table_count ? (
          <button
            className="-ml-1 flex items-center gap-1.5 rounded px-1 py-0.5 text-dim hover:bg-hover hover:text-fg"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
          >
            <ChevronRight size={12} className={clsx('transition-transform', open && 'rotate-90')} aria-hidden />
            <Table2 size={11} aria-hidden />
            <span key={row.table_count} className={clsx('tnum', countChanged && 'fade-up')}>
              {formatNumber(row.table_count)}
            </span>
            个对象
          </button>
        ) : row.schema_error ? (
          // "还没探查"和"探查失败了"是两回事：前者点一下按钮就行，后者点了
          // 也没用。以前两者显示成同一句，没有一处说的是真话
          // 红字上面那块已经说了，这里只说后果，不再叠一道红
          <span className="text-faint">结构不可用：助手看不到这个库有哪些表</span>
        ) : (
          <span className="text-[var(--warn)]">结构还没探查 · 助手看不到有哪些表</span>
        )}
        {canProbeOther && (
          <button className="inline-flex items-center gap-1 rounded px-1 py-0.5 text-faint hover:bg-hover hover:text-fg disabled:opacity-50"
                  disabled={!!busy} onClick={() => void introspectOther()} data-probe-schema
                  title="指定一个 schema 探查；探到了再决定要不要写进配置，不写就换回原来的">
            {busy.startsWith('schema:') ? <Spinner size={10} /> : <Search size={10} aria-hidden />}
            换个 schema 探查…
          </button>
        )}
        <span className="flex-1" />
        {synced > 0 && (
          <span className={clsx('tnum', staleSchema ? 'text-[var(--warn)]' : 'text-faint')}
                title={`${formatDateTime(row.schema_synced_at)} 同步${staleSchema ? '。库表可能变过了，点「探查结构」重新同步' : ''}`}>
            {staleSchema && <AlertTriangle size={10} className="mr-1 inline" aria-hidden />}
            结构同步于 {formatRelative(row.schema_synced_at)}
          </span>
        )}
      </div>

      {open && row.table_count > 0 && <SchemaBrowser row={row} />}
    </article>
  )
}

/** 连接地址。端口为空时不留尾巴冒号，淡色写上默认端口 */
function Address({ row, meta, uploaded }: { row: any; meta?: any; uploaded: boolean }) {
  if (uploaded) {
    return <span>上传的表格{row.table_count ? ` · ${row.table_count} 张表` : ''}</span>
  }
  if (row.kind === 'sqlite') return <span className="mono break-all">{row.database}</span>
  const target = row.kind === 'oracle'
    ? (row.options?.service_name ? `/${row.options.service_name}` : row.options?.sid ? `:${row.options.sid}` : '')
    : row.database ? `/${row.database}` : ''
  return (
    <span className="mono break-all">
      {row.username ? `${row.username}@` : ''}{row.host ?? ''}
      {row.port
        ? `:${row.port}`
        : meta?.default_port ? <span className="opacity-60" title="没填端口，用默认端口">:{meta.default_port}</span> : null}
      {target}
    </span>
  )
}

interface TableDetail {
  kind: string
  name: string
  comment?: string
  columns: { name: string; type: string; pk: boolean; notNull: boolean; note: string }[]
  /** 解析不出来时的原文（比如「数据源里没有这张表」） */
  text?: string
}

/**
 * 解析 GET /datasources/{id}/schema?table= 的 detail 文本：
 *   视图 ANALYTICS.X（注释，可能跨行）
 *     COL  TYPE  主键、非空、列注释
 * 列之间用两个以上空格分隔。
 */
export function parseTableDetail(text: string): TableDetail {
  const lines = (text ?? '').split('\n')
  const first = lines.findIndex((l) => l.startsWith('  '))
  const head = (first < 0 ? lines : lines.slice(0, first)).join('\n')
  const m = head.match(/^(表|视图) (\S+?)(?:（([\s\S]*)）)?$/)
  if (!m) return { kind: '', name: '', columns: [], text }
  const columns: TableDetail['columns'] = []
  for (const line of first < 0 ? [] : lines.slice(first)) {
    if (!line.startsWith('  ')) {
      // 列注释里的换行：接到上一列后面
      const last = columns[columns.length - 1]
      if (last) last.note = `${last.note}\n${line}`.trim()
      continue
    }
    const [name, type = '', marks = ''] = line.trim().split(/\s{2,}/)
    const tags = marks ? marks.split('、') : []
    columns.push({
      name, type,
      pk: tags.includes('主键'),
      notNull: tags.includes('非空'),
      note: tags.filter((t) => t !== '主键' && t !== '非空').join('、'),
    })
  }
  return { kind: m[1], name: m[2], comment: m[3]?.trim(), columns }
}

/** 一次最多画这么多行：上千个对象的库全画出来会卡，过滤一下就够用了 */
const SCHEMA_ROWS = 300

/**
 * 结构浏览器：Copilot 写 SQL 靠的就是这份结构，用户得能方便地核对「它看到了
 * 什么」。按 schema 分组，一行一张表，点开懒加载列（名称 / 类型 / 说明）。
 */
function SchemaBrowser({ row }: { row: any }) {
  const [tables, setTables] = useState<string[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [q, setQ] = useState('')
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const [details, setDetails] = useState<Record<string, TableDetail | { error: unknown } | 'loading'>>({})

  // 重新探查后表清单和列都可能变了：重取清单、清掉列缓存，但展开着的那几张
  // 保持展开（下面会按需重取）
  useEffect(() => {
    let live = true
    setDetails({})
    api.datasources.schema(row.id).then(
      (s) => { if (live) { setTables(s.tables ?? []); setError(null) } },
      (e) => { if (live) setError(e) },
    )
    return () => { live = false }
  }, [row.id, row.schema_synced_at, row.table_count])

  useEffect(() => {
    for (const t of expanded) {
      if (details[t]) continue
      setDetails((d) => ({ ...d, [t]: 'loading' }))
      api.datasources.schema(row.id, t).then(
        (s) => setDetails((d) => ({ ...d, [t]: parseTableDetail(s.detail ?? '') })),
        (e) => setDetails((d) => ({ ...d, [t]: { error: e } })),
      )
    }
  }, [expanded, details, row.id])

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return (tables ?? []).filter((t) => !needle || t.toLowerCase().includes(needle))
  }, [tables, q])
  const groups = useMemo(() => {
    const m = new Map<string, string[]>()
    for (const t of filtered.slice(0, SCHEMA_ROWS)) {
      const i = t.lastIndexOf('.')
      const g = i > 0 ? t.slice(0, i) : ''
      m.set(g, [...(m.get(g) ?? []), t])
    }
    return [...m.entries()]
  }, [filtered])

  const toggle = (t: string) => setExpanded((s) => {
    const next = new Set(s)
    if (next.has(t)) next.delete(t)
    else next.add(t)
    return next
  })

  if (error) return <div className="border-t p-3"><ErrorState compact error={error} /></div>
  if (!tables) return <div className="border-t p-3"><Skeleton rows={4} height={10} gap={8} /></div>

  return (
    <div className="border-t" data-schema-browser>
      {tables.length > 8 && (
        <div className="flex items-center gap-2 border-b px-3 py-1.5">
          <Search size={12} className="text-faint" aria-hidden />
          <input
            className="min-w-0 flex-1 bg-transparent text-xs outline-none placeholder:text-faint"
            placeholder={`在 ${tables.length} 个对象里找…`}
            aria-label="过滤表名"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          {q && <span className="tnum text-2xs text-faint">{filtered.length} 个匹配</span>}
        </div>
      )}
      {/* 不留上内边距：sticky 的分组头贴着滚动区的内边距停，留了就会有一条缝，下面的行从缝里漏出来 */}
      <div className="max-h-[420px] overflow-y-auto px-2 pb-1.5">
        {!filtered.length && <div className="px-1 py-2 text-xs text-faint">没有匹配「{q}」的对象</div>}
        {groups.map(([group, list]) => (
          <div key={group || '_'} className="mb-1">
            {(groups.length > 1 || group) && (
              <div className="sticky top-0 z-[1] -mx-2 flex items-center gap-1.5 border-b border-[var(--hairline)] bg-panel px-3 py-1 text-2xs font-medium text-faint">
                {group || '默认 schema'} <span className="tnum opacity-70">{list.length}</span>
              </div>
            )}
            {list.map((t) => {
              const isOpen = expanded.has(t)
              const d = details[t]
              return (
                <div key={t}>
                  <button
                    className="flex w-full items-center gap-1.5 rounded px-1 py-[3px] text-left hover:bg-hover"
                    aria-expanded={isOpen}
                    onClick={() => toggle(t)}
                  >
                    <ChevronRight size={11} className={clsx('shrink-0 text-faint transition-transform', isOpen && 'rotate-90')} aria-hidden />
                    <span className="mono truncate text-xs">{group ? t.slice(group.length + 1) : t}</span>
                    {d && typeof d === 'object' && 'columns' in d && d.columns.length > 0 && (
                      <span className="tnum text-2xs text-faint">{d.columns.length} 列</span>
                    )}
                  </button>
                  {isOpen && <ColumnList detail={d} />}
                </div>
              )
            })}
          </div>
        ))}
        {filtered.length > SCHEMA_ROWS && (
          <div className="px-1 py-1.5 text-2xs text-faint">
            还有 {formatNumber(filtered.length - SCHEMA_ROWS)} 个没列出来，输入关键字缩小范围
          </div>
        )}
      </div>
    </div>
  )
}

function ColumnList({ detail }: { detail?: TableDetail | { error: unknown } | 'loading' }) {
  if (!detail || detail === 'loading') {
    return <div className="mb-1 ml-5 py-1"><Skeleton rows={3} height={9} gap={6} /></div>
  }
  if ('error' in detail) return <div className="mb-1.5 ml-5"><ErrorState compact error={detail.error} /></div>
  if (!detail.columns.length) {
    return <div className="mb-1.5 ml-5 whitespace-pre-line text-2xs text-faint">{detail.text || '没有列信息'}</div>
  }
  return (
    <div className="mb-1.5 ml-5 mt-0.5 overflow-hidden rounded-md border bg-bg">
      {(detail.comment || detail.kind === '视图') && (
        <div className="flex gap-2 border-b px-2 py-1 text-2xs text-dim">
          {detail.kind === '视图' && <span className="chip">视图</span>}
          {detail.comment && <span className="whitespace-pre-line">{detail.comment}</span>}
        </div>
      )}
      <table className="w-full text-2xs">
        <thead className="text-faint">
          <tr className="border-b">
            <th className="w-[38%] px-2 py-1 text-left font-medium">列</th>
            <th className="w-[24%] px-2 py-1 text-left font-medium">类型</th>
            <th className="px-2 py-1 text-left font-medium">说明</th>
          </tr>
        </thead>
        <tbody>
          {detail.columns.map((c) => (
            <tr key={c.name} className="border-b border-[var(--hairline)] last:border-0">
              <td className="mono px-2 py-[3px]">
                {c.pk && <KeyRound size={9} className="mr-1 inline text-[var(--accent)]" aria-label="主键" />}
                {c.name}
              </td>
              <td className="mono px-2 py-[3px] text-faint">{c.type}</td>
              <td className="px-2 py-[3px] text-dim">
                {c.notNull && <span className="mr-1.5 text-faint">非空</span>}
                <span className="whitespace-pre-line">{c.note}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ---------------------------------------------------------------------------
// 接入 / 编辑数据库
// ---------------------------------------------------------------------------

interface SourceForm {
  name: string
  kind: string
  host: string
  port: number | null
  database: string
  username: string
  password: string
  description: string
  readonly: boolean
  enabled: boolean
  schema: string
  oracleMode: 'service_name' | 'sid'
  serviceName: string
  sid: string
  /** options 里除 schema / service_name / sid 之外的键，全部摊开可编辑 */
  advanced: [string, string][]
}

const OWN_OPTION_KEYS = new Set(['schema', 'service_name', 'sid'])

function toForm(source: any): SourceForm {
  const o = source.options ?? {}
  const oracle = source.kind === 'oracle'
  return {
    name: source.name ?? '',
    kind: source.kind ?? 'mysql',
    host: source.host ?? '',
    port: source.port ?? null,
    database: oracle ? '' : (source.database ?? ''),
    username: source.username ?? '',
    password: '',
    description: source.description ?? '',
    readonly: source.readonly ?? true,
    enabled: source.enabled ?? true,
    schema: o.schema ?? '',
    oracleMode: o.sid && !o.service_name ? 'sid' : 'service_name',
    // 老数据把服务名存在 database 上：回显它，保存时后端会挪进 options
    serviceName: o.service_name ?? (oracle ? source.database ?? '' : ''),
    sid: o.sid ?? '',
    advanced: Object.entries(o)
      .filter(([k]) => !OWN_OPTION_KEYS.has(k))
      .map(([k, v]) => [k, String(v ?? '')]),
  }
}

function toBody(form: SourceForm, isNew: boolean, wasOracle = true): any {
  const options: Record<string, string> = {}
  for (const [k, v] of form.advanced) if (k.trim() && v.trim()) options[k.trim()] = v.trim()
  if (form.schema.trim()) options.schema = form.schema.trim()
  const oracle = form.kind === 'oracle'
  if (oracle) {
    // 只写选中的那一个，另一个不带：两处都有值时引擎听 service_name，
    // 留着旧的 sid 只会在下次切换时暗中说了算
    if (form.oracleMode === 'sid') { if (form.sid.trim()) options.sid = form.sid.trim() }
    else if (form.serviceName.trim()) options.service_name = form.serviceName.trim()
  }
  const body: any = {
    kind: form.kind,
    host: form.kind === 'sqlite' ? null : form.host.trim() || null,
    port: form.kind === 'sqlite' ? null : form.port,
    username: form.kind === 'sqlite' ? null : form.username.trim() || null,
    options,
    readonly: form.readonly,
    description: form.description,
    enabled: form.enabled,
  }
  // Oracle 不写 database：服务名只存一处（options），写了反而可能压住它。
  // 从别的类型改成 Oracle 时要清掉：原来那个库名会被后端当成 service_name
  if (!oracle) body.database = form.database.trim() || null
  else if (isNew || !wasOracle) body.database = null
  // 不传 password 表示不动；只有真填了才提交
  if (form.password) body.password = form.password
  if (isNew) body.name = form.name.trim()
  return body
}

const FIELD_LABEL: Record<string, string> = {
  host: '主机', database: '数据库', username: '用户名',
}

function SourceEditor({ source, kinds, onClose, onSaved }: {
  source: any; kinds: any[]; onClose: () => void
  onSaved: (row: any, tested?: HealthRecord) => void
}) {
  const isNew = !source.id
  const [initial] = useState(() => toForm(source))
  const [form, setForm] = useState<SourceForm>(initial)
  const [saving, setSaving] = useState(false)
  const [test, setTest] = useState<{ since?: number; result?: HealthRecord; sig?: string }>({})
  const resultRef = useRef<HTMLDivElement>(null)
  const meta = kinds.find((k) => k.value === form.kind)
  const set = (patch: Partial<SourceForm>) => setForm((f) => ({ ...f, ...patch }))

  const oracle = form.kind === 'oracle'
  const sqlite = form.kind === 'sqlite'
  const needs: string[] = meta?.needs ?? []
  const nameError = isNew && form.name && !NAME_RE.test(form.name)
    ? '只能用小写字母开头的小写字母、数字、下划线（它会成为工具名 db_query__<标识>）'
    : null

  const missing: string[] = []
  if (isNew && !form.name) missing.push('标识')
  // 密码不拦：后端允许不带密码（trust 认证、本机 root 免密），拦了的话这种库
  // 既建不了、也改不了——连改一句说明都得先编一个密码，而编的密码又会把连接弄坏
  const noPassword = needs.includes('password') && !form.password && !source.has_password
  for (const n of needs) {
    if (n === 'password') continue
    if (n === 'database') {
      if (!form.database.trim()) missing.push(sqlite ? '数据库文件路径' : '数据库')
    } else if (!String((form as any)[n] ?? '').trim()) {
      missing.push(FIELD_LABEL[n] ?? n)
    }
  }
  if (oracle) {
    if (form.oracleMode === 'sid' ? !form.sid.trim() : !form.serviceName.trim()) {
      missing.push(form.oracleMode === 'sid' ? 'SID' : 'service_name')
    }
  }
  const required = (key: string) => needs.includes(key)
  const blocked = missing.length > 0 || !!nameError
  const body = toBody(form, isNew, source.kind === 'oracle')
  const sig = JSON.stringify({ ...body, password: form.password })
  const dirty = JSON.stringify(form) !== JSON.stringify(initial)
  const testFresh = test.result && test.sig === sig

  const runTest = async () => {
    const at = Date.now()
    setTest({ since: at })
    try {
      const r = await api.datasources.testConfig({
        ...body, id: source.id, name: form.name || source.name || undefined,
      })
      setTest({ sig, result: { ok: !!r.ok, ms: r.elapsed_ms ?? null, at: Date.now(), error: r.error, hint: r.hint, detail: r.detail } })
      if (!r.ok) requestAnimationFrame(() => resultRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }))
    } catch (e) {
      setTest({})
      toast.error(e)
    }
  }

  const save = async () => {
    if (blocked) return
    setSaving(true)
    try {
      const row = isNew
        ? await api.datasources.create(body)
        : await api.datasources.update(source.id, body)
      onSaved(row, testFresh ? test.result : undefined)
    } catch (e) {
      toast.error(e)
    } finally {
      setSaving(false)
    }
  }

  const advancedDefs: any[] = (meta?.advanced ?? []).filter((a: any) => !OWN_OPTION_KEYS.has(a.key))
  const knownAdvanced = new Set(advancedDefs.map((a) => a.key))
  const advValue = (k: string) => form.advanced.find(([key]) => key === k)?.[1] ?? ''
  const setAdv = (k: string, v: string) => setForm((f) => {
    const rest = f.advanced.filter(([key]) => key !== k)
    return { ...f, advanced: v ? [...rest, [k, v]] : rest }
  })
  const extras = form.advanced.map((kv, i) => [kv, i] as const).filter(([[k]]) => !knownAdvanced.has(k))
  const advancedCount = form.advanced.filter(([, v]) => v).length

  return (
    <Modal
      open
      onClose={onClose}
      dirty={dirty}
      width={620}
      title={isNew ? '接入数据库' : `编辑「${source.name}」`}
      footer={
        <>
          <div className="mr-auto flex min-w-0 items-center">
            {(test.since || test.result) && (
              <HealthPill
                record={test.result}
                checkingSince={test.since}
                stale={!!test.result && !testFresh}
                labels={{ ok: '连得上', fail: '连不上' }}
              />
            )}
          </div>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn" disabled={!!test.since || blocked} onClick={() => void runTest()}
                  title={blocked ? `还缺：${missing.join('、') || '标识格式'}` : '用眼前这份配置连一下，不保存'}>
            {test.since ? <Spinner size={11} /> : <Plug size={12} aria-hidden />} 测试连接
          </button>
          <button className="btn btn-primary" disabled={saving || blocked} onClick={() => void save()}
                  title={blocked ? `还缺：${missing.join('、') || '标识格式'}` : undefined}>
            {saving ? <Spinner size={11} /> : null} 保存
          </button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <Field label="标识" required={isNew} error={nameError}
                 hint={isNew ? `会成为工具名 db_query__${form.name || 'xxx'}；建好不能改` : '标识进了工具名，不能改'}>
            {(p) => (
              <input {...p} className="field mono" value={form.name} disabled={!isNew} placeholder="sales"
                     autoComplete="off" spellCheck={false}
                     style={nameError ? { borderColor: 'var(--err)' } : undefined}
                     onChange={(e) => set({ name: e.target.value })} />
            )}
          </Field>
          <Field label="类型">
            {(p) => (
              <select {...p} className="field" value={form.kind}
                      onChange={(e) => {
                        const next = kinds.find((x) => x.value === e.target.value)
                        // 端口没填、或者还是上一种的默认端口，就换成这一种的默认端口
                        const keep = form.port != null && form.port !== meta?.default_port
                        set({ kind: e.target.value, port: keep ? form.port : next?.default_port ?? null })
                      }}>
                {kinds.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
              </select>
            )}
          </Field>
        </div>

        {meta?.hint && (
          <div className="flex gap-2 rounded-lg border px-2.5 py-2 text-xs leading-relaxed text-dim"
               style={{ borderColor: 'color-mix(in srgb, var(--accent) 30%, var(--border))', background: 'var(--accent-soft)' }}>
            <Info size={13} className="mt-0.5 shrink-0 text-[var(--accent)]" aria-hidden />
            <span>{meta.hint}</span>
          </div>
        )}

        {sqlite ? (
          <Field label="数据库文件路径" required={required('database')} hint="绝对路径；~ 不会被展开">
            {(p) => (
              <input {...p} className="field mono" value={form.database} placeholder="/绝对/路径/data.db"
                     onChange={(e) => set({ database: e.target.value })} />
            )}
          </Field>
        ) : (
          <>
            <div className="grid grid-cols-[1fr_110px] gap-3">
              <Field label="主机" required={required('host')}>
                {(p) => (
                  <input {...p} className="field mono" value={form.host} placeholder="10.0.0.12 或 db.example.com"
                         onChange={(e) => set({ host: e.target.value })} />
                )}
              </Field>
              <Field label="端口" hint={meta?.default_port ? `留空用 ${meta.default_port}` : undefined}>
                {(p) => (
                  <input {...p} className="field mono" type="number" value={form.port ?? ''}
                         placeholder={String(meta?.default_port ?? '')}
                         onChange={(e) => set({ port: e.target.value ? Number(e.target.value) : null })} />
                )}
              </Field>
            </div>
            <div className="grid grid-cols-2 gap-3">
              {oracle ? (
                <OracleTarget form={form} set={set} />
              ) : (
                <Field label="数据库" required={required('database')}>
                  {(p) => (
                    <input {...p} className="field mono" value={form.database}
                           onChange={(e) => set({ database: e.target.value })} />
                  )}
                </Field>
              )}
              <Field label="schema" hint="只读账号名下常常没有对象，数据在别的 schema 里">
                {(p) => (
                  <input {...p} className="field mono" value={form.schema}
                         placeholder={oracle ? '如 ANALYTICS' : form.kind === 'postgres' ? '留空用 public' : '留空用默认'}
                         onChange={(e) => set({ schema: e.target.value })} />
                )}
              </Field>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <Field label="用户名" required={required('username')}>
                {(p) => (
                  <input {...p} className="field mono" value={form.username} autoComplete="off"
                         onChange={(e) => set({ username: e.target.value })} />
                )}
              </Field>
              <Field label="密码" hint={noPassword ? '没填密码：只有免密登录的库才能这样连' : undefined}>
                {(p) => (
                  <input {...p} className="field mono" type="password" autoComplete="new-password"
                         placeholder={source.has_password ? '已保存，留空则不改' : ''}
                         value={form.password}
                         onChange={(e) => set({ password: e.target.value })} />
                )}
              </Field>
            </div>

            <details className="rounded-lg border" open={advancedCount > 0 || undefined}>
              <summary className="cursor-pointer select-none px-2.5 py-1.5 text-xs text-dim hover:text-fg">
                高级连接参数{advancedCount ? `（${advancedCount}）` : ''}
                <span className="ml-1.5 text-2xs text-faint">对应连接串里的 options，没特殊需要不用填</span>
              </summary>
              <div className="space-y-2.5 border-t px-2.5 py-2.5">
                {advancedDefs.map((a) => (
                  <Field key={a.key} label={<>{a.label} <span className="mono text-faint">{a.key}</span></>} hint={a.help}>
                    {(p) => (
                      <input {...p} className="field mono" value={advValue(a.key)} placeholder={a.placeholder ?? ''}
                             onChange={(e) => setAdv(a.key, e.target.value)} />
                    )}
                  </Field>
                ))}
                {extras.map(([[k, v], i]) => (
                  <div key={i} className="flex items-center gap-2">
                    <input className="field mono w-40" value={k} placeholder="参数名" aria-label="参数名"
                           onChange={(e) => setForm((f) => ({ ...f, advanced: f.advanced.map((kv, j) => (j === i ? [e.target.value, kv[1]] : kv)) }))} />
                    <input className="field mono flex-1" value={v} placeholder="值" aria-label={`参数 ${k || '（未命名）'} 的值`}
                           onChange={(e) => setForm((f) => ({ ...f, advanced: f.advanced.map((kv, j) => (j === i ? [kv[0], e.target.value] : kv)) }))} />
                    <IconButton label={`去掉参数 ${k || '（未命名）'}`} icon={<X size={12} />}
                                onClick={() => setForm((f) => ({ ...f, advanced: f.advanced.filter((_, j) => j !== i) }))} />
                  </div>
                ))}
                <button className="btn btn-sm btn-ghost" onClick={() => setForm((f) => ({ ...f, advanced: [...f.advanced, ['', '']] }))}>
                  <Plus size={11} aria-hidden /> 加一项
                </button>
              </div>
            </details>
          </>
        )}

        <Field label="说明" hint="这句话会给助手看，它据此判断该查哪个库——写清楚里面有什么">
          {(p) => (
            <input {...p} className="field" value={form.description} placeholder="销售库：订单、客户、产品"
                   onChange={(e) => set({ description: e.target.value })} />
          )}
        </Field>

        <label className="flex cursor-pointer items-start gap-2 rounded-lg border p-2.5"
               style={form.readonly ? undefined : { borderColor: 'var(--warn)', background: 'color-mix(in srgb, var(--warn) 7%, transparent)' }}>
          <input type="checkbox" className="mt-0.5" checked={form.readonly}
                 onChange={(e) => set({ readonly: e.target.checked })} />
          <span className="text-xs">
            只读
            <span className="ml-1.5 text-2xs leading-relaxed text-faint">
              强烈建议保持勾选。这里的 SQL 由模型生成，关掉之后 UPDATE / DELETE
              会真的执行（DROP / TRUNCATE 任何情况下都不允许）
            </span>
          </span>
        </label>

        <label className="flex cursor-pointer items-center gap-2 text-xs">
          <input type="checkbox" checked={form.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
          启用<span className="text-2xs text-faint">停用后助手和 agent 都看不到它</span>
        </label>

        {missing.length > 0 && (
          <p className="text-2xs text-faint">还缺：{missing.join('、')}</p>
        )}

        {test.result && !test.result.ok && (
          <div ref={resultRef}>
            <ErrorState compact error={test.result} onRetry={blocked ? undefined : () => void runTest()} />
          </div>
        )}
      </div>
    </Modal>
  )
}

/**
 * Oracle 的连接目标：service_name 和 SID 二选一，读写 options 里对应的那个键。
 *
 * 以前这个框绑在 database 上，而后端把服务名存在 options.service_name——编辑时
 * 框是空的，用户以为配置丢了去补填，新值又被 options 里的旧值悄悄压住。
 */
const ORACLE_MODES = ['service_name', 'sid'] as const

function OracleTarget({ form, set }: { form: SourceForm; set: (patch: Partial<SourceForm>) => void }) {
  const sid = form.oracleMode === 'sid'
  const radio = useRadioGroup(ORACLE_MODES, form.oracleMode, (m) => set({ oracleMode: m }))
  return (
    <div className="min-w-0">
      <div className="mb-1 flex items-center gap-2">
        <label className="label !mb-0" htmlFor="ds-oracle-target">
          {sid ? 'SID' : 'service_name'}<span className="ml-0.5 text-[var(--err)]" aria-hidden>*</span>
        </label>
        <span role="radiogroup" aria-label="Oracle 用 service_name 还是 SID 连接" className="ml-auto inline-flex rounded-md border p-px">
          {ORACLE_MODES.map((m) => (
            <button key={m} type="button" {...radio(m)}
                    className={clsx('rounded px-1.5 text-2xs leading-4', form.oracleMode === m ? 'bg-accent-soft text-fg' : 'text-faint hover:text-dim')}
                    onClick={() => set({ oracleMode: m })}>
              {m === 'sid' ? 'SID' : 'service_name'}
            </button>
          ))}
        </span>
      </div>
      <input
        id="ds-oracle-target"
        className="field mono"
        aria-describedby="ds-oracle-target-hint"
        value={sid ? form.sid : form.serviceName}
        placeholder={sid ? 'ORCL' : 'ORCLPDB1'}
        onChange={(e) => set(sid ? { sid: e.target.value } : { serviceName: e.target.value })}
      />
      <div id="ds-oracle-target-hint" className="mt-1 text-2xs leading-relaxed text-faint">
        {sid ? '老库只给了 SID 时用它；保存时只存 SID' : '一般填这个；保存时只存 service_name'}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// 传表格
// ---------------------------------------------------------------------------

/** 列名看起来像一行数据：纯数字、日期、Unnamed / 空、重复 */
function suspiciousColumns(cols: { name: string }[]): Set<string> {
  const out = new Set<string>()
  const seen = new Map<string, number>()
  for (const c of cols) seen.set(c.name.toLowerCase(), (seen.get(c.name.toLowerCase()) ?? 0) + 1)
  for (const c of cols) {
    const n = c.name.trim()
    if (!n
      || /^-?\d+(\.\d+)?%?$/.test(n)
      || /^\d{4}[-/.年]\d{1,2}([-/.月]\d{1,2}日?)?/.test(n)
      || /^(unnamed|column|col|field)[\s_:]*\d*$/i.test(n)
      || (seen.get(n.toLowerCase()) ?? 0) > 1) out.add(c.name)
  }
  return out
}

/**
 * 传一个 Excel / CSV，变成可以用 SQL 查的表。
 *
 * 为什么不传进知识库：那条路只能把表格切块检索，数字就成了模型从片段里
 * "读"出来的。而这个项目的地基是"所有算术下沉到 SQL 或口径卡"——表格必须
 * 变成表，数字才是算出来的、才追溯得到是哪条查询。
 *
 * 导入完弹窗不关，停在结果上：推断出的列名和类型要当场给人看。表头行取错了
 * （比如文件前两行是标题），列名会变成一行数据——不给看的话，这个错要等到
 * 有人发现汇总一直少一行才暴露。以前导入成功的同一帧弹窗就被卸载了，这段
 * 回显从来没被人看到过。
 */
function TableUploader({ initialName, taken, onClose, onImported }: {
  initialName?: string
  /** 已经被数据库占用的名字（传表格只能就地替换表格，不能顶掉数据库） */
  taken: string[]
  onClose: () => void
  onImported: (row: any) => void
}) {
  const [file, setFile] = useState<File | null>(null)
  const [name, setName] = useState(initialName ?? '')
  const [description, setDescription] = useState('')
  const [headerRow, setHeaderRow] = useState(1)
  const [busy, setBusy] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.datasources.uploadTable>> | null>(null)
  const headerRef = useRef<HTMLInputElement>(null)
  const clock = useRunClock(busy)
  const busySince = useRef(0)

  const pick = (f: File | null) => {
    setFile(f)
    setResult(null)
    // 拿文件名当默认数据源名。它会变成工具名的一部分，所以只留 ASCII；
    // 中文文件名清洗完可能什么都不剩，那就让用户自己填
    if (f && !name) {
      const stem = f.name.replace(/\.[^.]+$/, '')
      const slug = stem.toLowerCase().replace(/[^a-z0-9_]+/g, '_').replace(/^_+|_+$/g, '')
      setName(/^[a-z]/.test(slug) ? slug.slice(0, 40) : '')
    }
  }

  const nameError = name && !NAME_RE.test(name)
    ? '只能用小写字母开头的小写字母、数字、下划线'
    : taken.includes(name) ? `「${name}」已经是一个数据库的标识了，换个名字` : null

  const submit = async () => {
    if (!file || !name || nameError) return
    busySince.current = Date.now()
    setBusy(true)
    try {
      const out = await api.datasources.uploadTable(file, { name, description, header_row: headerRow })
      setResult(out)
      onImported(out.source)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  const retry = () => {
    // 回到表单改行号：文件、名字都留着，同名重传会就地替换刚才那份
    setResult(null)
    setHeaderRow((n) => n + 1)
    requestAnimationFrame(() => { headerRef.current?.focus(); headerRef.current?.select() })
  }

  if (result) {
    const flagged = result.tables.map((t) => suspiciousColumns(t.columns))
    const anyFlagged = flagged.some((s) => s.size > 0)
    return (
      <Modal
        open
        onClose={onClose}
        width={640}
        title={result.replaced ? `已替换「${name}」里的数据` : `已建好数据源「${name}」`}
        footer={
          <>
            <button className="btn" onClick={retry}>表头不对 · 改行号重传</button>
            <button className="btn btn-primary" onClick={onClose} data-autofocus>
              列名对了 · 完成
            </button>
          </>
        }
      >
        <div className="space-y-3" data-upload-result>
          <p className="text-xs leading-relaxed text-dim">
            {result.tables.length} 张表，表头取的是第 {headerRow} 行。对一眼列名和类型：列名要是变成了一行数据，
            说明表头行号不对。
          </p>
          {anyFlagged && (
            <div role="alert" className="flex gap-2 rounded-lg border px-2.5 py-2 text-xs leading-relaxed"
                 style={{ borderColor: 'color-mix(in srgb, var(--warn) 45%, var(--border))', background: 'color-mix(in srgb, var(--warn) 8%, transparent)' }}>
              <AlertTriangle size={13} className="mt-0.5 shrink-0 text-[var(--warn)]" aria-hidden />
              <span>
                标黄的列名看起来像数据（纯数字、日期、空的或重复的）。表头多半不在第 {headerRow} 行，
                试试改成第 {headerRow + 1} 行重传。
              </span>
            </div>
          )}
          {result.tables.map((t, i) => (
            <div key={t.name} className="rounded-lg border bg-bg p-2.5">
              <div className="mb-1.5 flex flex-wrap items-baseline gap-x-2 text-xs">
                <span className="mono font-medium">{t.name}</span>
                {t.sheet !== t.name && <span className="text-faint">工作表「{t.sheet}」</span>}
                <span className="tnum text-faint">{formatNumber(t.rows)} 行 · {t.columns.length} 列</span>
              </div>
              <div className="flex flex-wrap gap-1">
                {t.columns.map((c, j) => {
                  const bad = flagged[i].has(c.name)
                  return (
                    <span key={`${c.name}-${j}`} className="chip" data-suspicious={bad || undefined}
                          style={bad ? { color: 'var(--warn)', borderColor: 'var(--warn)', background: 'color-mix(in srgb, var(--warn) 10%, transparent)' } : undefined}
                          title={bad ? '这个列名看起来像一行数据' : undefined}>
                      <span className="mono">{c.name || '（空）'}</span>
                      <span className="mono text-faint">{c.type}</span>
                    </span>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      </Modal>
    )
  }

  return (
    <Modal
      open
      onClose={onClose}
      dirty={!!file && !busy}
      title="传表格"
      footer={
        <>
          <button className="btn" onClick={onClose} disabled={busy}>取消</button>
          <button className="btn btn-primary tnum" onClick={() => void submit()}
                  disabled={busy || !file || !name || !!nameError}
                  title={!file ? '先选一个文件' : !name ? '给数据源起个名' : undefined}>
            {busy ? <><Spinner size={11} /> 导入中 {formatDuration(clock - busySince.current)}</> : <><Upload size={12} aria-hidden /> 导入</>}
          </button>
        </>
      }
    >
      <div className="space-y-3">
        <label
          className={clsx(
            'flex cursor-pointer flex-col items-center justify-center gap-1.5 rounded-lg border border-dashed px-4 py-5 text-center transition-colors',
            dragging ? 'border-[var(--accent)] bg-accent-soft' : 'hover:border-[var(--border-strong)] hover:bg-hover',
          )}
          onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => { e.preventDefault(); setDragging(false); pick(e.dataTransfer.files?.[0] ?? null) }}
        >
          <FileSpreadsheet size={20} className={file ? 'text-[var(--accent)]' : 'text-faint'} aria-hidden />
          {file ? (
            <span className="text-xs"><span className="mono">{file.name}</span> <span className="tnum text-faint">· {formatBytes(file.size)}</span></span>
          ) : (
            <span className="text-xs text-dim">拖一个文件进来，或点这里选</span>
          )}
          <span className="text-2xs text-faint">Excel（.xlsx）或 CSV / TSV，每个工作表变成一张表。.xls 是老格式，先另存为 .xlsx</span>
          <input type="file" className="sr-only" accept=".xlsx,.xlsm,.csv,.tsv" aria-label="选择表格文件"
                 onChange={(e) => pick(e.target.files?.[0] ?? null)} />
        </label>

        <div className="grid grid-cols-[1fr_120px] gap-3">
          <Field label="数据源名" required error={nameError}
                 hint={`会成为工具名的一部分（db_query__${name || 'sales'}）；同名重传就地替换`}>
            {(p) => (
              <input {...p} className="field mono" value={name} placeholder="sales" autoComplete="off" spellCheck={false}
                     style={nameError ? { borderColor: 'var(--err)' } : undefined}
                     onChange={(e) => setName(e.target.value)} />
            )}
          </Field>
          <Field label="表头在第几行">
            {(p) => (
              <input {...p} ref={headerRef} className="field tnum" type="number" min={1} value={headerRow}
                     onChange={(e) => setHeaderRow(Math.max(1, Number(e.target.value) || 1))} />
            )}
          </Field>
        </div>

        <Field label="说明（给助手看）">
          {(p) => (
            <input {...p} className="field" value={description} placeholder="2026 年各月销售明细"
                   onChange={(e) => setDescription(e.target.value)} />
          )}
        </Field>
      </div>
    </Modal>
  )
}

export function formatBytes(n: number): string {
  if (!Number.isFinite(n)) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}
