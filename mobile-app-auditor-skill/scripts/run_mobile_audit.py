#!/usr/bin/env python3
"""Run the checks and probes on each sampled screen of an explored Android app.

    python run_mobile_audit.py --run run [--screens S01,S02] [--skip-probes keyboard,dark_mode]

Reads <run>/screen_graph.json and <run>/crashes.json (from explore.py). Each sampled screen is reached by
replaying its steps from a fresh launch, then:

  checks (engine "mobile-checks", on the uiautomator hierarchy and a screenshot)
    label.control-no-name, label.icon-button-no-name   clickable elements with no accessible name
    target.below-24dp (failure, unless 24 dp circles around it touch no other target), target.below-48dp (advisory)
    contrast.text   text colour against its background from screenshot pixels inside the text bounds
  probes (each changes one thing, then puts it back)
    text_scaling    font scale 2.0: text cut off by a container that kept its height, or text that keeps its height while the rest grows
    display_size    the largest display size (density x 1.3): elements off screen
    orientation     rotate to landscape: does the screen stay portrait?
    keyboard        Tab through the screen: focus order, controls never reached, traps (Tab, Escape and Back don't move or leave)
    motion          screenshots 0 s, 5 s and 7.5 s: regions that change by themselves with no pause control
    dark_mode       the system dark theme: contrast again

A probe on a screen where exploration recorded a crash for that same action (a rotation crash) is
"blocked", with the crash id. Every changed setting is restored and listed. Results go to <run>/run.json,
with a marked screenshot per result in <run>/evidence/.
"""
from __future__ import annotations
import argparse, hashlib, json, math, re, statistics, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from adb_device import Device, label_of  # noqa: E402
import png_io  # noqa: E402

PAUSE = re.compile(r'\b(pause|stop|hide|play)\b', re.I)


def now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def lum(c) -> float:
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def ratio(a, b) -> float:
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return round((la + 0.05) / (lb + 0.05), 2)


def text_contrast(img: png_io.Image, b) -> dict | None:
    """Background = the commonest colour in the box; foreground = the colour (>= 1% of pixels) furthest from it."""
    x1, y1, x2, y2 = max(0, b[0]), max(0, b[1]), min(img.width, b[2]), min(img.height, b[3])
    if x2 - x1 < 4 or y2 - y1 < 4:
        return None
    counts: dict = {}
    for y in range(y1, y2):
        row = img.rows[y]
        for x in range(x1, x2):
            k = row[3 * x:3 * x + 3]
            counts[k] = counts.get(k, 0) + 1
    total = sum(counts.values())
    bg = max(counts, key=counts.get)
    cands = [k for k, n in counts.items() if n >= total * 0.01 and k != bg]
    if not cands:
        return None
    fg = max(cands, key=lambda k: ratio(tuple(k), tuple(bg)))
    return {'fg': '#%02x%02x%02x' % tuple(fg), 'bg': '#%02x%02x%02x' % tuple(bg), 'ratio': ratio(tuple(fg), tuple(bg))}


def mark(img: png_io.Image, b, path: Path) -> str:
    """Write the screenshot with a red box around the element; returns the file's SHA-256."""
    rows = [bytearray(r) for r in img.rows]
    for t in range(4):
        for x in range(max(0, b[0] - t), min(img.width, b[2] + t)):
            for y in (b[1] - t, b[3] + t - 1):
                if 0 <= y < img.height:
                    rows[y][3 * x:3 * x + 3] = b'\xe0\x10\x10'
        for y in range(max(0, b[1] - t), min(img.height, b[3] + t)):
            for x in (b[0] - t, b[2] + t - 1):
                if 0 <= x < img.width:
                    rows[y][3 * x:3 * x + 3] = b'\xe0\x10\x10'
    return png_io.write(path, png_io.Image(img.width, img.height, [bytes(r) for r in rows]))


def parent(n: dict, nodes: list[dict]) -> dict | None:
    """The nearest ancestor of a node in the same hierarchy dump."""
    anc = [m for m in nodes if n['path'].startswith(m['path'] + '/')]
    return max(anc, key=lambda m: m['depth']) if anc else None


