# WCAG Auditor rubric

Anchors for outcomes, severity, confidence, priority, triage and the verdict. `SKILL.md` says
when to apply them. `data/wcag-sc.json` is the machine-readable part: every SC from WCAG 2.0 to
2.2 with its level, version, whether it can be automated, which probes cover it, its failure
patterns with default severities, and a manual check.

WCAG defines conformance levels, not severity. The severities here are this skill's published
rubric layered on top of WCAG, and every report says so.

## Outcomes (W3C EARL)

| Outcome | Means | Needs |
|---|---|---|
| `failed` | At least one finding fails the SC on the sampled pages | The findings' ids |
| `passed` | The whole SC was checked on every sampled page and nothing failed | Probe, agent-review or manual evidence; tool passes alone never count. `automatable: none` SCs need agent-review or manual evidence |
| `cantTell` | Evidence exists but needs a person to decide | Needs-review items saying what to check |
| `inapplicable` | The sampled pages have no content the SC applies to | Why (no media, no authentication, obsolete in the version) |
| `untested` | No evidence either way | An entry in the manual test plan |

`automatable` in `data/wcag-sc.json`: `full` (probes and engines can decide it for the sampled
states; only 3.1.1), `partial` (automation finds some failures but never proves a pass), `none`
(judgment only).

## Severity

| Severity | Definition |
|---|---|
| **Critical** | A user group can't perceive, operate or complete essential content or a task. Always for a failure of a non-interference SC (1.4.2, 2.1.2, 2.2.2, 2.3.1), because conformance requirement 5 says those block use of the whole page |
| **Serious** | Severely degrades use; only a hard workaround exists |
| **Moderate** | Causes friction; a reasonable workaround exists |
| **Minor** | Low practical impact |

1. **Start at the failure pattern's default** (`failure_patterns[].default_severity`). Patterns
   describe how an SC fails, not the SC itself: an unnamed functional image is Critical, alt text
   that isn't equivalent is Moderate, both under 1.1.1.
