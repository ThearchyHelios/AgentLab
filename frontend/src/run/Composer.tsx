import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { ArrowUp, Database, Settings2, Sparkles, Square, Wrench } from 'lucide-react'
import clsx from 'clsx'
import { api } from '../api/client'
import { useCatalog, modelOptions } from '../store/catalog'
import { useStudio } from '../store/studio'
import { isComposing, Spinner } from '../components/ui'

/**
 * Copilot 的输入。
 *
 * 一个组件两种体量，而不是两个组件：
 *
 * - **hero**：还没说过话的时候，它就是这一栏的主体，竖直居中、三行高、自动
 *   聚焦，配上例句。这时候"写点什么"是唯一该做的事，输入框就该是最大的东西。
 *   之前它是一个 38px 高的框挤在长长的流底下，比它上面任何一块都不起眼——
 *   用户说"没有一个很好的地方引导写问题"，说的就是这个。
 * - **dock**：一旦有了对话，注意力转移到上面的过程上，它退到底部两行。
 *
 * 不做成两个组件的原因和 AssistantStream 一样：两个组件就是两个实例，切换时
 * React 会卸载其中一个，草稿、光标、输入法编辑中的候选词全没。
 */
export function Composer({ hero, onFocusChange }: {
  hero: boolean
  onFocusChange?: (focused: boolean) => void
}) {
  const nodes = useStudio((s) => s.nodes)
  const copilot = useStudio((s) => s.copilot)
  const { runCopilot, stopCopilot, retryCopilot } = useStudio()
  const providers = useCatalog((s) => s.providers)
  const tools = useCatalog((s) => s.tools)
  const [text, setText] = useState('')
  const [opts, setOpts] = useState(false)
  const [model, setModel] = useState('')
  const [effective, setEffective] = useState('')
  const [useBase, setUseBase] = useState(true)
  const [sources, setSources] = useState<any[]>([])
  const ref = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    void api.copilot.getModel().then((m) => {
      setModel(m.model ?? '')
      setEffective(m.effective_model ?? '')
    }).catch(() => undefined)
    void api.datasources.list().then(setSources).catch(() => undefined)
  }, [])

  // 工具栏的 Copilot 按钮把焦点甩过来
  useEffect(() => {
    const focus = () => ref.current?.focus()
    window.addEventListener('agentlab:focus-copilot', focus)
    return () => window.removeEventListener('agentlab:focus-copilot', focus)
  }, [])

  // hero 态就是"请开始"，不该还要求用户先点一下
  useEffect(() => {
    if (hero) ref.current?.focus()
  }, [hero])

  const pickModel = (value: string) => {
    setModel(value)
    void api.copilot.setModel({ model: value || null })
      .then((m) => setEffective(m.effective_model ?? ''))
      .catch(() => undefined)
  }

  const send = (override?: string) => {
    const q = (override ?? text).trim()
    if (!q || copilot.active) return
    runCopilot(q, useBase && nodes.length > 0, model || undefined)
    setText('')
  }

  const options = modelOptions(providers)
  const groups = [...new Set(options.map((o) => o.group))]
  const examples = hero ? exampleFor(nodes.length, sources) : []

  return (
    <div className={clsx('shrink-0', hero ? 'px-4' : 'border-t px-2.5 pb-2.5 pt-2')}>
      {hero && <HeroHeader sources={sources} toolCount={tools?.length ?? 0} />}

      {copilot.error && (
        <div role="alert" className="fade-up mb-2 flex items-center gap-2 rounded-lg border px-2.5 py-1.5 text-[10.5px]"
             style={{ borderColor: 'var(--err)', color: 'var(--err)' }}>
          <span className="min-w-0 flex-1 leading-relaxed">{copilot.error}</span>
          {/* 失败多半跟需求本身无关（模型抽风、协议跑偏、网断了），
              不该逼用户把需求再敲一遍 */}
          {copilot.lastInstruction && (
            <button className="btn btn-sm shrink-0" onClick={retryCopilot}>重试</button>
          )}
        </div>
      )}

      <PromptBox
        inputRef={ref}
        size="panel"
        rows={hero ? 3 : 2}
        value={text}
        onChange={setText}
        onSubmit={() => send()}
        busy={copilot.active}
        onStop={stopCopilot}
        stopLabel="停止生成"
        label="告诉助手要画什么流程"
        placeholder={nodes.length
          ? '告诉它这张图要改成什么样…'
          : '描述你想要的流程，它会直接画到左边'}
        onFocusChange={onFocusChange}
        leading={
          <>
            <button
              className={clsx('rounded-md p-1.5 transition-colors hover:bg-hover',
                opts ? 'text-[var(--accent)]' : 'text-faint')}
              title="模型和生成方式"
              aria-label="模型和生成方式"
              aria-expanded={opts}
              onClick={() => setOpts((v) => !v)}
            >
              <Settings2 size={13} />
            </button>
            {!!nodes.length && (
              <button
                className={clsx('rounded-md px-1.5 py-1 text-[10px] transition-colors hover:bg-hover',
                  useBase ? 'text-dim' : 'text-faint')}
                title={useBase ? '在当前这张图上改' : '不看现有的图，重新生成一张'}
                onClick={() => setUseBase((v) => !v)}
              >
                {useBase ? '改现有的图' : '重新生成'}
              </button>
            )}
          </>
        }
      />

      {opts && (
        <div className="fade-up mt-2 space-y-2 rounded-lg border bg-panel p-2.5">
          <div>
            <label className="label">Copilot 用哪个模型</label>
            <select className="field" value={model} onChange={(e) => pickModel(e.target.value)}>
              <option value="">跟随默认{effective ? `（当前：${effective}）` : ''}</option>
              {groups.map((g) => (
                <optgroup key={g} label={g}>
                  {options.filter((o) => o.group === g).map((o) => (
                    <option key={g + o.value} value={o.value}>{o.label}</option>
                  ))}
                </optgroup>
              ))}
            </select>
            <div className="mt-1 text-[10px] leading-snug text-faint">
              只影响 Copilot 自己，不改节点上的模型。它要按协议逐行输出操作，
              指令遵循弱的模型生成不出东西。
            </div>
          </div>
        </div>
      )}

      {copilot.active && (
        <div className="fade-up mt-2 flex items-center gap-1.5 text-[10px] text-faint">
          <Spinner size={10} />
          <span className="min-w-0 flex-1 truncate">
            {copilot.lastOp || PHASE_TEXT[copilot.phase] || '正在起草…'}
          </span>
          {copilot.model && <span className="truncate">{copilot.model}</span>}
          {copilot.elapsedMs > 0 && (
            <span className="mono">{Math.round(copilot.elapsedMs / 1000)}s</span>
          )}
        </div>
      )}

      {!!examples.length && (
        <div className="mt-3 space-y-1.5">
          {examples.map((ex, i) => (
            <button
              key={ex}
              className="rise-in w-full rounded-lg border bg-panel px-3 py-2 text-left text-[11.5px] leading-relaxed text-dim transition-colors hover:border-[var(--accent)] hover:text-fg"
              style={{ '--i': i + 1 } as any}
              onClick={() => send(ex)}
            >
              {ex}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * 输入坞：画布助手栏和问数据页共用的那一块。
 *
 * 整块是一个框，输入区和操作条在里面——比"输入框 + 旁边三个按钮"更像一个可以
 * 对它说话的地方。两个页面各写一份的话，焦点光晕、快捷键提示、停止键的颜色
 * 迟早各走各的。
 *
 * size：panel 是 360px 助手栏里的紧凑版；dock 是问数据页底部；hero 是问数据页
 * 空会话时居中的主输入。
 */
export function PromptBox({
  value, onChange, onSubmit, onStop, busy = false, blocked, placeholder, label,
  rows = 2, size = 'dock', inputRef, leading, onFocusChange, stopLabel = '停止', autoFocus,
}: {
  value: string
  onChange: (value: string) => void
  onSubmit: () => void
  /** 给了它，busy 期间发送键换成停止键 */
  onStop?: () => void
  busy?: boolean
  /** 暂时不能发的原因（历史没取回来之类）。输入照样能打，草稿不丢 */
  blocked?: string | null
  placeholder: string
  /** 读屏念的名字：占位符一有内容就不见了，不能拿它当标签 */
  label: string
  rows?: number
  size?: 'panel' | 'dock' | 'hero'
  inputRef?: RefObject<HTMLTextAreaElement | null>
  /** 操作条左侧的附加按钮 */
  leading?: ReactNode
  onFocusChange?: (focused: boolean) => void
  stopLabel?: string
  autoFocus?: boolean
}) {
  const [focused, setFocused] = useState(false)
  const ready = !!value.trim() && !blocked
  const big = size === 'hero'
  const btn = big ? 'h-8 w-8' : 'h-7 w-7'
  return (
    <div
      className={clsx(
        'rounded-xl border bg-bg transition-[box-shadow,border-color] duration-150',
        big && 'shadow-elev-2',
        focused && 'dock-focus',
      )}
    >
      <textarea
        ref={inputRef}
        aria-label={label}
        className={clsx(
          'w-full resize-none bg-transparent leading-relaxed outline-none placeholder:text-faint',
          size === 'panel' ? 'px-3 pt-2.5 text-[12.5px]' : big ? 'px-4 pt-3.5 text-base' : 'px-3 pt-2.5 text-sm',
        )}
        rows={rows}
        value={value}
        placeholder={placeholder}
        autoFocus={autoFocus}
        onChange={(e) => onChange(e.target.value)}
        onFocus={() => { setFocused(true); onFocusChange?.(true) }}
        onBlur={() => { setFocused(false); onFocusChange?.(false) }}
        onKeyDown={(e) => {
          // 输入法组字期间的回车是"选词"，不是"发送"。不判 isComposing 的话
          // 中文用户每打一个词就发出去一次
          if (e.key === 'Enter' && !e.shiftKey && !isComposing(e)) {
            e.preventDefault()
            if (!busy && ready) onSubmit()
          }
        }}
      />

      <div className={clsx('flex items-center gap-1', big ? 'px-3 pb-3' : 'px-2 pb-2')}>
        {leading}
        <span className="flex-1" />
        <span className={clsx('min-w-0 truncate pr-1 text-faint', size === 'panel' ? 'text-[9.5px]' : 'text-2xs')}
              role={blocked ? 'status' : undefined}>
          {blocked || (value.trim() ? '⏎ 发送 · ⇧⏎ 换行' : '')}
        </span>
        {/* 两个键各带 key：同一位置的同类元素 React 会复用，停止键的红底会顺着
            transition 慢慢褪成发送键，中间那几帧像是一个粉色的发送键 */}
        {busy && onStop ? (
          <button
            key="stop"
            className={clsx('flex shrink-0 items-center justify-center rounded-lg transition-colors', btn)}
            style={{ background: 'var(--err-solid)', color: 'var(--on-err)' }}
            title={stopLabel}
            aria-label={stopLabel}
            onClick={onStop}
          >
            <Square size={11} fill="currentColor" />
          </button>
        ) : (
          <button
            key="send"
            className={clsx('flex shrink-0 items-center justify-center rounded-lg transition-[background-color,color,opacity] duration-150 disabled:opacity-30', btn)}
            style={{
              background: ready ? 'var(--accent-solid)' : 'var(--bg-hover)',
              color: ready ? 'var(--on-accent)' : 'var(--text-faint)',
            }}
            disabled={!ready || busy}
            title={blocked || '发送（⏎）'}
            aria-label="发送"
            onClick={onSubmit}
          >
            <ArrowUp size={big ? 16 : 14} />
          </button>
        )}
      </div>
    </div>
  )
}

/**
 * hero 态的抬头。
 *
 * 不只是一句标语：它要回答"我能让它干什么"。Copilot 能接到的数据源和工具
 * 数量摆在这里，用户才知道"查销售库"这种话是有意义的——否则只能猜。
 */
function HeroHeader({ sources, toolCount }: { sources: any[]; toolCount: number }) {
  return (
    <div className="mb-4 text-center">
      <div className="breathe mx-auto mb-2.5 flex h-10 w-10 items-center justify-center rounded-2xl"
           style={{ background: 'color-mix(in srgb, var(--accent) 14%, transparent)' }}>
        <Sparkles size={18} style={{ color: 'var(--accent)' }} />
      </div>
      <div className="text-[13.5px] font-semibold">想让它做什么？</div>
      <div className="mt-1 text-2xs leading-relaxed text-faint">
        说一句话，它把流程画到左边的画布上
      </div>
      <div className="mt-2 flex items-center justify-center gap-3 text-[10px] text-faint">
        <span className="flex items-center gap-1">
          <Database size={10} />
          {sources.length ? `${sources.length} 个数据源` : '未接数据源'}
        </span>
        <span className="flex items-center gap-1"><Wrench size={10} />{toolCount} 个工具</span>
      </div>
    </div>
  )
}

/**
 * 例句。
 *
 * 用完整句子而不是截断成 16 个字的 chip——例句的作用是示范"说到什么颗粒度
 * 才够"，截断之后这个作用就没了，只剩装饰。
 *
 * 有数据源就用真实的库名造句：用户一眼看到的是自己的数据，而不是别人的示例。
 */
function exampleFor(nodeCount: number, sources: any[]): string[] {
  if (nodeCount) {
    return [
      '在最后加一步人工审核，通过了才输出',
      '中间那步改成 agent，让它自己决定要不要查数据库',
      '出错的时候重试两次，还不行就走另一条分支',
    ]
  }
  const name = sources[0]?.name
  return [
    name
      ? `从 ${name} 里取上季度的数据，算出同比，写一段中文分析`
      : '读取用户的问题，先查知识库，查到就基于资料回答并标注出处，查不到就联网搜索',
    '把一段长文本拆成要点，逐条用模型打分，低分的让模型重写一次，最后汇总成表格',
    '写代码分析数据，在沙箱里跑，出错就把报错喂回去让模型修，最多修三次',
  ]
}

// 从提交到第一个节点落地中间有 5~30 秒。阶段会变本身就是"它还活着"的信号，
// 恒定的"正在起草…"让人分不清是在想还是已经卡死
export const PHASE_TEXT: Record<string, string> = {
  connecting: '正在连接模型…',
  planning: '正在理解需求、规划结构…',
  building: '正在放置节点…',
  wiring: '正在连接数据流…',
  finalizing: '正在排版和校验…',
  repairing: '自查发现问题，正在修正…',
}
