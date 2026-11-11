---
name: visual-regression-reviewer
description: Compare current screenshots with a baseline set and turn the pixel differences into a short, ordered list of grouped causes for a person to review. Captures both sets itself (stabilised, two takes, with DOM boxes) or reads sets another tool produced, including Playwright toHaveScreenshot failures. Each group says what changed (layout shift, missing or new element, text, colour, image, font rendering, dynamic content), on which elements, pages and viewports, with cropped baseline, current and diff images, the measured change and a confidence. Noise is labelled, not hidden; intent is judged only against a given change description and labelled as judgment; nothing is ever approved by the skill.
version: 1.0.0
---

# Visual regression reviewer

## Mission

Do the review step that visual testing tools leave to people. Explain every difference between
two screenshot sets as a cause on named elements, group the same cause across pages and
viewports, separate likely noise from likely change, and hand the user an ordered list to accept
or reject. Say exactly what was compared, so "no differences" can't be read beyond its scope.

## Non-negotiable rules

1. **"No differences" is scoped.** Report the pages, states, viewports and browsers compared. Anything
   not captured is `untested`; a page in only one set is `added` or `removed`, never unchanged.
2. **Noise is labelled, not hidden.** `dynamic_content` and `font_rendering` groups are listed with
   their pixel counts and the reason they were classed that way.
3. **Every group has evidence**: baseline, current and diff crops (hashed), the element signatures
   and the measured change (pixels moved, colours, text before and after).
4. **Captures are repeated.** The current set is taken twice; pixels that differ between the takes are
   flaky, excluded from the comparison, and counted.
5. **Intent is judgment.** `likely_intended` / `likely_unintended` only against a given change
   context (PR, ticket, design note), always `agent_judgment` with low or medium confidence.
6. **Never approve.** The skill doesn't accept anything. `apply_review.py` copies accepted current
   images over baselines only when the user asks, from their own ticks in `review.md`, and only for
   pairs where every group is accepted. Nothing is committed.
7. **Safe targets.** Loopback needs nothing; any other host (production in `capture_both`) needs
   `authorised_by`. Non-GET requests are blocked by the harness.
8. **Record every tool** as `ran`, `not_run` or `errored`.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `mode` | `compare` | `compare` (two directories), `capture_and_compare`, `capture_both` (two URLs), `playwright` (a `test-results/` folder) |
| `baseline`, `current` | required in `compare` | Directories of PNGs matched by relative path |
| `targets` | required to capture | `targets.json`: sites, pages (with optional states), viewports, browsers, clock, ignore regions |
| `threshold` | 0.1 | pixelmatch's YIQ threshold |
| `min_region_pixels` | 20 | Smaller regions are counted, not grouped |
| `ignore_regions` | `{}` | Per page: `{"selector": sig}` or `{"rect": [x, y, w, h]}`, masked in both images |
| `change_context` | none | A PR description, diff or ticket, for intent |
| `authorised_by`, `auth` | as in `browser_harness.py` | |

## Workflow

Capture needs Playwright (`pip install -r requirements.txt`, `python -m playwright install
chromium`; ask first). Everything else is stdlib.

### 1. Capture (capture modes)

```bash
python scripts/capture.py targets.json --set baseline --out <run>/baseline
python scripts/capture.py targets.json --set current --out <run>/current      # two takes
```

Animations, transitions and the caret are disabled; it waits for network idle and fonts (retrying
once). Pass a `clock` in targets.json to freeze time, which removes clocks and rotating content.
Each capture saves the DOM boxes, so changes can be attributed to elements.

### 2. Diff

```bash
python scripts/diff_images.py --baseline <run>/baseline --current <run>/current --out <run>/diff.json --diffs <run>/diffs --ignore targets.json
python scripts/diff_images.py --playwright test-results --out <run>/diff.json --diffs <run>/diffs     # Playwright failures
```

### 3. Attribute and group

```bash
python scripts/group.py <run>/diff.json --out <run>/groups.json --crops <run>/crops --review <run>/review.md --targets targets.json
```

`attribute.py` turns each pair into element changes from the two DOM snapshots and assigns every
pixel to exactly one change (the smallest box containing it); `group.py` groups the same cause on
the same elements across pairs, rates each group (`RUBRIC.md`), cuts the crops and writes
`review.md`. Without DOM data (external screenshots) only regions are available: `font_rendering`
when anti-aliasing dominates, otherwise `unexplained`, with lower confidence.

### 4. Judge intent and write the review

For each group write an `explanation` (what moved or changed and what it follows from, such as a
shift caused by a removed element) and, when there is a change context, an `intent`. Without
context every intent is `unknown`. Set `review_order`: likely unintended first, then unknown,
then likely intended, each by review score. Watch for cross-OS baselines (a spike of
`font_rendering`: say baselines should come from the same OS and browser) and size changes (a
`size_mismatch` is a change in page height).

### 5. Validate and render

```bash
python scripts/validate_review.py <run>/review.json --groups <run>/groups.json --diff <run>/diff.json [--write-summary]
python scripts/render_report.py <run>/review.json [--brief]
```

### 6. Only if the user asks: apply their decisions

```bash
python scripts/apply_review.py <run>/review.md --groups <run>/groups.json --diff <run>/diff.json [--dry-run]
```

List the files it wrote in `files_written`, and say in `limitations` that apply_review.py wrote them
at the user's request.

## Output fields

- `scope`: mode, the two sets with manifest hashes, browsers, viewports, pages, ignore regions,
  threshold, change context, `authorised_by`.
- `metadata`: browser versions, tools, pair counts (matched, added, removed, size mismatch).
- `headline`: summary, groups by impact and by cause, pairs and pages compared, pages untested, flaky
  pixels excluded, evidence completeness, overall confidence, and `approval: "none"`.
- `groups[]`: cause, impact, confidence, reach, review score and order, noise, intent (with basis and
  confidence), elements, pages, viewports, browsers, measure, pixel counts, crops, explanation.
- `unchanged[]`, `added[]`, `removed[]`, `untested[]`, `files_written[]`, `limitations[]`.

## What the validator enforces

Groups match `groups.json`; crops exist and match their hashes; every changed, anti-aliasing and
flaky pixel of every pair is in a group or the ungrouped count; nothing in one set is unchanged;
intent is judgment with a context and never high confidence; nothing claims approval; review order
follows intent and score; the headline is recomputed, and "no changes" must name what was untested.
