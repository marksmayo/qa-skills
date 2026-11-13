#!/usr/bin/env python3
"""Run the repo's own TypeScript compiler and ESLint with stricter settings, writing the stricter
configs to a scratch directory only, and report the errors the strict settings add.

    python strict_overlay.py tsc <repo> --scratch <dir> [--tsconfig tsconfig.json] [--tsc "npx --no-install tsc"] [--out tsc.json]
    python strict_overlay.py eslint <repo> --scratch <dir> [--paths src] [--eslint "npx --no-install eslint"] [--out eslint.json]

tsc: a scratch tsconfig extends the repo's and turns on strictNullChecks and
noUncheckedIndexedAccess. Errors present only in the strict run are candidates (a value the
types say can be null or undefined is used as if it can't). The baseline is the repo's own config
through a second scratch config, so existing errors aren't reported; the two compiles run side by
side. Both scratch configs set incremental false and their tsBuildInfoFile in the scratch dir, so
nothing (not even tsconfig.tsbuildinfo) is written into the repository. The repo's tsconfig and
jsconfig are read as JSON with comments ("@/*" in a string is not a comment).
A solution-style tsconfig ("files": [] and "references", the create-vite layout) compiles nothing and
carries none of the app's compiler options, so the referenced project whose include covers src/
(tsconfig.app.json) is compiled instead; if none does, the result is errored with the reason. The
baseline lists the files it compiled (--listFiles): a compile that covered none of the repository's
source files is errored, never "ran with 0 new errors", and a baseline with more than 50 errors
carries a warning that the tsconfig is probably not the one the build uses. The output always names the
tsconfig it compiled and how many source files (and how many of the UI's files) it covered, and warns
when that tsconfig compiles under half of the files the UI entry points reach, when the baseline has
errors of its own, when workspace dependencies ("workspace:*") have no built output (build the
workspace first), and when the pages' browser code is inline scripts in HTML template literals that
no type checker sees. Without --tsconfig, the tsconfig nearest most UI entry points is compiled when
that is a nested project (app/tsconfig.json beside app/next.config.ts), not the root's.
--scope ui (default) compiles only the UI entry points (Next.js page/layout/error files, src/main.*,
the scripts index.html loads; see ui_graph.py) and what they import, plus declaration files: tests,
scripts and modules no page imports are left out. --scope all compiles the whole project.
Plain JavaScript (no tsconfig): when a TypeScript compiler is available (the repo's, or tsc on PATH),
a config generated in the scratch directory type-checks the JavaScript files (allowJs, checkJs,
noEmit; extending jsconfig.json if there is one), and the strict overlay is compared against it the
same way. Without any tsc the result is not_applicable.
eslint: the repo's config plus crash-relevant rules from plugins the config already loads
(react-hooks/exhaustive-deps, @typescript-eslint/no-floating-promises, no-non-null-assertion,
no-unnecessary-condition). Which plugins are loaded is asked of ESLint itself (--print-config on a
source file of the app's main extension, under src/, never a tool config such as eslint.config.js),
so plugins that come through a shared config such as eslint-config-next count; the type-aware rules
run only when the config sets parserOptions.project or projectService. Only violations the extra
rules add are reported, each with the source line as its snippet. --scope ui lints the files the UI
entry points reach.

Nothing in the repo is written. Tools are never installed: a missing tool is reported as not_run,
and a failing tool as errored, so the report can lower its evidence completeness.
"""
from __future__ import annotations
import argparse, json, re, shlex, shutil, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True  # importing a sibling must not write __pycache__ into the skill folder
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ui_graph import (load_jsonc, graph as ui_graph, walk as walk_repo, is_solution, ui_project, references,  # noqa: E402
                      is_tool_config)
from recon import node_server_model  # noqa: E402

