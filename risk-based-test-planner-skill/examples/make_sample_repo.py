#!/usr/bin/env python3
"""Build "larder", the sample shop repository the example plan is based on (stdlib + git).

    python make_sample_repo.py <empty or missing directory>

The example can't ship a .git directory, so this script creates one with deterministic,
back-dated commits by four authors between 2026-03-02 and 2026-10-28, and a tag v1.4 on
2026-10-01. Running it twice gives the same commit hashes. The answer key in README.md describes
what the history contains: payments churns and has six fix commits, marketing has the most churn
and no fixes, reports is the most complex and barely changes, auth is quiet and has no tests,
giftcards arrives after v1.4, and one commit only reformats code.
"""
from __future__ import annotations
import os, subprocess, sys
from pathlib import Path

AUTHORS = [('Ana Ruiz', 'ana@example.com'), ('Ben Okafor', 'ben@example.com'), ('Chloe Tan', 'chloe@example.com'),
           ('Dev Patel', 'dev@example.com')]


def git(repo: Path, *args: str, when: str | None = None, author: int = 0) -> str:
    name, email = AUTHORS[author % len(AUTHORS)]
    env = {**os.environ, 'GIT_AUTHOR_NAME': name, 'GIT_AUTHOR_EMAIL': email, 'GIT_COMMITTER_NAME': name,
           'GIT_COMMITTER_EMAIL': email, 'GIT_CONFIG_NOSYSTEM': '1', 'HOME': str(repo)}
    if when:
        env['GIT_AUTHOR_DATE'] = env['GIT_COMMITTER_DATE'] = f'{when}T10:00:00+00:00'
    res = subprocess.run(['git', '-C', str(repo), '-c', 'core.autocrlf=false', '-c', 'commit.gpgsign=false', *args],
                         capture_output=True, text=True, env=env)
    if res.returncode:
        raise SystemExit(f'git {" ".join(args)}: {res.stderr}')
    return res.stdout


def write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8', newline='\n')


def commit(repo: Path, when: str, msg: str, author: int) -> None:
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', msg, when=when, author=author)


PAYMENTS = '''from .gateway import charge_card, refund_charge

FEE_CENTS = {fee}


def capture_payment(order, card_token):
    """Charge the order total plus the card fee; return the charge id."""
    if order["total_cents"] <= 0:
        raise ValueError("nothing to charge")
    amount = order["total_cents"] + FEE_CENTS
    return charge_card(card_token, amount, currency="{currency}")


def refund_payment(charge_id, amount_cents, reason):
    if amount_cents <= 0:
        raise ValueError("refund must be positive")
    if reason not in {reasons}:
        raise ValueError("unknown refund reason")
    return refund_charge(charge_id, amount_cents)
{extra}'''

GATEWAY = '''import json
import urllib.request

GATEWAY_URL = "https://payments.example.test/v1"


def _post(path, payload):
    req = urllib.request.Request(GATEWAY_URL + path, data=json.dumps(payload).encode(), method="POST")
    with urllib.request.urlopen(req, timeout={timeout}) as resp:
        return json.loads(resp.read())


def charge_card(card_token, amount_cents, currency):
    return _post("/charges", {{"source": card_token, "amount": amount_cents, "currency": currency}})["id"]


def refund_charge(charge_id, amount_cents):
    return _post("/refunds", {{"charge": charge_id, "amount": amount_cents}})["id"]
'''

AUTH = '''import hashlib
import hmac
import secrets

SESSION_TTL_SECONDS = {ttl}


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000).hex()


def login(user, password):
    if not hmac.compare_digest(user["password_hash"], hash_password(password, user["salt"])):
        return None
    return {{"token": secrets.token_urlsafe(32), "user_id": user["id"], "ttl": SESSION_TTL_SECONDS}}


def has_permission(user, permission):
    return permission in user.get("roles", [])
'''

