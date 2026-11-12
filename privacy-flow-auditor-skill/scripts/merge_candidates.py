#!/usr/bin/env python3
"""Turn the live capture and the static scan into candidates, recipients and the consent matrix.

    python merge_candidates.py --capture run/capture.json --static run/static.json --flows flows.json \
        --lists domains.json [--declared declared_processors.json] [--regions AU,EU] --out run/candidates.json

Live candidates (L###):
  tracking_before_consent  each third-party domain contacted, and each first-party cookie not attributed to the
                           site, in the "before" state (and the "initial" phase of the other states)
  tracking_after_reject    each third-party domain contacted after "Reject all"
  personal_data_to_third_party / session_replay_unmasked   a canary in a request to a third party, per domain
  personal_data_in_url     a canary in a first-party URL, per path
  insecure_storage         a canary in localStorage, sessionStorage or a cookie
  personal_data_in_logs / sensitive_category_flow   a canary in the server log (local runs)
  undeclared_processor     a third party receiving a canary that isn't in the declared processors
  cross_border_transfer    a third party receiving a canary whose region (from the list) is outside --regions
  possible hashed identifier   a 32/40/64-hex value in a third-party request that matches no canary
Static candidates (S###) are carried over unchanged.

Vendors are named only from the domain list (or a code import in the static scan), never from the domain name.
A declared processor covers a domain when its name is the domain's vendor, one of the vendor's aliases or its
parent company (data/vendors.json "vendors", plus a "vendors" section in the list), or when the declared entry
lists the domain under "domains"; each recipient records the declared name and how it matched
(exact, alias, company or domain), so a loose match can be questioned.

Requests the browser harness blocked (third_party: block, the default) were attempted by the page but never
reached the vendor; candidates and recipients count them (blocked_requests), and the output lists the blocked
domains, because a blocked vendor couldn't set cookies or send the hits that would have followed.
"""
from __future__ import annotations
import argparse, ipaddress, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CATS = json.loads((HERE.parent / 'data' / 'categories.json').read_text(encoding='utf-8'))
VENDORS = json.loads((HERE.parent / 'data' / 'vendors.json').read_text(encoding='utf-8')).get('vendors', {})
TWO_LEVEL = ('com.au', 'net.au', 'org.au', 'gov.au', 'edu.au', 'co.uk', 'org.uk', 'gov.uk', 'ac.uk', 'co.nz', 'co.jp', 'com.br')


def registrable(host: str | None) -> str:
    """The registrable domain of a host; IP addresses, localhost and single-label hosts are returned whole."""
    if not host:
        return ''
    host = host.lower().strip('.').strip('[]')
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if host == 'localhost' or host.endswith('.localhost') or '.' not in host:
        return host
    parts = host.split('.')
    n = 3 if '.'.join(parts[-2:]) in TWO_LEVEL else 2
    return '.'.join(parts[-n:])


