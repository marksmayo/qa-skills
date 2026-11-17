#!/usr/bin/env python3
"""Run a command while the machine is busy starting processes and writing files (stdlib only).

    python spawn_load.py --procs 4 -- python run_tests.py --preset cargo --cwd <repo> --out runs.json \\
        --tests <id> --repeat 8 --label suite_load --run-prefix SL --record-env QA_SPAWN_LOAD
    python spawn_load.py --self-test

The suite-load variation of the flaky test hunter. A test that fails only in whole-suite runs and never alone,
not even under `run_tests.py --cpu-load`, is often waiting on something the rest of the suite slows down that a
busy CPU does not: starting a child process, loading a freshly written executable, the disk. `--cpu-load` spins
CPU-bound siblings; this script keeps `--procs` workers starting short-lived processes (and, unless
`--no-files`, writing and deleting a fresh file each time) for as long as the command runs, which is what the
other tests of a suite that launches programs do to the one under suspicion.

The command runs with QA_SPAWN_LOAD=<procs> in its environment; pass `--record-env QA_SPAWN_LOAD` to
run_tests.py so every run records it (the validator checks a `suite_load` variation's runs record it, or ran
the test beside others). The command's exit code is returned. It does not reproduce a particular machine: say
in the experiment what the load was (workers, processes started, seconds) from the line this prints.
"""
from __future__ import annotations
import argparse, os, shutil, subprocess, sys, tempfile, threading, time
from pathlib import Path

ENV_NAME = 'QA_SPAWN_LOAD'
FILE_BYTES = 1024 * 1024
SCRATCH: list[Path] = []      # the scratch folders this process made (the self-test checks they are gone)


def worker(stop: threading.Event, scratch: Path | None, counts: list[int], k: int) -> None:
    """Start short-lived interpreters one after another until told to stop; write and delete a fresh file each
    time when a scratch folder is given."""
    block = os.urandom(4096) * (FILE_BYTES // 4096)
    n = 0
    while not stop.is_set():
        if scratch is not None:
            p = scratch / f'load-{k}-{n}.bin'
            try:
                p.write_bytes(block)
                p.unlink()
            except OSError:
                pass
        try:
            subprocess.run([sys.executable, '-I', '-c', 'pass'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass
        n += 1
        counts[k] = n


def run_under_load(cmd: list[str], procs: int, files: bool = True) -> tuple[int, int, float]:
    """(the command's exit code, processes the workers started, seconds)."""
    scratch = Path(tempfile.mkdtemp(prefix='qa-spawn-load-')) if files else None
    if scratch is not None:
        SCRATCH.append(scratch)
    stop = threading.Event()
    counts = [0] * procs
    threads = [threading.Thread(target=worker, args=(stop, scratch, counts, k), daemon=True) for k in range(procs)]
    start = time.monotonic()
    for t in threads:
        t.start()
    try:
        code = subprocess.run(cmd, env={**os.environ, ENV_NAME: str(procs)}).returncode
    except OSError as exc:
        print(f'spawn_load: cannot start the command: {exc}', file=sys.stderr)
        code = 127
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=90)
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    return code, sum(counts), time.monotonic() - start


def self_test() -> int:
    probe = ('import os, sys, time; time.sleep(0.6); '
             f"sys.exit(0 if os.environ.get('{ENV_NAME}') == '2' else 3)")
    code, started, took = run_under_load([sys.executable, '-I', '-c', probe], 2)
    code7, _, _ = run_under_load([sys.executable, '-I', '-c', 'import sys; sys.exit(7)'], 1, files=False)
    left = [p for p in SCRATCH if p.exists()]
    checks = [(f'the command runs with {ENV_NAME} set to the worker count', code == 0),
              ('the workers start processes while the command runs', started >= 1),
              ("the command's exit code is returned", code7 == 7),
              ('the scratch folder is removed afterwards', bool(SCRATCH) and not left)]
    for name, ok in checks:
        print(('ok   ' if ok else 'FAIL ') + name)
    bad = [n for n, ok in checks if not ok]
    print('spawn_load self-test ' + ('FAILED' if bad else 'passed'))
    return 1 if bad else 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if '--self-test' in argv:
        return self_test()
    cmd: list[str] = []
    if '--' in argv:
        i = argv.index('--')
        argv, cmd = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 usage='spawn_load.py [--procs N] [--no-files] -- <command ...>')
    ap.add_argument('--procs', type=int, default=4, help='workers that keep starting processes (default 4)')
    ap.add_argument('--no-files', action='store_true', help='only start processes; write no scratch files')
    ap.add_argument('--self-test', action='store_true', help='run the built-in checks and exit')
    args = ap.parse_args(argv)
    if not cmd:
        ap.error('give the command to run after --')
    if args.procs < 1:
        ap.error('--procs must be 1 or more')
    code, started, took = run_under_load(cmd, args.procs, not args.no_files)
    print(f'spawn_load: {args.procs} worker(s) started {started} process(es)'
          + ('' if args.no_files else f' and wrote as many {FILE_BYTES // 1024} KiB files')
          + f' in {took:.1f} s while the command ran (exit {code})', file=sys.stderr)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
