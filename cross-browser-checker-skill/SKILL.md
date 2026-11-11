---
name: cross-browser-checker
description: Run the same pages, states and journeys in Playwright's Chromium, Firefox and WebKit, three times each, and report where they behave differently - script errors, failing steps, content that isn't rendered, missing elements, input fallbacks, layout and network differences - keeping only differences that repeat in every run and listing the rest as flaky. With a repository, also scan CSS, JavaScript and HTML for features the project's browserslist targets don't all support (web-features and caniuse data, pinned and hash-checked), and link them to the runtime differences they explain. Never says "compatible", and never treats WebKit as Safari.
version: 1.0.0
---

# Cross-browser checker

## Mission

Compare one version of a site across engines and report the differences that matter, each with
evidence from both sides. Separate real engine differences from flaky behaviour and from rendering
noise (font metrics, native controls). Explain differences with the features the project's targets
don't support, and say clearly what wasn't tested.

The UI crash hunter finds crashes in one browser; the visual regression reviewer compares a page with
its earlier self; this skill compares engines with each other. A behaviour every engine shares is not a
cross-browser finding, even if it's a bug: note it for the crash hunter.

## Non-negotiable rules

1. **Never "compatible".** The verdict is `differences_found` or `no_differences_detected`, the second
   scoped to these pages, states and engines. Real Safari, older versions and real devices are
   `untested` unless the user ran them.
2. **WebKit is not Safari.** Playwright's WebKit lacks Apple's media, font and platform layers (on
   Windows and Linux it is a different build again). Never call a WebKit result a Safari result;
   `out_of_scope` always lists Safari and real devices.
3. **A difference must repeat.** Each check runs 3 times per engine. A difference counts only when it
   holds in every run of the affected engine and in no run of the reference (Chromium). Anything in
   between is `flaky`, listed with its run counts.
4. **Evidence from both sides**: screenshots and messages from the reference and every affected engine.
5. **Static feature findings are `possible`** until an engine shows the difference; then they are
   `confirmed_at_runtime`, linked to it.
6. **Safe targets.** Loopback needs nothing; other hosts need `authorised_by`. Non-GET requests are
   blocked unless `allow_form_submission`.
7. **Record every engine and data file** as `ran`, `not_run` or `errored`, with versions and hashes.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `targets` | required, except static only | `targets.json`: site, pages, states (actions, `critical_path`), viewports |
| `engines` | all three | Missing ones are `not_run` |
| `browserslist` | from the repo, else `defaults` | `package.json` or `.browserslistrc` |
| `repo` | none | Turns on the static feature scan |
| `static_only` | false | Feature scan without browsers; every finding stays `possible` |
| `reruns` | 3 | Per engine and check |
| `authorised_by`, `auth`, `allow_form_submission` | as in `browser_harness.py` | |

## Workflow

`capture.py` needs Playwright (`pip install -r requirements.txt`, `python -m playwright install
chromium firefox webkit`; ask first). The rest is stdlib.

### 1. Data

```bash
python scripts/fetch_data.py        # caniuse-db and web-features, checked against npm integrity and SHA-256
```

### 2. Capture

```bash
python scripts/capture.py targets.json --out <run>/capture
```

Per page, state, viewport and engine, 3 fresh runs: a full-page screenshot, console and page errors,
failed requests, visible elements with boxes and input types, images with their natural size,
horizontal overflow, the scripts loaded (to spot user-agent sniffing), and each step's outcome.

### 3. Static feature scan (with a repo)

```bash
python scripts/browserslist_lite.py --repo <repo>          # see what the targets resolve to
python scripts/feature_scan.py --repo <repo> --out <run>/features.json
```

Unresolvable browserslist terms (Firefox ESR, `and`, regional usage) are listed, and the scan can't
assess them.

### 4. Compare engines

```bash
python scripts/compare_engines.py <run>/capture/runs.json --features <run>/features.json --out <run>/candidates.json
```

Differences: page and console errors, failed steps, failed requests and undecoded images, missing or
extra elements, input type fallbacks, box differences, overflow, and content not rendered (an element
with under 40% of the reference's ink). Font metrics, native form controls and consequences of other
differences (a box that grew because something inside it changed) are dismissed automatically and
counted, with reasons in `candidates.json`. Ratings follow `RUBRIC.md`.

### 5. Triage and write

Every candidate is promoted, `needs_review` (for example media that may play in branded browsers with
proprietary codecs), or dismissed with a checkable reason. For each difference write a title naming the
engines (never "Safari"), a remediation that addresses the cause (a feature check, a fallback, an
`@supports` block), and a verification step (rerun capture for that page and name what should pass).
When a site serves different scripts per engine (compare `scripts` across engines), say so for every
difference on that page.

### 6. Validate and render

```bash
python scripts/validate_report.py <run>/report.json --runs <run>/capture/runs.json --candidates <run>/candidates.json \
  --features <run>/features.json --targets targets.json [--write-summary]
python scripts/render_report.py <run>/report.json [--brief]
```

Commercial device clouds (BrowserStack, Sauce Labs) aren't integrated; with an account, run the same
targets there and compare by hand.

## Output fields

- `scope`: site, pages, viewports, engines with versions, reference engine, resolved browserslist,
  `out_of_scope`, reruns, `authorised_by`.
- `metadata`: tools, data versions and hashes, runs file.
- `headline`: verdict, summary, severity counts, flaky count, checks planned and run, evidence
  completeness, overall confidence.
- `differences[]`: kind and subkind, engines affected, reference, page and states, element, severity,
  confidence, priority, score, usage share, runs per state and engine, evidence per engine, linked
  feature cause, remediation and verification step.
- `feature_findings[]`, `flaky[]`, `dismissed_automatically`, `needs_review[]`, `triage[]`, `limitations[]`.

## What the validator enforces

Candidates, ratings, flaky list and feature links are recomputed from the recorded runs. Every
difference is consistent across its reruns, differs from the reference, and has screenshots (matching
their hashes) from both sides. Safari is never an engine or a title. No compatibility claims; no
"no differences" while an engine didn't run. Every candidate is triaged once.
