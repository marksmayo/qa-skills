"""Benchmarks for the canonical shared scripts (shared/scripts/) that many skills ship copies of."""
from __future__ import annotations

import difflib
import zlib

import pytest

import diff_scope
import journey_md
import png_io
import py_routes
import sarif
import stats
import unified_diff
from conftest import ROOT, read_tree

JOURNEYS = sorted((ROOT / 'journey-mapper-skill' / 'examples' / 'journeys').rglob('JRN-*.md'))
FERRY_OPS = ROOT / 'code-security-auditor-skill' / 'examples' / 'ferry-ops'
SCREENSHOT = ROOT / 'bug-reproducer-skill' / 'examples' / 'run' / 'artifacts' / 'TASK-11' / 'A1-1' / 'screenshot.png'


def _module(seed: int, functions: int) -> str:
    """One generated Python module: imports, constants, classes with methods, decorated and nested functions,
    docstrings, comments and string literals, the shapes the diff and function parsers meet in real code."""
    out = [f'"""Generated module {seed} for the parser benchmarks."""', 'from __future__ import annotations', '',
           'import functools', 'import json', 'import re', 'from pathlib import Path', '',
           f'LIMIT_{seed} = {seed * 7 + 3}', "PATTERN = re.compile(r'^(?P<key>\\w+)=(?P<value>.*)$')", '']
    indent = ''
    for f in range(functions):
        if f % 5 == 0:
            out += ['', f'class Store{seed}_{f}:', f'    """Keeps records for area {f}."""', '',
                    '    def __init__(self, root: Path):', '        self.root = root',
                    '        self.items: dict[str, int] = {}', '']
            indent = '    '
        elif f % 5 == 4:
            indent = ''
            out.append('')
        static = bool(indent) and f % 3 == 0
        if static:
            out.append(f'{indent}@staticmethod')
        elif not indent and f % 3 == 0:
            out.append('@functools.lru_cache(maxsize=None)')
        args = 'self, ' if indent and not static else ''
        out += [f'{indent}def handle_{seed}_{f}({args}name: str, count: int = {f}) -> dict:',
                f'{indent}    """Return the summary for name after {f} steps."""',
                f'{indent}    total = 0  # running total',
                f'{indent}    for i in range(count):',
                f'{indent}        if i % {f % 4 + 2} == 0:',
                f'{indent}            total += i * {seed}',
                f'{indent}        elif name.startswith("item-{f}"):',
                f'{indent}            total -= len(name)',
                f'{indent}    def inner(value):',
                f'{indent}        return {{"name": name, "value": value, "limit": LIMIT_{seed}}}',
                f'{indent}    data = json.loads(\'{{"area": {f}, "ok": true}}\')',
                f'{indent}    return inner(total) | data', '']
    return '\n'.join(out) + '\n'


def _sources() -> dict[str, str]:
    """Generated Python sources of a fixed size (about the size of shared/scripts when the benchmarks were added).
    Generated rather than read from the repository, so the input stays the same as the repository grows and only
    a change to the parsers moves these numbers."""
    return {f'pkg/mod_{i:02d}.py': _module(i, 25) for i in range(24)}


def _make_diff(sources: dict[str, str]) -> str:
    """A git-style unified diff touching every file: every 7th line edited, every 11th deleted."""
    chunks = []
    for rel, text in sources.items():
        old = text.splitlines(keepends=True)
        new = []
        for i, line in enumerate(old):
            if i % 11 == 5:
                continue
            new.append(line.rstrip('\n') + '  # changed\n' if i % 7 == 3 else line)
        chunks.append(f'diff --git a/{rel} b/{rel}\n')
        chunks.extend(difflib.unified_diff(old, new, f'a/{rel}', f'b/{rel}', n=3))
    return ''.join(chunks)


SOURCES = _sources()
DIFF = _make_diff(SOURCES)


# --- stats -----------------------------------------------------------------------------------

def test_stats_wilson_grid(benchmark):
    def run():
        return [stats.rate_band(stats.wilson(k, n)[0]) for n in range(1, 120) for k in range(0, n + 1, 3)]
    assert benchmark(run)


# --- diff parsing ----------------------------------------------------------------------------

def test_diff_scope_parse_diff(benchmark):
    files = benchmark(diff_scope.parse_diff, DIFF)
    assert len(files) == len(SOURCES)


def test_diff_scope_py_functions(benchmark):
    text = _module(99, 125)   # one large module, the size of shared/scripts/run_tests.py when the benchmarks were added
    funcs = benchmark(diff_scope.py_functions, text)
    assert funcs


