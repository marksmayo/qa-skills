"""A tiny health endpoint the wharf kiosks poll."""
import socketserver
import threading
from http.server import BaseHTTPRequestHandler


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class StatusServer(socketserver.TCPServer):
    allow_reuse_address = False  # one server per port


def start(port: int) -> StatusServer:
    srv = StatusServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
