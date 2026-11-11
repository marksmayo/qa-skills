#!/usr/bin/env python3
"""Serve the travel site on loopback: python serve.py site --port 8145.

Refuses to start if the port is already in use, so a stale server can't answer instead.
"""
from __future__ import annotations
import argparse, functools, sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Server(ThreadingHTTPServer):
    allow_reuse_address = sys.platform != 'win32'


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('version', choices=['site'])
    ap.add_argument('--port', type=int, default=8145)
    a = ap.parse_args()
    root = Path(__file__).resolve().parent / a.version
    Server(('127.0.0.1', a.port), functools.partial(Quiet, directory=str(root))).serve_forever()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
