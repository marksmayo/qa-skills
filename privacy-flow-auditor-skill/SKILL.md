---
name: privacy-flow-auditor
description: Map where personal data goes in a web app - what it collects, where it stores it, what it logs, and which third parties receive it, and whether that happens before the user consents or after they reject. Combines a static scan of the code (collection points, logging, analytics, session replay, storage, SDKs, mobile permissions) with a live browser capture in three consent states (before a choice, after "Reject all", after "Accept all"), typing synthetic canary values and searching every request, cookie and storage entry for them raw, URL-encoded, Base64 and hashed. Produces a data inventory, recipients, a consent matrix and ranked findings with GDPR, UK GDPR and Australian Privacy Principles references for a privacy professional. Never types or stores real personal data, and never says an app is compliant.
version: 1.0.0
---

# Privacy flow auditor

## Mission

Show what the app itself does with personal data when everything works as designed: what it
collects, where it keeps it, what it writes to logs, and who else receives it - with evidence for
every flow. The code security auditor asks whether an attacker can get at data; this skill asks where
the app sends it on purpose, including to analytics, advertising, chat and session-replay vendors.

Findings carry references to GDPR, UK GDPR, the ePrivacy Directive and PECR, and the Australian
Privacy Principles, **for a privacy professional**. A qualified person decides whether there's a
breach; this is not legal advice.

## Non-negotiable rules

1. **Canary data, never real data.** The live capture types a fresh synthetic set (`match_canaries.py
   new`): an email under `example.invalid`, a reserved fictional phone number, a name, a date of birth
   and a health-like reason, each carrying the set id. Real personal data is never typed, captured or stored.
2. **No personal data in any output.** Every captured URL, body, cookie, storage value and log line is
   written redacted (`<canary:email:sha256>`, `<redacted:email>`). Field names, hosts and categories are
   kept. The validator scans the report and every evidence file for email addresses, phone and card
   numbers, unredacted canaries and secrets.
3. **Every flow has evidence:** a captured request (state, request number, redacted URL, which canary in
   which encoding) or a code path (file and line from collection to sink). An SDK that's present but never
   seen sending data is `present`, not a transfer.
4. **Consent is tested in three states**: before a choice, after "Reject all", after "Accept all".
   Tracking before a choice or after rejection is the most important finding. A banner the skill can't
   operate leaves that state `untested`, and the report says so.
