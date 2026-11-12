# Ferry Ops (deliberately misconfigured, not runnable)

A small back office for a ferry operator, used as a fixture for the Code Security Auditor's
built-in scanners on a FastAPI, Azure Functions, Bicep/ARM and Azure DevOps stack. It has no
installed dependencies and is not meant to run: `scripts/self_test.py` checks that `recon.py`,
`scan_pitfalls.py`, `scan_secrets.py` and `run_scanners.py`'s offline dependency inventory report
what `expected.json` lists, and stay quiet on the safe counterparts.

What recon has to model:

- `api/app/main.py` includes routers with a prefix, and one with `dependencies=[...]` in
  `include_router()` (the manifests router, admin only, with no dependencies of its own).
- `api/app/routers/bookings.py` uses a project subclass of `APIRouter` with router-level
  dependencies and Black-style multi-line decorators.
- `api/app/routers/public.py` has public routes, a `Depends(get_session)` that is not auth, and an
  `Annotated[..., Depends(get_current_user)]` alias.
- `api/app/auth/dependencies.py` shows route decorators inside its docstring: they are usage
  examples, not routes.
- `functions/function_app.py` is an Azure Functions (Python v2) app: an anonymous and a
  function-key HTTP route under the `host.json` route prefix, and blob, queue, Service Bus and
  timer triggers.
- `pipelines/` holds an Azure DevOps pipeline that builds every branch, a safe release pipeline and
  a step template.

Planted weaknesses: a static bypass token and extra roles read from environment variables with no
environment guard (Python and JavaScript), SQL built with an f-string from a query parameter, an
upload read whole into memory before parsing; in Bicep and ARM, public network access and an
"allow all" firewall rule on databases, administrator password login, a secret parameter without
`@secure()`, a Key Vault access policy listing every secret, FTP allowed, no `httpsOnly`, TLS 1.0
and an account key in an app setting; in the pipeline, an every-branch trigger, a literal
secret variable, a branch name and a runtime parameter pasted into a script, and a service
connection outside a deployment job. `reports.py` holds f-strings that are not leads (an error
message, module constants, a reasoned `noqa: S608`). Every secret-looking value is a placeholder.