REPORTS = '''def stock_status(item):
    level = item["on_hand"] - item["reserved"]
    if item.get("discontinued"):
        if level > 0:
            return "clearance"
        return "gone"
    if level <= 0:
        if item.get("on_order"):
            return "backordered"
        return "out"
    if level < item.get("reorder_at", 5):
        if item.get("supplier_delay_days", 0) > 7:
            return "reorder_urgent"
        return "reorder"
    if level > item.get("max_stock", 100):
        return "overstocked"
    return "ok"


def weekly_stock_report(items, include_discontinued=False, group_by_aisle=True):
    rows = {{}}
    for item in items:
        if item.get("discontinued") and not include_discontinued:
            continue
        status = stock_status(item)
        key = item.get("aisle", "unassigned") if group_by_aisle else "all"
        bucket = rows.setdefault(key, {{}})
        bucket[status] = bucket.get(status, 0) + 1
        if status in ("out", "reorder_urgent") and item.get("seasonal"):
            bucket["seasonal_risk"] = bucket.get("seasonal_risk", 0) + 1
        elif status == "overstocked" and item.get("perishable"):
            bucket["waste_risk"] = bucket.get("waste_risk", 0) + 1
    for key in rows:
        for status in ("ok", "reorder", "out"):
            if status not in rows[key]:
                rows[key][status] = 0
    return rows{tail}
'''

CATALOG = '''PRODUCTS = {{
    "flour-1kg": {{"name": "Flour 1 kg", "price_cents": {flour}, "aisle": "baking"}},
    "oats-750g": {{"name": "Rolled oats 750 g", "price_cents": 459, "aisle": "breakfast"}},
    "honey-500g": {{"name": "Manuka honey 500 g", "price_cents": {honey}, "aisle": "spreads"}},
}}


def find(slug):
    return PRODUCTS.get(slug)


def search(term):
    term = term.lower()
    return sorted(slug for slug, p in PRODUCTS.items() if term in p["name"].lower())
'''

MARKETING = '''BANNERS = [
{banners}
]


def banner_for(aisle):
    for b in BANNERS:
        if b["aisle"] == aisle:
            return b["text"]
    return BANNERS[0]["text"]
'''

GIFTCARDS = '''import secrets

GIFT_CARD_MAX_CENTS = 50000


def issue_gift_card(amount_cents):
    if not 500 <= amount_cents <= GIFT_CARD_MAX_CENTS:
        raise ValueError("gift cards are $5 to $500")
    return {"code": secrets.token_hex(8).upper(), "balance_cents": amount_cents}


def redeem(card, order_total_cents):
    used = min(card["balance_cents"], order_total_cents)
    card["balance_cents"] -= used
    return order_total_cents - used
'''

TESTS = {
    'tests/test_payments.py': '''import pytest

from larder.payments import service


def test_zero_total_is_refused():
    with pytest.raises(ValueError):
        service.capture_payment({"total_cents": 0}, "tok")


def test_refund_reason_is_checked():
    with pytest.raises(ValueError):
        service.refund_payment("ch_1", 100, "because")
''',
    'tests/test_catalog.py': '''from larder.catalog import products


def test_search_is_case_insensitive():
    assert products.search("FLOUR") == ["flour-1kg"]
''',
    'tests/test_marketing.py': '''from larder.marketing import banners


def test_unknown_aisle_gets_the_first_banner():
    assert banners.banner_for("nowhere") == banners.BANNERS[0]["text"]
''',
    'tests/test_reports.py': '''from larder.reports import stock


def test_out_of_stock():
    assert stock.stock_status({"on_hand": 0, "reserved": 0}) == "out"
''',
}


def banners(n: int) -> str:
    aisles = ['baking', 'breakfast', 'spreads', 'produce', 'dairy', 'bakery', 'drinks', 'frozen', 'pantry', 'snacks']
    lines = []
    for i, a in enumerate(aisles):
        lines.append(f'    {{"aisle": "{a}", "text": "Week {n}: {["Fresh", "New", "Local", "Seasonal", "Bulk"][(n + i) % 5]} '
                     f'{a} deals, {(n * 7 + i * 3) % 40 + 10}% off selected lines"}},')
    return '\n'.join(lines)


