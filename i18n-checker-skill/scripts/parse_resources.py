#!/usr/bin/env python3
"""Parse translation resource files into flat entries with file and line (stdlib).

Formats: JSON (flat or nested; i18next, FormatJS, vue-i18n), ARB, gettext PO/POT, Android strings.xml
(string, plurals, string-array; translatable="false" is skipped), iOS .strings, .stringsdict and
.xcstrings. YAML (Rails) is reported as not parsed.

Each entry: {key, value, file, line, kind: string|plural|array, forms: {category: text} for plurals}.
Duplicate keys in one file are reported (JSON keys repeated in an object, Android names repeated).
"""
from __future__ import annotations
import json, plistlib, re
import xml.etree.ElementTree as ET
from pathlib import Path


def json_key_lines(text: str) -> dict[str, int]:
    """Map each dotted key path in a JSON document to the line its key appears on."""
    out: dict[str, int] = {}
    stack: list[str | None] = []
    pending: str | None = None
    line, i, n = 1, 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '\n':
            line += 1
        elif ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == '\\' else 1
            s = text[i + 1:j]
            k = j + 1
            while k < n and text[k] in ' \t\r\n':
                k += 1
            if k < n and text[k] == ':' and stack and stack[-1] == '{':
                pending = s
                path = '.'.join(p for p in [x for x in stack if x not in ('{', '[')] + [s] if p is not None)
                out.setdefault(path, line)
            line += text[i:j].count('\n')
            i = j
        elif ch == '{':
            if pending is not None:
                stack.append(pending)
            elif stack and stack[-1] == '[':
                stack.append(None)
            stack.append('{')
            pending = None
        elif ch == '}':
            if stack:
                stack.pop()
            if stack and stack[-1] not in ('{', '['):
                stack.pop()
        elif ch == '[':
            if pending is not None:
                stack.append(pending)
            stack.append('[')
            pending = None
        elif ch == ']':
            if stack:
                stack.pop()
            if stack and stack[-1] not in ('{', '['):
                stack.pop()
        elif ch == ',':
            pending = None
        i += 1
    return out


def parse_json(path: Path) -> tuple[list[dict], list[dict]]:
    text = path.read_text(encoding='utf-8-sig')
    dups: list[dict] = []
    lines = json_key_lines(text)

    def hook(pairs):
        seen = set()
        for k, _ in pairs:
            if k in seen:
                dups.append({'key': k, 'file': path.as_posix()})
            seen.add(k)
        return dict(pairs)
    data = json.loads(text, object_pairs_hook=hook)
    entries = []

    def walk(obj, prefix):
        for k, v in obj.items():
            if k.startswith('@'):          # ARB metadata
                continue
            key = f'{prefix}.{k}' if prefix else k
            if isinstance(v, dict):
                walk(v, key)
            elif isinstance(v, list):
                entries.append({'key': key, 'value': v, 'file': path.as_posix(), 'line': lines.get(key), 'kind': 'array'})
            elif isinstance(v, str):
                entries.append({'key': key, 'value': v, 'file': path.as_posix(), 'line': lines.get(key), 'kind': 'string'})
    walk(data, '')
    # i18next plural suffixes: key_one, key_other ... become one plural entry
    groups: dict[str, dict] = {}
    plain = []
    for e in entries:
        m = re.fullmatch(r'(.+)_(zero|one|two|few|many|other)', e['key'])
        if m and e['kind'] == 'string':
            g = groups.setdefault(m.group(1), {'key': m.group(1), 'value': None, 'file': e['file'], 'line': e['line'], 'kind': 'plural',
                                               'forms': {}, 'style': 'i18next'})
            g['forms'][m.group(2)] = e['value']
        else:
            plain.append(e)
    return plain + list(groups.values()), [dict(d, line=lines.get(d['key'])) for d in dups]


