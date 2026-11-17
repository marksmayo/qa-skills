#!/usr/bin/env python3
"""Rust tests for the dead test finder: a static scan only (stdlib only, nothing is compiled or run).

    python rust_tests.py <repo>          # print the definitions, candidates and what CI enables

The shared cargo_project.py lists every `#[test]` function of a Cargo project or workspace with its
`#[ignore]`, cfg and feature gates. Each is a definition (`<file>::<module path>::<name>`, framework
`cargo-test`, runner `cargo`, the libtest id in `cargo_id`). The rules read the test's body with comments and
strings masked:

    never_runs / ignored_never_run        #[ignore], and no CI file passes --ignored, --include-ignored or
                                          nextest's --run-ignored
    never_runs / feature_never_enabled    the test (or its whole target, through required-features) needs a
                                          feature the defaults leave off, and no CI file passes --all-features or
                                          --features naming it
    never_runs / cfg_never_true           a cfg that is false on Linux, macOS and Windows
    unreached_assertion / cfg_body        the whole body is one `#[cfg(...)] { ... }` block or one
                                          `if cfg!(...) { ... }`: where the condition is false the test is empty
    no_assertion / no_assertion           no assert!/assert_eq!/panic!-style macro, no #[should_panic], and no call
                                          of a same-file function or macro that holds one (two calls deep); the
                                          candidate says whether the body at least fails on an error (`?`,
                                          .unwrap(), .expect())

A test gated to one platform is not a candidate (the gate is in the definition's `gates`). Doc tests are counted,
not scanned. Rust tests are never listed by a runner here (`cargo test -- --list` builds every test binary) and
never neutralised: scan() returns the `static_only` entry that scan_tests.py puts in its output, and the report
must say so.
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True   # no __pycache__ in the skill folder when this script imports its siblings
import cargo_project  # noqa: E402

ASSERT = re.compile(r'\b(?:debug_|prop_|static_|k)?assert\w*\s*!|\b(?:panic|unreachable|unimplemented|todo|bail|ensure)\s*!'
                    r'|\bassert_\w+\s*\(|\.assert\s*\(\)|\.(?:success|failure)\s*\(\)|\binsta::|\bexpect!\s*\[')
FAILS_ON_ERROR = re.compile(r'\?\s*[;)\n,}]|\.unwrap\s*\(\)|\.expect\s*\(|\.unwrap_err\s*\(\)|\.expect_err\s*\(')
CALL = re.compile(r'\b([A-Za-z_]\w*)\s*(!?)\s*\(')
CHECKY = re.compile(r'(^|_)(check|expect|verify|ensure|assert|require|must|should)', re.I)
RUNS_IGNORED = re.compile(r'--(?:include-)?ignored\b|--run-ignored\b')
FEATURES = re.compile(r'(?:--features|-F)[=\s]+(?:"([^"]+)"|\'([^\']+)\'|([\w\-/,]+))')
KEYWORDS = {'if', 'while', 'for', 'match', 'loop', 'return', 'Some', 'Ok', 'Err', 'None', 'let', 'fn', 'move', 'in', 'as'}


def match_brace(code: str, i: int, open_c: str = '{', close_c: str = '}') -> int:
    """Index of the bracket closing the one at code[i] in masked code; len(code) - 1 when it never closes."""
    depth = 0
    for j in range(i, len(code)):
        c = code[j]
        if c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return j
    return len(code) - 1


def fn_body(code: str, name: str, start: int = 0) -> str | None:
    """The masked body (braces included) of the first `fn name` at or after offset start."""
    m = re.compile(r'\bfn\s+' + re.escape(name) + r'\b').search(code, start)
    if not m:
        return None
    semi, brace = code.find(';', m.end()), code.find('{', m.end())
    if brace < 0 or (0 <= semi < brace and '(' not in code[semi:brace]):
        return None
    return code[brace:match_brace(code, brace) + 1]


def macro_body(code: str, name: str) -> str | None:
    m = re.search(r'\bmacro_rules\s*!\s*' + re.escape(name) + r'\b', code)
    if not m:
        return None
    brace = code.find('{', m.end())
    return code[brace:match_brace(code, brace) + 1] if brace >= 0 else None


def helper_asserts(code: str, body: str, depth: int = 0, seen: set | None = None) -> bool:
    """body calls a function or macro defined in the same file that holds an assertion, two calls deep at most."""
    seen = set() if seen is None else seen
    for m in CALL.finditer(body):
        name, bang = m.group(1), m.group(2)
        if name in KEYWORDS or name in seen:
            continue
        seen.add(name)
        inner = macro_body(code, name) if bang else fn_body(code, name)
        if inner is None:
            continue
        if ASSERT.search(inner) or (depth < 1 and helper_asserts(code, inner, depth + 1, seen)):
            return True
    return False


def outside_checks(body: str, code: str) -> list[str]:
    """Names called in body that look like checks (check_x, expect_y, verify) but are defined in another file: they
    are assumed to assert, so the test is no candidate."""
    out = []
    for m in CALL.finditer(body):
        name = m.group(1)
        if name not in KEYWORDS and CHECKY.search(name) and name not in ('expect', 'expect_err') and \
                fn_body(code, name) is None and macro_body(code, name) is None:
            out.append(name)
    return out


def cfg_body(body: str) -> str | None:
    """The condition when the whole body is one `#[cfg(cond)] { ... }` block or one `if cfg!(cond) { ... }` with no
    else: where the condition is false, nothing in the test runs."""
    inner = body[1:-1].strip()
    m = re.match(r'#\s*\[\s*cfg\s*\(', inner) or re.match(r'if\s+cfg\s*!\s*\(', inner)
    if not m:
        return None
    close = match_brace(inner, m.end() - 1, '(', ')')
    cond = ' '.join(inner[m.end():close].split())
    rest = inner[close + 1:].lstrip()
    if inner.startswith('#'):
        if not rest.startswith(']'):
            return None
        rest = rest[1:].lstrip()
    if not rest.startswith('{') or match_brace(rest, 0) != len(rest) - 1:
        return None
    return cond or None


def ci_settings(root: Path, det: dict | None) -> dict:
    """What the CI files pass to cargo: {files, runs_ignored, all_features, features}. Read from every CI file's
    text, so a flag on a continuation line or in a matrix value counts (a candidate is raised only when no CI file
    names the flag at all)."""
    out = {'files': [], 'runs_ignored': False, 'all_features': False, 'features': []}
    feats: set[str] = set()
    for f in (det or {}).get('ci_files') or []:
        try:
            text = (root / f).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if 'cargo' not in text:
            continue
        out['files'].append(f)
        out['runs_ignored'] = out['runs_ignored'] or bool(RUNS_IGNORED.search(text))
        out['all_features'] = out['all_features'] or '--all-features' in text
        for m in FEATURES.finditer(text):
            for part in re.split(r'[,\s]+', next(g for g in m.groups() if g)):
                if part:
                    feats.add(part.rsplit('/', 1)[-1])
    out['features'] = sorted(feats)
    return out


def scan(root: Path, det: dict | None, adder, defs: list, skip=lambda rel: False) -> dict | None:
    """Append the Rust definitions to defs, raise candidates through adder(file)(category, rule, ids, line, why,
    details), and return the `static_only` entry ({language, tests, doc_tests, gates, ci, reason}); None when root
    holds no Cargo project or no #[test]. skip(rel) leaves a file out (vendored trees, fixture projects)."""
    model = cargo_project.read_cargo(root)
    if not model.get('is_cargo'):
        return None
    ci = ci_settings(root, det)
    where = ('no CI file passes it' if ci['files'] else 'no CI file runs cargo, so the default `cargo test` decides')
    codes: dict[str, str] = {}
    n, docs, gates = 0, 0, {}
    for crate in model.get('crates') or []:
        pkg = crate.get('dir') or '.'
        for t in crate.get('tests') or []:
            if skip(t['file']):
                continue
            if t.get('kind') == 'doc':
                docs += 1
                continue
            n += 1
            tid = f'{t["file"]}::{t["name"]}'
            why = t.get('why') or []
            d = {'id': tid, 'file': t['file'], 'line': t['line'], 'name': t['fn'], 'lang': 'rust', 'package': pkg,
                 'framework': 'cargo-test', 'runner': 'cargo', 'cargo_id': t.get('id'), 'calls': [],
                 'runs_by_default': t.get('runs_by_default')}
            if why:
                d['gates'] = [{k: g[k] for k in ('gate', 'detail', 'features', 'features_any', 'platforms') if k in g}
                              for g in why]
            defs.append(d)
            add = adder(t['file'])
            gated = False
            for g in why:
                gates[g['gate']] = gates.get(g['gate'], 0) + 1
                if g['gate'] == 'ignored' and not g.get('platforms') and not ci['runs_ignored']:
                    gated = True
                    add('never_runs', 'ignored_never_run', [tid], t['line'],
                        f'{t["fn"]} is #[ignore]d, which runs only with `-- --ignored` or `-- --include-ignored`, and '
                        f'{where}', {'gate': 'ignored', 'ci_files': ci['files']})
                elif g['gate'] in ('feature', 'required_features') and not ci['all_features']:
                    need = set(g.get('features') or [])
                    alts = g.get('features_any') or []
                    on = set(ci['features'])
                    if need <= on and all(set(a) & on for a in alts) and (need or alts):
                        continue
                    gated = True
                    add('never_runs', 'feature_never_enabled', [tid], t['line'],
                        f'{t["fn"]} {g["detail"]}; {where} (--all-features, or --features naming it)',
                        {'gate': g['gate'], 'features': sorted(need), 'features_any': alts, 'ci_files': ci['files'],
                         'ci_features': ci['features']})
                elif g['gate'] == 'cfg':
                    gated = True
                    add('never_runs', 'cfg_never_true', [tid], t['line'], f'{t["fn"]}: {g["detail"]}', {'gate': 'cfg'})
            if t['file'] not in codes:
                try:
                    codes[t['file']] = cargo_project.mask((root / t['file']).read_text(encoding='utf-8', errors='replace'))
                except OSError:
                    codes[t['file']] = ''
            code = codes[t['file']]
            start = sum(len(ln) + 1 for ln in code.split('\n')[:max(t['line'] - 1, 0)])
            body = fn_body(code, t['fn'], start) if t.get('fn') else None
            if body is None:
                continue
            cond = cfg_body(body)
            if cond and cond != 'test':
                add('unreached_assertion', 'cfg_body', [tid], t['line'],
                    f'the whole body of {t["fn"]} sits under cfg({cond}): where that is false the test is empty and passes',
                    {'cfg': cond})
                continue
            if gated or t.get('should_panic') or ASSERT.search(body) or helper_asserts(code, body) or \
                    outside_checks(body, code):
                continue
            soft = bool(FAILS_ON_ERROR.search(body)) or bool(re.search(r'->\s*(?:\w+::)*Result\b', code[start:start + 400]
                                                                         .split('{', 1)[0]))
            add('no_assertion', 'no_assertion', [tid], t['line'],
                f'{t["fn"]} has no assert!/assert_eq!/panic!-style macro and calls no same-file helper that has one'
                + ('; it fails only when a call returns an error or panics (?, unwrap, expect)' if soft else ''),
                {'fails_on_error': soft})
    if not n:
        return None
    return {'language': 'Rust', 'tests': n, 'doc_tests': docs, 'gates': gates, 'ci': ci,
            'reason': 'read with cargo_project.py and scanned statically: collect_check.py has no list command for '
                      'cargo tests (`cargo test -- --list` builds every test binary) and neutralise.py does not '
                      'neutralise Rust, so no Rust candidate is confirmed by a run'}


def main(argv=None) -> int:
    if len(argv or sys.argv[1:]) != 1:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    root = Path((argv or sys.argv[1:])[0]).resolve()
    import detect_conventions
    det = detect_conventions.Detector(root, 50_000).run(None)
    defs, cands = [], []

    def adder(rel):
        return lambda cat, rule, ids, line, why, details: cands.append(
            {'category': cat, 'rule': rule, 'test_ids': ids, 'file': rel, 'line': line, 'why': why, 'details': details})
    info = scan(root, det, adder, defs)
    print(json.dumps({'static_only': info, 'definitions': defs, 'candidates': cands}, indent=2))
    return 0 if info else 3


if __name__ == '__main__':
    sys.exit(main())
