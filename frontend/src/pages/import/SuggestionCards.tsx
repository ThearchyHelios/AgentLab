import { useEffect, useId, useState } from 'react'
import type { ReactNode } from 'react'
import { Lightbulb } from 'lucide-react'
import clsx from 'clsx'
import type { AnswerDropped, Card, Question, QuestionAnswer } from '../../types'
import { Notice, Spinner } from '../../components/ui'
import { ACCUMULATE_TEXT, RECIPE_TEXT } from '../../lib/terms'

// ===========================================================================
// 建议卡片与待确认问题：起草器给的每条建议一句理由、指向网格上的格子；问题只有封闭的选项，
// 选了哪一项发给服务端，由服务端按选项改配方（界面自己不改配方）
// ===========================================================================

/** 格子坐标的小按钮：点了网格滚到那一格 */
export function CellChips({ cells, onFocus, max = 6 }: { cells?: string[]; onFocus: (cell: string) => void; max?: number }) {
  if (!cells?.length) return null
  return (
    <span className="inline-flex flex-wrap items-center gap-1" data-cells>
      {cells.slice(0, max).map((c) => (
        <button key={c} type="button" className="chip mono hover:border-[var(--accent)] hover:text-fg" data-cell-ref={c}
                onClick={() => onFocus(c)} title={c}>
          {c.includes('!') ? c.slice(c.lastIndexOf('!') + 1) : c}
        </button>
      ))}
      {cells.length > max && <span className="text-2xs text-faint">+{cells.length - max}</span>}
    </span>
  )
}

/** 单选组的一个选项：detail 是选项下方的一句后果，badge 是选项旁的标注（如「破坏性变更」） */
export interface ChoiceOption { value: string; label: string; detail?: string; badge?: ReactNode }

/**
 * 封闭选项的单选组，**没有默认选中**：问题卡片和修复面板共用（P3-SPEC 10.1 复用清单）。选中哪一项、要不要
 * 理由、什么时候发请求由调用方定；这里只负责画和报。attr 是每个选项根节点上的定位属性名（问题用 data-option，
 * 修复面板用 data-fix-option），检查脚本按它找
 */
export function ChoiceGroup({ name, label, options, value, disabled = false, attr = 'data-option', vertical, onChange }: {
  name: string
  label: string
  options: ChoiceOption[]
  value: string | null
  disabled?: boolean
  attr?: string
  /** 有后果句的选项竖排：一句话挂在选项下方，横排时挤在一起分不清是哪一项的 */
  vertical?: boolean
  onChange: (value: string) => void
}) {
  const stacked = vertical ?? options.some((o) => o.detail || o.badge)
  return (
    <div role="radiogroup" aria-label={label} className={clsx(stacked ? 'space-y-1.5' : 'flex flex-wrap gap-x-4 gap-y-1')}>
      {options.map((o) => (
        <label key={o.value} className={clsx('gap-1.5 text-xs', stacked ? 'flex items-start' : 'inline-flex items-center',
          disabled ? 'cursor-wait' : 'cursor-pointer')} {...{ [attr]: o.value }}>
          <input type="radio" name={name} value={o.value} checked={value === o.value} disabled={disabled}
                 className={stacked ? 'mt-0.5' : undefined} onChange={() => onChange(o.value)} />
          <span className="min-w-0 flex-1">
            <span>{o.label}</span>
            {o.badge}
            {o.detail && <span className="mt-0.5 block text-2xs leading-relaxed text-dim" data-option-detail>{o.detail}</span>}
          </span>
        </label>
      ))}
    </div>
  )
}

/**
 * 一个问题：单选，**没有默认选中**（起草器的建议只写在旁边，不替人选）。选需要理由的选项（如「不登记」）
 * 时先出理由框，写了理由点「提交」才发；普通选项选了就发。已选的按服务端存的回答回显。
 * 导入模式（q_mode）的两个选项下方各写一句后果（P3-SPEC 2.2）：选之前就知道每期上传会怎样
 */
