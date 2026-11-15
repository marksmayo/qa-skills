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
| SVG stroke | An SVG shape (circle, rect, path, ...) whose box grew evenly on every side by its stroke width (one engine includes the stroke in its box) |
| Decoded photo | Low ink on an `<img>` the engine decoded (natural size) and drew with 5% ink or more: the ink metric on photographic content |
| Benign request | A media or image request the engine or page cancelled (`ERR_ABORTED`, `NS_BINDING_ABORTED`: a srcset candidate swapped, `/_next/image` replaced) and Firefox's `NS_ERROR_PARSED_DATA_CACHED`, listed in `benign_requests` |

A difference's runs record whether it held in each run: the reference's are all false, the affected
engine's all true (a box difference too), so a promoted box difference passes the validator's check
that the two sides differ.

Runs that captured but can't be compared are screened before comparing (`screen_runs`), recorded
`errored` with the reason, and their comparisons are not made: a run whose own requests to the site
were upgraded to https and failed (an environment cause: a CSP `upgrade-insecure-requests` on a
plain-http target; one `environment` entry, never one difference per request), and a blank or
incomplete rerun (under half its sibling reruns' median element count and 20 or more fewer, or a
one-colour screenshot when theirs aren't). One bad load is then one comparison not made, never
hundreds of flaky entries.

Content not rendered: the element's ink (pixels more than 60 levels from its most common colour,
ignoring a 2 px border) is under 40% of the reference's in every run, and the reference shows at least 2%. When the reference shows
under 2%, the other engines are compared with each other the same way (the one that shows ink is the
reference for that candidate). Runs laid out at different effective viewports (fullscreen) are not
compared for layout at all.

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
- **Reach:** pages: 1, 2, or 3 for three or more (states of one page count once). A layout
  difference of one element (by its signature, such as a shared header) on several pages is one
  candidate, and reach counts its pages; so is an undecoded image of one component and one console
  message on several pages. Repeated siblings (`:nth-of-type(N)`, `:nth-child(N)`) are one element, and
  the viewports a cause shows at are one candidate (`viewports`, `occurrences`); other differences are
  one candidate per page.
- P0 ≥ 24, P1 ≥ 12, P2 ≥ 6, otherwise P3. A failing step on a critical-path state is at least P1.

## Feature findings

A feature is reported when at least one resolved browserslist target lacks it (web-features gives no
version for that browser, or a later one). `fallback` is true when an `@supports` block or a pattern's
fallback check is in the same file. Status: `possible`, `confirmed_at_runtime` (linked to a difference
whose error message names the feature, or whose element matches the feature's CSS selector, in an
engine family that lacks it), or `not_assessed` (no data). Utility classes (Tailwind) count as uses
of the feature they generate; a pattern with `emits_fallback` (the framework writes a prefixed
alternative too) has a fallback. In minified built CSS only the `@supports` block around a use counts,
and the guarded and unguarded uses of one line are separate entries (the unguarded first, with
`guarded` counting the others), so the snippet shown is the use without a fallback.

## Headline

- **Verdict:** `differences_found` with at least one promoted difference; otherwise `inconclusive`
  when any planned engine-versus-reference comparison wasn't made (an engine `not_run` or `errored`,
  or a run of either side errored, skipped or screened: a comparison needs every rerun captured on
  both sides), or no comparison was made at all (an empty plan, a static-only report); otherwise
  `no_differences_detected`, or the more cautious `inconclusive` (candidates held for review, flaky
  entries), whose summary must say why. No comparison made is never "no differences". The verdict is
  the agent's: `--write-summary` fills the other headline fields and never rewrites it.
- **Run status:** `complete` when every planned run ran and every comparison was made, `partial`
  otherwise (with the reason), `not_run` for a static-only report (feature scan only: every engine
  `not_run`, verdict `inconclusive`, confidence low, every feature finding `possible` or
  `not_assessed`).
- **Comparisons:** planned (page or state, viewport) pairs times the engines other than the
  reference, and how many were made.
- **Checks planned:** the (page or state, viewport) pairs in `runs.json`'s `planned` (per-page and
  per-state `viewports` honoured), or from `--targets`, or the pairs that ran for an older capture;
  times engines and reruns.
- **Evidence completeness:** `partial` when an engine didn't run, a check or comparison didn't run,
  anything is flaky or needs review, or a feature is not assessed.
- **Overall confidence:** low when no comparison was made, or fewer than half of the planned
  comparisons or runs; medium when partial or any difference isn't high; otherwise high.
