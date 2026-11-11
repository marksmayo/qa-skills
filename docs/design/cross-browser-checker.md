# Design: `cross-browser-checker-skill`

Status: Built (v1.0.0). Build order: 19.

## Mission

The skill runs the same pages, states and journeys in Chromium, Firefox and WebKit, and reports
where they behave differently: script errors in one engine only, broken layout, features the
target browsers don't support, and journey steps that fail in one engine. It reruns every
difference so that flaky behaviour isn't reported as a browser difference.

It also checks the code (when available) against the project's browser targets: CSS and
JavaScript features used that aren't supported in every target, using browserslist queries and
feature data.

How it differs from the existing skills:

- **ui-crash-hunter** finds crashes in one browser. This skill compares engines with each other.
- **visual-regression-reviewer** compares a page with an earlier version of itself. This skill
  compares one version across engines, and reuses its diffing and grouping.
- **journey-to-tests** writes journey tests. This skill can run those tests on all three engines
  (via the shared `run_tests.py`) and compare outcomes.

## Decisions

- **Engines are Playwright's Chromium, Firefox and WebKit.** Playwright's WebKit is not Safari:
  it's the open-source engine built by Playwright, on Linux and Windows without Apple's media,
  font and platform layers. The report says this in its scope, and a WebKit pass never counts
  as a Safari pass. Branded Chrome and Edge channels are used when installed.
- **Static feature checks use open data:** caniuse-lite data (CC-BY-4.0) and
  `baseline-browser-mapping` (Apache-2.0) for the Web Platform Baseline status, plus the
  project's browserslist config. These are data files, read with stdlib Python, pinned and
  hash-checked like the WCAG engines.
- **No cloud device farms.** BrowserStack, Sauce Labs and similar need commercial accounts. The
  README says how to run the same config against them if the user has one, but v1 doesn't
  integrate them.
- **Comparison is relative.** A behaviour present in all three engines isn't a cross-browser
  finding, even if it's a bug; the report notes it for ui-crash-hunter.

## Non-negotiable rules

- **Never "compatible".** The verdict is `differences_found` or `no_differences_detected`, the
  second always scoped to "these pages, states and engines". Real Safari, older versions and
  real devices are `untested` unless the user ran them.
- **A difference must repeat.** Each check runs 3 times per engine. A difference counts only
  when it appears in every run of one engine and no run of another. Anything else is `flaky`,
  listed separately with its run counts.
- **Every difference has evidence from both sides:** screenshots, console logs, the failing
  step, the DOM boxes, per engine and version.
- **Static feature findings are `possible` until seen at runtime.** "Uses `:has()`" is not a
  failure on its own; it becomes one only when a target lacks support and there's no fallback,
  and it's confirmed only when an engine actually renders it wrong.
- **Tools and engines are recorded** as `ran`, `not_run` or `errored`, with versions.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `targets` | required, except in `static_only` | URLs, states, viewports (WCAG auditor config format) |
| `engines` | all three | Subset allowed; missing ones are `not_run` |
| `journeys` | optional | Journey Markdown files or an existing Playwright test command |
| `browserslist` | from repo, else `defaults` | Query or config path |
| `repo` | optional | Turns on static feature checks |
| `static_only` | false | Lighter mode: the static feature scan of `repo` with no live site and no browsers; every finding stays `possible` |
| `reruns` | 3 | Per engine and check |
| `authorised_by`, `auth`, `allow_form_submission` | as in `browser_harness.py` | |

## Workflow

1. **Scope.** Targets, engines with versions, browserslist query resolved to concrete browser
   versions, and what is out of scope (Safari, devices).
2. **Check tooling.** Installed engines, data file versions and hashes.
3. **Runtime capture** per page x state x viewport x engine, 3 times: screenshot, console
   messages and page errors, failed requests, DOM boxes of visible elements, computed styles of
   a sample, and the outcome of each scripted step.
