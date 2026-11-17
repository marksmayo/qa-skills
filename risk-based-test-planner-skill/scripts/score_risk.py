#!/usr/bin/env python3
"""Score every area of a repository for risk = likelihood x impact, citing the evidence for each factor (stdlib only).

    python score_risk.py --repo <repo> --history history.json --complexity complexity.json --scope scope.json \
        --out risk.json [--journeys journey_map.json] [--input security=security_audit.json] [--input flaky=flaky_report.json]
        [--input crash=crash_report.json] [--input wcag=audit.json] [--usage usage.json] [--areas areas.json]
        [--defects bugs.json] [--finding-area KIND:ID=AREA] [--release release.json] [--coverage coverage.out]
        [--fixture-dir DIR] [--include-fixtures] [--keep-copies]
    python score_risk.py --self-test

Sample and fixture projects (repo_layout: a nested project with its own manifest, expected output or app under
examples/, fixtures/, testdata/, samples/ ..., or a --fixture-dir) are left out: never an area, never area code or
runtime configuration, their tests never count, their changed lines are named in scope_outside_areas as such, and
they are listed in fixtures_excluded (an explicit --areas entry inside one keeps it; --include-fixtures keeps all).
Files kept identical in several places (a tools/check_shared.py-style mapping, or the same content) are scored once,
in the area of the canonical copy: the other copies are excluded with the canonical path, their changes and the tests
that import them count there, and copy_groups lists each group (--keep-copies scores every copy). A script's own
self-test (a self_test() and what only it uses) is test code: it counts as a test of the script's area, its lines are
test lines, and it is never a surface; self_test.py files and files a pytest configuration collects through its own
python_files are tests, except benchmarks (bench_*.py), which are neither product code nor counted tests. In Python,
docstrings and the strings of module-level tables and multi-line constants are data: the raw patterns that read
inside strings skip them. A CI workflow an explicit --areas entry covers is scored like templates (churn, defects,
change in scope).

A root go.mod makes the module's Go packages the areas (nested packages and non-Go code under the module beside
them); --coverage takes a Go coverprofile as the test-weakness evidence. Otherwise areas are the workspace packages when the repo has several (a package with more than
SPLIT_PACKAGE source files is split by subdirectory below its source root); for a Next.js App Router project,
one per API resource (app/api/<x>), one per top-level route segment or group, the files directly in app/ by
role (app (layout), pages/(root), app (metadata routes), app (error and loading pages), app (root)), runtime config
(a next.config.* or platform file setting headers, CSP, redirects or rewrites, from the shared next_app reader),
middleware/proxy, and lib/, components/ and the other source directories split by subdirectory
(and by file-name prefix when flat and large); otherwise the immediate subdirectories of the
main source directory (the top-level directory holding the most source files, followed down
while one subdirectory holds nearly all of it), with container directories such as modules/ and
common/ split again. --areas gives them explicitly as {"areas": [{"id": ..., "paths": [...]}]}.
A root module that creates a web app (Flask(__name__), FastAPI(), express(), the module a
gunicorn/uvicorn command in a Procfile, app.yaml or Dockerfile names) is product code: it is its
own area, with the templates/ and static/ directories beside it, never root tooling. A service worker that
navigator.serviceWorker.register names is its own area in any layout. Files the scope deletes count in the change in
scope of the area covering them (scope_deleted, with a warning); a repository with no test files gets a loud warning;
in a Next.js project, source files nothing imports are hinted in unreferenced.
Test, generated, vendored, tool-configuration and docs files are not areas. Templates, styles and
data records in an area (ASSET_EXT; data only inside a directory, never a manifest) count for its
churn, defects and change in scope, but not for complexity, surfaces or test weakness; an area
holding only such files (an explicit --areas entry for templates/ or a data directory) is kept,
with those three factors not_assessed. Changed templates, styles or data in no area are named in a
warning. Source files no area
covers are scored as the area "unassigned" and named in a warning, never dropped; every other code
file outside the areas (docs, configuration, generated, fixtures, root tooling and scripts) is
listed in excluded with its reason, and root tooling gets a warning. A directory named e2e, test,
tests or spec is test code unless it is a product package (a manifest plus src/, or a package.json
with main/module/exports/bin). Live tests (live/) and test helpers or fixtures don't count as tests
(tests_not_counted). --release (release_scope.py) adds pending changesets and breaking commits to
the change-in-scope evidence and flags breaking_change areas (at least Medium). --self-test checks
the surface idioms and the directory rule.

Tests are linked to areas through their imports, resolved to files (so a top-level 'src/*' area
matches only files directly in src/; Python through the sys.path roots a test would have:
relative imports, its ancestor directories, package roots, src layouts, pytest pythonpath and
conftest sys.path.insert), and by sitting inside an area. Journeys are linked through
the files their steps cite, the surface items they cover, the route handler behind each
"POST /api/x" note, and one hop of imports from those files. A partial journey map (scoped to
some domains) leaves the areas it didn't map not_assessed for journey criticality and reach.
Findings from other skills keep their severity, read by the adapters in ADAPTERS (crash
ranking.priority P0/P1 = critical/high, WCAG serious = high, security CVSS severity).
Change in scope is a percentile capped by the size of the change (SCOPE_CAPS).

Factors (1-5, from percentiles across the areas, see RUBRIC.md):
    likelihood: churn (2), defect history (3), complexity (1), change in scope (2), test weakness (2)
    impact:     journey criticality (3), surface (3), reach (1)

Defect history counts fix commits in the window plus, with --defects (fetch_ticket.py --jql
output, read-only), Bug tickets whose component or label names the area; a bug already linked
from a fix commit is counted once.

A factor with no evidence is not_assessed, never 1: churn and defects for an area with no
history before the scope (it is flagged novel), journeys without a journey map, reach without a
journey map or usage export. Likelihood and impact are the weighted means of the assessed
factors (one decimal); risk = likelihood x impact. Floors: an open critical or high finding from
another skill makes the area High (only assumed or possible ones: at least Medium, flagged
unconfirmed_findings); novel code is at least Medium. Each carried finding records its source
report's file name; --finding-area moves a finding whose report cites the wrong file. Without a
journey map or usage export, UI areas get a warning that their impact rests on surfaces alone.

Also usable as a module: allocate(planned, budget) is the effort allocation the validator
recomputes.
"""
from __future__ import annotations
import argparse, ast, fnmatch, json, math, os, re, sys
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True   # no __pycache__ in the skill folder when this script imports its siblings
import cargo_project, code_mask, detect_conventions, diff_scope, lang_census, next_app, py_loads, repo_layout  # noqa: E402

SURFACES = json.loads((HERE.parent / 'data' / 'surfaces.json').read_text(encoding='utf-8'))
LIKELIHOOD = {'churn': 2, 'defects': 3, 'complexity': 1, 'change_in_scope': 2, 'test_weakness': 2}
IMPACT = {'journey_criticality': 3, 'surface': 3, 'reach': 1}
PRIORITY_SCORE = {'P0': 5, 'P1': 4, 'P2': 3, 'P3': 2}
# journey-mapper's scale has no P0: its P1 is critical (the top), P2 important, P3 nice to have. A map whose journeys use
# only P1-P3 is read on that scale, so its critical journeys score 5 like a P0 elsewhere.
JOURNEY_MAPPER_SCORE = {'P1': 5, 'P2': 4, 'P3': 2}


def priority_scale(journeys: list[dict]) -> tuple[dict, str]:
    """(priority -> journey criticality, the scale's name) for the priorities a journey map uses."""
    used = {j.get('priority') for j in journeys}
    if journeys and 'P0' not in used and used <= set(JOURNEY_MAPPER_SCORE):
        return JOURNEY_MAPPER_SCORE, 'journey-mapper scale: P1 critical = 5, P2 = 4, P3 = 2'
    return PRIORITY_SCORE, 'P0 = 5, P1 = 4, P2 = 3, P3 = 2'
SEVERE = {'critical', 'high'}
UI_EXT = {'.tsx', '.jsx', '.vue', '.svelte'}
STDLIB = set(getattr(sys, 'stdlib_module_names', ())) | {'__future__'}
CODE_EXT = {'.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.java', '.kt', '.go', '.cs', '.swift', '.dart', '.rb', '.php',
            '.scala', '.rs', '.vue', '.svelte', '.sql', '.m', '.mm', '.h', '.c', '.cc', '.cpp', '.hpp'}
# Product files that are not code: server-rendered templates, styles, and data records the app loads. They count for
# churn and change in scope, not for complexity, surfaces or test weakness.
TEMPLATE_STYLE_EXT = {'.html', '.htm', '.jinja', '.jinja2', '.j2', '.njk', '.hbs', '.handlebars', '.mustache', '.ejs', '.twig',
                      '.liquid', '.css', '.scss', '.sass', '.less'}
DATA_EXT = {'.json', '.csv', '.tsv', '.yaml', '.yml', '.xml', '.ndjson', '.jsonl', '.geojson'}
ASSET_EXT = TEMPLATE_STYLE_EXT | DATA_EXT
NOT_DATA_NAME = re.compile(r'^(package(-lock)?\.json|composer\.json|[tj]sconfig.*\.json|\.[\w.-]*rc(\.\w+)?|.*\.lock|'
                           r'renovate\.json|vercel\.json|netlify\.toml|app\.json|manifest\.json|.*-lock\.ya?ml|'
                           r'docker-compose.*\.ya?ml|pnpm-workspace\.yaml|codecov\.ya?ml|mkdocs\.ya?ml)$', re.I)
CONFIG_DIRS = {'.github', '.gitlab', '.circleci', '.vscode', '.idea', '.devcontainer', 'config', 'configs', '.config'}
ROOT_APP_PY = re.compile(r'\b(?:Flask|Quart|FastAPI|Starlette|Sanic|Litestar|Bottle)\s*\(|\bfalcon\.(?:App|API)\s*\(|'
                         r'\bweb\.Application\s*\(|\bget_(?:wsgi|asgi)_application\s*\(')
ROOT_APP_JS = re.compile(r'\bexpress\s*\(\s*\)|\bcreateServer\s*\(|\bnew\s+(?:Hono|Koa)\s*\(|\bfastify\s*\(')
LAUNCH_FILES = ('Procfile', 'app.yaml', 'app.yml', 'fly.toml', 'render.yaml', 'Dockerfile', 'gunicorn.conf.py', 'Makefile')
LAUNCH_CMD = re.compile(r'\b(?:gunicorn|uvicorn|hypercorn|waitress-serve|daphne|granian)\b[^\n]*?\b([A-Za-z_][\w.]*):([A-Za-z_]\w*)')


# ---------------------------------------------------------------- shared arithmetic (the validator imports these)
def pct_score(value: float, values: list[float], zero_is_one: bool = True) -> int:
    if zero_is_one and value == 0:
        return 1
    if len(values) <= 1:
        return 3
    below = len([v for v in values if v < value])
    ties = len([v for v in values if v == value]) - 1
    p = (below + ties / 2) / (len(values) - 1)          # mid-rank: tied areas share a percentile
    return 5 if p >= 0.9 else 4 if p >= 0.75 else 3 if p >= 0.5 else 2 if p >= 0.25 else 1


def weighted(factors: dict, weights: dict) -> float | None:
    got = [(factors[k]['score'], w) for k, w in weights.items() if factors.get(k, {}).get('score') is not None]
    if not got:
        return None
    return round(sum(s * w for s, w in got) / sum(w for _, w in got), 1)


MEDIUM_FLOOR = ('novel', 'unconfirmed_findings', 'breaking_change')


def band_of(risk: float | None, flags: list[str]) -> str:
    band = 'Low' if risk is None else 'High' if risk >= 15 else 'Medium' if risk >= 8 else 'Low'
    if 'open_findings' in flags:
        band = 'High'
    if any(x in flags for x in MEDIUM_FLOOR) and band == 'Low':
        band = 'Medium'
    return band


# A source report's own confidence below this is not enough for the High floor: an "assumed" or "possible" critical
# finding (e.g. a migration reviewer that capped a rating because table sizes are unknown) makes the area at least
# Medium, flagged unconfirmed_findings, until someone confirms it.
WEAK_CONFIDENCE = {'assumed', 'possible', 'low', 'unconfirmed', 'suspected', 'speculative', 'unknown'}


def finding_flags(findings: list[dict]) -> list[str]:
    severe = [x for x in findings if x['severity'] in SEVERE]
    confirmed = [x for x in severe if str(x.get('confidence') or '').lower() not in WEAK_CONFIDENCE]
    return (['open_findings'] if confirmed else []) + (['unconfirmed_findings'] if severe and not confirmed else [])


def confidence_of(factors: dict) -> str:
    missing = [k for k, f in factors.items() if f['status'] == 'not_assessed']
    impact_basis = [k for k in IMPACT if factors[k]['status'] == 'assessed']
    if not impact_basis:
        return 'low'
    return 'high' if not missing else 'medium' if len(missing) == 1 else 'low'


def recompute(area: dict) -> dict:
    f = area['factors']
    lik, imp = weighted(f, LIKELIHOOD), weighted(f, IMPACT)
    risk = round(lik * imp, 1) if lik is not None and imp is not None else None
    return {'likelihood': lik, 'impact': imp, 'risk': risk, 'band': band_of(risk, area['flags']), 'confidence': confidence_of(f)}


SCOPE_CAPS = ((25, 2), (100, 3), (250, 4))   # (fewer than N lines changed in the area, highest score)
DATA_ONLY_CAP = 3       # churn and change in scope of an area that holds only data records never score above this


def scope_cap(lines: int) -> int:
    """The highest change_in_scope score a change of this size can get, whatever its percentile: when most areas are
    untouched, an 18-line change would otherwise rank in the top 10% and score 5."""
    return next((cap for limit, cap in SCOPE_CAPS if lines < limit), 5)


def high_floor(budget: int) -> int:
    return max(1, math.ceil(min(2, 0.1 * budget)))


HIGH_SHARE_FLOOR = 0.10     # without a budget, a planned High area gets at least this share (10%, as with a budget)


def floored_shares(planned: list[dict], total: float) -> dict:
    """Shares by risk when there is no budget, with the High floor the session allocation has: each planned High area
    gets at least HIGH_SHARE_FLOOR (or an equal split of everything when the Highs are many), and the other areas
    share the rest by risk. Without it, a High area made High only by an open finding could get 1%."""
    highs = [a for a in planned if a['band'] == 'High']
    floor = min(HIGH_SHARE_FLOOR, 1 / len(planned)) if planned else 0
    raw = {a['id']: (a['risk'] or 0) / total for a in planned}
    out, fixed = dict(raw), set()
    while True:                       # scaling the others down can push another High area under the floor
        more = {a['id'] for a in highs if a['id'] not in fixed and out[a['id']] < floor - 1e-12}
        if not more:
            break
        fixed |= more
        rest_raw = sum(v for k, v in raw.items() if k not in fixed)
        left = 1 - floor * len(fixed)
        out = {k: (floor if k in fixed else (v / rest_raw * left if rest_raw else 0)) for k, v in raw.items()}
    return {k: round(v, 3) for k, v in out.items()}


def allocate(planned: list[dict], budget: int | None, high_in_scope: int | None = None) -> tuple[dict, int, dict]:
    """planned: [{id, risk, band}]. Returns ({id: sessions or None}, sessions_short, {id: share}).

    sessions_short counts every High area in scope (high_in_scope), planned or not: leaving a High
    area out of the plan does not make the budget enough."""
    total = sum(a['risk'] or 0 for a in planned) or 1
    shares = {a['id']: round((a['risk'] or 0) / total, 3) for a in planned}
    if budget is None:
        return {a['id']: None for a in planned}, 0, floored_shares(planned, total)
    floor = high_floor(budget)
    order = sorted(planned, key=lambda a: (-(a['risk'] or 0), a['id']))
    highs = [a for a in order if a['band'] == 'High']
    alloc = {a['id']: 0 for a in planned}
    need = floor * len(highs)
    short = max(0, floor * max(high_in_scope or 0, len(highs)) - budget)
    if need > budget:
        left = budget
        for a in highs:
            give = min(floor, left)
            alloc[a['id']] = give
            left -= give
        return alloc, short, shares
    for a in highs:
        alloc[a['id']] = floor
    rest = budget - need
    raw = {a['id']: rest * (a['risk'] or 0) / total for a in planned}
    base = {k: int(v) for k, v in raw.items()}
    left = rest - sum(base.values())
    for k in sorted(raw, key=lambda k: (-(raw[k] - base[k]), -(next(a['risk'] for a in planned if a['id'] == k) or 0), k))[:left]:
        base[k] += 1
    for k in alloc:
        alloc[k] += base[k]
    return alloc, short, shares


# ---------------------------------------------------------------- areas and attribution
NOT_SOURCE_DIRS = {'e2e', 'cypress', 'playwright', 'fixtures', '__fixtures__', '__mocks__', 'mocks', 'stories', '.storybook',
                   'docs', 'doc', 'documentation', 'examples', 'public', 'static', 'migrations', 'node_modules'}
NEXT_ENTRY = re.compile(r'(?:^|/)(?:page|layout|route|template|loading|error|not-found|default)\.(?:tsx?|jsx?|mjs)$')
NEXT_GUARD = ('middleware', 'proxy')
SPLIT_FLAT = 8          # a source directory with more flat files than this is split by file-name prefix
SPLIT_PACKAGE = 40      # a workspace package with more source files than this is split by subdirectory
# Directories that hold modules rather than being one: src/modules/<x>, src/features/<x>, src/common/<x> ...
CONTAINERS = {'modules', 'features', 'domains', 'common', 'shared', 'packages', 'apps', 'services', 'libs', 'components',
              'pages', 'views', 'screens', 'routes', 'api', 'core'}
# Tool configuration written as code: .prettierrc.js, .eslintrc.cjs, babel.config.js, jest.setup.ts, noxfile.py ...
CONFIG_NAME = re.compile(r'^\.|.*\.(config|setup)\.\w+$|.*rc\.(js|cjs|mjs|ts|cts|mts)$|^(noxfile|fabfile|setup|conftest)\.py$')


# A directory named like test code (e2e/, test/, tests/, spec/) is test code unless it is a product package: it holds a
# package manifest and ships code (a src/ directory, a package.json declaring main, module, exports or bin, ...): a
# workspace package named e2e. A Playwright project's e2e/package.json with only specs stays test code. The rule is
# diff_scope.product_package, shared across the skills.
TEST_DIR_NAMES = {'e2e', 'test', 'tests', 'spec'}
PRODUCT_DIRS: set[str] = set()     # set by score() from the repository's files


def product_dirs(root: Path, files: list[str]) -> set[str]:
    dirs = set()
    for f in files:
        parts = PurePosixPath(f).parts[:-1]
        for i, x in enumerate(parts):
            if x in TEST_DIR_NAMES:
                dirs.add('/'.join(parts[:i + 1]))
    return {d for d in dirs if diff_scope.product_package(root, d)}


# An XCUITest runner app (WebDriverAgent-style: a *UITests target whose "test" starts a server and never asserts) is the
# product's on-device agent, not tests of it. detect_conventions treats every *Tests/*UITests directory as test code;
# a directory of that name whose Swift/Objective-C files hold no XCTest assertion is a runner. Set by score().
RUNNER_DIRS: set[str] = set()
APPLE_EXT = {'.swift', '.m', '.mm'}
C_FAMILY = {'.m', '.mm', '.h', '.c', '.cc', '.cpp', '.hpp'}
# A "test" target that opens a network listener is an on-device server (the runner), whatever it asserts.
XCT_SERVER = re.compile(r'\b(NWListener|GCDWebServer\w*|HTTPServer|FBWebServer|CFSocketCreate\w*|RoutingHTTPServer)\b|'
                        r'\b(listen|bind)\s*\(\s*\w+\s*,')
XCT_ASSERT = re.compile(r'\bXCT(Assert\w*|Fail|Expect\w*|Unwrap|Skip\w*)\b|#expect\s*\(|#require\s*\(|\bwaitForExpectations\b')


def set_runner_dirs(root: Path, files: list[str]) -> None:
    RUNNER_DIRS.clear()
    by_dir: dict[str, list[str]] = {}
    for f in files:
        p = PurePosixPath(f)
        if p.suffix not in APPLE_EXT:
            continue
        inner = [i for i, x in enumerate(p.parts[:-1]) if x.endswith('Tests')]
        if inner:                                       # the innermost *Tests directory holding the file
            by_dir.setdefault('/'.join(p.parts[:inner[-1] + 1]), []).append(f)
    for d, fs in by_dir.items():
        asserts, serves = 0, False
        for f in fs:
            try:
                text = (root / f).read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            asserts += len(XCT_ASSERT.findall(text))
            serves = serves or bool(XCT_SERVER.search(text))
        # No assertion at all, or a listener with fewer assertions than files (a runner XCTFails when its wait ends;
        # a server's own unit tests assert many times per file).
        if not asserts or (serves and asserts < len(fs)):
            RUNNER_DIRS.add(d)


def in_runner_dir(path: str) -> bool:
    return bool(RUNNER_DIRS) and any(path.startswith(d + '/') for d in RUNNER_DIRS)


# Sample and fixture projects (repo_layout.fixture_projects: a nested project with its own manifest, expected output or
# app under examples/, fixtures/, testdata/, samples/ ...): deliberately broken sample code is not the repository's own
# product, so none of its files is area code, a test of an area, an area of its own or runtime configuration. Set by
# score() ({dir: reason}); an explicit --areas entry inside one keeps it in.
FIXTURES: dict[str, str] = {}


def fixture_of(path: str) -> str | None:
    return repo_layout.is_fixture(path, list(FIXTURES)) if FIXTURES else None


def fixture_reason(path: str) -> str:
    d = fixture_of(path)
    return f'sample or fixture project {d}/ ({FIXTURES[d]}); --include-fixtures scores it'


# Files kept identical in several places (repo_layout.copy_groups: a check_shared.py-style mapping, or the same content):
# each group is scored once, in the area of its canonical copy (or, when no area holds the canonical copy, of the first
# copy an area holds). COPY_CANON maps every member to the canonical path, COPY_REP to the member that is scored. Set by
# score(); --keep-copies turns it off.
COPY_CANON: dict[str, str] = {}
COPY_REP: dict[str, str] = {}
COPY_HOW: dict[str, str] = {}


def is_copy(f: str) -> bool:
    """A member of a copy group that is not its canonical copy (before the areas are known)."""
    return COPY_CANON.get(f, f) != f


# Tests that are not a test framework: a script's own self-test (a self_test() function and what only it uses, run by
# `python x.py --self-test`), and Python files a pytest configuration collects through its own python_files
# (bench_*.py, check_*.py). SELF_TESTS: {file: (line ranges [(first index, end index)), test lines, command)}; data
# strings: lines of module-level tables and multi-line constants whose strings are data (a scanner's pattern table, a
# fixture page), where a raw pattern reading inside strings must not match. Set by score().
SELF_TESTS: dict[str, tuple[list[tuple[int, int]], int, str | None]] = {}
PYTEST_TESTS: dict[str, str] = {}
PY_BENCH: dict[str, str] = {}
SELF_TEST_FN = re.compile(r'^_?(?:run_)?self_?tests?$')
SELF_TEST_HINT = re.compile(r'^\s*(?:async\s+)?def\s+_?(?:run_)?self_?tests?\s*\(', re.M)


def python_test_layout(text: str) -> tuple[list[tuple[int, int]], set[int]]:
    """([(first line index, end line index)) of self-test code], {1-based lines whose strings are data}) for Python source.

    Self-test code is every module-level function named self_test (selftest, _self_test, run_self_test ...) and every
    module-level function or constant that only self-test code uses (fixture builders, case tables, sample pages).
    Data strings are the strings of other module-level assignments that sit in a list, tuple, set or dict literal, or
    span several lines: a pattern table or a fixture held as text, which describe code rather than run it."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return [], set()
    defs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    consts: dict[str, ast.AST] = {}
    for n in tree.body:
        targets = n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, ast.AnnAssign) else []
        for t in targets:
            if isinstance(t, ast.Name):
                consts.setdefault(t.id, n)
    named = set(defs) | set(consts)
    refs: dict[str, set[str]] = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = n.name
        else:
            owner = next((k for k, v in consts.items() if v is n), '<module>')
        refs.setdefault(owner, set()).update({x.id for x in ast.walk(n) if isinstance(x, ast.Name) and x.id in named} - {owner})
    members = {d for d in defs if SELF_TEST_FN.match(d)}
    grew = bool(members)
    while grew:
        grew = False
        for x in sorted(named - members):
            users = {o for o, ns in refs.items() if x in ns}
            if users and users <= members:
                members.add(x)
                grew = True
    ranges = []
    for name in sorted(members):
        n = defs.get(name) or consts.get(name)
        start = min([n.lineno] + [d.lineno for d in getattr(n, 'decorator_list', [])])
        ranges.append((start - 1, n.end_lineno))
    data: set[int] = set()

    def mark(node, in_coll: bool) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if in_coll or node.end_lineno > node.lineno:
                data.update(range(node.lineno, node.end_lineno + 1))
            return
        coll = in_coll or isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict))
        for c in ast.iter_child_nodes(node):
            mark(c, coll)
    for n in tree.body:
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None and not any(
                consts.get(m) is n for m in members):
            mark(n.value, False)
    for n in ast.walk(tree):            # docstrings (any string statement) describe code; they never run it
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
            data.update(range(n.lineno, n.end_lineno + 1))
    return sorted(ranges), data


def set_test_layout(root: Path, files: list[str]) -> None:
    """Fill SELF_TESTS (Python files with self-test code, with the command detect_conventions.find_self_tests gives),
    PYTEST_TESTS and PY_BENCH (Python files a pytest configuration collects through python_files that its default
    naming would not: bench_*.py and the like are benchmarks, other names tests)."""
    for x in (SELF_TESTS, PYTEST_TESTS, PY_BENCH):
        x.clear()
    py = [f for f in files if f.endswith('.py') and not fixture_of(f)]
    commands = {s['file']: s['command'] for s in detect_conventions.find_self_tests(root, py)}
    for f in py:
        try:
            text = (root / f).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if not SELF_TEST_HINT.search(text):
            continue
        ranges, _ = python_test_layout(text)
        if ranges:
            lines = text.splitlines()
            n = sum(1 for a, b in ranges for x in lines[a:b] if x.strip())
            SELF_TESTS[f] = (ranges, n, commands.get(f))
    fileset = set(files)
    cfg_dirs = sorted({str(PurePosixPath(f).parent) for f in files
                       if PurePosixPath(f).name in detect_conventions.PYTEST_CONFIG_NAMES and not fixture_of(f)})
    for d in cfg_dirs:
        cfg = detect_conventions.pytest_collection(root, d)
        if not cfg:
            continue
        base = '' if d == '.' else d + '/'
        for f in sorted(fileset):
            if not f.startswith(base) or not f.endswith('.py') or fixture_of(f) or f in PYTEST_TESTS or f in PY_BENCH:
                continue
            rel, name = f[len(base):], PurePosixPath(f).name
            if cfg['testpaths'] and '.' not in cfg['testpaths'] and not any(
                    rel == t or rel.startswith(t.rstrip('/') + '/') for t in cfg['testpaths']):
                continue
            if not any(fnmatch.fnmatch(name, p) for p in cfg['python_files']) or detect_conventions.is_test_file(f) \
                    or name == 'conftest.py':
                continue
            if name.startswith('bench') or {'bench', 'benches', 'benchmark', 'benchmarks'} & set(PurePosixPath(f).parts[:-1]):
                PY_BENCH[f] = f'{cfg["config"]} (python_files {" ".join(cfg["python_files"])})'
            else:
                PYTEST_TESTS[f] = f'{cfg["config"]} (python_files {" ".join(cfg["python_files"])})'


# Test weakness from a coverage profile (--coverage): statement coverage per area. A Go coverprofile
# (`go test -coverprofile`, "mode: set|count|atomic" then "<import path>/<file>.go:a.b,c.d <statements> <count>") is
# mapped to repository files through the module path in go.mod (nested go.mod modules too).
COVER_LINE = re.compile(r'^(?P<file>.+?\.go):\d+\.\d+,\d+\.\d+ (?P<n>\d+) (?P<count>\d+)$')


def go_modules(root: Path) -> list[tuple[str, str]]:
    """[(module path, directory relative to root)], longest module path first."""
    mods = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ('.git', 'vendor', 'node_modules', 'testdata', 'third_party')]
        if 'go.mod' in filenames:
            try:
                text = (Path(dirpath) / 'go.mod').read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            m = re.search(r'^\s*module\s+"?([^\s"]+)"?', text, re.M)
            if m:
                rel = Path(dirpath).relative_to(root).as_posix()
                mods.append((m.group(1), '' if rel == '.' else rel))
    return sorted(mods, key=lambda x: -len(x[0]))


def read_coverage(root: Path, path: Path) -> dict[str, tuple[int, int]]:
    """{repository file: (statements, statements covered)} from a Go coverprofile. Blocks repeated by several test
    binaries (one profile per package, concatenated) count once, covered if any run covered them."""
    mods = go_modules(root)
    blocks: dict[tuple[str, str], tuple[int, bool]] = {}
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.strip()
        m = COVER_LINE.match(line)
        if not m:
            continue
        imp, n, hit = m.group('file'), int(m.group('n')), int(m.group('count')) > 0
        rel = None
        for mod, d in mods:
            if imp.startswith(mod + '/'):
                rel = (d + '/' if d else '') + imp[len(mod) + 1:]
                break
        if rel is None:
            continue
        key = (rel, line.rsplit(' ', 2)[0])
        old = blocks.get(key)
        blocks[key] = (n, hit or bool(old and old[1]))
    out: dict[str, list[int]] = {}
    for (rel, _), (n, hit) in blocks.items():
        acc = out.setdefault(rel, [0, 0])
        acc[0] += n
        acc[1] += n if hit else 0
    return {k: (v[0], v[1]) for k, v in sorted(out.items())}


def coverage_score(frac: float) -> int:
    """Test weakness from statement coverage: 80%+ is 1, 60%+ 2, 40%+ 3, 20%+ 4, below 20% 5 (RUBRIC.md)."""
    return 1 if frac >= 0.8 else 2 if frac >= 0.6 else 3 if frac >= 0.4 else 4 if frac >= 0.2 else 5


def dir_names(path: str) -> set[str]:
    """The directory names on a path, leaving out a test-named directory that is a product package (PRODUCT_DIRS)."""
    parts = PurePosixPath(path).parts[:-1]
    return {x for i, x in enumerate(parts) if not (x in TEST_DIR_NAMES and '/'.join(parts[:i + 1]) in PRODUCT_DIRS)}


def is_source(f: str) -> bool:
    p = PurePosixPath(f)
    return (not fixture_of(f) and p.suffix in CODE_EXT and not is_test(f) and not f.endswith('.d.ts') and not dir_names(f) & NOT_SOURCE_DIRS
            and not CONFIG_NAME.match(p.name) and not is_bench(f))


DOC_DIRS = {'docs', 'doc', 'documentation', 'examples'}


def is_area_code(f: str) -> bool:
    """A file that counts in its area's scores: code that is not a test, tool configuration or documentation.
    Migrations, static and generated-looking directories still count here (a migration that drops a column is a
    destructive surface); is_source() is the stricter test for choosing areas. Runtime configuration (KEPT_CONFIG: a
    next.config.* or platform file setting headers, CSP, redirects or rewrites, or a configuration file an explicit
    --areas entry names by its path) is product behaviour and counts."""
    if fixture_of(f):
        return False
    if f in KEPT_CONFIG:
        return True
    p = PurePosixPath(f)
    return (p.suffix in CODE_EXT and not is_test(f) and not CONFIG_NAME.match(p.name)
            and not dir_names(f) & DOC_DIRS and not is_bench(f))


# Configuration that is product behaviour, not tooling: set by score() from the Next.js model (next.config.* with
# headers, CSP, redirects or rewrites; vercel.json, netlify.toml or public/_headers that set them) and from explicit
# --areas entries that name a configuration file by its path.
KEPT_CONFIG: set[str] = set()
# CI workflow files an explicit --areas entry covers (.github/workflows/*.yml ...): scored like templates and data (churn,
# defects, change in scope), with complexity, surface and test weakness not_assessed. Set by score().
CI_ASSETS: set[str] = set()
RUNTIME_CONFIG_REASON = ('runtime configuration (response headers, CSP, redirects or rewrites): product behaviour, '
                         'not tool configuration; add it to --areas to score it')
SW_REGISTER = re.compile(r'''\bserviceWorker\s*\.\s*register\s*\(\s*['"`]([^'"`]+)['"`]''')
NEXT_MODEL: dict = {}


def service_workers(root: Path, files: list[str]) -> dict[str, str]:
    """{worker file: 'registered at file:line'}: the scripts navigator.serviceWorker.register('/x.js') names, found in
    public/, static/, the Next.js public/ beside a nested project, or the root. A worker in public/ is product code that
    intercepts the site's requests, not a static asset. Workers the Next.js model lists unregistered are included."""
    fileset = set(files)
    out: dict[str, str] = {}
    for f in files:
        if PurePosixPath(f).suffix not in CODE_EXT or is_test(f) or 'node_modules' in f.split('/') or fixture_of(f):
            continue
        try:
            text = (root / f).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if 'serviceWorker' not in text:
            continue
        for m in SW_REGISTER.finditer(text):
            url = m.group(1).split('?')[0].lstrip('./').lstrip('/')
            top = f.split('/')[0] if '/' in f else ''
            for cand in (f'public/{url}', f'static/{url}', url, f'{top}/public/{url}' if top else None):
                if cand and cand in fileset:
                    out.setdefault(cand, f'registered at {f}:{text.count(chr(10), 0, m.start()) + 1}')
                    break
    for p in NEXT_MODEL.get('projects', []):
        for w in p.get('service_workers') or []:
            if w.get('exists') and w.get('worker_file') in fileset and not fixture_of(w['worker_file']):
                out.setdefault(w['worker_file'], 'a service worker in public/ (not registered by code found)')
    return out


