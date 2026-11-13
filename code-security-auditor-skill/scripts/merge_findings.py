#!/usr/bin/env python3
"""Merge scanner output into one list of candidates for triage.

    python merge_findings.py --root <repo> [--patterns patterns.json] [--secrets secrets.json]
                             [--sarif semgrep.sarif gosec.sarif ...] [--bandit bandit.json]
                             [--eslint eslint.json] [--deps pip-audit.json npm-audit.json osv.json ...]
                             [--npm-lock package-lock.json] [--pnpm-lock pnpm-lock.yaml]
                             [--scanners scanners.json] [--out candidates.json]

Code candidates (CAND-nnn) are merged by sink location (file and line): one line flagged by
three tools is one candidate with three sources and every CWE they reported. Secrets (SEC-nnn)
are merged by fingerprint or location; dependency advisories (DEP-nnn) by package, version and
advisory aliases. Tool rules are mapped to data/pitfalls.json entries where the catalogue lists them.
Secrets that scan_secrets.py found only in git history are candidates too (history_only, git_status
removed, with the commits); test-file fixtures it set aside are counted, not listed.

npm audit advisories are matched against the copies package-lock.json says are installed (--npm-lock,
default <root>/package-lock.json): version is the installed version, range the advisory's own range,
dev whether every copy is a devDependency, via the packages that pull it in, fixed_in the first
fixed version. A package installed in two majors gives one entry per major. For a pnpm workspace,
--pnpm-lock (default <root>/pnpm-lock.yaml, lockfile v6 to v9) gives the same facts to osv-scanner
and pnpm audit advisories: dev is true when every installed copy is reachable only from some
workspace package's devDependencies, and via names the direct dependencies (importer > package)
that pull it in. pnpm audit --json output (the npm audit v6 shape) is read too.

gitleaks: a hit from git history keeps its commit (partialFingerprints.commitSha) in
history_commits with in_git_history yes, and a hit in a file that no longer exists at HEAD is
history_only with git_status removed, as scan_secrets.py records them. A generic gitleaks rule
(generic-api-key) on a line where scan_secrets.py's own rules see no secret (a cache key hash, a
field name, a placeholder) and any gitleaks hit in a test file that scan_secrets.py would set aside
as a low-entropy fixture are listed under secrets_set_aside with the reason, not triaged one by one;
provider-specific rules always stay candidates. --scanners replaces the version a SARIF driver
reports (gitleaks prints a fixed placeholder version) with the one run_scanners.py recorded.

Secret values are never copied: snippets from secret scanners are dropped, and any secret found
in another tool's snippet or message is replaced with [REDACTED].
"""
from __future__ import annotations
import argparse, json, re, sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import sarif  # noqa: E402
import scan_secrets  # noqa: E402
import test_paths  # noqa: E402

CATALOGUE = HERE.parent / 'data' / 'pitfalls.json'
SECRET_TOOLS = ('gitleaks', 'trufflehog', 'detect-secrets', 'ggshield')
ESLINT_SECURITY = re.compile(r'^(security/|no-unsanitized/|security-node/)|^(no-eval|no-implied-eval|no-new-func|react/no-danger)$')

def redact(text: str) -> str:
    if not text:
        return text
    for s in scan_secrets.find_in_line(text):
        text = text.replace(s['value'], '[REDACTED]')
    return text

def norm_path(path: str, root: Path) -> str:
    p = (path or '').replace('\\', '/')
    if p.startswith('file:'):
        p = sarif._uri_to_path(p)
    try:
        if Path(p).is_absolute():
            return Path(p).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        pass
    while p.startswith('./'):
        p = p[2:]
    return p

def tool_index() -> dict[str, str]:
    cat = json.loads(CATALOGUE.read_text(encoding='utf-8'))
    idx: dict[str, str] = {}
    for p in cat['pitfalls']:
        for t in p.get('tools') or []:
            idx.setdefault(t.lower(), p['id'])
    return idx

def map_pitfall(idx: dict, tool: str, rule: str) -> str | None:
    tool = (tool or '').lower().split()[0] if tool else ''
    rule = rule or ''
    for key in (f'{tool}:{rule}', f'{tool}:{rule.split(".")[-1]}', f'{tool}:{rule.split("/")[-1]}'):
        if key.lower() in idx:
            return idx[key.lower()]
    for key, pid in idx.items():  # prefix entries such as checkov:CKV_AWS_
        if key.endswith('_') and f'{tool}:{rule}'.lower().startswith(key):
            return pid
    return None

