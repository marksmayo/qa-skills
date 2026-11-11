# Mobile App Auditor Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It audits a native mobile app while it runs on an emulator or test device, from the installed build:

- **Accessibility** against WCAG 2.2 A and AA as applied to mobile apps, with EN 301 549 clause 11
  references: unnamed controls, small targets, low contrast (also in dark mode), text that doesn't grow
  with the font scale, content lost at the largest display size, orientation locks, hardware-keyboard
  traps and unreachable controls, and content that moves by itself.
- **Crashes and ANRs**, found by guided exploration of every screen (taps and a rotation per screen), each
  with its steps, a screenshot, a log excerpt from the app's process, and a replay on a fresh app state.

The report reads like the WCAG auditor's (the same outcomes, severities, priorities and verdict) with the
UI crash hunter's crash tiers. It never claims conformance, restores every device setting it changes,
never taps buy, pay, delete or sign-out controls, and says plainly what it couldn't run (iOS off macOS).

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g. `.claude/skills/mobile-app-auditor/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/mobile-app-auditor/`, or include
`SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/mobile-app-auditor/`, and reference
`SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+ and, for Android, the
SDK platform tools (`adb`) with an emulator or a device. iOS needs macOS with Xcode 15+ (not built in this
version; reported as `not_run`).

## Usage

> Audit our Android app's accessibility and look for crashes. The debug build is app/build/outputs/apk/debug/app-debug.apk; it talks to the test backend.

```bash
python scripts/check_tools.py --out run/tools.json
adb install -r app-debug.apk
python scripts/explore.py --package com.example.app --activity .MainActivity --out run
python scripts/run_mobile_audit.py --run run --apk app-debug.apk
python scripts/crash_explore.py --run run --replay
python scripts/merge_findings.py --run run --out run/candidates.json
python scripts/validate_mobile_audit.py run/mobile_audit.json --run run
python scripts/render_report.py run/mobile_audit.json [--brief]
```

## Example

`examples/harbour-dental-app/` is a small Kotlin and Jetpack Compose app for the fictional clinic of the
WCAG auditor's example, with planted barriers, a crash and an ANR; `examples/run/` is a real run on an
Android 16 emulator (listed in `examples/run/commands.sh`), with the APK it tested, screenshots and logs;
`examples/mobile_audit.example.json` is the audit. `examples/README.md` has the answer key.

```bash
python scripts/validate_mobile_audit.py examples/mobile_audit.example.json --run examples/run
python scripts/render_report.py examples/mobile_audit.example.json
```

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: outcomes, severity, priority, checks and probes, crash ranking
- `schemas/mobile_audit.schema.json`: output contract
- `data/rule-map.json`: check and probe codes to SCs and failure patterns
- `data/wcag-sc.json`, `data/en301549-map.json`: shared with the WCAG auditor (edit them in `shared/`)
- `scripts/adb_device.py`: adb input, hierarchy, screenshots, settings with restore, logs
- `scripts/check_tools.py`: host, tools and devices
- `scripts/explore.py`: guided exploration, screen graph, crashes and ANRs
- `scripts/run_mobile_audit.py`: checks and probes per screen
- `scripts/crash_explore.py`: crash replay and the seeded monkey
- `scripts/merge_findings.py`: candidates per component and crash signature
- `scripts/validate_mobile_audit.py`, `scripts/render_report.py`
- `scripts/png_io.py`: shared copy (edit it in `shared/`)
- `examples/`: the app, the run and the audit

## Design principles

1. Audit the running app the user gets, not the source.
2. Every finding names the screen, the element and the device, with a marked screenshot.
3. Every crash has steps, a log and a replay.
4. Put the device back as you found it; say what wasn't run.
