import type { JSX } from 'react'
import { EvidenceDoc } from '../../run/EvidenceDoc'
import evidenceQuery from '../../run/__tests__/evidence-query.json'
import type { EvidenceDocData } from '../../types'

/**
 * 数字推演的预览用例，scripts/check-whatif.mjs 按它们检查：
 * - ?syn=whatif：一份报告（evidence-query.json 里后端 compose_doc 真跑出来的文档，只用通用名），点数字打开证据面板，
 *   指标步骤上的「推演」展开推演区。宽屏从右侧弹出，窄屏（390px）从底部抽出
 * - ?syn=whatif-rail：同一份报告摆在 360px 的画布右栏里，面板栏内展开
 *
 * 片段接口、推演接口都由检查脚本用 page.route 伪造（按运行 id 认出这份夹具），直接打开时面板照实说取不到证据链。
 * 给了 runClass，文档不再按运行 id 去问运行类别。
 */
const DOC = evidenceQuery.doc as unknown as EvidenceDocData

function Report({ dense = false }: { dense?: boolean }) {
  return (
    <EvidenceDoc doc={DOC} artifact={evidenceQuery.doc_artifact} runId={evidenceQuery.run_id} label="周报" tally
                 dense={dense} runClass="formal" />
  )
}

export const cases: Record<string, () => JSX.Element> = {
  whatif: () => (
    <div className="mx-auto max-w-3xl p-4 text-sm" data-fixture="whatif"><Report /></div>
  ),
  'whatif-rail': () => (
    <div className="h-full w-[360px] max-w-full overflow-y-auto border-r bg-panel p-2.5" data-fixture="whatif-rail">
      <Report dense />
    </div>
  ),
}
