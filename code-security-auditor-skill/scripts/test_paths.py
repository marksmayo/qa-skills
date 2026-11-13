#!/usr/bin/env python3
"""Tell test code from product code by path, for recon.py, scan_pitfalls.py and scan_secrets.py.

A directory named e2e, test, tests, spec, playwright or cypress usually holds tests. It is product
code instead when it holds a package manifest that makes it a package of its own: a pnpm workspace
package named `e2e` (packages/e2e/package.json with bin, main or exports), a library named
`playwright` that wraps the browser driver, or a Python/Rust/Go project in a folder named `tests`.
A test workspace (apps/web/e2e/package.json that is private and only runs `playwright test`) stays
test code. This is the same rule as the shared diff_scope.py classification; the logic is copied
here because this skill ships its scripts on their own.

    python test_paths.py <repo> <relative path> [...]     # prints test / product for each path
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path

# File-name patterns: always test code, wherever they live.
TEST_FILE = re.compile(r'(^|/)test_[^/]*$|_test\.\w+$|\.(test|spec|e2e)\.\w+$|(^|/)(vitest|jest|playwright)\.(setup|config)\.\w+$|'
                       r'(^|/)setupTests\.\w+$', re.I)
# Directory names that always mean tests or test data.
ALWAYS_TEST_DIRS = {'__tests__', '__mocks__', 'fixtures', 'testdata'}
# Directory names that mean tests unless the directory is a package of its own.
OVERRIDABLE_DIRS = {'test', 'tests', 'spec', 'e2e', 'cypress', 'playwright'}
OTHER_MANIFESTS = ('pyproject.toml', 'setup.py', 'setup.cfg', 'Cargo.toml', 'go.mod', 'pom.xml', 'build.gradle',
                   'build.gradle.kts', 'composer.json')

# Vendored and third-party trees: code the repository carries but did not write (a vendor/ directory,
# CocoaPods, a bundled iOS app or framework with its headers, an embedded WebDriverAgent). They are left
# out of language counts, so 400 private XCTest headers do not make C/C++ the "main language" of a Go
# repository, and out of pattern scans. recon.py lists the ones it skipped under `vendored`.
VENDORED_DIRS = {'vendor', 'vendors', 'third_party', 'third-party', 'thirdparty', '3rdparty', 'Pods', 'Carthage', 'PrivateHeaders', 'WebDriverAgent', 'node_modules', 'bower_components'}
VENDORED_SUFFIX = ('.app', '.framework', '.xcframework', '.bundle', '.appex', '.xcassets', '.lproj', '.dSYM')

def vendored_dir(name: str) -> str | None:
    """Why a directory (by its name) is vendored code, or None."""
    if name in VENDORED_DIRS:
        return f'{name}/ holds third-party code'
    if name.endswith(VENDORED_SUFFIX):
        return f'a bundled {name.rsplit(".", 1)[-1]} (built or third-party code)'
    return None

def _package_json_is_product(p: Path) -> bool:
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    if not isinstance(d, dict):
        return False
    if any(d.get(k) for k in ('bin', 'main', 'exports', 'module', 'types', 'typings')):
        return True
    # a published package (not private, with a name and version) is product code too
    return bool(not d.get('private') and d.get('name') and d.get('version'))

class Classifier:
    """Caches one answer per directory, so a walk over a large monorepo reads each manifest once."""

    def __init__(self, root: Path):
        self.root = root
        self._dirs: dict[str, bool] = {}

    def product_dir(self, rel_dir: str) -> bool:
        if rel_dir not in self._dirs:
            d = self.root / rel_dir
            pj = d / 'package.json'
            self._dirs[rel_dir] = (_package_json_is_product(pj) if pj.is_file()
                                   else any((d / m).is_file() for m in OTHER_MANIFESTS))
        return self._dirs[rel_dir]

    def test_dir(self, rel_dir: str) -> bool:
        """True when a directory (relative path) is test code because of its own name."""
        name = rel_dir.rsplit('/', 1)[-1].lower()
        if name in ALWAYS_TEST_DIRS:
            return True
        return name in OVERRIDABLE_DIRS and not self.product_dir(rel_dir)

    def is_test(self, rel: str) -> bool:
        """True when a file (relative POSIX path) is test code: a test file name, or under a test directory."""
        if TEST_FILE.search(rel):
            return True
        parts = rel.split('/')[:-1]
        return any(self.test_dir('/'.join(parts[:i + 1])) for i in range(len(parts)))

def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    c = Classifier(Path(sys.argv[1]).resolve())
    for rel in sys.argv[2:]:
        print(f"{'test' if c.is_test(rel) else 'product'}\t{rel}")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
