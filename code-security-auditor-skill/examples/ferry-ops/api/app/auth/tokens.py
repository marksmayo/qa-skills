def decode_bearer(token: str) -> dict:
    """Stand-in for JWT validation (signature, issuer, audience, expiry)."""
    if not token:
        raise ValueError("missing token")
    return {"sub": "user", "roles": []}
