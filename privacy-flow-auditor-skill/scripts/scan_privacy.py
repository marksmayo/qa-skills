#!/usr/bin/env python3
"""Static privacy scan: collection points, sinks, SDK inventory, mobile permissions, and candidate flows.

    python scan_privacy.py <repo> --out run/static.json [--semgrep semgrep.sarif]

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
HTML, and from importScripts() hosts in service workers.

Every candidate carries file and line; no value from the code beyond the matched line (truncated) is kept,
and string literals that look like personal data are redacted.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from match_canaries import Matcher  # noqa: E402

DATA = HERE.parent / 'data'
CATS = json.loads((DATA / 'categories.json').read_text(encoding='utf-8'))
SINKS = json.loads((DATA / 'sinks.json').read_text(encoding='utf-8'))
LANG = {'.py': 'py', '.js': 'js', '.mjs': 'js', '.jsx': 'js', '.ts': 'ts', '.tsx': 'ts', '.html': 'html', '.htm': 'html',
        '.java': 'java', '.kt': 'kt', '.swift': 'swift', '.m': 'm', '.rb': 'rb'}
SKIP = {'node_modules', '.git', 'dist', 'build', 'venv', '.venv', '__pycache__', 'Pods'}
REDACT = Matcher({'id': 'none', 'fields': {}})


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


def files(repo: Path):
    for p in sorted(repo.rglob('*')):
        if p.is_file() and not (set(p.relative_to(repo).parts) & SKIP):
            yield p


def attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\b{name}\s*=\s*["\']([^"\']*)["\']', tag, re.I)
    return m.group(1) if m else None


def jsx_tags(text: str, start_rx: str):
    """(offset, tag text) of each JSX element opening that matches start_rx, up to its closing '>' (braces skipped)."""
    for m in re.finditer(start_rx, text):
        i, depth, j = m.start(), 0, m.end()
        while j < len(text):
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
    ap.add_argument('--exclude', action='append', default=[], help='directory to skip (third-party or generated code); repeatable')
    a = ap.parse_args()
    repo = a.repo
    SKIP.update(a.exclude)
    collection, sinks, sdks, permissions, scripts, rum_inits, imports = [], [], [], [], [], [], []
    aliases: dict[str, dict] = {}
    sink_rx = [(s, re.compile(s['pattern'])) for s in SINKS['sinks']]
    texts = {}
    for p in files(repo):
        rel = p.relative_to(repo).as_posix()
        lang = LANG.get(p.suffix.lower())
        if p.name in ('package.json',):
            try:
                deps = {**json.loads(p.read_text(encoding='utf-8')).get('dependencies', {}), **json.loads(p.read_text(encoding='utf-8')).get('devDependencies', {})}
            except ValueError:
                deps = {}
            sdks += [{'file': rel, 'package': d, 'vendor': SINKS['sdk_packages'][d], 'source': 'manifest'} for d in deps if d in SINKS['sdk_packages']]
            continue
        if p.name in ('requirements.txt', 'Podfile', 'build.gradle', 'build.gradle.kts', 'pubspec.yaml', 'Gemfile'):
            t = p.read_text(encoding='utf-8', errors='replace')
            sdks += [{'file': rel, 'package': d, 'vendor': v, 'source': 'manifest'} for d, v in SINKS['sdk_packages'].items() if d in t]
            continue
        if p.name == 'AndroidManifest.xml' or p.name == 'Info.plist' or p.name == 'PrivacyInfo.xcprivacy':
            t = p.read_text(encoding='utf-8', errors='replace')
            for cat, c in CATS['categories'].items():
                for api in c.get('apis', []):
                    for n, line in enumerate(t.splitlines(), 1):
                        if api in line:
                            nxt = t.splitlines()[n] if n < len(t.splitlines()) else ''
                            permissions.append({'file': rel, 'line': n, 'api': api, 'category': cat,
                                                'usage_string': bool(re.search(r'<string>\s*\S', nxt)) if p.name == 'Info.plist' else None})
            continue
        if not lang:
            continue
        text = p.read_text(encoding='utf-8', errors='replace')
        texts[rel] = text
        lines = text.splitlines()
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
                collection.append({'file': rel, 'line': text.count('\n', 0, off) + 1, 'kind': 'form_field', 'field': nm or i or ac, 'id': i,
                                   'type': ty, 'autocomplete': ac, 'label': label[:80], 'category': cat})
        for m in re.finditer(r'''(?:\bfrom\s+|\brequire\(\s*|\bimport\s*\(\s*)["']([@\w./-]+)["']''', text):
            pkg = next((k for k in SINKS['sdk_packages'] if m.group(1) == k or m.group(1).startswith(k + '/')), None)
            if pkg:
                imports.append({'file': rel, 'line': text.count('\n', 0, m.start()) + 1, 'package': pkg, 'vendor': SINKS['sdk_packages'][pkg],
                                'source': 'import'})
        for m in re.finditer(r'\bimportScripts\(\s*["\'`](https?:)?//([^/"\'`:]+)', text):
            scripts.append({'file': rel, 'line': text.count('\n', 0, m.start()) + 1, 'host': m.group(2), 'source': 'importScripts'})
        for m in re.finditer(r'\b(datadogRum|DD_RUM)\.init\(', text):
            init = call_object(text, m.start())
            replay = bool(re.search(r'sessionReplaySampleRate\s*:\s*[1-9]', init) or re.search(r'startSessionReplayRecording\(', text)
                          or re.search(r'premiumSampleRate\s*:\s*[1-9]|replaySampleRate\s*:\s*[1-9]', init))
            level = re.search(r'defaultPrivacyLevel\s*:\s*["\'](\w[\w-]*)["\']', init)
            rum_inits.append({'file': rel, 'line': text.count('\n', 0, m.start()) + 1, 'replay': replay,
                              'privacy_level': level.group(1) if level else None,
                              'action_names_private': bool(re.search(r'enablePrivacyForActionName\s*:\s*true', init)),
                              'tracks_interactions': bool(re.search(r'trackUserInteractions\s*:\s*true', init)),
                              'before_send': 'beforeSend' in init, 'site': (re.search(r'\bsite\s*:\s*["\']([\w.-]+)["\']', init) or [None, None])[1],
                              'privacy_attributes': bool(re.search(r'data-dd-privacy', text))})
        if lang == 'html':
            labels = {m.group(1): re.sub(r'<[^>]+>', '', m.group(2)).strip() for m in re.finditer(r'<label[^>]*for=["\']([^"\']+)["\'][^>]*>(.*?)</label>', text, re.I | re.S)}
            for n, line in enumerate(lines, 1):
                for m in re.finditer(SINKS['collection']['html_input'], line, re.I):
                    tag = m.group(0)
                    if (attr(tag, 'type') or '').lower() in ('hidden', 'submit', 'button', 'checkbox', 'radio'):
                        continue
                    nm, i, ty, ac = attr(tag, 'name'), attr(tag, 'id'), attr(tag, 'type'), attr(tag, 'autocomplete')
                    label = labels.get(i or '', '')
                    cat = classify(nm, i, ac, ty, *re.findall(r'\w+', label.lower()))
                    collection.append({'file': rel, 'line': n, 'kind': 'form_field', 'field': nm or i, 'id': i, 'type': ty, 'autocomplete': ac,
                                       'label': label[:80], 'category': cat})
                for m in re.finditer(r'<script[^>]+src=["\'](https?:)?//([^/"\':]+)', line, re.I):
                    scripts.append({'file': rel, 'line': n, 'host': m.group(2)})
        # simple aliases: var x = document.getElementById('field'); var y = x.value...  -> x and y stand for field
        alias = {}
        for m in re.finditer(r'\b(?:var|let|const)\s+(\w+)\s*=\s*document\.(?:getElementById|querySelector)\([\'"]#?([\w-]+)[\'"]\)', text):
            alias[m.group(1)] = m.group(2)
        for m in re.finditer(r'\b(?:var|let|const)\s+(\w+)\s*=\s*(\w+)\.value\b', text):
            if m.group(2) in alias:
                alias[m.group(1)] = alias[m.group(2)]
        for m in re.finditer(r'\b(?:var|let|const)\s+(\w+)\s*=\s*new URLSearchParams\([^)]*\)\.get\([\'"]([\w-]+)[\'"]\)', text):
            alias[m.group(1)] = m.group(2)
        aliases[rel] = alias
        for n, line in enumerate(lines, 1):
            for key in ('py_form_read', 'js_value_read', 'js_register'):
                for m in re.finditer(SINKS['collection'][key], line):
                    collection.append({'file': rel, 'line': n, 'kind': 'code_read', 'field': m.group('name'), 'category': classify(m.group('name'))})
            for cat, c in CATS['categories'].items():
                for api in c.get('apis', []):
                    if '.' in api and api in line:
                        collection.append({'file': rel, 'line': n, 'kind': 'device_api', 'field': api, 'category': cat})
            for s, rx in sink_rx:
                if lang in s['langs'] and rx.search(line):
                    sinks.append({'file': rel, 'line': n, 'sink': s['id'], 'kind': s['kind'], 'vendor': s.get('vendor'),
                                  'excerpt': REDACT.redact(line.strip())[:160]})
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
        names = {f for f in fields if re.search(rf'(?<![\w-]){re.escape(f)}(?![\w-])', line)}
        names |= {f for v, f in aliases.get(s['file'], {}).items() if f in fields and re.search(rf'\b{re.escape(v)}\b', line)}
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
    used = {f: any(re.search(rf'\b{re.escape(f)}\b', t) for r, t in texts.items() if not r.endswith(('.html', '.htm'))) for f in fields}
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
    sdks += [{'file': s['file'], 'line': s['line'], 'host': s['host'], 'source': s.get('source', 'script_tag')} for s in scripts]
    # code imports add an SDK only where no manifest declared it (a service worker or a vendored bundle)
    declared = {x.get('package') for x in sdks}
    seen_imports = set()
    for x in imports:
        if x['package'] not in declared and (x['package'], x['file']) not in seen_imports:
            seen_imports.add((x['package'], x['file']))
            sdks.append(x)
    out = {'repo': repo.as_posix(), 'collection': collection, 'sinks': sinks, 'sdks': sdks, 'permissions': permissions, 'candidates': candidates}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(collection)} collection point(s), {len(sinks)} sink(s), {len(sdks)} SDK reference(s), {len(candidates)} candidate(s) -> {a.out}')
    for c in candidates:
        print(f'  {c["id"]} {c["type"]:30} {c["file"]}:{c["line"]} {c.get("fields")}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
