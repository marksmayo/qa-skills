# Design: `i18n-checker-skill`

Status: Designed; not built. Build order: 20.

## Mission

The skill finds internationalisation and localisation problems in two ways:

- **In the code and resource files:** missing and unused translation keys, keys whose
  placeholders differ between locales, broken ICU message syntax, missing plural categories,
  hard-coded user-facing strings, string concatenation that breaks translation, and
  locale-unaware date, number and currency formatting.
- **In the running UI:** it renders pages with a pseudo-locale (longer text, accented
  characters, brackets that show truncation) and with a right-to-left locale, and detects
  clipped, overlapping and untranslated text, and layout that doesn't mirror.

How it differs from the existing skills:

- **wcag-auditor** checks the language of the page (3.1.1) and reflow. This skill checks every
  locale the product ships, and its runtime probes reuse the WCAG auditor's clipping and
  overflow ideas with pseudo-localised text instead of text spacing.
- It can work from source alone, from a live site alone, or both.

## Decisions

- **Report and propose, never apply.** Missing keys are proposed as a patch (`missing_keys.patch`,
  source text copied and marked for translation); the skill never applies it or edits resource files.
- **No outside services.** No translation memory or machine-translation comparison: it would send
  product text to a third party. Translation quality stays `untested`.
- **Resource formats in v1:** JSON (flat and nested, i18next and FormatJS styles), gettext
  PO/POT, ICU MessageFormat strings inside those, Android `strings.xml` (including `plurals` and
  `string-array`), iOS `.strings` and `.stringsdict`, and Xcode `.xcstrings`. ARB (Flutter) and
  YAML (Rails) are read when present. All parsed with stdlib Python.
- **ICU and plural rules come from CLDR data** (Unicode licence, free to use), pinned and
  hash-checked: the plural categories each locale needs (`one`, `few`, `many`...), and the
  expected date, number and currency patterns.
- **Pseudo-localisation is built in:** expansion of 30–50% depending on string length (short
  strings expand more, as real translations do), accented substitutes for ASCII letters, and
  `⟦ ⟧` brackets so truncation is visible. It's applied at runtime by rewriting resource files
  served to the page (Playwright request interception) or by a text-node walk when the
  resources can't be found. Which method was used is recorded.
- **Hard-coded string detection is heuristic,** using framework-aware patterns (JSX text, Vue and
  Angular templates, Android layouts, SwiftUI `Text("...")`), with an allow-list for
  non-translatable text (brand names, codes). Findings are `possible` until confirmed in the
  rendered UI.

## Non-negotiable rules

- **Never "fully localised".** The verdict is `issues_found` or `none_detected`, scoped to the
  locales, files and pages checked. Translation quality (is the German right?) is out of scope
  for tools and is `untested` unless a reviewer did it; the agent can flag obvious problems
  (text left in English) as `agent_judgment`.
- **Missing isn't the same as untranslated.** A key absent from a locale file and a key present
  with the source text copied in are reported separately; the second is a `possible` finding,
  because some strings are legitimately the same.
- **Runtime findings need a screenshot and a measurement** (the element, its box, how many
  pixels of text overflow).
