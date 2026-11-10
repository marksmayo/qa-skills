# Design: `wcag-auditor-skill`

Status: Draft. Build order: 6.

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
