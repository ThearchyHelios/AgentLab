import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  AlertTriangle, ArrowRight, Check, CheckCircle2, CornerDownRight, PenLine, RotateCw, ShieldCheck, Sparkles, Wand2,
  XCircle,
} from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import {
  EDIT_LOCK_TEXT, diffGraphs, editLockOf, toFlow, toGraph, useEditLock, useStudio, type GraphDiff,
} from '../store/studio'
import { useCatalog } from '../store/catalog'
import { Modal, Spinner, toast, useRadioGroup } from '../components/ui'
import { humanizeError } from '../lib/errors'
import { PUBLISH_FIX_TEXT as T, WORKFLOW_STATUS_HINT, WORKFLOW_STATUS_LABEL } from '../lib/terms'
import { isSqlCheckCode, sqlRuleLabel } from '../lib/sqlcheck'
import { setLocalActor, useLocalActor } from '../lib/actor'
import {
  autoFixIds, choiceReady, contentSig, fixFieldLabel, fixFor, fixValueLines, fixValueText, isMissingEndpoint, mergeIssues,
  normalizeAutofix, normalizeCheck, type NodeNameOf,
} from './issues'
import type { AutofixResult, PublishCheck, PublishFix, PublishLevel, ValidationIssue, Workflow } from '../types'

const LEVEL_DETAIL: Record<PublishLevel, string> = {
  published: '只需通过基础校验。正式运行将基于此版本发起，之后在画布上的修改不影响该版本',
  governed: '还需通过治理门禁：不得包含「多 Agent 协作」等全动态规划节点；子工作流须固定版本；'
    + '至少一个「成果」节点须声明出具契约；Agent 的危险工具须经人工审批',
}

/**
 * 发布弹窗。
 *
 * 等级的初值跟着工作流现在的等级走：以前写死「已发布」，对一张受管工作流直接点
 * 「发布」，后端照 level 改写状态，它就被悄悄降了级。选中态用底色 + 勾，不只靠一圈
 * 1px 描边。发布是要留痕的动作，所以写明以谁的名义发布；没署名就就地填。
 *
 * 打开就先做一遍发布前检查（和真正发布同一套口径），问题先列出来，能修的就地给修法：
 * 以前红字只能一条条抄进助手里描述一遍。修复一律先出预览，人点「应用并重新检查」才存草稿、
 * 重查；发布永远要人自己点。不修也照旧能点发布，门禁照旧拦。老后端没有检查接口时，
 * 退回原来的样子：点发布才知道被拦下了什么。
 */
