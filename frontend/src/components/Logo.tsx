/**
 * 品牌标：三个节点、两段正交折线边——画布上的走线就是这种横平竖直的画法。
 * public/favicon.svg 是同一个图形（多了底板），改一处要同步改另一处。
 *
 * 颜色取 currentColor，由外面给（导航里是 text-accent）。live 时起点那个节点
 * 实心填上 --live：有运行在跑就亮着，是静态的状态指示，不做动画。
 */
export function Logo({
  size = 20,
  live = false,
  title = 'AgentLab · 受限动态编排',
  className,
}: {
  size?: number
  live?: boolean
  title?: string
  className?: string
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      role="img"
      aria-label={title}
      className={className}
    >
      <title>{title}</title>
      <path d="M8.5 12H12V6h3.5M12 12v6h3.5" />
      <rect
        x="2.5" y="9" width="6" height="6" rx="1.5"
        style={live ? { fill: 'var(--live)', stroke: 'var(--live)' } : undefined}
      />
      <rect x="15.5" y="3" width="6" height="6" rx="1.5" />
      <rect x="15.5" y="15" width="6" height="6" rx="1.5" />
    </svg>
  )
}
