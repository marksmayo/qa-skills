"""Booking confirmations are sent from a background thread."""
import hashlib
import threading


class Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self._threads: list[threading.Thread] = []

    def _render(self, booking_ref: str) -> str:
        # Rendering the confirmation takes a few tens of milliseconds of CPU.
        digest = booking_ref.encode()
        for _ in range(60_000):
            digest = hashlib.sha256(digest).digest()
        return f"Booking {booking_ref} confirmed ({digest.hex()[:8]})"

    def send_async(self, booking_ref: str) -> threading.Thread:
        t = threading.Thread(target=lambda: self.sent.append(self._render(booking_ref)))
        t.start()
        self._threads.append(t)
        return t

    def flush(self, timeout: float = 5.0) -> None:
        for t in self._threads:
            t.join(timeout)
