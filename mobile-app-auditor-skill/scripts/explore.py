#!/usr/bin/env python3
"""Guided exploration of an Android app: a screen graph, sampled screens, and every crash and ANR on the way.

    python explore.py --package au.example.app --activity .MainActivity --out run [--apk app.apk]
        [--max-screens 25] [--max-actions 60] [--budget-min 20] [--no-rotate] [--offline]
        [--input-delay 2] [--settle 6] [--launch-wait 2] [--step-wait 1.5] [--no-back]

Breadth-first from launch. Each distinct screen (top activity plus the set of control ids, or classes and
depths when there are no ids; text is left out so rotating banners don't make new screens) is a node; each
tap on a control, and one rotation per screen, is an edge. A screen is reached again with Back when that
lands on it (checked by its signature), else by replaying its steps from a fresh launch (--no-back always
replays). Controls whose visible name (their text, else their descendants' text, else the first sentence of
their description) matches buy, pay, delete, remove account, send, purchase, checkout, sign out or log out
are never tapped; each skipped control is listed in the graph with the word that matched.

After every action the runner waits --input-delay seconds, presses SHIFT once (harmless input, so a blocked
main thread shows up as an ANR) and watches for --settle seconds, reading only the log lines written since
the action. A crash or ANR is recorded with its steps, a screenshot, and log lines from the app's own process
only. An ANR takes its reason from the system's ANR line and its main thread from the newest dropbox entry of
this package that appeared after the run started (never another app's, never an older one); the record says
which input timed out, whether it was the runner's own SHIFT press, and the device's CPU pressure then. On an
ANR it presses Wait if the dialog is shown and records whether the same process is still alive and showing
the app (recovered, B2) or not (killed, B1); on a crash it relaunches and records whether the app starts
again (a crash loop if not).

Every device change (rotation, airplane mode for --offline) is journaled in <out>/device_changes.json before
it is made; `python adb_device.py restore --journal <out>/device_changes.json` puts back what a killed run
left. Writes <out>/screen_graph.json, <out>/crashes.json, <out>/events.json, and per screen
<out>/screens/<id>.json (the hierarchy) and <out>/screens/<id>.png.
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from adb_device import (Device, PendingRestore, PROBE_KEY, anr_reason_from_log, input_event, label_of,  # noqa: E402
                        newest_anr, parse_dropbox, short_reason, visible_name)

DESTRUCTIVE = re.compile(r'\b(buy|pay|delete|remove account|send|purchase|checkout|sign out|log ?out)\b', re.I)
SYSTEM_WAIT = 'android:id/aerr_wait'


def now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class LaunchError(RuntimeError):
    """The app shows no node of its own at launch."""


class Explorer:
    def __init__(self, d: Device, package: str, activity: str, out: Path, a):
        self.d, self.pkg, self.act, self.out, self.a = d, package, activity, out, a
        self.screens: dict = {}
        self.edges: list = []
        self.crashes: list = []
        self.events: list = []
        self.actions = 0
        self.t0 = time.time()
        self.here: str | None = None  # signature of the screen the device shows now, when known
        (out / 'screens').mkdir(parents=True, exist_ok=True)
        (out / 'crashes').mkdir(parents=True, exist_ok=True)
        # ANR traces already in the dropbox (other apps, earlier runs) are never this run's
        self.anr_seen = {e['key'] for e in parse_dropbox(d.anr_traces())}

    def log(self, **kw):
        kw['at'] = now()
        self.events.append(kw)

    # ---------------------------------------------------------------- screens
    def app_nodes(self, nodes):
        return [n for n in nodes if n['package'] == self.pkg]

    def signature(self, nodes) -> str:
        top = self.d.top_activity() or '?'
        keys = sorted({(n['resource_id'] or f'{n["class"]}@{n["depth"]}') for n in self.app_nodes(nodes)
                       if n['clickable'] or n['focusable'] or n['resource_id']})
        return hashlib.sha1((top + '|' + ','.join(keys)).encode()).hexdigest()[:10]

    def controls(self, nodes) -> list[dict]:
        out = []
        for n in self.app_nodes(nodes):
            if not (n['clickable'] and n['enabled']):
                continue
            label = label_of(n, nodes)
            name = visible_name(n, nodes)
            m = DESTRUCTIVE.search(name)
            out.append({'rid': n['resource_id'], 'label': label, 'name': name, 'class': n['class'], 'bounds': n['bounds'],
                        'skipped': bool(m), **({'skipped_for': m.group(0).lower()} if m else {})})
        return out

    # ---------------------------------------------------------------- actions
    def resolve(self, step, nodes):
        for n in self.app_nodes(nodes):
            if not n['clickable']:
                continue
            if step.get('rid') and n['resource_id'] == step['rid']:
                return n
            if not step.get('rid') and step.get('label') and label_of(n, nodes) == step['label']:
                return n
        return None

    def do(self, step, nodes=None) -> bool:
        self.here = None
        if 'rotate' in step:
            self.d.rotate(step['rotate'] == 'landscape')
            return True
        if 'key' in step:
            self.d.key(step['key'])
            return True
        nodes = nodes if nodes is not None else self.d.hierarchy()
        n = self.resolve(step, nodes)
        if not n:
            return False
        b = n['bounds']
        self.d.tap((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
        return True

    def fresh(self):
        self.d.stop(self.pkg)
        self.d.rotate(False)
        self.d.launch(self.pkg, self.act, wait=self.a.launch_wait)
        self.here = None

    def replay(self, steps) -> bool:
        self.fresh()
        for s in steps:
            if not self.do(s):
                return False
            time.sleep(self.a.step_wait)
        return True

    def health(self, steps, screen_id, since: str | None = None) -> dict | None:
        """Wait, press SHIFT once (harmless input), and watch the log lines written since the action for a crash or an ANR.
        `since` is the device time taken just before the action."""
        since = since or self.d.device_time()
        if not since:  # no device clock: empty the log instead, so an old ANR line is never read as new
            self.d.logcat_clear()
        time.sleep(self.a.input_delay)
        self.d.key(f'KEYCODE_{PROBE_KEY}')
        deadline = time.time() + self.a.settle
        while True:
            lines = self.d.logcat_since(since)
            crash = [l for l in lines if ' E AndroidRuntime' in l or 'FATAL' in l]
            if crash and any(self.pkg in l for l in crash):
                return self.record('crash', steps, screen_id, crash)
            anr = [l for l in lines if 'ANR in' in l and self.pkg in l]
            if anr:
                return self.record('anr', steps, screen_id, anr)
            if time.time() >= deadline:
                return None
            time.sleep(1)

    def anr_trace(self, deadline: float) -> dict | None:
        """The dropbox entry for this ANR; the system writes it some seconds after the ANR line."""
        while True:
            e = newest_anr(parse_dropbox(self.d.anr_traces()), self.pkg, self.anr_seen)
            if e or time.time() >= deadline:
                if e:
                    self.anr_seen.add(e['key'])
                return e
            time.sleep(3)

    def record(self, kind, steps, screen_id, lines) -> dict:
        cid = f'C{len(self.crashes) + 1:02d}'
        shot = self.out / 'crashes' / f'{cid}.png'
        if kind == 'crash':
            pid = next((m.group(1) for l in lines for m in [re.search(r'^\S+ \S+\s+(\d+)\s+\d+ E AndroidRuntime: Process: ' + re.escape(self.pkg), l)] if m), None)
            pid = pid or next((m.group(1) for l in lines for m in [re.search(r'^\S+ \S+\s+(\d+)\s+\d+ E AndroidRuntime: FATAL', l)] if m), None)
            pid_lines = [l for l in lines if pid and re.match(rf'^\S+ \S+\s+{pid}\s', l)]
            exc = next((l.split('AndroidRuntime: ', 1)[1] for l in pid_lines if 'Exception' in l or 'Error' in l), None)
            frame = next((l.split('AndroidRuntime: ', 1)[1].strip() for l in pid_lines if f'at {self.pkg}' in l), None)
            time.sleep(1)
            shot.write_bytes(self.d.screenshot())
            rec = {'id': cid, 'kind': 'crash', 'screen': screen_id, 'steps': steps, 'exception': exc, 'top_app_frame': frame,
                   'log_excerpt': [re.sub(r'^\S+ \S+\s+', '', l) for l in pid_lines][:40], 'log_source': 'logcat -b crash (app process only)'}
            self.fresh()
            try:
                ok = any(n['package'] == self.pkg for n in self.d.hierarchy())
            except RuntimeError:
                ok = False
            rec['relaunch'] = 'ok' if ok else 'crash_loop'
        else:
            pid_before = self.d.pid(self.pkg)
            t_anr = time.time()
            shot.write_bytes(self.d.screenshot())
            full = anr_reason_from_log(lines)
            nodes = self.d.hierarchy()
            wait = next((n for n in nodes if n['resource_id'] == SYSTEM_WAIT), None)
            if wait:
                b = wait['bounds']
                self.d.tap((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
            time.sleep(self.a.settle)
            # Alive means the same process still runs and shows the app: no reliance on log lines, which still hold the ANR.
            try:
                pid_after = self.d.pid(self.pkg)
                shows = any(n['package'] == self.pkg for n in self.d.hierarchy())
            except RuntimeError:
                pid_after, shows = None, False
            ok = pid_before is not None and pid_after == pid_before and shows
            trace = self.anr_trace(t_anr + self.a.anr_trace_wait)
            if trace and trace.get('pid') and pid_before and trace['pid'] != pid_before:
                trace = None  # an entry of another process of the same package: not this ANR
            full = full or (trace or {}).get('subject')
            frames = (trace or {}).get('main_thread', [])[:15]
            rec = {'id': cid, 'kind': 'anr', 'screen': screen_id, 'steps': steps, 'reason': short_reason(full), 'reason_full': full,
                   'input_event': input_event(full), 'top_app_frame': next((f for f in frames if self.pkg in f), None), 'main_thread': frames,
                   'trace': {'source': 'dumpsys dropbox data_app_anr', 'logged': trace['logged'], 'pid': trace['pid'],
                             'device_cpu_pressure_avg10': trace['cpu_pressure_avg10']} if trace else None,
                   'log_excerpt': [re.sub(r'^\S+ \S+\s+', '', l) for l in lines if self.pkg in l][:10],
                   'log_source': 'logcat ANR lines since the action and the newest dumpsys dropbox data_app_anr entry of this package (main thread)',
                   'dialog': 'shown' if wait else 'not_shown', 'pid_before': pid_before, 'pid_after': pid_after,
                   'after_wait': 'recovered' if ok else 'killed'}
            if not trace:
                rec['trace_note'] = 'no new dropbox entry for this package within the wait: main thread unknown'
            self.here = None
        rec['screenshot'] = f'crashes/{cid}.png'
        rec['at'] = now()
        self.crashes.append(rec)
        self.log(event=kind, crash=cid, steps=steps)
        return rec

    def visit(self, steps, via) -> str | None:
        nodes = self.d.hierarchy()
        if not self.app_nodes(nodes):
            return None
        sig = self.signature(nodes)
        self.here = sig
        if sig in self.screens:
            return self.screens[sig]['id']
        sid = f'S{len(self.screens) + 1:02d}'
        (self.out / 'screens' / f'{sid}.json').write_text(json.dumps(nodes, indent=0) + '\n', encoding='utf-8', newline='\n')
        (self.out / 'screens' / f'{sid}.png').write_bytes(self.d.screenshot())
        self.screens[sig] = {'id': sid, 'signature': sig, 'activity': self.d.top_activity(), 'steps': steps, 'via': via,
                             'controls': self.controls(nodes), 'title': next((n['text'] for n in self.app_nodes(nodes) if n['text']), '')}
        self.log(event='screen', screen=sid, steps=steps)
        return sid

    def launch_screen(self, tries: int = 3) -> str:
        """The first screen. A system dialog over a fresh boot ("System UI isn't responding") is waited out; an app that never
        shows a node of its own is a clear error, not an empty exploration."""
        seen = set()
        for i in range(tries):
            self.fresh()
            sid = self.visit([], 'launch')
            if sid:
                return sid
            nodes = self.d.hierarchy()
            seen |= {n['package'] for n in nodes if n['package']}
            wait = next((n for n in nodes if n['resource_id'] == SYSTEM_WAIT), None)
            if wait:
                b = wait['bounds']
                self.d.tap((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
                self.log(event='system_dialog_waited', attempt=i + 1)
            time.sleep(5)
        raise LaunchError(f'{self.pkg} showed no node of its own after {tries} launches (the screen showed {sorted(seen) or "nothing"}): '
                          'check the app starts, the activity name, and that no system dialog is in the way')

    def back_to(self, scr) -> bool:
        """Press Back and check the signature: True when that landed on the screen, so no relaunch is needed."""
        if self.a.no_back:
            return False
        self.d.back()
        time.sleep(self.a.step_wait)
        try:
            nodes = self.d.hierarchy()
        except RuntimeError:
            return False
        if self.app_nodes(nodes) and self.signature(nodes) == scr['signature']:
            self.here = scr['signature']
            return True
        self.here = None
        return False

    def run(self):
        root = self.launch_screen()
        by_id = {s['id']: s for s in self.screens.values()}
        queue = [root]
        seen_rot = set()
        while queue and len(self.screens) < self.a.max_screens:
            scr = by_id[queue.pop(0)]
            sid = scr['id']
            moves = [{'rid': c['rid'], 'label': c['label']} for c in scr['controls'] if not c['skipped']]
            if not self.a.no_rotate and sid not in seen_rot:
                moves.append({'rotate': 'landscape'})
                seen_rot.add(sid)
            for mv in moves:
                if self.actions >= self.a.max_actions or time.time() - self.t0 > self.a.budget_min * 60:
                    self.log(event='budget_reached')
                    return
                if self.here != scr['signature'] and not self.replay(scr['steps']):
                    self.log(event='replay_failed', screen=sid)
                    continue
                self.actions += 1
                since = self.d.device_time()
                ok = self.do(mv)
                self.log(event='action', screen=sid, action=mv, ok=ok)
                if not ok:
                    continue
                steps = scr['steps'] + [mv]
                bad = self.health(steps, sid, since)
                if bad and bad['kind'] == 'crash':
                    continue
                to = self.visit(steps, 'rotate' if 'rotate' in mv else 'tap')
                self.edges.append({'from': sid, 'action': mv, 'to': to, 'crash': bad['id'] if bad else None})
                if to and to not in queue and to != sid and not any(e['to'] == to for e in self.edges[:-1]):
                    queue.append(to)
                    by_id[to] = next(s for s in self.screens.values() if s['id'] == to)
                if 'rotate' in mv:
                    self.d.rotate(False)
                    self.here = None
                elif to != sid:
                    self.back_to(scr)
            self.d.rotate(False)
            self.here = None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--package', required=True)
    ap.add_argument('--activity', required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--apk')
    ap.add_argument('--serial')
    ap.add_argument('--max-screens', type=int, default=25)
    ap.add_argument('--max-actions', type=int, default=60)
    ap.add_argument('--budget-min', type=float, default=20)
    ap.add_argument('--input-delay', type=float, default=2.0,
                    help='seconds after an action before the SHIFT press, so a screen still building (a WebView) is not hit by the probe input')
    ap.add_argument('--settle', type=float, default=6.0, help='seconds to watch for a crash or ANR after the SHIFT press (an ANR fires after 5 s)')
    ap.add_argument('--anr-trace-wait', type=float, default=30.0, help='seconds to wait for the dropbox ANR entry')
    ap.add_argument('--launch-wait', type=float, default=2.0)
    ap.add_argument('--step-wait', type=float, default=1.5)
    ap.add_argument('--no-back', action='store_true', help='always reach a screen by a fresh launch and replay, never by Back')
    ap.add_argument('--no-rotate', action='store_true')
    ap.add_argument('--offline', action='store_true', help='airplane mode for the whole exploration (restored afterwards)')
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    try:
        d = Device(a.serial, journal=a.out / 'device_changes.json', script='explore')
    except PendingRestore as e:
        print(e, file=sys.stderr); return 2
    if a.apk:
        d.install(a.apk)
    ex = Explorer(d, a.package, a.activity, a.out, a)
    status, reason = 'complete', None
    try:
        with d.settings_guard() as changed:
            if a.offline:
                d.airplane_mode(True)
            ex.run()
    except LaunchError as e:
        status, reason = 'not_run', str(e)
        print(e, file=sys.stderr)
    except (Exception, KeyboardInterrupt) as e:  # still write what was found
        status, reason = 'partial', f'stopped by {type(e).__name__}: {str(e)[:200]}'
        print(reason, file=sys.stderr)
    if status == 'complete' and any(e['event'] == 'budget_reached' for e in ex.events):
        reason = 'budget reached: unexplored controls remain'
    skipped = [{'screen': s['id'], 'name': c['name'], 'matched': c['skipped_for']} for s in ex.screens.values() for c in s['controls'] if c['skipped']]
    graph = {'package': a.package, 'activity': a.activity, 'device': d.info(), 'started': ex.events[0]['at'] if ex.events else now(),
             'finished': now(), 'run_status': status, 'run_status_reason': reason, 'actions': ex.actions,
             'limits': {'max_screens': a.max_screens, 'max_actions': a.max_actions, 'budget_min': a.budget_min},
             'timing': {'input_delay': a.input_delay, 'settle': a.settle, 'launch_wait': a.launch_wait, 'step_wait': a.step_wait, 'back_navigation': not a.no_back},
             'network': 'offline (airplane mode)' if a.offline else 'device default',
             'excluded_pattern': DESTRUCTIVE.pattern, 'skipped_controls': skipped,
             'screens': list(ex.screens.values()), 'edges': ex.edges, 'settings_changed': d.changed}
    (a.out / 'screen_graph.json').write_text(json.dumps(graph, indent=1) + '\n', encoding='utf-8', newline='\n')
    (a.out / 'crashes.json').write_text(json.dumps(ex.crashes, indent=1) + '\n', encoding='utf-8', newline='\n')
    (a.out / 'events.json').write_text(json.dumps(ex.events, indent=1) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(ex.screens)} screen(s), {len(ex.edges)} edge(s), {ex.actions} action(s), {len(ex.crashes)} crash(es)/ANR(s) -> {a.out} [{status}]')
    for s in ex.screens.values():
        print(f'  {s["id"]} {s["activity"]} via {s["via"]} steps={len(s["steps"])} controls={len(s["controls"])}')
    for c in ex.crashes:
        print(f'  {c["id"]} {c["kind"]} on {c["screen"]} after {c["steps"][-1]}: {c.get("exception") or c.get("reason")}')
    for s in skipped:
        print(f'  skipped on {s["screen"]}: "{s["name"]}" ({s["matched"]})')
    if any(not c.get('restored') for c in d.changed):
        print(f'device settings NOT restored: python {HERE / "adb_device.py"} restore --journal {a.out / "device_changes.json"}', file=sys.stderr)
        return 1
    return 0 if status == 'complete' else 2


if __name__ == '__main__':
    sys.exit(main())
