import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { AlertTriangle, Check, CornerDownRight, PenLine, ShieldCheck, XCircle } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { diffGraphs, toFlow, useStudio, type GraphDiff } from '../store/studio'
import { Modal, Spinner, toast } from '../components/ui'
import { WORKFLOW_STATUS_HINT } from '../lib/terms'
import type { Workflow } from '../types'

type Level = 'published' | 'governed'

const LEVEL_DETAIL: Record<Level, string> = {
  published: '基础校验通过即可。正式运行从这一版发起，之后画布上继续改不影响它',
  governed: '另过治理门禁：不许有「多 Agent 协作」这类全动态规划节点；子工作流要钉住版本；'
    + '至少一个「成果」节点声明出具契约；Agent 的危险工具至少要人工审批',
}

/** 署名只存在这台浏览器里（和请求头 X-Actor 是同一份） */
const ACTOR_KEY = 'agentlab_actor'
const readActor = () => {
  try { return localStorage.getItem(ACTOR_KEY)?.trim() ?? '' } catch { return '' }
}

/**
 * 发布弹窗。
 *
 * 等级的初值跟着工作流现在的等级走：以前写死「已发布」，对一张受管工作流直接点
 * 「发布」，后端照 level 改写状态，它就被悄悄降了级。选中态用底色 + 勾，不只靠一圈
 * 1px 描边。发布是要留痕的动作，所以写明以谁的名义发布；没署名就就地填。
 * 门禁拦下的问题一条一行、前置节点名，点一下关掉弹窗、定位到那个节点。
 */
