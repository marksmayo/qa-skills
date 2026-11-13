#!/usr/bin/env python3
"""Thin adb wrapper for the mobile app auditor: input, hierarchy, screenshots, settings, logs (stdlib).

    from adb_device import Device
    d = Device(serial=None, journal=run / 'device_changes.json', script='explore')
    d.launch('au.example.app', '.MainActivity'); nodes = d.hierarchy(); png = d.screenshot()
    with d.settings_guard() as changed:          # every setting changed inside is restored on exit
        d.put_setting('system', 'font_scale', '2.0')

Nodes come from `uiautomator dump`: dicts with class, resource_id, text, desc, bounds (x1, y1, x2, y2 in px),
clickable, focusable, focused, enabled, scrollable, package, index path. Sizes in dp use the device density.

Every device change (a setting, the display density, night mode, airplane mode) is written to the journal
file BEFORE it is made, with the value it had before. If the process or the emulator dies mid-probe, the
journal still says what was left changed, and

    python adb_device.py restore --journal run/device_changes.json [--serial emulator-5554]
    python adb_device.py status  --journal run/device_changes.json

puts it back (or lists it). A Device opened on a journal that still holds unrestored changes refuses to
start: its "before" values would be the leftovers of the killed run.
"""
from __future__ import annotations
import argparse, contextlib, json, os, re, subprocess, sys, time, xml.etree.ElementTree as ET
from pathlib import Path

ADB = os.environ.get('ADB', 'adb')
PROBE_KEY = 'SHIFT_LEFT'  # the harmless key the explorer presses after each action


class PendingRestore(RuntimeError):
    """The journal holds changes an earlier run made and never put back."""


def now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _write_json(path: Path, data) -> None:
    """Write atomically, so a kill mid-write never leaves half a journal or half a run record."""
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(data, indent=1) + '\n', encoding='utf-8', newline='\n')
    os.replace(tmp, path)


def pending(journal: Path) -> list[dict]:
    """The changes in a journal that were not restored (every session)."""
    if not journal or not Path(journal).exists():
        return []
    data = json.loads(Path(journal).read_text(encoding='utf-8'))
    return [c for s in data.get('sessions', []) for c in s.get('changes', []) if not c.get('restored')]


