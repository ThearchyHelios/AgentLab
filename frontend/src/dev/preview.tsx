import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { Database, MessageSquare, RotateCw } from 'lucide-react'
import { AssistantStream, StreamEmpty, type StreamTurn } from '../run/AssistantStream'
import { Markdown } from '../run/Markdown'
import { ApprovalCard } from '../run/RunPanel'
import { decodeCopilot, decodeRun } from '../run/decode'
import { ToastHost } from '../components/ui'
import type { RunEvent } from '../types'
import fixtures from '../run/__tests__/fixtures.json'
import mdSamples from '../run/__tests__/markdown-samples.json'
import {
  COPILOT_STUCK, MIXED_OUTPUT, cancelledRun, longLoop, mixedRun, pipelineRun, teamRun,
} from '../run/__tests__/synthetic'
import '../index.css'

/**
 * AssistantStream 的离线预览。
 *
 * 只有 dev server 会加载（vite build 的 input 只有 index.html），所以它不进
 * 生产包。存在的理由：要改这个组件的排版，不该每次都真跑一次图——真跑一次
 * 意味着要连数据库、等模型、还得凑巧撞上人工介入分支才能看到审批卡长什么样。
 * 这里用的是真实导出的事件（fixtures.json）和照后端字段合成的事件
 * （synthetic.ts），走的是同一个 decodeRun，只有事件来源是静态的。
 *
 * 打开 http://localhost:5273/preview.html；scripts/check-stream.mjs 按下面这些
 * 查询参数逐个打开截图、断言。
 */

type Bucket = keyof typeof fixtures

