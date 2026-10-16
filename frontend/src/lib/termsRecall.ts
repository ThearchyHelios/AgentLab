/**
 * 口径召回的文案（docs/design/next-round/02-口径账本.md 做法 7、8）：版本用量、发布弹窗的召回预告、版本历史里的召回批次、
 * recall 待办卡、时间线上的两个封存后事件。规矩同 terms.ts 开头的术语表：「固定版本」不写钉住；「运行」不写跑；受管 /
 * 已发布；「完整出具 / 降档出具 / 不予出具」；不在句子里裸写键名。叫法同总方案 X10：召回、口径对账、来自口径。
 *
 * 召回只运行口径的数据部分（取数节点和口径卡）、不调用模型；两版在同一次运行里查同一份数据，差值在召回卡里算——这几句
 * 写在预告和待办里，让人知道这些数是怎么来的。消息、待办标题里不放数（服务端照规矩不放），界面上的数都点得开出处。
 */
import { formatNumber } from './format'

const n = (v: number) => formatNumber(v)

export const RECALL_TEXT = {
  // ---- 版本用量（版本历史、发布弹窗的「使用情况」那一行，跟在应用、生效记录、正式运行后面） ----
  usage: {
    answers: (count: number) => `问数回答 ${n(count)} 次`,
    exports: (count: number) => `导出 ${n(count)} 次`,
    schedules: (count: number) => `定时任务 ${n(count)} 个`,
    recall: (groups: number, changed: number) => `召回 ${n(groups)} 组${groups ? `（数变了 ${n(changed)} 组）` : ''}`,
    recalledFrom: (groups: number) => `被新版本召回 ${n(groups)} 组`,
  },

  // ---- 发布弹窗的召回预告 ----
  preview: {
    label: '召回',
    loading: '正在估算召回规模…',
    failed: '无法估算召回规模',
    none: '旧版本没有被问数回答、导出、定时任务或应用用过，发布后不需要召回',
    notCaliber: '旧版本里没有可复用的口径卡，发布后不召回',
    uses: (c: { answers: number; exports: number; schedules: number; apps: number; formal: number; pinned: number }) => {
      const parts = [
        c.answers ? `${n(c.answers)} 次问数回答` : '',
        c.exports ? `${n(c.exports)} 次导出` : '',
        c.schedules ? `${n(c.schedules)} 个定时任务` : '',
        c.apps ? `${n(c.apps)} 个应用` : '',
        c.formal ? `${n(c.formal)} 次正式运行` : '',
        c.pinned ? `${n(c.pinned)} 次固定了它的工作流的正式运行` : '',
      ].filter(Boolean)
      return parts.length ? `旧版本被 ${parts.join('、')}用过` : '旧版本没有被用过'
    },
    groups: (groups: number, will: number) => `按参数去重共 ${n(groups)} 组，发布后自动重算 ${n(will)} 组`,
    already: (count: number) => `其中 ${n(count)} 组之前已经召回过`,
    over: (count: number, limit: number) => `另有 ${n(count)} 组超出每次 ${n(limit)} 组的上限，发布后可在版本历史里手动接着算`,
    zero: '设置里每次最多重算 0 组：发布后不自动重算，只在版本历史里列出受影响的答案，可以手动接着算',
    notRecomputable: (count: number) => `${n(count)} 组无法重算`,
    hint: '重算只运行口径的数据部分（取数和口径卡），不调用模型；新旧两版在同一份数据上各算一次，数真变了才给答案的主人开待办',
    truncated: '用过的答案太多，只看了最近的一部分',
  },

  // ---- 版本历史里口径升到这一版之后的召回批次 ----
  batches: {
    title: '这一版的召回',
    loading: '正在读取召回情况…',
    failed: '无法读取召回情况',
    none: '旧版本没有被用过的答案，不需要召回',
    notCaliber: '旧版本里没有可复用的口径卡，不召回',
    summary: (c: Record<string, number>) => {
      const parts = [
        c.changed ? `数变了 ${n(c.changed)} 组` : '',
        c.unchanged ? `没变 ${n(c.unchanged)} 组` : '',
        c.running ? `正在重算 ${n(c.running)} 组` : '',
        c.failed ? `重算失败 ${n(c.failed)} 组` : '',
        c.not_recomputable ? `无法重算 ${n(c.not_recomputable)} 组` : '',
      ].filter(Boolean)
      return parts.join('，')
    },
    state: {
      changed: '数变了', unchanged: '没变', failed: '重算失败', running: '正在重算', pending: '未重算',
      not_recomputable: '无法重算',
    } as Record<string, string>,
    reason: (state: string, reason: string) => (state === 'not_recomputable' ? `无法重算：${reason}` : reason),
    remaining: (count: number) => `另有 ${n(count)} 组未重算`,
    continue: (size: number) => `接着算（每次 ${n(size)} 组）`,
    continuing: '正在发起…',
    continued: (count: number) => (count ? `已发起 ${n(count)} 组召回` : '没有发起新的召回'),
    from: (v: number) => `v${v} 的答案`,
    binding: '参数',
    noBinding: '没有参数',
    uses: (items: { kind_text: string; count: number }[]) => items.map((u) => `${u.kind_text} ${n(u.count)}`).join('、'),
    changedMetrics: (names: string[]) => `变了的指标：${names.join('、')}`,
    tasks: (count: number) => `开了 ${n(count)} 条待办`,
    openRun: '查看召回运行',
    more: (count: number) => `还有 ${n(count)} 组…`,
    truncated: '用过的答案太多，只看了最近的一部分',
  },

  // ---- recall 待办卡（待办箱、待办页） ----
  task: {
    intro: (name: string, from: number | null | undefined, to: number | null | undefined) =>
      `口径「${name}」从 v${from ?? '?'} 升到了 v${to ?? '?'}。在同一份数据上把新旧两版各算了一次，这一组答案用到的数变了。`,
    how: '两版在同一次运行里查同一份数据，差值在召回卡里算；原答案当时的值另外列出，和旧口径的值不同说明这段时间数据有变化。',
    binding: '这一组的参数',
    metric: '指标',
    old: (v: number | null | undefined) => `旧口径 v${v ?? '?'}`,
    new: (v: number | null | undefined) => `新口径 v${v ?? '?'}`,
    diff: '两版之差',
    original: '原答案当时的值',
    changed: '变了',
    same: '没变',
    masked: '已遮罩',
    missing: '—',
    answers: '用到这份口径的答案',
    answerOf: (kindText: string, at: string | null) => `${kindText}${at ? ` · ${at}` : ''}`,
    openAnswer: '查看答案',
    openTurn: '回到问数据',
    evidenceTitle: (name: string, side: string) => `${name} · ${side}`,
    reissue: '按新版重新出具',
    reissueHint: '按新版口径、这一组的参数发起一次正式运行（和按口径正式出具同一套检查）',
    reissued: '已按新版重新出具',
    issuedRun: '查看出具的正式运行',
    ack: '知道了',
    acked: '已记为知道了',
    doneReissue: '已按新版发起正式出具',
    doneAck: '已记为知道了',
    resolved: (decision: string | undefined) => (decision === 'reissue' ? '已按新版重新出具' : '已记为知道了'),
    catalogIntro: '答案依据的数据目录有变：按新目录检查，这次执行过的 SQL 出现了新的错误级问题。答案本身没有改，请核对是否还能用。',
    catalogItems: '变了的目录项',
    catalogChecks: (count: number) => `按新目录新出现的错误级问题 ${n(count)} 条`,
    structureIntro: (name: string, version: number | null | undefined) =>
      `口径「${name}」v${version ?? '?'} 依据的表结构变了，按最近一次的参数试算没有算出来。请检查口径的取数，必要时发布新版本。`,
    structureWhere: (table: string, column?: string | null) => `表 ${table}${column ? ` 的列 ${column}` : ''}`,
    probeError: '试算的报错',
    openProbe: '查看试算运行',
    change: {
      removed: '删掉了', rejected: '被驳回', changed: '改了',
    } as Record<string, string>,
  },

  // ---- 时间线（run/decode.ts 认 ledger.recalled、ledger.basis_changed） ----
  timeline: {
    answer: (v: number | null) => (v != null ? `已按 v${v} 核对，不影响` : '已按新口径核对，不影响'),
    recallChanged: (from: number | null, to: number | null) =>
      `召回核对${from != null && to != null ? `（v${from} → v${to}）` : ''}：数变了`,
    recallSame: (from: number | null, to: number | null) =>
      `召回核对${from != null && to != null ? `（v${from} → v${to}）` : ''}：数没变`,
    recallFailed: '召回重算失败',
    /** 认不出结论（缺字段）时只说核对过，不猜变没变 */
    generic: '口径召回核对',
    probeOk: '表结构变了之后试算口径：算出来了',
    probeFailed: '表结构变了之后试算口径：没有算出来',
    tasks: (count: number) => `开了 ${n(count)} 条待办`,
    basis: {
      catalog: '依据有变：数据目录里影响数字的项被驳回或修改',
      structure: '依据的表结构已变',
      values: '依据有变：出现了触及有效记录条件的新码值',
      catalog_edit: '依据有变：处理数据源变化时改了数据目录',
    } as Record<string, string>,
    /** 认不出是哪一种依据变了（缺字段、以后加的种类）：只说依据有变，不猜 */
    basisGeneric: '答案的依据有变',
    basisItems: (labels: string[]) => labels.join('、'),
    newErrors: (count: number) => `按新目录检查新出现 ${n(count)} 条错误级问题`,
    sub: '封存后追加，不在封存范围内',
  },
}
