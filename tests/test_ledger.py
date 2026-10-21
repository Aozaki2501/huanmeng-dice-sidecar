import asyncio
import itertools
import random
from datetime import date

import pytest

from sidecar import db
from sidecar.bot import Bot
from sidecar.ledger import Ledger, add_months, nearest_year, parse_list_line
from sidecar.repeat import RepeatTracker
from sidecar.roulette import MAX_PLAYERS, Roulette

P = {i: 10000 + i for i in range(1, 10)}  # P1..P9 的 QQ 号
BOT_QQ = 99999


# ---------- 死线 ----------
@pytest.mark.parametrize("start,months,expect", [
    (date(2026, 12, 31), 2, date(2027, 2, 28)),
    (date(2027, 12, 31), 2, date(2028, 2, 29)),
    (date(2026, 9, 17), 2, date(2026, 11, 17)),
    (date(2026, 11, 30), 3, date(2027, 2, 28)),
])
def test_add_months(start, months, expect):
    assert add_months(start, months) == expect


def test_nearest_year():
    ref = date(2026, 12, 20)
    assert nearest_year(1, 5, ref) == date(2027, 1, 5)
    assert nearest_year(11, 3, ref) == date(2026, 11, 3)
    assert nearest_year(7, 20, date(2027, 1, 10)) == date(2026, 7, 20)


def test_parse_list_line():
    ref = date(2026, 10, 17)
    assert parse_list_line("Flame（11/3，11/30）", ref) == ("Flame", [date(2026, 11, 3), date(2026, 11, 30)])
    assert parse_list_line("23（7/23）", ref) == ("23", [date(2026, 7, 23)])
    with pytest.raises(ValueError):
        parse_list_line("Flame 11/3", ref)
    with pytest.raises(ValueError):
        parse_list_line("Flame（十一月）", ref)


# ---------- 复读链：规则以群规原文为准 ----------
def run_chain(seq):
    """seq: [(玩家编号或 'bot', 文本或 None)]，返回所有结算。"""
    t = RepeatTracker()
    return [s for who, text in seq if (s := t.feed(1, P[who], text))]


def test_chain_needs_four():
    assert run_chain([(1, "草"), (2, "草"), (3, "草"), (9, "停")]) == []


def test_third_last_repeater():
    [s] = run_chain([(1, "草"), (2, "草"), (3, "草"), (4, "草"), (9, "停")])
    assert (s.target, s.reason, s.count) == (P[2], "third", 4)


def test_participant_breaks():
    [s] = run_chain([(1, "哈"), (2, "哈"), (3, "哈"), (4, "哈"), (3, "不哈了")])
    assert (s.target, s.reason) == (P[3], "breaker")


def test_origin_breaks_freely():
    [s] = run_chain([(1, "a"), (2, "a"), (3, "a"), (4, "a"), (1, "b")])
    assert (s.target, s.reason) == (P[2], "third")


def test_origin_who_joined_is_participant():
    [s] = run_chain([(1, "a"), (2, "a"), (3, "a"), (4, "a"), (1, "a"), (1, "b")])
    assert (s.target, s.reason) == (P[1], "breaker")


def test_origin_first_repeat_is_not_dup_but_second_is():
    assert run_chain([(1, "a"), (2, "a"), (3, "a"), (4, "a"), (1, "a")]) == []
    [s] = run_chain([(1, "a"), (2, "a"), (3, "a"), (4, "a"), (1, "a"), (1, "a")])
    assert (s.target, s.reason, s.count) == (P[1], "dup", 6)


def test_dup_after_effective():
    [s] = run_chain([(1, "a"), (2, "a"), (3, "a"), (4, "a"), (2, "a")])
    assert (s.target, s.reason, s.count) == (P[2], "dup", 5)


def test_dup_before_effective_not_punished():
    # P2 在生效前复读两次，第 4 条时生效；之后 P9 打断，倒数第三位是 P2
    [s] = run_chain([(1, "a"), (2, "a"), (2, "a"), (3, "a"), (9, "b")])
    assert (s.target, s.reason) == (P[2], "third")


def test_dup_stops_chain_and_restarts():
    seq = [(1, "a"), (2, "a"), (3, "a"), (4, "a"), (2, "a"), (5, "a"), (6, "a"), (9, "b")]
    results = run_chain(seq)
    assert [r.reason for r in results] == ["dup"]  # 中止后 P2 的那条作为新原句，只有 3 条，不生效


