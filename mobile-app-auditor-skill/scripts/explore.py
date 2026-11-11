#!/usr/bin/env python3
"""Guided exploration of an Android app: a screen graph, sampled screens, and every crash and ANR on the way.

    python explore.py --package au.example.app --activity .MainActivity --out run [--apk app.apk]
        [--max-screens 25] [--max-actions 60] [--budget-min 20] [--no-rotate]

Breadth-first from launch. Each distinct screen (top activity plus the set of control ids, or classes and
depths when there are no ids; text is left out so rotating banners don't make new screens) is a node; each
tap on a control, and one rotation per screen, is an edge. A screen is reached again by replaying its steps
from a fresh launch. Controls whose label matches buy, pay, delete, remove account, send, purchase,
checkout, sign out or log out are never tapped.

After every action the runner presses SHIFT twice (harmless input, so a blocked main thread shows up as an
ANR) and waits; it then reads the crash buffer and the ANR lines for the app. A crash or ANR is recorded
with its steps, a screenshot, and log lines from the app's own process only. On an ANR it presses Wait and
records whether the app recovers (B2) or not (B1); on a crash it relaunches and records whether the app
starts again (a crash loop if not).

Writes <out>/screen_graph.json, <out>/crashes.json, <out>/events.json, and per screen <out>/screens/<id>.json
(the hierarchy) and <out>/screens/<id>.png.
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from adb_device import Device, label_of  # noqa: E402

DESTRUCTIVE = re.compile(r'\b(buy|pay|delete|remove account|send|purchase|checkout|sign out|log ?out)\b', re.I)


def now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class Explorer:
    def __init__(self, d: Device, package: str, activity: str, out: Path, a):
        self.d, self.pkg, self.act, self.out, self.a = d, package, activity, out, a
        self.screens: dict = {}
        self.edges: list = []
        self.crashes: list = []
        self.events: list = []
        self.actions = 0
        self.t0 = time.time()
        (out / 'screens').mkdir(parents=True, exist_ok=True)
        (out / 'crashes').mkdir(parents=True, exist_ok=True)

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
            out.append({'rid': n['resource_id'], 'label': label, 'class': n['class'], 'bounds': n['bounds'],
                        'skipped': bool(DESTRUCTIVE.search(label))})
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
        self.d.launch(self.pkg, self.act, wait=3)

    def replay(self, steps) -> bool:
        self.fresh()
        for s in steps:
            if not self.do(s):
                return False
            time.sleep(2.5)
        return True

    def health(self, steps, screen_id) -> dict | None:
        """Press SHIFT (harmless input), wait, and look for a crash or an ANR of the app."""
        for wait in (1.0, 1.0):
            time.sleep(wait)
            self.d.key('KEYCODE_SHIFT_LEFT')
        deadline = time.time() + self.a.settle
        while time.time() < deadline:
            crash = [l for l in self.d.logcat('crash') if 'AndroidRuntime' in l or 'FATAL' in l]
            if crash and any(self.pkg in l for l in crash):
                return self.record('crash', steps, screen_id, crash)
            anr = [l for l in self.d.logcat('system') + self.d.logcat('main') if 'ANR in' in l and self.pkg in l]
            if anr:
                time.sleep(2)
                return self.record('anr', steps, screen_id, anr)
            time.sleep(1)
        return None

    def record(self, kind, steps, screen_id, lines) -> dict:
        cid = f'C{len(self.crashes) + 1:02d}'
        shot = self.out / 'crashes' / f'{cid}.png'
        pid_lines = []
        if kind == 'crash':
            pid = next((m.group(1) for l in lines for m in [re.search(r'^\S+ \S+\s+(\d+)\s+\d+ E AndroidRuntime: Process: ' + re.escape(self.pkg), l)] if m), None)
            pid = pid or next((m.group(1) for l in lines for m in [re.search(r'^\S+ \S+\s+(\d+)\s+\d+ E AndroidRuntime: FATAL', l)] if m), None)
            pid_lines = [l for l in lines if pid and re.match(rf'^\S+ \S+\s+{pid}\s', l)]
            exc = next((l.split('AndroidRuntime: ', 1)[1] for l in pid_lines if 'Exception' in l or 'Error' in l), None)
            frame = next((l.split('AndroidRuntime: ', 1)[1].strip() for l in pid_lines if f'at {self.pkg}' in l), None)
            time.sleep(1)
            self.d.adb('exec-out', 'screencap -p', binary=True)
            shot.write_bytes(self.d.screenshot())
            rec = {'id': cid, 'kind': 'crash', 'screen': screen_id, 'steps': steps, 'exception': exc, 'top_app_frame': frame,
                   'log_excerpt': [re.sub(r'^\S+ \S+\s+', '', l) for l in pid_lines][:40], 'log_source': 'logcat -b crash (app process only)'}
            self.d.logcat_clear()
            self.fresh()
            try:
                ok = any(n['package'] == self.pkg for n in self.d.hierarchy())
            except RuntimeError:
                ok = False
            rec['relaunch'] = 'ok' if ok else 'crash_loop'
        else:
            shot.write_bytes(self.d.screenshot())
            trace = self.d.anr_traces()
            reason = next((l.split('Reason: ', 1)[1].strip() for l in lines if 'Reason: ' in l), None)
            if not reason:
                m = re.search(r'Subject: (.+)', trace)
                reason = m.group(1).strip() if m else None
            # keep the kind of timeout, not the window details, so occurrences of one cause merge
            reason = re.sub(r'\s*\(.*\)\s*$', '', reason or '') or None
            main = re.search(r'"main".*?\n\n', trace, re.S)
            frames = [l.strip() for l in (main.group(0) if main else '').splitlines() if l.strip().startswith('at ')][:15]
            rec = {'id': cid, 'kind': 'anr', 'screen': screen_id, 'steps': steps, 'reason': reason,
                   'top_app_frame': next((f for f in frames if self.pkg in f), None), 'main_thread': frames,
                   'log_excerpt': [re.sub(r'^\S+ \S+\s+', '', l) for l in lines if self.pkg in l][:10],
                   'log_source': 'logcat ANR lines and dumpsys dropbox data_app_anr (main thread, app frames)'}
            nodes = self.d.hierarchy()
            wait = next((n for n in nodes if n['resource_id'] == 'android:id/aerr_wait'), None)
            if wait:
                b = wait['bounds']
                self.d.tap((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
            time.sleep(self.a.settle)
            try:
                ok = any(n['package'] == self.pkg for n in self.d.hierarchy()) and f'ANR in {self.pkg}' not in ''.join(self.d.logcat('system')[-5:])
            except RuntimeError:
                ok = False
            rec['after_wait'] = 'recovered' if ok else 'killed'
            self.d.logcat_clear()
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
        if sig in self.screens:
            return self.screens[sig]['id']
        sid = f'S{len(self.screens) + 1:02d}'
        (self.out / 'screens' / f'{sid}.json').write_text(json.dumps(nodes, indent=0) + '\n', encoding='utf-8', newline='\n')
        (self.out / 'screens' / f'{sid}.png').write_bytes(self.d.screenshot())
        self.screens[sig] = {'id': sid, 'signature': sig, 'activity': self.d.top_activity(), 'steps': steps, 'via': via,
                             'controls': self.controls(nodes), 'title': next((n['text'] for n in self.app_nodes(nodes) if n['text']), '')}
        self.log(event='screen', screen=sid, steps=steps)
        return sid

    def run(self):
        self.d.logcat_clear()
        self.fresh()
        root = self.visit([], 'launch')
        queue = [root]
        seen_rot = set()
        while queue and len(self.screens) < self.a.max_screens:
            sid = queue.pop(0)
            scr = next(s for s in self.screens.values() if s['id'] == sid)
            moves = [{'rid': c['rid'], 'label': c['label']} for c in scr['controls'] if not c['skipped']]
            if not self.a.no_rotate and sid not in seen_rot:
                moves.append({'rotate': 'landscape'})
                seen_rot.add(sid)
            for mv in moves:
                if self.actions >= self.a.max_actions or time.time() - self.t0 > self.a.budget_min * 60:
                    self.log(event='budget_reached')
                    return
                if not self.replay(scr['steps']):
                    self.log(event='replay_failed', screen=sid)
                    continue
                self.actions += 1
                ok = self.do(mv)
                self.log(event='action', screen=sid, action=mv, ok=ok)
                if not ok:
                    continue
                steps = scr['steps'] + [mv]
                bad = self.health(steps, sid)
                if bad and bad['kind'] == 'crash':
                    continue
                to = self.visit(steps, 'rotate' if 'rotate' in mv else 'tap')
                self.edges.append({'from': sid, 'action': mv, 'to': to, 'crash': bad['id'] if bad else None})
                if to and to not in queue and to != sid and not any(e['to'] == to for e in self.edges[:-1]):
                    queue.append(to)
            self.d.rotate(False)


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
    ap.add_argument('--settle', type=float, default=7.0, help='seconds to watch for a crash or ANR after each action')
    ap.add_argument('--no-rotate', action='store_true')
    a = ap.parse_args()
    d = Device(a.serial)
    if a.apk:
        d.adb('install', '-r', a.apk, timeout=180)
    a.out.mkdir(parents=True, exist_ok=True)
    ex = Explorer(d, a.package, a.activity, a.out, a)
    with d.settings_guard() as changed:
        ex.run()
    graph = {'package': a.package, 'activity': a.activity, 'device': d.info(), 'started': ex.events[0]['at'] if ex.events else now(),
             'finished': now(), 'actions': ex.actions, 'limits': {'max_screens': a.max_screens, 'max_actions': a.max_actions, 'budget_min': a.budget_min},
             'excluded_pattern': DESTRUCTIVE.pattern, 'screens': list(ex.screens.values()), 'edges': ex.edges, 'settings_changed': changed}
    (a.out / 'screen_graph.json').write_text(json.dumps(graph, indent=1) + '\n', encoding='utf-8', newline='\n')
    (a.out / 'crashes.json').write_text(json.dumps(ex.crashes, indent=1) + '\n', encoding='utf-8', newline='\n')
    (a.out / 'events.json').write_text(json.dumps(ex.events, indent=1) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(ex.screens)} screen(s), {len(ex.edges)} edge(s), {ex.actions} action(s), {len(ex.crashes)} crash(es)/ANR(s) -> {a.out}')
    for s in ex.screens.values():
        print(f'  {s["id"]} {s["activity"]} via {s["via"]} steps={len(s["steps"])} controls={len(s["controls"])}')
    for c in ex.crashes:
        print(f'  {c["id"]} {c["kind"]} on {c["screen"]} after {c["steps"][-1]}: {c.get("exception") or c.get("reason")}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
