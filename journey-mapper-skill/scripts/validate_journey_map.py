#!/usr/bin/env python3
"""Validate a journey_map.json: required structure plus the honesty checks the skill depends on
(every step has code evidence, every surface item is accounted for, coverage arithmetic adds up,
the mapper never claims a status other than "inferred"). Prints every problem, not just the first."""
from __future__ import annotations
import json, re, sys
from pathlib import Path

PLATFORMS = {'web', 'ios', 'android', 'desktop', 'api', 'cli'}
BUCKETS = {'routes': 'route', 'screens': 'screen', 'endpoints': 'endpoint', 'entry_points': 'entry_point', 'goals': 'goal'}
ORPHAN_REASONS = {'no_user_path_found', 'not_user_facing', 'out_of_scope', 'needs_review', 'false_positive'}
UNREACHABLE_REASONS = {'dead', 'flag_off', 'guarded_unreachable'}
NODE_KINDS = {'entry', 'screen', 'state', 'goal'}
JRN_RE = re.compile(r'^JRN-[A-Z0-9]+-\d{3}$')
DOMAIN_RE = re.compile(r'^[a-z0-9][a-z0-9-]*$')
TARGET_RE = re.compile(r'^(step (\d+)|(V\d+))$')


def check(d: dict) -> list[str]:
    errs: list[str] = []
    e = errs.append
    for key in ('metadata', 'surface', 'coverage', 'journeys'):
        if key not in d:
            e(f'missing {key}')
    if errs:
        return errs

    meta = d['metadata']
    for key in ('repository', 'generated_at', 'platforms', 'run_scope'):
        if key not in meta: e(f'metadata.{key} missing')
    if set(meta.get('platforms') or []) - PLATFORMS:
        e(f'metadata.platforms has unknown values: {sorted(set(meta["platforms"]) - PLATFORMS)}')
    scope = (meta.get('run_scope') or {}).get('domains')
    if scope != 'all' and not (isinstance(scope, list) and scope):
        e('metadata.run_scope.domains must be "all" or a non-empty list')

    def evidence_ok(ev, where):
        if not isinstance(ev, dict) or not ev.get('file') or not isinstance(ev.get('line'), int) or ev['line'] < 1:
            e(f'{where}: evidence needs file and line >= 1')

    surface_ids: dict[str, str] = {}
    for bucket, kind in BUCKETS.items():
        items = d['surface'].get(bucket)
        if not isinstance(items, list):
            e(f'surface.{bucket} must be a list'); continue
        for it in items:
            sid = it.get('id')
            if not sid: e(f'surface.{bucket}: item without id'); continue
            if sid in surface_ids: e(f'duplicate surface id {sid}')
            surface_ids[sid] = kind
            if it.get('kind') != kind: e(f'{sid}: kind {it.get("kind")!r} does not match bucket {bucket}')
            if not it.get('name'): e(f'{sid}: missing name')
            evidence_ok(it, sid)
            if it.get('confidence') not in ('high', 'medium', 'low'): e(f'{sid}: confidence must be high|medium|low')
            if it.get('source') not in ('extractor', 'agent'): e(f'{sid}: source must be extractor|agent')

    covered: set[str] = set()
    jids: set[str] = set()
    for j in d['journeys']:
        jid = j.get('id', '?')
        w = f'journey {jid}'
        if not JRN_RE.match(str(jid)): e(f'{w}: id must match JRN-<DOMAIN>-<NNN>')
        if jid in jids: e(f'{w}: duplicate id')
        jids.add(jid)
        for key in ('title', 'persona', 'priority_reason', 'entry_point', 'goal'):
            if not j.get(key): e(f'{w}: {key} missing')
        if not DOMAIN_RE.match(str(j.get('domain', ''))): e(f'{w}: domain must be a lowercase slug')
        if not j.get('platforms') or set(j['platforms']) - PLATFORMS: e(f'{w}: platforms missing or invalid')
        if j.get('priority') not in ('P1', 'P2', 'P3'): e(f'{w}: priority must be P1|P2|P3')
        if j.get('status', 'inferred') != 'inferred':
            e(f'{w}: the mapper may only emit status "inferred" (people promote journeys, not tools)')
        if j.get('automation') not in ('none', 'partial', 'automated'): e(f'{w}: automation must be none|partial|automated')
        if j.get('automation') in ('partial', 'automated') and not j.get('tests'):
            e(f'{w}: automation is {j["automation"]} but no tests are listed')
        covers = j.get('covers') or []
        if not covers: e(f'{w}: covers must list at least one surface item')
        for c in covers:
            if c not in surface_ids: e(f'{w}: covers unknown surface item {c}')
            else: covered.add(c)
        steps = j.get('steps') or []
        if not steps: e(f'{w}: no steps')
        for n, s in enumerate(steps, 1):
            if not s.get('action'): e(f'{w} step {n}: action missing')
            if not s.get('expect'): e(f'{w} step {n}: expect missing (every step needs an observable outcome)')
            if not s.get('evidence'): e(f'{w} step {n}: no code evidence')
            for ev in s.get('evidence') or []: evidence_ok(ev, f'{w} step {n}')
        vids: set[str] = set()
        for v in j.get('variants') or []:
            vid = v.get('id', '?')
            if not re.match(r'^V\d+$', str(vid)): e(f'{w}: variant id {vid!r} must look like V1')
            if vid in vids: e(f'{w}: duplicate variant {vid}')
            vids.add(vid)
            if not isinstance(v.get('from_step'), int) or not 1 <= v['from_step'] <= len(steps):
                e(f'{w} {vid}: from_step must reference an existing step')
            if not v.get('name') or not v.get('expect'): e(f'{w} {vid}: name and expect are required')
            for ev in v.get('evidence') or []: evidence_ok(ev, f'{w} {vid}')
        for na in j.get('not_automatable') or []:
            m = TARGET_RE.match(str(na.get('target', '')))
            if not m: e(f'{w}: not_automatable target must be "step N" or "Vn"'); continue
            if m.group(2) and not 1 <= int(m.group(2)) <= len(steps): e(f'{w}: not_automatable refers to unknown {m.group(1)}')
            if m.group(3) and m.group(3) not in vids: e(f'{w}: not_automatable refers to unknown variant {m.group(3)}')
            if not na.get('reason'): e(f'{w}: not_automatable entry needs a reason')
        for ev in j.get('evidence') or []: evidence_ok(ev, f'{w} journey-level')

    cov = d['coverage']
    orphan_ids, unreachable_ids = set(), set()
    for o in cov.get('orphans') or []:
        if o.get('item') not in surface_ids: e(f'orphan refers to unknown surface item {o.get("item")}')
        if o.get('reason') not in ORPHAN_REASONS: e(f'orphan {o.get("item")}: reason must be one of {sorted(ORPHAN_REASONS)}')
        orphan_ids.add(o.get('item'))
    for u in cov.get('unreachable') or []:
        if u.get('item') not in surface_ids: e(f'unreachable refers to unknown surface item {u.get("item")}')
        if u.get('reason') not in UNREACHABLE_REASONS: e(f'unreachable {u.get("item")}: reason must be one of {sorted(UNREACHABLE_REASONS)}')
        evidence_ok(u.get('evidence'), f'unreachable {u.get("item")}')
        unreachable_ids.add(u.get('item'))
    for a, b, label in ((covered, orphan_ids, 'in a journey and orphaned'), (covered, unreachable_ids, 'in a journey and unreachable'),
                        (orphan_ids, unreachable_ids, 'both orphaned and unreachable')):
        for sid in sorted(a & b): e(f'{sid} is {label}')
    missing = set(surface_ids) - covered - orphan_ids - unreachable_ids
    if missing:
        e(f'{len(missing)} surface item(s) not accounted for (cover them, or list as orphan/unreachable): {sorted(missing)[:10]}')
    if cov.get('surface_items_total') != len(surface_ids):
        e(f'coverage.surface_items_total is {cov.get("surface_items_total")} but surface has {len(surface_ids)} items')
    if cov.get('in_journey') != len(covered):
        e(f'coverage.in_journey is {cov.get("in_journey")} but journeys cover {len(covered)} distinct items')

    g = d.get('graph')
    if g is not None:
        nids = set()
        for n in g.get('nodes') or []:
            if n.get('id') in nids: e(f'graph: duplicate node {n.get("id")}')
            nids.add(n.get('id'))
            if n.get('kind') not in NODE_KINDS: e(f'graph node {n.get("id")}: kind must be one of {sorted(NODE_KINDS)}')
            if n.get('surface_item') and n['surface_item'] not in surface_ids: e(f'graph node {n.get("id")}: unknown surface_item')
        for ed in g.get('edges') or []:
            for end in ('from', 'to'):
                if ed.get(end) not in nids: e(f'graph edge {ed.get("from")}->{ed.get("to")}: unknown {end} node')
            if not ed.get('action'): e(f'graph edge {ed.get("from")}->{ed.get("to")}: action missing')
    return errs


def main() -> int:
    if len(sys.argv) != 2:
        print('usage: validate_journey_map.py journey_map.json', file=sys.stderr); return 2
    try:
        d = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    except Exception as exc:
        print(f'INVALID: {exc}', file=sys.stderr); return 1
    errs = check(d)
    if errs:
        for msg in errs: print(f'INVALID: {msg}', file=sys.stderr)
        print(f'{len(errs)} problem(s)', file=sys.stderr)
        return 1
    print('VALID')
    return 0

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
