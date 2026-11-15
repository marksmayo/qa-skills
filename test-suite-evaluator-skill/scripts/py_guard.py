#!/usr/bin/env python3
"""Run any Python test command with the network guard loaded into every Python process it starts (stdlib only).

    python py_guard.py run --log <scratch>/net_guard.log -- python -m unittest discover -s tests
    python py_guard.py env --log <scratch>/net_guard.log      # the variables to set yourself
    python py_guard.py --self-test

net_guard.py loads as a pytest plugin (`-p net_guard`), which guards the pytest process and its xdist workers
only: a suite run with `python -m unittest` (or nose, or a plain script), and the child Python processes the tests
start (`subprocess.run([sys.executable, "scripts/x.py"])`), are not guarded by it. This wrapper puts the folder
py_guard_site/ first on PYTHONPATH. It holds only a sitecustomize.py, which Python imports at start-up in every
process that inherits the variable; that file loads net_guard.py from this folder (without adding scripts/ to
sys.path, so no helper module here can shadow one of the tests'), then runs the sitecustomize it hides, if the
interpreter had one. QA_NET_GUARD_LOG is set, so every guarded process writes `net_guard: loaded pid=<pid>` and each
refusal `net_guard: refused <host>:<port>` to the log.

Limits, to state with the result: a process started with `python -I`, `-E` or `-S`, or with an environment that
drops PYTHONPATH, is not guarded (and logs no `loaded` line: count the lines against the processes you expect);
non-Python children are not guarded at all (use net_guard_proxy.py for them); and the guard covers the socket
module (connect, create_connection, DNS lookups), not a C extension that opens its own sockets.
"""
from __future__ import annotations
import argparse, os, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITE = HERE / 'py_guard_site'


def guard_env(log: str | None, environ: dict | None = None) -> dict:
    """The environment for a guarded run: PYTHONPATH starting with py_guard_site/, QA_NET_GUARD_LOG set."""
    env = dict(os.environ if environ is None else environ)
    old = env.get('PYTHONPATH')
    env['PYTHONPATH'] = str(SITE) + (os.pathsep + old if old else '')
    if log:
        env['QA_NET_GUARD_LOG'] = str(Path(log).resolve())
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
    args = ap.parse_args(argv)
    env = guard_env(args.log)
    if args.action == 'env':
        print(f'PYTHONPATH={env["PYTHONPATH"]}')
        print(f'QA_NET_GUARD_LOG={env["QA_NET_GUARD_LOG"]}')
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
