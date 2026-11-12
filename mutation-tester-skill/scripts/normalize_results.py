#!/usr/bin/env python3
"""Normalise mutation engines' raw results into one mutant list, limited to the scope (stdlib only).

    python normalize_results.py --scope scope.json --repo <repo> --out mutants.json
        [--builtin builtin.json] [--mutmut mutmut.json] [--stryker mutation-report.json]
        [--coverage coverage.json]

Every mutant gets: id (B1.. builtin, M1.. mutmut, S1.. Stryker/PIT), engine, raw_id, file, line,
end_line, function, operator (data/operators.json), kind, original and mutated source lines,
status (killed, survived, no_coverage, timeout, runtime_error, compile_error, not_run),
engine_status, covering_tests and killed_by.

- Mutants outside the scope's changed lines are dropped and counted per engine.
- mutmut prints each mutant as a diff of its function; the line is mapped back to the file by
  finding the function with `ast` and checked against the file's text. Its operator is inferred
  from the tokens that differ. mutmut counts some non-test exits as killed; anything but exit
  code 1 becomes runtime_error, with the engine's status kept in engine_status.
- StrykerJS (mutation-testing-elements JSON): covering_tests and killed_by are test names from
  the report's testFiles (`<test file>::Describe > title`, the Vitest/Jest JUnit form, when the
  embedded test source shows the describe chain), never Stryker's numeric ids. The version
  comes from `framework`. --stryker-run (written by `run_engine.py stryker`) supplies the command,
  config source and the before/after tree check; without it tree_changed stays null. JS/TS
  mutants get their function from ts_scan.py.
- --coverage (builtin_mutator.py --coverage-only) gives per-test line coverage. mutmut only knows
  which tests call a function, so a mutmut mutant that survived on a line no test executed
  becomes no_coverage, marked reclassified_from "survived".
"""
from __future__ import annotations
import argparse, ast, io, json, re, sys, tokenize
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ts_scan  # noqa: E402
OPS = json.loads((HERE.parent / 'data' / 'operators.json').read_text(encoding='utf-8'))
CMP = {'<', '<=', '>', '>=', '==', '!=', 'is', 'is not', 'in', 'not in', '===', '!=='}
BOUNDARY = {frozenset(p) for p in (('<', '<='), ('>', '>='))}
ARITH = {'+', '-', '*', '/', '//', '%', '**', '+=', '-=', '*=', '/=', '//=', '%=', '**=', '&', '|', '^', '<<', '>>'}
STATUS_STRYKER = {'Killed': 'killed', 'Survived': 'survived', 'NoCoverage': 'no_coverage', 'Timeout': 'timeout',
                  'RuntimeError': 'runtime_error', 'CompileError': 'compile_error', 'Ignored': 'not_run',
                  'Pending': 'not_run'}
STATUS_MUTMUT = {'killed': 'killed', 'survived': 'survived', 'timeout': 'timeout', 'no tests': 'no_coverage',
                 'suspicious': 'runtime_error', 'segfault': 'runtime_error', 'caught by type check': 'compile_error',
                 'skipped': 'not_run', 'not checked': 'not_run', 'check was interrupted by user': 'not_run'}


def kind_of(op: str) -> str:
    return OPS['operators'].get(op, OPS['operators']['other'])['kind']


def toks(src: str) -> list[str]:
    try:
        return [t.string for t in tokenize.generate_tokens(io.StringIO(src.strip() + '\n').readline)
                if t.type not in (tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER,
                                  tokenize.COMMENT)]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src.split()


def infer_operator(original: str, mutated: str) -> str:
    a, b = toks(original), toks(mutated)
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    j = 0
    while j < min(len(a), len(b)) - i and a[len(a) - 1 - j] == b[len(b) - 1 - j]:
        j += 1
    da, db = a[i:len(a) - j], b[i:len(b) - j]
    sa, sb = ' '.join(da), ' '.join(db)
    if original.lstrip().startswith('def ') and '=' in original:
        return 'default_arg'
    if sb == 'None' and sa != 'None':
        return 'value_none'
    if sa in CMP and sb in CMP:
        return 'comparison_boundary' if frozenset((sa, sb)) in BOUNDARY else (
            'comparison_relational' if sb == '!=' else 'comparison_negate')
    if sa in ARITH | {'='} and sb in ARITH | {'='}:
        return 'arithmetic'
    if {sa, sb} <= {'and', 'or'} and sa != sb:
        return 'logical'
    if {sa, sb} <= {'True', 'False'} and sa != sb:
        return 'boolean_constant'
    if sb.startswith('not ') and sb[4:].strip('() ') == sa.strip('() ') or sa.startswith('not ') and sa[4:] == sb:
        return 'negate_condition'
    if da and db and all(re.fullmatch(r'[\d.]+[jJ]?', t) for t in da + db):
        return 'constant_number'
    if da and db and all(re.match(r'[rbuf]*["\']', t, re.I) for t in da + db):
        return 'constant_string'
    if {sa, sb} & {'break', 'continue', 'return'}:
        return 'control_flow'
    if len(b) < len(a) and not db:
        return 'call_args'
    return 'other'


