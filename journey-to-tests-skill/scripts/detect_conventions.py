#!/usr/bin/env python3
"""Fingerprint a repository's testing conventions (static, stdlib only).

Returns one fingerprint per area of the repository (each directory with its own manifest), so
monorepos and mixed stacks get the conventions that apply where a new test will be written.

    python detect_conventions.py <repo> [--target path/to/feature] [--out conventions.json]
    python detect_conventions.py --self-test

A directory named e2e, tests or spec that is a product package (a manifest plus shipped code such as
src/, the rule of diff_scope.product_package) is not a test directory, so a workspace package named
`e2e` keeps unit, integration and e2e levels for its own tests. CI commands limited to a workspace
package (pnpm/turbo --filter, npm --workspace, yarn workspace, lerna --scope) go to that package's
area only. Signals whose call shapes other tools share (WebdriverIO's `browser.url(`, the word
appium) count only when the framework is declared or imported. `tooling` records the package
manager (used in the package.json run commands), workspaces, changesets, linters and the
TypeScript compiler; `node --test` suites are recognised as `node-test`.

Detection is heuristic: every conclusion carries the files it came from, and anything it can't
see is listed under `limitations`. CI configuration is treated as the source of truth for run
commands. Besides the usual file names, YAML files at the top level or under pipeline directories
(pipelines/, .azure-pipelines/, .azuredevops/, ci/, templates/ ...) count as CI when they are
shaped like an Azure DevOps pipeline or template (trigger/stages/jobs/steps with pool, task,
script or template entries).

`ci_gates` lists the test and coverage gates CI runs ({file, line, job, step, command, runner, fail_under}): cargo
test, cargo nextest, cargo llvm-cov and cargo tarpaulin with their --fail-under-lines/functions/regions thresholds,
pytest (--cov-fail-under), python -m unittest, coverage run -m unittest|pytest, coverage report --fail-under and go
test. Each area with Python tests has `python_runner` ({runner, reason, evidence, command}): unittest when CI runs
unittest or every test module defines unittest.TestCase classes and nothing configures or imports pytest; pytest
otherwise (said to be assumed when nothing names a runner). Areas carry `language_lines` (non-blank lines per
language) beside `languages` (files); the top level carries `languages` ({files, lines, test_files, test_lines} per
language) and `dominant_language`, decided by product lines as lang_census does (test files and tooling folders
such as scripts/ counted apart).

Tests that are not a framework: `self_tests` lists Python scripts that carry their own self-test, with the command to
run it from the repository root ({file, command, kind, line, area}): kind `flag` for an argparse `--self-test`
option or a literal `'--self-test' in sys.argv` check (`python <file> --self-test`), `function` for a module-level
self_test() called under `if __name__ == '__main__'` (`python <file>`), `script` for a self_test.py run as a
script. find_self_tests(root, files) is the helper; it reads files with ast, so '--self-test' in prose is not a flag,
and reads each file once per root (a repeated call only checks sizes and modification times).

is_test_file(path, root=None): by name or directory; with root, a test_*.py or *_test.py outside every test directory
that defines no test function or class (a product module such as test_paths.py) is not a test file. The Detector
passes its root.

pytest configuration: every pytest.ini, pyproject.toml [tool.pytest.ini_options], tox.ini [pytest] or setup.cfg
[tool:pytest] decides the Python test files below it by its testpaths (`.` means its own folder) and python_files;
an area's own config is under `pytest`, one in a folder with no manifest (benchmarks/pytest.ini) under the owning
area's `pytest_configs` ({config, dir, testpaths, python_files, python_classes, python_functions, collected,
not_collected, command}). Framework markers are matched in code only: a marker inside a string literal or comment
(a self-test's fixture source, a pattern table) is not a framework (code_mask.py).

Fixture projects: Detector(..., exclude_fixtures=True) (CLI --exclude-fixtures, --fixture-dir NAME) leaves out the
nested sample and fixture projects repo_layout.fixture_projects() finds and lists them under `fixtures_excluded`
({dir, reason, markers}); by default they are scanned as before.
"""
from __future__ import annotations
import argparse, ast, fnmatch, json, os, re, sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

SKIP_DIRS = {'.git', 'node_modules', 'vendor', 'dist', 'build', 'out', 'target', '.next', '.nuxt',
             '.svelte-kit', '.venv', 'venv', 'env', '__pycache__', '.tox', '.nox', '.gradle', '.idea',
             'Pods', 'DerivedData', 'coverage', '.dart_tool', 'bin', 'obj', '.terraform', '.cache',
             'playwright-report', 'test-results', 'site-packages', '.mypy_cache', '.pytest_cache', '.turbo',
             '.ruff_cache', '.hg', '.svn'}
# Folders named like build output are decided by evidence (lang_census.BuildEvidence: gitignored, a build config or
# minified bundles -> build output; served by the app or git-tracked and hand-written -> source), never by name alone.
BUILD_NAMES = {'dist', 'build', 'out', 'obj'}
try:   # shipped next to this script in every skill that carries detect_conventions.py
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import lang_census as _lc   # noqa: E402
except ImportError:   # pragma: no cover
    _lc = None
finally:
    sys.path.pop(0)
try:   # string and comment masking, so framework markers inside fixture strings are not frameworks
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import code_mask as _cm   # noqa: E402
except ImportError:   # pragma: no cover
    _cm = None
finally:
    sys.path.pop(0)
try:   # sample and fixture projects (exclude_fixtures=True)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import repo_layout as _rl   # noqa: E402
except ImportError:   # pragma: no cover
    _rl = None
finally:
    sys.path.pop(0)
MANIFESTS = {'package.json', 'pyproject.toml', 'setup.py', 'setup.cfg', 'requirements.txt',
             'requirements-dev.txt', 'pom.xml', 'build.gradle', 'build.gradle.kts', 'go.mod',
             'Gemfile', 'composer.json', 'Cargo.toml', 'pubspec.yaml', 'Package.swift'}
CI_GLOBS = ['.github/workflows/*.yml', '.github/workflows/*.yaml', '.gitlab-ci.yml', '.circleci/config.yml',
            'azure-pipelines.yml', 'azure-pipelines.yaml', 'bitbucket-pipelines.yml', 'Jenkinsfile', '.buildkite/*.yml',
            'buildspec.yml', '.drone.yml', 'codemagic.yaml', 'bitrise.yml']
# Azure DevOps pipelines and templates are often named after what they build (pipelines/api.yml,
# pipelines/templates/_tests.yml); YAML files in these directories (or at the top level) are read and
# kept when their shape is a pipeline's.
PIPELINE_DIRS = {'pipelines', '.pipelines', 'pipeline', '.azure-pipelines', 'azure-pipelines', '.azuredevops', '.azure',
                 'azure', 'azure-devops', '.vsts', 'ci', '.ci', 'build', 'devops', 'templates'}
AZDO_TOP = re.compile(r'^(trigger|pr|stages|jobs|steps|extends|resources|schedules|pool|variables|parameters)\s*:', re.M)
AZDO_SHAPE = re.compile(r'^\s*(-\s*)?(pool|vmImage|task|script|bash|pwsh|powershell|template|stage|job|deployment|'
                        r'checkout)\s*:', re.M)


def azure_pipeline_like(text: str) -> bool:
    """A YAML file shaped like an Azure DevOps pipeline or template: top-level pipeline keys (trigger, stages, jobs,
    steps, extends, ...) and step or job keys (pool, task, script, template, vmImage, ...), and not a Kubernetes,
    Compose, OpenAPI or GitHub Actions file."""
    tops = set(m.group(1) for m in AZDO_TOP.finditer(text))
    if not tops & {'trigger', 'pr', 'stages', 'jobs', 'steps', 'extends', 'schedules'}:
        return False
    if re.search(r'^(apiVersion|kind|services|openapi|swagger|on)\s*:', text, re.M) or 'runs-on:' in text:
        return False
    return len(set(m.group(2) for m in AZDO_SHAPE.finditer(text))) >= 1
LANG_EXT = {'.py': 'python', '.js': 'javascript', '.jsx': 'javascript', '.mjs': 'javascript',
            '.cjs': 'javascript', '.ts': 'typescript', '.tsx': 'typescript', '.java': 'java',
            '.kt': 'kotlin', '.kts': 'kotlin', '.go': 'go', '.rb': 'ruby', '.cs': 'csharp',
            '.php': 'php', '.rs': 'rust', '.dart': 'dart', '.swift': 'swift', '.feature': 'gherkin'}

# test-file name patterns -> naming label
NAMING = [
    (re.compile(r'^test_.+\.py$'), 'test_*.py'), (re.compile(r'^.+_test\.py$'), '*_test.py'),
    (re.compile(r'^.+\.spec\.[cm]?[jt]sx?$'), '*.spec.(js|ts)'), (re.compile(r'^.+\.test\.[cm]?[jt]sx?$'), '*.test.(js|ts)'),
    (re.compile(r'^.+\.cy\.[jt]sx?$'), '*.cy.(js|ts)'), (re.compile(r'^.+\.e2e\.[jt]s$'), '*.e2e.(js|ts)'),
    (re.compile(r'^.+(Test|Tests)\.(java|kt)$'), '*Test.(java|kt)'), (re.compile(r'^.+IT\.(java|kt)$'), '*IT.(java|kt)'),
    (re.compile(r'^.+_test\.go$'), '*_test.go'),
    (re.compile(r'^test_.+\.rs$'), 'test_*.rs'), (re.compile(r'^.+_test\.rs$'), '*_test.rs'),
    (re.compile(r'^.+_spec\.rb$'), '*_spec.rb'),
    (re.compile(r'^(test_.+|.+_test)\.rb$'), '*_test.rb'), (re.compile(r'^.+Tests?\.cs$'), '*Tests.cs'),
    (re.compile(r'^.+Test\.php$'), '*Test.php'), (re.compile(r'^.+_test\.dart$'), '*_test.dart'),
    (re.compile(r'^.+Tests?\.swift$'), '*Tests.swift'), (re.compile(r'^.+\.feature$'), '*.feature'),
]
TEST_DIR_PARTS = {'test', 'tests', '__tests__', 'spec', 'specs', 'e2e', 'cypress', 'integration_test',
                  'androidTest', 'acceptance', '.maestro', 'maestro', 'uitests'}

# A directory named like a test directory (e2e, tests, ...) is a product package, not test code, when it holds a
# manifest and ships code. The same rule as diff_scope.product_package (canonical there; copied because not every
# skill carrying this file carries diff_scope.py): a manifest plus src/, a package.json with main/module/exports/bin,
# a Cargo.toml with [lib]/[[bin]], go.mod beside non-test .go files, or the Python package a pyproject/setup names.
# A manifest alone (a spec-only Playwright workspace package) stays a test directory.
PRODUCT_MANIFESTS = ('package.json', 'pyproject.toml', 'setup.py', 'setup.cfg', 'Cargo.toml', 'go.mod', 'pom.xml',
                     'build.gradle', 'build.gradle.kts', 'composer.json', 'pubspec.yaml', 'deno.json', 'deno.jsonc')
_PRODUCT_CACHE: dict = {}


def product_package(root, rel_dir: str) -> str | None:
    """Why rel_dir (relative to root) is a product package rather than a test directory, or None."""
    if root is None:
        return None
    key = (str(root), rel_dir)
    if key in _PRODUCT_CACHE:
        return _PRODUCT_CACHE[key]
    d = Path(root) / rel_dir
    why = None
    try:
        found = [m for m in PRODUCT_MANIFESTS if (d / m).is_file()] if d.is_dir() else []
        if d.is_dir() and not found:
            found = [p.name for g in ('*.csproj', '*.fsproj', '*.gemspec') for p in d.glob(g)][:1]
        if found:
            m = found[0]
            if (d / 'src').is_dir():
                why = f'{m} and a src/ directory'
            elif m == 'package.json':
                try:
                    pkg = json.loads((d / m).read_text(encoding='utf-8', errors='replace'))
                except (ValueError, OSError):
                    pkg = {}
                fields = [k for k in ('main', 'module', 'exports', 'bin') if isinstance(pkg, dict) and pkg.get(k)]
                if fields:
                    why = f'package.json declaring {"/".join(fields)}'
            elif m == 'Cargo.toml':
                if re.search(r'^\s*\[(lib|\[bin\])\]', (d / m).read_text(encoding='utf-8', errors='replace'), re.M):
                    why = 'Cargo.toml with a [lib] or [[bin]] target'
            elif m == 'go.mod':
                if any(not p.name.endswith('_test.go') for p in d.glob('*.go')):
                    why = 'go.mod and non-test .go files'
            elif m in ('pyproject.toml', 'setup.py', 'setup.cfg'):
                text = (d / m).read_text(encoding='utf-8', errors='replace')
                names = {re.sub(r'[-.]', '_', n).lower()
                         for n in re.findall(r'''(?m)^\s*name\s*[=:]\s*["']?([\w.-]+)''', text)}
                pkgs = [p.name for p in d.iterdir() if p.is_dir() and p.name.lower() in names
                        and (p / '__init__.py').is_file()]
                if pkgs:
                    why = f'{m} and its Python package {pkgs[0]}/'
    except OSError:
        why = None
    if why:
        why = f'{rel_dir}/ is a product package ({why})'
    _PRODUCT_CACHE[key] = why
    return why

