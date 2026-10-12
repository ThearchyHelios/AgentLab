/*
 * 可验证报告的浏览器端核对：原生 JS，无依赖，不连网。
 *
 * 数据在页面末尾 id 为 bundle-data 的 JSON 数据块里（服务端 bundle/build.py 生成），逐项核对：
 *   ① 每条带规范字符串的事件：sha256(canonical) 等于它的 data_sha256；
 *   ② 封存清单：照 runner 的拼法（每行 seq|type|node_id|data_sha256，node_id 为空写空串，按 seq 排序，换行连接，
 *      没有结尾换行）重算，等于封存哈希；页面顶部直接写出的那份封存哈希（不执行脚本也看得到）要和它一致；
 *   ③ 每份嵌入的工件：sha256(原字节) 等于工件 id；
 *   ④ 引用链：报告文档的 id 是这个报告节点最后一次自查事件（已核对）记下的那份，文档记的运行、节点对得上；
 *      口径卡和快照的 id 出现在已核对的事件内容里，或者已核对的报告文档的证据目录里；
 *   ⑤ 报告里的每个数字和取值：引用的值等于口径卡里那个指标的值，或者快照里那一格的值（判等照搬服务端
 *      engine/expressions.py 的 cell_value / same_value）；正文显示的字等于按服务端的显示规则（engine/evidence.py 的
 *      render_number / render_cell：千分位、小数位、单位、万 / 亿换算、百分数，文字压成一行）把那个值渲染出来的字，
 *      两边都只去掉首尾空白。本页渲染不出来的写法判无法核对，不判通过。运行输入不随文件附带，照旧无法核对，但显示的字
 *      照样拿引用的值核对，对不上判不通过。核对通过的字要原样显示在正文里（checkLayout）：正文里根本不显示的判不通过，
 *      紧挨着写着数字、小数点、负号（读者看到的是另一个数）的判不通过；画着删除线、后面接着量级单位（含 bn、billion
 *      这类英文写法）或乘号乘方再接数字、紧挨着像负号的短横（含 en dash）、长得像数字的字母、显示成什么说不准的字、
 *      常见写法名单以外的字，或者同一句里有改变文字方向的字，读起来可能不是这个值的判无法核对；列表缩进有上限、表格只排
 *      有格的列，伪造的缩进、列号挤不走核对过的数；
 *   ⑥ 运行信息：运行结果、运行类别、出具档位拿封存范围内已核对的事件对照。工作流名、版本、起止时间、节点名称
 *      绑不进封存，页面上写明它们取自运行记录。
 * 每项给出通过 / 不通过 / 无法核对和原因。服务端导出时不会出现的状态（被引用的工件不在清单里、检索快照这类工件标着
 * 遮罩）一律算不通过：只可能是文件被改过。报告文档的证据目录里写着取自遮罩列的指标的值时，服务端只给它的哈希
 * （升级前生成的文档，或者生成之后数据源才设的遮罩），页面照实说明，这份报告算未能核对。总览只有一项无法核对都没有、
 * 每份报告都核对上了才是绿色的通过（overallOf）：有无法核对的项写「可核对的项全部通过，另有 N 项无法核对」，用中性色，
 * 无法核对的项里可能正藏着读者看到的另一个数。这些只证明内部一致，真伪要拿封存哈希去系统里对照，页面上写明；总览
 * 不是绿色的通过时，总览下面另醒目地写一遍（运行 ID、/verify 接口、定时任务通知里的清单哈希前 12 位）。
 *
 * SHA-256 优先用浏览器的 crypto.subtle；拿不到（非安全上下文、某些浏览器打开本地文件时）用下面的纯 JS 实现，
 * 两者结果一致。纯 JS 实现启动时先拿标准测试向量自检，自检不过就不下任何结论。
 * 脚本放在 head 里：一开始执行就给 html 标上 data-js（样式表据此把「尚未核对」换成「正在核对」，附件预览这类不执行
 * 脚本的地方看到的是前一句），等文档解析完再核对。来自数据的文字一律经 textContent 写进页面，不拼 HTML。
 */
