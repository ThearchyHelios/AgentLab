import { useEffect, useMemo, useRef, useState } from 'react'
import type { DragEvent, KeyboardEvent as ReactKeyboardEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  BookOpen, Brain, Check, ClipboardType, FileCode, FileText, Globe, Pencil, Plus, Presentation, Search, Sparkles, Upload,
  X,
} from 'lucide-react'
import clsx from 'clsx'
import { api, ApiError } from '../api/client'
import type { KbHit, KbSearchResult, ReindexJob, UploadProgress } from '../api/client'
import { useCatalog, useOnReconnect } from '../store/catalog'
import {
  confirmDialog, deferDelete, DeleteButton, EmptyState, ErrorState, Field, IconButton, isComposing, Modal,
  PageHeader, promptDialog, SectionBar, Skeleton, Spinner, StatusBadge, TabPanel, Tabs, toast, useRadioGroup,
  useTabRoute, useTicker, withoutDeferred,
} from '../components/ui'
import { humanizeError } from '../lib/errors'
import {
  formatBytes, formatDateTime, formatDuration, formatNumber, formatRelative, formatTime, NONE, parseServerTime, shortId,
} from '../lib/format'
import { matchShortcut } from '../lib/keys'
import { runName } from '../lib/terms'
import { useRunClock } from '../run/useRunClock'
import type { KbDocument, MemoryItem, MemorySource, Skill } from '../types'
import { UploadMeter } from './DataSourcesTab'

// 提到模块级：tab 名同时是 URL 的最后一段，两处各写一份迟早对不上
const TABS = [
  { key: 'kb', label: '知识库' },
  { key: 'memory', label: '长期记忆' },
  { key: 'skills', label: '方法论 Skill' },
]

export function KnowledgePage() {
  const [tab, setTab] = useTabRoute(TABS.map((t) => t.key), 'kb')
  return (
    <div className="flex h-full flex-col">
      <PageHeader
        icon={<BookOpen size={13} />}
        title="知识"
        subtitle="agent 运行时读得到的东西：知识库、长期记忆和方法论 Skill"
      />
      <Tabs tabs={TABS} active={tab} onChange={setTab} label="知识" idPrefix="knowledge" />
      <TabPanel idPrefix="knowledge" tabKey={tab} className="min-h-0 flex-1 overflow-y-auto">
        {tab === 'kb' && <KbTab />}
        {tab === 'memory' && <MemoryTab />}
        {tab === 'skills' && <SkillsTab />}
      </TabPanel>
    </div>
  )
}

// -------------------------------------------------------------------------

type EmbeddingInfo = Awaited<ReturnType<typeof api.kb.embedding>>

function useEmbedding(collection: string) {
  const [info, setInfo] = useState<EmbeddingInfo | null>(null)
  const refresh = async () => {
    const next = await api.kb.embedding(collection).catch(() => null)
    if (next) setInfo(next)
    return next
  }
  useEffect(() => { void refresh() }, [collection])
  useOnReconnect(() => void refresh())
  return { info, refresh }
}

/** 向量模型现在能不能用：和数据源、模型接入同一套剪影，但说的是「语义检索」 */
function SemanticPill({ info }: { info: EmbeddingInfo }) {
  const [status, text, color] = info.fallback
    ? ['failed', '已退回本地哈希 · 没有语义能力', 'var(--err)']
    : info.has_semantics === false
      ? ['skipped', '本地哈希 · 没有语义能力', 'var(--warn)']
      : ['done', '语义检索 · 正常', 'var(--text-dim)']
  return (
    <span role="status" data-semantic={status} className="inline-flex items-center gap-1.5 text-2xs" style={{ color }}>
      <StatusBadge status={status} size={12} decorative />
      {text}
    </span>
  )
}

/**
 * 当前用哪个 embedder，以及有多少条向量已经对不上。
 *
 * 在此之前切换 embedding 模型的唯一开关是 AGENTLAB_USE_OPENAI_EMBEDDINGS——
 * 一个没有界面、没人会发现的环境变量。于是所有人都在用那个没有语义能力的
 * 哈希向量（同义改写一条都召不回），而 alpha 默认还给了它一半权重。
 *
 * 换模型之后存量向量就和查询对不上了，检索会整体退回纯关键词。这一条必须
 * 显眼：少了一半能力却不说，用户只会觉得"最近搜得不准"。
 */
