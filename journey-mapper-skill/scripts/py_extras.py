#!/usr/bin/env python3
"""Interface idioms the shared route reader (py_routes.py) does not cover, read from source (stdlib only).

Used by list_interfaces.py (and, as an identical copy, by the journey mapper's extract_surface.py):

- conditions: the `if` tests a statement sits under (`if args.uploads: app.include_router(...)`), so a route or
  middleware that exists only in one serving mode says so (`condition`).
- static mounts: `app.mount("/", StaticFiles(directory="dist"))` (FastAPI and Starlette) and Flask's default
  `/static/<path:filename>` route, as routes.
- middleware: `app.add_middleware(Cls, ...)` and `@app.middleware("http")` functions, with their condition.
- command lines: a flags-only argparse program (no sub-commands) as one command with its options, and a
  hand-written `command = sys.argv[1]; if command == "pack":` dispatch as sub-commands.
- PHP front controllers: `preg_match('#^/x/(.*)#', $uri)` and `$uri === '/x'` on REQUEST_URI, read by pattern only.
- build-output folders: whether something in the repository produces a dist/, build/ or out/ folder (a package.json
  build script, a bundler config, a .gitignore entry, a generated-file header); a committed folder that nothing
  produces is hand-written source, served as it is.

    python py_extras.py --self-test
"""
from __future__ import annotations
import ast, json, re, sys
from pathlib import Path, PurePosixPath

BUILD_DIRS = ('dist', 'build', 'out')


def _unparse(node) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return ''


