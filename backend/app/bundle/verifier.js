/*
 * 可验证报告的浏览器端核对：原生 JS，无依赖，不连网。
 *
 * 数据在 <script type="application/json" id="bundle-data"> 里（服务端 bundle/build.py 生成），逐项核对：
 *   ① 每条带规范字符串的事件：sha256(canonical) 等于它的 data_sha256；
 *   ② 封存清单：照 runner 的拼法（每行 seq|type|node_id|data_sha256，node_id 为空写空串，按 seq 排序，换行连接，
 *      没有结尾换行）重算，等于封存哈希；
 *   ③ 每份嵌入的工件：sha256(原字节) 等于工件 id；
 *   ④ 引用链：报告文档的 id 是这个报告节点最后一次自查事件（已核对）记下的那份，文档记的运行、节点对得上；
 *      口径卡和快照的 id 出现在已核对的事件内容里，或者已核对的报告文档的证据目录里；
 *   ⑤ 报告里的每个数字：引用的值等于口径卡里那个指标的值，或者快照里那一格的值。判等照搬服务端
 *      engine/expressions.py 的 cell_value / same_value；数字怎么显示是服务端渲染好的，这里不重新排版。
 * 每项给出通过 / 不通过 / 无法核对和原因。这些只证明内部一致，真伪要拿封存哈希去系统里对照，页面上写明。
 *
 * SHA-256 优先用浏览器的 crypto.subtle；拿不到（非安全上下文、某些浏览器打开本地文件时）用下面的纯 JS 实现，
 * 两者结果一致。纯 JS 实现启动时先拿标准测试向量自检，自检不过就不下任何结论。
 * 来自数据的文字一律经 textContent 写进页面，不拼 HTML。
 */
