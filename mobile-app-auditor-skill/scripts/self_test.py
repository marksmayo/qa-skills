#!/usr/bin/env python3
"""Check the device-facing logic against a fake adb, with no emulator or device.

    python self_test.py

A fake device answers the adb commands the scripts send (settings, wm density, uimode, pidof, uiautomator
dumps, logcat, dumpsys dropbox) from canned, synthetic output. Checked: ANR records take the newest
dropbox entry of the audited package that appeared during the run (never another app's or an older one),
read Android 16's `Reason:` with no space, name the input that timed out and whether it was the explorer's
own key press, and decide "recovered" from the process (not from log lines still holding the ANR); a launch
behind a system dialog fails with a clear error; every device change is journaled before it is made, a
journal with unrestored changes refuses a new run, and `restore` puts them back; the destructive-control
filter reads the visible name, not a long description; clipped text at font scale 2.0 is caught when the
framework reports clipped bounds; icon-font glyphs are kept out of contrast; the keyboard probe stops once
focus goes round; run.json is resumable; the validator wants a partial run named in the limitations.
Writes only to a temporary directory; exit code 0 when every check passes.
"""
from __future__ import annotations
import json, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import adb_device  # noqa: E402
import crash_explore  # noqa: E402
import explore  # noqa: E402
import run_mobile_audit as rma  # noqa: E402

FAILS: list[str] = []
PKG = 'org.example.tides'
time.sleep = lambda s: None  # nothing waits for a fake device


def check(ok: bool, what: str) -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        FAILS.append(what)


# ------------------------------------------------------------------ fake device
def node(cls, pkg=PKG, rid='', text='', desc='', bounds=(0, 0, 100, 100), clickable=False, focused=False, kids=''):
    b = '[%d,%d][%d,%d]' % bounds
    return (f'<node class="{cls}" package="{pkg}" resource-id="{rid}" text="{text}" content-desc="{desc}" bounds="{b}" '
            f'clickable="{str(clickable).lower()}" enabled="true" focusable="{str(clickable).lower()}" focused="{str(focused).lower()}">{kids}</node>')


def dump(*nodes) -> bytes:
    return ('<?xml version="1.0"?><hierarchy rotation="0">' + ''.join(nodes) + '</hierarchy>').encode()


APP_SCREEN = dump(node('android.widget.FrameLayout', bounds=(0, 0, 1080, 2400), kids=node('android.widget.Button', rid='go', text='Go', clickable=True)))
SYSTEM_DIALOG = dump(node('android.widget.FrameLayout', pkg='com.android.systemui', bounds=(0, 0, 1080, 2400),
                          kids=node('android.widget.Button', pkg='com.android.systemui', rid='android:id/aerr_wait', text='Wait', bounds=(100, 1000, 500, 1100), clickable=True)))


def dropbox_entry(logged, process, pid, subject, frames, error_id, other_main=None):
    main = '\n'.join(f'  at {f}' for f in frames)
    other = ''
    if other_main:  # a second process's dump in the same entry, whose "main" thread must not be taken
        other = f'\n----- pid 300 at {logged}.0+0000 -----\nCmd line: system_server\n\n"main" prio=5 tid=1 Native\n' + \
                '\n'.join(f'  at {f}' for f in other_main) + '\n\n'
    return f'''========================================
{logged} data_app_anr (compressed text, 900 bytes)
Process: {process}
PID: {pid}
Timestamp: {logged}.000+0000
ErrorId: {error_id}

----- Output from /proc/pressure/cpu -----
some avg10=31.50 avg60=20.00 avg300=10.00 total=1
----- End output from /proc/pressure/cpu -----
Subject: {subject}

----- pid {pid} at {logged}.0+0000 -----
Cmd line: {process}

DALVIK THREADS (2):
"main" prio=5 tid=1 Native
  | group="main" sCount=1
{main}

"worker" prio=5 tid=2 Waiting
  at java.lang.Object.wait(Native method)
{other}'''


OLD_OTHER = dropbox_entry('2026-01-02 10:00:00', 'org.example.harbour', 111, 'Input dispatching timed out (Application does not have a focused window).',
                          ['java.lang.Thread.sleep(Native method)', 'org.example.harbour.MapScreen.onCreate(MapScreen.java:6)'], 'aaa-1')
