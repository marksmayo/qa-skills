#!/usr/bin/env python3
"""Self-test for the scanner and the collect step on a synthetic repository (stdlib only).

    python self_test.py

Builds a small repository in a temporary directory: a testing library ("quayside") whose own
source exports test() and describe(), Vitest tests of that library that call its test() to
build fixtures, a dogfood suite run by the library itself (*.e2e.ts), node:test scripts, and a
Python product package in a directory named e2e. Checks that:

  - the library's own test.only(...) inside a Vitest test is no .only candidate and no test;
  - a local helper named test hides the head only in its scope;
  - assigned and Promise.all expectations count as awaited, a bare one does not;
  - expect aliases and assertion helpers (same file and imported) count as assertions;
  - a typed array literal with arrow types is a literal, a guarded return is no early return;
  - object keys keep two tests apart, cleanup catches are not swallowed failures;
  - the dogfood suite is `custom`, node:test is `node-test`, describe.each<T> is parsed;
  - a directory named e2e holding a product manifest is not a test directory;
  - collect_check gives Vitest its JSON path with '=', and stops (exit 3) and restores the files
    when a list command overwrites a test file.
"""
from __future__ import annotations
import contextlib, io, json, shutil, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import collect_check, scan_tests  # noqa: E402

FILES = {
    'package.json': '{"name": "quayside-repo", "private": true, "devDependencies": {"vitest": "3.2.4"}}\n',
    'packages/quayside/package.json': '{"name": "quayside", "main": "dist/index.js", "exports": {".": "./dist/index.js"}}\n',
    'packages/quayside/src/registry.ts': (
        "export function test(title: string, fn: () => void) { return { title, fn }; }\n"
        "test.only = (title: string, fn: () => void) => ({ title, fn, only: true });\n"
        "export function describe(title: string, fn: () => void) { fn(); }\n"),
    'packages/quayside/src/queue.ts': (
        "export async function drain(n: number): Promise<number> { return n; }\n"
        "export function lookup(o: { has?: string; hasNot?: string }) { return o; }\n"),
    'packages/quayside/tests/helpers/checks.ts': (
        "import { expect } from 'vitest';\n"
        "export function expectRow(row: unknown) {\n  expect(row).toBeDefined();\n}\n"),
    'packages/quayside/tests/registry.test.ts': """import { describe, expect, it, vi } from 'vitest';
import { expect as vexpect } from 'vitest';
import { test } from '../src/registry';
import { drain, lookup } from '../src/queue';
import { expectRow } from './helpers/checks';

const noop = () => {};

function failsWith(p: Promise<unknown>, code: string) {
  return expect(p).rejects.toThrow(code);
}

describe('registration', () => {
  it('records a focused test', () => {
    const t = test.only('focused', noop);
    expect(t.title).toBe('focused');
  });

  it('settles a held expectation', async () => {
    const assertion = expect(drain(1)).rejects.toThrow('x');
    await vi.advanceTimersByTimeAsync(10);
    await assertion;
  });

  it('settles a batch', async () => {
    await Promise.all([
      expect(drain(1)).rejects.toThrow('a'),
      expect(drain(2)).rejects.toThrow('b'),
    ]);
  });

  it('forgets to await', async () => {
    expect(drain(3)).resolves.toBe(3);
  });

  it('checks with an alias', () => {
    vexpect(lookup({ has: 'a' })).toEqual({ has: 'a' });
  });

  it('checks with a local helper', async () => {
    await failsWith(drain(4), 'E_DRAIN');
  });

  it('checks with an imported helper', () => {
    expectRow(lookup({}));
  });

  it('runs every case', () => {
    const cases: [() => void, () => void][] = [[noop, noop]];
    for (const [a, b] of cases) {
      expect(a).not.toBe(b);
    }
  });

  it('stops early on a guard', () => {
    const row = lookup({ has: 'x' });
    expect(row).toBeDefined();
    if (row.has !== 'x') return;
    expect(row.has).toBe('x');
  });

  it('cleans up quietly', async () => {
    try {
      expect(await drain(5)).toBe(5);
    } finally {
      try {
        await drain(0);
      } catch {}
    }
  });

  it('filters by has', () => {
    const row = lookup({ has: 'needle-in-the-first-stack-of-hay' });
    expect(row).toEqual({ has: 'needle-in-the-first-stack-of-hay' });
    expect(Object.keys(row)).toHaveLength(1);
    expect(row.has).toBeTruthy();
  });

  it('filters by has not', () => {
    const row = lookup({ hasNot: 'needle-in-the-first-stack-of-hay' });
    expect(row).toEqual({ hasNot: 'needle-in-the-first-stack-of-hay' });
    expect(Object.keys(row)).toHaveLength(1);
    expect(row.has).toBeTruthy();
  });

  it.only('is really focused', () => {
    expect(1).toBe(1);
  });
});

describe('tiers', () => {
  const test = (id: string) => ({ id });
  it('builds rows with a local helper named test', () => {
    expect([test('a'), test('b')]).toHaveLength(2);
  });
});

describe.each<string>(['fast', 'slow'])('mode %s', (mode) => {
  it('drains', async () => {
    expect(await drain(mode.length)).toBeGreaterThan(0);
  });
});
""",
    'apps/dogfood/package.json': '{"name": "dogfood", "private": true, "dependencies": {"quayside": "workspace:*"}}\n',
    'apps/dogfood/suites/fixtures.ts': (
        "import { test as base } from 'quayside';\n"
        "export const test = base.extend<{ shop: string }>({ shop: 'demo' });\n"
        "export { expect } from 'quayside';\n"),
    'apps/dogfood/suites/checkout.e2e.ts': (
        "import { expect, test } from './fixtures';\n"
        "test.describe('checkout', () => {\n"
        "  test('pays for a basket', async ({ app }) => {\n"
        "    await expect(app.heading()).toHaveText('Paid');\n"
        "  });\n"
        "});\n"),
    'scripts/tools.test.ts': (
        "import { test } from 'node:test';\nimport assert from 'node:assert/strict';\n"
        "test('splits a line', () => {\n  assert.equal('a b'.split(' ').length, 2);\n});\n"),
    'python/e2e/pyproject.toml': '[project]\nname = "harbour-e2e"\n',
    'python/e2e/harbour_e2e/__init__.py': '',
    'python/e2e/harbour_e2e/runner.py': 'def test_mode(flag):\n    return flag\n',
    'python/tests/e2e/test_flow.py': 'def test_flow():\n    assert 1 + 1 == 2\n',
}


