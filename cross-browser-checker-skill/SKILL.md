---
name: cross-browser-checker
description: Run the same pages, states and journeys in Playwright's Chromium, Firefox and WebKit, three times each, and report where they behave differently - script errors, failing steps, content that isn't rendered, missing elements, input fallbacks, layout and network differences - keeping only differences that repeat in every run and listing the rest as flaky. With a repository, also scan CSS, JavaScript and HTML for features the project's browserslist targets don't all support (web-features and caniuse data, pinned and hash-checked), and link them to the runtime differences they explain. Never says "compatible", and never treats WebKit as Safari.
version: 1.5.0
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

1. **Never "compatible".** The verdict is `differences_found`, `no_differences_detected` (scoped to
   these pages, states and engines) or `inconclusive`. Real Safari, older versions and real devices
   are `untested` unless the user ran them.
2. **WebKit is not Safari.** Playwright's WebKit lacks Apple's media, font and platform layers (on
   Windows and Linux it is a different build again). Never call a WebKit result a Safari result;
   `out_of_scope` always lists Safari and real devices. Some differences come from the Playwright
   build itself: `data/engine-limitations.json` lists the known ones (WebKit on Windows decodes no
   AVIF; Playwright's Chromium has no H.264 or AAC). A candidate that matches one is marked
   `build_limitations`, has low confidence and goes to `needs_review`, never straight to a
   difference: confirm it in real Safari (macOS or iOS) or the branded browser first, and say so.
3. **A difference must repeat.** Each check runs 3 times per engine. A difference counts only when it
   holds in every run of the affected engine and in no run of the reference (Chromium). Anything in
   between is `flaky`, listed with its run counts.
4. **Evidence from both sides**: screenshots and messages from the reference and every affected engine.
5. **Static feature findings are `possible`** until an engine shows the difference; then they are
   `confirmed_at_runtime`, linked to it.
6. **Safe targets.** Loopback needs nothing; other hosts need `authorised_by`. Non-GET requests are
   blocked unless `allow_form_submission`.
7. **Record every engine and data file** as `ran`, `not_run` or `errored`, with versions and hashes.
8. **Third parties are blocked unless the targets allow them.** By default the harness aborts every
   request to a host that isn't the site, loopback or in `allowed_hosts` (analytics, ads, embeds,
   fonts from another host) and records each one in `blocked_requests`; console errors and failed
   requests caused by the blocking are left out of the runs. Their behaviour is then **not
   compared**: the report's `scope.third_party` lists the blocked hosts and a limitation says so.
   `"third_party": "allow"` compares them too.
