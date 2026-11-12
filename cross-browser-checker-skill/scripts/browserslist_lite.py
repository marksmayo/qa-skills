#!/usr/bin/env python3
"""Resolve common browserslist queries to concrete browser versions with caniuse data (stdlib).

    python browserslist_lite.py "defaults, firefox >= 115, safari >= 14"
    python browserslist_lite.py --repo examples/travel-site       # reads package.json or .browserslistrc

Supported: `defaults` (> 0.5%, last 2 versions, Firefox ESR, not dead), `> N%` / `>= N%`, `last N versions`,
`last N <browser> versions`, `<browser> >= V` (also >, <=, <), `<browser> V`, `not <query>`, `dead`/`not dead`,
and comma-separated unions. `Firefox ESR`, `and` combinations, regional usage (`> 5% in AU`), `cover`,
`since`, `extends` and Node queries are not resolved: they are listed as unresolved, and features can't be
assessed for them.

With --repo, the query comes from package.json's `browserslist`, else `.browserslistrc`; a Next.js project
(`next` in package.json) with neither uses Next's documented default support (16 and later: Chrome, Edge and
Firefox 111, Safari 16.4; 13-15: Chrome 64, Edge 79, Firefox 67, Opera 51, Safari 12), with iOS Safari taken
to follow Safari. A Vite project (`vite` in package.json, not Next.js) with neither compiles for its
`build.target`: read from vite.config.* when it is a literal string or array (esbuild names such as
chrome107 or safari16.4, or 'modules' / 'baseline-widely-available'), else the Vite major's documented
default (7 and later: 'baseline-widely-available' = Chrome and Edge 107, Firefox 104, Safari 16; 3-6:
'modules' = Chrome 87, Edge 88, Firefox 78, Safari 14; 2: Safari 13.1), iOS Safari again following
Safari; esXXXX and esnext name no browser and are reported as not assessed. Anything else falls back to
`defaults`. The source says which.

`dead` is an approximation of browserslist's list: IE, IE Mobile, BlackBerry, Baidu, Opera Mobile up
to 12.1 and Samsung Internet 4.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

ALIASES = {'firefox': 'firefox', 'ff': 'firefox', 'fx': 'firefox', 'chrome': 'chrome', 'edge': 'edge', 'safari': 'safari',
           'ios_saf': 'ios_saf', 'ios': 'ios_saf', 'ios_safari': 'ios_saf', 'opera': 'opera', 'op_mini': 'op_mini',
           'operamini': 'op_mini', 'android': 'android', 'samsung': 'samsung', 'and_chr': 'and_chr', 'chromeandroid': 'and_chr',
           'and_ff': 'and_ff', 'firefoxandroid': 'and_ff', 'ie': 'ie', 'explorer': 'ie', 'and_uc': 'and_uc', 'ucandroid': 'and_uc',
           'op_mob': 'op_mob', 'operamobile': 'op_mob', 'and_qq': 'and_qq', 'baidu': 'baidu', 'kaios': 'kaios', 'bb': 'bb',
           'blackberry': 'bb', 'ie_mob': 'ie_mob'}
DEAD = [('ie', None), ('ie_mob', None), ('bb', None), ('baidu', None), ('op_mob', '12.1'), ('samsung', '4')]
ENGINE = {'chrome': 'blink', 'edge': 'blink', 'opera': 'blink', 'and_chr': 'blink', 'samsung': 'blink', 'android': 'blink',
          'op_mob': 'blink', 'and_uc': 'blink', 'and_qq': 'blink', 'baidu': 'blink', 'kaios': 'gecko', 'firefox': 'gecko',
          'and_ff': 'gecko', 'safari': 'webkit', 'ios_saf': 'webkit', 'op_mini': 'presto', 'ie': 'trident', 'ie_mob': 'trident',
          'bb': 'webkit'}


def vnum(v: str) -> tuple:
    first = re.split(r'[-–]', str(v))[0]
    return tuple(int(x) if x.isdigit() else 0 for x in first.split('.'))


class Resolver:
    """caniuse-db agents: `versions` lines up with the top-level `eras` (e0 is the current release; later ones are
    unreleased), and `usage_global` maps a version to its share of global usage."""
    def __init__(self, caniuse: dict):
        self.agents = caniuse['agents']
        self.current = list(caniuse['eras']).index('e0')

    def released(self, b: str) -> list[dict]:
        a = self.agents[b]
        return [{'version': v, 'global_usage': a['usage_global'].get(v) or 0}
                for v in a['versions'][:self.current + 1] if v and v != 'TP']

    def usage(self, b: str, v: str) -> float:
        return self.agents[b]['usage_global'].get(v) or 0

    def all(self) -> set[tuple[str, str]]:
        return {(b, v['version']) for b in self.agents for v in self.released(b)}

    def term(self, t: str) -> tuple[set, str | None]:
        t = t.strip()
        tl = t.lower()
        if tl == 'defaults':
            out, notes = set(), []
            for sub in ('> 0.5%', 'last 2 versions', 'Firefox ESR'):
                s, n = self.term(sub)
                out |= s
                if n:
                    notes.append(n)
            dead, _ = self.term('dead')
            return out - dead, '; '.join(notes) or None
        if tl == 'dead':
            out = set()
            for b, upto in DEAD:
                if b in self.agents:
                    out |= {(b, v['version']) for v in self.released(b) if upto is None or vnum(v['version']) <= vnum(upto)}
            return out, None
        m = re.fullmatch(r'(>=?)\s*([\d.]+)%', t)
        if m:
            n = float(m.group(2))
            ok = (lambda u: u >= n) if m.group(1) == '>=' else (lambda u: u > n)
            return {(b, v['version']) for b in self.agents for v in self.released(b) if ok(v['global_usage'])}, None
        m = re.fullmatch(r'last\s+(\d+)\s+versions', tl)
        if m:
            n = int(m.group(1))
            return {(b, v['version']) for b in self.agents for v in self.released(b)[-n:]}, None
        m = re.fullmatch(r'last\s+(\d+)\s+(\w+)\s+versions?', tl)
        if m and ALIASES.get(m.group(2)) in self.agents:
            b = ALIASES[m.group(2)]
            return {(b, v['version']) for v in self.released(b)[-int(m.group(1)):]}, None
        m = re.fullmatch(r'(\w+)\s*(>=|<=|>|<)\s*([\d.]+)', tl)
        if m and ALIASES.get(m.group(1)) in self.agents:
            b, op, ver = ALIASES[m.group(1)], m.group(2), vnum(m.group(3))
            cmp = {'>=': lambda a: a >= ver, '<=': lambda a: a <= ver, '>': lambda a: a > ver, '<': lambda a: a < ver}[op]
            return {(b, v['version']) for v in self.released(b) if cmp(vnum(v['version']))}, None
        m = re.fullmatch(r'(\w+)\s+([\d.]+)', tl)
        if m and ALIASES.get(m.group(1)) in self.agents:
            b = ALIASES[m.group(1)]
            return {(b, v['version']) for v in self.released(b) if vnum(v['version'])[:len(vnum(m.group(2)))] == vnum(m.group(2))}, None
        return set(), f'unresolved: {t!r}'

    def resolve(self, query: str) -> dict:
        include, unresolved = set(), []
        for part in [p for p in re.split(r',|\n|\s+or\s+', query) if p.strip()]:
            part = part.strip()
            if re.search(r'\band\b', part, re.I):
                unresolved.append(f'unresolved: {part!r} (and-combinations are not supported)')
                continue
            neg = part.lower().startswith('not ')
            s, note = self.term(part[4:] if neg else part)
            if note:
                unresolved.append(note)
            include = include - s if neg else include | s
        rows = []
        for b, v in sorted(include, key=lambda x: (x[0], vnum(x[1]))):
            usage = self.usage(b, v)
            rows.append({'browser': b, 'version': v, 'engine': ENGINE.get(b, 'unknown'), 'usage': round(usage, 4)})
        return {'query': query, 'resolved': rows, 'unresolved': unresolved, 'coverage': round(sum(r['usage'] for r in rows), 2)}


# Next.js compiles for its own browser list when a project has no browserslist config. The documented defaults:
# Next.js 16 "Chrome 111+, Edge 111+, Firefox 111+, Safari 16.4+"; Next.js 13-15 "Chrome 64+, Edge 79+, Firefox 67+,
# Opera 51+, Safari 12+". iOS Safari is taken to follow Safari's version (the docs name Safari only).
NEXT_DEFAULTS = [(16, 'chrome >= 111, edge >= 111, firefox >= 111, safari >= 16.4, ios_saf >= 16.4'),
                 (0, 'chrome >= 64, edge >= 79, firefox >= 67, opera >= 51, safari >= 12, ios_saf >= 12')]


def next_version(repo: Path, pkg: dict) -> tuple[int | None, str]:
    """Next.js major version: the installed package's, else the dependency range's first number."""
    inst = repo / 'node_modules' / 'next' / 'package.json'
    if inst.is_file():
        try:
            v = json.loads(inst.read_text(encoding='utf-8')).get('version') or ''
            m = re.match(r'(\d+)', v)
            if m:
                return int(m.group(1)), f'installed next {v}'
        except (ValueError, OSError):
            pass
    rng = (pkg.get('dependencies') or {}).get('next') or (pkg.get('devDependencies') or {}).get('next') or ''
    m = re.search(r'(\d+)', rng)
    return (int(m.group(1)), f'next "{rng}" in package.json') if m else (None, f'next "{rng}" in package.json')


# Vite compiles for build.target, not browserslist. Its documented defaults: Vite 7 'baseline-widely-available'
# (Chrome 107, Edge 107, Firefox 104, Safari 16); Vite 3-6 'modules' (es2020, Chrome 87, Edge 88, Firefox 78,
# Safari 14); Vite 2 'modules' (es2019, Chrome 87, Edge 88, Firefox 78, Safari 13.1). iOS Safari is taken to
# follow Safari.
VITE_DEFAULTS = [(7, "'baseline-widely-available'", ['chrome107', 'edge107', 'firefox104', 'safari16']),
                 (3, "'modules'", ['es2020', 'edge88', 'firefox78', 'chrome87', 'safari14']),
                 (0, "'modules'", ['es2019', 'edge88', 'firefox78', 'chrome87', 'safari13.1'])]
VITE_CONFIGS = ('vite.config.ts', 'vite.config.mts', 'vite.config.js', 'vite.config.mjs', 'vite.config.cts', 'vite.config.cjs')
ESBUILD_BROWSER = {'chrome': 'chrome', 'edge': 'edge', 'firefox': 'firefox', 'safari': 'safari', 'ios': 'ios_saf',
                   'opera': 'opera'}


def installed_major(repo: Path, pkg: dict, name: str) -> tuple[int | None, str]:
    """A dependency's major version: the installed package's, else the range's first number."""
    inst = repo / 'node_modules' / name / 'package.json'
    if inst.is_file():
        try:
            v = json.loads(inst.read_text(encoding='utf-8')).get('version') or ''
            m = re.match(r'(\d+)', v)
            if m:
                return int(m.group(1)), f'installed {name} {v}'
        except (ValueError, OSError):
            pass
    rng = (pkg.get('dependencies') or {}).get(name) or (pkg.get('devDependencies') or {}).get(name) or ''
    m = re.search(r'(\d+)', rng)
    return (int(m.group(1)), f'{name} "{rng}" in package.json') if m else (None, f'{name} "{rng}" in package.json')


