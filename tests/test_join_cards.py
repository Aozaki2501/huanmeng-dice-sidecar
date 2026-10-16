"""第 5、6 阶段：入群欢迎、拉群校验、人物卡。"""
import pytest
from fastapi.testclient import TestClient

from sidecar import db
from sidecar.config import Config, hash_password
from sidecar.web import create_app
from test_ledger import BOT_QQ, P, FakeBot, feed

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20


@pytest.fixture
def fb(tmp_path):
    bot = FakeBot(db.connect(tmp_path / "t.db"), tmp_path)
    bot.settings.update({"main_group": 100}, "test")
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
    fb.waiting_cards[P[1]] = ("调查员A", 0)
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


def test_card_commands_in_group_redirect(fb):
    from test_ledger import msg
    feed(fb, msg(100, P[1], ".取卡"))
    assert fb.sent[-1][2] == "人物卡指令请私聊骰娘使用。"


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
