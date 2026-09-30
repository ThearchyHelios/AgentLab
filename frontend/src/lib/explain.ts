/**
 * 失败运行的「为什么 + 怎么办」。
 *
 * 新后端的 run.error 已经是人话（后端 app/core/errors.py），但库里大量老运行留的是原始
 * 异常：「NodeError: AnthropicModelNotFoundError: Error code: 404 - {…}」「_make_
 * query_tool.<locals>._run() got an unexpected keyword argument」。排错的人要的是
 * 哪个节点、为什么、下一步点哪里，所以这里按库里真实出现过的失败归类，并说清
 * 「接着跑」有没有用——输入缺了、人驳回了，原样接着跑只会再失败一次，那就不给
 * 这个按钮。
 *
 * 归类之外的一律交给 lib/errors 的 humanizeError（去掉异常类名、拆标题和原因）。
 *
 * 记录页的失败横幅、列表行，助手流的报错行、失败的步骤，问数据页的失败轮次都读
 * 这一份：同一次失败在三处说法不一，人就不知道该信哪句、该点哪个按钮。
 */

import { humanizeError } from './errors'

export type FixKind = 'settings' | 'canvas' | 'rerun' | 'tools'

export interface RunErrorExplain {
  title: string
  reason?: string
  action?: string
  /** 原样接着跑有没有可能过 */
  continuable: boolean
  /** 除了接着跑，最该去的地方 */
  fix?: FixKind
  /** fix 为 rerun 时缺的是哪一项输入：补上它就能重新发起 */
  missingInput?: string
  /**
   * 接着跑之前得先去 fix 那里改好：要改的东西不在这次运行的快照里（比如工具库里的
   * 参数定义），改好之后原样接着跑就能过，所以 continuable 仍为 true。主按钮给 fix，
   * 「接着跑」退成次要——否则人先点接着跑，同样的错再来一遍
   */
  fixFirst?: boolean
  /** fix 的站内地址，比笼统的「去工具库」更准：直接打开要改的那一项 */
  fixTo?: string
  /** 入口上写什么（「去数据页接入」）。不给就按 fix 的默认说法 */
  fixLabel?: string
  /**
   * 该去改的节点（名字）和出错的不是同一个：整形节点解析上游的文字失败，毛病在上游那个
   * 模型节点，打开整形节点的设置什么也改不了
   */
  fixNode?: string
  /** 原文，放进「技术细节」 */
  raw: string
}

/**
 * 状态码只在明说是 HTTP 的地方才算数：「Error code: 401」「HTTP 502」「（429）」。
 * 裸的三位数会误伤「查询结果超过 500 行上限」这种业务报错
 */
const http = (code: string) =>
  String.raw`(?:Error code|status(?: code)?|HTTP(?:/[\d.]+)?)\s*[:=]?\s*${code}\b|[（(]${code}[)）]`
const AUTH = new RegExp(`${http('40[13]')}|authentication|unauthori[sz]ed|invalid[_ ]api[_ ]key|鉴权`, 'i')
const RATE = new RegExp(`${http('429')}|rate.?limit|quota|额度|太频繁`, 'i')
// 后端 core/errors 的 404 现在写「请求的地址或模型不存在（404）」（以前是「对方说找不到（404）」）：
// 它说不清是地址还是模型，不归到「模型不存在」，和以前一样走通用说明
const MISSING_MODEL = new RegExp(`model ?not ?found|not_found_error|(?<!地址或)模型不存在|(?:${http('404')}).*model`, 'i')
const SERVER = new RegExp(
  `${http(String.raw`5\d\d`)}|对方服务出错|服务方内部错误|Internal Server Error|Bad Gateway|Service Unavailable|Gateway Time-?out|overloaded`, 'i')

/** 「NodeError: AnthropicModelNotFoundError: …」这种套了几层的类名前缀全剥掉 */
const CLASS_PREFIX = /^\s*(?:[a-z_][\w.]*\.)?[A-Z]\w*(?:Error|Exception|Exit|Timeout)\s*:\s*/

