#!/usr/bin/env python3
"""Static privacy scan: collection points, sinks, SDK inventory, mobile permissions, and candidate flows.

    python scan_privacy.py <repo> --out run/static.json [--exclude DIR] [--max-bytes N] [--semgrep semgrep.sarif]

Collection points: HTML inputs (name, id, type, autocomplete, label), JSX form elements in React code (input,
textarea, select and the common component-library fields such as MUI TextField, with name, id, type,
autoComplete, label and placeholder) and form-library registrations (register('email')), server-side form reads, and device APIs
and permissions (geolocation, contacts, camera, advertising id; AndroidManifest permissions; Info.plist usage
strings), each classified with data/categories.json. Sinks: data/sinks.json (logging, analytics, identify,
session replay, error trackers, storage, outbound HTTP). A candidate flow is a sink line that names a
collected field (or a variable holding it) - a pattern, for the agent to trace to a full path. Replay
masking switched off, a Datadog RUM init that records replays without setting defaultPrivacyLevel, client-side
persistence of a whole data cache (Apollo, React Query, Redux persist to localStorage or IndexedDB) and
error-tracker PII settings are candidates on their own. SDKs come from package manifests, from script hosts in
HTML (<script src>), from scripts injected by JavaScript (el.src = "https://...", setAttribute('src', ...), a URL
literal in a file that creates a script element), from URL literals on a listed vendor's domain
(data/vendors.json, such as the gtag.js and adsbygoogle.js loader URLs), and from importScripts() hosts.

A sink line names a collected field when the field appears as an identifier or a key: `email`, `{email: x}`,
`data['email']`, `${email}` / `{email}` inside a template or f-string. Ordinary string text ("Enter your name")
doesn't count, and for names that are everyday property names (data/categories.json generic_property_names,
such as name, title and type) neither does a property access (`path.name`).

Files are read once and every pattern runs over the whole text. Directories in SKIP and --exclude are pruned.
Files larger than --max-bytes (default 2 MB) are skipped; minified or generated files (*.min.js, bundles, or
very long lines) are scanned for SDK hosts only. Both are listed in the output under "skipped".

Every candidate carries file and line; no value from the code beyond the matched line (truncated) is kept,
and string literals that look like personal data are redacted.
"""
from __future__ import annotations
import argparse, bisect, json, os, re, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from match_canaries import Matcher  # noqa: E402

DATA = HERE.parent / 'data'
CATS = json.loads((DATA / 'categories.json').read_text(encoding='utf-8'))
SINKS = json.loads((DATA / 'sinks.json').read_text(encoding='utf-8'))
LANG = {'.py': 'py', '.js': 'js', '.mjs': 'js', '.jsx': 'js', '.ts': 'ts', '.tsx': 'ts', '.html': 'html', '.htm': 'html',
        '.java': 'java', '.kt': 'kt', '.swift': 'swift', '.m': 'm', '.rb': 'rb'}
SKIP = {'node_modules', '.git', 'dist', 'build', 'venv', '.venv', '__pycache__', 'Pods', '.next', '.nuxt', 'coverage',
        'bower_components', '.svelte-kit', '.parcel-cache', '.turbo', '.tox', '.mypy_cache', '.pytest_cache'}
