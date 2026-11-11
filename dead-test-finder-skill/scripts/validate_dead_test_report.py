#!/usr/bin/env python3
"""Validate a dead test report against the schema, the evidence files and the rubric (stdlib only).

    python validate_dead_test_report.py dead_test_report.json --base <dir the evidence paths are relative to>
        [--repo <repo>] [--write-summary]

The evidence files named in metadata.evidence_files (candidates, collection, runs,
neutralisations, similarity, skips) are read from --base. Checks: every candidate is a finding or
dismissed; every confirmed cant_fail or mock_only has a neutralisation where the test passed on
the original code and with the neutral body; every confirmed never_runs is missing from the
collect output; a duplicate has identical coverage and values (otherwise it is an overlap); a
stale skip has a done ticket or an age over the threshold; every proof passed on the original
and failed neutralised, in real runs; delete and merge are never applied; the tree hash is
unchanged across the runs. --repo checks every patch still applies and every changed file
exists. --write-summary fills in weights, scores, priorities and the summary.
"""
from __future__ import annotations
import argparse, json, re, shutil, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import unified_diff  # noqa: E402

SCHEMA = HERE.parent / 'schemas' / 'dead_test_report.schema.json'
WEIGHT = {'never_runs': 4, 'cant_fail': 4, 'mock_only': 3, 'async_not_awaited': 3, 'swallowed_failure': 3,
          'unreached_assertion': 2, 'stale_skip': 2, 'duplicate': 1, 'overlap': 1, 'weak': 1}
BANDS = [(12, 'P0'), (8, 'P1'), (4, 'P2'), (0, 'P3')]
CANDIDATE_TO = {'never_runs': {'never_runs'}, 'no_assertion': {'cant_fail', 'weak'}, 'tautology': {'cant_fail'},
                'unreached_assertion': {'unreached_assertion', 'cant_fail'}, 'swallowed_failure': {'swallowed_failure', 'cant_fail'},
                'async_not_awaited': {'async_not_awaited', 'cant_fail'}, 'mock_only': {'mock_only'}, 'skipped': {'stale_skip'},
                'duplicate': {'duplicate', 'overlap'}}
STRENGTHENING = {'cant_fail', 'async_not_awaited', 'weak', 'never_runs'}
BANNED = re.compile(r'\bno dead tests\b|\b(suite|tests?) (is|are) (healthy|effective|sound|clean)\b|\bhealthy (test )?suite\b|'
                    r'\ball (the )?tests (are )?(effective|meaningful|can fail)\b|\bdead[- ]test[- ]free\b|\bevery test can fail\b', re.I)


# ---------------------------------------------------------------- schema (subset of JSON Schema)
def schema_errors(value, schema: dict, root: dict, path: str = '$') -> list[str]:
    errs = []
    if 'enum' in schema and value not in schema['enum']:
        return [f'{path}: {value!r} is not one of {schema["enum"]}']
    t = schema.get('type')
    if t:
        types = t if isinstance(t, list) else [t]
        ok = any((tt == 'object' and isinstance(value, dict)) or (tt == 'array' and isinstance(value, list)) or
                 (tt == 'string' and isinstance(value, str)) or (tt == 'boolean' and isinstance(value, bool)) or
                 (tt == 'integer' and isinstance(value, int) and not isinstance(value, bool)) or
                 (tt == 'number' and isinstance(value, (int, float)) and not isinstance(value, bool)) or
                 (tt == 'null' and value is None) for tt in types)
        if not ok:
            return [f'{path}: expected {"/".join(types)}, got {type(value).__name__}']
    if isinstance(value, str):
        if len(value) < schema.get('minLength', 0):
            errs.append(f'{path}: shorter than {schema["minLength"]} characters')
        if schema.get('pattern') and not re.search(schema['pattern'], value):
            errs.append(f'{path}: {value!r} does not match {schema["pattern"]}')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 'minimum' in schema and value < schema['minimum']:
            errs.append(f'{path}: below {schema["minimum"]}')
        if 'maximum' in schema and value > schema['maximum']:
            errs.append(f'{path}: above {schema["maximum"]}')
    if isinstance(value, list):
        if len(value) < schema.get('minItems', 0):
            errs.append(f'{path}: needs at least {schema["minItems"]} item(s)')
        if 'items' in schema:
            for i, v in enumerate(value):
                errs += schema_errors(v, schema['items'], root, f'{path}[{i}]')
    if isinstance(value, dict):
        for k in schema.get('required', []):
            if k not in value:
                errs.append(f'{path}: missing {k!r}')
        props = schema.get('properties', {})
        for k, v in value.items():
            if k in props:
                errs += schema_errors(v, props[k], root, f'{path}.{k}')
            elif schema.get('additionalProperties') is False:
                errs.append(f'{path}: unexpected field {k!r}')
    return errs