def parse_po(path: Path) -> tuple[list[dict], list[dict]]:
    entries, cur, field, line_of = [], {}, None, None
    text = path.read_text(encoding='utf-8')

    def flush():
        if 'msgid' in cur and cur['msgid']:
            key = (cur.get('msgctxt', '') + '\x04' if cur.get('msgctxt') else '') + cur['msgid']
            if 'msgid_plural' in cur:
                forms = {f'{i}': cur.get(f'msgstr[{i}]', '') for i in range(6) if f'msgstr[{i}]' in cur}
                entries.append({'key': key, 'value': None, 'file': path.as_posix(), 'line': line_of, 'kind': 'plural', 'forms': forms,
                                'style': 'gettext', 'source_plural': cur['msgid_plural']})
            else:
                entries.append({'key': key, 'value': cur.get('msgstr', ''), 'file': path.as_posix(), 'line': line_of, 'kind': 'string'})
    for n, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if not s:
            flush()
            cur, field = {}, None
            continue
        if s.startswith('#'):
            continue
        m = re.match(r'(msgctxt|msgid_plural|msgid|msgstr(?:\[\d\])?)\s+"(.*)"$', s)
        if m:
            if m.group(1) == 'msgid' and 'msgid' in cur:
                flush()
                cur = {}
            field = m.group(1)
            cur[field] = m.group(2).encode().decode('unicode_escape') if '\\' in m.group(2) else m.group(2)
            if field == 'msgid':
                line_of = n
        elif s.startswith('"') and field:
            cur[field] += s[1:-1]
    flush()
    return entries, []


def parse_android(path: Path) -> tuple[list[dict], list[dict]]:
    text = path.read_text(encoding='utf-8')
    lines = {m.group(1): text[:m.start()].count('\n') + 1 for m in re.finditer(r'name="([^"]+)"', text)}
    root = ET.fromstring(text)
    entries, seen, dups = [], set(), []
    for el in root:
        name = el.get('name')
        if not name or el.get('translatable') == 'false':
            continue
        if name in seen:
            dups.append({'key': name, 'file': path.as_posix(), 'line': lines.get(name)})
        seen.add(name)
        if el.tag == 'string':
            entries.append({'key': name, 'value': ''.join(el.itertext()), 'file': path.as_posix(), 'line': lines.get(name), 'kind': 'string'})
        elif el.tag == 'plurals':
            forms = {it.get('quantity'): ''.join(it.itertext()) for it in el.findall('item')}
            entries.append({'key': name, 'value': None, 'file': path.as_posix(), 'line': lines.get(name), 'kind': 'plural', 'forms': forms,
                            'style': 'android'})
        elif el.tag == 'string-array':
            entries.append({'key': name, 'value': [''.join(it.itertext()) for it in el.findall('item')], 'file': path.as_posix(),
                            'line': lines.get(name), 'kind': 'array'})
    return entries, dups


def parse_strings(path: Path) -> tuple[list[dict], list[dict]]:
    raw = path.read_bytes()
    text = raw.decode('utf-16') if raw[:2] in (b'\xff\xfe', b'\xfe\xff') else raw.decode('utf-8-sig')
    entries, seen, dups = [], set(), []
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)"\s*;', text):
        line = text[:m.start()].count('\n') + 1
        if m.group(1) in seen:
            dups.append({'key': m.group(1), 'file': path.as_posix(), 'line': line})
        seen.add(m.group(1))
        entries.append({'key': m.group(1), 'value': m.group(2).replace('\\"', '"').replace('\\n', '\n'), 'file': path.as_posix(),
                        'line': line, 'kind': 'string'})
    return entries, dups


def parse_stringsdict(path: Path) -> tuple[list[dict], list[dict]]:
    d = plistlib.loads(path.read_bytes())
    entries = []
    for key, spec in d.items():
        for var, rule in spec.items():
            if isinstance(rule, dict) and rule.get('NSStringFormatSpecTypeKey') == 'NSStringPluralRuleType':
                forms = {k: v for k, v in rule.items() if k in ('zero', 'one', 'two', 'few', 'many', 'other')}
                entries.append({'key': key, 'value': spec.get('NSStringLocalizedFormatKey'), 'file': path.as_posix(), 'line': None,
                                'kind': 'plural', 'forms': forms, 'style': 'stringsdict'})
    return entries, []


