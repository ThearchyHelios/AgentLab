/**
 * 应用平台使用面的文案（应用首页、应用页、发起、实例、待办、登录）。规矩同 terms.ts 开头的术语表：
 * 「固定版本」不写钉住；「运行」不写跑；「服务端」不写后端；批准 / 驳回；……
 *
 * terms.ts 末尾 `export * from './termsApp'`：调用方照旧从 lib/terms 导入。搭建面的文案写在 terms.ts。
 */

export const APP_TEXT = {
  nav: '应用',
  title: '应用',
  pending: '这一页正在建设',
}
