import pytest

from harbourline_api.bookings import BookingError, create_booking


def test_booking_totals_the_party():
    booking = create_booking("circular-quay", "manly", [30, 30], 11)
    assert booking["passengers"] == 2
    assert booking["total_cents"] == 1960


def test_empty_party_is_rejected():
    with pytest.raises(BookingError):
        create_booking("circular-quay", "manly", [], 11)


def test_party_of_ten_is_rejected():
    with pytest.raises(BookingError):
        create_booking("circular-quay", "manly", [30] * 10, 11)
