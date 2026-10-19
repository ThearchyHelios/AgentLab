from __future__ import annotations

import ipaddress
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BACKEND_DIR.parent

#: 嵌入白名单里一项的主机部分：域名（可以 *. 开头）或方括号里的 IPv6，可以带端口；不许路径、查询、用户名
_ORIGIN_HOST = re.compile(r"(?:\*\.)?(?:[A-Za-z0-9-]+\.)*[A-Za-z0-9-]+(?::\d{1,5})?|\[[0-9A-Fa-f:.]+\](?::\d{1,5})?")

# session_id 拼进路径前必须过这一关。允许的字符刻意收得很窄：字母数字加
# 连字符下划线——点号也不放行，免得出现 ".." 这种能往上走的名字。
_SESSION_MAX = 64


def sanitize_session(session_id: str | None) -> str:
    """把任意字符串收敛成一个能安全当目录名的 token。

    以前这段逻辑在四个沙箱后端里各抄了一遍，而文件工具那条路径漏抄了，
    结果 sandbox_session 传 ".." 就能读到工作区外的文件（连加密主密钥都
    读得到）。所以统一收到这里，只此一份。
    """
    safe = "".join(c for c in (session_id or "") if c.isalnum() or c in "-_")
    return safe[:_SESSION_MAX] or "default"


#: 数据目录里的标记文件（Settings.ensure_dirs 建，deploy/deploy.sh 认）
DATA_DIR_MARKER = ".agentlab-data"


