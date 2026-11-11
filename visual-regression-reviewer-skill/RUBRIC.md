# Visual regression reviewer rubric

`group.py` computes impact, confidence, reach and score; `validate_review.py` checks them and the
review order.

## Pixels

A pixel changes when pixelmatch's YIQ colour distance exceeds `35215 × threshold²` (threshold 0.1
by default). A changed pixel that pixelmatch's anti-aliasing test explains in either image counts as
anti-aliasing. Pixels that differ between the two current takes are flaky and excluded. Every
changed and anti-aliasing pixel belongs to exactly one group or to the ungrouped count (regions
under `min_region_pixels`), and so does every flaky pixel.

## Causes

| Cause | Detected by |
|---|---|
| `layout_shift` | The same element signature's box moved or resized (outermost elements; one change per shared delta) |
| `missing_element` / `new_element` | A signature visible in only one set (outermost only) |
| `text_change` | Same signature, different own text |
| `colour_change` | Same signature, different colour or background colour |
| `image_change` | Same signature, different image source |
| `dynamic_content` | Half or more of the change's pixels lie within 8 px of flaky pixels; or its text looks like a time, date or counter; or it is a missing/new member of a rotating set whose sibling is dynamic |
| `font_rendering` | No element change explains the region, and anti-aliasing is 60% or more of its pixels |
| `unexplained` | Everything else |

## Impact

| Impact | Weight | Causes |
|---|---|---|
| high | 3 | `missing_element`; `layout_shift` over 8 px in any direction or size |
| medium | 2 | `layout_shift` up to 8 px, `new_element`, `text_change`, `colour_change`, `image_change`, `unexplained` |
| low | 1 | `font_rendering`, `dynamic_content` |

## Confidence

| Confidence | Weight | When |
|---|---|---|
| high | 3 | DOM data on both sides explains the change |
| medium | 2 | Region only, with a clear shape (pixels cover 5% or more of the region) |
| low | 1 | `unexplained`, or a region with scattered pixels |

## Reach, score and order

- **Reach:** pages with the group: 1 for one, 2 for two to four, 3 for five or more.
- **Review score** = impact weight × reach × confidence weight.
- **Review order:** `likely_unintended`, then `unknown`, then `likely_intended`; within each, higher
  score first.

## Intent

Only with a change context, and always `agent_judgment` with `low` or `medium` confidence:
`likely_intended` when the context describes the change, `likely_unintended` when the change is
outside what the context describes, `unknown` otherwise (and always without a context).

## Headline

- **Evidence completeness:** `partial` when anything is untested, added or removed, a pair differs in
  size, or a group isn't high confidence; otherwise `complete`.
- **Overall confidence:** low when any group is `unexplained`; medium when evidence is partial or a
  non-noise group isn't high; otherwise high.
- **Approval** is always `none`.
