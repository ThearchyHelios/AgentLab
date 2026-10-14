import { useEffect, useRef, useState } from 'react'
import { api } from '../../../api/client'
import { streamAppCopilot } from '../../../api/appCopilot'
import { toast } from '../../../components/ui'
import { AssistantStream, type StreamTurn } from '../../../run/AssistantStream'
import { PromptBox } from '../../../run/Composer'
import { COPILOT_PHASE_TEXT, copilotLook, decodeCopilot, looksOf, type CopilotOp } from '../../../run/decode'
import { APP_EDITOR_TEXT as T, LOOK_TEXT } from '../../../lib/terms'
import type { ConversationTurn } from '../../../types'
import type { AppSpec } from '../../../typesApp'
import { applyAppOp } from './appOps'
import { useEditor } from './context'

/*
 * 应用编辑器的助手（A8、L1）：只润色——布局、文案、控件选择。岗位绑定只能由人做，助手只留空并提示。
 *
 * 走 /api/copilot/app-stream：系统先代为查看固定的版本（look，by=system），模型可以再查看；应用操作（app_add 等）
 * 到一条落一条到草稿上（applyAppOp，和人在页面树上的编辑同一种动作），流结束时的 final 带服务端落地后的整份 spec，
 * 以它为准。助手问回来一句（ask）时这一轮到此结束，回答就是下一轮。过程照 decode.ts 的说法显示（查看、自查、操作）。
 *
 * 对话（D-e）：每个应用一段（kind=app、归属这个应用），进编辑器先取或建（GET /api/apps/{id}/assistant-conversation），
 * 请求带 conversation_id，服务端据此把前几轮（问题、说明、查看过什么）放进这一轮，同问数据；每轮结束落一条轮次
 * （问题、说明、助手问回来的话、meta.looks）。之前的轮次显示在上面，只有问题、查看记录和说明——当时的操作已经落进草稿，
 * 不重放。取不到对话时这一轮照常可用，只是接不上前面聊过的
 */

interface Turn {
  id: string
  question: string
  ops: CopilotOp[]
  phase: 'running' | 'done' | 'error'
  status: string
  error?: string
}

/** 落过库的一轮 → 显示用的一轮：查看记录照 decode 的说法，说明挂在「应用草稿已调整完成」那一行，问回来的话照 ask 显示 */
function pastTurn(t: ConversationTurn): StreamTurn {
  const looks: CopilotOp[] = Array.isArray(t.meta?.looks) ? t.meta!.looks : []
  const ops: CopilotOp[] = [...looks]
  if (t.status !== 'error') ops.push({ op: 'done', explanation: t.explanation || '' })
  if (t.answer) ops.push({ op: 'ask', question: t.answer })
  return {
    id: `past-${t.id}`, question: t.question, phase: t.status === 'error' ? 'error' : 'done', status: '',
    steps: decodeCopilot(ops, { context: 'app' }),
    ...(t.status === 'error' && t.error ? { error: t.error } : {}),
  }
}

