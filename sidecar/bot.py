"""消息处理：把 OneBot 事件分给各功能。海豹也会收到同样的消息，这里只响应自己的指令。"""
import asyncio
import html
import logging
import re
import sqlite3

from . import ledger as ledger_mod
from .ledger import Ledger, md
from .onebot import ActionError, OneBot
from .repeat import RepeatTracker
from .roulette import Outcome, Roulette
from .settings import Settings
from .templates import Templates

log = logging.getLogger("bot")

PREFIXES = (".", "。")
# 海豹的指令前缀；以这些开头、后面紧跟文字的消息算作指令，不能开链续链
_COMMAND_LIKE = re.compile(r"^[.。!/]\s*[A-Za-z一-鿿]")
CARD_COMMANDS = ("存卡", "取卡", "删卡")
RR_COMMANDS = ("轮盘", "开枪")  # 不用 .rr：海豹会把它当成 .r 掷骰
LEDGER_COMMANDS = ("团", "jt")


def plain_text(message) -> str | None:
    """纯文字消息返回去掉首尾空白的文字，含图片等其他内容或为空时返回 None。"""
    if isinstance(message, str):
        if "[CQ:" in message:
            return None
        return message.strip() or None
    parts = []
    for seg in message:
        if seg.get("type") != "text":
            return None
        parts.append(seg["data"].get("text", ""))
    return "".join(parts).strip() or None


def parse_command(text: str, names: tuple[str, ...]) -> tuple[str, str] | None:
    """识别 `.指令 参数`，中文指令和参数之间可以不加空格。返回 (指令, 参数)。"""
    text = text.strip()
    if not text.startswith(PREFIXES):
        return None
    body = text[1:].lstrip()
    for name in sorted(names, key=len, reverse=True):
        if body.startswith(name):
            rest = body[len(name):]
            if name.isascii() and rest[:1].isascii() and rest[:1].isalnum():
                continue  # `.jta` 不是 `.jt`
            return name, rest.strip()
    return None


def sender_name(event: dict) -> str:
    sender = event.get("sender") or {}
    return sender.get("card") or sender.get("nickname") or str(event.get("user_id"))


