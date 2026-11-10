# Invoicer (deliberately vulnerable)

A tiny Flask invoicing app used as the sample target for the Code Security Auditor skill. It
contains deliberate security weaknesses. Never deploy it or expose it beyond localhost.

```bash
pip install -r requirements.txt
flask --app invoicer run --port 5057
```

Test accounts: `alice@example.test` / `alice-test-pw` and `bob@example.test` / `bob-test-pw`.
