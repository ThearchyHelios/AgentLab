import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { Inbox, Plus, Trash2, RefreshCw } from 'lucide-react'
import {
  EmptyState, ErrorState, Field, IconButton, Kbd, Modal, OfflineBanner, Skeleton, StatusBadge,
  StatusPill, Tabs, TabPanel, ToastHost, confirmDialog, promptDialog, toast,
} from '../components/ui'
import { ApiError, api } from '../api/client'
import { useCatalog, useOnReconnect } from '../store/catalog'
import { STATUS } from '../lib/status'
import { formatClock, formatCost, formatDateTime, formatDuration, formatTime, formatTokens } from '../lib/format'
import * as format from '../lib/format'
import * as status from '../lib/status'
import * as keys from '../lib/keys'
import * as terms from '../lib/terms'
import * as errors from '../lib/errors'
import '../index.css'

/**
 * 基础组件的离线预览（ui.tsx）。
 *
 * 只有 dev server 会加载（vite build 的 input 只有 index.html），不进生产包。
 * 存在的理由：弹窗的焦点管理、Esc 的输入法判断、toast 的寿命这些东西，在真实
 * 页面里要凑齐场景才看得到；这里每样都摆出来，亮暗两套主题一键切换。
 *
 * 打开 http://localhost:5273/ui-harness.html
 *
 * window.__ui 和各个按钮的 id 是检查脚本在用的，改名要一起改。
 */

const ALL_STATUS = Object.keys(STATUS)

function Block({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-xl border bg-panel p-4" data-block={title}>
      <h3 className="mb-3 text-[11px] font-semibold uppercase tracking-wide text-faint">{title}</h3>
      {children}
    </section>
  )
}

