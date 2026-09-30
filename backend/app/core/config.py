from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BACKEND_DIR.parent

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

    # --- 前端构建产物（生产部署用）---
    # 设了就由服务端一并托管页面（见 app/web.py），页面和接口同源、只起一个进程。
    # 不设（开发模式）什么都不变：页面由 Vite 开发服务器提供。相对路径按仓库根目录算
    web_dist: Path | None = None

    # --- 密钥加密 ---
    # 用于加密落库的 provider api key。留空则自动生成并存到 data_dir/.secret_key。
    secret_key: str = ""

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

    @field_validator("http_tool_allowlist", "http_tool_fake_ip_ranges", "http_tool_doh_urls", mode="before")
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
        return f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def checkpoint_path(self) -> Path:
        """LangGraph checkpointer 用独立的库文件，避免和业务表抢写锁。"""
        return self.data_dir / "checkpoints.db"

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


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


settings = get_settings()
