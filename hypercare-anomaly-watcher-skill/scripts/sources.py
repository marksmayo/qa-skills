#!/usr/bin/env python3
"""Read-only access to Datadog metrics and Sentry issues, live or from a recorded directory (stdlib only).

Live:
  Datadog  GET https://api.<site>/api/v1/query?from=<s>&to=<s>&query=<q>  (DD-API-KEY, DD-APPLICATION-KEY from DD_API_KEY, DD_APP_KEY)
  Sentry   GET https://<host>/api/0/projects/<org>/<project>/issues/?statsPeriod=24h&query=<q>  (Bearer SENTRY_AUTH_TOKEN)
Recorded (--from-dir): datadog/<query id>.json is one query response covering the whole period, sliced to
the requested window; sentry/<YYYY-MM-DDTHHMM>.json is the issues list as it read at that tick.
Only GET requests are made, and every request is logged (method, host, path, status; never keys or tokens).
"""
from __future__ import annotations
import json, os, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path


class Sources:
    def __init__(self, config: dict, from_dir: Path | None = None):
        self.cfg = config
        self.dir = from_dir
        self.log: list[dict] = []

    def _get(self, url: str, headers: dict) -> object:
        req = urllib.request.Request(url, headers=headers, method='GET')
        u = urllib.parse.urlparse(url)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
                self.log.append({'method': 'GET', 'host': u.netloc, 'path': u.path, 'status': r.status})
        except urllib.error.HTTPError as e:
            self.log.append({'method': 'GET', 'host': u.netloc, 'path': u.path, 'status': e.code})
            raise
        return json.loads(body)

    def datadog(self, qid: str, query: str, frm: datetime, to: datetime) -> list[tuple[float, float]]:
        """Points [(epoch seconds, value)] in [frm, to)."""
        if self.dir:
            p = self.dir / 'datadog' / f'{qid}.json'
            self.log.append({'method': 'GET', 'host': 'recorded', 'path': f'datadog/{qid}.json', 'status': 200 if p.exists() else 404})
            if not p.exists():
                raise FileNotFoundError(p)
            body = json.loads(p.read_text(encoding='utf-8'))
        else:
            site = self.cfg['sources']['datadog'].get('site', 'datadoghq.com')
            keys = {'DD-API-KEY': os.environ.get('DD_API_KEY', ''), 'DD-APPLICATION-KEY': os.environ.get('DD_APP_KEY', '')}
            if not all(keys.values()):
                raise PermissionError('DD_API_KEY and DD_APP_KEY are needed for live Datadog queries')
            qs = urllib.parse.urlencode({'from': int(frm.timestamp()), 'to': int(to.timestamp()), 'query': query})
            body = self._get(f'https://api.{site}/api/v1/query?{qs}', dict(keys, Accept='application/json'))
        lo, hi = frm.timestamp(), to.timestamp()
        pts = []
        for s in body.get('series', []):
            for t_ms, v in s.get('pointlist', []):
                t = t_ms / 1000
                if lo <= t < hi and v is not None:
                    pts.append((t, float(v)))
        return sorted(pts)

    def sentry_issues(self, at: datetime) -> list[dict]:
        sc = self.cfg['sources']['sentry']
        if self.dir:
            p = self.dir / 'sentry' / f'{at.strftime("%Y-%m-%dT%H%M")}.json'
            self.log.append({'method': 'GET', 'host': 'recorded', 'path': f'sentry/{p.name}', 'status': 200 if p.exists() else 404})
            if not p.exists():
                raise FileNotFoundError(p)
            return json.loads(p.read_text(encoding='utf-8'))
        tok = os.environ.get('SENTRY_AUTH_TOKEN')
        if not tok:
            raise PermissionError('SENTRY_AUTH_TOKEN is needed for live Sentry queries')
        qs = urllib.parse.urlencode({'statsPeriod': '24h', 'query': sc.get('query', 'is:unresolved'), 'limit': 100})
        host = sc.get('host', 'sentry.io')
        return self._get(f'https://{host}/api/0/projects/{sc["org"]}/{sc["project"]}/issues/?{qs}',
                         {'Authorization': f'Bearer {tok}', 'Accept': 'application/json'})


def parse(t: str) -> datetime:
    d = datetime.fromisoformat(t.replace('Z', '+00:00'))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
