"""逾期名单：粘贴导入、入大群提醒、后台页面。"""
import pytest
from fastapi.testclient import TestClient

from sidecar import db
from sidecar.config import Config, hash_password
from sidecar.overdue import parse_paste
from sidecar.web import create_app
from test_ledger import BOT_QQ, FakeBot, feed

# 从腾讯文档复制出来的样子：Tab 分隔，年份行是合并单元格
PASTED = "\n".join([
    "逾期名单\t\t\t",
    "昵称\t死线日期\tQQ号\t备注",
    "2020\t\t\t",
    "五\t(8/26，8/29，9/12)\t499806631\t",
    "兔头\t(9/28)\txiaoc233@yeah.net\t",
    "2021\t\t\t",
    "临江\t(1/16)\t156044747\t",
    "鸵鸟\t(5/23)\t8915974\t改过名",
])


def test_parse_paste():
    rows, skipped = parse_paste(PASTED)
    assert [(r.name, r.qq, r.deadlines, r.note) for r in rows] == [
        ("五", 499806631, "2020 年 8/26，8/29，9/12", ""),
        ("临江", 156044747, "2021 年 1/16", ""),
        ("鸵鸟", 8915974, "2021 年 5/23", "改过名"),
    ]
    assert skipped == ["兔头（QQ 号一栏是「xiaoc233@yeah.net」）"]


@pytest.fixture
def fb(tmp_path):
    bot = FakeBot(db.connect(tmp_path / "t.db"), tmp_path)
    bot.settings.update({"main_group": 100}, "test")
    bot.overdue.save(parse_paste(PASTED)[0], "test")
    return bot


def joined(group, user):
    return {"post_type": "notice", "notice_type": "group_increase", "group_id": group, "user_id": user}


def test_reminds_when_overdue_member_joins_main_group(fb):
    feed(fb, joined(100, 156044747))
    assert fb.sent[-2][2].startswith("[at]\n欢迎新佬")
    assert fb.sent[-1] == ("group", 100, "⚠️ 管理请注意：[at]（QQ 156044747）在逾期名单上，名单昵称「临江」，"
                                         "死线 2021 年 1/16，没还清团就退群了。")


def test_no_reminder_for_others_or_other_groups(fb):
    feed(fb, joined(100, 10001))
    assert len(fb.sent) == 1  # 只有欢迎
    fb.templates.save("welcome.default", ["小群欢迎 {at}"], "test", scope="200")
    feed(fb, joined(200, 156044747))
    assert fb.sent[-1][2] == "小群欢迎 [at]"


def test_reminder_can_be_turned_off(fb):
    fb.templates.save("overdue.joined", [""], "test")
    feed(fb, joined(100, 156044747))
    assert len(fb.sent) == 1


def test_web_overdue_page(tmp_path, fb):
    cfg = Config("ws://x", "", ["127.0.0.1"], 0, hash_password("pw"), tmp_path)
    client = TestClient(create_app(cfg, fb))
    client.post("/login", data={"password": "pw"})
    fb.replies["get_group_member_list"] = [{"user_id": 156044747}, {"user_id": BOT_QQ}]
    page = client.get("/overdue").text
    assert "临江" in page and "在群里" in page and "其中 1 人现在在大群里" in page
    r = client.post("/overdue/import", data={"text": "2022\t\t\n阿青\t(2/1)\t123456789\t"})
    assert "已导入 1 人" in r.text and fb.overdue.get(123456789)["deadlines"] == "2022 年 2/1"
    client.post("/overdue/add", data={"name": "兔头", "qq": "22334455", "deadlines": "2020 年 9/28", "note": "邮箱改 QQ"})
    assert fb.overdue.get(22334455)["note"] == "邮箱改 QQ"
    client.post("/overdue/156044747/delete")
    assert fb.overdue.get(156044747) is None