class Audit:
    def __init__(self, d: Device, run: Path, graph: dict, crashes: list):
        self.d, self.run, self.g, self.crashes = d, run, graph, crashes
        self.pkg, self.act = graph['package'], graph['activity']
        self.checks: list = []
        self.results: list = []
        (run / 'evidence').mkdir(exist_ok=True)

    # -------------------------------------------------------------- navigation
    def reach(self, scr) -> list[dict]:
        self.d.stop(self.pkg)
        self.d.rotate(False)
        self.d.launch(self.pkg, self.act, wait=3)
        for st in scr['steps']:
            nodes = self.d.hierarchy()
            if 'rotate' in st:
                self.d.rotate(st['rotate'] == 'landscape')
            else:
                n = next((n for n in nodes if n['package'] == self.pkg and n['clickable'] and
                          ((st.get('rid') and n['resource_id'] == st['rid']) or (not st.get('rid') and label_of(n, nodes) == st.get('label')))), None)
                if not n:
                    raise RuntimeError(f'step {st} not found')
                b = n['bounds']
                self.d.tap((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
            time.sleep(2.5)
        time.sleep(1)
        return [n for n in self.d.hierarchy() if n['package'] == self.pkg]

    def shot(self) -> png_io.Image:
        return png_io.decode(self.d.screenshot())

    # -------------------------------------------------------------- recording
    def check(self, screen, kind, name, status, reason=None):
        self.checks.append({'screen': screen, 'kind': kind, 'name': name, 'status': status, **({'reason': reason} if reason else {})})

    def result(self, code, screen, status, n, img, nodes, measured=None):
        rid = n.get('resource_id')
        key = hashlib.sha1(f'{code}{screen}{n["path"]}'.encode()).hexdigest()[:8]
        path = self.run / 'evidence' / f'{screen}-{code.replace(".", "_")}-{key}.png'
        sha = mark(img, n['bounds'], path) if img else None
        b = n['bounds']
        self.results.append({'check': code, 'screen': screen, 'status': status,
                             'element': {'resource_id': rid, 'class': n['class'], 'label': label_of(n, nodes), 'bounds': b,
                                         'size_dp': [self.d.dp(b[2] - b[0]), self.d.dp(b[3] - b[1])]},
                             'measured': measured or {}, 'screenshot': path.relative_to(self.run).as_posix() if img else None, 'sha256': sha})

    # -------------------------------------------------------------- checks
    def static_checks(self, sid, nodes, img):
        for n in nodes:
            if n['clickable'] and not label_of(n, nodes):
                kids_text = any(m['text'] for m in nodes if m['path'].startswith(n['path'] + '/'))
                self.result('label.control-no-name', sid, 'failed', n, img, nodes)
                if not kids_text:
                    self.result('label.icon-button-no-name', sid, 'failed', n, img, nodes)
        self.check(sid, 'engine', 'mobile-checks:labels', 'ran')
        targets = [n for n in nodes if n['clickable'] and n['enabled']]
        r24 = 24 * self.d.density() / 160
        for n in targets:
            b = n['bounds']
            w, h = b[2] - b[0], b[3] - b[1]
            if w < r24 or h < r24:
                cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
                clash = []
                for m in targets:
                    if m is n:
                        continue
                    mb = m['bounds']
                    nx, ny = min(max(cx, mb[0]), mb[2]), min(max(cy, mb[1]), mb[3])
                    if math.hypot(cx - nx, cy - ny) < r24 / 2 + (0 if (mb[2] - mb[0] >= r24 and mb[3] - mb[1] >= r24) else r24 / 2):
                        clash.append(label_of(m, nodes) or m['resource_id'])
                self.result('target.below-24dp', sid, 'failed' if clash else 'passed_spacing', n, img, nodes,
                            {'size_dp': [self.d.dp(w), self.d.dp(h)], 'spacing_conflicts': clash})
            elif self.d.dp(w) < 48 or self.d.dp(h) < 48:
                self.result('target.below-48dp', sid, 'advisory', n, None, nodes, {'size_dp': [self.d.dp(w), self.d.dp(h)]})
        self.check(sid, 'engine', 'mobile-checks:target_size', 'ran')
        self.contrast(sid, nodes, img, 'contrast.text')
        self.check(sid, 'engine', 'mobile-checks:contrast', 'ran')

    def contrast(self, sid, nodes, img, code):
        for n in nodes:
            if not n['text'].strip():
                continue
            c = text_contrast(img, n['bounds'])
            if not c:
                continue
            large = self.d.dp(n['bounds'][3] - n['bounds'][1]) >= 32
            need = 3.0 if large else 4.5
            if c['ratio'] < need:
                self.result(code, sid, 'failed', n, img, nodes, dict(c, required=need, large_text=large))

    # -------------------------------------------------------------- probes
    def text_scaling(self, scr, base):
        sid = scr['id']
        self.d.put_setting('system', 'font_scale', '2.0')
        try:
            big = self.reach(scr)
            img = self.shot()
        finally:
            self.d.put_setting('system', 'font_scale', '1.0')
            self.d.restore()
        key = lambda n: n['resource_id'] or n['text']  # noqa: E731
        before = {key(n): n for n in base if n['text'].strip()}
        pairs = [(before[key(n)], n) for n in big if n['text'].strip() and key(n) in before]
        ratios = [(b2[3] - b2[1]) / max(1, b1[3] - b1[1]) for b1, b2 in ((p[0]['bounds'], p[1]['bounds']) for p in pairs)]
        typical = statistics.median(ratios) if ratios else 1
        H = img.height
        for (n1, n2), r in zip(pairs, ratios):
            if typical < 1.5:
                continue
            # Clipped: the text now reaches the bottom of its parent, and the parent (not a full-screen container) kept its height.
            p1, p2 = parent(n1, base), parent(n2, big)
            clipped = (p1 and p2 and p2['bounds'][3] - p2['bounds'][1] < H / 2
                       and (p2['bounds'][3] - p2['bounds'][1]) / max(1, p1['bounds'][3] - p1['bounds'][1]) < 1.05
                       and n2['bounds'][3] >= p2['bounds'][3] - 2 and r < typical)
            if r < 1.2 or clipped:
                self.result('text-scale.not-enlarged', sid, 'failed', n2, img, big,
                            {'font_scale': 2.0, 'height_ratio': round(r, 2), 'typical_ratio': round(typical, 2),
                             'clipped_by': (p2['resource_id'] or p2['class']) if clipped else None,
                             'container_height_px': [p1['bounds'][3] - p1['bounds'][1], p2['bounds'][3] - p2['bounds'][1]] if clipped else None})
        self.check(sid, 'probe', 'text_scaling', 'ran')

    def display_size(self, scr):
        sid = scr['id']
        dens = self.d.density()
        self.d.set_density(round(dens * 1.3))
        try:
            nodes = self.reach(scr)
            img = self.shot()
            W, H = img.width, img.height
            for n in nodes:
                b = n['bounds']
                if (n['clickable'] or n['text']) and (b[2] > W or b[3] > H or b[2] - b[0] <= 0 or b[3] - b[1] <= 0):
                    self.result('display-size.offscreen', sid, 'failed', n, img, nodes, {'density': round(dens * 1.3), 'screen_px': [W, H]})
        finally:
            self.d.set_density(None)
            self.d.restore()
        self.check(sid, 'probe', 'display_size', 'ran')

    def orientation(self, scr):
        sid = scr['id']
        crash = next((c for c in self.crashes if c['screen'] == sid and c['steps'] and 'rotate' in c['steps'][-1]), None)
        if crash:
            self.check(sid, 'probe', 'orientation', 'blocked', f'rotating this screen crashes the app ({crash["id"]})')
            return
        self.reach(scr)
        try:
            self.d.rotate(True)
            time.sleep(2)
            img = self.shot()
            nodes = [n for n in self.d.hierarchy() if n['package'] == self.pkg]
            if img.height > img.width and nodes:
                root = max(nodes, key=lambda n: (n['bounds'][2] - n['bounds'][0]) * (n['bounds'][3] - n['bounds'][1]))
                self.result('orientation.locked', sid, 'failed', root, img, nodes, {'requested': 'landscape', 'screen_px': [img.width, img.height]})
        finally:
            self.d.rotate(False)
            self.d.restore()
        self.check(sid, 'probe', 'orientation', 'ran')

    def keyboard(self, scr, base):
        sid = scr['id']
        self.reach(scr)
        focusable = [n for n in base if n['focusable'] or n['clickable']]
        seq = []
        for _ in range(len(focusable) + 3):
            self.d.key('KEYCODE_TAB')
            time.sleep(0.6)
            nodes = [n for n in self.d.hierarchy() if n['package'] == self.pkg]
            f = next((n for n in nodes if n['focused']), None)
            seq.append((f['resource_id'] or label_of(f, nodes)) if f else None)
        moved = len(set(x for x in seq if x)) > 1
        if not moved and len(focusable) > 1:
            before = scr['signature']
            self.d.key('KEYCODE_ESCAPE')
            time.sleep(1)
            self.d.back()
            time.sleep(1.5)
            nodes = [n for n in self.d.hierarchy() if n['package'] == self.pkg]
            still = {n['resource_id'] for n in nodes} >= {n['resource_id'] for n in base if n['resource_id']}
            if still:
                f = next((n for n in base if (n['resource_id'] or label_of(n, base)) == seq[-1]), focusable[0])
                img = self.shot()
                self.result('keyboard.trap', sid, 'failed', f, img, nodes, {'tab_sequence': seq, 'escape_and_back': 'stayed on the screen'})
        else:
            reached = set(x for x in seq if x)
            img = None
            for n in base:
                if n['clickable'] and n['enabled'] and (n['resource_id'] or label_of(n, base)) not in reached:
                    img = img or self.shot()
                    self.result('keyboard.unreachable', sid, 'failed', n, img, base, {'tab_sequence': seq})
        self.results.append({'check': 'keyboard.sequence', 'screen': sid, 'status': 'info', 'measured': {'tab_sequence': seq}})
        self.check(sid, 'probe', 'keyboard', 'ran')

    def motion(self, scr):
        sid = scr['id']
        nodes = self.reach(scr)
        a = self.shot()
        time.sleep(5)
        b = self.shot()
        time.sleep(2.5)
        c = self.shot()
        pause = any(PAUSE.search(label_of(n, nodes)) for n in nodes if n['clickable'])
        changed = []
        for n in nodes:
            if not n['resource_id'] or n['resource_id'].startswith('android:'):
                continue
            x1, y1, x2, y2 = n['bounds']
            diff = sum(1 for y in range(max(0, y1), min(b.height, y2), 3)
                       if b.rows[y][3 * x1:3 * x2] != c.rows[y][3 * x1:3 * x2])
            if diff:
                changed.append(n)
        # report the smallest changed elements (a container and its child would be one region)
        for n in changed:
            if not any(m is not n and m['path'].startswith(n['path'] + '/') for m in changed) and not pause:
                self.result('motion.auto-advance', sid, 'failed', n, c, nodes, {'changed_between_s': [5, 7.5], 'pause_control': pause})
        self.check(sid, 'probe', 'motion', 'ran')

    def dark_mode(self, scr):
        sid = scr['id']
        self.d.night_mode(True)
        try:
            nodes = self.reach(scr)
            img = self.shot()
            self.contrast(sid, nodes, img, 'contrast.text-dark')
        finally:
            self.d.night_mode(False)
            self.d.restore()
        self.check(sid, 'probe', 'dark_mode', 'ran')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--run', type=Path, required=True)
    ap.add_argument('--serial')
    ap.add_argument('--screens')
    ap.add_argument('--skip-probes', default='')
    ap.add_argument('--max-screens', type=int, default=25)
    ap.add_argument('--apk', type=Path, help='the build under test, to record its SHA-256')
    a = ap.parse_args()
    g = json.loads((a.run / 'screen_graph.json').read_text(encoding='utf-8'))
    crashes = json.loads((a.run / 'crashes.json').read_text(encoding='utf-8'))
    d = Device(a.serial)
    au = Audit(d, a.run, g, crashes)
    skip = set(filter(None, a.skip_probes.split(',')))
    wanted = set(a.screens.split(',')) if a.screens else None
    sample = []
    started = now()
    with d.settings_guard() as changed:
        for scr in g['screens'][:a.max_screens]:
            if wanted and scr['id'] not in wanted:
                continue
            sid = scr['id']
            try:
                nodes = au.reach(scr)
            except Exception as e:
                sample.append({'id': sid, 'activity': scr['activity'], 'reason': 'unreachable', 'note': str(e)[:200]})
                continue
            sample.append({'id': sid, 'activity': scr['activity'], 'title': scr.get('title'), 'reason': 'structured',
                           'steps': scr['steps'], 'note': 'every distinct screen found by guided exploration'})
            img = au.shot()
            png_io.write(a.run / 'evidence' / f'{sid}-base.png', img)
            au.static_checks(sid, nodes, img)
            for probe in ('text_scaling', 'display_size', 'orientation', 'keyboard', 'motion', 'dark_mode'):
                if probe in skip:
                    au.check(sid, 'probe', probe, 'not_run', 'skipped by --skip-probes')
                    continue
                try:
                    if probe in ('text_scaling', 'keyboard'):
                        getattr(au, probe)(scr, nodes)
                    else:
                        getattr(au, probe)(scr)
                except Exception as e:
                    au.check(sid, 'probe', probe, 'errored', f'{type(e).__name__}: {str(e)[:160]}')
                    d.restore()
            print(f'{sid}: {sum(1 for r in au.results if r["screen"] == sid and r["status"] == "failed")} failure(s)', flush=True)
    apk_sha = hashlib.sha256(a.apk.read_bytes()).hexdigest() if a.apk else None
    out = {'package': g['package'], 'device': g['device'], 'started': started, 'finished': now(), 'sample': sample,
           'checks': au.checks, 'results': au.results, 'settings_changed': changed, 'apk_sha256': apk_sha}
    (a.run / 'run.json').write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8', newline='\n')
    fails = [r for r in au.results if r['status'] == 'failed']
    print(f'{len(sample)} screen(s), {len(au.checks)} check(s), {len(fails)} failure result(s) -> {a.run / "run.json"}')
    for r in fails:
        print(f'  {r["screen"]} {r["check"]:26} {r["element"]["resource_id"] or r["element"]["label"][:30]} {r["measured"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
