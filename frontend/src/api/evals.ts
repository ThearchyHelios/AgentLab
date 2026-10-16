/**
 * 评测的接口（/api/evals，服务端 backend/app/api/evals.py；整组只给搭建者及以上），挂在 api.evals 上。形状见 typesEval.ts。
 *
 * 请求函数从 ./http 导入，不从 ./client 导入：client.ts 要导入这个文件把它挂到 api 上，反过来导入就成了循环（见 http.ts
 * 开头的说明）。错误都是 ApiError：改题集、改题目版本不符回 409；题目没过试算回 422（detail 是原因，field 指明是哪一格，
 * code 给程序认：eval_gold_masked 遮罩列、eval_gold_guard 没过只读守卫……）。
 *
 * 「口径命中与重复度」的详细分布直接调口径账本的 GET /api/ledger/stats（总方案 X2：E1–E4 只有一份实现），也放在这里。
 */
import type {
  EvalCaliber, EvalCaseBody, EvalCaseSaved, EvalCompare, EvalEstimate, EvalImported, EvalImportPlan, EvalPair,
  EvalPreview, EvalResult, EvalRun, EvalRunDetail, EvalRunQuery, EvalShadowBody, EvalShadowPlan, EvalSignals,
  EvalSnapshot, EvalSpend, EvalStartBody, EvalSuite, EvalSuiteDetail, EvalTrial, EvalUpgradePolicy, EvalUpgradeState,
  LedgerStats,
} from '../typesEval'
import { ApiError, BASE, NETWORK_MESSAGE, actorHeader, del, failure, get, patch, post, qs, report, type RequestOptions } from './http'
import { dispositionName } from './bundle'

const suite = (id: string) => `/evals/suites/${encodeURIComponent(id)}`
const evaluation = (id: string) => `/evals/evaluations/${encodeURIComponent(id)}`

