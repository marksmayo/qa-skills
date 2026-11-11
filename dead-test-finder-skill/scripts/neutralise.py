#!/usr/bin/env python3
"""Neutralise the code a test is meant to check, in a scratch copy, and see whether the test notices (stdlib only).

    python neutralise.py --repo <repo> --candidates candidates.json --test <test id> [--test ...] --run-dir run
        [--target <test id>=<module.function>] [--cwd client=/copy/with/node_modules] [--patch fix.diff]

For each test the targets are the functions it calls that come from product code (Python: the
imported names in candidates.json; JavaScript/TypeScript: functions imported from relative
paths). The test runs once unchanged (`original`), then once per target and variant:

    return   the body becomes `return None` (Python) or `return undefined` (JS/TS)
    raise    the body raises RuntimeError / Error("neutralised by dead-test-finder")

Runs go through run_tests.py, appended to <run-dir>/runs.json with labels like
"neutralise gst return". A test that passes with the `return` variant can't fail on that code;
whether it passes with `raise` says whether it at least checks that nothing raises.

--patch applies a proposed test fix (a unified diff with repo-relative paths) to the scratch copy
first, so a strengthening fix can be proved: it passes on the original code and fails when the
code is neutralised. Test ids may then name tests that only exist after the patch.

Python packages are copied to a temporary directory outside the repo; a --cwd directory is used as given (it
must be a scratch copy) and every file patched in it is restored afterwards. The repository's
tree hash is checked before and after; the script exits 3 if it changed.
"""
from __future__ import annotations
import argparse, ast, hashlib, json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_tests, scan_tests, unified_diff  # noqa: E402

MARK = 'neutralised by dead-test-finder'


def tree_hash(root: Path, skip: Path) -> str:
    """run_tests.code_hash over the repo, leaving out the run directory when it sits inside it."""
    h = hashlib.sha256()
    skip = skip.resolve()
    for p in sorted(run_tests.iter_files(root, {skip}), key=lambda p: p.relative_to(root).as_posix()):
        h.update(p.relative_to(root).as_posix().encode('utf-8') + b'\0' + p.read_bytes().replace(b'\r\n', b'\n') + b'\0')
    return h.hexdigest()


def py_target_file(pkg_dir: Path, dotted: str) -> tuple[Path, str] | None:
    """'ledger.tax.gst' -> (pkg_dir/ledger/tax.py, 'gst')."""
    parts = dotted.split('.')
    for cut in range(len(parts) - 1, 0, -1):
        base = pkg_dir.joinpath(*parts[:cut])
        for cand in (base.with_suffix('.py'), base / '__init__.py', pkg_dir / 'src' / Path(*parts[:cut]).with_suffix('.py')):
            if cand.is_file():
                return cand, parts[cut]
    return None


def py_patch(text: str, func: str, variant: str) -> str | None:
    tree = ast.parse(text)
    lines = text.split('\n')
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
            first = node.body[0]
            indent = ' ' * first.col_offset
            stmt = 'return None' if variant == 'return' else f'raise RuntimeError("{MARK}")'
            return '\n'.join(lines[:first.lineno - 1] + [f'{indent}{stmt}  # {MARK}'] + lines[node.end_lineno:])
    return None


def js_target_file(test_file: Path, name: str) -> Path | None:
    text = test_file.read_text(encoding='utf-8', errors='replace')
    for m in re.finditer(r'''import\s*\{([^}]*)\}\s*from\s*["'](\.[^"']+)["']''', text):
        names = [x.strip().split(' as ')[0] for x in m.group(1).split(',')]
        if name in names:
            base = (test_file.parent / m.group(2)).resolve()
            for cand in [base] + [base.with_suffix(e) for e in ('.ts', '.tsx', '.js', '.jsx', '.mjs')] + \
                    [base / f'index{e}' for e in ('.ts', '.js')]:
                if cand.is_file():
                    return cand
    return None


