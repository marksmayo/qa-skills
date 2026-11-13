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
"""
from __future__ import annotations
import argparse, json, os, re, sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

SKIP_DIRS = {'.git', 'node_modules', 'vendor', 'dist', 'build', 'out', 'target', '.next', '.nuxt',
             '.svelte-kit', '.venv', 'venv', 'env', '__pycache__', '.tox', '.nox', '.gradle', '.idea',
             'Pods', 'DerivedData', 'coverage', '.dart_tool', 'bin', 'obj', '.terraform', '.cache',
             'playwright-report', 'test-results', 'site-packages', '.mypy_cache', '.pytest_cache', '.turbo',
             '.ruff_cache', '.hg', '.svn'}
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
                       r'dotnet test|cargo (nextest run|test)|flutter test|phpunit|xcodebuild (\S+ )*test|maestro test|detox test|behave)\b')

def rel(p: Path, root: Path) -> str:
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

def is_test_file(path: str) -> bool:
    p = PurePosixPath(path)
    if p.suffix not in LANG_EXT and p.suffix not in ('.yaml', '.yml'):
        return False
    if naming_of(p.name):
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
    def __init__(self, root: Path, max_files: int):
        self.root = root.resolve()
        self.max_files = max_files
        self.files: list[str] = []
        self.limitations: list[str] = []
        self.tooling: dict = {}

    def walk(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith('.egg-info'))
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
        areas = {r: {'root': r, 'manifests': [], 'languages': {}, 'frameworks': {}, 'style': {}, 'test_files': [],
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

        base_urls = []
        for f in self.files:
            p = PurePosixPath(f)
            a = areas[self.owner(f, roots)]
            if p.suffix in LANG_EXT:
                lang = LANG_EXT[p.suffix]
                a['languages'][lang] = a['languages'].get(lang, 0) + 1
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
                m = re.search(r'base_?url\s*[:=]\s*[\'"`]?([^\'"`,\s]+)', text, re.I)
                if m:
                    base_urls.append({'value': m.group(1), 'source': f})
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
            if is_test_file(f):
                a['test_files'].append(f)

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
                    if rx.search(text):
                        if fw in GATED and fw not in declared(a) and not GATED[fw].search(text):
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
            'limitations': self.limitations + [
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
                if not is_test_file(f):
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
    }
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for rel_path, body in files.items():
            (root / rel_path).parent.mkdir(parents=True, exist_ok=True)
            (root / rel_path).write_text(body, encoding='utf-8')
        res = Detector(root, 10_000).run(None)
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
        ('tooling', res['tooling'].get('package_manager') == 'pnpm' and res['tooling'].get('changesets', {}).get('pending') == 1
         and 'oxlint' in res['tooling'].get('linters', {}) and res['tooling'].get('workspaces') == ['packages/*', 'apps/*']
         and 'note' in res['tooling'].get('typescript', {}), res['tooling']),
    ]
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
    args = ap.parse_args(argv)
    root = Path(args.repo)
    if not root.is_dir():
        print(f'Not a directory: {root}', file=sys.stderr); return 2
    res = Detector(root, args.max_files).run(args.target)
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