export const evals = {
  // ---- 题集
  suites: (opts?: { archived?: boolean } & RequestOptions) =>
    get<{ suites: EvalSuite[] }>(`/evals/suites${qs({ archived: opts?.archived ? 'true' : undefined })}`, opts),
  suite: (id: string, opts?: RequestOptions) => get<EvalSuiteDetail>(suite(id), opts),
  createSuite: (body: { name: string; description?: string; timezone?: string }) => post<EvalSuite>('/evals/suites', body),
  /** 改题集：带上读到的 version，对不上 409。交回改后的题集（含题目） */
  updateSuite: (id: string, body: Record<string, unknown> & { version: number }) => patch<EvalSuiteDetail>(suite(id), body),
  /** 删题集：有评测记录的不能删（409 eval_suite_has_runs），只能归档 */
  removeSuite: (id: string) => del(suite(id)),

  // ---- 题目
  /** 建一道题：服务端先试算一次，没过回 422、不保存 */
  createCase: (suiteId: string, body: EvalCaseBody) => post<EvalCaseSaved>(`${suite(suiteId)}/cases`, body),
  /** 改一道题（带 version）：内容变了要重新试算、没过不保存；只改标签、启用的不试算 */
  updateCase: (suiteId: string, caseId: string, body: EvalCaseBody & { version: number }) =>
    patch<EvalCaseSaved>(`${suite(suiteId)}/cases/${encodeURIComponent(caseId)}`, body),
  removeCase: (suiteId: string, caseId: string) => del(`${suite(suiteId)}/cases/${encodeURIComponent(caseId)}`),
  /** 只试算不保存：没过的照实写原因（不回 422） */
  trial: (suiteId: string, body: EvalCaseBody) => post<{ trial: EvalTrial }>(`${suite(suiteId)}/cases/trial`, body),
  /** 按此刻试算一道已有的题，记进题目 */
  trialCase: (suiteId: string, caseId: string) =>
    post<{ trial: EvalTrial }>(`${suite(suiteId)}/cases/${encodeURIComponent(caseId)}/trial`),
  /** 写 SQL 定期望的预览：结果的列和前几行；遮罩列、守卫照实拒收（不回 422） */
  preview: (suiteId: string, body: { datasource_id: string; sql: string }) =>
    post<EvalPreview>(`${suite(suiteId)}/cases/preview`, body),

  // ---- 从运行认定
  runQueries: (runId: string) => get<{ queries: EvalRunQuery[] }>(`/evals/runs/${encodeURIComponent(runId)}/queries`),
  createFromRun: (suiteId: string, body: { run_id: string; query: string; expectations: EvalCaseBody['expectations']; question?: string; tags?: string[]; datasource_ids?: string[] }) =>
    post<EvalCaseSaved>(`${suite(suiteId)}/cases/from-run`, body),

  // ---- 口径
  calibers: () => get<{ calibers: EvalCaliber[] }>('/evals/calibers'),
  upgrade: (suiteId: string, caseId: string) =>
    get<{ upgrade: EvalUpgradeState | null; choices: Record<EvalUpgradePolicy, string> }>(
      `${suite(suiteId)}/cases/${encodeURIComponent(caseId)}/upgrade`),
  /** 处置口径升版：三选一，没有缺省；处置后的题同样要试算通过才生效 */
  disposeUpgrade: (suiteId: string, caseId: string, body: { policy: EvalUpgradePolicy; version: number }) =>
    post<EvalCaseSaved>(`${suite(suiteId)}/cases/${encodeURIComponent(caseId)}/upgrade`, body),

  // ---- 导出导入
  /** 题集导出成 JSON 文件（附件）：回来的是文件本身和服务端给的文件名 */
  exportFile: async (suiteId: string): Promise<{ blob: Blob; name: string | null }> => {
    const path = `${suite(suiteId)}/export`
    let res: Response
    try {
      res = await fetch(BASE + path, { headers: actorHeader() })
    } catch (e: any) {
      const err = new ApiError(0, NETWORK_MESSAGE, { kind: 'network', raw: `${e?.name ?? 'Error'}: ${e?.message ?? e}` })
      report(false, err)
      throw err
    }
    if (!res.ok) throw failure(res.status, res.statusText, await res.text().catch(() => ''), path)
    report(true)
    return { blob: await res.blob(), name: dispositionName(res.headers.get('Content-Disposition')) }
  },
  importPreview: (content: unknown) => post<EvalImportPlan>('/evals/suites/import/preview', { content }),
  importSuite: (content: unknown, name?: string) => post<EvalImported>('/evals/suites/import', { content, ...(name ? { name } : {}) }),

  // ---- 目录快照
  snapshots: () => get<{ snapshots: EvalSnapshot[] }>('/evals/catalog-snapshots'),
  snapshot: (id: string) => get<EvalSnapshot>(`/evals/catalog-snapshots/${encodeURIComponent(id)}`),
  takeSnapshot: (sourceIds: string[]) => post<EvalSnapshot>('/evals/catalog-snapshots', { source_ids: sourceIds }),
  shadowEstimate: (body: EvalShadowBody) => post<EvalShadowPlan>('/evals/catalog-snapshots/shadow/estimate', body),
  startShadow: (body: EvalShadowBody) => post<EvalSnapshot>('/evals/catalog-snapshots/shadow', body),
  stopSnapshot: (id: string) => post<EvalSnapshot>(`/evals/catalog-snapshots/${encodeURIComponent(id)}/stop`),

  // ---- 评测
  estimate: (suiteId: string, body: EvalStartBody) => post<EvalEstimate>(`${suite(suiteId)}/evaluations/estimate`, body),
  start: (suiteId: string, body: EvalStartBody) =>
    post<{ evaluations: EvalRun[]; pair_id: string | null }>(`${suite(suiteId)}/evaluations`, body),
  evaluations: (suiteId: string, limit = 50) => get<{ evaluations: EvalRun[] }>(`${suite(suiteId)}/evaluations${qs({ limit })}`),
  evaluation: (id: string, opts?: RequestOptions) => get<EvalRunDetail>(evaluation(id), opts),
  results: (id: string) => get<{ evaluation_id: string; cleaned: boolean; results: EvalResult[] }>(`${evaluation(id)}/results`),
  result: (id: string, resultId: string) =>
    get<EvalResult & { details: Record<string, any> | null }>(`${evaluation(id)}/results/${encodeURIComponent(resultId)}`),
  stop: (id: string) => post<EvalRun>(`${evaluation(id)}/stop`),
  /** 只把没做完的题接着做（A/B 时同一题几臂一起重做） */
  retry: (id: string) => post<{ evaluations: EvalRun[] }>(`${evaluation(id)}/retry`),
  /** 按现在的评分规则重新评分：只读封存，不调模型、不查业务库 */
  rescore: (id: string) => post<EvalRun & { changed: number }>(`${evaluation(id)}/rescore`),
  pin: (id: string, pinned: boolean) => post<EvalRun>(`${evaluation(id)}/pin`, { pinned }),
  compare: (id: string, base?: string | null) => get<EvalCompare>(`${evaluation(id)}/compare${qs({ base: base || undefined })}`),
  pair: (pairId: string) => get<EvalPair>(`/evals/pairs/${encodeURIComponent(pairId)}`),
  cleanup: (suiteId: string) => post<Record<string, unknown>>(`${suite(suiteId)}/cleanup`),

  // ---- 口径命中与重复度
  signals: (days: number, points = 6) => get<EvalSignals>(`/evals/signals${qs({ days, points })}`),
  /** 口径账本的统计（E1–E4 只有这一份实现）：since 是 ISO 时间 */
  ledgerStats: (since?: string | null) => get<LedgerStats>(`/ledger/stats${qs({ since: since || undefined })}`),

  // ---- 设置里的今日花费
  spend: () => get<EvalSpend>('/settings/eval/spend'),
}
