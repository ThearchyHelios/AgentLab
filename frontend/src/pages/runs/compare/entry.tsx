import { useCallback, useEffect, useRef, useState } from 'react'
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

/** 查找超过这么久才挂「正在查找」的提示：答复快的不闪一下 */
const NOTE_DELAY_MS = 300

/**
 * 运行详情「更多」菜单里的「与上一次对比」。先问服务端默认的基准（同一定时任务的上一次，没有就同一工作流、同一类别的
 * 上一次），找到了再跳到对比页；找不到时留在原页，提示服务端给的原因，并给「手动选择基准」去对比页自己挑。
 * 这几种不给点，点了也只会被拒：没封存、还在运行的（对比只认已封存的运行，409）；不属于已保存工作流的——画布、问数据
 * 里没保存就运行的，和工作流删掉之后 workflow_id 被置空的（服务端找不到同一工作流的运行，手动选也是 422）。
 *
 * 菜单点完就收起：查找慢于一眨眼时挂一条提示，接口慢的时候不至于像没反应。离开运行详情（组件卸载）时取消请求、
 * 收起提示，晚到的答复不再把人带去对比页。
 *
 * 写成 hook（返回造菜单项的函数）：运行详情里有提前返回，菜单项本身只能在拿到运行之后造，查找中的状态得在那之前就挂上
 */
export function useCompareEntry(): (run: Run, live: boolean) => MenuItem {
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)
  // 在路上的那一次：stop 收起（或者不再挂出）「正在查找」的提示
  const pending = useRef<{ ctrl: AbortController; stop: () => void } | null>(null)
  useEffect(() => () => {
    const p = pending.current
    pending.current = null
    p?.ctrl.abort()
    p?.stop()
  }, [])
  return useCallback((run: Run, live: boolean): MenuItem => {
    const orphan = !run.workflow_id
    const unsealed = live || !run.manifest_hash
    return {
      key: 'compare',
      label: T.menu,
      icon: <GitCompareArrows size={12} />,
      disabled: orphan || unsealed || busy,
      hint: orphan ? T.menuNoWorkflow : unsealed ? T.menuSealedOnly : busy ? T.menuFinding : undefined,
      onSelect: () => {
        const ctrl = new AbortController()
        let note: number | null = null
        const timer = setTimeout(() => {
          note = toast.info(T.menuFinding, { sticky: true, key: `compare-finding:${run.id}` })
        }, NOTE_DELAY_MS)
        const stop = () => {
          clearTimeout(timer)
          if (note != null) toast.dismiss(note)
        }
        pending.current = { ctrl, stop }
        setBusy(true)
        api.compare.base(run.id, { signal: ctrl.signal }).then((found) => {
          if (ctrl.signal.aborted) return
          const via: CompareVia = { base: found.base_run_id, reason: found.reason }
          navigate(comparePath(run.id, found.base_run_id), { state: via })
        }, (e: unknown) => {
          if (ctrl.signal.aborted) return
          if (e instanceof ApiError && e.kind === 'http' && e.status === 404) {
            toast.info(e.message, { action: { label: T.pickManually, onClick: () => navigate(comparePath(run.id)) } })
          } else {
            toast.error(e)
          }
        }).finally(() => {
          stop()
          if (pending.current?.ctrl !== ctrl) return
          pending.current = null
          setBusy(false)
        })
      },
    }
  }, [navigate, busy])
}
