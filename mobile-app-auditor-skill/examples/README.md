# Example: harbour-dental-app

`harbour-dental-app/` is a small Android app (Kotlin, Jetpack Compose) for the fictional Harbour Dental
clinic, the same clinic as the WCAG auditor's sample site, with planted barriers, a crash and an ANR. It
has no backend. Three activities: the home screen with the clinic list and a "+" button, the booking
screen with a day picker dialog, and a map screen.

`run/` is the real run, listed in `run/commands.sh`, on an Android 16 emulator (AVD with the android-36
google_apis x86_64 image, Pixel 7 profile, 1080 x 2400 at 420 dpi):

- `app-release.apk`: the build tested (release, minified, not obfuscated, signed with the debug key); its
  SHA-256 is in `run.json`.
- `tools.json`, `screen_graph.json`, `events.json`, `crashes.json`: setup and guided exploration (4 screens,
  12 edges, 13 actions), with screenshots and hierarchies in `screens/` and `crashes/`.
- `run.json` and `evidence/`: 36 checks and probes, 4 blocked or errored, with marked screenshots.
- `replays.json`: every crash replayed twice after `pm clear` (all reproduced 2/2).
- `candidates.json`: 10 accessibility candidates and 2 crash candidates.

`mobile_audit.example.json` is the audit.

## Answer key

| Planted | Expected | Got |
|---|---|---|
| Icon-only "+" button (`book_fab`) with no content description | 1.1.1 and 4.1.2 | F-1 (P1) and F-2 (P1), both Critical on the Book a visit path |
| Grey #AAAAAA hint text on white (`booking_hint`) | 1.4.3 | F-3, 2.32:1 (P3); the app ignores the dark theme, so the dark-mode check (M004) is the same finding |
| `intro_box` fixed at 40 dp around one line of instructions | 1.4.4 at font scale 2.0 | F-4 (P1): the text reaches the bottom of a box that kept its 84 px height |
| `android:screenOrientation="portrait"` on the home screen | 1.3.4 | F-5 (P2) |
| The day picker dialog consumes Tab, Escape, Back and Down, and ignores Back | 2.1.2 | F-6 (P0): blocks the Book a visit path |
| Promotion banner advancing every 3 s with no pause | 2.2.2 | F-7 (P1) |
| 20 dp Help and Close icons side by side | 2.5.8 | Not a failure: Compose made the targets 24.4 x 48 dp and 48 x 48 dp, so only the 48 dp advisory A-1 is raised for Help |
| Crash on rotating the booking screen with the picker open (`getString("draft_day")!!`) | crash | X-2 (P1): B1, the process dies; reproduced 2/2 |
| `Thread.sleep(12_000)` in `MapActivity.onCreate` | ANR | X-1 (P0): three occurrences (both clinic rows, and rotating the map) with one signature; the app was killed after Wait once, so B1 |
| Decoy: decorative Canvas hero art with no semantics | no candidate | none raised; noted in `assisted_review` |
| Decoy: 16 dp chevrons inside full-width clickable clinic rows | no candidate | none raised (the 200 px row is the target) |

Headings can't be read from uiautomator, so 1.3.1 is `cantTell` with NR-1. On the map screen the motion
and dark-mode probes errored: uiautomator couldn't dump the screen while the ANR froze it. Rotation was
blocked on the map and the picker, where it crashes or freezes the app. Evidence completeness is
therefore 0.89 and overall confidence medium; iOS is `not_run`. The verdict is `does_not_conform`.

```bash
python scripts/validate_mobile_audit.py examples/mobile_audit.example.json --run examples/run
python scripts/render_report.py examples/mobile_audit.example.json
```

Rerunning `run/commands.sh` needs an emulator. A rerun changes the timestamps, screenshots and hashes, so
the audit has to be rebuilt afterwards. The banner's text depends on when the screenshots are taken.
