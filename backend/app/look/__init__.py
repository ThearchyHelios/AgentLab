"""助手按需查看（look 文档第二部分 + 总方案 L1–L4，docs/design/app-platform-and-look.md 第五部分）。BE-4 实现。

- base.py      LookKind 接口、targets / 记录的形状、上限常量、生成过程的注入接口（Applier / Checker）
- tables.py    表（库概览、表目录、表详情，含遮罩；与 db_schema__、/schema 共用 data/schema_view.py）
- capabilities.py  能力（只给操作名、参数、返回字段、读写、要不要确认；不给地址和凭据）
- versions.py  工作流版本（应用润色时看钉住的版本；app/apps/describe.py）
- service.py   解析 targets、按类型挑选、交回目录、字数上限、重复查看短路、产出记录
"""
