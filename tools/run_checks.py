#!/usr/bin/env python3
"""Run the repository's checks: every runnable command in CLAUDE.md's ```bash blocks, one at a time.

Each command runs in a scratch copy of the working tree (tracked and untracked-but-not-ignored files, indexed in a
fresh git repository so tracked/untracked rules still hold), so nothing is left in the working tree. Files a command
writes are kept until its block ends (a later command in the same block may read them) and then removed; a command
that changes a committed file fails.

How a CLAUDE.md line becomes a command:
  - only lines starting with `python` or `node` are commands; `cd DIR` applies to the rest of its block;
  - optional `[..]` groups are dropped, except `[--check]`, which is kept as `--check`; `a|b` takes `a`;
  - a line with a <placeholder> or `...` is SKIPPED (it needs the user's input);
  - network fetchers run only with --check (or --from-file/--from-dir); scripts that need a running app, a device,
    real CI or a person (measure.py, rerun.py, capture-session, run_poc.py without --check, ...) are SKIPPED;
  - a bare input file (`evaluation.json`) that is neither in the repository nor written by an earlier command of the
    block is SKIPPED as a file the user supplies;
  - a command that fails from the repository root is retried from its skill folder (some record paths relative to it).
Commands needing an optional toolchain (node, pytest, a Playwright browser) run when it is present and are SKIPPED
with the reason otherwise; --require makes a missing toolchain a failure. Every SKIP has a reason, and lines in a
command's output that report a check it skipped inside (no Go, no browser, ...) are listed in the summary.

    python tools/run_checks.py [--only PATTERN] [--skip-browser] [--json out.json] [--markdown out.md]
    python tools/run_checks.py --list
    python tools/run_checks.py --browser-only --require browser,node
    python tools/run_checks.py --shard 2/4
    python tools/run_checks.py --self-test

Exit status: 0 when nothing failed (skips allowed), 1 when a command failed, 2 when nothing was selected.
"""
from __future__ import annotations
import argparse, hashlib, importlib.util, json, os, platform, re, shlex, shutil, signal, subprocess, sys, tempfile, time
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKILL_RE = re.compile(r'(?<![\w-])([a-z0-9][a-z0-9-]*-skill)/')
PLACEHOLDER_RE = re.compile(r'<[^<>]+>')
OPTIONAL_RE = re.compile(r'\s*\[(?!--check\])[^\[\]]*\]')
ALT_RE = re.compile(r'^[\w.-]+(\|[\w.-]+)+$')
BARE_INPUT_RE = re.compile(r'^[\w.-]+\.(json|md|log|xml|diff|txt|ya?ml|csv|out|go|sarif)$')
OUTPUT_FLAGS = {'--out', '--out-dir', '--markdown', '--sarif', '--defects-md', '--csv', '--skeleton', '--write-lock',
                '--keep'}
OFFLINE_FETCH_FLAGS = {'--check', '--from-file', '--from-dir'}

# scripts that need something a check run does not have; they run only for --self-test (or --check where noted)
SERVICE_SCRIPTS = {
    'measure.py': 'drives real browsers against a served site and records lab timings',
    'rerun.py': 'starts real CI jobs',
    'run_scanners.py': 'runs external scanners and downloads advisories',
    'run_audit.py': 'drives a browser against a served site',
    'run_resilience.py': 'drives a browser against a served app',
    'reproduce.py': 'drives browsers against a served app',
    'minimise.py': 'drives browsers against a served app',
    'capture.py': 'drives browsers against served sites',
    'capture_flows.py': 'drives a browser against a served app',
    'render_locales.py': 'drives a browser against a served site',
    'strict_overlay.py': 'needs the app built (npm install)',
    'run_mobile_audit.py': 'needs an Android emulator or device',
    'explore.py': 'needs an Android emulator or device',
    'crash_explore.py': 'needs an Android emulator or device',
    'adb_device.py': 'needs an Android emulator or device',
    'dynamic_check.py': 'needs PostgreSQL (Docker, Podman or loopback)',
    'run_pvt.py': 'probes a running deployment',
    'run_suite.py': 'triggers CI pipelines',
    'run_poc.py': 'needs the sample app running (only --check runs)',
    'probe_web.py': 'needs the built app served (only --check runs)',
}
CHECK_IS_OFFLINE = {'run_poc.py', 'probe_web.py'}
SERVICE_TOKENS = {'capture-session': 'opens a visible browser for a person to sign in'}
REPAIR = {('check_shared.py', '--fix'): 'repairs the skill copies (writes); the check is the same command without --fix'}

# seconds per CLAUDE.md block (named by its skill) in a full local run, used only to balance --shard; a block not
# listed counts as 30. The Go self-tests make the mutation and impact blocks longer where go is installed.
WEIGHTS = {
    'mutation-tester-skill': 420, 'shared': 180, 'resilience-prober-skill': 130, 'ui-crash-hunter-skill': 120,
    'ci-failure-triager-skill': 115, 'test-impact-selector-skill': 100, 'privacy-flow-auditor-skill': 100,
    'wcag-auditor-skill': 65, 'risk-based-test-planner-skill': 45, 'cross-browser-checker-skill': 40,
    'code-security-auditor-skill': 40, 'db-migration-reviewer-skill': 30, 'api-contract-tester-skill': 30,
    'flaky-test-hunter-skill': 30, 'performance-budget-auditor-skill': 25, 'test-suite-evaluator-skill': 25,
    'i18n-checker-skill': 20, 'dead-test-finder-skill': 20, 'test-data-auditor-skill': 12, 'journey-mapper-skill': 10,
    'mobile-app-auditor-skill': 10, 'test-case-designer-skill': 7, 'ticket-readiness-reviewer-skill': 6,
    'journey-to-tests-skill': 6, 'ci-suite-runner-skill': 6, 'bug-reproducer-skill': 4,
    'visual-regression-reviewer-skill': 4, 'exploratory-charter-writer-skill': 4, 'qa-orchestrator-skill': 4,
    'post-deploy-validator-skill': 3, 'ticket-to-tdd-skill': 3, 'requirements-traceability-auditor-skill': 2,
    'hypercare-anomaly-watcher-skill': 2, 'release-readiness-gate-skill': 2,
}

