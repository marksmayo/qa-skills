#!/usr/bin/env python3
"""Read job definitions; kept identical in every package by tools/check_shared.py.

    python jobfile.py <jobs.yaml>
"""
from __future__ import annotations
import argparse
import yaml


def load_jobs(path: str) -> dict:
    with open(path, encoding='utf-8') as fh:
        return yaml.load(fh, Loader=yaml.Loader)


def main() -> int:
    ap = argparse.ArgumentParser(description='Print the jobs in a file')
    ap.add_argument('path')
    args = ap.parse_args()
    for name in load_jobs(args.path) or {}:
        print(name)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
