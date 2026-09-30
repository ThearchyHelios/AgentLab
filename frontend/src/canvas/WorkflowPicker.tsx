import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Check, Copy, LayoutTemplate, Plus, Search, Trash2, Workflow as WorkflowIcon } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { useCatalog } from '../store/catalog'
import { useStudio } from '../store/studio'
import { EmptyState, IconButton, Modal, confirmDialog, promptDialog, toast } from '../components/ui'
import { formatDateTime, formatTime, parseServerTime } from '../lib/format'
import { formatShortcut } from '../lib/keys'
import { leavePass, registerLeaveGuard } from '../lib/leave'
import { WORKFLOW_STATUS_LABEL } from '../lib/terms'
import type { GraphSpec, Workflow } from '../types'

/** 标签的中文。后端写的是机器标记，和中文界面混排很突兀 */
const TAG_LABEL: Record<string, string> = { extracted: '从运行提取', template: '模板' }

const pad2 = (n: number) => String(n).padStart(2, '0')
const stamp = (now = new Date()) =>
  `${pad2(now.getMonth() + 1)}-${pad2(now.getDate())} ${pad2(now.getHours())}:${pad2(now.getMinutes())}`
/** 新建的默认名：「未命名工作流 09-26 10:05」。以前统一叫「新工作流」，列表里一排同名的 */
export function defaultWorkflowName(now = new Date()): string {
  return `未命名工作流 ${stamp(now)}`
}

/**
 * 已经问过「放弃改动吗」：带着这个标记换图，画布那边不再问第二遍。leavePass 让外壳的
 * 离开守卫（见下面的 discardGuard）也放行这一跳
 */
type Go = (to: string, opts?: { replace?: boolean; state?: unknown }) => void
const DISCARDED = leavePass({ state: { discard: true } })

const BLANK: GraphSpec = {
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
}

/**
 * 有未保存的改动时，换图之前问一句。全局有撤销，但撤销栈属于这张图——换走就没了。
 * 返回 true 表示可以换。
 */
export async function confirmDiscard(): Promise<boolean> {
  const { dirty, workflow } = useStudio.getState()
  if (!dirty) return true
  return confirmDialog({
    // 可能是在别的页上问的（记录页打开另一张工作流）：说出是哪一张的改动
    title: workflow ? `放弃「${workflow.name}」未保存的改动？` : '放弃画布上未保存的改动？',
    consequences: ['切换后，这些修改和撤销记录都将丢失', `如需保留，请先取消，回到画布按 ${formatShortcut('Mod+S')} 保存`],
    confirmLabel: '放弃并切换',
    danger: true,
  })
}

/**
 * 守卫里人已经说了「放弃并切换」的那一跳。地址落到那儿之后编排页的 URL→画布不再问
 * 第二遍：takeDiscarded(pathname) 取一次就清掉。
 *
 * 先建再跳的（canLeave('/studio/') 问过，建完 navigate(`/studio/${新 id}`, leavePass())）
 * 问的时候还没有新 id，记下的只是 '/studio/'：它认接下来打开的任何一张。只认完全相同的
 * 路径的话，照着这个写法做反而要问两遍——第二遍点取消，刚建的那张就成了孤儿
 */
const ANY_WORKFLOW = '/studio/'
let discardedFor: string | null = null
export function takeDiscarded(pathname: string): boolean {
  const hit = discardedFor === pathname || (discardedFor === ANY_WORKFLOW && pathname.startsWith(ANY_WORKFLOW))
  if (hit) discardedFor = null
  return hit
}

/**
 * 画布有没保存的改动时，换到另一张工作流先问——在地址跳之前问。以前是编排页的 URL→画布
 * 事后才问：浏览器后退、⌘K、记录页的「在画布中打开」已经把地址换成了那一张，取消时再
 * replace 回来，从记录页过来的连 ?run= 也丢了。
 *
 * 跟着 dirty 登记在模块级，不是编排页挂着时才登记：画布在 store 里，离开编排页它也还在，
 * 从别的页打开另一张工作流一样会把它换掉。去别的页不拦（只认 /studio/<别的 id>）；关页、
 * 刷新由 lib/leave 让浏览器问一句。要先建东西再跳的（从问数据页建一张再打开），先
 * canLeave('/studio/') 问过、跳的时候带 leavePass()，整条路只问这一遍（见 takeDiscarded）
 */