class Settings(BaseSettings):
    """全局配置。所有项都可以用 AGENTLAB_ 前缀的环境变量覆盖。"""

    model_config = SettingsConfigDict(
        env_prefix="AGENTLAB_",
        env_file=(PROJECT_DIR / ".env", BACKEND_DIR / ".env"),
        extra="ignore",
    )

    # --- 服务 ---
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: list[str] = ["http://localhost:5273", "http://127.0.0.1:5273"]

    # --- 存储 ---
    data_dir: Path = PROJECT_DIR / "data"
    # 应用自己的库（工作流、运行、事件、审批……）。不设：数据目录里的 SQLite（agentlab.db，断点在 checkpoints.db），
    # 单机单进程。设成 postgresql+asyncpg://用户:密码@主机:端口/库名（postgresql:// 也认）时整个应用库用 PostgreSQL，
    # LangGraph 的断点也放进同一个库；checkpoint_url 另给就放那里。数据目录照旧要有：工件、上传表格、沙箱等仍是文件
    database_url: str = ""
    checkpoint_url: str = ""
    # 直连地址（可选，写法同 database_url）：普通查询走 database_url（可以是 PgBouncer），依赖会话状态的连接走这里——进程间通知的
    # LISTEN 那一条、跨进程锁（会话级咨询锁）、断点库（预备语句、建表时的会话锁）。不设就全用 database_url
    database_direct_url: str = ""
    # database_url 是 PgBouncer 的事务模式（AGENTLAB_DB_PGBOUNCER=1）：应用库不用客户端连接池（交给 PgBouncer）、关掉 asyncpg 和
    # SQLAlchemy 的预备语句缓存、预备语句用不会撞名的名字。这时必须另给 database_direct_url（事务模式下 LISTEN、会话级咨询锁不成立），
    # 启动时检查
    db_pgbouncer: bool = False
    # PostgreSQL 的连接池（SQLite 不用这几项）。pool_size=0 表示不用连接池、每次新建连接（前面有 PgBouncer 时用，
    # 测试里也这么用：每个测试一个事件循环，asyncpg 的连接不能跨循环）
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800
    # 启动时库还连不上（和 PostgreSQL 容器一起起、库还在初始化）最多等多少秒，按退避重试、照实打日志；过了再报错退出。
    # 密码不对、库不存在这种等也没用的不等
    db_startup_timeout: int = 90

    # --- 多进程（app/cluster，docs/design/orchestration-round §5 W2-b）---
    # 几个工作进程（scripts/prod.sh、Docker 的入口按它给 uvicorn --workers）。只有应用库是 PostgreSQL 时才能大于 1：
    # SQLite 只支持单进程，启动时拒绝（app/cluster/guard.py）
    workers: int = 1
    # 每个工作进程在库里登记一行：隔 worker_heartbeat_s 秒续一次租约，租约长 worker_lease_s 秒。进程没了（强杀、断电）
    # 租约到期后，别的进程接手它认领的运行、问数据、后台任务（各按各的规矩）。租约要比心跳长几倍：库一时慢了不算进程没了
    worker_lease_s: float = 30.0
    worker_heartbeat_s: float = 5.0

    # --- 前端构建产物（生产部署用）---
    # 设了就由服务端一并托管页面（见 app/web.py），页面和接口同源、只起一个进程。
    # 不设（开发模式）什么都不变：页面由 Vite 开发服务器提供。相对路径按仓库根目录算
    web_dist: Path | None = None

    # --- 密钥加密 ---
    # 用于加密落库的 provider api key。留空则自动生成并存到 data_dir/.secret_key。
    secret_key: str = ""

    # --- 身份（app/auth/）---
    # 登录方式 AGENTLAB_AUTH_MODE（off / local / oidc）由 app/auth 每次直接读环境变量：测试和部署脚本临时改它
    # 要立刻生效，不能被这个单例在导入时定死。这里只放会话和对外地址
    auth_session_idle_hours: int = 12
    auth_session_max_days: int = 7
    # 会话 cookie 带不带 Secure。不设时按请求本身是不是 https 定：本机 http 开发照常能登录
    auth_cookie_secure: bool | None = None
    # 对外的访问地址（反向代理后面时填，如 https://agentlab.example.com）。全站只认一个（core/public_url.py）：
    # 设了它，设置里的「对外访问地址」就不生效、不能改；通知和 Webhook 的链接、OIDC 的回调、来源核对都按它拼。
    # 不设时用设置里的那一项，再没有就按请求的 Host 拼（通知里不附链接）
    public_url: str = ""

    # --- 安全响应头（app/web_security.py）---
    # 允许哪些站点用 iframe 嵌整站（同站的门户，如 https://portal.example.com），逗号分隔。本站自己总是允许的。
    # 不设时只有本站能嵌（frame-ancestors 'self'，另加 X-Frame-Options: SAMEORIGIN）
    frame_ancestors: Annotated[list[str], NoDecode] = []
    # 分享页（/s）允许被哪些站点嵌，逗号分隔，写法同上。分享页只认这一项，不认上面那一项
    embed_origins: Annotated[list[str], NoDecode] = []

    # --- 沙箱 ---
    sandbox_backend: str = "auto"  # auto | microvm | seatbelt | bubblewrap | local | off
    sandbox_timeout: int = 30
    sandbox_memory_mb: int = 512
    sandbox_cpus: float = 1.0
    sandbox_network: bool = False
    sandbox_max_output: int = 20_000

    # --- microVM（硬件级隔离，独立内核）---
    # 跑代码用的 OCI 镜像。首次使用会拉取并缓存到 ~/.microsandbox。
    microvm_image: str = "python:3.12-slim"
    # microVM 是长驻的：同一 session 的多次执行复用同一台，省掉重复冷启动。
    # 空闲超过这个秒数就回收，避免闲置 VM 一直占着内存。
    microvm_idle_seconds: int = 300
    # 单次启动（含首次拉镜像）的等待上限
    microvm_boot_timeout: int = 300

    # --- 执行引擎护栏 ---
    # 单次执行（两次人工介入之间的那一段）的墙钟上限，runner 里强制执行
    max_run_seconds: int = 600
    # 单次模型调用的超时。流式调用时是"两段输出之间最长沉默多久"，非流式是整次
    # 请求。不设的话走 SDK 默认的 10 分钟、外加重试——一个挂住的服务商能把一个
    # 节点拖上半小时
    model_timeout_seconds: int = 300
    max_graph_steps: int = 200
    # 节点上配多少步都过不去这个硬顶。默认值仍是节点上的 12（nodeDefs.ts），
    # 这里只决定"想调大的人最多能调到哪"——实测 87% 的 agent 节点 ≤10 步就收口，
    # 绷紧的是那 10%，不该让所有人陪着多花钱
    max_agent_steps: int = 100
    #: 上传文件的大小上限（MB）。原来写死 10，而且是读完整个文件才检查——
    #: 传一个 1GB 的文件，内存在报 413 之前就吃掉了
    max_upload_mb: int = 50
    max_concurrent_runs: int = 20

    # --- 定时任务 ---
    # 后台调度循环开不开（AGENTLAB_SCHEDULER=0 / false / off 关掉）。拷一份数据目录另起的服务
    # （检查脚本的 --stacks、沙箱）必须关：库里的定时任务会照常到点发起运行、发出 webhook，
    # 拷贝里的任务和原库的是同一批，等于同一件事做两遍
    scheduler: bool = True
    # 调度循环一拍的间隔（秒）：定时认领、事件派发、流程的时限和核实都按它查一次。缺省 15；验收脚本（多进程端到端）调小
    scheduler_tick_s: float = 15.0

    # --- 工具 ---
    http_tool_timeout: int = 20
    http_tool_max_bytes: int = 2_000_000
    # 逗号分隔的域名白名单；为空表示不限制（仅拦截内网地址）
    http_tool_allowlist: Annotated[list[str], NoDecode] = []
    # 本机代理 fake-ip 模式给的假地址段，逗号分隔（Clash / mihomo 默认 198.18.0.0/15）。
    # 为空表示不认：解析到这些地址一律按内网拦截。配了之后，落在段里的解析结果改向
    # 下面的公共 DNS 核实真实地址——不能直接放行，内网域名在 fake-ip 下也是这个段
    http_tool_fake_ip_ranges: Annotated[list[str], NoDecode] = []
    # 核实用的 DNS-over-HTTPS（JSON 接口），按顺序试。写 IP 形式免得解析 DoH 自己的域名。
    # 境外的排前面：境内公共 DNS 对被封锁的域名返回污染结果（维基百科的 AAAA 给 2001::1），
    # 会被当成内网误拦；开 fake-ip 的机器本来就有代理，连得上境外的 DoH
    http_tool_doh_urls: Annotated[list[str], NoDecode] = [
        "https://1.1.1.1/dns-query",
        "https://223.5.5.5/resolve",
    ]

    @field_validator("http_tool_allowlist", "http_tool_fake_ip_ranges", "http_tool_doh_urls", "frame_ancestors",
                     "embed_origins", mode="before")
    @classmethod
    def _comma_list(cls, value: object) -> object:
        # 环境变量里写逗号分隔；也兼容 JSON 数组的写法
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                import json

                return json.loads(text)
            return [part.strip() for part in text.split(",") if part.strip()]
        return value

    @field_validator("public_url")
    @classmethod
    def _valid_public_url(cls, value: str) -> str:
        # 写错了启动时就报出来：以前拼出 agentlab.example.com/api/auth/oidc/callback 这样的回调，到登录时才发现
        from app.core.public_url import ENV_NAME, normalize, url_problem

        if not value.strip():
            return ""
        if problem := url_problem(value):
            what = "不能带 ? 或 # 之后的部分" if problem == "suffix" else "需要是 http:// 或 https:// 开头的完整地址"
            raise ValueError(f"{ENV_NAME}={value!r} {what}，例如 https://agentlab.example.com")
        return normalize(value) or ""

    @field_validator("frame_ancestors", "embed_origins")
    @classmethod
    def _valid_origins(cls, value: list[str], info: Any) -> list[str]:
        # 只收「协议://主机[:端口]」，主机可以写 *.example.com。路径、裸 *、没写协议的写错了启动时就报出来，别到嵌入
        # 失败（或者整站被谁都能嵌）才发现。'self' 本来就有，写了也不重复
        name = f"AGENTLAB_{(info.field_name or '').upper()}"
        out: list[str] = []
        for raw in value:
            item = raw.strip().rstrip("/")
            if item == "'self'":
                continue
            scheme, _, host = item.partition("://")
            if scheme.lower() not in ("http", "https") or not _ORIGIN_HOST.fullmatch(host):
                raise ValueError(f"{name} 里的「{raw}」需要写成「协议://主机」（可以带端口），例如 https://portal.example.com")
            if (origin := f"{scheme.lower()}://{host.lower()}") not in out:
                out.append(origin)
        return out

    @field_validator("auth_cookie_secure", mode="before")
    @classmethod
    def _blank_is_auto(cls, value: object) -> object:
        # AGENTLAB_AUTH_COOKIE_SECURE= 写成空值等于没设（按请求定）
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("scheduler", mode="before")
    @classmethod
    def _blank_is_default(cls, value: object) -> object:
        # AGENTLAB_SCHEDULER= 写成空值等于没设（默认开）。其余写法照 pydantic 的布尔解析：
        # 0 / false / off / no 关，1 / true / on / yes 开，认不出的值启动时就报错，不猜
        if isinstance(value, str) and not value.strip():
            return True
        return value

    @field_validator("database_url", "checkpoint_url", "database_direct_url", mode="before")
    @classmethod
    def _valid_postgres_url(cls, value: object, info: Any) -> object:
        # 写错了启动时就报出来，别等到第一次查库。只认 PostgreSQL：SQLite 就是不设
        if value is None or (isinstance(value, str) and not value.strip()):
            return ""
        if not isinstance(value, str):
            return value
        from app.db.urls import normalize_database_url, normalize_checkpoint_url

        name = f"AGENTLAB_{(info.field_name or '').upper()}"
        normalize = normalize_checkpoint_url if info.field_name == "checkpoint_url" else normalize_database_url
        try:
            return normalize(value.strip())
        except ValueError as e:
            raise ValueError(f"{name}：{e}") from None

    @field_validator("workers")
    @classmethod
    def _valid_workers(cls, value: int) -> int:
        if value < 1:
            raise ValueError("AGENTLAB_WORKERS 至少是 1")
        return value

    @field_validator("worker_heartbeat_s", "worker_lease_s")
    @classmethod
    def _positive_seconds(cls, value: float, info: Any) -> float:
        if value <= 0:
            raise ValueError(f"AGENTLAB_{(info.field_name or '').upper()} 要大于 0")
        return value

    @field_validator("web_dist", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # AGENTLAB_WEB_DIST= 写成空值等于没设，别变成 Path("") 也就是当前目录
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("web_dist")
    @classmethod
    def _relative_to_project(cls, value: Path | None) -> Path | None:
        if value is None or value.is_absolute():
            return value
        return PROJECT_DIR / value

    @field_validator("http_tool_fake_ip_ranges")
    @classmethod
    def _valid_ranges(cls, value: list[str]) -> list[str]:
        for cidr in value:
            ipaddress.ip_network(cidr, strict=False)  # 写错就在启动时报出来，别到运行时才静默失效
        return value

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agentlab.db"

    @property
    def db_url(self) -> str:
        """应用库的 SQLAlchemy 地址：设了 database_url 用它（已规范成 postgresql+asyncpg://），否则数据目录里的 SQLite。"""
        return self.database_url or f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def db_dialect(self) -> str:
        """"sqlite" 或 "postgresql"。"""
        return "postgresql" if self.database_url else "sqlite"

    @property
    def checkpoint_path(self) -> Path:
        """LangGraph checkpointer 用独立的库文件，避免和业务表抢写锁。只在 SQLite 时用。"""
        return self.data_dir / "checkpoints.db"

    @property
    def session_db_url(self) -> str:
        """依赖会话状态的连接（LISTEN、会话级咨询锁、断点库）用的应用库地址：设了直连地址用它，否则同 database_url。"""
        return self.database_direct_url or self.database_url

    @property
    def checkpoint_conninfo(self) -> str | None:
        """PostgreSQL 断点库的 libpq 连接串（psycopg 用）：checkpoint_url 优先，否则由直连地址（没设就是 database_url）换算。
        SQLite 时 None。断点库用预备语句、建表时取会话级的锁，所以缺省走直连。"""
        if self.checkpoint_url:
            return self.checkpoint_url
        if self.database_url:
            from app.db.urls import libpq_conninfo

            return libpq_conninfo(self.session_db_url)
        return None

    @property
    def workspace_dir(self) -> Path:
        """沙箱与文件工具的可写根目录，一切文件访问都被限制在这里面。"""
        return self.data_dir / "workspace"

    def session_dir(self, session_id: str | None, *, sub: str = "") -> Path:
        """会话工作目录。session_id 一律先净化再拼路径。

        这个函数存在的理由：session_id 有些调用点是外部可控的（工具 API 的
        请求体就能指定），拿它直接拼路径就等于把工作区根目录交给调用方摆布，
        后面再怎么做越界检查都是在被挪走的根下面做的，形同虚设。
        """
        safe = sanitize_session(session_id)
        root = (self.workspace_dir / (sub or "") / safe).resolve()
        base = self.workspace_dir.resolve()
        # 净化之后理应不可能跑出去，这里再断言一次：这条路径值得多一道保险
        if root != base and base not in root.parents:
            raise ValueError(f"非法的 session 目录：{session_id!r}")
        return root

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.workspace_dir, self.uploads_dir):
            d.mkdir(parents=True, exist_ok=True)
        # 认得出「这是 AgentLab 的数据目录」的标记：应用库放在 PostgreSQL 时这里没有 agentlab.db，设了 AGENTLAB_SECRET_KEY
        # 时也没有 .secret_key。deploy.sh 改属主前按它认，免得 DATA_DIR 写错把别人的目录 chown 了。只建一次，不覆盖
        marker = self.data_dir / DATA_DIR_MARKER
        if not marker.exists():
            from app.core import atomic

            try:
                atomic.create_text(marker, "AgentLab 的数据目录（deploy.sh 按这个文件认，别删）\n")
            except OSError:
                pass


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


settings = get_settings()
