#!/usr/bin/env python3
"""Check the host and the mobile tools, and record each as ran-able or not_run with the reason.

    python check_tools.py --out run/tools.json [--serial emulator-5554]

Android: adb, the emulator, AVDs and the connected device (model, Android version, API level).
iOS: needs macOS with Xcode 15+ (xcrun simctl); on any other host every iOS check is not_run with that reason.
Appium 2 and its drivers are optional; the Accessibility Test Framework needs its instrumentation APK.
"""
from __future__ import annotations
import argparse, json, platform, shutil, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from adb_device import Device  # noqa: E402


def run(cmd: list[str], timeout=60) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=sys.platform == 'win32' and cmd[0].endswith(('.bat', '.cmd')))
        return (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--serial')
    a = ap.parse_args()
    host = {'os': platform.system(), 'release': platform.release(), 'python': platform.python_version()}
    tools = []

    def tool(name, version, status, reason=None, licence=None):
        tools.append({'name': name, 'version': version, 'license': licence, 'status': status, 'reason': reason})

    adb = shutil.which('adb')
    v = run(['adb', 'version']) if adb else None
    tool('adb', (v or '').splitlines()[1].split()[-1] if v and len(v.splitlines()) > 1 else None, 'ran' if adb else 'not_run',
         None if adb else 'adb is not on PATH', 'Android SDK licence')
    device = None
    if adb:
        try:
            device = Device(a.serial).info()
        except Exception as e:
            device = {'error': str(e)[:200]}
    tool('mobile-checks', '1.0.0', 'ran' if device and 'error' not in device else 'not_run',
         None if device and 'error' not in device else 'no Android device or emulator connected', 'repo licence')
    tool('accessibility-test-framework', None, 'not_run',
         'the ATF instrumentation APK is not built in this version; the skill\'s own checks cover labels, target size and contrast', 'Apache-2.0')
    appium = shutil.which('appium')
    av = run(['appium', '--version']) if appium else None
    drivers = run(['appium', 'driver', 'list', '--installed']) if appium else None
    tool('appium', (av or '').splitlines()[-1] if av else None, 'not_run',
         'installed but not used: the Android run uses adb and uiautomator' if appium else 'not installed (optional)', 'Apache-2.0')
    if host['os'] == 'Darwin' and shutil.which('xcrun'):
        xv = run(['xcodebuild', '-version'])
        tool('xcode-accessibility-audit', (xv or '').splitlines()[0] if xv else None, 'not_run', 'iOS runs are not built in this version', 'Apple Xcode licence')
    else:
        tool('xcode-accessibility-audit', None, 'not_run', f'iOS needs macOS with Xcode 15 or later; this host is {host["os"]}', 'Apple Xcode licence')
    out = {'host': host, 'device': device, 'tools': tools, 'appium_drivers': [l.strip('- ').strip() for l in (drivers or '').splitlines() if '@' in l],
           'platforms': {'android': 'ran' if device and 'error' not in device else 'not_run',
                         'ios': 'not_run'}}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8', newline='\n')
    for t in tools:
        print(f'{t["name"]:30} {t["status"]:8} {t["version"] or "":12} {t["reason"] or ""}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
