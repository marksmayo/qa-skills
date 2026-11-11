# Visual Regression Reviewer Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It compares current screenshots with a baseline set and turns hundreds of changed pixels into a
short, ordered list of grouped causes for a person to review:

- **Captures both sets** in a real browser (stabilised, the current set twice, with DOM boxes), or
  reads directories another tool produced, including Playwright `toHaveScreenshot` failures.
- **Diffs with the standard library**: pixelmatch's YIQ distance and anti-aliasing test, flaky pixels
  from the two takes excluded, connected regions.
- **Explains each difference** as a cause on named elements (layout shift, missing or new element,
  text, colour, image, font rendering, dynamic content), with the measured change.
- **Groups the same cause** across pages and viewports, rates impact and confidence, and cuts
  baseline, current and diff crops.
- **Judges intent** against a PR description or ticket when given, and labels it as judgment.

It is strict about what it can say:

- "No differences" covers only what was compared; untested, added and removed pages are listed.
- Noise (clocks, carousels, anti-aliasing) is listed and explained, not hidden.
- Every changed pixel is accounted for.
- It never approves anything: `review.md` has a box per group for you, and `apply_review.py` copies
  accepted images over baselines only when you run it.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g.
`.claude/skills/visual-regression-reviewer/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/visual-regression-reviewer/`, or
include `SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/visual-regression-reviewer/`, and
reference `SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+. Capturing
needs Playwright (`pip install -r requirements.txt` and `python -m playwright install chromium`).

## Usage

> Compare staging with production for the home, product and checkout pages at desktop and mobile widths. I'm authorised to test production.

> Our Playwright screenshot tests failed; tell me what actually changed.

```bash
python scripts/capture.py targets.json --set baseline --out run/baseline
python scripts/capture.py targets.json --set current --out run/current
python scripts/diff_images.py --baseline run/baseline --current run/current --out run/diff.json --diffs run/diffs --ignore targets.json
python scripts/group.py run/diff.json --out run/groups.json --crops run/crops --review run/review.md --targets targets.json
python scripts/validate_review.py run/review.json --groups run/groups.json --diff run/diff.json
python scripts/render_report.py run/review.json [--brief]
```

## Example

`examples/storefront/` has two versions of a small shop (`v1/`, `v2/`) with planted changes;
`examples/change_context.md` is the PR description; `examples/run/` is the real run (listed in
`examples/run/commands.sh`) and `examples/review.example.json` the review. `examples/README.md` has
the answer key.

```bash
python scripts/validate_review.py examples/review.example.json --groups examples/run/groups.json --diff examples/run/diff.json
python scripts/render_report.py examples/review.example.json
```

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: pixels, causes, impact, confidence, review order, intent
- `schemas/visual_review.schema.json`: output contract
- `scripts/capture.py`: stabilised captures, two takes, DOM boxes
- `scripts/png_io.py`: stdlib PNG decode, encode, crop and diff images
- `scripts/diff_images.py`: pairing, YIQ diff, anti-aliasing, flaky pixels, regions
- `scripts/attribute.py`: element changes and pixel assignment per pair
- `scripts/group.py`: groups, ratings, crops, review.md
- `scripts/apply_review.py`: apply the user's ticks (only on request)
- `scripts/validate_review.py`, `scripts/render_report.py`, `scripts/schema_check.py`
- `scripts/browser_harness.py`: shared copy (edit it in `shared/`)
- `examples/`: the site, targets, change context, run and review

## Design principles

1. Explain differences as causes on elements, not as pixel counts.
2. Label noise; don't hide it.
3. Account for every pixel and say what wasn't compared.
4. The person decides; the skill never approves.