function DirtyModalDemo() {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  return (
    <>
      <button className="btn btn-sm" id="open-dirty" onClick={() => setOpen(true)}>打开编辑弹窗</button>
      <Modal
        open={open}
        onClose={() => { setOpen(false); setText('') }}
        title="编辑 Skill"
        dirty={text.trim() !== ''}
        footer={
          <>
            <button className="btn" onClick={() => { setOpen(false); setText('') }}>取消</button>
            <button className="btn btn-primary" onClick={() => { setOpen(false); setText(''); toast.ok('已保存') }}>保存</button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <Field label="名称" hint="小写英文、数字和下划线" required>
            {(p) => <input className="field" {...p} placeholder="sales_report" />}
          </Field>
          <Field label="指令" hint="填了内容再按 Esc 或点遮罩，会先问一句">
            {(p) => (
              <textarea className="field" rows={4} {...p} value={text} onChange={(e) => setText(e.target.value)} />
            )}
          </Field>
        </div>
      </Modal>
    </>
  )
}

/**
 * 页面自己拉的列表：断开期间拿到 []，恢复后靠 useOnReconnect 重拉。
 * 运行记录、数据源、会话这些页面都是这个形状。
 */
function LocalListDemo() {
  const [runs, setRuns] = useState<{ id: string }[] | null>(null)
  const [loads, setLoads] = useState(0)
  const load = () => {
    setLoads((n) => n + 1)
    void api.runs.list({ limit: 3 }).catch(() => []).then(setRuns)
  }
  useEffect(load, [])
  useOnReconnect(load)
  return (
    <div className="text-xs text-dim">
      <div className="flex items-center gap-2">
        <span>
          页面自己的列表：拉取 <span id="local-list-loads" className="mono">{loads}</span> 次 · 当前{' '}
          <span id="local-list-count" className="mono">{runs?.length ?? '—'}</span> 条
        </span>
        <button className="btn btn-sm ml-auto" id="local-list-reload" onClick={load}>重新拉取</button>
      </div>
      {runs && !runs.length && <EmptyState title="还没有运行记录" className="py-4" />}
    </div>
  )
}

function Harness() {
  const [theme, setTheme] = useState<'dark' | 'light'>(() => {
    // 没有显式主题时会跟随系统；预览要确定是哪一套，开场就钉住
    const t = (document.documentElement.getAttribute('data-theme') as 'dark' | 'light') || 'dark'
    document.documentElement.setAttribute('data-theme', t)
    return t
  })
  const [tab, setTab] = useState('a')
  const [last, setLast] = useState('')
  const backend = useCatalog((s) => s.backend)

  const flip = (t: 'dark' | 'light') => {
    setTheme(t)
    document.documentElement.setAttribute('data-theme', t)
  }

  return (
    <div className="flex h-full flex-col">
      <OfflineBanner />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto flex max-w-5xl flex-col gap-4 p-6">
          <div className="flex items-center gap-2">
            <h1 className="text-base font-semibold">基础组件</h1>
            <span className="text-xs text-faint">ui.tsx · lib/*</span>
            <div className="ml-auto flex gap-1">
              <button className="btn btn-sm" id="theme-dark" onClick={() => flip('dark')} aria-pressed={theme === 'dark'}>暗</button>
              <button className="btn btn-sm" id="theme-light" onClick={() => flip('light')} aria-pressed={theme === 'light'}>亮</button>
              <button
                className="btn btn-sm"
                id="toggle-offline"
                onClick={() => useCatalog.setState(backend === 'down'
                  ? { backend: 'ok', backendError: null, retryAt: null }
                  : { backend: 'down', backendError: '连不上后端服务（可能没启动或正在重启），稍后重试', retryAt: Date.now() + 8000, lastOkAt: Date.now() - 42_000 })}
              >
                {backend === 'down' ? '恢复在线' : '模拟离线'}
              </button>
            </div>
          </div>

          <Block title="状态徽标 · 彩色">
            <div className="flex flex-wrap gap-x-5 gap-y-3" id="badges-color">
              {ALL_STATUS.map((s) => (
                <div key={s} className="flex w-24 flex-col items-center gap-1.5">
                  <StatusBadge status={s} size={18} />
                  <span className="text-[11px] text-dim">{STATUS[s as keyof typeof STATUS].label}</span>
                  <span className="mono text-[10px] text-faint">{s}</span>
                </div>
              ))}
            </div>
          </Block>

          <Block title="状态徽标 · 灰度（去掉颜色也要认得出）">
            <div className="flex flex-wrap gap-x-5 gap-y-3" id="badges-gray" style={{ filter: 'grayscale(1)' }}>
              {ALL_STATUS.map((s) => (
                <div key={s} className="flex w-24 flex-col items-center gap-1.5">
                  <StatusBadge status={s} size={18} animate={false} />
                  <span className="text-[11px] text-dim">{STATUS[s as keyof typeof STATUS].short}</span>
                </div>
              ))}
            </div>
          </Block>

          <Block title="状态标签 StatusPill">
            <div className="flex flex-wrap gap-2">
              {ALL_STATUS.map((s) => <StatusPill key={s} status={s} />)}
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-dim">
              <span>后端 interrupted：</span>
              <StatusPill status="interrupted" pendingApproval />
              <StatusPill status="interrupted" pendingApproval={false} />
              <span className="ml-4">plain：</span>
              <StatusPill status="succeeded" plain />
              <StatusPill status="failed" plain />
            </div>
          </Block>

          <div className="grid grid-cols-2 gap-4">
            <Block title="弹窗 Modal · confirm · prompt">
              <div className="flex flex-wrap gap-2">
                <DirtyModalDemo />
                <button
                  className="btn btn-sm"
                  id="open-confirm"
                  onClick={async () => setLast(String(await confirmDialog({
                    title: '发布这个版本？', body: '发布后可以发起正式运行。', confirmLabel: '发布 v4',
                  })))}
                >确认框</button>
                <button
                  className="btn btn-sm btn-danger"
                  id="open-danger"
                  onClick={async () => setLast(String(await confirmDialog({
                    title: '删除工作流「⑥ 多 Agent 协作」？',
                    consequences: ['连同 4 条运行记录一起删除', '不可恢复'],
                    confirmLabel: '删除工作流和 4 条记录',
                    danger: true,
                  })))}
                >危险确认</button>
                <button
                  className="btn btn-sm btn-danger"
                  id="open-require"
                  onClick={async () => setLast(String(await confirmDialog({
                    title: '删除封存过的正式运行？',
                    body: '这条运行的清单封存过，是审计凭证。',
                    consequences: ['事件流、产出物、封存哈希一起删除', '之后无法再核对'],
                    confirmLabel: '删除凭证',
                    danger: true,
                    requireText: 'a3f9c2',
                  })))}
                >照抄确认</button>
                <button
                  className="btn btn-sm"
                  id="open-prompt"
                  onClick={async () => setLast(JSON.stringify(await promptDialog({
                    title: '新建工作流', label: '名称', initial: '未命名工作流 09-26 10:05', confirmLabel: '新建',
                    validate: (v) => (v === '新工作流' ? '已经有一个同名的了' : null),
                  })))}
                >输入框</button>
              </div>
              <div className="mt-2 text-[11px] text-faint">结果：<span id="dialog-result" className="mono text-dim">{last || '—'}</span></div>
            </Block>

            <Block title="提示 toast">
              <div className="flex flex-wrap gap-2">
                <button className="btn btn-sm" onClick={() => toast('已复制到剪贴板')}>普通</button>
                <button className="btn btn-sm" onClick={() => toast.ok('已保存')}>成功</button>
                <button className="btn btn-sm" onClick={() => toast.warn('这张图有 2 条警告')}>警告</button>
                <button className="btn btn-sm" id="toast-error" onClick={() => toast.error('保存失败：工作流名称不能为空')}>出错</button>
                <button
                  className="btn btn-sm"
                  id="toast-network"
                  onClick={() => toast.error(new ApiError(0, '连不上后端服务（可能没启动或正在重启），稍后重试',
                    { kind: 'network', raw: 'TypeError: Failed to fetch' }))}
                >网络断</button>
                <button
                  className="btn btn-sm"
                  onClick={() => toast.ok('已提取为草稿「⑥ 多 Agent 协作 · 提取模板」', {
                    action: { label: '去画布打开', onClick: () => toast('（这里会跳到画布）') },
                  })}
                >带动作</button>
                <button
                  className="btn btn-sm"
                  onClick={() => toast('已删除会话「本周销量」', 'info', {
                    action: { label: '撤销', onClick: () => toast.ok('已恢复') }, duration: 5000,
                  })}
                >撤销</button>
                <button className="btn btn-sm" onClick={() => { for (let i = 0; i < 6; i++) toast(`第 ${i + 1} 条`) }}>叠 6 条</button>
                <button className="btn btn-sm" onClick={() => toast.dismiss()}>全部关掉</button>
              </div>
            </Block>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <Block title="空态 EmptyState（模拟离线时会变）">
              <EmptyState
                icon={<Inbox size={22} />}
                title="还没有工作流"
                body="新建一张空白工作流，或者让助手按一句话画出来。"
                action={<button className="btn btn-sm btn-primary"><Plus size={12} /> 新建工作流</button>}
              />
              <EmptyState title="没有匹配的工具" offline={false} className="py-4" />
            </Block>
            <Block title="出错 ErrorState">
              <ErrorState
                error={new ApiError(0, '连不上后端服务（可能没启动或正在重启），稍后重试', { kind: 'network', raw: 'TypeError: Failed to fetch' })}
                onRetry={() => toast('重试')}
                className="py-4"
              />
              <ErrorState
                compact
                error={'2 validation errors for GraphSpec\nnodes.1.type\n  Input should be \'input\', \'output\' … [type=literal_error]'}
              />
              <ErrorState
                compact
                className="mt-2"
                error={new ApiError(409, '这条运行还在跑，先停止再删除。', { raw: '409 {"detail":"这条运行还在跑，先停止再删除。"}' })}
              />
            </Block>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <Block title="加载 Skeleton">
              <Skeleton rows={4} />
              <Skeleton rows={2} cols={3} height={44} className="mt-4" />
              <div className="mt-4 border-t pt-3"><LocalListDemo /></div>
            </Block>
            <Block title="图标按钮 · 键帽 · 格式">
              <div className="flex items-center gap-2">
                <IconButton label="删除这条记录" icon={<Trash2 size={13} />} variant="danger" />
                <IconButton label="刷新列表" shortcut="Mod+R" icon={<RefreshCw size={13} />} />
                <Kbd combo="Mod+K" />
                <Kbd combo="Mod+Enter" />
                <Kbd combo="Alt+V" />
                <Kbd combo="Shift+?" />
                <Kbd combo="Esc" />
              </div>
              <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs tabular-nums">
                <dt className="text-faint">耗时</dt>
                <dd>{[820, 7600, 59_960, 74_000, 119_600, 3_720_000].map(formatDuration).join(' · ')}</dd>
                <dt className="text-faint">计时</dt>
                <dd className="mono">{[7_630, 74_310, 3_723_400].map(formatClock).join(' · ')}</dd>
                <dt className="text-faint">tokens</dt>
                <dd>{formatTokens(56034)} · {formatTokens(56034, { compact: true })} · {formatTokens(null)}</dd>
                <dt className="text-faint">成本</dt>
                <dd>{[0.0312, 0.0004, 0, 1.2345].map(formatCost).join(' · ')}</dd>
                <dt className="text-faint">时间</dt>
                <dd title={formatDateTime('2026-09-26T01:11:19.891698')}>
                  {formatTime('2026-09-26T01:11:19.891698')} · {formatTime('2026-09-25T04:52:00Z')} · {formatDateTime('2026-09-26T01:11:19.891698')}
                </dd>
              </dl>
            </Block>
          </div>

          <Block title="标签页 Tabs · 表单字段 Field">
            <Tabs
              label="示例标签"
              idPrefix="demo"
              tabs={[{ key: 'a', label: '模型接入' }, { key: 'b', label: '数据源', badge: 2 }, { key: 'c', label: '偏好' }]}
              active={tab}
              onChange={setTab}
            />
            <TabPanel idPrefix="demo" tabKey={tab} className="grid grid-cols-2 gap-3 pt-3">
              <Field label="Base URL" htmlFor="demo-url" hint="OpenAI 兼容端点，以 /v1 结尾" required>
                <input id="demo-url" className="field" aria-describedby="demo-url-hint" placeholder="https://…/v1" />
              </Field>
              <Field label="标识" error="只能用小写英文、数字和下划线，并以字母开头">
                {(p) => <input className="field" defaultValue="Sales-DB" {...p} style={{ borderColor: 'var(--err)' }} />}
              </Field>
            </TabPanel>
          </Block>
        </div>
      </div>
    </div>
  )
}

;(window as any).__ui = {
  toast, confirmDialog, promptDialog, useCatalog, ApiError, api,
  // 检查脚本直接拿应用同一份模块实例测 lib：instanceof ApiError 才靠得住
  lib: { format, status, keys, terms, errors },
}

createRoot(document.getElementById('root')!).render(
  <BrowserRouter>
    <ToastHost>
      <Harness />
    </ToastHost>
  </BrowserRouter>,
)
