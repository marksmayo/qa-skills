---
name: wcag-auditor
description: Audit live web pages against WCAG (2.2 AA by default) without needing the source code. Runs three open-source engines inside a real browser (axe-core, IBM Equal Access, HTML_CodeSniffer), adds keyboard, focus, reflow, text-spacing, zoom, target-size, hover, motion, language and media probes, merges and de-duplicates the results by component, maps every issue to a success criterion, rates severity with a published rubric, and gives every SC at the target level an EARL outcome. Untested criteria become a manual test plan. The report never says the site conforms.
version: 1.0.0
---

# WCAG Auditor

## Mission

Find the accessibility barriers on a website that real people will hit, explain who they hurt and
how to fix them, and say plainly how much of WCAG the evidence actually covers.

What sets this apart from running one checker:

- **Three engines and a set of browser probes.** axe-core, IBM Equal Access and HTML_CodeSniffer
  run inside the page; the probes test what engines can't: Tab traversal and traps, focus
  visibility and obstruction, reflow at 320 px, text spacing, 200% zoom, target size, hover
  content, motion and page language.
- **One issue, one finding.** Results from every tool, viewport and state are merged per
  element, then per component, so a broken shared header is one finding listing every page.
- **A success-criterion matrix.** Every SC at the target level gets `passed`, `failed`,
  `cantTell`, `inapplicable` or `untested`, with the evidence for it.
- **Honest about the limits.** Automated tools can check only part of WCAG. The SCs nobody could
  test become a manual test plan with concrete steps.

## Non-negotiable rules

1. **Never say the site conforms.** WCAG conformance can't be established by automated and
   assisted testing. The verdict is `does_not_conform` (at least one high-confidence A or AA
   failure) or `not_established`. Never write "compliant", "conforms" or "accessible" as a
   conclusion. The best possible result is "No failures detected by automated and assisted
   testing; conformance not established."
2. **Use the EARL outcomes and never inflate them.** An SC with no evidence is `untested`, never
   `passed`. A passing tool rule doesn't pass the SC: if axe passes `color-contrast`, only the text
   it could compute passed. An SC is `passed` only with probe, agent-review or manual evidence for
   the whole criterion on the sampled pages, and an SC no tool can check (`automatable: none`)
   needs agent-review or manual evidence.
3. **"Needs review" stays `cantTell`.** axe `incomplete`, IBM `POTENTIAL`/`MANUAL` and HTMLCS
   warnings are never counted as failures and never discarded. Each one is promoted (with
   evidence), turned into a needs-review item, or dismissed with a reason someone can check.
4. **Best practice is not a WCAG failure.** axe `best-practice` rules, IBM recommendations, and
   SCs above the target level or outside the target version are advisories.
5. **Every finding records where it came from:** tool, version, rule id, ACT rule ids, selector,
   HTML snippet, URL, viewport, state and screenshot.
6. **Agent judgment gets a lower confidence.** Alt-text quality, whether an error message helps,
   whether a heading is really a heading: tag these `evidence_type: agent_judgment`, rate them
   `medium` at most (`low` when nothing corroborates them), and say in `resolution` what you
   looked at.
7. **Test only what you are allowed to test.** Before scanning, confirm the user owns the target
   or is authorised to test it, and record who confirmed it (`authorised_by`). Loopback targets
   need no confirmation. Respect rate limits; back off on 429 and 503.
8. **Change nothing.** Form submission is off by default: every non-GET request is aborted. Don't
   click destructive buttons, don't bypass CAPTCHAs (record the page as blocked), and never write
   credentials into any output.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `targets` | required | URLs, a sitemap URL, or a start URL plus a crawl limit |
| `wcag_version` | `2.2` | Also `2.1` and `2.0`. In 2.2, 4.1.1 Parsing is obsolete and recorded as `inapplicable` |
| `level` | `AA` | `A`, `AA` or `AAA`. Requirements above the target are advisories |
| `critical_paths` | optional | Named flows as steps (`url`, optional `state`, `label`), or journey files from journey-mapper |
| `auth` | optional | A Playwright `storageState` file, or a login recipe whose fields read credentials from environment variables |
| `viewports` | `1280x800`, `320x640` | 320 px also covers 1.4.10 Reflow |
| `max_pages` | 25 | Sampling cap |
| `allow_form_submission` | `false` | Only the user can turn it on, for a test environment |
| `standards_map` | off | `en301549` adds EN 301 549 clause numbers (V3.2.1; WCAG 2.2 additions have none) |

