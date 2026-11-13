#!/usr/bin/env python3
"""Scan source for security pitfalls from data/pitfalls.json.

    python scan_pitfalls.py <repo> [--packs python,github_actions] [--out patterns.json]

Every hit is a *candidate*: a line that matches a weakness pattern. It is not a finding until the
agent traces attacker-controlled input to it. Patterns are a fallback and a cross-check for real
analysers (Semgrep, Bandit, gosec, ...), whose output merge_findings.py combines with this one.

A pitfall can narrow its matches with:
  suppress_line   skip when the matching line also matches this
  suppress_near   skip when the lines around the match (window) match this
  require_near    keep only when the lines around the match match this
  require_file    keep only when the file matches this anywhere (e.g. pull_request_target)
  suppress_file   skip when the file matches this anywhere (e.g. a signature check in a webhook handler)
  require_path    keep only when the file's path matches this (e.g. webhook)
  suppress_above  skip when one of the `window` lines above the match matches this (e.g. @secure())
  require_above   keep only when one of the `window` lines above the match matches this
  scope_regex     with scope_suppress: find the nearest line above that matches scope_regex (e.g. the
  scope_suppress  enclosing `- job:` or `- deployment:`) and skip when it matches scope_suppress
  constant_interpolation
                  skip a Python f-string whose every {name} is a module constant (UPPER_CASE, or a
                  literal assigned at module level) or a loop variable over one
A pack can also require_content / exclude_content: a file joins the pack only when its text
matches (or doesn't match) the regex, so .json and .yml files are claimed by ARM templates and
Azure DevOps pipelines only when they are ones.
A pack can limit itself to paths with path_contains (a substring) or path_regex (a regex over the
relative path): the github_actions pack reads workflows and composite actions (action.yml under
.github/, or at the root of an action repository).
Test files, dependencies, build output, minified files and vendored trees (vendor/, third_party/,
Pods/, PrivateHeaders/, bundled .app/.framework directories, an embedded WebDriverAgent: see
test_paths.vendored_dir) are skipped, and vendored files never count toward the repository's main
language. When the main language has no pack, a loud warning says so (and names the languages that
were scanned, which are a result for those languages only). A directory named e2e,
test(s), spec, playwright or cypress is skipped only when it is not a package of its own (see
test_paths.py): a workspace package named e2e is product code and is scanned.
"""
from __future__ import annotations
import argparse, json, re, sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import lang_census  # noqa: E402
import test_paths  # noqa: E402

CATALOGUE = HERE.parent / 'data' / 'pitfalls.json'
SKIP_DIRS = {'node_modules', '.git', 'dist', 'build', 'out', '.next', '.nuxt', '.svelte-kit', '.turbo', '.cache', 'coverage', 'vendor',
             'Pods', 'DerivedData', '.gradle', '.dart_tool', '.idea', '.vscode', '__pycache__', '.venv', 'venv',
             'env', '.tox', '.mypy_cache', '.pytest_cache', 'site-packages', 'target', 'bin', 'obj', '.terraform',
             'test', 'tests', '__tests__', '__mocks__', 'spec', 'e2e', 'cypress', 'testdata', 'fixtures',
             'androidTest', 'UITests', 'Tests'}
ALLOWED_DOT_DIRS = {'.github', '.azuredevops', '.pipelines', '.azure-pipelines', '.ado'}
TEST_FILE = re.compile(r'(^test_.*\.py$|_test\.(py|go)$|(\.|_)(test|spec|e2e)\.\w+$|Tests?\.(java|kt|swift|cs)$|\.min\.js$|'
                       r'^(vitest|jest|playwright)\.(setup|config)\.\w+$|^setupTests\.\w+$)', re.I)
COMMENT = re.compile(r'^\s*(//|/\*|\*\s|#(?!!)|<!--|--\s)')
def load_catalogue(path: Path = CATALOGUE) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))

def pack_of(path: Path, rel: str, packs: dict) -> list[str]:
    out = []
    for name, pk in packs.items():
        if pk.get('path_contains') and pk['path_contains'] not in '/' + rel:
            continue
        if pk.get('path_regex') and not re.search(pk['path_regex'], rel):
            continue
        if (path.suffix in pk.get('extensions', []) or path.name in pk.get('filenames', [])
                or (pk.get('filename_prefix') and path.name.startswith(pk['filename_prefix']))):
            out.append(name)
    return out

