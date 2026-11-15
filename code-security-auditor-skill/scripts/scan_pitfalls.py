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
  require_inside  keep only when the match is in the body of an inline element of this tag (script: a
                  template value pasted into an inline <script>, not <script src="{{ ... }}">)
  suppress_tag    with require_inside: skip when the opening tag matches this (a JSON-LD block, which no
                  browser runs, and which auto-escaping already keeps closed)
  suppress_in_test_code
                  Rust: skip a match inside a #[cfg(test)] item or a #[test] function in a source file (unit
                  tests are not built into the shipped binary; --include-tests keeps them)
  bounded_cast    Rust: skip a narrowing cast of a value clamped or bounded in the cast expression or on the
                  lines just above (.clamp, .min, saturating_*, %, an assert! on it), and a float-to-integer
                  cast (.floor(), .round(), ...: those saturate and never wrap)
  group_ffi_safety
                  Rust: unsafe blocks that carry a SAFETY: comment and call foreign code (libloading symbols,
                  raw pointers into an FFI call, CStr/CString, libc/windows-sys) become one candidate per file
                  with grouped_lines, confidence low and group_reason; any other unsafe block stays on its own
  gha_read_only_token
                  GitHub Actions: skip a match in a job whose token has no write permission (the job's
                  permissions:, else the workflow's; read-all, {} or no `: write` entry). Without a
                  permissions block the token may have the repository default (write), so the match stays.
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
test_paths.py): a workspace package named e2e is product code and is scanned. Data directories
(200+ data files and no code, see data_dirs.py) and --exclude-dir paths are pruned without opening
a file and listed under skipped_data_dirs; --scan-data-dirs reads them.
"""
from __future__ import annotations
import argparse, json, re, sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import cargo_project  # noqa: E402
import data_dirs as data_dir_rule  # noqa: E402
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

def walk(root: Path, include_tests: bool = False, census: dict | None = None, data=None):
    """Files to scan. Directories lang_census excludes (vendored, bundled, embedded third-party packages),
    test_paths.vendored_dir names, and data directories and --exclude-dir paths (`data`, a data_dirs.DataDirs)
    are pruned before they are walked."""
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
                if data is not None and data.why(p):
                    continue  # a data tree (thousands of JSON records) or an excluded directory: recorded, never opened
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

def inside_tag(lines: list[str], i: int, col: int, tag: str) -> str | None:
    """The opening tag when column `col` of line i is in the body of an inline <tag> element (a <script> with no
    src=), not in the opening tag itself and not after its closing tag; otherwise None."""
    before = ('\n'.join(lines[max(0, i - 400):i]) + '\n' + lines[i][:col]).lower()
    close = before.rfind(f'</{tag}')
    opened = [m for m in re.finditer(rf'<{tag}\b[^>]*>', before) if m.start() > close]
    return opened[-1].group(0) if opened and 'src=' not in opened[-1].group(0) else None

# ---------------------------------------------------------------- Rust and GitHub Actions context
RUST_TEST_ATTR = re.compile(r'#\[\s*(?:[\w:]+::)?(?:test|rstest|test_case|quickcheck|proptest)\b[^\]]*\]')
RUST_CFG_ATTR = re.compile(r'#\[\s*cfg\s*\(')

def _cfg_is_test(expr: str) -> bool:
    """cfg(test) or cfg(all(test, ...)): the item is built only for tests (any(test, x) and not(test) are not)."""
    e = re.sub(r'\s+', '', expr)
    return e == 'test' or (e.startswith('all(') and bool(re.search(r'(?<![\w(])test\b|\(test\b', e.replace('not(test)', ''))))

def rust_test_lines(text: str) -> set[int]:
    """1-based lines inside #[cfg(test)] items and #[test] functions of a Rust source file."""
    masked = cargo_project.mask(text)
    out: set[int] = set()
    spans = [('cfg', m) for m in RUST_CFG_ATTR.finditer(masked)] + [('test', m) for m in RUST_TEST_ATTR.finditer(masked)]
    for kind, m in spans:
        if kind == 'cfg':
            open_idx = m.end() - 1
            close = cargo_project._balanced(masked, open_idx)
            if not _cfg_is_test(masked[open_idx + 1:close - 1]):
                continue
            end_attr = masked.find(']', close)
        else:
            end_attr = m.end() - 1
        if end_attr < 0:
            continue
        nxt = re.search(r'[{;]', masked[end_attr + 1:])
        if not nxt or nxt.group(0) == ';':
            continue  # `#[cfg(test)] mod tests;` (a file of its own) or `#[cfg(test)] use x;`
        brace = end_attr + 1 + nxt.start()
        stop = cargo_project._balanced(masked, brace, '{', '}')
        first, last = masked.count('\n', 0, m.start()) + 1, masked.count('\n', 0, stop) + 1
        out.update(range(first, last + 1))
    return out

BOUNDING = re.compile(r'\.clamp\s*\(|\bclamp\s*\(|\.min\s*\(|\bmin\s*\(|\bsaturating_\w+|\.rem_euclid\s*\(|%')
FLOAT_TO_INT = re.compile(r'\.(?:floor|round|ceil|trunc)\s*\(\s*\)\s*$')

