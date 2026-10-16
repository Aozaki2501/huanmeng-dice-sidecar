"""入口。
    uv run python -m sidecar               按 config.toml 启动
    uv run python -m sidecar hash-password 生成管理员密码哈希，填进 config.toml
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

from . import db
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
    bot = Bot(conn, cfg.onebot_url, cfg.onebot_token)
    app = create_app(cfg, bot)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
    logging.getLogger("sidecar").info("后台地址：%s", "、".join(f"http://{h}:{cfg.web_port}" for h in cfg.web_hosts))
    onebot_task = asyncio.create_task(bot.onebot.run())
    try:
        await server.serve(sockets=bind(cfg.web_hosts, cfg.web_port))
    finally:
        onebot_task.cancel()


def main() -> None:
    parser = argparse.ArgumentParser(prog="sidecar")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "hash-password"])
    parser.add_argument("--config", default="config.toml", type=Path)
    args = parser.parse_args()
    if args.command == "hash-password":
        pw = getpass.getpass("新密码：")
        if pw != getpass.getpass("再输一次：") or not pw:
            sys.exit("两次输入不一致或为空")
        print(hash_password(pw))
        return
    asyncio.run(serve(args.config))


if __name__ == "__main__":
    main()
