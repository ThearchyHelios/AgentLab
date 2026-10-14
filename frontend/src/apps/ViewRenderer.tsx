/**
 * 视图渲染器：按服务端解析好的视图模型（agentlab.view/1）渲染应用页。
 *
 * 前端只有一张「组件名 → 渲染器」的表（apps/components）；不认识的组件显示「此组件需要更新前端」，不报错、不空白。
 * 服务端解析失败的组件照实列在页面顶上（problems），其余照常显示（P1）。不可见的组件服务端不下发，这里没有任何可见性
 * 判断；数都是服务端渲染好的，这里没有任何求值和算术（P7）。
 *
 * 应用编辑器的预览（FE-6）也用它：传按草稿解析出的视图模型、preview 为真——不发起运行、不提交表单，点数字照样能看出处。
 * 一个组件画坏了只坏它自己（每个组件各包一层错误边界），不连累整页。
 */
import { useCallback, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { AlertTriangle, PackageX } from 'lucide-react'
import { ErrorBoundary } from '../components/ui'
import { APP_TEXT, VIEW_TEXT } from '../lib/termsApp'
import type { ViewEvidence, ViewModel, ViewNode } from '../typesApp'
import { ViewCtx, type ViewCtxValue } from './context'
import { RENDERERS } from './components'
import { EvidenceDrawer, type EvidenceTarget } from './EvidenceDrawer'

export interface ViewRendererProps {
  model: ViewModel
  appId: string
  /** 实例页传入：组件里的待办、动作、进度按这个实例解析 */
  runId?: string
  /** 预览：不发起运行、不提交表单，点数字照样能看出处 */
  preview?: boolean
  /** 页内跳转（页签、页面链接） */
  onNavigate?: (pageId: string) => void
  /** 视图刷新、实例发起之后要重新取视图模型 */
  onReload?: () => void
}

export function ViewRenderer({ model, appId, runId, preview = false, onNavigate, onReload }: ViewRendererProps) {
  const [evidence, setEvidence] = useState<EvidenceTarget | null>(null)
  const openEvidence = useCallback((ev: ViewEvidence, label: string, text?: string) => {
    setEvidence({ evidence: ev, label, text })
  }, [])
  // 视图模型换了（刷新、重新取）就是新的一代：画坏了的组件跟着重试一次
  const gen = useMemo(() => String(++generation), [model])
  const render = useCallback((nodes: ViewNode[] | undefined): ReactNode =>
    (nodes ?? []).map((n) => <Node key={n.id} node={n} gen={gen} />), [gen])
  const ctx = useMemo<ViewCtxValue>(() => ({ model, appId, runId, preview, onNavigate, onReload, render, openEvidence }),
    [model, appId, runId, preview, onNavigate, onReload, render, openEvidence])
  const problems = model.problems ?? []

  return (
    <ViewCtx.Provider value={ctx}>
      <div className="space-y-3" data-view-renderer={preview ? 'preview' : 'live'}>
        {problems.length > 0 && (
          <div role="alert" className="rounded-lg border px-3 py-2 text-xs leading-relaxed" data-view-problems={problems.length}
               style={{ borderColor: 'color-mix(in srgb, var(--warn) 40%, var(--border))', background: 'color-mix(in srgb, var(--warn) 7%, transparent)' }}>
            <div className="flex items-center gap-1.5 font-medium" style={{ color: 'var(--warn)' }}>
              <AlertTriangle size={13} aria-hidden /> {APP_TEXT.problems(problems.length)}
            </div>
            <div className="mt-0.5 text-dim">{APP_TEXT.problemsHint}</div>
            <ul className="mt-1 list-disc space-y-0.5 pl-4 text-fg">
              {problems.map((p, i) => (
                <li key={i} data-problem={p.code}>
                  <span className="mono text-dim">{p.component}</span>：{p.message}
                </li>
              ))}
            </ul>
          </div>
        )}
        {render(model.tree)}
      </div>
      <EvidenceDrawer target={evidence} onClose={() => setEvidence(null)} />
    </ViewCtx.Provider>
  )
}

let generation = 0

function Node({ node, gen }: { node: ViewNode; gen: string }) {
  const Renderer = RENDERERS[node.type]
  if (!Renderer) return <Unknown node={node} />
  return (
    <ErrorBoundary scope="block" resetKey={gen}>
      <div data-node={node.id} className="min-w-0">
        <Renderer node={{ ...node, props: node.props ?? {} }} />
      </div>
    </ErrorBoundary>
  )
}

function Unknown({ node }: { node: ViewNode }) {
  return (
    <div className="flex items-center gap-2 rounded-lg border border-dashed px-3 py-2 text-xs text-dim"
         data-unknown-component={node.type} data-node={node.id}>
      <PackageX size={14} aria-hidden className="shrink-0 text-faint" />
      {VIEW_TEXT.unknownComponent(node.type)}
    </div>
  )
}