export function PublishDialog({ workflow, onClose, onDone, onLocate }: {
  // published_by 后端已经给了，types.ts 还没跟上
  workflow: Workflow & { published_by?: string | null }
  onClose: () => void
  onDone: () => void
  onLocate: (nodeId: string) => void
}) {
  const load = useStudio((s) => s.load)
  const nodes = useStudio((s) => s.nodes)
  const [level, setLevel] = useState<Level>(workflow.status === 'governed' ? 'governed' : 'published')
  const [busy, setBusy] = useState(false)
  const [issues, setIssues] = useState<any[] | null>(null)
  const [actor, setActor] = useState(readActor)
  const [signing, setSigning] = useState('')
  const [delta, setDelta] = useState<GraphDiff | null>(null)
  const published = workflow.published_version
  const downgrade = workflow.status === 'governed' && level === 'published'

  // 和上一个已发布版比一比：发布之前知道这次到底改了什么
  useEffect(() => {
    if (published == null || published === workflow.version) return
    let alive = true
    api.workflows.version(workflow.id, published).then((v) => {
      if (!alive || !v.graph) return
      setDelta(diffGraphs(toFlow(v.graph), toFlow(workflow.graph)))
    }).catch(() => undefined)
    return () => { alive = false }
  }, [workflow.id, workflow.version, workflow.graph, published])

  const sign = () => {
    const name = signing.trim()
    if (!name) return
    try { localStorage.setItem(ACTOR_KEY, name) } catch { /* 隐私模式：只这一次有效 */ }
    setActor(name)
  }

  const publish = async () => {
    setBusy(true)
    setIssues(null)
    try {
      const res = await api.workflows.publish(workflow.id, level)
      setIssues(res.issues ?? [])
      if (res.ok) {
        toast.ok(`已发布 v${res.version}${level === 'governed' ? ' · 受管' : ''}${actor ? ` · ${actor}` : ''}`)
        const fresh = await api.workflows.get(workflow.id)
        // 发布不动图、不升版本，只换状态和发布人：换掉工作流的元信息就够了。走 load 的话
        // 撤销栈、助手的轮次、运行态全被清空，镜头还要重新取景——发布一下什么都没了。
        // 版本对不上（这期间别处又存过）才整张重载，画布这时没有未保存的改动（有就发不了）
        if (fresh.version === useStudio.getState().workflow?.version) useStudio.setState({ workflow: fresh })
        else load(fresh)
        onDone()
      } else {
        toast.warn('发布被门禁拦下：下面列了要改的地方，点一条定位到节点')
      }
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  const labelOf = (id?: string | null) => (id ? nodes.find((n) => n.id === id)?.data.label || id : '')
  const errors = (issues ?? []).filter((i) => i.level === 'error')

  return (
    <Modal open onClose={onClose} title={`发布「${workflow.name}」v${workflow.version}`} width={560}
           footer={<>
             <button className="btn" onClick={onClose}>取消</button>
             <button className="btn btn-primary" onClick={publish} disabled={busy} data-autofocus="">
               {busy ? <Spinner size={12} /> : <ShieldCheck size={12} />}
               {level === 'governed' ? '发布为受管' : '发布'}
             </button>
           </>}>
      <div className="mb-3 grid grid-cols-2 gap-2" role="radiogroup" aria-label="发布等级">
        {(['published', 'governed'] as Level[]).map((l) => {
          const on = level === l
          return (
            <button
              key={l}
              type="button"
              role="radio"
              aria-checked={on}
              onClick={() => setLevel(l)}
              // 两张卡说明长短不一，grid 把它们拉成一样高；按钮默认把内容竖直居中，标题就对不齐了
              className={clsx('relative flex flex-col justify-start rounded-lg border px-3 py-2.5 text-left transition-colors',
                on ? 'border-[var(--accent)] bg-accent-soft' : 'hover:bg-hover')}
            >
              {on && (
                <span className="absolute right-2 top-2 flex h-4 w-4 items-center justify-center rounded-full bg-accent-solid text-on-accent">
                  <Check size={10} strokeWidth={3} />
                </span>
              )}
              <div className="flex items-center gap-1.5 pr-5 text-xs font-semibold">
                <span className={clsx('flex h-3 w-3 shrink-0 items-center justify-center rounded-full border',
                  on && 'border-[var(--accent)]')}>
                  {on && <span className="h-1.5 w-1.5 rounded-full" style={{ background: 'var(--accent)' }} />}
                </span>
                {WORKFLOW_STATUS_HINT[l]}
              </div>
              <div className="mt-1 text-2xs leading-relaxed text-faint">{LEVEL_DETAIL[l]}</div>
            </button>
          )
        })}
      </div>

      {downgrade && (
        <div className="mb-3 flex items-start gap-1.5 rounded-md border px-2.5 py-2 text-2xs leading-relaxed"
             style={{ borderColor: 'var(--warn)', color: 'var(--warn)', background: 'var(--st-waiting-soft)' }}>
          <AlertTriangle size={12} className="mt-px shrink-0" />
          它现在是受管工作流。发布为「已发布」会降级：以后的正式运行不再受治理门禁约束。
        </div>
      )}

      <dl className="space-y-1.5 rounded-md border px-3 py-2 text-2xs">
        <div className="flex gap-2">
          <dt className="w-14 shrink-0 text-faint">这一版</dt>
          <dd className="min-w-0 flex-1">
            v{workflow.version}
            {published != null && (
              <span className="text-faint">
                {published === workflow.version ? ' · 就是当前已发布的这一版（重新发布只改等级）'
                  : delta ? ` · 比已发布的 v${published}：${describe(delta)}`
                  : ` · 已发布的是 v${published}`}
              </span>
            )}
            {workflow.published_by && published != null && (
              <span className="block text-faint">上次由「{workflow.published_by}」发布 v{published}</span>
            )}
          </dd>
        </div>
        <div className="flex items-start gap-2">
          <dt className="w-14 shrink-0 pt-px text-faint">署名</dt>
          <dd className="min-w-0 flex-1">
            {actor ? (
              <span>将以「<b className="font-semibold">{actor}</b>」的名义发布，写进发布记录
                <Link to="/settings/prefs" className="ml-1.5 text-faint underline-offset-2 hover:underline">改署名</Link>
              </span>
            ) : (
              <div>
                <div style={{ color: 'var(--warn)' }}>未署名：这次发布不会记录发布人</div>
                <div className="mt-1 flex items-center gap-1.5">
                  <input className="field h-7 py-0 text-xs" placeholder="写上你的名字，例如 张工" value={signing}
                         aria-label="署名" onChange={(e) => setSigning(e.target.value)}
                         onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); sign() } }} />
                  <button type="button" className="btn btn-sm shrink-0" disabled={!signing.trim()} onClick={sign}>
                    <PenLine size={11} /> 署名
                  </button>
                </div>
                <div className="mt-1 text-faint">只存在这台浏览器里，和审批、正式运行共用；也可以去「设置 · 偏好设置」改</div>
              </div>
            )}
          </dd>
        </div>
      </dl>

      {issues && !!issues.length && (
        <div className="mt-3">
          <div className="mb-1 text-2xs font-medium" style={{ color: errors.length ? 'var(--err)' : 'var(--warn)' }}>
            {errors.length ? `门禁拦下了 ${errors.length} 处，改完再发布` : '已发布，另有这些提示'}
          </div>
          <ul className="max-h-48 space-y-0.5 overflow-y-auto">
            {issues.map((issue, i) => {
              const err = issue.level === 'error'
              const Icon = err ? XCircle : AlertTriangle
              const where = labelOf(issue.node_id)
              return (
                <li key={i}>
                  <button
                    type="button"
                    disabled={!issue.node_id}
                    onClick={() => { onLocate(issue.node_id); onClose() }}
                    className={clsx('flex w-full items-start gap-1.5 rounded px-1.5 py-1 text-left text-2xs leading-snug',
                      issue.node_id && 'hover:bg-hover')}
                    title={issue.node_id ? '定位到这个节点' : undefined}
                  >
                    <Icon size={11} className="mt-px shrink-0" style={{ color: err ? 'var(--err)' : 'var(--warn)' }} />
                    <span className="min-w-0 flex-1">
                      {where && !String(issue.message).includes(`「${where}」`) && <b className="font-semibold">「{where}」</b>}
                      {issue.message}
                    </span>
                    {issue.node_id && <CornerDownRight size={10} className="mt-px shrink-0 text-faint" />}
                  </button>
                </li>
              )
            })}
          </ul>
        </div>
      )}
    </Modal>
  )
}

function describe(d: GraphDiff): string {
  if (!d.total) return '只挪了位置'
  const parts = [
    d.added.length && `新增 ${d.added.length} 个节点`,
    d.removed.length && `删掉 ${d.removed.length} 个节点`,
    d.changed.length && `改了 ${d.changed.length} 个节点的配置`,
    (d.edgesAdded || d.edgesRemoved) && `连线变动 ${d.edgesAdded + d.edgesRemoved} 处`,
  ].filter(Boolean)
  return parts.join('、')
}
