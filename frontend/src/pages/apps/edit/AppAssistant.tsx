import { useEffect, useRef, useState } from 'react'
import { streamAppCopilot } from '../../../api/appCopilot'
import { toast } from '../../../components/ui'
import { AssistantStream, type StreamTurn } from '../../../run/AssistantStream'
import { PromptBox } from '../../../run/Composer'
import { COPILOT_PHASE_TEXT, copilotLook, decodeCopilot, type CopilotOp } from '../../../run/decode'
import { APP_EDITOR_TEXT as T, LOOK_TEXT } from '../../../lib/terms'
import type { AppSpec } from '../../../typesApp'
import { applyAppOp } from './appOps'
import { useEditor } from './context'

/*
 * 应用编辑器的助手（A8、L1）：只润色——布局、文案、控件选择。岗位绑定只能由人做，助手只留空并提示。
 *
 * 走 /api/copilot/app-stream：系统先代为查看固定的版本（look，by=system），模型可以再查看；应用操作（app_add 等）
 * 到一条落一条到草稿上（applyAppOp，和人在页面树上的编辑同一种动作），流结束时的 final 带服务端落地后的整份 spec，
 * 以它为准。助手问回来一句（ask）时这一轮到此结束，回答就是下一轮。过程照 decode.ts 的说法显示（查看、自查、操作）
 */

interface Turn {
  id: string
  question: string
  ops: CopilotOp[]
  phase: 'running' | 'done' | 'error'
  status: string
  error?: string
}

export function AppAssistant() {
  const { appId, draft, setDraft, bindings } = useEditor()
  const [turns, setTurns] = useState<Turn[]>([])
  const [text, setText] = useState('')
  const cancel = useRef<(() => void) | null>(null)
  const latest = useRef<AppSpec>(draft)
  latest.current = draft
  useEffect(() => () => cancel.current?.(), [])
  const busy = turns.some((t) => t.phase === 'running')

  const patch = (id: string, fn: (t: Turn) => Partial<Turn>) =>
    setTurns((ts) => ts.map((t) => (t.id === id ? { ...t, ...fn(t) } : t)))

  const send = () => {
    const instruction = text.trim()
    if (!instruction || busy) return
    const id = `t${Date.now()}`
    setText('')
    setTurns((ts) => [...ts, { id, question: instruction, ops: [], phase: 'running', status: COPILOT_PHASE_TEXT.connecting }])
    let changed = false
    cancel.current = streamAppCopilot(
      { app_id: appId, instruction, spec: latest.current, bindings },
      (op: CopilotOp) => {
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
            patch(id, () => ({ phase: 'error', error: String(op.message ?? '生成失败') }))
            break
        }
      },
      (error) => {
        cancel.current = null
        patch(id, (t) => (t.phase === 'error' ? {} : error ? { phase: 'error', error } : { phase: 'done', status: '' }))
        if (!error && changed) toast.ok(T.assistantApplied)
      },
    )
  }

  const stream: StreamTurn[] = turns.map((t) => ({
    id: t.id, question: t.question, phase: t.phase, status: t.status,
    steps: decodeCopilot(t.ops, { context: 'app' }),
    ...(t.error ? { error: t.error } : {}),
  }))

  return (
    <div className="flex h-full min-h-0 flex-col" data-app-assistant={turns.length}>
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
