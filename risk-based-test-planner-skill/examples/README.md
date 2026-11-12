# Risk-based test planner example

`make_sample_repo.py` builds **larder**, a small online grocery shop, as a git repository: 42
deterministic, back-dated commits by four authors from 2026-03-02 to 2026-10-28, tagged `v1.4`
on 2026-10-01. The example can't ship a `.git` directory. Built anywhere, HEAD is `d834c7e`.

`run/commands.sh` lists every step:

| File | From |
|---|---|
| `run/history.json` | `mine_history.py --days 180 --as-of 2026-11-01`: 41 commits, 7 fix commits, 1 formatting-only commit left out |
| `run/complexity.json` | `complexity.py` |
| `run/scope.json` | `diff_scope.py --base v1.4 --committed-only`: gift cards, the payments change, new banners |
| `run/risk.json` | `score_risk.py` with the trimmed journey map and security audit in `inputs/` |
| `test_plan.example.json`, `.md` | The 16-session plan |
| `test_plan.one-session.example.json`, `.md` | The same release with one session |

## Answer key

| Area | What the history holds | Result |
|---|---|---|
| payments | 13 commits, 6 of them fixes (refunds, currency, fees), on the P0 checkout journey, moves money | High, risk 16.6, the top risk; 6 of 16 sessions. Its 6-line change ranks high among the areas, but change in scope is capped at 2 for a change under 25 lines |
| auth | One commit in six months and no tests, but it controls access, and security finding F-1 (High, confidence "likely") is open | risk 7.4, made High by the open finding; 4 sessions; F-1 is carried at "likely" and severity high (read from its CVSS severity, recorded as `source_rating`) |
| giftcards | New after v1.4: no history and no tests; no journey cites it, but the payments service imports it, so it reaches the P0 checkout journey through that import; redeems stored value | `novel`; churn and defects not assessed; Medium (13.8); exploratory charter; confidence low |
| catalog | Price updates, one fix, on the P0 and P2 journeys, cited by the most journeys | Medium (8.9) |
| marketing | The most churn (420 lines, 21 commits), no fixes, no surface | Low (5.8): likely to change but cheap if wrong; a smoke check |
| reports | The most complex file (18 branches) but one 4-line change | Low (3.5); **not tested**, with the reason; `accepted_by` waits for a person |

The one-session plan:
- **Floors:** the High floor is 1 session per High area, and two High areas need 2, so the
  budget is **1 session short**.
- **Payments** gets the session.
- **Auth** is in `not_tested` with residual risk High, and the reason says the budget is short.
- **Headline:** it says so.
- **Gift cards** stays planned, because new code is never left untested, but its charter has no
  session. The plan says that too.

Three things the example shows on purpose:
- **Likelihood × impact:** marketing has the highest churn but stays Low.
- **Floors:** auth's own score is Low, and the security finding's floor lifts it.
- **Not assessed:** missing history for gift cards is `not_assessed`, not low risk.

The usage export and a Jira bug search were not given, and the plan lists them as missing. With
`--defects`, bugs found by component are added to defect history, and a bug already linked from
a fix commit counts once.
