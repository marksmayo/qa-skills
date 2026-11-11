# i18n checker rubric

`merge_findings.py` computes these; `validate_report.py` checks the report against them.

## Severity

| Severity | Weight | When |
|---|---|---|
| Critical | 4 | ICU syntax that doesn't parse; a plural with no `other` branch; a missing key seen on a critical-path page |
| High | 3 | A missing key; a placeholder mismatch; an empty value; truncation or overlap (measured); a plural missing CLDR categories but with `other`; hard-coded text confirmed in the UI; fixed widths confirmed by truncation |
| Medium | 2 | Hand-formatted numbers, currency or dates; concatenated translations; RTL mirroring problems (measured); hard-coded text seen only in code; an untranslated value |
| Low | 1 | Unused and duplicate keys; fixed widths and physical CSS seen only in code |

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

- **Verdict:** `issues_found` with at least one finding, otherwise `none_detected`.
- **Evidence completeness:** `partial` when a locale wasn't run or was excluded, something needs
  review, unused keys couldn't be assessed for a set, or a resource file couldn't be parsed.
- **Overall confidence:** medium when partial or any finding isn't high; otherwise high.
