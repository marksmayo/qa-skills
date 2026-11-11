from sqlalchemy import text


def balance(session, account_id):
    return session.execute(text("SELECT SUM(amount) FROM transactions WHERE account_id = :a"), {"a": account_id}).scalar()


def large_transactions(session, limit):
    return session.execute(text("SELECT id, amount FROM transactions WHERE amount > :l ORDER BY amount DESC"), {"l": limit}).all()
