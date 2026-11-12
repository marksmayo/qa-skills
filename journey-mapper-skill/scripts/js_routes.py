#!/usr/bin/env python3
"""Client-side routes from React Router, Vue Router and Angular route configs (static; nothing is run).

    from js_routes import scan_js_routes
    routes, notes = scan_js_routes({'src/router.tsx': text, ...}, frameworks={'src/router.tsx': 'react-router'})

What it reads:
  - route objects: `{ path: 'x', element|Component|lazy|children|component|loadChildren|redirect... }` and
    `{ index: true, ... }`, nested through `children: [...]`, as passed to createBrowserRouter, useRoutes,
    Vue Router or Angular's RouterModule;
  - JSX: `<Route path="x" element={...}>` and `<Route index ...>`, nested `<Route>` elements and `<Routes>` blocks;
  - path values that are string literals, template literals, `A + B`, or constants from plain object
    literals anywhere in the project (`export const ROUTES = { LOGIN: '/login' } as const`, then
    `path: ROUTES.LOGIN` or `` path: `${ROUTES.SHOP}/*` ``);
  - mounts: a `<Routes>` block in a component is relative to the route that renders it. When a splat
    route (`path: 'shop/*'`) renders a component (`element: <ShopModule/>`, `lazy(() => import(...))`,
    `lazy: () => import(...)`) defined in another file, that file's relative routes are joined to the
    splat route's path.

Each route records the resolved full path when it could be resolved (`resolved: true`), otherwise the
path as written relative to an unknown parent, or the unresolved expression in braces.
"""
from __future__ import annotations
import bisect, re
from pathlib import PurePosixPath

ROUTE_KEYS = {'element', 'Component', 'component', 'lazy', 'children', 'loader', 'action', 'errorElement',
              'ErrorBoundary', 'loadChildren', 'loadComponent', 'redirect', 'redirectTo', 'name', 'meta', 'handle',
              'components', 'canActivate', 'pathMatch', 'beforeEnter', 'hydrateFallbackElement'}
ROOT_HINT = re.compile(r'\b(createBrowserRouter|createHashRouter|createMemoryRouter|createStaticRouter|BrowserRouter|'
                       r'HashRouter|MemoryRouter|RouterProvider|createRouter|RouterModule\.forRoot|provideRouter)\b')
COMP_DEF = re.compile(r'(?:^|\n)\s*(?:export\s+)?(?:default\s+)?(?:function\s+([A-Z]\w*)|(?:const|let)\s+([A-Z]\w*)\s*(?::[^=\n]+)?=|'
                      r'class\s+([A-Z]\w*))')
LAZY_DEF = re.compile(r'(?:const|let)\s+([A-Z]\w*)\s*=\s*(?:React\.)?lazy\(\s*(?:async\s*)?\(\s*\)\s*=>\s*import\(\s*["\']([^"\']+)["\']')
IMPORT_DEF = re.compile(r'import\s+(?:(\w+)\s*,?\s*)?(?:\{([^}]*)\})?\s*from\s*["\']([^"\']+)["\']')
CONST_OBJ = re.compile(r'(?:^|\n)\s*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*')
JS_EXTS = ('.tsx', '.ts', '.jsx', '.js', '.mjs', '.vue')


