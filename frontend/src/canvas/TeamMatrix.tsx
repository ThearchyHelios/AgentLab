import { memo } from 'react'
import clsx from 'clsx'
import { StatusBadge } from '../components/ui'
import { formatClock, formatDuration, NONE } from '../lib/format'
import { statusMeta } from '../lib/status'
import type { NodeTrace, Segment } from '../run/trace'
import { useRunClock } from '../run/useRunClock'
import type { TeamMember, TeamRound, TeamRun } from '../types'

/**
 * 实时计时：「01:14.3」。只有它订阅时钟，所在的卡片、矩阵不跟着每 100ms 重画。
 *
 * from 是服务端时钟的毫秒时间戳，skewMs 是客户端减服务端的偏差（见 trace.liveAt）。
 * coarse 只到秒：成员行、等待时长这些地方十分位只会让一排数字一直在抖。
 * 十分位单独包一层，系统关了动效时由 CSS 藏掉（那时时钟也只 1 秒走一次）。
 */
export function LiveClock({ from, skewMs = 0, coarse = false }: {
  from: number
  skewMs?: number
  coarse?: boolean
}) {
  const now = useRunClock(true)
  const text = formatClock(Math.max(0, now - skewMs - from))
  const dot = text.lastIndexOf('.')
  if (dot < 0) return <span className="tnum">{text}</span>
  return (
    <span className="tnum">
      {text.slice(0, dot)}
      {!coarse && <span className="nc-tenth">{text.slice(dot)}</span>}
    </span>
  )
}

/** 回放时的定值计时：和 LiveClock 的 coarse 一样只到秒，拿不到就是「—」 */
export const stillClock = (ms: number | undefined): string =>
  (ms == null ? NONE : formatClock(Math.max(0, ms)).replace(/\.\d$/, ''))

/**
 * 协作矩阵：supervisor 节点在卡片内部摊开的那块东西。
 *
 * 多 agent 节点在画布上原本是个黑盒——卡片转 30 秒圈，然后吐一段文本。
 * "它同时派了三个人"、"谁还在跑"、"并行到底省了多少"全都要点开右栏才知道，
 * 而右栏是线性的时间线，看不出并发的形状。这块东西就是把它摊开：
 *
 * - 花名册固定在原地（不按派单顺序重排）——几轮下来眼睛知道谁在哪一行
 * - 条形长度按**这一轮里最慢的那个人**归一化。三个人并排三条长短不一的条，
 *   墙钟为什么等于最长那条、而不是三条之和，不用解释就看懂了
 * - 本轮没被派到的人不显示成"待命"，而是留着它上一轮的耗时（压暗）——
 *   它已经交回来了，说它"待命"是错的，而且看的人会以为它还没干活
 * - 最上面一行是调度者：一个 70 秒的协作节点里常有 60 秒是它在想下一步派谁，
 *   以前这段在画布上什么都没有，看着像卡死了
 *
 * 行数从编辑态起就固定（花名册 + 调度者 + 头尾），跑起来只换内容不长个子：
 * 矩阵一长高就会压住下方的邻居节点。在跑的人在干什么放在悬停提示里，
 * 不再另起两行。
 *
 * 成员数据来自 decode.ts 的 reduceTeam（右栏泳道用的是同一份，两处不会打架）；
 * 调度段和成员的实时起点来自航迹。
 */

interface Roster {
  name: string
  description?: string
}

interface Props {
  roster: Roster[]
  /** 还没跑过就没有：这时矩阵就是花名册 */
  team?: TeamRun
  /** 这个节点的航迹：调度段、成员段的起止 */
  trace?: NodeTrace
  /** 节点正在执行。决定措辞是"进行中"还是"已收尾"，以及要不要走时钟 */
  live: boolean
  /** 回放中：计时不走时钟 */
  replay?: boolean
  /** 回放游标（毫秒时间戳）。给了就按这一刻截取成员和调度段，见 teamAt */
  at?: number | null
  maxRounds?: number
  maxParallel?: number
  /** 服务端时钟偏差，实时计时要扣掉 */
  skewMs?: number
}

/** 当前该看哪一轮：有在跑的就看那一轮，否则看最后一轮 */
function currentRound(team: TeamRun | undefined) {
  if (!team?.rounds.length) return undefined
  const running = team.rounds.filter((r) => r.members.some((m) => m.status === 'running'))
  return running.length ? running[running.length - 1] : team.rounds[team.rounds.length - 1]
}

