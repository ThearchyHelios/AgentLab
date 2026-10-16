/**
 * 口径账本的接口（docs/design/next-round/02-口径账本.md），挂在 api.caliberLedger 上。形状见 typesLedger.ts。
 *
 * - run：GET /api/runs/{id}/ledger，一次运行里每个数的口径标签、整题判断、能做的动作、出具过的正式运行、对过的账；
 * - issue：按口径正式出具（参数只由服务端从比对工件里取，这里只能选「按哪份口径」）；
 * - reconcile：口径对账（点开才跑一次确定性派生的只读图）；feedback：对账结果上的「说明有用 / 看不懂」（记成对账运行上的
 *   封存后事件，统计 E2 数它）；
 * - certifyQueue / dismiss：可沉淀的问题、「不是口径」（搭建者及以上）；
 * - freeze：固化成口径草稿（/copilot/from-run 的 freeze 选项，报告引用的查询变成固定 SQL 的取数节点）。
 *
 * 应用平台的「账本」（版本被哪些应用固定、生效记录）在 api.ledger（api/apps.ts），和这里不是一回事。
 * 请求函数从 ./http 导入，不从 ./client 导入（client.ts 要导入这个文件，反过来就成了循环）。
 */
import type {
  CertifyQueue, FreezeOut, LedgerFeedbackOut, LedgerFeedbackVerdict, LedgerIssueOut, LedgerReconcileIn, LedgerReconcileOut, RunLedger,
} from '../typesLedger'
import { get, post, type RequestOptions } from './http'

const at = (runId: string) => `/ledger/answers/${encodeURIComponent(runId)}`

export const caliberLedger = {
  run: (runId: string, opts?: RequestOptions) => get<RunLedger>(`/runs/${encodeURIComponent(runId)}/ledger`, opts),
  issue: (runId: string, body: { workflow_id: string; version: number }) => post<LedgerIssueOut>(`${at(runId)}/issue`, body),
  reconcile: (runId: string, body: LedgerReconcileIn) => post<LedgerReconcileOut>(`${at(runId)}/reconcile`, body),
  feedback: (runId: string, reconcileId: string, verdict: LedgerFeedbackVerdict) =>
    post<LedgerFeedbackOut>(`${at(runId)}/reconcile/${encodeURIComponent(reconcileId)}/feedback`, { verdict }),
  certifyQueue: (opts?: RequestOptions) => get<CertifyQueue>('/ledger/certify-queue', opts),
  dismiss: (body: { item_id: string; kind?: string; note: string }) =>
    post<{ ok: boolean; item_id: string }>('/ledger/certify-queue/dismiss', body),
  /**
   * 固化成口径草稿：代表运行里报告引用了的查询变成固定 SQL 的取数节点，peers（同一个 SQL 形状的别的运行）用来认哪些字面量
   * 跨轮变了。落成新的草稿工作流，人在画布上审改、按受管级别发布
   */
  freeze: (runId: string, opts: { peers?: string[]; conversation_id?: string | null; turn_id?: string | null } = {}) =>
    post<FreezeOut>('/copilot/from-run', {
      run_id: runId, freeze: true, peers: opts.peers ?? [],
      ...(opts.conversation_id ? { conversation_id: opts.conversation_id } : {}),
      ...(opts.turn_id ? { turn_id: opts.turn_id } : {}),
    }),
}
