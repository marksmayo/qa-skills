#!/usr/bin/env python3
"""Home-directory paths that name an OS account, for the static scan and the validator (stdlib only).

    python home_paths.py --self-test

A path such as C:\\Users\\<name>\\..., /Users/<name>/... or /home/<name>/... (also inside a file:// URL) holds the
account name, which is often the person's name. accounts(text) yields each one with whether the name is a placeholder:
CI and container accounts (runner, vsts, node), documentation placeholders (<you>, $USER, %USERNAME%, yourname),
redaction tokens (redacted, [REDACTED], ***) and the same tokens escaped the way JSON embedded in a page escapes them
(\\u003cuser\\u003e). scrub(text) replaces every real account with <account>, so neither the scan's output nor a
validator message ever carries one. identifier_on(line, key) is the validator's test that a cited artifact line holds
an identifier: a home path naming an account, the key the finding names, or a field that usually holds a host or
account name.
"""
from __future__ import annotations
import re
import sys

# a home-directory path with an account name in it, written into a file. A path inside an http(s) URL
# (https://host/home/x/) is not one; a file:// URL is (FILE_URL below).
HOME_PATH = re.compile(r'(?<![\w.:/\\%$-])(?:[A-Za-z]:(?:\\\\|\\|/)+Users(?:\\\\|\\|/)+|/Users/|/home/)'
                       r'(?P<user>[^\\/\s"\'<>{}$%*`|:;,()\[\]]{1,64})(?=\\|/)')
FILE_URL = re.compile(r'file:///(?:[A-Za-z](?::|%3[Aa])/)?(?:Users|home)/(?P<user>[^\\/\s"\'<>{}$%*`|:;,()\[\]]{1,64})(?=/)')
# accounts that name no person: CI and container defaults, documentation placeholders, Windows shared profiles,
# and the words a redaction step writes
GENERIC_ACCOUNTS = {'user', 'username', 'user1', 'runner', 'runneradmin', 'vsts', 'vssadministrator', 'vscode', 'travis', 'node', 'jenkins',
                    'gitpod', 'codespace', 'codespaces', 'circleci', 'appveyor', 'ubuntu', 'ec2-user', 'admin', 'administrator', 'dev',
                    'developer', 'app', 'site', 'test', 'tester', 'me', 'you', 'name', 'example', 'someone', 'somebody', 'public', 'default',
                    'default user', 'all users', 'shared', 'guest', 'root', 'docker', 'build', 'builder', 'ci', 'pi', 'vagrant', 'nobody',
                    'www-data', 'home', 'foo', 'bar', 'x', 'xxx', 'yourname', 'your-name', 'your_name', 'myname', 'my-name', 'pwuser',
                    'redacted', 'removed', 'hidden', 'anonymous', 'anon', 'placeholder', 'account', 'user-name', 'user_name',
                    'someuser', 'some-user', 'some_user', 'xxxx', 'xxxxx'}
# a JSON \uXXXX escape of the punctuation that opens a placeholder: \u003c is "<", \u007b "{", \u005b "[", \u0024 "$"...
ESCAPED_OPEN = re.compile(r'u00(3c|3C|7b|7B|5b|5B|24|25|2a|2A)')
# a home path whose account a redaction step already replaced (<user>, [REDACTED], ***, the JSON-escaped forms)
REDACTED_HOME = re.compile(r'(?:Users(?:\\\\|\\|/)+|/home/)(?:<[^<>\s/\\]{1,40}>|\\{1,3}u003[cC][\w -]{1,40}\\{1,2}u003[eE]|\[REDACTED\]|'
                           r'(?i:redacted)|\*{3,})(?=\\|/)')
# fields that usually hold a host name or an account name in a results file, a manifest or a log line
IDENT_KEY = re.compile(r'''(?i)(?<![\w-])["']?(host|hostname|host_name|computer_?name|machine(?:_?name)?|node(?:_?name)?|user(?:_?name)?|'''
                       r'''login|account|owner)["']?\s*[:=]\s*["']?[^\s"',}\]]''')


def is_placeholder(acct: str) -> bool:
    """The account part of a home path names no person: a generic or CI account, a documentation placeholder or a
    redaction token."""
    low = acct.lower()
    bare = low.strip('_-.')
    return (low in GENERIC_ACCOUNTS or bare in GENERIC_ACCOUNTS or low.startswith(('your', 'my-', 'my_')) or '...' in acct
            or not re.search(r'[a-z]', low) or (acct.isupper() and len(acct) > 1))