# dependency name (lowercase, substring-free exact match unless noted) -> (category, framework)
JS_DEPS = {
    'jest': ('unit', 'jest'), 'vitest': ('unit', 'vitest'), 'mocha': ('unit', 'mocha'), 'jasmine': ('unit', 'jasmine'),
    'ava': ('unit', 'ava'), '@playwright/test': ('e2e', 'playwright'), 'playwright': ('e2e', 'playwright'),
    'cypress': ('e2e', 'cypress'), '@wdio/cli': ('e2e', 'webdriverio'), 'webdriverio': ('e2e', 'webdriverio'),
    'selenium-webdriver': ('e2e', 'selenium'), 'testcafe': ('e2e', 'testcafe'), 'puppeteer': ('e2e', 'puppeteer'),
    'detox': ('mobile', 'detox'), 'appium': ('mobile', 'appium'), '@wdio/appium-service': ('mobile', 'appium'),
    '@cucumber/cucumber': ('bdd', 'cucumber'), 'playwright-bdd': ('bdd', 'playwright-bdd'),
    '@badeball/cypress-cucumber-preprocessor': ('bdd', 'cucumber'),
}
JS_STYLE_DEPS = {
    'chai': ('assertions', 'chai'), '@testing-library/jest-dom': ('assertions', 'jest-dom'),
    '@testing-library/react': ('helpers', 'testing-library'), '@testing-library/vue': ('helpers', 'testing-library'),
    '@testing-library/react-native': ('helpers', 'testing-library'), 'sinon': ('mocking', 'sinon'),
    'msw': ('mocking', 'msw'), 'nock': ('mocking', 'nock'), 'supertest': ('helpers', 'supertest'),
    '@faker-js/faker': ('data', 'faker'), 'fishery': ('data', 'fishery'), 'jest-junit': ('reporting', 'jest-junit'),
}
PY_DEPS = {
    'pytest': ('unit', 'pytest'), 'pytest-bdd': ('bdd', 'pytest-bdd'), 'behave': ('bdd', 'behave'),
    'pytest-playwright': ('e2e', 'playwright'), 'playwright': ('e2e', 'playwright'), 'selenium': ('e2e', 'selenium'),
    'appium-python-client': ('mobile', 'appium'), 'robotframework': ('e2e', 'robotframework'),
}
PY_STYLE_DEPS = {
    'pytest-mock': ('mocking', 'pytest-mock'), 'responses': ('mocking', 'responses'), 'respx': ('mocking', 'respx'),
    'freezegun': ('mocking', 'freezegun'), 'factory-boy': ('data', 'factory_boy'), 'factory_boy': ('data', 'factory_boy'),
    'faker': ('data', 'faker'), 'hypothesis': ('helpers', 'hypothesis'), 'pytest-asyncio': ('async', 'pytest-asyncio'),
    'pytest-django': ('helpers', 'pytest-django'), 'model-bakery': ('data', 'model_bakery'),
}
# substring signals in JVM/.NET/Ruby/PHP/Dart/Rust manifests -> (category, framework)
TEXT_DEPS = {
    'jvm': [('junit-jupiter', 'unit', 'junit5'), ('org.junit.jupiter', 'unit', 'junit5'), ("'junit:junit", 'unit', 'junit4'),
            ('"junit:junit', 'unit', 'junit4'), ('<artifactId>junit</artifactId>', 'unit', 'junit4'),
            ('testng', 'unit', 'testng'), ('kotest', 'unit', 'kotest'), ('espresso', 'mobile', 'espresso'),
            ('ui-test-junit4', 'mobile', 'compose-ui-test'), ('io.appium', 'mobile', 'appium'),
            ('selenium-java', 'e2e', 'selenium'), ('com.microsoft.playwright', 'e2e', 'playwright'),
            ('cucumber-java', 'bdd', 'cucumber'), ('mockito', 'mocking', 'mockito'), ('mockk', 'mocking', 'mockk'),
            ('assertj', 'assertions', 'assertj'), ('hamcrest', 'assertions', 'hamcrest'),
            ('com.google.truth', 'assertions', 'truth'), ('rest-assured', 'helpers', 'rest-assured'),
            ('testcontainers', 'helpers', 'testcontainers'), ('robolectric', 'helpers', 'robolectric')],
    'ruby': [('rspec', 'unit', 'rspec'), ('minitest', 'unit', 'minitest'), ('capybara', 'e2e', 'capybara'),
             ('cucumber', 'bdd', 'cucumber'), ('factory_bot', 'data', 'factory_bot'), ('webmock', 'mocking', 'webmock')],
    'php': [('phpunit/phpunit', 'unit', 'phpunit'), ('pestphp/pest', 'unit', 'pest'), ('behat/behat', 'bdd', 'behat'),
            ('laravel/dusk', 'e2e', 'dusk'), ('mockery/mockery', 'mocking', 'mockery')],
    'dart': [('flutter_test', 'unit', 'flutter_test'), ('integration_test', 'mobile', 'flutter-integration_test'),
             ('patrol', 'mobile', 'patrol'), ('mockito', 'mocking', 'mockito'), ('mocktail', 'mocking', 'mocktail'),
             ('bloc_test', 'helpers', 'bloc_test')],
    'csharp': [('xunit', 'unit', 'xunit'), ('nunit', 'unit', 'nunit'), ('mstest', 'unit', 'mstest'),
               ('microsoft.playwright', 'e2e', 'playwright'), ('selenium.webdriver', 'e2e', 'selenium'),
               ('appium.webdriver', 'mobile', 'appium'), ('specflow', 'bdd', 'specflow'), ('reqnroll', 'bdd', 'reqnroll'),
               ('moq', 'mocking', 'moq'), ('nsubstitute', 'mocking', 'nsubstitute'),
               ('fluentassertions', 'assertions', 'fluentassertions')],
}
# content signals inside test files -> (category, framework)
CONTENT_SIGNALS = [
    (re.compile(r'^\s*import pytest|@pytest\.'), 'unit', 'pytest'),
    (re.compile(r'^\s*import unittest|unittest\.TestCase'), 'unit', 'unittest'),
    (re.compile(r'from playwright|@playwright/test|playwright\.sync_api'), 'e2e', 'playwright'),
    (re.compile(r'\bcy\.(visit|get|request)\('), 'e2e', 'cypress'),
    (re.compile(r'\bbrowser\.url\(|\$\(\s*[\'"]~|[\'"]@wdio/|[\'"]webdriverio[\'"]'), 'e2e', 'webdriverio'),
    (re.compile(r'from selenium|selenium-webdriver|org\.openqa\.selenium'), 'e2e', 'selenium'),
    (re.compile(r'\bdevice\.launchApp\(|\bby\.id\(|from [\'"]detox[\'"]'), 'mobile', 'detox'),
    (re.compile(r'[\'"][^\'"\n]*appium[^\'"\n]*[\'"]\s*\)?\s*;?\s*$|from appium|io\.appium|AppiumBy|MobileBy|'
                r'appium\.webdriver', re.M | re.I), 'mobile', 'appium'),
    (re.compile(r'[\'"]node:test[\'"]'), 'unit', 'node-test'),
    (re.compile(r'^appId:\s*\S+', re.M), 'mobile', 'maestro'),
    (re.compile(r'XCUIApplication\(\)'), 'mobile', 'xcuitest'),
    (re.compile(r'^\s*import XCTest'), 'unit', 'xctest'),
    (re.compile(r'androidx\.test\.espresso|onView\('), 'mobile', 'espresso'),
    (re.compile(r'createComposeRule|createAndroidComposeRule'), 'mobile', 'compose-ui-test'),
    (re.compile(r'IntegrationTestWidgetsFlutterBinding'), 'mobile', 'flutter-integration_test'),
    (re.compile(r'^\s*func Test\w+\(t \*testing\.T\)', re.M), 'unit', 'go-testing'),
    (re.compile(r'^\s*#\[(tokio::)?test\]', re.M), 'unit', 'cargo-test'),
    (re.compile(r'^\s*(Feature|Scenario|Scenario Outline):', re.M), 'bdd', 'gherkin'),
    (re.compile(r'from pytest_bdd|pytest_bdd'), 'bdd', 'pytest-bdd'),
    (re.compile(r'^\s*from behave'), 'bdd', 'behave'),
]
# a content signal for these frameworks counts only when the area (or the repository root) declares the framework
# in a manifest or config, or the file imports it: their call shapes are shared by other tools' fixtures
GATED = {'webdriverio': re.compile(r'[\'"]@wdio/|[\'"]webdriverio[\'"]'),
         'appium': re.compile(r'from appium|io\.appium|appium\.webdriver|(?:from|require\()\s*[\'"][^\'"]*appium')}
STYLE_SIGNALS = {
    'assertions': [('expect(', 'expect'), ('assert ', 'assert'), ('self.assert', 'unittest-asserts'),
                   ('assertThat(', 'assertThat'), ('.should(', 'should'), ('XCTAssert', 'XCTAssert'),
                   ('require.', 'testify-require'), ('assert.', 'assert-module')],
    'mocking': [('jest.mock(', 'jest.mock'), ('vi.mock(', 'vi.mock'), ('unittest.mock', 'unittest.mock'),
                ('mocker.', 'pytest-mock'), ('monkeypatch', 'monkeypatch'), ('Mockito.', 'mockito'), ('mockk', 'mockk'),
                ('sinon.', 'sinon'), ('page.route(', 'playwright-route'), ('cy.intercept(', 'cy.intercept'),
                ('gomock', 'gomock'), ('httptest.', 'httptest')],
    'parametrisation': [('pytest.mark.parametrize', 'pytest.mark.parametrize'), ('test.each', 'test.each'),
                        ('it.each', 'it.each'), ('@ParameterizedTest', 'JUnit @ParameterizedTest'),
                        ('[]struct', 'go table-driven'), ('Scenario Outline', 'Gherkin Scenario Outline'),
                        ('[Theory]', 'xUnit [Theory]'), ('@TestCase', 'NUnit [TestCase]')],
    'fixtures': [('@pytest.fixture', 'pytest fixtures'), ('beforeEach(', 'beforeEach'), ('test.extend(', 'playwright test.extend'),
                 ('setUp(', 'setUp'), ('@BeforeEach', 'JUnit @BeforeEach'), ('@Before', 'JUnit @Before'),
                 ('let!(', 'rspec let!'), ('setUpAll(', 'flutter setUpAll')],
    'async': [('async def test', 'async tests'), ('pytest.mark.asyncio', 'pytest-asyncio'), ('async () =>', 'async tests'),
              ('async ({', 'async tests'), ('runTest {', 'kotlinx runTest'), ('async throws', 'swift async')],
}
SELECTOR_SIGNALS = {
    'data-testid': re.compile(r'data-testid|getByTestId|get_by_test_id|\[data-test(id)?=|data-cy|findByTestId'),
    'role': re.compile(r'getByRole|get_by_role|findByRole|ByRole\('),
    'label': re.compile(r'getByLabel|get_by_label|findByLabelText|getByLabelText|getByPlaceholder'),
    'text': re.compile(r'getByText|get_by_text|cy\.contains|findByText|text='),
    'accessibility-id': re.compile(r'accessibilityId|AccessibilityId|~[\w-]+[\'"]|by\.id\(|testID|withContentDescription|accessibilityIdentifier|find\.byKey|ValueKey\('),
    'css': re.compile(r'(?:locator|cy\.get|querySelector(?:All)?|\$\$?)\(\s*[\'"][.#][\w-]'),
    'xpath': re.compile(r'xpath|By\.xpath|//\w+\['),
}
E2E_BLOCKS = {
    'page_objects': re.compile(r'(?:^|/)(pages?|page[-_]?objects?|pom|screens?|screen[-_]?objects?)/|(?:Page|Screen|PageObject)\.(?:[jt]s|py|java|kt|swift|dart|rb|cs)$'),
    'custom_commands': re.compile(r'cypress/support/commands\.[jt]s$|(?:^|/)commands\.[jt]s$'),
    'login_helpers': re.compile(r'auth\.setup\.[jt]s$|global[-_.]setup\.[jt]s$|(?:^|/)(login|auth)[-_.]?(helper|fixture|setup)s?\.\w+$|storageState'),
    'seeding': re.compile(r'(?:^|/)(seeds?|factories|fixtures|testdata|test[-_]data)/|seed\.\w+$|factories\.py$'),
}
RUN_HINTS = re.compile(r'\b(pytest|python -m pytest|tox|nox|npm (run )?test\S*|npm run \S*(test|e2e)\S*|yarn (run )?\S*(test|e2e)\S*|'
                       r'pnpm (run )?\S*(test|e2e)\S*|(pnpm|yarn|npm|bun|turbo)( +(--filter|-F|--workspace|-w|--scope)[ =]\S+)+'
                       r' +(run +)?\S*test\S*|node +(--[\w-]+(=\S+)? +)*--test|npx(jest|vitest|playwright test|cypress run|wdio|detox test)|'
                       r'(jest|vitest|mocha)\b|playwright test|cypress run|go test|mvn (-\S+ )*(verify|test)|'
                       r'(\./)?gradlew? \S*(test|check|connected\w*AndroidTest)|bundle exec (rspec|rake|cucumber)|rspec|'
                       r'dotnet test|cargo (\+\S+ )?(nextest run|test|llvm-cov|tarpaulin)|flutter test|phpunit|xcodebuild (\S+ )*test|'
                       r'maestro test|detox test|behave|python[\d.]* -m unittest|coverage run)\b')

