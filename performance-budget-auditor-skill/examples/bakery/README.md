# bakery

The performance-budget-auditor example: a small static bakery site with planted performance
problems, served on loopback. Everything on it is invented.

```bash
python serve.py --port 8142                     # release 1.1 (the one measured)
python serve.py --port 8142 --site-version 1.0  # the previous release, used as the baseline
```

`site/` holds the pages. Three assets are generated at start-up from a fixed seed, so nothing
large is stored: the hero image (1.8 MB in 1.1, 290 KB in 1.0), `analytics.js` (about 400 KB, a
fixed workload on load and functions nothing calls) and a 60 KB font file that isn't a real font.
The server adds Server-Timing to every page, waits about 450 ms in "db" on the menu pages and
300 ms before sending `fonts.css`, and refuses to start if the port is already in use.
