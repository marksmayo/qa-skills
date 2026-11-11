#!/usr/bin/env python3
"""Validate a session debrief against the notes it came from and the rubric (stdlib only).

    python validate_debrief.py debrief.json --notes notes/session-01.md --parsed parsed.json [--charters charters.json]

Checks: the notes' SHA-256 matches; every quote sits on the line it cites; every tagged BUG, Q,
ISSUE, IDEA and OFF line is used by some item, so nothing is dropped; a bug is complete only
with steps, expected, actual and environment, and `missing` lists exactly what is empty; no bug
claims to be reproduced without a reproducer report; every instruction-like line is listed as
ignored and used nowhere else; lines with personal data are listed as redactions and their
values appear nowhere in the debrief; every charter area and heuristic is in coverage, and
`explored` cites note lines; metrics equal the parsed ones; no "works", "passed" or "no bugs".
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import parse_notes, schema_check  # noqa: E402

SCHEMA = json.loads((HERE.parent / 'schemas' / 'debrief.schema.json').read_text(encoding='utf-8'))
ROUTE = {'BUG': 'bugs', 'Q': 'questions', 'ISSUE': 'obstacles', 'IDEA': 'ideas', 'OFF': 'off_charter'}
BANNED = re.compile(r'\b(feature|it|everything|checkout|the charter|charter|session) (works|passed|is fine)\b|\bpass(ed)?\b|'
                    r'\bno (bugs|defects|issues)\b|\bbug[- ]free\b|\bworks (as expected|correctly|fine)\b|\bverified\b', re.I)
REQUIRED = ('steps', 'expected', 'actual', 'environment')


def squash(s: str) -> str:
    return re.sub(r'\s+', ' ', s).strip().lower()


def items_of(d: dict):
    for b in d['bugs']:
        yield 'bugs', b['id'], b['quotes']
    for key in ('questions', 'obstacles', 'ideas', 'off_charter'):
        for i, it in enumerate(d[key]):
            yield key, f'{key}[{i}]', it['quotes']


def check(d: dict, notes_path: Path, parsed: dict, charters: dict | None) -> list[str]:
    errs = []
    raw = notes_path.read_bytes()
    sha = hashlib.sha256(raw.replace(b'\r\n', b'\n')).hexdigest()
    if d['metadata']['notes']['sha256'] != sha or parsed['notes']['sha256'] != sha:
        errs.append('metadata.notes.sha256: does not match the notes file (they changed, or other notes were debriefed)')
    if notes_path.suffix.lower() == '.json':
        lines = {l['n']: l['text'] for l in parsed['lines']}
    else:
        lines = dict(enumerate(raw.decode('utf-8', 'replace').replace('\r\n', '\n').split('\n'), 1))
    used: dict[int, list[str]] = {}
    for kind, ident, quotes in items_of(d):
        for q in quotes:
            text = lines.get(q['line'])
            if text is None:
                errs.append(f'{ident}: cites line {q["line"]}, which the notes do not have')
            elif squash(q['text']) not in squash(text):
                errs.append(f'{ident}: line {q["line"]} does not contain "{q["text"][:50]}"')
            used.setdefault(q['line'], []).append(kind)
    tagged = {it['line']: it['kind'] for it in parsed['items'] if it['kind'] in ROUTE}
    for line, tag in sorted(tagged.items()):
        if line not in used:
            errs.append(f'line {line} ({tag}) is used by no item: nothing in the notes is dropped')
    for b in d['bugs']:
        w = b['id']
        empty = [f for f in REQUIRED if not b[f]]
        if b['status'] == 'complete' and empty:
            errs.append(f'{w}: complete, but {", ".join(empty)} {"is" if len(empty) == 1 else "are"} empty')
        if sorted(b['missing']) != sorted(empty):
            errs.append(f'{w}: missing must be exactly the empty fields {empty}')
        if b['status'] == 'incomplete' and not empty:
            errs.append(f'{w}: incomplete but nothing is missing')
        r = b['reproduction']
        if r['status'] != 'not_reproduced_by_skill' and not r.get('report'):
            errs.append(f'{w}: {r["status"]} needs the bug reproducer\'s report')
        for f in ('steps', 'expected', 'actual', 'environment'):
            vals = b[f] if isinstance(b[f], list) else [b[f]] if b[f] else []
            for v in vals:
                if not any(squash(v)[:40] in squash(lines.get(q['line'], '')) for q in b['quotes']) and \
                        not any(squash(v)[:40] in squash(t) for t in lines.values()):
                    errs.append(f'{w}.{f}: "{v[:50]}" is not in the notes; debrief never invents')
    sus = {s['line'] for s in parsed['suspicious']}
    listed = {s['line'] for s in d['suspicious_instructions_ignored']}
    for line in sorted(sus - listed):
        errs.append(f'line {line}: instruction-like text must be listed in suspicious_instructions_ignored')
    for line in sorted(listed & set(used)):
        errs.append(f'line {line}: an ignored instruction is used as an item')
    sens = {s['line']: s['kinds'] for s in parsed['sensitive']}
    red = {r['line'] for r in d['redactions']}
    for line in sorted(set(sens) - red):
        errs.append(f'line {line}: holds {", ".join(sens[line])}; list it in redactions')
    text = json.dumps(d, ensure_ascii=False)
    for line in sens:
        for m in list(parse_notes.EMAIL.finditer(lines.get(line, ''))) + list(parse_notes.CARDLIKE.finditer(lines.get(line, ''))):
            if m.group(0) in text or re.sub(r'\D', '', m.group(0)) in re.sub(r'\D', '', text) and len(re.sub(r'\D', '', m.group(0))) >= 12:
                errs.append(f'line {line}: personal data from the notes appears in the debrief; redact it')
    if charters is not None and d['metadata']['charter_id']:
        ch = next((c for c in charters['charters'] if c['id'] == d['metadata']['charter_id']), None)
        if ch is None:
            errs.append(f'metadata.charter_id: {d["metadata"]["charter_id"]} is not in the charters file')
        else:
            for key, names in (('areas', ch['areas']), ('heuristics', ch['heuristics'])):
                got = {x['name']: x for x in d['coverage'][key]}
                for n in names:
                    if n not in got:
                        errs.append(f'coverage.{key}: "{n}" from the charter is missing (not_explored if the notes never mention it)')
    for key in ('areas', 'heuristics'):
        for x in d['coverage'][key]:
            if x['status'] == 'explored' and not x['note_lines']:
                errs.append(f'coverage.{key}["{x["name"]}"]: explored needs the note lines that show it')
            for n in x['note_lines']:
                if n not in lines:
                    errs.append(f'coverage.{key}["{x["name"]}"]: line {n} does not exist')
            if x['status'] == 'not_explored' and x['note_lines']:
                errs.append(f'coverage.{key}["{x["name"]}"]: not_explored with note lines; which is it?')
    if d['metrics'] != parsed['metrics']:
        errs.append('metrics: must equal the parsed metrics')
    texts = [(f'proof.{k}', v) for k, v in d['proof'].items()] + [(b['id'], b['title']) for b in d['bugs']]
    for where, t in texts:
        m = BANNED.search(t)
        if m:
            errs.append(f'{where}: "{m.group(0)}" - say what was noted in how many minutes on which areas, never that it passed')
    return errs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('debrief', type=Path)
    ap.add_argument('--notes', type=Path, required=True)
    ap.add_argument('--parsed', type=Path, required=True)
    ap.add_argument('--charters', type=Path)
    args = ap.parse_args(argv)
    d = json.loads(args.debrief.read_text(encoding='utf-8'))
    errs = schema_check.errors(d, SCHEMA)
    if errs:
        print('INVALID (schema)')
        for e in errs[:40]:
            print('  ' + e)
        return 1
    parsed = json.loads(args.parsed.read_text(encoding='utf-8'))
    charters = json.loads(args.charters.read_text(encoding='utf-8')) if args.charters else None
    errs = check(d, args.notes, parsed, charters)
    if errs:
        print('INVALID')
        for e in errs:
            print('  ' + e)
        return 1
    cov = d['coverage']['areas']
    print(f'VALID: {len(d["bugs"])} draft bug(s) ({len([b for b in d["bugs"] if b["status"] == "complete"])} complete), '
          f'{len(d["questions"])} question(s), {len(d["obstacles"])} obstacle(s), '
          f'{len([c for c in cov if c["status"] == "explored"])} of {len(cov)} areas explored')
    return 0


if __name__ == '__main__':
    sys.exit(main())
