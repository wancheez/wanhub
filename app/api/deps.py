from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.api.auth import current_user
from app.core.config import TEMPLATES_DIR


def _user_ctx(request: Request) -> dict:
    """Inject `current_user` into every template render so the navbar can
    decide what to show without each route passing it explicitly.
    """
    return {"current_user": current_user(request)}


templates = Jinja2Templates(directory=str(TEMPLATES_DIR), context_processors=[_user_ctx])