def mutmut_func(key: str) -> tuple[str, str | None, str]:
    """fare_calc.pricing.x_total__mutmut_3 -> ('fare_calc/pricing', None, 'total'); methods use xǁClassǁname."""
    base = key.rsplit('__mutmut_', 1)[0]
    module, _, mangled = base.rpartition('.')
    if mangled.startswith('xǁ'):
        _, cls, name = mangled.split('ǁ', 2)
        return module.replace('.', '/'), cls, name
    return module.replace('.', '/'), None, mangled[2:] if mangled.startswith('x_') else mangled


def find_def(tree: ast.AST, cls: str | None, name: str):
    for node in ast.walk(tree):
        if cls and isinstance(node, ast.ClassDef) and node.name == cls:
            for n in node.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
                    return n
        if not cls and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def parse_function_diff(diff: str) -> tuple[int, list[str], list[str]] | None:
    """Return (offset of the first changed line within the function, removed lines, added lines)."""
    old_no, first, removed, added = None, None, [], []
    for line in diff.splitlines():
        m = re.match(r'^@@ -(\d+)', line)
        if m:
            old_no = int(m.group(1))
            continue
        if old_no is None or line.startswith(('---', '+++', '#')):
            continue
        if line.startswith('-'):
            first = old_no if first is None else first
            removed.append(line[1:])
            old_no += 1
        elif line.startswith('+'):
            first = old_no if first is None else first
            added.append(line[1:])
        else:
            old_no += 1
    return (first, removed, added) if first is not None else None


def enclosing_function(tree: ast.AST, line: int) -> str | None:
    best = None

    def walk(node, prefix):
        nonlocal best
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.lineno <= line <= child.end_lineno:
                    best = prefix + child.name
                walk(child, prefix + child.name + '.<locals>.')
            elif isinstance(child, ast.ClassDef):
                walk(child, prefix + child.name + '.')
    walk(tree, '')
    return best


class Sources:
    """File text, lines, and a function finder: Python's ast, or ts_scan for JS/TS."""
    def __init__(self, repo: Path):
        self.repo, self.cache, self.funcs = repo, {}, {}

    def get(self, rel: str):
        if rel not in self.cache:
            p = self.repo / rel
            text = p.read_text(encoding='utf-8') if p.is_file() else None
            tree = None
            if text is not None and rel.endswith('.py'):
                try:
                    tree = ast.parse(text)
                except SyntaxError:
                    pass
            self.cache[rel] = (text, text.splitlines() if text is not None else [], tree)
        return self.cache[rel]

    def function_at(self, rel: str, line: int) -> str | None:
        text, _, tree = self.get(rel)
        if tree is not None:
            return enclosing_function(tree, line)
        if text is not None and ts_scan.is_js(rel):
            if rel not in self.funcs:
                self.funcs[rel] = ts_scan.functions(text)
            return ts_scan.enclosing(self.funcs[rel], line)
        return None


def rel_to(root: str | None, path: str) -> str:
    """A report path relative to the project root, with forward slashes."""
    p = path.replace('\\', '/')
    if root:
        r = root.replace('\\', '/').rstrip('/') + '/'
        if p.lower().startswith(r.lower()):
            p = p[len(r):]
    return p


def stryker_test_names(raw: dict) -> dict[str, str]:
    """Stryker test id -> '<test file>::<name>'. Where the test file's source (embedded in the
    report) shows the describe chain, the name is joined with ' > ' like Vitest/Jest JUnit ids
    (run_tests.py); otherwise it is Stryker's own name (describe and title joined by spaces)."""
    root = raw.get('projectRoot')
    names = {}
    for f, tf in (raw.get('testFiles') or {}).items():
        rel = rel_to(root, f)
        text = tf.get('source')
        for t in tf.get('tests', []):
            name = t.get('name', str(t['id']))
            line = ((t.get('location') or {}).get('start') or {}).get('line')
            if text and line and ts_scan.is_js(rel):
                hit = ts_scan.test_at(text, line)
                if hit and ' '.join(hit['chain']) == name:
                    name = ' > '.join(hit['chain'])
            names[str(t['id'])] = f'{rel}::{name}' if rel else name
    return names


def from_builtin(raw: dict) -> list[dict]:
    out = []
    for m in raw['mutants']:
        out.append({'engine': 'builtin', 'raw_id': m['id'], 'file': m['file'], 'line': m['line'], 'end_line': m['end_line'],
                    'operator': m['operator'], 'original': m['original'], 'mutated': m['mutated'],
                    'status': m['status'], 'engine_status': m['status'], 'covering_tests': m.get('covering_tests', []),
                    'killed_by': m.get('killed_by'), **({'note': m['note']} if m.get('note') else {})})
    return out


