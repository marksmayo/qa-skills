---
name: privacy-flow-auditor
description: Map where personal data goes in a web app - what it collects, where it stores it, what it logs, and which third parties receive it, and whether that happens before the user consents or after they reject. Combines a static scan of the code (collection points, logging, analytics, session replay, storage, SDKs, mobile permissions) with a live browser capture in three consent states (before a choice, after "Reject all", after "Accept all"), typing synthetic canary values and searching every request, cookie and storage entry for them raw, URL-encoded, Base64 and hashed. Produces a data inventory, recipients, a consent matrix and ranked findings with GDPR, UK GDPR and Australian Privacy Principles references for a privacy professional. Never types or stores real personal data, and never says an app is compliant.
version: 1.2.0
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
4. **Consent is tested in three states**: before a choice, after "Reject all", after "Accept all"; add
   `partial` (save preferences with some purposes refused) and `dismiss` (the banner closed without a
   choice: its X or an overlay click) when the banner has them. Tracking before a choice or after
   rejection is the most important finding. A banner the skill can't operate leaves that state
   `untested`, and the report says so. An app with **no consent banner at all** (an internal tool) says
   so in `flows.json` (`"consent": {"banner": null}`): one state, `no_banner`, is captured and counts as
   before-consent; the other states are `not_applicable`, and the headline says "no consent banner" and
   how many third parties were contacted.
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
11. **A run that didn't happen is not a pass.** `run_status` is `complete`, `partial` (a consent state
   untested or missing, flow steps failed, the request budget ran out) or `not_run` (no capture: static
   only), with `run_status_reason`. A `not_run` audit is still delivered from the static scan alone, but
   it tests no consent state, has no observed finding or captured request, and never says nothing was
   sent or that consent is respected (the validator checks).

## Inputs

