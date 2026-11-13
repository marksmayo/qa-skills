---
name: wcag-auditor
description: Audit live web pages against WCAG (2.2 AA by default) without needing the source code. Runs three open-source engines inside a real browser (axe-core, IBM Equal Access, HTML_CodeSniffer), adds keyboard, focus, reflow, text-spacing, zoom, target-size, hover, motion, language and media probes, merges and de-duplicates the results by component, maps every issue to a success criterion, rates severity with a published rubric, and gives every SC at the target level an EARL outcome. Untested criteria become a manual test plan. The report never says the site conforms.
version: 1.2.0
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
8. **Change nothing.** Form submission is off by default: every non-GET request is aborted.
   Third-party requests are blocked too (GETs included), so an audit never sends analytics hits
   or ad requests; service workers are blocked. Don't click destructive buttons, don't bypass
   CAPTCHAs (record the page as blocked), and never write credentials into any output.
9. **A probe that couldn't run is not a pass.** The runner injects the engines, its probe helpers
   and a text-spacing stylesheet; a Content-Security-Policy with a hash or nonce refuses them
   silently. Pages therefore run with the CSP bypassed (`bypass_csp`, default true; it changes
   nothing for real users), every injection is checked, and one that didn't take makes that engine
   or probe `errored`, never "ran, nothing found". An SC passed on probe evidence needs its probe to
   have run on every planned check (the validator enforces it with `--run`).

## Inputs

| Input | Default | Notes |
|---|---|---|
| `targets` | required | URLs, a sitemap URL, or a start URL plus a crawl limit |
| `wcag_version` | `2.2` | Also `2.1` and `2.0`. In 2.2, 4.1.1 Parsing is obsolete and recorded as `inapplicable` |
| `level` | `AA` | `A`, `AA` or `AAA`. Requirements above the target are advisories |
| `critical_paths` | optional | Named flows as steps (`url`, optional `state`, `label`), or journey files from journey-mapper |
| `auth` | optional | `{"storage_state": file}`: a session a person captured by signing in by hand (`python scripts/browser_harness.py capture-session --url <app> --out <file outside the repo>`; its sessionStorage is restored too), or a login recipe whose fields read credentials from environment variables; `run.json` records only which kind was used |
| `viewports` | `1280x800`, `320x640` | 320 px also covers 1.4.10 Reflow |
| `max_pages` | 25 | Sampling cap for crawled and sitemap pages; listed targets are always audited |
| `crawl.max_loads` | 4 x `max_pages` | Most pages the crawl loads; it stops sooner once `max_pages` templates are found or `max_pages` loads in a row add no new template |
| `settle_seconds` | 10 | Most time spent waiting for each load to settle (network quiet for 500 ms, then a quiet DOM). Event streams, websockets, long-poll and hot-reload requests, and any request open longer than `long_lived_seconds` (4), don't hold it open; `events` names them |
| `random_sample` | 0.1 | Share of the remaining crawled pages added at random (seeded); `0` adds none |
| `third_party` | `block` | Every request to a host that isn't loopback, a target's host or in `allowed_hosts` is aborted and logged. Third-party content (embeds, fonts, widgets from other hosts) is then absent: say so in `limitations`, or let a host through |
| `allowed_hosts` | none | Further hosts to let through, such as a CDN the site needs (`"*.cdn.example"` allowed) |
| `service_workers` | `block` | `allow` lets the site register them |
| `allow_form_submission` | `false` | Only the user can turn it on, for a test environment |
| `bypass_csp` | `true` | Run pages with their CSP bypassed so engines and probes can be injected; `false` audits the CSP as served (injections it refuses are recorded as errored) |
| `consent` | none | An overlay to dismiss (`dismiss` selector or `steps`), `engines` `both` (default), `before` or `after`, and `probes_with_overlay` (default `["keyboard"]`) |
| `standards_map` | off | `en301549` adds EN 301 549 clause numbers (V3.2.1; WCAG 2.2 additions have none) |

