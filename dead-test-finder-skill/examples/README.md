# Dead test finder example

`ledger/` is a small bookkeeping app:

- `core/` is a Python package tested with pytest: journal entries, balances, GST and reports.
- `client/` is a TypeScript client tested with Vitest.

`tickets/LEDG-12.issue.json` is an exported Jira issue, normalised with `fetch_ticket.py`.
`proposals/` holds one patch per finding.

`run/` holds the real run, step by step in `run/commands.sh`:

| File | From |
|---|---|
| `candidates.json` | `scan_tests.py`: 15 test definitions, 8 candidates |
| `collection.json`, `collect-core.txt`, `collect-client.json` | `collect_check.py`: pytest collects 11 tests, Vitest lists 3 |
| `runs.json`, `junit/` | every neutralisation and proof run (`run_tests.py`); the scratch client path is shown as `<client>` |
| `neutralisations.json` | `neutralise.py`: 4 flagged tests, then 5 patched tests |
| `similarity.json` | `similarity.py`: the GST pair |
| `tickets.json`, `skips.json` | `fetch_ticket.py`, `check_skips.py` |

## Answer key

| Planted | Result |
|---|---|
| `check_balance.py`, outside pytest's `python_files` | F1 `never_runs`, confirmed by the collect output, P0 (money); the rename is proved |
| `test_post_entry_records_a_sale`, which asserts nothing | F2 `cant_fail`, confirmed: it passed with `post_entry()` returning `None` and failed only when it raised; P0; the assertion fix is proved |
| A tax test that patches `calculate_tax` and asserts on it | F3 `mock_only`, confirmed: it passed with `calculate_tax` neutralised both ways; P1 |
| `expect(save(...)).resolves.toBe(true)` with no `await` | F4 `async_not_awaited`, static. Neutralised, the test **failed**: Vitest 3.2.4 auto-awaits the hanging assertion (and warns). So it can fail today; the missing `await` is still the finding, P2, and the fix is proved |
| `@pytest.mark.skip(reason="LEDG-12 ...")` where LEDG-12 is Done | F5 `stale_skip`; the un-skipped test passes and is proved to catch a broken `trial_balance()` |
| Two GST tests with renamed variables | F6 `duplicate`: 100% similar, the same 10 product lines, the same values; merge proposed, P3 |
| `test_package_imports` | F7 `weak`, P3: nothing to neutralise; deletion proposed |
| A Windows-only `skipif` with a reason | dismissed as legitimate |

The un-awaited assertion is the instructive case. The rule flags it, but the neutralisation run
shows this version of Vitest still checks it, so the finding stays `static` at P2 instead of
becoming a confirmed can't-fail.

Skip ages are `not_checked`, because the example's lines weren't committed when the run was
made. Once they are committed, `git blame` would give their age in this repository.

Everything in `run/` is real output. Editing `ledger/` changes the candidates' lines and
invalidates the example until the run is repeated with `commands.sh`. That needs a scratch copy
of `ledger/client` with `npm install` done.
