#!/usr/bin/env bash
# The real run behind dead_test_report.example.json, from the repository root (Windows 11, Python 3.14
# with pytest, Node 26 with Vitest 3.2.4). examples/ledger is never changed: Python runs use temporary
# copies and the client runs use CLIENT, a scratch copy of ledger/client with node_modules installed.
set -euo pipefail
SKILL=dead-test-finder-skill
EX=$SKILL/examples
RUN=$EX/run
P=$EX/proposals
CLIENT=${CLIENT:?set CLIENT to a scratch copy of examples/ledger/client with npm install done}
cp -r $EX/ledger/client/src/. "$CLIENT/src/"
rm -rf $RUN/runs.json $RUN/junit $RUN/neutralisations.json

python $SKILL/scripts/scan_tests.py $EX/ledger --out $RUN/candidates.json
python $SKILL/scripts/collect_check.py $EX/ledger --candidates $RUN/candidates.json --run-dir $RUN --cwd "client=$CLIENT"

# Neutralise the flagged tests that call product code.
NE="python $SKILL/scripts/neutralise.py --repo $EX/ledger --candidates $RUN/candidates.json --run-dir $RUN --cwd client=$CLIENT"
$NE --test "core/tests/test_entries.py::test_post_entry_records_a_sale" --test "core/tests/test_tax.py::test_tax_is_calculated" \
    --test "client/src/api.test.ts::save > saves a valid entry" --test "core/tests/test_imports.py::test_package_imports"

# Prove each strengthening proposal: the patched test passes on the code and fails neutralised.
$NE --patch $P/assert-post-entry.diff --test "core/tests/test_entries.py::test_post_entry_records_a_sale"
$NE --patch $P/await-save.diff --test "client/src/api.test.ts::save > saves a valid entry"
$NE --patch $P/unmock-calculate-tax.diff --test "core/tests/test_tax.py::test_tax_is_calculated"
$NE --patch $P/rename-check-balance.diff --test "core/tests/test_balance.py::test_refund_reduces_the_balance" \
    --target "core/tests/test_balance.py::test_refund_reduces_the_balance=ledger.entries.balance"
$NE --patch $P/unskip-ledg-12.diff --test "core/tests/test_reports.py::test_trial_balance_with_two_currencies" \
    --target "core/tests/test_reports.py::test_trial_balance_with_two_currencies=ledger.reports.trial_balance"

python $SKILL/scripts/similarity.py --repo $EX/ledger --candidates $RUN/candidates.json --run-dir $RUN
python $SKILL/scripts/fetch_ticket.py --from-file $EX/tickets/LEDG-12.issue.json --source connector --out $RUN/tickets.json
python $SKILL/scripts/check_skips.py --repo $EX/ledger --candidates $RUN/candidates.json --tickets $RUN/tickets.json \
    --today 2026-11-11 --out $RUN/skips.json

# The report adds the review decisions; the computed fields are then filled in and checked.
python $SKILL/scripts/validate_dead_test_report.py $EX/dead_test_report.example.json --repo $EX/ledger --write-summary

# The JUnit files and runs.json keep failure traces; the scratch client path in them is replaced with
# <client>, and hostnames with "host".
python - "$CLIENT" "$RUN" <<'PY'
import re, sys
from pathlib import Path
client, run = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
bs = chr(92)
forms = {str(client), client.as_posix(), str(client).replace(bs, bs * 2)}
home = str(Path.home())
forms |= {f.replace(home, '~').replace(home.replace(bs, '/'), '~') for f in list(forms)}
for p in [run / 'runs.json', *sorted((run / 'junit').glob('*.xml'))]:
    text = p.read_text(encoding='utf-8')
    for f in sorted(forms, key=len, reverse=True):
        text = text.replace(f, '<client>')
    text = re.sub(r'hostname="[^"]*"', 'hostname="host"', text)
    p.write_text(text, encoding='utf-8', newline='\n')
PY
python $SKILL/scripts/validate_dead_test_report.py $EX/dead_test_report.example.json --repo $EX/ledger
