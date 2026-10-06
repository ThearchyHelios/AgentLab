import { useEffect, useRef, useState } from 'react'
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
 * 预先问到的默认基准。key 是问的哪一次运行的哪一份封存（运行 id + 清单哈希：失败后接着运行完会重新封存，基准可能跟着
 * 变）；previous 为 null 是没问到（找不到默认基准、出错），叫法退回「与上一次对比」，点了照常再问、照实说
 */
interface Known { key: string; previous: boolean | null }

/**
 * 运行详情「更多」菜单里的「与上一次对比」。先问服务端默认的基准（同一定时任务上一次成功的运行，没有就同一工作流、
 * 同一类别上一次成功的运行；定时任务触发历史里同名的入口由服务端用同一个函数挑，两处比的是同一对运行），找到了再跳到
 * 对比页；找不到时留在原页，提示服务端给的原因，并给「手动选择基准」去对比页自己挑。
 * 这几种不给点，点了也只会被拒：没封存、还在运行的（对比只认已封存的运行，409）；不属于已保存工作流的——画布、问数据
 * 里没保存就运行的，和工作流删掉之后 workflow_id 被置空的（服务端找不到同一工作流的运行，手动选也是 422）。
 *
 * 叫法和触发历史里的链接一致：基准不是紧挨着的上一次（上一次失败、取消、还没结束）时写「与上一次成功的运行对比」。
 * 点之前就得知道，所以拿到能对比的运行就预先问一次（previous）。问到之前这一项只写「正在查找」、不给点：菜单开着时不会
 * 先写一个叫法、问到了再换成另一个，也点不到一个说错了的入口；平常运行一打开就问，人点开菜单时早已问到。预先问不到
 * （找不到默认基准、出错）不提示，叫法退回「与上一次对比」，点了照常再问一次、照实说。
 *
 * 点了总是再问一次、按这一次的答复跳：详情页可能开了很久，中间有运行接着跑完、被删掉，基准跟着变，预先问到的不一定还是
 * 现在的。菜单点完就收起：查找慢于一眨眼时挂一条提示，接口慢的时候不至于像没反应。离开运行详情（组件卸载）时取消请求、
 * 收起提示，晚到的答复不再把人带去对比页。卸载取消不了全部：地址已经换了、新页面还没画出来（路由按过渡渲染）的那一下
 * 答复回来，组件还在，请求也没取消。所以答复回来时再看一眼地址：人已经不在点菜单时的那个运行详情上，就不跳、也不提示。
 *
 * 写成 hook、在运行详情的提前返回之前调用：预先查找和点了之后查找中的状态都得在拿到运行之前就挂上。run 为空（还在读
 * 运行记录）时交回的这一项不会被画出来
 */
export function useCompareEntry(run: Run | null, live: boolean): MenuItem {
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)
  const [known, setKnown] = useState<Known | null>(null)
  // 在路上的那一次：stop 收起（或者不再挂出）「正在查找」的提示
  const pending = useRef<{ ctrl: AbortController; stop: () => void } | null>(null)
  useEffect(() => () => {
    const p = pending.current
    pending.current = null
    p?.ctrl.abort()
    p?.stop()
  }, [])

  const orphan = !run?.workflow_id
  const unsealed = live || !run?.manifest_hash
  // 能对比的才预先问：不能对比的入口本来就不给点，问了也是 409、422
  const target = run && !orphan && !unsealed ? run.id : null
  const key = target ? `${target}:${run?.manifest_hash}` : null
  useEffect(() => {
    if (!target || !key) return
    const ctrl = new AbortController()
    api.compare.base(target, { signal: ctrl.signal }).then(
      (found) => setKnown({ key, previous: found.previous !== false }),
      () => { if (!ctrl.signal.aborted) setKnown({ key, previous: null }) },
    )
    return () => ctrl.abort()
  }, [target, key])

  const resolved = known && known.key === key ? known : null
  // 预先问的那一次还没回来：这一项只写正在查找，不先写一个叫法、问到了再换
  const asking = !!key && !resolved
  return {
    key: 'compare',
    label: asking ? T.menuFinding : resolved?.previous === false ? T.menuLastOk : T.menu,
    icon: <GitCompareArrows size={12} />,
    disabled: orphan || unsealed || asking || busy,
    hint: orphan ? T.menuNoWorkflow : unsealed ? T.menuSealedOnly : busy ? T.menuFinding : undefined,
    onSelect: () => {
      if (!run || !key) return
      const ctrl = new AbortController()
      // 点菜单时所在的运行详情。只比路径：同一页里换视图、改列表筛选（改查询串）不算离开
      const from = window.location.pathname
      const left = () => ctrl.signal.aborted || window.location.pathname !== from
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
        if (left()) return
        // 这一次的答复也定叫法：跳不走（地址没换成）时菜单上写的和刚问到的一致
        setKnown({ key, previous: found.previous !== false })
        const via: CompareVia = { base: found.base_run_id, reason: found.reason }
        navigate(comparePath(run.id, found.base_run_id), { state: via })
      }, (e: unknown) => {
        if (left()) return
        if (e instanceof ApiError && e.kind === 'http' && e.status === 404) {
          setKnown({ key, previous: null })
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
}
