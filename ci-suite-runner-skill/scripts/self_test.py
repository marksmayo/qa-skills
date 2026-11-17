#!/usr/bin/env python3
"""Self-test for the CI suite runner (stdlib only, no network).

    python self_test.py

Checks that the example can be regenerated on any date: every trigger in examples/commands.sh that
is meant to succeed (no `|| true`) is still authorised by examples/preauth.json when the real clock is
90 days, and ten years, ahead of today, and every trigger meant to be refused is still refused. The
trigger itself is never sent: the call to the CI is replaced by one that stops the run.
Also checks the --as-of rules: an as-of date is honoured only for a loopback API base, is recorded in
the authorisation, and still refuses a grant that had expired on it; the validator rejects an as-of
date on a record whose API is not loopback, and judges a grant's expiry on the recorded as-of date.
"""
from __future__ import annotations
import contextlib, io, json, os, re, shlex, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True   # no __pycache__ in the skill folder when this script imports its siblings
import run_suite  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    print(('ok    ' if cond else 'FAIL  ') + what)
    if not cond:
        FAILS.append(what)


CALLS: list[str] = []


def no_network(method, url, system, body=None, raw=False):
    """Stands in for the CI: records the request and answers 599, so the trigger stops after recording it."""
    CALLS.append(f'{method} {url}')
    return 599, {'message': 'not sent: self-test'}


def clock_at(day_offset: int):
    """A datetime class whose now() is the real time shifted by day_offset days."""
    real = datetime

    class Shifted(real):
        @classmethod
        def now(cls, tz=None):
            return real.now(tz) + timedelta(days=day_offset)
    return Shifted


def run_main(argv: list[str]) -> tuple[int | str, str]:
    old = sys.argv
    sys.argv = ['run_suite.py'] + argv
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                n = len(CALLS)
                code = run_suite.main()
                if any(c.startswith('POST ') for c in CALLS[n:]):
                    code = 'sent'
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
                out.write(str(e.code))
    finally:
        sys.argv = old
    return code, out.getvalue()


def commands() -> list[list[str]]:
    """The run_suite.py commands of examples/commands.sh, continuation lines joined, with '|| true' kept as a marker."""
    text = (SKILL / 'examples' / 'commands.sh').read_text(encoding='utf-8').replace('\\\n', ' ')
    env = {}
    out = []
    for line in text.splitlines():
        line = line.split(' #')[0].strip()
        m = re.fullmatch(r'([A-Z_]+)=(\S+)(?:\s.*)?', line)
        if m and 'run_suite.py' not in line:
            env[m.group(1)] = m.group(2).split(';')[0]
        if 'scripts/run_suite.py' not in line:
            continue
        for k, v in env.items():
            line = line.replace('$' + k, v)
        words = shlex.split(line)
        out.append(words[words.index('scripts/run_suite.py') + 1:])
    return out


def regenerable_on_any_date() -> None:
    cmds = commands()
    check(bool(cmds), 'commands.sh: run_suite.py commands found')
    plans = {c[c.index('--out') + 1]: c for c in cmds if c[0] == 'plan'}
    triggers = [c for c in cmds if c[0] == 'trigger' and '--preauth' in c]
    check(bool(triggers), 'commands.sh: triggers under the pre-authorisation found')
    cwd = os.getcwd()
    os.chdir(SKILL)   # commands.sh runs from the skill directory
    real_dt, real_call = run_suite.datetime, run_suite.call
    try:
        for days in (90, 3650):
            for t in triggers:
                expect_sent = '||' not in t
                args = [w for w in t[:t.index('||')]] if '||' in t else list(t)
                run_dir = args[args.index('--run') + 1]
                with tempfile.TemporaryDirectory() as d:
                    plan = list(plans[run_dir])
                    plan[plan.index('--out') + 1] = d
                    run_suite.datetime, run_suite.call = real_dt, no_network
                    code, msg = run_main(plan)
                    if code != 0:
                        check(False, f'plan for {run_dir} failed: {msg.strip()[-200:]}')
                        continue
                    args[args.index('--run') + 1] = d
                    run_suite.datetime = clock_at(days)
                    code, msg = run_main(args)
                what = 'authorised' if expect_sent else 'refused'
                ok = (code == 'sent') if expect_sent else (code == 2)
                check(ok, f'commands.sh: the trigger of {run_dir} is still {what} with the clock {days} days ahead'
                          + ('' if ok else f' (got {code}: {msg.strip().splitlines()[0] if msg.strip() else ""})'))
    finally:
        run_suite.datetime, run_suite.call = real_dt, real_call
        os.chdir(cwd)