TAGS = ('node', 'pytest', 'browser', 'downloads')
# fetchers of pinned downloads (kept in ~/.cache/qa-skills, not committed): their --check fails until they have run,
# so it needs the downloads; fetch_cldr.py checks committed data and needs nothing
DOWNLOAD_FETCHERS = {'fetch_engines.py', 'fetch_assets.py', 'fetch_data.py', 'fetch_lists.py'}
# scripts that stop without a skill's downloads -> that skill's fetcher
DOWNLOAD_USERS = {'browserslist_lite.py': 'fetch_data.py'}
# a self-test's own skip: "SKIP name: why", "name: skipped (why)", "... not on PATH", "N skipped" in its tally
# any command: a check left out because a download is missing ("caniuse data not in the cache; ... not rechecked")
NOT_CACHED_RE = re.compile(r'\bnot in the cache\b')
INTERNAL_SKIP_RE = re.compile(r'^SKIP\b|\bskipped \(|\bnot on PATH\b|\bnot installed here\b|^(?!R-\d).*\b[1-9]\d* skipped\b')


@dataclass
class Command:
    id: int
    block: int
    line: str              # the line as written in CLAUDE.md
    cwd: str               # relative to the repository root ('' for the root)
    argv: list = field(default_factory=list)
    skill: str = ''
    skip: str | None = None
    needs: list = field(default_factory=list)
    fetcher: str = ''      # for needs 'downloads': the fetcher whose --check says the pinned files are cached

    @property
    def text(self) -> str:
        return ' '.join(shlex.quote(a) for a in self.argv) if self.argv else self.line

    @property
    def script(self) -> str:
        return self.argv[1] if len(self.argv) > 1 and self.argv[0] in ('python', 'node') else ''


# ---------------------------------------------------------------- parsing

def _script_source(root: Path, cwd: str, script: str) -> str:
    try:
        return (root / cwd / script).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def classify(c: Command, root: Path) -> None:
    """Set c.skip (a reason) and c.needs from the command alone."""
    argv = c.argv
    name = Path(c.script).name if c.script else ''
    kept = OPTIONAL_RE.sub('', c.line)      # a placeholder inside a dropped [..] group does not count
    ph = PLACEHOLDER_RE.search(kept)
    if not ph and any(a == '...' or a.endswith(',...') for a in argv):
        ph = re.search(r'\S*\.\.\.', kept)
    if ph:
        c.skip = f"placeholder: needs the user's input ({ph.group(0)})"
        return
    if not argv or argv[0] not in ('python', 'node'):
        c.skip = 'not a python or node command'
        return
    for (script, flag), why in REPAIR.items():
        if name == script and flag in argv:
            c.skip = why
            return
    for tok, why in SERVICE_TOKENS.items():
        if tok in argv:
            c.skip = why
            return
    self_test = '--self-test' in argv or name == 'self_test.py'
    if name == 'run_checks.py' and not self_test:
        c.skip = 'the check runner itself'
        return
    if re.fullmatch(r'fetch_\w+\.py', name) and not (OFFLINE_FETCH_FLAGS & set(argv)) and not self_test:
        c.skip = 'downloads (network); fetchers run only with --check'
        return
    if name in SERVICE_SCRIPTS and not self_test and not (name in CHECK_IS_OFFLINE and '--check' in argv):
        c.skip = SERVICE_SCRIPTS[name]
        return
    needs = set()
    if argv[0] == 'node':
        needs.add('node')
    if ('--preset' in argv and argv[argv.index('--preset') + 1:argv.index('--preset') + 2] == ['pytest']) \
            or argv[1:3] == ['-m', 'pytest']:
        needs.add('pytest')
    if '--browser' in argv:
        needs.add('browser')
    elif name.endswith('.py'):
        src = _script_source(root, c.cwd, c.script)
        if re.search(r'^\s*(from playwright\b|import playwright\b)', src, re.M):
            needs.add('browser')
    if name in DOWNLOAD_FETCHERS or name in DOWNLOAD_USERS:
        needs.add('downloads')
        c.fetcher = str(Path(c.script).with_name(DOWNLOAD_USERS.get(name, name))).replace(os.sep, '/')
    c.needs = sorted(needs)


def normalise(line: str) -> list | None:
    line = OPTIONAL_RE.sub('', line).replace('[--check]', '--check')
    try:
        argv = shlex.split(line, comments=True)
    except ValueError:
        return None
    return [a.split('|')[0] if ALT_RE.match(a) else a for a in argv]


