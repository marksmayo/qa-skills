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
    incidental code (a Go CLI with a bundled driver's test fixture). A server-rendered Python site (Flask or
    Django rendering templates) is not incidental: see server_rendered()."""
    return lc.get('status') == 'unsupported_dominant'


# ---------------------------------------------------------------- server-rendered Python sites

# frameworks whose pages are server templates: the Python is the server, the templates are the UI
SERVER_TEMPLATE_FRAMEWORKS = {'flask', 'django'}


def is_template_path(rel: str) -> bool:
    """A server-side template: a template extension (.jinja, .j2, .tmpl, .erb, ...), or .html/.htm inside a
    templates/ directory (Flask's and Django's default)."""
    parts = rel.replace('\\', '/').split('/')
    return bool(TEMPLATE_FILE.search(parts[-1])) or (
        parts[-1].lower().endswith(('.html', '.htm')) and 'templates' in parts[:-1])


PY_WEB_DEP = re.compile(r'''^\s*["']?(flask|django)(?=[\s"'<>=~!\[;,]|$)''', re.I | re.M)


def python_web_frameworks(root: Path) -> list[str]:
    """Flask or Django named in a requirements file, pyproject.toml, Pipfile or setup.cfg at the root or one level
    down, or imported by a Python file at the root (a Flask app with no requirements file)."""
    found: set[str] = set()
    for pat in ('requirements*.txt', 'pyproject.toml', 'Pipfile', 'setup.cfg', '*/requirements*.txt', '*/pyproject.toml'):
        for f in root.glob(pat):
            try:
                found |= {m.lower() for m in PY_WEB_DEP.findall(f.read_text(encoding='utf-8', errors='replace'))}
            except OSError:
                pass
    if not found:
        for f in list(root.glob('*.py'))[:50]:
            try:
                text = f.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            if re.search(r'^\s*(from flask import|import flask\b)', text, re.M):
                found.add('flask')
    return sorted(found)


def server_rendered(lc: dict, frameworks: list[str], templates: list[str], scripts: int = 0) -> dict:
    """Mark a language census as a server-rendered site when its dominant unanalysed language is Python, a Python
    web framework renders server templates, and those templates exist: the templates (and static scripts) are the
    UI and are analysed; the Python is server code (a throw there is an HTTP 500, not a UI crash). The loud
    "NOT a crash analysis" warning is replaced by status "server_rendered" with a note; the Python stays not
    assessed. A Go or Rust tool with a stray template keeps the warning."""
    fw = sorted(set(frameworks) & SERVER_TEMPLATE_FRAMEWORKS)
    if lc.get('status') != 'unsupported_dominant' or lc.get('dominant_unsupported_language') != 'Python' \
            or not fw or not templates:
        return lc
    out = dict(lc)
    out['status'] = 'server_rendered'
    out['server_rendered'] = {'frameworks': fw, 'templates': len(templates), 'examples': templates[:10],
                              'static_scripts': scripts}
    out['warning'] = None
    out['note'] = (f"A server-rendered {'/'.join(fw)} site: Python ({lc.get('dominant_unsupported_files')} source files) "
                   f'is the server that renders {len(templates)} template(s). The UI is those templates and the static '
                   'scripts they load, and the crash hunter analyses them (inline scripts are scanned at template lines). '
                   'The Python server code is not analysed: a throw there is an HTTP 500 or an error page, not a UI crash; '
                   'record it as not assessed.')
    return out


# ---------------------------------------------------------------- data directories

# A data directory: at least DATA_DIR_MIN files directly inside it, at least 90% of them data files and none code.
# Walks prune it before descending (a directory of thousands of JSON records is not UI code and opening each file
# took minutes) and list it in skipped_data_dirs; --exclude DIR prunes any other directory.
DATA_DIR_MIN = 200
DATA_EXT = {'.json', '.geojson', '.topojson', '.csv', '.tsv', '.xml', '.ndjson', '.jsonl', '.txt', '.yaml', '.yml',
            '.parquet', '.avro', '.dat', '.xls', '.xlsx', '.gz', '.zip', '.pbf', '.sqlite', '.db'}
CODE_EXT = {'.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.vue', '.svelte', '.py', '.rb', '.go', '.java', '.kt', '.swift',
            '.dart', '.cs', '.php', '.html', '.htm', '.rs'}
EXCLUDE: set[str] = set()                 # repo-relative directories from --exclude
SKIPPED_DATA: dict[str, dict] = {}        # path -> {'path', 'files', 'reason'}, filled by prune_dir()
_NOT_DATA: set[str] = set()               # directories already checked and kept


def prune_dir(root: Path, d: Path) -> bool:
    """True when directory d is a --exclude directory or a data directory: the caller must not descend.
    Each pruned directory is recorded once in SKIPPED_DATA (see skipped_data_dirs())."""
    import os
    try:
        rel = d.relative_to(root).as_posix()
    except ValueError:
        return False
    if rel in SKIPPED_DATA:
        return True
    if rel in _NOT_DATA:
        return False
    _NOT_DATA.add(rel)   # removed again below when the directory is pruned
    excluded = rel in EXCLUDE
    try:
        if not excluded:
            with os.scandir(d) as it:
                names = [e.name for e in it if e.is_file()]
            if len(names) < DATA_DIR_MIN:
                return False
        else:
            names = []
    except OSError:
        return False
    if excluded:
        _NOT_DATA.discard(rel)
        SKIPPED_DATA[rel] = {'path': rel, 'files': None, 'reason': 'excluded by --exclude'}
        return True
    exts = [Path(n).suffix.lower() for n in names]
    data = sum(1 for x in exts if x in DATA_EXT)
    if data >= 0.9 * len(names) and not any(x in CODE_EXT for x in exts):
        _NOT_DATA.discard(rel)
        SKIPPED_DATA[rel] = {'path': rel, 'files': len(names),
                             'reason': f'a data directory ({data} data files of {len(names)}, no code)'}
        return True
    return False


def skipped_data_dirs() -> list[dict]:
    return sorted(SKIPPED_DATA.values(), key=lambda x: x['path'])


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
    """HTML documents held in string constants (or a whole template file), with their inline scripts. name is the
    file name or, better, its repository-relative path: .html under a templates/ directory is a template file."""
    if TEMPLATE_FILE.search(name) or is_template_path(name):
        if not HTML_DOC.search(text) and '<' not in text:
            return []
        spans = [(0, len(text), 'template_file')]
    elif name.lower().endswith(('.html', '.htm')):   # a plain HTML page: its inline scripts are browser code too
        spans = [(0, len(text), 'html_page')] if SCRIPT_BLOCK.search(text) else []
    elif suffix in HOST_EXT:
        spans = [(m.start(), m.end(), 'string_constant') for m in STRING_LITERAL.finditer(text)
                 if HTML_DOC.search(m.group(0))]
    else:
        return []
    engine = next((e for e, rx in ENGINE if rx.search(text)), None)
    if engine is None and spans and spans[0][2] == 'template_file' and re.search(r'\{%-?\s*(extends|block|include|if|for)\b', text):
        engine = 'jinja'   # Jinja or Django template syntax
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