def planned(d: str, api: str) -> str:
    code, msg = run_main(['plan', '--catalogue', 'examples/suites.json', '--suite', 'checkout-regression', '--environment', 'staging',
                          '--out', d, '--api-base', api])
    if code != 0:
        raise RuntimeError(f'plan failed: {msg}')
    return d


def as_of_rules() -> None:
    cwd = os.getcwd()
    os.chdir(SKILL)
    real_dt, real_call = run_suite.datetime, run_suite.call
    run_suite.call, run_suite.datetime = no_network, clock_at(3650)
    try:
        with tempfile.TemporaryDirectory() as d:
            run = planned(d + '/a', 'http://127.0.0.1:9/github')
            code, msg = run_main(['trigger', '--run', run, '--preauth', 'examples/preauth.json', '--as-of', '2026-11-11'])
            rec = json.loads((Path(run) / 'suite_run.json').read_text(encoding='utf-8'))
            au = (rec['authorisations'] or [{}])[-1]
            check(code == 'sent' and au.get('kind') == 'preauth' and au.get('as_of') == '2026-11-11',
                  f'--as-of on a loopback stub: the grant is judged on that date and the date is recorded (got {code}, {au})')

            run = planned(d + '/b', 'http://127.0.0.1:9/github')
            code, msg = run_main(['trigger', '--run', run, '--preauth', 'examples/preauth.json', '--as-of', '2026-12-01'])
            check(code == 2 and 'covers' in msg, f'--as-of after the grant expired is still refused (got {code})')

            run = planned(d + '/c', 'https://ci.example.com/github')
            n = len(CALLS)
            code, msg = run_main(['trigger', '--run', run, '--preauth', 'examples/preauth.json', '--as-of', '2026-11-11'])
            check(code not in (0, 'sent') and len(CALLS) == n and 'loopback' in msg,
                  f'--as-of is refused for a CI that is not on loopback, and nothing is sent (got {code}: {msg.strip()[:120]})')

            run = planned(d + '/e', 'http://127.0.0.1:9/github')
            code, msg = run_main(['trigger', '--run', run, '--confirmed-by', 'Release manager, in chat, today', '--as-of', '2026-11-11'])
            check(code not in (0, 'sent') and '--preauth only' in msg, f'--as-of without --preauth is an error (got {code})')
    finally:
        run_suite.datetime, run_suite.call = real_dt, real_call
        os.chdir(cwd)


def validator_as_of() -> None:
    import shutil
    import validate_suite_run as vsr
    cwd = os.getcwd()
    os.chdir(SKILL)
    try:
        def validate(edit) -> tuple[int, str]:
            with tempfile.TemporaryDirectory() as d:
                run = Path(d) / 'run'
                shutil.copytree(SKILL / 'examples' / 'runs' / 'checkout-staging', run)
                rec = json.loads((run / 'suite_run.json').read_text(encoding='utf-8'))
                edit(rec)
                (run / 'suite_run.json').write_text(json.dumps(rec, indent=1), encoding='utf-8')
                old, out = sys.argv, io.StringIO()
                sys.argv = ['validate_suite_run.py', str(run / 'suite_run.json'), '--preauth', 'examples/preauth.json']
                try:
                    with contextlib.redirect_stdout(out):
                        code = vsr.main()
                finally:
                    sys.argv = old
                return code, out.getvalue()

        def sent(rec):
            return next(x for x in rec['authorisations'] if x['decision'] == 'sent')

        code, msg = validate(lambda r: None)
        check(code == 0, f'validator: the committed staging run is valid ({msg.strip()[-160:]})')

        def late(r):
            sent(r)['as_of'] = '2026-12-05'
        code, msg = validate(late)
        check(code == 1 and 'expired' in msg, 'validator: a grant that had expired on the recorded as-of date is rejected')

        def remote(r):
            sent(r).setdefault('as_of', r['trigger']['at'][:10])
            for k in ('api_base',):
                r[k] = r[k].replace('http://127.0.0.1:8170', 'https://ci.example.com')
            r['plan']['url'] = r['trigger']['url'] = r['plan']['url'].replace('http://127.0.0.1:8170', 'https://ci.example.com')
        code, msg = validate(remote)
        check(code == 1 and 'loopback' in msg, 'validator: an as-of date on a record whose CI is not on loopback is rejected')
    finally:
        os.chdir(cwd)


def main() -> int:
    for t in (regenerable_on_any_date, as_of_rules, validator_as_of):
        try:
            t()
        except Exception as exc:  # a crash in one group is a failure, not the end of the run
            check(False, f'{t.__name__} raised {exc.__class__.__name__}: {exc}')
    print(f'\n{len(FAILS)} failure(s)' if FAILS else '\nall checks passed')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