const discardGuard = (): (() => void) => registerLeaveGuard({
  blocks: (next) => {
    const id = useStudio.getState().workflow?.id
    return next != null && next.pathname.startsWith('/studio/') && next.pathname !== `/studio/${id}`
  },
  confirm: async (next) => {
    const ok = await confirmDiscard()
    // 这一问说了取消，就别让上一问记下的还作数
    discardedFor = ok && next ? next.pathname : null
    return ok
  },
})
let unguard: (() => void) | null = null
const stopWatching = useStudio.subscribe((s) => {
  if (s.dirty === (unguard != null)) return
  if (s.dirty) {
    unguard = discardGuard()
  } else {
    unguard?.()
    unguard = null
    discardedFor = null
  }
})
// 热更新换上这个模块时画布可能已经带着没存的改动：订阅要等下一次变化才登记，先补上
if (useStudio.getState().dirty) unguard = discardGuard()
// 热更新换掉这个模块时撤掉旧的那道：两道守卫会把同一句问两遍
import.meta.hot?.dispose(() => { stopWatching(); unguard?.() })

/**
 * 新建工作流。起点可以是空白（输入 → 成果），也可以是一张模板。
 *
 * 模板走 duplicate：后端复制时会带上图和标签，然后改成用户起的名字。以前「复制一份」
 * 只在悬停时出现，命名为「副本」，复制完也不跳过去
 */
export async function createWorkflow(
  navigate: Go, refresh: () => Promise<void> | void, from?: Workflow,
): Promise<boolean> {
  const names = new Set(useCatalog.getState().workflows.map((w) => w.name))
  const name = await promptDialog({
    title: from ? `用「${from.name}」新建工作流` : '新建工作流',
    body: from ? '将复制该模板的节点和配置，你可以在此基础上修改。模板本身不受影响。' : '从「输入 → 成果」两个节点开始。',
    label: '名称',
    initial: from ? `${from.name.replace(/^[①-⑳]\s*/, '')} ${stamp()}` : defaultWorkflowName(),
    confirmLabel: '新建并打开',
    validate: (v) => (names.has(v) ? '已存在同名工作流，请使用其他名称' : null),
  })
  if (!name) return false
  if (!(await confirmDiscard())) return false
  try {
    let created: Workflow
    if (from) {
      const copy = await api.workflows.duplicate(from.id)
      created = await api.workflows.update(copy.id, { name })
    } else {
      created = await api.workflows.create({ name, graph: BLANK })
    }
    // 走地址而不是直接 load：新图也得有自己的 URL，否则刚建完刷新一下就回到了第一张
    navigate(`/studio/${created.id}`, DISCARDED)
    void refresh()
    toast.ok(`已创建「${created.name}」`)
    return true
  } catch (e) {
    toast.error(e)
    return false
  }
}

function StatusChip({ w }: { w: Workflow }) {
  const p = w.published_version
  const pub = WORKFLOW_STATUS_LABEL[w.status === 'governed' ? 'governed' : 'published']
  // 和工具栏的版本标签同一个口径：最新一版就是发布的那版才算「已发布」。发布后又保存过，
  // 状态退回草稿，但已发布的 vN 还在、正式运行照样跑它——只写「草稿」就把这件事藏起来了
  if ((w.status === 'governed' || w.status === 'published') && p === w.version) {
    return (
      <span className="chip shrink-0" style={{ color: 'var(--ok)', borderColor: w.status === 'governed' ? 'var(--ok)' : undefined }}>
        {pub} v{p}
      </span>
    )
  }
  return (
    <>
      <span className="chip shrink-0">{WORKFLOW_STATUS_LABEL.draft}</span>
      {p != null && (
        <span className="chip tnum shrink-0 text-faint" title={`正式运行使用${pub}的 v${p}`}>{pub} v{p}</span>
      )}
    </>
  )
}

type Filter = 'all' | 'mine' | 'templates'

/**
 * 选择工作流：搜索、分「我的 / 模板」、显示状态和更新时间；模板行常驻「用它新建」。
 * ↑↓ 移动、⏎ 打开。复制和删除常驻（淡色），不再只在悬停时出现。
 */
