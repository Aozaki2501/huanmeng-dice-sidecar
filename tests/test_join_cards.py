"""第 5、6 阶段：入群欢迎、拉群校验、人物卡。"""
import pytest
from fastapi.testclient import TestClient

from sidecar import db
from sidecar.config import Config, hash_password
from sidecar.onebot import ActionError
from sidecar.web import create_app
from test_ledger import BOT_QQ, P, FakeBot, feed

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20


@pytest.fixture
def fb(tmp_path):
    bot = FakeBot(db.connect(tmp_path / "t.db"), tmp_path)
    bot.settings.update({"main_group": 100, "card_announce_group": None}, "test")
    bot.join_check_delay = 0
    bot.http_get = lambda url: PNG
    return bot


def joined(group, user):
    return {"post_type": "notice", "notice_type": "group_increase", "group_id": group, "user_id": user}


def pm(user, text="", image=False):
    segs = [{"type": "text", "data": {"text": text}}] if text else []
    if image:
        segs.append({"type": "image", "data": {"file": "abc.png", "url": "https://example.invalid/abc"}})
    return {"post_type": "message", "message_type": "private", "user_id": user, "message": segs}


# ---------- 入群欢迎 ----------
def test_welcome_only_in_main_group(fb):
    feed(fb, joined(100, P[1]))
    group, text = fb.sent[-1][1], fb.sent[-1][2]
    assert group == 100 and text.startswith("[at]\n欢迎新佬，做个自我介绍吧~") and text.endswith("ʕ •ᴥ•ʔ")
    fb.sent.clear()
    feed(fb, joined(200, P[1]))
    assert fb.sent == []
    fb.templates.save("welcome.default", ["小群欢迎 {at}"], "test", scope="200")
    feed(fb, joined(200, P[1]))
    assert fb.sent[-1] == ("group", 200, "小群欢迎 [at]")


def test_welcome_skips_ignored_bots(fb):
    fb.settings.update({"ignore_qq": [P[9]]}, "test")
    feed(fb, joined(100, P[9]))
    assert fb.sent == []


# ---------- 拉群校验 ----------
def members(*qqs):
    return [{"user_id": q} for q in qqs]


def test_invite_without_main_members_leaves(fb):
    fb.settings.update({"ignore_qq": [P[9]]}, "test")
    lists = {300: members(BOT_QQ, P[9], P[5]), 100: members(BOT_QQ, P[9], P[1])}
    fb.replies["get_group_info"] = {"group_name": "野群"}

    async def call(action, **params):
        if action == "get_group_member_list":
            return lists[params["group_id"]]
        return await orig(action, **params)
    orig, fb.onebot.call = fb.onebot.call, call
    feed(fb, joined(300, BOT_QQ))
    assert fb.sent[-1] == ("group", 300, "这个群里没有找到幻梦境的群友，骰娘先告辞了。")
    assert ("set_group_leave", {"group_id": 300}) in fb.calls


def test_invite_with_main_member_stays(fb):
    lists = {300: members(BOT_QQ, P[1]), 100: members(BOT_QQ, P[1])}

    async def call(action, **params):
        if action == "get_group_member_list":
            return lists[params["group_id"]]
        return await orig(action, **params)
    orig, fb.onebot.call = fb.onebot.call, call
    feed(fb, joined(300, BOT_QQ))
    assert not any(a == "set_group_leave" for a, _ in fb.calls)
    assert fb.sent == []  # 进群问候默认为空，不发


def test_invite_whitelist_skips_check(fb):
    fb.settings.update({"invite_whitelist": [300]}, "test")
    feed(fb, joined(300, BOT_QQ))
    assert [a for a, _ in fb.calls] == ["get_group_info"]


