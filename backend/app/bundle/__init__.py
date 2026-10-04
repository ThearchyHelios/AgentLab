"""可验证报告：把一次已封存运行的报告导出成一个自包含的 HTML 文件，拿到文件的人在浏览器里就能核对。

- build.py：从事件、工件拼出页面数据，填进模板（纯函数，不碰数据库）
- template.html：页面骨架和样式
- verifier.js：浏览器端的核对和渲染（原生 JS，无依赖）

接口在 api/bundle.py（GET /api/runs/{run_id}/bundle.html）。
"""
