"""A network guard for test runs: refuses every connection that isn't loopback, and logs it (stdlib only).

    QA_NET_GUARD_LOG=<scratch>/net_guard.log python -m pytest -p net_guard ...   # PYTHONPATH must reach this file
    python net_guard.py --self-test

Loaded as a pytest plugin (`-p net_guard`, or `PYTEST_ADDOPTS="-p net_guard"`), it patches
`socket.socket.connect`, `connect_ex`, `sendto`, `sendmsg` and `socket.create_connection` before any test
runs, so connections and UDP datagrams to anything other than 127.0.0.0/8, ::1 and localhost raise
ConnectionRefusedError. The C type `_socket.socket` is replaced by a guarded subclass, so code that builds
the C socket directly is guarded too, and on Windows the asyncio Proactor loop's own connect (ConnectEx) and
sendto (WSASendTo) are guarded, since they never call socket.connect. Not covered: ctypes or other native
code calling the OS directly.
DNS is guarded too: `socket.getaddrinfo`, `gethostbyname` and `gethostbyname_ex` refuse any name that
is not loopback (localhost, *.localhost) or an IP literal with socket.gaierror before the lookup
reaches the resolver, and log `net_guard: refused lookup <name>:<port>`, so the log names the host
the client asked for rather than an address. pytest-xdist workers get the same `-p` option. Other processes the tests start (subprocesses,
browsers, databases) are not guarded.

The log proves the guard was on: its first line for every process is
`net_guard: loaded pid=<pid>`, and every refusal adds `net_guard: refused <host>:<port>`. Before
claiming "zero refused connections", check the log holds a `loaded` line: a guard that never
loaded (a PYTHONPATH that didn't reach it) refuses nothing and logs nothing.

With QA_NET_GUARD_LOG set the lines go to the log only, never to stderr, so tests that assert on
captured stderr are not disturbed by the guard. Without QA_NET_GUARD_LOG, or when the log cannot be
written, they go to stderr. A refusal names the first stack frame outside the standard library and
site-packages (`from <file>:<line>`) and, inside pytest, the running test (`test=<node id>`, from
PYTEST_CURRENT_TEST), so it can be tied to a test even when a shared helper made the call.

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
_real_getaddrinfo, _real_gethostbyname = socket.getaddrinfo, socket.gethostbyname
_real_gethostbyname_ex = socket.gethostbyname_ex
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


def _test_id() -> str:
    """The running pytest test (PYTEST_CURRENT_TEST, set by pytest for each phase), so a refusal from a helper shared
    by many parametrised tests can still be tied to one."""
    cur = os.environ.get('PYTEST_CURRENT_TEST', '')
    return f' test={cur.rsplit(" (", 1)[0]}' if cur else ''


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
        _say(f'net_guard: refused {host}:{port}{_caller()}{_test_id()}')
        raise ConnectionRefusedError(f'net_guard: connection to {host}:{port} refused (only loopback is allowed)')


def _is_ip(host) -> bool:
    try:
        ipaddress.ip_address(str(host).strip('[]').split('%')[0])
        return True
    except ValueError:
        return False


def _check_name(host, port='?') -> None:
    """Refuse a DNS lookup of a name that is neither loopback nor an IP literal, before it reaches the resolver."""
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode('ascii', 'replace')
    if host is None or _loopback(host) or _is_ip(host):
        return
    REFUSED.append(f'{host}:{port}')
    _say(f'net_guard: refused lookup {host}:{port}{_caller()}{_test_id()}')
    raise socket.gaierror(getattr(socket, 'EAI_NONAME', -2),
                          f'net_guard: lookup of {host} refused (only loopback is allowed)')


def _getaddrinfo(host, port, *args, **kwargs):
    _check_name(host, port)
    return _real_getaddrinfo(host, port, *args, **kwargs)


def _gethostbyname(host):
    _check_name(host)
    return _real_gethostbyname(host)


def _gethostbyname_ex(host):
    _check_name(host)
    return _real_gethostbyname_ex(host)


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
        _say(f'net_guard: refused {host}:{port}{_caller()}{_test_id()}')
        raise ConnectionRefusedError(f'net_guard: connection to {host}:{port} refused (only loopback is allowed)')
    return _real_create_connection(address, *args, **kwargs)


def _send_address(args):
    """The destination of sendto(data, [flags,] address) / sendmsg(buffers, ancdata, flags, address), if given."""
    return args[-1] if args and isinstance(args[-1], tuple) else None


def _sendto(self, data, *args):
    _check(self, _send_address(args))
    return _real_sendto(self, data, *args)


def _sendmsg(self, buffers, *args):
    if len(args) >= 3:
        _check(self, args[2])
    return _real_sendmsg(self, buffers, *args)


_real_sendto = socket.socket.sendto
_real_sendmsg = getattr(socket.socket, 'sendmsg', None)
socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex
socket.socket.sendto = _sendto
if _real_sendmsg is not None:
    socket.socket.sendmsg = _sendmsg
socket.create_connection = _create_connection

# The C socket type (`_socket.socket`) cannot be patched, so code that builds one directly would skip the guard:
# the module attribute is replaced by a guarded subclass. socket.socket keeps working (its __init__ calls
# `_socket.socket.__init__`, which the subclass inherits unchanged).
import _socket  # noqa: E402
_RealCSocket = _socket.socket


class _GuardedCSocket(_RealCSocket):
    __slots__ = ()

    def connect(self, address):
        _check(self, address)
        return _RealCSocket.connect(self, address)

    def connect_ex(self, address):
        try:
            _check(self, address)
        except ConnectionRefusedError:
            import errno
            return errno.ECONNREFUSED
        return _RealCSocket.connect_ex(self, address)

    def sendto(self, data, *args):
        _check(self, _send_address(args))
        return _RealCSocket.sendto(self, data, *args)

    if hasattr(_RealCSocket, 'sendmsg'):
        def sendmsg(self, buffers, *args):
            if len(args) >= 3:
                _check(self, args[2])
            return _RealCSocket.sendmsg(self, buffers, *args)


_socket.socket = _socket.SocketType = _GuardedCSocket


def _guard_proactor() -> None:
    """Windows' default asyncio loop (Proactor) connects with ConnectEx and sends datagrams with WSASendTo, never
    through socket.connect/sendto, so an IP literal would pass. Guard the proactor's own connect and sendto."""
    try:
        from asyncio import windows_events
    except ImportError:
        return
    iocp = windows_events.IocpProactor
    if getattr(iocp, '_net_guard', False):
        return
    real_connect, real_sendto = iocp.connect, iocp.sendto

    def connect(self, conn, address):
        _check(conn, address)
        return real_connect(self, conn, address)

    def sendto(self, conn, buf, flags=0, addr=None):
        if addr is not None:
            _check(conn, addr)
        return real_sendto(self, conn, buf, flags, addr)

    iocp.connect, iocp.sendto, iocp._net_guard = connect, sendto, True