OLD_MINE = dropbox_entry('2026-01-02 10:05:00', PKG, 222, 'Broadcast of Intent { act=android.intent.action.TIME_TICK }',
                         ['org.example.tides.OldThing.run(OldThing.java:1)'], 'bbb-2')
NEW_MINE = dropbox_entry('2026-01-03 09:00:31', PKG, 4242,
                         'Input dispatching timed out (5c1 org.example.tides/org.example.tides.Main is not responding. Waited 5006ms for KeyEvent(deviceId=-1, action=DOWN, keyCode=SHIFT_LEFT(59), scanCode=0), policyFlags=0x6b000000).',
                         ['android.os.BinderProxy.transactNative(Native method)', 'org.example.tides.ChartView.<init>(ChartView.java:40)',
                          'org.example.tides.Main.onCreate(Main.java:12)'], 'ccc-3',
                         other_main=['com.android.server.SystemServer.run(SystemServer.java:1)'])
ANR_LINE = ('01-03 09:00:13.000   707   866 I WindowManager: ANR in Window{5c1 u0 org.example.tides/org.example.tides.Main}. '
            'Reason:Input dispatching timed out (5c1 org.example.tides/org.example.tides.Main is not responding. Waited 5006ms for '
            'KeyEvent(deviceId=-1, action=DOWN, keyCode=SHIFT_LEFT(59), scanCode=0), policyFlags=0x6b000000).')


class FakeDevice(adb_device.Device):
    def __init__(self, *a, **k):
        self.settings = {('system', 'font_scale'): '1.0', ('system', 'accelerometer_rotation'): '1', ('system', 'user_rotation'): '0',
                         ('global', 'airplane_mode_on'): '0'}
        self.override = None
        self.night = 'no'
        self.pids = {PKG: [4242]}
        self.screens = [APP_SCREEN]
        self.logs: list[str] = []
        self.dropbox = 'Drop box contents: 0 entries\n'
        self.calls: list[str] = []
        self.on_put = None
        super().__init__(*a, **k)

    def adb(self, *args, timeout=60, binary=False, check=True):
        cmd = ' '.join(args)
        self.calls.append(cmd)
        if args[0] == 'exec-out' and 'qa_ui.xml' in args[1]:
            scr = self.screens[0] if len(self.screens) == 1 else self.screens.pop(0)
            return scr
        if args[0] == 'exec-out':
            return b''
        if args[0] == 'logcat':
            return ''
        if args[0] == 'install':
            return ''
        sh = args[1]
        m = re.match(r'settings get (\w+) (\w+)', sh)
        if m:
            return self.settings.get((m[1], m[2]), 'null') + '\n'
        m = re.match(r'settings put (\w+) (\w+) (\S+)', sh)
        if m:
            if self.on_put:
                self.on_put(m[1], m[2], m[3])
            self.settings[(m[1], m[2])] = m[3]
            return ''
        m = re.match(r'settings delete (\w+) (\w+)', sh)
        if m:
            self.settings.pop((m[1], m[2]), None)
            return ''
        if sh == 'wm density':
            return 'Physical density: 420\n' + (f'Override density: {self.override}\n' if self.override else '')
        m = re.match(r'wm density (\S+)', sh)
        if m:
            self.override = None if m[1] == 'reset' else m[1]
            return ''
        if sh == 'cmd uimode night':
            return f'Night mode: {self.night}\n'
        m = re.match(r'cmd uimode night (\w+)', sh)
        if m:
            self.night = m[1]
            return ''
        m = re.match(r'cmd connectivity airplane-mode (\w+)', sh)
        if m:
            self.settings[('global', 'airplane_mode_on')] = '1' if m[1] == 'enable' else '0'
            return ''
        if sh.startswith('pidof'):
            p = self.pids.get(sh.split()[1]) or []
            return (str(p.pop(0) if len(p) > 1 else p[0]) + '\n') if p else ''
        if sh.startswith('date'):
            return '01-03 09:00:08\n'
        if sh.startswith('logcat'):
            return '\n'.join(self.logs)
        if sh.startswith('dumpsys dropbox'):
            return self.dropbox
        if sh.startswith('dumpsys activity'):
            return f'  topResumedActivity=ActivityRecord{{1 u0 {PKG}/.Main t1}}\n'
        if sh.startswith('uiautomator dump'):
            return 'UI hierchary dumped to: /sdcard/qa_ui.xml\n'
        if sh.startswith('getprop'):
            return '1\n'
        if sh.startswith('wm size'):
            return 'Physical size: 1080x2400\n'
        return ''


