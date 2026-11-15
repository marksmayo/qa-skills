#!/usr/bin/env python3
"""Read the user-facing text of JSX files (React, Next.js) for the hard-coded text, concatenation, format and RTL
rules of scan_code.py. Stdlib, heuristic: a small JSX-aware scanner, not a full parser.

    python jsx_text.py <file.tsx>          # prints what it found as JSON
    python jsx_text.py --self-test

What it reads:
  runs        the text of each element's children, in any script, with digits and punctuation (& · : %), and with
              each {expression} beside it kept as {…}: "Progress: {…}%". Text in script, style, code and pre is left
              out. A run that interleaves literal text with two or more expressions is also a `concat` (a sentence
              assembled from fragments). Literal text right after an expression that starts with % is a hand-made
              percentage (`percent`).
  attributes  alt, aria-label, aria-description, title, placeholder and label given as a string or a {string} /
              {`template`} (a template's ${...} becomes {…})
  classes     className / class strings (also string literals inside {cn(...)}): Tailwind physical utilities that
              won't mirror in RTL (ml-, mr-, pl-, pr-, left-, right-, border-l, rounded-l, text-left, text-right,
              float-left ...), with variants (sm:, focus:) and negatives (-ml-2)
  arrows      → ← ⇒ ⇐ ➔ › ‹ (or &rarr; &larr; &rsaquo; &lsaquo;) drawn as text
  metadata    Next.js `export const metadata = {...}` and `generateMetadata()`: title, description, default, absolute,
              template, siteName and alt strings (what search results and link previews show)

A `<` opens JSX only where an expression can start (after ( = , : ? [ { & | ! > ; return ...), so comparisons and
TypeScript generics (Array<T>, <T,>() =>) are not read as markup. Lines are 1-based.
"""
from __future__ import annotations
import html, json, re, sys
from pathlib import Path

LETTERS = re.compile(r'[^\W\d_]{2}')          # two letters in any script
TEXT_ATTRS = {'alt', 'aria-label', 'aria-description', 'aria-placeholder', 'aria-roledescription', 'title', 'placeholder', 'label'}
SKIP_TAGS = {'script', 'style', 'code', 'pre', 'kbd', 'samp'}
EXPR_START = set('(=,:?[{&|!>;}+-*%~^') | {''}
EXPR_WORDS = {'return', 'yield', 'default', 'case', 'await', 'else', 'do', 'in', 'of', 'typeof', 'void', 'new'}
NAME = re.compile(r'[A-Za-z_$][\w.:$-]*')
ATTR = re.compile(r'[A-Za-z_$][\w:$.-]*')
ARROWS = re.compile(r'[←→⇐⇒➔‹›]|&(?:larr|rarr|lsaquo|rsaquo);')
# physical Tailwind utilities (after variants such as sm: or focus:, and an optional - for negative values)
PHYSICAL = re.compile(r'^-?(?:m[lr]|p[lr]|left|right|border-[lr]|rounded-[lr]|rounded-[tb][lr]|scroll-m[lr]|scroll-p[lr]|'
                      r'text-(?:left|right)|float-(?:left|right)|clear-(?:left|right)|origin-(?:left|right)|'
                      r'origin-(?:top|bottom)-(?:left|right)|space-x-reverse)(?:-[\w./\[\]%#()-]+)?$')
META_KEYS = re.compile(r'\b(title|description|default|absolute|template|siteName|alt)\s*:\s*(?=[\'"`])')