def from_mutmut(raw: dict, src: Sources) -> tuple[list[dict], list[str]]:
    out, problems = [], []
    for key, status in raw.get('results', {}).items():
        module, cls, name = mutmut_func(key)
        rel = next((c for c in (module + '.py', module + '/__init__.py') if src.get(c)[0] is not None), module + '.py')
        text, lines, tree = src.get(rel)
        parsed = parse_function_diff(raw['diffs'].get(key, ''))
        node = find_def(tree, cls, name) if tree else None
        if not parsed or node is None:
            problems.append(f'{key}: could not map to a line in {rel}')
            continue
        offset, removed, added = parsed
        line = node.lineno + offset - 1
        if removed and (line > len(lines) or lines[line - 1].strip() != removed[0].strip()):
            hits = [i + 1 for i in range(node.lineno - 1, node.end_lineno) if lines[i].strip() == removed[0].strip()]
            if len(hits) != 1:
                problems.append(f'{key}: line text not found once in {rel}:{node.lineno}-{node.end_lineno}')
                continue
            line = hits[0]
        end = line + max(len(removed), 1) - 1
        original = '\n'.join(removed) if removed else lines[line - 1]
        mutated = '\n'.join(added)
        st = STATUS_MUTMUT.get(status, 'runtime_error')
        code = raw.get('exit_codes', {}).get(key)
        m = {'engine': 'mutmut', 'raw_id': key, 'file': rel, 'line': line, 'end_line': end,
             'operator': infer_operator(original, mutated), 'original': original, 'mutated': mutated,
             'status': st, 'engine_status': status,
             'covering_tests': sorted(raw.get('tests_by_function', {}).get(key.rsplit('__mutmut_', 1)[0], [])),
             'killed_by': None}
        if st == 'killed' and code not in (None, 1):
            m['status'] = 'runtime_error'
            m['note'] = f'mutmut reports "killed" for exit code {code}, which is not a test failure'
        if status not in STATUS_MUTMUT:
            m['note'] = f'unrecognised mutmut status {status!r}'
        out.append(m)
    return out, problems


