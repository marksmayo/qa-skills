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


PARSERS = {'json': parse_json, 'arb': parse_json, 'po': parse_po, 'android': parse_android, 'strings': parse_strings,
           'stringsdict': parse_stringsdict}