def walk(root: Path, include_tests: bool = False, census: dict | None = None):
    """Files to scan. Directories lang_census excludes (vendored, bundled, embedded third-party packages) and
    test_paths.vendored_dir names are pruned before they are walked."""
    tests = test_paths.Classifier(root)
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name in SKIP_DIRS and not (include_tests and p.name in ('test', 'tests', 'spec')):
                    # a directory named e2e or tests that is a package of its own is product code
                    if not (p.name.lower() in test_paths.OVERRIDABLE_DIRS
                            and tests.product_dir(p.relative_to(root).as_posix())):
                        continue
                if p.name.startswith('.') and p.name not in ALLOWED_DOT_DIRS:
                    continue
                if test_paths.vendored_dir(p.name) or (census and lang_census.excluded_path(census, p.relative_to(root).as_posix())):
                    continue  # third-party or bundled code: neither scanned nor counted toward the main language
                stack.append(p)
            elif p.is_file() and (include_tests or not TEST_FILE.search(p.name)):
                yield p

def compile_pitfall(p: dict) -> dict:
    q = dict(p)
    q['_rx'] = re.compile(p['regex'], re.M)
    for k in ('suppress_line', 'suppress_near', 'require_near', 'require_file', 'suppress_file', 'require_path', 'suppress_above',
              'scope_regex', 'scope_suppress', 'require_above'):
        if p.get(k):
            q['_' + k] = re.compile(p[k], re.M)
    return q

FSTRING = re.compile(r'''\b[fF][rR]?("""|\'\'\'|"|')(.*?)(?:\1|$)''')
NEUTRAL_NAMES = {'len', 'str', 'int', 'repr', 'sorted', 'tuple', 'list', 'join', 'upper', 'lower'}

def constant_fstring(line: str, text: str) -> bool:
    """True when every name interpolated into the line's f-strings is a module constant: UPPER_CASE, a
    literal assigned at module level, or a loop variable over one (for table in ARCHIVE_TABLES)."""
    names = []
    for m in FSTRING.finditer(line):
        body = m.group(2).replace('{{', '').replace('}}', '')
        for expr in re.findall(r'\{([^{}]+)\}', body):
            expr = re.split(r'![rsa]|:(?![^\[\(]*[\]\)])', expr)[0]
            expr = re.sub(r'(["\']).*?\1', '', expr)
            names += [n for n in re.findall(r'(?<![\w.])([A-Za-z_]\w*)', expr) if n not in NEUTRAL_NAMES]
    if not names:
        return False
    def constant(n: str) -> bool:
        if re.fullmatch(r'_?[A-Z][A-Z0-9_]*', n):
            return True
        if re.search(rf'^{re.escape(n)}\s*(:[^=\n]+)?=\s*[\(\["\']', text, re.M):
            return True
        m = re.search(rf'\bfor\s+(?:\w+\s*,\s*)*{re.escape(n)}(?:\s*,\s*\w+)*\s+in\s+(_?[A-Z][A-Z0-9_]*\b|[\(\[])', text)
        return bool(m)
    return all(constant(n) for n in names)

def scan_file(path: Path, rel: str, pitfalls: list[dict]) -> list[dict]:
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    lines = text.splitlines()
    if not lines or max(len(l) for l in lines) > 3000:  # minified or generated
        return []
    active = [p for p in pitfalls if (not p.get('_require_file') or p['_require_file'].search(text))
              and not (p.get('_suppress_file') and p['_suppress_file'].search(text))
              and (not p.get('_require_path') or p['_require_path'].search(rel))]
    hits = []
    for i, line in enumerate(lines):
        if COMMENT.match(line):
            continue
        for p in active:
            m = p['_rx'].search(line)
            if not m or (p.get('_suppress_line') and p['_suppress_line'].search(line)):
                continue
            w = int(p.get('window', 4))
            near = '\n'.join(lines[max(0, i - w):i + w + 1])
            if p.get('_suppress_near') and p['_suppress_near'].search(near):
                continue
            if p.get('_require_near') and not p['_require_near'].search(near):
                continue
            if p.get('_suppress_above') and p['_suppress_above'].search('\n'.join(lines[max(0, i - w):i])):
                continue
            if p.get('_require_above') and not p['_require_above'].search('\n'.join(lines[max(0, i - w):i])):
                continue
            if p.get('_scope_regex') and p.get('_scope_suppress'):
                scope = next((lines[j] for j in range(i, -1, -1) if p['_scope_regex'].search(lines[j])), None)
                if scope is not None and p['_scope_suppress'].search(scope):
                    continue
            if p.get('constant_interpolation') and constant_fstring(line, text):
                continue
            hits.append({'pitfall_id': p['id'], 'cwe': [p['cwe']], 'file': rel, 'line': i + 1, 'column': m.start() + 1,
                         'snippet': line.strip()[:200], 'message': p['title'],
                         'sources': [{'tool': 'scan_pitfalls', 'rule_id': p['id']}]})
    return hits

