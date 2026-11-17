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

A template rendered by name from Python views (import_graph.py `renders`) gets import-graph evidence
through the views, and coverage evidence from the tests that ran a rendering view's function; a
template or static file is still a non_code blind spot (only a rendering test can notice), widened
to e2e and visual tests, else to the tests that reach it through the views that render it, else
the package. A data file in a directory is data_or_schema even in a single-package repository;
only one at the repository root is repository-level configuration. In --committed-only mode,
repo_code_hash is the hash of the HEAD tree that was analysed, not of the working tree.
"""
from __future__ import annotations
import argparse, difflib, hashlib, json, os, re, shutil, subprocess, sys, tarfile, tempfile, io
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True   # no __pycache__ in the skill folder when this script imports its siblings
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


REV_RX = re.compile(r'[0-9A-Za-z][0-9A-Za-z._/@{}~^+-]*')


def checked_rev(repo: Path | None, value, what: str) -> str:
    """A history file's commit or base as a revision git can never read as an option: it must look like a
    hash or a ref name (no leading '-', no spaces, no '..'), and with a repository it must resolve to a
    commit (`git rev-parse --verify --end-of-options <rev>^{commit}`); the full commit id is returned then."""
    v = str(value or '')
    if not REV_RX.fullmatch(v) or '..' in v:
        raise SystemExit(f'{what}: {v!r} is not a commit id or ref name (refused before it reached git)')
    if repo is None:
        return v
    res = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--verify', '--quiet', '--end-of-options', f'{v}^{{commit}}'],
                         capture_output=True, text=True)
    if res.returncode:
        raise SystemExit(f'{what}: {v!r} is not a commit in {repo}')
    return res.stdout.strip()


def extract(tar_bytes: bytes, dest: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        try:
            tf.extractall(dest, filter='data')
        except TypeError:                    # Python without extraction filters (before 3.12 and the backports)
            tf.extractall(dest)


# ---------------------------------------------------------------- inputs
def load_history(path: Path | None, repo: Path | None = None) -> list[dict]:
    """[{id, changed: [paths], failed: [test files], appearances: {test file: runs}}] from history.json.

    A change gives its `diff` (a file next to history.json), or names its `commit` in --repo's git history: the
    diff is then the commit against its parent, and the parent is the change's `base` (replay.py checks it
    out). A change may also name a `base` ref for its diff. Go results (gotestsum JUnit, whose classname is an
    import path) are mapped to their _test.go files through go.mod in --repo; a package that failed to build is
    `<package dir>/(package)`."""
    if not path:
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    base = path.parent
    out = []
    for ch in data.get('changes', []):
        ref = ch.get('base')
        if ref is not None:
            checked_rev(None, ref, f'history {ch.get("id")} base')   # resolved when replay.py checks it out
        if ch.get('diff'):
            diff_text = (base / ch['diff']).read_text(encoding='utf-8', errors='replace')
        elif ch.get('commit'):
            if repo is None:
                raise SystemExit(f'history {ch["id"]}: names a commit, which needs --repo (a git checkout)')
            sha = checked_rev(repo, ch['commit'], f'history {ch.get("id")} commit')
            diff_text = git(repo, 'diff', '-M', '--no-color', '--no-ext-diff', '--end-of-options', f'{sha}^', sha, '--')
            ref = ref or f'{ch["commit"]}^'
        else:
            raise SystemExit(f'history {ch["id"]}: needs a diff file or a commit')
        changed = sorted({p for f in diff_scope.parse_diff(diff_text) for p in (f['path'], f.get('old_path')) if p})
        failed, seen, cases_out = set(), set(), []
        for run in ch.get('runs', []):
            pkg = run.get('package', '.')
            root = (repo if pkg in ('.', '') else repo / pkg) if repo is not None else None
            cases, _ = parse_results.parse_junit(base / run['junit'], ch['id'], root=root)
            for c in cases:
                file = test_file_of(c, pkg, root)
                seen.add(file)
                cases_out.append({'id': c['id'], 'file': file, 'status': c['status'], 'package': pkg})
                if c['status'] in ('failed', 'error'):
                    failed.add(file)
        out.append({'id': ch['id'], 'diff': ch.get('diff'), 'commit': ch.get('commit'), 'base': ref,
                    'diff_text': diff_text, 'changed': changed, 'failed': sorted(failed), 'seen': sorted(seen),
                    'cases': cases_out, 'full_run': ch.get('full_run', False), 'description': ch.get('description', '')})
    return out


PACKAGE_LEVEL = '(package)'


def test_file_of(case: dict, package: str, root: Path | None = None) -> str:
    f = case.get('file') or case.get('classname') or case['id'].split('::')[0]
    f = f.replace('\\', '/')
    f = f[2:] if f.startswith('./') else ('' if f == '.' else f)
    # a Go package that failed to build has its directory as file: a package-level result
    if root is not None and (not f or (not PurePosixPath(f).suffix and (root / f).is_dir())):
        f = f'{f}/{PACKAGE_LEVEL}' if f else PACKAGE_LEVEL
    return f if package in ('.', '') or f.startswith(package + '/') else f'{package}/{f}'


def package_level_selected(failed: str, chosen: set[str]) -> bool:
    """A package-level failure (`<dir>/(package)`: the package did not build) is selected when any test file of
    that package directory is."""
    if not failed.endswith(PACKAGE_LEVEL):
        return False
    d = failed[:-len(PACKAGE_LEVEL)].rstrip('/')
    return any(str(PurePosixPath(c).parent) == (d or '.') for c in chosen)


def load_baseline_failures(path: Path | None) -> set[str]:
    """Tests that already fail at the base, before the change (a JSON list of test files or ids, or
    {"failures": [...]}): never evidence of a change's impact, and not counted by replay."""
    if not path:
        return set()
    data = json.loads(path.read_text(encoding='utf-8'))
    items = data.get('failures', []) if isinstance(data, dict) else data
    return {str(x).replace('\\', '/').split('::')[0] for x in items} | {str(x).replace('\\', '/') for x in items}


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
    if node.get('fixture') or graph.excluded_dir(path):
        return 'fixture'
    if import_graph.match(path, KINDS['snapshot']):
        return 'snapshot'
    if entry['kind'] == 'docs' or import_graph.match(path, KINDS['docs']):
        return 'docs'
    if node.get('rust_role') == 'bench':
        return 'bench'
    if node.get('test_level') and not node.get('inline_tests'):
        return 'test'
    named_only = any(x['file'] == path for x in graph.not_tests)   # test_paths.py: a module named like a test
    if import_graph.match(path, KINDS['fixture']) or (entry['kind'] == 'test' and not named_only):
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
        # a data file in a directory (records the app or tests read) is data, whatever the package depth; only one at
        # the root of a single-package repository is repository-level configuration
        return 'data_or_schema' if pkg != '.' or '/' in path else 'global_config'
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
    return diff_scope.functions(path, text)[0]