def next_model(root: Path, files: list[str]) -> dict:
    """The shared static Next.js model (next_app.read_next_app) when the repository holds a Next.js project, else {}."""
    try:
        if not next_app.find_next_roots(root, files):
            return {}
        return next_app.read_next_app(root)
    except Exception as e:                 # a reader failure must not stop the scoring; it is named in a warning
        return {'error': f'{type(e).__name__}: {e}', 'projects': []}


def runtime_config_files(model: dict, fileset: set) -> dict[str, str]:
    """{file: what it sets}: next.config.* with headers, CSP, redirects or rewrites, and platform files (vercel.json,
    netlify.toml, public/_headers) that set them."""
    out: dict[str, str] = {}
    for p in model.get('projects', []):
        c = p.get('config') or {}
        what = [k for k in ('headers', 'csp') if c.get(k)] + [k for k in ('redirects', 'rewrites') if c.get(k)]
        if c.get('file') in fileset and what:
            out[c['file']] = ', '.join(what)
        for name, pf in (p.get('platform') or {}).items():
            what = [k for k in ('headers', 'csp', 'redirects', 'rewrites') if pf.get(k)]
            if pf.get('file') in fileset and what:
                out[pf['file']] = ', '.join(what)
    return out


JS_SCRIPT_TYPES = {'', 'text/javascript', 'application/javascript', 'module', 'text/partytown'}


def next_surface_hits(model: dict) -> dict[str, list[tuple[str, str]]]:
    """{file: [(surface class, evidence)]} from the Next.js model: third-party script loaders (next/script, <script>,
    next/third-parties) for third_party_script, whatever their src is written as (a constant, a template); and the
    headers, CSP, redirects and rewrites of next.config.* and the platform files for response_policy."""
    hits: dict[str, list[tuple[str, str]]] = {}
    for p in model.get('projects', []):
        for s in p.get('scripts') or []:
            if s.get('kind') not in ('next/script', 'script', 'next/third-parties') or s.get('first_party'):
                continue
            if (s.get('type') or '').strip().lower() not in JS_SCRIPT_TYPES:    # JSON-LD and other data blocks run nothing
                continue
            hosts = [s['host']] if s.get('host') else list(s.get('body_hosts') or [])
            if s.get('kind') == 'next/third-parties' or hosts:
                hits.setdefault(s['file'], []).append(
                    ('third_party_script', f'{s["file"]}:{s["line"]} ({s["kind"]} {", ".join(hosts) or s.get("component") or ""})'.rstrip()))
        c = p.get('config') or {}
        if c.get('file'):
            for h in c.get('headers') or []:
                keys = ', '.join(x['key'] for x in h.get('headers') or [] if x.get('key'))
                hits.setdefault(c['file'], []).append(('response_policy', f'{c["file"]}:{h["line"]} (headers for {h.get("source")}: {keys})'))
            for k in ('redirects', 'rewrites'):
                if c.get(k):
                    hits.setdefault(c['file'], []).append(
                        ('response_policy', f'{c["file"]}:{c[k]["line"]} ({len(c[k].get("entries") or [])} {k})'))
        for name, pf in (p.get('platform') or {}).items():
            what = [f'{len(pf[k])} {k}' for k in ('headers', 'redirects', 'rewrites') if pf.get(k)]
            if pf.get('file') and what:
                hits.setdefault(pf['file'], []).append(('response_policy', f'{pf["file"]} ({", ".join(what)})'))
    return hits


def is_area_asset(f: str) -> bool:
    """A template, style or data record that belongs to its area's product (not a test, docs, tool configuration or a
    manifest). Data counts only inside a directory: a JSON or YAML file at the root is repository configuration.
    Product files in a language or format no script here reads (UNREAD_EXT: PowerShell and shell scripts, Slint and
    other UI markup, Lua ...) count the same way: churn, defects and change in scope, never complexity or surfaces."""
    if f in CI_ASSETS:
        return True
    if fixture_of(f):
        return False
    p = PurePosixPath(f)
    sfx = p.suffix.lower()
    if sfx in UNREAD_EXT and sfx not in ASSET_EXT:      # a script, UI definition or language no script here reads
        return not is_test(f) and not dir_names(f) & DOC_DIRS and 'node_modules' not in p.parts
    if sfx not in ASSET_EXT or is_test(f) or dir_names(f) & (DOC_DIRS | CONFIG_DIRS) or 'node_modules' in p.parts:
        return False
    if sfx in DATA_EXT:
        return '/' in f and not NOT_DATA_NAME.match(p.name) and not CONFIG_NAME.match(p.name)
    return True


def root_app_modules(root: Path, files: list[str]) -> dict[str, str]:
    """{root-level module: why it is the deployed app}: a module at the repository root that creates a web app
    (Flask(__name__), FastAPI(), express() ...), or that a launch command (gunicorn app:app in a Procfile, app.yaml,
    fly.toml or Dockerfile) names. Such a file is product code, not root tooling."""
    out: dict[str, str] = {}
    fileset = set(files)
    for f in files:
        if '/' in f or PurePosixPath(f).name not in LAUNCH_FILES:
            continue
        try:
            text = (root / f).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        for m in LAUNCH_CMD.finditer(text):
            mod = m.group(1).replace('.', '/') + '.py'
            if mod in fileset:
                out.setdefault(mod, f'{f} runs it ({m.group(0).strip()[:60]})')
    for f in files:
        sfx = PurePosixPath(f).suffix
        if '/' in f or f in out or sfx not in ('.py', '.js', '.mjs', '.cjs', '.ts') or is_test(f):
            continue
        try:
            text = (root / f).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        rx = ROOT_APP_PY if sfx == '.py' else ROOT_APP_JS
        m = rx.search(code_view(text, sfx))
        if m:
            out[f] = f'creates the web app ({m.group(0).strip()} at line {text.count(chr(10), 0, m.start()) + 1})'
    return out


def prefix_token(stem: str) -> str:
    return re.split(r'[-_.]|(?<=[a-z0-9])(?=[A-Z])', stem)[0].lower() or stem.lower()


def split_dir(d: str, source: list[str], label: str, by_prefix: bool = True) -> list[dict]:
    """Areas for one source directory: a subdirectory each; flat files grouped by file-name prefix (stripe.ts and
    stripe-webhooks.ts together) when there are many and by_prefix (the Next.js lib/ layout), the rest as
    '<label> (top level)'. Outside Next.js a prefix ('read*', 'cdp*') is not a module and gives two-file areas."""
    inside = [f for f in source if f.startswith(d + '/')]
    subs = sorted({f[len(d) + 1:].split('/')[0] for f in inside if f[len(d) + 1:].count('/') >= 1})
    flat = sorted(f for f in inside if f[len(d) + 1:].count('/') == 0 and PurePosixPath(f).name != '__init__.py')
    if not subs and len(flat) <= SPLIT_FLAT:
        return [{'id': label, 'paths': [d]}] if flat else []
    areas = [{'id': f'{label}/{s}', 'paths': [f'{d}/{s}']} for s in subs]
    if by_prefix and len(flat) > SPLIT_FLAT:
        groups: dict[str, list[str]] = {}
        for f in flat:
            groups.setdefault(prefix_token(PurePosixPath(f).stem), []).append(f)
        for tok, fs in sorted(groups.items()):
            if tok in subs:                   # lib/auth-options.ts goes with lib/auth/
                next(a for a in areas if a['id'] == f'{label}/{tok}')['paths'] += fs
            elif len(fs) >= 2:
                areas.append({'id': f'{label}/{tok}*', 'paths': fs})
    if flat:
        areas.append({'id': f'{label} (top level)', 'paths': [f'{d}/*']})
    return areas


NEXT_ROOT_ROLES = (('app (layout)', {'layout', 'template'}), ('pages/(root)', {'page'}),
                   ('app (metadata routes)', {'robots', 'sitemap', 'manifest', 'icon', 'apple-icon', 'opengraph-image',
                                              'twitter-image'}),
                   ('app (error and loading pages)', {'not-found', 'error', 'global-error', 'loading', 'default',
                                                      'forbidden', 'unauthorized'}))


def next_app_areas(root: Path, files: list[str], source: list[str]) -> list[dict] | None:
    """Areas for a Next.js App Router project, where the directory tree under app/ is routing, not modules: one area per
    API resource (app/api/<resource>), one per top-level route segment or route group; the files directly in app/ split
    by role: app (layout) for the root layout and template (they wrap every page and load what every visitor gets),
    pages/(root) for the home page, app (metadata routes) for robots, sitemap, manifest, icons and social images,
    app (error and loading pages) for not-found, error, global-error and loading, app (root) for the rest; then lib/,
    components/ and the other source directories split by subdirectory (and by file-name prefix when flat and large);
    middleware.ts / proxy.ts as their own area."""
    pkg = root / 'package.json'
    try:
        deps = json.loads(pkg.read_text(encoding='utf-8')) if pkg.is_file() else {}
    except ValueError:
        deps = {}
    if 'next' not in {**(deps.get('dependencies') or {}), **(deps.get('devDependencies') or {})}:
        return None
    app = next((d for d in ('src/app', 'app') if any(f.startswith(d + '/') and NEXT_ENTRY.search(f) for f in source)), None)
    if not app:
        return None
    base = app[:-len('app')].rstrip('/')
    areas: list[dict] = []
    in_app = [f[len(app) + 1:] for f in source if f.startswith(app + '/')]
    segs = sorted({r.split('/')[0] for r in in_app if '/' in r})
    for seg in segs:
        if seg == 'api':
            res = sorted({r.split('/')[1] for r in in_app if r.startswith('api/') and r.count('/') >= 2})
            areas += [{'id': f'api/{x}', 'paths': [f'{app}/api/{x}']} for x in res]
            if any(r.startswith('api/') and r.count('/') == 1 for r in in_app):
                areas.append({'id': 'api (top level)', 'paths': [f'{app}/api/*']})
        else:
            areas.append({'id': f'pages/{seg}', 'paths': [f'{app}/{seg}']})
    roles: dict[str, list[str]] = {}
    for r in sorted(r for r in in_app if '/' not in r):
        stem = PurePosixPath(r).stem
        role = next((rid for rid, stems in NEXT_ROOT_ROLES if stem in stems), None)
        if role:
            roles.setdefault(role, []).append(f'{app}/{r}')
    for rid, _ in NEXT_ROOT_ROLES:
        if roles.get(rid):
            areas.append({'id': rid, 'paths': roles[rid]})
    if any('/' not in r and not any(f'{app}/{r}' in v for v in roles.values()) for r in in_app):
        areas.append({'id': 'app (root)', 'paths': [f'{app}/*']})
    prefix = base + '/' if base else ''
    guards = [f for f in source if f.startswith(prefix) and f[len(prefix):].count('/') == 0
              and PurePosixPath(f).stem in NEXT_GUARD]
    if guards:
        areas.append({'id': 'middleware', 'paths': guards})
    tops = sorted({f[len(prefix):].split('/')[0] for f in source if f.startswith(prefix) and f[len(prefix):].count('/') >= 1}
                  - {'app'})
    if not base:     # app/ at the repository root: only the usual source directories, not scripts or tooling
        tops = [t for t in tops if t in {'lib', 'components', 'hooks', 'utils', 'server', 'services', 'features', 'domains',
                                         'modules', 'store', 'stores', 'context', 'contexts', 'actions', 'types', 'config'}]
    for t in tops:
        areas += split_dir(prefix + t, source, t)
    if base and any(f.startswith(prefix) and f[len(prefix):].count('/') == 0 and f not in guards for f in source):
        areas.append({'id': f'{base} (top level)', 'paths': [f'{base}/*']})
    return areas


def descend(d: str, source: list[str]) -> str:
    """Go down from d while one subdirectory holds nearly all its source files and d has none of its own (app/ ->
    app/src, packages/x -> packages/x/src/x): the modules are below that."""
    while True:
        inside = [f for f in source if f.startswith(d + '/')]
        if not inside or any(f[len(d) + 1:].count('/') == 0 and PurePosixPath(f).name != '__init__.py' for f in inside):
            return d
        counts: dict[str, int] = {}
        for f in inside:
            s = f[len(d) + 1:].split('/')[0]
            counts[s] = counts.get(s, 0) + 1
        top = max(sorted(counts), key=lambda s: counts[s])
        if counts[top] < 0.8 * len(inside):
            return d
        d = f'{d}/{top}'


def module_areas(d: str, source: list[str], label: str) -> list[dict]:
    """split_dir, then each container subdirectory (modules/, features/, common/ ...) with two or more subdirectories
    of its own is split again: a Vite or React app's src/modules/<module> are the modules, not src/modules."""
    out = []
    for a in split_dir(d, source, label, by_prefix=False):
        p = a['paths'][0]
        name = PurePosixPath(p).name
        if len(a['paths']) == 1 and not p.endswith('*') and name in CONTAINERS:
            inner = split_dir(p, source, a['id'], by_prefix=False)
            if len([x for x in inner if not x['id'].endswith('(top level)')]) >= 2:
                out += inner
                continue
        out.append(a)
    return out


def define_areas(root: Path, files: list[str], explicit: Path | None) -> tuple[list[dict], str]:
    if explicit:
        return json.loads(explicit.read_text(encoding='utf-8'))['areas'], 'explicit (--areas)'
    areas, basis = default_areas(root, files)
    # a second language that is a large share of the code and sits outside every area (Python scripts beside a Cargo
    # workspace) is product or release tooling, not incidental: it gets areas by directory, never "root tooling"
    # a root module that creates (or is launched as) the web app is product code: its own area, with the templates/ and
    # static/ directories beside it, never root tooling
    apps = {f: why for f, why in root_app_modules(root, files).items() if area_of(f, areas) is None}
    for f, why in sorted(apps.items()):
        extra = [d for d in ('templates', 'static') if any(x.startswith(d + '/') for x in files)
                 and area_of(next(x for x in files if x.startswith(d + '/')), areas) is None]
        areas.append({'id': f, 'paths': [f] + extra})
    if apps:
        basis += '; root app module(s): ' + '; '.join(f'{f} ({why})' for f, why in sorted(apps.items()))
    # a service worker in public/ or static/ intercepts the site's requests: product code, its own area
    workers = {f: why for f, why in service_workers(root, files).items() if area_of(f, areas) is None}
    if workers:
        areas.append({'id': 'service worker', 'paths': sorted(workers)})
        basis += '; service worker(s): ' + '; '.join(f'{f} ({why})' for f, why in sorted(workers.items()))
    # next.config.* and platform files that set headers, CSP, redirects or rewrites: what every response gets
    cfg = sorted(f for f in KEPT_CONFIG if area_of(f, areas) is None)
    if cfg:
        areas.append({'id': 'runtime config', 'paths': cfg})
        basis += '; runtime configuration: ' + ', '.join(cfg)
    extra, note = second_language_areas(root, files, areas)
    if extra:
        areas += extra
        basis += '; ' + note
    return areas, basis


SECOND_LANGUAGE_SHARE = 0.30     # a language with this share of the code's lines is never dropped as tooling
SECOND_LANGUAGE_MIN_LINES = 50


def code_lines_by_language(root: Path, files: list[str], owner) -> dict[str, list]:
    """{language: [lines outside every area, lines inside, [files outside]]} over the code that counts as source
    (tests, docs, config, generated and vendored code left out); lines are non-blank lines, owner(f) is f's area."""
    out: dict[str, list] = {}
    for f in files:
        lang = lang_census.LANG_BY_EXT.get(PurePosixPath(f).suffix)
        if not lang or not is_source(f) or is_copy(f) or diff_scope.classify(f, '', False, root)[0] in ('generated', 'vendored'):
            continue
        try:
            n = sum(1 for x in (root / f).read_text(encoding='utf-8', errors='replace').splitlines() if x.strip())
        except OSError:
            continue
        n -= RUST_INLINE_TESTS.get(f, ([], 0))[1]          # #[cfg(test)] modules are test lines, not code
        acc = out.setdefault(lang, [0, 0, []])
        if owner(f) is None:
            acc[0] += n
            acc[2].append(f)
        else:
            acc[1] += n
    return out


def second_language_areas(root: Path, files: list[str], areas: list[dict]) -> tuple[list[dict], str]:
    """Areas for a language that holds SECOND_LANGUAGE_SHARE or more of the code's lines and is mostly outside the
    default areas (Python helper scripts beside a Cargo workspace): one area per top-level directory holding it (split
    by subdirectory like a main source tree when large), and one for its files at the repository root."""
    by = code_lines_by_language(root, files, lambda f: area_of(f, areas))
    total = sum(v[0] + v[1] for v in by.values())
    new, notes = [], []
    for lang, (out_n, in_n, fs) in sorted(by.items()):
        if not total or out_n < SECOND_LANGUAGE_MIN_LINES or out_n < SECOND_LANGUAGE_SHARE * total or in_n >= out_n:
            continue
        groups: dict[str, list[str]] = {}
        for f in fs:
            groups.setdefault(f.split('/')[0] if '/' in f else '', []).append(f)
        made = []
        for top, gfs in sorted(groups.items()):
            if not top:
                made.append({'id': f'{lang} files at the root', 'paths': sorted(gfs)})
                continue
            base = descend(top, gfs)
            parts = module_areas(base, gfs, base) if len(gfs) > SPLIT_PACKAGE else []
            # unsplit, the area is the top-level directory, as a workspace package is: tool/ beside tool2/, never
            # tool/scripts beside tool2/ (whatever else the directory holds is excluded with its own reason)
            made += parts if len(parts) >= 2 else [{'id': top, 'paths': [top]}]
        new += made
        notes.append(f'{lang} outside the packages ({out_n} of {total} code lines, {len(fs)} file(s)): '
                     + ', '.join(a['id'] for a in made))
    return new, 'second language: ' + '; '.join(notes) if notes else ''


def default_areas(root: Path, files: list[str]) -> tuple[list[dict], str]:
    det = detect_conventions.Detector(root, 50_000).run(None)
    # generated and vendored files are excluded from scoring, so they never choose the main source directory
    # copies of a shared file never choose an area: they are scored once, with their canonical copy
    all_source = [f for f in files if is_area_code(f) and not is_copy(f)
                  and diff_scope.classify(f, '', False, root)[0] not in ('generated', 'vendored')]
    pkgs = sorted({a['root'] for a in det['areas'] if a['root'] != '.' and not fixture_of(a['root'] + '/')})
    pkgs = [p for p in pkgs if any(f.startswith(p + '/') for f in all_source)]   # not an empty or unchecked-out package
    in_pkg = lambda f: any(f.startswith(p + '/') for p in pkgs)  # noqa: E731

    def workspace(p: str) -> tuple[list[dict], bool]:
        mine = [f for f in all_source if f.startswith(p + '/') and not any(
            q != p and q.startswith(p + '/') and f.startswith(q + '/') for q in pkgs)]
        if (root / p / 'go.mod').is_file() and any(f.endswith('.go') for f in mine):
            return go_package_areas(p, mine), True       # a nested Go module: one area per Go package
        base = descend(p, mine)
        parts = module_areas(base, mine, p) if len(mine) > SPLIT_PACKAGE else []
        return (parts, True) if len(parts) >= 2 else ([{'id': p, 'paths': [p]}], False)

    # A root manifest is a package too. A root go.mod makes the module's Go packages the areas, whatever nested
    # package.json directories (an npm wrapper, a bundled driver) the repository also carries; otherwise, when the
    # code outside the nested packages is most of the source and the root has a product manifest, that code is scored
    # by the main-source rule beside the nested packages instead of being called root tooling.
    root_go = [f for f in all_source if f.endswith('.go') and not in_pkg(f)] if (root / 'go.mod').is_file() else []
    outside = [f for f in all_source if not in_pkg(f)]
    # a virtual Cargo workspace (a root Cargo.toml with no [package]) lists crates; it is not a root package
    virtual = bool((CARGO.get('workspace') or {}).get('virtual'))
    root_manifest = next((m for m in ROOT_MANIFESTS if (root / m).is_file() and not (m == 'Cargo.toml' and virtual)), None)
    if root_go or (pkgs and root_manifest and len(outside) > len(all_source) / 2):
        if root_go:
            areas, basis = go_package_areas('', root_go), 'Go packages of the root module (go.mod)'
            others = other_code_areas([f for f in outside if not f.endswith('.go')], areas)
            if others:
                areas += others
                basis += ', and the other code under the module by directory'
        else:
            areas = main_source_areas([f for f in outside if '/' in f])
            basis = (f'the root package ({root_manifest}): subdirectories of '
                     + (areas[0].pop('_main_root') + '/' if areas else 'nothing'))
        split = []
        for p in pkgs:
            got, was_split = workspace(p)
            areas += got
            if was_split:
                split.append(p)
                if p not in {a['id'] for a in got}:
                    # holds the package's own tests and loose files, so they do not fall to a Go package above it
                    areas.append({'id': p, 'paths': [p]})
        if pkgs:
            basis += f'; nested packages: {", ".join(pkgs)}'
        if split:
            basis += f' (split: {", ".join(split)})'
        return areas, basis
    if len(pkgs) > 1:
        areas, split = [], []
        for p in pkgs:
            got, was_split = workspace(p)
            areas += got
            if was_split:
                split.append(p)
        basis = 'workspace packages'
        if split:
            basis += f' (split by subdirectory where larger than {SPLIT_PACKAGE} source files: {", ".join(split)})'
        return areas, basis
    nxt = next_app_areas(root, files, [f for f in files if is_source(f)])
    if nxt:
        return nxt, 'Next.js App Router (route segments, API resources, source modules)'
    areas = main_source_areas([f for f in all_source if '/' in f])
    if not areas:
        return [], 'none'
    return areas, f'subdirectories of the main source directory {areas[0].pop("_main_root")}/'


ROOT_MANIFESTS = ('go.mod', 'Cargo.toml', 'pyproject.toml', 'setup.py', 'pom.xml', 'build.gradle', 'build.gradle.kts')


def main_source_areas(source: list[str]) -> list[dict]:
    """The immediate subdirectories of the main source directory (the top-level directory holding the most source
    files, followed down while one subdirectory holds nearly all of it), containers split again."""
    tops: dict[str, int] = {}
    for f in source:
        tops[f.split('/')[0]] = tops.get(f.split('/')[0], 0) + 1
    if not tops:
        return []
    main_root = descend(max(sorted(tops), key=lambda t: tops[t]), source)
    areas = module_areas(main_root, source, '')
    for a in areas:     # ids relative to the main source directory, as before: 'payments', 'modules/orders'
        a['id'] = a['id'].lstrip('/') or main_root
        if a['id'] == ' (top level)':
            a['id'] = f'{main_root} (top level)'
    if areas:
        areas[0]['_main_root'] = main_root
    return areas


def other_code_areas(code: list[str], go_areas: list[dict]) -> list[dict]:
    """Areas for code under a Go module that is not Go (a Swift runner app, a TypeScript helper beside the Go packages):
    each file goes with the highest directory below its nearest Go package directory (or the root), and each such
    directory is split by subdirectory like a main source tree. Code in a Go package's own directory stays with it."""
    pkg_dirs = {p for a in go_areas for p in a['paths'] if not p.endswith('.go')} | \
               {str(PurePosixPath(p).parent) for a in go_areas for p in a['paths'] if p.endswith('.go')}
    groups: dict[str, list[str]] = {}
    for f in code:
        parts = PurePosixPath(f).parts[:-1]
        if not parts or '/'.join(parts) in pkg_dirs:
            continue
        cut = max((i + 1 for i in range(len(parts)) if '/'.join(parts[:i + 1]) in pkg_dirs), default=0)
        if cut >= len(parts):
            continue
        groups.setdefault('/'.join(parts[:cut + 1]), []).append(f)
    areas = []
    for top, fs in sorted(groups.items()):
        base = descend(top, fs)
        parts = module_areas(base, fs, base) if base != top or len(fs) > SPLIT_FLAT else []
        areas += parts if len(parts) >= 2 else [{'id': base, 'paths': [base]}]
    return areas


def go_package_areas(base: str, go_files: list[str]) -> list[dict]:
    """One area per Go package below a module directory (base, '' for the repository root): each directory holding
    non-test .go files, id = its path (the module directory itself: the files at its top, listed). A directory without
    Go files of its own (testdata, assets) goes with the nearest package above it."""
    dirs: dict[str, list[str]] = {}
    for f in go_files:
        if f.endswith('_test.go') or 'testdata' in PurePosixPath(f).parts:
            continue
        dirs.setdefault(str(PurePosixPath(f).parent), []).append(f)
    areas = []
    for d, fs in sorted(dirs.items()):
        if d in ('.', base) or (not base and d == '.'):
            top = sorted(x for x in go_files if str(PurePosixPath(x).parent) == d)   # main.go and its _test.go files
            areas.append({'id': f'{base} (root package)' if base else '(root package)', 'paths': top})
        else:
            areas.append({'id': d, 'paths': [d]})
    return areas


SURFACE_ONLY_WARNING = ('impact rests on the surface factor alone for every area (journey criticality and reach are not '
                        'assessed): the ranking is by surface patterns, so the headline must say so; for a library or CLI, '
                        'give --usage (downloads or calls per package or entry point) or a journey map; for a desktop or '
                        'command-line app with neither, judge impact per area by hand (what a user loses when it fails: '
                        'their data, a running session, a result they publish) as a judged surface factor with a reason')
OUTSIDE_REASON ='outside the workspace packages and the main source tree (root tooling and scripts); add it to --areas to score it'


def is_test(path: str) -> bool:
    if in_runner_dir(path):                  # an XCUITest runner app is the product's on-device agent
        return False
    if path in RUST_TEST_FILES or path in PYTEST_TESTS:   # a Rust test module; a file a pytest config collects
        return True
    if PurePosixPath(path).name in detect_conventions.SELF_TEST_NAMES:   # self_test.py, run as a script
        return True
    p = PurePosixPath(path)
    if p.suffix in C_FAMILY and any(x.endswith('Tests') for x in p.parts[:-1]):   # an XCTest bundle's Objective-C
        return True
    return detect_conventions.is_test_file(path) or bool(dir_names(path) & {'tests', 'test', '__tests__', 'spec'})


def area_of(path: str, areas: list[dict]) -> str | None:
    best = None
    for a in areas:
        for p in a['paths']:
            if p.endswith('/*'):
                ok = path.startswith(p[:-1]) and '/' not in path[len(p) - 1:]
            else:
                ok = path == p or path.startswith(p.rstrip('/') + '/')
            if ok and (best is None or len(p) > best[1]):
                best = (a['id'], len(p))
    return best[0] if best else None


def js_aliases(root: Path, files: list[str]) -> list[tuple[str, str, str]]:
    """Import aliases as (config directory, alias prefix, target directory), relative to the root: tsconfig or
    jsconfig compilerOptions.paths, and resolve.alias in vite, vitest or webpack configs
    ('~': path.resolve(__dirname, 'src'), {find: '~', replacement: ...}, fileURLToPath(new URL('./src', ...)))."""
    found = []
    call = r'''(?:(?:path\.)?(?:resolve|join)\(\s*(?:__dirname|process\.cwd\(\))\s*,\s*|fileURLToPath\(\s*new URL\(\s*)['"]([^'"]+)['"]'''
    for f in files:
        name, base = PurePosixPath(f).name, PurePosixPath(f).parent
        if name in ('tsconfig.json', 'jsconfig.json'):
            text = re.sub(r'^\s*//.*$', '', (root / f).read_text(encoding='utf-8', errors='replace'), flags=re.M)
            try:
                opts = json.loads(re.sub(r',(\s*[}\]])', r'\1', text)).get('compilerOptions') or {}
            except ValueError:
                continue
            burl = base / (opts.get('baseUrl') or '.')
            for alias, targets in (opts.get('paths') or {}).items():
                if targets:
                    found.append((base, alias.rstrip('*').rstrip('/'), burl / targets[0].rstrip('*').rstrip('/')))
        elif re.match(r'(vite|vitest|webpack)\.config\.(js|mjs|cjs|ts)$', name):
            text = (root / f).read_text(encoding='utf-8', errors='replace')
            for m in re.finditer(r'''['"]?([~@#$][\w/-]*)['"]?\s*:\s*''' + call, text):
                found.append((base, m.group(1), base / m.group(2)))
            for m in re.finditer(r'''find\s*:\s*['"]([^'"]+)['"]\s*,\s*replacement\s*:\s*''' + call, text):
                found.append((base, m.group(1), base / m.group(2)))
    out = []
    for cfg, alias, target in found:
        parts: list[str] = []
        for x in PurePosixPath(target).parts:
            if x == '..' and parts:
                parts.pop()
            elif x not in ('.', ''):
                parts.append(x)
        out.append(('' if str(cfg) == '.' else cfg.as_posix(), alias, '/'.join(parts)))
    return out


def norm(path: PurePosixPath | str) -> str:
    parts: list[str] = []
    for x in PurePosixPath(path).parts:
        if x == '..' and parts:
            parts.pop()
        elif x not in ('.', ''):
            parts.append(x)
    return '/'.join(parts)


