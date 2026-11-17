#!/usr/bin/env python3
"""Read the text inside committed archives and logs, bounded (stdlib only; nothing is ever extracted to disk).

A scanner that walks a repository file by file never sees what a committed zip holds, and one that reads only known
data formats passes over `.log` files. Both can hold what the scan is looking for (a home-folder path, a host name,
a token), so "not opened" must at least be said, and the text should be read when it cheaply can be.

    is_text_name(name)       whether a file name is one read as text (TEXT_EXT: .log, .txt, .out, .err ... besides
                             the usual data and markup formats)
    is_archive_name(name)    'zip' for an archive read_archive() opens, the format's name ('tar', 'gzip', '7z' ...)
                             for one it only reports, else None
    list_archive(path)       the members of a zip, without reading any: {archive, format, members: [{name, size,
                             compressed, kind: text|binary|archive|dir, encrypted}], member_count, truncated, error}
    read_archive(path)       the text members of a zip, read in memory under caps:
                             {archive, format, member_count, read: [{name, text, bytes, truncated}],
                              skipped: [{name, reason}], bytes_read, complete, error}
    read_text(path)          a text file (a .log included) decoded leniently: {text, bytes, truncated} or None when
                             it is binary

Bounds (zip-bomb safe): at most MAX_MEMBERS members are listed, each text member is read through a stream that stops
at max_member_bytes whatever the header claims, the whole call stops at max_total_bytes, a member whose declared
compression ratio is above MAX_RATIO is not opened, and nested archives are never opened. Everything left unread is
in `skipped` with its reason, and `complete` is false when any text member was skipped or cut: a caller reports the
archive as partly read (not_assessed for the rest), never as clean.

    python archive_text.py <archive.zip> [--list]     # print what is inside (or the text members' sizes), as JSON
    python archive_text.py --self-test
"""
from __future__ import annotations
import json, os, sys, zipfile
from pathlib import Path, PurePosixPath

TEXT_EXT = {'.log', '.txt', '.text', '.out', '.err', '.stderr', '.stdout', '.trace', '.md', '.rst', '.json', '.jsonl',
            '.ndjson', '.geojson', '.csv', '.tsv', '.yaml', '.yml', '.xml', '.html', '.htm', '.svg', '.ini', '.cfg',
            '.conf', '.toml', '.env', '.properties', '.sql', '.diff', '.patch', '.har', '.css', '.js', '.mjs', '.ts',
            '.py', '.sh', '.ps1', '.bat', '.cmd', '.rs', '.go', '.lua', '.srt', '.vtt', '.m3u', '.m3u8', '.nfo',
            '.list', '.tap', '.junit', '.lock'}
ARCHIVE_EXT = {'.zip': 'zip', '.tar': 'tar', '.tgz': 'tar', '.gz': 'gzip', '.bz2': 'bzip2', '.xz': 'xz', '.7z': '7z',
               '.rar': 'rar', '.zst': 'zstd', '.whl': 'zip', '.jar': 'zip', '.nupkg': 'zip', '.apk': 'zip'}
OPENED = {'.zip'}            # read_archive opens these; packages (.whl, .jar ...) are listed as archives, not read
MAX_MEMBERS = 2000
MAX_MEMBER_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_RATIO = 200
RATIO_MIN_BYTES = 1024 * 1024
SNIFF = 4096


def is_text_name(name: str) -> bool:
    """Whether a file of this name is read as text (by extension; .log, .out and .err among them)."""
    return PurePosixPath(name.replace('\\', '/')).suffix.lower() in TEXT_EXT


def is_archive_name(name: str) -> str | None:
    """The archive format a file name says ('zip', 'tar', 'gzip', '7z' ...), else None. Only zip is opened."""
    return ARCHIVE_EXT.get(PurePosixPath(name.replace('\\', '/')).suffix.lower())