def args(**k):
    base = dict(settle=0, input_delay=0, anr_trace_wait=0, launch_wait=0, step_wait=0, no_back=False, no_rotate=True,
                max_screens=5, max_actions=5, budget_min=5)
    base.update(k)
    return SimpleNamespace(**base)


# ------------------------------------------------------------------ tests
def test_anr_parsing():
    print('ANR parsing')
    entries = adb_device.parse_dropbox('Drop box contents: 3 entries\n\n' + OLD_OTHER + OLD_MINE + NEW_MINE)
    check(len(entries) == 3, 'three dropbox entries')
    seen = {entries[1]['key']}
    e = adb_device.newest_anr(entries, PKG, seen)
    check(e is not None and e['key'] == 'ccc-3', "newest entry of the audited package, not another app's or an older one")
    check(e['main_thread'][1].endswith('ChartView.<init>(ChartView.java:40)') and not any('SystemServer' in f for f in e['main_thread']),
          "main thread from the package's own pid section, not another process's")
    check(e['cpu_pressure_avg10'] == 31.5, 'device CPU pressure recorded')
    check(adb_device.newest_anr(entries, 'org.example.absent', set()) is None, 'no entry for a package that never ANRed')
    full = adb_device.anr_reason_from_log([ANR_LINE])
    check(full is not None and full.startswith('Input dispatching timed out (5c1'), "Android 16 'Reason:' with no space is read")
    check(adb_device.anr_reason_from_log(['... ANR in x. Reason: Broadcast of Intent']) == 'Broadcast of Intent', "older 'Reason: ' still read")
    check(adb_device.short_reason(full) == 'Input dispatching timed out', 'short reason drops the details even when the subject ends in "."')
    check(adb_device.short_reason('Input dispatching timed out (Application does not have a focused window).') == 'Input dispatching timed out',
          'short reason of the older form')
    ie = adb_device.input_event(full)
    check(ie == {'waited_ms': 5006, 'event': 'KeyEvent', 'key': 'SHIFT_LEFT', 'probe_input': True}, "the timed-out input is the explorer's own SHIFT")
    check(crash_explore.what({'reason': 'Input dispatching timed out (Application does not have a focused window).'}) ==
          crash_explore.what({'reason': 'Input dispatching timed out'}), 'a replay compares the kind of timeout')


def test_explorer_anr(tmp: Path):
    print('Explorer ANR record')
    d = FakeDevice()
    d.dropbox = 'Drop box contents: 2 entries\n' + OLD_OTHER + OLD_MINE
    ex = explore.Explorer(d, PKG, '.Main', tmp / 'x1', args())
    d.dropbox += NEW_MINE
    d.logs = [ANR_LINE]  # the ANR line stays in the log after Wait: it must not decide the outcome
    rec = ex.record('anr', [{'rid': 'go', 'label': 'Go'}], 'S01', [ANR_LINE])
    check(rec['reason'] == 'Input dispatching timed out' and rec['reason_full'].startswith('Input dispatching timed out (5c1'), 'reason from the log line')
    check(rec['top_app_frame'] == 'at org.example.tides.ChartView.<init>(ChartView.java:40)', 'top app frame from this run\'s entry of this package')
    check(rec['after_wait'] == 'recovered' and rec['dialog'] == 'not_shown', 'same process alive and showing the app: recovered, despite the old ANR line')
    check(rec['input_event']['probe_input'] is True and rec['trace']['device_cpu_pressure_avg10'] == 31.5, 'probe input and device load recorded')
    d2 = FakeDevice()
    d2.dropbox = OLD_OTHER
    ex2 = explore.Explorer(d2, PKG, '.Main', tmp / 'x2', args())
    d2.pids = {PKG: [4242, 5151]}  # a new process after the wait: the ANR killed the app
    rec2 = ex2.record('anr', [{'rid': 'go', 'label': 'Go'}], 'S01', [ANR_LINE])
    check(rec2['after_wait'] == 'killed', 'another process after the wait: killed')
    check(rec2['top_app_frame'] is None and rec2['main_thread'] == [] and 'trace_note' in rec2,
          "no new entry of this package: main thread unknown, never another app's")


