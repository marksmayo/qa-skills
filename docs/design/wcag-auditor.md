# Design: `wcag-auditor-skill`

Status: Built (v1.0.0), see "As built" at the end. Build order: 6.

## Mission

The skill audits live web pages against WCAG. It needs only URLs, not source code. It runs
several open-source accessibility engines, adds browser probes that the agent drives, merges and
de-duplicates the results, maps each issue to a WCAG success criterion (SC), and rates severity
using a published rubric.

## Decisions

- **Default target is WCAG 2.2 Level AA.**
- **No Node.js.** The page runner is Python, using **Playwright for Python**.
  - **Engines run inside the page.** All three engines (axe-core, IBM Equal Access `ace.js`,
    HTML_CodeSniffer) are self-contained browser scripts, so they're injected into the page with
    `page.add_script_tag` and run there. No Node packages or wrappers are needed.
  - **This skill does need one install.** Its dependency is declared in the skill's own
    `requirements.txt`, and setup is `pip install -r requirements.txt` followed by
    `playwright install chromium`. That's an exception to the stdlib-only rule, and the root
    `CLAUDE.md` must note it.
  - **Everything else stays stdlib Python:** normalising, rating, validating and rendering.
- **No single numeric score.** A 1–10 score would suggest a conformance level the evidence can't
  support. The headline is a verdict, the severity counts, and SC coverage.
- **Optional mapping to EN 301 549 in v1.** Australia has adopted this standard as AS EN 301
  549, and Australian government procurement uses it. Section 508 (the US equivalent) mapping is
  deferred to a later version.

## What it can't do, and the honesty rules

- **Automated tools catch only part of WCAG.** Roughly 30–40% of SCs can be fully or partly
  checked by machines. The skill **never** outputs "conforms" or "compliant". The best possible
  verdict is "No failures detected by automated and assisted testing; conformance not
  established".
- **Results use the W3C EARL outcome terms:** `passed`, `failed`, `cantTell`, `inapplicable`,
  `untested`. An SC with no evidence is `untested`, never `passed`.
- **A passing tool rule doesn't mean the SC passes.** If axe passes `color-contrast`, only the
  text it could compute passed; 1.4.3 as a whole is not proven.
- **Tool "needs review" results are `cantTell`.** axe `incomplete`, IBM `potentialviolation` and
  Pa11y warnings are never counted as failures and never discarded.
- **Best practice isn't a WCAG failure.** axe `best-practice` rules and similar are reported as
  advisories.
- **Every finding records where it came from:** tool and version, rule ID, ACT rule ID, selector,
  HTML snippet, URL, viewport and screenshot.
- **Agent judgment gets a lower confidence.** Examples are alt-text quality and how helpful an
  error message is. These are tagged `evidence_type: agent_judgment`, rated at most medium
  confidence, and kept separate from tool-verified findings.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `targets` | required | URLs, a sitemap URL, or a start URL plus a crawl limit |
| `wcag_version` | `2.2` | Also `2.1` and `2.0`. In 2.2, 4.1.1 Parsing is obsolete and treated as `inapplicable` |
| `level` | `AA` | `A`, `AA` or `AAA`. Requirements above the target level are reported as advisories |
| `critical_paths` | optional | Named flows given as steps, or journey files from journey-mapper |
| `auth` | optional | A Playwright `storageState` file or a login recipe. Credentials are never written to output |
| `viewports` | `1280x800`, `320x640` | The 320 px width also covers 1.4.10 Reflow |
| `max_pages` | 25 | Sampling cap |
| `allow_form_submission` | `false` | Safe mode: no POSTs or destructive clicks unless explicitly allowed |
| `standards_map` | off | `en301549` maps findings to EN 301 549 clauses |

**Authorisation check:** before scanning, the skill confirms that the user owns the target or is
authorised to test it. Requests are rate-limited.

## Workflow (follows WCAG-EM, W3C's standard evaluation method)

1. **Define scope.** Record the version, level, targets and exclusions (third-party embeds,
   PDFs).
