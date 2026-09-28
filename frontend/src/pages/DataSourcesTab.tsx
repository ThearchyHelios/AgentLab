import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  AlertTriangle, ChevronRight, Database, EyeOff, FileSpreadsheet, Info, KeyRound, Lock, Plug, Plus, RefreshCw,
  Search, Table2, Upload, X,
} from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../api/client'
import type { IntrospectPreview, TableSchema, UploadProgress } from '../api/client'
import { useCatalog, useOnReconnect } from '../store/catalog'
import {
  confirmDialog, DeleteButton, EmptyState, ErrorState, Field, HealthPill, IconButton, Modal, promptDialog,
  SectionBar, Skeleton, Spinner, toast, useRadioGroup, useTicker,
} from '../components/ui'
import { humanizeError } from '../lib/errors'
import {
  formatBytes, formatDateTime, formatDuration, formatNumber, formatRelative, parseServerTime, shortLabel,
} from '../lib/format'
import { checkHealth, forgetHealth, healthFromServer, setHealth, useHealth } from '../lib/health'
import type { HealthRecord } from '../lib/health'
import { workflowList, workflowsMentioning } from '../lib/mentions'
import { useRunClock } from '../run/useRunClock'

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

/** 数据源 options 里的键：证据面板展示查询原始行时，这些列一律写「已遮罩」 */
const MASK_KEY = 'mask_columns'

/**
 * 增、删、改、传之后让全局的数据源目录跟上：检查器挑工具、问数据的范围、助手的数据源
 * 提示都读它（store/catalog 的 datasources），不重拉的话刚接入的库在那几处看不见
 */