export function AppAssistant() {
  const { appId, draft, setDraft, bindings } = useEditor()
  const [turns, setTurns] = useState<Turn[]>([])
  const [past, setPast] = useState<StreamTurn[]>([])
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [historyFailed, setHistoryFailed] = useState(false)
  const [text, setText] = useState('')
  const cancel = useRef<(() => void) | null>(null)
  const latest = useRef<AppSpec>(draft)
  latest.current = draft
  useEffect(() => () => cancel.current?.(), [])
  const busy = turns.some((t) => t.phase === 'running')

  // 取或建这个应用的助手对话，带着之前的轮次。老服务端只回对话本身时按 id 再取一次
  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const conv = await api.apps.assistantConversation(appId)
        const detail = Array.isArray(conv.turns) ? conv : await api.conversations.get(conv.id)
        if (!alive) return
        setConversationId(detail.id)
        setPast((detail.turns ?? []).map(pastTurn))
      } catch {
        if (alive) setHistoryFailed(true)
      }
    })()
    return () => { alive = false }
  }, [appId])

  const patch = (id: string, fn: (t: Turn) => Partial<Turn>) =>
    setTurns((ts) => ts.map((t) => (t.id === id ? { ...t, ...fn(t) } : t)))

  /** 这一轮落库：问题、说明、助手问回来的话、查看记录。失败不打扰（这一轮的改动已经在草稿上） */
  const record = (question: string, ops: CopilotOp[], error?: string) => {
    if (!conversationId) return
    const final = [...ops].reverse().find((o) => o.op === 'final')
    const done = [...ops].reverse().find((o) => o.op === 'done')
    const ask = [...ops].reverse().find((o) => o.op === 'ask')
    const looks = looksOf(ops)
    const body: Partial<ConversationTurn> = {
      explanation: String(final?.explanation ?? done?.explanation ?? ''),
      answer: ask ? String(ask.question ?? '') : '',
      status: error ? 'error' : 'done',
      error: error ?? '',
      ...(looks.length ? { meta: { looks } } : {}),
    }
    void api.conversations.startTurn(conversationId, question)
      .then((turn) => api.conversations.patchTurn(conversationId, turn.id, body))
      .catch(() => undefined)
  }

  const send = () => {
    const instruction = text.trim()
    if (!instruction || busy) return
    const id = `t${Date.now()}`
    setText('')
    setTurns((ts) => [...ts, { id, question: instruction, ops: [], phase: 'running', status: COPILOT_PHASE_TEXT.connecting }])
    let changed = false
    let failed: string | undefined
    const got: CopilotOp[] = []
    cancel.current = streamAppCopilot(
      { app_id: appId, instruction, spec: latest.current, bindings, conversation_id: conversationId },
      (op: CopilotOp) => {
        got.push(op)
        patch(id, (t) => {
          const last = t.ops[t.ops.length - 1]
          if (op.op === 'thinking' && last?.op === 'thinking') {
            return { ops: [...t.ops.slice(0, -1), { ...last, delta: String(last.delta ?? '') + String(op.delta ?? '') }] }
          }
          return { ops: [...t.ops, op] }
        })
        switch (op.op) {
          case 'heartbeat':
            patch(id, () => ({ status: op.phase === 'looking' ? LOOK_TEXT.running
              : COPILOT_PHASE_TEXT[String(op.phase)] ?? '正在处理' }))
            break
          case 'look': {
            const look = copilotLook(op)
            if (look) patch(id, () => ({ status: look.title }))
            break
          }
          case 'app_add': case 'app_update': case 'app_remove': case 'app_move': case 'app_page': case 'app_set':
            changed = true
            setDraft((s) => applyAppOp(s, op))
            break
          case 'final':
            // 服务端落地后的整份 spec 为准：边收边改的那一份可能少了被服务端拒掉、去重的操作
            if (op.spec && typeof op.spec === 'object') { changed = true; setDraft(() => op.spec as AppSpec) }
            break
          case 'error':
            failed = String(op.message ?? '生成失败')
            patch(id, () => ({ phase: 'error', error: failed }))
            break
        }
      },
      (error) => {
        cancel.current = null
        patch(id, (t) => (t.phase === 'error' ? {} : error ? { phase: 'error', error } : { phase: 'done', status: '' }))
        if (!error && changed) toast.ok(T.assistantApplied)
        record(instruction, got, failed ?? error)
      },
    )
  }

  const stream: StreamTurn[] = [
    ...past,
    ...turns.map((t) => ({
      id: t.id, question: t.question, phase: t.phase, status: t.status,
      steps: decodeCopilot(t.ops, { context: 'app' }),
      ...(t.error ? { error: t.error } : {}),
    })),
  ]

  return (
    <div className="flex h-full min-h-0 flex-col" data-app-assistant={turns.length} data-app-assistant-past={past.length}
         data-app-assistant-conversation={conversationId ?? undefined}>
      {historyFailed && (
        <p className="shrink-0 border-b px-3 py-1.5 text-2xs leading-relaxed" style={{ color: 'var(--st-waiting)' }}
           data-app-assistant-history-failed="">
          {T.assistantHistoryFailed}
        </p>
      )}
      <div className="min-h-0 flex-1">
        <AssistantStream turns={stream} dense
                         empty={<p className="p-4 text-xs leading-relaxed text-faint">{T.assistantEmpty}</p>} />
      </div>
      <div className="shrink-0 border-t p-2">
        <PromptBox value={text} onChange={setText} onSubmit={send} busy={busy} size="panel"
                   onStop={() => { cancel.current?.(); cancel.current = null }} stopLabel={T.assistantStop}
                   label={T.assistant} placeholder={T.assistantPlaceholder} />
        <p className="mt-1 text-2xs text-faint">{T.assistantHint}</p>
      </div>
    </div>
  )
}