2. **Explore and sample.**
   - Crawl or read the sitemap.
   - Group pages by template, using URL patterns and the DOM structure of their landmarks.
   - **Structured sample:** one page per template, plus every critical-path step.
   - **Random sample:** about 10% extra pages.
   - Record why each page was chosen.
3. **Check tooling.** Record each engine's availability and version. A missing engine is
   recorded as `not_run` and lowers evidence completeness.
4. **Automated engines** run on each page, viewport and state. For SPAs and dynamic UI they also
   run after opening menus, modals, accordions and form-error states.
5. **Probes** (see below).
6. **Normalise and merge** into one finding format, then de-duplicate and group by component.
7. **Assisted review.** The agent checks a bounded list of SCs that need judgment, using
   screenshots and accessibility-tree snapshots. These are tagged `agent_judgment`.
8. **Rate:** severity, confidence and priority.
9. **SC matrix.** Every SC at the target level gets an EARL outcome. The SCs still untested
   become a **manual test plan** with concrete steps.
10. **Output, validate and render.**

## Tool stack

All engines are injected into the page by the Python runner.

| Engine (browser script) | Licence | Role |
|---|---|---|
| axe-core (`axe.min.js`) | MPL-2.0 | Baseline engine with few false positives, aligned with ACT rules. Handles Shadow DOM and iframes |
| IBM Equal Access engine (`ace.js`, from `accessibility-checker-engine`) | Apache-2.0 | Second engine, with WCAG 2.2 rule mappings |
| HTML_CodeSniffer (`HTMLCS.js`) | BSD-3-Clause | Third perspective, and the engine Pa11y wraps. Rule codes encode SC numbers directly |
| Nu HTML Checker (`vnu.jar`) | MIT | Optional (needs Java). Duplicate IDs and invalid ARIA attributes, as supporting evidence only. Never sent to the hosted W3C service by default |
| Lighthouse, Pa11y CLI | — | **Left out.** Lighthouse's accessibility audit is axe, and Pa11y is a Node wrapper around axe and HTMLCS, so both would only duplicate results |

**Getting the engine scripts.**

- `scripts/fetch_engines.py` downloads each script at a **pinned version** from the npm
  registry or jsDelivr, using stdlib `urllib`.
- It checks each file against a **SHA-256** listed in `data/engines.lock.json`, and caches it
  locally. A file that fails the check is never injected.
- The version of each engine is recorded in the report metadata.

PDF accessibility is out of scope. The README points to veraPDF for anyone who needs it.

## Probes (Playwright for Python, driven by the agent)

| Probe | SCs | Method |
|---|---|---|
| Keyboard traversal | 2.1.1, 2.1.2, 2.4.3, 2.4.7 | Tab through the page. Record focus order, traps (focus stuck for N presses), unreachable interactive elements, and a screenshot per stop |
| Focus visibility and obstruction | 2.4.7, 2.4.11, 2.4.13 (AAA) | Compare pixels before and after focus. Check whether sticky headers or banners cover the focused element |
| Reflow | 1.4.10 | At 320 CSS px: horizontal scrolling and clipped content |
| Text spacing | 1.4.12 | Inject the text-spacing CSS, then detect overflow and clipping |
| Resize text | 1.4.4 | 200% zoom, then check for truncation |
| Target size | 2.5.8 | Bounding boxes against the 24×24 px rule, including the spacing exception |
| Content on hover or focus | 1.4.13 | Trigger it, then check it dismisses with Esc and stays while hovered |
| Motion | 2.2.2, 2.3.3 | Detect autoplay and animation. Compare behaviour with `prefers-reduced-motion` |
| Accessibility tree snapshot | 1.3.1, 2.4.6, 4.1.2 | Headings outline, landmarks, and name/role/value of controls, for the assisted review |
| Language | 3.1.1 | `lang` is present and valid, and matches the language detected from the page text |
| Media inventory | 1.2.x | List video, audio and `<track>` elements, mostly for the manual test plan |

## Rating model

WCAG defines conformance levels, not severity. So severity is a published rubric layered on top
of WCAG, and the report says so.