# ---------- 人物卡 ----------
def test_save_list_get_delete(fb):
    feed(fb, pm(P[1], ".存卡 调查员A", image=True))
    assert fb.sent[-1] == ("private", P[1], "已存档人物卡「调查员A」。")
    feed(fb, pm(P[1], ".存卡 调查员B"))
    assert "3 分钟内" in fb.sent[-1][2]
    feed(fb, pm(P[1], image=True))
    assert fb.sent[-1][2] == "已存档人物卡「调查员B」。"
    feed(fb, pm(P[1], ".存卡 调查员A", image=True))  # 新版本，旧图保留
    assert len(list((fb.cards.root / str(P[1])).iterdir())) == 3
    feed(fb, pm(P[1], ".取卡"))
    assert fb.sent[-1][2] == "你的人物卡：\n1. 调查员A\n2. 调查员B\n发送 .取卡 <编号或卡名> 取回图片。"
    feed(fb, pm(P[1], ".取卡 2"))
    assert fb.sent[-1][2] == "[image]"
    feed(fb, pm(P[1], ".删卡 调查员A"))
    assert fb.sent[-1][2] == "已删除人物卡「调查员A」。"
    assert [c["name"] for c in fb.cards.current(P[1])] == ["调查员B"]
    assert len(list((fb.cards.root / "_deleted" / str(P[1])).iterdir())) == 2
    feed(fb, pm(P[1], ".取卡 调查员A"))
    assert fb.sent[-1][2] == "没有找到人物卡「调查员A」，发送 .取卡 查看列表。"


def test_image_after_timeout_ignored(fb):
    feed(fb, pm(P[1], ".存卡 调查员A"))
    fb.waiting_cards[P[1]] = (P[1], "调查员A", 0)
    feed(fb, pm(P[1], image=True))
    assert fb.cards.current(P[1]) == []


def test_download_falls_back_to_get_image(fb, tmp_path):
    def broken(url):
        if "example.invalid" in url:
            raise OSError("过期")
        return PNG
    fb.http_get = broken
    fb.replies["get_image"] = {"url": "https://fresh/abc"}
    feed(fb, pm(P[1], ".存卡 调查员A", image=True))
    assert fb.sent[-1][2] == "已存档人物卡「调查员A」。"
    assert fb.calls[-1] == ("get_image", {"file": "abc.png"})


def test_save_failure_reported(fb):
    def broken(url):
        raise OSError("断网")
    fb.http_get = broken
    feed(fb, pm(P[1], ".存卡 调查员A", image=True))
    assert fb.sent[-1][2] == "人物卡「调查员A」的图片没能保存，请重新发送一次。"


def test_get_others_card_needs_main_group_member(fb):
    feed(fb, pm(P[1], ".存卡 调查员A", image=True))
    fb.outsiders.add((100, P[3]))
    feed(fb, pm(P[3], f".取卡 {P[1]}"))
    assert fb.sent[-1][2] == "只有大群成员可以取别人的人物卡。"
    feed(fb, pm(P[2], f".取卡 {P[1]}"))  # 普通大群成员（比如 KP）可以取
    assert fb.sent[-1][2].startswith(f"{P[1]}的人物卡：\n1. 调查员A")
    feed(fb, pm(P[2], f".取卡 {P[1]} 1"))
    assert fb.sent[-1][2] == "[image]"


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_main_group_admin_saves_others_card(fb, role):
    import json

    fb.roles[(100, P[2])] = role
    feed(fb, pm(P[2], f".存卡 {P[1]} 调查员 A", image=True))
    card = fb.cards.find(P[1], "调查员 A")
    assert card is not None and fb.cards.file(card).read_bytes() == PNG
    assert fb.cards.current(P[2]) == []
    assert fb.sent[-1] == ("private", P[2], f"已为 QQ {P[1]} 存档人物卡「调查员 A」。")
    assert not any(uid == P[1] for _, uid, _ in fb.sent)
    op = fb.conn.execute("SELECT * FROM op_log WHERE action = 'card.save'").fetchone()
    assert op["operator"] == str(P[2])
    assert json.loads(op["after"])["qq"] == P[1]
    # 卡主可自行取回、更新和删除代存的卡。
    feed(fb, pm(P[1], ".取卡 1"))
    assert fb.sent[-1] == ("private", P[1], "[image]")
    feed(fb, pm(P[1], ".存卡 调查员 A", image=True))
    assert fb.cards.find(P[1], "调查员 A")["id"] != card["id"]
    feed(fb, pm(P[1], ".删卡 调查员 A"))
    assert fb.cards.current(P[1]) == []


