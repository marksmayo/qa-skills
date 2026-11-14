#!/usr/bin/env python3
"""Validate a journey_map.json: required structure plus the honesty checks the skill depends on
(every step has code evidence, every surface item is accounted for, coverage arithmetic adds up,
the mapper never claims a status other than "inferred", out_of_scope orphans really are outside the
run scope, and the graph is recorded or explicitly declined). With --repo it also re-reads every cited
file and line (missing files, lines past the end, blank lines, excerpts that don't match) and checks
that each journey's tests exist. Prints every problem, not just the first.

Warnings (errors with --strict): a journey covers a surface item whose file none of the journey's
evidence cites, or whose file it cites only outside the item's own lines when several items share the
file (a single-file app: each handler runs from its line to the next item's line), which is padded
coverage; or a map-level open question repeats a journey's question. Entries of the wrong shape (a
not_automatable entry written as a "step N: reason" string) are reported, never a crash.

    python validate_journey_map.py journeys/journey_map.json [--repo <repository root>] [--strict]
    python validate_journey_map.py --self-test"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

PLATFORMS = {'web', 'ios', 'android', 'desktop', 'api', 'cli'}
BUCKETS = {'routes': 'route', 'screens': 'screen', 'endpoints': 'endpoint', 'entry_points': 'entry_point', 'goals': 'goal'}
ORPHAN_REASONS = {'no_user_path_found', 'not_user_facing', 'out_of_scope', 'not_mapped_this_run', 'needs_review',
                  'false_positive'}
UNREACHABLE_REASONS = {'dead', 'flag_off', 'guarded_unreachable'}
NODE_KINDS = {'entry', 'screen', 'state', 'goal'}
JRN_RE = re.compile(r'^JRN-[A-Z0-9]+-\d{3}$')
DOMAIN_RE = re.compile(r'^[a-z0-9][a-z0-9-]*$')
TARGET_RE = re.compile(r'^(step (\d+)|(V\d+))$')


class Repo:
    """Re-reads cited files so evidence can't point at a missing file, a line past the end or a blank line."""
    def __init__(self, root: Path):
        self.root = root
        self.cache: dict[str, list[str] | None] = {}

    def lines(self, rel: str):
        if rel not in self.cache:
            p = self.root / rel
            try:
                self.cache[rel] = p.read_text(encoding='utf-8', errors='replace').splitlines() if p.is_file() else None
            except OSError:
                self.cache[rel] = None
        return self.cache[rel]

    def check(self, ev: dict, where: str, e, allow_blank=False):
        rel, line = ev.get('file'), ev.get('line')
        if not rel or not isinstance(line, int):
            return
        lines = self.lines(rel)
        if lines is None:
            e(f'{where}: cited file {rel} does not exist in the repository'); return
        if not 1 <= line <= max(len(lines), 1):
            e(f'{where}: {rel}:{line} is past the end of the file ({len(lines)} lines)'); return
        text = lines[line - 1] if lines else ''
        if not allow_blank and not text.strip():
            e(f'{where}: {rel}:{line} is a blank line; cite the line that handles the action or produces the outcome')
        exc = ev.get('excerpt')
        if exc and ' '.join(exc.split()) not in ' '.join(text.split()):
            e(f'{where}: excerpt {exc!r} is not on {rel}:{line}')

    def test_exists(self, ref: str) -> bool:
        path = re.split(r'::|#', ref, maxsplit=1)[0]
        path = re.sub(r':\d+(:\d+)?$', '', path).strip()
        if any(ch in path for ch in '*?['):
            return any(self.root.glob(path))
        return (self.root / path).exists()


def item_domain(o: dict, items: dict) -> str | None:
    return o.get('domain') or (items.get(o.get('item')) or {}).get('domain')


def norm_q(text) -> str:
    return ' '.join(str(text).lower().split())


def cited_files(j: dict) -> set[str]:
    files = set()
    for s in j.get('steps') or []:
        files |= {ev.get('file') for ev in s.get('evidence') or [] if isinstance(ev, dict)}
    for v in j.get('variants') or []:
        files |= {ev.get('file') for ev in v.get('evidence') or [] if isinstance(ev, dict)}
    files |= {ev.get('file') for ev in j.get('evidence') or [] if isinstance(ev, dict)}
    return {f for f in files if f}


def cited_lines(j: dict) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    groups = [s.get('evidence') for s in j.get('steps') or [] if isinstance(s, dict)]
    groups += [v.get('evidence') for v in j.get('variants') or [] if isinstance(v, dict)]
    groups.append(j.get('evidence'))
    for evs in groups:
        for ev in evs or []:
            if isinstance(ev, dict) and ev.get('file') and isinstance(ev.get('line'), int):
                out.setdefault(ev['file'], set()).add(ev['line'])
    return out