If something essential is missing (the target, or authorisation for a non-loopback target), ask
once, then proceed with the defaults.

## Workflow (WCAG-EM)

### 1. Define the scope

Record the version, level, targets, exclusions (third-party embeds, PDFs, out-of-scope
subdomains) and critical paths. Write `audit_config.json` (`python scripts/run_audit.py --help`
lists every field). Use `states` for UI that only appears after an action (open menus, dialogs,
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

The runner crawls (or reads the sitemap), printing each page it loads, groups pages into templates by
URL pattern, page-level landmarks and the kinds of element in `main` (content landmarks such as
regions, labelled sections, forms and dialogs, and repeat counts, are left out, so a docs page with
four code-block regions shares a template with one that has none), and samples every critical-path page and every listed target (never dropped for
sharing a template), one page per template not yet covered, and about 10% more of the rest at
random (seeded). Each page records how it entered the sample (`critical_path`, `listed`,
`structured`, `random`, or `unreachable`). Check the sample makes sense before the full run; add
targets for templates the crawl missed. A plan-only run records every engine as `not_run` with the
reason "plan only": that is expected, not a failure. Responses that aren't HTML (a linked `.txt`,
XML or JSON file) are skipped, not sampled; `run.json` lists them under `crawl.skipped_non_html`
with why the crawl stopped.

### 4. Run engines and probes

```bash
python scripts/run_audit.py --config audit_config.json --out <run-dir>
```

