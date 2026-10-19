"""消息处理：把 OneBot 事件分给各功能。海豹也会收到同样的消息，这里只响应自己的指令。"""
import asyncio
import base64
import html
import logging
import re
import sqlite3
import time
import urllib.request
from pathlib import Path

from . import ledger as ledger_mod
from . import templates as tpl
from .cards import Cards
from .db import log_op
from .ledger import Ledger, md
from .monitor import Monitor
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
CARD_WAIT_SECONDS = 180  # `.存卡 <卡名>` 之后多久内发的图片算数
JOIN_CHECK_DELAY = 5  # 骰娘进新群后等几秒再查成员，让成员列表先同步
_QQ = re.compile(r"[1-9]\d{4,11}")


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


def command_text(message, self_id: int | None) -> str | None:
    """识别指令用的文字。和 plain_text 不同，这里容忍「回复」和「@」：
    在 QQ 里回复一条消息时，客户端会自动带上回复引用和对原发送者的 @，
    管理员回复骰娘的记团公告发 `.团 消 …` 是很自然的用法。
    @ 骰娘自己的去掉；@ 别人的换成 QQ 号，这样 `.团 消 @某人` 等同于按 QQ 号消团。
    含图片等其他内容时返回 None。"""
    if isinstance(message, str):
        return plain_text(message)
    parts = []
    for seg in message:
        kind, data = seg.get("type"), seg.get("data") or {}
        if kind == "text":
            parts.append(data.get("text", ""))
        elif kind == "at":
            qq = str(data.get("qq", ""))
            if qq.isdigit() and int(qq) != self_id:
                parts.append(f" {qq} ")
        elif kind != "reply":
            return None
    return " ".join("".join(parts).split()) or None


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


def split_message(message) -> tuple[str, list[dict]]:
    """拆出文字和图片消息段。"""
    if isinstance(message, str):
        return message.strip(), []
    text = "".join(s["data"].get("text", "") for s in message if s.get("type") == "text")
    return text.strip(), [s for s in message if s.get("type") == "image"]


def http_get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.read()


def sender_name(event: dict) -> str:
    sender = event.get("sender") or {}
    return sender.get("card") or sender.get("nickname") or str(event.get("user_id"))