class Imports:
    """What a file imports, resolved to repository files: Python imports, relative JS/TS imports and configured aliases
    (tsconfig/jsconfig paths, vite or webpack resolve.alias), trying the usual extensions and index files. A module
    that resolves only to a directory comes back as 'dir/'. Mocked modules (vi.mock, jest.mock) are not links."""
    EXTS = ('.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.py', '.vue', '.svelte')

    def __init__(self, root: Path, files: list[str]):
        self.root, self.fileset = root, set(files)
        self.dirs = {str(PurePosixPath(f).parent) for f in files}
        for d in list(self.dirs):
            while '/' in d:
                d = d.rsplit('/', 1)[0]
                self.dirs.add(d)
        self.aliases = js_aliases(root, files)
        self.cache: dict[str, list[str]] = {}
        self._loaded: dict[str, list[str]] = {}
        self.load_unresolved: dict[str, list[dict]] = {}       # {test file: path loads py_loads could not work out}
        self._roots: list[str] | None = None
        self._roots_for: dict[str, list[str]] = {}

    def targets(self, f: str) -> set[str]:
        try:
            text = (self.root / f).read_text(encoding='utf-8', errors='replace')
        except OSError:
            return set()
        targets = set()
        if f.endswith('.py'):
            try:
                tree = ast.parse(text)
            except SyntaxError:
                return set()
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom) and n.level:          # from .x import y / from .. import y
                    base = PurePosixPath(f).parent
                    for _ in range(n.level - 1):
                        base = base.parent
                    stem = (str(base) + '/' if str(base) != '.' else '') + (n.module or '').replace('.', '/')
                    for t in [f'{stem}/{a.name}' if n.module else (str(base) + '/' if str(base) != '.' else '') + a.name
                              for a in n.names] + ([stem] if n.module else []):
                        targets.add('./' + norm(t))                   # './': already relative to the repository
                    continue
                mods = [a.name for a in n.names] if isinstance(n, ast.Import) else \
                    [f'{n.module}.{a.name}' for a in n.names] + [n.module] if isinstance(n, ast.ImportFrom) and n.module else []
                for m in mods:
                    targets.add(m.replace('.', '/'))
        else:
            text = re.sub(r'''\b(?:vi|jest)\.(?:mock|doMock)\(\s*["'][^"']+["']''', '', text)
            for m in re.finditer(r'''(?:from|require\(|import\()\s*["'](\.[^"']+)["']''', text):
                targets.add(norm(PurePosixPath(f).parent / m.group(1)))
            for m in re.finditer(r'''(?:from|require\(|import\()\s*["']([^."'][^"']*)["']''', text):
                spec = m.group(1)
                for cfg, alias, target in self.aliases:
                    if (not cfg or f.startswith(cfg + '/')) and (spec == alias or spec.startswith(alias + '/')):
                        targets.add(norm(target + spec[len(alias):]))
                        break
        return targets

    def resolve(self, t: str, importer: str = '') -> str | None:
        if t.startswith('./'):                             # a Python relative import, already resolved to the repo
            return self._resolve(t[2:])
        found = self._resolve(t)
        if found is None and not t.startswith('src/'):     # Python src layout: import pkg.mod -> src/pkg/mod.py
            found = self._resolve('src/' + t)
        if found is None and importer.endswith('.py') and t.split('/')[0] not in STDLIB:   # through sys.path roots
            for r in self.roots_for(importer):
                found = self._resolve(f'{r}/{t}')
                if found:
                    break
        return found

    def py_roots(self) -> list[str]:
        """Directories Python code here is imported from, besides the repository root and src/: the parent of every
        top-level package (a directory with __init__.py whose parent has none: libs/x/src for libs/x/src/pkg/...,
        services/y for services/y/shared/...), the directories of pyproject.toml/setup.py/setup.cfg/pytest.ini/tox.ini
        and their src/, pytest `pythonpath` entries, and sys.path.insert/append targets in conftest.py and tests."""
        if self._roots is None:
            roots: set[str] = set()
            for f in self.fileset:
                p = PurePosixPath(f)
                if p.name == '__init__.py':
                    d = p.parent
                    while str(d.parent) != '.' and f'{d.parent}/__init__.py' in self.fileset:
                        d = d.parent
                    roots.add(str(d.parent) if str(d.parent) != '.' else '')
                if p.name in ('pyproject.toml', 'setup.py', 'setup.cfg', 'pytest.ini', 'tox.ini'):
                    d = '' if str(p.parent) == '.' else str(p.parent)
                    roots.update({d, (d + '/src').lstrip('/')})
                    try:
                        text = (self.root / f).read_text(encoding='utf-8', errors='replace')
                    except OSError:
                        text = ''
                    m = re.search(r'^\s*pythonpath\s*=\s*\[([^\]]*)\]', text, re.M) or \
                        re.search(r'^\s*pythonpath\s*=\s*([^\n\[]+)$', text, re.M)
                    if m:
                        for x in re.findall(r'''["']([^"']+)["']''', m.group(1)) or m.group(1).split():
                            roots.add(norm(PurePosixPath(d or '.') / x))
                if p.suffix == '.py' and (p.name == 'conftest.py' or is_test(f)):
                    try:
                        text = (self.root / f).read_text(encoding='utf-8', errors='replace')
                    except OSError:
                        continue
                    for call in re.findall(r'sys\.path\.(?:insert|append)\(([^\n]*)', text):
                        parts = re.findall(r'''["']([^"'/\\]+(?:/[^"']*)?)["']''', call)
                        if '__file__' in call:
                            base = p.parent
                            up = re.search(r'parents\[(\d+)\]', call)
                            # Path(__file__).parent and dirname(__file__) are the file's directory; each further
                            # .parent / dirname, or parents[N], goes up one more
                            for _ in range(int(up.group(1)) if up else call.count('.parent') + call.count('dirname') - 1):
                                base = base.parent
                            roots.add(norm(PurePosixPath(base) / '/'.join(parts)) if parts else norm(base))
                        elif parts:
                            roots.add(norm('/'.join(parts)))
            self._roots = sorted(r for r in roots if r and r in self.dirs)
        return self._roots

    def roots_for(self, importer: str) -> list[str]:
        """Import roots to try for a Python file: its own ancestor directories (pytest puts the first directory
        without __init__.py on sys.path; a service's tests import its packages by their name), nearest first, then the
        repository's import roots, those sharing the longest path with the importer first."""
        d = str(PurePosixPath(importer).parent)
        if d not in self._roots_for:
            anc, x = [], PurePosixPath(importer).parent
            while str(x) != '.':
                anc.append(str(x))
                x = x.parent
            def shared(r: str) -> int:
                n = 0
                for a, b in zip(r.split('/'), d.split('/')):
                    if a != b:
                        break
                    n += 1
                return n
            rest = sorted((r for r in self.py_roots() if r not in anc), key=lambda r: (-shared(r), r))
            self._roots_for[d] = anc + rest
        return self._roots_for[d]

    def _resolve(self, t: str) -> str | None:
        for cand in (t, *(t + e for e in self.EXTS), *(f'{t}/index{e}' for e in self.EXTS), f'{t}/__init__.py'):
            if cand in self.fileset:
                return cand
        if t in self.dirs:
            return t + '/'
        if '/' in t and t.count('/') >= 1:                 # from pkg.mod import Name -> pkg/mod/Name
            parent = t.rsplit('/', 1)[0]
            for cand in (parent + '.py',):
                if cand in self.fileset:
                    return cand
        return None

    def resolved(self, f: str) -> list[str]:
        if f not in self.cache:
            self.cache[f] = sorted({r for r in (self.resolve(t, f) for t in self.targets(f)) if r})
        return self.cache[f]

    def loaded(self, f: str) -> list[str]:
        """Repository files a Python file loads by path instead of importing by name (the shared py_loads reader):
        importlib.util.spec_from_file_location, SourceFileLoader and runpy.run_path with a literal or __file__-built
        path (a script with a hyphen in its name can be loaded no other way), bare imports through a sys.path folder,
        and what those scripts import from their own folder. Loads whose path cannot be worked out are kept in
        load_unresolved, never dropped silently."""
        if f not in self._loaded:
            reach: list[str] = []
            if f.endswith('.py'):
                try:
                    text = (self.root / f).read_text(encoding='utf-8', errors='replace')
                except OSError:
                    text = ''
                if py_loads.may_load(text):
                    got = py_loads.loaded_files(self.root, f)
                    reach = [r for r in got.get('reaches') or [] if r in self.fileset]
                    if got.get('unresolved'):
                        self.load_unresolved[f] = got['unresolved']
            self._loaded[f] = reach
        return self._loaded[f]

    def __call__(self, f: str) -> list[str]:
        """Imported files only (no directories), for one hop of journey attribution."""
        return [r for r in self.resolved(f) if not r.endswith('/')]


def area_of_dir(d: str, areas: list[dict]) -> str | None:
    best = None
    for a in areas:
        for p in a['paths']:
            if p.endswith('/*'):
                continue
            if d == p or d.startswith(p.rstrip('/') + '/'):
                if best is None or len(p) > best[1]:
                    best = (a['id'], len(p))
    return best[0] if best else None


RUST_TEST = re.compile(r'(?:^|/)test[_-]([A-Za-z0-9_]+)\.rs$')


def rust_test_areas(test_path: str, areas: list[dict], files: list[str]) -> set[str]:
    """Areas a Rust integration test exercises, by path convention. Rust integration tests sit in a
    separate tree (coreutils `tests/by-util/test_<util>.rs`, generally `tests/**/test_*.rs`) and
    reference the utility by name, not by a resolvable import path, so the filename is the link:
    `test_<util>.rs` -> `src/uu/<util>/` (the coreutils layout), else any `<util>.rs` source file or
    `<util>/` source directory. Returns the matching area ids."""
    m = RUST_TEST.search(test_path)
    if not m:
        return set()
    util = m.group(1)
    hits = set()
    for cand in (f'src/uu/{util}/src', f'src/uu/{util}', f'crates/{util}/src', f'crates/{util}', f'src/{util}', util):
        aid = area_of_dir(cand, areas)
        if aid:
            hits.add(aid)
    if not hits:                                        # fall back to a source file or directory named for the util
        for sf in files:
            if not sf.endswith('.rs') or is_test(sf):
                continue
            parts = PurePosixPath(sf).parts
            if PurePosixPath(sf).stem == util or util in parts[:-1]:
                aid = area_of(sf, areas)
                if aid:
                    hits.add(aid)
    return hits


# {area: tests linked to it only because they load one of its scripts by file path}; set by tests_by_area()
PATH_LOADS: dict[str, set[str]] = {}
LIVE_TEST_DIRS = {'live', 'tests-live', 'live-tests', 'live_tests'}
HELPER_DIRS = {'helpers', 'helper', 'support', 'fixtures', '__fixtures__', 'utils', 'testutils', 'test-utils', 'test_utils',
               'mocks', '__mocks__'}


def not_a_counted_test(t: str) -> str | None:
    """Why a file in a test tree doesn't count as a test of the areas it imports, or None when it does: live tests need
    real credentials or services and don't run in a routine CI run; helpers and fixtures in a test tree are not tests."""
    if fixture_of(t):
        return f'test of a {fixture_reason(t)}: it exercises the sample, not the repository\'s own code'
    if t in FIXTURE_BINS:
        return 'helper binary under tests/ (fn main, built for tests to run), not a test'
    names = dir_names(t)
    if names & LIVE_TEST_DIRS:
        return 'live test: needs real credentials or services, so it does not run in a routine CI run'
    if names & HELPER_DIRS and not detect_conventions.is_test_file(t):
        return 'test helper or fixture, not a test'
    return None


def tests_by_area(files: list[str], areas: list[dict], flaky: set[str], imports: Imports) -> tuple[dict, list[str], list[dict]]:
    """{area: [test files that exercise it]}, flaky test files left out, and the other test-tree files not counted (live
    tests, helpers) with the reason. A test exercises an area when it imports a
    file of the area (resolved to the file, so a top-level 'src/*' area matches only files directly in src/), imports
    a package directory inside the area, or sits inside the area itself (a co-located test). Rust integration tests,
    which import the utility by name rather than by a resolvable path, are linked by the `test_<util>.rs` convention."""
    out: dict[str, set[str]] = {a['id']: set() for a in areas}
    skipped, not_counted = [], []
    PATH_LOADS.clear()
    for t in files:
        if not is_test(t) or PurePosixPath(t).suffix not in CODE_EXT:
            continue
        if t in flaky:
            skipped.append(t)
            continue
        why = not_a_counted_test(t)
        if why:
            not_counted.append({'path': t, 'reason': why})
            continue
        # a test kept with the examples or docs is linked only by what it imports, never by sitting in an area; an
        # import of a sample project's code (or of other example code) links nothing
        beside_examples = bool(dir_names(t) & DOC_DIRS)
        hit = set() if beside_examples else {area_of(COPY_REP.get(t, t), areas)}
        for r in imports.resolved(t):     # a copy of a shared file is tested where its scored copy is
            if fixture_of(r.rstrip('/')) or dir_names(r.rstrip('/') + ('/x' if r.endswith('/') else '')) & DOC_DIRS:
                continue
            hit.add(area_of_dir(r.rstrip('/'), areas) if r.endswith('/') else area_of(COPY_REP.get(r, r), areas))
        for r in imports.loaded(t):       # scripts the test loads by file path (py_loads), and their sibling imports
            if fixture_of(r) or dir_names(r) & DOC_DIRS:
                continue
            aid = area_of(COPY_REP.get(r, r), areas)
            if aid and aid not in hit:
                PATH_LOADS.setdefault(aid, set()).add(t)
            hit.add(aid)
        if t.endswith('.rs'):
            hit |= rust_test_areas(t, areas, files)
            hit |= rust_crate_uses(t, areas)
        if beside_examples and not hit - {None}:
            not_counted.append({'path': t, 'reason': 'test kept with the examples or docs: it imports no area\'s code (it '
                                                     'tests the sample), so it is not a test of an area'})
        for aid in hit - {None}:
            out[aid].add(t)
    # Rust unit tests live in the source file, in #[cfg(test)] modules: the file is a test of its own area too
    for f in RUST_INLINE_TESTS:
        aid = area_of(f, areas)
        if aid in out and f not in flaky:
            out[aid].add(f)
    # a script's own self-test is a test of the script's area (once per copy group: the scored copy's)
    for f in SELF_TESTS:
        aid = area_of(f, areas)
        if aid in out and COPY_REP.get(f, f) == f and not fixture_of(f):
            out[aid].add(f)
    for f in sorted(PY_BENCH):
        not_counted.append({'path': f, 'reason': bench_reason(f)})
    return {k: sorted(v) for k, v in out.items()}, skipped, not_counted


RUST_PATH_ROOT = re.compile(r'(?:^|[^:\w])([a-z_][a-z0-9_]*)::')


def rust_crate_uses(t: str, areas: list[dict]) -> set[str]:
    """Areas of the workspace crates a Rust test names by path (`use station_core::mean;`, `station_core::x()`): an
    integration test in one crate's tests/ that drives another crate is a test of that crate too."""
    if not CRATE_DIRS:
        return set()
    try:
        text = cargo_project.mask((Path(CARGO['repo']) / t).read_text(encoding='utf-8', errors='replace'))
    except (OSError, KeyError):
        return set()
    hits = set()
    for name in set(RUST_PATH_ROOT.findall(text)) & set(CRATE_DIRS):
        d = CRATE_DIRS[name]
        aid = area_of_dir(d, areas) if d != '.' else None
        if aid:
            hits.add(aid)
    return hits


# ---------------------------------------------------------------- inputs from other skills
# Each skill rates its findings its own way; these map them onto critical/high/medium/low. A rating the adapter can't
# read is 'unknown' (never a guess): it doesn't set the High floor, and score_risk warns about it.
PRIORITY_SEVERITY = {'P0': 'critical', 'P1': 'high', 'P2': 'medium', 'P3': 'low', 'P4': 'low'}
WORD_SEVERITY = {'critical': 'critical', 'blocker': 'critical', 'high': 'high', 'serious': 'high', 'major': 'high',
                 'medium': 'medium', 'moderate': 'medium', 'low': 'low', 'minor': 'low', 'info': 'low', 'informational': 'low'}


def rating_of(f: dict, fields: list[str]) -> tuple[str, str]:
    """(severity, source_rating) from the first of `fields` (dotted paths) that holds a rating this adapter knows."""
    for field in fields:
        v = f
        for part in field.split('.'):
            v = v.get(part) if isinstance(v, dict) else None
        if isinstance(v, dict):                      # e.g. wcag priority {"band": "P0", ...}
            v = v.get('band')
        if isinstance(v, str) and v.strip():
            key = v.strip()
            sev = PRIORITY_SEVERITY.get(key.upper()) or WORD_SEVERITY.get(key.lower())
            if sev:
                return sev, f'{field} {key}'
    return 'unknown', 'no rating read from ' + ' / '.join(fields)


def files_of(f: dict) -> list[str]:
    files = [l['file'] for l in f.get('locations') or [] if isinstance(l, dict) and l.get('file')]
    if isinstance(f.get('file'), str):
        files.append(f['file'])
    return files


# The fields each adapter reads, in order. SKILL.md lists them; the validator re-reads the source report with the same
# adapter and checks every carried severity.
ADAPTERS = {
    'security': ('findings', ['cvss.severity', 'severity', 'priority']),
    'crash': ('crashes', ['ranking.priority', 'ranking.band', 'priority', 'severity']),
    'wcag': ('findings', ['severity', 'priority']),
}


def load_input(kind: str, path: Path) -> dict:
    data = json.loads(path.read_text(encoding='utf-8'))
    md = data.get('metadata') if isinstance(data.get('metadata'), dict) else {}
    meta = {'skill': kind, 'path': path.name,
            'generated_at': next((v for v in (data.get('generated_at'), md.get('generated_at'), data.get('generated'),
                                              md.get('generated'), md.get('created_at')) if isinstance(v, str) and v), None)}
    items = []
    if kind != 'flaky':
        key, fields = ADAPTERS.get(kind, ('findings', ['severity', 'priority', 'rating', 'ranking.priority']))
        for f in data.get(key, []):
            sev, src = rating_of(f, fields)
            files = files_of(f)
            if kind == 'wcag':
                files += [x for x in (f.get('component'),) if isinstance(x, str) and '/' in x and ' ' not in x]
            items.append({'id': f['id'], 'title': f.get('title'), 'severity': sev, 'source_rating': src,
                          'confidence': f.get('confidence'), 'files': files})
    else:
        for f in data.get('flakes', []):
            tid = f.get('test_id', '')
            file = tid.split('::')[0]
            if not file.endswith('.py') and '.' in file:
                file = file.replace('.', '/') + '.py'
            items.append({'id': f['id'], 'test_file': file, 'confidence': f.get('confidence'), 'severity': None, 'files': []})
    return {**meta, 'items': items}


HTTP_CALL = re.compile(r'\b(GET|POST|PUT|PATCH|DELETE)\s+(/[\w\-./\[\]:{}]*)')
ROUTE_FILE = re.compile(r'(?:^|/)(app|pages)/(.*?)/?(route|index)?\.(?:ts|tsx|js|jsx|mjs)$')


def route_index(files: list[str]) -> list[tuple[list[str], str]]:
    """URL patterns of route handlers: Next.js App Router app/**/route.ts and Pages Router pages/api/**. Route groups
    (x) and parallel slots @x drop out of the URL; [id], [...slug] and [[...slug]] are wildcards."""
    out = []
    for f in files:
        m = ROUTE_FILE.search(f)
        if not m:
            continue
        kind, rest, stem = m.group(1), m.group(2), m.group(3)
        if kind == 'app' and stem != 'route':
            continue
        if kind == 'pages':
            if not rest.startswith('api'):
                continue
            if not stem:                                    # pages/api/users.ts -> /api/users
                rest = f[m.start(2):].rsplit('.', 1)[0]
        segs = [s for s in rest.split('/') if s and not (s.startswith('(') and s.endswith(')') and not s.startswith('(.'))
                and not s.startswith('@')]
        out.append((segs, f))
    return out


def resolve_call(path: str, routes: list[tuple[list[str], str]]) -> str | None:
    want = [s for s in path.split('?')[0].split('/') if s]
    same = lambda a, b: a == b or a.startswith('[') or b.startswith((':', '{', '[')) or '$' in b  # noqa: E731
    best = None
    for segs, f in routes:
        if segs and segs[-1].startswith(('[...', '[[...')):
            prefix = segs[:-1]
            ok = len(want) >= len(prefix) + (0 if segs[-1].startswith('[[') else 1) and all(map(same, prefix, want))
        else:
            ok = len(segs) == len(want) and all(map(same, segs, want))
        if ok:
            exact = sum(a == b for a, b in zip(segs, want))
            if best is None or exact > best[0]:
                best = (exact, f)
    return best[1] if best else None


def journey_links(path: Path | None, files: list[str], imports, hubs: set | None = None) -> tuple[list[dict], dict]:
    """Journeys and the repository files each one reaches, with how: the files its steps cite as evidence, the files of
    the surface items it covers (endpoints resolve to their route handlers), the route handler behind every
    "POST /api/x" note in its evidence, and one hop of imports from those files (a page or route handler reaches the
    lib module that does the work). Also the map's scope: which files it mapped and which it declared out of scope."""
    scope = {'present': False, 'partial': False, 'domains': 'all', 'mapped_files': set(), 'out_of_scope_files': set()}
    if not path:
        return [], scope
    data = json.loads(path.read_text(encoding='utf-8'))
    scope['present'] = True
    fileset = set(files)
    routes = route_index(files)
    items = {i['id']: i for kind in (data.get('surface') or {}).values() if isinstance(kind, list) for i in kind if isinstance(i, dict)}
    orphans = (data.get('coverage') or {}).get('orphans') or []
    # out_of_scope (a domain the run didn't map) and not_mapped_this_run (in scope, a user path exists, not mapped yet)
    # both say the map is silent about the item, not that the item is off every journey.
    out_items = {o['item'] for o in orphans if o.get('reason') in ('out_of_scope', 'not_mapped_this_run')}
    domains = ((data.get('metadata') or {}).get('run_scope') or {}).get('domains', 'all')
    scope['domains'] = domains
    scope['partial'] = domains != 'all' or bool(out_items)
    for iid, it in items.items():
        f = it.get('file')
        if not f:
            continue
        if iid in out_items or (domains != 'all' and it.get('domain') and it['domain'] not in domains):
            scope['out_of_scope_files'].add(f)
        else:
            scope['mapped_files'].add(f)
    out = []
    for j in data.get('journeys', []):
        how: dict[str, str] = {}
        for s in j.get('steps', []) + j.get('variants', []):
            for e in s.get('evidence', []):
                if e.get('file'):
                    how.setdefault(e['file'], 'evidence')
                for m in HTTP_CALL.finditer(e.get('note') or ''):
                    r = resolve_call(m.group(2), routes)
                    if r:
                        how.setdefault(r, f'calls {m.group(1)} {m.group(2)}')
        for iid in j.get('covers', []):
            it = items.get(iid)
            if it and it.get('file'):
                how.setdefault(it['file'], f'covers {iid}')
        for f in [f for f in how if f in fileset]:
            if hubs and f in hubs:          # a module that creates the app or imports most areas links nothing by import
                continue
            for g in imports(f):
                how.setdefault(g, f'imported by {f}')
        out.append({'id': j['id'], 'title': j.get('title'), 'priority': j.get('priority'), 'files': sorted(how), 'how': how})
    for j in out:
        scope['mapped_files'] |= set(j['files'])
    return out, scope


HASH_COMMENT = {'.py', '.rb'}
JS_TOKENS = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|`(?:\\.|[^`\\])*`', re.S)
PY_TOKENS = re.compile(r'#[^\n]*|"""(?:\\.|[^\\])*?"""|\'\'\'(?:\\.|[^\\])*?\'\'\'|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', re.S)


def code_view(text: str, suffix: str) -> str:
    """The text with comments and string literals blanked (newlines kept, so line numbers hold): a payment provider
    named in a CSP header, an API description or a comment doesn't move money."""
    rx = PY_TOKENS if suffix in HASH_COMMENT else JS_TOKENS
    out = rx.sub(lambda m: '""' + '\n' * m.group(0).count('\n'), text)
    return blank_jsx_text(out) if suffix in JSX_EXT else out


JSX_EXT = {'.tsx', '.jsx', '.js'}
JSX_BEFORE_WORDS = {'return', 'yield', 'await', 'default', 'case', 'else', 'in', 'of'}
TOP_LEVEL = re.compile(r'\n(?=(?:export|function|const|let|var|import|class|async\s+function|type|interface)\b)')


def _brace_end(s: str, i: int) -> int:
    """Index of the } closing the { at i (strings and comments are already blanked), or len(s)-1."""
    depth = 0
    for j in range(i, len(s)):
        if s[j] == '{':
            depth += 1
        elif s[j] == '}':
            depth -= 1
            if depth == 0:
                return j
    return len(s) - 1


def blank_jsx_text(s: str) -> str:
    """Blank JSX text (copy between tags: '<p>We follow the GDPR</p>') in a code view whose strings and comments are
    already blanked, keeping newlines, tags and {expressions}. A '<' opens a tag when JSX is open, or at depth 0 when the
    character before it is not an identifier, ')' or ']' (so a generic like useState<string> or a comparison a < b is
    not a tag) unless that identifier is a keyword such as return. A declaration at column 0 closes any JSX left open,
    so a missed closing tag cannot blank the rest of the file."""
    if '<' not in s:
        return s
    out = list(s)
    n, i, depth = len(s), 0, 0
    while i < n:
        c = s[i]
        if c == '\n' and depth and TOP_LEVEL.match(s, i):
            depth = 0
        if depth and c == '{':
            i = _brace_end(s, i) + 1
            continue
        if c == '<' and i + 1 < n and (s[i + 1].isalpha() or s[i + 1] in '/>'):
            tag = bool(depth)
            if not tag:
                j = i - 1
                while j >= 0 and s[j] in ' \t\r\n':
                    j -= 1
                if j < 0 or not (s[j].isalnum() or s[j] in '_$)].'):
                    tag = True
                else:
                    k = j
                    while k >= 0 and (s[k].isalnum() or s[k] in '_$'):
                        k -= 1
                    tag = s[k + 1:j + 1] in JSX_BEFORE_WORDS
            if tag:
                j = i + 1
                while j < n and s[j] != '>':          # the tag's end, past {attribute expressions}
                    if s[j] == '{':
                        j = _brace_end(s, j)
                    elif s[j] == '<' and j > i + 1:   # not a tag after all ('<' again before '>')
                        break
                    j += 1
                if j < n and s[j] == '>':
                    if s[i + 1] == '/':
                        depth = max(0, depth - 1)
                    elif s[j - 1] != '/':
                        depth += 1
                    i = j + 1
                    continue
        if depth and c != '\n':
            out[i] = ' '
        i += 1
    return ''.join(out)


def comment_free(text: str, suffix: str) -> str:
    """The text with comments blanked but string literals kept (newlines kept): what raw_patterns run on, so SQL or a
    device command inside a string counts and the same words in a comment do not."""
    rx = PY_TOKENS if suffix in HASH_COMMENT else JS_TOKENS
    return rx.sub(lambda m: (' ' + '\n' * m.group(0).count('\n')) if m.group(0).startswith(('#', '//', '/*'))
                  else m.group(0), text)


TEST_CFG = re.compile(r'(?<!not\()\btest\b')
CFG_TEST_ATTR = re.compile(r'^\s*#\[\s*cfg\s*\((.*)\)\s*\]\s*$')
TEST_MOD_OPEN = re.compile(r'^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+\w+\s*\{')


def rust_test_ranges(text: str) -> list[tuple[int, int]]:
    """[(first line index, end line index)) of every inline test module: a `mod x { ... }` under a cfg attribute that
    turns on with test (#[cfg(test)], #[cfg(all(test, feature = "x"))], not #[cfg(not(test))]), at any depth. Rust
    keeps unit tests in the source file; those lines are test code, not product source or a surface."""
    masked = cargo_project.mask(text).splitlines()
    out: list[tuple[int, int]] = []
    i = 0
    while i < len(masked):
        m = CFG_TEST_ATTR.match(masked[i])
        if m and TEST_CFG.search(m.group(1)):
            j = i + 1
            while j < len(masked) and (not masked[j].strip() or masked[j].strip().startswith('#[')):
                j += 1
            if j < len(masked) and TEST_MOD_OPEN.match(masked[j]):
                depth, k = 0, j
                while k < len(masked):
                    depth += masked[k].count('{') - masked[k].count('}')
                    k += 1
                    if depth <= 0:
                        break
                out.append((i, k))
                i = k
                continue
        i += 1
    return out


# The Cargo model of the repository (cargo_project.read_cargo), set by set_cargo_model() from score(): files that are
# test modules (declared under a test cfg, `#[cfg(test)] mod x;` or with #[path]), bench targets, inline test-module
# line counts, crate names (for integration tests that `use` another crate) and the surface hits the model reads.
CARGO: dict = {}
RUST_TEST_FILES: set[str] = set()
BENCH_FILES: set[str] = set()
FIXTURE_BINS: set[str] = set()
RUST_INLINE_TESTS: dict[str, tuple[list[tuple[int, int]], int]] = {}
CRATE_DIRS: dict[str, str] = {}
BENCH_REASON = 'Cargo bench target (benches/ or [[bench]]): benchmark code, not product source'


def bench_reason(f: str) -> str:
    if f in PY_BENCH:
        return (f'benchmark collected by pytest through {PY_BENCH[f]}: it measures speed, so it is neither product '
                'source nor a test that counts')
    return BENCH_REASON


def is_bench(f: str) -> bool:
    return f in BENCH_FILES or f in PY_BENCH or (f.endswith('.rs') and 'benches' in dir_names(f) and bool(CARGO.get('is_cargo')))


def set_cargo_model(root: Path, files: list[str]) -> None:
    for x in (RUST_TEST_FILES, BENCH_FILES, FIXTURE_BINS, RUST_INLINE_TESTS, CRATE_DIRS, CARGO):
        x.clear()
    if not (root / 'Cargo.toml').is_file():
        return
    try:
        CARGO.update(cargo_project.read_cargo(root))
    except Exception as e:                       # a manifest the reader cannot parse: say so, score without the model
        CARGO.update({'is_cargo': True, 'crates': [], 'error': f'{type(e).__name__}: {e}'})
        return
    fileset = set(files)
    for c in CARGO.get('crates', []):
        CRATE_DIRS[c['name'].replace('-', '_')] = c['dir']
        for t in c.get('targets', []):
            if t['kind'] == 'bench':
                BENCH_FILES.add(t['path'])
        FIXTURE_BINS.update(b['file'] for b in c.get('fixture_bins', []))
    for f in files:
        if not f.endswith('.rs'):
            continue
        sc = cargo_project.scan_file(root / f, f)
        p = PurePosixPath(f)
        for d in sc.get('mods', []):
            if not any(TEST_CFG.search(c) for c in d.get('cfg') or []):
                continue
            if d.get('path_attr'):
                cands = [p.parent / d['path_attr']]
            else:
                base = p.parent if p.name in ('lib.rs', 'main.rs', 'mod.rs') else p.parent / p.stem
                base = base.joinpath(*d['inline']) if d['inline'] else base
                cands = [base / f'{d["name"]}.rs', base / d['name'] / 'mod.rs', p.parent / f'{d["name"]}.rs']
            hit = next((os.path.normpath(str(c)).replace('\\', '/') for c in cands
                        if os.path.normpath(str(c)).replace('\\', '/') in fileset), None)
            if hit:
                RUST_TEST_FILES.add(hit)
    for f in files:
        if not f.endswith('.rs') or f in RUST_TEST_FILES:
            continue
        try:
            text = (root / f).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        ranges = rust_test_ranges(text)
        if ranges:
            lines = text.splitlines()
            n = sum(1 for a, b in ranges for x in lines[a:b] if x.strip())
            RUST_INLINE_TESTS[f] = (ranges, n)


def in_rust_test_range(f: str, line: int) -> bool:
    return any(a < line <= b for a, b in RUST_INLINE_TESTS.get(f, ([], 0))[0])


def cargo_surface_hits() -> dict[str, list[tuple[str, str]]]:
    """{file: [(class, evidence)]} the Cargo model reads: clap CLIs (derive and builder) are public entry points,
    std::process::Command launches run processes, libloading loads load native code. Hits in test modules are left out."""
    out: dict[str, list[tuple[str, str]]] = {}
    for c in CARGO.get('crates', []):
        for cli in c.get('clis', []):
            if not in_rust_test_range(cli['file'], cli['line']):
                n = len(cli.get('commands') or [])
                out.setdefault(cli['file'], []).append(
                    ('public_api', f'{cli["file"]}:{cli["line"]} (clap {cli.get("style")} CLI'
                     + (f' {cli["name"]}' if cli.get('name') else '') + (f', {n} subcommand(s)' if n else '') + ')'))
        for p in c.get('process_launches', []):
            if not in_rust_test_range(p['file'], p['line']):
                prog = (p.get('program_expr') or '').strip()
                out.setdefault(p['file'], []).append(
                    ('subprocess', f'{p["file"]}:{p["line"]} (std::process::Command' + (f' {prog}' if prog else '') + ')'))
        for p in c.get('library_loads', []):
            if not in_rust_test_range(p['file'], p['line']):
                out.setdefault(p['file'], []).append(('native_code', f'{p["file"]}:{p["line"]} ({p.get("call") or "library load"})'))
    return out


TS_EXT = {'.ts', '.tsx', '.mts', '.cts'}
# A TypeScript member signature with no body (an interface or abstract member): `settle(): Promise<void>;`,
# `delete(id: string, signal: AbortSignal): Promise<boolean>;`. It declares a method; it doesn't call one.
TS_SIGNATURE = re.compile(r'^\s*(?:(?:public|private|protected|static|readonly|abstract|declare|async|override)\s+)*'
                          r'[\w$#]+\??\s*(?:<[^>]*>)?\s*\(.*\)\s*:\s*[^={}]+;\s*$')
_COMPILED: dict | None = None


def _any(patterns: list[str]):
    """One case-insensitive regex matching any of the patterns (None for none): one search per line, not one each."""
    return re.compile('|'.join(f'(?:{p})' for p in patterns), re.I) if patterns else None


def compiled_surfaces() -> dict:
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = {cls: (_any(spec.get('patterns', [])), _any(spec.get('raw_patterns', [])),
                           _any(spec.get('exclude', [])), spec.get('langs'),
                           {ext: _any(pats) for ext, pats in (spec.get('lang_exclude') or {}).items()},
                           _any(spec.get('raw_exclude', [])))
                     for cls, spec in SURFACES['classes'].items()}
    return _COMPILED


def line_classes(raw: str, code: str, suffix: str) -> list[str]:
    """The surface classes one line matches: patterns on the code view (comments and strings blanked), raw_patterns on
    the line as written, minus the class's excludes; a TypeScript member signature matches nothing."""
    if not raw.strip() or (suffix in TS_EXT and TS_SIGNATURE.match(code)):
        return []
    out = []
    for cls, (pat, rawp, excl, langs, lang_excl, raw_excl) in compiled_surfaces().items():
        if langs and suffix not in langs:        # the systems/CLI pack is Rust-only
            continue
        if not ((pat and pat.search(code)) or (rawp and rawp.search(raw))):
            continue
        lx = lang_excl.get(suffix)               # web-tuned idioms the language reuses harmlessly
        if (excl and excl.search(code)) or (lx and lx.search(code)) or (raw_excl and raw_excl.search(raw)):
            continue
        out.append(cls)
    return out


DIST_DIR = re.compile(r'^(?:dist|lib|build|out)(?:/(?:esm|cjs|es|umd|mjs))?/')
JS_OUT_EXT = re.compile(r'\.(?:m|c)?jsx?$')


def manifest_entries(root: Path, files: list[str]) -> dict[str, str]:
    """{source file: why it is a public entry point}: the files package.json main, module, exports and bin name (a
    dist/, lib/, build/ or out/ target mapped back to its src/ file, extensions swapped for TypeScript) and the modules
    pyproject.toml [project.scripts] / [tool.poetry.scripts] name. Type-only export conditions are skipped."""
    fileset = set(files)
    out: dict[str, str] = {}

    def to_source(pre: str, target: str) -> str | None:
        t = norm(target)
        if t.endswith('.d.ts'):
            return None
        stems = [t]
        if DIST_DIR.match(t):
            stems += ['src/' + DIST_DIR.sub('', t), DIST_DIR.sub('', t)]
        for s in stems:
            base = JS_OUT_EXT.sub('', s)
            for cand in (pre + s, *(pre + base + e for e in ('.ts', '.tsx', '.mts', '.cts', '.js', '.mjs', '.jsx')),
                         *(f'{pre}{base}/index{e}' for e in ('.ts', '.tsx', '.js'))):
                if cand in fileset:
                    return cand
        return None

    def walk_exports(v, label: str, acc: list):
        if isinstance(v, str):
            acc.append((label, v))
        elif isinstance(v, dict):
            for k, x in v.items():
                if k == 'types':
                    continue
                walk_exports(x, f'exports "{k}"' if k.startswith('.') else label, acc)
        elif isinstance(v, list):
            for x in v:
                walk_exports(x, label, acc)

    for f in files:
        p = PurePosixPath(f)
        if 'node_modules' in p.parts or p.name not in ('package.json', 'pyproject.toml'):
            continue
        pre = '' if str(p.parent) == '.' else str(p.parent) + '/'
        try:
            text = (root / f).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        targets: list[tuple[str, str]] = []
        if p.name == 'package.json':
            try:
                pkg = json.loads(text)
            except ValueError:
                continue
            if not isinstance(pkg, dict):
                continue
            for k in ('main', 'module'):
                if isinstance(pkg.get(k), str):
                    targets.append((k, pkg[k]))
            walk_exports(pkg.get('exports'), 'exports', targets)
            b = pkg.get('bin')
            if isinstance(b, str):
                targets.append(('bin', b))
            elif isinstance(b, dict):
                targets += [(f'bin "{k}"', v) for k, v in b.items() if isinstance(v, str)]
            for label, t in targets:
                src = to_source(pre, t)
                if src:
                    out.setdefault(src, f'{f} {label}')
        else:
            m = re.search(r'^\[(?:project|tool\.poetry)\.scripts\]\s*$(.*?)(?=^\[|\Z)', text, re.M | re.S)
            for name, mod in re.findall(r'''^\s*["']?([\w.-]+)["']?\s*=\s*["']([\w.]+)(?::[\w.]+)?["']''', m.group(1) if m else '', re.M):
                rel = mod.replace('.', '/')
                for cand in (f'{pre}{rel}.py', f'{pre}src/{rel}.py', f'{pre}{rel}/__init__.py', f'{pre}src/{rel}/__init__.py'):
                    if cand in fileset:
                        out.setdefault(cand, f'{f} scripts "{name}"')
                        break
    return out


