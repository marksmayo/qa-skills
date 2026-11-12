#!/usr/bin/env python3
"""Fingerprint a repository's testing conventions (static, stdlib only).

Returns one fingerprint per area of the repository (each directory with its own manifest), so
monorepos and mixed stacks get the conventions that apply where a new test will be written.

    python detect_conventions.py <repo> [--target path/to/feature] [--out conventions.json]

Detection is heuristic: every conclusion carries the files it came from, and anything it can't
see is listed under `limitations`. CI configuration is treated as the source of truth for run
commands.
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
            'azure-pipelines.yml', 'bitbucket-pipelines.yml', 'Jenkinsfile', '.buildkite/*.yml',
            'buildspec.yml', '.drone.yml', 'codemagic.yaml', 'bitrise.yml']
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
    (re.compile(r'^.+_test\.go$'), '*_test.go'), (re.compile(r'^.+_spec\.rb$'), '*_spec.rb'),
    (re.compile(r'^(test_.+|.+_test)\.rb$'), '*_test.rb'), (re.compile(r'^.+Tests?\.cs$'), '*Tests.cs'),
    (re.compile(r'^.+Test\.php$'), '*Test.php'), (re.compile(r'^.+_test\.dart$'), '*_test.dart'),
    (re.compile(r'^.+Tests?\.swift$'), '*Tests.swift'), (re.compile(r'^.+\.feature$'), '*.feature'),
]
TEST_DIR_PARTS = {'test', 'tests', '__tests__', 'spec', 'specs', 'e2e', 'cypress', 'integration_test',
                  'androidTest', 'acceptance', '.maestro', 'maestro', 'uitests'}

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
    (re.compile(r'\bbrowser\.url\(|\$\(\s*[\'"]~'), 'e2e', 'webdriverio'),
    (re.compile(r'from selenium|selenium-webdriver|org\.openqa\.selenium'), 'e2e', 'selenium'),
    (re.compile(r'\bdevice\.launchApp\(|\bby\.id\(|from [\'"]detox[\'"]'), 'mobile', 'detox'),
    (re.compile(r'appium|AppiumBy|MobileBy'), 'mobile', 'appium'),
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
                       r'pnpm (run )?\S*(test|e2e)\S*|npx (jest|vitest|playwright test|cypress run|wdio|detox test)|'
                       r'(jest|vitest|mocha)\b|playwright test|cypress run|go test|mvn (-\S+ )*(verify|test)|'
                       r'(\./)?gradlew? \S*(test|check|connected\w*AndroidTest)|bundle exec (rspec|rake|cucumber)|rspec|'
                       r'dotnet test|cargo test|flutter test|phpunit|xcodebuild (\S+ )*test|maestro test|detox test|behave)\b')

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
    return False

def level_of(path: str, frameworks: set[str], root: str = '.') -> str:
    """unit | integration | e2e, from directory names and suffixes relative to the area root."""
    if root != '.' and path.startswith(root + '/'):
        path = path[len(root) + 1:]
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

class Detector:
    def __init__(self, root: Path, max_files: int):
        self.root = root.resolve()
        self.max_files = max_files
        self.files: list[str] = []
        self.limitations: list[str] = []

    def walk(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith('.egg-info'))
            for fn in sorted(filenames):
                self.files.append(rel(Path(dirpath) / fn, self.root))
                if len(self.files) >= self.max_files:
                    self.limitations.append(f'Stopped after {self.max_files} files; raise --max-files for a complete scan.')
                    return

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

    def ci(self) -> tuple[list[str], list[dict]]:
        ci_files, commands = [], []
        for f in self.files:
            if any(PurePosixPath(f).match(g) for g in CI_GLOBS) or f in CI_GLOBS:
                ci_files.append(f)
                lines = read(self.root / f).splitlines()
                for i, line in enumerate(lines, 1):
                    s = re.sub(r'^\s*(-\s*)?(run|script|command|sh)\s*:\s*[|>]?\s*', '', line).strip()
                    if len(s) > 1 and s[0] == s[-1] and s[0] in '\'"':
                        s = s[1:-1]
                    if s and not s.startswith('#') and RUN_HINTS.search(s) and len(s) < 300:
                        cmd = {'command': s, 'source': f'{f}:{i}', 'origin': 'ci'}
                        for near in self._step_lines(lines, i - 1):
                            m = re.search(r'working[-_]directory\s*:\s*[\'"]?([^\'"\s]+)', near)
                            if m:
                                cmd['working_directory'] = m.group(1).strip('./') or '.'
                                break
                        commands.append(cmd)
        return ci_files, commands

    def run(self, target: str | None) -> dict:
        self.walk()
        roots = self.area_roots()
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
                    for k, v in scripts.items():
                        if re.search(r'test|e2e|spec|cy:|cypress|playwright|detox', k + ' ' + str(v), re.I):
                            a['run_commands'].append({'command': f'npm run {k}' if k != 'test' else 'npm test',
                                                      'resolves_to': v, 'source': f, 'origin': 'package.json'})
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

        for a in areas.values():
            scanned = a['test_files'][:400]
            if len(a['test_files']) > 400:
                self.limitations.append(f"Area {a['root']}: content signals sampled from 400 of {len(a['test_files'])} test files.")
            fws = set()
            for f in scanned:
                text = read(self.root / f, 120_000)
                for rx, cat, fw in CONTENT_SIGNALS:
                    if rx.search(text):
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
                if level_of(f, fws) == 'e2e' or any(x in fws for x in ('playwright', 'cypress', 'detox', 'appium', 'maestro', 'xcuitest', 'espresso')):
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
                lv = level_of(f, a['_fws'], r)
                levels[lv] = levels.get(lv, 0) + 1
            naming = {}
            for f in a['test_files']:
                lab = naming_of(PurePosixPath(f).name)
                if lab: naming[lab] = naming.get(lab, 0) + 1
            in_dirs = [f for f in a['test_files'] if set(PurePosixPath(f).parts[:-1]) & TEST_DIR_PARTS
                       or any(x.endswith('Tests') or x.endswith('UITests') for x in PurePosixPath(f).parts[:-1])
                       or ('src' in PurePosixPath(f).parts and 'test' in PurePosixPath(f).parts)]
            layout = ('none' if not a['test_files'] else 'test_dirs' if len(in_dirs) >= 0.8 * len(a['test_files'])
                      else 'colocated' if len(in_dirs) <= 0.2 * len(a['test_files']) else 'mixed')
            test_dirs = sorted({self._test_dir(f) for f in in_dirs})[:15]
            fw_names = {n for cat in a['frameworks'].values() for n in cat}
            cmds = [c for c in ci_cmds if self._ci_applies(c, r, fw_names, [x for x in roots if x != '.'])] + a['run_commands']
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

    @staticmethod
    def _test_dir(f: str) -> str:
        parts = PurePosixPath(f).parts
        for i, x in enumerate(parts[:-1]):
            if x in TEST_DIR_PARTS or x.endswith('Tests') or (x == 'test' and i and parts[i - 1] == 'src'):
                return '/'.join(parts[:i + 1])
        return str(PurePosixPath(f).parent)

    def _mirrors(self, tests: list[str]):
        """True when most tests under a test directory have a same-named source file under a parallel path."""
        if len(tests) < 3:
            return None
        sources = {}
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
    def _ci_applies(cmd: dict, root: str, frameworks: set[str], roots: list[str]) -> bool:
        """A CI command belongs to an area if it runs there, names it, or (unscoped) uses its framework."""
        if root == '.':
            return True
        wd = cmd.get('working_directory')
        if wd:
            return wd == root or wd.startswith(root + '/')
        text = cmd['command']
        names = lambda r: re.search(r'(^|[\s/=])' + re.escape(r) + r'(/|\s|$)', text)
        if names(root):
            return True
        if any(names(x) for x in roots):
            return False  # names a different area
        tool_fw = {'pytest': 'pytest', 'playwright': 'playwright', 'cypress': 'cypress', 'jest': 'jest', 'vitest': 'vitest',
                   'go test': 'go-testing', 'cargo test': 'cargo-test', 'flutter test': 'flutter_test', 'rspec': 'rspec',
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
        tlevel = level_of(t.as_posix(), set(), self.owner(t.as_posix(), [a['root'] for a in areas]))
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
                same_level = 3 if level_of(f, set(), a['root']) == tlevel else 0
                score = common * 2 + shared + name_bonus + same_area + size_ok + same_level
                if score >= 3:
                    scored.append((score, f, a['root']))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [{'path': f, 'area': r, 'score': s} for s, f, r in scored[:3]]

def main(argv=None) -> int:
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
