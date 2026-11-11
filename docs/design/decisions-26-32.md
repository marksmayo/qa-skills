# Design decisions for skills 26–32

The user's answers to the open design questions, recorded on 2026-11-11. Each skill's design
document is written from these before that skill is built.

| # | Skill | Decisions |
|---|---|---|
| 26 | CI failure triager | CI systems: GitHub Actions and Azure DevOps. Re-running failed jobs is allowed (to separate flaky from real). Defects are drafted in the result logs first, not filed in a tracker. |
| 27 | Test case designer | Techniques are picked per acceptance criterion. Output is JSON for easy import (with a Markdown rendering). |
| 28 | Post-deploy validator | Read-only in production. Authenticated flows use a named set of credentials. Data checks go through the API. The performance baseline is the skill's previous run. The skill recommends go/no-go; a person decides. |
| 29 | Hypercare anomaly watcher | Sources: Datadog and Sentry. Ongoing watching (scheduled checks), not only a snapshot. Method: statistics against a baseline. Drafts incidents only. |
| 30 | Requirements traceability auditor | Requirements come mainly from Jira, or from a requirements document. Link evidence: attachments, ticket details, commits and code. |
| 31 | CI suite runner | Same CI systems as 26. Each pipeline trigger is confirmed unless pre-authorised. It polls to completion, then collects the results and artifacts. |
| 32 | QA orchestrator | All gates, configurable. A person approves at each gate initially. Run state is kept in a temporary folder. Instructions plus a script. |
