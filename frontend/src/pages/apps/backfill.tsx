/**
 * 按新口径补算（D-f）：编辑器的视图来源和应用页（manage）上共用。
 *
 * 只在这种情况下给按钮：当前发布的应用版本把某个视图固定的版本相对上一个应用版本升了版，而且处置选的是「按新口径重算」，
 * 这个视图里还有趋势组件（趋势才需要历史）。哪些视图能补算、各会发起几次，服务端在应用详情里给（backfill，
 * apps/backfill.backfillable）；点了先按 GET …/backfill 取一次最新的计划，确认框写明会发起几次、跳过几次。服务端没给
 * 计划时（老服务端）按视图里趋势组件取的最近几次（last，缺省 12，最多 24，同服务端 apps/resolve._series）写上限。
 * 补算按已发布的应用版本做，草稿里改过的视图要先发布
 */
import { useEffect, useState } from 'react'
import { History } from 'lucide-react'
import { api } from '../../api/client'
import type { BackfillPlan } from '../../api/apps'
import { Spinner, confirmDialog, toast } from '../../components/ui'
import { APP_EDITOR_TEXT as T } from '../../lib/terms'
import { useAtLeast } from '../../store/auth'
import type { AppComponent, AppSpec, AppVersionRow } from '../../typesApp'

/** 趋势最多取几次，同服务端 apps/resolve.SERIES_MAX */
const SERIES_MAX = 24
const SERIES_DEFAULT = 12

export interface BackfillTarget {
  view: string
  alias: string
  from: number
  to: number
  /** 最多发起几次运行 */
  max: number
  /** 服务端算好的：会发起几次、跳过几次（同一输入已经补算过） */
  exact?: { launch: number; skip: number }
}

const fromPlan = (p: BackfillPlan): BackfillTarget => ({
  view: p.view, alias: p.alias, from: p.from_version, to: p.to_version, max: p.periods,
  exact: { launch: p.will_launch, skip: p.will_skip },
})

/** 视图里趋势组件最多取几次；没有趋势组件为 0 */
export function seriesDepth(spec: AppSpec, view: string): number {
  let n = 0
  const walk = (list: AppComponent[]) => list.forEach((c) => {
    const b = c.bind
    if (b && b.view === view && typeof b.series === 'string') {
      const last = typeof b.last === 'number' && b.last > 0 ? Math.floor(b.last) : SERIES_DEFAULT
      n = Math.max(n, Math.min(last, SERIES_MAX))
    }
    if (c.children?.length) walk(c.children)
  })
  spec.pages.forEach((p) => walk(p.body))
  return n
}

/** 哪些视图可以按新口径补算（见文件开头）。previous 是上一个应用版本，没有就不算升版 */
export function backfillTargets(spec: AppSpec, current: AppVersionRow, previous: AppVersionRow | undefined): BackfillTarget[] {
  if (!previous) return []
  const out: BackfillTarget[] = []
  for (const [view, v] of Object.entries(spec.views ?? {})) {
    const alias = v?.source?.pin
    if (!alias || v.source.kind === 'instance' || current.dispositions?.[alias] !== 'recompute') continue
    const now = current.pins.find((p) => p.alias === alias)
    const before = previous.pins.find((p) => p.alias === alias)
    if (!now || !before || before.version === now.version) continue
    const max = seriesDepth(spec, view)
    if (max > 0) out.push({ view, alias, from: before.version, to: now.version, max })
  }
  return out
}

/**
 * 这个应用当前发布版里能补算的视图。服务端在应用详情里给了（plans）就用它；没给（老服务端，或者保存草稿后的回包不带）
 * 按版本列表自己推。publishedVersion 为空（还没发布）时不取。manage 才能补算：调用方先判断角色，接口拒绝（403）时当作没有
 */
