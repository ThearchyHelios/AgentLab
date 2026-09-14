"""给沙箱挑一个看不见后端依赖的 Python 解释器。

原先的做法是「用 `sys.base_prefix` 下的那个」，理由是 venv 会把 FastAPI、
SQLAlchemy、凭据处理那一套挂进 sys.path，而 base_prefix 是干净的标准库环境。

这个假设在 venv 下成立，在 conda 下不成立：conda 环境没有 venv 那层分离，
`sys.prefix == sys.base_prefix`，于是"干净解释器"挑出来的就是项目环境本身，
沙箱代码能直接 `import cryptography` 去解密落库的 API Key。

所以这里不再假设，而是**实际验证**：挨个候选跑一下，看敏感包是否可见。
都不干净时退回当前解释器并加 `-S` 关掉 site-packages —— 标准库照常可用，
第三方包一个也看不见。
"""
from __future__ import annotations

import subprocess
import sys
from functools import lru_cache
from pathlib import Path

# 沙箱里绝不该出现的包。看得见任何一个，这个解释器就不算干净。
# anthropic/openai 带着 provider 客户端，cryptography 能解密落库的 Key，
# sqlalchemy 能直接连库，fastapi 则说明整个后端环境都在。
_SENSITIVE = ("fastapi", "sqlalchemy", "cryptography", "anthropic", "openai")

_PROBE = (
    "import importlib.util as u;"
    f"print(int(any(u.find_spec(m) for m in {_SENSITIVE!r})))"
)


def _is_clean(exe: str) -> bool:
    """跑一下这个解释器，看敏感包是否可见。探测失败就当它不干净。"""
    try:
        out = subprocess.run(
            [exe, "-c", _PROBE],
            capture_output=True, text=True, timeout=10,
        )
        return out.returncode == 0 and out.stdout.strip() == "0"
    except (OSError, subprocess.SubprocessError):
        return False


def _candidates() -> list[str]:
    """按优先级列出候选，重复的去掉。

    先试 base_prefix（venv 下这就是对的），再试系统自带的 python3
    （macOS/Linux 都有，且不会装项目依赖），最后是 Homebrew 的。
    """
    base = Path(sys.base_prefix) / "bin"
    major, minor = sys.version_info.major, sys.version_info.minor
    raw = [
        base / f"python{major}.{minor}",
        base / f"python{major}",
        base / "python",
        Path("/usr/bin/python3"),
        Path("/usr/local/bin/python3"),
        Path("/opt/homebrew/bin/python3"),
    ]
    seen, out = set(), []
    for p in raw:
        try:
            real = str(p.resolve())
        except OSError:
            continue
        if real in seen or not p.exists():
            continue
        seen.add(real)
        out.append(real)
    return out


@lru_cache(maxsize=1)
def resolve() -> tuple[str, bool]:
    """返回 (解释器路径, 是否干净)。探测有子进程开销，所以缓存。"""
    for exe in _candidates():
        if _is_clean(exe):
            return exe, True
    return str(Path(sys.executable).resolve()), False


def python_argv() -> list[str]:
    """沙箱执行 Python 时的命令前缀（不含脚本名）。

    刻意**不用** `-I`：它在 3.11+ 隐含 `-P`，脚本所在目录不会进 sys.path，
    于是代码节点的 files 参数（多文件）整个失效 —— `import helper` 报
    ModuleNotFoundError。用 `-s -E` 拿到同样的隔离意图（不加 user site、
    忽略 PYTHONPATH 之类的环境变量），又不影响同目录 import。
    """
    exe, clean = resolve()
    flags = ["-s", "-E", "-u"] if clean else ["-S", "-s", "-E", "-u"]
    return [exe, *flags]


def describe() -> dict[str, object]:
    """给 health 用：说清楚沙箱到底在用哪个解释器、干不干净。"""
    exe, clean = resolve()
    return {
        "python": exe,
        "clean": clean,
        "note": (
            "解释器本身看不到后端依赖"
            if clean else
            "没找到干净的解释器，改用 -S 关掉 site-packages（标准库仍可用，第三方包不可见）"
        ),
    }
