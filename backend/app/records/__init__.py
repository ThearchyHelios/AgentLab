"""记录表（docs/design/normal-user-apps.md N8、§3.7）：平台托管的 SQLite、只许追加。

- 每行带系统列 _seq / _id / _submitted_by / _submitted_at / _run_id / _supersedes / _schema_version；
- 登记成 origin=records 的只读数据源，看板照常用 db_query__<名字> 查；
- 写入只经动作：平台内置能力 records_append（效果写入、允许无人值守、幂等键即动作键），查询能力 records_lookup；
- 钉版本：运行开始时 data_versions 记下高水位 _seq；查询连接上建同名 TEMP VIEW，只露出 _seq ≤ 高水位、没被更正过的行；
- 行级可见：同一套临时视图按人过滤（use 只看自己的，observe / manage 看全部）；正式运行以服务身份跑、不过滤。

模块：store（文件层：建表、加列、追加、高水位、视图语句）、tables（records_tables 与数据源的登记、表版本，
ensure_table）、access（看哪些行）、views（查询时的 RecordsView、data_versions 里的那一项）、actions（平台能力
records_append / records_lookup 的执行）、listing（记录列表）。
"""
from __future__ import annotations

#: 数据源的 origin
RECORDS_ORIGIN = "records"
#: 平台内置能力（工具名，只能用字母、数字、下划线）
RECORDS_APPEND = "records_append"
RECORDS_LOOKUP = "records_lookup"
#: 系统列
SYSTEM_COLUMNS = ("_seq", "_id", "_submitted_by", "_submitted_at", "_run_id", "_supersedes", "_schema_version")
