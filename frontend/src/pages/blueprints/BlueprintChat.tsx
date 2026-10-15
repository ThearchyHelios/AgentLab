/**
 * 蓝图页左边的对话（docs/design/normal-user-apps.md §2.2、§4.2、N11）。
 *
 * 过程照 run/decode.ts 的说法显示（decodeCopilot 的 blueprint 语境：查看了什么、改了哪张卡片的哪一项、问了什么）；只回答
 * 不改卡片的一轮把回答当成果摆出来。助手问回来一句（bp_ask）时这一轮就此结束：问题和候选画成可点的选项，常显在输入框
 * 上面，写明「这一轮在等你回答」，直到回答；点选项就是回答，等于把它作为下一轮发出去。
 *
 * 之前的轮次（服务端落进蓝图对话的）只有问题、查看记录、回答和助手问回来的话（meta.asked）——当时的改动已经落在卡片上，
 * 不重放。
 */
import { useMemo, useState } from 'react'
import { MessageCircleQuestion } from 'lucide-react'
import { AssistantStream, type StreamTurn } from '../../run/AssistantStream'
import { PromptBox } from '../../run/Composer'
import { decodeCopilot, type CopilotOp } from '../../run/decode'
import { BLUEPRINT_TEXT } from '../../lib/terms'
import { chatBusy, openAsk, useBlueprint, type BpTurn } from '../../store/blueprint'
import type { ConversationTurn } from '../../types'

/** 落过库的一轮 → 显示用的一轮。meta.asked 是服务端记的反问 {question, options}；answer 是回答或这一轮的说明 */
function pastTurn(t: ConversationTurn): StreamTurn {
  const meta = (t.meta ?? {}) as { looks?: CopilotOp[]; asked?: { question?: string; options?: string[] } | null }
  const asked = meta.asked && typeof meta.asked === 'object' ? meta.asked : null
  const ops: CopilotOp[] = [...(Array.isArray(meta.looks) ? meta.looks.map((l) => ({ ...l, op: 'look' })) : [])]
  if (t.status !== 'error') ops.push({ op: 'done', explanation: t.explanation || '' })
  if (asked?.question) ops.push({ op: 'bp_ask', question: asked.question, options: asked.options ?? [] })
  return {
    id: `past-${t.id}`, question: t.question, phase: t.status === 'error' ? 'error' : 'done',
    status: asked ? BLUEPRINT_TEXT.turnAsked : '',
    steps: decodeCopilot(ops, { context: 'blueprint' }),
    // 回答、这一轮的说明当成果摆出来（和反问的问题是同一句时不重复）
    ...(t.answer && t.answer !== asked?.question ? { output: { answer: t.answer } } : {}),
    ...(t.status === 'error' && t.error ? { error: t.error } : {}),
  }
}

function liveTurn(t: BpTurn): StreamTurn {
  const reply = [...t.ops].reverse().find((o) => o.op === 'reply')
  const asked = t.ops.some((o) => o.op === 'bp_ask' || o.op === 'ask')
  return {
    id: t.id, question: t.question, phase: t.phase,
    status: t.phase === 'running' ? t.status : asked ? BLUEPRINT_TEXT.turnAsked : undefined,
    steps: decodeCopilot(t.ops, { context: 'blueprint' }),
    ...(reply ? { output: { answer: String(reply.text ?? '') } } : {}),
    ...(t.error ? { error: t.error } : {}),
    startedAt: t.phase === 'running' ? t.startedAt : null,
    ...(t.endedAt ? { elapsedMs: t.endedAt - t.startedAt } : {}),
  }
}

export function BlueprintChat({ blueprintId }: { blueprintId: string }) {
  const entry = useBlueprint((s) => s.byId[blueprintId])
  const [text, setText] = useState('')
  const busy = chatBusy(entry)
  const ask = openAsk(entry)
  const turns = useMemo(() => [...(entry?.past ?? []).map(pastTurn), ...(entry?.turns ?? []).map(liveTurn)],
    [entry?.past, entry?.turns])

  const send = (value: string, askId?: string | null) => {
    if (useBlueprint.getState().send(blueprintId, value, { askId: askId ?? ask?.id ?? null })) setText('')
  }

  return (
    <div className="flex h-full min-h-0 flex-col" data-blueprint-chat={entry?.turns.length ?? 0}
         data-blueprint-chat-past={entry?.past.length ?? 0}>
      <h2 className="shrink-0 border-b px-3 py-2 text-xs font-semibold">{BLUEPRINT_TEXT.chatTitle}</h2>
      {entry?.historyFailed && (
        <p className="shrink-0 border-b px-3 py-1.5 text-2xs leading-relaxed" style={{ color: 'var(--st-waiting)' }}
           data-blueprint-history-failed="">
          {BLUEPRINT_TEXT.historyFailed}
        </p>
      )}
      <div className="min-h-0 flex-1">
        <AssistantStream turns={turns} dense
                         empty={<p className="p-4 text-xs leading-relaxed text-faint">{BLUEPRINT_TEXT.chatEmpty}</p>} />
      </div>
      <div className="shrink-0 border-t p-2">
        {ask && (
          // 这一轮以反问收尾：问题和候选常显，直到回答
          <div className="mb-2 rounded-lg border p-2.5" role="status" data-blueprint-ask={ask.id}
               style={{ borderColor: 'color-mix(in srgb, var(--st-waiting) 45%, var(--border))',
                        background: 'color-mix(in srgb, var(--st-waiting) 7%, transparent)' }}>
            <div className="flex items-center gap-1.5 text-2xs font-semibold" style={{ color: 'var(--st-waiting)' }}
                 data-blueprint-waiting="">
              <MessageCircleQuestion size={12} aria-hidden /> {BLUEPRINT_TEXT.waiting}
            </div>
            <p className="mt-1 text-xs leading-relaxed text-fg" data-blueprint-ask-question="">{ask.question}</p>
            {ask.options.length > 0 && (
              <div className="mt-1.5 flex flex-wrap gap-1.5" role="group" aria-label={ask.question}>
                {ask.options.map((o) => (
                  <button key={o} type="button" className="btn btn-xs" disabled={busy} onClick={() => send(o, ask.id)}
                          data-blueprint-ask-option={o}>
                    {o}
                  </button>
                ))}
              </div>
            )}
            <p className="mt-1.5 text-2xs text-faint">{BLUEPRINT_TEXT.waitingHint}</p>
          </div>
        )}
        <PromptBox value={text} onChange={setText} onSubmit={() => send(text)} busy={busy} size="panel"
                   blocked={entry?.blueprint ? null : BLUEPRINT_TEXT.loadFailed}
                   onStop={() => useBlueprint.getState().stop(blueprintId)} stopLabel={BLUEPRINT_TEXT.chatStop}
                   label={BLUEPRINT_TEXT.chatLabel}
                   placeholder={ask ? BLUEPRINT_TEXT.chatAnswerPlaceholder : BLUEPRINT_TEXT.chatPlaceholder} />
        <p className="mt-1 text-2xs text-faint">{BLUEPRINT_TEXT.chatHint}</p>
      </div>
    </div>
  )
}