def surface_scan(root: Path, files: list[str], entries: dict[str, str] | None = None,
                 extra: dict[str, list[tuple[str, str]]] | None = None) -> tuple[int, list[str], list[str]]:
    """Surface classes found in the area's files. Patterns run on code with comments and strings blanked;
    raw_patterns (SQL statements, the 'use server' directive) run on the line as written. Type declaration files
    (.d.ts) declare, they don't do, so they are skipped. `entries` (manifest_entries) are the files a package manifest
    names as public entry points; they match the class with manifest_entries set. `extra` ({file: [(class,
    evidence)]}, next_surface_hits) adds what the Next.js model found: script loaders whose src is a constant, and the
    headers, CSP and redirects of next.config.* and platform files."""
    hits: dict[str, list[str]] = {}
    by_path = {cls: [re.compile(p, re.I) for p in spec.get('path_patterns', [])] for cls, spec in SURFACES['classes'].items()}
    entry_cls = next((c for c, spec in SURFACES['classes'].items() if spec.get('manifest_entries')), None)
    for f in files:
        if f.endswith('.d.ts'):
            continue
        for cls, pats in by_path.items():
            if any(p.search(f) for p in pats):
                hits.setdefault(cls, []).append(f'{f} (path)')
        if entry_cls and entries and f in entries:
            hits.setdefault(entry_cls, []).append(f'{f} ({entries[f]})')
        for cls, ev in (extra or {}).get(f, []):
            if cls in SURFACES['classes'] and ev not in hits.get(cls, []):
                hits.setdefault(cls, []).append(ev)
        try:
            text = (root / f).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        suffix = PurePosixPath(f).suffix
        raw_lines = comment_free(text, suffix).splitlines()     # raw_patterns see strings, not comments
        code_lines = code_view(text, suffix).splitlines()
        if suffix == '.rs':                     # inline #[cfg(test)] modules are test code, not a surface
            for a, b in rust_test_ranges(text):
                for k in range(a, min(b, len(raw_lines), len(code_lines))):
                    raw_lines[k] = code_lines[k] = ''
        if suffix == '.py':
            # self-test code (a self_test() and what only it uses: fixture pages, case tables) is test code; the strings
            # of module-level tables and multi-line constants are data (a scanner's pattern table), so the raw patterns,
            # which read inside strings, see those lines with string contents blanked (code_mask)
            ranges, data = python_test_layout(text)
            for a, b in ranges:
                for k in range(a, min(b, len(raw_lines), len(code_lines))):
                    raw_lines[k] = code_lines[k] = ''
            if data:
                masked = comment_free(code_mask.mask(text, 'python'), suffix).splitlines()
                if len(masked) == len(raw_lines):
                    for i in data:
                        if 0 < i <= len(raw_lines) and raw_lines[i - 1]:
                            raw_lines[i - 1] = masked[i - 1]
        for i, (raw, code) in enumerate(zip(raw_lines, code_lines), 1):
            for cls in line_classes(raw, code, suffix):
                if not any(h.startswith(f'{f}:{i} (') for h in hits.get(cls, [])):   # the model already cites it
                    hits.setdefault(cls, []).append(f'{f}:{i}')
    if not hits:
        return SURFACES['none_score'], [], ['no surface pattern matched in data/surfaces.json']
    best = max(hits, key=lambda c: SURFACES['classes'][c]['score'])
    ev = [f'{SURFACES["classes"][c]["label"]}: {", ".join(hits[c][:3])}' + (f' (+{len(hits[c]) - 3})' if len(hits[c]) > 3 else '')
          for c in sorted(hits, key=lambda c: -SURFACES['classes'][c]['score'])]
    return SURFACES['classes'][best]['score'], sorted(hits), ev


# ---------------------------------------------------------------- scoring
# The languages the scripts measure (complexity, surfaces, churn): the census names everything else.
MEASURED_LANGUAGES = {'Python', 'JavaScript', 'TypeScript', 'Vue', 'Svelte', 'Go', 'Rust', 'Java', 'Kotlin', 'Scala', 'C#',
                      'Swift', 'Dart', 'Ruby', 'PHP', 'Objective-C', 'C', 'C++', 'C/C++ header'}


def language_note(root: Path) -> tuple[dict, str | None]:
    """(language_coverage, warning): lang_census over the repository (vendored and embedded third-party trees left
    out) against MEASURED_LANGUAGES. Code in another language gets no complexity, churn area or surface score, so it is
    named, loudly when it is most of the repository."""
    cov = lang_census.language_coverage(lang_census.census(root), MEASURED_LANGUAGES, tool='score_risk.py',
                                        what='risk ranking')
    if cov['status'] == 'unsupported_dominant':
        return cov, cov['warning']
    if cov['unsupported']:
        return cov, ('code in languages these scripts do not measure is not scored (no complexity or surface): '
                     + ', '.join(f'{n} {k}' for k, n in cov['unsupported'].items()) + '; judge the areas holding it by hand')
    return cov, None


def uncovered_language(files: list[str], owner: dict, cx: dict, root: Path, area_basis: str) -> str | None:
    """A warning when most code files of the dominant language (vendored and generated left out) are outside every
    area: the plan would rank incidental files and miss the product, so it must not look complete."""
    by_lang: dict[str, list[int]] = {}
    for f in files:
        sfx = PurePosixPath(f).suffix
        loc = cx.get(f, {}).get('loc', 0)
        if sfx in CODE_EXT and not is_test(f) and loc > 0 and \
                diff_scope.classify(f, '', False, root)[0] not in ('generated', 'vendored'):
            acc = by_lang.setdefault(sfx, [0, 0, 0, 0])
            acc[0] += 1
            acc[1] += 1 if owner.get(f) is not None else 0
            acc[2] += loc
            acc[3] += loc if owner.get(f) is not None else 0
    if not by_lang:
        return None
    dominant = max(sorted(by_lang), key=lambda k: by_lang[k][2])
    total, inside, total_loc, inside_loc = by_lang[dominant]
    # by files or by lines: one large module outside the areas (an app.py beside many small scripts) is most of the code
    if (total >= 5 and inside < total / 2) or (total_loc >= 200 and inside_loc < total_loc / 2):
        return (f'the dominant language\'s code is not in the areas: {total - inside} of {total} {dominant} file(s), '
                f'{total_loc - inside_loc} of {total_loc} lines, are outside every area (area basis: {area_basis}); the '
                'ranking does not cover the product code, so give --areas before building a plan')
    return None


UI_MARKUP_EXT = {'.slint': 'Slint', '.qml': 'QML', '.xaml': 'XAML', '.axaml': 'Avalonia XAML', '.ui': 'Qt Designer',
                 '.glade': 'GTK Builder', '.blp': 'GTK Blueprint', '.kv': 'Kivy', '.xib': 'Interface Builder',
                 '.storyboard': 'Interface Builder'}


# Product files in a language or format no script here reads: every language of the census outside CODE_EXT (Lua,
# Elixir ...), UI markup and script languages (PowerShell, shell, batch ...). They count for their area's churn,
# defects and change in scope like templates and data, and are named wherever they count or change.
UNREAD_EXT = {**{e.lower(): k for e, k in lang_census.LANG_BY_EXT.items() if e.lower() not in CODE_EXT and e != '.pyi'},
              **lang_census.UI_MARKUP_BY_EXT, **UI_MARKUP_EXT, **lang_census.SCRIPT_BY_EXT}
SCRIPT_LANGUAGE_WARNING = 'script languages these scripts do not measure'
# Changed files that are never listed as lines to test: lockfiles, dotfiles, licence and changelog files, images,
# fonts, media and archives. (Documentation and binary files are left out by their diff_scope kind.)
SCOPE_NOISE = re.compile(r'(?:^|/)(?:\.[^/]*|[^/]*\.lock|[^/]*-lock\.(?:json|ya?ml)|go\.sum|LICEN[CS]E[^/]*|NOTICE[^/]*|'
                         r'AUTHORS[^/]*|CHANGELOG[^/]*|CODEOWNERS)$|\.(?:png|jpe?g|gif|svg|ico|webp|avif|bmp|woff2?|ttf|otf|'
                         r'eot|mp[34]|webm|ogg|wav|pdf|zip|gz|tgz|7z|jar|bin|exe|dll|so|dylib)$', re.I)
SCOPE_SKIP_KINDS = {'docs', 'binary', 'types', 'generated', 'vendored', 'test'}


def unread_kind(f: str) -> str | None:
    sfx = PurePosixPath(f).suffix.lower()
    return UNREAD_EXT.get(sfx) if sfx not in ASSET_EXT else None


def unread_words(paths: list[str]) -> str:
    """'PowerShell 2, Slint 1' for the unread files among paths ('' when there are none)."""
    kinds: dict[str, int] = {}
    for f in paths:
        k = unread_kind(f)
        if k:
            kinds[k] = kinds.get(k, 0) + 1
    return ', '.join(f'{k} {n}' for k, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])))


def outside_dir(sp: str, areas: list[dict]) -> str:
    """The directory a changed file outside the areas is listed under: the shortest directory above it that no area
    sits in or under (docs/ for docs/a/b.md; crates/x/benches/ for a bench of the area crates/x, never crates/, which
    every area id contains), or the file itself when every directory above it holds an area."""
    parts = sp.split('/')
    roots = [q.rstrip('/*') for a in areas for q in a['paths']]
    for i in range(1, len(parts)):
        d = '/'.join(parts[:i])
        if not any(r == d or r.startswith(d + '/') for r in roots):
            return d + '/'
    return sp


def script_languages(root: Path, files: list[str], owner: dict) -> list[dict]:
    """[{kind, files, lines, areas, examples}] for the script languages of lang_census.SCRIPT_BY_EXT among the tracked
    files (fixture projects left out), most lines first: nothing here reads them, so they are always named."""
    found: dict[str, list[tuple[str, int]]] = {}
    for f in files:
        kind = lang_census.SCRIPT_BY_EXT.get(PurePosixPath(f).suffix.lower())
        if not kind or fixture_of(f):
            continue
        try:
            n = sum(1 for x in (root / f).read_text(encoding='utf-8', errors='replace').splitlines() if x.strip())
        except OSError:
            continue
        found.setdefault(kind, []).append((f, n))
    out = [{'kind': k, 'files': len(fs), 'lines': sum(n for _, n in fs),
            'areas': sorted({owner[f] for f, _ in fs if owner.get(f) and is_area_asset(f)}),
            'in_no_area': len([f for f, _ in fs if not (owner.get(f) and is_area_asset(f))]),
            'examples': [f for f, _ in sorted(fs, key=lambda x: (-x[1], x[0]))[:3]]} for k, fs in found.items()]
    return sorted(out, key=lambda s: (-s['lines'], s['kind']))


def gated_rust_tests() -> list[dict]:
    """[{file, tests, gate, detail}] for the Rust tests of the Cargo model that a plain `cargo test` does not run on
    every platform (cargo_project runs_by_default False), or that it cannot decide (None), grouped by file and gate.
    Bench and example targets and doc tests are left out: they are not tests an area counts."""
    acc: dict[tuple[str, str, str], int] = {}
    for c in CARGO.get('crates', []):
        for t in c.get('tests') or []:
            if 'runs_by_default' not in t or t['runs_by_default'] is True:
                continue
            why = t.get('why') or []
            gate = why[0].get('gate') if why else 'undecided'
            if gate in ('target', 'doc'):
                continue
            first = why[0] if why else {}
            detail = (', '.join(first.get('features') or first.get('features_any') or [])
                      or ', '.join(first.get('platforms') or []) and 'runs only on ' + ', '.join(first.get('platforms') or [])
                      or str(first.get('detail') or t.get('why_undecided') or ''))
            acc[(t['file'], gate, detail[:120])] = acc.get((t['file'], gate, detail[:120]), 0) + 1
    return [{'file': f, 'tests': n, 'gate': g, 'detail': d} for (f, g, d), n in sorted(acc.items())]


def coverage_format(text: str) -> str:
    """What a --coverage file that holds no Go coverprofile line looks like, for the warning that it was not read."""
    head = text[:4000].lstrip()
    if text.startswith('SQLite format'):
        return 'a coverage.py data file (.coverage)'
    if head.startswith('{') or head.startswith('['):
        if 'llvm.coverage.json.export' in text[:200000] or 'llvm.coverage.json.export' in text[-4000:]:
            return 'an llvm-cov JSON export (cargo llvm-cov --json)'
        if '"executed_lines"' in text[:200000] or '"percent_covered"' in text[:200000]:
            return 'a coverage.py JSON report'
        return 'a JSON coverage report'
    if head.startswith('<'):
        return 'an XML coverage report (Cobertura, JaCoCo or coverage.py xml)'
    if re.search(r'^(?:TN:|SF:)', text[:4000], re.M):
        return 'an lcov tracefile'
    if head.startswith('mode:'):
        return 'a Go coverprofile of another module (no line maps to a file of this repository)'
    return 'a file in a format this script does not know'


def ui_markup_note(root: Path, files: list[str], owner: dict) -> str | None:
    """Desktop and mobile UI definition files (Slint, QML, XAML, Qt Designer, GTK, Kivy, Interface Builder): neither
    the language census nor the surface patterns read them, so they are named with their areas, never passed over."""
    found: dict[str, list[tuple[str, int]]] = {}
    for f in files:
        kind = UI_MARKUP_EXT.get(PurePosixPath(f).suffix.lower())
        if not kind or is_test(f):
            continue
        try:
            n = sum(1 for x in (root / f).read_text(encoding='utf-8', errors='replace').splitlines() if x.strip())
        except OSError:
            continue
        found.setdefault(kind, []).append((f, n))
    if not found:
        return None
    parts = []
    for kind, fs in sorted(found.items()):
        homes = sorted({owner.get(f) or 'no area' for f, _ in fs})
        parts.append(f'{kind} {len(fs)} file(s), {sum(n for _, n in fs)} lines ({", ".join(f for f, _ in fs[:3])}'
                     + (f' +{len(fs) - 3}' if len(fs) > 3 else '') + f'; area {", ".join(homes)})')
    return ('UI markup these scripts do not measure (no complexity, no surface): ' + '; '.join(parts)
            + '; a UI definition decides what users see and can do, so judge the impact of the areas holding it by hand')


SECOND_LANGUAGE_WARNING = 'a language with a large share of the code is outside every area'


def second_language_warning(root: Path, files: list[str], owner: dict, area_basis: str, skip: set | None = None) -> list[str]:
    """Loud warnings for each language (not only the dominant one) that holds SECOND_LANGUAGE_SHARE or more of the
    code's lines with most of them outside every area: half a product in helper scripts is not root tooling. The plan
    must name the language (validate_test_plan.py checks)."""
    # the catch-all 'unassigned' area scores stray files as one lump: a language mostly there is not in the areas either
    by = code_lines_by_language(root, files, lambda f: None if owner.get(f) == 'unassigned' else owner.get(f))
    total = sum(v[0] + v[1] for v in by.values())
    out = []
    for lang, (out_n, in_n, fs) in sorted(by.items()):
        if lang not in (skip or set()) and total and out_n >= SECOND_LANGUAGE_MIN_LINES and out_n >= SECOND_LANGUAGE_SHARE * total and in_n < out_n:
            dirs = sorted({f.split('/')[0] + '/' if '/' in f else f for f in fs})
            out.append(f'{SECOND_LANGUAGE_WARNING}: {lang} has {out_n} of the {total} code lines ({out_n / total:.0%}) in '
                       f'{len(fs)} file(s) outside every area or only in "unassigned" (area basis: {area_basis}): '
                       f'{", ".join(dirs[:6])}'
                       + (f' (+{len(dirs) - 6})' if len(dirs) > 6 else '') + f'; give --areas that score it, or the plan '
                       f'must name {lang} and say why it needs no testing (headline, an assumption or an open question)')
    return out


CI_FILES = re.compile(r'^(\.github/workflows/[^/]+\.ya?ml|\.gitlab-ci\.yml|azure-pipelines\.ya?ml|Jenkinsfile|'
                      r'\.circleci/config\.ya?ml|bitbucket-pipelines\.yml|\.travis\.yml|\.buildkite/.+|\.drone\.ya?ml)$')
NO_TESTS = 'the repository has no test files'


def no_tests_warning(files: list[str]) -> str | None:
    """A loud warning when no file is a test: test weakness is then 5 for every area and carries no ranking signal."""
    if any(is_test(f) and PurePosixPath(f).suffix in CODE_EXT for f in files):
        return None
    ci = [f for f in files if CI_FILES.match(f)]
    return (f'{NO_TESTS}' + ('' if ci else ' and no CI configuration') + ': test weakness is 5 for every area, so it '
            'ranks nothing; the plan must say so (headline, an assumption or an open question) and start with the tests '
            'the High areas need')


JS_LIKE = {'.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.vue', '.svelte'}
ENTRY_STEMS = {'middleware', 'proxy', 'instrumentation', 'instrumentation-client', 'mdx-components'}


