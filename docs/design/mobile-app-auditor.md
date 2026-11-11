# Design: `mobile-app-auditor-skill`

Status: Built (v1.0.0); see "As built" at the end. Build order: 24.

## Mission

The skill audits a native mobile app while it runs on an emulator, simulator or test device. It
covers two things the web skills can't reach:

- **Accessibility**, against WCAG 2.2 as applied to mobile apps (W3C's WCAG2Mobile guidance) and
  EN 301 549 clause 11 (software).
- **Crashes and freezes**: native crashes, Android ANRs (app not responding), and iOS hangs and
  watchdog terminations, found by guided and random exploration.

It works from an installed build (APK, AAB or IPA, or a simulator `.app`) and doesn't need source.
If source is available, findings also point at files and lines.

How it differs from the existing skills:

- **wcag-auditor** does the same accessibility job for web pages. This skill reuses its
  outcomes, severity rubric, priority formula, verdict and SC data, so web and mobile reports read
  the same.
- **ui-crash-hunter** starts from source code and confirms crashes in the app; for mobile it
  leaves the flow to the repo's own e2e tool. This skill starts from the running app, explores it,
  and reports crashes with the crash hunter's B1/B2 tiers. If both run, the crash hunter's static
  candidates can be confirmed here.
- **React Native and Flutter apps** are audited as the native apps they become. Framework-specific
  hints (missing `accessibilityLabel`, `Semantics` widgets) are added when source is present.

## Decisions

- **Real tools, honestly limited.** Android works on any OS with the Android SDK and an emulator.
  iOS needs macOS with Xcode 15 or later. On other hosts, iOS checks are `not_run` with that
  reason, never skipped silently.
- **Android is driven with `adb` and `uiautomator` first.** Plain `adb` (taps, key events,
  settings, logs) and `uiautomator dump` for the hierarchy need nothing beyond the Android SDK.
  Appium is optional: when Appium 2 and its drivers are installed (they need Node.js), the runner
  can use UiAutomator2 instead, and iOS runs through the XCUITest driver, which exposes Apple's
  own `performAccessibilityAudit`. Without Appium, iOS checks are `not_run` with that reason.
- **Random (monkey) exploration is off by default.** It taps unpredictably, so it runs only when
  `exploration` is `random` or `both`.
- **Built-in engines first.** Android: Google's Accessibility Test Framework (ATF) checks, run
  over the view hierarchy and a screenshot of each screen. iOS: Xcode's accessibility audit. The
  skill adds its own probes on top, as the WCAG auditor does.
- **Test accounts and test backends only.** Exploration taps things. The skill refuses to explore
  a build pointed at production unless the user confirms in `authorised_by` that the account and
  backend are disposable. Payment and destructive actions are excluded by accessible-name
  patterns (`buy`, `pay`, `delete`, `remove account`, `send`).
- **No screen reader is simulated.** TalkBack and VoiceOver order and announcements are listed in
  the manual test plan. The skill reads the accessibility tree they use, which catches missing
  labels and roles, but it doesn't claim to know what a screen reader user hears.

## Non-negotiable rules

- **The WCAG auditor's honesty rules apply unchanged:** EARL outcomes, `untested` when there's no
  evidence, tool passes never pass an SC, `agent_judgment` never high confidence, the verdict is
  only `does_not_conform` or `not_established`.
- **Every accessibility finding names the screen, the element** (resource id or accessibility
  identifier, class, bounds), **the check, a screenshot with the element marked**, and the device
  and OS version.
- **Every crash has a log.** A crash finding needs the crash log or ANR trace (from `logcat -b
  crash`, `dumpsys dropbox`, or the simulator's `.ips` report), the steps that led to it, and a
  screenshot of the last screen. Symbolication is attempted; an unsymbolicated trace is still
  evidence but lowers confidence to `observed`.
- **Exploration is reproducible.** Every random run records its seed and event log, so the steps
  can be replayed.
- **Device settings are restored.** Font scale, display size, orientation, dark mode and
  animation settings changed by probes are put back after each run, and the report lists what was
  changed.
- **No personal data or secrets** in screenshots or logs: synthetic test accounts only, and log
  lines are filtered to the app's process before they're stored.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `app` | required | APK/AAB, IPA or `.app` path, or a package name or bundle id already installed |
| `platforms` | what the app supports and the host can run | `android`, `ios` |
| `devices` | one phone per platform | AVD names or simulator device types, with OS versions |
| `screens` | discovered | Named screens with steps to reach them (`adb` actions, Appium actions, or a Maestro flow) |
| `critical_paths` | optional | As in the WCAG auditor, or journey files |
| `exploration` | `guided` | `guided` (every reachable control on every screen, breadth-first, capped), `random` (monkey-style, seeded), `both`, or `off` |
| `budget` | 20 minutes per device | Time and event caps |
| `auth` | optional | Test account credentials from environment variables |
| `source` | optional | Repo path, for file and line hints |
| `wcag_version`, `level` | `2.2`, `AA` | As in the WCAG auditor |
| `standards_map` | `en301549` | Clause 11 mapping |
| `authorised_by` | required unless the backend is local | |

## Workflow

1. **Set up.** Check the tools (`adb`, emulator, `xcrun simctl`, and Appium with its drivers if
   installed) and record versions or `not_run`. Install the build, grant only the permissions the
   user lists, and start log capture filtered to the app.
2. **Explore and sample screens.** Guided exploration builds a screen graph: each distinct screen
   (by activity or view controller plus a signature of its accessibility tree) is a node, each tap
   an edge. The sample is every screen on a critical path plus one screen per distinct layout, up
   to `max_screens` (default 25), with the reason recorded, as in WCAG-EM.
3. **Engines on each sampled screen:**
   - Android: ATF checks on the UiAutomator2 hierarchy and screenshot (touch target size, missing
     labels, duplicate labels, contrast of text and images, clickable spans, redundant
     descriptions, traversal order loops).
   - iOS: `performAccessibilityAudit` for each audit type (contrast, element detection, hit
     region, Dynamic Type, text clipping, trait, label).
4. **Probes on each sampled screen:**

   | Probe | SCs | Method |
   |---|---|---|
   | Text scaling | 1.4.4 | Android font scale 2.0 (`settings put system font_scale`), iOS largest accessibility text size; look for clipped or ellipsized text and overlapping bounds |
   | Display size | 1.4.10 | Android largest display size; overlapping or off-screen controls |
   | Orientation | 1.3.4 | Rotate; record whether the app locks orientation with no essential reason |
   | Target size | 2.5.8 | Bounds against 24 x 24 dp or pt (WCAG2Mobile maps CSS px to dp and pt), plus the platform guidelines (48 dp, 44 pt) as advisories |
   | Labels and roles | 1.1.1, 4.1.2, 1.3.1 | Accessibility tree: clickable elements with no label, images with no description, headings not marked as headings |
   | Keyboard and switch access | 2.1.1, 2.1.2, 2.4.3, 2.4.7 | Hardware keyboard Tab and arrow traversal through `adb shell input keyevent` (Appium key events on iOS); traps and unreachable controls, as in the WCAG keyboard probe |
   | Contrast | 1.4.3, 1.4.11 | From screenshot pixels inside each text and control bound, reusing the WCAG auditor's PNG decoder |
   | Dark mode | 1.4.3 | Repeat contrast with the system dark theme |
   | Motion and timing | 2.2.1, 2.2.2, 2.3.3 | Screenshots 5 s and 7.5 s apart; animations with the system "remove animations" setting on |
   | Gestures | 2.5.1, 2.5.7 | Agent review of custom gesture handlers when source is present; otherwise manual plan |

5. **Crash exploration.** Guided exploration also records crashes. If `random` is on, run Android
   `monkey` (seeded, `--pct-syskeys 0`, throttled, restricted to the package) and, when Appium is
   available, an equivalent seeded random tap run on iOS. Collect crashes, ANRs (`ANR in` log lines and
   `dumpsys dropbox` entries), iOS hang and crash reports, and the event log leading to each.
6. **Replay.** Each crash is replayed from its recorded steps on a fresh install. Reproduced crashes
   get confidence `reproduced`.
7. **Normalise and merge.** Map engine and probe results to SCs through the skill's
   `data/rule-map.json`, merge per element and then per component (the same resource id or
   accessibility identifier across screens), as in the WCAG auditor.
8. **Assisted review, rating, SC matrix, manual test plan, validate and render.**

## Rating

**Accessibility:** exactly the WCAG auditor's model (failure-pattern default severity, critical
path one level up, non-interference SCs always Critical, priority = severity weight x reach x
leverage with the same bands, floors and caps). "Reach" counts screens instead of pages.

**Crashes:** the UI crash hunter's numeric ranking:

- blast radius B1 = 3, B2 = 2 (an ANR is B2 when the app recovers after "Wait", B1 when it's
  killed);
- reach: critical path 3, a screen in the sample 2, reached only by random exploration 1;
- likelihood: from normal use in guided exploration 4, from an unusual sequence 3, after a
  rotation, backgrounding or setting change 2, from random input only 1;
- recoverability: crash loop on relaunch 3, restart needed 2, retry fixes it 1.

P0 >= 48, P1 >= 24, P2 >= 8, otherwise P3, as in the crash hunter. A crash that wasn't replayed is
capped at P1.

## Output (`mobile_audit.schema.json`)

Same top-level sections as the WCAG auditor's `audit.json` (`scope`, `metadata`, `headline`,
`sc_results`, `findings`, `advisories`, `needs_review`, `triage`, `manual_test_plan`,
`assisted_review`, `limitations`, `files_written`), with these changes:

- `metadata` records host OS, devices with OS versions, app version and build, Appium and driver
  versions, ATF version, Xcode version, and settings changed and restored.
- `findings[].instances[]` use `screen`, `element` (id, class or type, bounds, label) and
  `screenshot` instead of URL and selector.
- `sc_results[].en301549_clause` uses clause 11.
- A separate `crashes` section with crash-hunter tiers, log excerpts (app process only), steps,
  seed, replay result and score.
- `screen_graph` with screens, how each was reached, and which were sampled.

## Validator fails when

- Any of the WCAG auditor's validator rules fails (the shared rules are copied, not imported).
- An iOS check is `ran` on a non-macOS host, or `run.json` has no Xcode version for it.
- A crash has no log excerpt, steps or screenshot, or a `reproduced` crash has no replay record.
- A random-exploration crash has no seed.
- A changed device setting isn't listed as restored.
- A screenshot or log is referenced that isn't in the run directory.
- Log excerpts contain lines from other processes.

## Edge cases

- **Secure screens** (`FLAG_SECURE`, password fields) block screenshots: contrast and visual
  probes are `untested` there, with the reason.
- **WebViews** inside the app: the hierarchy shows little. If the WebView is debuggable, the skill
  hands its URL to the WCAG auditor's engines through Chrome DevTools or Safari Web Inspector;
  otherwise it's `untested`.
