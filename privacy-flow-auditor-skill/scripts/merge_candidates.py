#!/usr/bin/env python3
"""Turn the live capture and the static scan into candidates, recipients and the consent matrix.

    python merge_candidates.py --capture run/capture.json --static run/static.json --flows flows.json \
        --lists domains.json [--declared declared_processors.json] [--regions AU,EU] --out run/candidates.json
    python merge_candidates.py --static run/static.json [--lists domains.json] [--regions AU,EU] \
        --run-status-reason "why the capture did not run" --out run/candidates.json     # static only

Live candidates (L###):
  tracking_before_consent  each third-party domain contacted, and each first-party cookie not attributed to the
                           site, in the "before" state (and the "initial" phase of the other states); also each
                           domain contacted after the banner was closed without a choice ("dismiss")
  tracking_after_reject    each third-party domain contacted after "Reject all" (and, for "partial", after saving
                           preferences that refuse some purposes: only vendors in consent.partial_refuses when given)
                           Both also cover first-party tracking paths (an analytics proxy or error-tracker tunnel on
                           the site's own host: data/vendors.json path_rules and the list's EasyPrivacy path rules;
                           subtype first_party_proxy) and vendor requests the site's Content Security Policy stopped
                           in the browser (csp_blocked_attempts, from CSP reports and console violations)
  first_party_beacon       first-party writes (POST and the like) fired without a form submission: in the initial
                           or after-choice phase, or blocked by the harness's no-write rule (telemetry, consent
                           records, autosave), per path
  personal_data_to_third_party / session_replay_unmasked   a canary in a request to a third party, per domain
  personal_data_in_url     a canary in a first-party URL, per path
  insecure_storage         a canary in localStorage, sessionStorage or a cookie
  personal_data_in_logs / sensitive_category_flow   a canary in the server log (local runs)
  undeclared_processor     a third party receiving a canary that isn't in the declared processors
  cross_border_transfer    a third party receiving a canary whose region (from the list) is outside --regions
  possible hashed identifier   32/40/64-hex values in third-party requests that match no canary, one candidate per
                           (domain, key): facts say how many distinct values there were, whether a value differs on
                           every request (a per-load random id: Sentry sid, event_id, trace_id, a UUID) or recurs
                           across consent states, which are fresh browser contexts (a stable identifier)
Static candidates (S###) are carried over unchanged.

An app with no consent banner ("consent": {"banner": null} in flows.json) has one state, no_banner, treated like
"before" (tracking_before_consent candidates); before, reject and accept are not_applicable, and the output says
consent_banner: false.

Without --capture (the live half could not run) the output is static only: run_status not_run with the reason,
every consent state not_run, no recipients, and the static candidates. A capture with a consent state untested,
missing or with step errors gives run_status partial.

Domains: registrable domains know shared-hosting suffixes (data/public-suffix-private.json: vercel.app,
netlify.app, github.io, ...), and first_party_domains in flows.json are honoured as given (an entry covers itself
and its subdomains; a full host is never widened to its suffix).

Vendors are named only from the domain list (or a code import in the static scan), never from the domain name.
A declared processor covers a domain when its name is the domain's vendor, one of the vendor's aliases or its
parent company (data/vendors.json "vendors", plus a "vendors" section in the list), or when the declared entry
lists the domain under "domains"; each recipient records the declared name and how it matched
(exact, alias, company or domain), so a loose match can be questioned.

Requests the browser harness blocked to third parties (third_party: block, the default) were attempted by the
page but never reached the vendor; candidates and recipients count them (blocked_requests), and the output lists
the blocked third-party domains, because a blocked vendor couldn't set cookies or send the hits that would have
followed. Blocked first-party writes are listed separately (blocked_first_party_writes): the site is never a
blocked vendor.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from domains import FirstParty, registrable  # noqa: E402,F401  (registrable is imported by other scripts from here)

CATS = json.loads((HERE.parent / 'data' / 'categories.json').read_text(encoding='utf-8'))
VENDOR_DATA = json.loads((HERE.parent / 'data' / 'vendors.json').read_text(encoding='utf-8'))
VENDORS = VENDOR_DATA.get('vendors', {})
PATH_RULES = VENDOR_DATA.get('path_rules', {})
CONSENT_STATES = ('before', 'reject', 'accept')
NO_BANNER_REASON = 'the app has no consent banner (consent.banner is null in flows.json): there is no choice to make'


def no_banner_cfg(flows: dict | None) -> bool:
    """flows.json says the app has no consent banner at all: "consent": {"banner": null}."""
    c = (flows or {}).get('consent') or {}
    return 'banner' in c and c['banner'] is None
EXTRA_STATES = ('partial', 'dismiss')
SAFE = ('GET', 'HEAD', 'OPTIONS')
SDK_ID_KEYS = {'sid', 'event_id', 'trace_id', 'span_id', 'parent_span_id', 'replay_id', 'segment_id', 'session_id', 'sessionid',
               'trace', 'traceid', 'request_id', 'requestid', 'nonce', 'cid', 'uuid', 'id', 'sentry_key'}
CSP_REPORT = re.compile(r'"(?:blocked-uri|blockedURL|blockedURI)"\s*:\s*"(?P<url>https?://[^"]+)"')
CSP_DIRECTIVE = re.compile(r'"(?:effective-directive|effectiveDirective|violated-directive|violatedDirective)"\s*:\s*"(?P<d>[\w-]+)')


def norm(name: str | None) -> str:
    """A vendor name for comparison: lower case, letters and digits only, a trailing company suffix dropped."""
    n = re.sub(r'[^a-z0-9]+', ' ', (name or '').lower()).strip()
    n = re.sub(r'\s+(inc|llc|ltd|limited|pty ltd|pty|corp|corporation|gmbh|plc|ireland|ireland limited)$', '', n)
    return n.replace(' ', '')


def easyprivacy_regex(rule: str) -> str:
    """An EasyPrivacy path rule as a regex over path and query: * any text, ^ a separator or the end."""
    out = ''
    for ch in rule:
        out += '.*' if ch == '*' else r'(?:[^\w.%-]|$)' if ch == '^' else re.escape(ch)
    return out


class PathRules:
    """First-party tracking paths: curated rules (they name a vendor) first, then the list's EasyPrivacy rules."""

    def __init__(self, list_rules: list[str]):
        self.curated = PATH_RULES.get('rules', [])
        self.body = PATH_RULES.get('body_rules', [])
        self.rules = [r for r in list_rules if r]
        self.any = re.compile('|'.join(f'(?:{easyprivacy_regex(r)})' for r in self.rules)) if self.rules else None

    def match(self, url: str, body: str | None) -> dict | None:
        u = urlparse(url)
        pq = (u.path or '/') + ('?' + u.query if u.query else '')
        for r in self.curated:
            if r['pattern'] in pq:
                return {'rule': r['pattern'], 'rule_source': 'data/vendors.json path_rules', 'vendor': r.get('vendor'), 'category': r.get('category')}
        for r in self.body:
            if body and r['pattern'] in body and r.get('requires', '') in body.lower():
                return {'rule': r['pattern'], 'rule_source': 'data/vendors.json body_rules', 'vendor': r.get('vendor'),
                        'category': r.get('category'), 'what': r.get('what')}
        if self.any and self.any.search(pq):
            hit = next(r for r in self.rules if re.search(easyprivacy_regex(r), pq))
            return {'rule': hit, 'rule_source': 'EasyPrivacy path rule (list)', 'vendor': None, 'category': 'tracker'}
        return None


