#!/usr/bin/env python3
"""Parse exploratory session notes into numbered lines, tagged items and session metrics (stdlib only).

    python parse_notes.py notes/session-01.md --out parsed.json
    python parse_notes.py session.json --out parsed.json          # timeline JSON (see below)

Markdown notes follow templates/session-notes.md: a header (Charter, Tester, Date, Environment,
Duration) and lines like "[00:12] TEST: ..." ([HH:MM] since the session began). Tags: SETUP, TEST, BUG, INVESTIGATE, Q, ISSUE, IDEA,
NOTE, OFF, END. A BUG line can be followed by Steps:, Expected:, Actual:, Env: and Evidence: lines,
which belong to it. Untagged lines are kept with their number for you to classify; free-form
notes are all untagged.

Timeline JSON is {"session": {"charter", "testers", "environment", "duration_min"}, "items":
[{"time": "HH:MM" | minutes, "type": "setup|test|bug|investigate|question|issue|idea|note|off",
"text": ..., optional "steps", "expected", "actual", "environment", "evidence"}]}; each item is
one numbered line. Map another tool's export (for example the Xray Exploratory App's) to this
shape first; its native format is not read directly.

Metrics need timestamps: each tagged line's time runs until the next timestamp (or END, or the
Duration header). SETUP and ISSUE (an obstacle, such as an environment outage, as in
session-based test management) count as setup; BUG and INVESTIGATE as bug investigation; the
rest as testing; OFF also counts as off charter. Lines that look like instructions to an agent
and lines with personal data (emails, card-like numbers) are listed so the debrief can ignore
and redact them.
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys
from pathlib import Path

TAG = re.compile(r'^\s*(?:[-*]\s*)?\[(\d{1,2}):(\d{2})\]\s*([A-Z]+)(?::\s?(.*)|\s*)$')
FIELD = re.compile(r'^\s*(?:[-*]\s*)?(Steps|Expected|Actual|Env|Environment|Evidence)\s*:\s*(.*)$', re.I)
HEADER = re.compile(r'^\s*(Charter|Tester|Testers|Date|Environment|Duration)\s*:\s*(.+?)\s*$', re.I)
KNOWN = {'SETUP', 'TEST', 'BUG', 'INVESTIGATE', 'Q', 'ISSUE', 'IDEA', 'NOTE', 'OFF', 'END'}
SUSPICIOUS = re.compile(r'\b(ignore (the |all |any |previous |prior )*(instructions?|template|rules|charter)|mark (it|this|everything|the '
                        r'charter|all)\b.{0,30}\b(pass|passed|done|complete)|you are (an? )?(ai|assistant|agent)|as an ai|'
                        r'system prompt|disregard|do not report|don\'t report)\b|^\s*(ai|assistant|claude|gpt|agent)\s*:', re.I)
EMAIL = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b')
CARDLIKE = re.compile(r'(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)')
RESERVED_DOMAINS = ('example.com', 'example.org', 'example.net', '.example', '.test', '.invalid')
CATEGORY = {'SETUP': 'setup', 'ISSUE': 'setup', 'BUG': 'bug', 'INVESTIGATE': 'bug'}


def luhn(n: str) -> bool:
    total = 0
    for i, c in enumerate(reversed(n)):
        d = int(c)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def sensitive(text: str) -> list[str]:
    kinds = []
    for m in EMAIL.finditer(text):
        dom = m.group(0).split('@')[1].lower()
        if not any(dom == d or dom.endswith(d) for d in RESERVED_DOMAINS):
            kinds.append('email address')
    for m in CARDLIKE.finditer(text):
        digits = re.sub(r'\D', '', m.group(0))
        if 13 <= len(digits) <= 19 and luhn(digits):
            kinds.append('card-like number')
    return kinds


def parse_markdown(text: str) -> dict:
    header, lines, items = {}, [], []
    in_comment = False
    current = None
    for n, raw in enumerate(text.split('\n'), 1):
        line = raw.rstrip()
        if '<!--' in line:
            in_comment = '-->' not in line
            continue
        if in_comment:
            in_comment = '-->' not in line
            continue
        if not line.strip() or line.startswith('#'):
            continue
        entry = {'n': n, 'text': line.strip(), 'tag': None, 'minute': None}
        h = HEADER.match(line)
        t = TAG.match(line)
        f = FIELD.match(line)
        if h and not items:
            key = h.group(1).lower()
            header['testers' if key in ('tester', 'testers') else key] = h.group(2)
            entry['tag'] = 'HEADER'
        elif t and t.group(3) in KNOWN:
            minute = int(t.group(1)) * 60 + int(t.group(2))         # [HH:MM] since the session began
            entry.update(tag=t.group(3), minute=minute, body=(t.group(4) or '').strip())
            current = {'kind': t.group(3), 'line': n, 'minute': minute, 'text': (t.group(4) or '').strip(), 'fields': {}}
            items.append(current)
        elif f and current is not None and current['kind'] in ('BUG', 'INVESTIGATE'):
            key = {'env': 'environment'}.get(f.group(1).lower(), f.group(1).lower())
            current['fields'][key] = {'text': f.group(2).strip(), 'line': n}
            entry['tag'] = 'FIELD'
        else:
            current = None
        lines.append(entry)
    return {'format': 'markdown', 'header': header, 'lines': lines, 'items': items}


def to_minute(v) -> int | None:
    if isinstance(v, (int, float)):
        return int(v)
    m = re.match(r'^(\d+):(\d{2})$', str(v or ''))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def parse_timeline(data: dict) -> dict:
    s = data.get('session', {})
    header = {k: v for k, v in (('charter', s.get('charter')), ('testers', ', '.join(s.get('testers', [])) if s.get('testers') else None),
                                ('environment', s.get('environment')), ('duration', s.get('duration_min'))) if v}
    lines, items = [], []
    mapping = {'setup': 'SETUP', 'test': 'TEST', 'bug': 'BUG', 'investigate': 'INVESTIGATE', 'question': 'Q', 'issue': 'ISSUE',
               'idea': 'IDEA', 'note': 'NOTE', 'off': 'OFF', 'end': 'END'}
    for n, it in enumerate(data.get('items', []), 1):
        tag = mapping.get(str(it.get('type', '')).lower())
        minute = to_minute(it.get('time'))
        text = it.get('text', '')
        lines.append({'n': n, 'text': text, 'tag': tag, 'minute': minute, 'body': text})
        if tag:
            fields = {k: {'text': it[src], 'line': n} for k, src in (('steps', 'steps'), ('expected', 'expected'), ('actual', 'actual'),
                                                                      ('environment', 'environment'), ('evidence', 'evidence')) if it.get(src)}
            items.append({'kind': tag, 'line': n, 'minute': minute, 'text': text, 'fields': fields})
    return {'format': 'timeline_json', 'header': header, 'lines': lines, 'items': items}


def metrics(parsed: dict) -> dict | None:
    timed = sorted((it for it in parsed['items'] if it['minute'] is not None), key=lambda it: (it['minute'], it['line']))
    if len(timed) < 2:
        return None
    end = next((it['minute'] for it in timed if it['kind'] == 'END'), None)
    dur = parsed['header'].get('duration')
    if end is None and dur and re.match(r'^\d+', str(dur)):
        end = int(re.match(r'^\d+', str(dur)).group(0))
    if end is None:
        end = timed[-1]['minute']
    spent = {'test': 0, 'bug': 0, 'setup': 0}
    off = 0
    for a, b in zip(timed, timed[1:] + [{'minute': end}]):
        if a['kind'] == 'END':
            break
        span = max(0, min(b['minute'], end) - a['minute'])
        spent[CATEGORY.get(a['kind'], 'test')] += span
        if a['kind'] == 'OFF':
            off += span
    total = sum(spent.values())
    if total == 0:
        return None
    pct = lambda v: round(100 * v / total)  # noqa: E731
    return {'duration_min': total, 'test_pct': pct(spent['test']), 'bug_pct': pct(spent['bug']), 'setup_pct': pct(spent['setup']),
            'on_charter_pct': pct(total - off), 'minutes': {**spent, 'off_charter': off}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('notes', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    raw = args.notes.read_bytes()
    if args.notes.suffix.lower() == '.json':
        parsed = parse_timeline(json.loads(raw.decode('utf-8')))
    else:
        parsed = parse_markdown(raw.decode('utf-8', errors='replace').replace('\r\n', '\n'))
    parsed['notes'] = {'path': args.notes.name, 'sha256': hashlib.sha256(raw.replace(b'\r\n', b'\n')).hexdigest(),
                       'format': parsed.pop('format')}
    parsed['metrics'] = metrics(parsed)
    parsed['suspicious'] = [{'line': l['n'], 'text': l['text']} for l in parsed['lines'] if SUSPICIOUS.search(l.get('body') or l['text'])]
    parsed['sensitive'] = [{'line': l['n'], 'kinds': k} for l in parsed['lines'] if (k := sensitive(l['text']))]
    parsed['untagged'] = [l['n'] for l in parsed['lines'] if l['tag'] is None]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(parsed, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    kinds = {}
    for it in parsed['items']:
        kinds[it['kind']] = kinds.get(it['kind'], 0) + 1
    print(f'{len(parsed["lines"])} lines, items: ' + ', '.join(f'{v} {k}' for k, v in sorted(kinds.items())) +
          (f'; {len(parsed["untagged"])} untagged' if parsed['untagged'] else '') +
          (f'; {len(parsed["suspicious"])} instruction-like' if parsed['suspicious'] else '') +
          (f'; {len(parsed["sensitive"])} with personal data' if parsed['sensitive'] else '') + f' -> {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