If something essential is missing (the target, or authorisation for a non-loopback target), ask
once, then proceed with the defaults.

## Workflow (WCAG-EM)

### 1. Define the scope

Record the version, level, targets, exclusions (third-party embeds, PDFs, out-of-scope
subdomains) and critical paths. Write `audit_config.json` (see `scripts/run_audit.py --help` for
every field). Use `states` for UI that only appears after an action (open menus, dialogs,
accordions, form errors), and reference them from critical-path steps.

### 2. Check tooling

```bash
pip install -r requirements.txt && python -m playwright install chromium   # ask first
python scripts/fetch_engines.py                       # pinned engines, SHA-512 and SHA-256 verified
python scripts/fetch_engines.py --include-optional    # also the Nu HTML Checker (needs Java)
```

Each engine is verified against `data/engines.lock.json` before it is cached, and again before it
is injected; a file that fails either check is never used. A missing or failed engine is
recorded as `not_run` or `errored` with the reason, lowers evidence completeness, and must be
named in `limitations`. If installs aren't allowed, say what can't be done; don't simulate it.

### 3. Explore and sample

```bash
python scripts/run_audit.py --config audit_config.json --out <run-dir> --plan-only
```

The runner crawls (or reads the sitemap), groups pages into templates by URL pattern and landmark
structure, and samples one page per template, every critical-path page, and about 10% more at
random (seeded). Each page records why it was chosen. Check the sample makes sense before the full
run; add targets for templates the crawl missed.

### 4. Run engines and probes

```bash
python scripts/run_audit.py --config audit_config.json --out <run-dir>
```