def item_spans(items: dict) -> dict[str, tuple[int, float]]:
    """Each located route, screen, endpoint or entry point's line span in its file: from its line to the next such
    item's line in the same file (the rest of the file for the last). A single-file app (every handler in app.py)
    is then checked per handler, not per file."""
    by_file: dict[str, list[tuple[int, str]]] = {}
    for sid, it in items.items():
        if it.get('kind') in ('route', 'screen', 'endpoint', 'entry_point') and it.get('file') \
                and isinstance(it.get('line'), int) and it['line'] > 1:
            by_file.setdefault(it['file'], []).append((it['line'], sid))
    spans: dict[str, tuple[int, float]] = {}
    for rows in by_file.values():
        if len(rows) < 2:
            continue
        starts = sorted({ln for ln, _ in rows})
        for ln, sid in rows:
            later = [x for x in starts if x > ln]
            spans[sid] = (ln, later[0] if later else float('inf'))
    return spans


def warnings_for(d: dict) -> list[str]:
    """Problems worth a person's look that don't make the map invalid (errors with --strict)."""
    warns: list[str] = []
    items = {it.get('id'): it for bucket in BUCKETS for it in (d.get('surface') or {}).get(bucket) or []
             if isinstance(it, dict)}
    spans = item_spans(items)
    for j in d.get('journeys') or []:
        files = cited_files(j)
        lines = cited_lines(j)
        for c in j.get('covers') or []:
            it = items.get(c)
            if it and it.get('file') and it['file'] not in files:
                warns.append(f'journey {j.get("id")} covers {c} ({it.get("name")}), but no evidence in the journey cites '
                             f'{it["file"]}; cite the line where the journey reaches it, or drop it from covers')
            elif it and c in spans and it['file'] in lines:
                lo, hi = spans[c]
                if not any(lo <= ln < hi for ln in lines[it['file']]):
                    upto = 'the end of the file' if hi == float('inf') else f'line {hi}, the next item'
                    warns.append(f'journey {j.get("id")} covers {c} ({it.get("name")}, {it["file"]}:{lo}), but its evidence '
                                 f'cites {it["file"]} only outside that item\'s lines ({lo} up to {upto}); cite the line '
                                 f'where the journey reaches it, or drop it from covers')
    journey_qs = {norm_q(q) for j in d.get('journeys') or [] for q in j.get('open_questions') or []}
    for q in d.get('open_questions') or []:
        if norm_q(q.get('question', '')) in journey_qs:
            warns.append(f'open question {q.get("id")} repeats an open question of a journey; map-level open_questions are '
                         f'for questions that span journeys (reports count each text once)')
    return warns