For every sampled page and viewport the runner takes a full-page screenshot and runs the engines
with any consent banner or overlay showing (it's content too). With `consent` configured it then
runs `probes_with_overlay` (the keyboard probe by default: does focus enter the overlay, is focus
hidden behind it?) with it open (state `consent_open`), dismisses it, runs the engines again on the
uncovered page (state `consent_dismissed`, unless `engines` is `before`), and runs the probes;
`run.json` records which ran in each state. The motion probe watches fresh loads in their own pages,
so the audited page is never reloaded under later probes.

| Probe | SCs | What it measures |
|---|---|---|
| `keyboard` | 2.1.1, 2.1.2, 2.4.3 | Tab order from the top of the document (an invisible starting point focused at the start of the body; nothing is clicked, so the first Tab reaches a skip link even under a fixed header); traps (no new element for 15 presses, then Shift+Tab and Escape tried); clickable elements that aren't focusable, with their click listeners; menu links that only `:hover` reveals and no key opens (focus, ArrowDown, and Enter and Space unless the control follows a link or submits a form). Clickable heading text whose heading holds a focusable `#` anchor link is not reported; a clickable element with a focusable sibling link or button is needs review, not failed. In a named state with a modal open and focus inside it, Tab starts from the focused element and only the modal's controls are expected (`start: active_element`) |
| `focus_visible` | 2.4.7, 2.4.11, 2.4.12 | Pixels changed around each element when focused; whether fixed or sticky content covers it (a fixed header that contains the focused control doesn't count); focus guards (`data-*focus-guard`, 0 x 0 `aria-hidden` sentinels) are not measured |
| `reflow` | 1.4.10 | Horizontal scrolling at 320 px and the element causing it (tables, video and other two-dimensional content flagged as possibly exempt); content past the viewport on a page that can't scroll sideways (cut off: `reflow.clipped`, failed), or cut off by an ancestor with overflow hidden (needs review) |
| `text_spacing` | 1.4.12 | Text clipped once the WCAG spacing overrides are applied (visually hidden `.sr-only` text excluded); the overrides are read back, and a refused stylesheet makes the probe `errored` |
| `resize_text` | 1.4.4 | Text clipped at 200% zoom (half the viewport at device scale 2; visually hidden text excluded); viewport meta that blocks zoom |
| `target_size` | 2.5.8 | Pointer targets (links, buttons, controls, ARIA widgets; never a `tabindex` container or a visually hidden link) under 24 x 24 px whose 24 px circle overlaps another target, measured at the top of the page; fixed or sticky targets are compared only with each other; inline links exempt |
| `hover_focus` | 1.4.13 | Content shown on hover: dismissible with Escape, hoverable, persistent. Anything that appears inside the hovered element's own box (an icon, a tint) is not additional content and is only counted (`own_box_changes`) |
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
The map is generated from the engines' own metadata; its `overrides` table corrects the few
entries that metadata gets wrong (IBM `img_alt_redundant` is best practice, like axe's
`image-redundant-alt`, not a 1.1.1 failure).
`merge_findings.py` merges results about the same element (or an image and its link) across
tools, viewports and states, then groups them by component signature across pages. Each
candidate carries its sources, outcome, a suggested confidence and the rubric's default failure
pattern. `sc_draft` gives each SC a draft outcome; it never drafts `passed`.

Contrast "needs review" results (axe `incomplete`, HTMLCS `.Alpha`/`.BgImage`/`.Abs`, IBM
potentials) can bury the real failures when a page has a faint background gradient. The runner
records what lies behind each such element: the background image and the element carrying it,
translucent layers, and the text colour and size. `merge_findings.py` then gives each candidate a
`contrast_estimate` (the worst-case ratio against the gradient's colour stops, which bound a
gradient's luminance) and collapses candidates with the same cause and the same background into
one grouped candidate whose `group` lists the cause, the background, the member components,
counts, text colours and sample selectors. Candidates estimated below the threshold are never
grouped; they stay separate so they can be promoted. Visually hidden text is grouped on its own.
Runs without that record (older runs) are not grouped. Colours in any CSS syntax are converted to
sRGB (`rgb()`, hex, `hsl()`, `hwb()`, `lab()`, `lch()`, `oklab()`, `oklch()`, `color(srgb …)`,
`display-p3`, `xyz`; Tailwind 4 writes `oklch()` and Chromium reports some colours as `lab()`); one
it can't parse gives no estimate rather than a wrong one (`python scripts/merge_findings.py
--self-test` checks the conversions against known ratios). Text over an `<img>`, `<video>`,
`<canvas>` or `<svg>` painted behind it has an unknown background: its estimate has no ratio
(`unknown_background`), and the candidate is never grouped.

### 6. Triage every candidate

Every candidate gets exactly one triage entry:

- **finding**: a WCAG A/AA failure you are confident of. One root cause is one finding, even when
  it spans several candidates and SCs (an unnamed logo link reported as both 1.1.1 and 2.4.4).
- **needs_review**: a `cantTell` that a person must check, with `what_to_check`. A `failed`
  candidate kept as needs review needs a reason saying why the failure is not confirmed (what you
  looked at); repeating the needs-review text or the tool message is rejected.
- **advisory**: best practice, above the target level, outside the target version, or `obsolete`
  (4.1.1 in WCAG 2.2): the advisory kind follows the candidate's kind.
- **dismissed**: with a reason someone can check, such as the computed contrast ratio and the
  colours it came from, the probe measurement that contradicts the hint, or the exemption that
  applies. "False positive" alone is rejected.

A grouped contrast candidate is triaged once, like any other. A `contrast_estimate` comes from
recorded colours, not pixels: dismiss on it only when the group's lowest estimate clears the
threshold and you say so with the colours (`#222 on a #fafafa-#eef4ee gradient: at least 14.2:1`);
otherwise send it for review with the sample selectors. Check a sample in the screenshot either
way, because images, shadows and overlapping content aren't in the estimate; when the text colour
isn't plain `rgb()` (an `oklch()` or `lab()` theme), check the screenshot pixels first. A contrast
candidate with an estimate below the threshold, an unknown background, or a group with no estimate
is never dismissed (the validator rejects it).

A finding built only from `cantTell` candidates is agent judgment and needs a `resolution` saying
what you looked at.

### 7. Assisted review

Using the screenshots, the accessibility-tree snapshots and the page itself, review the SCs that
need judgment: 1.1.1 alt-text quality, 1.3.1 visual structure against markup, 1.3.5
autocomplete, 1.4.1 colour-only meaning, 2.4.2 and 2.4.6 descriptive titles, headings and labels,
2.4.4 link purpose, 3.2.3/3.2.4/3.2.6 consistency across pages, and 3.3.1/3.3.2 errors and
labels (use the form-error state). List what you reviewed in `assisted_review.scs_reviewed`; every
SC passed or failed with `agent_review` in its basis must be listed there.
Findings from this step are `agent_judgment`.

### 8. Rate

Follow `RUBRIC.md`:

- **Severity** starts at the default of the matching failure pattern in `data/wcag-sc.json`. It
  goes up one level when the finding is on a critical-path step and affects what that step needs,
  and it is always Critical for the non-interference SCs (1.4.2, 2.1.2, 2.2.2, 2.3.1). To deviate
  from the default, give `severity_override_reason`. Tool impact labels are inputs only.
- **Confidence**: `high` needs two agreeing failing sources or a failing rule that implements an
  ACT rule; one heuristic engine or one probe is `medium`; agent judgment is `medium` at most. A
  failed SC's confidence is never above the most confident finding that fails it.
- **Priority** = severity weight x reach x leverage, with floors; see `RUBRIC.md`. Set
  `blocks_critical_path` when a user group can't complete the step at all; that makes it P0.

### 9. Build the SC matrix and the manual test plan

Every SC at the target version and level gets one `sc_results` row. `failed` lists its findings;
`cantTell` lists its needs-review items; `inapplicable` says why (no media, no authentication);
`passed` says what evidence covers the whole criterion; everything else is `untested` and goes in
the `manual_test_plan` with concrete steps and pages.

### 10. Report, validate and render

Write `audit.json` (described by `schemas/audit.schema.json`; its `schema_version` is `"1.1"`; `"1.0"` reports still validate) next
to the run directory, then:

```bash
python scripts/validate_audit.py audit.json --run <run-dir> --candidates <run-dir>/candidates.json --write-summary
python scripts/render_report.py audit.json [--brief]
```

`--write-summary` fills or recomputes the headline numbers before anything is checked, so the draft
can leave them out; it never writes the verdict or its text. Fix every
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
  match `run.json`, so nothing claims to have run that didn't; an SC passed on probe evidence had
  its probes run on every planned check.
- With `--candidates`: no contrast candidate below the threshold, over an image or video, or
  grouped without an estimate is dismissed.

## Edge cases

- **Consent banners and overlays:** engines run with the overlay showing and again after
  `consent.dismiss` (or `consent.steps`) closes it; the keyboard probe also runs with it open; the
  other probes and states run after dismissal. A dismissal that fails is recorded
  (`consent_dismissed: false`) and the engines are not rerun.
- **Strict CSP (Next.js and similar):** leave `bypass_csp` on; with it off, expect engines and
  probes to be `errored` on pages whose CSP refuses injected scripts or styles.
- **CAPTCHAs:** stop, record the page as blocked, report affected SCs as `untested`.
- **SPAs:** the runner waits for network idle and a quiet DOM; add `wait_for` actions for slow views.
- **Dev servers** (hot reload, socket.io long-poll, event streams) never go network-idle: those
  connections are ignored and each load's settle is capped (`settle_seconds`); `events` records it.
- **Infinite scroll:** capped by `max_pages`; don't scroll endlessly.
- **WAF or rate limits:** the runner backs off; if it keeps failing, record it in `events` and limitations.
- **Login:** use a captured session (`capture-session`) or a login recipe with `env` fields. Never put credentials in the config, and keep the session file outside the repository.
- **PDFs:** out of scope; point to veraPDF.
