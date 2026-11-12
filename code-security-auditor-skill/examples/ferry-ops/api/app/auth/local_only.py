import os

APP_ENV = os.getenv("APP_ENV", "production")


def local_bypass_user(token: str) -> dict | None:
    """Safe counterpart: the static token works only when the app runs locally."""
    if APP_ENV == "local":
        local_token = os.getenv("LOCAL_DEV_TOKEN")
        if local_token and token == local_token:
            return {"sub": "local-user", "roles": []}
    return None
