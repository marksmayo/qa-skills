#!/usr/bin/env python3
"""Pick the tests to run for a change, with a reason for each, and list every blind spot (stdlib only).

    python select_tests.py --repo <repo> --diff change.diff [--diff-applied] --out selection.json
    python select_tests.py --repo <repo> [--base main] [--committed-only] --out selection.json
        [--mode strict|balanced|aggressive] [--coverage coverage_map.json ...] [--journeys journey_map.json]
        [--history history.json] [--related related.json] [--max-chars 8000]

A --diff is applied to a scratch copy of --repo (the repo is the base), or with --diff-applied
the repo is taken as the changed state and the diff is reverse-applied to make the base. In
git mode the working tree is the changed state and the merge base is extracted as the base.
The repo itself is never modified.

Evidence, per changed file (strength in brackets): coverage of a changed line or function [3],
the import graph [2], framework related-tests output [2], journey links [2], naming [1] and
co-failure history (failed in at least two past changes to the file) [1]. A changed test file is
always selected [3]. A test is selected at strength 2 or more, or at 1 when two sources agree.
Changes no evidence can bound are blind spots, widened according to --mode (see RUBRIC.md).
"""
from __future__ import annotations
import argparse, difflib, hashlib, json, os, re, shutil, subprocess, sys, tarfile, tempfile, io
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_command, diff_scope, import_graph, parse_results, run_tests  # noqa: E402
from unified_diff import apply_patch, parse_patch  # noqa: E402,F401

KINDS_PATH = HERE.parent / 'data' / 'file_kinds.json'
KINDS = import_graph.KINDS
STRENGTH = {'changed_test': 3, 'coverage': 3, 'import_graph': 2, 'framework_tools': 2, 'journeys': 2, 'naming': 1,
            'co_failure': 1}
SOURCES = ['coverage', 'import_graph', 'framework_tools', 'journeys', 'naming', 'co_failure']
HIGH_RISK = {'global_config', 'shared_fixture', 'dynamic_reference', 'deleted_or_renamed', 'huge_change'}
HUGE_SHARE = 0.30
FRAMEWORK_CONFIGS = ['jest.config.*', 'nx.json', 'turbo.json', 'BUILD', 'BUILD.bazel', 'WORKSPACE', 'MODULE.bazel',
                     'vitest.config.*', 'vitest.workspace.*']
PRIORITY_RANK = {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3}
NAMING_SKIP = {'index', '__init__', 'main', 'mod', 'lib', 'utils', 'util', 'types', 'setup', 'conftest'}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.replace('\r\n', '\n').encode('utf-8')).hexdigest()


def rel_input(p: Path | None) -> str | None:
    if p is None:
        return None
    try:
        return Path(os.path.relpath(p.resolve(), Path.cwd())).as_posix()
    except ValueError:
        return p.as_posix()


def copy_tree(src: Path, dest: Path) -> None:
    def ignore(d, names):
        return [n for n in names if n in import_graph.SKIP_DIRS and n != '__snapshots__' or n.endswith('.pyc')]
    shutil.copytree(src, dest, ignore=ignore)


def git(repo: Path, *args: str, binary: bool = False):
    res = subprocess.run(['git', '-C', str(repo), *args], capture_output=True)
    if res.returncode:
        raise SystemExit(f'git {" ".join(args)} failed: {res.stderr.decode("utf-8", "replace").strip()}')
    return res.stdout if binary else res.stdout.decode('utf-8', 'replace')


