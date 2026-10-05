import { useEffect, useState } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { GitCompareArrows } from 'lucide-react'
import { EmptyState, PageHeader, Skeleton, Spinner } from '../../components/ui'
import { RUN_COMPARE_TEXT } from '../../lib/terms'
import type { RunComparison } from '../../types'
import { useCandidates, useComparison, useDefaultBase, useRunRecord } from './compare/data'
import { comparePath, type CompareVia } from './compare/entry'
import { Metrics } from './compare/Metrics'
import { compareErrorKind } from './compare/model'
import { CompareError } from './compare/parts'
import { Queries } from './compare/Queries'
import { RunPair } from './compare/RunPair'
import { Summary } from './compare/Summary'
import { NotesSection, TemplateSection } from './compare/Template'

/**
 * 运行对比（/runs/compare?target=<本次>&base=<基准>）：两次运行的指标变化拆成数据、口径、模板与 SQL 三类讲清楚，
 * 能看维度归因和查询差异（GET /api/runs/{本次}/compare?base=）。
 *
 * 地址就是状态：本次和基准都在查询串里，换基准、互换都是改地址（replace，不往历史里堆），对比跟着重新请求，慢的
 * 旧答复回来也不认。只带本次时先问默认的基准（compare/base），找到了补进地址并写明是怎么选的；找不到照实说，
 * 下拉照样能手动挑。从运行详情「与上一次对比」进来时，菜单已经问过默认基准，来历跟着跳转带过来，这里不再问。
 *
 * 挂在 /runs 下面，导航照旧点亮「记录」；路由表里排在 /runs/:runId 之前，「compare」不会被当成运行 id。
 */
export function ComparePage() {
  const navigate = useNavigate()
  const location = useLocation()
  const [params] = useSearchParams()
  const target = params.get('target') || null
  const base = params.get('base') || null
  const via = location.state as CompareVia | null
  const finder = useDefaultBase(target && !base ? target : null)
  // 默认基准的来历：菜单带过来的，或者这里自己找到的；只对那一个基准有效，换了就不写
  const reason = via && via.base === base ? via.reason : null

  const found = finder.state === 'ok' ? finder.data : undefined
  useEffect(() => {
    if (!target || base || !found) return
    const next: CompareVia = { base: found.base_run_id, reason: found.reason }
    navigate(comparePath(target, found.base_run_id), { replace: true, state: next })
  }, [target, base, found, navigate])

  const targetRun = useRunRecord(target)
  const baseRun = useRunRecord(base)
  const workflowId = targetRun.data?.workflow_id
  const candidates = useCandidates(workflowId)
  const cmp = useComparison(target, base)

  const go = (t: string, b: string) => navigate(comparePath(t, b), { replace: true })

  let state: 'no-target' | 'finding' | 'loading' | 'error' | 'ok'
  if (!target) state = 'no-target'
  else if (!base) state = finder.state === 'error' ? 'error' : 'finding'
  else state = cmp.state === 'ok' ? 'ok' : cmp.state === 'error' ? 'error' : 'loading'

  return (
    <div className="flex h-full flex-col">
      <PageHeader icon={<GitCompareArrows size={13} />} title={RUN_COMPARE_TEXT.title} subtitle={RUN_COMPARE_TEXT.hint} />
      <div className="min-h-0 flex-1 overflow-y-auto" data-compare-page="" data-compare-state={state}>
        {!target ? (
          <EmptyState icon={<GitCompareArrows size={22} />} title={RUN_COMPARE_TEXT.noTarget} body={RUN_COMPARE_TEXT.noTargetHint}
                      offline={false} action={<Link className="btn btn-sm" to="/runs">{RUN_COMPARE_TEXT.toRuns}</Link>} />
        ) : (
          <div className="mx-auto flex w-full min-w-0 max-w-6xl flex-col gap-3 px-3 py-3 sm:px-4 sm:py-4">
            <RunPair
              target={target} base={base} targetRun={targetRun} baseRun={baseRun} candidates={candidates}
              unsaved={targetRun.state === 'ok' && !workflowId} reason={reason}
              onPick={(b) => go(target, b)} onSwap={() => { if (base) go(base, target) }}
            />
            {state === 'finding' && <Pending text={RUN_COMPARE_TEXT.finding} />}
            {state === 'loading' && <Pending text={RUN_COMPARE_TEXT.loading} />}
            {state === 'error' && (
              <CompareError
                kind={compareErrorKind(base ? cmp.error : finder.error, !base)}
                error={base ? cmp.error : finder.error}
                onRetry={base ? cmp.reload : finder.reload}
                target={target} base={base}
              />
            )}
            {state === 'ok' && cmp.data && <Comparison key={`${target}|${base}`} data={cmp.data} />}
          </div>
        )}
      </div>
    </div>
  )
}

/** 找基准、对比的请求在路上：一句在做什么，下面是占位的骨架 */
function Pending({ text }: { text: string }) {
  return (
    <div className="flex flex-col gap-3" data-compare-loading="" aria-busy="true">
      <div role="status" className="flex items-center gap-2 text-xs text-faint"><Spinner size={12} /> {text}</div>
      <Skeleton rows={1} cols={4} height={44} />
      <Skeleton rows={6} height={20} />
    </div>
  )
}

/** 对比结果。按「本次|基准」换 key：换了一对运行，筛选、展开都从头来 */
function Comparison({ data }: { data: RunComparison }) {
  const [filter, setFilter] = useState<string | null>(null)
  return (
    <>
      <Summary summary={data.summary} filter={filter} onFilter={setFilter} />
      <NotesSection notes={data.notes ?? []} />
      <Metrics metrics={data.metrics ?? []} filter={filter} onClearFilter={() => setFilter(null)} />
      <Queries queries={data.queries ?? []} />
      <TemplateSection template={data.template} />
    </>
  )
}
