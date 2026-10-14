import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, Check, Crosshair, RotateCcw, RotateCw, XCircle } from 'lucide-react'
import { api } from '../../../api/client'
import { SectionBar, Spinner, toast } from '../../../components/ui'
import { APP_EDITOR_TEXT as T } from '../../../lib/terms'
import type { AppSpec, LintIssue } from '../../../typesApp'
import { jsonErrorAt, lineOfPath, pathParts, shapeIssues, type ShapeIssue } from './appOps'
import { useEditor } from './context'

/*
 * 入口与表单（A2、§3.2、§3.3）：入口、表单、待办外观三块按 JSON 编辑。
 *
 * 这三块是数据约束（入口的字段映射、表单的必填和取值范围），助手只能改外观（D-h），所以要给人一处能直接改的地方；
 * 组件化的编辑器做不全 agentlab.form/1，JSON 是唯一不丢信息的写法。改完点「应用到草稿」才落到草稿上（和页面树、助手的
 * 修改同一份草稿），格式不对的不让应用；形状按目录的子集先查一遍（shapeIssues），规则以服务端的发布检查为准——
 * 「检查这一节」跑一次发布检查，只列落在这三块上的问题，点一条在 JSON 里定位到那一行；检查一节、发布被拦时点「定位」
 * 也跳到这里（focusPath）
 */

const BLOCKS = ['entries', 'forms', 'task_ui'] as const
type Block = typeof BLOCKS[number]

const TITLE: Record<Block, string> = { entries: T.jsonEntries, forms: T.jsonForms, task_ui: T.jsonTaskUi }
const HINT: Record<Block, string> = { entries: T.jsonEntriesHint, forms: T.jsonFormsHint, task_ui: T.jsonTaskUiHint }

interface Focus { block: Block; keys: string[]; seq: number }

export function FormsSection() {
  const { appId, draft, dirty, save, focusPath, clearFocus } = useEditor()
  const [issues, setIssues] = useState<LintIssue[] | null>(null)
  const [checking, setChecking] = useState(false)
  const [focus, setFocus] = useState<Focus | null>(null)
  const seq = useRef(0)

  // 从检查、发布那边带着 path 过来：拆成哪一块、块里的键，交给那一块去定位
  useEffect(() => {
    if (!focusPath) return
    const parts = pathParts(draft, focusPath)
    const block = parts[0] as Block
    if ((BLOCKS as readonly string[]).includes(block)) setFocus({ block, keys: parts.slice(1), seq: ++seq.current })
    clearFocus()
  }, [focusPath]) // eslint-disable-line react-hooks/exhaustive-deps

  const check = async () => {
    setChecking(true)
    try {
      if (dirty && !(await save())) return
      const res = await api.apps.lint(appId)
      setIssues(res.issues.filter((i) => i.path && (BLOCKS as readonly string[]).includes(pathParts(draft, i.path)[0])))
    } catch (e) {
      toast.error(e)
    } finally {
      setChecking(false)
    }
  }

  return (
    <div className="mx-auto max-w-4xl space-y-6 p-4" data-app-forms="">
      <SectionBar title={T.sections.forms} hint={T.formsHint}>
        <button type="button" className="btn btn-sm" disabled={checking} onClick={() => void check()} data-app-forms-check="">
          {checking ? <Spinner size={11} /> : <RotateCw size={11} aria-hidden />} {T.jsonCheck}
        </button>
      </SectionBar>
      {issues && (
        <p className="text-2xs" style={{ color: issues.length ? 'var(--warn)' : 'var(--ok)' }} data-app-forms-checked={issues.length}>
          {T.jsonChecked(issues.length)}
        </p>
      )}
      {BLOCKS.map((block) => (
        <JsonBlock key={block} block={block}
                   issues={(issues ?? []).filter((i) => pathParts(draft, i.path ?? '')[0] === block)}
                   focus={focus?.block === block ? focus : null}
                   onLocate={(keys) => setFocus({ block, keys, seq: ++seq.current })} />
      ))}
    </div>
  )
}

type Parsed = { ok: true; value: unknown } | { ok: false; message: string; at: { line: number; col: number } | null }

function parse(text: string): Parsed {
  try {
    return { ok: true, value: JSON.parse(text) }
  } catch (e) {
    const message = e instanceof Error ? e.message : String(e)
    return { ok: false, message, at: jsonErrorAt(text, message) }
  }
}

