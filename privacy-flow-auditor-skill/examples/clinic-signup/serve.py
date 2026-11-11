#!/usr/bin/env python3
"""Serve the clinic-signup app (port 8152) and the stub vendors (port 8153) on loopback.

    python serve.py [--log server.log]

The app writes its log to --log (default: stderr). The vendor stubs answer every request and keep nothing.
"""
from __future__ import annotations
import argparse, logging, mimetypes, secrets, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).resolve().parent
logger = logging.getLogger('clinic')
PATIENTS = []
GIF = bytes.fromhex('47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b')


class Server(ThreadingHTTPServer):
    allow_reuse_address = sys.platform != 'win32'
    daemon_threads = True


class App(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_file(self, rel: str, extra: dict | None = None):
        path = ROOT / 'site' / rel
        if not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/':
            # strictly necessary: the session cookie for the sign-up form
            cookie = f'sid={secrets.token_hex(16)}; HttpOnly; SameSite=Lax; Path=/'
            return self.send_file('index.html', {'Set-Cookie': cookie})
        if path in ('/signup', '/welcome'):
            return self.send_file(path[1:] + '.html')
        if path.startswith('/static/'):
            return self.send_file(path[1:])
        self.send_error(404)

    def do_POST(self):
        if urlparse(self.path).path != '/signup':
            return self.send_error(404)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers.get('Content-Length', 0))).decode()).items()}
        patient = {'name': form.get('name', ''), 'email': form.get('email', ''), 'phone': form.get('phone', ''),
                   'dob': form.get('dob', ''), 'reason': form.get('reason', '')}
        PATIENTS.append(patient)
        logger.info('new patient signup email=%s dob=%s reason=%s', patient['email'], patient['dob'], patient['reason'])
        self.send_response(303)
        self.send_header('Location', '/welcome?email=' + quote(patient['email']))
        self.end_headers()


class Vendors(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def answer(self):
        host = (self.headers.get('Host') or '').split(':')[0]
        path = urlparse(self.path).path
        if path.endswith('.js'):
            name = host.split('.')[-2]
            body = (ROOT / 'stub-vendors' / f'{name}.js').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'application/javascript')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.command == 'POST':
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', 'image/gif')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(GIF)))
        self.end_headers()
        self.wfile.write(GIF)

    do_GET = do_POST = answer

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'content-type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST')
        self.end_headers()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--log')
    ap.add_argument('--port', type=int, default=8152)
    ap.add_argument('--vendor-port', type=int, default=8153)
    a = ap.parse_args()
    handler = logging.FileHandler(a.log, encoding='utf-8') if a.log else logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    app, vendors = Server(('127.0.0.1', a.port), App), Server(('127.0.0.1', a.vendor_port), Vendors)
    threading.Thread(target=vendors.serve_forever, daemon=True).start()
    print(f'app on http://127.0.0.1:{a.port}/, vendors on 127.0.0.1:{a.vendor_port}', flush=True)
    try:
        app.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
