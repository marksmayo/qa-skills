#!/usr/bin/env python3
"""Download the tracker and cookie lists at their pinned commits, verify them, and build domains.json.

    python fetch_lists.py [--cache DIR] [--out DIR/domains.json]     # download the pinned commits, verify, build
    python fetch_lists.py --check                                    # verify the cached files only (no network)
    python fetch_lists.py --update [--write-lock PATH]               # pin each list's latest commit, download, record
    python fetch_lists.py --lock PATH ...                            # use a lock other than data/lists.lock.json

Each list is pinned to a git commit of its GitHub repository and fetched from
raw.githubusercontent.com/<repo>/<commit>/<path>, which never changes, so the sha256 in the lock holds for
good. EasyPrivacy's own URL (easylist.to) is rebuilt several times a day; its built copy is committed to the
gh-pages branch of easylist/easylist, and that is what is pinned. A hash mismatch at a pinned commit is
therefore a real problem (a corrupted or altered download), not a newer version.

--update asks the GitHub API for the latest commit on each list's branch, downloads it and records commit,
sha256 and version; it writes data/lists.lock.json, or the file named by --write-lock (a scratch copy, so a
run doesn't edit the skill). Say in the report which commits were used.

Lists (not committed to this repository; each has its own licence):
  EasyPrivacy           GPL-3.0-or-later / CC BY-SA 3.0   tracker domains (||domain^ rules)
  Open Cookie Database  Apache-2.0                        cookie names and domains -> platform, purpose, retention
The cache defaults to ~/.cache/qa-skills/privacy-lists. domains.json is what merge_candidates.py --lists reads:
{"source", "domains": {registrable domain: {vendor, category, region, tracker}}, "cookies": {name: {...}}, "vendors",
"path_rules": [EasyPrivacy path rules such as /_vercel/insights/script.js, for first-party analytics proxies and tunnels]}.
Registrable domains know the shared-hosting suffixes in data/public-suffix-private.json (a Public Suffix List
subset, pinned by SHA-256 under "bundled" in the lock and verified on every run, --check included), so a rule for
one vercel.app or netlify.app tenant never marks the whole suffix, or your own site, as a tracker.
Vendors are named only from data/vendors.json (curated); EasyPrivacy-only domains are trackers without
a vendor name, and the Open Cookie Database names cookies only. Neither list has regions, so cross-border candidates need a
region supplied by the user (an extra domains file merged with --extra).
"""
from __future__ import annotations
import argparse, csv, hashlib, io, json, re, sys, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from domains import PSL_FILE, psl_sha256, registrable  # noqa: E402
LOCK = HERE.parent / 'data' / 'lists.lock.json'
VENDORS = HERE.parent / 'data' / 'vendors.json'
CATEGORY = {'Analytics': 'analytics', 'Marketing': 'advertising', 'Functional': 'functional', 'Security': 'security', 'Personalization': 'personalisation'}
PATH_RULE = re.compile(r'^/[^#@|]')


def get(url: str, accept: str | None = None) -> bytes:
    headers = {'User-Agent': 'qa-skills privacy-flow-auditor fetch_lists.py'}
    if accept:
        headers['Accept'] = accept
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
        return r.read()


def pinned_url(spec: dict) -> str:
    return f'https://raw.githubusercontent.com/{spec["repo"]}/{spec["ref"]}/{spec["path"]}'


def latest_commit(spec: dict) -> str:
    """The commit at the head of the list's branch (GitHub API, no token needed)."""
    sha = get(f'https://api.github.com/repos/{spec["repo"]}/commits/{spec["branch"]}', accept='application/vnd.github.sha').decode().strip()
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError(f'unexpected answer from the GitHub API for {spec["repo"]}@{spec["branch"]}: {sha[:80]!r}')
    return sha


def version_of(name: str, data: bytes) -> str | None:
    m = re.search(rb'! Version: (\d+)', data[:4000]) if name == 'easyprivacy' else None
    return m.group(1).decode() if m else None


def path_rule(line: str) -> str | None:
    """An EasyPrivacy path rule that applies to first-party requests (/_vercel/insights/script.js, /?sentry_key=),
    without its options; None for domain rules, element hiding, exceptions, regex rules and third-party-only rules."""
    line = line.strip()
    if not PATH_RULE.match(line) or (line.endswith('/') and len(line) > 2 and '\\' in line):
        return None
    rule, _, opts = line.partition('$')
    if 'third-party' in opts.split(',') or 'domain=' in opts or len(rule) < 4:
        return None
    return rule


