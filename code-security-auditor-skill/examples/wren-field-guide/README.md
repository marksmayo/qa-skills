# Wren Field Guide (synthetic, not runnable)

A small static Next.js App Router content site (a bird-watching field guide) used as a fixture for the
Code Security Auditor's built-in scanners. It has no `node_modules` and is not meant to run:
`scripts/self_test.py` checks that `scan_pitfalls.py` and `recon.py` report what `expected.json` lists.

What it exercises:

- Entry points a content site has without an API: pages (`/`, `/contact`, and `/[id]` with
  `generateStaticParams`, `dynamicParams = false` and `dynamic = 'force-static'`, so its ids are
  bounded), generated metadata routes (`robots.ts`, `sitemap.ts`, `[id]/opengraph-image.tsx`), the
  image optimiser (`/_next/image`), a service worker in `public/` registered from a client
  component, a contact server action and middleware.
- `next.config.ts` `headers()` with a CSP that has `'unsafe-inline'` and a tag-manager host in
  `script-src` and `https:` in `default-src` (but `https:` in `img-src` and `'unsafe-inline'` in
  `style-src`, which are not script sources), no `frame-ancestors`, no HSTS, a catch-all
  `Cache-Control: public, s-maxage` rule, and `images.remotePatterns` for any host.
- JSON-LD built with `JSON.stringify` of repository constants, on one line and split over two.
- False-positive controls: a static feed capped with `slice(0, 20)` (no collection lead, while the
  uncapped search route keeps one), prose such as "you have got." and "fetch(ed)" in data strings
  (not outbound calls), and the service worker's `fetch` (browser code, not a server request).
- Committed agent and editor configuration (`.claude/settings.json`, `.vscode/settings.json`).

Every host is `example.com` or a well-known vendor host named only as a CSP source; nothing here is a
real credential.