class Scanner:
    def __init__(self, text: str):
        self.t, self.n = text, len(text)
        self.runs: list[dict] = []
        self.attrs: list[dict] = []
        self.classes: list[dict] = []
        self.percents: list[int] = []
        self._nl = [i for i, c in enumerate(text) if c == '\n']

    def line(self, i: int) -> int:
        lo, hi = 0, len(self._nl)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._nl[mid] < i:
                lo = mid + 1
            else:
                hi = mid
        return lo + 1

    # ---------------------------------------------------------------- JavaScript
    def string_end(self, i: int) -> int:
        """Index past the string or template literal at i (templates' ${...} scanned as JS)."""
        t, q = self.t, self.t[i]
        i += 1
        while i < self.n:
            c = t[i]
            if c == '\\':
                i += 2
                continue
            if c == q:
                return i + 1
            if q == '`' and c == '$' and t.startswith('{', i + 1):
                i = self.js(i + 2, '}') + 1
                continue
            if c == '\n' and q != '`':
                return i
            i += 1
        return self.n

    def regex_end(self, i: int) -> int:
        t, i, cls = self.t, i + 1, False
        while i < self.n and t[i] != '\n':
            c = t[i]
            if c == '\\':
                i += 2
                continue
            if c == '[':
                cls = True
            elif c == ']':
                cls = False
            elif c == '/' and not cls:
                return i + 1
            i += 1
        return i

    def js(self, i: int, stop: str | None = None) -> int:
        """Scan JavaScript from i; return the index of the unmatched closing `stop` (or len). JSX found on the way is read."""
        t, depth, prev, word = self.t, 0, '', ''
        while i < self.n:
            c = t[i]
            if c in ' \t\r\n':
                i += 1
                continue
            if t.startswith('//', i):
                j = t.find('\n', i)
                i = self.n if j < 0 else j
                continue
            if t.startswith('/*', i):
                j = t.find('*/', i + 2)
                i = self.n if j < 0 else j + 2
                continue
            if c in '\'"`':
                i = self.string_end(i)
                prev, word = 'str', ''
                continue
            if c == '/' and (prev in EXPR_START or word in EXPR_WORDS) and prev != 'str':
                i = self.regex_end(i)
                prev, word = 'str', ''
                continue
            if c == '<' and (prev in EXPR_START or word in EXPR_WORDS) and prev != 'str' and \
                    (t[i + 1:i + 2] == '>' or re.match(r'[A-Za-z_$]', t[i + 1:i + 2])):
                end = self.element(i)
                if end is not None:
                    i, prev, word = end, ')', ''
                    continue
            if c.isalnum() or c in '_$':
                m = re.compile(r'[\w$]+').match(t, i)
                word, prev = m.group(0), 'id'
                i = m.end()
                continue
            if c in '([{':
                depth += 1
            elif c in ')]}':
                if depth == 0 and stop and c == stop:
                    return i
                depth -= 1
            prev, word = c, ''
            i += 1
        return self.n

    # ---------------------------------------------------------------- JSX
    def element(self, i: int, skip: bool = False) -> int | None:
        """Read the JSX element at t[i] == '<'; return the index past it, or None when it isn't JSX."""
        t = self.t
        j = i + 1
        if t[j:j + 1] == '>':
            name, j = '', j + 1
            return self.children(j, name, skip)
        m = NAME.match(t, j)
        if not m:
            return None
        name, j = m.group(0), m.end()
        skip = skip or name.lower() in SKIP_TAGS
        found_attrs, found_classes = [], []
        while j < self.n:
            while j < self.n and t[j] in ' \t\r\n':
                j += 1
            if t.startswith('/>', j):
                self.attrs += found_attrs
                self.classes += found_classes
                return j + 2
            if t[j:j + 1] == '>':
                self.attrs += found_attrs
                self.classes += found_classes
                return self.children(j + 1, name, skip)
            if t[j:j + 1] == '{':                      # {...spread}
                j = self.js(j + 1, '}') + 1
                continue
            if t.startswith('/*', j):
                k = t.find('*/', j + 2)
                j = self.n if k < 0 else k + 2
                continue
            am = ATTR.match(t, j)
            if not am:
                return None
            aname, j = am.group(0), am.end()
            if aname == 'extends':                     # <T extends X>(): a TypeScript generic, not markup
                return None
            k = j
            while k < self.n and t[k] in ' \t\r\n':
                k += 1
            if t[k:k + 1] != '=':
                continue
            k += 1
            while k < self.n and t[k] in ' \t\r\n':
                k += 1
            if t[k:k + 1] in ('"', "'"):
                end = t.find(t[k], k + 1)
                if end < 0:
                    return None
                val, vstart, j = t[k + 1:end], k + 1, end + 1
                vals = [(val, vstart)]
            elif t[k:k + 1] == '{':
                end = self.js(k + 1, '}')
                expr = t[k + 1:end]
                j = end + 1
                vals = self.literals(expr, k + 1)
                if aname in TEXT_ATTRS:
                    whole = expr.strip()
                    vals = vals if (len(vals) == 1 and whole[:1] in '\'"`' and whole[-1:] == whole[:1]) else []
            elif t[k:k + 1] == '<':
                end = self.element(k, skip)
                if end is None:
                    return None
                j, vals = end, []
            else:
                return None
            if aname in TEXT_ATTRS and not skip:
                for v, pos in vals:
                    v = ' '.join(html.unescape(v).split())
                    if LETTERS.search(re.sub(r'\{…\}', '', v)):
                        found_attrs.append({'line': self.line(pos), 'text': v, 'attribute': aname, 'element': name})
            if aname in ('className', 'class'):
                for v, pos in vals:
                    bad = [tok for tok in v.split() if PHYSICAL.match(tok.rsplit(':', 1)[-1].lstrip('!'))]
                    if bad:
                        found_classes.append({'line': self.line(pos), 'classes': bad, 'element': name})
        return None

    def literals(self, expr: str, offset: int) -> list[tuple[str, int]]:
        """The string and template literals in an expression, with ${...} as {…}, and their positions."""
        out = []
        for m in re.finditer(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'|`((?:[^`\\]|\\.)*)`', expr):
            s = m.group(1) if m.group(1) is not None else m.group(2) if m.group(2) is not None else m.group(3)
            out.append((re.sub(r'\$\{[^{}]*\}', '{…}', s), offset + m.start() + 1))
        return out

    def children(self, i: int, name: str, skip: bool) -> int | None:
        t = self.t
        parts: list[tuple[str, object, int]] = []    # ('text', str, pos) | ('expr', None, pos)

        def flush():
            if not skip and parts:
                self.run(parts)
            parts.clear()
        while i < self.n:
            c = t[i]
            if c == '<':
                if t.startswith('</', i):
                    end = t.find('>', i)
                    if end < 0:
                        return None
                    flush()
                    return end + 1
                if t[i + 1:i + 2] == '>' or re.match(r'[A-Za-z_$]', t[i + 1:i + 2]):
                    flush()
                    end = self.element(i, skip)
                    if end is None:
                        return None
                    i = end
                    continue
                parts.append(('text', c, i))
                i += 1
                continue
            if c == '{':
                end = self.js(i + 1, '}')
                inner = t[i + 1:end]
                bare = re.sub(r'/\*.*?\*/', '', inner, flags=re.S).strip()
                if bare:
                    if re.search(r'<[A-Za-z>/]', inner):    # an expression holding markup ends the run
                        flush()
                    elif bare[:1] in '\'"`' and bare[-1:] == bare[:1] and len(bare) >= 2:
                        parts.append(('text', bare[1:-1], i + 1))    # {' '} or {"literal"} is text
                    else:
                        parts.append(('expr', None, i))
                i = end + 1
                continue
            j = i
            while j < self.n and t[j] not in '<{':
                j += 1
            parts.append(('text', t[i:j], i))
            i = j
        return None

    def run(self, parts: list) -> None:
        texts = [(s, p) for k, s, p in parts if k == 'text']
        literal = html.unescape(''.join(s for s, _ in texts))
        if not LETTERS.search(literal):
            return
        shown = ' '.join(html.unescape(''.join('{…}' if k == 'expr' else s for k, s, _ in parts)).split())
        first = next((p + (len(s) - len(s.lstrip())) for s, p in texts if LETTERS.search(html.unescape(s))), texts[0][1])
        exprs = sum(1 for k, _, _ in parts if k == 'expr')
        worded = sum(1 for s, _ in texts if LETTERS.search(html.unescape(s)))
        rec = {'line': self.line(first), 'text': shown, 'expressions': exprs, 'concat': exprs >= 2 and worded >= 2}
        arrows = [s for s, _ in texts if ARROWS.search(s)]
        if arrows:
            rec['arrows'] = ''.join(ARROWS.findall(' '.join(arrows)))
            rec['arrow_line'] = self.line(next(p + ARROWS.search(s).start() for s, p in texts if ARROWS.search(s)))
        self.runs.append(rec)
        for idx, (k, s, p) in enumerate(parts):
            if k == 'expr' and idx + 1 < len(parts) and parts[idx + 1][0] == 'text' and parts[idx + 1][1].startswith('%'):
                self.percents.append(self.line(parts[idx + 1][2]))


