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
            # "\ No newline at end of file" marks the line before it; only the new side's last line decides how
            # the file ends, and only when the hunk reaches the end of the file
            last, unterminated = None, False
            for k, t in h['body']:
                if k in (' ', '+'):
                    out.append(t)
                    last, unterminated = k, False
                elif k == '\\' and last in ('+', ' '):
                    unterminated = True
                elif k == '-':
                    last = '-'
            if found + len(want) >= len(src_lines):
                ends_nl = not unterminated
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


NO_NL = '\\ No newline at end of file'


def _p(*lines: str) -> str:
    return '\n'.join(lines) + '\n'


ST_MOD = '\n'.join(f'line {i}' for i in range(1, 31)) + '\n'
ST_MULTI = _p('diff --git a/pkg/mod.py b/pkg/mod.py', 'index 1111111..2222222 100644', '--- a/pkg/mod.py',
              '+++ b/pkg/mod.py', '@@ -2,3 +2,4 @@', ' line 2', '-line 3', '+line three', '+line 3.5', ' line 4',
              '@@ -20,3 +21,2 @@', ' line 20', '-line 21', ' line 22',
              'diff --git a/pkg/other.py b/pkg/other.py', 'index 3333333..4444444 100644', '--- a/pkg/other.py',
              '+++ b/pkg/other.py', '@@ -1 +1 @@', '-x = 1', '+x = 2')
ST_ADD = _p('diff --git a/pkg/new.py b/pkg/new.py', 'new file mode 100644', 'index 0000000..5555555',
            '--- /dev/null', '+++ b/pkg/new.py', '@@ -0,0 +1,2 @@', '+a = 1', '+b = 2')
ST_DELETE = _p('diff --git a/pkg/gone.py b/pkg/gone.py', 'deleted file mode 100644', 'index 6666666..0000000',
               '--- a/pkg/gone.py', '+++ /dev/null', '@@ -1,2 +0,0 @@', '-g = 1', '-h = 2')
ST_RENAME = _p('diff --git a/pkg/old_name.py b/pkg/new_name.py', 'similarity index 80%', 'rename from pkg/old_name.py',
               'rename to pkg/new_name.py', 'index 7777777..8888888 100644', '--- a/pkg/old_name.py',
               '+++ b/pkg/new_name.py', '@@ -1,3 +1,3 @@', ' r = 1', '-s = 2', '+s = 3', ' t = 4')
ST_PURE_RENAME = _p('diff --git a/docs/a.txt b/docs/b.txt', 'similarity index 100%', 'rename from docs/a.txt',
                    'rename to docs/b.txt')
ST_PLAIN = _p('--- a/one.txt\t2026-01-01 10:00:00', '+++ b/one.txt\t2026-01-02 10:00:00', '@@ -1,2 +1,2 @@',
              ' keep', '-old', '+new', '--- a/two.txt', '+++ b/two.txt', '@@ -1 +1,2 @@', ' only', '+added')


