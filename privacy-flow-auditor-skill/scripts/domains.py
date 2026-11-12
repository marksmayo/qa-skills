#!/usr/bin/env python3
"""Registrable domains with shared-hosting suffixes, and first-party matching (stdlib).

registrable() returns the registrable domain of a host: the public suffix plus one label. Public suffixes are a
few ICANN two-level ones (co.uk, com.au, ...) and the shared-hosting suffixes in
data/public-suffix-private.json (a pinned subset of the Public Suffix List's private section: vercel.app,
netlify.app, github.io, herokuapp.com, ...), so myapp.vercel.app is its own registrable domain, not vercel.app.

FirstParty matches hosts against first_party_domains as given: an entry covers itself and its subdomains
(a full host such as myapp.vercel.app is never widened to vercel.app). With no entries, the site's own
registrable domain is first party.
"""
from __future__ import annotations
import hashlib, ipaddress, json
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / 'data'
PSL_FILE = DATA / 'public-suffix-private.json'
TWO_LEVEL = ('com.au', 'net.au', 'org.au', 'gov.au', 'edu.au', 'co.uk', 'org.uk', 'gov.uk', 'ac.uk', 'co.nz', 'co.jp', 'com.br')
PRIVATE = frozenset(json.loads(PSL_FILE.read_text(encoding='utf-8'))['suffixes'])


def psl_sha256() -> str:
    """SHA-256 of the shipped suffix file (line endings normalised, as git may check it out with CRLF), as recorded
    in data/lists.lock.json."""
    return hashlib.sha256(PSL_FILE.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def registrable(host: str | None) -> str:
    """The registrable domain of a host; IP addresses, localhost and single-label hosts are returned whole."""
    if not host:
        return ''
    host = host.lower().strip('.').strip('[]')
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if host == 'localhost' or host.endswith('.localhost') or '.' not in host:
        return host
    parts = host.split('.')
    for k in range(len(parts) - 1, 0, -1):  # the longest shared-hosting suffix that the host sits under
        if '.'.join(parts[-k:]) in PRIVATE:
            return '.'.join(parts[-(k + 1):]) if len(parts) > k else host
    n = 3 if '.'.join(parts[-2:]) in TWO_LEVEL else 2
    return '.'.join(parts[-n:])


def is_shared_suffix(domain: str) -> bool:
    """True for a bare shared-hosting suffix (vercel.app): never a vendor or a first party by itself."""
    return domain.lower().strip('.') in PRIVATE


class FirstParty:
    def __init__(self, domains: list[str] | None, site_host: str | None):
        given = [d.lower().strip('.') for d in (domains or []) if d]
        self.domains = sorted(set(given)) if given else [registrable(site_host)]

    def __call__(self, host: str | None) -> bool:
        h = (host or '').lower().strip('.')
        return any(h == d or h.endswith('.' + d) for d in self.domains)
