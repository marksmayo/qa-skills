# Rubric: mobile app auditor

## Accessibility (the WCAG auditor's model, on screens)

**Outcomes** per target SC: `failed` (findings), `passed` (probe or human evidence, never a tool alone),
`cantTell` (needs review), `inapplicable` (with a basis; EN 301 549 clause 11 treats 2.4.1, 2.4.5, 3.2.3 and
3.2.4 as not applicable to non-web software), `untested` (in the manual test plan).

**Severity**: the failure pattern's default from `data/wcag-sc.json`; one level up on a critical path;
Critical for non-interference SCs (1.4.2, 2.1.2, 2.2.2, 2.3.1).

**Priority**: `severity weight (4/3/2/1) x reach x leverage`. Reach counts screens: 3 when the finding is on
at least two screens and at least half the sample, 2 on two or more, else 1. Leverage 2 when there are two or
more instances. P0 >= 24, P1 >= 12, P2 >= 6, else P3; Critical at least P1, Serious at least P2;
`blocks_critical_path` is P0; agent judgment at most P1.

**Verdict**: `does_not_conform` with any failed SC, otherwise `not_established`.

## Checks and probes (`data/rule-map.json`)

| Code | SC | Method |
|---|---|---|
| `label.control-no-name` | 4.1.2 | Clickable element with no text, description or labelled descendant |
| `label.icon-button-no-name` | 1.1.1 | The same with no text descendants (an icon-only control) |
| `target.below-24dp` | 2.5.8 | Under 24 x 24 dp, and a 24 dp circle on it meets another target |
| `target.below-48dp` | - | Advisory: Android's 48 dp guideline |
| `contrast.text`, `contrast.text-dark` | 1.4.3 | Commonest colour in the text box against the most contrasting colour with >= 1% of its pixels; 4.5:1, or 3:1 for text 32 dp tall or more |
| `text-scale.not-enlarged` | 1.4.4 | At font scale 2.0, while the median text grows 1.5x or more: a text grows under 1.2x. Or clipped: a text that grew (or on a screen whose texts grew) reaches the bottom of a parent that kept its height, and either now fills that parent top to bottom when it did not before, or would need more room than it has, grown 1.8x (body text up to 24 dp a line) or 1.5x (larger), or as much as the unheld texts grew. Android 14+ scales large text less, so the heading alone never decides; React Native reports a clipped text's visible bounds, so its height alone never decides either |
| `display-size.offscreen` | 1.4.10 | At density x 1.3, a control or text off screen |
| `orientation.locked` | 1.3.4 | Landscape requested; the screenshot stays portrait |
| `keyboard.trap` | 2.1.2 | Tab doesn't move focus, and Escape then Back leave the screen in place |
| `keyboard.unreachable` | 2.1.1 | A clickable control never focused in a full Tab cycle |
| `motion.auto-advance` | 2.2.2 | An element's pixels change between 5 s and 7.5 s with no pause, stop or hide control |
| `heading.not-exposed` | 1.3.1 | uiautomator doesn't expose headings: always needs review |
| `glyph.icon-font` | 1.1.1 | Text made only of private-use code points (an icon font): kept out of contrast; review the screenshot (a box means the font is missing) and the control's name |

Dismiss with a specific reason, for example: a small icon inside a full-width clickable row (the row is the
target); a decorative image correctly left out of the tree; text measured over a photo where the agent can
see the real background.

## Crashes (the UI crash hunter's ranking)

`score = blast radius x reach x likelihood x recoverability`

| Factor | Values |
|---|---|
| Blast radius (`tier`) | B1 app-fatal 3 (the app is killed or can't be used); B2 screen-fatal 2. An ANR is B2 when the app recovers after Wait (the same process still shows the app), B1 when it is killed |
| Reach | 3 on a critical path; 2 a sampled screen; 1 reached only by random exploration |
| Likelihood | `normal_use` 4 (guided taps); `unusual_sequence` 3; `state_change` 2 (rotation, backgrounding, a setting); `random_only` 1 |
| Recoverability | `crash_loop` 3 (relaunch crashes again); `restart` 2; `retry` 1 |

P0 >= 48, P1 >= 24, P2 >= 8, else P3. A crash not reproduced on replay is at most P1. Confidence:
`reproduced` (a replay record reproduced it) or `observed`.
