/**
 * 设置页「运行并发」一节的叫法（服务端 app/cluster/slots.py：交互、应用、定时、系统四类，各有全站上限和优先级，另有全站总上限）。
 * 叫法同服务端的 CLASS_LABEL；取值范围、合不合规只在服务端判（422 的原话落在这一节下面），界面不另判一套。
 */
export const CONCURRENCY_TEXT = {
  title: '运行并发',
  hint: '全站同时执行的运行按来源分四类，各有上限和优先级：名额满了时先给优先级高的一类，已经在执行的不打断。多进程部署时几个进程合起来算',
  maxTotal: '全站同时运行的上限',
  limit: '上限',
  priority: '优先级',
  priorityHint: '数字大的先拿到空出来的名额（0 到 100）',
  classes: {
    interactive: { label: '交互', hint: '问数据、画布试运行、手动发起的正式运行、蓝图试运行、个人应用的刷新' },
    app: { label: '应用', hint: '应用入口、按需视图、外部系统的钩子、接口调用' },
    schedule: { label: '定时', hint: '定时任务和事件触发' },
    system: { label: '系统', hint: '评测、口径召回、对账、补算、按口径正式出具' },
  } as Record<string, { label: string; hint: string }>,
  now: (running: number, waiting: number) => `现在 ${running} 个在执行${waiting ? `、${waiting} 个在等名额` : ''}`,
  save: '保存运行并发设置',
  saving: '正在保存…',
  saved: '已保存运行并发设置',
  discard: '放弃修改',
  readOnly: '只有管理员能修改运行并发设置',
  leaveTitle: (n: number) => `运行并发设置有 ${n} 项还没保存`,
  leaveConsequences: ['离开后这些修改会丢失'],
}

/** 四类的先后（同服务端 CLASSES） */
export const CONCURRENCY_CLASSES = ['interactive', 'app', 'schedule', 'system'] as const