class Device:
    def __init__(self, serial: str | None = None, journal: Path | None = None, script: str = ''):
        self.serial = serial or os.environ.get('ANDROID_SERIAL')
        self.changed: list[dict] = []
        self._density = None
        self.journal = Path(journal) if journal else None
        self._session = None
        if self.journal:
            left = pending(self.journal)
            if left:
                names = ', '.join(c.get('key', c['kind']) for c in left)
                raise PendingRestore(f'{self.journal} holds changes an earlier run never restored ({names}). Put them back first: '
                                     f'python {Path(__file__).name} restore --journal {self.journal}')
            data = json.loads(self.journal.read_text(encoding='utf-8')) if self.journal.exists() else {'sessions': []}
            self._session = {'script': script, 'serial': self.serial, 'started': now(), 'changes': self.changed}
            data['sessions'].append(self._session)
            self._data = data

    # ------------------------------------------------------------ plumbing
    def adb(self, *args: str, timeout: int = 60, binary: bool = False, check: bool = True):
        cmd = [ADB] + (['-s', self.serial] if self.serial else []) + list(args)
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if check and r.returncode:
            raise RuntimeError(f'{" ".join(args)}: {r.stderr.decode(errors="replace").strip()[:300]}')
        return r.stdout if binary else r.stdout.decode('utf-8', errors='replace').replace('\r\n', '\n')

    def shell(self, cmd: str, timeout: int = 60, check: bool = True) -> str:
        return self.adb('shell', cmd, timeout=timeout, check=check)

    def prop(self, name: str) -> str:
        return self.shell(f'getprop {name}').strip()

    def info(self) -> dict:
        size = re.search(r'(\d+)x(\d+)', self.shell('wm size'))
        return {'serial': self.serial or self.adb('get-serialno').strip(), 'model': self.prop('ro.product.model'),
                'android': self.prop('ro.build.version.release'), 'api': self.prop('ro.build.version.sdk'),
                'emulator': self.prop('ro.kernel.qemu') == '1' or self.prop('ro.boot.qemu') == '1',
                'size_px': [int(size.group(1)), int(size.group(2))] if size else None, 'density': self.density()}

    def density(self) -> int:
        if self._density is None:
            out = self.shell('wm density')
            m = re.search(r'Override density: (\d+)', out) or re.search(r'Physical density: (\d+)', out)
            self._density = int(m.group(1)) if m else 160
        return self._density

    def dp(self, px: float) -> float:
        return round(px * 160 / self.density(), 1)

    # ------------------------------------------------------------ app
    def install(self, apk: str):
        self.adb('install', '-r', apk, timeout=180)

    def clear(self, package: str):
        self.shell(f'pm clear {package}')

    def launch(self, package: str, activity: str | None = None, wait: float = 1.5):
        if activity:
            self.shell(f'am start -W -n {package}/{activity}', timeout=60)
        else:
            self.shell(f'monkey -p {package} -c android.intent.category.LAUNCHER 1', timeout=60)
        time.sleep(wait)

    def stop(self, package: str):
        self.shell(f'am force-stop {package}')

    def top_activity(self) -> str | None:
        out = self.shell('dumpsys activity activities', check=False)
        m = re.search(r'topResumedActivity=ActivityRecord\{\S+ \S+ ([\w.]+/[\w.$]+)', out) or re.search(r'mResumedActivity: ActivityRecord\{\S+ \S+ ([\w.]+/[\w.$]+)', out)
        return m.group(1) if m else None

    def pid(self, package: str) -> int | None:
        out = self.shell(f'pidof {package}', check=False).strip()
        return int(out.split()[0]) if out and out.split()[0].isdigit() else None

    # ------------------------------------------------------------ input
    def tap(self, x: int, y: int):
        self.shell(f'input tap {int(x)} {int(y)}')

    def key(self, code: str):
        self.shell(f'input keyevent {code}')

    def text(self, value: str):
        self.shell("input text '" + value.replace(' ', '%s').replace("'", '') + "'")

    def back(self):
        self.key('KEYCODE_BACK')

    def rotate(self, landscape: bool, wait: float = 1.5):
        """Rotation through the settings (journaled). Nothing is changed, and no time spent, when it is already so."""
        want = '1' if landscape else '0'
        if self.get_setting('system', 'accelerometer_rotation') == '0' and self.get_setting('system', 'user_rotation') == want:
            return
        self.put_setting('system', 'accelerometer_rotation', '0')
        self.put_setting('system', 'user_rotation', want)
        time.sleep(wait)

    def rotation(self) -> int:
        out = self.shell('dumpsys window displays', check=False)
        m = re.search(r'mCurrentRotation=(?:ROTATION_)?(\d+)', out) or re.search(r'mRotation=(\d+)', out)
        return int(m.group(1)) if m else 0

    # ------------------------------------------------------------ observation
    def hierarchy(self, retries: int = 3) -> list[dict]:
        last = ''
        for _ in range(retries):
            out = self.shell('uiautomator dump /sdcard/qa_ui.xml', check=False, timeout=60)
            last = out
            if 'dumped to' in out:
                xml = self.adb('exec-out', 'cat /sdcard/qa_ui.xml', binary=True)
                return parse_hierarchy(xml)
            time.sleep(1)
        raise RuntimeError(f'uiautomator dump failed: {last.strip()[:200]}')

    def screenshot(self) -> bytes:
        return self.adb('exec-out', 'screencap -p', binary=True, timeout=60)

    def logcat_clear(self):
        self.adb('logcat', '-b', 'all', '-c', check=False)

    def logcat(self, buffer: str = 'main', pid: int | None = None) -> list[str]:
        args = ['logcat', '-b', buffer, '-d', '-v', 'threadtime']
        if pid:
            args += [f'--pid={pid}']
        return self.adb(*args, timeout=60, check=False).splitlines()

    def device_time(self) -> str | None:
        """The device clock in logcat's format (MM-DD hh:mm:ss.mmm), to read only the lines logged since a moment."""
        out = self.shell("date +'%m-%d %H:%M:%S'", check=False).strip()
        return out + '.000' if re.fullmatch(r'\d\d-\d\d \d\d:\d\d:\d\d', out) else None

    def logcat_since(self, since: str | None, buffers=('crash', 'system', 'main')) -> list[str]:
        """Crash, system and main lines logged since `since` (device time) in one call; every line when it is None."""
        b = ' '.join(f'-b {x}' for x in buffers)
        if since:
            return self.shell(f"logcat {b} -v threadtime -t '{since}'", timeout=60, check=False).splitlines()
        return self.shell(f'logcat {b} -d -v threadtime', timeout=60, check=False).splitlines()

    def anr_traces(self) -> str:
        return self.shell('dumpsys dropbox --print data_app_anr', check=False, timeout=90)

    # ------------------------------------------------------------ settings
    def get_setting(self, ns: str, key: str) -> str:
        return self.shell(f'settings get {ns} {key}').strip()

    def _record(self, match, entry_fn):
        """Note a change (keeping the first 'before' of a reopened one) and save the journal BEFORE the change is made."""
        hit = next((c for c in self.changed if match(c)), None)
        if hit:
            hit['restored'] = False
            hit.pop('after', None)
        else:
            self.changed.append(dict(entry_fn(), restored=False))
        self._save()

    def _save(self):
        if self.journal:
            self._session['updated'] = now()
            _write_json(self.journal, self._data)

    def put_setting(self, ns: str, key: str, value: str):
        self._record(lambda c: c['kind'] == 'setting' and c['ns'] == ns and c['key'] == key,
                     lambda: {'kind': 'setting', 'ns': ns, 'key': key, 'before': self.get_setting(ns, key)})
        self.shell(f'settings put {ns} {key} {value}')

    def _density_override(self) -> str:
        m = re.search(r'Override density: (\d+)', self.shell('wm density'))
        return m.group(1) if m else 'reset'

    def set_density(self, value: int | None):
        self._record(lambda c: c['kind'] == 'density', lambda: {'kind': 'density', 'before': self._density_override()})
        self.shell(f'wm density {value}' if value else 'wm density reset')
        self._density = None

    def _night(self) -> str:
        return self.shell('cmd uimode night').strip().split(':')[-1].strip()

    def night_mode(self, on: bool):
        self._record(lambda c: c['kind'] == 'night_mode', lambda: {'kind': 'night_mode', 'before': self._night()})
        self.shell(f'cmd uimode night {"yes" if on else "no"}')

    def airplane_mode(self, on: bool):
        """Cut (or give back) the device's network: --offline, so no screen can reach a real backend."""
        self._record(lambda c: c['kind'] == 'airplane_mode',
                     lambda: {'kind': 'airplane_mode', 'before': self.get_setting('global', 'airplane_mode_on')})
        self.shell(f'cmd connectivity airplane-mode {"enable" if on else "disable"}')

    def restore(self) -> list[dict]:
        for c in reversed(self.changed):
            if c['restored']:
                continue
            if c['kind'] == 'setting':
                if c['before'] in ('null', ''):
                    self.shell(f'settings delete {c["ns"]} {c["key"]}', check=False)
                else:
                    self.shell(f'settings put {c["ns"]} {c["key"]} {c["before"]}')
                c['after'] = self.get_setting(c['ns'], c['key'])
                c['restored'] = c['after'] == c['before'] or (c['before'] in ('null', '') and c['after'] in ('null', ''))
            elif c['kind'] == 'density':
                self.shell('wm density reset' if c['before'] == 'reset' else f'wm density {c["before"]}')
                self._density = None
                c['after'] = self._density_override()
                c['restored'] = c['after'] == c['before']
            elif c['kind'] == 'night_mode':
                self.shell(f'cmd uimode night {c["before"]}', check=False)
                c['after'] = self._night()
                c['restored'] = c['after'] == c['before']
            elif c['kind'] == 'airplane_mode':
                self.shell(f'cmd connectivity airplane-mode {"enable" if c["before"] == "1" else "disable"}', check=False)
                c['after'] = self.get_setting('global', 'airplane_mode_on')
                c['restored'] = c['after'] == c['before']
            self._save()
        return self.changed

    @contextlib.contextmanager
    def settings_guard(self):
        try:
            yield self.changed
        finally:
            self.restore()


