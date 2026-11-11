#!/usr/bin/env python3
"""Find the signals in each acceptance criterion that point to a test design technique (stdlib only).

    python analyse_criteria.py ticket.json --out analysis.json

Per criterion:
  limits      numbers with a comparison ("from $10 to $500 inclusive", "at least 3", "below 10",
              "up to 50 characters") -> boundary value analysis, and partitions either side
  values      lists of values ("card, PayPal, gift card"; "Gold, Silver and Bronze") -> partitions, or
              parameters for a decision table or pairwise
  conditions  when / if / unless / but / never / only -> rules for a decision table
  states      status words with a change ("pending when placed, available when delivered") -> state transition
  combination "every", "each", "any combination" over several lists -> pairwise
  sequence    ordered steps ("then", "after", numbered steps) -> scenario
  vague       terms from the shared ambiguity lexicon and timing words without a number -> questions
Each signal keeps the text it matched. The suggested techniques are suggestions: the agent picks the
technique per criterion and says why, in model.json.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEX = json.loads((HERE.parent / 'data' / 'ambiguity-lexicon.json').read_text(encoding='utf-8'))
EXT = json.loads((HERE.parent / 'data' / 'expected-terms.json').read_text(encoding='utf-8'))
NUM = r'\$?\d+(?:[.,]\d+)?\s*(?:%|characters?|chars?|items?|days?|hours?|minutes?|seconds?|points?|years?)?'
LIMIT_PATTERNS = [
    (rf'\bfrom\s+({NUM})\s+to\s+({NUM})(\s+inclusive)?', 'range'),
    (rf'\bbetween\s+({NUM})\s+and\s+({NUM})(\s+inclusive)?', 'range'),
    (rf'\b(at least|no less than|minimum of|a minimum of)\s+({NUM})', 'min_inclusive'),
    (rf'\b(at most|no more than|up to|maximum of|a maximum of)\s+({NUM})', 'max_inclusive'),
    (rf'\b(below|under|less than|fewer than)\s+({NUM})', 'max_exclusive'),
    (rf'\b(above|over|more than|greater than|exceeding)\s+({NUM})', 'min_exclusive'),
]
STATE_WORDS = r'\b(pending|placed|paid|shipped|delivered|cancell?ed|returned|refunded|approved|rejected|draft|submitted|active|inactive|expired|suspended|closed|open|archived|locked)\b'
CHANGE_WORDS = r'\b(becomes?|become|when it is|once it is|moves? to|changes? to|transitions?|before it|after (it|delivery|payment)|can\'?t be|cannot be|no longer)\b'
COND = r'\b(when|if|unless|but|except|never|only|otherwise|provided that)\b'


def num(s: str) -> float:
    return float(re.sub(r'[^\d.]', '', s.replace(',', '')) or 'nan')


def lists(text: str) -> list[list[str]]:
    # in parentheses, values are comma-separated and may themselves contain "and" ("click and collect")
    out = [[re.sub(r'^(and|or)\s+', '', v.strip()) for v in m.split(',') if v.strip()] for m in re.findall(r'\(([^()]+)\)', text) if ',' in m]
    for m in re.finditer(r'\b([A-Z][a-z]+(?:,\s+[A-Z][a-z]+)+,?\s+(?:and|or)\s+[A-Z][a-z]+)\b', text):
        out.append([v.strip() for v in re.split(r',\s*|\s+and\s+|\s+or\s+', m.group(1)) if v.strip()])
    seen, uniq = set(), []
    for l in out:
        if tuple(l) not in seen:
            seen.add(tuple(l)); uniq.append(l)
    return uniq


def term_hits(text: str) -> list[dict]:
    hits = []
    low = text.lower()
    for kind, terms in LEX['terms'].items():
        for t in terms:
            if re.search(r'(?<![\w-])' + re.escape(t) + r'(?![\w-])', low):
                hits.append({'term': t, 'kind': kind, 'question': LEX['kinds'][kind]['question'].format(term=t)})
    for t in EXT['timing_without_number']:
        if re.search(r'\b' + re.escape(t) + r'\b', low) and not re.search(LEX['quantity_pattern'], low):
            hits.append({'term': t, 'kind': 'timing_without_number', 'question': f'"{t}" needs a number and a unit: within how many seconds or minutes?'})
    return hits


def analyse(ac: dict) -> dict:
    text = ac['text']
    limits = []
    for pat, kind in LIMIT_PATTERNS:
        for m in re.finditer(pat, text, re.I):
            g = [x for x in m.groups() if x]
            if kind == 'range':
                inc = bool(m.group(3)) or 'inclusive' in text[m.end():m.end() + 12].lower()
                limits.append({'kind': 'range', 'low': num(g[0]), 'high': num(g[1]), 'inclusive': inc, 'text': m.group(0)})
            else:
                limits.append({'kind': kind, 'value': num(g[-1]), 'text': m.group(0)})
    values = lists(text)
    conds = sorted({m.lower() for m in re.findall(COND, text, re.I)})
    states = sorted({m.lower() for m in re.findall(STATE_WORDS, text, re.I)})
    change = re.findall(CHANGE_WORDS, text, re.I)
    combo = bool(re.search(r'\b(every|each|any)\b', text, re.I)) and len(values) >= 2
    seq = bool(re.search(r'\b(then|after that|next|finally)\b', text, re.I)) or bool(re.match(r'\s*(given|when)\b', text, re.I))
    suggest = []
    if limits:
        suggest.append({'technique': 'boundary', 'reason': 'numeric limits: ' + '; '.join(l['text'] for l in limits)})
        suggest.append({'technique': 'partition', 'reason': 'classes either side of the limits'})
    if states and change:
        suggest.append({'technique': 'state_transition', 'reason': f'status words ({", ".join(states)}) with changes ({", ".join(sorted({c if isinstance(c, str) else c[0] for c in change}))})'})
    if combo and len(values) >= 3:
        suggest.append({'technique': 'pairwise', 'reason': f'"every" over {len(values)} lists of values: ' + ' x '.join(str(len(v)) for v in values)})
    elif conds and (values or len(conds) >= 2):
        suggest.append({'technique': 'decision_table', 'reason': f'conditions ({", ".join(conds)})' + (f' over values {values}' if values else '')})
    elif values:
        suggest.append({'technique': 'partition', 'reason': f'lists of values {values}'})
    if seq:
        suggest.append({'technique': 'scenario', 'reason': 'a sequence of steps'})
    vague = term_hits(text)
    if not suggest:
        suggest.append({'technique': 'scenario', 'reason': 'no limit, rule, state or parameter list found: one scenario per outcome'})
    return {'id': ac['id'], 'text': text, 'signals': {'limits': limits, 'values': values, 'conditions': conds, 'states': states,
            'changes': len(change), 'combination': combo, 'sequence': seq}, 'vague': vague, 'suggested': suggest}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('ticket', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    t = json.loads(a.ticket.read_text(encoding='utf-8'))
    out = {'ticket': t['key'], 'content_sha256': t.get('content_sha256'), 'lexicon_version': LEX.get('version'),
           'criteria': [analyse(ac) for ac in t['acceptance_criteria']]}
    a.out.write_text(json.dumps(out, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(out["criteria"])} criteria -> {a.out}')
    for c in out['criteria']:
        print(f'  {c["id"]}: ' + ', '.join(s['technique'] for s in c['suggested']) + (f'; vague: {", ".join(v["term"] for v in c["vague"])}' if c['vague'] else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
