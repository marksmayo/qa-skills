#!/usr/bin/env python3
"""Lint a ticket's acceptance criteria against the ambiguity lexicon (stdlib only).

    python lint_criteria.py ticket.json --out lint.json [--glossary glossary.md|glossary.json]
    python lint_criteria.py tickets.json --out lint.json

Reads ticket.json or tickets.json from fetch_ticket.py. For every criterion it reports vague
terms, unquantified non-functional words, open-ended lists, undefined references, weak modals,
subjective outcomes and compound criteria. Each flag carries a verbatim excerpt of the field it
came from, so the validator can re-check it. Bug tickets also get mechanical facts about their
reproduction steps, expected and actual results, environment, frequency and evidence.

Every flag is tool evidence that a term is present, not proof of a defect. The agent keeps each
flag in the report or dismisses it with a reason; validate_readiness.py --lint checks that none
is dropped silently. Glossary terms are suppressed and listed.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEXICON = HERE.parent / 'data' / 'ambiguity-lexicon.json'

ENV_RX = re.compile(r'\b(version|v\d+(?:\.\d+)+|\d+\.\d+\.\d+|build|release|browser|chrome|firefox|safari|edge|'
                    r'ios|android|windows|macos|linux|staging|production|prod|uat|environment|device|app version|mobile app|mobile|desktop|tablet)\b', re.I)
FREQ_RX = re.compile(r'\b(always|every time|each time|sometimes|intermittent(?:ly)?|occasionally|rarely|once|'
                     r'\d+\s*(?:/|out of|of)\s*\d+|\d+\s*%\s*of)\b', re.I)


def load_lexicon(path: Path = LEXICON) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def load_glossary(path: Path | None) -> dict[str, str]:
    """Terms the team has defined elsewhere: a JSON object or list, or Markdown 'term: definition' lines."""
    if not path:
        return {}
    text = path.read_text(encoding='utf-8')
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return {k.lower(): str(v) for k, v in data.items()}
        return {str(k).lower(): '' for k in data}
    except ValueError:
        out = {}
        for line in text.splitlines():
            m = re.match(r'^\s*[-*]?\s*\**([^:*]+?)\**\s*[:=-]\s*(.+)$', line)
            if m:
                out[m.group(1).strip().lower()] = m.group(2).strip()
        return out


def term_rx(term: str) -> re.Pattern:
    return re.compile(r'(?<![\w-])' + re.escape(term).replace(r'\ ', r'\s+') + r'(?![\w-])', re.I)


def excerpt(text: str, start: int, end: int, width: int = 60) -> str:
    """A verbatim slice of text around [start, end), cut at word boundaries where possible."""
    a, b = max(0, start - width), min(len(text), end + width)
    if a > 0:
        sp = text.find(' ', a, start)
        a = sp + 1 if sp != -1 else a
    if b < len(text):
        sp = text.rfind(' ', end, b)
        b = sp if sp != -1 else b
    return text[a:b].strip()


def lint_text(text: str, field: str, criterion_id: str | None, lex: dict, glossary: dict[str, str]) -> tuple[list, list]:
    flags, suppressed, spans = [], [], []
    has_quantity = bool(re.search(lex['quantity_pattern'], text, re.I))
    candidates = []
    for kind, terms in lex['terms'].items():
        for term in terms:
            for m in term_rx(term).finditer(text):
                candidates.append((m.start(), m.end(), kind, m.group(0)))
    # longest match first, so "etc." beats "etc" and "high availability" beats "available"
    candidates.sort(key=lambda c: (-(c[1] - c[0]), c[0]))
    seen_terms = set()
    for start, end, kind, matched in candidates:
        if any(start < e and end > s for s, e in spans):
            continue
        spans.append((start, end))
        key = (kind, matched.lower())
        if key in seen_terms:
            continue
        seen_terms.add(key)
        if matched.lower() in glossary:
            suppressed.append({'term': matched, 'field': field, 'criterion_id': criterion_id,
                               'reason': 'glossary' + (f': {glossary[matched.lower()]}' if glossary[matched.lower()] else '')})
            continue
        if kind == 'unquantified_nfr' and has_quantity:
            suppressed.append({'term': matched, 'field': field, 'criterion_id': criterion_id,
                               'reason': 'a number with a unit appears in the same text'})
            continue
        flags.append({'criterion_id': criterion_id, 'field': field, 'kind': kind, 'term': matched,
                      'excerpt': excerpt(text, start, end)})
    low = text.lower()
    if not any(x in low for x in lex['compound_exclude']):
        m = re.search(lex['compound_pattern'], text, re.I)
        if m:
            flags.append({'criterion_id': criterion_id, 'field': field, 'kind': 'compound', 'term': None,
                          'excerpt': m.group(0).strip()})
    return flags, suppressed


def bug_facts(t: dict) -> dict:
    b = t.get('bug') or {}
    body = '\n'.join([t.get('description_md') or '', t.get('summary') or ''])
    env = sorted({m.group(0) for m in ENV_RX.finditer(body)}, key=str.lower)
    freq = sorted({m.group(0) for m in FREQ_RX.finditer(body)}, key=str.lower)
    return {'steps_count': len(b.get('steps_to_reproduce') or []), 'has_expected': bool(b.get('expected')),
            'has_actual': bool(b.get('actual')), 'environment_mentions': env, 'frequency_mentions': freq,
            'attachments_count': len(t.get('attachments') or [])}


def lint_ticket(t: dict, lex: dict, glossary: dict[str, str]) -> dict:
    flags, suppressed = [], []
    for ac in t.get('acceptance_criteria') or []:
        f, s = lint_text(ac['text'], f"acceptance_criteria.{ac['id']}", ac['id'], lex, glossary)
        flags += f
        suppressed += s
    f, s = lint_text(t.get('summary') or '', 'summary', None, lex, glossary)
    flags += [x for x in f if x['kind'] in ('unquantified_nfr', 'subjective_outcome', 'undefined_reference')]
    suppressed += s
    kinds = lex['kinds']
    for i, fl in enumerate(flags, 1):
        fl['id'] = f"L-{t.get('key') or 'X'}-{i}"
        fl['dimension'] = kinds[fl['kind']]['dimension']
        fl['confidence'] = kinds[fl['kind']]['confidence']
        fl['evidence_type'] = 'tool'
        fl['question_hint'] = kinds[fl['kind']]['question'].replace('{term}', fl['term'] or '')
    desc = (t.get('description_md') or '').lower()
    nfr_mentions = sorted({w for w in lex['nfr_hint_words'] if re.search(r'\b' + re.escape(w) + r'\b', desc)})
    out = {'key': t.get('key'), 'type': t.get('type'), 'content_sha256': t.get('content_sha256'),
           'criteria': [{'id': a['id'], 'text': a['text'], 'source': a['source']} for a in t.get('acceptance_criteria') or []],
           'criteria_count': len(t.get('acceptance_criteria') or []),
           'flags': [{k: fl[k] for k in ('id', 'criterion_id', 'field', 'kind', 'term', 'excerpt', 'dimension',
                                         'confidence', 'evidence_type', 'question_hint')} for fl in flags],
           'suppressed': suppressed, 'nfr_mentions_in_description': nfr_mentions,
           'suspicious_instructions': t.get('suspicious_instructions') or []}
    if (t.get('type') or '').lower() in ('bug', 'defect', 'incident'):
        out['bug_facts'] = bug_facts(t)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('ticket', type=Path, help='ticket.json or tickets.json from fetch_ticket.py')
    ap.add_argument('--glossary', type=Path)
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    data = json.loads(args.ticket.read_text(encoding='utf-8'))
    tickets = data['tickets'] if isinstance(data, dict) and 'tickets' in data else [data]
    lex, glossary = load_lexicon(), load_glossary(args.glossary)
    result = {'lexicon_version': lex['version'], 'glossary_terms': sorted(glossary),
              'tickets': [lint_ticket(t, lex, glossary) for t in tickets]}
    text = json.dumps(result, indent=2, ensure_ascii=False) + '\n'
    if args.out:
        args.out.write_text(text, encoding='utf-8')
    else:
        sys.stdout.write(text)
    for r in result['tickets']:
        kinds: dict[str, int] = {}
        for f in r['flags']:
            kinds[f['kind']] = kinds.get(f['kind'], 0) + 1
        detail = ', '.join(f'{v} {k}' for k, v in sorted(kinds.items())) or 'no flags'
        print(f"{r['key']}: {r['criteria_count']} criteria, {detail}"
              + (f", {len(r['suppressed'])} suppressed" if r['suppressed'] else ''), file=sys.stderr)
    return 0


if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
