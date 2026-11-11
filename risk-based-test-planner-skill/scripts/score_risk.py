#!/usr/bin/env python3
"""Score every area of a repository for risk = likelihood x impact, citing the evidence for each factor (stdlib only).

    python score_risk.py --repo <repo> --history history.json --complexity complexity.json --scope scope.json \
        --out risk.json [--journeys journey_map.json] [--input security=security_audit.json] [--input flaky=flaky_report.json]
        [--input crash=crash_report.json] [--input wcag=audit.json] [--usage usage.json] [--areas areas.json]
        [--defects bugs.json]

Areas are the workspace packages when the repo has several, otherwise the immediate
subdirectories of the main source directory (the top-level directory holding the most source
files); --areas gives them explicitly as {"areas": [{"id": ..., "paths": [...]}]}. Test,
generated, vendored and docs files are not areas.

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
def define_areas(root: Path, files: list[str], explicit: Path | None) -> list[dict]:
    if explicit:
        return json.loads(explicit.read_text(encoding='utf-8'))['areas']
    det = detect_conventions.Detector(root, 50_000).run(None)
    pkgs = sorted({a['root'] for a in det['areas'] if a['root'] != '.'})
    if len(pkgs) > 1:
        return [{'id': p, 'paths': [p]} for p in pkgs]
    source = [f for f in files if PurePosixPath(f).suffix in CODE_EXT and not is_test(f) and '/' in f]
    tops: dict[str, int] = {}
    for f in source:
        tops[f.split('/')[0]] = tops.get(f.split('/')[0], 0) + 1
    if not tops:
        return []
    main_root = max(sorted(tops), key=lambda t: tops[t])
    subs = sorted({f.split('/')[1] for f in source if f.startswith(main_root + '/') and f.count('/') >= 2})
    areas = [{'id': s, 'paths': [f'{main_root}/{s}']} for s in subs]
    if any(f.count('/') == 1 and f.startswith(main_root + '/') and PurePosixPath(f).name != '__init__.py' for f in source):
        areas.append({'id': f'{main_root} (top level)', 'paths': [f'{main_root}/*']})
    return areas


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


def tests_by_area(root: Path, files: list[str], areas: list[dict], flaky: set[str]) -> tuple[dict, list[str]]:
    """{area: [test files that import it]}, and flaky test files left out. Relative imports and configured import
    aliases (tsconfig/jsconfig paths, vite or webpack resolve.alias) are followed; mocked modules are not links."""
    out: dict[str, set[str]] = {a['id']: set() for a in areas}
    aliases = js_aliases(root, files)
    skipped = []
    for t in files:
        if not is_test(t) or PurePosixPath(t).suffix not in CODE_EXT:
            continue
        if t in flaky:
            skipped.append(t)
            continue
        text = (root / t).read_text(encoding='utf-8', errors='replace')
        targets = set()
        if t.endswith('.py'):
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for n in ast.walk(tree):
                mods = [a.name for a in n.names] if isinstance(n, ast.Import) else \
                    [f'{n.module}.{a.name}' for a in n.names] + [n.module] if isinstance(n, ast.ImportFrom) and n.module else []
                for m in mods:
                    targets.add(m.replace('.', '/'))
        else:
            for m in re.finditer(r'''(?:from|require\(|import\()\s*["'](\.[^"']+)["']''', text):
                targets.add((PurePosixPath(t).parent / m.group(1)).as_posix())
            for m in re.finditer(r'''(?:from|require\(|import\()\s*["']([^."'][^"']*)["']''', text):
                spec = m.group(1)
                for cfg, alias, target in aliases:
                    if (not cfg or t.startswith(cfg + '/')) and (spec == alias or spec.startswith(alias + '/')):
                        targets.add((target + spec[len(alias):]).strip('/'))
                        break
        for tgt in targets:
            for a in areas:
                if any(tgt == p or tgt.startswith(p.rstrip('/*') + '/') for p in a['paths']):
                    out[a['id']].add(t)
    return {k: sorted(v) for k, v in out.items()}, skipped


# ---------------------------------------------------------------- inputs from other skills
def load_input(kind: str, path: Path) -> dict:
    data = json.loads(path.read_text(encoding='utf-8'))
    meta = {'skill': kind, 'path': path.name, 'generated_at': data.get('generated_at') or (data.get('metadata') or {}).get('generated_at')}
    items = []
    if kind == 'security':
        for f in data.get('findings', []):
            sev = ((f.get('cvss') or {}).get('severity') or f.get('severity') or '').lower()
            items.append({'id': f['id'], 'title': f.get('title'), 'severity': sev, 'confidence': f.get('confidence'),
                          'files': [l['file'] for l in f.get('locations', []) if l.get('file')]})
    elif kind == 'crash':
        for f in data.get('crashes', []):
            band = ((f.get('ranking') or {}).get('band') or f.get('priority') or '')
            sev = 'high' if band in ('P0', 'P1') else 'medium'
            items.append({'id': f['id'], 'title': f.get('title'), 'severity': sev, 'confidence': f.get('confidence'),
                          'files': [l['file'] for l in f.get('locations', []) if isinstance(l, dict) and l.get('file')]})
    elif kind == 'wcag':
        for f in data.get('findings', []):
            files = [x for x in (f.get('component'),) if isinstance(x, str) and '/' in x]
            items.append({'id': f['id'], 'title': f.get('title'), 'severity': (f.get('severity') or '').lower(),
                          'confidence': f.get('confidence'), 'files': files})
    elif kind == 'flaky':
        for f in data.get('flakes', []):
            tid = f.get('test_id', '')
            file = tid.split('::')[0]
            if not file.endswith('.py') and '.' in file:
                file = file.replace('.', '/') + '.py'
            items.append({'id': f['id'], 'test_file': file, 'confidence': f.get('confidence'), 'severity': None, 'files': []})
    return {**meta, 'items': items}


def journey_links(path: Path | None) -> tuple[list[dict], bool]:
    if not path:
        return [], False
    data = json.loads(path.read_text(encoding='utf-8'))
    out = []
    for j in data.get('journeys', []):
        files = {e['file'] for s in j.get('steps', []) + j.get('variants', []) for e in s.get('evidence', []) if e.get('file')}
        out.append({'id': j['id'], 'title': j.get('title'), 'priority': j.get('priority'), 'files': sorted(files)})
    return out, True


def surface_scan(root: Path, files: list[str]) -> tuple[int, list[str], list[str]]:
    hits: dict[str, list[str]] = {}
    for f in files:
        try:
            lines = (root / f).read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            for cls, spec in SURFACES['classes'].items():
                if any(re.search(p, line, re.I) for p in spec['patterns']) and not any(re.search(p, line, re.I) for p in spec.get('exclude', [])):
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
    areas = define_areas(root, files, args.areas)
    excluded = []
    for f in files:
        kind, why = diff_scope.classify(f, '', False)
        if kind in ('generated', 'vendored') and area_of(f, areas):
            excluded.append({'path': f, 'reason': why})
    excluded_paths = {e['path'] for e in excluded}
    inputs = [load_input(k, Path(p)) for k, p in (x.split('=', 1) for x in args.input)]
    flaky_files = {i['test_file'] for inp in inputs if inp['skill'] == 'flaky' for i in inp['items']}
    journeys, have_map = journey_links(args.journeys)
    usage = json.loads(args.usage.read_text(encoding='utf-8')) if args.usage else None
    bugs = []
    if args.defects:
        data = json.loads(args.defects.read_text(encoding='utf-8'))
        bugs = [t for t in data.get('tickets', [data]) if (t.get('type') or '').lower() in ('bug', 'defect')]
    tests, flaky_skipped = tests_by_area(root, files, areas, flaky_files)
    scope_files = {f['path']: f for f in (scope or {}).get('files', [])}
    window = hist['window']
    cmd = hist['command']
    raw = {}
    for a in areas:
        mine = [f for f in files if area_of(f, areas) == a['id'] and not is_test(f) and f not in excluded_paths
                and PurePosixPath(f).suffix in CODE_EXT]
        h = [hist['files'][f] for f in mine]
        added_in_scope = [f for f in mine if scope_files.get(f, {}).get('status') == 'added']
        novel = bool(mine) and len(added_in_scope) == len(mine)
        loc = sum(cx.get(f, {}).get('loc', 0) for f in mine)
        branches = sum(cx.get(f, {}).get('branches', 0) for f in mine)
        code_files = [f for f in mine if cx.get(f, {}).get('loc', 0) > 0]
        test_loc = sum(cx.get(t, {}).get('loc', 0) for t in tests[a['id']])
        scope_lines = sum(scope_files[f]['lines_added'] + scope_files[f]['lines_deleted'] for f in mine if f in scope_files)
        fix_shas = sorted({s for x in h for s in x['fix_commits']})
        commits = {c['sha']: c for c in hist['commits']}
        commit_keys = {k for s in fix_shas if s in commits for k in commits[s]['keys']}
        area_bugs = [t for t in bugs if a['id'].lower() in {x.lower() for x in (t.get('components') or []) + (t.get('labels') or [])}
                     and t['key'] not in commit_keys]
        raw[a['id']] = {
            'files': mine, 'novel': novel, 'loc': loc, 'branches': branches, 'code_files': len(code_files),
            'churn': sum(x['lines_added'] + x['lines_deleted'] for x in h),
            'commits': len({c['sha'] for c in hist['commits'] if not c['formatting'] and any(fl['path'] in mine for fl in c['files'])}),
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
            f['change_in_scope'] = {'score': pct_score(r['scope_lines'], vals('scope_lines')), 'status': 'assessed',
                                    'command': (scope.get('source') or {}).get('command') or 'diff_scope.py',
                                    'evidence': [f'{r["scope_lines"]} lines changed in the scope diff']}
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
        linked = [j for j in journeys if any(area_of(x, areas) == a['id'] for x in j['files'])]
        if have_map:
            best = max([PRIORITY_SCORE.get(j['priority'], 1) for j in linked], default=1)
            f['journey_criticality'] = {'score': best, 'status': 'assessed',
                                        'evidence': [f'{j["id"]} ({j["priority"]}) {j["title"]}' for j in linked] or
                                                    ['journey map present; no journey cites this area']}
        else:
            f['journey_criticality'] = {'score': None, 'status': 'not_assessed', 'evidence': ['no journey map: run journey-mapper']}
        s, classes, sev = surface_scan(root, r['files'])
        f['surface'] = {'score': s, 'status': 'assessed', 'evidence': sev}
        if usage and a['id'] in usage.get('areas', {}):
            share = usage['areas'][a['id']]
            f['reach'] = {'score': 5 if share >= 0.5 else 4 if share >= 0.25 else 3 if share >= 0.1 else 2 if share >= 0.02 else 1,
                          'status': 'assessed', 'evidence': [f'{share:.0%} of traffic ({args.usage.name})']}
        elif have_map:
            counts = {k: len([j for j in journeys if any(area_of(x, areas) == k for x in j['files'])]) for k in live}
            f['reach'] = {'score': pct_score(counts[a['id']], list(counts.values())), 'status': 'assessed',
                          'evidence': [f'cited by {counts[a["id"]]} of {len(journeys)} journeys (journey graph)']}
        else:
            f['reach'] = {'score': None, 'status': 'not_assessed', 'evidence': ['no usage export and no journey map']}
        findings = []
        for inp in inputs:
            for it in inp['items']:
                if it['files'] and any(area_of(x, areas) == a['id'] for x in it['files']) and inp['skill'] != 'flaky':
                    findings.append({'skill': inp['skill'], 'id': it['id'], 'title': it['title'], 'severity': it['severity'],
                                     'confidence': it['confidence']})
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
    return 0


if __name__ == '__main__':
    sys.exit(main())