def code_candidate(file, line, cwe, pitfall, message, snippet, source, end_line=None) -> dict:
    return {'kind': 'code', 'file': file, 'line': line, 'end_line': max(end_line or line or 0, line or 0) or None,
            'cwe': list(cwe or []), 'pitfall_ids': [pitfall] if pitfall else [],
            'message': redact(message or ''), 'snippet': redact((snippet or '')[:200]), 'sources': [source]}

def from_patterns(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding='utf-8'))
    return [code_candidate(c['file'], c['line'], c.get('cwe'), c.get('pitfall_id'), c.get('message'), c.get('snippet'),
                           {'tool': 'scan_pitfalls', 'rule_id': c.get('pitfall_id')}) for c in data.get('candidates', [])]

GENERIC_SECRET_RULES = re.compile(r'(?i)^(generic[-_]api[-_]key|generic[-_]secret|generic)$')

def sarif_commits(path: Path, root: Path) -> dict[tuple, list[str]]:
    """(file, line, rule) -> commit ids, from SARIF partialFingerprints.commitSha (gitleaks history hits)."""
    out: dict[tuple, list[str]] = {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return out
    for run in data.get('runs') or []:
        for r in run.get('results') or []:
            sha = (r.get('partialFingerprints') or {}).get('commitSha')
            loc = ((r.get('locations') or [{}])[0].get('physicalLocation') or {})
            uri = (loc.get('artifactLocation') or {}).get('uri') or ''
            line = (loc.get('region') or {}).get('startLine')
            if sha:
                out.setdefault((norm_path(uri, root), line, r.get('ruleId')), []).append(sha[:12])
    return out

def secret_triage_hint(s: dict, root: Path, tests: test_paths.Classifier) -> str | None:
    """Why a gitleaks hit can be set aside (scan_secrets.py's own placeholder and fixture rules), or None to keep it."""
    if s.get('history_only') or not s.get('file') or not s.get('line'):
        return None
    rule = s.get('type') or ''
    try:
        lines = (root / s['file']).read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return None
    if not 0 < s['line'] <= len(lines):
        return None
    line = lines[s['line'] - 1]
    ours = scan_secrets.find_in_line(line, Path(s['file']).name)
    in_test = tests.is_test(s['file'])
    if not ours and GENERIC_SECRET_RULES.match(rule):
        return ('generic rule; scan_secrets.py sees no secret on this line (an identifier, a hash used as a cache '
                'key, a field name or a placeholder)')
    if in_test and ours and not any(scan_secrets.real_looking({'type': o['type'], 'entropy': scan_secrets.entropy(o['value']),
                                                                'length': len(o['value'])}) for o in ours):
        return 'low-entropy literal in a test file (a fixture, as scan_secrets.py sets aside)'
    if in_test and not ours and GENERIC_SECRET_RULES.match(rule):
        return 'generic rule in a test file; no secret by scan_secrets.py rules'
    return None

def from_sarif(path: Path, root: Path, idx: dict) -> tuple[list[dict], list[dict]]:
    code, secrets = [], []
    commits = sarif_commits(path, root)
    for r in sarif.read(path, root):
        tool = r['tool'] or 'sarif'
        src = {'tool': tool, 'version': r.get('version'), 'rule_id': r.get('rule_id'), 'level': r.get('level')}
        file = norm_path(r['file'], root)
        rule = (r.get('rule_id') or '').lower()
        if tool.lower().startswith(SECRET_TOOLS) or (tool.lower().startswith('trivy') and 'secret' in rule):
            item = {'type': r.get('rule_id') or 'secret', 'file': file, 'line': r['line'], 'column': r.get('column'),
                    'key': None, 'fingerprint': r.get('fingerprint'), 'message': redact(r.get('message') or ''),
                    'sources': [src]}
            shas = sorted(set(commits.get((file, r['line'], r.get('rule_id'))) or []))
            if shas:  # found in a commit: the line number is from that commit, not necessarily HEAD
                item.update(history_commits=shas, in_git_history='yes', history_basis='full')
                if not (root / file).exists():
                    item.update(git_status='removed', history_only=True, history_line=r['line'], line=None,
                                files=[file])
            secrets.append(item)
            continue
        code.append(code_candidate(file, r['line'], r.get('cwe'), map_pitfall(idx, tool, r.get('rule_id')),
                                   r.get('message'), r.get('snippet'), src, r.get('end_line')))
    return code, secrets

def from_bandit(path: Path, root: Path, idx: dict) -> list[dict]:
    data = json.loads(path.read_text(encoding='utf-8'))
    out = []
    for r in data.get('results', []):
        cwe = (r.get('issue_cwe') or {}).get('id')
        src = {'tool': 'bandit', 'rule_id': r['test_id'], 'level': (r.get('issue_severity') or '').lower(),
               'confidence': (r.get('issue_confidence') or '').lower()}
        snippet = next((l.split(' ', 1)[-1] for l in (r.get('code') or '').splitlines()
                        if l.split(' ', 1)[0].strip() == str(r['line_number'])), '')
        out.append(code_candidate(norm_path(r['filename'], root), r['line_number'], [f'CWE-{cwe}'] if cwe else [],
                                  map_pitfall(idx, 'bandit', r['test_id']), r.get('issue_text'), snippet.strip(), src,
                                  max(r.get('line_range') or [r['line_number']])))
    return out

def from_eslint(path: Path, root: Path, idx: dict) -> list[dict]:
    out = []
    for f in json.loads(path.read_text(encoding='utf-8')):
        for m in f.get('messages', []):
            rid = m.get('ruleId') or ''
            if not ESLINT_SECURITY.search(rid):
                continue
            out.append(code_candidate(norm_path(f['filePath'], root), m.get('line'), [], map_pitfall(idx, 'eslint', rid),
                                      m.get('message'), '', {'tool': 'eslint', 'rule_id': rid, 'level': 'error' if m.get('severity') == 2 else 'warning'}))
    return out

# ---------------------------------------------------------------- npm: installed copies and ranges
def _ver(v: str) -> tuple | None:
    m = re.match(r'^\s*v?(\d+)(?:\.(\d+|x|\*))?(?:\.(\d+|x|\*))?(?:-([\w.]+))?', v or '')
    if not m:
        return None
    nums = [int(g) if g and g.isdigit() else 0 for g in m.groups()[:3]]
    return (*nums, 0 if m.group(4) else 1)  # a prerelease sorts before its release

def satisfies(version: str, rng: str) -> bool | None:
    """npm range check for the forms npm audit prints (<, <=, >, >=, =, hyphen ranges, ||, *).
    None when the range can't be parsed."""
    v = _ver(version)
    if v is None or not rng:
        return None
    for alt in rng.split('||'):
        alt = alt.strip()
        if alt in ('', '*'):
            return True
        hy = re.fullmatch(r'(\S+)\s+-\s+(\S+)', alt)
        comps = [('>=', hy.group(1)), ('<=', hy.group(2))] if hy else re.findall(r'(<=|>=|<|>|=)?\s*(v?\d[\w.*-]*)', alt)
        if not comps:
            return None
        ok = True
        for op, ref in comps:
            r = _ver(ref)
            if r is None:
                return None
            op = op or '='
            ok &= {'<': v < r, '<=': v <= r, '>': v > r, '>=': v >= r, '=': v[:3] == r[:3]}[op]
        if ok:
            return True
    return False

def lock_packages(lock: Path | None) -> dict[str, list[dict]]:
    """package name -> installed copies [{version, path, dev, required_by}] from package-lock.json (v2/v3).

    required_by: the package a nested copy sits under, or for a hoisted copy the packages that depend on it."""
    if not lock or not lock.exists():
        return {}
    try:
        data = json.loads(lock.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    out: dict[str, list[dict]] = {}
    pkgs = data.get('packages') or {}
    dependents: dict[str, set[str]] = {}
    for key, meta in pkgs.items():
        if not isinstance(meta, dict):
            continue
        owner = key.rsplit('node_modules/', 1)[1] if 'node_modules/' in key else '(the project)'
        for field in ('dependencies', 'optionalDependencies', 'peerDependencies'):
            for dep in meta.get(field) or {}:
                dependents.setdefault(dep, set()).add(owner)
    for key, meta in pkgs.items():
        if 'node_modules/' not in key or not isinstance(meta, dict) or not meta.get('version'):
            continue
        name = key.rsplit('node_modules/', 1)[1]
        dev = bool(meta.get('dev') or meta.get('devOptional'))
        parent = key.rsplit('/node_modules/', 1)[0].rsplit('node_modules/', 1)[-1] if key.count('node_modules/') > 1 else None
        # a hoisted copy serves the dependents that have no nested copy of their own
        req = [parent] if parent else sorted(o for o in dependents.get(name, set())
                                             if f'node_modules/{o}/node_modules/{name}' not in pkgs)[:5]
        out.setdefault(name, []).append({'version': meta['version'], 'path': key, 'dev': dev, 'required_by': req})
    return out

def _yaml_key(line: str) -> tuple[int, str, str] | None:
    """(indent, key, inline value) of a `key: value` line in a lockfile, with quotes removed."""
    m = re.match(r"^( *)(?:'([^']+)'|\"([^\"]+)\"|([^\s:'\"][^:]*?))\s*:(?:\s+(.*?))?\s*$", line)
    if not m:
        return None
    return len(m.group(1)), m.group(2) or m.group(3) or m.group(4), (m.group(5) or '').strip("'\"")

def _pnpm_node(key: str) -> tuple[str, str] | None:
    """name and version from a pnpm package key: 'hono@4.1.0', '@scope/x@1.0.0(peer@2)', '/x@1.0.0', '/x/1.0.0'."""
    key = key.lstrip('/')
    key = re.sub(r'\(.*$', '', key)
    m = re.match(r'^(@[^/@]+/[^@/]+|[^@/]+)[@/](\d[^\s]*)$', key)
    return (m.group(1), m.group(2)) if m else None

def pnpm_packages(lock: Path | None) -> dict[str, list[dict]]:
    """package name -> installed copies [{version, dev, required_by, importers}] from pnpm-lock.yaml (v6 to v9).

    A copy is dev when only workspace devDependencies reach it; required_by lists the direct dependencies
    (importer>package) through which it is installed."""
    if not lock or not lock.exists():
        return {}
    try:
        lines = lock.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return {}
    roots: list[tuple[str, str, bool]] = []  # (importer, node, dev)
    graph: dict[str, set[str]] = {}
    section = importer = kind = node = None
    sub = None
    for line in lines:
        if not line.strip() or line.lstrip().startswith('#') or line.startswith('---'):
            continue
        k = _yaml_key(line)
        if k is None:
            continue
        indent, key, val = k
        if indent == 0:
            section, importer, kind, node, sub = key, None, None, None, None
            continue
        if section == 'importers':
            if indent == 2:
                importer, kind = key, None
            elif indent == 4:
                kind = key if key in ('dependencies', 'devDependencies', 'optionalDependencies') else None
                cur = None
            elif indent == 6 and kind:
                cur = key
                if val:  # v5 style: name: version
                    v = re.sub(r'\(.*$', '', val)
                    if not v.startswith(('link:', 'file:', 'workspace:')):
                        roots.append((importer, f'{key}@{v}', kind == 'devDependencies'))
            elif indent == 8 and kind and key == 'version' and cur:
                v = re.sub(r'\(.*$', '', val)
                if not v.startswith(('link:', 'file:', 'workspace:')):
                    roots.append((importer, f'{cur}@{v}', kind == 'devDependencies'))
        elif section in ('packages', 'snapshots'):
            if indent == 2:
                nv = _pnpm_node(key)
                node = f'{nv[0]}@{nv[1]}' if nv else None
                if node:
                    graph.setdefault(node, set())
                sub = None
            elif indent == 4:
                sub = key if key in ('dependencies', 'optionalDependencies') else None
            elif indent == 6 and sub and node and val:
                v = re.sub(r'\(.*$', '', val)
                if not v.startswith(('link:', 'file:')):
                    graph[node].add(f'{key}@{v}')
    reach: dict[str, dict] = {}
    for imp, root_node, dev in roots:
        via = f"{imp}>{root_node.rsplit('@', 1)[0]}" if imp not in ('.', None) else root_node.rsplit('@', 1)[0]
        stack, seen = [root_node], set()
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            r = reach.setdefault(n, {'prod': False, 'dev': False, 'via': set(), 'importers': set()})
            r['dev' if dev else 'prod'] = True
            if len(r['via']) < 8:
                r['via'].add(via)
            r['importers'].add(imp)
            stack.extend(graph.get(n, ()))
    out: dict[str, list[dict]] = {}
    for n, r in reach.items():
        name, version = n.rsplit('@', 1)
        out.setdefault(name, []).append({'version': version, 'path': f'pnpm-lock.yaml:{n}', 'dev': r['dev'] and not r['prod'],
                                         'required_by': sorted(r['via'])[:5], 'importers': sorted(i for i in r['importers'] if i)[:5]})
    return out

def annotate_from_lock(items: list[dict], copies: dict[str, list[dict]]) -> None:
    """Fill dev/via/installed on npm advisories (osv-scanner, pnpm audit) from pnpm-lock.yaml facts."""
    for d in items:
        if (d.get('ecosystem') or '').lower() != 'npm' or d.get('dev') is not None:
            continue
        have = [c for c in copies.get(d['package']) or [] if not d.get('version') or c['version'] == d['version']]
        if have:
            d['dev'] = all(c['dev'] for c in have)
            d['via'] = sorted({v for c in have for v in c['required_by']})[:8]
            d['installed'] = have

def fixed_from_range(rng: str) -> list[str]:
    """The first fixed version implied by an upper bound (`<1.2.3` -> 1.2.3)."""
    return sorted({m for m in re.findall(r'<\s*(\d[\w.-]*)', rng or '')})

def from_npm_audit(data: dict, lock: dict[str, list[dict]]) -> list[dict]:
    """One entry per advisory per installed major version, with the installed versions, the advisory's own range,
    the dev/prod flag from the lockfile, and the chain of packages that pull it in (npm audit's effects)."""
    out = []
    vulns = data['vulnerabilities']
    for name, v in vulns.items():
        fix = v.get('fixAvailable')
        fix_info = {'name': fix.get('name'), 'version': fix.get('version'), 'semver_major': bool(fix.get('isSemVerMajor'))} \
            if isinstance(fix, dict) else (bool(fix) if fix is not None else None)
        effects = sorted(v.get('effects') or [])
        copies = lock.get(name) or []
        for via in v.get('via') or []:
            if not isinstance(via, dict):
                continue  # transitive pointer to another package's entry
            url = via.get('url') or ''
            ghsa = re.search(r'GHSA-[\w-]+', url)
            rng = via.get('range') or v.get('range') or ''
            fixed = [fix['version']] if isinstance(fix, dict) and fix.get('name') == name and fix.get('version') else fixed_from_range(rng)
            base = {'ecosystem': 'npm', 'package': name, 'advisory': ghsa.group(0) if ghsa else str(via.get('source')),
                    'aliases': [], 'fixed_in': fixed, 'summary': via.get('title') or '', 'severity': via.get('severity'),
                    'range': rng, 'via': effects, 'fix_available': fix_info}
            hit = [c for c in copies if satisfies(c['version'], rng) is not False]
            if not copies:
                out.append({**base, 'version': v.get('range') or rng, 'installed': [], 'dev': None,
                            'installed_note': 'package-lock.json not given or the package is not in it; version is the vulnerable range'})
                continue
            if not hit:  # npm audit flagged it, but no installed copy is in this advisory's range
                versions = sorted({c['version'] for c in copies}, key=lambda x: _ver(x) or (0,))
                out.append({**base, 'version': ', '.join(versions), 'installed': copies, 'dev': all(c['dev'] for c in copies),
                            'installed_note': f'no installed copy is in the range {rng!r} according to package-lock.json'})
                continue
            by_major: dict[str, list[dict]] = {}
            for c in hit:
                by_major.setdefault(c['version'].split('.')[0], []).append(c)
            for major, cs in sorted(by_major.items()):
                versions = sorted({c['version'] for c in cs}, key=lambda x: _ver(x) or (0,))
                via = sorted(set(effects) | {r for c in cs for r in c.get('required_by') or []})
                out.append({**base, 'version': ', '.join(versions), 'installed': cs, 'dev': all(c['dev'] for c in cs), 'via': via})
    return out

def from_deps(path: Path, lock: dict[str, list[dict]] | None = None) -> tuple[list[dict], dict]:
    data = json.loads(path.read_text(encoding='utf-8'))
    out = []
    if isinstance(data, dict) and 'dependencies' in data and isinstance(data['dependencies'], list):  # pip-audit
        tool = 'pip-audit'
        for dep in data['dependencies']:
            for v in dep.get('vulns') or []:
                out.append({'ecosystem': 'PyPI', 'package': dep['name'], 'version': dep.get('version'), 'advisory': v['id'],
                            'aliases': sorted(v.get('aliases') or []), 'fixed_in': v.get('fix_versions') or [],
                            'summary': ' '.join((v.get('description') or '').split())[:300]})
    elif isinstance(data, dict) and 'vulnerabilities' in data and isinstance(data['vulnerabilities'], dict):  # npm audit v7+
        tool = 'npm audit'
        out = from_npm_audit(data, lock or {})
    elif isinstance(data, dict) and isinstance(data.get('advisories'), dict):  # pnpm audit --json (npm audit v6 shape)
        tool = 'pnpm audit'
        for a in data['advisories'].values():
            ghsa = a.get('github_advisory_id') or (re.search(r'GHSA-[\w-]+', a.get('url') or '') or [None])[0]
            for f in a.get('findings') or [{'version': None, 'paths': []}]:
                via = sorted({'>'.join(x.split('>')[:2]) for x in f.get('paths') or [] if '>' in x})[:8]
                out.append({'ecosystem': 'npm', 'package': a.get('module_name'), 'version': f.get('version'),
                            'advisory': ghsa or str(a.get('id')), 'aliases': sorted(a.get('cves') or []),
                            'fixed_in': fixed_from_range(a.get('vulnerable_versions') or ''),
                            'summary': a.get('title') or '', 'severity': a.get('severity'),
                            'range': a.get('vulnerable_versions') or '', 'via_paths': via})
    elif isinstance(data, dict) and 'results' in data:  # osv-scanner
        tool = 'osv-scanner'
        for res in data['results']:
            for pkg in res.get('packages') or []:
                p = pkg.get('package') or {}
                for v in pkg.get('vulnerabilities') or []:
                    fixed = sorted({e['fixed'] for a in v.get('affected') or [] for rg in a.get('ranges') or []
                                    for e in rg.get('events') or [] if 'fixed' in e})
                    out.append({'ecosystem': p.get('ecosystem'), 'package': p.get('name'), 'version': p.get('version'),
                                'advisory': v['id'], 'aliases': sorted(v.get('aliases') or []), 'fixed_in': fixed,
                                'summary': v.get('summary') or ' '.join((v.get('details') or '').split())[:300],
                                'manifest': (res.get('source') or {}).get('path')})
    else:
        raise ValueError('not pip-audit, npm audit (v7+), pnpm audit or osv-scanner JSON')
    for d in out:
        d['sources'] = [{'tool': tool, 'advisory': d['advisory']}]
    return out, {'tool': tool, 'input': str(path), 'results': len(out)}

SINGLE_LINE_TOOLS = ('scan_pitfalls', 'bandit', 'eslint')
MAX_SPAN = 12  # a range wider than this (a whole function) is too coarse to merge on

def _related(a: dict, b: dict) -> bool:
    if a['file'] != b['file'] or a['line'] is None or b['line'] is None:
        return a['file'] == b['file'] and a['line'] == b['line']
    overlap = a['line'] <= (b['end_line'] or b['line']) and b['line'] <= (a['end_line'] or a['line'])
    narrow = ((a['end_line'] or a['line']) - a['line'] <= MAX_SPAN) and ((b['end_line'] or b['line']) - b['line'] <= MAX_SPAN)
    same_kind = bool(set(a['pitfall_ids']) & set(b['pitfall_ids']) or set(a['cwe']) & set(b['cwe']))
    return overlap and (a['line'] == b['line'] or narrow) and same_kind

def merge_code(cands: list[dict]) -> list[dict]:
    """Merge candidates that point at the same sink: overlapping lines and a shared pitfall or CWE.

    Semgrep reports a multi-line call at its first line; Bandit and the pattern scan report the
    line holding the dangerous expression. The merged candidate keeps the latter as its line.
    """
    out: list[dict] = []
    for c in sorted(cands, key=lambda c: (c['file'], c['line'] or 0)):
        have = next((h for h in out if _related(h, c)), None)
        if not have:
            out.append(c)
            continue
        for k in ('cwe', 'pitfall_ids'):
            have[k] += [x for x in c[k] if x not in have[k]]
        have['sources'] += [s for s in c['sources'] if s not in have['sources']]
        precise = c['sources'][0]['tool'] in SINGLE_LINE_TOOLS and not any(s['tool'] in SINGLE_LINE_TOOLS for s in have['sources'][:-1])
        if precise:
            have['line'], have['snippet'] = c['line'], c['snippet'] or have['snippet']
        have['end_line'] = max(have['end_line'] or 0, c['end_line'] or 0) or None
        if not have['snippet'] and c['snippet']:
            have['snippet'] = c['snippet']
        if c['sources'][0]['tool'] == 'scan_pitfalls':
            have['message'] = c['message']  # the catalogue title reads best
    out.sort(key=lambda c: (c['file'], c['line'] or 0))
    for n, c in enumerate(out, 1):
        c['id'] = f'CAND-{n:03d}'
        c['tools'] = sorted({s['tool'] for s in c['sources']})
    return out

def merge_secrets(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for s in items:
        have = next((h for h in out if (s.get('fingerprint') and h.get('fingerprint') == s['fingerprint'] and h['file'] == s['file'])
                     or (h['file'] == s['file'] and s['line'] is not None and h['line'] == s['line'])), None)
        if have:
            have['sources'] += [x for x in s['sources'] if x not in have['sources']]
            for k, v in s.items():
                if have.get(k) is None and v is not None:
                    have[k] = v
        else:
            out.append(s)
    out.sort(key=lambda s: (bool(s.get('history_only')), s['file'] or '', s['line'] or 0))
    for n, s in enumerate(out, 1):
        s['id'] = f'SEC-{n:03d}'
    return out

def merge_deps(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for d in items:
        ids = {d['advisory'], *d['aliases']}
        have = next((h for h in out if h['package'].lower() == d['package'].lower() and h['version'] == d['version']
                     and ids & {h['advisory'], *h['aliases']}), None)
        if have:
            have['aliases'] = sorted(({*have['aliases'], *ids}) - {have['advisory']})
            have['fixed_in'] = sorted(set(have['fixed_in']) | set(d['fixed_in']))
            have['sources'] += [s for s in d['sources'] if s not in have['sources']]
            if len(d.get('summary') or '') > len(have.get('summary') or ''):
                have['summary'] = d['summary']
        else:
            out.append(dict(d))
    out.sort(key=lambda d: (d['package'].lower(), d['advisory']))
    for n, d in enumerate(out, 1):
        d['id'] = f'DEP-{n:03d}'
    return out

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--root', type=Path, required=True, help='the repository the tools scanned')
    ap.add_argument('--patterns', type=Path)
    ap.add_argument('--secrets', type=Path)
    ap.add_argument('--sarif', nargs='*', type=Path, default=[])
    ap.add_argument('--bandit', type=Path)
    ap.add_argument('--eslint', type=Path)
    ap.add_argument('--deps', nargs='*', type=Path, default=[])
    ap.add_argument('--npm-lock', type=Path, help='package-lock.json for npm audit input (default: <root>/package-lock.json)')
    ap.add_argument('--pnpm-lock', type=Path, help='pnpm-lock.yaml for dev/via facts on npm advisories (default: <root>/pnpm-lock.yaml)')
    ap.add_argument('--scanners', type=Path, help="run_scanners.py's scanners.json: tool versions replace the SARIF drivers' own")
    ap.add_argument('--out', type=Path)
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')  # tool output can hold characters the console can't encode
    root = args.root.resolve()
    idx = tool_index()
    code, secrets, deps, tools, cred_files = [], [], [], [], []
    set_aside: list[dict] = []
    versions: dict[str, str] = {}
    if args.scanners:
        try:
            versions = {t['name'].lower(): t['version'] for t in json.loads(args.scanners.read_text(encoding='utf-8')).get('tools', [])
                        if t.get('version')}
        except (OSError, ValueError) as exc:
            print(f'Unable to read {args.scanners}: {exc}', file=sys.stderr)
            return 2
    try:
        if args.patterns:
            c = from_patterns(args.patterns); code += c
            tools.append({'tool': 'scan_pitfalls', 'input': str(args.patterns), 'results': len(c)})
        if args.secrets:
            data = json.loads(args.secrets.read_text(encoding='utf-8'))
            for s in data.get('secrets', []):
                secrets.append({**s, 'sources': [{'tool': 'scan_secrets', 'rule_id': s['type']}]})
            hist = data.get('history') or {}
            # Secrets only in history are still exposed: they are candidates too (location, commits and status only).
            for h in hist.get('removed_from_head') or []:
                secrets.append({'type': h['type'], 'file': (h.get('files') or [None])[0], 'line': None, 'column': None,
                                'key': h.get('key'), 'length': h.get('length'), 'entropy': h.get('entropy'),
                                'fingerprint': h['fingerprint'], 'files': h.get('files') or [],
                                'history_commits': h.get('commits') or [], 'in_git_history': 'yes', 'history_basis': 'full',
                                'git_status': 'removed', 'history_only': True,
                                'sources': [{'tool': 'scan_secrets', 'rule_id': h['type']}]})
            fixtures = data.get('test_fixtures') or {}
            tools.append({'tool': 'scan_secrets', 'input': str(args.secrets), 'results': len(data.get('secrets', [])),
                          'history': hist.get('status'), 'history_only': len(hist.get('removed_from_head') or []),
                          'test_fixtures': fixtures.get('count', 0)})
            if hist.get('note'):
                tools[-1]['history_note'] = hist['note']
            cred_files += data.get('credential_files', [])
        tests = test_paths.Classifier(root)
        for path in args.sarif:
            c, s = from_sarif(path, root, idx)
            for x in c + s:  # the version run_scanners.py recorded beats a SARIF driver's placeholder
                for src in x['sources']:
                    v = versions.get((src.get('tool') or '').lower().split()[0]) if src.get('tool') else None
                    if v and v != src.get('version'):
                        src['sarif_version'], src['version'] = src.get('version'), v
            kept = []
            for x in s:
                why = secret_triage_hint(x, root, tests)
                if why:
                    set_aside.append({'tool': x['sources'][0]['tool'], 'rule': x['type'], 'file': x['file'], 'line': x['line'],
                                      'reason': why})
                else:
                    kept.append(x)
            code += c; secrets += kept
            names = sorted({x['sources'][0]['tool'] for x in c + s}) or [path.stem]
            tools.append({'tool': ', '.join(names), 'input': str(path), 'results': len(c) + len(s)})
            if len(kept) < len(s):
                tools[-1]['set_aside'] = len(s) - len(kept)
        if args.bandit:
            c = from_bandit(args.bandit, root, idx); code += c
            tools.append({'tool': 'bandit', 'input': str(args.bandit), 'results': len(c)})
        if args.eslint:
            c = from_eslint(args.eslint, root, idx); code += c
            tools.append({'tool': 'eslint', 'input': str(args.eslint), 'results': len(c)})
        lock_path = args.npm_lock or (root / 'package-lock.json')
        lock = lock_packages(lock_path)
        pnpm_path = args.pnpm_lock or (root / 'pnpm-lock.yaml')
        pnpm = pnpm_packages(pnpm_path)
        for path in args.deps:
            d, info = from_deps(path, lock); deps += d; tools.append(info)
            if info['tool'] == 'npm audit':
                info['lockfile'] = str(lock_path) if lock else None
            if pnpm and info['tool'] in ('osv-scanner', 'pnpm audit'):
                annotate_from_lock(d, pnpm)
                info['pnpm_lockfile'] = str(pnpm_path)
    except (OSError, ValueError, KeyError) as exc:
        print(f'Unable to merge: {exc}', file=sys.stderr)
        return 2
    out = {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'repository': root.name,
        'tools': tools,
        'note': 'Candidates, not findings. Promote each to a finding with a traced path, or dismiss it with a checkable reason.',
        'candidates': merge_code(code),
        'secrets': merge_secrets(secrets),
        'dependencies': merge_deps(deps),
        # credential files and references to them (paths, not secrets): note them in the report, no triage needed
        'credential_files': cred_files,
        # gitleaks hits scan_secrets.py's own rules call placeholders or test fixtures: listed, not triaged
        'secrets_set_aside': {'count': len(set_aside), 'items': set_aside},
    }
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + '\n', encoding='utf-8')
        print(f"{len(out['candidates'])} code candidate(s), {len(out['secrets'])} secret(s), "
              f"{len(out['dependencies'])} dependency advisory(ies) from {len(tools)} input(s) -> {args.out}")
        for t in tools:
            print(f"  {t['results']:4}  {t['tool']}")
    else:
        print(text)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
