#!/usr/bin/env python3
"""Thin adb wrapper for the mobile app auditor: input, hierarchy, screenshots, settings, logs (stdlib).

    from adb_device import Device
    d = Device(serial=None)                      # the only device, or ANDROID_SERIAL
    d.launch('au.example.app', '.MainActivity'); nodes = d.hierarchy(); png = d.screenshot()
    with d.settings_guard() as changed:          # every setting changed inside is restored on exit
        d.put_setting('system', 'font_scale', '2.0')

Nodes come from `uiautomator dump`: dicts with class, resource_id, text, desc, bounds (x1, y1, x2, y2 in px),
clickable, focusable, focused, enabled, scrollable, package, index path. Sizes in dp use the device density.
"""
from __future__ import annotations
import contextlib, os, re, subprocess, time, xml.etree.ElementTree as ET

ADB = os.environ.get('ADB', 'adb')


class Device:
    def __init__(self, serial: str | None = None):
        self.serial = serial or os.environ.get('ANDROID_SERIAL')
        self.changed: list[dict] = []
        self._density = None

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
        self.adb('install', '-r', '-g' if False else '-r', apk, timeout=180)

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
        return int(out.split()[0]) if out else None

    # ------------------------------------------------------------ input
    def tap(self, x: int, y: int):
        self.shell(f'input tap {int(x)} {int(y)}')

    def key(self, code: str):
        self.shell(f'input keyevent {code}')

    def text(self, value: str):
        self.shell("input text '" + value.replace(' ', '%s').replace("'", '') + "'")

    def back(self):
        self.key('KEYCODE_BACK')

    def rotate(self, landscape: bool):
        self.put_setting('system', 'accelerometer_rotation', '0')
        self.put_setting('system', 'user_rotation', '1' if landscape else '0')
        time.sleep(1.5)

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

    def anr_traces(self) -> str:
        return self.shell('dumpsys dropbox --print data_app_anr', check=False, timeout=60)

    # ------------------------------------------------------------ settings
    def get_setting(self, ns: str, key: str) -> str:
        return self.shell(f'settings get {ns} {key}').strip()

    def put_setting(self, ns: str, key: str, value: str):
        before = self.get_setting(ns, key)
        if not self._reopen(lambda c: c['kind'] == 'setting' and c['ns'] == ns and c['key'] == key):
            self.changed.append({'kind': 'setting', 'ns': ns, 'key': key, 'before': before, 'restored': False})
        self.shell(f'settings put {ns} {key} {value}')

    def set_density(self, value: int | None):
        if not self._reopen(lambda c: c['kind'] == 'density'):
            self.changed.append({'kind': 'density', 'before': 'reset', 'restored': False})
        self.shell(f'wm density {value}' if value else 'wm density reset')
        self._density = None

    def night_mode(self, on: bool):
        if not self._reopen(lambda c: c['kind'] == 'night_mode'):
            before = self.shell('cmd uimode night').strip().split(':')[-1].strip()
            self.changed.append({'kind': 'night_mode', 'before': before, 'restored': False})
        self.shell(f'cmd uimode night {"yes" if on else "no"}')

    def _reopen(self, match) -> bool:
        """A setting changed again after a restore must be restored again: keep its first 'before', clear 'restored'."""
        hit = next((c for c in self.changed if match(c)), None)
        if hit:
            hit['restored'] = False
            hit.pop('after', None)
        return hit is not None

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
                self.shell('wm density reset')
                self._density = None
                c['restored'] = 'Override density' not in self.shell('wm density')
            elif c['kind'] == 'night_mode':
                self.shell(f'cmd uimode night {c["before"]}', check=False)
                c['after'] = self.shell('cmd uimode night').strip().split(':')[-1].strip()
                c['restored'] = c['after'] == c['before']
        return self.changed

    @contextlib.contextmanager
    def settings_guard(self):
        try:
            yield self.changed
        finally:
            self.restore()


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
