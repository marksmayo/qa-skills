#!/usr/bin/env python3
"""Scan UI source for crash pitfalls and merge candidates from other tools into one list.

    python scan_pitfalls.py <repo> [--packs web,android] [--sarif semgrep.sarif ...]
                                   [--extra overlay.json contract.json ...] [--out candidates.json]

Every hit is a *candidate* with pattern confidence: a line that matches a pitfall in
data/pitfalls.json. Candidates are not findings until the agent traces them to a realistic trigger.
--sarif adds results from SARIF-emitting analysers (Semgrep, detekt, SwiftLint, Android Lint),
mapped to pitfalls through each pitfall's 'tools' list. --extra adds candidates written by
strict_overlay.py and contract_diff.py. Hits on the same file, line and pitfall are merged, keeping
every source. Test files, dependencies, build output and minified files are skipped.
Promise chains (matcher "promise_chain") are parsed whole across lines; --self-test runs a built-in
check of that parser and exits.
"""
from __future__ import annotations
import argparse, json, re, sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True  # importing a sibling must not write __pycache__ into the skill folder
sys.path.insert(0, str(HERE))
import sarif  # noqa: E402

CATALOGUE = HERE.parent / 'data' / 'pitfalls.json'
SKIP_DIRS = {'node_modules', '.git', 'dist', 'build', 'out', '.next', '.nuxt', '.svelte-kit', 'coverage', 'vendor',
             'Pods', 'DerivedData', '.gradle', '.dart_tool', '.idea', '.vscode', '__pycache__', 'storybook-static',
             '.expo', 'e2e', 'cypress', 'playwright', '__tests__', '__mocks__',
             'test', 'tests', 'androidTest', 'integration_test', 'UITests', 'Tests'}
TEST_FILE = re.compile(r'(\.|_)(test|spec|stories|story)\.\w+$|Tests?\.(swift|kt|java)$|_test\.dart$|\.d\.ts$|\.min\.js$', re.I)
COMMENT = re.compile(r'^\s*(//|/\*|\*|#|<!--)')
SSR_DEPS = {'next': 'next', 'nuxt': 'nuxt', '@remix-run/react': 'remix', '@sveltejs/kit': 'sveltekit',
            'astro': 'astro', 'gatsby': 'gatsby'}

def is_bundle(p: Path) -> bool:
    """Built output (a sibling .map file, or a very large script) is not source to analyse."""
    try:
        return p.with_name(p.name + '.map').exists() or (p.suffix in ('.js', '.mjs', '.cjs') and p.stat().st_size > 400_000)
    except OSError:
        return False

def load_catalogue(path: Path = CATALOGUE) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))