def _block(text: str, i: int) -> str:
    """The text of the {...} starting at text[i]."""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == '{':
            depth += 1
        elif text[j] == '}':
            depth -= 1
            if depth == 0:
                return text[i + 1:j]
    return text[i + 1:]


def vite_build_target(repo: Path) -> tuple[list[str] | None, str | None]:
    """build.target from vite.config.* when it is written literally (a string or an array of strings), else None.
    Only the build block is read: a dev-server proxy also has a 'target'."""
    for name in VITE_CONFIGS:
        f = repo / name
        if not f.is_file():
            continue
        text = re.sub(r'//[^\n]*|/\*.*?\*/', '', f.read_text(encoding='utf-8', errors='replace'), flags=re.S)
        for m in re.finditer(r'\bbuild\s*:\s*\{', text):
            body = _block(text, m.end() - 1)
            # the target key at the top level of the build block (nested objects such as rollupOptions blanked)
            depth, top = 0, []
            for ch in body:
                if ch == '{':
                    depth += 1
                top.append(ch if depth == 0 else ' ')
                if ch == '}':
                    depth -= 1
            t = re.search(r'''\btarget\s*:\s*(\[[^\]]*\]|["'][^"']+["'])''', ''.join(top))
            if t:
                vals = re.findall(r'''["']([^"']+)["']''', t.group(1))
                return vals, name
        return None, name
    return None, None