def parse_claude_md(text: str, root: Path = ROOT) -> list:
    cmds = []
    for b, block in enumerate(re.findall(r'```bash\n(.*?)```', text, re.S)):
        cwd = ''
        for raw in block.splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('cd '):
                argv = shlex.split(line, comments=True)
                cwd = argv[1] if len(argv) > 1 else ''
                continue
            argv = normalise(line) or []
            c = Command(id=len(cmds) + 1, block=b, line=line, cwd=cwd, argv=argv)
            m = SKILL_RE.search(line)
            c.skill = cwd or (m.group(1) if m else ('shared' if 'shared/' in line else
                                                     'tools' if 'tools/' in line else ''))
            classify(c, root)
            twin = next((o for o in cmds if c.argv and o.argv == c.argv and o.cwd == c.cwd), None)
            if twin and not c.skip:
                c.skip = f'same command as #{twin.id}'
            cmds.append(c)
    return cmds


def bare_inputs(argv: list) -> list:
    """Bare file names the command reads (not the values of output flags)."""
    out = []
    for i, a in enumerate(argv[2:], start=2):
        if argv[i - 1] in OUTPUT_FLAGS or a.startswith('-'):
            continue
        if BARE_INPUT_RE.match(a):
            out.append(a)
    return out


# ---------------------------------------------------------------- toolchains

_TOOL_CACHE: dict = {}


def tool_status(tag: str, python: str = sys.executable, fetcher: str = '', root: Path = ROOT) -> tuple:
    key = f'{tag}:{fetcher}' if fetcher else tag
    if key in _TOOL_CACHE:
        return _TOOL_CACHE[key]
    if tag == 'downloads':
        try:
            r = subprocess.run([python, fetcher, '--check'], cwd=root, capture_output=True, timeout=300)
            res = (True, '') if r.returncode == 0 else \
                (False, f'the pinned downloads are not in the cache (run python {fetcher} once)')
        except (OSError, subprocess.TimeoutExpired) as e:
            res = (False, f'{fetcher} --check could not run ({type(e).__name__})')
    elif tag == 'node':
        res = (True, '') if shutil.which('node') else (False, 'node is not installed')
    elif tag == 'pytest':
        res = (True, '') if importlib.util.find_spec('pytest') else (False, 'pytest is not installed')
    elif tag == 'browser':
        if not importlib.util.find_spec('playwright'):
            res = (False, 'Playwright is not installed (pip install playwright)')
        else:
            code = ('from playwright.sync_api import sync_playwright\n'
                    'with sync_playwright() as p: print(p.chromium.executable_path)')
            try:
                r = subprocess.run([python, '-c', code], capture_output=True, text=True, timeout=120)
                exe = (r.stdout.strip().splitlines() or [''])[-1]
                res = (True, '') if r.returncode == 0 and exe and Path(exe).exists() else \
                    (False, 'Playwright Chromium is not installed (python -m playwright install chromium)')
            except (OSError, subprocess.TimeoutExpired) as e:
                res = (False, f'Playwright could not be started ({type(e).__name__})')
    else:
        res = (False, f'unknown toolchain {tag}')
    _TOOL_CACHE[key] = res
    return res


# ---------------------------------------------------------------- scratch tree

def _git(args, cwd, **kw):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, **kw)


def source_files(root: Path) -> list:
    """Tracked and untracked-but-not-ignored files (what a checkout plus local work holds)."""
    if shutil.which('git') and (root / '.git').exists():
        r = _git(['ls-files', '-z', '--cached', '--others', '--exclude-standard'], root)
        if r.returncode == 0:
            return sorted({p for p in r.stdout.decode('utf-8', 'replace').split('\0') if p and (root / p).is_file()})
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x != '.git']
        out += [os.path.relpath(os.path.join(d, f), root).replace(os.sep, '/') for f in files]
    return sorted(out)


def make_scratch(root: Path, dest: Path) -> dict:
    files = source_files(root)
    for rel in files:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, target)
    git = False
    if shutil.which('git'):
        ok = _git(['init', '-q'], dest).returncode == 0
        ok = ok and _git(['-c', 'core.autocrlf=false', 'add', '-A'], dest).returncode == 0
        git = bool(ok)
    return {'files': len(files), 'git_index': git}


def snapshot(base: Path) -> dict:
    snap = {}
    for d, dirs, files in os.walk(base):
        dirs[:] = [x for x in dirs if x != '.git']
        for f in files:
            p = os.path.join(d, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            snap[os.path.relpath(p, base).replace(os.sep, '/')] = (st.st_size, st.st_mtime_ns)
    return snap


def _same_text(a: Path, b: Path) -> bool:
    try:
        x, y = a.read_bytes(), b.read_bytes()
    except OSError:
        return False
    return x == y or x.replace(b'\r\n', b'\n') == y.replace(b'\r\n', b'\n')


def changes(before: dict, after: dict, base: dict, scratch: Path, root: Path) -> tuple:
    created = sorted(p for p in after if p not in before and p not in base)
    changed = []
    for p, sig in base.items():
        if p not in after:
            changed.append(p + ' (deleted)')
        elif after[p] != sig and not _same_text(scratch / p, root / p):
            changed.append(p)
    return created, sorted(changed)


def _rm_tree(path: Path) -> None:
    def onerr(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=onerr)
    else:
        shutil.rmtree(path, onerror=onerr)


def restore(scratch: Path, root: Path, base: dict) -> None:
    """Remove what the block wrote and put back every committed file it changed."""
    now = snapshot(scratch)
    for p in now:
        if p not in base:
            try:
                os.remove(scratch / p)
            except OSError:
                pass
    for p, sig in base.items():
        if now.get(p) != sig:
            (scratch / p).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / p, scratch / p)
    base_dirs = {str(Path(p).parent).replace(os.sep, '/') for p in base}
    base_dirs |= {'/'.join(d.split('/')[:i]) for d in base_dirs for i in range(1, d.count('/') + 1)}
    for d, dirs, _files in os.walk(scratch, topdown=False):
        if '.git' in Path(d).relative_to(scratch).parts:
            continue
        rel = os.path.relpath(d, scratch).replace(os.sep, '/')
        if rel != '.' and rel not in base_dirs:
            _rm_tree(Path(d)) if Path(d).exists() else None


