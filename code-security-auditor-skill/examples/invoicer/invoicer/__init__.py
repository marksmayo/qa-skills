import os
import subprocess

from flask import Flask, jsonify

from . import attachments, auth, db, invoices, webhooks


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "invoicer-dev-7f3c9a1e52")
    app.config["DATABASE"] = os.environ.get("INVOICER_DB", os.path.join(app.instance_path, "invoicer.db"))
    app.config["UPLOAD_DIR"] = os.path.join(app.root_path, "..", "uploads")
    os.makedirs(app.instance_path, exist_ok=True)

    db.init_app(app)
    app.register_blueprint(auth.bp)
    app.register_blueprint(invoices.bp)
    app.register_blueprint(attachments.bp)
    app.register_blueprint(webhooks.bp)

    @app.get("/healthz")
    def healthz():
        return "invoicer-ok"

    @app.get("/version")
    def version():
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
        return jsonify(version="0.4.0", commit=commit or None)

    return app
