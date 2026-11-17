#!/usr/bin/env python3
"""Run any Python test command with the network guard loaded into every Python process it starts (stdlib only).

    python py_guard.py run --log <scratch>/net_guard.log -- python -m unittest discover -s tests
    python py_guard.py env --log <scratch>/net_guard.log      # the variables to set yourself
    python py_guard.py run --log <scratch>/net_guard.log --home <scratch>/home -- python -m pytest   # scratch home
    python py_guard.py --self-test

net_guard.py loads as a pytest plugin (`-p net_guard`), which guards the pytest process and its xdist workers
only: a suite run with `python -m unittest` (or nose, or a plain script), and the child Python processes the tests
start (`subprocess.run([sys.executable, "scripts/x.py"])`), are not guarded by it. This wrapper puts the folder
py_guard_site/ first on PYTHONPATH. It holds only a sitecustomize.py, which Python imports at start-up in every
process that inherits the variable; that file loads net_guard.py from this folder (without adding scripts/ to
sys.path, so no helper module here can shadow one of the tests'), then runs the sitecustomize it hides, if the
interpreter had one. QA_NET_GUARD_LOG is set, so every guarded process writes `net_guard: loaded pid=<pid>` and each
refusal `net_guard: refused <host>:<port>` to the log. --home gives the run an empty home (HOME, USERPROFILE, XDG_*,
APPDATA, LOCALAPPDATA) but keeps CARGO_HOME, RUSTUP_HOME, Go's caches and the package-manager caches where they are
(net_guard_proxy.home_env()), so a test that shells out to cargo or go still finds its toolchain.

Limits, to state with the result: a process started with `python -I`, `-E` or `-S`, or with an environment that
drops PYTHONPATH, is not guarded (and logs no `loaded` line: count the lines against the processes you expect);
non-Python children are not guarded at all (use net_guard_proxy.py for them); and the guard covers the socket
module (connect, create_connection, DNS lookups), not a C extension that opens its own sockets.
"""
from __future__ import annotations
import argparse, os, shutil, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITE = HERE / 'py_guard_site'


def guard_env(log: str | None, environ: dict | None = None, home: str | None = None,
              real_home: Path | None = None) -> dict:
    """The environment for a guarded run: PYTHONPATH starting with py_guard_site/, QA_NET_GUARD_LOG set. With home,
    HOME, USERPROFILE, XDG_* and APPDATA/LOCALAPPDATA point into that (created) scratch directory, while CARGO_HOME,
    RUSTUP_HOME, GOPATH/GOCACHE/GOMODCACHE and the package-manager caches keep their real locations (as
    net_guard_proxy.py --home does), so a test that runs cargo or go still finds the toolchain."""
    env = dict(os.environ if environ is None else environ)
    old = env.get('PYTHONPATH')
    env['PYTHONPATH'] = str(SITE) + (os.pathsep + old if old else '')
    if log:
        env['QA_NET_GUARD_LOG'] = str(Path(log).resolve())
    if home:
        import importlib.util
        spec = importlib.util.spec_from_file_location('qa_net_guard_proxy', HERE / 'net_guard_proxy.py')
        ngp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ngp)
        Path(home).mkdir(parents=True, exist_ok=True)
        before = dict(env)
        env.update(ngp.home_env(home, real_home, before))
        if real_home is None:   # go's caches as go sees them now, before the home moves
            env.update({k: v for k, v in ngp.go_caches().items() if not before.get(k)})
    return env


def summary(log: str) -> dict:
    try:
        lines = Path(log).read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        lines = []
    return {'log': log, 'loaded': sum(1 for ln in lines if ln.startswith('net_guard: loaded')),
            'refused': [ln[len('net_guard: refused '):] for ln in lines if ln.startswith('net_guard: refused')]}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if '--self-test' in argv:
        return self_test()
    cmd = []
    if '--' in argv:
        i = argv.index('--')
        argv, cmd = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('action', choices=['run', 'env'])
    ap.add_argument('--log', required=True, help='the guard log (outside the checkout)')
    ap.add_argument('--home', help='an empty scratch directory to use as HOME/USERPROFILE (toolchain homes are kept)')
    args = ap.parse_args(argv)
    env = guard_env(args.log, home=args.home)
    if args.action == 'env':
        for k in ('PYTHONPATH', 'QA_NET_GUARD_LOG') + (('HOME', 'USERPROFILE', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
                                                        'XDG_DATA_HOME', 'XDG_STATE_HOME', 'APPDATA', 'LOCALAPPDATA',
                                                        'CARGO_HOME', 'RUSTUP_HOME', 'GOPATH', 'GOCACHE', 'GOMODCACHE',
                                                        'npm_config_cache', 'PIP_CACHE_DIR', 'GRADLE_USER_HOME',
                                                        'NUGET_PACKAGES') if args.home else ()):
            if env.get(k):
                print(f'{k}={env[k]}')
        return 0
    if not cmd:
        ap.error('give the command after --')
    code = subprocess.call(cmd, env=env)
    s = summary(env['QA_NET_GUARD_LOG'])
    print(f'py_guard: {s["loaded"]} guarded Python process(es), {len(s["refused"])} refused connection(s)'
          + (f': {", ".join(s["refused"][:5])}' if s['refused'] else '') + f' (log {s["log"]})', file=sys.stderr)
    if not s['loaded']:
        print('py_guard: WARNING: no `loaded` line: the guard never loaded (a -I/-E/-S interpreter, or PYTHONPATH '
              'dropped), so zero refusals proves nothing', file=sys.stderr)
    return code