def payments(fee=30, currency='NZD', reasons='("damaged", "late", "wrong_item")', extra=''):
    return PAYMENTS.format(fee=fee, currency=currency, reasons=reasons, extra=extra)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.splitlines()[2].strip())
        return 2
    repo = Path(sys.argv[1]).resolve()
    if repo.exists() and any(repo.iterdir()):
        raise SystemExit(f'{repo} is not empty')
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, 'init', '-q', '-b', 'main')
    write(repo, 'README.md', '# larder\n\nA small online grocery shop.\n')
    write(repo, 'pyproject.toml', '[project]\nname = "larder"\nversion = "1.3.0"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    write(repo, 'larder/__init__.py', '')
    for pkg in ('payments', 'auth', 'reports', 'catalog', 'marketing'):
        write(repo, f'larder/{pkg}/__init__.py', '')
    write(repo, 'larder/payments/service.py', payments())
    write(repo, 'larder/payments/gateway.py', GATEWAY.format(timeout=10))
    write(repo, 'larder/auth/session.py', AUTH.format(ttl=3600))
    write(repo, 'larder/reports/stock.py', REPORTS.format(tail=''))
    write(repo, 'larder/catalog/products.py', CATALOG.format(flour=329, honey=2899))
    write(repo, 'larder/marketing/banners.py', MARKETING.format(banners=banners(0)))
    write(repo, 'tests/__init__.py', '')
    for rel, text in TESTS.items():
        write(repo, rel, text)
    commit(repo, '2026-03-02', 'Initial shop: catalog, payments, auth, stock reports, banners', 0)

    # The 180-day window used by the example runs from 2026-05-05 to 2026-11-01.
    write(repo, 'larder/reports/stock.py', REPORTS.format(tail='\n\n\nREPORT_VERSION = 2\n'))
    commit(repo, '2026-05-20', 'Stock report: add a report version marker', 2)
    fixes = [
        ('2026-05-12', 'fix: refunds for spoiled goods were rejected', dict(reasons='("damaged", "spoiled", "late", "wrong_item")')),
        ('2026-06-03', 'fix: card fee missing from the charged amount', dict(fee=35)),
        ('2026-06-29', 'fix: refund reason "missing" was rejected', dict(reasons='("damaged", "spoiled", "late", "wrong_item", "missing")')),
        ('2026-07-21', 'fix: AUD orders were charged in NZD (PAY-41)', dict(currency='AUD')),
        ('2026-08-18', 'hotfix: revert currency change, charge in the order currency', dict(currency='NZD')),
        ('2026-09-09', 'fix: regression in partial refunds (PAY-57)', dict(fee=30)),
    ]
    features = [
        ('2026-05-26', 'Payments: log charge attempts', '\n\ndef log_attempt(order_id):\n    return f"attempt:{order_id}"\n'),
        ('2026-06-15', 'Payments: retry once on gateway timeout', '\n\ndef should_retry(error):\n    return "timeout" in str(error)\n'),
        ('2026-07-07', 'Payments: support store credit', '\n\ndef store_credit_cents(customer):\n    return customer.get("credit_cents", 0)\n'),
        ('2026-08-04', 'Payments: split tender groundwork', '\n\ndef split(amount_cents, parts):\n    return [amount_cents // parts] * parts\n'),
        ('2026-08-27', 'Payments: receipt numbers', '\n\ndef receipt_number(charge_id):\n    return "R-" + charge_id[-6:]\n'),
    ]
    state = dict(fee=30, currency='NZD', reasons='("damaged", "late", "wrong_item")', extra='')
    events = [(d, m, 'fix', kw) for d, m, kw in fixes] + [(d, m, 'feature', extra) for d, m, extra in features]
    for i in range(20):
        events.append((f'2026-{5 + i // 4:02d}-{(i % 4) * 7 + 6:02d}', f'Marketing: week {i + 1} banners', 'banner', i + 1))
    events += [('2026-06-10', 'Catalog: honey price update', 'catalog', dict(flour=329, honey=2999)),
               ('2026-07-14', 'fix: flour price was in dollars, not cents', 'catalog', dict(flour=349, honey=2999)),
               ('2026-09-02', 'Catalog: autumn prices', 'catalog', dict(flour=359, honey=3099)),
               ('2026-07-30', 'Auth: sessions last 2 hours', 'auth', 7200),
               ('2026-09-15', 'Gateway: longer timeout', 'gateway', 15)]
    for n, (when, msg, kind, data) in enumerate(sorted(events, key=lambda e: (e[0], e[1]))):
        if kind == 'fix':
            state.update(data)
        elif kind == 'feature':
            state['extra'] += data
        if kind in ('fix', 'feature'):
            write(repo, 'larder/payments/service.py', payments(**state))
        elif kind == 'banner':
            write(repo, 'larder/marketing/banners.py', MARKETING.format(banners=banners(data)))
        elif kind == 'catalog':
            write(repo, 'larder/catalog/products.py', CATALOG.format(**data))
        elif kind == 'auth':
            write(repo, 'larder/auth/session.py', AUTH.format(ttl=data))
        elif kind == 'gateway':
            write(repo, 'larder/payments/gateway.py', GATEWAY.format(timeout=data))
        commit(repo, when, msg, n)
    # A formatting-only commit: every changed line is replaced by one of the same length class.
    for rel in ('larder/catalog/products.py', 'larder/marketing/banners.py', 'larder/reports/stock.py'):
        p = repo / rel
        p.write_text(p.read_text(encoding='utf-8').replace('    return', '    return').replace('"', '"').replace(' = {', ' = {')
                     .replace('def ', 'def ').rstrip('\n') + '\n\n', encoding='utf-8', newline='\n')
        p.write_text(p.read_text(encoding='utf-8').rstrip('\n').replace('\n\n\n', '\n\n\n') + '\n', encoding='utf-8', newline='\n')
    text = (repo / 'larder/reports/stock.py').read_text(encoding='utf-8')
    (repo / 'larder/reports/stock.py').write_text(text.replace('rows = {}', 'rows = dict()'), encoding='utf-8', newline='\n')
    text = (repo / 'larder/catalog/products.py').read_text(encoding='utf-8')
    (repo / 'larder/catalog/products.py').write_text(text.replace('term = term.lower()', 'term = term.casefold()').replace(
        'p["name"].lower()', 'p["name"].casefold()'), encoding='utf-8', newline='\n')
    commit(repo, '2026-09-22', 'style: normalise string handling (no behaviour change)', 1)
    git(repo, 'tag', 'v1.4', when='2026-10-01')
    write(repo, 'larder/giftcards/__init__.py', '')
    write(repo, 'larder/giftcards/cards.py', GIFTCARDS)
    commit(repo, '2026-10-12', 'Gift cards: issue and redeem', 3)
    state['extra'] += '\n\ndef apply_gift_card(order, card):\n    from larder.giftcards.cards import redeem\n    order["total_cents"] = redeem(card, order["total_cents"])\n    return order\n'
    write(repo, 'larder/payments/service.py', payments(**state))
    commit(repo, '2026-10-20', 'Payments: pay part of an order with a gift card', 0)
    write(repo, 'larder/marketing/banners.py', MARKETING.format(banners=banners(30)))
    commit(repo, '2026-10-28', 'Marketing: gift card launch banners', 2)
    head = git(repo, 'rev-parse', '--short', 'HEAD').strip()
    count = git(repo, 'rev-list', '--count', 'HEAD').strip()
    print(f'larder built at {repo}: {count} commits, HEAD {head}, tag v1.4')
    return 0


if __name__ == '__main__':
    sys.exit(main())
