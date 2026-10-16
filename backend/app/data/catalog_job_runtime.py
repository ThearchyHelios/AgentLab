"""后台任务运行时的老路径：已上移到 app/core/job_runtime.py（docs/design/next-round/00-总方案.md X5）。

这里把那个模块原样放在老名字下（sys.modules 指向同一个模块对象）：起草、剖析和别处的 `from app.data import
catalog_job_runtime as runtime`、`from app.data.catalog_job_runtime import …` 拿到的是同一批名字、同一条队（lanes），
测试里改它的属性也就是改那个模块。新代码从 app.core.job_runtime 导入。
"""
from __future__ import annotations

import sys

from app.core import job_runtime

sys.modules[__name__] = job_runtime