def norm(name: str | None) -> str:
    """A vendor name for comparison: lower case, letters and digits only, a trailing company suffix dropped."""
    n = re.sub(r'[^a-z0-9]+', ' ', (name or '').lower()).strip()
    n = re.sub(r'\s+(inc|llc|ltd|limited|pty ltd|pty|corp|corporation|gmbh|plc|ireland|ireland limited)$', '', n)
    return n.replace(' ', '')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--capture', type=Path, required=True)
    ap.add_argument('--static', type=Path)
    ap.add_argument('--flows', type=Path, required=True)
    ap.add_argument('--lists', type=Path, required=True, help='domain and cookie list (fetch_lists.py output, or an example list)')
    ap.add_argument('--declared', type=Path)
    ap.add_argument('--regions', default='AU,EU')
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    cap = json.loads(a.capture.read_text(encoding='utf-8'))
    flows = json.loads(a.flows.read_text(encoding='utf-8'))
    lists = json.loads(a.lists.read_text(encoding='utf-8'))
    declared = json.loads(a.declared.read_text(encoding='utf-8')) if a.declared else {'processors': []}
    declared_by_name = {norm(p['vendor']): p['vendor'] for p in declared['processors']}
    declared_by_domain = {registrable(d): p['vendor'] for p in declared['processors'] for d in p.get('domains', [])}
    known = {**VENDORS, **lists.get('vendors', {})}
    regions = set(a.regions.split(','))
    first = {registrable(d) for d in flows.get('first_party_domains', [])} or {registrable(cap['site'].split('/')[2].split(':')[0])}
    domains, cookie_list = lists.get('domains', {}), lists.get('cookies', {})

    def vendor(dom: str) -> dict:
        return domains.get(dom) or {}

    def declared_as(dom: str) -> dict | None:
        """The declared processor that covers this domain, and how: exact, alias, company or domain."""
        if dom in declared_by_domain:
            return {'name': declared_by_domain[dom], 'match': 'domain'}
        v = vendor(dom).get('vendor')
        if not v:
            return None
        info = known.get(v) or {}
        for how, names in (('exact', [v]), ('alias', info.get('aliases', [])), ('company', [info.get('company')])):
            for n in names:
                if n and norm(n) in declared_by_name:
                    return {'name': declared_by_name[norm(n)], 'match': how}
        return None

    def kind(dom: str) -> str:
        if dom in first:
            return 'first_party'
        return 'declared_processor' if declared_as(dom) else 'third_party'

    def blocked(rs) -> int:
        return sum(1 for r in rs if r.get('blocked'))

    cands: list[dict] = []

    def cand(**kw):
        kw = {'id': f'L{len(cands) + 1:03d}', 'source': 'live', **kw}
        cands.append(kw)
        return kw

    states = {s['state']: s for s in cap['states']}
    tested = [s for s in states if states[s]['status'] == 'tested']
    # ---- tracking before consent and after reject
    for st_name, typ, phases in (('before', 'tracking_before_consent', None), ('reject', 'tracking_after_reject', 'after')):
        st = states.get(st_name)
        if not st or st['status'] != 'tested':
            continue
        reqs = [r for r in st['requests'] if phases is None or r['phase'] != 'initial']
        by_dom: dict = {}
        for r in reqs:
            d = registrable(r['host'])
            if d not in first:
                by_dom.setdefault(d, []).append(r)
        last = st['snapshots'][-1]
        initial = st['snapshots'][0]
        snap_cookies = (last if phases else initial)['cookies']
        for d, rs in sorted(by_dom.items()):
            v = vendor(d)
            ck = sorted({c['name'] for c in snap_cookies if (cookie_list.get(c['name']) or {}).get('domain') == d or registrable(c['domain'].lstrip('.')) == d})
            c = cand(type=typ, state=st_name, domain=d, vendor=v.get('vendor'), vendor_category=v.get('category'), recipient=kind(d),
                     requests=len(rs), paths=sorted({r['path'] for r in rs})[:10], phases=sorted({r['phase'] for r in rs}),
                     cookies=ck, example_requests=[{'state': st_name, 'n': r['n'], 'phase': r['phase'], 'method': r['method'], 'url': r['url'][:300]} for r in rs[:3]])
            if blocked(rs):
                c['blocked_requests'] = blocked(rs)
        if st_name == 'before':
            for c in initial['cookies']:
                if registrable(c['domain'].lstrip('.')) in first and not cookie_list.get(c['name']):
                    cand(type=typ, state=st_name, domain=registrable(c['domain'].lstrip('.')), vendor=None, recipient='first_party', cookie=c['name'],
                         cookie_flags={k: c[k] for k in ('http_only', 'secure', 'same_site', 'persistent', 'expires_days', 'value_length')},
                         requests=0, paths=[], phases=['initial'], cookies=[c['name']], example_requests=[])
    # ---- canaries in requests
    to_third: dict = {}
    in_url: dict = {}
    hexes: dict = {}
    for st_name in tested:
        for r in states[st_name]['requests']:
            d = registrable(r['host'])
            if r['matches'] and d not in first:
                e = to_third.setdefault(d, {'states': set(), 'fields': set(), 'encodings': set(), 'requests': [], 'blocked': 0})
                e['blocked'] += 1 if r.get('blocked') else 0
                e['states'].add(st_name)
                e['fields'] |= {m['field'] for m in r['matches']}
                e['encodings'] |= {f'{m["field"]}:{m["encoding"]}' for m in r['matches']}
                e['requests'].append({'state': st_name, 'n': r['n'], 'phase': r['phase'], 'method': r['method'], 'url': r['url'][:300],
                                      'where': sorted({m['where'] for m in r['matches']}), 'body': (r['body'] or '')[:300] or None})
            elif d in first and r['method'] == 'GET' and any(m['where'] == 'url' for m in r['matches']):
                e = in_url.setdefault(r['path'], {'states': set(), 'fields': set(), 'requests': []})
                e['states'].add(st_name)
                e['fields'] |= {m['field'] for m in r['matches'] if m['where'] == 'url'}
                e['requests'].append({'state': st_name, 'n': r['n'], 'phase': r['phase'], 'url': r['url'][:300]})
            if d not in first:
                for h in r.get('unmatched_hex', []):
                    e = hexes.setdefault((d, h), {'states': set(), 'requests': []})
                    e['states'].add(st_name)
                    e['requests'].append({'state': st_name, 'n': r['n'], 'phase': r['phase'], 'url': r['url'][:300]})
    fields_cat = {}
    for st in cap['states']:
        for r in st['requests']:
            for m in r['matches']:
                fields_cat[m['field']] = m['category']

    def cats(fields):
        return sorted({fields_cat[f] for f in fields})

    for d, e in sorted(to_third.items()):
        v = vendor(d)
        typ = 'session_replay_unmasked' if v.get('category') == 'session_replay' else 'personal_data_to_third_party'
        c = cand(type=typ, domain=d, vendor=v.get('vendor'), vendor_category=v.get('category'), recipient=kind(d), states=sorted(e['states']),
                 fields=sorted(e['fields']), categories=cats(e['fields']), encodings=sorted(e['encodings']), example_requests=e['requests'][:4],
                 requests=len(e['requests']))
        if e['blocked']:
            c['blocked_requests'] = e['blocked']
        if kind(d) == 'third_party':
            cand(type='undeclared_processor', domain=d, vendor=v.get('vendor'), vendor_category=v.get('category'), recipient='third_party',
                 states=sorted(e['states']), fields=sorted(e['fields']), categories=cats(e['fields']),
                 declared_source=declared.get('source'), example_requests=e['requests'][:2])
        if v.get('region') and v['region'] not in regions:
            cand(type='cross_border_transfer', domain=d, vendor=v.get('vendor'), recipient=kind(d), region=v['region'], regions=sorted(regions),
                 states=sorted(e['states']), fields=sorted(e['fields']), categories=cats(e['fields']), region_source=lists.get('source'))
    for path, e in sorted(in_url.items()):
        cand(type='personal_data_in_url', domain=sorted(first)[0], path=path, states=sorted(e['states']), fields=sorted(e['fields']),
             categories=cats(e['fields']), example_requests=e['requests'][:3], requests=len(e['requests']))
    for (d, h), e in sorted(hexes.items()):
        cand(type='personal_data_to_third_party', subtype='possible_hashed_identifier', domain=d, vendor=vendor(d).get('vendor'), recipient=kind(d),
             value=h, length=len(h), states=sorted(e['states']), fields=[], categories=[], requests=len(e['requests']),
             example_requests=e['requests'][:2], facts={'same_value_in_every_request': True, 'states_seen': sorted(e['states'])})
    # ---- storage and cookies
    stor: dict = {}
    for st_name in tested:
        for snap in states[st_name]['snapshots']:
            for s in snap['storage']:
                if s.get('matches'):
                    e = stor.setdefault((s['kind'], s['origin'], s['key']), {'states': set(), 'fields': set(), 'phase': snap['phase']})
                    e['states'].add(st_name)
                    e['fields'] |= {m['field'] for m in s['matches']}
            for c in snap['cookies']:
                if c.get('matches'):
                    e = stor.setdefault(('cookie', c['domain'], c['name']), {'states': set(), 'fields': set(), 'phase': snap['phase'],
                                                                            'flags': {k: c[k] for k in ('http_only', 'secure', 'same_site')}})
                    e['states'].add(st_name)
                    e['fields'] |= {m['field'] for m in c['matches']}
    for (k, origin, key), e in sorted(stor.items()):
        cand(type='insecure_storage', storage=k, origin=origin, key=key, states=sorted(e['states']), fields=sorted(e['fields']),
             categories=cats(e['fields']), first_seen=e['phase'], flags=e.get('flags'))
    # ---- server log
    log = cap.get('server_log')
    if log and log['hits']:
        fields = sorted({m['field'] for h in log['hits'] for m in h['matches']})
        cs = cats(fields)
        typ = 'sensitive_category_flow' if any(c in CATS['special'] for c in cs) else 'personal_data_in_logs'
        cand(type=typ, sink='server_log', log_file=log['file'], lines=[h['line'] for h in log['hits']], fields=fields, categories=cs,
             example_line=log['hits'][0]['text'])
    static = json.loads(a.static.read_text(encoding='utf-8'))['candidates'] if a.static else []
    # ---- recipients and consent matrix
    recipients = {}
    for st_name in tested:
        for r in states[st_name]['requests']:
            d = registrable(r['host'])
            e = recipients.setdefault(d, {'domain': d, 'hosts': set(), 'vendor': vendor(d).get('vendor'), 'vendor_category': vendor(d).get('category'),
                                          'region': vendor(d).get('region'), 'kind': kind(d), 'declared_as': declared_as(d) if d not in first else None,
                                          'states': set(), 'categories': set(), 'fields': set(), 'requests': 0, 'blocked_requests': 0})
            e['hosts'].add(r['host'])
            e['requests'] += 1
            e['blocked_requests'] += 1 if r.get('blocked') else 0
            e['states'].add(st_name)
            e['fields'] |= {m['field'] for m in r['matches']}
    for e in recipients.values():
        e['categories'] = cats(e['fields'])
        for k in ('hosts', 'states', 'fields'):
            e[k] = sorted(e[k])
        e['named_by'] = 'domain list' if e['vendor'] else None
    matrix = []
    for st_name in ('before', 'reject', 'accept'):
        st = states.get(st_name)
        if not st:
            matrix.append({'state': st_name, 'status': 'not_run'})
            continue
        if st['status'] != 'tested':
            matrix.append({'state': st_name, 'status': 'untested', 'reason': st.get('reason')})
            continue
        last = st['snapshots'][-1]
        third = sorted({registrable(r['host']) for r in st['requests'] if registrable(r['host']) not in first and (st_name == 'before' or r['phase'] != 'initial')})
        canaries = sorted({m['field'] for r in st['requests'] if registrable(r['host']) not in first for m in r['matches']})
        matrix.append({'state': st_name, 'status': 'tested', 'third_party_domains': third,
                       'cookies': sorted(c['name'] for c in last['cookies']), 'canaries_to_third_parties': canaries})
    blocked_domains = sorted({registrable(r['host']) for st_name in tested for r in states[st_name]['requests'] if r.get('blocked')})
    out = {'capture': a.capture.as_posix(), 'static': a.static.as_posix() if a.static else None,
           'inputs': {'flows': a.flows.as_posix(), 'lists': a.lists.as_posix(), 'declared': a.declared.as_posix() if a.declared else None,
                      'regions': a.regions}, 'lists': lists.get('source'),
           'declared': declared.get('source'), 'regions': sorted(regions), 'first_party': sorted(first),
           'third_party_mode': cap.get('third_party'), 'blocked_domains': blocked_domains,  # mode null: captured before it was recorded
           'recipients': sorted(recipients.values(), key=lambda e: e['domain']), 'consent_matrix': matrix, 'candidates': cands + static}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(cands)} live and {len(static)} static candidate(s) -> {a.out}')
    for c in out['candidates']:
        print(f'  {c["id"]} {c["type"]:28} {c.get("state") or ",".join(c.get("states", [])):20} {c.get("domain") or c.get("file", "")} {c.get("fields") or c.get("cookie") or ""}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