export function stripClassPrefix(text: string): string {
  let s = text
  for (let i = 0; i < 4 && CLASS_PREFIX.test(s); i++) s = s.replace(CLASS_PREFIX, '')
  return s.trim()
}

/** 模板渲染结果不是合法 JSON、后端又没有点名是哪一处时的通用建议（对应后端 io.TEMPLATE_HINT） */
const TEMPLATE_HINT = '请检查模板中的引号和逗号，字符串值需使用 | json 过滤器输出'

/** 后端 app/api/runs.py 的 TOOL_MISSING：发起运行时发现绑定的工具在本机不存在 */
export const RUN_TOOL_MISSING = 'run_tool_missing'
const DATA_TOOL = /^db_(?:query|schema)__/

/**
 * 「绑定的工具在本机不存在：「X」（调用工具）绑的 db_query__nope；「团队」的成员「研究员」绑的 mcp:a/b。
 * 去数据页接入，或在节点里重新选」。数据源工具（db_query__ / db_schema__）要去数据页接入，自定义和
 * MCP 工具去工具页；两种都缺时入口按第一个缺的走，话里两边都说到
 */
function explainToolMissing(plain: string, raw: string, coded = false): RunErrorExplain | null {
  // 后端原话「绑定的工具在本机不存在：…」；文案整改后可能去掉「在本机」，两种都认
  const m = plain.match(/^绑定的工具(?:在本机)?不存在[：:]\s*([\s\S]+)$/)
  // 带着机读码、话却改了说法：照样按这一类讲，名字认不出就只给工具页
  if (!m && !coded) return null
  const body = (m ? m[1] : plain).trim()
  const cut = body.lastIndexOf('。')
  const list = (cut >= 0 ? body.slice(0, cut) : body).trim()
  const advice = cut >= 0 ? body.slice(cut + 1).trim() : ''
  // 超过 8 处时后端在最后一个名字后面接「等 N 处」：那个「等」不是名字的一部分
  const names = [...list.matchAll(/绑定?的\s*([^\s；;，,。]+)/g)].map((x) => x[1].replace(/等$/, '')).filter(Boolean)
  const first = names[0] ?? ''
  const others = names.filter((n) => !DATA_TOOL.test(n))
  const toData = DATA_TOOL.test(first) || (!names.length && /数据」?页/.test(advice))
  const fixTo = toData ? '/data'
    : !others.length ? '/tools'
    : others.every((n) => n.startsWith('mcp:')) ? '/tools/mcp'
    : others.every((n) => !n.startsWith('mcp:')) ? '/tools/custom' : '/tools'
  return {
    title: names.length === 1 ? `绑定的工具「${first}」不存在` : '绑定的工具不存在',
    reason: `${endStop(list)}本次运行未启动，没有节点被执行。`,
    action: `${endStop(advice || (toData ? '请前往「数据」页接入，或在节点中重新选择' : '请前往「工具」页接入，或在节点中重新选择'))}接入后重新运行。`,
    continuable: false,
    fix: 'tools',
    fixTo,
    fixLabel: toData ? '前往「数据」页接入' : '前往「工具」页接入',
    raw,
  }
}

/**
 * 发起运行那一下被拒的报错（POST /runs 的 ApiError）。只认「绑定的工具在本机不存在」：认机读码
 * run_tool_missing，老后端没码时认原话。别的交给 toast 的通用翻译，返回 null
 */
export function explainStartError(e: unknown): RunErrorExplain | null {
  const code = e && typeof e === 'object' ? (e as { code?: unknown }).code : undefined
  const text = e instanceof Error ? e.message : typeof e === 'string' ? e : ''
  if (code !== RUN_TOOL_MISSING && !/^绑定的工具(?:在本机)?不存在/.test(text)) return null
  return explainToolMissing(stripClassPrefix(text), text, code === RUN_TOOL_MISSING)
}

