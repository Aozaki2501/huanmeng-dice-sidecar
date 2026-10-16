"""每周备份：数据库用 SQLite 的在线备份接口复制一份带日期的文件；人物卡图片只补复制新增的，
不会每次整份复制；海豹的最新自动备份包也顺手复制一份。备份目录建议放在网盘同步文件夹里。

备份目录结构：
    db/sidecar-YYYY-MM-DD.db      每周一份，保留最近 keep 份
    cards/...                     与 data/cards/ 相同的结构，只增不删
    sealdice/<海豹备份包>.zip     保留最近 keep 份
"""
import asyncio
import logging
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .ledger import today

log = logging.getLogger("backup")

INTERVAL_DAYS = 7


@dataclass
class BackupConfig:
    dir: Path | None  # 为空表示不备份
    sealdice_backups: Path | None  # 海豹的 backups 目录，为空表示不复制海豹备份
    keep: int = 8


def last_backup(dest: Path) -> float | None:
    files = sorted((dest / "db").glob("sidecar-*.db"))
    return files[-1].stat().st_mtime if files else None


def run_backup(data_dir: Path, cfg: BackupConfig) -> Path:
    dest = cfg.dir
    (dest / "db").mkdir(parents=True, exist_ok=True)
    target = dest / "db" / f"sidecar-{today().isoformat()}.db"
    tmp = target.with_suffix(".tmp")
    src, out = sqlite3.connect(data_dir / "sidecar.db"), sqlite3.connect(tmp)  # 单独开连接，不和服务抢同一个
    try:
        src.backup(out)
    finally:
        src.close()
        out.close()
    tmp.replace(target)
    _prune(sorted((dest / "db").glob("sidecar-*.db")), cfg.keep)

    copied = 0
    src_cards = data_dir / "cards"
    for f in src_cards.rglob("*") if src_cards.exists() else []:
        to = dest / "cards" / f.relative_to(src_cards)
        if f.is_file() and not (to.exists() and to.stat().st_size == f.stat().st_size):
            to.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, to)
            copied += 1

    sealdice = "未设置"
    if cfg.sealdice_backups:
        zips = sorted(cfg.sealdice_backups.glob("*.zip"), key=lambda p: p.stat().st_mtime)
        if zips:
            (dest / "sealdice").mkdir(exist_ok=True)
            if not (dest / "sealdice" / zips[-1].name).exists():
                shutil.copy2(zips[-1], dest / "sealdice" / zips[-1].name)
            _prune(sorted((dest / "sealdice").glob("*.zip"), key=lambda p: p.stat().st_mtime), cfg.keep)
            sealdice = zips[-1].name
        else:
            sealdice = "海豹还没有生成备份包"
    log.info("备份完成：%s；新复制人物卡 %d 张；海豹：%s", target.name, copied, sealdice)
    return target


def _prune(files: list[Path], keep: int) -> None:
    for f in files[:-keep]:
        f.unlink()


async def weekly(data_dir: Path, cfg: BackupConfig) -> None:
    """每小时看一次，距离上次备份满 7 天就备份。电脑关机错过的，开机后会补上。"""
    while True:
        last = last_backup(cfg.dir)
        if last is None or time.time() - last >= INTERVAL_DAYS * 86400:
            try:
                await asyncio.to_thread(run_backup, data_dir, cfg)
            except Exception:
                log.exception("备份失败")
        await asyncio.sleep(3600)


def restore(backup_db: Path, data_dir: Path) -> None:
    """用备份恢复：先把现在的数据库改名留底，再放回备份的数据库，并补回备份里的人物卡图片。
    运行前必须先停掉旁路服务。"""
    data_dir.mkdir(parents=True, exist_ok=True)  # 换了新电脑时数据目录还不存在
    db = data_dir / "sidecar.db"
    if db.exists():
        keep = db.with_name(f"sidecar.before-restore-{time.strftime('%Y%m%d-%H%M%S')}.db")
        src = sqlite3.connect(db)
        out = sqlite3.connect(keep)
        try:
            src.backup(out)
        finally:
            src.close()
            out.close()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db) + suffix)
            if p.exists():
                p.unlink()
        print(f"原来的数据库已留底：{keep}")
    shutil.copy2(backup_db, db)
    cards = backup_db.parent.parent / "cards"
    n = 0
    for f in cards.rglob("*") if cards.exists() else []:
        to = data_dir / "cards" / f.relative_to(cards)
        if f.is_file() and not to.exists():
            to.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, to)
            n += 1
    print(f"已恢复数据库 {backup_db.name}，补回人物卡图片 {n} 张")
