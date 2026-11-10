import yaml
from flask import Blueprint, abort, g, jsonify, render_template_string, request

from .auth import login_required
from .db import get_db

bp = Blueprint("invoices", __name__, url_prefix="/invoices")
COLUMNS = "id, number, customer, amount, status"


def as_dict(row):
    return {key: row[key] for key in row.keys()}


@bp.get("")
@login_required
def list_invoices():
    sort = request.args.get("sort", "number")
    rows = get_db().execute(
        f"SELECT {COLUMNS} FROM invoices WHERE owner_id = ? ORDER BY {sort}", (g.user_id,)
    ).fetchall()
    return jsonify([as_dict(r) for r in rows])


@bp.get("/search")
@login_required
def search():
    q = request.args.get("q", "")
    rows = get_db().execute(
        f"SELECT {COLUMNS} FROM invoices WHERE owner_id = {g.user_id} AND customer LIKE '%{q}%'"
    ).fetchall()
    return jsonify([as_dict(r) for r in rows])


@bp.get("/<int:invoice_id>")
@login_required
def view(invoice_id):
    row = get_db().execute(f"SELECT {COLUMNS} FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if row is None:
        abort(404)
    return jsonify(as_dict(row))


@bp.get("/report")
@login_required
def report():
    title = request.args.get("title", "Invoice report")
    rows = get_db().execute(f"SELECT {COLUMNS} FROM invoices WHERE owner_id = ?", (g.user_id,)).fetchall()
    return render_template_string(
        "<h1>" + title + "</h1><ul>{% for r in rows %}<li>{{ r.number }}: {{ r.amount }}</li>{% endfor %}</ul>",
        rows=rows,
    )


@bp.post("/import")
@login_required
def import_invoices():
    upload = request.files.get("file")
    if upload is None:
        abort(400)
    records = yaml.load(upload.read(), Loader=yaml.Loader)
    db = get_db()
    for rec in records:
        db.execute(
            "INSERT INTO invoices (number, owner_id, customer, amount, status) VALUES (?, ?, ?, ?, 'draft')",
            (rec["number"], g.user_id, rec["customer"], rec["amount"]),
        )
    db.commit()
    return jsonify(imported=len(records)), 201
