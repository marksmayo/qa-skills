"""A network guard for test runs: refuses every connection that isn't loopback, and logs it (stdlib only).

    QA_NET_GUARD_LOG=<scratch>/net_guard.log python -m pytest -p net_guard ...   # PYTHONPATH must reach this file
    python net_guard.py --self-test

Loaded as a pytest plugin (`-p net_guard`, or `PYTEST_ADDOPTS="-p net_guard"`), it patches
`socket.socket.connect`, `connect_ex` and `socket.create_connection` before any test runs, so
connections to anything other than 127.0.0.0/8, ::1 and localhost raise ConnectionRefusedError.
pytest-xdist workers get the same `-p` option. Other processes the tests start (subprocesses,
browsers, databases) are not guarded.

The log proves the guard was on: its first line for every process is
`net_guard: loaded pid=<pid>`, and every refusal adds `net_guard: refused <host>:<port>`. Before
claiming "zero refused connections", check the log holds a `loaded` line: a guard that never
loaded (a PYTHONPATH that didn't reach it) refuses nothing and logs nothing.

With QA_NET_GUARD_LOG set the lines go to the log only, never to stderr, so tests that assert on
captured stderr are not disturbed by the guard. Without QA_NET_GUARD_LOG, or when the log cannot be
written, they go to stderr. A refusal names the first stack frame outside the standard library and
site-packages (`from <file>:<line>`), so it can be tied to a test.

Go, Rust, JVM and .NET tests can't load this plugin (nor net_guard.cjs). For them use net_guard_proxy.py:

    python net_guard_proxy.py run --log <scratch>/net_guard.log --home <scratch>/home -- go test ./...

It runs the command behind a refusing loopback proxy (HTTP_PROXY, HTTPS_PROXY, ALL_PROXY; NO_PROXY=loopback)
that logs the same `net_guard: loaded ...` and `net_guard: refused <host>:<port>` lines, and with --home gives the
tests an empty HOME/USERPROFILE (and XDG/APPDATA dirs), keeping Go's GOCACHE/GOMODCACHE where they were and
setting GOTOOLCHAIN=local and GOPROXY=off; its summary lists the files tests left in that home. It guards only
clients that honour the proxy variables (Go's default net/http transport does; net.Dial and custom transports do
not), so say so with the result. With run_tests.py: start `net_guard_proxy.py serve --port N --log L` and pass
the pairs `net_guard_proxy.py env --port N --home H` prints to --env.

On Windows, give Windows Python Windows paths: Git Bash converts a single-entry `PYTHONPATH=/c/...`
but not a `;`-separated list, and a path after a prefix such as `--cov-report=json:` is never
converted, so the guard silently doesn't load or output lands in `C:\\c\\...`.
"""
from __future__ import annotations
import ipaddress
import os
import socket
import sys

_LOG = os.environ.get('QA_NET_GUARD_LOG')
_real_connect, _real_connect_ex = socket.socket.connect, socket.socket.connect_ex
_real_create_connection = socket.create_connection
REFUSED: list[str] = []


def _say(line: str) -> None:
    if _LOG:
        try:
            with open(_LOG, 'a', encoding='utf-8') as fh:
                fh.write(line + '\n')
            return
        except OSError:
            pass   # unwritable log: fall back to stderr
    try:
        sys.stderr.write(line + '\n')
    except (OSError, ValueError):
        pass


def _caller() -> str:
    """The first stack frame outside this file, the standard library and site-packages."""
    import sysconfig
    paths = sysconfig.get_paths()
    skip = tuple(os.path.normcase(os.path.abspath(paths[k])) + os.sep
                 for k in ('stdlib', 'platstdlib', 'purelib', 'platlib') if paths.get(k))
    me = os.path.normcase(os.path.abspath(__file__))
    f = sys._getframe(1)
    while f is not None:
        name = f.f_code.co_filename
        path = os.path.normcase(os.path.abspath(name)) if not name.startswith('<') else name
        if path != me and not name.startswith('<frozen') and not path.startswith(skip) \
                and 'site-packages' not in path:
            return f' from {name}:{f.f_lineno}'
        f = f.f_back
    return ''


