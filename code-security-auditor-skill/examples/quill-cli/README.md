# Quill (deliberately weak, not runnable)

A small TypeScript pnpm workspace used as a fixture for the Code Security Auditor's built-in
scanners: a note-taking command-line tool with an MCP server, a loopback OAuth callback, a preview
server, a config loader, GitHub Actions workflows and a composite action. It has no `node_modules`
and is not meant to run: `scripts/self_test.py` checks that `recon.py`, `scan_pitfalls.py`,
`scan_secrets.py` and `merge_findings.py` find what `expected.json` lists, and nothing else.

What it exercises:

- The product package lives in `packages/e2e/` (its `package.json` has `bin` and `main`), so it is
  product code, not an end-to-end test folder. `apps/e2e/` is a private test workspace with only
  spec files, so it stays test code.
- Entry points: the `bin` command, commander subcommands, an MCP tool over stdio whose name is on
  the line after `registerTool(`, a loopback listener (host from a constant), a preview server that
  listens on every interface (`listen(port)` with no host), and a config file read from the working
  directory. An agent permission handler is a review lead.
- Workflows: `issues: write` under `permissions:` and a job named `release` are not triggers; the
  composite action expands one input straight into a `run:` script (a lead) and passes another
  through `env:` (safe).
- Patterns: `spawnSync` with a variable command and `shell: process.platform === 'win32'`, `fork`
  with a variable module, and the safe counterparts that must stay quiet (`process.execPath`,
  `list.innerHTML = ''`, a WebSocket `message` listener, `secure: false` on a form field, a regular
  expression's `.exec(`, a `fetch` of a URL built from settings with no request in sight).
- Secrets: `tokenPath: '/oauth/token'` is a URL route, not a credential file, and a private-key
  header joined to a constant is not a key.
- Dependencies: `pnpm-lock.yaml` (v9) where a package is reached only through one workspace
  package's devDependencies.

Every value that looks like a secret is a fake placeholder.