# where a command starts on a CI line: the line's start or after && || ; | $( ( `, then any VAR=value assignments and
# wrappers (sudo, time, env, uv run, poetry run, pipenv run, pdm run, hatch run, rye run, xvfb-run); so `pytest` in
# `pip install pytest pytest-cov` or in an echo is never an invocation
CMD_START = (r'(?:^|&&|\|\||;|\||\$\(|\(|`)\s*(?:[A-Za-z_]\w*=\S*\s+)*'
             r'(?:(?:sudo|time|exec|env|uv run|poetry run|pipenv run|pdm run|hatch run|rye run|xvfb-run(?: -\S+)*)\s+)*'
             r'(?:[\w.~$/\\-]*[/\\])?')   # a path to the executable (.venv/bin/pytest)
# CI test gates: (runner, pattern) tried in order on each command line of a CI file; thresholds are read separately
CI_GATE_RUNNERS = [
    ('cargo-llvm-cov', re.compile(r'\bcargo (\+\S+ )?llvm-cov\b')),
    ('cargo-tarpaulin', re.compile(r'\bcargo (\+\S+ )?tarpaulin\b')),
    ('cargo-nextest', re.compile(r'\bcargo (\+\S+ )?nextest run\b')),
    ('cargo-test', re.compile(r'\bcargo (\+\S+ )?test\b')),
    ('coverage-unittest', re.compile(CMD_START + r'(?:python[\d.]* -m )?coverage run\b.*-m unittest\b')),
    ('coverage-pytest', re.compile(CMD_START + r'(?:python[\d.]* -m )?coverage run\b.*-m pytest\b')),
    ('coverage-report', re.compile(r'\bcoverage (report|xml|json|html)\b.*--fail-under')),
    ('unittest', re.compile(CMD_START + r'python[\d.]* -m unittest\b')),
    ('pytest', re.compile(CMD_START + r'(?:python[\d.]* -m |py(?: -[\d.]+)? -m )?pytest\b(?!-)')),
    ('go-test', re.compile(r'\bgo test\b')),
]
CI_THRESHOLDS = [
    (re.compile(r'--fail-under-(lines|functions|regions)[ =](\d+(?:\.\d+)?)'), None),
    (re.compile(r'--cov-fail-under[ =](\d+(?:\.\d+)?)'), 'total'),
    (re.compile(r'--fail-under[ =](\d+(?:\.\d+)?)'), 'total'),
]

def rel(p: Path, root: Path) -> str:
    s, b = str(p), str(root)
    if s.startswith(b):   # the common case, by string (Path.relative_to is costly): the same result
        if len(s) == len(b):
            return '.'
        if b.endswith(os.sep) or s[len(b)] == os.sep:
            return s[len(b):].lstrip(os.sep).replace(os.sep, '/')
    return PurePosixPath(p.relative_to(root)).as_posix()

def read(p: Path, limit: int = 400_000) -> str:
    try:
        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            return f.read(limit)
    except OSError:
        return ''

def naming_of(name: str) -> str | None:
    for rx, label in NAMING:
        if rx.match(name):
            return label
    return None

def pytest_collection(root: Path, area: str) -> dict | None:
    """pytest's testpaths and python_files from the area's own config (pytest.ini, pyproject.toml
    [tool.pytest.ini_options], tox.ini or setup.cfg [tool:pytest]), else None. python_files defaults to pytest's
    test_*.py and *_test.py."""
    d = root if area == '.' else root / area
    for name, section in (('pytest.ini', r'\[pytest\]'), ('pyproject.toml', r'\[tool\.pytest\.ini_options\]'),
                          ('tox.ini', r'\[pytest\]'), ('setup.cfg', r'\[tool:pytest\]')):
        p = d / name
        if not p.is_file():
            continue
        text = read(p)
        m = re.search(section + r'[ \t]*\n(.*?)(?=^\[|\Z)', text, re.S | re.M)
        if not m:
            continue
        body = m.group(1)

        def values(key):
            mm = re.search(rf'^\s*{key}\s*=\s*(\[[^\]]*\]|.+(?:\n[ \t]+.+)*)', body, re.M)
            if not mm:
                return []
            raw = mm.group(1)
            if raw.lstrip().startswith('['):
                return re.findall(r'["\']([^"\']+)["\']', raw)
            return raw.split()
        files = values('python_files') or ['test_*.py', '*_test.py']
        paths = [re.sub(r'^\./', '', t.replace('\\', '/')).strip('/') for t in values('testpaths')]
        return {'config': (PurePosixPath(area) / name).as_posix() if area != '.' else name, 'dir': area,
                'testpaths': ['.' if t in ('', '.') else t for t in paths], 'python_files': files,
                'python_classes': values('python_classes') or ['Test'],
                'python_functions': values('python_functions') or ['test']}
    return None


PYTEST_CONFIG_NAMES = ('pytest.ini', 'pyproject.toml', 'tox.ini', 'setup.cfg')


_SPANS: dict = {}   # (path, len(text)) -> masked spans of the last file read, so each file is lexed once


def code_search(rx, text: str, path: str):
    """rx.search(text), but a match that starts inside a string literal or comment does not count (code_mask), so
    fixture sources a self-test holds in strings and pattern tables are not read as the code they describe."""
    if _cm is None:
        return rx.search(text)
    lang = _cm.lang_of(path)
    if lang is None:
        return rx.search(text)
    m = rx.search(text)
    if m is None:
        return None   # no match at all: nothing to lex
    key = (path, len(text))
    if key not in _SPANS:
        _SPANS.clear()
        _SPANS[key] = _cm.masked_spans(text, lang, kinds=False)
    return _cm.code_search(rx, text, lang, _SPANS[key])


SELF_TEST_NAMES = {'self_test.py', 'selftest.py', 'self_tests.py'}
SELF_TEST_HINT = re.compile(r'--self-test|^(?:async\s+)?def self_test\b', re.M)


def _is_main_guard(node) -> bool:
    t = getattr(node, 'test', None)
    return isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) and t.left.id == '__name__' and any(
        isinstance(c, ast.Constant) and c.value == '__main__' for c in t.comparators)


def find_self_tests(root: Path, files) -> list[dict]:
    """Python scripts that carry their own self-test, with the command to run it from the repository root:
    kind 'flag' (an argparse --self-test option or a literal '--self-test' check: `python <file> --self-test`),
    'function' (a module-level self_test() called under `if __name__ == '__main__'`: `python <file>`) or 'script'
    (a self_test.py run as a script). Read with ast: '--self-test' inside a longer string (prose, a docstring) is
    not a flag. Only scripts with a __main__ block count. Each file is read once per root: a later call (the Detector,
    then a skill's own scanner) only checks each file's size and modification time."""
    out = []
    base = str(Path(root).resolve())
    for f in files:
        if not f.endswith('.py'):
            continue
        sig = _file_sig(root / f)
        hit = _SELF_TEST_CACHE.get((base, f))
        if hit is None or hit[0] != sig:
            hit = (sig, _self_test_entry(root, f))
            _SELF_TEST_CACHE[(base, f)] = hit
        if hit[1]:
            out.append(dict(hit[1]))
    return out


# (resolved root, file) -> ((size, mtime_ns), entry or None): find_self_tests reads each file once
_SELF_TEST_CACHE: dict = {}


def _file_sig(p: Path):
    try:
        st = p.stat()
        return st.st_size, st.st_mtime_ns
    except OSError:
        return None


def _self_test_entry(root: Path, f: str) -> dict | None:
    """find_self_tests' entry for one file, or None."""
    name = PurePosixPath(f).name
    text = read(root / f, 2_000_000)
    if name not in SELF_TEST_NAMES and not SELF_TEST_HINT.search(text):
        return None
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    mains = [n for n in tree.body if isinstance(n, ast.If) and _is_main_guard(n)]
    flag, argv_check = None, False
    if '--self-test' in text:
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and n.value == '--self-test' and flag is None:
                flag = n.lineno
            elif isinstance(n, ast.Compare) and any(isinstance(x, ast.Constant) and x.value == '--self-test'
                                                    for x in [n.left, *n.comparators]):
                argv_check = True
    if not mains and not argv_check:   # not run as a script: a module that only mentions the flag
        return None
    defs = {n.name: n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    calls = 'self_test' in defs and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                                        and c.func.id == 'self_test' for m in mains for c in ast.walk(m))
    if flag:
        return {'file': f, 'command': f'python {f} --self-test', 'kind': 'flag', 'line': flag}
    if 'self_test' in defs and calls:
        return {'file': f, 'command': f'python {f}', 'kind': 'function', 'line': defs['self_test']}
    if name in SELF_TEST_NAMES:
        return {'file': f, 'command': f'python {f}', 'kind': 'script', 'line': mains[0].lineno}
    return None


# (resolved root, file) -> ((size, mtime_ns), defines a test): is_test_file(path, root) reads each file once
_DEFINES_TESTS_CACHE: dict = {}


def defines_python_tests(root: Path, path: str) -> bool | None:
    """Does the Python file define a test: a module-level `def test*`/`async def test*`, or a class named Test* or
    deriving from a *TestCase? None when it cannot be read or parsed (unknown)."""
    p = Path(root) / path
    key, sig = (str(Path(root).resolve()), path), _file_sig(p)
    hit = _DEFINES_TESTS_CACHE.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]
    try:
        tree = ast.parse(read(p, 2_000_000))
        found = False
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith('test'):
                found = True
            elif isinstance(n, ast.ClassDef) and (n.name.startswith('Test') or any(
                    (getattr(b, 'attr', None) or getattr(b, 'id', '') or '').endswith('TestCase') for b in n.bases)):
                found = True
            if found:
                break
    except (SyntaxError, ValueError):
        found = None
    if sig is None:
        found = None
    _DEFINES_TESTS_CACHE[key] = (sig, found)
    return found


def _in_test_dir(p: PurePosixPath) -> bool:
    lower = {x.lower() for x in TEST_DIR_PARTS}
    return any(x.lower() in lower or x.endswith('Tests') for x in p.parts[:-1])


def is_test_file(path: str, root=None) -> bool:
    """Is path (repository-relative) a test file, by its name or its directory? With root, a Python file named like a
    test (test_*.py, *_test.py) outside every test directory that defines no test function or class is product code
    (a module such as test_paths.py that product code imports), not a test file; a self_test.py stays a test file."""
    p = PurePosixPath(path)
    if p.suffix not in LANG_EXT and p.suffix not in ('.yaml', '.yml'):
        return False
    if naming_of(p.name):
        if root is not None and p.suffix == '.py' and p.name not in SELF_TEST_NAMES and not _in_test_dir(p) \
                and defines_python_tests(root, path) is False:
            return False
        return True
    parts = set(p.parts[:-1])
    if p.suffix in ('.yaml', '.yml'):
        return bool(parts & {'.maestro', 'maestro'})
    if p.name in ('conftest.py', '__init__.py') or p.name.startswith('_'):
        return False
    if parts & {'__tests__', 'androidTest', 'integration_test'} or any(x.endswith('UITests') or x.endswith('Tests') for x in parts):
        return p.suffix in LANG_EXT
    if 'src' in p.parts and 'test' in p.parts and p.suffix in ('.java', '.kt', '.scala'):
        return True
    # Rust integration tests live in a top-level tests/ tree (e.g. tests/by-util/test_*.rs); count any
    # .rs file inside a test/tests directory, but not library sources nested under src/ (helper crates).
    if p.suffix == '.rs' and (parts & {'test', 'tests'}) and 'src' not in parts:
        return True
    return False

