/**
 * 口径账本的界面文案（docs/design/next-round/02-口径账本.md 做法 3–6、8 的界面部分；总方案 X10）：证据正文里数字上的
 * 小标记、证据面板的「口径」一步、问数据轮次底部的摘要和动作、口径对账、可沉淀的问题、设置里的口径账本一组。
 *
 * 叫法：来自口径（受管 / 已发布）· 等价已证明 / 助手选用、相关口径、这次的推理、口径对账、可沉淀的问题；出具档位沿用
 * 「完整出具 / 降档出具 / 不予出具」（服务端给 tier_text）；版本写「固定版本」。标签四种、认法两种、级别两种的叫法在
 * terms.ts 的 LEDGER_TEXT（时间线那一行同一套），这里不重写。规矩同 terms.ts 开头的术语表。
 */
import { formatNumber } from './format'

/** 口径的写法：「周销售」v3（受管） */
export const caliberName = (name: string | null | undefined, version: number | null | undefined, level?: string) =>
  `「${name || '未命名'}」${version != null ? `v${version}` : ''}${level ? `（${level}）` : ''}`

export const LEDGER_UI = {
  // ---- 证据正文：来自口径的数字上的小标记 ----
  mark: {
    /** 标记上的字就是级别（受管 / 已发布）；相关口径的数不加标记，只在面板里说 */
    title: (caliber: string, how: string) => `来自口径${caliber} · ${how}`,
    aria: (caliber: string, how: string) => `，来自口径${caliber}，${how}`,
  },

  // ---- 证据面板最上面的「口径」一步 ----
  step: {
    title: '口径',
    pending: '正在比对口径…',
    pendingSub: '运行封存后在后台和已发布口径比对，比完这里会自动更新',
    timeout: '比对口径还没有结果：可能在排队，或比对超过了时限',
    retry: '再取一次',
    failed: '无法读取口径比对',
    unreadable: (reason: string) => `口径比对记录无法读取${reason ? `：${reason}` : ''}`,
    params: '参数',
    noParams: '没有参数',
    masked: '已遮罩',
    column: (c: string) => `对应口径里的列「${c}」`,
    how: {
      independent: '这次 Agent 独立写的 SQL 被证明和这份口径的取数等价',
      composed: '助手按问法选用了这份口径，取数照那一版生成；这份口径答的是不是这个问题，要由人确认',
    },
    relatedSub: '这个数是这次的推理，下面这份已发布口径和它相关',
    where: '差在哪',
    noDiff: '没有归纳出具体差异',
    fallback: '两条 SQL 的结构对不上，未能归纳成具体差异，请对照原文',
    notComparable: '这份口径不能对账',
    reasoningSub: '没有相关的已发布口径',
    unknownSub: '没有和已发布口径比对',
  },

  // ---- 按口径正式出具（整题等价时） ----
  issue: {
    button: '按口径正式出具',
    busy: '正在发起…',
    /** 服务端判了能自动出具：比对完就自动发起，这里不另给按钮 */
    auto: '整题等价于这份口径，已在按口径自动正式出具',
    autoWaiting: '正在按口径自动出具…',
    autoTimeout: '自动出具的正式运行还没有出现，可以手动发起',
    launched: (caliber: string) => `已按口径${caliber}发起正式运行`,
    again: (caliber: string) => `已经按口径${caliber}出具过同样参数的这一次`,
    issuedRun: '已出具',
    readOnly: '对话在回收站中，恢复后才能出具',
  },
  /** 出具过的正式运行，挂在轮次下面 */
  issued: {
    title: (auto: boolean) => (auto ? '按口径自动出具' : '按口径正式出具'),
    running: '正式运行进行中',
    rejected: '被驳回',
    failed: '正式运行没有完成',
    cancelled: '已取消',
    open: '打开运行',
    /** 使用者读不了正式运行的原始记录（记录页只给搭建者） */
    userNote: '正式运行的记录由搭建者在记录页查看',
  },

  // ---- 口径对账 ----
  reconcile: {
    button: '看差多少',
    turnButton: (caliber: string) => `对账：${caliber}`,
    title: (caliber: string) => `口径对账：${caliber}`,
    intro: '按你的问法和按口径，在同一份数据上各算一次，差值在对账卡里算。只读，不写任何数据',
    where: '差在哪',
    params: '核对参数',
    paramsHint: '参数按这次的 SQL 推断，请核对',
    paramsFixed: '参数取自比对时绑定出来的值',
    editParams: '修改参数',
    required: '必填',
    defaulted: '留空用口径的缺省值',
    maskedPlaceholder: '已遮罩，请重新填写',
    pairs: '选择要对照的指标',
    pairsHint: '认不出这次的哪一列对应口径的哪个指标，请为要对照的列选一个口径卡上的指标',
    /** 有预选的：按逐格比对证明了的那几列预选上，人可以改 */
    pairsSuggested: '已按逐格比对的结论预选了能证明对应的列，请核对，可以改选',
    pairSuggestedTag: '预选',
    pairNone: '不对照',
    pairEmpty: '至少选一组对照',
    launch: '发起对账',
    busy: '正在发起…',
    cancel: '取消',
    launched: '已发起对账，结果在下面',
    readOnly: '对话在回收站中，恢复后才能对账',
  },
  /** 对账的结果卡片 */
  result: {
    title: (caliber: string) => `口径对账 · ${caliber}`,
    running: '对账进行中…',
    failed: '对账没有完成',
    cancelled: '对账已取消',
    inputs: { binding: '按比对时推断的参数', checked: '按核对过的参数' } as Record<string, string>,
    ours: '按你的问法',
    caliber: '按口径',
    diff: '差值',
    rel: '相对差',
    loading: '正在取值…',
    noValue: '取值失败',
    open: (what: string, label: string) => `${label} · ${what}：点开看出处`,
    more: (n: number) => `展开其余 ${formatNumber(n)} 组`,
    less: '收起',
    original: (text: string) => `原答案里写的是 ${text}；对账时按同样的 SQL 重新取数，数据有变化时两者可能不同`,
    openRun: '打开对账运行',
    /** 「差在哪」的说明有没有帮人看懂（统计 E2 数它，衡量差异说明写得好不好） */
    feedback: {
      ask: '「差在哪」的说明有帮助吗？',
      useful: '说明有用',
      unclear: '看不懂',
      mine: (v: 'useful' | 'unclear') => `你选了「${v === 'useful' ? '说明有用' : '看不懂'}」，可以改`,
      counts: (useful: number, unclear: number) => [useful ? `${formatNumber(useful)} 人觉得有用` : '',
        unclear ? `${formatNumber(unclear)} 人看不懂` : ''].filter(Boolean).join(' · '),
      saved: '已记下你的反馈',
      readOnly: '对话在回收站中，恢复后才能反馈',
    },
  },

  // ---- 答案身上的召回标记（问数据的轮次、证据面板「口径」一步） ----
  recall: {
    /** 口径升版后在同一份数据上重算过、数没变 */
    checked: (caliber: string) => `已按口径${caliber} 核对，不影响`,
    /** 依据有变：目录里影响数字的项被驳回或修改、出现新码值、处理变化时改了目录（目录类），表或列被删、类型变了（表结构） */
    basis: (what: 'catalog' | 'structure' | 'other', items: number) =>
      `${what === 'structure' ? '依据的表结构有变' : what === 'catalog' ? '依据的数据目录有变' : '答案的依据有变'}`
      + `${items ? `（${formatNumber(items)} 项）` : ''}`,
    newErrors: (n: number) => `按新目录检查新出现 ${formatNumber(n)} 条错误级问题`,
    basisHint: '答案本身没有改：它依据的数据目录或表结构在答案之后有变化，请核对是否还能用',
    /** 看的这个人手上说的是这个答案的 recall 待办：口径升版后数变了的、依据有变检查出新问题的 */
    taskCaliber: (version: number | null | undefined) => `口径升到 ${version != null ? `v${version}` : '新版本'}，这个答案用到的数变了`,
    taskBasis: '依据有变，按新目录检查出了新的问题',
    taskOther: '有一条召回待办',
    task: '去待办处理',
  },

  // ---- 问数据：轮次底部的摘要 ----
  turn: {
    pending: '正在比对口径…',
    timeout: '比对口径还没有结果',
    /** 一行摘要：几个数来自哪份口径、几个是这次的推理、几个没有比对 */
    summary: (parts: { caliber: number; calibers: string[]; reasoning: number; related: number; unknown: number }) => {
      const out: string[] = []
      if (parts.caliber) {
        out.push(parts.calibers.length === 1 ? `${formatNumber(parts.caliber)} 个数来自口径${parts.calibers[0]}`
          : `${formatNumber(parts.caliber)} 个数来自 ${formatNumber(parts.calibers.length)} 份口径`)
      }
      const reasoning = parts.reasoning + parts.related
      if (reasoning) {
        out.push(`${formatNumber(reasoning)} 个数是这次的推理${parts.related ? `（其中 ${formatNumber(parts.related)} 个有相关口径）` : ''}`)
      }
      if (parts.unknown) out.push(`${formatNumber(parts.unknown)} 个数没有比对`)
      return out.join('，')
    },
    whole: (caliber: string, how: string) => `整题等价于口径${caliber} · ${how}`,
    label: '口径',
  },

  // ---- 工作流列表页：可沉淀的问题 ----
  certify: {
    open: '可沉淀的问题',
    openHint: '被反复问到、反复写出同样查询的问题：固化成口径草稿，人审后发布，以后再问就认得出等价',
    back: '返回工作流列表',
    title: '可沉淀的问题',
    intro: '三类来源分开列出。固化成口径草稿会新建一张草稿工作流（报告引用的查询变成固定 SQL 的取数节点），在画布上审改、按受管级别发布后才生效',
    kinds: {
      shape: '确定性重复：同样形状的 SQL',
      cluster: '问题聚类：问法相近的问题',
      reused: '被反复用的答案',
    } as Record<string, string>,
    kindHint: {
      shape: '问数据里执行过、被报告引用、又没有对上任何口径的查询，按数据源和规范化后的 SQL 分组：出现在多次运行、多段对话里',
      cluster: '只按问题原文聚类（不含对话历史），问法相近不等于口径相同，固化前请确认是同一个问题',
      reused: '导出过多次、做成过应用、在画布里打开或提取过的答案',
    } as Record<string, string>,
    empty: '暂时没有可沉淀的问题',
    emptyBody: (runs: number) => `看了最近 ${formatNumber(runs)} 次问数据的运行，没有达到门槛的重复`,
    threshold: (runs: number, conversations: number) => `门槛：同一形状出现在 ${formatNumber(runs)} 次以上运行、来自 ${formatNumber(conversations)} 段以上对话`,
    shapeStats: (runs: number, conversations: number) => `${formatNumber(runs)} 次运行 · ${formatNumber(conversations)} 段对话`,
    params: (p: number, c: number) => `${formatNumber(p)} 个参数候选 · ${formatNumber(c)} 个口径常量`,
    clusterStats: (n: number) => `${formatNumber(n)} 个相近的问题`,
    uses: (u: { exports: number; app: boolean; extracted: boolean; canvas: boolean }) => [
      u.exports ? `导出 ${formatNumber(u.exports)} 次` : '', u.app ? '做成过应用' : '', u.extracted ? '提取过模板' : '',
      u.canvas ? '在画布里打开过' : '',
    ].filter(Boolean).join(' · '),
    source: (s: string) => `数据源 ${s}`,
    shape: 'SQL 形状（取值已换成「?」）',
    related: '已有相关口径',
    frozen: '已固化成',
    turns: '来自这些轮次',
    openTurn: '打开对话',
    openRun: '打开运行',
    freeze: '固化成口径草稿',
    freezeTurn: '按这一轮固化',
    freezeBusy: '正在固化…',
    frozenOk: (name: string) => `已固化成草稿「${name}」，在画布里审改后发布`,
    dismiss: '不是口径',
    dismissTitle: '这不是口径',
    dismissLabel: '理由',
    dismissPlaceholder: '例如：每次问的是不同门店的临时核对，不需要固定下来',
    dismissNeed: '请写明理由',
    dismissConfirm: '确认不是口径',
    dismissed: '已记下，这一项不再出现',
    noRun: '这一项没有可固化的运行',
    loadFailed: '无法读取可沉淀的问题',
  },

  // ---- 设置：口径账本一组 ----
  settings: {
    title: '口径账本',
    hint: '问数据的答案封存后在后台和已发布口径比对：按口径正式出具、口径对账、升版后的召回都按这里的设置',
    autoIssue: '自动按口径正式出具',
    autoIssueHint: '问数据的答案整题等价于受管口径（Agent 独立写的 SQL 被证明等价、图里没有人工节点和写入）时自动发起一次正式运行；不满足的只给一键出具',
    dailyLimit: '按口径正式出具每日上限',
    dailyLimitHint: '自动的、一键的合计；0 表示不再按口径出具。正式运行含报告撰写的模型费用',
    timeout: '口径比对时限（秒）',
    timeoutHint: '一次运行的口径比对最多算这么久，超时的照实记没比完',
    recallLimit: '口径升版后每次最多重算几组',
    recallLimitHint: '发布新版本后自动重算受影响的答案，按参数去重成组；超出的写明另有几组未重算；0 表示不自动重算',
    invalid: {
      limit: (max: number) => `请填写 0 到 ${formatNumber(max)} 之间的整数`,
      timeout: (lo: number, hi: number) => `请填写 ${lo} 到 ${hi} 之间的秒数`,
    },
    saveInvalid: '口径账本的设置中有无效值，请修正后再保存',
  },
}