# ---------------------------------------------------------------- running

def _kill_tree(p: subprocess.Popen) -> None:
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(p.pid)], capture_output=True)
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        p.kill()
    except OSError:
        pass


def execute(argv: list, cwd: Path, timeout: float, env: dict) -> tuple:
    exe = list(argv)
    if exe[0] == 'python':
        exe[0] = sys.executable
    elif exe[0] == 'node':
        exe[0] = shutil.which('node') or 'node'
    kw = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    t = time.time()
    try:
        p = subprocess.Popen(exe, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             env=env, **kw)
    except OSError as e:
        return 127, f'could not start: {e}', 0.0, False
    try:
        out, _ = p.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        out, _ = p.communicate()
        timed_out = True
    return (p.returncode if not timed_out else -9), out.decode('utf-8', 'replace'), time.time() - t, timed_out


def internal_skips(output: str, self_test: bool) -> list:
    """Lines in which a self-test says it skipped a check of its own (no Go, no browser), and lines in which any
    command says it left a check out because a download is not cached. Other output of other commands (reports that
    quote 'not installed' tools, test-run tallies) is not read."""
    keep = []
    for ln in output.splitlines():
        s = ln.strip()
        if not s or len(s) > 300:
            continue
        if (self_test and INTERNAL_SKIP_RE.search(s)) or NOT_CACHED_RE.search(s):
            keep.append(s)
    return keep[:25]


def run_command(c: Command, scratch: Path, root: Path, base: dict, opts) -> dict:
    res = {'id': c.id, 'block': c.block, 'skill': c.skill, 'command': c.line, 'argv': c.argv, 'cwd': c.cwd or '.',
           'needs': c.needs, 'status': 'skipped', 'reason': c.skip}
    if c.skip:
        return res
    for tag in c.needs:
        if tag == 'browser' and opts.skip_browser:
            res['reason'] = '--skip-browser (the browser job runs it)'
            return res
        ok, why = tool_status(tag, fetcher=c.fetcher if tag == 'downloads' else '', root=scratch)
        if not ok:
            res['reason'] = why
            if tag in opts.require:
                res['status'] = 'failed'
                res['reason'] = f'required toolchain missing: {why}'
            return res
    cwd = scratch / c.cwd
    missing = [f for f in bare_inputs(c.argv) if not (cwd / f).exists()]
    if missing:
        res['reason'] = (f'needs {", ".join(missing)}, a file the user supplies (not in the repository and not '
                         'written by an earlier command of its block)')
        return res
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    before = snapshot(scratch)
    rc, out, secs, timed_out = execute(c.argv, cwd, opts.timeout, env)
    ran_from = c.cwd or '.'
    if rc != 0 and not c.cwd and not timed_out:
        m = SKILL_RE.search(c.line)
        if m and (scratch / m.group(1)).is_dir():
            pre = m.group(1) + '/'
            argv2 = [a.replace(pre, '') if pre in a else a for a in c.argv]
            rc2, out2, secs2, to2 = execute(argv2, scratch / m.group(1), opts.timeout, env)
            if rc2 == 0:
                rc, out, ran_from, timed_out = rc2, out2, m.group(1), to2
                secs += secs2
                res['note'] = f'failed from the repository root; passed from {m.group(1)}/'
            else:
                out = out + f'\n--- retried from {m.group(1)}/ (rc={rc2}) ---\n' + out2
    created, changed = changes(before, snapshot(scratch), base, scratch, root)
    for p in changed:   # put committed files back at once, so later commands of the block see them as committed
        rel = p[:-len(' (deleted)')] if p.endswith(' (deleted)') else p
        (scratch / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, scratch / rel)
    res.update(rc=rc, seconds=round(secs, 1), ran_from=ran_from, outputs=created[:50], changed_files=changed,
               internal_skips=internal_skips(out, '--self-test' in c.argv or Path(c.script).name == 'self_test.py'))
    lines = out.rstrip().splitlines()
    if timed_out:
        res.update(status='failed', reason=f'timed out after {opts.timeout:.0f} s')
    elif rc != 0:
        res.update(status='failed', reason=f'exit status {rc}')
    elif changed:
        res.update(status='failed', reason='changed committed files: ' + ', '.join(changed[:10]))
    else:
        res.update(status='passed', reason=None)
    if res['status'] == 'failed':
        res['tail'] = lines[-60:]
        # the lines that say what failed, wherever they are (a self-test lists them before its summary)
        res['failures'] = [l for l in lines if FAILURE_LINE.search(l)][:40]
    return res


FAILURE_LINE = re.compile(r'^\s*(FAIL\b|FAILED\b|ERROR\b|Traceback \(most recent call last\)|AssertionError|\w+Error:)')


def block_skill(cmds: list, block: int) -> str:
    """The skill most commands of a block belong to (the block's name for WEIGHTS)."""
    names = [c.skill for c in cmds if c.block == block and c.skill]
    return max(sorted(set(names)), key=names.count) if names else f'block-{block}'


