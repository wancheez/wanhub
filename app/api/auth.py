"""FastAPI auth dependencies for web routes.

Session is a signed cookie via Starlette's SessionMiddleware (configured in
`app/main.py`). The cookie holds `user_id`; on every request we re-fetch
the user record so changes (deny/promote) take effect immediately.
"""

from urllib.parse import quote

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse

from app.services import web_users

SESSION_KEY = "user_id"


def set_session(request: Request, user_id: int) -> None:
    request.session[SESSION_KEY] = user_id


def clear_session(request: Request) -> None:
    request.session.pop(SESSION_KEY, None)


def current_user(request: Request) -> dict | None:
    """Return user dict if logged in AND still approved, else None."""
    user_id = request.session.get(SESSION_KEY)
    if not isinstance(user_id, int):
        return None
    user = web_users.get_by_id(user_id)
    if user is None or user["status"] != "approved":
        # User was denied or removed after login — drop the session.
        request.session.pop(SESSION_KEY, None)
        return None
    return user


def require_user_api(request: Request) -> dict:
    """Dependency for JSON API routes. Raises 401 on missing session."""
    user = current_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется вход",
        )
    return user


def login_redirect(request: Request) -> RedirectResponse:
    """Build a 303 redirect to /login with `?next=<current path>` so the user
    lands back on the page they wanted after logging in.
    """
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(f"/login?next={quote(target, safe='/?=&')}", status_code=303)
