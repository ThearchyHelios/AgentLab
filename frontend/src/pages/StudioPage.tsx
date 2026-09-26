import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  AlertTriangle, Check, ChevronDown, History, LayoutGrid, PanelRightClose, PanelRightOpen, Plus,
  Redo2, RotateCw, Save, ShieldCheck, Square, Undo2, Variable, Wand2, XCircle,
} from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { FlowCanvas } from '../canvas/FlowCanvas'
import { Palette, PALETTE_SEARCH_ID } from '../canvas/Palette'
import { InspectorSheet } from '../canvas/InspectorSheet'
import { CanvasDock, type DockTab } from '../canvas/CanvasDock'
import { LineageLayer } from '../canvas/LineageLayer'
import { VersionsSheet } from '../canvas/VersionsSheet'
import { PublishDialog } from '../canvas/PublishDialog'
import { WorkflowPicker, confirmDiscard, createWorkflow } from '../canvas/WorkflowPicker'
import { problemsOf, type Problem } from '../canvas/issues'
import { STUDIO_SHORTCUTS, hintOf, type StudioShortcutId } from '../canvas/shortcuts'
import { AssistantPanel } from '../run/AssistantPanel'
import { RunControl } from '../run/RunControl'
import { useRunClock } from '../run/useRunClock'
import { topology } from '../run/derive'
import { useStudio, toGraph } from '../store/studio'
import { useCatalog, useOnReconnect } from '../store/catalog'
import {
  EmptyState, ErrorState, IconButton, Spinner, isComposing, promptDialog, toast,
} from '../components/ui'
import { isNetworkError } from '../lib/errors'
import { formatClock } from '../lib/format'
import { isTypingTarget, matchShortcut } from '../lib/keys'
import { WORKFLOW_STATUS_LABEL } from '../lib/terms'
import type { Workflow } from '../types'

// 三栏的收起状态存在这台浏览器里。读写都可能抛（隐私模式、存储被禁），抛了就用默认值
const PALETTE_KEY = 'agentlab.studio.palette'
const ASSISTANT_KEY = 'agentlab.studio.assistant'
const readPref = (key: string): 'open' | 'closed' | null => {
  try {
    const v = localStorage.getItem(key)
    return v === 'open' || v === 'closed' ? v : null
  } catch {
    return null
  }
}
const writePref = (key: string, open: boolean) => {
  try { localStorage.setItem(key, open ? 'open' : 'closed') } catch { /* 下次用默认值 */ }
}
/** 窄于这个宽度默认收起节点库：1024 宽时三栏固定占掉 624px，画布只剩约 400px */
const NARROW = 1280
/** 助手栏可拖宽：长 prompt、成员 system 在 360px 里没法编辑；再宽画布就不够了 */
const ASIDE_KEY = 'agentlab.studio.assistantWidth'
const ASIDE_MIN = 300
const ASIDE_MAX = 560
const readAsideWidth = () => {
  try {
    const v = Number(localStorage.getItem(ASIDE_KEY))
    return Number.isFinite(v) && v >= ASIDE_MIN && v <= ASIDE_MAX ? v : 360
  } catch {
    return 360
  }
}

/** 功能键（F8）lib/keys 的 matchShortcut 按小写比 e.key，对不上 'F8'：这里补一道 */
function matches(e: KeyboardEvent, combo: string): boolean {
  if (matchShortcut(e, combo)) return true
  const m = /^(shift\+)?(f\d{1,2})$/i.exec(combo)
  return !!m && e.key.toLowerCase() === m[2].toLowerCase() && e.shiftKey === !!m[1]
    && !e.metaKey && !e.ctrlKey && !e.altKey
}

const PHASE_TEXT: Record<string, string> = {
  connecting: '正在连接模型',
  planning: '正在理解需求、规划步骤',
  building: '正在往画布上搭',
  wiring: '正在连线',
  finalizing: '排版、校验',
  repairing: '自查发现问题，正在修',
}