| Severity | Definition |
|---|---|
| **Critical** | A user group can't perceive, operate or complete essential content or a task. **Also automatic for any failure of the four non-interference SCs (1.4.2, 2.1.2, 2.2.2, 2.3.1)**, because WCAG conformance requirement 5 says these block use of the whole page |
| **Serious** | A Level A or AA failure that severely degrades use, where only a hard workaround exists |
| **Moderate** | An A or AA failure that causes friction but leaves a reasonable workaround |
| **Minor** | An A or AA failure with low practical impact |

How severity is set:

- **Starting point.** RUBRIC.md gives a default severity per SC *failure pattern*, not per SC.
- **Critical-path adjustment.** A failure on a critical-path step goes up one level, because
  conformance requirement 3 (complete processes) means one failed step fails the whole process.
- **Tool impact labels are inputs only.** axe and IBM impact levels are recorded but don't decide
  the rating.

The other two axes:

- **Confidence:**
  - **High:** a deterministic rule produced the result and it was confirmed by a second engine,
    a probe, or an ACT-consistent implementation.
  - **Medium:** a single engine's heuristic, or a probe result alone.
  - **Low:** agent judgment only.
- **Priority:** severity × reach (template and component groups) × fix leverage ("fix once,
  fixes N pages").

## De-duplication

- **Map every rule to an SC** using `data/rule-map.json`, which links axe tags, HTMLCS codes, IBM
  rule IDs and vnu messages to WCAG SCs and ACT rule IDs.
- **Fingerprint each finding** with (SC, ACT rule or normalised rule, normalised selector or
  accessibility-tree path, page template). Matching fingerprints across tools merge into one
  finding with several `sources`, and agreement raises confidence.
- **Group into components.** The same selector pattern across pages becomes one component finding
  with a list of affected pages.

## Output (`audit.schema.json`)

```text
metadata:     target, wcag_version, level, generated_at, browser+version,
              tools[{name, version, status: ran|not_run|errored}], viewports, auth_used,
              sample[{url, template, reason: structured|random|critical_path}]
headline:     conformance_verdict: "does_not_conform" | "not_established"   # never "conforms"
              severity_counts{critical, serious, moderate, minor}
              sc_coverage{total, failed, passed, cantTell, inapplicable, untested}
              evidence_completeness (0–1), overall_confidence, blocked_critical_paths[]
sc_results[]: {sc, title, level, principle, outcome (EARL), confidence,
               automatable: full|partial|none, finding_ids[], evidence_note, en301549_clause?}
findings[]:   {id, sc[], level, severity, confidence, priority, evidence_type: tool|probe|agent_judgment,
               sources[{tool, rule_id, act_rule}], component, pages[], instances[{url, selector,
               html_snippet, screenshot}], user_impact, affected_users[], remediation,
               verification_step, non_interference, critical_path}
advisories[]: same shape, kind: best_practice|above_target_level
needs_review[]: cantTell items with what a human must check
manual_test_plan[]: {sc, why_untested, steps[], pages[]}
```

## Validator fails when

- The verdict is `does_not_conform` without at least one high-confidence A or AA failure.
- An SC is marked `passed` with `automatable: none` and no probe or manual evidence.
- An `agent_judgment` finding has high confidence.
- `sc_results` is empty.
- A non-interference failure isn't rated Critical.
- The verdict is anything other than the two allowed values.

## Edge cases

- **Cookie and consent banners:** scan once with the banner showing (it's content too), then
  dismiss it.
- **CAPTCHAs:** stop and report as `untested`. Never try to bypass them.
- **Cross-origin iframes the engine can't reach:** report as `untested`.
- **Infinite scroll:** capped.
- **SPAs:** wait for network idle and for the DOM to settle.
- **WAF and rate limits:** back off and record what happened.

## Files

```text
wcag-auditor-skill/
  SKILL.md  RUBRIC.md  README.md (pip install -r requirements.txt + chromium, optional Java)
  requirements.txt          # playwright>=1.49,<2 — the skill's only third-party dependency
  data/wcag-sc.json         # all SCs 2.0–2.2: level, version added, automatable, non_interference
  data/rule-map.json        # tool rule → SC + ACT id
  data/en301549-map.json    # SC → EN 301 549 clause
  data/engines.lock.json    # pinned engine versions, download URLs, SHA-256
  schemas/audit.schema.json
  scripts/fetch_engines.py  # stdlib: download + verify + cache engine scripts
          run_audit.py      # Playwright for Python: inject engines, run probes → raw/*.json, screenshots/
          normalize.py  merge_findings.py  validate_audit.py  render_report.py   # stdlib
  examples/audit.example.json
```

## As built (v1.1.0)

Differences from the draft above, and decisions made while building:

- **Engines** are pinned in `data/engines.lock.json`: axe-core 4.14.0, IBM Equal Access
  (`accessibility-checker-engine`) 4.0.34, HTML_CodeSniffer 2.5.1 and, optionally, the Nu HTML
  Checker (`vnu-jar`) 26.10.12. `fetch_engines.py` downloads the npm tarball, checks the
  registry's SHA-512 integrity, reads out only the one file needed, and checks its SHA-256 (the
  vnu jar has no pinned SHA-256; its hash is recorded on first fetch and checked afterwards).
  `run_audit.py` re-verifies each file before injecting it. HTML_CodeSniffer's last release
  predates WCAG 2.2, so it contributes nothing for the 2.2 additions.
- **`data/rule-map.json` is generated from the engines' own metadata**: axe `wcagNNN` tags and
  `actIds`, IBM's WCAG ruleset checkpoints and the ACT column of its rule help. HTMLCS SCs are
  parsed from the message code. Probe result codes are mapped there too.
- **Severity is per failure pattern** (`failure_patterns` in `data/wcag-sc.json`, with a default
  severity each), as planned. A deviation needs `severity_override_reason`. The critical-path
  raise applies when the finding affects what the step needs, not to everything on that page.
- **Priority** is numeric: severity weight (4/3/2/1) x reach (3 on at least two pages and half
  the sample, 2 on two or more, 1 on one) x leverage (2 when one fix covers two or more
  instances). P0 >= 24, P1 >= 12, P2 >= 6, with floors (critical at least P1, serious at least P2,
  `blocks_critical_path` is P0) and a cap (agent judgment at most P1). The critical path raises
  severity only, so it isn't counted twice.
- **Confidence** `high` needs two agreeing failing sources or a failing rule that implements an
  ACT rule. A probe on its own is `medium`.
- **Merging** has two stages: per element (same page and XPath, or an element and its parent or
  grandparent, such as an image and its link, with overlapping SCs) and then per component
  signature across pages. Candidates are triaged one by one, like the other finding skills; the
  validator's `--candidates` check makes sure none is missed.
- **Probes** as built: keyboard traversal with trap detection (no new element for 15 presses,
  then Shift+Tab and Escape) and DevTools-protocol listener checks for clickable elements that
  aren't focusable; focus visibility by comparing screenshots of each element focused and after
  focus moves on (no blur, which would disturb the Tab sequence); obstruction by sampling nine
  points of the focused element; reflow; text spacing (only text clipped more than before);
  200% zoom as half the viewport at device scale 2; target size with the 24 px circle spacing
  test; hover content; motion between 5.0 s and 7.5 s after load (2.2.2 is about movement that
  lasts more than five seconds), also with reduced motion; accessibility-tree snapshots with
  Playwright's `aria_snapshot`; language by stop-word counts; and a media inventory. Pixel
  comparison uses a stdlib PNG decoder.
- **Evidence completeness** is recomputed from a `checks` matrix in `run.json` (page x viewport x
  required engine or applicable probe). The validator's `--run` check compares the report's tool
  statuses, check matrix, sample and browser version with `run.json`.
- **Not built in v1:** Section 508 mapping (as planned), a crawler that renders SPAs before
  collecting links (the crawl reads links after load and settle), journey-file input for
  critical paths (steps are given in the config), and EARL/JSON-LD export.
- **Found on a real static site, fixed** (an audit of a plant-guide site of many same-template
  pages):
  - Listed targets that shared a template with another target were dropped from the sample, and
    `random_sample: 0` still added a page. Listed targets and critical-path steps are now always
    audited (template de-duplication thins only crawled and sitemap pages), `0` adds no random
    pages, and each page records how it entered the sample: `critical_path`, `listed`,
    `structured`, `random` or `unreachable` (`listed` is new in the schema).
  - `text_spacing` and `resize_text` reported `.sr-only` text as clipped. Visually hidden
    elements (clip or clip-path to nothing, or 1 x 1 px with overflow hidden, on the element or
    an ancestor) are excluded.
  - Submenus that only `:hover` opens were invisible to the keyboard probe: hidden links never
    counted as unreached. The probe now finds links and buttons hidden inside a visible menu item,
    hovers the item, and for those it reveals tries the keyboard on the item's control (focus, for
    `:focus-within`; ArrowDown; Enter and Space unless the control follows a link or submits a
    form). Items Tab never reached and no key revealed become `keyboard.hover_only` (2.1.1):
    `failed` when every key was tried, `cantTell` when Enter wasn't, since the linked page may
    offer the same links.
  - IBM `img_alt_redundant` was mapped to 1.1.1 (IBM files it under that checkpoint), so merging
    gave it the unnamed-functional-image pattern and Critical. It is a best practice, like axe's
    `image-redundant-alt`. `rule-map.json` gained an `overrides` table, each entry with its
    reason, which `build_rule_map.py` re-applies after reading the engines' metadata (regenerating
    from the pinned engines reproduces the file byte for byte).
  - `validate_audit.py --write-summary` checked the schema first, so it rejected the empty
    headline numbers it exists to fill. It now fills them first; the verdict and its text are
    still the author's and still checked.
  - A faint page-level gradient made axe and HTMLCS return about 135 contrast "needs review"
    candidates that buried the real failures. The runner now records what lies behind each such
    element (background image and the element carrying it, translucent layers, text colour and
    size); `merge_findings.py` estimates the worst-case ratio against the gradient's colour stops
    (a gradient interpolates each channel linearly, so its luminance range is spanned by its
    stops) and collapses candidates with the same cause and background into one grouped
    candidate with counts, members, text colours and sample selectors. Candidates estimated
    below the threshold stay separate. Every item is still in exactly one candidate, so the
    triage check still accounts for all of them. Runs recorded before this (like the example)
    have no background record and are not grouped.
  - Third-party requests (GETs included) and service workers are blocked by default
    (`third_party`, `allowed_hosts`, `service_workers`), so an audit never fires analytics or ad
    tags.
