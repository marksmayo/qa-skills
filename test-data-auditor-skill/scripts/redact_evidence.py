#!/usr/bin/env python3
"""Redact flagged personal-data and secret values from evidence files, in place (stdlib only).

    python redact_evidence.py --candidates candidates.json --repo <repo> <run dir or files>...

Test runners copy test data into their output: Vitest and Jest put it in test names and failure
messages, pytest in assertion diffs. The validator rejects any evidence file holding a flagged
value, so run this over the JUnit files, runs.json and logs before validating. It uses the
validator's own matching (`flagged_values`, `leaks`), re-reading each candidate's location, so
what it removes is exactly what the validator would reject:

- a whole value is replaced by [redacted];
- a run of digits that shares six consecutive digits with a flagged number (separators
  ignored) has its digits replaced by X.

JSON files are parsed and only their strings (keys included) are changed, so numbers such as
durations stay intact; other text files are rewritten as text. Only counts and candidate ids are
printed, never a value. Run the validator afterwards: it remains the check.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import validate_test_data_report as v  # noqa: E402

TEXT_EXT = ('.json', '.xml', '.txt', '.md', '.log', '.html', '.sh')
DIGIT_RUN = re.compile(r'\d(?:[\s-]*\d){5,}')


class Redactor:
    def __init__(self, values_by_cand: dict[str, set[str]]):
        self.words, self.full, self.windows = set(), set(), set()
        self.owner: dict[str, str] = {}
        for cid, vals in values_by_cand.items():
            for val in vals:
                self.owner.setdefault(val, cid)
                if re.search(r'[A-Za-z]', val) and not re.search(r'\d', val):
                    self.words.add(val)
                    continue
                self.full.add(val)
                d = re.sub(r'\D', '', val)
                if len(d) >= 8 and d == re.sub(r'[\s-]', '', val):
                    for i in range(len(d) - 5):
                        self.windows.add(d[i:i + 6])
                        self.owner.setdefault(d[i:i + 6], cid)
        alt = lambda vs: '|'.join(re.escape(x) for x in sorted(vs, key=len, reverse=True))  # noqa: E731
        self.word_rx = re.compile(rf'(?<![\w@./\\-])(?:{alt(self.words)})(?![\w@/\\-]|\.\w)') if self.words else None
        self.full_rx = re.compile(alt(self.full)) if self.full else None
        self.count = 0
        self.cands: set[str] = set()

    def text(self, s: str) -> str:
        if self.full_rx:
            s = self.full_rx.sub(self._full, s)
        if self.word_rx:
            s = self.word_rx.sub(self._full, s)
        if self.windows:
            s = DIGIT_RUN.sub(self._digits, s)
        return s

    def _full(self, m: re.Match) -> str:
        self.count += 1
        self.cands.add(self.owner.get(m.group(0), '?'))
        return '[redacted]'

    def _digits(self, m: re.Match) -> str:
        run = m.group(0)
        d = re.sub(r'\D', '', run)
        hit = next((d[i:i + 6] for i in range(len(d) - 5) if d[i:i + 6] in self.windows), None)
        if not hit:
            return run
        self.count += 1
        self.cands.add(self.owner.get(hit, '?'))
        return re.sub(r'\d', 'X', run)

    def json(self, node):
        if isinstance(node, str):
            return self.text(node)
        if isinstance(node, list):
            return [self.json(x) for x in node]
        if isinstance(node, dict):
            return {self.text(k): self.json(x) for k, x in node.items()}
        return node


def files_of(paths: list[Path]) -> list[Path]:
    out = []
    for p in paths:
        if p.is_dir():
            out += sorted(f for f in p.rglob('*') if f.is_file() and f.suffix.lower() in TEXT_EXT)
        elif p.is_file():
            out.append(p)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('paths', nargs='+', type=Path, help='evidence files or directories (the run directory)')
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--dry-run', action='store_true', help='count what would be redacted without writing')
    args = ap.parse_args(argv)
    cands = json.loads(args.candidates.read_text(encoding='utf-8'))['candidates']
    repo = args.repo.resolve()
    values = {c['id']: v.flagged_values(c, repo) for c in cands
              if c['category'] in ('personal_data', 'secret') and not c['auto_dismiss']}
    red = Redactor({k: x for k, x in values.items() if x})
    total = 0
    for f in files_of(args.paths):
        if f.resolve() == args.candidates.resolve():
            continue                       # candidates.json holds fingerprints, never values
        raw = f.read_text(encoding='utf-8', errors='replace')
        red.count, red.cands = 0, set()
        out = None
        if f.suffix.lower() == '.json':
            try:
                out = json.dumps(red.json(json.loads(raw)), indent=2, ensure_ascii=False) + '\n'
            except ValueError:
                out = None
        if out is None:
            out = red.text(raw)
        if red.count:
            total += red.count
            if not args.dry_run:
                f.write_text(out, encoding='utf-8')
            ids = ', '.join(sorted(red.cands, key=lambda x: (len(x), x))[:12]) + (' ...' if len(red.cands) > 12 else '')
            print(f'{f.as_posix()}: {red.count} redaction(s) for {len(red.cands)} candidate(s) ({ids})')
    print(f'{total} redaction(s){" (dry run, nothing written)" if args.dry_run else ""}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