def test_image_breaks():
    [s] = run_chain([(1, "a"), (2, "a"), (3, "a"), (4, "a"), (9, None)])
    assert s.target == P[2]
    assert run_chain([(1, "a"), (2, "a"), (3, "a"), (4, "a"), (9, None), (5, "a"), (6, "b")]) == [s]


# ---------- 轮盘 ----------
def test_roulette_full_game():
    rr = Roulette(random.Random(1))
    g = 1
    for i in range(1, MAX_PLAYERS):
        assert rr.join(g, P[i], f"P{i}", 15).key == "rr.join"
    assert rr.join(g, P[1], "P1", 15).key == "rr.join_dup"
    assert rr.join(g, P[6], "P6", 15).key == "rr.join_full"
    assert rr.quit(g, P[1], "P1", 15).key == "rr.quit_started"
    assert rr.join(g, P[7], "P7", 15).key == "rr.join_started"
    assert rr.shoot(g, P[1], 15).key == "rr.shoot_need_roll"
    out = None
    for _ in range(50):
        game = rr.games[g]
        if game.order:
            break
        for p in [p for p in game.players if not p.roll]:
            out = rr.roll(g, p.id, 15)
    assert out.key == "rr.order_ready"
    order = rr.games[g].order
    assert [p.roll for p in order] == sorted((p.roll for p in order), reverse=True)
    assert rr.shoot(g, order[1].id, 15).key == "rr.shoot_not_turn"
    [bullet] = rr.games[g].bullets
    for i in range(bullet):
        assert rr.shoot(g, order[i].id, 15).key == "rr.click"
    out = rr.shoot(g, order[bullet].id, 15)
    assert out.key == "rr.bang" and out.shot.id == order[bullet].id
    assert g not in rr.games


def test_roulette_timeout_and_off(monkeypatch):
    rr = Roulette()
    for i in range(1, MAX_PLAYERS + 1):
        rr.join(1, P[i], f"P{i}", 15)
    rr.games[1].updated -= 16 * 60
    assert rr.status(1, 15).key == "rr.status_none"
    rr.join(1, P[1], "P1", 15)
    rr.games[1].updated -= 60 * 60  # 报名阶段不计时
    assert rr.status(1, 15).key == "rr.status_signup"
    assert rr.off(1, False, 15).key == "rr.off_denied"
    assert rr.off(1, True, 15).key == "rr.off"


@pytest.mark.parametrize("empty_chamber", range(MAX_PLAYERS))
def test_caucasus_full_game(empty_chamber):
    rr = Roulette(random.Random(1))
    for i in range(1, MAX_PLAYERS + 1):
        rr.join(1, P[i], f"P{i}", 15, caucasus=True)
    game = rr.games[1]
    while not game.order:
        for p in [p for p in game.players if not p.roll]:
            rr.roll(1, p.id, 15)
    assert len(game.bullets) == 5
    # 遍历全部六种空弹位，包含最后一枪空枪与最后一枪中弹。
    game.bullets = set(range(MAX_PLAYERS)) - {empty_chamber}
    victims = []
    for i, player in enumerate(game.order):
        assert rr.shoot(1, game.order[(i + 1) % MAX_PLAYERS].id, 15).key == "rr.shoot_not_turn"
        assert game.turn == i
        out = rr.shoot(1, player.id, 15)
        hit = i != empty_chamber
        assert bool(out.shot) == hit
        if hit:
            victims.append(out.shot.id)
        assert out.key == ("cr.bang" if hit else "cr.click") + ("_last" if i == 5 else "")
        if i < 5:
            assert rr.status(1, 15).values["shots"] == i + 1
            assert rr.shoot(1, player.id, 15).key == "rr.shoot_not_turn"
        else:
            assert out.values["survivor"] == game.order[empty_chamber].name
    assert len(set(victims)) == 5
    assert game.order[empty_chamber].id not in victims
    assert 1 not in rr.games
    assert rr.shoot(1, game.order[-1].id, 15).key == "rr.shoot_none"