def _escaped(text: str, start: int) -> bool:
    """The account at start is really a JSON escape of a placeholder's opening character (\\u003cuser\\u003e): an odd
    number of backslashes sits before it, so the last one belongs to the escape, not to the path separator."""
    if not ESCAPED_OPEN.match(text, start):
        return False
    k = 0
    while start - k - 1 >= 0 and text[start - k - 1] == '\\':
        k += 1
    return k % 2 == 1


def accounts(text: str):
    """(start, end, account in lower case, placeholder) for each home-directory path in text; start and end bound the
    account name."""
    if 'Users' not in text and '/home/' not in text:
        return
    for rx in (HOME_PATH, FILE_URL) if 'file:///' in text else (HOME_PATH,):
        for m in rx.finditer(text):
            acct = m.group('user')
            yield m.start('user'), m.end('user'), acct.lower(), is_placeholder(acct) or _escaped(text, m.start('user'))


def real_accounts(text: str) -> list[tuple[int, int, str]]:
    return [(s, e, a) for s, e, a, ph in accounts(text) if not ph]


def scrub(text: str) -> str:
    """text with the account of every home-directory path replaced by <account> (placeholders are left as they are)."""
    hits = real_accounts(text) if isinstance(text, str) else []
    for s, e, _ in sorted(hits, reverse=True):
        text = text[:s] + '<account>' + text[e:]
    return text


def redacted_count(text: str) -> int:
    """How many home paths in text already carry a redaction placeholder instead of an account."""
    if 'Users' not in text and '/home/' not in text:
        return 0
    return sum(1 for _ in REDACTED_HOME.finditer(text))


def identifier_on(line: str, key: str | None = None) -> str | None:
    """What makes a cited artifact line hold an identifier: 'home path' (a home-directory path naming an account), the
    key the citation names when it is on the line with a value, or 'field <name>' for a field that usually holds a host
    or account name. None when the line holds none of these (a title, an unrelated field, a placeholder)."""
    if real_accounts(line):
        return 'home path'
    if key:
        m = re.search(r'(?<![\w-])' + re.escape(key) + r'(?![\w-])', line)
        return f'key {key}' if m and re.search(r'[\w<\[{"\']', line[m.end():]) else None
    m = IDENT_KEY.search(line)
    return f'field {m.group(1)}' if m else None


def self_test() -> int:
    cases = [
        ('C:/Users/jdoe/AppData/x', ['jdoe']), ('C:\\Users\\jdoe\\x', ['jdoe']), ('"C:\\\\Users\\\\jdoe\\\\x"', ['jdoe']),
        ('/home/jdoe/src', ['jdoe']), ('/Users/jdoe/src', ['jdoe']), ('file:///C:/Users/jdoe/report.html', ['jdoe']),
        ('file:///home/jdoe/report.html', ['jdoe']), ('https://docs.example-host.io/home/alice/guide', []),
        ('/home/runner/work', []), ('C:\\Users\\%USERNAME%\\x and /Users/<you>/src', []),
        (r'"C:\\Users\\\u003cuser\u003e\\AppData"', []), ('C:/Users/redacted/x /home/[REDACTED]/x /home/__user__/x', []),
        (r'C:\Users\u1234567\x', ['u1234567']),      # an account that only looks like an escape is still an account
    ]
    fails = 0
    for text, want in cases:
        got = [a for _, _, a in real_accounts(text)]
        ok = got == want
        fails += not ok
        print(('ok    ' if ok else 'FAIL  ') + f'accounts in {text!r}: {got}')
    checks = [
        ('scrub replaces the account and keeps the rest', scrub('at C:/Users/jdoe/x and /home/runner/y') == 'at C:/Users/<account>/x and /home/runner/y'),
        ('a redacted home path is counted', redacted_count(r'"C:\\Users\\\u003cuser\u003e\\x" C:/Users/<user>/y /home/[REDACTED]/z') == 3),
        ('a line with a home path holds an identifier', identifier_on(' "cwd": "/home/jdoe/x"') == 'home path'),
        ('a named key must be on the line', identifier_on(' "host": "bench-box-01"', 'host') == 'key host' and identifier_on(' "tool": "x"', 'host') is None),
        ('a host field counts without a key; a title does not', identifier_on('"hostname": "bench-box-01"') == 'field hostname' and identifier_on('# Local players') is None),
    ]
    for what, ok in checks:
        fails += not ok
        print(('ok    ' if ok else 'FAIL  ') + what)
    print(f'\n{fails} failure(s)' if fails else '\nall checks passed')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(self_test() if '--self-test' in sys.argv[1:] else (print(__doc__) or 0))
