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


def _sources() -> dict[str, str]:
    """A realistic mix of Python sources from the repository (the shared scripts themselves)."""
    return {f'shared/scripts/{p.name}': p.read_text(encoding='utf-8') for p in sorted(
        (ROOT / 'shared' / 'scripts').glob('*.py'))}


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
    text = SOURCES['shared/scripts/run_tests.py']
    funcs = benchmark(diff_scope.py_functions, text)
    assert funcs


def test_diff_scope_js_functions(benchmark):
    js = read_tree(ROOT / 'ui-crash-hunter-skill' / 'examples' / 'pantry' / 'src', ('.js', '.jsx', '.ts', '.tsx'))
    texts = list(js.values())
    assert texts
    benchmark(lambda: [diff_scope.js_functions(t) for t in texts])


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