def from_stryker(raw: dict) -> tuple[list[dict], list[str]]:
    names = OPS['engines']['stryker']['names']
    tests = stryker_test_names(raw)
    unknown: set[str] = set()

    def test_name(tid) -> str:
        tid = str(tid)
        if tid not in tests:
            unknown.add(tid)
        return tests.get(tid, tid)

    out = []
    root = raw.get('projectRoot')
    for path, f in sorted(raw.get('files', {}).items()):
        path = rel_to(root, path)
        lines = (f.get('source') or '').splitlines()
        for mu in f.get('mutants', []):
            loc = mu['location']
            a, b = loc['start']['line'], loc['end']['line']
            original = '\n'.join(lines[a - 1:b]) if lines else ''
            mutated = original
            if lines and mu.get('replacement') is not None:
                sl, el = lines[a - 1], lines[b - 1]
                head, tail = sl[:loc['start']['column'] - 1], el[loc['end']['column'] - 1:]
                mutated = head + mu['replacement'] + tail
            op = names.get(mu.get('mutatorName', ''), 'other')
            if op == 'comparison_boundary' and mu.get('replacement') and re.search(r'[!=]==?', mu['replacement']):
                op = 'comparison_negate'
            out.append({'engine': 'stryker', 'raw_id': str(mu['id']), 'file': path, 'line': a, 'end_line': b,
                        'operator': op, 'original': original, 'mutated': mutated,
                        'status': STATUS_STRYKER.get(mu.get('status'), 'runtime_error'), 'engine_status': mu.get('status'),
                        'covering_tests': sorted(test_name(t) for t in mu.get('coveredBy') or []),
                        'killed_by': test_name(mu['killedBy'][0]) if mu.get('killedBy') else None})
    problems = []
    if unknown:
        problems.append(f'Stryker test id(s) {", ".join(sorted(unknown)[:5])} are not in the report\'s testFiles, so '
                        'their test names are unknown (run Stryker with coverageAnalysis "perTest" and a json reporter)')
    return out, problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--scope', type=Path, required=True)
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--builtin', type=Path)
    ap.add_argument('--mutmut', type=Path)
    ap.add_argument('--stryker', type=Path)
    ap.add_argument('--stryker-run', type=Path, help="run_engine.py stryker's record (command, config source, "
                    'tree check); without it the tree is unchecked and the validator says so')
    ap.add_argument('--coverage', type=Path)
    args = ap.parse_args(argv)
    repo = args.repo.resolve()
    scope = json.loads(args.scope.read_text(encoding='utf-8'))
    src = Sources(repo)
    in_scope = {f['path']: {n for a, b in f['ranges'] for n in range(a, b + 1)}
                for f in scope['files'] if f['kind'] == 'source' and f['status'] != 'deleted'}
    excluded = [{'path': f['path'], 'kind': f['kind'], 'why': f['kind_reason'] if f['status'] != 'deleted' else 'deleted'}
                for f in scope['files'] if f['kind'] != 'source' or f['status'] == 'deleted']
    cov = json.loads(args.coverage.read_text(encoding='utf-8')) if args.coverage else None
    engines, mutants, problems = [], [], []
    prefix = {'builtin': 'B', 'mutmut': 'M', 'stryker': 'S'}
    for name, path in (('builtin', args.builtin), ('mutmut', args.mutmut), ('stryker', args.stryker)):
        if not path:
            continue
        raw = json.loads(path.read_text(encoding='utf-8'))
        rec = {'name': name, 'raw': path.name, 'status': raw.get('status', 'ran'), 'reason': raw.get('reason', '')}
        if name == 'builtin':
            rec.update(version=raw['engine']['version'], command=raw['engine']['command'], config_source='builtin',
                       config=None, timeout_s=raw['timeout_s'], sampled=raw['sampled'], tree_changed=raw['tree']['changed'])
            ms = from_builtin(raw)
        elif name == 'mutmut':
            e = raw['engine']
            rec.update(version=e.get('version'), command=e.get('command'), config_source=e.get('config_source'),
                       config=e.get('config'), timeout_s=None, sampled=None, tree_changed=raw.get('tree_changed'))
            ms, probs = from_mutmut(raw, src) if rec['status'] == 'ran' else ([], [])
            problems += probs
        else:
            fw = raw.get('framework') or raw.get('frameworkInfo') or {}
            run = json.loads(args.stryker_run.read_text(encoding='utf-8')) if args.stryker_run else {}
            e = run.get('engine', {})
            rec.update(status=run.get('status', 'ran'), reason=run.get('reason', ''),
                       version=fw.get('version') or e.get('version'),
                       command=e.get('command') or (fw.get('name') or 'stryker') + ' (command not recorded)',
                       config_source=e.get('config_source'), config=e.get('config'),
                       timeout_s=(raw.get('config') or {}).get('timeoutMS', 0) / 1000 or None,
                       sampled=None, tree_changed=run.get('tree_changed'))
            if run.get('code_hash'):
                rec['code_hash'] = run['code_hash']
            ms, probs = from_stryker(raw)
            problems += probs
        kept = []
        for m in sorted(ms, key=lambda m: (m['file'], m['line'], m['raw_id'])):
            lines = in_scope.get(m['file'], set())
            if not any(n in lines for n in range(m['line'], m['end_line'] + 1)):
                continue
            m['function'] = src.function_at(m['file'], m['line'])
            m['kind'] = kind_of(m['operator'])
            if (cov and m['engine'] != 'builtin' and m['status'] == 'survived'):
                hit = cov['lines'].get(m['file'], {})
                ran = {t for n in range(m['line'], m['end_line'] + 1) for t in hit.get(str(n), [])} - {'<import>'}
                if not ran:
                    m['status'], m['reclassified_from'] = 'no_coverage', 'survived'
                    m['note'] = 'no test executed this line (per-test line coverage); the engine only tracks functions'
            kept.append(m)
        for i, m in enumerate(kept, 1):
            m['id'] = f'{prefix[name]}{i}'
        rec['generated'] = len(ms)
        rec['in_scope'] = len(kept)
        rec['out_of_scope'] = len(ms) - len(kept)
        counts: dict[str, int] = {}
        for m in kept:
            counts[m['status']] = counts.get(m['status'], 0) + 1
        rec['counts'] = counts
        engines.append(rec)
        mutants += kept
    order = ['id', 'engine', 'raw_id', 'file', 'line', 'end_line', 'function', 'operator', 'kind', 'original', 'mutated',
             'status', 'engine_status', 'reclassified_from', 'covering_tests', 'killed_by', 'note']
    mutants = [{k: m[k] for k in order if k in m} for m in mutants]
    out = {'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
           'scope': {'source': scope['source'], 'files': [{'path': p, 'ranges': f['ranges']} for f in scope['files']
                                                          for p in [f['path']] if p in in_scope],
                     'excluded': excluded},
           'coverage': {'file': args.coverage.name, 'method': cov['method']} if cov else None,
           'engines': engines, 'problems': problems, 'mutants': mutants}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    for e in engines:
        print(f'{e["name"]}: {e["in_scope"]} in scope ({e["out_of_scope"]} outside), ' +
              ', '.join(f'{v} {k}' for k, v in sorted(e['counts'].items())))
    for p in problems:
        print('PROBLEM:', p)
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
