# Rubric: privacy flow auditor

## Sensitivity (the most sensitive category in the finding, `data/categories.json`)

| Level | Categories |
|---|---|
| 4 | Special or sensitive: health, biometric, genetic, racial or ethnic origin, religion, sexual orientation, political opinions, trade union membership, criminal record |
| 3 | Government identifier, financial, precise location |
| 2 | Contact, identity, date of birth, device identifier, online identifier, contacts and media |
| 1 | Coarse location, browsing behaviour (derived) |

Children's services (`children: true`): every category one level higher, up to 4.

## Flow (`flow_basis`)

| Basis | Flow | Meaning |
|---|---|---|
| `undeclared_third_party` | 3 | Sent to a third party that isn't in the declared processors |
| `before_consent` | 3 | Sent, or a non-essential cookie set, before the user chooses |
| `after_reject` | 3 | Sent, or a cookie set, after "Reject all" |
| `declared_unexpected` | 2 | Sent to a declared processor in a way the user wouldn't expect (a hashed email to an ad platform, form values to session replay) |
| `logs` | 2 | Written to logs |
| `insecure_storage` | 2 | localStorage, sessionStorage, unencrypted mobile storage, a cookie without Secure/HttpOnly |
| `url` | 2 | In a URL, where it reaches logs, history and Referer headers |
| `no_use` | 1 | Collected without a use (agent judgment) |

An identity SDK's token cache in `localStorage` or `sessionStorage` (`auth_token_storage`) is
`insecure_storage`; its reach is usually 3 (every signed-in user). With no consent banner (`no_banner`),
anything sent is `before_consent`: there is no choice to wait for. The same holds for a send that is
on by default with an opt-out as the only choice (a command-line tool's call home, a page beacon): a
`telemetry` or `first_party_beacon` finding found by review sets `default_on: true` and may be
`before_consent`, on its basis (the code that sends unless an opt-out is set, or a sink log of a run with
no opt-out).

The validator checks the basis against the candidates: `before_consent` needs the before state (or
`default_on` as above), `after_reject` the reject state, `undeclared_third_party` an undeclared recipient,
and `declared_unexpected` a declared processor that names the recipient: the finding's vendor (or its
alias or parent company) or domain must be in the `--declared` file or in the text of
`metadata.declared_processors`; a finding that names no recipient is at most `pattern`.

## Reach (from `flows.json`)

3 a page or flow every user goes through (the landing page, sign-up, the site-wide header); 2 a common
flow; 1 a rare one. Record the reason in `reach_source`.

## Score and priority

`score = sensitivity x flow x reach`. P0 >= 27, P1 >= 18, P2 >= 8, otherwise P3.

**Floors.** A special category reaching a third party or declared processor, whatever the finding type,
is P0 (`special_category_to_third_party`). Tracking after "Reject all" is at least P1 (`tracking_after_reject`).

**Cap.** A `pattern` finding is never P0 (`pattern_not_P0`). The floor applies first, then the cap.

## Confidence

| Level | Meaning |
|---|---|
| `observed` | A canary, request or cookie seen in the live capture (needs a live candidate). A request the browser blocked was still attempted by the page, so it is observed; what the vendor would have done next is not, and goes in the limitations |
| `traced` | A full code path from collection to sink (file and line at each step) |
| `pattern` | A matching call with an incomplete path; agent judgment (excess collection, policy comparison) is never higher |

## Finding types and references

Each type maps to provisions in `data/provisions.json` (EU: GDPR and ePrivacy; UK: UK GDPR and PECR;
AU: the APPs), listed for the regions in scope:

| Type | Example |
|---|---|
| `tracking_before_consent` | A pixel, analytics request or non-essential cookie before the choice |
| `tracking_after_reject` | The same after "Reject all" |
| `personal_data_to_third_party` | A canary (or its hash) in a request to an analytics, ad or other vendor |
| `personal_data_in_url` | A canary in a first-party query string |
| `personal_data_in_logs` | A canary in a log line, or a logging call writing a collected field |
| `session_replay_unmasked` | Replay recording input values |
| `sensitive_category_flow` | Special-category data reaching logs or a third party |
| `insecure_storage` | A canary in browser storage or a weak cookie |
| `undeclared_processor` | A vendor receiving data that isn't in the declared list |
| `excess_collection` | A field no code uses (judgment) |
| `cross_border_transfer` | A vendor outside the regions receiving data (needs a region from the list) |
| `missing_usage_description` | A mobile permission without a purpose string |
| `first_party_beacon` | A first-party write fired without a form submission (telemetry, experiment exposure, autosave), usually before any choice |
| `telemetry` | A call home from software on the user's machine (a command-line tool's usage events, a crash report, an update check carrying an identifier), usually on by default with an environment variable or setting as the opt-out |

A domain is a tracker in the list sense only when EasyPrivacy blocks the whole registrable domain
(`||example.com^`). A rule for one host (`||stats.example.com^`) or one path (`||example.com/pixel.js`)
flags that host or path (`list_tracker_hosts` on the candidate), so a request to another service on a
shared domain (a Google Fonts stylesheet on `fonts.googleapis.com`) is a third-party request that
discloses the visitor's IP address and the page, not tracking. With `--declared`, every third party
contacted that no declared processor names is an `undeclared_processor` candidate, with a canary (what it
received) or without one (subtype `no_canary`: it received at least the IP address and page URL).

Tracking findings also cover first-party tracking paths (an analytics proxy or error-tracker tunnel on
the site's own host) and vendor calls the site's own CSP stopped in the browser (attempted, never sent),
and the `partial` (some purposes refused) and `dismiss` (banner closed without a choice) states: tracking
after a dismissal is tracking before consent.

## Static-only audits and findings found by review

With `run_status: not_run` (no capture), findings rest on the code: `traced` with a full code path, or
`pattern`. Nothing is `observed`, no consent state is named, and `before_consent` needs a static
`tracking_before_consent` candidate (pattern: a replay set up with no consent check, or an analytics, ad or
tag-manager loader with no consent check and no consent-mode default, `loader_before_consent`), or a
`default_on` review finding. A finding found by
reading the code (`source: agent`) carries a `basis` of re-checkable file lines (or runtime evidence) and
is at most `traced` without runtime evidence. With recorded runtime evidence (a `{"evidence": file}` basis:
a loopback sink or proxy log a command-line tool sent to) it may be `observed`, in a `not_run` audit too:
the browser run is one kind of runtime evidence, not the only one.

## Triage outcomes

`promoted`, `inventory` (expected and recorded, for example a declared processor's region; never for
special categories or tracking after rejection), or `dismissed` with a code the validator checks:
`strictly_necessary` (a first-party HttpOnly cookie, or a first-party CSRF cookie whose name says
csrf/xsrf and whose reason says it is the readable half of a double-submit check),
`functional_no_identifier`, `constant_in_source`, `random_per_load_id` (hex values that change on every
request and never recur across consent states), `pii_disabled` (default PII off, or replay masked by the
SDK default), `duplicate`, `false_match`, `not_personal_data`. A special-category candidate can only be
dismissed as `false_match`.
