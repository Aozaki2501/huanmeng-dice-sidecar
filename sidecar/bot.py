"""消息处理：把 OneBot 事件分给各功能。海豹也会收到同样的消息，这里只响应自己的指令。"""
import logging
import sqlite3

from .onebot import OneBot
from .settings import Settings
from .templates import Templates

log = logging.getLogger("bot")

PREFIXES = (".", "。")
CARD_COMMANDS = ("存卡", "取卡", "删卡")


def plain_text(message) -> str | None:
    """纯文字消息返回文字，含图片等其他内容时返回 None。"""
    if isinstance(message, str):
        return message
    parts = []
    for seg in message:
        if seg.get("type") != "text":
            return None
        parts.append(seg["data"].get("text", ""))
    return "".join(parts)


def parse_command(text: str, names: tuple[str, ...]) -> tuple[str, str] | None:
    """识别 `.指令 参数`，指令和参数之间可以不加空格。返回 (指令, 参数)。"""
    text = text.strip()
    if not text.startswith(PREFIXES):
        return None
    body = text[1:].lstrip()
    for name in sorted(names, key=len, reverse=True):
        if body.startswith(name):
            return name, body[len(name):].strip()
    return None


class Bot:
    def __init__(self, conn: sqlite3.Connection, onebot_url: str, onebot_token: str):
        self.conn = conn
        self.settings = Settings(conn)
        self.templates = Templates(conn)
        self.onebot = OneBot(onebot_url, onebot_token, self.handle_event)

    async def handle_event(self, event: dict) -> None:
        if event.get("post_type") == "message":
            user_id = int(event.get("user_id", 0))
            if user_id == self.onebot.self_id or self.settings.is_ignored(user_id):
                return
            if event.get("message_type") == "group":
                await self.on_group_message(event)
            elif event.get("message_type") == "private":
                await self.on_private_message(event)

    async def on_group_message(self, event: dict) -> None:
        text = plain_text(event.get("message", []))
        if text is None:
            return
        cmd = parse_command(text, CARD_COMMANDS)
        if cmd:
            await self.reply_group(event["group_id"], "card.private_only")

    async def on_private_message(self, event: dict) -> None:
        pass  # 人物卡存取在第 6 阶段实现

    async def reply_group(self, group_id: int, key: str, **values) -> None:
        segs = self.templates.render(key, str(group_id), **values)
        if segs:
            await self.onebot.send_group(group_id, segs)