- **Flutter** renders its own widgets; the accessibility tree only exists when semantics are on,
  which the OS turns on when an accessibility service runs. The skill enables a service (or the
  `--enable-semantics` debug flag on debug builds) before reading the tree.
- **Games and canvas-drawn screens:** the tree is empty; reported as `untested` rather than as a
  wall of missing-label failures.
- **Login with SMS or email codes, and CAPTCHAs:** stop and ask; never bypass.
- **Obfuscated release builds:** crash traces need the mapping file (`mapping.txt`, dSYM) to point
  at code; without it, findings name the screen and steps only.
- **Physical devices:** supported through `adb` and a provisioned iOS device, but settings
  changes are riskier, so probes that change settings ask first.

## Tool stack

| Tool | Licence | Role |
|---|---|---|
| Android SDK platform-tools and emulator (`adb`, `monkey`, `uiautomator`) | Android SDK licence (free) | The default Android driver: devices, input, logs, hierarchy, settings |
| Appium 2 server, UiAutomator2 and XCUITest drivers | Apache-2.0 | Optional: needed for iOS, and an alternative Android driver (Node.js, installed by the user) |
| Appium Python client | Apache-2.0 | Optional, declared in `requirements.txt`; used only when Appium is present |
| Accessibility Test Framework for Android | Apache-2.0 | Android checks, run in a small instrumentation APK the skill builds once and pins |
| Xcode 15+ (`xcrun simctl`, XCTest `performAccessibilityAudit`) | Apple Xcode licence (free) | iOS simulators, audit, crash reports; macOS only |
| Maestro | Apache-2.0 | Optional: screens and critical paths given as Maestro flows |
| `eslint-plugin-react-native-a11y`, Flutter `meetsGuideline` tests | MIT, BSD-3-Clause | Optional source hints for React Native and Flutter |

