#!/usr/bin/env python3
"""A network guard for runtimes net_guard.py cannot load into (Go, Rust, Java, .NET...): a refusing loopback proxy
plus an isolated home directory (stdlib only).

    python net_guard_proxy.py run --log <scratch>/net_guard.log --home <scratch>/home [--summary s.json] -- go test ./...
    python net_guard_proxy.py serve --port 18080 --log <scratch>/net_guard.log      # until stopped (Ctrl-C)
    python net_guard_proxy.py env --port 18080 --home <scratch>/home               # K=V pairs for run_tests.py --env
    python net_guard_proxy.py --self-test

net_guard.py (pytest) and net_guard.cjs (Node) patch sockets inside the test process. A Go, Rust or JVM test
binary can't load them, so this script works from outside: it starts an HTTP proxy on 127.0.0.1 that refuses
every request and CONNECT tunnel with 403 and logs `net_guard: refused <host>:<port> (<method>)`, and runs the
command with HTTP_PROXY, HTTPS_PROXY and ALL_PROXY (both cases) pointing at it and NO_PROXY set to loopback.
Its log starts with `net_guard: loaded proxy pid=<pid> port=<port>`, as net_guard.py's does.

Limits, which a report must state: only clients that honour the proxy variables are guarded. Go's net/http
default transport and http.ProxyFromEnvironment, curl, Python's urllib/requests and most JVM and .NET HTTP
clients do; a raw net.Dial / TCP socket, a client built with its own Transport (Proxy: nil), DNS lookups, and
UDP are NOT guarded, and nothing is logged for them. So "zero refused" means no proxy-honouring client tried to
leave, not that the tests made no network calls. Grep the tests for net.Dial / DialContext / custom transports
before claiming isolation.

--home DIR gives the command an empty home: HOME, USERPROFILE, XDG_CONFIG_HOME, XDG_CACHE_HOME, XDG_DATA_HOME,
XDG_STATE_HOME, APPDATA and LOCALAPPDATA all point inside DIR, so tests that write to ~/.something (caches,
settings, downloaded browsers or SDKs, installed skills) write there instead of into the real home, and the
summary lists every file they left in it (`home_writes`). Go keeps its build and module caches in the home
directory by default; when `go` is on PATH their current locations (GOCACHE, GOMODCACHE, GOPATH) are passed on
explicitly, so a moved home does not force a rebuild or a module download. GOTOOLCHAIN=local and GOPROXY=off
are also set (no toolchain or module downloads during the run; modules must already be in the cache), and
GOFLAGS is left as it was. Pass --keep-network-env to leave GOPROXY alone.

The recipe without this script (no log, no attribution): HTTP_PROXY=HTTPS_PROXY=http://127.0.0.1:1 (a closed
port), NO_PROXY=localhost,127.0.0.1,::1, HOME and USERPROFILE set to a scratch directory, GOCACHE and GOMODCACHE
set to the values `go env` printed before HOME changed, GOTOOLCHAIN=local, GOPROXY=off.
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

LOOPBACK = 'localhost,127.0.0.1,::1'
TOOLCHAIN_WRITES = re.compile(r'(^|/)go/telemetry/')


class Guard:
    """The refusing proxy: a server thread, its port, the log and the refusals so far."""

    def __init__(self, log: str | None, port: int = 0):
        self.log, self.refused, self.lock = log, [], threading.Lock()
        guard = self

        class Handler(BaseHTTPRequestHandler):
            def refuse(self):
                if self.command == 'CONNECT':
                    host, _, port = self.path.rpartition(':')
                else:
                    u = urlsplit(self.path)
                    host, port = u.hostname or self.headers.get('Host', '?'), u.port or (443 if u.scheme == 'https' else 80)
                guard.say(f'net_guard: refused {host}:{port} ({self.command})')
                with guard.lock:
                    guard.refused.append(f'{host}:{port}')
                body = b'net_guard_proxy: outbound network refused during a test run\n'
                self.send_response(403, 'Forbidden by net_guard_proxy')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Connection', 'close')
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_CONNECT = refuse

            def log_message(self, *a):   # quiet: the log file holds what matters
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def say(self, line: str) -> None:
        if self.log:
            try:
                with open(self.log, 'a', encoding='utf-8') as fh:
                    fh.write(line + '\n')
                return
            except OSError:
                pass
        print(line, file=sys.stderr)

    def start(self) -> 'Guard':
        self.say(f'net_guard: loaded proxy pid={os.getpid()} port={self.port}')
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def go_caches() -> dict[str, str]:
    """GOCACHE, GOMODCACHE and GOPATH as go sees them now (before HOME moves), or {} without go."""
    go = shutil.which('go')
    if not go:
        return {}
    try:
        res = subprocess.run([go, 'env', 'GOCACHE', 'GOMODCACHE', 'GOPATH'], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return {}
    vals = res.stdout.splitlines()
    return {k: v for k, v in zip(('GOCACHE', 'GOMODCACHE', 'GOPATH'), vals) if v.strip()} if res.returncode == 0 else {}


def env_pairs(port: int, home: str | None, go: bool = True, keep_network_env: bool = False) -> list[str]:
    """K=V pairs that route proxy-honouring clients to the guard and (with home) isolate the home directory."""
    url = f'http://127.0.0.1:{port}'
    pairs = [f'{k}={url}' for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')]
    pairs += [f'NO_PROXY={LOOPBACK}', f'no_proxy={LOOPBACK}']
    if home:
        h = str(Path(home).resolve())
        pairs += [f'HOME={h}', f'USERPROFILE={h}', f'XDG_CONFIG_HOME={h}/.config', f'XDG_CACHE_HOME={h}/.cache',
                  f'XDG_DATA_HOME={h}/.local/share', f'XDG_STATE_HOME={h}/.local/state',
                  f'APPDATA={h}/AppData/Roaming', f'LOCALAPPDATA={h}/AppData/Local']
    if go:
        pairs += [f'{k}={v}' for k, v in go_caches().items()] + ['GOTOOLCHAIN=local']
        if not keep_network_env:
            pairs.append('GOPROXY=off')
    return pairs


def files_under(d: Path) -> set[str]:
    out = set()
    for dp, _, fns in os.walk(d):
        for f in fns:
            out.add((Path(dp) / f).relative_to(d).as_posix())
    return out


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if '--self-test' in argv:
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='mode', required=True)
    r = sub.add_parser('run', help='start the proxy, run the command under it, stop')
    r.add_argument('--log', help='log file (default: stderr)')
    r.add_argument('--home', help='an empty scratch directory to use as the home directory')
    r.add_argument('--summary', help='write {refused, refused_hosts, home_writes, exit_code} here as JSON')
    r.add_argument('--keep-network-env', action='store_true', help='leave GOPROXY as it is')
    r.add_argument('command', nargs=argparse.REMAINDER)
    s = sub.add_parser('serve', help='run the proxy until stopped')
    s.add_argument('--port', type=int, default=0)
    s.add_argument('--log')
    e = sub.add_parser('env', help='print K=V pairs for a proxy started with serve')
    e.add_argument('--port', type=int, required=True)
    e.add_argument('--home')
    e.add_argument('--keep-network-env', action='store_true')
    args = ap.parse_args(argv)
    if args.mode == 'env':
        print('\n'.join(env_pairs(args.port, args.home, keep_network_env=args.keep_network_env)))
        return 0
    if args.mode == 'serve':
        g = Guard(args.log, args.port).start()
        print(f'net_guard_proxy listening on 127.0.0.1:{g.port}; stop with Ctrl-C', file=sys.stderr)
        try:
            g.thread.join()
        except KeyboardInterrupt:
            g.stop()
        return 0
    cmd = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not cmd:
        ap.error('run needs a command after --')
    home = Path(args.home).resolve() if args.home else None
    if home:
        home.mkdir(parents=True, exist_ok=True)
        if any(home.iterdir()):
            ap.error(f'--home {home}: not empty; give an empty scratch directory so the files tests write can be listed')
    g = Guard(args.log).start()
    env = dict(os.environ)
    for kv in env_pairs(g.port, str(home) if home else None, go=True, keep_network_env=args.keep_network_env):
        k, _, v = kv.partition('=')
        env[k] = v
    try:
        code = subprocess.run(cmd, env=env).returncode
    finally:
        g.stop()
    every = sorted(files_under(home)) if home else []
    toolchain = [w for w in every if TOOLCHAIN_WRITES.search(w)]   # the go command's own local counters, not the tests
    writes = [w for w in every if w not in toolchain]
    hosts = sorted(set(g.refused))
    print(f'net_guard_proxy: {len(g.refused)} refused request(s)' + (f' to {", ".join(hosts[:8])}' if hosts else '')
          + (f'; {len(writes)} file(s) written under the isolated home' if home else ''), file=sys.stderr)
    if args.summary:
        Path(args.summary).write_text(json.dumps({'refused': len(g.refused), 'refused_hosts': hosts,
                                                  'home_writes': writes, 'toolchain_writes': toolchain,
                                                  'exit_code': code}, indent=2) + '\n',
                                      encoding='utf-8')
    return code


CHILD = r'''
import os, pathlib, sys, urllib.request
out = []
for url in ("http://api.example.test/v1/ping", "https://updates.example.test/latest"):
    try:
        urllib.request.urlopen(url, timeout=5)
        out.append("reached " + url)
    except Exception as e:
        out.append("blocked " + url + " " + type(e).__name__)
p = pathlib.Path.home() / ".kiosk" / "state.json"
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text("{}")
print("\n".join(out))
print("HOME_IS", pathlib.Path.home())
'''


def self_test() -> int:
    import tempfile
    from pathlib import Path
    checks = []
    with tempfile.TemporaryDirectory() as d:
        log, home = Path(d) / 'net_guard.log', Path(d) / 'home'
        script = Path(d) / 'child.py'
        script.write_text(CHILD, encoding='utf-8')
        real_home = Path.home()
        code = main(['run', '--log', str(log), '--home', str(home), '--summary', str(Path(d) / 'summary.json'), '--',
                     sys.executable, str(script)])
        text = log.read_text(encoding='utf-8') if log.exists() else ''
        checks.append(('the log proves the proxy was on (a loaded line)', 'net_guard: loaded proxy' in text, text[:300]))
        checks.append(('a plain HTTP request to another host is refused and logged',
                       'refused api.example.test:80' in text, text[:300]))
        checks.append(('an HTTPS CONNECT to another host is refused and logged',
                       'refused updates.example.test:443' in text, text[:300]))
        checks.append(('the child ran with the isolated home, and the real home was not written',
                       (home / '.kiosk' / 'state.json').exists() and not (real_home / '.kiosk' / 'state.json').exists(),
                       sorted(str(p) for p in home.rglob('*')) if home.exists() else 'no home'))
        import json as _json
        summ = _json.loads((Path(d) / 'summary.json').read_text(encoding='utf-8')) if (Path(d) / 'summary.json').exists() else {}
        checks.append(('the summary counts refusals and names files the run wrote under the isolated home',
                       summ.get('refused') == 2 and '.kiosk/state.json' in summ.get('home_writes', []) and code == 0,
                       (code, summ)))
        env = dict(line.split('=', 1) for line in env_pairs(18080, str(home), go=False))
        checks.append(('env gives proxy, no-proxy and home variables for run_tests.py --env',
                       env.get('HTTPS_PROXY') == 'http://127.0.0.1:18080' and '127.0.0.1' in env.get('NO_PROXY', '')
                       and env.get('HOME') == str(home) and env.get('USERPROFILE') == str(home), env))
    failed = 0
    for name, ok, extra in checks:
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} {name}" + ('' if ok else f'  [{extra}]'))
    print(f'{failed} failed' if failed else 'ALL PASS')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