export function useBackfillTargets(appId: string, publishedVersion: number | null | undefined, enabled = true,
  plans?: BackfillPlan[] | null): BackfillTarget[] {
  const [targets, setTargets] = useState<BackfillTarget[]>([])
  const planKey = plans ? JSON.stringify(plans) : null
  useEffect(() => {
    if (!enabled || publishedVersion == null) { setTargets([]); return }
    if (plans) { setTargets(plans.filter((p) => p.periods > 0).map(fromPlan)); return }
    let alive = true
    void (async () => {
      try {
        const { items } = await api.apps.versions(appId)
        const current = items.find((v) => v.version === publishedVersion)
        const previous = items.filter((v) => v.version < publishedVersion).sort((a, b) => b.version - a.version)[0]
        if (!current || !previous || !Object.values(current.dispositions ?? {}).includes('recompute')) {
          if (alive) setTargets([])
          return
        }
        const detail = await api.apps.version(appId, publishedVersion)
        if (alive) setTargets(backfillTargets(detail.spec, current, previous))
      } catch {
        if (alive) setTargets([])
      }
    })()
    return () => { alive = false }
  }, [appId, publishedVersion, enabled, planKey]) // eslint-disable-line react-hooks/exhaustive-deps
  return targets
}

export function BackfillButton({ appId, target, onDone }: { appId: string; target: BackfillTarget; onDone?: () => void }) {
  const [busy, setBusy] = useState(false)
  const run = async () => {
    setBusy(true)
    // 先取一次最新的计划（不发起运行）：确认框写的次数要和点下去之后发起的一致。取不到（老服务端）按上限写
    const plan = await api.apps.backfillPlan(appId, target.view).then(fromPlan, () => null)
    setBusy(false)
    const t = plan ?? target
    if (t.exact && t.exact.launch === 0) {
      toast.info(T.backfillNothing(t.exact.skip))
      return
    }
    const ok = await confirmDialog({
      title: T.backfillTitle(t.view),
      consequences: t.exact ? T.backfillExact(t.exact.launch, t.exact.skip, t.alias, t.from, t.to)
        : T.backfillConsequences(t.max, t.alias, t.from, t.to),
      confirmLabel: t.exact ? T.backfillConfirmExact(t.exact.launch) : T.backfillConfirm(t.max),
    })
    if (!ok) return
    setBusy(true)
    try {
      const res = await api.apps.backfill(appId, target.view)
      toast.ok(T.backfillDone(res.started, res.skipped))
      if (res.refused) toast.warn(res.refused)
      onDone?.()
    } catch (e) {
      toast.error(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <button type="button" className="btn btn-xs" disabled={busy} onClick={() => void run()}
            title={T.backfillHint(target.alias, target.from, target.to)} data-app-backfill={target.view}
            data-app-backfill-max={target.exact?.launch ?? target.max}>
      {busy ? <Spinner size={10} /> : <History size={10} aria-hidden />} {T.backfill}
    </button>
  )
}

/**
 * 应用页上的补算入口（manage）：每个能补算的视图一行，写明从哪一版升到哪一版。只给搭建者以上取（使用者没有 manage），
 * 取应用详情看自己是不是 manage
 */
export function BackfillPanel({ appId, publishedVersion, onDone }: {
  appId: string
  publishedVersion: number | null | undefined
  onDone?: () => void
}) {
  const builder = useAtLeast('builder')
  const [manage, setManage] = useState(false)
  const [plans, setPlans] = useState<BackfillPlan[] | null>(null)
  useEffect(() => {
    if (!builder) return
    let alive = true
    api.apps.get(appId).then((a) => {
      if (!alive) return
      setManage(a.my_role === 'manage')
      setPlans(Array.isArray(a.backfill) ? a.backfill : null)
    }, () => undefined)
    return () => { alive = false }
  }, [appId, builder])
  const targets = useBackfillTargets(appId, publishedVersion, builder && manage, plans)
  if (!targets.length) return null
  return (
    <div className="space-y-1.5 rounded-lg border px-3 py-2" data-app-backfill-panel={targets.length}>
      {targets.map((t) => (
        <div key={t.view} className="flex flex-wrap items-center gap-2 text-2xs">
          <span className="min-w-0 flex-1 leading-relaxed text-dim">{T.backfillHint(t.alias, t.from, t.to)}</span>
          <BackfillButton appId={appId} target={t} onDone={onDone} />
        </div>
      ))}
    </div>
  )
}