9. **Pages are modified for capture, and the page's CSP is bypassed to do it.** Capture injects a
   style that freezes animations and transitions, and one that sets `content-visibility: visible`
   for the screenshot. A Content-Security-Policy with a hash or nonce in `style-src` (usual on
   Next.js) refuses both, so every context is created with Playwright's `bypass_csp` (`"bypass_csp":
   true`, the default; it changes nothing for real users). Each style is read back after injection;
   one that didn't apply makes the run `errored`. With `"bypass_csp": false` on such a site, every
   run errors and the verdict is `inconclusive`.
10. **Be gentle with remote sites.** Page loads are spaced by `rate_limit_seconds` (0.5 s off
   loopback); a 429 or 503 is retried after `Retry-After` (the shared harness policy: a wait over `max_retry_after_seconds` stops instead), and
   recorded; every run counts the requests it sent, `runs.json` totals them, and `request_budget`
   stops capturing once reached (later runs are `skipped`). Each check costs engines × reruns page
   loads, and a framework page can make 50 or more requests: size pages × viewports to the agreed
   budget first.
11. **A failed capture is never a pass.** A run that couldn't load, inject its styles or capture is
   `errored` (or `skipped` when the request budget ran out), with the error text; an engine none of
   whose runs captured is `errored`. Only captured runs are compared, and when any planned
   engine-versus-reference comparison couldn't be made and no difference was found, the verdict is
   `inconclusive`: the summary says so and a limitation names what wasn't compared. Zero comparisons
   made is never "no differences", and confidence is then low.
12. **Motion isn't compared by default.** Runs ask for reduced motion and freeze animations, for
   stable screenshots, so `out_of_scope` says engine differences in motion weren't compared.
   `"reduced_motion": "no-preference"` and `"freeze_animations": false` turn that off.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `targets` | required, except static only | `targets.json`: site, pages, states (actions, `critical_path`), viewports. A page or state may list `viewports`, a subset of the top-level list, to run only there (a mobile menu state at the mobile width); a state inherits its page's. Actions are the shared harness's steps: `{"click": sel}`, `{"fill": sel, "value": v}` (or `{"fill": [sel, v]}`), `{"select": sel, "value": v}`, `hover`, `press`, `check`, `uncheck`, `wait` (ms), `wait_for`, `goto`; every step is checked before a browser starts |
| `third_party` | `block` | `block` aborts and records requests to other hosts; `allow` lets them load |
| `allowed_hosts` | `[]` | Other hosts that are part of the site (its own CDN) |
| `service_workers` | `allow` | `block` blocks them in every run |
| `scroll` | `true` | Scroll through each page before the full-page screenshot (see Capture) |
| `bypass_csp` | `true` | Create contexts with Playwright's `bypass_csp` so the capture styles apply under a strict CSP |
| `reduced_motion`, `freeze_animations` | `reduce`, `true` | `no-preference` / `false` to compare motion too |
| `rate_limit_seconds` | 0.1 on loopback, else 0.5 | Seconds between page loads |
| `max_retries`, `max_retry_after_seconds`, `stop_on_429` | 2, 60, false | 429/503 retries after `Retry-After` (the shared harness policy); a longer Retry-After, or any 429 with `stop_on_429`, errors the run |
| `request_budget` | none | Stop capturing once the pages have sent this many requests; later runs are `skipped` |
| `engines` | all three | Missing ones are `not_run` |
| `browserslist` | from the repo, else Next.js's defaults for a Next.js project, else a Vite project's `build.target` (or Vite's default), else `defaults` | `package.json` or `.browserslistrc` |
| `repo` | none | Turns on the static feature scan |
| `static_only` | false | Feature scan without browsers; every finding stays `possible`, `run_status` is `not_run` and the verdict `inconclusive` (see "Static only") |
| `storage_state` | none | A signed-in test session for an app behind sign-in (see "Apps behind sign-in") |
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
Before the screenshot it scrolls through the page a viewport at a time, waits up to 5 s for lazy
images and returns to the top, and sets `content-visibility: visible` while the screenshot is taken:
Firefox and WebKit draw a full-page screenshot without scrolling, so `loading=lazy` images and
`content-visibility: auto` sections below the fold would otherwise come out blank and look like
content that isn't rendered (`"scroll": false` turns this off). `runs.json` records the platform,
the capture settings, the planned checks (page or state × viewport) and what each scroll did, and
per run `run_status` (`ran`, `errored`, `skipped`), the error, `requests_sent` and any 429/503
`backoff`; `requests` totals them. Each text element and form control records its computed
`font` family, an element another element covers at its centre (a modal overlay, a sticky panel) is
marked `occluded` with `covered_by`, and panels that scroll their own content are listed in
`scrollers` with their scroll offset. A full-page screenshot shows only the first box of such a panel
(an app shell of height `100vh`): content further down is compared by DOM box only, so say so in the
limitations and scroll the panel by hand when a finding depends on it. Check the engines' status and the warning capture prints before
going on: an errored run is never a pass. Run one engine at a time (`--engines webkit`) when memory
is short.

**Apps behind sign-in** (MSAL, OIDC or another identity provider). Without a session a single-page app
renders only its sign-in page, and following the sign-in redirect leaves the site. The least invasive
route is a captured session: a person signs in to a **test account** by hand with
`python scripts/browser_harness.py capture-session --url <app> --out <file outside the repo>`, and the
shared harness loads the file as `storage_state` into every engine's context. It carries cookies,
localStorage, IndexedDB and sessionStorage (put back on its origin before the app's scripts run), so an
MSAL cache in sessionStorage works too; the run stays read-only and blocks third parties, so a token
refresh fails: capture shortly before the run. The file holds live tokens: never commit it, never copy
its contents into `runs.json`, the report or a screenshot name, and delete it after the run; `runs.json`
records only counts. Without a session file, the app's own test sign-in or mock-auth mode is the
alternative. Never use a real user's account. Without a session, capture what renders
signed out (the sign-in page), set `run_status` to `partial` with the reason, and say in the
limitations which pages weren't compared because they need a session.

### 3. Static feature scan (with a repo)

```bash
python scripts/browserslist_lite.py --repo <repo>          # see what the targets resolve to
python scripts/feature_scan.py --repo <repo> --out <run>/features.json
```

Unresolvable browserslist terms (Firefox ESR, `and`, regional usage) are listed, and the scan can't
assess them. A Next.js project with no browserslist config compiles for Next's own documented
targets (Next 16: Chrome, Edge and Firefox 111, Safari 16.4), so those are used, and `source` says so.
A Vite project with no browserslist config compiles for its `build.target`: the scan reads it from
`vite.config.*` when it is a literal (`['chrome100', 'safari15']`, `'modules'`,
`'baseline-widely-available'`), else uses the Vite major's documented default (Vite 7:
`baseline-widely-available`, Chrome and Edge 107, Firefox 104, Safari 16; Vite 3-6: `modules`,
Chrome 87, Edge 88, Firefox 78, Safari 14), with iOS Safari following Safari; `source` says which.
`esNNNN`/`esnext` name no browser and are reported as not assessed.
The walk prunes `node_modules`, `.next`, build output, coverage and caches, and skips what doesn't
ship to browsers: test and story files (`*.test.*`, `*.spec.*`, `*.e2e.*`, `*.cy.*`, `*.stories.*`),
test directories (`e2e`, `test`, `tests`, `spec`, `specs`, unless the directory holds a package
manifest such as `package.json`, which makes it a product package of that name) and Node-only
scripts (a node shebang, or an import of a `node:` module, `fs`, `child_process` and other Node
built-ins: CLIs, build scripts, dev servers). A Node server that renders pages from template
literals still has its inline `<script>` and `<style>` blocks scanned, and so does an HTML page held
in a string constant of server-side code (a Go raw string for `html/template`, a Python or Java/Kotlin
triple-quoted string) or a template file (`.tmpl`, `.gohtml`, `.jinja`, `.erb`). Vendored and
third-party trees are left out (the shared `lang_census.py`: `vendor/`, `third_party/`, bundles, a
package with its own manifest and LICENSE under `drivers/` or an SDK directory). When another language
dominates (a Go tool), `features.json` carries `language_coverage` and a limitation (also a warning on
stderr) saying what was and wasn't read: with no page in a string constant, 0 uses is not evidence,
so scan a rendered page instead (copy it outside the repository and pass that folder as `--repo`).
`features.json` lists what was left out under `skipped`; look at it when a finding you expected is
missing.

Server-rendered sites (a Flask or Django app rendering Jinja or Django templates named `.html`, Rails,
Laravel) are supported: their templates and stylesheets are the browser code, and they are scanned like
any page. When the repository is mostly Python, Ruby or PHP and the scanned markup includes templates
(HTML under `templates/`, `views/`, `layouts/` or `partials/`, or with `{% %}`, `{{ }}`, `<% %>` or
`@extends`), `language_coverage.status` is `server_rendered`: it names the templates scanned, lists HTML
outside the template and static folders (`markup_outside_templates`, a stale rendered page or an old
build's output, which may not be served: check before reporting a use in one), and writes no language
limitation, because the server-side code is not browser code. `scanned_files` lists every file read
(count, and up to 300 paths).

Data directories are pruned before they are walked: a directory holding at least 200 files of which at
least 90% are data files (`.json`, `.geojson`, `.csv`, `.tsv`, `.xml`, `.ndjson`, `.jsonl`, `.txt`,
`.yaml`, `.parquet`, `.avro`) and none code or markup is listed in `skipped.data_dirs` with its file
count. `--exclude DIR` (repeatable, repository-relative) leaves out any other directory, such as an
archive of old builds (`skipped.excluded`).

JavaScript APIs, not syntax: a build transpiles syntax for its targets but doesn't polyfill APIs, so
the scan looks for APIs that newer targets have and older ones don't (`structuredClone`, `.at(-1)`,
`findLast`, `toSorted`, `Object.hasOwn`, `Object.groupBy`, the new `Set` methods,
`Promise.withResolvers`, `Array.fromAsync`, iterator helpers, `AbortSignal.any`, the async Clipboard
API, which also needs a secure context, `replaceAll`, `Intl.Segmenter`, `WeakRef`, error `cause`), with
support from web-features. A polyfill the project loads (core-js) is not detected: say so when a
finding depends on it.

CSS-in-JS (Emotion, MUI's `sx` and `styled`, styled-components) generates its CSS at runtime, so
neither the source scan nor built CSS sees it. When the repo depends on such a library, the scan reads
the styles written literally in the scripts that use it: object styles (a camelCase property with a
string value such as `textWrap: 'balance'`, a selector key such as `'&:has(.x)'`, an `@container`
key) and tagged template literals (`` styled.div`...` ``, `` css`...` ``). Those uses are marked
`via: "css-in-js"`, `css_in_js` records the libraries, and a limitation says that styles computed at
runtime were not scanned; the report must carry it.

Utility-class CSS (Tailwind) lives in class strings, not stylesheets. When the repo depends on
Tailwind, class strings are matched against the known non-baseline utilities (`text-balance`,
`backdrop-blur-*`, `bg-clip-text`, `has-[...]`, `@container`, `field-sizing-*`, `starting:`), and
`utility_css` says only those were scanned. The CSS the framework generates (colour functions,
`@property`, layers) is scanned only from built CSS (`--built-css <dir>`, or `.next/static/css`
when it exists, in the repo or beside any `next.config.*` up to three levels down, for a Next.js
project kept in a subfolder and built with `next build <dir>`); otherwise `features.json` carries a limitation, which the report must repeat.
Minified built CSS has every rule on line 1, so its uses carry a `column` and repeats of one feature on
one line collapse into one entry with `occurrences`. Tailwind 4 registers its variables with
`@property` and emits its own fallback for engines without it (`@layer properties { @supports (...)
{ *, ::before, ::after { --tw-...: initial } } }`); when built CSS has that block, its `@property`
uses count as having a fallback. Tailwind 4 itself requires Chrome and Edge 111, Firefox 128 and
Safari 16.4: when a declared target is below that floor, `utility_css.targets_below_floor` lists it
and a limitation says the project's real support floor is ambiguous.

### 4. Compare engines

```bash
python scripts/compare_engines.py <run>/capture/runs.json --features <run>/features.json --out <run>/candidates.json [--jobs 4]
```

Only captured runs are compared; `comparisons` lists every planned engine-versus-reference pair and
why any wasn't made (an errored or skipped run on either side). Differences: page and console errors, failed steps, failed requests and undecoded images, missing or
extra elements, input type fallbacks, box differences, overflow, and content not rendered (an element
with under 40% of the reference's ink). Font metrics, native form controls (their size change is
judged apart from a move: a control that also moved is judged as a move), line wraps (a row of items
under one parent, such as nav links, that keep their order but wrap onto another line in one engine,
with no item resized beyond font metrics: the whole row and what moved with it is one dismissal),
consequences of other differences (a box that grew because something inside it changed, an element
below it that moved), cumulative drift (an element that moved with the content just above it, by text
metrics adding up down a long page) and ink measured on boxes of different shape (an inline element
on another number of lines, or a box whose area changed more than twofold), boxes of an element
another element covers (`occluded`: an overlay or sticky panel lies over it) and boxes inside a panel
the automation scrolled by a different amount in each engine (moved by exactly that offset) are
dismissed automatically and counted, with reasons in `candidates.json`. A layout difference of one element (by
its signature, such as a shared header or footer) on several pages is one candidate: `pages` lists
them and reach counts them. Repeated siblings are one component: `:nth-of-type(N)` and `:nth-child(N)`
are taken out of the grouping key, so 40 cards with the same undecoded image are one candidate, whose
`elements` lists them. A cause seen at several viewports is one candidate too (`viewports`), and every
candidate counts the raw differences it stands for (`occurrences`). Console messages are compared one
message at a time (without the source position), so one message on many pages is one candidate. A
console message for an HTTP error response ("Failed to load resource: the server responded with a
status of 400") logged in one engine only is dismissed when every run of both engines recorded that
response status (`http_errors`, written by capture.py): the request failed alike, only the logging
differs (Firefox does not log fetch() error responses). Text whose computed font family differs between engines in every run
(a control that doesn't inherit the page font is drawn in the engine's own default face) is one
`font_family_differs` difference per pair of families, listing its elements; Playwright WebKit on
Windows draws such text in a serif face (`webkit-windows-serif-controls`), so that one goes to needs
review until a real Safari confirms it. When comparisons were made and nothing at all came out (no candidate,
flaky entry or dismissal), `candidates.json` carries a `self_check` line saying how many element
boxes were compared and how many were identical: open one screenshot pair per engine and confirm the
pages really rendered alike before reporting no differences. Each screenshot is decoded once, in
parallel (`--jobs`, default up to 4; the validator takes the same option). Candidates matching a known
engine-build limitation carry its id in `build_limitations`, and so do the candidates that follow from an
undecoded image with a known limitation on the same page and engine (the `<img>` an error handler hides, the
fallback it shows beside it, a box resized around it): their detail says which image. Flag emoji are drawn
as letters by Chromium and WebKit on Windows and as colour flags by Firefox (`windows-flag-emoji`), and
Windows WebKit may draw default-face controls in MS Shell Dlg rather than a serif
(`webkit-windows-serif-controls`). Ratings follow `RUBRIC.md`.

### 5. Triage and write

Every candidate is promoted, `needs_review` (for example media that may play in branded browsers with
proprietary codecs, and every candidate with `build_limitations`), or dismissed with a checkable
reason. Each triage entry is `{candidate_id, decision, difference_id, reason}`: `difference_id`
names the difference a promoted candidate became and is `null` for `needs_review` and `dismissed`.
The reason says what was compared and what it showed ("the WebKit screenshot shows the label on one
line, wider by 6 px; nothing is hidden"): a reason under 25 characters, or only "false positive",
"font metrics", "by design" and the like, fails validation. For a build limitation, the needs-review reason names it and what to check in the real
browser (its `confirm` text in `data/engine-limitations.json`); in the summary, say that WebKit
showed it and real Safari wasn't checked, never that Safari has it. For each difference write a title naming the
engines (never "Safari"), a remediation that addresses the cause (a feature check, a fallback, an
`@supports` block), and a verification step (rerun capture for that page and name what should pass).
When a site serves different scripts per engine (compare `scripts` across engines), say so for every
difference on that page.

A feature finding is `possible`, not a defect: say when it is a progressive enhancement that degrades
harmlessly (`text-wrap: balance` wraps normally, `::-webkit-scrollbar` keeps default scrollbars)
rather than leaving it to read as a break.

### 6. Validate and render

```bash
python scripts/validate_report.py <run>/report.json --runs <run>/capture/runs.json --candidates <run>/candidates.json \
  --features <run>/features.json --targets targets.json [--write-summary]