def test_roulette_modes_cannot_mix():
    rr = Roulette()
    rr.join(1, P[1], "P1", 15, caucasus=True)
    assert rr.join(1, P[2], "P2", 15).key == "rr.mode_conflict"
    assert len(rr.games[1].players) == 1
    # 不同群独立，最后一人退报名后可换玩法。
    assert rr.join(2, P[2], "P2", 15).key == "rr.join"
    assert rr.join(2, P[3], "P3", 15, caucasus=True).key == "rr.mode_conflict"
    assert rr.quit(1, P[1], "P1", 15).key == "rr.quit"
    assert rr.join(1, P[2], "P2", 15).key == "rr.join"


# ---------- 接入 bot：记团、公告、名单格式 ----------
class FakeBot(Bot):
    def __init__(self, conn, data_dir):
        super().__init__(conn, "ws://x", "", data_dir)
        self.onebot.self_id = BOT_QQ
        self.sent = []
        self.calls = []  # 其他接口调用 (action, params)
        self.replies = {}  # action -> 返回值
        self.roles = {}
        self.outsiders = set()  # (群号, QQ)：不在群里的人
        self.notices = []
        self.notice_ids = itertools.count(1)

        async def call(action, **params):
            if action == "send_group_msg":
                self.sent.append(("group", params["group_id"], _text(params["message"])))
            elif action == "send_private_msg":
                self.sent.append(("private", params["user_id"], _text(params["message"])))
            elif action == "_get_group_notice":
                return [dict(n) for n in self.notices]
            elif action == "_send_group_notice":
                text = params["content"].replace("\n", "&#10;")  # QQ 返回的正文是 HTML 转义过的
                self.notices.append({"notice_id": f"n{next(self.notice_ids)}", "sender_id": BOT_QQ,
                                     "message": {"text": text}})
            elif action == "_del_group_notice":
                self.notices = [n for n in self.notices if n["notice_id"] != params["notice_id"]]
            elif action == "get_group_member_info":
                if (params["group_id"], params["user_id"]) in self.outsiders:  # 像 SnowLuma 一样返回空资料
                    return {"role": "member", "card": "", "join_time": 0}
                return {"role": self.roles.get((params["group_id"], params["user_id"]), "member"),
                        "card": f"名片{params['user_id']}", "join_time": 1700000000}
            else:
                self.calls.append((action, params))
                return self.replies.get(action)
        self.onebot.call = call


def _text(segs):
    return "".join(s["data"].get("text", "") if s["type"] == "text" else f"[{s['type']}]" for s in segs)


def msg(group, user, text, role="member"):
    return {"post_type": "message", "message_type": "group", "group_id": group, "user_id": user,
            "sender": {"card": f"P{user - 10000}", "role": role},
            "message": [{"type": "text", "data": {"text": text}}] if text is not None else [{"type": "image", "data": {}}]}


@pytest.fixture
def fb(tmp_path):
    bot = FakeBot(db.connect(tmp_path / "t.db"), tmp_path)
    bot.settings.update({"main_group": 100, "repeat_groups": [100, 200]}, "test")
    return bot


def feed(bot, *events):
    async def go():
        for e in events:
            await bot.handle_event(e)
    asyncio.run(go())


def test_ledger_list_format(fb):
    led = Ledger(fb.conn)
    led.import_people([(P[1], "巧克力", [date(2026, 7, 20)]),
                       (P[2], "Flame", [date(2026, 11, 30), date(2026, 11, 3)]),
                       (P[3], "km", [date(2026, 12, 16)])], "test")
    feed(fb, msg(200, P[9], ".团"))
    assert fb.sent[-1] == ("group", 200, (
        "【记团名单】\n"
        "榜上有名的须在死线日期（月/日）前开团，不然会被赠送飞机票哟。消团券获得者在全部名单链接中也有记录。\n"
        "————————————\n"
        "全部名单：\n"
        "https://docs.qq.com/sheet/DQVFWdGlzdnF5VXFK\n"
        "————————————\n"
        "巧克力（7/20）\n"
        "Flame（11/3，11/30）\n"
        "km（12/16）"))


def test_repeat_in_small_group_announces_twice(fb):
    feed(fb, *(msg(200, P[i], "草") for i in range(1, 5)), msg(200, P[9], "停"))
    texts = [(kind, gid) for kind, gid, _ in fb.sent]
    assert texts == [("group", 200), ("group", 100)]
    assert "名片10002" in fb.sent[0][2] or "P2" in fb.sent[0][2]
    assert "复读链倒数第三" in fb.sent[0][2]
    assert fb.ledger.person(P[2]).deadlines


