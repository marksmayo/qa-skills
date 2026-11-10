import os

from flask import Blueprint, abort, current_app, request, send_file

from .auth import login_required

bp = Blueprint("attachments", __name__, url_prefix="/attachments")


@bp.get("")
@login_required
def download():
    name = request.args.get("name", "")
    path = os.path.join(current_app.config["UPLOAD_DIR"], name)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path)
