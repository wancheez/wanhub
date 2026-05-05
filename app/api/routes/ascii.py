import anthropic
from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import require_user_api
from app.schemas.ascii import AsciiArtResponse
from app.services.ascii import generate_ascii_art

router = APIRouter(tags=["ascii"])


@router.post("/api/ascii", response_model=AsciiArtResponse)
def post_ascii(_user: dict = Depends(require_user_api)) -> AsciiArtResponse:
    try:
        return generate_ascii_art()
    except anthropic.AuthenticationError as e:
        raise HTTPException(status_code=503, detail="Anthropic API key missing or invalid") from e
    except anthropic.APIError as e:
        raise HTTPException(status_code=503, detail=f"Anthropic API error: {e.message}") from e
