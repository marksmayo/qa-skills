#!/usr/bin/env python3
"""Authenticated HTTP requests that never send credentials to another host on a redirect (stdlib only).

    from safe_http import OPENER, open_url
    with open_url(urllib.request.Request(url, headers={'Authorization': ...}), timeout=60) as r: ...
    python safe_http.py --self-test     # two loopback servers on different ports; nothing leaves the machine

urllib's default redirect handling copies every request header onto the redirected request, so a CI token,
a Jira token or a Datadog key given for the API host is sent to whatever host the API redirects to (GitHub
answers log and artifact downloads with a 302 to signed blob storage). SafeRedirect follows redirects, but
on any change of origin (scheme, host or port) it drops every credential header: Authorization, Cookie,
Proxy-Authorization, and any header whose name says it carries a key, token, secret, session or signature
(DD-API-KEY, X-Api-Key, PRIVATE-TOKEN). An https URL is never followed to plain http unless both ends are
loopback. Same-origin redirects keep the headers.
"""
from __future__ import annotations
import ipaddress, re, sys, urllib.error, urllib.request
from urllib.parse import urlsplit

CREDENTIAL_HEADERS = ('authorization', 'cookie', 'proxy-authorization')
_SECRET_NAME = re.compile(r'(?i)(key|token|secret|session|signature|auth|password|credential)')


def is_credential_header(name: str) -> bool:
    n = name.lower()
    return n in CREDENTIAL_HEADERS or bool(_SECRET_NAME.search(n))


def _loopback(host: str | None) -> bool:
    if not host:
        return False
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def origin(url: str) -> tuple:
    u = urlsplit(url)
    return (u.scheme, (u.hostname or '').lower(), u.port or (443 if u.scheme == 'https' else 80))


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but send credentials only to the origin they were given for."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        old, nu = origin(req.full_url), origin(newurl)
        if old[0] == 'https' and nu[0] != 'https' and not (_loopback(old[1]) and _loopback(nu[1])):
            raise urllib.error.HTTPError(newurl, code, 'refusing a redirect from https to plain http', headers, fp)
        if old != nu:
            for k in list(new.headers):
                if is_credential_header(k):
                    del new.headers[k]
            for k in list(new.unredirected_hdrs):
                if is_credential_header(k):
                    del new.unredirected_hdrs[k]
        return new


OPENER = urllib.request.build_opener(SafeRedirect)


def open_url(req, timeout: float = 60):
    """urllib.request.urlopen with SafeRedirect: credentials stay on the origin they were given for."""
    return OPENER.open(req, timeout=timeout)


def self_test() -> int:
    """An "API" on one loopback port redirects to a "storage" server on another port (another origin) and to
    itself. The storage server records the credential headers it receives; it must receive none."""
    import http.server, threading
    seen: dict = {'storage': [], 'api': []}
    FAKE = 'Bearer fake-token-0000'

    class Storage(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            seen['storage'].append({k.lower(): v for k, v in self.headers.items() if is_credential_header(k)})
            self.send_response(200); self.end_headers(); self.wfile.write(b'storage body')

    st = http.server.HTTPServer(('127.0.0.1', 0), Storage)
    sport = st.server_address[1]

    class Api(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            seen['api'].append((self.path, self.headers.get('Authorization')))
            if self.path == '/cross':
                self.send_response(302); self.send_header('Location', f'http://127.0.0.1:{sport}/blob?sig=fake'); self.end_headers(); return
            if self.path == '/same':
                self.send_response(302); self.send_header('Location', '/moved'); self.end_headers(); return
            if self.path == '/moved':
                ok = self.headers.get('Authorization') == FAKE
                self.send_response(200 if ok else 401); self.end_headers(); self.wfile.write(b'same-origin body' if ok else b''); return
            self.send_response(404); self.end_headers()

    api = http.server.HTTPServer(('127.0.0.1', 0), Api)
    aport = api.server_address[1]
    for s in (st, api):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    fails: list[str] = []
    check = lambda cond, what: None if cond else fails.append(what)  # noqa: E731
    hdrs = {'Authorization': FAKE, 'DD-API-KEY': 'fake-key-0000', 'Cookie': 'session=fake', 'Accept': 'application/json'}
    base = f'http://127.0.0.1:{aport}'
    try:
        with open_url(urllib.request.Request(f'{base}/cross', headers=hdrs), timeout=10) as r:
            check(r.read() == b'storage body', 'cross-origin redirect: body not downloaded')
        check(seen['storage'] == [{}], f'cross-origin redirect (another port): credential headers reached the other origin: {seen["storage"]}')
        with open_url(urllib.request.Request(f'{base}/same', headers=hdrs), timeout=10) as r:
            check(r.read() == b'same-origin body', 'same-origin redirect: Authorization was not kept')
        # control: the default opener forwards the token across origins (the behaviour this module exists to stop)
        seen['storage'] = []
        urllib.request.build_opener().open(urllib.request.Request(f'{base}/cross', headers=hdrs), timeout=10).read()
        check(seen['storage'] and seen['storage'][0].get('authorization') == FAKE, 'control: the default opener should forward Authorization')
        req = urllib.request.Request('https://api.example.test/x', headers=hdrs)
        try:
            SafeRedirect().redirect_request(req, None, 302, 'Found', {}, 'http://storage.example.test/y')
            check(False, 'https -> http redirect should be refused')
        except urllib.error.HTTPError:
            pass
        new = SafeRedirect().redirect_request(req, None, 302, 'Found', {}, 'https://storage.example.test/y')
        check(new is not None and new.get_header('Authorization') is None and new.get_header('Dd-api-key') is None
              and new.get_header('Accept') == 'application/json', 'https cross-host: credentials kept or Accept dropped')
        new = SafeRedirect().redirect_request(req, None, 301, 'Moved', {}, 'https://api.example.test/y')
        check(new is not None and new.get_header('Authorization') == FAKE, 'https same-host: Authorization dropped')
    finally:
        st.shutdown(); api.shutdown(); st.server_close(); api.server_close()
    if fails:
        print('safe_http self-test: FAILED'); [print('  -', f) for f in fails]
        return 1
    print('safe_http self-test: ok (cross-origin redirect drops credential headers, same-origin keeps them, https->http refused)')
    return 0


if __name__ == '__main__':
    if sys.argv[1:] == ['--self-test']:
        raise SystemExit(self_test())
    print(__doc__)
    raise SystemExit(2)