python scripts/render_report.py <run>/report.json [--brief]
```

`--brief` shows the differences, the needs-review items and the limitations. The validator checks
`feature_findings` against the `--features` file passed (use for use), not only against the copy in
`candidates.json`.

**Static only** (`static_only`, or when no engine can run): skip the capture and write the
candidates file of the feature scan alone, then a report with `run_status: "not_run"`, a
`run_status_reason`, every engine `not_run`, no differences, triage or flaky entries, `runs_file:
null`, verdict `inconclusive`, and a summary and limitation that say no engine ran:

```bash
python scripts/compare_engines.py --static-only --features <run>/features.json [--targets targets.json] --out <run>/candidates.json
python scripts/validate_report.py <run>/report.json --candidates <run>/candidates.json --features <run>/features.json [--targets targets.json] [--write-summary]
```

`--targets` records the checks that were planned and not made; without it the plan is empty, and the
verdict is still `inconclusive`. `examples/report.static.example.json` is a static-only report.

Commercial device clouds (BrowserStack, Sauce Labs) aren't integrated; with an account, run the same
targets there and compare by hand.

## Output fields

- `scope`: site, pages, viewports, engines with versions and status (`ran`, `not_run`, `errored`),
  reference engine, resolved browserslist, `out_of_scope`, reruns, `authorised_by`, `third_party`
  (as in `candidates.json`; required when the capture recorded it), and `capture`, `requests` and `harness`
  (copies of `runs.json`'s, required when the capture recorded them; `harness` is the shared harness's
  `metadata()` per engine: CSP bypass, read-only mode, request totals, retry policy).
- `run_status` (`complete`, `partial`, `not_run`) and `run_status_reason` (unless complete): whether
  the capture ran in full; `not_run` is a static-only report.
- `metadata`: tools, data versions and hashes, runs file (`null` when static only).
- `headline`: verdict (`differences_found`, `no_differences_detected`, `inconclusive`), summary,
  severity counts, flaky count, checks planned and run (`checks.planned` and `checks.ran` count runs:
  page or state × viewport × engine × rerun), engine comparisons planned and made (page or state ×
  viewport × each engine other than the reference), evidence completeness (`complete` or `partial`:
  partial when an engine or run didn't run, a comparison wasn't made, or anything is flaky, needs
  review or not assessed), overall confidence. Write the verdict and summary; `--write-summary`
  computes and fills the other fields, which a draft may leave out.
- `differences[]`: kind and subkind, engines affected, reference, page and states, element, severity,
  confidence, priority, score, usage share, runs per state and engine, evidence per engine, linked
  feature cause, remediation and verification step.
- `feature_findings[]`, `flaky[]`, `dismissed_automatically`, `needs_review[]`, `triage[]`, `limitations[]`.

## What the validator enforces

Candidates, ratings, flaky list and feature links are recomputed from the recorded runs. Every
difference is consistent across its reruns, differs from the reference, and has screenshots (matching
their hashes) from both sides. Safari is never an engine or a title, and a summary sentence that
names Safari must say it wasn't tested (sentences split at a full stop and a space, so "Safari 16.4"
is one sentence). No compatibility claims. The verdict follows from the runs: `inconclusive`
whenever a planned comparison wasn't made, an engine didn't run, or no comparison was made at all (an
empty plan included), and nothing was promoted; never `no_differences_detected` with zero
comparisons; an inconclusive summary must say so ("inconclusive", "not compared", "not made", "no
engine ran", "static only") and not claim "no differences", and a limitation must name what wasn't
compared. `run_status`, when given, agrees: `complete` only when every planned run ran and every
comparison was made; a `not_run` report is validated without `--runs`, from a `--static-only`
candidates file, with every engine `not_run` and no differences, triage or confirmed features. Confidence is low when no comparison, or under half of
them or of the runs, was made. `scope.capture`, `scope.requests` and `scope.harness` equal `runs.json`'s; forced
reduced motion or frozen animations need `out_of_scope` to say motion wasn't compared; the feature
scan's limitations (utility CSS not scanned) must appear in the report's. Every
candidate is triaged once, with a reason that says what was checked, and none with
`build_limitations` is promoted. The schema is checked first: a schema error stops before any
screenshot is decoded. Checks planned come from
`runs.json`'s recorded plan, `--targets` (which must agree with it), or for an older capture the
page and viewport pairs that ran, so the count is the same with and without `--targets`. Blocked
third-party requests need `scope.third_party` and a limitation saying they weren't compared.