def code_mask(text: str) -> str:
    """The text with comments blanked (newlines kept); strings are kept."""
    out, i, n = list(text), 0, len(text)
    while i < n:
        c = text[i]
        if c in '"\'`':
            j = skip_string(text, i)
            i = j
            continue
        if text.startswith('//', i):
            j = text.find('\n', i)
            j = n if j == -1 else j
            for k in range(i, j): out[k] = ' '
            i = j
            continue
        if text.startswith('/*', i):
            j = text.find('*/', i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                if out[k] != '\n': out[k] = ' '
            i = j
            continue
        i += 1
    return ''.join(out)


def skip_string(text: str, i: int) -> int:
    q, n, j = text[i], len(text), i + 1
    depth = 0
    while j < n:
        c = text[j]
        if c == '\\':
            j += 2; continue
        if q == '`' and text.startswith('${', j):
            depth += 1; j += 2; continue
        if q == '`' and depth and c == '}':
            depth -= 1; j += 1; continue
        if c == q and not depth:
            return j + 1
        if c == '\n' and q != '`':
            return j
        j += 1
    return n


def brackets(text: str) -> dict[int, int]:
    """Open position -> close position for every (), [] and {} outside strings."""
    pairs, stack, i, n = {}, [], 0, len(text)
    while i < n:
        c = text[i]
        if c in '"\'`':
            i = skip_string(text, i); continue
        if c in '([{':
            stack.append(i)
        elif c in ')]}' and stack:
            pairs[stack.pop()] = i
        i += 1
    return pairs


def split_top(text: str, s: int, e: int, pairs: dict[int, int]) -> list[tuple[int, int]]:
    """Top-level comma-separated spans of text[s:e]."""
    spans, start, i = [], s, s
    while i < e:
        c = text[i]
        if c in '"\'`':
            i = skip_string(text, i); continue
        if c in '([{' and i in pairs:
            i = pairs[i] + 1; continue
        if c == ',':
            spans.append((start, i)); start = i + 1
        i += 1
    spans.append((start, e))
    return [(a, b) for a, b in spans if text[a:b].strip()]


ENTRY = re.compile(r'\s*(?:["\']([^"\']+)["\']|([A-Za-z_$][\w$]*))\s*:')


def object_entries(text: str, open_pos: int, pairs: dict[int, int]) -> dict[str, tuple[int, int]]:
    """key -> (value start, value end) for the top-level entries of the object literal at open_pos.
    A shorthand entry (`{ path, element }`) or method maps to an empty span."""
    close = pairs.get(open_pos)
    if close is None:
        return {}
    out = {}
    for a, b in split_top(text, open_pos + 1, close, pairs):
        m = ENTRY.match(text, a)
        if m and m.end() <= b:
            out[m.group(1) or m.group(2)] = (m.end(), b)
        else:
            w = re.match(r'\s*(?:async\s+)?([A-Za-z_$][\w$]*)', text[a:b])
            if w:
                out.setdefault(w.group(1), (b, b))
    return out


def string_value(expr: str):
    expr = expr.strip()
    if len(expr) >= 2 and expr[0] == expr[-1] and expr[0] in '"\'':
        return expr[1:-1]
    return None


class Constants:
    """Plain constant objects and strings across the project, for resolving route paths."""
    def __init__(self):
        self.defs: dict[str, list[tuple[str, object]]] = {}

    def collect(self, rel: str, text: str):
        masked = code_mask(text)
        pairs = brackets(masked)
        for m in CONST_OBJ.finditer(masked):
            pos = m.end()
            if pos < len(masked) and masked[pos] == '{' and pos in pairs:
                self.defs.setdefault(m.group(1), []).append((rel, self._obj(masked, pos, pairs, 0)))
            else:
                end = masked.find('\n', pos)
                expr = masked[pos:end if end != -1 else len(masked)].strip().rstrip(';').strip()
                expr = re.sub(r'\s+as\s+const$', '', expr)
                if expr and (expr[0] in '"\'`'):
                    self.defs.setdefault(m.group(1), []).append((rel, expr))

    def _obj(self, text, pos, pairs, depth):
        out = {}
        for key, (a, b) in object_entries(text, pos, pairs).items():
            val = text[a:b].strip()
            val = re.sub(r'\s+as\s+const$', '', val)
            if val.startswith('{') and depth < 3 and (a + text[a:b].find('{')) in pairs:
                out[key] = self._obj(text, a + text[a:b].find('{'), pairs, depth + 1)
            else:
                out[key] = val
        return out

    def lookup(self, dotted: str, rel: str):
        parts = dotted.split('.')
        defs = self.defs.get(parts[0]) or []
        if not defs:
            return None
        same = [v for f, v in defs if f == rel]
        if same:
            val = same[0]
        elif len({repr(v) for _, v in defs}) == 1:
            val = defs[0][1]
        else:
            return None
        for p in parts[1:]:
            if not isinstance(val, dict) or p not in val:
                return None
            val = val[p]
        return val if isinstance(val, str) else None

    def eval(self, expr: str, rel: str, depth: int = 0):
        """String value of a path expression, or None."""
        if depth > 6 or expr is None:
            return None
        expr = re.sub(r'\s+as\s+const$', '', expr.strip())
        if not expr:
            return None
        s = string_value(expr)
        if s is not None:
            return s
        if expr[0] == '`' and expr[-1] == '`':
            body, out, i = expr[1:-1], [], 0
            while i < len(body):
                if body.startswith('${', i):
                    j, d = i + 2, 1
                    while j < len(body) and d:
                        d += {'{': 1, '}': -1}.get(body[j], 0); j += 1
                    v = self.eval(body[i + 2:j - 1], rel, depth + 1)
                    if v is None:
                        return None
                    out.append(v); i = j
                else:
                    out.append(body[i]); i += 1
            return ''.join(out)
        if '+' in expr:
            parts = [p for p in re.split(r'\s*\+\s*', expr)]
            vals = [self.eval(p, rel, depth + 1) for p in parts]
            return ''.join(vals) if all(v is not None for v in vals) else None
        if re.fullmatch(r'[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*', expr):
            raw = self.lookup(expr, rel)
            return self.eval(raw, rel, depth + 1) if raw is not None else None
        return None


def join(parent: str, child: str) -> str:
    if child.startswith('/'):
        return child
    p = parent.rstrip('/')
    if p.endswith('/*'):
        p = p[:-2]
    elif p.endswith('*'):
        p = p[:-1].rstrip('/')
    return (p + '/' + child) if child else (p or '/')


def resolve_module(spec: str, rel: str, files: set[str]) -> str | None:
    if not spec.startswith('.'):
        stem = PurePosixPath(spec).name
        cands = [f for f in files if PurePosixPath(f).stem == stem or
                 (PurePosixPath(f).stem == 'index' and PurePosixPath(f).parent.name == stem)]
        return cands[0] if len(cands) == 1 else None
    base = PurePosixPath(rel).parent
    parts = []
    for seg in (base / spec).parts:
        if seg == '..':
            if parts: parts.pop()
        elif seg != '.':
            parts.append(seg)
    target = '/'.join(parts)
    for cand in [target] + [target + e for e in JS_EXTS] + [target + '/index' + e for e in JS_EXTS]:
        if cand in files:
            return cand
    return None


def scan_js_routes(texts: dict[str, str], frameworks: dict[str, str], consts: Constants) -> list[dict]:
    """texts: every JS/TS file of the project (for constants and component lookup); frameworks: the files
    to read routes from -> 'react-router' | 'vue-router' | 'angular'."""
    files = set(texts)
    comp_files: dict[str, set[str]] = {}
    for rel, text in texts.items():
        stem = PurePosixPath(rel).stem
        name = PurePosixPath(rel).parent.name if stem == 'index' else stem
        if name[:1].isupper():
            comp_files.setdefault(name, set()).add(rel)
        for m in COMP_DEF.finditer(text):
            n = m.group(1) or m.group(2) or m.group(3)
            comp_files.setdefault(n, set()).add(rel)

    per_file: dict[str, list[dict]] = {}
    roots: set[str] = set()
    for rel, fw in frameworks.items():
        text = texts[rel]
        masked = code_mask(text)
        offsets = [i for i, c in enumerate(masked) if c == '\n']
        L = lambda pos: bisect.bisect_right(offsets, pos) + 1
        pairs = brackets(masked)
        local_comp: dict[str, str] = {}          # component name -> file it comes from, via imports in this file
        for m in LAZY_DEF.finditer(masked):
            tgt = resolve_module(m.group(2), rel, files)
            if tgt: local_comp[m.group(1)] = tgt
        for m in IMPORT_DEF.finditer(masked):
            tgt = resolve_module(m.group(3), rel, files)
            if not tgt: continue
            if m.group(1): local_comp.setdefault(m.group(1), tgt)
            for part in (m.group(2) or '').split(','):
                nm = part.split(' as ')[-1].strip()
                if nm: local_comp.setdefault(nm, tgt)

        def targets(expr: str) -> set[str]:
            out = set()
            for m in re.finditer(r'import\(\s*["\']([^"\']+)["\']', expr):
                t = resolve_module(m.group(1), rel, files)
                if t: out.add(t)
            for name in re.findall(r'<([A-Z]\w*)|\b(?:Component|component)\s*:\s*([A-Z]\w*)', expr):
                for n in name:
                    if not n: continue
                    if n in local_comp: out.add(local_comp[n])
                    else: out |= {f for f in comp_files.get(n, ()) if f != rel}
            return out

        if fw != 'react-router' or ROOT_HINT.search(masked):
            roots.add(rel)
        records = []
        # route objects
        objs = []
        for open_pos, close in pairs.items():
            if masked[open_pos] != '{':
                continue
            ent = object_entries(masked, open_pos, pairs)
            has_path = 'path' in ent and ent['path'][0] < ent['path'][1]
            is_index = 'index' in ent and masked[ent['index'][0]:ent['index'][1]].strip() == 'true'
            if (has_path or is_index) and (set(ent) & ROUTE_KEYS):
                objs.append((open_pos, close, ent, has_path, is_index))
        objs.sort()
        spans = []   # (children start, children end, record)
        for open_pos, close, ent, has_path, is_index in objs:
            raw = masked[ent['path'][0]:ent['path'][1]].strip() if has_path else ''
            val = consts.eval(raw, rel) if has_path else ''
            parent = None
            for cs, ce, rec in spans:
                if cs <= open_pos < ce and (parent is None or cs >= parent[0]):
                    parent = (cs, rec)
            exprs = ' '.join(masked[a:b] for k, (a, b) in ent.items() if k in ('element', 'Component', 'component', 'lazy',
                                                                             'loadChildren', 'loadComponent'))
            rec = {'raw': raw, 'value': val, 'index': is_index, 'parent': parent[1] if parent else None,
                   'line': L(ent['path'][0] if has_path else open_pos), 'targets': targets(exprs),
                   'lazy': 'lazy' in ent or 'loadChildren' in ent or 'loadComponent' in ent or 'import(' in exprs,
                   'redirect': bool(re.search(r'<Navigate\b|\bredirect\b', exprs)) or 'redirect' in ent or 'redirectTo' in ent,
                   'file': rel, 'framework': fw, 'detector': 'route-config'}
            records.append(rec)
            if 'children' in ent:
                a, b = ent['children']
                spans.append((a, b, rec))
        # JSX <Route> elements
        if fw == 'react-router':
            stack = []
            for m in re.finditer(r'<(/?)(Routes?)\b', masked):
                closing, tag = m.group(1), m.group(2)
                if closing:
                    while stack and stack[-1][0] != tag:
                        stack.pop()
                    if stack: stack.pop()
                    continue
                j, depth = m.end(), 0
                while j < len(masked):
                    c = masked[j]
                    if c in '"\'`': j = skip_string(masked, j); continue
                    if c == '{': depth += 1
                    elif c == '}': depth -= 1
                    elif c == '>' and depth == 0: break
                    j += 1
                tag_text = masked[m.end():j]
                self_closing = tag_text.rstrip().endswith('/')
                if tag == 'Routes':
                    if not self_closing: stack.append(('Routes', None))
                    continue
                pm = re.search(r'\bpath\s*=\s*(\{|["\'])', tag_text)
                raw = ''
                if pm:
                    s = pm.start(1)
                    if pm.group(1) == '{':
                        open_abs = m.end() + s
                        close_abs = pairs.get(open_abs, open_abs)
                        raw = masked[open_abs + 1:close_abs].strip()
                    else:
                        k = skip_string(tag_text, s)
                        raw = tag_text[s:k]
                is_index = bool(re.search(r'(?<![\w-])index(?:\s*=\s*\{\s*true\s*\})?(?=[\s/>]|$)', tag_text))
                parent = next((r for t, r in reversed(stack) if t == 'Route'), None)
                if pm or is_index:
                    val = consts.eval(raw, rel) if pm else ''
                    rec = {'raw': raw, 'value': val, 'index': is_index and not pm, 'parent': parent,
                           'line': L(m.start()), 'targets': targets(tag_text),
                           'lazy': 'lazy' in tag_text, 'redirect': '<Navigate' in tag_text,
                           'file': rel, 'framework': fw, 'detector': 'jsx-route'}
                    records.append(rec)
                else:
                    rec = parent      # a pathless layout route: children keep the parent's path
                if not self_closing:
                    stack.append(('Route', rec))
        per_file[rel] = records

    def local_path(rec) -> str | None:
        """Path within its file's tree (relative to the file's mount), or None if an expression is unresolved."""
        own = rec['value']
        if own is None:
            return None
        if rec['parent'] is None:
            return own
        pp = local_path(rec['parent'])
        return None if pp is None else join(pp, own) if own or rec['index'] else pp

    mounts_cache: dict[str, list[str] | None] = {}

    def mounts(rel: str, seen=()) -> list[str] | None:
        """Absolute prefixes the routes of a file are mounted under ([''] for router roots), None if unknown."""
        if rel in mounts_cache:
            return mounts_cache[rel]
        if rel in roots:
            mounts_cache[rel] = ['']
            return ['']
        found = []
        for other, recs in per_file.items():
            if other == rel or other in seen:
                continue
            for r in recs:
                if rel in r['targets']:
                    lp = local_path(r)
                    if lp is None:
                        continue
                    for p in mounts(other, seen + (rel,)) or []:
                        found.append(join(p or '/', lp) if not lp.startswith('/') else lp)
        mounts_cache[rel] = sorted(set(found)) or None
        return mounts_cache[rel]

    out = []
    for rel, recs in per_file.items():
        bases = mounts(rel)
        for r in recs:
            lp = local_path(r)
            note = []
            if lp is None:
                name, resolved, conf = '{' + r['raw'] + '}', False, 'low'
                note.append('path expression not resolved')
                names = [name]
            elif lp.startswith('/'):
                names, resolved, conf = [lp], True, 'high'
            elif bases:
                names = [join(b or '/', lp) for b in bases]
                resolved, conf = True, 'high'
                if bases != ['']:
                    note.append('joined to the route that renders this component')
            else:
                names, resolved, conf = [lp or '/'], False, 'medium'
                note.append('relative to a parent route that was not found')
            if r['index']: note.append('index route')
            if r['redirect']: note.append('redirects')
            if r['lazy']: note.append('lazy-loaded')
            for name in names:
                if resolved and not name.startswith('/'):
                    name = '/' + name
                out.append({'name': name, 'file': rel, 'line': r['line'], 'detector': r['detector'],
                            'confidence': conf, 'framework': r['framework'], 'resolved': resolved,
                            'index': r['index'], **({'note': '; '.join(note)} if note else {})})
    return out