5. **Categories are specific**, from `data/categories.json` ("personal data" isn't a category).
6. **Vendors are named only from a list or a code import**, never guessed from a hostname.
7. **References, not verdicts.** Never write that an app is compliant, GDPR-ready or meets the APPs;
   "no findings" means only that the pages, flows and states listed weren't seen sending data.
8. **Secrets** found along the way are reported by location and type only.
9. **Safe targets.** Loopback (and `*.test` mapped to it) needs nothing; other hosts need `authorised_by`.
   Form submission stays blocked unless allowed for a test environment.
10. **No request reaches a real vendor.** The browser harness blocks third parties by default: every request
   to a host that isn't loopback (including `*.test` and `host_map` hosts), the site or `allowed_hosts` is
   aborted in the browser before it leaves, GETs included. The page still *attempts* it, so the capture
   records it (`"blocked": "third_party"`) and it counts as observed tracking. But a blocked vendor never
   answers: its script doesn't run, so it sets no cookies and sends none of the hits that would follow
   (a page-view collect, ad requests, a consent message of its own). Findings about blocked vendors are
   therefore a floor, not the full picture; say so in the limitations, naming the blocked domains (the
   validator checks). `"third_party": "allow"` in `flows.json` lets requests out, only when the person
   running the audit agrees the vendors receive the test's hits (`third_party_authorised_by`).

## Inputs

| Input | Default | Notes |
|---|---|---|
| `repo` | optional | Source for the static scan; `--exclude` third-party or generated directories |
| `flows` | required for the live part | `flows.json`: `site`, `consent` selectors for accept and reject, flows with steps; `{"fill": sel, "canary": field}` types a canary; each flow has a `reach` (3 every user, 2 common, 1 rare) with its reason |
| `authorised_by`, `allow_form_submission`, `host_map` | as in `browser_harness.py` | |
| `regions` | `AU,EU` | Which provision sets to reference (`UK` adds UK GDPR and PECR) |
| `first_party_domains` | the site's registrable domain | In `flows.json` |
| `declared_processors` | optional | The organisation's own vendor list (privacy policy section, records of processing): `{"source", "processors": [{"vendor", "domains"?}]}`. A declared name covers a domain when it is the domain's vendor, one of the vendor's aliases or its parent company (`data/vendors.json` `vendors`: "Google Analytics" covers the gtag.js loader on googletagmanager.com, "Google Ads" covers AdSense), or when the entry lists the domain; each recipient records the declared name and how it matched |
| `lists` | `fetch_lists.py` output | EasyPrivacy (tracker domains), Open Cookie Database (cookie names), `data/vendors.json` (vendor names); `--extra` adds regions or corrections |
| `privacy_policy_url` | optional | Read it and compare vendors and categories with what was observed: agent judgment, at most `pattern` |

## Workflow

The capture needs Playwright with Chromium (`pip install -r requirements.txt`; ask first). The rest is stdlib.

```bash
python scripts/fetch_lists.py                       # download the lists at their pinned commits, verify -> ~/.cache/qa-skills/privacy-lists/domains.json
python scripts/fetch_lists.py --update --write-lock <run>/lists.lock.json   # newer lists: pin the latest commits in a scratch lock
python scripts/match_canaries.py new --out <run>/canaries.json
python scripts/scan_privacy.py <repo> --out <run>/static.json [--exclude vendor] [--semgrep semgrep.sarif]
python scripts/capture_flows.py flows.json --canaries <run>/canaries.json --out <run>/capture.json [--server-log app.log] --screenshots <run>/screenshots
python scripts/merge_candidates.py --capture <run>/capture.json --static <run>/static.json --flows flows.json --lists domains.json --declared declared.json --regions AU,EU --out <run>/candidates.json
```

1. **Recon.** Stack, logging libraries, analytics and SDK packages, consent platform, data stores.
2. **Static scan.** Collection points (form fields by name, label, `autocomplete` and type, in HTML and in
   JSX, including component-library fields such as MUI `TextField`, and form-library registrations;
   server-side reads; device APIs and permissions), sinks (`data/sinks.json`), and candidate flows where a
   sink line names a collected field or a simple alias of one. Trace each to a full path (collection to
   sink) before calling it `traced`. Check error trackers' `sendDefaultPii` and `beforeSend`, and replay
   masking: a replay SDK that records with no explicit privacy level (Datadog RUM without
   `defaultPrivacyLevel`) is a candidate, because the default depends on the SDK version. A persisted
   client cache (Apollo `persistCache`, React Query persisters, Redux persist, localforage or IndexedDB)
   holds every query result at rest: trace which personal data the queries return, and whether the cache
   is encrypted and cleared at sign-out. The scan finds no personal data by itself in an app whose screens
   show data from an API rather than forms: read what the queries return.
3. **Live capture.** For each consent state, a fresh context records requests, cookies and storage
   before any interaction, then after the banner choice, then through each flow typing canaries. With
   `--server-log` on a local run, log lines carrying a canary are recorded too. Requests to third parties
   are recorded and blocked (rule 10). When no consent control can be operated, the reject and accept
   states are `untested` with the reason: the headline then has `null` (not an empty list) for them and
   names them in `states_not_tested`, and the statement says they weren't tested, never "none".
4. **Merge.** Candidates per third-party domain and state, per canary flow, per storage key, per log, plus
   undeclared processors, cross-border transfers (when the list has regions) and unrecognised hashes.
5. **Triage** every candidate: `promoted` to a finding, `inventory` (an expected flow recorded in the
   inventory, such as a declared processor's region, for the privacy team), or `dismissed` with a code:
   `strictly_necessary` (first-party HttpOnly session cookie), `functional_no_identifier` (no cookie, no
   identifier, no canary, not an analytics, advertising or replay vendor), `constant_in_source` (a hash-like
   value written literally in the code), `pii_disabled` (error tracker with default PII off), `duplicate`,
   `false_match`, `not_personal_data`. Special categories and tracking after rejection are never inventory-only.
6. **Rate** with `RUBRIC.md`; write for each finding the evidence summary, a fix, and a verification step
   (usually: re-run the capture and show the canary no longer reaches that recipient). Build the data
   inventory per field: collected at, stored, logged, in URLs, recipients with encodings and states.
7. **Validate and render.**

```bash
python scripts/validate_privacy_audit.py <run>/privacy_audit.json --candidates <run>/candidates.json --capture <run>/capture.json --canaries <run>/canaries.json --evidence <run> --repo <repo> --recompute [--write-summary]
python scripts/render_report.py <run>/privacy_audit.json [--brief]
```

## Edge cases

- **Scripts injected from JavaScript** (`script.src = "https://..."`, `setAttribute('src', ...)` after
  `createElement('script')`, a vendor's loader URL in a string) are in the static SDK inventory with
  `source: injected_script` or `vendor_url`, so a site with no `<script src>` to a vendor still shows them.
- **Large repositories:** the scan reads each file once; files over `--max-bytes` (2 MB) and minified or
  generated files are listed under `skipped` (generated files are still searched for SDK hosts), as are
  dependency and build directories. Say in the limitations what was skipped if it could hold app code.
- **Server-side tagging** (a first-party path that forwards to vendors) looks first party; list known
  tagging paths as a limitation.
- **Salted hashes** can't be matched; an unrecognised 32/40/64-hex value in a third-party request is a
  `pattern` candidate, dismissed only when the value is shown to be a constant in the code.
- **Single-page apps** send analytics on route changes; the capture waits for network idle after each step.
- **Children's services:** set `children: true` on findings; every category's sensitivity goes up one level.
- **Native apps:** the static scan reads manifests and usage strings; the live part belongs to the mobile
  app auditor, whose HAR can be searched with `match_canaries.py scan`.

## Output fields

`scope` (targets, repo, regions, flows with reach, consent states tested, covered, not_covered),
`metadata` (browser, canary set id, lists, declared processors source, tools), `inventory[]`,
`recipients[]` (with `declared_as` and blocked request counts), `consent_matrix[]`, `findings[]` (type, title, candidates, categories, fields, vendor,
recipient kind, consent states, flow basis, reach, sensitivity, flow, score, priority, floor, cap,
confidence, evidence, references, fix, verification), `triage[]`, `headline` (counts, third parties
before consent and after rejection - `null` for a state that wasn't tested - `states_not_tested`, special-category
flows, statement, disclaimer), `limitations[]`.

## What the validator enforces

No personal data, unredacted canary or secret in the report or any evidence file. Recipients and the
consent matrix equal the merge output, and `--recompute` re-runs the merge. Findings match their
candidates (type, fields, categories, states, recipient kind), cite requests that exist in the capture
and code lines that exist in the repo, and carry the references `data/provisions.json` gives for the
regions. Ratings, floors and caps are recomputed; `observed` needs a live candidate and `traced` a code
path; agent judgment stays `pattern`, and `pattern` is never P0. Every candidate is triaged once, and each
dismissal code is checked against the candidate's facts. Headline counts are recomputed, the disclaimer
is present, and no text calls the app compliant. A state that wasn't tested must be `null` in the headline,
listed in `states_not_tested`, and described as not tested in the statement (never "none"); when requests were
blocked in the browser, a limitation must say that third parties were blocked.
