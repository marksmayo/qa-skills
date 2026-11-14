"""Tide tables: a small Flask site with Jinja pages and a JSON API (fixture, not runnable)."""
import json
import os
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from flask_limiter import Limiter

app = Flask(__name__)
STATIONS_DIR = Path(__file__).parent / "stations"


def client_key():
    # Planted: the rate-limit key trusts a header any client can set.
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() or request.remote_addr


limiter = Limiter(client_key, app=app, default_limits=["60 per minute"])


def load_stations():
    return [json.loads(p.read_text()) for p in sorted(STATIONS_DIR.glob("*.json"))]


@app.route("/")
def index():
    return render_template("index.html", stations=load_stations()[:20])


@app.route("/station/<code>")
def station(code):
    data = next((s for s in load_stations() if s["code"] == code), None)
    return render_template("station.html", station=data)


@app.route("/api/stations")
@limiter.limit("30 per minute")
def api_stations():
    page = request.args.get("page", 1, type=int)
    items = load_stations()
    return jsonify(items[(page - 1) * 50: page * 50])


@app.route("/api/search")
def api_search():
    # Planted: every match is returned, with no page size or minimum query length.
    q = request.args.get("q", "").lower()
    return jsonify([s for s in load_stations() if q in s["name"].lower()])


if __name__ == "__main__":
    # Planted: the debugger is on unless FLASK_ENV says otherwise, on every interface.
    debug_mode = os.environ.get("FLASK_ENV", "development") == "development"
    app.run(host="0.0.0.0", port=5000, debug=debug_mode)
