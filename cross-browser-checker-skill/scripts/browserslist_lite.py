#!/usr/bin/env python3
"""Resolve common browserslist queries to concrete browser versions with caniuse data (stdlib).

    python browserslist_lite.py "defaults, firefox >= 115, safari >= 14"
    python browserslist_lite.py --repo examples/travel-site       # reads package.json or .browserslistrc

Supported: `defaults` (> 0.5%, last 2 versions, Firefox ESR, not dead), `> N%` / `>= N%`, `last N versions`,
`last N <browser> versions`, `<browser> >= V` (also >, <=, <), `<browser> V`, `not <query>`, `dead`/`not dead`,
and comma-separated unions. `Firefox ESR`, `and` combinations, regional usage (`> 5% in AU`), `cover`,
`since`, `extends` and Node queries are not resolved: they are listed as unresolved, and features can't be
assessed for them.

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


def query_from_repo(repo: Path) -> tuple[str, str]:
    pkg = repo / 'package.json'
    if pkg.is_file():
        bl = json.loads(pkg.read_text(encoding='utf-8')).get('browserslist')
        if isinstance(bl, list):
            return ', '.join(bl), 'package.json'
        if isinstance(bl, dict) and 'production' in bl:
            return ', '.join(bl['production']), 'package.json (production)'
    rc = repo / '.browserslistrc'
    if rc.is_file():
        lines = [l.split('#')[0].strip() for l in rc.read_text(encoding='utf-8').splitlines()]
        return ', '.join(l for l in lines if l and not l.startswith('[')), '.browserslistrc'
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