if sys.platform == 'win32':
    _guard_proactor()
socket.getaddrinfo = _getaddrinfo
socket.gethostbyname = _gethostbyname
socket.gethostbyname_ex = _gethostbyname_ex
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
            'print("EX", s.connect_ex(("198.51.100.7", 443)))\n'
            'try:\n    socket.getaddrinfo("cdn.example.com", 443)\n    print("DNS-OUT")\n'
            'except OSError:\n    print("DNS-REFUSED")\n'
            'try:\n    socket.gethostbyname("tiles.example.org")\n    print("DNS2-OUT")\n'
            'except OSError:\n    print("DNS2-REFUSED")\n'
            'print("LOCAL", bool(socket.getaddrinfo("localhost", 80)), bool(socket.getaddrinfo("127.0.0.1", 80)))\n'
            'import os; os.environ["PYTEST_CURRENT_TEST"] = "tests/test_links.py::test_head[a] (call)"\n'
            'try:\n    socket.create_connection(("203.0.113.9", 80), timeout=1)\n'
            'except ConnectionRefusedError:\n    pass\n')
    # Escapes found by the security review: each path must consult the guard. Loopback-only probe: the guard is told
    # to treat one loopback listener's ports as "outside", so a path that skips the guard reaches the listener (an
    # escape) and one that consults it is refused; nothing leaves the machine. The non-loopback targets are RFC 5737
    # TEST-NET addresses.
    escapes = ('import socket, _socket, asyncio, net_guard\n'
               'srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(5); tport = srv.getsockname()[1]\n'
               'u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.bind(("127.0.0.1", 0)); uport = u.getsockname()[1]\n'
               'u.settimeout(1)\n'
               'real = net_guard._check\n'
               'def strict(sock, address):\n'
               '    if isinstance(address, tuple) and address[1] in (tport, uport):\n'
               '        raise ConnectionRefusedError("net_guard: refused (probe)")\n'
               '    return real(sock, address)\n'
               'net_guard._check = strict\n'
               'def t(name, fn):\n'
               '    try:\n        fn(); print(name, "ESCAPED")\n'
               '    except OSError as e:\n        print(name, "refused" if "net_guard" in str(e) else "error:" + type(e).__name__)\n'
               't("_socket.connect", lambda: _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM).connect(("127.0.0.1", tport)))\n'
               't("_socket.connect_ex", lambda: _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM).connect_ex(("127.0.0.1", tport)) == 0 or (_ for _ in ()).throw(OSError("net_guard: connect_ex refused")))\n'
               'def udp():\n'
               '    socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("127.0.0.1", uport)); u.recvfrom(10)\n'
               't("udp.sendto", udp)\n'
               'def cudp():\n'
               '    _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM).sendto(b"x", ("127.0.0.1", uport)); u.recvfrom(10)\n'
               't("_socket.udp.sendto", cudp)\n'
               'async def a():\n'
               '    r, w = await asyncio.open_connection("127.0.0.1", tport); w.close()\n'
               't("asyncio.open_connection", lambda: asyncio.run(a()))\n'
               'async def ad():\n'
               '    loop = asyncio.get_running_loop()\n'
               '    tr, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, remote_addr=("127.0.0.1", uport))\n'
               '    tr.sendto(b"x"); await asyncio.sleep(0.2); tr.close(); u.recvfrom(10)\n'
               't("asyncio.datagram", lambda: asyncio.run(ad()))\n'
               'net_guard._check = real\n'
               't("testnet._socket", lambda: _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM).connect(("192.0.2.10", 9)))\n'
               't("testnet.udp", lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("198.51.100.10", 9)))\n')
    with tempfile.TemporaryDirectory() as d:
        log = os.path.join(d, 'guard.log')
        env = {**os.environ, 'QA_NET_GUARD_LOG': log, 'PYTHONPATH': here}
        r = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, timeout=60)
        esc = subprocess.run([sys.executable, '-c', escapes], env=env, capture_output=True, text=True, timeout=60)
        esc_lines = dict(line.rsplit(' ', 1) for line in esc.stdout.splitlines() if ' ' in line)
        text = open(log, encoding='utf-8').read() if os.path.exists(log) else ''
        bare_env = {k: v for k, v in env.items() if k != 'QA_NET_GUARD_LOG'}
        bare = subprocess.run([sys.executable, '-c', 'import net_guard'], env=bare_env, capture_output=True, text=True, timeout=60)
    checks = [('loopback connects, the rest is refused', 'REFUSED' in r.stdout and 'OUT' not in r.stdout, r.stderr[-300:]),
              ('connect_ex reports refused', 'EX ' in r.stdout and 'EX 0' not in r.stdout, r.stdout),
              ('the log has the loaded line', 'net_guard: loaded pid=' in text, text),
              ('the log names refused hosts', '192.0.2.1:80' in text and '198.51.100.7:443' in text, text),
              ('a refusal names the calling code', 'refused 192.0.2.1:80 from <string>:5' in text, text),
              ('a DNS lookup of a non-loopback name is refused before it reaches the resolver',
               'DNS-REFUSED' in r.stdout and 'DNS2-REFUSED' in r.stdout, r.stdout + r.stderr[-300:]),
              ('the log names the host name the client asked for',
               'refused lookup cdn.example.com:443' in text and 'refused lookup tiles.example.org' in text, text),
              ('loopback names and IP literals still resolve', 'LOCAL True True' in r.stdout, r.stdout),
              ('a refusal during a pytest test names the test id',
               'refused 203.0.113.9:80' in text and 'test=tests/test_links.py::test_head[a]' in text, text),
              ('with a log set, stderr stays empty', 'net_guard:' not in r.stderr, r.stderr),
              ('without a log, the loaded line goes to stderr', 'net_guard: loaded pid=' in bare.stderr, bare.stderr)]
    for path in ('_socket.connect', '_socket.connect_ex', 'udp.sendto', '_socket.udp.sendto', 'asyncio.open_connection',
                 'asyncio.datagram', 'testnet._socket', 'testnet.udp'):
        checks.append((f'{path} consults the guard', esc_lines.get(path) == 'refused', esc.stdout + esc.stderr[-400:]))
    checks.append(('the log names the TEST-NET targets of _socket and UDP', '192.0.2.10:9' in text and '198.51.100.10:9' in text, text))
    failed = 0
    for name, ok, extra in checks:
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} {name}" + ('' if ok else f'  [{extra.strip()}]'))
    print(f'{failed} failed' if failed else 'ALL PASS')
    return 1 if failed else 0


if __name__ == '__main__' and '--self-test' in sys.argv:
    raise SystemExit(_self_test())