def test_repeat_in_main_group_announces_once(fb):
    feed(fb, *(msg(100, P[i], "草") for i in range(1, 5)), msg(100, P[9], "停"))
    assert len(fb.sent) == 1


def test_ignored_bot_is_invisible(fb):
    fb.settings.update({"ignore_qq": [P[8]]}, "test")
    feed(fb, msg(200, P[1], "a"), msg(200, P[2], "a"), msg(200, P[8], "a"), msg(200, P[3], "a"),
         msg(200, P[4], "a"), msg(200, P[9], "b"))
    assert fb.ledger.person(P[2]).deadlines  # 机器人不算，P1 原句 + P2 P3 P4，倒数第三是 P2


def test_commands_break_but_dont_extend(fb):
    feed(fb, *(msg(200, P[i], "a") for i in range(1, 5)), msg(200, P[9], ".r 1d100"))
    assert fb.ledger.person(P[2]).deadlines


def test_clear_permission(fb):
    Ledger(fb.conn).import_people([(P[2], "Flame", [date(2026, 11, 3), date(2026, 11, 30)])], "test")
    feed(fb, msg(200, P[5], ".团 消 Flame", role="admin"))  # 小群管理，不是大群管理
    assert fb.sent[-1][2] == "只有大群管理员可以消团。"
    fb.roles[(100, P[6])] = "admin"
    feed(fb, msg(200, P[6], ".团 消 Flame"))
    assert fb.sent[-1][2] == "Flame 消去一团（死线 11/3），剩余：Flame（11/30）"
    feed(fb, msg(200, P[6], ".团 消 10002"))
    assert fb.sent[-1][2] == "Flame 消去一团（死线 11/30），已还清。"
    assert fb.conn.execute("SELECT count(*) FROM op_log WHERE action = 'ledger.clear'").fetchone()[0] == 2


def test_clear_ambiguous_and_numeric_name(fb):
    Ledger(fb.conn).import_people([(P[1], "23", [date(2026, 7, 23)]), (P[2], "Flame", [date(2026, 11, 3)]),
                                   (P[3], "Flame", [date(2026, 12, 1)])], "test")
    fb.settings.update({"ledger_admins": [P[6]]}, "test")
    feed(fb, msg(200, P[6], ".团 消 Flame"))
    assert "名单里有多个「Flame」" in fb.sent[-1][2] and "QQ 10003" in fb.sent[-1][2]
    feed(fb, msg(200, P[6], ".团 消 23"))
    assert fb.sent[-1][2] == "23 消去一团（死线 7/23），已还清。"


def test_mine_and_manual_add(fb):
    fb.roles[(100, P[6])] = "owner"
    feed(fb, msg(200, P[1], ".团 我"))
    assert fb.sent[-1][2] == "你目前不欠团。"
    feed(fb, msg(200, P[6], ".团 加 10001 巧克力 7/20"), msg(200, P[1], ".团 我"))
    assert fb.sent[-2][2] == "已记团：巧克力（7/20）"
    assert fb.sent[-1][2] == "巧克力（7/20），共 1 团。"
    feed(fb, msg(200, P[6], ".团 加 abc 巧克力"))
    assert fb.sent[-1][2].startswith("用法：.团 加")


def test_roulette_bang_records_and_announces(fb):
    fb.roulette.rng = random.Random(3)
    feed(fb, *(msg(200, P[i], ".轮盘 join") for i in range(1, 7)))
    game = fb.roulette.games[200]
    while not game.order:
        feed(fb, *(msg(200, p.id, ".轮盘 roll") for p in game.players if not p.roll))
    order = game.order
    [bullet] = game.bullets
    fb.sent.clear()
    feed(fb, *(msg(200, order[i].id, ".开枪") for i in range(bullet + 1)))
    victim = order[bullet]
    assert [(k, g) for k, g, _ in fb.sent[-2:]] == [("group", 200), ("group", 100)]
    assert "中弹倒地" in fb.sent[-2][2] and "【记团公告】" in fb.sent[-1][2]
    assert fb.ledger.person(victim.id).deadlines


