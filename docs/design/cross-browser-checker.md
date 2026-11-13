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

## As built (v1.1.0)

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
- **Found on a real static site, fixed:**
  - *States couldn't be limited to some viewports*, so a mobile-menu state ran (and was counted as
    planned) at desktop width. Pages and states take `viewports`; `compare_engines.planned_checks`
    expands targets for capture, and `runs.json` records the plan.
  - *The planned count ignored viewports without `--targets`.* The validator now counts from the
    recorded plan, from `--targets` (which must agree), or from the page and viewport pairs that ran.
  - *`compare_engines.py` took 13.5 minutes* (811 s) on 108 tall screenshots (up to 390x26,579),
    and the validator reruns it. Profiling showed PNG decoding in `png_io.py` at about three quarters
    of the time and the per-pixel ink count at the rest. Ink now counts colours a row at a time in C
    (identical results, ties included), each screenshot is decoded once in a worker process
    (`--jobs`, default 4), and screenshots of pages the reference didn't capture are skipped. The
    example's candidates are unchanged. A faster shared decoder (big-integer Up filter, slice-based
    alpha stripping) would roughly halve the rest; it belongs in `shared/scripts/png_io.py`.
  - *Full-page capture didn't scroll*, so Firefox and WebKit drew `content-visibility: auto`
    sections and `loading=lazy` images below the fold blank: hundreds of false "content not
    rendered" candidates. Capture now scrolls through the page, waits for lazy images, and sets
    `content-visibility: visible` for the screenshot. On a synthetic 12,000 px page Firefox went from
    10 false candidates to none.
  - *Cumulative drift* (elements 17-22 px lower after many small text-metric differences) made
    separate box candidates. An element that moved with its nearest neighbour above, within 16 px,
    is now dismissed as cumulative drift; a larger step stays a difference.
  - *The Safari check split sentences at the "." in "16.4"* and flagged an honest sentence; it now
    splits at a full stop followed by a space.
  - *Feature patterns* gained `content-visibility`, `contain-intrinsic-size` and
    `requestIdleCallback` (none in WebKit), all keyed to web-features ids already in the pinned data.
  - *AVIF in WebKit on Windows*: 27 differences came from a Playwright build that decodes no AVIF,
    which says nothing about Safari 16.4 and later. `data/engine-limitations.json` lists known build
    gaps; matching candidates carry `build_limitations`, get low confidence and go to review.
    `runs.json` now records the platform.
  - *Third parties.* The shared harness blocks third-party requests by default; capture takes
    `third_party`, `allowed_hosts` and `service_workers`, keeps the console errors and failed
    requests the blocking causes out of the runs, and the report carries the blocked hosts as
    `scope.third_party` with a limitation. The example's capture predates this and records none.