def restore_journal(journal: Path, serial: str | None = None, device_cls=Device) -> list[dict]:
    """Put back every unrestored change in a journal, newest session first; the journal records the outcome."""
    journal = Path(journal)
    data = json.loads(journal.read_text(encoding='utf-8'))
    done = []
    for s in reversed(data.get('sessions', [])):
        if all(c.get('restored') for c in s.get('changes', [])):
            continue
        d = device_cls(serial or s.get('serial'))
        d.changed = s['changes']
        d.restore()
        s['restored_by_command'] = now()
        done += [c for c in s['changes']]
        _write_json(journal, data)
    return done


def parse_hierarchy(xml: bytes) -> list[dict]:
    root = ET.fromstring(xml)
    out = []

    def walk(el, path):
        for i, ch in enumerate(el.findall('node')):
            a = ch.attrib
            b = [int(x) for x in re.findall(r'\d+', a.get('bounds', '[0,0][0,0]'))]
            out.append({'path': f'{path}/{i}', 'class': a.get('class', ''), 'resource_id': a.get('resource-id') or None,
                        'text': a.get('text') or '', 'desc': a.get('content-desc') or '', 'package': a.get('package'),
                        'bounds': b, 'clickable': a.get('clickable') == 'true', 'long_clickable': a.get('long-clickable') == 'true',
                        'focusable': a.get('focusable') == 'true', 'focused': a.get('focused') == 'true',
                        'enabled': a.get('enabled') == 'true', 'scrollable': a.get('scrollable') == 'true',
                        'checkable': a.get('checkable') == 'true', 'password': a.get('password') == 'true',
                        'heading': a.get('heading') == 'true' if 'heading' in a else None,
                        'depth': path.count('/'), 'children': len(ch.findall('node'))})
            walk(ch, f'{path}/{i}')
    walk(root, '')
    return out


