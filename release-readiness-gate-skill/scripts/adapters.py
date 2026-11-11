#!/usr/bin/env python3
"""Detect a report's type and extract its gate-relevant facts into one shape (stdlib only).

    python adapters.py report.json            # print the detected type and the extracted facts

Driven by data/report_types.json: each skill's report is detected by its `skill` field or, for older
reports, by its top-level keys; then its verdict, evidence completeness, overall confidence, findings
(id, title, severity, priority, confidence, evidence type, category, location) and what it reports as
untested or not run are read from the paths given there. Ratings are copied exactly as the source
rated them: the gate never re-scores. JUnit XML is read with the shared parse_results.py.
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TYPES = json.loads((HERE.parent / 'data' / 'report_types.json').read_text(encoding='utf-8'))
SEV_RANK = {'critical': 4, 'high': 3, 'serious': 3, 'medium': 2, 'moderate': 2, 'low': 1, 'minor': 1}
CONF_RANK = {'high': 3, 'medium': 2, 'low': 1}


def get(obj, path: str | None):
    """Dotted path with [n] list indexes; None when any step is missing."""
    if not path:
        return None
    cur = obj
    for part in re.findall(r'[^.\[\]]+|\[\d+\]', path):
        if part.startswith('['):
            i = int(part[1:-1])
            cur = cur[i] if isinstance(cur, list) and len(cur) > i else None
        else:
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is None:
            return None
    return cur


def detect(path: Path) -> tuple[str | None, dict | None]:
    """(type name, parsed report) or (None, None) when the file isn't a report the gate knows."""
    if path.suffix.lower() == '.xml':
        head = path.read_text(encoding='utf-8', errors='replace')[:2000]
        return ('ci-junit', None) if re.search(r'<testsuites?\b', head) else (None, None)
    try:
        d = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeDecodeError):
        return None, None
    if not isinstance(d, dict):
        return None, None
    sk = d.get('skill')
    sk = sk.get('name') if isinstance(sk, dict) else sk
    for name, t in TYPES['types'].items():
        if t['detect'].get('skill') and t['detect']['skill'] == sk:
            return name, d
    for name, t in TYPES['types'].items():
        keys = t['detect'].get('keys')
        if keys and all(k in d for k in keys) and not sk:
            return name, d
    return None, d


def band(p) -> str | None:
    if isinstance(p, dict):
        p = p.get('band') or p.get('priority')
    return p if isinstance(p, str) and re.fullmatch(r'P[0-3]', p) else None


def location(v, line=None) -> dict | None:
    if isinstance(v, list):
        v = v[0] if v else None
    if v is None:
        return None
    if isinstance(v, str):
        return {'file': v, 'line': line} if line is not None else {'ref': v}
    if isinstance(v, dict):
        out = {k: v[k] for k in ('file', 'line', 'url', 'selector', 'screen', 'page') if v.get(k) is not None}
        if 'element' in v and isinstance(v['element'], dict) and v['element'].get('resource_id'):
            out['element'] = v['element']['resource_id']
        return out or None
    return {'ref': str(v)}


def extract(name: str, d: dict) -> dict:
    t = TYPES['types'][name]
    first = lambda paths: next((get(d, p) for p in paths if get(d, p) is not None), None)  # noqa: E731
    facts = {'type': name, 'skill': t.get('skill_dir', name.split(':')[0]), 'skill_version': first(t.get('version', [])),
             'generated_at': first(t.get('generated_at', [])), 'verdict': get(d, t.get('verdict')),
             'evidence_completeness': get(d, t.get('completeness')), 'overall_confidence': get(d, t.get('confidence')),
             'blocked_critical_paths': get(d, t.get('blocked_paths')) or [], 'subject': d.get('subject'),
             'findings': [], 'untested': [], 'not_run_tools': []}
    c = facts['evidence_completeness']
    if isinstance(c, (int, float)) and not isinstance(c, bool) and c > 1:
        facts['evidence_completeness'] = round(c / 100, 4)  # reported as a percentage
    for spec in t.get('findings', []):
        for i, f in enumerate(get(d, spec['path']) or []):
            if not isinstance(f, dict):
                continue
            sev = get(f, spec.get('severity'))
            facts['findings'].append({
                'skill': facts['skill'], 'finding_id': get(f, spec.get('id')) if spec.get('id') else f'{spec["path"]}[{i}]',
                'title': str(get(f, spec.get('title')) or '')[:200], 'severity': sev.lower() if isinstance(sev, str) else None,
                'tier': get(f, spec.get('tier')), 'priority': band(get(f, spec.get('priority'))),
                'confidence': get(f, spec.get('confidence')), 'evidence_type': get(f, spec.get('evidence_type')),
                'category': get(f, spec.get('category')), 'class': spec.get('class'),
                'waiver_category': spec.get('waiver_category', t['waiver_category']),
                'location': location(get(f, spec.get('location')), get(f, spec.get('line'))),
                'facts': {k: get(f, p) for k, p in spec.get('facts', {}).items()}})
    for u in t.get('untested', []):
        items = get(d, u['path']) or []
        if isinstance(items, dict):
            items = list(items)
        for it in items if isinstance(items, list) else [items]:
            if u.get('where') and not all(get(it, k) in v for k, v in u['where'].items()):
                continue
            facts['untested'].append({'label': u['label'], 'item': str(get(it, u['show']) if u.get('show') and isinstance(it, dict) else it)[:200]})
    for k, x in t.get('extras', {}).items():
        facts[k] = [{f: get(it, fp) for f, fp in x['fields'].items()} for it in (get(d, x['path']) or []) if isinstance(it, dict)]
    tools = d.get('tools') or get(d, 'metadata.tools') or []
    for x in tools if isinstance(tools, list) else []:
        if isinstance(x, dict) and x.get('status') in ('not_run', 'errored'):
            facts['not_run_tools'].append(x.get('name'))
    return facts


def extract_junit(path: Path) -> dict:
    from parse_results import parse_junit
    cases, notes = parse_junit(path, 'CI')
    return {'type': 'ci-junit', 'skill': 'ci-junit', 'skill_version': None, 'generated_at': None, 'verdict': None,
            'evidence_completeness': None, 'overall_confidence': None, 'blocked_critical_paths': [], 'subject': None,
            'findings': [], 'untested': [{'label': 'skipped cases', 'item': c['id']} for c in cases if c['status'] == 'skipped'],
            'not_run_tools': [], 'cases': [{k: c.get(k) for k in ('id', 'classname', 'name', 'status', 'failure_type')} for c in cases],
            'parse_notes': notes}


def facts_for(path: Path) -> dict | None:
    name, d = detect(path)
    if name == 'ci-junit':
        return extract_junit(path)
    return extract(name, d) if name else None


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    f = facts_for(Path(sys.argv[1]))
    print(json.dumps(f, indent=1, ensure_ascii=False) if f else 'unrecognised')
