#!/usr/bin/env python3
"""bakery: the performance-budget-auditor example site, served on loopback.

    python serve.py [--port 8142] [--site-version 1.1|1.0]

Static pages from site/, plus three generated assets (made at start-up from a fixed seed, so
nothing large is stored in the repository):

  /img/hero.png        noise, 1000x600 (about 1.8 MB) in 1.1; 400x240 (about 290 KB) in 1.0
  /js/analytics.js     about 400 KB: a fixed workload at load, then functions nothing calls
  /fonts/bakery.woff2  60 KB of filler bytes (not a real font; the download is the point)

HTML pages send Server-Timing. /menu and /menu/breads wait about 450 ms in "db" before answering,
and /fonts/fonts.css about 300 ms. Static assets are cacheable for an hour; HTML is not cached.
"""
from __future__ import annotations
import argparse, random, struct, sys, time, zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITE = Path(__file__).resolve().parent / 'site'
ROUTES = {'/': 'index.html', '/menu': 'menu.html', '/menu/breads': 'menu-breads.html', '/order': 'order.html',
          '/about': 'about.html'}
SLOW_DB = {'/menu': 0.45, '/menu/breads': 0.45}
TYPES = {'.html': 'text/html; charset=utf-8', '.css': 'text/css', '.js': 'text/javascript', '.png': 'image/png',
         '.svg': 'image/svg+xml', '.woff2': 'font/woff2'}


def png(width: int, height: int, seed: int) -> bytes:
    rng = random.Random(seed)
    raw = b''.join(b'\x00' + rng.randbytes(width * 3) for _ in range(height))

    def chunk(typ: bytes, body: bytes) -> bytes:
        return struct.pack('>I', len(body)) + typ + body + struct.pack('>I', zlib.crc32(typ + body) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(raw, 1)) + chunk(b'IEND', b''))


def analytics_js() -> bytes:
    head = ('/* bakery analytics (sample): a fixed workload at load, then code nothing calls */\n'
            '(function () {\n  var x = 0;\n  for (var i = 0; i < 44000000; i++) { x += Math.sqrt(i) % 7; }\n'
            '  window.__bakeryAnalytics = x > 0;\n})();\n')
    rng = random.Random(7)
    body = []
    n = 0
    while len(head) + sum(map(len, body)) < 400_000:
        words = ' + '.join(f'"{rng.choice("abcdefghij") * 6}{rng.randint(0, 99999)}"' for _ in range(12))
        body.append(f'function unused{n}(a, b) {{ var s = {words}; return a ? s.length + b : s; }}\n')
        n += 1
    return (head + ''.join(body)).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = 'bakery'
    assets: dict[str, bytes] = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ROUTES:
            t0 = time.perf_counter()
            time.sleep(SLOW_DB.get(path, 0.0))
            db = (time.perf_counter() - t0) * 1000
            t1 = time.perf_counter()
            data = (SITE / ROUTES[path]).read_bytes()
            render = (time.perf_counter() - t1) * 1000
            return self.reply(200, data, TYPES['.html'], {'Cache-Control': 'no-cache',
                              'Server-Timing': f'db;dur={db:.1f}' + (';desc="menu query"' if path in SLOW_DB else '') +
                                               f', render;dur={render:.1f}'})
        if path in self.assets:
            ext = '.' + path.rsplit('.', 1)[-1]
            return self.reply(200, self.assets[path], TYPES[ext], {'Cache-Control': 'max-age=3600'})
        f = (SITE / path.lstrip('/')).resolve()
        if SITE in f.parents and f.is_file():
            if path == '/fonts/fonts.css':
                time.sleep(0.3)
            return self.reply(200, f.read_bytes(), TYPES.get(f.suffix, 'application/octet-stream'), {'Cache-Control': 'max-age=3600'})
        return self.reply(404, b'not found', 'text/plain', {})

    def reply(self, status: int, data: bytes, ctype: str, headers: dict):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)


class Server(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets a second server bind a port that is already in use, and requests then
    # reach whichever bound first. Refuse instead, so a stale server can never answer for this one.
    allow_reuse_address = sys.platform != 'win32'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8142)
    ap.add_argument('--site-version', choices=['1.1', '1.0'], default='1.1')
    a = ap.parse_args()
    w, h = (1000, 600) if a.site_version == '1.1' else (400, 240)
    Handler.assets = {'/img/hero.png': png(w, h, 42), '/js/analytics.js': analytics_js(),
                      '/fonts/bakery.woff2': random.Random(3).randbytes(60_000)}
    httpd = Server(('127.0.0.1', a.port), Handler)
    print(f'bakery {a.site_version} on http://127.0.0.1:{a.port} (hero {len(Handler.assets["/img/hero.png"])} bytes)', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