export function StudioPage() {
  const { workflowId } = useParams()
  const [params] = useSearchParams()
  const location = useLocation()
  const navigate = useNavigate()
  const { workflows, refresh } = useCatalog()
  const catalogLoaded = useCatalog((s) => s.loaded)
  const workflow = useStudio((s) => s.workflow)
  const dirty = useStudio((s) => s.dirty)
  // 只订阅数量，不订阅整个 nodes——后者每拖一下都会让整页重渲染
  const nodeCount = useStudio((s) => s.nodes.length)
  const issues = useStudio((s) => s.issues)
  const analysis = useStudio((s) => s.analysis)
  const streaming = useStudio((s) => s.streaming)
  const selectedId = useStudio((s) => s.selectedId)
  const canUndo = useStudio((s) => s.past.length > 0)
  const canRedo = useStudio((s) => s.future.length > 0)
  const undoLabel = useStudio((s) => s.past[s.past.length - 1]?.label)
  const redoLabel = useStudio((s) => s.future[s.future.length - 1]?.label)
  const pendingNote = useStudio((s) => s.pendingNote)
  const copilotActive = useStudio((s) => s.copilot.active)
  // 动作逐个取：解构整个 store 会让这一页跟着每一次状态变化（运行时每个 token）重渲染
  const load = useStudio((s) => s.load)
  const save = useStudio((s) => s.save)
  const setGraph = useStudio((s) => s.setGraph)
  const select = useStudio((s) => s.select)
  const focusNode = useStudio((s) => s.focusNode)
  const undo = useStudio((s) => s.undo)
  const redo = useStudio((s) => s.redo)
  const attachRun = useStudio((s) => s.attachRun)
  const analyzeNow = useStudio((s) => s.analyzeNow)

  /** 正在按 id 去取哪张图。防止 effect 重入时重复发请求、重复弹提示 */
  const resolving = useRef<string | null>(null)
  /** 按 id 取图失败（断网）的那一张：恢复连接时重试。画布有未保存改动时绝不覆盖 */
  const [loadError, setLoadError] = useState<{ id: string; error: unknown } | null>(null)
  /** 换图前正在问「放弃改动吗」：effect 重入时别再弹一个 */
  const asking = useRef<string | null>(null)

  const [picker, setPicker] = useState(false)
  const [saving, setSaving] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const [history, setHistory] = useState(false)
  const [dock, setDock] = useState<DockTab | null>(null)
  const [cursor, setCursor] = useState<string | null>(null)
  const [paletteOpen, setPaletteOpen] = useState(() => {
    const pref = readPref(PALETTE_KEY)
    return pref ? pref === 'open' : window.innerWidth >= NARROW
  })
  const [assistantOpen, setAssistantOpen] = useState(() => readPref(ASSISTANT_KEY) !== 'closed')
  const [asideWidth, setAsideWidth] = useState(readAsideWidth)

  const togglePalette = useCallback((open?: boolean) => {
    setPaletteOpen((v) => {
      const next = open ?? !v
      writePref(PALETTE_KEY, next)
      return next
    })
  }, [])
  const assistantRef = useRef(assistantOpen)
  const toggleAssistant = useCallback((open?: boolean) => {
    const next = open ?? !assistantRef.current
    assistantRef.current = next
    writePref(ASSISTANT_KEY, next)
    setAssistantOpen(next)
    // 属性面板和版本历史都叠在这一栏里：栏收起了它们也跟着收，别在看不见的地方开着
    if (!next) {
      select(null)
      setHistory(false)
    }
  }, [select])

  // /studio 不带 id：落到第一张图。replace 而不是 push，否则按后退会回到
  // 这个空壳地址、又被弹回来，人就在这儿出不去了。
  //
  // 画布上已经有东西就别跳——问数据页的「在画布里打开」正是这么送过来的：
  // 一张还没存、还没有 id 的草稿图。跳过去会拿第一张图把它盖掉，
  // 而且因为 setGraph 标了脏，还会先弹一句莫名其妙的"有未保存的改动"
  useEffect(() => {
    if (workflowId) return
    // 选择器里删掉了眼前这张、又没有别的可换：等地址落到 /studio 再卸画布。不能在删的
    // 当场卸——换地址是个过渡，比 store 晚一拍，中间那一帧地址还指着刚删的 id、画布
    // 却空了，「按地址打开」就会去取它、撞上 404，再弹一句「不在了」
    const gone = (location.state as { unload?: string } | null)?.unload
    if (gone && gone === workflow?.id) { load(null); return }
    if (!workflows.length || nodeCount) return
    navigate(`/studio/${workflows[0].id}`, { replace: true })
  }, [workflowId, workflows, nodeCount, navigate, location.state, workflow, load])

  const resolve = useCallback((id: string) => {
    if (resolving.current === id) return
    resolving.current = id
    setLoadError(null)
    void api.workflows.get(id).then((w) => {
      resolving.current = null
      if (useStudio.getState().workflow?.id !== id) load(w)
    }).catch((e) => {
      resolving.current = null
      // 连不上后端 ≠ 工作流不在了：留在这个地址上，恢复连接时再取一次
      if (isNetworkError(e)) {
        setLoadError({ id, error: e })
        return
      }
      toast.info('那张工作流不在了，可能已经被删了')
      navigate('/studio', { replace: true })
    })
  }, [load, navigate])

  // URL → 画布。地址说打开哪张就打开哪张
  useEffect(() => {
    if (!workflowId || workflow?.id === workflowId || !catalogLoaded) return

    // 选择器里本来就有一道"未保存改动"的确认，但那道闸在它的 onClick 里，
    // 而浏览器前进/后退会绕过选择器直接换图——改了一半的画布就这么没了。
    // 所以这里补一道，取消就把地址退回去。选择器问过的（state.discard）不再问
    if (dirty && !(location.state as { discard?: boolean } | null)?.discard) {
      if (asking.current === workflowId) return
      asking.current = workflowId
      void confirmDiscard().then((ok) => {
        asking.current = null
        if (!ok) {
          navigate(workflow ? `/studio/${workflow.id}` : '/studio', { replace: true })
          return
        }
        useStudio.setState({ dirty: false })
      })
      return
    }

    const known = workflows.find((w) => w.id === workflowId)
    if (known) { load(known); return }

    // 不在目录里：目录可能是旧的，也可能真被删了。按 id 取一次问清楚，
    // 而不是直接判死——分享出去的链接不该因为对方目录没刷新就打不开。
    //
    // 这一路是异步的，而 effect 的依赖里有好几个会变的东西（目录、dirty），
    // 请求还没回来它就又跑了一遍：实测同一个坏地址弹了三次"不在了"。
    // resolve 用一个 ref 记住正在解哪个 id，重复的直接让开
    resolve(workflowId)
  }, [workflowId, workflow, workflows, catalogLoaded, dirty, load, navigate, resolve, location.state])

  // 断开期间没取到的那张，连上之后再取一次。有未保存的改动就不动它
  useOnReconnect(() => {
    if (loadError && loadError.id === workflowId && !useStudio.getState().dirty) resolve(loadError.id)
    if (useStudio.getState().analysis === 'failed') void analyzeNow()
  })

  // ?run=<id>&focus=<node>：运行记录、右栏步骤行用它跳进画布。attachRun 要等 load
  // 完成之后再调——load 会清掉运行态。focus 在那之后随时可以调
  const runParam = params.get('run')
  const focusParam = params.get('focus')
  const handled = useRef('')
  useEffect(() => {
    if (!workflow || workflow.id !== workflowId) return
    const key = `${workflow.id}|${runParam ?? ''}|${focusParam ?? ''}`
    if (handled.current === key || (!runParam && !focusParam)) return
    handled.current = key
    void (async () => {
      if (runParam && useStudio.getState().run?.id !== runParam) {
        await attachRun(runParam).catch((e) => toast.error(e))
      }
      if (focusParam) {
        // 等画布把节点量完、视口就位再取景，不然对到的是 (0,0)
        requestAnimationFrame(() => requestAnimationFrame(() => focusNode(focusParam)))
      }
    })()
  }, [workflow, workflowId, runParam, focusParam, attachRun, focusNode])

  // 开始跑图或生成时，属性面板让开——不然进展发生在一块被盖住的地方。
  // （选中节点即滑出属性面板，取消选中即收起，不再需要手动切 tab）
  useEffect(() => {
    if (streaming || copilotActive) select(null)
  }, [streaming, copilotActive, select])

  // 选中节点要编辑它：助手栏收着的话，属性面板无处可去，先把栏展开。
  // 只在「选中换成了一个节点」的那一下展开：连 assistantOpen 一起盯着的话，
  // 选着节点按 ⌥A 收栏，这里马上又把它展开，栏就收不起来了
  const lastSelected = useRef<string | null>(null)
  useEffect(() => {
    const prev = lastSelected.current
    lastSelected.current = selectedId
    if (selectedId && selectedId !== prev && !assistantRef.current) toggleAssistant(true)
  }, [selectedId, toggleAssistant])

  const doSave = useCallback(async (note?: string) => {
    const s = useStudio.getState()
    if (!s.workflow || s.copilot.active) return
    setSaving(true)
    try {
      await save(note)
      toast.ok(note ?? s.pendingNote ? `已保存：${note ?? s.pendingNote}` : '已保存')
      void refresh()
    } catch (e) {
      toast.error(e)
    } finally {
      setSaving(false)
    }
  }, [save, refresh])

  const saveWithNote = useCallback(async () => {
    const note = await promptDialog({
      title: '保存并写版本说明',
      body: '说明会跟着这一版留在版本历史里，以后回头找「是哪一版改坏的」就靠它。',
      label: '这一版改了什么',
      initial: useStudio.getState().pendingNote,
      placeholder: '例如：叙述 prompt 禁止元话语数字',
      confirmLabel: '保存',
    })
    if (note != null) await doSave(note)
  }, [doSave])

  // 有未保存改动时拦一下关标签页／刷新。撤销栈只在这一页里，关掉就没了
  useEffect(() => {
    if (!dirty) return
    const guard = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', guard)
    return () => window.removeEventListener('beforeunload', guard)
  }, [dirty])

  const relayout = useCallback(async () => {
    const { nodes, edges, copilot } = useStudio.getState()
    if (copilot.active || !nodes.length) return
    try {
      setGraph(await api.copilot.layout(toGraph(nodes, edges)))
      toast.ok('已重新排版', { key: 'studio:layout', action: { label: '撤销', onClick: () => useStudio.getState().undo() } })
    } catch (e) {
      toast.error(e)
    }
  }, [setGraph])

  // ---- 问题 ----

  // 只跟着 issues 重算：每次改图之后校验都会重跑、issues 都会换新，这时的节点就是
  // 校验时的节点。订阅 nodes 的话，有问题的图每拖一帧整页都要重渲染
  const problems = useMemo<Problem[]>(() => {
    if (!issues.length) return []
    const { nodes, edges } = useStudio.getState()
    return problemsOf(issues, nodes, topology({ nodes, edges }).rank)
  }, [issues])
  const errorCount = problems.filter((p) => p.level === 'error').length
  const warnCount = problems.length - errorCount

  /** 点一条问题：选中节点、镜头对过去、检查器翻到出问题的字段 */
  const locate = useCallback((p: Problem) => {
    setCursor(p.id)
    if (!p.nodeId) return
    select(p.nodeId)
    focusNode(p.nodeId)
    if (p.field) {
      // 检查器是跟着 selectedId 挂上的：等它渲染出来再滚
      const key = p.field.key
      requestAnimationFrame(() => requestAnimationFrame(() => {
        const el = document.querySelector(`[data-field="${CSS.escape(key)}"]`)
        el?.scrollIntoView({ block: 'center', behavior: 'smooth' })
      }))
    }
  }, [select, focusNode])

  /** F8 / ⇧F8：在落在节点上的问题之间跳。图级、边上的没有节点可对准，列在面板顶上 */
  const step = useCallback((dir: 1 | -1) => {
    const nav = problems.filter((p) => p.scope === 'node')
    if (!nav.length) {
      if (problems.length) setDock('problems')
      else toast.info(analysis === 'failed' ? '分析失败，看不到问题清单' : '没有发现问题', { key: 'studio:problems' })
      return
    }
    setDock('problems')
    const i = nav.findIndex((p) => p.id === cursor)
    const next = i < 0 ? nav[dir > 0 ? 0 : nav.length - 1] : nav[(i + dir + nav.length) % nav.length]
    locate(next)
  }, [problems, cursor, locate, analysis])

  // ---- 快捷键 ----

  const openHistory = useCallback(() => {
    setHistory((v) => !v)
    toggleAssistant(true)
    select(null)
  }, [toggleAssistant, select])

  useEffect(() => {
    const run = (id: StudioShortcutId): boolean => {
      const s = useStudio.getState()
      switch (id) {
        case 'save': void doSave(); return true
        case 'saveNote': void saveWithNote(); return true
        case 'undo': undo(); return true
        case 'redo': redo(); return true
        case 'copy': {
          // 页面上选了一段文字（助手的回答、运行输出）时 ⌘C 是复制文字，不抢
          if (window.getSelection()?.toString()) return false
          const n = s.copySelection()
          if (n) toast(`已复制 ${n} 个节点`, 'info', { key: 'studio:copy', duration: 2000 })
          return n > 0
        }
        case 'paste': return s.pasteClipboard() > 0
        case 'duplicate': s.duplicateSelection(); return true
        case 'selectAll': s.selectAll(); return true
        case 'focus':
          if (!s.selectedId) return false
          focusNode(s.selectedId)
          return true
        case 'fit': useStudio.setState({ fitRequest: s.fitRequest + 1 }); return true
        case 'layout': void relayout(); return true
        case 'search':
          togglePalette(true)
          requestAnimationFrame(() => document.getElementById(PALETTE_SEARCH_ID)?.focus())
          return true
        case 'nextProblem': step(1); return true
        case 'prevProblem': step(-1); return true
        case 'problems': setDock((d) => (d === 'problems' ? null : 'problems')); return true
        case 'variables': setDock((d) => (d === 'variables' ? null : 'variables')); return true
        case 'history': openHistory(); return true
        case 'palette': togglePalette(); return true
        case 'assistant': toggleAssistant(); return true
        default: return false
      }
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || isComposing(e)) return
      // 弹窗开着时键盘归弹窗
      if (document.querySelector('[aria-modal="true"]')) return
      const hit = STUDIO_SHORTCUTS.find((s) => !s.passive && [s.combo, ...(s.alt ?? [])].some((c) => matches(e, c)))
      if (!hit) return
      // 输入框里只放行保存和 F8：撤销、复制粘贴、⌥V 在那儿是给文字的
      if (isTypingTarget(e.target) && !hit.inInputs && hit.id !== 'nextProblem' && hit.id !== 'prevProblem') return
      if (run(hit.id)) e.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [doSave, saveWithNote, undo, redo, focusNode, relayout, step, togglePalette, toggleAssistant, openHistory])

  // ---- 渲染 ----

  if (!workflows.length && !nodeCount) {
    return (
      <EmptyState
        className="h-full"
        title="还没有工作流"
        body="新建一张空白工作流，从模板开始，或者在右边用助手直接把想法生成出来。"
        action={<button className="btn btn-primary" onClick={() => void createWorkflow(navigate, refresh)}>
          <Plus size={12} /> 新建工作流
        </button>}
      />
    )
  }

  return (
    <div className="flex h-full flex-col">
      {/* 工具栏。48px 以内：toast 从它下沿开始排 */}
      <div className="@container flex h-12 shrink-0 items-center gap-1.5 border-b px-3">
        <button className="btn btn-ghost min-w-0 gap-1.5 px-2" onClick={() => setPicker(true)}
                title="切换、新建、从模板新建工作流">
          <span className="max-w-56 truncate font-medium @max-[1120px]:max-w-32">{workflow?.name ?? '选择工作流'}</span>
          <ChevronDown size={12} className="shrink-0 text-faint" />
        </button>

        {workflow && <VersionLabel workflow={workflow} />}
        {dirty && (
          <span className="chip shrink-0" style={{ color: 'var(--warn)', borderColor: 'var(--warn)' }}
                title={pendingNote ? `保存时的版本说明：${pendingNote}` : undefined}>
            未保存
          </span>
        )}
        {!!nodeCount && (
          <AnalysisChip analysis={analysis} errors={errorCount} warnings={warnCount}
                        open={dock === 'problems'}
                        onRetry={() => void analyzeNow()}
                        onClick={() => setDock((d) => (d === 'problems' ? null : 'problems'))} />
        )}

        <div className="min-w-2 flex-1" />

        {/* 生成期间这一排全部锁住（停止在画布上方的条和助手栏里）：这时候保存会存下
            半张图，运行跑的也是半张图，排版会被它的最终结果覆盖 */}
        <fieldset disabled={copilotActive} className="flex min-w-0 items-center gap-1.5">
          <div className="flex items-center">
            <IconButton label="撤销" title={undoLabel ? `${hintOf('撤销', 'undo')}：${undoLabel}` : hintOf('撤销', 'undo')}
                        disabled={!canUndo} onClick={undo} icon={<Undo2 size={13} />} />
            <IconButton label="重做" title={redoLabel ? `${hintOf('重做', 'redo')}：${redoLabel}` : hintOf('重做', 'redo')}
                        disabled={!canRedo} onClick={redo} icon={<Redo2 size={13} />} />
          </div>
          <span className="h-4 w-px shrink-0" style={{ background: 'var(--border)' }} />
          <button
            className="btn shrink-0"
            title="用自然语言生成或修改工作流（Copilot）"
            onClick={() => {
              select(null)   // 属性面板盖着的话先让开
              setHistory(false)
              toggleAssistant(true)
              requestAnimationFrame(() =>
                window.dispatchEvent(new Event('agentlab:focus-copilot')))
            }}
          >
            <Wand2 size={12} /> <span className="@max-[1120px]:hidden">助手</span>
          </button>
          <button
            className={clsx('btn shrink-0', dock === 'variables' && 'border-[var(--border-strong)] bg-hover')}
            aria-pressed={dock === 'variables'}
            title={hintOf('看这张工作流里有哪些变量、谁产出、谁引用', 'variables')}
            onClick={() => setDock((d) => (d === 'variables' ? null : 'variables'))}
          >
            <Variable size={12} /> <span className="@max-[1000px]:hidden">变量</span>
          </button>
          <IconButton label="版本历史" title={hintOf('版本历史', 'history')} variant="default"
                      aria-pressed={history} disabled={!workflow}
                      className={clsx(history && 'bg-hover')} onClick={openHistory}
                      icon={<History size={12} />} />
          <IconButton label="自动排版" title={hintOf('自动排版', 'layout')} variant="default"
                      disabled={!nodeCount} onClick={() => void relayout()} icon={<LayoutGrid size={12} />} />
          <button className="btn shrink-0" onClick={() => setPublishing(true)} disabled={!workflow || dirty}
                  title={dirty ? '先保存再发布' : '把当前版本立为已发布版本，正式运行只认它'}>
            <ShieldCheck size={12} /> <span className="@max-[1000px]:hidden">发布</span>
          </button>
          <button className="btn shrink-0" onClick={() => void doSave()} disabled={saving || !dirty}
                  title={pendingNote ? `${hintOf('保存', 'save')} · 版本说明：${pendingNote}` : hintOf('保存', 'save')}>
            {saving ? <Spinner size={12} /> : <Save size={12} />} <span className="@max-[1000px]:hidden">保存</span>
          </button>
          {/* 运行是对整张图的动作，和保存、发布同类，属于工具栏。放在助手栏里
              会和 Copilot 输入框两个"主要动作"互相压着 */}
          <RunControl />
        </fieldset>
        <IconButton label={assistantOpen ? '收起助手栏' : '展开助手栏'}
                    title={hintOf(assistantOpen ? '收起助手栏' : '展开助手栏', 'assistant')}
                    onClick={() => toggleAssistant()}
                    icon={assistantOpen ? <PanelRightClose size={13} /> : <PanelRightOpen size={13} />} />
      </div>

      {/* 三栏 */}
      <div className="flex min-h-0 flex-1">
        <aside className={clsx('shrink-0 border-r bg-panel', paletteOpen ? 'w-52' : 'w-12')}>
          <Palette collapsed={!paletteOpen} onToggle={() => togglePalette()} />
        </aside>
        {/* 停靠栏做成挤压式而不是浮层：React Flow 的 Controls 和 MiniMap
            是绝对定位在画布容器里的，浮层会把它俩埋掉，挤压会把它俩顶上去 */}
        <main className="relative flex min-w-0 flex-1 flex-col">
          <div className="relative min-h-0 flex-1">
            {loadError && loadError.id === workflowId && !workflow
              ? <ErrorState error={loadError.error} onRetry={() => resolve(loadError.id)} className="h-full" />
              : <FlowCanvas />}
            {copilotActive && <BuildingBar />}
            <LineageLayer />
          </div>
          {dock && (
            <CanvasDock tab={dock} onTab={setDock} onClose={() => setDock(null)}
                        problems={problems} activeProblem={cursor} onLocate={locate} />
          )}
        </main>
        {/* 助手常驻，属性和版本历史是盖在它上面的一层。做成 tab 的话它们就互斥了，
            而这几件事在时间上并不互斥——跑图跑到一半点开节点看配置，整条执行过程
            会从眼前消失，切回来滚动位置和输入草稿也没了。
            收起时只是藏起来不卸载：输入框里打了一半的字、滚动位置都留着 */}
        <aside className={clsx('relative shrink-0 border-l bg-panel', !assistantOpen && 'invisible overflow-hidden border-l-0')}
               style={{ width: assistantOpen ? asideWidth : 0 }}
               aria-hidden={!assistantOpen || undefined}>
          {assistantOpen && <WidthHandle width={asideWidth} onChange={setAsideWidth} />}
          {/* 展开时填满（左边框占掉 1px，写死宽度会顶出页面）；收起时保持原宽，里面的排版不塌 */}
          <div className="relative flex h-full flex-col overflow-hidden" style={{ width: assistantOpen ? '100%' : asideWidth }}>
            <AssistantPanel />
            <InspectorSheet />
            {history && workflow && <VersionsSheet workflow={workflow} onClose={() => setHistory(false)} />}
          </div>
        </aside>
      </div>

      <WorkflowPicker open={picker} onClose={() => setPicker(false)} />
      {publishing && workflow && (
        <PublishDialog workflow={workflow} onClose={() => setPublishing(false)}
                       onDone={() => { setPublishing(false); void refresh() }}
                       onLocate={(id) => { select(id); focusNode(id) }} />
      )}
    </div>
  )
}

// -------------------------------------------------------------------------

/** 助手栏左边缘的拖拽条。键盘也能调：←→ 每次 20px */
function WidthHandle({ width, onChange }: { width: number; onChange: (w: number) => void }) {
  const save = (w: number) => { try { localStorage.setItem(ASIDE_KEY, String(Math.round(w))) } catch { /* 下次用默认 */ } }
  const clamp = (w: number) => Math.min(ASIDE_MAX, Math.max(ASIDE_MIN, w))
  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label="拖动调整助手栏宽度"
      aria-valuemin={ASIDE_MIN}
      aria-valuemax={ASIDE_MAX}
      aria-valuenow={width}
      tabIndex={0}
      className="group absolute -left-1 top-0 z-30 h-full w-2 cursor-col-resize outline-none"
      onKeyDown={(e) => {
        if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
        e.preventDefault()
        const w = clamp(width + (e.key === 'ArrowLeft' ? 20 : -20))
        onChange(w)
        save(w)
      }}
      onDoubleClick={() => { onChange(360); save(360) }}
      onPointerDown={(e) => {
        e.preventDefault()
        const x0 = e.clientX
        let last = width
        const move = (ev: PointerEvent) => { last = clamp(width + (x0 - ev.clientX)); onChange(last) }
        const up = () => {
          window.removeEventListener('pointermove', move)
          window.removeEventListener('pointerup', up)
          save(last)
        }
        window.addEventListener('pointermove', move)
        window.addEventListener('pointerup', up)
      }}
    >
      <span className="mx-auto block h-full w-px opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100"
            style={{ background: 'var(--border-strong)' }} />
    </div>
  )
}

