import { useCallback, useEffect, useState } from 'react'
import {
  AlertTriangle, Check, ChevronDown, Copy, LayoutGrid, Plus, Save, ShieldCheck,
  Trash2, Wand2,
} from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { FlowCanvas } from '../canvas/FlowCanvas'
import { Inspector } from '../canvas/Inspector'
import { Palette } from '../canvas/Palette'
import { AssistantPanel } from '../run/AssistantPanel'
import { useStudio, toGraph } from '../store/studio'
import { useCatalog } from '../store/catalog'
import { Empty, Modal, Spinner, Tabs, useToast } from '../components/ui'
import type { Workflow } from '../types'

export function StudioPage() {
  const toast = useToast()
  const { workflows, refresh } = useCatalog()
  const workflow = useStudio((s) => s.workflow)
  const dirty = useStudio((s) => s.dirty)
  const issues = useStudio((s) => s.issues)
  const selectedId = useStudio((s) => s.selectedId)
  const streaming = useStudio((s) => s.streaming)
  const { load, save, setGraph } = useStudio()

  const [tab, setTab] = useState<'inspect' | 'run'>('run')
  const [picker, setPicker] = useState(false)
  const [saving, setSaving] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const copilotActive = useStudio((s) => s.copilot.active)

  // 首次进来自动打开第一张图
  useEffect(() => {
    if (!workflow && workflows.length) load(workflows[0])
  }, [workflows, workflow, load])

  // 选中节点时自动切到属性页，省一次点击
  useEffect(() => {
    if (selectedId) setTab('inspect')
  }, [selectedId])
  // 开始跑图或开始生成时切回助手栏，否则进展发生在一个看不见的 tab 里
  useEffect(() => {
    if (streaming || copilotActive) setTab('run')
  }, [streaming, copilotActive])

  const doSave = useCallback(async () => {
    if (!workflow) return
    setSaving(true)
    try {
      await save()
      toast('已保存', 'ok')
      void refresh()
    } catch (e: any) {
      toast(e.message ?? '保存失败', 'error')
    } finally {
      setSaving(false)
    }
  }, [workflow, save, toast, refresh])

  // Cmd/Ctrl+S 保存
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 's') {
        e.preventDefault()
        void doSave()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [doSave])

  // 有未保存改动时拦一下关标签页／刷新。画布没有撤销栈，关掉就真没了。
  useEffect(() => {
    if (!dirty) return
    const guard = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', guard)
    return () => window.removeEventListener('beforeunload', guard)
  }, [dirty])

  const errorCount = issues.filter((i) => i.level === 'error').length
  const warnCount = issues.filter((i) => i.level === 'warning').length

  const relayout = async () => {
    const { nodes, edges } = useStudio.getState()
    try {
      setGraph(await api.copilot.layout(toGraph(nodes, edges)))
      toast('已重新排版', 'ok')
    } catch (e: any) {
      toast(e.message ?? '排版失败', 'error')
    }
  }

  if (!workflows.length) {
    return (
      <Empty
        title="还没有工作流"
        hint="新建一张空白图，或者用 Copilot 直接把想法生成出来。"
        action={<NewWorkflowButton onDone={refresh} />}
      />
    )
  }

  return (
    <div className="flex h-full flex-col">
      {/* 工具栏 */}
      <div className="flex shrink-0 items-center gap-2 border-b px-3 py-2">
        <button className="btn btn-ghost gap-1.5" onClick={() => setPicker(true)}>
          <span className="max-w-56 truncate font-medium">{workflow?.name ?? '选择工作流'}</span>
          <ChevronDown size={12} className="text-faint" />
        </button>

        {workflow?.status === 'governed' && (
          <span className="chip" style={{ color: 'var(--ok)', borderColor: 'var(--ok)' }}>
            受管 v{workflow.published_version}
          </span>
        )}
        {workflow?.status === 'published' && (
          <span className="chip" style={{ color: 'var(--ok)' }}>已发布 v{workflow.published_version}</span>
        )}
        {(!workflow?.status || workflow.status === 'draft') && <span className="chip">草稿</span>}
        {dirty && <span className="chip" style={{ color: 'var(--warn)' }}>未保存</span>}
        {errorCount > 0 && (
          <span className="chip" style={{ color: 'var(--err)', borderColor: 'var(--err)' }}>
            <AlertTriangle size={9} /> {errorCount} 个错误
          </span>
        )}
        {errorCount === 0 && warnCount > 0 && (
          <span className="chip" style={{ color: 'var(--warn)' }}>{warnCount} 个提示</span>
        )}
        {errorCount === 0 && warnCount === 0 && useStudio.getState().nodes.length > 0 && (
          <span className="chip" style={{ color: 'var(--ok)' }}><Check size={9} /> 可运行</span>
        )}

        <div className="flex-1" />

        {/* Copilot 不再是弹窗：它就在右栏里，这个按钮只负责把人送过去。
            弹窗的问题是每改一次图都要重开一次，而且关掉之后需求文本就没了 */}
        <button
          className="btn"
          title="用自然语言生成或修改工作流"
          onClick={() => {
            setTab('run')
            // 等这一帧渲染完输入框才在 DOM 里
            requestAnimationFrame(() =>
              window.dispatchEvent(new Event('agentlab:focus-copilot')))
          }}
        >
          <Wand2 size={12} /> Copilot
        </button>
        <button className="btn" onClick={relayout} title="自动排版"><LayoutGrid size={12} /></button>
        <button className="btn" onClick={() => setPublishing(true)} disabled={!workflow || dirty}
                title={dirty ? '先保存再发布' : '把当前版本立为正式版本，正式运行只认它'}>
          <ShieldCheck size={12} /> 发布
        </button>
        <button className="btn btn-primary" onClick={doSave} disabled={saving || !dirty}>
          {saving ? <Spinner /> : <Save size={12} />} 保存
        </button>
      </div>

      {/* 三栏 */}
      <div className="flex min-h-0 flex-1">
        <aside className="w-52 shrink-0 border-r bg-panel">
          <Palette />
        </aside>
        <main className="relative min-w-0 flex-1">
          <FlowCanvas />
        </main>
        <aside className="flex w-[360px] shrink-0 flex-col border-l bg-panel">
          <Tabs
            tabs={[{ key: 'run', label: '助手' }, { key: 'inspect', label: '属性' }]}
            active={tab}
            onChange={(k) => setTab(k as any)}
          />
          {/* 两个都挂着、用 hidden 切换，而不是三元卸载其中一个。
              助手栏里有输入框草稿、展开状态、滚动位置——点一下"属性"看个
              节点再切回来全没了，等于逼人别用属性页 */}
          <div className={clsx('min-h-0 flex-1', tab !== 'run' && 'hidden')}>
            <AssistantPanel />
          </div>
          <div className={clsx('min-h-0 flex-1 overflow-y-auto', tab !== 'inspect' && 'hidden')}>
            <Inspector />
          </div>
        </aside>
      </div>

      <WorkflowPicker open={picker} onClose={() => setPicker(false)} />
      {publishing && workflow && (
        <PublishModal workflow={workflow} onClose={() => setPublishing(false)}
                      onDone={() => { setPublishing(false); void refresh() }} />
      )}
    </div>
  )
}

