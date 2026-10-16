import { useMemo } from 'react'
import clsx from 'clsx'
import { FREQ_PRESETS, HOUR_STEPS, WEEKDAY_ORDER, cronOf, timezoneOptions, type Freq, type FreqPreset } from '../lib/freq'
import { SCHEDULE_TEXT as T } from '../lib/terms'
import { Field, useRadioGroup } from './ui'

/**
 * 频率和时区的选择：预设（每天、每周、每月、每隔几小时）和自定义 cron，加时区。从定时任务的编辑表单抽出来，数据源的
 * 定时维护也用它（next-round 03 B4：维护就是一条目标为数据源的定时任务，频率的写法、说法和定时任务一样）。
 *
 * 只管填：cron 合不合法、接下来几次什么时候由服务端的预览判（POST /api/schedules/preview），问题由调用方经 freqError /
 * tzError 交进来，说明接到出问题的那一项上。
 */
export function FreqPicker({ freq, timezone, setFreq, setTimezone, freqError, tzError, idPrefix = 'schedule', title = T.freq }: {
  freq: Freq
  timezone: string
  setFreq: (patch: Partial<Freq>) => void
  setTimezone: (tz: string) => void
  freqError?: string
  tzError?: string
  /** 元素 id 的前缀：同一页放两个频率选择时各给一个，免得 id 撞上（data-* 属性不带前缀，检查脚本照旧认） */
  idPrefix?: string
  /** 这一节的标题，缺省「频率」 */
  title?: string
}) {
  const f = freq
  const pick = (preset: FreqPreset) => {
    if (preset === f.preset) return
    // 切到自定义：带上当前预设生成的 cron，在它的基础上改
    if (preset === 'custom') setFreq({ preset, cron: cronOf(f) || f.cron })
    else setFreq({ preset })
  }
  const radio = useRadioGroup(FREQ_PRESETS, f.preset, pick)
  const errId = `${idPrefix}-freq-error`
  // 说明接到出问题的那一项上：每周一天都没选时是星期那一组，否则是这个预设主要的输入框
  const weekdayError = !!freqError && f.preset === 'weekly' && !f.weekdays.length
  const errorProps = (primary: boolean) => (primary && freqError && !weekdayError
    ? { 'aria-invalid': true as const, 'aria-describedby': errId } : {})
  const zones = useMemo(() => timezoneOptions(timezone), [timezone])
  const timeInput = (
    <Field label={T.time} htmlFor={`${idPrefix}-time`}>
      <input id={`${idPrefix}-time`} type="time" step={60} className="field w-32" value={f.time} required
             onChange={(e) => setFreq({ time: e.target.value })} data-freq-time="" {...errorProps(true)} />
    </Field>
  )

  return (
    <section className="space-y-3 rounded-lg border bg-panel p-3.5" aria-labelledby={`${idPrefix}-freq-title`}>
      <h2 id={`${idPrefix}-freq-title`} className="text-xs font-semibold">{title}</h2>
      <div role="radiogroup" aria-labelledby={`${idPrefix}-freq-title`} className="flex flex-wrap gap-1">
        {FREQ_PRESETS.map((p) => (
          <button key={p} type="button" {...radio(p)} onClick={() => pick(p)} data-freq-preset={p}
                  className={clsx('rounded-md border px-2.5 py-1 text-xs transition-colors',
                    f.preset === p ? 'border-[var(--accent)] bg-accent-soft text-fg' : 'text-dim hover:bg-hover')}>
            {T.preset[p]}
          </button>
        ))}
      </div>

      <div className="flex flex-wrap items-start gap-x-4 gap-y-3">
        {f.preset === 'weekly' && (
          <div className="min-w-0">
            <div className="label" id={`${idPrefix}-weekdays`}>{T.weekdays}</div>
            <div role="group" aria-labelledby={`${idPrefix}-weekdays`} aria-describedby={weekdayError ? errId : undefined}
                 className="flex flex-wrap gap-1">
              {WEEKDAY_ORDER.map((d) => {
                const on = f.weekdays.includes(d)
                return (
                  <button key={d} type="button" aria-pressed={on} data-weekday={d}
                          onClick={() => setFreq({ weekdays: on ? f.weekdays.filter((x) => x !== d) : [...f.weekdays, d] })}
                          className={clsx('h-7 min-w-9 rounded-md border px-1.5 text-xs transition-colors',
                            on ? 'border-[var(--accent)] bg-accent-soft text-fg' : 'text-dim hover:bg-hover')}>
                    {T.weekdayShort[d]}
                  </button>
                )
              })}
            </div>
          </div>
        )}
        {f.preset === 'monthly' && (
          <Field label={T.monthday} htmlFor={`${idPrefix}-monthday`} hint={f.monthday > 28 ? T.monthdayHint : undefined}>
            <select id={`${idPrefix}-monthday`} className="field w-24" value={f.monthday} data-freq-monthday=""
                    aria-describedby={f.monthday > 28 ? `${idPrefix}-monthday-hint` : undefined}
                    onChange={(e) => setFreq({ monthday: Number(e.target.value) })}>
              {Array.from({ length: 31 }, (_, i) => i + 1).map((d) => <option key={d} value={d}>{T.monthdayOption(d)}</option>)}
            </select>
          </Field>
        )}
        {(f.preset === 'daily' || f.preset === 'weekly' || f.preset === 'monthly') && timeInput}
        {f.preset === 'hourly' && (
          <>
            <Field label={T.everyHours} htmlFor={`${idPrefix}-every`}>
              <select id={`${idPrefix}-every`} className="field w-32" value={f.everyHours} data-freq-every=""
                      onChange={(e) => setFreq({ everyHours: Number(e.target.value) })}>
                {HOUR_STEPS.map((n) => <option key={n} value={n}>{T.everyHoursOption(n)}</option>)}
              </select>
            </Field>
            <Field label={T.minute} htmlFor={`${idPrefix}-minute`}>
              <input id={`${idPrefix}-minute`} type="number" min={0} max={59} step={1} className="field w-20" data-freq-minute=""
                     value={Number.isFinite(f.minute) ? f.minute : ''} {...errorProps(true)}
                     onChange={(e) => setFreq({ minute: e.target.value === '' ? Number.NaN : Number(e.target.value) })} />
            </Field>
          </>
        )}
        {f.preset === 'custom' && (
          <Field label={T.cron} htmlFor={`${idPrefix}-cron`} hint={freqError ? undefined : T.cronHint} className="min-w-0 flex-1 basis-56">
            <input id={`${idPrefix}-cron`} className="field mono" value={f.cron} placeholder="0 9 * * 1-5" autoComplete="off" spellCheck={false}
                   onChange={(e) => setFreq({ cron: e.target.value })} data-freq-cron=""
                   {...(freqError ? errorProps(true) : { 'aria-describedby': `${idPrefix}-cron-hint` })} />
          </Field>
        )}
      </div>
      {f.preset === 'hourly' && !freqError && (
        <p className="text-2xs leading-relaxed text-faint">{T.hourlyHint(f.everyHours, f.minute)}</p>
      )}
      {freqError && (
        <div id={errId} className="text-2xs leading-relaxed text-[var(--err)]" role="alert" data-freq-error="">{freqError}</div>
      )}

      <Field label={T.timezone} htmlFor={`${idPrefix}-timezone`} hint={T.timezoneHint} error={tzError}>
        <select id={`${idPrefix}-timezone`} className="field" value={timezone} data-timezone=""
                aria-invalid={!!tzError || undefined}
                aria-describedby={tzError ? `${idPrefix}-timezone-error` : `${idPrefix}-timezone-hint`}
                onChange={(e) => setTimezone(e.target.value)}>
          {zones.map((z) => <option key={z.tz} value={z.tz}>{z.label}</option>)}
        </select>
      </Field>
    </section>
  )
}
