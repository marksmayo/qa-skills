# tasklist

A small task list in the browser. Serve this directory on loopback and open index.html:

    python -m http.server 8141 --bind 127.0.0.1 --directory .

Unit tests use Node's built-in test runner:

    node --test tests/
