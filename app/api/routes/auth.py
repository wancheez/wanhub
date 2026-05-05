"""Web auth flow: register, login, logout.

Registration is two-step: user submits the form, we store a `pending` row
and ping the admin in Telegram. After the admin presses ✅ in DM, the
account becomes `approved` and the user can log in.
"""

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.api.auth import clear_session, current_user, set_session
from app.api.deps import templates
from app.bot.notify import notify_admin_of_web_registration
from app.services import web_users

router = APIRouter(tags=["auth"])
log = logging.getLogger("app")


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request) -> Response:
    if current_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "register.html", {"error": None, "username": ""})


@router.post("/register", response_class=HTMLResponse)
async def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
) -> Response:
    if password != password_confirm:
        return templates.TemplateResponse(
            request,
            "register.html",
            {"error": "Пароли не совпадают", "username": username},
            status_code=400,
        )
    try:
        user_id = web_users.register(username, password)
    except web_users.UsernameTaken:
        return templates.TemplateResponse(
            request,
            "register.html",
            {"error": "Это имя уже занято", "username": username},
            status_code=400,
        )
    except ValueError as e:
        return templates.TemplateResponse(
            request,
            "register.html",
            {"error": str(e), "username": username},
            status_code=400,
        )

    # Best-effort notify admin via Telegram. If the bot isn't running or
    # admin id is unset, registration still succeeds — admin can approve
    # later via DB or future admin UI.
    try:
        await notify_admin_of_web_registration(user_id, username.strip())
    except Exception:
        log.exception("notify_admin_of_web_registration failed")

    return templates.TemplateResponse(
        request,
        "register.html",
        {"error": None, "username": "", "submitted": True},
    )


def _safe_next(value: str | None) -> str:
    """Only allow same-origin paths as redirect targets."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str | None = None) -> Response:
    if current_user(request) is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": None, "username": "", "next": _safe_next(next)},
    )


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
) -> Response:
    target = _safe_next(next)
    try:
        user = web_users.authenticate(username, password)
    except web_users.InvalidCredentials as e:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": str(e), "username": username, "next": target},
            status_code=401,
        )
    set_session(request, user["id"])
    return RedirectResponse(target, status_code=303)


@router.post("/logout")
def logout_submit(request: Request) -> Response:
    clear_session(request)
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout_get(request: Request) -> Response:
    """Convenience: GET /logout works too (link from navbar)."""
    clear_session(request)
    return RedirectResponse("/", status_code=303)