SELF_TEST_CASE = '''import socket, subprocess, sys, unittest


class Reach(unittest.TestCase):
    def test_in_process(self):
        with self.assertRaises(OSError):
            socket.create_connection(("192.0.2.10", 80), timeout=1)

    def test_child_process(self):
        code = ("import socket\\n"
                "try:\\n    socket.create_connection(('198.51.100.20', 443), timeout=1)\\n    print('OUT')\\n"
                "except OSError:\\n    print('REFUSED')\\n")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60).stdout
        self.assertIn("REFUSED", out)
'''


def self_test() -> int:
    import tempfile
    fails = []
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / 'tests').mkdir()
        (Path(d) / 'tests' / 'test_reach.py').write_text(SELF_TEST_CASE, encoding='utf-8')
        (Path(d) / 'tests' / 'net_guard.py').write_text('raise SystemExit("the tests\' own module was shadowed")\n',
                                                        encoding='utf-8')
        log = str(Path(d) / 'guard.log')
        r = subprocess.run([sys.executable, str(Path(__file__).resolve()), 'run', '--log', log, '--',
                            sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'],
                           cwd=d, capture_output=True, text=True, timeout=120)
        s = summary(log)
        if r.returncode != 0:
            fails.append(f'a unittest run under py_guard passes when both connections are refused (exit '
                         f'{r.returncode}: {r.stderr[-400:]})')
        if s['loaded'] < 2:
            fails.append(f'the guard loads into the unittest process and into the child Python it starts '
                         f'(loaded lines: {s["loaded"]})')
        if not any('192.0.2.10:80' in x for x in s['refused']) or not any('198.51.100.20:443' in x for x in s['refused']):
            fails.append(f'both refusals are logged with their hosts (got {s["refused"]})')
        if 'refused connection(s)' not in r.stderr:
            fails.append('the run prints a summary of loaded processes and refusals')
        # a scratch home moves HOME and USERPROFILE but keeps the toolchain homes (rustup finds no toolchain under an
        # empty USERPROFILE: "could not choose a version of cargo"), as net_guard_proxy.py --home does
        fake_real = Path(d) / 'realhome'
        for sub in ('.cargo', '.rustup'):
            (fake_real / sub).mkdir(parents=True)
        home = Path(d) / 'scratch-home'
        try:
            env = guard_env(log, environ={'PATH': os.environ.get('PATH', '')}, home=str(home), real_home=fake_real)
        except TypeError as e:   # noqa: PERF203
            env = {'error': repr(e)}
        h = str(home.resolve())
        if not (env.get('HOME') == h and env.get('USERPROFILE') == h and home.is_dir()
                and env.get('APPDATA', '').startswith(h) and env.get('XDG_CACHE_HOME', '').startswith(h)):
            fails.append(f'guard_env(home=...) points HOME, USERPROFILE, APPDATA and XDG_* into the scratch home (got {env})')
        if env.get('CARGO_HOME') != str(fake_real / '.cargo') or env.get('RUSTUP_HOME') != str(fake_real / '.rustup'):
            fails.append(f'guard_env(home=...) keeps CARGO_HOME and RUSTUP_HOME at the real ones (got {env})')
        cargo = shutil.which('cargo')
        if cargo and subprocess.run([cargo, '--version'], capture_output=True, timeout=120).returncode == 0:
            r3 = subprocess.run([sys.executable, str(Path(__file__).resolve()), 'run', '--log', log, '--home',
                                 str(Path(d) / 'home2'), '--', cargo, '--version'], capture_output=True, text=True,
                                timeout=300)
            if r3.returncode != 0:
                fails.append(f'cargo still runs under py_guard.py run --home (exit {r3.returncode}: {r3.stderr[-300:]})')
        else:
            print('py_guard self-test: SKIP cargo under --home: no usable cargo here')
        r2 = subprocess.run([sys.executable, '-I', '-c', 'import sys; print("sitecustomize" in sys.modules)'],
                            env=guard_env(log), capture_output=True, text=True, timeout=60)
        if 'False' not in r2.stdout:
            fails.append('an isolated (-I) interpreter is not guarded, as the documented limit says')
    if fails:
        print('py_guard self-test FAILED:\n  - ' + '\n  - '.join(fails))
        return 1
    print('py_guard self-test: ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
