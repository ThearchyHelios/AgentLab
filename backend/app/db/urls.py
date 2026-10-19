"""应用库、断点库的地址：规范写法、换算成 psycopg 用的连接串、写进日志的样子（不带密码）。

不依赖 app.core.config：配置的校验器要调这里，这里再导 config 就成了循环导入。
"""

from __future__ import annotations

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

#: 应用库认的写法。asyncpg 是 SQLAlchemy 这边的驱动；postgresql:// / postgres:// 是常见的通用写法，一律换成 asyncpg
_APP_SCHEMES = {"postgresql+asyncpg", "postgresql", "postgres"}
#: 断点库认的写法：psycopg 只认 libpq 的 postgresql:// / postgres://；带驱动名的 SQLAlchemy 写法也收，去掉驱动名
_CHECKPOINT_SCHEMES = {"postgresql", "postgres", "postgresql+psycopg", "postgresql+asyncpg"}

#: asyncpg 和 libpq 对 SSL 的参数名不一样：asyncpg 是 ssl=require，libpq 是 sslmode=require（取值同一套）
_ASYNCPG_TO_LIBPQ = {"ssl": "sslmode"}


def _parse(raw: str, schemes: set[str], example: str) -> URL:
    try:
        url = make_url(raw)
    except ArgumentError:
        raise ValueError(f"认不出这个地址，应当写成 {example}") from None
    if url.drivername not in schemes:
        raise ValueError(f"只认 PostgreSQL（{example}），不认 {url.drivername}://；用 SQLite 就不要设这一项")
    if not url.database:
        raise ValueError(f"地址里要写库名，例如 {example}")
    return url


def normalize_database_url(raw: str) -> str:
    """AGENTLAB_DATABASE_URL → postgresql+asyncpg://…（密码原样保留）。认不出的写法抛 ValueError。"""
    url = _parse(raw, _APP_SCHEMES, "postgresql+asyncpg://用户:密码@主机:5432/库名")
    return url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)


def normalize_checkpoint_url(raw: str) -> str:
    """AGENTLAB_CHECKPOINT_URL → libpq 认的 postgresql://…。认不出的写法抛 ValueError。"""
    url = _parse(raw, _CHECKPOINT_SCHEMES, "postgresql://用户:密码@主机:5432/库名")
    return libpq_conninfo(url.render_as_string(hide_password=False))


def libpq_conninfo(database_url: str) -> str:
    """SQLAlchemy 的地址 → psycopg 的连接串：去掉驱动名，asyncpg 写法的参数换成 libpq 的。"""
    url = make_url(database_url)
    query = {_ASYNCPG_TO_LIBPQ.get(k, k): v for k, v in url.query.items()}
    return url.set(drivername="postgresql", query=query).render_as_string(hide_password=False)


def describe(database_url: str) -> str:
    """写进日志的样子：主机、端口、库名、用户，不写密码。"""
    url = make_url(database_url)
    host = url.host or "本机套接字"
    port = f":{url.port}" if url.port else ""
    user = f"，用户 {url.username}" if url.username else ""
    return f"{host}{port}/{url.database}{user}"
