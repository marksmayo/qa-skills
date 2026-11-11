def test_waitlist_starts_empty(waitlist):
    assert waitlist == []


def test_join_waitlist(waitlist):
    waitlist.append(1)
    assert waitlist == [1]