- **Every locale planned is accounted for:** checked, `not_run` with a reason, or excluded.
- **Tools are recorded** as `ran`, `not_run` or `errored` (the project's own i18n linters, such as
  `i18next-parser` or Android Lint's `MissingTranslation`, are used when present).

## Inputs

| Input | Default | Notes |
|---|---|---|
| `repo` | optional | Turns on static checks |
| `targets` | optional | URLs, states and viewports (WCAG auditor config format) for runtime checks |
| `source_locale` | detected | The locale others are translated from |
| `locales` | all found | Locales that ship |
| `rtl_locales` | `ar`, then `he` | Arabic is the main RTL run; Hebrew is a second check (`fa` also accepted) |
| `pseudo` | on | Expansion and accents |
| `formats` | on | Locale-aware formatting checks |
| `allow_list` | optional | Strings and patterns that shouldn't be translated |
| `authorised_by`, `auth` | as in `browser_harness.py` | |

At least one of `repo` and `targets` is required.

## Workflow

1. **Discover resources.** Find locale files, the source locale and the i18n library in use;
   record each locale's file count and key count.
2. **Check tooling.** CLDR data version and hash; project linters present or `not_run`.
3. **Static: keys.** Missing keys per locale, keys not used in code (by searching for the key and
   for dynamic key patterns, which make `unused` uncertain), duplicate keys, and empty values.
4. **Static: messages.** Parse ICU syntax per message; compare placeholders and tags between the
   source and each translation; check that each plural message has every category the locale's
   CLDR rules need; check Android and iOS format specifiers (`%1$s`, `%@`) match.
5. **Static: code.** Hard-coded strings; concatenation of translated fragments
   (`t('hello') + name`); locale-unaware formatting (`toFixed`, manual date strings,
   `new Date().toString()`, `String.format` without a locale); fixed-width UI around translated
   text (fixed widths or `maxLines` with no ellipsis).
6. **Runtime: pseudo-locale.** Render each target with pseudo-localised resources; detect text
   whose scroll size exceeds its box, overlapping text boxes, and text nodes still in plain ASCII
   (likely hard-coded or missing).
7. **Runtime: RTL.** Render with Arabic and `dir="rtl"`, then repeat with Hebrew as a second
   check; check that the document
   direction is set, that leading and trailing elements mirror (from boxes), that physical CSS
   (`margin-left`) breaks the mirror, and that icons that imply direction are flipped.
8. **Runtime: formats.** For each shipped locale, read dates, numbers and currencies in the page
   and compare with CLDR patterns.
9. **Link** runtime findings to static causes (element text to key, element to component file).
10. **Candidates and triage**, then output, validate and render.

## Rating

- **Severity:** Critical for a crash or blank screen in a locale (broken ICU syntax, missing
  plural category that throws) and missing keys on a critical path in a shipped locale; High
  for truncated or overlapping text that hides meaning, untranslated UI on a main page, and
  placeholder mismatches that show raw `{name}`; Medium for wrong number or date format,
  concatenation, and RTL mirroring problems; Low for unused keys and duplicates.
- **Confidence:** `high` for parser-verified static findings (missing key, ICU error,
  placeholder mismatch) and runtime findings with a measurement; `medium` for hard-coded
  strings confirmed in the UI; `low` for hard-coded strings in code only and agent judgment.
- **Priority:** severity weight (4/3/2/1) x reach (locales affected: 1, 2 for 2–4, 3 for 5 or
  more, or all) x leverage (2 when one fix in a shared component or key covers several pages).
  P0 >= 24, P1 >= 12, P2 >= 6.

## Output (`i18n_report.schema.json`)

```text
scope:     repo?, targets?, source_locale, locales[{code, status, files, keys}], rtl_locales[],
           pseudo{expansion, method: intercept|text_walk}
metadata:  library, tools[{name, version, status}], cldr{version, sha256}
headline:  verdict: issues_found|none_detected, severity_counts, key_coverage{locale: share},
           translation_quality: "untested", evidence_completeness, overall_confidence
findings[]: {id, kind: missing_key|untranslated|placeholder_mismatch|icu_syntax|plural_categories|
             hardcoded_string|concatenation|format|truncation|overlap|rtl_mirroring|unused_key,
             locales[], severity, confidence, priority, locations[{file, line}|{url, selector}],
             keys[], evidence[{screenshot, measure, excerpt}], remediation, verification_step}
proposed_patch?: {path, keys_added, applied: false}
needs_review[], triage[], limitations[]
```

## Validator fails when

- A runtime finding has no screenshot or measurement, or a static finding has no file and line.
- A locale in `locales` has no status, or a `not_run` locale has no reason.
- The verdict text says "fully localised", "translated" or similar, or `translation_quality`
  isn't `untested` without reviewer evidence.
- An `untranslated` finding has high confidence.
- A candidate is missing from `triage`.

## Edge cases

- **Dynamic keys** (`t('status.' + s)`): unused-key results for that prefix become `cantTell`.
- **Server-rendered text** with no resource files: pseudo-localisation uses the text walk, which
  can't see strings in attributes or later updates; the report says so.
- **Locale negotiation:** the skill sets `Accept-Language`, the locale cookie or URL prefix as
  the site expects, and records which worked.
- **Mobile:** Android and iOS resources are checked statically, including plurals and format
  specifiers. Rendering native screens with a pseudo-locale is left to the mobile auditor skill;
  mobile web is rendered at mobile viewports.

## Files

```text
i18n-checker-skill/
  SKILL.md  RUBRIC.md  README.md  requirements.txt (playwright, runtime checks only)
  data/cldr.lock.json  cldr-plurals.json  cldr-formats.json   # extracted subsets, pinned
       hardcoded-patterns.json                                # per-framework detection patterns
  schemas/i18n_report.schema.json
  scripts/fetch_cldr.py  discover.py  parse_resources.py  icu_lite.py
          check_keys.py  scan_code.py
          render_locales.py       # pseudo and RTL runs; uses browser_harness.py
          browser_harness.py      # shared copy
          merge_findings.py  validate_report.py  render_report.py
  examples/
```

## Example

`examples/bookclub/` is a small static site with `en`, `de`, `fr` and `ar` JSON resources and a
tiny loader script, served on loopback, plus an Android `res/values*/strings.xml` set. Planted
problems with an answer key: missing `de` keys, an `fr` placeholder renamed, an ICU plural
missing the `few`/`many` categories for `ar`, a hard-coded "Sign in" button, a concatenated
greeting, a fixed-width tab bar that truncates under pseudo-localisation, `margin-left` icons
that don't mirror in RTL, a price formatted with `toFixed(2)` and a `$` sign, and an Android
`%s`/`%d` mismatch.

## Resolved questions

Agreed with the user before the build.

1. **Write fixes?** Propose missing keys as a patch; never apply it.
2. **Translation memory or machine-translation comparison?** No: it would send text to an outside service.
3. **Which RTL locale?** Arabic first, with Hebrew as a second check.
