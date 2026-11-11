# Visual review

Tick one box per group. Nothing is accepted until you tick it; apply_review.py copies current images over baselines only for pairs whose every non-noise group is accepted, and only when you run it.

## G1 layout_shift (medium impact, high confidence)

html>body>header>div.logo on about, home, product at 1280x800, 375x812. Measure: {"dx": 0, "dy": 4, "dw": 0, "dh": 0}. Crops: crops/G1-baseline.png, crops/G1-current.png, crops/G1-diff.png

- [ ] accept G1
- [ ] reject G1

## G2 layout_shift (high impact, high confidence)

html>body>main, html>body>footer on home at 1280x800, 375x812. Measure: {"dx": 0, "dy": -46, "dw": 0, "dh": 0}. Crops: crops/G2-baseline.png, crops/G2-current.png, crops/G2-diff.png

- [ ] accept G2
- [ ] reject G2

## G3 missing_element (high impact, high confidence)

html>body>div.banner on home at 1280x800, 375x812. Measure: {"text_before": "Free delivery on orders over $50"}. Crops: crops/G3-baseline.png, crops/G3-current.png, crops/G3-diff.png

- [ ] accept G3
- [ ] reject G3

## G4 colour_change (medium impact, high confidence)

html>body>main>button.buy on product at 1280x800, 375x812. Measure: {"property": "background", "colour_before": "rgb(31, 95, 191)", "colour_after": "rgb(31, 143, 79)"}. Crops: crops/G4-baseline.png, crops/G4-current.png, crops/G4-diff.png

- [ ] accept G4
- [ ] reject G4

## G5 text_change (medium impact, high confidence)

#price on product at 1280x800, 375x812. Measure: {"text_before": "$24.00", "text_after": "$26.00"}. Crops: crops/G5-baseline.png, crops/G5-current.png, crops/G5-diff.png

- [ ] accept G5
- [ ] reject G5

## G6 dynamic_content (low impact, high confidence)

#clock on home, product at 1280x800, 375x812. Measure: {"text_before": "9:13:01 pm", "text_after": "9:13:11 pm"}. Crops: crops/G6-baseline.png, crops/G6-current.png, crops/G6-diff.png

- [ ] accept G6
- [ ] reject G6

## G7 dynamic_content (low impact, high confidence)

html>body>main>div.carousel>div.slide on home at 1280x800, 375x812. Measure: {"text_before": "Gift cards now available"}. Crops: crops/G7-baseline.png, crops/G7-current.png, crops/G7-diff.png

- [ ] accept G7
- [ ] reject G7
