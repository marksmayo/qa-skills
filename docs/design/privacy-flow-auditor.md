# Design: `privacy-flow-auditor-skill`

Status: Built (v1.0.0). Build order: 23.

## Mission

The skill maps where personal data goes in an application:

- what it collects (forms, SDKs, device APIs);
- where it stores it;
- what it writes to logs;
- which third parties receive it, and whether that happens before the user consents.

It combines a code review with a live capture of the app's network traffic. It produces a data
inventory, a flow map and ranked findings, each tied to evidence.

How it differs from the existing skills:

- **code-security-auditor** asks whether an attacker can get at data. This skill asks what the app
  itself does with personal data when everything works as designed.
- **The WCAG auditor** and other browser skills look at the page. This skill looks at the requests
  the page sends, including to analytics, advertising and session-replay vendors.

It maps findings to GDPR articles, the UK GDPR and the Australian Privacy Principles (APPs) as
**references for a privacy professional**. Other laws (US state laws such as CCPA/CPRA) are out
of scope in v1. It never says an app is compliant, and it isn't legal advice.

## Decisions

- **Canary data, not real data.** In the live capture, the skill types synthetic personal data
  into forms, each value unique and traceable, for example `qa.canary.7f3a@example.invalid` and
  a reserved test phone number. It then searches every outgoing request for those values,
  including encoded forms: URL-encoded, Base64, and lowercased SHA-256, SHA-1 and MD5 hashes.
  That's how "hashed email" sharing with ad platforms shows up. Real personal data is never typed,
  captured or stored.
- **Consent is tested in three states.** Before any choice, after "reject all", and after "accept
  all". Tracking that happens before a choice or after rejection is the most important finding.
- **Secrets follow the code-security-auditor rule.** A key or token found along the way is
  reported by location and type only.
- **References, not verdicts.** Each finding carries the legal provisions it relates to (GDPR
  Art. 5(1)(c), 6, 7, 9, 13, 25, 28, 32, 44, with the same articles of the UK GDPR; ePrivacy
  Directive Art. 5(3) and PECR reg. 6 for cookies; APP 1, 3,
  5, 6, 8, 11). The report says a qualified person decides whether there's a breach.
- **Web in v1, with mobile code included.** Static analysis covers Android and iOS source (SDK
  inventories, manifest permissions, `Info.plist` usage strings, privacy manifests). Live capture
  of native apps belongs to `mobile-app-auditor`, which can hand over its traffic capture.

## Non-negotiable rules

- **No personal data in any output.** Captured requests are stored with values replaced by
  `<canary:email>` markers or `<redacted:type>`. Field names, hostnames and data categories are
  kept. The validator scans every evidence file for anything that looks like an email address,
  phone number, card number or government identifier that isn't a known canary.
- **Every flow has evidence.** A third-party transfer needs either a captured request (host, path
  pattern, which canary or category it carried, consent state) or a code path (file and line from
  the collection point to the SDK call). An SDK that's installed but never seen sending data is
  reported as `present`, not as a transfer.
- **Category claims are specific.** "Personal data" isn't a category. Each data item is classified
  as one of the categories in `data/categories.json` (contact, identity, government identifier,
  financial, precise location, device identifier, online identifier, health, biometric, and the
  other special and sensitive categories), with the evidence for that classification.
- **"No findings" doesn't mean "private" or "compliant".** The report lists which pages, flows,
  consent states, code paths and SDKs were covered.
- **Third-party identification comes from public lists.** A host is named as a vendor only through
  the domain list or a code import, never from a guess about the domain name.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `repo` | optional | Source for the static part |
| `targets` | optional | Base URL for the live part; at least one of `repo` and `targets` is needed |
| `flows` | discovered forms | Steps to sign-up, checkout, contact and similar forms, as in the other browser skills |
| `authorised_by` | required for non-loopback | Shared harness rule |
| `consent` | detected | Selectors or button names for the consent banner's reject and accept actions |
| `regions` | `AU`, `EU` | Which provision sets to reference |
| `first_party_domains` | target's site | Domains that count as the organisation's own |
| `declared_processors` | optional | The organisation's list of vendors (from its privacy policy or records), to compare with what's observed |
| `privacy_policy_url` | optional | The agent reads it and lists vendors and categories it mentions, marked as agent judgment |

