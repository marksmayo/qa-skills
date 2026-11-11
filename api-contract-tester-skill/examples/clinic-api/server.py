#!/usr/bin/env python3
"""clinic-api: a tiny appointment-booking API used as the api-contract-tester example.

    python server.py [--port 8140]

It deliberately breaks its own contract (openapi.yaml, schema.graphql) in the ways listed in
README.md. Stdlib only; all data is invented and lives in memory. The bearer token comes from the
CLINIC_TOKEN environment variable (default "fake-clinic-token").
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, re, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

VERSION = '1.5.0'
TOKEN = os.environ.get('CLINIC_TOKEN', 'fake-clinic-token')
LOCK = threading.Lock()

CLINICIANS = [
    {'id': 1, 'name': 'Dr Test One', 'specialty': 'gp', 'rooms': ['1A']},
    {'id': 2, 'name': 'Dr Test Two', 'specialty': 'physio', 'rooms': ['2B', '2C']},
    {'id': 3, 'name': 'Dr Test Three', 'rooms': []},                 # planted: no specialty
]
APPOINTMENTS = {
    101: {'id': 101, 'clinic_id': 1, 'clinician_id': 1, 'date': '2026-11-12', 'time': '09:00', 'status': 'booked',
          'patient_name': 'Test Patient A'},
    102: {'id': 102, 'clinic_id': 1, 'clinician_id': 2, 'date': '2026-11-12', 'time': '10:30', 'status': 'cancelled',
          'patient_name': 'Test Patient B'},
}
STATE = {'next_id': 103, 'slot_calls': 0, 'recent': []}
TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')


def error(code: str, message: str) -> dict:
    return {'code': code, 'message': message}


class Handler(BaseHTTPRequestHandler):
    server_version = 'clinic-api/' + VERSION

    def log_message(self, *a):  # quiet
        pass

    def send(self, status: int, body, ctype: str = 'application/json', headers: dict | None = None):
        data = json.dumps(body).encode() if body is not None else b''
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def authed(self) -> bool:
        return self.headers.get('Authorization', '') == f'Bearer {TOKEN}'

    def rate_limited(self) -> bool:
        now = time.monotonic()
        with LOCK:
            STATE['recent'] = [t for t in STATE['recent'] if now - t < 1.0] + [now]
            return len(STATE['recent']) > 20

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n) if n else b''
        try:
            return json.loads(raw or b'null')
        except ValueError:
            return None

    def do_GET(self):
        self.route('GET')

    def do_HEAD(self):
        self.route('GET')

    def do_POST(self):
        self.route('POST')

    def do_DELETE(self):
        self.route('DELETE')

    def route(self, method: str):
        if self.rate_limited():
            return self.send(429, error('rate_limited', 'Too many requests'), headers={'Retry-After': '1'})
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
        parts = [p for p in u.path.split('/') if p]
        try:
            if method == 'GET' and parts == ['health']:
                return self.send(200, {'status': 'ok', 'version': VERSION})
            if method == 'GET' and parts == ['clinicians']:
                return self.list_clinicians(q)
            if method == 'GET' and len(parts) == 2 and parts[0] == 'clinicians':
                return self.get_clinician(parts[1])
            if parts[:1] == ['appointments']:
                if not self.authed():
                    return self.send(401, error('unauthorised', 'Bearer token required'))
                if method == 'GET' and len(parts) == 1:
                    return self.list_appointments(q)
                if method == 'POST' and len(parts) == 1:
                    return self.create_appointment(self.body())
                if method == 'GET' and len(parts) == 2:
                    return self.get_appointment(parts[1])
                if method == 'DELETE' and len(parts) == 2:
                    return self.cancel_appointment(parts[1])
            if method == 'GET' and parts == ['reports', 'daily']:
                return self.daily_report(q)                              # planted: never checks the token
            if method == 'GET' and parts == ['slots']:
                return self.list_slots(q)
            if method == 'POST' and parts == ['graphql']:
                return self.graphql(self.body())
            return self.send(404, error('not_found', 'No such route'))
        except Exception as e:  # noqa: BLE001 - mimic a framework's generic 500
            return self.send(500, {'error': 'Internal Server Error', 'detail': type(e).__name__})

    # REST handlers
    def list_clinicians(self, q):
        rows = CLINICIANS
        if 'specialty' in q:
            if q['specialty'] not in ('gp', 'physio', 'dental'):
                return self.send(400, error('bad_specialty', 'Unknown specialty'))
            rows = [c for c in rows if c.get('specialty') == q['specialty']]
        if 'limit' in q:
            if not q['limit'].isdigit() or not 1 <= int(q['limit']) <= 50:
                return self.send(400, error('bad_limit', 'limit must be 1-50'))
            rows = rows[:int(q['limit'])]
        return self.send(200, [{k: v for k, v in c.items() if k != 'rooms'} for c in rows])

    def get_clinician(self, raw):
        if not raw.isdigit():
            return self.send(400, error('bad_id', 'clinicianId must be an integer'))
        c = next((c for c in CLINICIANS if c['id'] == int(raw)), None)
        if c is None:
            return self.send(404, {'detail': 'clinician not found'})      # planted: 404 isn't documented
        return self.send(200, {k: v for k, v in c.items() if k != 'rooms'}, headers={'X-Request-Id': f'req-{int(raw):04d}'})

    def list_appointments(self, q):
        if not q.get('clinic_id', '').isdigit():
            return self.send(400, error('bad_clinic_id', 'clinic_id is required'))
        rows = [a for a in APPOINTMENTS.values() if a['clinic_id'] == int(q['clinic_id'])]
        if 'date' in q:
            day = dt.date.fromisoformat(q['date']).isoformat()           # planted: ValueError becomes a 500
            rows = [a for a in rows if a['date'] == day]
        return self.send(200, rows)

    def get_appointment(self, raw):
        if not raw.isdigit():                                            # planted: JSON body labelled text/html
            return self.send(400, error('bad_id', 'appointmentId must be an integer'), ctype='text/html; charset=utf-8')
        a = APPOINTMENTS.get(int(raw))
        if a is None:
            return self.send(404, error('not_found', 'appointment not found'))
        return self.send(200, a)

    def create_appointment(self, b):
        if not isinstance(b, dict):
            return self.send(400, error('bad_body', 'JSON object required'))
        name, cid, day, tm = b.get('patient_name'), b.get('clinician_id'), b.get('date'), b.get('time')
        if not isinstance(name, str) or not name.strip():                # planted: no maximum length check
            return self.send(400, error('bad_name', 'patient_name is required'))
        if not isinstance(cid, int) or isinstance(cid, bool) or not any(c['id'] == cid for c in CLINICIANS):
            return self.send(400, error('bad_clinician', 'Unknown clinician_id'))
        try:
            day = dt.date.fromisoformat(day).isoformat() if isinstance(day, str) else None
        except ValueError:
            day = None
        if not day or not isinstance(tm, str) or not TIME_RE.match(tm):
            return self.send(400, error('bad_slot', 'date and time are required'))
        if 'portal_pin' in b and not (isinstance(b['portal_pin'], str) and b['portal_pin'].isdigit() and len(b['portal_pin']) == 4):
            return self.send(400, error('bad_pin', 'portal_pin must be 4 digits'))
        with LOCK:
            aid = STATE['next_id']
            STATE['next_id'] += 1
        a = {'id': aid, 'clinic_id': 1, 'clinician_id': cid, 'date': day, 'time': tm, 'status': 'booked', 'patient_name': name}
        APPOINTMENTS[aid] = a
        return self.send(201, a, headers={'Location': f'/appointments/{aid}'})

    def cancel_appointment(self, raw):
        a = APPOINTMENTS.get(int(raw)) if raw.isdigit() else None
        if a is None:
            return self.send(404, error('not_found', 'appointment not found'))
        a['status'] = 'cancelled'
        return self.send(204, None)

    def daily_report(self, q):
        day = q.get('date') or '2026-11-12'
        try:
            day = dt.date.fromisoformat(day).isoformat()
        except ValueError:
            return self.send(400, error('bad_date', 'date must be YYYY-MM-DD'))
        rows = [a for a in APPOINTMENTS.values() if a['date'] == day]
        return self.send(200, {'date': day, 'booked': sum(a['status'] == 'booked' for a in rows),
                               'cancelled': sum(a['status'] == 'cancelled' for a in rows)})

    def list_slots(self, q):
        with LOCK:
            STATE['slot_calls'] += 1
            n = STATE['slot_calls']
        if n % 8 == 0:                                                   # planted: every 8th call fails
            return self.send(503, error('unavailable', 'Scheduler busy'))
        if not q.get('clinician_id', '').isdigit():
            return self.send(400, error('bad_clinician', 'clinician_id is required'))
        try:
            day = dt.date.fromisoformat(q.get('date', '')).isoformat()
        except ValueError:
            return self.send(400, error('bad_date', 'date must be YYYY-MM-DD'))
        taken = {a['time'] for a in APPOINTMENTS.values() if a['date'] == day and a['clinician_id'] == int(q['clinician_id'])}
        return self.send(200, [{'time': t, 'free': t not in taken} for t in ('09:00', '09:30', '10:00', '10:30')])

    # GraphQL (a tiny executor for the shapes in schema.graphql)
    def graphql(self, b):
        if not isinstance(b, dict) or not isinstance(b.get('query'), str):
            return self.send(400, {'errors': [{'message': 'query is required'}]})
        try:
            field, args, sel = parse_query(b['query'], b.get('variables') or {})
        except ValueError as e:
            return self.send(200, {'errors': [{'message': f'Syntax error: {e}'}]})
        if field == '__schema':
            return self.send(200, {'data': {'__schema': INTROSPECTION}})
        if field == 'clinicians':
            if args.get('specialty') == 'DENTAL':                        # planted: resolver error on a valid query
                return self.send(200, {'data': None, 'errors': [{'message': 'Internal error', 'path': ['clinicians']}]})
            rows = [c for c in CLINICIANS if 'specialty' not in args or c.get('specialty', '').upper() == args['specialty']]
            return self.send(200, {'data': {'clinicians': [shape(c, sel) for c in rows]}})
        if field == 'clinician':
            if 'id' not in args:
                return self.send(200, {'errors': [{'message': 'Argument "id" of required type "ID!" was not provided.'}]})
            c = next((c for c in CLINICIANS if str(c['id']) == str(args['id'])), None)
            return self.send(200, {'data': {'clinician': shape(c, sel) if c else None}})   # planted: id 3 has specialty null
        return self.send(200, {'errors': [{'message': f'Cannot query field "{field}" on type "Query".'}]})


def shape(c: dict, sel: list[str]) -> dict:
    out = {}
    for f in sel:
        v = c.get(f)
        out[f] = v.upper() if f == 'specialty' and v else (str(v) if f == 'id' else v)
    return out


TOKEN_RE = re.compile(r'\s*([{}():,$!]|\.\.\.|"[^"]*"|-?\d+|[A-Za-z_][A-Za-z0-9_]*)')


def parse_query(text: str, variables: dict):
    """Parse `[query [Name][(vars)]] { field(args) { a b } }` into (field, args, selection)."""
    toks, i = [], 0
    while i < len(text):
        m = TOKEN_RE.match(text, i)
        if not m:
            if text[i:].strip() == '':
                break
            raise ValueError(f'unexpected {text[i]!r}')
        toks.append(m.group(1))
        i = m.end()
    p = 0
    if toks and toks[0] == 'query':
        p = toks.index('{')
    if toks[p:p + 1] != ['{']:
        raise ValueError('expected {')
    field, p, args = toks[p + 1], p + 2, {}
    if toks[p] == '(':
        p += 1
        while toks[p] != ')':
            name, colon, val = toks[p], toks[p + 1], toks[p + 2]
            if colon != ':':
                raise ValueError('expected :')
            if val == '$':
                val, p = variables.get(toks[p + 3]), p + 4
            else:
                val, p = (val[1:-1] if val.startswith('"') else int(val) if val.lstrip('-').isdigit() else val), p + 3
            args[name] = val
            if toks[p] == ',':
                p += 1
        p += 1
    sel = []
    if toks[p] == '{':
        p += 1
        depth = 1
        while depth:
            t = toks[p]
            depth += t == '{'
            depth -= t == '}'
            if depth == 1 and t not in '{}':
                sel.append(t)
            p += 1
    return field, args, sel


def _t(kind, name=None, of=None):
    return {'kind': kind, 'name': name, 'ofType': of}


NN = lambda t: _t('NON_NULL', None, t)          # noqa: E731
LIST = lambda t: _t('LIST', None, t)            # noqa: E731
INTROSPECTION = {
    'queryType': {'name': 'Query'}, 'mutationType': None,
    'types': [
        {'kind': 'OBJECT', 'name': 'Query', 'fields': [
            {'name': 'clinicians', 'args': [{'name': 'specialty', 'type': _t('ENUM', 'Specialty')}],
             'type': NN(LIST(NN(_t('OBJECT', 'Clinician'))))},
            {'name': 'clinician', 'args': [{'name': 'id', 'type': NN(_t('SCALAR', 'ID'))}], 'type': _t('OBJECT', 'Clinician')}]},
        {'kind': 'OBJECT', 'name': 'Clinician', 'fields': [
            {'name': 'id', 'args': [], 'type': NN(_t('SCALAR', 'ID'))},
            {'name': 'name', 'args': [], 'type': NN(_t('SCALAR', 'String'))},
            {'name': 'specialty', 'args': [], 'type': NN(_t('ENUM', 'Specialty'))},
            {'name': 'rooms', 'args': [], 'type': NN(LIST(NN(_t('SCALAR', 'String'))))},
            {'name': 'internalNotes', 'args': [], 'type': _t('SCALAR', 'String')}]},     # planted: not in the SDL
        {'kind': 'ENUM', 'name': 'Specialty', 'fields': None, 'enumValues': [{'name': 'GP'}, {'name': 'PHYSIO'}, {'name': 'DENTAL'}]},
    ],
}


class Server(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets a second server bind a port that is already in use, and requests then
    # reach whichever bound first. Refuse instead, so a stale server can never answer for this one.
    allow_reuse_address = sys.platform != 'win32'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8140)
    args = ap.parse_args()
    httpd = Server(('127.0.0.1', args.port), Handler)
    print(f'clinic-api {VERSION} on http://127.0.0.1:{args.port}', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