| Input | Default | Notes |
|---|---|---|
| `repo` | optional | Source for the static scan; `--exclude` third-party or generated directories |
| `flows` | required for the live part | `flows.json`: `site`, `consent` selectors for accept and reject, flows with steps; `{"fill": sel, "canary": field}` types a canary; each flow has a `reach` (3 every user, 2 common, 1 rare) with its reason |
| `authorised_by`, `allow_form_submission`, `host_map` | as in `browser_harness.py` | |
| `regions` | `AU,EU` | Which provision sets to reference (`UK` adds UK GDPR and PECR) |
| `first_party_domains` | the site's registrable domain | In `flows.json`; honoured as given: an entry covers itself and its subdomains, and a full host (`myapp.vercel.app`) is never widened to its hosting suffix. Registrable domains know shared-hosting suffixes (`data/public-suffix-private.json`: vercel.app, netlify.app, pages.dev, ...), so another tenant is never first party and a list rule for one tenant never marks your site a tracker |
| `consent` | required for the live part | `accept`, `reject` selectors; optional `partial` and `dismiss` (a selector or a list of actions), `banner` (waited for, up to `wait_ms` 8000, before any click) and `partial_refuses` (vendor categories the partial choice refuses). `{"banner": null}`: the app has no consent banner (state `no_banner` only) |
| `storage_state` | none | An app behind sign-in (MSAL, OIDC): a Playwright storage-state file **a person** saved after signing in by hand with a test account (`python scripts/browser_harness.py capture-session --url <app> --out <file>`, which also keeps sessionStorage, or `context.storage_state(path=...)`), kept outside the run folder and never committed or recorded; each state's context starts from it. Save it before any consent choice, or the stored choice leaks into the first state. The harness's `auth` login recipe (credentials from environment variables) also works. Record whose session in `metadata.session` |
| `bypass_csp` | `false` | The capture keeps the site's Content-Security-Policy in force, so a vendor call the CSP forbids is stopped as it would be for a visitor and recorded as a CSP-blocked attempt; `true` bypasses it, and a limitation must say so |
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
python scripts/merge_candidates.py --static <run>/static.json --run-status-reason "why the capture couldn't run" --out <run>/candidates.json   # static only
```

Rebuild `domains.json` (run `fetch_lists.py` again) after updating the skill: it now carries EasyPrivacy's
path rules and the shared-hosting-aware domains.

When the capture can't run (no browser, no authorised target, a site that blocks automation), still
deliver: merge the static scan alone (`run_status: not_run`), trace and triage its candidates, add
findings found by reading the code (below), and say plainly in the statement and limitations that no
consent state, request or canary was tested.

**Findings found by review.** Code reading finds flows no pattern catches. Such a finding sets
`"source": "agent"`, may have no candidates, and carries `"basis"`: `{file, line, excerpt}` items (the
validator re-reads each line with `--repo`) or `{evidence: path}` for runtime evidence. It is at most
`traced`, never `observed` without runtime evidence, and the renderer labels it "found by review".

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
   show data from an API rather than forms: read what the queries return. The scan also:
   - reads a sink call's whole argument span, so an object spread over several lines (`Sentry.setUser({`,
     `mixpanel.people.set({` with `$email` on the next line) links its keys; identify, `setUser` and
     `people.set` calls count identity keys (email, name, username, phone) even when no form collects them;
   - treats small app functions that pass a parameter to a logging, analytics or error-tracker call
     (`logSecurityEvent(event, data)`, `trackEvent(name, props)`) as sinks, one level deep (listed under
     `wrappers`); name others with `--log-sinks` / `--analytics-sinks`; `logger.*`, pino and winston calls
     are JS/TS log sinks;
   - takes a controlled React input's field from `value={form.email}` or its setter, and its label from
     `aria-label`, a `label` prop or the `<label>` just before it (the placeholder only last);
   - notes `consent_check_nearby` on analytics, identify, replay and HTTP candidates (a heuristic, not a
     verdict); marks sinks in server code (route handlers, middleware, `*.server.*`, Sentry server and
     edge configs) `side: server`, and raises a vendor endpoint called from server code as a
     `server_side_send` candidate: a browser capture never sees it and the browser's consent choice
     doesn't stop it (a library shared by server and client code isn't recognised as server code);
   - raises Sentry Session Replay with no masking options (`sdk_default_masking`, `masked_by_default`:
     dismissable as `pii_disabled` once the installed version's default is checked) and with no consent
     check nearby (`replay_before_consent`);
   - skips test directories and files (`__tests__`, `e2e`, `*.test.*`, `test_*.py`; `--include-tests`
     scans them), doesn't count social profile or documentation links as SDKs, and drops query strings
     from script hosts;
   - treats an identity SDK's token cache as storage of identity data: MSAL `cacheLocation`
     (`localStorage`, `sessionStorage`, or nothing set: the SDK default, sessionStorage),
     `storeAuthStateInCookie`, oidc-client-ts `userStore`, Auth0 `cacheLocation: 'localstorage'`. The
     `auth_token_storage` candidate names the SDK, the location and the OIDC scopes requested (`email`
     adds contact data). Any script on the origin can read web storage, so record it in the inventory even
     when it isn't promoted;
   - counts identity read in the browser as collected: tokens, `idTokenClaims`, account claims and
     variables holding a decoded or stored token. A debug helper that logs one becomes a candidate whose
     `identity_source` says where the value came from; a bearer token sent to the app's API does not;
   - lists file inputs, dropzones, client-side parsing (SheetJS, Papa Parse, FileReader) and files
     appended to `FormData` as collection points with no category: whatever the files hold is collected.
     Ask what they contain (customer lists, staff rosters?) and record the answer in the inventory, or
     say in the limitations that it is unknown;
   - counts collection points by category (`collection_by_category`). In an internal business app most
     are business parameters (dimensions, codes, quantities) with no category; they need no triage unless
     the code shows they hold personal data.
3. **Live capture.** For each consent state, a fresh context records requests, cookies and storage
   before any interaction, then after the banner choice, then through each flow typing canaries. With
   `--server-log` on a local run, log lines carrying a canary are recorded too. Requests to third parties
   are recorded and blocked (rule 10). Bodies are read as bytes and decompressed (gzip, deflate; brotli
   when the `brotli` module is installed) or Base64-decoded before searching; each request records
   `body_read`, and a binary body is `binary_not_searched` (say so when a vendor's bodies were binary).
   Content-Security-Policy violations in the console are recorded per state, and the merge also reads
   CSP report bodies (`blocked-uri`, `blockedURL`): vendor calls the site's own CSP stopped are attempts
   (`csp_blocked_attempts`). When the consent click triggers a write the run blocks (the site's record of
   the choice), the state records `consent_writes_blocked`, and a limitation must say the choice was never
   recorded server side. Screenshots mask every field a canary was typed into (Playwright `mask`); text
   the page echoes back can still show one, and images aren't scanned, so a limitation must say what the
   screenshots may show. The harness's settings and request counts are recorded under `harness`. When no consent control can be operated, the reject and accept
   states are `untested` with the reason: the headline then has `null` (not an empty list) for them and
   names them in `states_not_tested`, and the statement says they weren't tested, never "none".
4. **Merge.** Candidates per third-party domain and state, per canary flow, per storage key, per log, plus
   undeclared processors, cross-border transfers (when the list has regions) and unrecognised hashes. Also:
   first-party tracking paths (`subtype: first_party_proxy`: an analytics proxy such as `/_vercel/insights/`
   or a Sentry tunnel, from `data/vendors.json` `path_rules`/`body_rules` and the list's EasyPrivacy path
   rules), tracking after a partial choice or a dismissal, and `first_party_beacon` candidates for
   first-party writes fired without a form submission (initial or after-choice phase, or blocked by the
   no-write rule: telemetry, CSP reports, consent records). Unrecognised hex values are grouped per
   (domain, key) with facts: distinct values, whether a value differs on every request, whether it recurs
   across consent states (fresh contexts), and whether the key is a known SDK id (`sid`, `event_id`,
   `trace_id`, ...). Blocked first-party writes are listed separately (`blocked_first_party_writes`): the
   site is never a "blocked vendor".
5. **Triage** every candidate: `promoted` to a finding, `inventory` (an expected flow recorded in the
   inventory, such as a declared processor's region, for the privacy team), or `dismissed` with a code:
   `strictly_necessary` (first-party HttpOnly session cookie), `functional_no_identifier` (no cookie, no
   identifier, no canary, not an analytics, advertising or replay vendor; a first-party CSRF cookie, readable
   by design for a double-submit check, qualifies as `strictly_necessary` when its name says csrf/xsrf and
   the reason says so), `constant_in_source` (a hash-like value written literally in the code; .js, .ts,
   .tsx, .jsx, .mjs, .mts and more are searched), `random_per_load_id` (hex values that differ on every
   request and never recur in another consent state: a session or trace id, not a hashed identifier),
   `pii_disabled` (error tracker with default PII off, or replay masked by the SDK default), `duplicate`,
   `false_match`, `not_personal_data`. Special categories and tracking after rejection are never inventory-only.
6. **Rate** with `RUBRIC.md`; write for each finding the evidence summary, a fix, and a verification step
   (usually: re-run the capture and show the canary no longer reaches that recipient). Build the data
   inventory per field: collected at, stored, logged, in URLs, recipients with encodings and states.
7. **Validate and render.**

```bash
python scripts/validate_privacy_audit.py <run>/privacy_audit.json --candidates <run>/candidates.json --capture <run>/capture.json --canaries <run>/canaries.json --evidence <run> --repo <repo> --recompute [--write-summary]
python scripts/validate_privacy_audit.py <run>/privacy_audit.json --candidates <run>/candidates.json --repo <repo> --recompute   # static only
python scripts/render_report.py <run>/privacy_audit.json [--brief]
```

## Edge cases

- **Scripts injected from JavaScript** (`script.src = "https://..."`, `setAttribute('src', ...)` after
  `createElement('script')`, a vendor's loader URL in a string) are in the static SDK inventory with
  `source: injected_script` or `vendor_url`, so a site with no `<script src>` to a vendor still shows them.
- **Large repositories:** the scan reads each file once; files over `--max-bytes` (2 MB) and minified or
  generated files are listed under `skipped` (generated files are still searched for SDK hosts), as are
  dependency and build directories. Say in the limitations what was skipped if it could hold app code.
- **Server-side tagging** (a first-party path that forwards to vendors) looks first party; known proxy
  and tunnel paths are matched (`path_rules`, EasyPrivacy path rules), others go in the limitations.
- **Shared hosting** (`*.vercel.app`, `*.netlify.app`): the suffix subset keeps each app its own domain;
  add a suffix to `data/public-suffix-private.json` (and its hash in the lock) when one is missing.
- **Server-side sends** (route handlers posting to an analytics API) never appear in a browser capture
  and ignore the browser's consent choice: they come from the static scan only.
- **Per-load random ids** (Sentry `sid`, `event_id`, `trace_id`, UUIDs) are grouped per key; dismiss them
  as `random_per_load_id` only when the facts show a new value every time.
- **Error monitoring vs analytics:** a list may call an error tracker a tracker; whether it needs consent is
  the privacy professional's call, so the finding text carries that nuance.
- **Salted hashes** can't be matched; an unrecognised 32/40/64-hex value in a third-party request is a
  `pattern` candidate, dismissed only when the value is shown to be a constant in the code.
- **Single-page apps** send analytics on route changes; the capture waits for network idle after each step.
- **Apps behind sign-in** (Entra ID, Auth0, any OIDC provider) show only the sign-in page without a
  session, and the identity provider's redirect is a third party the harness blocks. Ask for a test
  account and have a person sign in once and save a storage-state file (`storage_state`), or use the
  app's own mock-auth or test flag if it has one; never sign in with a real person's account. Without
  a session, capture the sign-in page and say in the limitations that the signed-in app wasn't captured.
- **Internal tools with no tracking:** no banner and no third parties is a result worth stating, but only
  from a capture: `consent.banner: null`, and the headline says "no consent banner; N third parties
  contacted". A static-only audit says what the code does ("the code loads no analytics SDK"), never
  what the running app sends.
- **Children's services:** set `children: true` on findings; every category's sensitivity goes up one level.
- **Native apps:** the static scan reads manifests and usage strings; the live part belongs to the mobile
  app auditor, whose HAR can be searched with `match_canaries.py scan`.

## Output fields

`run_status` and `run_status_reason`, `scope` (targets, repo, regions, flows with reach, consent states
tested, covered, not_covered),
`metadata` (browser, canary set id, lists, declared processors source, tools), `inventory[]`,
`recipients[]` (with `declared_as` and blocked request counts), `consent_matrix[]`, `findings[]` (type, title, candidates, categories, fields, vendor,
recipient kind, consent states, flow basis, reach, sensitivity, flow, score, priority, floor, cap,
confidence, evidence, references, fix, verification; `source: agent` and `basis` for a finding found by review), `triage[]`, `headline` (counts, third parties
before consent and after rejection - `null` for a state that wasn't tested - `states_not_tested`, special-category
flows, statement, disclaimer; `consent_banner: false` and `states_not_applicable` for an app with no banner),
`metadata.session` (whose signed-in session the capture used), `limitations[]`.

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
blocked in the browser, a limitation must say that third parties were blocked. `run_status` equals the
merge's; a `not_run` audit has no observed finding, captured request, consent state, live candidate or
recipient, says in a limitation that the capture didn't run, and no statement or title claims what only a
browser run could show; in a not_run audit a clause that says where data goes ("sent only to", "never
leaves") must say it is what the code does ("by the code", "the static scan"). Agent findings' basis lines
are re-read. Limitations must also cover blocked first-party writes, a blocked consent record, a bypassed
CSP, screenshots in the evidence, and a signed-in session (with `metadata.session`); a storage-state file in
the evidence fails. File upload points from the scan must be followed up in the inventory, scope or
limitations. With no consent banner, the statement says so. `--write-summary` fills missing ratings and the
headline instead of failing on them; `--fix-ratings` rewrites computed rating fields that are wrong and
lists each change (the rating error shows the arithmetic).