def test_launch_dialog(tmp: Path):
    print('Launch behind a system dialog')
    d = FakeDevice()
    d.screens = [SYSTEM_DIALOG]
    ex = explore.Explorer(d, PKG, '.Main', tmp / 'x3', args())
    try:
        ex.run()
        check(False, 'launch with no app node raises LaunchError')
    except explore.LaunchError as e:
        check('showed no node of its own' in str(e), 'launch with no app node raises a clear LaunchError (no StopIteration)')
    check(any(c.startswith('shell input tap 300 1050') for c in d.calls), 'the system dialog\'s Wait was pressed')
    d = FakeDevice()
    d.screens = [SYSTEM_DIALOG, APP_SCREEN, APP_SCREEN]
    ex = explore.Explorer(d, PKG, '.Main', tmp / 'x4', args())
    check(ex.launch_screen() == 'S01', 'a dialog on the first launch is waited out and the app found on the next')


def test_journal(tmp: Path):
    print('Settings journal and restore')
    j = tmp / 'device_changes.json'
    d = FakeDevice(journal=j, script='run_mobile_audit')
    seen = {}

    def on_put(ns, key, value):
        if key == 'font_scale' and value == '2.0':
            data = json.loads(j.read_text(encoding='utf-8'))
            seen['entry'] = next((c for s in data['sessions'] for c in s['changes'] if c.get('key') == 'font_scale'), None)
    d.on_put = on_put
    d.put_setting('system', 'font_scale', '2.0')
    check(seen.get('entry') == {'kind': 'setting', 'ns': 'system', 'key': 'font_scale', 'before': '1.0', 'restored': False},
          'the change is in the journal, with its before value, before the setting is changed')
    d.set_density(546)
    d.night_mode(True)
    d.airplane_mode(True)
    # the process dies here: nothing restored
    try:
        FakeDevice(journal=j, script='explore')
        check(False, 'a new run on a journal with unrestored changes is refused')
    except adb_device.PendingRestore as e:
        check('restore --journal' in str(e), 'a new run on a journal with unrestored changes is refused, naming the restore command')
    left = adb_device.pending(j)
    check(sorted(c.get('key', c['kind']) for c in left) == ['airplane_mode', 'density', 'font_scale', 'night_mode'], 'status lists every change left')
    fresh = FakeDevice()
    fresh.settings[('system', 'font_scale')] = '2.0'
    fresh.override, fresh.night = '546', 'yes'
    fresh.settings[('global', 'airplane_mode_on')] = '1'
    done = adb_device.restore_journal(j, device_cls=lambda serial: fresh)
    check(all(c['restored'] for c in done) and fresh.settings[('system', 'font_scale')] == '1.0' and fresh.override is None
          and fresh.night == 'no' and fresh.settings[('global', 'airplane_mode_on')] == '0', 'restore puts every change back from the journal')
    check(adb_device.pending(j) == [], 'the journal records the restoration')
    FakeDevice(journal=j, script='explore')  # opens without complaint now
    check(len(json.loads(j.read_text(encoding='utf-8'))['sessions']) == 1, 'a new session is saved only once it changes something')
    d3 = FakeDevice()
    d3.override = '480'
    d3.set_density(600)
    d3.restore()
    check(d3.override == '480' and d3.changed[0]['restored'], 'an existing density override is put back, not reset')
    d4 = FakeDevice()
    d4.settings[('system', 'accelerometer_rotation')] = '0'
    d4.rotate(False)
    check(d4.changed == [], 'rotating to the orientation already set changes nothing')


def test_destructive():
    print('Destructive-control filter')
    nodes = adb_device.parse_hierarchy(dump(
        node('android.view.ViewGroup', rid='Archive', desc='Archive. Restore a failed sync, then delete a draft through a confirm dialog.',
             clickable=True, bounds=(0, 100, 1080, 200), kids=node('android.widget.TextView', text='Archive', bounds=(20, 120, 500, 180))),
        node('android.widget.Button', rid='drop', text='Delete draft', clickable=True, bounds=(0, 300, 400, 400)),
        node('android.view.ViewGroup', desc='Pay now. Charges the saved card', clickable=True, bounds=(0, 500, 400, 600)),
        node('android.widget.Button', rid='next', text='Next', clickable=True, bounds=(0, 700, 400, 800))))
    ex = explore.Explorer.__new__(explore.Explorer)
    ex.pkg = PKG
    ctl = {c['rid'] or c['name']: c for c in ex.controls(nodes)}
    check(not ctl['Archive']['skipped'], 'a navigation row is named by its visible title, not its long description')
    check(ctl['drop']['skipped'] and ctl['drop']['skipped_for'] == 'delete', 'a Delete button is skipped, with the word that matched')
    check(ctl['Pay now.']['skipped'], 'a description-only control is named by its first sentence')
    check(not ctl['next']['skipped'], 'an ordinary button is tapped')


