from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from app.api.auth import current_user, login_redirect, require_user_api
from app.api.deps import templates
from app.schemas.device import DeviceInfo
from app.services.device import get_device_info

router = APIRouter(tags=["device"])


@router.get("/device", response_class=HTMLResponse)
def device_page(request: Request) -> Response:
    if current_user(request) is None:
        return login_redirect(request)
    return templates.TemplateResponse(
        request,
        "device.html",
        {"device": get_device_info()},
    )


@router.get("/api/device", response_model=DeviceInfo)
def device_json(_user: dict = Depends(require_user_api)) -> DeviceInfo:
    return get_device_info()
