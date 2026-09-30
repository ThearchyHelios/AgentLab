import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'
import { useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  AlertTriangle, Check, ChevronDown, History, LayoutGrid, MoreHorizontal, PanelRightClose, PanelRightOpen, Plus,
  Redo2, RotateCw, Save, ShieldCheck, Square, Undo2, Variable, Wand2, XCircle,
} from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { FlowCanvas } from '../canvas/FlowCanvas'
import { Palette, PALETTE_SEARCH_ID } from '../canvas/Palette'
import { InspectorSheet, revealField } from '../canvas/InspectorSheet'
import { CanvasDock, type DockTab } from '../canvas/CanvasDock'
import { LineageLayer } from '../canvas/LineageLayer'
import { VersionsSheet } from '../canvas/VersionsSheet'
import { PublishDialog } from '../canvas/PublishDialog'
import { WorkflowPicker, confirmDiscard, createWorkflow, takeDiscarded } from '../canvas/WorkflowPicker'
import { problemsOf, type Problem } from '../canvas/issues'
import { STUDIO_SHORTCUTS, hintOf, studioShortcut, type StudioShortcutId } from '../canvas/shortcuts'
import { AssistantPanel } from '../run/AssistantPanel'
import { copilotProgress } from '../run/Composer'
import { RunControl } from '../run/RunControl'
import { useRunClock } from '../run/useRunClock'
import { topology } from '../run/derive'
import { EDIT_LOCK_TEXT, editLockOf, toGraph, useEditLock, useStudio } from '../store/studio'
import { useCatalog, useOnReconnect } from '../store/catalog'
import {
  EmptyState, ErrorState, IconButton, Spinner, isComposing, promptDialog, toast,
} from '../components/ui'
import { isNetworkError } from '../lib/errors'
import { formatClock } from '../lib/format'
import { formatShortcut, isTypingTarget, matchShortcut } from '../lib/keys'
import { WORKFLOW_STATUS_LABEL } from '../lib/terms'
import type { Workflow } from '../types'

// 三栏的收起状态存在这台浏览器里。读写都可能抛（隐私模式、存储被禁），抛了就用默认值
const PALETTE_KEY = 'agentlab.studio.palette'
const ASSISTANT_KEY = 'agentlab.studio.assistant'
/** 窄屏上助手栏的开合单独记：宽屏上一直开着，不等于在 1024 的笔记本上也要它压掉半张画布 */
const ASSISTANT_NARROW_KEY = 'agentlab.studio.assistant.narrow'
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
/**
 * 窄于这个宽度：节点库默认收成图标轨，助手栏默认收起、展开时浮在画布上而不是挤它。
 * 1024 宽时三栏固定占掉 624px，画布只剩约 400px；768 宽时 8 个节点只看得见 3 个
 */
const NARROW = 1280
const isNarrow = () => window.innerWidth < NARROW
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

/** 改图的快捷键。正式运行期间按了要说为什么没反应，不能安静地什么都不做 */
const EDIT_SHORTCUTS = new Set<StudioShortcutId>(['undo', 'redo', 'paste', 'duplicate', 'layout'])

