# WCAG Auditor Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It audits live web pages against WCAG (2.2 AA by default) without needing the source code. Three
open-source engines run inside a real browser, browser probes test what engines can't, the results
are merged per component and mapped to success criteria, and every SC at the target level gets an
outcome. What nobody could test becomes a manual test plan.

It doesn't bluff about the result:

- The verdict is `does_not_conform` or `not_established`. Never "conforms": automated and assisted
  testing can't establish conformance.
- Outcomes use the W3C EARL terms. An SC with no evidence is `untested`, and a passing tool rule
  never passes an SC on its own.
- Engine "needs review" results stay `cantTell`: they're never counted as failures and never
  dropped. Best-practice rules and AAA criteria (when the target is AA) are advisories.
- Every candidate from every engine and probe is promoted, sent for review, made an advisory, or
  dismissed with a reason you can check (a computed contrast ratio, a probe measurement).
- Severity is a published rubric layered on WCAG, starting from the failure pattern's default,
  raised on critical paths, and always Critical for the non-interference criteria.
- Agent judgment (alt-text quality, colour-only errors) is labelled as such and never rated high
  confidence.
- Every engine and probe is recorded as ran, not run or errored, and the validator checks the
  report against the run's own records. Pages run with their CSP bypassed so the engines and probes
  can be injected, every injection is checked, and a probe that couldn't apply its overrides is
  errored, never a silent pass.
- Contrast estimates understand modern CSS colours (`oklch()`, `lab()`, `color()`), give no ratio
  for text over images or video, and are self-tested against known ratios.
- An audit sends nothing to third parties: requests to other hosts are blocked unless you allow
  them, so analytics and ad tags never fire.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g.
