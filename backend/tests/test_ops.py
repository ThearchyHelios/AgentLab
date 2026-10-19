"""app/ops.py：deploy.sh、dev.sh 在容器里调的几件小事（密码文件、状态、SQLite 备份、删旧备份）。"""

from __future__ import annotations

import sqlite3
import stat

import pytest

from app import ops


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_secrets_are_generated_once_with_tight_permissions(tmp_path, capsys):
    assert ops.main(["postgres-secrets", "--data", str(tmp_path), "--host", "agl-postgres"]) == 0
    assert capsys.readouterr().out.strip() == "生成了新密码"
    folder = tmp_path / ".postgres"
    password = (folder / "password").read_text().strip()
    assert len(password) >= 32
    assert _mode(folder / "password") == 0o600 and _mode(folder / "pgpass") == 0o600
    assert _mode(folder) == 0o711, "别人列不出目录，但按名字读得到 state"
    assert (folder / "pgpass").read_text() == f"agl-postgres:5432:agentlab:agentlab:{password}\n"

    assert ops.main(["postgres-secrets", "--data", str(tmp_path), "--host", "other", "--port", "55432"]) == 0
    out = capsys.readouterr().out
    assert out.strip() == "沿用已有的密码" and password not in out, "密码不打印"
    assert (folder / "password").read_text().strip() == password
    assert (folder / "pgpass").read_text().startswith("other:55432:agentlab:agentlab:"), "pgpass 按现在的主机名重写"


def test_pgpass_escapes_colons_and_backslashes(tmp_path):
    folder = tmp_path / ".postgres-dev"
    folder.mkdir()
    (folder / "password").write_text("a:b\\c\n")
    assert ops.postgres_secrets(tmp_path, folder=".postgres-dev", host="127.0.0.1", port=55432, database="agentlab",
                                user="agentlab") is False
    assert (folder / "pgpass").read_text() == "127.0.0.1:55432:agentlab:agentlab:a\\:b\\\\c\n"


def test_an_empty_password_file_is_refused(tmp_path):
    (tmp_path / ".postgres").mkdir()
    (tmp_path / ".postgres" / "password").write_text("\n")
    with pytest.raises(SystemExit, match="是空的"):
        ops.postgres_secrets(tmp_path, folder=".postgres", host="h", port=5432, database="d", user="u")


def test_state_defaults_to_sqlite_and_records_switches(tmp_path, capsys):
    assert ops.read_state(tmp_path) == "sqlite"
    for mode in ("migrating", "builtin", "sqlite"):
        assert ops.main(["state", "--data", str(tmp_path), "--set", mode]) == 0
        assert capsys.readouterr().out.strip() == mode
        assert ops.read_state(tmp_path) == mode
    path = tmp_path / ".postgres" / "state"
    assert _mode(path) == 0o644, "宿主机上的 deploy.sh 直接读它"
    assert "since=" in path.read_text()
    path.write_text("mode=whatever\n")
    assert ops.read_state(tmp_path) == "sqlite", "认不出的当没切过"
    with pytest.raises(SystemExit):
        ops.main(["state", "--data", str(tmp_path), "--set", "external"])


def test_state_records_names_and_a_stable_deployment_id(tmp_path, capsys):
    assert ops.main(["state", "--data", str(tmp_path), "--set", "migrating", "--container", "agl-postgres",
                     "--volume", "agl-pgdata", "--network", "agl-net"]) == 0
    values = ops.read_state_values(tmp_path)
    assert values["mode"] == "migrating" and values["container"] == "agl-postgres" and values["volume"] == "agl-pgdata"
    dep = values["id"]
    assert len(dep) == 12
    since = values["since"]
    # 改模式：名字、编号照旧，since 换新
    ops.write_state(tmp_path, "builtin")
    again = ops.read_state_values(tmp_path)
    assert again["id"] == dep and again["network"] == "agl-net" and again["mode"] == "builtin"
    assert again["since"] >= since
    # 只补记名字：模式不变
    ops.write_state(tmp_path, None, container="agl-db")
    assert ops.read_state(tmp_path) == "builtin" and ops.read_state_values(tmp_path)["container"] == "agl-db"
    capsys.readouterr()
    assert ops.main(["state", "--data", str(tmp_path), "--get", "id"]) == 0
    assert capsys.readouterr().out.strip() == dep
    # 早先的状态文件（只有模式）：第一次写时补上编号
    (tmp_path / ".postgres" / "state").write_text("mode=migrating\n")
    ops.write_state(tmp_path, None, container="c", volume="v", network="n")
    old = ops.read_state_values(tmp_path)
    assert old["mode"] == "migrating" and old["id"] and old["id"] != dep
    with pytest.raises(SystemExit):
        ops.write_state(tmp_path, None, image="x")


