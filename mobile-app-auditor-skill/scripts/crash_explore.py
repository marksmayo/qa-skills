#!/usr/bin/env python3
"""Replay every recorded crash and ANR on a fresh app state, and optionally run a seeded monkey.

    python crash_explore.py --run run --replay [--attempts 2] [--offline]
    python crash_explore.py --run run --monkey --seed 4242 --events 500 [--throttle 300]

--replay: for each crash in <run>/crashes.json, clear the app's data (pm clear), launch, replay its steps
and watch for the same crash (same exception, or the same kind of ANR timeout). Writes <run>/replays.json; a crash replayed
at least once has confidence "reproduced".
--monkey (off by default: it taps unpredictably): Android monkey restricted to the package, seeded, system
keys off, throttled. Its seed and event log go to <run>/monkey/; crashes it finds are appended to
crashes.json with the seed and the event log as their steps (likelihood "random input only").
Device changes are journaled in <run>/device_changes.json before they are made (see adb_device.py restore).
"""
from __future__ import annotations
import argparse, json, re, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from adb_device import Device, PendingRestore, short_reason  # noqa: E402
from explore import Explorer, now  # noqa: E402


class Args:
    settle = 6.0
    input_delay = 2.0
    anr_trace_wait = 30.0
    launch_wait = 2.0
    step_wait = 1.5
    no_back = True


def what(c: dict | None) -> str | None:
    """What a crash is, for comparing a replay with the recording: the exception, or the ANR's kind of timeout."""
    if not c:
        return None
    return c.get('exception') or short_reason(c.get('reason'))


def replay(d: Device, run: Path, graph: dict, attempts: int) -> list[dict]:
    crashes = json.loads((run / 'crashes.json').read_text(encoding='utf-8'))
    out = []
    for c in crashes:
        if c.get('seed') is not None:
            out.append({'crash': c['id'], 'status': 'not_replayed', 'reason': 'found by monkey: replay the seed and event log by hand'})
            continue
        tries = []
        for i in range(attempts):
            ex = Explorer(d, graph['package'], graph['activity'], run / 'replays', Args())
            d.shell(f'pm clear {graph["package"]}', check=False)
            d.logcat_clear()
            ok = ex.replay(c['steps'][:-1])
            since = d.device_time()
            if ok:
                ok = ex.do(c['steps'][-1])
            got = ex.health(c['steps'], c['screen'], since) if ok else None
            same = bool(got) and got['kind'] == c['kind'] and what(got) == what(c)
            tries.append({'attempt': i + 1, 'at': now(), 'steps_replayed': ok, 'reproduced': same, 'got': what(got),
                          **({'input_event': got.get('input_event')} if got and got.get('input_event') else {})})
            d.rotate(False)
        out.append({'crash': c['id'], 'status': 'reproduced' if any(t['reproduced'] for t in tries) else 'not_reproduced',
                    'attempts': tries, 'fresh_state': 'pm clear before each attempt'})
        print(f'{c["id"]}: {out[-1]["status"]} ({sum(t["reproduced"] for t in tries)}/{attempts})', flush=True)
    return out


def monkey(d: Device, run: Path, graph: dict, seed: int, events: int, throttle: int) -> dict:
    pkg = graph['package']
    d.logcat_clear()
    log = d.shell(f'monkey -p {pkg} -s {seed} --pct-syskeys 0 --throttle {throttle} -v {events}', timeout=events * throttle // 1000 + 120, check=False)
    (run / 'monkey').mkdir(exist_ok=True)
    (run / 'monkey' / f'seed-{seed}.log').write_text(log, encoding='utf-8', newline='\n')
    crash = [l for l in d.logcat('crash') if pkg in l or 'FATAL' in l]
    anr = [l for l in d.logcat('system') if f'ANR in {pkg}' in l]
    return {'seed': seed, 'events': events, 'throttle_ms': throttle, 'log': f'monkey/seed-{seed}.log',
            'crash_lines': len(crash), 'anr_lines': len(anr), 'finished': 'Monkey finished' in log}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--run', type=Path, required=True)
    ap.add_argument('--serial')
    ap.add_argument('--replay', action='store_true')
    ap.add_argument('--attempts', type=int, default=2)
    ap.add_argument('--monkey', action='store_true')
    ap.add_argument('--seed', type=int, default=4242)
    ap.add_argument('--events', type=int, default=500)
    ap.add_argument('--throttle', type=int, default=300)
    ap.add_argument('--offline', action='store_true', help='airplane mode while replaying (restored afterwards)')
    a = ap.parse_args()
    graph = json.loads((a.run / 'screen_graph.json').read_text(encoding='utf-8'))
    try:
        d = Device(a.serial, journal=a.run / 'device_changes.json', script='crash_explore')
    except PendingRestore as e:
        print(e, file=sys.stderr); return 2
    res = m = None
    with d.settings_guard() as changed:
        if a.offline:
            d.airplane_mode(True)
        if a.replay:
            res = replay(d, a.run, graph, a.attempts)
        if a.monkey:
            m = monkey(d, a.run, graph, a.seed, a.events, a.throttle)
    # Written after the guard has restored the settings, so the records say what was put back.
    if res is not None:
        (a.run / 'replays.json').write_text(json.dumps({'replays': res, 'settings_changed': changed}, indent=1) + '\n',
                                            encoding='utf-8', newline='\n')
    if m is not None:
        (a.run / 'monkey' / 'summary.json').write_text(json.dumps(dict(m, settings_changed=changed), indent=1) + '\n', encoding='utf-8', newline='\n')
        print(m)
    return 0


if __name__ == '__main__':
    sys.exit(main())
