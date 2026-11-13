#!/usr/bin/env python3
r"""Run a test command once or many times under controlled variations (stdlib only).

    python run_tests.py --preset pytest --cwd <repo> --out runs.json [--tests ID ...] [--repeat 20]
        [--minutes 60] [--order given|reverse|shuffle] [--seed N] [--hash-seed N] [--workers N]
        [--tz Pacific/Kiritimati] [--locale tr_TR.UTF-8] [--cpu-load K] [--isolate] [--env K=V ...]
        [--timeout 600] [--label baseline] [--run-prefix R] [--check-tree]

    python run_tests.py --preset custom --cmd "npx mocha --reporter mocha-junit-reporter {tests}" \
        --env MOCHA_FILE={junit} --cwd <repo> --out runs.json

Each run is appended to `runs.json` in parse_results.py's format ({runs[], cases[]}). The recorded
command shows `python` for the interpreter and JUnit paths relative to runs.json. Extra fields
on the run:
    variation {order, seed, hash_seed, workers, tz, tz_applied, locale, cpu_load, isolate, env},
    duration_s, timed_out, code_hash, label, tests_selected, preset

`code_hash` fingerprints the source files under --cwd, so "same code" can be checked between
runs. Inside a git work tree it hashes the files git would track (`git ls-files -co
--exclude-standard`: tracked plus untracked-but-not-ignored), so files tests write into ignored
directories (uploads, caches) don't change it; elsewhere it walks the tree, pruning VCS,
dependency, cache and build directories before descending. Paths longer than Windows' 260
characters are read through the \\?\ prefix; a file that still can't be read is hashed as
unreadable and named in the run's warnings, never a crash. runs.json records the basis
(`code_hash_basis`: method and file count), and a change during the runs names the files.

Presets know each runner's JUnit, ordering, seed and worker flags; a variation a preset can't
apply is an error, never silently ignored. The `nextest` preset runs `cargo nextest run --profile
ci` (per-case JUnit, `--test-threads` workers) and copies target/nextest/ci/junit.xml to the per-run
JUnit path; the `cargo` preset runs `cargo test`, which writes no JUnit and so yields no per-case
data. A run that completes but parses no test cases is flagged `no_tests_parsed` on the run and
warned loudly, so a zero-case run is never mistaken for a pass. `--minutes` caps the wall time:
repeats stop early and the stop is recorded. The user's home directory is replaced with ~ in JUnit
files and output.
`--check-tree` hashes the working tree before and after and exits 3 if anything outside the
JUnit directory changed. The script never edits test files.

The repo's own test script: `--npm-script test` reads package.json's script and reuses its
environment (`cross-env TZ=... ` or `TZ=... ` prefixes) and runner flags (`--configLoader
native`) with the jest, vitest or playwright preset; `--runner-args "..."` appends flags to any
preset. Without either, a run whose npm test script sets env or flags gets a note saying what was
left out. Vitest ids from parse_results (`file::Describe > it`) are passed back as the file plus a
`-t` name pattern, so failed tests can be re-run by id. Vitest shuffles (`--sequence.shuffle`,
seeded) but cannot reverse, so `--order reverse` is refused for it.

Time zones: IANA names are passed to the child unchanged (Node, Go and Python on POSIX read them).
Only Python on Windows gets a translation, because its C runtime reads just "XXX-hh[:mm]"
(Pacific/Kiritimati -> LIN-14, a fixed offset without DST); an unknown zone there is an error.
Node and Python are asked which zone they actually see before the runs; a TZ the runtime
ignored is an error, and for other runners the note says the zone was set but not checked.
LANG and LC_ALL are set as given; many runtimes on Windows ignore them, and each run says so.
`--env K=V` may be repeated or take several pairs, anywhere on the command line.

Environment values are not copied into runs.json: `variation.env` records every name, but only the
harness's own variables (clock shift, TZ, locale, PYTHONPATH, PYTEST_ADDOPTS, NODE_OPTIONS, seeds,
JUnit paths) keep their value, with URL credentials and key=value secrets inside them redacted;
every other value is `<redacted>`. `--record-env NAME` keeps the value of a further variable that
is known not to be secret.

A run that did not run the tests is marked `errored: true` with an `error` reason, never left
looking like a clean empty run: a crashed process (a negative exit, or a Windows NTSTATUS exit such
as 0xC0000005 access violation or 0xC00000FD stack overflow), a timeout, pytest exit codes 2
(interrupted, e.g. a collection error), 3 (internal error), 4 (usage error, e.g. an unknown test
id) and 5 (no tests collected), or no JUnit test cases at all. Its cases, if any, are kept.

pytest ids: reordered and isolated runs collect node ids first. pytest prints them relative to its
rootdir, which may be an ancestor of --cwd (a pytest.ini higher up) or below it (a package's own
pyproject.toml); the ids are mapped back to paths relative to --cwd before they are passed on, and
the run says so. When the ids would make a long command line (Windows allows 32,767 characters),
the run passes only their files and a small generated plugin (`-p qa_skills_test_ids`, from a
temporary directory on PYTHONPATH) reads the ids from a file, keeps exactly those tests and puts
them in the listed order; an id it can't find stops the run with exit 4.

`--hash-path DIR` (repeatable) adds directories outside --cwd to `code_hash`, for code the tests
import from a sibling package (a monorepo's shared library); `code_hash_basis.extra_paths` lists
them. Without it the hash covers --cwd only.

Workspace monorepos: `--repo-root DIR` runs in --cwd (one package) but hashes DIR (the repository),
and records every test id and file with --cwd's path inside DIR as a prefix
(`packages/web/tests/unit/cli.test.ts::...`), so runs of different packages share one code_hash and
two packages' `tests/unit/cli.test.ts` never pool. Ids given back with --tests may carry the prefix;
it is removed before they reach the runner. `--npm-script` follows `pnpm run x` / `npm run x` chains,
skipping segments such as `pnpm run build` that don't run the tests, and a script that hands the
tests to another workspace package (`pnpm --filter web run test`, `npm -w web test`) is refused
with the package's directory to use as --cwd instead.

node:test: the `node-test` preset runs `node --test` with the JUnit reporter (plus spec on stdout),
`--test-concurrency` workers and `--test-randomize --test-random-seed` shuffles; ids from
parse_results (`file::Describe > test`) are passed back as files plus a `--test-name-pattern`.
TypeScript test files run on Node's own type stripping (Node 23.6+); on older Node, or with a
loader the repo uses, pass `--runner-args "--experimental-strip-types"` or `--runner-args
"--import tsx"`, or reuse the repo's script with `--npm-script`. Vitest `-t` patterns accept both
the " > " separator of Vitest 4+ and the space older versions match against.
"""
from __future__ import annotations
import argparse, hashlib, json, os, random, re, shlex, shutil, signal, subprocess, sys, tempfile, time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import parse_results  # noqa: E402

SKIP_DIRS = {'.git', '.hg', '.svn', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
             '.tox', '.nox', '.venv', 'venv', 'env', 'dist', 'build', 'out', 'target', '.gradle', '.idea', '.vscode',
             'coverage', 'htmlcov', '.next', '.nuxt', '.turbo', '.cache', 'test-results', 'playwright-report', '.junit'}
SKIP_SUFFIXES = {'.pyc', '.pyo', '.log', '.xml'}
ENV_PAIR = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
NODE_RUNNERS = {'jest': 'jest', 'vitest': 'vitest', 'playwright': '@playwright/test'}

# IANA name -> TZ string for the Windows C runtime, which reads only "XXX[+|-]hh[:mm]" with a three-letter
# name and no DST rules. These are fixed standard offsets; daylight saving is not modelled.
POSIX_TZ = {
    'UTC': 'UTC0', 'Etc/UTC': 'UTC0', 'Pacific/Kiritimati': 'LIN-14', 'Pacific/Pago_Pago': 'SST11',
    'Pacific/Honolulu': 'HST10', 'America/St_Johns': 'NST3:30', 'America/New_York': 'EST5',
    'Europe/London': 'GMT0', 'Asia/Kolkata': 'IST-5:30', 'Asia/Kathmandu': 'NPT-5:45',
    'Australia/Sydney': 'AES-10', 'Pacific/Auckland': 'NZS-12', 'Pacific/Chatham': 'CHA-12:45',
}

