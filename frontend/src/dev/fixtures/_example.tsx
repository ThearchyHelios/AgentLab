import type { JSX } from 'react'

/**
 * 预览用例的写法示例：打开 /preview.html?syn=scaffold-example 看到的就是下面这个。
 *
 * dev/fixtures 下每个 .tsx 导出 cases（用例名 → 组件），preview.tsx 自动收进来，?syn=<用例名> 先在这里找，
 * 找不到再走 preview.tsx 里原有的用例。用例名全站唯一，带上功能前缀（schedules-list、compare-basic），
 * 重名时后加载的会盖掉先加载的，控制台会提示。和 preview.tsx 一样只在开发服务器里加载，不进生产包。
 */
export const cases: Record<string, () => JSX.Element> = {
  'scaffold-example': () => (
    <div className="p-4 text-sm" data-fixture="scaffold-example">预览用例示例</div>
  ),
}
