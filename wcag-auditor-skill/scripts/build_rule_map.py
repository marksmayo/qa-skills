#!/usr/bin/env python3
"""Regenerate data/rule-map.json from the pinned engines' own rule metadata (maintainers only).

    python build_rule_map.py [--cache <dir>] [--ibm-tarball <accessibility-checker-engine-x.y.z.tgz>] [--check]

Loads axe-core and the IBM engine into a blank Chromium page (Playwright) and reads axe's
getRules() (wcagNNN tags, ACT ids) and IBM's getGuidelines() (WCAG ruleset checkpoints). IBM's
ACT ids come from help/rules.csv in its npm tarball, which is downloaded and integrity-checked
unless --ibm-tarball is given. The htmlcs and probe sections are kept from the current file.
--check exits 1 if the result differs from data/rule-map.json instead of writing it.
"""
from __future__ import annotations
import argparse, csv, io, json, re, sys, tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fetch_engines  # noqa: E402

DATA = HERE.parent / 'data'
TAG = re.compile(r'^wcag(\d)(\d)(\d+)$')


def sc_key(s):
    return [int(x) for x in s.split('.')]


def engine_meta(cache):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.set_content('<html><body></body></html>')
        pg.add_script_tag(path=str(fetch_engines.engine_path('axe-core', cache)))
        pg.add_script_tag(path=str(fetch_engines.engine_path('ibm-equal-access', cache)))
        axe = pg.evaluate('axe.getRules()')
        ibm = pg.evaluate('''(() => { const c = new ace.Checker(); return c.getGuidelines().map(g => ({id: g.id,
            checkpoints: g.checkpoints.map(cp => ({num: cp.num, rules: (cp.rules || []).map(r => ({id: r.id}))}))})); })()''')
        b.close()
    return axe, ibm


def ibm_act(tarball: Path | None) -> dict[str, set]:
    lock = {e['name']: e for e in fetch_engines.load_lock()['engines']}['ibm-equal-access']
    raw = tarball.read_bytes() if tarball else fetch_engines._get(lock['tarball'])
    if not fetch_engines.integrity_ok(raw, lock['tarball_integrity']):
        raise SystemExit('IBM tarball failed its integrity check')
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as tf:
        text = tf.extractfile(tf.getmember('package/help/rules.csv')).read().decode('utf-8')
    act: dict[str, set] = {}
    for row in list(csv.reader(text.splitlines()))[1:]:
        if len(row) >= 8:
            for a in re.findall(r'([0-9a-z]{6}):', row[7]):
                act.setdefault(row[0].strip(), set()).add(a)
    return act


def build(axe_rules, guidelines, act, current, known):
    axe = {}
    for r in axe_rules:
        tags = r['tags']
        sc = sorted({'.'.join(m.groups()) for t in tags if (m := TAG.match(t))}, key=sc_key)
        kind = 'experimental' if 'experimental' in tags else 'wcag' if sc else 'best_practice'
        axe[r['ruleId']] = {'sc': sc, 'act': r['actIds'] or [], 'kind': kind, 'obsolete': 'wcag2a-obsolete' in tags, 'help': r['help']}
    ibm_sc: dict[str, set] = {}
    for g in guidelines:
        if g['id'] not in ('WCAG_2_0', 'WCAG_2_1', 'WCAG_2_2', 'IBM_Accessibility'):
            continue
        for cp in g['checkpoints']:
            if re.match(r'^\d\.\d\.\d+$', cp['num']):
                for r in cp['rules']:
                    ibm_sc.setdefault(r['id'], set()).add(cp['num'])
    ibm = {}
    for rid in sorted(set(ibm_sc) | set(act)):
        sc = sorted((s for s in ibm_sc.get(rid, ()) if s in known), key=sc_key)
        ibm[rid] = {'sc': sc, 'act': sorted(act.get(rid, ())), 'kind': 'wcag' if sc else 'best_practice'}
    versions = {e['package'] if 'package' in e else e['name']: e['version'] for e in fetch_engines.load_lock()['engines'] if e['kind'] == 'browser_script'}
    return {'generated_from': {'axe-core': versions['axe-core'], 'accessibility-checker-engine': versions['accessibility-checker-engine'],
                               'html_codesniffer': versions['html_codesniffer']},
            'notes': current['notes'], 'axe': axe, 'ibm': ibm, 'htmlcs': current['htmlcs'], 'probe': current['probe']}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--cache')
    ap.add_argument('--ibm-tarball', type=Path)
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()
    current = json.loads((DATA / 'rule-map.json').read_text(encoding='utf-8'))
    known = {s['sc'] for s in json.loads((DATA / 'wcag-sc.json').read_text(encoding='utf-8'))['success_criteria']}
    axe_rules, guidelines = engine_meta(Path(args.cache) if args.cache else None)
    out = build(axe_rules, guidelines, ibm_act(args.ibm_tarball), current, known)
    unknown = sorted({s for r in out['axe'].values() for s in r['sc'] if s not in known})
    if unknown:
        print(f'axe maps SCs missing from data/wcag-sc.json: {unknown}')
        return 1
    text = json.dumps(out, indent=1, ensure_ascii=False) + '\n'
    if args.check:
        same = text == (DATA / 'rule-map.json').read_text(encoding='utf-8')
        print('rule-map.json is up to date' if same else 'rule-map.json differs from the engines\' metadata')
        return 0 if same else 1
    (DATA / 'rule-map.json').write_text(text, encoding='utf-8')
    print(f"wrote rule-map.json: {len(out['axe'])} axe rules, {len(out['ibm'])} IBM rules")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