def hex_key(h: str, text: str) -> str | None:
    """The JSON key or query parameter a hex value sits under, when the text shows it."""
    m = re.search(r'["\']?([\w$.-]{1,40})["\']?\s*[:=]\s*["\']?' + re.escape(h), text)
    return m.group(1).lower() if m else None


def csp_attempts(st: dict, first: set, dom_of) -> list[dict]:
    """Requests the site's Content Security Policy stopped in the browser: from console violations recorded by the
    capture, and from CSP report bodies the page sent (blocked-uri / blockedURL with the directive)."""
    out = []
    for v in st.get('csp_violations') or []:
        if v.get('host'):
            out.append({'state': st['state'], 'phase': v.get('phase'), 'source': 'console', 'url': v['url'], 'host': v['host'],
                        'directive': v.get('directive')})
    for r in st.get('requests') or []:
        body = r.get('body') or ''
        if r['method'] in SAFE or ('blocked' not in body.lower() and 'csp' not in r['path'].lower()):
            continue
        dirs = [m.group('d') for m in CSP_DIRECTIVE.finditer(body)]
        for i, m in enumerate(CSP_REPORT.finditer(body)):
            out.append({'state': st['state'], 'phase': r['phase'], 'source': f'csp_report #{r["n"]}', 'url': m.group('url')[:300],
                        'host': urlparse(m.group('url')).hostname, 'directive': dirs[i] if i < len(dirs) else None})
    return [x for x in out if x['host'] and dom_of(x['host']) not in first]