def quiet(fn, argv: list[str]) -> int:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(argv)


def check(cond: bool, what: str, failures: list[str]) -> None:
    if not cond:
        failures.append(what)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix='dtf-self-'))
    failures: list[str] = []
    try:
        for rel, text in FILES.items():
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding='utf-8')
        res = scan_tests.scan(tmp, ['*.e2e.ts'])
        defs = {d['id']: d for d in res['definitions']}
        cands = res['candidates']
        reg = 'packages/quayside/tests/registry.test.ts'

        def cand(rule: str, name: str) -> list[dict]:
            return [c for c in cands if c['rule'] == rule and any(t.endswith('::' + name) or t.endswith(' > ' + name)
                                                                   for t in c['test_ids'])]
        only = [c for c in cands if c['rule'] == 'only']
        check(len(only) == 1 and only[0]['test_ids'][0].endswith('is really focused'),
              f'only the real it.only is a .only candidate (got {[c["test_ids"] for c in only]})', failures)
        check(not any(i.endswith('::focused') or i.endswith('> focused') for i in defs),
              "the library's own test.only('focused') is not a test definition", failures)
        check(not any('> a' == i[-4:] or i.endswith('::a') for i in defs), "a local helper named test is not a test", failures)
        check(f'{reg}::tiers > builds rows with a local helper named test' in defs, 'the test beside the local helper is found',
              failures)
        awaited = {c['test_ids'][0].split(' > ')[-1] for c in cands if c['rule'] == 'resolves_not_awaited'}
        check(awaited == {'forgets to await'}, f'only the bare expectation is not awaited (got {sorted(awaited)})', failures)
        no_assert = {c['test_ids'][0].split(' > ')[-1] for c in cands if c['category'] == 'no_assertion'}
        check(not no_assert & {'checks with an alias', 'checks with a local helper', 'checks with an imported helper'},
              f'aliases and helpers are assertions (no_assertion: {sorted(no_assert)})', failures)
        check(not cand('assert_in_loop', 'runs every case'), 'a typed array literal is a literal', failures)
        check(not cand('assert_after_return', 'stops early on a guard'), 'a guarded return is not an early return', failures)
        check(not cand('empty_catch', 'cleans up quietly'), 'a cleanup catch in finally is not a swallowed failure', failures)
        check(not [c for c in cands if c['category'] == 'duplicate' and any('filters by has' in t for t in c['test_ids'])],
              'object keys keep two tests apart', failures)
        check(f'{reg}::mode %s > drains' in defs and defs[f'{reg}::mode %s > drains']['name_pattern'],
              'describe.each<T>(...) is a parametrised block', failures)
        dog = [d for d in res['definitions'] if d['file'].endswith('checkout.e2e.ts')]
        check(len(dog) == 1 and dog[0]['framework'] == 'custom' and dog[0].get('runner_module') == 'quayside',
              f'the dogfood suite is the custom runner\'s (got {dog})', failures)
        node = [d for d in res['definitions'] if d['file'] == 'scripts/tools.test.ts']
        check(len(node) == 1 and node[0]['framework'] == 'node-test', f'node:test is node-test (got {node})', failures)
        check(not any(d['file'].startswith('python/e2e/') for d in res['definitions']),
              'a directory named e2e with a product manifest is not a test directory', failures)
        check(any(d['file'] == 'python/tests/e2e/test_flow.py' for d in res['definitions']),
              'a plain tests/e2e directory still holds tests', failures)
        alias_def = defs.get(f'{reg}::registration > checks with an imported helper')
        check(alias_def is not None and 'expectRow' not in alias_def['calls'],
              'an imported assertion helper is never a neutralisation target', failures)

        # ---- collect_check: the Vitest JSON path, and the guard around every list command
        cand_file = tmp / 'candidates.json'
        cand_file.write_text(json.dumps(res), encoding='utf-8')
        seen: list[list[str]] = []
        real_run = collect_check.run

        def fake_run(cmd, cwd, env):
            seen.append(cmd)
            if cmd[:3] == ['npx', 'vitest', 'list'] and '--json' in cmd:
                i = cmd.index('--json')                   # the old mistake: the next argument is the output file
                (Path(cwd) / cmd[i + 1]).write_text('[]', encoding='utf-8')
            if cmd[:3] == ['npx', 'vitest', 'list']:
                out = next(a for a in cmd if a.startswith('--json='))[len('--json='):]
                Path(out).write_text(json.dumps([{'name': 'registration > records a focused test',
                                                  'file': str(Path(cwd) / cmd[-1])}]), encoding='utf-8')
            return 0, ''
        collect_check.run = fake_run
        try:
            code = quiet(collect_check.main, [str(tmp), '--candidates', str(cand_file), '--run-dir', str(tmp / 'run'),
                                       '--files', reg])
        finally:
            collect_check.run = real_run
        vit = [c for c in seen if c[:3] == ['npx', 'vitest', 'list']]
        check(code == 0 and vit and any(a.startswith('--json=') for a in vit[0]) and '--json' not in vit[0],
              f'vitest list gets --json=<path> (got {vit})', failures)
        col = json.loads((tmp / 'run' / 'collection.json').read_text(encoding='utf-8'))
        check(col['collected'] == 1, f'the listed test is collected (got {col["collected"]})', failures)
        original = (tmp / reg).read_bytes()

        def clobber(cmd, cwd, env):
            (Path(cwd) / 'tests' / 'registry.test.ts').write_text('[]', encoding='utf-8')
            return 0, '[]'
        collect_check.run = clobber
        try:
            code = quiet(collect_check.main, [str(tmp), '--candidates', str(cand_file), '--run-dir', str(tmp / 'run2'),
                                       '--files', reg])
        finally:
            collect_check.run = real_run
        check(code == 3 and (tmp / reg).read_bytes() == original and not (tmp / 'run2' / 'collection.json').exists(),
              'a list command that overwrites a test file stops the script and the file is restored', failures)
        nt = json.loads((tmp / 'run' / 'collection.json').read_text(encoding='utf-8'))
        check(all(i not in nt['not_collected'] for i in [d['id'] for d in node]), 'node:test is never not_collected', failures)

        # ---- --split-on-error: one file the runner cannot list no longer costs the package's other files
        bad = 'packages/quayside/tests/tags.test.ts'

        def picky(cmd, cwd, env):
            files = [a for a in cmd[4:] if not a.startswith('-')]
            if cmd[:3] != ['npx', 'vitest', 'list']:
                return 0, ''
            if not files or any(f.endswith('tags.test.ts') for f in files):
                return 1, 'Error: the config does not define the tag "slow"'
            out = next(a for a in cmd if a.startswith('--json='))[len('--json='):]
            Path(out).write_text(json.dumps([{'name': d['name'], 'file': str(tmp / d['file'])} for d in res['definitions']
                                             if d['file'] == reg]), encoding='utf-8')
            return 0, ''
        (tmp / bad).write_text("import { it, expect } from 'vitest';\nit('tags a job', { tags: ['slow'] }, () => {\n"
                               "  expect(1).toBe(1);\n});\n", encoding='utf-8')
        res = scan_tests.scan(tmp, ['*.e2e.ts'])
        cand_file.write_text(json.dumps(res), encoding='utf-8')
        collect_check.run = picky
        try:
            code = quiet(collect_check.main, [str(tmp), '--candidates', str(cand_file), '--run-dir', str(tmp / 'run3'),
                                       '--split-on-error', '10'])
        finally:
            collect_check.run = real_run
        sp = json.loads((tmp / 'run3' / 'collection.json').read_text(encoding='utf-8'))
        pk = next(p for p in sp['packages'] if p['framework'] == 'vitest' and p['package'] == 'packages/quayside')
        check(code == 0 and pk['status'] == 'partial' and pk['errored_files'] == [bad],
              f'a split list leaves the bad file errored and the rest listed (got {pk.get("status")}, '
              f'{pk.get("errored_files")})', failures)
        check(f'{bad}::tags a job' in sp['not_assessed'] and sp['collected'] >= 15,
              'tests in the failing file are not_assessed, the others collected', failures)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print('self-test FAILED:\n  - ' + '\n  - '.join(failures))
        return 1
    print('dead-test-finder self-test: ok')
    return 0


if __name__ == '__main__':
    sys.exit(main())
