/**
 * 输入组件 form（组件目录 v1 §3.5）：按 agentlab.form/1 画表单，表单在 ViewModel.forms[props.form]。
 *
 * 提交就是「按这些参数看这一页」：值写进地址的 params（/apps/x/p/home?params={…}），页面按它重新解析——按需视图用这组参数
 * 找缓存、点「刷新」时按它发起。参数放在地址里而不是组件状态里：刷新页面、复制链接给别人，看到的是同一组筛选。
 * 预览不提交。
 */
import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Send } from 'lucide-react'
import { SchemaForm } from '../../components/SchemaForm'
import { cleanValues, validateForm, withDefaults } from '../../lib/schemaForm'
import { ENTRY_TEXT, VIEW_TEXT } from '../../lib/termsApp'
import type { FormError, FormSchema } from '../../typesApp'
import { str, useView, type NodeProps } from '../context'

/** 地址里的页面参数（JSON 对象）；没有、读不出来都是空对象 */
export function parseViewParams(raw: string | null): Record<string, unknown> {
  if (!raw) return {}
  try {
    const v = JSON.parse(raw)
    return v && typeof v === 'object' && !Array.isArray(v) ? v : {}
  } catch {
    return {}
  }
}

export function useViewParams(): Record<string, unknown> {
  const [search] = useSearchParams()
  const raw = search.get('params')
  // 按原文记：同一组参数不换对象，用它的组件不必重算
  return useMemo(() => parseViewParams(raw), [raw])
}

export function FormBlock({ node }: NodeProps) {
  const { model, preview } = useView()
  const [, setSearch] = useSearchParams()
  const current = useViewParams()
  const schema: FormSchema | undefined = model.forms[str(node.data?.form ?? node.props.form)]
  const [values, setValues] = useState<Record<string, unknown>>(() => (schema ? withDefaults(schema, current) : {}))
  const [errors, setErrors] = useState<FormError[]>([])
  if (!schema) return <div className="text-2xs text-faint" data-component="form">{ENTRY_TEXT.formMissing}</div>

  const submit = () => {
    if (preview) return
    const local = validateForm(schema, values)
    setErrors(local)
    if (local.length) return
    const body = cleanValues(schema, values)
    setSearch((prev) => {
      const next = new URLSearchParams(prev)
      if (Object.keys(body).length) next.set('params', JSON.stringify(body))
      else next.delete('params')
      return next
    })
  }
  return (
    <div className="rounded-lg border bg-panel p-3" data-component="form">
      <SchemaForm schema={schema} value={values} errors={errors} idPrefix={`form-${node.id}`}
                  onChange={(v) => { setValues(v); if (errors.length) setErrors([]) }} onSubmit={submit} />
      <div className="mt-3">
        <button type="button" className="btn btn-primary" aria-disabled={preview || undefined}
                title={preview ? VIEW_TEXT.submitPreview : undefined} onClick={submit} data-form-submit="">
          <Send size={12} aria-hidden /> {str(node.props.submit_label) || VIEW_TEXT.applyParams}
        </button>
      </div>
    </div>
  )
}