def test_admin_delayed_upload_keeps_owner_and_waits_independently(fb):
    fb.roles[(100, P[2])] = "admin"
    feed(fb, pm(P[1], ".存卡 自存卡"), pm(P[2], f".存卡 {P[1]} 代存卡"))
    assert f"将保存到 QQ {P[1]} 名下" in fb.sent[-1][2]
    feed(fb, pm(P[2], image=True))
    assert fb.cards.find(P[1], "代存卡") is not None
    assert fb.cards.current(P[2]) == []
    assert P[1] in fb.waiting_cards and P[2] not in fb.waiting_cards
    feed(fb, pm(P[1], image=True))
    assert [c["name"] for c in fb.cards.current(P[1])] == ["代存卡", "自存卡"]


@pytest.mark.parametrize("image", [True, False])
@pytest.mark.parametrize("permission", ["member", "outsider", "small_group_admin", "ledger_admin", "no_main_group"])
def test_non_main_admin_cannot_save_others_card(fb, image, permission):
    if permission == "outsider":
        fb.outsiders.add((100, P[2]))
    elif permission == "small_group_admin":
        fb.roles[(200, P[2])] = "admin"
    elif permission == "ledger_admin":
        fb.settings.update({"ledger_admins": [P[2]]}, "test")
    elif permission == "no_main_group":
        fb.settings.update({"main_group": None}, "test")
    feed(fb, pm(P[2], f".存卡 {P[1]} 调查员A", image=image))
    assert "只有大群群主或管理员可以代存" in fb.sent[-1][2]
    assert P[2] not in fb.waiting_cards
    feed(fb, pm(P[2], image=True))
    assert fb.cards.current(P[1]) == fb.cards.current(P[2]) == []


def test_admin_permission_rechecked_when_image_arrives(fb):
    fb.roles[(100, P[2])] = "admin"
    feed(fb, pm(P[2], f".存卡 {P[1]} 调查员A"))
    assert P[2] in fb.waiting_cards
    fb.roles[(100, P[2])] = "member"
    feed(fb, pm(P[2], image=True))
    assert "只有大群群主或管理员可以代存" in fb.sent[-1][2]
    assert fb.cards.current(P[1]) == []
    assert P[2] not in fb.waiting_cards


def test_proxy_upload_timeout_and_replaced_request(fb):
    fb.roles[(100, P[2])] = "admin"
    feed(fb, pm(P[2], f".存卡 {P[1]} 调查员A"))
    fb.waiting_cards[P[2]] = (P[1], "调查员A", 0)
    feed(fb, pm(P[2], image=True))
    assert fb.cards.current(P[1]) == []
    feed(fb, pm(P[2], f".存卡 {P[1]} 调查员A"), pm(P[2], ".存卡 自己的卡"), pm(P[2], image=True))
    assert fb.cards.current(P[1]) == []
    assert fb.cards.find(P[2], "自己的卡") is not None


def test_proxy_save_failure_replies_to_operator(fb):
    fb.roles[(100, P[2])] = "admin"
    def broken(url):
        raise OSError("断网")
    fb.http_get = broken
    feed(fb, pm(P[2], f".存卡 {P[1]} 调查员A", image=True))
    assert fb.sent[-1] == ("private", P[2], "人物卡「调查员A」的图片没能保存，请重新发送一次。")
    assert fb.cards.current(P[1]) == fb.cards.current(P[2]) == []


def test_proxy_save_permission_lookup_failure(fb):
    from sidecar.onebot import ActionError

    async def broken(action, **params):
        if action == "get_group_member_info":
            raise ActionError("查询失败")
        return await orig(action, **params)
    orig, fb.onebot.call = fb.onebot.call, broken
    feed(fb, pm(P[2], f".存卡 {P[1]} 调查员A", image=True))
    assert "只有大群群主或管理员可以代存" in fb.sent[-1][2]
    assert fb.cards.current(P[1]) == []


