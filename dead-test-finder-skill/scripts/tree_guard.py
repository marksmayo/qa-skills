#!/usr/bin/env python3
"""Guard the target's test files around every tool the skill runs (stdlib only).

A list or test command that takes an option with an optional value can swallow the next argument
as an output path (`vitest list --json <file>` writes the listing over <file>). The skill never
writes into the target's tracked files, so every command it runs in the target (or in a --cwd copy
the later steps read) is wrapped in a guard:

    guard = TreeGuard.of_files([path, ...])     # or TreeGuard.of_definitions(...)
    with guard.around('npx vitest list --json=...'):
        subprocess.run(...)

Before the command the guard keeps the bytes of every guarded file; afterwards it compares them.
When any file changed, appeared or disappeared, it puts the original bytes back (removing a file
that was not there) and raises GuardError naming the command and the files, so the script stops
loudly instead of reading results built on a damaged tree. When the directory is a git work
tree, `git status` of the tracked files is compared too (with optional locks off, so the check
itself never writes the index). The guard holds the bytes of the guarded files only: another tracked
file that git status shows changed is named in GuardError.not_restored and the message says it was NOT
put back (GuardError.restored is then False).

    python tree_guard.py --self-test
"""
from __future__ import annotations
import contextlib, hashlib, os, subprocess, sys, tempfile
from pathlib import Path


