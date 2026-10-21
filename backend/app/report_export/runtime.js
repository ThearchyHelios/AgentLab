/*
 * 报告导出的浏览器端：点开正文里的一个数字 / 名称 / 引文，展示它的出处链（口径、来源、计算方式）。
 * 原生 JS，无依赖，不连网。正文是服务端按报告文档渲染好的 HTML，这里只做三件事：
 *   ① 填页面头部（报告节点、文档工件、封存状态、运行信息）；
 *   ② 给带状态的片段接上点击和键盘（事件委托在正文容器上），点开右侧的出处面板；
 *   ③ 按数据块里的出处链渲染面板：指标步骤（口径卡、口径与版本、原式、代入式、输入、复算、双盲复算、可比性）、
 *      输入步骤（值、来自哪个节点哪次查询的哪一格）、查询步骤（SQL、被引用的行、遮罩列、SQL 检查）、
 *      运行输入、表名字段名、引文，分析结论的六步（判定、方法、计算、输入、复算、封存），和网页上的证据面板
 *      （api/evidence 的片段接口）同一个拼法。正文后面系统写的「分析范围」里的编号同样点得开（data-seg 是 scope:A1）。
 * 链里的每一步带着导出那一刻的封存与哈希状态，照实标明；这份文件不是核对件（页脚写明）。
 * 来自数据的文字一律经 textContent 写进页面，不拼 HTML。
 */
