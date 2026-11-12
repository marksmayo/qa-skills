#!/usr/bin/env python3
"""Canary values, their encodings, and the search and redaction every capture goes through.

    python match_canaries.py new --out run/canaries.json [--id 7f3a]          # a fresh synthetic canary set
    python match_canaries.py scan capture.har --canaries run/canaries.json --out matches.json   # search a HAR (e.g. from the mobile app auditor)

Canaries are synthetic and unique per run: an email under example.invalid, a reserved fictional phone
number, a name, a date of birth and a health-like free-text reason, each carrying the set id. Every value
is searched for raw (case-insensitive), URL-encoded, Base64 and Base64url, and as lowercase hex SHA-256,
SHA-1 and MD5 of the trimmed lowercased value (and of the E.164 form for phone numbers). That is how a
"hashed email" sent to an ad platform shows up.

redact() replaces every match with <canary:FIELD:ENCODING>, and anything else that looks like an email
address, phone number or card number with <redacted:TYPE>, so no captured value is ever written out.
"""
from __future__ import annotations
import argparse, base64, hashlib, json, re, secrets, sys
from pathlib import Path
from urllib.parse import quote, quote_plus, unquote_plus

# ACMA reserves 0491 570 xxx-range numbers for use in drama and fiction; this one is on its list.
PHONE = '0491 570 156'
FIELDS = {
    'name': ('identity', 'Qa Canary {id}'),
    'email': ('contact', 'qa.canary.{id}@example.invalid'),
    'phone': ('contact', PHONE),
    'dob': ('date_of_birth', '1961-07-13'),
    'reason': ('health', 'canary {id} knee pain after a fall'),
}
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
EMAIL_ENC = re.compile(r'[A-Za-z0-9._%+-]+(%2540|%40|@)[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
PHONE_RX = re.compile(r'(?<![\w.])(\+?61[ -]?4\d{2}[ -]?\d{3}[ -]?\d{3}|04\d{2}[ -]?\d{3}[ -]?\d{3}|\+?1[ -]?\(?\d{3}\)?[ -]?\d{3}[ -]?\d{4}|\+44[ -]?7\d{3}[ -]?\d{6})(?![\w])')
CARD = re.compile(r'(?<!\d)(?:\d[ -]?){13,19}(?!\d)')


def luhn(digits: str) -> bool:
    d = [int(x) for x in digits][::-1]
    return len(d) >= 13 and sum(d[0::2] + [sum(divmod(2 * x, 10)) for x in d[1::2]]) % 10 == 0


EPOCH_MS = (946684800000, 4102444800000)   # 2000-01-01 to 2100-01-01 in milliseconds


def card_like(text: str, m: re.Match) -> bool:
    """True when a CARD match looks like a payment card number: 13-19 digits passing Luhn, not part of a decimal
    (no '.' or ',' digit just before or after it, as in 205.1234567890123), not an epoch-millisecond timestamp
    (13 digits between 2000 and 2100), not one digit repeated, and not glued to letters (an id such as v1-...)."""
    digits = re.sub(r'\D', '', m.group(0))
    if not 13 <= len(digits) <= 19 or not luhn(digits) or len(set(digits)) == 1:
        return False
    s = m.start()
    e = s + len(m.group(0).rstrip(' -'))
    if s >= 2 and text[s - 1] in '.,' and text[s - 2].isdigit():
        return False
    if re.match(r'[.,]\d', text[e:e + 2]):
        return False
    if (s and (text[s - 1].isalpha() or text[s - 1] == '_')) or (e < len(text) and (text[e].isalpha() or text[e] == '_')):
        return False
    if len(digits) == 13 and EPOCH_MS[0] <= int(digits) < EPOCH_MS[1] and re.fullmatch(r'\d+', m.group(0).strip()):
        return False
    return True


def decode_body(buf: bytes | None, content_encoding: str | None = None) -> tuple[str | None, str]:
    """A request body as text for searching, and how it was read: text, gzip, deflate, br, or binary (not
    searched). Compressed bodies (Content-Encoding, or gzip/zlib magic bytes, as session-replay and analytics SDKs
    send them) are decompressed first; brotli only when the brotli module is installed."""
    import gzip, zlib
    if not buf:
        return None, 'empty'
    enc = (content_encoding or '').lower().strip()
    tries = []
    if enc in ('gzip', 'x-gzip') or buf[:2] == b'\x1f\x8b':
        tries.append(('gzip', lambda b: gzip.decompress(b)))
    if enc == 'deflate' or (len(buf) > 1 and buf[0] == 0x78 and (buf[0] * 256 + buf[1]) % 31 == 0):
        tries.append(('deflate', lambda b: zlib.decompress(b)))
        tries.append(('deflate', lambda b: zlib.decompress(b, -15)))
    if enc == 'br':
        try:
            import brotli  # type: ignore
            tries.append(('br', brotli.decompress))
        except ImportError:
            return None, 'br_not_searched'
    for name, fn in tries:
        try:
            out = fn(buf)
            return out.decode('utf-8'), name
        except Exception:  # noqa: BLE001 - not that encoding, or not text
            continue
    try:
        return buf.decode('utf-8'), 'text'
    except UnicodeDecodeError:
        text = buf.decode('latin-1')
        printable = sum(ch.isprintable() or ch in '\r\n\t' for ch in text) / max(1, len(text))
        return (text, 'text_latin1') if printable > 0.95 else (None, 'binary_not_searched')


def new_set(set_id: str | None = None) -> dict:
    sid = set_id or secrets.token_hex(2)
    return {'id': sid, 'synthetic': True, 'note': 'Synthetic canary values; none belongs to a real person.',
            'fields': {k: {'category': cat, 'value': tpl.format(id=sid)} for k, (cat, tpl) in FIELDS.items()}}


def encodings(field: str, value: str) -> dict[str, str]:
    norm = value.strip().lower()
    forms = {'raw': value, 'url': quote(value, safe=''), 'url_plus': quote_plus(value), 'url_double': quote(quote(value, safe=''), safe=''),
             'base64': base64.b64encode(value.encode()).decode().rstrip('='),
             'base64url': base64.urlsafe_b64encode(value.encode()).decode().rstrip('='),
             'sha256': hashlib.sha256(norm.encode()).hexdigest(), 'sha1': hashlib.sha1(norm.encode()).hexdigest(),
             'md5': hashlib.md5(norm.encode()).hexdigest()}
    if field == 'phone':
        digits = re.sub(r'\D', '', value)
        e164 = '+61' + digits[1:] if digits.startswith('0') else '+' + digits
        forms.update({'digits': digits, 'e164': e164, 'e164_url': quote(e164, safe=''),
                      'sha256_e164': hashlib.sha256(e164.encode()).hexdigest()})
    out, seen = {}, set()
    for k, v in forms.items():
        if v and v not in seen:
            seen.add(v)
            out[k] = v
    return out


class Matcher:
    def __init__(self, canaries: dict):
        self.canaries = canaries
        self.table = []  # (field, encoding, needle), longest first so redaction never leaves a partial value
        for f, c in canaries['fields'].items():
            for enc, s in encodings(f, c['value']).items():
                self.table.append((f, enc, s))
        self.table.sort(key=lambda x: -len(x[2]))

    def find(self, text: str | None) -> list[dict]:
        if not text:
            return []
        hay = text.lower()
        dec = unquote_plus(text).lower() if '%' in text or '+' in text else hay
        out, seen = [], set()
        for f, enc, s in self.table:
            n = s.lower()
            if (n in hay or n in dec) and (f, enc) not in seen:
                seen.add((f, enc))
                out.append({'field': f, 'category': self.canaries['fields'][f]['category'], 'encoding': enc})
        # a raw match also matches its url form when the text was decoded; keep the most specific
        return out

    def redact(self, text: str | None) -> str | None:
        if text is None:
            return None
        for f, enc, s in self.table:
            text = re.sub(re.escape(s), f'<canary:{f}:{enc}>', text, flags=re.I)
        text = EMAIL_ENC.sub('<redacted:email>', text)
        text = PHONE_RX.sub('<redacted:phone>', text)
        text = CARD.sub(lambda m: '<redacted:card>' + m.group(0)[len(m.group(0).rstrip(' -')):]
                        if card_like(m.string, m) else m.group(0), text)
        return text


def scan_har(har: Path, m: Matcher) -> list[dict]:
    log = json.loads(har.read_text(encoding='utf-8'))
    out = []
    for e in log['log']['entries']:
        req = e['request']
        parts = {'url': req['url'], 'body': (req.get('postData') or {}).get('text'),
                 'headers': ' '.join(f'{h["name"]}: {h["value"]}' for h in req.get('headers', []))}
        hits = [dict(h, where=k) for k, v in parts.items() for h in m.find(v)]
        if hits:
            out.append({'method': req['method'], 'url': m.redact(req['url']), 'matches': hits})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    n = sub.add_parser('new')
    n.add_argument('--out', type=Path, required=True)
    n.add_argument('--id')
    s = sub.add_parser('scan')
    s.add_argument('har', type=Path)
    s.add_argument('--canaries', type=Path, required=True)
    s.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    if a.cmd == 'new':
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(new_set(a.id), indent=1) + '\n', encoding='utf-8', newline='\n')
        print(f'canary set -> {a.out}')
        return 0
    m = Matcher(json.loads(a.canaries.read_text(encoding='utf-8')))
    res = scan_har(a.har, m)
    a.out.write_text(json.dumps(res, indent=1) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(res)} request(s) carried a canary -> {a.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