def shard_of(cmds: list, k: int, n: int) -> set:
    """Blocks for shard k of n: heaviest first onto the lightest shard (deterministic). Every block is in exactly one
    shard, including blocks WEIGHTS does not know."""
    blocks = sorted({c.block for c in cmds})
    weight = {b: WEIGHTS.get(block_skill(cmds, b), 30) for b in blocks}
    load = [0.0] * n
    owner = {}
    for b in sorted(blocks, key=lambda b: (-weight[b], b)):
        i = min(range(n), key=lambda j: (load[j], j))
        load[i] += weight[b]
        owner[b] = i
    return {b for b, i in owner.items() if i == k - 1}


def summarise(results: list, meta: dict) -> dict:
    counts = {s: sum(r['status'] == s for r in results) for s in ('passed', 'failed', 'skipped')}
    return {'tool': 'tools/run_checks.py', **meta, 'counts': counts, 'commands': results}


def markdown(summary: dict) -> str:
    c = summary['counts']
    out = [f"## Repository checks: {c['passed']} passed, {c['failed']} failed, {c['skipped']} skipped", '',
           f"Python {summary.get('python')} on {summary.get('platform')}; {summary.get('selected')} commands selected"
           f" in {summary.get('seconds')} s.", '']
    fails = [r for r in summary['commands'] if r['status'] == 'failed']
    if fails:
        out += ['### Failed', '']
        for r in fails:
            out += [f"- `{r['command']}` ({r['cwd']}): {r['reason']}"]
            if r.get('failures'):
                out += ['', '  Failures:', '', '  ```', *['  ' + t for t in r['failures']], '  ```']
            if r.get('tail'):
                out += ['', '  ```', *['  ' + t for t in r['tail'][-25:]], '  ```', '']
    skips = {}
    for r in summary['commands']:
        if r['status'] == 'skipped':
            skips.setdefault(r['reason'], []).append(r['command'])
    if skips:
        out += ['', '### Skipped', '']
        for why, lst in sorted(skips.items(), key=lambda kv: -len(kv[1])):
            out += [f'- {why} ({len(lst)}):'] + [f'  - `{x}`' for x in lst]
    inner = [(r['command'], s) for r in summary['commands'] for s in r.get('internal_skips') or []]
    if inner:
        out += ['', '### Checks skipped inside commands that passed or failed', '']
        out += [f'- `{cmd}`: {s}' for cmd, s in inner]
    notes = [r for r in summary['commands'] if r.get('note')]
    if notes:
        out += ['', '### Notes', ''] + [f"- `{r['command']}`: {r['note']}" for r in notes]
    return '\n'.join(out) + '\n'


def run_all(cmds: list, root: Path, opts) -> dict:
    t0 = time.time()
    parent = Path(opts.scratch) if opts.scratch else None
    if parent:
        parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix='qa-checks-', dir=parent))
    status_before = _git(['status', '--porcelain'], root).stdout if (root / '.git').exists() and shutil.which('git') else None
    results = []
    try:
        info = make_scratch(root, scratch)
        base = snapshot(scratch)
        last_block = None
        for c in cmds:
            if last_block is not None and c.block != last_block:
                restore(scratch, root, base)
            last_block = c.block
            r = run_command(c, scratch, root, base, opts)
            results.append(r)
            if not opts.quiet:
                extra = f" {r.get('seconds', 0):6.1f}s" if r['status'] != 'skipped' else ' ' * 8
                why = f" :: {r['reason']}" if r.get('reason') else ''
                print(f"{r['status'].upper():8}{extra} [{r['cwd']}] {c.line}{why}", flush=True)
                if r['status'] == 'failed' and r.get('failures'):
                    print('\n'.join('    ! ' + t for t in r['failures']), flush=True)
                if r['status'] == 'failed' and r.get('tail'):
                    print('\n'.join('    | ' + t for t in r['tail'][-15:]), flush=True)
        restore(scratch, root, base)
    finally:
        if opts.keep_scratch:
            print(f'scratch copy kept at {scratch}')
        else:
            _rm_tree(scratch)
    if status_before is not None:
        status_after = _git(['status', '--porcelain'], root).stdout
        if status_after != status_before:
            results.append({'id': 0, 'block': -1, 'skill': '', 'command': 'working tree unchanged', 'argv': [],
                            'cwd': '.', 'needs': [], 'status': 'failed',
                            'reason': 'the working tree changed while the checks ran: '
                                      + status_after.decode('utf-8', 'replace')[:500]})
    meta = {'python': platform.python_version(), 'platform': platform.platform(terse=True),
            'selected': len(cmds), 'seconds': round(time.time() - t0, 1), 'scratch_files': info.get('files'),
            'scratch_git_index': info.get('git_index')}
    return summarise(results, meta)


def select(cmds: list, opts) -> list:
    out = cmds
    if opts.only:
        rx = re.compile(opts.only)
        out = [c for c in out if rx.search(c.line) or rx.search(c.skill)]
    if opts.browser_only:
        out = [c for c in out if 'browser' in c.needs]
    if opts.shard:
        k, n = (int(x) for x in opts.shard.split('/'))
        blocks = shard_of(cmds, k, n)
        out = [c for c in out if c.block in blocks]
    return out


# ---------------------------------------------------------------- self-test