def ssr_frameworks(root: Path) -> set[str]:
    found: set[str] = set()
    for pkg in [root / 'package.json', *root.glob('*/package.json'), *root.glob('apps/*/package.json')]:
        try:
            data = json.loads(pkg.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        deps = {**(data.get('dependencies') or {}), **(data.get('devDependencies') or {})}
        found |= {name for dep, name in SSR_DEPS.items() if dep in deps}
    return found

def source_files(root: Path, extensions: set[str]):
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in SKIP_DIRS and not p.name.startswith('.'):
                    stack.append(p)
            elif p.suffix in extensions and not TEST_FILE.search(p.name) and not is_bundle(p):
                yield p

REGEX_BEFORE = set('(,=:[!&|?{};+-*%<>~^') | {''}
OPEN, CLOSE = '([{', ')]}'

def mask_code(text: str) -> str:
    """Blank out comments, string, template and regex literal contents (same length, newlines kept), so
    brackets can be balanced on what is left."""
    out = list(text)
    i, n = 0, len(text)
    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != '\n':
                out[k] = ' '
    prev = ''
    while i < n:
        c = text[i]
        if c == '/' and text.startswith('//', i):
            j = text.find('\n', i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif c == '/' and text.startswith('/*', i):
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif c in '"\'`':
            j = i + 1
            while j < n and text[j] != c:
                if text[j] == '\\':
                    j += 1
                elif c != '`' and text[j] == '\n':
                    break
                j += 1
            blank(i + 1, j)
            i, prev = j + 1, c
        elif c == '/' and (prev in REGEX_BEFORE or re.search(r'\b(return|typeof|case|in|of)\s*$', text[max(0, i - 10):i])):
            j, cls = i + 1, False
            while j < n and text[j] != '\n' and (cls or text[j] != '/'):
                if text[j] == '\\':
                    j += 1
                elif text[j] == '[':
                    cls = True
                elif text[j] == ']':
                    cls = False
                j += 1
            blank(i + 1, j)
            i, prev = j + 1, '/'
        else:
            if not c.isspace():
                prev = c
            i += 1
    return ''.join(out)

def _match(code: str, i: int, step: int) -> int:
    """Index of the bracket matching code[i], scanning forwards (step 1) or backwards (step -1); -1 if none."""
    depth, j = 0, i
    while 0 <= j < len(code):
        ch = code[j]
        if ch in (OPEN if step == 1 else CLOSE):
            depth += 1
        elif ch in (CLOSE if step == 1 else OPEN):
            depth -= 1
            if depth == 0:
                return j
        j += step
    return -1

IDENT = re.compile(r'[\w$]')

def chain_start(code: str, dot: int) -> int:
    """Start of the receiver expression ending just before code[dot] ('.then'): identifiers, member
    accesses, calls and index expressions, across lines."""
    j = dot - 1
    while True:
        while j >= 0 and code[j].isspace():
            j -= 1
        if j < 0:
            return 0
        if code[j] in ')]':
            k = _match(code, j, -1)
            if k < 0:
                return j + 1
            j = k - 1
            continue
        if IDENT.match(code[j]):
            while j >= 0 and IDENT.match(code[j]):
                j -= 1
            k = j
            while k >= 0 and code[k].isspace():
                k -= 1
            if k >= 0 and code[k] == '.':
                j = k - 1 - (1 if k >= 1 and code[k - 1] == '?' else 0)
                continue
            return j + 1
        return j + 1

def chain_links(code: str, dot: int) -> tuple[list[tuple[str, int, int]], int]:
    """Calls in the chain from code[dot] onwards: (method, args, position), and where the chain ends."""
    links, j = [], dot
    while True:
        m = re.compile(r'\??\.\s*([\w$]+)\s*').match(code, j)
        if not m:
            return links, j
        name, j = m.group(1), m.end()
        if j < len(code) and code[j] == '(':
            end = _match(code, j, 1)
            if end < 0:
                return links, len(code)
            inner, depth, commas = code[j + 1:end], 0, 0
            for ch in inner:
                if ch in OPEN:
                    depth += 1
                elif ch in CLOSE:
                    depth -= 1
                elif ch == ',' and depth == 0:
                    commas += 1
            args = (commas + 1 if inner.strip() else 0) - (1 if inner.rstrip().endswith(',') else 0)
            links.append((name, args, m.start()))
            j = end + 1
        while j < len(code) and code[j] == '[':
            end = _match(code, j, 1)
            j = len(code) if end < 0 else end + 1
        k = j
        while k < len(code) and code[k].isspace():
            k += 1
        if code.startswith('.', k) or code.startswith('?.', k):
            j = k
        else:
            return links, j

def promise_chain_hits(text: str, rel: str, p: dict) -> list[dict]:
    """Promise chains with no rejection handler anywhere in the chain, parsed across lines."""
    code = mask_code(text)
    lines = text.splitlines()
    hits, done = [], set()
    for m in re.finditer(r'\??\.\s*then\s*\(', code):
        if m.start() in done:
            continue
        start = chain_start(code, m.start())
        j = start
        while j < len(code) and IDENT.match(code[j]):
            j += 1
        while j < len(code) and code[j] in '([':
            end = _match(code, j, 1)
            j = len(code) if end < 0 else end + 1
        every, _ = chain_links(code, j)
        if not any(pos == m.start() for _, _, pos in every):  # receiver not understood: judge from this link on
            every, _ = chain_links(code, m.start())
        for name, _, pos in every:
            if name == 'then':
                done.add(pos)
        handled = any(name == 'catch' or (name == 'then' and args >= 2) for name, args, _ in every)
        before = code[:start].rstrip()
        consumed = (re.search(r'(\breturn|\bawait|\byield|=>)$', before) is not None
                    or (before[-1:] in ('(', ',', '[') and not re.search(r'\b(if|while|for|switch)\s*\($', before)))
        if handled or consumed:
            continue
        ln = text.count('\n', 0, m.start()) + 1
        col = m.start() - (text.rfind('\n', 0, m.start()) + 1) + 1
        hits.append({'pitfall_id': p['id'], 'file': rel, 'line': ln, 'column': col,
                     'snippet': lines[ln - 1].strip()[:200], 'message': p['title'],
                     'sources': [{'tool': 'scan_pitfalls', 'rule_id': p['id']}]})
    return hits

def scan_file(path: Path, rel: str, pitfalls: list[dict]) -> list[dict]:
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    lines = text.splitlines()
    if lines and max(len(l) for l in lines) > 2000:  # minified or generated
        return []
    hits = []
    for p in pitfalls:
        if p.get('matcher') == 'promise_chain' and (not p.get('extensions') or path.suffix in p['extensions']):
            hits.extend(promise_chain_hits(text, rel, p))
    pitfalls = [p for p in pitfalls if not p.get('matcher')]
    for i, line in enumerate(lines):
        if COMMENT.match(line):
            continue
        for p in pitfalls:
            if p.get('extensions') and path.suffix not in p['extensions']:
                continue
            m = p['_rx'].search(line)
            if not m:
                continue
            if p.get('_suppress_line') and p['_suppress_line'].search(line):
                continue
            w = int(p.get('window', 6))
            before = '\n'.join(lines[max(0, i - w):i])
            after = '\n'.join(lines[i:i + w + 1])
            if p.get('_suppress_before') and p['_suppress_before'].search(before):
                continue
            if p.get('_suppress_after') and p['_suppress_after'].search(after):
                continue
            if p.get('_require_before') and not p['_require_before'].search(before):
                continue
            hits.append({'pitfall_id': p['id'], 'file': rel, 'line': i + 1, 'column': m.start() + 1,
                         'snippet': line.strip()[:200], 'message': p['title'],
                         'sources': [{'tool': 'scan_pitfalls', 'rule_id': p['id']}]})
    return hits

def tool_index(catalogue: dict) -> dict[str, str]:
    """'tool:rule' -> pitfall id, for mapping SARIF and overlay results back to the catalogue."""
    idx: dict[str, str] = {}
    for p in catalogue['pitfalls']:
        for t in p.get('tools') or []:
            idx.setdefault(t.lower(), p['id'])
    return idx

def from_sarif(paths: list[Path], root: Path, catalogue: dict) -> tuple[list[dict], list[dict]]:
    idx = tool_index(catalogue)
    out, tools = [], []
    for path in paths:
        results = sarif.read(path, root)
        names = sorted({r['tool'] for r in results}) or [path.stem]
        tools.append({'tool': ', '.join(names), 'input': str(path), 'results': len(results)})
        for r in results:
            tool = (r['tool'] or '').lower().split()[0] if r['tool'] else 'sarif'
            rule = r.get('rule_id') or ''
            pid = idx.get(f'{tool}:{rule}'.lower()) or idx.get(f'{tool}:{rule.split(".")[-1]}'.lower())
            out.append({'pitfall_id': pid, 'file': r['file'], 'line': r['line'], 'column': r.get('column'),
                        'snippet': r.get('snippet') or '', 'message': r['message'],
                        'sources': [{'tool': r['tool'], 'version': r.get('version'), 'rule_id': rule}]})
    return out, tools

def merge(cands: list[dict]) -> list[dict]:
    merged: dict[tuple, dict] = {}
    for c in cands:
        key = (c['file'], c['line'], c.get('pitfall_id') or c['message'])
        if key in merged:
            have = merged[key]
            for s in c['sources']:
                if s not in have['sources']:
                    have['sources'].append(s)
            for k in ('field', 'schema_ref', 'nullable_reason', 'guard_nearby'):
                if c.get(k) is not None and have.get(k) is None:
                    have[k] = c[k]
        else:
            merged[key] = dict(c)
    out = sorted(merged.values(), key=lambda c: (c['file'], c['line'] or 0, c.get('pitfall_id') or ''))
    for n, c in enumerate(out, 1):
        c['id'] = f'CAND-{n:03d}'
    return out

SELF_TEST_JS = r"""// a .then( in a comment, and "x.then(" in a string, are not code
const s = "fetch(a).then(b)";
const re = /\)\.then\(/g;
function returned() {
  return fetch("/a")
    .then((r) => r.json());
}
navigator.serviceWorker.getRegistration().then((reg) => {
  if (reg) return;
  navigator.serviceWorker
    .register("/sw.js")
    .then((registration) => {
      console.info("ok", registration);
    })
    .catch((error) => console.warn(error));
});
loadIndex().then(() => render(input.value));
loadIndex()
  .then(() => render(input.value))
  .finally(() => done());
api.get("/x").then(ok, (err) => showError(err));
self.addEventListener("install", (event) => {
  event.waitUntil(caches.open("v1").then((c) => c.addAll(["/"])));
});
const load = () => fetch("/b").then((r) => r.json());
async function awaited() { return await fetch("/c").then((r) => r.text()); }
fetch(`/d/${id}`, { method: "POST" })
  .then((r) => r.json())
  .then((j) => { show(j.items.map((x) => (x.then ? 1 : 2))); })
  .catch(() => {});
const ratio = a / b; const half = (x) / 2; client.get(u).then(cb);
"""
# unhandled chains; the chains split across lines that end in .catch (10-15, 27-30) are handled
SELF_TEST_EXPECT = [8, 17, 19, 31]

def self_test() -> int:
    p = next(q for q in load_catalogue()['pitfalls'] if q.get('matcher') == 'promise_chain')
    got = [h['line'] for h in promise_chain_hits(SELF_TEST_JS, 'app.js', p)]
    ok = got == SELF_TEST_EXPECT
    print(f"self-test {'passed' if ok else 'FAILED'}: promise chains flagged at lines {got}, expected {SELF_TEST_EXPECT}")
    return 0 if ok else 1

def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):  # JSON on stdout may hold any character; a cp1252 console can't print it
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path, nargs='?')
    ap.add_argument('--self-test', action='store_true', help='run the built-in promise-chain test and exit')
    ap.add_argument('--packs', help='comma-separated packs (default: every pack with matching files)')
    ap.add_argument('--sarif', nargs='*', type=Path, default=[], help='SARIF files from other analysers')
    ap.add_argument('--extra', nargs='*', type=Path, default=[], help='candidate files from strict_overlay.py / contract_diff.py')
    ap.add_argument('--catalogue', type=Path, default=CATALOGUE)
    ap.add_argument('--out', type=Path)
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if args.repo is None:
        ap.error('repo is required')
    root = args.repo.resolve()
    if not root.is_dir():
        print(f'Not a directory: {root}', file=sys.stderr)
        return 2
    cat = load_catalogue(args.catalogue)
    packs = cat['packs']
    wanted = set(args.packs.split(',')) if args.packs else set(packs)
    ssr = ssr_frameworks(root)
    active = []
    for p in cat['pitfalls']:
        if not p.get('regex') or p['pack'] not in wanted:
            continue
        if p.get('requires_framework') and not ssr & set(p['requires_framework']):
            continue
        q = dict(p)
        q['_rx'] = re.compile(p['regex'])
        for k in ('suppress_before', 'suppress_after', 'require_before', 'suppress_line'):
            if p.get(k):
                q['_' + k] = re.compile(p[k])
        active.append(q)
    exts = {e for name, pk in packs.items() if name in wanted for e in pk['extensions']}
    cands: list[dict] = []
    files_by_pack: dict[str, int] = {}
    for path in source_files(root, exts):
        rel = path.relative_to(root).as_posix()
        for name, pk in packs.items():
            if path.suffix in pk['extensions']:
                files_by_pack[name] = files_by_pack.get(name, 0) + 1
        cands.extend(scan_file(path, rel, [p for p in active if path.suffix in packs[p['pack']]['extensions']]))
    tools = [{'tool': 'scan_pitfalls', 'status': 'ran', 'results': len(cands),
              'packs': sorted(n for n in files_by_pack if n in wanted)}]
    if args.sarif:
        extra, info = from_sarif(args.sarif, root, cat)
        cands.extend(extra)
        tools.extend({**t, 'status': 'ran'} for t in info)
    for path in args.extra:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            print(f'Unable to read {path}: {exc}', file=sys.stderr)
            return 2
        cands.extend(data.get('candidates') or [])
        tools.append({'tool': data.get('tool', path.stem), 'status': data.get('status', 'ran'),
                      'results': len(data.get('candidates') or []), 'input': str(path)})
    out = {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'repository': root.name,
        'files_scanned': files_by_pack,
        'ssr_frameworks': sorted(ssr),
        'tools': tools,
        'note': 'Candidates are pattern-level leads, not findings. Trace each one to a realistic trigger or dismiss it.',
        'candidates': merge(cands),
    }
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + '\n', encoding='utf-8')
        by = {}
        for c in out['candidates']:
            by[c.get('pitfall_id') or 'unmapped'] = by.get(c.get('pitfall_id') or 'unmapped', 0) + 1
        print(f"{len(out['candidates'])} candidate(s) from {sum(files_by_pack.values())} file(s) -> {args.out}")
        for k, v in sorted(by.items(), key=lambda kv: -kv[1]):
            print(f'  {v:4}  {k}')
    else:
        print(text)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
