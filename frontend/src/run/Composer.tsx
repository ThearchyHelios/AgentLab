import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode, type RefObject } from 'react'
import { AlertCircle, ArrowUp, CornerDownRight, Database, Settings2, Sparkles, Square, Wrench } from 'lucide-react'
import clsx from 'clsx'
import { api, type CopilotModel } from '../api/client'
import { useCatalog, useDatasources, modelOptions } from '../store/catalog'
import { EDIT_LOCK_TEXT, useEditLock, useStudio } from '../store/studio'
import { isComposing, Spinner, TechDetails, useRadioGroup } from '../components/ui'
import { formatShortcut } from '../lib/keys'
import { ASSISTANT_MODEL_TEXT as MT } from '../lib/terms'
import type { DataSource } from '../types'
import { COPILOT_PHASE_TEXT } from './decode'

/** 「助手使用的模型」下拉里代表「存着的设定」的那一项的值（模型 id 不会是它） */
const SAVED = '\u0000saved'

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
  // 数据源和检查器挑工具读的是 catalog 里同一份（数据页改了会 reload）。停用的助手看不见，不算
  const { list: allSources } = useDatasources()
  const sources = useMemo(() => allSources.filter((d) => d.enabled !== false), [allSources])
  // 正式运行在跑已发布的版本：这时发出去 store 也会拦下，输入框先说清为什么发不了
  const lock = useEditLock()
  const [text, setText] = useState('')
  const [opts, setOpts] = useState(false)
  // 存着的助手模型设定（GET /api/copilot/model）：接入、模型、实际会用的模型、用不了时的原因
  const [saved, setSaved] = useState<CopilotModel | null>(null)
  // 重新选择时保存失败：下拉已退回选之前的样子，下面说明没改成
  const [saveFailed, setSaveFailed] = useState(false)
  // 只认最后一次选择的回包：连着选两次时，先发的那次晚回来也不能盖掉后一次
  const picks = useRef(0)
  const [useBase, setUseBase] = useState(true)
  const ref = useRef<HTMLTextAreaElement>(null)

  // 挂上时取一次；每次展开「助手使用的模型」再取一次：接入可能刚在设置里启用、停用或改了名。取的途中人已经重新选过
  // 的话不用这一份（它是选之前的样子）。重新取回来的就是服务端现在存着的，上一次「保存失败」的说明不再留着
  useEffect(() => {
    if (!opts && saved) return
    let alive = true
    setSaveFailed(false)
    const seq = picks.current
    void api.copilot.getModel().then((m) => { if (alive && seq === picks.current) setSaved(m) }).catch(() => undefined)
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opts])

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

  const options = modelOptions(providers)
  const groups = [...new Set(options.map((o) => o.group))]
  // 存着的设定下拉里列不出来：点名了接入（下拉只选模型，选了会清掉接入），或者模型不在可选列表中。照实列成一项并选中
  // 它——否则下拉停在「跟随默认」，看着一切正常，接入失效了也看不出来，再选「跟随默认」不算改动、清不掉存着的接入
  const custom = !!saved && (!!saved.provider || (!!saved.model && !options.some((o) => o.value === saved.model)))
  const model = custom ? SAVED : saved?.model ?? ''
  // 下拉下面写的原因：服务端给的这一格专用的说法（去向写「上方」），老服务端只给整句
  const broken = saved?.panel_error || saved?.error || null

  const pickModel = (value: string) => {
    if (value === SAVED) return
    const before = saved
    const seq = ++picks.current
    setSaveFailed(false)
    // 先按选的显示；存的是 provider 置空、只写模型（跟随默认时两项都空），回来的是服务端认下的样子。跟随默认时实际用
    // 哪个模型由服务端说了算，回来之前不写「当前」
    setSaved({ configured: !!value, provider: null, model: value || null, effective_provider: null,
      effective_model: value || null, error: null, panel_error: null })
    void api.copilot.setModel({ model: value || null }).then((m) => { if (seq === picks.current) setSaved(m) }).catch(() => {
      if (seq !== picks.current) return
      // 没存上：服务端存着的还是原来那一组，退回选之前的样子（原因、齿轮上的红点照旧），并说明保存失败
      setSaved(before)
      setSaveFailed(true)
    })
  }

  const send = (override?: string) => {
    const q = (override ?? text).trim()
    if (!q || copilot.active) return
    // 被正式运行拦下时（store 已经弹过提示）什么都没开始：这句话留在框里，结束后直接再发。
    // 存着的设定列不出来时不带模型：服务端按存着的整组取（带了模型就会绕过存着的接入，和这里说的对不上）
    if (!runCopilot(q, useBase && nodes.length > 0, model && model !== SAVED ? model : undefined)) return
    setText('')
    // 从头生成是一次性的：新图一落到画布上，下一句多半是在它上面改。停在「从头生成」
    // 的话，下一句又把刚生成的图整张换掉（撤销找得回，但人得先发现）
    setUseBase(true)
  }
  const mode = useRadioGroup(MODES, useBase ? 'base' : 'fresh', (m) => setUseBase(m === 'base'))

  const examples = hero ? exampleFor(nodes.length, sources) : []

  return (
    <div className={clsx('shrink-0', hero ? 'px-4' : 'border-t px-2.5 pb-2.5 pt-2')}>
      {hero && <HeroHeader sources={sources} toolCount={tools?.length ?? 0} />}

      {/* 说过话之后，出错的那一轮在上面的流里自己有报错块（怎么办、原文、「用同一句话
          重试」都在），这里再摆一条就是同一句话说两遍。只有还没有轮次可挂的时候才由它说。
          样子和流里的报错块一致：发生了什么、怎么办分开写，原文折进技术细节 */}
      {copilot.error && hero && (
        <div role="alert" data-copilot-error=""
             className="fade-up mb-2 flex items-start gap-2 rounded-lg border px-2.5 py-1.5 text-2xs"
             style={{ borderColor: 'color-mix(in srgb, var(--st-failed) 35%, var(--border))',
                      background: 'color-mix(in srgb, var(--st-failed) 6%, transparent)' }}>
          <AlertCircle size={12} className="mt-px shrink-0" style={{ color: 'var(--st-failed)' }} aria-hidden />
          <div className="min-w-0 flex-1 leading-relaxed">
            <div className="font-medium text-fg [overflow-wrap:anywhere]">{copilot.error}</div>
            {copilot.errorHint && (
              <div className="mt-0.5 flex items-start gap-1 text-dim">
                <CornerDownRight size={10} className="mt-[3px] shrink-0" aria-hidden />
                <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">{copilot.errorHint}</span>
              </div>
            )}
            {copilot.errorDetail && <TechDetails raw={copilot.errorDetail} className="mt-1" />}
          </div>
          {/* 失败多半跟需求本身无关（模型抽风、协议跑偏、网断了），
              不该逼用户把需求再敲一遍 */}
          {copilot.lastInstruction && (
            <button className="btn btn-xs shrink-0" onClick={retryCopilot}>重试</button>
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
        blocked={lock === 'formal' ? EDIT_LOCK_TEXT.formal : null}
        busy={copilot.active}
        onStop={stopCopilot}
        stopLabel="停止生成"
        label="描述要生成或修改的工作流"
        placeholder={nodes.length
          ? '描述要如何修改这个工作流…'
          : '描述你需要的工作流，助手会在左侧画布生成'}
        onFocusChange={onFocusChange}
        leading={
          <>
            <button
              className={clsx('relative rounded-md p-1.5 transition-colors hover:bg-hover',
                opts ? 'text-[var(--accent)]' : 'text-faint')}
              title={broken ? MT.broken : MT.label}
              aria-label={MT.label}
              aria-expanded={opts}
              data-copilot-model-broken={broken ? '' : undefined}
              onClick={() => setOpts((v) => !v)}
            >
              <Settings2 size={13} />
              {/* 存着的设定用不了：收着的时候也要看得出来，不然要等发出一句话才报错 */}
              {broken && <span className="absolute right-0.5 top-0.5 h-1.5 w-1.5 rounded-full bg-[var(--err)]" aria-hidden />}
            </button>
            {/* 两种方式并排摆出来，选中的那个就是这一句会怎么做。以前是一个 10px 的字，
                点一下就翻成另一个词，看不出它是开关，更看不出另一头是什么。从头生成
                不再二次确认：整轮是一步撤销，失败、停止、只回一句话都会把画布放回原样 */}
            {!!nodes.length && (
              <span role="radiogroup" aria-label="生成方式" className="inline-flex shrink-0 whitespace-nowrap rounded-md border p-px">
                {MODES.map((m) => (
                  <button key={m} type="button" {...mode(m)}
                          className={clsx('rounded px-1.5 text-2xs leading-5 transition-colors',
                            (m === 'base') === useBase ? 'bg-accent-soft text-fg' : 'text-faint hover:text-dim')}
                          title={m === 'base' ? '将画布上现有的工作流交给助手，在此基础上修改'
                            : `忽略画布上的现有工作流，重新生成（仍参考之前几轮对话）；原工作流可通过撤销（${formatShortcut('Mod+Z')}）找回`}
                          onClick={() => setUseBase(m === 'base')}>
                    {m === 'base' ? '在现有工作流上改' : '从头生成'}
                  </button>
                ))}
              </span>
            )}
          </>
        }
      />

      {opts && (
        <div className="fade-up mt-2 space-y-2 rounded-lg border bg-panel p-2.5">
          <div>
            <label className="label" htmlFor="copilot-model">{MT.label}</label>
            <select id="copilot-model" className="field" value={model} onChange={(e) => pickModel(e.target.value)}
                    aria-invalid={broken ? true : undefined} aria-describedby={broken ? 'copilot-model-error' : undefined}>
              {/* 「当前」只在选着跟随默认、用得了的时候写：选了别的模型时 effective_model 是那个模型，不是跟随默认会用的；
                  存着的设定列成单独一项时，这一项说的是选它以后会用哪个 */}
              <option value="">{MT.follow}{model === '' && saved?.effective_model ? MT.current(saved.effective_model) : ''}</option>
              {custom && <option value={SAVED}>{MT.saved(saved!.provider, saved!.model)}</option>}
              {groups.map((g) => (
                <optgroup key={g} label={g}>
                  {options.filter((o) => o.group === g).map((o) => (
                    <option key={g + o.value} value={o.value}>{o.label}</option>
                  ))}
                </optgroup>
              ))}
            </select>
            {saveFailed && (
              <div role="alert" data-copilot-model-save-failed=""
                   className="mt-1 text-2xs leading-relaxed text-[var(--err)]">{MT.saveFailed}</div>
            )}
            {broken && (
              <div id="copilot-model-error" role="alert" data-copilot-model-error=""
                   className="mt-1 text-2xs leading-relaxed text-[var(--err)] [overflow-wrap:anywhere]">{broken}</div>
            )}
            <div className="mt-1 text-2xs leading-snug text-faint">{MT.note}</div>
          </div>
        </div>
      )}

      {copilot.active && (
        <div className="fade-up mt-2 flex items-center gap-1.5 text-2xs text-faint">
          <Spinner size={10} />
          <span className="min-w-0 flex-1 truncate">
            {copilotProgress(copilot.lastOp, copilot.phase)}
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
              // 锁着时不能用透明度表示：rise-in 的动画停在 opacity:1，会把它盖掉
              className="rise-in w-full rounded-lg border bg-panel px-3 py-2 text-left text-[11.5px] leading-relaxed text-dim transition-colors hover:border-[var(--accent)] hover:text-fg disabled:pointer-events-none disabled:text-faint"
              style={{ '--i': i + 1 } as CSSProperties}
              disabled={lock === 'formal'}
              title={lock === 'formal' ? EDIT_LOCK_TEXT.formal : undefined}
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

      {blocked && (
        // 发不出去的原因单占一行、可以折行：挤在按键旁边截成「正式运行进行中，画…」就读不到为什么、
        // 什么时候能改；禁用的发送键上的 title 很多浏览器不显示
        <div role="status" data-prompt-blocked="" title={blocked}
             className={clsx('text-2xs leading-snug text-dim [overflow-wrap:anywhere]', big ? 'px-4 pb-1' : 'px-3 pb-1')}>
          {blocked}
        </div>
      )}
      <div className={clsx('flex items-center gap-1', big ? 'px-3 pb-3' : 'px-2 pb-2')}>
        {leading}
        <span className="flex-1" />
        {!blocked && (
          <span className={clsx('min-w-0 truncate pr-1 text-faint', size === 'panel' ? 'text-[9.5px]' : 'text-2xs')}>
            {value.trim() ? '⏎ 发送 · ⇧⏎ 换行' : ''}
          </span>
        )}
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
function HeroHeader({ sources, toolCount }: { sources: DataSource[]; toolCount: number }) {
  return (
    <div className="mb-4 text-center">
      <div className="breathe mx-auto mb-2.5 flex h-10 w-10 items-center justify-center rounded-2xl"
           style={{ background: 'color-mix(in srgb, var(--accent) 14%, transparent)' }}>
        <Sparkles size={18} style={{ color: 'var(--accent)' }} />
      </div>
      <div className="text-[13.5px] font-semibold">需要助手做什么？</div>
      <div className="mt-1 text-2xs leading-relaxed text-faint">
        用一句话描述需求，助手会在左侧画布生成工作流
      </div>
      <div className="mt-2 flex items-center justify-center gap-3 text-2xs text-faint">
        <span className="flex items-center gap-1">
          <Database size={10} />
          {sources.length ? `${sources.length} 个数据源` : '未接入数据源'}
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
function exampleFor(nodeCount: number, sources: DataSource[]): string[] {
  if (nodeCount) {
    return [
      '在最后加一步人工审批，批准后才输出',
      '把中间那一步改成 Agent，由它自行决定是否查询数据库',
      '出错时重试两次，仍然失败则转入另一条分支',
    ]
  }
  const name = sources[0]?.name
  return [
    name
      ? `从 ${name} 中读取上季度的数据，计算同比，并撰写一段中文分析`
      : '读取用户的问题，先检索知识库：检索到则基于资料回答并标注出处，否则联网搜索',
    '把一段长文本拆成要点，逐条用模型打分，低分的让模型重写一次，最后汇总成表格',
    '编写代码分析数据并在沙箱中执行，出错时将报错交给模型修复，最多三次',
  ]
}

const MODES = ['base', 'fresh'] as const

// 从提交到第一个节点落地中间有 5~30 秒。阶段会变本身就是"它还活着"的信号，
// 恒定的"正在起草…"让人分不清是在想还是已经卡死。阶段文案本身在 decode.ts
export function copilotProgress(lastOp: string | undefined, phase: string): string {
  const text = COPILOT_PHASE_TEXT[phase]
  return lastOp || (text ? `${text}…` : '正在起草…')
}
