from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.api.deps import templates
from app.services.profile import get_profile

router = APIRouter(tags=["pages"])


@router.get("/", response_class=HTMLResponse)
def about_me(request: Request):
    return templates.TemplateResponse(
        request,
        "about.html",
        {"profile": get_profile()},
    )