/** 直达某个自定义工具的编辑框（工具页读 ?edit= 打开它）。fixTo 和工具库里的「去改」共用 */
export const customToolEditPath = (name: string) => `/tools/custom?edit=${encodeURIComponent(name)}`

const TIMEOUT = /timed? ?out|timeout|超时|超过\s*[\d.]+\s*(?:s|秒)\s*(?:没有返回|被中断)/i
/** 够得上「一句中文人话」：零星一两个汉字（「httpx.ReadTimeout: 超时」）不算 */
const isZhProse = (s: string) => (s.match(/[\u4e00-\u9fff]/g)?.length ?? 0) >= 6
const endStop = (s: string) => (/[。！？.!?]$/.test(s) ? s : `${s}。`)

/**
 * 后端已经写成中文人话的超时（查询超时、工具超时、「等待超时：…」）。原话里有具体的
 * 原因（哪个查询、等了多久），常常还跟着一句建议（「加上 WHERE 条件或 LIMIT 缩小范围
 * 再查」）；换成笼统的「下游服务没在限定时间内响应」，这两样就都丢了。
 * 「X超时：…」的冒号前当标题；后面按第一个句号拆成原因和建议
 */
function explainZhTimeout(plain: string, raw: string): RunErrorExplain {
  const head = plain.match(/^([^：:。，,；]{2,12})[：:]\s*([\s\S]+)$/)
  const titled = head && /超时/.test(head[1])
  const body = (titled ? head[2] : plain).trim()
  const cut = body.search(/[。；](?=\s*\S)/)
  // 在「；」处拆开时原因不能挂着半个分号收尾
  const reason = (cut >= 0 ? body.slice(0, cut + 1).trim() : body).replace(/[；;，,]$/, '。')
  const advice = cut >= 0 ? body.slice(cut + 1).trim() : ''
  const title = titled ? head[1].trim() : /查询|SQL|数据库/i.test(plain) ? '查询超时' : /工具/.test(plain) ? '工具超时' : '等待超时'
  return {
    title,
    reason,
    // 查询等多久归数据源管（查询时限），画布上的节点没有这一项
    action: advice ? endStop(advice)
      : /查询|SQL/i.test(title) ? '请缩小查询范围（添加 WHERE 或 LIMIT）；如确需更长时间，请在「数据」页调大该数据源的查询时限，然后继续运行。'
        : '可直接继续运行；如反复超时，请在画布中调大该节点的超时时间。',
    continuable: true,
    raw,
  }
}

/**
 * 工具库里存着的坏参数定义（后端 tools.custom.broken_tool_message）。一个节点绑了几个
 * 坏工具时后端用「；」连成一句；单个工具的原因里自己也可能带「；」（「…是不是想写
 * integer；只能是 …」），所以只在下一段「自定义工具「」开头的地方断开
 */
const BROKEN_TOOL = /自定义工具「([^」]+)」的(参数定义[\s\S]*?)(?:[。.]\s*到「工具」页[^；]*)?(?=；\s*自定义工具「|$)/g

function explainBrokenTools(plain: string, raw: string): RunErrorExplain | null {
  const found = new Map<string, string>()
  for (const m of plain.matchAll(BROKEN_TOOL)) if (!found.has(m[1])) found.set(m[1], m[2].trim())
  if (!found.size) return null
  const [[first, problem], ...rest] = [...found]
  const base = { continuable: true, fix: 'tools' as const, fixFirst: true, fixTo: customToolEditPath(first), raw }
  if (!rest.length) {
    return {
      ...base,
      title: `自定义工具「${first}」的参数定义有误`,
      reason: `${endStop(problem)}绑定该工具的节点运行时必然失败。`,
      action: '请前往「工具」页打开该工具，修正参数定义并保存，然后继续运行；已完成的节点不会重新执行。',
    }
  }
  // 只报第一个的话，人改好它、接着跑，又在第二个上失败一次
  const names = [...found.keys()].map((n) => `「${n}」`).join('、')
  return {
    ...base,
    title: `${found.size} 个自定义工具的参数定义有误`,
    reason: `${[...found].map(([n, p]) => `「${n}」：${p.replace(/[。.]$/, '')}`).join('；')}。绑定这些工具的节点运行时必然失败。`,
    action: `请前往「工具」页修正${names}的参数定义并保存，全部修正后继续运行；已完成的节点不会重新执行。`,
  }
}