2. **Critical path: one level up** when the finding is on a critical-path step and affects what
   the step needs (the booking form's fields, not the site logo on that page). Conformance
   requirement 3 (complete processes) means one failed step fails the whole process.
3. **Non-interference: Critical**, whatever the pattern says.
4. **Deviate only with a reason.** `severity_override_reason` explains why the default doesn't fit
   (a duplicate route exists, only one browser is affected). Tool impact labels (axe `critical`,
   IBM `VIOLATION`) are inputs, never the rating.

## Confidence

| Confidence | Needs | Example |
|---|---|---|
| `high` | Two independent failing sources agree (two engines, or an engine and a probe), or the failing rule implements an ACT rule | axe image-alt (ACT 23a2a8) and IBM img_alt_valid on the same image |
| `medium` | One heuristic engine rule, or one probe on its own; or agent judgment corroborated by a tool's needs-review result | The keyboard probe finds a trap |
| `low` | Agent judgment alone | Three "Read more" links with no programmatic context |

`agent_judgment` is never `high`.

## Priority

`score = severity weight (critical 4, serious 3, moderate 2, minor 1) x reach x leverage`

- **Reach**: 3 when the finding is on at least two pages and at least half the sampled pages; 2
  when on two or more pages; 1 for one page.
- **Leverage**: 2 when one fix covers two or more instances (a shared component); otherwise 1.

| Band | Score | Floors and caps |
|---|---|---|
| **P0** | >= 24 | Also any finding with `blocks_critical_path` |
| **P1** | >= 12 | Critical findings are at least P1; agent-judgment findings are at most P1 |
| **P2** | >= 6 | Serious findings are at least P2 |
| **P3** | < 6 | |

Rank by band, then score, then severity, then confidence.

Examples: a critical component on every page (4 x 3 x 2 = 24) is P0; a serious site-wide component
(18) is P1; a keyboard trap on one booking step (4, floor P1) is P0 because it blocks the path; a
serious issue on one page (3) is P2 by the floor; a minor pair of small buttons (2) is P3.

## Verdict

| Verdict | When |
|---|---|
| `does_not_conform` | At least one finding with `high` confidence fails an A or AA SC |
| `not_established` | Anything else, including no findings at all |

There is no `conforms`. Verdict text must not claim conformance, compliance or accessibility;
with `not_established` it must say conformance is not established.

## Evidence completeness and overall confidence

`evidence_completeness` = checks that ran / checks planned, over `run.json` `checks` (each
sampled page x viewport x required engine and applicable probe). Blocked pages and missing
engines count as planned and not ran.

`overall_confidence`: `high` when completeness >= 0.9, all three required engines ran and an
assisted review was done; `medium` when completeness >= 0.6; otherwise `low`.

## Triage

**Promote** to a finding when the failure is real on the page: the screenshot, accessibility tree
or probe data confirms what the tool says. Merge candidates with one root cause into one finding.

**Needs review** when a person has to look or listen (captions, a control the probes couldn't
reach). Say exactly what to check.

**Advisory** for best practice, AAA when the target is AA, or SCs outside the target version.

**Dismiss** with a reason someone can check:

- a computed contrast ratio with its colours and where they come from (`#1d2b33 on #ffffff:
  14.5:1, site.css .cookie`), for engine "needs review" results on positioned or overlapping text;
- for a grouped contrast candidate (same cause, same background), the lowest `contrast_estimate`
  of the group with the text colours and gradient stops it came from, after checking a sample in
  the screenshot; a group whose estimate is below the threshold, or that has no estimate, is
  needs review, not dismissed;
- that the text is visually hidden (`.sr-only`): it is never seen, so 1.4.3 doesn't apply to it;
- a probe measurement that contradicts a potential issue (`focus probe: 1248 pixels changed`);
- an exemption that applies (video in 1.4.10; an inline link in 2.5.8);
- why the flagged words aren't instructions ("above the pharmacy" is a street location);
- the finding that already covers it.

"False positive" on its own is rejected.

## Safe testing

- Loopback targets need no confirmation; anything else needs `authorised_by`.
- `allow_form_submission: false` aborts every non-GET request and logs it in `blocked_requests`.
  Client-side validation can still be exercised with a state that clicks submit.
- Probes press Tab, Shift+Tab and Escape and hover. The hover-menu check also presses ArrowDown
  on a menu's control, and Enter and Space only when that control neither follows a link nor
  submits a form; if the page navigates anyway, it is reloaded and the event recorded.
- Third-party requests and service workers are blocked by default (`third_party`,
  `allowed_hosts`, `service_workers`).
- Login recipes read credentials from environment variables; the storage state stays in memory.
- CAPTCHAs are never bypassed.

## Engines

| Engine | Version | Licence | Result mapping |
|---|---|---|---|
| axe-core | 4.14.0 | MPL-2.0 | `violations` -> failed, `incomplete` -> cantTell; SCs from `wcagNNN` tags; ACT ids from rule metadata; `best-practice` -> advisory |
| IBM Equal Access | 4.0.34 | Apache-2.0 | `VIOLATION/FAIL` -> failed, `POTENTIAL`/`MANUAL` -> cantTell, `RECOMMENDATION` -> advisory; SCs from the WCAG rulesets; ACT ids from the rule help |
| HTML_CodeSniffer | 2.5.1 | BSD-3-Clause | errors -> failed, warnings -> cantTell, notices counted per SC (manual prompts); SC parsed from the message code. It predates WCAG 2.2 |
| Nu HTML Checker | 26.10.12 | MIT | Optional, needs Java; local only. Duplicate IDs and ARIA errors as supporting cantTell evidence |

Lighthouse (an axe wrapper) and Pa11y (axe and HTMLCS) are left out because they would only
duplicate these results.

## Reviewer checklist

- [ ] Authorisation recorded; sample covers every template and critical-path step.
- [ ] Every engine and probe is ran, not_run or errored with a reason; missing ones in limitations.
- [ ] Every candidate triaged with checkable reasons; cantTell never counted as failed.
- [ ] Each finding: SC, failure pattern, severity with rationale, confidence, sources, instances
      with screenshots, who is affected, fix and a verification step.
- [ ] Every target SC has an outcome; no tool-only passes; untested SCs in the manual test plan.
- [ ] The verdict follows the rubric and the text claims no conformance.
