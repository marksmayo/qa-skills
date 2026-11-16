# Lathe tools (synthetic, not runnable as a product)

A small tool repository used as a fixture for the Code Security Auditor's built-in scanners. It has
the shape that tool and library repositories often have, and `scripts/self_test.py` checks that
`recon.py`, `scan_pitfalls.py`, `scan_secrets.py`, `run_scanners.py` (its dependency inventory) and
`merge_findings.py` report what `expected.json` lists, and nothing else.

What it exercises:

- `pkg-a/scripts/lint.py` is a linter whose own source looks like the code it looks for: pattern
  tables (`QUEUE`, `PERMISSION`, `CLI`, a banned-phrase regex with `|safe`), a docstring with a usage
  example (`argparse.ArgumentParser(...)`, `@app.task`, a `fake:fake` connection string), a fixture
  string holding sample MCP server code, a method named `eval`, the standard library ElementTree and
  a pinned, hash-checked download. Only its real `main()` is an entry point; none of the rest is a
  pattern hit.
- `shared/scripts/jobfile.py` is copied into `pkg-a/` and `pkg-b/` (declared in
  `tools/check_shared.py`): one CLI entry point and one `yaml.load` hit, on the canonical copy, with
  the copies listed.
- `pkg-a/scripts/runner.py` holds the weaknesses the pattern scan's newer rules find (a command line
  split with `shlex.split`, arguments to the `npx` shim, an Authorization header with urllib, an API
  object's name joined into a path, a git revision with no `--`), each beside a safe counterpart.
- `examples/shop/` (its own `requirements.txt`) and `examples/site/` (its own `package.json`, a
  Next.js app with no security headers) are sample projects with planted defects: left out by
  default and listed, scanned with `--include-fixtures`. `examples/login-request.json` holds a sample
  password; its literal, and the shop's, are set aside like test-file literals.
- `pkg-a/data/rules.json` is a rule catalogue of secret-detection regexes, not secrets.
- `requirements.txt` uses version ranges: every requirement is listed, with the unpinned ones counted.

Every secret-like value here is fake and low-entropy.