export function explainRunError(error: string | null | undefined, detail?: string | null): RunErrorExplain {
  const raw = [error, detail && detail !== error ? detail : null].filter(Boolean).join('\n\n')
  const text = String(error ?? '').trim()
  const plain = stripClassPrefix(text)

  if (!text) {
    return { title: '运行失败，未记录原因', action: '请打开「原始事件」查看最后几条记录。', continuable: true, raw }
  }
  // 整形节点按 JSON 解析失败（后端 io._json_error）。两种原话：出错的位置落在上游模型写的文字里时
  // 点名上游（模板没错，别让人去查模板）；否则是模板本身写错了。两种都是图的问题：原样接着跑
  // 拿到的还是同一段文字、同一份模板，只会再失败一次，所以不给「接着跑」，指到画布上去改
  // 标题、原因、怎么办都照原话切，不改写：展开区据此认出原话已经说完了，不再整段贴一遍。
  // 放在最前：原话里带着用户起的节点名（「查询超时订单」「额度查询」），排在后面会被超时、限流、
  // 连不上那几条按整句关键词抢走，又把「接着跑」给回来
  const upstream = plain.match(/^(上游「([^」]+)」输出的不是合法 JSON（[^）]*）)[，,]\s*([^；;]*)[；;]\s*([\s\S]+)$/)
  if (upstream) {
    return {
      title: upstream[1],
      reason: `${endStop(upstream[3].trim())}模板本身没有错误。`,
      action: `${endStop(upstream[4].trim())}直接继续运行仍会得到同一段文字，将再次失败。`,
      continuable: false,
      fix: 'canvas',
      // 要改的是写出这段文字的上游，不是整形节点：入口指到上游去
      fixNode: upstream[2],
      raw,
    }
  }
  const badJson = plain.match(/^(模板渲染出来的不是合法 JSON（[^）]*）)[。.]?\s*([\s\S]*)$/)
  if (badJson) {
    const hint = badJson[2].trim()
    // 后端点名了模板里是哪一处、该怎么改（加 | json、去掉引号、取出来是空的）：照它说，
    // 不再追加「改用 output_schema」——那句指的是另一条路，和它给的改法对不上。
    // 只有老的笼统说法（或者什么都没说）才补这一句
    // 运行时（io.py）写「模板里 {{ x }}」，画布校验（schema.py）写「模板中的 {{ x }}」：两种都认
    const pointed = /^模板[里中]的? ?\{\{/.test(hint)
    return {
      title: badJson[1],
      action: `${endStop(hint || TEMPLATE_HINT)}`
        + (pointed ? '修改后重新运行。' : '模板中插入的是 Agent 或模型生成的文字时，建议在节点中改用「结构化输出 Schema」和「按出处核对字段」，让其返回结构化数据。修改后重新运行。'),
      continuable: false,
      fix: 'canvas',
      raw,
    }
  }
  // 发起就被拒：绑定的工具在本机不存在（POST /runs 422 run_tool_missing）。这次运行根本没开始，
  // 谈不上接着跑；去接入，或者回画布重新选
  const missingTool = explainToolMissing(plain, raw)
  if (missingTool) return missingTool
  // 工具库里存着的坏参数定义。它不在这次运行的快照里：到工具页改好，回来原样接着跑
  // 就能过；不先改，接着跑还是同样的失败
  const broken = explainBrokenTools(plain, raw)
  if (broken) return broken
  // 下面四类是「图本身有缺口」：模型没真调工具、团队轮数用完、校验修复想凑数、
  // 提示词点名的工具没绑定。原样接着跑是同一份配置，只会再来一遍，所以都不给
  // 「接着跑」，指到画布上去改
  // 「没有真正调用工具」是以前的说法，现在是「未实际调用工具」
  if (/没有真正调用工具|未实际调用工具|工具调用的原始标记|tool_markup_leak/.test(text)) {
    return {
      title: '模型未实际调用工具',
      reason: '模型以文本形式输出了工具调用，本步骤未查询到任何数据。常见原因是节点没有绑定工具，或模型、服务不支持工具调用。',
      action: '请在画布中确认该节点已绑定所需工具；如已绑定仍出现此问题，请换用支持工具调用的模型。修改后重新运行。',
      continuable: false,
      fix: 'canvas',
      raw,
    }
  }
  const exhausted = plain.match(/用完\s*(\d+)\s*轮(?:仍|还)?未完成[：:]?\s*(.*)/s)
  if (exhausted || /team_exhausted/.test(text)) {
    // 后端原话在理由和没派到的成员后面还跟着自己的建议（「。先看成员…」「。按降档交付：…」），
    // 那部分由下面的 action 说，原因里再留一遍就是同样的话说两遍
    const why = exhausted?.[2]?.split(/。\s*(?:先看成员|按降档交付)/)[0].replace(/[。\s]+$/, '').trim()
    return {
      title: exhausted ? `协作团队用完 ${exhausted[1]} 轮仍未完成` : '协作团队用完了轮数仍未完成',
      reason: why || '调度者始终未判定完成，成员的输出不能作为结论交付。',
      action: '请在画布中检查成员是否绑定了所需工具，并调大该团队的「最多轮数」；也可将「用完轮数时」改为「降档交付」。',
      continuable: false,
      fix: 'canvas',
      raw,
    }
  }
  if (/原文没有的值|修复.*编造/.test(text)) {
    const values = plain.match(/原文没有的值[：:]\s*([^\n。]+)/)?.[1]?.trim()
    return {
      title: '校验修复未采用：修复结果中出现了原文没有的值',
      reason: `上游输出中没有这些数据${values ? `（${values}）` : ''}，修复不能补造数据，本次校验判定为失败。`,
      action: '请先检查上游节点为何未获取到数据（常见原因是未绑定查询工具），修改后重新运行。',
      continuable: false,
      fix: 'canvas',
      raw,
    }
  }
  // 节点和协作成员两种说法：「…但节点没有绑定它」「成员「X」的…但没有给这个成员绑定它」
  const unbound = plain.match(/提示词要求(?:使)?用\s*「?([^」，,\s]+?)」?\s*[，,]?\s*但(?:节点)?(?:没有|未)(?:给这个成员)?绑定/)
  if (unbound) {
    const member = plain.match(/成员「([^」]+)」的提示词/)?.[1]
    return {
      title: member
        ? `成员「${member}」的提示词要求使用「${unbound[1]}」，但该成员未绑定此工具`
        : `提示词要求使用「${unbound[1]}」，但节点未绑定此工具`,
      reason: '运行时模型无法调用该工具，可能会生成虚构的结果。',
      action: '请在画布中为该节点绑定此工具，或删除提示词中的这项要求。',
      continuable: false,
      fix: 'canvas',
      raw,
    }
  }
  // 后端有一条路径以前写「人工拒绝：」（现在统一为「人工驳回：」），历史运行里两种都有
  if (/人工(?:驳回|拒绝)/.test(text)) {
    return {
      title: '人工审批驳回，运行终止',
      reason: plain.replace(/^人工(?:驳回|拒绝)[：:]?\s*/, '') || undefined,
      action: '这是审批人的决定，不是故障。如有需要，请修改内容后重新发起运行。',
      continuable: false,
      raw,
    }
  }
  const missing = plain.match(/缺少必填输入[：:]\s*([^\s，。,]+)/)
  if (missing) {
    return {
      title: `缺少必填输入「${missing[1]}」`,
      reason: '发起本次运行时未填写此项，输入节点已拦截。',
      action: '继续运行仍使用同一份输入，将再次失败。请补填此项后重新运行，其余输入保持不变。',
      continuable: false,
      fix: 'rerun',
      missingInput: missing[1],
      raw,
    }
  }
  if (AUTH.test(text)) {
    return {
      title: '模型鉴权失败',
      reason: /invalid_model/i.test(text)
        ? 'API Key 无权使用该模型，或模型名称有误（401）。'
        : 'API Key 无效、已过期，或无权使用该模型。',
      action: '请前往「设置 → 模型接入」检查 API Key 和模型名称，修改后继续运行；已完成的节点不会重新执行。',
      continuable: true,
      fix: 'settings',
      raw,
    }
  }
  if (MISSING_MODEL.test(text)) {
    const model = text.match(/model:\s*([\w.:/-]+)/)?.[1]
    return {
      title: model ? `模型「${model}」不存在` : '模型不存在',
      reason: '模型服务中不存在该模型名（404），可能拼写有误或已下线。',
      action: '请在「设置 → 模型接入」中换用可用的模型，或在画布中修改该节点的模型，然后继续运行。',
      continuable: true,
      fix: 'settings',
      raw,
    }
  }
  if (RATE.test(text)) {
    return {
      title: '请求过于频繁，或额度已用完',
      reason: '模型服务触发了限流（429）。',
      action: '请稍后继续运行；如额度已用完，请充值或更换模型。',
      continuable: true,
      raw,
    }
  }
  if (TIMEOUT.test(text)) {
    if (isZhProse(plain)) return explainZhTimeout(plain, raw)
    return {
      title: '等待超时',
      reason: '下游服务未在限定时间内响应，通常是暂时性问题。',
      action: '可直接继续运行；如反复超时，请在画布中调大该节点的超时时间。',
      continuable: true,
      raw,
    }
  }
  // 后端原话「连不上对方的服务」；文案整改后改为「无法连接…」，两种都认
  if (/连不上|无法连接|Connection ?(?:Error|refused)|ECONNREFUSED/i.test(text)) {
    return {
      title: '无法连接外部服务',
      reason: '网络不通，或数据库、MCP 服务、模型接口未启动。',
      action: '请确认外部服务已启动且地址可访问，然后继续运行；已完成的节点不会重新执行。',
      continuable: true,
      raw,
    }
  }
  if (SERVER.test(text)) {
    return {
      title: '外部服务返回错误',
      reason: '模型或工具服务返回了服务器错误（5xx），通常是暂时性问题。',
      action: '请稍后继续运行。',
      continuable: true,
      raw,
    }
  }
  if (/不允许出现 Set|\{\{\s*[\w.]+\s*\}\}.*表达式/.test(text)) {
    // 后端 expressions 已经把 {{ x }} 当 x 读了：这类老失败原图接着跑就能过，
    // 不该再让人去改表达式
    return {
      title: '条件表达式使用了 {{ x }} 写法',
      reason: '当前版本已兼容此写法。',
      action: '无需修改工作流，可直接继续运行。',
      continuable: true,
      raw,
    }
  }
  if (/表达式语法错误|invalid syntax/.test(text)) {
    return {
      title: '条件表达式有误',
      reason: plain.split('：').slice(1).join('：') || undefined,
      action: '请在画布中修改该节点的条件，然后在画布中重新运行。在此处继续运行仍使用原来的条件，将再次失败。',
      continuable: false,
      fix: 'canvas',
      raw,
    }
  }
  if (/unexpected keyword argument|参数对不上|参数不匹配/.test(text)) {
    const arg = text.match(/argument '(\w+)'|名为「(\w+)」/)
    return {
      title: '工具参数不匹配',
      reason: `工具不接受${arg ? `名为「${arg[1] ?? arg[2]}」的` : '传入的'}参数，可能是工具版本较旧所致。`,
      action: '可先继续运行；如仍失败，请在画布中检查该节点的工具配置。',
      continuable: true,
      fix: 'canvas',
      raw,
    }
  }
  if (/找不到工具|tool .*not found/i.test(text)) {
    const name = text.match(/找不到工具\s*'?"?([\w.-]+)/)
    return {
      title: `找不到工具${name ? `「${name[1]}」` : ''}`,
      reason: '该工具可能已被删除、重命名，或所在的 MCP 服务未连接。',
      action: '请前往「工具」页确认该工具是否存在，或在画布中为该节点更换工具。',
      continuable: true,
      fix: 'tools',
      raw,
    }
  }
  // 「至少要配」是以前的说法，现在是「至少需要配置」
  if (/至少要配|至少需要配置|没有配置|未配置|必须配置/.test(text)) {
    return {
      title: '节点配置不完整',
      reason: plain,
      action: '请在画布中完善该节点的配置。继续运行仍使用原来的配置，将再次失败；在画布中修改配置后可重新运行。',
      continuable: false,
      fix: 'canvas',
      raw,
    }
  }
  if (/Can receive only one value per step|InvalidUpdateError/.test(text)) {
    const key = text.match(/At key '(\w+)'/)?.[1]
    return {
      title: `并行节点在同一步中写入了同一变量${key ? `「${key}」` : ''}`,
      reason: '多条并行分支在同一步中各自写入了该变量，无法确定保留哪个值。',
      action: '请在画布中检查这些并行分支的汇合方式：让各分支写入不同的变量，或先汇合再写入。修改连线后需重新运行。',
      continuable: false,
      fix: 'canvas',
      raw,
    }
  }
  if (/unsupported operand|can only concatenate|not supported between/.test(text)) {
    return {
      title: '数据类型不匹配',
      reason: stripClassPrefix(plain) || undefined,
      action: '通常是代码或表达式把两种不同类型的值拼接在一起（如布尔值与字符串）。请在画布中检查该节点；直接继续运行仍会报同样的错误。',
      continuable: false,
      fix: 'canvas',
      raw,
    }
  }
  if (/结构化输出失败/.test(text)) {
    return {
      title: '结构化输出失败',
      reason: `模型未能按规定的字段结构输出${/Unsupported function/i.test(text) ? '：该模型不支持此输出结构' : ''}。`,
      action: '请在画布中简化该节点的输出结构（减少嵌套层级和字段数量），或换用支持结构化输出的模型。',
      continuable: false,
      fix: 'canvas',
      raw,
    }
  }
  if (/exceeds 64-bit|OverflowError|too large to convert/i.test(text)) {
    return {
      title: '数字超出整数范围',
      reason: '某个节点输出的整数在写入数据库或传给下游时溢出（64 位）。',
      action: '常见于把 ID、时间戳当作数字计算。请在画布中检查输出该数字的节点，改为字符串后重新运行。',
      continuable: false,
      fix: 'canvas',
      raw,
    }
  }
  // 「没有可恢复的断点，可能在服务重启前尚未开始执行」也提到服务重启，但它没有断点可继续，不归这一类
  if (/服务重启/.test(text) && !/没有可恢复的断点/.test(text)) {
    return {
      title: '服务重启中断了本次运行',
      reason: plain,
      action: '断点已保留，可直接继续运行；已完成的节点不会重新执行。',
      continuable: true,
      raw,
    }
  }
  // 兜底：句中还夹着的异常类名（「结构化输出失败：ValueError: …」）也去掉
  const h = humanizeError(plain)
  const scrub = (v?: string) => v?.replace(/\b[A-Z][A-Za-z]*(?:Error|Exception)\s*:\s*/g, '')
  return { title: scrub(h.title) ?? h.title, reason: scrub(h.reason), action: h.action, continuable: true, raw: raw || h.raw || text }
}
