import { useState, type JSX } from 'react'
import { EvidencePane } from '../../pages/runs/EvidencePane'
import { EvidenceDoc, EvidenceField } from '../../run/EvidenceDoc'
import { useEvidence } from '../../store/evidence'
import type { EvidenceDocData, Run } from '../../types'
import judge from '../../run/__tests__/evidence-judge.json'

/**
 * 正文里被反方审过的句子（期 4，run/EvidenceDoc 的句末标记）的预览用例，scripts/check-evidence.mjs 按它们检查。报告文档
 * 取自 run/__tests__/evidence-judge.json 的 plain（后端 compose_doc 真跑出来的周报，内容虚构）；证据图、片段接口、审计
 * 接口都由检查脚本伪造，这里不发任何写请求。
 *
 * - ?syn=evidence-marks-text：同一份周报，数字、取值都改成纯文字：整份报告一个可点的片段都没有，也没开裁判，挂上时键盘
 *   顺序是空的。证据图（检查脚本晚一点答）带着被审过的句子，标记到了以后得进 Tab 顺序。前面摆一个按钮，从它按 Tab 进报告
 * - ?syn=evidence-marks-stale：同一次运行先后在几个地方挂这份报告，看手里那份证据图过期了重不重取。按钮换场景（每按一次
 *   都重新挂）：审批卡里的初稿（old）；报告改写后第二次审批的新稿（new）；同一个实例从初稿换到新稿（switch，同画布的审批
 *   浮层换审批）；运行结束后成果区的新稿（output，EvidenceField）；运行详情的证据页（pane-interrupted：运行停在第二次审批
 *   上；pane-succeeded：运行已经结束、封存过）。两份文档预先放进证据 store：文档按工件 id 永久缓存，取文档不是这里要看的
 */

/** 同一次运行：初稿是夹具里那一份，新稿改了一句话和一个数（文档的 run_id 改成这次运行，成果区按它认是不是同一次运行） */
const RUN = 'run-evidence-marks-stale'
const OLD_ART = judge.plain.doc_artifact
const NEW_ART = 'b7'.repeat(32)
const BASE = judge.plain.doc as unknown as EvidenceDocData

const OLD: EvidenceDocData = { ...BASE, run_id: RUN }
const NEW: EvidenceDocData = {
  ...BASE,
  run_id: RUN,
  markdown: (BASE.markdown ?? '').replace('增长主要来自新客首单，老客复购持平。', '增长主要来自老客复购，新客首单持平。')
    .replace('退款 29 单', '退款 31 单'),
  blocks: BASE.blocks.map((b) => ({
    ...b,
    units: b.units?.map((u) => ({
      ...u,
      segments: u.segments?.map((s) => (s.id === 's11' ? { ...s, text: '增长主要来自老客复购，新客首单持平。' }
        : s.id === 's14' && s.cite ? { ...s, text: '31', cite: { ...s.cite, value: 31, rendered: '31' } } : s)),
    })),
  })),
}

/** 一个可点的片段都没有的那一份：非结构片段一律改成普通文字，去掉出处 */
const TEXT_ONLY: EvidenceDocData = {
  ...BASE,
  blocks: BASE.blocks.map((b) => ({
    ...b,
    units: b.units?.map((u) => ({
      ...u,
      segments: u.segments?.map((s) => (s.kind === 'structural' ? s : { id: s.id, kind: 'text', text: s.text, state: 'neutral' })),
    })),
  })),
}

function TextOnly() {
  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-3 p-4" data-fixture="evidence-marks-text">
      <button type="button" className="btn btn-xs self-start" data-marks-before="">报告前面的按钮</button>
      <div className="rounded-lg border bg-panel p-3" data-marks-doc="">
        <EvidenceDoc doc={TEXT_ONLY} artifact={judge.plain.doc_artifact} runId="run-evidence-marks-text" label="周报" />
      </div>
    </div>
  )
}

type Stage = 'old' | 'new' | 'switch' | 'output' | 'pane-interrupted' | 'pane-succeeded'
const STAGES: [Stage, string][] = [
  ['old', '审批卡 · 初稿'], ['new', '第二次审批 · 新稿'], ['switch', '同一张卡换稿'], ['output', '成果区 · 新稿'],
  ['pane-interrupted', '证据页 · 停在审批上'], ['pane-succeeded', '证据页 · 运行已结束'],
]

