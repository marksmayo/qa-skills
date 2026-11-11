#!/usr/bin/env python3
"""Validate a migration review against its contract, and recompute every rating and count.

    python validate_migration_review.py review.json --candidates run/candidates.json [--migrations run/migrations.json]
        [--dynamic run/dynamic.json] [--repo examples/ledger] [--write-summary]

Fails when: a required field is missing; a finding has no file, line, statement or engine range; a hazard,
size factor, exposure, score or priority differs from the rubric; a size has no source, or unknown is rated
below large; an assumed finding is above P1; a floor is missed; a compatibility finding has no code hits or
counts hits in tests or migrations; a measured finding has no dynamic result, or the dynamic run touched a
database the skill didn't create or a non-loopback host; any candidate isn't triaged exactly once; a data-loss
candidate is dismissed without the intended-contract-step reason and zero code readers; the headline counts
or verdict are wrong, or it says "safe" without the scope statement; row values appear in the dynamic results.
With --repo, every cited line must exist, and code hits must contain the column name.
With --write-summary, the headline is recomputed and written back.
"""
from __future__ import annotations
import argparse, ipaddress, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import find_readers  # noqa: E402

RULES = json.loads((HERE.parent / 'data' / 'rules.json').read_text(encoding='utf-8'))['rules']
SIZE = {'large': 3, 'unknown': 3, 'medium': 2, 'small': 1}
EXPOSURE = {'rolling': 3, 'blue_green': 2, 'stop_the_world': 1}
ORDER = ['P0', 'P1', 'P2', 'P3']
DISMISS = {'table_created_in_release', 'constant_default_pg11', 'intended_contract_step', 'validated_check_present',
           'online_schema_tool', 'not_applicable_engine_version', 'duplicate', 'false_match'}
DYN_KEYS = {'id', 'up', 'down', 'locks', 'probes', 'rows_changed'}


def rate(category: str, hazard: int, size_class: str, deploy: str) -> tuple[int, int, int]:
    size = 3 if category in ('data_loss', 'compatibility') else SIZE[size_class]
    exposure = 3 if category == 'data_loss' else EXPOSURE[deploy]
    return size, exposure, hazard * size * exposure


def priority(score: int) -> str:
    return 'P0' if score >= 27 else 'P1' if score >= 18 else 'P2' if score >= 8 else 'P3'


def expected_priority(f: dict, deploy: str) -> tuple[str, str | None, str | None]:
    _, _, score = rate(f['category'], f['hazard'], f['size']['class'], deploy)
    p, floor, cap = priority(score), None, None
    if f['category'] == 'data_loss' and not f.get('recoverable', False):
        floor = 'unrecoverable_data_loss'
        p = 'P0'
    elif f['category'] == 'compatibility' and f.get('code_hits') and ORDER.index(p) > 1:
        floor, p = 'compatibility_with_code_hits', 'P1'
    if f.get('confidence') == 'assumed' and p == 'P0':
        cap, p = 'assumed_capped_at_P1', 'P1'
    return p, floor, cap


