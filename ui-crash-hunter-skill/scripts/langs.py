#!/usr/bin/env python3
"""Language census, vendored trees and HTML held in string constants. Stdlib only; imported by recon.py,
scan_pitfalls.py and contract_diff.py.

Vendored and third-party trees are left out before anything is counted or scanned: directories named
vendor/, third_party/, external/ (and node_modules/, Pods/, Carthage/), bundled driver trees known by
name (WebDriverAgent, an XCTest framework), and any nested directory that holds its own LICENSE and a
manifest of another ecosystem than the repository root (a Node/Xcode driver bundled in a Go module).

language_census() counts source files per language over what is left. When the dominant language is
one the skill does not analyse (Go, Rust, Python, Ruby, C#, ...), the caller must say so loudly instead of
building a site model from a handful of incidental files.

embedded_documents() finds HTML documents held in string constants of server-side code (a Go raw string
rendered with html/template, a Python or Java/Kotlin triple-quoted string, a Rust raw string) and template
files (.tmpl, .gohtml, .jinja, .erb, ...), with the line span of every inline <script>, so their browser
code can be scanned with its real file and line.
"""
from __future__ import annotations
import re
from pathlib import Path

VENDOR_DIRS = {'vendor', 'vendors', 'third_party', 'third-party', 'thirdparty', '3rdparty', 'external', 'externals',
               'node_modules', 'bower_components', 'Pods', 'Carthage'}
# bundled driver/SDK trees recognised by name wherever they sit
EMBEDDED_TREE = re.compile(r'^(WebDriverAgent|XCTest(\.framework)?|appium-[\w-]+)$')
LICENSE_FILE = re.compile(r'^(LICEN[CS]E|COPYING)(\.\w+)?$', re.I)
# manifest -> ecosystem; a nested tree with its own LICENSE and a manifest of another ecosystem than the root is vendored
ECOSYSTEM = {'go.mod': 'go', 'package.json': 'node', 'Cargo.toml': 'rust', 'pyproject.toml': 'python',
             'setup.py': 'python', 'Gemfile': 'ruby', 'pom.xml': 'jvm', 'build.gradle': 'jvm', 'build.gradle.kts': 'jvm',
             'Package.swift': 'apple', 'Podfile': 'apple', 'Cartfile': 'apple', 'project.pbxproj': 'apple',
             'composer.json': 'php', 'pubspec.yaml': 'dart', 'mix.exs': 'elixir'}

LANG_BY_EXT = {
    '.js': 'javascript', '.jsx': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript',
    '.ts': 'typescript', '.tsx': 'typescript', '.mts': 'typescript', '.cts': 'typescript',
    '.vue': 'vue', '.svelte': 'svelte', '.kt': 'kotlin', '.kts': 'kotlin', '.java': 'java', '.swift': 'swift',
    '.dart': 'dart', '.go': 'go', '.rs': 'rust', '.py': 'python', '.rb': 'ruby', '.cs': 'csharp', '.php': 'php',
    '.m': 'objective-c', '.mm': 'objective-c', '.h': 'c-family', '.c': 'c-family', '.cc': 'c-family',
    '.cpp': 'c-family', '.hpp': 'c-family', '.scala': 'scala', '.ex': 'elixir', '.exs': 'elixir',
}
LANG_NAME = {'go': 'Go', 'rust': 'Rust', 'python': 'Python', 'ruby': 'Ruby', 'csharp': 'C#', 'php': 'PHP',
             'objective-c': 'Objective-C', 'c-family': 'C/C++', 'scala': 'Scala', 'elixir': 'Elixir',
             'javascript': 'JavaScript', 'typescript': 'TypeScript', 'kotlin': 'Kotlin', 'java': 'Java',
             'swift': 'Swift', 'dart': 'Dart', 'vue': 'Vue', 'svelte': 'Svelte'}
# the languages the crash hunter's packs read (web, android, ios, flutter)
ANALYSED = {'javascript', 'typescript', 'vue', 'svelte', 'kotlin', 'java', 'swift', 'dart'}
# below this share of analysed-language files, a site model built from them describes incidental code
ANALYSED_SHARE_MIN = 0.25


def vendored_roots(root: Path, files: list[Path]) -> list[str]:
    """Relative directories holding vendored or bundled third-party trees (outermost only)."""
    root_eco = {ECOSYSTEM[f.name] for f in files if f.parent == root and f.name in ECOSYSTEM}
    licensed, eco = set(), {}
    found = set()
    for f in files:
        try:
            rel_parts = f.relative_to(root).parts
        except ValueError:
            continue
        for i, part in enumerate(rel_parts[:-1]):
            if part in VENDOR_DIRS or EMBEDDED_TREE.match(part):
                found.add('/'.join(rel_parts[:i + 1]))
                break
        d = f.parent
        if f.name == 'project.pbxproj' and d.suffix == '.xcodeproj':
            d = d.parent
        if d == root:
            continue
        if LICENSE_FILE.match(f.name):
            licensed.add(d)
        if f.name in ECOSYSTEM:
            eco.setdefault(d, set()).add(ECOSYSTEM[f.name])
    if root_eco:
        for d in licensed:
            if eco.get(d) and not eco[d] & root_eco:
                found.add(d.relative_to(root).as_posix())
    outer = sorted(found, key=len)
    return [d for i, d in enumerate(outer) if not any(d.startswith(o + '/') for o in outer[:i])]


def under(rel: str, prefixes: list[str]) -> bool:
    return any(rel == p or rel.startswith(p + '/') for p in prefixes)