SAMPLE = '''
```bash
python a-skill/scripts/x.py --self-test
python a-skill/scripts/x.py <repo> --out y.json
python a-skill/scripts/fetch_data.py [--check]
python a-skill/scripts/fetch_data.py
cd b-skill   # relative
python scripts/v.py examples/r.json [--brief] [--sarif out.sarif]
```

```bash
python a-skill/scripts/render.py e.json --color always|never
node shared/scripts/clock_shift.mjs --self-test
python tools/check_shared.py --fix  # repair
python tools/check_shared.py
python a-skill/scripts/measure.py --plan p.json
python a-skill/scripts/measure.py --self-test
python a-skill/scripts/run_poc.py a-skill/examples/pocs/x.json --check
python a-skill/scripts/residue.py snapshot|diff ...
python r-skill/scripts/self_test.py --browser [PORT]
python f-skill/scripts/run_tests.py --preset pytest --cwd f-skill/examples/pkg --out runs.json
python a-skill/scripts/fetch_ticket.py --from-file a-skill/examples/T.json --out ticket.json
python a-skill/scripts/validate.py a-skill/examples/map.json [--repo <repo>] [--strict]
python a-skill/scripts/x.py --self-test
python tools/run_checks.py [--only PATTERN] [--skip-browser]
python tools/run_checks.py --self-test
python c-skill/scripts/browserslist_lite.py --repo c-skill/examples/site
python i-skill/scripts/fetch_cldr.py --check
```
'''

WRITER = '''import sys, pathlib
args = sys.argv[1:]
if '--out' in args:
    pathlib.Path(args[args.index('--out') + 1]).write_text('{}')
if '--touch' in args:
    pathlib.Path(args[args.index('--touch') + 1]).write_text('changed')
if '--same' in args:
    p = pathlib.Path(args[args.index('--same') + 1]); p.write_text(p.read_text())
if '--need-skill-cwd' in args and not pathlib.Path('scripts').is_dir():
    sys.exit(3)
if '--sleep' in args:
    import time; time.sleep(30)
if '--fail' in args:
    print('boom'); sys.exit(1)
if '--fail-early' in args:
    print('FAIL  early-check: the value was wrong')
    for i in range(100):
        print(f'ok    later check {i}')
    print('1 failure(s)'); sys.exit(1)
print('SKIP the Go checks (go not installed)')
'''
READER = '''import sys, pathlib
sys.exit(0 if pathlib.Path(sys.argv[1]).is_file() else 4)
'''
FAKE_MD = '''
```bash
python a-skill/scripts/w.py --out o.json
python a-skill/scripts/w.py --self-test
python a-skill/scripts/r.py o.json
python a-skill/scripts/r.py missing.json
python a-skill/scripts/w.py --touch a-skill/data.txt
python a-skill/scripts/w.py --same a-skill/data.txt
python a-skill/scripts/w.py --need-skill-cwd
python a-skill/scripts/w.py --fail
python a-skill/scripts/w.py --fail-early
python a-skill/scripts/w.py --sleep
node a-skill/scripts/n.mjs
```

```bash
python a-skill/scripts/r.py o.json
cd a-skill
python scripts/r.py data.txt
```
'''


