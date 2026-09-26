/**
 * 失败运行的「为什么 + 怎么办」。
 *
 * 新后端的 run.error 已经是人话（engine/errors.py），但库里大量老运行留的是原始
 * 异常：「NodeError: AnthropicModelNotFoundError: Error code: 404 - {…}」「_make_
 * query_tool.<locals>._run() got an unexpected keyword argument」。排错的人要的是
 * 哪个节点、为什么、下一步点哪里，所以这里按库里真实出现过的失败归类，并说清
 * 「接着跑」有没有用——输入缺了、人驳回了，原样接着跑只会再失败一次，那就不给
 * 这个按钮。
 *
 * 归类之外的一律交给 lib/errors 的 humanizeError（去掉异常类名、拆标题和原因）。
 * 这份规则应当和助手流的报错行共用，先放在记录页，见 requests。
 */

import { humanizeError } from '../../lib/errors'

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

export function explainRunError(error: string | null | undefined, detail?: string | null): RunErrorExplain {
  const raw = [error, detail && detail !== error ? detail : null].filter(Boolean).join('\n\n')
  const text = String(error ?? '').trim()
  const plain = stripClassPrefix(text)

  if (!text) {
    return { title: '运行失败，但没有留下原因', action: '打开「原始事件」看最后几条记录。', continuable: true, raw }
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
  if (/timed? ?out|timeout|超时/i.test(text)) {
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
      action: '不用改图，直接接着跑。',
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
