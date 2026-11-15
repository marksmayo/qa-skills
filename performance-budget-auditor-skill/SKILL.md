---
name: performance-budget-auditor
description: Measure web pages in a real browser against a performance budget, as lab data. Core Web Vitals (LCP, CLS, INP) with web-vitals' attribution, TBT, FCP, TTFB and bytes per resource type, each page run several times under fixed network and CPU profiles, with median, range and coefficient of variation. Each over-budget metric is pointed at its cause (the LCP element and its resource, the layout shift's target, the scripts in long tasks, the slow interaction handler, the largest downloads and unused JavaScript, Server-Timing for slow responses), rated, and compared with a baseline when one is given. Never calls a site fast: the best verdict is "within budget in the lab, under the tested profiles; field performance not established".
version: 1.4.0
---

# Performance budget auditor

## Mission

Measure how heavy and slow pages are under controlled conditions, against a budget, and say what
causes each overrun. Every number comes from recorded runs on a recorded machine, so a reader can
tell how far to trust it. Lab data is not what users experience; the report says so, and keeps
any field data (CrUX, RUM) separate.

This skill differs from the WCAG auditor and the UI crash hunter, which look for barriers and
crashes: its findings are budget overruns. It uses the same browser harness and the same rules
for targets and forms.

## Non-negotiable rules

1. **Never "fast".** The verdict is `over_budget` or `within_budget_in_lab`, and the second always
   says "under the tested profiles; field performance not established". No praise words in
   summaries or titles.
2. **Variance is reported.** Every metric has its runs, median, min, max and coefficient of
   variation (CV). A metric with CV above 0.25 after the run cap (`max_runs`, 10 by default) is
   `unstable`: it neither passes nor fails the budget, its note says "unstable at the cap of N
   runs", and a candidate built on it goes to `needs_review`. A spread too small to matter for the
   budget is stable whatever its CV: max − min within 10 ms, 0.01 CLS or 1 KB, or every run at or
   under half the budget (TBT of 0–39 ms against 300 ms is not noise that matters). With five or
   more runs, isolated outliers (at most a quarter of the runs, more than 3 robust standard
   deviations from the median) are set aside when the rest are stable and their median is on the
   same side of the budget: the measurement is stable `robust` and lists the outlier runs. Two slow
   round trips on a remote target can't then block a verdict, nor hide one.
3. **The run cap is honoured.** `runs` measured runs, extra runs only up to `max_runs`, and none
   when `max_runs` equals `runs`; each measurement records `runs_used` and `run_cap`. Say plainly
   what a plan costs: "runs: 5" is one warm-up, 5 to 10 measured runs and one diagnostic run per
   page, profile, cache mode and variant (7 to 12 loads) with the default cap. A page that answers
   with an HTTP error is measured once to confirm, then stopped, with the status in its note.
4. **Medians decide budgets**, never the best run. Warm-up and diagnostic runs never count.
5. **Every number has a source**: run ids, the API that produced it (web-vitals, Long Tasks,
   DevTools Network, Server-Timing), and the trace.
6. **INP needs an interaction.** Without a scripted interaction INP is `untested`; TBT is the lab
   proxy and is labelled as one.
7. **The machine is part of the evidence**: CPU, cores, OS, browser version, the throttling actually
   applied, and a CPU calibration. Results from different machines are never compared without
   saying so.
8. **Lab and field never mix.** Field data, when given, is shown separately and labelled `field`.
9. **Safe targets.** Loopback needs nothing; any other host needs `authorised_by`. Non-GET requests
   are blocked unless `allow_form_submission` (the harness enforces it).
10. **Record every tool** as `ran`, `not_run` or `errored`; web-vitals by version and SHA-256.
11. **Third parties are blocked unless the plan allows them.** By default the harness aborts every
   request to a host that isn't a target, loopback or in `allowed_hosts` (analytics, ads, embeds,
   fonts from another host), and records each one. Their cost and behaviour are then **not
   measured**: the report's `scope.third_party` lists the blocked hosts and a limitation says so.
   Set `"third_party": "allow"` to measure them (never sends forms either way); list a CDN the site
   serves its own assets from in `allowed_hosts`. A blocked request fails at once
   (`ERR_BLOCKED_BY_CLIENT`), so there is no upload contention and `networkidle` comes sooner than
   in production; an SDK bundled into the site's own JavaScript (error tracking, session replay) is
   still parsed and run, so its CPU cost is in the numbers and only its uploads are cut.