def bounded_cast(lines: list[str], i: int, col: int) -> bool:
    """True when the value cast at lines[i][col:] (`... as u8`) is bounded first: clamped or min'd in the cast
    expression, assigned from a clamp/min/saturating/% expression or asserted below a limit on the 3 lines above,
    or a float rounded to an integer (a float-to-integer `as` saturates, it never wraps)."""
    pre = lines[i][:col].rstrip()
    if pre.endswith(')'):
        depth, k = 0, len(pre) - 1
        while k > 0:
            depth += {')': 1, '(': -1}.get(pre[k], 0)
            if depth == 0:
                break
            k -= 1
        while k > 0 and (pre[k - 1].isalnum() or pre[k - 1] in '_.:'):
            k -= 1  # the call or method chain the parentheses belong to
        return bool(BOUNDING.search(pre[k:]) or FLOAT_TO_INT.search(pre))
    m = re.search(r'([A-Za-z_]\w*)\s*$', pre)
    if not m:
        return False
    name = re.escape(m.group(1))
    # a float (compared with a float literal on the line, or declared f32/f64 just above): the cast saturates
    if re.search(rf'\b{name}\s*[<>]=?\s*-?\d+\.\d+|-?\d+\.\d+\s*[<>]=?\s*{name}\b', lines[i]) or \
            any(re.search(rf'\b{name}\s*:\s*f(?:32|64)\b', a) for a in lines[max(0, i - 5):i + 1]):
        return True
    for a in lines[max(0, i - 3):i]:
        let = re.search(rf'\b{name}\s*(?::[^=]+)?=(?!=)(.*)', a)
        if let and BOUNDING.search(let.group(1)):
            return True
        if re.search(rf'\bassert!\s*\(\s*{name}\s*<', a):
            return True
    return False

FFI_BODY = re.compile(r'Library::|libloading|\.get\s*(?:::<[^>]*>)?\s*\(\s*b"|Symbol\s*<|\bffi::|\bCStr\b|\bCString\b|'
                      r'\.as_(?:mut_)?ptr\s*\(\s*\)|\*const\b|\*mut\b|\blibc::|\bwindows(?:_sys)?::|\bwinapi::|LoadLibrary|'
                      r'GetProcAddress|\bdlopen\b|\bdlsym\b')

def ffi_unsafe_block(text: str, masked: str, lines: list[str], i: int, col: int) -> bool:
    """The unsafe block at line i (0-based), column col, carries a SAFETY: comment on its line or the 3 above and
    calls foreign code (functions declared in an extern block of the file count)."""
    if not any(re.search(r'SAFETY\s*:', x, re.I) for x in lines[max(0, i - 3):i + 1]):
        return False
    start = sum(len(l) + 1 for l in lines[:i]) + col  # splitlines drops \r\n pairs the same way read_text keeps \n
    brace = masked.find('{', start)
    if brace < 0:
        return False
    body = text[brace:cargo_project._balanced(masked, brace, '{', '}')]
    if FFI_BODY.search(body):
        return True
    externs: set[str] = set()
    for blk in re.finditer(r'\bextern\s+"[^"]*"\s*\{', masked):
        end = cargo_project._balanced(masked, blk.end() - 1, '{', '}')
        externs |= set(re.findall(r'\bfn\s+(\w+)', masked[blk.end():end]))
    return any(re.search(rf'\b{re.escape(f)}\s*\(', body) for f in externs)

def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())

def _permissions_write(lines: list[str], k: int) -> bool:
    """Whether the permissions: key on line k grants any write (write-all, or a `scope: write` entry)."""
    val = re.sub(r'#.*$', '', lines[k].split(':', 1)[1]).strip().strip('\'"')
    if val:
        return val == 'write-all' or bool(re.search(r':\s*write\b', val))
    for l in lines[k + 1:]:
        if l.strip() and not l.lstrip().startswith('#') and _indent(l) <= _indent(lines[k]):
            break
        if re.search(r':\s*write\b', l):
            return True
    return False

def gha_token_writes(lines: list[str], i: int) -> bool | None:
    """Whether the job holding line i has a token with any write permission: True, False (read-all, {}, or no
    `: write` entry), or None when neither the job nor the workflow sets permissions (the repository default)."""
    jobs = next((k for k, l in enumerate(lines) if re.match(r'^jobs\s*:\s*$', l)), None)
    code = lambda k: bool(lines[k].strip()) and not lines[k].lstrip().startswith('#')  # noqa: E731
    if jobs is not None and i > jobs:
        job_indent = next((_indent(lines[k]) for k in range(jobs + 1, len(lines)) if code(k)), 2)
        head = next((k for k in range(i, jobs, -1) if code(k) and _indent(lines[k]) == job_indent), None)
        if head is not None:
            end = next((k for k in range(head + 1, len(lines)) if code(k) and _indent(lines[k]) <= job_indent), len(lines))
            body = [k for k in range(head + 1, end) if code(k)]
            key_indent = min((_indent(lines[k]) for k in body), default=0)
            for k in body:
                if _indent(lines[k]) == key_indent and re.match(r'^\s*permissions\s*:', lines[k]):
                    return _permissions_write(lines, k)
    top = next((k for k, l in enumerate(lines) if re.match(r'^permissions\s*:', l)), None)
    return _permissions_write(lines, top) if top is not None else None

