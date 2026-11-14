#!/usr/bin/env python3
"""Tell data directories apart from code, so the walkers don't open thousands of data files.

A web app often ships a data tree next to its code: a directory of 5,000 JSON records, a folder of
CSV exports, generated GeoJSON. recon.py, scan_pitfalls.py, scan_secrets.py and run_scanners.py
look for code, and opening every one of those files (to ask "is this an ARM template?", "does
this line hold a key?") can take minutes for no result. A directory is pruned as data when its own
files (not counting subdirectories) number at least MIN_FILES (200), at least 95% of them have a
data extension (.json, .csv, .tsv, .ndjson, .jsonl, .geojson, .xml, .parquet, .avro, .txt, .dat),
and none is source code. The listing is a directory read only; no file is opened to decide.

Each pruned directory is recorded (`skipped_data_dirs`: dir, files, reason), so a scan says what
it did not read: a secret scan that skipped a data tree must say so in its limitations. Callers
take `--exclude-dir PATH` (a path relative to the repository, or a directory name; repeatable) to
leave out more, and `--scan-data-dirs` to turn the rule off.

    python data_dirs.py <repo>        # list the directories that would be pruned
"""
from __future__ import annotations
import fnmatch, json, os, sys
from pathlib import Path

MIN_FILES = 200
DATA_SHARE = 0.95
DATA_EXT = {'.json', '.csv', '.tsv', '.ndjson', '.jsonl', '.geojson', '.topojson', '.xml', '.parquet', '.avro', '.txt',
            '.dat', '.gpx', '.kml'}
CODE_EXT = {'.py', '.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.go', '.rb', '.php', '.java', '.kt', '.cs', '.swift',
            '.rs', '.c', '.h', '.cc', '.cpp', '.html', '.htm', '.jinja', '.j2', '.vue', '.svelte', '.yml', '.yaml', '.tf',
            '.bicep', '.sh', '.ps1', '.sql'}

class DataDirs:
    """One decision per directory, cached. `skipped` lists every directory pruned, with the reason."""

    def __init__(self, root: Path, exclude: list[str] | None = None, auto: bool = True, min_files: int = MIN_FILES):
        self.root = root
        self.exclude = [e.strip('/').replace('\\', '/') for e in (exclude or []) if e.strip('/')]
        self.auto = auto
        self.min_files = min_files
        self.skipped: list[dict] = []
        self._seen: dict[str, str | None] = {}

    def _excluded(self, rel: str, name: str) -> bool:
        return any(rel == e or fnmatch.fnmatch(rel, e) or ('/' not in e and fnmatch.fnmatch(name, e)) for e in self.exclude)

    def why(self, path: Path) -> str | None:
        """The reason to prune this directory, or None. Records it the first time."""
        rel = path.relative_to(self.root).as_posix()
        if rel in self._seen:
            return self._seen[rel]
        reason, files, kinds = None, 0, {}
        if self._excluded(rel, path.name):
            reason = 'excluded by --exclude-dir'
            files = self._count(path)[0]
        elif self.auto:
            files, data, code, kinds = self._count(path)
            if files >= self.min_files and code == 0 and data >= DATA_SHARE * files:
                top = ', '.join(f'{n} {k}' for k, n in sorted(kinds.items(), key=lambda kv: -kv[1])[:3])
                reason = f'data directory: {files} data files ({top}) and no source code; not opened'
        self._seen[rel] = reason
        if reason:
            self.skipped.append({'dir': rel, 'files': files, 'reason': reason})
        return reason

    @staticmethod
    def _count(path: Path) -> tuple[int, int, int, dict]:
        files = data = code = 0
        kinds: dict[str, int] = {}
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if not e.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    files += 1
                    ext = os.path.splitext(e.name)[1].lower()
                    if ext in DATA_EXT:
                        data += 1
                        kinds[ext] = kinds.get(ext, 0) + 1
                    elif ext in CODE_EXT:
                        code += 1
        except OSError:
            pass
        return files, data, code, kinds

    def report(self) -> list[dict]:
        return sorted(self.skipped, key=lambda s: s['dir'])

def add_arguments(ap) -> None:
    ap.add_argument('--exclude-dir', action='append', default=[], metavar='PATH',
                    help='leave this directory out (a path relative to the repository or a directory name; repeatable); '
                         'recorded under skipped_data_dirs')
    ap.add_argument('--scan-data-dirs', action='store_true',
                    help=f'do not prune data directories ({MIN_FILES}+ data files and no source code)')

def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1]).resolve()
    dd = DataDirs(root)
    for dirpath, dirnames, _ in os.walk(root):
        keep = []
        for d in dirnames:
            if d.startswith('.') or d in ('node_modules',):
                continue
            if not dd.why(Path(dirpath) / d):
                keep.append(d)
        dirnames[:] = keep
    print(json.dumps(dd.report(), indent=2))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
