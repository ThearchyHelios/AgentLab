import type { ReactNode } from 'react'
import clsx from 'clsx'
import type { CheckResult, ImportAcceptance, ManifestOut } from '../../types'
import { CopyButton, Notice } from '../../components/ui'
import { formatBytes, formatCost, formatDateTime, formatTokens } from '../../lib/format'
import { RAW_STATE_LABEL, RECIPE_ORIGIN_LABEL, RECIPE_TEXT, VERSIONS_TEXT as VT } from '../../lib/terms'
import { CheckStatusChip } from './ImportReceipt'
import { ReceiptSummary } from './ReceiptBlocks'

// ===========================================================================
// 导入清单的渲染（P3-SPEC 7.3）：文件、配方、统计期、核对、接受、确认清单、修改记录、说明、回执摘要、AI 用量，
// 原始清单默认收起。
//
// 从版本页（VersionsDialog）抽出来，证据面板的「数据版本」一节也用它（P4-SPEC 4.1）：面板里查看导入清单
// 不能退回整份 JSON 输出——那样遮罩不起作用，64 KB 的清单也没法读。两处的行为必须一致，check-versions 守版本页，
// check-evidence 的 provenance 段守面板
// ===========================================================================

function Block({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section className="space-y-1.5 rounded-lg border bg-bg p-3 text-xs" data-manifest-block={id}>
      <h4 className="font-semibold">{title}</h4>
      {children}
    </section>
  )
}

const short = (sha: unknown) => (typeof sha === 'string' && sha ? sha.slice(0, 8) : '')
const cellsShort = (cells: string[]) => cells.map((c) => c.slice(c.lastIndexOf('!') + 1)).join('、')
const list = <T,>(v: unknown): T[] => (Array.isArray(v) ? (v as T[]) : [])

/**
 * 导入清单的各块。content 是服务端存的原样（内容寻址，取回时复验了哈希），字段都按「可能缺」读：期 2 的清单
 * 没有修改记录，简单导入只有导入回执。
 *
 * 清单之外的三样（文件名兜底、原件状态、配方第几版）由调用方给：版本页取自导入记录，证据面板取自推断来源接口的
 * 那一期（PartView）。原件状态和配方版次是当前状态，不在清单里
 */
