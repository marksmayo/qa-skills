#!/usr/bin/env python3
"""Which source files the UI can reach: entry points, an import graph that resolves tsconfig/jsconfig
`paths` aliases (`@/*`), and a file classification (test, route handler, UI). Stdlib only; imported
by recon.py, scan_pitfalls.py and strict_overlay.py, and runnable on its own:

    python ui_graph.py <repo> [--out graph.json]

Entry points: Next.js App Router files (page, layout, template, error, global-error, not-found,
loading, default), Pages Router pages (not pages/api), middleware.ts / proxy.ts and
instrumentation-client.ts; for other web apps src/main.*, src/index.*, the scripts index.html loads,
Remix app/root.*, SvelteKit +page/+layout files, Nuxt pages and app.vue. Route handlers
(app/**/route.ts, pages/api/**) are API code: a throw there is an HTTP 500, not a UI crash.

Everything is heuristic: imports built from computed strings are not followed, so a module reached
only that way shows as unreachable. The output says how many files each rule excluded so the
agent can check them.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

# directories never descended into (pruned before descending)
PRUNE = {'node_modules', '.git', '.next', '.nuxt', 'dist', 'build', 'out', 'coverage', '.venv', 'venv', '__pycache__',
         '.turbo', '.cache', '.svelte-kit', '.output', 'vendor', 'Pods', 'DerivedData', '.gradle', '.dart_tool',
         '.idea', '.vscode', 'storybook-static', '.expo', 'target', 'Carthage'}
SRC_EXT = ('.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.mts', '.cts', '.vue', '.svelte')
TEST_PART = re.compile(r'(^|/)(tests?|__tests__|__mocks__|e2e|cypress|playwright|test-shims|androidTest|integration_test|'
                       r'\w*UITests?)(/|$)')
TEST_NAME = re.compile(r'(\.|_)(test|spec|stories|story)\.\w+$|Tests?\.(swift|kt|java)$|_test\.dart$|'
                       r'^(vitest|jest|playwright|cypress)\.(config|setup)\.\w+$|\.setup\.\w+$', re.I)
ROUTE_HANDLER = re.compile(r'(^|/)(src/)?app/(.*/)?route\.(ts|js|mts|mjs|tsx|jsx)$|(^|/)(src/)?pages/api/')
NEXT_ENTRY = re.compile(r'(^|/)(src/)?app/(.*/)?(page|layout|template|error|global-error|not-found|loading|default|'
                        r'forbidden|unauthorized)\.(tsx|jsx|ts|js)$')
NEXT_PAGES = re.compile(r'(^|/)(src/)?pages/(?!api/).+\.(tsx|jsx|ts|js)$')
MIDDLEWARE = re.compile(r'^(src/)?(middleware|proxy)\.(ts|js|mts|mjs)$')
OTHER_ENTRY = re.compile(r'^(src/)?(main|index)\.(tsx|jsx|ts|js|mjs)$|^(src/)?(app/)?root\.(tsx|jsx)$|'
                         r'(^|/)\+(page|layout|error)\.(svelte|ts|js)$|^(src/)?app\.vue$|^(src/)?pages/.+\.vue$|'
                         r'^(src/)?instrumentation-client\.(ts|js)$')
IMPORT = re.compile(r'''(?:\bimport\s+(?:type\s+)?(?:[\w*{}\s,$]+?\s+from\s+)?|\bexport\s+(?:type\s+)?(?:[\w*{}\s,$]+?)\s+from\s+|'''
                    r'''\bimport\s*\(\s*|\brequire\s*\(\s*)['"]([^'"\n]+)['"]''')
TYPE_ONLY = re.compile(r'^\s*(import|export)\s+type\b')


def load_jsonc(path: Path) -> dict:
    """Parse JSON with comments and trailing commas (tsconfig, jsconfig), string-aware: '"@/*"' and
    '"**/*.ts"' are strings, not comments."""
    text = path.read_text(encoding='utf-8-sig')
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == '\\' else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith('//', i):
            j = text.find('\n', i)
            i = n if j < 0 else j
        elif text.startswith('/*', i):
            j = text.find('*/', i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    cleaned = re.sub(r',(\s*[}\]])', r'\1', ''.join(out))
    data = json.loads(cleaned or '{}')
    return data if isinstance(data, dict) else {}


def walk(root: Path, extra_prune: set[str] = frozenset()):
    """Every file under root, pruning dependency, build and cache directories before descending."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name in PRUNE or p.name in extra_prune or (p.name.startswith('.') and p.name not in {'.github', '.maestro'}):
                    continue
                stack.append(p)
            else:
                yield p


def classify(rel: str) -> str:
    """'test', 'route_handler' or 'ui' (anything else that is source)."""
    if TEST_PART.search(rel) or TEST_NAME.search(rel.rsplit('/', 1)[-1]):
        return 'test'
    if ROUTE_HANDLER.search(rel):
        return 'route_handler'
    return 'ui'


def _config_chain(root: Path) -> list[tuple[Path, dict]]:
    """tsconfig.json (or jsconfig.json) and the local configs it extends, nearest last."""
    for name in ('tsconfig.json', 'jsconfig.json'):
        p = root / name
        if p.exists():
            break
    else:
        return []
    chain, seen = [], set()
    while p and p.exists() and p not in seen:
        seen.add(p)
        try:
            cfg = load_jsonc(p)
        except (OSError, ValueError):
            break
        chain.insert(0, (p, cfg))
        ext = cfg.get('extends')
        ext = ext[0] if isinstance(ext, list) and ext else ext
        if isinstance(ext, str) and ext.startswith('.'):
            q = (p.parent / ext)
            p = q if q.suffix == '.json' else q.with_name(q.name + '.json')
        else:
            break
    return chain


