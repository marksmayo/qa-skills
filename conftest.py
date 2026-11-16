"""A bare `pytest` at the repository root runs the benchmarks, nothing else.

The repository's tests are the scripts' own `--self-test` runs (see CLAUDE.md), not pytest suites. Without this file
pytest walks every skill and collects each `scripts/self_test.py` (it matches `*_test.py`), whose `test_*(tmp)`
helpers then error on a missing `tmp` fixture, and the sample repositories under `examples/`, whose tests need their
own packages (and some are meant to fail).

This is a conftest.py rather than a root pytest.ini on purpose: pytest loads it only when the run's rootdir is this
directory (a bare `pytest` here). A pytest.ini here would become the inifile, and the rootdir, of every pytest run
inside an example that has no configuration of its own, changing its node ids and test-file patterns.
`pytest benchmarks` (CI) keeps using benchmarks/pytest.ini and never loads this file.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# everything but benchmarks/: the skills (self-tests and sample repositories), shared/ and tools/
collect_ignore = sorted(p.name for p in ROOT.iterdir() if p.is_dir() and p.name not in ('benchmarks',)
                        and not p.name.startswith('.'))


def pytest_configure(config):
    # benchmarks/pytest.ini is not the inifile when pytest starts here: give the run its file pattern
    if config.rootpath == ROOT:
        files = config.getini('python_files')
        if 'bench_*.py' not in files:
            files.append('bench_*.py')
