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
 *   ⑤ 报告里的每个数字和取值：引用的值等于口径卡里那个指标的值，或者快照里那一格的值。判等照搬服务端
 *      engine/expressions.py 的 cell_value / same_value；数字怎么显示是服务端渲染好的，这里不重新排版；
 *   ⑥ 运行信息：运行结果、运行类别、出具档位拿封存范围内已核对的事件对照。工作流名、版本、起止时间、节点名称
 *      绑不进封存，页面上写明它们取自运行记录。
 * 每项给出通过 / 不通过 / 无法核对和原因。服务端导出时不会出现的状态（被引用的工件不在清单里、口径卡标着遮罩）
 * 一律算不通过：只可能是文件被改过。这些只证明内部一致，真伪要拿封存哈希去系统里对照，页面上写明。
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
    passWithNa: '可核对的项全部通过',
    failed: (n) => `有 ${n} 项不通过`,
    incomplete: (n) => `有 ${n} 份报告未能核对`,
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
    masked: '含遮罩列，未随文件附带，只能核对到哈希',
    maskedOdd: '只有查询快照会因遮罩不随文件附带，这份工件却标着遮罩：导出时不会出现这种情况，文件可能被改过',
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
    dCell: '快照里的值', dShown: '正文显示', dShownNote: '显示格式由服务端渲染', dArtifact: '所在工件', dReport: '所在报告',
    dSql: 'SQL', dRow: (row) => `快照第 ${row + 1} 行`,
    nullValue: '空值（NULL）',
    appendedNote: '这些事件追加在封存之后，不在封存范围内，不参与清单核对，也不能当作证据。',
    appendedTag: '封存之后追加，不在封存范围内',
    shaSubtle: 'SHA-256 由浏览器内置实现计算（crypto.subtle）',
    shaJs: 'SHA-256 由本页内置的实现计算（浏览器未提供 crypto.subtle），结果与浏览器内置实现一致',
    renderNote: '数字的显示格式（千分位、单位、小数位）由服务端渲染；本页核对的是引用的值，不重新排版。',
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

  /** locate_cell：(那一格的原值, 列名)。列号换成列名；行是对象时按键取 */
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
      return [record[name], name]
    }
    const index = columns.indexOf(name)
    if (index < 0) throw new Error(`没有列「${name}」`)
    if (!Array.isArray(record) || index >= record.length) throw new Error(`第 ${row} 行中没有「${name}」对应的单元格`)
    return [record[index], name]
  }

  function columnKind(table, column) {
    const kind = own(own(table, 'column_types'), column)
    return typeof kind === 'string' ? kind : null
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

  function* segmentsOf(doc) {
    for (const block of Array.isArray(doc.blocks) ? doc.blocks : []) {
      for (const unit of isObj(block) && Array.isArray(block.units) ? block.units : []) {
        for (const seg of isObj(unit) && Array.isArray(unit.segments) ? unit.segments : []) {
          if (isObj(seg)) yield seg
        }
      }
    }
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

    // ③ 工件。服务端给每份工件的是三样之一：原字节、遮罩（只有查询快照）、服务端已没有文件
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
        if (a.kind === 'query') {
          item.reason = T.masked
        } else {
          item.result = FAIL
          item.reason = T.maskedOdd
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
        Object.assign(link, { result: NA, reason: art.a.missing ? T.chainDocMissing : T.chainDocUnparsable })
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
      for (const seg of segmentsOf(doc)) {
        if (seg.kind !== 'number' && seg.kind !== 'value') continue
        const check = trusted.has(report) ? checkNumber(seg, doc, arts, chained)
          : { result: NA, reason: T.numDocUntrusted, source: describe(seg, doc) }
        out.numbers.push({ ...check, seg, report, key: `${report.node_id}|${seg.id}` })
      }
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

  function checkNumber(seg, doc, arts, chained) {
    const cite = isObj(seg.cite) ? seg.cite : null
    const source = describe(seg, doc)
    if (!cite) return { result: NA, reason: T.numUncited, source }
    if (cite.status !== 'resolved') return { result: NA, reason: T.numUnresolved(cite.reason), source }
    if (cite.kind === 'input') return { result: NA, reason: T.numInput, source }
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
      return { ...info, result: NA, reason: art.a.masked ? T.masked : art.a.missing ? T.artifactMissing : T.numArtifactUnparsable }
    }
    if (!chained.has(entry.artifact)) return { ...info, result: NA, reason: T.numNotChained }
    if (cite.kind === 'metric') {
      const metricId = own(cite.locator, 'metric') ?? own(entry.locator, 'metric')
      const metrics = Array.isArray(art.json.metrics) ? art.json.metrics : []
      const metric = metrics.find((m) => isObj(m) && m.id === metricId)
      info.card = art.json
      if (!metric) return { ...info, result: FAIL, reason: T.numNoMetric }
      info.truth = metric.value
      return sameValue(cite.value, metric.value) ? { ...info, result: PASS } : { ...info, result: FAIL, reason: T.numMetricDiffers }
    }
    const loc = isObj(cite.locator) ? cite.locator : {}
    info.snapshot = art.json
    try {
      const [raw, column] = locateCell(art.json, loc.row, loc.column)
      info.row = loc.row
      info.column = column
      info.truth = cellValue(raw, columnKind(art.json, column))
    } catch (e) {
      return { ...info, result: FAIL, reason: T.numNoCell(e.message) }
    }
    return sameValue(cite.value, info.truth) ? { ...info, result: PASS } : { ...info, result: FAIL, reason: T.numCellDiffers }
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
    const segs = (Array.isArray(unit.segments) ? unit.segments : []).filter((s) => isObj(s) && s.kind !== 'structural')
    const text = segs.map((s) => String(s.text ?? '')).join('')
    const styles = inlineStyles(text)
    const frag = document.createDocumentFragment()
    let pos = 0
    for (const seg of segs) {
      const from = pos
      pos += String(seg.text ?? '').length
      let body = styledSlice(text, styles, from, pos)
      if (seg.strong) body = h('strong', {}, body)
      const number = ctx.numbers.get(`${ctx.report.node_id}|${seg.id}`)
      frag.append(number ? numberButton(number, body) : h('span', {}, body))
    }
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
        const items = []
        for (const u of units) {
          const lead = Array.isArray(u.segments) ? u.segments[0] : null
          const starts = !items.length || (isObj(lead) && lead.kind === 'structural' && ITEM_START.test(String(lead.text ?? '')))
          if (starts) items.push({ depth: Number(u.depth) || 0, units: [u] })
          else items[items.length - 1].units.push(u)
        }
        const list = h(block.ordered ? 'ol' : 'ul')
        if (block.ordered && Number.isInteger(block.start) && block.start !== 1) list.setAttribute('start', String(block.start))
        for (const item of items) {
          const li = h('li', {}, item.units.map(inline))
          if (item.depth) li.style.marginLeft = `${item.depth * 14}px`
          list.append(li)
        }
        return list
      }
      case 'table': {
        // 表头 row = -1；一格可能被分成几句，按位置归拢
        const cells = new Map()
        const rowIds = []
        let cols = 0
        for (const u of units) {
          const loc = isObj(u.loc) ? u.loc : { row: 0, col: 0 }
          push(cells, `${loc.row}:${loc.col}`, u)
          cols = Math.max(cols, (Number(loc.col) || 0) + 1)
          if (loc.row >= 0 && !rowIds.includes(loc.row)) rowIds.push(loc.row)
        }
        rowIds.sort((a, b) => a - b)
        const textAt = (r, c) => (cells.get(`${r}:${c}`) || []).map(unitText).join('').replace(/[*`]/g, '').trim()
        const numeric = Array.from({ length: cols }, (_, c) => {
          const texts = rowIds.map((r) => textAt(r, c)).filter(Boolean)
          return texts.length > 0 && texts.every((t) => NUMERIC_CELL.test(t))
        })
        const cell = (tag, r, c) => h(tag, { class: tag === 'td' && numeric[c] ? 'n' : null },
          (cells.get(`${r}:${c}`) || []).map(inline))
        return h('div', { class: 'table-wrap' }, h('table', {},
          h('thead', {}, h('tr', {}, Array.from({ length: cols }, (_, c) => cell('th', -1, c)))),
          h('tbody', {}, rowIds.map((r) => h('tr', {}, Array.from({ length: cols }, (_, c) => cell('td', r, c)))))))
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
    // 只有带出处的数字是服务端按引用的值渲染的；模型自己写的数、表格里的文字取值都是原文
    const rendered = number.seg.kind === 'number' && cite && cite.status === 'resolved'
    const rows = [
      [T.dResult, [badge(number.result), number.reason ? ` ${number.reason}` : '']],
      [T.dSource, number.source],
      number.node ? [T.dNode, nodeLabel(number.node)] : null,
      card ? [T.dCaliber, [card.caliber || '—', card.caliber_version ? ` ${card.caliber_version}` : ''].join('')] : null,
      cite && cite.status === 'resolved' && 'value' in cite ? [T.dCited, valueText(cite.value)] : null,
      'truth' in number ? [number.kind === 'metric' ? T.dCard : T.dCell, valueText(number.truth)] : null,
      [T.dShown, [String(number.seg.text ?? ''), rendered ? h('span', { class: 'muted' }, `（${T.dShownNote}）`) : null]],
      number.artifact ? [T.dArtifact, [h('code', {}, short(number.artifact)), artifactState(number.artifact)]] : null,
      [T.dReport, nodeLabel(number.report.node_id)],
    ]
    const parts = [kv(rows)]
    const snap = number.snapshot
    if (snap && typeof snap.sql === 'string' && snap.sql) parts.push(h('p', { class: 'sub' }, T.dSql), h('pre', { class: 'sql' }, snap.sql))
    const record = snap && Number.isInteger(number.row) && Array.isArray(snap.rows) ? snap.rows[number.row] : undefined
    if (Array.isArray(record) || isObj(record)) {
      // 被引用的那一行：快照随文件附带（没有遮罩）才走到这里，整行照原样列出，引用的那一格高亮
      let columns = (Array.isArray(snap.columns) ? snap.columns : []).map(String)
      if (isObj(record) && !columns.length) columns = Object.keys(record)
      const cellAt = (c, i) => (isObj(record) ? own(record, c) : record[i])
      parts.push(h('p', { class: 'sub' }, T.dRow(number.row)), h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, columns.map((c) => h('th', { class: c === number.column ? 'hit' : null }, c)))),
        h('tbody', {}, h('tr', {}, columns.map((c, i) => h('td', { class: c === number.column ? 'hit' : null },
          cellAt(c, i) == null ? '—' : textOf(cellAt(c, i)))))))))
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

  /** 报告文档没核对上的原因：{ key, text }。key 进 data-why，服务端已没有文件、读不懂的用中性色，不是红的 */
  function docWhy(item) {
    if (!item.art) return { key: 'absent', text: T.docAbsent }
    if (item.art.result === FAIL || (item.link && item.link.result === FAIL)) return { key: 'untrusted', text: T.docUntrusted }
    if (item.art.a.missing) return { key: 'missing', text: T.docMissing }
    return { key: 'unreadable', text: T.docUnreadable }
  }

  function renderReports(out) {
    const root = $('reports')
    if (!out.docs.length) {
      root.append(h('p', { class: 'note' }, T.noReports))
      return
    }
    const numbers = new Map(out.numbers.map((n) => [n.key, n]))
    for (const item of out.docs) {
      const label = nodeLabel(item.report.node_id)
      // 报告的徽标连同正文里的数字一起算：重算了全部哈希的伪造只在 ⑤ 露馅，标题旁不能还写着通过
      const nums = out.numbers.filter((n) => n.report === item.report)
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

  function renderOverall(out) {
    const all = [...out.events, ...out.manifest, ...out.artifacts, ...out.chain, ...out.numbers, ...out.meta]
    const count = (r) => all.filter((i) => i.result === r).length
    const [pass, fail, na] = [count(PASS), count(FAIL), count(NA)]
    // 报告文档核对不了（导出时服务端已没有文件、读不懂）：页面上没有可核的正文，不能写成绿色的「通过」
    const unverified = out.docs.filter((d) => !d.trusted).length
    const result = fail ? FAIL : unverified ? 'incomplete' : PASS
    const box = $('overall')
    box.setAttribute('data-overall', result)
    box.querySelector('.overall-mark').textContent = fail ? MARK.fail : unverified ? MARK.na : MARK.pass
    $('overall-text').textContent = fail ? T.failed(fail) : unverified ? T.incomplete(unverified) : na ? T.passWithNa : T.allPass
    $('overall-sub').textContent = T.tally(pass, fail, na)
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
    renderOverall(view.out)
    $('detail-close').addEventListener('click', closeDetail)
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDetail() })
    document.documentElement.setAttribute('data-verified', '')
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
