# Design: `performance-budget-auditor-skill`

Status: Designed; not built. Build order: 17.

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
