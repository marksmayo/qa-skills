import time

from ferry_booking.notifier import Notifier


def test_confirmation_is_sent():
    n = Notifier()
    n.send_async("FB-1042")
    time.sleep(0.05)
    assert n.sent and n.sent[0].startswith("Booking FB-1042 confirmed")


def test_confirmation_after_flush():
    n = Notifier()
    n.send_async("FB-1043")
    time.sleep(0.5)
    n.flush()
    assert len(n.sent) == 1