REDACT = Matcher({'id': 'none', 'fields': {}})
GENERIC = set(CATS.get('generic_property_names', {}).get('names', []))
VENDOR_DOMAINS = {d: v['vendor'] for d, v in json.loads((DATA / 'vendors.json').read_text(encoding='utf-8'))['domains'].items()}
TWO_LEVEL = ('com.au', 'net.au', 'org.au', 'gov.au', 'edu.au', 'co.uk', 'org.uk', 'gov.uk', 'ac.uk', 'co.nz', 'co.jp', 'com.br')
QUIET = {'.git', '__pycache__', '.mypy_cache', '.pytest_cache', '.tox', '.parcel-cache', '.turbo'}  # pruned without being listed
MANIFESTS = {'requirements.txt', 'Podfile', 'build.gradle', 'build.gradle.kts', 'pubspec.yaml', 'Gemfile'}
PLATFORM = {'AndroidManifest.xml', 'Info.plist', 'PrivacyInfo.xcprivacy'}
GENERATED_NAME = re.compile(r'\.(min|bundle|chunk|prod|umd)\.(js|mjs|css)$|[.-][0-9a-f]{8,}\.(js|mjs)$|\.map$', re.I)
STRING = re.compile(r'''(?P<p>\b[rRbBuUfF]{1,2})?(?P<s>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`)''', re.S)
KEYWORD = re.compile(r'[A-Za-z_$][\w$-]*')
URL_LIT = re.compile(r'''["'`](?:https?:)?//([a-z0-9.-]+\.[a-z]{2,}|\[[0-9a-f:]+\]|\d+\.\d+\.\d+\.\d+)(?::\d+)?(/[^"'`\s]*)?["'`]''', re.I)
SRC_ASSIGN = re.compile(r'''\.src\s*=\s*["'`](?:https?:)?//([^/"'`\s:]+)|\.setAttribute\(\s*["']src["']\s*,\s*["'`](?:https?:)?//([^/"'`\s:]+)''', re.I)
CREATES_SCRIPT = re.compile(r'''createElement\(\s*["']script["']\s*\)''', re.I)
IMPORT = re.compile(r'''(?:\bfrom\s+|\brequire\(\s*|\bimport\s*\(\s*)["']([@\w./-]+)["']''')
ALIAS_ELEMENT = re.compile(r'''\b(?:var|let|const)\s+(\w+)\s*=\s*document\.(?:getElementById|querySelector)\(['"]#?([\w-]+)['"]\)''')
ALIAS_VALUE = re.compile(r'\b(?:var|let|const)\s+(\w+)\s*=\s*(\w+)\.value\b')
ALIAS_PARAM = re.compile(r'''\b(?:var|let|const)\s+(\w+)\s*=\s*new URLSearchParams\([^)]*\)\.get\(['"]([\w-]+)['"]\)''')
SCRIPT_TAG = re.compile(r'''<script\b[^>]*?\bsrc\s*=\s*["']?(?:https?:)?//([^/"'\s:>]+)''', re.I)


def registrable(host: str) -> str:
    host = host.lower().strip('.')
    if re.fullmatch(r'[\d.]+|\[[0-9a-f:]+\]', host) or '.' not in host:
        return host
    parts = host.split('.')
    n = 3 if '.'.join(parts[-2:]) in TWO_LEVEL else 2
    return '.'.join(parts[-n:])


class Text:
    """A file's text with line lookup by offset."""

    def __init__(self, text: str):
        self.text = text
        self.starts = [0] + [m.end() for m in re.finditer('\n', text)]

    def line_of(self, off: int) -> int:
        return bisect.bisect_right(self.starts, off)

    def line(self, n: int) -> str:
        end = self.starts[n] - 1 if n < len(self.starts) else len(self.text)
        return self.text[self.starts[n - 1]:end].rstrip('\r')


def code_view(line: str) -> str:
    """The line with string text removed: a literal that is one identifier-like word stays as that word (a key),
    a template literal or f-string keeps its interpolations, and every other literal becomes ""."""
    def sub(m):
        body, pre = m.group('s'), (m.group('p') or '').lower()
        inner = body[1:-1]
        if KEYWORD.fullmatch(inner):
            return f' {inner} '
        if body[0] == '`':
            return ' ' + ' '.join(code_view(x) for x in re.findall(r'\$\{([^{}]*)\}', inner)) + ' '
        if 'f' in pre:
            return ' ' + ' '.join(code_view(x) for x in re.findall(r'\{([^{}!:]*)', inner)) + ' '
        return '""'
    return STRING.sub(sub, line)


def names_field(view: str, field: str) -> bool:
    """True when the field appears in the code view as an identifier or key (not inside another word; not as a
    property access for an everyday property name)."""
    lead = r'(?<![\w$.-])' if field in GENERIC else r'(?<![\w$-])'
    return re.search(lead + re.escape(field) + r'(?![\w$-])', view) is not None


