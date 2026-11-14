#!/usr/bin/env python3
"""Which source files the UI can reach: entry points, an import graph that resolves path aliases, and a
file classification (test, tool config, route handler, UI). Stdlib only; imported by recon.py,
scan_pitfalls.py and strict_overlay.py, and runnable on its own:

    python ui_graph.py <repo> [--out graph.json]

Aliases: tsconfig/jsconfig `paths` (`@/*`) with their `extends` chain, the projects a solution-style
tsconfig lists in `references` (create-vite's tsconfig.app.json), and Vite's `resolve.alias` when
vite.config.* writes it literally, and the tsconfig/jsconfig of nested projects (beside a
next.config.* or vite.config.*, or in a package that depends on a UI framework); a pattern several
projects define resolves to the target nearest the importing file. A directory holding next.config.*
is a Next.js project root. A test-named directory (e2e/, tests/) holding a package manifest is product
code, not tests. node_role() tells Node server and command-line modules apart from UI code. A specifier only an alias can mean (`@/x`, `~/x`) that resolves
nowhere counts as unresolved, and the output warns when imports did not resolve or when under 30% of
the UI source files are reachable.

Entry points: in a Next.js project (a package.json that depends on next), App Router files (page,
layout, template, error, global-error, not-found, loading, default), Pages Router pages (not
pages/api) and middleware.ts / proxy.ts, with app/ and pages/ at the project root or under src/
only; a pages/ folder elsewhere in a plain React app is not a router. For other web apps src/main.*,
src/index.*, the scripts index.html loads, Remix app/root.*, SvelteKit +page/+layout files, Nuxt
pages and app.vue, and instrumentation-client.ts. Route handlers (app/**/route.ts, pages/api/**)
are API code: a throw there is an HTTP 500, not a UI crash. Tool configs outside src/
(vite.config.ts, eslint.config.js) never run in the UI.

Everything is heuristic: imports built from computed strings are not followed, so a module reached
only that way shows as unreachable. The output says how many files each rule excluded so the
agent can check them.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from langs import prune_dir  # noqa: E402  (data directories and --exclude)

# directories never descended into (pruned before descending)
PRUNE = {'node_modules', '.git', '.next', '.nuxt', 'dist', 'build', 'out', 'coverage', '.venv', 'venv', '__pycache__',
         '.turbo', '.cache', '.svelte-kit', '.output', 'vendor', 'Pods', 'DerivedData', '.gradle', '.dart_tool',
         '.idea', '.vscode', 'storybook-static', '.expo', 'target', 'Carthage'}
SRC_EXT = ('.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.mts', '.cts', '.vue', '.svelte')
TEST_PART = re.compile(r'(^|/)(tests?|__tests__|__mocks__|e2e|cypress|playwright|test-shims|androidTest|integration_test|'
                       r'\w*UITests?|\w*Tests(?:_\w+)?)(/|$)')
TEST_NAME = re.compile(r'(\.|_)(test|spec|stories|story)\.\w+$|Tests?\.(swift|kt|java)$|_test\.dart$|'
                       r'^(vitest|jest|playwright|cypress)\.(config|setup)\.\w+$|\.setup\.\w+$', re.I)
ROUTE_HANDLER = re.compile(r'(^|/)(src/)?app/(.*/)?route\.(ts|js|mts|mjs|tsx|jsx)$|(^|/)(src/)?pages/api/')
# Next.js file conventions, matched against the path inside a Next.js project (a directory whose package.json
# depends on "next"): app/ and pages/ only at the project root or under src/. A pages/ folder inside a plain
# React app (src/modules/x/pages/...) is not a router.
NEXT_ENTRY = re.compile(r'^(src/)?app/(.*/)?(page|layout|template|error|global-error|not-found|loading|default|'
                        r'forbidden|unauthorized)\.(tsx|jsx|ts|js)$')
NEXT_PAGES = re.compile(r'^(src/)?pages/(?!api/).+\.(tsx|jsx|ts|js)$')
MIDDLEWARE = re.compile(r'^(src/)?(middleware|proxy)\.(ts|js|mts|mjs)$')
# a specifier that can only be a path alias: no npm package is named "@/x" or "~/x"
ALIAS_LIKE = re.compile(r'^(@|~|#)/')
UI_DEPS = {'react', 'react-dom', 'vue', 'svelte', 'preact', 'solid-js', '@angular/core', 'react-native', 'next', 'nuxt'}
CONFIG_NAME = re.compile(r'^([\w.-]+\.config|\.eslintrc|\.prettierrc|\.babelrc)(\.\w+)?$')


def is_tool_config(rel: str) -> bool:
    """Build and tool configuration (vite.config.ts, eslint.config.js, tailwind.config.js) outside src/:
    the UI never runs it."""
    name = rel.rsplit('/', 1)[-1]
    return bool(CONFIG_NAME.match(name)) and not re.search(r'(^|/)src/', rel)
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
                if prune_dir(root, p):   # a data directory (hundreds of JSON records) or --exclude
                    continue
                stack.append(p)
            else:
                yield p


# A directory named like a test folder (tests/, e2e/, ...) is test code only when it is not a package of its
# own: packages/e2e/ with a package.json and src/ is a product package that happens to be called e2e.
MANIFESTS = ('package.json', 'pyproject.toml', 'setup.py', 'setup.cfg', 'Cargo.toml', 'go.mod', 'pom.xml',
             'build.gradle', 'build.gradle.kts', 'composer.json', 'Gemfile', 'pubspec.yaml')
TEST_DIR_NAME = re.compile(r'^(tests?|__tests__|__mocks__|e2e|cypress|playwright|test-shims|androidTest|integration_test|'
                           r'\w*UITests?|\w*Tests(?:_\w+)?)$')
PACKAGE_DIRS: set[str] = set()  # test-named directories (relative) that hold a manifest; filled by note_packages()


def note_packages(root: Path, files: list[Path]) -> set[str]:
    """Record the test-named directories that hold a package manifest, so classify() treats them as product code.
    Returns them (relative paths)."""
    found = set()
    for f in files:
        if f.name in MANIFESTS and TEST_DIR_NAME.match(f.parent.name) and f.parent != root:
            found.add(f.parent.relative_to(root).as_posix())
    PACKAGE_DIRS.clear()
    PACKAGE_DIRS.update(found)
    return found


def in_test_dir(rel: str, packages: set[str] | None = None) -> bool:
    """True when a directory on the path is named like a test folder and is not a package (see MANIFESTS)."""
    packages = PACKAGE_DIRS if packages is None else packages
    parts = rel.split('/')[:-1]
    return any(TEST_DIR_NAME.match(p) and '/'.join(parts[:i + 1]) not in packages for i, p in enumerate(parts))


# Node code that never runs in a browser: an HTTP server (node:http createServer, Express, Fastify, Koa) or a
# command-line script (a node shebang, or process.argv with no DOM use)
HTTP_SERVER_IMPORT = re.compile(r'''(?:\bfrom\s+|\brequire\(\s*|\bimport\(\s*)["'](?:node:)?(?:http|https|http2|express|fastify|koa|@hapi/hapi)["']''')
SERVER_CALL = re.compile(r'\bcreateServer\s*\(|\.listen\s*\(|\bexpress\s*\(\s*\)|\bfastify\s*\(|\bnew\s+Koa\s*\(')
DOM_USE = re.compile(r'\b(?:document|window|localStorage|sessionStorage|navigator)\s*\.')


def node_role(text: str, name: str = '') -> str | None:
    """'server' for a Node HTTP server module, 'cli' for a Node command-line script, else None."""
    if HTTP_SERVER_IMPORT.search(text) and SERVER_CALL.search(text):
        return 'server'
    if name.endswith(('.mjs', '.cjs', '.js', '.ts', '.mts')) and (
            text.startswith('#!') and 'node' in text.split('\n', 1)[0] or
            ('process.argv' in text and not DOM_USE.search(text))):
        return 'cli'
    return None


def classify(rel: str) -> str:
    """'test', 'route_handler' or 'ui' (anything else that is source)."""
    if in_test_dir(rel) or TEST_NAME.search(rel.rsplit('/', 1)[-1]):
        return 'test'
    if is_tool_config(rel):
        return 'config'
    if ROUTE_HANDLER.search(rel):
        return 'route_handler'
    return 'ui'


def _root_config(root: Path) -> Path | None:
    for name in ('tsconfig.json', 'jsconfig.json'):
        if (root / name).exists():
            return root / name
    return None


def references(config: Path) -> list[Path]:
    """The projects a solution-style tsconfig lists in "references" (the create-vite layout since Vite 5:
    tsconfig.json has "files": [] and references tsconfig.app.json and tsconfig.node.json)."""
    try:
        cfg = load_jsonc(config)
    except (OSError, ValueError):
        return []
    out = []
    for r in cfg.get('references') or []:
        if not isinstance(r, dict) or not isinstance(r.get('path'), str):
            continue
        q = config.parent / r['path']
        q = q / 'tsconfig.json' if q.is_dir() else q
        if q.is_file() and q.resolve() != config.resolve():
            out.append(q)
    return out


def is_solution(config: Path) -> bool:
    """A tsconfig that compiles nothing itself and only lists referenced projects."""
    try:
        cfg = load_jsonc(config)
    except (OSError, ValueError):
        return False
    return bool(cfg.get('references')) and cfg.get('files') == [] and not cfg.get('include')


def _project_score(q: Path) -> int:
    try:
        cfg = load_jsonc(q)
    except (OSError, ValueError):
        return -1
    inc = [str(x) for x in (cfg.get('include') or []) + (cfg.get('files') or [])]
    stem = q.stem.replace('tsconfig', '')
    s = 4 if any(re.match(r'^(\./)?src(/|$)', x) for x in inc) else 0
    s += 2 if re.search(r'app|web|ui|client|src', stem, re.I) else 0
    s -= 3 if re.search(r'node|vite|test|spec|e2e|cypress|playwright', stem, re.I) else 0
    return s


def ui_project(config: Path) -> Path | None:
    """For a solution tsconfig: the referenced project that compiles the app's source (its "include"
    covers src/, or its name says app/web/ui/client), else None."""
    ranked = sorted(references(config), key=_project_score, reverse=True)
    return ranked[0] if ranked and _project_score(ranked[0]) > 0 else None


def _config_chains(root: Path) -> list[list[tuple[Path, dict]]]:
    """tsconfig.json (or jsconfig.json) with its extends chain, then each project it references."""
    p = _root_config(root)
    if p is None:
        return []
    return [_chain_from(p)] + [_chain_from(q) for q in references(p)]


def _config_chain(root: Path) -> list[tuple[Path, dict]]:
    """tsconfig.json (or jsconfig.json) and the local configs it extends, nearest last."""
    p = _root_config(root)
    return _chain_from(p) if p else []


def _chain_from(p: Path) -> list[tuple[Path, dict]]:
    """One tsconfig/jsconfig and the local configs it extends, nearest last."""
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


VITE_CONFIGS = ('vite.config.ts', 'vite.config.mts', 'vite.config.js', 'vite.config.mjs', 'vite.config.cts',
                'vite.config.cjs')
STR_LIT = re.compile(r'''(["'`])((?:(?!\1)[^\\\n])*)\1''')
# a literal alias target: a string, path.resolve/join(...), or fileURLToPath(new URL(...)); a variable is skipped
LITERAL_PATH_EXPR = re.compile(r'''^(?:(?:path\.)?(?:resolve|join)\(|fileURLToPath\(|new\s+URL\(|["'`])''')


def _close(text: str, i: int) -> int:
    """Index just past the bracket that closes text[i], skipping string contents."""
    pairs = {'{': '}', '[': ']', '(': ')'}
    stack, q, j = [], None, i
    while j < len(text):
        c = text[j]
        if q:
            if c == '\\':
                j += 1
            elif c == q:
                q = None
        elif c in '"\'`':
            q = c
        elif c in pairs:
            stack.append(pairs[c])
        elif stack and c == stack[-1]:
            stack.pop()
            if not stack:
                return j + 1
        j += 1
    return len(text)


def _split_top(body: str) -> list[str]:
    """Split on commas at bracket depth 0, outside strings."""
    parts, depth, q, cur = [], 0, None, ''
    for c in body:
        if q:
            cur += c
            if c == q:
                q = None
            continue
        if c in '"\'`':
            q = c
        elif c in '{[(':
            depth += 1
        elif c in '}])':
            depth -= 1
        if c == ',' and depth == 0:
            parts.append(cur.strip())
            cur = ''
        else:
            cur += c
    if cur.strip():
        parts.append(cur.strip())
    return parts


def _alias_target(expr: str, base: Path, root: Path) -> Path | None:
    expr = expr.strip()
    if not LITERAL_PATH_EXPR.match(expr):
        return None
    lits = [m.group(2) for m in STR_LIT.finditer(expr) if '${' not in m.group(2)]
    if not lits:
        return None
    if expr[0] in '"\'`' and lits[0].startswith('/'):
        return (root / lits[0].lstrip('/')).resolve()  # Vite resolves "/src" against the project root
    return base.joinpath(*[x for x in lits if x]).resolve()


def vite_aliases(root: Path) -> list[tuple[str, list[Path]]]:
    """resolve.alias from vite.config.* when it is written literally: { '@': path.resolve(__dirname, 'src') },
    fileURLToPath(new URL('./src', import.meta.url)), or [{ find: '@', replacement: ... }]. Vite matches a
    string key exactly or as a prefix followed by '/'. Regular-expression keys are not followed."""
    out: list[tuple[str, list[Path]]] = []
    for name in VITE_CONFIGS:
        cfg = root / name
        if not cfg.is_file():
            continue
        try:
            text = cfg.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        for m in re.finditer(r'\balias\s*:\s*([\[{])', text):
            body = text[m.start(1) + 1:_close(text, m.start(1)) - 1]
            pairs: list[tuple[str, str]] = []
            for part in _split_top(body):
                if m.group(1) == '{':
                    km = re.match(r'''^(?:(["'])(.+?)\1|([\w$]+))\s*:\s*(.+)$''', part, re.S)
                    if km:
                        pairs.append((km.group(2) or km.group(3), km.group(4)))
                else:
                    inner = part.strip()
                    if not inner.startswith('{'):
                        continue
                    fields = {}
                    for f in _split_top(inner[1:_close(inner, 0) - 1]):
                        fm = re.match(r'^([\w$]+)\s*:\s*(.+)$', f, re.S)
                        if fm:
                            fields[fm.group(1)] = fm.group(2)
                    lit = STR_LIT.fullmatch(fields.get('find', '').strip())
                    if lit and fields.get('replacement'):
                        pairs.append((lit.group(2), fields['replacement']))
            for key, expr in pairs:
                target = _alias_target(expr, cfg.parent, root)
                if target is None:
                    continue
                k = key.rstrip('/')
                out.append((k + '/*', [target / '*']))
                out.append((k, [target]))
        break
    return out


def nested_config_dirs(root: Path, files: list[Path]) -> list[Path]:
    """Project directories below the root with their own tsconfig/jsconfig: beside a next.config.* or
    vite.config.*, or in a package whose package.json depends on a UI framework (apps/web/tsconfig.json in a
    monorepo, app/tsconfig.json next to app/next.config.ts)."""
    dirs: set[Path] = set()
    for f in files:
        d = f.parent
        if d == root:
            continue
        if NEXT_CONFIG.match(f.name) or f.name in VITE_CONFIGS or \
                (f.name == 'package.json' and UI_DEPS & set(_deps(f))):
            if _root_config(d) is not None:
                dirs.add(d)
    return sorted(dirs)


def aliases(root: Path, files: list[Path] | None = None) -> list[tuple[str, list[Path]]]:
    """compilerOptions.paths as (pattern, [target patterns as absolute paths]); later configs in a chain
    win. A solution tsconfig's referenced projects (tsconfig.app.json) are read too, and Vite's literal
    resolve.alias entries come after them; baseUrl fallbacks ('*') come last. With `files`, the configs of
    nested projects (nested_config_dirs) are read as well; when two projects map the same pattern, resolve()
    prefers the target nearest the importing file."""
    out = _aliases_of(root)
    if files:
        for d in nested_config_dirs(root, files):
            for k, v in _aliases_of(d):
                if k != '*':
                    out.insert(len([a for a in out if a[0] != '*']), (k, v))
    return out


def _aliases_of(root: Path) -> list[tuple[str, list[Path]]]:
    out: list[tuple[str, list[Path]]] = []
    bases: list[Path] = []
    for chain in _config_chains(root):
        paths: dict = {}
        base: Path | None = None
        for p, cfg in chain:
            co = cfg.get('compilerOptions') or {}
            if co.get('baseUrl'):
                base = (p.parent / co['baseUrl'])
            if isinstance(co.get('paths'), dict):
                paths = {k: [(base or p.parent) / t for t in v] for k, v in co['paths'].items() if isinstance(v, list)}
        have = {a for a, _ in out}
        # the most specific pattern first, as TypeScript picks the longest prefix ("@/components/*" before "@/*")
        for k, v in sorted(paths.items(), key=lambda kv: -len(kv[0])):
            if k not in have:
                out.append((k, v))
        if base is not None and base not in bases:
            bases.append(base)
    have = {a for a, _ in out}
    out += [(k, v) for k, v in vite_aliases(root) if k not in have]
    out += [('*', [b / '*']) for b in bases]
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
    found: list[Path] = []
    for pat, targets in alias:
        if pat.endswith('*'):
            if not spec.startswith(pat[:-1]):
                continue
            rest = spec[len(pat) - 1:]
            for t in targets:
                r = _with_ext(Path(str(t).replace('*', rest)).resolve())
                if r:
                    found.append(r)
                    break
        elif spec == pat:
            for t in targets:
                r = _with_ext(t.resolve())
                if r:
                    found.append(r)
                    break
        if found and not _shared_pattern(pat, alias):
            break
    if not found:
        return None
    if len(found) == 1:
        return found[0]
    # the same pattern in several projects ("@/*" in a monorepo's apps): the target nearest the importing file
    here = frm.resolve().parent.parts
    def near(p: Path) -> int:
        n = 0
        for a, b in zip(here, p.parts):
            if a != b:
                break
            n += 1
        return n
    return max(found, key=near)


def _shared_pattern(pat: str, alias: list[tuple[str, list[Path]]]) -> bool:
    return sum(1 for a, _ in alias if a == pat) > 1


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


def _deps(pkg: Path) -> dict:
    try:
        data = json.loads(pkg.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}
    return {**(data.get('dependencies') or {}), **(data.get('devDependencies') or {}),
            **(data.get('peerDependencies') or {})} if isinstance(data, dict) else {}


NEXT_CONFIG = re.compile(r'^next\.config\.(js|mjs|cjs|ts|mts|cts)$')


def projects(root: Path, files: list[Path]) -> dict[str, dict]:
    """Every package.json directory under root (relative, '' for the root) -> its dependencies.

    A directory holding next.config.* is a Next.js project root too, even without a package.json of its own:
    `next build app` keeps next.config.ts, tsconfig.json and src/app/ in app/ while next is a dependency of the
    package above. Such a directory inherits the dependencies of the nearest package.json above it, plus next."""
    out = {}
    for f in files:
        if f.name == 'package.json':
            d = f.parent.relative_to(root).as_posix()
            out['' if d == '.' else d] = _deps(f)
    configs = set()
    for f in files:
        if NEXT_CONFIG.match(f.name):
            d = f.parent.relative_to(root).as_posix()
            configs.add('' if d == '.' else d)
    packages = dict(out)
    for d in sorted(configs):
        if 'next' in out.get(d, {}):
            continue
        parent, _ = project_of(d + '/x' if d else 'x', {k: v for k, v in packages.items() if k != d})
        out[d] = {**packages.get(parent, {}), **out.get(d, {}), 'next': packages.get(parent, {}).get('next', '*')}
    # a package that depends on next but keeps its next.config.* in a subdirectory is not itself the Next.js root
    for d in list(out):
        if 'next' in out[d] and d not in configs and \
                any(c != d and (d == '' or c.startswith(d + '/')) for c in configs):
            out[d] = {k: v for k, v in out[d].items() if k != 'next'}
    return out


def next_roots(root: Path, files: list[Path]) -> list[str]:
    """Directories (relative) that are Next.js project roots: a next.config.* file, or a package.json that
    depends on next."""
    return sorted(d for d, deps in projects(root, files).items() if 'next' in deps)


def project_of(rel: str, projs: dict[str, dict]) -> tuple[str, str]:
    """The nearest package.json directory above a file, and the file's path inside it."""
    parts = rel.split('/')
    for i in range(len(parts) - 1, -1, -1):
        d = '/'.join(parts[:i])
        if d in projs:
            return d, '/'.join(parts[i:])
    return '', rel


