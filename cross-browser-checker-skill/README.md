# Cross-Browser Checker Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It runs the same pages, states and journeys in Playwright's Chromium, Firefox and WebKit and reports
where they behave differently:

- **Runtime differences**: page and console errors, failing steps, content that isn't rendered,
  missing elements, input type fallbacks (no date control), layout, overflow, failed requests and
  undecoded images, each with screenshots and messages from both sides.
- **Repetition**: every check runs three times per engine; only differences that hold in every run of
  one engine and none of the reference's count. The rest are listed as flaky.
- **Noise handled**: font metrics, native controls, knock-on box changes and cumulative drift down
  a long page are dismissed automatically, with reasons; pages are scrolled before the full-page
  screenshot so lazy images and `content-visibility` sections aren't mistaken for missing content.
- **Build limitations**: differences that match a known gap in Playwright's engine builds (no AVIF
  in WebKit on Windows, no H.264 in Playwright's Chromium) are marked and sent for review in the
  real browser.
- **Third parties blocked by default**, and recorded; the report says their behaviour wasn't
  compared.
- **Static feature scan**: CSS, JavaScript and HTML checked against the project's browserslist targets
  with web-features and caniuse data, and linked to the runtime differences they explain.

It is strict about scope: it never says "compatible", never treats Playwright's WebKit as Safari, and
lists Safari, real devices and older versions as untested.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g.
`.claude/skills/cross-browser-checker/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/cross-browser-checker/`, or
include `SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/cross-browser-checker/`, and
reference `SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+. Capturing
needs Playwright with the three engines (`pip install -r requirements.txt`, `python -m playwright
install chromium firefox webkit`); the feature data comes from `python scripts/fetch_data.py`.
BrowserStack and similar clouds aren't integrated: with an account, run the same targets there.

## Usage

> Check our checkout pages in Chromium, Firefox and WebKit, and tell me what our browserslist targets can't handle.

> Just scan the repo for CSS and JS our targets don't support; no browsers.

```bash
python scripts/fetch_data.py
python scripts/capture.py targets.json --out run/capture
python scripts/feature_scan.py --repo . --out run/features.json
python scripts/compare_engines.py run/capture/runs.json --features run/features.json --out run/candidates.json
python scripts/validate_report.py run/report.json --runs run/capture/runs.json --candidates run/candidates.json --features run/features.json --targets targets.json
python scripts/render_report.py run/report.json [--brief]
```

## Example

`examples/travel-site/` is a small site with planted engine differences and a browserslist in its
`package.json`; `examples/targets.json` the pages and the recommendations journey; `examples/run/` the
real run (listed in `examples/run/commands.sh`) and `examples/report.example.json` the report.
`examples/README.md` has the answer key.

```bash
python scripts/validate_report.py examples/report.example.json --runs examples/run/capture/runs.json --candidates examples/run/candidates.json --features examples/run/features.json --targets examples/targets.json
python scripts/render_report.py examples/report.example.json
```

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: what counts as a difference, dismissals, severity, confidence, priority, features
- `schemas/cross_browser_report.schema.json`: output contract
- `data/data.lock.json`: caniuse-db and web-features versions, integrity and SHA-256
- `data/feature-patterns.json`: CSS, JS and HTML patterns mapped to web-features ids
- `data/engine-limitations.json`: known gaps in Playwright's engine builds (AVIF in WebKit on Windows, codecs in Chromium) that mark a candidate for review in the real browser
- `scripts/fetch_data.py`: download and verify the data
- `scripts/capture.py`: per-engine reruns with screenshots, messages, DOM boxes and steps
- `scripts/browserslist_lite.py`: resolve browserslist queries with caniuse data
- `scripts/feature_scan.py`: static feature scan against the targets
- `scripts/compare_engines.py`: differences, flaky, automatic dismissals, ratings, feature links
- `scripts/validate_report.py`, `scripts/render_report.py`, `scripts/schema_check.py`
- `scripts/browser_harness.py`, `scripts/png_io.py`: shared copies (edit them in `shared/`)
- `examples/`: the site, targets, run and report

## Design principles

1. Compare engines with each other; a bug every engine shares belongs elsewhere.
2. A difference must repeat; everything else is flaky.
3. WebKit is not Safari; say what wasn't tested.
4. Static findings are possibilities until an engine shows them.