def check(d: dict, repo: Repo | None = None) -> list[str]:
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

    scoped = isinstance(scope, list) and bool(scope)

    def evidence_ok(ev, where, allow_blank=False):
        if not isinstance(ev, dict) or not ev.get('file') or not isinstance(ev.get('line'), int) or ev['line'] < 1:
            e(f'{where}: evidence needs file and line >= 1'); return
        if repo:
            repo.check(ev, where, e, allow_blank)

    surface_ids: dict[str, str] = {}
    surface_items: dict[str, dict] = {}
    for bucket, kind in BUCKETS.items():
        items = d['surface'].get(bucket)
        if not isinstance(items, list):
            e(f'surface.{bucket} must be a list'); continue
        for it in items:
            sid = it.get('id')
            if not sid: e(f'surface.{bucket}: item without id'); continue
            if sid in surface_ids: e(f'duplicate surface id {sid}')
            surface_ids[sid] = kind
            surface_items[sid] = it
            if it.get('kind') != kind: e(f'{sid}: kind {it.get("kind")!r} does not match bucket {bucket}')
            if not it.get('name'): e(f'{sid}: missing name')
            evidence_ok(it, sid, allow_blank=True)   # file-based routes cite line 1, which may be blank
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
        elif scoped and j['domain'] not in scope:
            e(f'{w}: domain {j["domain"]!r} is outside the run scope {scope}')
        if repo:
            for t in j.get('tests') or []:
                if not repo.test_exists(str(t)): e(f'{w}: test {t} does not exist in the repository')
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
            if not isinstance(s, dict):
                e(f'{w} step {n}: must be an object with action, expect and evidence, not {type(s).__name__}'); continue
            if not s.get('action'): e(f'{w} step {n}: action missing')
            if not s.get('expect'): e(f'{w} step {n}: expect missing (every step needs an observable outcome)')
            if not s.get('evidence'): e(f'{w} step {n}: no code evidence')
            for ev in s.get('evidence') or []: evidence_ok(ev, f'{w} step {n}')
        vids: set[str] = set()
        for v in j.get('variants') or []:
            if not isinstance(v, dict):
                e(f'{w}: each variant must be an object with id, from_step, name and expect, not {type(v).__name__}'); continue
            vid = v.get('id', '?')
            if not re.match(r'^V\d+$', str(vid)): e(f'{w}: variant id {vid!r} must look like V1')
            if vid in vids: e(f'{w}: duplicate variant {vid}')
            vids.add(vid)
            if not isinstance(v.get('from_step'), int) or not 1 <= v['from_step'] <= len(steps):
                e(f'{w} {vid}: from_step must reference an existing step')
            if not v.get('name') or not v.get('expect'): e(f'{w} {vid}: name and expect are required')
            for ev in v.get('evidence') or []: evidence_ok(ev, f'{w} {vid}')
        for na in j.get('not_automatable') or []:
            if not isinstance(na, dict):
                e(f'{w}: each not_automatable entry must be an object {{"target": "step N" | "Vn", "reason": ...}}, '
                  f'not {type(na).__name__} {str(na)[:60]!r} (the Markdown "step N: reason" form belongs in the .md file)')
                continue
            m = TARGET_RE.match(str(na.get('target', '')))
            if not m: e(f'{w}: not_automatable target must be "step N" or "Vn"'); continue
            if m.group(2) and not 1 <= int(m.group(2)) <= len(steps): e(f'{w}: not_automatable refers to unknown {m.group(1)}')
            if m.group(3) and m.group(3) not in vids: e(f'{w}: not_automatable refers to unknown variant {m.group(3)}')
            if not na.get('reason'): e(f'{w}: not_automatable entry needs a reason')
        for ev in j.get('evidence') or []: evidence_ok(ev, f'{w} journey-level')

    cov = d['coverage']
    orphan_ids, unreachable_ids = set(), set()
    for o in cov.get('orphans') or []:
        if not isinstance(o, dict):
            e(f'coverage.orphans: each entry must be an object with item and reason, not {type(o).__name__}'); continue
        if o.get('item') not in surface_ids: e(f'orphan refers to unknown surface item {o.get("item")}')
        if o.get('reason') not in ORPHAN_REASONS: e(f'orphan {o.get("item")}: reason must be one of {sorted(ORPHAN_REASONS)}')
        if o.get('item') in orphan_ids: e(f'orphan {o.get("item")} is listed twice')
        orphan_ids.add(o.get('item'))
        dom = item_domain(o, surface_items)
        if o.get('reason') == 'out_of_scope':
            if not scoped:
                e(f'orphan {o.get("item")}: out_of_scope, but the run scope is all domains')
            elif not dom:
                e(f'orphan {o.get("item")}: out_of_scope needs the domain it belongs to (domain on the orphan or the item)')
            elif dom in scope:
                e(f'orphan {o.get("item")}: out_of_scope, but its domain {dom!r} is in the run scope {scope}; '
                  f'map it, or use not_mapped_this_run with a note')
        if o.get('reason') == 'not_mapped_this_run':
            if not o.get('note'):
                e(f'orphan {o.get("item")}: not_mapped_this_run needs a note naming the user path that was found')
            if scoped and dom and dom not in scope:
                e(f'orphan {o.get("item")}: not_mapped_this_run is for in-scope items; domain {dom!r} is out of scope (use out_of_scope)')
        if o.get('reason') == 'needs_review' and not o.get('note'):
            e(f'orphan {o.get("item")}: needs_review needs a note saying what a person must decide')
    for u in cov.get('unreachable') or []:
        if not isinstance(u, dict):
            e(f'coverage.unreachable: each entry must be an object with item, reason and evidence, not {type(u).__name__}'); continue
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
    declined = d.get('graph_declined')
    if g is None and not (isinstance(declined, str) and declined.strip()):
        e('graph missing: record the nodes and edges the journeys traverse (Step 3), or set graph_declined to the reason it was not built')
    if g is not None and declined:
        e('graph and graph_declined are both set; keep one')
    if g is not None:
        nids = set()
        for n in g.get('nodes') or []:
            if n.get('id') in nids: e(f'graph: duplicate node {n.get("id")}')
            nids.add(n.get('id'))
            if n.get('kind') not in NODE_KINDS: e(f'graph node {n.get("id")}: kind must be one of {sorted(NODE_KINDS)}')
            if n.get('surface_item') and n['surface_item'] not in surface_ids: e(f'graph node {n.get("id")}: unknown surface_item')
            if repo and n.get('file') and isinstance(n.get('line'), int): evidence_ok(n, f'graph node {n.get("id")}', allow_blank=True)
        for ed in g.get('edges') or []:
            for end in ('from', 'to'):
                if ed.get(end) not in nids: e(f'graph edge {ed.get("from")}->{ed.get("to")}: unknown {end} node')
            if not ed.get('action'): e(f'graph edge {ed.get("from")}->{ed.get("to")}: action missing')
            if repo and ed.get('file') and isinstance(ed.get('line'), int): evidence_ok(ed, f'graph edge {ed.get("from")}->{ed.get("to")}')
    return errs


