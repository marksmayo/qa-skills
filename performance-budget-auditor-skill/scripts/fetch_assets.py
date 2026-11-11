#!/usr/bin/env python3
"""Download, verify and cache the pinned browser assets listed in data/assets.lock.json (web-vitals).

Each asset is fetched as its npm tarball, checked against the registry's SHA-512 integrity, and
only the one file the skill needs is read out of it (nothing else is extracted). The file is also
checked against the SHA-256 in the lock file. A file that fails a check is never cached and never
injected into a page.

    python fetch_assets.py                  # fetch into the cache
    python fetch_assets.py --check          # verify the cache only, no network
    python fetch_assets.py --from-dir <dir> # use tarballs already downloaded (air-gapped)
    python fetch_assets.py --cache <dir>    # cache location (default ~/.cache/qa-skills/perf-assets)

Prints a JSON status list and exits 1 if an asset is unavailable. Adapted from the WCAG auditor's
fetch_engines.py.
"""
from __future__ import annotations
import argparse, base64, hashlib, io, json, os, sys, tarfile, time, urllib.request
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
LOCK = SKILL / 'data' / 'assets.lock.json'
UA = 'qa-skills-performance-budget-auditor/1.0 (+fetch_assets.py)'


def load_lock() -> dict:
    return json.loads(LOCK.read_text(encoding='utf-8'))


def cache_root(arg: str | None = None) -> Path:
    raw = arg or os.environ.get('PERF_ASSET_CACHE') or load_lock()['cache_default']
    return Path(os.path.expanduser(raw))


def engine_dir(cache: Path, eng: dict) -> Path:
    return cache / f"{eng['name']}-{eng['version']}"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def integrity_ok(data: bytes, integrity: str) -> bool:
    algo, _, b64 = integrity.partition('-')
    if algo not in ('sha512', 'sha384', 'sha256'):
        return False
    return base64.b64encode(hashlib.new(algo, data).digest()).decode() == b64


def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _member(tar_bytes: bytes, member: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode='r:gz') as tf:
        info = tf.getmember(member)
        if not info.isfile():
            raise ValueError(f'{member} is not a regular file in the tarball')
        f = tf.extractfile(info)
        return f.read()


def _manifest(d: Path) -> dict:
    p = d / 'verified.json'
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}


def verify_cached(cache: Path, eng: dict) -> tuple[Path | None, str]:
    """Return (path, '') when the cached file passes its checks, else (None, reason)."""
    d = engine_dir(cache, eng)
    f = d / eng['file']
    if not f.exists():
        return None, 'not in cache'
    got = sha256(f.read_bytes())
    want = eng.get('sha256') or _manifest(d).get('sha256')
    if not want:
        return None, 'no recorded SHA-256 to check against'
    if got != want:
        return None, f'SHA-256 mismatch (cache {got[:12]}, expected {want[:12]})'
    return f, ''


def fetch(cache: Path, eng: dict, from_dir: Path | None = None) -> dict:
    status = {'name': eng['name'], 'version': eng['version'], 'license': eng['license'],
              'required': eng.get('required', True), 'file': None, 'sha256': None, 'status': 'not_run', 'reason': ''}
    path, why = verify_cached(cache, eng)
    if path:
        status.update(file=str(path), sha256=sha256(path.read_bytes()), status='ready', reason='cached and verified')
        return status
    data = None
    errors = []
    tar_name = eng['tarball'].rsplit('/', 1)[-1]
    sources = [('local', from_dir / tar_name)] if from_dir else [('registry', eng['tarball'])]
    for kind, src in sources:
        try:
            raw = Path(src).read_bytes() if kind == 'local' else _get(src)
            if not integrity_ok(raw, eng['tarball_integrity']):
                errors.append(f'{kind}: tarball integrity check failed')
                continue
            data = _member(raw, eng['member'])
            break
        except Exception as e:  # network, missing file, bad tarball
            errors.append(f'{kind}: {type(e).__name__}: {e}')
    if data is None and eng.get('mirror') and eng.get('sha256') and not from_dir:
        try:
            data = _get(eng['mirror'])
        except Exception as e:
            errors.append(f'mirror: {type(e).__name__}: {e}')
    if data is None:
        status.update(status='unavailable', reason='; '.join(errors) or why)
        return status
    got = sha256(data)
    if eng.get('sha256') and got != eng['sha256']:
        status.update(status='unavailable', reason=f'SHA-256 mismatch ({got[:12]} != {eng["sha256"][:12]}); not cached')
        return status
    d = engine_dir(cache, eng)
    d.mkdir(parents=True, exist_ok=True)
    (d / eng['file']).write_bytes(data)
    note = 'tarball integrity and SHA-256 verified' if eng.get('sha256') else \
        'tarball integrity verified; SHA-256 recorded on first fetch'
    (d / 'verified.json').write_text(json.dumps({'sha256': got, 'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                                                 'source': eng['tarball'], 'check': note}, indent=1), encoding='utf-8')
    status.update(file=str(d / eng['file']), sha256=got, status='ready', reason=note)
    return status


def asset_path(name: str, cache: Path | None = None) -> Path:
    """Verified path of one cached asset, for measure.py. Raises if missing or tampered."""
    lock = load_lock()
    eng = next(e for e in lock['assets'] if e['name'] == name)
    path, why = verify_cached(cache or cache_root(), eng)
    if not path:
        raise FileNotFoundError(f'{name} {eng["version"]}: {why}. Run fetch_assets.py first.')
    return path


def main() -> int:
    sys.stdout.reconfigure(errors='replace')
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--cache')
    ap.add_argument('--assets', help='comma-separated names (default: all)')
    ap.add_argument('--check', action='store_true', help='verify the cache only; no downloads')
    ap.add_argument('--from-dir', help='directory holding the npm tarballs')
    args = ap.parse_args()
    lock = load_lock()
    cache = cache_root(args.cache)
    wanted = set(args.assets.split(',')) if args.assets else None
    out = []
    for eng in lock['assets']:
        if wanted is not None and eng['name'] not in wanted:
            continue
        if args.check:
            path, why = verify_cached(cache, eng)
            out.append({'name': eng['name'], 'version': eng['version'], 'required': eng.get('required', True),
                        'status': 'ready' if path else 'unavailable', 'file': str(path) if path else None,
                        'reason': 'cached and verified' if path else why})
        else:
            out.append(fetch(cache, eng, Path(args.from_dir) if args.from_dir else None))
    print(json.dumps(out, indent=1))
    return 1 if any(e['status'] != 'ready' for e in out) else 0


if __name__ == '__main__':
    raise SystemExit(main())