const CASES: { key: Bucket; question: string; phase: StreamTurn['phase']; status: string }[] = [
  { key: 'db', question: '这个库里有多少数据？挑最大的表说说', phase: 'done', status: '已完成' },
  { key: 'think', question: '分析上季度华东区销量下滑的原因', phase: 'running', status: '运行中' },
  { key: 'human', question: '把这批客户的信用额度调高 10%', phase: 'waiting', status: '等待审批' },
  { key: 'issue', question: '出一份本月经营分析', phase: 'done', status: '已完成' },
  { key: 'failed', question: '查一下不存在的那张表', phase: 'error', status: '' },
  { key: 'loop_approve', question: '写条上线公告，我过一眼再发', phase: 'done', status: '已完成' },
  { key: 'supervisor', question: '冬虫夏草是什么', phase: 'done', status: '已完成' },
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

/**
 * 合成事件的 ts 固定在过去某一刻，拿来演"进行中"时秒表会显示几天。
 * 整体平移到"最后一条是 lagMs 之前"，实时计时就是一个合理的数
 */
function rebase(events: RunEvent[], lagMs = 3200): RunEvent[] {
  const last = events[events.length - 1]?.ts ?? 0
  const shift = (Date.now() - lagMs) / 1000 - last
  return events.map((e) => ({ ...e, ts: e.ts + shift }))
}

/**
 * 一段恶意输入。这类测例必须是构造的——真实输出不会自带攻击，但模型输出
 * 是不可信内容，渲染层要么天生免疫，要么就是个 XSS 口子。
 */
const HOSTILE = [
  '正常文本 **加粗**。',
  '',
  '<script>window.__pwned = 1</script>',
  '<img src=x onerror="window.__pwned=1">',
  '[点我](javascript:window.__pwned=1)',
  '[正常链接](https://example.com)',
  '<iframe src="https://evil.example"></iframe>',
  '`<script>alert(1)</script>`',
].join('\n')

/**
 * 一份会超出折叠上限的报告，表格正好横跨切点。
 *
 * 之前折叠是按字符硬切的，切点落进表格中间——前面几行渲染成表格、最后半行
 * 留成原始的 `| 1 | ThearchyHelios | …`。用户看到的是一份被咬掉一口的报告，
 * 而它其实是完整的，只是被折叠了。
 */
function longReport(): string {
  const head = [
    "## 总结", "",
    "- **管理员（平台级/区域级角色）**：共 9 人", "- **商家账户**：共 5 个", "",
    "口径说明：".padEnd(400, "口"), "",
    "## 一、管理员名单", "",
  ].join("\n")
  const rows = Array.from({ length: 40 }, (_, i) =>
    `| ${i + 1} | user_${i} | user${i}@example.com | 昵称${i} | ` +
    `85e11976-6a7b-4167-b561-deec4fd${String(i).padStart(4, "0")} | 商家 ${i} | c${i}@example.com |`)
  return [
    head,
    "| userID | 用户名 | 邮箱 | 昵称 | providerID | 商家名称 | 联系人邮箱 |",
    "| --- | --- | --- | --- | --- | --- | --- |",
    ...rows,
    "", "> 说明：部分账户身兼管理员与商家双重身份。",
  ].join("\n")
}

/** 直接回答的那一轮：没查库，界面上必须说破 */
const NO_QUERY_TURN: StreamTurn = {
  id: "noquery", question: "刚才那 6 个管理员是怎么算的？", phase: "done", status: "已完成",
  steps: [], noQuery: true,
  output: { answer: "第 1 轮走的是 `role.level IN (platform, regional)` 这个口径。要换口径我重新查一遍。" },
}

/** 复核判了「不可信」：说明排在成果上方，且用报警色 */
const REVIEW_BROKEN_TURN: StreamTurn = {
  id: "reviewbroken", question: "商家账户有多少个？", phase: "done", status: "已完成",
  steps: [],
  review: {
    verdict: "annotated", severity: "broken", retry: true,
    note: "这次取数没跑通（表名对不上），下面这个数是模型根据上下文推断的，不能当结论用。",
    answer: null,
    signals: [
      { kind: "tool_error", severity: "broken",
        detail: "工具 db_query__shop 调用失败：no such table: merchant_account" },
      { kind: "step_limit", severity: "broken", detail: "agent 用满了 8 步还没给出结论" },
    ],
  },
  output: { answer: "大约 5 个左右。" },
}

/** 复核只是补了一句说明，答案照常给；外加改写前的原文可展开 */
const REVIEW_DEGRADED_TURN: StreamTurn = {
  id: "reviewdegraded", question: "谁能改别人的权限？", phase: "done", status: "已完成",
  steps: [],
  review: {
    verdict: "rewritten", severity: "degraded", retry: false,
    note: "这次知识库检索退回了纯关键词匹配，换了说法的内容可能没被搜到。",
    answer: null,
    signals: [
      { kind: "retrieve_degraded", severity: "degraded",
        detail: "重排结果解析不出来，按初筛顺序返回" },
    ],
  },
  output: { answer: "只有平台管理员可以修改他人权限，且需要走角色变更流程。" },
  rawOutput: { answer: "平台管理员可以修改他人权限。" },
}

/** 协作团队的泳道：前两轮并行、第三轮汇总 */
const TEAM_TURN: StreamTurn = {
  id: "team", question: "比较三种向量数据库在中文检索上的取舍", phase: "done", status: "已完成",
  steps: [{
    id: "n-team", seq: 1, kind: "node", status: "done", title: "调研团队", meta: "24.1 s", ms: 24100,
    team: {
      members: ["researcher", "analyst", "writer"],
      savedMs: 11800,
      finished: true,
      rounds: [
        {
          round: 0, parallel: 2, wallMs: 9600, sumMs: 16400,
          reason: "两边资料互不相干，可以同时查",
          members: [
            { agent: "researcher", instruction: "查 Milvus 与 Qdrant 的中文分词支持", ms: 9600,
              status: "done", result: "Milvus 2.4 起内置 jieba…" },
            { agent: "analyst", instruction: "查 pgvector 的中文检索方案", ms: 6800,
              status: "done", result: "pgvector 本身不分词，需配 zhparser…" },
          ],
        },
        {
          round: 1, parallel: 2, wallMs: 7400, sumMs: 12400,
          reason: "两组基准测试互不依赖",
          members: [
            { agent: "researcher", instruction: "跑召回率基准", ms: 7400, status: "done",
              result: "hit@1：Milvus 0.91 / Qdrant 0.89 / pgvector 0.84" },
            { agent: "analyst", instruction: "跑写入吞吐基准", ms: 5000, status: "done",
              result: "10 万条：Milvus 42s / Qdrant 51s / pgvector 88s" },
          ],
        },
        {
          round: 2, parallel: 1, wallMs: 7100, sumMs: 7100,
          reason: "两份基准都出来了，可以汇总",
          members: [
            { agent: "writer", instruction: "根据前面的结论写选型建议", ms: 7100,
              status: "done", result: "## 结论\n中文为主且要开箱即用 → Milvus…" },
          ],
        },
      ],
    },
  }],
  output: { 建议: "中文为主且要开箱即用选 Milvus；已有 Postgres 且数据量在百万以内，pgvector + zhparser 的运维成本最低。" },
}

const LONG_TURN: StreamTurn = {
  id: "long", question: "需要能够显示总结内容", phase: "done", status: "已完成",
  steps: [], output: { result: longReport() },
}

/** 合成场景：新后端才有的事件，老库里导不出来 */
function synthetic(name: string): StreamTurn[] {
  switch (name) {
    case 'team-live': {
      const ev = rebase(teamRun('members'))
      return [{ id: 'team-live', question: '比较三家供应商的交期风险', phase: 'running', status: '运行中',
                steps: decodeRun(ev), runId: 'syn-team-live' }]
    }
    case 'team-routing': {
      const ev = rebase(teamRun('routing'))
      return [{ id: 'team-routing', question: '比较三家供应商的交期风险', phase: 'running', status: '运行中',
                steps: decodeRun(ev), runId: 'syn-team-routing' }]
    }
    case 'team': {
      const ev = teamRun('done')
      return [{ id: 'team-done', question: '比较三家供应商的交期风险', phase: 'done', status: '已完成',
                steps: decodeRun(ev), output: ev[ev.length - 1].data.output, runId: 'syn-team-done' }]
    }
    case 'long': {
      const ev = longLoop(145)
      return [{ id: 'long-loop', question: '逐拍读取传感器，跑完这一批', phase: 'done', status: '已完成',
                steps: decodeRun(ev), output: ev[ev.length - 1].data.output, runId: 'syn-long' }]
    }
    case 'mixed': {
      const ev = mixedRun()
      const end = ev[ev.length - 1].data
      return [{ id: 'mixed', question: '9 月 19 日各班次出勤率', phase: 'error', status: '失败',
                steps: decodeRun(ev), output: MIXED_OUTPUT, runId: 'syn-mixed', runClass: 'exploratory',
                error: { error: String(end.error), detail: end.detail } }]
    }
    case 'issued': {
      // 同一份出具，跑完的样子：横幅、正文里画出来的数字、复制 / 导出
      const ev = mixedRun().filter((e) => e.type !== 'node.failed' && e.type !== 'run.failed')
      return [{ id: 'issued', question: '9 月 19 日各班次出勤率', phase: 'done', status: '已完成',
                steps: decodeRun(ev, { status: 'succeeded' }), output: MIXED_OUTPUT, runId: 'syn-issued',
                runClass: 'exploratory', review: REVIEW_DEGRADED_TURN.review }]
    }
    case 'live': {
      // 取数正在进行：第一条查询还没回来
      const all = mixedRun()
      const cut = all.findIndex((e) => e.type === 'tool.end' && e.data.call_id === 'q1')
      const ev = rebase(all.slice(0, cut), 12400)
      return [{ id: 'live', question: '9 月 19 日各班次出勤率', phase: 'running', status: '运行中',
                steps: decodeRun(ev), runId: 'syn-live' }]
    }
    case 'cancelled':
      return [{ id: 'cancelled', question: '把大表全量拉一遍', phase: 'done', statusCode: 'cancelled',
                steps: decodeRun(cancelledRun()), runId: 'syn-cancel' }]
    case 'chat': {
      // 问数据的一轮：先建图、再执行。执行开始后「规划」收成一行
      const plan = decodeCopilot([
        { op: 'heartbeat', phase: 'planning', elapsed_ms: 6400 },
        { op: 'thinking', delta: '要先查表结构，再按设备汇总 KPI。' },
        { op: 'plan', summary: '取数 → 汇总 → 出结论' },
        { op: 'add_node', node: { id: 'q', type: 'tool', label: '查询设备 KPI' } },
        { op: 'add_node', node: { id: 'o', type: 'output', label: '成果' } },
        { op: 'done', explanation: '两步' },
        { op: 'final', graph: { nodes: [{ id: 'q' }, { id: 'o' }] } },
      ])
      const ev = (fixtures as any).db as RunEvent[]
      return [{ id: 'chat', question: '这个库里有多少数据？挑最大的表说说', phase: 'done', status: '已完成',
                steps: [...plan, ...decodeRun(ev)], runId: 'syn-chat',
                output: ev[ev.length - 1].data.output,
                graph: { nodes: [{ id: 'q', type: 'tool', data: { label: '查询设备 KPI' } },
                                 { id: 'o', type: 'output', data: { label: '成果' } }] } }]
    }
    case 'copilot':
      return [{ id: 'copilot', question: '做一个每天的出勤日报', phase: 'done',
                status: '已放到画布，但还有 2 处问题要你处理',
                steps: decodeCopilot(COPILOT_STUCK, { context: 'canvas' }) }]
    case 'codes':
      // 编号、分组长得像数：'1063'、'001'。它们不该右对齐，也不该出「按 attribute_group 从高到低排」
      return [{ id: 'codes', question: '各产线本周产量', phase: 'done', status: '已完成', steps: [],
                output: { 结果: JSON.stringify({
                  columns: ['factory_code', 'line_name', 'attribute_group', 'output_qty'],
                  rows: [['1063', '一号线', '001', 1520], ['1064', '二号线', '002', 1310], ['1065', '三号线', '001', 980]],
                  row_count: 3 }) } }]
    case 'schema': {
      const ev = mixedRun().slice(0, 10)
      return [{ id: 'schema', question: '看看有哪些人事表', phase: 'done', status: '已完成',
                steps: decodeRun(ev, { status: 'succeeded' }) }]
    }
    default:
      return []
  }
}

/** Markdown 渲染用真实输出验，不用编的样本 */
function MarkdownCases() {
  return (
    <div className="mx-auto max-w-3xl space-y-4 p-4">
      {[...(mdSamples as string[]), HOSTILE].map((t, i) => (
        <div key={i} className="rounded-lg border bg-panel p-3">
          <div className="mb-2 text-2xs text-dim">真实输出 #{i + 1}</div>
          <Markdown text={t} />
        </div>
      ))}
    </div>
  )
}

/**
 * 一条在长的运行：window.__grow(n) 再放出 n 条事件。check-stream 用它验证
 * 「往上翻着看的时候不被拽回底部、底下浮出跳到最新」
 */
function Growing({ dense }: { dense: boolean }) {
  const [all] = useState(() => pipelineRun(60))
  const [n, setN] = useState(60)
  useEffect(() => {
    ;(window as any).__grow = (k: number) => setN((x) => Math.min(all.length, x + k))
  }, [all.length])
  const ev = rebase(all.slice(0, n))
  return (
    <AssistantStream dense={dense} turns={[{
      id: 'grow', question: '把这批报表各查一遍', phase: 'running', status: '运行中',
      steps: decodeRun(ev), runId: 'syn-grow',
    }]} />
  )
}

/** 审批卡的上下文：等了多久、挂在哪个节点、将以谁的名义签批 */
const APPROVAL = {
  id: 'ap-1', run_id: 'syn-mixed', node_id: 'review', mode: 'approve' as const,
  title: '出勤结论可以发出吗？', status: 'pending', response: {},
  payload: { message: '早班出勤率 **94.23%**，晚班 **73.08%**。' },
  created_at: new Date(Date.now() - 8 * 86_400_000 - 3_600_000).toISOString().replace('Z', ''),
  workflow_name: '出勤日报', node_label: '班长复核', run_class: 'formal' as const,
}

function Preview() {
  // ?case=db 只看一个用例，?theme=light 直接出浅色——都是为了截图可复现，
  // 不用靠点按钮
  const params = new URLSearchParams(location.search)
  const only = params.get('case')
  const syn = params.get('syn')
  const md = params.get('md') === '1'
  const theme = params.get('theme')
  if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme
  if (params.get('actor') != null) {
    try { localStorage.setItem('agentlab_actor', params.get('actor') ?? '') } catch { /* 预览里没有也行 */ }
  }
  const dense = params.get('dense') === '1'
  const [all] = useState(buildTurns)
  const turns = only ? all.filter((t) => t.id === only) : all
  const [denseToggle, setDense] = useState(false)
  const [emptyState, setEmptyState] = useState(false)
  // ?follow=1：真实用例也出追问标签，看它从真实结果集里派生出什么
  const onFollowUp = params.get('follow') === '1'
    ? (q: string) => { (window as any).__followUp = q } : undefined

  const empty = (
    <StreamEmpty
      icon={<MessageSquare size={22} />}
      title="问点什么"
      hint="它会自己接数据源、写查询、跑完给结论——你不用碰画布"
    />
  )

  if (md) return <div className="h-full overflow-y-auto"><MarkdownCases /></div>
  if (params.get('grow') === '1') return <Growing dense={dense} />
  if (params.get('approval') === '1') {
    return (
      <div className={dense ? 'w-[360px] border-r' : 'mx-auto max-w-3xl p-4'}>
        <div className="overflow-hidden rounded-lg border" style={{ borderColor: 'var(--warn)' }}>
          <ApprovalCard approval={APPROVAL} onResolved={() => undefined} />
        </div>
      </div>
    )
  }
  if (syn) {
    const linked: string[] = ((window as any).__linked ??= [])
    const stream = (
      <AssistantStream
        dense={dense}
        turns={synthetic(syn)}
        onStepHover={params.get('link') === '1' ? (id) => { (window as any).__hovered = id } : undefined}
        onStepFocus={params.get('link') === '1' ? (id) => { linked.push(id) } : undefined}
        renderTurnActions={(t) => (t.phase === 'error'
          ? <button type="button" className="btn btn-xs"><RotateCw size={11} aria-hidden /> 重试这一轮</button>
          : null)}
        onFollowUp={(q) => { (window as any).__followUp = q }}
      />
    )
    return dense ? <div className="h-full w-[360px] border-r bg-panel">{stream}</div> : stream
  }
  if (params.get('long') === '1') {
    return <AssistantStream turns={[LONG_TURN]} />
  }
  if (params.get('noquery') === '1') {
    return <AssistantStream turns={[NO_QUERY_TURN]} />
  }
  if (params.get('review') === '1') {
    return <AssistantStream turns={[REVIEW_BROKEN_TURN, REVIEW_DEGRADED_TURN]} />
  }
  if (params.get('fanout') === '1') {
    return <AssistantStream dense={dense} turns={[{
      id: 'fanout', question: '三个库各查一遍，汇总给我', phase: 'done', status: '已完成',
      steps: decodeRun((fixtures as any).fanout),
      output: { 结果: '三路都回来了' },
    }]} />
  }
  if (params.get('team') === '1') {
    return <AssistantStream turns={[TEAM_TURN]} dense={dense} />
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-3 border-b px-4 py-2 text-xs">
        <Database size={14} style={{ color: 'var(--accent)' }} />
        <span className="font-semibold">AssistantStream 预览</span>
        <span className="text-dim">真实事件 · 离线渲染</span>
        <span className="flex-1" />
        <label className="flex items-center gap-1.5 text-dim">
          <input type="checkbox" checked={denseToggle} onChange={(e) => setDense(e.target.checked)} />
          dense（360px 窄栏）
        </label>
        <label className="flex items-center gap-1.5 text-dim">
          <input type="checkbox" checked={emptyState}
                 onChange={(e) => setEmptyState(e.target.checked)} />
          空态
        </label>
        <button className="btn btn-sm btn-ghost"
                onClick={() => {
                  const el = document.documentElement
                  el.dataset.theme = el.dataset.theme === 'light' ? 'dark' : 'light'
                }}>
          切主题
        </button>
      </header>

      <div className="flex min-h-0 flex-1">
        {/* 宽屏：问数据页的形态 */}
        <div className="min-w-0 flex-1 border-r">
          <AssistantStream turns={emptyState ? [] : turns} dense={denseToggle} empty={empty}
                           onFollowUp={onFollowUp} />
        </div>
        {/* 窄栏：画布右侧助手栏的形态。固定 360px，和真实右栏一致 */}
        <div className="w-[360px] shrink-0 bg-panel">
          <AssistantStream turns={emptyState ? [] : turns} dense empty={empty} onFollowUp={onFollowUp} />
        </div>
      </div>
    </div>
  )
}

// 预览引用了 store（审批卡要用），别人改 store 时 HMR 会把这个入口重新执行一遍，
// 同一个容器上再 createRoot 会报错
const w = window as any
w.__previewRoot ??= createRoot(document.getElementById('root')!)
w.__previewRoot.render(
  <BrowserRouter>
    <ToastHost>
      <Preview />
    </ToastHost>
  </BrowserRouter>,
)
