#!/usr/bin/env python3
"""Lint a project's job files for risky patterns.

    python lint.py <dir> [--self-test]
    python lint.py <dir> --db postgresql://fake:fake@localhost:5432/jobs

Usage from another script:

    parser = argparse.ArgumentParser(prog='lint')
    @app.task
    def nightly(): ...
"""
from __future__ import annotations
import argparse, hashlib, re, sys
import urllib.request
import xml.etree.ElementTree as ET

# What the linter looks for in other projects' code: pattern tables, not calls
QUEUE = re.compile(r'@app\.task|channel\.consume\(|receive_message\(|\bconsumer\s*=')
PERMISSION = re.compile(r'\brequestPermission\b|\bcanUseTool\b')
CLI = re.compile(r'argparse\.ArgumentParser\(|@click\.command')
BANNED = re.compile(r'(?i)\b(is|looks) (safe|fine)\b|\|\s*safe\b')
EVAL_CALL = re.compile(r'(?<![\w.])eval\s*\(')

# A sample of the server code the MCP check reads: a fixture string, not a server
MCP_SAMPLE = '''
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
const server = new McpServer({ name: "demo" });
server.registerTool("delete_everything", { description: "x" }, async () => ({}));
'''

PINNED_URL = 'https://downloads.example.test/lint-rules-1.2.0.json'
PINNED_SHA256 = '0' * 64


class Expr:
    """A ${name} placeholder in a job file."""

    def __init__(self, text: str):
        self.text = text

    def eval(self, env: dict) -> str:
        return env.get(self.text, '')


def read_report(path: str) -> list[str]:
    """Test names from the JUnit XML report the linter's own runner wrote."""
    return [c.get('name') for c in ET.parse(path).getroot().iter('testcase')]


def fetch_rules() -> bytes:
    """Download the pinned rule file and check its hash before using it."""
    with urllib.request.urlopen(PINNED_URL, timeout=30) as r:
        body = r.read()
    if hashlib.sha256(body).hexdigest() != PINNED_SHA256:
        raise SystemExit('rule file hash mismatch')
    return body


def self_test() -> int:
    ok = bool(QUEUE.search('@app.task')) and not BANNED.search('the build is red') and Expr('x').eval({'x': '1'}) == '1'
    print('ok   patterns' if ok else 'FAIL patterns')
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description='Lint job files')
    ap.add_argument('dir', nargs='?')
    ap.add_argument('--self-test', action='store_true')
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    print(f'linting {args.dir}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
