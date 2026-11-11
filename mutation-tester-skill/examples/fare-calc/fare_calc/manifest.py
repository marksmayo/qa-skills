"""Passenger manifests."""


def manifest_lines(passengers: list) -> list:
    """Numbered manifest lines, one per passenger, in boarding order."""
    lines = []
    i = 0
    while i < len(passengers):
        lines.append(f"{i + 1}. {passengers[i]}")
        i += 1
    return lines
