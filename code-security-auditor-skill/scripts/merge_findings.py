#!/usr/bin/env python3
"""Merge scanner output into one list of candidates for triage.

    python merge_findings.py --root <repo> [--patterns patterns.json] [--secrets secrets.json]
                             [--sarif semgrep.sarif gosec.sarif ...] [--bandit bandit.json]
                             [--eslint eslint.json] [--deps pip-audit.json npm-audit.json osv.json ...]
                             [--out candidates.json]

Code candidates (CAND-nnn) are merged by sink location (file and line): one line flagged by
three tools is one candidate with three sources and every CWE they reported. Secrets (SEC-nnn)
are merged by fingerprint or location; dependency advisories (DEP-nnn) by package, version and
advisory aliases. Tool rules are mapped to data/pitfalls.json entries where the catalogue lists them.

Secret values are never copied: snippets from secret scanners are dropped, and any secret found
in another tool's snippet or message is replaced with [REDACTED].
"""
from __future__ import annotations
import argparse, json, re, sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sarif  # noqa: E402
import scan_secrets  # noqa: E402

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

def from_sarif(path: Path, root: Path, idx: dict) -> tuple[list[dict], list[dict]]:
    code, secrets = [], []
    for r in sarif.read(path, root):
        tool = r['tool'] or 'sarif'
        src = {'tool': tool, 'version': r.get('version'), 'rule_id': r.get('rule_id'), 'level': r.get('level')}
        file = norm_path(r['file'], root)
        rule = (r.get('rule_id') or '').lower()
        if tool.lower().startswith(SECRET_TOOLS) or (tool.lower().startswith('trivy') and 'secret' in rule):
            secrets.append({'type': r.get('rule_id') or 'secret', 'file': file, 'line': r['line'], 'column': r.get('column'),
                            'key': None, 'fingerprint': r.get('fingerprint'), 'message': redact(r.get('message') or ''),
                            'sources': [src]})
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

def from_deps(path: Path) -> tuple[list[dict], dict]:
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
        for name, v in data['vulnerabilities'].items():
            for via in v.get('via') or []:
                if not isinstance(via, dict):
                    continue  # transitive pointer to another package's entry
                url = via.get('url') or ''
                ghsa = re.search(r'GHSA-[\w-]+', url)
                fix = v.get('fixAvailable')
                out.append({'ecosystem': 'npm', 'package': name, 'version': v.get('range'), 'advisory': ghsa.group(0) if ghsa else str(via.get('source')),
                            'aliases': [], 'fixed_in': [fix['version']] if isinstance(fix, dict) and fix.get('version') else [],
                            'summary': via.get('title') or '', 'severity': via.get('severity')})
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
        raise ValueError('not pip-audit, npm audit (v7+) or osv-scanner JSON')
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
                     or (h['file'] == s['file'] and h['line'] == s['line'])), None)
        if have:
            have['sources'] += [x for x in s['sources'] if x not in have['sources']]
            for k, v in s.items():
                if have.get(k) is None and v is not None:
                    have[k] = v
        else:
            out.append(s)
    out.sort(key=lambda s: (s['file'], s['line'] or 0))
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
    ap.add_argument('--out', type=Path)
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')  # tool output can hold characters the console can't encode
    root = args.root.resolve()
    idx = tool_index()
    code, secrets, deps, tools, cred_files = [], [], [], [], []
    try:
        if args.patterns:
            c = from_patterns(args.patterns); code += c
            tools.append({'tool': 'scan_pitfalls', 'input': str(args.patterns), 'results': len(c)})
        if args.secrets:
            data = json.loads(args.secrets.read_text(encoding='utf-8'))
            for s in data.get('secrets', []):
                secrets.append({**s, 'sources': [{'tool': 'scan_secrets', 'rule_id': s['type']}]})
            tools.append({'tool': 'scan_secrets', 'input': str(args.secrets), 'results': len(data.get('secrets', [])),
                          'history': (data.get('history') or {}).get('status')})
            if (data.get('history') or {}).get('note'):
                tools[-1]['history_note'] = data['history']['note']
            cred_files += data.get('credential_files', [])
        for path in args.sarif:
            c, s = from_sarif(path, root, idx); code += c; secrets += s
            names = sorted({x['sources'][0]['tool'] for x in c + s}) or [path.stem]
            tools.append({'tool': ', '.join(names), 'input': str(path), 'results': len(c) + len(s)})
        if args.bandit:
            c = from_bandit(args.bandit, root, idx); code += c
            tools.append({'tool': 'bandit', 'input': str(args.bandit), 'results': len(c)})
        if args.eslint:
            c = from_eslint(args.eslint, root, idx); code += c
            tools.append({'tool': 'eslint', 'input': str(args.eslint), 'results': len(c)})
        for path in args.deps:
            d, info = from_deps(path); deps += d; tools.append(info)
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
