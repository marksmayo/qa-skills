#!/usr/bin/env python3
"""Scan a repository's CSS, JavaScript and HTML for features the browserslist targets don't all support (stdlib).

    python feature_scan.py --repo examples/travel-site --out run/features.json [--query "defaults"] [--built-css .next/static/css]

Patterns are in data/feature-patterns.json; support comes from web-features (the first version of each
core browser that supports the feature), and targets from browserslist_lite.py (the repo's
package.json or .browserslistrc, else Next.js's defaults for a Next.js project, else a Vite project's
build.target or Vite's default, else `defaults`). CSS is read with a small tokeniser (comments removed,
rules split into selectors and declarations, declarations before a nested rule kept with their parent,
@supports blocks tracked); scripts and markup are matched with the patterns' regexes, ignoring comments
in scripts. The walk prunes dependency, build and cache folders (node_modules, .next, dist, build, out,
coverage, ...) before descending, and leaves out what doesn't ship to browsers: test and story files
(*.test.*, *.spec.*, *.e2e.*, *.cy.*, *.stories.*), test directories (e2e, test, tests, spec, specs,
unless the directory holds a package manifest, which makes it a product package of that name), and
Node-only scripts (a node shebang, or an import of a node: module or of fs, child_process and other
Node built-ins: CLIs, build scripts, dev servers). `skipped` lists the test directories and Node-only
files left out.

CSS-in-JS: when package.json depends on Emotion, MUI, styled-components (CSS_IN_JS_DEPS), scripts that use
it are also read for CSS written literally: object styles (camelCase property with a string value, or a
selector / at-rule key) and tagged template literals (styled.div`...`, css`...`). Those uses carry
via "css-in-js", `css_in_js` records the libraries, and a limitation says runtime-computed styles were
not scanned. Minified built CSS (lines over 1000 characters) gives each use a column and collapses
repeats of one feature on one line (occurrences). With Tailwind 4, its `@layer properties` fallback in
built CSS counts as the fallback for @property, and targets below Tailwind 4's own browser floor are
listed (utility_css.targets_below_floor) with a limitation.

Utility-class CSS: when package.json depends on Tailwind CSS, class strings in scripts and markup are
matched against the `utility-class` patterns (known non-baseline utilities such as text-balance,
backdrop-blur-*, bg-clip-text, has-[...], @container), and `utility_css` records that only those were
scanned. The CSS such a framework generates is scanned only when built CSS is given (--built-css, or
.next/static/css when it exists, in the repo or beside any next.config.* up to three levels down, for a
Next.js project kept in a subfolder); otherwise `limitations` says it wasn't, for the report to carry.

A target is unsupported when web-features gives no version for its browser, or a later one. Browsers
web-features doesn't cover (Opera, Samsung Internet, UC, QQ, KaiOS, Opera Mini, Android WebView) are
`not_assessed` for that feature. A fallback is an @supports block around the use, or one of the
pattern's fallback regexes in the same file. Every finding's status is `possible`: it becomes
`confirmed_at_runtime` only when compare_engines.py links it to a difference an engine produced.
"""
from __future__ import annotations
import argparse, json, os, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
import browserslist_lite as bl  # noqa: E402

WF_BROWSER = {'chrome': 'chrome', 'and_chr': 'chrome_android', 'edge': 'edge', 'firefox': 'firefox', 'and_ff': 'firefox_android',
              'safari': 'safari', 'ios_saf': 'safari_ios'}
SKIP_DIRS = {'node_modules', '.git', 'dist', 'build', 'out', 'vendor', '__pycache__', '.next', '.nuxt', 'coverage', '.venv', 'venv',
             '.turbo', '.cache', '.svelte-kit', '.vercel', 'playwright-report', 'test-results', 'storybook-static',
             '__tests__', '__mocks__'}
TEST_FILE = re.compile(r'\.(test|spec|e2e|cy|stories)\.[cm]?[jt]sx?$')   # tests and stories don't ship to browsers
TEST_DIR_NAMES = {'e2e', 'test', 'tests', 'spec', 'specs'}
MANIFESTS = ('package.json', 'pyproject.toml', 'setup.py', 'setup.cfg', 'Cargo.toml', 'go.mod', 'pom.xml', 'build.gradle',
             'build.gradle.kts', 'composer.json', 'Gemfile', 'deno.json')
