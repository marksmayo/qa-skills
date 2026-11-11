from . import plugins
from .bookings import BookingError, create_booking
from .refunds import refund_cents


def handle(request):
    """A tiny request handler: {"path": ..., "body": {...}} -> (status, body)."""
    path, body = request["path"], request.get("body", {})
    if path == "/bookings":
        try:
            booking = create_booking(body["origin"], body["destination"], body["ages"], body["hour"])
        except BookingError as exc:
            return 422, {"error": str(exc)}
        except KeyError as exc:
            return 404, {"error": str(exc)}
        return 201, plugins.apply_all(booking, body.get("customer", {}))
    if path == "/refunds/quote":
        return 200, {"refund_cents": refund_cents(body["total_cents"], body["hours_before"])}
    return 404, {"error": f"no handler for {path}"}