`.claude/skills/wcag-auditor/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/wcag-auditor/`, or include
`SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/wcag-auditor/`, and reference
`SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version.

### Requirements

The browser runner needs Playwright for Python, the skill's only third-party dependency:

```bash
pip install -r requirements.txt
python -m playwright install chromium
python scripts/fetch_engines.py          # axe-core, IBM Equal Access and HTML_CodeSniffer, hash-verified
```

`fetch_engines.py` downloads the npm tarballs pinned in `data/engines.lock.json`, checks the
registry's SHA-512 integrity, reads out the one browser script it needs and checks its SHA-256.
Files are cached in `~/.cache/qa-skills/wcag-engines` (or `--cache`, or `WCAG_ENGINE_CACHE`), and
`--from-dir` installs from tarballs you downloaded yourself. The Nu HTML Checker is optional
(`--include-optional`, needs Java) and runs locally; pages are never sent to the hosted W3C
service. Everything after the browser run (normalising, merging, validating, rendering) is
stdlib-only Python 3.9+. PDF accessibility is out of scope; use veraPDF for that.

## Usage

Ask the agent:

> Audit https://staging.example.com against WCAG 2.2 AA with the WCAG Auditor skill. I own the
> site. The booking flow is the critical path.

or

> Use WCAG Auditor on http://localhost:3000 at Level AA, crawl up to 15 pages, and map the
> results to EN 301 549.

Scripts, in the order the skill runs them:

```bash
python scripts/fetch_engines.py [--include-optional] [--check]
python scripts/run_audit.py --config audit_config.json --out <run> [--plan-only]
python scripts/normalize.py <run> --out <run>/items.json
python scripts/merge_findings.py <run>/items.json --run <run> --out <run>/candidates.json
python scripts/validate_audit.py audit.json --run <run> --candidates <run>/candidates.json --write-summary
python scripts/render_report.py audit.json [--brief]
```

| Priority | Meaning |
|---|---|
| **P0** | Score >= 24 (a critical component on most pages), or a finding that blocks a critical path |
| **P1** | Score >= 12; every critical finding is at least P1 |
| P2 | Score >= 6; every serious finding is at least P2 |
| P3 | Score < 6 |

Score = severity (critical 4, serious 3, moderate 2, minor 1) x reach (share of sampled pages) x
leverage (one fix covers several instances).

## Output

`audit.json`, described by `schemas/audit.schema.json` (`schema_version` `"1.0"`):

| Section | Content |
|---|---|
| `scope` | Version, level, targets, exclusions, who authorised the test, critical paths |
| `metadata` | Browser, engines with versions and hashes, sample with reasons, check matrix, blocked requests |
| `headline` | Verdict, severity counts, SC coverage, evidence completeness, overall confidence, blocked critical paths |
| `sc_results` | Every SC at the target level with its EARL outcome, basis and evidence |
| `findings` | Ranked: SCs, failure pattern, severity and why, confidence, priority, sources with ACT ids, component, instances with screenshots, who is affected, fix, how to verify |
| `needs_review`, `advisories` | cantTell items with what to check; best practice and above-level issues |
| `triage` | Every candidate and what happened to it |
| `manual_test_plan` | Concrete steps for every untested SC |
| `assisted_review`, `limitations` | What was judged by the agent; what wasn't covered |

The run directory holds `run.json`, the raw engine and probe results, screenshots and
accessibility-tree snapshots, so every finding can be traced back to what the tools saw.

## Example

`examples/harbour-dental/site/` is a small dental-clinic site with deliberate barriers, and
`examples/run/` is a real run against it (Chromium 156, axe-core 4.14.0, IBM Equal Access 4.0.34,
HTML_CodeSniffer 2.5.1; the Nu HTML Checker didn't run). `examples/audit.example.json` is the
triaged report:

- 21 findings, 2 of them P0: a keyboard trap in the booking form's day picker that stops keyboard
  users booking, and a mobile menu button that can't be reached by keyboard on any page;
- 68 candidates triaged: 25 promoted into findings, 2 sent for review, 11 made advisories and 30
  dismissed with computed contrast ratios, probe measurements or the exemption that applies;
- 56 WCAG 2.2 AA criteria: 21 failed, 4 can't tell, 4 passed on agent review, 6 inapplicable and
  21 untested, each with a manual test step.

```bash
python scripts/validate_audit.py examples/audit.example.json --run examples/run --candidates examples/run/candidates.json
python scripts/render_report.py examples/audit.example.json --brief
```

To re-run it, serve the site on loopback and run the audit (needs the requirements above):

```bash
python -m http.server 8137 --bind 127.0.0.1 --directory examples/harbour-dental/site
python scripts/run_audit.py --config examples/harbour-dental/audit_config.json --out <run>
```

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: outcomes, severity, confidence, priority, verdict, triage and engine mappings
- `requirements.txt`: Playwright for Python, for `run_audit.py` only
- `data/wcag-sc.json`: all 87 SCs of WCAG 2.0 to 2.2 with level, version, automation, probes,
  failure patterns with default severities and a manual check
- `data/rule-map.json`: axe, IBM and HTMLCS rules and probe codes to SCs and ACT rules, with an
  `overrides` table (each entry with its reason) that the generator re-applies
- `data/en301549-map.json`: SC to EN 301 549 V3.2.1 clause
- `data/engines.lock.json`: pinned engine versions, tarball URLs, integrity and SHA-256
- `schemas/audit.schema.json`: output contract
- `scripts/fetch_engines.py`: download, verify and cache the engines
- `scripts/run_audit.py`, `scripts/probes.js`: the Playwright runner and its in-page probes
- `scripts/browser_harness.py`: the shared browser harness; run_audit.py uses its session loader, and
  `python scripts/browser_harness.py capture-session --url <app> --out <file outside the repo>` saves a session a
  person signs in to by hand, for `auth: {"storage_state": file}`
- `scripts/normalize.py`, `scripts/merge_findings.py`: map results to SCs, merge into candidates
- `scripts/validate_audit.py`: structure and honesty checks
- `scripts/render_report.py`: coloured terminal/Markdown report
- `scripts/build_rule_map.py`: regenerates `data/rule-map.json` from the engines' metadata (maintainers, when an engine version changes)
- `examples/`: sample site, real run and triaged report

## Design principles

1. Automated testing finds failures; it never proves conformance.
2. No evidence means `untested`, and "needs review" means a person must look.
3. One barrier is one finding, however many tools and pages report it.
4. Severity is about people: who can't do what, and whether they can finish the task.
5. Account for every candidate, every engine and every SC.
