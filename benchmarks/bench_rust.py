"""Benchmarks for the Rust and Cargo support: the shared Cargo reader, libtest parsing and the line-based census
on a generated workspace, and the Python interface passes that share one walked node list per module."""
from __future__ import annotations

import pytest

import cargo_project
import lang_census
import parse_results
from conftest import ROOT, load_script

CRATES, MODULES, FNS, TESTS = 6, 8, 10, 6


def _module(name: str, m: int) -> str:
    body = [f'//! Module {m} of {name}.', 'use std::process::Command;', '', f'pub const LIMIT: u32 = {m * 11 + 3};', '']
    for f in range(FNS):
        body += [f'/// Scales reading {f}.', f'pub fn scale_{f}(x: u32) -> u32 {{', '    let y = x.min(LIMIT) as u16;',
                 f'    u32::from(y) * {f + 1}', '}', '']
    body += ['pub fn launch() -> std::io::Result<std::process::ExitStatus> {',
             '    Command::new("tools/plotter").arg("--fast").status()', '}', '',
             'pub fn raw(p: *const u8) -> u8 {', '    // SAFETY: callers pass a valid pointer.', '    unsafe { *p }', '}', '',
             '#[cfg(test)]', 'mod tests {', '    use super::*;']
    for t in range(TESTS):
        body += ['    #[test]', f'    fn scales_{t}() {{', f'        assert_eq!(scale_{t}(1), {t + 1});', '    }']
    body += ['    #[cfg(feature = "plot")]', '    #[test]', '    fn plots() {}', '}', '']
    return '\n'.join(body)


MAIN_RS = '''use clap::{Parser, Subcommand};

#[derive(Parser)]
struct Cli {
    #[arg(long, env = "TIDE_HOME")]
    home: Option<String>,
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    Sync {
        #[arg(long)]
        force: bool,
    },
    Show,
}

fn main() {
    let _ = Cli::parse();
}
'''


def _cargo_workspace(root) -> None:
    """Library crates with modules, inline #[cfg(test)] modules, a feature-gated test, an integration test that runs
    the binary, a clap CLI, unsafe FFI, process launches and a helper binary under tests/fixtures."""
    members = []
    for c in range(CRATES):
        name = f'tide-part{c}'
        members.append(f'crates/{name}')
        d = root / 'crates' / name
        (d / 'src').mkdir(parents=True)
        (d / 'tests' / 'fixtures').mkdir(parents=True)
        dep = f'tide-part{c - 1} = {{ path = "../tide-part{c - 1}" }}\n' if c else ''
        (d / 'Cargo.toml').write_text(
            f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n\n'
            f'[features]\ndefault = ["std"]\nstd = []\nplot = []\n\n[dependencies]\n{dep}'
            'clap = { version = "4", features = ["derive"] }\n', encoding='utf-8')
        (d / 'src' / 'lib.rs').write_text(''.join(f'pub mod m{m};\n' for m in range(MODULES)), encoding='utf-8')
        for m in range(MODULES):
            (d / 'src' / f'm{m}.rs').write_text(_module(name, m), encoding='utf-8')
        (d / 'src' / 'main.rs').write_text(MAIN_RS, encoding='utf-8')
        (d / 'tests' / 'cli.rs').write_text(
            '#[test]\nfn prints_help() {\n    let exe = env!("CARGO_BIN_EXE_' + name + '");\n'
            '    let out = std::process::Command::new(exe).arg("--help").output().unwrap();\n'
            '    assert!(out.status.success());\n}\n', encoding='utf-8')
        (d / 'tests' / 'fixtures' / 'slow_gauge.rs').write_text(
            'fn main() {\n    std::thread::sleep(std::time::Duration::from_millis(5));\n}\n', encoding='utf-8')
    (root / 'Cargo.toml').write_text(
        '[workspace]\nresolver = "2"\nmembers = [' + ', '.join(f'"{m}"' for m in members) + ']\n', encoding='utf-8')


@pytest.fixture(scope='module')
def cargo_workspace(tmp_path_factory):
    root = tmp_path_factory.mktemp('cargo-ws')
    _cargo_workspace(root)
    return root


def test_cargo_project_read(benchmark, cargo_workspace):
    """The static Cargo reader (crates, targets, tests, clap CLIs, unsafe, launches) on a generated workspace."""
    model = benchmark(cargo_project.read_cargo, cargo_workspace)
    assert model['is_cargo'] and len(model['crates']) == CRATES
    assert sum(len(c['tests']) for c in model['crates']) >= CRATES * MODULES * TESTS


def _libtest_log(binaries: int = 12, tests: int = 60) -> str:
    """cargo test output: Running lines per binary, ok/FAILED/ignored results, a panic and the summaries."""
    out = ['   Compiling tide-part0 v0.1.0 (/ws/crates/tide-part0)', '    Finished `test` profile in 9.81s']
    for b in range(binaries):
        out += ['', f'     Running unittests src/lib.rs (target/debug/deps/tide_part{b}-0123456789abcdef)', '',
                f'running {tests} tests']
        ignored = 0
        for t in range(tests):
            if t == 7 and b % 4 == 0:
                status = 'FAILED'
            elif t % 17 == 0:
                status, ignored = 'ignored', ignored + 1
            else:
                status = 'ok'
            out.append(f'test m{t % 8}::tests::scales_{t} ... {status}')
        failed = 1 if b % 4 == 0 else 0
        if failed:
            out += ['', 'failures:', '', '---- m7::tests::scales_7 stdout ----',
                    f"thread 'm7::tests::scales_7' panicked at crates/tide-part{b}/src/m7.rs:42:9:",
                    'assertion `left == right` failed', '  left: 7', ' right: 8', '', 'failures:',
                    '    m7::tests::scales_7', '']
        out.append(f'test result: {"FAILED" if failed else "ok"}. {tests - failed - ignored} passed; {failed} failed; '
                   f'{ignored} ignored; 0 measured; 0 filtered out; finished in 0.03s')
    return '\n'.join(out) + '\n'


LIBTEST = _libtest_log()


def test_parse_libtest(benchmark):
    """cargo test output read per test (ids, panics with file and line), as every Rust run and CI log is."""
    cases, _ = benchmark(parse_results.parse_libtest, LIBTEST, 'R1')
    assert len(cases) == 12 * 60
    assert any(c.get('status') == 'failed' for c in cases)


def test_lang_census_by_lines(benchmark, cargo_workspace):
    """The line-based census (product, test and tooling lines counted apart) on the generated workspace."""
    result = benchmark(lang_census.census, cargo_workspace)
    assert result['dominant'] == 'Rust' and result.get('dominant_by') == 'lines'


def test_list_interfaces_python_ferry_ops(benchmark):
    """Routes, mounts, middleware and command lines of a FastAPI and Azure Functions service: py_routes plus the
    py_extras passes, which share one walked node list per module."""
    li = load_script('test-suite-evaluator-skill', 'list_interfaces')
    root = ROOT / 'code-security-auditor-skill' / 'examples' / 'ferry-ops'
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file())
    result = benchmark(li.python_interfaces, root, files)
    assert result[0]
