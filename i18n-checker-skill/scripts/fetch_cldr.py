#!/usr/bin/env python3
"""Build, pin and verify the CLDR subsets this skill reads (Unicode-3.0 licensed data).

    python fetch_cldr.py --check                  # verify data/cldr-*.json against data/cldr.lock.json (offline)
    python fetch_cldr.py --rebuild                # download the pinned CLDR files, check their SHA-256, rebuild the subsets
    python fetch_cldr.py --rebuild --version 49.0.0 --locales en,de,fr,ar   # pin a new version or locale set (rewrites the lock)

Sources (CLDR JSON packages on npm, read file by file through jsDelivr's npm mirror):
  cldr-core          supplemental/plurals.json and ordinals.json  -> data/cldr-plurals.json (every locale's categories)
  cldr-numbers-full  main/<locale>/numbers.json                   -> data/cldr-formats.json (symbols, decimal, percent, currency)
  cldr-dates-full    main/<locale>/ca-gregorian.json              -> data/cldr-formats.json (date formats)

The subsets are committed, so nothing is downloaded in normal use. A rebuild refuses any source file
whose SHA-256 differs from the lock, unless --version or --locales asks for a new pin.
"""
from __future__ import annotations
import argparse, hashlib, json, sys, time, urllib.request
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
DATA = SKILL / 'data'
LOCK = DATA / 'cldr.lock.json'
MIRROR = 'https://cdn.jsdelivr.net/npm/{pkg}@{ver}/{path}'
DEFAULT_LOCALES = ['en', 'de', 'fr', 'es', 'it', 'pt', 'nl', 'sv', 'da', 'nb', 'fi', 'pl', 'cs', 'ru', 'uk', 'tr', 'el', 'hu',
                   'ro', 'ar', 'he', 'fa', 'hi', 'ja', 'ko', 'zh', 'zh-Hant', 'th', 'vi', 'id']


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={'User-Agent': 'qa-skills-i18n-checker/1.0 (+fetch_cldr.py)'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def dump(obj) -> bytes:
    return (json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')


def build(ver: str, locales: list[str], pinned: dict | None) -> tuple[dict, dict, dict]:
    sources = {}

    def fetch(pkg: str, path: str):
        url = MIRROR.format(pkg=pkg, ver=ver, path=path)
        raw = get(url)
        h = sha(raw)
        if pinned is not None and pinned.get(url) != h:
            raise SystemExit(f'error: {url} has SHA-256 {h[:12]}, the lock says {str(pinned.get(url))[:12]}; refusing to rebuild')
        sources[url] = h
        return json.loads(raw.decode('utf-8'))

    card = fetch('cldr-core', 'supplemental/plurals.json')['supplemental']['plurals-type-cardinal']
    ordi = fetch('cldr-core', 'supplemental/ordinals.json')['supplemental']['plurals-type-ordinal']
    plurals = {'cardinal': {loc: sorted(k.split('-')[-1] for k in rules) for loc, rules in card.items()},
               'ordinal': {loc: sorted(k.split('-')[-1] for k in rules) for loc, rules in ordi.items()}}
    formats = {}
    for loc in locales:
        n = fetch('cldr-numbers-full', f'main/{loc}/numbers.json')['main'][loc]['numbers']
        ns = n.get('defaultNumberingSystem', 'latn')
        d = fetch('cldr-dates-full', f'main/{loc}/ca-gregorian.json')['main'][loc]['dates']['calendars']['gregorian']
        sym = n.get(f'symbols-numberSystem-{ns}') or n['symbols-numberSystem-latn']
        formats[loc] = {'numbering_system': ns, 'decimal': sym['decimal'], 'group': sym['group'], 'percent_sign': sym['percentSign'],
                        'decimal_pattern': (n.get(f'decimalFormats-numberSystem-{ns}') or n['decimalFormats-numberSystem-latn'])['standard'],
                        'percent_pattern': (n.get(f'percentFormats-numberSystem-{ns}') or n['percentFormats-numberSystem-latn'])['standard'],
                        'currency_pattern': (n.get(f'currencyFormats-numberSystem-{ns}') or n['currencyFormats-numberSystem-latn'])['standard'],
                        'date_short': d['dateFormats']['short'], 'date_medium': d['dateFormats']['medium']}
    return plurals, formats, sources


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--rebuild', action='store_true')
    ap.add_argument('--version')
    ap.add_argument('--locales')
    a = ap.parse_args()
    lock = json.loads(LOCK.read_text(encoding='utf-8')) if LOCK.exists() else None
    if a.check or not a.rebuild:
        if not lock:
            print('error: no data/cldr.lock.json; run --rebuild --version X first', file=sys.stderr)
            return 1
        bad = [f for f, h in lock['subsets'].items() if not (DATA / f).exists() or sha((DATA / f).read_bytes()) != h]
        print(json.dumps({'cldr_version': lock['version'], 'subsets': list(lock['subsets']), 'status': 'ready' if not bad else 'mismatch',
                          'mismatched': bad}, indent=1))
        return 1 if bad else 0
    new_pin = bool(a.version or a.locales) or not lock
    ver = a.version or lock['version']
    locales = a.locales.split(',') if a.locales else (lock['locales'] if lock else DEFAULT_LOCALES)
    plurals, formats, sources = build(ver, locales, None if new_pin else lock['sources'])
    files = {'cldr-plurals.json': dump(plurals), 'cldr-formats.json': dump(formats)}
    for name, data in files.items():
        (DATA / name).write_bytes(data)
    lock = {'schema': 1, 'source': 'Unicode CLDR JSON (npm cldr-core, cldr-numbers-full, cldr-dates-full) via jsDelivr', 'license': 'Unicode-3.0',
            'version': ver, 'locales': locales, 'built_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'sources': sources,
            'subsets': {name: sha(data) for name, data in files.items()}}
    LOCK.write_text(json.dumps(lock, indent=1) + '\n', encoding='utf-8')
    print(f'CLDR {ver}: {len(plurals["cardinal"])} locales of plural rules, {len(formats)} locales of formats -> data/', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