def js_patch(text: str, func: str, variant: str) -> str | None:
    m = re.search(rf'(?:function\s*\*?\s*{re.escape(func)}\s*\([^)]*\)[^{{]*|(?:const|let)\s+{re.escape(func)}\s*=\s*(?:async\s*)?'
                  rf'\([^)]*\)[^=]*=>\s*)\{{', text)
    if not m:
        return None
    open_i = m.end() - 1
    close = scan_tests.match_paren(text, open_i)
    body = '{ return undefined; }' if variant == 'return' else f'{{ throw new Error("{MARK}"); }}'
    return text[:open_i] + body + f' /* {MARK} */' + text[close + 1:]


def run_test(defn: dict, work: Path, run_dir: Path, label: str, prefix: str) -> tuple[str, str]:
    rel = defn['file'][len(defn['package']) + 1:] if defn['package'] != '.' else defn['file']
    if defn['lang'] == 'python':
        args = ['--preset', 'pytest', '--tests', rel + '::' + defn['id'].split('::', 1)[1]]
    else:
        args = ['--preset', 'vitest', '--tests', rel]
    runs_file = run_dir / 'runs.json'
    before = {r['id'] for r in json.loads(runs_file.read_text(encoding='utf-8'))['runs']} if runs_file.exists() else set()
    subprocess.run([sys.executable, str(HERE / 'run_tests.py'), *args, '--cwd', str(work), '--out', str(runs_file),
                    '--label', label, '--run-prefix', prefix], capture_output=True, text=True,
                   env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    data = json.loads(runs_file.read_text(encoding='utf-8'))
    new = [r for r in data['runs'] if r['id'] not in before]
    if not new:
        return 'none', 'not_run'
    rid = new[-1]['id']
    want = defn['id'].split('::', 1)[1]
    for c in data['cases']:
        if c['run_id'] != rid:
            continue
        if defn['lang'] == 'python' and c['name'] == want.split('::')[-1]:
            return rid, c['status']
        if defn['lang'] != 'python' and (c['name'] == want or c['name'] == want.split(' > ')[-1]):
            return rid, c['status']
    return rid, 'not_found'


def localise(text: str, prefix: str) -> str:
    """Make a repo-relative diff relative to the package directory."""
    if not prefix:
        return text
    p = re.escape(prefix)
    text = re.sub(r'^(---|\+\+\+) ([ab]/)' + p, r'\1 \2', text, flags=re.M)
    text = re.sub(r'^diff --git a/' + p + r'(\S+) b/' + p, r'diff --git a/\1 b/', text, flags=re.M)
    return re.sub(r'^(rename (?:from|to) )' + p, r'\1', text, flags=re.M)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--test', action='append', required=True)
    ap.add_argument('--target', action='append', default=[], help='TEST_ID=module.function (or JS function name)')
    ap.add_argument('--run-dir', type=Path, required=True)
    ap.add_argument('--cwd', action='append', default=[], help='PACKAGE=DIR scratch copy to patch in place (JS with node_modules)')
    ap.add_argument('--patch', type=Path, help='unified diff (repo-relative paths) applied to the scratch copy first')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    root = args.repo.resolve()
    defs = {d['id']: d for d in json.loads(args.candidates.read_text(encoding='utf-8'))['definitions']}
    packages = sorted({d['package'] for d in defs.values()}, key=len, reverse=True)
    overrides = dict(x.split('=', 1) for x in args.cwd)
    forced: dict[str, list[str]] = {}
    for t in args.target:
        tid, _, tgt = t.rpartition('=')
        forced.setdefault(tid, []).append(tgt)
    args.run_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out or args.run_dir / 'neutralisations.json'
    results = json.loads(out_path.read_text(encoding='utf-8'))['results'] if out_path.exists() else []
    runs_file = args.run_dir / 'runs.json'
    n = len(json.loads(runs_file.read_text(encoding='utf-8'))['runs']) if runs_file.exists() else 0
    h_before = tree_hash(root, args.run_dir)
    restore: dict[Path, str | None] = {}
    scratch = tempfile.mkdtemp(prefix='dtf-')
    patch_name = args.patch.as_posix() if args.patch else None
    try:
        for tid in args.test:
            d = defs.get(tid)
            if not d:
                if not args.patch:
                    print(f'unknown test id {tid}', file=sys.stderr)
                    return 2
                file = tid.split('::')[0]
                pkg = next((p for p in packages if p == '.' or file.startswith(p + '/')), '.')
                d = {'id': tid, 'file': file, 'package': pkg, 'lang': 'python' if file.endswith('.py') else 'typescript', 'calls': []}
            prefix = d['package'] + '/' if d['package'] != '.' else ''
            if d['package'] in overrides:
                work = Path(overrides[d['package']]).resolve()
            else:
                work = Path(tempfile.mkdtemp(prefix='dtf-neutralise-', dir=scratch)) / PurePosixPath(d['package'] or 'repo').name
                shutil.copytree(root / d['package'] if prefix else root, work,
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.pytest_cache', 'node_modules'))
            if args.patch:
                text = args.patch.read_text(encoding='utf-8')
                for f in unified_diff.parse_patch(text):
                    for rel in {f['old'], f['new']} - {None}:
                        if rel.startswith(prefix):
                            p = work / rel[len(prefix):]
                            restore.setdefault(p, p.read_text(encoding='utf-8') if p.is_file() else None)
                unified_diff.apply_patch(localise(text, prefix), work)
            entry = {'test_id': tid, 'patch': patch_name, 'targets': [], 'note': None}
            n += 1
            rid, status = run_test(d, work, args.run_dir, 'original' + (' (patched)' if args.patch else ''), f'N{n}')
            entry['original'] = {'run_id': rid, 'test_status': status}
            targets = forced.get(tid) or d['calls']
            if not targets:
                entry['note'] = 'no product function to neutralise (the test calls nothing it imports)'
            for tgt in targets:
                if d['lang'] == 'python':
                    loc, patcher = py_target_file(work, tgt), py_patch
                else:
                    f = js_target_file(work / d['file'][len(prefix):], tgt)
                    loc, patcher = ((f, tgt) if f else None), js_patch
                if not loc:
                    entry['targets'].append({'target': tgt, 'file': None, 'variants': [], 'note': 'definition not found'})
                    continue
                path, func = loc
                original = path.read_text(encoding='utf-8')
                tres = {'target': tgt, 'file': prefix + path.relative_to(work).as_posix(), 'variants': []}
                for variant in ('return', 'raise'):
                    patched = patcher(original, func, variant)
                    if patched is None:
                        tres['note'] = f'{func} not found'
                        break
                    path.write_text(patched, encoding='utf-8', newline='\n')
                    try:
                        n += 1
                        rid, st = run_test(d, work, args.run_dir, f'neutralise {func} {variant}' + (' (patched)' if args.patch else ''),
                                           f'N{n}')
                    finally:
                        path.write_text(original, encoding='utf-8', newline='\n')
                    tres['variants'].append({'variant': variant, 'run_id': rid, 'test_status': st})
                entry['targets'].append(tres)
            ran = [t for t in entry['targets'] if t['variants']]
            passed_return = [t for t in ran if any(v['variant'] == 'return' and v['test_status'] == 'passed' for v in t['variants'])]
            entry['outcome'] = ('not_run' if not ran else 'passed_neutralised' if len(passed_return) == len(ran) else
                                'failed_neutralised')
            entry['checks_no_exception'] = any(v['variant'] == 'raise' and v['test_status'] in ('failed', 'error')
                                               for t in ran for v in t['variants'])
            results = [r for r in results if not (r['test_id'] == tid and r.get('patch') == patch_name)] + [entry]
            print(f'{tid}{" (patched)" if patch_name else ""}: original {status}, {entry["outcome"]}' +
                  ''.join(f'\n  {t["target"]}: ' + ', '.join(f'{v["variant"]} -> {v["test_status"]}' for v in t['variants'])
                          for t in entry['targets']))
    finally:
        for p, text in restore.items():
            if text is None:
                p.unlink(missing_ok=True)
            else:
                p.write_text(text, encoding='utf-8', newline='\n')
        shutil.rmtree(scratch, ignore_errors=True)
    h_after = tree_hash(root, args.run_dir)
    out_path.write_text(json.dumps({'tree_hash_before': h_before, 'tree_hash_after': h_after, 'results': results}, indent=2) + '\n',
                        encoding='utf-8')
    if h_before != h_after:
        print('the repository changed during neutralisation', file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
