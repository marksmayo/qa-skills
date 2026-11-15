# Performance Budget Auditor Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It measures web pages in a real browser against a performance budget and says what causes each
overrun:

- **Core Web Vitals** (LCP, CLS, INP) through the web-vitals library's attribution build, plus TBT,
  FCP, TTFB and bytes per resource type.
- **Repeated runs** per page under fixed network and CPU profiles (`mobile-mid`, `desktop`), with
  median, min, max and coefficient of variation; noisy metrics get more runs, up to a cap the plan
  sets (`max_runs`), and stay `unstable` if they don't settle (a near-zero metric whose spread can't
  change the budget verdict, such as TBT of 17 ms against 300 ms, counts as stable, and so does one
  whose only noise is an isolated outlier or two that can't change the verdict; the outliers are
  listed). A page that answers with an HTTP error is measured once and stopped.
- **Variants and setup**: measure a page as a first visit and again with consent stored (or any
  storage, cookies or clicks before the measured load), so a consent banner that is the LCP
  doesn't hide the returning visitor's numbers.
- **Remote targets**: the CDN cache headers of every measured document, a limitation that the
  warm-up warmed the CDN, framework prefetches (Next.js `?_rsc=`) and client polling summarised
  apart from the largest downloads, and the site's own beacons and reports the harness blocked.
- **Site-wide causes**: the same script, stylesheet or banner on several templates is one finding.
- **Third parties blocked by default**: analytics, ads and embeds on other hosts are not loaded
  (the harness records each blocked request), so the numbers leave out their cost and the report
  says so; `"third_party": "allow"` in the plan measures them.
- **Attribution**: the LCP element and its resource with the load breakdown, the layout shift's
  target, the scripts in long tasks, the slow interaction handler, the largest downloads and their
  unused JavaScript, and Server-Timing for slow server responses.
- **A diagnostic run** per page with a DevTools trace, a filmstrip and coverage, kept out of the
  statistics.
- **A baseline comparison** that counts a change only when it's bigger than the noise.

It is strict about what lab numbers mean:

- It never calls a site fast. The best verdict is "within budget in the lab, under the tested
  profiles; field performance not established".
- Medians decide, not the best run; every number has its runs and its source.
- INP needs a scripted interaction; without one it's `untested`.
- The machine, the browser version and the throttling applied are recorded, and field data is
  never mixed with lab data.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g.
`.claude/skills/performance-budget-auditor/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/performance-budget-auditor/`,
or include `SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/performance-budget-auditor/`, and
reference `SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+. Measuring
needs Playwright (`pip install -r requirements.txt` and `python -m playwright install chromium`) and
web-vitals (`python scripts/fetch_assets.py`, which checks the npm integrity and SHA-256).
Lighthouse is optional and never required.

## Usage

> Check the shop's home, product and checkout pages against our budget.json on mobile and desktop.

> We shipped 2.4 yesterday; compare it with last week's run.

```bash
python scripts/fetch_assets.py
python scripts/measure.py plan.json --out run/measurements.json --artifacts run/artifacts
python scripts/attribute.py run/measurements.json --budget budget.json --out run/candidates.json
python scripts/compare.py baseline/candidates.json run/candidates.json --out run/regressions.json
python scripts/validate_report.py run/perf_report.json --measurements run/measurements.json --candidates run/candidates.json --budget budget.json --regressions run/regressions.json
python scripts/render_report.py run/perf_report.json [--brief]
```

On a machine you know is idle, run `measure.py` once with `--record-reference` to store its idle CPU
calibration; later runs are flagged busy (or loaded, from 1.2x) against it (a steady load looks calm
without one), and `--no-reference-update` measures without ever writing it. Warm runs keep the HTTP
cache (`"http_cache": "keep"`, the default for Chromium warm runs) and check that the second load
really used it (`warm_cache`); responses a service worker answered are counted apart, and what the
worker fetched itself is the budgetable `transfer_sw` (`service_workers`). Runs record compression and
cache headers and oversized images (`delivery`), the mobile profile emulates a device pixel ratio of
2.625, image, font, stylesheet and document weight can be budgeted, and what an interaction downloads
is the budgetable `transfer_interaction`. In Git Bash pass `--only about`, not `--only /about` (MSYS
rewrites a leading `/` into a Windows path; a filter that matches nothing exits 2).

`build_report.py` seeds the report from the run, the candidates and your triage (decisions, titles,
remediations, limitations), so only the judgement is written by hand:

```bash
python scripts/build_report.py --measurements run/measurements.json --candidates run/candidates.json --triage triage.json --out run/perf_report.json
```

## Example

`examples/bakery/` is a small site with planted problems: a 1.8 MB hero image as the LCP element,
a render-blocking font stylesheet, a late promo banner that shifts the page, a 400 KB script that
runs a long task and is almost all unused, a slow "Add to basket" handler, and a slow database step
on the menu pages. `examples/plan.json` and `examples/budget.json` are the inputs, `examples/run/`
the real run (listed in `examples/run/commands.sh`), and `examples/perf_report.example.json` the
report; `examples/README.md` has the answer key.

```bash
python scripts/validate_report.py examples/perf_report.example.json --measurements examples/run/measurements.json --candidates examples/run/candidates.json --budget examples/budget.json --regressions examples/run/regressions.json
python scripts/render_report.py examples/perf_report.example.json
python scripts/self_test.py   # calibration, warm cache, service workers, headers, DPR, image budgets, ties, seeder, INP and LCP checks
```

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: statistics and statuses, severity, confidence, reach, leverage, priority, comparison
- `schemas/perf_report.schema.json`: output contract
- `data/assets.lock.json`: web-vitals version, tarball integrity and SHA-256
- `data/default-budget.json`, `data/profiles.json`: the default budget and the profiles
- `scripts/fetch_assets.py`: download and verify web-vitals
- `scripts/measure.py`: repeated runs in Playwright with CDP throttling, web-vitals, Long Tasks,
  long animation frames, DevTools Network bytes, Server-Timing, traces, filmstrips and coverage
- `scripts/attribute.py`: statistics, statuses, Server-Timing, candidates and ratings (stdlib)
- `scripts/compare.py`: baseline comparison (stdlib)
- `scripts/build_report.py`: seed the report from the run, the candidates and the agent's triage
- `scripts/validate_report.py`, `scripts/render_report.py`, `scripts/schema_check.py`
- `scripts/browser_harness.py`: shared copy (edit it in `shared/`)
- `examples/`: the site, the plan, the budget, the run and the report

## Design principles

1. Lab data is lab data: say so, and never call it fast.
2. Report the spread, decide on the median, and refuse to judge what doesn't settle.
3. Every overrun names its cause and the evidence for it.
4. The machine is part of the measurement.
