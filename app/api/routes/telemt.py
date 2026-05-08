from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response

from app.api.auth import current_user, login_redirect, require_user_api
from app.api.deps import templates
from app.schemas.telemt import TelemtSnapshot
from app.services.telemt import TelemtUnavailable, get_telemt_snapshot

router = APIRouter(tags=["telemt"])


@router.get("/telemt", response_class=HTMLResponse)
async def telemt_page(request: Request) -> Response:
    if current_user(request) is None:
        return login_redirect(request)
    try:
        snapshot: TelemtSnapshot | None = await get_telemt_snapshot()
        error: str | None = None
    except TelemtUnavailable as e:
        snapshot = None
        error = str(e)
    return templates.TemplateResponse(
        request,
        "telemt.html",
        {"snapshot": snapshot, "error": error},
    )


@router.get("/api/telemt", response_model=TelemtSnapshot)
async def telemt_json(_user: dict = Depends(require_user_api)) -> TelemtSnapshot:
    try:
        return await get_telemt_snapshot()
    except TelemtUnavailable as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"telemt metrics unavailable: {e}",
        ) from e
