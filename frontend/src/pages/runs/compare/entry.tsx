import { useCallback, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { GitCompareArrows } from 'lucide-react'
import { ApiError, api } from '../../../api/client'
import { toast } from '../../../components/ui'
import { RUN_COMPARE_TEXT as T } from '../../../lib/terms'
import type { Run } from '../../../types'
import type { MenuItem } from '../parts'

/** 对比页的地址。via 是默认基准的来历（同一定时任务 / 同一工作流），只跟着这一次跳转走，换了基准就不再显示 */
export interface CompareVia { base: string; reason: string }

export const comparePath = (target: string, base?: string | null) =>
  `/runs/compare?${new URLSearchParams(base ? { target, base } : { target })}`

/**
 * 运行详情「更多」菜单里的「与上一次对比」。先问服务端默认的基准（同一定时任务的上一次，没有就同一工作流、同一类别的
 * 上一次），找到了再跳到对比页；找不到时留在原页，提示服务端给的原因，并给「手动选择基准」去对比页自己挑。
 * 没封存、还在运行的不给点：对比只认已封存的运行，点了也是 409。
 *
 * 写成 hook（返回造菜单项的函数）：运行详情里有提前返回，菜单项本身只能在拿到运行之后造，查找中的状态得在那之前就挂上
 */
export function useCompareEntry(): (run: Run, live: boolean) => MenuItem {
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)
  return useCallback((run: Run, live: boolean): MenuItem => {
    const blocked = live || !run.manifest_hash
    return {
      key: 'compare',
      label: T.menu,
      icon: <GitCompareArrows size={12} />,
      disabled: blocked || busy,
      hint: blocked ? T.menuSealedOnly : busy ? T.menuFinding : undefined,
      onSelect: () => {
        setBusy(true)
        api.compare.base(run.id).then((found) => {
          const via: CompareVia = { base: found.base_run_id, reason: found.reason }
          navigate(comparePath(run.id, found.base_run_id), { state: via })
        }, (e: unknown) => {
          if (e instanceof ApiError && e.kind === 'http' && e.status === 404) {
            toast.info(e.message, { action: { label: T.pickManually, onClick: () => navigate(comparePath(run.id)) } })
          } else {
            toast.error(e)
          }
        }).finally(() => setBusy(false))
      },
    }
  }, [navigate, busy])
}
