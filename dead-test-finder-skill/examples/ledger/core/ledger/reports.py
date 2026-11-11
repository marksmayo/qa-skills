from .entries import balance


def trial_balance(journal):
    accounts = sorted({e.account for e in journal})
    return {a: balance(journal, a) for a in accounts}


def to_csv_lines(totals):
    return [f"{account},{cents}" for account, cents in sorted(totals.items())]