(() => {
  'use strict'

  const PASS = 'pass'
  const FAIL = 'fail'
  const NA = 'na'
  /** 汇总用（分块、报告的徽标）：一组里有通过也有无法核对，没有不通过 */
  const PARTIAL = 'partial'
  /** 一块里条目超过这么多就收起来，只把不通过和无法核对的列在外面 */
  const FOLD_AT = 20

  // ------------------------------------------------------------------ 文案

  const RESULT = { pass: '通过', fail: '不通过', na: '无法核对', partial: '部分无法核对' }
  const MARK = { pass: '✓', fail: '✗', na: '?', partial: '?' }
  const KIND_LABEL = { report_doc: '报告文档', metric_set: '口径卡', query: '查询快照' }
  const TIER_LABEL = { formal: '完整出具', degraded: '降档出具', withheld: '不予出具' }
  const CLASS_LABEL = { formal: '正式运行', exploratory: '探索运行' }
  const STATUS_LABEL = { succeeded: '已完成', failed: '已失败', cancelled: '已取消' }
  /** 结束类事件 → 它说明的运行结果（runner：成功、失败、取消各发一条，落库之后才封存） */
  const TERMINAL = { 'run.finished': 'succeeded', 'run.failed': 'failed', 'run.cancelled': 'cancelled' }
  const EVENT_LABEL = {
    'run.started': '运行开始', 'run.finished': '运行结束', 'run.failed': '运行失败', 'run.cancelled': '运行取消',
    'run.interrupted': '等待人工介入', 'run.resumed': '恢复运行', 'node.started': '节点开始', 'node.finished': '节点完成',
    'node.failed': '节点失败', 'node.skipped': '节点跳过', 'edge.taken': '分支走向', 'llm.start': '模型调用开始',
    'llm.end': '模型调用结束', 'llm.thinking': '模型思考', 'tool.start': '工具调用', 'tool.end': '工具返回',
    'tool.error': '工具出错', 'tool.gated': '门控判定', 'retrieve.end': '知识检索', 'memory.end': '记忆读写',
    'merge.end': '合并查询', 'sandbox.start': '沙箱开始', 'sandbox.end': '沙箱结束', 'agent.step.start': 'Agent 步骤开始',
    'agent.step.end': 'Agent 步骤结束', 'agent.route.start': '调度决策开始', 'agent.route.end': '调度决策结束',
    'human.requested': '请求人工审批', 'human.resolved': '人工审批结果', 'approval.inquiry': '审批质询',
    'issuance': '出具判定', 'caliber.upgrade': '口径卡升版处置', 'catalog.drift': '数据目录变化',
    'report.checked': '报告自查', 'evidence.judged': '按需裁判', 'log': '日志', 'usage': '用量',
  }
  const T = {
    allPass: '全部通过',
    passWithNa: (n) => `可核对的项全部通过，另有 ${n} 项无法核对`,
    failed: (n) => `有 ${n} 项不通过`,
    incomplete: (n) => `有 ${n} 份报告未能核对`,
    /** 总览不是绿色的通过时醒目地写在下面：这个文件核对不了真伪，真伪以系统里的封存核对为准 */
    verifyHint: (run, prefix) => `这个文件只能核对内部是否一致，判断不了真伪：请在系统中打开运行 ${run}，确认清单哈希与上方`
      + `一致并点「核对」（接口 GET /api/runs/${run}/verify），或与定时任务通知里的清单哈希前 12 位「${prefix}」对照，`
      + '一致才说明这些记录出自系统、未被改过。',
    tally: (p, f, n) => `通过 ${p} 项 · 不通过 ${f} 项 · 无法核对 ${n} 项`,
    broken: '页面数据无法解析，无法核对',
    selfTestFailed: '本页的 SHA-256 实现在这个浏览器中自检未通过，不给出核对结论',
    metaRun: '运行 ID', metaStatus: '运行结果', metaClass: '运行类别', metaTier: '出具档位', metaRange: '封存范围',
    metaExported: '导出时间', metaWorkflow: '工作流', metaVersion: '版本', metaVersionHash: '版本哈希', metaStarted: '开始',
    metaFinished: '结束',
    recordNote: '以下几项和页面上的节点名称取自运行记录，不在封存范围内，需要在系统中一并对照',
    // 序号不连续（模型的流式输出占序号、不落库），后面注明范围内实际有几条。说法同记录页的封存凭证
    range: (seq, count) => `第 1–${seq} 条事件（范围内 ${count} 条）`,
    rangeLegacy: '（早期运行，范围按结束事件推算）',
    markPass: '已核对',
    markFail: (claim) => `文件里写的是「${claim}」，见核对明细 ⑥`,
    markNa: '无法核对',
    noReports: '这次运行没有报告文档，下方只核对运行记录。',
    reportTitle: (label) => `报告「${label}」`,
    reportParts: (doc, nums) => `文档 ${doc} · 数字与取值 ${nums}`,
    reportNoNumbers: '无',
    docUntrusted: '这份报告文档没有通过核对（见核对明细 ③ ④），正文仅供参考，其中的数字不予核对。',
    docUnreadable: '这份报告文档的哈希一致，但内容无法解析，无法显示正文。',
    docMissing: '导出时服务端已没有这份报告文档，无法显示正文，其中的数字也无法核对。',
    docMasked: '这份报告文档的证据目录里写着取自遮罩列的指标的值（生成时没有按遮罩换掉），未随文件附带，无法显示正文，'
      + '其中的数字也无法核对。',
    docAbsent: '文件里没有附带这份报告文档。',
    checkEvents: '① 事件内容与哈希',
    checkManifest: '② 封存清单',
    checkArtifacts: '③ 工件与哈希',
    checkChain: '④ 引用链',
    checkNumbers: '⑤ 报告中的数字与取值',
    checkMeta: '⑥ 运行信息',
    sumEvents: (n) => `${n} 条带内容的事件；其余事件只附哈希，参与 ② 的清单核对`,
    sumManifest: (n) => `按 ${n} 条事件的哈希重算`,
    sumCount: (n, unit) => `${n} ${unit}`,
    sumNumbers: (numbers, values) => `数字 ${numbers} 个 · 取值 ${values} 个`,
    sumMeta: '与封存范围内已核对的事件对照',
    showAll: (n) => `展开全部 ${n} 项`,
    eventMismatch: '内容与记录的哈希不一致',
    eventNotJson: '内容不是有效的 JSON',
    manifestComputed: '重算结果',
    manifestShown: '页面顶部写出的封存哈希',
    manifestLines: (n) => `查看参与清单核对的 ${n} 行（每行 序号|类型|节点|内容哈希，可复制后用任意 SHA-256 工具自行计算）`,
    manifestOk: '与封存清单哈希一致',
    manifestMismatch: '与封存清单哈希不一致：事件被增删、改动或换了顺序',
    beyondSeal: (seq) => `第 ${seq} 条事件的序号超出封存范围`,
    rangeMismatch: (seq, last) => `封存范围写的是第 1–${seq} 条，范围内最后一条事件却是第 ${last} 条`,
    shownOk: '与核对用的封存哈希一致',
    shownMismatch: '与数据里核对用的封存哈希不一致，页面被改过；上方现在显示的是核对用的那一份',
    artifactMismatch: '内容与工件 id（哈希）不一致',
    artifactUndecodable: '内容无法解码',
    artifactUnparsable: '哈希一致，但内容无法解析',
    /** 查询快照只给哈希：服务端证明不了这条查询没碰设了遮罩的列（没碰的照常随文件附带） */
    maskedQuery: '这条查询用到或可能用到设了遮罩的列，快照未随文件附带，只能核对到哈希',
    /** 口径卡只给哈希：卡上有输入取自遮罩列（卡上记着输入的值和代入式） */
    masked: '含遮罩列，未随文件附带，只能核对到哈希',
    maskedMismatch: '导出时服务端的这份文件与工件 id（哈希）不一致，可能被改过；按含遮罩列处理，未随文件附带',
    maskedUnparsable: '导出时服务端的这份文件无法解析，认不出是否含遮罩列，按含遮罩列处理，未随文件附带',
    /** 报告文档只给哈希：证据目录里写着值取自遮罩列的指标的原值（升级前的报告撰写节点生成的，或者生成之后数据源才设的遮罩） */
    maskedDoc: '证据目录里写着取自遮罩列的指标的值（生成时没有按遮罩换掉），未随文件附带，只能核对到哈希',
    maskedOdd: '只有查询快照、口径卡和报告文档会因遮罩不随文件附带，这份工件却标着遮罩：导出时不会出现这种情况，文件可能被改过',
    artifactMissing: '导出时服务端已没有这份工件的文件',
    artifactAbsent: '内容未随文件附带：导出时不会出现这种情况，文件可能被改过',
    chainDoc: (label) => `报告「${label}」的文档`,
    chainByEvent: (seq, what) => `第 ${seq} 条事件（${what}）`,
    chainByDoc: (label, alias) => `报告「${label}」的证据目录（${alias}）`,
    chainFrom: '引用自',
    chainNoCheckEvent: '找不到这个报告节点的报告自查事件',
    chainCheckEventFailed: '报告自查事件的内容没有通过核对',
    chainOtherDoc: '这个节点最后一次报告自查记下的不是这份文档',
    chainDocNotListed: '文件的工件清单里没有这份报告文档：导出时不会出现这种情况，文件可能被改过',
    chainDocUnverified: '文档内容没有通过核对，无法确认它记下的运行和节点',
    chainDocMissing: '导出时服务端已没有这份报告文档，无法确认它记下的运行和节点',
    chainDocMasked: '文档的证据目录里写着取自遮罩列的指标的值，未随文件附带，无法确认它记下的运行和节点',
    chainDocUnparsable: '文档的哈希一致，但内容无法解析，无法确认它记下的运行和节点',
    chainDocElsewhere: '文档记下的运行或节点与这次不符',
    chainDocLeftOut: '运行记录里有这个报告节点的自查事件，文件里却没有附带它的文档',
    chainUnreferenced: '已核对的事件和报告文档里都没有引用这份工件',
    srcMetric: (name) => `口径卡指标「${name}」`,
    srcCell: (alias, row, column) => `查询 ${alias} · 第 ${row + 1} 行 · ${column}`,
    srcQuery: (alias) => `查询 ${alias}`,
    srcInput: (field) => `运行输入「${field}」`,
    srcNone: '没有出处',
    srcOther: '其他引用',
    numUncited: '该数字未使用引用标记，无法核对其来源',
    numUnresolved: (why) => `报告生成时这处引用未能解析${why ? `：${why}` : ''}`,
    numInput: '引用的是运行输入，运行输入未随文件附带',
    numOther: '这类引用不在核对范围内',
    numDocUntrusted: '所在的报告文档没有通过核对',
    numNoEntry: '报告的证据目录里没有这处引用指向的条目',
    numArtifactNotListed: '引用的工件不在文件的工件清单里：导出时不会出现这种情况，文件可能被改过',
    numArtifactFailed: '引用的工件没有通过核对（见核对明细 ③）',
    numArtifactUnparsable: '引用的工件哈希一致，但内容无法解析',
    numNotChained: '引用的工件不在引用链上',
    numNoMetric: '口径卡里没有这个指标',
    numMetricDiffers: '引用的值与口径卡里的值不一致',
    numNoCell: (why) => `快照里找不到这一格：${why}`,
    numCellDiffers: '引用的值与快照里那一格的值不一致',
    /** cited 已经加好引号（空值写「空值（NULL）」）；expected 和引用的值写法一样时不写（文字取值） */
    numShownDiffers: (shown, cited, expected) => `正文显示「${shown}」，引用的值是${cited}`
      + (expected == null ? '' : `，按显示规则应显示为「${expected}」`),
    numShownUnknown: (why) => `无法核对显示文字：${why}`,
    shownFormat: (fmt) => `显示格式「${fmt}」无法识别`,
    shownConv: (conv) => `换算「${conv}」无法识别`,
    shownTextConv: (conv) => `文字不能换算为「${conv}」`,
    shownRatioScale: (conv) => `比率不能换算为「${conv}」`,
    shownPctUnit: '单位已是百分号，不能再换算为百分数',
    shownDecimals: (d) => `小数位「${d}」无法识别`,
    shownZero: '按显示规则会显示为 0',
    shownTooLong: '数值位数超出可显示的范围',
    shownNotFinite: '不是有限的数值',
    shownInexact: '数值位数过多，本页无法精确核对',
    shownUnit: '口径卡记下的单位不是文字',
    shownShape: '这个值是结构化数据，本页无法按系统的显示规则核对',
    shownDigits: '数字里有全角或其他文字的数字，本页无法按系统的显示规则核对',
    shownDisplay: (v) => `报告文档记下的单元格显示规则版本「${v}」无法识别`,
    shownOld: '这个浏览器版本较旧，请用新版浏览器打开本文件',
    shownBroken: '本页按系统的显示规则处理这个值时出错',
    /** 正文的排法（checkLayout）：显示文字核对过了，读起来却不是这个值 */
    numGlued: '正文里紧挨着这个数的字改变了读者看到的数：前后紧挨着的数字、负号或小数点会和它连成另一个数',
    shownBlurred: '紧挨着的正文里有看不见或在不同设备上显示不同的字，读起来可能和核对过的不是同一个值',
    shownTurned: '这一句里有从右往左书写的文字或改变文字方向的控制字符，显示出来的顺序可能和核对过的不一样',
    shownStruck: '这个数画着删除线，或者紧挨着画了删除线的文字，读的人可能以为它已作废、以旁边的数为准',
    shownScaled: '后面接着量级单位（万、亿、千、百、%、‰、k、M、B 这类）。系统显示这个数时不带它，读起来的数可能差了量级',
    shownTimes: '后面紧接着乘号或乘方和数字（×10⁴、*10^4 这类），读起来的数可能差了量级',
    shownDash: '前面紧挨着一道像负号的短横，读起来可能是负数',
    shownLookalike: '紧挨着长得像数字的字母或符号（l、I、O、上标数字、圈码这类），读起来可能多出一位',
    shownOdd: '紧挨着这个数的字不在常见写法里，无法确认读者看到的数',
    numNotShown: '正文里没有显示这一处：导出时不会出现这种情况，文件可能被改过',
    numAria: (kind, text, result) => `${kind === 'value' ? '取值' : '数字'} ${text}，${result}，查看出处`,
    statusOk: (seq, what) => `与第 ${seq} 条事件（${what}）一致`,
    statusMismatch: (claim, seq, what) => `文件里写的是「${claim}」，与封存范围内最后一条结束类事件（第 ${seq} 条，${what}）不符`,
    statusLegacy: '早期运行在结束事件落库之前封存，结束事件不在封存范围内，运行结果无法核对',
    statusNoEvent: '封存范围内没有运行结束、失败或取消的事件，运行结果无法核对',
    statusContinued: (claim, seq, what) => `文件里写的是「${claim}」，但封存范围内第 ${seq} 条事件（${what}）之后运行又继续执行，`
      + '它不是运行的结束，范围内也没有更晚的结束事件可以印证',
    classOk: (seq) => `与第 ${seq} 条事件（运行开始）记下的运行参数一致`,
    classMismatch: (claim, seq) => `文件里写的是「${claim}」，与第 ${seq} 条事件（运行开始）记下的运行参数不符`,
    classLegacy: (seq) => `第 ${seq} 条事件（运行开始）出自早期运行，没有记下区分运行类别的参数，运行类别无法核对`,
    classUnchecked: (seq) => `第 ${seq} 条事件（运行开始）的内容没有通过核对，运行类别无法核对`,
    classAbsent: (seq) => `第 ${seq} 条事件（运行开始）的内容未随文件附带：导出时不会出现这种情况，文件可能被改过`,
    classNoEvent: '封存范围内没有运行开始事件，运行类别无法核对',
    tierOk: (seq) => `与第 ${seq} 条事件（出具判定）一致`,
    tierMismatch: (claim, seq) => `文件里写的是「${claim}」，与封存范围内最后一条出具判定（第 ${seq} 条）不符`,
    tierNoEvent: (claim) => `文件里写着出具档位「${claim}」，封存范围内却没有出具判定`,
    tierUnchecked: (seq) => `第 ${seq} 条事件（出具判定）的内容没有通过核对，出具档位无法核对`,
    tierAbsent: (seq) => `第 ${seq} 条事件（出具判定）的内容未随文件附带：导出时不会出现这种情况，文件可能被改过`,
    metaNeedsManifest: '封存清单没有通过核对，事件的类型和序号不可信，无法对照',
    dResult: '核对结果', dSource: '出处', dNode: '来源节点', dCaliber: '口径', dCited: '引用的值', dCard: '口径卡里的值',
    dCell: '快照里的值', dShown: '正文显示', dShownSame: '与按显示规则渲染的结果一致', dExpected: '按显示规则应为',
    dArtifact: '所在工件', dReport: '所在报告',
    dSql: 'SQL', dRow: (row) => `快照第 ${row + 1} 行`,
    nullValue: '空值（NULL）',
    appendedNote: '这些事件追加在封存之后，不在封存范围内，不参与清单核对，也不能当作证据。',
    appendedTag: '封存之后追加，不在封存范围内',
    shaSubtle: 'SHA-256 由浏览器内置实现计算（crypto.subtle）',
    shaJs: 'SHA-256 由本页内置的实现计算（浏览器未提供 crypto.subtle），结果与浏览器内置实现一致',
    renderNote: '正文里带出处的数字和取值，本页按系统的显示规则（千分位、小数位、单位、万 / 亿换算、百分数，文字压成一行）'
      + '把口径卡或快照里的值（运行输入按引用的值）重新渲染一遍，与显示的文字逐字比对；核对通过的字在正文里原样显示，'
      + '紧挨着它写着数字、负号或小数点的判不通过；画着删除线、后面接着量级单位、紧挨着像负号的短横、长得像数字的字母、'
      + '看不见的字或其他不常见的字，或者同一句里有改变文字方向的字，判无法核对。有一项无法核对，总览就不是绿色的通过。',
    schema: (s) => `文件格式 ${s}`,
  }

  // ------------------------------------------------------------------ SHA-256

  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ])

  /** 纯 JS 的 SHA-256（FIPS 180-4）：字节 → 64 位小写十六进制 */
  function sha256js(bytes) {
    const n = bytes.length
    const padded = new Uint8Array(Math.ceil((n + 9) / 64) * 64)
    padded.set(bytes)
    padded[n] = 0x80
    const view = new DataView(padded.buffer)
    // 消息长度按 64 位大端写在最后：位数可能超过 2^32，拆成高低两半
    view.setUint32(padded.length - 8, Math.floor(n / 0x20000000))
    view.setUint32(padded.length - 4, (n % 0x20000000) * 8)
    const H = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19])
    const W = new Uint32Array(64)
    for (let off = 0; off < padded.length; off += 64) {
      for (let i = 0; i < 16; i++) W[i] = view.getUint32(off + i * 4)
      for (let i = 16; i < 64; i++) {
        const a = W[i - 15]
        const b = W[i - 2]
        const s0 = ((a >>> 7) | (a << 25)) ^ ((a >>> 18) | (a << 14)) ^ (a >>> 3)
        const s1 = ((b >>> 17) | (b << 15)) ^ ((b >>> 19) | (b << 13)) ^ (b >>> 10)
        W[i] = W[i - 16] + s0 + W[i - 7] + s1
      }
      let a = H[0]; let b = H[1]; let c = H[2]; let d = H[3]
      let e = H[4]; let f = H[5]; let g = H[6]; let h = H[7]
      for (let i = 0; i < 64; i++) {
        const S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7))
        const ch = (e & f) ^ (~e & g)
        const t1 = (h + S1 + ch + K[i] + W[i]) | 0
        const S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10))
        const maj = (a & b) ^ (a & c) ^ (b & c)
        const t2 = (S0 + maj) | 0
        h = g; g = f; f = e; e = (d + t1) | 0
        d = c; c = b; b = a; a = (t1 + t2) | 0
      }
      H[0] += a; H[1] += b; H[2] += c; H[3] += d; H[4] += e; H[5] += f; H[6] += g; H[7] += h
    }
    let out = ''
    for (let i = 0; i < 8; i++) out += H[i].toString(16).padStart(8, '0')
    return out
  }

  const encoder = new TextEncoder()
  const decoder = new TextDecoder('utf-8', { fatal: true })
  const utf8 = (text) => encoder.encode(text)
  const bytesHex = (buf) => Array.from(new Uint8Array(buf), (x) => x.toString(16).padStart(2, '0')).join('')

  // 标准测试向量（空串、abc、跨块的 448 位串、中文、1000 个 a）
  const VECTORS = [
    ['', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'],
    ['abc', 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'],
    ['abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq', '248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1'],
    ['可验证报告', 'e49666cf360bf3171c8e7d9645148517861e1966a3b4c6b6785c1cd3a2014a13'],
    ['a'.repeat(1000), '41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3'],
  ]

  /** 用哪一种 SHA-256：{ kind: 'subtle' | 'js', digest(字节) → Promise<十六进制> }；纯 JS 实现自检不过返回 null */
  async function pickDigest() {
    if (!VECTORS.every(([text, sum]) => sha256js(utf8(text)) === sum)) return null
    const subtle = typeof crypto !== 'undefined' && crypto ? crypto.subtle : undefined
    if (subtle && typeof subtle.digest === 'function') {
      try {
        if (bytesHex(await subtle.digest('SHA-256', utf8('abc'))) === VECTORS[1][1]) {
          return { kind: 'subtle', digest: async (bytes) => bytesHex(await subtle.digest('SHA-256', bytes)) }
        }
      } catch (e) {
        // 非安全上下文、打开本地文件时可能拿不到或者用不了：退回内置实现
      }
    }
    return { kind: 'js', digest: async (bytes) => sha256js(bytes) }
  }

  function fromBase64(text) {
    const bin = atob(text)
    const out = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
    return out
  }

  const NONFINITE = { NaN: NaN, Infinity: Infinity, '-Infinity': -Infinity }

  /**
   * JSON.parse，另认 Python 的 json.dumps 写出的裸 NaN、Infinity、-Infinity（JSON 标准不认）。规范字符串和工件文件
   * 都是 json.dumps 写的，查询结果里浮点的 NaN 会原样落进去，标准的 JSON.parse 一读就报错，没被改过的文件也会被判成
   * 不通过。字符串外面的这几个词先换成占位的字符串再解析，解析时换回对应的数：判等和服务端一样，NaN 和谁都不相等。
   * 只用来读内容，哈希照旧按原字节算
   */
  function parseJson(text) {
    try {
      return JSON.parse(text)
    } catch (e) {
      // 下面按宽松的认法再试一次
    }
    let mark = '\uE000nonfinite:'
    while (text.includes(mark)) mark += '\uE000'
    let out = ''
    let from = 0
    let inString = false
    for (let i = 0; i < text.length; i++) {
      const c = text[i]
      if (inString) {
        if (c === '\\') i++
        else if (c === '"') inString = false
        continue
      }
      if (c === '"') {
        inString = true
        continue
      }
      const word = c === '-' || c === 'I' || c === 'N' ? ['-Infinity', 'Infinity', 'NaN'].find((w) => text.startsWith(w, i)) : null
      if (word) {
        out += text.slice(from, i) + JSON.stringify(mark + word)
        i += word.length - 1
        from = i + 1
      }
    }
    out += text.slice(from)
    return JSON.parse(out, (_, v) => (typeof v === 'string' && v.startsWith(mark) && v.slice(mark.length) in NONFINITE
      ? NONFINITE[v.slice(mark.length)] : v))
  }

  // ------------------------------------------------------------------ 判等：照搬 engine/expressions.py

  const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v)
  const own = (obj, key) => (isObj(obj) && typeof key === 'string' && Object.prototype.hasOwnProperty.call(obj, key)
    ? obj[key] : undefined)

  // 数据源把 DECIMAL 落成数字文本（"45678.50"、"45678"、"1E-7"）：按数算；带前导零的是编号，照文本（同 _NUMERIC_TEXT）
  const NUMERIC_TEXT = /^\s*-?(?:0|[1-9]\d*)(?:\.\d+)?(?:E[+-]?\d+)?\s*$/

  /** cell_value：快照里的一格 → 参与比对的值。kind 是查询时记下的列类型，text 列原样 */
  function cellValue(raw, kind) {
    if (kind === 'text') return raw
    return typeof raw === 'string' && NUMERIC_TEXT.test(raw) ? Number(raw) : raw
  }

  /** math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9) */
  function isClose(a, b) {
    if (a === b) return true
    if (!Number.isFinite(a) || !Number.isFinite(b)) return false
    return Math.abs(a - b) <= Math.max(1e-9 * Math.max(Math.abs(a), Math.abs(b)), 1e-9)
  }

  const textOf = (v) => (typeof v === 'string' ? v : isObj(v) || Array.isArray(v) ? JSON.stringify(v) : String(v))

  /** same_value：数按数比（数字文本也算数），布尔按 Python 的规矩（True == 1），其余压成一行按文本比 */
  function sameValue(model, truth) {
    if (model == null || truth == null) return model == null && truth == null
    const m = cellValue(model)
    const t = cellValue(truth)
    if (typeof m === 'boolean' || typeof t === 'boolean') {
      const num = (x) => (typeof x === 'boolean' ? Number(x) : typeof x === 'number' ? x : NaN)
      return num(m) === num(t)
    }
    if (typeof m === 'number' && typeof t === 'number') return isClose(m, t)
    return textOf(m).replace(/\s+/g, ' ').trim() === textOf(t).replace(/\s+/g, ' ').trim()
  }

  /** locate_cell：(那一格的原值, 列名, 这一格在行里的下标或键)。列号换成列名；行是对象时按键取 */
  function locateCell(table, row, column) {
    if (!isObj(table) || !Array.isArray(table.rows)) throw new Error('快照里没有表格数据')
    const rows = table.rows
    if (!Number.isInteger(row)) throw new Error(`行号需为从 0 开始的整数，当前为「${row}」`)
    if (!rows.length) throw new Error('查询结果为空（0 行）')
    if (row < 0 || row >= rows.length) throw new Error(`请求第 ${row} 行，但查询结果只有 ${rows.length} 行（行号从 0 开始）`)
    const record = rows[row]
    let columns = (Array.isArray(table.columns) ? table.columns : []).map(String)
    if (isObj(record) && !columns.length) columns = Object.keys(record)
    let name = column
    if (Number.isInteger(column)) {
      if (column < 0 || column >= columns.length) throw new Error(`请求第 ${column} 列，但查询结果只有 ${columns.length} 列`)
      name = columns[column]
    } else if (typeof column !== 'string') {
      throw new Error(`列需填写列名或从 0 开始的列号，当前为「${column}」`)
    }
    if (isObj(record)) {
      if (!Object.prototype.hasOwnProperty.call(record, name)) throw new Error(`没有列「${name}」`)
      return [record[name], name, name]
    }
    const index = columns.indexOf(name)
    if (index < 0) throw new Error(`没有列「${name}」`)
    if (!Array.isArray(record) || index >= record.length) throw new Error(`第 ${row} 行中没有「${name}」对应的单元格`)
    return [record[index], name, index]
  }

  function columnKind(table, column) {
    const kind = own(own(table, 'column_types'), column)
    return typeof kind === 'string' ? kind : null
  }

  // ------------------------------------------------------------------ 显示规则：照搬 engine/evidence.py
  //
  // 正文里带出处的数字和取值，字是服务端按引用的值渲染的（render_number / render_cell）。这里按同样的规则把口径卡、快照里
  // 的值再渲染一遍，和正文显示的字逐字比对：只改显示文字、重算全部哈希的伪造（74,613.65元 改成 84,613.65元、华南改成
  // 华东）在这里露馅。数照 Python 的 Decimal 算：系数用 BigInt 存，位数照原样记，四舍五入是 ROUND_HALF_UP（一半远离 0）；
  // 浮点取 repr 的那串字（和 JS 的 String 同一个最短写法），整数取 JSON 原文（超过 2^53 的，JSON.parse 读成 double 会丢位）。
  // 服务端渲染不出来的（引用就算解析不了，正常导出不会出现）和本页没有移植的写法一律抛 Unshown：核对结果是「无法核对」，
  // 不是通过

  /** 显示文字核对不了：不认识的格式、本页没有移植的写法 */
  class Unshown extends Error {}

  const FORMATS = ['plain', 'thousands', 'percent_of_ratio']
  const MISSING = '—'
  /** 换算成万、亿：小数点左移几位 */
  const CONV_SCALE = { '万': 4, '亿': 8 }
  const CONV_PLACES = /^\.([0-6])$/
  const MAX_PLACES = 100
  const MAX_DIGITS = 1000
  /** Python 的 int() 最多认这么多位：更长的整数文本 cell_value 照文本 */
  const INT_DIGITS = 4300
  /** Python 的空白（str.isspace()，re 的 \s 同一套）：比 JS 的 \s 多 \x1c–\x1f、\x85，少 U+FEFF */
  const PY_SPACE = '\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000'
  const PY_RUNS = new RegExp(`[${PY_SPACE}]+`, 'g')
  const PY_EDGES = new RegExp(`^[${PY_SPACE}]+|[${PY_SPACE}]+$`, 'g')
  // 较早的浏览器没有 BigInt、不认 \p{…}：只是显示文字核对不了（判无法核对），整段脚本不能因此出错，别的照常核对
  const BIG = typeof BigInt === 'function' ? BigInt : null
  const unicode = (source) => {
    try {
      return new RegExp(source, 'u')
    } catch (e) {
      return null
    }
  }
  // Python 的 \d 认所有 Unicode 十进制数字：_NUMERIC_TEXT、_ISO_DATETIME 照这个认
  const PY_NUMERIC = unicode(`^[${PY_SPACE}]*-?(?:0|[1-9]\\p{Nd}*)(?:\\.\\p{Nd}+)?(?:E[+-]?\\p{Nd}+)?[${PY_SPACE}]*$`)
  const ISO_DATETIME = unicode('^(\\p{Nd}{4}-\\p{Nd}{2}-\\p{Nd}{2})T(\\p{Nd}{2}:\\p{Nd}{2}(?::\\p{Nd}{2}(?:\\.\\p{Nd}+)?)?'
    + '(?:Z|[+-]\\p{Nd}{2}:?\\p{Nd}{2})?)$')

  const pyStrip = (text) => text.replace(PY_EDGES, '')
  /** _clean：文本值压成一行 */
  const pyClean = (text) => pyStrip(text.replace(PY_RUNS, ' '))

  /** 十进制数 { neg, coef, exp }：(−1)^neg × coef × 10^exp，coef 是非负的 BigInt。text 是 JSON 的数、数字文本或 String(数) */
  function decimalOf(text) {
    if (!BIG) throw new Unshown(T.shownOld)
    const m = /^(-?)(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(text)
    if (!m) throw new Unshown(T.shownBroken)
    const frac = m[3] || ''
    return { neg: m[1] === '-', coef: BIG(m[2] + frac), exp: Number(m[4] || 0) - frac.length }
  }

  const isZero = (d) => d.coef === BIG(0)
  const width = (coef) => coef.toString().length

  /** Decimal.adjusted()：最高位的指数 */
  const adjusted = (d) => (isZero(d) ? d.exp : width(d.coef) - 1 + d.exp)

  /** Decimal.quantize：舍入到 10^exp 这一位，ROUND_HALF_UP（一半远离 0） */
  function quantize(d, exp) {
    if (d.exp >= exp) return { neg: d.neg, coef: d.coef * BIG(10) ** BIG(d.exp - exp), exp }
    const shift = exp - d.exp
    // 要舍掉的位数比系数还多：不到一半，舍成 0（不必真去算 10 的几万次方）
    if (shift > width(d.coef)) return { neg: d.neg, coef: BIG(0), exp }
    const unit = BIG(10) ** BIG(shift)
    const up = (d.coef % unit) * BIG(2) >= unit ? BIG(1) : BIG(0)
    return { neg: d.neg, coef: d.coef / unit + up, exp }
  }

  /** Decimal.normalize：去掉尾零，0 写成 0E0 */
  function normalize(d) {
    if (isZero(d)) return { neg: d.neg, coef: d.coef, exp: 0 }
    let { coef, exp } = d
    while (coef % BIG(10) === BIG(0)) {
      coef /= BIG(10)
      exp++
    }
    return { neg: d.neg, coef, exp }
  }

  /** format(Decimal, ',f')：定点写法，group 时整数部分千分位。0 不带负号（服务端 copy_abs），正指数的 0 写成「0」 */
  function fixed(d, group) {
    const exp = isZero(d) && d.exp > 0 ? 0 : d.exp
    let digits = d.coef.toString()
    let int = digits
    let frac = ''
    if (exp > 0) int = digits + '0'.repeat(exp)
    else if (exp < 0) {
      digits = digits.padStart(1 - exp, '0')
      int = digits.slice(0, digits.length + exp)
      frac = digits.slice(digits.length + exp)
    }
    if (group) {
      let out = ''
      for (let end = int.length; end > 0; end -= 3) out = int.slice(Math.max(0, end - 3), end) + (out ? `,${out}` : '')
      int = out
    }
    return (d.neg && !isZero(d) ? '-' : '') + int + (frac ? `.${frac}` : '')
  }

  /** _digits：按小数位四舍五入后写出来。不是 0 的值写出来成了 0，服务端不写（引用算解析不了） */
  function digitsOf(d, places, group, strip) {
    const q = quantize(d, places == null ? -10 : -places)
    if (isZero(q) && !isZero(d)) throw new Unshown(T.shownZero)
    return fixed(places == null || strip ? normalize(q) : q, group)
  }

  const shifted = (d, k) => ({ neg: d.neg, coef: d.coef, exp: d.exp + k })

  /** _render_numeric：万 / 亿留两位、去尾零，单位接在后面；pct、比率乘 100 加 %（不接单位）；int、.N 改小数位 */
  function renderNumeric(d, unit, decimals, fmt, conv) {
    if (adjusted(d) >= MAX_DIGITS) throw new Unshown(T.shownTooLong)
    const ratio = fmt === 'percent_of_ratio'
    const group = fmt !== 'plain'
    if (typeof conv === 'string' && Object.prototype.hasOwnProperty.call(CONV_SCALE, conv)) {
      if (ratio) throw new Unshown(T.shownRatioScale(conv))
      return digitsOf(shifted(d, -CONV_SCALE[conv]), 2, true, true) + conv + unit
    }
    if (conv === 'pct') {
      if (unit.includes('%') || unit.includes('％')) throw new Unshown(T.shownPctUnit)
      return `${digitsOf(shifted(d, 2), decimals != null ? Math.max(decimals - 2, 0) : 1, group, false)}%`
    }
    let places
    if (conv === 'int') places = 0
    else if (typeof conv === 'string' && CONV_PLACES.test(conv)) places = Number(conv.slice(1))
    else if (conv) throw new Unshown(T.shownConv(conv))
    else places = decimals == null ? null : ratio ? Math.max(decimals - 2, 0) : decimals
    if (ratio) return `${digitsOf(shifted(d, 2), places, group, false)}%`
    return digitsOf(d, places, group, false) + unit
  }

  /** _decimal：JSON 里的数 → 十进制数。token 是它在 JSON 里的原文：整数按原文取精确值，浮点按 repr 的那串字 */
  function pyDecimal(value, token) {
    if (typeof token === 'string' && /^-?\d+$/.test(token)) return decimalOf(token)
    if (!Number.isFinite(value)) throw new Unshown(T.shownNotFinite)
    // 没有原文（找不到它在文件里的位置）：浮点和 2^53 以内的整数，String 就是 Python 的写法；更大的整数不知道原来的位
    if (typeof token !== 'string' && Number.isInteger(value) && !Number.isSafeInteger(value)) {
      throw new Unshown(T.shownInexact)
    }
    return decimalOf(String(value))
  }

  /**
   * render_number：口径卡里的指标按卡上的格式渲染（单元格也走这里，格式是千分位）。token 是值在 JSON 里的原文（数才有），
   * decimalsToken 是小数位的原文：写成 2.0 的在 Python 里是浮点，服务端不认
   */
  function renderNumber(value, token, { unit = '', decimals = null, decimalsToken = null, fmt = null, conv = null } = {}) {
    const format = fmt || 'thousands'
    if (!FORMATS.includes(format)) throw new Unshown(T.shownFormat(format))
    if (value == null) return MISSING
    if (typeof value !== 'number') {
      if (conv) throw new Unshown(T.shownTextConv(conv))
      if (typeof value === 'boolean') return value ? '是' : '否'
      if (typeof value === 'string') return pyClean(value)
      throw new Unshown(T.shownShape)
    }
    if (decimals != null && (typeof decimals !== 'number' || !Number.isInteger(decimals) || Math.abs(decimals) > MAX_PLACES
      || (typeof decimalsToken === 'string' && !/^-?\d+$/.test(decimalsToken)))) {
      throw new Unshown(T.shownDecimals(decimalsToken ?? decimals))
    }
    return renderNumeric(pyDecimal(value, token), unit, decimals, format, conv)
  }

  /**
   * render_cell(shown(raw))：快照里的一格。kind 是查询时记下的列类型，display 是报告文档记下的单元格显示规则版本
   * （2 版起 ISO 日期时间的 T 换成空格）。DECIMAL 落成的数字文本按数渲染、不经 float；文字压成一行，竖线换成 ¦
   */
  function renderCell(raw, token, conv, kind, display) {
    if (display !== 1 && display !== 2) throw new Unshown(T.shownDisplay(display))
    if (!PY_NUMERIC || !ISO_DATETIME) throw new Unshown(T.shownOld)
    let value = raw
    if (display >= 2 && typeof raw === 'string') {
      const m = ISO_DATETIME.exec(pyStrip(raw))
      if (m) value = `${m[1]} ${m[2]}`
    }
    if (typeof value === 'string' && kind !== 'text' && PY_NUMERIC.test(value)) {
      const text = pyStrip(value)
      // Python 把全角、阿拉伯文这些数字也当数（int('1٢٣') 是 123），本页只照 ASCII 算
      if (/[^\x00-\x7f]/.test(text)) throw new Unshown(T.shownDigits)
      if (!/^-?\d+$/.test(text) || text.replace('-', '').length <= INT_DIGITS) {
        return renderNumeric(decimalOf(text), '', null, 'thousands', conv)
      }
    }
    // 对象、列表：服务端按 Python 的 json.dumps 写（键的先后、浮点的写法），本页没有移植
    if (isObj(value) || Array.isArray(value)) throw new Unshown(T.shownShape)
    return renderNumber(value, token, { conv }).replace(/\|/g, '¦')
  }

  /**
   * JSON 文本里每个数的原文。交回和 JSON.parse 同形状的一棵树：对象是 Map（键重复时后面的算，同 JSON.parse），数组是
   * 数组，数是它的原文，别的值是 null。Python 写出的裸 NaN、Infinity、-Infinity 也认（同 parseJson）。读不了抛错
   */
  function numberTokens(text) {
    const NUMBER = /-?(?:Infinity|(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)|NaN/y
    const WORD = /true|false|null/y
    let at = 0
    const fail = () => {
      throw new Error('JSON 格式有误')
    }
    const skip = () => {
      while (at < text.length && ' \t\n\r'.includes(text[at])) at++
    }
    const string = () => {
      const from = at++
      while (at < text.length && text[at] !== '"') at += text[at] === '\\' ? 2 : 1
      if (at >= text.length) fail()
      at++
      return text.slice(from, at)
    }
    const value = () => {
      skip()
      const c = text[at]
      if (c === '{' || c === '[') {
        const object = c === '{'
        const close = object ? '}' : ']'
        const out = object ? new Map() : []
        at++
        skip()
        if (text[at] === close) {
          at++
          return out
        }
        for (;;) {
          if (object) {
            skip()
            if (text[at] !== '"') fail()
            const key = JSON.parse(string())
            skip()
            if (text[at++] !== ':') fail()
            out.set(key, value())
          } else {
            out.push(value())
          }
          skip()
          const next = text[at++]
          if (next === close) return out
          if (next !== ',') fail()
        }
      }
      if (c === '"') {
        string()
        return null
      }
      for (const re of [NUMBER, WORD]) {
        re.lastIndex = at
        const m = re.exec(text)
        if (m) {
          at = re.lastIndex
          return re === NUMBER ? m[0] : null
        }
      }
      return fail()
    }
    const tree = value()
    skip()
    if (at !== text.length) fail()
    return tree
  }

  /** numberTokens 的树里一处的原文：path 是一串键和下标。不是数、找不到都是 null */
  function tokenAt(tree, path) {
    let node = tree
    for (const key of path) {
      if (node instanceof Map) node = node.has(key) ? node.get(key) : null
      else if (Array.isArray(node) && Number.isInteger(key)) node = key >= 0 && key < node.length ? node[key] : null
      else return null
    }
    return typeof node === 'string' ? node : null
  }

  // ------------------------------------------------------------------ 正文的排法：核对和显示共用
  //
  // 正文怎样排，核对时就怎样算（不碰页面，node 里也算得了）：一个数字、取值在正文里显示成什么字、紧挨着它的是什么字，
  // 都按这一节的函数算；显示时（renderBlock / renderUnit）也走它们，两边不会各算各的。

  // 行内语法：代码、链接（只显示文字，不给链接：页面不连网）、粗体、斜体、删除线，和前端 run/Markdown.tsx 的规则一致。
  // 斜体要求左边不是 *（否则 **粗体** 会被它先拆掉）：前端用后行断言，这里不用（较早的 Safari 不认，构造正则时
  // 就会报错，整页核对跟着失败），匹配到以后自己看左边那个字
  const INLINE = '(`+)([^`]+?)\\1|\\[([^\\]]+)\\]\\(([^)\\s]+)[^)]*\\)|\\*\\*([^*]+?)\\*\\*|__([^_]+?)__'
    + '|\\*([^*\\n]+?)\\*(?!\\*)|~~([^~]+?)~~'
  const BIT = { code: 1, bold: 2, strike: 4, link: 8, em: 16 }
  // 列表项的开头：行首的 -、*、+ 或者序号
  const ITEM_START = /(^|\n)[ \t]*([-*+]|\d+[.)])[ \t]+$/

  /** 整句算一次行内样式：服务端把一句切成了好几个片段，粗体、代码可能跨片段，每个片段取自己那一截 */
  function inlineStyles(text) {
    const hide = new Uint8Array(text.length)
    const style = new Uint8Array(text.length)
    const walk = (from, to, inherited, depth) => {
      const part = text.slice(from, to)
      const re = new RegExp(INLINE, 'g')
      let last = 0
      for (let m = re.exec(part); m; m = re.exec(part)) {
        const [whole, ticks, code, linkText, , bold1, bold2, italic, strike] = m
        if (italic !== undefined && m.index > 0 && part[m.index - 1] === '*') {
          // 左边紧挨着 *：不算斜体（前端后行断言排除的情形），从下一个字接着找
          re.lastIndex = m.index + 1
          continue
        }
        for (let i = last; i < m.index; i++) style[from + i] = inherited
        const inner = code ?? linkText ?? bold1 ?? bold2 ?? italic ?? strike ?? ''
        const base = from + m.index
        const off = code !== undefined ? ticks.length : whole.indexOf(inner)
        for (let i = 0; i < whole.length; i++) hide[base + i] = 1
        for (let i = 0; i < inner.length; i++) hide[base + off + i] = 0
        const bit = code !== undefined ? BIT.code : linkText !== undefined ? BIT.link
          : bold1 !== undefined || bold2 !== undefined ? BIT.bold : italic !== undefined ? BIT.em : BIT.strike
        if (code !== undefined || linkText !== undefined || depth >= 3) {
          for (let i = 0; i < inner.length; i++) style[base + off + i] = inherited | bit
        } else {
          walk(base + off, base + off + inner.length, inherited | bit, depth + 1)
        }
        last = m.index + whole.length
      }
      for (let i = last; i < part.length; i++) style[from + i] = inherited
    }
    walk(0, text.length, 0, 0)
    return { hide, style }
  }

  // 列表最多显示这么多级缩进（每级 14px）。服务端按原文的缩进算级数（两个空格一级），没有上限：伪造的级数写成十万、
  // 负数，那一项会被挤出屏幕或挤到左边看不见的地方，读的人看不到核对通过的数，页面却照样通过；窄屏上也会横向溢出
  const MAX_DEPTH = 8

  /** 列表的各项：一项从行首写着列表符号或序号的结构片段开始，后面的句子接在这一项里。depth 是显示的缩进级数 */
  function listItems(units) {
    const items = []
    for (const u of units) {
      const lead = Array.isArray(u.segments) ? u.segments[0] : null
      const starts = !items.length || (isObj(lead) && lead.kind === 'structural' && ITEM_START.test(String(lead.text ?? '')))
      const depth = Math.min(Math.max(Math.floor(Number(u.depth)) || 0, 0), MAX_DEPTH)
      if (starts) items.push({ depth, units: [u] })
      else items[items.length - 1].units.push(u)
    }
    return items
  }

  /**
   * 表格：{ cells（位置 → 句子，一格可能被分成几句，按位置归拢）, rows（表身各行的行号，表头是 -1，另算）, cols（有格的
   * 列号，从小到大）}。服务端每行从第 0 列起一格不落，列号连着；只排有格的列：伪造的列号写成十万，那一格紧接着前一列
   * 显示，不会被几万个空列挤到表格右边看不见的地方（也不会建出几十万个空格、拖垮页面）
   */
  function tableCells(units) {
    const cells = new Map()
    const rows = []
    const cols = new Set()
    for (const u of units) {
      const loc = isObj(u.loc) ? u.loc : { row: 0, col: 0 }
      push(cells, `${loc.row}:${loc.col}`, u)
      const col = Number(loc.col)
      if (Number.isInteger(col) && col >= 0) cols.add(col)
      if (loc.row >= 0 && !rows.includes(loc.row)) rows.push(loc.row)
    }
    rows.sort((a, b) => a - b)
    return { cells, rows, cols: [...cols].sort((a, b) => a - b) }
  }

  /**
   * 一块里连着显示的几组句子：段落、标题、引用整块一组，列表每项一组，表格每格一组（只有表头和表身各行各列上的格
   * 显示出来）。分隔线不显示句子，代码块按原文显示、不标数字：都交回空的
   */
  function flowsOf(block) {
    const units = Array.isArray(block.units) ? block.units.filter(isObj) : []
    switch (block.type) {
      case 'hr':
      case 'code':
        return []
      case 'list':
        return listItems(units).map((item) => item.units)
      case 'table': {
        const { cells, rows, cols } = tableCells(units)
        return [-1, ...rows].flatMap((r) => cols.map((c) => cells.get(`${r}:${c}`) || []))
      }
      default:
        return [units]
    }
  }

  const plans = new WeakMap()

  /**
   * 一句怎样显示：{ segs（结构片段以外的片段）, text（连起来的字）, styles（行内样式）, at（每个片段在 text 里的起止）,
   * raw }。数字、取值片段里有字会被行内标记藏起来时（伪造的链接把它塞进不显示的链接地址、取值里本身带 * 这类），整句
   * 按未排版的原文显示（raw）：核对过的字一个不少地露出来，读的人不会只看到旁边伪造的那个数。核对和显示各算一次会是
   * 同一个结果，算过的记下来
   */
  function unitPlan(unit) {
    let plan = plans.get(unit)
    if (plan) return plan
    const segs = (Array.isArray(unit.segments) ? unit.segments : []).filter((s) => isObj(s) && s.kind !== 'structural')
    const at = []
    let text = ''
    for (const seg of segs) {
      const piece = String(seg.text ?? '')
      at.push([text.length, text.length + piece.length])
      text += piece
    }
    let styles = inlineStyles(text)
    const raw = segs.some((seg, i) => (seg.kind === 'number' || seg.kind === 'value')
      && styles.hide.subarray(at[i][0], at[i][1]).some((x) => x === 1))
    if (raw) styles = { hide: new Uint8Array(text.length), style: new Uint8Array(text.length) }
    plan = { segs, text, styles, at, raw }
    plans.set(unit, plan)
    return plan
  }

  // 紧挨着核对通过的数字、取值的字，按码点分几类（kindOf）：
  // - digit：数字。Python 的 \d 认的全都算（全角的、数学字母区的、阿拉伯文的也是）；
  // - minus：负号。只认真当负号用的三个：半角的连字符减号、数学减号「−」、全角的「－」；
  // - dash：长得像负号、平时却不当负号用的短横：连字符、不断行连字符、数字宽的横线、en dash（「–」，最常见的负号替身，
  //   页面上和「-」几乎一样）、小号的连字符减号、上下标负号、修饰字母负号、粗减号、别的文字里的连字符。不判不通过：紧挨着
  //   数字开头、前面又不是字母数字时，读起来可能是负数，判无法核对；前面是数字的是区间（「2025–2026」）。破折号（——、—、
  //   ―，小号的）、波浪线、汉字「一」、假名长音、制表符的横线不在这里，是正文里的普通写法（「运费统一10元」「合计——7,036.50元」），
  //   在常见写法的名单里（COMMON）；
  // - sep：半角逗号、句点和长得一样的窄标点（低位单引号、单点前导符、小号的逗号句点这类，取自易混字符表）：「1,」后面
  //   接着数字也算紧挨着。全角的「，」「．」不算：字宽，中文里两个数之间本来就常用「，」隔开；
  // - unit：量级单位（万、亿、千、百、十、兆，繁体、大写的萬、億、拾、佰、仟，%、‰、‱，k、K、M、B，当「万」用的 w、W，
  //   全角写法也算）：接在数字后面，读者看到的数差了量级。系统显示一个数时单位、万 / 亿换算都在核对过的那段字里，不会另接
  //   在后面，判无法核对。英文的量级写法（T、G、m、b，bn、mn、tn，thousand、million、billion、trillion）另按紧接着的
  //   整串字母认（MAGNITUDE），「ms」「GB」这类别的单位不算；
  // - look：长得像数字、却不是数字的字：拉丁字母 l、I、O、o，竖线和长得像它们的字（|、丨、竖排的破折号、制表符的竖线、
  //   别的文字里的 I、O，全角的也算），上下标数字、圈码、罗马数字、汉字「〇」、空心圆这类数字符号。紧挨着数字，读起来
  //   可能多出一位（「l74,613.65元」），判无法核对；
  // - skip：显示时不占位置的字（零宽空格、连接符、变体选择符、韩文填充符这类格式字符和默认不显示的字），找紧挨着的数字、
  //   负号时隔着它们照样算紧挨着；它自己紧挨着数字时不在常见写法的名单里，判无法核对；
  // - blur：显示成什么说不准的字：控制字符（制表、换行这几个空白除外）、未分配和私用区的字、孤立的代理项、组合附加符号
  //   （叠在前一个字上：叠在空格上能在数字前面画出一道横线）、可选连字符（在它那里换行时显示成连字符）、发丝空格、对象
  //   替换符，以及在 Chrome 的常用字体里量出来几乎不占宽度的几个字。各家浏览器、字体把它们显示成数字、横线还是什么都
  //   不显示说不准：隔着它们照样往外找数字、负号；找不到的也判无法核对，原因另写；
  // - other：别的字。
  // 紧挨着数字头尾的那一个字（不越过 skip、blur）还要在常见写法的名单里（COMMON：汉字、中文标点、半角和全角空格、段内
  // 的换行（页面上显示成一个空格）、ASCII 字母和常用的半角标点、算式里的几个符号、金额前面的「￥」「¥」、上一轮放行的破折号
  // 写法），不在名单里的（别的文字系统的字母、制表符和各种宽度的空格、零宽字符、全角拉丁字母、表情、弯撇号、偏旁形的汉字
  // 这类）判无法核对：伪造的写法换一个易混字、换一种空白就能绕开上面任何一条按字认的规则，名单以外的一律说不准。
  // 改变文字方向的控制字符（BIDI）和从右往左书写的文字（RTL：希伯来文、阿拉伯文这类）另算：它们能把一位数挪到数字的
  // 另一边显示（逻辑上在数字后面的「5」显示在数字前面），按字的先后看不出来，同一句里出现就判无法核对
  const DIGIT = unicode('^\\p{Nd}$') || /^[0-9\uff10-\uff19]$/
  const MINUS = /^[-\u2212\uff0d]$/
  // \p{Pd} 里去掉负号和正文里的破折号、波浪线（这几个另算），再加上码点不在 \p{Pd} 里的减号类符号
  const DASH = unicode('^(?![-\\u2014\\u2015\\u2e3a\\u2e3b\\u301c\\u3030\\ufe31\\ufe32\\ufe58\\uff0d])'
    + '[\\p{Pd}\\u02d7\\u207b\\u208b\\u2043\\u2796]$')
    || /^[\u058a\u1400\u1806\u2010-\u2013\u2e17\u2e1a\u2e40\u30a0\ufe63\u02d7\u207b\u208b\u2043\u2796]$/
  const SEP = unicode('^[.,\\u00b8\\u201a\\u2024\\u2e12\\ua4f8\\ua4f9\\ua60e\\uabec\\ufe50\\ufe52\\u{16f53}\\u{1d16d}]$')
    || /^[.,\u00b8\u201a\u2024\u2e12\ua4f8\ua4f9\ua60e\uabec\ufe50\ufe52]$/
  const UNIT = /^[万亿千百十兆萬億拾佰仟%\uff05\u2030\u2031kKMBwW\uff4b\uff2b\uff2d\uff22\uff57\uff37]$/
  // 量级单位里本身就能接在数后面的那几个汉字：核对过的字以它们结尾（换算成万的「4.57万」），再接一个也差了量级（「4.57万亿」）
  const UNIT_HAN = /^[万亿千百兆萬億]$/
  // 英文的量级写法：紧接着（隔着空白、左括号也算）的整串 ASCII 字母就是它们才算（「45,678.5bn」「45,678.5 billion」），
  // 单个字母只认 T、G、m、b（大小写照写），「ms」「GB」「kg」这类别的单位不算（k、K、M、B 打头的另按 unit 认，照旧）
  const MAGNITUDE = /^(?:bn|mn|tn|thousands?|millions?|billions?|trillions?)$/i
  const MAGNITUDE_LETTER = /^[TGmb]$/
  // 乘号、乘方：后面紧接着数字（「45,678.5×10⁴」「45,678.5*10^4」）读起来是另一个量级
  const TIMES = /^[\u00d7*^]$/
  // 竖线一类：半角、全角的竖线，汉字「丨」，竖排的破折号，制表符的竖线，拉丁、西里尔、希腊字母里长得像 l、I 的；圆圈一类：
  // 西里尔、希腊的 O、o，空心圆
  const LOOK_EXTRA = 'o\\uff4f|\\uff5c\\u4e28\\ufe31\\ufe32\\u2502\\u01c0\\u04c0\\u0406\\u0399\\u041e\\u043e\\u039f\\u03bf\\u25cb'
  const LOOK = unicode(`^[lIO\\uff4c\\uff29\\uff2f${LOOK_EXTRA}\\p{No}\\p{Nl}]$`)
    || new RegExp(`^[lIO\\uff4c\\uff29\\uff2f${LOOK_EXTRA}\\u00b2\\u00b3\\u00b9\\u00bc-\\u00be\\u2070-\\u2079\\u2080-\\u2089`
      + '\\u2150-\\u2189\\u2460-\\u249b\\u24ea-\\u24ff\\u2776-\\u2793\\u3007\\u3021-\\u3029\\u3038-\\u303a]$')
  // 连字符减号前面的字母数字（只认 ASCII：别的文字系统里长得像撇号、点号、竖线的字母不算）：「SKU-12」「A-3」是编号的一部分，
  // 不是负号
  const WORDY = /^[A-Za-z0-9]$/
  // 量级单位前面可以隔着的：各种空白（制表、换行、不同宽度的空格），左括号、左引号（「45,678.5（万元）」读作四亿多）
  const LEAD_IN = unicode('^[\\s\\p{Zs}(\\[{\\uff08\\uff3b\\uff5b\\u3008\\u300a\\u300c\\u300e\\u3010\\u3014\\u3016\\u3018'
    + '\\u301a\\u201c\\u2018]$')
    || /^[\s(\[{\uff08\uff3b\uff5b\u3008\u300a\u300c\u300e\u3010\u3014\u3016\u3018\u301a\u201c\u2018]$/
  // 常见写法的名单（紧挨着数字头尾的那一个字）。汉字只认基本区、扩展 A 和兼容区的表意文字（含「一」），去掉丨、丶、丿这类
  // 当笔画用的字和刂、冫、氵这类偏旁形的字（刂 显示成两道竖线，紧挨着数字读起来像多了「11」；康熙部首、部首补充两个区块
  // 不是 \p{Lo}，本来就不在里面，这里一并写明），扩展 B 区以后的字（𠄌、𠃑 这类笔画形的生僻字）也不收；中文标点按全角的写，
  // 段内的换行在页面上显示成一个空格，收；半角标点里不收撇号和弯撇号（‘’，有的地方拿它们当千分位：1’234）、反斜杠，
  // 货币符号只收金额前面常写的「￥」「¥」；逗号、句点、连字符另看上下文（usual）。破折号、波浪线、假名长音、制表符的横线
  // 是上一轮放行的写法
  const COMMON_REST = ' \\nA-Za-z\\u3000\\u3001-\\u3003\\u3008-\\u3011\\u3014-\\u301f\\u3030\\u30fc()\\[\\]{}:;!?"=+/~*#<>'
    + '\\uff01-\\uff03\\uff08-\\uff0c\\uff0f\\uff1a-\\uff1f\\uff3b\\uff3d\\uff5b\\uff5d\\uff5e\\u00a5\\uffe5'
    + '\\u2013-\\u2015\\u201c\\u201d\\u2026\\u2e3a\\u2e3b\\ufe58\\u2500\\u00d7\\u00f7\\u2248\\u2264\\u2265'
  const STROKES = '\\u4e28\\u4e36\\u4e3f\\u4e40\\u4e41\\u4e5a\\u4e5b\\u4e85'
    + '\\u5202\\u51ab\\u6c35\\u4ebb\\u961d\\u6274\\u5fc4\\u72ad\\u793b\\u8864\\u8ba0\\u9485\\u9963\\u7e9f\\u4e2c'
    + '\\u2e80-\\u2eff\\u2f00-\\u2fdf'
  const COMMON = unicode(`^(?:(?![${STROKES}\\u{20000}-\\u{3ffff}])(?=\\p{Script=Han})\\p{Lo}|[${COMMON_REST}])$`)
    || new RegExp(`^(?:(?![${STROKES}])[\\u3400-\\u4dbf\\u4e00-\\u9fff\\uf900-\\ufaff]|[${COMMON_REST}])$`)
  // 变体选择符只换前一个字的字形（文字样式还是表情样式），算 skip
  const VARIATION = unicode('^[\\u180b-\\u180d\\u180f\\ufe00-\\ufe0f\\u{e0100}-\\u{e01ef}]$')
    || /^[\u180b-\u180d\u180f\ufe00-\ufe0f]$/
  const BLUR_EXTRA = '\\u00ad\\u02dd\\u1843\\u1cd3\\u200a\\u2044\\ufffc'
  const BLUR = unicode(`^(?![\\t\\n\\f\\r])[\\p{Cc}\\p{Cn}\\p{Co}\\p{Cs}\\p{Mn}\\p{Me}${BLUR_EXTRA}]$`)
    || new RegExp('^(?![\\t\\n\\f\\r])[\\x00-\\x1f\\x7f-\\x9f\\u0300-\\u036f\\u0483-\\u0489\\u1ab0-\\u1aff\\u1dc0-\\u1dff'
      + `\\u20d0-\\u20ff\\ud800-\\udfff\\ue000-\\uf8ff\\ufe20-\\ufe2f\\ufff0-\\ufff8${BLUR_EXTRA}]$`)
  const SKIP = unicode('^[\\p{Cf}\\p{Default_Ignorable_Code_Point}]$')
    || /^[\u034f\u061c\u115f\u1160\u17b4\u17b5\u180e\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164\ufeff\uffa0]$/
  const BIDI = /^[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]$/
  const RTL = unicode('^[\\u0590-\\u08ff\\ufb1d-\\ufdff\\ufe70-\\ufefe\\u{10800}-\\u{10fff}\\u{1e800}-\\u{1efff}]$')
    || /^[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufefe]$/

  /** 一个字（码点）算哪一类，见上 */
  function kindOf(c) {
    if (VARIATION.test(c)) return 'skip'
    if (DIGIT.test(c)) return 'digit'
    if (MINUS.test(c)) return 'minus'
    if (DASH.test(c)) return 'dash'
    if (SEP.test(c)) return 'sep'
    if (UNIT.test(c)) return 'unit'
    if (LOOK.test(c)) return 'look'
    if (BLUR.test(c)) return 'blur'
    return SKIP.test(c) ? 'skip' : 'other'
  }

  const NOWHERE = { at: -1, kind: null, blurred: false }

  /**
   * ⑤ 的最后一步：核对通过的数字、取值，读的人在正文里看到的就该是核对过的那串字。items 是这份报告的 片段 → 核对结果，
   * 每一项记下正文里显示出来的字（onPage）。
   * - 正文里根本不显示它（分隔线里、表格里不在各行各列上的格）：伪造的字可以写在别处，读的人看不到核对过的那个数。
   *   服务端导出时不会出现（分隔线不带句子，表格的格都在行列上），判不通过；
   * - 紧挨着的字里写着数字、小数点（前面补一位、后面接一位或一段小数、接 e 和数字；隔着不占位置、显示成什么说不准的字，
   *   显示时藏起来的排版标记也算紧挨着），或者紧挨着数字开头的是负号、负号前面又不是 ASCII 字母数字（「SKU-12」的连字符
   *   是编号的一部分）：读者看到的是另一个数，判不通过。伪造者不动核对过的那段字、只改旁边的正文，重算全部哈希就做得到。
   *   服务端组装的文档不这样排：两个数之间补空格（evidence._glue），写作者自己写在引用前面的字（「统一」「——」「SKU-」）
   *   都不在这几样里，原样导出照样通过。两个数之间的连字符是区间，不算负号；
   * - 这一句里有改变文字方向的字；核对过的字画着删除线，或者紧挨着画了删除线的字；后面接着量级单位（中间隔着空白、左括号
   *   也算，英文的 T、bn、billion 这类按整串字母认）；后面紧接着乘号、乘方再接数字（×10⁴、*10^4）；前面紧挨着像负号的短横
   *   （en dash 也算）；紧挨着长得像数字的字母、数字符号；紧挨着显示成什么说不准的字；紧挨着数字头尾的那一个字不在常见写法的
   *   名单里（COMMON，白名单：换一个易混字、换一种空白就能绕开按字认的规则，名单以外的一律说不准）：读起来可能不是核对过的
   *   那个数，判无法核对，原因各自写明——真报告里也可能有，不能说成被改过。总览里有一项无法核对就不是绿色的通过（overallOf），
   *   下面写明去系统里核对封存。这几样不会把上一条的不通过降成无法核对。名单只是兜底：易混字、排版的花样数不完（「（单位：
   *   万元）」这类隔着几个字的说明也认不出），这份文件的真伪以系统里封存的清单哈希为准
   * 代码块按原文显示、不标数字，照旧
   */
  function checkLayout(doc, items) {
    const numeric = (seg) => seg.kind === 'number' || seg.kind === 'value'
    const soft = (kind) => kind === 'skip' || kind === 'blur'
    for (const block of Array.isArray(doc.blocks) ? doc.blocks.filter(isObj) : []) {
      if (block.type === 'code') {
        for (const unit of Array.isArray(block.units) ? block.units.filter(isObj) : []) {
          for (const seg of unitPlan(unit).segs) if (items.has(seg)) items.get(seg).onPage = String(seg.text ?? '')
        }
        continue
      }
      for (const flow of flowsOf(block)) {
        // 这一组显示出来的字（藏起来的排版标记不算），逐个记下属于哪个片段、带着什么行内样式（删除线要看）
        const shownUnits = []
        for (const unit of flow) {
          const { segs, text, styles, at } = unitPlan(unit)
          segs.forEach((seg, i) => {
            let shown = ''
            for (let k = at[i][0]; k < at[i][1]; k++) {
              if (styles.hide[k]) continue
              shown += text[k]
              shownUnits.push([text[k], seg, styles.style[k]])
            }
            if (items.has(seg)) items.get(seg).onPage = shown
          })
        }
        // 拼成码点：代理对的两半在页面上挨着就显示成一个字（数学字母区的数字这类），分在两个文字片段里、中间隔着
        // 藏起来的排版标记也一样；不成对的半个单独算一个字（显示成替换字符），不和后面的字拼。可点的数字自成一块，它的
        // 字不和外面的拼
        const chars = []
        for (let k = 0; k < shownUnits.length; k++) {
          const [c, seg, style] = shownUnits[k]
          const next = shownUnits[k + 1]
          const paired = next && c >= '\ud800' && c < '\udc00' && next[0] >= '\udc00' && next[0] < '\ue000'
            && (next[1] === seg || (!items.has(seg) && !items.has(next[1])))
          chars.push(paired ? [c + shownUnits[++k][0], seg, style] : [c, seg, style])
        }
        // 可点的数字自成一块，它里面的字改变不了外面的方向：只看别的片段
        const turned = chars.some(([c, seg]) => !items.has(seg) && (BIDI.test(c) || RTL.test(c)))
        const kinds = chars.map(([c]) => kindOf(c))
        const spans = new Map()
        chars.forEach(([, seg], k) => {
          if (spans.has(seg)) spans.get(seg)[1] = k
          else spans.set(seg, [k, k])
        })
        // 从 k 往 step 方向找下一个看得见的字：skip 跳过，blur 也越过去、记下来。{ at, kind, blurred }
        const look = (k, step) => {
          let blurred = false
          for (let j = k + step; j >= 0 && j < chars.length; j += step) {
            if (kinds[j] === 'skip') continue
            if (kinds[j] === 'blur') {
              blurred = true
              continue
            }
            return { at: j, kind: kinds[j], blurred }
          }
          return { ...NOWHERE, blurred }
        }
        const struck = (j) => j >= 0 && (chars[j][2] & BIT.strike) !== 0
        // 前一个字算不算字母数字（只认 ASCII）：负号前面是它的，是「SKU-12」这类编号的一部分
        const wordy = (at) => at >= 0 && WORDY.test(chars[at][0])
        // 从 j 起紧接着的一整串 ASCII 字母是不是英文的量级写法（MAGNITUDE）
        const magnitude = (j) => {
          let word = ''
          for (let k = j; k >= 0 && k < chars.length && /^[A-Za-z]$/.test(chars[k][0]); k++) word += chars[k][0]
          return MAGNITUDE_LETTER.test(word) || MAGNITUDE.test(word)
        }
        // 紧挨着数字头尾的那一个字（j，不越过看不见的字）在不在常见写法里。逗号、连字符另看上下文：走到这里，数字头前面的
        // 连字符已是编号、区间的写法，逗号前面也不是数字（那两样判不通过）；数字尾后面的是句末、区间。句点在数字头前面只认
        // 前面是字母的缩写（NO.12），别的读起来像小数点
        const usual = (j, left) => {
          if (j < 0 || j >= chars.length) return true
          const c = chars[j][0]
          if (COMMON.test(c) || c === '-' || c === ',') return true
          return c === '.' && (!left || (j > 0 && /^[A-Za-z]$/.test(chars[j - 1][0])))
        }
        for (const [seg, item] of items) {
          if (item.result !== PASS || !spans.has(seg)) continue
          const [s0, s1] = spans.get(seg)
          // 片段自己的头一个、末一个字：越过它自己头尾不占位置、说不准的字（取值里本来就带的组合符号这类）
          let first = s0
          while (first < s1 && soft(kinds[first])) first++
          let last = s1
          while (last > first && soft(kinds[last])) last--
          const lead = kinds[first] === 'digit' || kinds[first] === 'minus'
          const tail = kinds[last] === 'digit'
          const l1 = look(s0, -1)
          const l2 = l1.at >= 0 ? look(l1.at, -1) : NOWHERE
          const r1 = look(s1, 1)
          const r2 = r1.at >= 0 ? look(r1.at, 1) : NOWHERE
          const before = kinds[first] === 'digit' && (l1.kind === 'digit' || (l1.kind === 'sep' && l2.kind === 'digit'))
          // 负号、像负号的短横紧挨着数字开头，前面不是字母数字、也不是另一个数（两个数之间是区间）
          const signed = lead && (l1.kind === 'minus' || l1.kind === 'dash') && !wordy(l2.at)
            && !(l2.at >= 0 && numeric(chars[l2.at][1]))
          // 后面接着 e 和数字（科学记数法，「45,678.5e3」读作四千多万），同紧挨着数字
          const r3 = r2.at >= 0 ? look(r2.at, 1) : NOWHERE
          const signs = (a) => a.kind === 'minus' || (a.at >= 0 && chars[a.at][0] === '+')
          const exponent = tail && r1.at >= 0 && /^[eE]$/.test(chars[r1.at][0])
            && (r2.kind === 'digit' || (signs(r2) && r3.kind === 'digit'))
          const after = tail && (r1.kind === 'digit' || (r1.kind === 'sep' && r2.kind === 'digit') || exponent)
          // 后面的量级单位：中间隔着空白、左括号也算（「45,678.5 万」「45,678.5（万元）」「45,678.5 billion」）
          let ru = r1
          while (ru.at >= 0 && LEAD_IN.test(chars[ru.at][0])) ru = look(ru.at, 1)
          const scaled = (tail || UNIT_HAN.test(chars[last][0])) && (ru.kind === 'unit' || magnitude(ru.at))
          // 后面紧接着乘号、乘方再接数字（「45,678.5×10⁴」读作四亿多）。后面那个数本身也核对过的是两个数相乘，不算
          const times = tail && r1.at >= 0 && TIMES.test(chars[r1.at][0]) && r2.kind === 'digit'
            && !numeric(chars[r2.at][1])
          const crossed = chars.slice(s0, s1 + 1).some((_, j) => struck(s0 + j)) || struck(l1.at) || struck(r1.at)
          const lookalike = (kinds[first] === 'digit' && l1.kind === 'look') || (tail && r1.kind === 'look')
          // 数字头尾紧挨着（或隔着一个逗号、句点）的是说不准的字：它本身就可能显示成数字、负号
          const near = (a, b) => a.blurred || (a.kind === 'sep' && b.blurred)
          const blurred = (lead && near(l1, l2)) || (tail && near(r1, r2))
          // 紧挨着数字头尾的那一个字不在常见写法里（别的文字系统的字母、不常见的空白、零宽字符、全角拉丁字母、表情……）
          const odd = (lead && !usual(s0 - 1, true)) || (tail && !usual(s1 + 1, false))
          if (before || (signed && l1.kind === 'minus') || after) {
            // 紧挨着写着数字、负号、小数点：读者看到的是另一个数（伪造者不动核对过的字、只改旁边的正文，重算全部哈希就
            // 做得到），判不通过。排在下面那几样前面判：伪造者在同一句里再放一个说不准的字、改变方向的字，不能把不通过
            // 降成无法核对
            Object.assign(item, { result: FAIL, reason: T.numGlued })
          } else {
            const why = turned ? T.shownTurned : crossed ? T.shownStruck : scaled ? T.shownScaled : times ? T.shownTimes
              : signed ? T.shownDash : lookalike ? T.shownLookalike : blurred ? T.shownBlurred : odd ? T.shownOdd : null
            if (why) Object.assign(item, { result: NA, reason: T.numShownUnknown(why) })
          }
        }
      }
    }
    for (const item of items.values()) {
      if (item.onPage === undefined && item.result === PASS) Object.assign(item, { result: FAIL, reason: T.numNotShown })
    }
  }

  /**
   * 只看正文的排法（checkLayout）：文档里每个解析成功的数字、取值都当作核对通过，交回 [{ id, ref, result, reason }]。
   * node 里用（tests/test_bundle_shown.py 拿各种写法逐条核；也拿开发数据里的报告文档扫一遍，看真报告会不会被误判）
   */
  function layoutOf(doc) {
    const items = new Map()
    for (const [seg] of segmentsOf(doc)) {
      const cite = isObj(seg.cite) ? seg.cite : {}
      if ((seg.kind === 'number' || seg.kind === 'value') && cite.status === 'resolved') {
        items.set(seg, { result: PASS, reason: '' })
      }
    }
    checkLayout(doc, items)
    return [...items].map(([seg, item]) => ({ id: seg.id ?? null, ref: seg.ref ?? null, text: String(seg.text ?? ''),
      result: item.result, reason: item.reason || '', onPage: item.onPage ?? null }))
  }

  // ------------------------------------------------------------------ 核对

  function push(map, key, value) {
    if (!map.has(key)) map.set(key, [])
    map.get(key).push(value)
  }

  /** 内容里出现过的工件 id（64 位十六进制的字符串），不论在哪一层 */
  function collectIds(value, found) {
    if (typeof value === 'string') {
      if (/^[0-9a-f]{64}$/.test(value)) found.add(value)
    } else if (Array.isArray(value)) {
      for (const v of value) collectIds(v, found)
    } else if (isObj(value)) {
      for (const v of Object.values(value)) collectIds(v, found)
    }
    return found
  }

  /** 文档里的每个片段和它在文档 JSON 里的位置（引用的值按原文取，见 numberTokens） */
  function* segmentsOf(doc) {
    const list = (holder, key) => (isObj(holder) && Array.isArray(holder[key]) ? holder[key] : [])
    for (const [b, block] of list(doc, 'blocks').entries()) {
      for (const [u, unit] of list(block, 'units').entries()) {
        for (const [s, seg] of list(unit, 'segments').entries()) {
          if (isObj(seg)) yield [seg, ['blocks', b, 'units', u, 'segments', s]]
        }
      }
    }
  }

  /** 一份嵌入工件里每个数的原文（numberTokens）：用到时才算，只算一次；算不出来是 null */
  function tokensOf(item) {
    if (item.tokens === undefined) {
      try {
        item.tokens = numberTokens(decoder.decode(fromBase64(item.a.b64)))
      } catch (e) {
        item.tokens = null
      }
    }
    return item.tokens
  }

  /** shownSeal：页面顶部直接写出的封存哈希（服务端渲染进模板，不执行脚本也看得到的那一份） */
  async function verify(data, digest, shownSeal) {
    const out = {
      events: [], manifest: [], artifacts: [], chain: [], numbers: [], docs: [], meta: [], checked: new Map(), order: [],
    }
    const events = Array.isArray(data.events) ? data.events.filter(isObj) : []
    const meta = data.meta

    // ① 带规范字符串的事件
    for (const ev of events) {
      if (typeof ev.canonical !== 'string') continue
      const item = { ev, result: FAIL, reason: '' }
      if ((await digest(utf8(ev.canonical))) !== ev.data_sha256) {
        item.reason = T.eventMismatch
      } else {
        try {
          out.checked.set(ev, parseJson(ev.canonical))
          item.result = PASS
        } catch (e) {
          item.reason = T.eventNotJson
        }
      }
      out.events.push(item)
    }

    // ② 封存清单：seq 相同的几条按文件里的先后（服务端按数据库的顺序给，和封存时一样）
    const order = events.map((ev, i) => [ev, i]).sort((x, y) => x[0].seq - y[0].seq || x[1] - y[1]).map(([ev]) => ev)
    out.order = order
    const lines = order.map((ev) => `${ev.seq}|${ev.type}|${ev.node_id == null ? '' : ev.node_id}|${ev.data_sha256}`)
    const computed = await digest(utf8(lines.join('\n')))
    const beyond = order.find((ev) => !(ev.seq <= meta.manifest_seq))
    const last = order[order.length - 1]
    const manifest = { kind: 'computed', computed, count: lines.length, lines, result: FAIL, reason: '' }
    if (computed !== meta.manifest_hash) manifest.reason = T.manifestMismatch
    else if (beyond) manifest.reason = T.beyondSeal(beyond.seq)
    // 新运行封存时记下的就是当时最后一条事件的序号（runner._seal）；早期运行的范围是推算的，不一定落在某条事件上
    else if (!meta.legacy && last && last.seq !== meta.manifest_seq) manifest.reason = T.rangeMismatch(meta.manifest_seq, last.seq)
    else {
      manifest.result = PASS
      manifest.reason = T.manifestOk
    }
    out.manifest.push(manifest)
    // 页面顶部写出的那一份要和核对用的一致：不然改过事件、重算了数据里的哈希，顶上却留着原来那串，读的人拿它去系统
    // 里对照会对得上。脚本执行后顶上换成核对用的这一份
    const seal = String(meta.manifest_hash ?? '')
    out.manifest.push({ kind: 'shown', shown: shownSeal, result: shownSeal === seal ? PASS : FAIL,
      reason: shownSeal === seal ? T.shownOk : T.shownMismatch })

    // ③ 工件。服务端给每份工件的是三样之一：原字节、遮罩（查询快照、口径卡，可另带 mismatch、unparsable）、服务端已没有文件
    const arts = new Map()
    for (const a of Array.isArray(data.artifacts) ? data.artifacts.filter(isObj) : []) {
      const item = { a, result: NA, reason: '', json: null, loose: null, parsed: false }
      if (typeof a.b64 === 'string') {
        let bytes = null
        try {
          bytes = fromBase64(a.b64)
        } catch (e) {
          item.result = FAIL
          item.reason = T.artifactUndecodable
        }
        if (bytes) {
          const intact = (await digest(bytes)) === a.id
          try {
            // 对不上的也解析出来（loose）：被改过的报告仍显示正文，醒目地标着不可信
            item.loose = parseJson(decoder.decode(bytes))
            item.parsed = true
          } catch (e) {
            item.loose = null
          }
          if (!intact) {
            item.result = FAIL
            item.reason = T.artifactMismatch
          } else if (!item.parsed) {
            // 哈希对得上，就是系统里存的那一份：读不懂不是被改过，照实说核对不了
            item.reason = T.artifactUnparsable
          } else {
            item.result = PASS
            item.json = item.loose
          }
        }
      } else if (a.masked) {
        // 查询快照：这条查询用到或可能用到设了遮罩的列；口径卡：卡上有输入取自遮罩列（卡上记着输入的值和代入式）；
        // 报告文档：证据目录里写着值取自遮罩列的指标的原值（升级前的报告撰写节点生成的，或者生成之后数据源才设的遮罩；
        // 现在的报告撰写节点写成「已遮罩」）。
        // 三种都只给 id，检索快照这类别的工件不会。
        // 内容不随文件附带，浏览器算不出它对不对得上：导出时服务端看到自己的文件和 id 对不上（mismatch）、解析不出来
        // （unparsable）的照实说，对不上的判不通过——不然被改过的文件看起来和正常的遮罩一样，篡改的证据就丢了。报告文档
        // 只给哈希时页面上没有正文可核，总览不是绿色的通过（overallOf 按报告文档没核对上算）
        if (a.kind !== 'query' && a.kind !== 'metric_set' && a.kind !== 'report_doc') {
          item.result = FAIL
          item.reason = T.maskedOdd
        } else if (a.mismatch) {
          item.result = FAIL
          item.reason = T.maskedMismatch
        } else {
          item.reason = a.unparsable ? T.maskedUnparsable : a.kind === 'query' ? T.maskedQuery
            : a.kind === 'report_doc' ? T.maskedDoc : T.masked
        }
      } else if (a.missing) {
        item.reason = T.artifactMissing
      } else {
        item.result = FAIL
        item.reason = T.artifactAbsent
      }
      arts.set(a.id, item)
      out.artifacts.push(item)
    }

    // ④ 引用链
    const byEvent = new Map()   // 工件 id → 引用它的已核对事件
    for (const [ev, body] of out.checked) for (const id of collectIds(body, new Set())) push(byEvent, id, ev)
    // 每个报告节点认最后一次记下了文档的自查（和服务端、证据接口同一个认法）。内容没核对通过的那条
    // 不知道记没记文档，也算上：宁可报不通过，也不能跳过它去认一份更早的
    const lastChecked = new Map()
    for (const ev of order) {
      if (ev.type !== 'report.checked' || !ev.node_id) continue
      const body = out.checked.get(ev)
      if (body !== undefined && !own(body, 'doc_artifact')) continue
      lastChecked.set(ev.node_id, ev)
    }
    const reports = Array.isArray(data.reports) ? data.reports.filter(isObj) : []
    const trusted = new Map()   // 报告 → 已核对的文档
    const chained = new Set()
    for (const report of reports) {
      const art = arts.get(report.doc_artifact)
      const ev = lastChecked.get(report.node_id)
      const link = { kind: 'report_doc', report, art, ev, result: FAIL, reason: '' }
      if (!ev) link.reason = T.chainNoCheckEvent
      else if (!out.checked.has(ev)) link.reason = T.chainCheckEventFailed
      else if (own(out.checked.get(ev), 'doc_artifact') !== report.doc_artifact) link.reason = T.chainOtherDoc
      else if (!art) link.reason = T.chainDocNotListed
      else if (art.result === FAIL) Object.assign(link, { result: NA, reason: T.chainDocUnverified })
      else if (art.result !== PASS) {
        Object.assign(link, { result: NA,
          reason: art.a.masked ? T.chainDocMasked : art.a.missing ? T.chainDocMissing : T.chainDocUnparsable })
      } else if (own(art.json, 'run_id') !== meta.run_id || own(art.json, 'node_id') !== report.node_id) {
        link.reason = T.chainDocElsewhere
      } else link.result = PASS
      out.chain.push(link)
      if (link.result === PASS) {
        trusted.set(report, art.json)
        chained.add(report.doc_artifact)
      }
    }
    // 运行记录里自查过、文件却没附带的报告：不能悄悄少一份
    for (const [nodeId, ev] of lastChecked) {
      if (!reports.some((r) => r.node_id === nodeId)) {
        const body = out.checked.get(ev)
        out.chain.push({ kind: 'report_doc', report: { node_id: nodeId, doc_artifact: own(body, 'doc_artifact') },
          art: null, ev, result: FAIL, reason: body === undefined ? T.chainCheckEventFailed : T.chainDocLeftOut })
      }
    }
    const byDoc = new Map()   // 工件 id → [{ report, alias }]
    for (const [report, doc] of trusted) {
      const catalog = isObj(doc.catalog) ? doc.catalog : {}
      for (const alias of Object.keys(catalog)) {
        const entry = catalog[alias]
        if (isObj(entry) && typeof entry.artifact === 'string') push(byDoc, entry.artifact, { report, alias })
      }
    }
    for (const item of out.artifacts) {
      if (item.a.kind === 'report_doc') continue
      const evs = byEvent.get(item.a.id) || []
      const docs = byDoc.get(item.a.id) || []
      const ok = evs.length > 0 || docs.length > 0
      out.chain.push({ kind: item.a.kind, art: item, events: evs, docs, result: ok ? PASS : FAIL,
        reason: ok ? '' : T.chainUnreferenced })
      if (ok) chained.add(item.a.id)
    }

    // ⑤ 报告里的数字和取值
    for (const report of reports) {
      const art = arts.get(report.doc_artifact)
      const link = out.chain.find((c) => c.kind === 'report_doc' && c.report === report)
      const doc = trusted.get(report) || (art && art.parsed && isObj(art.loose) ? art.loose : null)
      out.docs.push({ report, art, link, doc, trusted: trusted.has(report) })
      if (!doc) continue
      for (const [seg, path] of segmentsOf(doc)) {
        if (seg.kind !== 'number' && seg.kind !== 'value') continue
        // 引用的值在文档 JSON 里的原文：超过 2^53 的整数照原文写进原因和出处面板
        const citedToken = () => tokenAt(tokensOf(art), [...path, 'cite', 'value'])
        const check = trusted.has(report) ? checkNumber(seg, doc, arts, chained, citedToken)
          : { result: NA, reason: T.numDocUntrusted, source: describe(seg, doc) }
        out.numbers.push({ ...check, seg, report, key: `${report.node_id}|${seg.id}` })
      }
      // 核对通过的，还要看读的人在正文里看到的是不是这串字
      if (trusted.has(report)) checkLayout(doc, new Map(out.numbers.filter((n) => n.report === report).map((n) => [n.seg, n])))
    }

    // ⑥ 运行信息
    out.meta = checkMeta(meta, out)
    return out
  }

  /** 片段的出处说一句（核对前就能说的那部分） */
  function describe(seg, doc) {
    const cite = isObj(seg.cite) ? seg.cite : null
    if (!cite) return T.srcNone
    const entry = own(doc.catalog, cite.alias)
    const loc = isObj(cite.locator) ? cite.locator : {}
    if (cite.kind === 'metric') return T.srcMetric(own(entry, 'name') || loc.metric || cite.ref || '')
    if (cite.kind === 'cell') {
      return Number.isInteger(loc.row) ? T.srcCell(cite.alias, loc.row, String(loc.column ?? '')) : T.srcQuery(cite.alias)
    }
    if (cite.kind === 'input') return T.srcInput(loc.field || cite.ref || '')
    return T.srcOther
  }

  /**
   * citedToken() 交回引用的值在文档 JSON 里的原文（数才有）。引用的值对上了，再核正文显示的字（checkShown）：两样都对上
   * 才算通过
   */
  function checkNumber(seg, doc, arts, chained, citedToken = () => null) {
    const cite = isObj(seg.cite) ? seg.cite : null
    const source = describe(seg, doc)
    if (!cite) return { result: NA, reason: T.numUncited, source }
    if (cite.status !== 'resolved') return { result: NA, reason: T.numUnresolved(cite.reason), source }
    if (cite.kind === 'input') {
      // 运行输入不随文件附带，引用的值核对不了，照旧无法核对。正文显示的字照样拿引用的值按服务端的写法（render_number
      // 不分组、不接单位，可带换算）重新渲染比对：只改显示文字（统计周期、地区这类）的伪造在这里露馅，判不通过
      const info = { source, kind: cite.kind, cite, citedToken: citedToken() }
      const shown = checkShown(info, seg, () => renderNumber(cite.value, info.citedToken,
        { fmt: 'plain', conv: convOf(cite) }))
      return shown.result === FAIL ? shown : { ...shown, result: NA, reason: T.numInput }
    }
    if (cite.kind !== 'metric' && cite.kind !== 'cell') return { result: NA, reason: T.numOther, source }
    const entry = own(doc.catalog, cite.alias)
    const info = { source, kind: cite.kind, cite }
    const wanted = cite.kind === 'metric' ? 'metric' : 'query'
    if (!isObj(entry) || entry.kind !== wanted || typeof entry.artifact !== 'string') {
      return { ...info, result: FAIL, reason: T.numNoEntry }
    }
    info.artifact = entry.artifact
    info.node = entry.node_id
    const art = arts.get(entry.artifact)
    // 服务端把已核对的文档引用到的口径卡、快照逐个列进清单（拿不到内容的也列，标明原因）：不在清单里只能是被拿掉了
    if (!art) return { ...info, result: FAIL, reason: T.numArtifactNotListed }
    if (art.result === FAIL) return { ...info, result: NA, reason: T.numArtifactFailed }
    if (art.result !== PASS) {
      return { ...info, result: NA, reason: art.a.masked ? art.reason : art.a.missing ? T.artifactMissing : T.numArtifactUnparsable }
    }
    if (!chained.has(entry.artifact)) return { ...info, result: NA, reason: T.numNotChained }
    info.citedToken = citedToken()
    if (cite.kind === 'metric') {
      const metricId = own(cite.locator, 'metric') ?? own(entry.locator, 'metric')
      const metrics = Array.isArray(art.json.metrics) ? art.json.metrics : []
      const at = metrics.findIndex((m) => isObj(m) && m.id === metricId)
      const metric = metrics[at]
      info.card = art.json
      if (!metric) return { ...info, result: FAIL, reason: T.numNoMetric }
      info.truth = metric.value
      info.truthToken = tokenAt(tokensOf(art), ['metrics', at, 'value'])
      if (!sameValue(cite.value, metric.value)) return { ...info, result: FAIL, reason: T.numMetricDiffers }
      // 口径卡上记着这个指标的单位、小数位、显示格式，报告里另写的换算记在引用上
      return checkShown(info, seg, () => renderNumber(metric.value, info.truthToken, {
        unit: unitOf(metric.unit), decimals: metric.decimals,
        decimalsToken: tokenAt(tokensOf(art), ['metrics', at, 'decimals']), fmt: metric.format, conv: convOf(cite) }))
    }
    const loc = isObj(cite.locator) ? cite.locator : {}
    info.snapshot = art.json
    let cell
    try {
      const [raw, column, key] = locateCell(art.json, loc.row, loc.column)
      info.row = loc.row
      info.column = column
      info.truth = cellValue(raw, columnKind(art.json, column))
      info.truthToken = tokenAt(tokensOf(art), ['rows', loc.row, key])
      // 落成数字文本的（DECIMAL）按数比对，出处面板照快照的原文写：读成 double 后超过 2^53 的订单号会丢位，
      // 和一位不差的「引用的值」并排时自相矛盾
      if (typeof raw === 'string' && typeof info.truth === 'number') info.truthText = pyStrip(raw)
      cell = { raw, column }
    } catch (e) {
      return { ...info, result: FAIL, reason: T.numNoCell(e.message) }
    }
    if (!sameValue(cite.value, info.truth)) return { ...info, result: FAIL, reason: T.numCellDiffers }
    const display = Object.prototype.hasOwnProperty.call(doc, 'cell_display') ? doc.cell_display : 1
    return checkShown(info, seg, () => renderCell(cell.raw, info.truthToken, convOf(cite),
      columnKind(art.json, cell.column), display))
  }

  /** 引用上记的换算（|万、|pct 这类），没写是 null */
  const convOf = (cite) => (own(cite, 'conv') === undefined ? null : cite.conv)

  /** 口径卡的单位：服务端是 str(unit or "")，不是文字的本页不认 */
  function unitOf(unit) {
    if (!unit) return ''
    if (typeof unit !== 'string') throw new Unshown(T.shownUnit)
    return unit
  }

  /** 数的写法：超过 2^53 的整数照 JSON 原文（String 会丢位），别的同 String */
  const numberText = (value, token) => (typeof token === 'string' && /^-?\d+$/.test(token) && !Number.isSafeInteger(value)
    ? token : String(value))

  /** 出处面板里的值：数同 numberText，别的同 valueText */
  const exactText = (value, token) => (typeof value === 'number' ? numberText(value, token) : valueText(value))

  /**
   * 正文显示的字和按显示规则渲染出来的字逐字比对，两边都只去掉首尾空白：口径卡的单位带尾随空白（「元 」）时，服务端照原样
   * 接在数后面，正文和渲染出来的都是「74,613.65元 」，只去一边的话原样导出的报告也对不上。渲染不出来的判无法核对：
   * 不认识的格式不能当成核对过了。对上了记 shownOk（出处面板据此注明一致）；对不上的点名两样：正文显示的字、引用的值
   * （加上按显示规则应显示的字，和引用的值写法一样时不写）
   */
  function checkShown(info, seg, render) {
    let expected
    try {
      expected = render()
    } catch (e) {
      return { ...info, result: NA, reason: T.numShownUnknown(e instanceof Unshown ? e.message : T.shownBroken) }
    }
    info.expected = expected
    const shown = String(seg.text ?? '')
    if (pyStrip(shown) === pyStrip(expected)) return { ...info, result: PASS, shownOk: true }
    const value = info.cite.value
    const cited = typeof value === 'number' ? numberText(value, info.citedToken) : value == null ? null : textOf(value)
    return { ...info, result: FAIL, reason: T.numShownDiffers(shown, cited == null ? T.nullValue : `「${cited}」`,
      expected === cited ? null : expected) }
  }

  /**
   * ⑥ 运行信息：运行结果、运行类别、出具档位不信元信息，拿封存范围内已核对的事件对照，页面上显示的也是事件里的。
   * 事件的类型和序号由 ② 的清单绑定，清单没通过就无从对照。每项 { field, shown（显示的值）, claim（元信息写的）, result, reason }
   */
  function checkMeta(meta, out) {
    const items = []
    const add = (field, shown, claim, result, reason) => items.push({ field, shown, claim, result, reason })
    const order = out.order
    const sealOk = out.manifest[0].result === PASS
    const label = (map, value) => map[value] || (value ? String(value) : '—')
    const what = (ev) => EVENT_LABEL[ev.type] || ev.type

    // 运行结果：按封存范围内真正截止的那条结束类事件核对——它之后再没有更晚的事件。新运行在结束事件落库之后马上封存，
    // 它就是范围内的最后一条；早期运行在 run.finished 落库之前封存，成功结束的那条在范围外，失败、取消的那条在范围的最后。
    // 先失败、经继续运行接着跑的，范围里那条运行失败之后运行又继续了，它不是运行的结束：拿它去对，元信息改成「已失败」
    // 也会被标成已核对。真正截止的那条不在范围里时，只有早期运行成功结束说得通（无法核对），别的说法都没有事件印证
    const statusClaim = label(STATUS_LABEL, meta.status)
    const lastSeq = order.length ? order[order.length - 1].seq : null
    const final = [...order].reverse().find((ev) => TERMINAL[ev.type])
    // 同一序号的几条（老数据里有）都算最后：结束事件和别的事件撞了序号，不能因此说它之后运行又继续了
    const end = final && final.seq === lastSeq ? final : null
    if (!sealOk) add('status', statusClaim, statusClaim, NA, T.metaNeedsManifest)
    else if (end) {
      const ok = TERMINAL[end.type] === meta.status
      add('status', label(STATUS_LABEL, TERMINAL[end.type]), statusClaim, ok ? PASS : FAIL,
        ok ? T.statusOk(end.seq, what(end)) : T.statusMismatch(statusClaim, end.seq, what(end)))
    } else if (meta.legacy && meta.status === 'succeeded') add('status', statusClaim, statusClaim, NA, T.statusLegacy)
    // 显示的值写「—」：范围里没有说明运行怎样结束的事件，不能把文件里写的当成核对出来的
    else if (final) add('status', '—', statusClaim, FAIL, T.statusContinued(statusClaim, final.seq, what(final)))
    else add('status', statusClaim, statusClaim, NA, T.statusNoEvent)

    // 运行类别：运行开始事件记下的工具信任档。正式运行记 formal，探索运行记各工具的档位（一个对象），升级前的运行没有
    const classClaim = label(CLASS_LABEL, meta.run_class)
    const started = order.find((ev) => ev.type === 'run.started')
    if (!sealOk) add('class', classClaim, classClaim, NA, T.metaNeedsManifest)
    else if (!started) add('class', classClaim, classClaim, NA, T.classNoEvent)
    else if (typeof started.canonical !== 'string') add('class', classClaim, classClaim, FAIL, T.classAbsent(started.seq))
    else if (!out.checked.has(started)) add('class', classClaim, classClaim, NA, T.classUnchecked(started.seq))
    else {
      const trust = own(out.checked.get(started), 'tool_trust')
      const expected = trust === 'formal' ? 'formal' : isObj(trust) ? 'exploratory' : null
      if (!expected) add('class', classClaim, classClaim, NA, T.classLegacy(started.seq))
      else {
        const ok = expected === meta.run_class
        add('class', label(CLASS_LABEL, expected), classClaim, ok ? PASS : FAIL,
          ok ? T.classOk(started.seq) : T.classMismatch(classClaim, started.seq))
      }
    }

    // 出具档位：封存范围内最后一条记下了档位的出具判定（和服务端 build._tier 同一个认法），还要是元信息指的那一条
    const tierClaim = isObj(meta.tier) ? label(TIER_LABEL, meta.tier.value) : '—'
    let found = null
    for (const ev of [...order].reverse()) {
      if (ev.type !== 'issuance') continue
      if (typeof ev.canonical !== 'string') found = { result: FAIL, reason: T.tierAbsent(ev.seq) }
      else if (!out.checked.has(ev)) found = { result: NA, reason: T.tierUnchecked(ev.seq) }
      else if (typeof own(out.checked.get(ev), 'tier') === 'string') found = { ev, value: out.checked.get(ev).tier }
      if (found) break
    }
    if (!sealOk) {
      if (meta.tier != null || found) add('tier', tierClaim, tierClaim, NA, T.metaNeedsManifest)
    } else if (!found) {
      if (meta.tier != null) add('tier', tierClaim, tierClaim, FAIL, T.tierNoEvent(tierClaim))
    } else if (!found.ev) add('tier', tierClaim, tierClaim, found.result, found.reason)
    else {
      const ok = isObj(meta.tier) && meta.tier.value === found.value && meta.tier.seq === found.ev.seq
      add('tier', label(TIER_LABEL, found.value), tierClaim, ok ? PASS : FAIL,
        ok ? T.tierOk(found.ev.seq) : T.tierMismatch(tierClaim, found.ev.seq))
    }
    return items
  }

  // ------------------------------------------------------------------ 渲染：工具

  const $ = (id) => document.getElementById(id)

  function h(tag, props, ...kids) {
    const el = document.createElement(tag)
    for (const [key, value] of Object.entries(props || {})) {
      if (value == null || value === false) continue
      if (key === 'class') el.className = value
      else if (key.startsWith('on')) el.addEventListener(key.slice(2), value)
      else el.setAttribute(key, value === true ? '' : String(value))
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : String(kid))
    }
    return el
  }

  const short = (id) => (typeof id === 'string' && id.length > 12 ? `${id.slice(0, 12)}…` : String(id ?? ''))
  const badge = (result) => h('span', { class: 'badge', 'data-result': result }, `${MARK[result]} ${RESULT[result]}`)
  const pad = (n) => String(n).padStart(2, '0')

  /** 时间按读者所在时区显示，后面注明时区：文件会被转给别的时区的人 */
  function when(iso) {
    if (!iso) return '—'
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return String(iso)
    const offset = -d.getTimezoneOffset()
    const zone = `UTC${offset < 0 ? '-' : '+'}${Math.floor(Math.abs(offset) / 60)}${Math.abs(offset) % 60 ? `:${pad(Math.abs(offset) % 60)}` : ''}`
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}（${zone}）`
  }

  function valueText(v) {
    if (v == null) return T.nullValue
    if (typeof v === 'string') return `「${v}」`
    return textOf(v)
  }

  function kv(rows) {
    return h('dl', { class: 'kv' }, rows.filter(Boolean).map(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]))
  }

  /** 一组结果汇总成一个：有不通过就是不通过；全通过、全无法核对照实；通过和无法核对都有是「部分无法核对」 */
  function summarize(items) {
    if (!items.length) return NA
    if (items.some((i) => i.result === FAIL)) return FAIL
    const pass = items.some((i) => i.result === PASS)
    const na = items.some((i) => i.result === NA)
    return pass && na ? PARTIAL : pass ? PASS : NA
  }

  /** 分项计数「通过 24 · 无法核对 3」，是零的不写 */
  function tallyText(items) {
    return [PASS, FAIL, NA].map((r) => [r, items.filter((i) => i.result === r).length])
      .filter(([, n]) => n > 0).map(([r, n]) => `${RESULT[r]} ${n}`).join(' · ')
  }

  // ------------------------------------------------------------------ 渲染：报告正文

  function styledSlice(text, styles, from, to) {
    const frag = document.createDocumentFragment()
    let i = from
    while (i < to) {
      if (styles.hide[i]) { i++; continue }
      const st = styles.style[i]
      let j = i + 1
      while (j < to && !styles.hide[j] && styles.style[j] === st) j++
      let node = document.createTextNode(text.slice(i, j))
      if (st & BIT.code) node = h('code', {}, node)
      if (st & BIT.em) node = h('em', {}, node)
      if (st & BIT.bold) node = h('strong', {}, node)
      if (st & BIT.strike) node = h('s', {}, node)
      if (st & BIT.link) node = h('span', { class: 'link' }, node)
      frag.append(node)
      i = j
    }
    return frag
  }

  const unitText = (unit) => (Array.isArray(unit.segments) ? unit.segments : [])
    .filter((s) => isObj(s) && s.kind !== 'structural').map((s) => String(s.text ?? '')).join('')

  function renderUnit(unit, ctx) {
    const { segs, text, styles, at } = unitPlan(unit)
    const frag = document.createDocumentFragment()
    segs.forEach((seg, i) => {
      let body = styledSlice(text, styles, at[i][0], at[i][1])
      if (seg.strong) body = h('strong', {}, body)
      // 按片段本身找它的核对结果，不按片段 id：伪造的文字片段写上和数字一样的 id，也借不到它的结果、显示不成可点的数字
      const number = ctx.numbers.get(seg)
      frag.append(number ? numberButton(number, body) : h('span', {}, body))
    })
    return frag
  }

  const NUMERIC_CELL = /^[-+−]?[\d,]+(?:\.\d+)?%?[^\d\s]{0,4}$/

  function renderBlock(block, ctx) {
    const units = Array.isArray(block.units) ? block.units.filter(isObj) : []
    const inline = (u) => renderUnit(u, ctx)
    switch (block.type) {
      case 'heading': {
        const level = Math.min(Math.max(Number(block.level) || 2, 1), 3)
        return h(`h${level + 3}`, { class: `doc-h doc-h${level}` }, units.map(inline))
      }
      case 'hr':
        return h('hr')
      case 'quote':
        return h('blockquote', {}, units.map(inline))
      case 'code':
        return h('pre', {}, h('code', {}, units.map(unitText).join('')))
      case 'list': {
        const list = h(block.ordered ? 'ol' : 'ul')
        if (block.ordered && Number.isInteger(block.start) && block.start !== 1) list.setAttribute('start', String(block.start))
        for (const item of listItems(units)) {
          const li = h('li', {}, item.units.map(inline))
          if (item.depth) li.style.marginLeft = `${item.depth * 14}px`
          list.append(li)
        }
        return list
      }
      case 'table': {
        // 表头 row = -1；一格可能被分成几句，按位置归拢（tableCells）
        const { cells, rows: rowIds, cols } = tableCells(units)
        const textAt = (r, c) => (cells.get(`${r}:${c}`) || []).map(unitText).join('').replace(/[*`]/g, '').trim()
        const numeric = new Set(cols.filter((c) => {
          const texts = rowIds.map((r) => textAt(r, c)).filter(Boolean)
          return texts.length > 0 && texts.every((t) => NUMERIC_CELL.test(t))
        }))
        const cell = (tag, r, c) => h(tag, { class: tag === 'td' && numeric.has(c) ? 'n' : null },
          (cells.get(`${r}:${c}`) || []).map(inline))
        return h('div', { class: 'table-wrap' }, h('table', {},
          h('thead', {}, h('tr', {}, cols.map((c) => cell('th', -1, c)))),
          h('tbody', {}, rowIds.map((r) => h('tr', {}, cols.map((c) => cell('td', r, c)))))))
      }
      default:
        return h('p', {}, units.map(inline))
    }
  }

  function renderDoc(doc, ctx) {
    const blocks = Array.isArray(doc.blocks) ? doc.blocks.filter(isObj) : []
    if (!blocks.length) return h('div', { class: 'doc' }, h('pre', {}, String(doc.markdown ?? '')))
    return h('div', { class: 'doc' }, blocks.map((b) => renderBlock(b, ctx)))
  }

  // ------------------------------------------------------------------ 渲染：数字的出处

  const view = { data: null, out: null, active: null, back: null }

  const nodeLabel = (id) => {
    const node = own(view.data.nodes, id)
    return isObj(node) && node.label ? String(node.label) : String(id ?? '—')
  }
  const eventWhat = (ev) => [EVENT_LABEL[ev.type] || ev.type, ev.node_id ? nodeLabel(ev.node_id) : null].filter(Boolean).join(' · ')

  function numberButton(number, body) {
    const kind = number.seg.kind === 'value' ? 'value' : 'number'
    const btn = h('button', {
      type: 'button', class: 'num', 'data-num': number.key, 'data-kind': kind, 'data-result': number.result,
      'aria-label': T.numAria(kind, String(number.seg.text ?? ''), RESULT[number.result]),
      onclick: () => openNumber(number),
    }, body)
    number.button = btn
    return btn
  }

  function artifactState(id) {
    const item = view.out.artifacts.find((a) => a.a.id === id)
    if (!item) return ''
    return [' ', badge(item.result), item.reason ? h('span', { class: 'muted' }, ` ${item.reason}`) : null]
  }

  function detailOf(number) {
    const cite = isObj(number.seg.cite) ? number.seg.cite : null
    const card = number.card
    // 显示文字核对过的（引用的值对上了，按显示规则渲染得出来）：一致时注明，对不上时另列应显示的字。没有出处的数、
    // 核对不了的引用不写：那些字没人重新渲染过。一致不等于通过：运行输入的显示文字对得上，引用的值仍核对不了；紧挨着
    // 还有数字的，这处的字本身也对得上，读起来却不是这个值
    const checked = typeof number.expected === 'string'
    const same = checked && number.shownOk === true
    const rows = [
      [T.dResult, [badge(number.result), number.reason ? ` ${number.reason}` : '']],
      [T.dSource, number.source],
      number.node ? [T.dNode, nodeLabel(number.node)] : null,
      card ? [T.dCaliber, [card.caliber || '—', card.caliber_version ? ` ${card.caliber_version}` : ''].join('')] : null,
      cite && cite.status === 'resolved' && 'value' in cite ? [T.dCited, exactText(cite.value, number.citedToken)] : null,
      'truth' in number ? [number.kind === 'metric' ? T.dCard : T.dCell,
        number.truthText ?? exactText(number.truth, number.truthToken)] : null,
      checked && !same ? [T.dExpected, number.expected] : null,
      [T.dShown, [String(number.seg.text ?? ''), same ? h('span', { class: 'muted' }, `（${T.dShownSame}）`) : null]],
      number.artifact ? [T.dArtifact, [h('code', {}, short(number.artifact)), artifactState(number.artifact)]] : null,
      [T.dReport, nodeLabel(number.report.node_id)],
    ]
    const parts = [kv(rows)]
    const snap = number.snapshot
    if (snap && typeof snap.sql === 'string' && snap.sql) parts.push(h('p', { class: 'sub' }, T.dSql), h('pre', { class: 'sql' }, snap.sql))
    const record = snap && Number.isInteger(number.row) && Array.isArray(snap.rows) ? snap.rows[number.row] : undefined
    if (Array.isArray(record) || isObj(record)) {
      // 被引用的那一行：快照随文件附带（没碰设了遮罩的列）才走到这里，整行照原样列出，引用的那一格高亮。数照 JSON
      // 原文写（同上面「快照里的值」）：超过 2^53 的订单号读成 double 会丢位，和一位不差的「引用的值」对不上
      let columns = (Array.isArray(snap.columns) ? snap.columns : []).map(String)
      if (isObj(record) && !columns.length) columns = Object.keys(record)
      const art = view.out.artifacts.find((a) => a.a.id === number.artifact)
      const tokens = art ? tokensOf(art) : null
      const cellText = (c, i) => {
        const value = isObj(record) ? own(record, c) : record[i]
        if (value == null) return '—'
        if (typeof value !== 'number') return textOf(value)
        return numberText(value, tokenAt(tokens, ['rows', number.row, isObj(record) ? c : i]))
      }
      parts.push(h('p', { class: 'sub' }, T.dRow(number.row)), h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, columns.map((c) => h('th', { class: c === number.column ? 'hit' : null }, c)))),
        h('tbody', {}, h('tr', {}, columns.map((c, i) => h('td', { class: c === number.column ? 'hit' : null,
          'data-cell': c }, cellText(c, i))))))))
    }
    return parts
  }

  function openNumber(number) {
    const panel = $('detail')
    if (panel.hidden) view.back = document.activeElement
    if (view.active) view.active.classList.remove('is-active')
    view.active = number.button || null
    if (view.active) view.active.classList.add('is-active')
    $('detail-title').textContent = String(number.seg.text ?? '')
    $('detail-body').replaceChildren(...detailOf(number))
    panel.hidden = false
    panel.setAttribute('data-open', number.key)
    // 宽屏上正文给面板让出右边一栏（样式表 html.detail-open）：版面变窄、换了行，点的数字可能挪了位置
    document.documentElement.classList.add('detail-open')
    $('detail-close').focus({ preventScroll: true })
    if (!view.active) return
    const sheet = panel.getBoundingClientRect()
    const spot = view.active.getBoundingClientRect()
    if (sheet.top > 0) {
      // 窄屏上面板从底部抽出：点的数字被盖住时把它滚到面板上方
      if (spot.bottom > sheet.top) window.scrollBy({ top: spot.top - Math.max(16, sheet.top / 3) })
    } else {
      // 宽屏：让开之后数字还在视野里（表格横向滚动时也滚到它）
      view.active.scrollIntoView({ block: 'nearest', inline: 'nearest' })
    }
  }

  function closeDetail() {
    const panel = $('detail')
    if (panel.hidden) return
    panel.hidden = true
    panel.removeAttribute('data-open')
    document.documentElement.classList.remove('detail-open')
    if (view.active) view.active.classList.remove('is-active')
    if (view.back && typeof view.back.focus === 'function') view.back.focus()
    view.active = null
  }

  // ------------------------------------------------------------------ 渲染：页面各块

  function rowsOf(rows) {
    return rows.filter(Boolean).flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v)])
  }

  function renderHead(data, out) {
    const meta = data.meta
    $('title').textContent = String(meta.workflow_name || '')
    // 顶上换成核对用的这一份（② 已经核过它和原来写的是否一致）
    $('seal-hash').textContent = String(meta.manifest_hash || '')
    for (const el of document.querySelectorAll('.run-id')) el.textContent = String(meta.run_id || '')
    const checked = (field, fallback) => {
      const item = out.meta.find((m) => m.field === field)
      if (!item) return fallback
      const note = item.result === PASS ? T.markPass : item.result === FAIL ? T.markFail(item.claim) : T.markNa
      return h('span', { 'data-meta': field, 'data-result': item.result }, item.shown,
        h('span', { class: item.result === FAIL ? 'bad' : 'muted' }, `（${note}）`))
    }
    $('meta').replaceChildren(...rowsOf([
      [T.metaRun, h('code', {}, String(meta.run_id || ''))],
      [T.metaStatus, checked('status', '—')],
      [T.metaClass, checked('class', '—')],
      [T.metaTier, checked('tier', '—')],
      [T.metaRange, `${T.range(meta.manifest_seq, out.order.length)}${meta.legacy ? T.rangeLegacy : ''}`],
      [T.metaExported, when(meta.exported_at)],
    ]))
    // 绑不进封存的几样单独一组，写明出处：改了它们，五项核对照样通过，去系统里对照封存哈希也发现不了
    const note = $('record-note')
    note.textContent = T.recordNote
    note.hidden = false
    $('record').replaceChildren(...rowsOf([
      [T.metaWorkflow, String(meta.workflow_name || '—')],
      meta.version != null ? [T.metaVersion, `v${meta.version}`] : null,
      meta.version_hash ? [T.metaVersionHash, h('code', {}, String(meta.version_hash))] : null,
      [T.metaStarted, when(meta.started_at)],
      [T.metaFinished, when(meta.finished_at)],
    ]))
  }

  /** 报告文档没核对上的原因：{ key, text }。key 进 data-why，服务端已没有文件、读不懂、因遮罩不随文件附带的用中性色，
   *  不是红的 */
  function docWhy(item) {
    if (!item.art) return { key: 'absent', text: T.docAbsent }
    if (item.art.result === FAIL || (item.link && item.link.result === FAIL)) return { key: 'untrusted', text: T.docUntrusted }
    if (item.art.a.masked) return { key: 'masked', text: T.docMasked }
    if (item.art.a.missing) return { key: 'missing', text: T.docMissing }
    return { key: 'unreadable', text: T.docUnreadable }
  }

  function renderReports(out) {
    const root = $('reports')
    if (!out.docs.length) {
      root.append(h('p', { class: 'note' }, T.noReports))
      return
    }
    for (const item of out.docs) {
      const label = nodeLabel(item.report.node_id)
      // 报告的徽标连同正文里的数字一起算：重算了全部哈希的伪造只在 ⑤ 露馅，标题旁不能还写着通过
      const nums = out.numbers.filter((n) => n.report === item.report)
      // 正文里按片段本身找核对结果（renderUnit）
      const numbers = new Map(nums.map((n) => [n.seg, n]))
      const docResult = [item.art, item.link].some((x) => x && x.result === FAIL) || !item.art ? FAIL : item.trusted ? PASS : NA
      const result = summarize([{ result: docResult }, ...nums])
      const box = h('article', { class: 'report', 'data-report': item.report.node_id, 'data-result': result },
        h('header', { class: 'report-head' }, h('h3', {}, T.reportTitle(label)), badge(result),
          h('span', { class: 'mono muted' }, short(item.report.doc_artifact)),
          h('span', { class: 'report-parts', 'data-report-parts': '' },
            T.reportParts(RESULT[docResult], nums.length ? tallyText(nums) : T.reportNoNumbers))))
      if (!item.trusted) {
        const why = docWhy(item)
        box.append(h('p', { class: 'warn', 'data-why': why.key }, why.text))
      }
      if (item.doc) box.append(renderDoc(item.doc, { report: item.report, numbers }))
      root.append(box)
    }
  }

  function checkBlock(id, title, summary, items, line) {
    const result = summarize(items)
    const tally = items.length > 1 ? tallyText(items) : ''
    const block = h('section', { class: 'check', 'data-check': id, 'data-result': result },
      h('div', { class: 'check-head' }, badge(result), h('h3', {}, title),
        h('span', { class: 'check-sum' }, tally ? `${summary} · ${tally}` : summary)))
    const list = (rows) => h('ul', { class: 'items' }, rows.map((item) => {
      const li = h('li', { 'data-result': item.result }, badge(item.result), h('span', { class: 'what' }, line(item)))
      if (item.reason) li.append(h('span', { class: 'why' }, item.reason))
      return li
    }))
    if (items.length <= FOLD_AT) {
      block.append(list(items))
    } else {
      // 条目多（事件、数字常有几百条）：不通过的和无法核对的列在外面（不通过的在前），没核到的最需要被看见；
      // 全部的收起来
      const open = [...items.filter((i) => i.result === FAIL), ...items.filter((i) => i.result === NA)]
      if (open.length) block.append(list(open))
      block.append(h('details', {}, h('summary', {}, T.showAll(items.length)), list(items)))
    }
    return block
  }

  function renderChecks(out) {
    const root = $('checks')
    root.append(checkBlock('events', T.checkEvents, T.sumEvents(out.events.length), out.events,
      (i) => `${T.chainByEvent(i.ev.seq, eventWhat(i.ev))}`))
    const computed = out.manifest[0]
    const manifest = checkBlock('manifest', T.checkManifest, T.sumManifest(computed.count), out.manifest,
      (m) => [m.kind === 'computed' ? T.manifestComputed : T.manifestShown, ' ', h('code', {}, m.kind === 'computed' ? m.computed : m.shown || '—')])
    // 参与核对的原文照拼法列出来：读的人不信这一页的脚本，也能复制出去用别的工具算一遍
    manifest.append(h('details', {}, h('summary', {}, T.manifestLines(computed.count)),
      h('pre', { class: 'sql', 'data-manifest-lines': '' }, computed.lines.join('\n'))))
    root.append(manifest)
    root.append(checkBlock('artifacts', T.checkArtifacts, T.sumCount(out.artifacts.length, '份'), out.artifacts,
      (i) => [KIND_LABEL[i.a.kind] || i.a.kind, i.a.node_id ? ` · ${nodeLabel(i.a.node_id)}` : '', ' · ',
        h('code', {}, short(i.a.id))]))
    root.append(checkBlock('chain', T.checkChain, T.sumCount(out.chain.length, '条'), out.chain, (c) => {
      if (c.kind === 'report_doc') {
        const from = c.ev ? ` ← ${T.chainByEvent(c.ev.seq, eventWhat(c.ev))}` : ''
        return [T.chainDoc(nodeLabel(c.report.node_id)), ' ', h('code', {}, short(c.report.doc_artifact)), from]
      }
      const refs = [...c.docs.map((d) => T.chainByDoc(nodeLabel(d.report.node_id), d.alias)),
        ...c.events.map((ev) => T.chainByEvent(ev.seq, eventWhat(ev)))]
      return [KIND_LABEL[c.kind] || c.kind, ' ', h('code', {}, short(c.art.a.id)),
        refs.length ? ` ← ${T.chainFrom}${refs.join('、')}` : '']
    }))
    const values = out.numbers.filter((n) => n.seg.kind === 'value').length
    root.append(checkBlock('numbers', T.checkNumbers, T.sumNumbers(out.numbers.length - values, values), out.numbers,
      (n) => h('button', { type: 'button', 'data-num-item': n.key, onclick: () => {
        if (n.button) n.button.scrollIntoView({ block: 'center' })
        openNumber(n)
      } }, `${String(n.seg.text ?? '')} · ${n.source}`)))
    const metaLabel = { status: T.metaStatus, class: T.metaClass, tier: T.metaTier }
    root.append(checkBlock('meta', T.checkMeta, T.sumMeta, out.meta, (m) => `${metaLabel[m.field]}：${m.shown}`))
  }

  function renderAppended(data) {
    const later = Array.isArray(data.appended) ? data.appended.filter(isObj) : []
    if (!later.length) return
    const root = $('appended')
    root.hidden = false
    root.append(h('p', { class: 'note' }, T.appendedNote), h('ul', { class: 'items check' }, later.map((ev) =>
      h('li', { 'data-appended': ev.seq }, h('span', { class: 'badge', 'data-result': NA }, T.appendedTag),
        h('span', { class: 'what' }, `${T.chainByEvent(ev.seq, eventWhat(ev))}${ev.summary ? `：${ev.summary}` : ''}`)))))
  }

  /**
   * 总览：{ result, text, pass, fail, na }。result 是 pass（绿）、fail（红）、incomplete（有报告文档核对不了）、partial
   * （可核对的都通过，另有几项无法核对）。只有一项都没有无法核对、每份报告都核对上了才是绿色的通过：无法核对的项里可能
   * 正藏着读者看到的另一个数（紧挨着改的量级单位、删除线这类），以前照样给绿色的「可核对的项全部通过」。不是绿色的，
   * 页面另醒目地写明真伪要去系统里核对封存（renderOverall）
   */
  function overallOf(out) {
    const all = [...out.events, ...out.manifest, ...out.artifacts, ...out.chain, ...out.numbers, ...out.meta]
    const count = (r) => all.filter((i) => i.result === r).length
    const [pass, fail, na] = [count(PASS), count(FAIL), count(NA)]
    // 报告文档核对不了（导出时服务端已没有文件、读不懂、因遮罩不随文件附带）：页面上没有可核的正文
    const unverified = out.docs.filter((d) => !d.trusted).length
    const result = fail ? FAIL : unverified ? 'incomplete' : na ? PARTIAL : PASS
    const text = fail ? T.failed(fail) : unverified ? T.incomplete(unverified) : na ? T.passWithNa(na) : T.allPass
    return { result, text, pass, fail, na }
  }

  function renderOverall(out, data) {
    const { result, text, pass, fail, na } = overallOf(out)
    const box = $('overall')
    box.setAttribute('data-overall', result)
    box.querySelector('.overall-mark').textContent = MARK[result] || MARK.na
    $('overall-text').textContent = text
    $('overall-sub').textContent = T.tally(pass, fail, na)
    const hint = $('overall-hint')
    const run = String(data.meta.run_id ?? '')
    hint.textContent = result === PASS ? '' : T.verifyHint(run, String(data.meta.manifest_hash ?? '').slice(0, 12))
    hint.hidden = result === PASS
  }

  function renderFoot(data, kind) {
    $('foot').replaceChildren(
      h('p', { 'data-sha': kind }, kind === 'subtle' ? T.shaSubtle : T.shaJs),
      h('p', {}, T.renderNote),
      h('p', {}, T.schema(String(data.schema || ''))))
  }

  /** 下不了结论：总览写明原因。也标上核对结束（data-verified="fatal"），等着看结果的一方不必干等 */
  function fatal(message) {
    const box = $('overall')
    box.setAttribute('data-overall', FAIL)
    box.querySelector('.overall-mark').textContent = MARK.fail
    $('overall-text').textContent = message
    document.documentElement.setAttribute('data-verified', 'fatal')
  }

  async function main() {
    let data
    try {
      data = JSON.parse($('bundle-data').textContent)
      if (!isObj(data) || !isObj(data.meta) || !Array.isArray(data.events)) throw new Error('bad data')
    } catch (e) {
      fatal(T.broken)
      return
    }
    view.data = data
    const picked = await pickDigest()
    if (!picked) {
      fatal(T.selfTestFailed)
      return
    }
    view.out = await verify(data, picked.digest, $('seal-hash').textContent.trim())
    renderHead(data, view.out)
    renderReports(view.out)
    renderChecks(view.out)
    renderAppended(data)
    renderFoot(data, picked.kind)
    renderOverall(view.out, data)
    $('detail-close').addEventListener('click', closeDetail)
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDetail() })
    document.documentElement.setAttribute('data-verified', '')
  }

  // 在 node 里载入（tests/test_bundle_shown.py 拿服务端的渲染逐条对照、整份核对）：没有页面，交出核对用的纯函数就停
  if (typeof document === 'undefined') {
    if (typeof module === 'object' && module) {
      module.exports = { verify, renderNumber, renderCell, numberTokens, tokenAt, parseJson, Unshown, listItems, tableCells,
        layoutOf, overallOf }
    }
    return
  }

  // 脚本在 head 里：一开始执行就标上 data-js，样式表据此把「尚未核对」换成「正在核对」；文档解析完再核对
  document.documentElement.setAttribute('data-js', '')
  const start = () => main().catch((e) => {
    console.error(e)
    fatal(T.broken)
  })
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true })
  else start()
})()
