"""文案模板：目录、校验、渲染和存储。
所有对群友可见的文字都从这里取，代码里不写死文案。新增文案时在 CATALOG 里加一条即可，
后台页面会自动列出来。"""
import json
import random
import sqlite3
import string
import time
from dataclasses import dataclass, field

from .db import log_op


@dataclass(frozen=True)
class TemplateDef:
    key: str
    group: str
    title: str
    description: str
    variables: dict[str, str]  # 变量名 -> 说明
    default: list[str]
    per_group: bool = False  # 是否允许按群单独设置
    samples: dict[str, str] = field(default_factory=dict)  # 预览用示例值


# 变量的预览示例值，模板里没单独指定时用这里的
SAMPLES = {
    "at": "@示例群友",
    "nickname": "示例群友",
    "group_name": "未可知的幻梦境",
    "name": "Flame",
    "left": "4",
    "next": "巧克力",
    "deadline": "12/17",
    "entry": "Flame（11/3，11/30）",
    "text": "草",
    "count": "4",
    "reason": "倒数第三位复读",
}

GROUPS = ["入群与拉群", "俄罗斯轮盘", "复读链", "团账本", "人物卡"]

CATALOG = [
    TemplateDef(
        "welcome.default", "入群与拉群", "入群欢迎", "新成员入群时发送。可以为某个群单独设置，没设置的群用默认文案。",
        {"at": "@ 新成员", "nickname": "新成员昵称", "group_name": "群名"},
        ["欢迎 {at} 来到{group_name}。"], per_group=True,
    ),
    TemplateDef(
        "invite.reject", "入群与拉群", "拉群拒绝", "骰娘被拉进没有大群成员的群时，退群前发送。",
        {"group_name": "群名"},
        ["这个群里没有找到幻梦境的群友，骰娘先告辞了。"],
    ),
    TemplateDef(
        "invite.hello", "入群与拉群", "进群问候", "骰娘被拉进群且校验通过时发送。留空则不发送。",
        {"group_name": "群名"},
        [""],
    ),
    TemplateDef(
        "rr.click", "俄罗斯轮盘", "空枪", "开枪没有中弹时发送。",
        {"name": "开枪者", "left": "剩余弹仓格数", "next": "下一位开枪者"},
        ["{name} 扣动扳机……咔哒。空弹，存活确认！剩余弹仓 {left} 格，下一位：{next}"],
    ),
    TemplateDef(
        "rr.bang", "俄罗斯轮盘", "中弹", "开枪中弹时在本群发送。",
        {"name": "中弹者", "deadline": "死线（月/日）", "entry": "此人在账本中的整行"},
        ["{name} 扣动扳机……砰！！{name} 中弹倒地！记一团，死线 {deadline}。"],
    ),
    TemplateDef(
        "rr.announce", "俄罗斯轮盘", "轮盘记团公告", "轮盘中弹后发到大群的公告。",
        {"name": "中弹者", "deadline": "死线（月/日）", "entry": "此人在账本中的整行"},
        ["【记团公告】{name} 在俄罗斯轮盘中中弹，记一团，死线 {deadline}。"],
    ),
    TemplateDef(
        "repeat.announce", "复读链", "复读链记团公告", "复读链被打断、结算记团时发送。",
        {"text": "复读的内容", "count": "链长", "name": "被记团的人", "reason": "记团原因",
         "deadline": "死线（月/日）", "entry": "此人在账本中的整行"},
        ["【记团公告】「{text}」复读 {count} 次后被打断。{name}：{reason}，记一团，死线 {deadline}。"],
    ),
    TemplateDef(
        "ledger.title", "团账本", "名单标题与说明", "`.团` 输出的开头部分。",
        {},
        ["【记团名单】\n榜上有名的须在死线日期（月/日）前开团，不然会被赠送飞机票哟。消团券获得者在全部名单链接中也有记录。"],
    ),
    TemplateDef(
        "ledger.link", "团账本", "全部名单链接", "`.团` 输出中「全部名单」下面的链接。",
        {},
        ["https://docs.qq.com/sheet/DQVFWdGlzdnF5VXFK"],
    ),
    TemplateDef(
        "ledger.removed", "团账本", "消团结果", "`.团 消` 成功后发送。",
        {"name": "被消团的人", "deadline": "被消去那一团的死线", "entry": "此人在账本中的整行（消团后）"},
        ["{name} 消去一团（死线 {deadline}）。"],
    ),
    TemplateDef(
        "card.saved", "人物卡", "存卡成功", "私聊存卡成功后发送。",
        {"name": "卡名"},
        ["已存档人物卡「{name}」。"],
    ),
    TemplateDef(
        "card.private_only", "人物卡", "请私聊使用", "在群里使用人物卡指令时发送。",
        {},
        ["人物卡指令请私聊骰娘使用。"],
    ),
]
BY_KEY = {t.key: t for t in CATALOG}

_formatter = string.Formatter()


class TemplateError(ValueError):
    pass


