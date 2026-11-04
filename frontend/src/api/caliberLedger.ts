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
  CaliberDescriptor, CaliberDraft, CaliberDraftAttachOut, CaliberDraftSummary, CaliberTrialOut, CertifyQueue, DraftMetric,
  DraftTrialOut, FreezeOut, LedgerFeedbackOut, LedgerFeedbackVerdict, LedgerIssueOut, LedgerReconcileIn, LedgerReconcileOut,
  RunLedger,
} from '../typesLedger'
import { get, patch, post, type RequestOptions } from './http'

const at = (runId: string) => `/ledger/answers/${encodeURIComponent(runId)}`
const draftAt = (draftId: string) => `/ledger/drafts/${encodeURIComponent(draftId)}`

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
  /** 口径目录：每份已发布口径卡的入口 Schema、依赖、能否组合/出具（不返回 SQL、参数值） */
  catalog: (opts?: RequestOptions) => get<{ calibers: CaliberDescriptor[] }>('/ledger/calibers', opts),
  /** 按固定版本试跑：只读探索运行，返回指标值和 run_id；不是正式出具 */
  trial: (workflowId: string, version: number, nodeId: string, inputs: Record<string, unknown>) =>
    post<CaliberTrialOut>(
      `/ledger/calibers/${encodeURIComponent(workflowId)}/${version}/${encodeURIComponent(nodeId)}/trial`,
      { inputs },
    ),
  /**
   * 从业务文档起草口径（app/ledger/docdraft.py，搭建者）：发起后立刻返回 drafting，模型在后台读文档、对照数据目录
   * 写定义和取数，落成草稿图并自动试运行；结果用 draft() 反复取，直到状态离开 drafting
   */
  drafts: (status?: string, opts?: RequestOptions) =>
    get<{ drafts: CaliberDraftSummary[] }>(`/ledger/drafts${status ? `?status=${encodeURIComponent(status)}` : ''}`, opts),
  /** 一份草稿的完整内容：payload 摊平在上层（metrics / entry_fields / dropped / open_questions / alignment / trial） */
  draft: (draftId: string, opts?: RequestOptions) => get<CaliberDraft>(draftAt(draftId), opts),
  /** 起草后自动发起的那次只读试运行：状态和每个指标的值，running 时轮询到终态 */
  draftTrial: (draftId: string, opts?: RequestOptions) => get<DraftTrialOut>(`${draftAt(draftId)}/trial`, opts),
  /**
   * 按填好的入口字段再试运行一次（POST 同一个路径）：参数按入口字段校验，缺必填回 422；
   * 返回 {run_id, status, metrics, error}，status 是 running / succeeded / failed / needs_input
   */
  runDraftTrial: (draftId: string, inputs: Record<string, unknown>) =>
    post<DraftTrialOut>(`${draftAt(draftId)}/trial`, { inputs }),
  /** 选文档发起起草：documents 是要用的知识库文档 id，guidance 是给模型看的附加要求，
   *  fixed_filters 是每次跑都不变的条件（工厂、产线、属性组），服务端要求写死在 SQL 里、不做成入口字段 */
  extractDraft: (body: { documents: string[]; collection?: string; datasource_id?: string; guidance?: string;
    fixed_filters?: string }) =>
    post<{ draft_id: string; status: string }>('/ledger/drafts/extract', body),
  /**
   * 改标题、整体换掉指标、或把答清的问题消掉（resolved 是问题原文）。服务端据此重算状态：还有问题 → questions_open，
   * 否则 ready；已经接上或驳回的不改状态
   */
  patchDraft: (draftId: string, body: { title?: string; metrics?: DraftMetric[]; resolved?: string[] }) =>
    patch<CaliberDraft>(draftAt(draftId), body),
  /** 接上一次真实运行补取数路径（app/ledger/docattach.py）：由它建工作流和版本，定义来自草稿 */
  attachDraft: (draftId: string, runId: string) => post<CaliberDraftAttachOut>(`${draftAt(draftId)}/attach`, { run_id: runId }),
  /** 「不是口径」：置 rejected，写一条审计，以后不再出现在列表的待办里 */
  dismissDraft: (draftId: string, note?: string) =>
    post<{ ok: boolean; draft_id: string; status: string }>(`${draftAt(draftId)}/dismiss`, { note: note ?? null }),
}