INLINE_BLOCK = re.compile(r'<(script|style)\b[^>]*>', re.I)
SCRIPT_SUFFIXES = ('.js', '.mjs', '.cjs', '.ts', '.mts', '.cts')
NODE_ONLY = re.compile(r'''\A#!.*\bnode\b|(?:\bfrom\s*|\brequire\(\s*|\bimport\(\s*|^\s*import\s+)['"](?:node:[\w/]+|'''
                       r'''(?:fs|child_process|worker_threads|readline|net|cluster|dgram|tls|v8|vm|inspector)(?:/promises)?)['"]''',
                       re.M)
SUFFIXES = ('.css', '.scss', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.html', '.htm', '.vue', '.svelte')
MARKUP = ('.html', '.htm', '.vue', '.svelte')
CLASS_SOURCES = ('.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx') + MARKUP
# browsers a CSS framework major itself requires (its documented floor), web-features browser keys of caniuse names
FRAMEWORK_FLOORS = {('tailwind', 4): {'chrome': '111', 'edge': '111', 'firefox': '128', 'safari': '16.4', 'ios_saf': '16.4'}}


def css_rules(text: str, columns: bool = False):
    """Yield (selector, declarations, line, inside_supports_condition, ancestors) for every rule, with nesting depth;
    with columns, a sixth item: the rule's column on its line (minified CSS puts every rule on line 1)."""
    # comments go; with columns they are blanked instead, so offsets stay true
    text = re.sub(r'/\*.*?\*/', (lambda m: re.sub(r'[^\n]', ' ', m.group(0))) if columns else
                  (lambda m: '\n' * m.group(0).count('\n')), text, flags=re.S)
    stack: list[tuple[str, int, int]] = []
    buf, start_line, start_col, i, line, line_start = '', 1, 1, 0, 1, 0
    while i < len(text):
        ch = text[i]
        if ch == '\n':
            line += 1
            line_start = i + 1
        if ch == '{':
            if ';' in buf and stack:
                # nested CSS (SCSS, CSS nesting, CSS-in-JS): declarations before a nested rule belong to the parent
                decl, buf = buf.rsplit(';', 1)
                psel, pln, pcol = stack[-1]
                row = (psel, '' if psel.startswith('@') else decl.strip(), pln,
                       ' '.join(s for s, _, _ in stack[:-1] if s.startswith('@supports')), [s for s, _, _ in stack[:-1]])
                yield row + (pcol,) if columns else row
            stack.append((buf.strip(), start_line, start_col))
            buf, start_line, start_col = '', line, i - line_start + 2
        elif ch == '}':
            sel, ln, col = stack.pop() if stack else ('', line, 1)
            supports = ' '.join(s for s, _, _ in stack if s.startswith('@supports'))
            row = (sel, '' if sel.startswith('@') else buf.strip(), ln, supports, [s for s, _, _ in stack])
            yield row + (col,) if columns else row
            buf, start_line, start_col = '', line, i - line_start + 2
        else:
            if not buf.strip():
                start_line, start_col = line, i - line_start + 1
            buf += ch
        i += 1


def decls(block: str) -> list[tuple[str, str]]:
    out = []
    for d in block.split(';'):
        if ':' in d:
            k, v = d.split(':', 1)
            out.append((k.strip().lower(), v.strip().lower()))
    return out


def is_test_dir(d: Path) -> bool:
    """A directory named e2e, test, tests, spec or specs is test code only when it holds no package manifest: with one
    (package.json, pyproject.toml, Cargo.toml, ...) it is a product package that happens to have that name (a pnpm
    workspace package called e2e), and is scanned like any other. The same rule as the shared diff_scope.py."""
    return d.name.lower() in TEST_DIR_NAMES and not any((d / m).is_file() for m in MANIFESTS)


def node_only(text: str) -> bool:
    """A script that runs under Node, never in a browser: a node shebang, or an import of a node: module or of a Node
    built-in that has no browser counterpart (fs, child_process, worker_threads, ...)."""
    return bool(NODE_ONLY.search(text[:20000]))


def files(repo: Path, skipped: dict | None = None):
    """Source files, walking with excluded directories pruned before descending (a node_modules tree is never listed).
    Test directories (is_test_dir), test and story files, and Node-only scripts (CLIs, build scripts, servers) don't ship
    to browsers and are left out; with `skipped`, the test directories and Node-only files are listed there."""
    for root, dirs, names in os.walk(repo):
        keep = []
        for d in sorted(dirs):
            if d in SKIP_DIRS:
                continue
            if is_test_dir(Path(root) / d):
                if skipped is not None:
                    skipped.setdefault('test_dirs', []).append(rel_name(Path(root) / d, repo))
                continue
            keep.append(d)
        dirs[:] = keep
        for n in sorted(names):
            p = Path(root) / n
            if p.suffix not in SUFFIXES or TEST_FILE.search(n):
                continue
            if p.suffix in SCRIPT_SUFFIXES:
                try:
                    text = p.read_text(encoding='utf-8', errors='replace')
                except OSError:
                    text = ''
                if node_only(text):
                    # its own code runs in Node; pages it renders from template literals ship their inline blocks
                    html = bool(INLINE_BLOCK.search(text))
                    if skipped is not None:
                        skipped.setdefault('node_only_files', []).append(rel_name(p, repo) + (' (inline <script>/<style> blocks scanned)' if html else ''))
                    if html:
                        yield p, True
                    continue
            yield p, None


def next_built_css(repo: Path, depth: int = 3) -> list[Path]:
    """The built CSS of every Next.js project in the repo: <dir>/.next/static/css for the repo itself and for each folder
    (up to `depth` levels down, the walk pruned) holding a next.config.* file, as in a project kept under app/ and built
    with `next build app`, or one app of a workspace."""
    out = []
    for root, dirs, names in os.walk(repo):
        here = Path(root)
        level = len(here.relative_to(repo).parts)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS) if level < depth else []
        if here == repo or any(n.startswith('next.config.') for n in names):
            css = here / '.next' / 'static' / 'css'
            if css.is_dir():
                out.append(css)
    return out


