import pytest
from fastapi.testclient import TestClient

from sidecar import db
from sidecar import settings as settings_mod
from sidecar import templates as tpl
from sidecar.bot import Bot, parse_command, plain_text
from sidecar.config import Config, hash_password, verify_password
from sidecar.web import create_app


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


# ---------- 模板 ----------
def test_validate_rejects_unknown_variable():
    with pytest.raises(tpl.TemplateError, match=r"\{nick\}"):
        tpl.validate(tpl.BY_KEY["welcome.default"], ["欢迎 {nick}"])


def test_validate_reports_which_variant():
    with pytest.raises(tpl.TemplateError, match="第 2 条"):
        tpl.validate(tpl.BY_KEY["rr.click"], ["{name}", "{oops}"])


@pytest.mark.parametrize("bad", ["欢迎 {at", "{}", "{at!r}", "{at:>5}"])
def test_validate_rejects_bad_syntax(bad):
    with pytest.raises(tpl.TemplateError):
        tpl.validate(tpl.BY_KEY["welcome.default"], [bad])


def test_literal_braces_allowed():
    tpl.validate(tpl.BY_KEY["welcome.default"], ["{{不是变量}} {at}"])
    assert tpl.preview(tpl.BY_KEY["welcome.default"], "{{x}}") == "{x}"


def test_render_at_segment(conn):
    t = tpl.Templates(conn)
    segs = t.render("welcome.default", "", at=tpl.at(123), nickname="n", group_name="G")
    assert segs == [
        {"type": "text", "data": {"text": "欢迎 "}},
        {"type": "at", "data": {"qq": "123", "name": ""}},
        {"type": "text", "data": {"text": " 来到G。"}},
    ]


def test_save_variants_and_random_pick(conn):
    t = tpl.Templates(conn)
    t.save("rr.click", ["A {name}", "  ", "B {name}"], "web")
    assert t.variants("rr.click") == ["A {name}", "B {name}"]
    seen = {t.render_text("rr.click", name="x", left=1, next="y") for _ in range(50)}
    assert seen == {"A x", "B x"}


def test_save_rejects_and_keeps_old(conn):
    t = tpl.Templates(conn)
    with pytest.raises(tpl.TemplateError):
        t.save("card.saved", ["{who}"], "web")
    assert t.variants("card.saved") == tpl.BY_KEY["card.saved"].default


def test_empty_template_means_no_send(conn):
    assert tpl.Templates(conn).render("invite.hello", group_name="G") is None


def test_per_group_fallback_and_reset(conn):
    t = tpl.Templates(conn)
    t.save("welcome.default", ["群A {at}"], "web", scope="111")
    assert t.variants("welcome.default", "111") == ["群A {at}"]
    assert t.variants("welcome.default", "222") == tpl.BY_KEY["welcome.default"].default
    t.reset("welcome.default", "web", scope="111")
    assert t.variants("welcome.default", "111") == tpl.BY_KEY["welcome.default"].default
    assert conn.execute("SELECT count(*) FROM op_log").fetchone()[0] == 2


def test_scope_ignored_for_non_group_template(conn):
    t = tpl.Templates(conn)
    with pytest.raises(tpl.TemplateError):
        t.save("rr.bang", ["x"], "web", scope="111")


# ---------- 配置 ----------
def test_settings_parse_and_defaults(conn):
    s = settings_mod.Settings(conn)
    assert s.get("deadline_months") == 2
    assert s.repeat_groups == []
    d = settings_mod.BY_KEY
    s.update({"main_group": 12345, "ignore_qq": settings_mod.parse(d["ignore_qq"], "22222，33333\n22222")}, "web")
    assert s.get("ignore_qq") == [22222, 33333]
    assert s.repeat_groups == [12345]
    with pytest.raises(ValueError, match="abc"):
        settings_mod.parse(d["ledger_admins"], "12345 abc")
    with pytest.raises(ValueError):
        settings_mod.parse(d["deadline_months"], "0")


# ---------- 指令识别 ----------
def test_parse_command():
    assert parse_command(".存卡 调查员A", ("存卡",)) == ("存卡", "调查员A")
    assert parse_command("。取卡1", ("取卡",)) == ("取卡", "1")
    assert parse_command("存卡", ("存卡",)) is None
    assert parse_command(".r 1d100", ("存卡",)) is None


def test_plain_text():
    assert plain_text([{"type": "text", "data": {"text": "草"}}]) == "草"
    assert plain_text([{"type": "image", "data": {}}]) is None


# ---------- 密码与后台 ----------
def test_password_hash():
    h = hash_password("pw")
    assert verify_password("pw", h) and not verify_password("px", h)


def test_web_login_and_save(tmp_path):
    cfg = Config("ws://x", "", ["127.0.0.1"], 0, hash_password("pw"), tmp_path)
    bot = Bot(db.connect(tmp_path / "t.db"), "ws://x", "")
    client = TestClient(create_app(cfg, bot))
    assert client.get("/texts", follow_redirects=False).headers["location"] == "/login"
    client.post("/login", data={"password": "bad"})
    assert client.get("/texts", follow_redirects=False).status_code == 303
    client.post("/login", data={"password": "pw"})
    assert "请私聊使用" in client.get("/texts").text
    r = client.post("/texts/card.private_only/save", data={"scope": "", "variant": ["新文案"]})
    assert r.status_code == 200
    assert bot.templates.variants("card.private_only") == ["新文案"]
    r = client.post("/texts/card.saved/save", data={"scope": "", "variant": ["{bad}"]})
    assert "未定义的变量" in r.text
    assert client.post("/texts/preview", json={"key": "card.saved", "text": "存了{name}"}).json() == {"preview": "存了Flame"}
    r = client.post("/settings", data={"main_group": "12345", "deadline_months": "x"})
    assert "需要填整数" in r.text and bot.settings.get("main_group") is None
