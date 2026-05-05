"""Web chat endpoints. All gated behind the auth dependency."""

import logging

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from app.api.auth import current_user, login_redirect, require_user_api
from app.api.deps import templates
from app.bot.skills.send_image import extract_image_intent
from app.services import web_chat, web_chat_history
from app.services.image_query import rewrite_query
from app.services.image_search import find_image_urls

router = APIRouter(tags=["chat"])
log = logging.getLogger("app")


@router.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request) -> Response:
    if current_user(request) is None:
        return login_redirect(request)
    return templates.TemplateResponse(request, "chat.html", {})


class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


class ChatOut(BaseModel):
    reply: str
    image_url: str | None = None


class HistoryItem(BaseModel):
    role: str
    content: str


@router.get("/api/chat/history", response_model=list[HistoryItem])
def get_history(user: dict = Depends(require_user_api)) -> list[HistoryItem]:
    return [HistoryItem(**m) for m in web_chat_history.load_history(user["id"], limit=200)]


@router.post("/api/chat", response_model=ChatOut)
async def post_chat(payload: ChatIn, user: dict = Depends(require_user_api)) -> ChatOut:
    # Local skill: «пришли фото X» short-circuits the LLM and returns an image.
    intent = extract_image_intent(payload.message)
    if intent is not None:
        return await _handle_image_intent(user, payload.message, intent)

    try:
        reply = await web_chat.web_chat(user["id"], payload.message, user_name=user["username"])
    except anthropic.AuthenticationError as e:
        raise HTTPException(status_code=503, detail="Anthropic API key missing or invalid") from e
    except anthropic.APIError as e:
        log.exception("Anthropic API error in web chat")
        raise HTTPException(status_code=503, detail=f"Anthropic API error: {e.message}") from e
    return ChatOut(reply=reply or "(пустой ответ)")


async def _handle_image_intent(user: dict, message: str, intent: dict) -> ChatOut:
    """DDG image search → returns first URL. Frontend renders <img>."""
    query = await rewrite_query(intent["raw"], fallback=intent["fallback"])
    log.info("web image search: %r → %r", intent["raw"], query)
    urls = await find_image_urls(query, limit=5)
    web_chat_history.append_message(user["id"], "user", message)
    if not urls:
        reply = f"Не нашёл картинок по «{query}»."
        web_chat_history.append_message(user["id"], "assistant", reply)
        return ChatOut(reply=reply)
    image_url = urls[0]
    # Persist a marker so future Claude turns know an image was sent.
    web_chat_history.append_message(
        user["id"], "assistant", f"[отправил картинку «{query}»: {image_url}]"
    )
    return ChatOut(reply=f"Картинка по запросу: {query}", image_url=image_url)


@router.post("/api/chat/reset")
def reset_chat(user: dict = Depends(require_user_api)) -> dict:
    n = web_chat.reset_web_chat(user["id"])
    return {"deleted": n}