def looks_text(data: bytes) -> bool:
    """Whether the first bytes of a file are text: no NUL byte (UTF-16 with a byte-order mark excepted) and few
    control characters."""
    head = data[:SNIFF]
    if not head:
        return True
    if head[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return True
    if b'\x00' in head:
        return False
    control = sum(1 for b in head if b < 9 or 13 < b < 32 and b != 27)
    return control * 20 <= len(head)


def decode(data: bytes) -> str:
    """Bytes as text: UTF-8 (with or without a byte-order mark), UTF-16 with one, else Latin-1 (never an error)."""
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return data.decode('utf-16', 'replace')
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return data.decode('latin-1')


def read_text(path, max_bytes: int = MAX_MEMBER_BYTES) -> dict | None:
    """{text, bytes, truncated} for a file whose content is text (at most max_bytes of it), None when it is binary
    or cannot be read."""
    try:
        with open(path, 'rb') as fh:
            data = fh.read(max_bytes + 1)
    except OSError:
        return None
    if not looks_text(data):
        return None
    cut = len(data) > max_bytes
    data = data[:max_bytes]
    return {'text': decode(data), 'bytes': len(data), 'truncated': cut}


def _kind(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if name.endswith('/'):
        return 'dir'
    if is_archive_name(name):
        return 'archive'
    suffix = PurePosixPath(name).suffix.lower()
    return 'text' if suffix in TEXT_EXT or not suffix else 'binary'


def _open(path):
    try:
        return zipfile.ZipFile(path), None
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, NotImplementedError) as e:
        return None, f'{type(e).__name__}: {str(e)[:160]}'


def list_archive(path, max_members: int = MAX_MEMBERS) -> dict:
    """The members of a zip without reading any content. A member's kind is by name: text (TEXT_EXT, or no
    extension: read_archive then checks the content), archive (nested, never opened), binary, dir."""
    out = {'archive': Path(path).name, 'format': is_archive_name(str(path)) or 'zip', 'members': [], 'member_count': 0,
           'truncated': False, 'error': None}
    zf, err = _open(path)
    if zf is None:
        out['error'] = err
        return out
    with zf:
        infos = zf.infolist()
        out['member_count'] = len(infos)
        out['truncated'] = len(infos) > max_members
        for info in infos[:max_members]:
            out['members'].append({'name': info.filename, 'size': info.file_size, 'compressed': info.compress_size,
                                   'kind': _kind(info), 'encrypted': bool(info.flag_bits & 0x1)})
    return out


def read_archive(path, max_members: int = MAX_MEMBERS, max_member_bytes: int = MAX_MEMBER_BYTES,
                 max_total_bytes: int = MAX_TOTAL_BYTES, text_only: bool = True) -> dict:
    """The text members of a zip archive, read in memory (see the module text for the caps and the result).

    A member is read when its name says text (TEXT_EXT) or it has no extension and its first bytes are text; with
    text_only=False every member whose first bytes are text is read. `skipped` names every member not read, with one
    of: binary, nested archive (not opened), encrypted, compression ratio, member limit, total byte limit, or the
    read error. A member cut at max_member_bytes is in `read` with truncated: true."""
    out = {'archive': Path(path).name, 'format': is_archive_name(str(path)) or 'zip', 'member_count': 0, 'read': [],
           'skipped': [], 'bytes_read': 0, 'complete': False, 'error': None}
    zf, err = _open(path)
    if zf is None:
        out['error'] = err
        out['skipped'].append({'name': '*', 'reason': f'the archive could not be opened ({err})'})
        return out
    complete = True
    with zf:
        infos = zf.infolist()
        out['member_count'] = len(infos)
        seen = 0
        for n, info in enumerate(infos):
            name = info.filename
            kind = _kind(info)
            if kind == 'dir':
                continue
            if seen >= max_members:
                rest = sum(1 for i in infos[n:] if not i.filename.endswith('/'))
                out['skipped'].append({'name': name + (f' and {rest - 1} more' if rest > 1 else ''), 'reason':
                                       f'member limit ({max_members}) reached: not read'})
                complete = False
                break
            seen += 1
            if kind == 'archive':
                out['skipped'].append({'name': name, 'reason': 'nested archive: not opened'})
                complete = False
                continue
            if kind == 'binary' and text_only:
                out['skipped'].append({'name': name, 'reason': 'binary by its name: not read'})
                continue
            if info.flag_bits & 0x1:
                out['skipped'].append({'name': name, 'reason': 'encrypted: not read'})
                complete = False
                continue
            if info.file_size >= RATIO_MIN_BYTES and info.file_size > MAX_RATIO * max(info.compress_size, 1):
                out['skipped'].append({'name': name, 'reason':
                                       f'compression ratio above {MAX_RATIO} ({info.file_size} bytes from '
                                       f'{info.compress_size}): not opened'})
                complete = False
                continue
            room = max_total_bytes - out['bytes_read']
            if room <= 0:
                rest = sum(1 for i in infos[n:] if not i.filename.endswith('/'))
                out['skipped'].append({'name': name + (f' and {rest - 1} more' if rest > 1 else ''), 'reason':
                                       f'total byte limit ({max_total_bytes}) reached: not read'})
                complete = False
                break
            want = min(max_member_bytes, room)
            try:
                with zf.open(info) as fh:      # a stream: stops at `want` whatever the header says
                    data = fh.read(want + 1)
            except (OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError, EOFError, ValueError) as e:
                out['skipped'].append({'name': name, 'reason': f'could not be read ({type(e).__name__})'})
                complete = False
                continue
            if not looks_text(data):
                out['skipped'].append({'name': name, 'reason': 'binary content: not read'})
                continue
            cut = len(data) > want
            data = data[:want]
            out['bytes_read'] += len(data)
            out['read'].append({'name': name, 'text': decode(data), 'bytes': len(data), 'truncated': cut})
            if cut:
                complete = False
    out['complete'] = complete
    return out


def self_test() -> int:
    import shutil, tempfile
    fails = []
    tmp = Path(tempfile.mkdtemp(prefix='archive-text-'))
    try:
        z = tmp / 'run-results.zip'
        with zipfile.ZipFile(z, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('run/summary.json', '{"player": "tide", "path": "/home/example-user/media/a.mkv"}')
            zf.writestr('run/stderr.log', 'opening /home/example-user/media/a.mkv\n' * 3)
            zf.writestr('run/README', 'plain text with no extension\n')
            zf.writestr('run/frame.png', b'\x89PNG\r\n\x1a\n' + bytes(range(256)) * 4)
            zf.writestr('run/nested.zip', b'PK\x03\x04 not really')
            zf.writestr('run/empty/', '')
            zf.writestr('run/latin.txt', 'caf\xe9 au lait\n'.encode('latin-1'))
        listing = list_archive(z)
        kinds = {m['name']: m['kind'] for m in listing.get('members', [])}
        if kinds != {'run/summary.json': 'text', 'run/stderr.log': 'text', 'run/README': 'text', 'run/frame.png': 'binary',
                     'run/nested.zip': 'archive', 'run/empty/': 'dir', 'run/latin.txt': 'text'}:
            fails.append(f'list_archive names every member with its kind: {kinds or listing}')
        res = read_archive(z)
        got = {r['name']: r['text'] for r in res.get('read', [])}
        if sorted(got) != ['run/README', 'run/latin.txt', 'run/stderr.log', 'run/summary.json'] \
                or 'example-user' not in got.get('run/stderr.log', '') or 'caf' not in got.get('run/latin.txt', ''):
            fails.append(f'read_archive returns the text members (a .log and an extensionless text file included): {sorted(got)}')
        why = {s_['name']: s_['reason'] for s_ in res.get('skipped', [])}
        if 'binary' not in why.get('run/frame.png', '') or 'nested archive' not in why.get('run/nested.zip', ''):
            fails.append(f'binary members and nested archives are skipped with a reason, never opened: {why}')
        if res.get('complete') is not False or res.get('member_count') != 7:
            fails.append(f'an archive with a nested archive left unopened is not complete: {res.get("complete")}, {res.get("member_count")}')
        plain = tmp / 'plain.zip'
        with zipfile.ZipFile(plain, 'w') as zf:
            zf.writestr('a.log', 'one\n')
            zf.writestr('b.png', b'\x89PNG' + bytes(64))
        res2 = read_archive(plain)
        plain.unlink()
        if res2.get('complete') is not True or [r['name'] for r in res2.get('read', [])] != ['a.log']:
            fails.append(f'an archive whose text members were all read whole is complete (binary members aside): {res2}')
        if any(p.name != 'run-results.zip' for p in tmp.iterdir()):
            fails.append(f'nothing is extracted to disk: {sorted(p.name for p in tmp.iterdir())}')

        bomb = tmp / 'bomb.zip'
        with zipfile.ZipFile(bomb, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('zeros.log', b'0' * (40 * 1024 * 1024))
            zf.writestr('small.log', 'ok\n')
        res = read_archive(bomb)
        why = {s_['name']: s_['reason'] for s_ in res.get('skipped', [])}
        if 'ratio' not in why.get('zeros.log', '') or res.get('bytes_read', 10**9) > 1024 or res.get('complete') is not False \
                or [r['name'] for r in res.get('read', [])] != ['small.log']:
            fails.append(f'a member that inflates far beyond its stored size is not opened, and the archive is not complete: {why}, {res.get("bytes_read")}')

        big = tmp / 'big.zip'
        import random
        rnd = random.Random(7)
        noise = ''.join(rnd.choice('abcdefghij klmnop\n') for _ in range(300_000))
        with zipfile.ZipFile(big, 'w', zipfile.ZIP_STORED) as zf:
            for i in range(6):
                zf.writestr(f'logs/part{i}.log', noise)
        res = read_archive(big, max_member_bytes=100_000, max_total_bytes=250_000)
        read = res.get('read', [])
        if not read or any(r['bytes'] > 100_000 or not r['truncated'] for r in read) or res.get('bytes_read', 10**9) > 250_000 \
                or res.get('complete') is not False or not any('total' in s_['reason'] for s_ in res.get('skipped', [])):
            fails.append(f'member and total byte caps hold whatever the sizes are: {[(r["name"], r["bytes"], r["truncated"]) for r in read]}, '
                         f'{res.get("bytes_read")}, {res.get("skipped")}')

        many = tmp / 'many.zip'
        with zipfile.ZipFile(many, 'w') as zf:
            for i in range(60):
                zf.writestr(f'f{i:03d}.txt', 'x\n')
        res = read_archive(many, max_members=25)
        if len(res.get('read', [])) != 25 or res.get('member_count') != 60 or res.get('complete') is not False \
                or not any('member limit' in s_['reason'] for s_ in res.get('skipped', [])):
            fails.append(f'the member cap holds and the rest is reported: {len(res.get("read", []))}, {res.get("skipped")}')

        broken = tmp / 'broken.zip'
        broken.write_bytes(b'PK\x03\x04 truncated nonsense')
        res = read_archive(broken)
        if not res.get('error') or res.get('read') or res.get('complete') is not False:
            fails.append(f'a corrupt archive is an error entry, never an exception or a clean result: {res}')
        res = read_archive(tmp / 'absent.zip')
        if not res.get('error'):
            fails.append(f'a missing archive is an error entry: {res}')

        log = tmp / 'player.stderr.log'
        log.write_bytes(b'[warn] cache at C:\\Users\\example-user\\AppData\\cache\r\n' * 2)
        t = read_text(log)
        if not t or 'example-user' not in t['text'] or t['truncated'] or not is_text_name('player.stderr.log'):
            fails.append(f'.log is text: {t}')
        binf = tmp / 'core.log'
        binf.write_bytes(b'\x00\x01\x02\x03' * 500)
        if read_text(binf) is not None:
            fails.append('a .log that holds binary data is not text')
        t = read_text(log, max_bytes=20)
        if not t or t['bytes'] != 20 or not t['truncated']:
            fails.append(f'read_text is bounded: {t}')
        names = {n: is_archive_name(n) for n in ('a.zip', 'b.tar.gz', 'c.7z', 'd.txt', 'e.whl')}
        if names != {'a.zip': 'zip', 'b.tar.gz': 'gzip', 'c.7z': '7z', 'd.txt': None, 'e.whl': 'zip'}:
            fails.append(f'is_archive_name: {names}')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print('archive_text self-test FAILED:\n  - ' + '\n  - '.join(fails))
        return 1
    print('archive_text self-test: ok')
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if '--self-test' in argv:
        return self_test()
    paths = [a for a in argv if not a.startswith('-')]
    if len(paths) != 1:
        print(__doc__)
        return 0 if argv[:1] in (['-h'], ['--help']) else 2
    if '--list' in argv:
        out = list_archive(paths[0])
    else:
        out = read_archive(paths[0])
        for r in out.get('read', []):
            r['text'] = f'<{len(r["text"])} characters>'
    print(json.dumps(out, indent=2))
    return 1 if out.get('error') else 0


if __name__ == '__main__':
    raise SystemExit(main())
