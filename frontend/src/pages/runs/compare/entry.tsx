import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { GitCompareArrows } from 'lucide-react'
import { ApiError, api } from '../../../api/client'
import { toast } from '../../../components/ui'
import { RUN_COMPARE_TEXT as T } from '../../../lib/terms'
import type { Run } from '../../../types'
import type { MenuItem } from '../parts'
import { compareErrorKind } from './model'

/** 对比页的地址。via 是默认基准的来历（同一定时任务 / 同一工作流），只跟着这一次跳转走，换了基准就不再显示 */
export interface CompareVia { base: string; reason: string }

export const comparePath = (target: string, base?: string | null) =>
  `/runs/compare?${new URLSearchParams(base ? { target, base } : { target })}`

/** 查找超过这么久才挂「正在查找」的提示：答复快的不闪一下 */
const NOTE_DELAY_MS = 300
/**
 * 预先问默认基准最多等这么久：连得上却卡着不回（代理、网络卡住）时按没问到算，入口不一直停在不可点的「正在查找」。
 * 平常一两百毫秒就回来；点了之后那一次不设这个限，慢了有「正在查找」的提示
 */
const ASK_TIMEOUT_MS = 5000

/**
 * 预先问到的默认基准。key 是问的哪一次运行的哪一份封存（运行 id + 清单哈希：失败后接着运行完会重新封存，基准可能跟着
 * 变）。base：previous 是紧挨着的上一次，earlier 是再往前成功的那一次（上一次失败、取消、还没结束），none 是没有默认
 * 基准（对比页会写「没有默认基准」、让人手动选）；null 是没问到（出错、超时），叫法退回「与上一次对比」，点了照常再问、
 * 照实说
 */
interface Known { key: string; base: 'previous' | 'earlier' | 'none' | null }

/**
 * 运行详情「更多」菜单里的「与上一次对比」。先问服务端默认的基准（同一定时任务上一次成功的运行，没有就同一工作流、
 * 同一类别上一次成功的运行；定时任务触发历史里同名的入口由服务端用同一个函数挑，两处比的是同一对运行），找到了再跳到
 * 对比页；点了才发现找不到（预先问时还有）时留在原页，提示服务端给的原因，并给「手动选择基准」去对比页自己挑。
 * 这几种不给点，点了也只会被拒：没封存、还在运行的（对比只认已封存的运行，409）；不属于已保存工作流的——画布、问数据
 * 里没保存就运行的，和工作流删掉之后 workflow_id 被置空的（服务端找不到同一工作流的运行，手动选也是 422）。
 *
 * 叫法和触发历史里的链接一致：基准不是紧挨着的上一次（上一次失败、取消、还没结束）时写「与上一次成功的运行对比」。
 * 点之前就得知道，所以拿到能对比的运行就预先问一次（previous）。问到之前这一项只写「正在查找」、不给点：菜单开着时不会
 * 先写一个叫法、问到了再换成另一个，也点不到一个说错了的入口；平常运行一打开就问，人点开菜单时早已问到。问到没有默认
 * 基准（多为工作流的第一次运行）时写「选择基准对比…」，点了直接去只带本次的对比页手动选：没有上一次可比还写「与上一次
 * 对比」会误导人，再问一遍、弹一条提示也是多走一步。预先问不到（出错、ASK_TIMEOUT_MS 内没回来）不提示，叫法退回
 * 「与上一次对比」、可点，点了照常再问一次、照实说。
 *
 * 切去别的标签页、窗口再切回来时再问一次：详情页可能开了很久，这期间在别处删掉、接着运行完了哪一次，基准跟着变。再问
 * 期间保留原来的叫法、照样可点（不退回「正在查找」，菜单开着也不闪），问到了不一样才换，问不到不改。
 *
 * 点了总是再问一次、按这一次的答复跳（没有默认基准的除外，对比页自己会再找一次）：预先问到的不一定还是现在的。菜单
 * 点完就收起：查找慢于一眨眼时挂一条提示，接口慢的时候不至于像没反应。离开运行详情（组件卸载）时取消请求、收起提示，
 * 晚到的答复不再把人带去对比页。卸载取消不了全部：地址已经换了、新页面还没画出来（路由按过渡渲染）的那一下答复回来，
 * 组件还在，请求也没取消。所以答复回来时再看一眼地址：人已经不在点菜单时的那个运行详情上，就不跳、也不提示。
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
  // 能对比的才预先问：对比页只认已封存、属于已保存工作流的运行，这两种入口本来就不给点，先问没有用处
  const target = run && !orphan && !unsealed ? run.id : null
  const key = target ? `${target}:${run?.manifest_hash}` : null
  // 切回这一页的次数：每切回来一次再问一次
  const [round, setRound] = useState(0)
  useEffect(() => {
    if (!key) return
    const onShow = () => { if (document.visibilityState === 'visible') setRound((n) => n + 1) }
    document.addEventListener('visibilitychange', onShow)
    return () => document.removeEventListener('visibilitychange', onShow)
  }, [key])
  useEffect(() => {
    if (!target || !key) return
    const ctrl = new AbortController()
    api.compare.base(target, { signal: ctrl.signal, timeoutMs: ASK_TIMEOUT_MS }).then(
      (found) => { if (!ctrl.signal.aborted) setKnown({ key, base: found.previous === false ? 'earlier' : 'previous' }) },
      (e: unknown) => {
        if (ctrl.signal.aborted) return
        // 没有默认基准算问到了（判法同对比页）；出错、超时算没问到：头一回退回「与上一次对比」，再问时不改原来的叫法
        if (compareErrorKind(e, true) === 'no-base') setKnown({ key, base: 'none' })
        else setKnown((k) => (k?.key === key ? k : { key, base: null }))
      },
    )
    return () => ctrl.abort()
  }, [target, key, round])

  const resolved = known && known.key === key ? known : null
  // 预先问的那一次还没回来：这一项只写正在查找，不先写一个叫法、问到了再换
  const asking = !!key && !resolved
  const label = asking ? T.menuFinding
    : resolved?.base === 'earlier' ? T.menuLastOk : resolved?.base === 'none' ? T.menuPick : T.menu
  return {
    key: 'compare',
    label,
    icon: <GitCompareArrows size={12} />,
    disabled: orphan || unsealed || asking || busy,
    hint: orphan ? T.menuNoWorkflow : unsealed ? T.menuSealedOnly : busy ? T.menuFinding : undefined,
    onSelect: () => {
      if (!run || !key) return
      // 已经知道没有默认基准：直接去只带本次的对比页，那里自己再找一次（这期间有了就用上），找不到写明、在上方手动选
      if (resolved?.base === 'none') {
        navigate(comparePath(run.id))
        return
      }
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
        setKnown({ key, base: found.previous === false ? 'earlier' : 'previous' })
        const via: CompareVia = { base: found.base_run_id, reason: found.reason }
        navigate(comparePath(run.id, found.base_run_id), { state: via })
      }, (e: unknown) => {
        if (left()) return
        if (e instanceof ApiError && e.kind === 'http' && e.status === 404) {
          // 点了才发现没有默认基准（这期间基准那一次被删了，或者预先问没问到）：照实说，下次打开菜单写「选择基准对比…」
          setKnown({ key, base: compareErrorKind(e, true) === 'no-base' ? 'none' : null })
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
