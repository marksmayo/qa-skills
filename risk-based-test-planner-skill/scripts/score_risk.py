#!/usr/bin/env python3
"""Score every area of a repository for risk = likelihood x impact, citing the evidence for each factor (stdlib only).

    python score_risk.py --repo <repo> --history history.json --complexity complexity.json --scope scope.json \
        --out risk.json [--journeys journey_map.json] [--input security=security_audit.json] [--input flaky=flaky_report.json]
        [--input crash=crash_report.json] [--input wcag=audit.json] [--usage usage.json] [--areas areas.json]
        [--defects bugs.json]

Areas are the workspace packages when the repo has several; for a Next.js App Router project,
one per API resource (app/api/<x>), one per top-level route segment or group, app (root),
middleware/proxy, and lib/, components/ and the other source directories split by subdirectory
(and by file-name prefix when flat and large); otherwise the immediate subdirectories of the
main source directory (the top-level directory holding the most source files). --areas gives
them explicitly as {"areas": [{"id": ..., "paths": [...]}]}. Test, generated, vendored and docs
files are not areas. Source files no area covers are scored as the area "unassigned" and named
in a warning, never dropped.

Tests are linked to areas through their imports, resolved to files (so a top-level 'src/*' area
matches only files directly in src/), and by sitting inside an area. Journeys are linked through
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
another skill makes the area High; novel code is at least Medium.

Also usable as a module: allocate(planned, budget) is the effort allocation the validator
recomputes.
"""
from __future__ import annotations
import argparse, ast, json, math, re, sys
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import detect_conventions, diff_scope  # noqa: E402

SURFACES = json.loads((HERE.parent / 'data' / 'surfaces.json').read_text(encoding='utf-8'))
LIKELIHOOD = {'churn': 2, 'defects': 3, 'complexity': 1, 'change_in_scope': 2, 'test_weakness': 2}
IMPACT = {'journey_criticality': 3, 'surface': 3, 'reach': 1}
PRIORITY_SCORE = {'P0': 5, 'P1': 4, 'P2': 3, 'P3': 2}
SEVERE = {'critical', 'high'}
CODE_EXT = {'.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.java', '.kt', '.go', '.cs', '.swift', '.dart', '.rb', '.php',
            '.scala', '.rs', '.vue', '.svelte', '.sql'}


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


def band_of(risk: float | None, flags: list[str]) -> str:
    band = 'Low' if risk is None else 'High' if risk >= 15 else 'Medium' if risk >= 8 else 'Low'
    if 'open_findings' in flags:
        band = 'High'
    if 'novel' in flags and band == 'Low':
        band = 'Medium'
    return band


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


def allocate(planned: list[dict], budget: int | None, high_in_scope: int | None = None) -> tuple[dict, int, dict]:
    """planned: [{id, risk, band}]. Returns ({id: sessions or None}, sessions_short, {id: share}).

    sessions_short counts every High area in scope (high_in_scope), planned or not: leaving a High
    area out of the plan does not make the budget enough."""
    total = sum(a['risk'] or 0 for a in planned) or 1
    shares = {a['id']: round((a['risk'] or 0) / total, 3) for a in planned}
    if budget is None:
        return {a['id']: None for a in planned}, 0, shares
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
                   'docs', 'examples', 'public', 'static', 'migrations', 'node_modules'}
NEXT_ENTRY = re.compile(r'(?:^|/)(?:page|layout|route|template|loading|error|not-found|default)\.(?:tsx?|jsx?|mjs)$')
NEXT_GUARD = ('middleware', 'proxy')
SPLIT_FLAT = 8          # a source directory with more flat files than this is split by file-name prefix


def is_source(f: str) -> bool:
    p = PurePosixPath(f)
    return (p.suffix in CODE_EXT and not is_test(f) and not f.endswith('.d.ts') and not set(p.parts[:-1]) & NOT_SOURCE_DIRS
            and not re.match(r'.*\.(config|setup)\.\w+$', p.name))


def prefix_token(stem: str) -> str:
    return re.split(r'[-_.]|(?<=[a-z0-9])(?=[A-Z])', stem)[0].lower() or stem.lower()


def split_dir(d: str, source: list[str], label: str) -> list[dict]:
    """Areas for one source directory: a subdirectory each; flat files grouped by file-name prefix (stripe.ts and
    stripe-webhooks.ts together) when there are many, the rest as '<label> (top level)'."""
    inside = [f for f in source if f.startswith(d + '/')]
    subs = sorted({f[len(d) + 1:].split('/')[0] for f in inside if f[len(d) + 1:].count('/') >= 1})
    flat = sorted(f for f in inside if f[len(d) + 1:].count('/') == 0 and PurePosixPath(f).name != '__init__.py')
    if not subs and len(flat) <= SPLIT_FLAT:
        return [{'id': label, 'paths': [d]}] if flat else []
    areas = [{'id': f'{label}/{s}', 'paths': [f'{d}/{s}']} for s in subs]
    if len(flat) > SPLIT_FLAT:
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


