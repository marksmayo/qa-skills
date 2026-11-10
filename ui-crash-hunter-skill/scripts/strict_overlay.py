#!/usr/bin/env python3
"""Run the repo's own TypeScript compiler and ESLint with stricter settings, writing the stricter
configs to a scratch directory only, and report the errors the strict settings add.

    python strict_overlay.py tsc <repo> --scratch <dir> [--tsconfig tsconfig.json] [--tsc "npx --no-install tsc"] [--out tsc.json]
    python strict_overlay.py eslint <repo> --scratch <dir> [--paths src] [--eslint "npx --no-install eslint"] [--out eslint.json]

tsc: a scratch tsconfig extends the repo's and turns on strictNullChecks and
noUncheckedIndexedAccess. Errors present only in the strict run are candidates (a value the
types say can be null or undefined is used as if it can't). The baseline run is the repo's own
config, so existing errors aren't reported.
eslint: the repo's config plus crash-relevant rules from plugins the repo already has
(react-hooks/exhaustive-deps, @typescript-eslint/no-floating-promises, no-non-null-assertion,
no-unnecessary-condition). Only violations the extra rules add are reported.

Nothing in the repo is written. Tools are never installed: a missing tool is reported as not_run,
and a failing tool as errored, so the report can lower its evidence completeness.
"""
from __future__ import annotations
import argparse, json, re, shlex, shutil, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

TSC_LINE = re.compile(r'^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s+error\s+(?P<code>TS\d+):\s+(?P<msg>.*)$')
NULL_CODES = {'TS2531', 'TS2532', 'TS2533', 'TS18047', 'TS18048', 'TS18049', 'TS2722', 'TS2721'}
ESLINT_RULES = [
    ('react-hooks', 'react-hooks/exhaustive-deps', 'WEB-SETSTATE-RENDER-LOOP'),
    ('@typescript-eslint', '@typescript-eslint/no-floating-promises', 'WEB-PROMISE-NO-CATCH'),
    ('@typescript-eslint', '@typescript-eslint/no-non-null-assertion', 'WEB-NON-NULL-ASSERTION'),
    ('@typescript-eslint', '@typescript-eslint/no-unnecessary-condition', None),
]
ESLINT_CONFIGS = ('eslint.config.js', 'eslint.config.mjs', 'eslint.config.cjs', 'eslint.config.ts', '.eslintrc', '.eslintrc.js',
                  '.eslintrc.cjs', '.eslintrc.json', '.eslintrc.yml', '.eslintrc.yaml')

def now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def result(tool: str, status: str, **kw) -> dict:
    return {'tool': tool, 'status': status, 'generated_at': now(), 'candidates': [], **kw}

def check_scratch(root: Path, scratch: Path) -> str | None:
    s = scratch.resolve()
    if s == root or root in s.parents:
        return f'--scratch must be outside the repository ({s} is inside {root})'
    s.mkdir(parents=True, exist_ok=True)
    return None

def resolve_cmd(cmd: str, root: Path) -> list[str] | None:
    parts = shlex.split(cmd, posix=(sys.platform != 'win32'))
    exe = shutil.which(parts[0])
    if not exe:
        return None
    if parts[0] in ('npx', 'npx.cmd') and '--no-install' in parts:
        tool = parts[parts.index('--no-install') + 1]
        bin_dir = root / 'node_modules' / '.bin'
        if not any((bin_dir / n).exists() for n in (tool, tool + '.cmd')):
            return None
    return [exe] + parts[1:]

def run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
    return p.returncode, (p.stdout or '') + (p.stderr or '')

def parse_tsc(output: str, root: Path) -> list[dict]:
    errs = []
    for line in output.splitlines():
        m = TSC_LINE.match(line.strip())
        if m:
            f = Path(m['file'])
            f = f if f.is_absolute() else root / f
            try:
                rel = f.resolve().relative_to(root).as_posix()
            except ValueError:
                rel = m['file'].replace('\\', '/')
            errs.append({'file': rel, 'line': int(m['line']), 'column': int(m['col']), 'code': m['code'], 'message': m['msg']})
    return errs

