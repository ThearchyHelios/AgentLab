import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Database, MessageSquare } from 'lucide-react'
import { AssistantStream, StreamEmpty, type StreamTurn } from '../run/AssistantStream'
import { Markdown } from '../run/Markdown'
import { decodeRun } from '../run/decode'
import { ToastHost } from '../components/ui'
import fixtures from '../run/__tests__/fixtures.json'
import mdSamples from '../run/__tests__/markdown-samples.json'
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
  { key: 'supervisor', question: '冬虫夏草是什么', phase: 'done', status: '完成' },
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
  id: "noquery", question: "刚才那 6 个管理员是怎么算的？", phase: "done", status: "完成",
  steps: [], noQuery: true,
  output: { answer: "第 1 轮走的是 `role.level IN (platform, regional)` 这个口径。要换口径我重新查一遍。" },
}

/** 复核判了「不可信」：说明排在成果上方，且用报警色 */
const REVIEW_BROKEN_TURN: StreamTurn = {
  id: "reviewbroken", question: "商家账户有多少个？", phase: "done", status: "完成",
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
  id: "reviewdegraded", question: "谁能改别人的权限？", phase: "done", status: "完成",
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
  id: "team", question: "比较三种向量数据库在中文检索上的取舍", phase: "done", status: "完成",
  steps: [{
    id: "n-team", seq: 1, kind: "node", status: "done", title: "调研团队", meta: "24.1s",
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
  id: "long", question: "需要能够显示总结内容", phase: "done", status: "完成",
  steps: [], output: { result: longReport() },
}

/** Markdown 渲染用真实输出验，不用编的样本 */
function MarkdownCases() {
  return (
    <div className="mx-auto max-w-3xl space-y-4 p-4">
      {[...(mdSamples as string[]), HOSTILE].map((t, i) => (
        <div key={i} className="rounded-lg border bg-panel p-3">
          <div className="mb-2 text-[10px] text-faint">真实输出 #{i + 1}</div>
          <Markdown text={t} />
        </div>
      ))}
    </div>
  )
}

function Preview() {
  // ?case=db 只看一个用例，?theme=light 直接出浅色——都是为了截图可复现，
  // 不用靠点按钮
  const params = new URLSearchParams(location.search)
  const only = params.get('case')
  const md = params.get('md') === '1'
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

  if (md) return <div className="h-full overflow-y-auto"><MarkdownCases /></div>
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
    return <AssistantStream dense={params.get('dense') === '1'} turns={[{
      id: 'fanout', question: '三个库各查一遍，汇总给我', phase: 'done', status: '完成',
      steps: decodeRun((fixtures as any).fanout),
      output: { 结果: '三路都回来了' },
    }]} />
  }
  if (params.get('team') === '1') {
    return <AssistantStream turns={[TEAM_TURN]} dense={params.get('dense') === '1'} />
  }

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
