#!/usr/bin/env python3
"""Rust entry points, CLI flags and review leads for recon.py, from the shared Cargo model (cargo_project.py).

    python rust_entries.py <repo>      # prints the entry points, flags and leads as JSON

- Each clap CLI (derive or builder) is an entry point at its Parser struct or Command::new line (method ROOT), and
  each subcommand one at its variant or command line (method COMMAND); a binary with no clap CLI is one entry point
  at its fn main. `use` lines are never handlers.
- Every clap argument is a CLI flag, with the environment variable bound to it (`#[arg(env = "X")]`, `.env("X")`).
- A runtime environment variable that chooses an executable or a library (read within 15 lines of a process
  launch, a library load or a working-directory path, or named like one: *_BIN, *_EXE, *_LIB, *_DLL, *_PLUGIN,
  *_PYTHON, ...) is a config_input entry point (method ENV) and a review lead.
- Process launches and library loads become review leads naming where the program or library is found: a path
  built from env::current_dir(), a relative path (also through a const or a `let` default), a DLL by bare name.
Crates are read from the root Cargo.toml (a workspace or a single crate) and from crate directories below a root
that has none. Test fixture binaries (tests/fixtures) and test targets are not entry points.
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cargo_project  # noqa: E402
import scan_pitfalls  # noqa: E402

TEST_DIR = re.compile(r'/(?:tests|benches|examples)/')
LOCAL = 'local invocation: whoever runs the binary supplies its arguments, environment and working directory'
CHOOSER_NAME = re.compile(r'(?i)(?:^|_)(?:BIN|BINARY|EXE|EXECUTABLE|LIB|LIBRARY|DLL|SO|DYLIB|PLUGINS?|PYTHON|NODE|'
                          r'INTERPRETER|HELPER|TOOL|COMMAND|CMD|PROGRAM)$')
SYSTEM_VARS = {'PATH', 'PATHEXT', 'HOME', 'USERPROFILE', 'TEMP', 'TMP', 'TMPDIR', 'APPDATA', 'LOCALAPPDATA', 'SYSTEMROOT',
               'WINDIR', 'PROGRAMFILES', 'CARGO_MANIFEST_DIR', 'OUT_DIR'}  # the platform's own, not the tool's configuration
NEAR = 30  # lines after an environment read in which a launch, a load or a program/library path counts as its use
BINARY_LITERAL = re.compile(r'"[^"\s]*\.(exe|dll|so|dylib)(?:\.\d+)*"', re.I)


def crate_roots(root: Path, manifests: list[str]) -> list[str]:
    """'' when the root has a Cargo.toml; otherwise the top-most directories holding one (given repo-relative)."""
    if (root / 'Cargo.toml').is_file():
        return ['']
    dirs = sorted({str(Path(m).parent.as_posix()) for m in manifests if Path(m).name == 'Cargo.toml'}, key=len)
    out: list[str] = []
    for d in dirs:
        if d in ('', '.') or any(d.startswith(o + '/') for o in out):
            continue
        out.append(d)
    return out


def literals(text: str, expr: str, depth: int = 0) -> list[str]:
    """String literals an expression names: itself, a const/static it names, or the right-hand side of the `let`
    that bound it (one level), e.g. `&path` where `let path = env::var(..).unwrap_or_else(|_| LIB.to_string())`."""
    expr = re.sub(r'\b(?:std::)?env::var(?:_os)?\s*\(\s*"[^"]*"\s*\)', '', expr)  # the variable's name is not a path
    found = re.findall(r'"([^"]*)"', expr)
    if depth > 2:
        return found
    for name in re.findall(r'\b([A-Za-z_]\w*)\b', re.sub(r'"[^"]*"', '', expr)):
        c = re.search(rf'\b(?:const|static)\s+{re.escape(name)}\s*:[^=]*=\s*"([^"]*)"', text)
        if c:
            found.append(c.group(1))
            continue
        let = re.search(rf'\blet\s+(?:mut\s+)?{re.escape(name)}\b[^=]*=\s*([^;]+);', text)
        if let and name not in ('env', 'var', 'std'):
            found += literals(text, let.group(1), depth + 1)
    return found


def _main_line(path: Path) -> int | None:
    try:
        for i, line in enumerate(path.read_text(encoding='utf-8', errors='replace').splitlines(), 1):
            if re.match(r'^\s*(?:pub\s+)?(?:async\s+)?fn\s+main\s*\(', line):
                return i
    except OSError:
        pass
    return None


def read(root: Path, manifests: list[str] | None = None) -> dict:
    """{'entries': [...], 'cli_flags': [...], 'leads': [...], 'crates': n, 'notes': [...]}"""
    entries, flags, leads, notes = [], [], [], []
    texts: dict[str, str] = {}
    test_lines: dict[str, set[int]] = {}

    def text_of(rel: str) -> str:
        if rel not in texts:
            try:
                texts[rel] = (root / rel).read_text(encoding='utf-8', errors='replace')
            except OSError:
                texts[rel] = ''
        return texts[rel]

    ncrates = 0
    for base in crate_roots(root, manifests or []):
        model = cargo_project.read_cargo(root / base if base else root)
        pre = f'{base}/' if base else ''
        notes += [f'{pre}{n}' if base else n for n in model['notes']]
        for c in model['crates']:
            ncrates += 1
            cli_files = set()
            for cli in c['clis']:
                f = pre + cli['file']
                cli_files.add(f)
                entries.append({'kind': 'cli', 'framework': 'clap', 'method': 'ROOT' if not cli.get('parent') else 'COMMAND',
                                'path': cli['name'] or cli['bin'], 'handler': f"{f}:{cli['line']}", 'function': cli.get('struct'),
                                'auth': 'unknown', 'auth_evidence': LOCAL})
                for cmd in cli['commands']:
                    entries.append({'kind': 'cli', 'framework': 'clap', 'method': 'COMMAND', 'path': cmd['name'],
                                    'handler': f"{pre}{cmd['file']}:{cmd['line']}", 'function': cmd.get('variant'),
                                    'auth': 'unknown', 'auth_evidence': LOCAL})
                for a in cli['args']:
                    flags.append({'file': pre + a['file'], 'line': a['line'], 'name': a['long'] or a['name'],
                                  'env': [a['env']] if a.get('env') else [], 'framework': 'clap'})
            for t in c['targets']:
                if t['kind'] != 'bin' or not t.get('path'):
                    continue
                f = pre + t['path']
                if f in cli_files or any(x.get('bin') == t['name'] for x in c['clis']):
                    continue
                ln = _main_line(root / f)
                if ln:
                    entries.append({'kind': 'cli', 'framework': 'rust', 'method': 'ROOT', 'path': t['name'], 'handler': f'{f}:{ln}',
                                    'function': 'main', 'auth': 'unknown', 'auth_evidence': LOCAL})
            def shipped(x: dict) -> bool:  # not in tests/, benches/, examples/ or a #[cfg(test)] item
                f = pre + x['file']
                if TEST_DIR.search('/' + x['file']):
                    return False
                if f not in test_lines:
                    test_lines[f] = scan_pitfalls.rust_test_lines(text_of(f))
                return x['line'] not in test_lines[f]
            launches = [(pre + x['file'], x['line'], 'executable', x['program_expr']) for x in c['process_launches'] if shipped(x)]
            loads = [(pre + x['file'], x['line'], 'library', x['path_expr']) for x in c['library_loads'] if shipped(x)]
            relp = [(pre + x['file'], x['line'], x['used_for'], x['expr'], x.get('base')) for x in c['relative_paths'] if shipped(x)]
            c = {**c, 'env_reads': [x for x in c['env_reads'] if shipped(x)]}
            uses = launches + loads + [(f, l, u, e) for f, l, u, e, _ in relp if u in ('executable', 'library')]
            for f in sorted({pre + x['file'] for x in c['env_reads']}):  # "tools/x.exe", "lib/x.so": a program or library path
                for k, line in enumerate(text_of(f).splitlines(), 1):
                    for lit in BINARY_LITERAL.finditer(line):
                        uses.append((f, k, 'library' if lit.group(1).lower() != 'exe' else 'executable', lit.group(0)))
            seen = set()
            for r in c['env_reads']:
                f = pre + r['file']
                if r['kind'] == 'runtime' and not r.get('name') and r.get('expr'):  # env::var(SOME_CONST)
                    names = [x for x in literals(text_of(f), r['expr']) if re.fullmatch(r'[A-Za-z_]\w*', x)]
                    r = {**r, 'name': names[0] if names else None}
                if r['kind'] != 'runtime' or not r.get('name') or r['name'].upper() in SYSTEM_VARS:
                    continue
                near = [u for u in uses if u[0] == f and 0 <= u[1] - r['line'] <= NEAR]
                if not near and not CHOOSER_NAME.search(r['name']):
                    continue
                if (f, r['name']) in seen:
                    continue
                seen.add((f, r['name']))
                used = near[0][2] if near else ('library' if re.search(r'(?i)LIB|DLL|SO$|DYLIB|PLUGIN', r['name']) else 'executable')
                entries.append({'kind': 'config_input', 'framework': 'rust', 'method': 'ENV', 'path': r['name'],
                                'handler': f"{f}:{r['line']}", 'function': None, 'auth': 'unknown',
                                'auth_evidence': f"environment variable {r['name']} chooses the {used} the program "
                                                 f"{'runs' if used == 'executable' else 'loads'}"})
                leads.append({'area': 'configuration', 'entry_point': None, 'handler': f"{f}:{r['line']}",
                              'why': f"{f}:{r['line']} reads {r['name']}, which chooses the {used} the program "
                                     f"{'runs' if used == 'executable' else 'loads'}: whatever sets the environment (a "
                                     "launcher, a CI job, a .env loader, a shell profile) chooses the code that runs; check "
                                     "it is documented as trusted configuration, never taken from a project or document the "
                                     "tool opens, and that a relative value is not resolved against the working directory"})
            for f, ln, used, expr, b in relp:
                if b == 'current_dir()':
                    leads.append({'area': 'language_specific', 'entry_point': None, 'handler': f'{f}:{ln}',
                                  'why': f"{f}:{ln} builds a path from env::current_dir() (used for {'an ' if used == 'executable' else 'a '}{used}): "
                                         'the working directory is wherever the user started the tool (a cloned repository, a '
                                         'download folder), so a file planted there runs (CWE-427); resolve from current_exe() '
                                         'or a configured absolute path'})
            for f, ln, used, expr in launches + loads:
                lits = [x for x in literals(text_of(f), expr) if x]
                rel = [x for x in lits if not re.match(r'^(/|\\|[A-Za-z]:[\\/])', x) and ('/' in x or '\\' in x or x.startswith('.'))]
                bare_dll = [x for x in lits if used == 'library' and re.fullmatch(r'[^/\\]+\.dll', x)]
                if rel:
                    leads.append({'area': 'language_specific', 'entry_point': None, 'handler': f'{f}:{ln}',
                                  'why': f"{f}:{ln} {'runs' if used == 'executable' else 'loads'} {', '.join(repr(x) for x in rel)}, "
                                         'a path relative to the working directory: a file planted in the folder the tool is '
                                         'started from runs instead (CWE-427)'})
                elif bare_dll:
                    leads.append({'area': 'language_specific', 'entry_point': None, 'handler': f'{f}:{ln}',
                                  'why': f"{f}:{ln} loads {', '.join(repr(x) for x in bare_dll)} by bare name: on Windows the "
                                         'standard DLL search order includes the application folder, the PATH and the working '
                                         'directory (CWE-427); load by absolute path or with LOAD_LIBRARY_SEARCH_* flags'})
            if launches or loads:
                where = ', '.join(f'{f}:{ln}' for f, ln, _, _ in (launches + loads)[:8])
                leads.append({'area': 'injection', 'entry_point': None, 'handler': None,
                              'why': f"{len(launches)} process launch(es) and {len(loads)} library load(s) in crate {c['name']} "
                                     f"({where}{', ...' if len(launches) + len(loads) > 8 else ''}): check where each program "
                                     'or library is found (PATH, the working directory, an environment variable) and which '
                                     'arguments come from input'})
    for l in leads:
        l.pop('handler', None)
    return {'entries': entries, 'cli_flags': flags, 'leads': leads, 'crates': ncrates, 'notes': notes}


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    print(json.dumps(read(Path(sys.argv[1]).resolve()), indent=2))
