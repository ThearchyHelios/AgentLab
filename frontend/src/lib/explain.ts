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
const MISSING_MODEL = new RegExp(`model ?not ?found|not_found_error|模型不存在|(?:${http('404')}).*model`, 'i')
const SERVER = new RegExp(
  `${http(String.raw`5\d\d`)}|对方服务出错|Internal Server Error|Bad Gateway|Service Unavailable|Gateway Time-?out|overloaded`, 'i')

/** 「NodeError: AnthropicModelNotFoundError: …」这种套了几层的类名前缀全剥掉 */
const CLASS_PREFIX = /^\s*(?:[a-z_][\w.]*\.)?[A-Z]\w*(?:Error|Exception|Exit|Timeout)\s*:\s*/

export function stripClassPrefix(text: string): string {
  let s = text
  for (let i = 0; i < 4 && CLASS_PREFIX.test(s); i++) s = s.replace(CLASS_PREFIX, '')
  return s.trim()
}

/** 后端 io.TEMPLATE_HINT：模板渲染出来不是合法 JSON、又说不出是哪一处时的老说法 */
const TEMPLATE_HINT = '检查模板里的引号、逗号，字符串值要用 | json 过滤器输出'

/** 后端 app/api/runs.py 的 TOOL_MISSING：发起运行时发现绑定的工具在本机不存在 */
export const RUN_TOOL_MISSING = 'run_tool_missing'
const DATA_TOOL = /^db_(?:query|schema)__/

/**
 * 「绑定的工具在本机不存在：「X」（调用工具）绑的 db_query__nope；「团队」的成员「研究员」绑的 mcp:a/b。
 * 去数据页接入，或在节点里重新选」。数据源工具（db_query__ / db_schema__）要去数据页接入，自定义和
 * MCP 工具去工具页；两种都缺时入口按第一个缺的走，话里两边都说到
 */
function explainToolMissing(plain: string, raw: string, coded = false): RunErrorExplain | null {
  const m = plain.match(/^绑定的工具在本机不存在[：:]\s*([\s\S]+)$/)
  // 带着机读码、话却改了说法：照样按这一类讲，名字认不出就只给工具页
  if (!m && !coded) return null
  const body = (m ? m[1] : plain).trim()
  const cut = body.lastIndexOf('。')
  const list = (cut >= 0 ? body.slice(0, cut) : body).trim()
  const advice = cut >= 0 ? body.slice(cut + 1).trim() : ''
  // 超过 8 处时后端在最后一个名字后面接「等 N 处」：那个「等」不是名字的一部分
  const names = [...list.matchAll(/绑的\s*([^\s；;，,。]+)/g)].map((x) => x[1].replace(/等$/, '')).filter(Boolean)
  const first = names[0] ?? ''
  const others = names.filter((n) => !DATA_TOOL.test(n))
  const toData = DATA_TOOL.test(first) || (!names.length && /数据页/.test(advice))
  const fixTo = toData ? '/data'
    : !others.length ? '/tools'
    : others.every((n) => n.startsWith('mcp:')) ? '/tools/mcp'
    : others.every((n) => !n.startsWith('mcp:')) ? '/tools/custom' : '/tools'
  return {
    title: names.length === 1 ? `绑定的工具「${first}」在本机不存在` : '绑定的工具在本机不存在',
    reason: `${endStop(list)}这次运行没有发起，前面的节点一个都没跑。`,
    action: `${endStop(advice || (toData ? '去数据页接入，或在节点里重新选' : '去工具页接入，或在节点里重新选'))}接入之后重新运行。`,
    continuable: false,
    fix: 'tools',
    fixTo,
    fixLabel: toData ? '去数据页接入' : '去工具页接入',
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
  if (code !== RUN_TOOL_MISSING && !/^绑定的工具在本机不存在/.test(text)) return null
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
      : /查询|SQL/i.test(title) ? '缩小查询范围（加 WHERE / LIMIT）；确实要跑更久，就到「数据」页把这个库的查询时限调大，再接着跑。'
        : '直接接着跑；反复超时就到画布里调大这个节点的超时。',
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
      title: `自定义工具「${first}」的参数定义写坏了`,
      reason: `${endStop(problem)}绑了这个工具的节点运行时一定失败。`,
      action: '到「工具」页打开它，把参数定义改好保存；回来接着跑就能过，前面跑完的节点不会重跑。',
    }
  }
  // 只报第一个的话，人改好它、接着跑，又在第二个上失败一次
  const names = [...found.keys()].map((n) => `「${n}」`).join('、')
  return {
    ...base,
    title: `${found.size} 个自定义工具的参数定义写坏了`,
    reason: `${[...found].map(([n, p]) => `「${n}」：${p.replace(/[。.]$/, '')}`).join('；')}。绑了这些工具的节点运行时一定失败。`,
    action: `到「工具」页把${names}的参数定义都改好保存；全改好再接着跑，前面跑完的节点不会重跑。`,
  }
}