def strip_product_dirs(path: str, repo) -> str:
    """path with everything up to the deepest test-named directory that is a product package removed
    (packages/e2e/tests/integration/x.test.ts -> tests/integration/x.test.ts when packages/e2e is a product package),
    so a package named e2e does not make its unit tests e2e. Without repo, path is returned unchanged."""
    if repo is None:
        return path
    parts = PurePosixPath(path).parts
    cut = 0
    for i, seg in enumerate(parts[:-1]):
        if seg.lower() in {x.lower() for x in TEST_DIR_PARTS} and product_package(repo, '/'.join(parts[:i + 1])):
            cut = i + 1
    return '/'.join(parts[cut:]) if cut else path


def level_of(path: str, frameworks: set[str], root: str = '.', repo=None) -> str:
    """unit | integration | e2e, from directory names and suffixes relative to the area root. Give repo (the
    repository directory path is relative to) so a product package named e2e does not count as an e2e directory."""
    if root != '.' and path.startswith(root + '/'):
        path = path[len(root) + 1:]
    path = strip_product_dirs(path, repo if root == '.' else (Path(repo) / root if repo is not None else None))
    parts = [x.lower() for x in PurePosixPath(path).parts]
    joined = '/'.join(parts)
    if 'unit' in parts[:-1]:
        return 'unit'
    if any(x in parts for x in ('e2e', 'cypress', 'acceptance', 'uitests', 'androidtest', 'integration_test', '.maestro', 'maestro', 'ui-tests', 'ui_tests')) \
            or any(x.endswith('uitests') for x in parts) or '.cy.' in joined or '.e2e.' in joined or joined.endswith('.feature'):
        return 'e2e'
    if any(x in parts[:-1] for x in ('integration', 'it', 'functional', 'contract')) or re.search(r'it\.(java|kt)$', joined):
        return 'integration'
    return 'unit'

def deps_of_package_json(text: str) -> tuple[dict, dict]:
    try:
        data = json.loads(text)
    except ValueError:
        return {}, {}
    deps = {}
    for key in ('dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies'):
        if isinstance(data.get(key), dict):
            deps.update(data[key])
    scripts = data.get('scripts') if isinstance(data.get('scripts'), dict) else {}
    return deps, scripts

def py_dep_names(text: str) -> set[str]:
    names = set()
    for m in re.finditer(r'(?m)^\s*["\']?([A-Za-z0-9][A-Za-z0-9_.\-]*)(?:\[[^\]]*\])?\s*(?:[<>=!~;"\',]|$)', text):
        names.add(m.group(1).lower().replace('_', '-'))
    return names

WS_FILTER = re.compile(r'(?:--filter|-F|--workspace|--scope)(?:=|\s+)[\'"]?([^\s\'"]+)|'
                       r'\byarn\s+workspace\s+[\'"]?([^\s\'"]+)|\bnpm\s+(?:\S+\s+)*?-w\s+[\'"]?([^\s\'"]+)')


def workspace_filters(command: str) -> list[str]:
    """The workspace packages a command is limited to (pnpm/turbo --filter, -F, npm --workspace/-w, yarn workspace,
    lerna --scope), as written."""
    return [next(g for g in m.groups() if g) for m in WS_FILTER.finditer(command)]


def filter_matches(target: str, root: str, pkg_name: str | None) -> bool:
    """Whether a workspace filter selects the area at root (whose package.json name is pkg_name). Handles names,
    name globs (@scope/*), directory filters (./packages/x, {packages/x}) and pnpm's dependency suffixes (x..., ...x)."""
    import fnmatch
    t = target.strip()
    t = re.sub(r'^\.\.\.\^?|\^?\.\.\.$', '', t)          # pnpm: x... (with dependencies), ...x (dependents)
    if t.startswith('!'):
        return False
    if t.startswith('{') and t.endswith('}'):
        t = t[1:-1]
    if t.startswith('./') or (t.startswith('.') is False and '/' in t and not t.startswith('@')):
        t = t[2:] if t.startswith('./') else t
        t = t.rstrip('/')
        return fnmatch.fnmatch(root, t) or root == t
    return bool(pkg_name) and fnmatch.fnmatchcase(pkg_name, t)