function QuestionItem({ q, answer, busy, onAnswer }: {
  q: Question; answer?: QuestionAnswer; busy: boolean; onAnswer: (qid: string, a: QuestionAnswer) => Promise<boolean>
}) {
  const name = useId()
  const [pending, setPending] = useState<string | null>(null)
  const [reason, setReason] = useState(answer?.reason ?? '')
  // 服务端的回答变了（刷新、改了配方清空了）：放下没提交的那一项
  useEffect(() => { setPending(null); setReason(answer?.reason ?? '') }, [answer?.value, answer?.reason])
  const chosen = pending ?? answer?.value ?? null
  const needsReason = q.options.find((o) => o.value === chosen)?.needs_reason
  const suggested = q.default ? q.options.find((o) => o.value === q.default)?.label : null
  const options: ChoiceOption[] = q.options.map((o) => ({
    value: o.value, label: o.label, detail: q.id === 'q_mode' ? ACCUMULATE_TEXT.modeConsequence[o.value] : undefined,
  }))
  return (
    <fieldset className="space-y-1.5 rounded-md border bg-bg px-2.5 py-2" data-question={q.id}>
      <legend className="px-1 text-xs font-medium">{q.text}</legend>
      <ChoiceGroup name={name} label={q.text} options={options} value={chosen} disabled={busy}
                   onChange={(v) => {
                     // 先按选中画（等服务端回来才勾上，点了像没反应）；没发成功就退回服务端存的那一项
                     setPending(v)
                     if (q.options.find((o) => o.value === v)?.needs_reason) return
                     void onAnswer(q.id, { value: v }).then((ok) => { if (!ok) setPending(null) })
                   }} />
      {suggested && !chosen && <p className="text-2xs text-faint" data-question-suggested={q.default}>{RECIPE_TEXT.suggested(suggested)}</p>}
      {needsReason && (
        <div className="space-y-1" data-question-reason>
          <label className="label" htmlFor={`${name}-reason`}>{RECIPE_TEXT.reasonLabel}</label>
          <div className="flex items-start gap-2">
            <textarea id={`${name}-reason`} className="field text-xs" rows={2} maxLength={200} value={reason}
                      placeholder={RECIPE_TEXT.reasonPlaceholder} disabled={busy}
                      onChange={(e) => setReason(e.target.value)} />
            <button type="button" className="btn btn-sm shrink-0" disabled={busy || !reason.trim()}
                    title={!reason.trim() ? RECIPE_TEXT.reasonRequired : undefined}
                    onClick={() => onAnswer(q.id, { value: chosen!, reason: reason.trim() })}>
              {RECIPE_TEXT.reasonSubmit}
            </button>
          </div>
        </div>
      )}
    </fieldset>
  )
}

/**
 * 改配方后被移除的回答（遗留项 2）：按原因分两句，列问题文字，不露问题 id。changed 是问题还在、原来的选项已经
 * 和新配方对不上，要重新回答；gone 是问题已经没有了
 */
export function AnswersDropped({ dropped }: { dropped?: AnswerDropped[] }) {
  if (!dropped?.length) return null
  const list = (reason: string) => dropped.filter((d) => d.reason === reason).map((d) => `「${d.text}」`).join('、')
  const changed = list('changed')
  const gone = list('gone')
  if (!changed && !gone) return null
  return (
    <Notice tone="warn" attr={{ 'data-answers-dropped': '' }}>
      {changed && <div data-answers-dropped-reason="changed">{RECIPE_TEXT.answersChanged(changed)}</div>}
      {gone && <div data-answers-dropped-reason="gone">{RECIPE_TEXT.answersGone(gone)}</div>}
    </Notice>
  )
}

export function SuggestionCards({ cards, questions, answers, busy, applying = false, dropped, onAnswer, onFocusCell }: {
  cards: Card[]
  questions: Question[]
  answers: Record<string, QuestionAnswer>
  busy: boolean
  /** 刚选的回答正在服务端应用（重新起草、干跑）：大表要等几秒，标出来免得以为没反应 */
  applying?: boolean
  /** 改配方后被移除的回答：问题区顶部提示 */
  dropped?: AnswerDropped[]
  /** 返回是否发成功 */
  onAnswer: (qid: string, a: QuestionAnswer) => Promise<boolean>
  onFocusCell: (cell: string) => void
}) {
  const byId = new Map(questions.map((q) => [q.id, q]))
  const linked = new Set(cards.map((c) => c.question).filter(Boolean) as string[])
  const loose = questions.filter((q) => !linked.has(q.id))
  return (
    <div className="space-y-3" data-suggestions>
      <AnswersDropped dropped={dropped} />
      <section className="space-y-2">
        <div className="flex items-center gap-2">
          <h3 className="text-xs font-semibold">{RECIPE_TEXT.cardsTitle}</h3>
          {applying && (
            <span className="inline-flex items-center gap-1 text-2xs text-faint" role="status" data-answer-applying>
              <Spinner size={10} /> {RECIPE_TEXT.answering}
            </span>
          )}
        </div>
        {!cards.length && <p className="text-xs text-faint">{RECIPE_TEXT.noCards}</p>}
        <ol className="space-y-2">
          {cards.map((c) => {
            const q = c.question ? byId.get(c.question) : undefined
            return (
              <li key={c.id} className="space-y-1.5 rounded-lg border bg-panel px-3 py-2" data-suggestion-card={c.id}>
                <div className="flex items-start gap-2">
                  <Lightbulb size={12} className="mt-0.5 shrink-0 text-[var(--warn)]" aria-hidden />
                  <div className="min-w-0 flex-1">
                    <div className="text-xs font-medium">{c.title}</div>
                    <p className="mt-0.5 text-2xs leading-relaxed text-dim" data-card-reason>{c.reason}</p>
                  </div>
                </div>
                <CellChips cells={c.cells} onFocus={onFocusCell} />
                {q && <QuestionItem q={q} answer={answers[q.id]} busy={busy} onAnswer={onAnswer} />}
              </li>
            )
          })}
        </ol>
      </section>
      {loose.length > 0 && (
        <section className="space-y-2">
          <h3 className="text-xs font-semibold">{RECIPE_TEXT.questionsTitle}</h3>
          {loose.map((q) => <QuestionItem key={q.id} q={q} answer={answers[q.id]} busy={busy} onAnswer={onAnswer} />)}
        </section>
      )}
    </div>
  )
}
