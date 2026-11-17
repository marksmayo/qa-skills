#!/usr/bin/env python3
"""Run a unittest suite the way `python -m unittest discover` does, with per-test results and optional
per-test line tracing (stdlib only). Used by builtin_mutator.py and prove_kill.py for --runner unittest.

    python unittest_runner.py <config.json>

The callers copy this file into a private folder and run it from the repository root, so nothing from
the skill's scripts folder can shadow a module of the repository: sys.path[0] is set to the working
directory, as `python -m unittest` does. The config (JSON) holds:

    start, pattern, top   discovery, as `-s`, `-p`, `-t` (default ".", "test*.py", none)
    ids                   run only these tests (ids `<file>::<Class>::<method>`); null runs every test;
                          [] runs none (a timing run of discovery alone)
    files                 run only tests defined in these files or folders (repository-relative)
    failfast              stop at the first failure (as -f)
    trace                 repository files whose executed lines are recorded per test ("<import>" while
                          modules are imported, before any test starts)
    cov_out, result_out   where the trace and the results are written

Test ids name the file that defines the test class, relative to the working directory, then the class
and the method: tests/test_levels.py::LevelsTest::test_band (the form pytest gives the same test). A
module that fails to import is a test with status "error" and the id `<module>::(import)`. Exit 0 when
every selected test passed (or was skipped), 1 when one failed or errored, 5 when none ran.
"""
import inspect
import json
import os
import sys
import threading
import unittest

_CFG = json.load(open(sys.argv[1], encoding='utf-8'))
sys.argv = sys.argv[:1]
sys.path[0] = os.getcwd()          # as `python -m unittest`: the working directory, never this file's folder
_FILES = {os.path.normcase(os.path.abspath(p)) for p in (_CFG.get('trace') or [])}
_hits = {}
_current = ['<import>']


def _tracer(frame, event, arg):
    f = os.path.normcase(os.path.abspath(frame.f_code.co_filename))
    if f not in _FILES:
        return None

    def local(frame, event, arg):
        if event == 'line':
            _hits.setdefault(_current[0], {}).setdefault(f, set()).add(frame.f_lineno)
        return local
    if event == 'call':
        _hits.setdefault(_current[0], {}).setdefault(f, set()).add(frame.f_lineno)
    return local


if _FILES:
    sys.settrace(_tracer)
    threading.settrace(_tracer)


def _rel(path):
    try:
        return os.path.relpath(os.path.abspath(path), os.getcwd()).replace(os.sep, '/')
    except ValueError:
        return path.replace(os.sep, '/')


def test_id(test):
    """<file>::<Class>::<method> for a TestCase; <module>::(import) for a module that failed to load."""
    cls = type(test)
    if cls.__name__ == '_FailedTest':
        return test.id().rsplit('.', 1)[0] + '::(import)'
    try:
        src = inspect.getsourcefile(cls) or inspect.getfile(cls)
    except (TypeError, OSError):
        src = cls.__module__.replace('.', '/') + '.py'
    method = getattr(test, '_testMethodName', None) or test.id().rsplit('.', 1)[-1]
    return f'{_rel(src)}::{cls.__qualname__}::{method}'


def flatten(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from flatten(t)
        else:
            yield t


class Result(unittest.TextTestResult):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.records = []

    def startTest(self, test):
        _current[0] = test_id(test)
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        _current[0] = '<import>'

    def _add(self, test, status, err=None):
        rec = {'id': test_id(test), 'status': status}
        if err is not None:
            rec['message'] = self._exc_info_to_string(err, test)[-600:] if isinstance(err, tuple) else str(err)[:600]
        self.records.append(rec)

    def addSuccess(self, test):
        super().addSuccess(test)
        self._add(test, 'passed')

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._add(test, 'failed', err)

    def addError(self, test, err):
        super().addError(test, err)
        self._add(test, 'error', err)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._add(test, 'skipped', reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._add(test, 'passed')

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._add(test, 'failed', 'unexpected success')

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            failed = issubclass(err[0], test.failureException)
            self._add(test, 'failed' if failed else 'error', err)


def main():
    loader = unittest.TestLoader()
    suite = loader.discover(_CFG.get('start') or '.', _CFG.get('pattern') or 'test*.py', _CFG.get('top'))
    tests = list(flatten(suite))
    ids = _CFG.get('ids')
    files = [f.rstrip('/') for f in (_CFG.get('files') or [])]
    chosen = []
    for t in tests:
        tid = test_id(t)
        path = tid.split('::', 1)[0]
        if ids is not None and tid not in ids and not (tid.endswith('::(import)')):
            continue
        if files and not any(path == f or path.startswith(f + '/') for f in files) and not tid.endswith('::(import)'):
            continue
        chosen.append(t)
    # A failure message may hold any character (an emoji in rendered HTML); a stream in the locale encoding (cp1252
    # on Windows) would raise while printing it and turn a real kill into a crashed run. So the runner's own stream
    # is UTF-8, and the process's stdout and stderr never raise on a character they cannot encode.
    for std in (sys.stdout, sys.stderr):
        if hasattr(std, 'reconfigure'):
            try:
                std.reconfigure(errors='backslashreplace')
            except (ValueError, OSError):
                pass
    with open(os.devnull, 'w', encoding='utf-8', errors='backslashreplace') as null:
        runner = unittest.TextTestRunner(stream=null, verbosity=0, failfast=bool(_CFG.get('failfast')),
                                         resultclass=Result)
        result = runner.run(unittest.TestSuite(chosen))
    sys.settrace(None)
    records = result.records
    # a test whose subtests failed is also reported passed by addSuccess; keep the worst status per id
    worst = {}
    rank = {'error': 3, 'failed': 2, 'passed': 1, 'skipped': 0}
    for r in records:
        if r['id'] not in worst or rank[r['status']] > rank[worst[r['id']]['status']]:
            worst[r['id']] = r
    out = {'tests': list(worst.values()), 'discovered': len(tests), 'selected': len(chosen)}
    if _CFG.get('result_out'):
        with open(_CFG['result_out'], 'w', encoding='utf-8') as fh:
            json.dump(out, fh)
    if _CFG.get('cov_out'):
        loaded = set()
        for mod in list(sys.modules.values()):
            f = getattr(mod, '__file__', None)
            if f:
                loaded.add(os.path.normcase(os.path.abspath(f)))
        tail = lambda p: p.replace(os.sep, '/').split('/')[-2:]  # noqa: E731
        modules = {p: {'loaded': p in loaded, 'other_copies': sorted(f for f in loaded if f != p and tail(f) == tail(p))}
                   for p in _FILES}
        bad = any(r['status'] in ('failed', 'error') for r in worst.values())
        with open(_CFG['cov_out'], 'w', encoding='utf-8') as fh:
            json.dump({'exitstatus': 1 if bad else (0 if worst else 5),
                       'hits': {t: {f: sorted(l) for f, l in fs.items()} for t, fs in _hits.items()},
                       'modules': modules}, fh)
    if any(r['status'] in ('failed', 'error') for r in worst.values()):
        return 1
    return 0 if worst else 5


if __name__ == '__main__':
    sys.exit(main())
