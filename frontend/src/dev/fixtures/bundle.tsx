import type { JSX } from 'react'
import { ProvenanceBar } from '../../pages/runs/Telemetry'
import type { RunPhase } from '../../run/trace'
import type { Run } from '../../types'

/**
 * 可验证报告的预览用例：记录页封存凭证条上的「导出可验证报告」。scripts/check-bundle.mjs 按 ?syn=<用例名>
 * 打开，用 page.route 假装 /api/runs/{id}/bundle.html，不碰真实数据。
 *
 * - bundle-provenance-sealed：已封存，可以导出
 * - bundle-provenance-running / -waiting：还在运行、等待审批，按钮不可用并说明原因
 * - bundle-provenance-unsealed：结束了却没有封存清单（加封存之前的老运行）
 * - bundle-provenance-again：失败后接着跑，留着上一次的封存，正在运行：同样不可用（跑完会换掉清单哈希），左边写着
 *   「已封存」，原因写在屏幕上
 * - bundle-provenance-failed：失败结束、已封存：可以导出，提示里说明之后继续运行会重新封存
 */
const SEALED: Run = {
  id: 'f4b0f4b0f4b0f4b0f4b0f4b0f4b0f4b0',
  workflow_id: 'wf-bundle-sample',
  workflow_name: '门店周报',
  status: 'succeeded',
  input: {},
  output: {},
  error: null,
  usage: {},
  run_class: 'formal',
  version: 3,
  version_hash: 'a1'.repeat(32),
  manifest_hash: 'c3b5'.repeat(16),
  manifest_seq: 59,
  started_by: '值班分析员',
  created_at: '2026-09-07T01:00:00Z',
  started_at: '2026-09-07T01:00:01Z',
  finished_at: '2026-09-07T01:02:03Z',
}

const unsealed = (status: Run['status']): Run => ({ ...SEALED, status, manifest_hash: null, manifest_seq: null, finished_at: null })

function Frame({ run, phase }: { run: Run; phase: RunPhase }) {
  return (
    <div className="mx-auto max-w-3xl p-4" data-fixture="bundle-provenance">
      <div className="overflow-hidden rounded-lg border bg-panel">
        <ProvenanceBar run={run} eventCount={25} phase={phase} />
      </div>
    </div>
  )
}

export const cases: Record<string, () => JSX.Element> = {
  'bundle-provenance-sealed': () => <Frame run={SEALED} phase="succeeded" />,
  'bundle-provenance-running': () => <Frame run={unsealed('running')} phase="running" />,
  'bundle-provenance-waiting': () => <Frame run={unsealed('interrupted')} phase="waiting" />,
  'bundle-provenance-unsealed': () => <Frame run={{ ...unsealed('succeeded'), finished_at: SEALED.finished_at }} phase="succeeded" />,
  'bundle-provenance-again': () => <Frame run={{ ...SEALED, status: 'running', finished_at: null }} phase="running" />,
  'bundle-provenance-failed': () => <Frame run={{ ...SEALED, status: 'failed' }} phase="failed" />,
}