def scan(root: Path, catalogue: dict, wanted: set[str] | None = None, include_tests: bool = False) -> dict:
    packs = catalogue['packs']
    wanted = wanted or set(packs)
    active = [compile_pitfall(p) for p in catalogue['pitfalls'] if p.get('regex') and p['pack'] in wanted]
    cands, files_by_pack = [], {}
    census = lang_census.census(root)
    for path in walk(root, include_tests, census):
        rel = path.relative_to(root).as_posix()
        names = [n for n in pack_of(path, rel, packs) if n in wanted]
        if any(packs[n].get('require_content') or packs[n].get('exclude_content') for n in names):
            try:
                head = path.read_text(encoding='utf-8', errors='replace') if path.stat().st_size < 2_000_000 else ''
            except OSError:
                head = ''
            names = [n for n in names if (not packs[n].get('require_content') or re.search(packs[n]['require_content'], head, re.M))
                     and not (packs[n].get('exclude_content') and re.search(packs[n]['exclude_content'], head, re.M))]
        if not names:
            continue
        for n in names:
            files_by_pack[n] = files_by_pack.get(n, 0) + 1
        cands.extend(scan_file(path, rel, [p for p in active if p['pack'] in names]))
    cands.sort(key=lambda c: (c['file'], c['line'], c['pitfall_id']))
    covered_exts = {e for n in wanted for e in packs[n].get('extensions', [])}
    # Loud-fail: the main language (vendored and third-party trees excluded) has no pack scanning it
    supported = {lang_census.LANG_BY_EXT[e] for e in covered_exts if e in lang_census.LANG_BY_EXT}
    if 'C' in supported or 'C++' in supported or 'Objective-C' in supported:
        supported.add('C/C++ header')
    cov = lang_census.language_coverage(census, supported, tool='scan_pitfalls.py (packs: ' + ', '.join(sorted(wanted)) + ')',
                                        what='pattern scan')
    cov['excluded'] = census['excluded']
    warnings = [cov['warning']] if cov['status'] == 'unsupported_dominant' else []
    return {'tool': 'scan_pitfalls', 'status': 'ran',
            'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'repository': root.name, 'files_scanned': files_by_pack, 'catalogue_version': catalogue.get('version'),
            'note': 'Candidates are pattern-level leads, not findings. Trace a source-to-sink path or dismiss each one.',
            'warnings': warnings, 'language_coverage': cov, 'candidates': cands}

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--packs', help='comma-separated packs (default: all)')
    ap.add_argument('--include-tests', action='store_true', help='also scan test files and directories')
    ap.add_argument('--catalogue', type=Path, default=CATALOGUE)
    ap.add_argument('--out', type=Path)
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')  # tool output can hold characters the console can't encode
    root = args.repo.resolve()
    if not root.is_dir():
        print(f'Not a directory: {root}', file=sys.stderr)
        return 2
    cat = load_catalogue(args.catalogue)
    wanted = set(args.packs.split(',')) if args.packs else None
    unknown = (wanted or set()) - set(cat['packs'])
    if unknown:
        print(f"Unknown pack(s): {', '.join(sorted(unknown))}. Known: {', '.join(cat['packs'])}", file=sys.stderr)
        return 2
    out = scan(root, cat, wanted, args.include_tests)
    for w in out.get('warnings') or []:
        print('!' * 78, file=sys.stderr)
        print(f'WARNING: {w}', file=sys.stderr)
        print('!' * 78, file=sys.stderr)
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + '\n', encoding='utf-8')
        by: dict[str, int] = {}
        for c in out['candidates']:
            by[c['pitfall_id']] = by.get(c['pitfall_id'], 0) + 1
        print(f"{len(out['candidates'])} candidate(s) from {sum(out['files_scanned'].values())} file(s) -> {args.out}")
        for k, v in sorted(by.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f'  {v:4}  {k}')
    else:
        print(text)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
