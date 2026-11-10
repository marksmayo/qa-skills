#!/usr/bin/env sh
# The runs behind examples/flaky_report.example.json, in order. Run from flaky-test-hunter-skill/.
# Results depend on the machine and on the time of day (the time-zone flake), so a rerun gives
# different counts; validate the report against the runs.json it was built from.
set -e
R="python scripts/run_tests.py --preset pytest --cwd examples/ferry-booking --out examples/run/runs.json"
python scripts/scan_pitfalls.py examples/ferry-booking --out examples/run/smells.json
$R --label baseline --run-prefix R
$R --tests tests/test_refunds.py::test_one_third_refund_rounds_to_nearest_cent tests/test_schedule.py::test_departs_today \
   tests/test_seats.py::test_family_of_four_gets_two_rows --repeat 3 --label confirm_failing --run-prefix F
$R --repeat 20 --label repeat --run-prefix N
$R --order reverse --label order_reverse --run-prefix O
for s in 11 22 33; do $R --order shuffle --seed $s --label order_shuffle --run-prefix S; done
$R --isolate --label isolate --run-prefix I
$R --tests tests/test_seats.py --hash-seed 0 --repeat 3 --label hash_seed_0 --run-prefix H0
$R --tests tests/test_seats.py --hash-seed 1 --repeat 3 --label hash_seed_1 --run-prefix H1
$R --tests tests/test_status_server.py --repeat 5 --label serial_port_tests --run-prefix PS
$R --tests tests/test_status_server.py --workers 2 --repeat 5 --label parallel_2 --run-prefix P
$R --tz UTC --label tz_utc_suite --run-prefix TS
$R --tests tests/test_schedule.py --tz UTC --repeat 3 --label tz_utc --run-prefix TU
$R --tests tests/test_schedule.py --tz Pacific/Kiritimati --repeat 3 --label tz_kiritimati --run-prefix TK
$R --tests tests/test_notifier.py --repeat 5 --label serial_notifier --run-prefix CN
$R --tests tests/test_notifier.py --cpu-load 20 --repeat 5 --label cpu_load_20 --run-prefix C
$R --locale tr_TR.UTF-8 --label locale_tr --run-prefix LT
python scripts/bisect_polluter.py --runs examples/run/runs.json --from-run O-1 \
   --victim tests.test_fares::test_cache_starts_empty --cwd examples/ferry-booking --out examples/run/bisect.json
