from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.auth.dependencies import get_current_user
from app.db import get_session

router = APIRouter(prefix="/public")
CurrentUser = Annotated[dict, Depends(get_current_user)]


@router.get(
    "/sailings",
)
async def list_sailings(session=Depends(get_session)):
    return session.execute(text("SELECT id, route FROM sailings")).all()


@router.get("/sailings/search")
async def search_sailings(port: str, session=Depends(get_session)):
    # Planted: the port name is pasted into the SQL text.
    return session.execute(text(f"SELECT id, route FROM sailings WHERE port = '{port}'")).all()


@router.get("/me")
async def me(user: CurrentUser):
    return user
