#!/usr/bin/env python3
"""Language coverage, vendored trees and HTML held in string constants. Stdlib only; imported by recon.py,
scan_pitfalls.py and contract_diff.py.

The language census and the vendored-tree exclusion come from the shared lang_census.py (vendor/,
third_party/, node_modules/, built bundles, header trees, git submodules, and a nested package with its own
manifest and LICENSE under a drivers/SDK/external directory, such as a bundled WebDriverAgent). When the
dominant language is one the crash hunter's packs do not read (Go, Rust, Python, ...), the callers say so
loudly (language_coverage.status "unsupported_dominant" with its warning) instead of building a site model
from a handful of incidental files.

embedded_documents() finds HTML documents held in string constants of server-side code (a Go raw string
rendered with html/template, a Python or Java/Kotlin triple-quoted string, a Rust raw string) and template
files (.tmpl, .gohtml, .jinja, .erb, ...), with the line span of every inline <script>, so their browser
code can be scanned with its real file and line.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import lang_census  # noqa: E402  (shared copy; edit shared/scripts/lang_census.py)

# the languages the crash hunter's packs read (web, android, ios, flutter), in lang_census's names
ANALYSED = {'JavaScript', 'TypeScript', 'Vue', 'Svelte', 'Kotlin', 'Java', 'Swift', 'Dart'}


def vendored_roots(root: Path, files=None) -> list[str]:
    """Repo-relative directories lang_census leaves out (vendored, third-party, built or dependency trees)."""
    return [e['path'] for e in lang_census.census(root)['excluded']]


def under(rel: str, prefixes: list[str]) -> bool:
    return any(rel == p or rel.startswith(p + '/') for p in prefixes)


def language_census(root: Path, files=None, vendored=None, is_test=None, tool: str = 'this skill') -> dict:
    """lang_census's census and coverage for the crash hunter's languages, as one `language_coverage` record:
    status ok | partial | unsupported_dominant (with warning) | no_source, the counts per language and the
    excluded trees (paths in `excluded`, with reasons in `excluded_trees`)."""
    c = lang_census.census(root)
    cov = lang_census.language_coverage(c, ANALYSED, tool=tool, what='crash analysis')
    cov['languages'] = c['languages']
    cov['excluded'] = [e['path'] for e in c['excluded']]
    cov['excluded_trees'] = c['excluded'][:30]
    return cov


def language_warning(lc: dict, tool: str = '') -> str | None:
    """The loud warning for a repository whose dominant language the skill does not analyse."""
    if lc.get('status') != 'unsupported_dominant':
        return None
    return (lc['warning'] + ' An empty or small result does not mean the UI is crash-free: read the UI code by hand '
            '(see site.embedded_templates for HTML held in string constants) or probe the running UI.')


def incidental(lc: dict) -> bool:
    """True when an unanalysed language dominates, so a site model from the analysed files would describe
    incidental code (a Go CLI with a bundled driver's test fixture)."""
    return lc.get('status') == 'unsupported_dominant'


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