# ---------------------------------------------------------------- computed
def weight(f: dict) -> int:
    if f['category'] == 'cant_fail' and f['confidence'] != 'confirmed':
        return 3
    return WEIGHT[f['category']]


def priority(f: dict) -> str:
    if (f['evidence'].get('collection') or {}).get('marker'):
        return 'P0'
    p = next(p for floor, p in BANDS if f['score'] >= floor)
    return 'P2' if f['confidence'] == 'suspected' and p in ('P0', 'P1') else p


def fill(rep: dict) -> None:
    for f in rep['findings']:
        f['weight'] = weight(f)
        f['score'] = f['weight'] * f['criticality']
        f['priority'] = priority(f)


def completeness(rep: dict, ev: dict) -> float:
    pk = ev['collection']['packages']
    collected = len([p for p in pk if p['status'] == 'ran']) / len(pk) if pk else 1.0
    neut_cands = [c for c in ev['candidates']['candidates']
                  if c['category'] in ('no_assertion', 'tautology', 'unreached_assertion', 'swallowed_failure', 'async_not_awaited', 'mock_only')]
    done = {r['test_id'] for r in ev['neutralisations']['results'] if not r.get('patch') and r['outcome'] != 'not_run'}
    noth = {r['test_id'] for r in ev['neutralisations']['results'] if not r.get('patch') and r['outcome'] == 'not_run'
            and r.get('note')}
    neut = len([c for c in neut_cands if c['test_ids'][0] in done or c['test_ids'][0] in noth]) / len(neut_cands) if neut_cands else 1.0
    skips = (ev.get('skips') or {}).get('skips', [])
    keyed = [s for s in skips if s.get('ticket')]
    tick = len([s for s in keyed if s['ticket']['checked']]) / len(keyed) if keyed else 1.0
    return round((collected + neut + tick) / 3, 4)


def summary(rep: dict, ev: dict) -> dict:
    fs = rep['findings']
    count = lambda key: {k: len([f for f in fs if f[key] == k]) for k in sorted({f[key] for f in fs})}  # noqa: E731
    return {'tests_defined': len(rep['tests']), 'collected': ev['collection']['collected'],
            'neutralised': len([t for t in rep['tests'] if t['status'] == 'neutralised']),
            'findings_by_category': count('category'), 'by_priority': count('priority'), 'evidence_completeness': completeness(rep, ev)}


# ---------------------------------------------------------------- checks
def case_status(runs: dict, run_id: str, test_id: str) -> str | None:
    name = test_id.split('::')[-1]
    for c in runs['cases']:
        if c['run_id'] == run_id and (c['name'] == name or c['name'] == name.split(' > ')[-1]):
            return c['status']
    return None