def next_path_in_project(rel: str, projs: dict[str, dict]) -> str | None:
    """The path inside its Next.js project when the file's nearest package.json depends on next, else None."""
    d, sub = project_of(rel, projs)
    return sub if 'next' in projs.get(d, {}) else None


def entries(root: Path, files: list[Path]) -> list[Path]:
    out = []
    projs = projects(root, files)
    for f in files:
        rel = f.relative_to(root).as_posix()
        if f.suffix not in SRC_EXT or classify(rel) != 'ui':
            continue
        sub = next_path_in_project(rel, projs)
        d, in_proj = project_of(rel, projs)
        ui_pkg = d != '' and bool(UI_DEPS & set(projs.get(d, {})))  # a UI package inside a monorepo: web/src/main.tsx
        if (sub is not None and (NEXT_ENTRY.match(sub) or NEXT_PAGES.match(sub) or MIDDLEWARE.match(sub))) or \
                OTHER_ENTRY.search(rel) or (ui_pkg and OTHER_ENTRY.search(in_proj)):
            out.append(f)
    out += [h for h in html_entries(root, files) if h not in out]
    return out


def graph(root: Path, files: list[Path] | None = None) -> dict:
    """The import graph from the UI entry points. Returns entries, reachable files (relative paths),
    and per-file imports. 'reachable' is None when no entry point was recognised."""
    root = root.resolve()
    files = files if files is not None else list(walk(root))
    packages = note_packages(root, files)
    alias = aliases(root, files)
    ents = entries(root, files)
    if not ents:
        return {'entries': [], 'reachable': None, 'aliases': [a for a, _ in alias], 'unresolved': 0,
                'unresolved_examples': [], 'warnings': [], 'test_named_packages': sorted(packages)}
    seen: set[Path] = set()
    todo = [e.resolve() for e in ents]
    edges: dict[str, list[str]] = {}
    unresolved = 0
    unresolved_examples: list[str] = []
    alias_prefixes = [a[:-1] for a, _ in alias if a != '*' and a.endswith('*')]
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
                    # a relative path, a configured alias, or a specifier only an alias can mean ("@/x": no npm
                    # package has that name) that did not resolve: the graph is missing an edge
                    if spec.startswith(('.', '/')) or ALIAS_LIKE.match(spec) or \
                            any(p and spec.startswith(p) for p in alias_prefixes):
                        unresolved += 1
                        if len(unresolved_examples) < 10:
                            unresolved_examples.append(f'{rel}: {spec}')
                    continue
                if not t.is_relative_to(root) or 'node_modules' in t.parts:
                    continue
                outs.append(t.relative_to(root).as_posix())
                if t not in seen:
                    todo.append(t)
        edges[rel] = sorted(set(outs))
    reach = sorted(p.relative_to(root).as_posix() for p in seen if p.is_relative_to(root))
    ui_files = [f for f in files if f.suffix in SRC_EXT and not f.name.endswith('.d.ts')
                and classify(f.relative_to(root).as_posix()) == 'ui']
    warnings = []
    if unresolved:
        warnings.append(f'{unresolved} import(s) did not resolve (e.g. {unresolved_examples[0]}): modules behind them '
                        'count as unreachable. Check the path aliases (tsconfig paths, including referenced projects '
                        'such as tsconfig.app.json, the tsconfig beside a nested next.config.* or vite.config.*, and '
                        'vite.config resolve.alias)')
    share = len([r for r in reach if not r.endswith('.d.ts')]) / len(ui_files) if ui_files else 1.0
    if len(ui_files) >= 20 and share < 0.3:
        warnings.append(f'only {round(share * 100)}% of the {len(ui_files)} UI source files are reachable from the '
                        f'{len(ents)} entry point(s): imports the graph cannot follow (an alias it does not know, a '
                        'computed import) would make most of the app look unreachable. Read the unreachable files, '
                        'and use --all-files / --scope all if they are in fact UI code')
    return {'entries': sorted(e.relative_to(root).as_posix() for e in ents), 'reachable': reach,
            'aliases': [a for a, _ in alias], 'unresolved': unresolved, 'unresolved_examples': unresolved_examples,
            'ui_files': len(ui_files), 'reachable_share': round(share, 2), 'warnings': warnings,
            'test_named_packages': sorted(packages), 'edges': edges}


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
        print(f"{len(g['entries'])} entry point(s), {len(g['reachable'] or [])} reachable file(s), "
              f"{g['unresolved']} unresolved import(s) -> {args.out}")
        for w in g.get('warnings') or []:
            print(f'WARN: {w}')
    else:
        print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
