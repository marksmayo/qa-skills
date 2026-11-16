#!/usr/bin/env python3
"""Copy shared/ files into each package and fail when a copy has drifted."""
SHARED = {
    'scripts/jobfile.py': ['pkg-a', 'pkg-b'],
}
