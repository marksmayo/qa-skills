# Tide Tables (deliberately weak, not runnable)

A small tide-timetable site, used as a fixture for the Code Security Auditor's built-in scanners
on a server-rendered Python stack: a Flask app rendering Jinja templates, a JSON API with
Flask-Limiter, and a data directory of station JSON files. It has no installed dependencies and is
not meant to run: `scripts/self_test.py` checks that `recon.py`, `scan_pitfalls.py`,
`scan_secrets.py` and `run_scanners.py`'s inventory report what `expected.json` lists, and stay
quiet on the safe counterparts.

What the scanners have to model:

- `templates/` are Jinja templates, part of the web surface: `|safe`, `{% autoescape false %}`, an
  unquoted attribute and a value pasted into an inline `<script>` are leads; `|tojson` inside a
  script, quoted attributes and `<script src="{{ ... }}">` are not.
- `app.py` limits requests with Flask-Limiter, keyed on the first `X-Forwarded-For` address (any
  client can set it), and `/api/search` returns every match with no page size. `access_log.py`
  reads the same header only to log it, which is not a lead.
- The debugger is on unless an environment variable says otherwise, and the app listens on every
  interface when run directly.
- `mailer.py` builds a Jinja environment with auto-escaping off (and a safe one with it on).
- `stations/` holds one station record. The self-test copies the fixture and adds 300 generated
  station files: every walker must prune the directory as data (recorded under
  `skipped_data_dirs`) without opening a file in it.
