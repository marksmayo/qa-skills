#!/usr/bin/env python3
"""Validate a visual review against the schema, the groups, the crops and the pixel counts.

    python validate_review.py review.json --groups run/groups.json --diff run/diff.json [--crops run/crops] [--write-summary]

Fails when:
  * a group's cause, impact, confidence, reach, score, elements, pages, viewports, measure or pixel
    counts differ from groups.json, or a group is missing or invented;
  * a group has no crops, a crop file is missing, or its SHA-256 differs from crops/manifest.json;
  * a pair's grouped pixels plus its ungrouped pixels don't equal its changed and anti-aliasing pixels
    in diff.json (and the same for flaky pixels);
  * a page in only one set is listed as unchanged, or unchanged/added/removed/untested differ from
    the inputs;
  * an intent other than unknown is not marked agent_judgment, or has high confidence; an intent is
    given with no change context; anything claims approval (the headline's approval must be "none",
    and no text may say a change was approved or accepted);
  * review_order doesn't follow intent (unintended, unknown, intended) then review score;
  * the headline counts differ, or the summary says there are no visual changes while something was
    untested, added or removed without saying so.
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import schema_check  # noqa: E402

GROUP_FIELDS = ('cause', 'impact', 'confidence', 'reach', 'review_score', 'noise', 'element_sigs', 'pages', 'viewports', 'browsers',
                'measure', 'pixels', 'aa_pixels', 'flaky_pixels', 'crops')
APPROVAL = re.compile(r'\b(approved|auto-?approved|accepted|signed off)\b', re.I)
NO_CHANGE = re.compile(r'\bno (visual )?(changes|differences)\b', re.I)
INTENT_ORDER = {'likely_unintended': 0, 'unknown': 1, 'likely_intended': 2}


def load(p: Path):
    return json.loads(p.read_text(encoding='utf-8'))


def expected_headline(rep: dict, groups: dict, diff: dict) -> dict:
    gs = rep['groups']
    pages = sorted({Path(p['rel']).stem for p in diff['pairs']})
    untested_pages = sorted({Path(u['pair']).stem for u in groups['untested']} | {Path(x).stem for x in groups['added'] + groups['removed']})
    partial = bool(groups['untested'] or groups['added'] or groups['removed'] or any(t['size_mismatch'] for t in groups['totals'])
                   or any(g['confidence'] != 'high' for g in gs))
    real = [g for g in gs if not g['noise']]
    if any(g['cause'] == 'unexplained' for g in gs):
        conf = 'low'
    elif partial or any(g['confidence'] != 'high' for g in real):
        conf = 'medium'
    else:
        conf = 'high'
    return {'groups_by_impact': dict(Counter(g['impact'] for g in gs)), 'groups_by_cause': dict(Counter(g['cause'] for g in gs)),
            'pairs_compared': len(diff['pairs']), 'pages_compared': pages, 'pages_untested': untested_pages,
            'flaky_pixels_excluded': sum(p['flaky_pixels'] for p in diff['pairs']),
            'evidence_completeness': 'partial' if partial else 'complete', 'overall_confidence': conf}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('review', type=Path)
    ap.add_argument('--groups', type=Path, required=True)
    ap.add_argument('--diff', type=Path, required=True)
    ap.add_argument('--crops', type=Path)
    ap.add_argument('--write-summary', action='store_true')
    a = ap.parse_args()
    rep, groups, diff = load(a.review), load(a.groups), load(a.diff)
    crops_dir = a.crops or a.groups.parent / 'crops'
    errs: list[str] = []
    schema = load(HERE.parent / 'schemas' / 'visual_review.schema.json')
    errs += [f'schema: {e}' for e in schema_check.errors(rep, schema)]
    if errs:
        print('\n'.join(errs))
        print(f'INVALID: {len(errs)} schema error(s)')
        return 1

    # groups
    gmap = {g['id']: g for g in groups['groups']}
    if sorted(g['id'] for g in rep['groups']) != sorted(gmap):
        errs.append(f'groups: the review lists {sorted(g["id"] for g in rep["groups"])}; groups.json has {sorted(gmap)}')
    manifest = load(crops_dir / 'manifest.json') if (crops_dir / 'manifest.json').is_file() else {}
    for g in rep['groups']:
        src = gmap.get(g['id'])
        if not src:
            continue
        for k in GROUP_FIELDS:
            if g[k] != src[k]:
                errs.append(f'{g["id"]}: {k} differs from groups.json')
        for c in g['crops']:
            for kind in ('baseline', 'current', 'diff'):
                f = crops_dir / c[kind]
                if not f.is_file():
                    errs.append(f'{g["id"]}: crop {c[kind]} is missing')
                elif hashlib.sha256(f.read_bytes()).hexdigest() != manifest.get(c[kind]):
                    errs.append(f'{g["id"]}: crop {c[kind]} does not match crops/manifest.json')
        if g['intent'] != 'unknown':
            if g['intent_basis'] != 'agent_judgment':
                errs.append(f'{g["id"]}: an intent judgment must be marked agent_judgment')
            if rep['scope']['change_context'] is None:
                errs.append(f'{g["id"]}: intent {g["intent"]} with no change context to judge it against')
            if g['intent_confidence'] not in ('low', 'medium'):
                errs.append(f'{g["id"]}: intent confidence must be low or medium, never high')
        elif g['intent_basis'] != 'none' or g['intent_confidence'] is not None:
            errs.append(f'{g["id"]}: an unknown intent has basis none and no confidence')
        if APPROVAL.search(g['explanation']):
            errs.append(f'{g["id"]}: the explanation claims approval; accepting a change is the user\'s decision')

    # review order
    order = sorted(rep['groups'], key=lambda g: (INTENT_ORDER[g['intent']], -g['review_score'], g['id']))
    for i, g in enumerate(order, 1):
        if g['review_order'] != i:
            errs.append(f'{g["id"]}: review_order {g["review_order"]}, expected {i} (unintended, unknown, intended; then review score)')

    # pixel accounting
    by_pair: dict[str, list[int]] = {}
    for g in groups['groups']:
        for inst in g['instances']:
            t = by_pair.setdefault(inst['pair'], [0, 0])
            t[0] += inst['pixels'] + inst['aa_pixels']
            t[1] += inst['flaky_pixels']
    pairs = {p['rel']: p for p in diff['pairs']}
    for t in groups['totals']:
        p = pairs.get(t['pair'])
        if not p:
            errs.append(f'totals: {t["pair"]} is not in diff.json')
            continue
        got = by_pair.get(t['pair'], [0, 0])
        if got[0] + t['ungrouped_pixels'] != p['changed_pixels'] + p['aa_pixels']:
            errs.append(f'{t["pair"]}: grouped {got[0]} + ungrouped {t["ungrouped_pixels"]} != changed and anti-aliasing '
                        f'{p["changed_pixels"] + p["aa_pixels"]}')
        if got[1] + t['ungrouped_flaky_pixels'] != p['flaky_pixels']:
            errs.append(f'{t["pair"]}: flaky pixels grouped {got[1]} + ungrouped {t["ungrouped_flaky_pixels"]} != {p["flaky_pixels"]}')
    if sorted(t['pair'] for t in groups['totals']) != sorted(pairs):
        errs.append('groups.json totals do not cover every pair in diff.json; rerun group.py')

    # coverage lists
    for k in ('unchanged', 'added', 'removed', 'untested'):
        if rep[k] != groups[k]:
            errs.append(f'{k} differs from groups.json')
    one_set = set(diff['added']) | set(diff['removed'])
    for u in rep['unchanged']:
        if u['pair'] in one_set:
            errs.append(f'unchanged: {u["pair"]} exists in only one set')
        if u['pair'] in by_pair:
            errs.append(f'unchanged: {u["pair"]} has grouped changes')
    md = rep['metadata']['pairs']
    if md != {'matched': len(diff['pairs']), 'added': len(diff['added']), 'removed': len(diff['removed']),
              'size_mismatch': sum(p['size_mismatch'] for p in diff['pairs'])}:
        errs.append('metadata.pairs does not match diff.json')

    # headline
    hl = rep['headline']
    exp = expected_headline(rep, groups, diff)
    for k, v in exp.items():
        if hl[k] != v:
            if a.write_summary:
                hl[k] = v
            else:
                errs.append(f'headline.{k} is {hl[k]!r}; expected {v!r}')
    if APPROVAL.search(hl['summary']):
        errs.append('headline.summary claims approval')
    if NO_CHANGE.search(hl['summary']) and (rep['untested'] or rep['added'] or rep['removed']) and 'untested' not in hl['summary'].lower():
        errs.append('headline.summary says there are no visual changes but does not say what was untested, added or removed')
    if rep['files_written'] and not any('apply_review' in x for x in rep['limitations']):
        errs.append('files_written is only for baselines copied by apply_review.py at the user\'s request; say so in limitations')

    if a.write_summary and not errs:
        a.review.write_text(json.dumps(rep, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
        print(f'headline written to {a.review}')
    if errs:
        print('\n'.join(errs))
        print(f'INVALID: {len(errs)} error(s)')
        return 1
    print(f'VALID: {len(rep["groups"])} group(s) over {len(diff["pairs"])} pair(s); {len(rep["unchanged"])} unchanged, '
          f'{len(rep["untested"])} untested')
    return 0


if __name__ == '__main__':
    sys.exit(main())
