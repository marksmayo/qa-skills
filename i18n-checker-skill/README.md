# i18n Checker Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It finds internationalisation and localisation problems in resource files, code and the running UI:

- **Keys and messages**: missing, untranslated, empty, duplicate and unused keys; placeholders that
  differ between locales (ICU, i18next, printf); broken ICU syntax; plural categories CLDR requires
  but a message lacks.
- **Code**: hard-coded text, concatenated translations, hand-formatted numbers, currency and dates,
  fixed widths that hide overflow, and physical CSS that won't mirror in right-to-left languages.
- **Running pages**: a pseudo-locale (longer, accented, bracketed text), every shipped locale and RTL
  locales, measuring truncation, overlap, untranslated text, raw placeholders and keys, formats
  against CLDR, and mirroring, with screenshots.

Formats: JSON (i18next, FormatJS, vue-i18n), ARB, gettext PO, Android `strings.xml`, iOS `.strings`,
`.stringsdict` and `.xcstrings`, all parsed with the Python standard library. CLDR plural rules and
number and date patterns are pinned, hash-checked subsets.

It is strict about what it can claim:

- Never "fully localised": the verdict is scoped, and translation quality is untested unless a named
  reviewer checked it.
- A key that's missing and a key with the source text copied in are different findings; the second is
  only ever possible.
- Missing keys are proposed as a patch and never applied; no text is sent to an outside service.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g. `.claude/skills/i18n-checker/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/i18n-checker/`, or include
`SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/i18n-checker/`, and reference
`SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The static checks need Python 3.9+ only. The
runtime renders need Playwright (`pip install -r requirements.txt` and `python -m playwright install
chromium`). `scripts/fetch_cldr.py --rebuild` refreshes the CLDR subsets when you choose to.

## Usage

> Check our translations for German, French and Arabic, and render the main pages in each.

> Are there hard-coded strings or placeholder problems in the Android resources?

```bash
python scripts/discover.py --repo . --out run/resources.json
python scripts/check_keys.py run/resources.json --repo . --out run/keys.json
python scripts/scan_code.py --repo . --out run/code.json
python scripts/render_locales.py targets.json --out run/runtime
python scripts/merge_findings.py --resources run/resources.json --keys run/keys.json --code run/code.json --runtime run/runtime/runtime.json --repo . --out run/candidates.json --patch run/missing_keys.patch
python scripts/validate_report.py run/report.json --candidates run/candidates.json --keys run/keys.json --resources run/resources.json --repo . --runtime run/runtime/runtime.json
python scripts/render_report.py run/report.json [--brief]
```

## Example

`examples/bookclub/` is a small site with `en`, `de`, `fr` and `ar` resources and a tiny loader, plus an
Android resource set, with planted problems; `examples/targets.json` the runtime plan; `examples/run/` the
real run (listed in `examples/run/commands.sh`) with screenshots and the proposed patch; and
`examples/report.example.json` the report. `examples/README.md` has the answer key.

```bash
python scripts/validate_report.py examples/report.example.json --candidates examples/run/candidates.json --keys examples/run/keys.json --resources examples/run/resources.json --repo examples/bookclub --runtime examples/run/runtime/runtime.json
python scripts/render_report.py examples/report.example.json
```

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: severity, confidence, reach, leverage, runtime measurements, headline
- `schemas/i18n_report.schema.json`: output contract
- `data/cldr.lock.json`, `data/cldr-plurals.json`, `data/cldr-formats.json`: pinned CLDR subsets (Unicode-3.0)
- `data/hardcoded-patterns.json`: detection patterns and the allow-list
- `scripts/fetch_cldr.py`: check or rebuild the CLDR subsets
- `scripts/discover.py`, `scripts/parse_resources.py`: find and parse resource files
- `scripts/icu_lite.py`: ICU MessageFormat and placeholder parser
- `scripts/check_keys.py`, `scripts/scan_code.py`: static checks
- `scripts/render_locales.py`: pseudo-locale, locale and RTL renders with measurements
- `scripts/merge_findings.py`: link, rate and propose the missing-keys patch
- `scripts/validate_report.py`, `scripts/render_report.py`, `scripts/schema_check.py`
- `scripts/browser_harness.py`: shared copy (edit it in `shared/`)
- `examples/`: the site, the Android resources, the run and the report

## Design principles

1. Prove what a parser can prove; confirm heuristics in the rendered page.
2. A screenshot and a measurement, or a file and a line, for every finding.
3. Propose fixes; don't apply them.
4. Translation quality needs a person.