def parse_xcstrings(path: Path) -> dict[str, list[dict]]:
    """One .xcstrings file holds every locale: returns {locale: entries}."""
    d = json.loads(path.read_text(encoding='utf-8'))
    out: dict[str, list[dict]] = {}
    for key, spec in (d.get('strings') or {}).items():
        if spec.get('shouldTranslate') is False:
            continue
        for loc, l in (spec.get('localizations') or {}).items():
            unit = l.get('stringUnit')
            if unit:
                out.setdefault(loc, []).append({'key': key, 'value': unit.get('value'), 'file': path.as_posix(), 'line': None, 'kind': 'string'})
            plural = ((l.get('variations') or {}).get('plural'))
            if plural:
                out.setdefault(loc, []).append({'key': key, 'value': None, 'file': path.as_posix(), 'line': None, 'kind': 'plural',
                                                'forms': {k: v['stringUnit']['value'] for k, v in plural.items()}, 'style': 'xcstrings'})
        if d.get('sourceLanguage') and d['sourceLanguage'] not in (spec.get('localizations') or {}):
            out.setdefault(d['sourceLanguage'], []).append({'key': key, 'value': key, 'file': path.as_posix(), 'line': None, 'kind': 'string'})
    return out


# ---------------------------------------------------------------- JS / TS message objects
# A literal object of strings assigned to a translations-like name, or a module's default export:
#   const translations = {en: {title: "Player"}, ru: {title: "Плеер"}};   export default {title: 'Player'};
# Read without executing anything: keys (identifiers or quoted), string values ('', "", `` without ${}),
# nested objects, comments and trailing commas. Any other value (a function, a number, a spread, a call) is
# counted in `skipped`, never guessed.
MESSAGE_VAR = re.compile(r'(?:\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]{1,80})?=\s*|\bexport\s+default\s+|\bmodule\.exports\s*=\s*)(?=\{)')
MESSAGE_NAME = re.compile(r'(?i)(translation|message|i18n|l10n|locale|string|dictionar|dict|lang|resource|text)')
JS_LOC = re.compile(r'^[a-z]{2,3}(?:[-_][A-Za-z]{2,4})?$')
# ISO 639-1 language codes: a file named fs.js or io.js is not a locale
ISO639_1 = set('''aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr cs cu cv cy da de dv dz ee el
en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht hu hy hz ia id ie ig ii ik io is it iu ja jv ka kg ki kj kk
kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg mh mi mk ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om or
os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw ta te tg th ti tk tl tn to tr ts tt tw ty
ug uk ur uz ve vi vo wa wo xh yi yo za zh zu'''.split())
# codes that are also common English words or file names (io.js, is.js, id.js): a locale only in a locale folder
AMBIGUOUS = {'io', 'is', 'it', 'as', 'or', 'to', 'no', 'an', 'am', 'be', 'id', 'ms', 'my', 'so', 'na', 'oc', 'os', 'ch'}
LOCALE_DIR = re.compile(r'(?i)locale|i18n|l10n|lang|translation|messages|intl')


def is_locale_name(stem: str, parent: str = '') -> bool:
    """A file stem that names a locale: de, pt-BR, zh_Hant."""
    code = stem.split('-')[0].split('_')[0].lower()
    if not JS_LOC.match(stem) or code not in ISO639_1:
        return False
    return code not in AMBIGUOUS or bool(LOCALE_DIR.search(parent))