def parse(text: str) -> list[tuple[str, str | None]]:
    """拆成 (字面文字, 变量名或 None) 的列表；花括号写错时抛 TemplateError。
    想输出花括号本身时写成 {{ 和 }}。"""
    try:
        parts = list(_formatter.parse(text))
    except ValueError:
        raise TemplateError("花括号没有配对。想输出花括号本身，请写成 {{ 或 }}") from None
    out = []
    for literal, name, spec, conv in parts:
        if name is not None:
            if spec or conv:
                raise TemplateError(f"变量 {{{name}{'!' + conv if conv else ''}{':' + spec if spec else ''}}} 的写法不支持，只能写成 {{变量名}}")
            if not name:
                raise TemplateError("出现了空的 {}，请写上变量名")
        out.append((literal, name))
    return out


def validate(defn: TemplateDef, variants: list[str]) -> None:
    for i, text in enumerate(variants, 1):
        where = f"第 {i} 条" if len(variants) > 1 else ""
        try:
            parts = parse(text)
        except TemplateError as e:
            raise TemplateError(f"{where}{e}") from None
        for _, name in parts:
            if name is not None and name not in defn.variables:
                allowed = "、".join("{" + v + "}" for v in defn.variables) or "无"
                raise TemplateError(f"{where}用到了未定义的变量 {{{name}}}。这条文案可用的变量：{allowed}")


def to_segments(text: str, values: dict) -> list[dict]:
    """渲染成 OneBot 消息段。变量值可以是字符串，也可以是消息段（比如 @）。"""
    segs: list[dict] = []

    def add_text(s: str):
        if not s:
            return
        if segs and segs[-1]["type"] == "text":
            segs[-1]["data"]["text"] += s
        else:
            segs.append({"type": "text", "data": {"text": s}})

    for literal, name in parse(text):
        add_text(literal)
        if name is None:
            continue
        value = values[name]
        if isinstance(value, dict) and "type" in value:
            segs.append(value)
        else:
            add_text(str(value))
    return segs


def to_plain(segs: list[dict]) -> str:
    out = []
    for s in segs:
        if s["type"] == "text":
            out.append(s["data"]["text"])
        elif s["type"] == "at":
            out.append(s["data"].get("name") or f"@{s['data']['qq']}")
        else:
            out.append(f"[{s['type']}]")
    return "".join(out)


def at(user_id: int, name: str = "") -> dict:
    return {"type": "at", "data": {"qq": str(user_id), "name": name}}


def clean_variants(variants: list[str]) -> list[str]:
    """去掉空白变体；全部为空时保留一条空串，表示不发送。"""
    kept = [v.replace("\r\n", "\n").strip("\n") for v in variants if v.strip()]
    return kept or [""]


class Templates:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def variants(self, key: str, scope: str = "") -> list[str]:
        """取某条文案的变体。按群设置的文案先找该群，找不到再用默认。"""
        defn = BY_KEY[key]
        scopes = [scope, ""] if scope and defn.per_group else [""]
        for s in scopes:
            row = self.conn.execute("SELECT variants FROM templates WHERE key = ? AND scope = ?", (key, s)).fetchone()
            if row:
                return json.loads(row["variants"])
        return list(defn.default)

    def is_customized(self, key: str, scope: str = "") -> bool:
        return self.conn.execute("SELECT 1 FROM templates WHERE key = ? AND scope = ?", (key, scope)).fetchone() is not None

    def scopes(self, key: str) -> list[str]:
        rows = self.conn.execute("SELECT scope FROM templates WHERE key = ? AND scope != '' ORDER BY scope", (key,))
        return [r["scope"] for r in rows]

    def render(self, key: str, scope: str = "", **values) -> list[dict] | None:
        """随机选一条变体渲染；文案为空时返回 None，表示不发送。"""
        choices = [v for v in self.variants(key, scope) if v.strip()]
        if not choices:
            return None
        return to_segments(random.choice(choices), values)

    def render_text(self, key: str, scope: str = "", **values) -> str | None:
        segs = self.render(key, scope, **values)
        return None if segs is None else to_plain(segs)

    def save(self, key: str, variants: list[str], operator: str, scope: str = "") -> None:
        defn = BY_KEY[key]
        if scope and not defn.per_group:
            raise TemplateError("这条文案不支持按群设置")
        variants = clean_variants(variants)
        validate(defn, variants)
        before = self.variants(key, scope)
        self.conn.execute("BEGIN")
        self.conn.execute(
            "INSERT INTO templates (key, scope, variants, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key, scope) DO UPDATE SET variants = excluded.variants, updated_at = excluded.updated_at",
            (key, scope, json.dumps(variants, ensure_ascii=False), int(time.time())),
        )
        log_op(self.conn, operator, "template.save", {"key": key, "scope": scope, "variants": before},
               {"key": key, "scope": scope, "variants": variants})
        self.conn.execute("COMMIT")

    def reset(self, key: str, operator: str, scope: str = "") -> None:
        """恢复默认。对按群设置的文案，就是删掉该群的单独设置。"""
        before = self.variants(key, scope)
        self.conn.execute("BEGIN")
        self.conn.execute("DELETE FROM templates WHERE key = ? AND scope = ?", (key, scope))
        log_op(self.conn, operator, "template.reset", {"key": key, "scope": scope, "variants": before}, None)
        self.conn.execute("COMMIT")


def preview(defn: TemplateDef, text: str) -> str:
    values = {name: defn.samples.get(name, SAMPLES.get(name, name)) for name in defn.variables}
    return to_plain(to_segments(text, values))
