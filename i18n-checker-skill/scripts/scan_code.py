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

JSX (.jsx, .tsx, and .js/.mjs holding JSX) is read by jsx_text.py: element text in any script beside {expressions}
("Progress: {…}%"), alt/aria-label/title/placeholder/label attributes and Next.js metadata exports (hardcoded_string,
`attribute` / framework next-metadata), sentences interleaving text with two or more expressions (concatenation),
{n}% (format), className Tailwind physical utilities and arrow glyphs (rtl_physical). A locale literal in
toLocale*String / Intl.* is a format finding. The visually-hidden idiom (.sr-only: 1px box, clip / clip-path) is
never a fixed width. At most one finding of a kind per line (two rules can see the same line).

Text in any script counts (Cyrillic, Arabic, CJK ...), not only Latin. dist/, build/ and out/ are scanned unless the
repository builds them (see repo_walk.py); skipped directories are in `skipped_dirs` and on stderr, and --include DIR
scans one anyway. code.json records `files_scanned` (0 means the scan read nothing: report it, never as clean).
"""
from __future__ import annotations
import argparse, json, re, sys
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
import repo_walk  # noqa: E402
import jsx_text  # noqa: E402
LETTERS = re.compile(r'[^\W\d_]{2}')     # two letters in any script


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
        if not text or not LETTERS.search(text):
            return
        if any(t[0] in ('script', 'style', 'title', 'head', 'code', 'pre') for t in self.stack):
            return
        if any(t[1] for t in self.stack):
            return
        tag, _, ident, cls = self.stack[-1] if self.stack else ('', False, None, None)
        self.found.append({'text': text, 'line': self.getpos()[0], 'element': tag + (f'#{ident}' if ident else f'.{cls.split()[0]}' if cls else ''),
                           'has_id': bool(ident)})


WALK: dict = {}


def files(repo: Path, exts):
    for p in WALK['files']:
        if p.suffix in exts:
            rel = p.relative_to(repo).as_posix()
            WALK['scanned'].add(rel)
            if p.suffix == '.xml' and re.search(r'(^|/)values[^/]*/', rel):
                continue
            yield p, rel


JSX_HINT = re.compile(r'''from\s+['"](?:react|preact|solid-js)|</[A-Za-z][\w.]*>|/>''')
CLASS_ATTR = re.compile(r'''\bclass(?:Name)?\s*=\s*(?:"([^"]*)"|'([^']*)')''')


def html_classes(text: str, rel: str, add) -> None:
    """Physical Tailwind utilities in class="..." of HTML and Vue templates."""
    for m in CLASS_ATTR.finditer(text):
        v = m.group(1) if m.group(1) is not None else m.group(2)
        bad = [t for t in v.split() if jsx_text.PHYSICAL.match(t.rsplit(':', 1)[-1].lstrip('!'))]
        if bad:
            add('rtl_physical', rel, text.count('\n', 0, m.start()) + 1, f'physical Tailwind utilities {" ".join(bad)} will not '
                'mirror in RTL (use ms-/me-/ps-/pe-/start-/end-/text-start/text-end)', snippet=' '.join(bad), match=bad[0])


HIDDEN_SEL = re.compile(r'(?i)sr-only|visually-?hidden|screen-?reader|a11y-hidden|assistive|offscreen')


def visually_hidden(selector: str, body: str) -> bool:
    """The visually-hidden (sr-only) idiom: text kept for screen readers in a 1px box with clip / clip-path. Its
    overflow is hidden on purpose, so it is never a fixed width that cuts off translations."""
    if re.search(r'(?<![-\w])clip\s*:\s*rect\(\s*0', body) or re.search(r'clip-path\s*:\s*inset\(\s*50%', body):
        return True
    if re.search(r'(?<![-\w])width\s*:\s*1px', body) and re.search(r'(?<![-\w])height\s*:\s*1px', body):
        return True
    return bool(HIDDEN_SEL.search(selector) and re.search(r'(?<![-\w])(width|height)\s*:\s*[01](px)?\b', body))


def jsx_findings(text: str, rel: str, allow: set, add) -> None:
    """Hard-coded text, sentences assembled from fragments, hand-made percentages, physical Tailwind classes and
    arrows in a JSX file, and the strings of a Next.js metadata export."""
    r = jsx_text.read(text)
    for x in r['runs']:
        if x['text'] not in allow:
            add('hardcoded_string', rel, x['line'], f'JSX text {x["text"]!r}', text=x['text'], framework='jsx')
        if x['concat']:
            add('concatenation', rel, x['line'], f'a sentence assembled in JSX from text and {x["expressions"]} expressions '
                f'({x["text"][:80]!r}); translators never see the whole sentence: use one message with placeholders',
                snippet=x['text'][:160])
        if x.get('arrows'):
            add('rtl_physical', rel, x['arrow_line'], f'direction arrow {x["arrows"]} drawn as text; it will not mirror in RTL '
                '(use an icon flipped with :dir(rtl) or a logical glyph)', snippet=x['text'][:160], match=x['arrows'])
    for x in r['attributes']:
        if x['text'] not in allow:
            add('hardcoded_string', rel, x['line'], f'{x["element"]} {x["attribute"]}={x["text"]!r} is literal text',
                text=x['text'], attribute=x['attribute'], framework='jsx')
    for x in r['metadata']:
        if x['text'] not in allow:
            add('hardcoded_string', rel, x['line'], f'metadata {x["key"]} {x["text"]!r} (what search results and link previews show)',
                text=x['text'], framework='next-metadata')
    for x in r['classes']:
        add('rtl_physical', rel, x['line'], f'physical Tailwind utilities {" ".join(x["classes"])} on <{x["element"]}> will not '
            'mirror in RTL (use ms-/me-/ps-/pe-/start-/end-/text-start/text-end)', snippet=' '.join(x['classes']),
            match=x['classes'][0])
    for line in r['percents']:
        add('format', rel, line, 'a percentage formatted by hand ({value}%); use Intl.NumberFormat with style "percent" '
            '(the sign\'s position and spacing differ by locale)')


def css_fixed_width(text: str):
    text = re.sub(r'/\*.*?\*/', lambda m: '\n' * m.group(0).count('\n'), text, flags=re.S)
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', text):
        body = m.group(2)
        if visually_hidden(m.group(1), body):
            continue
        if re.search(r'(?<![-\w])width\s*:\s*\d+(\.\d+)?(px|em|rem|ch)\b', body) and \
                (re.search(r'white-space\s*:\s*nowrap', body) or re.search(r'overflow(-x)?\s*:\s*(hidden|clip)', body)) and \
                not re.search(r'text-overflow\s*:\s*ellipsis', body):
            yield m.group(1).strip(), text[:m.start(2)].count('\n') + 1, ' '.join(body.split())[:120]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--allow', default='')
    ap.add_argument('--include', action='append', default=[], metavar='DIR',
                    help='scan a directory that would be skipped as build output (repeatable)')
    a = ap.parse_args()
    WALK['files'], WALK['skipped'] = repo_walk.walk(a.repo, a.include)
    WALK['scanned'] = set()
    repo_walk.report_skipped(WALK['skipped'], 'scan_code.py')
    pats = json.loads((SKILL / 'data' / 'hardcoded-patterns.json').read_text(encoding='utf-8'))
    allow = set(pats['allow_list']) | {x.strip() for x in a.allow.split(',') if x.strip()}
    out = []

    seen_at: set = set()

    def add(kind, rel, line, detail, **kw):
        if kind != 'hardcoded_string':      # one finding of a kind per line (two rules can see the same line)
            if (kind, rel, line) in seen_at:
                return
            seen_at.add((kind, rel, line))
        out.append({'kind': kind, 'file': rel, 'line': line, 'detail': detail, 'status': 'possible', **kw})

    for spec in pats['hardcoded']:
        for p, rel in files(a.repo, spec['ext']):
            text = p.read_text(encoding='utf-8', errors='replace')
            if spec['kind'] == 'jsx':
                if p.suffix in ('.js', '.mjs') and (p.name.endswith('.min.js') or not JSX_HINT.search(text)):
                    continue
                jsx_findings(text, rel, allow, add)
                continue
            if spec['kind'] == 'next-metadata':
                if p.suffix in ('.jsx', '.tsx') or (p.suffix in ('.js', '.mjs') and JSX_HINT.search(text)):
                    continue        # read with the JSX rule
                for x in jsx_text.metadata(text):
                    if x['text'] not in allow:
                        add('hardcoded_string', rel, x['line'], f'metadata {x["key"]} {x["text"]!r} (what search results and '
                            'link previews show)', text=x['text'], framework='next-metadata')
                continue
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
                    # a // comment (not the // of a URL); CSS and HTML have no // comments
                    code = re.split(r'(?<!:)//', ln)[0] if p.suffix not in ('.css', '.scss', '.html', '.htm') else ln
                    if spec.get('unless') and re.search(spec['unless'], code):
                        continue
                    for m in re.finditer(spec['match'], code):
                        add(kind, rel, n, spec.get('what') or f'{kind}: {code.strip()[:100]}', snippet=ln.strip()[:160], match=m.group(0))
                        break
    for p, rel in files(a.repo, ('.html', '.htm', '.vue')):
        html_classes(p.read_text(encoding='utf-8', errors='replace'), rel, add)
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
    a.out.write_text(json.dumps({'findings': out, 'allow_list': sorted(allow), 'files_scanned': len(WALK['scanned']),
                                 'skipped_dirs': WALK['skipped']}, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    from collections import Counter
    print(f'{len(out)} code finding(s) {dict(Counter(f["kind"] for f in out))} in {len(WALK["scanned"])} file(s) -> {a.out}',
          file=sys.stderr)
    if not WALK['scanned']:
        print('scan_code.py: WARNING no source file was scanned (no HTML, JS/TS, CSS, Android or Swift file outside the '
              'skipped directories): hard-coded text and concatenation were not assessed', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
