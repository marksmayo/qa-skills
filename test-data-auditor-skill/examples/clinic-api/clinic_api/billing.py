import locale


def format_fee(cents):
    locale.setlocale(locale.LC_ALL, "")
    return locale.currency(cents / 100, grouping=True)
