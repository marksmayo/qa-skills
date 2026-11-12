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
import argparse, ast, difflib, hashlib, json, math, re, sys
from datetime import date, timedelta
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import datafiles, detect_conventions, inventory as inv, jsparse, schema_drift  # noqa: E402

RULES = json.loads((HERE.parent / 'data' / 'rules.json').read_text(encoding='utf-8'))
RESERVED = json.loads((HERE.parent / 'data' / 'reserved.json').read_text(encoding='utf-8'))
F = lambda key: re.compile(rf'^({RULES[key]})$', re.I)  # noqa: E731
BIRTH, NAME, ADDRESS, PHONE_F, EMAIL_F = (F(k) for k in ('birth_fields', 'name_fields', 'address_fields', 'phone_fields', 'email_fields'))
# an expiry field may carry a prefix: trial_end_date, certification_expires_at (camelCase is converted first)
EXPIRY = re.compile(rf'^(?:\w+_)?({RULES["expiry_fields"]})$', re.I)
# fields whose values are never phone numbers, however phone-shaped (ids, passwords, codes, dates, counts, money)
NOT_PHONE_FIELD = re.compile(RULES['not_phone_fields'], re.I)


def snake(name: str | None) -> str:
    """camelCase and PascalCase field names as snake_case, so one set of field patterns serves Python and JavaScript."""
    if not name:
        return ''
    return re.sub(r'(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])', '_', str(name)).replace('-', '_').lower()
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
# a secret value that announces itself as a placeholder, or has too little entropy to be a real credential
PLACEHOLDER_SECRET = re.compile(RULES['placeholder_secret_words'], re.I)

# JavaScript and TypeScript (matched on masked source: comments removed, string contents blanked)
JS_NOW = re.compile(r'\bDate\.now\s*\(\s*\)|\bnew\s+Date\s*\(\s*\)|\b(?:dayjs|moment)\s*\(\s*\)|\bDateTime\.(?:now|utc)\s*\(\s*\)'
                    r'|\bTemporal\.Now\b')
JS_NOW_VAR = re.compile(r'\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::\s*[\w.]+\s*)?=\s*(?:Date\.now\s*\(\s*\)|new\s+Date\s*\(\s*\)'
                        r'(?:\.getTime\(\))?|(?:dayjs|moment)\s*\(\s*\)|DateTime\.(?:now|utc)\s*\(\s*\))\s*;?\s*$')
JS_CMP = re.compile(r'(?<![=!<>])(<=|>=|<|>)(?![=>])')
JS_DATE_HELPER = re.compile(r'\b(isPast|isFuture|isBefore|isAfter|isExpired|differenceIn\w+|compareAsc|compareDesc)\s*\(')
JS_NOT_FIELDS = {'getTime', 'valueOf', 'now', 'Date', 'new', 'Math', 'length', 'toISOString', 'getDate', 'setDate', 'const', 'let',
                 'var', 'return', 'if', 'await', 'this', 'Number', 'parse', 'utc', 'dayjs', 'moment', 'diff', 'isBefore', 'isAfter',
                 'isPast', 'isFuture'}
JS_FROZEN = re.compile(r'useFakeTimers|setSystemTime|MockDate|mockdate|timekeeper|\bclock\.install\(|page\.clock\.|advanceTo\(|'
                       r'jest\.useFakeTimers|sinon\.useFakeTimers|mockDate\(|freezeTime|tk\.freeze')
ATTR_BEFORE = re.compile(r'([A-Za-z_:][\w:.-]*)\s*=\s*$')        # an XML/SVG/HTML attribute name before its value
MARKUP_EXT = ('.svg', '.xml', '.html', '.htm', '.xhtml')
GEOMETRY_FIELD = re.compile(r'^(d|points|view_box|transform|path)$')
JS_KEY_BEFORE = re.compile(r'([A-Za-z_$][\w$]*)[\'"]?\s*:\s*(?:new\s+Date\s*\(|[\w$.]+\s*\()?\s*[\'"`]?$')
JS_ID_CALL = re.compile(r'(?<![\w$])(get|find|findOne|fetch|load|lookup|retrieve|[\w$]*By[Ii]d)\s*\(\s*(\d+)\s*[,)]'
                        r'|\bwhere\s*:\s*\{\s*()id\s*:\s*(\d+)\s*[,}]')
