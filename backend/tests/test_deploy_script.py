"""deploy/deploy.sh：内置 PostgreSQL 的起停顺序、生成的 docker 参数、模式互斥、升级前备份、migrate-to-postgres 的回退。

docker 换成 tests/fake_docker.py（PATH 最前面），不碰本机的 docker；脚本照打包时那样填好版本和架构，放进临时目录跑。
真起容器的端到端见 deploy/README.md「用 PostgreSQL」末尾记的做法（不在单测里跑）。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fake_docker import host_arch

REPO = Path(__file__).resolve().parents[2]
FAKE = Path(__file__).resolve().parent / "fake_docker.py"
VERSION = "t1"
APP = "agl-t"
PG = f"{APP}-postgres"
NET = f"{APP}-net"
VOL = f"{APP}-pgdata"
PG_IMAGE = "postgres:16.15"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="要 bash")


class Deploy:
    def __init__(self, root: Path, *, env_lines: list[str] | None = None) -> None:
        self.root = root
        self.pkg = root / "pkg"
        self.pkg.mkdir()
        text = (REPO / "deploy" / "deploy.sh").read_text(encoding="utf-8")
        text = text.replace("__AGENTLAB_VERSION__", VERSION).replace("__AGENTLAB_ARCH__", host_arch())
        (self.pkg / "deploy.sh").write_text(text, encoding="utf-8")
        self.write_env(env_lines or [])
        (self.pkg / "image.tar").write_text(f"agentlab:{VERSION} 10001:10001\n")
        (self.pkg / "postgres-image.tar").write_text(f"{PG_IMAGE} \n")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "{FAKE}" "$@"\n')
        docker.chmod(0o755)
        self.state_file = root / "docker-state.json"
        self.log_file = root / "docker-calls.jsonl"
        self.env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(root), "LANG": "C.UTF-8",
            "FAKE_DOCKER_STATE": str(self.state_file), "FAKE_DOCKER_LOG": str(self.log_file),
        }

    @property
    def data(self) -> Path:
        return (self.pkg / "data").resolve()

    def write_env(self, extra: list[str]) -> None:
        lines = ["HOST_PORT=18765", "BIND_ADDR=127.0.0.1", "DATA_DIR=./data", f"CONTAINER_NAME={APP}",
                 "ENABLE_MICROVM=0", "ENABLE_BWRAP=0", "AGENTLAB_SECRET_KEY=", *extra]
        (self.pkg / "agentlab.env").write_text("\n".join(lines) + "\n")

    def __call__(self, *args: str, ok: bool = True, **env: str) -> subprocess.CompletedProcess:
        self.log_file.write_text("")
        out = subprocess.run(["bash", str(self.pkg / "deploy.sh"), *args], capture_output=True, text=True,
                             env={**self.env, **env}, timeout=120)
        if ok:
            assert out.returncode == 0, out.stdout + out.stderr
        return out

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log_file.read_text().splitlines() if line.strip()]

    def state(self) -> dict:
        return json.loads(self.state_file.read_text()) if self.state_file.exists() else {"containers": {}}

    def containers(self) -> dict:
        return self.state()["containers"]

    def mode(self) -> str:
        path = self.data / ".postgres" / "state"
        if not path.exists():
            return "sqlite"
        return next(line.split("=", 1)[1] for line in path.read_text().splitlines() if line.startswith("mode="))


def index_of(calls: list[list[str]], pred) -> int:
    for i, argv in enumerate(calls):
        if pred(argv):
            return i
    raise AssertionError("没找到这次调用：\n" + "\n".join(" ".join(c) for c in calls))


def runs_named(name: str):
    return lambda argv: argv[:1] == ["run"] and "--name" in argv and argv[argv.index("--name") + 1] == name


def is_cmd(cmd: str, name: str):
    return lambda argv: argv[:1] == [cmd] and argv[-1] == name


def oneoff(entry: str, *words: str):
    return lambda argv: (argv[:1] == ["run"] and "--rm" in argv and "--entrypoint" in argv
                         and argv[argv.index("--entrypoint") + 1] == entry and all(w in argv for w in words))


def make_sqlite_data(data: Path) -> None:
    data.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(data / "agentlab.db")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO t (v) VALUES ('一')")
    db.commit()
    db.close()
    (data / ".secret_key").write_text("x")


@pytest.fixture
def deploy(tmp_path):
    return Deploy(tmp_path)


# --------------------------------------------------------------------------
# 参数
# --------------------------------------------------------------------------


@pytest.mark.parametrize("args", [["bogus"], ["start", "--postgres"], ["upgrade", "--postgres"], ["status", "--postgres"],
                                  ["restart", "--all"],
                                  ["install", "--purge-image"], ["migrate-to-postgres", "-f"], ["status", "--tail", "3"]])
def test_unknown_arguments_are_refused_before_doing_anything(deploy, args):
    out = deploy(*args, ok=False)
    assert out.returncode == 2
    assert "认不出" in out.stderr
    assert deploy.calls() == [], "认参数时一次 docker 都没调"


def test_help_lists_the_postgres_commands(deploy):
    out = deploy("--help")
    for word in ("install [--postgres]", "migrate-to-postgres", "restart [--postgres]", "logs [-f] [--tail N] [--postgres]",
                 "DEPLOY_<名字>"):
        assert word in out.stdout


# --------------------------------------------------------------------------
# install --postgres
# --------------------------------------------------------------------------


def test_install_postgres_starts_the_database_first_and_wires_the_app_without_the_password(deploy):
    out = deploy("install", "--postgres")
    calls = deploy.calls()
    net = index_of(calls, lambda a: a[:2] == ["network", "create"] and a[-1] == NET)
    pg = index_of(calls, runs_named(PG))
    pg_ready = index_of(calls, lambda a: a[:1] == ["inspect"] and a[-1] == PG and ".State.Health" in a[2])
    app = index_of(calls, runs_named(APP))
    assert net < pg < pg_ready < app, "先建网络、起库、等库就绪，再起服务"

    pg_args = calls[pg]
    assert "-p" not in pg_args and not any(a.startswith("--publish") for a in pg_args), "库的端口不对外"
    assert pg_args[pg_args.index("--network") + 1] == NET
    assert f"{VOL}:/var/lib/postgresql/data" in pg_args
    assert "POSTGRES_PASSWORD_FILE=/run/agentlab/password" in pg_args
    assert any(a.endswith("/.postgres/password:/run/agentlab/password:ro") for a in pg_args)
    assert "pg_isready -h 127.0.0.1 -U agentlab -d agentlab" in pg_args
    assert pg_args[-1] == PG_IMAGE

    app_args = calls[app]
    assert app_args[app_args.index("--network") + 1] == NET
    assert f"AGENTLAB_DATABASE_URL=postgresql+asyncpg://agentlab@{PG}:5432/agentlab" in app_args
    assert "PGPASSFILE=/data/.postgres/pgpass" in app_args
    assert "127.0.0.1:18765:8000" in app_args

    secret = (deploy.data / ".postgres" / "password").read_text().strip()
    assert len(secret) >= 32
    everything = json.dumps(calls, ensure_ascii=False) + out.stdout + out.stderr
    assert secret not in everything, "密码不进任何 docker 参数和输出"
    for name, mode in (("password", 0o600), ("pgpass", 0o600), ("state", 0o644)):
        assert stat.S_IMODE((deploy.data / ".postgres" / name).stat().st_mode) == mode, name
    assert stat.S_IMODE((deploy.data / ".postgres").stat().st_mode) == 0o711
    pgpass = (deploy.data / ".postgres" / "pgpass").read_text().strip()
    assert pgpass.startswith(f"{PG}:5432:agentlab:agentlab:")
    assert deploy.mode() == "builtin"
    assert "内置 PostgreSQL" in out.stdout
    assert deploy.containers()[PG]["labels"]["agentlab.role"] == "postgres"


def test_once_builtin_later_installs_keep_using_it_without_the_flag(deploy):
    deploy("install", "--postgres")
    secret = (deploy.data / ".postgres" / "password").read_text()
    deploy("install")
    calls = deploy.calls()
    assert not any(runs_named(PG)(a) for a in calls), "库的容器已经在跑，不重建"
    app_args = calls[index_of(calls, runs_named(APP))]
    assert f"AGENTLAB_DATABASE_URL=postgresql+asyncpg://agentlab@{PG}:5432/agentlab" in app_args
    assert (deploy.data / ".postgres" / "password").read_text() == secret, "密码沿用"


def test_default_install_is_sqlite_and_touches_no_postgres(deploy):
    deploy("install")
    calls = deploy.calls()
    app_args = calls[index_of(calls, runs_named(APP))]
    assert not any("AGENTLAB_DATABASE_URL" in a for a in app_args) and "--network" not in app_args
    assert not any(a[:2] == ["network", "create"] for a in calls)
    assert PG not in deploy.containers()
    assert not (deploy.data / ".postgres").exists()


def test_builtin_and_external_are_mutually_exclusive(tmp_path):
    deploy = Deploy(tmp_path, env_lines=["AGENTLAB_DATABASE_URL=postgresql+asyncpg://u:p@db.example:5432/agentlab"])
    out = deploy("install", "--postgres", ok=False)
    assert out.returncode == 1 and "两种库只能选一种" in out.stderr and "AGENTLAB_DATABASE_URL" in out.stderr
    assert APP not in deploy.containers() and PG not in deploy.containers()
    out = deploy("migrate-to-postgres", ok=False)
    assert "外接 PostgreSQL" in out.stderr
    # 不加 --postgres：外接的照常用，不起内置的
    deploy("install")
    assert PG not in deploy.containers()
    assert "外接 PostgreSQL" in deploy("status").stdout


def test_external_url_on_a_builtin_data_dir_is_refused(deploy):
    deploy("install", "--postgres")
    deploy.write_env(["AGENTLAB_DATABASE_URL=postgresql+asyncpg://u:p@db.example:5432/agentlab"])
    out = deploy("install", ok=False)
    assert "两种库只能选一种" in out.stderr
    # 停服务这类不改配置的照样能做，按数据目录里记的管两个容器
    deploy("stop")
    assert deploy.containers()[PG]["state"] == "exited"


def test_install_postgres_on_a_sqlite_data_dir_points_to_migrate(deploy):
    make_sqlite_data(deploy.pkg / "data")
    out = deploy("install", "--postgres", ok=False)
    assert "migrate-to-postgres" in out.stderr
    assert deploy.containers() == {} and deploy.mode() == "sqlite"


def test_install_postgres_refuses_a_leftover_volume(deploy):
    deploy.state_file.write_text(json.dumps({"images": {}, "containers": {}, "networks": [], "volumes": [VOL]}))
    out = deploy("install", "--postgres", ok=False)
    assert VOL in out.stderr and "docker volume rm" in out.stderr
    assert PG not in deploy.containers()


def test_deploy_overrides_take_names_from_the_environment(deploy):
    env = {"DEPLOY_CONTAINER_NAME": "agl-x", "DEPLOY_POSTGRES_CONTAINER": "agl-x-db", "DEPLOY_DOCKER_NETWORK": "agl-x-n",
           "DEPLOY_POSTGRES_VOLUME": "agl-x-v", "DEPLOY_HOST_PORT": "18766", "DEPLOY_POSTGRES_IMAGE": "postgres:16"}
    (deploy.pkg / "postgres-image.tar").write_text("postgres:16 \n")
    deploy("install", "--postgres", **env)
    calls = deploy.calls()
    pg_args = calls[index_of(calls, runs_named("agl-x-db"))]
    assert pg_args[-1] == "postgres:16" and "agl-x-v:/var/lib/postgresql/data" in pg_args
    app_args = calls[index_of(calls, runs_named("agl-x"))]
    assert "127.0.0.1:18766:8000" in app_args and app_args[app_args.index("--network") + 1] == "agl-x-n"
    assert set(deploy.containers()) == {"agl-x", "agl-x-db"}


@pytest.mark.parametrize("name,value", [("DEPLOY_POSTGRES_CONTAINER", APP), ("DEPLOY_DOCKER_NETWORK", "bad name"),
                                        ("DEPLOY_BACKUP_KEEP", "0"), ("DEPLOY_POSTGRES_IMAGE", "a b")])
def test_bad_overrides_are_refused(deploy, name, value):
    out = deploy("install", "--postgres", ok=False, **{name: value})
    assert out.returncode == 1 and "错误：" in out.stderr
    assert deploy.containers() == {}


# --------------------------------------------------------------------------
# start / stop / restart / status / logs / uninstall
# --------------------------------------------------------------------------


def test_start_stop_restart_order_the_two_containers(deploy):
    deploy("install", "--postgres")
    deploy("stop")
    calls = deploy.calls()
    assert index_of(calls, is_cmd("stop", APP)) < index_of(calls, is_cmd("stop", PG)), "先停服务再停库"
    assert {c["state"] for c in deploy.containers().values()} == {"exited"}

    deploy("start")
    calls = deploy.calls()
    pg_start = index_of(calls, is_cmd("start", PG))
    pg_ready = index_of(calls, lambda a: a[:1] == ["inspect"] and a[-1] == PG and ".State.Health" in a[2])
    assert pg_start < pg_ready < index_of(calls, is_cmd("start", APP)), "先起库、等就绪，再起服务"

    deploy("restart", "--postgres")
    calls = deploy.calls()
    order = [index_of(calls, is_cmd("stop", APP)), index_of(calls, is_cmd("stop", PG)),
             index_of(calls, is_cmd("start", PG)), index_of(calls, is_cmd("start", APP))]
    assert order == sorted(order), "--postgres：先停服务、再停库，先起库、再起服务"
    assert {c["state"] for c in deploy.containers().values()} == {"running"}


def test_restart_by_default_leaves_the_database_alone(deploy):
    deploy("install", "--postgres")
    out = deploy("restart")
    calls = deploy.calls()
    assert ["restart", APP] in calls
    assert not any(a[:1] in (["stop"], ["start"], ["restart"]) and a[-1] == PG for a in calls), "库不跟着重启"
    assert "已重启" in out.stdout
    # 库停着时先把库起来，服务才连得上
    state = deploy.state()
    state["containers"][PG]["state"] = "exited"
    deploy.state_file.write_text(json.dumps(state))
    deploy("restart")
    calls = deploy.calls()
    assert index_of(calls, is_cmd("start", PG)) < index_of(calls, lambda a: a == ["restart", APP])
    assert not any(is_cmd("stop", PG)(a) for a in calls)
    assert deploy.containers()[PG]["state"] == "running"


def test_restart_postgres_needs_the_builtin_database(deploy):
    deploy("install")
    out = deploy("restart", "--postgres", ok=False)
    assert "没用内置 PostgreSQL" in out.stderr
    assert not any(a[:1] in (["stop"], ["restart"]) for a in deploy.calls()), "什么都没动"


def test_status_and_logs_show_the_database(deploy):
    deploy("install", "--postgres")
    out = deploy("status").stdout
    assert "内置 PostgreSQL" in out and "pg_isready：healthy" in out and "端口不对外" in out
    deploy("logs", "--postgres", "--tail", "5")
    assert deploy.calls()[-1] == ["logs", "--tail", "5", PG]
    deploy("logs")
    assert deploy.calls()[-1] == ["logs", "--tail", "200", APP]


def test_logs_postgres_without_builtin_says_so(deploy):
    deploy("install")
    out = deploy("logs", "--postgres", ok=False)
    assert "没用内置 PostgreSQL" in out.stderr


def test_uninstall_removes_both_containers_and_the_network_but_keeps_the_volume(deploy):
    deploy("install", "--postgres")
    out = deploy("uninstall")
    state = deploy.state()
    assert state["containers"] == {} and NET not in state["networks"]
    assert VOL in state["volumes"], "数据卷留着"
    assert f"docker volume rm {VOL}" in out.stdout
    assert PG_IMAGE in state["images"], "PostgreSQL 的镜像不删"
    assert (deploy.data / ".postgres" / "password").exists()
    # 卸载后同一个数据目录再装：照旧用那个数据卷和密码
    deploy("install")
    assert deploy.containers()[PG]["mounts"]["/var/lib/postgresql/data"] == VOL


def test_uninstall_refuses_a_foreign_container_with_the_postgres_name(deploy):
    deploy("install")
    state = deploy.state()
    state["images"]["redis:7"] = {"arch": host_arch(), "user": ""}
    state["containers"][PG] = {"image": "redis:7", "state": "running", "env": {}, "labels": {}, "mounts": {},
                               "ports": [], "network": "bridge"}
    deploy.state_file.write_text(json.dumps(state))
    out = deploy("uninstall", ok=False)
    assert "不是 deploy.sh 起的内置 PostgreSQL" in out.stderr
    assert PG in deploy.containers()


# --------------------------------------------------------------------------
# upgrade：先备份
# --------------------------------------------------------------------------


def test_upgrade_builtin_dumps_after_stopping_the_app_and_keeps_the_last_n(deploy):
    deploy("install", "--postgres")
    folder = deploy.data / "backups" / "postgres"
    folder.mkdir(parents=True)
    for i in range(4):
        (folder / f"agentlab-2026010{i}T000000Z.dump").write_bytes(b"old")
    deploy("upgrade", DEPLOY_BACKUP_KEEP="3")
    calls = deploy.calls()
    stop = index_of(calls, is_cmd("stop", APP))
    dump = index_of(calls, oneoff("sh", "pg_dump"))
    new_app = index_of(calls, runs_named(APP))
    assert stop < dump < new_app, "停了旧服务再备份，备份完才起新的"
    dump_args = calls[dump]
    assert dump_args[dump_args.index("--network") + 1] == NET and "PGPASSFILE=/data/.postgres/pgpass" in dump_args
    assert dump_args[dump_args.index("-c") + 1].startswith("umask 077 && "), "备份文件从建出来就是 0600"
    assert dump_args[dump_args.index("--user") + 1] == "10001:10001", "属主照数据目录：容器里的用户"
    kept = sorted(p.name for p in folder.iterdir())
    assert len(kept) == 3 and kept[-1].startswith("agentlab-") and kept[-1] > "agentlab-20260103", kept
    assert (folder / kept[-1]).read_bytes() == b"PGDMP fake"
    assert stat.S_IMODE((folder / kept[-1]).stat().st_mode) == 0o600
    for level in (deploy.data / "backups", folder):
        assert stat.S_IMODE(level.stat().st_mode) == 0o700, level


def test_upgrade_sqlite_backs_up_the_database_files(deploy):
    deploy("install")
    make_sqlite_data(deploy.pkg / "data")
    deploy("upgrade")
    calls = deploy.calls()
    assert index_of(calls, is_cmd("stop", APP)) < index_of(calls, oneoff("python", "backup-sqlite")) \
        < index_of(calls, runs_named(APP))
    (stamp,) = list((deploy.data / "backups" / "sqlite").iterdir())
    copy = sqlite3.connect(stamp / "agentlab.db")
    assert copy.execute("SELECT v FROM t").fetchall() == [("一",)]
    copy.close()
    for level in (deploy.data / "backups", deploy.data / "backups" / "sqlite", stamp):
        assert stat.S_IMODE(level.stat().st_mode) == 0o700, level
    assert stat.S_IMODE((stamp / "agentlab.db").stat().st_mode) == 0o600


def test_failed_backup_aborts_the_upgrade_and_restarts_the_old_service(deploy):
    deploy("install", "--postgres")
    before = deploy.containers()[APP]
    out = deploy("upgrade", ok=False, FAKE_PGDUMP_FAIL="1")
    assert "备份没做成，没有升级" in out.stderr
    after = deploy.containers()[APP]
    assert after["state"] == "running" and after == before
    assert not any(runs_named(APP)(a) for a in deploy.calls())


def test_upgrade_with_a_new_postgres_image_recreates_the_database_container(deploy):
    deploy("install", "--postgres")
    (deploy.pkg / "postgres-image.tar").write_text("postgres:16.99 \n")
    deploy("upgrade", DEPLOY_POSTGRES_IMAGE="postgres:16.99")
    calls = deploy.calls()
    assert index_of(calls, is_cmd("stop", APP)) < index_of(calls, runs_named(PG))
    pg = deploy.containers()[PG]
    assert pg["image"] == "postgres:16.99" and pg["mounts"]["/var/lib/postgresql/data"] == VOL


def test_a_postgres_image_that_cannot_start_is_rolled_back(deploy):
    deploy("install", "--postgres")
    (deploy.pkg / "postgres-image.tar").write_text("postgres:17.1 \n")
    out = deploy("upgrade", ok=False, DEPLOY_POSTGRES_IMAGE="postgres:17.1", FAKE_UNHEALTHY_IMAGE="postgres:17.1")
    assert "已换回原来的 postgres:16.15" in out.stderr
    containers = deploy.containers()
    assert containers[PG]["image"] == PG_IMAGE and containers[PG]["state"] == "running"
    assert containers[APP]["state"] == "running", "原来在跑的服务起回来了"


# --------------------------------------------------------------------------
# migrate-to-postgres
# --------------------------------------------------------------------------


def _sqlite_deployment(deploy: Deploy) -> None:
    make_sqlite_data(deploy.pkg / "data")
    deploy("install")
    assert deploy.mode() == "sqlite"


def test_migrate_runs_the_steps_in_order_and_switches(deploy):
    _sqlite_deployment(deploy)
    out = deploy("migrate-to-postgres")
    calls = deploy.calls()
    steps = [
        index_of(calls, is_cmd("stop", APP)),
        index_of(calls, oneoff("python", "backup-sqlite")),
        index_of(calls, runs_named(PG)),
        index_of(calls, lambda a: a[:2] == ["exec", PG] and "psql" in a),
        index_of(calls, oneoff("python", "copy-to-postgres", "--dry-run")),
        index_of(calls, lambda a: oneoff("python", "copy-to-postgres")(a) and "--dry-run" not in a),
        index_of(calls, oneoff("python", "state", "builtin")),
        index_of(calls, runs_named(APP)),
    ]
    assert steps == sorted(steps), steps
    copy = calls[steps[5]]
    assert f"postgresql+asyncpg://agentlab@{PG}:5432/agentlab" in copy and "--assume-stopped" in copy
    assert "PGPASSFILE=/data/.postgres/pgpass" in copy
    assert deploy.mode() == "builtin"
    app = deploy.containers()[APP]
    assert app["env"]["AGENTLAB_DATABASE_URL"].endswith(f"@{PG}:5432/agentlab") and app["state"] == "running"
    assert f"{APP}-previous" not in deploy.containers()
    assert (deploy.data / "agentlab.db").exists(), "SQLite 的库文件留着"
    assert list((deploy.data / "backups" / "sqlite").iterdir())
    assert "已切到内置 PostgreSQL" in out.stdout
    # 再跑一次：什么都不做
    again = deploy("migrate-to-postgres")
    assert "已经切到内置 PostgreSQL" in again.stdout
    assert not any(a[:1] in (["stop"], ["run"]) for a in deploy.calls() if "--rm" not in a)


@pytest.mark.parametrize("fail", ["dry", "real"])
def test_a_failed_copy_leaves_the_sqlite_service_as_it_was_and_rerunning_works(deploy, fail):
    _sqlite_deployment(deploy)
    before = deploy.containers()[APP]
    out = deploy("migrate-to-postgres", ok=False, FAKE_COPY_FAIL=fail)
    assert "没有切换，仍用 SQLite" in out.stderr
    containers = deploy.containers()
    assert containers[APP] == before, "服务原样起回来，还是 SQLite 的那个容器"
    assert containers[PG]["state"] == "exited", "内置库停掉"
    assert deploy.mode() == "migrating"
    assert "上次 migrate-to-postgres 没切成" in deploy("status").stdout
    # 重新运行：数据卷认得出是自己上次留下的，清空再导
    deploy("migrate-to-postgres")
    calls = deploy.calls()
    assert index_of(calls, lambda a: a[:2] == ["exec", PG] and "psql" in a) \
        < index_of(calls, oneoff("python", "copy-to-postgres", "--dry-run"))
    psql = calls[index_of(calls, lambda a: a[:2] == ["exec", PG] and "psql" in a)]
    assert any("DROP DATABASE IF EXISTS" in a for a in psql)
    assert deploy.mode() == "builtin"


def test_a_stopped_service_stays_stopped_after_a_failed_migration(deploy):
    _sqlite_deployment(deploy)
    deploy("stop")
    deploy("migrate-to-postgres", ok=False, FAKE_COPY_FAIL="real")
    assert deploy.containers()[APP]["state"] == "exited"


def test_an_unhealthy_new_service_is_swapped_back_to_the_sqlite_one(deploy):
    _sqlite_deployment(deploy)
    before = deploy.containers()[APP]
    out = deploy("migrate-to-postgres", ok=False, FAKE_UNHEALTHY_ENV="AGENTLAB_DATABASE_URL")
    assert "没有切换，仍用 SQLite" in out.stderr
    containers = deploy.containers()
    assert containers[APP] == before and f"{APP}-previous" not in containers
    assert containers[PG]["state"] == "exited"
    assert deploy.mode() == "migrating", "记成 builtin 的改回来了"


def test_migrate_without_an_app_container_removes_the_failed_one(deploy):
    make_sqlite_data(deploy.pkg / "data")
    deploy("migrate-to-postgres", ok=False, FAKE_UNHEALTHY_ENV="AGENTLAB_DATABASE_URL")
    assert APP not in deploy.containers(), "按内置库起的、没通过健康检查的容器不留着"
    assert deploy.mode() == "migrating"


def test_migrate_refuses_a_volume_it_did_not_create(deploy):
    _sqlite_deployment(deploy)
    state = deploy.state()
    state["volumes"].append(VOL)
    deploy.state_file.write_text(json.dumps(state))
    out = deploy("migrate-to-postgres", ok=False)
    assert VOL in out.stderr and "没有动它" in out.stderr
    calls = deploy.calls()
    assert not any(a[:1] == ["stop"] or a[:1] == ["exec"] for a in calls), "服务没停、库没碰"
    assert deploy.mode() == "sqlite"


def test_migrate_needs_a_sqlite_database(deploy):
    out = deploy("migrate-to-postgres", ok=False)
    assert "install --postgres" in out.stderr


def test_package_script_takes_the_postgres_image_from_deploy_sh():
    """包里带的 PostgreSQL 镜像和 deploy.sh 要的是同一个标签：标签只在 deploy.sh 里定一处。"""
    deploy_sh = (REPO / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    assert f'POSTGRES_IMAGE="{PG_IMAGE}"' in deploy_sh
    package = (REPO / "scripts" / "package-docker.sh").read_text(encoding="utf-8")
    assert "postgres:16" not in package, "打包脚本里别再写一份标签"
    out = subprocess.run(["sed", "-n", r's/^POSTGRES_IMAGE="\([^"]*\)"$/\1/p', str(REPO / "deploy" / "deploy.sh")],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == PG_IMAGE


def test_env_template_lists_the_postgres_settings_commented_out_with_the_defaults():
    """agentlab.env 模板末尾的「内置 PostgreSQL（可选）」：几项都注释着（不改任何人的部署），缺省值和 deploy.sh 里的一致。"""
    template = (REPO / "deploy" / "agentlab.env").read_text(encoding="utf-8")
    section = template[template.index("# ---- 内置 PostgreSQL（可选）----"):]
    assert template.rstrip().endswith(section.rstrip()), "追加在文件末尾"
    commented = dict(line[2:].split("=", 1) for line in section.splitlines()
                     if line.startswith("# ") and "=" in line and line[2:3].isupper())
    assert commented == {"POSTGRES_IMAGE": PG_IMAGE, "POSTGRES_CONTAINER": "agentlab-postgres",
                         "DOCKER_NETWORK": "agentlab-net", "POSTGRES_VOLUME": "agentlab-pgdata", "BACKUP_KEEP": "5"}
    assert not any(line.split("=", 1)[0] in commented for line in template.splitlines() if not line.startswith("#")), \
        "都是注释"


def test_settings_written_in_agentlab_env_take_effect(tmp_path):
    deploy = Deploy(tmp_path, env_lines=["POSTGRES_CONTAINER=agl-db", "DOCKER_NETWORK=agl-n", "POSTGRES_VOLUME=agl-v",
                                         "BACKUP_KEEP=1"])
    deploy("install", "--postgres")
    assert set(deploy.containers()) == {APP, "agl-db"}
    assert deploy.containers()["agl-db"]["mounts"]["/var/lib/postgresql/data"] == "agl-v"
    assert deploy.containers()[APP]["network"] == "agl-n"


def test_dev_script_refuses_unknown_arguments_and_documents_postgres():
    env = {**os.environ, "PATH": "/usr/bin:/bin"}
    out = subprocess.run(["bash", str(REPO / "scripts" / "dev.sh"), "--bogus"], capture_output=True, text=True, env=env)
    assert out.returncode == 2 and "认不出的参数" in out.stdout
    out = subprocess.run(["bash", str(REPO / "scripts" / "dev.sh"), "--help"], capture_output=True, text=True, env=env)
    assert out.returncode == 0 and "--postgres" in out.stdout and "AGENTLAB_DEV_PG_PORT" in out.stdout