function EmbedderBar({ info, collection, refresh, onChanged }: {
  info: EmbeddingInfo | null
  collection: string
  refresh: () => Promise<EmbeddingInfo | null>
  onChanged: () => void | Promise<void>
}) {
  const collections = useCatalog((s) => s.collections)
  const [busy, setBusy] = useState<'' | 'pick' | 'probe' | 'rebuild'>('')
  const [editing, setEditing] = useState(false)
  const [baseUrl, setBaseUrl] = useState('')
  const [models, setModels] = useState<string[]>([])
  const [model, setModel] = useState('')
  // 在看的那次重建（后台跑，轮询进度）。只有看着它跑完的，才报「重建完了」
  const [job, setJob] = useState<ReindexJob | null>(null)
  const [jobFailed, setJobFailed] = useState<ReindexJob | null>(null)
  const running = job?.state === 'running'
  // 重建途中换模型，前半截是旧模型建的、后半截是新的：跑着的时候把换模型也锁上
  const locked = !!busy || running
  const clock = useRunClock(running)

  useEffect(() => {
    if (info) { setBaseUrl(info.base_url || ''); setModel(info.model || '') }
  }, [info?.base_url, info?.model])

  // 进页先看一眼：刷新之前、或者别的标签页点的重建可能还在跑，接着显示它的进度
  useEffect(() => {
    let live = true
    api.kb.reindexStatus().then((j) => { if (live && j.state === 'running') setJob(j) }, () => {})
    return () => { live = false }
  }, [])

  // 一秒问一次进度，拿到的就是后端按批记下的真实数字，不在两次之间插值
  useEffect(() => {
    if (!running) return
    let live = true
    const t = setTimeout(async () => {
      let next: ReindexJob | { state: 'idle' }
      try {
        next = await api.kb.reindexStatus()
      } catch {
        // 这一拍没问到（后端抖了一下）：原样再排一拍
        if (live) setJob((j) => (j ? { ...j } : j))
        return
      }
      if (!live) return
      if (next.state === 'running') { setJob(next); return }
      setJob(null)
      if (next.state === 'idle') {
        // 进度只记在后端进程里：重启过就没了，重建多半也没做完
        toast.warn('后端重启过，这次重建的进度丢了：看一眼还剩多少对不上的，需要就再点一次重建')
      } else if (next.state === 'failed') {
        setJobFailed(next)
      } else {
        const r = next.result
        const mem = r?.memories_reindexed ?? 0
        toast.ok(`已用 ${r?.embedder ?? '当前模型'} 重建 ${formatNumber(r?.reindexed ?? 0)} 段知识` + (mem ? ` 和 ${formatNumber(mem)} 条记忆` : ''))
      }
      await refresh()
      await onChanged()
    }, 1000)
    return () => { live = false; clearTimeout(t) }
  }, [job])

  if (!info) return <Skeleton rows={1} height={44} className="mb-3" />
  // 记忆和知识库共用一个 embedder，换模型时一起失效。只报一个的话，
  // 用户点完重建还是想不起事，而且不知道为什么
  const staleChunks = info.stale_chunks ?? 0
  const staleMemories = info.stale_memories ?? 0
  // 没建倒排的片段也归这个按钮管：它们走全表扫，结果对但慢，而重建正好
  // 把倒排一起建了。按钮只认"向量对不上"的话，这些片段永远等不到人来点
  const unindexed = info.unindexed_chunks ?? 0
  const stale = staleChunks + staleMemories + unindexed
  // 配了语义模型、实际在用本地哈希：保存的配置没生效（多半是启动时本机的模型服务
  // 还没起），服务后来起了也不会自己连上。以前这里按"配的是不是 local"判断，于是
  // 既不说没有语义能力，还高亮"重建索引"——那些"对不上"的向量正是那个模型建好的，
  // 一点就拿哈希覆盖掉，连上之后还得再重建一遍
  const fallback = !!info.fallback
  const staleParts = [
    staleChunks ? `${formatNumber(staleChunks)} 段知识` : '',
    staleMemories ? `${formatNumber(staleMemories)} 条记忆` : '',
  ].filter(Boolean).join('、')

  const pick = async (kind: string, m = '', url = '', done = '切到') => {
    setBusy('pick')
    try {
      const out = await api.kb.setEmbedding({ kind, model: m, base_url: url })
      // 换模型之后维度多半变了，存量向量全部作废——这件事要当场说，
      // 而不是等用户发现"最近搜得不准"
      const n = (out.stale_chunks ?? 0) + (out.stale_memories ?? 0)
      toast.ok(`已${done} ${out.embedder}（${out.dim} 维）` + (n ? `，${n} 条存量向量需要重建` : ''))
      setEditing(false)
      await refresh()
    } catch (e) {
      // 400 里写的是"连不上哪个地址""哪个模型名不对"，都能照着做
      toast.error(e)
    } finally { setBusy('') }
  }

  /**
   * 切到本地哈希是全局降级：知识库和记忆一起退回纯关键词，所有工作流的检索
   * 节点都受影响。以前它是一行普通按钮，点下去立刻生效，后果要等事后的 toast
   * 才说出来。
   */
  const pickLocal = async () => {
    const chunks = collections.reduce((n, c) => n + (c.chunks ?? 0), 0)
    const memories = (await api.memory.scopes().catch(() => [])).reduce((n, s) => n + (s.count ?? 0), 0)
    const ok = await confirmDialog({
      title: '把向量模型切到本地哈希？',
      danger: true,
      body: '本地哈希零配置、不联网、免费，但它是词频哈希，没有语义能力。',
      consequences: [
        `${formatNumber(chunks)} 段知识、${formatNumber(memories)} 条记忆的向量和它对不上：检索、召回退回纯关键词，直到重建索引`,
        '同义表达搜不出来：「管理员」搜不到「平台角色」',
        '知识库和长期记忆一起切，所有工作流的检索节点都受影响',
      ],
      requireText: '本地哈希',
      confirmLabel: '切到本地哈希',
    })
    if (ok) await pick('local')
  }

  /** 换成另一个语义模型：维度多半会变，存量向量要重建。和当前配置一样的（重新连接）不问 */
  const pickModel = async (m: string, url: string) => {
    const same = info.kind === 'openai' && m === info.model && url === (info.base_url || '')
    if (!same) {
      const ok = await confirmDialog({
        title: `换成 ${m}？`,
        consequences: ['存量向量是旧模型建的，换完要重建一次索引，重建前这部分退回关键词检索'],
        confirmLabel: '换模型',
      })
      if (!ok) return
    }
    await pick('openai', m, url, same ? '重新连上' : '切到')
  }

  const probe = async () => {
    setBusy('probe')
    try {
      const out = await api.kb.probeEmbedding(baseUrl)
      setModels(out.models)
      if (out.models.length === 1) setModel(out.models[0])
      if (!out.models.length) toast.warn('这个地址上没有可用模型')
    } catch (e) {
      toast.error(e)
    } finally { setBusy('') }
  }

  /**
   * 后台重建：立刻回来，进度靠轮询。几千段配远端 embedding 要好几分钟，以前整个
   * 请求压着，按钮上只有一个不知道还要多久的计时
   */
  const rebuild = async () => {
    setJobFailed(null)
    setBusy('rebuild')
    try {
      setJob(await api.kb.startReindex(collection))
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // 已经有一次在跑（别的标签页，或者别人点的）：不再起第二次，接着看它的进度
        toast.info(e.message)
        const cur = await api.kb.reindexStatus().catch(() => null)
        if (cur?.state === 'running') setJob(cur)
      } else {
        toast.error(e)
      }
    } finally { setBusy('') }
  }

  return (
    <section className="mb-3 rounded-lg border bg-panel" aria-label="向量模型" data-embedder>
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 px-3 py-2.5 text-xs">
        <span className="text-faint">向量模型</span>
        <span className="mono">{info.embedder} · <span className="tnum">{info.dim}</span> 维</span>
        {/* 看实际在用的那个，不看配的是什么 */}
        <SemanticPill info={info} />
        <button className="btn btn-sm btn-ghost" disabled={locked} aria-expanded={editing}
                onClick={() => setEditing((v) => !v)}>
          {editing ? '收起' : '换一个'}
        </button>
        <span className="flex-1" />
        {(running || (stale > 0 && !fallback)) && (
          <button className="btn btn-sm btn-primary tnum" disabled={locked || running} onClick={() => void rebuild()}>
            {running || busy === 'rebuild'
              ? <><Spinner size={11} /> 重建中</>
              : <>重建索引（{formatNumber(stale)} 段）</>}
          </button>
        )}
      </div>

      {running && job && <ReindexProgress job={job} now={clock} />}
      {jobFailed && !running && (
        <div className="mx-3 mb-3">
          <ErrorState compact error={{ ok: false, error: jobFailed.error ?? '重建索引没做完', hint: jobFailed.hint ?? undefined }}
                      onRetry={() => void rebuild()} />
        </div>
      )}

      {fallback && (
        <div className="mx-3 mb-3 rounded-lg border px-3 py-2 text-xs leading-relaxed"
             style={{ borderColor: 'color-mix(in srgb, var(--warn) 45%, var(--border))', background: 'color-mix(in srgb, var(--warn) 7%, transparent)' }}>
          <p className="text-[var(--warn)]">
            配置的是 {info.model}（{info.base_url || 'api.openai.com'}），但没连上，
            现在实际用的是本地哈希向量：检索只认字面，搜不出同义表达。
          </p>
          {info.fallback_reason && <p className="mono mt-1 break-all text-2xs text-faint">{info.fallback_reason}</p>}
          {staleParts && (
            <p className="mt-1.5 text-dim">
              这 {staleParts}是用 {info.model} 建的，向量完好，先别重建：现在重建用的是本地哈希，
              只会把它们覆盖掉。重新连上后自动恢复语义检索。
            </p>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button className="btn btn-sm btn-primary" disabled={locked}
                    onClick={() => void pickModel(info.model, info.base_url)}>
              {busy === 'pick' ? <Spinner size={11} /> : null} 重新连接
            </button>
            <span className="text-faint">服务起来之后点这里，不用重启。</span>
          </div>
        </div>
      )}

      {editing && (
        <div className="space-y-2 border-t px-3 py-3">
          <div className="rounded-lg border p-2.5">
            <div className="mb-1.5 text-xs text-dim">
              自定义端点 <span className="text-faint">— 任何讲 OpenAI /v1/embeddings 的服务：本机的 LM Studio、Ollama、vLLM、TEI，或你自己的网关</span>
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              <input className="field h-7 flex-1" placeholder="http://127.0.0.1:1234/v1" aria-label="embedding 服务地址"
                     value={baseUrl} disabled={locked}
                     onChange={(e) => setBaseUrl(e.target.value)} />
              {/* 模型名手填太容易错：LM Studio 里叫 text-embedding-qwen3-embedding-4b，
                  不是 Qwen3-Embedding-4B。填错的表现是切换时 404，人还以为服务没起 */}
              <button className="btn btn-sm" disabled={locked || !baseUrl.trim()} onClick={() => void probe()}>
                {busy === 'probe' ? <Spinner size={11} /> : null} 看看有哪些模型
              </button>
            </div>
            {!!models.length && (
              <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                <select className="field h-7 flex-1" value={model} disabled={locked} aria-label="embedding 模型"
                        onChange={(e) => setModel(e.target.value)}>
                  {models.map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
                <button className="btn btn-sm btn-primary" disabled={locked || !model}
                        onClick={() => void pickModel(model, baseUrl)}>用它</button>
              </div>
            )}
            <p className="mt-1.5 text-2xs text-faint">
              留空地址则走 api.openai.com（要 OPENAI_API_KEY，按量计费）。换模型后维度多半会变，存量向量要重建一次。
            </p>
          </div>

          <button className="btn btn-sm w-full justify-start" disabled={locked || info.kind === 'local'}
                  onClick={() => void pickLocal()}>
            本地哈希向量 <span className="font-normal text-faint">— 零配置、不联网、免费，但只认字面不认语义（全局降级，会先确认）</span>
          </button>
        </div>
      )}

      {!fallback && staleParts && (
        <p className="mx-3 mb-3 text-xs leading-relaxed text-[var(--warn)]">
          有 {staleParts}的向量是用别的模型建的，和当前的 {info.embedder} 对不上——检索、召回这些内容时
          会退回纯关键词（搜不出同义表达）。重建索引后恢复。
        </p>
      )}
      {unindexed > 0 && !staleParts && (
        <p className="mx-3 mb-3 text-xs text-faint">
          有 {formatNumber(unindexed)} 段还没建倒排索引，检索它们会退回全表扫——结果是对的，只是慢。重建一次就好。
        </p>
      )}
      {info.kind !== 'openai' && stale === 0 && (
        <p className="mx-3 mb-3 text-xs text-faint">
          本地向量不联网、零配置，但它是词频哈希、没有语义泛化——
          「管理员」搜不到「平台角色」。认真用知识库的话换成上面的真模型。
        </p>
      )}
    </section>
  )
}

/**
 * 重建索引的进度：后端按批（64 条一批）记下的真实数字，不插值。
 *
 * 三段：知识片段的向量 → 倒排索引 → 记忆的向量。倒排那一段后端没有分批进度，
 * 条停在片段做完的位置，写明「这一步没有分批进度」，不去假装它在走
 */
function ReindexProgress({ job, now }: { job: ReindexJob; now: number }) {
  const started = parseServerTime(job.started_at)?.getTime()
  const pct = job.total > 0 ? Math.min(1, job.done / job.total) : null
  const step = job.phase === 'index'
    ? '正在重建倒排索引'
    : job.phase === 'memories'
      ? `正在重算记忆的向量 · ${formatNumber(job.memories.done)} / ${formatNumber(job.memories.total)} 条`
      : `正在重算知识片段的向量 · ${formatNumber(job.chunks.done)} / ${formatNumber(job.chunks.total)} 段`
  return (
    <div className="border-t px-3 py-2.5" data-reindex={job.phase}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
        <span className="tnum">{step}</span>
        <span className="flex-1" />
        <span className="tnum text-2xs text-faint">
          {pct != null && <>合计 {formatNumber(job.done)} / {formatNumber(job.total)} · {Math.round(pct * 100)}% · </>}
          已用 {started ? formatDuration(Math.max(0, now - started)) : NONE}
        </span>
      </div>
      {pct != null && (
        <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-hover" role="progressbar" aria-label="重建索引进度"
             aria-valuemin={0} aria-valuemax={job.total} aria-valuenow={job.done}
             aria-valuetext={`${step}，合计 ${job.done} / ${job.total}`}>
          {/* 用 scaleX 推进而不是改 width：只动 transform，不触发重排 */}
          <div className="h-full origin-left rounded-full bg-[var(--st-running)] transition-transform duration-500"
               style={{ transform: `scaleX(${pct})` }} />
        </div>
      )}
      <p className="mt-1.5 text-2xs text-faint">
        {job.phase === 'index'
          ? '片段的向量都算完了，在把倒排索引整个重建一遍。这一步没有分批进度，库大的话要等一会儿。'
          : `${job.collection ? `只重建「${job.collection}」的片段；` : ''}记忆和知识库共用一个向量模型，一起重建。可以离开这一页，回来还能看到进度。`}
      </p>
    </div>
  )
}

// -------------------------------------------------------------------------

function KbTab() {
  // 地址上带了文档 id 就看那一份的详情。集合是筛选条件不是位置，所以不进 URL
  const { id } = useParams()
  if (id) return <DocDetail docId={id} />
  return <KbList />
}

/** 按扩展名挑图标：一眼分得出 PPT、PDF 和代码 */
function DocIcon({ doc }: { doc: KbDocument }) {
  const ext = extOf(doc.source || doc.title)
  const Icon = !doc.source ? ClipboardType
    : ext === 'pptx' ? Presentation
    : ext === 'html' || ext === 'htm' ? Globe
    : ['py', 'ts', 'js', 'json', 'yaml', 'yml'].includes(ext) ? FileCode
    : FileText
  return <Icon size={15} className="shrink-0 text-faint" aria-hidden />
}

const extOf = (name: string) => (/\.([a-z0-9]+)$/i.exec(name ?? '')?.[1] ?? '').toLowerCase()

/**
 * 一份文档被切成了什么样。
 *
 * 切块这一层做了不少事——按行边界留重叠、跨块把表头续上（后端 memory/kb.py
 * 的 chunk_text），而在此之前结果在界面上一处都看不见。检索不准的时候第一个
 * 该看的就是它：表头有没有续上、重叠对不对、哪一段被截断了。
 */
function DocDetail({ docId }: { docId: string }) {
  const navigate = useNavigate()
  const [data, setData] = useState<Awaited<ReturnType<typeof api.kb.document>> | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [flash, setFlash] = useState<number | null>(null)

  useEffect(() => {
    let live = true
    void api.kb.document(docId)
      .then((d) => { if (live) setData(d) })
      .catch((e) => { if (live) setError(e) })
    return () => { live = false }
  }, [docId])

  // 从检索结果跳过来时带着 #chunk-N：落到那一段并闪一下
  useEffect(() => {
    if (!data) return
    const m = /^#chunk-(\d+)$/.exec(location.hash)
    if (m) jump(Number(m[1]))
  }, [data])

  const jump = (ordinal: number) => {
    document.getElementById(`chunk-${ordinal}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    setFlash(ordinal)
    setTimeout(() => setFlash((f) => (f === ordinal ? null : f)), 1400)
  }

  const back = (
    <button className="btn btn-sm mb-3" onClick={() => navigate('/knowledge/kb')}>← 回到知识库</button>
  )
  if (error) {
    const gone = humanizeError(error).status === 404
    return (
      <div className="mx-auto max-w-4xl p-4">
        {back}
        {gone
          ? <EmptyState icon={<BookOpen size={22} />} title="这份文档不在了" body="可能已经被删了。回到知识库看看现在有哪些。" />
          : <ErrorState error={error} />}
      </div>
    )
  }
  if (!data) return <div className="mx-auto max-w-4xl p-4">{back}<Skeleton rows={6} height={14} /></div>

  const doc = data.document
  // 一份文档里混着两个模型建的向量是真会发生的（换模型之后只重建了一部分），
  // 而那会让检索对这部分内容悄悄退回关键词
  const models = [...new Set(data.chunks.map((c) => c.embed_model).filter(Boolean))]
  const noVector = data.chunks.filter((c) => !c.has_vector).length

  return (
    <div className="mx-auto max-w-4xl p-4">
      {back}
      <h2 className="mb-1 flex items-center gap-2 text-sm font-semibold"><DocIcon doc={doc} />{doc.title}</h2>
      <div className="mb-3 flex flex-wrap items-center gap-2 text-2xs text-faint">
        <span className="chip">{doc.collection}</span>
        {doc.source && doc.source !== doc.title && <span>{doc.source}</span>}
        <span className="tnum">{formatNumber(data.chunks.length)} 个片段</span>
        {doc.created_at && <span title={formatDateTime(doc.created_at)}>{formatTime(doc.created_at)}</span>}
        {models.map((m) => <span key={m} className="mono chip">{m}</span>)}
        {models.length > 1 && (
          <span className="text-[var(--warn)]">这份文档里混着两个模型建的向量，对不上当前模型的那些会退回关键词检索</span>
        )}
        {noVector > 0 && <span className="text-[var(--warn)]">{noVector} 段还没有向量</span>}
      </div>

      {data.chunks.length > 0 && <ChunkMap chunks={data.chunks} models={models} onPick={jump} />}

      {!data.chunks.length && (
        <EmptyState icon={<BookOpen size={20} />} title="还没有切块"
                    body={doc.status === 'processing' ? '正在处理，稍后刷新' : doc.error || '这份文档没有产生任何片段'} />
      )}

      <div className="space-y-2">
        {data.chunks.map((c) => (
          <div key={c.id} id={`chunk-${c.ordinal}`}
               className={clsx('scroll-mt-4 rounded-lg border bg-panel p-2.5', flash === c.ordinal && 'shadow-[var(--glow-accent)]')}>
            <div className="mb-1 flex items-center gap-2 text-2xs text-faint">
              <span className="font-medium text-dim">片段 {c.ordinal}</span>
              <span className="tnum">{formatNumber(c.chars)} 字</span>
              {c.token_len > 0 && <span className="tnum">{formatNumber(c.token_len)} tokens</span>}
              {!c.has_vector && <span className="text-[var(--warn)]">没有向量</span>}
              {c.truncated && <span className="ml-auto">（这里只显示开头一部分）</span>}
            </div>
            <div className="whitespace-pre-wrap text-xs leading-relaxed">{c.content}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

/**
 * 切块地图：一格一个片段，高度是字数，没向量的标 warn，混了第二个模型的打斜纹。
 * 整份文档切得匀不匀、哪几段没向量，一眼看完，不用往下翻几百张卡片。
 */
function ChunkMap({ chunks, models, onPick }: {
  chunks: { ordinal: number; chars: number; has_vector: boolean; embed_model: string }[]
  models: string[]
  onPick: (ordinal: number) => void
}) {
  const max = Math.max(1, ...chunks.map((c) => c.chars))
  const main = models[0]
  const shown = chunks.slice(0, 3000)
  return (
    <figure className="mb-4 rounded-lg border bg-panel p-2.5" data-chunk-map>
      {/* 每一格都能点（跳到那一段），是按钮，不是列表项：role=listitem 会把按钮语义盖掉 */}
      <div className="flex flex-wrap items-end gap-[2px]" role="group" aria-label="切块地图">
        {shown.map((c) => {
          const h = 6 + Math.round((c.chars / max) * 18)
          const other = models.length > 1 && c.embed_model && c.embed_model !== main
          return (
            <button
              key={c.ordinal}
              type="button"
              title={`片段 ${c.ordinal} · ${c.chars} 字 · ${c.has_vector ? `有向量（${c.embed_model || '未知模型'}）` : '没有向量'}`}
              aria-label={`片段 ${c.ordinal}`}
              onClick={() => onPick(c.ordinal)}
              className="w-[6px] rounded-[1px] opacity-80 transition-opacity hover:opacity-100 focus-visible:outline focus-visible:outline-1 focus-visible:outline-[var(--accent)]"
              style={{
                height: h,
                background: !c.has_vector ? 'var(--warn)'
                  : other ? 'repeating-linear-gradient(45deg, var(--text-faint) 0 2px, transparent 2px 4px)'
                  : 'var(--text-faint)',
              }}
            />
          )
        })}
      </div>
      <figcaption className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-2xs text-faint">
        <span>一格一个片段，高度是字数（最长 {formatNumber(max)} 字）</span>
        <span className="inline-flex items-center gap-1"><i className="inline-block h-2 w-1.5 rounded-[1px] bg-[var(--warn)]" />没有向量</span>
        {models.length > 1 && <span>斜纹：不是 {main} 建的</span>}
        {chunks.length > shown.length && <span>只画了前 {formatNumber(shown.length)} 段</span>}
        <span>点一格跳到那一段</span>
      </figcaption>
    </figure>
  )
}

/** 后端 /kb/formats 拿不到时的兜底，和后端解析器保持一致 */
const FALLBACK_FORMATS = {
  extensions: ['.docx', '.pdf', '.pptx', '.htm', '.html', '.txt', '.md', '.markdown', '.json', '.log', '.yaml', '.yml', '.py', '.ts', '.js'],
  tabular: ['.csv', '.tsv', '.xls', '.xlsx'],
  legacy: ['.doc', '.ppt', '.xls'],
}

interface Uploading {
  key: number; name: string; size: number
  /** 轮到它、开始发的时刻。一次传一个，排在后面的还没开始 */
  startedAt?: number
  progress?: UploadProgress
  /** 字节发完的时刻：之后是后端在解析、切块 */
  sentAt?: number
}

function KbList() {
  const navigate = useNavigate()
  const refreshCatalog = useCatalog((s) => s.refresh)
  const [collection, setCollection] = useState('default')
  const [collections, setCollections] = useState<{ collection: string; documents: number; chunks: number }[]>([])
  const [docs, setDocs] = useState<KbDocument[] | null>(null)
  const [docsError, setDocsError] = useState<unknown>(null)
  const [formats, setFormats] = useState<Awaited<ReturnType<typeof api.kb.formats>> | null>(null)
  const [adding, setAdding] = useState(false)
  const [uploads, setUploads] = useState<Uploading[]>([])
  const [dragging, setDragging] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const uploadSeq = useRef(0)
  const aborts = useRef(new Map<number, AbortController>())
  const { info, refresh: refreshInfo } = useEmbedding(collection)

  const load = async () => {
    const [cs, ds] = await Promise.allSettled([api.kb.collections(), api.kb.documents(collection)])
    if (cs.status === 'fulfilled') setCollections(cs.value)
    // 还在撤销窗口里的文档 DELETE 没发，后端照样返回它：滤掉，不然轮询一刷就回来了
    if (ds.status === 'fulfilled') { setDocs(withoutDeferred(ds.value, '/api/kb/documents')); setDocsError(null) }
    else setDocsError(ds.reason)
  }
  useEffect(() => { setDocs(null); void load() }, [collection])
  useEffect(() => { api.kb.formats().then(setFormats, () => {}) }, [])
  useOnReconnect(() => { void load(); if (!formats) api.kb.formats().then(setFormats, () => {}) })

  // 有文档还在切块就接着轮询。一个 10MB 文档配上远端 embedding 要几十分钟，
  // 不轮询的话界面会一直停在"处理中"，人以为卡死了
  useEffect(() => {
    if (!docs?.some((d) => d.status === 'processing')) return
    const timer = setInterval(() => { void load() }, 2000)
    return () => clearInterval(timer)
  }, [docs, collection])

  const fmt = formats ?? FALLBACK_FORMATS
  const accept = formats?.accept ?? FALLBACK_FORMATS.extensions.join(',')

  const upload = async (files: File[]) => {
    if (!files.length) return
    const toData = { label: '去数据源传表格', onClick: () => navigate('/data/tables') }
    const ok: File[] = []
    for (const file of files) {
      const ext = `.${extOf(file.name)}`
      // 表格进知识库只会被切成文本：聚合算不了、数字也回指不了，那条路要走数据源。
      // 选文件框里已经不放行，拖进来的还得在这里拦
      if (fmt.tabular.includes(ext)) {
        toast.error(`${file.name} 是表格，不进知识库：传到数据源才能用 SQL 算数、查得到出处`, { action: toData })
      } else if (fmt.legacy.includes(ext)) {
        toast.error(`${file.name} 是老的二进制格式（${ext}），先另存为 ${ext}x 再传`)
      } else if (!fmt.extensions.includes(ext)) {
        toast.error(`${file.name}：知识库不认 ${ext} 文件。能传的有 ${fmt.extensions.join(' ')}`)
      } else {
        ok.push(file)
      }
    }
    if (!ok.length) return
    const items = ok.map((f) => ({ key: ++uploadSeq.current, name: f.name, size: f.size, file: f }))
    for (const item of items) aborts.current.set(item.key, new AbortController())
    setUploads((u) => [...items.map(({ file: _f, ...rest }) => rest), ...u])
    const patch = (key: number, next: (x: Uploading) => Partial<Uploading>) =>
      setUploads((u) => u.map((x) => (x.key === key ? { ...x, ...next(x) } : x)))
    // 一次传一个：并发传几个大文件，每个都慢，谁都等不到「处理中」
    for (const item of items) {
      const ctl = aborts.current.get(item.key)
      try {
        if (!ctl || ctl.signal.aborted) continue
        patch(item.key, () => ({ startedAt: Date.now() }))
        await api.kb.upload(item.file, collection, {
          signal: ctl.signal,
          onProgress: (p) => patch(item.key, (x) => ({ progress: p, sentAt: x.sentAt ?? (p.sent ? Date.now() : undefined) })),
        })
        await load()
      } catch (e) {
        // 人点的取消：不算出错
        if (e instanceof DOMException && e.name === 'AbortError') continue
        const h = humanizeError(e)
        const text = `${item.name} 没传上：${h.reason ? `${h.title}，${h.reason}` : h.title}`
        toast.error(text, { detail: h.raw, action: /表格|数据源/.test(text) ? toData : undefined })
      } finally {
        aborts.current.delete(item.key)
        setUploads((u) => u.filter((x) => x.key !== item.key))
      }
    }
    await load()
    await refreshCatalog()
  }
  // 排着队的直接拿掉；在传的由上面那个循环收尾（它会拿到 AbortError）
  const cancelUpload = (key: number) => {
    aborts.current.get(key)?.abort()
    setUploads((u) => u.filter((x) => x.key !== key || !!x.startedAt))
  }

  const newCollection = async () => {
    const name = await promptDialog({
      title: '新建知识库', label: '名称', placeholder: '比如 quality_sop',
      validate: (v) => (collections.some((c) => c.collection === v) ? '已经有这个知识库了'
        : /^[\w.-]{1,64}$/.test(v) ? null : '只用字母、数字、下划线、点和短横'),
      confirmLabel: '建好并切过去',
    })
    if (name) setCollection(name)
  }

  const removeDoc = (doc: KbDocument) => deferDelete({
    what: `「${doc.title}」`,
    url: `/api/kb/documents/${doc.id}`,
    hide: () => setDocs((ds) => ds && ds.filter((d) => d.id !== doc.id)),
    restore: () => void load(),
    commit: () => api.kb.remove(doc.id),
    done: () => { void load(); void refreshCatalog() },
  })

  const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types ?? []).includes('Files')
  const known = collections.some((c) => c.collection === collection)
  const current = collections.find((c) => c.collection === collection)
  const choices = [...collections, ...(known ? [] : [{ collection, documents: 0, chunks: 0 }])]
  const radio = useRadioGroup(choices.map((c) => c.collection), collection, setCollection)

  return (
    <div className="mx-auto max-w-4xl p-4">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <div role="radiogroup" aria-label="知识库" className="flex flex-wrap items-center gap-1.5">
            {choices.map((c) => {
              const on = c.collection === collection
              return (
                <button key={c.collection} type="button" {...radio(c.collection)}
                        className={clsx('chip', on ? 'border-[var(--accent)] bg-accent-soft text-fg' : 'hover:border-[var(--border-strong)] hover:text-fg')}
                        onClick={() => setCollection(c.collection)}>
                  {on && <Check size={10} className="text-[var(--accent)]" aria-hidden />}
                  <span className="font-medium">{c.collection}</span>
                  <span className="tnum text-faint">{formatNumber(c.documents)} 篇 · {formatNumber(c.chunks)} 段</span>
                </button>
              )
            })}
          </div>
          <button type="button" className="chip border-dashed text-faint hover:text-fg" onClick={() => void newCollection()}>
            <Plus size={10} aria-hidden /> 新知识库
          </button>
        </div>
        <div className="flex-1" />
        <button className="btn btn-sm" onClick={() => fileRef.current?.click()}
                title={`能传：${fmt.extensions.join(' ')}。表格去数据源`}>
          {uploads.length ? <Spinner size={11} /> : <Upload size={12} aria-hidden />}
          {uploads.length ? `上传中 ${uploads.length} 个` : '上传文件'}
        </button>
        {/* 能收什么以后端解析器为准（/kb/formats）：以前这里手写一份，漏了 .pptx
            却放了 .csv——PPT 选不中，CSV 选得中但必被拒 */}
        <input ref={fileRef} type="file" multiple hidden accept={accept} data-kb-upload
               onChange={(e) => { const fs = Array.from(e.target.files ?? []); e.target.value = ''; void upload(fs) }} />
        <button className="btn btn-sm btn-primary" onClick={() => setAdding(true)}>
          <Plus size={12} aria-hidden /> 粘贴文本
        </button>
      </div>

      <EmbedderBar info={info} collection={collection} refresh={refreshInfo} onChanged={load} />

      <KbSearch collection={collection} info={info} />

      <section
        aria-label="文档"
        className={clsx('relative rounded-lg border bg-panel', dragging && 'border-dashed border-[var(--accent)]')}
        onDragOver={(e) => { if (hasFiles(e)) { e.preventDefault(); setDragging(true) } }}
        onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragging(false) }}
        onDrop={(e) => { if (!hasFiles(e)) return; e.preventDefault(); setDragging(false); void upload(Array.from(e.dataTransfer.files)) }}
      >
        <div className="flex items-center gap-2 border-b px-3 py-2 text-xs">
          <span className="font-medium">文档</span>
          {current && <span className="tnum text-faint">{formatNumber(current.documents)} 篇 · {formatNumber(current.chunks)} 段</span>}
          <span className="ml-auto text-2xs text-faint">文件可以直接拖进来</span>
        </div>
        {dragging && (
          <div className="pointer-events-none absolute inset-0 z-[2] flex items-center justify-center rounded-lg bg-accent-soft/80 text-xs text-fg">
            <Upload size={14} className="mr-1.5" aria-hidden /> 松手上传到「{collection}」
          </div>
        )}

        {uploads.map((u) => <UploadingRow key={u.key} item={u} onCancel={() => cancelUpload(u.key)} />)}

        {docs === null ? (
          docsError ? <ErrorState compact error={docsError} onRetry={() => void load()} className="m-3" />
            : <div className="p-3"><Skeleton rows={4} height={30} gap={8} /></div>
        ) : !docs.length && !uploads.length ? (
          <EmptyState
            icon={<BookOpen size={22} />}
            title={known ? '这个知识库还是空的' : `新知识库「${collection}」`}
            body="上传 PDF、Word、PPT、网页或文本，会自动切块并建立索引。Excel / CSV 请走数据源。"
            action={
              <div className="flex gap-2">
                <button className="btn btn-primary btn-sm" onClick={() => fileRef.current?.click()}><Upload size={12} aria-hidden /> 上传文件</button>
                <button className="btn btn-sm" onClick={() => setAdding(true)}><Plus size={12} aria-hidden /> 粘贴文本</button>
              </div>
            }
          />
        ) : (
          <ul className="divide-y">
            {docs.map((doc) => <DocRow key={doc.id} doc={doc} onRemove={() => removeDoc(doc)} />)}
          </ul>
        )}
      </section>

      <AddDocModal open={adding} collection={collection} onClose={() => setAdding(false)}
                   onSaved={async () => { setAdding(false); await load(); await refreshCatalog() }} />
    </div>
  )
}

/**
 * 上传占位行：真实的字节进度，发完之后写「处理中」。字节没发完之前可以取消——
 * 后端收不全就什么都不会建；发完了后端已经在切块，取消只会让人以为没传上
 */
function UploadingRow({ item, onCancel }: { item: Uploading; onCancel: () => void }) {
  const now = useRunClock(true)
  const sent = !!item.progress?.sent
  return (
    <div className="flex items-center gap-3 border-b px-3 py-2" data-uploading>
      {item.startedAt ? <Spinner size={14} /> : <Upload size={14} className="shrink-0 text-faint" aria-hidden />}
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="truncate text-sm">{item.name}</span>
          <span className="tnum shrink-0 text-2xs text-faint">{formatBytes(item.size)}</span>
        </div>
        <div className="mt-0.5 text-2xs">
          <UploadMeter progress={item.progress ?? null} startedAt={item.startedAt} sentAt={item.sentAt}
                       processing="后端在解析、切块" now={now} />
        </div>
      </div>
      {!sent && (
        <button className="btn btn-sm btn-ghost shrink-0" onClick={onCancel} aria-label={`取消上传 ${item.name}`}>
          <X size={12} aria-hidden /> 取消
        </button>
      )}
    </div>
  )
}

/** 处理进度的速率：同一份文档前后两次轮询之间走了多少段，推出还要多久。只用真实进度，不插值 */
const progressSeen = new Map<string, { at: number; done: number }>()

function DocRow({ doc, onRemove }: { doc: KbDocument; onRemove: () => void }) {
  const navigate = useNavigate()
  const processing = doc.status === 'processing'
  const progress = doc.meta?.progress
  const now = Date.now()
  let eta: number | null = null
  if (processing && progress?.total) {
    const first = progressSeen.get(doc.id)
    if (!first) progressSeen.set(doc.id, { at: now, done: progress.done })
    else if (progress.done > first.done && now - first.at > 3000) {
      const rate = (progress.done - first.done) / (now - first.at)
      eta = (progress.total - progress.done) / rate
    }
  } else {
    progressSeen.delete(doc.id)
  }
  const ext = extOf(doc.source || '')
  const meta = [
    doc.source && doc.source !== doc.title ? doc.source : null,
    doc.source ? (ext ? ext.toUpperCase() : null) : '粘贴的文本',
    `${formatNumber(doc.chunk_count)} 个片段`,
    doc.created_at ? formatTime(doc.created_at) : null,
  ].filter(Boolean).join(' · ')
  const pct = progress?.total ? Math.min(100, (progress.done / progress.total) * 100) : null

  return (
    <li className="group flex items-center gap-3 px-3 py-2 hover:bg-hover" data-doc={doc.title}>
      <DocIcon doc={doc} />
      <button className="min-w-0 flex-1 text-left" onClick={() => navigate(`/knowledge/kb/${doc.id}`)}
              title="看看它被切成了什么样">
        <div className="truncate text-sm">{doc.title}</div>
        {processing ? (
          <div className="mt-1">
            <div className="flex items-center gap-2 text-2xs text-[var(--accent)]">
              <Spinner size={10} />
              <span className="tnum">
                正在切块并算向量{progress?.total ? ` · ${formatNumber(progress.done)} / ${formatNumber(progress.total)} 段` : '…'}
                {eta != null && <span className="text-faint"> · 预计还要 {formatDuration(eta)}</span>}
              </span>
            </div>
            {pct != null && (
              <div className="mt-1 h-0.5 overflow-hidden rounded-full bg-hover" role="progressbar"
                   aria-valuemin={0} aria-valuemax={progress!.total} aria-valuenow={progress!.done} aria-label="处理进度">
                {/* 用 scaleX 推进而不是改 width：只动 transform，不触发重排 */}
                <div className="h-full origin-left rounded-full bg-[var(--accent)] transition-transform duration-500"
                     style={{ transform: `scaleX(${pct / 100})` }} />
              </div>
            )}
          </div>
        ) : doc.status === 'failed' ? (
          // 失败原因要写出来："这是扫描件"和"本地服务没起"是两件事
          <div className="text-2xs text-[var(--err)]">处理失败：{doc.error}</div>
        ) : (
          <div className="truncate text-2xs text-faint" title={doc.created_at ? formatDateTime(doc.created_at) : undefined}>{meta}</div>
        )}
      </button>
      <DeleteButton label={`删除文档 ${doc.title}`} onClick={onRemove} />
    </li>
  )
}

/**
 * 检索调试台：预演 agent 的 kb_search 会拿到什么。
 *
 * α 的初值取运行时实际用的 default_alpha（以前写死 0，也就是纯关键词，而运行时
 * 是 0.5——调试台和运行时对不上，人会照着错的命中去调知识库）。偏离时标出来，
 * 一键回到运行时的值。
 */
function KbSearch({ collection, info }: { collection: string; info: EmbeddingInfo | null }) {
  const runtime = info?.default_alpha ?? null
  const [query, setQuery] = useState('')
  const [alpha, setAlpha] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [res, setRes] = useState<KbSearchResult | null>(null)

  // 集合切换、或运行时默认值变了（换了 embedder），就回到运行时的值
  useEffect(() => { if (runtime != null) setAlpha(runtime) }, [collection, runtime])
  const a = alpha ?? runtime ?? 0.5

  const search = async () => {
    if (!query.trim()) { setRes(null); return }
    setBusy(true)
    try {
      setRes(await api.kb.search(query, collection, a))
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  const off = runtime != null && Math.abs(a - runtime) > 1e-9
  return (
    <section className="mb-3 rounded-lg border bg-panel p-3" aria-label="检索调试台" data-kb-search>
      <div className="mb-2 flex items-baseline gap-2">
        <h2 className="text-xs font-medium">检索调试台</h2>
        <span className="text-2xs text-faint">预演 agent 的 kb_search 会拿到什么</span>
      </div>
      <div className="flex items-center gap-2">
        <input className="field" placeholder="测试检索效果…" value={query} aria-label="检索内容"
               onChange={(e) => setQuery(e.target.value)}
               onKeyDown={(e) => { if (e.key === 'Enter' && !isComposing(e)) void search() }} />
        <button className="btn" onClick={() => void search()} disabled={busy}>
          {busy ? <Spinner size={12} /> : <Search size={12} aria-hidden />} 检索
        </button>
      </div>

      <div className="mt-3 flex items-start gap-2.5 text-2xs text-faint">
        <span className="w-10 shrink-0 leading-[14px]">关键词</span>
        <div className="relative flex-1 pb-4">
          {/* 自己画轨道：原生轨道在深色下是一条发白的灰条，也看不出填到了哪 */}
          <input
            type="range" min={0} max={1} step={0.05} value={a} data-alpha
            aria-label="混合权重 α（0 纯关键词，1 纯语义）"
            aria-valuetext={`α ${a.toFixed(2)}：语义占 ${Math.round(a * 100)}%`}
            onChange={(e) => setAlpha(Number(e.target.value))}
            className="relative z-[1] block h-1.5 w-full cursor-pointer appearance-none rounded-full outline-none
                       [&::-webkit-slider-thumb]:h-3.5 [&::-webkit-slider-thumb]:w-3.5 [&::-webkit-slider-thumb]:appearance-none
                       [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:border-2 [&::-webkit-slider-thumb]:border-[var(--accent)]
                       [&::-webkit-slider-thumb]:bg-[var(--bg-panel)] [&::-webkit-slider-thumb]:shadow-elev-1
                       focus-visible:[&::-webkit-slider-thumb]:shadow-[var(--glow-accent)]
                       [&::-moz-range-thumb]:h-3 [&::-moz-range-thumb]:w-3 [&::-moz-range-thumb]:rounded-full
                       [&::-moz-range-thumb]:border-2 [&::-moz-range-thumb]:border-[var(--accent)] [&::-moz-range-thumb]:bg-[var(--bg-panel)]"
            style={{ background: `linear-gradient(to right, var(--accent) 0 ${a * 100}%, var(--bg-hover) ${a * 100}% 100%)` }}
          />
          {runtime != null && (
            // 刻度对准圆心：14px 的圆心在轨道两头各缩进 7px 的范围里走
            <>
              <span aria-hidden className="pointer-events-none absolute top-[-3px] z-[2] h-3 w-px -translate-x-1/2"
                    style={{ left: `calc(${runtime * 100}% + ${7 - runtime * 14}px)`, background: off ? 'var(--text-faint)' : 'transparent' }} />
              <span aria-hidden className="pointer-events-none absolute top-[12px] -translate-x-1/2 whitespace-nowrap"
                    style={{ left: `calc(${runtime * 100}% + ${7 - runtime * 14}px)`, color: off ? 'var(--text-faint)' : 'var(--accent)' }}>
                运行时 {runtime}
              </span>
            </>
          )}
        </div>
        <span className="w-8 shrink-0 leading-[14px]">语义</span>
        <span className="mono tnum w-10 shrink-0 text-right leading-[14px] text-dim">{a.toFixed(2)}</span>
      </div>
      {off && (
        <div className="text-2xs text-[var(--warn)]" data-alpha-off>
          与运行时不同（运行时 α = {runtime}），命中会和 agent 拿到的不一样 ·{' '}
          <button className="underline underline-offset-2 hover:text-fg" onClick={() => setAlpha(runtime)}>重置</button>
        </div>
      )}

      {res && (
        <div className="mt-3 space-y-2">
          <div className="flex flex-wrap items-center gap-2 text-2xs text-faint">
            <span className="tnum">{res.results.length} 条命中 · 按 α = {(res.alpha ?? a).toFixed(2)} 检索</span>
            {!!res.degraded?.length && (
              <span className="text-[var(--warn)]" title={res.degraded.join('\n')}>已退回关键词：{res.degraded[0]}</span>
            )}
            {res.results.some((h) => h.contrib) && (
              <span className="ml-auto inline-flex items-center gap-1.5" data-contrib-legend>
                总分 =
                <Swatch color={VIA_SEMANTIC} /> 语义贡献 +
                <Swatch color={VIA_KEYWORD} /> 关键词贡献
              </span>
            )}
          </div>
          {!res.results.length && <div className="text-xs text-faint">没有命中</div>}
          {res.results.map((hit) => <HitCard key={hit.chunk_id} hit={hit} />)}
        </div>
      )}
    </section>
  )
}

/**
 * 贡献条的两种颜色：分类色，只说「哪一路检索」，不表达状态（ok / warn / err /
 * accent 留给状态）。取值在 index.css 的主题令牌里，亮暗各一套
 */
const VIA_SEMANTIC = 'var(--via-semantic)'
const VIA_KEYWORD = 'var(--via-keyword)'

function Swatch({ color }: { color: string }) {
  return <i aria-hidden className="inline-block h-2 w-2 shrink-0 rounded-[2px]" style={{ background: color }} />
}

/**
 * 一条命中。总分是候选集上归一化后的混合分（0–1），拆成两段：语义那一路贡献了
 * 多少、关键词那一路贡献了多少（两段之和就是总分，后端按 α 加权后给的）。人一眼
 * 看得出这条是靠哪一路捞上来的，调 α 时知道在动哪一段。
 *
 * 原始分另外写成数：语义是余弦（0–1），关键词是 BM25（没有上界，画条会误导）。
 * 老后端没有 contrib，就退回一根总分条
 */
function HitCard({ hit }: { hit: KbHit }) {
  const vec = typeof hit.signals?.vector === 'number' ? hit.signals.vector : null
  const kw = typeof hit.signals?.keyword === 'number' ? hit.signals.keyword : null
  const c = hit.contrib
  const only = vec && !kw ? '只靠语义命中' : kw && !vec ? '只靠关键词命中' : null
  const raw = (v: number | null, digits: number) => (v == null ? '—' : v.toFixed(digits))
  return (
    <Link to={`/knowledge/kb/${hit.document_id}#chunk-${hit.ordinal}`}
          className="block rounded-lg border bg-bg p-2.5 hover:border-[var(--border-strong)]" data-hit>
      <div className="flex items-center gap-2 text-2xs text-faint">
        <span className="truncate font-medium text-dim">{hit.title}</span>
        <span className="shrink-0">片段 {hit.ordinal}</span>
        {only && <span className="chip shrink-0">{only}</span>}
        <span className="ml-auto shrink-0">总分</span>
        {c ? <ContribBar vector={c.vector} keyword={c.keyword} /> : <Bar value={hit.score} />}
        <span className="mono tnum w-11 shrink-0 text-right text-dim">{Number(hit.score).toFixed(3)}</span>
      </div>
      {c ? (
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-2xs text-faint" data-contrib>
          <span className="inline-flex items-center gap-1">
            <Swatch color={VIA_SEMANTIC} />语义 <span className="mono tnum text-dim">+{Math.max(0, c.vector).toFixed(3)}</span>
            <span>（余弦 <span className="mono tnum">{raw(vec, 3)}</span>）</span>
          </span>
          <span className="inline-flex items-center gap-1">
            <Swatch color={VIA_KEYWORD} />关键词 <span className="mono tnum text-dim">+{Math.max(0, c.keyword).toFixed(3)}</span>
            <span>（BM25 <span className="mono tnum">{raw(kw, 2)}</span>）</span>
          </span>
        </div>
      ) : (
        <div className="mt-1 flex items-center gap-3 text-2xs text-faint">
          <span className="inline-flex items-center gap-1.5">语义余弦 <Bar value={vec ?? 0} thin /> <span className="mono tnum">{raw(vec, 3)}</span></span>
          <span>关键词 BM25 <span className="mono tnum">{raw(kw, 2)}</span></span>
        </div>
      )}
      <div className="mt-1.5 line-clamp-3 text-xs leading-relaxed">{hit.content}</div>
    </Link>
  )
}

/**
 * 总分拆成两段的条：整根轨道是 1 分，两段依次排开，中间留 2px 缝。关键词那段是
 * 后端用减法算出来的，四舍五入可能出一点点负数，画的时候当 0
 */
function ContribBar({ vector, keyword }: { vector: number; keyword: number }) {
  const v = Math.max(0, Math.min(1, vector))
  const k = Math.max(0, Math.min(1 - v, keyword))
  return (
    <span className="inline-flex h-1.5 w-16 shrink-0 gap-[2px] overflow-hidden rounded-full bg-hover" aria-hidden
          title={`语义贡献 ${v.toFixed(3)} + 关键词贡献 ${k.toFixed(3)} = 总分 ${(v + k).toFixed(3)}`}>
      {v > 0 && <span className="h-full shrink-0 rounded-l-full" style={{ width: `${v * 100}%`, background: VIA_SEMANTIC }} />}
      {k > 0 && <span className={clsx('h-full shrink-0', !v && 'rounded-l-full')} style={{ width: `${k * 100}%`, background: VIA_KEYWORD }} />}
    </span>
  )
}

function Bar({ value, thin = false }: { value: number; thin?: boolean }) {
  const pct = Math.max(0, Math.min(1, Number(value) || 0)) * 100
  return (
    <span className={clsx('inline-block w-16 shrink-0 overflow-hidden rounded-full bg-hover', thin ? 'h-1' : 'h-1.5')} aria-hidden>
      <span className="block h-full rounded-full" style={{ width: `${pct}%`, background: thin ? 'var(--text-faint)' : 'var(--accent)' }} />
    </span>
  )
}

function AddDocModal({ open, collection, onClose, onSaved }: {
  open: boolean; collection: string; onClose: () => void; onSaved: () => void
}) {
  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    setBusy(true)
    try {
      await api.kb.ingest({ collection, title, content })
      setTitle(''); setContent('')
      onSaved()
      toast.ok('已导入并建立索引')
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open={open} onClose={onClose} title={`添加到「${collection}」`} width={640} dirty={!!content.trim()}
           footer={<>
             <button className="btn" onClick={onClose}>取消</button>
             <button className="btn btn-primary" disabled={busy || !content.trim()} onClick={() => void submit()}>
               {busy ? <Spinner size={11} /> : null} 导入
             </button>
           </>}>
      <div className="space-y-3">
        <Field label="标题" hint="留空就用内容的第一行">
          {(p) => <input {...p} className="field" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="可留空" />}
        </Field>
        <Field label="内容" required>
          {(p) => (
            <textarea {...p} className="field" rows={14} value={content} onChange={(e) => setContent(e.target.value)}
                      placeholder="粘贴文本，会按段落自动切块" />
          )}
        </Field>
      </div>
    </Modal>
  )
}

// -------------------------------------------------------------------------

const MEMORY_KIND: Record<string, string> = { fact: '事实', preference: '偏好', episode: '经历' }
const IMPORTANCE_STEPS = [0.2, 0.4, 0.6, 0.8, 1]

/** 重要度五格刻度：比「0.5」这样的裸数字好比较 */
function Importance({ value }: { value: number }) {
  const filled = Math.max(0, Math.min(5, Math.round((Number(value) || 0) * 5)))
  return (
    <span className="inline-flex items-center gap-1" title={`重要度 ${value}`}>
      <span className="text-faint">重要度</span>
      <span className="inline-flex gap-[2px]" aria-label={`重要度 ${filled} / 5`} role="img">
        {IMPORTANCE_STEPS.map((_, i) => (
          <span key={i} className="h-2 w-1 rounded-[1px]" style={{ background: i < filled ? 'var(--text-dim)' : 'var(--bg-hover)' }} />
        ))}
      </span>
    </span>
  )
}

/** 记忆从哪来：审查「这条口径能不能信」的第一件事 */
function MemorySourceLine({ item }: { item: MemoryItem }) {
  // 老后端没有 source，只在 meta 里记了 run_id / node_id
  const src: MemorySource | null = item.source
    ?? (item.meta?.run_id ? { kind: 'run', run_id: item.meta.run_id, node_id: item.meta.node_id } : null)
  if (!src) return null
  if (src.kind === 'manual') return <span>手动添加</span>
  if (src.kind === 'playground') return <span>在工具库里写入</span>
  if (src.kind !== 'run' || !src.run_id) return <span>{src.kind}</span>
  const bits = [src.node_label || src.node_id, src.workflow_name && runName(src)].filter(Boolean).join(' · ')
  const label = <>运行 <span className="mono">{shortId(src.run_id)}</span>{bits ? ` · ${bits}` : ''}</>
  return src.run_exists === false ? (
    <span title="写下它的那次运行已经删了">来源：{label}（运行已删除）</span>
  ) : (
    <Link to={`/runs/${src.run_id}`} className="underline decoration-dotted underline-offset-2 hover:text-fg" data-memory-source>
      来源：{label}
    </Link>
  )
}

type MemorySort = 'created' | 'recalled' | 'count'

function MemoryTab() {
  const [scope, setScope] = useState('default')
  const [scopes, setScopes] = useState<{ scope: string; count: number }[]>([])
  const [items, setItems] = useState<MemoryItem[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [text, setText] = useState('')
  const [kind, setKind] = useState('fact')
  const [writing, setWriting] = useState(false)
  const [query, setQuery] = useState('')
  const [recalling, setRecalling] = useState(false)
  const [hits, setHits] = useState<any[] | null>(null)
  const [filter, setFilter] = useState('')
  const [sort, setSort] = useState<MemorySort>('created')
  useTicker(60_000)

  const load = async () => {
    const [ss, list] = await Promise.allSettled([api.memory.scopes(), api.memory.list(scope)])
    if (ss.status === 'fulfilled') setScopes(ss.value)
    if (list.status === 'fulfilled') { setItems(withoutDeferred(list.value, '/api/memory')); setError(null) }
    else if (items) toast.error(list.reason)
    else setError(list.reason)
  }
  useEffect(() => { setItems(null); setHits(null); void load() }, [scope])
  useOnReconnect(load)

  const write = async () => {
    const content = text.trim()
    if (!content || writing) return
    setWriting(true)
    try {
      await api.memory.add({ content, scope, kind })
      setText('')
      await load()
      toast.ok('已记住')
    } catch (e) {
      toast.error(e)
    } finally {
      setWriting(false)
    }
  }

  // 回忆调试只看不计数（peek）：真实召回会给命中项 use_count += 1 并影响下次排序
  const recall = async () => {
    if (!query.trim()) { setHits(null); return }
    setRecalling(true)
    try {
      setHits((await api.memory.recall(query, { scope })).results ?? [])
    } catch (e) {
      toast.error(e)
    } finally {
      setRecalling(false)
    }
  }

  const remove = async (item: MemoryItem) => {
    const ok = await confirmDialog({
      title: '删除这条记忆？',
      body: <span className="line-clamp-3 text-fg">{item.content}</span>,
      consequences: [
        'agent 以后召回不到它：按这条口径算的数，下次可能就不一样了',
        '删除后 5 秒内可以撤销',
      ],
      danger: true,
      confirmLabel: '删除记忆',
    })
    if (!ok) return
    deferDelete({
      what: '一条记忆',
      url: `/api/memory/${item.id}`,
      hide: () => setItems((xs) => xs && xs.filter((x) => x.id !== item.id)),
      restore: () => void load(),
      commit: () => api.memory.remove(item.id),
      done: () => void load(),
    })
  }

  const newScope = async () => {
    const name = await promptDialog({
      title: '新的记忆作用域', label: '名称', placeholder: '比如 quality',
      validate: (v) => (/^[\w.-]{1,64}$/.test(v) ? null : '只用字母、数字、下划线、点和短横'),
      confirmLabel: '切过去',
    })
    if (name) setScope(name)
  }

  const shown = useMemo(() => {
    const needle = filter.trim().toLowerCase()
    const list = (items ?? []).filter((i) => !needle || i.content.toLowerCase().includes(needle))
    const t = (v?: string | null) => parseServerTime(v)?.getTime() ?? 0
    return [...list].sort((a, b) => sort === 'count' ? b.use_count - a.use_count
      : sort === 'recalled' ? t(b.last_used_at) - t(a.last_used_at)
      : t(b.created_at) - t(a.created_at))
  }, [items, filter, sort])

  const knownScope = scopes.some((s) => s.scope === scope)
  const scopeChoices = [...scopes, ...(knownScope ? [] : [{ scope, count: 0 }])]
  const radio = useRadioGroup(scopeChoices.map((s) => s.scope), scope, setScope)

  return (
    <div className="mx-auto max-w-4xl p-4">
      <SectionBar title="长期记忆" hint="跨运行保留。agent 用 memory_write / memory_recall 自己读写；口径类的事实会影响以后的计算，写之前想清楚。" />

      <div className="mb-3 flex flex-wrap items-center gap-1.5">
        <div role="radiogroup" aria-label="记忆作用域" className="flex flex-wrap items-center gap-1.5">
          {scopeChoices.map((s) => {
            const on = s.scope === scope
            return (
              <button key={s.scope} type="button" {...radio(s.scope)}
                      className={clsx('chip', on ? 'border-[var(--accent)] bg-accent-soft text-fg' : 'hover:border-[var(--border-strong)] hover:text-fg')}
                      onClick={() => setScope(s.scope)}>
                {on && <Check size={10} className="text-[var(--accent)]" aria-hidden />}
                <span className="font-medium">{s.scope}</span><span className="tnum text-faint">{formatNumber(s.count)}</span>
              </button>
            )
          })}
        </div>
        <button type="button" className="chip border-dashed text-faint hover:text-fg" onClick={() => void newScope()}>
          <Plus size={10} aria-hidden /> 新作用域
        </button>
      </div>

      <div className="mb-3 rounded-lg border bg-panel p-2.5">
        <textarea
          className="field min-h-0"
          rows={2}
          placeholder="写一条记忆，写完整的句子…（回车记住，Shift+回车换行）"
          aria-label="新记忆"
          value={text}
          data-memory-input
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // 组字时的回车是选词：不判这一条，拼音选词那一下就把半句话写进了长期记忆
            if (e.key !== 'Enter' || e.shiftKey || isComposing(e)) return
            e.preventDefault()
            void write()
          }}
        />
        <div className="mt-2 flex items-center gap-2">
          <select className="field h-7 w-24 py-0" value={kind} aria-label="记忆类型" onChange={(e) => setKind(e.target.value)}>
            {Object.entries(MEMORY_KIND).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
          <span className="text-2xs text-faint">写进「{scope}」</span>
          <button className="btn btn-primary btn-sm ml-auto" disabled={!text.trim() || writing} onClick={() => void write()}>
            {writing ? <Spinner size={11} /> : <Plus size={12} aria-hidden />} 记住
          </button>
        </div>
      </div>

      <div className="mb-3 rounded-lg border bg-panel p-2.5" data-memory-recall>
        <div className="flex gap-2">
          <input className="field" placeholder="测试回忆效果…" value={query} aria-label="回忆测试"
                 onChange={(e) => setQuery(e.target.value)}
                 onKeyDown={(e) => { if (e.key === 'Enter' && !isComposing(e)) void recall() }} />
          <button className="btn" onClick={() => void recall()} disabled={recalling}>
            {recalling ? <Spinner size={12} /> : <Search size={12} aria-hidden />} 回忆
          </button>
        </div>
        <p className="mt-1.5 text-2xs text-faint">只看，不计入召回次数：调试几下不会改变 agent 以后的排序。</p>
        {hits && (
          <div className="mt-2 border-t pt-1.5">
            {!hits.length && <div className="py-1.5 text-xs text-faint">没有召回任何记忆</div>}
            {hits.map((h) => (
              <div key={h.id} className="flex items-start gap-2 border-b border-[var(--hairline)] py-1.5 text-xs last:border-0">
                <span className="inline-flex shrink-0 items-center gap-1.5 pt-0.5">
                  <Bar value={h.score} />
                  <span className="mono tnum w-10 text-2xs text-dim">{Number(h.score).toFixed(3)}</span>
                </span>
                <span className="min-w-0 flex-1 leading-relaxed">{h.content}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {items === null ? (
        error ? <ErrorState error={error} onRetry={() => void load()} /> : <Skeleton rows={4} height={40} gap={8} />
      ) : !items.length ? (
        <EmptyState icon={<Brain size={22} />} title={knownScope ? '这个作用域还没有记忆' : `新作用域「${scope}」`}
                    body="记忆会跨运行保留，agent 可以用 memory_write / memory_recall 工具自己读写，也可以在上面手动写。" />
      ) : (
        <>
          <div className="mb-2 flex items-center gap-2">
            <div className="flex flex-1 items-center gap-1.5 rounded-md border bg-bg px-2">
              <Search size={12} className="text-faint" aria-hidden />
              <input className="h-7 min-w-0 flex-1 bg-transparent text-xs outline-none placeholder:text-faint"
                     placeholder={`在 ${items.length} 条里找…`} aria-label="过滤记忆" value={filter}
                     onChange={(e) => setFilter(e.target.value)} />
            </div>
            <select className="field h-7 w-32 py-0" value={sort} aria-label="排序" onChange={(e) => setSort(e.target.value as MemorySort)}>
              <option value="created">最近写入</option>
              <option value="recalled">最近召回</option>
              <option value="count">召回最多</option>
            </select>
          </div>
          <div className="space-y-1.5">
            {shown.map((item) => (
              <MemoryRow key={item.id} item={item} onRemove={() => void remove(item)}
                         onSaved={(next) => setItems((xs) => xs && xs.map((x) => (x.id === next.id ? { ...x, ...next } : x)))} />
            ))}
            {!shown.length && <div className="py-3 text-center text-xs text-faint">没有匹配「{filter}」的记忆</div>}
          </div>
        </>
      )}
    </div>
  )
}

function MemoryRow({ item, onRemove, onSaved }: {
  item: MemoryItem; onRemove: () => void; onSaved: (next: MemoryItem) => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState({ content: item.content, kind: item.kind, importance: item.importance })
  const [busy, setBusy] = useState(false)

  const start = () => {
    setDraft({ content: item.content, kind: item.kind, importance: item.importance })
    setEditing(true)
  }
  const save = async () => {
    const content = draft.content.trim()
    if (!content || busy) return
    setBusy(true)
    try {
      // 原地改：来源、记下的时间和召回记录都保留；改了内容后端会重算向量
      const next = await api.memory.update(item.id, { content, kind: draft.kind, importance: draft.importance })
      onSaved(next ?? { ...item, ...draft, content })
      setEditing(false)
      toast.ok('已更新这条记忆')
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }
  const onKey = (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (isComposing(e)) return
    if (matchShortcut(e.nativeEvent, 'Mod+Enter')) { e.preventDefault(); void save() }
    else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); setEditing(false) }
  }

  return (
    <div className="group flex items-start gap-3 rounded-lg border bg-panel px-3 py-2" data-memory={item.id}>
      <div className="min-w-0 flex-1">
        {editing ? (
          <div>
            <textarea className="field" rows={Math.min(8, Math.max(2, Math.ceil(draft.content.length / 60)))} autoFocus
                      aria-label="修改记忆内容" value={draft.content} onKeyDown={onKey}
                      onChange={(e) => setDraft({ ...draft, content: e.target.value })} />
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <select className="field h-7 w-24 py-0" value={draft.kind} aria-label="记忆类型"
                      onChange={(e) => setDraft({ ...draft, kind: e.target.value })}>
                {Object.entries(MEMORY_KIND).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                {!MEMORY_KIND[draft.kind] && <option value={draft.kind}>{draft.kind}</option>}
              </select>
              <select className="field h-7 w-28 py-0" value={String(draft.importance)} aria-label="重要度"
                      onChange={(e) => setDraft({ ...draft, importance: Number(e.target.value) })}>
                {[...new Set([...IMPORTANCE_STEPS, item.importance])].sort().map((v) => (
                  <option key={v} value={String(v)}>重要度 {v}</option>
                ))}
              </select>
              <span className="text-2xs text-faint">⌘/Ctrl+回车保存，Esc 取消</span>
              <span className="flex-1" />
              <button className="btn btn-sm btn-ghost" onClick={() => setEditing(false)} disabled={busy}>取消</button>
              <button className="btn btn-sm btn-primary" onClick={() => void save()} disabled={busy || !draft.content.trim()}>
                {busy ? <Spinner size={11} /> : null} 保存
              </button>
            </div>
          </div>
        ) : (
          <div className="whitespace-pre-wrap text-sm leading-relaxed">{item.content}</div>
        )}
        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-2xs text-faint">
          <span className="chip">{MEMORY_KIND[item.kind] ?? item.kind}</span>
          <Importance value={item.importance} />
          <span className="tnum">被召回 {formatNumber(item.use_count)} 次</span>
          {item.created_at && (
            <span title={formatDateTime(item.created_at)}>记下于 {formatTime(item.created_at)}</span>
          )}
          <span title={item.last_used_at ? formatDateTime(item.last_used_at) : undefined}>
            {item.last_used_at ? `上次召回 ${formatRelative(item.last_used_at)}` : '还没被召回过'}
          </span>
          <MemorySourceLine item={item} />
        </div>
      </div>
      {!editing && (
        <div className="flex shrink-0 items-center gap-0.5">
          <IconButton label="修改这条记忆" icon={<Pencil size={12} />} className="text-faint" onClick={start} />
          <DeleteButton label="删除这条记忆" onClick={onRemove} />
        </div>
      )}
    </div>
  )
}

// -------------------------------------------------------------------------

function SkillsTab() {
  // catalog 随时会被别处刷新（重连、别的页面保存）：撤销窗口里的那条要滤掉，不然刷一下就回来了
  const skills = withoutDeferred(useCatalog((s) => s.skills), '/api/skills')
  const refresh = useCatalog((s) => s.refresh)
  const [editing, setEditing] = useState<Partial<Skill> | null>(null)

  useEffect(() => { void refresh() }, [])

  const remove = (skill: Skill) => deferDelete({
    what: ` Skill「${skill.name}」`,
    url: `/api/skills/${skill.id}`,
    // catalog 里的 Skill 是全站共用的：先从那里拿掉，撤销时刷新回来
    hide: () => useCatalog.setState((s) => ({ skills: s.skills.filter((x) => x.id !== skill.id) })),
    restore: () => void refresh(),
    commit: () => api.skills.remove(skill.id),
    done: () => void refresh(),
  })

  const create = () => setEditing({ examples: [], tags: [], enabled: true })

  return (
    <div className="mx-auto max-w-4xl p-4">
      <SectionBar title="方法论 Skill" hint="可复用的做事方法。挂到 LLM / Agent 节点上，运行时注入 system prompt。">
        <button className="btn btn-primary btn-sm" onClick={create}><Plus size={12} aria-hidden /> 新建 Skill</button>
      </SectionBar>

      {!skills.length ? (
        <EmptyState
          icon={<Sparkles size={22} />}
          title="还没有 Skill"
          source="skills"
          body="把「先给结论、区分事实与推断」「口径卡怎么写」这类做事方法写成 Skill，挂到节点上复用。"
          action={<button className="btn btn-primary btn-sm" onClick={create}><Plus size={12} aria-hidden /> 新建 Skill</button>}
        />
      ) : (
        <div className="space-y-2.5">
          {skills.map((skill) => (
            <article key={skill.id} className="rounded-lg border bg-panel p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium">{skill.name}</span>
                {skill.tags?.map((t) => <span key={t} className="chip">{t}</span>)}
                {!skill.enabled && <span className="chip">已停用</span>}
                <div className="flex-1" />
                <button className="btn btn-sm btn-ghost" onClick={() => setEditing(skill)}>编辑</button>
                <DeleteButton label={`删除 Skill ${skill.name}`} onClick={() => remove(skill)} />
              </div>
              {skill.description && <div className="mt-1 text-xs text-faint">{skill.description}</div>}
              <pre className="mono mt-2 max-h-32 overflow-auto whitespace-pre-wrap rounded-md border bg-bg p-2 text-2xs leading-relaxed text-dim">
                {skill.instructions}
              </pre>
            </article>
          ))}
        </div>
      )}

      {editing && (
        <SkillEditor skill={editing} onClose={() => setEditing(null)}
                     onSaved={async (name) => { setEditing(null); await refresh(); toast.ok(`已保存 Skill「${name}」`) }} />
      )}
    </div>
  )
}

/** 「质量,工艺」「质量，工艺」都拆成两个标签 */
const splitTags = (raw: string) => raw.split(/[,，、]/).map((s) => s.trim()).filter(Boolean)

/**
 * 标签输入：回车或逗号（中英文都认）生成一个标签，退格删最后一个。
 *
 * 以前 value 是 tags.join(',')、onChange 当场 split，刚敲下的逗号立刻被吃掉，
 * 逐字输入「质量,工艺」得到的是「质量工艺」一个标签。现在输入框保留原文，
 * 遇到分隔符才收成标签；组字期间不收（拼音里没有逗号，但选词的回车不能当提交）。
 */
function TagInput({ id, tags, text, onTags, onText, suggestions }: {
  id?: string
  tags: string[]
  text: string
  onTags: (tags: string[]) => void
  onText: (text: string) => void
  suggestions: string[]
}) {
  const commit = (raw: string) => {
    const parts = splitTags(raw)
    if (parts.length) onTags([...new Set([...tags, ...parts])])
    onText('')
  }
  return (
    <div className="field flex min-h-[34px] flex-wrap items-center gap-1 py-1" onClick={(e) => (e.currentTarget.querySelector('input') as HTMLInputElement | null)?.focus()}>
      {tags.map((t) => (
        <span key={t} className="chip text-fg">
          {t}
          <button type="button" className="-mr-1 rounded p-px text-faint hover:text-[var(--err)]" aria-label={`去掉标签 ${t}`}
                  title="去掉" onClick={() => onTags(tags.filter((x) => x !== t))}>
            <X size={10} />
          </button>
        </span>
      ))}
      <input
        id={id}
        className="min-w-24 flex-1 bg-transparent text-xs outline-none placeholder:text-faint"
        list="skill-tag-suggestions"
        value={text}
        placeholder={tags.length ? '' : '回车或逗号分隔，比如 出具、口径卡'}
        onChange={(e) => {
          const v = e.target.value
          const composing = (e.nativeEvent as InputEvent).isComposing
          if (!composing && /[,，、]/.test(v)) {
            // 分隔符前面的收成标签，最后一个分隔符后面的留在框里接着敲
            const cut = Math.max(v.lastIndexOf(','), v.lastIndexOf('，'), v.lastIndexOf('、'))
            const parts = splitTags(v.slice(0, cut))
            if (parts.length) onTags([...new Set([...tags, ...parts])])
            onText(v.slice(cut + 1))
          } else {
            onText(v)
          }
        }}
        onKeyDown={(e) => {
          if (isComposing(e)) return
          if (e.key === 'Enter') { e.preventDefault(); commit(text) }
          else if (e.key === 'Backspace' && !text && tags.length) onTags(tags.slice(0, -1))
        }}
        onBlur={() => { if (text.trim()) commit(text) }}
      />
      <datalist id="skill-tag-suggestions">
        {suggestions.filter((s) => !tags.includes(s)).map((s) => <option key={s} value={s} />)}
      </datalist>
    </div>
  )
}

function SkillEditor({ skill, onClose, onSaved }: {
  skill: Partial<Skill>; onClose: () => void; onSaved: (name: string) => void
}) {
  const skills = useCatalog((s) => s.skills)
  const [initial] = useState(() => ({
    name: skill.name ?? '', description: skill.description ?? '',
    instructions: skill.instructions ?? '', tags: skill.tags ?? [],
    examples: skill.examples ?? [], suggested_tools: skill.suggested_tools ?? [],
    enabled: skill.enabled ?? true,
  }))
  const [form, setForm] = useState<any>(initial)
  const [tagText, setTagText] = useState('')
  const [busy, setBusy] = useState(false)
  const suggestions = [...new Set(skills.flatMap((s) => s.tags ?? []))]
  const dirty = JSON.stringify(form) !== JSON.stringify(initial) || !!tagText.trim()

  const submit = async () => {
    if (!form.name.trim() || busy) return
    setBusy(true)
    // 框里还没收成标签的那半截也算上：点保存之前不必先按回车
    const body = { ...form, tags: [...new Set([...form.tags, ...splitTags(tagText)])] }
    try {
      if (skill.id) await api.skills.update(skill.id, body)
      else await api.skills.create(body)
      onSaved(body.name)
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open onClose={onClose} dirty={dirty} title={skill.id ? `编辑 Skill「${skill.name}」` : '新建 Skill'} width={640}
           footer={<>
             <button className="btn" onClick={onClose}>取消</button>
             <button className="btn btn-primary" disabled={!form.name.trim() || busy} onClick={() => void submit()}>
               {busy ? <Spinner size={11} /> : null} 保存
             </button>
           </>}>
      <div className="space-y-3">
        <Field label="名称" required>
          {(p) => <input {...p} className="field" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />}
        </Field>
        <Field label="一句话说明">
          {(p) => (
            <input {...p} className="field" value={form.description}
                   onChange={(e) => setForm({ ...form, description: e.target.value })} />
          )}
        </Field>
        <Field label="指令内容" hint="会拼进 system prompt">
          {(p) => (
            <textarea {...p} className="field" rows={10} value={form.instructions}
                      placeholder={'分析问题时遵循：\n1. 先给结论\n2. 区分事实与推断'}
                      onChange={(e) => setForm({ ...form, instructions: e.target.value })} />
          )}
        </Field>
        <Field label="标签" htmlFor="skill-tags" hint="回车、逗号或顿号分隔；已有的标签会出现在补全里">
          <TagInput id="skill-tags" tags={form.tags} text={tagText} suggestions={suggestions}
                    onTags={(tags) => setForm((f: any) => ({ ...f, tags }))} onText={setTagText} />
        </Field>
        <label className="flex items-center gap-2 text-xs">
          <input type="checkbox" checked={!!form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
          启用<span className="text-2xs text-faint">停用后节点上挂着也不注入</span>
        </label>
      </div>
    </Modal>
  )
}