- **Found on a Next.js site behind a strict CSP, fixed (v1.1.0):**
  - *Capture was impossible under a hashed or nonce `style-src`*: the freeze style was refused in all
    three engines, every run failed, and the engines were still recorded as `ran`. Contexts are now
    created with Playwright's `bypass_csp` (`"bypass_csp": true` by default, passed to the harness
    and applied by capture itself when the harness copy doesn't), each injected style is read back
    through a marker custom property, and a run that didn't load, inject or capture is `errored`
    with the error text. An engine with no captured run is `errored`. Checked on a loopback fixture
    with a hashed `style-src`: with `bypass_csp` false all 6 runs errored with each engine's CSP
    message; with the default all ran.
  - *Total failure validated as "no differences detected"* (0 of 9 runs, confidence medium). Errored
    runs are no longer compared (they would produce false "missing element" differences),
    `comparisons` in `candidates.json` lists every planned engine-versus-reference pair and why any
    wasn't made, the verdict gains `inconclusive`, the headline counts comparisons planned and made,
    confidence is low when none or fewer than half were made, and the validator rejects
    `no_differences_detected` with zero comparisons, an inconclusive summary that doesn't say so or
    claims no differences, and a missing limitation for comparisons not made.
  - *No 429 back-off and a fixed 0.1 s rate limit in capture*: loads now go through capture's own
    loop with a configurable rate limit (0.5 s off loopback) and Retry-After (seconds or a date)
    following the shared harness's `retry_decision` (a wait over `max_retry_after_seconds` errors the run instead), each wait recorded in the run, and the harness's `metadata()` is kept per engine in `runs.json` and the report's `scope.harness`; requests sent are counted per
    run and engine, and `request_budget` skips the remaining runs once reached. `scope.capture` and
    `scope.requests` carry the settings and counts.
  - *Forced reduced motion wasn't listed as out of scope*; it is now configurable
    (`reduced_motion`, `freeze_animations`) and, when on, the validator requires `out_of_scope` to
    say motion wasn't compared.
  - *The feature scan took 4 min 08 s* walking and sorting all of `node_modules` before filtering.
    It now prunes excluded folders before descending (adding `.next`, `out`, `coverage`, caches,
    `playwright-report`, `storybook-static`) and skips tests and stories: about 4-5 s on the same
    repository with a warm file cache (10.5 s with the old walk, warm).
  - *Wrong targets for Next.js*: with no browserslist config it used `defaults`. A project
    depending on `next` now gets Next's documented default support (16+: Chrome, Edge and Firefox
    111, Safari 16.4; 13-15: Chrome 64, Edge 79, Firefox 67, Opera 51, Safari 12), with the version
    read from the installed package or the range, and iOS Safari taken to follow Safari; `source`
    says so.
  - *Tailwind was invisible*: class strings are matched against known non-baseline utilities when
    the repo depends on Tailwind (`utility_css` records it); built CSS is scanned with `--built-css`
    or from `.next/static/css`, with new patterns for `oklch()`/`oklab()`, `color-mix()` and
    `@property`. Without built CSS, `features.json` carries a limitation the report must repeat.
  - The example's capture predates these settings, so its report has no `scope.capture` or
    `scope.requests`; `candidates.json` was regenerated (only `comparisons` added, 8 of 8 made).
- **v1.2.0** (a static feature scan of a React and Vite 7 single-page app styled with MUI (Emotion) and
  Tailwind CSS 4, behind an identity-provider sign-in):
  - *Wrong targets for Vite*: with no browserslist config the scan used `defaults` (0 findings). A
    project depending on `vite` now gets its literal `build.target` from `vite.config.*` (only the
    `build` block, so a dev-proxy `target` is ignored), else the Vite major's documented default (7:
    `baseline-widely-available`, Chrome and Edge 107, Firefox 104, Safari 16; 3-6: `modules`); `source`
    says which.
  - *Static only wasn't supported by the validator*: it required `--runs` and demanded
    `no_differences_detected` when the plan was empty. `compare_engines.py --static-only` writes the
    candidates file of a feature scan alone (engines `not_run`, the plan from `--targets`, 0
    comparisons), reports carry `run_status`/`run_status_reason`, and a `not_run` report validates
    without `--runs`. Any engine not run, or no comparison made at all, now means `inconclusive`;
    "not made", "not run", "no engine ran" and "static only" count as saying so.
    `examples/report.static.example.json` is the static-only report of the example site.
  - *CSS-in-JS was invisible*: with Emotion, MUI or styled-components in the dependencies, object
    styles and tagged template literals written in scripts are matched (`via: "css-in-js"`), and a
    limitation says runtime-computed styles were not scanned. The CSS tokeniser keeps declarations
    that come before a nested rule.
  - *Tailwind 4*: its `@layer properties` fallback now counts as the fallback for its `@property`
    uses, and declared targets below Tailwind 4's own floor (Chrome 111, Firefox 128, Safari 16.4)
    are listed in `utility_css.targets_below_floor` with a limitation. Minified built CSS uses carry a
    `column` and collapse per feature and line (`occurrences`) instead of 20 identical `:1` entries.
  - *Small JS catalogue*: 16 API patterns added (`.at(-n)`, `findLast`, `toSorted`, `Object.hasOwn`,
    `Object.groupBy`, `Set` methods, `Promise.withResolvers`, `Array.fromAsync`, iterator helpers,
    `AbortSignal.any`, async Clipboard, `replaceAll`, `Promise.any`, `Intl.Segmenter`, `WeakRef`, error
    `cause`), with support from the cached web-features data.
  - SKILL.md describes capturing apps behind sign-in with a test-account `storage_state` loaded by the
    shared harness (never committed or recorded). The example's capture and feature scan are
    unchanged: rerunning `feature_scan.py` on `travel-site/` gives the same `features.json`.