def label_of(n: dict, nodes: list[dict]) -> str:
    """The accessible label of a node: its own text or description, else its descendants' (as TalkBack merges them)."""
    own = (n['desc'] or n['text']).strip()
    if own:
        return own
    kids = [m for m in nodes if m['path'].startswith(n['path'] + '/') and (m['desc'] or m['text'])]
    return ' '.join((m['desc'] or m['text']).strip() for m in kids)[:120]


def visible_name(n: dict, nodes: list[dict]) -> str:
    """What the control says on screen: its own text, else its descendants' text, else the first sentence of its description.

    A navigation row whose description explains the next screen ("Archive. ... then delete a draft ...") is named
    by its visible title, so words in the explanation don't make the row look destructive."""
    if n['text'].strip():
        return n['text'].strip()
    kids = [m['text'].strip() for m in nodes if m['path'].startswith(n['path'] + '/') and m['text'].strip()]
    if kids:
        return ' '.join(kids)[:120]
    return re.split(r'(?<=[.!?])\s|\s[-\u2013\u2014]\s', n['desc'].strip(), maxsplit=1)[0][:120]


# ---------------------------------------------------------------- ANR records
ENTRY_SPLIT = re.compile(r'^={20,}\s*$', re.M)


def parse_dropbox(text: str) -> list[dict]:
    """Split `dumpsys dropbox --print data_app_anr` into entries: when, process, pid, subject, the main thread of
    that process's own trace section, and the device's CPU pressure at the time."""
    out = []
    for chunk in ENTRY_SPLIT.split(text)[1:]:
        head = re.search(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) data_app_anr', chunk, re.M)
        if not head:
            continue
        field = lambda k: (re.search(rf'^{k}: (.*)$', chunk, re.M) or [None, None])[1]  # noqa: E731
        proc, pid = field('Process'), field('PID')
        main: list[str] = []
        # the process's own section: "----- pid N at ... -----" with "Cmd line: <process>", then its "main" thread
        parts = re.split(r'^----- pid (\d+) at .*-----\s*$', chunk, flags=re.M)
        for i in range(1, len(parts) - 1, 2):
            body = parts[i + 1]
            if parts[i] == pid or re.search(rf'^Cmd line: {re.escape(proc or "")}\s*$', body, re.M):
                m = re.search(r'^"main".*?(?:\n\s*\n|\Z)', body, re.S | re.M)
                if m:
                    main = [l.strip() for l in m.group(0).splitlines() if l.strip().startswith('at ')]
                break
        cpu = re.search(r'/proc/pressure/cpu -----\s*\nsome avg10=([\d.]+)', chunk)
        out.append({'logged': head.group(1), 'process': proc, 'pid': int(pid) if pid and pid.isdigit() else None,
                    'timestamp': field('Timestamp'), 'subject': field('Subject'),
                    'key': field('ErrorId') or f'{head.group(1)}|{proc}|{pid}', 'main_thread': main,
                    'cpu_pressure_avg10': float(cpu.group(1)) if cpu else None})
    return out