export function StudioPage() {
  const { workflowId } = useParams()
  const [params, setParams] = useSearchParams()
  const location = useLocation()
  const navigate = useNavigate()
  // 逐个取：整个 catalog 订阅下来，连接心跳、待审批轮询每跳一次整页（连同检查器里每个字段）都要重渲染
  const workflows = useCatalog((s) => s.workflows)
  const refresh = useCatalog((s) => s.refresh)
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
  // 正式运行在跑已发布的版本：改图的按钮置灰（运行控制照常能用，停得下来）
  const formalLock = useEditLock() === 'formal'
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
  // 窗口拖过门槛时只换摆法（占位 / 浮层），不替人开合：正开着的栏不会因为拖窄一点就没了
  const [narrow, setNarrow] = useState(isNarrow)
  useEffect(() => {
    const mq = matchMedia(`(max-width: ${NARROW - 0.02}px)`)
    const sync = () => setNarrow(mq.matches)
    mq.addEventListener('change', sync)
    return () => mq.removeEventListener('change', sync)
  }, [])
  const narrowRef = useRef(narrow)
  narrowRef.current = narrow
  const [assistantOpen, setAssistantOpen] = useState(() => {
    const pref = readPref(isNarrow() ? ASSISTANT_NARROW_KEY : ASSISTANT_KEY)
    return pref ? pref === 'open' : !isNarrow()
  })
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
    writePref(narrowRef.current ? ASSISTANT_NARROW_KEY : ASSISTANT_KEY, next)
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
      toast.info('该工作流不存在，可能已被删除')
      navigate('/studio', { replace: true })
    })
  }, [load, navigate])

  // URL → 画布。地址说打开哪张就打开哪张
  useEffect(() => {
    if (!workflowId || workflow?.id === workflowId || !catalogLoaded) return

    // 换到另一张之前的确认，平时在地址跳之前就问过了：选择器自己问（state.discard），
    // 后退、⌘K、别的页上的链接由离开守卫问（见 WorkflowPicker 的 discardGuard，问过的
    // 那一跳 takeDiscarded 认得出来）。这里是兜底：绕过了守卫还带着没存的改动进来的，
    // 事后问一句，取消就把地址退回去
    if (dirty && !(location.state as { discard?: boolean } | null)?.discard) {
      if (takeDiscarded(location.pathname)) {
        useStudio.setState({ dirty: false })
        return
      }
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

  // ?upgrade=1：记录页「这次的报告没有逐段证据 → 升级这张图」跳过来的。图打开之后打开问题面板、
  // 要一份升级预览（只读，要人点应用才存）。读完就摘掉：留着的话刷新、后退回来都会再要一次，
  // 应用过之后再要，回来的是「没有需要升级的地方」。画布锁着（正式运行在跑、助手在改）不要，说清为什么
  const upgradeParam = params.get('upgrade')
  const upgraded = useRef<string | null>(null)
  useEffect(() => {
    if (!upgradeParam || !workflow || workflow.id !== workflowId) return
    // 同一次跳转只要一次（开发模式下 effect 会被连跑两遍，摘参数的那次替换还没落地）
    if (upgraded.current === location.key) return
    upgraded.current = location.key
    setParams((prev) => {
      const next = new URLSearchParams(prev)
      next.delete('upgrade')
      return next
    }, { replace: true })
    setDock('problems')
    void useStudio.getState().previewUpgrade()
  }, [upgradeParam, workflow, workflowId, setParams, location.key])

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
      body: '版本说明会随此版本保存在版本历史中，便于日后追溯各版本的改动。',
      label: '本版本的改动',
      initial: useStudio.getState().pendingNote,
      placeholder: '例如：调整报告节点的系统提示，要求数字注明出处',
      confirmLabel: '保存',
    })
    if (note != null) await doSave(note)
  }, [doSave])

  const relayout = useCallback(async () => {
    const s = useStudio.getState()
    const { nodes, edges } = s
    if (editLockOf(s) || !nodes.length) return
    const sent = toGraph(nodes, edges)
    const sig = JSON.stringify(sent)
    try {
      const laid = await api.copilot.layout(sent)
      // 排的是发请求那一刻的图。回来之前画布锁了（正式运行开始、助手开始改），或者人又动过它，
      // 就不套用：锁着时 setGraph 落不下，改过的话旧图的排版会把刚才那几下盖掉。两种都不能
      // 照样报「已重新排版」——那条的撤销撤的是别的改动
      const now = useStudio.getState()
      const lock = editLockOf(now)
      if (lock) {
        toast.warn(EDIT_LOCK_TEXT[lock], { key: 'studio:readonly' })
        return
      }
      if (JSON.stringify(toGraph(now.nodes, now.edges)) !== sig) {
        toast.info('排版期间画布已被修改，本次排版未应用，请重新排版', { key: 'studio:layout-stale' })
        return
      }
      if (setGraph(laid)) {
        toast.ok('已重新排版', { key: 'studio:layout', action: { label: '撤销', onClick: () => useStudio.getState().undo() } })
      }
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

  /**
   * 点一条问题：选中节点、镜头对过去、检查器翻到出问题的字段——能落到第几项、项里的哪一栏
   * （第 2 个成员的工具）就落到那儿。不抢焦点：F8 在问题之间跳时键盘还留在原处
   */
  const locate = useCallback((p: Problem) => {
    setCursor(p.id)
    if (p.nodeId) revealField(p.nodeId, p.field)
  }, [])

  /** F8 / ⇧F8：在落在节点上的问题之间跳。图级、边上的没有节点可对准，列在面板顶上 */
  const step = useCallback((dir: 1 | -1) => {
    const nav = problems.filter((p) => p.scope === 'node')
    if (!nav.length) {
      if (problems.length) setDock('problems')
      else toast.info(analysis === 'failed' ? '分析失败，无法显示问题清单' : '未发现问题', { key: 'studio:problems' })
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
  const openCopilot = useCallback(() => {
    select(null)   // 属性面板盖着的话先让开
    setHistory(false)
    toggleAssistant(true)
    requestAnimationFrame(() => window.dispatchEvent(new Event('agentlab:focus-copilot')))
  }, [select, toggleAssistant])
  const toggleVariables = useCallback(() => setDock((d) => (d === 'variables' ? null : 'variables')), [])

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
      if (EDIT_SHORTCUTS.has(hit.id) && editLockOf(useStudio.getState()) === 'formal') {
        e.preventDefault()
        toast.warn(EDIT_LOCK_TEXT.formal, { key: 'studio:readonly' })
        return
      }
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
        source="workflows"
        title="还没有工作流"
        body="新建一个空白工作流、从模板开始，或者在右侧用助手直接生成。"
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
            半张图，运行跑的也是半张图，排版会被它的最终结果覆盖。
            这一排不许被压得比内容窄：以前压窄了子项互相叠着画，768 宽时「运行」盖住了
            收起助手栏。放不下时让左边的工作流名先截短，次要按钮收进「更多操作」 */}
        <fieldset disabled={copilotActive} className="flex shrink-0 items-center gap-1.5">
          <div className="flex items-center">
            <IconButton label="撤销" title={formalLock ? EDIT_LOCK_TEXT.formal
                          : undoLabel ? `${hintOf('撤销', 'undo')}：${undoLabel}` : hintOf('撤销', 'undo')}
                        disabled={!canUndo || formalLock} onClick={undo} icon={<Undo2 size={13} />} />
            <IconButton label="重做" title={formalLock ? EDIT_LOCK_TEXT.formal
                          : redoLabel ? `${hintOf('重做', 'redo')}：${redoLabel}` : hintOf('重做', 'redo')}
                        disabled={!canRedo || formalLock} onClick={redo} icon={<Redo2 size={13} />} />
          </div>
          <span className="h-4 w-px shrink-0" style={{ background: 'var(--border)' }} />
          <button
            className="btn shrink-0 @max-[880px]:hidden"
            title={formalLock ? EDIT_LOCK_TEXT.formal : '用自然语言生成或修改工作流'}
            disabled={formalLock}
            onClick={openCopilot}
          >
            <Wand2 size={12} /> <span className="@max-[1120px]:hidden">助手</span>
          </button>
          <button
            className={clsx('btn shrink-0 @max-[880px]:hidden', dock === 'variables' && 'border-[var(--border-strong)] bg-hover')}
            aria-pressed={dock === 'variables'}
            title={hintOf('查看工作流中的变量及其产出和引用节点', 'variables')}
            onClick={toggleVariables}
          >
            <Variable size={12} /> <span className="@max-[1000px]:hidden">变量</span>
          </button>
          <IconButton label="版本历史" title={hintOf('版本历史', 'history')} variant="default"
                      aria-pressed={history} disabled={!workflow}
                      className={clsx('@max-[880px]:hidden', history && 'bg-hover')} onClick={openHistory}
                      icon={<History size={12} />} />
          <IconButton label="自动排版" title={formalLock ? EDIT_LOCK_TEXT.formal : hintOf('自动排版', 'layout')}
                      variant="default" disabled={!nodeCount || formalLock} className="@max-[880px]:hidden"
                      onClick={() => void relayout()} icon={<LayoutGrid size={12} />} />
          <MoreMenu items={[
            { id: 'copilot', label: '助手', icon: <Wand2 size={12} />, disabled: formalLock,
              title: formalLock ? EDIT_LOCK_TEXT.formal : '用自然语言生成或修改工作流', onSelect: openCopilot },
            { id: 'variables', label: '变量', icon: <Variable size={12} />, shortcut: 'variables', checked: dock === 'variables',
              onSelect: toggleVariables },
            { id: 'history', label: '版本历史', icon: <History size={12} />, shortcut: 'history', checked: history,
              disabled: !workflow, onSelect: openHistory },
            { id: 'layout', label: '自动排版', icon: <LayoutGrid size={12} />, shortcut: 'layout',
              disabled: !nodeCount || formalLock, title: formalLock ? EDIT_LOCK_TEXT.formal : undefined,
              onSelect: () => void relayout() },
            { id: 'publish', label: '发布…', icon: <ShieldCheck size={12} />, disabled: !workflow || dirty,
              title: dirty ? '请先保存再发布' : '将当前版本设为已发布版本，正式运行仅使用该版本', onSelect: () => setPublishing(true) },
          ]} />
          <button className="btn shrink-0 @max-[880px]:hidden" onClick={() => setPublishing(true)} disabled={!workflow || dirty}
                  title={dirty ? '请先保存再发布' : '将当前版本设为已发布版本，正式运行仅使用该版本'}>
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
      <div className="relative flex min-h-0 flex-1">
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
            收起时只是藏起来不卸载：输入框里打了一半的字、滚动位置都留着。
            窄屏上它浮在画布右侧、不挤画布：展开看一眼助手、改一个节点，画布不跟着缩一半 */}
        <aside className={clsx('shrink-0 border-l bg-panel',
                               narrow ? 'absolute inset-y-0 right-0 z-30 shadow-elev-3' : 'relative',
                               !assistantOpen && 'invisible overflow-hidden border-l-0')}
               style={{ width: assistantOpen ? asideWidth : 0, ...(narrow ? { maxWidth: 'calc(100% - 48px)' } : {}) }}
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

interface MoreItem {
  id: string
  label: string
  icon: ReactNode
  onSelect: () => void
  shortcut?: StudioShortcutId
  disabled?: boolean
  checked?: boolean
  title?: string
}

/**
 * 工具栏窄了（容器 < 880px）才出现的「更多操作」：助手、变量、版本历史、自动排版、发布收在这里，
 * 快捷键照常能用。点外面、按 Esc、选了一项都收起；↑↓ 在项之间走
 */
function MoreMenu({ items }: { items: MoreItem[] }) {
  const [open, setOpen] = useState(false)
  const wrap = useRef<HTMLDivElement>(null)
  const menuId = useId()
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => { if (!wrap.current?.contains(e.target as Node)) setOpen(false) }
    // 捕获阶段先接住 Esc：不然它会接着去清画布上的运行结果、收属性面板
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || isComposing(e)) return
      e.preventDefault()
      e.stopPropagation()
      setOpen(false)
      wrap.current?.querySelector<HTMLElement>('[aria-haspopup="menu"]')?.focus()
    }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey, true)
    requestAnimationFrame(() => wrap.current?.querySelector<HTMLElement>('[role^="menuitem"]:not(:disabled)')?.focus())
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey, true)
    }
  }, [open])
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    e.preventDefault()
    const list = [...(wrap.current?.querySelectorAll<HTMLElement>('[role^="menuitem"]:not(:disabled)') ?? [])]
    const i = list.indexOf(document.activeElement as HTMLElement)
    list[(i + (e.key === 'ArrowDown' ? 1 : -1) + list.length) % list.length]?.focus()
  }
  return (
    <div ref={wrap} className="relative hidden shrink-0 @max-[880px]:block">
      <IconButton label="更多操作" variant="default" aria-haspopup="menu" aria-expanded={open}
                  aria-controls={open ? menuId : undefined} className={clsx(open && 'bg-hover')}
                  onClick={() => setOpen((v) => !v)} icon={<MoreHorizontal size={13} />} />
      {open && (
        <div id={menuId} role="menu" aria-label="更多操作" data-esc-layer onKeyDown={onKeyDown}
             className="fade-up absolute right-0 top-[calc(100%+6px)] z-50 w-48 rounded-lg border bg-panel p-1 shadow-elev-3">
          {items.map((it) => (
            <button key={it.id} type="button" role={it.checked == null ? 'menuitem' : 'menuitemcheckbox'}
                    aria-checked={it.checked} disabled={it.disabled} title={it.title}
                    className={clsx('flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-hover disabled:opacity-50',
                                    it.checked && 'bg-hover')}
                    onClick={() => { setOpen(false); it.onSelect() }}>
              <span className="shrink-0 text-dim">{it.icon}</span>
              <span className="min-w-0 flex-1 truncate">{it.label}</span>
              {it.shortcut && <span className="tnum shrink-0 text-2xs text-faint">{formatShortcut(studioShortcut(it.shortcut).combo)}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

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
function VersionLabel({ workflow: w }: { workflow: Workflow }) {
  const p = w.published_version
  // 画布就是已发布的那一版才算「在线」。老数据里有状态还挂着受管、版本却已经往前走了的
  // （旧的 restore 接口不退回草稿），那也得说清画布是草稿
  const live = (w.status === 'governed' || w.status === 'published') && p === w.version
  const ahead = p != null ? w.version - p : 0
  const pubWord = WORKFLOW_STATUS_LABEL[w.status === 'governed' ? 'governed' : 'published']
  const pubText = p != null ? `${pubWord} v${p}` : ''
  const title = [
    live ? `画布与${pubText}一致` : `画布是草稿 v${w.version}`,
    p != null && !live ? `正式运行使用已发布的 v${p}${ahead > 0 ? `，画布领先 ${ahead} 版` : ''}` : '',
    p == null ? '尚未发布，只能发起探索运行' : '',
    w.published_by ? `发布人：${w.published_by}` : '',
  ].filter(Boolean).join('\n')
  return (
    <span className="flex shrink-0 items-center overflow-hidden rounded-full border text-2xs leading-5" title={title}>
      {!live && <span className="tnum px-2 text-dim">草稿 v{w.version}</span>}
      {/* 最窄的工具栏里画布不是已发布那一版时只留「草稿 vN」：已发布的版本号在正式运行按钮上 */}
      {p != null && (
        <span className={clsx('tnum flex items-center gap-1 px-2', !live && 'border-l @max-[760px]:hidden')}
              style={{ color: 'var(--ok)', ...(w.status === 'governed' ? { background: 'var(--st-done-soft)' } : {}) }}>
          {/* 工具栏窄了只留盾牌和版本号，字留给读屏（全文在 title 里） */}
          <ShieldCheck size={10} /> <span><span className="@max-[880px]:sr-only">{pubWord} </span>v{p}</span>
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
              title="校验和变量分析请求失败，暂时无法判断该工作流能否运行。点击重试">
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
      title={hintOf(errors || warnings ? '打开问题面板，点击条目可定位到节点' : '打开问题面板', 'problems')}
      onClick={onClick}
    >
      {/* 工具栏窄了只留图标和数字：「错」「提示」两个字给读屏，数字和图标的颜色、形状照样分得开 */}
      {errors > 0 && (
        <span className="tnum flex items-center gap-0.5">
          <XCircle size={9} /> <span>{errors}<span className="@max-[880px]:sr-only"> 错</span></span>
        </span>
      )}
      {errors > 0 && warnings > 0 && <span className="text-faint">·</span>}
      {warnings > 0 && (
        <span className="tnum flex items-center gap-0.5" style={{ color: 'var(--warn)' }}>
          <AlertTriangle size={9} /> <span>{warnings}<span className="@max-[880px]:sr-only"> 提示</span></span>
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
  const text = copilotProgress(copilot.lastOp, copilot.phase)
  return (
    <div className="pointer-events-none absolute inset-x-0 top-3 z-20 flex justify-center px-3">
      <div className="fade-up pointer-events-auto flex max-w-full items-center gap-2.5 rounded-full border bg-panel py-1 pl-3 pr-1 text-2xs shadow-elev-2"
           style={{ borderColor: 'color-mix(in srgb, var(--copilot) 45%, var(--border))' }}>
        <Wand2 size={12} className="shrink-0" style={{ color: 'var(--copilot)' }} />
        {/* 播报区只圈住阶段文字：计时器 100ms 一跳，圈进来读屏就会一直念秒数 */}
        <span role="status" aria-live="polite" className="flex min-w-0 items-center gap-2.5">
          <span className="shrink-0 font-medium">助手正在修改工作流</span>
          <span className="min-w-0 truncate text-dim">{text}</span>
        </span>
        <span className="mono tnum shrink-0 text-faint" aria-hidden>{formatClock(now - started.current)}</span>
        <span className="shrink-0 text-faint">· 画布已锁定</span>
        <button type="button" className="btn btn-xs shrink-0 rounded-full" onClick={stopCopilot}
                title="停止本轮：画布恢复到本轮开始前的状态，已完成的部分可通过重做找回">
          <Square size={9} fill="currentColor" /> 停止
        </button>
      </div>
    </div>
  )
}
