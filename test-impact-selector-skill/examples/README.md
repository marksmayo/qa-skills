# Test impact selector example

`harbourline/` is a small monorepo for a harbour ferry operator:

- `api/` is a Python package tested with pytest: pricing, bookings, refunds, a request handler,
  and a loyalty plugin that `plugins/__init__.py` loads with `importlib.import_module(f"...{name}")`.
  The `client` and `family` fixtures are in `api/tests/conftest.py`.
- `web/` is a TypeScript package tested with Vitest. It holds unit tests, an e2e flow test, and a
  visual snapshot test that inlines `checkout.css`. `web/test/setup.ts` is a Vitest `setupFiles`
  entry that registers the receipt styles.
- `pnpm-workspace.yaml` and a real `pnpm-lock.yaml` are at the root.

There's no nested git repository. The changes are diffs that apply to `harbourline/` as it
stands: `select_tests.py` applies each one to a scratch copy.

| Folder | Content |
|---|---|
| `changes/` | The five changes the reports select for |
| `history/` | 12 earlier changes, each with real full-run JUnit from both packages (`history.json` lists them) |
| `journeys/journey_map.json` | A trimmed journey-mapper output: two journeys, their evidence files and linked tests |
| `run/` | The real outputs, step by step in `run/commands.sh` |
| `impact_report.example.json` | The C1 report, with replay, a verify run and a CI snippet |
| `reports/` | The C2 to C5 reports |

`run/` holds:

| File | From |
|---|---|
| `coverage-api.json` | coverage.py 7.17.2 with pytest-cov `--cov-context=test`, `coverage json --show-contexts` |
| `coverage_map.json` | `coverage_map.py normalise` |
| `C1/` to `C5/selection.json` | `select_tests.py` |
| `C1/ci_snippet.yml` | `build_command.py --ci github` |
| `C1/verify/` | `run_tests.py`: the selection and both full suites with C1 applied |
| `replay.json` | `replay.py` over `history/` |

## Answer key

| Change | Result |
|---|---|
| C1: a late-sailing discount in `pricing.py` | 6 of 11 test files, no blind spots. Coverage selects the four test files that run `fare_cents`: two unit and two API. Import selects `test_refund_api.py`, because the handler imports bookings, which imports pricing. The journey map links the web e2e test to `pricing.py`. `test_booking_api.py` also failed in 2 earlier pricing changes (co-failure). |
| C2: a new fixture in `api/tests/conftest.py` | `shared_fixture`. The conftest applies to every API test, so all 6 API test files are selected. |
| C3: a silver tier in the loyalty plugin | `dynamic_reference`: the file is a target of the `import_module` f-string, so the whole api package (6) is selected. Coverage alone links the booking and loyalty API tests. |
| C4: pin Vitest to 3.2.3 | `global_config` twice: `web/package.json` (the web package) and `pnpm-lock.yaml` (the full suite, 11). |
| C5: the pay button colour in `checkout.css` | `non_code`: only the e2e and visual tests that import the stylesheet (2). `checkout.test.ts` imports it too but checks markup only. |
| Replay of 12 changes | 11 of 12 failing test files selected (91.7%, 95% CI 64.6% to 98.5%) |

The one miss is the planted one: H07 rewords the compact receipt footer, and
`web/e2e/checkout.e2e.test.ts` fails. That test reaches `compact.ts` only through the receipt
registry, which `web/test/setup.ts` fills. Nothing links the two:

- no import chain runs from the test to `compact.ts`;
- the web package has no per-test coverage;
- no journey cites `compact.ts`.

`vitest related src/receipts/compact.ts` lists only `compact.test.ts` as well. The report calls
this a `weak_rule` miss and proposes the fix: treat `setupFiles` as imported by every test.

The other replayed changes:

| Change | Failure | How it was selected |
|---|---|---|
| H08 (loyalty plugin) | the loyalty API test | the `dynamic_reference` widening and coverage |
| H10 (`routes.json`) | `test_pricing.py` | the `data_or_schema` widening to the api package |
| H09 (README) | none | a docs-only change selects nothing |
| H11 (a refactor) | none | 5 test files selected, all of which pass |

Every file in `run/` and `history/` is real output. Editing `harbourline/` changes its code hash
and the coverage map's file hashes, which marks the map stale. Regenerate with
`run/commands.sh`.
