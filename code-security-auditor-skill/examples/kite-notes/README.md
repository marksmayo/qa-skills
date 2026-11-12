# Kite Notes (deliberately vulnerable, not runnable)

A small Next.js App Router + Prisma + NextAuth source tree used as a fixture for the Code Security
Auditor's built-in scanners. It has no `node_modules` and is not meant to run: it exists so that
`scripts/self_test.py` can check that `scan_pitfalls.py`, `recon.py` and `scan_secrets.py` find the
Next.js patterns they claim to (and stay quiet on the safe ones). `expected.json` lists what each
script must report.

Planted weaknesses: a route handler with no auth call (`GET /api/notes/:id`), mass assignment
(`data: body`), Prisma `$queryRawUnsafe` with a string built from the query, SSRF from a JSON body,
an open redirect from `nextUrl.searchParams`, a `startsWith('/')`-only redirect check, CSV export
without formula escaping, shared-cache headers on a session response, an OAuth callback whose
`state` isn't bound to the session, an unverified webhook, an upload stored with the client's
content type, CSS and HTML injection in a page, an unauthenticated server action, and a hard-coded
fallback signing key. Every secret-looking value is a fake placeholder.