def anr_reason_from_log(lines: list[str]) -> str | None:
    """The reason from the system's ANR line. Android 16 prints `Reason:Input ...` with no space; older ones `Reason: Input ...`."""
    for l in lines:
        m = re.search(r'Reason:\s*(.+)$', l)
        if m:
            return m.group(1).strip()
    return None


def short_reason(reason: str | None) -> str | None:
    """The kind of timeout without the window details, so occurrences of one cause merge:
    'Input dispatching timed out (... Waited 5006ms for KeyEvent(...)).' -> 'Input dispatching timed out'."""
    if not reason:
        return None
    r = reason.strip()
    i = r.find('(')
    if i > 0:
        r = r[:i]
    return r.strip().rstrip('.').strip() or None


def input_event(reason: str | None) -> dict | None:
    """The input the app failed to take within the timeout, when the reason names it; probe_input is True when it was the
    explorer's own harmless key press (the block was real, but the probe's input is what exposed it)."""
    if not reason:
        return None
    m = re.search(r'Waited (\d+)ms for (\w+)\((.*)', reason)
    if not m:
        return None
    key = re.search(r'keyCode=(\w+?)\(', m.group(3)) or re.search(r'keyCode=(\w+)', m.group(3))
    return {'waited_ms': int(m.group(1)), 'event': m.group(2), 'key': key.group(1) if key else None,
            'probe_input': bool(key) and key.group(1) == PROBE_KEY}


def newest_anr(entries: list[dict], package: str, seen: set) -> dict | None:
    """The newest dropbox entry of this package that was not there before (entries are oldest first)."""
    mine = [e for e in entries if e['process'] == package and e['key'] not in seen]
    return mine[-1] if mine else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', choices=['restore', 'status'])
    ap.add_argument('--journal', type=Path, required=True)
    ap.add_argument('--serial', help='the device to restore (default: the one each session recorded, else ANDROID_SERIAL)')
    a = ap.parse_args()
    if not a.journal.exists():
        print(f'{a.journal}: no journal, nothing was changed'); return 0
    left = pending(a.journal)
    if a.command == 'status':
        for c in left:
            print(f'  not restored: {c["kind"]} {c.get("ns", "")} {c.get("key", "")} (before: {c["before"]})')
        print(f'{len(left)} change(s) not restored')
        return 1 if left else 0
    if not left:
        print('every change in the journal was already restored'); return 0
    done = restore_journal(a.journal, a.serial)
    bad = [c for c in done if not c.get('restored')]
    for c in done:
        print(f'  {c["kind"]} {c.get("ns", "")} {c.get("key", "")}: before {c["before"]}, now {c.get("after")} -> {"restored" if c["restored"] else "NOT restored"}')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