- **Example:** `examples/harbour-dental/` is a static site with planted barriers;
  `examples/run/` is a real run (Chromium 156, Playwright 1.64.1). The Nu HTML Checker did not
  run: downloading and executing its jar was not permitted in the environment that produced the
  example, and the report records that.
- **Found on a Next.js site behind a strict CSP, fixed (v1.1.0):**
  - *Text spacing passed silently*: the probe's stylesheet was refused by a hashed `style-src`, no
    exception was raised, and 1.4.12 looked checked. Contexts now use Playwright's `bypass_csp`
    (`"bypass_csp": true` by default, recorded in `run.json`); the override sets a marker custom
    property that is read back, and a refused style makes the probe `errored`. probes.js is checked
    after injection too, and a page whose CSP refuses it still gets sampled, with every engine and
    probe `errored` and the CSP message as the reason. The validator rejects an SC passed on probe
    evidence when that probe didn't run on every planned check. Checked on a loopback fixture: with
    `bypass_csp` false and a nonce `script-src`, all engines and probes errored; with only a hashed
    `style-src`, the engines ran and text spacing errored; with the default, all ran.
  - *Contrast estimates misread CSS Color 4*: `lab(75.08 -60.73 19.41)` (#00d492, about 1.9:1 on
    white) became RGB (75, 60, 19) and a 10.2:1 "not below" estimate. `merge_findings.py` now
    converts hex, `rgb()` (both syntaxes), `hsl()`, `hwb()`, `lab()`, `lch()`, `oklab()`, `oklch()`
    and `color()` (srgb, srgb-linear, display-p3, xyz) to sRGB with the CSS Color 4 formulas,
    handles `none`, negative numbers and alpha, reads every colour function in a gradient, and gives
    no estimate for anything it can't parse. `--self-test` checks the conversions against known
    ratios (#00d492 on white 1.94:1 in all of its notations). The browser side reads alpha in any
    syntax.
  - *Text over images and video* was estimated against the section colour. The runner records an
    `<img>`, `<video>`, `<canvas>` or `<svg>` painted behind the text (`media_behind`); its estimate
    then has no ratio (`unknown_background`), and such candidates are never grouped. The validator
    rejects a dismissed contrast candidate below the threshold, over media, or grouped without an
    estimate.
  - *The keyboard probe clicked (1, 1)* to start, which landed inside a fixed header past the skip
    link (and could activate a control). It now focuses an invisible starting point inserted at the
    start of the body and removes it after the first Tab; nothing is clicked. On the fixture with a
    fixed header the skip link is the first stop.
  - *Target size* compared fixed-header links with in-flow elements at scrolled coordinates and
    counted a `tabindex="-1"` skip-link target and a visually hidden skip link as targets (15 false
    2.5.8 failures). It now measures at the top of the page, counts pointer targets only, skips
    visually hidden ones, and never compares a fixed or sticky target with an in-flow one.
  - *The motion probe reloaded the audited page*, bringing a dismissed consent banner back for the
    probes after it. It now watches fresh loads in their own pages and runs last.
  - *Overlays*: `consent` takes `dismiss` or `steps`; engines run with the overlay and again after
    dismissal (state `consent_dismissed`, `engines`: both, before or after), and the keyboard probe
    also runs with it open (state `consent_open`, `probes_with_overlay`). `run.json` records which.
  - *Reflow at 320 px* produced no candidate when the page clipped overflow instead of scrolling.
    Content past the viewport on a page that can't scroll is now `reflow.clipped` (failed), and
    content cut off by an ancestor with overflow hidden is recorded and raised as needs review;
    elements parked off-screen to the left (skip links) and visually hidden ones are ignored.
  - `--help` prints the whole config reference, a plan-only run records engines as not run "plan
    only", `run.json` counts requests sent, and the renderer counts audited pages, not unreachable
    ones. `schema_version` (`"1.0"`, unchanged) is documented.
  - The example run predates these probe changes; its items and candidates regenerate unchanged
    with the new `normalize.py` and `merge_findings.py`, and the report validates as before.
- **Found on a docs site served by a dev server, fixed (v1.2.0):**
  - *Every load waited the full network-idle timeout*: the dev server held a long-poll open, so
    Playwright's `networkidle` never came and a 12-page plan took 21 minutes. `settle()` now counts
    the page's requests itself and ignores event streams, websockets, long-poll and hot-reload
    endpoints and any request open longer than `long_lived_seconds` (4); the whole settle (network,
    then a quiet DOM) is capped by `settle_seconds` (10). `events` names the long-lived connections
    and any cap reached, so the report can say what was not waited for.
  - *The crawl loaded `max_pages x 4` pages* with no output, in plan-only mode too. It prints each
    load, takes `crawl.max_loads`, and stops once `max_pages` templates are found or `max_pages`
    loads in a row add no new template (listed targets and critical-path steps are always loaded
    first). `run.json` gains `crawl` (loads, limit, templates, why it stopped) and `settle`.
  - *Every docs page was its own template*: code blocks are `region` landmarks and the key took the
    first six children of `main` literally. The key now drops content landmarks (region, labelled
    section, form, dialog) and repeat counts.
  - *A linked text file was sampled as unreachable*: non-HTML responses are skipped and listed
    (`crawl.skipped_non_html`); links to text, data, media and asset files are not queued.
  - *Probe false positives*: hovering a sidebar item changed only its own box, and since `:hover`
    still held after Escape it was "not dismissible" (31 candidates). Elements that appear inside
    the hovered element's own box are no longer content on hover (counted in `own_box_changes`;
    `normalize.py` applies the same rule to older runs from the recorded rectangles). Clickable
    heading text whose heading holds a focusable `#` anchor link is not reported as unreachable,
    and one with a focusable sibling control is needs review. A fixed header that contains the
    focused control no longer "obscures" it (also applied to older runs from the recorded covering
    element). Focus guards (`data-*focus-guard`, 0 x 0 `aria-hidden` spans) are not measured for a
    focus indicator. In a named state with a modal open and focus inside (a search dialog opened
    with a shortcut), the keyboard probe starts from the focused element and expects only the
    modal's controls (`start: active_element`), instead of tabbing through the page behind it.
  - *Repeated components split into one candidate per instance* because their ids were generated
    (React `_R_1p5x_`, Base UI) or per-page paths. Component signatures drop such instance ids
    (probes.js for new runs, `merge_findings.py` for any run). Re-merging the reported run with the
    new `normalize.py` and `merge_findings.py` (no re-capture) gave 124 candidates instead of 215.
  - *Validator leniency*: an SC passed or failed with `agent_review` in its basis must be in
    `assisted_review.scs_reviewed`; a failed SC is never more confident than its most confident
    finding; a `failed` candidate kept as needs review needs a reason beyond the needs-review text
    or the tool message. 4.1.1 (obsolete in 2.2) candidates are advisories of the new kind
    `obsolete` (schema 1.1; 1.0 reports still validate), and an advisory's kind must match an
    obsolete or outside-version candidate's kind.
  - Not done: grouped triage (one entry for many candidates by rule and match). The id and
    own-box fixes remove most of the repetition that made it necessary; the schema still takes
    one triage entry per candidate. The example run predates these changes and was not re-captured
    (the release gate and orchestrator examples hash the report); its items and candidates
    regenerate unchanged and the report (schema 1.0) validates as before.
