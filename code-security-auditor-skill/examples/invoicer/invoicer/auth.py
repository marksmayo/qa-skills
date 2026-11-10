import functools
import logging
import random
import string

from flask import Blueprint, abort, g, redirect, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db

bp = Blueprint("auth", __name__)
log = logging.getLogger(__name__)


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("user_id") is None:
            abort(401)
        g.user_id = session["user_id"]
        return view(*args, **kwargs)

    return wrapped


@bp.post("/login")
def login():
    email = request.form.get("email", "")
    password = request.form.get("password", "")
    log.info("login attempt email=%s password=%s", email, password)
    user = get_db().execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
    if user is None or not check_password_hash(user["password_hash"], password):
        abort(401)
    session.clear()
    session["user_id"] = user["id"]
    return redirect(request.args.get("next") or "/invoices")


@bp.post("/logout")
def logout():
    session.clear()
    return redirect("/")


@bp.post("/password-reset")
def password_reset():
    email = request.form.get("email", "")
    token = "".join(random.choices(string.ascii_letters + string.digits, k=8))
    db = get_db()
    db.execute("UPDATE users SET reset_token = ? WHERE email = ?", (token, email))
    db.commit()
    # The token is emailed to the user (not implemented in this example).
    return "", 204


@bp.post("/password-reset/confirm")
def password_reset_confirm():
    email = request.form.get("email", "")
    db = get_db()
    user = db.execute("SELECT id, reset_token FROM users WHERE email = ?", (email,)).fetchone()
    if user is None or not user["reset_token"] or request.form.get("token") != user["reset_token"]:
        abort(400)
    db.execute(
        "UPDATE users SET password_hash = ?, reset_token = NULL WHERE id = ?",
        (generate_password_hash(request.form["new_password"]), user["id"]),
    )
    db.commit()
    return "", 204
