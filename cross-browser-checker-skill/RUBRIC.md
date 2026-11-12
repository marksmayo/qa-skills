# Cross-browser checker rubric

`compare_engines.py` computes these; `validate_report.py` recomputes them from the runs.

## What counts as a difference

An observation (an error, a failed step, a missing element, low ink, an input type) is a difference
only when it holds in **every** run of the affected engine and in **no** run of the reference
engine, or the reverse. Anything else is flaky. The same observation in several engines, or in several
states of one page, is one candidate.

Dismissed automatically, with the reason recorded:

| Reason | Rule |
|---|---|
| Font metrics | Moved under 16 px, and width and height within 4 px or 5% |
| Native control rendering | An input, button, select or textarea drawn at another size (not moved over 16 px) |
| Consequence | A box that changed size because a difference inside it did; a box resized with its container (same width change); an element moved because a box above it grew; an input's box when its type fell back; an element inside another missing element |
| Cumulative drift | An element that only moved (no size change) when the nearest element above it, overlapping it horizontally, moved by nearly the same amount (within 16 px both ways): small text-metric differences add up down a long page. A step of more than 16 px between neighbours stays a difference, and what's below it moves with it |

Content not rendered: the element's ink (pixels more than 60 levels from its most common colour,
ignoring a 2 px border) is under 40% of the reference's in every run, and the reference shows at least 2%.

## Severity

| Severity | Weight | When |
|---|---|---|
| Critical | 4 | A scripted step fails; a page error that breaks the page (a failed step or missing element on the same page and engines) |
| High | 3 | A page error that doesn't visibly break the page; content not rendered; an element missing; an image that doesn't decode |
| Medium | 2 | Box differences, overflow, extra elements, failed requests, an input type fallback |
| Low | 1 | Console errors without a page error, and other cosmetic differences |

## Confidence

| Confidence | When |
|---|---|
| high | Repeated in every run and explained by an engine's error message or a linked feature-support gap |
| medium | Repeated in every run, not explained |
| low | Static-only (feature findings that no engine has shown), or the candidate matches a known Playwright engine-build limitation (`data/engine-limitations.json`: engine, platform, subkind and a pattern over the image source, element, detail and messages; a capture without a recorded platform matches every platform). These go to `needs_review`, never straight to a difference |

## Priority

`score = severity weight × usage band × reach`.

- **Usage band:** the global usage share of the affected engines' browser families (caniuse, by
  engine: Blink, Gecko, WebKit): under 5% is 1, 5–20% is 2, over 20% is 3. WebKit's share is used for
  Playwright WebKit as the nearest proxy, not because it is Safari.
- **Reach:** pages (1 per candidate; states of one page count once).
- P0 ≥ 24, P1 ≥ 12, P2 ≥ 6, otherwise P3. A failing step on a critical-path state is at least P1.

## Feature findings

A feature is reported when at least one resolved browserslist target lacks it (web-features gives no
version for that browser, or a later one). `fallback` is true when an `@supports` block or a pattern's
fallback check is in the same file. Status: `possible`, `confirmed_at_runtime` (linked to a difference
whose error message names the feature, or whose element matches the feature's CSS selector, in an
engine family that lacks it), or `not_assessed` (no data).

## Headline

- **Verdict:** `differences_found` with at least one promoted difference; otherwise
  `no_differences_detected`, never with an engine `not_run`.
- **Checks planned:** the (page or state, viewport) pairs in `runs.json`'s `planned` (per-page and
  per-state `viewports` honoured), or from `--targets`, or the pairs that ran for an older capture;
  times engines and reruns.
- **Evidence completeness:** `partial` when an engine didn't run, a check didn't run, anything is flaky
  or needs review, or a feature is not assessed.
- **Overall confidence:** medium when partial or any difference isn't high; otherwise high.