// -------------------------------------------------------------------------

function NewWorkflowButton({ onDone }: { onDone: () => void }) {
  const load = useStudio((s) => s.load)
  const toast = useToast()
  return (
    <button
      className="btn btn-primary"
      onClick={async () => {
        const name = prompt('工作流名称', '新工作流')
        if (!name) return
        try {
          const created = await api.workflows.create({
            name,
            graph: {
              nodes: [
                {
                  id: 'start', type: 'input', position: { x: 100, y: 260 },
                  data: { label: '输入', config: { fields: [{ name: 'question', required: true }] } },
                },
                {
                  id: 'done', type: 'output', position: { x: 460, y: 260 },
                  data: { label: '成果', config: { fields: [{ name: '结果', value: '{{ last_message }}' }] } },
                },
              ],
              edges: [{ source: 'start', target: 'done' }],
            },
          })
          load(created)
          onDone()
          toast('已创建', 'ok')
        } catch (e: any) {
          toast(e.message ?? '创建失败', 'error')
        }
      }}
    >
      <Plus size={12} /> 新建工作流
    </button>
  )
}

function WorkflowPicker({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { workflows, refresh } = useCatalog()
  const load = useStudio((s) => s.load)
  const current = useStudio((s) => s.workflow)
  const toast = useToast()

  const act = async (fn: () => Promise<any>, msg: string) => {
    try {
      await fn()
      await refresh()
      toast(msg, 'ok')
    } catch (e: any) {
      toast(e.message ?? '操作失败', 'error')
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="工作流" width={680}
           footer={<NewWorkflowButton onDone={() => { void refresh(); onClose() }} />}>
      <div className="space-y-1">
        {workflows.map((w: Workflow) => (
          <div
            key={w.id}
            className={clsx(
              'group flex cursor-pointer items-center gap-3 rounded-lg border px-3 py-2 hover:bg-hover',
              current?.id === w.id && 'border-[var(--accent)]',
            )}
            onClick={() => {
              // 切换会把当前画布整个换掉。没存过的改动就这么没了，
              // 而且全局没有撤销——至少问一句
              if (
                useStudio.getState().dirty &&
                !confirm('当前画布有未保存的改动，切换后会丢失。确定要切换吗？')
              ) {
                return
              }
              load(w)
              onClose()
            }}
          >
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="truncate text-[12.5px] font-medium">{w.name}</span>
                {w.is_template && <span className="chip">模板</span>}
                {w.tags?.map((t) => <span key={t} className="chip">{t}</span>)}
              </div>
              <div className="truncate text-[11px] text-faint">{w.description}</div>
            </div>
            <span className="shrink-0 text-[10px] text-faint">
              {w.graph?.nodes?.length ?? 0} 节点 · v{w.version}
              {w.run_count ? ` · 跑过 ${w.run_count} 次` : ''}
            </span>
            <button
              className="btn btn-ghost btn-sm opacity-0 group-hover:opacity-100"
              title="复制一份"
              onClick={(e) => { e.stopPropagation(); void act(() => api.workflows.duplicate(w.id), '已复制') }}
            >
              <Copy size={11} />
            </button>
            <button
              className="btn btn-ghost btn-sm opacity-0 group-hover:opacity-100"
              title="删除"
              onClick={(e) => {
                e.stopPropagation()
                if (confirm(`删除「${w.name}」？运行记录也会一并删除。`)) {
                  void act(() => api.workflows.remove(w.id), '已删除')
                }
              }}
            >
              <Trash2 size={11} className="text-[var(--err)]" />
            </button>
          </div>
        ))}
      </div>
    </Modal>
  )
}

// -------------------------------------------------------------------------

function PublishModal({ workflow, onClose, onDone }: {
  workflow: Workflow; onClose: () => void; onDone: () => void
}) {
  const toast = useToast()
  const load = useStudio((s) => s.load)
  const [level, setLevel] = useState<'published' | 'governed'>('published')
  const [busy, setBusy] = useState(false)
  const [issues, setIssues] = useState<any[] | null>(null)

  const publish = async () => {
    setBusy(true)
    setIssues(null)
    try {
      const res = await api.workflows.publish(workflow.id, level)
      setIssues(res.issues ?? [])
      if (res.ok) {
        toast(`已发布 v${res.version}（${level === 'governed' ? '受管' : '正式'}）`, 'ok')
        const fresh = await api.workflows.get(workflow.id)
        load(fresh)
        onDone()
      } else {
        toast('发布被门禁拦下，看问题列表', 'error')
      }
    } catch (e: any) {
      toast(e.message ?? '发布失败', 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open onClose={onClose} title={`发布「${workflow.name}」v${workflow.version}`}
           footer={<>
             <button className="btn" onClick={onClose}>取消</button>
             <button className="btn btn-primary" onClick={publish} disabled={busy}>
               {busy ? <Spinner /> : <ShieldCheck size={12} />} 发布
             </button>
           </>}>
      <div className="mb-3 grid grid-cols-2 gap-1.5">
        <button onClick={() => setLevel('published')}
                className={level === 'published' ? 'rounded-lg border border-[var(--accent)] px-3 py-2 text-left' : 'rounded-lg border px-3 py-2 text-left hover:bg-hover'}>
          <div className="text-[12px] font-medium">正式（published）</div>
          <div className="text-[10.5px] text-faint">基础校验通过即可；正式运行从此版本发起</div>
        </button>
        <button onClick={() => setLevel('governed')}
                className={level === 'governed' ? 'rounded-lg border border-[var(--accent)] px-3 py-2 text-left' : 'rounded-lg border px-3 py-2 text-left hover:bg-hover'}>
          <div className="text-[12px] font-medium">受管（governed）</div>
          <div className="text-[10.5px] text-faint">出具级门禁：禁 supervisor、方法卡必须钉版、必须有出具契约</div>
        </button>
      </div>
      <div className="text-[10.5px] leading-relaxed text-faint">
        发布把当前版本立为不可变的正式版本。之后画布上继续改不影响它——正式运行永远执行发布时的快照。
      </div>
      {issues && !!issues.length && (
        <div className="mt-3 space-y-1">
          {issues.map((issue, i) => (
            <div key={i} className="text-[10.5px]"
                 style={{ color: issue.level === 'error' ? 'var(--err)' : 'var(--warn)' }}>
              {issue.level === 'error' ? '✕' : '!'} {issue.message}
            </div>
          ))}
        </div>
      )}
    </Modal>
  )
}

// CopilotModal 和 CopilotStatusBar 都去掉了，搬进 run/AssistantPanel.tsx 的
// 右栏里。弹窗的问题不是样式：每改一次图都要重开一次，关掉之后需求文本就没了，
// 想微调只能重打一遍；而浮条盖在画布左下角（z-30），和 Toast（z-100）、
// Modal（z-50）三层各自为政，本身也是重复的一份 Copilot 状态渲染。