def _loopback(host) -> bool:
    if host is None:
        return False
    host = str(host).strip('[]').lower()
    if host in ('localhost', 'localhost.localdomain', '') or host.endswith('.localhost'):
        return True
    try:
        return ipaddress.ip_address(host.split('%')[0]).is_loopback
    except ValueError:
        return False


def _check(sock, address) -> None:
    if sock.family not in (socket.AF_INET, socket.AF_INET6) or not isinstance(address, tuple):
        return   # Unix sockets and other families are local
    host, port = address[0], address[1] if len(address) > 1 else '?'
    if not _loopback(host):
        REFUSED.append(f'{host}:{port}')
        _say(f'net_guard: refused {host}:{port}{_caller()}')
        raise ConnectionRefusedError(f'net_guard: connection to {host}:{port} refused (only loopback is allowed)')


def _connect(self, address):
    _check(self, address)
    return _real_connect(self, address)


def _connect_ex(self, address):
    try:
        _check(self, address)
    except ConnectionRefusedError:
        import errno
        return errno.ECONNREFUSED
    return _real_connect_ex(self, address)


def _create_connection(address, *args, **kwargs):
    host, port = address[0], address[1]
    if not _loopback(host):
        REFUSED.append(f'{host}:{port}')
        _say(f'net_guard: refused {host}:{port}{_caller()}')
        raise ConnectionRefusedError(f'net_guard: connection to {host}:{port} refused (only loopback is allowed)')
    return _real_create_connection(address, *args, **kwargs)


socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex
socket.create_connection = _create_connection
_say(f'net_guard: loaded pid={os.getpid()}')


def pytest_report_header(config):
    return f'net_guard: only loopback connections allowed (log: {"set" if _LOG else "stderr only"})'


def _self_test() -> int:
    import subprocess
    import tempfile
    here = os.path.dirname(os.path.abspath(__file__))
    code = ('import socket, net_guard\n'
            'srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)\n'
            'c = socket.create_connection(("127.0.0.1", srv.getsockname()[1])); c.close()\n'
            'try:\n    socket.create_connection(("192.0.2.1", 80), timeout=1)\n    print("OUT")\n'
            'except ConnectionRefusedError:\n    print("REFUSED")\n'
            's = socket.socket()\n'
            'print("EX", s.connect_ex(("198.51.100.7", 443)))\n')
    with tempfile.TemporaryDirectory() as d:
        log = os.path.join(d, 'guard.log')
        env = {**os.environ, 'QA_NET_GUARD_LOG': log, 'PYTHONPATH': here}
        r = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, timeout=60)
        text = open(log, encoding='utf-8').read() if os.path.exists(log) else ''
        bare_env = {k: v for k, v in env.items() if k != 'QA_NET_GUARD_LOG'}
        bare = subprocess.run([sys.executable, '-c', 'import net_guard'], env=bare_env, capture_output=True, text=True, timeout=60)
    checks = [('loopback connects, the rest is refused', 'REFUSED' in r.stdout and 'OUT' not in r.stdout, r.stderr[-300:]),
              ('connect_ex reports refused', 'EX ' in r.stdout and 'EX 0' not in r.stdout, r.stdout),
              ('the log has the loaded line', 'net_guard: loaded pid=' in text, text),
              ('the log names refused hosts', '192.0.2.1:80' in text and '198.51.100.7:443' in text, text),
              ('a refusal names the calling code', 'refused 192.0.2.1:80 from <string>:5' in text, text),
              ('with a log set, stderr stays empty', 'net_guard:' not in r.stderr, r.stderr),
              ('without a log, the loaded line goes to stderr', 'net_guard: loaded pid=' in bare.stderr, bare.stderr)]
    failed = 0
    for name, ok, extra in checks:
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} {name}" + ('' if ok else f'  [{extra.strip()}]'))
    print(f'{failed} failed' if failed else 'ALL PASS')
    return 1 if failed else 0


if __name__ == '__main__' and '--self-test' in sys.argv:
    raise SystemExit(_self_test())
