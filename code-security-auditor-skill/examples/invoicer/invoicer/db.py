import sqlite3

from flask import current_app, g
from werkzeug.security import generate_password_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    reset_token TEXT
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,
    number TEXT NOT NULL,
    owner_id INTEGER NOT NULL REFERENCES users (id),
    customer TEXT NOT NULL,
    amount REAL NOT NULL,
    status TEXT NOT NULL
);
"""
SEED_USERS = [("alice@example.test", "alice-test-pw"), ("bob@example.test", "bob-test-pw")]
SEED_INVOICES = [
    ("INV-1001", 1, "Acme Pty Ltd", 1200.00, "sent"),
    ("INV-1002", 1, "Globex", 450.50, "paid"),
    ("INV-2001", 2, "Initech", 990.00, "sent"),
    ("INV-2002", 2, "Umbrella Corp", 15000.00, "draft"),
]


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(app):
    with app.app_context():
        conn = get_db()
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            for email, password in SEED_USERS:
                conn.execute(
                    "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                    (email, generate_password_hash(password)),
                )
            conn.executemany(
                "INSERT INTO invoices (number, owner_id, customer, amount, status) VALUES (?, ?, ?, ?, ?)",
                SEED_INVOICES,
            )
            conn.commit()


def init_app(app):
    app.teardown_appcontext(close_db)
    init_db(app)