## Workflow

1. **Recon.** Stack, frameworks, logging libraries, analytics and SDK packages from manifests
   (`package.json`, `requirements.txt`, Gradle, Podfile, `pubspec.yaml`), consent-management
   platform, data stores and their encryption settings.
2. **Static scan** (`scan_privacy.py`, always runs):
   - **Collection points:** form fields and model fields whose names, labels, `autocomplete` and
     `type` values match the category patterns; device APIs (geolocation, contacts, camera,
     advertising ID, `READ_PHONE_STATE`, `NSLocationWhenInUseUsageDescription`).
   - **Sinks:** logging calls (`logger.*`, `console.*`, `print`, `Log.d`, `NSLog`, `os_log`),
     analytics and SDK calls (`gtag`, `fbq`, `analytics.track`, `Sentry.setUser`, session-replay
     init), storage (`localStorage`, cookies, database columns, files), outbound HTTP.
   - **Flows:** a collection point's value reaching a sink, traced by the agent through the code
     like a source-to-sink path in the security auditor.
   - **Semgrep OSS** with the skill's own rules, if installed, merges in through `sarif.py`.
3. **Live capture** (`capture_flows.py`, through `browser_harness.py`):
   - For each consent state, load the start page, record cookies, storage and every request before
     any interaction, then run each flow typing canary values.
   - Search all request URLs, headers and bodies, and all cookies and storage values, for each
     canary in each encoding.
   - Classify each request host as first party, a declared processor, or a third party, and name
     the vendor through the domain list.
   - Form submission stays blocked, as in the other browser skills: the canary search covers what
     the page sends to analytics and vendors as the user types and navigates. With
     `allow_form_submission` on a test environment, the submission itself is captured too.
4. **Inventory.** Merge static and live evidence into data items (category, where collected,
   where stored, where logged, who receives it, under which consent state).
5. **Triage** every candidate: promoted to a finding, recorded as inventory only (a legitimate,
   expected flow), or dismissed with a reason.
6. **Rank, validate and render.**

## Finding types

| Type | Example |
|---|---|
| `tracking_before_consent` | A pixel or analytics request, or a non-essential cookie set, before the user chooses |
| `tracking_after_reject` | The same after "reject all" |
| `personal_data_to_third_party` | A canary (or its hash) sent to an ad or analytics host |
| `personal_data_in_url` | A canary in a query string, where it lands in logs and `Referer` headers |
| `personal_data_in_logs` | A logging call that writes a collected value, or a server log line containing a canary (local runs) |
| `session_replay_unmasked` | A session-replay SDK recording input fields without masking |
| `sensitive_category_flow` | Health, biometric or other special-category data reaching a third party or logs |
| `insecure_storage` | Personal data in `localStorage`, unencrypted mobile storage, or a non-`Secure`/`HttpOnly` cookie |
| `undeclared_processor` | A third party receiving data that isn't in `declared_processors` |
| `excess_collection` | A collected field that no code path uses (agent judgment, never high confidence) |
| `cross_border_transfer` | Data sent to a vendor whose endpoints are outside the region, from the domain list's metadata |
| `missing_usage_description` | A mobile permission requested without a purpose string or privacy-manifest entry |

## Rating

**Sensitivity** of the most sensitive category involved: special or sensitive category (health,
biometric, racial or ethnic origin, religion, sexual orientation, criminal record, children's
data) 4; government identifier, financial or precise location 3; contact, identity or device and
online identifiers 2; coarse or derived data 1.

**Flow:** sent to an undeclared third party, or before consent or after rejection 3; sent to a
declared processor in a way the user wouldn't expect (hashed email to an ad platform), written to
logs, or stored insecurely 2; collected without a use 1.

**Reach:** a page or flow every user goes through (sign-up, the site-wide header) 3; a common
flow 2; a rare one 1.