def static_only(a, static: dict, lists: dict, regions: set) -> dict:
    reason = a.run_status_reason or 'no live capture was given: the browser half did not run'
    no_banner = no_banner_cfg(json.loads(a.flows.read_text(encoding='utf-8')) if a.flows else None)
    matrix = ([{'state': 'no_banner', 'status': 'not_run', 'reason': reason}]
              + [{'state': s, 'status': 'not_applicable', 'reason': NO_BANNER_REASON} for s in CONSENT_STATES]) if no_banner else         [{'state': s, 'status': 'not_run', 'reason': reason} for s in CONSENT_STATES]
    return {**({'consent_banner': False} if no_banner else {}),'run_status': 'not_run', 'run_status_reason': reason, 'capture': None, 'static': a.static.as_posix(),
            'inputs': {'flows': a.flows.as_posix() if a.flows else None, 'lists': a.lists.as_posix() if a.lists else None,
                       'declared': a.declared.as_posix() if a.declared else None, 'regions': a.regions},
            'lists': lists.get('source'),
            'declared': json.loads(a.declared.read_text(encoding='utf-8')).get('source') if a.declared else None, 'regions': sorted(regions), 'first_party': [], 'third_party_mode': None,
            'blocked_domains': [], 'blocked_first_party_writes': [], 'csp_attempts': [], 'recipients': [],
            'consent_matrix': matrix, 'candidates': static['candidates']}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--capture', type=Path, help='the live capture; leave out for a static-only (not_run) output')
    ap.add_argument('--static', type=Path)
    ap.add_argument('--flows', type=Path, help='flows.json (required with --capture)')
    ap.add_argument('--lists', type=Path, help='domain and cookie list (fetch_lists.py output, or an example list); required with --capture')
    ap.add_argument('--declared', type=Path)
    ap.add_argument('--regions', default='AU,EU')
    ap.add_argument('--run-status-reason', help='static only: why the live capture did not run')
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    regions = set(a.regions.split(','))
    lists = json.loads(a.lists.read_text(encoding='utf-8')) if a.lists else {}
    if not a.capture:
        if not a.static:
            ap.error('give --capture, or --static for a static-only output')
        out = static_only(a, json.loads(a.static.read_text(encoding='utf-8')), lists, regions)
        return write(a, out, 0)
    if not (a.flows and a.lists):
        ap.error('--capture needs --flows and --lists')
    cap = json.loads(a.capture.read_text(encoding='utf-8'))
    flows = json.loads(a.flows.read_text(encoding='utf-8'))
    declared = json.loads(a.declared.read_text(encoding='utf-8')) if a.declared else {'processors': []}
    declared_by_name = {norm(p['vendor']): p['vendor'] for p in declared['processors']}
    declared_by_domain = {registrable(d): p['vendor'] for p in declared['processors'] for d in p.get('domains', [])}
    known = {**VENDORS, **lists.get('vendors', {})}
    fp = FirstParty(flows.get('first_party_domains'), urlparse(cap['site']).hostname)
    first = set(fp.domains)
    domains, cookie_list = lists.get('domains', {}), lists.get('cookies', {})
    rules = PathRules(lists.get('path_rules') or [])
    consent_cfg = flows.get('consent') or {}

    def dom_of(host: str | None) -> str:
        """A first-party host's first-party entry (as given in first_party_domains), else its registrable domain."""
        h = (host or '').lower().strip('.')
        hit = sorted((d for d in fp.domains if h == d or h.endswith('.' + d)), key=len, reverse=True)
        return hit[0] if hit else registrable(h)

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
    csp = {s: csp_attempts(states[s], first, dom_of) for s in tested}
    refuses = set(consent_cfg.get('partial_refuses') or [])
    # ---- tracking before consent, after reject, after a partial choice, after closing the banner without one
    for st_name, typ, phases in (('before', 'tracking_before_consent', None), ('no_banner', 'tracking_before_consent', None),
                                 ('reject', 'tracking_after_reject', 'after'),
                                 ('partial', 'tracking_after_reject', 'after'), ('dismiss', 'tracking_before_consent', 'after')):
        st = states.get(st_name)
        if not st or st['status'] != 'tested':
            continue
        reqs = [r for r in st['requests'] if phases is None or r['phase'] != 'initial']
        by_dom: dict = {}
        for r in reqs:
            d = dom_of(r['host'])
            if d not in first:
                by_dom.setdefault(d, []).append(r)
        csp_by_dom: dict = {}
        for x in csp[st_name]:
            if phases is None or x['phase'] != 'initial':
                csp_by_dom.setdefault(dom_of(x['host']), []).append(x)
        last = st['snapshots'][-1]
        initial = st['snapshots'][0]
        snap_cookies = (last if phases else initial)['cookies']
        for d in sorted(set(by_dom) | set(csp_by_dom)):
            rs = by_dom.get(d, [])
            v = vendor(d)
            if st_name == 'partial' and refuses and v.get('category') not in refuses | {None, 'tracker'}:
                continue  # a purpose the partial choice accepted
            ck = sorted({c['name'] for c in snap_cookies if (cookie_list.get(c['name']) or {}).get('domain') == d or dom_of(c['domain'].lstrip('.')) == d})
            c = cand(type=typ, state=st_name, domain=d, vendor=v.get('vendor'), vendor_category=v.get('category'), recipient=kind(d),
                     requests=len(rs), paths=sorted({r['path'] for r in rs})[:10], phases=sorted({r['phase'] for r in rs}),
                     cookies=ck, example_requests=[{'state': st_name, 'n': r['n'], 'phase': r['phase'], 'method': r['method'], 'url': r['url'][:300]} for r in rs[:3]])
            if blocked(rs):
                c['blocked_requests'] = blocked(rs)
            if csp_by_dom.get(d):
                c['csp_blocked_attempts'] = len(csp_by_dom[d])
                c['csp_examples'] = [{k: x[k] for k in ('phase', 'source', 'url', 'directive')} for x in csp_by_dom[d][:3]]
            if st_name == 'partial':
                c['partial_refuses'] = sorted(refuses) or None
        # first-party tracking paths (analytics proxies, error-tracker tunnels) on the site's own host
        fpt: dict = {}
        for r in reqs:
            d = dom_of(r['host'])
            if d in first:
                hit = rules.match(r['url'], r.get('body'))
                if hit:
                    fpt.setdefault((d, r['path']), {'hit': hit, 'rs': []})['rs'].append(r)
        for (d, path), e in sorted(fpt.items()):
            rs, hit = e['rs'], e['hit']
            c = cand(type=typ, subtype='first_party_proxy', state=st_name, domain=d, path=path, vendor=hit['vendor'],
                     vendor_category=hit['category'], recipient='first_party', matched_rule=hit['rule'], rule_source=hit['rule_source'],
                     requests=len(rs), paths=[path], phases=sorted({r['phase'] for r in rs}), cookies=[],
                     example_requests=[{'state': st_name, 'n': r['n'], 'phase': r['phase'], 'method': r['method'], 'url': r['url'][:300]} for r in rs[:3]])
            if hit.get('what'):
                c['note'] = hit['what']
            if blocked(rs):
                c['blocked_requests'] = blocked(rs)
        if st_name in ('before', 'no_banner'):
            for c in initial['cookies']:
                if dom_of(c['domain'].lstrip('.')) in first and not cookie_list.get(c['name']):
                    cand(type=typ, state=st_name, domain=dom_of(c['domain'].lstrip('.')), vendor=None, recipient='first_party', cookie=c['name'],
                         cookie_flags={k: c[k] for k in ('http_only', 'secure', 'same_site', 'persistent', 'expires_days', 'value_length')},
                         requests=0, paths=[], phases=['initial'], cookies=[c['name']], example_requests=[])
    # ---- first-party writes without a form submission (telemetry, consent records, autosave)
    beacons: dict = {}
    for st_name in tested:
        for r in states[st_name]['requests']:
            if r['method'] in SAFE or dom_of(r['host']) not in first or r.get('resource_type') == 'document':
                continue  # a form submission navigates (a document request); beacons are fetch, XHR, sendBeacon or ping
            if r.get('blocked') == 'write' or r['phase'] == 'initial' or (r['phase'] or '').startswith('after_'):
                e = beacons.setdefault(r['path'], {'states': set(), 'phases': set(), 'methods': set(), 'requests': [], 'blocked': 0, 'fields': set(),
                                                   'referer_fields': set()})
                e['states'].add(st_name)
                e['phases'].add(r['phase'])
                e['methods'].add(r['method'])
                e['blocked'] += 1 if r.get('blocked') else 0
                # a canary only in the Referer came from the page URL, not from what the write sends
                e['fields'] |= {m['field'] for m in r['matches'] if m['where'] != 'referer'}
                e['referer_fields'] |= {m['field'] for m in r['matches'] if m['where'] == 'referer'}
                e['requests'].append({'state': st_name, 'n': r['n'], 'phase': r['phase'], 'method': r['method'], 'url': r['url'][:300],
                                      'body_read': r.get('body_read')})
    blocked_fp_writes = []
    for path, e in sorted(beacons.items()):
        blocked_fp_writes.append({'path': path, 'requests': len(e['requests']), 'blocked': e['blocked'], 'states': sorted(e['states'])})
        c = cand(type='first_party_beacon', domain=sorted(first)[0], path=path, recipient='first_party', states=sorted(e['states']),
                 phases=sorted(e['phases']), methods=sorted(e['methods']), requests=len(e['requests']), blocked_requests=e['blocked'],
                 fields=sorted(e['fields']), categories=[], before_choice=bool({'before', 'no_banner'} & e['states']) or 'initial' in e['phases'],
                 example_requests=e['requests'][:3])
        if e['referer_fields'] - e['fields']:
            c['referer_fields'] = sorted(e['referer_fields'] - e['fields'])
            c['note'] = ('the canary rode only in the Referer header: the page URL holds it (a personal_data_in_url shape, such as a '
                         'search query written into the address), not what this write sends')
    # ---- canaries in requests
    to_third: dict = {}
    in_url: dict = {}
    hexes: dict = {}
    for st_name in tested:
        for r in states[st_name]['requests']:
            d = dom_of(r['host'])
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
                text = (r['url'] or '') + ' ' + (r.get('body') or '')
                for h in r.get('unmatched_hex', []):
                    e = hexes.setdefault((d, hex_key(h, text)), {'values': {}, 'requests': []})
                    e['values'].setdefault(h, set()).add(st_name)
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
    for (d, key), e in sorted(hexes.items(), key=lambda x: (x[0][0], x[0][1] or '')):
        vals = e['values']
        top = max(vals, key=lambda h: (len(vals[h]), sum(1 for q in e['requests'] if q['url'].find(h) >= 0), h))
        states_seen = sorted({s for ss in vals.values() for s in ss})
        cand(type='personal_data_to_third_party', subtype='possible_hashed_identifier', domain=d, vendor=vendor(d).get('vendor'), recipient=kind(d),
             key=key, value=top, length=len(top), states=states_seen, fields=[], categories=[], requests=len(e['requests']),
             example_requests=e['requests'][:2],
             facts={'distinct_values': len(vals), 'same_value_in_every_request': len(vals) == 1,
                    'differs_every_request': len(vals) > 1 and len(vals) == len(e['requests']),
                    'value_in_several_states': any(len(ss) > 1 for ss in vals.values()), 'states_seen': states_seen,
                    'known_sdk_id_key': (key or '') in SDK_ID_KEYS})
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

    def recipient(d: str) -> dict:
        return recipients.setdefault(d, {'domain': d, 'hosts': set(), 'vendor': vendor(d).get('vendor'), 'vendor_category': vendor(d).get('category'),
                                         'region': vendor(d).get('region'), 'kind': kind(d), 'declared_as': declared_as(d) if d not in first else None,
                                         'states': set(), 'categories': set(), 'fields': set(), 'requests': 0, 'blocked_requests': 0})
    for st_name in tested:
        for r in states[st_name]['requests']:
            e = recipient(dom_of(r['host']))
            e['hosts'].add(r['host'])
            e['requests'] += 1
            e['blocked_requests'] += 1 if r.get('blocked') else 0
            e['states'].add(st_name)
            e['fields'] |= {m['field'] for m in r['matches']}
        for x in csp[st_name]:
            e = recipient(dom_of(x['host']))
            e['hosts'].add(x['host'])
            e['states'].add(st_name)
            e['csp_blocked_attempts'] = e.get('csp_blocked_attempts', 0) + 1
    for e in recipients.values():
        e['categories'] = cats(e['fields'])
        for k in ('hosts', 'states', 'fields'):
            e[k] = sorted(e[k])
        e['named_by'] = 'domain list' if e['vendor'] else None
    matrix = []
    no_banner = no_banner_cfg(flows) or 'no_banner' in states
    order = (('no_banner',) + CONSENT_STATES) if no_banner else CONSENT_STATES + tuple(s for s in EXTRA_STATES if s in states)
    for st_name in order:
        st = states.get(st_name)
        if no_banner and st_name in CONSENT_STATES:
            matrix.append({'state': st_name, 'status': 'not_applicable', 'reason': NO_BANNER_REASON})
            continue
        if not st:
            matrix.append({'state': st_name, 'status': 'not_run'})
            continue
        if st['status'] != 'tested':
            matrix.append({'state': st_name, 'status': 'untested', 'reason': st.get('reason')})
            continue
        last = st['snapshots'][-1]
        seen_from = st_name in ('before', 'no_banner')
        third = sorted({dom_of(r['host']) for r in st['requests'] if dom_of(r['host']) not in first and (seen_from or r['phase'] != 'initial')})
        canaries = sorted({m['field'] for r in st['requests'] if dom_of(r['host']) not in first for m in r['matches']})
        row = {'state': st_name, 'status': 'tested', 'third_party_domains': third,
               'cookies': sorted(c['name'] for c in last['cookies']), 'canaries_to_third_parties': canaries}
        csp_doms = sorted({dom_of(x['host']) for x in csp[st_name] if seen_from or x['phase'] != 'initial'})
        if csp_doms:
            row['csp_blocked_domains'] = csp_doms
        if st.get('consent_writes_blocked'):
            row['consent_writes_blocked'] = st['consent_writes_blocked']
        matrix.append(row)
    problems = [f'{r["state"]} {r["status"]}' + (f' ({r["reason"]})' if r.get('reason') else '') for r in matrix
                if r['status'] not in ('tested', 'not_applicable')]
    problems += [f'{s} had {len(states[s]["errors"])} flow step error(s)' for s in tested if states[s].get('errors')]
    harness = cap.get('harness') or {}
    if (harness.get('requests') or {}).get('budget_exhausted'):
        problems.append('the request budget ran out, so later requests were aborted')
    if harness.get('stopped'):
        problems.append(f'the harness stopped the run: {harness["stopped"]}')
    blocked_domains = sorted({dom_of(r['host']) for st_name in tested for r in states[st_name]['requests']
                              if r.get('blocked') and dom_of(r['host']) not in first})
    out = {**({'consent_banner': False} if no_banner else {}), 'run_status': 'partial' if problems else 'complete', 'run_status_reason': '; '.join(problems) if problems else None,
           'capture': a.capture.as_posix(), 'static': a.static.as_posix() if a.static else None,
           'inputs': {'flows': a.flows.as_posix(), 'lists': a.lists.as_posix(), 'declared': a.declared.as_posix() if a.declared else None,
                      'regions': a.regions}, 'lists': lists.get('source'),
           'declared': declared.get('source'), 'regions': sorted(regions), 'first_party': sorted(first),
           'third_party_mode': cap.get('third_party'), 'blocked_domains': blocked_domains,  # mode null: captured before it was recorded
           'blocked_first_party_writes': blocked_fp_writes, 'csp_attempts': [x for s in tested for x in csp[s]],
           'recipients': sorted(recipients.values(), key=lambda e: e['domain']), 'consent_matrix': matrix, 'candidates': cands + static}
    return write(a, out, len(cands))


def write(a, out: dict, live: int) -> int:
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8', newline='\n')
    print(f'{live} live and {len(out["candidates"]) - live} static candidate(s), run {out["run_status"]} -> {a.out}')
    for c in out['candidates']:
        print(f'  {c["id"]} {c["type"]:28} {c.get("state") or ",".join(c.get("states", [])):20} {c.get("domain") or c.get("file", "")} {c.get("fields") or c.get("cookie") or ""}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
