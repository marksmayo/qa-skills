from fastapi import Depends, FastAPI

from app.auth.dependencies import get_current_user, require_admin
from app.routers import bookings, manifests, public
from .routers.uploads import router as uploads_router

app = FastAPI(title="Ferry Ops API")

app.include_router(public.router)
app.include_router(bookings.router, prefix="/v1")
app.include_router(
    manifests.router,
    prefix="/v1",
    dependencies=[Depends(get_current_user), Depends(require_admin)],
)
app.include_router(uploads_router, prefix="/v1")


@app.get("/healthz")
def healthz():
    return {"ok": True}