export function WorkflowPicker({ open, onClose }: { open: boolean; onClose: () => void }) {
  // 逐个取：整个 catalog 订阅下来，连接心跳每跳一次开着的选择器都要重算一遍列表
  const workflows = useCatalog((s) => s.workflows)
  const refresh = useCatalog((s) => s.refresh)
  const navigate = useNavigate()
  const current = useStudio((s) => s.workflow)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const [cursor, setCursor] = useState(0)
  const listRef = useRef<HTMLUListElement>(null)

  useEffect(() => { if (open) { setQuery(''); setCursor(0) } }, [open])

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase()
    const at = (w: Workflow) => parseServerTime(w.updated_at ?? '')?.getTime() ?? 0
    return workflows
      .filter((w) => filter === 'all' || (filter === 'templates') === !!w.is_template)
      .filter((w) => !q || w.name.toLowerCase().includes(q) || (w.description ?? '').toLowerCase().includes(q)
        || (w.tags ?? []).some((t) => (TAG_LABEL[t] ?? t).toLowerCase().includes(q)))
      // 最近改过的在上面：要找的多半是刚才在编的那张
      .sort((a, b) => at(b) - at(a))
  }, [workflows, query, filter])

  useEffect(() => { setCursor(0) }, [query, filter])
  useEffect(() => {
    listRef.current?.querySelector(`[data-index="${cursor}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [cursor])

  const openOne = async (w: Workflow) => {
    if (w.id === current?.id) { onClose(); return }
    // 闸放在导航**之前**：地址一变，URL→画布那个 effect 就会加载，那时候再问就晚了
    if (!(await confirmDiscard())) return
    navigate(`/studio/${w.id}`, DISCARDED)
    onClose()
  }

  const remove = async (w: Workflow) => {
    const ok = await confirmDialog({
      title: `删除工作流「${w.name}」？`,
      consequences: [
        '工作流及其全部版本将被删除，且无法恢复',
        w.run_count ? `${w.run_count} 条运行记录将保留，但不再关联到该工作流` : '该工作流尚无运行记录',
      ],
      confirmLabel: '删除工作流',
      danger: true,
    })
    if (!ok) return
    try {
      await api.workflows.remove(w.id)
      toast.ok(`已删除「${w.name}」`)
      await refresh()
      // 删的就是眼前这张：画布和地址都还是它，接着按保存就是往一张不存在的图上写。
      // 只改地址不够——/studio 的「落到第一张」在画布上有节点时不跳（那是给问数据页
      // 送来的草稿留的路），所以直接换到剩下的一张；一张不剩就带上 unload，让编排页
      // 在地址落到 /studio 之后把画布卸空。目录要先刷完：拿旧目录挑「下一张」可能又
      // 挑回刚删的那张
      if (w.id === useStudio.getState().workflow?.id) {
        useStudio.setState({ dirty: false })
        const next = useCatalog.getState().workflows.find((x) => x.id !== w.id)
        if (next) navigate(`/studio/${next.id}`, { replace: true, ...DISCARDED })
        else navigate('/studio', { replace: true, state: { discard: true, unload: w.id } })
      }
    } catch (e) {
      toast.error(e)
    }
  }

  const duplicate = async (w: Workflow) => {
    try {
      const copy = await api.workflows.duplicate(w.id)
      await refresh()
      toast.ok(`已复制为「${copy.name}」`, { action: { label: '打开', onClick: () => void openOne(copy) } })
    } catch (e) {
      toast.error(e)
    }
  }

  const counts = {
    all: workflows.length,
    mine: workflows.filter((w) => !w.is_template).length,
    templates: workflows.filter((w) => w.is_template).length,
  }

  return (
    <Modal open={open} onClose={onClose} title="工作流" width={720}
           footer={<>
             <span className="mr-auto self-center text-2xs text-faint">↑↓ 选择 · {formatShortcut('Enter')} 打开 · Esc 关闭</span>
             <button className="btn btn-primary" onClick={async () => { if (await createWorkflow(navigate, refresh)) onClose() }}>
               <Plus size={12} /> 新建空白工作流
             </button>
           </>}>
      <div className="mb-2 flex items-center gap-2">
        <div className="relative flex-1">
          <Search size={12} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" />
          <input
            className="field pl-7"
            placeholder="按名称、说明、标签搜索…"
            value={query}
            data-autofocus=""
            aria-label="搜索工作流"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.nativeEvent.isComposing) return
              if (e.key === 'ArrowDown') { e.preventDefault(); setCursor((c) => Math.min(shown.length - 1, c + 1)) }
              else if (e.key === 'ArrowUp') { e.preventDefault(); setCursor((c) => Math.max(0, c - 1)) }
              else if (e.key === 'Enter' && shown[cursor]) { e.preventDefault(); void openOne(shown[cursor]) }
            }}
          />
        </div>
        <div className="flex shrink-0 rounded-md border p-0.5" role="tablist" aria-label="筛选">
          {([['all', '全部'], ['mine', '我的'], ['templates', '模板']] as [Filter, string][]).map(([k, label]) => (
            <button key={k} type="button" role="tab" aria-selected={filter === k}
                    className={clsx('rounded px-2 py-0.5 text-2xs transition-colors',
                      filter === k ? 'bg-hover text-fg' : 'text-faint hover:text-dim')}
                    onClick={() => setFilter(k)}>
              {label} <span className="tnum text-faint">{counts[k]}</span>
            </button>
          ))}
        </div>
      </div>

      {!shown.length ? (
        <EmptyState offline={false} icon={<WorkflowIcon size={20} />} title="没有匹配的工作流"
                    body={query ? `没有名称或说明包含「${query}」的工作流` : undefined}
                    action={query ? <button className="btn btn-sm" onClick={() => setQuery('')}>清除搜索</button> : undefined} />
      ) : (
        <ul ref={listRef} className="max-h-[52vh] space-y-1 overflow-y-auto pr-0.5" role="listbox" aria-label="工作流">
          {shown.map((w, i) => {
            const here = current?.id === w.id
            return (
              <li
                key={w.id}
                data-index={i}
                role="option"
                aria-selected={here}
                className={clsx(
                  'group relative flex cursor-pointer items-center gap-3 rounded-lg border px-3 py-2 transition-colors',
                  here ? 'border-[var(--accent)] bg-accent-soft' : i === cursor ? 'bg-hover' : 'hover:bg-hover',
                )}
                onMouseEnter={() => setCursor(i)}
                onClick={() => void openOne(w)}
              >
                {here && (
                  <span className="absolute right-1.5 top-1.5 flex h-3.5 w-3.5 items-center justify-center rounded-full bg-accent-solid text-on-accent"
                        title="当前已打开">
                    <Check size={9} strokeWidth={3} />
                  </span>
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="truncate text-xs font-medium">{w.name}</span>
                    {w.is_template && <span className="chip shrink-0"><LayoutTemplate size={9} /> 模板</span>}
                    <StatusChip w={w} />
                    {w.tags?.filter((t) => t !== 'template').map((t) => (
                      <span key={t} className="chip shrink-0 text-faint">{TAG_LABEL[t] ?? t}</span>
                    ))}
                  </div>
                  <div className="mt-0.5 truncate text-2xs text-faint">
                    {w.description || '（无说明）'}
                  </div>
                </div>
                <div className="tnum shrink-0 text-right text-2xs leading-relaxed text-faint">
                  <div title={w.updated_at ? formatDateTime(w.updated_at) : undefined}>
                    {w.updated_at ? `${formatTime(w.updated_at)} 更新` : '—'}
                  </div>
                  <div>{w.graph?.nodes?.length ?? 0} 个节点 · v{w.version}{w.run_count ? ` · 运行 ${w.run_count} 次` : ''}</div>
                </div>
                <div className="flex shrink-0 items-center gap-0.5" onClick={(e) => e.stopPropagation()}>
                  {w.is_template && (
                    <button type="button" className="btn btn-sm"
                            onClick={async () => { if (await createWorkflow(navigate, refresh, w)) onClose() }}>
                      <Plus size={11} /> 以此模板新建
                    </button>
                  )}
                  <IconButton label={`复制「${w.name}」`} title="复制"
                              className="text-faint group-hover:text-dim"
                              onClick={() => void duplicate(w)} icon={<Copy size={11} />} />
                  <IconButton label={`删除「${w.name}」`} title="删除"
                              className="text-faint hover:text-err"
                              onClick={() => void remove(w)} icon={<Trash2 size={11} />} />
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </Modal>
  )
}
