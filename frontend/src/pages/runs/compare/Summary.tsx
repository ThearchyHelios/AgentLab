import clsx from 'clsx'
import { formatNumber } from '../../../lib/format'
import { RUN_COMPARE_TEXT as T } from '../../../lib/terms'
import type { RunCompareSummary } from '../../../types'
import { CLASS_ORDER, classTone } from './model'

/**
 * 摘要条：八类指标各有几个，个数照服务端的 summary 写（不按指标表重数）。点一类只看这一类的指标，再点一次回到全部；
 * 是 0 的不能点——筛出一张空表没有意义
 */
export function Summary({ summary, filter, onFilter }: {
  summary: RunCompareSummary; filter: string | null; onFilter: (k: string | null) => void
}) {
  return (
    <section aria-label={T.summary} data-compare-summary="">
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border bg-[var(--border)] sm:grid-cols-4">
        {CLASS_ORDER.map((k) => {
          const n = Number(summary?.[k] ?? 0)
          const on = filter === k
          const tone = classTone(k)
          return (
            <button
              key={k}
              type="button"
              data-compare-class={k}
              aria-pressed={on}
              disabled={n === 0}
              title={`${T.classHint[k]}${n ? `\n${T.summaryHint}` : ''}`}
              onClick={() => onFilter(on ? null : k)}
              className={clsx(
                'relative flex min-w-0 flex-col items-start gap-0.5 px-3 py-2 text-left outline-none transition-colors',
                'focus-visible:z-10 focus-visible:shadow-[inset_0_0_0_2px_var(--accent)] disabled:cursor-default',
                on ? 'bg-hover shadow-[inset_0_-2px_0_var(--accent)]' : 'bg-panel enabled:hover:bg-hover',
              )}
            >
              <span data-compare-count="" className={clsx('mono tnum text-lg leading-6', !n && 'text-faint')}
                    style={n ? { color: tone.color === 'var(--text-faint)' ? 'var(--text)' : tone.color } : undefined}>
                {formatNumber(n)}
              </span>
              <span className="truncate text-2xs text-dim">{T.classLabel[k]}</span>
            </button>
          )
        })}
      </div>
    </section>
  )
}
