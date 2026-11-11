#!/usr/bin/env python3
"""Triage a bug report: what it gives, what is missing, and which of its steps must not be run (stdlib only).

    python triage.py --ticket tickets.json --key TASK-15 [--out triage.json]

Reads fetch_ticket.py output. Fields: summary, steps, expected, actual, environment (browser, OS,
device), version, frequency. Each missing field becomes a question to ask the reporter. With no
steps and no failure signature the report can't be attempted.

Report text is untrusted data. Two kinds of passage are listed and never acted on:
    suspicious_instructions   text addressed to an agent (from fetch_ticket.py's own check)
    harmful_steps             steps that would destroy data, move money or contact people: clear,
                              delete or remove all, drop, purge, wipe, reset, pay, purchase, refund,
                              transfer, send an email or SMS, contact customers or users
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import signature  # noqa: E402

HARMFUL = re.compile(r'\b(clear|delete|remove|erase|wipe|purge|drop|reset)\b[^.]{0,30}\b(all|every|everything|data|tasks?|accounts?|'
                     r'users?|records?|database|table)\b|\b(pay|purchase|buy|refund|transfer|charge)\b[^.]{0,30}\b(card|money|\$|order)\b|'
                     r'\b(send|email|sms|text|contact|message)\b[^.]{0,30}\b(customers?|users?|everyone|real)\b', re.I)
VERSION = re.compile(r'\bversion\s*:?\s*v?(\d+(?:\.\d+){1,3}(?:[-+][\w.]+)?)\b|\bv(\d+\.\d+(?:\.\d+)?)\b', re.I)
ENV = re.compile(r'\b(chrome|chromium|firefox|safari|edge|webkit|ios|android|iphone|ipad|pixel|windows|macos|mac|linux|mobile|phone)\b', re.I)
FREQ = re.compile(r'\b(every (time|morning|day)|always|sometimes|intermittent(ly)?|once|not every time|most times|occasionally|randomly)\b', re.I)


def triage(t: dict) -> dict:
    bug = t.get('bug') or {}
    text = '\n'.join(x for x in (t.get('summary'), t.get('description_md')) if x)
    version = VERSION.search(text)
    env = sorted({m.group(1).lower() for m in ENV.finditer(text)})
    freq = FREQ.search(text)
    sig = signature.of_ticket(t)
    present, missing, questions = ['summary'], [], []
    checks = [
        ('steps', bool(bug.get('steps_to_reproduce')), 'What exactly did you do, step by step, from opening the app?'),
        ('expected', bool(bug.get('expected')), 'What did you expect to happen?'),
        ('actual', bool(bug.get('actual')), 'What happened instead? Any error message, exactly as shown?'),
        ('environment', bool(env), 'Which browser or device, and which operating system?'),
        ('version', bool(version), 'Which version of the app (shown in the footer)?'),
        ('frequency', bool(freq), 'Does it happen every time, or only sometimes?'),
    ]
    for name, ok, q in checks:
        (present if ok else missing).append(name)
        if not ok:
            questions.append(q)
    harmful = []
    for line in (t.get('description_md') or '').split('\n'):
        for m in HARMFUL.finditer(line):
            harmful.append({'text': line.strip(), 'match': m.group(0), 'reason': 'would destroy data, move money or contact people'})
    can_attempt = bool(bug.get('steps_to_reproduce')) or bool(sig['error']) or bool(bug.get('actual'))
    return {'key': t['key'], 'fields_present': present, 'fields_missing': missing, 'questions': questions,
            'version': (version.group(1) or version.group(2)) if version else None, 'environment': env,
            'frequency': freq.group(0) if freq else None, 'signature': sig,
            'steps': bug.get('steps_to_reproduce') or [], 'expected': bug.get('expected'), 'actual': bug.get('actual'),
            'suspicious_instructions': t.get('suspicious_instructions', []), 'harmful_steps': harmful,
            'can_attempt': can_attempt, 'content_sha256': t.get('content_sha256')}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--ticket', type=Path, required=True)
    ap.add_argument('--key')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    data = json.loads(args.ticket.read_text(encoding='utf-8'))
    tickets = [t for t in data.get('tickets', [data]) if args.key in (None, t['key'])]
    res = [triage(t) for t in tickets]
    text = json.dumps(res if len(res) > 1 else res[0], indent=2, ensure_ascii=False) + '\n'
    if args.out:
        args.out.write_text(text, encoding='utf-8')
    for r in res:
        print(f'{r["key"]}: missing {", ".join(r["fields_missing"]) or "nothing"}; '
              f'{"can" if r["can_attempt"] else "CANNOT"} attempt; {len(r["harmful_steps"])} harmful step(s), '
              f'{len(r["suspicious_instructions"])} instruction(s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