/**
 * 「草稿 v3 · 已发布 v2（领先 1 版）」。
 *
 * 画布在哪一版、正式运行跑的是哪一版，是两件事：保存会把状态退回草稿，已发布的那一版
 * 仍由 published_version 指着。以前只显示一个「草稿」，看不出正式运行其实还能跑、
 * 跑的是哪一版。
 */
function VersionLabel({ workflow: w }: { workflow: Workflow & { published_by?: string | null } }) {
  const p = w.published_version
  // 画布就是已发布的那一版才算「在线」。老数据里有状态还挂着受管、版本却已经往前走了的
  // （旧的 restore 接口不退回草稿），那也得说清画布是草稿
  const live = (w.status === 'governed' || w.status === 'published') && p === w.version
  const ahead = p != null ? w.version - p : 0
  const pubText = p != null ? `${WORKFLOW_STATUS_LABEL[w.status === 'governed' ? 'governed' : 'published']} v${p}` : ''
  const title = [
    live ? `画布就是${pubText}` : `画布是草稿 v${w.version}`,
    p != null && !live ? `正式运行跑的是已发布的 v${p}${ahead > 0 ? `，画布比它新 ${ahead} 版` : ''}` : '',
    p == null ? '还没发布过：只能发起探索运行' : '',
    w.published_by ? `发布人：${w.published_by}` : '',
  ].filter(Boolean).join('\n')
  return (
    <span className="flex shrink-0 items-center overflow-hidden rounded-full border text-2xs leading-5" title={title}>
      {!live && <span className="tnum px-2 text-dim">草稿 v{w.version}</span>}
      {p != null && (
        <span className={clsx('tnum flex items-center gap-1 px-2', !live && 'border-l')}
              style={{ color: 'var(--ok)', ...(w.status === 'governed' ? { background: 'var(--st-done-soft)' } : {}) }}>
          <ShieldCheck size={10} /> {pubText}
          {!live && ahead > 0 && <span className="text-faint @max-[1120px]:hidden">（领先 {ahead} 版）</span>}
        </span>
      )}
    </span>
  )
}