def language_census(root: Path, files: list[Path], vendored: list[str] | None = None, is_test=None) -> dict:
    """Source files per language, vendored trees and tests left out; the dominant language, and whether the
    skill analyses it."""
    vendored = vendored_roots(root, files) if vendored is None else vendored
    counts: dict[str, int] = {}
    for f in files:
        lang = LANG_BY_EXT.get(f.suffix)
        if not lang:
            continue
        rel = f.relative_to(root).as_posix()
        if under(rel, vendored) or (is_test and is_test(rel)):
            continue
        counts[lang] = counts.get(lang, 0) + 1
    total = sum(counts.values())
    dominant = max(counts, key=lambda k: (counts[k], k)) if counts else None
    analysed_files = sum(n for k, n in counts.items() if k in ANALYSED)
    share = round(analysed_files / total, 3) if total else None
    out = {'by_language': dict(sorted(counts.items(), key=lambda kv: -kv[1])), 'source_files': total,
           'dominant': dominant, 'dominant_files': counts.get(dominant, 0),
           'analysed': dominant in ANALYSED if dominant else None,
           'analysed_languages': sorted(ANALYSED), 'analysed_share': share, 'vendored': vendored}
    return out


def language_warning(lc: dict, tool: str) -> str | None:
    """The loud warning for a repository whose dominant language the skill does not analyse."""
    if lc.get('dominant') is None or lc.get('analysed'):
        return None
    name = LANG_NAME.get(lc['dominant'], lc['dominant'])
    return (f"{name} is the dominant language ({lc['dominant_files']} of {lc['source_files']} source files, vendored "
            f"trees and tests left out) and {tool} does not analyse it: an empty or small result does not mean the "
            f"UI is crash-free. Only {round((lc['analysed_share'] or 0) * 100)}% of the source is in an analysed "
            'language; read the UI code by hand (see embedded_templates for HTML held in string constants) or '
            'probe the running UI.')


def incidental(lc: dict) -> bool:
    """True when the analysed-language files are too few to model the site from (a Go CLI with a bundled
    driver test fixture)."""
    return lc.get('dominant') is not None and not lc.get('analysed') and (lc.get('analysed_share') or 0) < ANALYSED_SHARE_MIN


# ---------------------------------------------------------------- HTML in string constants

HOST_EXT = {'.go', '.py', '.rb', '.rs', '.java', '.kt', '.cs', '.php', '.scala', '.ex', '.exs', '.swift'}
TEMPLATE_FILE = re.compile(r'\.(tmpl|gohtml|gotmpl|tpl|jinja2?|j2|erb|hbs|handlebars|mustache|njk|liquid|twig|ejs)$', re.I)
STRING_LITERAL = re.compile(r'`[^`]*`|"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|r#+"[\s\S]*?"#+')
HTML_DOC = re.compile(r'<(!doctype\s+html|html\b|body\b|script\b)', re.I)
SCRIPT_BLOCK = re.compile(r'<script\b([^>]*)>(.*?)</script\s*>', re.I | re.S)
STYLE_BLOCK = re.compile(r'<style\b[^>]*>(.*?)</style\s*>', re.I | re.S)
DATA_SCRIPT = re.compile(r'\btype\s*=\s*["\']?(?!text/javascript|module|application/javascript)[\w/+-]+', re.I)
ENGINE = (('go-html-template', re.compile(r'"html/template"')), ('go-text-template', re.compile(r'"text/template"')),
          ('jinja', re.compile(r'\bjinja2\b|render_template')), ('erb', re.compile(r'\bERB\.new\b')))


def _line(text: str, pos: int) -> int:
    return text.count('\n', 0, pos) + 1


def scripts_in(text: str, start: int, end: int) -> list[dict]:
    """Inline <script> blocks in text[start:end]: body offsets and line spans."""
    out = []
    for m in SCRIPT_BLOCK.finditer(text, start, end):
        if re.search(r'\bsrc\s*=', m.group(1), re.I) or DATA_SCRIPT.search(m.group(1)) or not m.group(2).strip():
            continue
        out.append({'body_start': m.start(2), 'body_end': m.end(2), 'line': _line(text, m.start(2)),
                    'end_line': _line(text, m.end(2))})
    return out


def embedded_documents(text: str, suffix: str, name: str = '') -> list[dict]:
    """HTML documents held in string constants (or a whole template file), with their inline scripts."""
    if TEMPLATE_FILE.search(name):
        if not HTML_DOC.search(text) and '<' not in text:
            return []
        spans = [(0, len(text), 'template_file')]
    elif suffix in HOST_EXT:
        spans = [(m.start(), m.end(), 'string_constant') for m in STRING_LITERAL.finditer(text)
                 if HTML_DOC.search(m.group(0))]
    else:
        return []
    engine = next((e for e, rx in ENGINE if rx.search(text)), None)
    docs = []
    for a, b, kind in spans:
        scripts = scripts_in(text, a, b)
        docs.append({'kind': kind, 'line': _line(text, a), 'end_line': _line(text, b), 'engine': engine,
                     'inline_scripts': len(scripts), 'styles': len(STYLE_BLOCK.findall(text, a, b)),
                     'script_spans': [{'line': s['line'], 'end_line': s['end_line']} for s in scripts],
                     '_scripts': scripts})
    return docs


def script_text(text: str, s: dict) -> str:
    """The text of one inline script, padded so line and column numbers are the host file's."""
    pre = text[:s['body_start']]
    col = s['body_start'] - (pre.rfind('\n') + 1)
    return '\n' * pre.count('\n') + ' ' * col + text[s['body_start']:s['body_end']]