def naming_matches(path: str, tests: list[str], package_of) -> list[str]:
    p = PurePosixPath(path)
    stem = p.name.split('.')[0]
    if stem.lower() in NAMING_SKIP:
        return []
    if '-' in stem and p.suffix == '.py':
        stem = stem.replace('-', '_')        # test_install_gauge.py tests install-gauge.py (loaded by file path)
    names = {f'test_{stem}.py', f'{stem}_test.py', f'{stem}_test.go', f'{stem}_test.dart', f'{stem}_spec.rb',
             f'{stem}Test.java', f'{stem}Tests.java', f'{stem}IT.java', f'{stem}Test.kt', f'{stem}Tests.kt',
             f'{stem}Tests.swift', f'{stem}Test.swift', f'{stem}Tests.cs', f'{stem}Test.cs'}
    js = re.compile(r'^' + re.escape(stem) + r'\.(test|spec|e2e|visual|cy)\.[cm]?[jt]sx?$')
    if p.suffix == '.go':
        # a Go test sits in its package's directory: x_test.go elsewhere tests another package's x.go
        return sorted(t for t in tests if PurePosixPath(t).name == f'{stem}_test.go' and PurePosixPath(t).parent == p.parent)
    pkg = package_of(path)
    return sorted(t for t in tests if package_of(t) == pkg and (PurePosixPath(t).name in names or js.match(PurePosixPath(t).name)))


def can_load(site: dict, path: str) -> bool:
    """Whether a dynamic loading site's language can load the file: Python's importlib loads .py modules, a
    JavaScript import()/require() loads JavaScript, JSON, styles and the like, never Python, Go or Rust."""
    ext = PurePosixPath(path).suffix.lower()
    if site.get('lang') == 'python':
        return ext == '.py'
    return ext not in ('.py', '.go', '.rs')


def fixture_host(fixture_dir: str) -> str:
    """The folder whose tool a fixture project serves: the part of its path before the examples/, fixtures/,
    testdata/ ... folder that holds it (tool/examples/app -> tool), or its parent folder."""
    import repo_layout
    parts = fixture_dir.split('/')
    for i, part in enumerate(parts):
        if part in repo_layout.FIXTURE_PARENTS:
            return '/'.join(parts[:i]) or '.'
    return '/'.join(parts[:-1]) or '.'