def define_areas(root: Path, files: list[str], explicit: Path | None) -> tuple[list[dict], str]:
    if explicit:
        return json.loads(explicit.read_text(encoding='utf-8'))['areas'], 'explicit (--areas)'
    det = detect_conventions.Detector(root, 50_000).run(None)
    pkgs = sorted({a['root'] for a in det['areas'] if a['root'] != '.'})
    if len(pkgs) > 1:
        return [{'id': p, 'paths': [p]} for p in pkgs], 'workspace packages'
    nxt = next_app_areas(root, files, [f for f in files if is_source(f)])
    if nxt:
        return nxt, 'Next.js App Router (route segments, API resources, source modules)'
    source = [f for f in files if PurePosixPath(f).suffix in CODE_EXT and not is_test(f) and '/' in f]
    tops: dict[str, int] = {}
    for f in source:
        tops[f.split('/')[0]] = tops.get(f.split('/')[0], 0) + 1
    if not tops:
        return [], 'none'
    main_root = max(sorted(tops), key=lambda t: tops[t])
    subs = sorted({f.split('/')[1] for f in source if f.startswith(main_root + '/') and f.count('/') >= 2})
    areas = [{'id': s, 'paths': [f'{main_root}/{s}']} for s in subs]
    if any(f.count('/') == 1 and f.startswith(main_root + '/') and PurePosixPath(f).name != '__init__.py' for f in source):
        areas.append({'id': f'{main_root} (top level)', 'paths': [f'{main_root}/*']})
    return areas, f'subdirectories of the main source directory {main_root}/'


def is_test(path: str) -> bool:
    return detect_conventions.is_test_file(path) or bool(set(PurePosixPath(path).parts[:-1]) & {'tests', 'test', '__tests__', 'spec'})


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

    def resolve(self, t: str) -> str | None:
        found = self._resolve(t)
        if found is None and not t.startswith('src/'):     # Python src layout: import pkg.mod -> src/pkg/mod.py
            found = self._resolve('src/' + t)
        return found

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
            self.cache[f] = sorted({r for r in (self.resolve(t) for t in self.targets(f)) if r})
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


def tests_by_area(files: list[str], areas: list[dict], flaky: set[str], imports: Imports) -> tuple[dict, list[str]]:
    """{area: [test files that exercise it]}, and flaky test files left out. A test exercises an area when it imports a
    file of the area (resolved to the file, so a top-level 'src/*' area matches only files directly in src/), imports
    a package directory inside the area, or sits inside the area itself (a co-located test)."""
    out: dict[str, set[str]] = {a['id']: set() for a in areas}
    skipped = []
    for t in files:
        if not is_test(t) or PurePosixPath(t).suffix not in CODE_EXT:
            continue
        if t in flaky:
            skipped.append(t)
            continue
        hit = {area_of(t, areas)}
        for r in imports.resolved(t):
            hit.add(area_of_dir(r.rstrip('/'), areas) if r.endswith('/') else area_of(r, areas))
        for aid in hit - {None}:
            out[aid].add(t)
    return {k: sorted(v) for k, v in out.items()}, skipped


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
    meta = {'skill': kind, 'path': path.name, 'generated_at': data.get('generated_at') or (data.get('metadata') or {}).get('generated_at')}
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


def surface_scan(root: Path, files: list[str]) -> tuple[int, list[str], list[str]]:
    """Surface classes found in the area's files. Patterns run on code with comments and strings blanked;
    raw_patterns (SQL statements, the 'use server' directive) run on the line as written. Type declaration files
    (.d.ts) declare, they don't do, so they are skipped."""
    hits: dict[str, list[str]] = {}
    compiled = {cls: ([re.compile(p, re.I) for p in spec.get('patterns', [])],
                      [re.compile(p, re.I) for p in spec.get('raw_patterns', [])],
                      [re.compile(p, re.I) for p in spec.get('exclude', [])]) for cls, spec in SURFACES['classes'].items()}
    for f in files:
        if f.endswith('.d.ts'):
            continue
        try:
            text = (root / f).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        raw_lines = text.splitlines()
        code_lines = code_view(text, PurePosixPath(f).suffix).splitlines()
        for i, (raw, code) in enumerate(zip(raw_lines, code_lines), 1):
            for cls, (pats, raws, excl) in compiled.items():
                if (any(p.search(code) for p in pats) or any(p.search(raw) for p in raws)) and not any(p.search(code) for p in excl):
                    hits.setdefault(cls, []).append(f'{f}:{i}')
    if not hits:
        return SURFACES['none_score'], [], ['no surface pattern matched in data/surfaces.json']
    best = max(hits, key=lambda c: SURFACES['classes'][c]['score'])
    ev = [f'{SURFACES["classes"][c]["label"]}: {", ".join(hits[c][:3])}' + (f' (+{len(hits[c]) - 3})' if len(hits[c]) > 3 else '')
          for c in sorted(hits, key=lambda c: -SURFACES['classes'][c]['score'])]
    return SURFACES['classes'][best]['score'], sorted(hits), ev


