"""Access log lines. Reads the forwarding header only to log it, so it is not a lead."""
from flask import request


def log_line():
    via = request.headers.get("X-Forwarded-For", "-")
    return "%s %s via %s" % (request.method, request.path, via)