function JsonBlock({ block, issues, focus, onLocate }: {
  block: Block
  /** 发布检查落在这一块上的问题 */
  issues: LintIssue[]
  focus: Focus | null
  onLocate: (keys: string[]) => void
}) {
  const { draft, setDraft } = useEditor()
  const value = draft[block]
  const pretty = useMemo(() => JSON.stringify(value ?? (block === 'entries' ? [] : {}), null, 2), [value, block])
  const [text, setText] = useState(pretty)
  // 上次和草稿对齐时的那份：text 和它不同就是有未应用的修改
  const [base, setBase] = useState(pretty)
  const [located, setLocated] = useState<number | null>(null)
  const ref = useRef<HTMLTextAreaElement>(null)
  const local = text !== base
  // 草稿在别处改了（助手、页面树）：这里没有未应用的修改就跟上，有的话留着人手上的（应用时以人的为准）
  useEffect(() => {
    if (text === base) setText(pretty)
    setBase(pretty)
  }, [pretty]) // eslint-disable-line react-hooks/exhaustive-deps

  const parsed = useMemo(() => parse(text), [text])
  // 入口引用的表单按这一节的表单查：表单一块有未应用的修改时仍按草稿里的，应用之后再对
  const shape: ShapeIssue[] = parsed.ok ? shapeIssues(block, parsed.value, draft, T) : []

  const apply = () => {
    if (!parsed.ok) return
    const next = parsed.value
    setDraft((s: AppSpec) => ({ ...s, [block]: next }))
    const fresh = JSON.stringify(next, null, 2)
    setText(fresh)
    setBase(fresh)
    toast.ok(T.jsonApplied(TITLE[block]))
  }

  /** 选中 keys 指的那一行并滚到可见处。人手上的修改是合法 JSON 时先排好版（内容不变），不合法的按草稿里的找 */
  const locate = useCallback((keys: string[]) => {
    const now = parse(text)
    let shown = text
    let target: unknown = value
    if (now.ok) {
      shown = JSON.stringify(now.value, null, 2)
      target = now.value
      if (shown !== text) setText(shown)
    } else if (local) {
      return
    }
    const line = keys.length ? lineOfPath(target, keys) : 0
    if (line < 0) return
    setLocated(line)
    requestAnimationFrame(() => {
      const ta = ref.current
      if (!ta) return
      const lines = shown.split('\n')
      const start = lines.slice(0, line).reduce((n, l) => n + l.length + 1, 0)
      const end = start + (lines[line]?.length ?? 0)
      ta.focus({ preventScroll: true })
      ta.setSelectionRange(start, end)
      const lh = Number.parseFloat(getComputedStyle(ta).lineHeight) || 16
      ta.scrollTop = Math.max(0, line * lh - ta.clientHeight / 3)
      ta.closest('[data-app-json]')?.scrollIntoView({ block: 'nearest' })
    })
  }, [text, value, local])

  // 外面要定位到这一块（检查、发布那边点「定位」，或者这一节里点问题）
  useEffect(() => {
    if (focus) locate(focus.keys)
  }, [focus?.seq]) // eslint-disable-line react-hooks/exhaustive-deps

  const problems: { message: string; keys: string[]; level: 'error' | 'warning'; code?: string }[] = [
    ...shape.map((x) => ({ ...x, level: 'error' as const })),
    ...issues.map((i) => ({ message: i.message, keys: pathParts(draft, i.path ?? '').slice(1), level: i.level, code: i.code })),
  ]
  const rows = Math.min(24, Math.max(6, text.split('\n').length + 1))

  return (
    <section data-app-json={block} data-app-json-dirty={local || undefined} data-app-json-located={located ?? undefined}>
      <SectionBar title={TITLE[block]} hint={HINT[block]}>
        {local && <span className="text-2xs" style={{ color: 'var(--st-waiting)' }}>{T.jsonDirty}</span>}
        <button type="button" className="btn btn-sm btn-ghost" disabled={!local}
                onClick={() => { setText(pretty); setBase(pretty) }} data-app-json-reset="">
          <RotateCcw size={11} aria-hidden /> {T.jsonReset}
        </button>
        <button type="button" className="btn btn-sm btn-primary" disabled={!local || !parsed.ok} onClick={apply}
                data-app-json-apply="">
          <Check size={11} aria-hidden /> {T.jsonApply}
        </button>
      </SectionBar>
      <textarea ref={ref} className="field mono w-full text-2xs leading-relaxed" rows={rows} spellCheck={false}
                value={text} onChange={(e) => setText(e.target.value)} aria-label={TITLE[block]}
                aria-invalid={!parsed.ok || undefined}
                style={!parsed.ok ? { borderColor: 'var(--err)' } : undefined} />
      {!parsed.ok && (
        <p className="mt-1 text-2xs" style={{ color: 'var(--err)' }} data-app-json-error="" role="alert">
          {T.jsonSyntax(parsed.at ? T.jsonAt(parsed.at.line, parsed.at.col) : parsed.message)}
        </p>
      )}
      {problems.length > 0 && (
        <ul className="mt-2 divide-y rounded-lg border" data-app-json-issues={problems.length}>
          {problems.map((x, i) => (
            <li key={i} className="flex items-start gap-2 px-3 py-1.5 text-xs" data-app-json-issue={x.code ?? 'shape'}>
              {x.level === 'error'
                ? <XCircle size={12} className="mt-0.5 shrink-0" style={{ color: 'var(--err)' }} aria-hidden />
                : <AlertTriangle size={12} className="mt-0.5 shrink-0" style={{ color: 'var(--warn)' }} aria-hidden />}
              <span className="min-w-0 flex-1">{x.message}</span>
              {(x.keys.length > 0 || x.code) && (
                <button type="button" className="btn btn-xs btn-ghost shrink-0" onClick={() => onLocate(x.keys)}
                        data-app-json-locate="">
                  <Crosshair size={10} aria-hidden /> {T.locate}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
