# Privacy Flow Auditor Skill

A portable skill for Claude Code, OpenAI Codex, Gemini CLI, and other coding agents.

It maps where personal data goes in a web app, with evidence for every flow:

- **Collected**: form fields, server-side reads, device APIs and mobile permissions, each classified
  into a specific category (contact, health, government identifier, ...); identity from an identity SDK
  (MSAL, OIDC, Auth0 tokens and claims); file uploads, whose contents the report asks about.
- **Sent**: every request in three consent states (before a choice, after "Reject all", after "Accept
  all"), searched for synthetic canary values raw, URL-encoded, Base64 and hashed (SHA-256, SHA-1, MD5),
  so a "hashed email" sent to an ad platform shows up.
- **Kept and logged**: browser storage (including an identity SDK's token cache), cookies, the server log
  of a local run, and logging calls in the code.

An app with no consent banner is captured in a single `no_banner` state (`"consent": {"banner": null}`),
and an app behind sign-in is captured from a storage-state file a person saves after signing in with a
test account (`"storage_state"` in `flows.json`; kept out of the run folder and never recorded).
- **Who**: each recipient as first party, a declared processor or an undeclared third party, named only
  from a public or curated list.

The result is a data inventory, the recipients, a consent matrix and ranked findings - tracking before
consent or after rejection, personal data to third parties, unmasked session replay, health data in logs,
data in URLs, insecure storage, undeclared processors - each with GDPR, UK GDPR, ePrivacy/PECR and
Australian Privacy Principles references for a privacy professional.

It never types or stores real personal data (every captured value is redacted), never lets a test request
reach a real vendor (third parties are blocked in the browser and recorded as attempts), and never says an
app is compliant.

## Install

### Claude Code
Copy this directory into a project or user-level skills directory, e.g. `.claude/skills/privacy-flow-auditor/`.

### OpenAI Codex
Place this directory in the repository, for example `.agents/skills/privacy-flow-auditor/`, or include
`SKILL.md` in the task context.

### Gemini CLI
Place the directory in the repository, for example `.gemini/skills/privacy-flow-auditor/`, and reference
`SKILL.md` from the project's Gemini instructions.

Exact skill discovery paths can vary by client version. The scripts need Python 3.9+; the live capture
needs Playwright (`pip install -r requirements.txt` and `python -m playwright install chromium`).

## Usage

> Audit where our sign-up form's data goes. Staging is https://staging.example.com and I can authorise it; our processors are listed in section 7 of the privacy policy.

```bash
python scripts/fetch_lists.py
python scripts/match_canaries.py new --out run/canaries.json
python scripts/scan_privacy.py . --out run/static.json
python scripts/capture_flows.py flows.json --canaries run/canaries.json --out run/capture.json --screenshots run/screenshots
python scripts/merge_candidates.py --capture run/capture.json --static run/static.json --flows flows.json --lists ~/.cache/qa-skills/privacy-lists/domains.json --declared declared.json --out run/candidates.json
python scripts/validate_privacy_audit.py run/privacy_audit.json --candidates run/candidates.json --capture run/capture.json --canaries run/canaries.json --evidence run --repo . --recompute
python scripts/render_report.py run/privacy_audit.json [--brief]
```

When the browser capture can't run, the audit is still delivered from the static scan alone, marked
`run_status: not_run`, with no consent state claimed:

```bash
python scripts/merge_candidates.py --static run/static.json --run-status-reason "why the capture couldn't run" --out run/candidates.json
python scripts/validate_privacy_audit.py run/privacy_audit.json --candidates run/candidates.json --repo . --recompute
```

## Example

`examples/clinic-signup/` is a small patient sign-up site with stub vendors on made-up `.test` hostnames
and planted problems; `examples/flows.json` the flows; `examples/lists.example.json` the vendor list for
the made-up vendors; `examples/run/` the real run (listed in `examples/run/commands.sh`) with every value
redacted; `examples/privacy_audit.example.json` the audit; `examples/static-only/` and
`examples/privacy_audit.static-only.example.json` the same app audited from the code alone.
`examples/README.md` has the answer key.

```bash
python scripts/validate_privacy_audit.py examples/privacy_audit.static-only.example.json --candidates examples/static-only/candidates.json --repo examples/clinic-signup
python scripts/validate_privacy_audit.py examples/privacy_audit.example.json --candidates examples/run/candidates.json --capture examples/run/capture.json --canaries examples/run/canaries.json --evidence examples/run --repo examples/clinic-signup
python scripts/render_report.py examples/privacy_audit.example.json
```

## Files

- `SKILL.md`: complete agent instructions
- `RUBRIC.md`: sensitivity, flow, reach, priority, floors, confidence, triage outcomes
- `schemas/privacy_audit.schema.json`: output contract
- `data/categories.json`: categories, sensitivity and field patterns
- `data/sinks.json`: logging, analytics, replay, error-tracker, storage and HTTP sinks; SDK packages
- `data/provisions.json`: finding type to GDPR, UK GDPR, ePrivacy, PECR and APP references
- `data/vendors.json`: curated domain-to-vendor names, plus vendor aliases and parent companies for matching declared processors
- `data/lists.lock.json`: EasyPrivacy and the Open Cookie Database pinned to git commits, with hashes (downloaded, not committed), and the bundled suffix list's hash
- `data/public-suffix-private.json`: shared-hosting suffixes from the Public Suffix List (vercel.app, netlify.app, ...), so each app on them is its own domain
- `scripts/domains.py`: registrable domains with those suffixes, and first-party matching as given
- `scripts/fetch_lists.py`: download, verify and combine the lists; `--check` verifies the cache, `--update --write-lock` re-pins into a scratch lock
- `scripts/match_canaries.py`: canary sets, encodings, search and redaction
- `scripts/scan_privacy.py`: static collection points, sinks, candidate flows and SDK hosts (including scripts injected from JavaScript), Go log and HTTP sinks, command-line flags and `telemetry` call-home candidates; records `language_coverage` and warns when most of the code is in a language it does not read; lists what it skipped
- `scripts/lang_census.py`: shared copy (edit it in `shared/`); source files per language with vendored and embedded trees left out
- `scripts/self_test.py`: synthetic checks of the Go scan, the language warning, template loaders before consent, the page URL sent to analytics, list rules narrower than a domain, undeclared processors without a canary, and the validator's telemetry, declared-processor, inventory, headline and runtime-evidence rules
- `scripts/capture_flows.py`: the browser capture in each consent state (before, reject, accept; optionally partial and dismiss)
- `scripts/merge_candidates.py`: candidates, recipients and the consent matrix
- `scripts/validate_privacy_audit.py`, `scripts/render_report.py`
- `scripts/browser_harness.py`, `scripts/sarif.py`: shared copies (edit them in `shared/`)
- `examples/`: the app, the flows, the run and the audit

## Design principles

1. Synthetic canaries in, redacted evidence out.
2. Test consent in all three states; tracking after rejection matters most.
3. Name vendors from lists, categories from a fixed set, and flows from evidence.
4. References for a professional, never a compliance verdict.