def check(rep: dict, ev: dict, repo: Path | None, base: Path) -> list[str]:
    errs = []
    cands = {c['id']: c for c in ev['candidates']['candidates']}
    defs = {d['id'] for d in ev['candidates']['definitions']}
    runs = ev['runs']
    run_ids = {r['id'] for r in runs['runs']}
    neut = ev['neutralisations']['results']
    sims = {p['candidate_id']: p for p in (ev.get('similarity') or {}).get('pairs', [])}
    skips = {s['candidate_id']: s for s in (ev.get('skips') or {}).get('skips', [])}
    # triage
    seen = [c for f in rep['findings'] for c in f['candidates']] + [d['candidate_id'] for d in rep['dismissed']]
    for c in sorted(set(cands) - set(seen), key=lambda x: int(x[1:])):
        errs.append(f'{c}: neither a finding nor dismissed')
    for c in sorted({x for x in seen if seen.count(x) > 1}):
        errs.append(f'{c}: appears more than once')
    for c in sorted(set(seen) - set(cands)):
        errs.append(f'{c}: not in the candidates file')
    # tests inventory
    tests = {t['id']: t for t in rep['tests']}
    if set(tests) != defs:
        errs.append(f'tests: must list every defined test exactly ({len(defs)} in the candidates file)')
    neutralised = {r['test_id'] for r in neut if not r.get('patch') and r['outcome'] != 'not_run'}
    for tid, t in tests.items():
        if (t['status'] == 'neutralised') != (tid in neutralised):
            errs.append(f'tests[{tid}]: status neutralised must match the neutralisation runs')
        if t['status'] == 'not_assessed' and not t.get('reason'):
            errs.append(f'tests[{tid}]: not_assessed needs a reason')
    if rep['collection']['not_collected'] != ev['collection']['not_collected'] or \
            rep['collection']['collected'] != ev['collection']['collected'] or rep['collection']['defined'] != ev['collection']['defined']:
        errs.append('collection: must match the collection file')
    for f in rep['findings']:
        w = f['id']
        linked = [cands[c] for c in f['candidates'] if c in cands]
        for c in linked:
            if f['category'] not in CANDIDATE_TO.get(c['category'], set()):
                errs.append(f'{w}: a {c["category"]} candidate cannot become {f["category"]}')
        if linked and f['location']['file'] not in {c['file'] for c in linked}:
            errs.append(f'{w}: location is not one of its candidates\' files')
        e = f['evidence']
        if f['category'] in ('cant_fail', 'mock_only', 'async_not_awaited', 'swallowed_failure', 'unreached_assertion') and \
                f['confidence'] == 'confirmed':
            n = e.get('neutralisation')
            rec = next((r for r in neut if r['test_id'] == (n or {}).get('test_id') and not r.get('patch')), None)
            if not n or not rec:
                errs.append(f'{w}: confirmed needs a neutralisation run of the test')
            else:
                if rec['outcome'] != 'passed_neutralised' or n['outcome'] != rec['outcome']:
                    errs.append(f'{w}: confirmed needs the test to pass with the neutral body (it {rec["outcome"].replace("_", " ")})')
                if rec['original']['test_status'] != 'passed':
                    errs.append(f'{w}: the test must pass on the original code first')
                for t in rec['targets']:
                    for v in t['variants']:
                        if v['run_id'] not in run_ids or case_status(runs, v['run_id'], rec['test_id']) != v['test_status']:
                            errs.append(f'{w}: neutralisation run {v["run_id"]} does not show {rec["test_id"]} {v["test_status"]}')
        if f['category'] == 'cant_fail' and f['confidence'] == 'confirmed':
            n = e.get('neutralisation') or {}
            if n.get('checks_no_exception') and not e.get('note'):
                errs.append(f'{w}: the raise variant failed, so the test does check that nothing raises; say so in evidence.note')
        if f['category'] == 'weak' and (e.get('neutralisation') or {}).get('outcome') == 'passed_neutralised' and \
                not (e.get('neutralisation') or {}).get('checks_no_exception'):
            errs.append(f'{w}: passes even when the code raises, so it is cant_fail, not weak')
        if f['category'] == 'never_runs' and f['confidence'] == 'confirmed':
            col = e.get('collection') or {}
            if not col.get('marker') and not all(t in ev['collection']['not_collected'] for t in f['test_ids']):
                errs.append(f'{w}: confirmed needs the collect output to show the test missing')
            if col.get('raw_output') and not (base / Path(rep['metadata']['evidence_files']['collection']).parent / col['raw_output']).is_file():
                errs.append(f'{w}: raw collect output {col["raw_output"]} is missing')
        if f['category'] in ('duplicate', 'overlap'):
            sim = next((sims[c] for c in f['candidates'] if c in sims), None)
            if not sim or not e.get('similarity'):
                errs.append(f'{w}: needs the similarity score and the coverage comparison')
            elif f['category'] == 'duplicate' and sim['verdict'] != 'duplicate':
                errs.append(f'{w}: coverage or values differ, so this is an overlap, not a duplicate')
            elif e['similarity'].get('coverage_identical') != sim.get('coverage_identical'):
                errs.append(f'{w}: coverage_identical differs from the similarity file')
        if f['category'] == 'stale_skip':
            s = next((skips[c] for c in f['candidates'] if c in skips), None)
            if not s or s['verdict'] != 'stale':
                errs.append(f'{w}: stale needs a done ticket or an age over the threshold in the skips file')
        p = f['proposal']
        if f['applied'] and p['action'] in ('delete', 'merge'):
            errs.append(f'{w}: deletions and merges are left to a person, never applied')
        if f['applied'] and (not rep['metadata']['apply'] or f['category'] not in STRENGTHENING):
            errs.append(f'{w}: only strengthening fixes are applied, and only with apply: true')
        if p.get('patch_path') and not (base / p['patch_path']).is_file():
            errs.append(f'{w}: patch {p["patch_path"]} is missing')
        if p['action'] != 'investigate' and not p.get('patch_path'):
            errs.append(f'{w}: a {p["action"]} proposal needs a patch file')
        pr = p.get('proof')
        if pr:
            rec = next((r for r in neut if r['test_id'] == pr['test_id'] and r.get('patch') and
                        Path(r['patch']).name == Path(p.get('patch_path') or '').name), None)
            if not rec:
                errs.append(f'{w}: no patched neutralisation run proves the fix')
            else:
                ok = rec['original']['test_status'] == 'passed' and rec['original']['run_id'] == pr['original_run_id']
                failed = [v['run_id'] for t in rec['targets'] for v in t['variants'] if v['test_status'] in ('failed', 'error')]
                want = 'proved' if ok and failed else 'not_proved'
                if pr['result'] != want or sorted(pr['neutralised_run_ids']) != sorted(failed):
                    errs.append(f'{w}: proof must be {want}, with the failing neutralised runs {failed}')
    for d in rep['dismissed']:
        s = skips.get(d['candidate_id'])
        if s and s['verdict'] == 'stale':
            errs.append(f'{d["candidate_id"]}: a stale skip is a finding, not a dismissal')
        c = cands.get(d['candidate_id'])
        if c and c['category'] == 'never_runs' and c['test_ids'][0] in ev['collection']['not_collected']:
            errs.append(f'{d["candidate_id"]}: the collect output confirms it never runs; it cannot be dismissed')
    # tree hashes
    for name in ('neutralisations', 'similarity'):
        e = ev.get(name)
        if e and e.get('tree_hash_before') != e.get('tree_hash_after'):
            errs.append(f'{name}: the working tree changed during the runs')
    if not rep['metadata']['apply'] and rep['files_changed']:
        errs.append('files_changed: nothing is changed without apply: true')
    if rep['metadata']['apply'] and sorted(rep['files_changed']) != sorted(
            {fp for f in rep['findings'] if f['applied'] for fp in patched_files(base / f['proposal']['patch_path'])}):
        errs.append('files_changed: must list exactly the files the applied patches touch')
    if repo is not None:
        for f in rep['findings']:
            pp = f['proposal'].get('patch_path')
            if pp and not f['applied'] and (base / pp).is_file():
                with tempfile.TemporaryDirectory() as tmp:
                    dest = Path(tmp) / 'r'
                    shutil.copytree(repo, dest, ignore=shutil.ignore_patterns('node_modules', '.git', '__pycache__'))
                    try:
                        unified_diff.apply_patch((base / pp).read_text(encoding='utf-8'), dest)
                    except SystemExit as exc:
                        errs.append(f'{f["id"]}: patch {pp} no longer applies ({exc})')
    # text
    texts = [('summary.headline', rep['summary'].get('headline', ''))] + [(f'limitations[{i}]', t) for i, t in enumerate(rep['limitations'])] + \
        [(f'{f["id"]}.why', f['why']) for f in rep['findings']] + [(f'{f["id"]}.proposal', f['proposal']['rationale']) for f in rep['findings']]
    for where, t in texts:
        m = BANNED.search(t)
        if m:
            errs.append(f'{where}: "{m.group(0)}" - say what was scanned and neutralised instead')
    want = json.loads(json.dumps(rep))
    fill(want)
    for a, b in zip(rep['findings'], want['findings']):
        for k in ('weight', 'score', 'priority'):
            if a[k] != b[k]:
                errs.append(f'{a["id"]}.{k}: {a[k]!r} but computed {b[k]!r} (run --write-summary)')
    for k, v in summary(rep, ev).items():
        if rep['summary'].get(k) != v:
            errs.append(f'summary.{k}: {rep["summary"].get(k)!r} but computed {v!r} (run --write-summary)')
    if any(f['priority'] == 'P0' for f in rep['findings']) and 'P0' not in rep['summary'].get('headline', ''):
        errs.append('summary.headline: name the P0 findings')
    return errs


