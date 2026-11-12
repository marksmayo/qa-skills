from fastapi import APIRouter

# No dependencies here: main.py adds get_current_user and require_admin in include_router().
router = APIRouter(prefix="/manifests", tags=["manifests"])


@router.delete("/{manifest_id}")
async def delete_manifest(manifest_id: int):
    return {"deleted": manifest_id}
