"""运行期配置：在后台「配置」页修改，保存后立即生效。"""
import json
import re
import sqlite3
from dataclasses import dataclass

from .db import log_op


@dataclass(frozen=True)
class SettingDef:
    key: str
    title: str
    help: str
    kind: str  # 'qq' 单个号码 | 'qq_list' 号码列表 | 'int'
    default: object
    min: int = 0
    max: int = 0


SETTINGS = [
    SettingDef("main_group", "大群群号", "记团公告发到这个群，拉群校验和消团权限也以它为准。", "qq", None),
    SettingDef("card_announce_group", "存卡通知群", "私聊存卡成功后，向这个群发文字通知。默认管理群 779254872；可填大群群号，留空则关闭。", "qq", 779254872),
    SettingDef("repeat_groups", "复读检测群", "在这些群里检测复读链。留空表示只检测大群。", "qq_list", []),
    SettingDef("ignore_qq", "忽略 QQ", "各类机器人的 QQ 号。它们的消息完全忽略，拉群校验时也不算作大群成员。", "qq_list", []),
    SettingDef("ledger_admins", "记团管理员 QQ", "除大群群主和管理员外，额外允许消团的人。", "qq_list", []),
    SettingDef("invite_whitelist", "拉群白名单", "没有大群成员也允许骰娘留下的群。", "qq_list", []),
    SettingDef("deadline_months", "死线月数", "记团后多少个月内须开团。", "int", 2, 1, 24),
    SettingDef("rr_timeout_minutes", "轮盘超时分钟数", "开局后多久无人操作就作废。", "int", 15, 1, 1440),
]
BY_KEY = {s.key: s for s in SETTINGS}

_QQ = re.compile(r"^[1-9]\d{4,11}$")


def parse(defn: SettingDef, text: str):
    """把表单里的文本解析成值，格式不对时抛出 ValueError（信息直接展示给用户）。"""
    text = text.strip()
    if defn.kind == "qq":
        if not text:
            return None
        if not _QQ.match(text):
            raise ValueError(f"「{defn.title}」不是有效的号码：{text}")
        return int(text)
    if defn.kind == "qq_list":
        items = [x for x in re.split(r"[\s,，、;；]+", text) if x]
        bad = [x for x in items if not _QQ.match(x)]
        if bad:
            raise ValueError(f"「{defn.title}」里有无效的号码：{'、'.join(bad)}")
        return sorted({int(x) for x in items})
    if defn.kind == "int":
        if not re.fullmatch(r"\d+", text):
            raise ValueError(f"「{defn.title}」需要填整数")
        value = int(text)
        if not defn.min <= value <= defn.max:
            raise ValueError(f"「{defn.title}」需要在 {defn.min} 到 {defn.max} 之间")
        return value
    raise AssertionError(defn.kind)


def format_value(defn: SettingDef, value) -> str:
    if value is None:
        return ""
    if defn.kind == "qq_list":
        return "\n".join(str(x) for x in value)
    return str(value)


class Settings:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get(self, key: str):
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else BY_KEY[key].default

    def all(self) -> dict:
        return {s.key: self.get(s.key) for s in SETTINGS}

    def update(self, values: dict, operator: str) -> None:
        before = self.all()
        changed = {k: v for k, v in values.items() if before[k] != v}
        if not changed:
            return
        self.conn.execute("BEGIN")
        for key, value in changed.items():
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
        log_op(self.conn, operator, "settings.update", {k: before[k] for k in changed}, changed)
        self.conn.execute("COMMIT")

    # 常用的派生值
    @property
    def repeat_groups(self) -> list[int]:
        groups = self.get("repeat_groups")
        main = self.get("main_group")
        return groups or ([main] if main else [])

    def is_ignored(self, user_id: int) -> bool:
        return user_id in self.get("ignore_qq")