export function ManifestBody({ m, fileName, rawState, recipeSeq }: {
  m: ManifestOut
  /** 清单没记文件名时显示的名字 */
  fileName?: string | null
  /** 原件状态（kept / purged / absent），取不到为 null 时不显示 */
  rawState?: string | null
  /** 配方第几版，取不到为 null 时不显示 */
  recipeSeq?: number | null
}) {
  const c = m.content ?? {}
  const B = VT.manifestBlocks
  const simple = m.kind === 'build_report'
  const receipt = simple ? c : c.receipt
  const checks = list<CheckResult>(c.checks)
  const titleOf = (id: string) => checks.find((x) => x.id === id)?.title
  const accepts: ImportAcceptance[] = [
    ...list<ImportAcceptance>(c.acceptances?.overrides).map((a) => ({ ...a, kind: 'override' })),
    ...list<ImportAcceptance>(c.acceptances?.waivers).map((a) => ({ ...a, kind: 'waiver' })),
  ]
  const confirmations = list<{ id: string; label: string; at?: string }>(c.confirmations)
  const edits = Array.isArray(c.edits) ? list<Record<string, any>>(c.edits) : null
  const rendered = Object.entries((c.notes?.rendered ?? {}) as Record<string, { comment?: string; columns?: Record<string, string> }>)
  const usage = list<Record<string, any>>(c.ai?.usage)
  const tokens = usage.reduce((n, u) => n + (Number(u.input_tokens) || 0) + (Number(u.output_tokens) || 0), 0)
  const cost = usage.reduce((n, u) => n + (Number(u.cost_usd) || 0), 0)
  const period = c.period as { start?: string; end?: string; source?: string; cells?: string[]; signed_by?: string | null } | null
  const outside = list(receipt?.outside_text)

  return (
    <div className="space-y-2.5" data-manifest={m.kind} data-verified={m.verified ? 'true' : 'false'}>
      {simple
        ? <Notice tone="info">{VT.manifestSimple}</Notice>
        : !m.verified && <Notice tone="err" attr={{ 'data-manifest-unverified': '' }}>{VT.manifestUnverified}</Notice>}

      {!simple && (
        <>
          <Block id="file" title={B.file}>
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <span className="mono break-all">{c.file?.name || fileName}</span>
              {c.file?.size != null && <span className="tnum text-dim">{formatBytes(Number(c.file.size))}</span>}
              {short(c.file?.raw_sha256) && <span className="mono text-dim">{VT.sha(short(c.file?.raw_sha256))}</span>}
              {rawState != null && <span className="chip">{RAW_STATE_LABEL[rawState] ?? rawState}</span>}
            </div>
            {c.signed_by?.name && <div className="text-2xs text-faint">{VT.signedBy(c.signed_by.name)}</div>}
          </Block>

          <Block id="recipe" title={B.recipe}>
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              {recipeSeq != null && <span>{VT.recipeSeq(recipeSeq)}</span>}
              {c.recipe?.origin && <span className="text-dim">{RECIPE_ORIGIN_LABEL[c.recipe.origin] ?? c.recipe.origin}</span>}
              {short(c.recipe?.sha256) && <span className="mono text-dim">{VT.sha(short(c.recipe?.sha256))}</span>}
            </div>
            {c.recipe?.canonical && <RawJson summary={VT.recipeJson} value={c.recipe.canonical} attr="data-recipe-json" />}
          </Block>

          <Block id="period" title={B.period}>
            {period?.start && period?.end ? (
              <div className="space-y-0.5">
                <div className="tnum">{VT.period(period.start, period.end)}</div>
                <div className="text-2xs text-faint">
                  {period.source === 'human'
                    ? `${VT.periodHuman}，${VT.signedBy(period.signed_by || VT.unsigned)}`
                    : period.cells?.length ? RECIPE_TEXT.periodFromCells(cellsShort(period.cells)) : ''}
                </div>
              </div>
            ) : <p className="text-faint">{VT.periodUnknown}</p>}
          </Block>

          <Block id="checks" title={B.checks}>
            {checks.length ? (
              <ul className="space-y-1.5">
                {checks.map((k) => (
                  <li key={k.id} className="space-y-1 rounded-md border px-2.5 py-1.5" data-manifest-check={k.id} data-status={k.status}>
                    <div className="flex flex-wrap items-center gap-2">
                      <CheckStatusChip status={k.status} />
                      <span className="mono text-faint">{k.id}</span>
                      <span className="min-w-0 flex-1">{k.title}</span>
                      {/* 计数照试运行回执的规则（ImportReceipt 的 CheckRow）：「说明」类的 failed 是预期内的不相等个数，不写 */}
                      {!!k.checked && k.status !== 'info' && (
                        <span className="tnum text-2xs text-faint" data-check-counts="">
                          {VT.checkCounts(k.checked, k.failed ?? 0, k.unverifiable ?? 0)}
                        </span>
                      )}
                    </div>
                    {/* 清单是当证据核对的，细节不截断：服务端每个核对最多逐条列 20 格，其余写成「另有 N 格不一致」
                        「N 格无法核对：…」，截掉的正是这几行 */}
                    {!!k.details?.length && (
                      <ul className="space-y-0.5 text-2xs leading-relaxed text-dim" data-check-details={k.details.length}>
                        {k.details.map((d, i) => <li key={i}>{d}</li>)}
                      </ul>
                    )}
                    {k.sql && <RawJson summary={VT.sql} value={k.params?.length ? `${k.sql}\n-- ${JSON.stringify(k.params)}` : k.sql} attr="data-check-sql" />}
                  </li>
                ))}
              </ul>
            ) : <p className="text-faint">{VT.checksNone}</p>}
          </Block>

          <Block id="acceptances" title={B.acceptances}>
            {accepts.length ? (
              <ul className="space-y-1">
                {accepts.map((a, i) => (
                  <li key={`${a.check_id}-${i}`} className="space-y-0.5 rounded-md border px-2.5 py-1.5" data-manifest-acceptance={a.check_id}>
                    <div className="flex flex-wrap items-baseline gap-x-2">
                      <span className="chip">{VT.acceptKind[a.kind] ?? VT.acceptKind.override}</span>
                      <span className="min-w-0 flex-1 break-words">
                        {VT.acceptRow(titleOf(a.check_id) ? `${a.check_id} ${titleOf(a.check_id)}` : a.check_id, a.reason)}
                      </span>
                    </div>
                    <div className="flex flex-wrap gap-x-2 text-2xs text-faint">
                      <span>{VT.signedBy(a.signed_by || VT.unsigned)}</span>
                      {a.at && <span className="tnum">{formatDateTime(a.at)}</span>}
                    </div>
                  </li>
                ))}
              </ul>
            ) : <p className="text-faint">{VT.acceptancesNone}</p>}
          </Block>

          <Block id="confirmations" title={B.confirmations}>
            {confirmations.length ? (
              <ul className="list-inside list-disc space-y-0.5 text-dim">
                {confirmations.map((x, i) => <li key={`${x.id}-${i}`}>{x.label}</li>)}
              </ul>
            ) : <p className="text-faint">{VT.confirmationsNone}</p>}
          </Block>

          <Block id="edits" title={B.edits}>
            {edits == null
              ? <p className="text-faint">{VT.editsUnrecorded}</p>
              : edits.length ? (
                <ul className="space-y-1">
                  {edits.map((e, i) => (
                    <li key={`${e.seq ?? i}`} className={clsx('flex flex-wrap items-baseline gap-x-2 rounded-md border px-2.5 py-1.5', e.superseded && 'opacity-60')}
                        data-manifest-edit={e.kind}>
                      <span className="chip">{VT.editKind[e.kind] ?? VT.editKind.fix}</span>
                      <span className="min-w-0 flex-1 break-words">{e.title}</span>
                      {e.superseded && <span className="text-2xs text-faint">{VT.editSuperseded}</span>}
                      <span className="text-2xs text-faint">{VT.signedBy(e.signed_by || VT.unsigned)}</span>
                      {e.at && <span className="tnum text-2xs text-faint">{formatDateTime(e.at)}</span>}
                    </li>
                  ))}
                </ul>
              ) : <p className="text-faint">{VT.editsNone}</p>}
          </Block>

          <Block id="notes" title={B.notes}>
            {rendered.length ? (
              <ul className="space-y-2">
                {rendered.map(([table, n]) => (
                  <li key={table} className="space-y-0.5" data-manifest-note={table}>
                    <div className="font-medium">{table}</div>
                    {n.comment && <p className="leading-relaxed text-dim">{n.comment}</p>}
                    {Object.entries(n.columns ?? {}).length > 0 && (
                      <ul className="space-y-0.5 text-2xs text-dim">
                        {Object.entries(n.columns ?? {}).map(([col, text]) => <li key={col}>{col}：{text}</li>)}
                      </ul>
                    )}
                  </li>
                ))}
              </ul>
            ) : <p className="text-faint">{VT.notesNone}</p>}
          </Block>
        </>
      )}

      <section className="space-y-1.5 rounded-lg border bg-bg p-3" data-manifest-block="receipt">
        <ReceiptSummary receipt={receipt} />
        {outside.length > 0 && <p className="text-2xs text-faint" data-outside-note="">{VT.outsideTextNote}</p>}
      </section>

      {!simple && (
        <Block id="ai" title={B.ai}>
          <p className={usage.length ? 'text-dim' : 'text-faint'}>
            {usage.length ? VT.aiUsage(usage.length, formatTokens(tokens), formatCost(cost)) : VT.aiNone}
          </p>
        </Block>
      )}

      <RawJson summary={VT.manifestRaw} value={c} attr="data-manifest-raw" />
    </div>
  )
}

/** 等宽的原文（原始清单、配方 JSON、核对的 SQL），默认收起：给要逐字核对的人看，带复制 */
function RawJson({ summary, value, attr }: { summary: string; value: unknown; attr: string }) {
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2)
  return (
    <details className="text-2xs" {...{ [attr]: '' }}>
      <summary className="cursor-pointer select-none text-faint hover:text-dim">{summary}</summary>
      <div className="mt-1.5 flex items-start gap-1.5">
        <pre className="mono max-h-80 min-w-0 flex-1 overflow-auto whitespace-pre-wrap break-all rounded-md border bg-[var(--bg)] p-2 leading-relaxed text-dim">
          {text}
        </pre>
        <CopyButton text={text} />
      </div>
    </details>
  )
}
