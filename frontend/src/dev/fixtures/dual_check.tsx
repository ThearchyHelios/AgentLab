import type { JSX } from 'react'
import { ApprovalCard } from '../../run/RunPanel'
import type { Approval } from '../../types'
import data from './dual_check.json'

/**
 * 双盲复算的预览用例，scripts/check-verify.mjs 按它们检查（夹具在同目录的 dual_check.json，检查脚本读的是同一份）：
 * - ?syn=dual-check-approval：正式运行里两路对不上的审批卡，宽栏（两条 SQL 左右并排）
 * - ?syn=dual-check-approval-rail：同一张卡摆在 360px 的画布右栏里（两条 SQL 上下排）
 * - ?syn=dual-check-approval-exploratory：探索运行，后果里不提出具
 *
 * 完整对照的工件（GET /api/artifacts/{id}）和审批回复接口由检查脚本用 page.route 伪造，直接打开时卡上照实说
 * 无法加载完整对照。提交后 onResolved 只把运行 id 记到 window.__dualResolved，不去接事件流
 */
const approval = (patch: Partial<Approval> = {}): Approval => ({
  ...(data.approval as unknown as Approval),
  created_at: new Date(Date.now() - 180_000).toISOString().replace('Z', ''),
  ...patch,
})

function Card({ value }: { value: Approval }) {
  return (
    <div className="overflow-hidden rounded-lg border" style={{ borderColor: 'var(--warn)' }}>
      <ApprovalCard approval={value} onResolved={(id) => { (window as any).__dualResolved = id }} />
    </div>
  )
}

export const cases: Record<string, () => JSX.Element> = {
  'dual-check-approval': () => (
    <div className="mx-auto max-w-3xl p-4 text-sm" data-fixture="dual-check-approval"><Card value={approval()} /></div>
  ),
  'dual-check-approval-rail': () => (
    <div className="h-full w-[360px] max-w-full overflow-y-auto border-r bg-panel" data-fixture="dual-check-approval-rail">
      <Card value={approval()} />
    </div>
  ),
  'dual-check-approval-exploratory': () => (
    <div className="mx-auto max-w-3xl p-4 text-sm" data-fixture="dual-check-approval-exploratory">
      <Card value={approval({ run_class: 'exploratory' })} />
    </div>
  ),
}
