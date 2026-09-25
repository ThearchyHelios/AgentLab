import clsx from 'clsx'
import { formatDuration } from '../run/decode'
import type { TeamMember, TeamRun } from '../types'

/**
 * 协作矩阵：supervisor 节点跑起来时在卡片内部展开的那块东西。
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
 *
 * 数据来自 decode.ts 的 reduceTeam：右栏泳道用的是同一份，两处不会打架。
 */

interface Roster {
  name: string
  description?: string
}

interface Props {
  roster: Roster[]
  team: TeamRun
  /** 还在跑。用来决定措辞是"正在协作"还是"已收尾" */
  live: boolean
  maxRounds?: number
}

/** 当前该看哪一轮：有在跑的就看那一轮，否则看最后一轮 */
function currentRound(team: TeamRun) {
  const running = team.rounds.filter((r) => r.members.some((m) => m.status === 'running'))
  return running.length ? running[running.length - 1] : team.rounds[team.rounds.length - 1]
}

export function TeamMatrix({ roster, team, live, maxRounds }: Props) {
  const round = currentRound(team)
  // 事件里出现过、但花名册里没有的名字也要显示。配置被人改过、或者调度者报了
  // 一个不存在的成员时，这里至少能看出"确实派过这么个人"
  const extra = team.members.filter((n) => !roster.some((r) => r.name === n))
  const names = [...roster.map((r) => r.name), ...extra]
  if (!names.length) return null

  /** 每个人这次要显示的那条记录：本轮优先，否则它在最近一轮里的成绩 */
  const rows = names.map((name) => {
    const inRound = round?.members.find((m) => m.agent === name)
    let last: TeamMember | undefined
    let lastRound = 0
    for (const r of team.rounds) {
      const m = r.members.find((x) => x.agent === name)
      if (m) { last = m; lastRound = r.round }
    }
    return {
      name,
      active: !!inRound,
      member: inRound ?? last,
      roundNo: inRound ? (round?.round ?? 0) : lastRound,
      hint: roster.find((r) => r.name === name)?.description,
    }
  })

  // 条形按同一批数归一化，长短才可比
  const busiest = Math.max(1, ...rows.map((r) => r.member?.ms ?? 0))
  const parallel = round?.parallel ?? 1

  return (
    <div className="team-matrix">
      <div className="team-head">
        <span>
          第 {(round?.round ?? 0) + 1} 轮
          {maxRounds ? ` / 上限 ${maxRounds}` : ''}
        </span>
        <span className="team-head-rule" />
        <span>{parallel > 1 ? `${parallel} 人同时进行` : live ? '串行推进' : '已收尾'}</span>
      </div>

      {rows.map(({ name, active, member, roundNo, hint }) => {
        const running = member?.status === 'running'
        const done = member?.status === 'done'
        return (
          <div key={name}>
            <div
              className={clsx(
                'team-row',
                running && 'team-row-running',
                done && 'team-row-done',
                active && 'team-row-active',
                !member && 'team-row-idle',
                member && !active && 'team-row-past',
              )}
              title={[hint, member ? `第 ${roundNo + 1} 轮 · ${formatDuration(member.ms) || '0ms'}` : '']
                .filter(Boolean).join('\n') || name}
            >
              <span className="team-dot" />
              <span className="team-name">{name}</span>
              <span className="team-bar">
                {running ? (
                  <span className="team-bar-running" />
                ) : done && member ? (
                  <span
                    className="team-bar-fill"
                    style={{ width: `${Math.max(8, (member.ms / busiest) * 100)}%` }}
                  />
                ) : null}
              </span>
              <span className="team-ms">
                {running ? '进行中'
                  : done && member ? formatDuration(member.ms)
                  : '待命'}
              </span>
            </div>
            {running && member?.instruction && (
              <div className="team-note">{member.instruction}</div>
            )}
          </div>
        )
      })}

      <div className="team-foot">
        <span>{team.rounds.length} 轮</span>
        <span className="flex-1" />
        {/* 名册就在上面几行里，再数一遍人数是废话。这里只留一个真正要算的数：
            并行到底省了多少；一次都没并发过就直说串行 */}
        <span className={clsx(team.savedMs > 0 && 'team-saved')}>
          {team.savedMs > 0 ? `并行省下 ${formatDuration(team.savedMs)}` : '全程串行'}
        </span>
      </div>
    </div>
  )
}
