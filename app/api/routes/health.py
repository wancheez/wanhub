from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/ping")
def ping():
    return {"status": "ok", "message": "pong"}