For every sampled page and viewport the runner takes a full-page screenshot, runs the engines
with any consent banner showing (it's content too), dismisses the banner, then runs the probes:

| Probe | SCs | What it measures |
|---|---|---|
| `keyboard` | 2.1.1, 2.1.2, 2.4.3 | Tab order; traps (no new element for 15 presses, then Shift+Tab and Escape tried); clickable elements that aren't focusable, with their click listeners |
| `focus_visible` | 2.4.7, 2.4.11, 2.4.12 | Pixels changed around each element when focused; whether fixed or sticky content covers it |
| `reflow` | 1.4.10 | Horizontal scrolling at 320 px and the element causing it (tables, video and other two-dimensional content flagged as possibly exempt) |
| `text_spacing` | 1.4.12 | Text clipped once the WCAG spacing overrides are applied |
| `resize_text` | 1.4.4 | Text clipped at 200% zoom (half the viewport at device scale 2); viewport meta that blocks zoom |
| `target_size` | 2.5.8 | Targets under 24 x 24 px whose 24 px circle overlaps another target; inline links exempt |
| `hover_focus` | 1.4.13 | Content shown on hover: dismissible with Escape, hoverable, persistent |
| `motion` | 2.2.2 | Content still changing 5 to 7.5 s after load with no pause control; again with reduced motion |
| `a11y_tree` | 1.3.1, 2.4.6, 4.1.2 | Accessibility-tree snapshot, headings and landmarks for the assisted review |
| `language` | 3.1.1 | `lang` present, valid, and matching the language of the text |
| `media` | 1.2.x, 1.4.2 | Video and audio elements, tracks, autoplay; embedded players |

States from the config run the engines again, plus any probes listed for them. CAPTCHAs stop the
page (`blocked`); cross-origin frames the engines can't reach are `untested`.

### 5. Normalise and merge

```bash
python scripts/normalize.py <run-dir> --out <run-dir>/items.json
python scripts/merge_findings.py <run-dir>/items.json --run <run-dir> --out <run-dir>/candidates.json
```

`normalize.py` maps every result to SCs and ACT rules through `data/rule-map.json`, and classifies
it as `wcag`, `best_practice`, `above_level`, `outside_version`, `obsolete` or `experimental`.
`merge_findings.py` merges results about the same element (or an image and its link) across
tools, viewports and states, then groups them by component signature across pages. Each
candidate carries its sources, outcome, a suggested confidence and the rubric's default failure
pattern. `sc_draft` gives each SC a draft outcome; it never drafts `passed`.

### 6. Triage every candidate

Every candidate gets exactly one triage entry:

- **finding**: a WCAG A/AA failure you are confident of. One root cause is one finding, even when
  it spans several candidates and SCs (an unnamed logo link reported as both 1.1.1 and 2.4.4).
- **needs_review**: a `cantTell` that a person must check, with `what_to_check`.
- **advisory**: best practice, above the target level, or outside the target version.
- **dismissed**: with a reason someone can check, such as the computed contrast ratio and the
  colours it came from, the probe measurement that contradicts the hint, or the exemption that
  applies. "False positive" alone is rejected.

A finding built only from `cantTell` candidates is agent judgment and needs a `resolution` saying
what you looked at.

### 7. Assisted review

Using the screenshots, the accessibility-tree snapshots and the page itself, review the SCs that
need judgment: 1.1.1 alt-text quality, 1.3.1 visual structure against markup, 1.3.5
autocomplete, 1.4.1 colour-only meaning, 2.4.2 and 2.4.6 descriptive titles, headings and labels,
2.4.4 link purpose, 3.2.3/3.2.4/3.2.6 consistency across pages, and 3.3.1/3.3.2 errors and
labels (use the form-error state). List what you reviewed in `assisted_review.scs_reviewed`.
Findings from this step are `agent_judgment`.

### 8. Rate

Follow `RUBRIC.md`:

- **Severity** starts at the default of the matching failure pattern in `data/wcag-sc.json`. It
  goes up one level when the finding is on a critical-path step and affects what that step needs,
  and it is always Critical for the non-interference SCs (1.4.2, 2.1.2, 2.2.2, 2.3.1). To deviate
  from the default, give `severity_override_reason`. Tool impact labels are inputs only.
- **Confidence**: `high` needs two agreeing failing sources or a failing rule that implements an
  ACT rule; one heuristic engine or one probe is `medium`; agent judgment is `medium` at most.
- **Priority** = severity weight x reach x leverage, with floors; see `RUBRIC.md`. Set
  `blocks_critical_path` when a user group can't complete the step at all; that makes it P0.

### 9. Build the SC matrix and the manual test plan

Every SC at the target version and level gets one `sc_results` row. `failed` lists its findings;
`cantTell` lists its needs-review items; `inapplicable` says why (no media, no authentication);
`passed` says what evidence covers the whole criterion; everything else is `untested` and goes in
the `manual_test_plan` with concrete steps and pages.

### 10. Report, validate and render

Write `audit.json` (described by `schemas/audit.schema.json`) next to the run directory, then:

```bash
python scripts/validate_audit.py audit.json --run <run-dir> --candidates <run-dir>/candidates.json --write-summary
python scripts/render_report.py audit.json [--brief]
```

`--write-summary` recomputes the headline numbers; it does not write the verdict text. Fix every
problem the validator reports, then give the user the rendered report: the verdict and its
reason, the P0 and P1 findings, blocked critical paths, what was untested and the limitations.

## Report fields that the validator checks

- The verdict follows the evidence and the verdict text claims no conformance.
- `sc_results` has every target SC exactly once, matching `data/wcag-sc.json`; failed SCs and
  findings agree; no tool-only passes; `untested` SCs are in the manual plan; `cantTell` SCs have
  needs-review items; 4.1.1 is `inapplicable` in 2.2.
- Findings: SCs inside the target; a valid failure pattern; severity from the rubric; confidence
  rules; priority, reach and leverage recomputed; priority order; critical-path steps that exist
  and match the finding's pages; instances on sampled pages with screenshots that exist.
- Triage: every candidate exactly once; references that exist and point back; checkable
  dismissals; no best-practice candidate promoted to a WCAG finding; finding sources come from
  their candidates.
- Headline: severity counts, SC coverage, evidence completeness (ran / planned checks), overall
  confidence and blocked critical paths recomputed.
- With `--run`: tool statuses, versions, the check matrix, the sample and the browser version
  match `run.json`, so nothing claims to have run that didn't.

## Edge cases

- **Consent banners:** engines run with the banner showing, then `consent.dismiss` closes it for
  the probes and states.
- **CAPTCHAs:** stop, record the page as blocked, report affected SCs as `untested`.
- **SPAs:** the runner waits for network idle and a quiet DOM; add `wait_for` actions for slow views.
- **Infinite scroll:** capped by `max_pages`; don't scroll endlessly.
- **WAF or rate limits:** the runner backs off; if it keeps failing, record it in `events` and limitations.
- **Login:** use `storageState` or a login recipe with `env` fields. Never put credentials in the config.
- **PDFs:** out of scope; point to veraPDF.
