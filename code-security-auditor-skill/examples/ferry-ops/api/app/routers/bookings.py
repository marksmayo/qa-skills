from fastapi import Depends

from app.auth.dependencies import check_booking_permission, get_current_user
from app.routing import FerryRouter

# A project's own APIRouter subclass: every route on it needs a signed-in user.
router = FerryRouter(
    prefix="/bookings",
    tags=["bookings"],
    dependencies=[Depends(get_current_user), Depends(check_booking_permission)],
)


@router.get(
    "/{booking_id}",
    summary="One booking, looked up by id (the lookup must also filter by the caller's operator)",
)
async def get_booking(booking_id: int):
    return {"id": booking_id}


@router.post("")
async def create_booking(payload: dict):
    return payload
