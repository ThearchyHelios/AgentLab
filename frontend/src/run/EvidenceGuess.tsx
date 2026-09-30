import { useId, useMemo, useState } from 'react'
import { ChevronRight } from 'lucide-react'
import clsx from 'clsx'
import { EVIDENCE_STATE, candidateText } from '../lib/evidence'
import { formatNumber } from '../lib/format'
import { EVIDENCE_TEXT } from '../lib/terms'
import type { EvidenceGuess } from '../types'
import { Markdown, type MarkSpec } from './Markdown'

/**
 * 没有契约的旧运行：按数值猜的可能来源（用户拍板第 8 条）。
 *
 * 默认折叠：收着的时候就是原来的答案，一个字不多；标题写明这是「猜测」。展开后同一段答案里
 * 有候选的数字画最淡的点状线（证据状态表里「旧运行候选」那一档，绝不用确定性的实线和绿），
 * 下面逐个列出候选，开头先写「猜测的来源，不能当证据」——同值的巧合很多，这不是证据。
 *
 * 答案照原来的 Markdown 版式画（旧答案常有表格、列表），候选是行内的标记，不是按钮：键盘和读屏
 * 用户在候选清单里看全每一个，标记上也带着读屏专用的一句「猜测的来源：…」。
 */
export function EvidenceGuessView({ guesses, dense = false, defaultOpen = false }: {
  guesses: EvidenceGuess[]
  dense?: boolean
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  const bodyId = useId()
  const stats = useMemo(() => guesses.reduce((acc, g) => ({
    numbers: acc.numbers + (g.stats?.numbers ?? 0),
    guessed: acc.guessed + (g.stats?.guessed ?? 0),
  }), { numbers: 0, guessed: 0 }), [guesses])
  const note = guesses.find((g) => g.note)?.note || EVIDENCE_TEXT.guessNote
  const many = guesses.length > 1
  if (!guesses.length) return null
  return (
    <section data-ev-guess="" data-open={open ? 'true' : 'false'} className="min-w-0">
      <button type="button" className="flex w-full min-w-0 items-center gap-1.5 rounded px-1 py-0.5 text-left text-2xs transition-colors hover:bg-hover"
              aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen((v) => !v)} data-ev-guess-toggle="">
        <ChevronRight size={11} aria-hidden className={clsx('shrink-0 text-faint', open && 'rotate-90')} />
        <span className="shrink-0 font-medium" style={{ color: EVIDENCE_STATE.candidate.color }}>
          <span aria-hidden>{EVIDENCE_STATE.candidate.glyph} </span>{EVIDENCE_TEXT.guessTitle}
        </span>
        <span className="min-w-0 truncate text-faint">
          · {EVIDENCE_TEXT.guessCount(stats.guessed, stats.numbers)} · 不可作为证据
        </span>
      </button>
      <div id={bodyId} className="mt-1.5 space-y-3" data-ev-guess-body={open ? '' : undefined}>
        {open && (
          <p className="rounded border border-dashed px-2 py-1 text-2xs leading-relaxed text-dim" data-ev-guess-note="">
            {note}
          </p>
        )}
        {guesses.map((g, i) => (
          <GuessField key={g.field ?? i} guess={g} open={open} dense={dense} title={many ? g.field : undefined} />
        ))}
      </div>
    </section>
  )
}

function GuessField({ guess, open, dense, title }: {
  guess: EvidenceGuess; open: boolean; dense: boolean; title?: string
}) {
  const segs = useMemo(() => (guess.segments ?? []).filter((s) => s.kind === 'number'), [guess])
  const guessed = segs.filter((s) => s.state === 'candidate' && s.candidates?.length)
  const missed = segs.filter((s) => !(s.state === 'candidate' && s.candidates?.length))
  // 候选按位置标在原文上（码点偏移，Markdown 自己换算、核对切出来的字）；收着的时候不标，就是原来的答案
  const marks = useMemo<MarkSpec[] | undefined>(() => (open ? guessed.map((s) => ({
    token: s.text, tone: 'candidate' as const, start: s.span?.[0], end: s.span?.[1],
    title: (s.candidates ?? []).map(candidateText).join('；'),
  })) : undefined), [open, guessed])
  const text = guess.markdown ?? ''
  return (
    <div className="min-w-0" data-ev-guess-field={guess.field ?? ''}>
      {title && <div className="mb-0.5 text-2xs font-medium text-faint">{title}</div>}
      <Markdown text={text} dense={dense} marks={marks} />
      {open && (
        <div className="mt-2 text-2xs">
          <div className="mb-1 font-medium text-faint">{EVIDENCE_TEXT.guessCandidates}</div>
          {guessed.length ? (
            <ol className="space-y-1" data-ev-candidates="">
              {guessed.map((s) => (
                <li key={s.id} className="flex min-w-0 items-baseline gap-2" data-ev-candidate={s.id}>
                  <span className="mono tnum shrink-0 font-medium">
                    <span aria-hidden style={{ color: EVIDENCE_STATE.candidate.color }}>{EVIDENCE_STATE.candidate.glyph}</span>{s.text}
                  </span>
                  <span className="min-w-0 text-dim [overflow-wrap:anywhere]">
                    {(s.candidates ?? []).map(candidateText).join('；')}
                  </span>
                </li>
              ))}
            </ol>
          ) : <p className="text-dim">{EVIDENCE_TEXT.guessNone}</p>}
          {missed.length > 0 && guessed.length > 0 && (
            <p className="mt-1 text-faint" data-ev-guess-missed="">
              {EVIDENCE_TEXT.guessNone}：{missed.map((s) => s.text).join('、')}（{formatNumber(missed.length)} 个）
            </p>
          )}
        </div>
      )}
    </div>
  )
}