JS_AUTO_INC = re.compile(r'expect\s*\(\s*[\w$.]*\.id\s*\)\s*\.\s*(?:toBe|toEqual|toStrictEqual)\s*\(\s*\d+\s*\)')
FORMATTED_LITERAL = re.compile(r"""['"`][^'"`]*(\d{1,3},\d{3}\b|\d{1,3}\.\d{3},\d{2}\b|\b\d{1,2}[ /](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"""
                               r"""|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w* \d{1,2}\b|\b\d{1,2}:\d{2}\s?[AaPp][Mm]\b)""")
JS_KEYWORDS = {'if', 'for', 'while', 'switch', 'catch', 'with', 'return', 'function', 'else', 'do', 'try'}
JS_TOP_DECL = re.compile(r'^(?:export\s+)?(let|const|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:\[|\{|new\s+(?:Map|Set|Array|WeakMap)\b)')
JS_FUNC = re.compile(r'\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(|\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*'
                     r'(?:async\s+)?(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)|^\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*'
                     r'(?::[^{]+)?\{')


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


# ---------------------------------------------------------------- numbers that are not personal data
# Dates, times, version strings and epoch timestamps are digit runs too; the phone and card rules
# skip them (found on a real repository, where ISO dates were reported as phone numbers).
DATE_TOKEN = re.compile(r'(?<![\d.])(?:(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])'
                        r'|(?:0?[1-9]|[12]\d|3[01])[-/.](?:0?[1-9]|1[0-2])[-/.](?:19|20)\d{2}'
                        r'|(?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])[-/.](?:19|20)\d{2})(?![\d.])')
TIME_TOKEN = re.compile(r'(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:\.\d+)?)?(?!\d)')
DATE_TIME = re.compile(rf'^(?:{DATE_TOKEN.pattern}|{TIME_TOKEN.pattern})(?:(?:[T ]|\s*-\s*|\s+to\s+)'
                       rf'(?:{DATE_TOKEN.pattern}|{TIME_TOKEN.pattern}))*(?:Z|[+-]\d{{2}}:?\d{{2}})?$', re.I)
VERSION = re.compile(r'^v?\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?$', re.I)
TIME_FIELD = re.compile(r'(?i:(^|_)(ts|time|timestamp|epoch|date|datetime|created|updated|modified|expires?|exp|iat|nbf)(_|$)'
                        r'|_at$|_on$)|(At|On|Time|Date|Timestamp)$')


def compact_date(d: str) -> bool:
    """An eight-digit yyyymmdd that is a real calendar date (19xx or 20xx)."""
    if len(d) != 8 or d[:2] not in ('19', '20'):
        return False
    try:
        date(int(d[:4]), int(d[4:6]), int(d[6:]))
        return True
    except ValueError:
        return False


def epoch(d: str, field: str = '') -> bool:
    """Unix time in milliseconds (13 digits, 2001-2036), or in seconds when the field names a time."""
    if len(d) == 13 and d[0] == '1' and 1_000_000_000_000 <= int(d) < 2_100_000_000_000:
        return True
    return len(d) == 10 and 1_000_000_000 <= int(d) < 2_100_000_000 and bool(field and TIME_FIELD.search(field))


DATE_SHAPE = re.compile(r'^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?$|^\d{1,2}[-/.]\d{1,2}[-/.]\d{4}$')
SSN_SHAPE = re.compile(r'^\d{3}-\d{2}-\d{4}$')


def not_phone(value: str, field: str = '') -> str | None:
    """Why a phone-shaped value is not a phone number, or None."""
    v = value.strip()
    d = digits(v)
    if DATE_TIME.match(v) or (v == d and compact_date(d)):
        return 'a date or time'
    if DATE_SHAPE.match(v):
        return 'a date-shaped string (an invalid date in a date-validation test is still not a phone)'
    if SSN_SHAPE.match(v):
        return 'a social security number shape (checked by the SSN rule instead)'
    if VERSION.match(v) and '.' in v:
        return 'a version string'
    if v == d and epoch(d, field):
        return 'an epoch timestamp'
    if field and NOT_PHONE_FIELD.search(snake(field)):
        return f'the field {field} holds an identifier, code, secret, date or amount, not a phone number'
    return None