def test_explicit_self_owner_and_numeric_card_name(fb):
    feed(fb, pm(P[1], f".存卡 {P[1]} 调查员A", image=True), pm(P[1], ".存卡 123456", image=True))
    assert [c["name"] for c in fb.cards.current(P[1])] == ["调查员A", "123456"]


def test_card_commands_in_group_redirect(fb):
    from test_ledger import msg
    feed(fb, msg(100, P[1], ".取卡"))
    assert fb.sent[-1][2] == "人物卡指令请私聊骰娘使用。"


# ---------- 存卡通知 ----------
@pytest.fixture
def announcing_bot(fb):
    fb.settings.update({"card_announce_group": 779254872}, "test")
    return fb


@pytest.mark.parametrize("separate_image", [False, True])
def test_save_announces_only_after_image_saved(announcing_bot, separate_image):
    bot = announcing_bot
    feed(bot, pm(P[1], ".存卡 调查员A", image=not separate_image))
    if separate_image:
        assert not any(kind == "group" for kind, _, _ in bot.sent)
        feed(bot, pm(P[1], image=True))
    assert bot.cards.file(bot.cards.current(P[1])[0]).read_bytes() == PNG
    assert bot.sent[-2:] == [
        ("private", P[1], "已存档人物卡「调查员A」。"),
        ("group", 779254872, f"【存卡通知】QQ {P[1]} 已存档人物卡「调查员A」。\n"
         f"管理可私聊骰娘发送 .取卡 {P[1]} 调查员A 查看。"),
    ]
    assert not any(action == "_send_group_notice" for action, _ in bot.calls)


def test_same_name_update_announces_again(announcing_bot):
    bot = announcing_bot
    feed(bot, pm(P[1], ".存卡 调查员A", image=True), pm(P[1], ".存卡 调查员A", image=True))
    assert len([s for s in bot.sent if s[0] == "group"]) == 2
    assert bot.conn.execute("SELECT count(*) FROM cards").fetchone()[0] == 2
    assert len(bot.cards.current(P[1])) == 1


@pytest.mark.parametrize("failure", ["download", "storage"])
def test_failed_save_does_not_announce(announcing_bot, monkeypatch, failure):
    bot = announcing_bot

    def broken(*args, **kwargs):
        raise OSError("磁盘或网络故障")

    if failure == "download":
        bot.http_get = broken
    else:
        monkeypatch.setattr(bot.cards, "save", broken)
    feed(bot, pm(P[1], ".存卡 调查员A", image=True))
    assert bot.sent == [("private", P[1], "人物卡「调查员A」的图片没能保存，请重新发送一次。")]
    assert bot.cards.current(P[1]) == []


def test_invalid_or_expired_save_does_not_announce(announcing_bot):
    bot = announcing_bot
    from test_ledger import msg
    feed(bot, pm(P[1], ".存卡", image=True), msg(100, P[1], ".存卡 调查员A"),
         pm(P[1], ".存卡 调查员A"))
    bot.waiting_cards[P[1]] = (P[1], "调查员A", 0)
    feed(bot, pm(P[1], image=True), pm(P[2], image=True))
    assert not any(kind == "group" and gid == 779254872 for kind, gid, _ in bot.sent)
    assert bot.cards.current(P[1]) == []


@pytest.mark.parametrize("separate_image", [False, True])
def test_proxy_save_announces_card_owner(announcing_bot, separate_image):
    bot = announcing_bot
    bot.roles[(100, P[2])] = "admin"
    feed(bot, pm(P[2], f".存卡 {P[1]} 代存卡", image=not separate_image))
    if separate_image:
        assert not any(kind == "group" for kind, _, _ in bot.sent)
        feed(bot, pm(P[2], image=True))
    assert bot.cards.find(P[1], "代存卡") is not None
    assert bot.cards.current(P[2]) == []
    assert bot.sent[-1] == ("group", 779254872,
                            f"【存卡通知】QQ {P[1]} 已存档人物卡「代存卡」。\n"
                            f"管理可私聊骰娘发送 .取卡 {P[1]} 代存卡 查看。")


