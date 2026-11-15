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


# dependency trees are excluded by name; counting their files means walking every installed package (thousands of
# files beside a 30-file app), so they are listed without being walked (source_files_counted: false)
DEPENDENCY_TREES = {'node_modules', 'bower_components', 'jspm_packages'}


def census(root: Path) -> dict:
    """lang_census.census, without descending into dependency trees only to count their files."""
    real = lang_census.count_sources

    def count(d, cap: int = 200000) -> int:
        return 0 if Path(d).name in DEPENDENCY_TREES else real(d, cap)
    lang_census.count_sources = count
    try:
        c = lang_census.census(root)
    finally:
        lang_census.count_sources = real
    for e in c.get('excluded') or []:
        if Path(e['path']).name in DEPENDENCY_TREES:
            e['source_files'] = 0
            e['source_files_counted'] = False
    return c


def vendored_roots(root: Path, files=None) -> list[str]:
    """Repo-relative directories lang_census leaves out (vendored, third-party, built or dependency trees)."""
    return [e['path'] for e in with_build_dirs(root, census(root))['excluded']]


def under(rel: str, prefixes: list[str]) -> bool:
    return any(rel == p or rel.startswith(p + '/') for p in prefixes)


def language_census(root: Path, files=None, vendored=None, is_test=None, tool: str = 'this skill') -> dict:
    """lang_census's census and coverage for the crash hunter's languages, as one `language_coverage` record:
    status ok | partial | unsupported_dominant (with warning) | no_source, the counts per language and the
    excluded trees (paths in `excluded`, with reasons in `excluded_trees`)."""
    c = with_build_dirs(root, census(root))
    cov = lang_census.language_coverage(c, ANALYSED, tool=tool, what='crash analysis')
    cov['languages'] = c['languages']
    cov['excluded'] = [e['path'] for e in c['excluded']]
    cov['excluded_trees'] = c['excluded'][:30]
    skipped, kept = build_dirs()
    if kept:
        cov['kept_build_dirs'] = kept
    held = [s for s in skipped if any(l in ANALYSED for l in s.get('languages', {}))]
    if cov.get('status') == 'unsupported_dominant' and held:
        cov['warning'] += ' ' + ' '.join(
            f"{s['path']}/ holds {', '.join(f'{n} {l}' for l, n in s['languages'].items() if l in ANALYSED)} file(s) and was "
            f"skipped as {s['reason']}; if it is the UI's source (a committed front end the server serves), rerun with "
            f"--include {s['path']} or on {s['path']}/ itself." for s in held)
    return cov


def with_build_dirs(root: Path, c: dict) -> dict:
    """The shared census with build-output trees that are in fact UI source (keep_build_dir) counted back in, and the
    others recorded as skipped with their languages. lang_census itself always leaves dist/, build/ and out/ out."""
    from collections import Counter
    import os
    langs = Counter(c.get('languages') or {})
    excluded = []
    for e in c.get('excluded') or []:
        d = root / e['path']
        if Path(e['path']).name not in BUILD_NAMES and e['path'] not in INCLUDE:
            excluded.append(e)
            continue
        kept = keep_build_dir(root, d)
        here = Counter()
        for dp, dns, fns in os.walk(d):
            dns[:] = [x for x in dns if x not in ('node_modules', '.git')]
            for f in fns:
                lang = lang_census.LANG_BY_EXT.get(Path(f).suffix)
                if lang:
                    here[lang] += 1
        if kept:
            langs.update(here)
        else:
            excluded.append(e)
            if e['path'] in BUILD_SKIPPED:
                BUILD_SKIPPED[e['path']]['languages'] = dict(here.most_common())
    out = dict(c)
    out['languages'] = dict(sorted(langs.items(), key=lambda kv: (-kv[1], kv[0])))
    out['dominant'] = next(iter(out['languages']), None)
    out['dominant_files'] = out['languages'].get(out['dominant'], 0) if out['dominant'] else 0
    out['total_source_files'] = sum(langs.values())
    out['excluded'] = excluded
    out['excluded_source_files'] = sum(e.get('source_files', 0) for e in excluded)
    return out


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


