#!/usr/bin/env python3
"""Merge static and runtime findings into rated candidates, link causes, and propose a patch for missing keys.

    python merge_findings.py --resources run/resources.json --keys run/keys.json --code run/code.json
        [--runtime run/runtime/runtime.json] [--repo examples/bookclub] --out run/candidates.json [--patch run/missing_keys.patch]

Links (a runtime finding joins the static candidate it confirms; unlinked runtime findings are candidates of their own):
  missing_key shown on a page      -> missing_key for that key and locale
  raw placeholder {nom}            -> placeholder_mismatch whose value has it
  untranslated text in the pseudo run -> hardcoded_string with that text, or concatenation when only part of the text
                                       was translated
  format in a locale               -> the code that formats numbers/currency or dates by hand
  truncation                       -> fixed_width CSS whose selector matches the element
  rtl_mirroring (physical spacing) -> rtl_physical CSS whose rule matches the element
A static candidate a runtime finding confirms becomes `confirmed`; heuristic ones without one stay `possible`.

Ratings follow RUBRIC.md. The patch adds each missing key to its locale file with the source text copied in, for
translation; it is a proposal and is never applied.
"""
from __future__ import annotations
import argparse, difflib, json, re, sys
from pathlib import Path

WEIGHT = {'Critical': 4, 'High': 3, 'Medium': 2, 'Low': 1}
PARSER_VERIFIED = {'missing_key', 'icu_syntax', 'placeholder_mismatch', 'plural_categories', 'duplicate_key', 'unused_key', 'empty_value'}
CODE_LEVEL = {'hardcoded_string', 'concatenation', 'format', 'fixed_width', 'rtl_physical'}


def css_selector_at(path: Path, line: int) -> str | None:
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return None
    text = '\n'.join(lines[:line])
    i = text.rfind('{')
    if i < 0:
        return None
    j = max(text.rfind('}', 0, i), -1)
    return text[j + 1:i].strip()


def selector_matches(css_sel: str | None, element: str) -> bool:
    if not css_sel:
        return False
    last = css_sel.split(',')[0].strip().split()[-1]
    toks = re.findall(r'[.#]?[\w-]+', last)
    el = element.split(':in(')[0]
    el_toks = set(re.findall(r'[.#]?[\w-]+', el))
    return all(t in el_toks or (not t.startswith(('.', '#')) and el.startswith(t)) for t in toks)


def severity(c: dict) -> str:
    k = c['kind']
    if k == 'icu_syntax':
        return 'Critical'
    if k == 'plural_categories':
        return 'High' if c.get('has_other', True) else 'Critical'
    if k == 'missing_key':
        return 'Critical' if any(e.get('critical_path') for e in c['evidence']) else 'High'
    if k in ('placeholder_mismatch', 'empty_value', 'truncation', 'overlap'):
        return 'High'
    if k == 'hardcoded_string':
        return 'High' if c['status'] == 'confirmed' else 'Medium'
    if k in ('format', 'concatenation', 'rtl_mirroring', 'untranslated'):
        return 'Medium'
    if k == 'rtl_physical':
        return 'Medium' if c['status'] == 'confirmed' else 'Low'
    if k == 'fixed_width':
        return 'High' if c['status'] == 'confirmed' else 'Low'
    return 'Low'


