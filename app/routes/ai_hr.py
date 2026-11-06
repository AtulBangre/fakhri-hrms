"""Ask HR: the in-app assistant for employees and HR. See ``services/assistant.py``."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..auth import SessionUser, current_user
from ..deps import get_settings_dep, get_storage
from ..services import assistant

router = APIRouter(prefix="/api/ai-hr", tags=["ai-hr"])


class QueryRequest(BaseModel):
    query: str


@router.post("/query")
def ask_hr(
    payload: QueryRequest,
    user: SessionUser | None = Depends(current_user),
    storage=Depends(get_storage),
    settings=Depends(get_settings_dep),
):
    if not user:
        return {"answer": "Please log in to ask questions.", "suggestions": [], "actions": []}
    return assistant.answer(storage, settings, user, payload.query)
