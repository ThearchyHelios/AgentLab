import { useNavigate, useParams } from 'react-router-dom'
import { Gauge } from 'lucide-react'
import { EmptyState, PageHeader, Tabs } from '../../components/ui'
import { EVAL_TEXT as T } from '../../lib/termsEval'
import { EvaluationPage } from './EvaluationPage'
import { Signals } from './Signals'
import { Snapshots } from './Snapshots'
import { SuiteDetail } from './SuiteDetail'
import { SuiteList } from './SuiteList'

/**
 * 评测（顶级导航「评测」，搭建者以上可见；docs/design/next-round/01 第 18 项、评测-D8）。地址：
 *
 * - /evals 题集列表；/evals/signals 口径命中与重复度；/evals/snapshots 目录快照与影子起草（三个页签）；
 * - /evals/suites/:id 一个题集（题目、评测记录；?add=1&question=… 打开加题并带上问法）；
 * - /evals/runs/:id 一次评测（进度、总览、逐题、和上一次比、A/B；?case=… 定位到那道题）。评测通知、记录页里评测运行的来源
 *   都链到这里。
 *
 * 路由表在 App 里只有 /evals、/evals/:section、/evals/:section/:id 三条，这里认 section。
 */
export function EvalsPage() {
  const { section, id } = useParams()
  if (!section) return <EvalsHome tab="suites" />
  if ((section === 'signals' || section === 'snapshots') && !id) return <EvalsHome tab={section} />
  if (section === 'suites' && id) return <SuiteDetail key={id} id={id} />
  if (section === 'runs' && id) return <EvaluationPage key={id} id={id} />
  return (
    <div className="flex h-full flex-col" data-evals-page="missing">
      <PageHeader icon={<Gauge size={13} />} title={T.title} subtitle={T.hint} />
      <EmptyState icon={<Gauge size={22} />} title={T.notFound} offline={false} />
    </div>
  )
}

type HomeTab = 'suites' | 'signals' | 'snapshots'
const HOME_TABS: HomeTab[] = ['suites', 'signals', 'snapshots']

function EvalsHome({ tab }: { tab: HomeTab }) {
  const navigate = useNavigate()
  return (
    <div className="flex h-full flex-col" data-evals-page={tab}>
      <PageHeader icon={<Gauge size={13} />} title={T.title} subtitle={T.hint} />
      <Tabs tabs={HOME_TABS.map((k) => ({ key: k, label: T.tabs[k] }))} active={tab} label={T.tabsLabel} idPrefix="evals"
            onChange={(k) => navigate(k === 'suites' ? '/evals' : `/evals/${k}`, { replace: true })} />
      <div className="min-h-0 flex-1 overflow-y-auto" role="tabpanel" id={`evals-panel-${tab}`} aria-labelledby={`evals-tab-${tab}`}>
        {tab === 'suites' && <SuiteList />}
        {tab === 'signals' && <Signals />}
        {tab === 'snapshots' && <Snapshots />}
      </div>
    </div>
  )
}
