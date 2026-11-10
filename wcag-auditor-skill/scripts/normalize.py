#!/usr/bin/env python3
"""Normalise a run_audit.py run directory into one list of results mapped to WCAG success criteria.

    python normalize.py <run-dir> --out items.json

Every engine result and probe observation becomes an item with its tool, rule, ACT rule ids,
SCs, EARL outcome (`failed` or `cantTell`), kind (`wcag`, `best_practice`, `above_level`,
`outside_version`, `obsolete`, `experimental`), page, template, viewport, state, element and
screenshot. Tool "needs review" results stay `cantTell`; nothing is dropped silently. HTML_CodeSniffer
notices (manual-check prompts) and rule passes are counted per SC in `sc_evidence`, never turned into passes.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / 'data'
VERSION_ORDER = {'2.0': 0, '2.1': 1, '2.2': 2}
LEVEL_ORDER = {'A': 0, 'AA': 1, 'AAA': 2}


def load(name):
    return json.loads((DATA / name).read_text(encoding='utf-8'))


class Norm:
    def __init__(self, run: dict):
        self.run = run
        self.version = run['wcag_version']
        self.level = run['level']
        self.sc = {s['sc']: s for s in load('wcag-sc.json')['success_criteria']}
        self.map = load('rule-map.json')
        self.versions = {e['name']: e['version'] for e in run['engines']}
        self.templates = {s['id']: s.get('template') for s in run['sample']}
        self.items: list[dict] = []
        self.evidence: dict[str, dict] = {}
        self.unmapped: dict[str, int] = {}

    # classification ------------------------------------------------------
    def classify(self, scs: list[str], kind: str) -> tuple[list[str], str, list[str]]:
        """Split SCs into in-scope ones and say what kind of item this is."""
        if kind in ('best_practice', 'experimental'):
            return [], kind, scs
        inside, why = [], set()
        for s in scs:
            meta = self.sc.get(s)
            if not meta:
                why.add('unknown'); continue
            if meta.get('obsolete_in') and VERSION_ORDER[self.version] >= VERSION_ORDER[meta['obsolete_in']]:
                why.add('obsolete'); continue
            if VERSION_ORDER[meta['added_in']] > VERSION_ORDER[self.version]:
                why.add('outside_version'); continue
            if LEVEL_ORDER[meta['level']] > LEVEL_ORDER[self.level]:
                why.add('above_level'); continue
            inside.append(s)
        if inside:
            return inside, 'wcag', [s for s in scs if s not in inside]
        for k in ('above_level', 'outside_version', 'obsolete'):
            if k in why:
                return [], k, scs
        return [], 'best_practice', scs

    def ev(self, sc: str) -> dict:
        return self.evidence.setdefault(sc, {'tool_rule_passes': {}, 'htmlcs_notices': 0, 'probe_checks': 0, 'probe_failures': 0, 'probe_cant_tell': 0})

    def add(self, rec: dict, **kw):
        scs, kind, related = self.classify(kw.pop('sc'), kw.pop('kind', 'wcag'))
        el = kw.pop('el', None) or {}
        item = {
            'id': f'N-{len(self.items) + 1:04d}', 'tool': kw['tool'], 'tool_version': self.versions.get(kw['tool']),
            'rule_id': kw['rule_id'], 'act_rules': kw.get('act_rules', []), 'sc': scs, 'related_sc': related,
            'outcome': kw['outcome'], 'kind': kind, 'evidence_type': 'probe' if kw['tool'] == 'probe' else 'tool',
            'pattern': kw.get('pattern'), 'impact': kw.get('impact'), 'message': (kw.get('message') or '')[:400],
            'page': rec['page'], 'url': rec['url'], 'template': self.templates.get(rec['page']),
            'viewport': rec['viewport'], 'state': rec['state'], 'screenshot': kw.get('screenshot') or rec.get('screenshot'),
            'xpath': el.get('xpath'), 'selector': el.get('selector') or kw.get('selector'), 'sig': el.get('sig') or kw.get('sig') or 'document',
            'html': el.get('html') or kw.get('html'), 'data': kw.get('data'),
        }
        self.items.append(item)
        if kw['tool'] == 'probe':
            for s in scs:
                self.ev(s)['probe_failures' if item['outcome'] == 'failed' else 'probe_cant_tell'] += 1
        return item

    # engines -------------------------------------------------------------
    def axe(self, rec, r):
        for bucket, outcome in (('violations', 'failed'), ('incomplete', 'cantTell')):
            for v in r.get(bucket, []):
                m = self.map['axe'].get(v['id'])
                if not m:
                    self.unmapped[f"axe:{v['id']}"] = self.unmapped.get(f"axe:{v['id']}", 0) + 1
                    m = {'sc': [], 'act': [], 'kind': 'best_practice'}
                for n in v['nodes']:
                    self.add(rec, tool='axe-core', rule_id=v['id'], act_rules=m['act'], sc=m['sc'], kind=m['kind'],
                             outcome=outcome, impact=n.get('impact') or v.get('impact'), message=n.get('summary') or v['help'],
                             el=n.get('el'), selector=' >>> '.join(map(str, n['target'])), html=n.get('html'))
        for p in r.get('passes', []):
            m = self.map['axe'].get(p['id'])
            if m and m['kind'] == 'wcag':
                for s in m['sc']:
                    d = self.ev(s)['tool_rule_passes']
                    d[f"axe:{p['id']}"] = d.get(f"axe:{p['id']}", 0) + p['nodes']

    def ibm(self, rec, r):
        for x in r.get('results', []):
            m = self.map['ibm'].get(x['ruleId'])
            if not m:
                self.unmapped[f"ibm:{x['ruleId']}"] = self.unmapped.get(f"ibm:{x['ruleId']}", 0) + 1
                m = {'sc': [], 'act': [], 'kind': 'best_practice'}
            policy, result = x['value']
            kind = m['kind'] if policy == 'VIOLATION' else 'best_practice'
            outcome = 'failed' if result == 'FAIL' else 'cantTell'
            self.add(rec, tool='ibm-equal-access', rule_id=x['ruleId'], act_rules=m['act'], sc=m['sc'], kind=kind,
                     outcome=outcome, impact=f'{policy}/{result}', message=x.get('message'), el=x.get('el'),
                     html=x.get('snippet'), data={'reason': x.get('reasonId')})
        for rule, n in (r.get('passes') or {}).items():
            m = self.map['ibm'].get(rule)
            if m and m['kind'] == 'wcag':
                for s in m['sc']:
                    d = self.ev(s)['tool_rule_passes']
                    d[f'ibm:{rule}'] = d.get(f'ibm:{rule}', 0) + n

    def htmlcs(self, rec, r):
        pat = re.compile(self.map['htmlcs']['code_pattern'])
        for msg in r.get('messages', []):
            m = pat.search(msg['code'])
            sc = [f'{m.group(2)}.{m.group(3)}.{m.group(4)}'] if m else []
            rule = msg['code'].split('.', 1)[1] if '.' in msg['code'] else msg['code']
            self.add(rec, tool='htmlcs', rule_id=rule, sc=sc, kind='wcag' if sc else 'best_practice',
                     outcome='failed' if msg['type'] == 1 else 'cantTell', message=msg.get('msg'), el=msg.get('el'))
        for code, n in (r.get('notices') or {}).items():
            m = pat.search(code)
            if m:
                self.ev(f'{m.group(2)}.{m.group(3)}.{m.group(4)}')['htmlcs_notices'] += n

    def vnu(self, rec, r):
        for msg in r.get('messages', []):
            if msg.get('type') != 'error':
                continue
            text = msg.get('message') or ''
            if re.search(r'Duplicate ID|aria-|role', text, re.I):
                sc = ['4.1.1'] if 'Duplicate ID' in text else ['4.1.2']
                self.add(rec, tool='vnu', rule_id='vnu-error', sc=sc, outcome='cantTell', message=text, html=msg.get('extract'),
                         data={'line': msg.get('line'), 'note': 'supporting evidence only'})

    # probes --------------------------------------------------------------
    def probe(self, rec, code, outcome, el=None, message='', data=None, screenshot=None, html=None):
        m = self.map['probe'][code]
        return self.add(rec, tool='probe', rule_id=code, sc=m['sc'], kind='wcag', outcome=outcome, pattern=m['pattern'],
                        el=el, message=message, data=data, screenshot=screenshot, html=html)

    def checked(self, sc, n=1):
        for s in sc:
            if s in self.sc:
                self.ev(s)['probe_checks'] += n

    def probes(self, rec, p):
        k = p.get('keyboard')
        if k and k.get('status') == 'ran':
            self.checked(['2.1.1', '2.1.2', '2.4.3'], len(k['stops']))
            if k.get('trap'):
                t = k['trap']
                outcome = 'failed' if t['escaped_with'] is None else 'cantTell'
                msg = ('Focus cycles inside this element and cannot leave it with Tab, Shift+Tab or Escape'
                       if t['escaped_with'] is None else f"Tab cannot leave this element; {t['escaped_with']} does, without instructions")
                self.probe(rec, 'keyboard.trap', outcome, el=t.get('container'), message=msg,
                           data={'elements': t['elements'], 'after_presses': t['after_presses'],
                                 'unreached_after_trap': len(k['unreached'])}, screenshot=t.get('screenshot'))
            else:
                for u in k['unreached']:
                    self.probe(rec, 'keyboard.unreachable', 'cantTell', el=u, message='Focusable element was never reached by Tab')
            for po in k['pointer_only']:
                ls = po.get('listeners')
                if ls:
                    self.probe(rec, 'keyboard.unreachable', 'failed', el=po,
                               message=f"Has {', '.join(ls)} listener(s) and a pointer cursor but is not focusable or a native control",
                               data={'listeners': ls})
                else:
                    self.probe(rec, 'keyboard.unreachable', 'cantTell', el=po,
                               message='Looks clickable (pointer cursor or onclick) but is not focusable; no listener found on the element itself',
                               data={'listeners': ls})
            for s in k['positive_tabindex']:
                self.probe(rec, 'keyboard.order', 'cantTell', el=s, message=f"tabindex={s['tabindex']} changes the focus order")
            for j in k['jumps']:
                self.probe(rec, 'keyboard.order', 'cantTell', el={'xpath': j['to']}, message='Focus jumped back up the page', data=j)
            for s in k['stops']:
                ob = s.get('obscured') or {}
                full = ob.get('total') and ob['covered'] == ob['total']
                if full:
                    self.probe(rec, 'focus.obscured', 'failed', el=s, message='Focused element entirely hidden by fixed or sticky content',
                               data={'covered_points': ob['covered'], 'sampled_points': ob['total'], 'covered_by': ob.get('by')})
                elif ob.get('covered'):
                    self.probe(rec, 'focus.partly_obscured', 'failed', el=s, message='Focused element partly hidden by fixed or sticky content',
                               data={'covered_points': ob['covered'], 'sampled_points': ob['total'], 'covered_by': ob.get('by')})
                self.checked(['2.4.11'])
                ind = s.get('indicator') or {}
                if ind.get('changed_px') is None or full:
                    continue  # not measurable; an obscured element is reported under 2.4.11, not 2.4.7
                self.checked(['2.4.7'])
                n, per = ind['changed_px'], ind['perimeter'] or 1
                if n < 10:
                    self.probe(rec, 'focus.not_visible', 'failed', el=s, message=f'No visible change when focused ({n} pixels changed)', data=ind)
                elif n < per / 2:
                    self.probe(rec, 'focus.not_visible', 'cantTell', el=s, message=f'Weak focus indicator ({n} pixels changed, perimeter {per})', data=ind)
        r = p.get('reflow')
        if r and r.get('status') == 'ran':
            self.checked(['1.4.10'])
            if r['horizontal_scroll']:
                for c in r['culprits']:
                    if c['inside_scroller']:
                        continue
                    if c['exempt_kind']:
                        self.probe(rec, 'reflow.horizontal_scroll', 'cantTell', el=c,
                                   message=f"<{c['exempt_kind']}> causes horizontal scrolling at {r['viewport_width']} px; exempt only if it needs two-dimensional layout",
                                   data={'overflow_px': c['overflow_px'], 'scroll_width': r['scroll_width']})
                    else:
                        self.probe(rec, 'reflow.horizontal_scroll', 'failed', el=c,
                                   message=f"Overflows the {r['viewport_width']} px viewport by {c['overflow_px']} px and makes the page scroll horizontally",
                                   data={'overflow_px': c['overflow_px'], 'scroll_width': r['scroll_width']})
        t = p.get('text_spacing')
        if t and t.get('status') == 'ran':
            self.checked(['1.4.12'])
            for c in t['clipped']:
                self.probe(rec, 'text_spacing.clipped', 'failed', el=c,
                           message=f"Text clipped with WCAG text-spacing overrides ({c['overflow']} px hidden, {c['overflow_before']} before)",
                           data={'overflow': c['overflow'], 'overflow_before': c['overflow_before']})
        z = p.get('resize_text')
        if z and z.get('status') == 'ran':
            self.checked(['1.4.4'])
            for c in z['clipped']:
                self.probe(rec, 'resize_text.clipped', 'failed', el=c, message='Text clipped at 200% zoom',
                           data={'scroll': c['scroll'], 'client': c['client']}, screenshot=z.get('screenshot'))
            if z['zoom_disabled']:
                self.probe(rec, 'resize_text.zoom_disabled', 'failed', message=f"Viewport meta prevents zoom: {z['viewport_meta']}")
        ts = p.get('target_size')
        if ts and ts.get('status') == 'ran':
            self.checked(['2.5.8'], ts['targets'])
            for u in ts['undersized']:
                if u['spacing_ok'] or u['inline']:
                    continue
                self.probe(rec, 'target_size.too_small', 'failed', el=u,
                           message=f"{u['width']}x{u['height']} px target; a 24 px circle on it overlaps another target",
                           data={'width': u['width'], 'height': u['height'], 'conflicts_with': (u.get('conflicts_with') or {}).get('xpath')})
        h = p.get('hover_focus')
        if h and h.get('status') == 'ran':
            self.checked(['1.4.13'], h['checked'])
            for pop in h['popups']:
                d = {'trigger': pop['trigger']['xpath'], 'content': pop['content']['xpath']}
                if not pop['dismissible']:
                    self.probe(rec, 'hover_focus.not_dismissible', 'failed', el=pop['trigger'],
                               message='Content shown on hover stays visible after Escape', data=d)
                if not pop['hoverable']:
                    self.probe(rec, 'hover_focus.not_hoverable', 'failed', el=pop['trigger'],
                               message='Content shown on hover disappears when the pointer moves onto it', data=d)
        mo = p.get('motion')
        if mo and mo.get('status') == 'ran':
            self.checked(['2.2.2'])
            for c in mo['normal']['changing']:
                if c.get('pause_control'):
                    continue
                self.probe(rec, 'motion.autoplay', 'failed', el=c,
                           message='Content still changes automatically more than 5 s after load, with no pause, stop or hide control found')
            for a in mo['normal']['infinite_animations']:
                self.probe(rec, 'motion.autoplay', 'cantTell', el=a, message='Infinite or long CSS animation running')
            for l in mo['normal']['legacy']:
                self.probe(rec, 'motion.autoplay', 'failed', el=l, message='marquee or blink element')
            red = mo.get('reduced_motion') or {}
            for c in red.get('changing', []):
                self.add(rec, tool='probe', rule_id='motion.ignores_reduced_motion', sc=['2.3.3'], kind='best_practice',
                         outcome='cantTell', el=c, message='Still moving with prefers-reduced-motion: reduce',
                         pattern=self.map['probe']['motion.ignores_reduced_motion']['pattern'])
        md = p.get('media')
        if md and md.get('status') == 'ran':
            for it in md['items']:
                self.checked(['1.2.2', '1.4.2'])
                if it['kind'] == 'video' and not any(t['kind'] in ('captions', 'subtitles') for t in it['tracks']):
                    self.probe(rec, 'media.video_no_track', 'cantTell', el=it,
                               message='Video has no captions or subtitles track; check for open (burned-in) captions or a captioned alternative',
                               data={'tracks': it['tracks'], 'controls': it['controls']})
                if it['autoplay'] and not it['muted']:
                    self.probe(rec, 'media.autoplay_audio', 'failed' if not it['controls'] else 'cantTell', el=it,
                               message='Media autoplays with sound' + ('' if it['controls'] else ' and has no controls'))
            for e in md['embeds']:
                self.probe(rec, 'media.video_no_track', 'cantTell', el=e, message='Embedded player; captions must be checked manually')
        lg = p.get('language')
        if lg and lg.get('status') == 'ran':
            self.checked(['3.1.1'])
            if lg['lang'] is None or not lg['lang'].strip():
                self.probe(rec, 'language.missing', 'failed', message='html element has no lang attribute')
            elif not lg['valid']:
                self.probe(rec, 'language.invalid', 'failed', message=f"lang=\"{lg['lang']}\" is not a valid language tag")
            elif lg['detected'] and lg['detected'] != lg['lang'].split('-')[0].lower():
                self.probe(rec, 'language.mismatch', 'failed', html=f"<html lang=\"{lg['lang']}\">",
                           message=f"lang=\"{lg['lang']}\" but the page text reads as '{lg['detected']}' ({lg['words']} words)",
                           data={'counts': lg.get('counts')})

    def run_dir(self, d: Path):
        for f in sorted((d / 'raw').glob('*.json')):
            rec = json.loads(f.read_text(encoding='utf-8'))
            e = rec.get('engines', {})
            if e.get('axe-core', {}).get('status') == 'ran':
                self.axe(rec, e['axe-core'])
            if e.get('ibm-equal-access', {}).get('status') == 'ran':
                self.ibm(rec, e['ibm-equal-access'])
            if e.get('htmlcs', {}).get('status') == 'ran':
                self.htmlcs(rec, e['htmlcs'])
            if e.get('vnu', {}).get('status') == 'ran':
                self.vnu(rec, e['vnu'])
            self.probes(rec, rec.get('probes', {}))


def main() -> int:
    sys.stdout.reconfigure(errors='replace')
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('run_dir')
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    d = Path(args.run_dir)
    run = json.loads((d / 'run.json').read_text(encoding='utf-8'))
    n = Norm(run)
    n.run_dir(d)
    out = {'tool': 'normalize.py', 'wcag_version': n.version, 'level': n.level,
           'engines': run['engines'], 'items': n.items, 'sc_evidence': n.evidence, 'unmapped_rules': n.unmapped}
    Path(args.out).write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding='utf-8')
    kinds = {}
    for i in n.items:
        kinds[(i['kind'], i['outcome'])] = kinds.get((i['kind'], i['outcome']), 0) + 1
    print(json.dumps({'items': len(n.items), 'by_kind': {f'{k}/{o}': v for (k, o), v in sorted(kinds.items())},
                      'unmapped': n.unmapped}, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
