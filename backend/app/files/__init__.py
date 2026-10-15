"""文件（docs/design/normal-user-apps.md N9、§3.8）：按 sha256 内容寻址存放（data/files/<前 2 位>/<sha>，只读），
两步上传（先拿引用再提交表单），提交时绑到一次运行（一次性），引用连同 sha256 进运行输入、随封存固定。
读文件：模板函数 file_text(ref)；导入：平台内置能力 table_import_period（按上传源已有的配方导入一期，效果写入）。
下载按绑定的运行或记录核对读权限。没绑定的上传 24 小时后清理。

包 B3 实现。
"""
from __future__ import annotations

#: 平台内置能力：把一个文件按上传源已有的配方导入成一期
TABLE_IMPORT_PERIOD = "table_import_period"
#: 文件类型白名单（待拍板 Q5 按推荐）
ALLOWED_MIME_PREFIXES = (
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv",
    "text/plain",
    "image/png",
    "image/jpeg",
)
#: 没绑定的上传多久后清理（秒）
UNBOUND_TTL_S = 24 * 3600
