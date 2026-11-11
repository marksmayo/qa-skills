#!/usr/bin/env python3
"""parcel-tracker: the resilience-prober example. A small single-page app and a stub API on loopback.

    python server.py [--port 8147]

The API always answers correctly; every fault in the example run is injected in the browser.
All parcels and addresses are invented. Refuses to start if the port is already in use.
"""
from __future__ import annotations
import argparse, json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITE = Path(__file__).resolve().parent / 'site'
ORDERS = [{'id': 'P1001', 'from': 'Test Books', 'eta': '2026-11-14'}, {'id': 'P1002', 'from': 'Example Outdoor', 'eta': '2026-11-18'}]
STATUS = {'P1001': {'id': 'P1001', 'status': 'In transit', 'last_scan': 'Sorting centre', 'received': False},
          'P1002': {'id': 'P1002', 'status': 'Label created', 'last_scan': None, 'received': False}}
TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css'}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, status: int, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = self.path.split('?')[0]
        if p == '/api/orders':
            return self.reply(200, ORDERS)
        if p.startswith('/api/status/'):
            s = STATUS.get(p.rsplit('/', 1)[-1])
            return self.reply(200, s) if s else self.reply(404, {'error': 'not found'})
        if p == '/api/recommendations':
            return self.reply(200, {'items': [{'title': 'Track by SMS'}, {'title': 'Choose a safe place'}]})
        if p == '/api/notices':
            return self.reply(200, {'notice': 'Deliveries run as normal this week.'})
        f = (SITE / (p.lstrip('/') or 'index.html')).resolve()
        if SITE in f.parents and f.is_file():
            return self.reply(200, f.read_bytes(), TYPES.get(f.suffix, 'application/octet-stream'))
        return self.reply(404, b'not found', 'text/plain')

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(n) or b'{}')
        if self.path == '/api/address':
            return self.reply(200, {'saved': True, 'street': body.get('street')})
        if self.path.startswith('/api/received/'):
            pid = self.path.rsplit('/', 1)[-1]
            STATUS.get(pid, {})['received'] = True
            return self.reply(200, {'id': pid, 'received': True})
        return self.reply(404, {'error': 'not found'})


class Server(ThreadingHTTPServer):
    allow_reuse_address = sys.platform != 'win32'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8147)
    a = ap.parse_args()
    Server(('127.0.0.1', a.port), Handler).serve_forever()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
