#!/usr/bin/env python3
"""Score every area of a repository for risk = likelihood x impact, citing the evidence for each factor (stdlib only).

    python score_risk.py --repo <repo> --history history.json --complexity complexity.json --scope scope.json \
        --out risk.json [--journeys journey_map.json] [--input security=security_audit.json] [--input flaky=flaky_report.json]
        [--input crash=crash_report.json] [--input wcag=audit.json] [--usage usage.json] [--areas areas.json]
        [--defects bugs.json] [--finding-area KIND:ID=AREA] [--release release.json] [--coverage coverage.out]
    python score_risk.py --self-test

A root go.mod makes the module's Go packages the areas (nested packages and non-Go code under the module beside
them); --coverage takes a Go coverprofile as the test-weakness evidence. Otherwise areas are the workspace packages when the repo has several (a package with more than
SPLIT_PACKAGE source files is split by subdirectory below its source root); for a Next.js App Router project,
one per API resource (app/api/<x>), one per top-level route segment or group, app (root),
middleware/proxy, and lib/, components/ and the other source directories split by subdirectory
(and by file-name prefix when flat and large); otherwise the immediate subdirectories of the
main source directory (the top-level directory holding the most source files, followed down
while one subdirectory holds nearly all of it), with container directories such as modules/ and
common/ split again. --areas gives them explicitly as {"areas": [{"id": ..., "paths": [...]}]}.
Test, generated, vendored, tool-configuration and docs files are not areas. Source files no area
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
import argparse, ast, json, math, os, re, sys
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import detect_conventions, diff_scope, lang_census  # noqa: E402

SURFACES = json.loads((HERE.parent / 'data' / 'surfaces.json').read_text(encoding='utf-8'))
LIKELIHOOD = {'churn': 2, 'defects': 3, 'complexity': 1, 'change_in_scope': 2, 'test_weakness': 2}
IMPACT = {'journey_criticality': 3, 'surface': 3, 'reach': 1}
PRIORITY_SCORE = {'P0': 5, 'P1': 4, 'P2': 3, 'P3': 2}
SEVERE = {'critical', 'high'}
UI_EXT = {'.tsx', '.jsx', '.vue', '.svelte'}
STDLIB = set(getattr(sys, 'stdlib_module_names', ())) | {'__future__'}
CODE_EXT = {'.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.java', '.kt', '.go', '.cs', '.swift', '.dart', '.rb', '.php',
            '.scala', '.rs', '.vue', '.svelte', '.sql', '.m', '.mm', '.h', '.c', '.cc', '.cpp', '.hpp'}


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
    return (p.suffix in CODE_EXT and not is_test(f) and not f.endswith('.d.ts') and not dir_names(f) & NOT_SOURCE_DIRS
            and not CONFIG_NAME.match(p.name))


DOC_DIRS = {'docs', 'doc', 'documentation', 'examples'}


def is_area_code(f: str) -> bool:
    """A file that counts in its area's scores: code that is not a test, tool configuration or documentation.
    Migrations, static and generated-looking directories still count here (a migration that drops a column is a
    destructive surface); is_source() is the stricter test for choosing areas."""
    p = PurePosixPath(f)
    return (p.suffix in CODE_EXT and not is_test(f) and not CONFIG_NAME.match(p.name)
            and not dir_names(f) & DOC_DIRS)


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


def next_app_areas(root: Path, files: list[str], source: list[str]) -> list[dict] | None:
    """Areas for a Next.js App Router project, where the directory tree under app/ is routing, not modules: one area per
    API resource (app/api/<resource>), one per top-level route segment or route group, app (root) for the root layout
    and files; then lib/, components/ and the other source directories split by subdirectory (and by file-name prefix
    when flat and large); middleware.ts / proxy.ts as their own area."""
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
    if any('/' not in r for r in in_app):
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
    det = detect_conventions.Detector(root, 50_000).run(None)
    all_source = [f for f in files if is_area_code(f)]
    pkgs = sorted({a['root'] for a in det['areas'] if a['root'] != '.'})
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
    root_manifest = next((m for m in ROOT_MANIFESTS if (root / m).is_file()), None)
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
                        'give --usage (downloads or calls per package or entry point) or a journey map')
OUTSIDE_REASON ='outside the workspace packages and the main source tree (root tooling and scripts); add it to --areas to score it'


def is_test(path: str) -> bool:
    if in_runner_dir(path):                  # an XCUITest runner app is the product's on-device agent
        return False
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


LIVE_TEST_DIRS = {'live', 'tests-live', 'live-tests', 'live_tests'}
HELPER_DIRS = {'helpers', 'helper', 'support', 'fixtures', '__fixtures__', 'utils', 'testutils', 'test-utils', 'test_utils',
               'mocks', '__mocks__'}


def not_a_counted_test(t: str) -> str | None:
    """Why a file in a test tree doesn't count as a test of the areas it imports, or None when it does: live tests need
    real credentials or services and don't run in a routine CI run; helpers and fixtures in a test tree are not tests."""
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
        hit = {area_of(t, areas)}
        for r in imports.resolved(t):
            hit.add(area_of_dir(r.rstrip('/'), areas) if r.endswith('/') else area_of(r, areas))
        if t.endswith('.rs'):
            hit |= rust_test_areas(t, areas, files)
        for aid in hit - {None}:
            out[aid].add(t)
    return {k: sorted(v) for k, v in out.items()}, skipped, not_counted


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