def built_css_files(paths: list[Path]):
    for b in paths:
        if b.is_file():
            yield b
        elif b.is_dir():
            for root, dirs, names in os.walk(b):
                dirs[:] = sorted(d for d in dirs if d != 'node_modules')
                yield from (Path(root) / n for n in sorted(names) if n.endswith('.css'))


def utility_framework(repo: Path) -> dict | None:
    """Tailwind CSS, when package.json depends on it: its version, from the installed package or the range."""
    pkg = repo / 'package.json'
    if not pkg.is_file():
        return None
    try:
        d = json.loads(pkg.read_text(encoding='utf-8'))
    except ValueError:
        return None
    deps = {**(d.get('devDependencies') or {}), **(d.get('dependencies') or {})}
    name = next((k for k in ('tailwindcss', '@tailwindcss/postcss', '@tailwindcss/vite') if k in deps), None)
    if not name:
        return None
    version = deps[name]
    inst = repo / 'node_modules' / 'tailwindcss' / 'package.json'
    if inst.is_file():
        try:
            version = json.loads(inst.read_text(encoding='utf-8')).get('version') or version
        except ValueError:
            pass
    return {'framework': 'tailwind', 'version': version}


def strip_js_comments(t: str) -> str:
    return re.sub(r'//[^\n]*|/\*.*?\*/', lambda m: ' ' * len(m.group(0).split('\n')[0]) + '\n' * m.group(0).count('\n'), t, flags=re.S)


def rel_name(f: Path, repo: Path) -> str:
    try:
        return f.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return f.as_posix()


def scan(repo: Path, patterns: list[dict], utility: bool = False, built: list[Path] | None = None,
         css_in_js: bool = False, tailwind_major: int | None = None, skipped: dict | None = None) -> list[dict]:
    """Uses of every pattern in the repo's sources; utility-class patterns only when `utility` (Tailwind found); CSS
    written in scripts when `css_in_js` (a CSS-in-JS library found); the built CSS files with the CSS patterns."""
    uses = []
    for f, markup in files(repo, skipped):
        uses += scan_file(f, rel_name(f, repo), patterns, utility, css_in_js, markup=markup)
    for f in built_css_files(built or []):
        uses += scan_file(f, rel_name(f, repo), patterns, False, tailwind_major=tailwind_major)
    return uses