class Bot:
    def __init__(self, conn: sqlite3.Connection, onebot_url: str, onebot_token: str, data_dir: Path = Path("data")):
        self.conn = conn
        self.cards = Cards(conn, data_dir / "cards")
        self.waiting_cards: dict[int, tuple[str, float]] = {}  # QQ -> (卡名, 截止时间)
        self.http_get = http_get
        self.join_check_delay = JOIN_CHECK_DELAY
        self.settings = Settings(conn)
        self.templates = Templates(conn)
        self.ledger = Ledger(conn)
        self.repeat = RepeatTracker()
        self.roulette = Roulette()
        self.onebot = OneBot(onebot_url, onebot_token, self.handle_event)
        self.monitor = Monitor(self.onebot)
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
        elif event.get("post_type") == "notice" and event.get("notice_type") == "group_increase":
            await self.on_group_increase(int(event["group_id"]), int(event["user_id"]))

    async def on_group_message(self, event: dict) -> None:
        group_id, user_id = int(event["group_id"]), int(event["user_id"])
        text = plain_text(event.get("message", []))

        if group_id in self.settings.repeat_groups:
            chain_text = None if text is None or _COMMAND_LIKE.match(text) else text
            settled = self.repeat.feed(group_id, user_id, chain_text)
            if settled:
                await self.settle_repeat(group_id, settled)

        text = command_text(event.get("message", []), self.onebot.self_id)
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
        uid = int(event["user_id"])
        text, images = split_message(event.get("message", []))
        if cmd := parse_command(text, CARD_COMMANDS):
            await self.on_card(uid, *cmd, images)
        elif cmd := parse_command(text, LEDGER_COMMANDS):
            await self.on_ledger(event, ("private", uid), cmd[1])
        elif images and uid in self.waiting_cards:
            name, until = self.waiting_cards.pop(uid)
            if time.monotonic() <= until:
                await self.save_card(uid, name, images[0])

    # ---------- 入群欢迎与拉群校验 ----------
    async def on_group_increase(self, group_id: int, user_id: int) -> None:
        if user_id == self.onebot.self_id:
            await self.check_new_group(group_id)
            return
        if self.settings.is_ignored(user_id):
            return
        # 只在大群欢迎；其他群在后台单独设置了欢迎语才发
        if group_id != self.settings.get("main_group") and not self.templates.is_customized("welcome.default", str(group_id)):
            return
        await self.send(("group", group_id), "welcome.default", at=tpl.at(user_id),
                        nickname=await self.member_name(group_id, user_id), group_name=await self.group_name(group_id))

    async def group_name(self, group_id: int) -> str:
        try:
            return (await self.onebot.call("get_group_info", group_id=group_id) or {}).get("group_name") or str(group_id)
        except ActionError:
            return str(group_id)

    async def check_new_group(self, group_id: int) -> None:
        """骰娘被拉进新群：群里没有大群成员、又不在白名单，就发拒绝语后主动退群。"""
        main = self.settings.get("main_group")
        if not main or group_id == main:
            return
        name = await self.group_name(group_id)
        if group_id in self.settings.get("invite_whitelist"):
            log.info("进群 %s（%s）：在白名单中，放行", group_id, name)
            await self.send(("group", group_id), "invite.hello", group_name=name)
            return
        await asyncio.sleep(self.join_check_delay)
        try:
            here = await self.onebot.call("get_group_member_list", group_id=group_id, no_cache=True) or []
            big = await self.onebot.call("get_group_member_list", group_id=main, no_cache=True) or []
        except ActionError as e:
            log.error("进群 %s 校验失败，先留在群里：%s", group_id, e)
            return
        skip = {self.onebot.self_id, *self.settings.get("ignore_qq")}
        common = ({int(m["user_id"]) for m in here} & {int(m["user_id"]) for m in big}) - skip
        info = {"group_id": group_id, "group_name": name, "common": len(common)}
        if common:
            log.info("进群 %s（%s）：有 %d 位大群成员，留下", group_id, name, len(common))
            log_op(self.conn, "bot", "group.join", None, info)
            await self.send(("group", group_id), "invite.hello", group_name=name)
            return
        log.info("进群 %s（%s）：没有大群成员，退群", group_id, name)
        log_op(self.conn, "bot", "group.leave", None, info)
        await self.send(("group", group_id), "invite.reject", group_name=name)
        try:
            await self.onebot.call("set_group_leave", group_id=group_id)
        except ActionError as e:
            log.error("退出群 %s 失败：%s", group_id, e)

    # ---------- 人物卡（私聊） ----------
    async def on_card(self, uid: int, cmd: str, args: str, images: list[dict]) -> None:
        target = ("private", uid)
        if cmd == "存卡":
            if not args:
                await self.send(target, "card.save_usage")
            elif images:
                await self.save_card(uid, args, images[0])
            else:
                self.waiting_cards[uid] = (args, time.monotonic() + CARD_WAIT_SECONDS)
                await self.send(target, "card.wait_image", name=args)
        elif cmd == "取卡":
            parts = args.split(maxsplit=1)
            owner = uid
            if parts and _QQ.fullmatch(parts[0]):
                owner, parts = int(parts[0]), parts[1:]
                if owner != uid and not await self.is_main_group_member(uid):  # KP 取 PL 的卡
                    await self.send(target, "card.denied")
                    return
            who = "你" if owner == uid else str(owner)
            if not parts:
                cards = self.cards.current(owner)
                if cards:
                    lines = "\n".join(f"{i}. {c['name']}" for i, c in enumerate(cards, 1))
                    await self.send(target, "card.list", owner=who, cards=lines)
                else:
                    await self.send(target, "card.list_none", owner=who)
                return
            card = self.cards.find(owner, parts[0])
            if card is None:
                await self.send(target, "card.not_found", query=parts[0])
                return
            data = base64.b64encode(self.cards.file(card).read_bytes()).decode()
            await self.deliver(target, [{"type": "image", "data": {"file": f"base64://{data}"}}], "人物卡")
        elif cmd == "删卡":
            if not args:
                await self.send(target, "card.delete_usage")
                return
            card = self.cards.find(uid, args)
            if card is None:
                await self.send(target, "card.not_found", query=args)
                return
            self.cards.delete(uid, card["name"], str(uid))
            await self.send(target, "card.deleted", name=card["name"])

    async def save_card(self, uid: int, name: str, image: dict) -> None:
        self.waiting_cards.pop(uid, None)
        try:
            data = await self.download_image(image)
            self.cards.save(uid, name, data)
        except Exception as e:
            log.error("保存 %s 的人物卡「%s」失败：%s", uid, name, e)
            await self.send(("private", uid), "card.save_failed", name=name)
            return
        log.info("存卡：%s「%s」", uid, name)
        await self.send(("private", uid), "card.saved", name=name)

    async def download_image(self, image: dict) -> bytes:
        """QQ 图片链接会过期，收到就下载。直接下载失败时，向协议端要一个新链接再试。"""
        data = image.get("data") or {}
        if data.get("url"):
            try:
                return await asyncio.to_thread(self.http_get, data["url"])
            except Exception as e:
                log.warning("直接下载图片失败，改用 get_image：%s", e)
        info = await self.onebot.call("get_image", file=data.get("file") or data.get("file_id") or "") or {}
        if info.get("file") and Path(info["file"]).is_file():
            return Path(info["file"]).read_bytes()
        if info.get("url"):
            return await asyncio.to_thread(self.http_get, info["url"])
        raise ActionError("get_image 没有返回可用的图片")

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

    async def is_main_group_member(self, user_id: int) -> bool:
        main = self.settings.get("main_group")
        if not main:
            return False
        try:
            info = await self.onebot.call("get_group_member_info", group_id=main, user_id=user_id, no_cache=True)
        except ActionError:
            return False
        # SnowLuma 查群外的人不报错，而是返回入群时间为 0 的空资料
        return bool((info or {}).get("join_time"))

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
    def ledger_list_text(self, with_qq: bool = False) -> str:
        """`.团` 的名单。群公告里每行后面加 QQ 号（with_qq），方便管理对上人。"""
        lines = [self.templates.render_text("ledger.title") or ""]
        rule = "————————————"
        lines.append(rule)
        link = self.templates.render_text("ledger.link")
        if link:
            lines += ["全部名单：", link, rule]
        people = self.ledger.people()
        lines += [f"{p.entry} {p.qq}" if with_qq else p.entry for p in people] if people else [self.templates.render_text("ledger.empty") or ""]
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
                await self.onebot.call("_send_group_notice", group_id=main, content=self.ledger_list_text(with_qq=True),
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
        elif sub in ("名", "改名", "name"):
            rest = parts[1:]
            if len(rest) >= 2 and _QQ.fullmatch(rest[0]):
                qq, name = int(rest[0]), " ".join(rest[1:])
                if qq != uid and not await self.is_main_group_admin(uid):
                    await self.send(target, "ledger.rename_denied")
                    return
            elif rest:
                qq, name = uid, " ".join(rest)
            else:
                await self.send(target, "ledger.rename_usage")
                return
            if len(name) > 20:
                await self.send(target, "ledger.rename_usage")
                return
            old = self.ledger.member_name(qq) or str(qq)
            self.ledger.rename(qq, name, str(uid))
            await self.send(target, "ledger.renamed", old=old, name=name)
            person = self.ledger.person(qq)
            if person and person.deadlines:
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
