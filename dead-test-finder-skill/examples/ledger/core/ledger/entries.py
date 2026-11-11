from dataclasses import dataclass


class LedgerError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    account: str
    amount_cents: int
    memo: str = ""


def post_entry(journal, account, amount_cents, memo=""):
    if amount_cents == 0:
        raise LedgerError("an entry needs a non-zero amount")
    entry = Entry(account, amount_cents, memo)
    journal.append(entry)
    return entry


def balance(journal, account):
    return sum(e.amount_cents for e in journal if e.account == account)