const syncCatalog = () => void useCatalog.getState().reload('datasources')

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
  const drop = (id: string) => {
    setRows((rs) => rs && rs.filter((r) => r.id !== id))
    syncCatalog()
  }

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
              // 卡片上换 schema、探查结构改的也是这个源：检查器、问数据读的目录要跟上
              onChange={(r) => { upsert(r); syncCatalog() }}
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
          onSaved={(row, tested, reconnected) => {
            const isNew = !editing.id
            setEditing(null)
            upsert(row)
            syncCatalog()
            // 本机记着的那次测连接说的是改之前的库：换了地址、账号或连接参数就不作数了
            // （后端同样清掉了自己记的那份，除非表单里刚测过这一份）
            if (tested) setHealth(`datasource:${row.id}`, tested)
            else if (reconnected) forgetHealth(`datasource:${row.id}`)
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
            syncCatalog()
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
  // 后端记着上次测的结果：换了浏览器、清了缓存也还在。本机刚测过的更新就用本机的
  const { record, checkingSince } = useHealth(healthKey, healthFromServer(row))
  const workflows = useCatalog((s) => s.workflows)
  const [busy, setBusy] = useState('')
  const uploaded = isUploadedTable(row)
  // 传上来的表通常只有一两张，直接摊开；库动辄几十上百个对象，默认收着
  const [open, setOpen] = useState(() => uploaded && row.table_count > 0 && row.table_count <= 3)
  const [masking, setMasking] = useState(false)
  const masks = maskList(row.options?.[MASK_KEY])
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
  // 缓存是按哪个 schema 探的（null：老缓存没记，或者还没探过）。和配置对不上时，
  // 助手写 SQL 用的是另一个 schema 的表——改了配置没重探、或者重探失败都会这样
  const cachedSchema: string | null = row.cached_schema ?? null
  const schemaDrift = cachedSchema != null && cachedSchema !== configured

  const introspect = async () => {
    if (busy) return
    busySince.current = Date.now()
    setBusy('introspect')
    try {
      const next = await api.datasources.introspect(row.id)
      onChange(next)
      if (next.table_count) {
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
   * 换个 schema 看看：只看不存（dry_run），缓存和配置都不动。探到对象再问要不要
   * 写进配置；要，就改配置并按新配置真探一次。
   *
   * 以前后端没有「只看」，一探结果就落进缓存，选「不改」还得马上再探一次换回去，
   * 换回去失败时助手看到的就是另一个 schema 的表
   */
  const probeOther = async (schema: string) => {
    if (busy) return
    busySince.current = Date.now()
    setBusy(`schema:${schema}`)
    let preview: IntrospectPreview
    try {
      preview = await api.datasources.previewSchema(row.id, schema)
    } catch (e) {
      toast.error(e)
      return
    } finally {
      setBusy('')
    }
    if (preview.schema_error) {
      toast.warn(`「${schema}」探不出来：${preview.schema_error}`, { detail: '只是看看：配置和缓存都没动' })
      return
    }
    if (!preview.table_count) {
      toast.info(`「${schema}」下没有对象。只是看看，配置和缓存都没动`)
      return
    }
    const total = Math.max(preview.table_count, preview.total ?? 0)
    const sample = preview.tables.slice(0, 6).map((t) => t.slice(t.lastIndexOf('.') + 1))
    const ok = await confirmDialog({
      title: `把 schema 改成「${schema}」？`,
      body: `「${schema}」下有 ${formatNumber(total)} 个对象${sample.length ? `，比如 ${sample.join('、')}${total > sample.length ? ' 等' : ''}` : ''}。`,
      consequences: [
        `改：写进这个数据源的配置并重新探查，以后助手写 SQL 都用 ${schema}`,
        `不改：什么都不动。刚才只是看看，助手看到的还是「${configuredLabel}」的结构`,
      ],
      confirmLabel: `改成 ${schema}`,
      cancelLabel: '不改',
    })
    if (!ok) return
    busySince.current = Date.now()
    setBusy('introspect')
    try {
      // options 整组替换：Oracle 的 service_name / sid 这些要原样带上
      onChange(await api.datasources.update(row.id, { options: { ...(row.options ?? {}), schema } }))
    } catch (e) {
      const h = humanizeError(e)
      toast.error(`没改成（${h.title}）：配置还是「${configuredLabel}」，缓存也没动`, { detail: h.raw })
      setBusy('')
      return
    }
    try {
      const next = await api.datasources.introspect(row.id)
      onChange(next)
      toast.ok(`已把「${row.name}」的 schema 改成 ${schema}，探到 ${formatNumber(next.table_count ?? 0)} 个对象`)
    } catch (e) {
      // 配置已经改了、缓存还是旧的：卡片上那行「结构和配置对不上」会一直提醒
      const h = humanizeError(e)
      toast.error(`schema 已改成 ${schema}，但重新探查没成功（${h.title}）：助手眼下看到的还是原来的结构。再点一次「探查结构」`, { detail: h.raw })
    } finally {
      setBusy('')
    }
  }

  useEffect(() => { if (kick) void introspect() }, [kick])

  // 只看不存，好好的库也可以随手看看别的 schema；单文件的 SQLite 和传上来的表没有 schema 可换
  const synced = parseServerTime(row.schema_synced_at)?.getTime() ?? 0
  const canProbeOther = !uploaded && row.kind !== 'sqlite'
  // 候选里去掉配置里现有的那个：点它等于 dry_run 自己，探到了还问「把 schema 改成它？」
  const otherSchemas: string[] = (row.available_schemas ?? []).filter((s: string) => s !== configured).slice(0, 12)
  const introspectOther = async () => {
    const others = otherSchemas.slice(0, 8)
    const schema = await promptDialog({
      title: '看看哪个 schema？',
      body: `只看不存：缓存和配置都不动，探到对象再问要不要写进配置。${others.length ? `这台服务器上还有：${others.join('、')}` : ''}`,
      label: 'schema',
      placeholder: others[0] ?? (row.kind === 'postgres' ? 'public' : row.kind === 'oracle' ? 'ANALYTICS' : row.database || ''),
      confirmLabel: '看看',
      validate: (v) => (!v ? '填一个 schema 名' : v === configured ? `「${v}」就是配置里的，点「探查结构」就行` : null),
    })
    if (schema) await probeOther(schema)
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
        {masks.length > 0 && (
          <span className="chip" data-mask-columns={masks.join(',')} title={`证据面板里显示成「已遮罩」：${masks.join('、')}`}>
            <EyeOff size={10} aria-hidden /> 遮罩 {formatNumber(masks.length)} 列
          </span>
        )}
        {uploaded && (
          // 传上来的表没有编辑框（只能重传），遮罩列在这里改
          <button type="button" className="text-2xs underline decoration-dotted underline-offset-2 hover:text-dim"
                  onClick={() => setMasking(true)} data-edit-mask="">
            {masks.length ? '改遮罩列' : '设遮罩列'}
          </button>
        )}
        {!!row.tools?.length && <span className="mono">{row.tools.join(' · ')}</span>}
      </div>
      {masking && (
        <MaskColumnsDialog row={row} onClose={() => setMasking(false)}
                           onSaved={(next) => { setMasking(false); onChange(next); syncCatalog() }} />
      )}

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
          {!!otherSchemas.length && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <span className="text-faint">
                当前是「{row.options?.schema || row.database || '默认'}」。这台服务器上还有，点一个看看（只看不存）：
              </span>
              {otherSchemas.map((s: string) => (
                <button key={s} className="chip hover:border-[var(--accent)] hover:text-fg" disabled={!!busy}
                        onClick={() => void probeOther(s)}>
                  {busy === `schema:${s}` ? <Spinner size={10} /> : <Search size={10} aria-hidden />}
                  看看 {s}
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {schemaDrift && (
        <div
          className="mx-3 mb-3 flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg border px-3 py-2 text-xs leading-relaxed"
          style={{
            borderColor: 'color-mix(in srgb, var(--warn) 45%, var(--border))',
            background: 'color-mix(in srgb, var(--warn) 7%, transparent)',
          }}
          data-schema-drift
        >
          <AlertTriangle size={12} className="shrink-0 text-[var(--warn)]" aria-hidden />
          <span className="min-w-0 flex-1">
            <span className="text-[var(--warn)]">
              助手看到的结构是按「{cachedSchema || '默认 schema'}」探的，配置里写的是「{configuredLabel}」
            </span>
            <span className="text-dim">：它写 SQL 用的表可能不在配置的 schema 里。</span>
          </span>
          <button className="btn btn-xs" disabled={!!busy} onClick={() => void introspect()}>
            <RefreshCw size={11} aria-hidden /> 按配置重新探查
          </button>
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
          <button className="tnum inline-flex items-center gap-1 rounded px-1 py-0.5 text-faint hover:bg-hover hover:text-fg disabled:opacity-50"
                  disabled={!!busy} onClick={() => void introspectOther()} data-probe-schema
                  title="指定一个 schema 看看有哪些对象：只看不存，缓存和配置都不动；探到了再决定要不要写进配置">
            {busy.startsWith('schema:')
              ? <><Spinner size={10} /> 在看「{busy.slice(7)}」 {formatDuration(clock - busySince.current)}</>
              : <><Search size={10} aria-hidden /> 换个 schema 看看…</>}
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

/** 一次最多画这么多行：上千个对象的库全画出来会卡，过滤一下就够用了 */
const SCHEMA_ROWS = 300

type ColumnsState = TableSchema | { error: unknown } | 'loading'

/**
 * 结构浏览器：助手写 SQL 靠的就是这份结构，用户得能方便地核对「它看到了
 * 什么」。按 schema 分组，一行一张表，点开懒加载列（名称 / 类型 / 说明）。
 *
 * 列信息取后端给的结构化字段（columns / kind / comment / found）。以前解析给
 * 模型看的那段 detail 文本，列注释里带两个空格或换行就会错位
 */
function SchemaBrowser({ row }: { row: any }) {
  const [tables, setTables] = useState<string[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [q, setQ] = useState('')
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const [details, setDetails] = useState<Record<string, ColumnsState>>({})

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
      api.datasources.tableSchema(row.id, t).then(
        (s) => setDetails((d) => ({ ...d, [t]: s })),
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
              const loaded = d && typeof d === 'object' && 'table' in d ? d : null
              return (
                <div key={t}>
                  <button
                    className="flex w-full items-center gap-1.5 rounded px-1 py-[3px] text-left hover:bg-hover"
                    aria-expanded={isOpen}
                    onClick={() => toggle(t)}
                  >
                    <ChevronRight size={11} className={clsx('shrink-0 text-faint transition-transform', isOpen && 'rotate-90')} aria-hidden />
                    <span className="mono truncate text-xs">{group ? t.slice(group.length + 1) : t}</span>
                    {loaded?.kind === 'view' && <span className="text-2xs text-faint">视图</span>}
                    {!!loaded?.columns?.length && (
                      <span className="tnum text-2xs text-faint">{loaded.columns.length} 列</span>
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

function ColumnList({ detail }: { detail?: ColumnsState }) {
  if (!detail || detail === 'loading') {
    return <div className="mb-1 ml-5 py-1"><Skeleton rows={3} height={9} gap={6} /></div>
  }
  if ('error' in detail) return <div className="mb-1.5 ml-5"><ErrorState compact error={detail.error} /></div>
  if (detail.found === false) {
    // 清单里有、缓存里却找不到：清单和列是两次请求取的，中间重新探查过。
    // 只认明确的 false：老后端的响应里根本没有 found，得往下走到原文显示
    return (
      <div className="mb-1.5 ml-5 text-2xs text-faint">
        缓存里没有这张表的结构了，多半是刚重新探查过。收起再展开，或者点「探查结构」
      </div>
    )
  }
  // 老后端只给 detail 文本：原样显示，不去解析
  if (!detail.columns) {
    return <div className="mono mb-1.5 ml-5 whitespace-pre-wrap text-2xs text-dim">{detail.detail || '没有列信息'}</div>
  }
  const view = detail.kind === 'view'
  return (
    <div className="mb-1.5 ml-5 mt-0.5 overflow-hidden rounded-md border bg-bg">
      {(detail.comment || view) && (
        <div className="flex gap-2 border-b px-2 py-1 text-2xs text-dim">
          {view && <span className="chip">视图</span>}
          {detail.comment && <span className="whitespace-pre-line">{detail.comment}</span>}
        </div>
      )}
      {!detail.columns.length ? (
        <div className="px-2 py-1.5 text-2xs text-faint">探查时没读到列信息</div>
      ) : (
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
                  {c.not_null && <span className="mr-1.5 text-faint">非空</span>}
                  {c.comment && <span className="whitespace-pre-line">{c.comment}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
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
  /** options.mask_columns：证据面板展示查询原始行时遮掉的列 */
  maskColumns: string[]
  /** options 里除 schema / service_name / sid / mask_columns 之外的键，全部摊开可编辑 */
  advanced: [string, string][]
}

const OWN_OPTION_KEYS = new Set(['schema', 'service_name', 'sid', MASK_KEY])

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
    maskColumns: maskList(o[MASK_KEY]),
    advanced: Object.entries(o)
      .filter(([k]) => !OWN_OPTION_KEYS.has(k))
      .map(([k, v]) => [k, String(v ?? '')]),
  }
}

function toBody(form: SourceForm, isNew: boolean, wasOracle = true): any {
  const options: Record<string, string | string[]> = {}
  for (const [k, v] of form.advanced) if (k.trim() && v.trim()) options[k.trim()] = v.trim()
  if (form.schema.trim()) options.schema = form.schema.trim()
  if (form.maskColumns.length) options[MASK_KEY] = form.maskColumns
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

/**
 * 422 的中文 detail 说的是哪一项。眼下只有查询时限按字段拒（后端 query_timeout_problem），
 * 认不出的交回 toast
 */
function fieldOfRejection(message: string): string | null {
  if (/查询时限/.test(message)) return 'query_timeout_s'
  return null
}

const FIELD_LABEL: Record<string, string> = {
  host: '主机', database: '数据库', username: '用户名',
}

/**
 * 决定连到哪个库的那几项（提交体里的）。schema 不在内：它只影响探查哪一片，不影响
 * 连不连得上，后端判断测连接结果还作不作数时同样不看它；遮罩列只管面板上怎么显示，也不在内
 */
function connectionOf(body: any): string {
  const { schema: _schema, [MASK_KEY]: _mask, ...options } = body.options ?? {}
  return JSON.stringify([
    body.kind, body.host ?? null, body.port ?? null, body.database ?? null, body.username ?? null,
    options, !!body.password,
  ])
}

function SourceEditor({ source, kinds, onClose, onSaved }: {
  source: any; kinds: any[]; onClose: () => void
  /** reconnected：连接配置改过了，之前测的结果不再代表它 */
  onSaved: (row: any, tested?: HealthRecord, reconnected?: boolean) => void
}) {
  const isNew = !source.id
  const [initial] = useState(() => toForm(source))
  const [form, setForm] = useState<SourceForm>(initial)
  const [saving, setSaving] = useState(false)
  const [test, setTest] = useState<{ since?: number; result?: HealthRecord; sig?: string }>({})
  // 后端按字段拒掉的保存（422，detail 是一句中文）：写在那一项下面，不只弹 toast——
  // toast 盖在弹窗上面，几秒就没了，人还得猜是哪一项
  const [fieldError, setFieldError] = useState<{ key: string; message: string } | null>(null)
  // 高级连接参数开没开，由人（和报错）决定，不跟着「填了几项」走：删空唯一一项的那一下
  // 要是跟着收起，正在改的框被藏起来，焦点掉到 body，接着敲的字全落空
  const [advOpen, setAdvOpen] = useState(() => initial.advanced.some(([, v]) => v))
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
    setFieldError(null)
    try {
      const row = isNew
        ? await api.datasources.create(body)
        : await api.datasources.update(source.id, body)
      const before = connectionOf(toBody(initial, isNew, source.kind === 'oracle'))
      onSaved(row, testFresh ? test.result : undefined, connectionOf(body) !== before)
    } catch (e) {
      const rejected = e instanceof ApiError && e.status === 422 ? e.message : ''
      const key = rejected ? fieldOfRejection(rejected) : null
      if (key) {
        setFieldError({ key, message: rejected })
        setAdvOpen(true)
        requestAnimationFrame(() => document.getElementById(`ds-adv-${key}`)?.focus())
      } else {
        toast.error(e)
      }
    } finally {
      setSaving(false)
    }
  }

  const advancedDefs: any[] = (meta?.advanced ?? []).filter((a: any) => !OWN_OPTION_KEYS.has(a.key))
  const knownAdvanced = new Set(advancedDefs.map((a) => a.key))
  const advValue = (k: string) => form.advanced.find(([key]) => key === k)?.[1] ?? ''
  const setAdv = (k: string, v: string) => {
    if (fieldError?.key === k) setFieldError(null)
    setForm((f) => {
      const rest = f.advanced.filter(([key]) => key !== k)
      return { ...f, advanced: v ? [...rest, [k, v]] : rest }
    })
  }
  const extras = form.advanced.map((kv, i) => [kv, i] as const).filter(([[k]]) => !knownAdvanced.has(k))
  const advancedCount = form.advanced.filter(([, v]) => v).length

  // 高级连接参数：SQLite 也有（查询时限），以前只在网络库的分支里画，SQLite 源配不了
  const advanced = (
    <details className="rounded-lg border" open={advOpen} onToggle={(e) => setAdvOpen(e.currentTarget.open)}>
      <summary className="cursor-pointer select-none px-2.5 py-1.5 text-xs text-dim hover:text-fg">
        高级连接参数{advancedCount ? `（${advancedCount}）` : ''}
        <span className="ml-1.5 text-2xs text-faint">
          {sqlite ? '查询时限这类，没特殊需要不用填' : '对应连接串里的 options，没特殊需要不用填'}
        </span>
      </summary>
      <div className="space-y-2.5 border-t px-2.5 py-2.5">
        {advancedDefs.map((a) => {
          const err = fieldError && fieldError.key === a.key ? fieldError.message : undefined
          return (
            <Field key={a.key} htmlFor={`ds-adv-${a.key}`} label={<>{a.label} <span className="mono text-faint">{a.key}</span></>}
                   hint={a.help} error={err}>
              <input id={`ds-adv-${a.key}`} className="field mono" value={advValue(a.key)} placeholder={a.placeholder ?? ''}
                     aria-invalid={err ? true : undefined}
                     aria-describedby={err ? `ds-adv-${a.key}-error` : a.help ? `ds-adv-${a.key}-hint` : undefined}
                     style={err ? { borderColor: 'var(--err)' } : undefined}
                     onChange={(e) => setAdv(a.key, e.target.value)} />
            </Field>
          )
        })}
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
        {!sqlite && (
          <button className="btn btn-sm btn-ghost" onClick={() => setForm((f) => ({ ...f, advanced: [...f.advanced, ['', '']] }))}>
            <Plus size={11} aria-hidden /> 加一项
          </button>
        )}
      </div>
    </details>
  )

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
          <>
            <Field label="数据库文件路径" required={required('database')} hint="绝对路径；~ 不会被展开">
              {(p) => (
                <input {...p} className="field mono" value={form.database} placeholder="/绝对/路径/data.db"
                       onChange={(e) => set({ database: e.target.value })} />
              )}
            </Field>
            {advancedDefs.length > 0 && advanced}
          </>
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

            {advanced}
          </>
        )}

        <Field label="说明" hint="这句话会给助手看，它据此判断该查哪个库——写清楚里面有什么">
          {(p) => (
            <input {...p} className="field" value={form.description} placeholder="销售库：订单、客户、产品"
                   onChange={(e) => set({ description: e.target.value })} />
          )}
        </Field>

        <MaskColumnsField sourceId={source.id} synced={!!source.table_count} value={form.maskColumns}
                          onChange={(maskColumns) => set({ maskColumns })} />

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
// 遮罩列（options.mask_columns）
// ---------------------------------------------------------------------------

/** options.mask_columns → 列名列表。列表和「a, b」这样的文字都认（后端 masked_columns 同一个认法），按大小写不敏感去重 */
function maskList(raw: unknown): string[] {
  const items = typeof raw === 'string' ? raw.split(/[,，、\s]+/) : Array.isArray(raw) ? raw : []
  const seen = new Set<string>()
  const out: string[] = []
  for (const item of items) {
    const name = typeof item === 'string' || typeof item === 'number' ? String(item).trim() : ''
    if (!name || seen.has(name.toLowerCase())) continue
    seen.add(name.toLowerCase())
    out.push(name)
  }
  return out
}

/** 挑列时最多读几张表的列：库动辄上百张表，一张一个请求，挑列用不着全读 */
const MASK_SCAN_TABLES = 40

/**
 * 从已探查的结构里收列名（列名 → 出现在哪几张表）。第一次聚焦输入框才取，同一个数据源取一次
 */
function useSchemaColumns(sourceId: string | undefined, active: boolean) {
  const [state, setState] = useState<{ status: 'idle' | 'loading' | 'ok' | 'error'; columns: Map<string, string[]>; tables: number; scanned: number }>(
    { status: 'idle', columns: new Map(), tables: 0, scanned: 0 })
  // 取过就不再取（状态不进依赖：置成 loading 那一下会让 effect 重跑，把正在取的那次当成过期的丢掉）
  const started = useRef<string | null>(null)
  useEffect(() => {
    if (!active || !sourceId || started.current === sourceId) return
    started.current = sourceId
    let alive = true
    setState((s) => ({ ...s, status: 'loading' }))
    void (async () => {
      try {
        const list: string[] = (await api.datasources.schema(sourceId))?.tables ?? []
        const pick = list.slice(0, MASK_SCAN_TABLES)
        const columns = new Map<string, string[]>()
        for (let i = 0; i < pick.length; i += 6) {
          const batch = await Promise.all(pick.slice(i, i + 6).map((t) => api.datasources.tableSchema(sourceId, t).catch(() => null)))
          batch.forEach((detail, j) => {
            for (const c of detail?.columns ?? []) {
              const name = String(c?.name ?? '').trim()
              if (!name) continue
              columns.set(name, [...(columns.get(name) ?? []), pick[i + j]])
            }
          })
        }
        if (alive) setState({ status: 'ok', columns, tables: list.length, scanned: pick.length })
      } catch {
        if (alive) setState((s) => ({ ...s, status: 'error' }))
      }
    })()
    return () => { alive = false; started.current = null }
  }, [active, sourceId])
  return state
}

/**
 * 遮罩的列：列名一个个加成标签，可以从已探查的结构里挑，也可以直接输。
 * 用户拍板写明：在有身份体系之前，遮罩只减少暴露，不是安全边界
 */
function MaskColumnsInput({ id, sourceId, synced, value, onChange }: {
  id: string; sourceId?: string; synced: boolean; value: string[]; onChange: (v: string[]) => void
}) {
  const [draft, setDraft] = useState('')
  const [touched, setTouched] = useState(false)
  const known = useSchemaColumns(sourceId, touched && synced)
  const add = (text: string) => {
    const next = maskList([...value, ...maskList(text)])
    if (next.length !== value.length) onChange(next)
    setDraft('')
  }
  const has = new Set(value.map((v) => v.toLowerCase()))
  const options = [...known.columns.entries()].filter(([c]) => !has.has(c.toLowerCase()))
    .sort((a, b) => a[0].localeCompare(b[0]))
  const state = !sourceId ? '保存并探查结构之后可以从列里挑；现在可以直接输列名'
    : !synced ? '还没探查过结构：直接输列名'
    : known.status === 'loading' ? '正在读已探查的结构…'
    : known.status === 'error' ? '已探查的结构没取到：直接输列名'
    : known.status === 'ok'
      ? `从已探查的 ${formatNumber(known.scanned)} 张表里挑${known.tables > known.scanned ? `（共 ${formatNumber(known.tables)} 张，只读了前 ${formatNumber(known.scanned)} 张，别的直接输列名）` : ''}，或者直接输列名`
      : '点输入框可以从已探查的列里挑'
  return (
    <div data-mask-input="">
      {value.length > 0 && (
        <div className="mb-1.5 flex flex-wrap gap-1" aria-label="已遮罩的列">
          {value.map((c) => (
            <span key={c} className="chip mono" style={{ color: 'var(--text)', borderColor: 'var(--border-strong)' }} data-mask-chip={c}>
              <EyeOff size={10} aria-hidden /> {c}
              <button type="button" className="ml-0.5 hover:opacity-60" aria-label={`不再遮罩 ${c}`}
                      onClick={() => onChange(value.filter((v) => v !== c))}>
                <X size={9} aria-hidden />
              </button>
            </span>
          ))}
        </div>
      )}
      <div className="flex gap-1.5">
        <input id={id} className="field mono min-w-0 flex-1" list={`${id}-list`} value={draft} autoComplete="off" spellCheck={false}
               placeholder="phone, email" aria-describedby={`${id}-hint`}
               onFocus={() => setTouched(true)}
               onChange={(e) => {
                 const v = e.target.value
                 // 从候选里点中一项（浏览器报 insertReplacementText，不是逐字敲的）、或者敲了逗号：直接加成标签。
                 // 逐字敲到恰好等于某个列名（敲 id_card 途中的 id）不算点中
                 const kind = (e.nativeEvent as InputEvent).inputType
                 const picked = (!kind || kind === 'insertReplacementText') && known.columns.has(v)
                 if (/[,，、]/.test(v) || picked) add(v)
                 else setDraft(v)
               }}
               onKeyDown={(e) => {
                 if (e.key === 'Enter' && !e.nativeEvent.isComposing && draft.trim()) { e.preventDefault(); add(draft) }
                 if (e.key === 'Backspace' && !draft && value.length) onChange(value.slice(0, -1))
               }} />
        <button type="button" className="btn btn-sm" disabled={!draft.trim()} onClick={() => add(draft)}>加上</button>
      </div>
      <datalist id={`${id}-list`}>
        {options.slice(0, 300).map(([c, tables]) => (
          <option key={c} value={c}>{tables.slice(0, 3).join('、')}{tables.length > 3 ? ` 等 ${tables.length} 张表` : ''}</option>
        ))}
      </datalist>
      <div id={`${id}-hint`} className="mt-1 space-y-0.5 text-2xs leading-relaxed text-faint">
        <div>证据面板展示查询结果的原始行时，这些列（不分大小写）一律显示成「已遮罩」。{state}</div>
        <div style={{ color: 'var(--st-waiting)' }} data-mask-boundary="">
          在有身份体系之前，遮罩只减少暴露，不是安全边界：完整快照仍能按工件取到，SQL 里给列起别名也能绕开
        </div>
      </div>
    </div>
  )
}

function MaskColumnsField(props: { sourceId?: string; synced: boolean; value: string[]; onChange: (v: string[]) => void }) {
  return (
    <div data-field="mask_columns">
      <label className="label" htmlFor="ds-mask-columns">遮罩的列</label>
      <MaskColumnsInput id="ds-mask-columns" {...props} />
    </div>
  )
}

/** 传上来的表没有编辑框：卡片上单独改遮罩列，options 其余的键原样带上 */
function MaskColumnsDialog({ row, onClose, onSaved }: { row: any; onClose: () => void; onSaved: (row: any) => void }) {
  const [initial] = useState(() => maskList(row.options?.[MASK_KEY]))
  const [value, setValue] = useState(initial)
  const [saving, setSaving] = useState(false)
  const dirty = value.join('\u0000') !== initial.join('\u0000')
  const save = async () => {
    setSaving(true)
    try {
      const { [MASK_KEY]: _old, ...rest } = row.options ?? {}
      onSaved(await api.datasources.update(row.id, { options: value.length ? { ...rest, [MASK_KEY]: value } : rest }))
      toast.ok(value.length ? `「${row.name}」遮罩 ${value.length} 列` : `「${row.name}」不再遮罩任何列`)
    } catch (e) {
      toast.error(e)
    } finally {
      setSaving(false)
    }
  }
  return (
    <Modal open onClose={onClose} dirty={dirty} width={520} title={`「${row.name}」的遮罩列`}
           footer={<>
             <button className="btn" onClick={onClose}>取消</button>
             <button className="btn btn-primary" disabled={saving || !dirty} onClick={() => void save()}>
               {saving ? <Spinner size={11} /> : null} 保存
             </button>
           </>}>
      <MaskColumnsField sourceId={row.id} synced={!!row.table_count} value={value} onChange={setValue} />
    </Modal>
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

const isAbort = (e: unknown) => e instanceof DOMException && e.name === 'AbortError'

/**
 * 上传的真实进度：字节发了多少（XHR 的上传进度），发完之后是后端在处理，那段
 * 没有进度可报——只写「处理中」和已等了多久，不把条拉满冒充完成。浏览器算不出
 * 总字节时只写已发多少，不画百分比。
 *
 * 知识库的上传占位行也用它
 */
export function UploadMeter({ progress, startedAt, sentAt, processing, now }: {
  progress: UploadProgress | null
  /** 开始发的时刻；还没轮到它时不传 */
  startedAt?: number
  /** 字节发完的时刻 */
  sentAt?: number
  /** 发完之后后端在干什么，比如「正在读表、推断列类型」 */
  processing: string
  now: number
}) {
  if (!startedAt) return <span className="text-faint">排队中，前面的传完就轮到它</span>
  if (progress?.sent) {
    return (
      <span className="inline-flex items-center gap-1.5 text-dim" data-upload-phase="processing">
        <Spinner size={10} /> 传完了，{processing} · <span className="tnum">{formatDuration(now - (sentAt ?? now))}</span>
      </span>
    )
  }
  // 浏览器还没报过进度（刚开始，或者中间有层拦着不报）：不写「已传 0 B」，只写在传
  const loaded = progress?.loaded ?? null
  const total = progress?.total ?? null
  const pct = total && loaded != null ? Math.min(1, loaded / total) : null
  return (
    <span className="block" data-upload-phase="sending">
      <span className="tnum flex flex-wrap gap-x-1.5 text-dim">
        <span>上传中{loaded != null && <> · 已传 {formatBytes(loaded)}{total ? ` / ${formatBytes(total)}` : ''}</>}</span>
        {pct != null && <span>· {Math.round(pct * 100)}%</span>}
        <span className="text-faint">· {formatDuration(now - startedAt)}</span>
      </span>
      {pct != null && (
        <span className="mt-1 block h-1 overflow-hidden rounded-full bg-hover" role="progressbar"
              aria-label="上传进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct * 100)}>
          {/* 用 scaleX 推进而不是改 width：只动 transform，不触发重排 */}
          <span className="block h-full origin-left rounded-full bg-[var(--st-running)] transition-transform duration-200"
                style={{ transform: `scaleX(${pct})` }} />
        </span>
      )}
    </span>
  )
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
  const [progress, setProgress] = useState<{ p: UploadProgress; sentAt?: number } | null>(null)
  const [dragging, setDragging] = useState(false)
  const [result, setResult] = useState<Awaited<ReturnType<typeof api.datasources.uploadTable>> | null>(null)
  const headerRef = useRef<HTMLInputElement>(null)
  const clock = useRunClock(busy)
  const busySince = useRef(0)
  const upload = useRef<{ abort: () => void; sent: boolean } | null>(null)
  const sent = !!progress?.p.sent

  // 弹窗关掉时字节还没发完，就不传了：后端收不全，什么都不会建。已经发完的让它
  // 做完——后端已经在建表，这时断开只会让人以为没传上
  useEffect(() => () => { if (upload.current && !upload.current.sent) upload.current.abort() }, [])

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
    const ctl = new AbortController()
    const handle = { abort: () => ctl.abort(), sent: false }
    upload.current = handle
    busySince.current = Date.now()
    setProgress(null)
    setBusy(true)
    try {
      const out = await api.datasources.uploadTable(file, { name, description, header_row: headerRow }, {
        signal: ctl.signal,
        onProgress: (p) => {
          if (p.sent) handle.sent = true
          setProgress((cur) => ({ p, sentAt: cur?.sentAt ?? (p.sent ? Date.now() : undefined) }))
        },
      })
      setResult(out)
      onImported(out.source)
    } catch (e) {
      if (isAbort(e)) toast.info(`没传完就取消了，「${name}」没有建也没有改`)
      else toast.error(e)
    } finally {
      if (upload.current === handle) upload.current = null
      setBusy(false)
      setProgress(null)
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
          {busy && !sent ? (
            <button className="btn" onClick={() => upload.current?.abort()}>取消上传</button>
          ) : (
            <button className="btn" onClick={onClose} disabled={busy}
                    title={busy ? '文件已经传完，后端正在建表，等它做完' : undefined}>取消</button>
          )}
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
            'flex flex-col items-center justify-center gap-1.5 rounded-lg border border-dashed px-4 py-5 text-center transition-colors',
            busy ? 'opacity-60' : 'cursor-pointer',
            dragging ? 'border-[var(--accent)] bg-accent-soft' : !busy && 'hover:border-[var(--border-strong)] hover:bg-hover',
          )}
          onDragOver={(e) => { e.preventDefault(); if (!busy) setDragging(true) }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => { e.preventDefault(); setDragging(false); if (!busy) pick(e.dataTransfer.files?.[0] ?? null) }}
        >
          <FileSpreadsheet size={20} className={file ? 'text-[var(--accent)]' : 'text-faint'} aria-hidden />
          {file ? (
            <span className="text-xs"><span className="mono">{file.name}</span> <span className="tnum text-faint">· {formatBytes(file.size)}</span></span>
          ) : (
            <span className="text-xs text-dim">拖一个文件进来，或点这里选</span>
          )}
          <span className="text-2xs text-faint">Excel（.xlsx）或 CSV / TSV，每个工作表变成一张表。.xls 是老格式，先另存为 .xlsx</span>
          <input type="file" className="sr-only" accept=".xlsx,.xlsm,.csv,.tsv" aria-label="选择表格文件" disabled={busy}
                 onChange={(e) => pick(e.target.files?.[0] ?? null)} />
        </label>

        {busy && (
          <div className="rounded-lg border bg-bg px-3 py-2 text-2xs" data-upload-progress>
            <UploadMeter progress={progress?.p ?? null} startedAt={busySince.current} sentAt={progress?.sentAt}
                         processing="正在读表、推断列类型" now={clock} />
          </div>
        )}

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
