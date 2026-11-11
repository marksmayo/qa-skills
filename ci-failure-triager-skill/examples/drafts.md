# Drafted defects

From github run 109 on `5a2cbd7d7a47` (main).

## Sample booking TW-1001 in tests/fixtures/bookings.json sails 2026-11-10, now in the past

*test data issue, drafted from CI failure X01; not filed.*

tests.test_booking::test_sample_booking_is_open reads the fixture added in 902084ea and asserts the booking is open. Its sailing date is fixed at 2026-11-10, so the test has failed since that date passed (the run date is 2026-11-12). The product check is_open() behaves as written.

**Steps to reproduce**

1. git checkout 5a2cbd7d7a47
2. python -m pytest tests/test_booking.py::test_sample_booking_is_open

**Expected:** The sample booking is open (a sailing date after the run date)

**Actual:** AssertionError: sample booking TW-1001 sails 2026-11-10, which is in the past

**Suspect commit:** `902084eac0c8` Add sample bookings fixture (shares tests/fixtures/bookings.json, tests/test_booking.py)

**Evidence**

- logs/5101.txt lines 26-31 (attempt 1)
- Re-run attempt 2: failed
- https://github.com/example-org/tidewater/actions/runs/109

## Half-price fare of 24.25 prints 12.12 instead of 12.13 after the rounding change

*defect, drafted from CI failure X02; not filed.*

Since "Use banker's rounding for fares" (613509c8), fare() rounds half to even. A half-price fare of 24.25 is 12.125, which tickets print as 12.13 (half up); fare() now returns 12.12. tests.test_fares::test_fare_rounds_half_up fails on main in run 109, in both attempts.

**Steps to reproduce**

1. git checkout 5a2cbd7d7a47
2. python -m pytest tests/test_fares.py::test_fare_rounds_half_up

**Expected:** fare(Decimal('24.25'), discount_pct=50) == Decimal('12.13')

**Actual:** Decimal('12.12')

**Suspect commit:** `613509c84ff9` Use banker's rounding for fares (shares tidewater/fares.py)

**Evidence**

- logs/5101.txt lines 32-38 (attempt 1)
- Re-run attempt 2: failed
- https://github.com/example-org/tidewater/actions/runs/109