`score = sensitivity x flow x reach`. P0 >= 27, P1 >= 18, P2 >= 8, otherwise P3.

Floors: `sensitive_category_flow` to a third party is P0; `tracking_after_reject` is at least P1.

**Confidence:** `observed` (a canary or the request seen in the live capture), `traced` (a full
code path from collection to sink), `pattern` (a matching call with an incomplete path). Agent
judgment (excess collection, policy comparisons) is at most `pattern`. P0 needs `observed` or
`traced`.

## Output (`privacy_audit.schema.json`)

| Section | Content |
|---|---|
| `scope` | Repo, targets, authorisation, regions, flows, consent states tested |
| `metadata` | Browser, tools and versions, domain list version, canary set id (never the values for real accounts; canaries are synthetic) |
| `inventory` | Data items: category, collection points, storage, logs, recipients, consent states, evidence |
| `recipients` | Each host: vendor, first party / declared / third party, region, categories received, consent states seen |
| `consent_matrix` | Per consent state: cookies set, requests to third parties, canaries seen |
| `findings` | Type, category, sensitivity, flow, reach, score, priority, confidence, evidence, legal references, fix, verification step |
| `triage` | Every candidate and its outcome |
| `headline` | Counts by priority, third parties seen before consent and after rejection, special-category flows |
| `limitations` | Server-side processing not visible, native apps not captured, flows not run, encodings not checked |

## Validator fails when

- Any output or evidence file contains a value that looks like personal data and isn't a known
  canary marker, or contains a secret value.
- A transfer finding has neither a captured request nor a code path.
- A recipient is named as a vendor without a domain-list entry or a code import.
- A finding has no category, or uses a category not in `data/categories.json`.
- An agent-judgment finding is above `pattern`, or a `pattern` finding is P0.
- A consent-state finding refers to a state that wasn't tested.
- Any candidate isn't triaged.
- Text anywhere says "compliant", "GDPR-ready", "meets the APPs" or similar.

## Edge cases

- **Consent banners the skill can't operate:** the before-consent state is still captured; the
  reject and accept states are marked untested.
- **Server-side tagging** (a first-party subdomain that forwards to vendors): requests look first
  party. The skill flags known server-side tagging paths and lists them as a limitation.
- **Hashing with a salt** can't be matched; the skill records that a request carried an
  unrecognised high-entropy value next to an email-shaped field name, as a `pattern` candidate.
- **Single-page apps** send analytics on route changes; the capture waits for network idle after
  each step.
- **Error trackers** (Sentry, Bugsnag) often capture user context and breadcrumbs; the static scan
  checks `beforeSend` scrubbing and `sendDefaultPii`.
- **Children's services:** if the flows include age gates or the user says the service targets
  children, sensitivity for all categories goes up one level.

## Tool stack

| Tool | Licence | Role |
|---|---|---|
| Playwright for Python | Apache-2.0 | Live capture through `browser_harness.py` |
| Semgrep OSS | LGPL-2.1 | Optional extra static rules |
| EasyPrivacy list | GPL-3.0 or CC BY-SA 3.0 | Tracker domain matching, pinned and hash-checked in `data/lists.lock.json` |
| Open Cookie Database | Apache-2.0 | Cookie name to vendor and purpose |

DuckDuckGo Tracker Radar and the Disconnect list are left out: their licences don't allow
commercial use. Bearer CLI is left out until its licence is reviewed. Everything else is stdlib
Python.

## Files

```text
privacy-flow-auditor-skill/
  SKILL.md  RUBRIC.md  README.md  requirements.txt
  data/categories.json             # categories, sensitivity, field-name and label patterns
  data/sinks.json                  # logging, analytics, storage and SDK sink patterns per stack
  data/provisions.json             # finding type -> GDPR, UK GDPR, ePrivacy, PECR and APP references
  data/lists.lock.json             # pinned tracker and cookie lists with hashes
  schemas/privacy_audit.schema.json
  scripts/browser_harness.py  sarif.py   # shared copies
          fetch_lists.py           # download + verify the domain and cookie lists
          scan_privacy.py          # static collection points, sinks, candidate flows
          capture_flows.py         # Playwright: consent states, canaries, redacted capture
          match_canaries.py        # search captures for canaries in every encoding
          validate_privacy_audit.py  render_report.py
  examples/
```

