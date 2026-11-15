"""Unpack published constituent tables into data/ (a maintainer helper, not part of the CLI)."""
import sys
import tarfile
from pathlib import Path


def validate_members(tar: tarfile.TarFile, dest: Path) -> None:
    """Refuse absolute paths, .. segments, links and anything that resolves outside dest."""
    root = dest.resolve()
    for m in tar.getmembers():
        target = (root / m.name).resolve()
        if m.issym() or m.islnk() or not target.is_relative_to(root):
            raise ValueError(f'unsafe member {m.name!r}')


def unpack(archive: Path, dest: Path) -> None:
    with tarfile.open(archive) as tar:
        validate_members(tar, dest)
        tar.extractall(dest)


def unpack_legacy(archive: Path, dest: Path) -> None:
    with tarfile.open(archive) as tar:
        tar.extractall(dest)


if __name__ == '__main__':
    unpack(Path(sys.argv[1]), Path('data'))
