---
name: i18n-checker
description: Find internationalisation and localisation problems in resource files, code and the running UI. Statically - missing, untranslated, empty, duplicate and unused keys, placeholders that differ between locales, broken ICU syntax, plural categories CLDR requires but a message lacks, hard-coded text, concatenated translations, hand-formatted numbers and dates, fixed widths and physical CSS that won't mirror. At runtime - pages rendered in a pseudo-locale (longer, accented, bracketed text), every shipped locale and RTL locales, measuring truncation, overlap, untranslated text, raw placeholders and keys, formats against CLDR, and mirroring. Proposes missing keys as a patch, never applies it, never sends text to outside services, and never calls a product fully localised.
version: 1.0.0
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
   locales, files and pages checked. `translation_quality` is `untested` unless a named reviewer checked
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
| `authorised_by`, `auth` | as in `browser_harness.py` | |

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
   (`plurals`, `string-array`), iOS `.strings`, `.stringsdict`, `.xcstrings`. YAML is listed as not
   parsed. Each set (web, Android) is compared within itself.
2. **Keys and messages.** ICU syntax, placeholders of every style (ICU, `{{i18next}}`, printf `%1$s`),
   CLDR plural categories, missing, untranslated, empty, duplicate and unused keys. Dynamic key prefixes
   (`t('status.' + s)`) make unused keys under them `cantTell`; a set whose platform has no code here is
   not assessed for unused keys.
3. **Code.** Hard-coded text (HTML without a translation attribute, JSX/Vue text, Android layouts,
   SwiftUI), concatenated translations, hand-formatted numbers, currency and dates, fixed widths that
   hide overflow, physical CSS that won't mirror. All `possible`.
4. **Runtime.** Pseudo-localisation rewrites the source resource file as it is served (or walks text
   nodes when there's no resource file, which misses attributes and later text; the method is
   recorded). Each locale is loaded with the site's locale strategy (query, cookie, path prefix or
   Accept-Language).
5. **Merge and link.** Runtime findings confirm static causes; ratings follow `RUBRIC.md`.
6. **Triage.** Promote, `needs_review` (an `untranslated` value that may be correct goes to a reviewer),
   or dismiss with a checkable reason (placeholder text a script replaces at runtime, shown translated in
   the pseudo run). For each finding write a title, a remediation naming the file and the fix (a key, a
   placeholder, `Intl.NumberFormat`, a logical CSS property), and a verification step.
7. **Validate and render.**

```bash
python scripts/validate_report.py <run>/report.json --candidates <run>/candidates.json --keys <run>/keys.json \
  --resources <run>/resources.json --repo <repo> --runtime <run>/runtime/runtime.json [--write-summary]
python scripts/render_report.py <run>/report.json [--brief]
```

Native mobile screens are not rendered here (the mobile app auditor does that); Android and iOS
resources are checked statically, and mobile web at mobile viewports.

## Output fields

- `scope`: repo, site, pages, viewports, source locale, every locale with status and reason, RTL
  locales, pseudo expansion and method.
- `metadata`: library, tools, CLDR version and subset hashes.
- `headline`: verdict, summary, severity counts, key coverage per set and locale,
  `translation_quality`, evidence completeness, overall confidence.
- `findings[]`: kind, status, locales, severity, confidence, priority, score, reach, leverage,
  locations, keys, runtime evidence, remediation, verification step.
- `proposed_patch` (`applied: false`), `needs_review[]`, `triage[]`, `limitations[]`.

## What the validator enforces

Findings match their candidates; static findings point at real lines; runtime findings have a
screenshot on disk and a measurement; every shipped locale has a status, and not_run ones a reason;
no "fully localised" claims; translation quality untested without a named reviewer; untranslated
findings never high confidence; the patch is unapplied and matches the missing JSON keys; the headline
is recomputed.