## Example

`examples/clinic-signup/` is a small patient sign-up app (Flask with a static front end), served on
loopback, with stub "vendor" endpoints on other loopback ports mapped to made-up vendor hostnames (under
`.test`) through the harness's host mapping; a small example domain list classifies them, so no
real vendor is named. The answer key in its README lists:

- an analytics pixel firing before the consent banner is answered;
- a lowercased SHA-256 of the email sent to an ad endpoint after "accept all";
- the same pixel still firing after "reject all";
- the email in a query string on the confirmation redirect;
- `logger.info` writing the date of birth and the "reason for visit" field (health data);
- a session-replay snippet with input masking off;
- the email address kept in `localStorage`;
- a declared-processors list that leaves out the chat widget.

Decoys for triage: a first-party login cookie set before consent (strictly necessary), a hashed
value that's a cache key rather than an identifier, and an error tracker with `sendDefaultPii`
off. `examples/run/` is a real capture with every value redacted.

## Resolved questions

Agreed with the user before the build.

1. **Which laws to reference?** GDPR, UK GDPR and the Australian Privacy Principles.
2. **Compare the privacy policy with observed traffic?** Yes, labelled as agent judgment.
3. **Example's stub vendors?** Made-up hostnames.

## As built (v1.0.0)

Where the build differs from the design above, and what the example showed.

- **Vendor names come from a curated `data/vendors.json`**, not from the Open Cookie Database's domain
  column: that column is mostly empty and named hotjar.com after the wrong platform. EasyPrivacy gives the
  tracker flag, the Open Cookie Database names cookies. Neither list has regions, so cross-border
  candidates need regions from the user (`fetch_lists.py --extra`). The lists are downloaded to a cache,
  pinned by hash in `data/lists.lock.json`, and not committed (EasyPrivacy is GPL/CC BY-SA).