def cmd_tsc(args, root: Path) -> dict:
    tsconfig = (root / args.tsconfig).resolve()
    if not tsconfig.exists():
        return result('tsc-strict', 'not_applicable', note=f'no {args.tsconfig} in the repository')
    tsc = resolve_cmd(args.tsc, root)
    if not tsc:
        return result('tsc-strict', 'not_run', note=f'"{args.tsc}" not available (TypeScript not installed in the repo); not installed by this script')
    try:
        base_cfg = json.loads(re.sub(r'(?m)^\s*//.*$|/\*.*?\*/', '', tsconfig.read_text(encoding='utf-8-sig'), flags=re.S))
    except ValueError:
        base_cfg = {}
    co = base_cfg.get('compilerOptions') or {}
    overlay = {'extends': tsconfig.as_posix(),
               'compilerOptions': {'strictNullChecks': True, 'noUncheckedIndexedAccess': True, 'noEmit': True,
                                   'incremental': False, 'composite': False}}
    if 'typeRoots' not in co:
        overlay['compilerOptions']['typeRoots'] = [(root / 'node_modules' / '@types').as_posix()]
    if 'include' not in base_cfg and 'files' not in base_cfg:
        overlay['include'] = [(root / '**' / '*').as_posix()]
        overlay['exclude'] = [(root / d).as_posix() for d in ('node_modules', 'dist', 'build', 'out', '.next')]
    scratch_cfg = args.scratch.resolve() / 'tsconfig.strict.json'
    scratch_cfg.write_text(json.dumps(overlay, indent=2), encoding='utf-8')
    try:
        base_rc, base_out = run(tsc + ['-p', str(tsconfig), '--noEmit', '--pretty', 'false'], root, args.timeout)
        strict_rc, strict_out = run(tsc + ['-p', str(scratch_cfg), '--pretty', 'false'], root, args.timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return result('tsc-strict', 'errored', note=str(exc))
    base, strict = parse_tsc(base_out, root), parse_tsc(strict_out, root)
    if strict_rc not in (0, 1, 2) or (strict_rc and not strict):
        return result('tsc-strict', 'errored', note='tsc failed without diagnostics', output=strict_out[-2000:])
    seen = {(e['file'], e['line'], e['column'], e['code']) for e in base}
    new = [e for e in strict if (e['file'], e['line'], e['column'], e['code']) not in seen]
    version = run(tsc + ['--version'], root, 60)[1].strip()
    cands = []
    for e in new:
        try:
            src = (root / e['file']).read_text(encoding='utf-8', errors='replace').splitlines()[e['line'] - 1].strip()
        except (OSError, IndexError):
            src = ''
        cands.append({'pitfall_id': 'WEB-UNGUARDED-OPTIONAL' if e['code'] in NULL_CODES else None,
                      'file': e['file'], 'line': e['line'], 'column': e['column'], 'snippet': src[:200],
                      'message': f"{e['code']}: {e['message']}",
                      'sources': [{'tool': 'tsc-strict', 'version': version, 'rule_id': e['code']}]})
    return result('tsc-strict', 'ran', version=version, scratch_config=scratch_cfg.as_posix(),
                  command=' '.join(tsc + ['-p', scratch_cfg.as_posix()]), baseline_errors=len(base), strict_errors=len(strict),
                  new_errors=len(new), candidates=cands)

def cmd_eslint(args, root: Path) -> dict:
    if not any((root / c).exists() for c in ESLINT_CONFIGS) and 'eslintConfig' not in _pkg(root):
        return result('eslint-strict', 'not_run', note='no ESLint configuration in the repository')
    eslint = resolve_cmd(args.eslint, root)
    if not eslint:
        return result('eslint-strict', 'not_run', note=f'"{args.eslint}" not available; not installed by this script')
    deps = _pkg(root)
    deps = {**(deps.get('dependencies') or {}), **(deps.get('devDependencies') or {})}
    have = {'react-hooks': 'eslint-plugin-react-hooks' in deps,
            '@typescript-eslint': '@typescript-eslint/eslint-plugin' in deps or 'typescript-eslint' in deps}
    rules = [(r, pid) for plugin, r, pid in ESLINT_RULES if have.get(plugin)]
    if not rules:
        return result('eslint-strict', 'not_applicable', note='none of the crash-relevant ESLint plugins are installed')
    paths = args.paths or ['.']
    extra = []
    for r, _ in rules:
        extra += ['--rule', f'{r}: error']
    try:
        _, base_out = run(eslint + ['-f', 'json', *paths], root, args.timeout)
        _, strict_out = run(eslint + ['-f', 'json', *extra, *paths], root, args.timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return result('eslint-strict', 'errored', note=str(exc))
    def parse(out: str) -> list[dict] | None:
        start = out.find('[')
        try:
            return json.loads(out[start:]) if start >= 0 else None
        except ValueError:
            return None
    base, strict = parse(base_out), parse(strict_out)
    if strict is None:
        return result('eslint-strict', 'errored', note='ESLint did not produce JSON', output=strict_out[-2000:])
    key = lambda f, m: (f, m.get('line'), m.get('column'), m.get('ruleId'))
    seen = {key(f['filePath'], m) for f in base or [] for m in f.get('messages', [])}
    pid_by_rule = dict(rules)
    cands = []
    for f in strict:
        rel = Path(f['filePath']).resolve()
        rel = rel.relative_to(root).as_posix() if root in rel.parents else f['filePath']
        for m in f.get('messages', []):
            if key(f['filePath'], m) in seen or m.get('ruleId') not in pid_by_rule:
                continue
            cands.append({'pitfall_id': pid_by_rule[m['ruleId']], 'file': rel, 'line': m.get('line'), 'column': m.get('column'),
                          'snippet': '', 'message': f"{m['ruleId']}: {m.get('message', '')}",
                          'sources': [{'tool': 'eslint-strict', 'rule_id': m['ruleId']}]})
    return result('eslint-strict', 'ran', rules=[r for r, _ in rules], command=' '.join(eslint + extra + paths), candidates=cands)

def _pkg(root: Path) -> dict:
    try:
        return json.loads((root / 'package.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('tsc', 'eslint'):
        s = sub.add_parser(name)
        s.add_argument('repo', type=Path)
        s.add_argument('--scratch', type=Path, required=True, help='directory outside the repo for overlay configs')
        s.add_argument('--timeout', type=int, default=900)
        s.add_argument('--out', type=Path)
    sub.choices['tsc'].add_argument('--tsconfig', default='tsconfig.json')
    sub.choices['tsc'].add_argument('--tsc', default='npx --no-install tsc')
    sub.choices['eslint'].add_argument('--paths', nargs='*')
    sub.choices['eslint'].add_argument('--eslint', default='npx --no-install eslint')
    args = ap.parse_args()
    root = args.repo.resolve()
    problem = check_scratch(root, args.scratch)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    out = cmd_tsc(args, root) if args.cmd == 'tsc' else cmd_eslint(args, root)
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + '\n', encoding='utf-8')
        extra = f", {out.get('new_errors', len(out['candidates']))} new error(s)" if out['status'] == 'ran' else f": {out.get('note', '')}"
        print(f"{out['tool']}: {out['status']}{extra} -> {args.out}")
    else:
        print(text)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
