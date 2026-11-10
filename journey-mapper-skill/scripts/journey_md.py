#!/usr/bin/env python3
"""Parse, lint and render Journey Markdown files (spec: JOURNEY_FORMAT.md). Stdlib only.

The canonical copy lives in shared/scripts/journey_md.py. Skills ship identical copies, kept in
sync by tools/check_shared.py: edit the canonical copy, then run `python tools/check_shared.py --fix`.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

REQUIRED = ('id', 'title', 'persona', 'priority', 'status', 'entry_point', 'goal')
ORDER = ('id', 'title', 'domain', 'platforms', 'persona', 'priority', 'priority_reason', 'status',
         'entry_point', 'goal', 'flags', 'automation', 'tests')
ENUMS = {'priority': ('P1', 'P2', 'P3'), 'status': ('inferred', 'reviewed', 'approved'),
         'automation': ('none', 'partial', 'automated')}
LISTS = ('platforms', 'flags', 'tests')
PLATFORMS = ('web', 'ios', 'android', 'desktop', 'api', 'cli')
SECTIONS = ('Preconditions', 'Steps', 'Variants', 'Test data', 'Not automatable', 'Open questions',
            'Notes', 'Evidence')

ID_RE = re.compile(r'^JRN-[A-Z0-9]+-\d{3}$')
STEP_RE = re.compile(r'^(\d+)\.\s+\*\*Action:\*\*\s*(.*?)\s*(?:\*\*Expect:\*\*\s*(.*))?$')
EXPECT_RE = re.compile(r'^\s+\*\*Expect:\*\*\s*(.*)$')
VARIANT_RE = re.compile(r'^\*\*(V\d+)\s*[–—-]\s*(.+?)\*\*\s*\(from step (\d+)\)\s*:\s*(.*)$')
VARIANT_BODY_RE = re.compile(r'^(?:\*\*Action:\*\*\s*(.*?)\s*)?\*\*Expect:\*\*\s*(.*)$')
NA_RE = re.compile(r'^(?:step\s+(\d+)|(V\d+))\s*:\s*(.+)$', re.I)
EVIDENCE_RE = re.compile(r'^(?:step\s+(\d+)\s*:\s*)?(.+)$', re.I)
CODE_REF_RE = re.compile(r'`[^`]+:\d+`')


class JourneyError(ValueError):
    """The file can't be parsed at all (e.g. broken front matter)."""


# ---------- front matter ----------

def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'':
        return v[1:-1]
    return v

def _strip_comment(v: str) -> str:
    quote = None
    for i, c in enumerate(v):
        if quote:
            if c == quote: quote = None
        elif c in '"\'':
            quote = c
        elif c == '#' and (i == 0 or v[i - 1].isspace()):
            return v[:i].rstrip()
    return v.rstrip()

def _split_list(inner: str) -> list[str]:
    items, buf, quote = [], '', None
    for c in inner:
        if quote:
            buf += c
            if c == quote: quote = None
        elif c in '"\'':
            quote = c; buf += c
        elif c == ',':
            items.append(buf); buf = ''
        else:
            buf += c
    items.append(buf)
    return [_unquote(x) for x in items if x.strip()]

def parse_front_matter(lines: list[str]) -> dict:
    meta: dict = {}
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith('#'):
            continue
        m = re.match(r'^([A-Za-z_][\w-]*):(?:\s+(.*))?$', raw.rstrip())
        if not m:
            raise JourneyError(f'unparseable front matter line {raw.strip()!r} '
                               '(only "key: value" and "key: [a, b]" are supported)')
        key, value = m.group(1), _strip_comment(m.group(2) or '')
        if key in meta:
            raise JourneyError(f'duplicate front matter key: {key}')
        if value.startswith('['):
            if not value.endswith(']'):
                raise JourneyError(f'unterminated list for {key}')
            meta[key] = _split_list(value[1:-1])
        else:
            meta[key] = _unquote(value)
    return meta

def _quote(v, in_list: bool = False) -> str:
    v = ' '.join(str(v).split())
    needs = (not v or v[0] in '"\'[]{},#&*!|>%@`' or ' #' in v
             or (in_list and (',' in v or ']' in v)))
    if not needs:
        return v
    q = "'" if '"' in v and "'" not in v else '"'
    return q + v.replace(q, "'") + q


