#!/usr/bin/env python3
"""Harbour Tickets: a small ferry-ticket site and API standing in for production (stdlib, loopback only).

    python server.py --release 1.8.0 [--port 8160]
    python server.py --release 1.9.0 [--port 8160]

Release 1.9.0 has planted problems for the post-deploy validator's example:
  - GET /timetable answers 500 (a template error);
  - GET /api/fares returns a negative price for the child fare, and is about 300 ms slower;
  - GET /about is about 30 ms slower (within tolerance: a decoy).
Signing in: POST /api/login with {"username", "password"} returns a bearer token. The only account is
"smoke-reader" with the obviously fake password "not-a-real-password". GET /api/account needs the token.
POST /api/bookings would create a booking (the validator must never send it).
Everything is invented; nothing is stored.
"""
from __future__ import annotations
import argparse, json, secrets, sys, time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USER, PASSWORD = 'smoke-reader', 'not-a-real-password'
TOKENS: set[str] = set()
REL = {'release': '1.8.0'}


def page(title: str, body: str) -> bytes:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="app-version" content="{REL["release"]}">'
            f'<title>{title} - Harbour Tickets</title></head><body><h1>{title}</h1>{body}</body></html>').encode()


def sailings() -> list[dict]:
    today = datetime.now(timezone.utc).date()
    return [{'id': f'HT-{i}', 'route': r, 'departs': f'{today + timedelta(days=d)}T{h}:00:00Z', 'seats_left': s}
            for i, (r, d, h, s) in enumerate([('Harbour - Northside', 0, '08', 41), ('Harbour - Northside', 0, '12', 12),
                                             ('Northside - Harbour', 1, '09', 80), ('Harbour - Island', 1, '15', 0)], 1)]


class H(BaseHTTPRequestHandler):
    server_version = 'HarbourTickets'
    sys_version = ''

    def log_message(self, *a):
        pass

    def send(self, code: int, body: bytes, ctype: str = 'text/html; charset=utf-8', extra: dict | None = None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-App-Version', REL['release'])
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def json(self, code: int, obj, extra=None):
        self.send(code, json.dumps(obj).encode(), 'application/json', extra)

    def authed(self) -> bool:
        h = self.headers.get('Authorization', '')
        return h.startswith('Bearer ') and h[7:] in TOKENS

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        new = REL['release'] == '1.9.0'
        p = self.path.split('?')[0]
        if p == '/version':
            return self.json(200, {'version': REL['release'], 'commit': 'a1b2c3d' if not new else 'e4f5a6b'})
        if p == '/':
            return self.send(200, page('Harbour Tickets', '<p>Book a ferry between Harbour, Northside and the Island.</p><a href="/timetable">Timetable</a>'))
        if p == '/timetable':
            if new:
                return self.send(500, page('Something went wrong', '<p>Error 500. We are looking into it.</p>'))
            return self.send(200, page('Timetable', ''.join(f'<p>{s["route"]} {s["departs"]}</p>' for s in sailings())))
        if p == '/about':
            time.sleep(0.03 if new else 0)
            return self.send(200, page('About us', '<p>Family-run ferries since 1962.</p>'))
        if p == '/api/sailings':
            return self.json(200, {'sailings': sailings(), 'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})
        if p == '/api/fares':
            time.sleep(0.32 if new else 0.02)
            fares = [{'type': 'adult', 'price': 24.50}, {'type': 'child', 'price': -12.25 if new else 12.25}, {'type': 'concession', 'price': 18.00}]
            return self.json(200, {'currency': 'AUD', 'fares': fares})
        if p == '/api/account':
            if not self.authed():
                return self.json(401, {'error': 'sign in required'})
            return self.json(200, {'username': USER, 'display_name': 'Smoke Reader', 'email': 'smoke.reader@example.test', 'bookings': 0})
        return self.send(404, page('Not found', '<p>No such page.</p>'))

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        data = self.rfile.read(n) if n else b'{}'
        if self.path == '/api/login':
            try:
                b = json.loads(data)
            except ValueError:
                return self.json(400, {'error': 'bad json'})
            if b.get('username') == USER and b.get('password') == PASSWORD:
                tok = secrets.token_hex(16)
                TOKENS.add(tok)
                return self.json(200, {'token': tok, 'expires_in': 900}, {'Set-Cookie': f'ht_session={tok}; HttpOnly; Path=/'})
            return self.json(401, {'error': 'wrong username or password'})
        if self.path == '/api/bookings':
            return self.json(201, {'booking': 'HT-B-1', 'note': 'a booking was created'})
        return self.json(404, {'error': 'not found'})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--release', choices=['1.8.0', '1.9.0'], required=True)
    ap.add_argument('--port', type=int, default=8160)
    a = ap.parse_args()
    REL['release'] = a.release
    ThreadingHTTPServer.allow_reuse_address = sys.platform != 'win32'
    srv = ThreadingHTTPServer(('127.0.0.1', a.port), H)
    print(f'Harbour Tickets {a.release} on http://127.0.0.1:{a.port}', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