def journey_links(path: Path | None, files: list[str], imports) -> tuple[list[dict], dict]:
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
    return rx.sub(lambda m: '""' + '\n' * m.group(0).count('\n'), text)


def comment_free(text: str, suffix: str) -> str:
    """The text with comments blanked but string literals kept (newlines kept): what raw_patterns run on, so SQL or a
    device command inside a string counts and the same words in a comment do not."""
    rx = PY_TOKENS if suffix in HASH_COMMENT else JS_TOKENS
    return rx.sub(lambda m: (' ' + '\n' * m.group(0).count('\n')) if m.group(0).startswith(('#', '//', '/*'))
                  else m.group(0), text)


RUST_TEST_CFG = re.compile(r'^#\[cfg\((?:test\)|all\(\s*test\b)')


def rust_test_mod_cut(raw_lines: list[str]) -> int | None:
    """Line index at which a trailing top-level `#[cfg(test)] mod ... { ... }` begins, or None. Rust
    keeps unit tests inline in the source file, conventionally in a module at the end; that is test
    code, not a surface, so it is dropped before scanning (the integration tests under tests/ are
    linked separately). Only a top-level (column 0) cfg(test) attribute followed by a `mod` is cut."""
    for i, line in enumerate(raw_lines):
        if RUST_TEST_CFG.match(line):
            nxt = next((raw_lines[j] for j in range(i + 1, len(raw_lines)) if raw_lines[j].strip()), '')
            if re.match(r'(pub\s+)?mod\s+\w', nxt):
                return i
    return None


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
                           {ext: _any(pats) for ext, pats in (spec.get('lang_exclude') or {}).items()})
                     for cls, spec in SURFACES['classes'].items()}
    return _COMPILED


def line_classes(raw: str, code: str, suffix: str) -> list[str]:
    """The surface classes one line matches: patterns on the code view (comments and strings blanked), raw_patterns on
    the line as written, minus the class's excludes; a TypeScript member signature matches nothing."""
    if not raw.strip() or (suffix in TS_EXT and TS_SIGNATURE.match(code)):
        return []
    out = []
    for cls, (pat, rawp, excl, langs, lang_excl) in compiled_surfaces().items():
        if langs and suffix not in langs:        # the systems/CLI pack is Rust-only
            continue
        if not ((pat and pat.search(code)) or (rawp and rawp.search(raw))):
            continue
        lx = lang_excl.get(suffix)               # web-tuned idioms the language reuses harmlessly
        if (excl and excl.search(code)) or (lx and lx.search(code)):
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