class Detector:
    def __init__(self, root: Path, max_files: int, include=(), exclude_fixtures: bool = False, fixture_dirs=()):
        self.exclude_fixtures = exclude_fixtures
        self.fixture_dirs = list(fixture_dirs or ())
        self.fixtures: list[dict] = []
        self.root = root.resolve()
        self.max_files = max_files
        self.files: list[str] = []
        self.limitations: list[str] = []
        self.tooling: dict = {}
        self.include = [i.replace("\\", "/").strip("/") for i in include or ()]
        self.build_output: list[dict] = []

    def keep_dir(self, dirpath: str, d: str) -> bool:
        if d.endswith('.egg-info'):
            return False
        if d in BUILD_NAMES:
            sub = rel(Path(dirpath) / d, self.root)
            if _lc is None:
                is_b, why = True, f'{d}/ is named like build output (lang_census.py is not available to check)'
            else:
                is_b, why = self.evidence.decide(sub)
            self.build_output.append({'path': sub, 'decision': 'build_output' if is_b else 'source', 'reason': why})
            return not is_b
        return d not in SKIP_DIRS

    def walk(self):
        self.build_output = []
        self.evidence = _lc.BuildEvidence(self.root, self.include) if _lc else None
        if self.exclude_fixtures:
            if _rl is None:
                self.limitations.append('Fixture projects were not excluded: repo_layout.py is not available.')
            else:
                self.fixtures = _rl.fixture_projects(self.root, self.fixture_dirs)
        fixture_dirs = {f['dir'] for f in self.fixtures}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if self.keep_dir(dirpath, d)
                                 and rel(Path(dirpath) / d, self.root) not in fixture_dirs)
            for fn in sorted(filenames):
                self.files.append(rel(Path(dirpath) / fn, self.root))
                if len(self.files) >= self.max_files:
                    self.limitations.append(f'Stopped after {self.max_files} files; raise --max-files for a complete scan.')
                    return

    def repo_tooling(self) -> dict:
        """Repository-level tooling read from the root: the JavaScript package manager (packageManager field or
        lockfile), workspace globs (pnpm-workspace.yaml or package.json workspaces), changesets (and how many are
        pending), linters and the TypeScript compiler (tsc, or tsgo from @typescript/native-preview)."""
        out: dict = {}
        files = set(self.files)
        pj = {}
        if 'package.json' in files:
            try:
                pj = json.loads(read(self.root / 'package.json'))
            except ValueError:
                pj = {}
        if not isinstance(pj, dict):
            pj = {}
        pm_field = pj.get('packageManager') if isinstance(pj.get('packageManager'), str) else ''
        locks = [('pnpm-lock.yaml', 'pnpm'), ('yarn.lock', 'yarn'), ('bun.lock', 'bun'), ('bun.lockb', 'bun'),
                 ('package-lock.json', 'npm'), ('npm-shrinkwrap.json', 'npm')]
        pm = pm_field.split('@')[0] if pm_field else next((m for lf, m in locks if lf in files), None)
        if pm:
            out['package_manager'] = pm
            out['package_manager_source'] = 'package.json packageManager' if pm_field else \
                next(lf for lf, m in locks if lf in files and m == pm)
        ws = []
        if 'pnpm-workspace.yaml' in files:
            text = read(self.root / 'pnpm-workspace.yaml')
            m = re.search(r'^packages\s*:\s*\n((?:\s+-\s*.*\n?)+)', text, re.M)
            if m:
                ws = [x.strip().strip('\'"') for x in re.findall(r'-\s*(.+)', m.group(1))]
            out['workspaces_source'] = 'pnpm-workspace.yaml'
        elif isinstance(pj.get('workspaces'), (list, dict)):
            w = pj['workspaces']
            ws = list(w if isinstance(w, list) else w.get('packages', []))
            out['workspaces_source'] = 'package.json workspaces'
        if ws:
            out['workspaces'] = ws
        if '.changeset/config.json' in files:
            pending = [f for f in files if f.startswith('.changeset/') and f.endswith('.md') and f != '.changeset/README.md']
            out['changesets'] = {'config': '.changeset/config.json', 'pending': len(pending)}
        deps = {}
        for key in ('dependencies', 'devDependencies'):
            if isinstance(pj.get(key), dict):
                deps.update(pj[key])
        scripts = ' '.join(str(v) for v in (pj.get('scripts') or {}).values()) if isinstance(pj.get('scripts'), dict) else ''
        linters = {}
        for name, signs in (('eslint', ('eslint',)), ('oxlint', ('oxlint',)), ('biome', ('@biomejs/biome',)),
                            ('ruff', ()), ('prettier', ('prettier',))):
            src = next((f'package.json {d}' for d in signs if d in deps), None)
            if not src and re.search(rf'(^|[\s&;|]){name}\b', scripts):
                src = 'package.json scripts'
            if not src:
                conf = {'eslint': ('eslint.config.js', 'eslint.config.mjs', 'eslint.config.ts', '.eslintrc.json', '.eslintrc.js', '.eslintrc.cjs', '.eslintrc'),
                        'oxlint': ('.oxlintrc.json', 'oxlint.json', 'oxlintrc.json'), 'biome': ('biome.json', 'biome.jsonc'),
                        'ruff': ('ruff.toml', '.ruff.toml'), 'prettier': ('.prettierrc', '.prettierrc.json', 'prettier.config.js')}[name]
                src = next((c for c in conf if c in files), None)
            if src:
                linters[name] = src
        if linters:
            out['linters'] = linters
        ts = {}
        if '@typescript/native-preview' in deps or re.search(r'(^|[\s&;|])tsgo\b', scripts):
            ts['tsgo'] = deps.get('@typescript/native-preview', 'package.json scripts')
        if 'typescript' in deps:
            ts['tsc'] = deps['typescript']
            if re.match(r'^[\^~>=]*\s*7\.', str(deps['typescript'])):
                ts['note'] = 'TypeScript 7 (the native compiler): tools that load the TypeScript API in-process may not support it'
        if ts:
            out['typescript'] = ts
        return out

    def area_roots(self) -> list[str]:
        roots = {'.'}
        for f in self.files:
            p = PurePosixPath(f)
            if p.name in MANIFESTS or p.suffix == '.csproj':
                roots.add(str(p.parent) if str(p.parent) != '' else '.')
        return sorted(roots, key=lambda r: (r.count('/'), r))

    @staticmethod
    def owner(path: str, roots: list[str]) -> str:
        """The deepest area root that contains path."""
        matches = [r for r in roots if r != '.' and (path == r or path.startswith(r + '/'))]
        return max(matches, key=len) if matches else '.'

    def _is_ci(self, f: str) -> bool:
        p = PurePosixPath(f)
        if any(p.match(g) for g in CI_GLOBS) or f in CI_GLOBS:
            return True
        if p.suffix not in ('.yml', '.yaml') or f.startswith('.github/'):
            return False
        if len(p.parts) > 1 and not (set(p.parts[:-1]) & PIPELINE_DIRS):
            return False
        return azure_pipeline_like(read(self.root / f, 200_000))

    def ci(self) -> tuple[list[str], list[dict]]:
        ci_files, commands = [], []
        for f in self.files:
            if self._is_ci(f):
                ci_files.append(f)
                lines = read(self.root / f).splitlines()
                for i, line in enumerate(lines, 1):
                    s = re.sub(r'^\s*(-\s*)?(run|script|command|sh)\s*:\s*[|>]?\s*', '', line).strip()
                    if len(s) > 1 and s[0] == s[-1] and s[0] in '\'"':
                        s = s[1:-1]
                    if s and not s.startswith('#') and RUN_HINTS.search(s) and len(s) < 300:
                        cmd = {'command': s, 'source': f'{f}:{i}', 'origin': 'ci'}
                        for near in self._step_lines(lines, i - 1):
                            m = re.search(r'working[-_]?directory\s*:\s*[\'"]?([^\'"\s]+)', near, re.I)
                            if m:
                                cmd['working_directory'] = m.group(1).strip('./') or '.'
                                break
                        commands.append(cmd)
        return ci_files, commands

    def ci_gates(self, ci_files: list[str]) -> list[dict]:
        """Test and coverage gates in CI: every command line that runs cargo test, cargo nextest, cargo llvm-cov,
        cargo tarpaulin, pytest, python -m unittest, coverage run -m unittest|pytest, coverage report --fail-under or
        go test, with {file, line, job, step, command, runner, fail_under: {lines|functions|regions|total: N} | None}.
        The job is the GitHub Actions job id (or Azure DevOps job name) the line sits under; the step its name."""
        gates = []
        for f in ci_files:
            lines = read(self.root / f).splitlines()
            job, jobs_indent, step = None, None, None
            for i, line in enumerate(lines, 1):
                ind = len(line) - len(line.lstrip())
                st = line.strip()
                if re.match(r'^jobs\s*:\s*$', st):
                    jobs_indent, job = ind, None
                    continue
                if jobs_indent is not None and ind == jobs_indent + 2 and re.match(r'^[\w.-]+\s*:\s*$', st):
                    job, step = st.rstrip(':').strip(), None
                    continue
                m = re.match(r'^-\s*(?:job|deployment)\s*:\s*(\S+)', st)
                if m:
                    job, step = m.group(1).strip('\'"'), None
                m = re.match(r'^-?\s*(?:name|displayName)\s*:\s*(.+)$', st)
                if m and (st.startswith('-') or step is None or ind > (jobs_indent or 0) + 2):
                    step = m.group(1).strip().strip('\'"')
                    continue
                if st.startswith('- ') and not re.match(r'^-\s*(run|script|bash|pwsh|powershell)\s*:', st):
                    step = None
                cmd = re.sub(r'^\s*(-\s*)?(run|script|command|sh|bash|pwsh|powershell)\s*:\s*[|>]?[-+]?\s*', '', line).strip()
                if len(cmd) > 1 and cmd[0] == cmd[-1] and cmd[0] in '\'"':
                    cmd = cmd[1:-1]
                if not cmd or cmd.startswith('#') or len(cmd) > 400:
                    continue
                runner = next((r for r, rx in CI_GATE_RUNNERS if rx.search(cmd)), None)
                if not runner:
                    continue
                fail_under = {}
                for rx, key in CI_THRESHOLDS:
                    for mm in rx.finditer(cmd):
                        k = key or mm.group(1)
                        if k not in fail_under:
                            fail_under[k] = float(mm.group(mm.lastindex))
                gates.append({'file': f, 'line': i, 'job': job, 'step': step, 'command': cmd, 'runner': runner,
                              'fail_under': fail_under or None})
        return gates

    def python_runner(self, area: dict, ci_gates: list[dict], has_pytest_config: bool) -> dict | None:
        """{runner: unittest|pytest, reason, evidence, command} for an area with Python tests. unittest when CI runs
        unittest (python -m unittest, coverage run -m unittest) or every test file is a unittest.TestCase module,
        and nothing says pytest (config, conftest.py, a pytest import or marker); pytest otherwise. CI's own command
        is given when there is one."""
        py = [f for f in area['test_files'] if f.endswith('.py')]
        if not py:
            return None
        texts = {f: read(self.root / f, 120_000) for f in py[:200]}
        pytest_files = [f for f, t in texts.items() if re.search(r'^\s*(import pytest|from pytest\b)|@pytest\.', t, re.M)
                        or PurePosixPath(f).name == 'conftest.py']
        testcase_files = [f for f, t in texts.items() if re.search(r'unittest\.TestCase|\(\s*TestCase\s*\)', t)]
        ci_unit = [g for g in ci_gates if g['runner'] in ('unittest', 'coverage-unittest')]
        ci_pyt = [g for g in ci_gates if g['runner'] in ('pytest', 'coverage-pytest')]
        if ci_unit and not ci_pyt and not pytest_files:
            g = ci_unit[0]
            return {'runner': 'unittest', 'reason': 'CI runs the tests with unittest and no test uses pytest',
                    'evidence': [f"{g['file']}:{g['line']}"] + testcase_files[:2], 'command': g['command']}
        if testcase_files and len(testcase_files) == len(texts) and not pytest_files and not has_pytest_config \
                and not ci_pyt:
            d = self._test_dir(py[0]) or '.'
            return {'runner': 'unittest', 'reason': 'every Python test module defines unittest.TestCase classes and '
                    'nothing configures or imports pytest', 'evidence': testcase_files[:3],
                    'command': f'python -m unittest discover -s {d}'}
        if ci_pyt:
            g = ci_pyt[0]
            return {'runner': 'pytest', 'reason': 'CI runs pytest', 'evidence': [f"{g['file']}:{g['line']}"],
                    'command': g['command']}
        if pytest_files or has_pytest_config:
            return {'runner': 'pytest', 'reason': 'pytest is configured or imported by the tests',
                    'evidence': pytest_files[:3], 'command': 'python -m pytest'}
        return {'runner': 'pytest', 'reason': 'assumed: plain test functions with no runner named (pytest also runs '
                'unittest classes); check CI', 'evidence': py[:2], 'command': 'python -m pytest'}

    def run(self, target: str | None) -> dict:
        self.walk()
        roots = self.area_roots()
        self.tooling = self.repo_tooling()
        pkg_names = {}
        for r in roots:
            pj = self.root / r / 'package.json'
            if r != '.' and pj.is_file():
                try:
                    name = json.loads(read(pj)).get('name')
                except (ValueError, AttributeError):
                    name = None
                if isinstance(name, str):
                    pkg_names[r] = name
        ci_files, ci_cmds = self.ci()
        ci_gates = self.ci_gates(ci_files)
        repo_langs: dict[str, dict] = {}
        product_lines: dict[str, int] = {}
        tooling_lines: dict[str, int] = {}
        manifest_dirs = {r for r in roots if r != '.'}
        areas = {r: {'root': r, 'manifests': [], 'languages': {}, 'language_lines': {}, 'frameworks': {}, 'style': {},
                     'test_files': [],
                     'run_commands': [], 'e2e_blocks': {k: [] for k in E2E_BLOCKS}, 'selectors': {},
                     'config_files': []} for r in roots}
        def add_fw(area, cat, name, evidence):
            area['frameworks'].setdefault(cat, {}).setdefault(name, [])
            if evidence not in area['frameworks'][cat][name] and len(area['frameworks'][cat][name]) < 3:
                area['frameworks'][cat][name].append(evidence)
        def add_style(area, cat, name, evidence):
            area['style'].setdefault(cat, {}).setdefault(name, [])
            if evidence not in area['style'][cat][name] and len(area['style'][cat][name]) < 3:
                area['style'][cat][name].append(evidence)

        base_urls, pw_configs = [], []
        for f in self.files:
            p = PurePosixPath(f)
            a = areas[self.owner(f, roots)]
            if p.suffix in LANG_EXT:
                lang = LANG_EXT[p.suffix]
                a['languages'][lang] = a['languages'].get(lang, 0) + 1
                n_lines = sum(1 for x in read(self.root / f).splitlines() if x.strip())
                a['language_lines'][lang] = a['language_lines'].get(lang, 0) + n_lines
                rl = repo_langs.setdefault(lang, {'files': 0, 'lines': 0, 'test_files': 0, 'test_lines': 0})
                rl['files'] += 1
                rl['lines'] += n_lines
                cname = (_lc.LANG_BY_EXT.get(p.suffix) if _lc else None) or lang
                if (_lc.is_test_file(f, manifest_dirs) if _lc else is_test_file(f, self.root)):
                    rl['test_files'] += 1
                    rl['test_lines'] += n_lines
                elif _lc and _lc.is_tooling_file(f):
                    tooling_lines[cname] = tooling_lines.get(cname, 0) + n_lines
                else:
                    product_lines[cname] = product_lines.get(cname, 0) + n_lines
            name = p.name
            if name in MANIFESTS or p.suffix == '.csproj':
                a['manifests'].append(f)
                text = read(self.root / f)
                if name == 'package.json':
                    deps, scripts = deps_of_package_json(text)
                    for d in deps:
                        if d in JS_DEPS: add_fw(a, *JS_DEPS[d], f)
                        if d in JS_STYLE_DEPS: add_style(a, *JS_STYLE_DEPS[d], f)
                    pm = self.tooling.get('package_manager') or 'npm'
                    for k, v in scripts.items():
                        if re.search(r'test|e2e|spec|cy:|cypress|playwright|detox', k + ' ' + str(v), re.I):
                            cmd = f'{pm} test' if k == 'test' and pm != 'bun' else f'{pm} run {k}'
                            a['run_commands'].append({'command': cmd, 'resolves_to': v, 'source': f,
                                                      'origin': 'package.json'})
                        if re.search(r'(^|[\s&;|])node\s+(--[\w-]+(=\S+)?\s+)*--test\b', str(v)):
                            add_fw(a, 'unit', 'node-test', f)
                elif name in ('pyproject.toml', 'setup.py', 'setup.cfg') or name.startswith('requirements'):
                    names = py_dep_names(text)
                    for d, v in PY_DEPS.items():
                        if d in names: add_fw(a, *v, f)
                    for d, v in PY_STYLE_DEPS.items():
                        if d in names: add_style(a, *v, f)
                    if '[tool.pytest' in text: add_fw(a, 'unit', 'pytest', f); a['config_files'].append(f)
                else:
                    group = ('jvm' if name in ('pom.xml', 'build.gradle', 'build.gradle.kts') else 'ruby' if name == 'Gemfile'
                             else 'php' if name == 'composer.json' else 'dart' if name == 'pubspec.yaml'
                             else 'csharp' if p.suffix == '.csproj' else None)
                    low = text.lower()
                    for needle, cat, fw in TEXT_DEPS.get(group, []):
                        if needle.lower() in low:
                            (add_fw if cat in ('unit', 'e2e', 'mobile', 'bdd') else add_style)(a, cat, fw, f)
                    if name == 'go.mod': add_fw(a, 'unit', 'go-testing', f)
                    if name == 'Cargo.toml': add_fw(a, 'unit', 'cargo-test', f)
                    if name == 'go.mod' and 'stretchr/testify' in text: add_style(a, 'assertions', 'testify', f)
            if re.match(r'(playwright|cypress|vitest|vite|jest|wdio|detox|karma)\.conf(ig)?\.[cm]?[jt]s$|^\.detoxrc|^pytest\.ini$|^tox\.ini$|^noxfile\.py$|^conftest\.py$|^jest\.config|^behave\.ini$|^cucumber\.(js|json|ya?ml)$', name):
                a['config_files'].append(f)
                text = read(self.root / f)
                bu = base_url_of(text)
                if bu:
                    base_urls.append({**bu, 'source': f})
                if name.startswith('playwright.config'):
                    pw_configs.append(f)
                fw = {'playwright': 'playwright', 'cypress': 'cypress', 'wdio': 'webdriverio', 'jest': 'jest',
                      'vitest': 'vitest', '.detoxrc': 'detox', 'detox': 'detox', 'pytest': 'pytest',
                      'conftest': 'pytest', 'behave': 'behave', 'cucumber': 'cucumber'}
                for k, v in fw.items():
                    if name.startswith(k):
                        cat = 'e2e' if v in ('playwright', 'cypress', 'webdriverio') else 'mobile' if v == 'detox' \
                            else 'bdd' if v in ('behave', 'cucumber') else 'unit'
                        add_fw(a, cat, v, f)
            if name == 'nextest.toml':   # cargo-nextest config (usually .config/nextest.toml)
                a['config_files'].append(f)
                add_fw(a, 'unit', 'cargo-nextest', f)
            if name == 'Makefile' or name == 'justfile':
                for i, line in enumerate(read(self.root / f).splitlines(), 1):
                    m = re.match(r'^([\w.-]*test[\w.-]*|e2e[\w.-]*)\s*:', line)
                    if m:
                        a['run_commands'].append({'command': f'make {m.group(1)}', 'source': f'{f}:{i}', 'origin': name})
            for block, rx in E2E_BLOCKS.items():
                if rx.search(f) and p.suffix in LANG_EXT and not naming_of(name):
                    if len(a['e2e_blocks'][block]) < 15:
                        a['e2e_blocks'][block].append(f)
            if is_test_file(f, self.root):
                a['test_files'].append(f)

        for f in pw_configs:   # a Playwright config that no package.json script runs still has a command
            owner = self.owner(f, roots)
            a = areas[owner]
            if any('playwright' in f"{c['command']} {c.get('resolves_to', '')}" for c in a['run_commands']):
                continue
            pm = self.tooling.get('package_manager') or 'npm'
            runner = {'pnpm': 'pnpm exec', 'yarn': 'yarn', 'bun': 'bunx'}.get(pm, 'npx')
            rel_cfg = f if owner == '.' else f[len(owner) + 1:]
            a['run_commands'].append({'command': f'{runner} playwright test' + (f' --config {rel_cfg}' if '/' in rel_cfg else ''),
                                      'source': f, 'origin': 'playwright config',
                                      'note': 'no package.json script runs Playwright; run from the area root'})
        # pytest's own collection rules decide which Python files are its tests: every pytest configuration counts, an
        # area's own and one in a folder with no manifest (benchmarks/pytest.ini), each governing the files below it
        cfg_dirs = {}
        for r in areas:
            cfg = pytest_collection(self.root, r)
            if cfg:
                cfg_dirs[r] = cfg
        for f in self.files:
            p = PurePosixPath(f)
            d = str(p.parent) if str(p.parent) not in ('', '.') else '.'
            if p.name in PYTEST_CONFIG_NAMES and d not in cfg_dirs:
                cfg = pytest_collection(self.root, d)
                if cfg:
                    cfg_dirs[d] = cfg

        def governing(f):
            ds = [d for d in cfg_dirs if d == '.' or f.startswith(d + '/')]
            return max(ds, key=lambda d: (d != '.', len(d))) if ds else None
        for d, cfg in sorted(cfg_dirs.items(), key=lambda kv: (kv[0].count('/'), kv[0])):
            r = d if d in areas else self.owner(d, roots)
            a = areas[r]
            base = '' if d == '.' else d + '/'

            def collected(f, cfg=cfg, base=base, d=d):
                if not f.startswith(base) or not f.endswith('.py') or PurePosixPath(f).name == 'conftest.py':
                    return False
                if governing(f) != d:
                    return False
                rel = f[len(base):]
                if cfg['testpaths'] and '.' not in cfg['testpaths'] and not any(
                        rel == t or rel.startswith(t.rstrip('/') + '/') for t in cfg['testpaths']):
                    return False
                return any(fnmatch.fnmatch(PurePosixPath(f).name, p) for p in cfg['python_files'])
            py_tests = [f for f in a['test_files'] if f.endswith('.py') and PurePosixPath(f).name != 'conftest.py'
                        and f.startswith(base) and governing(f) == d]
            outside = sorted(f for f in py_tests if not collected(f))
            extra = sorted(f for f in self.files if self.owner(f, roots) == r and collected(f) and f not in a['test_files'])
            a['test_files'] = [f for f in a['test_files'] if f not in outside] + extra
            entry = {'config': cfg['config'], 'testpaths': cfg['testpaths'], 'python_files': cfg['python_files'],
                     'python_classes': cfg['python_classes'], 'python_functions': cfg['python_functions'],
                     'collected': sum(1 for f in a['test_files'] if collected(f)),
                     'not_collected': outside[:50],
                     **({'note': f'{len(outside)} Python test file(s) are outside pytest\'s testpaths or '
                                 'python_files, so a bare `pytest` does not collect them; they run only when named '
                                 'explicitly (check CI)'} if outside else {})}
            if d == r:
                a['pytest'] = entry
            else:
                a.setdefault('pytest_configs', []).append({**entry, 'dir': d, 'command': f'python -m pytest {d}'})
        declared_at = {r: {n for cat in areas[r]['frameworks'].values() for n in cat} for r in areas}
        declared = lambda a: declared_at[a['root']] | declared_at.get('.', set())  # noqa: E731
        for a in areas.values():
            scanned = a['test_files'][:400]
            if len(a['test_files']) > 400:
                self.limitations.append(f"Area {a['root']}: content signals sampled from 400 of {len(a['test_files'])} test files.")
            fws = set()
            for f in scanned:
                text = read(self.root / f, 120_000)
                for rx, cat, fw in CONTENT_SIGNALS:
                    if code_search(rx, text, f):
                        if fw in GATED and fw not in declared(a) and not code_search(GATED[fw], text, f):
                            continue
                        add_fw(a, cat, fw, f); fws.add(fw)
                for cat, sigs in STYLE_SIGNALS.items():
                    for needle, label in sigs:
                        if needle in text:
                            add_style(a, cat, label, f)
                for m in re.finditer(r'@pytest\.mark\.(\w+)', text):
                    if m.group(1) not in ('parametrize', 'asyncio', 'skip', 'skipif', 'xfail', 'usefixtures'):
                        add_style(a, 'markers', f'@pytest.mark.{m.group(1)}', f)
                for m in re.finditer(r'[\'"`][^\'"`\n]*?(@[A-Za-z][\w-]+)', text):
                    if re.match(r'@(smoke|regression|e2e|slow|critical|JRN-|sanity|p[0-3]\b)', m.group(1), re.I):
                        add_style(a, 'markers', 'title tags (@tag)', f); break
                if re.search(r'@Tag\(|tags:\s*\[|\{\s*tag:\s*', text):
                    add_style(a, 'markers', 'framework tags', f)
                if 'storageState' in text or 'storage_state' in text:
                    if f not in a['e2e_blocks']['login_helpers']: a['e2e_blocks']['login_helpers'].append(f)
                if level_of(f, fws, repo=self.root) == 'e2e' or any(x in fws for x in ('playwright', 'cypress', 'detox', 'appium', 'maestro', 'xcuitest', 'espresso')):
                    for k, rx in SELECTOR_SIGNALS.items():
                        n = len(rx.findall(text))
                        if n:
                            a['selectors'][k] = a['selectors'].get(k, 0) + n
            a['_fws'] = fws

        result_areas = []
        for r, a in areas.items():
            if not a['test_files'] and not a['frameworks'] and not a['manifests']:
                continue
            levels = {}
            for f in a['test_files']:
                lv = level_of(f, a['_fws'], r, repo=self.root)
                levels[lv] = levels.get(lv, 0) + 1
            naming = {}
            for f in a['test_files']:
                lab = naming_of(PurePosixPath(f).name)
                if lab: naming[lab] = naming.get(lab, 0) + 1
            in_dirs = [f for f in a['test_files'] if self._test_dir(f, True)]
            layout = ('none' if not a['test_files'] else 'test_dirs' if len(in_dirs) >= 0.8 * len(a['test_files'])
                      else 'colocated' if len(in_dirs) <= 0.2 * len(a['test_files']) else 'mixed')
            test_dirs = sorted({self._test_dir(f) for f in in_dirs})[:15]
            fw_names = {n for cat in a['frameworks'].values() for n in cat}
            cmds = [c for c in ci_cmds if self._ci_applies(c, r, fw_names, [x for x in roots if x != '.'],
                                                           pkg_names.get(r))] + a['run_commands']
            sel = a['selectors']
            stable = {k: v for k, v in sel.items() if k in ('data-testid', 'role', 'label', 'accessibility-id', 'text')}
            prefer = ['role', 'label', 'data-testid', 'accessibility-id', 'text']
            convention = max(stable, key=lambda k: (stable[k], -prefer.index(k))) if stable else None
            area_base = [b for b in base_urls if r == '.' or b['source'].startswith(r + '/')]
            fingerprint = {
                'root': r,
                'has_tests': bool(a['test_files']),
                'languages': dict(sorted(a['languages'].items(), key=lambda kv: -kv[1])),
                'language_lines': dict(sorted(a['language_lines'].items(), key=lambda kv: -kv[1])),
                'manifests': a['manifests'],
                'frameworks': a['frameworks'],
                'config_files': sorted(set(a['config_files'])),
                'test_files_count': len(a['test_files']),
                'levels': levels,
                'layout': {'style': layout, 'test_dirs': test_dirs, 'naming': naming,
                           'mirrors_source': self._mirrors(a['test_files'])},
                'run_commands': self._dedupe(cmds),
                'style': a['style'],
                'e2e': {**a['e2e_blocks'], 'base_url': area_base[0] if area_base else None,
                        'selectors': sel, 'selector_convention': convention},
            }
            if a.get('pytest'):
                fingerprint['pytest'] = a['pytest']
            if a.get('pytest_configs'):
                fingerprint['pytest_configs'] = a['pytest_configs']
            pr = self.python_runner(a, [g for g in ci_gates if self._ci_applies(g, r, fw_names, [x for x in roots
                                                                                         if x != '.'], pkg_names.get(r))],
                                    bool(a.get('pytest') or a.get('pytest_configs')) or 'pytest' in fw_names - {'unittest'} and any(
                                        'pytest' in c for c in a['config_files'] + a['manifests']))
            if pr:
                fingerprint['python_runner'] = pr
            if not a['test_files']:
                fingerprint['proposal'] = self._proposal(fingerprint)
            result_areas.append(fingerprint)

        out = {
            'repository': self.root.name,
            'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'ci_files': ci_files,
            'tooling': self.tooling,
            'areas': result_areas,
            'has_tests': any(x['has_tests'] for x in result_areas),
            'ci_gates': ci_gates,
            'languages': dict(sorted(repo_langs.items(), key=lambda kv: -kv[1]['lines'])),
            'dominant_language': (max(product_lines or tooling_lines, key=lambda k: (product_lines or tooling_lines)[k])
                                  if (product_lines or tooling_lines) else None),
            'dominant_by': 'product lines (lang_census rule: test files and tooling folders counted apart)',
            'build_output': self.build_output,
            'self_tests': [{**s, 'area': self.owner(s['file'], roots)} for s in find_self_tests(self.root, self.files)],
            **({'fixtures_excluded': self.fixtures} if self.exclude_fixtures else {}),
            'limitations': self.limitations + [
                f"{b['path']}/ was not scanned: {b['reason']} (pass --include {b['path']} if it is source)"
                for b in self.build_output if b['decision'] == 'build_output'] + [
                'Static detection only: conventions inferred from manifests, config and test file contents.',
                'Run commands from CI are listed first; verify them before relying on them.',
            ] + ([] if ci_files else ['No CI configuration found; run commands come from manifests and Makefiles only.']),
        }
        if target:
            out['target'] = target
            out['target_area'] = self.owner(target.replace('\\', '/').strip('/'), [x['root'] for x in result_areas] or ['.'])
            out['exemplars'] = self.exemplars(target, [a for a in areas.values() if a['test_files']])
        return out

    def _test_dir(self, f: str, strict: bool = False) -> str | None:
        """The test directory f sits in. A test-named directory that is a product package (a workspace package
        named e2e with its own manifest and src/) does not count. strict: None when f is in no test directory."""
        parts = PurePosixPath(f).parts
        skip = len(parts) - len(PurePosixPath(strip_product_dirs(f, self.root)).parts)
        for i, x in enumerate(parts[:-1]):
            if i < skip:
                continue
            if x in TEST_DIR_PARTS or x.endswith('Tests') or (x == 'test' and i and parts[i - 1] == 'src'):
                return '/'.join(parts[:i + 1])
        return None if strict else str(PurePosixPath(f).parent)

    def _mirrors(self, tests: list[str]):
        """True when most tests under a test directory have a same-named source file under a parallel path."""
        if len(tests) < 3:
            return None
        sources = getattr(self, '_sources', None)
        if sources is None:
            sources = self._sources = {}
            for f in self.files:
                if not is_test_file(f, self.root):
                    sources.setdefault(PurePosixPath(f).stem.lower(), []).append(f)
        hits = considered = 0
        for t in tests:
            p = PurePosixPath(t)
            stem = re.sub(r'(^test_|_test$|\.test$|\.spec$|\.cy$|Tests?$|_spec$|IT$)', '', p.stem).lower()
            tail = [x.lower() for x in p.parts[:-1] if x not in TEST_DIR_PARTS and x not in ('src', 'test', 'java', 'kotlin')]
            if not tail:
                continue
            considered += 1
            for src in sources.get(stem, []):
                sparts = [x.lower() for x in PurePosixPath(src).parts[:-1]]
                if tail[-1] in sparts:
                    hits += 1; break
        return (hits >= 0.5 * considered) if considered >= 3 else None

    @staticmethod
    def _step_lines(lines: list[str], idx: int) -> list[str]:
        """The lines of the YAML list item (CI step) containing lines[idx]."""
        indent = lambda s: len(s) - len(s.lstrip())
        start = idx
        while start > 0 and not lines[start].lstrip().startswith('- '):
            start -= 1
        base = indent(lines[start])
        end = start + 1
        while end < len(lines) and (not lines[end].strip() or indent(lines[end]) > base):
            end += 1
        return lines[start:end]

    @staticmethod
    def _ci_applies(cmd: dict, root: str, frameworks: set[str], roots: list[str], pkg_name: str | None = None) -> bool:
        """A CI command belongs to an area if it runs there, filters to its workspace package (pnpm/turbo --filter,
        npm --workspace, yarn workspace, lerna --scope), names it, or (unscoped) uses its framework."""
        if root == '.':
            return True
        wd = cmd.get('working_directory')
        if wd:
            return wd == root or wd.startswith(root + '/')
        text = cmd['command']
        targets = workspace_filters(text)
        if targets:
            return any(filter_matches(t, root, pkg_name) for t in targets)
        names = lambda r: re.search(r'(^|[\s/=])' + re.escape(r) + r'(/|\s|$)', text)
        if names(root):
            return True
        if any(names(x) for x in roots):
            return False  # names a different area
        tool_fw = {'pytest': 'pytest', 'playwright': 'playwright', 'cypress': 'cypress', 'jest': 'jest', 'vitest': 'vitest',
                   'go test': 'go-testing', 'cargo test': 'cargo-test', 'cargo nextest': 'cargo-nextest',
                   'node --test': 'node-test',
                   'flutter test': 'flutter_test', 'rspec': 'rspec',
                   'dotnet test': 'xunit', 'detox': 'detox', 'maestro': 'maestro', 'phpunit': 'phpunit', 'behave': 'behave',
                   'gradle': 'junit5', 'mvn': 'junit5'}
        return any(k in text and v in frameworks for k, v in tool_fw.items())

    @staticmethod
    def _dedupe(cmds):
        seen, out = set(), []
        for c in sorted(cmds, key=lambda c: 0 if c['origin'] == 'ci' else 1):
            if c['command'] not in seen:
                seen.add(c['command']); out.append(c)
        return out[:20]

    @staticmethod
    def _proposal(fp: dict) -> dict:
        langs = fp['languages']
        top = next(iter(langs), None)
        defaults = {'python': 'pytest', 'typescript': 'vitest (or jest if the project does not use Vite)',
                    'javascript': 'vitest (or jest if the project does not use Vite)', 'java': 'JUnit 5', 'kotlin': 'JUnit 5',
                    'go': 'go testing', 'ruby': 'RSpec', 'csharp': 'xUnit', 'php': 'PHPUnit', 'rust': 'cargo test',
                    'dart': 'flutter_test', 'swift': 'XCTest'}
        return {'reason': 'No existing tests found in this area.',
                'unit_default': defaults.get(top), 'e2e_default_web': 'Playwright',
                'e2e_default_mobile': {'dart': 'Flutter integration_test', 'swift': 'XCUITest', 'kotlin': 'Espresso / Compose UI test',
                                       'java': 'Espresso'}.get(top, 'Detox (React Native) or Maestro'),
                'note': 'Proposal only. Do not install a framework without the user agreeing.'}

    def exemplars(self, target: str, areas: list[dict]) -> list[dict]:
        t = PurePosixPath(target.replace('\\', '/').strip('/'))
        tparts = [x.lower() for x in t.parts]
        tstem = re.sub(r'\W', '', t.stem.lower().split('.')[0])
        tlevel = level_of(t.as_posix(), set(), self.owner(t.as_posix(), [a['root'] for a in areas]), repo=self.root)
        scored = []
        for a in areas:
            for f in a['test_files']:
                fp = PurePosixPath(f)
                fparts = [x.lower() for x in fp.parts]
                common = 0
                for x, y in zip(tparts, fparts):
                    if x != y: break
                    common += 1
                shared = len(set(tparts) & set(fparts))
                name_bonus = 3 if tstem and tstem in re.sub(r'\W', '', fp.name.lower()) else 0
                same_area = 2 if t.as_posix().startswith(a['root'] + '/') or a['root'] == '.' else 0
                size = (self.root / f).stat().st_size if (self.root / f).exists() else 0
                size_ok = 1 if 300 <= size <= 20_000 else 0
                same_level = 3 if level_of(f, set(), a['root'], repo=self.root) == tlevel else 0
                score = common * 2 + shared + name_bonus + same_area + size_ok + same_level
                if score >= 3:
                    scored.append((score, f, a['root']))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [{'path': f, 'area': r, 'score': s} for s, f, r in scored[:3]]