PRESETS = {
    'pytest': {
        'cmd': [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '--junitxml={junit}'],
        'tests': 'args', 'workers': ['-n', '{n}'], 'workers_check': 'xdist', 'order': 'explicit_ids',
        'collect': [sys.executable, '-m', 'pytest', '--collect-only', '-q', '-p', 'no:cacheprovider'],
        'seed_env': 'PYTHONHASHSEED',
    },
    'jest': {
        'cmd': ['npx', 'jest', '--ci', '--reporters=default', '--reporters=jest-junit'],
        'env': {'JEST_JUNIT_OUTPUT_FILE': '{junit}'}, 'tests': 'args', 'workers': ['--maxWorkers={n}'],
        'shuffle': ['--randomize', '--seed={seed}'],
    },
    'vitest': {
        'cmd': ['npx', 'vitest', 'run', '--reporter=junit', '--outputFile={junit}'], 'tests': 'args',
        'workers': ['--maxWorkers={n}'], 'shuffle': ['--sequence.shuffle', '--sequence.seed={seed}'],
    },
    'playwright': {
        'cmd': ['npx', 'playwright', 'test', '--reporter=junit'], 'env': {'PLAYWRIGHT_JUNIT_OUTPUT_NAME': '{junit}'},
        'tests': 'args', 'workers': ['--workers={n}'],
    },
    'gotestsum': {
        'cmd': ['gotestsum', '--junitfile', '{junit}', '--', '-count=1'], 'tests': 'args', 'workers': ['-p', '{n}'],
        'shuffle': ['-shuffle={seed}'],
    },
    # cargo-nextest writes JUnit to target/nextest/<profile>/junit.xml when the profile configures
    # [profile.<profile>.junit] (e.g. .config/nextest.toml). run_one copies that file to the per-run
    # {junit} path (`junit_from`). --test-threads sets the worker count; nextest has no seeded shuffle
    # flag, so --order is refused for it (recorded as not_run), as with the other non-reordering runners.
    'nextest': {
        'cmd': ['cargo', 'nextest', 'run', '--profile', 'ci'], 'tests': 'args',
        'workers': ['--test-threads', '{n}'], 'junit_from': 'target/nextest/ci/junit.xml',
    },
    # Plain `cargo test` emits no JUnit, so a run parses 0 cases and is flagged `no_tests_parsed`
    # (no per-case data). Install cargo-nextest and use --preset nextest for per-case results.
    'cargo': {'cmd': ['cargo', 'test'], 'tests': 'args'},
    # node:test (Node 20+). The junit reporter writes {junit}; spec keeps a readable log on stdout.
    'node-test': {
        'cmd': ['node', '--test', '--test-reporter=junit', '--test-reporter-destination={junit}',
                '--test-reporter=spec', '--test-reporter-destination=stdout'],
        'tests': 'args', 'workers': ['--test-concurrency={n}'],
        'shuffle': ['--test-randomize', '--test-random-seed={seed}'],
    },
    'custom': {'cmd': None, 'tests': 'placeholder'},
}


# ---------------------------------------------------------------- environment values in the record
# Variables the harness itself sets to control a run; their values are evidence (which clock, which zone).
RECORDED_ENV = {'TDA_CLOCK_SHIFT', 'TDA_CLOCK_DATE', 'TDA_CLOCK_ANNOUNCED', 'FAKETIME', 'TZ', 'LANG', 'LANGUAGE',
                'PYTHONPATH', 'PYTEST_ADDOPTS', 'NODE_OPTIONS', 'PYTHONHASHSEED', 'PYTHONDONTWRITEBYTECODE',
                'PYTHONUNBUFFERED', 'PYTHONIOENCODING', 'PYTHONUTF8', 'CI', 'NODE_ENV', 'FORCE_COLOR', 'NO_COLOR',
                'JEST_JUNIT_OUTPUT_FILE', 'MOCHA_FILE', 'PLAYWRIGHT_JUNIT_OUTPUT_NAME', 'VITEST_SEED', 'JEST_SEED',
                'GOFLAGS', 'GOMAXPROCS'}
SECRET_NAME = re.compile(r'PASS|PWD|SECRET|KEY|TOKEN|CRED|AUTH|COOKIE|SESSION|CONN|DSN|PRIVATE|CERT|SIGNATURE|SAS\b',
                         re.I)
URL_CREDENTIALS = re.compile(r'(\b[a-z][a-z0-9+.-]*://)[^/@\s]+@', re.I)
INLINE_SECRET = re.compile(r'\b((?:password|pwd|passwd|secret|token|apikey|api_key|accountkey|sharedaccesskey|'
                           r'sharedaccesssignature|sig)\s*=\s*)[^;&\s]+', re.I)


def scrub(text: str) -> str:
    """Credentials inside a value (user:pass@ in URLs, Password=...; in connection strings) and the home directory."""
    return redact_home(INLINE_SECRET.sub(r'\1<redacted>', URL_CREDENTIALS.sub(r'\1<redacted>@', text)))


def recorded_env(name: str, value: str, keep: set[str] = frozenset()) -> str:
    """The value runs.json records for an environment variable: the scrubbed value for the harness's own variables
    (and names given with --record-env), `<redacted>` for everything else."""
    known = name in RECORDED_ENV or name.startswith('LC_') or name in keep
    if known and (name in keep or not SECRET_NAME.search(name)):
        return scrub(value)
    return '<redacted>'


def env_pairs(groups) -> list[str]:
    """Flatten `--env` given as `nargs='+', action='append'` (repeated, several pairs each) into one K=V list."""
    out = []
    for g in groups or []:
        out.extend([g] if isinstance(g, str) else g)
    return out


def redact_home(text: str) -> str:
    """Replace the user's home directory with ~ (both slash styles), so reports don't carry it."""
    home = str(Path.home())
    for h in sorted({home, home.replace('\\', '/'), home.replace('\\', '\\\\')}, key=len, reverse=True):
        if len(h) > 3:
            text = text.replace(h, '~')
    return text


def long_path(p) -> str:
    """The path as a string that also works past Windows' 260-character limit (\\\\?\\ prefix); unchanged elsewhere."""
    s = os.path.abspath(str(p))
    if os.name != 'nt' or s.startswith('\\\\?\\'):
        return s
    return '\\\\?\\UNC\\' + s[2:] if s.startswith('\\\\') else '\\\\?\\' + s


def read_bytes(p) -> bytes:
    with open(long_path(p), 'rb') as fh:
        return fh.read()


def skipped(rel_parts: list[str]) -> bool:
    return any(d in SKIP_DIRS or d.endswith('.egg-info') for d in rel_parts)