4. **Compare engines** pairwise against Chromium (the reference, configurable):
   - **errors:** a page error or console error in one engine only;
   - **layout:** elements whose box differs by more than 4 px or 5%, overflow in one engine,
     elements hidden in one engine (from boxes, then the visual reviewer's diff to confirm);
   - **behaviour:** a journey step or test passes in one engine and fails in another;
   - **network:** requests that fail in one engine only (for example a blocked format).
5. **Static feature scan** (with `repo`): parse CSS (properties, values, at-rules, selectors) and
   JavaScript (syntax and well-known APIs) with stdlib tokenisers, look each feature up in the
   data, and list unsupported features per target with fallback detection (`@supports`, feature
   checks in code, vendor prefixes).
6. **Link** static findings to runtime differences where the element or file matches.
7. **Candidates and triage.** Each candidate is promoted, `flaky`, `needs_review` or dismissed with
   a reason (for example a 2 px font metric difference).
8. **Output, validate and render.**

## Rating

- **Severity:** Critical when a journey step fails or a page error breaks the page in one engine;
  High when content is hidden, overlapped or unusable; Medium for visible layout breakage that
  leaves the page usable; Low for cosmetic differences.
- **Confidence:** `high` when the difference repeated in all reruns and is explained by a
  feature-support gap or an engine-specific error message; `medium` when it repeated but isn't
  explained; `low` for static-only findings.
- **Priority:** severity weight (4/3/2/1) x share of target users on the affected engine from
  browserslist usage data (1 under 5%, 2 for 5–20%, 3 over 20%) x reach (1, 2, 3 by pages).
  P0 >= 24, P1 >= 12, P2 >= 6. A failing critical-path step is at least P1.

## Output (`cross_browser_report.schema.json`)

```text
scope:     targets, engines[{name, version, channel}], reference_engine, browserslist{query,
           resolved[]}, out_of_scope (always includes "Safari", "real devices")
metadata:  tools[{name, version, sha256?, status}], data{caniuse_lite, baseline_mapping}, reruns
headline:  verdict: differences_found|no_differences_detected, severity_counts, flaky_count,
           checks{planned, ran}, evidence_completeness, overall_confidence
differences[]: {id, kind: error|layout|behaviour|network, engines_affected[], pages[], severity,
                confidence, priority, runs{engine: [outcomes]}, evidence[{engine, screenshot,
                console, step}], cause?{feature, support, fallback_found}, remediation, verification_step}
feature_findings[]: {feature, files[{path, line}], unsupported_in[], baseline_status, fallback,
                     status: possible|confirmed_at_runtime}
flaky[], needs_review[], triage[], limitations[]
```

## Validator fails when

- A difference doesn't have consistent outcomes across all its reruns, or lacks evidence from
  both engines.
- A WebKit result is described as Safari, or Safari appears in `engines`.
- The verdict is `no_differences_detected` while any engine planned is `not_run`, or the text
  says "compatible", "works in all browsers" or similar.
- A static feature finding is `confirmed_at_runtime` with no linked difference.
- A candidate is missing from `triage`.

## Edge cases

- **User-agent sniffing:** sites that serve different code per engine are detected by comparing
  the scripts loaded, and reported as context for every difference on that page.
- **Fonts:** engines render the same font with different metrics. Differences under 4 px or
  confined to text width are dismissed as font metrics, with the measurement.
- **Codecs and DRM:** media that fails in Playwright's WebKit or Chromium (no proprietary
  codecs) is `needs_review`, because branded browsers may play it.
- **Mobile web:** device emulation per engine (Playwright's device descriptors). Emulation isn't
  a phone: touch, iOS Safari's viewport and real performance are `untested`.

## Files

```text
cross-browser-checker-skill/
  SKILL.md  RUBRIC.md  README.md  requirements.txt (playwright)
  data/data.lock.json          # caniuse-lite and baseline-browser-mapping versions, URLs, SHA-256
       feature-patterns.json   # CSS/JS syntax and API -> caniuse/BCD feature id
  schemas/cross_browser_report.schema.json
  scripts/fetch_data.py         # download, verify, cache feature data
          capture.py            # per-engine reruns; uses browser_harness.py
          browser_harness.py  run_tests.py  parse_results.py   # shared copies
          compare_engines.py  feature_scan.py  browserslist_lite.py
          validate_report.py  render_report.py
  examples/
```

`browserslist_lite.py` resolves the common query forms (`defaults`, `last N versions`, `> N%`,
`not dead`, named browsers and versions). Queries it can't resolve are reported, and the
feature scan for them is `not_assessed`.

## Example

`examples/travel-site/` is a static site on loopback with planted engine differences and an
answer key: a date input styled with a Chromium-only pseudo-element that hides it in Firefox, a
script that calls an API one engine lacks, with no feature check, so it throws there, a `:has()` layout with no
fallback for an older target in browserslist, a flex `gap` difference, a WebP-only image that
WebKit on Windows doesn't render, and a deliberately flaky timer to show flaky handling.

## Resolved questions

Agreed with the user before the build.

1. **Reference engine?** Chromium.
2. **Commercial device cloud?** No: open-source only.
3. **Code-only static feature scan?** Yes, as a lighter mode.

## As built (v1.0.0)

Where the build differs from the design above, and what the example showed.

- **Data:** caniuse-db 1.0.30001818 (plain `data.json`, used for browser versions and usage when
  resolving browserslist) and web-features 3.44.0 (per-feature Baseline status and first supporting
  version, used for support), both pinned with npm integrity and SHA-256. caniuse-lite's packed
  format and `baseline-browser-mapping` weren't needed: web-features covers APIs caniuse lacks, such
  as User-Agent Client Hints.
- **The planted WebP difference didn't exist.** Playwright's WebKit on Windows decodes WebP (480x200 in
  every run), so the WebP image stays in the example as a checked non-difference. A real WebKit
  difference turned up instead: no date control, so `<input type="date">` falls back to a text field.
  `input_type_fallback` was added for it.
- **Content not rendered is measured by ink**: the share of an element's pixels far from its most
  common colour, compared with the reference engine's. It is robust to font differences, which a
  pixel diff across engines is not.
- **Automatic dismissals.** The first comparison produced 18 candidates, most of them font metrics,
  native control sizes and knock-on box changes. Rules for those (font metrics, native controls, a
  box that changed because something inside it did, a box resized with its container, an element
  moved by a box above, an input whose type fell back, an element inside a missing one) brought it to
  the five real differences, and every dismissal is recorded with its reason.
- **States of one page merge** into one candidate; reach counts pages.
- **`png_io.py` moved to `shared/`** (used here and by the visual regression reviewer).
- **The example server refuses a busy port**, like the other example servers.
- **Not built:** journey Markdown and Playwright test runs per engine through `run_tests.py`
  (scripted states cover journeys here), branded Chrome and Edge channels, device emulation, and
  user-agent sniffing detection beyond recording the scripts each engine loaded.
- **Example results** (Chromium 158, Firefox 159, WebKit 27.2, 3 runs each, 36 runs): 5 differences
  on the home page (two Critical, from one client-hints call), the deals banner flaky, 42 automatic
  dismissals, 4 feature findings (2 confirmed at runtime, 2 possible).
- **Testing:** 17 of 17 seeded report violations are caught.
