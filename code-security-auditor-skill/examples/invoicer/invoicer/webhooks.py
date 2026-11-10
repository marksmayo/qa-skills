import requests
from flask import Blueprint, abort, jsonify, request

from .auth import login_required

bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")


@bp.post("/test")
@login_required
def test_webhook():
    url = (request.get_json(silent=True) or {}).get("url")
    if not url or not url.startswith(("http://", "https://")):
        abort(400)
    resp = requests.get(url, timeout=5)
    return jsonify(status=resp.status_code, body=resp.text[:500])