def build(easy: bytes, ocd: bytes, lock: dict) -> dict:
    domains: dict = {}
    paths: list[str] = []
    for line in easy.decode('utf-8', 'replace').splitlines():
        m = re.match(r'^\|\|([a-z0-9.-]+\.[a-z]{2,})[\^/]', line.strip())
        if m:
            # registrable() knows shared-hosting suffixes: ||someone.vercel.app^ marks that app, not vercel.app
            domains.setdefault(registrable(m.group(1)), {'vendor': None, 'category': 'tracker', 'region': None, 'tracker': True})['tracker'] = True
            continue
        r = path_rule(line)
        if r:
            paths.append(r)
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
    curated = json.loads(VENDORS.read_text(encoding='utf-8'))
    for d, v in curated['domains'].items():
        e = domains.setdefault(d, {'vendor': None, 'category': None, 'region': None, 'tracker': False})
        e.update(vendor=v['vendor'], category=v['category'])  # keeps the EasyPrivacy tracker flag
    src = '; '.join(f'{k} {v["repo"]}@{v["ref"][:10]}{" version " + v["version"] if v.get("version") else ""} (sha256 {v["sha256"][:12]})'
                    for k, v in lock['lists'].items())
    return {'source': src, 'domains': dict(sorted(domains.items())), 'cookies': dict(sorted(cookies.items())),
            'vendors': curated.get('vendors', {}), 'path_rules': sorted(set(paths))}


def check_bundled(lock: dict) -> bool:
    """The files shipped in data/ (the Public Suffix List subset) match their pinned SHA-256."""
    ok = True
    for name, spec in (lock.get('bundled') or {}).items():
        digest = psl_sha256() if name == 'public_suffix_private' else None
        if digest != spec['sha256']:
            print(f'{name}: sha256 {(digest or "?")[:12]} of {PSL_FILE.name} differs from the pinned {spec["sha256"][:12]}; '
                  'update the lock when the file is edited on purpose')
            ok = False
        else:
            print(f'{name}: {spec["file"]} (bundled), sha256 {digest[:12]} ok')
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--cache', type=Path, default=Path.home() / '.cache' / 'qa-skills' / 'privacy-lists')
    ap.add_argument('--out', type=Path)
    ap.add_argument('--lock', type=Path, default=LOCK, help='the lock to read (default data/lists.lock.json)')
    ap.add_argument('--update', action='store_true', help='pin the latest commit of each list and record its hash')
    ap.add_argument('--write-lock', type=Path, help='with --update: write the new lock here instead of over --lock')
    ap.add_argument('--check', action='store_true', help='verify the cached files against the lock, without downloading')
    ap.add_argument('--extra', type=Path, help='a domains file merged over the result (regions, first-party vendors, corrections)')
    a = ap.parse_args()
    if a.check and a.update:
        ap.error('--check and --update do not go together')
    if a.write_lock and not a.update:
        ap.error('--write-lock needs --update')
    lock = json.loads(a.lock.read_text(encoding='utf-8'))
    bundled_ok = check_bundled(lock)
    blobs, missing = {}, []
    for name, spec in lock['lists'].items():
        path = a.cache / spec['file']
        if a.check:
            if not path.exists():
                print(f'{name}: not downloaded ({path} is missing); run fetch_lists.py without --check')
                missing.append(name)
                continue
            data = path.read_bytes()
        else:
            if a.update:
                spec['ref'] = latest_commit(spec)
            data = get(pinned_url(spec))
            a.cache.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        if a.update:
            spec.update(url=pinned_url(spec), sha256=digest, version=version_of(name, data))
        elif digest != spec['sha256']:
            where = 'the cached copy' if a.check else f'the download of {spec["repo"]}@{spec["ref"][:10]}'
            print(f'{name}: sha256 {digest[:12]} of {where} differs from the pinned {spec["sha256"][:12]}. The pinned commit never changes, '
                  f'so the file is corrupted or altered{", or the cache holds another pin: download again without --check" if a.check else ""}. '
                  f'To move to a newer list, run --update (with --write-lock for a scratch lock) and say so in the report.')
            return 1
        blobs[name] = data
        print(f'{name}: {spec["repo"]}@{spec["ref"][:10]}{" version " + spec["version"] if spec.get("version") else ""}, '
              f'{len(data):,} bytes, sha256 {digest[:12]} ok')
    if a.update:
        target = a.write_lock or a.lock
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(lock, indent=1) + '\n', encoding='utf-8', newline='\n')
        print(f'lock -> {target}')
    if a.check:
        return 1 if missing or not bundled_ok else 0
    if not bundled_ok:
        return 1
    out = build(blobs['easyprivacy'], blobs['open_cookie_database'], lock)
    if a.extra:
        extra = json.loads(a.extra.read_text(encoding='utf-8'))
        for k in ('domains', 'cookies', 'vendors'):
            for d, v in extra.get(k, {}).items():
                out[k].setdefault(d, {}).update(v)
        out['source'] += f'; extra: {extra.get("source", a.extra.name)}'
    target = a.out or a.cache / 'domains.json'
    target.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(out["domains"]):,} domains ({sum(1 for v in out["domains"].values() if v.get("vendor")):,} named), {len(out["cookies"]):,} cookies -> {target}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