TSC_LINE = re.compile(r'^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s+error\s+(?P<code>TS\d+):\s+(?P<msg>.*)$')
NULL_CODES = {'TS2531', 'TS2532', 'TS2533', 'TS18047', 'TS18048', 'TS18049', 'TS2722', 'TS2721'}
ESLINT_RULES = [
    ('react-hooks', 'react-hooks/exhaustive-deps', 'WEB-SETSTATE-RENDER-LOOP'),
    ('@typescript-eslint', '@typescript-eslint/no-floating-promises', 'WEB-PROMISE-NO-CATCH'),
    ('@typescript-eslint', '@typescript-eslint/no-non-null-assertion', 'WEB-NON-NULL-ASSERTION'),
    ('@typescript-eslint', '@typescript-eslint/no-unnecessary-condition', None),
]
TYPE_AWARE = {'@typescript-eslint/no-floating-promises', '@typescript-eslint/no-unnecessary-condition'}
ESLINT_CONFIGS = ('eslint.config.js', 'eslint.config.mjs', 'eslint.config.cjs', 'eslint.config.ts', 'eslint.config.mts',
                  'eslint.config.cts', '.eslintrc', '.eslintrc.js',
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

def run_both(cmds: list[list[str]], cwd: Path, timeout: int, scratch: Path) -> list[tuple[int, str]]:
    """Run commands at the same time (the baseline and the strict compile are independent), each
    writing to a file in the scratch directory; returns (exit code, output) per command."""
    procs = []
    for n, cmd in enumerate(cmds):
        out = open(scratch / f'run-{n}.log', 'w+', encoding='utf-8', errors='replace')
        procs.append((subprocess.Popen(cmd, cwd=cwd, stdout=out, stderr=subprocess.STDOUT), out))
    deadline = time.monotonic() + timeout
    results = []
    try:
        for p, out in procs:
            p.wait(timeout=max(1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        for p, _ in procs:
            p.kill()
        raise
    finally:
        for p, out in procs:
            out.seek(0)
            results.append((p.returncode, out.read()))
            out.close()
    return results

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

def compiled_files(output: str, root: Path) -> int:
    """How many of the repository's own source files a `tsc --listFiles` run compiled (not node_modules or lib files)."""
    return len(compiled_list(output, root))

def compiled_list(output: str, root: Path) -> list[str]:
    """The repository's own source files (relative paths) a `tsc --listFiles` run compiled."""
    out = []
    for line in output.splitlines():
        s = line.strip()
        if not s or TSC_LINE.match(s) or not re.search(r'\.(tsx?|jsx?|mts|cts|mjs|cjs|vue|svelte)$', s):
            continue
        p = Path(s)
        try:
            rel = p.resolve().relative_to(root)
        except (ValueError, OSError):
            continue
        if 'node_modules' not in rel.parts and not s.endswith('.d.ts'):
            out.append(rel.as_posix())
    return out

DEFAULT_TSC = 'npx --no-install tsc'
SKIP_DIRS = {'node_modules', '.git', 'dist', 'build', 'out', '.next', '.nuxt', 'coverage', 'vendor', '__pycache__',
             'storybook-static', '.venv', 'venv'}
JS_EXT = ('.js', '.mjs', '.cjs', '.jsx')

def js_sources(root: Path, limit: int = 3000) -> list[Path]:
    """Plain JavaScript source files: no dependencies, build output, minified or bundled files."""
    found, stack = [], [root]
    while stack and len(found) < limit:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in SKIP_DIRS and not p.name.startswith('.'):
                    stack.append(p)
            elif p.suffix in JS_EXT and not p.name.endswith('.min.js') and not p.with_name(p.name + '.map').exists():
                try:
                    if p.stat().st_size <= 400_000:
                        found.append(p)
                except OSError:
                    pass
    return found

def checkjs_config(args, root: Path) -> tuple[Path | None, dict | None, str | None]:
    """For a repository without a tsconfig: a scratch config that type-checks its JavaScript (allowJs,
    checkJs, noEmit), extending the repo's jsconfig.json when it has one. Returns (config, info, why_not)."""
    jsconfig = root / 'jsconfig.json'
    files = js_sources(root)
    if not files and not jsconfig.exists():
        return None, None, f'no {args.tsconfig} and no JavaScript sources in the repository'
    cfg: dict = {'compilerOptions': {'allowJs': True, 'checkJs': True, 'noEmit': True, 'skipLibCheck': True,
                                     'target': 'ES2022', 'lib': ['DOM', 'DOM.Iterable', 'ES2022'], 'types': [],
                                     'maxNodeModuleJsDepth': 0, 'incremental': False}}
    own: dict = {}
    if jsconfig.exists():
        cfg['extends'] = jsconfig.as_posix()
        try:
            own = load_jsonc(jsconfig).get('compilerOptions') or {}
        except (OSError, ValueError):
            own = {}
    # the baseline is loose (newer compilers default to strict), so the overlay shows what strictness adds
    for k in ('strict', 'strictNullChecks', 'noImplicitAny'):
        if k not in own:
            cfg['compilerOptions'][k] = False
    if files:
        cfg['files'] = [f.as_posix() for f in files]
    path = args.scratch.resolve() / 'tsconfig.checkjs.json'
    path.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
    return path, {'mode': 'checkJs', 'generated_config': path.as_posix(), 'js_files': len(files),
                  'extends_jsconfig': jsconfig.exists()}, None

def nearest_config(root: Path, entries: list[str]) -> Path | None:
    """The tsconfig.json nearest most UI entry points (walking up from each to the root), when that is not the
    root's own: a Next.js app nested in app/ keeps its tsconfig (with its jsx and "@/*" paths) beside
    app/next.config.ts, while the root tsconfig.json may compile only the test suites."""
    votes: dict[Path, int] = {}
    for e in entries:
        d = (root / e).parent
        while True:
            if (d / 'tsconfig.json').is_file():
                votes[d / 'tsconfig.json'] = votes.get(d / 'tsconfig.json', 0) + 1
                break
            if d == root or root not in d.parents:
                break
            d = d.parent
    if not votes:
        return None
    best, n = max(votes.items(), key=lambda kv: kv[1])
    return best if best.parent != root and n * 2 > len(entries) else None


def unbuilt_workspace_packages(root: Path) -> list[str]:
    """Workspace dependencies ("workspace:*") whose installed package points its types/main/exports at files
    that do not exist (an unbuilt dist/): the compile then reports every import of them as an error."""
    out = []
    deps = {}
    for d in (root, *root.parents):
        pkg = _pkg(d)
        deps.update({k: v for k, v in {**(pkg.get('dependencies') or {}), **(pkg.get('devDependencies') or {})}.items()
                     if isinstance(v, str) and v.startswith('workspace:') and k not in deps})
        if (d / 'pnpm-workspace.yaml').exists() or pkg.get('workspaces'):
            break
    for name in sorted(deps):
        pdir = root / 'node_modules' / name
        try:
            meta = json.loads((pdir / 'package.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        targets = [meta.get(k) for k in ('types', 'typings', 'main', 'module') if isinstance(meta.get(k), str)]
        exp = meta.get('exports')
        stack = [exp]
        while stack and len(targets) < 40:
            x = stack.pop()
            if isinstance(x, str):
                targets.append(x)
            elif isinstance(x, dict):
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
        targets = [t for t in targets if t.startswith('.') and '*' not in t]
        if targets and not any((pdir / t).exists() for t in targets):
            out.append(name)
    return out


def cmd_tsc(args, root: Path) -> dict:
    extra: dict = {}
    if args.tsconfig:
        tsconfig = (root / args.tsconfig).resolve()
    else:
        tsconfig = (root / 'tsconfig.json').resolve()
        g0 = ui_graph(root)
        nested = nearest_config(root, g0['entries'])
        if nested is not None:
            extra['project_note'] = (f"compiled {nested.relative_to(root).as_posix()}, the tsconfig nearest the UI entry "
                                     f"points (not the root's {tsconfig.name}); pass --tsconfig to choose another")
            tsconfig = nested
    args.tsconfig = args.tsconfig or 'tsconfig.json'
    if tsconfig.exists() and is_solution(tsconfig):
        # a solution tsconfig ("files": [] + "references") compiles nothing itself, and extending it gives
        # none of the app's compiler options (jsx, paths, target): use the referenced project for the app
        project = ui_project(tsconfig)
        if project is None:
            return result('tsc-strict', 'errored', solution_config=tsconfig.name,
                          references=[q.name for q in references(tsconfig)],
                          note=f'{tsconfig.name} is a solution file ("files": [] and references) and no referenced project '
                               "compiles the app's source (src/); pass the app's project with --tsconfig")
        extra['solution_config'] = tsconfig.name
        extra['project_note'] = (f'{tsconfig.name} is a solution file (compiles nothing itself); compiled the referenced '
                                 f'project {project.name}, whose include covers the app source')
        tsconfig = project.resolve()
    if not tsconfig.exists():
        # plain JavaScript: type-check it with checkJs through a generated config, when a tsc is available
        tsc = resolve_cmd(args.tsc, root) or (resolve_cmd('tsc', root) if args.tsc == DEFAULT_TSC else None)
        if not tsc:
            return result('tsc-strict', 'not_applicable',
                          note=f'no {args.tsconfig}, and no TypeScript compiler in the repository or on PATH to check the '
                               'JavaScript with checkJs; not installed by this script')
        tsconfig, extra, why_not = checkjs_config(args, root)
        if not tsconfig:
            return result('tsc-strict', 'not_applicable', note=why_not)
    else:
        tsc = resolve_cmd(args.tsc, root)
        if not tsc:
            return result('tsc-strict', 'not_run', note=f'"{args.tsc}" not available (TypeScript not installed in the repo); not installed by this script')
    try:
        base_cfg = load_jsonc(tsconfig)
    except (OSError, ValueError) as exc:
        return result('tsc-strict', 'errored', note=f'cannot parse {tsconfig.name}: {exc}')
    co = base_cfg.get('compilerOptions') or {}
    scratch = args.scratch.resolve()
    # Both runs go through scratch configs that extend the repo's, so neither writes into the repository:
    # incremental (the Next.js default) would otherwise write tsconfig.tsbuildinfo beside the repo's config.
    common = {'noEmit': True, 'incremental': False, 'composite': False,
              'tsBuildInfoFile': (scratch / 'tsbuildinfo').as_posix()}
    if 'typeRoots' not in co:
        # The default typeRoots are relative to the config file, which now lives in the scratch dir. With an
        # explicit "types" list (e.g. "vitest/globals"), entries are looked up in typeRoots, so node_modules
        # itself is a root too; without one, it must not be (every package would become a global type package).
        roots = [d / 'node_modules' / '@types' for d in (root, *root.parents) if (d / 'node_modules' / '@types').is_dir()]
        if co.get('types'):
            roots += [d / 'node_modules' for d in (root, *root.parents) if (d / 'node_modules').is_dir()]
        common['typeRoots'] = [p.as_posix() for p in roots] or [(root / 'node_modules' / '@types').as_posix()]
    scope_note = None
    files_override: dict = {}
    if args.scope == 'ui' and extra.get('mode') != 'checkJs':
        g = ui_graph(root)
        if g['reachable']:
            dts = sorted(p.as_posix() for p in walk_repo(root) if p.name.endswith('.d.ts'))
            files_override = {'files': [(root / f).as_posix() for f in g['entries']] + dts, 'include': []}
            scope_note = (f"scope ui: compiled from {len(g['entries'])} UI entry point(s) and what they import "
                          f"({len(g['reachable'])} source file(s)) plus {len(dts)} declaration file(s); tests, scripts and "
                          "modules no page imports are not compiled (--scope all compiles the whole project)")
        else:
            scope_note = 'scope ui requested, but no UI entry point was recognised; the whole project was compiled'
    if not files_override and 'include' not in base_cfg and 'files' not in base_cfg:
        files_override = {'include': [(root / '**' / '*').as_posix()],
                          'exclude': [(root / d).as_posix() for d in ('node_modules', 'dist', 'build', 'out', '.next')]}
    base_overlay = {'extends': tsconfig.as_posix(), 'compilerOptions': dict(common), **files_override}
    overlay = {'extends': tsconfig.as_posix(),
               'compilerOptions': {**common, 'strictNullChecks': True, 'noUncheckedIndexedAccess': True}, **files_override}
    base_cfg_path = scratch / 'tsconfig.baseline.json'
    scratch_cfg = scratch / 'tsconfig.strict.json'
    base_cfg_path.write_text(json.dumps(base_overlay, indent=2), encoding='utf-8')
    scratch_cfg.write_text(json.dumps(overlay, indent=2), encoding='utf-8')
    started = time.monotonic()
    try:
        (base_rc, base_out), (strict_rc, strict_out) = run_both(
            [tsc + ['-p', str(base_cfg_path), '--pretty', 'false', '--listFiles'],
             tsc + ['-p', str(scratch_cfg), '--pretty', 'false']],
            root, args.timeout, scratch)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return result('tsc-strict', 'errored', note=str(exc))
    elapsed = round(time.monotonic() - started, 1)
    if scope_note:
        extra = {**extra, 'scope': args.scope, 'scope_note': scope_note}
    base, strict = parse_tsc(base_out, root), parse_tsc(strict_out, root)
    if strict_rc not in (0, 1, 2) or (strict_rc and not strict):
        return result('tsc-strict', 'errored', note='tsc failed without diagnostics', output=strict_out[-2000:])
    if base_rc not in (0, 1, 2) or (base_rc and not base):
        return result('tsc-strict', 'errored', note='the baseline tsc run failed without diagnostics', output=base_out[-2000:])
    compiled = compiled_list(base_out, root)
    covered = len(compiled)
    extra['tsconfig'] = tsconfig.relative_to(root).as_posix() if tsconfig.is_relative_to(root) else tsconfig.as_posix()
    warnings = []
    g = ui_graph(root) if extra.get('mode') != 'checkJs' else None
    ui = set(g['reachable'] or []) if g else set()
    if ui:
        hit = sum(1 for f in compiled if f in ui)
        extra['ui_files_compiled'] = hit
        if hit * 2 < len([f for f in ui if not f.endswith('.d.ts')]):
            warnings.append(f"{extra['tsconfig']} compiled {hit} of the {len(ui)} file(s) the UI entry points reach "
                            f"({covered} source file(s) in all): it is probably not the tsconfig that builds the UI. "
                            'Pass the UI project with --tsconfig')
    unbuilt = unbuilt_workspace_packages(root)
    if unbuilt:
        extra['unbuilt_workspace_packages'] = unbuilt
    if unbuilt and base:  # only worth a warning when the baseline has errors they may explain
        warnings.append(f"workspace package(s) {', '.join(unbuilt[:5])} have no built output (their types/main/exports "
                        'files are missing): every import of them is a baseline error. Build the workspace first, '
                        'then rerun')
    server = node_server_model(root, [f for f in walk_repo(root) if f.suffix in ('.js', '.mjs', '.cjs', '.ts', '.mts')])
    if server and server['inline_scripts']:
        warnings.append(f"the pages' browser code is {server['inline_scripts']} inline <script> block(s) inside HTML "
                        f"template literals ({', '.join([t['file'] for t in server['templates'] if t['inline_scripts']][:3])}), which no type "
                        'checker sees: this compile covers the server and test code, not that browser code')
    if not covered:
        # zero errors from a compile of zero files is not a result
        return result('tsc-strict', 'errored', **extra, baseline_config=base_cfg_path.as_posix(), compiled_files=0,
                      note=f'the compile covered no source file of the repository ({tsconfig.name} selects none with this '
                           'scope); 0 new errors would mean nothing. Pass the project that compiles the app with --tsconfig')
    extra['compiled_files'] = covered
    if len(base) > 50:
        warnings.insert(0, f'the baseline compile already has {len(base)} errors: if the repository\'s own type check is '
                           'clean, this is not the tsconfig its build uses (wrong compiler options or paths), and the '
                           'new errors are unreliable; pass the right project with --tsconfig')
    elif base:
        warnings.append(f'the baseline compile of {extra["tsconfig"]} has {len(base)} error(s) of its own; new errors are '
                        'counted against it, so check that it is the tsconfig the build uses')
    if warnings:
        extra['warning'] = warnings[0]
        extra['warnings'] = warnings
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
    return result('tsc-strict', 'ran', **extra, version=version, scratch_config=scratch_cfg.as_posix(),
                  baseline_config=base_cfg_path.as_posix(), seconds=elapsed,
                  command=' '.join(tsc + ['-p', scratch_cfg.as_posix()]), baseline_errors=len(base), strict_errors=len(strict),
                  new_errors=len(new), candidates=cands)

def cmd_eslint(args, root: Path) -> dict:
    if not any((root / c).exists() for c in ESLINT_CONFIGS) and 'eslintConfig' not in _pkg(root):
        return result('eslint-strict', 'not_run', note='no ESLint configuration in the repository')
    eslint = resolve_cmd(args.eslint, root)
    if not eslint:
        return result('eslint-strict', 'not_run', note=f'"{args.eslint}" not available; not installed by this script')
    paths = args.paths
    scope_note = None
    if not paths and args.scope == 'ui':
        g = ui_graph(root)
        reach = [f for f in (g['reachable'] or []) if f.endswith(('.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs')) and not f.endswith('.d.ts')]
        if reach and len(reach) <= 600 and sum(len(r) + 1 for r in reach) < 20000:  # the Windows command-line limit
            paths = reach
            scope_note = f'scope ui: linted the {len(reach)} source file(s) reachable from the UI entry points'
    if not paths:
        # not '.': a command-line --rule for a plugin applies to every linted file, and ESLint aborts on a file
        # (eslint.config.js) that the config's plugin block does not cover. Lint the app's own source files
        # of its main language family instead, as one glob per top-level folder (a file list can exceed the
        # Windows command-line limit).
        main = probe_file(root)
        fam = ('.ts', '.tsx', '.mts', '.cts') if main and main.endswith(('.ts', '.tsx')) else ('.js', '.jsx', '.mjs', '.cjs')
        src = [p.relative_to(root).as_posix() for p in walk_repo(root) if p.suffix in fam and not p.name.endswith('.d.ts')]
        src = [f for f in src if not is_tool_config(f)]
        exts = sorted({Path(f).suffix.lstrip('.') for f in src})
        pattern = ('{' + ','.join(exts) + '}') if len(exts) > 1 else (exts[0] if exts else '')
        top = sorted({f.split('/', 1)[0] for f in src if '/' in f})
        paths = [f'{d}/**/*.{pattern}' for d in top] + [f for f in src if '/' not in f]
        if paths:
            scope_note = (f'scope all: linted {len(src)} {"/".join(exts)} source file(s); tool configs '
                          '(eslint.config.js, vite.config.ts) and the other language family left out')
    paths = paths or ['.']
    # Which plugins the config really loads: eslint-config-next and other shared configs bring
    # eslint-plugin-react-hooks and typescript-eslint in transitively, so package.json alone undercounts.
    sample = next((p for p in paths if Path(p).suffix in ('.tsx', '.ts', '.jsx', '.js') and not is_tool_config(p)), None) \
        or probe_file(root)
    plugins, typed, how = None, False, 'package.json'
    if sample:
        try:
            rc, out = run(eslint + ['--print-config', sample], root, min(args.timeout, 120))
            cfg = json.loads(out[out.find('{'):]) if rc == 0 and '{' in out else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            cfg = None
        if isinstance(cfg, dict):
            raw = cfg.get('plugins') or []
            raw = list(raw.keys()) if isinstance(raw, dict) else raw
            plugins = {str(p).split(':')[0] for p in raw}
            po = {**((cfg.get('languageOptions') or {}).get('parserOptions') or {}), **(cfg.get('parserOptions') or {})}
            typed = bool(po.get('project') or po.get('projectService'))
            how = f'eslint --print-config {sample}'
    if plugins is None:
        deps = _pkg(root)
        deps = {**(deps.get('dependencies') or {}), **(deps.get('devDependencies') or {})}
        nm = root / 'node_modules'
        plugins = set()
        if 'eslint-plugin-react-hooks' in deps or (nm / 'eslint-plugin-react-hooks').is_dir():
            plugins.add('react-hooks')
        if {'@typescript-eslint/eslint-plugin', 'typescript-eslint'} & set(deps) or (nm / '@typescript-eslint' / 'eslint-plugin').is_dir():
            plugins.add('@typescript-eslint')
    skipped = []
    rules = []
    for plugin, r, pid in ESLINT_RULES:
        if plugin not in plugins:
            continue
        if r in TYPE_AWARE and not typed:
            skipped.append(r)
            continue
        rules.append((r, pid))
    if not rules:
        if how == 'package.json':
            return result('eslint-strict', 'not_run', note='could not confirm which plugins the ESLint config loads '
                          '(eslint --print-config failed) and none of the crash-relevant plugins is a dependency')
        return result('eslint-strict', 'not_applicable', plugins_detected_by=how, plugins=sorted(plugins),
                      note='the ESLint config loads none of the crash-relevant plugins (react-hooks, @typescript-eslint)'
                           + (f'; type-aware rules {", ".join(skipped)} skipped: no parserOptions.project/projectService'
                              if skipped else ''))
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
                          'snippet': line_at(root, rel, m.get('line')), 'message': f"{m['ruleId']}: {m.get('message', '')}",
                          'sources': [{'tool': 'eslint-strict', 'rule_id': m['ruleId']}]})
    more = {'scope_note': scope_note} if scope_note else {}
    if skipped:
        more['skipped_rules'] = skipped
        more['note'] = (f'type-aware rules not run ({", ".join(skipped)}): the config sets no parserOptions.project or '
                        'projectService, and the overlay does not add type information')
    shown = paths if len(paths) <= 5 else paths[:5] + [f'... ({len(paths)} files)']
    return result('eslint-strict', 'ran', rules=[r for r, _ in rules], plugins_detected_by=how, plugins=sorted(plugins),
                  command=' '.join(eslint + extra + shown), candidates=cands, **more)

def probe_file(root: Path) -> str | None:
    """A source file to ask ESLint about: the app's dominant extension, under src/ when there is one, never a
    tool config (a flat config often applies only to **/*.{ts,tsx}, so eslint.config.js would show no plugins)."""
    found = [p.relative_to(root).as_posix() for p in walk_repo(root)
             if p.suffix in ('.tsx', '.ts', '.jsx', '.js') and not p.name.endswith('.d.ts')]
    found = [f for f in found if not is_tool_config(f) and not re.search(r'(^|/)(tests?|__tests__|e2e)/|\.(test|spec)\.', f)]
    if not found:
        return None
    counts: dict[str, int] = {}
    for f in found:
        counts[Path(f).suffix] = counts.get(Path(f).suffix, 0) + 1
    dominant = max(counts, key=lambda e: (counts[e], e in ('.tsx', '.ts')))
    pool = [f for f in found if f.endswith(dominant)]
    in_src = [f for f in pool if re.match(r'^(.*/)?src/', f)]
    return sorted(in_src or pool, key=lambda f: (f.count('/'), f))[0]

def line_at(root: Path, rel: str, line: int | None) -> str:
    try:
        return (root / rel).read_text(encoding='utf-8', errors='replace').splitlines()[int(line) - 1].strip()[:200]
    except (OSError, IndexError, TypeError, ValueError):
        return ''

def _pkg(root: Path) -> dict:
    try:
        return json.loads((root / 'package.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}

def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):  # JSON on stdout may hold any character; a cp1252 console can't print it
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('tsc', 'eslint'):
        s = sub.add_parser(name)
        s.add_argument('repo', type=Path)
        s.add_argument('--scratch', type=Path, required=True, help='directory outside the repo for overlay configs')
        s.add_argument('--timeout', type=int, default=900)
        s.add_argument('--out', type=Path)
        s.add_argument('--scope', choices=['ui', 'all'], default='ui',
                       help='ui (default): only files reachable from UI entry points; all: the whole project')
    sub.choices['tsc'].add_argument('--tsconfig', default=None,
                                    help='the project to compile (default: tsconfig.json, or the tsconfig nearest the UI '
                                         'entry points when that is a nested project such as app/tsconfig.json)')
    sub.choices['tsc'].add_argument('--tsc', default=DEFAULT_TSC)
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
        if out.get('tsconfig'):  # always say what was compiled, so a wrong project is visible
            print(f"  compiled {out['tsconfig']}: {out.get('compiled_files', 0)} source file(s)"
                  + (f", {out['ui_files_compiled']} of them reachable from the UI" if 'ui_files_compiled' in out else '')
                  + (f", baseline errors {out['baseline_errors']}" if 'baseline_errors' in out else ''))
        for w in out.get('warnings') or ([out['warning']] if out.get('warning') else []):
            print(f'  WARN: {w}')
    else:
        print(text)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