# ---------- body ----------

def _items(lines: list[str]) -> list[str]:
    """Bullet items with indented continuation lines joined. Non-bullet text becomes its own item."""
    items: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        m = re.match(r'^[-*]\s+(.*)$', line)
        if m:
            items.append(m.group(1).strip())
        elif line[:1].isspace() and items:
            items[-1] += ' ' + line.strip()
        else:
            items.append(line.strip())
    return [i for i in items if i.lower() != '(none)']

def _steps(lines: list[str], problems: list) -> list[dict]:
    steps: list[dict] = []
    field = 'action'
    for line in lines:
        if not line.strip():
            continue
        m = STEP_RE.match(line)
        if m:
            steps.append({'n': int(m.group(1)), 'action': m.group(2).strip(),
                          'expect': (m.group(3) or '').strip()})
            field = 'expect' if m.group(3) is not None else 'action'
            continue
        m = EXPECT_RE.match(line)
        if m and steps:
            steps[-1]['expect'] = m.group(1).strip(); field = 'expect'
            continue
        if line[:1].isspace() and steps:
            steps[-1][field] = (steps[-1][field] + ' ' + line.strip()).strip()
            continue
        problems.append(('error', f'Steps: unparseable line {line.strip()!r}'))
    return steps

def _variants(lines: list[str], problems: list) -> list[dict]:
    variants = []
    for text in _items(lines):
        m = VARIANT_RE.match(text)
        if not m:
            problems.append(('error', f'Variants: unparseable entry {text!r}'))
            continue
        body = VARIANT_BODY_RE.match(m.group(4).strip())
        if not body:
            problems.append(('error', f'Variants: {m.group(1)} has no **Expect:**'))
        variants.append({'id': m.group(1), 'name': m.group(2).strip(), 'from_step': int(m.group(3)),
                         'action': (body.group(1) or '').strip() if body else '',
                         'expect': body.group(2).strip() if body else ''})
    return variants

def _not_automatable(lines: list[str], problems: list) -> list[dict]:
    out = []
    for text in _items(lines):
        m = NA_RE.match(text)
        if not m:
            problems.append(('strict', f'Not automatable: unstructured entry {text!r} '
                                       '(use "step N: reason" or "Vn: reason")'))
            out.append({'target': None, 'reason': text})
            continue
        out.append({'target': f'step {m.group(1)}' if m.group(1) else m.group(2).upper(),
                    'reason': m.group(3).strip()})
    return out

def _evidence(lines: list[str]) -> list[dict]:
    out = []
    for text in _items(lines):
        m = EVIDENCE_RE.match(text)
        out.append({'step': int(m.group(1)) if m.group(1) else None, 'text': m.group(2).strip()})
    return out

def _raw(lines: list[str]) -> str:
    text = '\n'.join(lines).strip('\n')
    return '' if text.strip().lower() in ('', '- (none)', '* (none)', '(none)') else text

def parse_journey(text: str) -> dict:
    lines = text.lstrip('﻿').replace('\r\n', '\n').split('\n')
    if not lines or lines[0].strip() != '---':
        raise JourneyError('missing front matter (the file must start with ---)')
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
    if end is None:
        raise JourneyError('unterminated front matter (no closing ---)')
    meta = parse_front_matter(lines[1:end])
    problems: list = []
    sections: dict[str, list[str]] = {}
    order: list[str] = []
    current = None
    for line in lines[end + 1:]:
        m = re.match(r'^##\s+(.+?)\s*$', line)
        if m:
            current = m.group(1)
            if current in sections:
                problems.append(('error', f'duplicate section: {current}'))
            else:
                order.append(current)
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    get = lambda name: sections.get(name, [])
    return {
        'meta': meta,
        'preconditions': _items(get('Preconditions')),
        'steps': _steps(get('Steps'), problems),
        'variants': _variants(get('Variants'), problems),
        'test_data': _raw(get('Test data')),
        'not_automatable': _not_automatable(get('Not automatable'), problems),
        'open_questions': _items(get('Open questions')),
        'notes': _raw(get('Notes')),
        'evidence': _evidence(get('Evidence')),
        'section_order': order,
        'problems': problems,
    }

def load(path) -> dict:
    return parse_journey(Path(path).read_text(encoding='utf-8'))