- **v1.3.0** (a pnpm monorepo: a fixture site rendered by a dependency-free Node server from template
  literals, nine pages with a 23-link inline nav, and a Next.js app kept in a subfolder):
  - *Line-wrap cascades*: WebKit's wider system font wrapped the last links of the nav row, and each
    moved link became a candidate on every page (72 of 116, one cause). `compare_engines.py` now finds
    rows (children of one parent on one line) whose items keep their order but wrap onto another line
    in the other engine, with no item resized beyond font metrics (native controls excepted), and
    dismisses the whole row and anything inside its items that moved with them as one line wrap. A
    layout difference of one element signature on several pages is now one candidate (`pages`, reach
    1, 2 or 3+, as the rubric always said; reach was fixed at 1).
  - *Native controls that also moved* stayed candidates. A control's size change is now judged apart
    from its move: the box is reported as a move only (`native_size`), which the wrap, drift and
    consequence rules then judge. The "moved because something above it changed" rule had an
    operator-precedence slip that could pick a cause below the element and then keep it; it now looks
    only above.
  - *Ink on reshaped boxes*: an inline label that wrapped onto two lines, and a list box drawn at
    another size, read as content not rendered. Ink is now compared only between boxes of comparable
    shape (line boxes from `getClientRects`, recorded by new captures as `lines`, or an area change
    under twofold); otherwise the candidate is dismissed with that reason. The example's date input
    (a planted, real ink loss with a comparable box) is still found.
  - *Feature scan shipped tests and CLIs*: `tests/*.e2e.ts` suites and a Node benchmark script were
    scanned as shipped code, and the validator then forced their known false positives into the
    report. The walk now skips `*.e2e.*` and `*.cy.*` files, test directories (`e2e`, `test`,
    `tests`, `spec`, `specs`) that hold no package manifest (the shared convention: a workspace
    package named `e2e` is product code), and Node-only scripts (node shebang, `node:` or Node
    built-in imports), whose inline `<script>`/`<style>` blocks are still scanned when they render
    pages; `features.json` lists them under `skipped`.
  - *Nested Next.js*: built CSS is looked for beside every `next.config.*` up to three levels down,
    not only at `<repo>/.next/static/css`.
  - *Zero candidates*: a comparison with nothing at all to report writes `self_check` (element boxes
    compared and how many were identical) for the agent to confirm by eye.
  - *Validator ergonomics*: `--write-summary` fills placeholders for the computed headline fields
    before the schema check, so a draft may omit them; triage reasons are checked for vagueness (under
    25 characters, or only "false positive", "font metrics" and the like); a progress line is printed
    before screenshots are decoded (a schema error already stopped before that). SKILL.md documents
    `difference_id: null` for unpromoted entries, `evidence_completeness`, and that `checks.planned`
    counts runs.
  - *Action vocabulary*: `capture.py` checks every state step before a browser starts and accepts both
    `{"fill": sel, "value": v}` (the harness's) and `{"fill": [sel, v]}` (the crash hunter's probe form).
  - The example's capture is unchanged; `candidates.json` was regenerated from it (each candidate gains
    `pages`; the candidates, flaky list and 42 automatic dismissals are otherwise the same).
