"""导入即注册。新增内置工具时在这里加一行。"""

from app.tools.builtin import code, data, files, knowledge, web  # noqa: F401

__all__ = ["code", "data", "files", "knowledge", "web"]