def summary(rep: dict) -> dict:
    counts = {p: sum(1 for f in rep['findings'] if f['priority'] == p) for p in ORDER}
    p0 = sorted({f['migration'] for f in rep['findings'] if f['priority'] == 'P0' and f.get('migration')})
    verdict = 'do_not_deploy' if counts['P0'] else 'deploy_with_changes' if counts['P1'] or counts['P2'] else 'no_blocking_findings'
    return {'counts': counts, 'migrations_with_p0': p0, 'verdict': verdict}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('review', type=Path)
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--migrations', type=Path)
    ap.add_argument('--dynamic', type=Path)
    ap.add_argument('--repo', type=Path)
    ap.add_argument('--write-summary', action='store_true')
    a = ap.parse_args()
    rep = json.loads(a.review.read_text(encoding='utf-8'))
    cands = {c['id']: c for c in json.loads(a.candidates.read_text(encoding='utf-8'))['candidates']}
    errs: list[str] = []
    e = errs.append

    for k in ('schema_version', 'skill', 'scope', 'metadata', 'migrations', 'findings', 'triage', 'dynamic', 'headline', 'limitations'):
        if k not in rep:
            e(f'missing top-level field {k}')
    if errs:
        print('\n'.join(errs)); return 1
    scope = rep['scope']
    deploy = scope.get('deploy_model')
    if deploy not in EXPOSURE:
        e(f'scope.deploy_model {deploy!r} is not one of {sorted(EXPOSURE)}')
        deploy = 'rolling'
    for k in ('repo', 'tool', 'engine', 'migrations_reviewed', 'checked', 'not_checked'):
        if not scope.get(k):
            e(f'scope.{k} is missing or empty')
    eng = scope.get('engine') or {}
    if not eng.get('source'):
        e('scope.engine has no source (how the engine and version were found)')

    if a.migrations:
        md = json.loads(a.migrations.read_text(encoding='utf-8'))
        pend = [m['id'] for m in md['migrations'] if m.get('pending')]
        if scope.get('migrations_reviewed') != pend:
            e(f'scope.migrations_reviewed {scope.get("migrations_reviewed")} differs from the pending set {pend}')
        mig_by_id = {m['id']: m for m in md['migrations']}
        for m in rep['migrations']:
            src = mig_by_id.get(m['id'])
            if not src:
                e(f'migration {m["id"]} is not in the migrations file'); continue
            for k in ('file', 'down_state', 'transactional'):
                if m.get(k) != src.get(k):
                    e(f'migration {m["id"]}: {k} {m.get(k)!r} differs from the migrations file ({src.get(k)!r})')

    # dynamic
    dyn = rep['dynamic']
    if dyn.get('status') not in ('ran', 'not_run'):
        e('dynamic.status must be ran or not_run')
    if dyn.get('status') == 'not_run' and not dyn.get('reason'):
        e('dynamic is not_run without a reason')
    measured_ids: set = set()
    if dyn.get('status') == 'ran':
        if not dyn.get('created_by_skill') or not str(dyn.get('database', '')).startswith('qa_migrate_'):
            e('dynamic results come from a database the skill did not create')
        try:
            if not ipaddress.ip_address(dyn.get('host', '')).is_loopback:
                e(f'dynamic host {dyn.get("host")} is not loopback')
        except ValueError:
            if dyn.get('host') != 'localhost':
                e(f'dynamic host {dyn.get("host")!r} is not a loopback address')
        if a.dynamic:
            raw = json.loads(a.dynamic.read_text(encoding='utf-8'))
            if raw.get('migrations') != dyn.get('migrations'):
                e('dynamic.migrations differ from the dynamic results file')
        for m in dyn.get('migrations', []):
            extra = set(m) - DYN_KEYS
            if extra:
                e(f'dynamic result for {m.get("id")} has fields {sorted(extra)} that could carry row values')
            measured_ids.add(m.get('id'))

    # findings
    fids = set()
    for f in rep['findings']:
        fid = f.get('id', '?')
        if fid in fids:
            e(f'{fid}: duplicate finding id')
        fids.add(fid)
        for k in ('file', 'line', 'statement', 'engine_range', 'effect', 'rewrite', 'verification', 'candidates'):
            if not f.get(k):
                e(f'{fid}: missing {k}')
        rule = RULES.get(f.get('rule'))
        if not rule:
            e(f'{fid}: unknown rule {f.get("rule")}'); continue
        if f.get('category') != rule['category'] or f.get('hazard') != rule['hazard']:
            e(f'{fid}: category/hazard {f.get("category")}/{f.get("hazard")} differ from the rule ({rule["category"]}/{rule["hazard"]})')
            continue
        sz = f.get('size') or {}
        if sz.get('class') not in SIZE:
            e(f'{fid}: size class {sz.get("class")!r} unknown')
            continue
        if not sz.get('source'):
            e(f'{fid}: table size has no source')
        size_f, exp_f, score = rate(f['category'], f['hazard'], sz['class'], deploy)
        if (f.get('size_factor'), f.get('exposure'), f.get('score')) != (size_f, exp_f, score):
            e(f'{fid}: size/exposure/score {f.get("size_factor")}/{f.get("exposure")}/{f.get("score")} should be {size_f}/{exp_f}/{score}')
        p, floor, cap = expected_priority(f, deploy)
        if f.get('priority') != p:
            e(f'{fid}: priority {f.get("priority")} should be {p} (score {score}{", floor " + floor if floor else ""}{", " + cap if cap else ""})')
        if f.get('floor') != floor or f.get('cap') != cap:
            e(f'{fid}: floor/cap {f.get("floor")}/{f.get("cap")} should be {floor}/{cap}')
        if f.get('confidence') not in ('measured', 'matched', 'assumed'):
            e(f'{fid}: confidence {f.get("confidence")!r}')
        if f.get('confidence') == 'measured' and f.get('migration') not in measured_ids:
            e(f'{fid}: measured without a dynamic result for migration {f.get("migration")}')
        if (sz['class'] == 'unknown' or eng.get('assumed')) and f.get('confidence') == 'matched' and f['category'] not in ('data_loss', 'compatibility', 'irreversible'):
            e(f'{fid}: depends on an unknown (table size or engine version) but is rated matched, not assumed')
        if f['category'] == 'compatibility':
            if not f.get('code_hits'):
                e(f'{fid}: compatibility finding without code hits')
            for h in f.get('code_hits', []):
                if find_readers.classify(h['file']) != 'code':
                    e(f'{fid}: code hit {h["file"]} is a test or migration file and must not count')
        for cid in f.get('candidates', []):
            c = cands.get(cid)
            if not c:
                e(f'{fid}: candidate {cid} is not in the candidates file'); continue
            if c['source'] == 'builtin':
                for k in ('rule', 'file', 'line', 'statement', 'table'):
                    if c.get(k) != f.get(k):
                        e(f'{fid}: {k} {f.get(k)!r} differs from candidate {cid} ({c.get(k)!r})')
                if c.get('code_hits') != f.get('code_hits'):
                    e(f'{fid}: code hits differ from candidate {cid}')
                if (c.get('size') or {}).get('class') != sz['class'] or (c.get('size') or {}).get('source') != sz.get('source'):
                    e(f'{fid}: size differs from candidate {cid}')
        if a.repo:
            p_ = a.repo / f['file']
            if not p_.exists():
                e(f'{fid}: {f["file"]} not found in the repo')
            elif f['line'] > len(p_.read_text(encoding='utf-8').splitlines()):
                e(f'{fid}: line {f["line"]} is past the end of {f["file"]}')
            for h in f.get('code_hits', []):
                lines = (a.repo / h['file']).read_text(encoding='utf-8').splitlines() if (a.repo / h['file']).exists() else []
                word = f.get('column') if f['rule'] != 'NOT-NULL-WHILE-WRITTEN-WITHOUT' else None
                if h['line'] > len(lines) or (word and not re.search(rf'\b{re.escape(word)}\b', lines[h['line'] - 1])):
                    e(f'{fid}: code hit {h["file"]}:{h["line"]} does not mention {word}')

    # triage
    seen: dict = {}
    for t in rep['triage']:
        cid = t.get('candidate')
        if cid in seen:
            e(f'candidate {cid} is triaged twice')
        seen[cid] = t
        c = cands.get(cid)
        if not c:
            e(f'triage names {cid}, which is not in the candidates file'); continue
        if t.get('decision') == 'promoted':
            f = next((x for x in rep['findings'] if x['id'] == t.get('finding')), None)
            if not f or cid not in f.get('candidates', []):
                e(f'{cid}: promoted to {t.get("finding")}, which does not list it')
        elif t.get('decision') == 'dismissed':
            if t.get('reason_code') not in DISMISS:
                e(f'{cid}: dismissal reason_code {t.get("reason_code")!r} not in {sorted(DISMISS)}')
            if not t.get('reason'):
                e(f'{cid}: dismissed without a reason')
            if c.get('category') == 'data_loss':
                if t.get('reason_code') != 'intended_contract_step' or (c.get('facts') or {}).get('code_readers', 1) != 0:
                    e(f'{cid}: a data-loss candidate may only be dismissed as an intended contract step with zero code readers')
            if t.get('reason_code') == 'table_created_in_release' and not (c.get('facts') or {}).get('table_created_in_release'):
                e(f'{cid}: dismissed as a new table, but the table was not created in this release')
            if t.get('reason_code') == 'constant_default_pg11':
                fx = c.get('facts') or {}
                if fx.get('volatile') or not fx.get('engine_version_11_or_later'):
                    e(f'{cid}: dismissed as a constant default on PostgreSQL 11+, but the facts do not support it')
        else:
            e(f'{cid}: decision must be promoted or dismissed')
    missing = sorted(set(cands) - set(seen))
    if missing:
        e(f'candidates not triaged: {missing}')
    for f in rep['findings']:
        for cid in f.get('candidates', []):
            if (seen.get(cid) or {}).get('finding') != f['id']:
                e(f'{f["id"]}: lists {cid}, but triage does not promote it to this finding')

    # headline
    want = summary(rep)
    if a.write_summary:
        rep['headline'].update(want)
        a.review.write_text(json.dumps(rep, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    hl = rep['headline']
    for k, v in want.items():
        if hl.get(k) != v:
            e(f'headline.{k} {hl.get(k)!r} should be {v!r}')
    stmt = hl.get('statement', '')
    if re.search(r'\bsafe\b', stmt, re.I) and not hl.get('scope_statement'):
        e('headline says "safe" without a scope_statement')
    if not hl.get('scope_statement'):
        e('headline has no scope_statement (what was and was not checked)')
    if dyn.get('status') == 'not_run' and not any('dynamic' in x.lower() for x in rep['limitations']):
        e('dynamic check not run, but limitations do not say so')

    if errs:
        print(f'{a.review}: {len(errs)} problem(s)')
        for x in errs:
            print('  -', x)
        return 1
    c = want['counts']
    print(f'{a.review}: valid. {len(rep["findings"])} finding(s) (P0 {c["P0"]}, P1 {c["P1"]}, P2 {c["P2"]}, P3 {c["P3"]}); '
          f'{len(seen)} candidate(s) triaged; verdict {want["verdict"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
