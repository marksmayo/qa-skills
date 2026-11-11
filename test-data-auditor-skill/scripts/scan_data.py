#!/usr/bin/env python3
"""Scan test data against the static rules and write candidates for review (stdlib only).

    python scan_data.py <repo> --inventory inventory.json --out candidates.json [--today 2026-11-11]

Categories: personal_data, time_bomb, magic_id, environment_coupling, shared_mutable,
nondeterministic, schema_drift, snapshot_rot, unused_or_duplicate, locale_assumption, and
secret (routed to code-security-auditor, never rated here). Settings are in data/rules.json and
reserved values in data/reserved.json.

Personal data and secrets are never written out: a candidate gives the file, line, field, the
kind of data and why it was flagged, plus a short SHA-256 fingerprint of the normalised value so
the validator can tell candidates apart. A value on a reserved list (example.com, ACMA and
Ofcom fiction numbers, published test cards, specimen IDs) or with an invalid check digit is
dismissed automatically, and the dismissal names the rule.
"""
from __future__ import annotations
import argparse, ast, difflib, hashlib, json, re, sys
from datetime import date, timedelta
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import datafiles, detect_conventions, inventory as inv, schema_drift  # noqa: E402

RULES = json.loads((HERE.parent / 'data' / 'rules.json').read_text(encoding='utf-8'))
RESERVED = json.loads((HERE.parent / 'data' / 'reserved.json').read_text(encoding='utf-8'))
F = lambda key: re.compile(rf'^({RULES[key]})$', re.I)  # noqa: E731
EXPIRY, BIRTH, NAME, ADDRESS, PHONE_F, EMAIL_F = (F(k) for k in ('expiry_fields', 'birth_fields', 'name_fields', 'address_fields',
                                                                  'phone_fields', 'email_fields'))
