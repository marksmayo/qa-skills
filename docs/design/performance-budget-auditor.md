# Design: `performance-budget-auditor-skill`

Status: Built (v1.0.0). Build order: 17.

## Mission

The skill measures web pages in a real browser against a performance budget: Core Web Vitals
(LCP, CLS, INP), supporting timings (TTFB, FCP, TBT), and resource weight per type. It runs each
page several times under fixed network and CPU profiles, reports the median and the spread, and
points each over-budget metric at what caused it (the LCP element, long tasks, the scripts that
ran them, the largest downloads).

How it differs from the existing skills:

- **wcag-auditor** and **ui-crash-hunter** look for barriers and crashes. This skill looks for
  slowness and weight, and its findings are budget overruns, not defects in a pass/fail sense.
- It reuses the same browser runner (`browser_harness.py`) and the same safety rules for targets
  and forms.

## Decisions

- **Lab data, not field data.** Everything is measured in a controlled browser. The report says
  so in its headline, and never presents a result as what real users experience. If the user
  provides CrUX or RUM numbers, they're shown alongside, labelled as field data, and never mixed
  into the lab statistics.
- **Measurement is Playwright plus the DevTools protocol plus `web-vitals`.** The `web-vitals`
  library (Apache-2.0) is injected into the page to collect LCP, CLS and INP the same way Chrome
  defines them. Traces come from CDP `Tracing` for long tasks and their scripts.
- **Lighthouse is optional** (Apache-2.0, needs Node). When present, its audits are added as
  supporting evidence; it never replaces the skill's own repeated runs.
- **Chromium only for Web Vitals.** LCP, CLS and INP are Chromium APIs. Firefox and WebKit runs
  are available for load timings and weight, and the report marks the vitals there as
  `not_supported`.
- **Default runs per page: 5**, after one discarded warm-up run.

## Non-negotiable rules

- **Never "fast".** The verdict is `over_budget` or `within_budget_in_lab`. The second always
  carries "under the tested profiles; field performance not established".
- **Variance is reported.** Every metric has median, min, max and the coefficient of variation
  (CV). A metric with CV above 0.25 is `unstable`: it can't pass or fail the budget on its own,
  and the skill reruns it up to 10 times before giving up.
- **Medians decide budgets**, not the best run.
- **Every number has a source:** run id, trace file, and which API produced it.
- **INP needs an interaction.** INP is measured only on pages with a scripted interaction (a
  state, as in the WCAG auditor). Pages without one get INP `untested`, and TBT is shown as a lab
  proxy, labelled as a proxy.
- **The machine is part of the evidence.** CPU model, core count, OS, browser version and the
  throttling actually applied are recorded. Results from different machines are never compared
  without saying so.
- **Tools are recorded** as `ran`, `not_run` or `errored`.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `targets` | required | URLs, or a start URL and crawl limit (sampling as in the WCAG auditor: one per template plus critical paths) |
| `budget` | built-in defaults | A `budget.json` file; the format is a superset of Lighthouse's `budget.json` |
| `profiles` | `mobile-mid`, `desktop` | Network and CPU throttling sets, see below |
| `runs` | 5 | Per page and profile, plus one warm-up |
| `cache` | `cold` | `cold`, `warm` or `both` |
| `interactions` | optional | Scripted steps per page for INP |
| `authorised_by`, `auth`, `allow_form_submission` | as in `browser_harness.py` | |
| `baseline` | optional | A previous report, for regression comparison |
| `field_data` | optional | CrUX or RUM numbers to display alongside |

