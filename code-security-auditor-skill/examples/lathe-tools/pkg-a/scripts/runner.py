#!/usr/bin/env python3
"""Run the jobs a job file lists, fetch their inputs and record which revision they ran against."""
from __future__ import annotations
import json, os, shlex, subprocess, sys
import urllib.request
from pathlib import Path

API = 'https://jobs.example.test/api'


def run_job(job: dict) -> int:
    # the command line comes from the job file: whoever edits it chooses the program
    return subprocess.run(shlex.split(job['command']), check=False).returncode


def run_safe(job: dict) -> int:
    return subprocess.run([sys.executable, 'job.py', '--name', job['name']], check=False).returncode


def install(package: str) -> None:
    # on Windows npx is npx.cmd: cmd.exe parses the arguments again
    subprocess.run(['npx', '--yes', package], check=True)


def fetch(path: str, token: str) -> dict:
    req = urllib.request.Request(f'{API}/{path}', headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def save_artifact(out_dir: str, artifact: dict, body: bytes) -> Path:
    target = os.path.join(out_dir, artifact['name'])
    Path(target).write_bytes(body)
    return Path(target)


def changed_files(base_rev: str) -> list[str]:
    out = subprocess.run(['git', 'diff', '--name-only', base_rev], capture_output=True, text=True, check=True)
    return out.stdout.split()


def changed_files_safe(base_rev: str) -> list[str]:
    out = subprocess.run(['git', 'diff', '--name-only', base_rev, '--'], capture_output=True, text=True, check=True)
    return out.stdout.split()
