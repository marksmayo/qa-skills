#!/usr/bin/env python3
"""List the scoped Rust lines that are not compiled here: code under a `#[cfg(...)]` that is false on
this platform, or that needs a feature the crate's defaults do not enable (stdlib only).

    python rust_gates.py --repo <repo> --scope scope.json [--platform linux|macos|windows] [--out gates.json]

A mutation engine can only mutate what the compiler builds. A `#[cfg(unix)]` block is not built on
Windows, a `#[cfg(feature = "x")]` function is not built by a plain `cargo build`: cargo-mutants
makes no mutant there (or every mutant is unviable), and the tests that would catch one are gated
the same way. Such lines are neither survivors nor kills; the report lists them as not assessed
and says where they can be mutated (another platform, or `--features x`).

Output: {platform, gated: [{file, lines: [first, last] of the gated item, scoped: the part of the
scope inside it, cfg, gate, platforms?, features?, detail}]}. gate is `platform` (compiled only on
`platforms`), `feature` (needs `features`, not enabled by default), `cfg` (false in every build this
reader knows, such as `not(test)`: never compiled into the test binary) or `undecided` (a predicate
this reader cannot decide: target_arch, a custom --cfg). Code under `#[cfg(test)]` is test code and
is not listed. The cfg expressions and the crates' default features are read with cargo_project.py.
Exit 0; 3 when the scope has no Rust source file.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cargo_project  # noqa: E402

CFG_OPEN = re.compile(r'#(!?)\[\s*cfg\s*\(')
ATTR = re.compile(r'\s*#!?\[')
ITEM_WORD = re.compile(r'\s*(?:pub\b|fn\b|impl\b|mod\b|struct\b|enum\b|trait\b|const\b|static\b|use\b|type\b|let\b|'
                       r'unsafe\b|async\b|extern\b|macro_rules\b|union\b)')


def line_of(text: str, idx: int) -> int:
    return text.count('\n', 0, idx) + 1


def item_end(masked: str, pos: int) -> int:
    """Index just past the item, statement, field or arm that starts at pos (after its attributes), in masked text:
    its block's closing brace, or the `;` (or, for a field or match arm, the `,`) that ends it first."""
    while True:
        m = ATTR.match(masked, pos)
        if not m:
            break
        pos = cargo_project._balanced(masked, m.end() - 1, '[', ']')
    commas = not ITEM_WORD.match(masked, pos)
    depth = 0
    for k in range(pos, len(masked)):
        c = masked[k]
        if c in '([':
            depth += 1
        elif c in ')]':
            if depth == 0:
                return k            # the end of the enclosing argument list
            depth -= 1
        elif depth == 0:
            if c == '{':
                end = cargo_project._balanced(masked, k, '{', '}')
                rest = re.match(r'\s*(else\b|,|;)', masked[end:end + 40])   # `if x { } else { }`, `Arm => { },`
                if rest and rest.group(1) == 'else':
                    nxt = masked.find('{', end)
                    return cargo_project._balanced(masked, nxt, '{', '}') if nxt >= 0 else end
                return end
            if c == ';' or (c == ',' and commas):
                return k + 1
            if c == '}':
                return k
    return len(masked)


def gated_items(text: str) -> list[dict]:
    """[{start, end, cfg}]: the line span of every item, statement, field or arm under a #[cfg(...)] attribute
    (the attribute's own line included); `#![cfg(...)]` gates the whole file."""
    masked = cargo_project.mask(text)
    out = []
    for m in CFG_OPEN.finditer(masked):
        close = cargo_project._balanced(masked, m.end() - 1, '(', ')')
        expr = ' '.join(text[m.end():close - 1].split())
        tail = re.match(r'\s*\]', masked[close:close + 20])
        if not tail:
            continue
        after = close + tail.end()
        if m.group(1):
            out.append({'start': 1, 'end': text.count('\n') + 1, 'cfg': expr})
            continue
        end = item_end(masked, after)
        out.append({'start': line_of(text, m.start()), 'end': line_of(text, max(m.start(), end - 1)), 'cfg': expr})
    return out


