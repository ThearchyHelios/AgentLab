/**
 * 「组件名 → 渲染器」：前端只有这一张表（应用平台 §2.2 A2）。名字和后端 app/apps/catalog.py 的 COMPONENTS 一一对应；
 * 加组件时目录项、服务端校验器、解析器、这里的渲染器四样都要给。表里没有的组件由 ViewRenderer 画「此组件需要更新前端」。
 */
import type { ComponentType } from 'react'
import type { NodeProps } from '../context'
import { ColumnsBlock, DividerBlock, PageBlock, SectionBlock, TabsBlock } from './layout'
import {
  ChartBlock, FieldBlock, MetricBlock, NoticeBlock, ReportBlock, SeriesBlock, StampBlock, StatusBlock, TableBlock, TextBlock,
} from './display'
import { FormBlock } from './input'
import {
  ActionStatus, EntryButton, InstanceHeader, InstanceList, InstanceTimeline, SignoffsBlock, TaskForm, TaskList,
} from './process'
import { RecordsList } from './records'

export const RENDERERS: Record<string, ComponentType<NodeProps>> = {
  // 布局
  page: PageBlock,
  section: SectionBlock,
  columns: ColumnsBlock,
  tabs: TabsBlock,
  divider: DividerBlock,
  // 展示
  stamp: StampBlock,
  metric: MetricBlock,
  series: SeriesBlock,
  table: TableBlock,
  chart: ChartBlock,
  report: ReportBlock,
  text: TextBlock,
  notice: NoticeBlock,
  field: FieldBlock,
  status: StatusBlock,
  // 输入
  form: FormBlock,
  // 流程
  entry_button: EntryButton,
  instance_list: InstanceList,
  instance_header: InstanceHeader,
  instance_timeline: InstanceTimeline,
  task_list: TaskList,
  task_form: TaskForm,
  signoffs: SignoffsBlock,
  action_status: ActionStatus,
  // 填报登记的逐行查看（normal-user-apps N8）：按看的人实时取，使用者只看自己的
  records_list: RecordsList,
}
