# Example: storefront v1 and v2

`storefront/v1/` and `storefront/v2/` are two versions of a small static shop, served on loopback
(`storefront/serve.py`). `targets.json` captures four pages at 1280x800 and 375x812 in Chromium;
`change_context.md` is the PR description. `run/` is the real run (listed in `run/commands.sh`):
captures with DOM boxes, `diff.json`, `groups.json`, crops, diff images and `review.md`.
`review.example.json` is the review.

## Answer key

| Planted in v2 | Expected | Got |
|---|---|---|
| Logo 4 px lower (CSS) | One `layout_shift` group on every page with the header | G1: `div.logo` dy +4 px on home, product and about at both viewports; medium impact; likely intended |
| "Free delivery" banner removed from home | `missing_element`, and the content below moves up | G3: `div.banner` missing, high impact. G2: `main` and `footer` moved up 46 px, high impact, explained as following from G3 |
| Add to cart button blue to green | `colour_change` | G4: background rgb(31, 95, 191) to rgb(31, 143, 79); likely intended |
| Price $24.00 to $26.00 | `text_change`, not in the PR | G5: likely unintended (agent judgment, medium), first in review order |
| Carousel rotating every 1.5 s, with a new fourth slide | `dynamic_content` | G7: the slides as one rotating set, noise; the fourth slide couldn't be checked without a frozen clock |
| Footer clock | `dynamic_content` | G6: noise, by its time-like text and the two takes |
| Contact page, the same in both | Unchanged | Unchanged at both viewports |

Flaky pixels excluded: 5,644; 5,636 are in the clock and carousel groups and 8 in a region too small to group. At 375 px the header overflows to
417 px in both versions, so those captures are 417 px wide; that isn't a difference.

```bash
python scripts/validate_review.py examples/review.example.json --groups examples/run/groups.json --diff examples/run/diff.json
python scripts/render_report.py examples/review.example.json
```