def test_denied_proxy_save_does_not_announce(announcing_bot):
    bot = announcing_bot
    feed(bot, pm(P[2], f".存卡 {P[1]} 代存卡", image=True))
    assert not any(kind == "group" for kind, _, _ in bot.sent)
    assert bot.cards.current(P[1]) == []


def test_announcement_target_changes_and_can_be_disabled(announcing_bot):
    bot = announcing_bot
    bot.settings.update({"card_announce_group": 100}, "test")
    feed(bot, pm(P[1], ".存卡 调查员A", image=True))
    assert [gid for kind, gid, _ in bot.sent if kind == "group"] == [100]
    bot.sent.clear()
    bot.settings.update({"card_announce_group": None}, "test")
    feed(bot, pm(P[1], ".存卡 调查员B", image=True))
    assert bot.sent == [("private", P[1], "已存档人物卡「调查员B」。")]


def test_announcement_failure_keeps_saved_card(announcing_bot, caplog):
    bot = announcing_bot
    original = bot.onebot.call

    async def call(action, **params):
        if action == "send_group_msg":
            raise ActionError("群消息发送失败")
        return await original(action, **params)

    bot.onebot.call = call
    feed(bot, pm(P[1], ".存卡 调查员A", image=True))
    assert bot.sent == [("private", P[1], "已存档人物卡「调查员A」。")]
    assert "card.announce" in caplog.text and "779254872" in caplog.text
    feed(bot, pm(P[1], ".取卡 调查员A"))
    assert bot.sent[-1] == ("private", P[1], "[image]")


def test_web_announcement_settings_and_template_apply_immediately(tmp_path, announcing_bot):
    bot = announcing_bot
    cfg = Config("ws://x", "", ["127.0.0.1"], 0, hash_password("pw"), tmp_path)
    client = TestClient(create_app(cfg, bot))
    client.post("/login", data={"password": "pw"})
    assert "存卡通知群" in client.get("/settings").text
    assert "存卡群通知" in client.get("/texts").text
    values = {"main_group": "12345", "card_announce_group": "123456789", "deadline_months": "2",
              "rr_timeout_minutes": "15"}
    response = client.post("/settings", data={**values, "card_announce_group": "abc"})
    assert "不是有效的号码" in response.text
    assert bot.settings.get("card_announce_group") == 779254872
    client.post("/settings", data=values)
    client.post("/texts/card.announce/save", data={"scope": "123456789", "variant": ["新卡：{qq} / {name}"]})
    feed(bot, pm(P[1], ".存卡 调查员 A", image=True))
    assert bot.sent[-1] == ("group", 123456789, f"新卡：{P[1]} / 调查员 A")
    bot.sent.clear()
    client.post("/texts/card.announce/save", data={"scope": "123456789", "variant": [""]})
    feed(bot, pm(P[1], ".存卡 调查员B", image=True))
    assert bot.sent == [("private", P[1], "已存档人物卡「调查员B」。")]
    client.post("/settings", data={**values, "card_announce_group": ""})
    assert bot.settings.get("card_announce_group") is None


def test_web_cards_page(tmp_path, fb):
    cfg = Config("ws://x", "", ["127.0.0.1"], 0, hash_password("pw"), tmp_path)
    client = TestClient(create_app(cfg, fb))
    client.post("/login", data={"password": "pw"})
    feed(fb, pm(P[1], ".存卡 调查员A", image=True))
    card = fb.cards.current(P[1])[0]
    assert "调查员A" in client.get("/cards").text
    assert client.get(f"/cards/{card['id']}/image").content == PNG
    client.post(f"/cards/{card['id']}/delete")
    assert fb.cards.current(P[1]) == [] and "已删除" in client.get("/cards").text
    client.post(f"/cards/{card['id']}/restore")
    assert [c["name"] for c in fb.cards.current(P[1])] == ["调查员A"]
    assert client.get(f"/cards/{card['id']}/image").content == PNG