def unreferenced_files(root: Path, files: list[str], areas: list[dict], owner: dict, excluded_paths: set, imports,
                       entries: dict) -> tuple[list[str], str | None]:
    """(files, note): JavaScript/TypeScript source files of a Next.js project that no file in the repository imports and
    that are not entry points by convention (App Router and Pages Router files, metadata routes, middleware,
    instrumentation, package manifest entries, service workers, runtime configuration). A hint of dead code, never a
    score. Skipped, with a note, when so many files look unreferenced that import resolution is likely incomplete."""
    if not NEXT_MODEL.get('projects'):
        return [], None
    code = [f for f in files if PurePosixPath(f).suffix in JS_LIKE and not f.endswith('.d.ts')]
    imported = {t for f in code for t in imports(f)}
    entry_dirs = {d for p in NEXT_MODEL['projects'] for d in (p.get('app_dir'), p.get('pages_dir')) if d}
    workers = {w.get('worker_file') for p in NEXT_MODEL['projects'] for w in p.get('service_workers') or []}
    cands = []
    for f in code:
        p = PurePosixPath(f)
        if (owner.get(f) is None or f in excluded_paths or is_test(f) or not is_area_code(f) or f in imported
                or f in entries or f in workers or f in KEPT_CONFIG or CONFIG_NAME.match(p.name) or p.stem in ENTRY_STEMS
                or any(f.startswith(d + '/') for d in entry_dirs) or dir_names(f) & {'public', 'static', 'scripts'}):
            continue
        cands.append(f)
    pool = [f for f in code if owner.get(f) is not None and not is_test(f) and not any(f.startswith(d + '/') for d in entry_dirs)]
    if cands and len(cands) > max(3, len(pool) // 3):
        return [], (f'the unreferenced-file hint was skipped: {len(cands)} of {len(pool)} source files outside the routing '
                    'directories resolved no importer, so import resolution (aliases, barrels) looks incomplete')
    return sorted(cands), None


def score(args) -> dict:
    root = args.repo.resolve()
    hist = json.loads(args.history.read_text(encoding='utf-8'))
    cx = json.loads(args.complexity.read_text(encoding='utf-8'))['files']
    scope = json.loads(args.scope.read_text(encoding='utf-8')) if args.scope else None
    # history.json lists every tracked file (mine_history adds the ones the window didn't touch, with zero counts)
    files = sorted(p for p in hist['files'] if (root / p).is_file())
    explicit = json.loads(args.areas.read_text(encoding='utf-8'))['areas'] if args.areas else []
    # Sample and fixture projects are left out (an explicit area inside one keeps it), then the tests that are not a
    # test framework are found, then the copy groups (tests and fixtures never form one).
    FIXTURES.clear()
    kept_fixtures = []
    if not getattr(args, 'include_fixtures', False):
        for fx in repo_layout.fixture_projects(root, getattr(args, 'fixture_dir', None) or ()):
            named_in = sorted({a['id'] for a in explicit for p in a['paths']
                               if p.rstrip('/*') == fx['dir'] or p.startswith(fx['dir'] + '/')})
            if named_in:
                kept_fixtures.append(f'{fx["dir"]} (--areas: {", ".join(named_in)})')
            else:
                FIXTURES[fx['dir']] = fx['reason']
    set_test_layout(root, files)
    for x in (COPY_CANON, COPY_REP, COPY_HOW):
        x.clear()
    groups = [] if getattr(args, 'keep_copies', False) else repo_layout.copy_groups(
        root, [f for f in files if not fixture_of(f) and not is_test(f) and not dir_names(f) & DOC_DIRS
               and PurePosixPath(f).suffix.lower() in CODE_EXT | ASSET_EXT])
    for g in groups:
        for m in [g['canonical']] + g['copies']:
            COPY_CANON[m], COPY_HOW[m] = g['canonical'], g['how']
    PRODUCT_DIRS.clear()
    PRODUCT_DIRS.update(product_dirs(root, files))
    set_runner_dirs(root, files)
    set_cargo_model(root, files)
    fileset = set(files)
    NEXT_MODEL.clear()
    NEXT_MODEL.update(next_model(root, files))
    KEPT_CONFIG.clear()
    runtime_cfg = {f: v for f, v in runtime_config_files(NEXT_MODEL, fileset).items() if not fixture_of(f)}
    KEPT_CONFIG.update(runtime_cfg)
    if args.areas:
        # an explicit area that names a configuration file by its path is the user's decision: it is scored
        named = {p for a in explicit for p in a['paths']}
        KEPT_CONFIG.update(f for f in named & fileset if CONFIG_NAME.match(PurePosixPath(f).name)
                           or ('/' not in f and PurePosixPath(f).suffix.lower() in DATA_EXT | {'.toml'}))
    areas, area_basis = define_areas(root, files, args.areas)
    warnings = []
    CI_ASSETS.clear()
    if args.areas:          # a CI workflow an explicit area covers is product behaviour of the repository: scored
        CI_ASSETS.update(f for f in files if CI_FILES.match(f) and area_of(f, areas))
    # each copy group is scored once: on its canonical copy, or on the first copy an area holds when none holds that
    copy_out = []
    for g in groups:
        members = [g['canonical']] + sorted(g['copies'], key=lambda m: (m.count('/'), len(m), m))
        rep_ = next((m for m in members if area_of(m, areas)), g['canonical'])
        for m in members:
            COPY_REP[m] = rep_
        copy_out.append({'canonical': g['canonical'], 'how': g['how'], 'copies': len(g['copies']),
                         'scored_as': rep_, 'area': area_of(rep_, areas),
                         **({'undeclared': g['undeclared']} if g.get('undeclared') else {}),
                         **({'drifted': g['drifted']} if g.get('drifted') else {})})

    def copy_reason(f: str) -> str:
        r = COPY_REP[f]
        return (f'a copy of {COPY_CANON[f]} ({"declared shared file" if COPY_HOW[f] == "declared" else "identical content"});'
                f' scored once, in area {area_of(r, areas) or "none"}' + (f' (as {r})' if r != COPY_CANON[f] else '')
                + '; --keep-copies scores every copy')
    if FIXTURES:
        warnings.append(f'{len(FIXTURES)} sample or fixture project(s) are not scored (their code, tests and changes are '
                        'left out; listed in fixtures_excluded): ' + ', '.join(sorted(FIXTURES)[:6])
                        + (f' (+{len(FIXTURES) - 6})' if len(FIXTURES) > 6 else '')
                        + '; --include-fixtures scores them, --fixture-dir names more')
    if kept_fixtures:
        warnings.append('sample or fixture project(s) scored because --areas names them: ' + ', '.join(kept_fixtures))
    if copy_out:
        warnings.append(f'{len(copy_out)} file(s) are kept identical in {sum(g["copies"] for g in copy_out)} further '
                        'place(s): each is scored once, in the area of its canonical copy, and the copies are listed in '
                        'excluded (copy_groups); --keep-copies scores every copy')
    if NEXT_MODEL.get('error'):
        warnings.append(f'the Next.js model could not be read ({NEXT_MODEL["error"]}): script loaders, next.config headers '
                        'and redirects and service workers are not scored from it')
    excluded = []
    kept_generated: dict[str, list[tuple[str, str]]] = {}
    for f in files:
        sfx = PurePosixPath(f).suffix
        if fixture_of(f) and area_of(f, areas) and sfx.lower() in CODE_EXT | ASSET_EXT | set(UNREAD_EXT) and not is_test(f):
            excluded.append({'path': f, 'reason': fixture_reason(f)})
            continue
        if COPY_REP.get(f, f) != f and area_of(f, areas) and (is_area_code(f) or is_area_asset(f)):
            excluded.append({'path': f, 'reason': copy_reason(f)})
            continue
        kind, why = diff_scope.classify(f, '', False, root)
        if kind == 'generated' and args.areas and area_of(f, areas):
            # an explicit area is the user's decision: its files are scored even when their name or directory looks
            # generated (a committed, hand-written dist/), and a warning says so
            kept_generated.setdefault(area_of(f, areas), []).append((f, why))
        elif kind in ('generated', 'vendored') and area_of(f, areas):
            excluded.append({'path': f, 'reason': why})
        elif PurePosixPath(f).suffix in CODE_EXT and not is_test(f) and not is_area_code(f) and area_of(f, areas):
            excluded.append({'path': f, 'reason': 'tool configuration file' if CONFIG_NAME.match(PurePosixPath(f).name)
                             else bench_reason(f) if is_bench(f) else 'documentation or examples directory'})
    excluded_paths = {e['path'] for e in excluded}
    for aid, kept in sorted(kept_generated.items()):
        warnings.append(f'area {aid}: {len(kept)} file(s) classified generated ({kept[0][1]}) are scored because --areas '
                        f'names them ({", ".join(f for f, _ in kept[:4])}{" ..." if len(kept) > 4 else ""}); check they are '
                        'hand-written source, or take the area out of --areas')
    owner = {f: area_of(f, areas) for f in files}
    # Source files no area covers are never dropped silently: they form the 'unassigned' area, and a warning names them.
    # Empty files (an empty __init__.py) and files outside the main source tree in the default layout are not counted.
    in_tree = (lambda f: True) if args.areas else (lambda f: any(f.startswith(p.rstrip('/*').rsplit('/', 1)[0] + '/')
                                                                   for a in areas for p in a['paths']))
    unassigned = sorted(f for f in files if owner[f] is None and is_source(f) and f not in excluded_paths
                        and COPY_REP.get(f, f) == f
                        and cx.get(f, {}).get('loc', 0) > 0 and in_tree(f))
    if unassigned:
        areas = areas + [{'id': 'unassigned', 'paths': unassigned}]
        for f in unassigned:
            owner[f] = 'unassigned'
        warnings.append(f'{len(unassigned)} source file(s) are in no area and were scored as "unassigned": '
                        + ', '.join(unassigned[:8]) + (f' (+{len(unassigned) - 8})' if len(unassigned) > 8 else '')
                        + '; add them to --areas')
    # Every other code file outside the areas is listed in excluded with its reason, so nothing leaves silently: docs and
    # examples, tool configuration, generated or vendored code, fixtures and migrations outside an area, and (in the
    # default layout) root tooling and scripts outside the workspace packages or the main source tree.
    outside = []
    for f in files:
        p = PurePosixPath(f)
        if (owner[f] is not None or f in excluded_paths or p.suffix not in CODE_EXT or is_test(f) or f.endswith('.d.ts')
                or cx.get(f, {}).get('loc', 0) == 0):
            continue
        kind, why = diff_scope.classify(f, '', False, root)
        names = dir_names(f)
        if fixture_of(f):
            reason = fixture_reason(f)
        elif COPY_REP.get(f, f) != f:
            reason = copy_reason(f)
        elif kind in ('generated', 'vendored'):
            reason = why
        elif f in KEPT_CONFIG:
            reason = RUNTIME_CONFIG_REASON
        elif CONFIG_NAME.match(p.name):
            reason = 'tool configuration file'
        elif is_bench(f):
            reason = bench_reason(f)
        elif names & DOC_DIRS:
            reason = 'documentation or examples directory'
        elif names & NOT_SOURCE_DIRS:
            reason = f'{sorted(names & NOT_SOURCE_DIRS)[0]}/ directory (fixtures, mocks, stories, static files or migrations)'
        else:
            reason = OUTSIDE_REASON
        outside.append({'path': f, 'reason': reason})
    excluded += outside
    # Changed files the scope holds that no scored area counts (in no area, or excluded as generated, vendored, docs or
    # root tooling) count nowhere: name them by directory and reason, with their changed lines, so a release whose
    # content is all outside the areas never reads as "nothing in scope changed".
    # A changed file of a type nothing here reads (a Cargo.toml, a go.mod, a .proto) is still a change of the release:
    # inside a scored area it adds to that area's change in scope (extra_scope, named in the evidence), outside it is
    # listed here. Only documentation, binary, generated and vendored files, lockfiles, dotfiles and media stay out.
    why_excluded = {e['path']: e['reason'] for e in excluded}
    live_ids = {owner[f] for f in files if owner.get(f) and f not in excluded_paths and (is_area_code(f) or is_area_asset(f))}
    known_ext = CODE_EXT | ASSET_EXT | set(UNREAD_EXT)
    extra_scope: dict[str, list[dict]] = {}
    loose: dict[tuple[str, str], list[int]] = {}
    for sf in (scope or {}).get('files', []):
        sp = sf['path']
        if is_test(sp):
            continue
        known = PurePosixPath(sp).suffix.lower() in known_ext
        sp_area = area_of(sp, areas)
        other_reason = None
        if not known:
            kind = sf.get('kind') or diff_scope.classify(sp, '', False, root)[0]
            if (kind in SCOPE_SKIP_KINDS or SCOPE_NOISE.search(sp) or fixture_of(sp)
                    or not (sf.get('lines_added') or 0) + (sf.get('lines_deleted') or 0)):
                continue
            if sp_area in live_ids and sp not in why_excluded:
                extra_scope.setdefault(sp_area, []).append(sf)
                continue
            other_reason = ('a build or configuration file' if kind == 'config' else 'a file of a type these scripts do '
                            'not read') + (f' in the unscored area {sp_area}' if sp_area else ' in no area')
        elif sp_area is not None and sp not in why_excluded:
            if sp not in fileset or ((is_area_code(sp) or is_area_asset(sp)) and sp_area in live_ids):
                continue                   # counted by its area (a file the scope deleted counts through gone_scope)
            if sp_area in live_ids and not dir_names(sp) & DOC_DIRS:
                # a manifest or configuration file inside an area (package.json, tsconfig.json): its lines are part
                # of the area's change, though the area's other factors do not read it
                if not SCOPE_NOISE.search(sp) and not fixture_of(sp):
                    extra_scope.setdefault(sp_area, []).append(sf)
                continue
            if sp_area in live_ids:
                continue                   # documentation or examples inside an area
            other_reason = f'in the unscored area {sp_area}'
        if COPY_REP.get(sp, sp) != sp and area_of(COPY_REP[sp], areas):
            continue                       # a copy's change counts once, in the area of the copy that is scored
        top = outside_dir(sp, areas)
        reason = why_excluded.get(sp) or (fixture_reason(sp) if fixture_of(sp)
                                          else 'documentation or examples directory' if dir_names(sp) & DOC_DIRS
                                          else other_reason if other_reason
                                          else f'{unread_kind(sp)} these scripts do not read, in no area' if unread_kind(sp)
                                          else 'a template, style or data file in no area' if is_area_asset(sp)
                                          else 'in no area')
        if sf.get('status') == 'deleted' and sp not in fileset:
            reason = f'deleted in the scope; {reason}'
        acc = loose.setdefault((top, reason), [0, 0])
        acc[0] += 1
        acc[1] += (sf.get('lines_added') or 0) + (sf.get('lines_deleted') or 0)
    scope_outside = [{'path': d, 'reason': r, 'files': n, 'lines': ln}
                     for (d, r), (n, ln) in sorted(loose.items(), key=lambda x: (-x[1][1], x[0]))]
    if scope_outside:
        warnings.append(f'{sum(x["lines"] for x in scope_outside)} changed line(s) in {sum(x["files"] for x in scope_outside)} '
                        'file(s) of the scope are in no scored area and count nowhere: '
                        + ', '.join(f'{x["path"]} ({x["files"]} file(s), {x["lines"]} lines; {x["reason"]})' for x in scope_outside[:6])
                        + '; no area\'s change in scope reflects them, so add them to --areas (or say in the plan why they '
                        'need no testing) and lower the plan\'s confidence until they are scored')
    # Deleted code is removed behaviour (a redirect, a guard, a handler): the scope's deletions are named wherever they
    # count, so a release that removes middleware never reads as a small change.
    gone_scope = [sf for sf in (scope or {}).get('files', []) if sf.get('status') == 'deleted' and sf['path'] not in fileset
                  and not is_test(sf['path']) and PurePosixPath(sf['path']).suffix.lower() in known_ext]
    if gone_scope:
        gone_scope.sort(key=lambda sf: (-(sf.get('lines_deleted') or 0), sf['path']))
        warnings.append(f'{len(gone_scope)} file(s) deleted in the scope ({sum(sf.get("lines_deleted") or 0 for sf in gone_scope)} '
                        'lines removed): ' + ', '.join(f'{sf["path"]} ({sf.get("lines_deleted") or 0} lines, '
                                                       f'{area_of(sf["path"], areas) or "no area"})' for sf in gone_scope[:6])
                        + (f' (+{len(gone_scope) - 6})' if len(gone_scope) > 6 else '') + '; deleted code is removed '
                        'behaviour: plan a regression check that what it did (redirects, guards, handlers) still happens '
                        'or is meant to stop')
    public_code = [e['path'] for e in outside if dir_names(e['path']) & {'public', 'static'}]
    if public_code:
        warnings.append(f'{len(public_code)} code file(s) in public/ or static/ are served to browsers but not scored: '
                        + ', '.join(public_code[:6]) + (f' (+{len(public_code) - 6})' if len(public_code) > 6 else '')
                        + '; a script a page loads (a worker, a widget) is product code, so add it to --areas if it is one')
    tooling = [e['path'] for e in outside if e['reason'] == OUTSIDE_REASON]
    if tooling:
        warnings.append(f'{len(tooling)} code file(s) outside the workspace packages and the main source tree are not scored '
                        '(listed in excluded): ' + ', '.join(tooling[:8]) + (f' (+{len(tooling) - 8})' if len(tooling) > 8 else '')
                        + '; root tooling and scripts decide what ships, so add them to --areas if they matter')
    # Loud fail: when most of the repository's code (by language) is outside every area, the plan would rank incidental
    # files. Say which language and how much, so a plausible-looking plan is not built on the wrong code.
    lost = uncovered_language(files, owner, cx, root, area_basis)
    if lost:
        warnings.append(lost)
    said = re.search(r' (\.\w+) file\(s\)', lost or '')        # the language uncovered_language already named
    warnings += second_language_warning(root, files, owner, area_basis,
                                        {lang_census.LANG_BY_EXT.get(said.group(1))} if said else set())
    lang_cov, note = language_note(root)
    if note:
        warnings.append(note)
    markup = ui_markup_note(root, files, owner)
    if markup:
        warnings.append(markup)
    scripts = script_languages(root, files, owner)
    if scripts:
        warnings.append(
            f'{SCRIPT_LANGUAGE_WARNING} (no complexity, no surface, no link to tests): '
            + '; '.join(f'{s["kind"]} {s["files"]} file(s), {s["lines"]} lines ({", ".join(s["examples"])}; '
                        + (f'area {", ".join(s["areas"])}' if s['areas'] else 'no area')
                        + (f', {s["in_no_area"]} in no area' if s['areas'] and s['in_no_area'] else '') + ')' for s in scripts)
            + '; an installer, packaging or release script is shipped code: where an area holds it, it counts for churn, '
            'defects and change in scope only, and the plan must name each kind and say how it is tested or why it '
            'needs no testing (headline, an assumption or an open question)')
    inputs = [load_input(k, Path(p)) for k, p in (x.split('=', 1) for x in args.input)]
    names = [(i['skill'], i['path']) for i in inputs]
    if len(set(names)) != len(names):
        raise SystemExit('two --input reports of the same kind have the same file name; give them distinct names, since '
                         'each carried finding records the report it came from by name')
    # --finding-area KIND:ID=AREA: the source report points the finding at the wrong file (say so in the plan's
    # assumptions); the finding records the area its files gave as reassigned_from
    reassign: dict[tuple[str, str], str] = {}
    for spec in args.finding_area:
        m = re.match(r'^([^:=]+):([^=]+)=(.+)$', spec)
        if not m:
            raise SystemExit(f'--finding-area {spec!r}: expected KIND:ID=AREA')
        if m.group(3) not in {a['id'] for a in areas}:
            raise SystemExit(f'--finding-area {spec!r}: {m.group(3)!r} is not an area')
        if not any(i['skill'] == m.group(1) and any(it['id'] == m.group(2) for it in i['items']) for i in inputs):
            raise SystemExit(f'--finding-area {spec!r}: no {m.group(1)} finding {m.group(2)} in the --input reports')
        reassign[(m.group(1), m.group(2))] = m.group(3)
        warnings.append(f'{m.group(1)} {m.group(2)} was moved to area {m.group(3)} by --finding-area (its report cites '
                        'another file); say why in the plan\'s assumptions')
    for inp in inputs:
        for it in inp['items']:
            if inp['skill'] != 'flaky' and it['severity'] == 'unknown':
                warnings.append(f'{inp["skill"]} {it["id"]}: {it["source_rating"]}; its severity is unknown and does not set '
                                f'the High floor')
    flaky_files = {i['test_file'] for inp in inputs if inp['skill'] == 'flaky' for i in inp['items']}
    imports = Imports(root, files)
    # Hub modules: a root module that creates the web app imports every module it wires up (some lazily, inside a
    # function), and so does any module whose imports span most areas. Following one hop of imports out of a hub would
    # hand every journey that cites it to every area, so journeys reach a hub's imports only by citing them.
    hubs = {}
    for f, why in root_app_modules(root, files).items():
        hubs[f] = why
    if len(areas) >= 3:
        for f in files:
            if f in hubs or PurePosixPath(f).suffix not in CODE_EXT or is_test(f):
                continue
            reached = {area_of(g, areas) for g in imports(f)} - {None, area_of(f, areas)}
            if len(reached) > max(2, len(areas) // 2):
                hubs[f] = f'imports {len(reached)} of {len(areas)} areas'
    journeys, jscope = journey_links(args.journeys, files, imports, set(hubs))
    if journeys and hubs:
        skipped = sorted(h for h in hubs if any(h in j['files'] for j in journeys))
        if skipped:
            warnings.append('journey links do not follow imports out of hub module(s) '
                            + ', '.join(f'{h} ({hubs[h]})' for h in skipped[:4])
                            + ': a module a hub imports is linked to a journey only when the journey cites it')
    scale, scale_name = priority_scale(journeys)
    have_map = jscope['present']
    usage = json.loads(args.usage.read_text(encoding='utf-8')) if args.usage else None
    bugs = []
    if args.defects:
        data = json.loads(args.defects.read_text(encoding='utf-8'))
        bugs = [t for t in data.get('tickets', [data]) if (t.get('type') or '').lower() in ('bug', 'defect')]
    tests, flaky_skipped, tests_not_counted = tests_by_area(files, areas, flaky_files, imports)
    unresolved_loads = [(t, u) for t in sorted(imports.load_unresolved) for u in imports.load_unresolved[t]]
    if unresolved_loads:
        warnings.append(f'{len(unresolved_loads)} load(s) by file path in {len(imports.load_unresolved)} test file(s) could '
                        'not be worked out, so those tests are linked to no area through them and test weakness may read '
                        'too high there: '
                        + ', '.join(f'{t}:{u.get("line")} ({u.get("via")} {str(u.get("expr"))[:50]})' for t, u in unresolved_loads[:6])
                        + (f' (+{len(unresolved_loads) - 6})' if len(unresolved_loads) > 6 else '')
                        + '; judge test weakness by hand for the areas those scripts are in')
    gated = gated_rust_tests()
    coverage: dict[str, tuple[int, int]] = {}
    coverage_not_read = []
    for prof in args.coverage:
        got = read_coverage(root, prof)
        if not got:
            looks = coverage_format(prof.read_text(encoding='utf-8', errors='replace'))
            coverage_not_read.append({'file': prof.name, 'looks_like': looks})
            warnings.append(f'--coverage {prof.name}: NOT READ. It looks like {looks}; --coverage reads only a Go coverprofile '
                            '(go test -coverprofile=coverage.out ./...) of this repository\'s module. Test weakness is the '
                            'test-size ratio for every area, not this coverage: say in the plan that the coverage figure '
                            'was not used, or judge test weakness from it by hand with a reason')
        for k, (n, c) in got.items():
            o = coverage.get(k, (0, 0))
            coverage[k] = (max(o[0], n), max(o[1], c))
    entries = manifest_entries(root, files)
    next_hits = next_surface_hits(NEXT_MODEL)
    for f, hs in cargo_surface_hits().items():     # clap CLIs, process launches and library loads the Cargo model reads
        next_hits.setdefault(f, []).extend(hs)
    if CARGO.get('error'):
        warnings.append(f'the Cargo manifests could not be read ({CARGO["error"]}): test modules, benches, clap CLIs, '
                        'process launches and library loads are not taken from the Cargo model')
    release = json.loads(args.release.read_text(encoding='utf-8')) if args.release else None
    rel_pkgs = {n: p for n, p in ((release or {}).get('packages') or {}).items() if isinstance(p, dict) and p.get('dir') is not None}

    def package_of(f: str) -> str | None:
        """The release package whose directory holds f (the deepest, so packages/x/y beats packages/x)."""
        best = None
        for n, p in rel_pkgs.items():
            d = p['dir'].rstrip('/')
            if (d in ('', '.') or f.startswith(d + '/')) and (best is None or len(d) > best[1]):
                best = (n, len(d))
        return best[0] if best else None
    aof = lambda x: owner.get(x) or area_of(x, areas)  # noqa: E731
    for j in journeys:
        j['areas'] = {}
        for direct in (True, False):                   # say how a journey reaches an area: direct links before imports
            for x in j['files']:
                aid = aof(x)
                if aid and direct != j['how'][x].startswith('imported by'):
                    j['areas'].setdefault(aid, f'{j["how"][x]} ({x})' if not j['how'][x].startswith('imported by')
                                          else f'{x} {j["how"][x]}')
    scope_files = {f['path']: f for f in (scope or {}).get('files', [])}
    window = hist['window']
    cmd = hist['command']
    commits = {c['sha']: c for c in hist['commits']}
    # Files gone from HEAD keep their history and their scope deletions in the area whose paths cover them: a file
    # deleted before HEAD keeps its churn and fix commits, and a deletion in the scope counts as lines changed.
    gone_hist: dict[str, list[str]] = {}
    for p in hist['files']:
        if p not in fileset and not is_test(p) and (is_area_code(p) or is_area_asset(p)) and area_of(p, areas):
            gone_hist.setdefault(area_of(p, areas), []).append(p)
    gone_in_scope: dict[str, list[dict]] = {}
    for sf in gone_scope:
        if area_of(sf['path'], areas):
            gone_in_scope.setdefault(area_of(sf['path'], areas), []).append(sf)
    raw = {}
    for a in areas:
        mine = [f for f in files if owner[f] == a['id'] and is_area_code(f) and f not in excluded_paths]
        assets = [f for f in files if owner[f] == a['id'] and is_area_asset(f) and f not in excluded_paths]
        every = mine + assets                      # history and scope count templates, styles and data too
        gone_h, gone_s = gone_hist.get(a['id'], []), gone_in_scope.get(a['id'], [])
        mine_set = set(every) | set(gone_h)
        h = [hist['files'][f] for f in every + gone_h]
        added_in_scope = [f for f in every if scope_files.get(f, {}).get('status') == 'added']
        novel = bool(every) and len(added_in_scope) == len(every)
        inline = {f: RUST_INLINE_TESTS[f][1] for f in mine if f in RUST_INLINE_TESTS}   # #[cfg(test)] module lines
        selft = {f: SELF_TESTS[f][1] for f in mine if f in SELF_TESTS}                  # self-test code lines
        for f, n in selft.items():
            inline[f] = inline.get(f, 0) + n
        loc = sum(max(0, cx.get(f, {}).get('loc', 0) - inline.get(f, 0)) for f in mine)
        branches = sum(cx.get(f, {}).get('branches', 0) for f in mine)
        code_files = [f for f in mine if cx.get(f, {}).get('loc', 0) > 0]
        test_loc = sum(RUST_INLINE_TESTS[t][1] if t in RUST_INLINE_TESTS
                       else SELF_TESTS[t][1] if t in SELF_TESTS and not is_test(t)
                       else cx.get(t, {}).get('loc', 0) for t in tests[a['id']])
        scope_lines = sum(scope_files[f]['lines_added'] + scope_files[f]['lines_deleted'] for f in every if f in scope_files)
        scope_lines += sum((sf.get('lines_added') or 0) + (sf.get('lines_deleted') or 0) for sf in gone_s)
        extra_s = extra_scope.get(a['id'], [])     # changed manifests and other files of the area nothing here reads
        scope_lines += sum((sf.get('lines_added') or 0) + (sf.get('lines_deleted') or 0) for sf in extra_s)
        fix_shas = sorted({s for x in h for s in x['fix_commits']})
        commit_keys = {k for s in fix_shas if s in commits for k in commits[s]['keys']}
        area_bugs = [t for t in bugs if a['id'].lower() in {x.lower() for x in (t.get('components') or []) + (t.get('labels') or [])}
                     and t['key'] not in commit_keys]
        raw[a['id']] = {
            'files': mine, 'assets': assets, 'novel': novel, 'loc': loc, 'branches': branches,
            'churn': sum(x['lines_added'] + x['lines_deleted'] for x in h),
            'commits': len({c['sha'] for c in hist['commits'] if not c['formatting'] and any(fl['path'] in mine_set for fl in c['files'])}),
            'authors': sorted({au for x in h for au in x['authors']}),
            'fixes': fix_shas, 'fix_subjects': [f'{s} {commits[s]["subject"]}' for s in fix_shas if s in commits],
            'keys': sorted(commit_keys),
            'bugs': [f'{t["key"]} ({t.get("type")}, {t.get("status")}) {(t.get("summary") or "")[:80]}' for t in area_bugs],
            'branch_density': round(branches / len(code_files), 2) if code_files else 0.0,
            'scope_lines': scope_lines, 'tests': tests[a['id']], 'test_loc': test_loc, 'code_files': code_files,
            'packages': sorted({package_of(x) for x in mine} - {None}),
            'breaking_commits': [c for c in (release or {}).get('breaking_commits') or [] if set(c.get('files') or []) & mine_set],
            'gone_history': gone_h, 'gone_scope': gone_s, 'extra_scope': extra_s,
            # only data records (regenerated results, fixtures, tables): their line counts are not comparable with code
            'data_only': bool(assets) and not code_files and all(
                PurePosixPath(x).suffix.lower() in DATA_EXT and x not in CI_ASSETS for x in assets),
            'inline_tests': {f: n for f, n in inline.items() if f in RUST_INLINE_TESTS}, 'self_tests': selft,
        }
    # A package with a breaking pending changeset (major, or a "Breaking" note) flags the areas of it the scope changed;
    # when the scope changed none of them (or there is no scope), every area of the package.
    changed_pkgs = {n for r in raw.values() if r['scope_lines'] for n in r['packages']}
    live = [k for k in raw if raw[k]['files'] or raw[k]['assets']]
    coded = [k for k in live if raw[k]['code_files']]
    vals = lambda key, only=None, among=None: [raw[k][key] for k in (among or live) if (only is None or only(k))]  # noqa: E731
    # Line counts are ranked among the areas that hold code or hand-written assets: a folder of regenerated data records
    # is ranked against them but is not part of their pool, so its thousands of lines never lower a code area's score.
    line_pool = lambda key, me, only=None: [raw[k][key] for k in live  # noqa: E731
                                            if (k == me or not raw[k]['data_only']) and (only is None or only(k))]
    hist_ok = not hist['shallow']
    out_areas, unscored = [], []
    for a in areas:
        r = raw[a['id']]
        if not r['files'] and not r['assets']:
            under = [f for f in files if area_of(f, [a]) == a['id']]
            gone = [f for f in under if f in excluded_paths]
            reason = (f'all {len(gone)} file(s) under its paths are excluded ({why_excluded.get(gone[0], "excluded")})' if gone
                      else f'{len(under)} file(s) under its paths, none of them code, templates, styles or data' if under
                      else 'its paths match no file in the repository')
            unscored.append({'id': a['id'], 'paths': a['paths'], 'reason': reason})
            continue
        no_code = not r['code_files']
        unread = unread_words(r['assets'])
        n_unread = len([x for x in r['assets'] if unread_kind(x)])
        NO_CODE = ((f'no code files: {len(r["assets"])} template, style or data file(s) only' if not unread
                    else f'no code these scripts read: {n_unread} file(s) they do not read ({unread})'
                    + (f' and {len(r["assets"]) - n_unread} template, style or data file(s)' if len(r['assets']) > n_unread else ''))
                   if r['assets'] else 'no code with lines')
        capped = {}
        f = {}
        if not hist_ok:
            na = {'score': None, 'status': 'not_assessed', 'evidence': ['shallow clone: fetch more history (git fetch --unshallow)']}
            f['churn'] = dict(na)
            f['defects'] = dict(na)
        elif r['novel']:
            f['churn'] = {'score': None, 'status': 'not_assessed', 'evidence': ['no history before the scope: every file is new']}
            f['defects'] = {'score': None, 'status': 'not_assessed', 'evidence': ['no history before the scope: every file is new']}
        else:
            hv = lambda key: line_pool(key, a['id'], lambda k: not raw[k]['novel'])  # noqa: E731
            churn_pct = pct_score(r['churn'], hv('churn'))
            if r['data_only'] and churn_pct > DATA_ONLY_CAP:
                capped['churn'] = churn_pct
            f['churn'] = {'score': min(churn_pct, DATA_ONLY_CAP) if r['data_only'] else churn_pct, 'status': 'assessed', 'command': cmd,
                          'evidence': [f'{r["churn"]} lines changed in {r["commits"]} commits by {len(r["authors"])} author(s), '
                                       f'{window["from"]} to {window["to"]} (formatting-only commits excluded)'
                                       + (f'; including {len(r["gone_history"])} file(s) no longer at HEAD '
                                          f'({", ".join(r["gone_history"][:3])}{" ..." if len(r["gone_history"]) > 3 else ""})'
                                          if r['gone_history'] else '')]}
            n_def = lambda k: len(raw[k]['fixes']) + len(raw[k]['bugs'])  # noqa: E731
            f['defects'] = {'score': pct_score(n_def(a['id']), [n_def(k) for k in live if not raw[k]['novel']]),
                            'status': 'assessed', 'command': cmd,
                            'evidence': [f'{len(r["fixes"])} fix commit(s) in the window'
                                         + (f' and {len(r["bugs"])} bug ticket(s) by component or label' if r['bugs'] else '')]
                                        + r['fix_subjects'][:8] + r['bugs'][:5] +
                                        ([f'linked issues: {", ".join(r["keys"])}'] if r['keys'] else [])}
        if no_code and r['assets']:
            f['complexity'] = {'score': None, 'status': 'not_assessed', 'evidence': [NO_CODE + '; complexity reads code']}
        else:
            f['complexity'] = {'score': pct_score(r['branch_density'], vals('branch_density', among=coded or live)),
                               'status': 'assessed',
                               'evidence': [f'{r["branches"]} branches in {len(r["code_files"])} file(s), {r["branch_density"]} per file; '
                                            f'{r["loc"]} lines of code (complexity.py)']}
        if scope is not None:
            pct = pct_score(r['scope_lines'], line_pool('scope_lines', a['id']))
            cap = scope_cap(r['scope_lines'])
            if r['data_only'] and min(pct, cap) > DATA_ONLY_CAP:
                capped['change_in_scope'] = min(pct, cap)
                cap = DATA_ONLY_CAP
            sl = lambda x: scope_files[x]['lines_added'] + scope_files[x]['lines_deleted']  # noqa: E731
            in_unread = [x for x in r['assets'] if x in scope_files and unread_kind(x)]
            in_assets = sum(sl(x) for x in r['assets'] if x in scope_files and not unread_kind(x))
            f['change_in_scope'] = {'score': min(pct, cap), 'status': 'assessed',
                                    'command': (scope.get('source') or {}).get('command') or 'diff_scope.py',
                                    'evidence': [f'{r["scope_lines"]} lines changed in the scope diff'
                                                 + (f' ({in_assets} in templates, styles or data)' if in_assets else '')
                                                 + (f' ({sum(sl(x) for x in in_unread)} in {len(in_unread)} file(s) these '
                                                    f'scripts do not read: '
                                                    + ', '.join(f'{unread_kind(x)} {x}' for x in in_unread[:3])
                                                    + (f' +{len(in_unread) - 3}' if len(in_unread) > 3 else '') + ')'
                                                    if in_unread else '')
                                                 + (f' ({sum((sf.get("lines_added") or 0) + (sf.get("lines_deleted") or 0) for sf in r["extra_scope"])}'
                                                    f' in {len(r["extra_scope"])} manifest, build or other file(s) of the area: '
                                                    + ', '.join(sf['path'] for sf in r['extra_scope'][:3])
                                                    + (f' +{len(r["extra_scope"]) - 3}' if len(r['extra_scope']) > 3 else '') + ')'
                                                    if r['extra_scope'] else '')
                                                 + (f' ({sum(sf.get("lines_deleted") or 0 for sf in r["gone_scope"])} in '
                                                    f'{len(r["gone_scope"])} deleted file(s): '
                                                    f'{", ".join(sf["path"] for sf in r["gone_scope"][:3])})' if r['gone_scope'] else '')
                                                 + (f' (percentile {pct}, capped at {cap} for a change this size)'
                                                    if cap < pct and 'change_in_scope' not in capped else '')]}
        else:
            f['change_in_scope'] = {'score': None, 'status': 'not_assessed', 'evidence': ['no scope diff given']}
        breaking = False
        for n in r['packages']:
            p = rel_pkgs[n]
            if not p.get('bump') and not p.get('breaking'):
                continue
            brk = p.get('breaking_changesets') or []
            files_cs = brk + [c for c in p.get('changesets') or [] if c not in brk]     # the breaking ones first
            f['change_in_scope']['evidence'].append(
                f'pending changeset(s) for {n}: {p.get("bump") or "no bump"}' + (' (breaking)' if p.get('breaking') else '')
                + (f' ({", ".join(files_cs[:3])}' + (f' +{len(files_cs) - 3}' if len(files_cs) > 3 else '') + ')' if files_cs else ''))
            if p.get('breaking') and (r['scope_lines'] or n not in changed_pkgs):
                breaking = True
        for c in r['breaking_commits'][:5]:
            f['change_in_scope']['evidence'].append(f'breaking commit {c.get("sha", "")[:12]} {c.get("subject", "")[:100]}')
            breaking = True
        if no_code and r['assets']:
            tw, tev = None, [NO_CODE + '; its tests are those of the code that renders or loads it']
        elif not r['tests']:
            tw, tev = 5, ['no test imports this area' + (f' ({len(flaky_skipped)} flaky test file(s) not counted)' if flaky_skipped else '')]
        else:
            ratio = r['test_loc'] / r['loc'] if r['loc'] else 1
            tw = 4 if ratio < 0.2 else 3 if ratio < 0.5 else 2 if ratio < 1 else 1
            by_path = sorted(PATH_LOADS.get(a['id'], set()) & set(r['tests']))
            mine_gated = [g for g in gated if g['file'] in set(r['tests'])]
            for g in mine_gated:
                g['area'] = a['id']
            n_rust = sum(1 for c in CARGO.get('crates', []) for t in c.get('tests') or [] if t.get('file') in set(r['tests']))
            tev = [f'{len(r["tests"])} test file(s) import it ({", ".join(r["tests"][:4])}); {r["test_loc"]} test lines for '
                   f'{r["loc"]} lines of code (ratio {ratio:.2f})'
                   + (f'; {len(r["inline_tests"])} source file(s) hold #[cfg(test)] modules '
                      f'({sum(r["inline_tests"].values())} lines, counted as test lines, not code)' if r['inline_tests'] else '')
                   + (f'; {len(r["self_tests"])} script(s) carry their own self-test ({sum(r["self_tests"].values())} '
                      'lines, counted as test lines, not code)' if r['self_tests'] else '')
                   + (f'; {len(by_path)} of them load its scripts by file path, not by import '
                      f'({", ".join(by_path[:3])})' if by_path else '')
                   + (f'; {sum(g["tests"] for g in mine_gated)} of its {n_rust} Rust test(s) do not run under a default '
                      '`cargo test` on every platform ('
                      + '; '.join(f'{g["tests"]} in {PurePosixPath(g["file"]).name}: {g["gate"]}'
                                  + (f' {g["detail"]}' if g['detail'] else '') for g in mine_gated[:4])
                      + (f' +{len(mine_gated) - 4}' if len(mine_gated) > 4 else '')
                      + '): their lines count here, so check which CI job runs them before trusting this score'
                      if mine_gated else '')]
        cov_files = [x for x in r['files'] if x in coverage]
        stmts = sum(coverage[x][0] for x in cov_files)
        if stmts:                         # a coverage profile measures it: statement coverage, not the size ratio
            covered = sum(coverage[x][1] for x in cov_files)
            tw = coverage_score(covered / stmts)
            tev = [f'statement coverage {covered / stmts:.1%} ({covered} of {stmts} statements in {len(cov_files)} file(s), '
                   f'{", ".join(p.name for p in args.coverage)})'
                   + (f'; {len(r["code_files"]) - len(cov_files)} code file(s) of the area are not in the profile'
                      if len(r['code_files']) > len(cov_files) else '')] + tev
            f['test_weakness'] = {'score': tw, 'status': 'assessed', 'command': 'go test -coverprofile', 'evidence': tev}
        else:
            f['test_weakness'] = {'score': tw, 'status': 'assessed' if tw is not None else 'not_assessed', 'evidence': tev}
        linked = [j for j in journeys if a['id'] in j['areas']]
        # A partial journey map (run scoped to some domains, or items declared out_of_scope) says nothing about the
        # areas it didn't map: those are not_assessed, not "no journey". An area the map did reach stays assessed.
        unmapped = have_map and not linked and jscope['partial'] and not (set(r['files']) & jscope['mapped_files'])
        partial_note = (f'journey map is partial (domains: {", ".join(jscope["domains"]) if isinstance(jscope["domains"], list) else jscope["domains"]}'
                        f'; {len(jscope["out_of_scope_files"])} file(s) out of scope or not mapped this run) and does not map this area')
        if have_map and not unmapped:
            best = max([scale.get(j['priority'], 1) for j in linked], default=1)
            f['journey_criticality'] = {'score': best, 'status': 'assessed',
                                        'evidence': ([f'{j["id"]} ({j["priority"]}) {j["title"]} - {j["areas"][a["id"]]}' for j in linked]
                                                     + ([f'priority scale: {scale_name}'] if scale is JOURNEY_MAPPER_SCORE else [])) if linked else
                                                    ['journey map present; no journey cites this area']}
        elif unmapped:
            f['journey_criticality'] = {'score': None, 'status': 'not_assessed', 'evidence': [partial_note]}
        else:
            f['journey_criticality'] = {'score': None, 'status': 'not_assessed', 'evidence': ['no journey map: run journey-mapper']}
        if no_code and r['assets']:
            classes = []
            f['surface'] = {'score': None, 'status': 'not_assessed', 'evidence': [NO_CODE + '; surface patterns read code']}
        else:
            s, classes, sev = surface_scan(root, r['files'], entries, next_hits)
            f['surface'] = {'score': s, 'status': 'assessed', 'evidence': sev}
        if usage and a['id'] in usage.get('areas', {}):
            share = usage['areas'][a['id']]
            f['reach'] = {'score': 5 if share >= 0.5 else 4 if share >= 0.25 else 3 if share >= 0.1 else 2 if share >= 0.02 else 1,
                          'status': 'assessed', 'evidence': [f'{share:.0%} of traffic ({args.usage.name})']}
        elif unmapped:
            f['reach'] = {'score': None, 'status': 'not_assessed', 'evidence': [partial_note + '; no usage export']}
        elif have_map:
            counts = {k: len([j for j in journeys if k in j['areas']]) for k in live}
            f['reach'] = {'score': pct_score(counts[a['id']], list(counts.values())), 'status': 'assessed',
                          'evidence': [f'cited by {counts[a["id"]]} of {len(journeys)} journeys (journey graph)']}
        else:
            f['reach'] = {'score': None, 'status': 'not_assessed', 'evidence': ['no usage export and no journey map']}
        findings = []
        for inp in inputs:
            for it in inp['items']:
                if inp['skill'] == 'flaky':
                    continue
                moved = reassign.get((inp['skill'], it['id']))
                home = moved if moved else ({aof(x) for x in it['files']} if it['files'] else set())
                if (moved and moved == a['id']) or (not moved and a['id'] in home):
                    fnd = {'skill': inp['skill'], 'id': it['id'], 'title': it['title'], 'severity': it['severity'],
                           'source_rating': it['source_rating'], 'confidence': it['confidence'], 'source': inp['path']}
                    if moved:
                        fnd['reassigned_from'] = sorted(x for x in {aof(x) for x in it['files']} if x) or None
                    findings.append(fnd)
        for k, was in capped.items():
            f[k]['evidence'][0] += (f' (percentile {was}, capped at {DATA_ONLY_CAP} for a data-only area: line counts of '
                                    'data records, often regenerated by a tool, are not comparable with code changes)')
        flags = (['novel'] if r['novel'] else []) + (['breaking_change'] if breaking else []) + finding_flags(findings)
        area = {'id': a['id'], 'paths': a['paths'], 'files': len(r['files']),
                **({'asset_files': len(r['assets'])} if r['assets'] else {}),
                'journeys': [j['id'] for j in linked], 'surfaces': classes, 'flags': flags, 'open_findings': findings,
                'tests': r['tests'], 'factors': {k: f[k] for k in list(LIKELIHOOD) + list(IMPACT)}}
        area.update(recompute(area))
        out_areas.append(area)
    # A data-only area has no complexity and no test evidence, so its likelihood is its line counts and fix commits
    # alone: it is never ranked above every area that holds code. Its line-count factors are lowered until it is not.
    code_lik = [x['likelihood'] for x in out_areas if raw[x['id']]['code_files'] and x['likelihood'] is not None]
    if code_lik:
        for x in out_areas:
            if not raw[x['id']]['data_only']:
                continue
            lowered = {}
            while x['likelihood'] is not None and x['likelihood'] > max(code_lik):
                cand = [k for k in ('churn', 'change_in_scope') if (x['factors'][k]['score'] or 0) > 1]
                if not cand:
                    break
                k = max(cand, key=lambda k: x['factors'][k]['score'])
                lowered.setdefault(k, x['factors'][k]['score'])
                x['factors'][k]['score'] -= 1
                x.update(recompute(x))
            for k, was in lowered.items():
                x['factors'][k]['evidence'][0] += (f' (lowered from {was} to {x["factors"][k]["score"]}: a data-only area is '
                                                   f'not ranked above every code area on likelihood; the highest is '
                                                   f'{max(code_lik)})')
    data_only = [x['id'] for x in out_areas if raw[x['id']]['data_only']]
    if data_only:
        warnings.append(f'{len(data_only)} area(s) hold only data records ({", ".join(data_only[:6])}): their churn and '
                        f'change in scope are capped at {DATA_ONLY_CAP} and never put them above every code area, and they '
                        'are left out of the pool the code areas are ranked in; if the records are hand-maintained '
                        'product data (prices, rules, translations), judge those factors up with a reason')
    gated_out = [g for g in gated if g.get('area')]
    if gated_out:
        warnings.append(f'{sum(g["tests"] for g in gated_out)} Rust test(s) in {len({g["file"] for g in gated_out})} file(s) do '
                        'not run under a default `cargo test` on every platform (a non-default feature, required-features, '
                        '#[ignore] or a platform cfg; listed in tests_not_run_by_default): they still count as test lines '
                        'of their areas, so check which CI job runs them and judge test weakness up where none does')
    out_areas.sort(key=lambda a: (-(a['risk'] or 0), a['id']))
    ui = [a['id'] for a in out_areas if any(PurePosixPath(f).suffix in UI_EXT for f in raw[a['id']]['files'])]
    if ui and not have_map and not usage:
        warnings.append(f'no journey map: the impact of {len(ui)} UI area(s) ({", ".join(ui[:6])}'
                        + (f' +{len(ui) - 6}' if len(ui) > 6 else '') + ') rests on surface patterns alone, so a page that '
                        'drives an important flow can rank Low; run journey-mapper (or give --usage) before trusting the UI '
                        'ranking, and say so in the plan')
    blind = [a['id'] for a in out_areas if a['impact'] is None]
    blind_unread = any(unread_kind(x) for b in blind for x in raw[b]['assets'])
    if blind:
        warnings.append(f'{len(blind)} area(s) hold only templates, styles or data'
                        + (' or files these scripts do not read' if blind_unread else '') + ' and have no impact evidence (surface '
                        f'patterns read code): {", ".join(blind[:6])}; their band is Low only for want of evidence, so give '
                        'a journey map or --usage, or judge their impact in the plan')
    for u in unscored:
        if args.areas or u['id'] != 'unassigned':
            warnings.append(f'area {u["id"]} is not scored: {u["reason"]}; it is listed in unscored_areas (no file to score '
                            'is not low risk: fix its paths or say in the plan why it is out of scope)')
    no_tests = no_tests_warning([f for f in files if not fixture_of(f)])
    if no_tests:
        warnings.append(no_tests)
    scored = lambda p: (area_of(p, areas) is not None and p not in excluded_paths  # noqa: E731
                        and (is_area_code(p) or is_area_asset(p)))
    stray = [c for c in hist['commits'] if c.get('fix') and (not c.get('merge') or c.get('merge_own_changes')) and c['files']
             and not any(scored(fl['path']) for fl in c['files']) and not all(is_test(fl['path']) for fl in c['files'])]
    if stray:
        warnings.append(f'{len(stray)} fix commit(s) in the window touch no scored area and count in no area\'s defects: '
                        + '; '.join(f'{c["sha"][:7]} {c["subject"][:70]} ({", ".join(fl["path"] for fl in c["files"][:3])})'
                                    for c in stray[:6]) + (f' (+{len(stray) - 6})' if len(stray) > 6 else '')
                        + '; if what they fixed is product behaviour (deploy configuration, redirects, scripts that ship), '
                        'add those files to --areas')
    unreferenced, unref_note = unreferenced_files(root, files, areas, owner, excluded_paths, imports, entries)
    if unreferenced:
        warnings.append(f'hint: {len(unreferenced)} source file(s) that nothing in the repository imports (possibly dead '
                        'code; their complexity still counts in their areas): ' + ', '.join(unreferenced[:8])
                        + (f' (+{len(unreferenced) - 8})' if len(unreferenced) > 8 else '')
                        + '; confirm before testing them, and say in the plan if an area\'s complexity rests on them')
    elif unref_note:
        warnings.append(unref_note)
    if out_areas and all(a['factors'][k]['status'] == 'not_assessed' for a in out_areas for k in ('journey_criticality', 'reach')):
        warnings.append(SURFACE_ONLY_WARNING)
    rel_meta = None
    if release:
        rel_meta = {'base': release.get('base'), 'changesets': len(release.get('changesets') or []),
                    'packages': {n: {'dir': p['dir'], 'bump': p.get('bump'), 'breaking': bool(p.get('breaking'))}
                                 for n, p in sorted(rel_pkgs.items()) if p.get('bump') or p.get('breaking')},
                    'breaking_commits': len(release.get('breaking_commits') or [])}
    return {'repo': root.name, 'head': hist['head'],
            'history': {**window, 'commits': hist['summary']['commits'], 'shallow': hist['shallow'],
                        'excluded_formatting': hist['excluded_formatting']},
            'scope': ({'kind': (scope.get('source') or {}).get('kind'), 'value': (scope.get('source') or {}).get('base_ref'),
                       'command': (scope.get('source') or {}).get('command')} if scope else None),
            'inputs_used': [{'skill': i['skill'], 'path': i['path'], 'generated_at': i['generated_at'], 'items': len(i['items'])}
                            for i in inputs] + ([{'skill': 'journey-mapper', 'path': args.journeys.name, 'generated_at': None,
                                                  'items': len(journeys)}] if have_map else []),
            'inputs_missing': [n for n, ok in (('journey-mapper', have_map), ('usage export', bool(usage)),
                                               ('Jira bug search', bool(args.defects))) if not ok],
            'journey_map_scope': ({'domains': jscope['domains'], 'partial': jscope['partial'],
                                   'out_of_scope_files': len(jscope['out_of_scope_files'])} if have_map else None),
            'area_basis': area_basis, 'unassigned': unassigned, 'unscored_areas': unscored, 'scope_outside_areas': scope_outside,
            'scope_deleted': [{'path': sf['path'], 'lines': sf.get('lines_deleted') or 0, 'area': area_of(sf['path'], areas)}
                              for sf in gone_scope],
            'unreferenced': unreferenced,
            'warnings': warnings + list(hist.get('notes') or []),
            'excluded': excluded, 'flaky_tests_not_counted': flaky_skipped, 'tests_not_counted': tests_not_counted,
            'tests_not_run_by_default': gated_out, 'script_languages': scripts, 'coverage_not_read': coverage_not_read,
            'fixtures_excluded': [{'dir': d, 'reason': r} for d, r in sorted(FIXTURES.items())],
            'copy_groups': copy_out,
            'self_tests': [{'file': f, 'command': c, 'lines': n, 'area': area_of(f, areas)}
                           for f, (_, n, c) in sorted(SELF_TESTS.items()) if COPY_REP.get(f, f) == f],
            'pytest_collected': [{'file': f, 'config': c, 'kind': 'benchmark' if f in PY_BENCH else 'test'}
                                 for f, c in sorted({**PYTEST_TESTS, **PY_BENCH}.items())],
            'release': rel_meta, 'language_coverage': lang_cov,
            'weights': {'likelihood': LIKELIHOOD, 'impact': IMPACT}, 'areas': out_areas}


# (file suffix, line as written, the classes it must match). Synthetic idioms from SDK, CLI and UI-automation code that
# used to misfire, and the real surfaces next to them that must still match.
SELF_TEST_CASES = [
    ('.ts', 'const opts = settleDefaults(input);', []),
    ('.ts', 'await this.settlePendingFrames(view);', []),
    ('.ts', '  settle(): Promise<void>;', []),
    ('.ts', 'const shot = await captureSnapshot(page);', []),
    ('.ts', 'captureOutput(proc);', []),
    ('.ts', 'return await capture({ ...step, timeoutMs: 5 });', []),
    ('.ts', 'queue.settleRejected(err);', []),
    ('.ts', 'const id = await capturePayment(order);', ['money']),
    ('.py', 'charge_id = capture(payment_id, amount)', ['money']),
    ('.py', 'def charge_card(card_token, amount_cents, currency):', ['money']),
    ('.ts', 'await stripe.refunds.create({ charge });', ['money']),
    ('.ts', 'constructor(private readonly ledger: StepLedger) {}', []),
    ('.ts', 'if (state.ledger.length) lines.push(state.ledger);', []),
    ('.py', 'ledger.post_entry(account, -amount)', ['money']),
    ('.ts', 'return truncateBytes(clean(label), 4096);', []),
    ('.ts', 'const short = truncate(label, 80);', []),
    ('.py', 'truncate_table(conn, "orders")', ['destructive']),
    ('.ts', 'inflater.destroy(err);', []),
    ('.ts', "socket.on('error', () => res.destroy());", []),
    ('.ts', 'proc.stdout.destroy();', []),
    ('.js', 'await order.destroy({ where: { id } });', ['destructive']),
    ('.ts', 'inflight?.delete(req.id);', []),
    ('.ts', 'locks?.delete(slot.key);', []),
    ('.ts', '  delete(recordId: string, opts: Options): Promise<boolean>;', []),
    ('.ts', 'await prisma.user.deleteMany({ where: { orgId } });', ['destructive']),
    ('.ts', 'private requireSession(): BrowserSession {', []),
    ('.py', '@require_login', ['access']),
    ('.py', 'require_admin(request)', ['access']),
    ('.ts', 'function formatRule(matcher: RuleMatcher): string {', []),
    ('.ts', "export const config = { matcher: ['/dashboard/:path*'] };", ['access']),
    ('.tsx', 'const [consent, setConsent] = useState(false);', []),
    ('.py', 'record_consent(user_id, consent)', ['compliance']),
    ('.tsx', 'if (!consent) {', []),
    ('.tsx', 'onClick={() => setConsent(!consent)}', []),
    ('.ts', 'return () => registry().delete(worker);', []),
    ('.ts', 'delete (target as Record<string, unknown>)[key];', []),
    ('.ts', 'this.window.erase();', []),
    ('.ts', "conn.on('close', () => client.destroy());", []),
    ('.ts', 'const safe = redactSecrets(report, vault);', []),
    ('.ts', 'export function redactSecrets(text: string): string {', ['secrets']),
    ('.py', 'def redact_tokens(text):', ['secrets']),
    ('.ts', 'const creds = resolveSecrets(settings, env);', ['secrets']),
    ('.ts', "headers['Authorization'] = token;", ['secrets']),
    ('.ts', 'const init = { headers: { Authorization: `Bearer ${token}` } };', ['secrets']),
    ('.ts', 'if (opts.basicAuth) answerChallenge(opts.basicAuth);', ['secrets']),
    ('.ts', 'const tokens = countTokens(prompt);', []),
    ('.ts', "program.command('run').action(run);", ['public_api']),
    ('.ts', 'export default defineCommand({ meta: { name: "init" } });', ['public_api']),
    ('.rs', 'let m = diagnostics::capture(args);', []),
    # Go: the builtin delete(map, key), clearing a text field and closing a WebDriver-style session destroy nothing
    ('.go', '\tdelete(pending, id)', []),
    ('.go', '\tdelete(s.handlers, name)', []),
    ('.go', 'return d.eraseText(ctx, n)', []),
    ('.go', 'if err := c.DeleteSession(); err != nil {', []),
    ('.go', 'func (c *Client) DeleteSession() error {', []),
    ('.go', 'if err := store.DeleteRecord(ctx, id); err != nil {', ['destructive']),
    # Go: real surfaces the Rust-only systems pack and the JS/Python idioms missed
    ('.go', 'req.SetBasicAuth(user, key)', ['secrets']),
    ('.go', 'key := os.Getenv("SAMPLE_ACCESS_KEY")', ['secrets']),
    ('.go', 'func RedactTypedText(s string) string {', ['secrets']),
    ('.go', 'func redactURL(u string) string {', ['secrets']),
    ('.go', 'out, err := exec.Command("adb", "-s", serial, "uninstall", pkg).Output()', ['destructive', 'subprocess']),
    ('.go', 'cmd := fmt.Sprintf("pm clear %s", pkg)', ['destructive']),
    ('.go', 'Commands: []*cli.Command{', ['public_api']),
    ('.go', 'var rootCmd = &cobra.Command{', ['public_api']),
    ('.go', 'if err := os.RemoveAll(dir); err != nil {', ['filesystem_destruction']),
    ('.go', 'ln, err := net.Listen("tcp", addr)', ['subprocess']),
    ('.go', 'cmd := exec.CommandContext(ctx, bin, args...)', ['subprocess']),
    ('.go', 'if err := os.Chmod(path, 0o600); err != nil {', ['privilege']),
    ('.go', 'n := len(items) // the frame\'s context', []),
    ('.go', '// clearState runs pm clear, or an uninstall, on the device', []),
    ('.go', '_, err := c.delete(c.sessionPath())', []),
    ('.go', 'func (c *Client) delete(path string) (map[string]any, error) {', []),
    ('.go', 'in.drop(page.key())', []),
    ('.go', 'if err := client.DeleteSessionWithin(timeout); err != nil {', []),
    ('.go', '"deleteCount": eraseCount(n),', []),
    ('.swift', 'let erased = eraseTrailingCharacters(app: app, count: n)', []),
    ('.go', 'if _, err := d.shell("pm clear " + appID); err != nil {', ['destructive']),
    ('.go', 'func (d *Driver) wipeData(bundle string) error {', ['destructive']),
    ('.py', 'cur.execute("DELETE FROM sessions WHERE id = %s", (sid,))', ['destructive']),
    ('.py', 'x = 1  # DELETE FROM is never run here', []),
    # pandas reshaping: a new DataFrame without some rows or columns, nothing stored is destroyed
    ('.py', "df = df.drop_duplicates(subset=['code'])", []),
    ('.py', 'rows = frame.dropna()', []),
    ('.py', "slim = df.drop(columns=['notes'])", []),
    ('.py', 'df.drop(labels, axis=1, inplace=True)', []),
    ('.py', 'drop_table(conn, "sessions")', ['destructive']),
    # PHP front controllers and integrations; files written from request data (Python, JavaScript, PHP)
    ('.php', "$uri = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);", ['entry']),
    ('.php', "if (isset($_POST['note'])) {", ['entry']),
    ('.php', '$ch = curl_init($target);', ['external']),
    ('.php', "$body = file_get_contents('https://assets.example.test/x');", ['external']),
    ('.php', "file_put_contents($dir . '/' . $name, $data);", ['file_write']),
    ('.php', "move_uploaded_file($_FILES['f']['tmp_name'], $dest);", ['entry', 'file_write']),
    ('.py', 'with open(save_path, "wb") as fh:', ['file_write']),
    ('.py', "with open(path, 'a', encoding='utf-8') as log:", ['file_write']),
    ('.py', 'with open(path) as fh:', []),
    ('.py', "with open(path, 'rb') as fh:", []),
    ('.py', 'target.write_bytes(payload)', ['file_write']),
    ('.js', 'fs.writeFileSync(target, body);', ['file_write']),
    ('.ts', 'await fs.promises.appendFile(logPath, line);', ['file_write']),
    # JSX text is copy: a page that mentions the GDPR or consent does not handle either
    ('.tsx', '<p>We follow the GDPR and keep a data retention policy.</p>', []),
    ('.jsx', '<li>Ask us to record_consent( for you</li>', []),
    ('.tsx', 'if (a > b && record_consent(user) < 3) {', ['compliance']),
    # a same-origin fetch (a relative URL, or a service worker passing the request through) is not an integration
    ('.ts', "const res = await fetch('/api/bikes');", []),
    ('.ts', 'const res = await fetch(`/api/bikes/${id}`);', []),
    ('.js', 'event.respondWith(fetch(event.request));', ['service_worker']),
    ('.js', 'return fetch(request).then((r) => r);', []),
    ('.ts', "const res = await fetch('https://rates.example.test/v1');", ['external']),
    ('.ts', 'const res = await fetch(url, { method: "POST" });', ['external']),
    # third-party tag loaders: string literals are blanked for patterns, so these are read on the raw line
    ('.tsx', '<Script src="https://www.googletagmanager.com/gtag/js?id=G-TEST000" strategy="afterInteractive" />',
     ['third_party_script']),
    ('.tsx', "  gtag('config', 'G-TEST000');", ['third_party_script']),
    ('.tsx', '(window.adsbygoogle = window.adsbygoogle || []).push({});', ['third_party_script']),
    ('.ts', '  { key: "Content-Security-Policy", value: "script-src https://www.googletagmanager.com" },', []),
    # a service worker's fetch handler answers every request the page makes, from the network or a cache
    ('.js', "self.addEventListener('fetch', (event) => {", ['service_worker']),
    ('.js', '  event.respondWith(caches.match(event.request));', ['service_worker']),
    ('.ts', "window.addEventListener('resize', onResize);", []),
    # graphics objects freed in an error path or Drop destroy no stored data; a store's delete does
    ('.rs', '        self.gl.delete_texture(self.tex);', []),
    ('.rs', '    gl.delete_framebuffer(fb);', []),
    ('.rs', 'device.destroy_buffer(staging, None);', []),
    ('.ts', 'gl.deleteTexture(tex);', []),
    ('.rs', 'store.delete_reading(id)?;', ['destructive']),
    ('.rs', 'repo.delete_user(&conn, user_id)?;', ['destructive']),
    # Rust CLIs (clap), process launches and native code; Python process launches
    ('.rs', '#[derive(Parser)]', ['public_api']),
    ('.rs', '#[derive(Debug, Subcommand)]', ['public_api']),
    ('.rs', '#[derive(Debug, Clone)]', []),
    ('.rs', 'let lib = unsafe { libloading::Library::new(path)? };', ['native_code', 'raw_syscall']),
    ('.rs', 'unsafe extern "C" {', ['native_code']),
    ('.py', 'subprocess.run([exe, "--version"], check=True)', ['subprocess']),
    ('.py', 'proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)', ['subprocess']),
    ('.py', 'lib = ctypes.CDLL(path)', ['native_code']),
]


def self_test() -> int:
    """Check the surface idioms in SELF_TEST_CASES and the test/product directory rule; prints failures, returns 0/1."""
    bad = []
    for suffix, line, want in SELF_TEST_CASES:
        got = line_classes(comment_free(line, suffix), code_view(line, suffix), suffix)
        if sorted(got) != sorted(want):
            bad.append(f'{suffix} {line!r}: matched {got}, expected {want}')
    files = ['packages/e2e/package.json', 'packages/e2e/src/run.ts', 'packages/e2e/tests/run.test.ts',
             'apps/shop/e2e/package.json', 'apps/shop/e2e/checkout.spec.ts', 'apps/shop/e2e/helpers.ts',
             'apps/shop/src/cart.ts']
    import tempfile
    saved = set(PRODUCT_DIRS)
    tmp = tempfile.TemporaryDirectory()
    for f in files:                      # a workspace package named e2e (package.json + src/), an app's e2e/ project
        p = Path(tmp.name) / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"name": "e2e", "exports": {".": "./dist/index.js"}}\n' if f == 'packages/e2e/package.json'
                     else '{"name": "shop-e2e", "private": true}\n' if f.endswith('package.json') else '\n',
                     encoding='utf-8')
    PRODUCT_DIRS.clear()
    PRODUCT_DIRS.update(product_dirs(Path(tmp.name), files))
    try:
        for f, want_test in (('packages/e2e/src/run.ts', False), ('packages/e2e/tests/run.test.ts', True),
                             ('apps/shop/e2e/checkout.spec.ts', True), ('apps/shop/src/cart.ts', False)):
            if is_test(f) != want_test:
                bad.append(f'{f}: is_test {is_test(f)}, expected {want_test} (product dirs {sorted(PRODUCT_DIRS)})')
        for f, want_src in (('packages/e2e/src/run.ts', True), ('apps/shop/e2e/helpers.ts', False)):
            if is_source(f) != want_src:
                bad.append(f'{f}: is_source {not want_src}, expected {want_src} (a package named e2e is product source; '
                           'an e2e/ test project is not)')
        for t, want in (('packages/sdk/tests/live/oauth.ts', True), ('packages/sdk/tests/helpers/server.ts', True),
                        ('packages/sdk/tests/client.test.ts', False)):
            if bool(not_a_counted_test(t)) != want:
                bad.append(f'{t}: not counted {not want}, expected {want}')
    finally:
        PRODUCT_DIRS.clear()
        PRODUCT_DIRS.update(saved)
        tmp.cleanup()
    checks = (go_layout_checks() + web_app_checks() + static_front_end_checks() + next_site_checks() + cargo_workspace_checks()
              + tool_repo_checks() + loaded_site_checks() + data_area_checks())
    bad += [name for name, ok in checks if not ok]
    for b in bad:
        print('FAIL ' + b)
    total = len(SELF_TEST_CASES) + 9 + len(checks)       # 9 path-rule checks, then the Go layout checks
    print(f'self-test: {total - len(bad)} passed, {len(bad)} failed')
    return 1 if bad else 0