def self_test() -> int:
    """Negative cases on a copy of the shipped example: each must be reported, never crash the validator."""
    import copy
    example = Path(__file__).resolve().parent.parent / 'examples' / 'journeys' / 'journey_map.json'
    base = json.loads(example.read_text(encoding='utf-8'))
    checks = []

    def run(d):
        try:
            return check(d, None), None
        except Exception as exc:   # a crash is a failed check: the validator must report, not raise
            return None, f'{type(exc).__name__}: {exc}'

    errs, crash = run(copy.deepcopy(base))
    checks.append(('the shipped example is valid', crash is None and not errs, crash or errs))
    d = copy.deepcopy(base)
    d['journeys'][0]['not_automatable'] = ['step 1: needs a real card']   # the Markdown form, not an object
    errs, crash = run(d)
    checks.append(('a not_automatable entry written as a string is reported, not a crash',
                   crash is None and any('not_automatable' in x and 'object' in x for x in errs or []), crash or errs))
    # a single-file app: two handlers in app.py; the journey cites a line of the first handler only, so covering the
    # second is padding even though the file is cited
    d = copy.deepcopy(base)
    d['surface']['endpoints'] += [
        {'id': 'E-901', 'kind': 'endpoint', 'name': 'GET /ferries', 'file': 'app.py', 'line': 10, 'detector': 'flask-route',
         'confidence': 'high', 'source': 'extractor'},
        {'id': 'E-902', 'kind': 'endpoint', 'name': 'GET /ferries/<slug>', 'file': 'app.py', 'line': 40,
         'detector': 'flask-route', 'confidence': 'high', 'source': 'extractor'}]
    j = d['journeys'][0]
    j['covers'] = list(j['covers']) + ['E-901', 'E-902']
    j['steps'][0]['evidence'] = list(j['steps'][0]['evidence']) + [{'file': 'app.py', 'line': 12}]
    warns = warnings_for(d)
    checks.append(('covering a handler of a cited file whose own lines no evidence cites is padded coverage',
                   any('E-902' in w for w in warns) and not any('E-901' in w for w in warns), warns))
    failed = 0
    for name, ok, extra in checks:
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} {name}" + ('' if ok else f'  [{extra}]'))
    print(f'{failed} failed' if failed else 'ALL PASS')
    return 1 if failed else 0


def main() -> int:
    if '--self-test' in sys.argv[1:]:
        return self_test()
    ap = argparse.ArgumentParser(description='Validate a journey_map.json.')
    ap.add_argument('journey_map', type=Path)
    ap.add_argument('--repo', type=Path, help='repository root: re-read every cited file and line, and check test paths exist')
    ap.add_argument('--strict', action='store_true', help='treat warnings (covered items whose file no evidence cites, '
                                                          'repeated open questions) as errors')
    args = ap.parse_args()
    try:
        d = json.loads(args.journey_map.read_text(encoding='utf-8'))
    except Exception as exc:
        print(f'INVALID: {exc}', file=sys.stderr); return 1
    if args.repo and not args.repo.is_dir():
        print(f'INVALID: --repo {args.repo} is not a directory', file=sys.stderr); return 1
    errs = check(d, Repo(args.repo) if args.repo else None)
    warns = warnings_for(d) if not errs or args.strict else []
    if args.strict:
        errs += warns
        warns = []
    for msg in warns:
        print(f'WARNING: {msg}', file=sys.stderr)
    if errs:
        for msg in errs: print(f'INVALID: {msg}', file=sys.stderr)
        print(f'{len(errs)} problem(s)', file=sys.stderr)
        return 1
    print(f'VALID ({len(warns)} warning(s))' if warns else 'VALID')
    return 0

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
