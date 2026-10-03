import { useState } from 'react'
import type { CatalogItem } from '../../types'
import { isComposing, Modal, Spinner } from '../../components/ui'
import { CODES_TEXT as KT } from '../../lib/terms'
import { StatusChip } from './parts'

// ===========================================================================
// 填写码值的含义。数据剖析只知道列里出现过哪些取值（码值候选的含义是空串），含义要人填：逐个码值一个输入框，
// 比在编辑模式里改「码值=含义」的多行文字省事。保存走整份提交，这一项记为人工填写、已确认；
// 暂不清楚的含义可以留空，表格里照旧显示「含义待填写」。
// ===========================================================================

export function CodesDialog({ column, item, saving, onSave, onClose }: {
  column: string
  item: CatalogItem<Record<string, string>>
  saving: boolean
  /** 新的码值对照（码值不变，只改含义） */
  onSave: (value: Record<string, string>) => void
  onClose: () => void
}) {
  const [initial] = useState(() => ({ ...item.value }))
  const [value, setValue] = useState<Record<string, string>>(initial)
  const codes = Object.keys(initial)
  const dirty = codes.some((k) => (value[k] ?? '').trim() !== (initial[k] ?? '').trim())
  const save = () => {
    if (!dirty || saving) return
    onSave(Object.fromEntries(codes.map((k) => [k, (value[k] ?? '').trim()])))
  }
  return (
    <Modal open onClose={onClose} dirty={dirty} width={520} title={KT.title(column)}
           footer={(
             <>
               <button type="button" className="btn" onClick={onClose}>{KT.cancel}</button>
               <button type="button" className="btn btn-primary" disabled={!dirty || saving} onClick={save} data-codes-save="">
                 {saving ? <Spinner size={11} /> : null} {KT.save}
               </button>
             </>
           )}>
      <div className="space-y-3" data-codes-dialog={column}>
        <div className="flex flex-wrap items-center gap-1.5 text-2xs text-faint">
          <StatusChip status={item.status} source={item.source} />
        </div>
        {item.note && (
          <div className="rounded border bg-bg px-2 py-1.5 text-2xs leading-relaxed text-dim">
            <span className="text-faint">{KT.note}：</span>{item.note}
          </div>
        )}
        <div className="overflow-hidden rounded-lg border">
          <table className="w-full border-collapse text-xs">
            <thead>
              <tr className="border-b bg-elev text-left text-2xs text-faint">
                <th scope="col" className="w-[32%] px-3 py-1.5 font-medium">{KT.head.code}</th>
                <th scope="col" className="px-2 py-1.5 font-medium">{KT.head.meaning}</th>
              </tr>
            </thead>
            <tbody>
              {codes.map((k, i) => (
                <tr key={k} className="border-b border-hairline last:border-b-0">
                  <th scope="row" className="mono px-3 py-1 text-left font-normal [overflow-wrap:anywhere]">
                    <label htmlFor={`codes-${column}-${i}`}>{k}</label>
                  </th>
                  <td className="px-2 py-1">
                    <input id={`codes-${column}-${i}`} className="field !py-1" value={value[k] ?? ''} placeholder={KT.placeholder}
                           maxLength={500} autoFocus={i === codes.findIndex((c) => !(initial[c] ?? '').trim())}
                           onChange={(e) => setValue((v) => ({ ...v, [k]: e.target.value }))}
                           // 回车保存；输入法组字时的回车是选词，不算
                           onKeyDown={(e) => { if (e.key === 'Enter' && !isComposing(e)) { e.preventDefault(); save() } }}
                           data-code={k} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="text-2xs leading-relaxed text-faint">{KT.hint}</p>
      </div>
    </Modal>
  )
}
