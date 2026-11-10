"""Seat allocation for a group booking."""


def allocate(passengers: int, row_size: int = 2) -> set[str]:
    """Seat codes for a group, filling rows A, B, C... from seat 1."""
    seats = set()
    for i in range(passengers):
        row = chr(ord("A") + i // row_size)
        seats.add(f"{row}{i % row_size + 1}")
    return seats
