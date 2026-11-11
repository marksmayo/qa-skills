#!/usr/bin/env python3
"""Download the tracker and cookie lists, check them against data/lists.lock.json, and build domains.json.

    python fetch_lists.py [--cache DIR] [--out DIR/domains.json]     # download, verify the pinned hashes, build
    python fetch_lists.py --update                                   # accept the current versions and re-pin them
    python fetch_lists.py --check                                    # verify the cached files only

Lists (not committed to this repository; each has its own licence):
  EasyPrivacy           GPL-3.0-or-later / CC BY-SA 3.0   tracker domains (||domain^ rules)
  Open Cookie Database  Apache-2.0                        cookie names and domains -> platform, purpose, retention
The cache defaults to ~/.cache/qa-skills/privacy-lists. domains.json is what merge_candidates.py --lists reads:
{"source", "domains": {registrable domain: {vendor, category, region, tracker}}, "cookies": {name: {...}}}.
Vendors are named only from data/vendors.json (curated); EasyPrivacy-only domains are trackers without
a vendor name, and the Open Cookie Database names cookies only. Neither list has regions, so cross-border candidates need a
region supplied by the user (an extra domains file merged with --extra).
"""
from __future__ import annotations
import argparse, csv, hashlib, io, json, re, sys, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCK = HERE.parent / 'data' / 'lists.lock.json'
VENDORS = HERE.parent / 'data' / 'vendors.json'
CATEGORY = {'Analytics': 'analytics', 'Marketing': 'advertising', 'Functional': 'functional', 'Security': 'security', 'Personalization': 'personalisation'}
TWO_LEVEL = ('com.au', 'net.au', 'org.au', 'gov.au', 'edu.au', 'co.uk', 'org.uk', 'gov.uk', 'ac.uk', 'co.nz', 'co.jp', 'com.br')


def registrable(host: str) -> str:
    parts = host.lower().strip('.').split('.')
    n = 3 if '.'.join(parts[-2:]) in TWO_LEVEL else 2
    return '.'.join(parts[-n:])


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={'User-Agent': 'qa-skills privacy-flow-auditor fetch_lists.py'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def build(easy: bytes, ocd: bytes, lock: dict) -> dict:
    domains: dict = {}
    for line in easy.decode('utf-8', 'replace').splitlines():
        m = re.match(r'^\|\|([a-z0-9.-]+\.[a-z]{2,})[\^/]', line.strip())
        if m:
            domains.setdefault(registrable(m.group(1)), {'vendor': None, 'category': 'tracker', 'region': None, 'tracker': True})['tracker'] = True
    cookies: dict = {}
    for row in csv.DictReader(io.StringIO(ocd.decode('utf-8-sig', 'replace'))):
        dom = (row.get('Domain') or '').strip()
        cat = CATEGORY.get((row.get('Category') or '').strip(), (row.get('Category') or '').strip().lower() or None)
        name = (row.get('Cookie / Data Key name') or '').strip()
        if name and row.get('Wildcard match', '0').strip() == '0':
            cookies.setdefault(name, {'vendor': (row.get('Platform') or '').strip() or None,
                                      'domain': registrable(dom.lstrip('*.')) if dom and '.' in dom and ' ' not in dom else None,
                                      'purpose': cat, 'retention': (row.get('Retention period') or '').strip() or None})
    # vendor names come only from the curated data/vendors.json
    for d, v in json.loads(VENDORS.read_text(encoding='utf-8'))['domains'].items():
        e = domains.setdefault(d, {'vendor': None, 'category': None, 'region': None, 'tracker': False})
        e.update(vendor=v['vendor'], category=v['category'])  # keeps the EasyPrivacy tracker flag
    src = '; '.join(f'{k} {v.get("version") or v.get("ref", "")[:10]} (sha256 {v["sha256"][:12]})' for k, v in lock['lists'].items())
    return {'source': src, 'domains': dict(sorted(domains.items())), 'cookies': dict(sorted(cookies.items()))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--cache', type=Path, default=Path.home() / '.cache' / 'qa-skills' / 'privacy-lists')
    ap.add_argument('--out', type=Path)
    ap.add_argument('--update', action='store_true')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--extra', type=Path, help='a domains file merged over the result (regions, first-party vendors, corrections)')
    a = ap.parse_args()
    lock = json.loads(LOCK.read_text(encoding='utf-8'))
    a.cache.mkdir(parents=True, exist_ok=True)
    blobs = {}
    for name, spec in lock['lists'].items():
        path = a.cache / spec['file']
        if a.check:
            data = path.read_bytes() if path.exists() else b''
        else:
            data = get(spec['url'])
            path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        if a.update:
            spec['sha256'] = digest
            if name == 'easyprivacy':
                m = re.search(rb'! Version: (\d+)', data)
                spec['version'] = m.group(1).decode() if m else None
        elif digest != spec['sha256']:
            print(f'{name}: sha256 {digest[:12]} differs from the pinned {spec["sha256"][:12]}; '
                  f'run with --update to accept the current version (and say so in the report)')
            return 1
        blobs[name] = data
        print(f'{name}: {len(data):,} bytes, sha256 {digest[:12]} ok')
    if a.update:
        LOCK.write_text(json.dumps(lock, indent=1) + '\n', encoding='utf-8', newline='\n')
    if a.check:
        return 0
    out = build(blobs['easyprivacy'], blobs['open_cookie_database'], lock)
    if a.extra:
        extra = json.loads(a.extra.read_text(encoding='utf-8'))
        for k in ('domains', 'cookies'):
            for d, v in extra.get(k, {}).items():
                out[k].setdefault(d, {}).update(v)
        out['source'] += f'; extra: {extra.get("source", a.extra.name)}'
    target = a.out or a.cache / 'domains.json'
    target.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(out["domains"]):,} domains ({sum(1 for v in out["domains"].values() if v["vendor"]):,} named), {len(out["cookies"]):,} cookies -> {target}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