# ---------------------------------------------------------------- scoring
def score(args) -> dict:
    root = args.repo.resolve()
    hist = json.loads(args.history.read_text(encoding='utf-8'))
    cx = json.loads(args.complexity.read_text(encoding='utf-8'))['files']
    scope = json.loads(args.scope.read_text(encoding='utf-8')) if args.scope else None
    files = sorted(p for p in hist['files'] if (root / p).is_file())
    areas, area_basis = define_areas(root, files, args.areas)
    warnings = []
    excluded = []
    for f in files:
        kind, why = diff_scope.classify(f, '', False)
        if kind in ('generated', 'vendored') and area_of(f, areas):
            excluded.append({'path': f, 'reason': why})
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
    inputs = [load_input(k, Path(p)) for k, p in (x.split('=', 1) for x in args.input)]
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
    tests, flaky_skipped = tests_by_area(files, areas, flaky_files, imports)
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
        mine = [f for f in files if owner[f] == a['id'] and not is_test(f) and f not in excluded_paths
                and PurePosixPath(f).suffix in CODE_EXT]
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
            'files': mine, 'novel': novel, 'loc': loc, 'branches': branches, 'code_files': len(code_files),
            'churn': sum(x['lines_added'] + x['lines_deleted'] for x in h),
            'commits': len({c['sha'] for c in hist['commits'] if not c['formatting'] and any(fl['path'] in mine_set for fl in c['files'])}),
            'authors': sorted({au for x in h for au in x['authors']}),
            'fixes': fix_shas, 'fix_subjects': [f'{s} {commits[s]["subject"]}' for s in fix_shas if s in commits],
            'keys': sorted(commit_keys),
            'bugs': [f'{t["key"]} ({t.get("type")}, {t.get("status")}) {(t.get("summary") or "")[:80]}' for t in area_bugs],
            'branch_density': round(branches / len(code_files), 2) if code_files else 0.0,
            'scope_lines': scope_lines, 'tests': tests[a['id']], 'test_loc': test_loc,
        }
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
                           'evidence': [f'{r["branches"]} branches in {r["code_files"]} file(s), {r["branch_density"]} per file; '
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
        if not r['tests']:
            tw, tev = 5, ['no test imports this area' + (f' ({len(flaky_skipped)} flaky test file(s) not counted)' if flaky_skipped else '')]
        else:
            ratio = r['test_loc'] / r['loc'] if r['loc'] else 1
            tw = 4 if ratio < 0.2 else 3 if ratio < 0.5 else 2 if ratio < 1 else 1
            tev = [f'{len(r["tests"])} test file(s) import it ({", ".join(r["tests"][:4])}); {r["test_loc"]} test lines for '
                   f'{r["loc"]} lines of code (ratio {ratio:.2f})']
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
        s, classes, sev = surface_scan(root, r['files'])
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
                if it['files'] and any(aof(x) == a['id'] for x in it['files']) and inp['skill'] != 'flaky':
                    findings.append({'skill': inp['skill'], 'id': it['id'], 'title': it['title'], 'severity': it['severity'],
                                     'source_rating': it['source_rating'], 'confidence': it['confidence']})
        flags = (['novel'] if r['novel'] else []) + (['open_findings'] if any(x['severity'] in SEVERE for x in findings) else [])
        area = {'id': a['id'], 'paths': a['paths'], 'files': len(r['files']),
                'journeys': [j['id'] for j in linked], 'surfaces': classes, 'flags': flags, 'open_findings': findings,
                'tests': r['tests'], 'factors': {k: f[k] for k in list(LIKELIHOOD) + list(IMPACT)}}
        area.update(recompute(area))
        out_areas.append(area)
    out_areas.sort(key=lambda a: (-(a['risk'] or 0), a['id']))
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
            'excluded': excluded, 'flaky_tests_not_counted': flaky_skipped,
            'weights': {'likelihood': LIKELIHOOD, 'impact': IMPACT}, 'areas': out_areas}


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
    ap.add_argument('--out', type=Path, required=True)
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