def aliases(root: Path) -> list[tuple[str, list[Path]]]:
    """compilerOptions.paths as (pattern, [target patterns as absolute paths]); later configs win."""
    paths: dict = {}
    base: Path | None = None
    for p, cfg in _config_chain(root):
        co = cfg.get('compilerOptions') or {}
        if co.get('baseUrl'):
            base = (p.parent / co['baseUrl'])
        if isinstance(co.get('paths'), dict):
            paths = {k: [(base or p.parent) / t for t in v] for k, v in co['paths'].items() if isinstance(v, list)}
    out = [(k, v) for k, v in paths.items()]
    if base is not None:
        out.append(('*', [base / '*']))
    return out


def _with_ext(p: Path) -> Path | None:
    if p.is_file():
        return p
    for e in SRC_EXT + ('.d.ts',):
        q = p.with_name(p.name + e)
        if q.is_file():
            return q
    if p.is_dir():
        for e in SRC_EXT:
            q = p / ('index' + e)
            if q.is_file():
                return q
    # TypeScript ESM: "./x.js" written for x.ts
    if p.suffix in ('.js', '.jsx', '.mjs'):
        for e in ('.ts', '.tsx', '.mts'):
            q = p.with_suffix(e)
            if q.is_file():
                return q
    return None


def resolve(spec: str, frm: Path, root: Path, alias: list[tuple[str, list[Path]]]) -> Path | None:
    if spec.startswith('.'):
        return _with_ext((frm.parent / spec).resolve())
    if spec.startswith('/'):
        return _with_ext((root / spec.lstrip('/')).resolve())
    for pat, targets in alias:
        if pat.endswith('*'):
            if not spec.startswith(pat[:-1]):
                continue
            rest = spec[len(pat) - 1:]
            for t in targets:
                r = _with_ext(Path(str(t).replace('*', rest)).resolve())
                if r:
                    return r
        elif spec == pat:
            for t in targets:
                r = _with_ext(t.resolve())
                if r:
                    return r
    return None


def html_entries(root: Path, files: list[Path]) -> list[Path]:
    out = []
    for f in files:
        if f.name == 'index.html' and classify(f.relative_to(root).as_posix()) == 'ui':
            text = f.read_text(encoding='utf-8', errors='replace')
            for src in re.findall(r'<script\b[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']', text, re.I):
                if re.match(r'^([a-z]+:)?//', src, re.I):
                    continue
                t = (root / src.lstrip('/')) if src.startswith('/') else (f.parent / src)
                t = _with_ext(t.resolve())
                if t and t.suffix in SRC_EXT and not t.with_name(t.name + '.map').exists() and t.stat().st_size < 400_000:
                    out.append(t)
    return out


def entries(root: Path, files: list[Path]) -> list[Path]:
    out = []
    for f in files:
        rel = f.relative_to(root).as_posix()
        if f.suffix not in SRC_EXT or classify(rel) != 'ui':
            continue
        if NEXT_ENTRY.search(rel) or NEXT_PAGES.search(rel) or MIDDLEWARE.match(rel) or OTHER_ENTRY.search(rel):
            out.append(f)
    out += [h for h in html_entries(root, files) if h not in out]
    return out


def graph(root: Path, files: list[Path] | None = None) -> dict:
    """The import graph from the UI entry points. Returns entries, reachable files (relative paths),
    and per-file imports. 'reachable' is None when no entry point was recognised."""
    root = root.resolve()
    files = files if files is not None else list(walk(root))
    alias = aliases(root)
    ents = entries(root, files)
    if not ents:
        return {'entries': [], 'reachable': None, 'aliases': [a for a, _ in alias], 'unresolved': 0}
    seen: set[Path] = set()
    todo = [e.resolve() for e in ents]
    edges: dict[str, list[str]] = {}
    unresolved = 0
    while todo:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        try:
            text = f.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        rel = f.relative_to(root).as_posix() if f.is_relative_to(root) else str(f)
        outs = []
        for m in IMPORT.finditer(text):
                if TYPE_ONLY.match(m.group(0)):
                    continue  # type-only imports erase at build time
                spec = m.group(1)
                t = resolve(spec, f, root, alias)
                if t is None:
                    if spec.startswith(('.', '/')) or any(spec.startswith(a.rstrip('*')) for a, _ in alias if a != '*'):
                        unresolved += 1
                    continue
                if not t.is_relative_to(root) or 'node_modules' in t.parts:
                    continue
                outs.append(t.relative_to(root).as_posix())
                if t not in seen:
                    todo.append(t)
        edges[rel] = sorted(set(outs))
    reach = sorted(p.relative_to(root).as_posix() for p in seen if p.is_relative_to(root))
    return {'entries': sorted(e.relative_to(root).as_posix() for e in ents), 'reachable': reach,
            'aliases': [a for a, _ in alias], 'unresolved': unresolved, 'edges': edges}


def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--out', type=Path)
    args = ap.parse_args()
    g = graph(args.repo)
    text = json.dumps(g, indent=2)
    if args.out:
        args.out.write_text(text + '\n', encoding='utf-8')
        print(f"{len(g['entries'])} entry point(s), {len(g['reachable'] or [])} reachable file(s) -> {args.out}")
    else:
        print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