# ---------------------------------------------------------------- build-output directories

# Directory names the walkers treat as build output. Such a directory is the UI's SOURCE (walked like any other) when a
# server in the repository serves it (FastAPI StaticFiles(directory=...), Flask static_folder, express.static, sirv,
# serve-static, send_from_directory), when --include names it, or when it holds an index.html, nothing in the
# repository builds it (no build script or bundler config beside it) and no .gitignore lists it: a committed,
# hand-written front end. Otherwise it is skipped, and every skip is recorded (skipped_build_dirs) with its file count.
BUILD_NAMES = {'dist', 'build', 'out'}
INCLUDE: set[str] = set()                 # repo-relative directories from --include
BUILD_SKIPPED: dict[str, dict] = {}
BUILD_KEPT: dict[str, dict] = {}
_SERVED: dict[str, dict[str, str]] = {}   # root -> {served dir: reason}
SERVE_CALL = re.compile(r'\b(StaticFiles|static_folder|send_from_directory|express\.static|serveStatic|serve_static|sirv|'
                        r'fastifyStatic|koaStatic|StaticFileHandler|web\.static|add_static|static_url_path)\b\s*[(=:]', re.I)
STR_LIT = re.compile(r'''["']([\w./\\-]+)["']''')
BUILD_TOOLS = re.compile(r'"(vite|webpack|rollup|parcel|esbuild|react-scripts|@angular/cli|next|nuxt|astro|gatsby|'
                         r'@sveltejs/kit|vue-cli-service|@vue/cli-service|tsup|snowpack|gulp|grunt)"')
BUNDLER_CONFIG = re.compile(r'^(vite|webpack|rollup|parcel|esbuild|svelte|astro|nuxt|next|angular|gulpfile|gruntfile|tsup)'
                            r'([.-]config)?\.(js|cjs|mjs|ts|json)$|^angular\.json$|^\.parcelrc$', re.I)


def served_dirs(root: Path) -> dict[str, str]:
    """Repo-relative directories a server in the repository serves as static files, with the file:line that says so.
    Reads Python, JavaScript/TypeScript and PHP files at the root and two levels down (dependency trees skipped)."""
    import os
    key = str(root)
    if key in _SERVED:
        return _SERVED[key]
    out: dict[str, str] = {}
    exts = ('.py', '.js', '.mjs', '.cjs', '.ts', '.php')
    checked = 0
    for dp, dns, fns in os.walk(root):
        rel_dp = Path(dp).relative_to(root)
        dns[:] = [d for d in dns if not d.startswith('.') and d not in ('node_modules', 'vendor', '__pycache__', 'venv', '.venv')
                  and len(rel_dp.parts) < 2 and d not in BUILD_NAMES]
        for f in fns:
            if not f.endswith(exts) or checked > 400:
                continue
            p = Path(dp) / f
            try:
                if p.stat().st_size > 400_000:
                    continue
                text = p.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            checked += 1
            for m in SERVE_CALL.finditer(text):
                seg = text[m.end():m.end() + 240]
                seg = seg.split('\n', 1)[0] if m.group(1) in ('static_folder', 'static_url_path') else ')'.join(seg.split(')', 2)[:2])
                for lit in STR_LIT.findall(seg)[:4]:     # only literals that name an existing directory count
                    lit = lit.replace('\\', '/').strip('/')
                    if lit.startswith('./'):
                        lit = lit[2:]
                    for base in (Path(dp), root):
                        cand = (base / lit).resolve()
                        try:
                            rel = cand.relative_to(root.resolve()).as_posix()
                        except ValueError:
                            continue
                        if rel and rel != '.' and cand.is_dir():
                            line = text.count('\n', 0, m.start()) + 1
                            src = p.relative_to(root).as_posix()
                            out.setdefault(rel, f'served as static files by {src}:{line} ({m.group(1)})')
                            break
    _SERVED[key] = out
    return out