@pytest.mark.parametrize("group", [100, 200])
@pytest.mark.parametrize("empty_chamber", range(MAX_PLAYERS))
def test_caucasus_records_five_people_and_announces(fb, group, empty_chamber):
    fb.roulette.rng = random.Random(3)
    feed(fb, *(msg(group, P[i], ".高加索轮盘 报名") for i in range(1, 7)))
    assert "高加索轮盘" in fb.sent[-1][2]
    game = fb.roulette.games[group]
    while not game.order:
        feed(fb, *(msg(group, p.id, ".高加索 掷点") for p in game.players if not p.roll))
    assert "5颗子弹" in fb.sent[-1][2]
    game.bullets = set(range(MAX_PLAYERS)) - {empty_chamber}
    fb.sent.clear()
    for player in game.order:
        feed(fb, msg(group, player.id, ".开枪"))
        # 同一个人重发不应再写入账本。
        before = len(fb.ledger.records())
        feed(fb, msg(group, player.id, ".开枪"))
        assert len(fb.ledger.records()) == before
    records = fb.ledger.records()
    assert len(records) == 5
    assert {r["qq"] for r in records} == {p.id for i, p in enumerate(game.order) if i != empty_chamber}
    assert all(r["source"] == "rr" for r in records)
    assert fb.ledger.person(game.order[empty_chamber].id) is None
    assert len([t for _, _, t in fb.sent if "记一团，死线" in t]) == (5 if group == 100 else 10)
    assert any("唯一生还者" in t and game.order[empty_chamber].name in t for _, _, t in fb.sent)
    assert len(fb.notices) == 1  # 每次更新名单公告，只保留最后一份。
    assert group not in fb.roulette.games


@pytest.mark.parametrize("end", ["off", "timeout"])
def test_caucasus_partial_game_keeps_recorded_debts(fb, end):
    feed(fb, *(msg(200, P[i], ".高加索 报名") for i in range(1, 7)))
    game = fb.roulette.games[200]
    while not game.order:
        feed(fb, *(msg(200, p.id, ".高加索 掷点") for p in game.players if not p.roll))
    game.bullets = set(range(5))
    feed(fb, msg(200, game.order[0].id, ".高加索 开枪"))
    assert len(fb.ledger.records()) == 1
    if end == "off":
        feed(fb, msg(200, P[9], ".高加索 结束"))
        assert fb.sent[-1][2] == "只有管理员可以强制结束。"
        assert 200 in fb.roulette.games
        feed(fb, msg(200, P[9], ".高加索 结束", role="admin"))
        assert "已经记下的欠团保留" in fb.sent[-1][2]
    else:
        game.updated -= 16 * 60
        feed(fb, msg(200, P[9], ".高加索"))
        assert "当前没有进行中的轮盘" in fb.sent[-1][2]
    assert len(fb.ledger.records()) == 1
    assert 200 not in fb.roulette.games


def test_caucasus_help_and_mode_conflicts(fb):
    feed(fb, msg(200, P[1], ".高加索轮盘"))
    assert "5颗子弹" in fb.sent[-1][2]
    assert "已记团保留" in fb.sent[-1][2]
    feed(fb, msg(200, P[1], ".高加索 报名"), msg(200, P[2], ".轮盘 报名"))
    assert "本群已有高加索轮盘" in fb.sent[-1][2]
    assert len(fb.roulette.games[200].players) == 1
    feed(fb, msg(100, P[1], ".轮盘 报名"), msg(100, P[2], ".高加索 报名"))
    assert "本群已有俄罗斯轮盘" in fb.sent[-1][2]
    feed(fb, msg(100, P[1], ".高加索 开枪"))
    assert "本群已有俄罗斯轮盘" in fb.sent[-1][2]
    assert len(fb.roulette.games[100].players) == 1
    feed(fb, msg(100, P[1], ".高加索 help"))
    assert "5颗子弹" in fb.sent[-1][2]


def test_card_command_in_group(fb):
    feed(fb, msg(200, P[1], ".存卡 调查员"))
    assert fb.sent[-1][2] == "人物卡指令请私聊骰娘使用。"


def test_ledger_change_replaces_group_notice(fb):
    fb.notices = [
        {"notice_id": "old", "sender_id": BOT_QQ, "message": {"text": "【记团名单】&#10;旧名单"}},
        {"notice_id": "rules", "sender_id": BOT_QQ, "message": {"text": "【幻梦境赌场】&#10;群规"}},
        {"notice_id": "human", "sender_id": P[1], "message": {"text": "【记团名单】&#10;群主手发"}},
    ]
    fb.roles[(100, P[6])] = "admin"
    feed(fb, msg(200, P[6], ".团 加 10001 巧克力 7/20"))
    ids = [n["notice_id"] for n in fb.notices]
    assert ids[:2] == ["rules", "human"] and len(ids) == 3
    assert fb.notices[-1]["message"]["text"].endswith("&#10;巧克力（7/20） 10001")
    feed(fb, msg(100, P[6], ".团 消 巧克力"))
    assert len(fb.notices) == 3 and fb.notices[-1]["message"]["text"].endswith("&#10;暂无。")


