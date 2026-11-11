#!/usr/bin/env python3
"""Extract and normalise a failure signature, and compare two (stdlib only).

    python signature.py --ticket ticket.json [--key TASK-11]      # signature of a normalised report
    python signature.py --text "TypeError: cannot read 'id' of undefined at app.js:42"

A signature has two parts:
    error     the most specific error the text quotes (an exception type and message, an HTTP status, a
              quoted alert or message), normalised: lower case, numbers -> N, ids and hashes -> ID,
              dates and times -> DATE, paths and URLs -> PATH, quotes and punctuation dropped
    terms     the content words of the summary and the actual result, for matching reports that
              describe the same failure in different words

`match(a, b)` is 1 when the errors are equal (or one contains the other), or when the terms
overlap by at least half of the smaller set (the overlap coefficient); otherwise 0.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

STOP = set('a an and are as at be but by can do does for from has have i if in into is it its me my no not of on or our please '
           'so some that the their them then there these this those to too up was we were what when where which while who will '
           'with would you your again also only every most time times expected actual steps step version browser click open '
           'shows show showing shown page app happens'.split())
ERROR_RX = [
    re.compile(r'\b([A-Z][A-Za-z]*(?:Error|Exception))\b[:\s]+([^\n]{0,160})'),
    re.compile(r'\bHTTP\s*(\d{3})\b|\bstatus(?: code)?\s*(\d{3})\b', re.I),
    re.compile(r'"([^"\n]{6,160})"'),
]


def normalise(text: str) -> str:
    t = text.lower()
    t = re.sub(r'https?://\S+|(?:[a-z]:)?[\\/][\w.\\/-]+\.\w+(?::\d+)*', ' PATH ', t)
    t = re.sub(r'\b\d{4}-\d{2}-\d{2}(?:[t ]\d{2}:\d{2}(?::\d{2})?)?\b|\b\d{1,2}:\d{2}(?::\d{2})?\b', ' DATE ', t)
    t = re.sub(r'\b[0-9a-f]{8,}\b|\b[0-9a-f]{8}-[0-9a-f-]{27,}\b', ' ID ', t)
    t = re.sub(r'\d+(?:\.\d+)?', 'N', t)
    t = re.sub(r'[^\w\s]', ' ', t)
    return re.sub(r'\s+', ' ', t).strip().replace('path', 'PATH').replace('date', 'DATE').replace(' id ', ' ID ')


def terms(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z]+", text.lower().replace("'s", '')):
        if len(w) < 3 or w in STOP:
            continue
        w = re.sub(r'(ies)$', 'y', w)
        w = re.sub(r'(?<=[a-z]{3})(s|es|ed|ing)$', '', w)
        out.append(w)
    return sorted(set(out))


def extract(text: str) -> dict:
    for rx in ERROR_RX:
        m = rx.search(text)
        if m:
            raw = m.group(0).strip()
            return {'raw_excerpt': raw, 'error': normalise(raw), 'kind': ['exception', 'http_status', 'quoted_message'][ERROR_RX.index(rx)]}
    return {'raw_excerpt': None, 'error': None, 'kind': None}


def of_ticket(t: dict) -> dict:
    bug = t.get('bug') or {}
    actual = bug.get('actual') or ''
    sig = extract(actual) if actual else {'raw_excerpt': None, 'error': None, 'kind': None}
    if sig['error'] is None:
        sig = extract(t.get('description_md') or '')
    text = ' '.join(x for x in (t.get('summary'), actual, t.get('description_md')) if x)
    return {**sig, 'terms': terms(text), 'from': 'actual result' if actual else 'summary and description'}


def overlap(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    return len(set(a) & set(b)) / min(len(set(a)), len(set(b)))


def match(a: dict, b: dict) -> int:
    ea, eb = a.get('error'), b.get('error')
    if ea and eb and (ea == eb or ea in eb or eb in ea):
        return 1
    return 1 if overlap(a.get('terms', []), b.get('terms', [])) >= 0.5 else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--ticket', type=Path)
    ap.add_argument('--key')
    ap.add_argument('--text')
    args = ap.parse_args(argv)
    if args.text:
        out = {**extract(args.text), 'terms': terms(args.text)}
    else:
        data = json.loads(args.ticket.read_text(encoding='utf-8'))
        tickets = data.get('tickets', [data])
        t = next(x for x in tickets if args.key in (None, x['key']))
        out = {'key': t['key'], **of_ticket(t)}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