class Selector:
    def __init__(self, repo: Path, base_dir: Path, new_dir: Path, scope: dict, mode: str, coverage: list[dict],
                 journeys: list[dict], history: list[dict], related: dict | None, history_given: bool,
                 cargo_metadata: dict | None = None, include_fixtures: bool = False, fixture_dirs=(), exclude=()):
        self.repo, self.base_dir, self.new_dir, self.scope, self.mode = repo, base_dir, new_dir, scope, mode
        self.coverage, self.journeys, self.history, self.related = coverage, journeys, history, related
        self.history_given = history_given
        self.go_test_flags: str | None = None
        self.baseline_failures: set[str] = set()
        # the base tree differs from the changed tree only in the changed paths: build the changed tree's graph,
        # then the base graph reusing its reads, parses and (when no manifest or configuration changed, and no
        # file was added, deleted or renamed) its package detection and Cargo model
        changed = {p for f in scope['files'] for p in (f['path'], f.get('old_path')) if p}
        same_layout = all(f['status'] == 'modified' for f in scope['files']) and not any(
            import_graph.match(p, KINDS['build_config']) or import_graph.match(p, KINDS['lockfiles'])
            or import_graph.match(p, KINDS['ci']) for p in changed)
        layout = {'include_fixtures': include_fixtures, 'fixture_dirs': fixture_dirs, 'exclude': exclude}
        self.g_new = import_graph.Graph(new_dir, cargo_metadata=cargo_metadata, **layout).build()
        import_graph.js_dynamic_targets(self.g_new)
        self.g_base = import_graph.Graph(base_dir, reuse=self.g_new, changed=changed, reuse_packages=same_layout,
                                         cargo_metadata=cargo_metadata, **layout).build()
        import_graph.js_dynamic_targets(self.g_base)
        self.tests = self.g_new.tests()
        self.test_set = set(self.tests)
        self.reasons: dict[str, list[dict]] = {}
        self.blind: list[dict] = []
        self.notes: list[str] = []
        self._left_out: dict[str, dict] = {}
        self._named_by: dict[str, str] = {}
        if cargo_metadata is not None:
            self.notes.append(self.g_new.cargo_metadata_note or '--cargo-metadata: no Cargo.toml at the repository '
                              'root, so the cargo metadata was not used.')

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
        if test not in self.test_set:
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
                    if t not in self.baseline_failures:     # failing at the base already: not evidence
                        counts.setdefault(t, []).append(h['id'])
        touching = [h['id'] for h in self.history if path in h['changed'] or (old and old in h['changed'])]
        for t, ids in counts.items():
            if len(ids) >= 2:
                self.add(t, 'co_failure', f'failed in {len(ids)} of {len(touching)} past changes to this file ({", ".join(ids)})', path)

    def render_evidence(self, entry: dict) -> list[str]:
        """Coverage evidence for a template: the tests that ran a view function rendering it (or a template that
        extends or includes it). Returns the rendering sites as 'file:line'."""
        path = entry['path']
        on_base = entry['status'] != 'added'
        graph, root = (self.g_base, self.base_dir) if on_base else (self.g_new, self.new_dir)
        target = entry.get('old_path') or path if on_base else path
        tpls, todo = {target}, [target]
        while todo:
            cur = todo.pop()
            for src, _via in graph.reverse.get(cur, []):
                if graph.is_template(src) and src not in tpls:
                    tpls.add(src)
                    todo.append(src)
        sites = [(src, r['line'], r['template']) for src, e in graph.files.items() for r in e.get('renders', [])
                 if r['template'] in tpls]
        for m in self.coverage:
            for src, line, tpl in sites:
                per = m['files'].get(src)
                text = self.read(root, src) if per is not None else None
                if text is None:
                    continue
                tag = ' (coverage map is stale for this file)' if src in m['_stale'] else ''
                for fn in functions_of(src, text):
                    if fn['start'] <= line <= fn['end']:
                        seen = set()
                        for ln in range(fn['start'], fn['end'] + 1):
                            for idx in per.get(str(ln), []):
                                tid = m['tests'][idx]
                                f = tid.split('::')[0]
                                if f not in seen:
                                    seen.add(f)
                                    self.add(f, 'coverage', f'{tid.split("::", 1)[-1]} ran {fn["qualname"]}(), which '
                                             f'renders {tpl} ({src}:{line}){tag}', path)
                        break
        return [f'{src}:{line}' for src, line, _ in sites]

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
        tests = sorted(set(t for t in tests if t in self.test_set))
        if widen:
            for t in tests:
                self.add(t, 'blind_spot', f'{bid} {kind}: {scope}', entry['path'], strength=0)
        spot = {'id': bid, 'path': entry['path'], 'kind': kind, 'action': action, 'required_scope': scope,
                'widened_to': tests if widen else [], 'would_widen_to': tests, 'note': note}
        left = self._left_out.get(entry['path']) if scope in ('package', 'package_and_contract') else None
        if left:
            # the package scope was narrowed by evidence (package_tests): say what was left out and why
            spot['left_out'] = [{k: x[k] for k in ('family', 'tests', 'reason')} for x in left['left_out']]
            spot['note'] = note + ' ' + left['note']
        elif scope in ('package', 'package_and_contract') and self._named_by.get(entry['path']):
            spot['note'] = note + ' ' + self._named_by[entry['path']]
        self.blind.append(spot)

    def package_tests(self, path: str) -> list[str]:
        """The tests of path's package that a change to it can reach by language (import_graph.same_family): a Rust
        file's crate's Rust tests, never the Python tests beside them, and a Python or JavaScript file's tests, never
        Rust or Go ones. A file of no language (data, styles, configuration) reaches every test of the package."""
        if path.endswith('.go') and self.g_new.go_mods:
            return self.go_package_tests(path)
        pkg = self.g_new.package_of(path)
        folder = self.root_folder(path)
        if folder:
            # a manifest-less root is not one package: the top-level folder that holds tests is the scope, with the
            # guide checks that run its scripts and every test that reaches the file
            g = self.g_new
            mine = [t for t in self.tests if t.startswith(folder + '/') or
                    any(i['via'] == 'runs' and i['path'].startswith(folder + '/') for i in g.files[t]['imports']
                        if g.files[t].get('command_test'))]
            reach = set(self.g_new.tests_reaching(path)) | set(self.g_base.tests_reaching(path))
            return import_graph.same_family(path, sorted(set(mine) | reach))
        mine = self.tests if pkg == '.' else [t for t in self.tests if self.g_new.package_of(t) == pkg]
        mine = import_graph.same_family(path, mine)
        if pkg == '.' and import_graph.family(path) is None:
            mine = self.by_evidence(path, mine)
        return mine

    FAMILY_NAME = {'rust': 'Rust', 'go': 'Go'}

    def by_evidence(self, path: str, tests: list[str]) -> list[str]:
        """Narrow the root package's tests for a file of no language (a data file, a PowerShell or shell script, UI
        markup) by what the sources show. A compiled test (Rust, Go) can read such a file only through a path written
        in its language's source, so those tests stay in only when a file of that language names the file or its
        folder; otherwise they are left out, and the blind spot says so (`left_out`). Tests of interpreted languages
        stay: they build paths at run time too freely to rule out. The tests that name the file are noted."""
        if path in self._left_out or path in self._named_by:
            return [t for t in tests if import_graph.family(t) not in self._left_out.get(path, {}).get('families', ())]
        g = self.g_new
        name = PurePosixPath(path).name
        folder = PurePosixPath(path).parent.name
        needles = [name] + ([f'{folder}/', f'"{folder}"', f"'{folder}'"] if folder else [])

        def names_it(f: str) -> int | None:
            text = g.text(f) or ''
            hit = min((i for i in (text.find(n) for n in needles) if i >= 0), default=-1)
            return None if hit < 0 else text.count(chr(10), 0, hit) + 1
        families, left = [], []
        for fam, label in self.FAMILY_NAME.items():
            mine = [t for t in tests if import_graph.family(t) == fam]
            if not mine:
                continue
            ext = '.rs' if fam == 'rust' else '.go'
            if any(names_it(f) for f in sorted(g.all_paths) if f.endswith(ext) and not g.excluded_dir(f)):
                continue
            families.append(fam)
            left.append({'family': fam, 'tests': len(mine), 'label': label,
                         'reason': f'no {label} file names {name}' + (f' or its folder {folder}/' if folder else '') +
                                   ', and a compiled test reads a file only through a path in its source'})
        named = []
        for t in tests:
            if import_graph.family(t) in families or t not in g.all_paths:
                continue
            text = g.text(t) or ''
            at = text.find(name)
            if at >= 0:
                named.append(f'{t}:{text.count(chr(10), 0, at) + 1}')
        said = (f'Named in {", ".join(named[:3])}' + (f' and {len(named) - 3} more test file(s)' if len(named) > 3 else '')
                + '.') if named else ''
        if left:
            self._left_out[path] = {'families': families, 'left_out': left, 'note': (said + ' ' if said else '') + ' '.join(
                f'Left out: the {x["tests"]} {x["label"]} test file(s), because {x["reason"]}.' for x in left)}
        else:
            self._named_by[path] = said
        return [t for t in tests if import_graph.family(t) not in families]

    def root_folder(self, path: str) -> str | None:
        """The top-level folder that stands in for a package: path's package is a repository root with no manifest
        (a tool repository of folders with scripts), path lies in a top-level folder, and that folder holds tests.
        None otherwise."""
        g = self.g_new
        if g.package_of(path) != '.' or '/' not in path:
            return None
        root = next((p for p in g.packages if p['root'] == '.'), None)
        if root is None or root.get('manifests'):
            return None
        top = path.split('/', 1)[0]
        return top if any(t.startswith(top + '/') for t in self.tests) else None

    def scope_name(self, path: str) -> str:
        folder = self.root_folder(path)
        return (f'its folder {folder}/ (the repository root has no manifest, so each top-level folder with tests '
                'stands in for a package)') if folder else 'its package'

    def go_package_tests(self, path: str) -> list[str]:
        """A Go package is a directory: its own _test.go files and the tests that import it; when there are none,
        the Go tests of its module. Never another language's tests (a Swift agent or a Node tool beside it)."""
        d = PurePosixPath(path).parent
        same = {t for t in self.tests if t.endswith('_test.go') and PurePosixPath(t).parent == d}
        reach = {t for g in (self.g_new, self.g_base) for t in g.tests_reaching(path) if t.endswith('_test.go')}
        if same or reach:
            return sorted(same | reach)
        mod_dir = max((m for m in self.g_new.go_mods.values() if m in ('.', '') or path.startswith(m + '/')),
                      key=len, default='.')
        return sorted(t for t in self.tests if t.endswith('_test.go') and (mod_dir in ('.', '') or t.startswith(mod_dir + '/')))

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
                    if not can_load(d, path):
                        continue            # a JavaScript import() cannot load a Python module, nor the reverse
                    if (d['targets'] is not None and path in d['targets']) or (d['targets'] is None and not static_importers):
                        out.append(f'{src}:{d["line"]} {d["text"]}')
        return sorted(set(out))

    # ------------------------------------------------------------ run
    def run(self) -> dict:
        files = self.scope['files']
        cov = self.coverage_status()
        changes = []
        source_files = [p for p, e in self.g_base.files.items() if e['lang'] and (not e['test_level'] or e.get('inline_tests'))]
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
            if kind == 'bench':
                crate = self.g_new.files.get(path, self.g_base.files.get(path, {})).get('crate')
                self.notes.append(f'{path} belongs to a bench target of {crate}: cargo test does not build or run '
                                  'benches, so no test is selected for it. Check it still builds with '
                                  f'`cargo bench -p {crate} --no-run` (or the CI job that runs the benchmarks).')
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
            if kind in ('source', 'unknown', 'fixture'):
                self.evidence(entry)
            if kind == 'fixture':
                graph = self.g_base if entry['status'] == 'deleted' else self.g_new
                fx = graph.excluded_dir(entry.get('old_path') or path if entry['status'] == 'deleted' else path) or \
                    graph.excluded_dir(path) or path
                host = fixture_host(fx)
                why = next((x['reason'] for x in graph.excluded if x['dir'] == fx), 'excluded')
                mine = [t for t in self.tests if host == '.' or t.startswith(host + '/')]
                reach = sorted(set(self.g_new.tests_reaching(path)) | set(self.g_base.tests_reaching(entry.get('old_path') or path)))
                note = (f'File in {fx} ({why}), left out of the repository\'s tests: a sample the repository\'s own '
                        f'tests read. {len(reach)} test(s) name or import it; it widens to the {len(mine)} test(s) of '
                        f'{host if host != "." else "the repository"}, the tool that owns the sample.')
                if not mine and not reach:
                    mine = self.package_tests(path)
                    note += ' No test under that folder, so its package.'
                self.blind_spot(entry, 'fixture_project', note, 'fixture_host', sorted(set(mine) | set(reach)))
                continue
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
                    elif (self.g_new.files.get(path) or self.g_base.files.get(path) or {}).get('rust_role') == 'fixture_bin':
                        self.blind_spot(entry, 'shared_fixture', 'Helper binary under tests/ (cargo builds it for the '
                                        'integration tests; it is not a test target): the tests that launch it are not '
                                        'visible as imports, so its reach is the tests of its crate.',
                                        'package', self.package_tests(path))
                    elif PurePosixPath(path).suffix.lower() in KINDS['data_ext']:
                        self.blind_spot(entry, 'shared_fixture', 'Data or manifest file in a test directory that no test '
                                        'imports (a package manifest, a JSON fixture read by path): its reach is the package.',
                                        'package', self.package_tests(path))
                    else:
                        pkg_fw = next((p.get('frameworks') or [] for p in self.g_new.packages
                                       if p['root'] == self.g_new.package_of(path)), [])
                        how = ('it is probably loaded by runner configuration (setupFiles, globalSetup)'
                               if set(pkg_fw) & build_command.JS_FWS else
                               'the tests that read or load it by path are not visible as imports')
                        self.blind_spot(entry, 'shared_fixture', f'Test helper or setup file no test imports: {how}, so '
                                        'its reach is the package.', 'package', self.package_tests(path))
            elif kind == 'global_config' and import_graph.is_rust_config(path) and self.g_new.rust_crates:
                rust = [t for t in self.tests if import_graph.family(t) == 'rust']
                self.blind_spot(entry, 'global_config', 'Cargo workspace manifest, lockfile or toolchain: it can change how '
                                'every Rust test builds and runs, and not the tests of other languages.', 'language', rust)
            elif kind == 'global_config' and entry['status'] == 'added' and import_graph.match(path, KINDS['ci']):
                self.blind_spot(entry, 'global_config', 'A new CI file: it adds jobs or steps and nothing that exists '
                                'refers to it yet, so it cannot change how an existing test runs or what it tests. No '
                                'test is widened for it; what it runs is checked by running the new workflow itself. (A '
                                'modified CI file widens to the full suite.)', 'no_existing_test', [])
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
                                f'imports do not show. Widened to the tests of {self.scope_name(path)}.' + (f' Plus {len(contract)} contract test file(s).' if contract else
                                                          ' No contract tests found.'),
                                'package_and_contract', self.package_tests(path) + contract)
            elif kind == 'non_code':
                reach = sorted(set(self.g_new.tests_reaching(path)) | set(self.g_base.tests_reaching(path)))
                rendered = []
                if reach and (self.g_new.is_template(path) or self.g_base.is_template(entry.get('old_path') or path)):
                    # a server-rendered template: the tests reaching the views that render it are evidence
                    self.evidence(entry)
                    rendered = self.render_evidence(entry)
                ui = [t for t in reach if self.g_new.files[t]['test_level'] in ('e2e', 'visual')]
                note = 'Template, style, translation or image: only tests that render it can notice a change.'
                if rendered:
                    note += f' Rendered by name at {", ".join(rendered[:3])}' + (' and more' if len(rendered) > 3 else '') + '.'
                if not ui:
                    ui = [t for t in self.package_tests(path) if self.g_new.files[t]['test_level'] in ('e2e', 'visual')]
                    note += ' No e2e or visual test reaches it by import, so every e2e and visual test in the package.'
                if not ui and reach:
                    ui = reach
                    note += (' The package has no e2e or visual tests, so the tests that reach it through the views '
                             'and templates that render or link it (they can notice a missing file or a broken '
                             'render, not a visual change).')
                if not ui:
                    ui = self.package_tests(path)
                    note += ' The package has no e2e or visual tests, so its whole suite.'
                self.blind_spot(entry, 'non_code', note, 'e2e_and_visual', ui)
            if kind in ('source', 'unknown'):
                sites = self.dynamic_sites(entry)
                if sites:
                    self.blind_spot(entry, 'dynamic_reference', 'Loaded by a computed name, so imports cannot show every '
                                    'test that reaches it: ' + '; '.join(sites[:3]), 'package', self.package_tests(path))
                self.unresolved_importers(entry)
                if entry['status'] in ('deleted', 'renamed'):
                    gone = entry.get('old_path') or path
                    importers = sorted(self.g_base.tests_reaching(gone))
                    self.blind_spot(entry, 'deleted_or_renamed', f'{gone} was {entry["status"]}; tests that imported it in '
                                    f'the base tree: {len(importers)}.', 'importers', importers)
                linked = any(r['changed_path'] == path and r['source'] != 'blind_spot' for rs in self.reasons.values() for r in rs)
                strong = self.selected_for(path)
                if not strong and entry['status'] != 'deleted':
                    self.unmapped(entry, linked)
        n_changed_src = len([c for c in changed_sources if c['status'] != 'added'])
        if source_files and n_changed_src / len(source_files) > HUGE_SHARE:
            self.blind_spot({'path': '(change)'}, 'huge_change', f'{n_changed_src} of {len(source_files)} source files '
                            f'changed (over {int(HUGE_SHARE * 100)}%): selection would save little, so run the full suite.',
                            'full_suite', self.tests)
        return self.result(changes, cov)

    def unresolved_importers(self, entry: dict) -> None:
        """Files whose bare import could not be resolved (sys.path set from the environment, PYTHONPATH) and whose
        candidate is the changed file: the tests among them, and the tests that reach them, are one blind spot
        widened to exactly those tests, unless import evidence already links each of them to the file."""
        path = entry['path']
        sites: dict[str, int] = {}
        for graph, target in ((self.g_new, path), (self.g_base, entry.get('old_path') or path)):
            for src, e in graph.files.items():
                for u in e.get('unresolved') or ():
                    if target in (u.get('candidates') or ()):
                        sites.setdefault(src, u['line'])
        if not sites:
            return
        tests: set[str] = set()
        for src in sites:
            if src in self.test_set:
                tests.add(src)
            tests |= set(self.g_new.tests_reaching(src)) & self.test_set
        linked = {t for t, rs in self.reasons.items()
                  if any(r['changed_path'] == path and r['source'] == 'import_graph' for r in rs)}
        tests -= linked
        if not tests:
            return
        shown = [f'{src}:{line}' for src, line in sorted(sites.items())]
        self.blind_spot(entry, 'dynamic_reference',
                        f'{len(sites)} file(s) import a module of this name that the graph could not resolve (sys.path or '
                        f'PYTHONPATH set at run time): {", ".join(shown[:4])}' + (' and more' if len(shown) > 4 else '') +
                        f'. This file is a candidate, so the {len(tests)} test file(s) among them and reaching them '
                        'are run.', 'unresolved_importers', sorted(tests))

    def unmapped(self, entry: dict, linked: bool) -> None:
        """An unmapped source file: widened to its package; the note says what is known about its tests, and says
        "No tests found" only when nothing links it and its package scope holds no test. A file in a language with no
        import parser whose package has no test that could be found falls back to the full suite."""
        path = entry['path']
        node = self.g_new.files.get(path, {})
        lang = node.get('lang')
        unparsed = bool(lang) and self.g_new.languages.get(lang) == 'no_parser'
        if not lang and self.unread_language(path):
            lang, unparsed = self.unread_language(path), True
        tests = self.package_tests(path)
        weak = sorted({t for t, rs in self.reasons.items() for r in rs if r['changed_path'] == path and r['source'] != 'blind_spot'})
        importers = sorted({src for src, e in self.g_new.files.items() for u in e.get('unresolved', [])
                            if path in (u.get('candidates') or [])})
        parts = ['No evidence source links this file to a test' if not linked else
                 f'Only weak evidence (one strength-1 source) links this file to a test ({", ".join(weak[:3])})']
        if unparsed:
            parts[0] += f' (no import parser for {lang})'
        if importers:
            parts.append(f'{len(importers)} file(s) import a module of this name the graph could not resolve '
                         f'(sys.path or PYTHONPATH set at run time): {", ".join(importers[:4])}')
        scope = 'package'
        if not tests and unparsed and self.tests:
            scope, tests = 'full_suite', list(self.tests)
            parts.append(f'Its package has no test file this selector could find and {lang} has no import parser, '
                         'so it widens to the full suite')
        elif tests:
            parts.append(f'It widens to the {len(tests)} test file(s) of {self.scope_name(path)}')
        elif not linked and not importers:
            parts.append('No tests found for it: hand it to ticket-to-tdd or journey-to-tests')
        else:
            parts.append('Its package has no test file of its language to widen to')
        self.blind_spot(entry, 'unmapped_source', '. '.join(parts) + '.', scope, tests)

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
                if t in self.baseline_failures:
                    continue
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
            if t in self.g_new.rust_tests:
                # its libtest ids: a verify run may record a unit test under its crate's root file
                item['test_ids'] = [x['id'] for x in self.g_new.rust_tests[t]]
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
        scripts = {}
        for p in packages:
            pj = import_graph.read_jsonc(self.new_dir / p['root'] / 'package.json') if p['root'] != '.' else \
                import_graph.read_jsonc(self.new_dir / 'package.json')
            if isinstance(pj.get('scripts'), dict) and isinstance(pj['scripts'].get('test'), str):
                scripts[p['root']] = pj['scripts']['test']
        go_flags, go_from = [], None
        if any(s['file'].endswith('_test.go') for s in selected):
            if self.go_test_flags is not None:
                import shlex
                go_flags, go_from = shlex.split(self.go_test_flags), '--go-test-flags'
            else:
                go_flags, go_from = build_command.ci_go_flags(self.new_dir)
        g = self.g_new
        self_tests = {t: {'command': g.files[t]['self_test'],
                          'framework': 'command' if g.files[t].get('command_test') else 'selftest'}
                      for t in self.tests if g.files[t].get('self_test')}
        run_roots = {}
        for t in self.tests:
            if t.endswith('.py') and t not in self_tests:
                cfg = g.pytest_config_of(t)
                d, pkg = (cfg or {}).get('dir') or '.', g.package_of(t)
                if d != pkg and (pkg == '.' or d.startswith(pkg + '/')):
                    run_roots[t] = d
        command = build_command.build(selected, packages, full, {p['root']: p['test_files'] for p in packages},
                                      {t: self.g_new.files[t]['test_level'] for t in self.tests}, self.max_chars,
                                      scripts=scripts, builds=self.builds_first(changes, selected),
                                      go_flags=go_flags, go_flags_from=go_from, cargo=self.cargo_info(),
                                      python_runners=self.g_new.python_runners, self_tests=self_tests,
                                      run_roots=run_roots, excluded=[x['dir'] for x in g.excluded])
        self.notes += build_command.not_run_lines(command)
        sources = self.sources(changes, cov)
        n_docs = len([c for c in changes if c['kind'] == 'docs'])
        out = {
            'sources': sources, 'changes': changes, 'packages': packages, 'selected': selected,
            'considered_not_selected': considered, 'blind_spots': self.blind,
            'total_tests': len(self.tests), 'not_selected_count': len(self.tests) - len(selected), 'command': command,
            'docs_only': bool(changes) and n_docs == len(changes),
            'notes': self.notes,
        }
        lc = self.language_coverage(changes)
        if lc:
            out['language_coverage'] = lc
        if self.g_new.excluded:
            out['excluded'] = [dict(x) for x in self.g_new.excluded]
        if self.g_new.not_tests:
            nt = self.g_new.not_tests
            self.notes.append(f'{len(nt)} Python file(s) named like tests define no test function or class, so they '
                              'are not counted as tests: ' + ', '.join(x['file'] for x in nt[:5]) +
                              (' and more' if len(nt) > 5 else '') + '.')
        return out

    def unread_language(self, path: str) -> str | None:
        """The language of a file nothing here parses and the graph does not list (lang_census's names): script
        languages (PowerShell, Shell, Batch ...), UI markup (Slint, QML, XAML ...) and languages outside the graph's
        own table (Lua, C, C++ ...). None for a file the graph gives a language, and for data, styles and documents."""
        import lang_census
        ext = PurePosixPath(path).suffix
        low = ext.lower()
        if low in import_graph.NO_PARSER or low in import_graph.PY_EXT or low in import_graph.JS_EXT or low == '.pyi':
            return None
        return (lang_census.SCRIPT_BY_EXT.get(low) or lang_census.UI_MARKUP_BY_EXT.get(low)
                or lang_census.LANG_BY_EXT.get(ext) or lang_census.LANG_BY_EXT.get(low))

    def language_coverage(self, changes: list[dict]) -> dict | None:
        """Which languages the import graph parsed, and the changed files in a language it has no parser for: the
        graph's own no_parser languages (Kotlin, Swift ...) and, for changed source files, the languages nothing here
        reads at all (unread_language: Slint, Lua, PowerShell, shell ...). With any such file, `warning` is set: those
        files are not traced to tests (each is an unmapped blind spot, widened), and the report must name the language
        in its limitations. None when every language was parsed and no changed file is in an unread one."""
        langs = {**self.g_base.languages, **self.g_new.languages}
        no_parser = sorted(l for l, s in langs.items() if s == 'no_parser')
        hit: dict[str, list[str]] = {}
        for c in changes:
            if c['kind'] in ('docs', 'bench'):
                continue
            node = self.g_new.files.get(c['path']) or self.g_base.files.get(c.get('old_path') or c['path']) or {}
            if node.get('lang') in no_parser:
                hit.setdefault(node['lang'], []).append(c['path'])
            elif not node.get('lang') and c['kind'] in ('source', 'unknown'):
                name = self.unread_language(c['path'])
                if name:
                    hit.setdefault(name.lower(), []).append(c['path'])
        if not no_parser and not hit:
            return None
        out = {'parsed': sorted(l for l, s in langs.items() if s == 'parsed'), 'no_parser': sorted(set(no_parser) | set(hit)),
               'changed_unparsed': sorted(hit), 'changed_unparsed_files': {l: fs[:10] for l, fs in sorted(hit.items())},
               'warning': None}
        if hit:
            n = sum(len(v) for v in hit.values())
            out['warning'] = (f'{n} changed file(s) in {", ".join(sorted(hit))} have no import parser here '
                              f'({", ".join(f for v in hit.values() for f in v[:2])}): nothing traces them to tests, so '
                              'each is an unmapped blind spot widened to its package (to the full suite when no test '
                              'of its package could be found), and tests in that language are found by file name '
                              "only. Name this in the report's limitations.")
        return out

    def cargo_info(self) -> dict | None:
        """What build_command needs for cargo commands: every Rust test file's libtest ids, each crate's test files,
        and the CI's own workspace-wide `cargo test` command when there is one."""
        g = self.g_new
        if not g.rust_crates:
            return None
        crates: dict[str, dict] = {}
        for f, ts in g.rust_tests.items():
            for t in ts:
                c = crates.setdefault(t['crate'], {'dir': t['package'], 'root': t['root'], 'test_files': []})
                if f not in c['test_files'] and f in self.test_set:
                    c['test_files'].append(f)
        workspace = None
        for gate in g.ci_gates:
            cmd = gate.get('command') or ''
            if gate.get('runner') == 'cargo-test' and re.match(r'^cargo\s+test\b', cmd) and \
                    re.search(r'\s--(workspace|all)\b', cmd) and \
                    not re.search(r'\s(-p|--package|--features|--all-features)\b|[|&;>]|\s--(\s|$)', cmd):
                workspace = {'argv': cmd.split(), 'from': f'{gate["file"]}:{gate["line"]}'}
                break
        return {'tests': {f: ts for f, ts in g.rust_tests.items() if f in self.test_set}, 'crates': crates,
                'workspace_command': workspace}

    def builds_first(self, changes: list[dict], selected: list[dict]) -> list[dict]:
        """Workspace packages whose changed sources reach selected tests in other packages only through the
        package's build output (package.json main/exports point at dist/): those tests run the previous
        build unless the package is built first."""
        pm = next((name for f, name in (('pnpm-lock.yaml', 'pnpm'), ('yarn.lock', 'yarn'), ('bun.lockb', 'bun'))
                   if (self.new_dir / f).is_file()), 'npm')
        out = []
        for root in sorted({self.g_new.package_of(c['path']) for c in changes if c['kind'] == 'source'} - {'.'}):
            out_dir = import_graph.build_output(self.new_dir / root)
            if not out_dir:
                continue
            users = sorted({s['package'] for s in selected if s['package'] != root and any(
                r['source'] == 'import_graph' and self.g_new.package_of(r['changed_path']) == root
                for r in self.reasons.get(s['file'], []))})
            if not users:
                continue
            pj = import_graph.read_jsonc(self.new_dir / root / 'package.json')
            has_build = isinstance(pj.get('scripts'), dict) and 'build' in pj['scripts']
            why = (f'selected tests in {", ".join(users)} import {pj.get("name") or root} by name, which loads its '
                   f'{out_dir}/ build; the change is in its sources')
            if has_build:
                out.append({'package': root, 'cwd': root, 'argv': [pm, 'run', 'build'], 'why': why})
            else:
                self.notes.append(f'{root}: {why}, and it has no build script: build it before running those tests.')
        return out

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
        unread = {c['path']: self.unread_language(c['path']) for c in graph_changes if c['kind'] in ('source', 'unknown')}
        parsed = [c for c in graph_changes if (self.g_new.files.get(c['path']) or self.g_base.files.get(c['path']) or {}).get('lang')
                  in (None, *[l for l, s in langs.items() if s == 'parsed']) and not unread.get(c['path'])]
        unparsed_langs = sorted({l for l, s in langs.items() if s == 'no_parser'} | {v.lower() for v in unread.values() if v})
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
        # git runs in two rounds of parallel processes (starting a process is the slow part on Windows): the
        # commits, then the diff, the untracked files and the trees, each tree extracted as soon as it arrives
        from concurrent.futures import ThreadPoolExecutor
        base_dir = work / 'base'
        new_dir = work / 'new' if committed_only else repo
        base_dir.mkdir(parents=True)
        if committed_only:
            new_dir.mkdir(parents=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            mb = pool.submit(git, repo, 'merge-base', base, 'HEAD')
            hd = pool.submit(git, repo, 'rev-parse', 'HEAD')
            merge_base, head = mb.result().strip(), hd.result().strip()
            rng = [f'{merge_base}...HEAD'] if committed_only else [merge_base]
            jobs = [pool.submit(lambda: extract(git(repo, 'archive', '--format=tar', merge_base, binary=True),
                                                base_dir))]
            if committed_only:
                jobs.append(pool.submit(lambda: extract(git(repo, 'archive', '--format=tar', 'HEAD', binary=True),
                                                        new_dir)))
            diff_job = pool.submit(git, repo, 'diff', '-M', '--no-color', '--no-ext-diff', *rng)
            untracked_job = None if committed_only else pool.submit(git, repo, 'ls-files', '--others',
                                                                    '--exclude-standard')
            text = diff_job.result()
            untracked = untracked_job.result() if untracked_job else ''
            for j in jobs:
                j.result()
        scope_files = diff_scope.scope_from_diff(text, new_dir)
        if not committed_only:
            for rel in untracked.splitlines():
                body = Selector.read(repo, rel)
                kind, why = diff_scope.classify(rel, '\n'.join((body or '').splitlines()[:5]), body is None, repo)
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
           history_entries: list[dict] | None = None, baseline_failures: Path | None = None,
           go_test_flags: str | None = None, cargo_metadata: Path | None = None, include_fixtures: bool = False,
           fixture_dirs: list[str] | None = None, exclude: list[str] | None = None) -> dict:
    """Run a selection. history_entries overrides the entries read from --history (replay passes earlier ones only)."""
    repo = repo.resolve()
    cov = load_coverage(coverage or [])
    hist = history_entries if history_entries is not None else load_history(history, repo)
    history_given = history is not None
    if mode == 'balanced':
        missing = [x for x, ok in (('a coverage map', bool(cov)), ('history', history_given)) if not ok]
        if missing:
            raise SystemExit(f'balanced mode needs {" and ".join(missing)}; use strict (the default) or aggressive.')
    with tempfile.TemporaryDirectory(prefix='impact-') as tmp:
        base_dir, new_dir, scope, change = prepare_trees(repo, Path(tmp), diff, diff_applied, base, committed_only)
        sel = Selector(repo, base_dir, new_dir, scope, mode, cov, journey_links(journeys), hist,
                       json.loads(related.read_text(encoding='utf-8')) if related else None, history_given,
                       json.loads(cargo_metadata.read_text(encoding='utf-8')) if cargo_metadata else None,
                       include_fixtures=include_fixtures, fixture_dirs=fixture_dirs or (), exclude=exclude or ())
        sel.max_chars = max_chars
        sel.go_test_flags = go_test_flags
        sel.baseline_failures = load_baseline_failures(baseline_failures)
        if sel.baseline_failures:
            sel.notes.append(f'{len(sel.baseline_failures)} test(s) or file(s) fail at the base already '
                             '(--baseline-failures); their past failures are not co-failure evidence.')
        res = sel.run()
        # committed-only analyses the HEAD tree (git archive): record its hash, not the working tree's
        if committed_only and not diff:
            # an extracted archive is no git work tree: hash it by the walk rule without starting git to find out
            # (one process start less; on a busy Windows machine each costs about a second)
            saved = run_tests.git_files
            run_tests.git_files = lambda root: None
            try:
                code_hash = run_tests.code_hash(new_dir)
            finally:
                run_tests.git_files = saved
        else:
            code_hash = run_tests.code_hash(repo)
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
                       'file_kinds_sha256': kinds_sha,
                       **({'baseline_failures': rel_input(baseline_failures)} if baseline_failures else {}),
                       **({'go_test_flags': go_test_flags} if go_test_flags is not None else {}),
                       **({'cargo_metadata': rel_input(cargo_metadata)} if cargo_metadata else {}),
                       **({'include_fixtures': True} if include_fixtures else {}),
                       **({'fixture_dirs': list(fixture_dirs)} if fixture_dirs else {}),
                       **({'exclude': list(exclude)} if exclude else {})},
            'repo_code_hash': code_hash, 'change': change, 'mode': mode, **res}