Data shared with the WCAG auditor (`wcag-sc.json`, `en301549-map.json`) is copied into this skill
and added to `tools/check_shared.py`, so it can't drift.

## Files

```text
mobile-app-auditor-skill/
  SKILL.md  RUBRIC.md  README.md  requirements.txt   # Appium-Python-Client (optional; Android runs without it)
  data/wcag-sc.json  en301549-map.json   # shared copies from the WCAG auditor
  data/rule-map.json                     # ATF checks, iOS audit types, probe codes -> SC
  data/tools.lock.json                   # Appium, drivers, ATF APK with hashes
  schemas/mobile_audit.schema.json
  scripts/check_tools.py      # host, SDKs, Appium (optional), devices -> statuses
          explore.py          # guided exploration, screen graph, sampling
          run_mobile_audit.py # engines and probes per sampled screen -> run/
          crash_explore.py    # monkey / random taps, log collection, replay
          normalize.py  merge_findings.py  validate_mobile_audit.py  render_report.py
  examples/
```

## Example

`examples/harbour-dental-app/` is a small native Android app (Kotlin, Jetpack Compose) for the same
fictional clinic as the WCAG auditor's example, so the two reports can be compared. A prebuilt
debug APK is committed with its hash. The answer key lists:

- an icon-only "Book" button with no content description;
- a 32 dp close button;
- body text that clips at font scale 2.0;
- the app locked to portrait;
- grey-on-white hint text below 4.5:1;
- a date picker that traps hardware-keyboard focus;
- an auto-advancing banner with no pause;
- a crash when rotating the booking screen with the date picker open (B2);
- an ANR when opening the clinic map.

Decoys for triage: a decorative image correctly marked unimportant, and a list row whose small
icon sits inside a full-width tappable row. `examples/run/` is a real Android emulator run. The
iOS half is `not_run` unless the example is regenerated on macOS, and the report says so.

## Resolved questions

Agreed with the user before the build.

1. **Example's iOS half `not_run` without a Mac?** Yes.
2. **Example app?** Kotlin and Compose.
3. **Driver?** `adb` and `uiautomator` first; Appium optional.
4. **Random (monkey) exploration?** Off by default.

## As built (v1.0.0)

Where the build differs from the design above, and what the example showed.

- **Android only, through `adb` and `uiautomator`.** Appium is detected but not used; iOS is `not_run` off
  macOS. Google's ATF is not run (its instrumentation APK isn't built in this version): the skill's own
  checks cover labels, target size and contrast, and `tools.json` says so. There is no
  `data/tools.lock.json` and no separate `normalize.py`: `merge_findings.py` maps results through
  `data/rule-map.json` and merges them.
- **Settings are tracked and restored by `adb_device.py`.** A setting changed again after a restore is
  reopened, so it is restored again. Records are written only after the restore, so they say what the
  device was actually left with. The first example run found both bugs: auto-rotate was left off while the
  records said it had been restored.
- **Text scaling also catches clipping:** Android 14+ scales large text non-linearly, so a text that grew
  1.33x can still be cut off. A text reaching the bottom of a parent that kept its height is a failure,
  as long as the median text grew 1.5x.
- **Exploration records ANRs with what Wait did** (`recovered` or `killed`). It replays crashes after
  `pm clear`. A probe that would repeat a recorded crash is `blocked`.
- **The example APK is a shrunk release build** (1.3 MB) rather than a debug build (11.6 MB). It isn't
  obfuscated, but R8 rewrites line numbers, so crash frames need the mapping file to match the source.
- **Example results:** 4 screens, 36 checks (4 blocked or errored), 10 accessibility and 2 crash
  candidates, 7 findings (1 P0, 4 P1, 1 P2, 1 P3), one advisory, one needs-review item. There are two crash
  findings: the map ANR is P0, because the app was killed once after Wait, making it B1; the rotation crash
  is P1, also B1, since the process dies. The planted 20 dp targets didn't fail 2.5.8: Compose gave them
  24.4 dp and 48 dp touch areas, leaving a 48 dp advisory. Both decoys raised no candidate.
- **Testing:** 38 of 38 seeded violations are caught, including unrestored settings, a crash log that mixes
  processes, and a replay that didn't reproduce.
