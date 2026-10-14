/**
 * 选人或组（FE-5 实现）：岗位绑定、应用授权、改派共用。关登录时只能手填名字（没有用户表）。
 * 契约骨架里只有接口和最简实现，props 不变。
 */
export interface Principal {
  kind: 'user' | 'group' | 'everyone'
  name: string
}

export interface PrincipalPickerProps {
  value: Principal[]
  onChange: (value: Principal[]) => void
  /** 允许选「所有人」（应用授权用） */
  allowEveryone?: boolean
  /** 只能选一个 */
  single?: boolean
  label?: string
}

export function PrincipalPicker({ value }: PrincipalPickerProps) {
  return <div>{value.map((p) => p.name).join('、')}</div>
}