def _name(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _const(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _kw(call: ast.Call, *names):
    for k in call.keywords:
        if k.arg in names:
            return k.value
    return None


# ---------------------------------------------------------------- conditions
MAIN_GUARD = re.compile(r'^__name__\s*==\s*[\'"]__main__[\'"]$')


def if_conditions(tree) -> list[tuple[int, int, str]]:
    """(first line, last line, condition) for each if body and else branch, outermost first."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = _unparse(node.test)
        if MAIN_GUARD.match(test):
            continue
        if node.body:
            out.append((node.body[0].lineno, node.body[-1].end_lineno or node.body[-1].lineno, test))
        if node.orelse:
            out.append((node.orelse[0].lineno, node.orelse[-1].end_lineno or node.orelse[-1].lineno, f'not ({test})'))
    out.sort(key=lambda c: (c[0], -c[1]))
    return out


def condition_at(conds: list[tuple[int, int, str]], line: int) -> str | None:
    """The conditions a line sits under, joined with `and` (outermost first), or None."""
    parts = [c for a, b, c in sorted(conds, key=lambda c: (c[0], -c[1])) if a <= line <= b]
    return ' and '.join(dict.fromkeys(parts)) or None


class Conditions:
    """Condition lookup per module of a py_routes.Project."""

    def __init__(self, project):
        self.project = project
        self.by_rel = {m.rel: if_conditions(m.tree) for m in project.modules.values()}

    def at(self, rel: str, line: int) -> str | None:
        return condition_at(self.by_rel.get(rel, []), line)

    def chain(self, chain: list) -> list[str]:
        """Conditions of the include calls along a mount chain (router keys from the top app down)."""
        out = []
        for parent, child in zip(chain, chain[1:]):
            for e in self.project.mounts.get(child, []):
                if e['parent'] == parent:
                    c = self.at(e['file'], e['line'])
                    if c:
                        out.append(c)
        return out


def join_conditions(parts) -> str | None:
    parts = [p for p in dict.fromkeys(p for p in parts if p)]
    return ' and '.join(parts) or None


def route_conditions(project) -> dict[tuple[str, int, str], str]:
    """{(file, decorator line, mount prefix): condition} for every route declaration that sits under an if, or whose
    router is included under one (`if args.uploads: app.include_router(uploads.router)`)."""
    conds = Conditions(project)
    out = {}
    for h in project.hits():
        m, call, key = h['module'], h['call'], h['key']
        own = conds.at(m.rel, call.lineno)
        try:
            chains = project.mount_chains(key) if key else []
        except Exception:  # noqa: BLE001
            chains = []
        for c in chains or [{'prefix': '', 'chain': []}]:
            cond = join_conditions([own] + conds.chain(c['chain']))
            if cond:
                out[(m.rel, call.lineno, c['prefix'] or '')] = cond
    return out


# ---------------------------------------------------------------- static mounts and middleware
def _join(*parts: str) -> str:
    segs = [p.strip('/') for p in parts if p and p.strip('/')]
    return '/' + '/'.join(segs) if segs else '/'


def static_mounts(project, root: Path | None = None) -> list[dict]:
    """StaticFiles mounts and Flask static folders as routes: {method, path, file, line, framework, directory,
    function, condition?, prefix_status}."""
    conds = Conditions(project)
    out = []
    for m in project.modules.values():
        for node in ast.walk(m.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'mount' and node.args:
                app = node.args[1] if len(node.args) > 1 else _kw(node, 'app')
                if not (isinstance(app, ast.Call) and (_name(app.func) or '').endswith('StaticFiles')):
                    continue
                path = _const(node.args[0]) or m.consts.get(getattr(node.args[0], 'id', None)) or '<dynamic>'
                dnode = _kw(app, 'directory') or (app.args[0] if app.args else None)
                directory = (_const(dnode) or m.consts.get(getattr(dnode, 'id', None))) if dnode is not None else None
                try:
                    key = project.resolve(m, node.func.value)
                    chains = project.mount_chains(key) if key else []
                except Exception:  # noqa: BLE001
                    key, chains = None, []
                own = conds.at(m.rel, node.lineno)
                for c in chains or [{'prefix': '', 'chain': [], 'reached': True}]:
                    cond = join_conditions([own] + conds.chain(c['chain']))
                    out.append({'method': 'GET', 'path': _join(c['prefix'], path, '{path:path}'), 'file': m.rel,
                                'line': node.lineno, 'framework': 'starlette-static', 'function': 'StaticFiles',
                                'directory': directory, 'included': bool(c.get('reached', True)),
                                **({'condition': cond} if cond else {})})
            elif isinstance(node, ast.Call) and _name(node.func) == 'Flask' and root is not None:
                folder = _kw(node, 'static_folder')
                if folder is not None and _const(folder) is None:
                    continue                                       # static_folder=None or computed: not knowable
                folder_s = _const(folder) if folder is not None else 'static'
                url = _kw(node, 'static_url_path')
                url_s = _const(url) if url is not None else '/' + PurePosixPath(folder_s).name
                base = PurePosixPath(m.rel).parent
                if url_s is None or not (root / base / folder_s).is_dir():
                    continue
                out.append({'method': 'GET', 'path': _join(url_s, '<path:filename>'), 'file': m.rel, 'line': node.lineno,
                            'framework': 'flask-static', 'function': 'static', 'directory': folder_s, 'included': True})
    return out


def middleware(project) -> list[dict]:
    """app.add_middleware(Cls, ...) and @app.middleware("http") functions: {file, line, class, owner, condition?}."""
    conds = Conditions(project)
    out = []
    for m in project.modules.values():
        for node in ast.walk(m.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'add_middleware' \
                    and node.args:
                cls = _name(node.args[0]) or _unparse(node.args[0])
                cond = conds.at(m.rel, node.lineno)
                out.append({'file': m.rel, 'line': node.lineno, 'class': cls, 'owner': _unparse(node.func.value),
                            **({'condition': cond} if cond else {})})
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == 'middleware':
                        cond = conds.at(m.rel, dec.lineno)
                        out.append({'file': m.rel, 'line': dec.lineno, 'class': node.name, 'owner': _unparse(dec.func.value),
                                    'kind': 'function', **({'condition': cond} if cond else {})})
    return sorted(out, key=lambda x: (x['file'], x['line']))


# ---------------------------------------------------------------- command lines
def _prog(m, parser_call=None) -> str:
    stem = PurePosixPath(m.rel).stem
    prog = PurePosixPath(m.rel).parent.name if stem in ('__main__', '__init__') else stem
    if parser_call is not None:
        v = _kw(parser_call, 'prog')
        if _const(v):
            prog = _const(v)
    return prog


def _is_argv1(node) -> bool:
    return (isinstance(node, ast.Subscript) and _unparse(node.value) == 'sys.argv'
            and _unparse(node.slice) == '1')


def command_lines(project) -> list[dict]:
    """Flags-only argparse programs ({command, options, file, line, framework 'argparse'}) and sys.argv[1] dispatch
    ({command: 'prog name', file, line, framework 'sys.argv'})."""
    out = []
    for m in project.modules.values():
        calls = [n for n in ast.walk(m.tree) if isinstance(n, ast.Call)]
        parsers = [c for c in calls if _name(c.func) == 'ArgumentParser']
        subs = [c for c in calls if isinstance(c.func, ast.Attribute) and c.func.attr in ('add_subparsers', 'add_parser')]
        if parsers and not subs:
            opts = []
            for c in calls:
                if isinstance(c.func, ast.Attribute) and c.func.attr == 'add_argument' and c.args:
                    first = _const(c.args[0])
                    if first:
                        opts.append(first)
            if opts:
                out.append({'command': _prog(m, parsers[0]), 'options': opts, 'file': m.rel, 'line': parsers[0].lineno,
                            'framework': 'argparse'})
        names = set()
        for n in ast.walk(m.tree):
            if isinstance(n, ast.Assign) and _is_argv1(n.value):
                names |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        seen = set()
        for n in ast.walk(m.tree):
            if not (isinstance(n, ast.Compare) and len(n.ops) == 1):
                continue
            left = n.left
            if not (_is_argv1(left) or (isinstance(left, ast.Name) and left.id in names)):
                continue
            right = n.comparators[0]
            if isinstance(n.ops[0], ast.Eq) and _const(right):
                words = [_const(right)]
            elif isinstance(n.ops[0], ast.In) and isinstance(right, (ast.Tuple, ast.List, ast.Set)):
                words = [_const(e) for e in right.elts if _const(e)]
            else:
                continue
            for w in words:
                if w and not w.startswith('-') and w not in seen:
                    seen.add(w)
                    out.append({'command': f'{_prog(m)} {w}', 'file': m.rel, 'line': n.lineno, 'framework': 'sys.argv'})
    return out


# ---------------------------------------------------------------- PHP
PHP_PREG = re.compile(r'''\bpreg_match\s*\(''')
PHP_PIECE = re.compile(r'''\s*(?:'((?:\\.|[^'\\])*)'|"((?:\\.|[^"\\])*)"|\$(\w+))\s*''')
PHP_LITERAL_VAR = re.compile(r'''^\s*\$(\w+)\s*=\s*(['"])([^'"]*)\2\s*;''', re.M)


def _php_pattern(text: str, pos: int, literals: dict) -> tuple[str | None, str, bool]:
    """(pattern, subject, literal?) of the preg_match call opened at pos: concatenated quoted strings and variables
    ($BASE . '/x/(.*)'); a variable assigned a literal string is substituted, any other leaves the pattern partial."""
    parts, literal = [], True
    while True:
        m = PHP_PIECE.match(text, pos)
        if not m:
            return None, '', False
        if m.group(3) is not None:
            if m.group(3) in literals:
                parts.append(literals[m.group(3)])
            else:
                parts.append('{' + m.group(3) + '}')
                literal = False
        else:
            parts.append(m.group(1) if m.group(1) is not None else m.group(2))
        pos = m.end()
        if text.startswith('.', pos):
            pos += 1
            continue
        break
    if not text.startswith(',', pos):
        return None, '', False
    end = re.search(r'[,)]', text[pos + 1:])
    subject = text[pos + 1: pos + 1 + end.start()] if end else ''
    pat = ''.join(parts)
    if len(pat) < 2:
        return None, subject, literal
    delim = {'(': ')', '{': '}', '[': ']', '<': '>'}.get(pat[0], pat[0])
    body = re.sub(re.escape(delim) + r'[imsxuUD]*$', '', pat[1:])
    return body, subject.strip(), literal
PHP_EQ = re.compile(r'''\$(\w+)\s*===?\s*(['"])(/[^'"]*)\2|(['"])(/[^'"]*)\4\s*===?\s*\$(\w+)''')
PHP_CASE = re.compile(r'''\bcase\s+(['"])(/[^'"]*)\1\s*:''')
URI_VAR = re.compile(r'''\$(\w+)\s*=\s*[^;]*\$_SERVER\s*\[\s*['"](?:REQUEST_URI|PATH_INFO)['"]\s*\]''')


def _regex_path(rx: str) -> tuple[str | None, bool]:
    """(path template, exact?) from a URI regex: groups become {param} ({param:path} for .* and .+)."""
    rx = rx.lstrip('^')
    rx = re.sub(r'\$$', '', rx)
    rx = re.sub(r'\((?:\?P?<\w+>)?\.[*+]\)', '{param:path}', rx)
    rx = re.sub(r'\((?:\?P?<\w+>)?[^()]*\)', '{param}', rx)
    rx = rx.replace('\\/', '/').replace('\\.', '.').replace('\\-', '-')
    exact = not re.search(r'[\\*+?\[\]|()^$]', rx)
    rx = re.sub(r'\.[*+]$', '{rest:path}', rx)
    if not rx.startswith('/'):
        return None, False
    return rx, exact


def php_routes(root: Path, files: list[str]) -> tuple[list[dict], list[str]]:
    """Routes of PHP front controllers that route on REQUEST_URI (pattern reading only), and notes."""
    php = [f for f in files if f.endswith('.php')]
    if not php:
        return [], []
    rewrites = {}
    for f in files:
        if PurePosixPath(f).name == '.htaccess':
            text = (root / f).read_text(encoding='utf-8', errors='replace')
            for mm in re.finditer(r'RewriteRule\s+\S+\s+/?(\S+\.php)', text):
                rewrites[(PurePosixPath(f).parent / mm.group(1)).as_posix().lstrip('./') or mm.group(1)] = f
    routes, controllers = [], []
    for rel in php:
        text = (root / rel).read_text(encoding='utf-8', errors='replace')
        if 'REQUEST_URI' not in text and 'PATH_INFO' not in text:
            continue
        uri_vars = set(URI_VAR.findall(text))
        line = lambda pos: text.count('\n', 0, pos) + 1  # noqa: E731
        found = 0
        literals = {m.group(1): m.group(3) for m in PHP_LITERAL_VAR.finditer(text)}
        for mm in PHP_PREG.finditer(text):
            rx, subject, literal = _php_pattern(text, mm.end(), literals)
            if rx is None:
                continue
            if 'REQUEST_URI' not in subject and 'PATH_INFO' not in subject and subject.lstrip('$') not in uri_vars:
                continue
            path, exact = _regex_path(rx)
            if path:
                note = None if exact and literal else f'from the regex {rx!r}' + ('' if literal else ' (a computed part)') + \
                    '; check by hand'
                routes.append({'method': 'ANY', 'path': path, 'file': rel, 'line': line(mm.start()), 'framework': 'php',
                               'included': True, **({'note': note} if note else {})})
                found += 1
        for mm in PHP_EQ.finditer(text):
            var, path = (mm.group(1), mm.group(3)) if mm.group(1) else (mm.group(6), mm.group(5))
            if var in uri_vars:
                routes.append({'method': 'ANY', 'path': path, 'file': rel, 'line': line(mm.start()), 'framework': 'php',
                               'included': True})
                found += 1
        for mm in PHP_CASE.finditer(text):
            if uri_vars:
                routes.append({'method': 'ANY', 'path': mm.group(2), 'file': rel, 'line': line(mm.start()),
                               'framework': 'php', 'included': True})
                found += 1
        controllers.append((rel, found))
    notes = []
    read = [f'{r} ({n} route(s){", the .htaccess rewrites to it: a front controller" if r in rewrites else ""})'
            for r, n in controllers]
    others = [f for f in php if f not in {r for r, _ in controllers}]
    if read:
        notes.append(f'PHP read by pattern only (preg_match and comparisons on REQUEST_URI): {", ".join(read)}; '
                     f'other routing in PHP is not read, so check these files by hand')
    if others:
        notes.append(f'{len(others)} PHP file(s) not read (no routing on REQUEST_URI found): '
                     f'{", ".join(others[:8])}{" ..." if len(others) > 8 else ""}')
    return routes, notes


# ---------------------------------------------------------------- build-output folders
BUNDLER_CONFIG = re.compile(r'^(webpack|vite|rollup|esbuild|parcel|snowpack|rspack|tsup|gulpfile|Gruntfile|'
                            r'next|nuxt|svelte|astro|angular|vue)\b.*\.(c|m)?(js|ts|json)$|^angular\.json$', re.I)
BUILD_SCRIPT = re.compile(r'\b(vite|webpack|rollup|esbuild|parcel|tsc|next|nuxt|ng|astro|svelte-kit|tsup|rspack|'
                          r'react-scripts|gulp|grunt|babel|sass|emcc|make|python\s+-m\s+build)\b')
GENERATED_HEADER = re.compile(r'(@generated|do not edit|auto-?generated|generated by)', re.I)


def build_producer(root: Path, rel_dir: str) -> str | None:
    """What produces the folder rel_dir (dist/, build/, out/), or None when nothing in the repository does."""
    name = PurePosixPath(rel_dir).name
    d = PurePosixPath(rel_dir).parent
    dirs = []
    while True:
        dirs.append(d)
        if d.as_posix() in ('.', ''):
            break
        d = d.parent
    for base in dirs:
        here = root / base
        gi = here / '.gitignore'
        if gi.is_file():
            for ln in gi.read_text(encoding='utf-8', errors='replace').splitlines():
                ln = ln.strip()
                if ln.strip('/') in (name, rel_dir, f'{name}/*', f'{name}/**'):
                    return f'{(base / ".gitignore").as_posix()} ignores {name}/'
        pkg = here / 'package.json'
        if pkg.is_file():
            try:
                scripts = json.loads(pkg.read_text(encoding='utf-8', errors='replace')).get('scripts') or {}
            except (ValueError, AttributeError):
                scripts = {}
            for k, v in scripts.items():
                if isinstance(v, str) and (k in ('build', 'prepare', 'prepublishOnly', 'dist') or BUILD_SCRIPT.search(v)) \
                        and (BUILD_SCRIPT.search(v) or name in v):
                    return f'{(base / "package.json").as_posix()} script {k!r} ({v[:60]})'
        try:
            entries = [p.name for p in here.iterdir() if p.is_file()]
        except OSError:
            entries = []
        for e in entries:
            if BUNDLER_CONFIG.match(e):
                return f'{(base / e).as_posix()} (a bundler or framework config)'
        for e in ('setup.py', 'pyproject.toml', 'Makefile', 'CMakeLists.txt', 'Cargo.toml', 'pom.xml', 'build.gradle'):
            if e in entries and name in ('build', 'dist', 'out'):
                text = (here / e).read_text(encoding='utf-8', errors='replace')
                if e in ('setup.py', 'pyproject.toml', 'Cargo.toml', 'pom.xml', 'build.gradle') or f'{name}/' in text:
                    return f'{(base / e).as_posix()} (a build tool that writes {name}/)'
    folder = root / rel_dir
    try:
        sample = sorted(p for p in folder.rglob('*') if p.is_file())[:40]
    except OSError:
        sample = []
    gen = [p for p in sample if p.suffix in ('.js', '.css', '.html', '.mjs')
           and GENERATED_HEADER.search(p.read_text(encoding='utf-8', errors='replace')[:400])]
    if sample and len(gen) * 2 > len(sample):
        return f'{len(gen)} of {len(sample)} sampled files carry a generated-file header'
    if sample and all(p.suffix in ('.map', '.whl', '.gz', '.zip', '.tar', '.egg', '.exe', '.dll', '.so', '.o', '.class',
                                   '.pyc', '.jar') for p in sample):
        return 'only packaged or compiled artefacts'
    return None


def self_test() -> int:
    import tempfile, shutil
    fails = []
    src = ('import argparse, sys\nfrom fastapi import FastAPI\nfrom fastapi.staticfiles import StaticFiles\n'
           'p = argparse.ArgumentParser()\np.add_argument("--port")\nargs = p.parse_args()\napp = FastAPI()\n'
           'if args.port:\n    app.add_middleware(Gate)\nelse:\n    app.mount("/s", StaticFiles(directory="pub"))\n'
           'cmd = sys.argv[1]\nif cmd in ("a", "b"):\n    pass\n')
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import py_routes
    proj = py_routes.Project({'srv.py': src})
    st = static_mounts(proj)
    if not (st and st[0]['path'] == '/s/{path:path}' and st[0].get('condition') == 'not (args.port)'):
        fails.append(f'static mount in an else branch: {st}')
    mw = middleware(proj)
    if not (mw and mw[0]['class'] == 'Gate' and mw[0].get('condition') == 'args.port'):
        fails.append(f'conditional middleware: {mw}')
    cl = {c['command'] for c in command_lines(proj)}
    if not {'srv', 'srv a', 'srv b'} <= cl:
        fails.append(f'command lines: {cl}')
    for rx, want in (('^/x/(.*)', '/x/{param:path}'), ('^/api/([^/]+)/info$', '/api/{param}/info'), ('^/x$', '/x')):
        if _regex_path(rx)[0] != want:
            fails.append(f'_regex_path({rx!r}) = {_regex_path(rx)[0]!r}, want {want!r}')
    tmp = Path(tempfile.mkdtemp(prefix='pyx-'))
    try:
        (tmp / 'dist').mkdir()
        (tmp / 'dist' / 'index.html').write_text('<p>hand written</p>', encoding='utf-8')
        if build_producer(tmp, 'dist') is not None:
            fails.append('a committed dist/ that nothing produces has no producer')
        (tmp / 'vite.config.ts').write_text('export default {}', encoding='utf-8')
        if build_producer(tmp, 'dist') is None:
            fails.append('a bundler config produces dist/')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print('py_extras self-test FAILED:\n  - ' + '\n  - '.join(fails))
        return 1
    print('py_extras self-test: ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(self_test() if '--self-test' in sys.argv[1:] else (print(__doc__) or 0))
