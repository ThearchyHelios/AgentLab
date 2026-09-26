import { useMemo } from 'react'
import type { Edge } from '@xyflow/react'
import { useStudio } from '../store/studio'

/**
 * 变量血缘画在画布上。悬停变量（停靠栏里的一行、模板里的一枚胶囊）时：产出它的节点
 * 实线描边、引用它的虚线描边，两者之间的走线提亮，其余节点和线退到 45%。
 *
 * 排查「为什么这里取到的是空值」，以前得在抽屉、检查器、画布三处之间来回对照；
 * 这个产品的卖点就是看得见的数据流，偏偏缺了「数据从哪流到哪」这一眼。
 *
 * 画法是一段按 data-id 生成的样式，不给卡片和边加 props：lineage 一变只换这一段文字，
 * 大图上也不用让每张卡片各自订阅、各自重渲染。压暗走卡片自己的 --nc-dim、线色走
 * --sf-edge，和卡片、走线原有的过渡和 reduced-motion 规则一起生效，不另起动画。
 * 画布要自己画血缘的话，在 .react-flow 上写 data-lineage-native，这一层就让开。
 */
export function LineageLayer() {
  const lineage = useStudio((s) => s.lineage)
  const edges = useStudio((s) => s.edges)
  const css = useMemo(() => (lineage ? lineageCss(lineage, edges) : ''), [lineage, edges])
  return css ? <style data-lineage={lineage!.var}>{css}</style> : null
}

type Lineage = { var: string; producers: string[]; consumers: string[] }

const ROOT = '.react-flow:not([data-lineage-native])'
const q = (s: string) => `"${s.replace(/["\\]/g, '\\$&')}"`
const ids = (list: Iterable<string>) => [...list].map((id) => `[data-id=${q(id)}]`).join(',')

/**
 * 产出者和引用者之间往往隔着好几跳，只取直连边会断成几截。一条边 u→v 在某条
 * 「产出 → … → 引用」的路上，当且仅当 u 能从产出者走到、v 能走到引用者
 */
export function lineagePath(lineage: Lineage, edges: Pick<Edge, 'id' | 'source' | 'target'>[]) {
  const out = new Map<string, string[]>()
  const inc = new Map<string, string[]>()
  for (const e of edges) {
    out.set(e.source, [...(out.get(e.source) ?? []), e.target])
    inc.set(e.target, [...(inc.get(e.target) ?? []), e.source])
  }
  const walk = (seeds: string[], next: Map<string, string[]>) => {
    const seen = new Set(seeds)
    const stack = [...seeds]
    while (stack.length) {
      for (const n of next.get(stack.pop()!) ?? []) {
        if (!seen.has(n)) { seen.add(n); stack.push(n) }
      }
    }
    return seen
  }
  const down = walk(lineage.producers, out)
  const up = walk(lineage.consumers, inc)
  const path = lineage.producers.length && lineage.consumers.length
    ? edges.filter((e) => down.has(e.source) && up.has(e.target))
    : []
  const through = new Set<string>([...lineage.producers, ...lineage.consumers])
  for (const e of path) { through.add(e.source); through.add(e.target) }
  return { path, through }
}

function lineageCss(lineage: Lineage, edges: Edge[]): string {
  const { path, through } = lineagePath(lineage, edges)
  if (!through.size) return ''
  // 标签按缩放反向补回来：缩到 0.4 时卡片上的 10px 只剩 4px，看不出写的是什么
  const tag = `position: absolute; bottom: 100%; left: 0; margin-bottom: calc(4px / var(--zoom, 1));
    padding: 0 calc(5px / var(--zoom, 1)); border-radius: calc(3px / var(--zoom, 1));
    font: 600 min(calc(10px / var(--zoom, 1)), 26px) / 1.45 var(--font-sans); white-space: nowrap; pointer-events: none;`
  const rules = [
    `${ROOT} .react-flow__node > .nc { --nc-dim: 0.45; }`,
    `${ROOT} .react-flow__node:is(${ids(through)}) > .nc { --nc-dim: 1; }`,
    `${ROOT} .react-flow__edge { opacity: 0.45; }`,
  ]
  if (lineage.producers.length) {
    rules.push(
      `${ROOT} .react-flow__node:is(${ids(lineage.producers)}) > .nc {
        box-shadow: 0 0 0 2px var(--bg), 0 0 0 3.5px var(--accent), var(--elev-2); }`,
      `${ROOT} .react-flow__node:is(${ids(lineage.producers)}) > .nc::after {
        content: ${q(`产出 ${lineage.var}`)}; ${tag} background: var(--accent-solid); color: var(--on-accent); }`,
    )
  }
  if (lineage.consumers.length) {
    rules.push(
      `${ROOT} .react-flow__node:is(${ids(lineage.consumers)}) > .nc {
        outline: 1.5px dashed var(--accent); outline-offset: 3px; }`,
      `${ROOT} .react-flow__node:is(${ids(lineage.consumers)}) > .nc::after {
        content: "引用"; ${tag} background: var(--bg-elev); color: var(--accent);
        box-shadow: inset 0 0 0 1px var(--accent); }`,
    )
  }
  if (path.length) {
    const on = ids(path.map((e) => e.id))
    rules.push(
      `${ROOT} .react-flow__edge:is(${on}) { opacity: 1; --sf-edge: var(--accent); }`,
      `${ROOT} .react-flow__edge:is(${on}) .react-flow__edge-path { stroke: var(--accent); }`,
    )
  }
  return rules.join('\n')
}
