import type { JSX } from 'react'
import { EvidenceDoc } from '../../run/EvidenceDoc'
import type { EvidenceDocData } from '../../types'
import judge from '../../run/__tests__/evidence-judge.json'

/**
 * 报告正文句末「质询」图标（审批质询）平时的样子，scripts/check-evidence.mjs 按它量平时状态的对比度：
 * - ?syn=evidence-ask：同一份没开裁判的周报（run/__tests__/evidence-judge.json 的 plain，后端 compose_doc 真跑出来的，
 *   内容虚构）摆两份，都给了质询入口：一份在页面底色上（同审批卡里展开的报告），一份在面板底色上（同运行详情的证据页）
 *
 * 证据图、片段接口由检查脚本伪造；不点「质询」，提问框不挂
 */
const DOC = judge.plain.doc as unknown as EvidenceDocData
const ACCESS = { approvalId: 'ap-evidence-ask', host: 'evidence-ask' }

export const cases: Record<string, () => JSX.Element> = {
  'evidence-ask': () => (
    <div className="flex max-w-3xl flex-col gap-3 p-4" data-fixture="evidence-ask">
      <div className="rounded-lg border bg-bg p-3" data-ask-on="bg">
        <EvidenceDoc doc={DOC} artifact={judge.plain.doc_artifact} runId={judge.plain.run_id} label="周报" inquiry={ACCESS} />
      </div>
      <div className="rounded-lg border bg-panel p-3" data-ask-on="panel">
        <EvidenceDoc doc={DOC} artifact={judge.plain.doc_artifact} runId={judge.plain.run_id} label="周报" inquiry={ACCESS} />
      </div>
    </div>
  ),
}
