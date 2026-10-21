"""导入即注册。新增内置工具时在这里加一行。"""

from app.tools.builtin import analysis, code, data, files, knowledge, platform, web  # noqa: F401

__all__ = ["analysis", "code", "data", "files", "knowledge", "platform", "web"]