def read(text: str) -> dict:
    s = Scanner(text)
    s.js(0)
    return {'runs': s.runs, 'attributes': s.attrs, 'classes': s.classes, 'percents': sorted(set(s.percents)),
            'metadata': metadata(text)}


def metadata(text: str) -> list[dict]:
    """Strings in a Next.js metadata export or generateMetadata()."""
    out = []
    s = Scanner(text)
    for m in re.finditer(r'export\s+const\s+metadata\b[^=]*=\s*\{|export\s+(?:async\s+)?function\s+generateMetadata\b[^{]*\{', text):
        end = s.js(m.end(), '}')
        body = text[m.end():end]
        for k in META_KEYS.finditer(body):
            start = m.end() + k.end()
            stop = s.string_end(start)
            raw = text[start + 1:stop - 1]
            val = ' '.join(re.sub(r'\$\{[^{}]*\}', '{…}', raw).split())
            if LETTERS.search(re.sub(r'%s|\{…\}', '', val)):
                out.append({'line': s.line(start), 'text': val, 'key': k.group(1)})
    return out


def self_test() -> int:
    src = """const a = n < 3 && m > 1;
const f = <T,>(x: T) => x;
export const metadata = { title: `Shop ${x}`, description: 'Bikes for hire.' };
function C() {
  return <div className="ml-2 md:text-right" title="Hint text">Don't stop · {a} items &amp; more{' '}<b>{x}</b>%
    <code>npm run go</code>{/* note */}<p>Next →</p></div>;
}
"""
    r = read(src)
    texts = [x['text'] for x in r['runs']]
    fails = []
    if "Don't stop · {…} items & more" not in texts:
        fails.append(f'runs: {texts}')
    if any('npm' in x for x in texts):
        fails.append('code text read')
    if not r['classes'] or r['classes'][0]['classes'] != ['ml-2', 'md:text-right']:
        fails.append(f'classes: {r["classes"]}')
    if [a['text'] for a in r['attributes']] != ['Hint text']:
        fails.append(f'attributes: {r["attributes"]}')
    if [m['text'] for m in r['metadata']] != ['Shop {…}', 'Bikes for hire.']:
        fails.append(f'metadata: {r["metadata"]}')
    if not any(x.get('arrows') == '→' for x in r['runs']):
        fails.append('arrow not found')
    print('jsx_text self-test ' + ('passed' if not fails else 'FAILED: ' + '; '.join(fails)))
    return 1 if fails else 0


if __name__ == '__main__':
    if sys.argv[1:] == ['--self-test']:
        sys.exit(self_test())
    sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(read(Path(sys.argv[1]).read_text(encoding='utf-8')), indent=1, ensure_ascii=False))
