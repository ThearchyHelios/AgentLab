import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { BookMarked, Check, ExternalLink, RotateCw } from 'lucide-react'
import clsx from 'clsx'
import { ApiError, api } from '../api/client'
import { Notice, Spinner, toast } from '../components/ui'
import { CatalogImpactList } from '../components/CatalogImpact'
import { CATALOG_PATCH_TEXT as T, CATALOG_STATUS_LABEL } from '../lib/terms'
import type { CatalogPatchChange } from '../types'
import {
  catalogTableHref, codeMeaning, patchSubmit, patchTarget, patchValueText, patchWhere, type CatalogPatch,
} from './catalogPatch'

// ===========================================================================
// 「建议更新数据目录」卡片：助手把用户说的一条数据事实整理成目录修改提案（catalog_patch），这里逐项列出改前、
// 改后和理由，人点「保存到数据目录」才写入——服务端从不自动写。
//
// 保存带着提案对照的版本：别人在这之后改过这张表就 409，卡片写明「这张表刚被修改过」，「重新载入」按最新的
// 目录重算改前和改后（预览接口，只读），人看过再保存。保存成功后列出受影响的模板（影响面），给到数据目录那张表的入口。
//
// 卡片的结局（已保存、已忽略、重新载入后的内容）记在模块级的表里：画布右栏在对话层和运行层之间切换会卸载
// 卡片，回来时不能又变回「未保存」，再点一次只会撞上 409。
// ===========================================================================

type Phase = 'open' | 'saving' | 'conflict' | 'reloading' | 'saved' | 'ignored'

interface CardState {
  phase: Phase
  changes: CatalogPatchChange[]
  version: number
  /** 重新载入后已无法按建议保存的项（列已删除……） */
  problems: string[]
  /** 保存被拒（422）时服务端的原话 */
  error: string | null
  savedVersion?: number
}

const remembered = new Map<string, CardState>()

export function CatalogPatchCard({ patch, turnId, dense = false }: {
  patch: CatalogPatch
  /** 所在轮次：和 patch.key 一起认出「同一张卡片」 */
  turnId: string
  dense?: boolean
}) {
  const memo = `${turnId}:${patch.key}`
  const [state, setStateRaw] = useState<CardState>(() => remembered.get(memo)
    ?? { phase: 'open', changes: patch.changes, version: patch.version, problems: [], error: null })
  const setState = useCallback((next: Partial<CardState>) => {
    setStateRaw((cur) => {
      const merged = { ...cur, ...next }
      remembered.set(memo, merged)
      return merged
    })
  }, [memo])

  const label = patch.tableLabel ?? patch.table
  const pending = state.changes.filter((c) => c.state !== 'same')
  const href = catalogTableHref(patch.sourceId, patch.table)

  const save = async () => {
    if (!pending.length) return
    setState({ phase: 'saving', error: null })
    try {
      const d = await api.dataCatalog.patch(patch.sourceId, patch.table, patchSubmit(state.changes), state.version)
      setState({ phase: 'saved', savedVersion: d.version })
      toast.ok(T.savedToast(label))
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setState({ phase: 'conflict' })
      else if (e instanceof ApiError && e.kind === 'http' && (e.status === 422 || e.status === 404)) {
        setState({ phase: 'open', error: e.message })
      } else {
        setState({ phase: 'open' })
        toast.error(e)
      }
    }
  }

  const reload = async () => {
    setState({ phase: 'reloading', error: null })
    try {
      // 交回的是助手原来的提案：码值的补充要落在最新的目录上重算，不能沿用上一次算好的「改后」
      const p = await api.dataCatalog.patchPreview(patch.sourceId, patch.table, patchSubmit(patch.changes))
      setState({ phase: 'open', changes: p.changes, version: p.version, problems: p.problems })
    } catch (e) {
      setState({ phase: 'conflict' })
      toast.error(e)
    }
  }

  if (state.phase === 'ignored') {
    return (
      <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg border border-dashed px-2.5 py-1.5 text-2xs text-faint"
           data-catalog-patch={patch.table} data-patch-phase="ignored">
        <BookMarked size={11} aria-hidden />
        <span className="min-w-0 flex-1">{T.ignored}</span>
        <button type="button" className="rounded px-1 text-dim hover:bg-hover hover:text-fg" onClick={() => setState({ phase: 'open' })}
                data-patch-unignore="">
          {T.undoIgnore}
        </button>
      </div>
    )
  }

  const busy = state.phase === 'saving' || state.phase === 'reloading'
  const saved = state.phase === 'saved'
  const settled = saved || (!pending.length && state.phase === 'open')

  return (
    <section className="mt-2 rounded-lg border bg-bg" aria-label={T.title}
             data-catalog-patch={patch.table} data-patch-phase={state.phase} data-patch-version={state.version}>
      <header className={clsx('flex flex-wrap items-baseline gap-x-2 gap-y-0.5 border-b', dense ? 'px-2.5 py-1.5' : 'px-3 py-2')}>
        <span className="inline-flex items-center gap-1.5 text-xs font-semibold">
          <BookMarked size={12} className="shrink-0" style={{ color: 'var(--accent)' }} aria-hidden />
          {T.title}
        </span>
        <span className="min-w-0 text-2xs text-dim">
          「{label}」{patch.tableLabel && <span className="mono text-faint"> {patch.table}</span>}
          <span className="text-faint"> · {T.sourceOf(patch.source)}</span>
        </span>
      </header>

      <ol className={clsx('divide-y divide-[var(--hairline)]', dense ? 'px-2.5' : 'px-3')}>
        {state.changes.map((c) => <ChangeRow key={c.path} change={c} dense={dense} />)}
      </ol>

      <div className={clsx('space-y-2 border-t', dense ? 'px-2.5 py-2' : 'px-3 py-2.5')}>
        {state.problems.length > 0 && (
          <Notice tone="warn" attr={{ 'data-patch-problems': String(state.problems.length) }}>
            <p>{T.problems}</p>
            <ul className="mt-0.5 list-disc pl-4 text-dim">{state.problems.map((p) => <li key={p}>{p}</li>)}</ul>
          </Notice>
        )}
        {state.error && <Notice tone="err" attr={{ 'data-patch-error': '' }}>{state.error}</Notice>}
        {state.phase === 'conflict' || state.phase === 'reloading'
          ? (
            <Notice tone="warn" attr={{ 'data-patch-conflict': '' }}>
              <p className="font-medium">{T.conflictTitle}</p>
              <p className="mt-0.5 text-dim">{T.conflictBody}</p>
              <button type="button" className="btn btn-sm mt-2" disabled={busy} onClick={() => void reload()} data-patch-reload="">
                {state.phase === 'reloading' ? <><Spinner size={11} /> {T.reloading}</> : <><RotateCw size={11} aria-hidden /> {T.reload}</>}
              </button>
            </Notice>
          )
          : settled
            ? (
              <div className="space-y-2">
                <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs" data-patch-done="">
                  <Check size={12} style={{ color: 'var(--ok)' }} aria-hidden />
                  <span>{saved && state.savedVersion != null ? T.saved(state.savedVersion) : T.allSame}</span>
                  <Link to={href} className="inline-flex items-center gap-1 text-2xs text-[var(--accent)] hover:underline" data-patch-open="">
                    {T.open} <ExternalLink size={10} aria-hidden />
                  </Link>
                </p>
                {saved && <CatalogImpactList sourceId={patch.sourceId} table={patch.table} compact />}
              </div>
            )
            : (
              <div className="flex flex-wrap items-center gap-2">
                <button type="button" className="btn btn-sm btn-primary" disabled={busy} onClick={() => void save()} data-patch-save="">
                  {state.phase === 'saving' ? <><Spinner size={11} /> {T.saving}</> : T.save}
                </button>
                <button type="button" className="btn btn-sm" disabled={busy} onClick={() => setState({ phase: 'ignored' })} data-patch-ignore="">
                  {T.ignore}
                </button>
                <span className="min-w-0 flex-1 text-2xs text-faint">{T.hint}</span>
              </div>
            )}
      </div>
    </section>
  )
}

