---
name: performance-budget-auditor
description: Measure web pages in a real browser against a performance budget, as lab data. Core Web Vitals (LCP, CLS, INP) with web-vitals' attribution, TBT, FCP, TTFB and bytes per resource type, each page run several times under fixed network and CPU profiles, with median, range and coefficient of variation. Each over-budget metric is pointed at its cause (the LCP element and its resource, the layout shift's target, the scripts in long tasks, the slow interaction handler, the largest downloads and unused JavaScript, Server-Timing for slow responses), rated, and compared with a baseline when one is given. Never calls a site fast: the best verdict is "within budget in the lab, under the tested profiles; field performance not established".
version: 1.0.0
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
   variation (CV). A metric with CV above 0.25 after up to 10 runs is `unstable`: it neither passes
   nor fails the budget, and a candidate built on it goes to `needs_review`. A spread too small to
   matter for the budget is stable whatever its CV: max − min within 10 ms, 0.01 CLS or 1 KB, or
   every run at or under half the budget (TBT of 0–39 ms against 300 ms is not noise that matters).
3. **Medians decide budgets**, never the best run. Warm-up and diagnostic runs never count.
4. **Every number has a source**: run ids, the API that produced it (web-vitals, Long Tasks,
   DevTools Network, Server-Timing), and the trace.
5. **INP needs an interaction.** Without a scripted interaction INP is `untested`; TBT is the lab
   proxy and is labelled as one.
6. **The machine is part of the evidence**: CPU, cores, OS, browser version, the throttling actually
   applied, and a CPU calibration. Results from different machines are never compared without
   saying so.
7. **Lab and field never mix.** Field data, when given, is shown separately and labelled `field`.
8. **Safe targets.** Loopback needs nothing; any other host needs `authorised_by`. Non-GET requests
   are blocked unless `allow_form_submission` (the harness enforces it).
9. **Record every tool** as `ran`, `not_run` or `errored`; web-vitals by version and SHA-256.
10. **Third parties are blocked unless the plan allows them.** By default the harness aborts every
   request to a host that isn't a target, loopback or in `allowed_hosts` (analytics, ads, embeds,
   fonts from another host), and records each one. Their cost and behaviour are then **not
   measured**: the report's `scope.third_party` lists the blocked hosts and a limitation says so.
   Set `"third_party": "allow"` to measure them (never sends forms either way); list a CDN the site
   serves its own assets from in `allowed_hosts`.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `targets` | required | URLs with a template name and why each is in the sample (one per template plus critical paths) |
| `budget` | `data/default-budget.json` | This skill's format (`{"profiles": {name: {metric: value}}}`), a Lighthouse `budget.json`, or both (`"lighthouse": [...]`). Its `source` field names it in the report (a copy of the defaults keeps saying it is the defaults) |
| `profiles` | `mobile-mid`, `desktop` | `data/profiles.json` |
| `runs` | 5 | Per page, profile and cache mode, after one warm-up |
| `cache` | `cold` | `cold`, `warm` or `both` |
| `interactions` | none | Steps per page for INP (`[{"click": "#add"}]`) |
| `third_party` | `block` | `block` aborts and records requests to other hosts; `allow` loads and measures them |
| `allowed_hosts` | `[]` | Other hosts that are part of the site (its own CDN): `["cdn.example.com", "*.example-cdn.net"]` |
| `service_workers` | `cold` | `cold` blocks them on cold runs only; `block` on every run (a site whose worker would serve warm runs from its cache); `allow` |
| `authorised_by`, `auth`, `allow_form_submission` | as in `browser_harness.py` | |
| `baseline` | none | A previous run's `candidates.json`, for regression comparison |
| `field_data` | none | CrUX or RUM figures to show alongside, labelled field |

Profiles: `mobile-mid` is 412x915, 1.6 Mbps down, 750 Kbps up, 150 ms RTT, 4x CPU slowdown;
`desktop` is 1350x940, 10 Mbps, 40 ms RTT, no CPU slowdown. CPU slowdown is relative to the
measuring machine.

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

It records the machine and a CPU calibration (warning when its CV is above 0.1: the machine is
busy), then per page, profile and cache mode: a warm-up, N measured runs in fresh contexts, extra
runs while a budgeted metric's CV is above 0.25 (up to 10), and one diagnostic run with the trace,
filmstrip and JavaScript coverage. Cold runs block service workers (`"service_workers": "block"`
blocks them for warm runs too). Requests to third-party hosts are blocked and listed in
`blocked_requests` unless the plan says `"third_party": "allow"`. Interactions run after the page
settles, for INP.

### 4. Attribute and rate

```bash
python scripts/attribute.py <run>/measurements.json --budget budget.json --out <run>/candidates.json
```

It also records the budget file's path (the validator reads it when not given `--budget`) and the
third-party summary (`third_party`: mode, allowed hosts, blocked hosts with request counts), which
goes into the report's `scope.third_party` unchanged.

Statistics and statuses per metric, Server-Timing per route, and one candidate per (template,
metric, cause), rated by `RUBRIC.md`.

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

Watch for: consent banners (measure with and after dismissing them, and report both); bimodal
results from A/B tests (`bimodal: true`: report the two groups, don't trust the median); third
parties (blocked by default, so not measured: say so in the limitations and name the blocked
hosts; when the plan allows them they're reported by origin, and excluding them from the budget is
the user's call); mobile web (an emulated profile, not a real device; native apps are out of scope).

### 7. Write, validate, render

Write `<run>/perf_report.json` (`schemas/perf_report.schema.json`), then:

```bash
python scripts/validate_report.py <run>/perf_report.json --measurements <run>/measurements.json --candidates <run>/candidates.json \
  --budget budget.json [--regressions <run>/regressions.json] [--write-summary]
python scripts/render_report.py <run>/perf_report.json [--brief]
```

Fix every validator error; never edit the runs.

## Output fields

- `scope`: targets (template, reason, critical path, interaction), profiles, budget source and
  note (as in `candidates.json`), runs, cache modes, browsers, `authorised_by`, and `third_party`
  (as in `candidates.json`; required when the run recorded it).
- `metadata`: machine, calibration, tools (web-vitals with SHA-256, Playwright browsers,
  Lighthouse), and the measurements file.
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
the limitations. Field
data stays out of the measurements. web-vitals is the pinned build. The check is structural:
timings depend on the machine, so a rerun gives different numbers.
