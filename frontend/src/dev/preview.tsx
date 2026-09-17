import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Database, MessageSquare } from 'lucide-react'
import { AssistantStream, StreamEmpty, type StreamTurn } from '../run/AssistantStream'
import { decodeRun } from '../run/decode'
import { ToastHost } from '../components/ui'
import fixtures from '../run/__tests__/fixtures.json'
import '../index.css'

/**
 * AssistantStream 的离线预览。
 *
 * 只有 dev server 会加载（vite build 的 input 只有 index.html），所以它不进
 * 生产包。存在的理由：要改这个组件的排版，不该每次都真跑一次图——真跑一次
 * 意味着要连数据库、等模型、还得凑巧撞上人工介入分支才能看到审批卡长什么样。
 * 这里用的是真实导出的事件，走的是同一个 decodeRun，只有事件来源是静态的。
 *
 * 打开 http://localhost:5273/preview.html
 */

type Bucket = keyof typeof fixtures

const CASES: { key: Bucket; question: string; phase: StreamTurn['phase']; status: string }[] = [
  { key: 'db', question: '这个库里有多少数据？挑最大的表说说', phase: 'done', status: '完成' },
  { key: 'think', question: '分析上季度华东区销量下滑的原因', phase: 'running', status: '正在执行…' },
  { key: 'human', question: '把这批客户的信用额度调高 10%', phase: 'waiting', status: '等待你的确认' },
  { key: 'issue', question: '出一份本月经营分析', phase: 'done', status: '完成' },
  { key: 'failed', question: '查一下不存在的那张表', phase: 'error', status: '' },
  { key: 'loop_approve', question: '写条上线公告，我过一眼再发', phase: 'done', status: '完成' },
]

function buildTurns(): StreamTurn[] {
  return CASES.map((c) => {
    const events = (fixtures as any)[c.key] as any[]
    const steps = decodeRun(events)
    const finished = events.find((e) => e.type === 'run.finished')
    const failed = events.find((e) => e.type === 'run.failed')
    return {
      id: c.key,
      question: c.question,
      phase: c.phase,
      status: c.status,
      steps,
      thinking: c.phase === 'running'
        ? '先看一下 ANALYTICS 里有哪些和销量相关的视图，再按区域和月份聚合……'
        : undefined,
      output: finished?.data?.output ?? null,
      error: failed ? String(failed.data?.error ?? '运行失败') : undefined,
      runId: events[0]?.run_id,
    }
  })
}

function Preview() {
  // ?case=db 只看一个用例，?theme=light 直接出浅色——都是为了截图可复现，
  // 不用靠点按钮
  const params = new URLSearchParams(location.search)
  const only = params.get('case')
  if (params.get('theme') === 'light') document.documentElement.dataset.theme = 'light'
  const [all] = useState(buildTurns)
  const turns = only ? all.filter((t) => t.id === only) : all
  const [dense, setDense] = useState(false)
  const [emptyState, setEmptyState] = useState(false)

  const empty = (
    <StreamEmpty
      icon={<MessageSquare size={22} />}
      title="问点什么"
      hint="它会自己接数据源、写查询、跑完给结论——你不用碰画布"
    />
  )

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-3 border-b px-4 py-2 text-[12px]">
        <Database size={14} style={{ color: 'var(--accent)' }} />
        <span className="font-semibold">AssistantStream 预览</span>
        <span className="text-faint">真实事件 · 离线渲染</span>
        <span className="flex-1" />
        <label className="flex items-center gap-1.5 text-[11.5px] text-dim">
          <input type="checkbox" checked={dense} onChange={(e) => setDense(e.target.checked)} />
          dense（360px 窄栏）
        </label>
        <label className="flex items-center gap-1.5 text-[11.5px] text-dim">
          <input type="checkbox" checked={emptyState}
                 onChange={(e) => setEmptyState(e.target.checked)} />
          空态
        </label>
        <button className="btn btn-sm btn-ghost"
                onClick={() => {
                  const el = document.documentElement
                  el.dataset.theme = el.dataset.theme === 'light' ? '' : 'light'
                }}>
          切主题
        </button>
      </header>

      <div className="flex min-h-0 flex-1">
        {/* 宽屏：问数据页的形态 */}
        <div className="min-w-0 flex-1 border-r">
          <AssistantStream turns={emptyState ? [] : turns} empty={empty} />
        </div>
        {/* 窄栏：画布右侧助手栏的形态。固定 360px，和真实右栏一致 */}
        <div className="w-[360px] shrink-0 bg-panel">
          <AssistantStream turns={emptyState ? [] : turns} dense empty={empty} />
        </div>
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <ToastHost>
    <Preview />
  </ToastHost>,
)