export function PublishDialog({ workflow, onClose, onDone, onLocate }: {
  workflow: Workflow
  onClose: () => void
  onDone: () => void
  /** 定位一条问题：选中节点，能落到具体配置项（调用工具的 SQL）就落到那一项 */
  onLocate: (issue: ValidationIssue) => void
}) {
  const load = useStudio((s) => s.load)
  const dirty = useStudio((s) => s.dirty)
  const [level, setLevel] = useState<PublishLevel>(workflow.status === 'governed' ? 'governed' : 'published')
  const [busy, setBusy] = useState(false)
  /** 真的点了发布、被门禁拦下时它回的问题。和检查同一套口径，拦下之后以它为准 */
  const [gate, setGate] = useState<ValidationIssue[] | null>(null)
  // 署名只存在这台浏览器里（和请求头 X-Actor 是同一份，见 lib/actor）。存不进去（隐私模式）
  // 时这一次弹窗里先记着，照样写得进发布提示
  const stored = useLocalActor()
  const [unsaved, setUnsaved] = useState<string | null>(null)
  const actor = stored ?? unsaved ?? ''
  const [signing, setSigning] = useState('')
  const [delta, setDelta] = useState<GraphDiff | null>(null)
  const published = workflow.published_version
  const downgrade = workflow.status === 'governed' && level === 'published'
  const pf = usePreflight(workflow.id, level, { onRechecked: () => setGate(null) })

  // 换了等级：上一档被拦下的那张单子不再适用
  useEffect(() => { setGate(null) }, [level])

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
    // 写完会广播：导航底部的首字、别处的署名跟着变，不用等下一次获得焦点
    if (!setLocalActor(name)) setUnsaved(name)
  }

  const publish = async () => {
    setBusy(true)
    setGate(null)
    try {
      const res = await api.workflows.publish(workflow.id, level)
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
        setGate(normalizeCheck({ issues: res.issues }, level).issues)
        toast.warn('发布未通过门禁检查。请查看下方问题清单，点击条目可定位到对应节点')
      }
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  // 预览摆着、正在存、存失败了：这时发布的是还没修的那一版，先让人把修复这件事办完。
  // 存失败（或被锁没存上）时预览可能已经收起（换过等级），说「先应用或放弃预览」就不对了
  const held = unsavedFix(pf) ? T.unsaved : pf.pending ? T.pendingPreview : dirty ? T.unsaved : ''

  return (
    <Modal open onClose={onClose} title={`发布「${workflow.name}」v${workflow.version}`} width={560}
           footer={<>
             <button className="btn" onClick={onClose}>取消</button>
             <button className="btn btn-primary" onClick={publish} disabled={busy || !!held} title={held || undefined}
                     data-autofocus="" data-publish-submit="">
               {busy ? <Spinner size={12} /> : <ShieldCheck size={12} />}
               {level === 'governed' ? '发布为受管' : '发布'}
             </button>
           </>}>
      <div className="mb-3 grid grid-cols-2 gap-2" role="radiogroup" aria-label="发布等级">
        {(['published', 'governed'] as PublishLevel[]).map((l) => {
          const on = level === l
          return (
            <button
              key={l}
              type="button"
              role="radio"
              aria-checked={on}
              // 修复正在出预览、存草稿、重查时不换档：换档会把这一次的预览和状态清掉，
              // 在途的保存要是失败了，就没有地方再给「重试保存」
              disabled={pf.working}
              onClick={() => setLevel(l)}
              // 两张卡说明长短不一，grid 把它们拉成一样高；按钮默认把内容竖直居中，标题就对不齐了
              className={clsx('relative flex flex-col justify-start rounded-lg border px-3 py-2.5 text-left transition-colors',
                'disabled:cursor-not-allowed disabled:opacity-60',
                on ? 'border-[var(--accent)] bg-accent-soft' : 'enabled:hover:bg-hover')}
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
          当前为受管工作流。以「已发布」等级发布将降级，此后的正式运行不再受治理门禁约束。
        </div>
      )}

      <dl className="space-y-1.5 rounded-md border px-3 py-2 text-2xs">
        <div className="flex gap-2">
          <dt className="w-14 shrink-0 text-faint">当前版本</dt>
          <dd className="min-w-0 flex-1">
            v{workflow.version}
            {published != null && (
              <span className="text-faint">
                {published === workflow.version ? ' · 即当前已发布版本（重新发布仅变更等级）'
                  : delta ? ` · 相比已发布的 v${published}：${describe(delta)}`
                  : ` · 当前已发布版本为 v${published}`}
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
              <span>将以「<b className="font-semibold">{actor}</b>」的名义发布，并记入发布记录
                <Link to="/settings/prefs" className="ml-1.5 text-faint underline-offset-2 hover:underline">修改署名</Link>
              </span>
            ) : (
              <div>
                <div style={{ color: 'var(--warn)' }}>未署名：本次发布不会记录发布人</div>
                <div className="mt-1 flex items-center gap-1.5">
                  <input className="field h-7 py-0 text-xs" placeholder="填写发布人姓名" value={signing}
                         aria-label="署名" onChange={(e) => setSigning(e.target.value)}
                         onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); sign() } }} />
                  <button type="button" className="btn btn-sm shrink-0" disabled={!signing.trim()} onClick={sign}>
                    <PenLine size={11} /> 署名
                  </button>
                </div>
                <div className="mt-1 text-faint">署名仅保存在当前浏览器中，审批和正式运行共用；也可在「设置 → 偏好设置」中修改</div>
              </div>
            )}
          </dd>
        </div>
      </dl>

      <div className="mt-3" data-preflight="dialog">
        <Preflight pf={pf} gate={gate} onLocate={(issue) => { if (issue.node_id) { onLocate(issue); onClose() } }} />
      </div>
    </Modal>
  )
}

function describe(d: GraphDiff): string {
  if (!d.total) return '仅调整了节点位置'
  const parts = [
    d.added.length && `新增 ${d.added.length} 个节点`,
    d.removed.length && `删除 ${d.removed.length} 个节点`,
    d.changed.length && `修改了 ${d.changed.length} 个节点的配置`,
    (d.edgesAdded || d.edgesRemoved) && `连线变动 ${d.edgesAdded + d.edgesRemoved} 处`,
  ].filter(Boolean)
  return parts.join('、')
}

// -------------------------------------------------------------------------
// 发布前检查：发布弹窗和问题面板共用
// -------------------------------------------------------------------------

type CheckState = {
  status: 'idle' | 'loading' | 'ready' | 'unsupported' | 'error'
  /** 上一次查到的：重查期间照样列着，不闪成一片空白 */
  data?: PublishCheck
  /** data 是按哪一版画布内容查的（contentSig） */
  sig?: string
  error?: unknown
}

type PreviewState =
  | { status: 'loading'; assist: boolean }
  | { status: 'ready'; assist: boolean; result: AutofixResult; sig: string }
  | { status: 'error'; assist: boolean; error: unknown }
  | { status: 'unsupported'; assist: boolean }

type ApplyState =
  | { status: 'saving' | 'checking' | 'stale' }
  /** unsaved：修复已经落到画布上，要存时画布锁着、没存（重试保存时撞上的） */
  | { status: 'locked'; why: string; unsaved?: boolean }
  | { status: 'failed'; error: unknown }

type PreviewRequest = { apply: string[]; choices?: Record<string, unknown>; assist?: boolean }