(() => {
  'use strict'

  // ------------------------------------------------------------------ 文案

  const T = {
    schema: (s) => `文件格式 ${s}`,
    stateLabel: {
      deterministic: '有出处', none: '无证据', suspect: '疑似不存在的名称', unverified: '无法核实',
      unjudged: '未裁判', candidate: '猜测的来源',
    },
    kindLabel: { number: '数字', value: '取值', entity: '名称', quote: '引文', analysis: '判定' },
    segTitle: (kind, text) => `${kind}「${text}」`,
    metaRun: '运行 ID', metaClass: '运行类别', metaStatus: '运行状态', metaReport: '报告节点',
    metaStarted: '开始', metaFinished: '结束', metaExported: '导出时间',
    classLabel: { formal: '正式运行', exploratory: '探索运行' },
    statusLabel: { succeeded: '已完成', failed: '已失败', cancelled: '已取消', rejected: '被驳回', running: '进行中', waiting: '等待审批' },
    docHashBad: '这份报告文档与哈希不一致（疑似被修改），正文仅供参考，其中的口径和出处未经核对。',
    sealNone: '这次运行导出时尚未封存：下列出处来自导出那一刻的系统记录，未经封存核对。',
    sealOk: (hash) => `运行已封存，清单哈希 ${hash}。要向系统外的人证明文件未被修改，请导出「可验证报告」（记录页 · 凭证详情），拿封存哈希去系统对照。`,
    sealBroken: '封存核对不一致：事件在封存后被修改、删除或插入，下列出处未经核对。',
    noChain: '这一处没有可展示的出处链。',
    // 指标步骤
    metric: '口径卡指标', dName: '指标', dValue: '值', dCaliber: '口径', dExpression: '原式（计算方式）',
    dSubstituted: '代入式（代入输入后的算式）', dInputs: '输入', dRecompute: '复算', dUnit: '单位',
    dSource: '来源', dPinned: '口径来源', dUpgrade: '升版处置', dDual: '双盲复算', dArtifact: '工件',
    dNode: '来源节点', dStatus: '状态', dDecimals: '小数位',
    recomputeOk: '复算一致', recomputeBad: '复算不一致：代入式的计算结果与记录值不符', recomputeNone: '无法复算',
    // 输入追没追到原始数据（查询快照里的格、运行输入）：和「有出处」（确定性复算一致）是两件事，同证据面板的说法
    dOrigin: '原始数据', originTraced: '输入都追到了原始数据（查询快照里的格或运行输入）',
    originStop: (path, label) => `${path} 的出处追到「${label}」为止，没追到原始数据`,
    originUnknown: (path) => `${path} 无法确定来自哪个节点，没追到原始数据`,
    incomplete: '结果不完整', incompleteWhy: (t) => `基于被截断的查询结果计算，结果不完整${t ? `：${t}` : ''}`,
    sqlCheckFailed: 'SQL 检查未通过', sqlCheckWhy: (t) => `所依据的查询未通过 SQL 检查，结果不可靠${t ? `：${t}` : ''}`,
    missingValue: '缺少输入：口径卡本次未计算出该指标的值',
    dualLabel: { agreed: '一致', disagreed: '不一致', unchecked: '无法复算', skipped: '未复算' },
    dualMore: (t) => `（${t}）`,
    // 输入步骤
    input: '输入', dPath: '路径', dFrom: '取自', dQuery: '查询', dCell: '位置', dMasked: '已遮罩',
    maskNote: '取自设置了遮罩的列，值不随文件显示。遮罩只减少暴露，不是安全边界',
    maskUnverified: (t) => `遮没遮核对不了：${t}`,
    truncated: '查询结果已截断，该字段只含取回的部分行',
    // 查询步骤
    query: '查询', dSql: 'SQL', dSourceName: '数据源', dTool: '工具', dRows: '被引用的行', dAllRows: (n) => `共 ${n} 行`,
    dTotalRows: '行数',
    dColumnsMasked: '遮罩列', dChecks: 'SQL 检查', windowCut: '被引用的行前后行数太多，只列出其中一部分',
    truncatedRows: '查询结果被截断，取回的行不完整',
    merge: '合并查询', mergeInputs: '合并的输入', mergeTraced: '被引用格的来历', mergeWarnings: '执行时的警告',
    mergeNoLineage: '没有逐格来历',
    dRowOf: (n) => `第 ${n + 1} 行`,
    // 其余步骤
    runInput: '运行输入', dField: '字段', entity: '表或字段', quote: '引文', dNameEntity: '名称',
    dTable: '表', dColumn: '列', dFields: '字段', dFieldsMore: (n) => `另 ${n} 个字段`,
    dQueries: '出现在哪几次查询', dChecked: '查过的地方', dClosest: '最接近的已知名称',
    dContent: '原文', dMatch: '比对', dCollection: '知识库', dWhy: '原因',
    present: '在', absent: '不在',
    matchOk: '原文一致', matchBad: '与原文不一致', matchNone: '无法比对',
    // 完整性（每一步都可带）
    dIntegrity: '核对', sealedOk: '已封存 · 哈希核对一致', sealedNo: '不在封存范围内',
    hashBad: '工件哈希核对失败', hashNone: '未能读取工件',
    note: '说明',
    close: '关闭', openAria: (text) => `查看出处：${text}`,
    // 口径（口径账本，封存后比对）：来自口径 / 相关口径 / 这次的推理 / 没比对
    ledger: '口径', ledgerFrom: (name, version, level) => `来自口径「${name}」v${version}${level ? `（${level}）` : ''}`,
    ledgerHow: { independent: '等价已证明', composed: '助手选用' },
    ledgerRelated: '相关口径', ledgerReasoning: '这次的推理：没有相关的已发布口径', ledgerUnknown: '没有比对',
    dBinding: '参数', dDiffers: '差在哪', dHow: '怎么认的', dColumnOf: '对应口径的列',
    unnamed: '已删除的工作流',
    // 口径卡指标上的可比性（跨两份同形查询算的环比、同比）
    dComparability: '两期可比性', comparabilityLabel: { comparable: '两期可比', conditional: '两期有条件可比', incomparable: '两期不可比' },
    comparabilityOpen: '展开这份可比性结论（判定、方法、计算、输入、复算、封存）',
    // 分析结论：判定 → 方法 → 计算 → 输入 → 复算 → 封存
    analysis: '分析结论', dPhrase: '判定语', dCriterion: '判定标准', dSubjects: '点名的段',
    dDirection: '方向', directionLabel: { up: '上升', down: '下降' }, dCaveats: '附带说明', dReason: '算不了的原因',
    dCitedPart: '这一处引用的是', citedPhrase: '判定语', citedValue: (k) => `具名的数「${k}」`,
    citedCell: (r, c) => `结果表第 ${r + 1} 行「${c}」`, dWho: '谁做的',
    originLabel: { card: '分析卡（模板定好的分析）', agent: 'Agent 现场做的分析', metric: '口径卡执行时自动做的可比性检查' },
    dHypothesis: 'Agent 写的假设', refused: '这次分析没有做成', renderOk: '与按结论渲染的结果一致', renderBad: '与按结论渲染的结果不一致',
    method: '方法', dMethod: '方法与版本', dParams: '参数', dSettings: '实际用的阈值', dProbes: '现查结果',
    probeBasis: { live: '现查', profile: '剖析', observed: '取值观察' },
    probeBound: (expr, value) => `按运行当天现算的界 ${expr} = ${value}`, probeEmpty: '这一列一个值都没有',
    probeRange: (min, max) => `${min} 至 ${max}`,
    calc: '计算', dValues: '具名的数', dFormulas: '关键数的代入式', dResultTable: '结果表',
    resultRows: (shown, total) => `列出 ${shown} 行，共 ${total} 行`,
    analysisInput: '输入', dRole: '角色', dRef: '调用里写的名字', dColumnsUsed: '用到的列', dRowsUsed: '用到的行',
    allRows: '全部行', dHeadline: '总数那一格', dHeadlineRatio: '整体比率那一格', dClosure: '闭合核对',
    closureOk: (h, t) => `总数 ${h} 与各段之和 ${t} 闭合`, closureBad: (h, t) => `总数 ${h} 与各段之和 ${t} 对不上`,
    ratioClosureOk: (h, t) => `整体比率 ${h} 与各段分子之和 ÷ 分母之和 ${t} 闭合`,
    ratioClosureBad: (h, t) => `整体比率 ${h} 与各段分子之和 ÷ 分母之和 ${t} 对不上`,
    dParent: '下钻自', recompute: '复算', seal: '封存', inputOfAnalysis: (alias) => `分析结论 ${alias} 的输入`,
    inLedger: '封存范围内的台账记过这份分析结论', notInLedger: '封存范围内的台账里没有这份分析结论',
    viaCard: '记在口径卡的指标集里，随卡封存（不进台账）',
  }

  const MASKED = '已遮罩'
  const NONE = '—'

  // ------------------------------------------------------------------ 工具

  const $ = (id) => document.getElementById(id)
  const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v)
  const own = (obj, key) => (isObj(obj) && typeof key === 'string' && Object.prototype.hasOwnProperty.call(obj, key)
    ? obj[key] : undefined)
  const textOf = (v) => (typeof v === 'string' ? v : isObj(v) || Array.isArray(v) ? JSON.stringify(v) : String(v))
  const short = (id) => (typeof id === 'string' && id.length > 12 ? `${id.slice(0, 12)}…` : String(id ?? ''))
  const pad = (n) => String(n).padStart(2, '0')

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

  /** 时间按读者所在时区显示（文件会被转给别的时区的人），后面注明时区 */
  function when(iso) {
    if (!iso) return NONE
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return String(iso)
    const offset = -d.getTimezoneOffset()
    const zone = `UTC${offset < 0 ? '-' : '+'}${Math.floor(Math.abs(offset) / 60)}${Math.abs(offset) % 60 ? `:${pad(Math.abs(offset) % 60)}` : ''}`
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}（${zone}）`
  }

  const valueText = (v) => (v == null ? '空值（NULL）' : typeof v === 'string' ? `「${v}」` : textOf(v))

  function kv(rows) {
    return h('dl', { class: 'kv' }, rows.filter(Boolean).map(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]))
  }

  const badge = (kind, text) => h('span', { class: 'badge', 'data-kind': kind }, text)
  const ok = (good) => (good === true ? badge('ok', '✓') : good === false ? badge('bad', '✗') : badge('na', '—'))

  // ------------------------------------------------------------------ 数据

  const view = { data: null, active: null, back: null }

  function parseData() {
    try {
      const data = JSON.parse($('report-data').textContent)
      if (isObj(data) && isObj(data.meta)) return data
    } catch (e) {
      // 数据块坏了：正文还在（服务端渲染），只是点不开出处
    }
    return null
  }

  // ------------------------------------------------------------------ 头部

  function renderHead(data) {
    const meta = data.meta
    $('report-label').textContent = `报告节点「${meta.report?.label || meta.report?.node_id || NONE}」`
    $('doc-id').textContent = meta.doc_artifact ? `文档 ${short(meta.doc_artifact)}` : ''
    if (meta.doc_hash_ok === false) {
      const warn = $('doc-warn')
      warn.textContent = T.docHashBad
      warn.hidden = false
    }
    const label = (map, value) => map[value] || (value ? String(value) : NONE)
    $('meta').replaceChildren(...[...[
      [T.metaRun, h('code', {}, String(meta.run_id || ''))],
      [T.metaClass, label(T.classLabel, meta.run_class)],
      // 被驳回（cancelled + end_reason=rejected）照运行记录写「被驳回」，不写成手动停止的「已取消」
      [T.metaStatus, label(T.statusLabel, meta.status === 'cancelled' && meta.end_reason === 'rejected' ? 'rejected' : meta.status)],
      [T.metaReport, String(meta.report?.label || meta.report?.node_id || NONE)],
      [T.metaStarted, when(meta.started_at)],
      [T.metaFinished, when(meta.finished_at)],
      [T.metaExported, when(meta.exported_at)],
    ].filter(Boolean).flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v)])])
    const seal = meta.seal || {}
    const box = $('seal-text')
    if (!seal.sealed) box.textContent = T.sealNone
    else if (seal.ok === false) box.textContent = T.sealBroken
    else box.textContent = T.sealOk(String(seal.manifest_hash || ''))
    $('foot-schema').textContent = T.schema(String(data.schema || ''))
  }

  // ------------------------------------------------------------------ 每一步的渲染

  const nodeLabel = (id) => {
    const node = own(view.data.nodes, id)
    return isObj(node) && node.label ? String(node.label) : (id ? String(id) : NONE)
  }

  /** 指标步骤：名称、值、口径（口径卡、版本、钉住的来源、升版处置）、原式、代入式、输入、复算、双盲复算 */
  function metricPart(step) {
    const body = []
    const rows = [
      [T.dName, step.name || step.metric || NONE],
      step.unit ? [T.dUnit, String(step.unit)] : null,
      step.decimals != null ? [T.dDecimals, String(step.decimals)] : null,
    ]
    body.push(kv(rows))
    // 口径：口径卡名 + 版本；钉在别的工作流上的写明来自哪一版
    const caliber = [`${step.caliber || NONE}`, step.version ? ` ${step.version}` : ''].join('')
    const source = isObj(step.source) ? step.source : null
    body.push(kv([
      [T.dCaliber, h('span', {}, `口径卡「${caliber}」`)],
      source ? [T.dPinned, `工作流「${source.workflow_name || source.workflow_id}」的 ${source.caliber} @ ${source.caliber_version}`] : null,
    ]))
    if (source && isObj(step.caliber_upgrade)) {
      body.push(h('p', { class: 'muted', style: 'font-size:12px;margin:6px 0 0' },
        `${T.dUpgrade}：钉在 ${step.caliber_upgrade.pinned || NONE}，上游最新 ${step.caliber_upgrade.latest || NONE}，${step.caliber_upgrade.policy_label || step.caliber_upgrade.policy || NONE}`))
    }
    if (step.status === 'missing_input') body.push(h('p', { style: 'color: var(--wait); margin: 8px 0 0' }, T.missingValue))
    if (step.incomplete) body.push(h('p', { style: 'color: var(--wait); margin: 8px 0 0' }, T.incompleteWhy(step.incomplete_reason)))
    if (step.sql_check_failed) body.push(h('p', { style: 'color: var(--wait); margin: 8px 0 0' }, T.sqlCheckWhy(step.sql_check_reason)))
    // 计算方式：原式 + 代入式
    body.push(h('p', { class: 'sub' }, T.dExpression), h('pre', { class: 'sql' }, String(step.expression || NONE)))
    body.push(h('p', { class: 'sub' }, T.dSubstituted), h('pre', { class: 'sql' }, String(step.substituted || NONE)))
    const untraced = Array.isArray(step.untraced) ? step.untraced.filter(isObj) : null
    body.push(h('div', {}, kv([
      [T.dRecompute, step.recompute_ok == null ? T.recomputeNone
        : step.recompute_ok ? T.recomputeOk : T.recomputeBad],
      // 卡上没有输入、导出之前的服务端没给时不写
      untraced ? [T.dOrigin, untraced.length
        ? untraced.map((u) => (u.node_id ? T.originStop(String(u.path || NONE), u.label || nodeLabel(u.node_id))
          : T.originUnknown(String(u.path || NONE)))).join('；')
        : T.originTraced] : null,
    ])))
    // 可比性：口径卡执行时自动做的可比性检查（跨两份同形查询算的环比、同比），做过才有
    if (isObj(step.comparability)) {
      const c = step.comparability
      const why = Array.isArray(c.reasons) && c.reasons.length ? `：${c.reasons.join('；')}` : ''
      body.push(h('div', { style: 'margin-top:8px' }, kv([
        [T.dComparability,
          `${own(T.comparabilityLabel, c.verdict) || c.phrase || NONE}${c.verdict === 'comparable' ? '' : why}`],
        c.criterion ? [T.dCriterion, String(c.criterion)] : null,
        c.artifact ? [T.dArtifact, h('code', {}, short(c.artifact))] : null,
      ])))
      // 点得开那份可比性结论：服务端给的出处链（同分析结论的引用），收在一个可展开的块里
      const steps = Array.isArray(c.steps) ? c.steps.filter((x) => isObj(x) && own(PARTS, x.step)) : []
      if (steps.length) {
        body.push(h('details', { class: 'comparability', 'data-comparability': '' },
          h('summary', {}, T.comparabilityOpen), ...steps.map((x) => PARTS[x.step](x))))
      }
    }
    // 双盲复算（期 4）：复核过这个指标才有
    if (isObj(step.dual_check)) {
      const d = step.dual_check
      const more = d.reason || (d.blind_rendered ? `盲算值 ${d.blind_rendered}` : '')
      body.push(h('div', { style: 'margin-top:8px' }, kv([
        [T.dDual, `${T.dualLabel[d.status] || d.status || NONE}${more ? T.dualMore(more) : ''}`],
      ])))
    }
    return h('section', { class: 'part', 'data-step': 'metric' }, h('h3', {}, T.metric),
      h('div', { class: 'part-body' }, ...body))
  }

  /** 输入步骤：路径、值（遮罩的写「已遮罩」）、取自哪个节点、哪次查询的哪一格 */
  function inputPart(step) {
    const masked = Array.isArray(step.masked) && step.masked.length
    const rows = [
      [T.dPath, h('code', {}, String(step.path || NONE))],
      [T.dValue, step.value === MASKED ? T.dMasked : valueText(step.value)],
      step.node_id ? [T.dNode, nodeLabel(step.node_id)] : null,
      step.query ? [T.dQuery, String(step.query)] : null,
      step.cell ? [T.dCell, String(step.cell)] : null,
      step.via ? [T.dFrom, step.via === 'tool_cell' ? '查询结果的一格' : step.via === 'agent_field' ? 'Agent 交来的字段'
        : step.via === 'position' ? '按位置取的一格' : String(step.via)] : null,
    ]
    const bits = []
    if (masked) bits.push(badge('na', `${T.dColumnsMasked}：${step.masked.join('、')}`))
    if (step.mask_unverified) bits.push(badge('na', T.maskUnverified(step.mask_unverified)))
    if (step.status === 'truncated') bits.push(badge('wait', T.truncated))
    return h('section', { class: 'part', 'data-step': 'input' },
      h('h3', {}, `${T.input}「${step.path || NONE}」`),
      h('div', { class: 'part-body' }, kv(rows),
        masked ? h('p', { class: 'muted', style: 'font-size:12px;margin:6px 0 0' }, T.maskNote) : null,
        bits.length ? h('div', { class: 'chips' }, ...bits) : null))
  }

  /** 查询步骤：SQL、数据源、被引用的行（高亮那一格）、遮罩列、SQL 检查、合并视图 */
  function queryPart(step) {
    const body = []
    const rows = [
      step.source ? [T.dSourceName, String(step.source)] : null,
      step.tool ? [T.dTool, h('code', {}, String(step.tool))] : null,
      step.node_id ? [T.dNode, nodeLabel(step.node_id)] : null,
      step.alias ? [T.dQuery, String(step.alias)] : null,
      step.total_rows != null ? [T.dTotalRows, T.dAllRows(step.total_rows)] : null,
    ]
    body.push(kv(rows.filter(Boolean)))
    if (typeof step.sql === 'string' && step.sql) body.push(h('p', { class: 'sub' }, T.dSql), h('pre', { class: 'sql' }, step.sql))
    if (step.mask_note) body.push(h('p', { class: 'muted', style: 'font-size:12px;margin:6px 0 0' }, String(step.mask_note)))
    if (step.note) body.push(h('p', { style: 'color: var(--wait); margin: 8px 0 0' }, String(step.note)))
    // 被引用的行：前后各带几行，引用的那几格高亮；遮罩列写「已遮罩」
    if (Array.isArray(step.rows) && step.rows.length && Array.isArray(step.columns)) {
      const offset = Number.isInteger(step.row_offset) ? step.row_offset : 0
      const index = Array.isArray(step.row_index) ? step.row_index : step.rows.map((_, i) => i + offset)
      const cells = (step.highlight && Array.isArray(step.highlight.cells) ? step.highlight.cells : [])
      const hit = new Set(cells.map((c) => `${c[0]}:${c[1]}`))
      const hitCols = new Set(cells.map((c) => String(c[1])))
      const masked = new Set((step.masked || []).map(String))
      const table = h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, step.columns.map((c) => h('th', { class: hitCols.has(String(c)) ? 'hit' : null }, String(c))))),
        h('tbody', {}, step.rows.map((row, i) => {
          const r = index[i]
          const record = Array.isArray(row) ? row : isObj(row) ? step.columns.map((c) => own(row, c)) : [row]
          return h('tr', {}, record.map((v, j) => h('td', {
            class: [hit.has(`${r}:${step.columns[j]}`) ? 'hit' : '', typeof v === 'number' ? 'n' : '']
              .filter(Boolean).join(' ') || null,
          }, masked.has(String(step.columns[j])) ? MASKED : v == null ? NONE : textOf(v))))
        }))))
      body.push(h('p', { class: 'sub' }, `${T.dRows}（${T.dRowOf(offset)} 起，引用的格高亮）`), table)
      if (step.window_truncated) body.push(h('p', { class: 'muted', style: 'font-size:12px;margin:6px 0 0' }, T.windowCut))
      if (step.truncated) body.push(h('p', { class: 'muted', style: 'font-size:12px;margin:6px 0 0' }, T.truncatedRows))
    }
    // SQL 检查（查出问题才有）
    if (Array.isArray(step.checks) && step.checks.length) {
      body.push(h('p', { class: 'sub' }, T.dChecks),
        h('ul', { style: 'margin:4px 0 0;padding-left:1.2em;font-size:13px' },
          step.checks.map((c) => h('li', {}, `${c.level || 'error'} · ${c.code || ''}：${c.message || ''}`))))
    }
    // 合并查询：输入、合并 SQL、逐格来历、警告
    if (isObj(step.merge)) {
      body.push(h('p', { class: 'sub' }, T.merge))
      if (Array.isArray(step.merge.inputs) && step.merge.inputs.length) {
        body.push(h('ul', { style: 'margin:4px 0 0;padding-left:1.2em;font-size:13px' },
          step.merge.inputs.map((i) => h('li', {},
            `${i.query || i.alias || NONE} · ${i.label || nodeLabel(i.node_id)} · ${i.rows ?? '?'} 行${i.sealed === false ? ' · 未封存' : ''}`))))
      }
      if (Array.isArray(step.merge.traced) && step.merge.traced.length) {
        body.push(h('p', { class: 'sub' }, T.mergeTraced),
          h('ul', { style: 'margin:4px 0 0;padding-left:1.2em;font-size:13px' },
            step.merge.traced.map((t) => h('li', {}, t.input
              ? `第 ${t.cell[0] + 1} 行「${t.cell[1]}」← ${t.query || t.input} 的 ${T.dRowOf(t.row)}「${t.column}」`
              : `第 ${t.cell[0] + 1} 行「${t.cell[1]}」：${t.note || T.mergeNoLineage}`))))
      }
      if (Array.isArray(step.merge.warnings) && step.merge.warnings.length) {
        body.push(h('p', { class: 'sub' }, T.mergeWarnings),
          h('ul', { style: 'margin:4px 0 0;padding-left:1.2em;font-size:13px' },
            step.merge.warnings.map((w) => h('li', {}, String(w.message || '')))))
      }
    }
    const bits = []
    if ('sealed' in step) bits.push(step.sealed ? badge('ok', T.sealedOk) : badge('na', T.sealedNo))
    if (step.hash_ok === false) bits.push(badge('bad', T.hashBad))
    if (bits.length) body.push(h('div', { class: 'chips' }, ...bits))
    return h('section', { class: 'part', 'data-step': 'query' },
      h('h3', {}, `${T.query}${step.alias ? ` ${step.alias}` : ''}${step.input_of ? ` · ${T.inputOfAnalysis(step.input_of)}` : ''}`),
      h('div', { class: 'part-body' }, ...body))
  }

  /** 运行输入步骤 */
  function runInputPart(step) {
    return h('section', { class: 'part', 'data-step': 'run_input' }, h('h3', {}, T.runInput),
      h('div', { class: 'part-body' }, kv([
        [T.dField, String(step.field || NONE)],
        [T.dValue, valueText(step.value)],
        step.node_id ? [T.dNode, nodeLabel(step.node_id)] : null,
      ])))
  }

  /** 实体（表、字段）步骤：出现在哪几次查询、表结构快照、字段清单；可疑实体的查证和最接近的名字 */
  function entityPart(step) {
    const body = []
    const rows = [
      [T.dNameEntity, step.name || NONE],
      step.table ? [T.dTable, String(step.table)] : null,
      step.column ? [T.dColumn, String(step.column)] : null,
    ]
    body.push(kv(rows.filter(Boolean)))
    if (step.reason) body.push(h('p', { style: 'color: var(--wait); margin: 8px 0 0' }, String(step.reason)))
    if (Array.isArray(step.sources) && step.sources.length) {
      body.push(h('p', { class: 'sub' }, T.dQueries),
        h('ul', { style: 'margin:4px 0 0;padding-left:1.2em;font-size:13px' },
          step.sources.map((s) => h('li', {},
            `${s.kind === 'schema' ? '表结构快照' : '查询结果'} ${s.alias || short(s.artifact)} · ${s.sealed ? '已封存' : '未封存'}${s.hash_ok === false ? ' · 哈希核对失败' : ''}${s.present === false ? ` · 名字${T.absent}` : ''}`))))
    }
    if (Array.isArray(step.fields) && step.fields.length) {
      body.push(h('p', { class: 'sub' }, T.dFields),
        h('p', { style: 'margin:4px 0 0;font-size:13px' },
          step.fields.map((f) => `${f.name}${f.type ? `（${f.type}）` : ''}`).join('、')
          + (step.fields_more ? T.dFieldsMore(step.fields_more) : '')))
    }
    if (step.checked && (step.checked.schemas || step.checked.queries)) {
      body.push(h('p', { class: 'muted', style: 'font-size:12px;margin:6px 0 0' },
        `${T.dChecked}：表结构快照 ${step.checked.schemas} 份、查询 ${step.checked.queries} 次，都没有这个名字`))
    }
    if (Array.isArray(step.closest) && step.closest.length) {
      body.push(h('p', { class: 'sub' }, T.dClosest),
        h('ul', { style: 'margin:4px 0 0;padding-left:1.2em;font-size:13px' },
          step.closest.map((c) => h('li', {}, `${c.name}${c.table ? `（表 ${c.table}）` : ''}`))))
    }
    return h('section', { class: 'part', 'data-step': 'entity' }, h('h3', {}, `${T.entity}「${step.name || NONE}」`),
      h('div', { class: 'part-body' }, ...body))
  }

  /** 引文步骤：原话在哪次检索的哪条命中里，带上原文好高亮 */
  function quotePart(step) {
    const body = []
    const rows = [
      [T.dContent, String(step.text || NONE)],
      step.collection ? [T.dCollection, String(step.collection)] : null,
      step.query ? [T.dQuery, String(step.query)] : null,
      step.node_id ? [T.dNode, nodeLabel(step.node_id)] : null,
    ]
    body.push(kv(rows.filter(Boolean)))
    if (typeof step.content === 'string' && step.content) {
      const match = isObj(step.match) ? step.match : {}
      const start = Number.isInteger(match.start) ? match.start : null
      const end = Number.isInteger(match.end) ? match.end : null
      const frag = document.createDocumentFragment()
      if (start != null && end != null && 0 <= start && start <= end && end <= step.content.length) {
        frag.append(step.content.slice(0, start))
        frag.append(h('mark', { class: 'hit', style: 'padding:0 1px;border-radius:3px' }, step.content.slice(start, end)))
        frag.append(step.content.slice(end))
      } else {
        frag.append(step.content)
      }
      body.push(h('p', { class: 'sub' }, T.dContent), h('blockquote', { style: 'margin:4px 0 0' }, frag))
    }
    body.push(kv([[T.dMatch, step.match_ok === true ? T.matchOk : step.match_ok === false ? T.matchBad : T.matchNone]]))
    if (step.note) body.push(h('p', { style: 'color: var(--wait); margin: 8px 0 0' }, String(step.note)))
    const bits = []
    if ('sealed' in step) bits.push(step.sealed ? badge('ok', T.sealedOk) : badge('na', T.sealedNo))
    if (step.hash_ok === false) bits.push(badge('bad', T.hashBad))
    if (bits.length) body.push(h('div', { class: 'chips' }, ...bits))
    return h('section', { class: 'part', 'data-step': 'quote' }, h('h3', {}, `${T.quote}「${String(step.text || '').slice(0, 24)}…」`),
      h('div', { class: 'part-body' }, ...body))
  }

  /** 完整性徽标：已封存、哈希核对失败（每一步都可带） */
  function integrity(step) {
    const bits = []
    if ('sealed' in step) bits.push(step.sealed ? badge('ok', T.sealedOk) : badge('na', T.sealedNo))
    if (step.hash_ok === false) bits.push(badge('bad', T.hashBad))
    return bits.length ? h('div', { class: 'chips' }, ...bits) : null
  }

  const muted = (text) => h('p', { class: 'muted', style: 'font-size:12px;margin:6px 0 0' }, String(text))
  const waiting = (text) => h('p', { style: 'color: var(--wait); margin: 8px 0 0' }, String(text))

  /** 一张小表：表头 heads，行 rows（每行一组格），高亮的格（行号:列 id）和列 id 对上 */
  function smallTable(heads, ids, rows, index, hits) {
    const hit = new Set((hits || []).map((c) => `${c[0]}:${c[1]}`))
    return h('div', { class: 'table-wrap' }, h('table', {},
      h('thead', {}, h('tr', {}, heads.map((c) => h('th', {}, String(c))))),
      h('tbody', {}, rows.map((row, i) => h('tr', {}, (Array.isArray(row) ? row : ids.map((c) => own(row, c))).map((v, j) => h('td', {
        class: [hit.has(`${index[i]}:${ids[j]}`) ? 'hit' : '', typeof v === 'number' ? 'n' : ''].filter(Boolean).join(' ') || null,
      }, v == null ? NONE : textOf(v))))))))
  }

  /** 分析结论 · 判定：判定语、判定标准、判定码和点名的段、附带说明、算不了的原因、这一处引用的是哪一项、谁做的、
   *  「在验证什么」（谁写的照实标：Agent 写的假设 / 分析卡上写的『在验证什么』） */
  function analysisPart(step) {
    const body = []
    const verdict = isObj(step.verdict) ? step.verdict : null
    const cited = isObj(step.cited) ? step.cited : {}
    const citedText = cited.what === 'value' ? T.citedValue(cited.key) : cited.what === 'cell'
      // 结果表的格写列名（服务端照结果表记的 labels 给），不把列 id 当叫法；老接口没给列名的照旧写 id
      ? T.citedCell(Number(cited.row) || 0, String(cited.column_label || cited.column || '')) : T.citedPhrase
    body.push(kv([
      [T.dPhrase, String(step.phrase || NONE)],
      step.criterion ? [T.dCriterion, String(step.criterion)] : null,
      verdict && Array.isArray(verdict.subjects) && verdict.subjects.length
        ? [T.dSubjects, verdict.subjects.map((s) => (Array.isArray(s) ? s.join(' · ') : String(s))).join('、')] : null,
      verdict && verdict.direction ? [T.dDirection, T.directionLabel[verdict.direction] || String(verdict.direction)] : null,
      [T.dCitedPart, citedText],
      [T.dWho, `${own(T.originLabel, step.origin) || NONE}`
        + `${step.node_id ? ` · ${step.label || nodeLabel(step.node_id)}` : ''}`],
    ]))
    if (step.status === 'refused') body.push(waiting(`${T.refused}${step.reason ? `：${step.reason}` : ''}`))
    if (Array.isArray(step.caveats) && step.caveats.length) {
      body.push(h('p', { class: 'sub' }, T.dCaveats),
        h('ul', { style: 'margin:4px 0 0;padding-left:1.2em;font-size:13px' }, step.caveats.map((c) => h('li', {}, String(c.text || '')))))
    }
    if (step.hypothesis) {
      // 「在验证什么」：谁写的服务端照实给（Agent 写的假设 / 分析卡上写的『在验证什么』），这里照原样显示
      body.push(h('p', { class: 'sub' }, String(step.hypothesis_label || T.dHypothesis)),
        h('p', { style: 'margin:4px 0 0;font-size:13px' }, String(step.hypothesis)))
      if (step.hypothesis_note) body.push(muted(step.hypothesis_note))
    }
    if (step.note) body.push(waiting(step.note))
    if (typeof step.render_ok === 'boolean') body.push(muted(step.render_ok ? T.renderOk : T.renderBad))
    return h('section', { class: 'part', 'data-step': 'analysis' },
      h('h3', {}, `${[T.analysis, step.alias].filter(Boolean).join(' ')} · ${step.method_label || NONE}`),
      h('div', { class: 'part-body' }, ...body))
  }

  /** 现查结果的一条：有 expr 的是按运行当天现算的界（CURRENT_DATE 这类在库里算出的日期），其余是一列的实际范围；现查到的
   *  范围最早、最晚都空，是这一列一个值都没有 */
  function probeText(p) {
    const how = `（${own(T.probeBasis, p.basis) || p.basis || NONE}${p.at ? ` · ${p.at}` : ''}）`
    if (typeof p.expr === 'string' && p.expr) return `${T.probeBound(p.expr, p.value || NONE)}${how}`
    const where = `${p.table || NONE}.${p.column || NONE}`
    const empty = p.basis === 'live' && !p.min && !p.max
    return `${where}：${empty ? T.probeEmpty : T.probeRange(p.min || NONE, p.max || NONE)}${how}`
  }

  /** 分析结论 · 方法：方法与版本、参数、实际用的阈值、现查结果 */
  function analysisMethodPart(step) {
    const body = [kv([[T.dMethod, `${step.method_label || NONE} · ${step.method || NONE}`]])]
    if (isObj(step.params) && Object.keys(step.params).length) {
      body.push(h('p', { class: 'sub' }, T.dParams), h('pre', { class: 'sql' }, JSON.stringify(step.params, null, 2)))
    }
    if (Array.isArray(step.settings) && step.settings.length) {
      body.push(h('p', { class: 'sub' }, T.dSettings), kv(step.settings.map((x) => [String(x.label || x.key), textOf(x.value)])))
    }
    if (Array.isArray(step.probes) && step.probes.length) {
      body.push(h('p', { class: 'sub' }, T.dProbes), h('ul', { style: 'margin:4px 0 0;padding-left:1.2em;font-size:13px' },
        step.probes.filter(isObj).map((p) => h('li', {}, probeText(p)))))
    }
    return h('section', { class: 'part', 'data-step': 'analysis_method' }, h('h3', {}, T.method), h('div', { class: 'part-body' }, ...body))
  }

  /** 分析结论 · 计算：关键数的代入式、具名的数、结果表（引用的格高亮） */
  function analysisCalcPart(step) {
    const body = []
    if (Array.isArray(step.formulas) && step.formulas.length) {
      body.push(h('p', { class: 'sub' }, T.dFormulas), h('pre', { class: 'sql' }, step.formulas.map((f) => String(f.text || '')).join('\n')))
    }
    if (Array.isArray(step.values) && step.values.length) {
      body.push(h('p', { class: 'sub' }, T.dValues), kv(step.values.map((v) => [String(v.key),
        h('span', { class: v.cited ? 'hit' : null }, String(v.rendered ?? NONE))])))
    }
    const table = isObj(step.table) ? step.table : null
    if (table && Array.isArray(table.columns) && Array.isArray(table.rows)) {
      const labels = isObj(table.labels) ? table.labels : {}
      const index = Array.isArray(table.row_index) ? table.row_index : table.rows.map((_, i) => i)
      body.push(h('p', { class: 'sub' }, `${T.dResultTable}（${T.resultRows(table.rows.length, table.total_rows ?? table.rows.length)}）`),
        smallTable(table.columns.map((c) => labels[c] || c), table.columns, table.rows.map((r) => (Array.isArray(r)
          ? r.map((v, j) => ((table.masked || []).includes(table.columns[j]) ? MASKED : v)) : r)), index,
        isObj(table.highlight) ? table.highlight.cells : []))
      if (table.window_truncated) body.push(muted(T.windowCut))
    }
    return h('section', { class: 'part', 'data-step': 'analysis_calc' }, h('h3', {}, T.calc), h('div', { class: 'part-body' }, ...body))
  }

  /** 分析结论 · 输入：角色、落到哪个 Q、用到的列和行、总数（比率拆解是整体比率）那一格和闭合核对、下钻的上一层 */
  function analysisInputPart(step) {
    const rows = Array.isArray(step.rows) ? step.rows.map((r) => `第 ${Number(r) + 1} 行`).join('、') : step.rows === 'all' ? T.allRows : null
    const closure = isObj(step.closure) ? step.closure : null
    const ratio = closure !== null && closure.mode === 'ratio'
    const closed = closure && (ratio ? (closure.closed ? T.ratioClosureOk : T.ratioClosureBad)
      : (closure.closed ? T.closureOk : T.closureBad))
    const body = [kv([
      [T.dRole, String(step.role_label || step.role || NONE)],
      step.ref ? [T.dRef, h('code', {}, String(step.ref))] : null,
      step.query ? [T.dQuery, String(step.query)] : null,
      step.alias ? [T.dParent, String(step.alias)] : null,
      step.node_id ? [T.dNode, nodeLabel(step.node_id)] : null,
      Array.isArray(step.columns) && step.columns.length ? [T.dColumnsUsed, step.columns.join('、')] : null,
      rows ? [T.dRowsUsed, rows] : null,
      'value' in step ? [ratio ? T.dHeadlineRatio : T.dHeadline, `${step.column ? `第 ${Number(step.row) + 1} 行「${step.column}」 = ` : ''}${step.value_rendered ?? valueText(step.value)}`] : null,
      closure ? [T.dClosure, closed(closure.headline, closure.total)] : null,
      step.phrase ? [T.dPhrase, String(step.phrase)] : null,
    ])]
    const bits = integrity(step)
    if (bits) body.push(bits)
    return h('section', { class: 'part', 'data-step': 'analysis_input' },
      h('h3', {}, `${T.analysisInput}${step.query || step.alias ? ` ${step.query || step.alias}` : ''}`), h('div', { class: 'part-body' }, ...body))
  }

  /** 分析结论 · 复算：按封存的输入重算、和封存的比（服务端算好的那一句） */
  function analysisRecomputePart(step) {
    const kind = step.status === 'same' ? 'ok' : step.status === 'different' ? 'bad' : 'na'
    return h('section', { class: 'part', 'data-step': 'analysis_recompute' }, h('h3', {}, T.recompute),
      h('div', { class: 'part-body' }, h('div', { class: 'chips' }, badge(kind, String(step.text || NONE)))))
  }

  /** 分析结论 · 封存：台账记没记过、哈希核对 */
  function analysisSealPart(step) {
    const body = [kv([[T.dArtifact, step.artifact ? h('code', {}, short(step.artifact)) : NONE],
      [T.seal, step.via === 'metric_set' ? T.viaCard : step.ledger ? T.inLedger : T.notInLedger]])]
    if (step.note) body.push(waiting(step.note))
    const bits = integrity(step)
    if (bits) body.push(bits)
    return h('section', { class: 'part', 'data-step': 'analysis_seal' }, h('h3', {}, T.seal), h('div', { class: 'part-body' }, ...body))
  }

  const PARTS = {
    metric: metricPart, input: inputPart, query: queryPart, run_input: runInputPart,
    entity: entityPart, quote: quotePart,
    analysis: analysisPart, analysis_method: analysisMethodPart, analysis_calc: analysisCalcPart,
    analysis_input: analysisInputPart, analysis_recompute: analysisRecomputePart, analysis_seal: analysisSealPart,
  }

  /** 口径一步（口径账本给的标签，服务端已按遮罩给）：来自口径的写明哪一版、级别、怎么认的、参数；相关口径写差在哪；
   *  这次的推理、没比对照实说。比对在封存之后，底下写明不在封存范围内（note），事后补比的另写按现在的口径回看 */
  function ledgerPart(label, ledger) {
    const body = []
    const nameOf = (x) => x.workflow_name || T.unnamed
    const bindingText = (items) => (Array.isArray(items) && items.length
      ? items.map((b) => `${b.field} = ${b.masked ? MASKED : valueText(b.value)}`).join('；') : NONE)
    if (label.kind === 'caliber') {
      body.push(h('p', {}, T.ledgerFrom(nameOf(label), label.version, label.level_text || '')))
      body.push(kv([
        [T.dHow, T.ledgerHow[label.how] || String(label.how || NONE)],
        [T.dBinding, bindingText(label.binding)],
        label.column ? [T.dColumnOf, String(label.column)] : null,
      ]))
    } else if (label.kind === 'related') {
      body.push(h('p', {}, T.ledgerRelated))
      for (const item of Array.isArray(label.related) ? label.related : []) {
        const lines = (((item.diff || {}).items) || []).map((d) => h('li', {}, String(d.text || '')))
        body.push(h('p', { class: 'sub' }, T.ledgerFrom(nameOf(item), item.version, item.level_text || '')))
        if (item.reason) body.push(h('p', { class: 'muted' }, String(item.reason)))
        if (lines.length) body.push(h('p', { class: 'sub' }, T.dDiffers), h('ul', {}, lines))
      }
      if (label.reason) body.push(h('p', { class: 'muted' }, String(label.reason)))
    } else if (label.kind === 'reasoning') {
      body.push(h('p', {}, T.ledgerReasoning))
    } else {
      body.push(h('p', {}, T.ledgerUnknown + (label.reason ? `：${label.reason}` : '')))
    }
    const notes = [ledger.note, ledger.retro ? ledger.retro_note : null].filter(Boolean).join(' · ')
    if (notes) body.push(h('p', { class: 'muted' }, notes))
    return h('section', { class: 'part', 'data-step': 'ledger' }, h('h3', {}, T.ledger), h('div', { class: 'part-body' }, ...body))
  }

  // ------------------------------------------------------------------ 面板

  function detailOf(seg, chain, note) {
    const kind = T.kindLabel[seg.dataset.kind] || '片段'
    const state = seg.dataset.state || ''
    const head = h('section', { class: 'part' }, h('h3', {}, `${kind}「${seg.textContent}」`),
      h('div', { class: 'part-body' }, kv([
        [T.dStatus, T.stateLabel[state] || state || NONE],
        [T.dNode, nodeLabel((view.data.meta.report || {}).node_id)],
      ])))
    const parts = [head]
    const ledger = isObj(view.data.ledger) ? view.data.ledger : null
    const label = ledger ? own(ledger.labels, seg.dataset.seg) : undefined
    if (isObj(label)) parts.push(ledgerPart(label, ledger))
    if (chain && chain.length) {
      for (const step of chain) {
        const render = PARTS[step.step]
        if (render) parts.push(render(step))
      }
    } else {
      parts.push(h('p', { class: 'muted' }, T.noChain))
    }
    if (note) parts.push(h('section', { class: 'part' }, h('h3', {}, T.note), h('div', { class: 'part-body' }, note)))
    return parts
  }

  function openSeg(seg) {
    const panel = $('detail')
    if (panel.hidden) view.back = document.activeElement
    if (view.active) view.active.classList.remove('is-active')
    view.active = seg
    seg.classList.add('is-active')
    const chain = view.data ? (view.data.chains || {})[(view.data.refs || {})[seg.dataset.seg]] : undefined
    const note = view.data ? (view.data.notes || {})[seg.dataset.seg] : undefined
    $('detail-title').textContent = `${T.segTitle(T.kindLabel[seg.dataset.kind] || '片段', seg.textContent)}`
    $('detail-body').replaceChildren(...detailOf(seg, Array.isArray(chain) ? chain : null, note))
    panel.hidden = false
    panel.setAttribute('data-open', seg.dataset.seg)
    document.documentElement.classList.add('detail-open')
    $('detail-close').focus({ preventScroll: true })
    const sheet = panel.getBoundingClientRect()
    const spot = seg.getBoundingClientRect()
    if (sheet.top > 0) {
      if (spot.bottom > sheet.top) window.scrollBy({ top: spot.top - Math.max(16, sheet.top / 3) })
    } else {
      seg.scrollIntoView({ block: 'nearest', inline: 'nearest' })
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

  // ------------------------------------------------------------------ 启动

  function main() {
    const data = parseData()
    if (data) {
      view.data = data
      renderHead(data)
    }
    const body = $('report')
    body.addEventListener('click', (e) => {
      const seg = e.target.closest('button[data-seg]')
      if (seg) openSeg(seg)
    })
    $('detail-close').addEventListener('click', closeDetail)
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDetail() })
    document.documentElement.setAttribute('data-js', '')
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', main, { once: true })
  else main()
})()
