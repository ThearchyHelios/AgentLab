import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AlertTriangle, Check, ChevronDown, Copy, LayoutGrid, Plus, Save, Sparkles, Trash2, Wand2,
} from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { FlowCanvas } from '../canvas/FlowCanvas'
import { Inspector } from '../canvas/Inspector'
import { Palette } from '../canvas/Palette'
import { RunPanel } from '../run/RunPanel'
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
  const [copilot, setCopilot] = useState(false)
  const [saving, setSaving] = useState(false)

  // 首次进来自动打开第一张图
  useEffect(() => {
    if (!workflow && workflows.length) load(workflows[0])
  }, [workflows, workflow, load])

  // 选中节点时自动切到属性页，省一次点击
  useEffect(() => {
    if (selectedId) setTab('inspect')
  }, [selectedId])
  useEffect(() => {
    if (streaming) setTab('run')
  }, [streaming])

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

        <button className="btn" onClick={() => setCopilot(true)} title="用自然语言生成或修改工作流">
          <Wand2 size={12} /> Copilot
        </button>
        <button className="btn" onClick={relayout} title="自动排版"><LayoutGrid size={12} /></button>
        <button className="btn btn-primary" onClick={doSave} disabled={saving || !dirty}>
          {saving ? <Spinner /> : <Save size={12} />} 保存
        </button>
      </div>

      {/* 三栏 */}
      <div className="flex min-h-0 flex-1">
        <aside className="w-52 shrink-0 border-r bg-panel">
          <Palette />
        </aside>
        <main className="min-w-0 flex-1">
          <FlowCanvas />
        </main>
        <aside className="flex w-[360px] shrink-0 flex-col border-l bg-panel">
          <Tabs
            tabs={[{ key: 'run', label: '运行' }, { key: 'inspect', label: '属性' }]}
            active={tab}
            onChange={(k) => setTab(k as any)}
          />
          <div className="min-h-0 flex-1">
            {tab === 'inspect' ? <Inspector /> : <RunPanel />}
          </div>
        </aside>
      </div>

      <WorkflowPicker open={picker} onClose={() => setPicker(false)} />
      <CopilotModal open={copilot} onClose={() => setCopilot(false)} />
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
            onClick={() => { load(w); onClose() }}
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

function CopilotModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const toast = useToast()
  const nodes = useStudio((s) => s.nodes)
  const edges = useStudio((s) => s.edges)
  const setGraph = useStudio((s) => s.setGraph)
  const [instruction, setInstruction] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ explanation: string; issues: any[] } | null>(null)
  const [useBase, setUseBase] = useState(true)

  const generate = async () => {
    if (!instruction.trim()) return
    setBusy(true)
    setResult(null)
    try {
      const res = await api.copilot.generate({
        instruction,
        base_graph: useBase && nodes.length ? toGraph(nodes, edges) : null,
      })
      setGraph(res.graph)
      setResult({ explanation: res.explanation, issues: res.issues })
      toast('已生成，记得检查后保存', 'ok')
    } catch (e: any) {
      toast(e.message ?? '生成失败', 'error')
    } finally {
      setBusy(false)
    }
  }

  const examples = [
    '读取用户上传的问题，先查知识库，查到就基于资料回答并标注出处，查不到就联网搜索',
    '把一段长文本拆成要点，逐条用模型打分，低分的让模型重写一次，最后汇总成表格',
    '写代码分析数据，在沙箱里跑，出错就把报错喂回去让模型修，最多修三次',
  ]

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={<span className="flex items-center gap-1.5"><Sparkles size={14} /> Copilot 生成工作流</span>}
      width={620}
      footer={
        <>
          <button className="btn" onClick={onClose}>关闭</button>
          <button className="btn btn-primary" onClick={generate} disabled={busy || !instruction.trim()}>
            {busy ? <Spinner /> : <Wand2 size={12} />} 生成到画布
          </button>
        </>
      }
    >
      <label className="label">描述你想要的工作流</label>
      <textarea
        className="field"
        rows={4}
        value={instruction}
        onChange={(e) => setInstruction(e.target.value)}
        placeholder="用大白话说清楚：输入是什么、中间要做哪几步、遇到什么情况走不同分支、最后要什么结果"
      />

      <div className="mt-2 flex flex-wrap gap-1.5">
        {examples.map((ex) => (
          <button key={ex} className="chip hover:border-[var(--accent)]" onClick={() => setInstruction(ex)}>
            {ex.slice(0, 26)}…
          </button>
        ))}
      </div>

      {!!nodes.length && (
        <label className="mt-3 flex items-center gap-2 text-[11.5px]">
          <input type="checkbox" checked={useBase} onChange={(e) => setUseBase(e.target.checked)}
                 className="accent-[var(--accent)]" />
          在当前这张图的基础上修改（不勾则重新生成）
        </label>
      )}

      <div className="mt-2 text-[10.5px] leading-relaxed text-faint">
        生成结果会直接铺到画布上并自动排版，但<b>不会自动保存</b>——先看一遍再决定。
      </div>

      {result && (
        <div className="mt-3 border-t pt-3">
          <div className="label">Copilot 说明</div>
          <div className="whitespace-pre-wrap rounded border bg-bg p-2 text-[11.5px] leading-relaxed">
            {result.explanation || '（无）'}
          </div>
          {!!result.issues.length && (
            <div className="mt-2 space-y-1">
              {result.issues.map((issue, i) => (
                <div key={i} className="text-[10.5px]"
                     style={{ color: issue.level === 'error' ? 'var(--err)' : 'var(--warn)' }}>
                  {issue.level === 'error' ? '✕' : '!'} {issue.message}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </Modal>
  )
}