ID_FIELDS = {k: re.compile(rf'^({v})$', re.I) for k, v in RULES['id_fields'].items()}
STREET = re.compile(RULES['street_pattern'])
EMAIL = re.compile(r'(?<![\w.+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)')
CARD = re.compile(r'(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)')
SSN = re.compile(r'(?<!\d)(\d{3})-(\d{2})-(\d{4})(?!\d)')
NINO = re.compile(r'\b([A-CEGHJ-PR-TW-Z]{2})\s?(\d{2})\s?(\d{2})\s?(\d{2})\s?([A-D])\b')
IBAN = re.compile(r'\b([A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?)\b')
PHONE_TEXT = re.compile(r'(?<![\w+])(\+?\d[\d ()-]{7,16}\d)(?!\w)')
ISO_DATE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})(?:[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?$')
URL = re.compile(r'\b(?:https?|wss?|s3|gs)://([A-Za-z0-9.-]+)(?::\d+)?(/[^\s"\'<>]*)?')
ARN = re.compile(r'\barn:aws:[a-z0-9-]+:[a-z0-9-]*:(\d{12}):')
LOCAL_PATH = re.compile(r'(?:/Users/[A-Za-z][\w.-]+/|/home/[a-z][\w.-]+/|[A-Za-z]:\\\\?Users\\\\?[A-Za-z][\w.-]+)')
UUID = re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b', re.I)
SECRETS = {k: re.compile(v) for k, v in RULES['secret_patterns'].items()}
ID_CALL = re.compile(r'^(get|find|fetch|load|lookup|read|retrieve)(_\w+)?$|_by_id$|^by_id$', re.I)
PLACEHOLDER = re.compile(r'\b(test|example|sample|fake|dummy|placeholder|lorem|foo|bar|demo)\b', re.I)


def fp(value) -> str:
    return hashlib.sha256(re.sub(r'\s+', '', str(value)).lower().encode('utf-8')).hexdigest()[:16]


def digits(s: str) -> str:
    return re.sub(r'\D', '', s)


# ---------------------------------------------------------------- check digits
def luhn(n: str) -> bool:
    total = 0
    for i, c in enumerate(reversed(n)):
        d = int(c)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def medicare_ok(n: str) -> bool:
    return len(n) in (10, 11) and n[0] in '23456' and sum(int(a) * b for a, b in zip(n[:8], (1, 3, 7, 9, 1, 3, 7, 9))) % 10 == int(n[8])


def tfn_ok(n: str) -> bool:
    w = (1, 4, 3, 7, 5, 8, 6, 9, 10) if len(n) == 9 else (10, 7, 8, 4, 6, 3, 5, 1) if len(n) == 8 else None
    return bool(w) and sum(int(a) * b for a, b in zip(n, w)) % 11 == 0


def nhs_ok(n: str) -> bool:
    if len(n) != 10:
        return False
    check = 11 - sum(int(a) * b for a, b in zip(n[:9], range(10, 1, -1))) % 11
    check = 0 if check == 11 else check
    return check != 10 and check == int(n[9])


def ssn_ok(a: str, g: str, s: str) -> bool:
    return a not in ('000', '666') and not a.startswith('9') and g != '00' and s != '0000'


def nino_ok(prefix: str) -> bool:
    return prefix[0] not in 'DFIQUV' and prefix[1] not in 'DFIOQUV' and prefix not in ('BG', 'GB', 'NK', 'KN', 'TN', 'NT', 'ZZ')


def iban_ok(s: str) -> bool:
    s = s.replace(' ', '')
    if not 15 <= len(s) <= 34:
        return False
    r = s[4:] + s[:4]
    num = ''.join(str(int(c, 36)) for c in r)
    return int(num) % 97 == 1


def phone_reserved(d: str) -> str | None:
    p = RESERVED['phones']
    local = '0' + d[2:] if d.startswith('61') and len(d) == 11 else '0' + d[2:] if d.startswith('44') and len(d) in (12, 13) else d
    if local in p['au_mobile']['numbers']:
        return 'ACMA number reserved for creative works'
    if any(local.startswith(x) for x in p['au_landline']['prefixes']):
        return 'ACMA landline range reserved for creative works'
    if any(local.startswith(x) for x in p['uk']['prefixes']):
        return 'Ofcom range reserved for drama'
    if re.match(p['us']['pattern'], d):
        return 'NANPA fictitious 555-01xx number'
    return None


def email_class(domain: str) -> tuple[str, str]:
    d = domain.lower()
    r = RESERVED['email_domains_reserved']
    if d in r['exact'] or any(d.endswith(s) for s in r['suffixes']):
        return 'reserved', f'reserved documentation domain ({r["$source"]})'
    if d in RESERVED['public_mail_domains']:
        return 'public', 'an email address at a public mail domain'
    return 'other', 'an email address at a non-reserved domain'


def host_reserved(host: str) -> bool:
    h = host.lower()
    r = RESERVED['hosts_reserved']
    return h in r['exact'] or any(h.endswith(s) for s in r['suffixes']) or any(h.startswith(p) for p in r['ip_prefixes'])


# ---------------------------------------------------------------- scanner
class Scanner:
    def __init__(self, root: Path, inventory: dict, today: date):
        self.root, self.inv, self.today = root, inventory, today
        self.cands: list[dict] = []
        self.by_path = {i['path']: i for i in inventory['inventory']}
        self.files = inv.walk(root)
        self.sources = [f for f in self.files if f.endswith('.py') and not inv.in_test_area(f)]
        self.notes: list[str] = []

    def add(self, category: str, rule: str, file: str, line: int, why: str, field: str | None = None, data_type: str | None = None,
            value=None, auto_dismiss: str | None = None, details: dict | None = None, tests: list[str] | None = None):
        key = (category, rule, file, line, field, fp(value) if value is not None else None)
        if any(c['_key'] == key for c in self.cands):
            return
        c = {'_key': key, 'category': category, 'rule': rule, 'file': file, 'line': line, 'field': field, 'data_type': data_type,
             'why': why, 'fingerprint': fp(value) if value is not None and category in ('personal_data', 'secret') else None,
             'auto_dismiss': auto_dismiss, 'details': details or {},
             'affected_tests': tests if tests is not None else self.by_path.get(file, {}).get('loaded_by', [])}
        self.cands.append(c)

    # ---------------- personal data
    def personal_value(self, file, line, field, value, record_fields=None):
        if not isinstance(value, str) or not value.strip():
            return
        fname = field or ''
        for m in EMAIL.finditer(value):
            cls, why = email_class(m.group(2))
            self.add('personal_data', 'email', file, line, why, field, 'email address', m.group(0),
                     auto_dismiss=why if cls == 'reserved' else None,
                     details={'domain_class': cls, 'domain': m.group(2).lower() if cls != 'other' else None})
        if PHONE_F.match(fname) or (not fname and PHONE_TEXT.fullmatch(value.strip())):
            d = digits(value)
            if 8 <= len(d) <= 13:
                res = phone_reserved(d)
                self.add('personal_data', 'phone', file, line, res or 'a phone number outside the reserved ranges', field,
                         'phone number', d, auto_dismiss=res)
        d = digits(value)
        for idt, rx in ID_FIELDS.items():
            if not rx.match(fname):
                continue
            ok = {'medicare': medicare_ok, 'tfn': tfn_ok, 'nhs': nhs_ok}.get(idt)
            label = {'medicare': 'a Medicare number', 'tfn': 'a tax file number', 'nhs': 'an NHS number', 'ssn': 'a social security number',
                     'nino': 'a National Insurance number', 'iban': 'an IBAN'}[idt]
            examples = RESERVED['id_examples']['values']
            if idt == 'nhs' and d.startswith(examples['nhs']['prefix']):
                self.add('personal_data', idt, file, line, label, field, label, d, auto_dismiss='NHS test range (starts 999)')
            elif ok:
                if not d:
                    continue
                valid = ok(d)
                listed = d in examples.get(idt, [])
                self.add('personal_data', idt, file, line, f'{label} with a valid check digit' if valid else f'{label} field',
                         field, label, d, auto_dismiss='published specimen value' if listed else
                         None if valid else f'check digit invalid, so not {label}')
        if CARD.search(value):
            for m in CARD.finditer(value):
                n = digits(m.group(0))
                if 13 <= len(n) <= 19 and luhn(n):
                    test = n in RESERVED['test_cards']['numbers']
                    self.add('personal_data', 'card', file, line, 'a Luhn-valid card number not on any published test list'
                             if not test else 'a published test card number', field, 'card number', n,
                             auto_dismiss='published test card number' if test else None)
        for m in SSN.finditer(value):
            if ssn_ok(*m.groups()):
                n = ''.join(m.groups())
                self.add('personal_data', 'ssn', file, line, 'a social security number in valid format', field,
                         'social security number', n,
                         auto_dismiss='published specimen value' if n in RESERVED['id_examples']['values']['ssn'] else None)
        for m in NINO.finditer(value.upper()):
            n = ''.join(m.groups())
            if nino_ok(m.group(1)):
                self.add('personal_data', 'nino', file, line, 'a National Insurance number in valid format', field,
                         'National Insurance number', n,
                         auto_dismiss='published specimen value' if n in RESERVED['id_examples']['values']['nino'] else None)
        for m in IBAN.finditer(value):
            s = m.group(1).replace(' ', '')
            if iban_ok(s):
                self.add('personal_data', 'iban', file, line, 'an IBAN with a valid checksum', field, 'IBAN', s,
                         auto_dismiss='published specimen value' if s in RESERVED['id_examples']['values']['iban'] else None)

    def personal_record(self, rec: dict, file: str):
        fields = rec['fields']
        for k, v in fields.items():
            self.personal_value(file, v['line'], k, v['value'], fields)
        names = [(k, v) for k, v in fields.items() if NAME.match(k) and isinstance(v['value'], str) and v['value'].strip()]
        if not names:
            return
        nm = names[0]
        for k, v in fields.items():
            if ADDRESS.match(k) and isinstance(v['value'], str) and STREET.search(v['value']):
                self.add('personal_data', 'name_and_address', file, v['line'], 'a full name with a street address', k,
                         'name and street address', nm[1]['value'] + '|' + v['value'],
                         details={'name_field': nm[0], 'placeholder_words': bool(PLACEHOLDER.search(v['value'] + ' ' + nm[1]['value']))})
            if BIRTH.match(k) and isinstance(v['value'], str) and ISO_DATE.match(v['value'].strip()):
                self.add('personal_data', 'dob_with_name', file, v['line'], 'a date of birth beside a name', k, 'date of birth',
                         nm[1]['value'] + '|' + v['value'],
                         details={'name_field': nm[0], 'placeholder_words': bool(PLACEHOLDER.search(nm[1]['value']))})

    # ---------------- time bombs
    def now_comparisons(self) -> dict[str, list[dict]]:
        """{field name: [{file, line, op, function}]} for comparisons between an attribute/key and a now() call."""
        out: dict[str, list[dict]] = {}
        nows = {c.split('.')[-1].rstrip('()') for c in RULES['now_calls']}
        for rel in self.sources + [f for f in self.files if f.endswith('.py') and inv.in_test_area(f)]:
            try:
                tree = ast.parse((self.root / rel).read_text(encoding='utf-8', errors='replace'))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Compare):
                    continue
                parts = [node.left] + node.comparators
                has_now = any(isinstance(n, ast.Call) and getattr(n.func, 'attr', getattr(n.func, 'id', None)) in nows
                              for p in parts for n in ast.walk(p))
                if not has_now:
                    continue
                for i, p in enumerate(parts):
                    name = p.attr if isinstance(p, ast.Attribute) else \
                        p.slice.value if isinstance(p, ast.Subscript) and isinstance(p.slice, ast.Constant) else None
                    if isinstance(name, str):
                        op = type(node.ops[0 if i == 0 else i - 1]).__name__
                        out.setdefault(name, []).append({'file': rel, 'line': node.lineno, 'op': op, 'field_left': i == 0})
        return out

    def time_bombs(self, rec: dict, file: str, comparisons: dict):
        for k, v in rec['fields'].items():
            val = v['value']
            if not isinstance(val, str) or not EXPIRY.match(k):
                continue
            m = ISO_DATE.match(val.strip())
            if not m:
                continue
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if d < self.today:
                continue
            cmp = comparisons.get(k, [])
            fails = None
            if cmp:
                c = cmp[0]
                # field >= today (or today <= field) stays true until the day after; > / < flip on the day itself
                inclusive = (c['op'] in ('GtE', 'LtE'))
                fails = d + timedelta(days=1) if inclusive else d
            days = (fails or d) - self.today
            self.add('time_bomb', 'fixed_expiry', file, v['line'],
                     f'{k} is fixed at {d.isoformat()}' + (f' and compared with today at {cmp[0]["file"]}:{cmp[0]["line"]}'
                                                            if cmp else '; no comparison with now was found'),
                     k, 'date', details={'date': d.isoformat(), 'compared_with_now': cmp,
                                         'fails_on_date': fails.isoformat() if fails else None,
                                         'days_until': days.days})

    def code_time_bombs(self, rel: str, tree: ast.AST):
        nows = {c.split('.')[-1].rstrip('()') for c in RULES['now_calls']}
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls_now = any(isinstance(n, ast.Call) and getattr(n.func, 'attr', getattr(n.func, 'id', None)) in nows for n in ast.walk(fn))
            for n in ast.walk(fn):
                lit = None
                if isinstance(n, ast.Call) and getattr(n.func, 'id', getattr(n.func, 'attr', None)) in ('date', 'datetime') and \
                        len(n.args) >= 3 and all(isinstance(a, ast.Constant) and isinstance(a.value, int) for a in n.args[:3]):
                    try:
                        lit = date(*[a.value for a in n.args[:3]])
                    except ValueError:
                        lit = None
                elif isinstance(n, ast.Constant) and isinstance(n.value, str) and ISO_DATE.match(n.value):
                    g = ISO_DATE.match(n.value)
                    try:
                        lit = date(int(g.group(1)), int(g.group(2)), int(g.group(3)))
                    except ValueError:
                        lit = None
                if lit and calls_now and lit >= self.today:
                    self.add('time_bomb', 'future_literal_with_now', rel, n.lineno,
                             f'{fn.name} compares the fixed date {lit.isoformat()} with the current date', None, 'date',
                             details={'date': lit.isoformat(), 'fails_on_date': None, 'days_until': (lit - self.today).days},
                             tests=[t for t in self.by_path.get(rel, {}).get('loaded_by', []) if t.endswith('::' + fn.name)] or None)

    # ---------------- test code rules
    def seed_ids(self) -> dict[int, str]:
        out = {}
        for item in self.inv['inventory']:
            if item['kind'] != 'seed' or item['status'] != 'scanned':
                continue
            try:
                for r in datafiles.load_records(self.root / item['path'], item['path']):
                    v = r['fields'].get('id', {}).get('value')
                    if isinstance(v, int):
                        out[v] = f'{item["path"]}:{r["line"]}'
            except datafiles.NotAssessed:
                pass
        return out

    def test_code(self, rel: str, tree: ast.AST, seeds: dict[int, str]):
        item = self.by_path.get(rel, {})
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not fn.name.startswith('test'):
                continue
            tid = next((t for t in item.get('loaded_by', []) if t.endswith('::' + fn.name)), f'{rel}::{fn.name}')
            for n in ast.walk(fn):
                if isinstance(n, ast.Call):
                    name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, 'id', '')
                    args = list(n.args) + [k.value for k in n.keywords if k.arg in ('id', 'pk') or (k.arg or '').endswith('_id')]
                    for a in args[:1] if ID_CALL.search(name or '') else [k.value for k in n.keywords
                                                                            if k.arg in ('id', 'pk') or (k.arg or '').endswith('_id')]:
                        if isinstance(a, ast.Constant) and isinstance(a.value, int) and not isinstance(a.value, bool) and a.value >= 2:
                            seed = seeds.get(a.value)
                            self.add('magic_id', 'literal_lookup', rel, a.lineno,
                                     f'{fn.name} looks up id {a.value} by a literal' + (f', which matches the seed row at {seed}' if seed
                                                                                      else '; no seed row has it'),
                                     None, 'id', details={'id': a.value, 'seed_row': seed, 'call': name}, tests=[tid])
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and UUID.search(n.value):
                    self.add('magic_id', 'literal_uuid', rel, n.lineno, f'{fn.name} uses a literal UUID', None, 'uuid',
                             details={}, tests=[tid])
                if isinstance(n, ast.Compare) and isinstance(n.left, ast.Attribute) and n.left.attr in ('id', 'pk') and \
                        any(isinstance(c, ast.Constant) and isinstance(c.value, int) for c in n.comparators):
                    self.add('magic_id', 'auto_increment', rel, n.lineno, f'{fn.name} asserts an auto-increment id', None, 'id',
                             details={}, tests=[tid])

    def shared_mutable(self, py: inv.PyTests):
        wide = set(RULES['wide_scopes'])
        muts = set(RULES['mutating_methods'])
        for path, fxs in py.fixtures.items():
            for name, fn in fxs.items():
                scope = None
                for d in fn.decorator_list:
                    if isinstance(d, ast.Call):
                        for kw in d.keywords:
                            if kw.arg == 'scope' and isinstance(kw.value, ast.Constant):
                                scope = kw.value.value
                if scope not in wide:
                    continue
                rets = [n.value for n in ast.walk(fn) if isinstance(n, (ast.Return, ast.Yield)) and n.value is not None]
                mutable = any(isinstance(r, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)) or
                              (isinstance(r, ast.Call) and getattr(r.func, 'id', '') in ('list', 'dict', 'set', 'defaultdict', 'deque'))
                              for r in rets)
                if not mutable:
                    continue
                users, mutators = [], []
                for tp, tfn in py.all_tests():
                    vis = py.visible_fixtures(tp)
                    if name not in py.requested(tfn, vis) or vis[name][0] != path:
                        continue
                    tid = py.test_id(tp, tfn)
                    users.append(tid)
                    for n in ast.walk(tfn):
                        target = None
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in muts:
                            target = n.func.value
                        elif isinstance(n, (ast.Assign, ast.AugAssign, ast.Delete)):
                            ts = n.targets if isinstance(n, (ast.Assign, ast.Delete)) else [n.target]
                            target = next((t.value for t in ts if isinstance(t, ast.Subscript)), None)
                        if isinstance(target, ast.Name) and target.id == name:
                            mutators.append({'test': tid, 'line': n.lineno})
                            break
                if mutators:
                    self.add('shared_mutable', 'mutated_wide_fixture', path, fn.lineno,
                             f'{scope}-scoped fixture {name} returns a mutable value and {len({m["test"] for m in mutators})} test(s) '
                             f'mutate it; {len(users)} test(s) share it', name, None,
                             details={'fixture': name, 'scope': scope, 'mutated_by': mutators, 'shared_by': users}, tests=users)

    def nondeterministic(self, rel: str, text: str, tree: ast.AST):
        seeded = re.search(r'random\.seed\(|Random\(\s*\d|Faker\.seed\(|seed_instance\(|\.seed\(\s*\d|faker\.seed', text)
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            mod = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else None
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', '')
            if (mod == 'random' and name not in ('seed', 'Random')) and not seeded:
                self.add('nondeterministic', 'unseeded_random', rel, n.lineno, f'random.{name}() with no seed in this file', None, None)
            elif name == 'Faker' and not seeded:
                self.add('nondeterministic', 'unseeded_faker', rel, n.lineno, 'Faker() with no seed in this file', None, None)
            elif name in ('uuid4', 'uuid1') and self.by_path.get(rel, {}).get('kind') in ('fixture', 'factory', 'snapshot'):
                self.add('nondeterministic', 'generated_uuid', rel, n.lineno, f'{name}() in test data', None, None)

    def locale(self, py_tests: inv.PyTests):
        calls = set(RULES['locale_calls_py'])
        dependent = {}
        for rel in self.sources:
            try:
                tree = ast.parse((self.root / rel).read_text(encoding='utf-8', errors='replace'))
            except SyntaxError:
                continue
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                used = set()
                machine = False
                for n in ast.walk(fn):
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name):
                        full = f'{n.func.value.id}.{n.func.attr}'
                        if full in calls:
                            used.add(full)
                        if full == 'locale.setlocale' and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant) and n.args[1].value in ('', None):
                            machine = True
                    if isinstance(n, ast.Call) and getattr(n.func, 'attr', '') == 'strftime' and n.args and \
                            isinstance(n.args[0], ast.Constant) and re.search(r'%[aAbBcxXp]', str(n.args[0].value)):
                        used.add('strftime ' + n.args[0].value)
                if used and machine:
                    dependent[fn.name] = {'file': rel, 'line': fn.lineno, 'calls': sorted(used)}
        for tp, tfn in py_tests.all_tests():
            for n in ast.walk(tfn):
                if not isinstance(n, ast.Compare):
                    continue
                parts = [n.left] + n.comparators
                call = next((p for p in parts if isinstance(p, ast.Call) and getattr(p.func, 'id', getattr(p.func, 'attr', '')) in dependent), None)
                lit = next((p for p in parts if isinstance(p, ast.Constant) and isinstance(p.value, str)), None)
                if call is not None and lit is not None:
                    name = getattr(call.func, 'id', getattr(call.func, 'attr', ''))
                    dep = dependent[name]
                    self.add('locale_assumption', 'machine_locale_literal', tp, n.lineno,
                             f'{tfn.name} compares {name}() with the literal "{lit.value}"; {name} formats with the machine '
                             f'locale ({dep["file"]}:{dep["line"]}, {", ".join(dep["calls"])})', None, 'formatted value',
                             details={'literal': lit.value, 'function': f'{dep["file"]}:{dep["line"]}'}, tests=[py_tests.test_id(tp, tfn)])

    def environment(self, rel: str, line_no: int, text: str):
        for m in URL.finditer(text):
            host = m.group(1)
            if host_reserved(host):
                continue
            if re.search(RULES['environment_markers'], host, re.I) or m.group(0).startswith(('s3://', 'gs://')):
                self.add('environment_coupling', 'environment_host', rel, line_no, f'points at {host}, which looks like a '
                         'real environment', None, 'hostname', details={'host': host})
        for m in ARN.finditer(text):
            self.add('environment_coupling', 'account_id', rel, line_no, 'an AWS ARN with a real-looking account id', None,
                     'account id', details={})
        if LOCAL_PATH.search(text):
            self.add('environment_coupling', 'local_path', rel, line_no, 'an absolute path on someone\'s machine', None, 'path',
                     details={})

    def secrets(self, rel: str, line_no: int, text: str):
        for kind, rx in SECRETS.items():
            m = rx.search(text)
            if m:
                self.add('secret', kind, rel, line_no, f'looks like a secret ({kind.replace("_", " ")})', None, 'secret', m.group(0),
                         details={'route_to': 'code-security-auditor'})

    # ---------------- files
    def snapshots_and_duplicates(self):
        items = [i for i in self.inv['inventory'] if i['status'] == 'scanned']
        for i in items:
            if i['kind'] == 'snapshot':
                p = PurePosixPath(i['path'])
                owner = None
                if p.suffix == '.snap':
                    owner = (p.parent.parent / p.stem).as_posix()
                elif p.suffix == '.ambr':
                    owner = (p.parent.parent / (p.stem + '.py')).as_posix()
                if owner and owner not in self.files:
                    self.add('snapshot_rot', 'orphan_snapshot', i['path'], 1, f'no test file {owner} exists for this snapshot', None,
                             None, details={'expected_test': owner})
                if i.get('lines', 0) > RULES['snapshot_max_lines']:
                    self.add('snapshot_rot', 'large_snapshot', i['path'], 1, f'{i["lines"]} lines asserted wholesale', None, None,
                             details={'lines': i['lines']})
            elif i['kind'] in ('fixture', 'recording', 'seed') and not i['loaded_by']:
                self.add('unused_or_duplicate', 'unused', i['path'], 1, 'no test loads this file (its name appears in no test, '
                         'fixture or loader)', None, None)
        data = [i for i in items if i['kind'] in ('fixture', 'recording', 'seed') and i.get('lines', 0) >= RULES['duplicate_min_lines']]
        texts = {i['path']: re.sub(r'\s+', ' ', (self.root / i['path']).read_text(encoding='utf-8')) for i in data}
        for a in range(len(data)):
            for b in range(a + 1, len(data)):
                x, y = data[a], data[b]
                if x['format'] != y['format']:
                    continue
                tx, ty = texts[x['path']], texts[y['path']]
                # cheap upper bounds first: the length bound, then quick_ratio; full matching is quadratic on large files
                if not tx or not ty or 2 * min(len(tx), len(ty)) / (len(tx) + len(ty)) < RULES['duplicate_similarity']:
                    continue
                sm = difflib.SequenceMatcher(None, tx, ty, autojunk=False)
                if sm.real_quick_ratio() < RULES['duplicate_similarity'] or sm.quick_ratio() < RULES['duplicate_similarity']:
                    continue
                if len(tx) + len(ty) > RULES.get('duplicate_max_chars', 400_000):
                    # very large files: compare their line sets instead (Jaccard), and say so
                    lx = set((self.root / x['path']).read_text(encoding='utf-8').splitlines())
                    ly = set((self.root / y['path']).read_text(encoding='utf-8').splitlines())
                    ratio = len(lx & ly) / len(lx | ly) if lx | ly else 0.0
                    if ratio >= RULES['duplicate_similarity']:
                        self.add('unused_or_duplicate', 'near_duplicate', y['path'], 1, f'{ratio:.0%} of distinct lines shared with {x["path"]}',
                                 None, None, details={'similar_to': x['path'], 'similarity': round(ratio, 3), 'method': 'line_jaccard'})
                    continue
                ratio = sm.ratio()
                if ratio >= RULES['duplicate_similarity']:
                    self.add('unused_or_duplicate', 'near_duplicate', y['path'], 1, f'{ratio:.0%} similar to {x["path"]}', None, None,
                             details={'similar_to': x['path'], 'similarity': round(ratio, 3)})

    def run(self) -> dict:
        comparisons = self.now_comparisons()
        seeds = self.seed_ids()
        py = inv.PyTests(self.root, self.files)
        for item in self.inv['inventory']:
            if item['status'] != 'scanned':
                continue
            rel = item['path']
            p = self.root / rel
            size_mb = p.stat().st_size / 1_000_000
            if size_mb > RULES['large_file_mb']:
                self.notes.append(f'{rel} is {size_mb:.0f} MB: scanned line by line only, without structure')
                records = []
            else:
                try:
                    records = datafiles.load_records(p, rel)
                except datafiles.NotAssessed as exc:
                    item['status'], item['reason'] = 'not_assessed', str(exc)
                    records = []
            for r in records:
                self.personal_record(r, rel)
                if item['kind'] in ('fixture', 'seed', 'recording', 'factory', 'inline'):
                    self.time_bombs(r, rel, comparisons)
            text = p.read_text(encoding='utf-8', errors='replace')
            structured = {(r_line) for r in records for r_line in [v['line'] for v in r['fields'].values()]}
            for n, line in enumerate(text.split('\n'), 1):
                self.secrets(rel, n, line)
                self.environment(rel, n, line)
                if n not in structured:
                    for m in re.finditer(r'"([^"\n]{3,200})"|\'([^\'\n]{3,200})\'', line):
                        self.personal_value(rel, n, None, m.group(1) or m.group(2))
            if rel.endswith('.py'):
                try:
                    tree = ast.parse(text)
                except SyntaxError:
                    tree = None
                if tree is not None:
                    self.nondeterministic(rel, text, tree)
                    if item['kind'] == 'inline':
                        self.test_code(rel, tree, seeds)
                        self.code_time_bombs(rel, tree)
        self.shared_mutable(py)
        self.locale(py)
        for d in schema_drift.drift(self.root, self.inv)['findings']:
            what = {'extra_field': f'with a field {d["field"]} that {d["model"]} ({d["model_source"]}) no longer has',
                    'missing_required': f'without {d["field"]}, which {d["model"]} requires',
                    'invalid_enum': f'with {d["field"]} values outside {d["model"]}\'s enum'}[d['rule']]
            n = len(d['lines'])
            self.add('schema_drift', d['rule'], d['file'], d['lines'][0], f'{n} record{"s" if n != 1 else ""} {what}', d['field'], None,
                     details={'model': d['model'], 'model_source': d['model_source'], 'lines': d['lines'],
                              **({'allowed': d['allowed']} if 'allowed' in d else {})})
        self.snapshots_and_duplicates()
        out = []
        for i, c in enumerate(sorted(self.cands, key=lambda c: (c['file'], c['line'], c['category'], c['rule'])), 1):
            c.pop('_key')
            out.append({'id': f'C{i}', **c})
        return {'repo': self.root.name, 'today': self.today.isoformat(), 'candidates': out,
                'inventory_status': {i['path']: {'status': i['status'], 'reason': i['reason']} for i in self.inv['inventory']},
                'summary': {'candidates': len(out), 'auto_dismissed': len([c for c in out if c['auto_dismiss']]),
                            'by_category': {k: len([c for c in out if c['category'] == k]) for k in sorted({c['category'] for c in out})}},
                'notes': self.notes}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--inventory', type=Path, required=True)
    ap.add_argument('--today', help='date the scan treats as today (default: the real date)')
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    today = date.fromisoformat(args.today) if args.today else date.today()
    res = Scanner(args.repo.resolve(), json.loads(args.inventory.read_text(encoding='utf-8')), today).run()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=2) + '\n', encoding='utf-8')
    s = res['summary']
    print(f'{s["candidates"]} candidates ({s["auto_dismissed"]} dismissed by a reserved-value rule): '
          + ', '.join(f'{v} {k}' for k, v in s['by_category'].items()) + f' -> {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
