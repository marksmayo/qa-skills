#!/usr/bin/env python3
"""Run reproduction attempts for one bug report and record what happened (Playwright for Python).

    python reproduce.py spec.json --out attempts.json [--artifacts DIR] [--base DIR] [--only A1]

A spec lists candidate reproductions. Each candidate runs `runs` times (default 3); every run
is one attempt with its own screenshot, console log and (first run only) Playwright trace:

    {"key": "TASK-11", "version": "1.2.0", "version_selector": "#version",
     "authorised_by": null, "allow_form_submission": false, "auth": null,
     "failure": {"any": [{"text_contains": "#tasks", "text": "Pay the water bill"}]},
     "observe": ["#tasks", "#status"],
     "candidates": [
       {"id": "A1", "label": "as reported", "variation": null, "runs": 3, "browser": "chromium",
        "environment": {"timezone": "Australia/Sydney", "locale": "en-AU", "clock": "2026-11-12T08:00:00+11:00",
                        "viewport": "1280x800", "cpu_throttle": null, "network": null,
                        "local_storage": {"key": "json string"}},
        "steps": [{"goto": "http://127.0.0.1:8141/"}, {"click": "#filter-today"}],
        "assumptions": ["..."]}]}

Failure checks (the report's failure, written as something a browser can observe):
    {"text_contains": sel, "text": s}      element text contains s
    {"text_missing": sel, "text": s}       element text doesn't contain s
    {"count": sel, "has_text": s, "gte": n} at least n matching elements
    {"dialog_matches": regex}              an alert/confirm/prompt message matches (dialogs are dismissed)
    {"page_error_matches": regex}          an uncaught page error matches
    {"console_error_matches": regex}       a console error matches
    {"no_download": true}                  the steps started no download
Combine with {"all": [...]} or {"any": [...]}.

Results: matched (the failure checks hold), different (no match, but a page error, console error
or dialog appeared that the report doesn't describe), no_failure, errored (a step or the
browser failed; the error is recorded). The version the page shows is read from
version_selector and recorded as the attempt's version, whatever the spec says.

An app behind sign-in takes "auth": {"storage_state": "<file outside the repo>"}, a session a person
captured with `python browser_harness.py capture-session --url <app> --out <file>` (or a login recipe; see
browser_harness.py). The attempts record only that a session was used ("auth": {"method": ...}), never its
contents or path, and the first run's Playwright trace is not taken in a signed-in session.

Safety: every target must be loopback or have authorised_by (browser_harness.check_authorised);
non-GET requests are aborted unless allow_form_submission. Console text, dialog messages and
observed text are redacted (emails, card numbers, bearer tokens, key=value secrets).
"""
from __future__ import annotations
import argparse, json, re, sys, time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from browser_harness import Harness, check_authorised  # noqa: E402