Built-in budget (Google's "good" thresholds for vitals, weight figures chosen as a starting
point and labelled as this skill's defaults):

| Metric | `mobile-mid` | `desktop` |
|---|---|---|
| LCP | 2.5 s | 2.5 s |
| CLS | 0.1 | 0.1 |
| INP | 200 ms | 200 ms |
| TBT | 300 ms | 200 ms |
| Total transfer | 1.6 MB | 2.5 MB |
| JavaScript transfer | 400 KB | 600 KB |

Profiles: `mobile-mid` is a 412x915 viewport, 1.6 Mbps down, 750 Kbps up, 150 ms RTT and 4x CPU
slowdown; `desktop` is 1350x940, 10 Mbps, 40 ms RTT and no CPU slowdown. CPU slowdown is
relative to the measuring machine, so the report records the machine.

## Workflow

1. **Scope and sample.** Record targets, profiles, budget and the reason each page is sampled.
2. **Check tooling.** Browser version, `web-vitals` version and hash, Lighthouse or `not_run`.
3. **Calibrate.** Run a fixed CPU benchmark page three times and record the score. If the
   machine is busy (CV above 0.1 between calibration runs), warn and record it.
4. **Measure.** For each page x profile x cache mode: one warm-up, then N runs in a fresh
   context each, with throttling, the trace, `web-vitals` callbacks, the resource timing list and
   a filmstrip (screenshots at 100 ms intervals until load).
5. **Interact** where an interaction is scripted, for INP.
6. **Attribute.** For each over-budget metric, find the cause: LCP element and its resource
   (load delay, load time, render delay breakdown); CLS sources with their elements; long tasks
   over 50 ms with their scripts; the largest and render-blocking resources; unused JavaScript
   from CDP coverage. For slow TTFB, the server-side breakdown by route comes from
   `Server-Timing` response headers when the server sends them, read passively (no extra
   requests).
7. **Compare** with the baseline when given: a change counts only when the medians differ by
   more than the larger of 10% and the two runs' combined spread.
8. **Candidates and triage.** One candidate per (page template, metric, cause). Each is promoted,
   sent for review, or dismissed with a reason (for example the slow resource is a third-party
   embed the user excluded).
9. **Output, validate and render.**

## Rating

- **Severity** by how far the median is over budget: Critical at 2x or more, or LCP over 4 s or
  CLS over 0.25 (Google's "poor"); High at 1.5x; Medium at 1.2x; Low when just over.
- **Confidence:** `high` when CV is at most 0.1 and all runs are over budget; `medium` when CV is
  at most 0.25 and the median is over; `low` otherwise (such findings are `needs_review`, never
  findings).
- **Priority:** severity weight (4/3/2/1) x reach (pages sharing the template or resource: 1, 2, 3)
  x leverage (2 when one fix, such as one script, covers several metrics or pages). P0 >= 24,
  P1 >= 12, P2 >= 6. Critical-path pages raise severity one level.

## Output (`perf_report.schema.json`)

```text
scope:     targets, profiles, budget{source, metrics}, runs, cache, authorised_by
metadata:  browser, machine{cpu, cores, os, calibration}, tools[{name, version, sha256?, status}],
           sample[{url, template, reason}]
headline:  verdict: over_budget|within_budget_in_lab, data_kind: "lab", over_budget_counts,
           unstable_metrics, evidence_completeness, overall_confidence
measurements[]: {url, profile, cache, metric, runs[], median, min, max, cv, budget, status:
                 within|over|unstable|untested|not_supported}
server_timing[]: {route, ttfb_median, entries[{name, dur_median, desc?}]}   # from Server-Timing headers
findings[]: {id, metric, pages[], severity, confidence, priority, median, budget, attribution{...},
             evidence[{trace, filmstrip, run_ids}], remediation, verification_step}
regressions[], field_data?, needs_review[], triage[], limitations[]
```

## Validator fails when

- A measurement has fewer runs than configured without a recorded reason, or its median, CV,
  min or max don't match its runs.
- An `unstable` metric is reported as `within` or used for a finding.
- The verdict is `within_budget_in_lab` while any measurement is `over`, or the text says "fast",
  "performant" or similar.
- Field data appears in `measurements`, or `data_kind` isn't `lab`.
- INP is reported for a page with no interaction.
- A candidate is missing from `triage`.

## Edge cases

- **Consent banners** delay LCP: measure with the banner (as real first visits do) and also
  after dismissal, reporting both.
- **A/B tests and personalisation** cause bimodal results: when runs split into two clusters, the
  skill says so instead of averaging them.
- **Service workers** make "cold" runs warm: they're disabled for cold runs and allowed for warm.
- **Ads and third parties:** reported separately by origin; the user can exclude them from the
  budget but they're still measured.
- **Mobile web:** covered by the `mobile-mid` profile and viewport. It isn't a real device, and
  the report says so. Native apps are out of scope.

## Files

```text
performance-budget-auditor-skill/
  SKILL.md  RUBRIC.md  README.md  requirements.txt (playwright)
  data/assets.lock.json         # web-vitals version, URL, SHA-256
       default-budget.json  profiles.json
  schemas/perf_report.schema.json
  scripts/fetch_assets.py       # download and verify web-vitals
          measure.py            # Playwright + CDP; uses browser_harness.py
          browser_harness.py    # shared copy
          attribute.py          # trace and resource analysis (stdlib)
          compare.py  validate_report.py  render_report.py
  examples/
```

## Example

`examples/bakery/` is a static site served on loopback with planted problems and an answer key:
a 1.8 MB hero image as the LCP element without `fetchpriority` or sizing, a render-blocking web
font, a 400 KB unused script that runs a 300 ms long task, a late-inserted banner that shifts
content (CLS), and a slow click handler for INP. One page is planted within budget. Because lab
numbers depend on the machine, the example's validator check is structural (statistics match
runs, rules hold), not exact timings.

## Resolved questions

Agreed with the user before the build.

1. **Budgets?** Default weight budgets, overridden by the user's budget file when given.
2. **Lighthouse in the default stack?** No: optional.
3. **Server-side timing breakdown?** Yes: Server-Timing headers and TTFB by route, read passively.

## As built (v1.0.0)

Where the build differs from the design above, and what the example showed.

- **Diagnostic runs are separate.** The trace (with screenshots), the filmstrip and JavaScript
  coverage come from one extra run per page and profile that no statistic counts, because tracing
  and coverage slow the page. The saved trace drops screenshots (kept as the filmstrip) and tasks
  under 1 ms to stay small; it still opens in DevTools.
- **Transfer bytes come from DevTools Network events** (`encodedDataLength`) in Chromium, and from
  Resource Timing in Firefox and WebKit. Long tasks feed TBT; long animation frames name the
  scripts.
- **INP:** web-vitals reports INP only once the browser is idle, so the runner polls for up to 3 s
  after the interaction and falls back to Event Timing entries, recording which source gave the value.
- **Leverage** is 2 when the same cause (a URL, an element, a server step) is behind candidates for
  more than one metric. **A Critical candidate is at least P1**: without that floor, a 14x INP on
  the critical ordering page came out P3.
- **TTFB is keyed by the Server-Timing entry**, so the same slow step on several pages is one candidate.
- **Budgets:** the skill's format, a Lighthouse `budget.json`, or both in one file (`"lighthouse"`).
- **The comparison** has a floor (10 ms, 0.01 CLS, 1 KB) so tiny metrics near zero don't count as
  regressions, and flags comparisons across machines.
- **The shared harness** gained custom network profiles, blocked service workers for cold runs,
  mobile emulation, the page's CDP session and trace recording.
- **Not built:** warm-cache runs in the example (the runner supports them), consent-banner
  dismissal (a scripted step does it), and Lighthouse integration (recorded `not_run`).
- **Example results** (one Windows machine, 20 cores; Chromium 158, Firefox 159; web-vitals 6.2.3):
  - 16 of 88 measurements over budget; 7 findings, all high confidence; nothing unstable.
  - LCP 10.3 s on mobile from the 1.8 MB hero (2.25 s in the 1.0 baseline); INP 2.8 s on the
    ordering page; TBT about 4 s from analytics.js on both menu pages; TTFB 456 ms from a 450 ms
    `db` step; CLS 0.13 from the banner; /about within on both profiles.
  - The /order mobile diagnostic run timed out on its click while tracing; it is recorded, not rerun.
- **Testing:** 33 of 33 seeded report violations are caught.
- **Found on a real static site, fixed:**
  - *Near-zero metrics counted as unstable.* TBT with a 17 ms median and a 39 ms max against a
    300 ms budget, and CLS of 0.0027 against 0.1, had CVs far above 0.25, which forced ten runs and
    "partial" evidence. A metric is now stable when its CV is within 0.25, its spread is within the
    comparison's floor (10 ms, 0.01 CLS, 1 KB), or every run is at or under half the budget;
    `attribute.is_stable` decides it for `measure.py`'s extra runs, the statuses and the validator.
    Confidence for an over-budget measurement is high or medium, since over implies stable.
  - *The renderer dropped query strings*, so `/search` and `/search?q=x` looked like one page; it
    now shows path and query everywhere.
  - *A copied default budget was recorded by its file name*, and a validator run without
    `--budget` expected the built-in label. The budget's `source` is now the file's own `source`
    field when present (the file name goes in `note`), `candidates.json` records the budget path,
    and the validator reads that path when `--budget` isn't given.
  - *Third parties.* The shared harness now blocks requests to third-party hosts by default. The
    plan takes `third_party`, `allowed_hosts` and `service_workers` (`cold`, `block`, `allow`);
    the measurements record the mode and every blocked request, `attribute.py` summarises the
    blocked hosts, the report carries it as `scope.third_party`, and the validator requires a
    limitation saying their cost and behaviour were not measured. The example predates this, so
    its run records no third-party mode (the bakery has no third parties).

### Found on a real remote deployment (v1.1.0)

A run against a Next.js site on a CDN (six pages, two profiles, third parties blocked) showed where
a loopback-tuned design breaks:

- *The run cap.* `MAX_RUNS = 10` was hard-coded, so a plan of "5 runs" became 7 to 10 measured
  runs per page (111 loads instead of about 84). The plan now takes `max_runs` (default 10; equal
  to `runs` for no extra runs) and `min_runs` (stop early when everything is stable), the config
  records both, every measurement records `runs_used` and `run_cap`, a metric still unstable at
  the cap says "unstable at the cap of N runs", and the renderer prints runs planned, cap and used
  instead of a fixed "5 runs".
- *Remote network outliers.* Two slow round trips out of ten (TTFB 859 and 1050 ms against
  ~200 ms) made TTFB, FCP and LCP unstable although the median was unambiguous. A fourth
  stability rule, `robust`, sets aside at most a quarter of the runs that sit more than 3 robust
  standard deviations from the median, when the rest are stable and their median is on the same
  side of the budget; the outlier run ids are listed in the measurement. Interleaving pages
  round-robin was considered and not built: the robust rule addresses the verdict, and run order
  stays simple to read.
- *CDN state was invisible.* Each run records the document's cache headers (`x-vercel-cache`,
  `cf-cache-status`, `x-cache`, `age`, `via`, ...), `attribute.py` summarises them as `cdn_cache`,
  and a report on a non-loopback target must have a limitation saying "cold" meant a cold browser
  and a warm CDN (the warm-up primes it).