def go_layout_checks() -> list[tuple[str, bool]]:
    """A synthetic Go module at the repository root with two small nested package.json directories (an npm wrapper and
    a bundled driver), an XCUITest runner app, Objective-C sources and a coverprofile: the Go packages must be areas,
    the runner is not tests, Objective-C counts, and the coverprofile becomes test-weakness evidence."""
    import tempfile
    out: list[tuple[str, bool]] = []
    saved = (set(PRODUCT_DIRS), set(RUNNER_DIRS) if 'RUNNER_DIRS' in globals() else set())
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        layout = {
            'go.mod': 'module example.test/sample\n\ngo 1.22\n',
            'main.go': 'package main\n\nfunc main() { run() }\n',
            'pkg/alpha/alpha.go': 'package alpha\n\nfunc A(x int) int {\n\tif x > 0 {\n\t\treturn 1\n\t}\n\treturn 0\n}\n',
            'pkg/alpha/alpha_test.go': 'package alpha\n\nimport "testing"\n\nfunc TestA(t *testing.T) {}\n',
            'pkg/beta/beta.go': 'package beta\n\nfunc B() {}\n',
            'pkg/beta/inner/inner.go': 'package inner\n\nfunc C() {}\n',
            'npm/wrapper/package.json': '{"name": "sample-wrapper", "bin": {"sample": "./index.js"}}\n',
            'npm/wrapper/index.js': 'module.exports = 1;\n',
            'drivers/agent/package.json': '{"name": "agent-tools", "private": true}\n',
            'drivers/agent/tool.js': 'module.exports = 2;\n',
            'drivers/agent/Agent/Server.m': '#import "Server.h"\n@implementation Server\n@end\n',
            'drivers/agent/Agent/Server.h': '@interface Server\n@end\n',
            'drivers/runner/SampleRunnerUITests/SampleRunnerUITests.swift':
                'import XCTest\nclass SampleRunnerUITests: XCTestCase {\n  func testRun() {\n'
                '    if !Server().serveForever() { XCTFail("runner wait ended") }\n  }\n}\n',
            'drivers/runner/SampleRunnerUITests/Server.swift':
                'import Network\nfinal class Server {\n  func serveForever() -> Bool {\n'
                '    let l = try? NWListener(using: .tcp)\n    return l != nil\n  }\n}\n',
            'drivers/ios/HostApp/main.swift': 'print("host")\n',
            'drivers/embed.go': 'package drivers\n\nvar Files = 1\n',
            'app/SampleAppTests/LoginTests.swift': 'import XCTest\nclass LoginTests: XCTestCase {\n  func testA() { XCTAssertTrue(true) }\n}\n',
            'drivers/agent/AgentTests/ServerTests.m': '- (void)testServe { XCTAssertNotNil(self.server); }\n',
            'drivers/agent/AgentTests/ServerTests.h': '@interface ServerTests\n@end\n',
        }
        for rel, text in layout.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding='utf-8')
        files = sorted(layout)
        try:
            PRODUCT_DIRS.clear()
            PRODUCT_DIRS.update(product_dirs(root, files))
            set_runner_dirs(root, files)
            areas, basis = define_areas(root, files, None)
            ids = {a['id'] for a in areas}
            out.append((f'root go.mod: Go packages are areas (got {sorted(ids)}, {basis})',
                        {'pkg/alpha', 'pkg/beta', 'pkg/beta/inner'} <= ids and any('main.go' in a['paths'] for a in areas)))
            out.append(('root go.mod: pkg/alpha/alpha.go is owned by pkg/alpha', area_of('pkg/alpha/alpha.go', areas) == 'pkg/alpha'))
            out.append((f'Swift code under a Go package directory is its own area, not the Go package\'s '
                        f'(got {area_of("drivers/ios/HostApp/main.swift", areas)})',
                        area_of('drivers/ios/HostApp/main.swift', areas) not in (None, 'drivers')))
            out.append(('an XCUITest runner app (no assertions) is not test code',
                        not is_test('drivers/runner/SampleRunnerUITests/Server.swift')
                        and not is_test('drivers/runner/SampleRunnerUITests/SampleRunnerUITests.swift')))
            out.append(('an XCTest bundle with assertions stays test code', is_test('app/SampleAppTests/LoginTests.swift')))
            out.append(('Objective-C files of an XCTest bundle are test code', is_test('drivers/agent/AgentTests/ServerTests.m')
                        and is_test('drivers/agent/AgentTests/ServerTests.h')))
            out.append(('Objective-C sources are area code', is_area_code('drivers/agent/Agent/Server.m')
                        and is_area_code('drivers/agent/Agent/Server.h')))
            gofiles = [f'pkg/m{i}/m.go' for i in range(6)]
            w = uncovered_language(gofiles + ['npm/w/index.js'], {f: None for f in gofiles} | {'npm/w/index.js': 'npm/w'},
                                   {f: {'loc': 3} for f in gofiles + ['npm/w/index.js']}, root, 'workspace packages')
            out.append((f'loud warning when the dominant language is outside every area (got {w!r})', bool(w) and '.go' in w))
            (root / 'tools').mkdir(exist_ok=True)
            (root / 'tools' / 'gen.lua').write_text('print(1)\n', encoding='utf-8')
            cov, note = language_note(root)
            out.append((f'the language census names code no script measures (got {note!r})',
                        cov.get('unsupported', {}).get('Lua') == 1 and bool(note) and 'Lua' in note))
            prof = root / 'coverage.out'
            prof.write_text('mode: set\nexample.test/sample/pkg/alpha/alpha.go:3.20,4.11 1 1\n'
                            'example.test/sample/pkg/alpha/alpha.go:4.11,6.3 1 0\n'
                            'example.test/sample/pkg/alpha/alpha.go:7.2,7.10 2 1\n'
                            'example.test/sample/pkg/beta/beta.go:3.12,3.13 0 0\n', encoding='utf-8')
            cov = read_coverage(root, prof)
            out.append((f'coverprofile maps module paths to files (got {cov})',
                        cov.get('pkg/alpha/alpha.go') == (4, 3)))
            out.append(('coverage 75% scores test weakness 2, 30% scores 4', coverage_score(0.75) == 2 and coverage_score(0.30) == 4))
        except Exception as e:                       # a missing function is a failed check, not a crash
            out.append((f'Go layout checks raised {type(e).__name__}: {e}', False))
        finally:
            PRODUCT_DIRS.clear()
            PRODUCT_DIRS.update(saved[0])
            if 'RUNNER_DIRS' in globals():
                RUNNER_DIRS.clear()
                RUNNER_DIRS.update(saved[1])
    return out


def static_front_end_checks() -> list[tuple[str, bool]]:
    """A synthetic FastAPI server at the root that imports every helper module (one lazily inside a function), serving
    a committed front end from dist/ (no package.json, no bundler) whose files carry the scope's changes, a PHP front
    controller beside it, an upload module writing files from the request, and a journey map on journey-mapper's
    P1-P3 scale. Explicit areas under dist/ must be kept, not dropped; changed lines no area counts must be named;
    the root app module must not hand every journey to every module it imports; journey-mapper's P1 is critical."""
    import tempfile
    out: list[tuple[str, bool]] = []
    saved = (set(PRODUCT_DIRS), set(RUNNER_DIRS) if 'RUNNER_DIRS' in globals() else set())
    layout = {
        'server.py': ('import argparse\nfrom fastapi import FastAPI\nfrom fastapi.staticfiles import StaticFiles\n'
                      'from extras import uploads, cache\n\napp = FastAPI()\napp.include_router(uploads.router)\n\n\n'
                      '@app.get("/assets/{path:path}")\nasync def assets(path: str):\n    return cache.fetch(path)\n\n\n'
                      'def unpack(url):\n    from tools.packer import unpack_all\n    return unpack_all(url)\n\n\n'
                      'app.mount("/", StaticFiles(directory="dist"), name="dist")\n'),
        'extras/__init__.py': '',
        'extras/uploads.py': ('from fastapi import APIRouter, UploadFile\n\nrouter = APIRouter()\n\n\n'
                              '@router.post("/upload")\nasync def upload(token: str, file: UploadFile):\n'
                              '    save_path = "saves/" + token\n    with open(save_path, "wb") as fh:\n'
                              '        fh.write(await file.read())\n    return {}\n'),
        'extras/cache.py': 'def fetch(path):\n    if not path:\n        return None\n    return path\n',
        'tools/__init__.py': '',
        'tools/packer.py': 'def unpack_all(url):\n    for part in url.split("/"):\n        print(part)\n',
        'dist/index.html': '<!doctype html><button id="go">Go</button><script src="app.js"></script>\n' * 3,
        'dist/app.js': ''.join(f'function step{i}() {{ if (window.x) {{ return {i}; }} return 0; }}\n' for i in range(30)),
        'dist/modules/loader.js': 'export const load = () => fetch("/assets/a.bin");\n',
        **{f'dist/modules/part{i}.js': f'export const part{i} = (x) => x + {i};\n' for i in range(4)},
        'index.php': ("<?php\n$uri = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);\n"
                      "if (preg_match('#^/assets/(.*)#', $uri, $m)) {\n    $ch = curl_init('https://assets.example.test/' . $m[1]);\n}\n"),
        '.htaccess': 'RewriteEngine On\nRewriteRule ^.*$ index.php [L]\n',
        'requirements.txt': 'fastapi\n',
        'web/lib/chart.min.js': 'var c=function(a){return a+1};' * 40 + '\n',
    }
    changed = {'dist/app.js': 200, 'dist/index.html': 20, 'extras/cache.py': 3}
    journey_map = {'journeys': [
        {'id': 'JRN-PLAY-001', 'title': 'Open the app and start', 'priority': 'P1', 'covers': [],
         'steps': [{'evidence': [{'file': 'server.py', 'line': 9}]}]},
        {'id': 'JRN-OPS-001', 'title': 'Operator unpacks an archive', 'priority': 'P3', 'covers': [],
         'steps': [{'evidence': [{'file': 'tools/packer.py', 'line': 1}]}]}]}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in layout.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding='utf-8')
        files = sorted(layout)
        hist = {'repo': 'kiosk', 'head': 'abc123', 'shallow': False, 'notes': [], 'command': 'git log',
                'window': {'days': 180, 'from': '2026-05-01', 'to': '2026-11-01', 'head_date': '2026-11-01'},
                'excluded_formatting': [], 'summary': {'commits': 1, 'fix_commits': 0, 'formatting_commits': 0},
                'commits': [{'sha': 'c1', 'date': '2026-10-01', 'author': 'A', 'subject': 'Add touch controls', 'fix': False,
                             'keys': [], 'formatting': False,
                             'files': [{'path': p, 'added': n, 'deleted': 0} for p, n in changed.items()]}],
                'files': {f: {'commits': 0, 'lines_added': 0, 'lines_deleted': 0, 'authors': [], 'fix_commits': [],
                              'formatting_commits': [], 'first_commit': '2026-01-01'} for f in files}}
        for p, n in changed.items():
            hist['files'][p].update(commits=1, lines_added=n, authors=['A'])
        cx = {'files': {f: {'loc': len(layout[f].splitlines()), 'branches': layout[f].count('if '), 'method': 'regex'}
                        for f in files if f.endswith(('.py', '.js', '.php'))}}
        scope = {'source': {'kind': 'git', 'base_ref': 'v1', 'command': 'git diff'},
                 'files': [{'path': p, 'status': 'modified', 'kind': 'source', 'lines_added': n, 'lines_deleted': 0}
                           for p, n in changed.items()]}
        (root / '_in').mkdir()
        for name, data in (('history.json', hist), ('complexity.json', cx), ('scope.json', scope),
                           ('journeys.json', journey_map)):
            (root / '_in' / name).write_text(json.dumps(data), encoding='utf-8')
        (root / '_in' / 'areas.json').write_text(json.dumps({'areas': [
            {'id': 'server', 'paths': ['server.py']}, {'id': 'uploads', 'paths': ['extras/uploads.py']},
            {'id': 'cache', 'paths': ['extras/cache.py']}, {'id': 'packer', 'paths': ['tools']},
            {'id': 'front end', 'paths': ['dist']}, {'id': 'php', 'paths': ['index.php']},
            {'id': 'docker', 'paths': ['deploy/docker']}, {'id': 'vendor bundle', 'paths': ['web/lib']}]}), encoding='utf-8')

        def run(areas: bool) -> dict:
            ns = argparse.Namespace(repo=root, history=root / '_in' / 'history.json', complexity=root / '_in' / 'complexity.json',
                                    scope=root / '_in' / 'scope.json', journeys=root / '_in' / 'journeys.json', input=[],
                                    usage=None, areas=(root / '_in' / 'areas.json') if areas else None, defects=None,
                                    finding_area=[], release=None, coverage=[])
            return score(ns)
        try:
            PRODUCT_DIRS.clear()
            set_runner_dirs(root, files)
            res = run(True)
            got = {a['id']: a for a in res['areas']}
            out.append((f'an explicit area under dist/ is scored, not dropped (got {sorted(got)})', 'front end' in got))
            unscored = {u['id']: u for u in res.get('unscored_areas', [])}
            out.append((f'an explicit area matching no file is listed in unscored_areas and warned about (got '
                        f'{res.get("unscored_areas")})', 'docker' in unscored
                        and any('docker' in w and 'no file' in w for w in res['warnings'])))
            out.append((f'an explicit area whose files are classified generated says so in a warning (got {res["warnings"]})',
                        any('vendor bundle' in w and 'generated' in w for w in res['warnings'])))
            srv = got.get('server', {}).get('factors', {}).get('journey_criticality', {})
            out.append((f'journey-mapper\'s P1 (critical, the top of its P1-P3 scale) scores 5 (got {srv})',
                        srv.get('score') == 5))
            pk = got.get('packer', {}).get('factors', {}).get('journey_criticality', {})
            out.append((f'a module the root app module imports does not inherit every journey that cites the app '
                        f'(got {pk})', pk.get('score') == 2 and not any('server.py' in e for e in pk.get('evidence', []))))
            php = got.get('php', {})
            out.append((f'a PHP front controller is a public entry point with an external integration (got '
                        f'{php.get("surfaces")})', {'entry', 'external'} <= set(php.get('surfaces') or [])))
            up = got.get('uploads', {})
            out.append((f'a file written from request data is a file_write surface (got {up.get("surfaces")})',
                        'file_write' in (up.get('surfaces') or [])))
            res = run(False)
            outside = res.get('scope_outside_areas') or []
            # the served dist/ is source under the shared build-output rule, so its changed lines are counted in an
            # area; what is left outside (a stray module tree) must still be listed with its reason and warned about
            out.append((f'changed lines no area counts are listed with their reason and warned about (got {outside}, '
                        f'{res["warnings"]})', bool(outside) and all(x.get('reason') and x.get('lines') for x in outside)
                        and any('changed line' in w and outside[0]['path'] in w for w in res['warnings'])))
            out.append((f'changed lines in a served dist/ are counted in an area, not left outside (got {outside})',
                        not any(x['path'].startswith('dist') for x in outside)))
            basis = res['area_basis']
            counted = {f for a in res['areas'] for f in [a['id']]}
            out.append((f'the default areas never pick a main source directory whose files are all excluded (basis '
                        f'{basis!r}, areas {sorted(counted)})', not ('main source directory dist/' in basis
                                                                       and all(e['path'].startswith('dist/') for e in res['excluded'] if e['path'].startswith('dist/'))
                                                                       and not any(a['id'].startswith('dist') or a['id'] in ('modules', 'dist (top level)') for a in res['areas']))))
        finally:
            PRODUCT_DIRS.clear()
            PRODUCT_DIRS.update(saved[0])
            if 'RUNNER_DIRS' in globals():
                RUNNER_DIRS.clear()
                RUNNER_DIRS.update(saved[1])
    return out


