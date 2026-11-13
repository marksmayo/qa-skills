#!/usr/bin/env python3
"""Check every skipped test against its ticket and its age (stdlib only, read-only).

    python check_skips.py --repo <repo> --candidates candidates.json --out skips.json
        [--tickets tickets.json ...] [--skip-age-days 180] [--today 2026-11-11]

Tickets come from fetch_ticket.py output (one ticket.json or a tickets.json batch, from Jira's REST
API or a connector export); a key with no ticket file is `not_checked`, never assumed open or
closed. Jira is only read, never written. The age of each skip comes from `git blame` on its
line when the repo is a git work tree; otherwise it is not_checked.

Verdicts:
    stale        the ticket's status category is done (or its status is a done-like name), or an
                 unconditional skip with no ticket is older than --skip-age-days
    open         the ticket is still open: the skip is waiting on it
    legitimate   a conditional skip (platform, optional dependency, a guard such as
                 `if not path.exists(): pytest.skip("... not found")`) with a stated reason
    unknown      none of the above could be established (no ticket file, no git history), or a
                 conditional skip with no reason: age alone never makes a guard skip stale

A skip is conditional when it is a skipif/skipUnless/conditional xfail mark, or a runtime
pytest.skip() inside an if, except, try, loop or short-circuit expression, or after an early
return, and in JavaScript an `it.skipIf(cond)` or a runtime Playwright `test.skip(cond, reason)` /
`test.fixme(cond)` inside a test or describe block (scan_tests.py records the condition). A
candidate the scanner misread can be dismissed in the report with `scanner_error` and a basis.
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scan_tests  # noqa: E402

RULES = scan_tests.RULES


def load_tickets(paths: list[Path]) -> dict[str, dict]:
    out = {}
    for p in paths:
        data = json.loads(p.read_text(encoding='utf-8'))
        for t in data.get('tickets', [data]):
            if t.get('key'):
                out[t['key']] = t
    return out


def blame_age(repo: Path, file: str, line: int, today: date) -> tuple[int | None, str]:
    try:
        inside = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--is-inside-work-tree'], capture_output=True, text=True,
                                encoding='utf-8', errors='replace')
    except OSError:
        return None, 'not_checked: git is not available'
    if inside.returncode or inside.stdout.strip() != 'true':
        return None, 'not_checked: not a git work tree'
    # git writes author names as UTF-8 whatever the console code page: decoding with the locale's (cp1252 on Windows)
    # killed the reader thread on a name with a letter such as s-acute and left stdout None
    res = subprocess.run(['git', '-C', str(repo), 'blame', '--porcelain', '-L', f'{line},{line}', '--', file],
                         capture_output=True, text=True, encoding='utf-8', errors='replace')
    m = re.search(r'^author-time (\d+)$', res.stdout or '', re.M)
    if res.returncode or not m:
        return None, 'not_checked: the line is not committed yet'
    when = datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc).date()
    return (today - when).days, f'git blame ({when.isoformat()})'


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--tickets', type=Path, action='append', default=[])
    ap.add_argument('--skip-age-days', type=int, default=RULES['skip_age_days'])
    ap.add_argument('--today')
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    today = date.fromisoformat(args.today) if args.today else date.today()
    tickets = load_tickets(args.tickets)
    data = json.loads(args.candidates.read_text(encoding='utf-8'))
    repo = args.repo.resolve()
    done_cats = set(RULES['done_categories'])
    done_names = set(RULES['done_statuses'])
    rows = []
    for c in data['candidates']:
        if c['category'] != 'skipped':
            continue
        d = c['details']
        key = d.get('ticket')
        t = tickets.get(key) if key else None
        ticket = None
        if key:
            ticket = {'key': key, 'checked': t is not None, 'status': t.get('status') if t else None,
                      'status_category': t.get('status_category') if t else None,
                      'source': t.get('source') if t else None}
        age, age_source = blame_age(repo, c['file'], c['line'], today)
        if ticket and ticket['checked']:
            done = (ticket['status_category'] or '').lower() in done_cats or (ticket['status'] or '').lower() in done_names
            verdict = 'stale' if done else 'open'
            why = (f'{key} is {ticket["status"]}' + (f' ({ticket["status_category"]})' if ticket['status_category'] else '') +
                   (', so the reason for the skip has gone' if done else '; the skip is waiting on it'))
        elif d.get('conditional') and d.get('reason'):
            verdict, why = 'legitimate', f'conditional skip ({d.get("condition")}) with a reason: {d["reason"]}'
        elif key:
            verdict, why = 'unknown', f'{key} could not be read (no ticket file or connector); status not_checked'
        elif d.get('conditional'):
            verdict, why = 'unknown', (f'conditional skip ({d.get("condition")}) with no stated reason; a guard skip is not '
                                       'stale by age alone, so check what the condition guards against')
        elif age is not None and age > args.skip_age_days:
            verdict, why = 'stale', f'no ticket, and the skip is {age} days old (threshold {args.skip_age_days})'
        elif age is not None:
            verdict, why = 'unknown', f'no ticket; {age} days old, within the {args.skip_age_days}-day threshold'
        else:
            verdict, why = 'unknown', f'no ticket, and the age is {age_source.split(": ", 1)[-1]}'
        rows.append({'candidate_id': c['id'], 'test_ids': c['test_ids'], 'file': c['file'], 'line': c['line'], 'kind': d.get('kind'),
                     'reason': d.get('reason'), 'conditional': d.get('conditional', False), 'condition': d.get('condition'),
                     'ticket': ticket, 'age_days': age, 'age_source': age_source, 'verdict': verdict, 'why': why})
        print(f'{c["id"]} {c["test_ids"][0]}: {verdict} - {why}')
    args.out.write_text(json.dumps({'today': today.isoformat(), 'skip_age_days': args.skip_age_days, 'skips': rows}, indent=2) + '\n',
                        encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
