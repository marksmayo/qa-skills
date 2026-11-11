# Example: clinic-signup

`clinic-signup/` is a small patient sign-up site (`serve.py`, stdlib, loopback port 8152) with stub
vendors on port 8153. The browser maps `*.test` to 127.0.0.1, so the site is
`www.clinic-signup.test` and the vendors are Trackly (analytics), Adnetic (advertising), Replayr
(session replay), Chatterbox (support chat) and Bugcatch (error tracking), all made up. The stubs keep
nothing. `declared_processors.json` is the clinic's own vendor list; it leaves Chatterbox out.
`lists.example.json` stands in for the fetched lists and names the made-up vendors (its regions are
invented).

`run/` is the real run, listed in `run/commands.sh`: the canary set (`canaries.json`, synthetic), the
static scan, the three-state capture with every value redacted, screenshots, and `candidates.json`
(19 live and 6 static candidates). `privacy_audit.example.json` is the audit. Chromium 158 via Playwright.

## Answer key

| Planted | Expected | Got |
|---|---|---|
| The Trackly pixel and its `_tk` cookie load on every page before the banner is answered | tracking_before_consent | F04, P1 |
| ... and still after "Reject all" | tracking_after_reject | F03, P1 |
| After "Accept all", leaving the email field sends a lowercased SHA-256 of the email to Adnetic | personal_data_to_third_party (hashed) | F08, P2: `email:sha256` in `px.adnetic.test/ev` |
| The sign-up redirect puts the email in `/welcome?email=` | personal_data_in_url | F07, P2 |
| (follows from the above) Trackly sends the full page URL, so the email reaches it in every state, including after rejection | personal_data_to_third_party | F05, P1: `email:url_double` in the `dl` parameter |
| `logger.info` writes email, date of birth and the reason for visit | sensitive_category_flow (health in logs) | F02, P1, observed in the server log and traced in `serve.py` |
| Session replay initialised with `maskAllInputs: false` | session_replay_unmasked, with health data | F01, P0 (special category to a processor) |
| The email kept in `localStorage` | insecure_storage | F09, P2 |
| Chatterbox receives the email but isn't declared | undeclared_processor | F06, P1 |
| Decoy: the first-party `sid` session cookie before consent | dismissed | `strictly_necessary` |
| Decoy: a 64-hex cache key in the ad script URL | dismissed | `constant_in_source` (written in `consent.js`) |
| Decoy: Bugcatch with `sendDefaultPii: false` and a scrubber | dismissed | `pii_disabled`; its script load before consent is `functional_no_identifier` |

Trackly's and Adnetic's US regions (from the example list) are recorded as `inventory` for the privacy
team, not as findings: both are declared processors.

```bash
python scripts/validate_privacy_audit.py examples/privacy_audit.example.json --candidates examples/run/candidates.json --capture examples/run/capture.json --canaries examples/run/canaries.json --evidence examples/run --repo examples/clinic-signup
python scripts/render_report.py examples/privacy_audit.example.json
```