def placeholder_digits(d: str) -> str | None:
    """A phone number whose subscriber part is one repeated digit or a straight run (1234567, 9876543), or None."""
    tail = d[-7:]
    if len(tail) < 7:
        return None
    if len(set(tail)) == 1:
        return 'placeholder pattern: one repeated digit'
    if len(set(tail)) == 2:
        return 'placeholder pattern: the last seven digits use only two digit values (such as 1112222 or 0000004)'
    steps = {(int(b) - int(a)) % 10 for a, b in zip(tail, tail[1:])}
    if steps in ({1}, {9}):
        return 'placeholder pattern: a run of sequential digits'
    return None


def not_card(text: str, start: int, end: int, field: str = '') -> bool:
    """True when the card-shaped digit run text[start:end] is a date, time, version or timestamp."""
    run = text[start:end]
    if DATE_TOKEN.search(run):
        return True
    before, after = text[max(0, start - 1):start], text[end:end + 2]
    if before in ('.', ':', 'v', 'V') or re.match(r'[.:]\d|T\d', after):
        return True  # part of a version, a decimal, or a date followed by a time
    # a date that overlaps the run's edge ("2024-01-15 ..." where the match starts mid-date)
    off = max(0, start - 12)
    if any(m.start() + off < end and m.end() + off > start for m in DATE_TOKEN.finditer(text[off:end + 12])):
        return True
    return run == digits(run) and epoch(run, field)


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
    return placeholder_digits(d)


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


def entropy(s: str) -> float:
    """Shannon entropy in bits per character."""
    if not s:
        return 0.0
    counts = {c: s.count(c) for c in set(s)}
    return -sum(n / len(s) * math.log2(n / len(s)) for n in counts.values())


def placeholder_secret(kind: str, match: str) -> str | None:
    """Why a matched secret is an obvious placeholder (a placeholder word, or too little entropy), or None.

    Only the value is judged: the quoted string of a key or password field, or the token after the scheme of an
    authorization header. Private keys are never placeholders (the header line carries no value to judge)."""
    if kind == 'private_key':
        return None
    q = re.findall(r"""["']([^"']+)["']\s*$""", match) or re.findall(r'(?:bearer|basic|token)\s+(\S+)', match, re.I) or [match]
    value = q[-1]
    w = PLACEHOLDER_SECRET.search(value)
    if w:
        return f'placeholder secret: the value contains the placeholder word "{w.group(0).lower()}"'
    # a value that names what it stands for: google-access-token, zoom_client_secret, pi_123_secret_abc
    if re.fullmatch(r'[a-z]+(?:[-_.][a-z0-9]+)+', value) and \
            re.search(r'(^|[-_.])(secret|token|key|apikey|password|passwd|pass|access|refresh|client|session|auth|bearer)([-_.]|$)', value):
        return 'placeholder secret: a self-describing value made of lowercase words naming what it stands for'
    core = re.sub(r'[^A-Za-z0-9]', '', value)
    if len(set(core)) <= 4 or entropy(core) < RULES['placeholder_secret_max_entropy']:
        return 'placeholder secret: too little entropy to be a real credential'
    return None