def test_diff_scope_js_functions(benchmark):
    js = read_tree(ROOT / 'ui-crash-hunter-skill' / 'examples' / 'pantry' / 'src', ('.js', '.jsx', '.ts', '.tsx'))
    texts = list(js.values())
    assert texts
    result = benchmark(lambda: [diff_scope.js_functions(t) for t in texts])
    assert len(result) == len(texts)
    for text, funcs in zip(texts, result):
        n = len(text.splitlines())
        assert all(1 <= f['start'] <= f['end'] <= n for f in funcs), funcs
    names = {f['name'] for funcs in result for f in funcs}
    # the pantry app's components, hooks and helpers (declarations, arrow functions, class methods)
    assert {'App', 'RecipeList', 'RecipeDetail', 'Settings', 'update', 'loadPrefs', 'savePrefs', 'render',
            'getDerivedStateFromError'} <= names, sorted(names)


def test_unified_diff_parse_patch(benchmark):
    files = benchmark(unified_diff.parse_patch, DIFF)
    assert len(files) == len(SOURCES)


# --- journey markdown ------------------------------------------------------------------------

def test_journey_md_parse_lint_render(benchmark):
    texts = [p.read_text(encoding='utf-8') for p in JOURNEYS]
    assert texts

    def run():
        out = []
        for t in texts:
            j = journey_md.parse_journey(t)
            out.append((journey_md.lint(j), journey_md.render(j)))
        return out
    benchmark(run)


# --- python route discovery ------------------------------------------------------------------

def test_py_routes_scan_python(benchmark):
    files = read_tree(FERRY_OPS, ('.py',))
    hosts = {k: v for k, v in read_tree(FERRY_OPS, ('.json',)).items() if k.endswith('host.json')}
    result = benchmark(py_routes.scan_python, files, hosts)
    assert result['routes']


# --- PNG ------------------------------------------------------------------------------------

def test_png_decode_screenshot(benchmark):
    data = SCREENSHOT.read_bytes()
    img = benchmark(png_io.decode, data)
    assert img.width and img.height


def test_png_encode_crop(benchmark):
    img = png_io.decode(SCREENSHOT.read_bytes())

    def run():
        return png_io.encode(png_io.crop(img, 0, 0, min(img.width, 400), min(img.height, 300)))
    assert benchmark(run).startswith(png_io.SIG)


@pytest.mark.parametrize('ctype', [6, 0])
def test_png_decode_rgba_and_grey(benchmark, ctype):
    """Decode paths that composite alpha or expand greyscale pixel by pixel."""
    w, h = 160, 120
    bpp = {6: 4, 0: 1}[ctype]
    raw = b''.join(bytes([i % 5]) + bytes((x * 7 + y * 3 + i) % 256 for x in range(w * bpp))
                   for i, y in enumerate(range(h)))
    import struct
    def chunk(t, b):
        return struct.pack('>I', len(b)) + t + b + struct.pack('>I', zlib.crc32(t + b) & 0xffffffff)
    data = (png_io.SIG + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, ctype, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))
    img = benchmark(png_io.decode, data)
    assert img.width == w and len(img.rows) == h


# --- SARIF ----------------------------------------------------------------------------------

def test_sarif_build(benchmark):
    results = [{'rule_id': f'RULE-{i % 40}', 'level': ('error', 'warning', 'note')[i % 3],
                'message': f'Finding {i} in a module', 'file': f'src/mod_{i % 25}.py', 'line': i % 400 + 1,
                'column': i % 30 + 1, 'cwe': [f'CWE-{79 + i % 5}'], 'tags': ['security']} for i in range(600)]
    log = benchmark(sarif.build, 'bench-tool', '1.0', results)
    assert log['runs'][0]['results']


# --- test results ----------------------------------------------------------------------------

JUNIT = sorted([*(ROOT / 'release-readiness-gate-skill' / 'examples' / 'rc-2026.11.2' / 'reports').glob('*.xml'),
                *(ROOT / 'api-contract-tester-skill' / 'examples' / 'run' / 'schemathesis').glob('*.xml'),
                *(ROOT / 'test-data-auditor-skill' / 'examples' / 'run' / 'junit').glob('*.xml'),
                ROOT / 'requirements-traceability-auditor-skill' / 'examples' / 'run' / 'junit.xml'])


def test_parse_results_junit(benchmark):
    """Every run in every skill is read through parse_junit: pytest, Vitest and Schemathesis reports."""
    import parse_results
    assert len(JUNIT) > 10

    def run():
        return [parse_results.parse_junit(p, f'R{i}') for i, p in enumerate(JUNIT)]
    assert any(cases for cases, _ in benchmark(run))


def test_next_app_read(benchmark):
    """The static Next.js reader on the security auditor's Next.js, Prisma and NextAuth fixture."""
    import next_app
    model = benchmark(next_app.read_next_app, ROOT / 'code-security-auditor-skill' / 'examples' / 'kite-notes')
    assert model['is_next'] and model['projects'][0]['counts']['route_handlers'] > 0
