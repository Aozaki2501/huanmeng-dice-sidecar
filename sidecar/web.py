"""网页管理后台：服务端渲染，保存后立即生效。"""
import asyncio
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import settings as settings_mod
from . import templates as tpl
from .bot import Bot
from .config import Config, session_secret, verify_password

pages = Jinja2Templates(directory=Path(__file__).parent / "pages")


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

    return app


def _scope_label(scope: str) -> str:
    return f"（群 {scope}）" if scope else ""
