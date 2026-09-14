from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.core.config import settings
from app.tools.registry import ToolContext, register

_MAX_READ = 200_000


def _resolve(rel: str, ctx: ToolContext) -> Path:
    """把相对路径锁死在这次运行自己的工作目录里。

    两道关都要过：session_dir() 先净化 session_id 再定根目录，然后 rel
    先 resolve 再比对根。少了第一道，`../../etc/passwd` 里的越界检查就是
    在一个被挪到 /etc 的"根"下面做的，检查照样通过——这正是之前的漏洞。
    """
    root = settings.session_dir(ctx.sandbox_session)
    root.mkdir(parents=True, exist_ok=True)
    target = (root / rel.lstrip("/")).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"路径越界：{rel}")
    return target


class FileReadArgs(BaseModel):
    path: str = Field(description="相对工作目录的文件路径")


@register(
    name="file_read",
    category="文件",
    description="读取工作目录下的文件内容。",
    args_schema=FileReadArgs,
)
async def file_read(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    args = FileReadArgs(**kwargs)
    try:
        target = _resolve(args.path, ctx)
    except ValueError as e:
        return {"error": str(e)}
    if not target.exists():
        return {"error": f"文件不存在：{args.path}"}
    if target.is_dir():
        return {"error": f"{args.path} 是目录，用 file_list"}
    data = target.read_bytes()[:_MAX_READ]
    return {
        "path": args.path,
        "size": target.stat().st_size,
        "content": data.decode("utf-8", errors="replace"),
        "truncated": target.stat().st_size > _MAX_READ,
    }


class FileWriteArgs(BaseModel):
    path: str = Field(description="相对工作目录的文件路径")
    content: str = Field(description="要写入的内容")
    append: bool = Field(default=False, description="追加而不是覆盖")


@register(
    name="file_write",
    category="文件",
    description="写入文件到工作目录。父目录会自动创建。",
    args_schema=FileWriteArgs,
    dangerous=True,
)
async def file_write(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    args = FileWriteArgs(**kwargs)
    try:
        target = _resolve(args.path, ctx)
    except ValueError as e:
        return {"error": str(e)}
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a" if args.append else "w", encoding="utf-8") as f:
        f.write(args.content)
    return {"path": args.path, "bytes": target.stat().st_size, "ok": True}


class FileListArgs(BaseModel):
    path: str = Field(default=".", description="要列出的目录")


@register(
    name="file_list",
    category="文件",
    description="列出工作目录下的文件。",
    args_schema=FileListArgs,
)
async def file_list(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    args = FileListArgs(**kwargs)
    try:
        target = _resolve(args.path, ctx)
    except ValueError as e:
        return {"error": str(e)}
    if not target.exists():
        return {"path": args.path, "entries": []}
    entries = [
        {
            "name": p.name,
            "type": "dir" if p.is_dir() else "file",
            "size": p.stat().st_size if p.is_file() else None,
        }
        for p in sorted(target.iterdir())[:500]
    ]
    return {"path": args.path, "entries": entries}