export type PreflightState = ReturnType<typeof usePreflight>

const canvasGraph = () => {
  const s = useStudio.getState()
  return toGraph(s.nodes, s.edges)
}
const isAbort = (e: unknown) => (e as { name?: string } | null)?.name === 'AbortError'
const reasonOf = (e: unknown) => {
  const h = humanizeError(e)
  return h.reason ? `${h.title}：${h.reason}` : h.title
}
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)
/** 修复已经落到画布上、还没存上：存失败了，或者重试保存时画布锁着 */
const unsavedApply = (a: ApplyState) => a.status === 'failed' || (a.status === 'locked' && !!a.unsaved)
const unsavedFix = (pf: PreflightState) => !!pf.apply && unsavedApply(pf.apply)

/**
 * 节点 id → 名字和类型。跟着 issues 换新（改名、改配置之后校验都会重跑），不订阅 nodes：
 * 问题面板开着时拖一下节点，这一整块不必跟着每帧重渲染
 */
function useNodeLookup(): (id: string) => { label: string; type: string } | undefined {
  const tick = useStudio((s) => s.issues)
  const map = useMemo(() => new Map(useStudio.getState().nodes.map((n) => [n.id, { label: n.data.label, type: n.data.nodeType }])),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [tick])
  return useCallback((id: string) => map.get(id), [map])
}

/**
 * 发布前检查的状态：查、出修复预览、应用（落画布 → 走保存接口存草稿 → 重查）。
 * 查的一律是画布上此刻的图：发布弹窗只在没有未保存改动时打开得了，这时画布就是草稿；
 * 问题面板里画布可能改了没存，查的也正是眼前这张。接口只读，存草稿走的是现有的保存。
 */
