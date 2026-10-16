"""第 7 阶段：掉线提醒、每周备份与恢复。"""
import os
import time

from sidecar import backup, db
from sidecar.cards import Cards
from sidecar.ledger import Ledger
from sidecar.monitor import ALERT_AFTER, Monitor
from sidecar.onebot import OneBot


class FakeWS:
    pass


def make_monitor():
    ob = OneBot("ws://x", "", None)
    sent = []
    return ob, Monitor(ob, notify=lambda title, text: sent.append(title)), sent


def test_alert_after_problem_persists():
    ob, mon, sent = make_monitor()
    ob.ws = FakeWS()
    ob._dispatch('{"post_type":"meta_event","meta_event_type":"heartbeat","status":{"online":true,"good":true},"interval":30000}')
    mon.check()
    assert mon.problem is None and sent == []
    ob._dispatch('{"post_type":"meta_event","meta_event_type":"heartbeat","status":{"online":false,"good":false},"interval":30000}')
    t = time.time()
    mon.check(t)
    assert "掉线" in mon.problem and sent == []  # 刚出问题先不提醒，可能只是重连
    mon.check(t + ALERT_AFTER)
    mon.check(t + ALERT_AFTER + 30)
    assert sent == ["骰娘掉线了"]  # 只提醒一次
    ob._dispatch('{"post_type":"meta_event","meta_event_type":"heartbeat","status":{"online":true,"good":true},"interval":30000}')
    ob.last_heartbeat = t + ALERT_AFTER + 60  # 心跳在恢复时刚到
    mon.check(t + ALERT_AFTER + 60)
    assert sent == ["骰娘掉线了", "骰娘已恢复"] and mon.problem is None


def test_problem_kinds():
    ob = OneBot("ws://x", "", None)
    assert "连不上" in ob.problem()
    ob.ws = FakeWS()
    assert ob.problem() is None
    ob.last_heartbeat, ob.heartbeat_interval = time.time() - 200, 30
    assert "没有心跳" in ob.problem()


def test_short_blip_not_alerted():
    ob, mon, sent = make_monitor()
    t = time.time()
    mon.check(t)  # 没连上
    ob.ws = FakeWS()
    mon.check(t + 60)
    mon.check(t + ALERT_AFTER + 60)
    assert sent == []


def test_backup_and_restore_drill(tmp_path):
    data, dest, seal = tmp_path / "data", tmp_path / "backup", tmp_path / "sealdice-backups"
    conn = db.connect(data / "sidecar.db")
    Ledger(conn).add(10001, "巧克力", "manual", "test", 2)
    cards = Cards(conn, data / "cards")
    cards.save(10001, "调查员A", b"\x89PNG one")
    seal.mkdir()
    (seal / "bak_old.zip").write_bytes(b"old")
    os.utime(seal / "bak_old.zip", (1, 1))
    (seal / "bak_new.zip").write_bytes(b"new")
    cfg = backup.BackupConfig(dest, seal, keep=2)

    target = backup.run_backup(data, cfg)
    assert target.exists() and backup.last_backup(dest)
    assert len(list((dest / "cards").rglob("*.png"))) == 1
    assert [p.name for p in (dest / "sealdice").iterdir()] == ["bak_new.zip"]

    # 第二次只补新图，旧图不重复复制
    cards.save(10001, "调查员B", b"\x89PNG two")
    first = next((dest / "cards").rglob("*调查员A*.png"))
    mtime = first.stat().st_mtime_ns
    backup.run_backup(data, cfg)
    assert len(list((dest / "cards").rglob("*.png"))) == 2 and first.stat().st_mtime_ns == mtime

    # 恢复演练：删掉数据库和图片，再从备份恢复
    conn.close()
    for p in data.glob("sidecar.db*"):
        p.unlink()
    for p in (data / "cards").rglob("*.png"):
        p.unlink()
    backup.restore(target, data)
    conn = db.connect(data / "sidecar.db")
    assert [p.entry.split("（")[0] for p in Ledger(conn).people()] == ["巧克力"]
    restored = Cards(conn, data / "cards")
    assert [c["name"] for c in restored.current(10001)] == ["调查员A", "调查员B"]
    assert all(restored.file(c).read_bytes().startswith(b"\x89PNG") for c in restored.current(10001))


def test_backup_keeps_latest_n(tmp_path):
    data, dest = tmp_path / "data", tmp_path / "backup"
    db.connect(data / "sidecar.db").close()
    (dest / "db").mkdir(parents=True)
    for d in ("2026-01-01", "2026-01-08", "2026-01-15"):
        (dest / "db" / f"sidecar-{d}.db").write_bytes(b"")
    backup.run_backup(data, backup.BackupConfig(dest, None, keep=2))
    assert len(list((dest / "db").glob("sidecar-*.db"))) == 2