def target_query(vals: list[str]) -> tuple[str | None, list[str]]:
    """esbuild targets (chrome107, safari16.4, es2020, 'baseline-widely-available', 'modules', 'esnext') as a
    browserslist query, with the values that name no browser."""
    parts, other = [], []
    for v in vals:
        v = v.strip().lower()
        if v in ('baseline-widely-available', 'modules'):
            vals2 = next(t for _, k, t in VITE_DEFAULTS if k.strip("'") == v)
            q, o = target_query(vals2)
            parts += [q] if q else []
            other += o
            continue
        m = re.fullmatch(r'([a-z]+)(\d+(?:\.\d+)*)', v)
        if m and m.group(1) in ESBUILD_BROWSER:
            parts.append(f'{ESBUILD_BROWSER[m.group(1)]} >= {m.group(2)}')
            if m.group(1) == 'safari' and not any(x.startswith('ios') for x in vals):
                parts.append(f'ios_saf >= {m.group(2)}')
        else:
            other.append(v)
    return (', '.join(parts) or None), other


def vite_query(repo: Path, pkg: dict) -> tuple[str, str]:
    """A Vite project without a browserslist config: its build.target when literal, else the Vite major's default."""
    vals, cfg = vite_build_target(repo)
    if vals:
        q, other = target_query(vals)
        extra = f'; {", ".join(other)} name no browser and are not assessed' if other else ''
        if q:
            return q, f'Vite build.target {vals} in {cfg} (no browserslist config; iOS Safari taken to follow Safari{extra})'
        return 'defaults', (f'Vite build.target {vals} in {cfg} names no browser version (e.g. esnext); browserslist '
                            'defaults used instead')
    major, how = installed_major(repo, pkg, 'vite')
    _, name, targets = next(d for d in VITE_DEFAULTS if major is None or major >= d[0])
    q, _ = target_query(targets)
    assumed = '' if major is not None else '; version unknown, the newest default assumed'
    return q, (f'Vite default build.target {name} = {", ".join(targets)} (no browserslist config and no literal '
               f'build.target; {how}{assumed}; iOS Safari taken to follow Safari)')