def confidence(c: dict) -> str:
    k = c['kind']
    if k == 'untranslated':
        return 'low'
    if k in PARSER_VERIFIED:
        return 'high'
    if c['status'] == 'confirmed' or c['source'] == 'runtime':
        return 'medium' if k == 'hardcoded_string' else 'high'
    return 'low' if k in ('hardcoded_string', 'fixed_width', 'rtl_physical') else 'medium'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--resources', type=Path, required=True)
    ap.add_argument('--keys', type=Path, required=True)
    ap.add_argument('--code', type=Path, required=True)
    ap.add_argument('--runtime', type=Path)
    ap.add_argument('--repo', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--patch', type=Path)
    a = ap.parse_args()
    res = json.loads(a.resources.read_text(encoding='utf-8'))
    keys = json.loads(a.keys.read_text(encoding='utf-8'))
    code = json.loads(a.code.read_text(encoding='utf-8'))
    rt = json.loads(a.runtime.read_text(encoding='utf-8')) if a.runtime else None
    repo = a.repo or Path(res['repo'])
    shipped = sorted({(res['source_locale'] if l in ('default', 'template') else l) for s in res['sets'].values() for l in s['locales']}
                     - {res['source_locale']})
    cands: list[dict] = []
    # static key findings
    grouped: dict[tuple, dict] = {}
    for f in keys['findings']:
        gk = (f['kind'], f['set'], f['locale']) if f['kind'] == 'missing_key' else (f['kind'], f['set'], f['key'])
        c = grouped.setdefault(gk, {'kind': f['kind'], 'source': 'static', 'status': 'confirmed' if f['kind'] in PARSER_VERIFIED else 'possible',
                                    'set': f['set'], 'locales': [], 'keys': [], 'locations': [], 'details': [], 'evidence': [],
                                    'has_other': f.get('has_other', True), 'missing_in': f.get('missing_in')})
        if f['locale'] and f['locale'] not in c['locales']:
            c['locales'].append(f['locale'])
        if f['key'] and f['key'] not in c['keys']:
            c['keys'].append(f['key'])
        c['locations'].append({'file': f['file'], 'line': f['line']})
        c['details'].append(f['detail'])
        c.setdefault('values', []).append(f.get('value'))
    cands += grouped.values()
    # code findings, one candidate per (kind, text or file:line)
    cg: dict[tuple, dict] = {}
    for f in code['findings']:
        key = (f['kind'], f.get('text') or f'{f["file"]}:{f["line"]}')
        c = cg.setdefault(key, {'kind': f['kind'], 'source': 'static', 'status': 'possible', 'set': None, 'locales': list(shipped), 'keys': [],
                                'locations': [], 'details': [], 'evidence': [], 'text': f.get('text'), 'note': f.get('note'),
                                'selector': f.get('selector') or (css_selector_at(repo / f['file'], f['line']) if f['file'].endswith('.css') else None)})
        c['locations'].append({'file': f['file'], 'line': f['line']})
        c['details'].append(f['detail'])
    cands += cg.values()
    # runtime findings
    unlinked: dict[tuple, dict] = {}
    if rt:
        for r in rt['runs']:
            for f in r['findings']:
                ev = {'page': r['page'], 'locale': r['locale'], 'mode': r['mode'], 'screenshot': r.get('screenshot'), 'element': f['element'],
                      'text': f['text'], 'box': f['box'], 'measure': f['measure'], 'critical_path': r['critical_path']}
                target = None
                if f['kind'] == 'missing_key':
                    target = next((c for c in cands if c['kind'] == 'missing_key' and r['locale'] in c['locales'] and f['text'] in c['keys']), None)
                elif f['kind'] == 'raw_placeholder':
                    target = next((c for c in cands if c['kind'] == 'placeholder_mismatch' and r['locale'] in c['locales'] and
                                   any(v and p in v for v in c.get('values', []) for p in f['measure']['placeholders'])), None)
                elif f['kind'] == 'untranslated':
                    out = f['measure']['outside_pseudo']
                    if f['measure']['partly_translated']:
                        target = next((c for c in cands if c['kind'] == 'concatenation'), None)
                    else:
                        target = next((c for c in cands if c['kind'] == 'hardcoded_string' and c.get('text') and c['text'] in out), None)
                elif f['kind'] == 'format':
                    is_date = 'date' in json.dumps(f['measure'])
                    target = next((c for c in cands if c['kind'] == 'format' and ('date' in ' '.join(c['details'])) == is_date), None)
                elif f['kind'] == 'truncation':
                    target = next((c for c in cands if c['kind'] == 'fixed_width' and selector_matches(c.get('selector'), f['element'])), None)
                elif f['kind'] == 'rtl_mirroring' and 'property' in f['measure']:
                    target = next((c for c in cands if c['kind'] == 'rtl_physical' and selector_matches(c.get('selector'), f['element'])
                                   and f['measure']['property'].split('-width')[0] in ' '.join(c['details'] + [c.get('selector') or ''] +
                                                                                        [open_line(repo, l) for l in c['locations']])), None)
                if target:
                    target['status'] = 'confirmed'
                    target['evidence'].append(ev)
                    if r['locale'] not in target['locales'] and target['kind'] not in CODE_LEVEL:
                        target['locales'].append(r['locale'])
                    if target['kind'] in ('fixed_width', 'rtl_physical'):
                        target['runtime_kind'] = f['kind'] if f['kind'] != 'truncation' else 'truncation'
                else:
                    uk = (f['kind'], f['element'], json.dumps({k: v for k, v in f['measure'].items() if k in ('glyph', 'property', 'dir')}))
                    c = unlinked.setdefault(uk, {'kind': f['kind'], 'source': 'runtime', 'status': 'confirmed', 'set': None, 'locales': [],
                                                 'keys': [], 'locations': [], 'details': [], 'evidence': []})
                    if r['locale'] not in c['locales']:
                        c['locales'].append(r['locale'])
                    c['evidence'].append(ev)
                    c['details'].append(f'{f["kind"]} on {f["element"]}: {json.dumps(f["measure"], ensure_ascii=False)[:120]}')
    cands += unlinked.values()
    # rate
    for c in cands:
        c['locales'] = sorted(c['locales'])
        if c['kind'] in CODE_LEVEL:
            c['locales'] = list(shipped)
        n = len(c['locales'])
        c['reach'] = 3 if (n >= 5 or (n >= 2 and set(c['locales']) >= set(shipped))) else 2 if n >= 2 else 1
        pages = {e['page'] for e in c['evidence']}
        files = {l['file'] for l in c['locations']}
        c['leverage'] = 2 if len(pages) >= 2 or (c['kind'] == 'hardcoded_string' and len(files) >= 2) else 1
        c['severity'] = severity(c)
        c['confidence'] = confidence(c)
        c['score'] = WEIGHT[c['severity']] * c['reach'] * c['leverage']
        c['priority'] = 'P0' if c['score'] >= 24 else 'P1' if c['score'] >= 12 else 'P2' if c['score'] >= 6 else 'P3'
        if c['severity'] == 'Critical' and c['priority'] in ('P2', 'P3'):
            c['priority'] = 'P1'
        c.pop('values', None)
    order = {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3}
    cands.sort(key=lambda c: (order[c['priority']], -c['score'], c['kind'], json.dumps(c['keys'] or c['locations'])))
    for i, c in enumerate(cands, 1):
        c['id'] = f'K{i}'
    cands = [{'id': c['id'], **{k: v for k, v in c.items() if k != 'id'}} for c in cands]
    patch = proposed_patch(res, keys, repo) if a.patch else None
    if a.patch and patch:
        a.patch.write_text(patch['text'], encoding='utf-8')
    out = {'source_locale': res['source_locale'], 'shipped_locales': shipped, 'candidates': cands,
           'proposed_patch': {'path': a.patch.name, 'keys_added': patch['keys_added'], 'applied': False} if patch else None}
    a.out.write_text(json.dumps(out, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'{len(cands)} candidate(s) -> {a.out}', file=sys.stderr)
    return 0


def open_line(repo: Path, loc: dict) -> str:
    try:
        return (repo / loc['file']).read_text(encoding='utf-8').splitlines()[loc['line'] - 1]
    except (OSError, IndexError, TypeError):
        return ''


def proposed_patch(res: dict, keys: dict, repo: Path) -> dict | None:
    """Add every missing JSON key to its locale file with the source text copied in. Other formats are listed only."""
    by_file: dict[str, list[dict]] = {}
    for f in keys['findings']:
        if f['kind'] == 'missing_key' and f.get('missing_in', '').endswith('.json') and isinstance(f.get('source_value'), str):
            by_file.setdefault(f['missing_in'], []).append(f)
    if not by_file:
        return None
    text_out, added = [], {}
    for rel, fs in sorted(by_file.items()):
        orig = (repo / rel).read_text(encoding='utf-8')
        new = orig
        for f in reversed(fs):          # each insertion goes first in its object, so insert in reverse to keep source order
            parts = f['key'].split('.')
            leaf = json.dumps(parts[-1], ensure_ascii=False) + ': ' + json.dumps(f['source_value'], ensure_ascii=False)
            pos = 0
            for depth, part in enumerate(parts[:-1]):
                m = re.compile(r'"' + re.escape(part) + r'"\s*:\s*\{').search(new, pos)
                if not m:
                    nested = leaf
                    for p in reversed(parts[depth:-1]):
                        nested = json.dumps(p) + ': {' + nested + '}'
                    leaf = nested
                    break
                pos = m.end()
            else:
                m = None
            insert_at = pos if pos else new.index('{') + 1
            after = new[insert_at:].lstrip()
            sep = '' if after.startswith('}') else ', '
            new = new[:insert_at] + leaf + sep + new[insert_at:]
            added.setdefault(rel, []).insert(0, f['key'])
        json.loads(new)
        text_out += difflib.unified_diff(orig.splitlines(True), new.splitlines(True), f'a/{rel}', f'b/{rel}')
    header = ('# Proposed: add the keys missing from these locale files, with the source text copied in for translation.\n'
              '# Translate the values before applying. Nothing has been applied.\n')
    return {'text': header + ''.join(text_out), 'keys_added': added}


if __name__ == '__main__':
    sys.exit(main())
