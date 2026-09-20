import { useEffect, useRef, useState } from 'react'
import { BookOpen, Brain, Plus, Search, Sparkles, Trash2, Upload } from 'lucide-react'
import { api } from '../api/client'
import { useCatalog } from '../store/catalog'
import { Empty, Modal, Spinner, Tabs, useToast } from '../components/ui'
import type { KbDocument, MemoryItem, Skill } from '../types'

export function KnowledgePage() {
  const [tab, setTab] = useState('kb')
  return (
    <div className="flex h-full flex-col">
      <Tabs
        tabs={[
          { key: 'kb', label: '知识库' },
          { key: 'memory', label: '长期记忆' },
          { key: 'skills', label: '方法论 Skill' },
        ]}
        active={tab}
        onChange={setTab}
      />
      <div className="min-h-0 flex-1 overflow-y-auto">
        {tab === 'kb' && <KbTab />}
        {tab === 'memory' && <MemoryTab />}
        {tab === 'skills' && <SkillsTab />}
      </div>
    </div>
  )
}

// -------------------------------------------------------------------------

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
function EmbedderBar({ collection, onChanged }: {
  collection: string
  onChanged: () => void | Promise<void>
}) {
  const toast = useToast()
  const [info, setInfo] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState(false)
  const [baseUrl, setBaseUrl] = useState('')
  const [models, setModels] = useState<string[]>([])
  const [model, setModel] = useState('')

  const refresh = async () => {
    const next = await api.kb.embedding(collection).catch(() => null)
    setInfo(next)
    if (next) { setBaseUrl(next.base_url || ''); setModel(next.model || '') }
  }
  useEffect(() => { void refresh() }, [collection])

  if (!info) return null
  // 记忆和知识库共用一个 embedder，换模型时一起失效。只报一个的话，
  // 用户点完重建还是想不起事，而且不知道为什么
  const staleChunks = info.stale_chunks ?? 0
  const staleMemories = info.stale_memories ?? 0
  // 没建倒排的片段也归这个按钮管：它们走全表扫，结果对但慢，而重建正好
  // 把倒排一起建了。按钮只认"向量对不上"的话，这些片段永远等不到人来点
  const unindexed = info.unindexed_chunks ?? 0
  const stale = staleChunks + staleMemories + unindexed

  const pick = async (kind: string, m = '', url = '') => {
    setBusy(true)
    try {
      const out = await api.kb.setEmbedding({ kind, model: m, base_url: url })
      // 换模型之后维度多半变了，存量向量全部作废——这件事要当场说，
      // 而不是等用户发现"最近搜得不准"
      const stale = (out.stale_chunks ?? 0) + (out.stale_memories ?? 0)
      toast(`已切到 ${out.embedder}（${out.dim} 维）`
            + (stale ? `，${stale} 条存量向量需要重建` : ''), 'ok')
      setEditing(false)
      await refresh()
    } catch (e: any) {
      // 400 里写的是"连不上哪个地址""哪个模型名不对"，都能照着做
      toast(e?.message ?? '切换失败', 'error')
    } finally { setBusy(false) }
  }

  const probe = async () => {
    setBusy(true)
    try {
      const out = await api.kb.probeEmbedding(baseUrl)
      setModels(out.models)
      if (out.models.length === 1) setModel(out.models[0])
      if (!out.models.length) toast('这个地址上没有可用模型', 'error')
    } catch (e: any) {
      toast(e?.message ?? '连不上', 'error')
    } finally { setBusy(false) }
  }

  const rebuild = async () => {
    setBusy(true)
    try {
      const out = await api.kb.reindex(collection)
      const mem = out.memories_reindexed ?? 0
      toast(`已用 ${out.embedder} 重建 ${out.reindexed} 段知识`
            + (mem ? ` 和 ${mem} 条记忆` : ''), 'ok')
      await refresh()
      await onChanged()
    } catch (e: any) {
      toast(e?.message ?? '重建失败', 'error')
    } finally { setBusy(false) }
  }

  return (
    <div className="mb-3 rounded-lg border bg-panel p-3 text-[12px]">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-faint">向量模型</span>
        <span className="mono text-[11px]">{info.embedder} · {info.dim} 维</span>
        {info.kind === 'local' && (
          <span className="text-[10.5px]" style={{ color: 'var(--warn)' }}>没有语义能力</span>
        )}
        <button className="btn btn-sm btn-ghost" disabled={busy}
                onClick={() => setEditing((v) => !v)}>
          {editing ? '收起' : '换一个'}
        </button>
        <span className="flex-1" />
        {stale > 0 && (
          <button className="btn btn-sm btn-primary" disabled={busy} onClick={rebuild}>
            重建索引（{stale} 段）
          </button>
        )}
      </div>
      {editing && (
        <div className="mt-3 space-y-2 border-t pt-3">
          <button className="btn btn-sm w-full justify-start" disabled={busy}
                  onClick={() => void pick('local')}>
            本地哈希向量 — 零配置、不联网、免费，但只认字面不认语义
          </button>

          <div className="rounded-lg border p-2">
            <div className="mb-1.5 text-[11px] text-faint">
              自定义端点 — 任何讲 OpenAI /v1/embeddings 的服务：本机的 LM Studio、
              Ollama、vLLM、TEI，或你自己的网关
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              <input className="field h-7 flex-1" placeholder="http://127.0.0.1:1234/v1"
                     value={baseUrl} disabled={busy}
                     onChange={(e) => setBaseUrl(e.target.value)} />
              {/* 模型名手填太容易错：LM Studio 里叫 text-embedding-qwen3-embedding-4b，
                  不是 Qwen3-Embedding-4B。填错的表现是切换时 404，人还以为服务没起 */}
              <button className="btn btn-sm" disabled={busy || !baseUrl.trim()}
                      onClick={() => void probe()}>看看有哪些模型</button>
            </div>
            {!!models.length && (
              <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                <select className="field h-7 flex-1" value={model} disabled={busy}
                        onChange={(e) => setModel(e.target.value)}>
                  {models.map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
                <button className="btn btn-sm btn-primary" disabled={busy || !model}
                        onClick={() => void pick('openai', model, baseUrl)}>用它</button>
              </div>
            )}
            <p className="mt-1.5 text-[10.5px] text-faint">
              留空地址则走 api.openai.com（要 OPENAI_API_KEY，按量计费）。
              换模型后维度多半会变，存量向量要重建一次。
            </p>
          </div>
        </div>
      )}

      {staleChunks + staleMemories > 0 && (
        <p className="mt-2 text-[11px]" style={{ color: 'var(--warn)' }}>
          有{staleChunks ? ` ${staleChunks} 段知识` : ''}
          {staleChunks && staleMemories ? '、' : ''}
          {staleMemories ? ` ${staleMemories} 条记忆` : ''}
          的向量是用别的模型建的，和当前模型对不上——检索/召回这些内容时
          会退回纯关键词（搜不出同义表达）。重建索引后恢复。
        </p>
      )}
      {unindexed > 0 && staleChunks + staleMemories === 0 && (
        <p className="mt-2 text-[11px] text-faint">
          有 {unindexed} 段还没建倒排索引，检索它们会退回全表扫——结果是对的，
          只是慢。重建一次就好。
        </p>
      )}
      {info.kind !== 'openai' && stale === 0 && (
        <p className="mt-2 text-[11px] text-faint">
          本地向量不联网、零配置，但它是词频哈希、没有语义泛化——
          「管理员」搜不到「平台角色」。认真用知识库的话换成上面的真模型。
        </p>
      )}
    </div>
  )
}

// -------------------------------------------------------------------------

function KbTab() {
  const toast = useToast()
  const refreshCatalog = useCatalog((s) => s.refresh)
  const [collection, setCollection] = useState('default')
  const [collections, setCollections] = useState<any[]>([])
  const [docs, setDocs] = useState<KbDocument[]>([])
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<any[] | null>(null)
  // 初值跟着 embedder 能力走：本地哈希向量下给向量权重会让命中率下降
  const [alpha, setAlpha] = useState(0)
  const [adding, setAdding] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const load = async () => {
    setCollections(await api.kb.collections().catch(() => []))
    setDocs(await api.kb.documents(collection).catch(() => []))
  }
  useEffect(() => { void load() }, [collection])

  const search = async () => {
    if (!query.trim()) { setHits(null); return }
    const res = await api.kb.search(query, collection, alpha)
    setHits(res.results ?? [])
  }

  const upload = async (files: FileList | null) => {
    if (!files?.length) return
    for (const file of Array.from(files)) {
      try {
        await api.kb.upload(file, collection)
        toast(`已导入 ${file.name}`, 'ok')
      } catch (e: any) {
        toast(`${file.name}：${e.message}`, 'error')
      }
    }
    await load(); await refreshCatalog()
  }

  return (
    <div className="mx-auto max-w-4xl p-4">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input className="field w-44" value={collection} list="collections"
               onChange={(e) => setCollection(e.target.value)} placeholder="知识库名称" />
        <datalist id="collections">
          {collections.map((c) => <option key={c.collection} value={c.collection} />)}
        </datalist>
        <div className="flex-1" />
        <button className="btn" onClick={() => fileRef.current?.click()}>
          <Upload size={12} /> 上传文件
        </button>
        {/* 后端认得的格式这里就要放行，否则解析做了也白做——选文件时压根
            选不中 PDF 和 Word。表格类（xlsx）故意不放：它进知识库会被切成
            文本，聚合算不了、数字也无法回指，那条路要走数据源 */}
        <input ref={fileRef} type="file" multiple hidden
               accept=".pdf,.docx,.html,.htm,.txt,.md,.json,.csv,.log,.yaml,.yml,.py,.ts,.js"
               onChange={(e) => upload(e.target.files)} />
        <button className="btn btn-primary" onClick={() => setAdding(true)}>
          <Plus size={12} /> 粘贴文本
        </button>
      </div>

      <div className="mb-3 flex flex-wrap gap-1.5">
        {collections.map((c) => (
          <button key={c.collection} className="chip hover:border-[var(--accent)]"
                  onClick={() => setCollection(c.collection)}
                  style={c.collection === collection ? { borderColor: 'var(--accent)', color: 'var(--accent)' } : undefined}>
            {c.collection} · {c.documents} 篇 / {c.chunks} 片段
          </button>
        ))}
      </div>

      <EmbedderBar collection={collection} onChanged={load} />

      {/* 检索调试：能直观看到混合检索里语义和关键词各占多少 */}
      <div className="mb-4 rounded-lg border bg-panel p-3">
        <div className="mb-2 flex items-center gap-2">
          <input className="field" placeholder="测试检索效果…" value={query}
                 onChange={(e) => setQuery(e.target.value)}
                 onKeyDown={(e) => e.key === 'Enter' && search()} />
          <button className="btn" onClick={search}><Search size={12} /> 检索</button>
        </div>
        <label className="flex items-center gap-2 text-[11px] text-faint">
          关键词
          <input type="range" min={0} max={1} step={0.1} value={alpha} className="flex-1 accent-[var(--accent)]"
                 onChange={(e) => setAlpha(Number(e.target.value))} />
          语义
          <span className="w-8 text-right">{alpha.toFixed(1)}</span>
        </label>

        {hits && (
          <div className="mt-3 space-y-2">
            {!hits.length && <div className="text-[11px] text-faint">没有命中</div>}
            {hits.map((hit) => (
              <div key={hit.chunk_id} className="rounded border bg-bg p-2">
                <div className="mb-1 flex items-center gap-2 text-[10.5px] text-faint">
                  <span className="font-medium text-dim">{hit.title}</span>
                  <span>片段 {hit.ordinal}</span>
                  <span className="ml-auto">总分 {hit.score}</span>
                  <span>语义 {hit.signals?.vector?.toFixed(3)}</span>
                  <span>关键词 {hit.signals?.keyword?.toFixed(2)}</span>
                </div>
                <div className="line-clamp-3 text-[11px] leading-relaxed">{hit.content}</div>
              </div>
            ))}
          </div>
        )}
      </div>

      {!docs.length && <Empty icon={<BookOpen size={22} />} title="这个知识库还是空的"
                             hint="上传文本文件或粘贴内容，会自动切块并建立索引。" />}
      <div className="space-y-1.5">
        {docs.map((doc) => (
          <div key={doc.id} className="flex items-center gap-3 rounded-lg border bg-panel px-3 py-2">
            <div className="min-w-0 flex-1">
              <div className="truncate text-[12px]">{doc.title}</div>
              <div className="truncate text-[10.5px] text-faint">{doc.source} · {doc.chunk_count} 个片段</div>
            </div>
            <button className="btn btn-sm btn-ghost" onClick={async () => {
              if (!confirm(`删除「${doc.title}」？`)) return
              await api.kb.remove(doc.id); await load(); await refreshCatalog()
            }}>
              <Trash2 size={11} className="text-[var(--err)]" />
            </button>
          </div>
        ))}
      </div>

      <AddDocModal open={adding} collection={collection} onClose={() => setAdding(false)}
                   onSaved={async () => { setAdding(false); await load(); await refreshCatalog() }} />
    </div>
  )
}

function AddDocModal({ open, collection, onClose, onSaved }: {
  open: boolean; collection: string; onClose: () => void; onSaved: () => void
}) {
  const toast = useToast()
  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [busy, setBusy] = useState(false)

  return (
    <Modal open={open} onClose={onClose} title={`添加到「${collection}」`} width={640}
           footer={<>
             <button className="btn" onClick={onClose}>取消</button>
             <button className="btn btn-primary" disabled={busy || !content.trim()} onClick={async () => {
               setBusy(true)
               try {
                 await api.kb.ingest({ collection, title, content })
                 setTitle(''); setContent(''); onSaved(); toast('已导入并建立索引', 'ok')
               } catch (e: any) { toast(e.message ?? '导入失败', 'error') } finally { setBusy(false) }
             }}>{busy ? <Spinner /> : '导入'}</button>
           </>}>
      <div className="mb-3">
        <label className="label">标题</label>
        <input className="field" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="可留空" />
      </div>
      <div>
        <label className="label">内容</label>
        <textarea className="field" rows={14} value={content} onChange={(e) => setContent(e.target.value)}
                  placeholder="粘贴文本，会按段落自动切块" />
      </div>
    </Modal>
  )
}

// -------------------------------------------------------------------------

function MemoryTab() {
  const toast = useToast()
  const [scope, setScope] = useState('default')
  const [scopes, setScopes] = useState<any[]>([])
  const [items, setItems] = useState<MemoryItem[]>([])
  const [text, setText] = useState('')
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<any[] | null>(null)

  const load = async () => {
    setScopes(await api.memory.scopes().catch(() => []))
    setItems(await api.memory.list(scope).catch(() => []))
  }
  useEffect(() => { void load() }, [scope])

  return (
    <div className="mx-auto max-w-3xl p-4">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input className="field w-44" value={scope} list="scopes" placeholder="作用域"
               onChange={(e) => setScope(e.target.value)} />
        <datalist id="scopes">{scopes.map((s) => <option key={s.scope} value={s.scope} />)}</datalist>
        {scopes.map((s) => (
          <button key={s.scope} className="chip hover:border-[var(--accent)]" onClick={() => setScope(s.scope)}
                  style={s.scope === scope ? { borderColor: 'var(--accent)', color: 'var(--accent)' } : undefined}>
            {s.scope} · {s.count}
          </button>
        ))}
      </div>

      <div className="mb-3 flex gap-2">
        <input className="field" placeholder="写一条记忆，写完整的句子…" value={text}
               onChange={(e) => setText(e.target.value)}
               onKeyDown={async (e) => {
                 if (e.key !== 'Enter' || !text.trim()) return
                 await api.memory.add({ content: text, scope })
                 setText(''); await load(); toast('已记住', 'ok')
               }} />
        <button className="btn btn-primary" disabled={!text.trim()} onClick={async () => {
          await api.memory.add({ content: text, scope }); setText(''); await load(); toast('已记住', 'ok')
        }}>
          <Plus size={12} /> 记住
        </button>
      </div>

      <div className="mb-3 flex gap-2">
        <input className="field" placeholder="测试回忆效果…" value={query}
               onChange={(e) => setQuery(e.target.value)}
               onKeyDown={async (e) => {
                 if (e.key !== 'Enter') return
                 setHits((await api.memory.search(query, scope)).results ?? [])
               }} />
        <button className="btn" onClick={async () => setHits((await api.memory.search(query, scope)).results ?? [])}>
          <Search size={12} /> 回忆
        </button>
      </div>

      {hits && (
        <div className="mb-4 rounded-lg border bg-panel p-2">
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-faint">回忆结果</div>
          {!hits.length && <div className="px-1 py-2 text-[11px] text-faint">没有召回任何记忆</div>}
          {hits.map((h) => (
            <div key={h.id} className="border-b px-1 py-1.5 text-[11px] last:border-0">
              <span className="mr-2 text-[var(--accent)]">{h.score}</span>
              {h.content}
            </div>
          ))}
        </div>
      )}

      {!items.length && <Empty icon={<Brain size={22} />} title="这个作用域还没有记忆"
                             hint="记忆会跨运行保留，agent 可以用 memory_write / memory_recall 工具自己读写。" />}
      <div className="space-y-1.5">
        {items.map((item) => (
          <div key={item.id} className="flex items-start gap-3 rounded-lg border bg-panel px-3 py-2">
            <div className="min-w-0 flex-1">
              <div className="text-[11.5px] leading-relaxed">{item.content}</div>
              <div className="mt-1 flex gap-2 text-[10px] text-faint">
                <span className="chip">{item.kind}</span>
                <span>重要度 {item.importance}</span>
                <span>被召回 {item.use_count} 次</span>
              </div>
            </div>
            <button className="btn btn-sm btn-ghost" onClick={async () => {
              await api.memory.remove(item.id); await load()
            }}>
              <Trash2 size={11} className="text-[var(--err)]" />
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}

// -------------------------------------------------------------------------

function SkillsTab() {
  const toast = useToast()
  const { skills, refresh } = useCatalog()
  const [editing, setEditing] = useState<Partial<Skill> | null>(null)

  useEffect(() => { void refresh() }, [])

  return (
    <div className="mx-auto max-w-3xl p-4">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <div className="text-sm font-semibold">方法论 Skill</div>
          <div className="text-[11px] text-faint">
            可复用的做事方法。挂到 LLM / Agent 节点上，运行时注入 system prompt
          </div>
        </div>
        <button className="btn btn-primary" onClick={() => setEditing({ examples: [], tags: [], enabled: true })}>
          <Plus size={12} /> 新建
        </button>
      </div>

      {!skills.length && <Empty icon={<Sparkles size={22} />} title="还没有 Skill" />}
      <div className="space-y-2">
        {skills.map((skill) => (
          <div key={skill.id} className="rounded-lg border bg-panel p-3">
            <div className="flex items-center gap-2">
              <span className="text-[12.5px] font-medium">{skill.name}</span>
              {skill.tags?.map((t) => <span key={t} className="chip">{t}</span>)}
              <div className="flex-1" />
              <button className="btn btn-sm" onClick={() => setEditing(skill)}>编辑</button>
              <button className="btn btn-sm btn-ghost" onClick={async () => {
                if (!confirm(`删除「${skill.name}」？`)) return
                await api.skills.remove(skill.id); await refresh()
              }}>
                <Trash2 size={11} className="text-[var(--err)]" />
              </button>
            </div>
            <div className="mt-1 text-[11px] text-faint">{skill.description}</div>
            <pre className="mono mt-2 max-h-32 overflow-auto whitespace-pre-wrap rounded border bg-bg p-2 text-[10.5px] leading-relaxed text-dim">
              {skill.instructions}
            </pre>
          </div>
        ))}
      </div>

      {editing && (
        <SkillEditor skill={editing} onClose={() => setEditing(null)}
                     onSaved={async () => { setEditing(null); await refresh(); toast('已保存', 'ok') }} />
      )}
    </div>
  )
}

function SkillEditor({ skill, onClose, onSaved }: {
  skill: Partial<Skill>; onClose: () => void; onSaved: () => void
}) {
  const toast = useToast()
  const [form, setForm] = useState<any>({
    name: skill.name ?? '', description: skill.description ?? '',
    instructions: skill.instructions ?? '', tags: skill.tags ?? [],
    examples: skill.examples ?? [], suggested_tools: skill.suggested_tools ?? [],
    enabled: skill.enabled ?? true,
  })

  return (
    <Modal open onClose={onClose} title={skill.id ? '编辑 Skill' : '新建 Skill'} width={620}
           footer={<>
             <button className="btn" onClick={onClose}>取消</button>
             <button className="btn btn-primary" disabled={!form.name} onClick={async () => {
               try {
                 if (skill.id) await api.skills.update(skill.id, form)
                 else await api.skills.create(form)
                 onSaved()
               } catch (e: any) { toast(e.message ?? '保存失败', 'error') }
             }}>保存</button>
           </>}>
      <div className="mb-3">
        <label className="label">名称</label>
        <input className="field" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
      </div>
      <div className="mb-3">
        <label className="label">一句话说明</label>
        <input className="field" value={form.description}
               onChange={(e) => setForm({ ...form, description: e.target.value })} />
      </div>
      <div className="mb-3">
        <label className="label">指令内容（会拼进 system prompt）</label>
        <textarea className="field" rows={10} value={form.instructions}
                  placeholder={'分析问题时遵循：\n1. 先给结论\n2. 区分事实与推断'}
                  onChange={(e) => setForm({ ...form, instructions: e.target.value })} />
      </div>
      <div>
        <label className="label">标签（逗号分隔）</label>
        <input className="field" value={(form.tags ?? []).join(',')}
               onChange={(e) => setForm({ ...form, tags: e.target.value.split(',').map((s: string) => s.trim()).filter(Boolean) })} />
      </div>
    </Modal>
  )
}
