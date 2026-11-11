#!/usr/bin/env python3
"""Scan a repository's CSS, JavaScript and HTML for features the browserslist targets don't all support (stdlib).

    python feature_scan.py --repo examples/travel-site --out run/features.json [--query "defaults"]

Patterns are in data/feature-patterns.json; support comes from web-features (the first version of each
core browser that supports the feature), and targets from browserslist_lite.py (the repo's
package.json or .browserslistrc, else `defaults`). CSS is read with a small tokeniser (comments
removed, rules split into selectors and declarations, @supports blocks tracked); scripts and markup
are matched with the patterns' regexes, ignoring comments in scripts.

A target is unsupported when web-features gives no version for its browser, or a later one. Browsers
web-features doesn't cover (Opera, Samsung Internet, UC, QQ, KaiOS, Opera Mini, Android WebView) are
`not_assessed` for that feature. A fallback is an @supports block around the use, or one of the
pattern's fallback regexes in the same file. Every finding's status is `possible`: it becomes
`confirmed_at_runtime` only when compare_engines.py links it to a difference an engine produced.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
import browserslist_lite as bl  # noqa: E402

WF_BROWSER = {'chrome': 'chrome', 'and_chr': 'chrome_android', 'edge': 'edge', 'firefox': 'firefox', 'and_ff': 'firefox_android',
              'safari': 'safari', 'ios_saf': 'safari_ios'}
SKIP_DIRS = {'node_modules', '.git', 'dist', 'build', 'vendor', '__pycache__'}


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
    for p in sorted(repo.rglob('*')):
        if p.is_file() and not (set(p.relative_to(repo).parts) & SKIP_DIRS):
            if p.suffix in ('.css', '.scss', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.html', '.htm', '.vue', '.svelte'):
                yield p


def strip_js_comments(t: str) -> str:
    return re.sub(r'//[^\n]*|/\*.*?\*/', lambda m: ' ' * len(m.group(0).split('\n')[0]) + '\n' * m.group(0).count('\n'), t, flags=re.S)


def scan(repo: Path, patterns: list[dict]) -> list[dict]:
    uses = []
    for f in files(repo):
        rel = f.relative_to(repo).as_posix()
        text = f.read_text(encoding='utf-8', errors='replace')
        css_parts, js_parts = [], []
        if f.suffix in ('.css', '.scss'):
            css_parts.append((text, 0))
        elif f.suffix in ('.html', '.htm', '.vue', '.svelte'):
            for m in re.finditer(r'<style[^>]*>(.*?)</style>', text, re.S | re.I):
                css_parts.append((m.group(1), text[:m.start(1)].count('\n')))
            for m in re.finditer(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', text, re.S | re.I):
                js_parts.append((m.group(1), text[:m.start(1)].count('\n')))
        else:
            js_parts.append((text, 0))
        for p in patterns:
            fb = any(re.search(x, text) for x in p.get('fallback') or [])
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
            elif k == 'html' and f.suffix in ('.html', '.htm', '.vue', '.svelte'):
                for m in re.finditer(p['match'], text):
                    ln = text[:m.start()].count('\n') + 1
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
    a = ap.parse_args()
    from fetch_data import asset_path
    caniuse = json.loads(asset_path('caniuse-db').read_text(encoding='utf-8'))
    wf = json.loads(asset_path('web-features').read_text(encoding='utf-8'))['features']
    pats = json.loads((SKILL / 'data' / 'feature-patterns.json').read_text(encoding='utf-8'))['patterns']
    query, source = (a.query, 'argument') if a.query else bl.query_from_repo(a.repo)
    targets = bl.Resolver(caniuse).resolve(query)
    uses = scan(a.repo, pats)
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
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    print(f'{len(uses)} use(s), {len(findings)} feature finding(s) for "{query}" -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
