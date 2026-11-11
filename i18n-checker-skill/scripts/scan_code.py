#!/usr/bin/env python3
"""Scan code for text that bypasses translation and code that breaks it (heuristic, stdlib).

    python scan_code.py --repo examples/bookclub --out run/code.json [--allow "Bookclub,ACME"]

Patterns are in data/hardcoded-patterns.json:
  hardcoded_string  visible text in HTML without a translation attribute (data-i18n, i18n, v-t ...), JSX/Vue
                    text nodes, Android layout text attributes, SwiftUI Text(verbatim:), and DOM text set to a literal
  concatenation     a translated fragment joined to other text with + (translators never see the sentence)
  format            numbers, currency or dates formatted by hand instead of with the locale
  fixed_width       CSS that fixes a width and hides overflow (no text-overflow: ellipsis), maxLines without ellipsize
  rtl_physical      physical direction properties (margin-left, text-align: left) that won't mirror in RTL
Every finding is `possible`: the rendered UI confirms or dismisses it (render_locales.py).
"""
from __future__ import annotations
import argparse, json, re, sys
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
SKIP = {'node_modules', '.git', 'dist', 'build', 'vendor', '__pycache__'}


class TextFinder(HTMLParser):
    """Visible text nodes whose nearest element (or ancestor) has no translation attribute."""
    VOID = {'br', 'img', 'input', 'meta', 'link', 'hr', 'source', 'wbr', 'area', 'base', 'col', 'embed', 'param', 'track'}

    def __init__(self, attrs: list[str]):
        super().__init__(convert_charrefs=True)
        self.attrs, self.stack, self.found = attrs, [], []

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        a = dict(attrs)
        self.stack.append((tag, any(x in a for x in self.attrs), a.get('id'), a.get('class')))

    def handle_endtag(self, tag):
        while self.stack:
            t = self.stack.pop()[0]
            if t == tag:
                break

    def handle_data(self, data):
        text = ' '.join(data.split())
        if not text or not re.search(r'[A-Za-z]{2}', text):
            return
        if any(t[0] in ('script', 'style', 'title', 'head', 'code', 'pre') for t in self.stack):
            return
        if any(t[1] for t in self.stack):
            return
        tag, _, ident, cls = self.stack[-1] if self.stack else ('', False, None, None)
        self.found.append({'text': text, 'line': self.getpos()[0], 'element': tag + (f'#{ident}' if ident else f'.{cls.split()[0]}' if cls else ''),
                           'has_id': bool(ident)})


def files(repo: Path, exts):
    for p in sorted(repo.rglob('*')):
        if p.is_file() and p.suffix in exts and not (set(p.relative_to(repo).parts) & SKIP):
            rel = p.relative_to(repo).as_posix()
            if p.suffix == '.xml' and re.search(r'(^|/)values[^/]*/', rel):
                continue
            yield p, rel


def css_fixed_width(text: str):
    text = re.sub(r'/\*.*?\*/', lambda m: '\n' * m.group(0).count('\n'), text, flags=re.S)
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', text):
        body = m.group(2)
        if re.search(r'(?<![-\w])width\s*:\s*\d+(\.\d+)?(px|em|rem|ch)\b', body) and \
                (re.search(r'white-space\s*:\s*nowrap', body) or re.search(r'overflow(-x)?\s*:\s*(hidden|clip)', body)) and \
                not re.search(r'text-overflow\s*:\s*ellipsis', body):
            yield m.group(1).strip(), text[:m.start(2)].count('\n') + 1, ' '.join(body.split())[:120]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--allow', default='')
    a = ap.parse_args()
    pats = json.loads((SKILL / 'data' / 'hardcoded-patterns.json').read_text(encoding='utf-8'))
    allow = set(pats['allow_list']) | {x.strip() for x in a.allow.split(',') if x.strip()}
    out = []

    def add(kind, rel, line, detail, **kw):
        out.append({'kind': kind, 'file': rel, 'line': line, 'detail': detail, 'status': 'possible', **kw})

    for spec in pats['hardcoded']:
        for p, rel in files(a.repo, spec['ext']):
            text = p.read_text(encoding='utf-8', errors='replace')
            if spec['kind'] == 'html-text':
                tf = TextFinder(spec['translated_attrs'])
                tf.feed(text)
                for f in tf.found:
                    if f['text'] not in allow:
                        add('hardcoded_string', rel, f['line'], f'{f["element"]} shows {f["text"]!r} with no translation attribute',
                            text=f['text'], element=f['element'],
                            note='set by a script at runtime?' if f['has_id'] else None, framework=spec['framework'])
            else:
                for m in re.finditer(spec['match'], text):
                    if m.group(1).strip() not in allow:
                        add('hardcoded_string', rel, text[:m.start()].count('\n') + 1, f'literal text {m.group(1).strip()!r}',
                            text=m.group(1).strip(), framework=spec['framework'])
    for key, kind in (('concatenation', 'concatenation'), ('format', 'format'), ('rtl_physical', 'rtl_physical')):
        for spec in pats[key]:
            for p, rel in files(a.repo, spec['ext']):
                lines = p.read_text(encoding='utf-8', errors='replace').splitlines()
                for n, ln in enumerate(lines, 1):
                    code = ln.split('//')[0] if p.suffix not in ('.css', '.scss') else ln
                    for m in re.finditer(spec['match'], code):
                        add(kind, rel, n, spec.get('what') or f'{kind}: {code.strip()[:100]}', snippet=ln.strip()[:160], match=m.group(0))
                        break
    for spec in pats['fixed_width']:
        for p, rel in files(a.repo, spec['ext']):
            text = p.read_text(encoding='utf-8', errors='replace')
            if spec['kind'] == 'css-fixed-width':
                for sel, line, body in css_fixed_width(text):
                    add('fixed_width', rel, line, f'{sel} has a fixed width and hides overflow without an ellipsis', selector=sel, snippet=body)
            else:
                for m in re.finditer(spec['match'], text):
                    add('fixed_width', rel, text[:m.start()].count('\n') + 1, spec['what'], snippet=m.group(0))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({'findings': out, 'allow_list': sorted(allow)}, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    from collections import Counter
    print(f'{len(out)} code finding(s) {dict(Counter(f["kind"] for f in out))} -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