def self_test() -> int:
    checks = []

    def check(name, ok, extra=''):
        checks.append((name, bool(ok), extra))

    cmds = parse_claude_md(SAMPLE, root=Path(tempfile.gettempdir()) / 'no-such-repo')
    by = {c.line: c for c in cmds}
    g = lambda prefix: next((c for c in cmds if c.line.startswith(prefix)), Command(0, 0, '', ''))  # noqa: E731
    check('parses every python/node line and no cd line', len(cmds) == 22 and not any(c.line.startswith('cd ') for c in cmds),
          [c.line for c in cmds])
    check('a self-test runs', g('python a-skill/scripts/x.py --self-test').skip is None)
    check('a command with a <placeholder> is skipped', 'placeholder' in (g('python a-skill/scripts/x.py <repo>').skip or ''))
    c = g('python a-skill/scripts/fetch_data.py [--check]')
    check('a fetcher with [--check] runs with --check', c.skip is None and c.argv[-1] == '--check', c.argv)
    check('a fetcher without --check is skipped as a download',
          'download' in (by.get('python a-skill/scripts/fetch_data.py', c).skip or ''))
    c = g('python scripts/v.py')
    check('a cd line applies to the commands after it, and optional groups are dropped',
          c.cwd == 'b-skill' and c.argv == ['python', 'scripts/v.py', 'examples/r.json'] and c.skip is None, (c.cwd, c.argv))
    check('a|b takes a', g('python a-skill/scripts/render.py').argv[-1] == 'always', g('python a-skill/scripts/render.py').argv)
    check('a node command needs node', g('node shared').needs == ['node'] and g('node shared').skip is None)
    check('check_shared --fix is skipped, check_shared runs',
          'repairs' in (g('python tools/check_shared.py --fix').skip or '')
          and by.get('python tools/check_shared.py') is not None and by['python tools/check_shared.py'].skip is None)
    check('a script that needs a served site is skipped, its --self-test runs',
          'browser' in (g('python a-skill/scripts/measure.py --plan').skip or '')
          and g('python a-skill/scripts/measure.py --self-test').skip is None)
    check('run_poc.py --check runs', g('python a-skill/scripts/run_poc.py').skip is None)
    check('an elided command (...) is skipped', 'placeholder' in (g('python a-skill/scripts/residue.py').skip or ''))
    c = g('python r-skill/scripts/self_test.py --browser')
    check('--browser needs a browser, and its optional [PORT] is dropped', c.needs == ['browser'] and c.argv[-1] == '--browser',
          (c.needs, c.argv))
    check('--preset pytest needs pytest', g('python f-skill/scripts/run_tests.py').needs == ['pytest'])
    check('fetch_ticket --from-file is offline and runs', g('python a-skill/scripts/fetch_ticket.py').skip is None)
    c = g('python a-skill/scripts/validate.py')
    check('a placeholder inside a dropped optional group does not skip the command',
          c.skip is None and c.argv == ['python', 'a-skill/scripts/validate.py', 'a-skill/examples/map.json'], (c.skip, c.argv))
    check('a command listed twice runs once', 'same command as #1' in (cmds[-5].skip or ''), cmds[-5].skip)
    check('the runner does not run itself, but its self-test runs',
          'runner itself' in (g('python tools/run_checks.py [--only').skip or '')
          and g('python tools/run_checks.py --self-test').skip is None)
    real = parse_claude_md(Path(ROOT / 'CLAUDE.md').read_text(encoding='utf-8'))
    shards = [shard_of(real, k, 4) for k in range(1, 5)]
    every = {c.block for c in real}
    check('--shard K/4 puts every CLAUDE.md block in exactly one shard',
          sum(len(s) for s in shards) == len(every) and set().union(*shards) == every, [sorted(s) for s in shards])
    loads = [sum(WEIGHTS.get(block_skill(real, b), 30) for b in s) for s in shards]
    check('the shards are balanced within the heaviest block', max(loads) - min(loads) <= max(WEIGHTS.values(), default=0),
          loads)
    c = g('python a-skill/scripts/fetch_data.py [--check]')
    check('a fetcher --check needs its downloads', c.needs == ['downloads'] and c.fetcher == 'a-skill/scripts/fetch_data.py',
          (c.needs, c.fetcher))
    c = g('python c-skill/scripts/browserslist_lite.py')
    check('browserslist_lite needs the cross-browser data', c.needs == ['downloads']
          and c.fetcher == 'c-skill/scripts/fetch_data.py', (c.needs, c.fetcher))
    check('fetch_cldr --check (committed data) needs nothing', g('python i-skill/scripts/fetch_cldr.py').needs == [])
    check('bare inputs ignore output flags', bare_inputs(['python', 's.py', 'in.json', '--out', 'o.json']) == ['in.json'])

    # end to end on a throwaway repository: chaining, cleanup, committed-file changes, retries, skips, timeouts
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / 'repo'
        (root / 'a-skill' / 'scripts').mkdir(parents=True)
        (root / 'a-skill' / 'scripts' / 'w.py').write_text(WRITER, encoding='utf-8')
        (root / 'a-skill' / 'scripts' / 'r.py').write_text(READER, encoding='utf-8')
        (root / 'a-skill' / 'scripts' / 'n.mjs').write_text('process.exit(0)\n', encoding='utf-8')
        (root / 'a-skill' / 'data.txt').write_text('committed\n', encoding='utf-8')
        (root / 'CLAUDE.md').write_text(FAKE_MD, encoding='utf-8')
        cmds = parse_claude_md(FAKE_MD, root=root)
        before = sorted(p.relative_to(root).as_posix() for p in root.rglob('*'))
        opts = argparse.Namespace(skip_browser=False, require=set(), timeout=3.0, scratch=str(Path(d) / 'scratch'),
                                  keep_scratch=False, quiet=True)
        _TOOL_CACHE['node'] = (False, 'node is not installed')
        try:
            s = run_all(cmds, root, opts)
            _TOOL_CACHE['node'] = (False, 'node is not installed')
            s2 = run_all([c for c in cmds if c.argv[0] == 'node'], root,
                         argparse.Namespace(**{**vars(opts), 'require': {'node'}}))
        finally:
            _TOOL_CACHE.pop('node', None)
        r = {}
        for x in s['commands']:
            r.setdefault(x['command'], x)
        st = lambda line: (r.get(line) or {}).get('status')  # noqa: E731
        check('a command writing an output passes, and records the output',
              st('python a-skill/scripts/w.py --out o.json') == 'passed'
              and r['python a-skill/scripts/w.py --out o.json'].get('outputs') == ['o.json'],
              r.get('python a-skill/scripts/w.py --out o.json'))
        check('a later command of the same block reads that output', st('python a-skill/scripts/r.py o.json') == 'passed')
        check('a bare input nobody wrote is skipped as user-supplied',
              st('python a-skill/scripts/r.py missing.json') == 'skipped'
              and 'user supplies' in (r['python a-skill/scripts/r.py missing.json']['reason'] or ''))
        check('changing a committed file fails', st('python a-skill/scripts/w.py --touch a-skill/data.txt') == 'failed'
              and 'a-skill/data.txt' in r['python a-skill/scripts/w.py --touch a-skill/data.txt']['reason'])
        check('rewriting a committed file with the same content passes', st('python a-skill/scripts/w.py --same a-skill/data.txt') == 'passed')
        check('a command failing from the root is retried from its skill folder',
              st('python a-skill/scripts/w.py --need-skill-cwd') == 'passed'
              and r['python a-skill/scripts/w.py --need-skill-cwd'].get('ran_from') == 'a-skill')
        check('a failing command fails with its output', st('python a-skill/scripts/w.py --fail') == 'failed'
              and 'boom' in '\n'.join(r['python a-skill/scripts/w.py --fail'].get('tail') or []))
        early = r['python a-skill/scripts/w.py --fail-early']
        check('a failing command names its FAIL lines even when they scrolled out of the tail',
              st('python a-skill/scripts/w.py --fail-early') == 'failed'
              and any('early-check' in f for f in early.get('failures') or []))
        check('a command over the timeout fails as timed out', st('python a-skill/scripts/w.py --sleep') == 'failed'
              and 'timed out' in (r['python a-skill/scripts/w.py --sleep'].get('reason') or ''))
        check('a missing toolchain is a skip with its reason', st('node a-skill/scripts/n.mjs') == 'skipped'
              and 'node' in (r['node a-skill/scripts/n.mjs']['reason'] or ''))
        check('--require makes a missing toolchain a failure', [x['status'] for x in s2['commands']] == ['failed'])
        check('outputs of one block are gone in the next', st('python a-skill/scripts/r.py o.json') == 'passed'
              and [x['status'] for x in s['commands'] if x['command'] == 'python a-skill/scripts/r.py o.json'] == ['passed', 'skipped'])
        check('a cd block runs from its folder', st('python scripts/r.py data.txt') == 'passed')
        check('internal skip lines of a self-test are reported',
              internal_skips('PASS a\nSKIP  prove_kill_go: go is not on PATH\nlcp: skipped (Playwright not installed)\n'
                             'R-1 [run] 5 passed, 0 failed, 0 error, 0 skipped; exit 0\nok   a timeout is skipped, not fatal\n'
                             'passed: 0 failed, 2 skipped', True)
              == ['SKIP  prove_kill_go: go is not on PATH', 'lcp: skipped (Playwright not installed)', 'passed: 0 failed, 2 skipped'],
              internal_skips('SKIP x', True))
        check('report text that mentions skips is not an internal skip',
              internal_skips('- gitleaks  [not_run] - not installed\nSKIP x\n- step skipped: "clear all"', False) == [])
        w = 'warning: caniuse data not in the cache; usage shares are not rechecked'
        check('a validator that rechecks less without its downloads is reported', internal_skips(w, False) == [w])
        check('a fake self-test\'s skip line reaches the summary',
              any('Go checks' in x for x in (r.get('python a-skill/scripts/w.py --self-test') or {}).get('internal_skips') or []))
        check('the counts add up', sum(s['counts'].values()) == len(cmds), s['counts'])
        after = sorted(p.relative_to(root).as_posix() for p in root.rglob('*'))
        check('nothing is left in the repository', after == before, set(after) ^ set(before))
        check('the scratch copy is removed', not any((Path(d) / 'scratch').iterdir()))
        check('the Markdown summary names failures and skip reasons',
              '### Failed' in markdown(s) and 'user supplies' in markdown(s))

    failed = 0
    for name, ok, extra in checks:
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} {name}" + ('' if ok else f'  [{extra}]'))
    print(f'{failed} failed' if failed else 'ALL PASS')
    return 1 if failed else 0


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--only', help='regular expression; run only commands (or skills) that match')
    ap.add_argument('--list', action='store_true', help='list the commands and how each is classified; run nothing')
    ap.add_argument('--skip-browser', action='store_true', help='skip commands that drive a Playwright browser')
    ap.add_argument('--browser-only', action='store_true', help='run only commands that drive a Playwright browser')
    ap.add_argument('--require', default='', help=f'comma-separated toolchains ({", ".join(TAGS)}) whose absence fails')
    ap.add_argument('--shard', help='K/N: run the K-th of N balanced groups of CLAUDE.md blocks')
    ap.add_argument('--timeout', type=float, default=1200.0, help='seconds per command (default 1200)')
    ap.add_argument('--json', help='write the summary as JSON here')
    ap.add_argument('--markdown', help='write the summary as Markdown here')
    ap.add_argument('--scratch', help='parent folder for the scratch copy (default: the system temporary folder)')
    ap.add_argument('--keep-scratch', action='store_true', help='keep the scratch copy for debugging')
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--claude-md', default=str(ROOT / 'CLAUDE.md'), help=argparse.SUPPRESS)
    ap.add_argument('--self-test', action='store_true', help='check the parser, classifier and runner')
    opts = ap.parse_args(argv)
    if opts.self_test:
        return self_test()
    opts.require = {t.strip() for t in opts.require.split(',') if t.strip()}
    if opts.require - set(TAGS):
        ap.error(f'unknown toolchain in --require: {", ".join(sorted(opts.require - set(TAGS)))}')
    if opts.shard and not re.fullmatch(r'[1-9]\d*/[1-9]\d*', opts.shard or '') or \
            opts.shard and int(opts.shard.split('/')[0]) > int(opts.shard.split('/')[1]):
        ap.error('--shard takes K/N with 1 <= K <= N')
    cmds = parse_claude_md(Path(opts.claude_md).read_text(encoding='utf-8'))
    chosen = select(cmds, opts)
    if opts.list:
        for c in chosen:
            state = f'SKIP ({c.skip})' if c.skip else 'RUN' + (f" needs {','.join(c.needs)}" if c.needs else '')
            print(f'{c.id:4} [{c.cwd or "."}] {c.line}\n       -> {state}')
        print(f'{len(chosen)} of {len(cmds)} commands')
        return 0 if chosen else 2
    if not chosen:
        print('no command selected', file=sys.stderr)
        return 2
    summary = run_all(chosen, ROOT, opts)
    c = summary['counts']
    print(f"\n{c['passed']} passed, {c['failed']} failed, {c['skipped']} skipped ({summary['seconds']} s)")
    md = markdown(summary)
    if opts.json:
        Path(opts.json).write_text(json.dumps(summary, indent=1), encoding='utf-8')
    if opts.markdown:
        Path(opts.markdown).write_text(md, encoding='utf-8')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as fh:
            fh.write(md)
    if not opts.quiet:
        skipped = {}
        for r in summary['commands']:
            if r['status'] == 'skipped':
                skipped[r['reason']] = skipped.get(r['reason'], 0) + 1
        for why, n in sorted(skipped.items(), key=lambda kv: -kv[1]):
            print(f'  skipped {n:3}: {why}')
    return 1 if c['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