REDACT = [
    (re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+'), '[email]'),
    (re.compile(r'\b(?:\d[ -]?){13,19}\b'), '[card]'),
    (re.compile(r'(?i)\bbearer\s+[\w.~+/-]{8,}=*'), 'Bearer [secret]'),
    (re.compile(r'(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+'), r'\1=[secret]'),
]


def redact(s):
    if not isinstance(s, str):
        return s
    for rx, rep in REDACT:
        s = rx.sub(rep, s)
    return s


def now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def evaluate(check: dict, page, seen: dict) -> tuple[bool, str]:
    if 'all' in check or 'any' in check:
        parts = [evaluate(c, page, seen) for c in check.get('all') or check.get('any')]
        ok = all(p[0] for p in parts) if 'all' in check else any(p[0] for p in parts)
        return ok, '; '.join(p[1] for p in parts if p[0] == ok) or '; '.join(p[1] for p in parts)
    if 'text_contains' in check or 'text_missing' in check:
        sel = check.get('text_contains') or check.get('text_missing')
        text = page.inner_text(sel, timeout=3000) if page.query_selector(sel) else ''
        has = check['text'] in text
        ok = has if 'text_contains' in check else not has
        return ok, f'{sel} {"contains" if has else "does not contain"} "{check["text"]}"'
    if 'count' in check:
        loc = page.locator(check['count'])
        if check.get('has_text'):
            loc = loc.filter(has_text=check['has_text'])
        n = loc.count()
        return n >= check['gte'], f'{n} element(s) {check["count"]}' + (f' with "{check["has_text"]}"' if check.get('has_text') else '')
    for key, src in (('dialog_matches', 'dialogs'), ('page_error_matches', 'page_errors'), ('console_error_matches', 'console_errors')):
        if key in check:
            hit = next((m for m in seen[src] if re.search(check[key], m)), None)
            return hit is not None, f'{src[:-1].replace("_", " ")} "{hit}"' if hit else f'no {src[:-1].replace("_", " ")} matching /{check[key]}/'
    if 'no_download' in check:
        return not seen['downloads'], 'no download started' if not seen['downloads'] else f'downloaded {", ".join(seen["downloads"])}'
    raise ValueError(f'unknown failure check {check}')


def run_one(h: Harness, spec: dict, cand: dict, n: int, art: Path, base: Path) -> dict:
    env = cand.get('environment') or {}
    rid = f'{cand["id"]}-{n}'
    out_dir = art / rid
    out_dir.mkdir(parents=True, exist_ok=True)
    rec = {'id': rid, 'candidate': cand['id'], 'label': cand.get('label'), 'variation': cand.get('variation'), 'channel': 'browser',
           'browser': h.browser_name, 'browser_version': h.version, 'version': None, 'version_stated': spec.get('version'),
           'environment': {k: env.get(k) for k in ('viewport', 'timezone', 'locale', 'clock', 'cpu_throttle', 'network')},
           'steps': cand['steps'], 'assumptions': cand.get('assumptions', []), 'started': now()}
    if env.get('local_storage'):
        rec['environment']['local_storage_keys'] = sorted(env['local_storage'])
    seen = {'dialogs': [], 'downloads': []}
    t0 = time.time()
    page = h.new_page(viewport=env.get('viewport') or '1280x800', timezone=env.get('timezone'), locale=env.get('locale'),
                      clock=env.get('clock'), cpu_throttle=env.get('cpu_throttle'), network=env.get('network'), trace=(n == 1))
    page.on('dialog', lambda d: (seen['dialogs'].append(redact(d.message)), d.dismiss()))
    page.on('download', lambda d: seen['downloads'].append(d.suggested_filename))
    if env.get('local_storage'):
        page.add_init_script('(items => { if (!sessionStorage.getItem("__seeded")) { for (const [k, v] of Object.entries(items)) '
                             'localStorage.setItem(k, v); sessionStorage.setItem("__seeded", "1"); } })('
                             + json.dumps(env['local_storage']) + ');')
    error = None
    try:
        h.act(page, cand['steps'])
        page.wait_for_timeout(int(spec.get('settle_ms', 300)))
    except Exception as e:  # a failed step is recorded, not raised
        error = f'{type(e).__name__}: {str(e).splitlines()[0]}'
    log = h.log(page)
    seen['page_errors'] = [redact(f'{e["name"]}: {e["message"]}') for e in log['page_errors']]
    seen['console_errors'] = [redact(c['text']) for c in log['console'] if c['type'] == 'error']
    try:
        if spec.get('version_selector') and page.query_selector(spec['version_selector']):
            rec['version'] = page.inner_text(spec['version_selector']).strip()
        rec['observed'] = {sel: redact(page.inner_text(sel)) if page.query_selector(sel) else None for sel in spec.get('observe', [])}
        matched, why = evaluate(spec['failure'], page, seen)
    except Exception as e:
        matched, why = False, ''
        error = error or f'{type(e).__name__}: {str(e).splitlines()[0]}'
    shot = h.screenshot(page, out_dir / 'screenshot.png')
    trace = out_dir / 'trace.zip' if n == 1 else None
    h.close_page(page, trace)
    rel = lambda p: p.resolve().relative_to(base.resolve()).as_posix()  # noqa: E731
    logs = {'console': [{'type': c['type'], 'text': redact(c['text'])} for c in log['console']], 'page_errors': seen['page_errors'],
            'failed_requests': log['failed_requests'], 'dialogs': seen['dialogs'], 'downloads': seen['downloads'],
            'blocked_requests': h.blocked, 'not_applied': log['not_applied']}
    (out_dir / 'console.json').write_text(json.dumps(logs, indent=2, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    unexpected = seen['page_errors'] or seen['console_errors'] or seen['dialogs']
    rec['result'] = 'errored' if error else 'matched' if matched else 'different' if unexpected else 'no_failure'
    rec['observed_signature'] = why if matched else (unexpected[0] if unexpected and not error else None)
    rec['checks'] = why
    rec.update({'dialogs': seen['dialogs'], 'downloads': seen['downloads'], 'page_errors': seen['page_errors'],
                'console_errors': seen['console_errors'], 'not_applied': log['not_applied'], 'error': error,
                'duration_ms': int((time.time() - t0) * 1000),
                'artifacts': [rel(shot), rel(out_dir / 'console.json')] + ([rel(trace)] if trace else [])})
    return rec


def run_spec(spec: dict, art: Path, base: Path, only: set | None = None, session: dict | None = None) -> list[dict]:
    """Run the candidates; when a signed-in session was used, session (if given) gets {"method": ...}."""
    urls = [s['goto'] for c in spec['candidates'] for s in c['steps'] if 'goto' in s]
    check_authorised(urls, spec.get('authorised_by'))
    out = []
    for cand in spec['candidates']:
        if only and cand['id'] not in only:
            continue
        cfg = {'browser': cand.get('browser', 'chromium'), 'authorised_by': spec.get('authorised_by'),
               'allow_form_submission': spec.get('allow_form_submission', False), 'rate_limit_seconds': 0}
        if spec.get('auth'):
            cfg['auth'] = spec['auth']
        with Harness(cfg) as h:
            if h.auth_info and session is not None:
                session['method'] = h.auth_info['method']
            for n in range(1, int(cand.get('runs', 3)) + 1):
                rec = run_one(h, spec, cand, n, art, base)
                print(f'{rec["id"]} {rec["browser"]} v{rec["version"]}: {rec["result"]}  {rec["checks"] or rec["error"] or ""}')
                out.append(rec)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('spec', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--artifacts', type=Path, help='default: <out dir>/artifacts/<key>')
    ap.add_argument('--base', type=Path, help='artifact paths are recorded relative to this (default: the out file\'s directory)')
    ap.add_argument('--only', nargs='*', help='run only these candidate ids (appends to --out)')
    args = ap.parse_args(argv)
    spec = json.loads(args.spec.read_text(encoding='utf-8'))
    base = args.base or args.out.parent
    art = args.artifacts or args.out.parent / 'artifacts' / spec['key']
    prev = json.loads(args.out.read_text(encoding='utf-8'))['attempts'] if args.only and args.out.exists() else []
    only = set(args.only) if args.only else None
    session: dict = {}
    runs = run_spec(spec, art, base, only, session)
    keep = [r for r in prev if r['candidate'] not in (only or set())]
    doc = {'key': spec['key'], 'spec': args.spec.name, 'version_stated': spec.get('version'), 'failure': spec['failure'],
           'generated': now(), **({'auth': session} if session else {}), 'attempts': sorted(keep + runs, key=lambda r: (r['candidate'], int(r['id'].rsplit('-', 1)[1])))}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    m = sum(r['result'] == 'matched' for r in doc['attempts'])
    print(f'{spec["key"]}: {m} of {len(doc["attempts"])} attempts matched -> {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