export function usePreflight(workflowId: string | undefined, level: PublishLevel,
  { onRechecked }: { onRechecked?: () => void } = {}) {
  const [check, setCheck] = useState<CheckState>({ status: 'idle' })
  const [preview, setPreview] = useState<PreviewState | null>(null)
  const [apply, setApply] = useState<ApplyState | null>(null)
  const [choices, setChoices] = useState<Record<string, unknown>>({})
  const seq = useRef(0)
  const checkCtl = useRef<AbortController | null>(null)
  const previewCtl = useRef<AbortController | null>(null)
  const lastRequest = useRef<PreviewRequest | null>(null)
  /** 落到画布上、还没存上的那一次：重试保存时用同一句版本说明 */
  const pendingSave = useRef<{ labels: string[]; count: number } | null>(null)
  const rechecked = useRef(onRechecked)
  rechecked.current = onRechecked

  const runCheck = useCallback(async (fresh = false): Promise<PublishCheck | null> => {
    if (!workflowId) return null
    const n = ++seq.current
    checkCtl.current?.abort()
    const ctl = new AbortController()
    checkCtl.current = ctl
    const graph = canvasGraph()
    setCheck((c) => (fresh ? { status: 'loading' } : { ...c, status: 'loading', error: undefined }))
    try {
      const raw = await api.workflows.publishCheck(workflowId, { level, graph }, { signal: ctl.signal })
      if (n !== seq.current) return null
      const data = normalizeCheck(raw, level)
      setCheck({ status: 'ready', data, sig: contentSig(graph) })
      return data
    } catch (e) {
      if (n !== seq.current || isAbort(e)) return null
      setCheck(isMissingEndpoint(e) ? { status: 'unsupported' } : { status: 'error', error: e })
      return null
    }
  }, [workflowId, level])

  // 打开、换等级：从头查一遍，上一档的预览和选择都不再适用。只有「修复已经落到画布上、
  // 还没存上」这件事跨档留着：换了等级它照样没存，「重试保存」得一直看得见
  useEffect(() => {
    previewCtl.current?.abort()
    setPreview(null)
    setApply((a) => (pendingSave.current && a && unsavedApply(a) ? a : null))
    setChoices({})
    void runCheck(true)
    return () => {
      checkCtl.current?.abort()
      previewCtl.current?.abort()
    }
  }, [runCheck])

  const requestPreview = useCallback(async (req: PreviewRequest) => {
    if (!workflowId) return
    previewCtl.current?.abort()
    const ctl = new AbortController()
    previewCtl.current = ctl
    lastRequest.current = req
    const graph = canvasGraph()
    const assist = !!req.assist
    setApply(null)
    setPreview({ status: 'loading', assist })
    try {
      const raw = await api.workflows.autofix(workflowId, {
        level, graph, apply: req.apply,
        ...(req.choices && Object.keys(req.choices).length ? { choices: req.choices } : {}),
        ...(assist ? { assist: true } : {}),
      }, { signal: ctl.signal })
      if (ctl.signal.aborted) return
      setPreview({ status: 'ready', assist, result: normalizeAutofix(raw), sig: contentSig(graph) })
    } catch (e) {
      if (ctl.signal.aborted || isAbort(e)) return
      setPreview(isMissingEndpoint(e) ? { status: 'unsupported', assist } : { status: 'error', assist, error: e })
    }
  }, [workflowId, level])

  const discard = useCallback(() => {
    previewCtl.current?.abort()
    setPreview(null)
    setApply(null)
  }, [])

  /**
   * 存草稿，和页面自己的保存同一套规矩：画布锁着（助手在改、正式运行在跑）不存——助手改到一半
   * 的图存下去就是半成品；恢复旧版本留下的「回滚到 vN」不能被这一句盖掉，两句接着写
   */
  const save = useCallback(async () => {
    const what = pendingSave.current
    if (!what) return
    const studio = useStudio.getState()
    const lock = editLockOf(studio)
    if (lock) {
      setApply({ status: 'locked', why: EDIT_LOCK_TEXT[lock], unsaved: true })
      return
    }
    const pendingNote = studio.pendingNote.trim()
    const note = T.saveNote(what.labels)
    setApply({ status: 'saving' })
    try {
      await studio.save(pendingNote ? `${pendingNote}；${note}` : note)
    } catch (e) {
      setApply({ status: 'failed', error: e })
      return
    }
    pendingSave.current = null
    void useCatalog.getState().refresh()
    setApply({ status: 'checking' })
    await runCheck()
    rechecked.current?.()
    setPreview(null)
    setApply(null)
    toast.ok(T.applied(what.count, useStudio.getState().workflow?.version))
  }, [runCheck])

  const applyPreview = useCallback(async () => {
    if (preview?.status !== 'ready' || !preview.result.graph) return
    // 预览是按发请求那一刻的画布算的：之后又改过的话，套上去会把那几下盖掉
    if (contentSig(canvasGraph()) !== preview.sig) {
      setApply({ status: 'stale' })
      return
    }
    const { result } = preview
    const count = result.changes.length || result.applied.length
    const labels = result.changes.map((c) => c.label || fixLabelOf(check.data, c.fix_id)).filter(Boolean) as string[]
    if (!useStudio.getState().applyFixes(result.graph!, T.undoLabel(count))) {
      // 预览摆着的时候画布锁上了（正式运行开始、助手开始改）：落不下去，说清为什么
      const lock = editLockOf(useStudio.getState())
      setApply({ status: 'locked', why: lock ? EDIT_LOCK_TEXT[lock] : EDIT_LOCK_TEXT.formal })
      return
    }
    pendingSave.current = { labels: labels.length ? labels : [preview.assist ? T.assist : T.fixOne], count }
    await save()
  }, [preview, check.data, save])

  const retryPreview = useCallback(() => {
    if (lastRequest.current) void requestPreview(lastRequest.current)
  }, [requestPreview])

  const choose = useCallback((fix: PublishFix, value: unknown) => {
    setChoices((c) => {
      if (!fix.multiple) return { ...c, [fix.id]: value }
      const cur = Array.isArray(c[fix.id]) ? c[fix.id] as unknown[] : []
      const next = cur.some((v) => same(v, value)) ? cur.filter((v) => !same(v, value)) : [...cur, value]
      return { ...c, [fix.id]: next }
    })
  }, [])

  const working = preview?.status === 'loading' || apply?.status === 'saving' || apply?.status === 'checking'
  return {
    level, check, preview, apply, choices, working,
    /** 预览摆着、正在存、存失败（或被锁没存上）：发布按钮等这件事办完 */
    pending: preview?.status === 'loading' || preview?.status === 'ready'
      || apply?.status === 'saving' || apply?.status === 'checking' || (!!apply && unsavedApply(apply)),
    recheck: () => runCheck(),
    requestPreview, discard, applyPreview, retryPreview, retrySave: save, choose,
  }
}

function fixLabelOf(data: PublishCheck | undefined, id: string): string {
  return data?.fixes.find((f) => f.id === id)?.label ?? ''
}

/**
 * 发布前检查的整块：一句结论（+ 一键修复）、修复预览、问题清单（每条旁边给修法）。
 * gate：真点了发布、被门禁拦下时回的那张单子，有它就列它。
 */
