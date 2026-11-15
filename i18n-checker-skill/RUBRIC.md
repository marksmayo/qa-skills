# i18n checker rubric

`merge_findings.py` computes these; `validate_report.py` checks the report against them.

## Severity

| Severity | Weight | When |
|---|---|---|
| Critical | 4 | ICU syntax that doesn't parse; a plural with no `other` branch; a missing key seen on a critical-path page |
| High | 3 | A missing key; a placeholder mismatch; an empty value; truncation or overlap (measured); a plural missing CLDR categories but with `other`; hard-coded text confirmed in the UI; fixed widths confirmed by truncation |
| Medium | 2 | Hand-formatted numbers, currency or dates; concatenated translations; RTL mirroring problems (measured); hard-coded text seen only in code; an untranslated value |
| Low | 1 | Unused and duplicate keys; fixed widths and physical CSS seen only in code; hard-coded text in a single-locale product (readiness) |

A **single-locale product** (no locale ships besides the source: `candidates.json` `single_locale`) has
nothing shown in another language yet, so its findings are readiness findings for a future locale:
hard-coded text is Low; concatenation and formats stay Medium (they will break the first translation).

## Confidence

| Confidence | When |
|---|---|
| high | Parser-verified (missing, empty, duplicate and unused keys, ICU syntax, placeholders, plural categories), or confirmed at runtime with a measurement |
| medium | Hard-coded text confirmed in the rendered UI; code findings (concatenation, format) not yet seen at runtime |
| low | Hard-coded text, fixed widths and physical CSS seen only in code; untranslated values (never higher) |

## Reach, leverage, priority

- **Reach:** locales affected: 1 for one, 2 for two to four, 3 for five or more or every shipped
  locale. Code-level problems (hard-coded text, concatenation, formats, CSS) affect every shipped locale.
- **Leverage:** 2 when one fix covers several pages (the evidence spans two or more pages, or the same
  hard-coded text is in several files), otherwise 1.
- **Score** = severity weight × reach × leverage. P0 ≥ 24, P1 ≥ 12, P2 ≥ 6, otherwise P3. A Critical
  finding is at least P1.

## Runtime measurements

| Kind | Measured |
|---|---|
| truncation | text wider than its box while overflow is hidden or clipped: overflow, box and text width in px |
| overlap | two text elements (neither containing the other) whose boxes intersect: area in px |
| untranslated (pseudo run) | visible text outside ⟦ ⟧; partly translated text points at concatenation |
| raw_placeholder | `{name}` or `{{name}}` still showing |
| missing_key | the text is a message key |
| format | decimal separator or currency position against CLDR's patterns; date separators against CLDR's short date |
| rtl_mirroring | `dir` not rtl; left/right margin, padding or border with nothing on the other side; arrows pointing right |

Pseudo-localisation: accented look-alikes, 50% longer up to 10 letters, 40% up to 20, 30% above, and
⟦ ⟧ around each message. ICU arguments, plural keys and `#` are kept.

## Headline

- **Verdict:** `not_assessed` when no resource set was parsed, the code scan read no file and nothing
  rendered; `readiness_only` for a single-locale product (the summary must say only one locale ships);
  otherwise `issues_found` with at least one finding, else `none_detected`.
- **Evidence completeness:** `partial` when a locale wasn't run or was excluded, something needs
  review, unused keys couldn't be assessed for a set, a resource file couldn't be parsed, no resource
  set was found, the code scan read no file, the pseudo-locale was a text walk (it can't tell
  hard-coded text from translated text), or the product is single-locale.
- **Overall confidence:** low when nothing was analysed; medium when partial or any finding isn't
  high; otherwise high.
- A headline more cautious than the computed one (partial, a lower confidence, `not_assessed` for
  `none_detected` or `readiness_only`) is accepted.
- **Claims and counts:** no "fully localised", no "RTL-ready" / "supports RTL" without an RTL locale
  checked, no "ready for localisation" / "ready for any language"; numbers in the summary (issues,
  severities, need review, dismissed) equal the report's.
- **Agent findings** (`source: "agent"`, with a `basis` re-read by `--repo`) are never high
  confidence; rate their severity with the table above.
