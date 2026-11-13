---
name: mobile-app-auditor
description: Audit a native mobile app while it runs on an emulator or test device - accessibility against WCAG 2.2 as applied to mobile (EN 301 549 clause 11), and crashes, ANRs and hangs found by guided exploration. Explores the installed build screen by screen, runs label, target-size and contrast checks on the accessibility hierarchy and screenshots, and probes font scaling, display size, orientation, hardware-keyboard traversal and traps, moving content and dark mode, restoring every device setting it changes. Every finding names the screen, the element, the device and a marked screenshot; every crash has its steps, a screenshot, an app-only log excerpt and a replay on a fresh install. Uses the WCAG auditor's outcomes, severities and verdict, and the UI crash hunter's crash tiers. Android through adb and uiautomator; iOS needs macOS and is otherwise not_run, never skipped silently.
version: 1.1.0
---

# Mobile app auditor

## Mission

Audit a native app (Kotlin, Java, Swift, React Native, Flutter - all audited as the native app they become)
from an installed build, without needing source:

- **Accessibility**: WCAG 2.2 A and AA as applied to mobile apps, mapped to EN 301 549 clause 11 (software).
- **Crashes and freezes**: native crashes, Android ANRs, iOS hangs, found while exploring.

The WCAG auditor does the same accessibility job for web pages; this skill uses its outcomes, failure
patterns, severity and priority rules and verdict, so the reports read the same. The UI crash hunter starts
from source; this skill starts from the running app and uses its B1/B2 tiers.

## Non-negotiable rules

1. **The WCAG auditor's honesty rules apply unchanged.** EARL outcomes per SC; `untested` without
   evidence; a tool pass never passes an SC; agent judgment is never high confidence; the verdict is only
   `does_not_conform` or `not_established`, and no text claims conformance.
2. **Every accessibility finding names** the screen, the element (resource id or accessibility identifier,
   class, bounds, label), the check, a screenshot with the element marked, and the device and OS version.
3. **Every crash has a log**: the crash buffer or ANR trace, from the app's process (ANR lines are the
   system's lines about the app), the steps that led to it, and a screenshot. Each is replayed on a fresh
   app state; only a replayed crash is `reproduced`.
4. **Exploration is reproducible**: guided steps are recorded; a random (monkey) run records its seed and
   event log. Random exploration is off unless asked for.
5. **Device settings are restored**: font scale, display size, rotation, dark mode and airplane mode are
   put back after each probe, and the report lists every change and its restoration. Each change is
   written to `<run>/device_changes.json` before it is made; if a run or the emulator dies, run
   `python scripts/adb_device.py restore --journal <run>/device_changes.json` before anything else (the
   scripts refuse to start while that journal holds unrestored changes).