export function Preflight({ pf, gate, onLocate, stale }: {
  pf: PreflightState
  gate?: ValidationIssue[] | null
  onLocate: (issue: ValidationIssue) => void
  /** 问题面板：查完之后画布又改过 */
  stale?: boolean
}) {
  const nodeOf = useNodeLookup()
  const lock = useEditLock()
  const { check } = pf
  const fixes = check.data?.fixes ?? []
  const issues = gate ?? check.data?.issues ?? null
  // validate 和门禁对同一处各报一条：合成一行，计数也按一处算
  const rows = useMemo(() => (issues ? mergeIssues(issues) : null), [issues])
  const errors = (rows ?? []).filter((r) => r.issue.level === 'error').length
  const warns = (rows ?? []).length - errors
  const canFix = !lock && check.status !== 'unsupported'
  const autoIds = issues ? autoFixIds(issues, fixes) : []
  const labelOf = (id?: string | null) => (id ? nodeOf(id)?.label || id : '')
  const nameOf: NodeNameOf = (id) => nodeOf(id)?.label || undefined
  const anyFix = !!issues?.some((i) => fixFor(i, fixes))
  // 同一个修复只画一次控件：挂在第一条指向它的问题上（几条问题共用一个修复时，比如全图默认
  // 一改、跟随它的几个节点一起好了），别处再画一组就是两个共用状态的「预览」
  const drawn = new Set<string>()
  const fixOf = (issue: ValidationIssue): PublishFix | undefined => {
    const fix = canFix ? fixFor(issue, fixes) : undefined
    if (!fix || drawn.has(fix.id)) return undefined
    drawn.add(fix.id)
    return fix
  }

  return (
    <div className="space-y-2 text-2xs" data-preflight-status={check.status}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        {check.status === 'loading' && !check.data && !gate && (
          <span className="flex items-center gap-1.5 text-faint"><Spinner size={11} /> {T.checking}</span>
        )}
        {check.status === 'unsupported' && !gate && (
          <span className="text-faint" data-preflight-unsupported="">{T.unsupported}</span>
        )}
        {check.status === 'error' && !gate && (
          <>
            <span style={{ color: 'var(--warn)' }}>{T.failed}：{reasonOf(check.error)}</span>
            <button type="button" className="btn btn-xs" onClick={() => void pf.recheck()}>
              <RotateCw size={10} aria-hidden /> {T.retry}
            </button>
          </>
        )}
        {issues && (
          <span className="flex min-w-0 items-center gap-1.5 font-medium" data-preflight-summary=""
                style={{ color: errors ? 'var(--err)' : warns ? 'var(--warn)' : 'var(--ok)' }}>
            {errors ? <XCircle size={12} className="shrink-0" aria-hidden />
              : <CheckCircle2 size={12} className="shrink-0" aria-hidden />}
            <span className="min-w-0">
              {gate ? T.gateBlocked(errors) : errors ? T.blocked(errors) : T.passed}
              {warns > 0 && <span className="font-normal text-faint">{errors || gate ? '，' : '；'}{T.warnings(warns)}</span>}
            </span>
          </span>
        )}
        {check.status === 'loading' && (check.data || gate) && <Spinner size={11} />}
        <span className="flex-1" />
        {canFix && autoIds.length > 0 && (
          <button type="button" className="btn btn-xs" data-fix-all={autoIds.length} disabled={pf.working}
                  onClick={() => void pf.requestPreview({ apply: autoIds })}>
            <Wand2 size={10} aria-hidden /> {T.fixAll(autoIds.length)}
          </button>
        )}
      </div>
      {stale && check.status === 'ready' && (
        <div className="flex items-center gap-1.5" style={{ color: 'var(--st-waiting)' }} data-preflight-stale="">
          <AlertTriangle size={11} className="shrink-0" aria-hidden /> {T.stale}
        </div>
      )}
      {lock && anyFix && (
        <div className="text-faint" data-preflight-locked="">{T.locked(EDIT_LOCK_TEXT[lock])}</div>
      )}

      {pf.preview && <FixPreview pf={pf} labelOf={labelOf} nameOf={nameOf}
                                 typeOf={(id) => (id ? nodeOf(id)?.type : undefined)} />}
      {/* 修复落到画布上却没存上，预览又已经收起了（换过等级）：「重试保存」照样得在 */}
      {pf.preview?.status !== 'ready' && unsavedFix(pf) && <SaveTrouble pf={pf} />}

      {rows && !!rows.length && (
        <ul className="space-y-0.5" aria-label="发布前检查的问题">
          {rows.map(({ issue, others }, i) => (
            <PreflightRow key={`${issue.code ?? ''}:${issue.node_id ?? ''}:${i}`} issue={issue} others={others}
                          fix={fixOf(issue)} where={labelOf(issue.node_id)}
                          pf={pf} onLocate={() => onLocate(issue)} />
          ))}
        </ul>
      )}
    </div>
  )
}

function PreflightRow({ issue, others, fix, where, pf, onLocate }: {
  issue: ValidationIssue; others: string[]; fix?: PublishFix; where: string; pf: PreflightState; onLocate: () => void
}) {
  const err = issue.level === 'error'
  const Icon = err ? XCircle : AlertTriangle
  return (
    <li data-preflight-issue={issue.code ?? ''} data-fix-kind={fix?.kind}>
      <button
        type="button"
        disabled={!issue.node_id}
        onClick={onLocate}
        className={clsx('flex w-full items-start gap-1.5 rounded px-1.5 py-1 text-left leading-snug',
          issue.node_id && 'hover:bg-hover')}
        // 合成一行的那几条的其他说法：悬停看得到，不在清单里重复一遍
        title={[issue.node_id ? '定位到该节点' : '', ...others].filter(Boolean).join('\n') || undefined}
      >
        <Icon size={11} className="mt-px shrink-0" style={{ color: err ? 'var(--err)' : 'var(--warn)' }}
              aria-label={err ? '错误' : '提示'} />
        <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">
          {where && !String(issue.message).includes(`「${where}」`) && <b className="font-semibold">「{where}」</b>}
          {/* 对照数据目录的 SQL 检查：先写中文规则名，不露规则编号 */}
          {isSqlCheckCode(issue.code) && <span className="font-medium" data-preflight-rule="">{sqlRuleLabel(issue.code)}：</span>}
          {issue.message}
        </span>
        {issue.node_id && <CornerDownRight size={10} className="mt-px shrink-0 text-faint" aria-hidden />}
      </button>
      {fix && <FixControl fix={fix} pf={pf} />}
    </li>
  )
}

