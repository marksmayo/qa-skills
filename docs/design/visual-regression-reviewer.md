# Design: `visual-regression-reviewer-skill`

Status: Designed; not built. Build order: 18.

## Mission

The skill compares a set of current screenshots with a baseline set and turns hundreds of pixel
differences into a short list of grouped causes for a person to review. Each group says what
changed (layout shift, missing element, colour, text, font rendering, dynamic content), where,
on which pages and viewports, and how sure the skill is. It can capture both sets itself, or
take sets that another tool (Playwright, BackstopJS, Storybook test runners) already produced.

How it differs from the existing skills:

- Visual testing tools already flag pixel differences. This skill does the review step they
  leave to people: it explains and groups them, and separates likely noise from likely change.
- It never approves anything. Accepting a change is always the user's decision.
- It shares the browser runner and PNG decoder with the WCAG auditor (`browser_harness.py`).

## Decisions

- **Pixel diffing is stdlib Python** (the shared PNG decoder, extended with an encoder for diff
  images). No ImageMagick, OpenCV or pixelmatch is required. A per-pixel colour distance in
  YIQ space, like pixelmatch's, with an anti-aliasing check, decides which pixels changed.
- **DOM data makes the groups.** When the skill captures screenshots itself, it also saves each
  visible element's bounding box, text and computed styles. Changed pixels are attributed to
  elements, and elements are matched between baseline and current by a stable signature (the
  WCAG auditor's `sig` approach). Without DOM data (external screenshots), grouping falls back
  to connected regions only, and confidence drops.
- **Never auto-approve.** The skill doesn't mark anything accepted. It writes a review file the
  user can act on. Only when the user explicitly asks does it read the decisions in `review.md`
  and copy the accepted current images over their baselines, leaving the change uncommitted and
  listed in the report; rejected and undecided groups are never touched.
- **Stability first.** Captures disable animations and caret blink, wait for fonts and network
  idle, and freeze time when asked (`clock` in Playwright).

## Non-negotiable rules

- **"No differences" is scoped.** The report says which pages, states, viewports and browsers
  were compared. Anything not captured is `untested`; a page that exists only in one set is
  `added` or `removed`, not a pass.
- **Noise is labelled, not hidden.** A group classed as `rendering_noise` or `dynamic_content`
  is still listed, with its pixel count and the reason it was classed that way.
- **Every group has evidence:** baseline, current and diff images (cropped to the region plus a
  margin), the element signatures involved, and the measured change (pixels moved, colour
  before and after, text before and after).
- **Captures are repeated.** Each current screenshot is taken twice; pixels that differ between
  the two takes are flaky and excluded from the comparison, and their count is reported.
- **Agent judgment is labelled.** Deciding whether a change is intended (matches a ticket or
  design) is `agent_judgment`, never high confidence.
- **Tools are recorded** as `ran`, `not_run` or `errored`.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `mode` | `compare` | `compare` (two sets given), `capture_and_compare` (capture current, baseline given), `capture_both` (two URLs, such as production and staging; a production URL needs `authorised_by`), `playwright` (read a `test-results/` folder of `toHaveScreenshot` failures: `*-expected.png`, `*-actual.png`, `*-diff.png`) |
| `baseline`, `current` | required in `compare` | Directories of PNGs, matched by relative path |
| `targets` | required to capture | URLs, states and viewports as in the WCAG auditor config |
| `browsers` | `chromium` | Also `firefox`, `webkit` |
| `ignore_regions` | `[]` | Selectors or rectangles per page; masked in both images |
| `threshold` | 0.1 | Per-pixel YIQ distance, as pixelmatch's default |
| `min_region_pixels` | 20 | Smaller regions are counted but not grouped |
| `change_context` | optional | A diff, PR description or ticket, used to judge whether a change was intended |
| `authorised_by`, `auth` | as in `browser_harness.py` | |

## Workflow

1. **Pair images.** Match baseline and current by path; list `added`, `removed` and size
   mismatches (a size change is itself a finding about page height or viewport).
2. **Capture** (capture modes): two takes per page x state x viewport x browser, plus DOM boxes.
3. **Stabilise.** Exclude pixels that differ between the two takes; apply ignore regions.
4. **Diff.** Changed pixels with the anti-aliasing check; connected regions; diff images.
5. **Attribute.** Map regions to elements using the boxes from both sets. Detect moves (the
   same element, same pixels, different position), resizes, text changes, colour changes, and
   elements present in only one set.
6. **Classify** each region into a cause (table below).
7. **Group** across pages and viewports: the same element signature and the same cause become
   one group ("header logo moved 4 px down on 12 pages at 320 px").
8. **Judge intent** against `change_context` when given (`likely_intended`, `likely_unintended`,
   `unknown`), as agent judgment.
9. **Review file and report.** Every group is listed for review. Validate and render.

## Causes

| Cause | Detected by |
|---|---|
| `layout_shift` | An element's box moved or resized, and pixels inside it match after offsetting |
| `missing_element` / `new_element` | An element signature in only one set |
| `text_change` | Text content differs for the same element |
| `colour_change` | Same box and shape, consistent colour delta across the region |
| `image_change` | `img`, `picture`, `svg` or `background-image` source or pixels differ |
| `font_rendering` | Region is text, boxes unchanged, and the diff is mostly anti-aliasing pixels |
| `dynamic_content` | Region differs between the two current takes, or matches known patterns (dates, counters, carousels) |
| `unexplained` | Everything else |

## Rating

- **Impact:** `high` for `missing_element`, `layout_shift` over 8 px or overlapping another
  element, and text truncation; `medium` for `layout_shift` under 8 px, `colour_change`,
  `image_change`, `text_change`; `low` for `font_rendering`, `dynamic_content`.
- **Confidence:** `high` when DOM data on both sides explains the region; `medium` with DOM data
  on one side, or connected-region analysis only but a clear shape; `low` otherwise.
- **Review order:** impact weight (3/2/1) x reach (pages: 1, 2 for 2–4, 3 for 5 or more) x
  confidence (3/2/1), unintended before unknown before intended.

## Output (`visual_review.schema.json`)

```text
scope:     mode, sets{baseline, current, sha256 manifests}, browsers, viewports, states,
           ignore_regions, threshold
metadata:  browser versions, tools[{name, version, status}], pairs{matched, added, removed, size_mismatch}
headline:  groups_by_impact, groups_by_cause, pages_compared, pages_untested,
           flaky_pixels_excluded, evidence_completeness, overall_confidence
groups[]:  {id, cause, impact, confidence, review_order, intent: likely_intended|likely_unintended|unknown,
            element_sig?, pages[], viewports[], browsers[], measure{dx, dy, dw, dh, colour_before,
            colour_after, text_before, text_after}, crops[{baseline, current, diff}], explanation}
unchanged[], untested[], limitations[]
review.md: one checkbox per group, for the user to accept or reject
```

## Validator fails when

- A group has no crops, or its crop files' hashes don't match the manifests.
- Pixel counts in groups and noise don't add up to the total changed pixels.
- A page in only one set is reported as unchanged.
- An `agent_judgment` intent has high confidence, or any field claims approval.
- The headline says "no visual changes" while `untested` is non-empty, without saying so.

## Edge cases

- **Full-page screenshots of different heights:** compared over the shared height, with the
  difference reported as a `layout_shift` of the page.
- **Fonts not loaded:** detected by `document.fonts.status`; the capture is retried, then flagged.
- **Cross-OS baselines** (macOS baseline, Linux CI): `font_rendering` spikes; the report warns
  that baselines should come from the same OS and browser.
- **Mobile web:** viewports and device scale factors from Playwright's device list. Native app
  screenshots can be compared in `compare` mode without DOM data.

## Files

```text
visual-regression-reviewer-skill/
  SKILL.md  RUBRIC.md  README.md  requirements.txt (playwright, capture modes only)
  schemas/visual_review.schema.json
  scripts/capture.py          # two takes + DOM boxes; uses browser_harness.py
          browser_harness.py  # shared copy (includes the PNG decoder)
          png_io.py           # stdlib PNG encode for crops and diff images
          diff_images.py      # YIQ distance, anti-aliasing check, connected regions
          attribute.py        # regions -> elements, moves, causes
          group.py  validate_review.py  render_report.py
  examples/
```

## Example

`examples/storefront/` has two versions of a small static site (`v1/` and `v2/`) served on
loopback, with planted changes and an answer key: the logo moved 4 px, a "Free delivery"
banner removed, a button colour change, a price text change, a new carousel slide (dynamic), a
clock in the footer (dynamic), and a deliberately unchanged page. `examples/run/` holds real
captures and diffs, and `examples/review.example.json` the grouped review.

## Resolved questions

Agreed with the user before the build.

1. **Read Playwright `toHaveScreenshot` failure folders?** Yes.
2. **Compare production with staging?** Only with `authorised_by`.
3. **Write accepted baselines?** Only on explicit request, from the decisions in `review.md`.