def extract(tar_bytes: bytes, dest: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        try:
            tf.extractall(dest, filter='data')
        except TypeError:                    # Python without extraction filters (before 3.12 and the backports)
            tf.extractall(dest)


# ---------------------------------------------------------------- inputs
def load_history(path: Path | None) -> list[dict]:
    """[{id, changed: [paths], failed: [test files], appearances: {test file: runs}}] from history.json."""
    if not path:
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    base = path.parent
    out = []
    for ch in data.get('changes', []):
        diff_text = (base / ch['diff']).read_text(encoding='utf-8', errors='replace')
        changed = sorted({p for f in diff_scope.parse_diff(diff_text) for p in (f['path'], f.get('old_path')) if p})
        failed, seen, cases_out = set(), set(), []
        for run in ch.get('runs', []):
            cases, _ = parse_results.parse_junit(base / run['junit'], ch['id'])
            for c in cases:
                file = test_file_of(c, run.get('package', '.'))
                seen.add(file)
                cases_out.append({'id': c['id'], 'file': file, 'status': c['status'], 'package': run.get('package', '.')})
                if c['status'] in ('failed', 'error'):
                    failed.add(file)
        out.append({'id': ch['id'], 'diff': ch['diff'], 'changed': changed, 'failed': sorted(failed), 'seen': sorted(seen),
                    'cases': cases_out, 'full_run': ch.get('full_run', False), 'description': ch.get('description', '')})
    return out


def test_file_of(case: dict, package: str) -> str:
    f = case.get('file') or case.get('classname') or case['id'].split('::')[0]
    f = f.replace('\\', '/').lstrip('./')
    return f if package in ('.', '') or f.startswith(package + '/') else f'{package}/{f}'


def load_coverage(paths: list[Path]) -> list[dict]:
    maps = []
    for p in paths:
        m = json.loads(p.read_text(encoding='utf-8'))
        m['_path'] = rel_input(p)
        maps.append(m)
    return maps


def journey_links(path: Path | None) -> list[dict]:
    if not path:
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    out = []
    for j in data.get('journeys', []):
        files = set()
        for s in j.get('steps', []) + j.get('variants', []):
            for e in s.get('evidence', []):
                if e.get('file'):
                    files.add(e['file'].replace('\\', '/'))
        out.append({'id': j['id'], 'priority': j.get('priority'), 'tests': [t.replace('\\', '/') for t in j.get('tests', [])],
                    'files': sorted(files)})
    return out


# ---------------------------------------------------------------- classification
def classify(entry: dict, g_new, g_base) -> str:
    path = entry['path']
    graph = g_base if entry['status'] == 'deleted' else g_new
    node = graph.files.get(path, {})
    pkg = graph.package_of(path)
    if import_graph.match(path, KINDS['snapshot']):
        return 'snapshot'
    if entry['kind'] == 'docs' or import_graph.match(path, KINDS['docs']):
        return 'docs'
    if node.get('test_level'):
        return 'test'
    if import_graph.match(path, KINDS['fixture']) or entry['kind'] == 'test':
        return 'shared_fixture'
    if import_graph.match(path, KINDS['lockfiles']) or import_graph.match(path, KINDS['ci']):
        return 'global_config'
    if import_graph.match(path, KINDS['build_config']):
        return 'package_config' if pkg != '.' else 'global_config'
    if entry['kind'] == 'migration' or import_graph.match(path, KINDS['schema']):
        return 'data_or_schema'
    if import_graph.match(path, KINDS['non_code']):
        return 'non_code'
    if entry['kind'] in ('source', 'generated', 'vendored', 'types') or node.get('lang'):
        return 'source'
    if PurePosixPath(path).suffix.lower() in KINDS['data_ext']:
        return 'data_or_schema' if pkg != '.' else 'global_config'
    if entry['kind'] == 'config':
        return 'package_config' if pkg != '.' else 'global_config'
    if entry['kind'] == 'binary':
        return 'non_code'
    return 'unknown'


# ---------------------------------------------------------------- evidence
def old_side_lines(base_text: str | None, new_text: str | None) -> tuple[set[int], set[int]]:
    """(base lines replaced or deleted, base lines next to an insertion), 1-based."""
    if base_text is None:
        return set(), set()
    a = base_text.replace('\r\n', '\n').splitlines()
    b = (new_text or '').replace('\r\n', '\n').splitlines()
    changed, anchors = set(), set()
    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ('replace', 'delete'):
            changed.update(range(i1 + 1, i2 + 1))
        elif tag == 'insert':
            anchors.update(x for x in (i1, i1 + 1) if 1 <= x <= len(a))
    return changed, anchors


def functions_of(path: str, text: str) -> list[dict]:
    funcs = diff_scope.py_functions(text) if path.endswith('.py') else None
    return funcs if funcs is not None else diff_scope.heuristic_functions(text)


def naming_matches(path: str, tests: list[str], package_of) -> list[str]:
    p = PurePosixPath(path)
    stem = p.name.split('.')[0]
    if stem.lower() in NAMING_SKIP:
        return []
    names = {f'test_{stem}.py', f'{stem}_test.py', f'{stem}_test.go', f'{stem}_test.dart', f'{stem}_spec.rb',
             f'{stem}Test.java', f'{stem}Tests.java', f'{stem}IT.java', f'{stem}Test.kt', f'{stem}Tests.kt',
             f'{stem}Tests.swift', f'{stem}Test.swift', f'{stem}Tests.cs', f'{stem}Test.cs'}
    js = re.compile(r'^' + re.escape(stem) + r'\.(test|spec|e2e|visual|cy)\.[cm]?[jt]sx?$')
    pkg = package_of(path)
    return sorted(t for t in tests if package_of(t) == pkg and (PurePosixPath(t).name in names or js.match(PurePosixPath(t).name)))


class Selector:
    def __init__(self, repo: Path, base_dir: Path, new_dir: Path, scope: dict, mode: str, coverage: list[dict],
                 journeys: list[dict], history: list[dict], related: dict | None, history_given: bool):
        self.repo, self.base_dir, self.new_dir, self.scope, self.mode = repo, base_dir, new_dir, scope, mode
        self.coverage, self.journeys, self.history, self.related = coverage, journeys, history, related
        self.history_given = history_given
        self.g_new = import_graph.build(new_dir)
        self.g_base = import_graph.build(base_dir)
        self.tests = self.g_new.tests()
        self.reasons: dict[str, list[dict]] = {}
        self.blind: list[dict] = []
        self.notes: list[str] = []

    # ------------------------------------------------------------ sources
    def coverage_status(self) -> list[dict]:
        out = []
        for m in self.coverage:
            stale = []
            for f, h in m['collected']['file_hashes'].items():
                p = self.base_dir / f
                if not p.is_file() or hashlib.sha256(p.read_bytes().replace(b'\r\n', b'\n')).hexdigest() != h:
                    stale.append(f)
            m['_stale'] = stale
            out.append(m)
        return out

    def add(self, test: str, source: str, evidence: str, changed: str, strength: int | None = None) -> None:
        if test not in self.tests:
            return
        rs = self.reasons.setdefault(test, [])
        if any(r['source'] == source and r['changed_path'] == changed for r in rs):
            return
        rs.append({'source': source, 'strength': STRENGTH.get(source, 0) if strength is None else strength,
                   'evidence': evidence, 'changed_path': changed})

    def evidence(self, entry: dict) -> None:
        path, old = entry['path'], entry.get('old_path')
        exists_new = entry['status'] != 'deleted'
        base_path = old or path
        base_text = self.read(self.base_dir, base_path) if entry['status'] != 'added' else None
        new_text = self.read(self.new_dir, path) if exists_new else None
        # coverage
        changed, anchors = old_side_lines(base_text, new_text)
        for m in self.coverage:
            per = m['files'].get(base_path)
            if per is None:
                continue
            stale = base_path in m['_stale']
            tag = ' (coverage map is stale for this file)' if stale else ''
            hits: dict[str, list[int]] = {}
            for ln in sorted(changed | anchors):
                for idx in per.get(str(ln), []):
                    hits.setdefault(m['tests'][idx], []).append(ln)
            func_hits: dict[str, str] = {}
            if base_text is not None:
                for fn in functions_of(base_path, base_text):
                    if any(fn['start'] <= ln <= fn['end'] for ln in changed | anchors):
                        for ln in range(fn['start'], fn['end'] + 1):
                            for idx in per.get(str(ln), []):
                                func_hits.setdefault(m['tests'][idx], fn['qualname'])
            files: dict[str, list[str]] = {}
            for tid, lines in hits.items():
                files.setdefault(tid.split('::')[0], []).append(f'{tid.split("::", 1)[-1]} ran base line(s) '
                                                                f'{", ".join(map(str, sorted(set(lines))[:6]))}')
            for tid, fn in func_hits.items():
                f = tid.split('::')[0]
                if f not in files:
                    files.setdefault(f, []).append(f'{tid.split("::", 1)[-1]} ran {fn}(), which the change edits')
            for f, why in files.items():
                self.add(f, 'coverage', f'{why[0]}' + (f' (and {len(why) - 1} more test{"s" if len(why) > 2 else ""} in this file)' if len(why) > 1 else '') + tag, path)
        # import graph
        targets = ([(self.g_new, path)] if exists_new else []) + ([(self.g_base, old)] if old else []) + \
                  ([(self.g_base, path)] if not exists_new else [])
        for graph, target in targets:
            for t, chain in graph.tests_reaching(target).items():
                hops = ' -> '.join(chain)
                self.add(t, 'import_graph', f'{hops}' if len(chain) <= 6 else f'{chain[0]} -> ... -> {chain[-1]} ({len(chain) - 1} hops)', path)
        # framework tools
        if self.related:
            for t in self.related.get('related', {}).get(path, []):
                self.add(t, 'framework_tools', f'{self.related.get("tool", "framework")} lists it as related', path)
        # journeys
        for j in self.journeys:
            if path in j['files'] or (old and old in j['files']):
                for t in j['tests']:
                    self.add(t, 'journeys', f'{j["id"]} ({j["priority"]}) cites {path} and links this test', path)
        # naming
        for t in naming_matches(path, self.tests, self.g_new.package_of):
            self.add(t, 'naming', f'{PurePosixPath(t).name} is named after {PurePosixPath(path).name}', path)
        # co-failure
        counts: dict[str, list[str]] = {}
        for h in self.history:
            if path in h['changed'] or (old and old in h['changed']):
                for t in h['failed']:
                    counts.setdefault(t, []).append(h['id'])
        touching = [h['id'] for h in self.history if path in h['changed'] or (old and old in h['changed'])]
        for t, ids in counts.items():
            if len(ids) >= 2:
                self.add(t, 'co_failure', f'failed in {len(ids)} of {len(touching)} past changes to this file ({", ".join(ids)})', path)

    @staticmethod
    def read(root: Path, rel: str | None) -> str | None:
        if not rel:
            return None
        p = root / rel
        try:
            return p.read_text(encoding='utf-8', errors='replace') if p.is_file() else None
        except OSError:
            return None

    # ------------------------------------------------------------ blind spots
    def blind_spot(self, entry: dict, kind: str, note: str, scope: str, tests: list[str]) -> None:
        bid = f'B{len(self.blind) + 1}'
        widen = self.mode == 'strict' or (self.mode == 'balanced' and kind in HIGH_RISK)
        action = scope if widen else 'labelled'
        tests = sorted(set(t for t in tests if t in self.tests))
        if widen:
            for t in tests:
                self.add(t, 'blind_spot', f'{bid} {kind}: {scope}', entry['path'], strength=0)
        self.blind.append({'id': bid, 'path': entry['path'], 'kind': kind, 'action': action, 'required_scope': scope,
                           'widened_to': tests if widen else [], 'would_widen_to': tests, 'note': note})

    def package_tests(self, path: str) -> list[str]:
        pkg = self.g_new.package_of(path)
        return self.tests if pkg == '.' else [t for t in self.tests if self.g_new.package_of(t) == pkg]

    def dynamic_sites(self, entry: dict) -> list[str]:
        """Dynamic loading sites that could reach this changed file, as 'file:line text'."""
        out = []
        for graph, path in ((self.g_new, entry['path']), (self.g_base, entry.get('old_path') or entry['path'])):
            node = graph.files.get(path)
            if not node:
                continue
            for d in node['dynamic']:
                out.append(f'{path}:{d["line"]} {d["text"]} (in the changed file)')
            pkg = graph.package_of(path)
            static_importers = graph.reverse.get(path, [])
            for src, e in graph.files.items():
                if e['package'] != pkg or src == path:
                    continue
                for d in e['dynamic']:
                    if (d['targets'] is not None and path in d['targets']) or (d['targets'] is None and not static_importers):
                        out.append(f'{src}:{d["line"]} {d["text"]}')
        return sorted(set(out))

    # ------------------------------------------------------------ run
    def run(self) -> dict:
        files = self.scope['files']
        cov = self.coverage_status()
        changes = []
        source_files = [p for p, e in self.g_base.files.items() if e['lang'] and not e['test_level']]
        for entry in files:
            kind = classify(entry, self.g_new, self.g_base)
            changes.append({'path': entry['path'], 'old_path': entry.get('old_path'), 'status': entry['status'], 'kind': kind,
                            'kind_reason': entry.get('kind_reason'), 'line_ranges': entry.get('ranges', []),
                            'renamed_from': entry.get('old_path') if entry['status'] == 'renamed' else None})
        changed_sources = [c for c in changes if c['kind'] == 'source']
        for c, entry in zip(changes, files):
            kind = c['kind']
            path = c['path']
            if kind == 'docs':
                continue
            if kind == 'test':
                if entry['status'] != 'deleted':
                    self.add(path, 'changed_test', 'the change edits this test file', path)
                else:
                    self.notes.append(f'{path} was deleted; there is nothing to run for it.')
                continue
            if kind == 'snapshot':
                owner = re.sub(r'/__snapshots__/(.+)\.snap$', r'/\1', path)
                if owner in self.tests:
                    self.add(owner, 'changed_test', f'the change edits its snapshot {PurePosixPath(path).name}', path)
                else:
                    self.blind_spot(entry, 'shared_fixture', 'Snapshot whose test file could not be found.', 'package',
                                    self.package_tests(path))
                continue
            if kind in ('source', 'unknown'):
                self.evidence(entry)
            if kind == 'shared_fixture':
                if PurePosixPath(path).name == 'conftest.py':
                    reach = self.g_new.conftest_reach(path) if entry['status'] != 'deleted' else self.g_base.conftest_reach(path)
                    self.blind_spot(entry, 'shared_fixture', f'conftest.py applies to every test under '
                                    f'{PurePosixPath(path).parent.as_posix()}/, whether or not it names a fixture.',
                                    'fixture_reach', reach)
                else:
                    reach = sorted(set(self.g_new.tests_reaching(path)) | set(self.g_base.tests_reaching(path)))
                    if reach:
                        self.blind_spot(entry, 'shared_fixture', f'Test helper imported by {len(reach)} test file(s).',
                                        'fixture_reach', reach)
                    else:
                        self.blind_spot(entry, 'shared_fixture', 'Test helper or setup file no test imports: it is probably '
                                        'loaded by runner configuration (setupFiles, globalSetup), so its reach is the package.',
                                        'package', self.package_tests(path))
            elif kind == 'global_config':
                self.blind_spot(entry, 'global_config', 'Lockfile, CI or repository-level build configuration: it can change '
                                'how every test runs.', 'full_suite', self.tests)
            elif kind == 'package_config':
                pkg = self.g_new.package_of(path)
                c['kind'] = 'global_config'
                self.blind_spot(entry, 'global_config', f'Build or test configuration of package {pkg}: it can change how '
                                'every test in the package runs.', 'package', self.package_tests(path))
            elif kind == 'data_or_schema':
                contract = [t for t in self.tests if import_graph.match(t, KINDS['contract_tests'])]
                self.blind_spot(entry, 'data_or_schema', 'Data, schema or migration file: code reads it at run time, which '
                                'imports do not show.' + (f' Plus {len(contract)} contract test file(s).' if contract else
                                                          ' No contract tests found.'),
                                'package_and_contract', self.package_tests(path) + contract)
            elif kind == 'non_code':
                reach = sorted(set(self.g_new.tests_reaching(path)) | set(self.g_base.tests_reaching(path)))
                ui = [t for t in reach if self.g_new.files[t]['test_level'] in ('e2e', 'visual')]
                note = 'Template, style, translation or image: only tests that render it can notice a change.'
                if not ui:
                    ui = [t for t in self.package_tests(path) if self.g_new.files[t]['test_level'] in ('e2e', 'visual')]
                    note += ' No e2e or visual test reaches it by import, so every e2e and visual test in the package.'
                if not ui:
                    ui = self.package_tests(path)
                    note += ' The package has no e2e or visual tests, so its whole suite.'
                self.blind_spot(entry, 'non_code', note, 'e2e_and_visual', ui)
            if kind in ('source', 'unknown'):
                sites = self.dynamic_sites(entry)
                if sites:
                    self.blind_spot(entry, 'dynamic_reference', 'Loaded by a computed name, so imports cannot show every '
                                    'test that reaches it: ' + '; '.join(sites[:3]), 'package', self.package_tests(path))
                if entry['status'] in ('deleted', 'renamed'):
                    gone = entry.get('old_path') or path
                    importers = sorted(self.g_base.tests_reaching(gone))
                    self.blind_spot(entry, 'deleted_or_renamed', f'{gone} was {entry["status"]}; tests that imported it in '
                                    f'the base tree: {len(importers)}.', 'importers', importers)
                linked = any(r['changed_path'] == path and r['source'] != 'blind_spot' for rs in self.reasons.values() for r in rs)
                strong = self.selected_for(path)
                if not strong and entry['status'] != 'deleted':
                    lang = self.g_new.files.get(path, {}).get('lang')
                    unparsed = lang and self.g_new.languages.get(lang) == 'no_parser'
                    self.blind_spot(entry, 'unmapped_source',
                                    ('No evidence source links this file to a test' if not linked else
                                     'Only weak evidence (one strength-1 source) links this file to a test')
                                    + (f' (no import parser for {lang})' if unparsed else '') +
                                    '. No tests found for it: hand it to ticket-to-tdd or journey-to-tests.',
                                    'package', self.package_tests(path))
        n_changed_src = len([c for c in changed_sources if c['status'] != 'added'])
        if source_files and n_changed_src / len(source_files) > HUGE_SHARE:
            self.blind_spot({'path': '(change)'}, 'huge_change', f'{n_changed_src} of {len(source_files)} source files '
                            f'changed (over {int(HUGE_SHARE * 100)}%): selection would save little, so run the full suite.',
                            'full_suite', self.tests)
        return self.result(changes, cov)

    def selected_for(self, path: str) -> bool:
        for t, rs in self.reasons.items():
            mine = [r for r in rs if r['changed_path'] == path and r['source'] != 'blind_spot']
            if mine and (max(r['strength'] for r in mine) >= 2 or len({r['source'] for r in mine}) >= 2):
                return True
        return False

    def result(self, changes: list[dict], cov: list[dict]) -> dict:
        selected = []
        considered = []
        fail_rate = {}
        for h in self.history:
            for t in h['seen']:
                fail_rate.setdefault(t, [0, 0])
                fail_rate[t][1] += 1
                if t in h['failed']:
                    fail_rate[t][0] += 1
        for t, rs in sorted(self.reasons.items()):
            ev = [r for r in rs if r['source'] != 'blind_spot']
            strength = max([r['strength'] for r in ev], default=0)
            sources = {r['source'] for r in ev}
            chosen = strength >= 2 or (strength == 1 and len(sources) >= 2) or any(r['source'] == 'blind_spot' for r in rs)
            prio = min([PRIORITY_RANK.get(j['priority'], 9) for j in self.journeys if t in j['tests']], default=9)
            fr = fail_rate.get(t, [0, 0])
            item = {'test_id': t, 'file': t, 'package': self.g_new.package_of(t), 'test_level': self.g_new.files[t]['test_level'],
                    'strength': strength, 'reasons': rs,
                    'journey_priority': next((p for p, r in PRIORITY_RANK.items() if r == prio), None),
                    'past_failures': f'{fr[0]}/{fr[1]}' if fr[1] else None,
                    'widened_by': sorted({r['evidence'].split()[0] for r in rs if r['source'] == 'blind_spot'})}
            (selected if chosen else considered).append(item)
        selected.sort(key=lambda s: (-s['strength'], PRIORITY_RANK.get(s['journey_priority'], 9),
                                     -(int(s['past_failures'].split('/')[0]) / int(s['past_failures'].split('/')[1])
                                       if s['past_failures'] else 0), s['file']))
        for i, s in enumerate(selected, 1):
            s['order'] = i
        sel_files = {s['file'] for s in selected}
        packages = []
        for p in sorted(self.g_new.packages, key=lambda p: p['root']):
            mine = [t for t in self.tests if self.g_new.package_of(t) == p['root']]
            if not mine and p['root'] == '.':
                continue
            packages.append({**p, 'test_files': mine, 'selected': len([t for t in mine if t in sel_files]),
                             'full_suite': bool(mine) and all(t in sel_files for t in mine)})
        full = {p['root'] for p in packages if p['full_suite']}
        command = build_command.build(selected, packages, full, {p['root']: p['test_files'] for p in packages},
                                      {t: self.g_new.files[t]['test_level'] for t in self.tests}, self.max_chars)
        sources = self.sources(changes, cov)
        n_docs = len([c for c in changes if c['kind'] == 'docs'])
        return {
            'sources': sources, 'changes': changes, 'packages': packages, 'selected': selected,
            'considered_not_selected': considered, 'blind_spots': self.blind,
            'total_tests': len(self.tests), 'not_selected_count': len(self.tests) - len(selected), 'command': command,
            'docs_only': bool(changes) and n_docs == len(changes),
            'notes': self.notes,
        }

    def sources(self, changes: list[dict], cov: list[dict]) -> list[dict]:
        src_changes = [c for c in changes if c['kind'] in ('source', 'unknown')]
        out = []
        if not self.coverage:
            out.append({'name': 'coverage', 'status': 'not_run', 'reason': 'No per-test coverage map was given.',
                        'version': None, 'factor': 0.0 if src_changes else None})
        else:
            stale = sorted({f for m in cov for f in m['_stale']})
            covered_pkgs = sorted({m['package'] for m in cov})
            in_map = [c for c in src_changes if any(self.g_base.package_of(c.get('old_path') or c['path']) == m['package'] or
                                                     m['package'] == '.' for m in cov)]
            share = len(in_map) / len(src_changes) if src_changes else None
            status = 'stale' if stale else 'ran'
            reason = (f'Per-test coverage for package(s) {", ".join(covered_pkgs)}.' +
                      (f' Stale: {len(stale)} measured file(s) differ from the base tree ({", ".join(stale[:4])}); '
                       'their coverage is used with a warning.' if stale else ' Every measured file matches the base tree.'))
            uncovered = [c['path'] for c in src_changes if c not in in_map]
            if uncovered:
                reason += f' No coverage for changed files outside those packages: {", ".join(uncovered[:4])}.'
            out.append({'name': 'coverage', 'status': status, 'reason': reason,
                        'version': ', '.join(sorted({f'{m["format"]} {m.get("tool_version") or ""}'.strip() for m in cov})),
                        'collected_at_commit': next((m['collected'].get('commit') for m in cov if m['collected'].get('commit')), None),
                        'inputs': [m['_path'] for m in cov], 'stale_files': stale,
                        'factor': None if share is None else round(share * (0.5 if stale else 1.0), 4)})
        langs = self.g_new.languages
        graph_changes = [c for c in changes if c['kind'] not in ('docs', 'global_config')]
        parsed = [c for c in graph_changes if (self.g_new.files.get(c['path']) or self.g_base.files.get(c['path']) or {}).get('lang')
                  in (None, *[l for l, s in langs.items() if s == 'parsed'])]
        unparsed_langs = sorted(l for l, s in langs.items() if s == 'no_parser')
        out.append({'name': 'import_graph', 'status': 'ran', 'version': 'import_graph.py',
                    'reason': f'Parsed {", ".join(sorted(l for l, s in langs.items() if s == "parsed")) or "nothing"}.' +
                              (f' No parser for {", ".join(unparsed_langs)}.' if unparsed_langs else ''),
                    'factor': round(len(parsed) / len(graph_changes), 4) if graph_changes else None})
        fw_cfg = [p for p in self.g_new.files if import_graph.match(p, FRAMEWORK_CONFIGS)]
        if self.related:
            out.append({'name': 'framework_tools', 'status': 'ran', 'version': self.related.get('version'),
                        'reason': f'{self.related.get("tool")}: {self.related.get("command")}', 'factor': 1.0})
        elif fw_cfg:
            out.append({'name': 'framework_tools', 'status': 'not_run', 'version': None, 'factor': 0.0,
                        'reason': f'{", ".join(fw_cfg[:3])} present, but no related-tests output was given (--related).'})
        else:
            out.append({'name': 'framework_tools', 'status': 'not_applicable', 'version': None, 'factor': None,
                        'reason': 'No Jest, Vitest, Nx, Turborepo or Bazel configuration found.'})
        out.append({'name': 'journeys', 'status': 'ran' if self.journeys else 'not_run', 'version': None,
                    'reason': f'{len(self.journeys)} journeys with test links.' if self.journeys else 'No journey map was given.',
                    'factor': 1.0 if self.journeys else 0.0})
        out.append({'name': 'naming', 'status': 'ran', 'version': None, 'reason': 'Test file names matched to changed file names.',
                    'factor': 1.0})
        out.append({'name': 'co_failure', 'status': 'ran' if self.history_given else 'not_run', 'version': None,
                    'reason': (f'{len(self.history)} earlier change(s) with full-run results.' if self.history_given else
                               'No history was given.'), 'factor': 1.0 if self.history_given else 0.0})
        return out


def evidence_completeness(sources: list[dict]) -> float | None:
    vals = [s['factor'] for s in sources if s.get('factor') is not None and s['status'] != 'not_applicable']
    return round(sum(vals) / len(vals), 4) if vals else None


# ---------------------------------------------------------------- entry points
def prepare_trees(repo: Path, work: Path, diff: Path | None, diff_applied: bool, base: str, committed_only: bool):
    """(base_dir, new_dir, scope, change{...})"""
    if diff:
        text = diff.read_text(encoding='utf-8', errors='replace')
        if diff_applied:
            new_dir = repo
            base_dir = work / 'base'
            copy_tree(repo, base_dir)
            apply_patch(text, base_dir, reverse=True)
        else:
            base_dir = repo
            new_dir = work / 'new'
            copy_tree(repo, new_dir)
            apply_patch(text, new_dir)
        scope_files = diff_scope.scope_from_diff(text, new_dir)
        change = {'source': 'diff', 'diff': rel_input(diff), 'diff_applied': diff_applied, 'base': None, 'head': None,
                  'diff_sha256': sha256_text(text)}
    else:
        merge_base = git(repo, 'merge-base', base, 'HEAD').strip()
        head = git(repo, 'rev-parse', 'HEAD').strip()
        rng = [f'{merge_base}...HEAD'] if committed_only else [merge_base]
        text = git(repo, 'diff', '-M', '--no-color', '--no-ext-diff', *rng)
        base_dir = work / 'base'
        base_dir.mkdir(parents=True)
        extract(git(repo, 'archive', '--format=tar', merge_base, binary=True), base_dir)
        if committed_only:
            new_dir = work / 'new'
            new_dir.mkdir(parents=True)
            extract(git(repo, 'archive', '--format=tar', 'HEAD', binary=True), new_dir)
        else:
            new_dir = repo
        scope_files = diff_scope.scope_from_diff(text, new_dir)
        if not committed_only:
            for rel in git(repo, 'ls-files', '--others', '--exclude-standard').splitlines():
                body = Selector.read(repo, rel)
                kind, why = diff_scope.classify(rel, '\n'.join((body or '').splitlines()[:5]), body is None)
                n = len((body or '').splitlines())
                entry = {'path': rel, 'old_path': None, 'status': 'added', 'kind': kind, 'kind_reason': why + ' (untracked)',
                         'ranges': [[1, n]] if n else [], 'deleted_at': [], 'lines_added': n, 'lines_deleted': 0, 'present': True}
                scope_files.append(entry)
        change = {'source': 'git', 'diff': None, 'diff_applied': None, 'base': base, 'merge_base': merge_base, 'head': head,
                  'working_tree': not committed_only, 'diff_sha256': sha256_text(text)}
    return base_dir, new_dir, {'files': scope_files}, change


def select(repo: Path, diff: Path | None = None, diff_applied: bool = False, base: str = 'main', committed_only: bool = False,
           mode: str = 'strict', coverage: list[Path] | None = None, journeys: Path | None = None,
           history: Path | None = None, related: Path | None = None, max_chars: int = 8000,
           history_entries: list[dict] | None = None) -> dict:
    """Run a selection. history_entries overrides the entries read from --history (replay passes earlier ones only)."""
    repo = repo.resolve()
    cov = load_coverage(coverage or [])
    hist = history_entries if history_entries is not None else load_history(history)
    history_given = history is not None
    if mode == 'balanced':
        missing = [x for x, ok in (('a coverage map', bool(cov)), ('history', history_given)) if not ok]
        if missing:
            raise SystemExit(f'balanced mode needs {" and ".join(missing)}; use strict (the default) or aggressive.')
    with tempfile.TemporaryDirectory(prefix='impact-') as tmp:
        base_dir, new_dir, scope, change = prepare_trees(repo, Path(tmp), diff, diff_applied, base, committed_only)
        sel = Selector(repo, base_dir, new_dir, scope, mode, cov, journey_links(journeys), hist,
                       json.loads(related.read_text(encoding='utf-8')) if related else None, history_given)
        sel.max_chars = max_chars
        res = sel.run()
    if mode == 'balanced' and any(s['name'] == 'coverage' and s['status'] == 'stale' for s in res['sources']):
        res['notes'].append('balanced mode with a stale coverage map: refresh the map, or use strict.')
    widened = sorted({t for b in res['blind_spots'] for t in b['widened_to']})
    res['summary'] = {
        'changed_files': len(res['changes']), 'selected': len(res['selected']), 'total_tests': res['total_tests'],
        'widened_by_blind_spots': len([s for s in res['selected'] if s['strength'] == 0]),
        'tests_in_widened_scopes': len(widened), 'blind_spots': len(res['blind_spots']),
        'blind_spots_labelled_only': len([b for b in res['blind_spots'] if b['action'] == 'labelled']),
        'full_suite_selected': bool(res['total_tests']) and len(res['selected']) == res['total_tests'],
        'evidence_completeness': evidence_completeness(res['sources']),
    }
    kinds_sha = hashlib.sha256(KINDS_PATH.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
    return {'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'tool': 'select_tests.py',
            'inputs': {'repo': rel_input(repo), 'diff': rel_input(diff), 'diff_applied': diff_applied,
                       'base': None if diff else base, 'committed_only': committed_only, 'mode': mode,
                       'coverage': [rel_input(p) for p in coverage or []], 'journeys': rel_input(journeys),
                       'history': rel_input(history), 'related': rel_input(related), 'max_chars': max_chars,
                       'file_kinds_sha256': kinds_sha},
            'repo_code_hash': run_tests.code_hash(repo), 'change': change, 'mode': mode, **res}


def args_from_inputs(inputs: dict) -> dict:
    p = lambda x: Path(x) if x else None  # noqa: E731
    return {'repo': Path(inputs['repo']), 'diff': p(inputs.get('diff')), 'diff_applied': inputs.get('diff_applied', False),
            'base': inputs.get('base') or 'main', 'committed_only': inputs.get('committed_only', False), 'mode': inputs['mode'],
            'coverage': [Path(c) for c in inputs.get('coverage', [])], 'journeys': p(inputs.get('journeys')),
            'history': p(inputs.get('history')), 'related': p(inputs.get('related')), 'max_chars': inputs.get('max_chars', 8000)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--diff', type=Path, help='unified diff of the change (applied to a scratch copy of --repo)')
    ap.add_argument('--diff-applied', action='store_true', help='--repo already contains the change; reverse-apply for the base')
    ap.add_argument('--base', default='main')
    ap.add_argument('--committed-only', action='store_true')
    ap.add_argument('--mode', choices=('strict', 'balanced', 'aggressive'), default='strict')
    ap.add_argument('--coverage', type=Path, action='append', default=[], help='coverage_map.json (repeat per package)')
    ap.add_argument('--journeys', type=Path)
    ap.add_argument('--history', type=Path)
    ap.add_argument('--related', type=Path, help='related-tests output: {tool, version, command, related: {path: [tests]}}')
    ap.add_argument('--max-chars', type=int, default=8000)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    res = select(args.repo, args.diff, args.diff_applied, args.base, args.committed_only, args.mode, args.coverage,
                 args.journeys, args.history, args.related, args.max_chars)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=2) + '\n', encoding='utf-8')
    s = res['summary']
    print(f'{s["selected"]} of {s["total_tests"]} test files selected ({s["widened_by_blind_spots"]} only by widening), '
          f'{s["blind_spots"]} blind spot(s), evidence completeness {s["evidence_completeness"]} -> {args.out}')
    for c in res['command']['commands']:
        print(f'  (cd {c["cwd"]} && {" ".join(c["argv"])})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
