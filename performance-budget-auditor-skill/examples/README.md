# Example: the bakery site

`bakery/` is a small static site (release 1.1) with planted performance problems; `plan.json`
samples five pages over four templates and `budget.json` is the budget (the skill's defaults plus a
Lighthouse-format TTFB budget for `/menu*`). `run/` is the real run, listed in `run/commands.sh`:
the baseline (release 1.0, home and about), the current measurements with traces, filmstrips and
coverage, the candidates and the comparison. `perf_report.example.json` is the report.

The numbers were measured on one Windows machine (recorded in the report) with Playwright 1.65
(Chromium 158, Firefox 159) and web-vitals 6.2.3. On another machine every value differs; the
validator checks the structure, not the timings.

## Answer key

| Planted | Expected | Got |
|---|---|---|
| 1.8 MB hero image as the LCP element, no `fetchpriority`, no size attributes | LCP over on mobile, Critical; weight over | F2: LCP 10.3 s on mobile-mid (10.1 s of it the download), 1.76 s on desktop (within). F6: 1.87 MB against 1.6 MB on mobile-mid. Both share the cause, so leverage 2 |
| Render-blocking `fonts.css` (served after 300 ms) | Shown as a cause | Listed as render-blocking (329 ms) in F2's attribution. FCP has no budget here, so it is not a finding of its own |
| Promo banner inserted above `<main>` after 1 s | CLS over | F7: 0.131 on mobile-mid, 0.118 on desktop, both moving main and footer |
| `analytics.js`: a long task at load, almost all of it unused | TBT over on the menu template, JS weight over | F1: TBT 4.0 s on mobile-mid and 266 ms on desktop, reach 2 (both menu pages), Critical. F5: 400,192 bytes against 400,000 (just over, Low); coverage shows 390 KB unused |
| Slow Add to basket handler on the ordering page (critical path) | INP over | F3: 2.79 s on mobile-mid, 264 ms on desktop; all of it processing in `BUTTON#add.onclick` |
| `db` step of about 450 ms on the menu pages | TTFB over against the user budget, attributed by Server-Timing | F4: 456 ms against 300 ms on both profiles, `db` 450 ms (menu query) |
| `/about` | Within budget | Within on both profiles; Firefox measured for load timings and weight, vitals `not_supported` |
| Release 1.0 had a 290 KB hero | Regression on home | LCP 2.25 s to 10.29 s on mobile-mid and 420 ms to 1.76 s on desktop, weight up 435%, desktop CLS 0.087 to 0.118 |

INP is `untested` on every page except `/order`, the only one with a scripted interaction. The
mobile diagnostic run for `/order` timed out on its click while tracing at 4x CPU, so that profile
has no trace; its five measured runs completed.

```bash
python scripts/validate_report.py examples/perf_report.example.json --measurements examples/run/measurements.json --candidates examples/run/candidates.json --budget examples/budget.json --regressions examples/run/regressions.json
python scripts/render_report.py examples/perf_report.example.json
```
