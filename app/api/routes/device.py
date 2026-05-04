from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.api.deps import templates
from app.schemas.device import DeviceInfo
from app.services.device import get_device_info

router = APIRouter(tags=["device"])


@router.get("/device", response_class=HTMLResponse)
def device_page(request: Request):
    return templates.TemplateResponse(
        request,
        "device.html",
        {"device": get_device_info()},
    )


@router.get("/api/device", response_model=DeviceInfo)
def device_json() -> DeviceInfo:
    return get_device_info()
