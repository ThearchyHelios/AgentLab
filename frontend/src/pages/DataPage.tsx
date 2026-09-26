import { Database } from 'lucide-react'
import { TabPanel, Tabs, useTabRoute } from '../components/ui'
import { DataSourcesTab, PageHeader } from './DataSourcesTab'
import type { DataView } from './DataSourcesTab'

/**
 * 「数据」：接进来的库和表。
 *
 * 以前数据源藏在「设置」的第二个标签里，而它是默认首页「问数据」的前提——新用户
 * 的第一步要靠读「去设置 → 数据源」这句话自己找路。数据库和表格是同一类「给
 * agent 喂数据」的资源，放到顶级入口下，各占一个标签。
 *
 * 地址：/data/databases、/data/tables；/data 落到数据库。
 */
const TABS: { key: DataView; label: string }[] = [
  { key: 'databases', label: '数据库' },
  { key: 'tables', label: '表格' },
]

export function DataPage() {
  const [tab, setTab] = useTabRoute(TABS.map((t) => t.key), 'databases')
  return (
    <div className="flex h-full flex-col">
      <PageHeader
        icon={<Database size={13} />}
        title="数据"
        subtitle="数据源都在这里：接进来的库和传上来的表，助手编排时看得见结构，agent 运行时能直接查"
      />
      <Tabs tabs={TABS} active={tab} onChange={setTab} label="数据" idPrefix="data" />
      <TabPanel idPrefix="data" tabKey={tab} className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl p-4">
          {/* 两个标签共用同一个实例：切标签不重拉、不卸载卡片 */}
          <DataSourcesTab view={tab as DataView} />
        </div>
      </TabPanel>
    </div>
  )
}