def surface_scan(root: Path, files: list[str], entries: dict[str, str] | None = None) -> tuple[int, list[str], list[str]]:
    """Surface classes found in the area's files. Patterns run on code with comments and strings blanked;
    raw_patterns (SQL statements, the 'use server' directive) run on the line as written. Type declaration files
    (.d.ts) declare, they don't do, so they are skipped. `entries` (manifest_entries) are the files a package manifest
    names as public entry points; they match the class with manifest_entries set."""
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
        try:
            text = (root / f).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        suffix = PurePosixPath(f).suffix
        cut = rust_test_mod_cut(text.splitlines()) if suffix == '.rs' else None   # a trailing #[cfg(test)] mod
        raw_lines = comment_free(text, suffix).splitlines()     # raw_patterns see strings, not comments
        code_lines = code_view(text, suffix).splitlines()
        if cut is not None:
            raw_lines, code_lines = raw_lines[:cut], code_lines[:cut]
        for i, (raw, code) in enumerate(zip(raw_lines, code_lines), 1):
            for cls in line_classes(raw, code, suffix):
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
        if sfx in CODE_EXT and not is_test(f) and cx.get(f, {}).get('loc', 0) > 0 and \
                diff_scope.classify(f, '', False, root)[0] not in ('generated', 'vendored'):
            acc = by_lang.setdefault(sfx, [0, 0])
            acc[0] += 1
            acc[1] += 1 if owner.get(f) is not None else 0
    if not by_lang:
        return None
    dominant = max(sorted(by_lang), key=lambda k: by_lang[k][0])
    total, inside = by_lang[dominant]
    if total >= 5 and inside < total / 2:
        return (f'the dominant language\'s code is not in the areas: {total - inside} of {total} {dominant} file(s) are '
                f'outside every area (area basis: {area_basis}); the ranking does not cover the product code, so give '
                '--areas before building a plan')
    return None


