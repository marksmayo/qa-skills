# Example: travel-site

`travel-site/` is a small static site (served by `travel-site/serve.py`) whose `package.json` sets
the browserslist `defaults, firefox >= 115, safari >= 14`. `targets.json` captures the home page (and
its recommendations journey, a critical path), the deals page and the about page at 1280x800 in
Chromium 158, Firefox 159 and WebKit 27.2 (Playwright 1.65 on Windows), three runs each. `run/` is
the real run (listed in `run/commands.sh`); `report.example.json` the report.

## Answer key

| Planted | Expected | Got |
|---|---|---|
| `app.js` reads `navigator.userAgentData.brands` with no feature check | Page error in Firefox and WebKit; the journey step that depends on it fails | D2 (page error, Critical, linked to the `ua-client-hints` finding, now confirmed at runtime) and D1 (the recommendations step fails in all 3 runs of each, Critical, P1). D4: the list it builds is missing |
| Date input styled `color: transparent` with its text drawn only by `::-webkit-datetime-edit` | Content hidden outside Chromium | D3: `#depart` shows 0% ink in Firefox and WebKit against about 11% in Chromium; linked to the `::-webkit-` pseudo-element finding |
| (not planted) | | D5: WebKit on Windows has no date control; `<input type="date">` falls back to a text field |
| `.card:has(.sale)` with no fallback | `:has()` unsupported in Firefox 115-120 | Feature finding, `possible`: every engine here supports it |
| Flex `gap` | Unsupported in Safari 14 (14.1 has it) | Feature finding, `possible` |
| A WebP image on the deals page | The design expected WebKit on Windows not to render it | It decoded in all three engines (480x200), so it is not a difference, and every target supports WebP |
| A banner shown on about half of all loads | Flaky | Listed as flaky: Chromium 3/3, Firefox 1/3, WebKit 2/3 in this run; not a difference |
| The about page | No differences | None |

42 box differences were dismissed automatically (14 font metrics, 8 native form controls, 20
consequences of other differences); `candidates.json` lists each with its reason.

```bash
python scripts/validate_report.py examples/report.example.json --runs examples/run/capture/runs.json --candidates examples/run/candidates.json --features examples/run/features.json --targets examples/targets.json
python scripts/render_report.py examples/report.example.json
```
