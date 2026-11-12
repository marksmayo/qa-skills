#!/usr/bin/env python3
"""Scan a repository's CSS, JavaScript and HTML for features the browserslist targets don't all support (stdlib).

    python feature_scan.py --repo examples/travel-site --out run/features.json [--query "defaults"] [--built-css .next/static/css]

Patterns are in data/feature-patterns.json; support comes from web-features (the first version of each
core browser that supports the feature), and targets from browserslist_lite.py (the repo's
package.json or .browserslistrc, else Next.js's defaults for a Next.js project, else `defaults`). CSS is
read with a small tokeniser (comments removed, rules split into selectors and declarations, @supports
blocks tracked); scripts and markup are matched with the patterns' regexes, ignoring comments in scripts.
The walk prunes dependency, build and cache folders (node_modules, .next, dist, build, out, coverage, ...)
before descending.

Utility-class CSS: when package.json depends on Tailwind CSS, class strings in scripts and markup are
matched against the `utility-class` patterns (known non-baseline utilities such as text-balance,
backdrop-blur-*, bg-clip-text, has-[...], @container), and `utility_css` records that only those were
scanned. The CSS such a framework generates is scanned only when built CSS is given (--built-css, or
.next/static/css when it exists); otherwise `limitations` says it wasn't, for the report to carry.

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
TEST_FILE = re.compile(r'\.(test|spec|stories)\.[cm]?[jt]sx?$')   # tests and stories don't ship to browsers
SUFFIXES = ('.css', '.scss', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.html', '.htm', '.vue', '.svelte')
MARKUP = ('.html', '.htm', '.vue', '.svelte')
CLASS_SOURCES = ('.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx') + MARKUP


def css_rules(text: str):
    """Yield (selector, declarations, line, inside_supports_condition) for every rule, with nesting depth."""
    text = re.sub(r'/\*.*?\*/', lambda m: '\n' * m.group(0).count('\n'), text, flags=re.S)
    stack: list[tuple[str, int]] = []
    buf, start_line, i, line = '', 1, 0, 1
    while i < len(text):
        ch = text[i]
        if ch == '\n':
            line += 1
        if ch == '{':
            stack.append((buf.strip(), start_line))
            buf, start_line = '', line
        elif ch == '}':
            sel, ln = stack.pop() if stack else ('', line)
            supports = ' '.join(s for s, _ in stack if s.startswith('@supports'))
            if not sel.startswith('@'):
                yield sel, buf.strip(), ln, supports, [s for s, _ in stack]
            elif sel.startswith('@'):
                yield sel, '', ln, supports, [s for s, _ in stack]
            buf, start_line = '', line
        else:
            if not buf.strip():
                start_line = line
            buf += ch
            if ch == ';' and stack and not stack[-1][0].startswith('@'):
                pass
        i += 1


def decls(block: str) -> list[tuple[str, str]]:
    out = []
    for d in block.split(';'):
        if ':' in d:
            k, v = d.split(':', 1)
            out.append((k.strip().lower(), v.strip().lower()))
    return out


def files(repo: Path):
    """Source files, walking with excluded directories pruned before descending (a node_modules tree is never listed)."""
    for root, dirs, names in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for n in sorted(names):
            p = Path(root) / n
            if p.suffix in SUFFIXES and not TEST_FILE.search(n):
                yield p


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


def scan(repo: Path, patterns: list[dict], utility: bool = False, built: list[Path] | None = None) -> list[dict]:
    """Uses of every pattern in the repo's sources; utility-class patterns only when `utility` (Tailwind found); the
    built CSS files with the CSS patterns."""
    uses = []
    for f in files(repo):
        uses += scan_file(f, rel_name(f, repo), patterns, utility)
    for f in built_css_files(built or []):
        uses += scan_file(f, rel_name(f, repo), patterns, False)
    return uses


def scan_file(f: Path, rel: str, patterns: list[dict], utility: bool) -> list[dict]:
    uses = []
    text = f.read_text(encoding='utf-8', errors='replace')
    css_parts, js_parts = [], []
    if f.suffix in ('.css', '.scss'):
        css_parts.append((text, 0))
    elif f.suffix in MARKUP:
        for m in re.finditer(r'<style[^>]*>(.*?)</style>', text, re.S | re.I):
            css_parts.append((m.group(1), text[:m.start(1)].count('\n')))
        for m in re.finditer(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', text, re.S | re.I):
            js_parts.append((m.group(1), text[:m.start(1)].count('\n')))
    else:
        js_parts.append((text, 0))
    for p in patterns:
        fb = bool(p.get('emits_fallback')) or any(re.search(x, text) for x in p.get('fallback') or [])
        k = p['kind']
        if k.startswith('css'):
            for css, offset in css_parts:
                for sel, block, ln, supports, ancestors in css_rules(css):
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
                        uses.append({'feature': p['feature'], 'file': rel, 'line': ln + offset, 'snippet': hit[:160],
                                     'selector': sel if not sel.startswith('@') else None,
                                     'fallback': in_supports or fb})
        elif k == 'js-api':
            for js, offset in js_parts:
                clean = strip_js_comments(js)
                for m in re.finditer(p['match'], clean):
                    ln = clean[:m.start()].count('\n') + 1 + offset
                    uses.append({'feature': p['feature'], 'file': rel, 'line': ln,
                                 'snippet': js.splitlines()[ln - offset - 1].strip()[:160], 'selector': None, 'fallback': fb})
        elif k == 'html' and f.suffix in MARKUP:
            for m in re.finditer(p['match'], text):
                ln = text[:m.start()].count('\n') + 1
                uses.append({'feature': p['feature'], 'file': rel, 'line': ln, 'snippet': text.splitlines()[ln - 1].strip()[:160],
                             'selector': None, 'fallback': fb})
        elif k == 'utility-class' and utility and f.suffix in CLASS_SOURCES:
            # a class token, optionally behind variants ("md:hover:backdrop-blur-xl"), in any string of the file
            clean = strip_js_comments(text) if f.suffix not in MARKUP else text
            rx = r'(?<![\w/-])(?:[\w\[\]@&*>=.()-]+:)*!?(?:' + p['match'] + r')'
            for m in re.finditer(rx, clean):
                ln = clean[:m.start()].count('\n') + 1
                uses.append({'feature': p['feature'], 'file': rel, 'line': ln, 'snippet': text.splitlines()[ln - 1].strip()[:160],
                             'selector': None, 'fallback': fb})
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
    auto_built = a.repo / '.next' / 'static' / 'css'
    if not built and auto_built.is_dir():
        built = [auto_built]
    built_files = [rel_name(f, a.repo) for f in built_css_files(built)]
    fw = utility_framework(a.repo)
    uses = scan(a.repo, pats, utility=bool(fw), built=built)
    limitations = []
    if fw:
        fw.update(classes='known non-baseline utilities only (utility-class patterns in data/feature-patterns.json)',
                  built_css=built_files)
        if not built_files:
            limitations.append(f'Utility-class CSS ({fw["framework"]} {fw["version"]}) was scanned only for the known non-baseline '
                               'utilities; the CSS it generates (colour functions, @property, cascade layers, nesting) was not '
                               'scanned: build the site and pass --built-css')
    elif built_files:
        fw = {'framework': None, 'version': None, 'classes': 'not scanned', 'built_css': built_files}
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
                         'files': [{k: u[k] for k in ('file', 'line', 'snippet', 'selector', 'fallback')} for u in us],
                         'unsupported_in': unsupported, 'unsupported_usage': round(sum(x['usage'] for x in unsupported), 3),
                         'not_assessed': sorted(not_assessed), 'fallback': all(u['fallback'] for u in us),
                         'status': 'possible', 'linked_differences': []})
    out = {'repo': a.repo.as_posix(), 'browserslist': {'query': query, 'source': source, 'resolved_count': len(targets['resolved']),
                                                       'coverage': targets['coverage'], 'unresolved': targets['unresolved']},
           'uses': len(uses), 'features': findings}
    if fw:   # only written when there is something to say, so a plain CSS/JS repo's output is unchanged
        out['utility_css'] = fw
    if limitations:
        out['limitations'] = limitations
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    print(f'{len(uses)} use(s), {len(findings)} feature finding(s) for "{query}" -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