def classify(*names: str) -> str | None:
    """The most sensitive category whose patterns match any of the names."""
    best = None
    for n in names:
        if not n:
            continue
        low = n.lower().replace('-', '_')
        for cat, c in CATS['categories'].items():
            hit = any(low == p or low.startswith(p + '_') or low.endswith('_' + p) or (len(p) > 4 and p in low) for p in c['names']) \
                or low in c.get('autocomplete', []) or low in c.get('types', [])
            if hit and (best is None or c['sensitivity'] > CATS['categories'][best]['sensitivity']):
                best = cat
    return best


def files(repo: Path, pruned: list):
    """Every file under repo, pruning SKIP directories while walking (each pruned directory is added to pruned)."""
    for root, dirs, names in os.walk(repo):
        pruned += [(Path(root) / d).relative_to(repo).as_posix() for d in sorted(dirs) if d in SKIP and d not in QUIET]
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        for n in sorted(names):
            yield Path(root) / n


def attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\b{name}\s*=\s*["\']([^"\']*)["\']', tag, re.I)
    return m.group(1) if m else None


def jsx_tags(text: str, start_rx: str):
    """(offset, tag text) of each JSX element opening that matches start_rx, up to its closing '>' (braces skipped)."""
    for m in re.finditer(start_rx, text):
        i, depth, j = m.start(), 0, m.end()
        stop = min(len(text), j + 4000)
        while j < stop:
            c = text[j]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
            elif c == '>' and depth == 0:
                break
            j += 1
        yield i, text[i:j + 1]