/** 一条问题的修法：auto 一个「修复」，choice 一组选项（不替人选），assist 交给 Copilot */
function FixControl({ fix, pf }: { fix: PublishFix; pf: PreflightState }) {
  const value = pf.choices[fix.id]
  if (fix.kind === 'choice') {
    const ready = choiceReady(fix, value)
    const picked = (v: unknown) => (fix.multiple
      ? Array.isArray(value) && value.some((x) => same(x, v))
      : value !== undefined && same(value, v))
    // 选的是「交给 Copilot」那一项：和点「交给 Copilot」一回事，请求带 assist，等待时说 Copilot 在修
    const handoff = !fix.multiple && (fix.options ?? []).some((o) => o.handoff === true && picked(o.value))
    return (
      <fieldset className="mb-1 ml-[22px] mt-0.5 rounded border px-2 py-1.5" data-fix-choice={fix.id}>
        <legend className="px-1 text-faint">{fix.label}{fix.multiple ? ` · ${T.multiple}` : ''}</legend>
        <div className="flex flex-wrap gap-x-3 gap-y-1">
          {(fix.options ?? []).map((o, k) => (
            <label key={k} className="inline-flex min-w-0 cursor-pointer items-start gap-1.5" title={o.hint ?? undefined}>
              <input type={fix.multiple ? 'checkbox' : 'radio'} name={`fix-${fix.id}`} className="mt-0.5 accent-[var(--accent)]"
                     checked={picked(o.value)} disabled={pf.working} onChange={() => pf.choose(fix, o.value)} />
              <span className="min-w-0 [overflow-wrap:anywhere]">
                {o.label}
                {fix.default !== undefined && (fix.multiple
                  ? Array.isArray(fix.default) && fix.default.some((d) => same(d, o.value))
                  : same(fix.default, o.value)) && (
                  <span className="ml-1 rounded border px-1 text-faint">{T.suggested}</span>
                )}
                {o.hint && <span className="block text-faint">{o.hint}</span>}
              </span>
            </label>
          ))}
        </div>
        <div className="mt-1.5 flex items-center gap-2">
          <button type="button" className="btn btn-xs" data-fix-action="choice" disabled={!ready || pf.working}
                  data-fix-handoff={handoff ? '' : undefined} title={handoff ? T.assistHint : undefined}
                  onClick={() => void pf.requestPreview({
                    apply: [fix.id], choices: { [fix.id]: value }, ...(handoff ? { assist: true } : {}) })}>
            {handoff ? <Sparkles size={10} aria-hidden /> : <Wand2 size={10} aria-hidden />} {handoff ? T.assist : T.choose}
          </button>
          {!ready && <span className="text-faint">{T.chooseFirst}</span>}
        </div>
      </fieldset>
    )
  }
  const assist = fix.kind === 'assist'
  return (
    <div className="mb-1 ml-[22px] flex flex-wrap items-center gap-x-2 gap-y-0.5">
      <button type="button" className="btn btn-xs shrink-0" data-fix-action={fix.kind} disabled={pf.working}
              title={assist ? T.assistHint : T.fixHint(fix.label)}
              onClick={() => void pf.requestPreview(assist ? { apply: [], assist: true } : { apply: [fix.id] })}>
        {assist ? <Sparkles size={10} aria-hidden /> : <Wand2 size={10} aria-hidden />} {assist ? T.assist : T.fixOne}
      </button>
      <span className="min-w-0 text-faint [overflow-wrap:anywhere]">{fix.label}</span>
    </div>
  )
}