/**
 * 校验结论。以前是个点不动的 span，有 error 时还把 warning 的数藏起来；分析失败时
 * 照样写「可运行」，刚打开、还没校验完也先闪一下「可运行」。
 */
function AnalysisChip({ analysis, errors, warnings, open, onClick, onRetry }: {
  analysis: string; errors: number; warnings: number; open: boolean
  onClick: () => void; onRetry: () => void
}) {
  if (analysis === 'failed') {
    return (
      <button type="button" className="chip shrink-0 cursor-pointer hover:bg-hover" onClick={onRetry}
              style={{ color: 'var(--warn)', borderColor: 'var(--warn)' }}
              title="校验和变量分析的请求没有成功，现在说不清这张图能不能跑。点一下重试">
        <RotateCw size={9} /> 分析失败 · 重试
      </button>
    )
  }
  if (analysis === 'pending' && !errors && !warnings) {
    return <span className="chip shrink-0 text-faint"><Spinner size={9} /> 校验中…</span>
  }
  const tone = errors ? 'var(--err)' : warnings ? 'var(--warn)' : 'var(--ok)'
  return (
    <button
      type="button"
      className={clsx('chip shrink-0 cursor-pointer transition-colors hover:bg-hover', open && 'bg-hover')}
      style={{ color: tone, borderColor: errors ? 'var(--err)' : undefined }}
      aria-expanded={open}
      title={hintOf(errors || warnings ? '打开问题面板，点一条定位到节点' : '打开问题面板', 'problems')}
      onClick={onClick}
    >
      {errors > 0 && <span className="tnum flex items-center gap-0.5"><XCircle size={9} /> {errors} 错</span>}
      {errors > 0 && warnings > 0 && <span className="text-faint">·</span>}
      {warnings > 0 && (
        <span className="tnum flex items-center gap-0.5" style={{ color: 'var(--warn)' }}>
          <AlertTriangle size={9} /> {warnings} 提示
        </span>
      )}
      {!errors && !warnings && <><Check size={9} /> 可运行</>}
    </button>
  )
}