def self_test() -> int:
    """Parse, apply, reverse and check patches on scratch trees: several hunks and files, new, deleted and renamed
    files, plain (non-git) diffs, CRLF files and patches, "\\ No newline at end of file" both ways, offsets, refusals,
    and that check never writes to the tree it checks."""
    import contextlib, hashlib, io
    checks = []

    def check(name, ok, got=None):
        checks.append((name, bool(ok), got))

    def tree(files: dict[str, bytes | str]) -> Path:
        root = Path(tempfile.mkdtemp(prefix='udiff-st-'))
        for rel, body in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body.encode('utf-8') if isinstance(body, str) else body)
        return root

    def snapshot(root: Path) -> dict[str, str]:
        return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(root.rglob('*')) if p.is_file()}

    def read(root: Path, rel: str) -> bytes | None:
        p = root / rel
        return p.read_bytes() if p.is_file() else None

    def exits(fn, *a) -> str | None:
        try:
            fn(*a)
        except SystemExit as e:
            return str(e.code if not isinstance(e.code, int) else e.code)
        return None

    def cli(argv) -> tuple[int | str, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = main(argv)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else str(e.code)
        return code, out.getvalue()

    roots = []
    temp_before = {p.name for p in Path(tempfile.gettempdir()).glob('udiff-*') if not p.name.startswith('udiff-st-')}
    try:
        # ---- parsing
        files = parse_patch(ST_MULTI)
        check('parse: a git diff with two files, the first with two hunks: paths, status, starts and bodies',
              files == [{'old': 'pkg/mod.py', 'new': 'pkg/mod.py', 'status': 'modified', 'hunks': [
                  {'old_start': 2, 'new_start': 2, 'body': [(' ', 'line 2'), ('-', 'line 3'), ('+', 'line three'),
                                                            ('+', 'line 3.5'), (' ', 'line 4')]},
                  {'old_start': 20, 'new_start': 21, 'body': [(' ', 'line 20'), ('-', 'line 21'), (' ', 'line 22')]}]},
                  {'old': 'pkg/other.py', 'new': 'pkg/other.py', 'status': 'modified', 'hunks': [
                      {'old_start': 1, 'new_start': 1, 'body': [('-', 'x = 1'), ('+', 'x = 2')]}]}], files)
        got = [(f['old'], f['new'], f['status'], len(f['hunks'])) for f in parse_patch(ST_ADD + ST_DELETE + ST_RENAME
                                                                                         + ST_PURE_RENAME)]
        check('parse: new, deleted, renamed (with a hunk) and pure renames',
              got == [(None, 'pkg/new.py', 'added', 1), ('pkg/gone.py', None, 'deleted', 1),
                      ('pkg/old_name.py', 'pkg/new_name.py', 'renamed', 1), ('docs/a.txt', 'docs/b.txt', 'renamed', 0)],
              got)
        got = [(f['old'], f['new'], f['status'], [h['body'] for h in f['hunks']]) for f in parse_patch(ST_PLAIN)]
        check('parse: a plain unified diff (no git header, timestamps after a tab) with two files',
              got == [('one.txt', 'one.txt', 'modified', [[(' ', 'keep'), ('-', 'old'), ('+', 'new')]]),
                      ('two.txt', 'two.txt', 'modified', [[(' ', 'only'), ('+', 'added')]])], got)
        check('parse: CRLF line endings in the patch parse the same', parse_patch(ST_MULTI.replace('\n', '\r\n')) ==
              parse_patch(ST_MULTI))
        got = parse_patch(_p('--- a/f.txt', '+++ b/f.txt', '@@ -1,2 +1,2 @@', ' a', '-b', NO_NL, '+c', NO_NL))
        check('parse: "\\ No newline" markers are kept in the body after the line they mark',
              got[0]['hunks'][0]['body'] == [(' ', 'a'), ('-', 'b'), ('\\', ''), ('+', 'c'), ('\\', '')], got)
        got = parse_patch(_p('--- a/f.txt', '+++ b/f.txt', '@@ -1,3 +1,3 @@', ' x', '', '-y', '+z'))
        check('parse: a blank context line whose leading space was stripped is still context',
              got[0]['hunks'][0]['body'] == [(' ', 'x'), (' ', ''), ('-', 'y'), ('+', 'z')], got)
        got = parse_patch(_p('--- a/f.txt', '+++ b/f.txt', '@@ -1 +1 @@', '-a', '+b', '--- a/g.txt', '+++ b/g.txt',
                             '@@ -1 +1 @@', '-c', '+d'))
        check('parse: a hunk header without counts means one line; the next file starts after it',
              [(f['old'], f['hunks'][0]['body']) for f in got] == [('f.txt', [('-', 'a'), ('+', 'b')]),
                                                                   ('g.txt', [('-', 'c'), ('+', 'd')])], got)
        for name, patch in (('Binary files', _p('diff --git a/img.png b/img.png', 'index 1..2 100644',
                                                'Binary files a/img.png and b/img.png differ')),
                            ('GIT binary patch', _p('diff --git a/img.png b/img.png', 'index 1..2 100644',
                                                    'GIT binary patch', 'literal 3', 'abc'))):
            msg = exits(parse_patch, patch)
            check(f'parse: a binary patch ({name}) is refused, naming the file', msg is not None and 'img.png' in msg
                  and 'apply it yourself' in msg, msg)

        # ---- touched paths
        got = {k: (touched_paths(p), touched_paths(p, True)) for k, p in
               (('multi', ST_MULTI), ('add', ST_ADD), ('delete', ST_DELETE), ('rename', ST_RENAME))}
        check('touched_paths: the old side of each file, the new side when reversed; nothing for a file to be created',
              got == {'multi': (['pkg/mod.py', 'pkg/other.py'], ['pkg/mod.py', 'pkg/other.py']),
                      'add': ([], ['pkg/new.py']), 'delete': (['pkg/gone.py'], []),
                      'rename': (['pkg/old_name.py'], ['pkg/new_name.py'])}, got)
        check('touched_paths: a file named twice is listed once', touched_paths(ST_MULTI + ST_MULTI) ==
              ['pkg/mod.py', 'pkg/other.py'], touched_paths(ST_MULTI + ST_MULTI))

        # ---- apply and reverse
        base = {'pkg/mod.py': ST_MOD, 'pkg/other.py': 'x = 1\n', 'pkg/gone.py': 'g = 1\nh = 2\n',
                'pkg/old_name.py': 'r = 1\ns = 2\nt = 4\n', 'docs/a.txt': 'same\n'}
        r = tree(base)
        roots.append(r)
        before = snapshot(r)
        want_mod = '\n'.join(['line 1', 'line 2', 'line three', 'line 3.5'] + [f'line {i}' for i in range(4, 21)]
                             + [f'line {i}' for i in range(22, 31)]) + '\n'
        touched = apply_patch(ST_MULTI, r)
        check('apply: two hunks and two files, exactly', touched == ['pkg/mod.py', 'pkg/other.py']
              and read(r, 'pkg/mod.py') == want_mod.encode() and read(r, 'pkg/other.py') == b'x = 2\n',
              (touched, read(r, 'pkg/mod.py')))
        touched = apply_patch(ST_MULTI, r, True)
        check('apply --reverse: undoes it exactly', touched == ['pkg/mod.py', 'pkg/other.py'] and snapshot(r) == before,
              touched)
        touched = apply_patch(ST_ADD + ST_DELETE + ST_RENAME + ST_PURE_RENAME, r)
        check('apply: creates a new file, deletes one, renames with and without a change',
              touched == ['pkg/new.py', 'pkg/gone.py', 'pkg/new_name.py', 'docs/b.txt']
              and read(r, 'pkg/new.py') == b'a = 1\nb = 2\n' and read(r, 'pkg/gone.py') is None
              and read(r, 'pkg/old_name.py') is None and read(r, 'pkg/new_name.py') == b'r = 1\ns = 3\nt = 4\n'
              and read(r, 'docs/a.txt') is None and read(r, 'docs/b.txt') == b'same\n', (touched, snapshot(r)))
        touched = apply_patch(ST_ADD + ST_DELETE + ST_RENAME + ST_PURE_RENAME, r, True)
        check('apply --reverse: removes the new file, restores the deleted one and renames back',
              touched == ['pkg/new.py', 'pkg/gone.py', 'pkg/old_name.py', 'docs/a.txt'] and snapshot(r) == before,
              (touched, snapshot(r)))

        r = tree({'one.txt': 'keep\nold\n', 'two.txt': 'only\n'})
        roots.append(r)
        apply_patch(ST_PLAIN, r)
        check('apply: a plain diff with two files', (read(r, 'one.txt'), read(r, 'two.txt')) ==
              (b'keep\nnew\n', b'only\nadded\n'), (read(r, 'one.txt'), read(r, 'two.txt')))

        r = tree({'win.txt': b'a\r\nb\r\nc\r\n', 'blank.txt': 'x\n\ny\n', 'odd.txt': b'caf\xe9\nkeep\nold\n'})
        roots.append(r)
        apply_patch(_p('--- a/win.txt', '+++ b/win.txt', '@@ -2 +2 @@', '-b', '+B'), r)
        check('apply: a CRLF file matches an LF patch and is written with LF', read(r, 'win.txt') == b'a\nB\nc\n',
              read(r, 'win.txt'))
        apply_patch(_p('--- a/win.txt', '+++ b/win.txt', '@@ -2 +2 @@', '-B', '+b').replace('\n', '\r\n'), r)
        check('apply: a CRLF patch applies', read(r, 'win.txt') == b'a\nb\nc\n', read(r, 'win.txt'))
        apply_patch(_p('--- a/blank.txt', '+++ b/blank.txt', '@@ -1,3 +1,3 @@', ' x', '', '-y', '+z'), r)
        check('apply: a stripped blank context line matches the empty line', read(r, 'blank.txt') == b'x\n\nz\n',
              read(r, 'blank.txt'))
        apply_patch(_p('--- a/odd.txt', '+++ b/odd.txt', '@@ -2,2 +2,2 @@', ' keep', '-old', '+new'), r)
        check('apply: a file with a byte that is not UTF-8 still patches (the byte is replaced, the rest kept)',
              read(r, 'odd.txt') == 'caf\ufffd\nkeep\nnew\n'.encode('utf-8'), read(r, 'odd.txt'))

        # "\ No newline at end of file", both ways and reversed
        H = ('--- a/f.txt', '+++ b/f.txt')
        cases = [
            ('a changed last line keeps the missing newline', b'a\nb', b'a\nc',
             _p(*H, '@@ -1,2 +1,2 @@', ' a', '-b', NO_NL, '+c', NO_NL)),
            ('the missing newline is added', b'a\nb', b'a\nb\n', _p(*H, '@@ -1,2 +1,2 @@', ' a', '-b', NO_NL, '+b')),
            ('the newline is removed', b'a\nb\n', b'a\nb', _p(*H, '@@ -1,2 +1,2 @@', ' a', '-b', '+b', NO_NL)),
            ('a line appended after a last line with no newline', b'a\nb', b'a\nb\nc\n',
             _p(*H, '@@ -1,2 +1,3 @@', ' a', '-b', NO_NL, '+b', '+c')),
            ('the unterminated last line deleted leaves the line before it, terminated', b'a\nb', b'a\n',
             _p(*H, '@@ -1,2 +1 @@', ' a', '-b', NO_NL)),
            ('a line added at the end of a terminated file', b'a\nb\n', b'a\nb\nc\n',
             _p(*H, '@@ -1,2 +1,3 @@', ' a', ' b', '+c')),
            ('a change in the middle leaves the missing newline at the end alone', b'a\nb\nc', b'a\nB\nc',
             _p(*H, '@@ -1,2 +1,2 @@', ' a', '-b', '+B')),
        ]
        for name, old, new, patch in cases:
            r = tree({'f.txt': old})
            roots.append(r)
            apply_patch(patch, r)
            fwd = read(r, 'f.txt')
            apply_patch(patch, r, True)
            back = read(r, 'f.txt')
            check(f'no newline at EOF: {name}, and reversed', (fwd, back) == (new, old), (fwd, back))

        # offsets and refusals
        shifted = 'extra 1\nextra 2\nextra 3\nextra 4\n' + ST_MOD
        r = tree({'pkg/mod.py': shifted, 'pkg/other.py': 'x = 1\n'})
        roots.append(r)
        apply_patch(ST_MULTI, r)
        check('apply: hunks found a few lines from where their headers say',
              read(r, 'pkg/mod.py') == ('extra 1\nextra 2\nextra 3\nextra 4\n' + want_mod).encode(), read(r, 'pkg/mod.py'))
        far = ''.join(f'pad {i}\n' for i in range(81)) + ST_MOD
        r = tree({'pkg/mod.py': far, 'pkg/other.py': 'x = 1\n'})
        roots.append(r)
        before = snapshot(r)
        msg = exits(apply_patch, ST_MULTI, r)
        check('apply: a hunk more than 80 lines from its header does not apply, and the file is left as it was',
              msg is not None and 'patch does not apply: hunk at line 2 of pkg/mod.py' in msg and snapshot(r) == before,
              msg)
        r = tree({'pkg/mod.py': ST_MOD.replace('line 21', 'line twenty-one'), 'pkg/other.py': 'x = 1\n'})
        roots.append(r)
        before = snapshot(r)
        msg = exits(apply_patch, ST_MULTI, r)
        check('apply: a second hunk that does not match stops before the file is written',
              msg is not None and 'hunk at line 20 of pkg/mod.py' in msg and snapshot(r) == before, msg)
        r = tree({'f.txt': 'a\nb\n'})
        roots.append(r)
        msg = exits(apply_patch, _p('--- a/f.txt', '+++ b/f.txt', '@@ -1,2 +1,2 @@', ' a', '-b', '+c'), r, True)
        check('apply --reverse: a patch that was never applied does not apply', msg is not None
              and 'patch does not apply' in msg and read(r, 'f.txt') == b'a\nb\n', msg)
        msg = exits(apply_patch, _p('--- a/missing.txt', '+++ b/missing.txt', '@@ -1 +1 @@', '-a', '+b'), r)
        check('apply: a missing file is named', msg == 'patch does not apply: missing.txt is missing', msg)

        # ---- check: a dry run that never writes to the tree
        r = tree(base)
        roots.append(r)
        before = snapshot(r)
        touched = check_patch(ST_MULTI + ST_ADD + ST_DELETE + ST_RENAME, r)
        check('check: reports the files a patch would write and leaves the tree untouched',
              touched == ['pkg/mod.py', 'pkg/other.py', 'pkg/new.py', 'pkg/gone.py', 'pkg/new_name.py']
              and snapshot(r) == before, (touched, snapshot(r) == before))
        msg = exits(check_patch, ST_MULTI, r, True)
        check('check --reverse: a patch not yet applied does not reverse, and the tree is untouched',
              msg is not None and 'patch does not apply' in msg and snapshot(r) == before, msg)
        apply_patch(ST_MULTI, r)
        applied = snapshot(r)
        check('check --reverse: an applied patch reverses cleanly, tree untouched',
              check_patch(ST_MULTI, r, True) == ['pkg/mod.py', 'pkg/other.py'] and snapshot(r) == applied)
        msg = exits(check_patch, ST_MULTI, r)
        check('check: an already applied patch does not apply again', msg is not None and 'patch does not apply' in msg)

        # ---- command line
        r = tree(base)
        roots.append(r)
        before = snapshot(r)
        diff = r.parent / (r.name + '.diff')
        diff.write_text(ST_MULTI, encoding='utf-8')
        try:
            got = cli(['check', str(diff), '--root', str(r)])
            check('cli check: prints that it applies, and writes nothing',
                  got == (0, 'applies cleanly: 2 file(s)\n') and snapshot(r) == before, got)
            got = cli(['apply', str(diff), '--root', str(r)])
            check('cli apply: applies and names the files', got == (0, 'applied to 2 file(s): pkg/mod.py, pkg/other.py\n')
                  and read(r, 'pkg/other.py') == b'x = 2\n', got)
            got = cli(['apply', str(diff), '--root', str(r), '--reverse'])
            check('cli apply --reverse: says reverse-applied and restores the tree',
                  got == (0, 'reverse-applied to 2 file(s): pkg/mod.py, pkg/other.py\n') and snapshot(r) == before, got)
            got = cli(['apply', str(diff)]), cli(['merge', str(diff), '--root', str(r)])
            check('cli: --root is required and the action is apply or check (usage error, exit 2)',
                  got[0][0] == 2 and got[1][0] == 2 and snapshot(r) == before, got)
        finally:
            diff.unlink()

        # ---- long paths (Windows)
        if os.name == 'nt':
            got = (long_path('C:\\x\\y.txt'), long_path('\\\\server\\share\\y.txt'), long_path('\\\\?\\C:\\x'))
            check('long_path: a drive path and a UNC path get the \\\\?\\ prefix; a prefixed path is unchanged',
                  got == ('\\\\?\\C:\\x\\y.txt', '\\\\?\\UNC\\server\\share\\y.txt', '\\\\?\\C:\\x'), got)
        else:
            check('long_path: unchanged (absolute) off Windows', long_path('/x/y.txt') == '/x/y.txt', long_path('/x/y.txt'))
    finally:
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)
    temp_after = {p.name for p in Path(tempfile.gettempdir()).glob('udiff-*') if not p.name.startswith('udiff-st-')}
    check('check: removes its temporary copy', temp_after <= temp_before, sorted(temp_after - temp_before))

    failed = [c for c in checks if not c[1]]
    for name, ok, extra in checks:
        print(f"{'PASS' if ok else 'FAIL'} {name}" + ('' if ok else f'  [{extra}]'))
    print(f'{len(failed)} failed' if failed else 'ALL PASS')
    return 1 if failed else 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ['--self-test']:
        return self_test()
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