def test_clear_by_replying_to_announcement(fb):
    """回复骰娘的记团公告来消团：消息里带着回复引用和对骰娘的 @，之前会被整条忽略。"""
    fb.roles[(100, P[6])] = "admin"
    feed(fb, *(msg(200, P[i], "草") for i in range(1, 5)), msg(200, P[9], "停"))
    e = msg(200, P[6], "")
    e["message"] = [{"type": "reply", "data": {"id": "1"}}, {"type": "at", "data": {"qq": str(BOT_QQ)}},
                    {"type": "text", "data": {"text": " .团 消 10002"}}]
    feed(fb, e)
    assert fb.sent[-1][2].endswith("已还清。")


def test_clear_by_at(fb):
    fb.roles[(100, P[6])] = "admin"
    feed(fb, *(msg(200, P[i], "草") for i in range(1, 5)), msg(200, P[9], "停"))
    e = msg(200, P[6], "")
    e["message"] = [{"type": "text", "data": {"text": ".团 消 "}}, {"type": "at", "data": {"qq": str(P[2])}}]
    feed(fb, e)
    assert fb.sent[-1][2].endswith("已还清。")


def test_at_and_reply_still_break_repeat_chain(fb):
    """指令识别放宽了，复读链仍然只认纯文字：带 @ 或回复的消息会打断，不会续链。"""
    e = msg(200, P[4], "")
    e["message"] = [{"type": "at", "data": {"qq": str(P[1])}}, {"type": "text", "data": {"text": "草"}}]
    feed(fb, *(msg(200, P[i], "草") for i in range(1, 4)), e, msg(200, P[5], "草"), msg(200, P[9], "停"))
    assert fb.ledger.person(P[2]) is None


# ---------- 圈名 ----------
def test_self_bind_name_before_recorded(fb):
    """先绑了圈名，以后被记团就用圈名，而不是当时的群名片。"""
    feed(fb, msg(200, P[2], ".团 名 Flame"))
    assert fb.sent[-1][2] == "名单上的「10002」已改为「Flame」。"
    feed(fb, *(msg(200, P[i], "草") for i in range(1, 5)), msg(200, P[9], "停"))
    assert fb.ledger.person(P[2]).name == "Flame"
    assert "Flame（" in fb.sent[-1][2]


def test_admin_renames_other_and_notice_updates(fb):
    feed(fb, *(msg(200, P[i], "草") for i in range(1, 5)), msg(200, P[9], "停"))  # 记 P2，名字是群名片 P2
    feed(fb, msg(200, P[3], f".团 名 {P[2]} Flame"))
    assert fb.sent[-1][2].startswith("只有大群管理员可以改别人的名字")
    fb.roles[(100, P[6])] = "admin"
    e = msg(200, P[6], "")
    e["message"] = [{"type": "text", "data": {"text": ".团 名 "}}, {"type": "at", "data": {"qq": str(P[2])}},
                    {"type": "text", "data": {"text": " Flame 酱"}}]
    feed(fb, e)
    assert fb.sent[-1][2] == "名单上的「名片10002」已改为「Flame 酱」。"
    assert fb.notices[-1]["message"]["text"].endswith("Flame 酱（" + fb.ledger.person(P[2]).entry.split("（")[1] + f" {P[2]}")
    feed(fb, msg(200, P[2], ".团 我"))
    assert fb.sent[-1][2].startswith("Flame 酱（")


def test_rename_usage(fb):
    feed(fb, msg(200, P[2], ".团 名"))
    assert fb.sent[-1][2].startswith("用法：.团 名")
    feed(fb, msg(200, P[2], ".团 名 " + "长" * 21))
    assert fb.sent[-1][2].startswith("用法：.团 名")
    feed(fb, msg(200, P[2], ".团 名 23"))  # 短数字是名字，不当 QQ 号
    assert fb.ledger.member_name(P[2]) == "23"