/**
 * 这一轮是几个人并行：按派出去的人数，不按此刻还有几个在跑。三人并行的一轮
 * 交回两个之后仍是并行的一轮，说成「串行推进」就和底部的「并行省下」打架了。
 * 老事件不带 parallel 时 reduceTeam 记成 1，按这一轮实际出现的人数兜底
 */
const widthOf = (round: TeamRound | undefined): number =>
  Math.max(round?.parallel ?? 1, round?.members.length ?? 0)

/** 某类段里最后一段。给了游标就只看那一刻已经开始的，还没结束的当作仍开着 */
function lastSeg(trace: NodeTrace | undefined, kind: Segment['kind'], agent?: string, at?: number | null): Segment | undefined {
  const segs = trace?.segments ?? []
  for (let i = segs.length - 1; i >= 0; i -= 1) {
    const s = segs[i]
    if (s.kind !== kind || (agent != null && s.agent !== agent)) continue
    if (at == null) return s
    if (s.start > at) continue
    return s.end != null && s.end > at ? { ...s, end: null } : s
  }
  return undefined
}

const SETTLED: TeamMember['status'][] = ['done', 'failed', 'cancelled', 'suspended']

/**
 * 回放到某一刻时的矩阵数据。
 *
 * runtime 里的 team 是跑完之后的样子：拖回第 8 秒，卡片写着「运行中」，矩阵却是
 * 「3 人并行完成 · 省下 11 s」。成员段记着每个人派出、交回的时刻，按游标截一刀
 * 重建那一刻。没有成员段的老数据（事件不带 ts）截不了，原样给出去
 */
export function teamAt(team: TeamRun | undefined, trace: NodeTrace | undefined, at: number | null | undefined): TeamRun | undefined {
  if (!team || at == null) return team
  const segs = (trace?.segments ?? []).filter((x) => x.kind === 'member')
  if (!segs.length) return team
  const used = new Set<Segment>()
  const rounds: TeamRound[] = []
  for (const r of team.rounds) {
    const members: TeamMember[] = []
    for (const m of r.members) {
      const seg = segs.find((x) => !used.has(x) && x.agent === m.agent
        && (x.iteration == null || x.iteration === r.round + 1))
      if (!seg) { members.push(m); continue }
      used.add(seg)
      if (seg.start > at) continue
      const ended = seg.end != null && seg.end <= at
      const settled = SETTLED.find((st) => st === seg.status)
      members.push({
        ...m,
        status: ended ? settled ?? m.status : 'running',
        ms: Math.max(0, (ended ? seg.end! : at) - seg.start),
      })
    }
    if (!members.length) continue
    const ms = members.map((m) => m.ms)
    rounds.push({ ...r, members, wallMs: Math.max(...ms), sumMs: ms.reduce((a, b) => a + b, 0) })
  }
  // 和 reduceTeam 同一个口径：只算整轮都交回了的
  const savedMs = rounds.reduce((acc, x) => acc
    + (x.members.every((m) => m.status === 'done') ? Math.max(0, x.sumMs - x.wallMs) : 0), 0)
  const finished = team.finished && trace?.endedAt != null && trace.endedAt <= at
  return { ...team, rounds, savedMs, finished }
}

/**
 * 一行摘要：远景档矩阵收起来时，卡片上只剩这一句。
 * 「2/3 在跑 · 第 2 轮」「调度中 · 第 2 轮」「共 3 轮 · 并行省下 28.4 s」
 * 进行中的把此刻的读数放前面：精简档缩到 0.4 倍以下时这一句放不全，截掉的该是轮次
 */
export function teamBrief(team: TeamRun | undefined, trace: NodeTrace | undefined, live: boolean, at?: number | null): string {
  team = teamAt(team, trace, at)
  const round = currentRound(team)
  const dispatch = lastSeg(trace, 'dispatch', undefined, at)
  const thinking = live && dispatch && dispatch.end == null
  if (!team?.rounds.length) return thinking ? '调度中' : live ? '进行中' : NONE
  const no = `第 ${(round?.round ?? 0) + 1} 轮`
  if (live) {
    if (thinking) return `调度中 · ${no}`
    const running = round?.members.filter((m) => m.status === 'running').length ?? 0
    const size = round?.members.length ?? 0
    if (running) return `${running}/${size} 在跑 · ${no}`
    // 人都交回了、下一次调度还没开始（老后端没有 route 事件时这段能有十几秒）
    return widthOf(round) > 1 ? `${size} 人已交回 · ${no}` : `等调度者 · ${no}`
  }
  const total = `共 ${team.rounds.length} 轮`
  if (team.savedMs > 0) return `${total} · 并行省下 ${formatDuration(team.savedMs)}`
  return team.finished && !team.rounds.some((r) => widthOf(r) > 1) ? `${total} · 全程串行` : total
}

