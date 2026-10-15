"""应用：已发布工作流版本的使用面（docs/design/app-platform-and-look.md 第二、三部分）。

模块分工（BE-3 实现，契约见设计文档附录 C）：
- spec.py      应用描述 agentlab.app/1、表单 agentlab.form/1 的形状
- catalog.py   受信组件目录 v1
- versions.py  「已发布且未退役」的唯一说法、退役
- forms.py     表单校验、输入节点字段 → 表单
- entries.py   应用入口发起实例
- ops.py       应用操作（助手润色、编辑器共用）
- lint.py      应用的发布检查（app.*）
- derive.py    从工作流版本确定性派生应用
- describe.py  给助手查看用的「工作流版本」详情
- resolve.py   页面解析与视图模型 agentlab.view/1（BE-3 新建）
"""