def web_app_checks() -> list[tuple[str, bool]]:
    """A synthetic server-rendered recipe site: a root app.py that creates a Flask app, Jinja templates and a
    stylesheet beside it, a directory of JSON recipe records the app loads, and more (but smaller) one-off scripts
    in scripts/ than there are app modules. The root app module must be an area (not root tooling), template and
    data changes must count as change in scope, and explicit areas holding only templates or data must be kept."""
    import tempfile
    out: list[tuple[str, bool]] = []
    saved = (set(PRODUCT_DIRS), set(RUNNER_DIRS) if 'RUNNER_DIRS' in globals() else set())
    app = ('from flask import Flask, render_template\n\napp = Flask(__name__)\n\n\n'
           + ''.join(f'@app.route("/r{i}")\ndef recipe_{i}():\n    if {i} > 2:\n        return render_template("recipe.html")\n'
                     f'    return render_template("index.html")\n\n\n' for i in range(40)))
    layout = {'app.py': app, 'templates/index.html': '<h1>Recipes</h1>\n' * 5,
              'templates/recipe.html': '{% extends "index.html" %}\n' * 5, 'static/site.css': 'body { margin: 0; }\n',
              'tests/test_app.py': 'from app import app\n\n\ndef test_index():\n    assert app\n',
              'requirements.txt': 'flask\n', 'Procfile': 'web: gunicorn app:app\n'}
    layout.update({f'scripts/fix_{i}.py': f'print({i})\n' for i in range(12)})
    layout.update({f'recipes/r{i}.json': f'{{"id": {i}}}\n' for i in range(30)})
    changed = {'templates/recipe.html': 20, 'recipes/r1.json': 4, 'recipes/r2.json': 4, 'app.py': 6, 'static/site.css': 2}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in layout.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding='utf-8')
        files = sorted(layout)
        hist = {'repo': 'recipes', 'head': 'abc123', 'shallow': False, 'notes': [], 'command': 'git log',
                'window': {'days': 180, 'from': '2026-05-01', 'to': '2026-11-01', 'head_date': '2026-11-01'},
                'excluded_formatting': [], 'summary': {'commits': 2, 'fix_commits': 1, 'formatting_commits': 0},
                'commits': [{'sha': 'c1', 'date': '2026-10-01', 'author': 'A', 'subject': 'Fix recipe page', 'fix': True,
                             'keys': [], 'formatting': False,
                             'files': [{'path': p, 'added': n, 'deleted': 0} for p, n in changed.items()]},
                            {'sha': 'c2', 'date': '2026-09-01', 'author': 'B', 'subject': 'Add scripts', 'fix': False,
                             'keys': [], 'formatting': False,
                             'files': [{'path': f'scripts/fix_{i}.py', 'added': 1, 'deleted': 0} for i in range(12)]}],
                'files': {f: {'commits': 0, 'lines_added': 0, 'lines_deleted': 0, 'authors': [], 'fix_commits': [],
                              'formatting_commits': [], 'first_commit': '2026-01-01'} for f in files}}
        for p, n in changed.items():
            hist['files'][p].update(commits=1, lines_added=n, authors=['A'], fix_commits=['c1'])
        for i in range(12):
            hist['files'][f'scripts/fix_{i}.py'].update(commits=1, lines_added=1, authors=['B'])
        cx = {'files': {f: {'loc': len(layout[f].splitlines()), 'branches': layout[f].count('if '), 'method': 'ast'}
                        for f in files if f.endswith('.py')}}
        scope = {'source': {'kind': 'git', 'base_ref': 'v1', 'command': 'git diff'},
                 'files': [{'path': p, 'status': 'modified', 'kind': 'source', 'lines_added': n, 'lines_deleted': 0}
                           for p, n in changed.items()]}
        for name, data in (('history.json', hist), ('complexity.json', cx), ('scope.json', scope)):
            (root / '_in').mkdir(exist_ok=True)
            (root / '_in' / name).write_text(json.dumps(data), encoding='utf-8')
        (root / '_in' / 'areas.json').write_text(json.dumps({'areas': [
            {'id': 'web', 'paths': ['app.py']}, {'id': 'templates', 'paths': ['templates', 'static']},
            {'id': 'recipes data', 'paths': ['recipes']}, {'id': 'scripts', 'paths': ['scripts']}]}), encoding='utf-8')

        def run(areas: bool) -> dict:
            ns = argparse.Namespace(repo=root, history=root / '_in' / 'history.json', complexity=root / '_in' / 'complexity.json',
                                    scope=root / '_in' / 'scope.json', journeys=None, input=[], usage=None,
                                    areas=(root / '_in' / 'areas.json') if areas else None, defects=None, finding_area=[],
                                    release=None, coverage=[])
            return score(ns)
        try:
            PRODUCT_DIRS.clear()
            set_runner_dirs(root, files)
            res = run(False)
            ids = [a['id'] for a in res['areas']]
            excl = [e['path'] for e in res['excluded']]
            out.append((f'a root module that creates the web app is an area, not root tooling (areas {ids}, excluded '
                        f'{[e for e in excl if not e.startswith("recipes")][:4]})', 'app.py' not in excl
                        and any('app.py' in area_paths for area_paths in (a['paths'] for a in res['areas']))))
            web = next((a for a in res['areas'] if 'app.py' in a['paths']), None)
            cis = (web or {}).get('factors', {}).get('change_in_scope', {}).get('evidence', [''])[0]
            out.append((f'template and stylesheet changes beside the app count in its change in scope (got {cis!r})',
                        cis.startswith('28 lines')))
            warned = [w for w in res['warnings'] if 'recipes/' in w]
            out.append((f'changed data files in no area are named in a warning (got {res["warnings"]})', bool(warned)))
            res = run(True)
            got = {a['id']: a for a in res['areas']}
            out.append((f'explicit areas holding only templates or data are kept (got {sorted(got)})',
                        {'templates', 'recipes data'} <= set(got)))
            data_cis = got.get('recipes data', {}).get('factors', {}).get('change_in_scope', {}).get('evidence', [''])[0]
            out.append((f'a data area\'s change in scope counts its changed records (got {data_cis!r})', data_cis.startswith('8 lines')))
            tpl = got.get('templates', {}).get('factors', {})
            out.append((f'a template-only area has complexity not_assessed, not a score from zero code (got '
                        f'{tpl.get("complexity")})', tpl.get('complexity', {}).get('status') == 'not_assessed'))
            out.append((f'areas with no impact evidence are named in a warning (got {res["warnings"]})',
                        any('no impact evidence' in w and 'templates' in w for w in res['warnings'])))
            files6 = [f'pkg/m{i}.py' for i in range(6)]
            w = uncovered_language(files6 + ['engine.py'], {f: 'pkg' for f in files6} | {'engine.py': None},
                                   {f: {'loc': 5} for f in files6} | {'engine.py': {'loc': 2000}}, root, 'main source')
            out.append((f'loud warning when most of the dominant language\'s lines are outside every area (got {w!r})',
                        bool(w) and 'line' in w))
        except Exception as e:                       # a missing function is a failed check, not a crash
            import traceback
            out.append((f'web app checks raised {type(e).__name__}: {e} {traceback.format_exc()[-600:]}', False))
        finally:
            PRODUCT_DIRS.clear()
            PRODUCT_DIRS.update(saved[0])
            if 'RUNNER_DIRS' in globals():
                RUNNER_DIRS.clear()
                RUNNER_DIRS.update(saved[1])
    return out


NEXT_SITE = {
    'package.json': '{"name": "spokes", "dependencies": {"next": "15.1.0", "react": "19.0.0"}}\n',
    'tsconfig.json': '{"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}\n',
    'next.config.ts': (
        "const csp = \"default-src 'self'; script-src 'self' https://www.googletagmanager.com\";\n"
        'const nextConfig = {\n'
        '  async headers() {\n'
        "    return [{ source: '/(.*)', headers: [{ key: 'Content-Security-Policy', value: csp },\n"
        "      { key: 'X-Frame-Options', value: 'DENY' }] }];\n"
        '  },\n'
        '  async redirects() {\n'
        "    return [{ source: '/old-bikes', destination: '/bikes', permanent: true }];\n"
        '  },\n'
        '};\n'
        'export default nextConfig;\n'),
    'vercel.json': '{"redirects": [{"source": "/shop", "destination": "/bikes", "permanent": true}]}\n',
    'src/app/layout.tsx': (
        "import Script from 'next/script';\n"
        "import Header from '@/components/Header';\n"
        "import SwRegister from '@/components/SwRegister';\n"
        '\n'
        'export default function RootLayout({ children }: { children: React.ReactNode }) {\n'
        '  return (\n'
        '    <html lang="en">\n'
        '      <body>\n'
        '        <Header />\n'
        '        {children}\n'
        '        <SwRegister />\n'
        '        <Script src="https://www.googletagmanager.com/gtag/js?id=G-TEST000" strategy="afterInteractive" />\n'
        '        <Script id="ga-init" strategy="afterInteractive">{`\n'
        '          window.dataLayer = window.dataLayer || [];\n'
        "          function gtag(){dataLayer.push(arguments);}\n"
        "          gtag('config', 'G-TEST000');\n"
        '        `}</Script>\n'
        '      </body>\n'
        '    </html>\n'
        '  );\n'
        '}\n'),
    'src/app/page.tsx': ("import { listBikes } from '@/lib/bikes';\n\nexport default async function Home() {\n"
                         '  const bikes = await listBikes();\n  return <main>{bikes.length} bikes</main>;\n}\n'),
    'src/app/robots.ts': ("export default function robots() {\n  return { rules: { userAgent: '*', allow: '/' } };\n}\n"),
    'src/app/sitemap.ts': ("export default function sitemap() {\n  return [{ url: 'https://bikes.example.test/' }];\n}\n"),
    'src/app/not-found.tsx': 'export default function NotFound() {\n  return <p>No such bike.</p>;\n}\n',
    'src/app/error.tsx': ("'use client';\nexport default function Error({ reset }: { reset: () => void }) {\n"
                          '  return <button onClick={() => reset()}>Try again</button>;\n}\n'),
    'src/app/about/page.tsx': (
        'export default function About() {\n'
        '  return (\n'
        '    <article>\n'
        '      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify({\n'
        "        '@context': 'https://schema.org', '@type': 'BikeStore', url: 'https://bikes.example.test' }) }} />\n"
        '      <h1>About the shop</h1>\n'
        '      <p>\n'
        '        We follow the GDPR and keep a data retention policy for every order.\n'
        '      </p>\n'
        '    </article>\n'
        '  );\n'
        '}\n'),
    'src/app/bikes/[id]/page.tsx': ("import { getBike } from '@/lib/bikes';\n\nexport function generateStaticParams() {\n"
                                    "  return [{ id: 'roadster' }];\n}\n\nexport default async function Bike({ params }: any) {\n"
                                    '  const bike = await getBike(params.id);\n  return <h1>{bike.name}</h1>;\n}\n'),
    'src/app/bikes/[id]/opengraph-image.tsx': ("import { ImageResponse } from 'next/og';\n\nexport default function Image() {\n"
                                               '  return new ImageResponse(<div>Bike</div>);\n}\n'),
    'src/components/Header.tsx': 'export default function Header() {\n  return <header>Spokes</header>;\n}\n',
    'src/components/SwRegister.tsx': ("'use client';\nimport { useEffect } from 'react';\n\nexport default function SwRegister() {\n"
                                      '  useEffect(() => {\n'
                                      "    if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');\n"
                                      '  }, []);\n  return null;\n}\n'),
    'src/components/OldGallery.tsx': ('export default function OldGallery({ items }: { items: string[] }) {\n'
                                      '  if (!items.length) return null;\n  return <ul>{items.map((i) => <li key={i}>{i}</li>)}</ul>;\n}\n'),
    'src/lib/bikes.ts': ("export async function listBikes() {\n  const res = await fetch('/api/bikes');\n  return res.json();\n}\n"
                         'export async function getBike(id: string) {\n'
                         '  const res = await fetch(`/api/bikes/${id}`);\n  return res.json();\n}\n'),
    'public/sw.js': ("self.addEventListener('fetch', (event) => {\n"
                     '  const url = new URL(event.request.url);\n'
                     '  if (url.origin !== self.location.origin) return;\n'
                     "  if (event.request.mode === 'navigate') {\n"
                     '    event.respondWith(fetch(event.request).catch(() => caches.match(\'/offline\')));\n'
                     '    return;\n'
                     '  }\n'
                     '  event.respondWith(caches.match(event.request).then((hit) => hit || fetch(event.request)));\n'
                     '});\n'),
    'public/robots-extra.txt': 'User-agent: *\n',
    'scripts/build-feed.mjs': "import fs from 'node:fs';\nfs.writeFileSync('public/feed.xml', '<rss/>');\n",
}


def next_site_checks() -> list[tuple[str, bool]]:
    """A synthetic Next.js App Router bike shop with no tests: next.config headers (CSP) and redirects, a vercel.json
    redirect, a root layout loading an analytics tag with next/script, metadata routes, not-found and error pages, a
    service worker in public/ registered by a client component, a component nothing imports, same-origin fetches,
    prose that mentions the GDPR, and a scope that deletes middleware. Each gap of the App Router layout is a check."""
    import tempfile
    out: list[tuple[str, bool]] = []
    saved = (set(PRODUCT_DIRS), set(RUNNER_DIRS))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in NEXT_SITE.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding='utf-8')
        files = sorted(NEXT_SITE)
        stat = lambda: {'commits': 0, 'lines_added': 0, 'lines_deleted': 0, 'authors': [], 'fix_commits': [],  # noqa: E731
                        'formatting_commits': [], 'first_commit': '2026-01-01'}
        hist = {'repo': 'spokes', 'head': 'abc123', 'shallow': False, 'notes': [], 'command': 'git log',
                'window': {'days': 180, 'from': '2026-05-01', 'to': '2026-11-01', 'head_date': '2026-11-01'},
                'excluded_formatting': [], 'summary': {'commits': 3, 'fix_commits': 2, 'formatting_commits': 0},
                'commits': [{'sha': 'c1', 'date': '2026-10-01', 'author': 'A', 'subject': 'Fix analytics loading', 'fix': True,
                             'keys': [], 'formatting': False,
                             'files': [{'path': 'src/app/layout.tsx', 'added': 6, 'deleted': 2}]},
                            {'sha': 'c2', 'date': '2026-10-02', 'author': 'A', 'subject': 'Fix feed build script', 'fix': True,
                             'keys': [], 'formatting': False, 'files': [{'path': 'scripts/build-feed.mjs', 'added': 1, 'deleted': 1}]},
                            {'sha': 'c3', 'date': '2026-10-03', 'author': 'A', 'subject': 'Fix legacy redirect', 'fix': True,
                             'keys': [], 'formatting': False, 'files': [{'path': 'src/lib/legacy.ts', 'added': 3, 'deleted': 1}]}],
                'files': {f: stat() for f in files + ['src/lib/legacy.ts']}}
        hist['files']['src/app/layout.tsx'].update(commits=1, lines_added=6, lines_deleted=2, authors=['A'], fix_commits=['c1'])
        hist['files']['scripts/build-feed.mjs'].update(commits=1, lines_added=1, lines_deleted=1, authors=['A'], fix_commits=['c2'])
        hist['files']['src/lib/legacy.ts'].update(commits=1, lines_added=3, lines_deleted=1, authors=['A'], fix_commits=['c3'])
        cx = {'files': {f: {'loc': len(t.splitlines()), 'branches': t.count('if '), 'method': 'regex'}
                        for f, t in NEXT_SITE.items() if PurePosixPath(f).suffix in CODE_EXT}}
        scope = {'source': {'kind': 'git', 'base_ref': 'v1', 'command': 'git diff'},
                 'files': [{'path': 'src/middleware.ts', 'status': 'deleted', 'kind': 'source', 'lines_added': 0,
                            'lines_deleted': 39},
                           {'path': 'src/lib/legacy.ts', 'status': 'deleted', 'kind': 'source', 'lines_added': 0,
                            'lines_deleted': 12},
                           {'path': 'src/app/layout.tsx', 'status': 'modified', 'kind': 'source', 'lines_added': 4,
                            'lines_deleted': 0}]}
        (root / '_in').mkdir()
        for name, data in (('history.json', hist), ('complexity.json', cx), ('scope.json', scope)):
            (root / '_in' / name).write_text(json.dumps(data), encoding='utf-8')
        (root / '_in' / 'areas.json').write_text(json.dumps({'areas': [
            {'id': 'site', 'paths': ['src']}, {'id': 'platform-config', 'paths': ['next.config.ts', 'vercel.json']}]}),
            encoding='utf-8')

        def run(areas: bool) -> dict:
            ns = argparse.Namespace(repo=root, history=root / '_in' / 'history.json', complexity=root / '_in' / 'complexity.json',
                                    scope=root / '_in' / 'scope.json', journeys=None, input=[], usage=None,
                                    areas=(root / '_in' / 'areas.json') if areas else None, defects=None, finding_area=[],
                                    release=None, coverage=[])
            return score(ns)

        def area_with(res: dict, path: str) -> dict:
            return next((a for a in res['areas'] if area_of(path, [a]) == a['id']), {})
        try:
            PRODUCT_DIRS.clear()
            set_runner_dirs(root, files)
            res = run(False)
            ids = [a['id'] for a in res['areas']]
            warn = ' | '.join(res['warnings'])
            excl = {e['path']: e['reason'] for e in res['excluded']}
            cfg = area_with(res, 'next.config.ts')
            out.append((f'next.config.* with headers/redirects is a scored runtime-config area, not tool configuration '
                        f'(areas {ids}, excluded {excl.get("next.config.ts")!r})', bool(cfg) and 'next.config.ts' not in excl))
            sw = area_with(res, 'public/sw.js')
            out.append((f'a registered service worker in public/ is a scored area, not a static file (areas {ids}, excluded '
                        f'{excl.get("public/sw.js")!r})', bool(sw) and 'public/sw.js' not in excl))
            out.append((f'a same-origin fetch in the service worker is not an external integration (got {sw.get("surfaces")})',
                        bool(sw) and 'external' not in sw.get('surfaces', [])))
            out.append((f'the fetch handler of the service worker is a surface (got {sw.get("surfaces")})',
                        'service_worker' in sw.get('surfaces', [])))
            lib = area_with(res, 'src/lib/bikes.ts')
            out.append((f'relative-URL fetches are not an external integration (got {lib.get("surfaces")})',
                        bool(lib) and 'external' not in lib.get('surfaces', [])))
            lay, rob = area_with(res, 'src/app/layout.tsx'), area_with(res, 'src/app/robots.ts')
            out.append((f'the root layout is its own area, apart from metadata routes and pages (layout '
                        f'{lay.get("id")!r}, robots {rob.get("id")!r}, home {area_with(res, "src/app/page.tsx").get("id")!r})',
                        bool(lay) and lay.get('id') not in (rob.get('id'), area_with(res, 'src/app/page.tsx').get('id'))))
            out.append((f'a next/script analytics loader is a third-party script surface (layout surfaces '
                        f'{lay.get("surfaces")})', 'third_party_script' in lay.get('surfaces', [])))
            out.append((f'metadata routes (robots, sitemap) are entry points (got {rob.get("surfaces")})',
                        bool(set(rob.get('surfaces', [])) & {'ui_route', 'entry'})))
            nf = area_with(res, 'src/app/not-found.tsx')
            out.append((f'not-found and error pages are user-facing pages (got {nf.get("surfaces")})',
                        'ui_route' in nf.get('surfaces', [])))
            og = area_with(res, 'src/app/bikes/[id]/opengraph-image.tsx')
            og_ev = ' '.join(og.get('factors', {}).get('surface', {}).get('evidence', []))
            out.append((f'a generated opengraph-image is a public entry (got {og_ev!r})', 'opengraph-image' in og_ev))
            about = area_with(res, 'src/app/about/page.tsx')
            out.append((f'"GDPR" in JSX text is copy, not a compliance surface (got {about.get("surfaces")})',
                        bool(about) and 'compliance' not in about.get('surfaces', [])))
            out.append((f'a JSON-LD data block is not a third-party script loader (got {about.get("surfaces")})',
                        bool(about) and 'third_party_script' not in about.get('surfaces', [])))
            deleted = [x for x in res['scope_outside_areas'] if 'deleted' in x['reason']]
            out.append((f'a deleted file in no area is listed in scope_outside_areas as deleted (got {res["scope_outside_areas"]})',
                        any(x['lines'] == 39 for x in deleted)))
            top = area_with(res, 'src/lib/legacy.ts')
            cis = ' '.join(top.get('factors', {}).get('change_in_scope', {}).get('evidence', []))
            out.append((f'a deleted file counts in the change in scope of the area covering it (got {cis!r})',
                        cis.startswith('12 lines') and 'deleted' in cis))
            out.append((f'deleted files in the scope are named in a warning (got {warn!r})',
                        'src/middleware.ts' in warn and 'deleted' in warn))
            out.append((f'a repository with no test files says so loudly (got {warn!r})', 'no test files' in warn))
            out.append((f'fix commits that touch no scored area are named (got {warn!r})', 'Fix feed build script' in warn))
            out.append((f'a file deleted before HEAD keeps its fix commits in the covering area (got '
                        f'{top.get("factors", {}).get("defects", {}).get("evidence")})',
                        any('c3' in e for e in top.get('factors', {}).get('defects', {}).get('evidence', []))))
            out.append((f'source files nothing imports are hinted as possibly dead (got {res.get("unreferenced")})',
                        'src/components/OldGallery.tsx' in (res.get('unreferenced') or [])
                        and 'src/app/robots.ts' not in (res.get('unreferenced') or [])
                        and 'src/components/Header.tsx' not in (res.get('unreferenced') or [])))
            res = run(True)
            got = {a['id']: a for a in res['areas']}
            out.append((f'an explicit area naming next.config.ts and vercel.json is scored, not dropped as tool configuration '
                        f'(unscored {res["unscored_areas"]})', 'platform-config' in got))
            pc = got.get('platform-config', {})
            out.append((f'next.config headers/CSP/redirects are a response-policy surface (got {pc.get("surfaces")})',
                        'response_policy' in pc.get('surfaces', [])))
        except Exception as e:
            import traceback
            out.append((f'Next.js site checks raised {type(e).__name__}: {e} {traceback.format_exc()[-600:]}', False))
        finally:
            PRODUCT_DIRS.clear()
            PRODUCT_DIRS.update(saved[0])
            RUNNER_DIRS.clear()
            RUNNER_DIRS.update(saved[1])
    return out


_PY_TOOL = ('import json\nimport subprocess\nimport sys\n\n\n'
            + ''.join(f'def step_{i}(rows):\n    out = []\n    for r in rows:\n        if r > {i}:\n            out.append(r)\n'
                      f'    return out\n\n\n' for i in range(6))
            + 'def main():\n    subprocess.run([sys.executable, "-c", "print(1)"], check=True)\n')
CARGO_SITE = {
    'Cargo.toml': '[workspace]\nmembers = ["crates/*"]\nresolver = "2"\n',
    'crates/station-core/Cargo.toml': '[package]\nname = "station-core"\nversion = "0.1.0"\nedition = "2024"\n',
    'crates/station-core/src/units.rs': ''.join(f'pub fn to_unit_{i}(x: f64) -> f64 {{\n    if x < 0.0 {{\n        return 0.0;\n'
                                                f'    }}\n    x * {i}.0\n}}\n\n' for i in range(30)),
    'crates/station-core/src/lib.rs': (
        'pub mod store;\npub mod units;\n\npub fn mean(xs: &[f64]) -> f64 {\n    if xs.is_empty() {\n        return 0.0;\n    }\n'
        '    xs.iter().sum::<f64>() / xs.len() as f64\n}\n\n#[cfg(test)]\nmod tests {\n    use super::*;\n\n'
        '    #[test]\n    fn mean_of_two() {\n        assert_eq!(mean(&[1.0, 3.0]), 2.0);\n    }\n\n'
        '    #[test]\n    fn mean_of_none() {\n        assert_eq!(mean(&[]), 0.0);\n    }\n}\n'),
    'crates/station-core/src/store.rs': (
        'pub struct Db;\n\nimpl Db {\n    pub fn execute(&self, _sql: &str) {}\n}\n\n'
        'pub fn delete_reading(db: &Db, id: u32) {\n    let _ = id;\n    db.execute("DELETE FROM readings WHERE id = ?1");\n}\n'),
    'crates/station-core/benches/lookup.rs': (
        'fn main() {\n    let dir = std::env::temp_dir().join("bench");\n    std::fs::create_dir_all(&dir).unwrap();\n'
        '    std::fs::remove_dir_all(&dir).unwrap();\n}\n'),
    'crates/station-render/Cargo.toml': (
        '[package]\nname = "station-render"\nversion = "0.1.0"\nedition = "2024"\n\n[features]\ngpu-tests = []\n'),
    'crates/station-render/src/lib.rs': 'pub mod gpu;\n',
    'crates/station-render/ui/panel.slint': ('export component Panel inherits Window {\n    in property <string> reading;\n'
                                            '    Text { text: reading; }\n}\n'),
    'crates/station-render/src/gpu.rs': (
        'pub struct Gl;\n\nimpl Gl {\n    pub fn delete_texture(&self, _t: u32) {}\n    pub fn delete_framebuffer(&self, _f: u32) {}\n}\n\n'
        'pub struct Target {\n    gl: Gl,\n    tex: u32,\n    fb: u32,\n}\n\n'
        'impl Drop for Target {\n    fn drop(&mut self) {\n        self.gl.delete_texture(self.tex);\n'
        '        self.gl.delete_framebuffer(self.fb);\n    }\n}\n\n'
        'pub fn load_backend(path: &str) -> Option<libloading::Library> {\n'
        '    // SAFETY: the backend is built with this crate\n    unsafe { libloading::Library::new(path).ok() }\n}\n\n'
        '#[cfg(all(test, feature = "gpu-tests"))]\n#[path = "gpu_checks.rs"]\nmod gpu_checks;\n'),
    'crates/station-render/src/gpu_checks.rs': (
        'use super::*;\n\n' + ''.join(f'#[test]\nfn draws_{i}() {{\n    let gl = Gl;\n    gl.delete_texture({i});\n'
                                       f'    assert_eq!({i} + 1, {i + 1});\n}}\n\n' for i in range(12))),
    'crates/station-cli/Cargo.toml': (
        '[package]\nname = "station-cli"\nversion = "0.1.0"\nedition = "2024"\n\n[[bin]]\nname = "station"\npath = "src/main.rs"\n\n'
        '[dependencies]\nclap = { version = "4", features = ["derive"] }\nstation-core = { path = "../station-core" }\n'),
    'crates/station-cli/src/main.rs': (
        'use clap::{Parser, Subcommand};\nuse std::process::Command;\n\n#[derive(Parser)]\nstruct Cli {\n'
        '    #[command(subcommand)]\n    command: Cmd,\n}\n\n#[derive(Subcommand)]\nenum Cmd {\n    Show,\n    Plot,\n}\n\n'
        'fn main() {\n    match Cli::parse().command {\n        Cmd::Show => println!("{}", station_core::mean(&[1.0])),\n'
        '        Cmd::Plot => {\n            let _ = Command::new("plotter").arg("--out").status();\n        }\n    }\n}\n'),
    'crates/station-cli/tests/cli.rs': (
        'use station_core::mean;\n\n#[test]\nfn shows_mean() {\n    assert_eq!(mean(&[2.0]), 2.0);\n}\n'),
    'crates/station-cli/tests/fixtures/fake_plotter.rs': 'fn main() {\n    println!("plotted");\n}\n',
    # distinct scripts (identical files would be one copy group, scored once)
    **{f'scripts/{n}.py': _PY_TOOL + f'\n\ndef name():\n    return "{n}"\n' for n in ('report', 'fetch', 'calibrate')},
    'tests/test_report.py': ('import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))\n'
                             'import report\n\n\ndef test_step():\n    assert report.step_1([3]) == [3]\n'),
}


def cargo_workspace_checks() -> list[tuple[str, bool]]:
    """A synthetic weather-station Cargo workspace (three crates: a library with unit tests in a #[cfg(test)] module and a
    bench, a renderer that frees GPU objects in Drop, loads a backend library and includes a feature-gated test module,
    a clap derive CLI that launches a process, with an integration test and a tests/fixtures helper binary) beside
    Python scripts that are about 40% of the code. Crates are areas, the Python scripts get areas, in-source tests count
    as tests, benches and test modules are not product source, GPU deletes destroy no data, and clap, process launches
    and library loading are surfaces."""
    import tempfile
    out: list[tuple[str, bool]] = []
    saved = (set(PRODUCT_DIRS), set(RUNNER_DIRS))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in CARGO_SITE.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding='utf-8')
        files = sorted(CARGO_SITE)
        hist = {'repo': 'station', 'head': 'abc123', 'shallow': False, 'notes': [], 'command': 'git log',
                'window': {'days': 180, 'from': '2026-05-01', 'to': '2026-11-01', 'head_date': '2026-11-01'},
                'excluded_formatting': [], 'summary': {'commits': 0, 'fix_commits': 0, 'formatting_commits': 0},
                'commits': [],
                'files': {f: {'commits': 0, 'lines_added': 0, 'lines_deleted': 0, 'authors': [], 'fix_commits': [],
                              'formatting_commits': [], 'first_commit': '2026-01-01'} for f in files}}
        cx = {'files': {f: {'loc': len([x for x in CARGO_SITE[f].splitlines() if x.strip()]),
                            'branches': CARGO_SITE[f].count('if '), 'method': 'regex'}
                        for f in files if PurePosixPath(f).suffix in CODE_EXT}}
        scope = {'source': {'kind': 'git', 'base_ref': 'v1', 'command': 'git diff'},
                 'files': [{'path': 'crates/station-core/benches/lookup.rs', 'status': 'modified', 'kind': 'source',
                            'lines_added': 5, 'lines_deleted': 0}]}
        (root / '_in').mkdir()
        for name, data in (('history.json', hist), ('complexity.json', cx), ('scope.json', scope)):
            (root / '_in' / name).write_text(json.dumps(data), encoding='utf-8')
        ns = argparse.Namespace(repo=root, history=root / '_in' / 'history.json', complexity=root / '_in' / 'complexity.json',
                                scope=root / '_in' / 'scope.json', journeys=None, input=[], usage=None, areas=None,
                                defects=None, finding_area=[], release=None, coverage=[])
        try:
            PRODUCT_DIRS.clear()
            res = score(ns)
            got = {a['id']: a for a in res['areas']}
            ids = sorted(got)
            out.append((f'Cargo crates are the default areas (got {ids}, {res["area_basis"]})',
                        {'crates/station-core', 'crates/station-render', 'crates/station-cli'} <= set(ids)))
            py_area = [a for a in res['areas'] if any(p.startswith('scripts') for p in a['paths'])]
            excl = {e['path']: e['reason'] for e in res['excluded']}
            out.append((f'Python scripts that are a large share of the code get an area, not "root tooling" (areas {ids}, '
                        f'excluded {[p for p in excl if p.startswith("scripts")]})', bool(py_area)
                        and not any(p.startswith('scripts/') for p in excl)))
            core = got.get('crates/station-core', {})
            tw = core.get('factors', {}).get('test_weakness', {})
            out.append((f'unit tests in a #[cfg(test)] module count as tests of their crate (got {tw})',
                        tw.get('score') != 5 and any('lib.rs' in t for t in core.get('tests', []))))
            rend = got.get('crates/station-render', {})
            tw = rend.get('factors', {}).get('test_weakness', {})
            out.append((f'a feature-gated test module (#[cfg(all(test, ...))] mod) counts as tests (got {tw})',
                        tw.get('score') != 5 and any('gpu_checks.rs' in t for t in rend.get('tests', []))))
            out.append(('a #[path] test module is test code, not product source',
                        is_test('crates/station-render/src/gpu_checks.rs')))
            out.append((f'benches are not product source (excluded: {excl.get("crates/station-core/benches/lookup.rs")})',
                        not is_area_code('crates/station-core/benches/lookup.rs')
                        and 'bench' in (excl.get('crates/station-core/benches/lookup.rs') or '')))
            out.append((f'a bench does not set its crate\'s surface (got {core.get("surfaces")})',
                        'filesystem_destruction' not in core.get('surfaces', [])))
            out.append(('a tests/fixtures helper binary is not product source',
                        not is_area_code('crates/station-cli/tests/fixtures/fake_plotter.rs')))
            out.append((f'freeing GPU objects is not data destruction (got {rend.get("surfaces")})',
                        'destructive' not in rend.get('surfaces', [])))
            out.append((f'a DELETE FROM in a store still destroys data (got {core.get("surfaces")})',
                        'destructive' in core.get('surfaces', [])))
            out.append((f'library loading is a surface (got {rend.get("surfaces")})', 'native_code' in rend.get('surfaces', [])))
            cli = got.get('crates/station-cli', {})
            out.append((f'a clap derive CLI is a public entry point and Command::new a process launch (got {cli.get("surfaces")})',
                        {'public_api', 'subprocess'} <= set(cli.get('surfaces', []))))
            out.append((f'an integration test that uses another workspace crate is a test of that crate too (got '
                        f'{core.get("tests")})', 'crates/station-cli/tests/cli.rs' in core.get('tests', [])))
            rng = rust_test_ranges('#[cfg(not(test))]\nmod real {\n    fn a() {}\n}\n#[cfg(test)]\nmod t {\n    #[test]\n'
                                   '    fn b() {}\n}\npub fn after() {}\n')
            out.append((f'a #[cfg(not(test))] module is product code and code after a test module is not cut (got {rng})',
                        rng == [(4, 9)]))
            out.append((f'UI markup no script measures (.slint) is named (got {[w[:80] for w in res["warnings"]]})',
                        any('.slint' in w for w in res['warnings'])))
            out.append((f'a tests/fixtures helper binary (fn main) is not counted as a test (got {cli.get("tests")})',
                        'crates/station-cli/tests/fixtures/fake_plotter.rs' not in cli.get('tests', [])
                        and any(t['path'].endswith('fake_plotter.rs') for t in res['tests_not_counted'])))
            # a virtual workspace whose helper scripts are most of the source: the root Cargo.toml is no root package
            for i in range(4):
                (root / 'scripts' / f'extra_{i}.py').write_text(_PY_TOOL * 3, encoding='utf-8')
            more = files + [f'scripts/extra_{i}.py' for i in range(4)]
            set_cargo_model(root, more)
            areas3, basis3 = define_areas(root, more, None)
            out.append((f'a virtual workspace root (no [package]) is not a root package (got {basis3})',
                        'root package' not in basis3 and {'crates/station-core', 'scripts'} <= {a['id'] for a in areas3}))
            out.append(('the surface-only warning tells an app that is neither a web UI nor a library how to judge impact',
                        any(w.startswith('impact rests on the surface factor alone') and 'desktop' in w for w in res['warnings'])))
            pys = next(iter(py_area), {})
            out.append((f'a Python subprocess.run is a process launch (got {pys.get("surfaces")})',
                        'subprocess' in pys.get('surfaces', [])))
            # explicit areas that leave the Python out: a loud warning naming the second language
            (root / '_in' / 'areas.json').write_text(json.dumps({'areas': [
                {'id': c, 'paths': [c]} for c in ('crates/station-core', 'crates/station-render', 'crates/station-cli')]}),
                encoding='utf-8')
            res2 = score(argparse.Namespace(**{**vars(ns), 'areas': root / '_in' / 'areas.json'}))
            lost = [w for w in res2['warnings'] if w.startswith(SECOND_LANGUAGE_WARNING)]
            out.append((f'a second language with 30%+ of the lines outside every area gets a loud warning (got '
                        f'{[w[:90] for w in res2["warnings"]]})', bool(lost) and 'Python' in lost[0]))
        except Exception as e:                       # a missing function is a failed check, not a crash
            import traceback
            out.append((f'Cargo workspace checks raised {type(e).__name__}: {e} {traceback.format_exc()[-600:]}', False))
        finally:
            for x in (RUST_TEST_FILES, BENCH_FILES, FIXTURE_BINS, RUST_INLINE_TESTS, CRATE_DIRS, CARGO):
                x.clear()
            PRODUCT_DIRS.clear()
            PRODUCT_DIRS.update(saved[0])
            RUNNER_DIRS.clear()
            RUNNER_DIRS.update(saved[1])
    return out