def test_prune_keeps_the_newest_by_name(tmp_path):
    for name in ("agentlab-20260101T000000Z.dump", "agentlab-20260103T000000Z.dump", "agentlab-20260102T000000Z.dump"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "20260101T000000Z").mkdir()
    (tmp_path / "20260101T000000Z" / "agentlab.db").write_bytes(b"x")
    (tmp_path / ".hidden").write_bytes(b"x")
    gone = ops.prune(tmp_path, 2)
    assert sorted(p.name for p in gone) == ["20260101T000000Z", "agentlab-20260101T000000Z.dump"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [".hidden", "agentlab-20260102T000000Z.dump",
                                                          "agentlab-20260103T000000Z.dump"]
    assert ops.prune(tmp_path / "missing", 2) == []


def test_backup_sqlite_copies_a_consistent_snapshot_including_the_wal(tmp_path, capsys):
    db = sqlite3.connect(tmp_path / "agentlab.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE t (v TEXT)")
    db.execute("INSERT INTO t VALUES ('还在 WAL 里')")
    db.commit()
    # 不关连接、不做检查点：新写的那行还只在 -wal 里
    assert (tmp_path / "agentlab.db-wal").exists()
    sqlite3.connect(tmp_path / "checkpoints.db").close()
    try:
        assert ops.main(["backup-sqlite", "--data", str(tmp_path), "--keep", "2"]) == 0
    finally:
        db.close()
    target = tmp_path / "backups" / "sqlite"
    (stamp,) = list(target.iterdir())
    assert capsys.readouterr().out.strip() == str(stamp)
    copy = sqlite3.connect(stamp / "agentlab.db")
    assert copy.execute("SELECT v FROM t").fetchall() == [("还在 WAL 里",)]
    copy.close()
    assert (stamp / "checkpoints.db").exists()
    # 备份里是整个库：目录只有属主进得去，文件只有属主读得到
    for level in (tmp_path / "backups", target, stamp):
        assert _mode(level) == 0o700, level
    assert _mode(stamp / "agentlab.db") == 0o600 and _mode(stamp / "checkpoints.db") == 0o600
    assert sorted(p.name for p in stamp.iterdir()) == ["agentlab.db", "checkpoints.db"], "没留下 -journal 之类的"
    # 只留最近 keep 份
    for name in ("20200101T000000Z", "20200102T000000Z"):
        (target / name).mkdir()
    ops.backup_sqlite(tmp_path, keep=2)
    assert len(list(target.iterdir())) == 2 and not (target / "20200101T000000Z").exists()


def test_backup_sqlite_also_copies_the_record_tables(tmp_path):
    """复核 R-d4：填报的记录表 records/**/*.db（原地追加写的 SQLite）也用备份接口拷进同一份备份，相对路径照旧。"""
    sqlite3.connect(tmp_path / "agentlab.db").close()
    (tmp_path / "records" / "nested").mkdir(parents=True)
    rec = sqlite3.connect(tmp_path / "records" / "sales.v1.db")
    rec.execute("PRAGMA journal_mode=WAL")
    rec.execute("CREATE TABLE records (v TEXT)")
    rec.execute("INSERT INTO records VALUES ('还在 WAL 里的一行')")
    rec.commit()
    sqlite3.connect(tmp_path / "records" / "nested" / "visits.v2.db").close()
    (tmp_path / "records" / "notes.txt").write_text("不是库，不拷")
    try:
        target = ops.backup_sqlite(tmp_path, keep=5)
    finally:
        rec.close()
    copied = sorted(str(p.relative_to(target)) for p in target.rglob("*") if p.is_file())
    assert copied == ["agentlab.db", "records/nested/visits.v2.db", "records/sales.v1.db"]
    db = sqlite3.connect(target / "records" / "sales.v1.db")
    assert db.execute("SELECT v FROM records").fetchall() == [("还在 WAL 里的一行",)]
    db.close()
    for folder in (target / "records", target / "records" / "nested"):
        assert _mode(folder) == 0o700
    assert _mode(target / "records" / "sales.v1.db") == 0o600


def test_record_tables_alone_are_left_to_the_whole_directory_backup(tmp_path):
    """库在 PostgreSQL（没有 agentlab.db、checkpoints.db）时不做这份备份：记录表跟着整个数据目录备份。"""
    (tmp_path / "records").mkdir()
    sqlite3.connect(tmp_path / "records" / "sales.v1.db").close()
    assert ops.backup_sqlite(tmp_path, keep=5) is None


def test_backup_dir_creates_and_tightens_every_level(tmp_path, capsys):
    (tmp_path / "backups").mkdir(mode=0o755)
    (tmp_path / "backups").chmod(0o755)
    assert ops.main(["backup-dir", "--data", str(tmp_path), "postgres"]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "backups" / "postgres")
    assert _mode(tmp_path / "backups") == 0o700, "已有的收紧"
    assert _mode(tmp_path / "backups" / "postgres") == 0o700


def test_backup_sqlite_without_databases_does_nothing(tmp_path, capsys):
    assert ops.main(["backup-sqlite", "--data", str(tmp_path)]) == 0
    assert "没有 SQLite 库" in capsys.readouterr().out
    assert not (tmp_path / "backups").exists()
