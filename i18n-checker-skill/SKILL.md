---
name: i18n-checker
description: Find internationalisation and localisation problems in resource files, code and the running UI. Statically - missing, untranslated, empty, duplicate and unused keys, placeholders that differ between locales, broken ICU syntax, plural categories CLDR requires but a message lacks, hard-coded text, concatenated translations, hand-formatted numbers and dates, fixed widths and physical CSS that won't mirror. At runtime - pages rendered in a pseudo-locale (longer, accented, bracketed text), every shipped locale and RTL locales, measuring truncation, overlap, untranslated text, raw placeholders and keys, formats against CLDR, and mirroring. Proposes missing keys as a patch, never applies it, never sends text to outside services, and never calls a product fully localised, RTL-ready or ready for localisation. A product that ships one locale gets a readiness review, not an issues verdict.
version: 1.2.0
---

# i18n checker

## Mission

Show where a product breaks in the languages it ships, with a file and line or a screenshot and a
measurement for every finding. Separate what a parser proves (a missing key, a broken placeholder) from
what a heuristic suggests (a hard-coded string) and confirm heuristics in the rendered UI. Say what
wasn't checked; translation quality is always out of reach for tools.

The WCAG auditor checks the page language and reflow; this skill checks every shipped locale.

## Non-negotiable rules

1. **Never "fully localised".** The verdict is `issues_found` or `none_detected`, scoped to the
   locales, files and pages checked, `readiness_only` for a single-locale product (rule 10), or
   `not_assessed` when no resources were parsed, no code was read
   and nothing rendered: stop and say so, never a scoped `none_detected` over an empty input. Never
   claim readiness: "RTL-ready" or "supports RTL" needs an RTL locale checked, and "ready for
   localisation", "localisation-ready" or "ready for any language" is never written (tools show
   problems, not readiness). `translation_quality` is `untested` unless a named reviewer checked
   the translations ("Translations reviewed by ..." in limitations).
2. **Missing is not untranslated.** A key absent from a locale is `missing_key`; a key present with the
   source text copied in is `untranslated`, a `possible` finding that is never high confidence (some
   words are the same in both languages).
3. **Evidence.** Static findings have a file and line; runtime findings have a screenshot and a
   measurement (the element, its box, the pixels of overflow, the values against CLDR).
4. **Every locale is accounted for**: `checked`, `not_run` with a reason, or `excluded` with a reason.
5. **Report and propose, never apply.** Missing keys become `missing_keys.patch` with the source text
   copied in for translation; resource files are never edited.
6. **No outside services.** No translation memory or machine translation: it would send product text
   to a third party.
7. **Safe targets** (runtime): loopback needs nothing; other hosts need `authorised_by`.
8. **Record every tool** (`ran`, `not_run`, `errored`), including the project's own i18n linters.
9. **Loud about what wasn't read.** `discover.py` lists files that look like resources but weren't parsed
   (`not_parsed`, including locale-named `.js` files with no message object) and every skipped
   directory (`skipped_dirs`, also on stderr); `scan_code.py` records `files_scanned`. Put them in
   `limitations`. A format this skill can't read is reported as not parsed, never silently ignored.
   The validator requires a limitation for each not_run or excluded locale (its code or name), for
   files not parsed, for a text-walk pseudo-locale, and for a product with no resource set or no
   second locale.
10. **A single-locale product is a readiness review.** When no locale ships besides the source (no
   second locale in any resource set and none rendered; `candidates.json` has `single_locale: true`),
   key, placeholder and plural checks have nothing to compare: say so. The verdict is
   `readiness_only` (or `not_assessed`), the summary says only one locale ships ("single-locale",
   "English only"), hard-coded text is Low (readiness for a future locale), and copy the owner may
   never translate goes to `needs_review` rather than being dismissed or promoted wholesale.
11. **Dismiss with a checkable reason.** A dismissal names what makes it safe to drop (the text, the
   file or line, "a product name, never translated"); "false positive" or "not needed for this site"
   alone is rejected.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `repo` | optional | Static checks |
