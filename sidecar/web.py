"""网页管理后台：服务端渲染，保存后立即生效。"""
import asyncio
import csv
import io
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import ledger
from . import settings as settings_mod
from . import templates as tpl
from .bot import Bot
from .config import Config, session_secret, verify_password

pages = Jinja2Templates(directory=Path(__file__).parent / "pages")
pages.env.filters["datetime"] = lambda ts: datetime.fromtimestamp(ts, ledger.BEIJING).strftime("%Y-%m-%d %H:%M")


def flash(request: Request, text: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, text])


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx["flashes"] = request.session.pop("flash", [])
    ctx["path"] = request.url.path
    return pages.TemplateResponse(request, name, ctx)


def create_app(cfg: Config, bot: Bot) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        if request.url.path != "/login" and not request.session.get("auth"):
            if request.method == "GET":
                return RedirectResponse("/login", status_code=303)
            return JSONResponse({"error": "未登录"}, status_code=401)
        return await call_next(request)

    # 后加的中间件在外层，会话要先于登录检查解析出来
    app.add_middleware(
        SessionMiddleware, secret_key=session_secret(cfg.data_dir), session_cookie="sidecar_session",
        max_age=30 * 24 * 3600, same_site="lax", https_only=False,
    )

    # ---------- 登录 ----------
    @app.get("/login")
    async def login_page(request: Request):
        return render(request, "login.html", no_password=not cfg.admin_password_hash)

    @app.post("/login")
    async def login(request: Request, password: str = Form("")):
        if cfg.admin_password_hash and verify_password(password, cfg.admin_password_hash):
            request.session.clear()
            request.session["auth"] = True
            return RedirectResponse("/", status_code=303)
        await asyncio.sleep(1)  # 放慢暴力尝试
        flash(request, "密码不对", "err")
        return RedirectResponse("/login", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/")
    async def index():
        return RedirectResponse("/texts", status_code=303)

    # ---------- 文案 ----------
    def text_items():
        groups = []
        for group in tpl.GROUPS:
            items = []
            for d in (x for x in tpl.CATALOG if x.group == group):
                scopes = [""] + (bot.templates.scopes(d.key) if d.per_group else [])
                editors = []
                for scope in scopes:
                    variants = bot.templates.variants(d.key, scope)
                    editors.append({
                        "scope": scope,
                        "variants": variants,
                        "previews": [tpl.preview(d, v) for v in variants],
                        "customized": bot.templates.is_customized(d.key, scope),
                    })
                items.append({"defn": d, "editors": editors})
            groups.append((group, items))
        return groups

    @app.get("/texts")
    async def texts_page(request: Request):
        return render(request, "texts.html", groups=text_items())

    @app.post("/texts/{key}/save")
    async def texts_save(request: Request, key: str):
        form = await request.form()
        scope = str(form.get("scope", ""))
        try:
            bot.templates.save(key, [str(v) for v in form.getlist("variant")], "web", scope)
            flash(request, f"已保存「{tpl.BY_KEY[key].title}」{_scope_label(scope)}")
        except tpl.TemplateError as e:
            flash(request, f"「{tpl.BY_KEY[key].title}」{_scope_label(scope)}没有保存：{e}", "err")
        return RedirectResponse(f"/texts#{key}", status_code=303)

    @app.post("/texts/{key}/reset")
    async def texts_reset(request: Request, key: str, scope: str = Form("")):
        bot.templates.reset(key, "web", scope)
        flash(request, f"「{tpl.BY_KEY[key].title}」{_scope_label(scope)}" + ("已删除单独设置" if scope else "已恢复默认"))
        return RedirectResponse(f"/texts#{key}", status_code=303)

    @app.post("/texts/{key}/add-group")
    async def texts_add_group(request: Request, key: str, group_id: str = Form("")):
        group_id = group_id.strip()
        if not group_id.isdigit():
            flash(request, "请填写群号", "err")
        elif bot.templates.is_customized(key, group_id):
            flash(request, f"群 {group_id} 已经有单独设置了", "err")
        else:
            bot.templates.save(key, bot.templates.variants(key), "web", group_id)
            flash(request, f"已为群 {group_id} 添加单独设置，内容先复制自默认文案")
        return RedirectResponse(f"/texts#{key}", status_code=303)

    @app.post("/texts/preview")
    async def texts_preview(request: Request):
        body = await request.json()
        defn = tpl.BY_KEY.get(body.get("key"))
        if defn is None:
            return JSONResponse({"error": "未知文案"}, status_code=404)
        text = str(body.get("text", ""))
        try:
            tpl.validate(defn, [text])
        except tpl.TemplateError as e:
            return {"error": str(e)}
        return {"preview": tpl.preview(defn, text)}

    # ---------- 配置 ----------
    @app.get("/settings")
    async def settings_page(request: Request):
        values = bot.settings.all()
        fields = [{"defn": d, "text": settings_mod.format_value(d, values[d.key]), "error": ""} for d in settings_mod.SETTINGS]
        return render(request, "settings.html", fields=fields)

    @app.post("/settings")
    async def settings_save(request: Request):
        form = await request.form()
        parsed, fields, has_error = {}, [], False
        for d in settings_mod.SETTINGS:
            text = str(form.get(d.key, ""))
            error = ""
            try:
                parsed[d.key] = settings_mod.parse(d, text)
            except ValueError as e:
                error, has_error = str(e), True
            fields.append({"defn": d, "text": text, "error": error})
        if has_error:
            request.session.setdefault("flash", []).append(["err", "有填写错误，没有保存，请看下方标红的项"])
            return render(request, "settings.html", fields=fields)
        bot.settings.update(parsed, "web")
        flash(request, "配置已保存")
        return RedirectResponse("/settings", status_code=303)

    # ---------- 团账本 ----------
    pending: set[asyncio.Task] = set()

    def refresh_notice_later() -> None:
        """改完账本后在后台更新大群名单公告，不让网页等它。"""
        task = asyncio.create_task(bot.refresh_notice())
        pending.add(task)
        task.add_done_callback(pending.discard)

    @app.get("/ledger")
    async def ledger_page(request: Request):
        people: dict[int, dict] = {}
        for r in bot.ledger.records():
            people.setdefault(r["qq"], {"qq": r["qq"], "name": r["name"], "records": []})["records"].append(r)
        return render(request, "ledger.html", people=list(people.values()), today=ledger.today().isoformat(),
                      sources=ledger.SOURCES)

    @app.post("/ledger/add")
    async def ledger_add(request: Request, qq: str = Form(""), name: str = Form(""), deadline: str = Form("")):
        qq, name = qq.strip(), name.strip()
        if not qq.isdigit() or not name:
            flash(request, "请填写 QQ 号和名字", "err")
        else:
            d = _date(deadline)
            person, _ = bot.ledger.add(int(qq), name, "manual", "web", bot.settings.get("deadline_months"), deadline=d)
            flash(request, f"已记团：{person.entry}")
            refresh_notice_later()
        return RedirectResponse("/ledger", status_code=303)

    @app.post("/ledger/{record_id}/delete")
    async def ledger_delete(request: Request, record_id: int):
        bot.ledger.delete_record(record_id, "web")
        flash(request, "已删除这条记录")
        refresh_notice_later()
        return RedirectResponse("/ledger", status_code=303)

    @app.post("/ledger/{record_id}/deadline")
    async def ledger_deadline(request: Request, record_id: int, deadline: str = Form("")):
        d = _date(deadline)
        if d is None:
            flash(request, "日期格式不对", "err")
        else:
            bot.ledger.set_deadline(record_id, d, "web")
            flash(request, f"死线已改为 {ledger.md(d)}")
            refresh_notice_later()
        return RedirectResponse("/ledger", status_code=303)

    @app.post("/members/{qq}/rename")
    async def member_rename(request: Request, qq: int, name: str = Form("")):
        if name.strip():
            bot.ledger.rename(qq, name.strip(), "web")
            flash(request, f"已改名为 {name.strip()}")
            refresh_notice_later()
        return RedirectResponse("/ledger", status_code=303)

    @app.get("/ledger.csv")
    async def ledger_csv():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["名字", "QQ", "死线", "来源", "记团日期"])
        for r in bot.ledger.records():
            w.writerow([r["name"], r["qq"], r["deadline"], ledger.SOURCES.get(r["source"], r["source"]), r["recorded_on"] or ""])
        # 带 BOM，Excel 和腾讯文档直接打开不乱码
        return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=ledger-{ledger.today()}.csv"})

    @app.get("/ledger/import")
    async def import_page(request: Request):
        return render(request, "import.html", rows=None, text="")

    @app.post("/ledger/import")
    async def import_parse(request: Request, text: str = Form("")):
        ref, rows, errors = ledger.today(), [], []
        known = {r["name"]: r["qq"] for r in bot.conn.execute("SELECT name, qq FROM members")}
        for line in text.splitlines():
            if not line.strip().endswith(("）", ")")):
                continue  # 标题、说明、链接、分隔线等不是名单行
            try:
                name, deadlines = ledger.parse_list_line(line, ref)
            except ValueError as e:
                errors.append(str(e))
                continue
            rows.append({"name": name, "deadlines": [d.isoformat() for d in deadlines],
                         "shown": "，".join(ledger.md(d) for d in deadlines), "qq": known.get(name, "")})
        for e in errors:
            flash(request, f"跳过：{e}", "err")
        return render(request, "import.html", rows=rows, text=text)

    @app.post("/ledger/import/confirm")
    async def import_confirm(request: Request):
        form = await request.form()
        names, qqs, dls = form.getlist("name"), form.getlist("qq"), form.getlist("deadlines")
        bad = [n for n, q in zip(names, qqs) if not str(q).strip().isdigit()]
        if bad:
            flash(request, f"这些人还没填 QQ 号，没有导入：{'、'.join(bad)}", "err")
            return RedirectResponse("/ledger/import", status_code=303)
        items = [(int(q), str(n), [date.fromisoformat(d) for d in str(dl).split(",")])
                 for n, q, dl in zip(names, qqs, dls)]
        n = bot.ledger.import_people(items, "web")
        flash(request, f"已导入 {len(items)} 人、{n} 条记录")
        refresh_notice_later()
        return RedirectResponse("/ledger", status_code=303)

    # ---------- 人物卡 ----------
    @app.get("/cards")
    async def cards_page(request: Request):
        people: dict[int, dict] = {}
        for c in bot.cards.all():
            people.setdefault(c["qq"], {"qq": c["qq"], "name": c["member"] or "", "cards": []})["cards"].append(c)
        return render(request, "cards.html", people=list(people.values()))

    @app.get("/cards/{card_id}/image")
    async def card_image(card_id: int):
        card = bot.cards.get(card_id)
        if card is None or not bot.cards.file(card).is_file():
            return Response(status_code=404)
        return FileResponse(bot.cards.file(card))

    @app.post("/cards/{card_id}/delete")
    async def card_delete(request: Request, card_id: int):
        bot.cards.delete_id(card_id, "web")
        flash(request, "已删除这张卡，可以随时恢复")
        return RedirectResponse(f"/cards#card-{card_id}", status_code=303)

    @app.post("/cards/{card_id}/restore")
    async def card_restore(request: Request, card_id: int):
        bot.cards.restore_id(card_id, "web")
        flash(request, "已恢复这张卡")
        return RedirectResponse(f"/cards#card-{card_id}", status_code=303)

    return app


def _date(text: str):
    try:
        return date.fromisoformat(text.strip()) if text.strip() else None
    except ValueError:
        return None


def _scope_label(scope: str) -> str:
    return f"（群 {scope}）" if scope else ""