- *An HTTP 500 page spent the whole budget* and the reason was lost. A run that gets a status of
  400 or more (other than 429/503, which are retried after Retry-After) stops that page: no extra
  runs, no diagnostic; the run records `stop_reason`, and failed runs are listed in the note with
  their HTTP status.
- *Consent banners* were advice only, and the banner was the first-visit LCP on every page. Targets
  take `setup` (local/session storage written by an init script before page scripts, cookies, and
  actions on a first load after which the cold run's HTTP cache is cleared through DevTools) and
  `variants` (named states, each its own measurement and run-id segment). An LCP element inside a
  `position: fixed` overlay, or a text LCP painted more than 500 ms after load, is named so in the
  cause.
- *Site-wide causes were split per template* (the banner, the shared chunk and the global
  stylesheet made 12 candidates for 4 problems). Candidates are now keyed by (metric, cause) when
  the cause is specific (a URL, a server step, an element with an id or class), with `templates`
  and `site_wide`; reach counts every page.
- *Blocked first-party writes* (814 CSP reports and telemetry posts) were in the measurements but
  nowhere in the report. `scope.blocked_first_party` lists them and the validator requires a
  limitation.
- *Next.js RSC prefetches and client polling* were hidden by the 12-largest-resources cut. Each
  run records a `request_summary` (`rsc_prefetch` by `?_rsc=` or the RSC request header, `fetch`,
  and fetch paths repeated within the load), summarised as `request_summary`.
- *The filmstrip ended at load*, before a late LCP; it now runs to the later of load and the final
  LCP plus 500 ms (at most 20 s).
- *A failed interaction discarded the run.* The interaction is caught on its own: the run keeps its
  load metrics, records `interaction_error`, and the INP measurement's note lists the failures.
- The example's measurements predate these fields (no variants, CDN headers or request summaries,
  config without `min_runs`); `attribute.py` reads them with the defaults, and the regenerated
  candidates and report carry the new fields. The new paths were checked against a loopback
  fixture with a delayed consent banner, a 500 page, RSC prefetches, polling and a beacon.
- *Shared harness (merged later).* `measure.py` uses the harness's `retry_decision` for 429/503
  (and `stop_on_429` from the plan), measures with the site's CSP enforced unless the plan sets
  `bypass_csp`, and records `h.metadata()` with the request totals as `measurements.harness`,
  which the report copies to `metadata.harness`. Blocked first-party requests use each blocked
  entry's `site` (same_site) when present. The validator requires a limitation when the CSP was
  bypassed or the request budget ran out, and a spent budget makes the evidence partial.