function runOf(status: 'interrupted' | 'succeeded'): Run {
  const done = status === 'succeeded'
  return {
    id: RUN, workflow_id: null, workflow_name: '周报', status, input: {}, output: {}, error: null,
    usage: { input_tokens: 0, output_tokens: 0, cost_usd: 0 } as Run['usage'], run_class: 'formal', version: 2,
    manifest_hash: done ? 'c'.repeat(64) : null, manifest_seq: done ? 64 : null,
    created_at: '2026-10-05T06:00:00', started_at: '2026-10-05T06:00:00',
  }
}

/** 审批卡里展开的报告（同 run/ApprovalReport 的 ReportDoc：限高、窄栏、自带证据条） */
function Card({ doc, artifact }: { doc: EvidenceDocData; artifact: string }) {
  return (
    <div className="max-h-96 w-[380px] max-w-full overflow-y-auto rounded border bg-bg p-2" data-marks-card={artifact}>
      <EvidenceDoc doc={doc} artifact={artifact} runId={RUN} label="周报" dense tally runClass="formal" />
    </div>
  )
}

/** 同一个 EvidenceDoc 实例从初稿换到新稿 */
function Switch() {
  const [fresh, setFresh] = useState(false)
  return (
    <div className="flex flex-col gap-2">
      <button type="button" className="btn btn-xs self-start" data-marks-switch="" onClick={() => setFresh(true)}>换成新稿</button>
      <div className="max-h-96 w-[380px] max-w-full overflow-y-auto rounded border bg-bg p-2" data-marks-card={fresh ? NEW_ART : OLD_ART}>
        <EvidenceDoc doc={fresh ? NEW : OLD} artifact={fresh ? NEW_ART : OLD_ART} runId={RUN} label="周报" dense tally runClass="formal" />
      </div>
    </div>
  )
}

function Stale() {
  // 首次渲染前把两份文档放进 store：文档按工件 id 永久缓存，证据页、成果区按工件 id 取的就是它们
  useState(() => {
    useEvidence.setState((s) => ({
      docs: { ...s.docs, [OLD_ART]: { status: 'ok', data: OLD }, [NEW_ART]: { status: 'ok', data: NEW } },
    }))
    return true
  })
  const [at, setAt] = useState<{ stage: Stage; n: number }>({ stage: 'old', n: 0 })
  const go = (stage: Stage) => setAt((cur) => ({ stage, n: cur.n + 1 }))
  return (
    <div className="flex h-screen min-w-0 flex-col gap-2 p-3" data-fixture="evidence-marks-stale">
      <div className="flex flex-wrap gap-1.5">
        {STAGES.map(([stage, label]) => (
          <button key={stage} type="button" className="btn btn-xs" data-marks-stage={stage} onClick={() => go(stage)}>{label}</button>
        ))}
      </div>
      {/* 每按一次都按新的 key 重新挂：同一场景按两次，看手里那份图对上了还会不会重取 */}
      <div key={at.n} className="min-h-0 min-w-0 flex-1" data-marks-at={at.stage}>
        {at.stage === 'old' && <Card doc={OLD} artifact={OLD_ART} />}
        {at.stage === 'new' && <Card doc={NEW} artifact={NEW_ART} />}
        {at.stage === 'switch' && <Switch />}
        {at.stage === 'output' && (
          <div className="max-w-3xl rounded-lg border bg-panel p-3">
            <EvidenceField artifact={NEW_ART} text={NEW.markdown ?? ''} runId={RUN} label="answer" tally runClass="formal" />
          </div>
        )}
        {(at.stage === 'pane-interrupted' || at.stage === 'pane-succeeded') && (
          <EvidencePane run={runOf(at.stage === 'pane-succeeded' ? 'succeeded' : 'interrupted')} output={null}
                        labelOf={(id) => (id === 'write' ? '写周报' : undefined)} refreshKey="0" />
        )}
      </div>
    </div>
  )
}

export const cases: Record<string, () => JSX.Element> = {
  'evidence-marks-text': () => <TextOnly />,
  'evidence-marks-stale': () => <Stale />,
}