def decide(expr: str, platform: str, enabled: set, every: set) -> dict | None:
    """None when the code is compiled on `platform` with the default features; else {gate, detail, ...}."""
    tree = cargo_project._cfg_parse(expr)
    env = cargo_project.PLATFORMS[platform]
    here = cargo_project._cfg_eval(tree, env, enabled)
    if here is True:
        return None
    if here is None:
        return {'gate': 'undecided', 'detail': f'cfg({expr}) cannot be decided by this reader (a target or custom cfg); '
                                               'check whether this build compiles it'}
    per = {p: cargo_project._cfg_eval(tree, e, enabled) for p, e in cargo_project.PLATFORMS.items()}
    where = sorted(p for p, v in per.items() if v is True)
    if where:
        return {'gate': 'platform', 'platforms': where,
                'detail': f'cfg({expr}) is false on {platform}: compiled only on {", ".join(where)}'}
    full = {p: cargo_project._cfg_eval(tree, e, every | enabled) for p, e in cargo_project.PLATFORMS.items()}
    need, alts = cargo_project.cfg_features([expr])
    need = sorted(set(need) - enabled)
    if any(v is True for v in full.values()) or need or alts:
        flat = need or sorted({x for g in alts for x in g})
        out = {'gate': 'feature', 'features': flat,
               'detail': f'cfg({expr}) needs ' + (f'--features {",".join(need)}' if need else 'one of ' + ', '.join(flat))
                         + ', which the default features do not enable'}
        if full.get(platform) is not True:
            out['platforms'] = sorted(p for p, v in full.items() if v is True)
            if out['platforms']:
                out['detail'] += f', and is compiled only on {", ".join(out["platforms"])}'
        return out
    return {'gate': 'cfg', 'detail': f'cfg({expr}) is false in every test build this reader knows (default features, '
                                     'Linux, macOS and Windows)'}


def crate_features(model: dict | None, rel: str) -> tuple[set, set]:
    """(enabled by default, every feature) of the crate that holds rel."""
    best = None
    for c in (model or {}).get('crates') or []:
        d = (c.get('dir') or '.').strip('/')
        if d in ('', '.') or rel == d or rel.startswith(d + '/'):
            if best is None or len(d) > len((best.get('dir') or '.').strip('/')):
                best = c
    feats = (best or {}).get('features') or {}
    every = set(feats.get('all') or [])
    return every - set(feats.get('optional') or []), every


def scope_gates(repo, scope: dict, platform: str | None = None) -> list[dict]:
    """The gated items that overlap the scope's Rust source ranges and are not compiled on `platform` (sys.platform
    values or linux/macos/windows; this machine's when omitted) with the default features."""
    repo = Path(repo)
    plat = cargo_project.SYS_PLATFORM.get(platform or sys.platform, platform or sys.platform)
    if plat not in cargo_project.PLATFORMS:
        plat = 'linux'
    model = None
    out = []
    for f in scope.get('files') or []:
        if f.get('kind') != 'source' or f.get('status') == 'deleted' or not f['path'].endswith('.rs') or not f.get('ranges'):
            continue
        try:
            text = (repo / f['path']).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if 'cfg' not in text:
            continue
        if model is None:
            try:
                model = cargo_project.read_cargo(repo)
            except Exception:  # noqa: BLE001  (an unreadable manifest: features are then unknown, gates still listed)
                model = {}
        enabled, every = crate_features(model, f['path'])
        for g in gated_items(text):
            scoped = [[max(a, g['start']), min(b, g['end'])] for a, b in f['ranges'] if a <= g['end'] and g['start'] <= b]
            if not scoped:
                continue
            why = decide(g['cfg'], plat, enabled, every)
            if why is None:
                continue
            out.append({'file': f['path'], 'lines': [g['start'], g['end']], 'scoped': scoped, 'cfg': g['cfg'], **why})
    return out


def warnings(gates: list[dict], platform: str) -> list[str]:
    out = []
    for g in gates:
        where = f'{g["file"]}:{g["lines"][0]}-{g["lines"][1]}'
        fix = {'platform': 'mutate it on ' + ' or '.join(g.get('platforms') or ['a platform that compiles it']),
               'feature': 'mutate it with --features ' + ','.join(g.get('features') or []) +
                          ' (run_engine.py cargo-mutants --cargo-mutants-arg=--features=...)',
               'cfg': 'no test build compiles it', 'undecided': 'check the build before trusting any result there'}[g['gate']]
        out.append(f'{where} is not compiled here ({platform}, default features): {g["detail"]}. No mutant can be made '
                   f'or tested there, and tests gated the same way do not run; list these lines as not assessed in the '
                   f'limitations ({fix}).')
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--scope', type=Path, required=True)
    ap.add_argument('--platform', choices=sorted(cargo_project.PLATFORMS), help='default: this machine')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    scope = json.loads(args.scope.read_text(encoding='utf-8'))
    plat = args.platform or cargo_project.SYS_PLATFORM.get(sys.platform, 'linux')
    if not any(f['path'].endswith('.rs') and f.get('kind') == 'source' for f in scope.get('files') or []):
        print('no Rust source file in scope', file=sys.stderr)
        return 3
    gates = scope_gates(args.repo.resolve(), scope, plat)
    text = json.dumps({'platform': plat, 'gated': gates}, indent=2) + '\n'
    if args.out:
        args.out.write_text(text, encoding='utf-8')
    else:
        sys.stdout.write(text)
    for w in warnings(gates, plat):
        print(f'WARNING: {w}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