def patched_files(p: Path) -> list[str]:
    return sorted({x for f in unified_diff.parse_patch(p.read_text(encoding='utf-8')) for x in (f['old'], f['new']) if x})


def load_evidence(rep: dict, base: Path) -> dict:
    ev = {}
    for k, v in rep['metadata']['evidence_files'].items():
        ev[k] = json.loads((base / v).read_text(encoding='utf-8')) if v else None
    return ev


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('report', type=Path)
    ap.add_argument('--base', type=Path, help='directory the evidence and patch paths are relative to (default: the report\'s)')
    ap.add_argument('--repo', type=Path)
    ap.add_argument('--write-summary', action='store_true')
    args = ap.parse_args(argv)
    rep = json.loads(args.report.read_text(encoding='utf-8'))
    base = (args.base or args.report.parent).resolve()
    try:
        ev = load_evidence(rep, base)
    except (OSError, KeyError, ValueError) as exc:
        print(f'INVALID: cannot read the evidence files ({exc})')
        return 1
    if args.write_summary:
        try:
            fill(rep)
            rep['summary'] = {'headline': (rep.get('summary') or {}).get('headline', ''), **summary(rep, ev)}
        except (KeyError, TypeError) as exc:
            print(f'INVALID: cannot compute the summary ({exc!r})')
            return 1
        args.report.write_text(json.dumps(rep, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    schema = json.loads(SCHEMA.read_text(encoding='utf-8'))
    errs = schema_errors(rep, schema, schema)
    if errs:
        print('INVALID (schema)')
        for e in errs[:40]:
            print('  ' + e)
        return 1
    errs = check(rep, ev, args.repo.resolve() if args.repo else None, base)
    if errs:
        print('INVALID')
        for e in errs:
            print('  ' + e)
        return 1
    s = rep['summary']
    print(f'VALID: {len(rep["findings"])} findings ({", ".join(f"{v} {k}" for k, v in s["by_priority"].items())}), '
          f'{s["tests_defined"]} tests defined, {s["collected"]} collected, {s["neutralised"]} neutralised')
    return 0


if __name__ == '__main__':
    sys.exit(main())