(() => {
  'use strict'

  const PASS = 'pass'
  const FAIL = 'fail'
  const NA = 'na'

  // ------------------------------------------------------------------ 文案

  const RESULT = { pass: '通过', fail: '不通过', na: '无法核对' }
  const MARK = { pass: '✓', fail: '✗', na: '?' }
  const KIND_LABEL = { report_doc: '报告文档', metric_set: '口径卡', query: '查询快照' }
  const TIER_LABEL = { formal: '完整出具', degraded: '降档出具', withheld: '不予出具' }
  const CLASS_LABEL = { formal: '正式运行', exploratory: '探索运行' }
  const STATUS_LABEL = { succeeded: '已完成', failed: '已失败', cancelled: '已取消' }
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
    tally: (p, f, n) => `通过 ${p} 项 · 不通过 ${f} 项 · 无法核对 ${n} 项`,
    broken: '页面数据无法解析，无法核对',
    selfTestFailed: '本页的 SHA-256 实现在这个浏览器中自检未通过，不给出核对结论',
    sealNote: (id) => '这个文件只证明内部一致：报告里的数字能追到口径卡或查询快照，文档、快照和运行记录彼此对得上。'
      + '它不能单独证明这些记录出自系统、未被整体替换。核对真伪：在系统中打开这次运行（运行 ID '
      + `${id}），确认「凭证详情」里的清单哈希与上面一致，并点「核对」确认事件记录与封存时一致`
      + `（接口 GET /api/runs/${id}/verify）。`,
    metaRun: '运行 ID', metaWorkflow: '工作流', metaClass: '运行类别', metaStatus: '运行结果', metaVersionHash: '版本哈希',
    metaStarted: '开始',
    metaFinished: '结束', metaRange: '封存范围', metaTier: '出具档位', metaExported: '导出时间',
    // 序号不连续（模型的流式输出占序号、不落库），后面注明范围内实际有几条。说法同记录页的封存凭证
    range: (seq, count) => `第 1–${seq} 条事件（范围内 ${count} 条）`,
    rangeLegacy: '（早期运行，范围按结束事件推算）',
    tierChecked: (seq) => `第 ${seq} 条事件，已核对`,
    tierUnchecked: '与已核对的事件对不上',
    noReports: '这次运行没有报告文档，下方只核对运行记录。',
    reportTitle: (label) => `报告「${label}」`,
    docUntrusted: '这份报告文档没有通过核对（见核对明细 ③ ④），正文仅供参考，其中的数字不予核对。',
    docUnreadable: '报告文档无法解析，无法显示正文。',
    checkEvents: '① 事件内容与哈希',
    checkManifest: '② 封存清单',
    checkArtifacts: '③ 工件与哈希',
    checkChain: '④ 引用链',
    checkNumbers: '⑤ 报告中的数字',
    sumEvents: (n) => `${n} 条带内容的事件；其余事件只附哈希，参与 ② 的清单核对`,
    sumManifest: (n) => `按 ${n} 条事件的哈希重算`,
    sumCount: (n, unit) => `${n} ${unit}`,
    showAll: (n) => `展开全部 ${n} 项`,
    eventMismatch: '内容与记录的哈希不一致',
    eventNotJson: '内容不是有效的 JSON',
    manifestComputed: '重算结果',
    manifestLines: (n) => `查看参与清单核对的 ${n} 行（每行 序号|类型|节点|内容哈希，可复制后用任意 SHA-256 工具自行计算）`,
    manifestOk: '与封存清单哈希一致',
    manifestMismatch: '与封存清单哈希不一致：事件被增删、改动或换了顺序',
    beyondSeal: (seq) => `第 ${seq} 条事件的序号超出封存范围`,
    artifactMismatch: '内容与工件 id（哈希）不一致',
    artifactUndecodable: '内容无法解码',
    artifactNotJson: '内容不是有效的 JSON',
    masked: '含遮罩列，未随文件附带，只能核对到哈希',
    artifactMissing: '导出时服务端已没有这份工件的文件',
    artifactAbsent: '内容未随文件附带',
    chainDoc: (label) => `报告「${label}」的文档`,
    chainByEvent: (seq, what) => `第 ${seq} 条事件（${what}）`,
    chainByDoc: (label, alias) => `报告「${label}」的证据目录（${alias}）`,
    chainFrom: '引用自',
    chainNoCheckEvent: '找不到这个报告节点的报告自查事件',
    chainCheckEventFailed: '报告自查事件的内容没有通过核对',
    chainOtherDoc: '这个节点最后一次报告自查记下的不是这份文档',
    chainDocUnverified: '文档内容没有通过核对，无法确认它记下的运行和节点',
    chainDocElsewhere: '文档记下的运行或节点与这次不符',
    chainDocLeftOut: '运行记录里有这个报告节点的自查事件，文件里却没有附带它的文档',
    chainUnreferenced: '已核对的事件和报告文档里都没有引用这份工件',
    srcMetric: (name) => `口径卡指标「${name}」`,
    srcCell: (alias, row, column) => `查询 ${alias} · 第 ${row + 1} 行 · ${column}`,
    srcQuery: (alias) => `查询 ${alias}`,
    srcInput: (field) => `运行输入「${field}」`,
    srcNone: '没有出处',
    srcOther: '其他引用',
    numUncited: '这个数字没有引用出处（出具时已标为无出处）',
    numUnresolved: (why) => `报告生成时这处引用未能解析${why ? `：${why}` : ''}`,
    numInput: '引用的是运行输入，运行输入未随文件附带',
    numOther: '这类引用不在核对范围内',
    numDocUntrusted: '所在的报告文档没有通过核对',
    numNoEntry: '报告的证据目录里没有这处引用指向的条目',
    numArtifactAbsent: '引用的工件未随文件附带',
    numArtifactFailed: '引用的工件没有通过哈希核对',
    numNotChained: '引用的工件不在引用链上',
    numNoMetric: '口径卡里没有这个指标',
    numMetricDiffers: '引用的值与口径卡里的值不一致',
    numNoCell: (why) => `快照里找不到这一格：${why}`,
    numCellDiffers: '引用的值与快照里那一格的值不一致',
    numAria: (text, result) => `数字 ${text}，${result}，查看出处`,
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

  async function verify(data, digest) {
    const out = { events: [], manifest: null, artifacts: [], chain: [], numbers: [], docs: [], checked: new Map() }
    const events = Array.isArray(data.events) ? data.events : []
    const meta = isObj(data.meta) ? data.meta : {}

    // ① 带规范字符串的事件
    for (const ev of events) {
      if (typeof ev.canonical !== 'string') continue
      const item = { ev, result: FAIL, reason: '' }
      if ((await digest(utf8(ev.canonical))) !== ev.data_sha256) {
        item.reason = T.eventMismatch
      } else {
        try {
          out.checked.set(ev, JSON.parse(ev.canonical))
          item.result = PASS
        } catch (e) {
          item.reason = T.eventNotJson
        }
      }
      out.events.push(item)
    }

    // ② 封存清单：seq 相同的几条按文件里的先后（服务端按数据库的顺序给，和封存时一样）
    const order = events.map((ev, i) => [ev, i]).sort((x, y) => x[0].seq - y[0].seq || x[1] - y[1]).map(([ev]) => ev)
    const lines = order.map((ev) => `${ev.seq}|${ev.type}|${ev.node_id == null ? '' : ev.node_id}|${ev.data_sha256}`)
    const computed = await digest(utf8(lines.join('\n')))
    const beyond = order.find((ev) => !(ev.seq <= meta.manifest_seq))
    out.manifest = {
      computed, count: lines.length, lines,
      result: computed === meta.manifest_hash && !beyond ? PASS : FAIL,
      reason: computed !== meta.manifest_hash ? T.manifestMismatch : beyond ? T.beyondSeal(beyond.seq) : T.manifestOk,
    }

    // ③ 工件
    const arts = new Map()
    for (const a of Array.isArray(data.artifacts) ? data.artifacts : []) {
      const item = { a, result: NA, reason: '', json: null, loose: null }
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
            item.loose = JSON.parse(decoder.decode(bytes))
          } catch (e) {
            item.loose = null
          }
          if (!intact) {
            item.result = FAIL
            item.reason = T.artifactMismatch
          } else if (item.loose === null) {
            item.result = FAIL
            item.reason = T.artifactNotJson
          } else {
            item.result = PASS
            item.json = item.loose
          }
        }
      } else {
        item.reason = a.masked ? T.masked : a.missing ? T.artifactMissing : T.artifactAbsent
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
      else if (!art || art.result !== PASS) { link.result = NA; link.reason = T.chainDocUnverified }
      else if (own(art.json, 'run_id') !== meta.run_id || own(art.json, 'node_id') !== report.node_id) link.reason = T.chainDocElsewhere
      else link.result = PASS
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

    // ⑤ 报告里的数字
    for (const report of reports) {
      const art = arts.get(report.doc_artifact)
      const doc = trusted.get(report) || (art && isObj(art.loose) ? art.loose : null)
      out.docs.push({ report, art, doc, trusted: trusted.has(report) })
      if (!doc) continue
      for (const seg of segmentsOf(doc)) {
        if (seg.kind !== 'number' && seg.kind !== 'value') continue
        const check = trusted.has(report) ? checkNumber(seg, doc, arts, chained)
          : { result: NA, reason: T.numDocUntrusted, source: describe(seg, doc) }
        out.numbers.push({ ...check, seg, report, key: `${report.node_id}|${seg.id}` })
      }
    }
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
    if (!art) return { ...info, result: NA, reason: T.numArtifactAbsent }
    if (art.a.masked || art.a.missing) return { ...info, result: NA, reason: art.reason }
    if (art.result !== PASS) return { ...info, result: NA, reason: T.numArtifactFailed }
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

  function worst(items) {
    if (items.some((i) => i.result === FAIL)) return FAIL
    if (items.some((i) => i.result === PASS)) return PASS
    return NA
  }

  // ------------------------------------------------------------------ 渲染：报告正文

  // 行内语法：代码、链接（只显示文字，不给链接：页面不连网）、粗体、删除线。和前端 Markdown.tsx 的规则一致，
  // 斜体不认（要用后行断言，老浏览器不支持）
  const INLINE = '(`+)([^`]+?)\\1|\\[([^\\]]+)\\]\\(([^)\\s]+)[^)]*\\)|\\*\\*([^*]+?)\\*\\*|__([^_]+?)__|~~([^~]+?)~~'
  const BIT = { code: 1, bold: 2, strike: 4, link: 8 }
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
        for (let i = last; i < m.index; i++) style[from + i] = inherited
        const [whole, ticks, code, linkText, , bold1, bold2, strike] = m
        const inner = code ?? linkText ?? bold1 ?? bold2 ?? strike ?? ''
        const base = from + m.index
        const off = code !== undefined ? ticks.length : whole.indexOf(inner)
        for (let i = 0; i < whole.length; i++) hide[base + i] = 1
        for (let i = 0; i < inner.length; i++) hide[base + off + i] = 0
        const bit = code !== undefined ? BIT.code : linkText !== undefined ? BIT.link
          : bold1 !== undefined || bold2 !== undefined ? BIT.bold : BIT.strike
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
    const btn = h('button', {
      type: 'button', class: 'num', 'data-num': number.key, 'data-result': number.result,
      'aria-label': T.numAria(String(number.seg.text ?? ''), RESULT[number.result]),
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
    const rows = [
      [T.dResult, [badge(number.result), number.reason ? ` ${number.reason}` : '']],
      [T.dSource, number.source],
      number.node ? [T.dNode, nodeLabel(number.node)] : null,
      card ? [T.dCaliber, [card.caliber || '—', card.caliber_version ? ` ${card.caliber_version}` : ''].join('')] : null,
      cite && cite.status === 'resolved' && 'value' in cite ? [T.dCited, valueText(cite.value)] : null,
      'truth' in number ? [number.kind === 'metric' ? T.dCard : T.dCell, valueText(number.truth)] : null,
      [T.dShown, [String(number.seg.text ?? ''), h('span', { class: 'muted' }, `（${T.dShownNote}）`)]],
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
    $('detail-close').focus({ preventScroll: true })
    // 窄屏上面板从底部抽出：点的数字被盖住时把它滚到面板上方
    const sheet = panel.getBoundingClientRect()
    const spot = view.active ? view.active.getBoundingClientRect() : null
    if (spot && sheet.top > 0 && spot.bottom > sheet.top) window.scrollBy({ top: spot.top - Math.max(16, sheet.top / 3) })
  }

  function closeDetail() {
    const panel = $('detail')
    if (panel.hidden) return
    panel.hidden = true
    panel.removeAttribute('data-open')
    if (view.active) view.active.classList.remove('is-active')
    if (view.back && typeof view.back.focus === 'function') view.back.focus()
    view.active = null
  }

  // ------------------------------------------------------------------ 渲染：页面各块

  function renderHead(data) {
    const meta = data.meta
    $('title').textContent = String(meta.workflow_name || '')
    $('seal-hash').textContent = String(meta.manifest_hash || '')
    $('seal-note').textContent = T.sealNote(String(meta.run_id || ''))
    let tier = '—'
    if (isObj(meta.tier)) {
      const ev = data.events.find((e) => e.seq === meta.tier.seq && e.type === 'issuance')
      const body = ev ? view.out.checked.get(ev) : undefined
      const ok = isObj(body) && body.tier === meta.tier.value
      tier = h('span', { 'data-tier': ok ? 'checked' : 'unchecked' }, TIER_LABEL[meta.tier.value] || String(meta.tier.value),
        h('span', { class: ok ? 'muted' : 'bad' }, `（${ok ? T.tierChecked(meta.tier.seq) : T.tierUnchecked}）`))
    }
    const runClass = CLASS_LABEL[meta.run_class] || meta.run_class || '—'
    const rows = [
      [T.metaRun, h('code', {}, String(meta.run_id || ''))],
      [T.metaWorkflow, String(meta.workflow_name || '—')],
      [T.metaClass, meta.version != null ? `${runClass} v${meta.version}` : runClass],
      meta.status ? [T.metaStatus, STATUS_LABEL[meta.status] || String(meta.status)] : null,
      meta.version_hash ? [T.metaVersionHash, h('code', {}, String(meta.version_hash))] : null,
      [T.metaStarted, when(meta.started_at)],
      [T.metaFinished, when(meta.finished_at)],
      [T.metaRange, `${T.range(meta.manifest_seq, data.events.length)}${meta.legacy ? T.rangeLegacy : ''}`],
      [T.metaTier, tier],
      [T.metaExported, when(meta.exported_at)],
    ]
    $('meta').replaceChildren(...rows.filter(Boolean).flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]))
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
      const link = out.chain.find((c) => c.kind === 'report_doc' && c.report === item.report)
      const result = worst([item.art || { result: NA }, link || { result: NA }])
      const box = h('article', { class: 'report', 'data-report': item.report.node_id, 'data-result': result },
        h('header', { class: 'report-head' }, h('h3', {}, T.reportTitle(label)), badge(result),
          h('span', { class: 'mono muted' }, short(item.report.doc_artifact))))
      if (!item.trusted) box.append(h('p', { class: 'warn' }, item.doc ? T.docUntrusted : T.docUnreadable))
      if (item.doc) box.append(renderDoc(item.doc, { report: item.report, numbers }))
      root.append(box)
    }
  }

  function checkBlock(id, title, summary, items, line) {
    const result = items.length ? worst(items) : NA
    const block = h('section', { class: 'check', 'data-check': id, 'data-result': result },
      h('div', { class: 'check-head' }, badge(result), h('h3', {}, title), h('span', { class: 'check-sum' }, summary)))
    const list = (rows) => h('ul', { class: 'items' }, rows.map((item) => {
      const li = h('li', { 'data-result': item.result }, badge(item.result), h('span', { class: 'what' }, line(item)))
      if (item.reason) li.append(h('span', { class: 'why' }, item.reason))
      return li
    }))
    if (items.length <= 20) {
      block.append(list(items))
    } else {
      // 条目多（事件常有几百条）：先列没通过的，全部的收起来
      block.append(list(items.filter((i) => i.result === FAIL)))
      block.append(h('details', {}, h('summary', {}, T.showAll(items.length)), list(items)))
    }
    return block
  }

  function renderChecks(out) {
    const root = $('checks')
    root.append(checkBlock('events', T.checkEvents, T.sumEvents(out.events.length), out.events,
      (i) => `${T.chainByEvent(i.ev.seq, eventWhat(i.ev))}`))
    const m = out.manifest
    const manifest = checkBlock('manifest', T.checkManifest, T.sumManifest(m.count), [m],
      () => [T.manifestComputed, ' ', h('code', {}, m.computed)])
    // 参与核对的原文照拼法列出来：读的人不信这一页的脚本，也能复制出去用别的工具算一遍
    manifest.append(h('details', {}, h('summary', {}, T.manifestLines(m.count)),
      h('pre', { class: 'sql', 'data-manifest-lines': '' }, m.lines.join('\n'))))
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
    root.append(checkBlock('numbers', T.checkNumbers, T.sumCount(out.numbers.length, '个'), out.numbers,
      (n) => h('button', { type: 'button', 'data-num-item': n.key, onclick: () => {
        if (n.button) n.button.scrollIntoView({ block: 'center' })
        openNumber(n)
      } }, `${String(n.seg.text ?? '')} · ${n.source}`)))
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
    const all = [...out.events, out.manifest, ...out.artifacts, ...out.chain, ...out.numbers]
    const count = (r) => all.filter((i) => i.result === r).length
    const [pass, fail, na] = [count(PASS), count(FAIL), count(NA)]
    const result = fail ? FAIL : PASS
    const box = $('overall')
    box.setAttribute('data-overall', result)
    box.querySelector('.overall-mark').textContent = MARK[result]
    $('overall-text').textContent = fail ? T.failed(fail) : na ? T.passWithNa : T.allPass
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
    view.out = await verify(data, picked.digest)
    renderHead(data)
    renderReports(view.out)
    renderChecks(view.out)
    renderAppended(data)
    renderFoot(data, picked.kind)
    renderOverall(view.out)
    $('detail-close').addEventListener('click', closeDetail)
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDetail() })
    document.documentElement.setAttribute('data-verified', '')
  }

  main().catch((e) => {
    console.error(e)
    fatal(T.broken)
  })
})()