def scan_file(path: Path, rel: str, pitfalls: list[dict], include_tests: bool = False) -> list[dict]:
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    lines = text.splitlines()
    if not lines or max(len(l) for l in lines) > 3000:  # minified or generated
        return []
    test_lines: set[int] = set()
    if path.suffix == '.rs' and not include_tests and any(p.get('suppress_in_test_code') for p in pitfalls):
        test_lines = rust_test_lines(text)
    masked = cargo_project.mask(text) if path.suffix == '.rs' and any(p.get('group_ffi_safety') for p in pitfalls) else None
    grouped: dict[str, list[dict]] = {}
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
            if p.get('require_inside'):
                opening = inside_tag(lines, i, m.start(), p['require_inside'])
                if not opening or (p.get('suppress_tag') and re.search(p['suppress_tag'], opening)):
                    continue
            if p.get('suppress_in_test_code') and (i + 1) in test_lines:
                continue
            if p.get('bounded_cast') and bounded_cast(lines, i, m.start()):
                continue
            if p.get('gha_read_only_token') and gha_token_writes(lines, i) is False:
                continue
            hit = {'pitfall_id': p['id'], 'cwe': [p['cwe']], 'file': rel, 'line': i + 1, 'column': m.start() + 1,
                   'snippet': line.strip()[:200], 'message': p['title'],
                   'sources': [{'tool': 'scan_pitfalls', 'rule_id': p['id']}]}
            if p.get('group_ffi_safety') and masked is not None and m.group(0).rstrip().endswith('{') \
                    and ffi_unsafe_block(text, masked, lines, i, m.start()):
                grouped.setdefault(p['id'], []).append(hit)
                continue
            hits.append(hit)
    for pid, group in grouped.items():  # one low-confidence candidate per file for documented FFI unsafe blocks
        first = dict(group[0])
        first.update(grouped_lines=[g['line'] for g in group], confidence='low',
                     message=first['message'] + f' ({len(group)} FFI block(s) with SAFETY: comments, grouped)',
                     group_reason=f"{len(group)} unsafe block(s) in this file call foreign code (a loaded library's "
                                  'symbols, raw pointers passed to an FFI call) and carry a SAFETY: comment; review them '
                                  "together against the library's contract (lines " + ', '.join(str(g['line']) for g in group) + ')')
        hits.append(first)
    hits.sort(key=lambda h: (h['line'], h['pitfall_id']))
    return hits

HEAD_BYTES = 65536  # require_content/exclude_content read the start of a file (an ARM template's $schema is at the top)

def head_of(path: Path, n: int = HEAD_BYTES) -> str:
    try:
        with path.open('rb') as fh:
            return fh.read(n).decode('utf-8', errors='replace')
    except OSError:
        return ''

def scan(root: Path, catalogue: dict, wanted: set[str] | None = None, include_tests: bool = False,
         exclude_dirs: list[str] | None = None, data_dirs: bool = True) -> dict:
    """data_dirs=False turns off the data-directory rule (--scan-data-dirs); exclude_dirs are --exclude-dir paths."""
    packs = catalogue['packs']
    wanted = wanted or set(packs)
    active = [compile_pitfall(p) for p in catalogue['pitfalls'] if p.get('regex') and p['pack'] in wanted]
    cands, files_by_pack = [], {}
    census = lang_census.census(root)
    data = data_dir_rule.DataDirs(root, exclude_dirs, auto=data_dirs)
    for path in walk(root, include_tests, census, data):
        rel = path.relative_to(root).as_posix()
        names = [n for n in pack_of(path, rel, packs) if n in wanted]
        if any(packs[n].get('require_content') or packs[n].get('exclude_content') for n in names):
            head = head_of(path)
            names = [n for n in names if (not packs[n].get('require_content') or re.search(packs[n]['require_content'], head, re.M))
                     and not (packs[n].get('exclude_content') and re.search(packs[n]['exclude_content'], head, re.M))]
        if not names:
            continue
        for n in names:
            files_by_pack[n] = files_by_pack.get(n, 0) + 1
        cands.extend(scan_file(path, rel, [p for p in active if p['pack'] in names], include_tests))
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
            'warnings': warnings, 'language_coverage': cov,
            # data directories and --exclude-dir paths left out of the walk: not read, so not scanned
            'skipped_data_dirs': data.report(), 'candidates': cands}

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--packs', help='comma-separated packs (default: all)')
    ap.add_argument('--include-tests', action='store_true', help='also scan test files and directories')
    ap.add_argument('--catalogue', type=Path, default=CATALOGUE)
    data_dir_rule.add_arguments(ap)
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
    out = scan(root, cat, wanted, args.include_tests, args.exclude_dir, not args.scan_data_dirs)
    for s in out['skipped_data_dirs']:
        print(f"  skipped {s['dir']}/: {s['reason']}", file=sys.stderr)
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
