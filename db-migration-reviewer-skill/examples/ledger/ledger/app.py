from flask import Flask, request

from .models import Transaction
from .queries import balance

app = Flask(__name__)


@app.post("/accounts/<int:account_id>/transactions")
def add_transaction(account_id):
    tx = Transaction(account_id=account_id, amount=request.json["amount"])
    app.session.add(tx)
    app.session.commit()
    return {"id": tx.id}


@app.get("/accounts/<int:account_id>/balance")
def get_balance(account_id):
    return {"balance": str(balance(app.session, account_id))}
