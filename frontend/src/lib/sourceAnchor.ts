/**
 * 指名数据源页上的一张卡片的地址：<数据源页>#source-<id>，带 &maintenance 时顺带打开那张卡片的「定时维护」。
 *
 * 全站只认这一种写法，拼、拆都在这里：目录页后台任务条上探查的「查看」、定时任务页上维护定时的「去数据源页设置」都链到这里。
 * 两边各写一遍的话改了一边另一边就落空——复核 W4R-04 就是这样：定时任务页链的是 ?source=<id>，数据源页只认 #source-，
 * 点了落在列表顶上，既不滚到那张卡片也不打开定时维护。
 *
 * 用 #（不用查询串）：页内的元素 id 就是 source-<id>，浏览器自己也认这个锚点；数据源页的两个标签（数据库 / 表格）共用一个组件，
 * 查询串留给页面别的状态。
 */
export type SourceAnchorOpen = 'maintenance'

const PREFIX = 'source-'
const OPENS: readonly SourceAnchorOpen[] = ['maintenance']

/** 页内卡片的元素 id */
export const sourceCardId = (id: string) => `${PREFIX}${id}`

/** 地址里的 # 部分（带 #）：sourceAnchor(id) 只滚过去，sourceAnchor(id, 'maintenance') 还打开定时维护 */
export const sourceAnchor = (id: string, open?: SourceAnchorOpen) =>
  `#${PREFIX}${encodeURIComponent(id)}${open ? `&${open}` : ''}`

/** 拆开地址里的 # 部分；不是指名卡片的（没有 #、别的锚点、编码坏了）给 null。认不得的标记不管，只当滚过去 */
export function parseSourceAnchor(hash: string): { id: string; open: SourceAnchorOpen | null } | null {
  const [head, ...flags] = hash.replace(/^#/, '').split('&')
  if (!head.startsWith(PREFIX) || head.length === PREFIX.length) return null
  let id: string
  try {
    id = decodeURIComponent(head.slice(PREFIX.length))
  } catch {
    return null
  }
  return { id, open: OPENS.find((o) => flags.includes(o)) ?? null }
}