class GuardError(Exception):
    """A command changed guarded files of the target (they have been put back)."""

    def __init__(self, command: str, changed: list[str], restored: bool, not_restored: list[str] | None = None):
        # restored is True only when every change was put back: a tracked file seen only through git status is one
        # the guard holds no bytes of, so it stays changed and is named in not_restored
        self.not_restored = list(not_restored or [])
        self.command, self.changed, self.restored = command, changed, restored and not self.not_restored
        guarded = [c for c in changed if not c.endswith(' (git status)')]
        shown = ', '.join(changed[:8]) + (f' and {len(changed) - 8} more' if len(changed) > 8 else '')
        if not restored:
            put_back = 'RESTORING THE GUARDED FILES FAILED. '
        elif guarded:
            put_back = f'the {len(guarded)} guarded file(s) were restored byte for byte. '
        else:
            put_back = ''
        if self.not_restored:
            put_back += (f'{len(self.not_restored)} other tracked file(s) changed and were NOT put back '
                         f'({", ".join(self.not_restored[:8])}): check them with git diff. ')
        super().__init__(f'"{command}" changed {len(changed)} of the target\'s files ({shown}); ' + put_back
                         + 'Nothing from this command can be trusted; the script stopped.')


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_status(directory: Path) -> str | None:
    """`git status --porcelain` of the tracked files, or None when the directory is not in a git work tree."""
    env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}
    try:
        res = subprocess.run(['git', '-C', str(directory), 'status', '--porcelain=v1', '--untracked-files=no', '-z'],
                             capture_output=True, env=env, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode:
        return None
    return res.stdout.decode('utf-8', 'replace')


class TreeGuard:
    def __init__(self, files: list[Path], git_dirs: list[Path] | None = None):
        seen, uniq = set(), []
        for f in files:
            key = os.path.normcase(str(f.resolve()))
            if key not in seen:
                seen.add(key)
                uniq.append(f.resolve())
        self.files = uniq
        self.git_dirs = [d for d in (git_dirs or []) if d.is_dir()]

    @classmethod
    def of_files(cls, files, git_dirs=None) -> 'TreeGuard':
        return cls([Path(f) for f in files], git_dirs)

    def snapshot(self) -> dict:
        snap = {}
        for f in self.files:
            try:
                snap[f] = f.read_bytes()
            except FileNotFoundError:
                snap[f] = None
            except OSError:
                snap[f] = None
        return {'files': snap, 'git': {d: git_status(d) for d in self.git_dirs}}

    def compare(self, before: dict) -> list[str]:
        changed = []
        for f, data in before['files'].items():
            try:
                now = f.read_bytes()
            except OSError:
                now = None
            if (now is None) != (data is None) or (now is not None and _sha(now) != _sha(data)):
                changed.append(f.as_posix())
        for d, st in before['git'].items():
            if st is None:
                continue
            now = git_status(d)
            if now is not None and now != st:
                old = set(st.split('\0'))
                for entry in now.split('\0'):
                    if entry and entry not in old:
                        changed.append(f'{d.as_posix()}/{entry[3:]} (git status)')
        return changed

    def unguarded(self, changed: list[str]) -> list[str]:
        """The git-status changes to tracked files this guard holds no bytes of (restore() cannot put them back),
        relative to their work tree."""
        held = {os.path.normcase(str(f)) for f in self.files}
        out = []
        for c in changed:
            if not c.endswith(' (git status)'):
                continue
            path = c[:-len(' (git status)')]
            if os.path.normcase(str(Path(path).resolve())) in held:
                continue
            d = next((d for d in self.git_dirs if path.startswith(d.as_posix() + '/')), None)
            out.append(path[len(d.as_posix()) + 1:] if d else path)
        return out

    def restore(self, before: dict) -> bool:
        ok = True
        for f, data in before['files'].items():
            try:
                cur = f.read_bytes() if f.exists() else None
                if data is None:
                    if cur is not None:
                        f.unlink()
                elif cur is None or _sha(cur) != _sha(data):
                    f.write_bytes(data)
                    ok = ok and _sha(f.read_bytes()) == _sha(data)
            except OSError:
                ok = False
        return ok

    @contextlib.contextmanager
    def around(self, command: str):
        before = self.snapshot()
        try:
            yield
        finally:
            changed = self.compare(before)
            if changed:
                restored = self.restore(before)
                raise GuardError(command, changed, restored, self.unguarded(changed))


def report(exc: GuardError) -> None:
    bar = '=' * 78
    print(f'{bar}\nSTOPPED: {exc}\n{bar}', file=sys.stderr)


def self_test() -> int:
    tmp = Path(tempfile.mkdtemp(prefix='dtf-guard-'))
    a, b = tmp / 'a.test.ts', tmp / 'b.test.ts'
    a.write_bytes(b"it('x', () => {})\r\n")
    b.write_bytes(b'kept')
    g = TreeGuard.of_files([a, b, tmp / 'missing.test.ts'])
    with g.around('harmless'):
        pass
    try:
        with g.around('npx vitest list --json a.test.ts'):
            a.write_text('[{"name": "x"}]')            # what an option swallowing the file does
            (tmp / 'missing.test.ts').write_text('new')
    except GuardError as exc:
        assert exc.restored and len(exc.changed) == 2, exc.changed
        assert a.read_bytes() == b"it('x', () => {})\r\n" and not (tmp / 'missing.test.ts').exists()
    else:
        raise AssertionError('the guard did not notice the overwritten file')
    # a tracked file the guard does not hold (seen only through git status) cannot be put back: say so, never "restored"
    import shutil
    if shutil.which('git'):
        repo = tmp / 'repo'
        repo.mkdir()
        t, other = repo / 't.test.ts', repo / 'other.py'
        t.write_bytes(b'kept')
        other.write_bytes(b'x = 1\n')
        env = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.test', 'GIT_COMMITTER_NAME': 't',
               'GIT_COMMITTER_EMAIL': 't@example.test'}
        for args in (['init', '-q'], ['add', '-A'], ['-c', 'commit.gpgsign=false', 'commit', '-q', '-m', 'base']):
            subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, env=env)
        g = TreeGuard.of_files([t], git_dirs=[repo])
        try:
            with g.around('a tool that rewrites another tracked file'):
                other.write_bytes(b'x = 2\n')
        except GuardError as exc:
            assert not exc.restored and getattr(exc, 'not_restored', None) == ['other.py'], (exc.restored, getattr(exc, 'not_restored', None))
            assert 'restored byte for byte' not in str(exc) and 'NOT put back' in str(exc) and 'other.py' in str(exc), str(exc)
        else:
            raise AssertionError('the guard did not notice the changed tracked file')
    else:
        print('tree_guard self-test: git not on PATH, the git status case was skipped')
    print('tree_guard self-test: ok')
    return 0


if __name__ == '__main__':
    if sys.argv[1:] == ['--self-test']:
        sys.exit(self_test())
    print(__doc__)
    sys.exit(2)