export function explainRunError(error: string | null | undefined, detail?: string | null): RunErrorExplain {
  const raw = [error, detail && detail !== error ? detail : null].filter(Boolean).join('\n\n')
  const text = String(error ?? '').trim()
  const plain = stripClassPrefix(text)

  if (!text) {
    return { title: '运行失败，但没有留下原因', action: '打开「原始事件」看最后几条记录。', continuable: true, raw }
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
      reason: `${endStop(upstream[3].trim())}模板本身没有写错。`,
      action: `${endStop(upstream[4].trim())}原样接着跑拿到的还是同一段文字，会再失败一次。`,
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
    const pointed = hint.startsWith('模板里 {{')
    return {
      title: badJson[1],
      action: `${endStop(hint || TEMPLATE_HINT)}`
        + (pointed ? '改完再运行。' : '模板里插的是 agent / 模型写的文字时，改用 output_schema + cite_fields 让它交结构化数据。改完再运行。'),
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
  if (/没有真正调用工具|工具调用的原始标记|tool_markup_leak/.test(text)) {
    return {
      title: '模型没有真正调用工具',
      reason: '它把工具调用当成文字写了出来，这一步一次都没查到数据。常见原因是节点没有绑定工具，或者模型、服务不支持工具调用。',
      action: '到画布里确认这个节点绑定了要用的工具；绑定了还这样，就换一个支持工具调用的模型。改完再运行。',
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
      reason: why || '调度者一直没有判定完成，成员的原话不能当作结论交出去。',
      action: '到画布里看看成员有没有绑定要用的工具，再调大这个团队的最多轮数；也可以把「用完轮数时」改成降档交付。',
      continuable: false,
      fix: 'canvas',
      raw,
    }
  }
  if (/原文没有的值|修复.*编造/.test(text)) {
    const values = plain.match(/原文没有的值[：:]\s*([^\n。]+)/)?.[1]?.trim()
    return {
      title: '校验修复被拒绝：修复结果里出现了原文没有的值',
      reason: `上游产出里没有这些数据${values ? `（${values}）` : ''}，修复不许凑数，这次校验判为失败。`,
      action: '先看上游节点为什么没拿到数据（常见是没有绑定查询工具），改好后重新运行。',
      continuable: false,
      fix: 'canvas',
      raw,
    }
  }
  // 节点和协作成员两种说法：「…但节点没有绑定它」「成员「X」的…但没有给这个成员绑定它」
  const unbound = plain.match(/提示词要求用\s*「?([^」，,\s]+?)」?\s*[，,]?\s*但(?:节点)?没有(?:给这个成员)?绑定/)
  if (unbound) {
    const member = plain.match(/成员「([^」]+)」的提示词/)?.[1]
    return {
      title: member
        ? `成员「${member}」的提示词要求用「${unbound[1]}」，但没有给它绑定`
        : `提示词要求用「${unbound[1]}」，但节点没有绑定它`,
      reason: '运行时模型拿不到这个工具，只能编一个结果出来。',
      action: '到画布里给这个节点绑定该工具，或者改掉提示词里的要求。',
      continuable: false,
      fix: 'canvas',
      raw,
    }
  }
  if (/人工驳回/.test(text)) {
    return {
      title: '人工审批驳回，运行终止',
      reason: plain.replace(/^人工驳回[：:]?\s*/, '') || undefined,
      action: '这是审批人的决定，不是故障。需要的话改好内容后重新发起一次运行。',
      continuable: false,
      raw,
    }
  }
  const missing = plain.match(/缺少必填输入[：:]\s*([^\s，。,]+)/)
  if (missing) {
    return {
      title: `缺少必填输入「${missing[1]}」`,
      reason: '发起这次运行时没有填这一项，输入节点直接拦下了。',
      action: '接着跑还是同一份输入，会再失败一次。补上这一项重新运行，其余输入照旧。',
      continuable: false,
      fix: 'rerun',
      missingInput: missing[1],
      raw,
    }
  }
  if (AUTH.test(text)) {
    return {
      title: '模型鉴权没通过',
      reason: /invalid_model/i.test(text)
        ? 'key 没有这个模型的权限，或者模型名写错了（401）。'
        : 'key 无效、过期，或者没有这个模型的权限。',
      action: '去 设置 → 模型接入 检查 key 和模型名，改好后「接着跑」，前面跑完的节点不会重跑。',
      continuable: true,
      fix: 'settings',
      raw,
    }
  }
  if (MISSING_MODEL.test(text)) {
    const model = text.match(/model:\s*([\w.:/-]+)/)?.[1]
    return {
      title: model ? `模型「${model}」不存在` : '模型不存在',
      reason: '供应商那边没有这个模型名（404），可能拼错了或已经下线。',
      action: '在 设置 → 模型接入 换一个可用的模型，或者到画布里改这个节点的模型，再接着跑。',
      continuable: true,
      fix: 'settings',
      raw,
    }
  }
  if (RATE.test(text)) {
    return {
      title: '请求太频繁，或额度用完了',
      reason: '模型供应商限了流（429）。',
      action: '等一会儿直接接着跑；额度用完就去供应商那边充值或换一个模型。',
      continuable: true,
      raw,
    }
  }
  if (TIMEOUT.test(text)) {
    if (isZhProse(plain)) return explainZhTimeout(plain, raw)
    return {
      title: '等待超时',
      reason: '下游服务没在限定时间内响应，多半是暂时的。',
      action: '直接接着跑；反复超时就到画布里调大这个节点的超时。',
      continuable: true,
      raw,
    }
  }
  if (/连不上|Connection ?(?:Error|refused)|ECONNREFUSED/i.test(text)) {
    return {
      title: '连不上对方服务',
      reason: '网络不通，或者数据库、MCP、模型接口没有启动。',
      action: '确认对方服务起着、地址能通，再接着跑；前面跑完的节点不会重跑。',
      continuable: true,
      raw,
    }
  }
  if (SERVER.test(text)) {
    return {
      title: '对方服务出错了',
      reason: '模型或工具那一端返回了服务器错误（5xx），多半是暂时的。',
      action: '等一会儿直接接着跑。',
      continuable: true,
      raw,
    }
  }
  if (/不允许出现 Set|\{\{\s*[\w.]+\s*\}\}.*表达式/.test(text)) {
    // 后端 expressions 已经把 {{ x }} 当 x 读了：这类老失败原图接着跑就能过，
    // 不该再让人去改表达式
    return {
      title: '条件表达式用了旧写法',
      reason: '当时的版本不认 {{ x }} 这种写法；现在已经自动兼容。',
      action: '不用改工作流，直接接着跑。',
      continuable: true,
      raw,
    }
  }
  if (/表达式语法错误|invalid syntax/.test(text)) {
    return {
      title: '条件表达式写错了',
      reason: plain.split('：').slice(1).join('：') || undefined,
      action: '到画布里改这个节点的条件，改完从画布接着跑。这里接着跑用的还是当时那条写错的条件，会再失败一次。',
      continuable: false,
      fix: 'canvas',
      raw,
    }
  }
  if (/unexpected keyword argument|参数对不上/.test(text)) {
    const arg = text.match(/argument '(\w+)'|名为「(\w+)」/)
    return {
      title: '工具参数对不上',
      reason: `工具不接受${arg ? `名为「${arg[1] ?? arg[2]}」的` : '传进去的'}参数，多半是旧版本工具的问题。`,
      action: '先直接接着跑试试；还不行就到画布里检查这个节点的工具配置。',
      continuable: true,
      fix: 'canvas',
      raw,
    }
  }
  if (/找不到工具|tool .*not found/i.test(text)) {
    const name = text.match(/找不到工具\s*'?"?([\w.-]+)/)
    return {
      title: `找不到工具${name ? `「${name[1]}」` : ''}`,
      reason: '它可能被删了、改了名，或者所在的 MCP 服务没连上。',
      action: '去工具库确认它还在，或者在画布里给这个节点换一个工具。',
      continuable: true,
      fix: 'tools',
      raw,
    }
  }
  if (/至少要配|没有配置|未配置|必须配置/.test(text)) {
    return {
      title: '节点配置不完整',
      reason: plain,
      action: '到画布里把这个节点配好。接着跑用的还是当时的配置，会再失败一次；画布里改完配置可以直接接着跑。',
      continuable: false,
      fix: 'canvas',
      raw,
    }
  }
  if (/Can receive only one value per step|InvalidUpdateError/.test(text)) {
    const key = text.match(/At key '(\w+)'/)?.[1]
    return {
      title: `同一拍里有两个节点同时写了${key ? `变量「${key}」` : '同一个变量'}`,
      reason: '并行的分支在同一步里各自往它写了一个值，执行图不知道该留哪个。',
      action: '到画布里检查这几条并行分支的汇合：让它们写不同的变量，或者先汇合再写。改了连线要重新运行。',
      continuable: false,
      fix: 'canvas',
      raw,
    }
  }
  if (/unsupported operand|can only concatenate|not supported between/.test(text)) {
    return {
      title: '数据类型对不上',
      reason: stripClassPrefix(plain) || undefined,
      action: '多半是代码或表达式把两种类型拼在了一起（比如布尔值加字符串）。到画布里检查这个节点；原样接着跑还会再报同样的错。',
      continuable: false,
      fix: 'canvas',
      raw,
    }
  }
  if (/结构化输出失败/.test(text)) {
    return {
      title: '结构化输出失败',
      reason: `模型没能按规定的字段结构回答${/Unsupported function/i.test(text) ? '：这个模型不支持这种输出结构' : ''}。`,
      action: '到画布里简化这个节点的输出结构（少几层嵌套、少几个字段），或者换一个支持结构化输出的模型。',
      continuable: false,
      fix: 'canvas',
      raw,
    }
  }
  if (/exceeds 64-bit|OverflowError|too large to convert/i.test(text)) {
    return {
      title: '数字太大，超出了整数的范围',
      reason: '某个节点产出的整数在写库或传给下游时溢出了（64 位）。',
      action: '常见于把 ID、时间戳当数字来算。到画布里检查产出这个数的节点，改成字符串后重新运行。',
      continuable: false,
      fix: 'canvas',
      raw,
    }
  }
  if (/服务重启/.test(text)) {
    return {
      title: '服务重启打断了这次运行',
      reason: plain,
      action: '断点还在，接着跑即可；前面跑完的节点不会重跑。',
      continuable: true,
      raw,
    }
  }
  // 兜底：句中还夹着的异常类名（「结构化输出失败：ValueError: …」）也去掉
  const h = humanizeError(plain)
  const scrub = (v?: string) => v?.replace(/\b[A-Z][A-Za-z]*(?:Error|Exception)\s*:\s*/g, '')
  return { title: scrub(h.title) ?? h.title, reason: scrub(h.reason), action: h.action, continuable: true, raw: raw || h.raw || text }
}
