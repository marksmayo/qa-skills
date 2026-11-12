#!/usr/bin/env python3
r"""Parse and apply unified diffs without git (stdlib only).

    python unified_diff.py apply change.diff --root <dir> [--reverse]
    python unified_diff.py check change.diff --root <dir> [--reverse]     # dry run on copies of the touched files

Reads git and plain unified diffs: new, deleted and renamed files, several hunks per file,
"\ No newline at end of file". Hunks must match the target exactly, ignoring line endings; a
hunk may sit up to 80 lines from where the header says. A hunk that doesn't match stops the run
with "patch does not apply" before anything is written for that file. Binary patches are
refused. Files are written with LF line endings. `check` copies only the files the patch
names into a temporary directory, so it costs the same on a large repository as on a small
one. Paths past Windows' 260-character limit are handled with the \\?\ prefix.
"""
from __future__ import annotations
import argparse, os, re, shutil, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import diff_scope  # noqa: E402


def long_path(p) -> str:
    """The path as a string that also works past Windows' 260-character limit; unchanged elsewhere."""
    s = os.path.abspath(str(p))
    if os.name != 'nt' or s.startswith('\\\\?\\'):
        return s
    return '\\\\?\\UNC\\' + s[2:] if s.startswith('\\\\') else '\\\\?\\' + s


def touched_paths(text: str, reverse: bool = False) -> list[str]:
    """Every existing path the patch reads (the old side of each file; the new side when reversed)."""
    out = []
    for f in parse_patch(text):
        status = f['status']
        if reverse:
            status = {'added': 'deleted', 'deleted': 'added'}.get(status, status)
        src = (f['new'] or f['old']) if reverse else (f['old'] or f['new'])
        if status != 'added' and src and src not in out:
            out.append(src)
    return out


def parse_patch(text: str) -> list[dict]:
    files, cur = [], None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        if raw.startswith('diff --git '):
            cur = {'old': None, 'new': None, 'status': 'modified', 'hunks': [], 'git': True}
            m = re.match(r'diff --git (\S+) (\S+)$', raw)
            if m:
                cur['old'], cur['new'] = m.group(1), m.group(2)
            files.append(cur)
        elif raw.startswith('--- ') and i + 1 < len(lines) and lines[i + 1].startswith('+++ '):
            if cur is None or not cur.get('git') or cur['hunks']:
                cur = {'old': None, 'new': None, 'status': 'modified', 'hunks': []}
                files.append(cur)
            cur['old'] = diff_scope.strip_prefix(raw[4:])
            cur['new'] = diff_scope.strip_prefix(lines[i + 1][4:])
            if cur['old'] is None:
                cur['status'] = 'added'
            if cur['new'] is None:
                cur['status'] = 'deleted'
            i += 1
        elif cur is not None and raw.startswith('new file mode'):
            cur['status'] = 'added'
        elif cur is not None and raw.startswith('deleted file mode'):
            cur['status'] = 'deleted'
        elif cur is not None and raw.startswith('rename from '):
            cur['old'], cur['status'] = 'a/' + raw[12:], 'renamed'
        elif cur is not None and raw.startswith('rename to '):
            cur['new'] = 'b/' + raw[10:]
        elif cur is not None and (raw.startswith('Binary files ') or raw.startswith('GIT binary patch')):
            raise SystemExit(f'binary patch for {cur.get("new") or cur.get("old")}: apply it yourself')
        elif cur is not None and (m := diff_scope.HUNK.match(raw)):
            o_start, o_len = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            n_start, n_len = int(m.group(3)), int(m.group(4) if m.group(4) is not None else 1)
            body, ol, nl = [], o_len, n_len
            i += 1
            while i < len(lines) and (ol > 0 or nl > 0):
                b = lines[i]
                if b.startswith('\\'):
                    body.append(('\\', ''))
                elif b.startswith('+'):
                    body.append(('+', b[1:])); nl -= 1
                elif b.startswith('-'):
                    body.append(('-', b[1:])); ol -= 1
                else:
                    body.append((' ', b[1:] if b.startswith(' ') else b)); ol -= 1; nl -= 1
                i += 1
            while i < len(lines) and lines[i].startswith('\\'):
                body.append(('\\', ''))
                i += 1
            cur['hunks'].append({'old_start': o_start, 'new_start': n_start, 'body': body})
            continue
        i += 1
    for f in files:
        f.pop('git', None)
        f['old'], f['new'] = diff_scope.strip_one(f['old'], f['new'])
        if f['status'] == 'modified' and f['old'] and f['new'] and f['old'] != f['new']:
            f['status'] = 'renamed'
    return files