BASE_URL_RX = re.compile(r'base_?url\s*[:=]\s*', re.I)
ENV_RX = re.compile(r'process\.env\.(\w+)|process\.env\[\s*[\'"](\w+)[\'"]\s*\]|import\.meta\.env\.(\w+)|'
                    r'os\.environ(?:\.get\(\s*|\[\s*|\.setdefault\(\s*)[\'"](\w+)[\'"]|os\.getenv\(\s*[\'"](\w+)[\'"]|'
                    r'Cypress\.env\(\s*[\'"](\w+)[\'"]')
STR_RX = re.compile(r'([\'"`])((?:\\.|(?!\1).)*)\1')


def base_url_of(text: str) -> dict | None:
    """The base URL a test config sets: {value, expression, env, default}. value is the URL used when nothing is set:
    the literal, or the fallback after ?? / || (or os.environ.get's default) when the URL comes from an environment
    variable; env names that variable. A URL computed some other way keeps value = the expression."""
    m = BASE_URL_RX.search(text)
    if not m:
        return None
    expr, depth, quote = [], 0, None
    for ch in text[m.end():m.end() + 400]:
        if quote:
            quote = None if ch == quote else quote
        elif ch in '\'"`':
            quote = ch
        elif ch in '([{':
            depth += 1
        elif ch in ')]}':
            if depth == 0:
                break
            depth -= 1
        elif ch in ',;\n' and depth == 0:
            break
        expr.append(ch)
    expr = ''.join(expr).strip()
    if not expr:
        return None
    env = ENV_RX.search(expr)
    env_name = next((g for g in env.groups() if g), None) if env else None
    out = {'value': expr, 'expression': expr}
    if env_name:
        out['env'] = env_name
        rest = expr[env.end():]
        fb = re.match(r'\s*(?:\)\s*)?(?:\?\?|\|\||\bor\b|,)\s*' + STR_RX.pattern, rest)
        out['default'] = fb.group(2) if fb else None
        if fb:
            out['value'] = fb.group(2)
    else:
        lit = STR_RX.fullmatch(expr)
        if lit:
            out['value'] = lit.group(2)
    return out


