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
#: libpq 写法的 SSL 证书参数：asyncpg 不收这几个连接参数（SQLAlchemy 原样交过去就是 TypeError），改用同名的环境变量
_LIBPQ_SSL_FILES = {"sslrootcert": "PGSSLROOTCERT", "sslcert": "PGSSLCERT", "sslkey": "PGSSLKEY",
                    "sslcrl": "PGSSLCRL", "sslpassword": "PGSSLPASSWORD"}
_SSL_MODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
#: 应用库地址里认的参数。SQLAlchemy 把地址里的参数原样交给 asyncpg.connect，它不认的（application_name、connect_timeout
#: 这类 libpq 写法）连的时候才报 TypeError；这里在启动时就说清楚
_APP_PARAMS = {"ssl", "prepared_statement_cache_size", "target_session_attrs", "async_fallback"}
#: 只给 SQLAlchemy / asyncpg 看的参数，libpq 不认（psycopg 连的时候会报「invalid connection option」）
_ASYNCPG_ONLY = {"prepared_statement_cache_size", "prepared_statement_name_func", "command_timeout", "async_fallback"}


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
    """AGENTLAB_DATABASE_URL → postgresql+asyncpg://…（密码原样保留）。认不出的写法抛 ValueError。

    SSL：libpq 的 sslmode= 换成 asyncpg 的 ssl=（取值同一套：disable / allow / prefer / require / verify-ca / verify-full）；
    证书文件（sslrootcert 之类）asyncpg 不收连接参数，要用 PGSSLROOTCERT 等环境变量（asyncpg 和断点库的 libpq 都认）。"""
    url = _parse(raw, _APP_SCHEMES, "postgresql+asyncpg://用户:密码@主机:5432/库名")
    query = dict(url.query)
    if files := [k for k in query if k in _LIBPQ_SSL_FILES]:
        raise ValueError("地址里不能写 " + "、".join(files) + "：asyncpg 不收这几个参数，改设环境变量 "
                         + "、".join(_LIBPQ_SSL_FILES[k] for k in files))
    if "sslmode" in query:
        if "ssl" in query:
            raise ValueError("ssl= 和 sslmode= 只写一个")
        mode = str(query.pop("sslmode"))
        if mode not in _SSL_MODES:
            raise ValueError(f"认不出 sslmode={mode}（可选 {' / '.join(sorted(_SSL_MODES))}）")
        query["ssl"] = mode
    if other := sorted(k for k in query if k not in _APP_PARAMS):
        raise ValueError("地址里的参数 " + "、".join(other) + " 不认（asyncpg 不收）；能写的只有 "
                         + "、".join(sorted(_APP_PARAMS | {"sslmode"})))
    return url.set(drivername="postgresql+asyncpg", query=query).render_as_string(hide_password=False)


def normalize_checkpoint_url(raw: str) -> str:
    """AGENTLAB_CHECKPOINT_URL → libpq 认的 postgresql://…。认不出的写法抛 ValueError。"""
    url = _parse(raw, _CHECKPOINT_SCHEMES, "postgresql://用户:密码@主机:5432/库名")
    return libpq_conninfo(url.render_as_string(hide_password=False))


def libpq_conninfo(database_url: str) -> str:
    """SQLAlchemy 的地址 → psycopg 的连接串：去掉驱动名，asyncpg 写法的参数换成 libpq 的。"""
    url = make_url(database_url)
    query = {_ASYNCPG_TO_LIBPQ.get(k, k): v for k, v in url.query.items() if k not in _ASYNCPG_ONLY}
    return url.set(drivername="postgresql", query=query).render_as_string(hide_password=False)


def describe(database_url: str) -> str:
    """写进日志的样子：主机、端口、库名、用户，不写密码。"""
    url = make_url(database_url)
    host = url.host or "本机套接字"
    port = f":{url.port}" if url.port else ""
    user = f"，用户 {url.username}" if url.username else ""
    return f"{host}{port}/{url.database}{user}"