/** 这一轮没交齐时怎么说：有人失败、被取消、被服务重启打断 */
function unsettledNote(members: TeamMember[]): string {
  const failed = members.filter((m) => m.status === 'failed').length
  if (failed) return `${failed} 人失败`
  const stuck = members.find((m) => m.status !== 'done' && m.status !== 'running')
  return stuck ? statusMeta(stuck.status).label : ''
}

/** 成员这一格的文字：没测到的一律「—」，不写 0，也不写"待命" */
function memberText(m: TeamMember | undefined, status = m?.status): string {
  if (!m || !status) return NONE
  if (status === 'done') return formatDuration(m.ms)
  if (status === 'failed' && m.ms) return `失败 ${formatDuration(m.ms)}`
  return statusMeta(status).short
}

function TeamMatrixImpl({ roster, team: final, trace, live, replay, at, maxRounds, maxParallel, skewMs = 0 }: Props) {
  const team = teamAt(final, trace, at)
  const round = currentRound(team)
  // 事件里出现过、但花名册里没有的名字也要显示。配置被人改过、或者调度者报了
  // 一个不存在的成员时，这里至少能看出"确实派过这么个人"
  const extra = (team?.members ?? []).filter((n) => !roster.some((r) => r.name === n))
  const names = [...roster.map((r) => r.name), ...extra]
  if (!names.length) return null

  /** 每个人这次要显示的那条记录：本轮优先，否则它在最近一轮里的成绩 */
  const rows = names.map((name) => {
    const inRound = round?.members.find((m) => m.agent === name)
    let last: TeamMember | undefined
    let lastRound = 0
    for (const r of team?.rounds ?? []) {
      const m = r.members.find((x) => x.agent === name)
      if (m) { last = m; lastRound = r.round }
    }
    const member = inRound ?? last
    const roundNo = inRound ? (round?.round ?? 0) : lastRound
    // 成员报错时 reduceTeam 仍记成"交回了"（它只看有没有 end）；航迹的成员段记着
    // 这一段是失败收场的，以它为准，不然超时的那个人在矩阵里是一条绿条
    const seg = member ? lastSeg(trace, 'member', name, at) : undefined
    const status: TeamMember['status'] | undefined = member?.status === 'done'
      && seg?.status === 'failed' && seg.iteration === roundNo + 1 ? 'failed' : member?.status
    return {
      name,
      active: !!inRound,
      member,
      status,
      roundNo,
      hint: roster.find((r) => r.name === name)?.description,
    }
  })

  // 条形按同一批数归一化，长短才可比
  const busiest = Math.max(1, ...rows.map((r) => (r.active ? r.member?.ms ?? 0 : 0)))
  const runningNow = round?.members.filter((m) => m.status === 'running').length ?? 0
  const ticking = live && !replay

  // 调度者：有 agent.route.* 就是精确值，老后端只有从日志推出来的估算（标 ≈）
  const dispatch = lastSeg(trace, 'dispatch', undefined, at)
  const thinking = live && !!dispatch && dispatch.end == null
  const decided = dispatch?.end != null ? dispatch.end - dispatch.start : undefined
  const dispatchRound = team?.rounds.find((r) => r.round === (dispatch?.iteration ?? 0) - 1)
  const dispatchNote = dispatch && !thinking
    ? dispatchRound?.members.length ? `派 ${dispatchRound.members.length} 人` : '收尾'
    : ''

  const width = widthOf(round)
  let headRight = ''
  if (!team) headRight = maxParallel ? `并发上限 ${maxParallel}` : ''
  else if (live) {
    headRight = thinking ? '调度中'
      : width > 1 ? runningNow ? `本轮 ${width} 人并行 · ${runningNow} 人在跑` : `本轮 ${width} 人已交回`
        : runningNow ? '串行推进' : '等调度者'
  } else {
    headRight = unsettledNote(rows.filter((r) => r.active && r.member)
      .map((r) => ({ ...r.member!, status: r.status! })))
      || (width > 1 ? `${width} 人并行完成` : team.finished ? '已收尾' : '')
  }
  // 省下多少只算整轮交齐的；有并行过但没交齐（失败、取消）的不能说成"全程串行"
  const serialOnly = !!team?.finished && !team.rounds.some((r) => widthOf(r) > 1)

  return (
    <div className={clsx('team-matrix', live && 'is-live')}>
      <div className="team-head">
        <span className="tnum">
          {team?.rounds.length
            ? `第 ${(round?.round ?? 0) + 1} 轮${maxRounds ? ` / 上限 ${maxRounds}` : ''}`
            : `花名册${maxRounds ? ` · 上限 ${maxRounds} 轮` : ''}`}
        </span>
        <span className="team-head-rule" />
        <span>{headRight}</span>
      </div>

      <div
        className={clsx('team-row team-row-dispatch', thinking && 'team-row-running', !dispatch && 'team-row-idle')}
        title={[
          '调度者：决定下一轮派谁、几个人同时做',
          round?.reason ? `理由：${round.reason}` : '',
          dispatch?.estimated ? '耗时由事件间隔推算' : '',
        ].filter(Boolean).join('\n')}
      >
        <StatusBadge status={thinking ? 'running' : dispatch ? 'done' : 'idle'} size={9} decorative />
        <span className="team-name">调度者</span>
        <span className="team-bar">
          {thinking && <span className="team-bar-running" />}
        </span>
        <span className="team-ms tnum">
          {thinking ? (
            ticking ? <>思考 <LiveClock from={dispatch!.start} skewMs={skewMs} coarse /></>
              : at != null ? `思考 ${stillClock(at - dispatch!.start)}` : '思考中'
          ) : decided != null ? (
            `${dispatch?.estimated ? '≈' : ''}${formatDuration(decided)}${dispatchNote ? ` · ${dispatchNote}` : ''}`
          ) : NONE}
        </span>
      </div>

      {rows.map(({ name, active, member, status, roundNo, hint }) => {
        const running = status === 'running'
        const done = status === 'done'
        const failed = status === 'failed'
        const open = running ? lastSeg(trace, 'member', name, at) : undefined
        return (
          <div
            key={name}
            className={clsx(
              'team-row',
              running && 'team-row-running',
              done && 'team-row-done',
              failed && 'team-row-failed',
              active && 'team-row-active',
              !member && 'team-row-idle',
              member && !active && 'team-row-past',
            )}
            data-member-status={status ?? 'idle'}
            title={[
              hint,
              running && member?.instruction ? `在做：${member.instruction}` : '',
              member ? `第 ${roundNo + 1} 轮 · ${memberText(member, status)}` : '',
            ].filter(Boolean).join('\n') || name}
          >
            <StatusBadge status={status ?? 'idle'} size={9} decorative />
            <span className="team-name">{name}</span>
            <span className="team-bar">
              {running ? (
                <span className="team-bar-running" />
              ) : (done || failed) && member && active ? (
                <span
                  className="team-bar-fill"
                  data-ratio={Math.max(0.08, member.ms / busiest).toFixed(3)}
                  style={{ transform: `scaleX(${Math.max(0.08, member.ms / busiest)})` }}
                />
              ) : null}
            </span>
            <span className="team-ms tnum">
              {running && open && open.end == null && ticking
                ? <LiveClock from={open.start} skewMs={skewMs} coarse />
                : running && open && at != null ? stillClock(at - open.start)
                : memberText(member, status)}
            </span>
          </div>
        )
      })}

      <div className="team-foot">
        <span className="tnum">{team?.rounds.length ? `${team.rounds.length} 轮` : NONE}</span>
        <span className="flex-1" />
        {/* 名册就在上面几行里，再数一遍人数是废话。这里只留一个真正要算的数：
            并行到底省了多少。一轮都还没整轮交回时写「—」——"省下 0 ms"是在报
            一个还没发生的收益；跑完了一次都没并发过就直说串行 */}
        <span className={clsx('tnum', team && team.savedMs > 0 && 'team-saved')}>
          {team && team.savedMs > 0 ? `并行省下 ${formatDuration(team.savedMs)}`
            : serialOnly && !live ? '全程串行' : NONE}
        </span>
      </div>
    </div>
  )
}

export const TeamMatrix = memo(TeamMatrixImpl)