/** 修复预览：逐项「节点 · 字段：原值 → 新值」，没采用的写原因，Copilot 的问题原样摆出来 */
function FixPreview({ pf, labelOf, nameOf, typeOf }: {
  pf: PreflightState; labelOf: (id?: string | null) => string; nameOf: NodeNameOf
  typeOf: (id?: string | null) => string | undefined
}) {
  const dirty = useStudio((s) => s.dirty)
  const p = pf.preview!
  const box = 'rounded-md border px-2.5 py-2'
  // 点的可能是清单最底下那一条的「修复」：预览在清单上面出来，得让人看得见
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => { ref.current?.scrollIntoView({ block: 'nearest' }) }, [p.status])
  if (p.status === 'loading') {
    return (
      <div ref={ref} className={clsx(box, 'flex items-center gap-2')} data-fix-preview="loading" aria-live="polite">
        <Spinner size={11} />
        <span className="min-w-0 flex-1 text-dim">{p.assist ? T.assisting : T.previewing}</span>
        <button type="button" className="btn btn-xs" onClick={pf.discard}>{T.stop}</button>
      </div>
    )
  }
  if (p.status === 'unsupported' || p.status === 'error') {
    return (
      <div ref={ref} className={clsx(box, 'flex flex-wrap items-center gap-2')} style={{ borderColor: 'var(--warn)' }}
           data-fix-preview={p.status} role="alert">
        <AlertTriangle size={11} className="shrink-0" style={{ color: 'var(--warn)' }} aria-hidden />
        <span className="min-w-0 flex-1" style={{ color: 'var(--warn)' }}>
          {p.status === 'unsupported' ? T.autofixMissing : `${T.autofixFailed}：${reasonOf(p.error)}`}
        </span>
        {p.status === 'error' && (
          <button type="button" className="btn btn-xs" onClick={pf.retryPreview}><RotateCw size={10} aria-hidden /> {T.retry}</button>
        )}
        <button type="button" className="btn btn-xs" onClick={pf.discard}>{T.discard}</button>
      </div>
    )
  }
  const { result } = p
  const applying = pf.apply?.status === 'saving' || pf.apply?.status === 'checking'
  const unsaved = unsavedFix(pf)
  const left = result.remaining.filter((i) => i.level === 'error').length
  const usable = !!result.graph && (result.changes.length > 0 || result.applied.length > 0)
  return (
    <section ref={ref} className={clsx(box, 'space-y-1.5')} style={{ borderColor: 'var(--accent)' }}
             data-fix-preview="ready" aria-label="修复预览">
      <div className="font-medium">{result.changes.length ? T.previewTitle(result.changes.length) : T.noChange}</div>
      {!!result.changes.length && (
        <ul className="space-y-1" data-fix-changes="">
          {result.changes.map((c, i) => {
            const where = c.node_title || labelOf(c.node_id) || T.whole
            const field = fixFieldLabel(c.field, typeOf(c.node_id))
            return (
              <li key={i} className="leading-snug" data-fix-change={c.fix_id}>
                <span className="[overflow-wrap:anywhere]">
                  <b className="font-semibold">「{where}」</b>{field && ` · ${field}`}：
                  <FixValue value={c.before} field={c.field} nameOf={nameOf} className="text-faint line-through" data-fix-before="" />
                  <ArrowRight size={10} className="mx-1 inline align-[-1px] text-faint" aria-label="改为" />
                  <FixValue value={c.after} field={c.field} nameOf={nameOf} className="font-medium" data-fix-after="" />
                </span>
                {c.label && <span className="block text-faint">{c.label}</span>}
              </li>
            )
          })}
        </ul>
      )}
      {!!result.rejected.length && (
        <div data-fix-rejected="">
          <div className="text-faint">{T.rejected}</div>
          <ul className="space-y-0.5">
            {result.rejected.map((r, i) => (
              <li key={i} className="flex items-start gap-1 [overflow-wrap:anywhere]" style={{ color: 'var(--warn)' }}>
                <AlertTriangle size={10} className="mt-0.5 shrink-0" aria-hidden />
                <span className="min-w-0">
                  {r.fix_id === 'assist' ? T.assistSaid : fixLabelOf(pf.check.data, r.fix_id) || r.fix_id}：{r.reason}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {result.assist && (result.assist.summary || !!result.assist.questions?.length) && (
        <div className="space-y-1" data-fix-assist="">
          {result.assist.summary && (
            <div className="flex items-start gap-1 [overflow-wrap:anywhere]">
              <Sparkles size={10} className="mt-0.5 shrink-0 text-faint" aria-hidden />
              <span className="min-w-0"><span className="text-faint">{T.assistSaid}：</span>{result.assist.summary}</span>
            </div>
          )}
          {!!result.assist.questions?.length && (
            <div className="rounded border px-2 py-1" style={{ borderColor: 'var(--st-waiting)' }} data-fix-questions="">
              <div style={{ color: 'var(--st-waiting)' }}>{T.questions}</div>
              <ul className="list-disc pl-4">
                {result.assist.questions.map((q, i) => <li key={i} className="[overflow-wrap:anywhere]">{q}</li>)}
              </ul>
            </div>
          )}
        </div>
      )}
      {usable && (
        <div style={{ color: result.ok ? 'var(--ok)' : 'var(--warn)' }} data-fix-after-check="">
          {result.ok ? T.afterOk : T.afterLeft(left)}
        </div>
      )}

      {pf.apply?.status === 'stale' && <div role="alert" style={{ color: 'var(--warn)' }}>{T.stalePreview}</div>}
      {pf.apply?.status === 'locked' && !pf.apply.unsaved && (
        <div role="alert" style={{ color: 'var(--warn)' }}>{T.locked(pf.apply.why)}</div>
      )}
      {unsaved && <SaveTrouble pf={pf} />}
      {!unsaved && (
        <div className="flex flex-wrap items-center gap-2 pt-0.5">
          <span className="min-w-0 flex-1 text-faint">
            {applying ? (pf.apply?.status === 'saving' ? T.saving : T.rechecking)
              : usable ? `${T.applyNote}${dirty ? `。${T.dirtyNote}` : ''}` : ''}
          </span>
          <button type="button" className="btn btn-xs" onClick={pf.discard} disabled={applying}>{T.discard}</button>
          {usable && (
            <button type="button" className="btn btn-xs btn-primary" data-fix-apply="" disabled={applying || pf.apply?.status === 'stale'}
                    onClick={() => void pf.applyPreview()}>
              {applying ? <Spinner size={10} /> : <Check size={10} aria-hidden />} {T.apply}
            </button>
          )}
        </div>
      )}
    </section>
  )
}

/**
 * 预览里的一个值。整份契约（生成契约骨架）一键一行地写，别的压成一行；节点引用写节点名
 */
function FixValue({ value, field, nameOf, className, ...data }: {
  value: unknown; field?: string | null; nameOf: NodeNameOf; className?: string
  'data-fix-before'?: string; 'data-fix-after'?: string
}) {
  const lines = fixValueLines(value, field, nameOf)
  if (!lines) return <span className={className} {...data}>{fixValueText(value, field, nameOf)}</span>
  return (
    <span className={clsx(className, 'block pl-3')} {...data}>
      {lines.map((line, i) => <span key={i} className="block">{line}</span>)}
    </span>
  )
}

/**
 * 修复已经落到画布上、还没存上：存失败了，或者重试保存时画布锁着（助手在改、正式运行在跑）。
 * 说清楚为什么，给「重试保存」。预览里有一份；预览收起了（换过等级）就在清单上面单独一份
 */
function SaveTrouble({ pf }: { pf: PreflightState }) {
  const a = pf.apply
  if (!a || !unsavedApply(a)) return null
  const failed = a.status === 'failed'
  return (
    <div role="alert" className="flex flex-wrap items-center gap-2" style={{ color: failed ? 'var(--err)' : 'var(--warn)' }}
         data-fix-save-failed={failed ? '' : 'locked'}>
      <span className="min-w-0 flex-1">
        {failed ? `${T.saveFailed}：${reasonOf(a.error)}` : a.status === 'locked' ? `${T.saveFailed}（${a.why}）` : ''}。
        {T.saveFailedHint}
      </span>
      <button type="button" className="btn btn-xs" onClick={() => void pf.retrySave()}>
        <RotateCw size={10} aria-hidden /> {T.retrySave}
      </button>
    </div>
  )
}

const LEVELS = ['published', 'governed'] as const

/**
 * 问题面板里的「发布前检查」：选一档，查一遍，同一套修法。画布上就能提前发现、提前修，
 * 不用等到点发布那一刻。点一条照样定位到节点
 */
export function PublishPreflightPane({ onLocate }: { onLocate: (issue: ValidationIssue) => void }) {
  const workflow = useStudio((s) => s.workflow)
  const [level, setLevel] = useState<PublishLevel>(workflow?.status === 'governed' ? 'governed' : 'published')
  // 修复正在出预览、存草稿、重查时不换档（和发布弹窗同一条）：方向键也换不了
  const busy = useRef(false)
  const pick = useCallback((l: PublishLevel) => { if (!busy.current) setLevel(l) }, [])
  const radio = useRadioGroup(LEVELS, level, pick)
  const pf = usePreflight(workflow?.id, level)
  busy.current = pf.working
  // 查完之后画布又改了：结论可能已经过时。跟着 issues 重算（每次改图后校验都会换新），
  // 不订阅 nodes——那样拖一帧就要把整张图序列化一遍
  const tick = useStudio((s) => s.issues)
  const sig = pf.check.sig
  const stale = useMemo(() => sig != null && contentSig(canvasGraph()) !== sig, [tick, sig])
  if (!workflow) return null
  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2" data-preflight="panel">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-2xs">
        <div role="radiogroup" aria-label="检查等级" className="inline-flex rounded-md border p-px">
          {LEVELS.map((l) => (
            <button key={l} type="button" {...radio(l)} onClick={() => pick(l)} disabled={pf.working}
                    className={clsx('rounded px-2 py-0.5 disabled:cursor-not-allowed disabled:opacity-60',
                      level === l ? 'bg-accent-soft text-fg' : 'text-faint enabled:hover:text-dim')}>
              {WORKFLOW_STATUS_LABEL[l]}
            </button>
          ))}
        </div>
        <span className="flex-1" />
        <button type="button" className="btn btn-xs" onClick={() => void pf.recheck()}
                disabled={pf.check.status === 'loading' || pf.working}>
          <RotateCw size={10} aria-hidden /> {T.recheck}
        </button>
      </div>
      <Preflight pf={pf} onLocate={onLocate} stale={stale} />
    </div>
  )
}