def test_text_and_keyboard():
    print('Text scaling, icon glyphs, keyboard')
    p1 = {'bounds': [0, 100, 540, 215]}
    p2 = {'bounds': [0, 100, 540, 215]}  # the row kept its 44 dp height
    n1 = {'bounds': [30, 133, 500, 180]}  # 47 px, one line
    n2 = {'bounds': [30, 120, 500, 215]}  # reported clipped to the row: grew 2.0x but needs about 2.2x
    check(rma.clipped(n1, n2, p1, p2, 2.2, 2400), 'text clipped by a row that kept its height is caught though its reported height grew')
    check(rma.clipped(n1, {'bounds': [30, 100, 500, 215]}, p1, p2, 1.8, 2400),
          'centred text overflowing both edges of the row (it now fills the row it did not fill) is caught')
    check(rma.expected_growth(18, [1.0, 1.0]) == 1.8 and rma.expected_growth(40, []) == 1.5 and rma.expected_growth(18, [2.2, 2.3, 2.1]) == 2.2,
          'expected growth: body text 1.8x, large text 1.5x, more when the free texts grew more (a header that kept its size does not lower it)')
    check(not rma.clipped(n1, n2, p1, {'bounds': [0, 100, 540, 330]}, 2.2, 2400), 'a row that grew does not clip')
    check(not rma.clipped(n1, {'bounds': [30, 120, 500, 200]}, p1, p2, 2.2, 2400), 'a text clear of the row bottom is not clipped')
    check(rma.icon_glyph('') == ['U+E001'] and rma.icon_glyph('Home') == [] and rma.icon_glyph('\U000f0001 ') == ['U+F0001'],
          'private-use glyphs are icon-font text')
    check(rma.tab_done(['a', 'b', 'c', 'a']) and not rma.tab_done(['a', 'b', 'c']), 'Tab stops once focus is back at the first element')
    check(rma.tab_done([None, None, None]) and rma.tab_done(['a', 'a', 'a']), 'Tab stops when focus is stuck')


def test_resume(tmp: Path):
    print('Resumable run.json')
    p = tmp / 'run.json'
    rma.save_run(p, {'package': PKG, 'apk_sha256': 'ab' * 32, 'sample': [{'id': 'S01'}], 'run_status': 'partial'})
    st = rma.resume_state(p, PKG, 'ab' * 32)
    check(st['sample'] == [{'id': 'S01'}] and not (tmp / 'run.json.tmp').exists(), 'run.json written atomically and read back for --resume')
    for pkg, sha, what in ((PKG, 'cd' * 32, 'another build'), ('org.example.other', None, 'another app')):
        try:
            rma.resume_state(p, pkg, sha)
            check(False, f'--resume refuses {what}')
        except SystemExit:
            check(True, f'--resume refuses {what}')


