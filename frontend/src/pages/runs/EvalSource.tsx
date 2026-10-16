import type { MouseEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Gauge } from 'lucide-react'
import { EVAL_TEXT } from '../../lib/termsEval'
import type { Run } from '../../types'
import type { EvalRunOrigin } from '../../typesEval'

const T = EVAL_TEXT.runSource

/**
 * 记录页里评测发起的运行（来源 eval）：作答、算期望、作答后复算期望、保存题目时的试算。能跳回对应的那道题——有评测编号的
 * 去那次评测的逐题结果并定位到那道题，没有的（保存题目时的试算）去题集并定位到那道题。地址只从 run.origin 里取（服务端记的
 * 来源，封存范围内），不猜。
 */
export function evalHref(run: Pick<Run, 'origin_kind' | 'origin'>): string | null {
  if (run.origin_kind !== 'eval') return null
  const o = (run.origin ?? {}) as EvalRunOrigin
  const q = o.case_id ? `?case=${encodeURIComponent(o.case_id)}` : ''
  if (o.eval_run_id) return `/evals/runs/${encodeURIComponent(o.eval_run_id)}${q}`
  if (o.suite_id) return `/evals/suites/${encodeURIComponent(o.suite_id)}${q}`
  return null
}

/** 这次运行在评测里做的是哪一步：作答、算期望、作答后复算期望（保存题目时的试算另说） */
function roleText(o: EvalRunOrigin): string {
  const role = o.role ? T.role[o.role] ?? o.role : T.caseUnknown
  return o.trial ? `${T.trial} · ${role}` : role
}

/**
 * 运行名旁的「评测」标记（同「定时」标记）：整行是打开运行的按钮，这里拦住冒泡，只去评测里的那道题。按钮里放不了可聚焦的
 * 链接，键盘的入口在运行详情页头（RunEvalSource）
 */
export function EvalTag({ run }: { run: Pick<Run, 'origin_kind' | 'origin'> }) {
  const navigate = useNavigate()
  const href = evalHref(run)
  if (!href) return null
  const go = (e: MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    navigate(href)
  }
  return (
    <span className="chip shrink-0 cursor-pointer hover:border-[var(--border-strong)] hover:text-fg" title={T.tagTitle}
          data-run-eval={href} onClick={go}>
      <Gauge size={10} aria-hidden /> <span aria-hidden>{roleText((run.origin ?? {}) as EvalRunOrigin)}</span>
      <span className="sr-only">{T.tagSr}</span>
    </span>
  )
}

/** 运行详情页头里「来自评测」那一段：真的链接（Tab 到得了），去评测里对应的那道题 */
export function RunEvalSource({ run }: { run: Pick<Run, 'origin_kind' | 'origin'> }) {
  const href = evalHref(run)
  if (!href) return null
  return (
    <Link to={href} title={T.linkTitle} data-run-eval-link={href}
          className="inline-flex min-w-[7em] max-w-full items-center gap-1 rounded-sm text-dim outline-none hover:text-fg hover:underline focus-visible:ring-2 focus-visible:ring-[var(--accent)]">
      <Gauge size={10} aria-hidden className="shrink-0" />
      <span className="min-w-0 truncate">{T.link(roleText((run.origin ?? {}) as EvalRunOrigin))}</span>
    </Link>
  )
}
