import { useState } from 'react'
import { AlertTriangle, Sparkles } from 'lucide-react'
import { ApiError, api } from '../../api/client'
import type { AiPreview, Staging } from '../../types'
import { Modal, Spinner } from '../../components/ui'
import { errorMessage } from '../../lib/errors'
import { formatCost, formatNumber } from '../../lib/format'
import { RECIPE_TEXT } from '../../lib/terms'

// ===========================================================================
// AI 兜底起草：规则起草不完整时，经用户同意把表格结构（不含数字的值）发给模型起草配方。
// 先取「将要发送的全文」给人看，同意才发；发的时候带上那份全文的 sha256，服务端重算对不上就拒绝
// ===========================================================================

/**
 * 卡片区顶部的 AI 入口和结果。只在服务端说 offered 时出现（首次导入、规则草稿不完整）；
 * 模型接入不可用时按钮置灰、写出原因。staging_closed 之类要关掉向导的错误交给 onError
 */
export function AiDraftOffer({ staging, busy, setBusy, onStaging, onError }: {
  staging: Staging
  busy: boolean
  setBusy: (v: boolean) => void
  onStaging: (s: Staging) => void
  /** 返回 true 表示已经处理（比如这次导入已结束、向导关了），这里不再显示 */
  onError: (e: unknown) => boolean
}) {
  const [phase, setPhase] = useState<'idle' | 'previewing' | 'consent' | 'drafting'>('idle')
  const [preview, setPreview] = useState<AiPreview | null>(null)
  const [error, setError] = useState<{ message: string; next?: string } | null>(null)
  const ai = staging.ai
  const result = staging.ai_draft

  const fail = (e: unknown) => {
    if (onError(e)) return
    const code = e instanceof ApiError ? e.code : undefined
    // 服务端的原话照写。ai_failed、ai_too_large 的原话里已经带了下一步（有没有工作配方，能做的事不一样：
    // 没有时配方面板里只能粘贴一份），界面不再另补；过期补一句重新查看
    if (code === 'ai_preview_stale') setError({ message: errorMessage(e), next: RECIPE_TEXT.aiStale })
    else setError({ message: errorMessage(e) })
  }

  const open = async () => {
    setError(null)
    setPhase('previewing')
    setBusy(true)
    try {
      setPreview(await api.tableImports.draftAiPreview(staging.id))
      setPhase('consent')
    } catch (e) {
      setPhase('idle')
      fail(e)
    } finally {
      setBusy(false)
    }
  }

  const send = async () => {
    if (!preview) return
    setPhase('drafting')
    setBusy(true)
    try {
      onStaging(await api.tableImports.draftAi(staging.id, preview.sha256))
      setPreview(null)
      setPhase('idle')
    } catch (e) {
      setPreview(null)
      setPhase('idle')
      fail(e)
    } finally {
      setBusy(false)
    }
  }

  const usage = result && result.draft && result.total_tokens != null
    ? RECIPE_TEXT.aiDone(formatNumber(result.total_tokens), formatCost(result.cost_usd ?? 0))
    : null

  if (!ai?.offered && !usage) return null
  return (
    <div className="space-y-1.5">
      {ai?.offered && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2" data-ai-offer
             style={{ borderColor: 'color-mix(in srgb, var(--copilot) 40%, var(--border))', background: 'color-mix(in srgb, var(--copilot) 6%, transparent)' }}>
          <button type="button" className="btn btn-sm" disabled={busy || !ai.available} onClick={() => void open()}
                  aria-describedby={!ai.available ? 'ai-unavailable' : undefined}>
            {phase === 'previewing' || phase === 'drafting'
              ? <Spinner size={11} />
              : <Sparkles size={12} className="text-[var(--copilot)]" aria-hidden />}
            {phase === 'drafting' ? RECIPE_TEXT.aiDrafting : phase === 'previewing' ? RECIPE_TEXT.aiPreviewing : RECIPE_TEXT.aiOffer}
          </button>
          {!ai.available && (
            <span id="ai-unavailable" className="min-w-0 flex-1 text-2xs leading-relaxed text-faint" data-ai-unavailable>
              {ai.reason || RECIPE_TEXT.aiUnavailable}
            </span>
          )}
        </div>
      )}
      {usage && <p className="text-2xs text-dim" data-ai-usage>{usage}</p>}
      {error && (
        <div role="alert" className="flex gap-2 rounded-lg border px-3 py-2 text-xs leading-relaxed" data-ai-error
             style={{ borderColor: 'color-mix(in srgb, var(--err) 35%, var(--border))', background: 'color-mix(in srgb, var(--err) 6%, transparent)' }}>
          <AlertTriangle size={13} className="mt-0.5 shrink-0 text-[var(--err)]" aria-hidden />
          <span>{error.message}{error.next && <span className="text-dim">（{error.next}）</span>}</span>
        </div>
      )}
      {phase === 'consent' && preview && (
        <AiConsentDialog preview={preview} model={preview.model || ai?.model || ''} busy={busy}
                         onCancel={() => { setPreview(null); setPhase('idle') }} onAgree={() => void send()} />
      )}
    </div>
  )
}

/** 同意框：说清发什么、不发什么，下面是将要发送的全文（等宽、可滚动）。取消不发任何请求 */
function AiConsentDialog({ preview, model, busy, onCancel, onAgree }: {
  preview: AiPreview; model: string; busy: boolean; onCancel: () => void; onAgree: () => void
}) {
  return (
    <Modal open onClose={onCancel} width={720} title={RECIPE_TEXT.aiConsentTitle}
           footer={
             <>
               <button className="btn" onClick={onCancel} data-autofocus>{RECIPE_TEXT.cancel}</button>
               <button className="btn btn-primary" disabled={busy} onClick={onAgree}>{RECIPE_TEXT.aiAgree}</button>
             </>
           }>
      <div className="space-y-2.5" data-ai-consent>
        <p className="text-xs leading-relaxed text-dim">{RECIPE_TEXT.aiConsentLead(model)}</p>
        <p className="text-xs font-medium leading-relaxed">{RECIPE_TEXT.aiConsentNot}</p>
        <p className="text-xs leading-relaxed text-dim">{RECIPE_TEXT.aiConsentAfter}</p>
        <div className="label">{RECIPE_TEXT.aiPreviewLabel(formatNumber(preview.chars ?? preview.text.length))}</div>
        <pre className="mono max-h-80 overflow-auto whitespace-pre-wrap rounded-lg border bg-bg p-2.5 text-2xs leading-relaxed" data-ai-preview
             tabIndex={0}>{preview.text}</pre>
      </div>
    </Modal>
  )
}