| `targets` | optional | `targets.json`: site, pages (`critical_path`), viewports, locales, `rtl_locales`, `locale_strategy`, `resources` |
| `source_locale` | detected | `en` when present, else the locale with the most keys |
| `locales` | all found | Locales that ship |
| `rtl_locales` | `ar`, then `he` | Arabic is the main RTL run; Hebrew a second check |
| `pseudo` | on | Expansion, accents, ⟦ ⟧ brackets |
| `allow_list` | `data/hardcoded-patterns.json` | Text that is never translated (brand names, codes) |
| `authorised_by`, `auth` | as in `browser_harness.py` | `auth: {"storage_state": file}` runs signed in, with a session a person captured by signing in by hand (`python scripts/browser_harness.py capture-session --url <app> --out <file outside the repo>`; cookies, localStorage, IndexedDB and sessionStorage); the run stays read-only and records only that a session was used, never the file or its contents |

At least one of `repo` and `targets` is required.

## Workflow

Static scripts are stdlib; `render_locales.py` needs Playwright (`pip install -r requirements.txt`,
`python -m playwright install chromium`; ask first). CLDR data is committed and pinned.

```bash
python scripts/fetch_cldr.py --check                                     # the pinned CLDR subsets are intact
python scripts/discover.py --repo <repo> --out <run>/resources.json        # files, locales, source, library
python scripts/check_keys.py <run>/resources.json --repo <repo> --out <run>/keys.json
python scripts/scan_code.py --repo <repo> --out <run>/code.json
python scripts/render_locales.py targets.json --out <run>/runtime         # source, pseudo, each locale, RTL
python scripts/merge_findings.py --resources <run>/resources.json --keys <run>/keys.json --code <run>/code.json \
  --runtime <run>/runtime/runtime.json --repo <repo> --out <run>/candidates.json --patch <run>/missing_keys.patch
```

1. **Discover.** JSON (flat or nested; i18next plural suffixes), ARB, PO, Android `strings.xml`
   (`plurals`, `string-array`), iOS `.strings`, `.stringsdict`, `.xcstrings`, and JS/TS message
   objects: a literal object of strings assigned to a translations-like name
   (`const translations = {en: {...}, ru: {...}}`, one set per object) or the default export of a
   locale-named module (`locales/de.js`). Values that aren't string literals are counted in
   `not_parsed`, never guessed; unused keys of a JS object are not assessed (its keys sit in the code).
   YAML is listed as not parsed. Each set (web, Android) is compared within itself.
   `dist/`, `build/` and `out/` are build output only when the repository builds them (a build script,
   a bundler config, a tsconfig `outDir`, a `.gitignore` entry); a committed, hand-written front end in
   `dist/` that a server mounts as static files is scanned. `--include DIR` scans a skipped one.
2. **Keys and messages.** ICU syntax, placeholders of every style (ICU, `{{i18next}}`, printf `%1$s`),
   CLDR plural categories, missing, untranslated, empty, duplicate and unused keys. Dynamic key prefixes
   (`t('status.' + s)`) make unused keys under them `cantTell`; a set whose platform has no code here is
   not assessed for unused keys.
3. **Code.** Hard-coded text in any script (HTML without a translation attribute, JSX/Vue text,
   Android layouts, SwiftUI), concatenated translations, hand-formatted numbers, currency and dates, fixed widths that
   hide overflow, physical CSS that won't mirror. All `possible`. JSX (`.jsx`, `.tsx`, and `.js` holding
   JSX) is read by `scripts/jsx_text.py`: element text in any script with digits and punctuation, kept
   whole beside `{expressions}` ("Progress: {…}%"); the text attributes `alt`, `aria-label`, `title`,
   `placeholder` and `label`; Next.js `metadata` exports and `generateMetadata()` (titles and
   descriptions search results show). A JSX sentence that interleaves text with two or more
   expressions is a concatenation; `{n}%` is a hand-made percentage; a locale literal passed to
   `toLocale*String` or `Intl.*` (`"en-GB"`) is a format finding; Tailwind physical utilities
   (`ml-`, `pr-`, `left-`, `text-right`, `rounded-l`, with variants such as `sm:`) and arrows drawn
   as text (→, `&rarr;`) are `rtl_physical`. The visually-hidden idiom (`.sr-only`: a 1px box with
   `clip`/`clip-path`) is never a fixed width. A service worker (`public/sw.js`; `sw` is also
   Swahili) is never a locale resource: `discover.py` lists it under `service_workers`.