- **`merge_candidates.py`** (not in the design's file list) turns the capture and the static scan into
  candidates, recipients and the consent matrix; the validator's `--recompute` re-runs it.
- **Matching happens during the capture**, so raw values never reach disk; `match_canaries.py scan`
  searches a HAR from elsewhere. Encodings include double URL-encoding, which is how the email reached
  the analytics vendor through the page URL in the example.
- **The special-category floor applies to any finding type** that sends a special category to a third
  party or processor (the example's P0 is a `session_replay_unmasked` finding carrying health text).
- **Triage has three outcomes:** promoted, `inventory` (expected flows, such as a declared processor's
  region) and dismissed with a checked reason code.
- **The browser harness gained `host_map`** (Chromium host-resolver rules), so the sample site and its
  stub vendors use made-up `.test` hostnames on loopback.
- **Not built:** reading the privacy policy (left to the agent, as judgment), server-side tagging
  detection, the mobile manifest checks beyond permissions and usage strings, and Semgrep rules (only the
  SARIF merge).
- **Example results:** 3 consent states tested; 25 candidates; 9 findings (1 P0, 5 P1, 3 P2), matching
  the eight planted problems plus the email reaching Trackly through the URL; all three decoys dismissed.
- **Static scan gaps found on a real React app** (an internal app with no forms of its own, where the
  scan produced no candidates and every finding was traced by hand) are fixed:
  - JSX form elements are collection points (`input`, `textarea`, `select` and component-library fields
    such as MUI `TextField`, with `name`, `id`, `autoComplete`, `label`, `placeholder`), as are
    `register('field')` calls;
  - a Datadog RUM `init` that records session replays with `defaultPrivacyLevel` unset or `allow` is a
    `session_replay_unmasked` candidate, with the init's privacy facts (action-name privacy, `beforeSend`,
    site); with replay off, tracked interactions without action-name privacy are a candidate;
  - Datadog browser logs, IndexedDB, localforage and whole-cache persisters (Apollo `persistCache`,
    React Query, Redux persist) are sinks, and a persisted cache is an `insecure_storage` candidate;
  - SDKs imported in code that no manifest declares (a service worker), and `importScripts()` hosts, are
    in the SDK inventory.
- **Testing:** 34 of 34 seeded violations are caught, including personal data and secrets planted in
  evidence files.
- **Gaps found on a real static site, fixed** (a plant-guide site that injects Google Analytics and AdSense
  from its own scripts, with no consent banner):
  - `fetch_lists.py` pinned EasyPrivacy's unversioned easylist.to URL, which is rebuilt several times a day,
    so the hash pin broke within hours. Both lists are now pinned to a git commit of their GitHub repository
    (EasyPrivacy's built list is committed to the `gh-pages` branch of easylist/easylist), which never
    changes. `--update` pins each branch's latest commit and can write the lock elsewhere (`--write-lock`),
    and `--check` with an empty cache says the lists aren't downloaded instead of reporting a hash mismatch.
  - `scan_privacy.py` found no SDKs because both scripts were injected from JavaScript; it now records
    hosts from `el.src = "https://..."`, `setAttribute('src', ...)`, URL literals in a file that creates a
    script element, and URL literals on a listed vendor's domain. Its 51 candidates were all `name` matched
    on `path.name` and inside strings: a field now matches only as an identifier, a key or an interpolation,
    property accesses don't count for everyday names (`generic_property_names`), and `data['name']` is no
    longer a form read (`request.form`, `formData`, `req.body` and similar are). It took 3.5 minutes on
    about 2,350 files because every line was searched with every pattern; it now reads each file once, runs
    each pattern over the whole text only when one of its literal `hints` (in `data/sinks.json`) occurs,
    prunes dependency directories while walking, and lists what it skipped (files over `--max-bytes`,
    minified or generated files, skipped directories). The same repository takes about 20 seconds and
    gives no false candidates.
  - Declared processors matched only by exact vendor name, so a policy naming "Google Analytics" and
    "Google Ads" left googletagmanager.com ("Google Tag Manager") and googlesyndication.com ("Google
    AdSense") undeclared. `data/vendors.json` now has vendor aliases and parent companies; a recipient
    records the declared name and whether it matched exactly, by alias, by company or by a listed domain.
  - With no consent control, the headline said `After "Reject all": none`. An untested state is now
    `null` in the headline and listed in `states_not_tested` with its reason; the renderer prints "not
    tested (reason)", and the validator rejects an empty list or a "none" statement for it.
  - `registrable()` turned `127.0.0.1` into `0.1`; IP addresses and `localhost` are now returned whole.
  - The shared browser harness now blocks third parties by default. The capture records each attempted
    request with `blocked: third_party`, the merge counts blocked requests per recipient and lists the
    blocked domains, the renderer names them, and the validator requires a limitation saying that blocked
    vendors set no cookies and sent no follow-up hits. Stub vendors on `*.test` hosts are loopback and still load.

### v1.1.0: gaps found on a Next.js app on shared hosting (static scan and a live capture)

- **Static-only mode.** The merge and the validator needed a capture, so a static review could only be
  reported by inventing one. `merge_candidates.py` without `--capture` now writes a `not_run` output
  (every consent state `not_run`, no recipients, the static candidates), and the audit carries
  `run_status` (`complete`, `partial`, `not_run`) and `run_status_reason`. A `not_run` audit validates
  without a capture or canaries, but the validator rejects observed findings, captured requests, consent
  states, live candidates and statements only a browser run could support. `examples/static-only/` and
  `privacy_audit.static-only.example.json` show it. A capture with an untested or missing state, step
  errors or an exhausted request budget is `partial`.
- **Findings found by review** (`source: agent`, `basis` of file lines re-read with `--repo`, or runtime
  evidence) cover flows no pattern catches; the renderer labels them.
- **Shared hosting.** `registrable()` had no Public Suffix List, so `myapp.vercel.app` became `vercel.app`:
  other tenants were first party, and EasyPrivacy rules for two other vercel.app apps made the site's own
  recipient a "tracker". `data/public-suffix-private.json` is a hand-transcribed subset of the PSL private
  section, pinned by SHA-256 in the lock (`bundled`) and checked by `fetch_lists.py`; `scripts/domains.py`
  is used by the scan, the list build and the merge; `first_party_domains` are honoured as given.
- **First-party tracking paths.** `fetch_lists.py` dropped EasyPrivacy's path rules; they are now kept
  (`path_rules`), and with curated rules in `data/vendors.json` (Vercel Web Analytics and Speed Insights
  paths, Sentry tunnels by path or by an envelope body) they flag analytics proxies and tunnels on the
  site's own host (`subtype: first_party_proxy`).
- **Blocked first-party writes** were listed as a "blocked vendor" (the site) and were no candidate.
  The renderer now lists only third parties as blocked vendors; first-party writes fired without a form
  submission are `first_party_beacon` candidates (a new type with its own references), listed separately
  (`blocked_first_party_writes`), and need a limitation.
- **CSP.** Calls the site's CSP stops never reach the request event. The capture keeps the CSP in force
  (`bypass_csp: false`, the shared harness's default is true), records console CSP violations, and the
  merge reads CSP report bodies; vendor attempts become `csp_blocked_attempts` on tracking candidates and
  recipients. A capture run with the CSP bypassed needs a limitation.
- **Per-load ids.** 49 Sentry `sid`/`event_id`/`trace_id` values became 49 "possible hashed identifier"
  candidates. They are grouped per (domain, key) with facts (distinct values, a new value every request,
  recurring across consent states, a known SDK id key), and `random_per_load_id` dismisses them only when
  the facts show it.
- **Consent states** `partial` (save preferences, with `partial_refuses`) and `dismiss` (closed without a
  choice); the capture waits for the banner before clicking; a blocked consent-record write is recorded
  (`consent_writes_blocked`) and needs a limitation.
- **Evidence.** Bodies are read as bytes and decompressed (gzip, deflate, brotli when installed) or
  Base64-decoded before searching, with `body_read` per request. The card redactor no longer fires on
  epoch-millisecond timestamps, decimal fractions or ids glued to letters. Screenshots mask the fields a
  canary was typed into, and a limitation must say what images may show. The capture records the
  harness's settings and request counts (`harness`).
- **Validator.** `--write-summary` crashed with `KeyError: 'priority'`; it now fills missing ratings (and
  reports what it can't compute). A readable CSRF cookie can be `strictly_necessary` with a reason that
  says so; `constant_in_source` searches .tsx/.jsx/.mjs/.mts and more, pruning dependency directories.
- **Static scan.** Sink calls are read across their whole argument span (multi-line `Sentry.setUser({`,
  `mixpanel.people.set({ $email ...`); identify and error-tracker calls count identity keys; small app
  functions that pass a parameter to a log, analytics or error-tracker call are sinks one level deep
  (`logSecurityEvent`, `trackMixpanelEvent`; `--log-sinks`/`--analytics-sinks` add more), and `logger.*`,
  pino and winston are JS/TS log sinks; controlled React inputs take their field from `value={form.x}` or
  the setter and their label from the `<label>` before them; consent checks near a sink are noted;
  server code is marked and vendor endpoints called from it are `server_side_send` candidates; Sentry
  Replay without masking options or a nearby consent check is a candidate; test directories and files
  are skipped by default; social profile and documentation links are not SDKs; script hosts lose query
  strings. On the real app (462 files, about 2 s) the scan went from 1 candidate to 70, including the
  flows a reviewer had found by hand (identity to Sentry and Mixpanel, email in security logs through a
  wrapper, Mixpanel events from API routes, replay before consent).
- **Not done:** a library shared by server and client code is not recognised as server code; `@/` path
  aliases are not resolved; server actions are not modelled; the PSL subset is not the full private
  section (no network download step); a live CSP report or compressed Replay segment was not exercised
  against a real site (unit-checked on synthetic input only).
