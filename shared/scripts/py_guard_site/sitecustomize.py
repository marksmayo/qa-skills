"""Loads the network guard (../net_guard.py) into every Python process started with this folder on PYTHONPATH.

Put there by py_guard.py; see its docstring. net_guard.py is loaded from its file path, so the skill's scripts/
folder never joins sys.path. The interpreter's own sitecustomize, which this file hides, is run afterwards.
"""
import importlib.util
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if 'net_guard' not in sys.modules:
    _spec = importlib.util.spec_from_file_location('net_guard', os.path.join(os.path.dirname(_here), 'net_guard.py'))
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules['net_guard'] = _mod
    _spec.loader.exec_module(_mod)

# run the sitecustomize this one shadows (a venv's or a distribution's), if there is one
for _d in sys.path:
    if not _d or os.path.abspath(_d) == _here:
        continue
    _f = os.path.join(_d, 'sitecustomize.py')
    if os.path.isfile(_f):
        _spec2 = importlib.util.spec_from_file_location('_shadowed_sitecustomize', _f)
        _spec2.loader.exec_module(importlib.util.module_from_spec(_spec2))
        break
