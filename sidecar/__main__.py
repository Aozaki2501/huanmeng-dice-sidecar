"""入口。
    uv run python -m sidecar               按 config.toml 启动
    uv run python -m sidecar hash-password 生成管理员密码哈希，填进 config.toml
    uv run python -m sidecar backup        立即备份一次（需要在 config.toml 里设置备份目录）
    uv run python -m sidecar restore <备份目录里的 db/sidecar-日期.db>   用备份恢复，先停掉服务
"""
import argparse
import asyncio
import getpass
import logging
import socket
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import uvicorn

from . import backup, db
from .bot import Bot
from .config import hash_password, load_config
from .web import create_app


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    file = TimedRotatingFileHandler(log_dir / "sidecar.log", when="midnight", backupCount=14, encoding="utf-8")
    console = logging.StreamHandler(sys.stdout)
    for h in (file, console):
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[file, console])


def bind(hosts: list[str], port: int) -> list[socket.socket]:
    socks = []
    for host in hosts:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        s = socket.socket(family, socket.SOCK_STREAM)
        s.bind((host, port))
        s.listen(128)
        s.setblocking(False)
        socks.append(s)
    return socks


async def serve(config_path: Path) -> None:
    cfg = load_config(config_path)
    setup_logging(cfg.data_dir / "logs")
    conn = db.connect(cfg.db_path)
    bot = Bot(conn, cfg.onebot_url, cfg.onebot_token, cfg.data_dir)
    app = create_app(cfg, bot)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
    logging.getLogger("sidecar").info("后台地址：%s", "、".join(f"http://{h}:{cfg.web_port}" for h in cfg.web_hosts))
    if not cfg.desktop_alert:
        bot.monitor.notify = lambda title, text: None
    tasks = [asyncio.create_task(bot.onebot.run()), asyncio.create_task(bot.monitor.run())]
    if cfg.backup_dir:
        tasks.append(asyncio.create_task(backup.weekly(cfg.data_dir, backup_config(cfg))))
    else:
        logging.getLogger("sidecar").warning("config.toml 没有设置备份目录，不会自动备份")
    try:
        await server.serve(sockets=bind(cfg.web_hosts, cfg.web_port))
    finally:
        for t in tasks:
            t.cancel()


def backup_config(cfg) -> backup.BackupConfig:
    return backup.BackupConfig(cfg.backup_dir, cfg.sealdice_backups, cfg.backup_keep)


def main() -> None:
    parser = argparse.ArgumentParser(prog="sidecar")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "hash-password", "backup", "restore"])
    parser.add_argument("file", nargs="?", type=Path, help="restore 时要恢复的备份数据库文件")
    parser.add_argument("--config", default="config.toml", type=Path)
    args = parser.parse_args()
    if args.command == "hash-password":
        pw = getpass.getpass("新密码：")
        if pw != getpass.getpass("再输一次：") or not pw:
            sys.exit("两次输入不一致或为空")
        print(hash_password(pw))
        return
    if args.command in ("backup", "restore"):
        cfg = load_config(args.config)
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        if args.command == "backup":
            if not cfg.backup_dir:
                sys.exit("config.toml 的 [backup] dir 还没填")
            print(f"已备份到 {backup.run_backup(cfg.data_dir, backup_config(cfg))}")
        else:
            if not args.file or not args.file.is_file():
                sys.exit("用法：uv run python -m sidecar restore <备份目录里的 db/sidecar-日期.db>")
            if input("恢复前请先停掉旁路服务。确认恢复？输入 yes：").strip() != "yes":
                sys.exit("已取消")
            backup.restore(args.file, cfg.data_dir)
        return
    try:
        asyncio.run(serve(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
