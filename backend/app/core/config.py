from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BACKEND_DIR.parent


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

    # --- 密钥加密 ---
    # 用于加密落库的 provider api key。留空则自动生成并存到 data_dir/.secret_key。
    secret_key: str = ""

    # --- 沙箱 ---
    sandbox_backend: str = "auto"  # auto | docker | local | off
    sandbox_image: str = "python:3.12-slim"
    sandbox_timeout: int = 30
    sandbox_memory_mb: int = 512
    sandbox_cpus: float = 1.0
    sandbox_network: bool = False
    sandbox_max_output: int = 20_000

    # --- 执行引擎护栏 ---
    max_run_seconds: int = 600
    max_graph_steps: int = 200
    max_agent_steps: int = 25
    max_concurrent_runs: int = 20

    # --- 工具 ---
    http_tool_timeout: int = 20
    http_tool_max_bytes: int = 2_000_000
    # 逗号分隔的域名白名单；为空表示不限制（仅拦截内网地址）
    http_tool_allowlist: list[str] = []

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