def jsx_attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\b{name}\s*=\s*(?:\{{\s*)?["\'`]([^"\'`]*)["\'`]', tag)
    return m.group(1) if m else None


def call_object(text: str, start: int) -> str:
    """The text of the call starting at start, up to its closing parenthesis."""
    i = text.index('(', start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == '(':
            depth += 1
        elif text[j] == ')':
            depth -= 1
            if not depth:
                return text[i:j + 1]
    return text[i:]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('repo', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--semgrep', type=Path)
    ap.add_argument('--exclude', action='append', default=[], help='directory name to skip (third-party or generated code); repeatable')
    ap.add_argument('--max-bytes', type=int, default=2_000_000, help='skip files larger than this (0: no limit); skipped files are listed')
    a = ap.parse_args()
    repo = a.repo
    SKIP.update(a.exclude)
    t0 = time.time()
    collection, sinks, sdks, permissions, scripts, rum_inits, imports, skipped = [], [], [], [], [], [], [], []
    aliases: dict[str, dict] = {}
    sink_rx = [(s, re.compile(s['pattern'], re.M), s.get('hints') or []) for s in SINKS['sinks']]
    chint = SINKS.get('collection_hints', {})

    def has(text: str, hints) -> bool:
        """False only when a pattern can't match: none of its hints occurs (no hints: always search)."""
        return not hints or any(h in text for h in hints)

    coll_rx = {k: re.compile(v, re.M) for k, v in SINKS['collection'].items() if k not in ('html_input', 'jsx_input')}
    html_input = re.compile(SINKS['collection']['html_input'], re.I)
    apis = [(cat, api) for cat, c in CATS['categories'].items() for api in c.get('apis', [])]
    texts = {}
    n_files = 0
    pruned: list[str] = []
    for p in files(repo, pruned):
        rel = p.relative_to(repo).as_posix()
        lang = LANG.get(p.suffix.lower())
        if not (lang or p.name in MANIFESTS or p.name in PLATFORM or p.name == 'package.json'):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if a.max_bytes and size > a.max_bytes:
            skipped.append({'file': rel, 'bytes': size, 'reason': f'larger than --max-bytes {a.max_bytes:,}'})
            continue
        n_files += 1
        if p.name == 'package.json':
            try:
                pj = json.loads(p.read_text(encoding='utf-8'))
                deps = {**pj.get('dependencies', {}), **pj.get('devDependencies', {})}
            except (ValueError, AttributeError):
                deps = {}
            sdks += [{'file': rel, 'package': d, 'vendor': SINKS['sdk_packages'][d], 'source': 'manifest'} for d in deps if d in SINKS['sdk_packages']]
            continue
        if p.name in MANIFESTS:
            t = p.read_text(encoding='utf-8', errors='replace')
            sdks += [{'file': rel, 'package': d, 'vendor': v, 'source': 'manifest'} for d, v in SINKS['sdk_packages'].items() if d in t]
            continue
        if p.name in PLATFORM:
            t = p.read_text(encoding='utf-8', errors='replace')
            tl = t.splitlines()
            for cat, api in apis:
                if api not in t:
                    continue
                for n, line in enumerate(tl, 1):
                    if api in line:
                        nxt = tl[n] if n < len(tl) else ''
                        permissions.append({'file': rel, 'line': n, 'api': api, 'category': cat,
                                            'usage_string': bool(re.search(r'<string>\s*\S', nxt)) if p.name == 'Info.plist' else None})
            continue
        text = p.read_text(encoding='utf-8', errors='replace')
        T = Text(text)
        generated = bool(GENERATED_NAME.search(p.name))
        if not generated and lang in ('js', 'ts') and size > 20000:  # a bundle: few, very long lines
            generated = max(b - a_ for a_, b in zip(T.starts, T.starts[1:] + [len(text) + 1])) > 5000
        # ---- SDK hosts: script tags, injected scripts, vendor URLs (also in generated files)
        found_hosts = set()

        def script(host, off, source):
            host = host.lower()
            n = T.line_of(off)
            if host in found_hosts:  # each host once per file, at its first mention
                return
            found_hosts.add(host)
            scripts.append({'file': rel, 'line': n, 'host': host, 'source': source, 'vendor': VENDOR_DOMAINS.get(registrable(host))})
        low = text.lower()
        if lang == 'html' and '<script' in low:
            for m in SCRIPT_TAG.finditer(text):
                script(m.group(1), m.start(), 'script_tag')
        if lang in ('js', 'ts', 'html') and '//' in text:
            if '.src' in text or 'setattribute(' in low:
                for m in SRC_ASSIGN.finditer(text):
                    script(m.group(1) or m.group(2), m.start(), 'injected_script')
            creates = 'createelement(' in low and CREATES_SCRIPT.search(text) is not None
            for m in (URL_LIT.finditer(text) if creates or any(d in low for d in VENDOR_DOMAINS) else ()):
                host, path = m.group(1), m.group(2) or ''
                if VENDOR_DOMAINS.get(registrable(host)):
                    script(host, m.start(), 'injected_script' if creates else 'vendor_url')
                elif creates and re.search(r'\.m?js(?:[?#]|$)|/js(?:[/?#]|$)', path):
                    script(host, m.start(), 'injected_script')
        for m in (re.finditer(r'\bimportScripts\(\s*["\'`](https?:)?//([^/"\'`:]+)', text) if 'importScripts(' in text else ()):
            script(m.group(2), m.start(), 'importScripts')
        if generated:
            skipped.append({'file': rel, 'bytes': size, 'reason': 'minified or generated (scanned for SDK hosts only)'})
            continue
        texts[rel] = text
        if lang in ('js', 'ts') and p.suffix.lower() in ('.jsx', '.tsx', '.js', '.ts') and re.search(r'<[A-Za-z]', text):
            for off, tag in jsx_tags(text, SINKS['collection']['jsx_input']):
                ty = jsx_attr(tag, 'type')
                if (ty or '').lower() in ('hidden', 'submit', 'button', 'checkbox', 'radio'):
                    continue
                nm, i, ac = jsx_attr(tag, 'name'), jsx_attr(tag, 'id'), jsx_attr(tag, 'autoComplete') or jsx_attr(tag, 'autocomplete')
                label = jsx_attr(tag, 'label') or jsx_attr(tag, 'placeholder') or jsx_attr(tag, 'aria-label') or ''
                if not (nm or i or ac or label):
                    continue
                cat = classify(nm, i, ac, ty, *re.findall(r'\w+', label.lower()))
                collection.append({'file': rel, 'line': T.line_of(off), 'kind': 'form_field', 'field': nm or i or ac, 'id': i,
                                   'type': ty, 'autocomplete': ac, 'label': label[:80], 'category': cat})
        for m in (IMPORT.finditer(text) if ('from' in text or 'require(' in text or 'import' in text) else ()):
            pkg = next((k for k in SINKS['sdk_packages'] if m.group(1) == k or m.group(1).startswith(k + '/')), None)
            if pkg:
                imports.append({'file': rel, 'line': T.line_of(m.start()), 'package': pkg, 'vendor': SINKS['sdk_packages'][pkg],
                                'source': 'import'})
        for m in (re.finditer(r'\b(datadogRum|DD_RUM)\.init\(', text) if '.init(' in text else ()):
            init = call_object(text, m.start())
            replay = bool(re.search(r'sessionReplaySampleRate\s*:\s*[1-9]', init) or re.search(r'startSessionReplayRecording\(', text)
                          or re.search(r'premiumSampleRate\s*:\s*[1-9]|replaySampleRate\s*:\s*[1-9]', init))
            level = re.search(r'''defaultPrivacyLevel\s*:\s*["'](\w[\w-]*)["']''', init)
            rum_inits.append({'file': rel, 'line': T.line_of(m.start()), 'replay': replay,
                              'privacy_level': level.group(1) if level else None,
                              'action_names_private': bool(re.search(r'enablePrivacyForActionName\s*:\s*true', init)),
                              'tracks_interactions': bool(re.search(r'trackUserInteractions\s*:\s*true', init)),
                              'before_send': 'beforeSend' in init, 'site': (re.search(r'''\bsite\s*:\s*["']([\w.-]+)["']''', init) or [None, None])[1],
                              'privacy_attributes': bool(re.search(r'data-dd-privacy', text))})
        if lang == 'html':
            labels = {m.group(1): re.sub(r'<[^>]+>', '', m.group(2)).strip()
                      for m in re.finditer(r'''<label[^>]*for=["']([^"']+)["'][^>]*>(.*?)</label>''', text, re.I | re.S)} if '<label' in text else {}
            for m in (html_input.finditer(text) if has(low, chint.get('html_input')) else ()):
                tag = m.group(0)
                if (attr(tag, 'type') or '').lower() in ('hidden', 'submit', 'button', 'checkbox', 'radio'):
                    continue
                nm, i, ty, ac = attr(tag, 'name'), attr(tag, 'id'), attr(tag, 'type'), attr(tag, 'autocomplete')
                label = labels.get(i or '', '')
                cat = classify(nm, i, ac, ty, *re.findall(r'\w+', label.lower()))
                collection.append({'file': rel, 'line': T.line_of(m.start()), 'kind': 'form_field', 'field': nm or i, 'id': i, 'type': ty,
                                   'autocomplete': ac, 'label': label[:80], 'category': cat})
        # simple aliases: var x = document.getElementById('field'); var y = x.value...  -> x and y stand for field
        alias = {}
        for m in (ALIAS_ELEMENT.finditer(text) if 'getElementById(' in text or 'querySelector(' in text else ()):
            alias[m.group(1)] = m.group(2)
        for m in (ALIAS_VALUE.finditer(text) if alias and '.value' in text else ()):
            if m.group(2) in alias:
                alias[m.group(1)] = alias[m.group(2)]
        for m in (ALIAS_PARAM.finditer(text) if 'URLSearchParams' in text else ()):
            alias[m.group(1)] = m.group(2)
        aliases[rel] = alias
        for key in ('py_form_read', 'js_value_read', 'js_register'):
            for m in (coll_rx[key].finditer(text) if has(text, chint.get(key)) else ()):
                collection.append({'file': rel, 'line': T.line_of(m.start()), 'kind': 'code_read', 'field': m.group('name'),
                                   'category': classify(m.group('name'))})
        for cat, api in apis:
            if '.' in api and api in text:
                i = text.find(api)
                while i >= 0:
                    collection.append({'file': rel, 'line': T.line_of(i), 'kind': 'device_api', 'field': api, 'category': cat})
                    i = text.find(api, T.starts[T.line_of(i)] if T.line_of(i) < len(T.starts) else len(text))
        seen_lines = set()
        for sk, rx, hints in sink_rx:
            if lang not in sk['langs'] or not has(text, hints):
                continue
            for m in rx.finditer(text):
                n = T.line_of(m.start())
                if (sk['id'], n) in seen_lines:
                    continue
                seen_lines.add((sk['id'], n))
                line = T.line(n)
                sinks.append({'file': rel, 'line': n, 'sink': sk['id'], 'kind': sk['kind'], 'vendor': sk.get('vendor'),
                              'excerpt': REDACT.redact(line.strip())[:160], '_view': code_view(line)})
    sinks.sort(key=lambda x: (x['file'], x['line'], [k['id'] for k in SINKS['sinks']].index(x['sink'])))
    collection.sort(key=lambda x: (x['file'], x['line']))
    # fields known from collection points; variables in the same file that hold them are named after them here
    fields = {c['field']: c['category'] for c in collection if c.get('field') and c.get('category')}
    candidates = []

    def cand(**kw):
        kw['id'] = f'S{len(candidates) + 1:03d}'
        kw['source'] = 'static'
        candidates.append(kw)
        if 'sink' in kw:
            seen_sink[(kw['sink'], kw.get('excerpt'))] = kw

    seen_sink = {}
    for s in sinks:
        line = s['excerpt']
        if (s['sink'], line) in seen_sink:
            seen_sink[(s['sink'], line)].setdefault('also_at', []).append(f'{s["file"]}:{s["line"]}')
            continue
        view = s['_view']
        names = {f for f in fields if names_field(view, f)}
        names |= {f for v, f in aliases.get(s['file'], {}).items() if f in fields and re.search(rf'(?<![\w$.]){re.escape(v)}(?![\w$])', view)}
        named = sorted(names)
        if s['sink'] == 'cache-persist':
            same = next((c for c in candidates if c.get('sink') == 'cache-persist' and c['file'] == s['file']), None)
            if same:
                same.setdefault('also_at', []).append(f'{s["file"]}:{s["line"]}')
                continue
            cand(type='insecure_storage', file=s['file'], line=s['line'], sink=s['sink'], excerpt=line, fields=[], categories=[],
                 confidence='pattern', note='a whole client data cache is persisted to device storage: every query result it holds is '
                 'kept at rest; trace which personal data the queries return, whether it is encrypted, and whether it is cleared at sign-out')
        elif s['sink'] == 'datadog-rum-init':
            continue  # judged from the whole init call below
        elif s['sink'] == 'replay-masking-off':
            cand(type='session_replay_unmasked', file=s['file'], line=s['line'], sink=s['sink'], excerpt=line, fields=[], categories=[],
                 confidence='pattern', note='input masking switched off in a session-replay configuration')
        elif s['sink'] == 'default-pii':
            on = bool(re.search(r'[:=]\s*(true|True)', line))
            cand(type='personal_data_to_third_party', file=s['file'], line=s['line'], sink=s['sink'], excerpt=line, fields=[], categories=[],
                 confidence='pattern', note=f'error tracker default PII {"on" if on else "off"}', facts={'default_pii': on,
                 'scrubber': bool(re.search(r'beforeSend|before_send', line))})
        elif named:
            typ = {'log': 'personal_data_in_logs', 'storage': 'insecure_storage', 'replay': 'session_replay_unmasked'}.get(s['kind'], 'personal_data_to_third_party')
            cats = sorted({fields[f] for f in named})
            if typ == 'personal_data_in_logs' and any(c in CATS['special'] for c in cats):
                typ = 'sensitive_category_flow'
            cand(type=typ, file=s['file'], line=s['line'], sink=s['sink'], excerpt=line, fields=named, categories=cats, confidence='pattern',
                 vendor=s.get('vendor'))
    for r in rum_inits:
        if r['replay'] and r['privacy_level'] in (None, 'allow'):
            how = 'set to allow' if r['privacy_level'] else 'not set, so the SDK version\'s default applies'
            cand(type='session_replay_unmasked', file=r['file'], line=r['line'], sink='datadog-rum-init', vendor='Datadog',
                 excerpt=f'datadogRum.init: session replay on, defaultPrivacyLevel {how}', fields=[], categories=[], confidence='pattern',
                 note='session replay records pages as users see them; with no explicit privacy level, text and inputs may be recorded. '
                      'Check the SDK version\'s default and the replays themselves',
                 facts={k: r[k] for k in ('privacy_level', 'action_names_private', 'tracks_interactions', 'before_send', 'site', 'privacy_attributes')})
        elif r['tracks_interactions'] and not r['action_names_private'] and not r['before_send']:
            cand(type='personal_data_to_third_party', file=r['file'], line=r['line'], sink='datadog-rum-init', vendor='Datadog',
                 excerpt='datadogRum.init: user interactions tracked without enablePrivacyForActionName or beforeSend', fields=[],
                 categories=[], confidence='pattern', note='click action names are taken from on-screen text, which can include personal data',
                 facts={k: r[k] for k in ('privacy_level', 'action_names_private', 'tracks_interactions', 'before_send', 'site')})
    for p in permissions:
        if p['usage_string'] is False:
            cand(type='missing_usage_description', file=p['file'], line=p['line'], api=p['api'], categories=[p['category']], fields=[], confidence='pattern')
    used = dict.fromkeys(fields, False)
    if fields:
        any_field = re.compile(r'\b(' + '|'.join(sorted(map(re.escape, fields), key=len, reverse=True)) + r')\b')
        for r, t in texts.items():
            if not r.endswith(('.html', '.htm')):
                for m in any_field.finditer(t):
                    used[m.group(1)] = True
    for f, u in used.items():
        if not u:
            c = next(c for c in collection if c.get('field') == f)
            cand(type='excess_collection', file=c['file'], line=c['line'], fields=[f], categories=[fields[f]], confidence='pattern',
                 note='collected, but no code outside the form mentions the field (agent judgment)')
    if a.semgrep:
        import sarif
        for r in sarif.read(a.semgrep, repo):
            cand(type='personal_data_to_third_party', file=r['file'], line=r['line'], sink=f'semgrep:{r["rule_id"]}', excerpt=REDACT.redact(r['snippet'])[:160],
                 fields=[], categories=[], confidence='pattern', note=r['message'][:200])
    sdks += sorted(scripts, key=lambda x: (x['file'], x['line']))
    # code imports add an SDK only where no manifest declared it (a service worker or a vendored bundle)
    declared = {x.get('package') for x in sdks}
    seen_imports = set()
    for x in imports:
        if x['package'] not in declared and (x['package'], x['file']) not in seen_imports:
            seen_imports.add((x['package'], x['file']))
            sdks.append(x)
    for x in sinks:
        x.pop('_view', None)
    skipped += [{'file': d + '/', 'bytes': None, 'reason': 'directory skipped (dependencies, build output or --exclude)'} for d in pruned]
    out = {'repo': repo.as_posix(), 'files_scanned': n_files, 'seconds': round(time.time() - t0, 1), 'skipped': skipped,
           'collection': collection, 'sinks': sinks, 'sdks': sdks, 'permissions': permissions, 'candidates': candidates}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8', newline='\n')
    print(f'{n_files} file(s) in {out["seconds"]}s: {len(collection)} collection point(s), {len(sinks)} sink(s), {len(sdks)} SDK reference(s), '
          f'{len(candidates)} candidate(s) -> {a.out}')
    if skipped:
        print(f'  skipped {len(skipped)} file(s) (listed under "skipped"): ' + ', '.join(f'{x["file"]} ({x["reason"].split(" (")[0]})' for x in skipped[:5])
              + (' ...' if len(skipped) > 5 else ''))
    hosts = sorted({(x['host'], x.get('vendor') or '-', x['source']) for x in sdks if x.get('host')})
    for h, v, src in hosts:
        print(f'  script host {h} ({v}, {src})')
    for c in candidates:
        print(f'  {c["id"]} {c["type"]:30} {c["file"]}:{c["line"]} {c.get("fields")}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