def test_audit_resume(tmp: Path):
    print('run_mobile_audit: incremental run.json and --resume')
    import png_io
    run = tmp / 'audit-run'
    (run / 'evidence').mkdir(parents=True)
    small = dump(node('android.widget.FrameLayout', bounds=(0, 0, 200, 400),
                      kids=node('android.widget.Button', rid='go', text='Go', clickable=True, bounds=(10, 10, 190, 120))))
    graph = {'package': PKG, 'activity': '.Main', 'device': {'model': 'fake'},
             'screens': [{'id': 'S01', 'activity': f'{PKG}/.Main', 'steps': [], 'signature': 'a'},
                         {'id': 'S02', 'activity': f'{PKG}/.Main', 'steps': [{'rid': 'go', 'label': 'Go'}], 'signature': 'b'}]}
    (run / 'screen_graph.json').write_text(json.dumps(graph), encoding='utf-8')
    (run / 'crashes.json').write_text('[]', encoding='utf-8')
    png = png_io.encode(png_io.blank(200, 400))

    class Dev(FakeDevice):
        die_on_tap = False

        def adb(self, *a, **k):
            if a[0] == 'exec-out' and 'screencap' in a[1]:
                return png
            if a[0] == 'shell' and a[1].startswith('input tap') and Dev.die_on_tap:
                raise KeyboardInterrupt  # the process is killed while reaching S02
            return super().adb(*a, **k)

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.screens = [small]
    real = rma.Device
    rma.Device = Dev
    argv = sys.argv
    try:
        Dev.die_on_tap = True
        sys.argv = ['run_mobile_audit.py', '--run', str(run), '--skip-probes', 'keyboard']
        rc = rma.main()
        st = json.loads((run / 'run.json').read_text(encoding='utf-8'))
        check(rc == 2 and st['run_status'] == 'partial' and [s['id'] for s in st['sample']] == ['S01'] and 'S02' in st['run_status_reason'],
              'a run killed on S02 keeps S01 in run.json, marked partial, naming what is left')
        check({c['screen'] for c in st['checks']} == {'S01'} and all(c['restored'] for c in st['settings_changed']),
              "only finished screens' checks are kept, and the settings were restored")
        Dev.die_on_tap = False
        sys.argv = ['run_mobile_audit.py', '--run', str(run), '--skip-probes', 'keyboard', '--resume']
        rc = rma.main()
        st = json.loads((run / 'run.json').read_text(encoding='utf-8'))
        check(rc == 0 and st['run_status'] == 'complete' and [s['id'] for s in st['sample']] == ['S01', 'S02'] and len(st['resumed']) == 1,
              '--resume audits only S02 and completes the run')
        check(sum(1 for c in st['checks'] if c['screen'] == 'S01' and c['name'] == 'text_scaling') == 1, 'S01 is not audited twice')
        check(adb_device.pending(run / 'device_changes.json') == [], 'the journal holds no unrestored change')
    finally:
        rma.Device = real
        sys.argv = argv


def test_validator(tmp: Path):
    print('Validator: partial runs')
    src = HERE.parent / 'examples'
    run = tmp / 'run'
    shutil.copytree(src / 'run', run, ignore=shutil.ignore_patterns('*.apk'))
    audit = json.loads((src / 'mobile_audit.example.json').read_text(encoding='utf-8'))
    r = json.loads((run / 'run.json').read_text(encoding='utf-8'))
    r.update(run_status='partial', run_status_reason='stopped by KeyboardInterrupt; 1 screen(s) not audited (S05): rerun with --resume')
    (run / 'run.json').write_text(json.dumps(r), encoding='utf-8')

    def validate(a):
        f = tmp / 'audit.json'
        f.write_text(json.dumps(a), encoding='utf-8')
        return subprocess.run([sys.executable, str(HERE / 'validate_mobile_audit.py'), str(f), '--run', str(run)], capture_output=True, text=True)
    out = validate(audit)
    check(out.returncode == 1 and "metadata.run_status 'complete' should be 'partial'" in out.stdout, 'metadata.run_status must equal run.json')
    audit['metadata']['run_status'] = 'partial'
    out = validate(audit)
    check(out.returncode == 1 and 'limitations do not say the audit is partial' in out.stdout, 'a partial run must be named in the limitations')
    audit['limitations'].append('The run is partial: S05 was not audited.')
    out = validate(audit)
    check(out.returncode == 0, 'partial run named in the limitations validates')
    (run / 'device_changes.json').write_text(json.dumps({'sessions': [{'changes': [{'kind': 'setting', 'ns': 'system', 'key': 'font_scale',
                                                                                    'before': '1.0', 'restored': False}]}]}), encoding='utf-8')
    out = validate(audit)
    check(out.returncode == 1 and 'device_changes.json: system font_scale' in out.stdout, 'an unrestored journal entry fails validation')


def main() -> int:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        test_anr_parsing()
        test_explorer_anr(tmp)
        test_launch_dialog(tmp)
        test_journal(tmp)
        test_destructive()
        test_text_and_keyboard()
        test_resume(tmp)
        test_audit_resume(tmp)
        test_validator(tmp)
    print(f'{len(FAILS)} failure(s)' if FAILS else 'all checks passed')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