6. **Test accounts and test backends only.** Controls whose visible name (their text, else their
   descendants' text, else the first sentence of their description) says buy, pay, delete, remove
   account, send, purchase, checkout, sign out or log out are never tapped; the screen graph lists each
   skipped control (`skipped_controls`) and the limitations name the screens left unexplored because of
   them. A screen that would reach a third-party or production backend is run with `--offline`
   (airplane mode, restored afterwards) unless the backend is a test one. A build pointed at production needs
   `authorised_by` confirming the account and backend are disposable. SMS or email codes and CAPTCHAs: stop
   and ask.
7. **No screen reader is simulated.** The skill reads the hierarchy TalkBack and VoiceOver use; what they
   announce and in which order goes in the manual test plan.
8. **iOS needs macOS with Xcode 15+.** Elsewhere every iOS check is `not_run` with that reason.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `app` | required | APK (or an installed package) for Android; `.app` or IPA for iOS |
| `activity` | the launcher activity | Where exploration starts |
| `devices` | the connected emulator | Record model, OS version, API level, density |
| `critical_paths` | optional | Named step lists (`{"rid"}` or `{"label"}` taps); findings on them go up one severity level |
| `exploration` | `guided` | `guided`, `random` (monkey, seeded), `both`, `off` |
| `budget` | 20 minutes, 60 actions, 25 screens | Per device |
| `wcag_version`, `level` | `2.2`, `AA` | |
| `authorised_by` | required unless the backend is local or absent | |

## Workflow

Android needs the SDK platform tools (adb) and an emulator or device; everything else is stdlib Python.

```bash
python scripts/check_tools.py --out <run>/tools.json
python scripts/explore.py --package <pkg> --activity .MainActivity --out <run> [--offline]
python scripts/run_mobile_audit.py --run <run> --apk app.apk [--budget-min 30] [--resume] [--offline]
python scripts/crash_explore.py --run <run> --replay [--monkey --seed 4242]
python scripts/merge_findings.py --run <run> --out <run>/candidates.json
python scripts/adb_device.py status --journal <run>/device_changes.json   # anything left changed?
python scripts/adb_device.py restore --journal <run>/device_changes.json  # put it back
```

`run_mobile_audit.py` rewrites `run.json` after every screen (`run_status: "partial"` until the last one), so
a run that is killed keeps every finished screen: put the device back with `restore`, then carry on with
`--resume`. A run stopped by `--budget-min`, or never resumed, stays `partial`; the audit then copies
`run_status` and its reason into `metadata` and says in the limitations that it is partial and which screens
were not audited. A `not_run` or `partial` exploration (`screen_graph.json` `run_status`) is named in the
limitations the same way.

1. **Set up.** Record host, tools and versions (`not_run` with a reason for anything missing: iOS off macOS,
   the ATF APK, Appium). Install the build; grant only the permissions the user lists.
2. **Explore.** Breadth-first from launch: each distinct screen (activity plus the set of control ids) is a
   node, each tap and one rotation per screen an edge. After every action, harmless input is sent and the
   crash buffer and ANR lines written since the action are read. Every screen found is sampled (up to the
   cap), with the reason. An app that shows no node of its own at launch (a system dialog that won't go
   away, a wrong activity) stops exploration with an error, never an empty graph.
3. **Check and probe** each sampled screen (see `RUBRIC.md` for the mapping): labels, target size (24 dp
   with the spacing exception; 48 dp as an advisory), text contrast from pixels, font scale 2.0, the largest
   display size, rotation, hardware-keyboard Tab traversal and traps (Tab, Escape and Back), content that
   changes by itself between 5 s and 7.5 s with no pause control, and dark mode. A probe that would repeat a
   recorded crash is `blocked` with the crash id.
4. **Replay** every crash twice after `pm clear`. An ANR record names the input that timed out
   (`input_event`); when it is the explorer's own SHIFT press (`probe_input: true`), the block was real
   but was exposed by synthetic input while the screen was building, and `trace.device_cpu_pressure_avg10`
   says how loaded the device was. Say both in the crash finding, and treat an ANR that only appears under
   heavy device load and does not replay as `observed`, not `reproduced`. `after_wait` is decided by the
   process: the same pid still showing the app is `recovered`.
5. **Merge** results per component (the same check on the same id across screens) and crashes by
   signature (exception or ANR reason plus top app frame).
6. **Triage** every candidate: `finding`, `needs_review`, `advisory`, `crash`, or `dismissed` with a
   specific reason (for example a small icon inside a full-width row: the row is the target). Look at each
   marked screenshot before promoting.
7. **Rate** with `RUBRIC.md`, fill `sc_results` for every target SC, write the manual test plan (screen
   reader order and announcements, gestures, switch access, reduced motion), and the limitations.
8. **Validate and render.**

```bash
python scripts/validate_mobile_audit.py <run>/mobile_audit.json --run <run> [--write-summary]
python scripts/render_report.py <run>/mobile_audit.json [--brief]
```

## Edge cases

- **Secure screens** (`FLAG_SECURE`) give black screenshots: contrast and motion are `untested` there.
- **WebViews**: if debuggable, hand the URL to the WCAG auditor; otherwise `untested`. A WebView that is
  slow to build can ANR under the explorer's input: raise `--input-delay` before calling it the app's.
- **Icon fonts**: text made only of private-use code points is an icon glyph, never measured as text
  contrast; it is a `glyph.icon-font` review candidate - a box or blank in the screenshot means the icon
  font is not loaded (a real defect only a look at the screenshot shows).
- **React Native merged containers** (`accessible={true}`): uiautomator still lists the inner views, so
  label checks can report inner, unlabelled views TalkBack never focuses. Dismiss those with that reason;
  the barrier to report is usually the merge itself (a whole form as one node, 4.1.2 / 1.3.1), from the
  screenshot and the tree.
- **Slow runs**: every probe reaches its screen again; budget about 1 to 2 minutes per screen. Use
  `--budget-min` and `--resume` rather than one long run.
- **Windows Git Bash**: adb paths such as `/sdcard/...` typed by hand are rewritten by MSYS; prefix the
  command with `MSYS_NO_PATHCONV=1` (the scripts themselves are not affected).
- **Flutter** exposes semantics only when an accessibility service runs; enable one before reading the tree.
- **Games and canvas screens**: an empty tree is `untested`, not a wall of missing labels.
- **Compose**: test tags appear as resource ids only with `testTagsAsResourceId` (also inside dialogs).
- **Obfuscated release builds**: crash frames need the mapping file; without it, name the screen and steps.
- **Physical devices**: probes that change settings ask first.

## Output fields

As the WCAG auditor's audit (schema 1.1.0): `scope`, `metadata` (with `run_status` and `run_status_reason`
from `run.json`), `headline`, `sc_results` (with `en301549_clause` from
clause 11), `findings` (instances use `screen`, `element`, `screenshot`, `device`), `advisories`,
`needs_review`, `triage`, `manual_test_plan`, `assisted_review`, `limitations`; plus `crashes` (tier, steps,
log excerpt, screenshot, replay, likelihood, recoverability, score, priority) and `screen_graph`.

## What the validator enforces

The WCAG auditor's rules (SC coverage and outcomes, severity, priority, verdict, language) on screens;
clause 11 numbering with the four web-only SCs inapplicable; tools and checks equal the run; no iOS run off
macOS; every setting restored (records and journal); `metadata.run_status` equal to the run's, with a
partial run or exploration named in the limitations; instances with screen, element, device and a screenshot whose SHA-256
matches; crashes with steps, screenshot and single-process log excerpts, replay records behind
`reproduced`, seeds behind random crashes, ANR tiers from what Wait did, and recomputed crash scores; every
candidate triaged once.