# ---------- lint ----------

def lint(j: dict, path=None) -> list[tuple[str, str]]:
    """Return (level, message) pairs. level is 'error', 'strict' (error under --strict) or 'warning'."""
    p = list(j['problems'])
    meta = j['meta']
    for k in REQUIRED:
        if not meta.get(k):
            p.append(('error', f'missing required front matter: {k}'))
    for k, allowed in ENUMS.items():
        if k in meta and meta[k] not in allowed:
            p.append(('error', f'{k} must be one of {", ".join(allowed)} (got {meta[k]!r})'))
    for k in LISTS:
        if k in meta and not isinstance(meta[k], list):
            p.append(('error', f'{k} must be a list like [a, b]'))
    for k in meta:
        if k not in ORDER:
            p.append(('strict', f'unknown front matter key: {k}'))
    if meta.get('id') and not ID_RE.match(str(meta['id'])):
        p.append(('error', f'id {meta["id"]!r} must match JRN-<DOMAIN>-<NNN>'))
    if not meta.get('domain'):
        p.append(('strict', 'missing domain'))
    for pl in meta.get('platforms') or []:
        if isinstance(meta.get('platforms'), list) and pl not in PLATFORMS:
            p.append(('strict', f'unknown platform {pl!r}'))
    if meta.get('automation') in ('partial', 'automated') and not meta.get('tests'):
        p.append(('error', f'automation is {meta["automation"]} but tests is empty'))
    for s in j['section_order']:
        if s not in SECTIONS:
            p.append(('strict', f'unknown section: {s}'))
    if 'Steps' not in j['section_order']:
        p.append(('error', 'missing ## Steps section'))
    steps = j['steps']
    if not steps:
        p.append(('error', 'journey has no steps'))
    for i, s in enumerate(steps, 1):
        if s['n'] != i:
            p.append(('error', f'step numbering: expected {i}, found {s["n"]}'))
        if not s['action']:
            p.append(('error', f'step {s["n"]} has no **Action:**'))
        if not s['expect']:
            p.append(('error', f'step {s["n"]} has no **Expect:**'))
    nums = {s['n'] for s in steps}
    vids: set[str] = set()
    for v in j['variants']:
        if v['id'] in vids:
            p.append(('error', f'duplicate variant id {v["id"]}'))
        vids.add(v['id'])
        if v['from_step'] not in nums:
            p.append(('error', f'{v["id"]} branches from unknown step {v["from_step"]}'))
    for na in j['not_automatable']:
        t = na['target']
        if t and t.startswith('step ') and int(t[5:]) not in nums:
            p.append(('error', f'Not automatable refers to unknown {t}'))
        elif t and not t.startswith('step ') and t not in vids:
            p.append(('error', f'Not automatable refers to unknown variant {t}'))
    ev = j['evidence']
    if not ev:
        p.append(('strict', 'no ## Evidence entries'))
    for e in ev:
        if e['step'] is not None and e['step'] not in nums:
            p.append(('error', f'evidence refers to unknown step {e["step"]}'))
        if not CODE_REF_RE.search(e['text']):
            p.append(('strict', f'evidence has no `path:line` reference: {e["text"][:60]!r}'))
    if ev:
        for s in steps:
            if not any(e['step'] == s['n'] for e in ev):
                p.append(('strict', f'step {s["n"]} has no evidence'))
    if path is not None and meta.get('id') and not Path(path).name.startswith(str(meta['id'])):
        p.append(('strict', f'file name should start with {meta["id"]}'))
    return p

def is_journey_file(path: Path) -> bool:
    try:
        with open(path, encoding='utf-8') as fh:
            return fh.readline().lstrip('﻿').strip() == '---'
    except (OSError, UnicodeDecodeError):
        return False

def iter_journey_files(paths) -> tuple[list[Path], list[Path]]:
    """Return (journey files, skipped files). Explicitly named files are never skipped."""
    found, skipped = [], []
    for p in map(Path, paths):
        if p.is_dir():
            for f in sorted(p.rglob('*.md')):
                (found if is_journey_file(f) else skipped).append(f)
        else:
            found.append(p)
    return found, skipped