def git_files(root: Path) -> list[str] | None:
    """Files git would track under root (tracked plus untracked-but-not-ignored), relative POSIX paths; None outside
    a work tree, when git is missing, or when git lists nothing (root itself ignored)."""
    try:
        res = subprocess.run(['git', '-C', str(root), '-c', 'core.quotepath=off', 'ls-files', '-co', '--exclude-standard', '-z'],
                             capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode:
        return None
    rels = [r for r in res.stdout.decode('utf-8', 'replace').split('\0') if r]
    return rels or None


def hash_tree(root: Path, notes: list[str] | None = None, basis: dict | None = None) -> tuple[str, dict[str, str]]:
    """(code hash, {relative POSIX path: SHA-256 of the file}) in one pass over the source files."""
    root = Path(root)
    rels = git_files(root)
    method = 'git ls-files -co --exclude-standard' if rels is not None else 'walk'
    found: dict[str, Path] = {}
    if rels is None:
        for p in iter_files(root, errors=notes):
            found[p.relative_to(root).as_posix()] = p
    else:
        for rel in rels:
            parts = rel.rstrip('/').split('/')
            if skipped(parts[:-1]) or (rel.endswith('/') and skipped(parts)):
                continue
            p = root / rel
            if rel.endswith('/') or os.path.isdir(long_path(p)):   # a nested repository git lists as a directory
                for q in iter_files(p, errors=notes):
                    found[q.relative_to(root).as_posix()] = q
            elif Path(parts[-1]).suffix not in SKIP_SUFFIXES and os.path.isfile(long_path(p)):
                found[rel] = p
    h, per_file = hashlib.sha256(), {}
    for rel in sorted(found):   # POSIX path string order: the same on every platform
        try:
            data = read_bytes(found[rel]).replace(b'\r\n', b'\n')
        except OSError as e:
            data = b'\0unreadable\0'
            if notes is not None:
                notes.append(f'code_hash: could not read {rel} ({type(e).__name__}); hashed as unreadable')
        h.update(rel.encode('utf-8') + b'\0')
        h.update(data)
        h.update(b'\0')
        per_file[rel] = hashlib.sha256(data).hexdigest()
    if basis is not None:
        basis.update({'method': method, 'files': len(per_file)})
    return h.hexdigest(), per_file


def hash_trees(root: Path, extra=(), notes: list[str] | None = None, basis: dict | None = None) -> tuple[str, dict[str, str]]:
    """hash_tree(root), widened by extra directories (sibling packages the tests import). Without extras it is
    exactly hash_tree(root); with them, the per-file keys of each extra are prefixed by its path relative to root."""
    chash, digests = hash_tree(root, notes, basis)
    if not extra:
        return chash, digests
    root = Path(root).resolve()
    h = hashlib.sha256(b'code_hash with extra paths\0' + chash.encode() + b'\0')
    shown = []
    for e in sorted({Path(root, x).resolve() for x in extra}, key=lambda p: p.as_posix()):
        rel = Path(os.path.relpath(e, root)).as_posix()
        sub_basis: dict = {}
        sub, sub_digests = hash_tree(e, notes, sub_basis)
        h.update(rel.encode('utf-8') + b'\0' + sub.encode() + b'\0')
        digests.update({f'{rel}/{k}': v for k, v in sub_digests.items()})
        shown.append(rel)
        if basis is not None:
            basis['files'] = basis.get('files', 0) + sub_basis.get('files', 0)
    if basis is not None:
        basis['extra_paths'] = shown
    return h.hexdigest(), digests


def code_hash(root: Path, notes: list[str] | None = None, basis: dict | None = None, extra=()) -> str:
    """SHA-256 of every source file's relative path and content (line endings normalised), sorted by POSIX path
    string so the order is the same on every platform (Path ordering is case-insensitive on Windows). Inside git,
    only files git would track count; otherwise a pruned walk. Never raises for an unreadable file. `extra`
    directories (relative to root or absolute) widen the hash; see hash_trees."""
    return hash_trees(root, extra, notes, basis)[0]


def iter_files(root: Path, extra_skip: set[Path] | None = None, errors: list[str] | None = None):
    """Files under root, pruning SKIP_DIRS (and extra_skip directories) before descending. Yields ordinary Paths
    under root; the walk itself goes through the long-path prefix so deep trees are listed on Windows too."""
    root = Path(root)
    walk_root = long_path(root)
    skip = {os.path.normcase(str(Path(s).resolve())) for s in (extra_skip or ())}
    base = os.path.normcase(str(root.resolve())) if skip else ''

    def onerror(e):
        if errors is not None:
            errors.append(f'could not list {os.path.relpath(e.filename, walk_root) if e.filename else root} ({type(e).__name__})')
    for dirpath, dirnames, filenames in os.walk(walk_root, onerror=onerror):
        rel_dir = os.path.relpath(dirpath, walk_root)
        rel_dir = '' if rel_dir == '.' else rel_dir
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith('.egg-info')
                       and not (skip and os.path.normcase(os.path.join(base, rel_dir, d)) in skip)]
        here = root / rel_dir if rel_dir else root
        for f in filenames:
            if Path(f).suffix not in SKIP_SUFFIXES:
                yield here / f


def tree_snapshot(root: Path, skip: set[Path]) -> dict[str, str]:
    out = {}
    for p in iter_files(root, skip):
        try:
            out[p.relative_to(root).as_posix()] = hashlib.sha256(read_bytes(p)).hexdigest()
        except OSError:
            pass
    return out


def runtime_of(preset_name: str, cmd: str | None) -> str:
    """Which runtime reads TZ for this run: python, node, go, rust or unknown."""
    if preset_name == 'pytest':
        return 'python'
    if preset_name in NODE_RUNNERS or preset_name == 'node-test':
        return 'node'
    if preset_name == 'gotestsum':
        return 'go'
    if preset_name in ('cargo', 'nextest'):
        return 'rust'
    first = Path(shlex.split(cmd)[0]).name.lower() if cmd and shlex.split(cmd) else ''
    first = re.sub(r'\.(exe|cmd|bat)$', '', first)
    if re.match(r'(python[\d.]*|py|pytest)$', first):
        return 'python'
    if first in ('node', 'npx', 'npm', 'pnpm', 'yarn', 'bun', 'vitest', 'jest', 'playwright', 'mocha', 'ava'):
        return 'node'
    if first in ('cargo', 'rustc', 'nextest', 'cargo-nextest'):
        return 'rust'
    return 'unknown'


POSIX_OFFSET = re.compile(r'^[A-Z]{3,}([+-]?)(\d{1,2})(?::(\d\d))?$')


def tz_value(tz: str, runtime: str) -> tuple[str, str | None]:
    """The TZ value to put in the child's environment, and a note. Only Python on Windows needs a translation."""
    if os.name == 'nt' and runtime == 'python':
        if tz in POSIX_TZ:
            return POSIX_TZ[tz], (f'TZ {tz} applied to Python as {POSIX_TZ[tz]!r}, a fixed standard offset '
                                  '(the Windows C runtime has no IANA zones or DST rules)')
        if POSIX_OFFSET.match(tz):
            return tz, None
        raise ValueError(f'TZ {tz} has no translation for Python on Windows (known: {", ".join(sorted(POSIX_TZ))}); '
                         'pass a POSIX string such as "XYZ-5:30", or record the time-zone variation as not_run')
    return tz, None