CSS_IN_JS_DEPS = ('@emotion/react', '@emotion/styled', '@emotion/css', 'styled-components', '@mui/material', '@mui/system',
                  '@mui/joy', '@stitches/react', 'goober', '@linaria/core')
CSS_IN_JS_FILE = re.compile(r'''from\s+['"](@emotion/|@mui/|styled-components|@stitches/|goober|@linaria/)|\bsx\s*[=:]|'''
                            r'''\bstyled\s*[.(]|\bcss\s*`|\bmakeStyles\s*\(''')
# a tagged template literal holding CSS: styled.div`...`, styled(Box)`...`, css`...`, keyframes`...`, createGlobalStyle`...`
CSS_TAG = re.compile(r'\b(?:styled(?:\.\w+|\([^()`]*\))(?:\.attrs\([^`]*?\))?|css|keyframes|createGlobalStyle|injectGlobal|globalCss)\s*`')
# an object-style entry: backdropFilter: 'blur(4px)', '&:has(> img)': {...}, '@container (min-width: 30em)': {...}
OBJ_ENTRY = re.compile(r'''(?<![\w$.])(?:(['"])([^'"\n]{1,120})\1|([A-Za-z_$][\w$]*))\s*:\s*(?:(['"`])([^'"`\n]{0,200})\4|(\{))''')