def lint_paths(paths, strict: bool = False) -> dict:
    files, skipped = iter_journey_files(paths)
    results, ids = [], {}
    for f in files:
        errors, warnings, jid = [], [], None
        try:
            j = load(f)
            jid = j['meta'].get('id')
            for level, msg in lint(j, f):
                (errors if level == 'error' or (level == 'strict' and strict) else warnings).append(msg)
        except (JourneyError, OSError, UnicodeDecodeError) as exc:
            errors.append(str(exc))
        if jid and not f.name.endswith('.proposed.md'):
            if jid in ids:
                errors.append(f'duplicate journey id {jid} (also in {ids[jid]})')
            ids.setdefault(jid, str(f))
        results.append({'file': str(f), 'id': jid, 'errors': errors, 'warnings': warnings})
    return {'files': results, 'skipped': [str(s) for s in skipped],
            'errors': sum(len(r['errors']) for r in results),
            'warnings': sum(len(r['warnings']) for r in results)}


# ---------- render ----------

def _variant_line(v: dict) -> str:
    action = f"**Action:** {v['action']} " if v.get('action') else ''
    return f"**{v['id']} – {v['name']}** (from step {v['from_step']}): {action}**Expect:** {v['expect']}"

def render(j: dict) -> str:
    """Render a journey dict (the shape parse_journey returns) to Journey Markdown."""
    meta = j['meta']
    out = ['---']
    for k in list(ORDER) + [k for k in meta if k not in ORDER]:
        if k not in meta or meta[k] is None:
            continue
        v = meta[k]
        if isinstance(v, (list, tuple)):
            out.append(f'{k}: [' + ', '.join(_quote(x, True) for x in v) + ']')
        else:
            out.append(f'{k}: {_quote(v)}')
    out += ['---', '']
    flat = lambda s: ' '.join(str(s).split())

    def bullets(title, items):
        out.append(f'## {title}')
        out.extend(f'- {flat(x)}' for x in items) if items else out.append('- (none)')
        out.append('')

    def raw(title, value):
        if isinstance(value, (list, tuple)):
            bullets(title, value)
        else:
            out.extend([f'## {title}', value.strip() if value and value.strip() else '- (none)', ''])

    bullets('Preconditions', j.get('preconditions') or [])
    out.append('## Steps')
    for i, s in enumerate(j.get('steps') or [], 1):
        out.append(f"{i}. **Action:** {flat(s['action'])}")
        out.append(f"   **Expect:** {flat(s['expect'])}")
    out.append('')
    bullets('Variants', [_variant_line(v) for v in j.get('variants') or []])
    raw('Test data', j.get('test_data') or '')
    bullets('Not automatable', [f"{x['target']}: {x['reason']}" if x.get('target') else x['reason']
                                for x in j.get('not_automatable') or []])
    bullets('Open questions', j.get('open_questions') or [])
    if (j.get('notes') or '').strip():
        raw('Notes', j['notes'])
    bullets('Evidence', [(f"Step {e['step']}: " if e.get('step') else '') + e['text']
                         for e in j.get('evidence') or []])
    return '\n'.join(out).rstrip() + '\n'


# ---------- CLI ----------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Parse and lint Journey Markdown files.')
    sub = ap.add_subparsers(dest='cmd', required=True)
    lp = sub.add_parser('lint', help='lint journey files or directories')
    lp.add_argument('paths', nargs='+')
    lp.add_argument('--strict', action='store_true', help='treat strict-level warnings as errors')
    lp.add_argument('--json', action='store_true', help='print results as JSON')
    pp = sub.add_parser('parse', help='print a parsed journey as JSON')
    pp.add_argument('file')
    args = ap.parse_args(argv)

    if args.cmd == 'parse':
        try:
            j = load(args.file)
        except (JourneyError, OSError) as exc:
            print(f'INVALID: {exc}', file=sys.stderr); return 1
        print(json.dumps(j, indent=2, ensure_ascii=False)); return 0

    res = lint_paths(args.paths, args.strict)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        for r in res['files']:
            if r['errors'] or r['warnings']:
                print(r['file'])
                for e in r['errors']: print(f'  ERROR: {e}')
                for w in r['warnings']: print(f'  WARN:  {w}')
        print(f"{len(res['files'])} journey file(s), {res['errors']} error(s), "
              f"{res['warnings']} warning(s), {len(res['skipped'])} non-journey file(s) skipped")
    return 1 if res['errors'] else 0

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