- **Found on a generated HTML report of a Go CLI, fixed (v1.3.0)** (one `report.html` per run folder,
  a `100vh` app shell with scrolling panels, step rows with script-generated ids):
  - One click-only row pattern became 18 candidates, one per step, because the rows carry ids like
    `flow-3-cmd-12-d2`. `merge_findings.py` now drops ids with a numeric segment from the component
    signature (`--self-test` checks it).
  - `run.json` records `blocked_requests` as a list but the schema wanted a count. Schema 1.2 takes
    either, and `--run` checks the number against `run.json`.
  - Full-page screenshots of a `100vh` shell show only the first box of each scrolling panel, and
    nothing said so. The runner records `inner_scroll` per page and a `limitations` entry in `run.json`
    (also printed as a warning) for the report to carry.
  - Five copies of one generated page in five folders, with no landmarks, made five templates. Pages
    without page-level landmarks are keyed by file name and DOM shape, not folder.
  - An agent finding that rested on reading the page's script had no checkable basis. Findings may
    carry `basis: [{file, line, excerpt}]`, re-read with `--repo`; an `agent_judgment` finding that
    cites `file:line` without a basis is rejected.
  - `scripts/self_test.py` runs these checks (the scroll check needs Playwright). The example run was
    not re-captured: its candidates regenerate unchanged and the report validates as before.
  - Not done: a probe for single-key shortcuts (2.1.4), pressing Enter/Space on the selected item, and
    an event for short-interval polling; those still need the agent to read the script.
- **Found on a server-rendered Python site with a navigation menu of a few hundred links, fixed (v1.4.0)**:
  - The keyboard probe's press budget (3 per visible focusable element) ignored the links a
    `:focus-within` menu adds to the Tab order once focus enters it, so the traversal ended inside the
    menu and reported the rest of the page as unreachable. The budget now grows by 20 presses each time
    a press reaches a new element, up to `keyboard.max_presses` (2000).
  - A traversal that still stops at its limit was counted as `ran`. It is now `partial` with a reason
    (schema 1.3 adds the check status): normalize.py keeps its stops but not its unreached list,
    completeness counts it as not run, confidence can't be high, probe-based passes are refused, and
    the validator wants it named in `limitations`.
  - The crawl followed a menu of one URL shape in document order and spent every load on it. It now
    loads at most `crawl.per_pattern` (2) pages of a shape while links of an unseen shape wait.
  - `self_test.py` covers all three (the keyboard check drives Chromium on a synthetic page). The
    example run was not re-captured; its items regenerate unchanged and the report validates.
  - Not done here: speculative connections (`preconnect`, `dns-prefetch`) that request interception
    can't see need a host-resolver guard in the shared browser harness; listed error pages still
    block probe-based passes.
