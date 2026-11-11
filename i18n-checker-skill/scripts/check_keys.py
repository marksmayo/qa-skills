#!/usr/bin/env python3
"""Check translation keys and messages across locales (stdlib, parser-verified).

    python check_keys.py run/resources.json --repo examples/bookclub --out run/keys.json

Per resource set, each locale against the source locale:
  missing_key           in the source, absent here
  untranslated          present with exactly the source text (a `possible` finding: some strings are the same)
  empty_value           present but empty
  placeholder_mismatch  ICU arguments, i18next {{names}} or printf specifiers (%d vs %s, %1$s) differ from the source
  icu_syntax            the message doesn't parse as ICU MessageFormat
  plural_categories     a plural (ICU, Android, i18next, stringsdict) lacks a category CLDR requires for this locale
  duplicate_key         the same key twice in one file
  unused_key            no code mentions the key; keys under a dynamic prefix (t('status.' + s)) are cantTell,
                        and a set whose platform has no code in the repo is not assessed
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
import icu_lite  # noqa: E402

CODE_EXT = {'web': ('.js', '.mjs', '.ts', '.tsx', '.jsx', '.vue', '.svelte', '.html', '.htm'),
            'android': ('.kt', '.java', '.xml'), 'ios': ('.swift', '.m', '.mm')}
SKIP = {'node_modules', '.git', 'dist', 'build', 'vendor', '__pycache__'}
DYNAMIC = re.compile(r"""\b(?:t|\$t|i18n\.t|translate|formatMessage)\s*\(\s*(?:['"]([\w.-]+\.)['"]\s*\+|`([\w.-]+\.)\$\{)""")


def platform(fmt: str) -> str:
    return 'android' if fmt == 'android' else 'ios' if fmt in ('strings', 'stringsdict', 'xcstrings') else 'web'


def code_text(repo: Path, plat: str) -> str:
    chunks = []
    for p in repo.rglob('*'):
        if p.is_file() and p.suffix in CODE_EXT[plat] and not (set(p.relative_to(repo).parts) & SKIP):
            rel = p.relative_to(repo).as_posix()
            if '/locales/' in f'/{rel}' or re.search(r'(^|/)values[^/]*/', rel):
                continue
            chunks.append(p.read_text(encoding='utf-8', errors='replace'))
    return '\n'.join(chunks)


def cldr_categories(locale: str, plurals: dict) -> list[str] | None:
    for cand in (locale, locale.split('-')[0]):
        if cand in plurals['cardinal']:
            return plurals['cardinal'][cand]
    return None


def finding(kind, set_name, locale, entry, detail, **kw):
    return {'kind': kind, 'set': set_name, 'locale': locale, 'key': entry['key'] if entry else kw.pop('key', None),
            'file': entry['file'] if entry else kw.pop('file', None), 'line': entry['line'] if entry else kw.pop('line', None),
            'detail': detail, **kw}


def ph(value: str) -> dict:
    return icu_lite.placeholders(value) if isinstance(value, str) else {'style': None, 'names': [], 'error': None, 'plurals': {}}


def check_set(name: str, s: dict, entries: dict, plurals: dict, source_locale: str) -> list[dict]:
    out = []
    src_code = s['source_locale']
    src = {e['key']: e for e in entries.get(src_code, [])}
    for loc, es in entries.items():
        real_loc = source_locale if loc in ('default', 'template') else loc
        cats = cldr_categories(real_loc, plurals)
        here = {e['key']: e for e in es}
        for e in es:
            for text in ([e['value']] if e['kind'] == 'string' else list((e.get('forms') or {}).values())):
                p = ph(text)
                if p['error']:
                    out.append(finding('icu_syntax', name, real_loc, e, p['error'], value=text))
                for arg, keys in p['plurals'].items():
                    if cats:
                        missing = [c for c in cats if c not in keys]
                        if missing:
                            out.append(finding('plural_categories', name, real_loc, e, f'{{{arg}, plural}} lacks {", ".join(missing)} '
                                               f'(CLDR {real_loc}: {", ".join(cats)})', missing=missing, has_other='other' in keys))
            if e['kind'] == 'plural' and cats and e.get('style') in ('android', 'i18next', 'stringsdict', 'xcstrings'):
                missing = [c for c in cats if c not in (e.get('forms') or {})]
                if missing:
                    out.append(finding('plural_categories', name, real_loc, e, f'plural {e["key"]} lacks {", ".join(missing)} '
                                       f'(CLDR {real_loc}: {", ".join(cats)})', missing=missing, has_other='other' in (e.get('forms') or {})))
            if e['kind'] == 'string' and isinstance(e['value'], str) and not e['value'].strip():
                out.append(finding('empty_value', name, real_loc, e, 'the value is empty'))
        if loc == src_code:
            continue
        for k, se in src.items():
            e = here.get(k)
            if e is None:
                out.append(finding('missing_key', name, real_loc, None,
                                   f'{k} is missing from {s["locales"][loc]["files"][0]} (source: {str(se.get("value") or se.get("forms"))[:80]!r})',
                                   key=k, file=se['file'], line=se['line'], missing_in=s['locales'][loc]['files'][0],
                                   source_value=se.get('value'), source_forms=se.get('forms')))
                continue
            if se['kind'] == 'string' and e['kind'] == 'string':
                if e['value'] == se['value'] and re.search(r'[A-Za-z]{2}', se['value'] or ''):
                    out.append(finding('untranslated', name, real_loc, e, f'same text as {src_code}: {se["value"]!r}'))
                a, b = ph(se['value']), ph(e['value'])
                if not a['error'] and not b['error'] and (a['names'] != b['names']):
                    out.append(finding('placeholder_mismatch', name, real_loc, e,
                                       f'{src_code} has {a["names"] or "none"} ({a["style"]}), {real_loc} has {b["names"] or "none"} ({b["style"]})',
                                       source_placeholders=a['names'], placeholders=b['names'], value=e['value']))
            elif se['kind'] == 'plural' and e['kind'] == 'plural':
                sa = {n for t in (se.get('forms') or {}).values() for n in ph(t)['names']}
                sb = {n for t in (e.get('forms') or {}).values() for n in ph(t)['names']}
                if sa != sb:
                    out.append(finding('placeholder_mismatch', name, real_loc, e, f'{src_code} plural uses {sorted(sa)}, {real_loc} uses {sorted(sb)}',
                                       source_placeholders=sorted(sa), placeholders=sorted(sb)))
    return out


def unused(name: str, s: dict, entries: dict, repo: Path) -> tuple[list[dict], dict]:
    plat = platform(s['format'])
    code = code_text(repo, plat)
    if not code.strip():
        return [], {'set': name, 'status': 'not_assessed', 'reason': f'no {plat} code in the repository to search'}
    prefixes = {m.group(1) or m.group(2) for m in DYNAMIC.finditer(code)}
    out, cant = [], []
    for e in entries.get(s['source_locale'], []):
        k = e['key']
        if plat == 'android':
            used = re.search(rf'R\.(string|plurals|array)\.{re.escape(k)}\b|@(string|plurals|array)/{re.escape(k)}\b', code)
        elif plat == 'ios':
            used = re.search(rf'"{re.escape(k)}"', code)
        else:
            used = re.search(rf'''['"`]{re.escape(k)}['"`]''', code)
        if used:
            continue
        if any(k.startswith(p) for p in prefixes):
            cant.append(k)
            continue
        out.append(finding('unused_key', name, s['source_locale'], e, f'no code mentions {k!r}'))
    return out, {'set': name, 'status': 'assessed', 'dynamic_prefixes': sorted(prefixes), 'cant_tell': cant}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('resources', type=Path)
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    res = json.loads(a.resources.read_text(encoding='utf-8'))
    plurals = json.loads((SKILL / 'data' / 'cldr-plurals.json').read_text(encoding='utf-8'))
    findings, unused_status = [], []
    for name, s in res['sets'].items():
        findings += check_set(name, s, res['entries'][name], plurals, res['source_locale'])
        u, st = unused(name, s, res['entries'][name], a.repo)
        findings += u
        unused_status.append(st)
    for d in res['duplicates']:
        findings.append({'kind': 'duplicate_key', 'set': None, 'locale': None, 'key': d['key'], 'file': d['file'], 'line': d.get('line'),
                         'detail': f'{d["key"]} appears twice'})
    coverage = {}
    for name, s in res['sets'].items():
        src_keys = {e['key'] for e in res['entries'][name].get(s['source_locale'], [])}
        for loc, es in res['entries'][name].items():
            if loc != s['source_locale'] and src_keys:
                real = res['source_locale'] if loc in ('default', 'template') else loc
                coverage[f'{name}:{real}'] = round(len(src_keys & {e['key'] for e in es}) / len(src_keys), 3)
    out = {'source_locale': res['source_locale'], 'findings': findings, 'unused_check': unused_status, 'key_coverage': coverage}
    a.out.write_text(json.dumps(out, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    from collections import Counter
    print(f'{len(findings)} static key finding(s) {dict(Counter(f["kind"] for f in findings))} -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
