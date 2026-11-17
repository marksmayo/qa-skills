#!/usr/bin/env python3
"""Validate a test impact report against the schema, the evidence and the rubric's honesty rules (stdlib only).

    python validate_impact_report.py impact_report.json [--selection selection.json] [--recompute]
        [--replay replay.json] [--runs runs.json] [--write-summary]

--selection checks the report's sources, changes, selected tests, blind spots, counts and command
are exactly select_tests.py's output. --recompute re-runs the selection from the inputs recorded
in it (paths relative to the current directory) and checks the result is the same. --replay
checks the recall figures and the misses against replay.py's output. --runs checks a verify run:
every test file that failed in a `verify-full <package>` run outside the selection must be
listed (a failed case is inside the selection when its file is selected, or when its test id is
one of a selected file's `test_ids`: libtest unit tests may be recorded under their crate's root
file). A failure listed in `verify.known_failures` is not a miss when the runs it names bear it
out: `flaky` needs a pass and a failure of that test on the same code, `baseline` a failure in a
`verify-base ...` run; each is still named in limitations. --write-summary fills in the computed
summary fields (the headline stays yours).
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True   # no __pycache__ in the skill folder when this script imports its siblings
import import_graph, stats  # noqa: E402

SCHEMA = HERE.parent / 'schemas' / 'impact_report.schema.json'
KINDS = import_graph.KINDS
COPIED = ('sources', 'changes', 'selected', 'blind_spots', 'total_tests', 'not_selected_count', 'command')
EVIDENCE_SOURCES = {'coverage', 'import_graph', 'framework_tools', 'journeys', 'naming', 'co_failure'}
SCOPES = {'global_config': {'full_suite', 'package', 'language', 'no_existing_test'}, 'shared_fixture': {'fixture_reach', 'package'},
          'dynamic_reference': {'package', 'unresolved_importers'}, 'data_or_schema': {'package_and_contract'},
          'unmapped_source': {'package', 'full_suite'},
          'deleted_or_renamed': {'importers'}, 'non_code': {'e2e_and_visual'}, 'huge_change': {'full_suite'},
          'fixture_project': {'fixture_host'}}
HIGH_RISK = {'global_config', 'shared_fixture', 'dynamic_reference', 'deleted_or_renamed', 'huge_change'}
BANNED = re.compile(r'\bsafe(ly)? to (skip|omit|ignore|drop)\b|\bsafely (skip|skipped|omit|omitted|ignored?)\b|'
                    r'\b(selection|subset|it) is safe\b|\bsafe selection\b|\bno risk\b|\brisk[- ]free\b|\bguarantee[sd]?\b|'
                    r'\b(can(no|\')t|won\'t|will not) (break|fail)\b|\bskippable\b|\bunaffected tests\b|\bnothing else (is|was) affected\b', re.I)
PCT = re.compile(r'(\d+(?:\.\d+)?)\s?%')
WHEN = re.compile(r'merge|nightly|daily|schedul|every|release|main\b', re.I)


# ---------------------------------------------------------------- schema (subset of JSON Schema)
def schema_errors(value, schema: dict, root: dict, path: str = '$') -> list[str]:
    if '$ref' in schema:
        node = root
        for part in schema['$ref'].lstrip('#/').split('/'):
            node = node[part]
        return schema_errors(value, node, root, path)
    errs = []
    if 'const' in schema and value != schema['const']:
        return [f'{path}: must be {schema["const"]!r}']
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
        extra = schema.get('additionalProperties', True)
        for k, v in value.items():
            if k in props:
                errs += schema_errors(v, props[k], root, f'{path}.{k}')
            elif extra is False:
                errs.append(f'{path}: unexpected field {k!r}')
    return errs


# ---------------------------------------------------------------- computed fields
def completeness(sources: list[dict]) -> float | None:
    vals = [s['factor'] for s in sources if s.get('factor') is not None and s['status'] != 'not_applicable']
    return round(sum(vals) / len(vals), 4) if vals else None


def recall_text(r: dict) -> str | None:
    if not r or not r.get('failures_total'):
        return None
    sel = r['failures_total'] - r['failures_missed']
    return (f'{sel} of {r["failures_total"]} failures selected in {r["changes_replayed"]} replayed changes '
            f'({100 * r["recall"]:.1f}%, 95% CI {100 * r["wilson_low"]:.1f}%-{100 * r["wilson_high"]:.1f}%)')


def computed_summary(rep: dict) -> dict:
    sel = rep['selected']
    r = rep.get('replay')
    measured = bool(r and r.get('failures_total'))
    return {
        'selected': len(sel), 'total_tests': rep['total_tests'],
        'widened_by_blind_spots': len([s for s in sel if s['strength'] == 0]),
        'blind_spots': len(rep['blind_spots']),
        'blind_spots_labelled_only': len([b for b in rep['blind_spots'] if b['action'] == 'labelled']),
        'full_suite_selected': bool(rep['total_tests']) and len(sel) == rep['total_tests'],
        'evidence_completeness': completeness(rep['sources']),
        'recall_status': 'measured' if measured else 'not_assessed',
        'recall_text': recall_text(r) if measured else None,
        'sources_not_run': sorted(s['name'] for s in rep['sources'] if s['status'] in ('not_run', 'errored')),
    }


def all_text(rep: dict) -> list[tuple[str, str]]:
    out = [('summary.headline', rep['summary'].get('headline', ''))]
    out += [(f'limitations[{i}]', t) for i, t in enumerate(rep.get('limitations', []))]
    out += [('recommendations.full_suite', rep['recommendations'].get('full_suite', ''))]
    out += [(f'recommendations.other[{i}]', t) for i, t in enumerate(rep['recommendations'].get('other', []))]
    out += [(f'blind_spots[{b["id"]}].comment', b.get('comment', '')) for b in rep['blind_spots']]
    for m in (rep.get('replay') or {}).get('misses', []):
        out += [(f'replay.misses[{m["change"]}]', m.get('explanation', '') + ' ' + m.get('fix', ''))]
    if rep.get('verify'):
        out.append(('verify.note', rep['verify'].get('note', '')))
        out += [(f'verify.known_failures[{k.get("id")}].note', str(k.get('note') or ''))
                for k in rep['verify'].get('known_failures') or []]
    out += [(f'notes[{i}]', t) for i, t in enumerate(rep.get('notes', []))]
    return out


# ---------------------------------------------------------------- checks
def check(rep: dict, selection: dict | None, replay: dict | None, runs: dict | None) -> list[str]:
    errs = []
    mode = rep['metadata']['mode']
    src = {s['name']: s for s in rep['sources']}
    tests_all = {t for p in rep['metadata'].get('packages', selection['packages'] if selection else []) for t in p.get('test_files', [])}
    pkg_tests = {p['root']: p.get('test_files', []) for p in rep['metadata'].get('packages', selection['packages'] if selection else [])}

    # copied sections
    if selection:
        for k in COPIED:
            mine = rep.get(k)
            if k == 'blind_spots':
                mine = [{f: v for f, v in b.items() if f != 'comment'} for b in mine]
            if mine != selection.get(k):
                errs.append(f'{k}: differs from {rep["metadata"]["selection_file"]} (copy it unchanged)')
        if rep['metadata']['mode'] != selection['mode']:
            errs.append('metadata.mode: differs from the selection')
        if rep['metadata']['change'].get('diff_sha256') != selection['change']['diff_sha256']:
            errs.append('metadata.change.diff_sha256: differs from the selection')
        if rep['metadata']['repo_code_hash'] != selection['repo_code_hash']:
            errs.append('metadata.repo_code_hash: differs from the selection')
        if rep['metadata']['file_kinds_sha256'] != selection['inputs']['file_kinds_sha256']:
            errs.append('metadata.file_kinds_sha256: differs from the selection')
        if rep['metadata'].get('packages') != selection['packages']:
            errs.append('metadata.packages: differs from the selection')
        if 'language_coverage' in selection and rep.get('language_coverage') != selection['language_coverage']:
            errs.append('language_coverage: differs from the selection (copy it unchanged)')
        if rep.get('excluded') != selection.get('excluded'):
            errs.append('excluded: differs from the selection (copy the fixture projects and folders it left out)')
    if len({s['name'] for s in rep['sources']}) != len(rep['sources']) or set(src) != EVIDENCE_SOURCES:
        errs.append(f'sources: need exactly one entry for each of {sorted(EVIDENCE_SOURCES)}')

    # mode
    if mode == 'balanced':
        if src.get('coverage', {}).get('status') not in ('ran',):
            errs.append('balanced mode needs a coverage map that ran and is not stale; use strict')
        if src.get('co_failure', {}).get('status') != 'ran':
            errs.append('balanced mode needs history; use strict')

    # selected tests
    seen_orders = []
    selected_files = {s['file'] for s in rep['selected']}
    blind_ids = {b['id'] for b in rep['blind_spots']}
    for s in rep['selected']:
        where = f'selected[{s["test_id"]}]'
        if not s['reasons']:
            errs.append(f'{where}: no reason')
            continue
        ev = [r for r in s['reasons'] if r['source'] != 'blind_spot']
        for r in s['reasons']:
            if r['source'] in EVIDENCE_SOURCES and src.get(r['source'], {}).get('status') not in ('ran', 'stale'):
                errs.append(f'{where}: a reason cites {r["source"]}, which is recorded {src.get(r["source"], {}).get("status")}')
            if r['source'] == 'blind_spot' and r['evidence'].split()[0] not in blind_ids:
                errs.append(f'{where}: a widening reason cites {r["evidence"].split()[0]}, which is not a blind spot')
        strength = max([r['strength'] for r in ev], default=0)
        if s['strength'] != strength:
            errs.append(f'{where}: strength {s["strength"]} but its strongest evidence is {strength}')
        rule_ok = strength >= 2 or (strength == 1 and len({r['source'] for r in ev}) >= 2) or \
            any(r['source'] == 'blind_spot' for r in s['reasons'])
        if not rule_ok:
            errs.append(f'{where}: selected on a single strength-1 source and no widening; that does not meet the rule')
        if tests_all and s['file'] not in tests_all:
            errs.append(f'{where}: not a test file in the inventory')
        seen_orders.append(s['order'])
    for x in rep.get('excluded') or []:
        inside = sorted(f for f in selected_files if f == x['dir'] or f.startswith(x['dir'] + '/'))
        if inside:
            errs.append(f'selected: {inside[:3]} lie in {x["dir"]}, which the selection excluded ({x.get("reason")}); '
                        'the tests of a fixture project are not tests of the repository')
    if seen_orders != list(range(1, len(seen_orders) + 1)):
        errs.append('selected: order must run 1..n in list order')
    if rep['not_selected_count'] != rep['total_tests'] - len(rep['selected']):
        errs.append('not_selected_count: must be total_tests minus the selected count')

    # every change linked, a blind spot, or docs
    for c in rep['changes']:
        p = c['path']
        if c['kind'] in ('docs', 'bench'):     # a bench target: cargo test never builds or runs it
            continue
        cited = any(r['changed_path'] == p and r['source'] != 'blind_spot' for s in rep['selected'] for r in s['reasons'])
        blind = any(b['path'] == p for b in rep['blind_spots'])
        if c['kind'] == 'test' and c['status'] == 'deleted':
            continue
        if not cited and not blind:
            errs.append(f'changes[{p}]: neither linked to a selected test nor a blind spot (and not docs)')

    # blind spots
    for b in rep['blind_spots']:
        where = f'blind_spots[{b["id"]}]'
        if b['required_scope'] not in SCOPES[b['kind']]:
            errs.append(f'{where}: {b["kind"]} needs scope {sorted(SCOPES[b["kind"]])}, not {b["required_scope"]}')
        if b['required_scope'] == 'no_existing_test':
            # only a newly added CI file may widen to nothing: nothing that exists refers to it yet
            ch = next((c for c in rep['changes'] if c['path'] == b['path']), {})
            if ch.get('status') != 'added' or not import_graph.match(b['path'], KINDS['ci']):
                errs.append(f'{where}: no_existing_test is only for an added CI file; {b["path"]} is '
                            f'{ch.get("status") or "not a change"}, so it widens')
            if b['widened_to'] or b['would_widen_to']:
                errs.append(f'{where}: no_existing_test widens to no test')
        for x in b.get('left_out') or []:
            fam = x.get('family')
            if b['required_scope'] not in ('package', 'package_and_contract') or import_graph.family(b['path']) is not None:
                errs.append(f'{where}: left_out is only for a file of no language widened to its package')
            if fam not in ('rust', 'go') or not str(x.get('reason') or '').strip():
                errs.append(f'{where}: left_out needs a family (rust or go) and the reason its tests were left out')
            elif any(import_graph.family(t) == fam for t in b['would_widen_to']):
                errs.append(f'{where}: left_out says the {fam} tests were left out, but its scope lists some')
        widen = mode == 'strict' or (mode == 'balanced' and b['kind'] in HIGH_RISK)
        if widen:
            if b['action'] != b['required_scope']:
                errs.append(f'{where}: in {mode} mode a {b["kind"]} blind spot must widen to {b["required_scope"]}, not {b["action"]}')
            if sorted(b['widened_to']) != sorted(b['would_widen_to']):
                errs.append(f'{where}: widened_to must be every test in its scope')
            missing = [t for t in b['widened_to'] if t not in selected_files]
            if missing:
                errs.append(f'{where}: widened to tests that are not selected: {missing[:3]}')
            if b['required_scope'] == 'full_suite' and tests_all and set(b['widened_to']) != tests_all:
                errs.append(f'{where}: full_suite must cover all {len(tests_all)} test files')
            if b['required_scope'] == 'package' and pkg_tests:
                pkg = max((r for r in pkg_tests if r == '.' or b['path'].startswith(r + '/')), key=len, default='.')
                want = set(import_graph.same_family(b['path'], sorted(pkg_tests.get(pkg, []) if pkg != '.' else tests_all)))
                for x in b.get('left_out') or []:
                    # tests of a compiled language left out for a file of no language that none of its sources names
                    gone = {t for t in want if import_graph.family(t) == x.get('family')}
                    if pkg != '.' or x.get('tests') != len(gone):
                        errs.append(f'{where}: left_out {x.get("family")} must count the {len(gone)} {x.get("family")} '
                                    'test file(s) of the root package')
                    want -= gone
                top = b['path'].split('/', 1)[0] if '/' in b['path'] else None
                bare = pkg == '.' and not next((p.get('manifests') for p in rep['metadata'].get('packages') or []
                                                if p['root'] == '.'), None)
                folder = {t for t in want if top and t.startswith(top + '/')} if bare else set()
                if folder and import_graph.family(b['path']) != 'go':
                    # a manifest-less root: the top-level folder with tests stands in for the package (its tests, the
                    # guide checks that run its scripts and the tests that reach the file)
                    if not folder <= set(b['widened_to']):
                        errs.append(f'{where}: folder scope must cover every test under {top}/')
                elif import_graph.family(b['path']) == 'go':
                    # a Go package is a directory: its own tests and its importers', else the module's Go tests
                    if not set(b['widened_to']) <= want or (want and not b['widened_to']):
                        errs.append(f'{where}: package scope of a Go file must be Go tests of {pkg}')
                elif want and set(b['widened_to']) != want:
                    errs.append(f'{where}: package scope must cover every test in {pkg} that a '
                                f'{import_graph.family(b["path"]) or "non-code"} file can reach')
            if b['required_scope'] == 'language' and tests_all:
                if import_graph.family(b['path']) is None:
                    errs.append(f'{where}: language scope needs a file of one language')
                elif set(b['widened_to']) != set(import_graph.same_family(b['path'], sorted(tests_all))):
                    errs.append(f'{where}: language scope must cover every {import_graph.family(b["path"])} test file')
        else:
            if b['action'] != 'labelled' or b['widened_to']:
                errs.append(f'{where}: in {mode} mode a {b["kind"]} blind spot is labelled, not widened')

    # replay
    r = rep.get('replay')
    if r:
        total, missed = r['failures_total'], r['failures_missed']
        if total:
            want_recall = round((total - missed) / total, 4)
            low, high = stats.wilson(total - missed, total)
            if r['recall'] != want_recall:
                errs.append(f'replay.recall: {r["recall"]} but {total - missed}/{total} = {want_recall}')
            if (r['wilson_low'], r['wilson_high']) != (low, high):
                errs.append(f'replay: Wilson interval must be {low}-{high} for {total - missed}/{total}')
        elif r['recall'] is not None:
            errs.append('replay.recall: no failures were replayed, so recall must be null')
        if len(r['misses']) != missed:
            errs.append(f'replay.misses: {len(r["misses"])} listed but failures_missed is {missed}')
        order = [m['why_missed'] for m in r['misses']]
        if order != sorted(order, key=lambda w: 0 if w == 'missing_source' else 1):
            errs.append('replay.misses: rank missing_source misses above weak_rule ones')
        for m in r['misses']:
            if m['why_missed'] == 'missing_source' and not re.search('|'.join(EVIDENCE_SOURCES | {'coverage map', 'journey', 'history'}),
                                                                   m['explanation'], re.I):
                errs.append(f'replay.misses[{m["change"]}]: a missing_source miss must name the source that was unavailable')
        if replay:
            for k in ('changes_replayed', 'failures_total', 'failures_missed', 'recall', 'wilson_low', 'wilson_high'):
                if r.get(k) != replay.get(k):
                    errs.append(f'replay.{k}: {r.get(k)} but {r["replay_file"]} says {replay.get(k)}')
            if replay.get('failures_baseline') and r.get('failures_baseline') != replay['failures_baseline']:
                errs.append(f'replay.failures_baseline: {r.get("failures_baseline")} but {r["replay_file"]} left '
                            f'{replay["failures_baseline"]} failure(s) of tests failing at the base uncounted; say so')
            if len(replay.get('changes_skipped') or []) != len(r.get('changes_skipped') or []) and \
                    'changes_skipped' in r:
                errs.append(f'replay.changes_skipped: must list the {len(replay["changes_skipped"])} change(s) the '
                            'replay skipped')
            if r.get('mode', mode) != replay['mode']:
                errs.append('replay.mode: differs from the replay file')
            if replay['mode'] != mode:
                errs.append(f'replay ran in {replay["mode"]} mode but the selection is {mode}: recall describes another mode')
            want = sorted((m['change'], m['test_id']) for m in replay['misses'])
            got = sorted((m['change'], m['test_id']) for m in r['misses'])
            if want != got:
                errs.append(f'replay.misses: must be exactly the replay\'s misses {want}')
    summ = rep['summary']
    if summ.get('recall_status') == 'measured' and not (r and r.get('failures_total')):
        errs.append('summary.recall_status: measured needs a replay with failures')
    if not (r and r.get('failures_total')):
        for where, text in all_text(rep):
            if re.search(r'\brecall\b', text, re.I) and PCT.search(text):
                errs.append(f'{where}: gives a recall figure without a replay; recall is not_assessed')

    # verify
    v = rep.get('verify')
    if v and runs:
        by_id = {x['id']: x for x in runs['runs']}
        for rid in v['selected_run_ids'] + v['full_run_ids']:
            if rid not in by_id:
                errs.append(f'verify: run {rid} is not in {v["runs_file"]}')
            elif by_id[rid].get('errored'):
                errs.append(f'verify: run {rid} errored ({by_id[rid].get("error") or "the tests did not run"}); '
                            'an errored run is no evidence that the selection holds')
        roots = set(pkg_tests) | {'.'}
        id_file = {tid: x['file'] for x in rep['selected'] for tid in x.get('test_ids') or []}

        def case_file(c: dict) -> str:
            """The case's file, repository-relative: the run label's last word is its package when it is one."""
            label = (by_id.get(c['run_id']) or {}).get('label') or ''
            pkg = label.split()[-1] if ' ' in label else '.'
            if pkg not in roots:
                pkg = '.'
            f = (c.get('file') or '').replace('\\', '/')
            return f if pkg == '.' or f.startswith(pkg + '/') else f'{pkg}/{f}'

        def case_id(c: dict) -> str:
            return re.sub(r'#\d+$', '', c.get('id') or '')
        inside: set[str] = set()
        outside_cases: list[tuple[str, str]] = []
        for c in runs['cases']:
            run = by_id.get(c['run_id'])
            if not run or c['run_id'] not in v['full_run_ids'] or c['status'] not in ('failed', 'error'):
                continue
            f, cid = case_file(c), case_id(c)
            if f in selected_files:
                inside.add(f)
            elif cid in id_file:
                # a libtest unit test recorded under its target's root file: its id belongs to a selected file
                inside.add(id_file[cid])
            else:
                outside_cases.append((f, cid))
        waived: set[int] = set()
        lims = rep.get('limitations', [])
        for k in v.get('known_failures') or []:
            kid = str(k.get('id') or '')
            where = f'verify.known_failures[{kid}]'
            hits = {i for i, (f, cid) in enumerate(outside_cases) if kid and kid in (f, cid)}
            if not hits:
                errs.append(f'{where}: not a failure of the full run outside the selection; list only tests that failed '
                            'there')
                continue
            rids = k.get('run_ids') or []
            bad = [r for r in rids if r not in by_id or by_id[r].get('errored')]
            mine = [c for c in runs['cases'] if c['run_id'] in rids and kid in (case_file(c), case_id(c))]
            fails = [c for c in mine if c['status'] in ('failed', 'error')]
            passes = [c for c in mine if c['status'] == 'passed']
            ok = False
            if bad or not rids:
                errs.append(f'{where}: run_ids must name runs of {v["runs_file"]} that did not error ({bad or "none given"})')
            elif k.get('kind') == 'flaky':
                hashes = {by_id[c['run_id']].get('code_hash') for c in fails + passes} - {None}
                if not fails or not passes:
                    errs.append(f'{where}: flaky needs runs in run_ids in which it failed and in which it passed; it '
                                f'failed in {len(fails)} and passed in {len(passes)} of them')
                elif len(hashes) > 1:
                    errs.append(f'{where}: flaky needs a pass and a failure on the same code; the runs have '
                                f'{len(hashes)} different code hashes')
                else:
                    ok = True
            elif k.get('kind') == 'baseline':
                base = [r for r in rids if (by_id[r].get('label') or '').startswith('verify-base')
                        and r not in v['full_run_ids']]
                if not any(c['run_id'] in base for c in fails):
                    errs.append(f'{where}: baseline needs a verify-base run (the tests at the base, before the change) '
                                'in run_ids in which it failed')
                else:
                    ok = True
            else:
                errs.append(f'{where}: kind must be flaky or baseline')
            if not str(k.get('note') or '').strip():
                errs.append(f'{where}: say in note what the runs showed')
            if not any(kid in t for t in lims):
                errs.append(f'{where}: name {kid} in limitations; a known failure is reported, not hidden')
            if ok:
                waived |= hits
        outside = {f for i, (f, _cid) in enumerate(outside_cases) if i not in waived}
        if sorted(outside) != sorted(v['failures_outside_selection']):
            errs.append(f'verify.failures_outside_selection: the full runs failed outside the selection in {sorted(outside)}')
        if sorted(inside) != sorted(v['failures_inside_selection']):
            errs.append(f'verify.failures_inside_selection: must be {sorted(inside)}')
    elif v and not runs:
        errs.append('verify: pass --runs to check the verify run')
    for f in (v or {}).get('failures_outside_selection') or []:
        if not any(f in t for t in rep.get('limitations', [])):
            errs.append(f'verify: failures outside the selection are misses; name each one in limitations ({f} is not)')

    # a changed file in a language with no import parser: the report must say so
    lc = rep.get('language_coverage') or (selection or {}).get('language_coverage') or {}
    if lc.get('warning'):
        lim = ' '.join(rep.get('limitations', [])).lower()
        for lang in lc.get('changed_unparsed') or ['(unnamed)']:
            if lang.lower() not in lim:
                errs.append(f'limitations: changed {lang} file(s) have no import parser (language_coverage.warning); '
                            f'name {lang} in a limitation, since those changes were widened, not traced to tests')

    # selected tests the commands do not run by default: the report must say so
    lim = ' '.join(rep.get('limitations', [])).lower()
    for g in rep['command'].get('not_run_by_default') or []:
        gates = set(g.get('gates') or [])
        need = []
        if gates & {'feature', 'required_features'}:
            need += [f for f in g.get('features') or [] if f.lower() not in lim] or \
                ([] if g.get('features') or 'feature' in lim else ['the feature'])
        if 'ignored' in gates and 'ignore' not in lim:
            need.append('the #[ignore]')
        if 'platform' in gates and not any(w in lim for w in [*(g.get('platforms') or []), 'platform', 'unix', 'windows']):
            need.append('the platforms (' + ', '.join(g.get('platforms') or []) + ')')
        if gates - {'feature', 'required_features', 'ignored', 'platform'} and 'cfg' not in lim:
            need.append('the cfg')
        if need:
            errs.append(f'command.not_run_by_default[{g.get("crate")}: {", ".join(sorted(gates))}]: {len(g.get("tests") or [])} '
                        f'selected test(s) are not run by the commands, or not on every platform ({g.get("detail")}); name '
                        f'{" and ".join(need)} in a limitation')

    # text rules
    for where, text in all_text(rep):
        if BANNED.search(text):
            errs.append(f'{where}: "{BANNED.search(text).group(0)}" — unselected tests are "not selected by this evidence", never safe')
    if not WHEN.search(rep['recommendations']['full_suite']):
        errs.append('recommendations.full_suite: say when the full suite runs (on merge, nightly, before release)')
    head = summ.get('headline', '')
    if not re.search(rf'\b{len(rep["selected"])} of {rep["total_tests"]}\b', head):
        errs.append(f'summary.headline: must state "{len(rep["selected"])} of {rep["total_tests"]}" test files')
    allowed = set()
    if summ.get('evidence_completeness') is not None:
        allowed.add(round(100 * summ['evidence_completeness'], 1))
    if rep['total_tests']:
        allowed.add(round(100 * len(rep['selected']) / rep['total_tests'], 1))
    if r and r.get('failures_total'):
        allowed |= {round(100 * r['recall'], 1), round(100 * r['wilson_low'], 1), round(100 * r['wilson_high'], 1)}
    share = (r or {}).get('mean_selected_share')
    if share is not None:
        allowed.add(round(100 * share, 1))
        # a recall measured while the replayed selections took most of the suite says little about the selector
        if r.get('failures_total') and share >= 0.5 and re.search(r'\brecall\b|\bfailures selected\b', head, re.I) and \
                not any(abs(float(m.group(1)) - 100 * share) <= 0.5 for m in PCT.finditer(head)):
            errs.append(f'summary.headline: gives the replay recall, but the replayed selections took {100 * share:.1f}% '
                        'of the tests on average; say so beside the recall')
    for m in PCT.finditer(re.sub(r'\b95\s?% (CI|confidence)', '', head)):
        if not any(abs(float(m.group(1)) - a) <= 0.05 or abs(float(m.group(1)) - round(a)) < 0.5 and '.' not in m.group(1)
                   for a in allowed):
            errs.append(f'summary.headline: {m.group(0)} is not one of the computed figures')
    for f in rep.get('files_written', []):
        if import_graph.match(f, KINDS['ci']):
            errs.append(f'files_written: {f} is CI configuration; write the snippet elsewhere for the user to add')
    if rep.get('ci_snippet') and import_graph.match(rep['ci_snippet']['path'], KINDS['ci']):
        errs.append('ci_snippet.path: never write into CI configuration')

    # computed summary
    want = computed_summary(rep)
    for k, val in want.items():
        if summ.get(k) != val:
            errs.append(f'summary.{k}: {summ.get(k)!r} but computed {val!r} (run --write-summary)')
    return errs


def recompute(selection: dict) -> list[str]:
    import select_tests  # noqa: E402
    again = select_tests.select(**select_tests.args_from_inputs(selection['inputs']))
    errs = []
    for k in COPIED + ('packages', 'repo_code_hash', 'change', 'excluded'):
        if again.get(k) != selection.get(k):
            errs.append(f'--recompute: {k} differs when the selection is run again from its inputs')
    return errs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('report', type=Path)
    ap.add_argument('--selection', type=Path)
    ap.add_argument('--recompute', action='store_true')
    ap.add_argument('--replay', type=Path)
    ap.add_argument('--runs', type=Path)
    ap.add_argument('--write-summary', action='store_true')
    args = ap.parse_args(argv)
    rep = json.loads(args.report.read_text(encoding='utf-8'))
    schema = json.loads(SCHEMA.read_text(encoding='utf-8'))
    if args.write_summary:
        try:
            filled = computed_summary(rep)
        except (KeyError, TypeError) as exc:
            print(f'INVALID: cannot compute the summary ({exc!r}); fix the report structure first')
            return 1
        rep['summary'] = {'headline': (rep.get('summary') or {}).get('headline', ''), **filled}
        args.report.write_text(json.dumps(rep, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    errs = schema_errors(rep, schema, schema)
    if errs:
        print('INVALID (schema)')
        for e in errs[:40]:
            print('  ' + e)
        return 1
    load = lambda p: json.loads(p.read_text(encoding='utf-8')) if p else None  # noqa: E731
    selection, replay, runs = load(args.selection), load(args.replay), load(args.runs)
    errs = check(rep, selection, replay, runs)
    if args.recompute:
        if not selection:
            errs.append('--recompute needs --selection')
        else:
            errs += recompute(selection)
    if errs:
        print('INVALID')
        for e in errs:
            print('  ' + e)
        return 1
    s = rep['summary']
    print(f'VALID: {s["selected"]} of {s["total_tests"]} test files, {s["blind_spots"]} blind spot(s), '
          f'recall {s["recall_status"]}' + (f' ({s["recall_text"]})' if s.get('recall_text') else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
