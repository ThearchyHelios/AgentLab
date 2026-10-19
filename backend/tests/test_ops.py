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
    # 只留最近 keep 份
    for name in ("20200101T000000Z", "20200102T000000Z"):
        (target / name).mkdir()
    ops.backup_sqlite(tmp_path, keep=2)
    assert len(list(target.iterdir())) == 2 and not (target / "20200101T000000Z").exists()


def test_backup_sqlite_without_databases_does_nothing(tmp_path, capsys):
    assert ops.main(["backup-sqlite", "--data", str(tmp_path)]) == 0
    assert "没有 SQLite 库" in capsys.readouterr().out
    assert not (tmp_path / "backups").exists()
