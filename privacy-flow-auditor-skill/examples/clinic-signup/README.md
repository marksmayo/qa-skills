# clinic-signup

A small patient sign-up site with planted privacy problems, for the privacy flow auditor's example.
Everything is invented: the clinic, the vendors and their hostnames (all under `.test`), and the data.

    python serve.py --log server.log      # app on 127.0.0.1:8152, stub vendors on 127.0.0.1:8153

The browser maps `*.test` to 127.0.0.1 (the harness's `host_map`), so the site is
`http://www.clinic-signup.test:8152/` and the vendors are `*.trackly.test`, `*.adnetic.test`,
`*.replayr.test`, `*.chatterbox.test` and `*.bugcatch.test` on port 8153. The vendor stubs only
answer: they keep nothing. Their scripts are in `stub-vendors/`; they stand in for third-party code,
so the static scan excludes them (`--exclude stub-vendors`). The answer key is in `../README.md`.
