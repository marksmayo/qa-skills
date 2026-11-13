#!/usr/bin/env python3
"""Measure size and branching per source file, as a cyclomatic proxy (stdlib only).

    python complexity.py <repo> --out complexity.json

Python files are parsed with `ast`: each if/elif, loop, except handler, boolean operator operand,
conditional expression, comprehension condition and match case counts as one branch. Other
languages (JS/TS, Java, Kotlin, Go, C#, Swift, Dart, Ruby, PHP) use a keyword count on code with
strings and comments removed: if, else if, for, while, case, catch, &&, ||, ?: and ??. Lines of
code exclude blank lines and comments. Each file records which method was used. Dependency,
build and cache directories (node_modules, .next, dist, coverage, .venv and the like) are pruned
before they are walked.
"""
from __future__ import annotations
import argparse, ast, json, os, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import detect_conventions  # noqa: E402

CODE_EXT = {'.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.java', '.kt', '.go', '.cs', '.swift', '.dart', '.rb', '.php',
            '.scala', '.rs', '.m', '.mm', '.h', '.c', '.cc', '.cpp', '.hpp'}
KEYWORDS = re.compile(r'\b(if|for|while|case|catch|elif|elsif|when|guard)\b|&&|\|\||\?\?|\?(?=[^.:?]*:)')
# Go: if, for (every loop form), each case of a switch, type switch or select, && and ||. `default` and `else` are not
# branches of their own; Go has no ternary.
GO_KEYWORDS = re.compile(r'\b(if|for|case)\b|&&|\|\|')
# Rust: if/for/while plus each match arm (=>) and each `?` try-operator (an early-return branch);
# `match` itself is not counted, its arms are. Scoped to .rs so JS `str.match(` is untouched.
RUST_KEYWORDS = re.compile(r'\b(if|for|while)\b|&&|\|\||=>|(?<![?<>=!])\?(?![?.])')
# Comments and string literals are matched by ONE left-to-right scan, so whichever starts first wins: an apostrophe in a
# comment ("// the frame's context") does not open a string that swallows the code after it, and "//" inside a string
# is not a comment. Single- and double-quoted strings end at the line end.
_BLOCK, _LINE = r'/\*.*?\*/', r'//[^\n]*'
_DQ, _SQ, _TPL = r'"(?:\\.|[^"\\\n])*"', r"'(?:\\.|[^'\\\n])*'", r'`(?:\\.|[^`\\])*`'
C_LIKE = re.compile('|'.join((_BLOCK, _LINE, _DQ, _SQ, _TPL)), re.S)
HASH_LIKE = re.compile('|'.join((_BLOCK, _LINE, r'#[^\n]*', _DQ, _SQ, _TPL)), re.S)
HASH_COMMENT_EXT = {'.rb', '.php', '.py'}     # .py only when ast cannot parse it
# Go: a raw string in backquotes has no escapes and may span lines; a rune is one character or one escape.
GO_TOKENS = re.compile('|'.join((_BLOCK, _LINE, _DQ, r'`[^`]*`', r"'(?:\\[^'\n]{1,9}|[^'\\\n])'")), re.S)
# Rust strings (incl. raw r#"..."# and byte strings) and single char literals, but not lifetimes ('a).
RUST_TOKENS = re.compile('|'.join((_BLOCK, _LINE, r'r#+"(?:[^"]|"(?!#))*?"#+', r'b?r?"(?:\\.|[^"\\])*"',
                                   r"b?'(?:\\.|[^\\'])'", r"b?'\\.'")), re.S)


def strip_code(text: str, rx: re.Pattern) -> str:
    """Comments removed and string literals replaced by "" in one scan (see C_LIKE)."""
    return rx.sub(lambda m: '' if m.group(0).startswith(('//', '/*', '#')) else '""', text)


def py_branches(tree: ast.AST) -> int:
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp, ast.With)):
            n += 0 if isinstance(node, ast.With) else 1
        elif isinstance(node, ast.BoolOp):
            n += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            n += len(node.ifs)
        elif hasattr(ast, 'match_case') and isinstance(node, ast.match_case):
            n += 1
    return n