# ---------------------------------------------------------------- scanner
class Scanner:
    def __init__(self, root: Path, inventory: dict, today: date):
        self.root, self.inv, self.today = root, inventory, today
        self.cands: list[dict] = []
        self.by_path = {i['path']: i for i in inventory['inventory']}
        self.files = inv.walk(root)
        self.sources = [f for f in self.files if f.endswith('.py') and not inv.in_test_area(f)]
        # JS/TS product code and tests (declaration files and bundles are not read)
        self.js_files = [f for f in self.files if jsparse.is_js(f) and not re.search(r'\.d\.[cm]?ts$|\.min\.js$', f)]
        self._js: dict[str, tuple] = {}
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
    def personal_value(self, file, line, field, value, record_fields=None, loose=False):
        """Check one value. `loose`: the field name came from source code (JavaScript object keys, `key = '...'`), so a
        phone-shaped value is checked even under a field not named as a phone, unless the field says it is not one."""
        if not isinstance(value, str) or not value.strip():
            return
        fname = snake(field)
        uuids = [(u.start(), u.end()) for u in UUID.finditer(value)]
        for m in EMAIL.finditer(value):
            if re.search(r'(UID|Message-ID|Content-ID|References|In-Reply-To)\s*:\s*<?$', value[:m.start()], re.I):
                continue                   # an iCalendar UID or a mail header id, not a mailbox
            cls, why = email_class(m.group(2))
            self.add('personal_data', 'email', file, line, why, field, 'email address', m.group(0),
                     auto_dismiss=why if cls == 'reserved' else None,
                     details={'domain_class': cls, 'domain': m.group(2).lower() if cls != 'other' else None})
        phone_shaped = PHONE_TEXT.fullmatch(value.strip()) and (not fname or loose)
        if (PHONE_F.match(fname) or phone_shaped) and not not_phone(value, fname):
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
        if CARD.search(value) and not GEOMETRY_FIELD.match(fname):
            for m in CARD.finditer(value):
                if not_card(value, m.start(), m.end(), fname):
                    continue
                if any(a < m.end() and b > m.start() for a, b in uuids):
                    continue               # part of a UUID: a UUID is never a card number, Luhn or not
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
        loose = rec.get('kind') == 'object_literal'
        for k, v in fields.items():
            if not v.get('date_call'):
                self.personal_value(file, v['line'], k, v['value'], fields, loose=loose)
        names = [(k, v) for k, v in fields.items() if NAME.match(snake(k)) and isinstance(v['value'], str) and v['value'].strip()]
        if not names:
            return
        nm = names[0]
        for k, v in fields.items():
            if ADDRESS.match(snake(k)) and isinstance(v['value'], str) and STREET.search(v['value']):
                self.add('personal_data', 'name_and_address', file, v['line'], 'a full name with a street address', k,
                         'name and street address', nm[1]['value'] + '|' + v['value'],
                         details={'name_field': nm[0], 'placeholder_words': bool(PLACEHOLDER.search(v['value'] + ' ' + nm[1]['value']))})
            if BIRTH.match(snake(k)) and isinstance(v['value'], str) and ISO_DATE.match(v['value'].strip()):
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
            if not isinstance(val, str) or not EXPIRY.match(snake(k)):
                continue
            m = ISO_DATE.match(val.strip())
            if not m:
                continue
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if d < self.today:
                continue
            cmp = comparisons.get(k, []) + (comparisons.get(snake(k), []) if snake(k) != k else [])
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
            for m in rx.finditer(text):
                why = placeholder_secret(kind, m.group(0))
                self.add('secret', kind, rel, line_no, f'looks like a secret ({kind.replace("_", " ")})', None, 'secret', m.group(0),
                         auto_dismiss=why, details={'route_to': None if why else 'code-security-auditor'})

    # ---------------- JavaScript and TypeScript
    def js_parsed(self, rel: str):
        """(text, tokens, masked text, masked lines) for a JS/TS file, cached."""
        if rel not in self._js:
            text = (self.root / rel).read_text(encoding='utf-8', errors='replace')
            toks = jsparse.tokenize(text)
            m = jsparse.masked(text, toks)
            self._js[rel] = (text, toks, m, m.split('\n'))
        return self._js[rel]

    def js_now_comparisons(self, out: dict[str, list[dict]]):
        """Add {field: [{file, line, op}]} for JS/TS lines comparing a property with the clock (Date.now(), new Date(),
        a variable holding either, or isPast/isBefore/isAfter against now)."""
        for rel in self.js_files:
            try:
                _, _, _, lines = self.js_parsed(rel)
            except OSError:
                continue
            now_vars = set()
            for ln in lines:
                m = JS_NOW_VAR.search(ln)
                if m:
                    now_vars.add(m.group(1))
            now_rx = JS_NOW if not now_vars else re.compile(JS_NOW.pattern + r'|\b(?:' + '|'.join(map(re.escape, now_vars)) + r')\b')
            for n, ln in enumerate(lines, 1):
                if not now_rx.search(ln) or JS_NOW_VAR.search(ln):
                    continue
                op = JS_CMP.search(ln)
                helper = JS_DATE_HELPER.search(ln)
                if not op and not helper:
                    continue
                kind = {'<': 'Lt', '>': 'Gt', '<=': 'LtE', '>=': 'GtE'}.get(op.group(1) if op else '', 'Lt')
                names = {m.group(1) for m in re.finditer(r'\.\s*([A-Za-z_$][\w$]*)', ln)} | \
                    {m.group(1) for m in re.finditer(r'\b([A-Za-z_$][\w$]*)\s*(?:\.getTime\(\)|\.valueOf\(\))?\s*(?:<=?|>=?)(?!=)', ln)} | \
                    {m.group(1) for m in re.finditer(r'(?:<=?|>=?)\s*([A-Za-z_$][\w$]*)\b', ln)}
                for name in names - JS_NOT_FIELDS - now_vars:
                    out.setdefault(name, []).append({'file': rel, 'line': n, 'op': kind, 'field_left': True})
                    out.setdefault(snake(name), []).append({'file': rel, 'line': n, 'op': kind, 'field_left': True}) \
                        if snake(name) != name else None

    def js_test_rules(self, rel: str, item: dict, comparisons: dict, seeds: dict, covered: set[int]):
        """Time bombs, magic ids, unseeded randomness and module-level shared state in one JS/TS test or support file."""
        text, toks, masked_text, lines = self.js_parsed(rel)
        tests = item.get('loaded_by') or [f'{rel}::*']
        frozen = JS_FROZEN.search(text)
        reads_clock = JS_NOW.search(masked_text)
        # a future literal date in a test that does not freeze the clock
        for k, t in enumerate(toks):
            if t.line in covered or frozen:
                continue
            iso = None
            if t.kind == 'str' and t.value and ISO_DATE.match(t.value.strip()):
                g = ISO_DATE.match(t.value.strip())
                try:
                    iso = date(int(g.group(1)), int(g.group(2)), int(g.group(3)))
                except ValueError:
                    iso = None
            elif t.kind == 'id' and t.text in ('new', 'Date'):
                dv, _ = jsparse.date_value(toks, k)         # new Date(2027, 4, 12), Date.UTC(...)
                if dv:                                       # a string argument is the same line and field: add() dedupes
                    iso = date.fromisoformat(dv[:10])
            if not iso or iso < self.today or frozen:
                continue
            key = JS_KEY_BEFORE.search(lines[t.line - 1][:max(0, t.pos - (text.rfind('\n', 0, t.pos) + 1))])
            field = key.group(1) if key else None
            cmp = (comparisons.get(field, []) + comparisons.get(snake(field), [])) if field else []
            if not (reads_clock or cmp):
                continue
            self.add('time_bomb', 'future_literal_with_now', rel, t.line,
                     f'the fixed date {iso.isoformat()} sits in a test that does not freeze the clock'
                     + (f'; {field} is compared with now at {cmp[0]["file"]}:{cmp[0]["line"]}' if cmp else
                        ' and reads the real clock (Date.now() or new Date())'),
                     field, 'date', details={'date': iso.isoformat(), 'fails_on_date': None, 'days_until': (iso - self.today).days,
                                             'compared_with_now': cmp[:3]}, tests=tests)
        if item['kind'] not in ('inline', 'support'):
            return
        for t in jsparse.string_literals(toks):
            if UUID.search(t.value):
                self.add('magic_id', 'literal_uuid', rel, t.line, 'a literal UUID in test code', None, 'uuid', details={}, tests=tests)
        for n, ln in enumerate(lines, 1):
            for m in JS_ID_CALL.finditer(ln):
                call, v = (m.group(1), int(m.group(2))) if m.group(2) else ('where', int(m.group(4)))
                if v >= 2:
                    seed = seeds.get(v)
                    self.add('magic_id', 'literal_lookup', rel, n, f'{call} looks up id {v} by a literal' +
                             (f', which matches the seed row at {seed}' if seed else '; no seed row has it'), None, 'id',
                             details={'id': v, 'seed_row': seed, 'call': call}, tests=tests)
            if JS_AUTO_INC.search(ln):
                self.add('magic_id', 'auto_increment', rel, n, 'asserts an auto-increment id', None, 'id', details={}, tests=tests)
        seeded = re.search(r'faker\.seed\(|seedrandom|\.seed\(\s*\d|spyOn\(\s*Math\s*,\s*[\'"]random', text)
        for n, ln in enumerate(lines, 1):
            if seeded:
                break
            if re.search(r'\bMath\.random\s*\(', ln):
                self.add('nondeterministic', 'unseeded_random', rel, n, 'Math.random() with no seed or stub in this file', None, None,
                         tests=tests)
            if re.search(r'\bfaker\.\w+', ln) and not re.search(r'^\s*import\b', ln):
                self.add('nondeterministic', 'unseeded_faker', rel, n, 'faker with no faker.seed() in this file', None, None, tests=tests)
        self.js_shared_state(rel, lines, tests)

    def js_generated_ids(self, rel: str, item: dict):
        """crypto.randomUUID(), uuid v4 and nanoid in fixtures, factories and snapshots (JS/TS)."""
        _, _, _, lines = self.js_parsed(rel)
        for n, ln in enumerate(lines, 1):
            m = re.search(r'\b(randomUUID|uuidv4|v4|nanoid|uuid)\s*\(', ln)
            if m:
                self.add('nondeterministic', 'generated_uuid', rel, n, f'{m.group(1)}() in test data', None, None)

    def js_shared_state(self, rel: str, lines: list[str], tests: list[str]):
        """Module-level arrays, objects, maps and sets that tests mutate and no beforeEach/afterEach resets."""
        hooks = []
        joined = '\n'.join(lines)
        starts = [0]
        for ln in lines:
            starts.append(starts[-1] + len(ln) + 1)
        for m in re.finditer(r'\b(beforeEach|afterEach|beforeAll|afterAll)\s*\(', joined):
            depth, i = 1, m.end()
            while i < len(joined) and depth:
                depth += joined[i] == '('
                depth -= joined[i] == ')'
                i += 1
            hooks.append((m.group(1), joined.count('\n', 0, m.start()) + 1, joined.count('\n', 0, i) + 1))
        for n, ln in enumerate(lines, 1):
            d = JS_TOP_DECL.match(ln)
            if not d:
                continue
            name, keyword = d.group(2), d.group(1)
            mut = re.compile(rf'\b{re.escape(name)}\s*\.\s*(push|pop|shift|unshift|splice|set|add|delete|clear|sort|reverse|fill)\s*\('
                             rf'|\b{re.escape(name)}\s*(\.[\w$]+|\[[^\]]*\])+\s*(=(?!=)|\+=|-=|\+\+|--)|Object\.assign\(\s*{re.escape(name)}\b'
                             rf'|\bdelete\s+{re.escape(name)}\s*[.\[]' + (rf'|(?<![.\w$]){re.escape(name)}\s*=(?![=>])' if keyword == 'let' else ''))
            muts, resets = [], []
            for k, other in enumerate(lines, 1):
                if k == n or not mut.search(other):
                    continue
                hook = next((h for h in hooks if h[1] <= k <= h[2]), None)
                (resets if hook and hook[0] in ('beforeEach', 'afterEach') else muts if not hook else []).append(k)
            if muts and not resets:
                self.add('shared_mutable', 'mutated_module_state', rel, n,
                         f'module-level {keyword} {name} holds a mutable value that {len(muts)} line(s) change '
                         f'(first at line {muts[0]}) and no beforeEach/afterEach resets', name, None,
                         details={'name': name, 'mutated_at': muts[:20], 'shared_by': tests}, tests=tests)

    def js_locale(self):
        """Tests that compare a literal with JS formatting that uses the machine locale (no explicit locale argument)."""
        calls = '|'.join(re.escape(c.split('(')[0].split('.')[-1]) for c in RULES['locale_calls_js'])
        machine = re.compile(rf'(?:\.|\bIntl\.)({calls})\s*\(\s*(\)|undefined\b|\[\s*\])')
        dependent: dict[str, dict] = {}
        for rel in self.js_files:
            if inv.in_test_area(rel):
                continue
            _, _, _, lines = self.js_parsed(rel)
            for n, ln in enumerate(lines, 1):
                m = machine.search(ln)
                if not m:
                    continue
                indent = len(ln) - len(ln.lstrip())
                for k in range(n - 1, 0, -1):
                    prev = lines[k - 1]
                    f = JS_FUNC.search(prev)
                    if f and (f.group(1) or f.group(2) or f.group(3)) in JS_KEYWORDS:
                        f = None
                    if f and len(prev) - len(prev.lstrip()) <= indent:
                        dependent.setdefault(f.group(1) or f.group(2) or f.group(3),
                                             {'file': rel, 'line': n, 'calls': set()})['calls'].add(m.group(1))
                        break
        names = '|'.join(map(re.escape, dependent))
        for item in self.inv['inventory']:
            rel = item['path']
            if item['kind'] != 'inline' or not jsparse.is_js(rel) or item['status'] != 'scanned':
                continue
            text, _, _, lines = self.js_parsed(rel)
            raw = text.split('\n')
            # a rendered component that formats with the machine locale, asserted by formatted text (1,234 or 12 Mar)
            jsx = [x for x in (re.findall(rf'<({names})\b', text) if names else []) if x[:1].isupper()]
            for n, ln in enumerate(lines, 1):
                if jsx and FORMATTED_LITERAL.search(raw[n - 1]) and re.search(r'getByText|findByText|queryByText|toHaveTextContent|'
                                                                             r'toContain|toBe|toEqual', ln):
                    info = dependent[jsx[0]]
                    self.add('locale_assumption', 'machine_locale_literal', rel, n,
                             f'asserts formatted text from <{jsx[0]}>, which formats with the machine locale '
                             f'({info["file"]}:{info["line"]}, {", ".join(sorted(info["calls"]))} with no locale argument)', None,
                             'formatted value', details={'function': f'{info["file"]}:{info["line"]}'},
                             tests=item.get('loaded_by') or [f'{rel}::*'])
                    continue
                if not re.search(r'\.\s*(toBe|toEqual|toStrictEqual|toContain|toMatch|toHaveTextContent)\s*\(\s*$|'
                                 r'\.\s*(toBe|toEqual|toStrictEqual|toContain|toMatch|toHaveTextContent)\s*\(\s*[\'"`]', ln):
                    continue
                if not re.search(r'\.\s*(toBe|toEqual|toStrictEqual|toContain|toMatch|toHaveTextContent)\s*\(\s*[\'"`]', raw[n - 1]):
                    continue
                direct = machine.search(ln)
                dep = re.search(rf'\b({names})\s*\(', ln) if names else None
                if direct:
                    self.add('locale_assumption', 'machine_locale_literal', rel, n,
                             f'compares {direct.group(1)}() with no locale argument against a literal; the output follows the '
                             'machine locale and time zone', None, 'formatted value', details={'function': None},
                             tests=item.get('loaded_by') or [f'{rel}::*'])
                elif dep:
                    info = dependent[dep.group(1)]
                    self.add('locale_assumption', 'machine_locale_literal', rel, n,
                             f'compares {dep.group(1)}() with a literal; it formats with the machine locale '
                             f'({info["file"]}:{info["line"]}, {", ".join(sorted(info["calls"]))} with no locale argument)', None,
                             'formatted value', details={'function': f'{info["file"]}:{info["line"]}'},
                             tests=item.get('loaded_by') or [f'{rel}::*'])

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
        self.js_now_comparisons(comparisons)
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
                    if jsparse.is_js(rel):
                        js_text, js_toks, _, _ = self.js_parsed(rel)
                        records = jsparse.records(js_text, rel, js_toks)
                    else:
                        records = datafiles.load_records(p, rel)
                except datafiles.NotAssessed as exc:
                    item['status'], item['reason'] = 'not_assessed', str(exc)
                    records = []
            n_before = len(self.cands)
            for r in records:
                self.personal_record(r, rel)
                if item['kind'] in ('fixture', 'seed', 'recording', 'factory', 'inline', 'support'):
                    self.time_bombs(r, rel, comparisons)
            text = p.read_text(encoding='utf-8', errors='replace')
            structured = {(r_line) for r in records for r_line in [v['line'] for v in r['fields'].values()]}
            for n, line in enumerate(text.split('\n'), 1):
                self.secrets(rel, n, line)
                self.environment(rel, n, line)
                if n not in structured:
                    for m in re.finditer(r'"([^"\n]{3,200})"|\'([^\'\n]{3,200})\'', line):
                        key = JS_KEY_BEFORE.search(line[:m.start()]) if jsparse.is_js(rel) else \
                            ATTR_BEFORE.search(line[:m.start()]) if rel.lower().endswith(MARKUP_EXT) else None
                        self.personal_value(rel, n, key.group(1) if key else None, m.group(1) or m.group(2), loose=bool(key))
            if jsparse.is_js(rel) and size_mb <= RULES['large_file_mb']:
                covered = {c['line'] for c in self.cands[n_before:] if c['category'] == 'time_bomb'}
                self.js_test_rules(rel, item, comparisons, seeds, covered)
                if item['kind'] in ('fixture', 'factory', 'snapshot'):
                    self.js_generated_ids(rel, item)
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
        self.js_locale()
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
