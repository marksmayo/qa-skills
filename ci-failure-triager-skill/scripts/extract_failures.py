#!/usr/bin/env python3
"""List every failure in one attempt of a normalised CI run, with its log lines (stdlib only).

    python extract_failures.py --run run/ [--attempt 1] --out run/failures.json

A failed job with failing JUnit cases gives one failure per case (kind `test`); a failed job without
them, or a failed step that isn't the test step, gives one failure for the step (kind `step`). Each
failure keeps the log lines that show it (the test's section of the output, or the first error lines of
the step), quoted with their line numbers, the files in its traceback, and its message. The first
attempt is the run as it first failed; later attempts are re-runs.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from parse_results import parse_junit  # noqa: E402

SIG = json.loads((HERE.parent / 'data' / 'signatures.json').read_text(encoding='utf-8'))
TEST_STEP = re.compile(SIG['test_step'], re.I)
TS = re.compile(r'^\d{4}-\d\d-\d\dT[\d:.]+Z ')
ERR = re.compile(r'(##\[error\]|\bERROR\b|\berror\b|Error:|FAILED|fatal:|Exception)')
FRAME = re.compile(r'(?:File "([^"]+)", line (\d+)|^\s*([\w./-]+\.(?:py|ts|tsx|js|jsx|java|kt|go|rb|cs|rs|swift)):(\d+))', re.M)
SECRET = re.compile(r'(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|xox[abpr]-[A-Za-z0-9-]{10,}|(?i:bearer)\s+[A-Za-z0-9._-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?i:(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)\S{6,})')


def redact(line: str) -> str:
    return SECRET.sub(lambda m: m.group(0)[:4] + '[REDACTED]', line)


def strip(line: str) -> str:
    return TS.sub('', line)


def frames(text: str) -> list[dict]:
    out, seen = [], set()
    for m in FRAME.finditer(text or ''):
        f = (m.group(1) or m.group(3)).replace('\\', '/')
        f = re.sub(r'^.*?/(?=(tests?|src|lib|app)/)', '', f)
        key = (f, int(m.group(2) or m.group(4)))
        if key not in seen:
            seen.add(key)
            out.append({'file': key[0], 'line': key[1]})
    return out


def step_sections(lines: list[str]) -> list[tuple[str, int, int]]:
    """(group title, first line, last line) for each ##[group]/##[section] block, 1-based."""
    marks = [(i, strip(l)) for i, l in enumerate(lines) if re.match(r'##\[(group|section)\]', strip(l))]
    out = []
    for k, (i, t) in enumerate(marks):
        end = marks[k + 1][0] - 1 if k + 1 < len(marks) else len(lines) - 1
        out.append((re.sub(r'^##\[(group|section)\](Run |Starting: )?', '', t), i + 1, end + 1))
    return out


def excerpt(lines: list[str], start: int, end: int, limit: int = 12) -> list[dict]:
    return [{'n': n, 'text': redact(strip(lines[n - 1]))} for n in range(start, min(end, start + limit - 1) + 1)]


def test_lines(lines: list[str], name: str) -> list[dict]:
    """The test's section of the failure output: from its ___ header to the next header."""
    hdr = re.compile(r'^_{3,} (\S+\.)?' + re.escape(name) + r'(\[.*\])? _{3,}$')
    for i, l in enumerate(lines):
        if hdr.match(strip(l).strip()):
            j = i + 1
            while j < len(lines) and not re.match(r'^(_{3,} \S.* _{3,}|={3,}.*={3,}|-+ generated)', strip(lines[j]).strip()):
                j += 1
            return excerpt(lines, i + 1, j)
    for i, l in enumerate(lines):
        if name in l and ERR.search(l):
            return excerpt(lines, i + 1, i + 6)
    return []


def step_lines(lines: list[str], step_name: str) -> list[dict]:
    secs = step_sections(lines)
    key = re.sub(r'^Run ', '', step_name)
    sec = next((s for s in secs if s[0].strip() == key.strip() or key.strip() in s[0]), None)
    lo, hi = (sec[1], sec[2]) if sec else (1, len(lines))
    first = next((n for n in range(lo, hi + 1) if ERR.search(strip(lines[n - 1])) and not strip(lines[n - 1]).startswith('##[group]')), None)
    if first is None:
        return excerpt(lines, max(lo, hi - 7), hi, 8)
    return excerpt(lines, max(lo, first - 2), hi, 10)


def extract(run_dir: Path, run: dict, attempt: int) -> list[dict]:
    att = next(a for a in run['attempts'] if a['n'] == attempt)
    out = []
    for j in att['jobs']:
        if j['conclusion'] in ('success', 'skipped', None):
            continue
        lines = (run_dir / j['log']).read_text(encoding='utf-8').split('\n') if j.get('log') and (run_dir / j['log']).exists() else []
        cases = []
        for x in j.get('junit', []):
            cs, _ = parse_junit(run_dir / x, f'A{attempt}')
            cases += [c for c in cs if c['status'] in ('failed', 'error')]
        failed_steps = [s for s in j['steps'] if s['conclusion'] == 'failure']
        for c in cases:
            name = c['id'].split('::')[-1]
            out.append({'job': j['name'], 'job_id': j['id'], 'step': next((s['name'] for s in failed_steps if TEST_STEP.search(s['name'])), failed_steps[0]['name'] if failed_steps else None),
                        'kind': 'test', 'test_id': c['id'], 'test_file': c.get('file'), 'failure_type': c.get('failure_type'),
                        'message': redact((c.get('message_excerpt') or '').split('\n')[0])[:300],
                        'frames': frames(c.get('message_excerpt') or ''), 'log_excerpt': {'path': j.get('log'), 'lines': test_lines(lines, name)}})
        for s in failed_steps:
            if cases and TEST_STEP.search(s['name']):
                continue  # explained by its failing cases
            ex = step_lines(lines, s['name'])
            msg = next((l['text'] for l in ex if ERR.search(l['text']) and not l['text'].startswith('##[')), ex[-1]['text'] if ex else '')
            out.append({'job': j['name'], 'job_id': j['id'], 'step': s['name'], 'kind': 'step', 'test_id': None, 'test_file': None,
                        'failure_type': 'step', 'message': msg[:300], 'frames': [], 'log_excerpt': {'path': j.get('log'), 'lines': ex}})
        if not cases and not failed_steps:
            out.append({'job': j['name'], 'job_id': j['id'], 'step': None, 'kind': 'step', 'test_id': None, 'test_file': None, 'failure_type': 'job',
                        'message': f'job concluded {j["conclusion"]} with no failed step', 'frames': [],
                        'log_excerpt': {'path': j.get('log'), 'lines': excerpt(lines, max(1, len(lines) - 7), len(lines), 8) if lines else []}})
    for i, f in enumerate(out, 1):
        f['id'] = f'X{i:02d}'
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--run', type=Path, required=True)
    ap.add_argument('--attempt', type=int, default=1)
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    run = json.loads((a.run / 'run.json').read_text(encoding='utf-8'))
    fs = extract(a.run, run, a.attempt)
    a.out.write_text(json.dumps({'run_id': run['run']['id'], 'attempt': a.attempt, 'failures': fs}, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(fs)} failure(s) in attempt {a.attempt} -> {a.out}')
    for f in fs:
        print(f'  {f["id"]} {f["job"]:14} {f["kind"]:5} {f["test_id"] or f["step"]}: {f["message"][:90]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