class Bot:
    def __init__(self, conn: sqlite3.Connection, onebot_url: str, onebot_token: str):
        self.conn = conn
        self.settings = Settings(conn)
        self.templates = Templates(conn)
        self.ledger = Ledger(conn)
        self.repeat = RepeatTracker()
        self.roulette = Roulette()
        self.onebot = OneBot(onebot_url, onebot_token, self.handle_event)
        self._notice_lock = asyncio.Lock()

    # ---------- 分发 ----------
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
        group_id, user_id = int(event["group_id"]), int(event["user_id"])
        text = plain_text(event.get("message", []))

        if group_id in self.settings.repeat_groups:
            chain_text = None if text is None or _COMMAND_LIKE.match(text) else text
            settled = self.repeat.feed(group_id, user_id, chain_text)
            if settled:
                await self.settle_repeat(group_id, settled)

        if text is None:
            return
        reply = lambda key, **v: self.send(("group", group_id), key, **v)
        if cmd := parse_command(text, CARD_COMMANDS):
            await reply("card.private_only")
        elif cmd := parse_command(text, RR_COMMANDS):
            await self.on_roulette(event, group_id, *cmd)
        elif cmd := parse_command(text, LEDGER_COMMANDS):
            await self.on_ledger(event, ("group", group_id), cmd[1])

    async def on_private_message(self, event: dict) -> None:
        text = plain_text(event.get("message", []))
        if text is None:
            return
        if cmd := parse_command(text, LEDGER_COMMANDS):
            await self.on_ledger(event, ("private", int(event["user_id"])), cmd[1])
        # 人物卡存取在第 6 阶段实现

    # ---------- 发送 ----------
    async def send(self, target: tuple[str, int], key: str, **values) -> None:
        kind, target_id = target
        segs = self.templates.render(key, str(target_id) if kind == "group" else "", **values)
        if segs:
            await self.deliver(target, segs, key)

    async def deliver(self, target: tuple[str, int], segs: list[dict], label: str) -> None:
        kind, target_id = target
        try:
            if kind == "group":
                await self.onebot.send_group(target_id, segs)
            else:
                await self.onebot.send_private(target_id, segs)
        except ActionError as e:
            log.error("发送 %s 到 %s %s 失败：%s", label, kind, target_id, e)

    async def record(self, group_id: int, qq: int, name: str, source: str, reply_key: str,
                     announce_key: str, **values) -> None:
        """记一团：在事发群回复，再向大群发公告（事发群就是大群时只发一次）。"""
        person, deadline = self.ledger.add(qq, name, source, f"bot:{source}", self.settings.get("deadline_months"))
        values.update(name=person.name, deadline=md(deadline), entry=person.entry)
        log.info("记团：%s（%s）来源 %s，群 %s", person.name, qq, source, group_id)
        await self.send(("group", group_id), reply_key, **values)
        main = self.settings.get("main_group")
        if main and main != group_id:
            await self.send(("group", main), announce_key, **values)
        await self.refresh_notice()

    async def is_main_group_admin(self, user_id: int) -> bool:
        if user_id in self.settings.get("ledger_admins"):
            return True
        main = self.settings.get("main_group")
        if not main:
            return False
        try:
            info = await self.onebot.call("get_group_member_info", group_id=main, user_id=user_id, no_cache=True)
        except ActionError:
            return False  # 不在大群里
        return (info or {}).get("role") in ("owner", "admin")

    # ---------- 复读链 ----------
    async def settle_repeat(self, group_id: int, s) -> None:
        known = self.ledger.person(s.target)
        name = known.name if known else await self.member_name(group_id, s.target)
        reason = self.templates.render_text(f"repeat.reason_{s.reason}") or ""
        preview = s.text if len(s.text) <= 20 else s.text[:20] + "…"
        await self.record(group_id, s.target, name, "repeat", "repeat.announce", "repeat.announce",
                          text=preview, count=s.count, reason=reason)

    async def member_name(self, group_id: int, user_id: int) -> str:
        try:
            info = await self.onebot.call("get_group_member_info", group_id=group_id, user_id=user_id)
            return info.get("card") or info.get("nickname") or str(user_id)
        except ActionError:
            return str(user_id)

    # ---------- 俄罗斯轮盘 ----------
    async def on_roulette(self, event: dict, group_id: int, cmd: str, args: str) -> None:
        uid, name = int(event["user_id"]), sender_name(event)
        timeout = self.settings.get("rr_timeout_minutes")
        sub = "shoot" if cmd == "开枪" else (args.split() or [""])[0]
        rr = self.roulette
        if sub == "help":
            out = Outcome("rr.help")
        elif sub in ("join", "报名"):
            out = rr.join(group_id, uid, name, timeout)
        elif sub in ("quit", "退出"):
            out = rr.quit(group_id, uid, name, timeout)
        elif sub in ("roll", "掷点"):
            out = rr.roll(group_id, uid, timeout)
        elif sub in ("shoot", "开枪"):
            out = rr.shoot(group_id, uid, timeout)
        elif sub in ("off", "结束"):
            is_admin = (event.get("sender") or {}).get("role") in ("owner", "admin") \
                or uid in self.settings.get("ledger_admins")
            out = rr.off(group_id, is_admin, timeout)
        elif sub in ("list", "名单"):
            await self.send_ledger_list(("group", group_id))
            return
        elif sub == "":
            out = rr.status(group_id, timeout)
        else:
            out = Outcome("rr.help")

        if out.key == "rr.help":
            await self.send(("group", group_id), "rr.help", timeout=timeout)
        elif out.key == "rr.status_none":
            help_text = self.templates.render_text("rr.help", timeout=timeout) or ""
            await self.send(("group", group_id), "rr.status_none", help=help_text)
        elif out.shot:
            await self.record(group_id, out.shot.id, out.shot.name, "rr", "rr.bang", "rr.announce")
        else:
            await self.send(("group", group_id), out.key, **out.values)

    # ---------- 团账本 ----------
    def ledger_list_text(self) -> str:
        lines = [self.templates.render_text("ledger.title") or ""]
        rule = "————————————"
        lines.append(rule)
        link = self.templates.render_text("ledger.link")
        if link:
            lines += ["全部名单：", link, rule]
        people = self.ledger.people()
        lines += [p.entry for p in people] if people else [self.templates.render_text("ledger.empty") or ""]
        return "\n".join(line for line in lines if line)

    async def send_ledger_list(self, target) -> None:
        await self.deliver(target, [{"type": "text", "data": {"text": self.ledger_list_text()}}], "记团名单")

    async def refresh_notice(self) -> None:
        """账本变动后把最新名单发成大群群公告，再删掉骰娘之前发的名单公告。
        接口不能编辑公告，只能替换；按开头一行与名单标题相同来认旧公告，不动别人发的公告。"""
        main = self.settings.get("main_group")
        if not main:
            return
        async with self._notice_lock:
            try:
                marks = {v.split("\n")[0] for v in self.templates.variants("ledger.title") if v.strip()}
                notices = await self.onebot.call("_get_group_notice", group_id=main) or []
                old = [n["notice_id"] for n in notices if n.get("sender_id") == self.onebot.self_id
                       and html.unescape((n.get("message") or {}).get("text", "")).split("\n")[0] in marks]
                await self.onebot.call("_send_group_notice", group_id=main, content=self.ledger_list_text(),
                                       is_show_edit_card=0, tip_window_type=1, confirm_required=0)
                for notice_id in old:
                    await self.onebot.call("_del_group_notice", group_id=main, notice_id=notice_id)
            except Exception as e:  # 公告失败不影响记团本身
                log.error("更新大群名单公告失败：%s", e)

    async def on_ledger(self, event: dict, target, args: str) -> None:
        uid = int(event["user_id"])
        parts = args.split()
        sub = parts[0] if parts else ""
        if sub in ("", "list", "名单"):
            await self.send_ledger_list(target)
        elif sub in ("我", "me"):
            p = self.ledger.person(uid)
            if p and p.deadlines:
                await self.send(target, "ledger.mine", entry=p.entry, count=len(p.deadlines))
            else:
                await self.send(target, "ledger.mine_none")
        elif sub in ("消", "del"):
            if not await self.is_main_group_admin(uid):
                await self.send(target, "ledger.denied")
                return
            query = args[len(sub):].strip()
            found = self.ledger.find(query) if query else []
            if len(found) > 1:
                candidates = "\n".join(f"{p.entry}  QQ {p.qq}" for p in found)
                await self.send(target, "ledger.ambiguous", query=query, candidates=candidates)
            elif not found:
                await self.send(target, "ledger.not_found", query=query)
            else:
                person, deadline = self.ledger.remove_earliest(found[0].qq, str(uid))
                if person.deadlines:
                    await self.send(target, "ledger.removed", name=person.name, deadline=md(deadline), entry=person.entry)
                else:
                    await self.send(target, "ledger.removed_all", name=person.name, deadline=md(deadline))
                await self.refresh_notice()
        elif sub in ("加", "add"):
            if not await self.is_main_group_admin(uid):
                await self.send(target, "ledger.add_denied")
                return
            try:
                qq, name, raw = parts[1], parts[2], parts[3]
                if not re.fullmatch(r"[1-9]\d{4,11}", qq):
                    raise ValueError
                m, d = (int(x) for x in re.split(r"[/.月]", raw.rstrip("日")))
                deadline = ledger_mod.nearest_year(m, d, ledger_mod.today())
            except (IndexError, ValueError):
                await self.send(target, "ledger.add_usage")
                return
            person, _ = self.ledger.add(int(qq), name, "manual", str(uid), self.settings.get("deadline_months"),
                                        deadline=deadline)
            await self.send(target, "ledger.added", entry=person.entry)
            await self.refresh_notice()
        else:
            await self.send(target, "ledger.help")