def probe_tz(runtime: str, value: str, env: dict, cwd: Path) -> tuple[bool | None, str]:
    """Ask the runtime which zone it sees under TZ=value: (applied?, note). None when it couldn't be checked."""
    if runtime == 'node':
        node = shutil.which('node', path=env.get('PATH'))
        if not node:
            return None, f'TZ set to {value}; node was not found, so whether the runner applies it was not checked'
        res = subprocess.run([node, '-e', 'process.stdout.write(Intl.DateTimeFormat().resolvedOptions().timeZone + " " + '
                              'String(-new Date(Date.UTC(2026, 0, 15)).getTimezoneOffset()))'],
                             cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
        seen = res.stdout.strip()
        zone = seen.split(' ')[0] if seen else ''
        if res.returncode or not zone:
            return None, f'TZ set to {value}; asking node which zone it sees failed, so it was not checked'
        utcish = {'UTC', 'Etc/UTC', 'GMT', 'Etc/GMT', 'Etc/Universal', 'Universal', 'Zulu', 'Etc/Zulu'}
        if zone == 'Etc/Unknown' or (zone in utcish and value not in utcish):
            return False, f'node ignored TZ={value} (it reports {zone})'
        same = zone == value
        return True, f'TZ={value}: node reports {zone}' + ('' if same else ' (an alias or link of the requested zone)')
    if runtime == 'python' and os.name == 'nt':
        res = subprocess.run([sys.executable, '-c', 'import time; print(-time.timezone // 60)'], cwd=cwd, env=env,
                             capture_output=True, text=True, timeout=60)
        m = POSIX_OFFSET.match(value)
        try:
            seen = int(res.stdout.strip())
        except ValueError:
            return None, f'TZ set to {value}; asking Python for its offset failed, so it was not checked'
        if m:
            want = (int(m.group(2)) * 60 + int(m.group(3) or 0)) * (1 if m.group(1) == '-' else -1)
            if seen != want:
                return False, f'Python ignored TZ={value} (standard offset {seen:+d} min, expected {want:+d})'
        return True, f'TZ={value}: Python reports a standard offset of {seen:+d} min'
    return None, f'TZ set to {value} in the environment; whether this runner applies it was not checked'


# ---------------------------------------------------------------- the repo's own npm script
SCRIPT_PREFIX_OK = {'cross-env', 'cross-env-shell', 'env', 'npx', 'pnpm', 'yarn', 'exec', '--'}
DROP_FLAGS = re.compile(r'^--(reporter|reporters|outputFile|output-file|watch|ui|coverage\.reporter|'
                        r'test-reporter|test-reporter-destination)(=|$)')
WS_DELEGATE = re.compile(r'^(?:pnpm|yarn|npm|bun)$')


def workspace_globs(root: Path) -> list[str]:
    """Workspace package globs from pnpm-workspace.yaml or package.json workspaces at root."""
    out = []
    pw = root / 'pnpm-workspace.yaml'
    if pw.is_file():
        m = re.search(r'^packages\s*:\s*\n((?:[ \t]+-.*\n?)+)', pw.read_text(encoding='utf-8', errors='replace'), re.M)
        if m:
            out = [x.strip().strip('\'"') for x in re.findall(r'-\s*(.+)', m.group(1))]
    if not out and (root / 'package.json').is_file():
        try:
            w = json.loads((root / 'package.json').read_text(encoding='utf-8')).get('workspaces')
        except (OSError, ValueError, AttributeError):
            w = None
        out = list(w if isinstance(w, list) else (w or {}).get('packages', []) if isinstance(w, dict) else [])
    return [g for g in out if not g.startswith('!')]


def find_workspace_package(root: Path, name: str) -> str | None:
    """The directory (relative to root) of the workspace package called name, or None."""
    for g in workspace_globs(root):
        for d in sorted(root.glob(g.rstrip('/'))):
            pj = d / 'package.json'
            if pj.is_file():
                try:
                    if json.loads(pj.read_text(encoding='utf-8')).get('name') == name:
                        return d.relative_to(root).as_posix()
                except (OSError, ValueError, AttributeError):
                    continue
    return None


def delegated_package(toks: list[str]) -> str | None:
    """The workspace package a script segment hands the tests to: pnpm --filter X / -F X, npm -w X /
    --workspace X, yarn workspace X; None otherwise."""
    if not toks or not WS_DELEGATE.match(toks[0]):
        return None
    for i, t in enumerate(toks[1:], 1):
        m = re.match(r'^(?:--filter|-F|--workspace|-w)(?:=(.+))?$', t)
        if m:
            return m.group(1) or (toks[i + 1] if i + 1 < len(toks) else None)
        if toks[0] == 'yarn' and t == 'workspace' and i + 1 < len(toks):
            return toks[i + 1]
    return None


def npm_script(cwd: Path, name: str, runner: str, depth: int = 0) -> tuple[dict, list[str], str]:
    """(env, runner args, script text) from package.json's script `name`, following `npm run x` one level at a time.
    Raises ValueError when the script doesn't call the runner in a form this can reproduce."""
    pkg = json.loads((cwd / 'package.json').read_text(encoding='utf-8'))
    script = (pkg.get('scripts') or {}).get(name)
    if not script:
        raise ValueError(f'package.json has no "{name}" script')
    segments = [s.strip() for s in re.split(r'&&|\|\||;', script) if s.strip()]
    delegated, inner_errors = [], []
    for seg in segments:
        try:
            toks = shlex.split(seg)
        except ValueError:
            continue
        target = delegated_package(toks)
        if target:
            if re.search(r'\b(test|spec|e2e)', seg):
                delegated.append((target, seg))
            continue
        m = None
        if len(toks) >= 3 and toks[0] in ('npm', 'pnpm', 'yarn', 'bun') and toks[1] in ('run', 'run-script') and depth < 3:
            m = toks[2]
        elif len(toks) == 2 and toks[0] in ('pnpm', 'yarn', 'bun') and toks[1] in (pkg.get('scripts') or {}) and depth < 3:
            m = toks[1]
        if m:
            try:   # a chain such as `pnpm run build && vitest run`: a segment that doesn't run the tests is skipped
                env, args, _ = npm_script(cwd, m, runner, depth + 1)
                return env, args, script
            except ValueError as e:
                inner_errors.append(str(e))
                continue
        bins = {runner, 'playwright'} if runner == 'playwright' else {runner}
        idx = next((i for i, t in enumerate(toks) if Path(t).name in bins), None)
        if runner == 'node' and idx is not None and '--test' not in toks[idx + 1:]:
            idx = None   # `node scripts/build.js`, not a node:test run
        if idx is None:
            continue
        env = {}
        for t in toks[:idx]:
            if ENV_PAIR.match(t):
                k, _, v = t.partition('=')
                env[k] = v
            elif t not in SCRIPT_PREFIX_OK:
                raise ValueError(f'script "{name}" runs {runner} behind "{t}", which can\'t be reproduced here; '
                                 'use --preset custom --cmd with the full command')
        rest = toks[idx + 1:]
        if rest and rest[0] in ('run', 'test') and runner in ('vitest', 'playwright'):
            rest = rest[1:]
        if runner == 'node':
            rest = [t for t in rest if t != '--test']   # the preset already passes --test
        args, skip_next = [], False
        for i, t in enumerate(rest):
            if skip_next:
                skip_next = False
                continue
            if DROP_FLAGS.match(t):
                skip_next = '=' not in t and t in ('--reporter', '--reporters', '--outputFile', '--output-file',
                                                   '--test-reporter', '--test-reporter-destination')
                continue
            args.append(t)
        return env, args, script
    if delegated:
        name_, seg = delegated[0]
        where = find_workspace_package(cwd, name_)
        raise ValueError(f'script "{name}" hands the tests to the workspace package {name_!r} ({seg!r}); run with '
                         + (f'--cwd {where} ' if where else '--cwd <that package\'s directory> ')
                         + '--npm-script test (its own script), and --repo-root . to keep repository-relative ids')
    raise ValueError(f'script "{name}" ({script!r}) does not call {runner}'
                     + (f' ({"; ".join(inner_errors[:2])})' if inner_errors else '') + '; use --preset custom --cmd')


def node_entry(cwd: Path, pkg: str) -> str | None:
    """The JavaScript file behind a package's CLI, so it can be started with node instead of a .cmd shim."""
    pj = cwd / 'node_modules' / pkg / 'package.json'
    try:
        meta = json.loads(pj.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    b = meta.get('bin')
    if isinstance(b, str):
        rel = b
    elif isinstance(b, dict):
        rel = b.get(pkg.split('/')[-1]) or b.get('playwright')
    else:
        rel = None
    if not rel:
        return None
    p = (pj.parent / rel).resolve()
    return str(p) if p.is_file() else None


def vitest_ids(tests: list[str]) -> tuple[list[str], str | None]:
    """parse_results ids (`file::Describe > it`) -> (file filters, a -t pattern matching exactly those names)."""
    files, names = [], []
    if len({'::' in t for t in tests}) > 1:
        raise ValueError('vitest: mix of whole files and test ids in one run; -t would filter the whole files too. '
                         'Run them separately, or use --isolate')
    for t in tests:
        f, sep, name = t.partition('::')
        if f not in files:
            files.append(f)
        if sep and name:
            name = re.sub(r'#\d+$', '', name)
            # Vitest 4+ matches -t against the full name joined with " > ", older versions with a space: accept both
            names.append('(?: > | )'.join(re.sub(r'[.*+?^${}()|[\]\\/]', lambda m: '\\' + m.group(0), part)
                                          for part in name.split(' > ')))
    if not names:
        return files, None
    return files, '^(?:' + '|'.join(dict.fromkeys(names)) + ')$'


def node_test_ids(tests: list[str]) -> tuple[list[str], str | None]:
    """parse_results ids for node:test (`file::Describe > test`) -> (files, a --test-name-pattern). Node matches the
    pattern against the test's name joined with its ancestors' by spaces."""
    files, names = [], []
    if len({'::' in t for t in tests}) > 1:
        raise ValueError('node-test: mix of whole files and test ids in one run; --test-name-pattern would filter the '
                         'whole files too. Run them separately, or use --isolate')
    for t in tests:
        f, sep, name = t.partition('::')
        if f not in files:
            files.append(f)
        if sep and name:
            name = re.sub(r'#\d+$', '', name)
            names.append(re.sub(r'[.*+?^${}()|[\]\\/]', lambda m: '\\' + m.group(0), name.replace(' > ', ' ')))
    if not names:
        return files, None
    return files, '^(?:' + '|'.join(dict.fromkeys(names)) + ')$'


ARGS_BUDGET = 6000   # characters of test ids on one command line before they go through a file instead


def chunks(items: list[str], budget: int = ARGS_BUDGET) -> list[list[str]]:
    out, cur, size = [], [], 0
    for x in items:
        if cur and size + len(x) + 1 > budget:
            out.append(cur)
            cur, size = [], 0
        cur.append(x)
        size += len(x) + 1
    return out + ([cur] if cur else [])


def map_ids_to_cwd(ids: list[str], cwd: Path, given: list[str]) -> tuple[list[str], str | None]:
    """pytest prints node ids relative to its rootdir, which need not be --cwd: an ancestor holding pytest.ini, or a
    package below --cwd whose pyproject.toml the given paths lead to. Map each id's file part to a path relative to
    cwd, trying cwd, its ancestors and the directories between cwd and each given path. (ids, note or None)."""
    cwd = cwd.resolve()
    bases = [cwd, *cwd.parents]
    for g in given:
        p = (cwd / g.split('::')[0]).resolve()
        chain = []
        while p != cwd and cwd in p.parents:
            chain.append(p)
            p = p.parent
        bases += [q for q in reversed(chain) if q.is_dir()]
    out, used = [], None
    for i in ids:
        f, sep, rest = i.partition('::')
        if (cwd / f).exists():
            out.append(i)
            continue
        base = used if used is not None and (used / f).exists() else next((b for b in bases if (b / f).exists()), None)
        if base is None:
            raise SystemExit(f'pytest collected {i!r}, but no file {f} exists relative to --cwd, its parents or the '
                             'given test paths; run from pytest\'s rootdir (the directory holding its ini file)')
        used = base
        out.append(Path(os.path.relpath(base / f, cwd)).as_posix() + sep + rest)
    if used is None:
        return out, None
    return out, (f'pytest reported node ids relative to its rootdir '
                 f'{Path(os.path.relpath(used, cwd)).as_posix()}; they were mapped to paths relative to --cwd.')


def collect_pytest(cwd: Path, tests: list[str], env: dict) -> tuple[list[str], str | None]:
    ids, outs = [], []
    for part in (chunks(tests) or [[]]):   # long --tests lists are collected in pieces, keeping their order
        res = subprocess.run(PRESETS['pytest']['collect'] + part, cwd=cwd, env=env, capture_output=True, text=True,
                             encoding='utf-8', errors='replace')
        ids += [l.strip() for l in res.stdout.splitlines() if '::' in l and not l.startswith(' ')]
        outs.append(res.stdout[-800:] + res.stderr[-800:])
    if not ids:
        raise SystemExit('pytest collected no tests:\n' + '\n'.join(outs)[-1600:])
    return map_ids_to_cwd(list(dict.fromkeys(ids)), cwd, tests)


IDS_PLUGIN = 'qa_skills_test_ids'
IDS_PLUGIN_SOURCE = r'''"""Generated by run_tests.py: keep exactly the test ids listed in $QA_SKILLS_TEST_IDS, in that order."""
import os
import pytest


def _key(item, cwd):
    path = getattr(item, "path", None) or getattr(item, "fspath", None)
    _, sep, rest = item.nodeid.partition("::")
    return os.path.relpath(str(path), cwd).replace(os.sep, "/") + sep + rest


def pytest_collection_modifyitems(session, config, items):
    listed = os.environ.get("QA_SKILLS_TEST_IDS")
    if not listed:
        return
    with open(listed, encoding="utf-8") as fh:
        want = [line.rstrip("\r\n") for line in fh if line.strip()]
    cwd = os.environ.get("QA_SKILLS_IDS_CWD") or os.getcwd()
    rank = {w: n for n, w in enumerate(want)}
    keep, drop, found = [], [], set()
    for item in items:
        k = _key(item, cwd)
        if k in rank and k not in found:
            keep.append(item)
            found.add(k)
        else:
            drop.append(item)
    missing = [w for w in want if w not in found]
    if missing:
        pytest.exit(f"qa_skills_test_ids: {len(missing)} listed test id(s) were not collected, e.g. {missing[0]}",
                    returncode=4)
    keep.sort(key=lambda it: rank[_key(it, cwd)])
    if drop:
        config.hook.pytest_deselected(items=drop)
    items[:] = keep
'''


def ids_by_file(tests: list[str], cwd: Path, budget: int = ARGS_BUDGET) -> list[str]:
    """The paths to give pytest when the ids themselves go through a file: each test's file, in first-seen order, or
    their common directory when even the file list is long. Collecting a file or directory then the plugin's filter
    gives the same rootdir and configuration as naming the ids."""
    files = list(dict.fromkeys(t.split('::')[0] for t in tests))
    if sum(len(f) + 1 for f in files) <= budget:
        return files
    common = os.path.commonpath([str((cwd / f).resolve()) for f in files])
    common = Path(common) if os.path.isdir(common) else Path(common).parent
    return [Path(os.path.relpath(common, cwd)).as_posix()]


def kill_tree(proc: subprocess.Popen) -> None:
    if os.name == 'nt':
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def start_hogs(k: int) -> list[subprocess.Popen]:
    kw = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    return [subprocess.Popen([sys.executable, '-c', 'while True: pass'], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, **kw) for _ in range(k)]


def has_module(name: str, cwd: Path, env: dict) -> bool:
    return subprocess.run([sys.executable, '-c', f'import {name}'], cwd=cwd, env=env, capture_output=True).returncode == 0


def build(args, preset: dict, junit: Path, tests: list[str], seed: int | None) -> tuple[list[str], dict]:
    fill = lambda s: s.replace('{junit}', str(junit)).replace('{seed}', str(seed)).replace('{n}', str(args.workers))  # noqa: E731
    if preset['cmd'] is None:
        cmd = []
        for tok in shlex.split(args.cmd):
            if tok == '{tests}':
                cmd += tests  # the token expands to one argument per test
            else:
                cmd.append(fill(tok))
        if tests and '{tests}' not in args.cmd:
            cmd += tests
    else:
        cmd = [fill(x) for x in (shlex.split(args.cmd) if args.cmd else preset['cmd'])]
        if args.cmd and '{junit}' not in args.cmd and preset is PRESETS['pytest']:
            cmd += [f'--junitxml={junit}']
        if args.preset == 'node-test' and not args.cmd:
            # node reads options only before the first file: flags first, then the script's files or the tests
            script = list(getattr(args, 'script_args', []))
            flags, positional, i = [], [], 0
            while i < len(script):
                t = script[i]
                if t.startswith('-'):
                    flags.append(t)
                    if '=' not in t and t in ('--import', '--require', '-r', '--loader', '--experimental-loader',
                                              '--test-name-pattern', '--test-skip-pattern', '--test-concurrency',
                                              '--conditions', '-C', '--env-file') and i + 1 < len(script):
                        flags.append(script[i + 1])
                        i += 1
                else:
                    positional.append(t)
                i += 1
            cmd += shlex.split(args.runner_args or '') + flags
            if args.workers:
                cmd += [fill(x) for x in preset['workers']]
            if args.order == 'shuffle':
                cmd += [fill(x) for x in preset['shuffle']]
            files, pattern = node_test_ids(tests) if tests else (positional, None)
            cmd += ([f'--test-name-pattern={pattern}'] if pattern else []) + files
            env = {k: fill(v) for k, v in preset.get('env', {}).items()}
            return cmd, env
        cmd += getattr(args, 'script_args', [])
        cmd += shlex.split(args.runner_args or '')
        if args.workers:
            cmd += [fill(x) for x in preset['workers']]
        if args.order == 'shuffle' and 'shuffle' in preset:
            cmd += [fill(x) for x in preset['shuffle']]
        if args.preset == 'vitest' and not args.cmd:
            files, pattern = vitest_ids(tests)
            cmd += files + (['-t', pattern] if pattern else [])
        else:
            cmd += tests
    env = {k: fill(v) for k, v in preset.get('env', {}).items()}
    return cmd, env


CMD_META = re.compile(r'[\^&|<>()%!"]')


def resolve_exe(cmd: list[str], env: dict, cwd: Path) -> list[str]:
    """On Windows, npx/gradlew etc. are .cmd/.bat shims that cmd.exe parses, and cmd.exe would act on ^ & | < > in
    arguments (a -t pattern, a test name). Such a command starts the package's own JS entry with node instead."""
    if os.name != 'nt' or os.path.isabs(cmd[0]):
        return cmd
    found = shutil.which(cmd[0], path=env.get('PATH'))
    if not found:
        return cmd
    if found.lower().endswith(('.cmd', '.bat')) and any(CMD_META.search(a) for a in cmd[1:]):
        if Path(cmd[0]).name.lower() == 'npx' and len(cmd) > 1:
            pkg = NODE_RUNNERS.get(cmd[1], cmd[1])
            entry = node_entry(cwd, pkg)
            node = shutil.which('node', path=env.get('PATH'))
            if entry and node:
                return [node, entry, *cmd[2:]]
        raise SystemExit(f'{cmd[0]} is a batch shim on Windows and an argument holds characters cmd.exe would '
                         'interpret (^ & | < > ( ) % ! "); run by file instead, or use --preset custom with node')
    return [found, *cmd[1:]]


PYTEST_EXIT = {2: 'pytest exit 2: the session was interrupted (usually a collection error)',
               3: 'pytest exit 3: internal error', 4: 'pytest exit 4: usage error (an option or test id pytest could not use)',
               5: 'pytest exit 5: no tests were collected'}
NTSTATUS = {0xC0000005: 'access violation', 0xC00000FD: 'stack overflow', 0xC0000409: 'stack buffer overrun',
            0xC0000374: 'heap corruption', 0xC000001D: 'illegal instruction', 0xC0000094: 'integer divide by zero',
            0xC000013A: 'interrupted (Ctrl-C)', 0xC0000135: 'a DLL was not found', 0xC0000142: 'a DLL failed to initialise'}


FATAL_RX = re.compile(r'Fatal Python error|Windows fatal exception|Segmentation fault|core dumped')


def run_error(code: int | None, timed_out: bool, cases: list, is_pytest: bool, output: str = '') -> str | None:
    """Why this run did not run the tests, or None when it did. A crashed or errored run is never a clean empty run."""
    if timed_out:
        return 'timed out; the process tree was killed'
    if code is not None and code >= 0xC0000000:   # Windows NTSTATUS: the process died, usually without a traceback
        return f'the process crashed (exit 0x{code:08X}, {NTSTATUS.get(code, "an NTSTATUS error")}); no clean result'
    if code and FATAL_RX.search(output or ''):
        return f'the interpreter crashed ({FATAL_RX.search(output).group(0)} in the output, exit {code}); no clean result'
    if code is not None and code < 0:
        try:
            name = signal.Signals(-code).name
        except (ValueError, AttributeError):
            name = f'signal {-code}'
        return f'the process was killed by {name}; no clean result'
    if is_pytest and code in PYTEST_EXIT:
        return PYTEST_EXIT[code]
    if not cases:
        if code in (132, 134, 135, 136, 137, 139):   # a shell reporting 128 + signal
            return f'the process was killed by signal {code - 128} (exit {code}) and wrote no test results'
        return f'no test cases were parsed (exit {code}); the tests did not run, or wrote no JUnit'
    return None


def plugin_dir(args) -> str:
    """A temporary directory holding the generated ids plugin, made once per invocation."""
    if not getattr(args, 'ids_dir', None):
        args.ids_dir = tempfile.mkdtemp(prefix='qa-ids-')
        Path(args.ids_dir, IDS_PLUGIN + '.py').write_text(IDS_PLUGIN_SOURCE, encoding='utf-8')
    return args.ids_dir


def run_one(args, preset, run_id: str, tests: list[str], base_env: dict, junit_dir: Path, cwd: Path,
            seed: int | None, notes: list[str]) -> dict:
    junit = (junit_dir / f'{run_id}.xml').resolve()
    if junit.exists():
        junit.unlink()
    notes = list(notes)
    via_file, argv_tests = None, tests
    if args.preset == 'pytest' and tests and sum(len(t) + 1 for t in tests) > ARGS_BUDGET:
        d = plugin_dir(args)
        via_file = Path(d, f'{run_id}.ids.txt')
        via_file.write_text('\n'.join(tests) + '\n', encoding='utf-8')
        argv_tests = ['-p', IDS_PLUGIN, *ids_by_file(tests, cwd)]
        notes.append(f'{len(tests)} test ids ({sum(len(t) + 1 for t in tests)} characters) were passed through a file '
                     f'to the generated -p {IDS_PLUGIN} plugin, which kept exactly those tests in the listed order; '
                     'the command names only their files.')
    cmd, extra_env = build(args, preset, junit, argv_tests, seed)
    env = {**base_env, **extra_env}
    if via_file:
        env['QA_SKILLS_TEST_IDS'], env['QA_SKILLS_IDS_CWD'] = str(via_file), str(cwd)
        env['PYTHONPATH'] = str(via_file.parent) + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    if os.name == 'nt' and len(subprocess.list2cmdline(cmd)) > 32000:
        raise SystemExit(f'the command for {run_id} is {len(subprocess.list2cmdline(cmd))} characters, over the Windows '
                         'limit of 32,767; select fewer tests per run, or use --isolate')
    kw = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    exe = resolve_exe(cmd, env, cwd)
    hogs = start_hogs(args.cpu_load) if args.cpu_load else []
    t0 = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.Popen(exe, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
        try:
            out, _ = proc.communicate(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            out, _ = proc.communicate()
            timed_out = True
        code = proc.returncode
    finally:
        for h in hogs:
            kill_tree(h)
    duration = round(time.monotonic() - t0, 3)
    copy_notes = []
    junit_from = preset.get('junit_from')   # nextest writes JUnit to a profile path, not {junit}: copy it here
    if junit_from and not junit.exists():
        src = cwd / junit_from
        if os.path.isfile(long_path(src)):
            shutil.copyfile(long_path(src), long_path(junit))
            copy_notes.append(f'Copied JUnit from {junit_from} (cargo-nextest profile output) to {junit.name}.')
        else:
            copy_notes.append(f'cargo-nextest wrote no JUnit at {junit_from}; this run has no per-case data '
                              '(configure [profile.ci.junit] in .config/nextest.toml, or install cargo-nextest).')
    if junit.exists():
        junit.write_text(redact_home(junit.read_text(encoding='utf-8', errors='replace')), encoding='utf-8')
    cases, pnotes = parse_results.parse_junit(junit, run_id, cwd) if junit.exists() else ([], [f'{junit.name}: not written'])
    prefix = getattr(args, 'id_prefix', '')
    if prefix:   # --repo-root: ids and files relative to the repository, so packages never pool
        for c in cases:
            c['id'] = f"{prefix}/{c['id']}"
            if c.get('file') and not c['file'].startswith(('~', '/')) and not re.match(r'^[A-Za-z]:', c['file']):
                c['file'] = f"{prefix}/{c['file']}"

    run_notes = list(notes) + pnotes + copy_notes
    if timed_out:
        run_notes.append(f'Timed out after {args.timeout}s; the process tree was killed.')
    is_pytest = args.preset == 'pytest' or bool(args.cmd and re.search(r'\bpytest\b', args.cmd))
    text = (out or b'').decode('utf-8', 'replace')
    error = run_error(code, timed_out, cases, is_pytest, text)
    if error:
        tail = scrub(text[-600:])
        run_notes.append(f'Errored: {error}. Treat this run as not having run the tests'
                         + (' (its parsed cases are partial)' if cases else '') + '. Output tail: ' + tail)
    if Path(cmd[0]).name.lower() == 'npx' and Path(exe[0]).name.lower().startswith('node'):   # see resolve_exe
        run_notes.append(f'Started as node {Path(exe[1]).name} (the package CLI) instead of the {cmd[0]} batch shim, '
                         'so cmd.exe could not reinterpret the arguments.')
    shown = ' '.join('python' if c == sys.executable else (shlex.quote(c) if re.search(r'\s', c) else c) for c in cmd)
    rel = os.path.relpath(junit, args.out.resolve().parent).replace(os.sep, '/')
    shown = shown.replace(str(junit), rel)
    shown = scrub(shown)
    return {'id': run_id, 'command': shown, 'exit_code': code, 'junit_paths': [rel],
            'started_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'totals': parse_results.totals(cases), 'file_hashes': {}, 'notes': run_notes,
            'duration_s': duration, 'timed_out': timed_out, 'errored': bool(error), 'error': error,
            'label': args.label, 'preset': args.preset,
            'tests_selected': [f'{prefix}/{t}' for t in tests] if prefix else tests,
            **({'id_prefix': prefix} if prefix else {})}, cases


def self_test() -> int:
    """Check id patterns, npm script chains and workspace delegation, and (with node on PATH) a node-test run with
    --repo-root on a small synthetic workspace."""
    checks = []
    _, pat = vitest_ids(['tests/a.test.ts::cart > totals > adds tax'])
    rx = re.compile(pat)
    checks.append(('a vitest -t pattern matches the " > " and the space-joined name',
                   bool(rx.match('cart > totals > adds tax') and rx.match('cart totals adds tax')), pat))
    _, pat = node_test_ids(['a.test.mjs::sum > inner > deep # 1'])
    checks.append(('a node-test pattern matches the space-joined name', bool(re.match(pat, 'sum inner deep # 1')), pat))
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / 'pnpm-workspace.yaml').write_text('packages:\n  - packages/*\n', encoding='utf-8')
        (root / 'package.json').write_text(json.dumps({'name': 'shop', 'scripts': {
            'build': 'tsc -b', 'test': 'pnpm run build && pnpm --filter @shop/cart run test',
            'test:scripts': 'pnpm run build && node --test --test-reporter=spec "scripts/*.test.mjs"',
            'test:unit': 'pnpm run build && cross-env TZ=UTC vitest run --project unit'}}), encoding='utf-8')
        pkg = root / 'packages' / 'cart'
        (pkg / 'tests').mkdir(parents=True)
        (pkg / 'package.json').write_text(json.dumps({'name': '@shop/cart', 'scripts': {
            'test': 'node --test "tests/*.test.mjs"'}}), encoding='utf-8')
        (pkg / 'tests' / 'cart.test.mjs').write_text(
            "import { describe, it } from 'node:test';\nimport assert from 'node:assert';\n"
            "describe('cart', () => { it('adds', () => assert.equal(1 + 1, 2)); it('fails', () => assert.equal(1, 2)); });\n",
            encoding='utf-8')
        try:
            npm_script(root, 'test', 'node')
            checks.append(('a script that hands the tests to a workspace package is refused with its directory', False, 'accepted'))
        except ValueError as e:
            checks.append(('a script that hands the tests to a workspace package is refused with its directory',
                           '--cwd packages/cart' in str(e), str(e)))
        env, args, _ = npm_script(root, 'test:scripts', 'node')
        checks.append(('a build step before node --test is skipped, reporters dropped', args == ['scripts/*.test.mjs'], args))
        env, args, _ = npm_script(root, 'test:unit', 'vitest')
        checks.append(('a build step before vitest is skipped, env and flags kept',
                       env == {'TZ': 'UTC'} and args == ['--project', 'unit'], (env, args)))
        if shutil.which('node'):
            out = root / 'out' / 'runs.json'
            code = main(['--preset', 'node-test', '--npm-script', 'test', '--cwd', str(pkg), '--repo-root', str(root),
                         '--out', str(out)])
            data = json.loads(out.read_text(encoding='utf-8')) if out.exists() else {'runs': [], 'cases': []}
            ids = sorted(c['id'] for c in data['cases'])
            checks.append(('node-test with --repo-root records repository-relative ids',
                           code == 0 and ids == ['packages/cart/tests/cart.test.mjs::cart > adds',
                                                 'packages/cart/tests/cart.test.mjs::cart > fails'], (code, ids)))
            code = main(['--preset', 'node-test', '--npm-script', 'test', '--cwd', str(pkg), '--repo-root', str(root),
                         '--out', str(out), '--tests', 'packages/cart/tests/cart.test.mjs::cart > fails'])
            data = json.loads(out.read_text(encoding='utf-8'))
            last = [c for c in data['cases'] if c['run_id'] == data['runs'][-1]['id']]
            checks.append(('a repository-relative id is run back by name',
                           [(c['id'], c['status']) for c in last] == [('packages/cart/tests/cart.test.mjs::cart > fails', 'failed')],
                           [(c['id'], c['status']) for c in last]))
        else:
            print('SKIP node-test runs: node is not on PATH')
    failed = 0
    for name, ok, extra in checks:
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} {name}" + ('' if ok else f'  [{extra}]'))
    print(f'{failed} failed' if failed else 'ALL PASS')
    return 1 if failed else 0


def main(argv=None) -> int:
    if '--self-test' in (sys.argv[1:] if argv is None else argv):
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--preset', choices=sorted(PRESETS), default='pytest')
    ap.add_argument('--cmd', help='base command; required for --preset custom ({junit}, {tests}, {seed}, {n} placeholders)')
    ap.add_argument('--cwd', type=Path, default=Path('.'))
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--junit-dir', type=Path, help='default: <out dir>/junit')
    ap.add_argument('--tests', nargs='*', default=[])
    ap.add_argument('--tests-file', type=Path, help='one test id per line')
    ap.add_argument('--repeat', type=int, default=1)
    ap.add_argument('--minutes', type=float, default=60.0, help='wall-time cap for all repeats (default 60)')
    ap.add_argument('--timeout', type=int, default=900, help='per-run timeout in seconds')
    ap.add_argument('--order', choices=['given', 'reverse', 'shuffle'], default='given')
    ap.add_argument('--seed', type=int, help='shuffle seed (default: a recorded random seed per run)')
    ap.add_argument('--hash-seed', help='PYTHONHASHSEED for every run ("random" leaves Python to choose)')
    ap.add_argument('--workers', type=int)
    ap.add_argument('--tz')
    ap.add_argument('--locale')
    ap.add_argument('--cpu-load', type=int, default=0, help='number of busy sibling processes during each run')
    ap.add_argument('--isolate', action='store_true', help='each test in its own process')
    ap.add_argument('--env', nargs='+', action='append', default=[], metavar='K=V',
                    help='environment for the runs; repeat it or give several pairs, anywhere on the command line. '
                         'Only the harness\'s own variables keep their value in runs.json; others are <redacted>')
    ap.add_argument('--record-env', nargs='+', action='append', default=[], metavar='NAME',
                    help='also record the value of these variables in runs.json (only for values that are not secret)')
    ap.add_argument('--hash-path', action='append', default=[], type=Path, metavar='DIR',
                    help='add a directory outside --cwd (a sibling package the tests import) to code_hash; repeatable')
    ap.add_argument('--npm-script', metavar='NAME', help="reuse package.json's script NAME (its env prefix and runner "
                                                           'flags) with the jest, vitest, playwright or node-test preset')
    ap.add_argument('--repo-root', type=Path, metavar='DIR',
                    help='run in --cwd but hash DIR and record test ids and files relative to it (prefixed with '
                         "--cwd's path inside DIR), so a monorepo's packages share one code_hash and never pool ids")
    ap.add_argument('--runner-args', help='extra flags appended to the preset command, e.g. "--configLoader native"')
    ap.add_argument('--label', default='run')
    ap.add_argument('--run-prefix', default='R')
    ap.add_argument('--check-tree', action='store_true')
    args, extra = ap.parse_known_args(argv)
    stray = [x for x in extra if ENV_PAIR.match(x)]
    if [x for x in extra if not ENV_PAIR.match(x)] or (stray and not args.env):
        ap.error('unrecognized arguments: ' + ' '.join(extra))
    args.env = env_pairs(args.env) + stray   # K=V pairs separated from --env by other flags
    keep = set(env_pairs(args.record_env))
    for hp in args.hash_path:
        if not (args.cwd / hp).is_dir():
            ap.error(f'--hash-path {hp}: not a directory (relative paths are taken from --cwd)')
    args.hash_path = [(args.cwd / hp).resolve() for hp in args.hash_path]
    args.id_prefix = ''
    if args.repo_root:
        rr, cw = args.repo_root.resolve(), args.cwd.resolve()
        if not rr.is_dir() or (rr != cw and rr not in cw.parents):
            ap.error(f'--repo-root {args.repo_root}: must be a directory that contains --cwd')
        args.id_prefix = '' if rr == cw else Path(os.path.relpath(cw, rr)).as_posix()
    bad = [kv for kv in args.env if not ENV_PAIR.match(kv)]
    if bad:
        ap.error(f'--env takes K=V pairs, not {bad[0]!r}')
    preset = PRESETS[args.preset]
    if preset['cmd'] is None and not args.cmd:
        ap.error('--preset custom needs --cmd')
    cwd = args.cwd.resolve()
    junit_dir = (args.junit_dir or args.out.parent / 'junit').resolve()
    junit_dir.mkdir(parents=True, exist_ok=True)

    tests = list(args.tests) + ([l.strip() for l in args.tests_file.read_text(encoding='utf-8').splitlines() if l.strip()]
                                if args.tests_file else [])
    if args.id_prefix:   # ids recorded with --repo-root carry the package path; the runner wants them without it
        tests = [t[len(args.id_prefix) + 1:] if t.startswith(args.id_prefix + '/') else t for t in tests]
    env = dict(os.environ)
    notes, variation = [], {'order': args.order, 'seed': None, 'hash_seed': args.hash_seed, 'workers': args.workers,
                            'tz': args.tz, 'tz_applied': None, 'locale': args.locale, 'cpu_load': args.cpu_load,
                            'isolate': args.isolate, 'env': {}}
    args.script_args = []
    runner = 'node' if args.preset == 'node-test' else Path(NODE_RUNNERS.get(args.preset, '')).name
    if args.npm_script:
        if (args.preset not in NODE_RUNNERS and args.preset != 'node-test') or args.cmd:
            ap.error('--npm-script works with the jest, vitest, playwright and node-test presets (without --cmd)')
        try:
            s_env, args.script_args, script = npm_script(cwd, args.npm_script, runner)
        except (OSError, ValueError) as e:
            ap.error(str(e))
        for k, v in s_env.items():
            env[k] = v
            variation['env'][k] = recorded_env(k, v, keep)
        notes.append(f'From npm script "{args.npm_script}" ({scrub(script)}): env '
                     + (', '.join(f'{k}={recorded_env(k, v, keep)}' for k, v in s_env.items()) or 'none') + '; runner flags '
                     + (' '.join(args.script_args) or 'none') + '.')
        if re.search(r'&&|\|\||;', script):
            msg = (f'npm script "{args.npm_script}" chains several commands; only the test runner was reproduced. '
                   'Run the other steps (a build of workspace packages the tests import from dist/, for example) '
                   'before these runs if the tests depend on them.')
            notes.append(msg)
            print('NOTE: ' + msg)
    elif (args.preset in NODE_RUNNERS or args.preset == 'node-test') and not args.cmd and (cwd / 'package.json').is_file():
        try:
            s_env, s_args, script = npm_script(cwd, 'test', runner)
            given = {kv.partition('=')[0] for kv in args.env} | ({'TZ'} if args.tz else set())
            left_env = [f'{k}={recorded_env(k, v, keep)}' for k, v in s_env.items() if k not in given]
            left_args = [a for a in s_args if a not in shlex.split(args.runner_args or '')]
            if left_env or left_args:
                parts = ([f'env {", ".join(left_env)}'] if left_env else []) + \
                        ([f'runner flags {" ".join(left_args)}'] if left_args else [])
                msg = (f'npm test ({scrub(script)}) also sets {" and ".join(parts)}; this run did not use them '
                       '(--npm-script test would).')
                notes.append(msg)
                print('NOTE: ' + msg)
        except (OSError, ValueError):
            pass
    for kv in args.env:
        k, _, v = kv.partition('=')
        env[k] = v
        variation['env'][k] = recorded_env(k, v, keep)
    if args.tz:
        rt = runtime_of(args.preset, args.cmd)
        try:
            env['TZ'], note = tz_value(args.tz, rt)
        except ValueError as e:
            ap.error(str(e))
        if note:
            notes.append(note)
        applied, pnote = probe_tz(rt, env['TZ'], env, cwd)
        if applied is False:
            ap.error(pnote + '; record the time-zone variation as not_run')
        notes.append(pnote)
        variation['tz_applied'] = env['TZ']
    if args.locale:
        env['LANG'] = env['LC_ALL'] = args.locale
        if os.name == 'nt':
            notes.append(f'LANG/LC_ALL set to {args.locale}; many runtimes on Windows ignore them.')
    if args.hash_seed and args.hash_seed != 'random':
        if preset.get('seed_env') != 'PYTHONHASHSEED' and args.preset != 'custom':
            ap.error(f'--hash-seed applies to Python runners only, not {args.preset}')
        env['PYTHONHASHSEED'] = str(args.hash_seed)
    if args.workers:
        if 'workers' not in preset:
            ap.error(f'{args.preset} has no worker flag; use --preset custom')
        if preset.get('workers_check') and not has_module(preset['workers_check'], cwd, env):
            ap.error(f'--workers needs {preset["workers_check"]} installed for {args.preset}; record the parallel variation as not_run')
    if args.order != 'given' and preset.get('order') != 'explicit_ids' and 'shuffle' not in preset:
        ap.error(f'{args.preset} cannot reorder tests; record the order variation as not_run')
    if args.order == 'reverse' and preset.get('order') != 'explicit_ids':
        ap.error(f'{args.preset} supports shuffle, not reverse (the runner orders files itself); use --order shuffle '
                 'with several seeds, or record the reverse-order variation as not_run')
    if preset.get('order') == 'explicit_ids' and (args.order != 'given' or args.isolate):
        if args.preset == 'pytest' and has_module('pytest_randomly', cwd, env):
            preset = {**preset, 'cmd': preset['cmd'] + ['-p', 'no:randomly']}
        tests, id_note = collect_pytest(cwd, tests, env)
        if id_note:
            notes.append(id_note)
            print('NOTE: ' + id_note)

    data = {'runs': [], 'cases': []}
    if args.out.exists():
        data = json.loads(args.out.read_text(encoding='utf-8'))
    existing = {r['id'] for r in data['runs']}
    n0 = sum(1 for r in data['runs'] if r['id'].startswith(args.run_prefix + '-'))
    hash_notes, basis = [], {}
    hash_root = args.repo_root.resolve() if args.repo_root else cwd
    chash, digests = hash_trees(hash_root, args.hash_path, hash_notes, basis)
    if args.repo_root:
        basis['root'] = '.' if hash_root == cwd else 'repo-root'
        data['id_prefix'] = args.id_prefix
    data['code_hash_basis'] = basis
    for n in hash_notes:
        print('WARN: ' + n)
    if hash_notes:
        data.setdefault('warnings', []).extend(f'{args.label}: {n}' for n in hash_notes)
    before = tree_snapshot(cwd, {junit_dir, args.out.resolve().parent}) if args.check_tree else None
    deadline = time.monotonic() + args.minutes * 60
    batches = [[t] for t in tests] if args.isolate else [tests]
    done, planned, stopped = 0, args.repeat * len(batches), False
    for i in range(args.repeat):
        for batch in batches:
            if time.monotonic() > deadline:
                stopped = True
                break
            seed = args.seed if args.seed is not None else random.SystemRandom().randrange(1, 2**31)
            order = list(batch)
            if args.order == 'reverse':
                order.reverse()
            elif args.order == 'shuffle' and preset.get('order') == 'explicit_ids':
                random.Random(seed).shuffle(order)
            run_id = f'{args.run_prefix}-{n0 + done + 1}'
            while run_id in existing:
                n0 += 1
                run_id = f'{args.run_prefix}-{n0 + done + 1}'
            run, cases = run_one(args, preset, run_id, order, env, junit_dir, cwd,
                                 seed if args.order == 'shuffle' else None, notes)
            run['variation'] = {**variation, 'seed': seed if args.order == 'shuffle' else None}
            run['code_hash'] = chash
            data['runs'].append(run)
            data['cases'] += cases
            existing.add(run_id)
            done += 1
            t = run['totals']
            print(f"{run_id} [{args.label}] {t['passed']} passed, {t['failed']} failed, {t['error']} error, "
                  f"{t['skipped']} skipped; exit {run['exit_code']}; {run['duration_s']}s"
                  + (' TIMEOUT' if run['timed_out'] else '') + (f" ERRORED: {run['error']}" if run['errored'] else ''))
        if stopped:
            break
    if getattr(args, 'ids_dir', None):
        shutil.rmtree(args.ids_dir, ignore_errors=True)
    errored = [r['id'] for r in data['runs'][len(data['runs']) - done:] if r.get('errored')] if done else []
    if errored:
        print(f'WARN: {len(errored)} of {done} run(s) errored and did not run the tests: {", ".join(errored[:10])}')
    if stopped:
        print(f'Stopped at the {args.minutes}-minute cap after {done} of {planned} planned run(s).')
        data.setdefault('stops', []).append({'label': args.label, 'completed': done, 'planned': planned,
                                             'minutes': args.minutes})
    after_hash, after_digests = hash_trees(hash_root, args.hash_path)
    if after_hash != chash:
        moved = sorted(k for k in set(digests) | set(after_digests) if digests.get(k) != after_digests.get(k))
        shown = ', '.join(moved[:10]) + (f' and {len(moved) - 10} more' if len(moved) > 10 else '')
        data.setdefault('warnings', []).append(f'{args.label}: source files changed during the runs: {shown}')
        print(f'WARN: source files changed during the runs ({shown}); these runs do not share one code version.')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    if before is not None:
        after = tree_snapshot(cwd, {junit_dir, args.out.resolve().parent})
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        if changed:
            print('Working tree changed: ' + ', '.join(changed[:20]))
            return 3
    return 0


if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