CI_RUST_UNITTEST = """name: ci
on: [push]
jobs:
  rust:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Coverage gate
        run: cargo llvm-cov --workspace --all-features --fail-under-lines 90 --fail-under-functions 85
      - name: Tests
        run: |
          cargo nextest run --workspace
          cargo test --doc
  python:
    runs-on: ubuntu-latest
    steps:
      - name: Helper script tests
        run: |
          coverage run -m unittest discover -s tests
          coverage report --fail-under=80
      - run: python -m unittest discover -s tests -p "test_*.py"
      - name: Install tools
        run: |
          pip install pytest pytest-cov
          python -m pip install --upgrade pytest
          uv pip install pytest-xdist
          pipx run pytest-check-links docs
          echo "pytest is not used here"
"""


def _rust_unittest_checks() -> list:
    """A tide-table Cargo workspace with Python helper scripts tested by unittest, and a GitHub workflow with a
    cargo llvm-cov gate, nextest, and coverage run -m unittest."""
    import tempfile
    rs = 'pub fn level(h: f64) -> f64 {\n    h * 2.0\n}\n' * 30
    files = {'Cargo.toml': '[workspace]\nmembers = ["crates/*"]\n',
             'crates/tides-core/Cargo.toml': '[package]\nname = "tides-core"\n',
             'crates/tides-core/src/lib.rs': rs + '#[cfg(test)]\nmod tests {\n    #[test]\n    fn t() {}\n}\n',
             'crates/tides-core/src/table.rs': rs,
             'crates/tides-core/tests/levels.rs': '#[test]\nfn levels() {}\n',
             '.github/workflows/ci.yml': CI_RUST_UNITTEST}
    for i in range(6):
        files[f'scripts/report{i}.py'] = 'def build():\n    return 1\n'
        files[f'tests/test_report{i}.py'] = ('import sys, unittest\nfrom pathlib import Path\n'
                                             'sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))\n'
                                             f'import report{i}\n\n\nclass ReportTest(unittest.TestCase):\n'
                                             '    def test_build(self):\n        self.assertEqual(report0.build(), 1)\n')
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for rel_path, body in files.items():
            (root / rel_path).parent.mkdir(parents=True, exist_ok=True)
            (root / rel_path).write_text(body, encoding='utf-8')
        try:
            res = Detector(root, 10_000).run(None)
        except Exception as e:   # noqa: BLE001
            return [('detect_conventions runs on a Cargo workspace with unittest helpers', False, repr(e))]
    areas = {a['root']: a for a in res['areas']}
    gates = res.get('ci_gates') or []
    by = {g.get('runner'): g for g in gates}
    cov = by.get('cargo-llvm-cov', {})
    checks = [
        ('ci_gates: cargo llvm-cov with its --fail-under thresholds, the workflow file, line, job and step',
         cov.get('file') == '.github/workflows/ci.yml' and cov.get('line') == 9 and cov.get('job') == 'rust'
         and cov.get('step') == 'Coverage gate' and cov.get('fail_under') == {'lines': 90.0, 'functions': 85.0}, gates),
        ('ci_gates: nextest, cargo test, coverage run -m unittest with its report threshold, python -m unittest',
         {'cargo-nextest', 'cargo-test', 'coverage-unittest', 'unittest'} <= set(by)
         and by.get('coverage-unittest', {}).get('line') == 19 and by.get('coverage-unittest', {}).get('step') == 'Helper script tests' and by.get('coverage-unittest', {}).get('job') == 'python'
         and (by.get('coverage-report') or {}).get('fail_under') == {'total': 80.0}, sorted(by)),
        ('ci_gates: installing pytest (pip install pytest pytest-cov) or mentioning it is not a pytest gate',
         not [g for g in gates if g.get('runner') in ('pytest', 'coverage-pytest')],
         [(g.get('line'), g.get('command')) for g in gates if g.get('runner') in ('pytest', 'coverage-pytest')]),
        ('CI run commands include cargo llvm-cov and coverage run -m unittest',
         {'cargo llvm-cov --workspace --all-features --fail-under-lines 90 --fail-under-functions 85',
          'coverage run -m unittest discover -s tests'} <= {c['command'] for c in areas.get('.', {}).get('run_commands', [])},
         [c['command'] for c in areas.get('.', {}).get('run_commands', [])]),
        ('the Python runner is unittest when the tests are TestCase classes and CI runs unittest (not assumed pytest)',
         (areas.get('.', {}).get('python_runner') or {}).get('runner') == 'unittest'
         and 'coverage run -m unittest discover -s tests' in (areas.get('.', {}).get('python_runner') or {}).get('command', ''),
         areas.get('.', {}).get('python_runner')),
        ('per-area languages carry lines, Rust included',
         (areas.get('crates/tides-core', {}).get('language_lines') or {}).get('rust', 0) >= 180,
         areas.get('crates/tides-core', {}).get('language_lines')),
        ('repository languages (files and lines) and the dominant one by product lines',
         (res.get('languages') or {}).get('rust', {}).get('lines', 0) > 0 and res.get('dominant_language') == 'Rust',
         (res.get('languages'), res.get('dominant_language'))),
    ]
    return checks