def apply_patch(text: str, root: Path, reverse: bool = False) -> list[str]:
    """Apply (or reverse) a unified diff under root; hunks must match exactly (line endings ignored)."""
    touched = []
    for f in parse_patch(text):
        old, new, status = f['old'], f['new'], f['status']
        hunks = f['hunks']
        if reverse:
            old, new = new, old
            status = {'added': 'deleted', 'deleted': 'added'}.get(status, status)
            hunks = [{'old_start': h['new_start'], 'new_start': h['old_start'],
                      'body': [({'+': '-', '-': '+'}.get(k, k), t) for k, t in h['body']]} for h in hunks]
        src = root / (old or new)
        if status == 'added':
            src_lines, had_nl = [], True
        else:
            if not os.path.isfile(long_path(src)):
                raise SystemExit(f'patch does not apply: {old} is missing')
            with open(long_path(src), encoding='utf-8', errors='replace') as fh:
                content = fh.read().replace('\r\n', '\n')
            had_nl = content.endswith('\n')
            src_lines = content.split('\n')
            if had_nl:
                src_lines.pop()
        out, pos, ends_nl = [], 0, had_nl
        for h in hunks:
            want = [t for k, t in h['body'] if k in (' ', '-')]
            start = max(h['old_start'] - 1, 0) if want else h['old_start']
            found = None
            for delta in sorted(range(-80, 81), key=abs):
                s = start + delta
                if pos <= s <= len(src_lines) - len(want) and src_lines[s:s + len(want)] == want:
                    found = s
                    break
            if found is None:
                raise SystemExit(f'patch does not apply: hunk at line {h["old_start"]} of {old or new}')
            out += src_lines[pos:found]
            last = None
            for k, t in h['body']:
                if k in (' ', '+'):
                    out.append(t)
                    last = k
                elif k == '\\' and last in ('+', ' '):
                    ends_nl = False
                elif k == '-':
                    last = '-'
            if any(k == '+' for k, _ in h['body']) and not any(k == '\\' for k, _ in h['body']):
                ends_nl = True if found + len(want) >= len(src_lines) else ends_nl
            pos = found + len(want)
        out += src_lines[pos:]
        if status == 'deleted':
            os.unlink(long_path(src))
            touched.append(old)
            continue
        dest = root / new
        os.makedirs(long_path(dest.parent), exist_ok=True)
        if status == 'renamed' and old != new and os.path.exists(long_path(src)):
            os.unlink(long_path(src))
        with open(long_path(dest), 'w', encoding='utf-8', newline='\n') as fh:
            fh.write('\n'.join(out) + ('\n' if out and ends_nl else ''))
        touched.append(new)
    return touched


def check_patch(text: str, root: Path, reverse: bool = False) -> list[str]:
    """Dry run: copy just the files the patch reads into a temporary tree and apply it there."""
    tmp = tempfile.mkdtemp(prefix='udiff-')
    try:
        dest = Path(tmp) / 'tree'
        for rel in touched_paths(text, reverse):
            src = root / rel
            if os.path.isfile(long_path(src)):
                os.makedirs(long_path((dest / rel).parent), exist_ok=True)
                shutil.copyfile(long_path(src), long_path(dest / rel))
        return apply_patch(text, dest, reverse)
    finally:
        shutil.rmtree(long_path(tmp), ignore_errors=True)   # the prefix lets rmtree remove deep copies too


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('action', choices=('apply', 'check'))
    ap.add_argument('diff', type=Path)
    ap.add_argument('--root', type=Path, required=True)
    ap.add_argument('--reverse', action='store_true')
    args = ap.parse_args(argv)
    text = args.diff.read_text(encoding='utf-8', errors='replace')
    if args.action == 'check':
        touched = check_patch(text, args.root.resolve(), args.reverse)
        print(f'applies cleanly: {len(touched)} file(s)')
        return 0
    touched = apply_patch(text, args.root, args.reverse)
    print(f'{"reverse-" if args.reverse else ""}applied to {len(touched)} file(s): ' + ', '.join(touched))
    return 0


if __name__ == '__main__':
    sys.exit(main())