def measure(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding='utf-8')
    except (UnicodeDecodeError, OSError):
        return None
    if path.suffix == '.py':
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        loc = len([l for l in text.splitlines() if l.strip() and not l.strip().startswith('#')])
        if tree is not None:
            return {'loc': loc, 'branches': py_branches(tree), 'method': 'ast'}
    if path.suffix == '.rs':
        code = strip_code(text, RUST_TOKENS)
        loc = len([l for l in code.splitlines() if l.strip()])
        return {'loc': loc, 'branches': len(RUST_KEYWORDS.findall(code)), 'method': 'keywords-rust'}
    if path.suffix == '.go':
        code = strip_code(text, GO_TOKENS)
        loc = len([l for l in code.splitlines() if l.strip()])
        return {'loc': loc, 'branches': len(GO_KEYWORDS.findall(code)), 'method': 'keywords-go'}
    code = strip_code(text, HASH_LIKE if path.suffix in HASH_COMMENT_EXT else C_LIKE)
    loc = len([l for l in code.splitlines() if l.strip()])
    return {'loc': loc, 'branches': len(KEYWORDS.findall(code)), 'method': 'keywords'}


PRUNE = detect_conventions.SKIP_DIRS | {'.git', 'node_modules', '.next', '.nuxt', 'dist', 'build', 'out', 'coverage', '.venv',
                                        'venv', '__pycache__', '.turbo', '.cache', '.vercel', '.output'}


def walk(root: Path) -> list[Path]:
    """Code files under root, sorted. Dependency, build and cache directories are pruned before descending, so a
    node_modules folder is never walked."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in PRUNE and not d.endswith('.egg-info'))
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix in CODE_EXT:
                out.append(p)
    return sorted(out, key=lambda p: p.relative_to(root).as_posix())


GO_SAMPLE = '''package sample

// the frame's context is checked here
func Pick(xs []int, mode string) int {
	pattern := `if (a || b) { "for" }`
	for _, x := range xs {
		if x > 0 && mode != "" || x < -5 {
			return x
		}
	}
	switch mode {
	case "a":
		return 1
	case "b", "c":
		return 2
	default:
	}
	select {
	case <-done:
	}
	r, q := 'x', '\''
	_, _, _ = r, q, pattern
	return 0
}
'''
# for, if, &&, ||, three cases (two switch, one select) = 7; the backquoted raw string and the comments hold none.
JS_SAMPLE = '''// the user's choice
function pick(a, b) {
  if (a && b) { return 1; }   // it's fine
  const s = 'if (x) {}';
  return a ?? b;
}
'''


def self_test() -> int:
    """Count branches in synthetic Go and JavaScript snippets whose comments hold apostrophes, and in Objective-C."""
    import tempfile
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        cases = [('sample.go', GO_SAMPLE, 7), ('sample.js', JS_SAMPLE, 3),
                 ('Sample.m', '// the agent\'s server\n- (void)run { if (a) { for (;;) {} } }\n', 2)]
        for name, text, want in cases:
            p = Path(tmp) / name
            p.write_text(text, encoding='utf-8')
            m = measure(p) if p in walk(Path(tmp)) else None
            got = m['branches'] if m else None
            if got != want:
                bad.append(f'{name}: {got} branches, expected {want} ({m["method"] if m else "not measured"})')
    for b in bad:
        print('FAIL ' + b)
    print(f'self-test: {3 - len(bad)} passed, {len(bad)} failed')
    return 1 if bad else 0


def main(argv=None) -> int:
    if '--self-test' in (sys.argv[1:] if argv is None else argv):
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    root = args.repo.resolve()
    files = {}
    for p in walk(root):
        m = measure(p)
        if m:
            files[p.relative_to(root).as_posix()] = m
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({'repo': root.name, 'files': files}, indent=2) + '\n', encoding='utf-8')
    print(f'{len(files)} files, {sum(f["loc"] for f in files.values())} lines of code, '
          f'{sum(f["branches"] for f in files.values())} branches -> {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