def query_from_repo(repo: Path) -> tuple[str, str]:
    pkg_file = repo / 'package.json'
    pkg = {}
    if pkg_file.is_file():
        pkg = json.loads(pkg_file.read_text(encoding='utf-8'))
        bl = pkg.get('browserslist')
        if isinstance(bl, list):
            return ', '.join(bl), 'package.json'
        if isinstance(bl, str):
            return bl, 'package.json'
        if isinstance(bl, dict) and 'production' in bl:
            return ', '.join(bl['production']), 'package.json (production)'
    rc = repo / '.browserslistrc'
    if rc.is_file():
        lines = [l.split('#')[0].strip() for l in rc.read_text(encoding='utf-8').splitlines()]
        return ', '.join(l for l in lines if l and not l.startswith('[')), '.browserslistrc'
    deps = {**(pkg.get('devDependencies') or {}), **(pkg.get('dependencies') or {})}
    if 'vite' in deps and 'next' not in deps:
        return vite_query(repo, pkg)
    if 'next' in deps:
        major, how = next_version(repo, pkg)
        query = next(q for floor, q in NEXT_DEFAULTS if major is None or major >= floor)
        assumed = '' if major is not None else '; version unknown, the newest defaults assumed'
        return query, (f'Next.js default browser support (no browserslist config; {how}{assumed}; iOS Safari taken to '
                       'follow Safari)')
    return 'defaults', 'browserslist default (no config found)'


def load_caniuse() -> dict:
    from fetch_data import asset_path
    return json.loads(asset_path('caniuse-db').read_text(encoding='utf-8'))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('query', nargs='?')
    ap.add_argument('--repo', type=Path)
    a = ap.parse_args()
    q, src = (a.query, 'argument') if a.query else query_from_repo(a.repo or Path('.'))
    res = Resolver(load_caniuse()).resolve(q)
    res['source'] = src
    print(json.dumps(res, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