4. **Runtime.** Pseudo-localisation rewrites the source resource file as it is served (or walks text
   nodes when there's no resource file, which misses attributes and later text, and brackets
   hard-coded text too, so it cannot find untranslated text: `pseudo.untranslated_check` is then
   `not_assessed` and the evidence partial). On a product with no i18n library or resource file the
   text walk is the only possible mode, so untranslated text stays not assessed however often it
   runs (`pseudo.only_mode`); say so up front and still use the run for expansion (truncation,
   overlap). Visually-hidden text (sr-only skip links, route announcers) is not measured. Each locale is loaded with the site's locale strategy
   (query, cookie, path prefix or Accept-Language). In each real locale run, `<html lang>` naming
   another language and text left exactly as the source showed it are `untranslated` (never high
   confidence); placeholders and button values wider than their input are `truncation`; overlap is
   measured on line boxes, so wrapped inline links are not reported. Without a `resources` pattern
   pass `--resources <run>/resources.json` so only shipped locales render; otherwise an RTL locale
   listed only in `rtl_locales` is `not_run` rather than rendering the source text as RTL.
5. **Merge and link.** Runtime findings confirm static causes; ratings follow `RUBRIC.md`.
6. **Triage.** Promote, `needs_review` (an `untranslated` value that may be correct goes to a reviewer),
   or dismiss with a checkable reason (placeholder text a script replaces at runtime, shown translated in
   the pseudo run; a product name that is never translated). Counts in the summary ("14 issues",
   "55 need review") must match the report. For each finding write a title, a remediation naming the file and the fix (a key, a
   placeholder, `Intl.NumberFormat`, a logical CSS property), and a verification step.
   Problems found by reading the code that no candidate covers (a language switch that never updates
   `<html lang>`, a message built with markup) are findings with `"source": "agent"`,
   `"candidate_id": null` and a `basis` `[{file, line, excerpt}]` that `--repo` re-reads; never high
   confidence.
7. **Validate and render.**

```bash
python scripts/validate_report.py <run>/report.json --candidates <run>/candidates.json --keys <run>/keys.json \
  --resources <run>/resources.json --repo <repo> --runtime <run>/runtime/runtime.json [--write-summary]
python scripts/render_report.py <run>/report.json [--brief]
```

`--brief` prints the verdict, one line per finding, the items that need review (the first 15, then a
count) and the limitations.

Native mobile screens are not rendered here (the mobile app auditor does that); Android and iOS
resources are checked statically, and mobile web at mobile viewports.

## Output fields

- `scope`: repo, site, pages, viewports, source locale, every locale with status and reason, RTL
  locales, pseudo expansion and method.
- `metadata`: library, tools, CLDR version and subset hashes.
- `headline`: verdict (`issues_found`, `none_detected`, `readiness_only`, `not_assessed`), summary, severity counts, key coverage per set and locale,
  `translation_quality`, evidence completeness, overall confidence.
- `findings[]`: kind, status, locales, severity, confidence, priority, score, reach, leverage,
  locations, keys, runtime evidence, remediation, verification step; agent findings add `source` and
  `basis`.
- `proposed_patch` (`applied: false`), `needs_review[]`, `triage[]`, `limitations[]`.

## What the validator enforces

Findings match their candidates; static findings point at real lines; runtime findings have a
screenshot on disk and a measurement; every shipped locale has a status, and not_run ones a reason;
no "fully localised" claims, no "RTL-ready" without an RTL locale checked, no "ready for
localisation" at all; dismissals with a checkable reason; summary counts equal to the report's;
limitations naming not_run locales, unparsed files, a text walk and a missing resource set or second
locale; `readiness_only` for a single-locale product; translation quality untested without a named reviewer; untranslated
findings never high confidence; the patch is unapplied and matches the missing JSON keys; agent
findings have a basis that is on its line; the headline is recomputed. Evidence is partial when a
locale wasn't checked, something needs review, unused keys weren't assessed, a file wasn't parsed, no
resource set was found, the code scan read nothing, the pseudo run was a text walk, or the product
is single-locale; with nothing
analysed at all the verdict is `not_assessed` and confidence low. A headline more cautious than the
computed one is accepted.