# The weather-station workspace again, with what a release tool chain adds around the crates: a launcher module whose
# `use` nests process::Command and hands over with .exec(), a hyphenated packaging script that its test loads by file
# path, installer scripts in PowerShell and shell, and a scope that changes the Slint panel, two Cargo manifests, an
# installer and a bench.
LOADED_SITE = {
    **CARGO_SITE,
    'crates/station-cli/src/main.rs': CARGO_SITE['crates/station-cli/src/main.rs'].replace('use clap', 'mod launch;\nuse clap', 1),
    'crates/station-cli/src/launch.rs': (
        'use std::{ffi::OsString, process::Command};\n\npub fn hand_over(tool: OsString) -> std::io::Error {\n'
        '    use std::os::unix::process::CommandExt;\n    Command::new(tool).arg("--serve").exec()\n}\n'),
    'tools/pack-release.py': _PY_TOOL + '\n\ndef archive_name():\n    return "station.zip"\n',
    'tests/test_pack_release.py': (
        'import importlib.util\nfrom pathlib import Path\n\nROOT = Path(__file__).resolve().parents[1]\n'
        'SPEC = importlib.util.spec_from_file_location("pack_release", ROOT / "tools" / "pack-release.py")\n'
        'pack = importlib.util.module_from_spec(SPEC)\nSPEC.loader.exec_module(pack)\n\n\n'
        'def test_name():\n    assert pack.archive_name() == "station.zip"\n'),
    'packaging/install-runtime.ps1': 'param([string]$Target)\n' + ''.join(f'Write-Host "step {i}"\n' for i in range(20)),
    'packaging/make-archive.sh': '#!/bin/sh\nset -e\ntar -czf station.tar.gz target/release/station\n',
}
LOADED_SCOPE = {'crates/station-core/benches/lookup.rs': 5, 'crates/station-render/ui/panel.slint': 55,
                'crates/station-cli/Cargo.toml': 2, 'Cargo.toml': 3, 'packaging/install-runtime.ps1': 20,
                'crates/station-core/README.md': 9}


def _site_inputs(root: Path, site: dict, changed: dict, churn: dict | None = None) -> argparse.Namespace:
    """Write a synthetic repository and the history, complexity and scope files score() reads; changed and churn are
    {path: lines}."""
    for rel, text in site.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
    files = sorted(site)
    stat = lambda: {'commits': 0, 'lines_added': 0, 'lines_deleted': 0, 'authors': [], 'fix_commits': [],  # noqa: E731
                    'formatting_commits': [], 'first_commit': '2026-01-01'}
    churn = churn or {}
    hist = {'repo': 'station', 'head': 'abc123', 'shallow': False, 'notes': [], 'command': 'git log',
            'window': {'days': 180, 'from': '2026-05-01', 'to': '2026-11-01', 'head_date': '2026-11-01'},
            'excluded_formatting': [], 'summary': {'commits': len(churn), 'fix_commits': 0, 'formatting_commits': 0},
            'commits': [{'sha': f'c{i}', 'date': '2026-10-01', 'author': 'author-1', 'subject': f'Change {p}', 'fix': False,
                         'keys': [], 'formatting': False, 'files': [{'path': p, 'added': n, 'deleted': 0}]}
                        for i, (p, n) in enumerate(sorted(churn.items()))],
            'files': {f: stat() for f in files}}
    for p, n in churn.items():
        hist['files'][p].update(commits=1, lines_added=n, authors=['author-1'])
    cx = {'files': {f: {'loc': len([x for x in site[f].splitlines() if x.strip()]), 'branches': site[f].count('if '),
                        'method': 'regex'} for f in files if PurePosixPath(f).suffix in CODE_EXT}}
    scope = {'source': {'kind': 'git', 'base_ref': 'v1', 'command': 'git diff'},
             'files': [{'path': p, 'status': 'modified', 'kind': diff_scope.classify(p, '', False, root)[0],
                        'lines_added': n, 'lines_deleted': 0} for p, n in changed.items()]}
    (root / '_in').mkdir()
    for name, data in (('history.json', hist), ('complexity.json', cx), ('scope.json', scope)):
        (root / '_in' / name).write_text(json.dumps(data), encoding='utf-8')
    return argparse.Namespace(repo=root, history=root / '_in' / 'history.json', complexity=root / '_in' / 'complexity.json',
                              scope=root / '_in' / 'scope.json', journeys=None, input=[], usage=None, areas=None,
                              defects=None, finding_area=[], release=None, coverage=[])


def loaded_site_checks() -> list[tuple[str, bool]]:
    """LOADED_SITE: a test that loads a script by file path is a test of the script's area; changed files of types no
    script reads (Slint, PowerShell, Cargo.toml) count in their area's change or are listed in scope_outside_areas
    under a path no area id contains; script languages are listed with a warning; a nested `use` and .exec() are a
    process launch; tests a default `cargo test` does not run are noted; an explicit area of installer scripts is
    scored; a coverage file that is no Go coverprofile is named as unread."""
    import tempfile
    out: list[tuple[str, bool]] = []
    saved = (set(PRODUCT_DIRS), set(RUNNER_DIRS))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        try:
            PRODUCT_DIRS.clear()
            ns = _site_inputs(root, LOADED_SITE, LOADED_SCOPE)
            res = score(ns)
            got = {a['id']: a for a in res['areas']}
            ev = lambda aid, k: ' '.join(got.get(aid, {}).get('factors', {}).get(k, {}).get('evidence') or [])  # noqa: E731
            tools = got.get('tools', {})
            out.append((f'a test that loads a hyphenated script with spec_from_file_location is a test of the script\'s '
                        f'area (tools tests {tools.get("tests")}, test weakness {ev("tools", "test_weakness")!r})',
                        'tests/test_pack_release.py' in (tools.get('tests') or [])
                        and tools.get('factors', {}).get('test_weakness', {}).get('score') != 5))
            out.append((f'a changed Slint file counts in its area\'s change in scope and is named (render: '
                        f'{ev("crates/station-render", "change_in_scope")!r})',
                        ev('crates/station-render', 'change_in_scope').startswith('55 lines')
                        and 'Slint' in ev('crates/station-render', 'change_in_scope')))
            out.append((f'a changed Cargo.toml inside a crate counts in the crate\'s change in scope and is named (cli: '
                        f'{ev("crates/station-cli", "change_in_scope")!r})',
                        ev('crates/station-cli', 'change_in_scope').startswith('2 lines')
                        and 'Cargo.toml' in ev('crates/station-cli', 'change_in_scope')))
            outside = {x['path']: x for x in res['scope_outside_areas']}
            out.append((f'changed files of unread types in no area are listed, never dropped (got {sorted(outside)})',
                        'PowerShell' in outside.get('packaging/', {}).get('reason', '')
                        and outside.get('Cargo.toml', {}).get('lines') == 3))
            out.append((f'a changed file outside the areas is listed under a directory no area sits in, not the '
                        f'top-level one every area id contains (got {sorted(outside)})',
                        'crates/station-core/benches/' in outside and 'crates/' not in outside))
            out.append((f'documentation changes are not listed as changed lines to test (got {sorted(outside)})',
                        not any('README' in p for p in outside)
                        and not ev('crates/station-core', 'change_in_scope').startswith('9 lines')))
            kinds = {s['kind']: s for s in res.get('script_languages') or []}
            out.append((f'script languages are listed with their files and lines, and warned about (got {kinds})',
                        set(kinds) == {'PowerShell', 'Shell'} and kinds['PowerShell']['files'] == 1
                        and any(w.startswith(SCRIPT_LANGUAGE_WARNING) for w in res['warnings'])))
            out.append((f'Command::new behind a nested `use std::{{.., process::Command}}` with .exec() is a process launch '
                        f'(cli: {ev("crates/station-cli", "surface")!r})', 'launch.rs' in ev('crates/station-cli', 'surface')))
            gated = res.get('tests_not_run_by_default') or []
            out.append((f'tests a default `cargo test` does not run are noted on the area and listed (render: '
                        f'{ev("crates/station-render", "test_weakness")!r}, listed {gated[:2]})',
                        'default' in ev('crates/station-render', 'test_weakness')
                        and any(g.get('gate') == 'feature' and g.get('area') == 'crates/station-render' for g in gated)))
            (root / '_in' / 'areas.json').write_text(json.dumps({'areas': [
                {'id': c, 'paths': [c]} for c in ('crates/station-core', 'crates/station-render', 'crates/station-cli',
                                                  'scripts', 'tools', 'packaging')]}), encoding='utf-8')
            (root / '_in' / 'cov.json').write_text('{"data": [{"files": [], "totals": {"lines": {"percent": 91.0}}}], '
                                                   '"type": "llvm.coverage.json.export"}\n', encoding='utf-8')
            res2 = score(argparse.Namespace(**{**vars(ns), 'areas': root / '_in' / 'areas.json',
                                               'coverage': [root / '_in' / 'cov.json']}))
            pk = next((a for a in res2['areas'] if a['id'] == 'packaging'), {})
            out.append((f'an explicit area holding only installer scripts is scored on churn and change in scope, with '
                        f'what cannot be read not_assessed (got {pk.get("factors", {}).get("change_in_scope")}, unscored '
                        f'{res2["unscored_areas"]})', pk.get('factors', {}).get('change_in_scope', {}).get('score') is not None
                        and pk.get('factors', {}).get('surface', {}).get('status') == 'not_assessed'
                        and 'PowerShell' in ' '.join(pk.get('factors', {}).get('surface', {}).get('evidence') or [])))
            cov_w = [w for w in res2['warnings'] if w.startswith('--coverage')]
            out.append((f'a coverage file that is not a Go coverprofile is named as unread, with what it looks like '
                        f'(got {cov_w})', bool(cov_w) and 'llvm-cov' in cov_w[0] and 'Go coverprofile' in cov_w[0]))
        except Exception as e:                       # a missing function is a failed check, not a crash
            import traceback
            out.append((f'loaded-site checks raised {type(e).__name__}: {e} {traceback.format_exc()[-600:]}', False))
        finally:
            for x in (RUST_TEST_FILES, BENCH_FILES, FIXTURE_BINS, RUST_INLINE_TESTS, CRATE_DIRS, CARGO):
                x.clear()
            PRODUCT_DIRS.clear()
            PRODUCT_DIRS.update(saved[0])
            RUNNER_DIRS.clear()
            RUNNER_DIRS.update(saved[1])
    return out


# Two small code areas beside a folder of regenerated result files: the data folder's line counts dwarf the code's.
DATA_SITE = {
    'src/app/orders.py': ''.join(f'def order_{i}(x):\n    if x > {i}:\n        return x\n    return {i}\n\n\n' for i in range(8)),
    'src/lib/totals.py': ''.join(f'def total_{i}(rows):\n    if not rows:\n        return 0\n    return sum(rows) + {i}\n\n\n'
                                 for i in range(8)),
    'tests/test_orders.py': 'from src.app.orders import order_1\n\n\ndef test_order():\n    assert order_1(5) == 5\n',
    **{f'results/run_{i}.json': '{"samples": [' + ', '.join(str(n) for n in range(40)) + ']}\n' for i in range(5)},
}
DATA_AREAS = [{'id': 'app', 'paths': ['src/app']}, {'id': 'lib', 'paths': ['src/lib']}, {'id': 'results', 'paths': ['results']}]


def data_area_checks() -> list[tuple[str, bool]]:
    """DATA_SITE with explicit areas: a data-only area (regenerated JSON) never outranks the code areas on likelihood,
    says why its line counts were capped, and does not push the code areas' percentiles down."""
    import tempfile
    out: list[tuple[str, bool]] = []
    saved = (set(PRODUCT_DIRS), set(RUNNER_DIRS))
    try:
        PRODUCT_DIRS.clear()
        runs = {}
        for name, areas in (('with', DATA_AREAS), ('without', DATA_AREAS[:2])):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                lines = {'src/app/orders.py': 30, 'src/lib/totals.py': 12, **{f'results/run_{i}.json': 2900 for i in range(5)}}
                ns = _site_inputs(root, DATA_SITE, lines, churn=lines)
                (root / '_in' / 'areas.json').write_text(json.dumps({'areas': areas}), encoding='utf-8')
                ns.areas = root / '_in' / 'areas.json'
                runs[name] = {a['id']: a for a in score(ns)['areas']}
        got, alone = runs['with'], runs['without']
        data = got.get('results', {})
        code_max = max((got[k]['likelihood'] or 0) for k in ('app', 'lib') if k in got)
        f = data.get('factors', {})
        out.append((f'a data-only area does not outrank the code areas on likelihood (results {data.get("likelihood")}, '
                    f'highest code area {code_max}; churn {f.get("churn")}, change {f.get("change_in_scope")})',
                    data.get('likelihood') is not None and data['likelihood'] <= code_max))
        out.append((f'the capped factors say why (churn evidence {f.get("churn", {}).get("evidence")})',
                    any('data' in e and 'capped' in e for e in f.get('churn', {}).get('evidence') or [])))
        same = all(got[k]['factors'][x]['score'] == alone[k]['factors'][x]['score']
                   for k in ('app', 'lib') for x in ('churn', 'change_in_scope'))
        out.append((f'a data-only area\'s line counts do not lower the code areas\' churn or change-in-scope percentiles '
                    f'(with it: {[(k, got[k]["factors"]["churn"]["score"], got[k]["factors"]["change_in_scope"]["score"]) for k in ("app", "lib")]}, '
                    f'without: {[(k, alone[k]["factors"]["churn"]["score"], alone[k]["factors"]["change_in_scope"]["score"]) for k in ("app", "lib")]})',
                    same))
    except Exception as e:                           # a missing function is a failed check, not a crash
        import traceback
        out.append((f'data-area checks raised {type(e).__name__}: {e} {traceback.format_exc()[-600:]}', False))
    finally:
        PRODUCT_DIRS.clear()
        PRODUCT_DIRS.update(saved[0])
        RUNNER_DIRS.clear()
        RUNNER_DIRS.update(saved[1])
    return out


# A synthetic tool repository: a shared helper copied into two tool folders by a check_shared-style mapping, a scanner
# whose pattern table holds the code it looks for, a script that carries its own --self-test (with a fixture page in a
# string and temporary files it writes), an examples/ sample shop with planted defects, its own tests, a lockfile and a
# Next.js config, pytest benchmarks collected through python_files = bench_*.py, and a CI workflow.
_TOOL_UTIL = (
    '"""Shared helper: read a page and list the scripts it loads."""\n'
    'import re\nimport sys\nimport tempfile\nfrom pathlib import Path\n\n'
    'SCRIPT_SRC = re.compile(r"<script[^>]*src=([^ >]+)", re.I)\n\n\n'
    'def scripts_of(page):\n    out = []\n    for m in SCRIPT_SRC.finditer(page):\n        if m.group(1):\n'
    '            out.append(m.group(1))\n    return out\n\n\n'
    'FIXTURE_PAGE = (\'<Script src="https://www.googletagmanager.com/gtag/js?id=G-TEST000" />\'\n'
    '                "<script>gtag(\'config\', \'G-TEST000\');</script>")\n\n\n'
    'def self_test():\n    d = Path(tempfile.mkdtemp())\n    (d / "page.html").write_text(FIXTURE_PAGE)\n'
    '    shell = \'out, err := exec.Command("adb", "-s", serial, "uninstall", pkg).Output()\'\n'
    '    ok = scripts_of(FIXTURE_PAGE) and "uninstall" in shell\n    print("ok  scripts" if ok else "FAIL  scripts")\n'
    '    return 0 if ok else 1\n\n\n'
    'if __name__ == "__main__":\n    if "--self-test" in sys.argv:\n        sys.exit(self_test())\n'
    '    print(scripts_of(sys.stdin.read()))\n')
_TOOL_SCAN = (
    '"""Scan source text for risky statements.\n\nIt only reads: the device\'s data (pm clear) and DELETE FROM statements are '
    'named, never run.\n"""\nimport subprocess\nimport sys\n\n'
    'RULES = [\n    ("destructive", "DROP TABLE"),\n    ("destructive", "DELETE FROM"),\n'
    '    ("secrets", "\'Authorization\': "),\n    ("script", "gtag("),\n]\n\n\n'
    'def scan(text):\n    hits = []\n    for kind, needle in RULES:\n        if needle in text:\n'
    '            hits.append(kind)\n    return hits\n\n\n'
    'def lint(path):\n    return subprocess.run(["flake8", path], check=False).returncode\n\n\n'
    'if __name__ == "__main__":\n    print(scan(open(sys.argv[1]).read()))\n')
TOOL_REPO = {
    'README.md': '# Sample tools\n',
    'tools/check_shared.py': ("SKILLS = ['alpha-tool', 'beta-tool']\nSHARED = {\n    'scripts/pagekit.py': SKILLS,\n}\n\n\n"
                              'def main():\n    return 0\n'),
    'shared/scripts/pagekit.py': _TOOL_UTIL,
    'alpha-tool/scripts/pagekit.py': _TOOL_UTIL,
    'beta-tool/scripts/pagekit.py': _TOOL_UTIL,
    'beta-tool/requirements.txt': 'requests\n',
    'gamma-tool/requirements.txt': 'requests\n',
    'gamma-tool/run.py': ''.join(f'def step_{i}(x):\n    if x > {i}:\n        return x\n    return {i}\n\n\n' for i in range(8)),
    'alpha-tool/scripts/scan.py': _TOOL_SCAN,
    'delta-tool/scripts/merge.py': ''.join(f'def merge_{i}(a, b):\n    if a and b:\n        return a + b\n    return a or b\n\n\n'
                                           for i in range(16)),
    'beta-tool/scripts/report.py': ''.join(f'def part_{i}(rows):\n    if not rows:\n        return []\n    return rows[{i}:]\n\n\n'
                                           for i in range(10)),
    'alpha-tool/examples/shop/package.json': '{"name": "shop", "dependencies": {"next": "15.1.0"}}\n',
    'alpha-tool/examples/shop/package-lock.json': '{"name": "shop", "lockfileVersion": 3}\n',
    'alpha-tool/examples/shop/next.config.ts': ("const nextConfig = {\n  async headers() {\n    return [{ source: '/(.*)', "
                                                "headers: [{ key: 'X-Frame-Options', value: 'DENY' }] }];\n  },\n};\n"
                                                'export default nextConfig;\n'),
    'alpha-tool/examples/shop/src/app/page.tsx': 'export default function Home() {\n  return <main>shop</main>;\n}\n',
    'alpha-tool/examples/shop/src/cart.py': 'def total(items):\n    return sum(i["price"] for i in items)\n',
    'alpha-tool/examples/shop/tests/test_cart.py': ('from cart import total\n\n\n' + ''.join(
        f'def test_total_{i}():\n    assert total([{{"price": {i}}}]) == {i}\n\n\n' for i in range(20))),
    'alpha-tool/examples/shop/data/prices.json': '{"flour": 349}\n',
    # example outputs and a regression test beside the sample, outside its project folder
    'alpha-tool/examples/run/first.json': '{"findings": []}\n' * 8,
    'alpha-tool/examples/run/second.json': '{"findings": []}\n' * 8,
    'alpha-tool/examples/regression/test_shop_regressions.py': ('import scan\n\n\n' + ''.join(
        f'def test_rule_{i}():\n    assert scan.scan("DROP TABLE t{i}")\n\n\n' for i in range(20))),
    'benchmarks/pytest.ini': '[pytest]\npython_files = bench_*.py\n',
    'benchmarks/bench_scan.py': ('import sys\nsys.path.insert(0, "alpha-tool/scripts")\nimport scan\n\n\n'
                                 'def bench_scan(benchmark):\n    benchmark(scan.scan, "DROP TABLE x")\n'),
    'benchmarks/requirements.txt': 'pytest-codspeed\n',
    '.github/workflows/checks.yml': 'on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n',
}


def tool_repo_checks() -> list[tuple[str, bool]]:
    """The synthetic TOOL_REPO, default and explicit areas: sample projects stay out (no area, no tests, no generated
    warning, the right reason for their changed lines), the copies of a shared file count once, pattern tables and
    self-test fixtures are not surfaces, a script's --self-test is a test of its area, pytest benchmarks are neither
    product code nor tests, sibling tools get areas of the same shape, and an explicit CI area is scored."""
    import tempfile
    out: list[tuple[str, bool]] = []
    saved = (set(PRODUCT_DIRS), set(RUNNER_DIRS))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in TOOL_REPO.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding='utf-8')
        files = sorted(TOOL_REPO)
        changed = {'shared/scripts/pagekit.py': 12, 'alpha-tool/scripts/pagekit.py': 12, 'beta-tool/scripts/pagekit.py': 12,
                   'alpha-tool/scripts/scan.py': 5, 'alpha-tool/examples/shop/data/prices.json': 2,
                   '.github/workflows/checks.yml': 3}
        stat = lambda: {'commits': 0, 'lines_added': 0, 'lines_deleted': 0, 'authors': [], 'fix_commits': [],  # noqa: E731
                        'formatting_commits': [], 'first_commit': '2026-01-01'}
        hist = {'repo': 'tools', 'head': 'abc123', 'shallow': False, 'notes': [], 'command': 'git log',
                'window': {'days': 180, 'from': '2026-05-01', 'to': '2026-11-01', 'head_date': '2026-11-01'},
                'excluded_formatting': [], 'summary': {'commits': 1, 'fix_commits': 0, 'formatting_commits': 0},
                'commits': [{'sha': 'c1', 'date': '2026-10-01', 'author': 'A', 'subject': 'Sync the page helper', 'fix': False,
                             'keys': [], 'formatting': False,
                             'files': [{'path': p, 'added': n, 'deleted': 0} for p, n in changed.items()]}],
                'files': {f: stat() for f in files}}
        for p, n in changed.items():
            hist['files'][p].update(commits=1, lines_added=n, authors=['A'])
        cx = {'files': {f: {'loc': len([x for x in t.splitlines() if x.strip()]), 'branches': t.count('if '), 'method': 'regex'}
                        for f, t in TOOL_REPO.items() if PurePosixPath(f).suffix in CODE_EXT}}
        scope = {'source': {'kind': 'git', 'base_ref': 'v1', 'command': 'git diff'},
                 'files': [{'path': p, 'status': 'modified', 'kind': 'source', 'lines_added': n, 'lines_deleted': 0}
                           for p, n in changed.items()]}
        (root / '_in').mkdir()
        for name, data in (('history.json', hist), ('complexity.json', cx), ('scope.json', scope)):
            (root / '_in' / name).write_text(json.dumps(data), encoding='utf-8')
        (root / '_in' / 'areas.json').write_text(json.dumps({'areas': [
            {'id': 'alpha-tool', 'paths': ['alpha-tool']}, {'id': 'beta-tool', 'paths': ['beta-tool']},
            {'id': 'shared', 'paths': ['shared']}, {'id': 'ci', 'paths': ['.github']}]}), encoding='utf-8')

        def run(areas: bool) -> dict:
            ns = argparse.Namespace(repo=root, history=root / '_in' / 'history.json', complexity=root / '_in' / 'complexity.json',
                                    scope=root / '_in' / 'scope.json', journeys=None, input=[], usage=None,
                                    areas=(root / '_in' / 'areas.json') if areas else None, defects=None, finding_area=[],
                                    release=None, coverage=[])
            return score(ns)

        def area_with(res: dict, path: str) -> dict:
            return next((a for a in res['areas'] if area_of(path, [a]) == a['id']), {})
        try:
            PRODUCT_DIRS.clear()
            set_runner_dirs(root, files)
            res = run(False)
            ids = sorted(a['id'] for a in res['areas'])
            excl = {e['path']: e['reason'] for e in res['excluded']}
            out.append((f'a sample project under examples/ is never an area, even with a Next.js config that sets headers '
                        f'(areas {ids})', not any('examples' in p for a in res['areas'] for p in a['paths'] + [a['id']])))
            out.append((f'the fixture projects left out are reported (got {res.get("fixtures_excluded")})',
                        [f['dir'] for f in res.get('fixtures_excluded') or []] == ['alpha-tool/examples/regression',
                                                                               'alpha-tool/examples/shop']))
            homes = [a['id'] for a in res['areas']
                     if any('pagekit.py' in e for e in ' '.join(a['factors']['surface']['evidence']).split(', '))]
            out.append((f'copies of a shared file are excluded from their areas, naming the canonical copy (excluded '
                        f'{ {k: v for k, v in excl.items() if "pagekit" in k} })',
                        all('shared/scripts/pagekit.py' in excl.get(c, '') for c in ('alpha-tool/scripts/pagekit.py',
                                                                                     'beta-tool/scripts/pagekit.py'))))
            groups = res.get('copy_groups') or []
            out.append((f'copy groups are reported once, with the area that scores them (got {groups})',
                        len(groups) == 1 and groups[0].get('canonical') == 'shared/scripts/pagekit.py'
                        and groups[0].get('copies') == 2 and groups[0].get('area') == area_with(res, 'shared/scripts/pagekit.py').get('id')))
            alpha = area_with(res, 'alpha-tool/scripts/scan.py')
            beta = area_with(res, 'beta-tool/scripts/report.py')
            out.append((f'the change in scope of a copy counts once, in the canonical copy\'s area (alpha '
                        f'{alpha.get("factors", {}).get("change_in_scope", {}).get("evidence")})',
                        alpha.get('factors', {}).get('change_in_scope', {}).get('evidence', [''])[0].startswith('5 lines')))
            sh = area_with(res, 'shared/scripts/pagekit.py')
            out.append((f'a self-test\'s fixture page and its temporary files are not surfaces (shared surfaces '
                        f'{sh.get("surfaces")})', bool(sh) and not set(sh.get('surfaces') or []) & {'third_party_script',
                                                                                                    'file_write', 'destructive'}))
            out.append((f'a pattern table holding the statements a scanner looks for is not a surface; its real process '
                        f'launch is (alpha surfaces {alpha.get("surfaces")})',
                        bool(alpha) and 'subprocess' in alpha.get('surfaces', [])
                        and not set(alpha.get('surfaces') or []) & {'destructive', 'secrets', 'third_party_script'}))
            out.append((f'a script\'s --self-test is a test of its own area (shared tests {sh.get("tests")}, test weakness '
                        f'{sh.get("factors", {}).get("test_weakness")})', 'shared/scripts/pagekit.py' in (sh.get('tests') or [])
                        and sh.get('factors', {}).get('test_weakness', {}).get('score') != 5))
            out.append((f'pytest benchmarks (python_files = bench_*.py) are not product code (areas {ids}, excluded '
                        f'{excl.get("benchmarks/bench_scan.py")!r})', 'benchmarks' not in ids
                        and 'bench' in (excl.get('benchmarks/bench_scan.py') or '')))
            out.append((f'pytest benchmarks are not counted as tests (tests_not_counted {res["tests_not_counted"]})',
                        'benchmarks/bench_scan.py' not in alpha.get('tests', [])
                        and any(t['path'] == 'benchmarks/bench_scan.py' and 'bench' in t['reason'] for t in res['tests_not_counted'])))
            out.append((f'sibling tools get areas of the same shape, with or without a manifest (alpha {alpha.get("id")!r}, '
                        f'beta {beta.get("id")!r})', alpha.get('id') == 'alpha-tool' and beta.get('id') == 'beta-tool'))
            outside = {x['path']: x['reason'] for x in res['scope_outside_areas']}
            out.append((f'changed lines in a sample project are named as such, not "in no area" (got {outside})',
                        'sample or fixture project' in outside.get('alpha-tool/examples/', '')))
            res = run(True)
            got = {a['id']: a for a in res['areas']}
            at = got.get('alpha-tool', {})
            out.append((f'tests of a sample project, or kept with the examples, do not count as the tool\'s tests (alpha '
                        f'tests {at.get("tests")})', 'alpha-tool/examples/shop/tests/test_cart.py' not in (at.get('tests') or [])
                        and 'alpha-tool/examples/regression/test_shop_regressions.py' not in (at.get('tests') or [])
                        and any(t['path'] == 'alpha-tool/examples/shop/tests/test_cart.py' for t in res['tests_not_counted'])))
            out.append((f'a sample project\'s lockfile draws no "classified generated" warning (got '
                        f'{[w for w in res["warnings"] if "generated" in w]})', not any('package-lock' in w for w in res['warnings'])))
            ci = got.get('ci', {})
            out.append((f'an explicit area holding a CI workflow is scored, with churn and change in scope (unscored '
                        f'{res["unscored_areas"]})', bool(ci) and ci.get('factors', {}).get('change_in_scope', {}).get('score')
                        is not None and ci.get('factors', {}).get('surface', {}).get('status') == 'not_assessed'))
        except Exception as e:                       # a missing function is a failed check, not a crash
            import traceback
            out.append((f'tool repository checks raised {type(e).__name__}: {e} {traceback.format_exc()[-600:]}', False))
        finally:
            PRODUCT_DIRS.clear()
            PRODUCT_DIRS.update(saved[0])
            RUNNER_DIRS.clear()
            RUNNER_DIRS.update(saved[1])
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--history', type=Path, required=True)
    ap.add_argument('--complexity', type=Path, required=True)
    ap.add_argument('--scope', type=Path)
    ap.add_argument('--journeys', type=Path)
    ap.add_argument('--input', action='append', default=[], help='KIND=path, KIND one of security, crash, wcag, flaky')
    ap.add_argument('--usage', type=Path)
    ap.add_argument('--areas', type=Path)
    ap.add_argument('--defects', type=Path, help='bug tickets from fetch_ticket.py --jql (read-only)')
    ap.add_argument('--finding-area', action='append', default=[],
                    help='KIND:ID=AREA: carry this input finding on AREA, when its report cites the wrong file (repeatable)')
    ap.add_argument('--release', type=Path, help='release.json from release_scope.py: pending changesets and breaking commits')
    ap.add_argument('--coverage', type=Path, action='append', default=[],
                    help='a Go coverprofile (go test -coverprofile=coverage.out ./...): statement coverage per area '
                         'becomes the test-weakness evidence (repeatable)')
    ap.add_argument('--fixture-dir', action='append', default=[], metavar='NAME|PATH',
                    help='also treat this directory (a name anywhere, or a repository-relative path) as holding sample or '
                         'fixture projects, which are not scored (repeatable)')
    ap.add_argument('--include-fixtures', action='store_true',
                    help='score sample and fixture projects (examples/, fixtures/, testdata/ ...) as the repository\'s own code')
    ap.add_argument('--keep-copies', action='store_true',
                    help='score every copy of a file kept identical in several places, not once on its canonical copy')
    ap.add_argument('--out', type=Path, required=True)
    if '--self-test' in (sys.argv[1:] if argv is None else argv):
        return self_test()
    args = ap.parse_args(argv)
    res = score(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=2) + '\n', encoding='utf-8')
    for a in res['areas']:
        print(f'{a["id"]:<12} L {a["likelihood"]}  I {a["impact"]}  risk {a["risk"]}  {a["band"]:<6} {a["confidence"]:<6} '
              f'{",".join(a["flags"])}')
    print(f'areas: {res["area_basis"]}', file=sys.stderr)
    for w in res['warnings']:
        print(f'warning: {w}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