def _gitignored(root: Path, d: Path) -> str | None:
    rel = d.relative_to(root).as_posix()
    for gi in (root / '.gitignore', d.parent / '.gitignore'):
        try:
            lines = [l.strip() for l in gi.read_text(encoding='utf-8', errors='replace').splitlines()]
        except OSError:
            continue
        for l in lines:
            if l and not l.startswith('#') and l.strip('/') in (d.name, rel, f'**/{d.name}'):
                return f'{gi.relative_to(root).as_posix()} lists it'
    return None


def _built_by(d: Path) -> str | None:
    """What in the repository builds directory d: a build script or bundler beside it, or None."""
    import json as _json
    parent = d.parent
    for f in sorted(parent.iterdir()) if parent.is_dir() else []:
        if f.is_file() and BUNDLER_CONFIG.match(f.name):
            return f'{f.name} beside it'
    pkg = parent / 'package.json'
    try:
        data = _json.loads(pkg.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        if (data.get('scripts') or {}).get('build'):
            return f'package.json build script ({str(data["scripts"]["build"])[:60]})'
        if BUILD_TOOLS.search(_json.dumps({**(data.get('dependencies') or {}), **(data.get('devDependencies') or {})})):
            return 'a bundler in package.json'
    return None


def count_source_files(d: Path) -> int:
    import os
    n = 0
    for _, dns, fns in os.walk(d):
        dns[:] = [x for x in dns if x not in ('node_modules', '.git')]
        n += sum(1 for f in fns if Path(f).suffix in lang_census.LANG_BY_EXT)
    return n


def keep_build_dir(root: Path, d: Path) -> bool:
    """For a directory a walker would skip as build output: True when it is the UI's source (see above). Every
    decision is recorded once, in BUILD_KEPT or BUILD_SKIPPED. Names outside BUILD_NAMES return False."""
    try:
        rel = d.relative_to(root).as_posix()
    except ValueError:
        return False
    if d.name not in BUILD_NAMES and rel not in INCLUDE:
        return False
    if rel in BUILD_KEPT:
        return True
    if rel in BUILD_SKIPPED:
        return False
    why = None
    if rel in INCLUDE:
        why = 'included by --include'
    else:
        served = served_dirs(root)
        why = served.get(rel)
        if why is None and (d / 'index.html').is_file():
            ignored, built = _gitignored(root, d), _built_by(d)
            if not ignored and not built:
                why = 'holds index.html, nothing in the repository builds it and no .gitignore lists it (committed source)'
    if why:
        BUILD_KEPT[rel] = {'path': rel, 'reason': why}
        return True
    reason = _gitignored(root, d) or _built_by(d)
    BUILD_SKIPPED[rel] = {'path': rel, 'source_files': count_source_files(d),
                          'reason': f'build output ({reason})' if reason else 'build output (directory name)'}
    return False


def build_dirs() -> tuple[list[dict], list[dict]]:
    """(skipped, kept) build-output decisions so far, for the output's skipped_build_dirs / kept_build_dirs."""
    return (sorted(BUILD_SKIPPED.values(), key=lambda x: x['path']), sorted(BUILD_KEPT.values(), key=lambda x: x['path']))


def report_build_skips(tool: str) -> None:
    """One stderr line per skipped build-output directory that holds source files."""
    for s in sorted(BUILD_SKIPPED.values(), key=lambda x: x['path']):
        if s['source_files']:
            print(f"{tool}: skipped {s['path']}/ ({s['source_files']} source file(s)): {s['reason']}; "
                  f"rerun with --include {s['path']} if it is the UI's source", file=sys.stderr)


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