def args_from_inputs(inputs: dict) -> dict:
    p = lambda x: Path(x) if x else None  # noqa: E731
    return {'repo': Path(inputs['repo']), 'diff': p(inputs.get('diff')), 'diff_applied': inputs.get('diff_applied', False),
            'base': inputs.get('base') or 'main', 'committed_only': inputs.get('committed_only', False), 'mode': inputs['mode'],
            'coverage': [Path(c) for c in inputs.get('coverage', [])], 'journeys': p(inputs.get('journeys')),
            'history': p(inputs.get('history')), 'related': p(inputs.get('related')), 'max_chars': inputs.get('max_chars', 8000),
            'baseline_failures': p(inputs.get('baseline_failures')), 'go_test_flags': inputs.get('go_test_flags'),
            'cargo_metadata': p(inputs.get('cargo_metadata')), 'include_fixtures': inputs.get('include_fixtures', False),
            'fixture_dirs': inputs.get('fixture_dirs'), 'exclude': inputs.get('exclude')}


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
    ap.add_argument('--baseline-failures', type=Path, help='tests that already fail at the base: JSON list of test '
                    'files or ids, or {"failures": [...]}; left out of co-failure evidence')
    ap.add_argument('--go-test-flags', help='go test flags for the Go command (default: those of the repo\'s CI or '
                    'Makefile `go test` line, e.g. "-race -timeout 25m"); "" for none')
    ap.add_argument('--cargo-metadata', type=Path, help='`cargo metadata --format-version 1 --no-deps` output: the '
                    'targets of the crates and features from cargo itself instead of the static reading of the manifests')
    ap.add_argument('--include-fixtures', action='store_true', help='count the tests of sample and fixture projects '
                    '(examples/, fixtures/, testdata/ ... with their own manifest) as the repository\'s tests; by '
                    'default they are left out and listed under `excluded`')
    ap.add_argument('--fixture-dir', action='append', default=[], help='another folder name or path that holds '
                    'fixture projects (repeatable)')
    ap.add_argument('--exclude', action='append', default=[], help='a folder to leave out of the tests and the graph '
                    '(repeatable); a change inside it is a fixture_project blind spot')
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    res = select(args.repo, args.diff, args.diff_applied, args.base, args.committed_only, args.mode, args.coverage,
                 args.journeys, args.history, args.related, args.max_chars, baseline_failures=args.baseline_failures,
                 go_test_flags=args.go_test_flags, cargo_metadata=args.cargo_metadata,
                 include_fixtures=args.include_fixtures, fixture_dirs=args.fixture_dir, exclude=args.exclude)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=2) + '\n', encoding='utf-8')
    s = res['summary']
    print(f'{s["selected"]} of {s["total_tests"]} test files selected ({s["widened_by_blind_spots"]} only by widening), '
          f'{s["blind_spots"]} blind spot(s), evidence completeness {s["evidence_completeness"]} -> {args.out}')
    for c in res['command']['commands']:
        print(f'  (cd {c["cwd"]} && {" ".join(c["argv"])})')
    if res.get('excluded'):
        print(f'  {len(res["excluded"])} fixture project(s) or folder(s) left out: '
              + ', '.join(x['dir'] for x in res['excluded'][:6]) + (' ...' if len(res['excluded']) > 6 else '')
              + ' (--include-fixtures to count their tests)', file=sys.stderr)
    for line in build_command.not_run_lines(res['command']):
        print('  note: ' + line, file=sys.stderr)
    warning = (res.get('language_coverage') or {}).get('warning')
    if warning:
        print('WARNING: ' + warning, file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
