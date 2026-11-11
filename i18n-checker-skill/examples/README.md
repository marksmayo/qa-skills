# Example: bookclub

`bookclub/site/` is a small static site with `en`, `de`, `fr` and `ar` resources in `site/locales/` and a
tiny loader (`site/js/i18n.js`) that fills `[data-i18n]` elements and shows a key when its message is
missing; `bookclub/android/res/` is an Android resource set. `targets.json` renders the home and events
pages (events is a critical path) at 1280x800 in Chromium 158. `run/` is the real run (listed in
`run/commands.sh`): resources, key and code findings, runtime screenshots and measurements, candidates
and `missing_keys.patch`. `report.example.json` is the report.

## Answer key

| Planted | Expected | Got |
|---|---|---|
| `de.json` lacks `events.title` and `events.rsvp` | missing_key; the German pages show the keys | F4, Critical (the events heading shows "events.title" on a critical-path page), P1; `missing_keys.patch` adds both with the English text, not applied |
| `fr.json` renames `{name}` to `{nom}` | placeholder_mismatch, raw `{nom}` on the page | F11; the fr run shows "Bon retour, {nom} !" |
| `ar.json` plural has zero, one, two and other | plural_categories: CLDR ar also needs few and many | F12 |
| "Sign in" button without data-i18n | hard-coded text, confirmed in the pseudo run | F2, on both pages, outside the pseudo brackets |
| Greeting built as t("greeting") + ", " + name | concatenation | F5; the pseudo run shows ", Robin!" outside the brackets |
| Fixed 96 px tabs with overflow hidden | truncation | F1: the German "Veranstaltungen" overflows by 20 px on both pages (the pseudo-locale's text fitted) |
| `.cta .icon` margin-left, `.next` padding-left and border-left, a → arrow | RTL mirroring in ar | F3 and F8 (measured margins, padding and border), F9 (the arrow) |
| Price as "$" + toFixed(2); date as month/day/year | format against CLDR | F7 (de, fr: decimal point and symbol position; ar: symbol position) and F6 (de: "/" where CLDR uses dd.MM.yy) |
| Android de `books_count` uses %s, en %d | placeholder_mismatch | F10 |
| `legacy.banner` used nowhere | unused_key | F13 |
| de "Newsletter" equals the English | untranslated, possible | Needs review: normal German usage |
| Placeholder text in `#greeting`, `#price`, `#next` | Static hard-coded candidates | Dismissed: app.js replaces them through i18n.t(), and the pseudo run shows them translated |
| Hebrew in rtl_locales, no `he.json` | not_run with a reason | not_run: no resources at locales/he.json |

```bash
python scripts/validate_report.py examples/report.example.json --candidates examples/run/candidates.json --keys examples/run/keys.json --resources examples/run/resources.json --repo examples/bookclub --runtime examples/run/runtime/runtime.json
python scripts/render_report.py examples/report.example.json
```