def score(args) -> dict:
    root = args.repo.resolve()
    hist = json.loads(args.history.read_text(encoding='utf-8'))
    cx = json.loads(args.complexity.read_text(encoding='utf-8'))['files']
    scope = json.loads(args.scope.read_text(encoding='utf-8')) if args.scope else None
    # history.json lists every tracked file (mine_history adds the ones the window didn't touch, with zero counts)
    files = sorted(p for p in hist['files'] if (root / p).is_file())
    PRODUCT_DIRS.clear()
    PRODUCT_DIRS.update(product_dirs(root, files))
    set_runner_dirs(root, files)
    areas, area_basis = define_areas(root, files, args.areas)
    warnings = []
    excluded = []
    for f in files:
        kind, why = diff_scope.classify(f, '', False, root)
        if kind in ('generated', 'vendored') and area_of(f, areas):
            excluded.append({'path': f, 'reason': why})
        elif PurePosixPath(f).suffix in CODE_EXT and not is_test(f) and not is_area_code(f) and area_of(f, areas):
            excluded.append({'path': f, 'reason': 'tool configuration file' if CONFIG_NAME.match(PurePosixPath(f).name)
                             else 'documentation or examples directory'})
    excluded_paths = {e['path'] for e in excluded}
    owner = {f: area_of(f, areas) for f in files}
    # Source files no area covers are never dropped silently: they form the 'unassigned' area, and a warning names them.
    # Empty files (an empty __init__.py) and files outside the main source tree in the default layout are not counted.
    in_tree = (lambda f: True) if args.areas else (lambda f: any(f.startswith(p.rstrip('/*').rsplit('/', 1)[0] + '/')
                                                                   for a in areas for p in a['paths']))
    unassigned = sorted(f for f in files if owner[f] is None and is_source(f) and f not in excluded_paths
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
        if kind in ('generated', 'vendored'):
            reason = why
        elif CONFIG_NAME.match(p.name):
            reason = 'tool configuration file'
        elif names & DOC_DIRS:
            reason = 'documentation or examples directory'
        elif names & NOT_SOURCE_DIRS:
            reason = f'{sorted(names & NOT_SOURCE_DIRS)[0]}/ directory (fixtures, mocks, stories, static files or migrations)'
        else:
            reason = OUTSIDE_REASON
        outside.append({'path': f, 'reason': reason})
    excluded += outside
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
    lang_cov, note = language_note(root)
    if note:
        warnings.append(note)
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
    journeys, jscope = journey_links(args.journeys, files, imports)
    have_map = jscope['present']
    usage = json.loads(args.usage.read_text(encoding='utf-8')) if args.usage else None
    bugs = []
    if args.defects:
        data = json.loads(args.defects.read_text(encoding='utf-8'))
        bugs = [t for t in data.get('tickets', [data]) if (t.get('type') or '').lower() in ('bug', 'defect')]
    tests, flaky_skipped, tests_not_counted = tests_by_area(files, areas, flaky_files, imports)
    coverage: dict[str, tuple[int, int]] = {}
    for prof in args.coverage:
        got = read_coverage(root, prof)
        if not got:
            warnings.append(f'--coverage {prof.name}: no line mapped to a repository file (is it a Go coverprofile of this '
                            'module?); test weakness falls back to the test-size ratio')
        for k, (n, c) in got.items():
            o = coverage.get(k, (0, 0))
            coverage[k] = (max(o[0], n), max(o[1], c))
    entries = manifest_entries(root, files)
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
    raw = {}
    for a in areas:
        mine = [f for f in files if owner[f] == a['id'] and is_area_code(f) and f not in excluded_paths]
        mine_set = set(mine)
        h = [hist['files'][f] for f in mine]
        added_in_scope = [f for f in mine if scope_files.get(f, {}).get('status') == 'added']
        novel = bool(mine) and len(added_in_scope) == len(mine)
        loc = sum(cx.get(f, {}).get('loc', 0) for f in mine)
        branches = sum(cx.get(f, {}).get('branches', 0) for f in mine)
        code_files = [f for f in mine if cx.get(f, {}).get('loc', 0) > 0]
        test_loc = sum(cx.get(t, {}).get('loc', 0) for t in tests[a['id']])
        scope_lines = sum(scope_files[f]['lines_added'] + scope_files[f]['lines_deleted'] for f in mine if f in scope_files)
        fix_shas = sorted({s for x in h for s in x['fix_commits']})
        commit_keys = {k for s in fix_shas if s in commits for k in commits[s]['keys']}
        area_bugs = [t for t in bugs if a['id'].lower() in {x.lower() for x in (t.get('components') or []) + (t.get('labels') or [])}
                     and t['key'] not in commit_keys]
        raw[a['id']] = {
            'files': mine, 'novel': novel, 'loc': loc, 'branches': branches,
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
        }
    # A package with a breaking pending changeset (major, or a "Breaking" note) flags the areas of it the scope changed;
    # when the scope changed none of them (or there is no scope), every area of the package.
    changed_pkgs = {n for r in raw.values() if r['scope_lines'] for n in r['packages']}
    live = [k for k in raw if raw[k]['files']]
    vals = lambda key, only=None: [raw[k][key] for k in live if (only is None or only(k))]  # noqa: E731
    hist_ok = not hist['shallow']
    out_areas = []
    for a in areas:
        r = raw[a['id']]
        if not r['files']:
            continue
        f = {}
        if not hist_ok:
            na = {'score': None, 'status': 'not_assessed', 'evidence': ['shallow clone: fetch more history (git fetch --unshallow)']}
            f['churn'] = dict(na)
            f['defects'] = dict(na)
        elif r['novel']:
            f['churn'] = {'score': None, 'status': 'not_assessed', 'evidence': ['no history before the scope: every file is new']}
            f['defects'] = {'score': None, 'status': 'not_assessed', 'evidence': ['no history before the scope: every file is new']}
        else:
            hv = lambda key: vals(key, lambda k: not raw[k]['novel'])  # noqa: E731
            f['churn'] = {'score': pct_score(r['churn'], hv('churn')), 'status': 'assessed', 'command': cmd,
                          'evidence': [f'{r["churn"]} lines changed in {r["commits"]} commits by {len(r["authors"])} author(s), '
                                       f'{window["from"]} to {window["to"]} (formatting-only commits excluded)']}
            n_def = lambda k: len(raw[k]['fixes']) + len(raw[k]['bugs'])  # noqa: E731
            f['defects'] = {'score': pct_score(n_def(a['id']), [n_def(k) for k in live if not raw[k]['novel']]),
                            'status': 'assessed', 'command': cmd,
                            'evidence': [f'{len(r["fixes"])} fix commit(s) in the window'
                                         + (f' and {len(r["bugs"])} bug ticket(s) by component or label' if r['bugs'] else '')]
                                        + r['fix_subjects'][:8] + r['bugs'][:5] +
                                        ([f'linked issues: {", ".join(r["keys"])}'] if r['keys'] else [])}
        f['complexity'] = {'score': pct_score(r['branch_density'], vals('branch_density')), 'status': 'assessed',
                           'evidence': [f'{r["branches"]} branches in {len(r["code_files"])} file(s), {r["branch_density"]} per file; '
                                        f'{r["loc"]} lines of code (complexity.py)']}
        if scope is not None:
            pct = pct_score(r['scope_lines'], vals('scope_lines'))
            cap = scope_cap(r['scope_lines'])
            f['change_in_scope'] = {'score': min(pct, cap), 'status': 'assessed',
                                    'command': (scope.get('source') or {}).get('command') or 'diff_scope.py',
                                    'evidence': [f'{r["scope_lines"]} lines changed in the scope diff'
                                                 + (f' (percentile {pct}, capped at {cap} for a change this size)' if cap < pct else '')]}
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
        if not r['tests']:
            tw, tev = 5, ['no test imports this area' + (f' ({len(flaky_skipped)} flaky test file(s) not counted)' if flaky_skipped else '')]
        else:
            ratio = r['test_loc'] / r['loc'] if r['loc'] else 1
            tw = 4 if ratio < 0.2 else 3 if ratio < 0.5 else 2 if ratio < 1 else 1
            tev = [f'{len(r["tests"])} test file(s) import it ({", ".join(r["tests"][:4])}); {r["test_loc"]} test lines for '
                   f'{r["loc"]} lines of code (ratio {ratio:.2f})']
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
            f['test_weakness'] = {'score': tw, 'status': 'assessed', 'evidence': tev}
        linked = [j for j in journeys if a['id'] in j['areas']]
        # A partial journey map (run scoped to some domains, or items declared out_of_scope) says nothing about the
        # areas it didn't map: those are not_assessed, not "no journey". An area the map did reach stays assessed.
        unmapped = have_map and not linked and jscope['partial'] and not (set(r['files']) & jscope['mapped_files'])
        partial_note = (f'journey map is partial (domains: {", ".join(jscope["domains"]) if isinstance(jscope["domains"], list) else jscope["domains"]}'
                        f'; {len(jscope["out_of_scope_files"])} file(s) out of scope or not mapped this run) and does not map this area')
        if have_map and not unmapped:
            best = max([PRIORITY_SCORE.get(j['priority'], 1) for j in linked], default=1)
            f['journey_criticality'] = {'score': best, 'status': 'assessed',
                                        'evidence': [f'{j["id"]} ({j["priority"]}) {j["title"]} - {j["areas"][a["id"]]}' for j in linked] or
                                                    ['journey map present; no journey cites this area']}
        elif unmapped:
            f['journey_criticality'] = {'score': None, 'status': 'not_assessed', 'evidence': [partial_note]}
        else:
            f['journey_criticality'] = {'score': None, 'status': 'not_assessed', 'evidence': ['no journey map: run journey-mapper']}
        s, classes, sev = surface_scan(root, r['files'], entries)
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
        flags = (['novel'] if r['novel'] else []) + (['breaking_change'] if breaking else []) + finding_flags(findings)
        area = {'id': a['id'], 'paths': a['paths'], 'files': len(r['files']),
                'journeys': [j['id'] for j in linked], 'surfaces': classes, 'flags': flags, 'open_findings': findings,
                'tests': r['tests'], 'factors': {k: f[k] for k in list(LIKELIHOOD) + list(IMPACT)}}
        area.update(recompute(area))
        out_areas.append(area)
    out_areas.sort(key=lambda a: (-(a['risk'] or 0), a['id']))
    ui = [a['id'] for a in out_areas if any(PurePosixPath(f).suffix in UI_EXT for f in raw[a['id']]['files'])]
    if ui and not have_map and not usage:
        warnings.append(f'no journey map: the impact of {len(ui)} UI area(s) ({", ".join(ui[:6])}'
                        + (f' +{len(ui) - 6}' if len(ui) > 6 else '') + ') rests on surface patterns alone, so a page that '
                        'drives an important flow can rank Low; run journey-mapper (or give --usage) before trusting the UI '
                        'ranking, and say so in the plan')
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
            'area_basis': area_basis, 'unassigned': unassigned, 'warnings': warnings + list(hist.get('notes') or []),
            'excluded': excluded, 'flaky_tests_not_counted': flaky_skipped, 'tests_not_counted': tests_not_counted,
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
    checks = go_layout_checks()
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