12. **Blocked first-party requests are reported.** Non-GET requests to the site itself (beacons,
   CSP reports, telemetry, web-vitals posts) are blocked too unless `allow_form_submission`;
   `scope.blocked_first_party` lists them by method and path, and a limitation says their upload
   cost and the page's handling of the failures (retries, console errors) are not measured.
13. **Cold is a cold browser, not a cold CDN.** The warm-up primes any CDN, edge or image-optimiser
   cache in front of a remote target. Each measured document's cache headers (`x-vercel-cache`,
   `cf-cache-status`, `x-cache`, `age`, ...) are in `cdn_cache`; for a target that isn't loopback a
   limitation says the runs measured a warm CDN.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `targets` | required | URLs with a template name and why each is in the sample (one per template plus critical paths) |
| `budget` | `data/default-budget.json` | This skill's format (`{"profiles": {name: {metric: value}}}`), a Lighthouse `budget.json`, or both (`"lighthouse": [...]`). Its `source` field names it in the report (a copy of the defaults keeps saying it is the defaults) |
| `profiles` | `mobile-mid`, `desktop` | `data/profiles.json` |
| `runs` | 5 | Measured runs per page, profile, cache mode and variant, after one warm-up |
| `max_runs` | 10 | Hard cap on measured plus extra runs (`--max-runs`); set it to `runs` for no extra runs, e.g. against a shared or rate-limited environment |
| `min_runs` | `runs` | Stop as soon as every budgeted metric is stable after this many runs (`--min-runs`) |
| `cache` | `cold` | `cold`, `warm` or `both` |
| `interaction` | none | Steps per target for INP (`[{"click": "#add"}]`); a failed step loses INP only, never the run's load metrics |
| `setup` | none | Per target: state to put the browser in before every measured load: `local_storage`, `session_storage` (`{key: value}`), `cookies`, and `actions` (steps run on a first load, after which a cold run's HTTP cache is cleared) |
| `variants` | none | Per target: `[{"name": "first-visit"}, {"name": "after-consent", "setup": {...}}]`: the same page measured in several states, each its own measurement |
| `third_party` | `block` | `block` aborts and records requests to other hosts; `allow` loads and measures them |
| `allowed_hosts` | `[]` | Other hosts that are part of the site (its own CDN): `["cdn.example.com", "*.example-cdn.net"]` |
| `service_workers` | `cold` | `cold` blocks them on cold runs only; `block` on every run (a site whose worker would serve warm runs from its cache); `allow` |
| `http_cache` | `keep` for Chromium with warm runs, else `route` | The shared harness's setting: `route` guards requests with Playwright routing, which turns the HTTP cache off; `keep` (Chromium) guards each page over CDP and keeps it. Warm runs with `route` are refused. Under `keep` a service worker's own requests are not intercepted by the guard (`metadata.harness.service_workers.requests_guarded: false`) |
| `bypass_csp` | `false` | Measure the page under its own Content-Security-Policy (web-vitals is an init script, which CSP doesn't block); `true` bypasses it, and a limitation must then say the CSP was not enforced |
| `stop_on_429` | `false` | Stop a page at its first 429 instead of retrying after Retry-After (the shared harness's `retry_decision`) |
| `authorised_by`, `auth`, `allow_form_submission` | as in `browser_harness.py` | `auth: {"storage_state": file}` runs signed in, with a session a person captured by signing in by hand (`python scripts/browser_harness.py capture-session --url <app> --out <file outside the repo>`; cookies, localStorage, IndexedDB and sessionStorage); the run stays read-only and records only that a session was used, never the file or its contents |
| `baseline` | none | A previous run's `candidates.json`, for regression comparison |
| `field_data` | none | CrUX or RUM figures to show alongside, labelled field |

Profiles: `mobile-mid` is 412x915 at device pixel ratio 2.625 (as Lighthouse emulates a mid-range
phone, so `srcset` picks what a real phone picks), 1.6 Mbps down, 750 Kbps up, 150 ms RTT, 4x CPU
slowdown; `desktop` is 1350x940, 10 Mbps, 40 ms RTT, no CPU slowdown. CPU slowdown is relative to the
measuring machine. The pixel ratio is applied over CDP in Chromium and recorded in
`throttling.applied.device_scale_factor`; a mobile run without it needs a limitation.

## Workflow

Scripts are in `scripts/`. `measure.py` needs Playwright (`pip install -r requirements.txt`,
`python -m playwright install chromium`; ask first). Everything else is stdlib.

### 1. Scope and sample

Write a plan (`plan.json`: targets with `template`, `reason`, `critical_path`, `interaction`;
profiles, runs, cache, budget, `authorised_by`). Sample one page per template plus critical paths;
add a second page of a template when you want to see what the template shares.

### 2. Tooling

```bash
python scripts/fetch_assets.py          # web-vitals 6.2.3, checked against its npm integrity and SHA-256
```

Record Lighthouse as `not_run` unless you run it; when you do, its audits are supporting evidence
only and never replace the repeated runs.

### 3. Measure

```bash
python scripts/measure.py plan.json --out <run>/measurements.json --artifacts <run>/artifacts
python scripts/measure.py plan.json --out <run>/measurements.json --append --browser firefox --profiles desktop --only about --no-diagnostic
```

`--only` and `--templates` that leave no target exit 2 and name the filter (in Git Bash, pass `about`,
not `/about`: an argument that starts with `/` is rewritten into a Windows path). `--no-reference-update`
measures without ever writing the stored idle reference (use it when told not to touch the machine's
cache).

It records the machine and a CPU calibration, timed again at the end of the run. The machine is
`busy` when the calibration runs vary (CV above 0.1), when the median is 1.5x or more the idle
reference for this machine (the fastest calibration stored in the asset cache, or
`--calibration-reference MS`), or when the start and end calibrations are 1.5x or more apart. A
steady load (another process using the CPU throughout) gives a low CV, so without a reference the
calibration's `note` says a steady load would not show: never call that machine idle. A steady load
also looks calm to the calibration, so the first idle reference is stored only with
`--record-reference` (run it on a machine you know is idle); after that a calm run (not busy, CV
within 0.03, start and end within 1.1x) only ever lowers it, and `calibration.reference_update` says
what happened. An `--append` pass calibrates again and is kept beside the earlier ones under
`calibration.passes`; the file is busy when any pass was. A busy
machine inflates every CPU-bound number (LCP, TBT, INP under CPU throttling). The calibration's
`state` grades the machine: `busy`, `loaded` (1.2x to 1.5x the idle reference at the start or end:
not busy, but CPU-bound numbers are inflated by up to that factor, and a limitation must say so),
`idle`, or `unknown` (no reference). Then per page, variant, profile and cache mode: a warm-up, N measured runs in fresh
contexts, extra runs while a budgeted metric is unstable (up to `max_runs`), and one diagnostic run
with the trace, filmstrip (until the later of load and the final LCP, plus 500 ms) and JavaScript
coverage. A 429 or 503 is retried twice after Retry-After; any other HTTP error stops that page
after one measured run. Each run also records the document's CDN cache headers and a request
summary (framework prefetches such as Next.js `?_rsc=`, fetch/XHR counts, fetch paths repeated
within a load), which the 12 largest resources kept per run would hide. Cold runs block service workers (`"service_workers": "block"`
blocks them for warm runs too). Requests to third-party hosts are blocked and listed in
`blocked_requests` unless the plan says `"third_party": "allow"`. Interactions run after the page
settles, for INP. In Chromium, the requests an interaction starts (a video, the modules a "play"
button loads) are counted apart, until the network has been quiet for 0.5 s (at most 10 s):
`transfer_interaction` (a metric a budget can set, in bytes) and `interaction_resources`; the load's
`transfer_total` no longer includes them. Each resource also records its response headers
(`headers`: content-encoding, cache-control, whether a validator exists, expires). A warm run checks
that its second load used the HTTP cache (`cache_check`: resources from the disk or memory cache or
revalidated with a 304, or a service worker's own requests answered from the HTTP cache); a harness
that routes every request disables the cache (Playwright turns it off under request interception, and
the harness metadata then records `http_cache: {"mode": "disabled"}`), and a warm mode that never hit
the cache measured a cold load twice. Responses a service worker answered are its own state, never an
HTTP-cache hit (`cache_check.from_service_worker`): a worker can answer every request from its cache
and still fetch each one again (stale-while-revalidate), and the page's DevTools Network domain never
sees those fetches. Where workers are allowed (warm runs by default), each run records
`service_worker` (controlled, responses answered, workers) and, in Chromium, what the worker fetched
from the network itself: the `transfer_sw` metric (bytes; not part of `transfer_total`, which is the
page's own requests). Each run also records its rendered images (`images`: natural and rendered size,
device pixel ratio) for the oversized-image check.

### 4. Attribute and rate

```bash
python scripts/attribute.py <run>/measurements.json --budget budget.json --out <run>/candidates.json
```

It also records the budget file's path (the validator reads it when not given `--budget`) and the
third-party summary (`third_party`: mode, allowed hosts, blocked hosts with request counts), which
goes into the report's `scope.third_party` unchanged.

Statistics and statuses per metric, Server-Timing per route, and one candidate per (metric,
cause), rated by `RUBRIC.md`. A cause shared by several templates (the same script, stylesheet,
image, server step or element with an id or class, such as a banner in the root layout) is one
site-wide candidate listing every page, so its reach counts them all. It also writes
`blocked_first_party`, `cdn_cache`, `request_summary`, `warm_cache` (per page measured warm: whether
the warm loads used the HTTP cache) and `delivery` (per page whose runs recorded headers: text
resources of 1 KB or more sent without compression, and first-party resources with no cache
lifetime: no `max-age`, `immutable` or `Expires`; `s-maxage` is for shared caches such as a CDN
and gives the browser only heuristic freshness; plus `oversized_images`, images whose natural width
is more than 1.5x their rendered width times the device pixel ratio, with the bytes beyond what the
slot needs) and `service_workers` (per page and cache mode where a worker was active: runs it
controlled, responses it answered, what it fetched), which go into the report unchanged. A budget can
set `transfer_image`, `transfer_font`, `transfer_stylesheet` and `transfer_document` (or Lighthouse
`resourceSizes` of image, font, stylesheet, document); they are measured only where a budget sets
them, and Lighthouse entries the skill cannot measure (media, other, third-party, resourceCounts) are
listed in `candidates.json` as `budget.ignored` with a warning, never dropped silently. A transfer cause says when its largest resource was
sent uncompressed. A metric unstable with its median under the budget is a candidate too (key
`unstable:<metric>`, low confidence, so needs_review): its cause gives the range, the runs furthest
from the median and the calibration state, for the triage to say whether the page or the machine
varied. Uncompressed text and missing cache lifetimes are remediations for a weight budget even
when the budget passes: name them in the limitations or next steps.

An LCP element inside a `position: fixed` overlay, or a text LCP painted more than 500 ms after
load with no resource, is named as such in the cause: usually a consent banner, a modal or a
client-rendered block (a timer, or hydration). Measure the page with and without it (`variants`).

An LCP image painted as a CSS background (`css_background`) is found only once the stylesheet
applies; the cause says whether a `<link rel=preload as=image>` names it (`preloaded`), with the
resource load delay. A TBT cause names every script within 10% of the top one (one hydration task
split across framework chunks makes a near tie) and is keyed on that set; causes that share a script
are one candidate, so a framework's hydration cost is one site-wide cause rather than one per chunk.
A CPU-bound overrun (LCP, TBT, INP) no larger than the machine's own slowdown against its idle
reference is low confidence (`needs_review`): the machine alone could explain it. A weight cause
names framework prefetches (Next.js `?_rsc=`) when they are a fifth or more of the page's weight.
Prefetches are speculative idle-time fetches of other pages, but they are bytes the visit downloads
on the user's connection, so they stay in `transfer_total` (a weight budget includes them); the
remediation decides whether to prefetch less (`prefetch={false}` on low-value links, viewport-only
prefetch) or to budget them apart. An INP cause names the largest of its three subparts: a long input delay is
the main thread being busy when the user acted (`overlapping_long_tasks`, style and layout), not
the handler, which only processing time blames. Note that `{"click": sel}` is a mouse click that
hovers first, even in a mobile profile: hover styles (a menu opened on `:hover`) are part of it.

### 5. Compare with a baseline (optional)

```bash
python scripts/compare.py baseline/candidates.json <run>/candidates.json --out <run>/regressions.json
```

A change counts only when it exceeds the larger of 10% and the two measurements' combined spread.

### 6. Triage

Every candidate is `promoted`, `needs_review` (always for low confidence: unstable or noisy) or
`dismissed` with a checkable reason (for example a third-party embed the user excluded). For each
finding write a title that states the measurement (no praise or blame words), the remediation
(what to change and why the attribution points there), and a verification step (rerun the plan
for that page and say which median should drop below the budget).

Watch for: consent banners (give the target two `variants`, a first visit and one whose `setup`
stores the consent or clicks the button, and report both: the banner is often the first visit's
LCP); bimodal
results from A/B tests (`bimodal: true`: report the two groups, don't trust the median); third
parties (blocked by default, so not measured: say so in the limitations and name the blocked
hosts; when the plan allows them they're reported by origin, and excluding them from the budget is
the user's call); mobile web (an emulated profile, not a real device; native apps are out of scope);
remote targets (an HTTP error page, CDN cache state, and outlier runs: read `cdn_cache` and each
measurement's `outliers` before trusting a TTFB). Hashed bundle names (`21ev0xpk.js`) are the only
cause the runs can name: point the remediation at the build's own bundle analyser or source maps
(the chunk's `sourceMappingURL`, `next build` with a bundle analyser, `vite build --sourcemap`) to
map the chunk to modules.

### 7. Write, validate, render

Write the triage (`triage.json`: the summary, a decision and reason per candidate, and for each
promoted one its title, remediation and verification step, plus limitations and next steps) and seed
the report from it; everything the validator recomputes is copied or computed, and each limitation
the runs require that the triage does not already state is added in a default wording (listed on
stderr: reword it where the run needs more):

```bash
python scripts/build_report.py --measurements <run>/measurements.json --candidates <run>/candidates.json   --triage triage.json --out <run>/perf_report.json [--regressions <run>/regressions.json] [--plan plan.json]
python scripts/validate_report.py <run>/perf_report.json --measurements <run>/measurements.json --candidates <run>/candidates.json \
  --budget budget.json [--regressions <run>/regressions.json] [--write-summary]
python scripts/render_report.py <run>/perf_report.json [--brief]
```

Fix every validator error; never edit the runs.

## Output fields

- `scope`: targets (template, reason, critical path, interaction, variants, setup), profiles,
  budget source and note (as in `candidates.json`), runs, `min_runs`, `max_runs`, cache modes,
  browsers, `authorised_by`, `third_party` (as in `candidates.json`; required when the run
  recorded it) and `blocked_first_party` (required when any were blocked).
- `cdn_cache[]`, `request_summary[]`, `warm_cache[]`, `delivery[]` and `service_workers[]` (as in
  `candidates.json`; required when the runs recorded them).
- `metadata`: machine, calibration, tools (web-vitals with SHA-256, Playwright browsers,
  Lighthouse), the measurements file, and `metadata.harness`: the measurements' `harness` record
  copied unchanged (the shared browser harness's metadata: CSP bypass, read-only mode, third-party
  mode, request budget and totals), required when the measurements carry it.
- `headline`: verdict, `data_kind: "lab"`, a summary, over-budget counts per metric, unstable count,
  evidence completeness and overall confidence.
- `measurements[]` (exactly as `attribute.py` computes them), `server_timing[]`, `findings[]`,
  `regressions[]`, `field_data`, `needs_review[]`, `triage[]`, `limitations[]`, `next_steps[]`.

## What the validator enforces

Every statistic, status, candidate, rating and headline field is recomputed from the runs.
Unstable metrics never pass or make findings; low-confidence candidates are never findings. The
verdict follows the measurements and the summary says lab and avoids praise. INP appears only with
an interaction; Chromium-only metrics are `not_supported` elsewhere. Evidence files exist.
`scope.third_party` matches the run, and blocked third-party requests are named as not measured in
the limitations; blocked first-party requests are listed and named in the limitations; a remote
target has a limitation about the warm CDN; the run cap and variants match the config; a metric
unstable at the cap says so; `warm_cache` and `delivery` follow from the runs, and warm runs that
did not use the HTTP cache need a limitation saying so (warm and cache); so do INP untested on every
page, a loaded machine, a service worker active on any run, and a mobile profile run at device pixel
ratio 1 (`limitation_rules`). `--write-summary` fills
the computed headline fields, so a draft may leave them out. Field
data stays out of the measurements. web-vitals is the pinned build. The check is structural:
timings depend on the machine, so a rerun gives different numbers.