/** 一项：改到哪儿、改前、改后、理由 */
function ChangeRow({ change: c, dense }: { change: CatalogPatchChange; dense: boolean }) {
  const isCodes = patchTarget(c.path)?.kind === 'column' && c.path.endsWith('.codes')
  return (
    <li className={clsx('text-xs', dense ? 'py-1.5' : 'py-2')} data-patch-change={c.path} data-patch-state={c.state}>
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="font-medium">{patchWhere(c)}</span>
        {c.state === 'confirm' && <span className="text-2xs text-faint">{T.stateConfirm}</span>}
        {c.state === 'same' && <span className="text-2xs text-faint">{T.stateSame}</span>}
      </div>
      <dl className="mt-1 grid grid-cols-[auto_minmax(0,1fr)] gap-x-2 gap-y-0.5 text-2xs leading-relaxed">
        <dt className="text-faint">{T.before}</dt>
        <dd className={clsx('break-words', c.before == null ? 'text-faint' : 'text-dim')} data-patch-before="">
          {patchValueText(c.path, c.before)}
          {c.before != null && c.before_status && <span className="text-faint">（{CATALOG_STATUS_LABEL[c.before_status]}）</span>}
        </dd>
        <dt className="text-faint">{T.after}</dt>
        <dd className="break-words text-fg" data-patch-after="">
          {isCodes ? <CodesAfter before={c.before} after={c.after} /> : patchValueText(c.path, c.after)}
        </dd>
        {c.reason && (
          <>
            <dt className="text-faint">{T.reason}</dt>
            <dd className="break-words text-dim" data-patch-reason="">{c.reason}</dd>
          </>
        )}
      </dl>
    </li>
  )
}

/** 码值的改后：新加和改过的码加重，原有的照常写——一眼看出这条提案补了什么 */
function CodesAfter({ before, after }: { before: unknown; after: unknown }) {
  const prev = (before && typeof before === 'object' ? before : {}) as Record<string, string>
  const entries = Object.entries((after && typeof after === 'object' ? after : {}) as Record<string, string>)
  return (
    <span>
      {entries.map(([k, v], i) => (
        <span key={k}>
          {i > 0 && '、'}
          <span className={clsx(prev[k] !== v && 'font-medium text-[var(--accent)]', !String(v ?? '').trim() && 'text-faint')}
                data-code-changed={prev[k] !== v ? k : undefined}>
            {k}={codeMeaning(v)}
          </span>
        </span>
      ))}
    </span>
  )
}