def self_test() -> int:
    """Check product packages named e2e, gated framework signals, node:test, workspace filters and tooling."""
    import tempfile
    files = {
        'package.json': json.dumps({'name': 'shop-monorepo', 'private': True, 'packageManager': 'pnpm@10.2.1',
                                    'scripts': {'test': 'pnpm -r run test', 'test:scripts': 'node --test "scripts/**/*.test.ts"',
                                                'lint': 'oxlint'},
                                    'devDependencies': {'oxlint': '1.0.0', 'typescript': '7.0.0'}}),
        'pnpm-workspace.yaml': 'packages:\n  - packages/*\n  - apps/*\n',
        '.changeset/config.json': '{}', '.changeset/brave-cats.md': '---\n"e2e": minor\n---\nA change.\n',
        'scripts/release.test.ts': "import { test } from 'node:test';\ntest('x', () => {});\n",
        'packages/e2e/package.json': json.dumps({'name': 'e2e', 'exports': './dist/index.js',
                                                 'devDependencies': {'vitest': '5.0.0'}}),
        'packages/e2e/src/browser.ts': 'export const browser = {}\n',
        'packages/e2e/tests/unit/browser.test.ts': "import { test } from 'vitest';\ntest('opens', async () => { await browser.url('/'); });\n",
        'packages/e2e/tests/integration/session.test.ts': "import { test } from 'vitest';\ntest('session', () => {});\n",
        'packages/basket/package.json': json.dumps({'name': '@shop/basket', 'main': 'index.js',
                                                    'devDependencies': {'vitest': '5.0.0'}}),
        'packages/basket/index.js': 'module.exports = {}\n',
        'packages/basket/tests/basket.test.ts': "import { test } from 'vitest';\ntest('adds', () => {});\n",
        'apps/site/package.json': json.dumps({'name': '@shop/site', 'devDependencies': {'@wdio/cli': '9.0.0'}}),
        'apps/site/e2e/home.e2e.ts': "describe('home', () => { it('loads', async () => { await browser.url('/'); }); });\n",
        '.github/workflows/ci.yml': 'jobs:\n  t:\n    steps:\n      - run: pnpm --filter @shop/basket exec vitest run\n'
                                    '      - run: pnpm --filter e2e run test\n',
        # a Python service whose pytest config narrows collection to tests/ and adds a check_*.py pattern
        'svc/pyproject.toml': '[project]\nname = "timetable"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
                              'python_files = ["test_*.py", "check_*.py"]\naddopts = "-v"\n',
        'svc/timetable/__init__.py': '', 'svc/tests/test_routes.py': 'def test_a():\n    assert 1\n',
        'svc/tests/check_data.py': 'def test_b():\n    assert 1\n',
        'svc/tools/tests/test_sync.py': 'def test_c():\n    assert 1\n',
        # a FastAPI app serving a committed, hand-written dist/ folder, and a Vite app whose dist/ is built
        'web/server.py': 'from fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory="dist"))\n',
        'web/dist/app.js': 'function start() {}\n',
        'spa/package.json': json.dumps({'name': 'spa', 'scripts': {'build': 'vite build'}}),
        'spa/dist/index.js': 'export const a = 1\n',
        # a Playwright suite with no test script, whose baseURL falls back to a default when the variable is unset
        'apps/birds/package.json': json.dumps({'name': 'birds', 'devDependencies': {'@playwright/test': '1.50.0'}}),
        'apps/birds/playwright.config.ts': ("import { defineConfig } from '@playwright/test';\nexport default defineConfig({\n"
                                            "  testDir: './e2e',\n"
                                            "  use: { baseURL: process.env.BIRDS_URL ?? \"http://127.0.0.1:8520\", },\n});\n"),
        'apps/birds/e2e/home.spec.ts': "import { test } from '@playwright/test';\ntest('home', async ({ page }) => {});\n",
        # benchmarks in a folder with no manifest, collected by their own pytest.ini (testpaths = .)
        'bench/pytest.ini': '[pytest]\ntestpaths = .\npython_files = bench_*.py\npython_functions = bench_* test_*\n',
        'bench/bench_speed.py': 'def bench_parse():\n    assert 1\n',
        # a tool package whose scripts carry their own self-tests, and a self_test.py holding fixture sources in strings
        'tool/pyproject.toml': '[project]\nname = "tool"\n',
        'tool/scan.py': ('"""Scan things. Run with --self-test to check it."""\nimport argparse\n\n\ndef main():\n'
                         '    ap = argparse.ArgumentParser()\n    ap.add_argument("--self-test", action="store_true")\n'
                         '    ap.parse_args()\n\n\nif __name__ == "__main__":\n    main()\n'),
        'tool/check.py': ('import sys\n\n\ndef self_test():\n    return 0\n\n\n'
                          'if __name__ == "__main__":\n    raise SystemExit(self_test())\n'),
        'tool/quick.py': 'import sys\nif "--self-test" in sys.argv[1:]:\n    print("ok    adds")\n',
        'tool/notes.py': '"""Mentions python x.py --self-test in prose only."""\nX = "run it with --self-test later"\n',
        'tool/self_test.py': ('"""Checks for the scanner."""\nGO = """\nfunc TestAdd(t *testing.T) {}\n"""\n'
                              'JS = "import { test } from \'node:test\'"\n\n\ndef main():\n    return 0\n\n\n'
                              'if __name__ == "__main__":\n    raise SystemExit(main())\n'),
        # a product module named like a test (outside every test directory, no test in it, imported by the tool), a
        # real test beside it, and a test-support module inside tests/
        'tool/test_paths.py': 'def is_test_path(p):\n    return p.startswith("tests/")\n',
        'tool/report.py': 'from test_paths import is_test_path\n',
        'tool/test_report.py': 'import unittest\n\n\nclass TestReport(unittest.TestCase):\n    def test_a(self):\n'
                               '        self.assertTrue(1)\n',
        'lib/tests/test_helpers.py': 'def make_row():\n    return {}\n',
        # a planted sample project under examples/
        'examples/shop/package.json': json.dumps({'name': 'shop', 'devDependencies': {'vitest': '5.0.0'}}),
        'examples/shop/tests/cart.test.ts': "import { test } from 'vitest';\ntest('cart', () => {});\n",
    }
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for rel_path, body in files.items():
            (root / rel_path).parent.mkdir(parents=True, exist_ok=True)
            (root / rel_path).write_text(body, encoding='utf-8')
        det = Detector(root, 10_000)
        res = det.run(None)
        try:
            res_fx = Detector(root, 10_000, exclude_fixtures=True).run(None)
        except TypeError as e:   # an older Detector without the option
            res_fx = {'error': str(e)}
        # find_self_tests reads each file once: a second call (same root, unchanged files) reads nothing, and a changed
        # file is read again
        g = globals()
        real_read, reads = g['read'], []
        g['read'] = lambda p, *a, **k: reads.append(p) or real_read(p, *a, **k)
        try:
            find_self_tests(root, det.files)
            first = len(reads)
            find_self_tests(root, det.files)
            second = len(reads) - first
            with open(root / 'tool' / 'notes.py', 'a', encoding='utf-8') as fh:
                fh.write('\n\nif __name__ == "__main__":\n    import sys\n    print("--self-test" in sys.argv)\n')
            again = find_self_tests(root, det.files)
            third = len(reads) - first - second
        finally:
            g['read'] = real_read
        try:
            named = (is_test_file('tool/test_paths.py', root), is_test_file('tool/test_paths.py'),
                     is_test_file('tool/test_report.py', root), is_test_file('lib/tests/test_helpers.py', root))
        except TypeError as e:
            named = str(e)
    areas = {a['root']: a for a in res['areas']}
    fws = lambda r: {n for cat in areas[r]['frameworks'].values() for n in cat}  # noqa: E731
    cmds = lambda r: [c['command'] for c in areas[r]['run_commands']]  # noqa: E731
    checks = [
        ('a product package named e2e is not a test directory', areas['packages/e2e']['layout']['test_dirs'] == ['packages/e2e/tests'],
         areas['packages/e2e']['layout']),
        ('its integration tests are integration, not e2e', areas['packages/e2e']['levels'] == {'unit': 1, 'integration': 1},
         areas['packages/e2e']['levels']),
        ('browser.url( without WebdriverIO declared is not WebdriverIO', 'webdriverio' not in fws('packages/e2e'), fws('packages/e2e')),
        ('browser.url( with WebdriverIO declared is WebdriverIO', 'webdriverio' in fws('apps/site'), fws('apps/site')),
        ('node --test is recognised', 'node-test' in fws('.'), fws('.')),
        ('a --filter command goes to the filtered package only',
         'pnpm --filter @shop/basket exec vitest run' in cmds('packages/basket')
         and 'pnpm --filter @shop/basket exec vitest run' not in cmds('packages/e2e')
         and 'pnpm --filter e2e run test' in cmds('packages/e2e'), (cmds('packages/basket'), cmds('packages/e2e'))),
        ('package.json scripts use the package manager', 'pnpm run test:scripts' in cmds('.'), cmds('.')),
        ('pytest testpaths and python_files decide the Python test files',
         'svc' in areas and areas['svc']['test_files_count'] == 2
         and (areas['svc'].get('pytest') or {}).get('not_collected') == ['svc/tools/tests/test_sync.py']
         and (areas['svc'].get('pytest') or {}).get('python_files') == ['test_*.py', 'check_*.py'],
         areas.get('svc', {}).get('pytest') if 'svc' in areas else sorted(areas)),
        ('tooling', res['tooling'].get('package_manager') == 'pnpm' and res['tooling'].get('changesets', {}).get('pending') == 1
         and 'oxlint' in res['tooling'].get('linters', {}) and res['tooling'].get('workspaces') == ['packages/*', 'apps/*']
         and 'note' in res['tooling'].get('typescript', {}), res['tooling']),
        ('a served dist/ is walked as source and a built one is not, each recorded with its reason',
         'web/dist/app.js' in det.files and 'spa/dist/index.js' not in det.files
         and {b['path']: b['decision'] for b in res.get('build_output') or []} == {'web/dist': 'source',
                                                                                   'spa/dist': 'build_output'},
         res.get('build_output')),
        ('a baseURL with a ?? fallback keeps the environment variable and the default',
         'apps/birds' in areas
         and {k: (areas['apps/birds']['e2e']['base_url'] or {}).get(k) for k in ('value', 'env', 'default')}
         == {'value': 'http://127.0.0.1:8520', 'env': 'BIRDS_URL', 'default': 'http://127.0.0.1:8520'},
         (areas.get('apps/birds', {}).get('e2e') or {}).get('base_url')),
        ('a Playwright config with no test script still gives a run command',
         'pnpm exec playwright test' in (cmds('apps/birds') if 'apps/birds' in areas else []), cmds('apps/birds') if 'apps/birds' in areas else sorted(areas)),
        ('a pytest.ini in a folder with no manifest (testpaths = .) decides its test files, with python_functions',
         any(c.get('config') == 'bench/pytest.ini' and c.get('python_functions') == ['bench_*', 'test_*']
                 and c.get('collected') == 1
                 for c in areas['.'].get('pytest_configs', [])), areas['.'].get('pytest_configs')),
        ('framework markers inside string literals are not frameworks',
         'tool' in areas and not ({'go-testing', 'node-test'} & fws('tool')), fws('tool') if 'tool' in areas else sorted(areas)),
        ('self-tests are listed with their commands: an argparse --self-test flag, a literal check, and a self_test() '
         'called under __main__; prose that only mentions the flag is not one',
         {(s['file'], s['command'], s['kind']) for s in res.get('self_tests', [])} == {
             ('tool/scan.py', 'python tool/scan.py --self-test', 'flag'),
             ('tool/quick.py', 'python tool/quick.py --self-test', 'flag'),
             ('tool/check.py', 'python tool/check.py', 'function'),
             ('tool/self_test.py', 'python tool/self_test.py', 'script')}, res.get('self_tests')),
        ('fixture projects are areas by default, and left out (and recorded) with exclude_fixtures',
         'examples/shop' in areas and 'examples/shop' not in {a['root'] for a in res_fx.get('areas', [])}
         and [f['dir'] for f in res_fx.get('fixtures_excluded', [])] == ['examples/shop']
         and 'fixtures_excluded' not in res, res_fx.get('fixtures_excluded', res_fx.get('error'))),
    ]
    checks += [
        ('find_self_tests reads a file once per root: a repeated call reads nothing, a changed file is read again',
         second == 0 and third == 1 and 'tool/notes.py' in {s['file'] for s in again}, (first, second, third)),
        ('a test_*.py outside every test directory with no test in it is product code (given the root); a real test '
         'beside it and a support module in tests/ stay test files',
         named == (False, True, True, True), named),
        ('the Detector lists that module as source, not as a test file',
         areas['tool']['test_files_count'] == 2, areas['tool']['test_files_count']),   # test_report.py, self_test.py
    ]
    checks += _rust_unittest_checks()
    failed = 0
    for name, ok, extra in checks:
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} {name}" + ('' if ok else f'  [{extra}]'))
    print(f'{failed} failed' if failed else 'ALL PASS')
    return 1 if failed else 0


def main(argv=None) -> int:
    if '--self-test' in (sys.argv[1:] if argv is None else argv):
        return self_test()
    ap = argparse.ArgumentParser(description='Fingerprint the testing conventions of a repository.')
    ap.add_argument('repo', nargs='?', default='.')
    ap.add_argument('--target', help='path (file or directory) where new tests or code will go; selects exemplars')
    ap.add_argument('--out', help='write JSON here instead of stdout')
    ap.add_argument('--max-files', type=int, default=50_000)
    ap.add_argument('--include', action='append', default=[], metavar='DIR',
                    help='scan this folder even if it is named like build output (e.g. a hand-written dist/)')
    ap.add_argument('--exclude-fixtures', action='store_true',
                    help='leave out nested sample and fixture projects (repo_layout.fixture_projects); listed under '
                         'fixtures_excluded')
    ap.add_argument('--fixture-dir', action='append', default=[], metavar='NAME|PATH',
                    help='with --exclude-fixtures: also treat this directory as a fixture parent')
    args = ap.parse_args(argv)
    root = Path(args.repo)
    if not root.is_dir():
        print(f'Not a directory: {root}', file=sys.stderr); return 2
    res = Detector(root, args.max_files, args.include, args.exclude_fixtures, args.fixture_dir).run(args.target)
    if res.get('fixtures_excluded'):
        print(f"{len(res['fixtures_excluded'])} fixture project(s) left out: "
              + ', '.join(f['dir'] for f in res['fixtures_excluded'][:5])
              + (' ...' if len(res['fixtures_excluded']) > 5 else ''), file=sys.stderr)
    for b in res.get('build_output') or []:
        print(f"{b['decision']}: {b['reason']}", file=sys.stderr)
    text = json.dumps(res, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text + '\n', encoding='utf-8')
        n = sum(a['test_files_count'] for a in res['areas'])
        print(f"{len(res['areas'])} area(s), {n} test file(s), {len(res['ci_files'])} CI file(s) -> {args.out}")
    else:
        print(text)
    return 0

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
