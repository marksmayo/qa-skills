from fastapi import APIRouter


class FerryRouter(APIRouter):
    """APIRouter with the project's default responses."""

    def __init__(self, **kwargs):
        kwargs.setdefault("responses", {404: {"description": "Not found"}})
        super().__init__(**kwargs)
