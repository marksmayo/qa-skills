# Design: `i18n-checker-skill`

Status: Built (v1.0.0). Build order: 20.

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

## As built (v1.0.0, updated to v1.1.0 and v1.2.0)

Where the build differs from the design above, and what the example showed.

- **CLDR 49.0.0 subsets are committed** (`data/cldr-plurals.json` for every locale's plural categories,
  `data/cldr-formats.json` for 30 locales' separators, currency, percent and date patterns), built by
  `fetch_cldr.py` from the cldr-core, cldr-numbers-full and cldr-dates-full packages file by file and
  pinned by SHA-256 in `data/cldr.lock.json`. Nothing is downloaded in normal use.
- **Runtime detections** gained `raw_placeholder` and `missing_key` (text that is a key), which link
  to the static placeholder and missing-key findings; partly translated text in the pseudo run links to
  concatenation.
- **Overlap is measured in the page** with `contains()`, so ancestors never count as overlapping their
  children (the first version reported a footer overlapping its own spans).
- **Linking runtime to static** uses the CSS rule's selector against the element's tag, id and class, so
  `#next` (class `next`) confirms the `.next` rule.
- **A Critical finding is at least P1**, as in the other skills: a missing key shown on a critical page
  otherwise came out P2.
- **The missing-keys patch** inserts each key into its parent object textually, in source order, keeping
  the file's formatting; JSON only. Other formats are listed in the report, not patched.
- **Formats** compare separators and currency position with CLDR; date order can't be told from text
  when the separators match (fr and ar), which the report states.
- **Not built:** YAML (Rails) resources (listed as not parsed), locale negotiation by cookie and
  Accept-Language beyond what `locale_strategy` passes, running the project's own i18n linters, and
  fixed-width checks for iOS.
- **Example results (v1.2.0):** 18 candidates; 14 findings (1 Critical, 5 High, 7 Medium, 1 Low), 3 dismissed
  (script-filled placeholder text), 1 needs review (de "Newsletter"); Hebrew not run (no resources).
  The pseudo-locale fitted the tab bar; real German text overflowed it.
- **Testing:** 16 of 16 seeded report violations are caught.
- **Found on a FastAPI app serving a hand-written static front end from a committed `dist/`, fixed
  (v1.1.0)**:
  - `discover.py`, `scan_code.py` and `check_keys.py` skipped `dist/` and `build/` by name, so a served,
    hand-written front end was never read and the report said "none detected, complete, high".
    `scripts/repo_walk.py` prunes before descending and treats `dist/`, `build/`, `out/` as build output
    only when the repository builds them (build script, bundler config, tsconfig `outDir`,
    `.gitignore`); skipped directories go to `skipped_dirs` and stderr, and `--include DIR` scans one.
  - JS message objects (`const translations = {en: {...}, ru: {...}}`, a locale-named module's default
    export) are read without running code (format `js-object`, set `<file>:<name>`); values that aren't
    string literals are counted in `not_parsed`; unused keys of such a set are not assessed. A
    locale-named `.js` file (ISO 639-1 stem) with no message object is listed in `not_parsed`.
  - Hard-coded HTML text in any script counts (it was `[A-Za-z]{2}` only).
  - Validator: evidence is partial when no set was parsed, the code scan read nothing
    (`candidates.inputs.code_files_scanned`, from merge) or the pseudo run was a text walk; with
    nothing analysed the verdict is `not_assessed` (new) and confidence low. A more cautious headline
    is accepted (the honest partial/medium report was rejected before). Agent findings
    (`source: "agent"`, `candidate_id: null`, `basis` re-read with `--repo`) are allowed, never high.
  - Runtime: locale runs flag `<html lang>` naming another language and text left exactly as the
    source showed it (`compare_to_source`); overlap uses line boxes (`getClientRects`), so wrapped
    inline links no longer overlap the text after them; placeholders and button values are measured
    for truncation; the text-walk pseudo run records `untranslated_check: not_assessed`; without a
    resource pattern only `locales` (or locales in `--resources`) render, so `rtl_locales` alone no
    longer renders the source as RTL; `blocked_requests` and request counts are recorded.
  - `merge_findings.py` counts locales a runtime render served as shipped. `fetch_cldr.py --check`
    hashes with line endings normalised (a CRLF checkout reported a mismatch). The renderer no longer
    prints empty "RTL ." and "Key coverage: ." values.
  - `scripts/self_test.py` (new) covers each of these on synthetic repositories. The example's static
    outputs were regenerated (new fields only: `skipped_dirs`, `files_seen`, `include`,
    `files_scanned`, `inputs`); candidates, keys, patch and report are unchanged; the runtime run was
    not re-captured.
  - Not done: the JSX/Vue/DOM hard-coded patterns in `data/hardcoded-patterns.json` still start at a
    Latin capital; no pseudo-locale for JS message objects (the text walk is used and reported).
- **Found on a single-locale Next.js App Router content site (no i18n library, no tests), fixed
  (v1.2.0)**:
  - The visually-hidden idiom (`.sr-only`: 1x1 px, `overflow: hidden`, `clip: rect(0 0 0 0)` or
    `clip-path: inset(50%)`) was measured as truncation at runtime, which then "confirmed" the static
    fixed-width finding on the `.sr-only` rule as High, high confidence. The page snapshot now skips
    visually-hidden elements and their descendants, and `scan_code.py` never reads the idiom as a
    fixed width.
  - `public/sw.js` was listed as a Swahili resource not parsed. `discover.py` uses the shared
    `next_app.py` service-worker model and a text check (install/fetch handlers, `importScripts`,
    `skipWaiting`) and lists workers under `service_workers`; `sw` joined the ambiguous codes that need
    a locale folder.
  - JSX was read with an ASCII, capital-first regex. `scripts/jsx_text.py` is a small JSX-aware scanner
    (a `<` opens markup only where an expression can start, so comparisons and TypeScript generics are
    skipped): element text in any script with digits and `& · : %`, kept whole beside `{expressions}`;
    `alt`, `aria-label`, `title`, `placeholder`, `label` attributes (string or template); Next.js
    `metadata` and `generateMetadata()` strings; sentences interleaving text with two or more
    expressions (concatenation, the only kind visible without a `t()` call); `{n}%`; className
    Tailwind physical utilities and arrow glyphs. HTML and Vue get the class and arrow rules too.
  - Formats: a locale literal passed to `toLocale*String` or `Intl.*` and hand-made percentages (an
    `unless` pattern skips CSS percentages in style objects and class strings).
  - Single-locale products: `merge_findings.py` writes `single_locale` (no locale ships besides the
    source), rates hard-coded text Low (readiness) and the validator expects the new verdict
    `readiness_only` (or `not_assessed`) with a summary saying only one locale ships. Before, such a
    site got `issues_found` driven by heuristics alone.
  - Validator, stricter where it accepted a dishonest report: "RTL-ready" / "supports RTL" with no RTL
    locale checked and any "ready for localisation / any language" claim are rejected (negated claims
    pass); vague dismissals (under 25 characters, or "false positive" / "not needed" with nothing
    checkable) are rejected; limitations must name each not_run or excluded locale, unparsed files, a
    text-walk pseudo-locale and the absence of resources or a second locale; summary counts (issues,
    severities, need review, dismissed) must match. A probe report on the target (all candidates
    dismissed as "not needed for this English site", no limitations, `none_detected`, "RTL-ready and
    ready for localisation into any language") validated before and fails now on each count.
  - `render_locales.py` records `pseudo.only_mode` when there is no resource set (the text walk is the
    only mode, so untranslated text can never be assessed there); `--brief` now prints the
    needs-review items (first 15, then a count) and the limitations.
  - Runtime arrows now link to their static cause: the example's arrow became two `rtl_physical`
    candidates (one per page, each confirmed by its ar run) instead of one runtime-only
    `rtl_mirroring` candidate. The example's static outputs and candidates were regenerated; the
    runtime run was not re-captured (the snapshot change only skips visually-hidden elements, which
    the example has none of).
  - Not done: proper nouns and brand names in copy are still flagged (the allow-list is generic; the
    agent dismisses them with a reason); content kept in typed data modules is not scanned for text;
    text in `opengraph-image` files is flagged like page text.