class _JsObj:
    def __init__(self, text: str, start: int):
        self.t, self.i, self.n = text, start, len(text)
        self.skipped = 0

    def line(self, i: int) -> int:
        return self.t.count('\n', 0, i) + 1

    def ws(self):
        t = self.t
        while self.i < self.n:
            c = t[self.i]
            if c in ' \t\r\n':
                self.i += 1
            elif t.startswith('//', self.i):
                j = t.find('\n', self.i)
                self.i = self.n if j < 0 else j
            elif t.startswith('/*', self.i):
                j = t.find('*/', self.i + 2)
                self.i = self.n if j < 0 else j + 2
            else:
                break

    def string(self) -> str | None:
        q = self.t[self.i]
        j, out = self.i + 1, []
        while j < self.n and self.t[j] != q:
            if self.t[j] == '\\' and j + 1 < self.n:
                nxt = self.t[j + 1]
                out.append({'n': '\n', 't': '\t', 'r': '\r'}.get(nxt, nxt))
                j += 2
                continue
            if q == '`' and self.t.startswith('${', j):
                return None
            out.append(self.t[j])
            j += 1
        self.i = j + 1
        return ''.join(out)

    def skip_value(self):
        """Skip an expression up to the next , or closing bracket at this depth."""
        depth = 0
        while self.i < self.n:
            c = self.t[self.i]
            if c in '\'"`':
                j = self.i + 1
                while j < self.n and self.t[j] != c:
                    j += 2 if self.t[j] == '\\' else 1
                self.i = j + 1
                continue
            if self.t.startswith('//', self.i) or self.t.startswith('/*', self.i):
                self.ws()
                continue
            if c in '([{':
                depth += 1
            elif c in ')]}':
                if depth == 0:
                    return
                depth -= 1
            elif c == ',' and depth == 0:
                return
            self.i += 1

    def obj(self):
        """Parse {...} at self.i: {key: (value|dict, line)}."""
        assert self.t[self.i] == '{'
        self.i += 1
        out = {}
        while True:
            self.ws()
            if self.i >= self.n:
                raise ValueError('unterminated object')
            c = self.t[self.i]
            if c == '}':
                self.i += 1
                return out
            if c == ',':
                self.i += 1
                continue
            kline = self.line(self.i)
            if c in '\'"':
                key = self.string()
            elif c == '[' or self.t.startswith('...', self.i):
                self.skipped += 1
                self.skip_value()
                continue
            else:
                m = re.compile(r'[A-Za-z_$][\w$]*|\d+').match(self.t, self.i)
                if not m:
                    raise ValueError(f'unexpected {c!r} at line {kline}')
                key = m.group(0)
                self.i = m.end()
            self.ws()
            if self.i < self.n and self.t[self.i] == '(':      # a method: skip it
                self.skipped += 1
                self.skip_value()
                continue
            if self.i >= self.n or self.t[self.i] != ':':
                self.skipped += 1                                # shorthand property
                continue
            self.i += 1
            self.ws()
            c = self.t[self.i] if self.i < self.n else ''
            if c == '{':
                out[key] = (self.obj(), kline)
            elif c in '\'"`':
                save = self.i
                v = self.string()
                self.ws()
                if v is None or (self.i < self.n and self.t[self.i] not in ',}'):
                    self.i = save
                    self.skipped += 1
                    self.skip_value()
                else:
                    out[key] = (v, kline)
            else:
                self.skipped += 1
                self.skip_value()


def find_message_objects(text: str, stem: str, parent: str = "") -> list[dict]:
    """Literal objects of strings in a JS/TS file: [{name, locales: {locale|None: entries-ish dict}, line, skipped}].
    A top-level object whose keys are locale codes holding objects is one set with those locales; otherwise the
    object is one locale (the file's stem when it is a locale code)."""
    found = []
    for m in MESSAGE_VAR.finditer(text):
        name = m.group(1)
        if name and not MESSAGE_NAME.search(name):
            continue
        if not name and not is_locale_name(stem, parent):
            continue                     # a default export is a message module only in a locale-named file
        p = _JsObj(text, m.end())
        try:
            obj = p.obj()
        except (ValueError, AssertionError, IndexError):
            continue
        if not obj:
            continue
        loc_keys = [k for k in obj if is_locale_name(k, "locale")]
        if len(loc_keys) >= 2 and all(isinstance(obj[k][0], dict) for k in loc_keys) and len(loc_keys) == len(obj):
            locales = {k.replace('_', '-'): obj[k][0] for k in loc_keys}
        elif is_locale_name(stem, parent):
            locales = {stem.replace('_', '-'): obj}
        else:
            locales = {None: obj}
        found.append({'name': name or 'default', 'line': p.line(m.start()), 'locales': locales, 'skipped': p.skipped})
    return found


def js_entries(obj: dict, file: str, prefix: str = '') -> list[dict]:
    out = []
    for k, (v, line) in obj.items():
        key = f'{prefix}.{k}' if prefix else k
        if isinstance(v, dict):
            out += js_entries(v, file, key)
        else:
            out.append({'key': key, 'value': v, 'file': file, 'line': line, 'kind': 'string'})
    groups, plain = {}, []
    for e in out:
        m = re.fullmatch(r'(.+)_(zero|one|two|few|many|other)', e['key'])
        if m:
            g = groups.setdefault(m.group(1), {'key': m.group(1), 'value': None, 'file': e['file'], 'line': e['line'], 'kind': 'plural',
                                               'forms': {}, 'style': 'i18next'})
            g['forms'][m.group(2)] = e['value']
        else:
            plain.append(e)
    return plain + list(groups.values())


PARSERS = {'json': parse_json, 'arb': parse_json, 'po': parse_po, 'android': parse_android, 'strings': parse_strings,
           'stringsdict': parse_stringsdict}
