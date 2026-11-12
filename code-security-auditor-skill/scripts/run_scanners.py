#!/usr/bin/env python3
"""Detect and run the open-source security scanners that apply to a repository.

    python run_scanners.py <repo> --out-dir <dir outside the repo> [--only semgrep,bandit] [--skip trivy]
                           [--offline] [--dry-run] [--timeout 900] [--path-prepend <venv/bin>]

Every tool is recorded with a status: ran, errored (with the error), not_run (not installed,
offline, or needs something the script won't do on its own, with the reason) or not_applicable
(nothing for it to scan). Nothing is installed, and the repository is never written to; outputs
go to --out-dir, which must be outside the repository. A manifest, scanners.json, lists every
tool's command, version, output file and status for the report's `tools` section.

Network: Semgrep fetches registry rules (metrics are turned off); pip-audit, npm audit,
osv-scanner and Trivy query vulnerability databases with package names and versions, never
source code. --offline skips those tools and records why.

Only free, open-source tools whose licences allow scanning closed-source code are used: Semgrep
OSS with community rules, Bandit, gosec, Brakeman, ESLint security plugins, Psalm, gitleaks,
osv-scanner, pip-audit, npm audit, zizmor (GitHub Actions), Trivy, Checkov and hadolint. CodeQL is deliberately absent.
SpotBugs + FindSecBugs and MobSF need a build or a server, so they are recorded as not_run with
instructions; pass their SARIF to merge_findings.py if you run them.
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

SKIP_DIRS = {'node_modules', '.git', 'dist', 'build', '.venv', 'venv', '__pycache__', 'vendor', 'target', '.terraform'}
EXT = {'python': {'.py'}, 'javascript': {'.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.vue', '.svelte'},
       'go': {'.go'}, 'ruby': {'.rb'}, 'php': {'.php'}, 'java': {'.java', '.kt', '.kts'}, 'csharp': {'.cs'},
       'c': {'.c', '.h', '.cc', '.cpp'}, 'swift': {'.swift'}, 'terraform': {'.tf'}}

def inventory(root: Path) -> dict:
    langs: dict[str, int] = {}
    names: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and (not d.startswith('.') or d == '.github')]
        rel = Path(dirpath).relative_to(root).as_posix()
        for f in filenames:
            names.append(f'{rel}/{f}' if rel != '.' else f)
            for lang, exts in EXT.items():
                if Path(f).suffix in exts:
                    langs[lang] = langs.get(lang, 0) + 1
    base = {Path(n).name for n in names}
    return {
        'langs': langs, 'files': names,
        'requirements': [n for n in names if re.search(r'(^|/)requirements[\w.-]*\.txt$', n)],
        'pyproject': 'pyproject.toml' in base,
        'package_lock': [n for n in names if n.endswith('package-lock.json')],
        'other_js_locks': [n for n in names if n.endswith(('yarn.lock', 'pnpm-lock.yaml'))],
        'lockfiles': [n for n in names if Path(n).name in {'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'poetry.lock',
                      'Pipfile.lock', 'go.mod', 'Gemfile.lock', 'composer.lock', 'Cargo.lock', 'pom.xml', 'build.gradle',
                      'gradle.lockfile', 'pubspec.lock', 'packages.lock.json'} or re.search(r'requirements[\w.-]*\.txt$', n)],
        'dockerfiles': [n for n in names if Path(n).name in ('Dockerfile', 'Containerfile') or Path(n).name.startswith('Dockerfile.')],
        'iac': [n for n in names if n.endswith(('.tf', '.bicep')) or Path(n).name in ('Dockerfile',) or
                re.search(r'(^|/)(k8s|kubernetes|helm|charts|cloudformation)/', n)],
        'rails': 'Gemfile' in base and any(n.endswith('config/routes.rb') for n in names),
        'eslint_security': eslint_security_plugin(root),
        'psalm': (root / 'vendor' / 'bin' / 'psalm').exists(),
        'is_git': (root / '.git').exists(),
        'mobile_package': [n for n in names if n.endswith(('.apk', '.aab', '.ipa'))],
    }

def eslint_security_plugin(root: Path) -> bool:
    try:
        pkg = json.loads((root / 'package.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    deps = {**(pkg.get('dependencies') or {}), **(pkg.get('devDependencies') or {})}
    return any(d in deps for d in ('eslint-plugin-security', 'eslint-plugin-no-unsanitized', 'eslint-plugin-security-node'))

def version_of(exe: str, args=('--version',)) -> str | None:
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=60, env=tool_env(), encoding='utf-8', errors='replace')
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r'\d+\.\d+(\.\d+)?', (p.stdout or '') + ' ' + (p.stderr or ''))
    return m.group(0) if m else None

def tool_env() -> dict:
    env = dict(os.environ)
    env.update({'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8', 'SEMGREP_SEND_METRICS': 'off', 'NO_COLOR': '1'})
    return env

def plan(root: Path, out: Path, inv: dict, semgrep_configs: list[str]) -> list[dict]:
    """One entry per tool: {name, area, network, applies (reason if not), exe, runs: [{cmd, cwd, output, stdout_to}]}."""
    L = inv['langs']
    src = sum(L.values()) > 0
    tools = []
    def add(name, area, exe, applies, runs, network=False, not_run=None, note=None):
        tools.append({'name': name, 'area': area, 'exe': exe, 'applies': applies, 'runs': runs, 'network': network,
                      'not_run': not_run, 'note': note})
    cfg = [a for c in semgrep_configs for a in ('--config', c)]
    add('semgrep', 'sast', 'semgrep', True if src else 'no source files',
        [{'cmd': ['semgrep', 'scan', *cfg, '--metrics=off', '--disable-version-check', '--sarif', '--output', str(out / 'semgrep.sarif'), '--quiet', str(root)],
          'output': 'semgrep.sarif'}], network=any(c.startswith(('p/', 'r/')) for c in semgrep_configs))
    add('bandit', 'sast', 'bandit', True if L.get('python') else 'no Python files',
        [{'cmd': ['bandit', '-r', str(root), '-f', 'json', '-o', str(out / 'bandit.json'), '-q',
                  '-x', '*/tests/*,*/test/*,*/.venv/*,*/venv/*,*/node_modules/*'], 'output': 'bandit.json'}])
    add('gosec', 'sast', 'gosec', True if L.get('go') else 'no Go files',
        [{'cmd': ['gosec', '-fmt', 'sarif', '-out', str(out / 'gosec.sarif'), '-no-fail', './...'], 'cwd': str(root), 'output': 'gosec.sarif'}])
    add('brakeman', 'sast', 'brakeman', True if inv['rails'] else 'not a Rails application',
        [{'cmd': ['brakeman', '-q', '--no-pager', '--no-exit-on-warn', '-f', 'sarif', '-o', str(out / 'brakeman.sarif'), str(root)],
          'output': 'brakeman.sarif'}])
    js_applies = True if L.get('javascript') else 'no JavaScript/TypeScript files'
    add('eslint-security', 'sast', 'npx', js_applies,
        [{'cmd': ['npx', '--no-install', 'eslint', '-f', 'json', '-o', str(out / 'eslint.json'), '.'], 'cwd': str(root), 'output': 'eslint.json'}],
        not_run=None if inv['eslint_security'] else 'the repository has no ESLint security plugin (eslint-plugin-security / no-unsanitized); adding one changes the repository, so ask first',
        note="uses the repository's own ESLint config; security rules only report if that config enables them")
    add('psalm-taint', 'sast', str(root / 'vendor' / 'bin' / 'psalm'), True if L.get('php') else 'no PHP files',
        [{'cmd': [str(root / 'vendor' / 'bin' / 'psalm'), '--taint-analysis', '--no-progress', f"--report={out / 'psalm.sarif'}"],
          'cwd': str(root), 'output': 'psalm.sarif'}],
        not_run=None if inv['psalm'] else 'Psalm is not installed in vendor/ (composer require --dev vimeo/psalm changes the repository; ask first)')
    add('spotbugs-findsecbugs', 'sast', None, True if L.get('java') else 'no Java/Kotlin files', [],
        not_run='needs a compiled build: run SpotBugs with the FindSecBugs plugin in the project build and pass its SARIF to merge_findings.py')
    add('mobsf', 'mobile', None, True if inv['mobile_package'] else 'no .apk/.aab/.ipa package provided', [],
        not_run='needs a MobSF server; scan the package there and pass its SARIF/JSON findings to the agent')
    gl_args = ['git', str(root)] if inv['is_git'] else ['dir', str(root)]
    add('gitleaks', 'secrets', 'gitleaks', True,
        [{'cmd': ['gitleaks', *gl_args, '--report-format', 'sarif', '--report-path', str(out / 'gitleaks.sarif'), '--redact',
                  '--no-banner', '--exit-code', '0'], 'output': 'gitleaks.sarif',
          'fallback': ['gitleaks', 'detect', '--source', str(root), *([] if inv['is_git'] else ['--no-git']), '--report-format', 'sarif',
                       '--report-path', str(out / 'gitleaks.sarif'), '--redact', '--no-banner', '--exit-code', '0']}],
        note='scans git history too when the repository is a git checkout')
    add('osv-scanner', 'dependencies', 'osv-scanner', True if inv['lockfiles'] else 'no lockfile or manifest',
        [{'cmd': ['osv-scanner', 'scan', 'source', '-r', '--format', 'json', '--output', str(out / 'osv.json'), str(root)], 'output': 'osv.json',
          'fallback': ['osv-scanner', '--format', 'json', '--output', str(out / 'osv.json'), '-r', str(root)], 'ok_codes': [0, 1]}], network=True)
    pip_runs = [{'cmd': ['pip-audit', '-r', str(root / r), '-f', 'json', '-o', str(out / f"pip-audit{'' if i == 0 else '-' + str(i)}.json"),
                         '--progress-spinner', 'off'], 'output': f"pip-audit{'' if i == 0 else '-' + str(i)}.json", 'ok_codes': [0, 1]}
                for i, r in enumerate(inv['requirements'])]
    if not pip_runs and inv['pyproject']:
        pip_runs = [{'cmd': ['pip-audit', str(root), '-f', 'json', '-o', str(out / 'pip-audit.json'), '--progress-spinner', 'off'],
                     'output': 'pip-audit.json', 'ok_codes': [0, 1]}]
    add('pip-audit', 'dependencies', 'pip-audit', True if pip_runs else 'no requirements files or pyproject.toml', pip_runs, network=True)
    npm_runs = [{'cmd': ['npm', 'audit', '--json'], 'cwd': str(root / Path(lock).parent), 'stdout_to': f"npm-audit{'' if i == 0 else '-' + str(i)}.json",
                 'output': f"npm-audit{'' if i == 0 else '-' + str(i)}.json", 'ok_codes': [0, 1]} for i, lock in enumerate(inv['package_lock'])]
    add('npm-audit', 'dependencies', 'npm', True if npm_runs else ('yarn/pnpm lockfile only: use osv-scanner, or pnpm audit / yarn npm audit'
        if inv['other_js_locks'] else 'no package-lock.json'), npm_runs, network=True)
    workflows = [n for n in inv['files'] if n.startswith('.github/workflows/') and n.endswith(('.yml', '.yaml'))]
    add('zizmor', 'ci', 'zizmor', True if workflows else 'no GitHub Actions workflows',
        [{'cmd': ['zizmor', '--format', 'sarif', '--offline', str(root)], 'cwd': str(root), 'stdout_to': 'zizmor.sarif',
          'output': 'zizmor.sarif', 'ok_codes': [0, 10, 11, 12, 13, 14]}])
    add('trivy', 'iac_containers', 'trivy', True,
        [{'cmd': ['trivy', 'fs', '--scanners', 'vuln,misconfig,secret', '--format', 'sarif', '--output', str(out / 'trivy.sarif'),
                  '--quiet', str(root)], 'output': 'trivy.sarif'}], network=True)
    add('checkov', 'iac_containers', 'checkov', True if inv['iac'] else 'no IaC or container files',
        [{'cmd': ['checkov', '-d', str(root), '-o', 'sarif', '--output-file-path', str(out / 'checkov'), '--quiet', '--compact'],
          'output': 'checkov/results_sarif.sarif', 'ok_codes': [0, 1]}])
    add('hadolint', 'iac_containers', 'hadolint', True if inv['dockerfiles'] else 'no Dockerfiles',
        [{'cmd': ['hadolint', '-f', 'sarif', '--no-fail', str(root / d)], 'stdout_to': f'hadolint-{i}.sarif', 'output': f'hadolint-{i}.sarif'}
         for i, d in enumerate(inv['dockerfiles'])])
    return tools

def run_tool(t: dict, out: Path, timeout: int) -> dict:
    rec = {'name': t['name'], 'area': t['area'], 'network': t['network'], 'status': None, 'version': None,
           'commands': [], 'outputs': [], 'reason': None}
    if t.get('note'):
        rec['note'] = t['note']
    exe = shutil.which(t['exe']) if t['exe'] else None
    if t['exe'] and not exe and not Path(t['exe']).exists():
        rec.update(status='not_run', reason=f"{t['exe']} is not installed")
        return rec
    rec['version'] = version_of(exe or t['exe']) if t['exe'] not in ('npx',) else version_of('npx', ('eslint', '--version'))
    errors = []
    for r in t['runs']:
        cmds = [r['cmd']] + ([r['fallback']] if r.get('fallback') else [])
        for i, cmd in enumerate(cmds):
            cmd = [exe if (j == 0 and exe and cmd[0] == t['exe']) else c for j, c in enumerate(cmd)]
            (out / r['output']).parent.mkdir(parents=True, exist_ok=True)  # e.g. checkov/, only when it runs
            try:
                p = subprocess.run(cmd, cwd=r.get('cwd'), capture_output=True, text=True, timeout=timeout, env=tool_env(),
                                   encoding='utf-8', errors='replace')
            except subprocess.TimeoutExpired:
                errors.append(f'timed out after {timeout}s'); break
            except OSError as exc:
                errors.append(str(exc)); break
            if r.get('stdout_to'):
                (out / r['stdout_to']).write_text(p.stdout, encoding='utf-8')
            target = out / r['output']
            ok = target.exists() and target.stat().st_size > 0 and (p.returncode in r.get('ok_codes', [0]) or valid_json(target))
            rec['commands'].append(' '.join(cmd))
            if ok:
                rec['outputs'].append(str(target))
                break
            msg = (p.stderr or p.stdout or '').strip().splitlines()
            if i == len(cmds) - 1:
                errors.append(f"exit {p.returncode}: {(msg[-1] if msg else 'no output')[:300]}")
    if rec['outputs']:
        rec['status'] = 'ran'
        if errors:
            rec['reason'] = 'some runs failed: ' + '; '.join(errors)
    else:
        rec['status'], rec['reason'] = 'errored', '; '.join(errors) or 'no output produced'
    return rec

def valid_json(path: Path) -> bool:
    try:
        json.loads(path.read_text(encoding='utf-8'))
        return True
    except (OSError, ValueError):
        return False

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--out-dir', type=Path, required=True)
    ap.add_argument('--only', help='comma-separated tool names')
    ap.add_argument('--skip', help='comma-separated tool names')
    ap.add_argument('--offline', action='store_true', help='skip tools that need the network')
    ap.add_argument('--dry-run', action='store_true', help='print the plan without running anything')
    ap.add_argument('--timeout', type=int, default=900, help='seconds per tool run')
    ap.add_argument('--semgrep-config', nargs='*', default=['p/default', 'p/owasp-top-ten'])
    ap.add_argument('--path-prepend', nargs='*', default=[], help='directories to search for tools first (e.g. a venv)')
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')  # tool output can hold characters the console can't encode
    root = args.repo.resolve()
    out = args.out_dir.resolve()
    if not root.is_dir():
        print(f'Not a directory: {root}', file=sys.stderr)
        return 2
    if out == root or root in out.parents:
        print(f'--out-dir must be outside the repository ({out} is inside {root})', file=sys.stderr)
        return 2
    if args.path_prepend:
        os.environ['PATH'] = os.pathsep.join([str(Path(p).resolve()) for p in args.path_prepend] + [os.environ.get('PATH', '')])
    inv = inventory(root)
    tools = plan(root, out, inv, args.semgrep_config)
    only = set(args.only.split(',')) if args.only else None
    skip = set(args.skip.split(',')) if args.skip else set()
    if not args.dry_run:
        out.mkdir(parents=True, exist_ok=True)
    records = []
    for t in tools:
        if (only and t['name'] not in only) or t['name'] in skip:
            records.append({'name': t['name'], 'area': t['area'], 'status': 'not_run', 'reason': 'excluded by --only/--skip'}); continue
        if t['applies'] is not True:
            records.append({'name': t['name'], 'area': t['area'], 'status': 'not_applicable', 'reason': t['applies']}); continue
        if t['not_run']:
            records.append({'name': t['name'], 'area': t['area'], 'status': 'not_run', 'reason': t['not_run']}); continue
        if args.offline and t['network']:
            records.append({'name': t['name'], 'area': t['area'], 'status': 'not_run', 'reason': '--offline: this tool needs the network'}); continue
        if args.dry_run:
            exe = shutil.which(t['exe']) if t['exe'] else None
            records.append({'name': t['name'], 'area': t['area'], 'status': 'planned' if exe else 'not_run',
                            'reason': None if exe else f"{t['exe']} is not installed",
                            'commands': [' '.join(r['cmd']) for r in t['runs']]}); continue
        print(f"running {t['name']} ...", file=sys.stderr)
        records.append(run_tool(t, out, args.timeout))
    manifest = {'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'repository': str(root),
                'out_dir': str(out), 'languages': inv['langs'], 'tools': records}
    if args.dry_run:
        print(json.dumps(manifest, indent=2))
        return 0
    (out / 'scanners.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    width = max(len(r['name']) for r in records)
    for r in records:
        extra = r.get('version') or ''
        print(f"  {r['name']:<{width}}  {r['status']:<14} {extra:<10} {r.get('reason') or ''}")
    print(f"manifest -> {out / 'scanners.json'}")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