/**
 * 助手搭图时画布上方的一条：在干什么、用了多久、停止。
 *
 * 以前阶段、用时只在右栏底部一行小字里，视线得在右栏和画布之间来回跳；画布本身
 * 看不出「正在被改」，还能拖能改——改的东西会被最终结果悄悄覆盖。现在画布锁住，
 * 这一条说明为什么锁、多久了、怎么停。
 */
function BuildingBar() {
  const copilot = useStudio((s) => s.copilot)
  const stopCopilot = useStudio((s) => s.stopCopilot)
  const now = useRunClock(true)
  const started = useRef(Date.now())
  const text = copilot.lastOp || PHASE_TEXT[copilot.phase] || '正在起草…'
  return (
    <div className="pointer-events-none absolute inset-x-0 top-3 z-20 flex justify-center px-3">
      <div className="fade-up pointer-events-auto flex max-w-full items-center gap-2.5 rounded-full border bg-panel py-1 pl-3 pr-1 text-2xs shadow-elev-2"
           style={{ borderColor: 'color-mix(in srgb, var(--copilot) 45%, var(--border))' }}>
        <Wand2 size={12} className="shrink-0" style={{ color: 'var(--copilot)' }} />
        {/* 播报区只圈住阶段文字：计时器 100ms 一跳，圈进来读屏就会一直念秒数 */}
        <span role="status" aria-live="polite" className="flex min-w-0 items-center gap-2.5">
          <span className="shrink-0 font-medium">助手正在改这张工作流</span>
          <span className="min-w-0 truncate text-dim">{text}</span>
        </span>
        <span className="mono tnum shrink-0 text-faint" aria-hidden>{formatClock(now - started.current)}</span>
        <span className="shrink-0 text-faint">· 画布已锁定</span>
        <button type="button" className="btn btn-xs shrink-0 rounded-full" onClick={stopCopilot}
                title="停下这一轮：画布退回这一轮之前，做了一半的可以用重做找回">
          <Square size={9} fill="currentColor" /> 停止
        </button>
      </div>
    </div>
  )
}