def css_in_js_libraries(repo: Path) -> list[str]:
    pkg = repo / 'package.json'
    try:
        d = json.loads(pkg.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    deps = {**(d.get('devDependencies') or {}), **(d.get('dependencies') or {})}
    return [k for k in CSS_IN_JS_DEPS if k in deps]


def template_css(js: str) -> list[tuple[str, int]]:
    """CSS from tagged template literals, ${...} replaced by a placeholder; (css, line offset). Bare declarations
    (styled.div`color: red`) are wrapped in a rule so the tokeniser sees them."""
    out = []
    for m in CSS_TAG.finditer(js):
        i, depth, buf = m.end(), 0, []
        while i < len(js):
            c = js[i]
            if c == '\\':
                buf.append(js[i:i + 2])
                i += 2
                continue
            if depth == 0 and c == '`':
                break
            if js.startswith('${', i):
                depth += 1
                i += 2
                continue
            if depth and c == '}':
                depth -= 1
                if depth == 0:
                    buf.append('0')
                i += 1
                continue
            if depth == 0:
                buf.append(c)
            elif c == '\n':
                buf.append('\n')
            i += 1
        css = ''.join(buf)
        offset = js[:m.end()].count('\n')
        out.append((css if re.search(r'[{}]', css) and not re.match(r'\s*[\w-]+\s*:', css) else '& {' + css + '}', offset))
    return out


def kebab(key: str) -> str:
    """backdropFilter -> backdrop-filter, WebkitBackdropFilter -> -webkit-backdrop-filter."""
    k = re.sub(r'([A-Z])', lambda m: '-' + m.group(1).lower(), key)
    return k if not k.startswith(('-webkit', '-moz', '-ms')) or key[0].isupper() else k.lstrip('-')


def object_style_uses(js: str, rel: str, patterns: list[dict], fb_of) -> list[dict]:
    """CSS features in object styles (sx props, styled({...}), theme styleOverrides): a camelCase property with a
    string value, or a selector / at-rule key."""
    uses = []
    lines = js.splitlines()
    for m in OBJ_ENTRY.finditer(js):
        key = m.group(2) or m.group(3)
        val = m.group(5)
        nested = bool(m.group(6))
        ln = js[:m.start()].count('\n') + 1
        snippet = lines[ln - 1].strip()[:160] if ln - 1 < len(lines) else ''
        for p in patterns:
            k, hit = p['kind'], False
            if nested and (key.startswith(('&', ':', '@')) or ':' in key):
                if k == 'css-selector' and p['match'] in key:
                    hit = True
                elif k == 'css-pseudo-regex' and re.search(p['match'], key):
                    hit = True
                elif k == 'css-at-rule' and key.startswith(p['match']):
                    hit = True
            elif val is not None and k == 'css-declaration' and re.fullmatch(r'[A-Za-z]+', key):
                prop = kebab(key)
                hit = bool(re.fullmatch(p['property'], prop)) and (not p.get('value') or p['value'] in val.lower())
            if hit:
                uses.append({'feature': p['feature'], 'file': rel, 'line': ln, 'snippet': snippet,
                             'selector': key if nested else None, 'fallback': fb_of(p), 'via': 'css-in-js'})
    return uses


def scan_file(f: Path, rel: str, patterns: list[dict], utility: bool, css_in_js: bool = False,
              tailwind_major: int | None = None, markup: bool | None = None) -> list[dict]:
    """Uses in one file. `markup` reads a script as markup: only its inline <script> and <style> blocks and the html
    patterns (a Node server that renders pages from template literals ships those blocks, not its own code)."""
    uses = []
    text = f.read_text(encoding='utf-8', errors='replace')
    markup = f.suffix in MARKUP if markup is None else markup
    css_parts, js_parts = [], []
    if f.suffix in ('.css', '.scss'):
        css_parts.append((text, 0))
    elif markup:
        for m in re.finditer(r'<style[^>]*>(.*?)</style>', text, re.S | re.I):
            css_parts.append((m.group(1), text[:m.start(1)].count('\n')))
        for m in re.finditer(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', text, re.S | re.I):
            js_parts.append((m.group(1), text[:m.start(1)].count('\n')))
    else:
        js_parts.append((text, 0))
    fb_of = lambda p: bool(p.get('emits_fallback')) or any(re.search(x, text) for x in p.get('fallback') or [])
    if css_in_js and f.suffix in CLASS_SOURCES and not markup and CSS_IN_JS_FILE.search(text):
        clean = strip_js_comments(text)
        css_parts += template_css(clean)
        uses += object_style_uses(clean, rel, [p for p in patterns if p['kind'].startswith('css')], fb_of)
    # minified CSS (built output): every rule is on line 1, so uses carry a column and repeats of one feature collapse
    minified = f.suffix == '.css' and any(len(l) > 1000 for l in text.splitlines())
    # Tailwind 4 emits @layer properties { @supports (...) { *, ::before, ::after { --tw-...: initial } } } as the
    # fallback for engines without @property: its registered properties then fall back to plain custom properties
    tw4_props = bool(tailwind_major and tailwind_major >= 4 and re.search(r'@layer\s+properties\s*\{\s*@supports', text)
                     and '--tw-' in text)
    for p in patterns:
        fb = fb_of(p) or (tw4_props and p['feature'] == 'registered-custom-properties')
        k = p['kind']
        if k.startswith('css'):
            for css, offset in css_parts:
                for sel, block, ln, supports, ancestors, col in css_rules(css, columns=True):
                    hit = None
                    if k == 'css-selector' and p['match'] in sel:
                        hit = sel
                    elif k == 'css-pseudo-regex' and re.search(p['match'], sel):
                        hit = sel
                    elif k == 'css-at-rule' and sel.startswith(p['match']):
                        hit = sel
                    elif k == 'css-declaration':
                        for prop, val in decls(block):
                            if re.fullmatch(p['property'], prop) and (not p.get('value') or p['value'] in val):
                                hit = f'{sel} {{ {prop}: {val} }}'
                    elif k == 'css-flex-gap':
                        ds = dict(decls(block))
                        if ds.get('display') in ('flex', 'inline-flex') and any(g in ds for g in ('gap', 'row-gap', 'column-gap')):
                            hit = f'{sel} {{ display: {ds["display"]}; gap: {ds.get("gap") or ds.get("row-gap") or ds.get("column-gap")} }}'
                    if hit:
                        in_supports = bool(supports) and any(re.search(x, supports) for x in p.get('fallback') or [])
                        use = {'feature': p['feature'], 'file': rel, 'line': ln + offset, 'snippet': hit[:160],
                               'selector': sel if not sel.startswith('@') else None, 'fallback': in_supports or fb}
                        if minified:
                            use['column'] = col
                        if f.suffix not in ('.css', '.scss') and not markup:
                            use['via'] = 'css-in-js'   # CSS from a tagged template literal in a script
                        uses.append(use)
        elif k == 'js-api':
            for js, offset in js_parts:
                clean = strip_js_comments(js)
                for m in re.finditer(p['match'], clean):
                    ln = clean[:m.start()].count('\n') + 1 + offset
                    uses.append({'feature': p['feature'], 'file': rel, 'line': ln,
                                 'snippet': js.splitlines()[ln - offset - 1].strip()[:160], 'selector': None, 'fallback': fb})
        elif k == 'html' and markup:
            for m in re.finditer(p['match'], text):
                ln = text[:m.start()].count('\n') + 1
                uses.append({'feature': p['feature'], 'file': rel, 'line': ln, 'snippet': text.splitlines()[ln - 1].strip()[:160],
                             'selector': None, 'fallback': fb})
        elif k == 'utility-class' and utility and f.suffix in CLASS_SOURCES:
            # a class token, optionally behind variants ("md:hover:backdrop-blur-xl"), in any string of the file
            clean = strip_js_comments(text) if not markup else text
            rx = r'(?<![\w/-])(?:[\w\[\]@&*>=.()-]+:)*!?(?:' + p['match'] + r')'
            for m in re.finditer(rx, clean):
                ln = clean[:m.start()].count('\n') + 1
                uses.append({'feature': p['feature'], 'file': rel, 'line': ln, 'snippet': text.splitlines()[ln - 1].strip()[:160],
                             'selector': None, 'fallback': fb})
    if minified:  # one entry per feature and line, with how many uses it stands for and the first one's column
        merged: dict[tuple, dict] = {}
        for u in uses:
            m = merged.get((u['feature'], u['line']))
            if m is None:
                merged[(u['feature'], u['line'])] = dict(u, occurrences=1)
            else:
                m['occurrences'] += 1
                m['fallback'] = m['fallback'] and u['fallback']
        uses = list(merged.values())
    return uses


def support_for(feature: str, pattern: dict, wf: dict) -> tuple[dict | None, str | None]:
    if pattern.get('support'):
        return pattern['support'], pattern.get('baseline')
    f = wf.get(feature)
    if not f:
        return None, None
    st = f.get('status') or {}
    b = st.get('baseline')
    return st.get('support') or {}, ('high' if b == 'high' else 'low' if b == 'low' else 'limited')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--query')
    ap.add_argument('--built-css', type=Path, action='append',
                    help='a built CSS file or folder to scan as well (repeatable); default .next/static/css when it exists')
    a = ap.parse_args()
    from fetch_data import asset_path
    caniuse = json.loads(asset_path('caniuse-db').read_text(encoding='utf-8'))
    wf = json.loads(asset_path('web-features').read_text(encoding='utf-8'))['features']
    pats = json.loads((SKILL / 'data' / 'feature-patterns.json').read_text(encoding='utf-8'))['patterns']
    query, source = (a.query, 'argument') if a.query else bl.query_from_repo(a.repo)
    targets = bl.Resolver(caniuse).resolve(query)
    built = list(a.built_css or [])
    if not built:
        built = next_built_css(a.repo)
    built_files = [rel_name(f, a.repo) for f in built_css_files(built)]
    fw = utility_framework(a.repo)
    tw_major = None
    if fw:
        m = re.search(r'(\d+)', str(fw['version']))
        tw_major = int(m.group(1)) if m else None
    cij = css_in_js_libraries(a.repo)
    skipped: dict = {}
    uses = scan(a.repo, pats, utility=bool(fw), built=built, css_in_js=bool(cij), tailwind_major=tw_major, skipped=skipped)
    limitations = []
    if fw:
        fw.update(classes='known non-baseline utilities only (utility-class patterns in data/feature-patterns.json)',
                  built_css=built_files)
        if not built_files:
            limitations.append(f'Utility-class CSS ({fw["framework"]} {fw["version"]}) was scanned only for the known non-baseline '
                               'utilities; the CSS it generates (colour functions, @property, cascade layers, nesting) was not '
                               'scanned: build the site and pass --built-css')
        floor = FRAMEWORK_FLOORS.get(('tailwind', tw_major)) if tw_major else None
        if floor:
            low: dict[str, list[str]] = {}
            for t in targets['resolved']:
                if t['browser'] in floor and bl.vnum(t['version']) < bl.vnum(floor[t['browser']]):
                    low.setdefault(t['browser'], []).append(t['version'])
            below = []
            for b, vs in sorted(low.items()):
                vs = sorted(vs, key=bl.vnum)
                below.append(f'{b} {vs[0]}' + (f'-{vs[-1]}' if len(vs) > 1 else ''))
            fw['framework_floor'] = {b: v for b, v in floor.items()}
            if below:
                fw['targets_below_floor'] = below
                limitations.append(f'Tailwind CSS {tw_major} itself requires {", ".join(f"{b} {v}" for b, v in floor.items())} '
                                   f'or later, above some declared targets ({", ".join(below[:6])}'
                                   f'{", ..." if len(below) > 6 else ""}): the project\'s real support floor is ambiguous, '
                                   'and its utility CSS is not expected to work below it')
    elif built_files:
        fw = {'framework': None, 'version': None, 'classes': 'not scanned', 'built_css': built_files}
    css_in_js = None
    if cij:
        n = sum(1 for u in uses if u.get('via') == 'css-in-js')
        css_in_js = {'libraries': cij, 'uses': n,
                     'scanned': 'styles written literally in scripts that use the library: object styles (sx props, '
                                'styled({...}), theme styleOverrides) and tagged template literals (styled.div`...`, css`...`)'}
        limitations.append(f'CSS-in-JS ({", ".join(cij)}) generates its CSS at runtime: only styles written literally in '
                           'scripts were scanned; styles computed at runtime (theme functions, spread or conditional '
                           'objects, values from variables) were not, and neither the source scan nor --built-css sees them')
    findings = []
    by_feature: dict[str, list] = {}
    for u in uses:
        by_feature.setdefault(u['feature'], []).append(u)
    for feat, us in by_feature.items():
        pat = next(p for p in pats if p['feature'] == feat)
        support, baseline = support_for(feat, pat, wf)
        if support is None:
            findings.append({'feature': feat, 'name': pat['name'], 'baseline_status': None, 'files': us, 'unsupported_in': [],
                             'not_assessed': ['the feature is not in web-features'], 'fallback': all(u['fallback'] for u in us),
                             'status': 'not_assessed', 'linked_differences': []})
            continue
        unsupported, not_assessed = [], set()
        for t in targets['resolved']:
            key = WF_BROWSER.get(t['browser'])
            if not key:
                not_assessed.add(t['browser'])
                continue
            first = support.get(key)
            if first is None or bl.vnum(first) > bl.vnum(t['version']):
                unsupported.append({'browser': t['browser'], 'version': t['version'], 'engine': t['engine'], 'usage': t['usage'],
                                    'first_supported': first})
        if not unsupported:
            continue
        findings.append({'feature': feat, 'name': pat['name'], 'baseline_status': baseline,
                         'files': [{k: u[k] for k in ('file', 'line', 'column', 'occurrences', 'snippet', 'selector',
                                                       'fallback', 'via') if k in u} for u in us],
                         'unsupported_in': unsupported, 'unsupported_usage': round(sum(x['usage'] for x in unsupported), 3),
                         'not_assessed': sorted(not_assessed), 'fallback': all(u['fallback'] for u in us),
                         'status': 'possible', 'linked_differences': []})
    out = {'repo': a.repo.as_posix(), 'browserslist': {'query': query, 'source': source, 'resolved_count': len(targets['resolved']),
                                                       'coverage': targets['coverage'], 'unresolved': targets['unresolved']},
           'uses': sum(u.get('occurrences', 1) for u in uses), 'features': findings}
    if fw:   # only written when there is something to say, so a plain CSS/JS repo's output is unchanged
        out['utility_css'] = fw
    if css_in_js:
        out['css_in_js'] = css_in_js
    if skipped:   # test directories and Node-only scripts left out (they don't ship to browsers); only written when any
        out['skipped'] = skipped
    if limitations:
        out['limitations'] = limitations
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    print(f'{len(uses)} use(s), {len(findings)} feature finding(s) for "{query}" -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
