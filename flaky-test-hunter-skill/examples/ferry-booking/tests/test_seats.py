from ferry_booking.seats import allocate


def test_family_of_four_gets_two_rows():
    assert list(allocate(4)) == ["A1", "A2", "B1", "B2"]


def test_seat_count_matches_passengers():
    assert len(allocate(5)) == 5
