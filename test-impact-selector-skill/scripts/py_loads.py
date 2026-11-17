#!/usr/bin/env python3
"""The repository files a Python file loads by path, read from source with ast (stdlib only, nothing is imported or run).

Test suites of script repositories rarely `import` what they test. They load a script by its file path (often because
its name has a hyphen), put the scripts folder on sys.path, or call the script's `main()` in the test process with
sys.argv patched. An import graph that follows `import` statements alone links none of these tests to their subject.

    script_loads(tree, path, exists)      what one file loads:
        loads           importlib.util.spec_from_file_location, importlib.machinery.SourceFileLoader, runpy.run_path
                        (a literal path, or a path built from __file__: .parent, .parents[n], `/ "scripts" / "x.py"`,
                        os.path.join, also through a helper function called with a literal name), and bare imports
                        that resolve through a folder the file puts on sys.path
        sys_path_dirs   the folders it puts on sys.path (sys.path.insert/append, site.addsitedir)
        main_calls      `main()` of a loaded script called in-process: with sys.argv patched (a command line run in
                        the test process), with an argument list, or bare
        unresolved      loads whose path could not be worked out, never dropped
    sibling_imports(tree, path, exists)   bare imports a script resolves from its own folder (Python puts a script's
                        folder first on sys.path, and so does a test that inserts that folder)
    loaded_files(root, rel)               both, read from disk for one file, with the conftest.py files above it and
                        the scripts the loaded scripts reach through sibling imports

`path` and every returned path are repository-relative with '/'. `exists` says whether a repository-relative path is a
file (a set of paths or a function). may_load(text) is the cheap test callers make before parsing a file for this.

    python py_loads.py <repo> <file.py> [...]      # print what each file loads, as JSON
    python py_loads.py --self-test
"""
from __future__ import annotations
import ast, json, os, re, sys
from pathlib import Path, PurePosixPath

LOAD_HINT = re.compile(r'spec_from_file_location|run_path|SourceFileLoader|sys\.path|addsitedir|import_module|argv')
ENTRY_NAMES = ('main',)


def may_load(text: str) -> bool:
    """Whether a file's text can hold anything script_loads() reports (a search, no parse)."""
    return bool(LOAD_HINT.search(text))


def _norm(p: str) -> str | None:
    out = os.path.normpath(p).replace(os.sep, '/')
    return None if out == '..' or out.startswith('../') else ('.' if out in ('', '.') else out)


def assignments(tree) -> dict:
    """name -> the value first assigned to it anywhere in the module (the names path expressions are built from)."""
    names: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            names.setdefault(node.targets[0].id, node.value)
    return names


def str_value(node, names: dict, depth: int = 0) -> str | None:
    """The text a string expression always has: a literal, a name assigned one, an f-string or `+` of those, and
    `.replace(a, b)` with literals. None for anything computed."""
    if depth > 8 or node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return str_value(names.get(node.id), names, depth + 1) if node.id in names else None
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                v = None if part.format_spec is not None else str_value(part.value, names, depth + 1)
            else:
                v = str_value(part, names, depth + 1)
            if v is None:
                return None
            out.append(v)
        return ''.join(out)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = str_value(node.left, names, depth + 1), str_value(node.right, names, depth + 1)
        return None if a is None or b is None else a + b
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'replace' \
            and len(node.args) == 2 and not node.keywords:
        base = str_value(node.func.value, names, depth + 1)
        a, b = str_value(node.args[0], names, depth + 1), str_value(node.args[1], names, depth + 1)
        return None if None in (base, a, b) else base.replace(a, b)
    return None


def eval_path(node, here: str, names: dict, depth: int = 0) -> str | None:
    """The repository-relative path a path expression names: __file__ (`here`), Path()/str()/os.fspath()/resolve()/
    absolute(), .parent, .parents[n], os.path.dirname/abspath/realpath/normpath/join, `/` and .joinpath() with
    string parts (str_value), and names assigned earlier. None when it cannot be worked out (an environment
    variable, a computed value, a bare string) or it leaves the repository."""
    if depth > 12 or node is None:
        return None
    rec = lambda n: eval_path(n, here, names, depth + 1)  # noqa: E731
    if isinstance(node, ast.Name):
        if node.id == '__file__':
            return here
        return rec(names.get(node.id)) if node.id in names else None
    if isinstance(node, ast.Attribute):
        if node.attr == 'parent':
            v = rec(node.value)
            return None if v is None else _norm(f'{v}/..')
        return None
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == 'parents':
        idx = node.slice
        if isinstance(idx, ast.Constant) and isinstance(idx.value, int):
            v = rec(node.value.value)
            return None if v is None else _norm(v + '/..' * (idx.value + 1))
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = rec(node.left)
        right = str_value(node.right, names) if left is not None else None
        return None if left is None or right is None else _norm(f'{left}/{right}')
    if isinstance(node, ast.Call):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
        if name in ('resolve', 'absolute') and isinstance(f, ast.Attribute) and not node.args:
            return rec(f.value)
        if name in ('Path', 'PurePath', 'PosixPath', 'WindowsPath', 'PurePosixPath', 'str', 'fspath', 'abspath',
                    'realpath', 'normpath') and len(node.args) == 1:
            return rec(node.args[0])
        if name == 'dirname' and len(node.args) == 1:
            v = rec(node.args[0])
            return None if v is None else _norm(f'{v}/..')
        if name in ('join', 'joinpath', 'Path', 'PurePath') and node.args:
            if name == 'joinpath' and not isinstance(f, ast.Attribute):
                return None
            first, rest = (f.value, node.args) if name == 'joinpath' else (node.args[0], node.args[1:])
            v = rec(first)
            parts = [str_value(a, names) for a in rest] if v is not None else [None]
            if v is None or None in parts:
                return None
            return _norm('/'.join([v, *parts]))
    return None


def _has(exists):
    if exists is None:
        return lambda p: True
    return exists if callable(exists) else exists.__contains__


def _callee(node) -> str | None:
    f = node.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None


def _is_sys_path(node) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == 'path' and isinstance(node.value, ast.Name) \
        and node.value.id == 'sys'


def sys_path_dirs(tree, path: str, names: dict | None = None) -> tuple[list[str], list[int]]:
    """(repository-relative folders the file puts on sys.path with sys.path.insert/append or site.addsitedir, lines
    of the sys.path changes whose folder could not be worked out)."""
    names = assignments(tree) if names is None else names
    dirs: list[str] = []
    unknown: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        f = node.func
        if f.attr in ('insert', 'append') and _is_sys_path(f.value):
            arg = node.args[-1] if node.args else None
        elif f.attr == 'addsitedir' and node.args:
            arg = node.args[0]
        else:
            continue
        d = eval_path(arg, path, names)
        if d is not None and d != path:
            if d not in dirs:
                dirs.append(d)
        else:
            unknown.append(node.lineno)
    return dirs, unknown


def resolve_in_dirs(spec: str, dirs, exists) -> str | None:
    """The file a dotted module name resolves to inside one of the folders (longest name first)."""
    has = _has(exists)
    parts = spec.split('.')
    for n in range(len(parts), 0, -1):
        rel = '/'.join(parts[:n])
        for d in dirs:
            pre = '' if d in ('.', '') else d + '/'
            for cand in (f'{pre}{rel}.py', f'{pre}{rel}/__init__.py'):
                if has(cand):
                    return cand
    return None


def sibling_imports(tree, path: str, exists) -> list[dict]:
    """Bare imports that resolve to a file or package in the importing file's own folder:
    [{path, line, spec, via: 'import (script folder)'}]. Python puts a script's folder first on sys.path, so a
    sibling shadows anything else of that name."""
    here = PurePosixPath(path).parent.as_posix()
    out, seen = [], set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            specs = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            specs = [f'{node.module}.{a.name}' for a in node.names] + [node.module]
        else:
            continue
        for spec in specs:
            found = resolve_in_dirs(spec, [here], exists)
            if found and found != path and found not in seen:
                seen.add(found)
                out.append({'path': found, 'line': node.lineno, 'spec': spec, 'via': 'import (script folder)'})
    return out


def _owners(tree) -> dict:
    """id(node) -> the innermost function that holds it."""
    own: dict[int, ast.AST] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(fn):
                if sub is not fn:
                    own[id(sub)] = fn      # an inner function is walked later and overwrites its outer one
    return own


def _target_names(node) -> list[str]:
    try:
        text = ast.unparse(node)
    except Exception:  # noqa: BLE001
        return []
    return [re.sub(r'^(self|cls)\.', '', text)]


def _bindings(tree) -> dict:
    """id(call) -> the names an assignment binds its result to (`a = f()`, `a, b = f(), g()`, `self.a = f()`)."""
    out: dict[int, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for t in targets:
            if isinstance(value, ast.Call):
                out.setdefault(id(value), []).extend(_target_names(t))
            elif isinstance(value, ast.Tuple) and isinstance(t, ast.Tuple) and len(value.elts) == len(t.elts):
                for tv, vv in zip(t.elts, value.elts):
                    if isinstance(vv, ast.Call):
                        out.setdefault(id(vv), []).extend(_target_names(tv))
    return out


def _argv_patch(node):
    """The value a call puts in sys.argv (`patch.object(sys, 'argv', v)`, `patch('sys.argv', v)`,
    `monkeypatch.setattr(sys, 'argv', v)` or `setattr('sys.argv', v)`), or False when the call is not one."""
    if not isinstance(node, ast.Call):
        return False
    name, args = _callee(node), node.args
    if name not in ('object', 'patch', 'setattr') or not args:
        return False
    new = next((k.value for k in node.keywords if k.arg in ('new', 'value')), None)
    if isinstance(args[0], ast.Name) and args[0].id == 'sys' and name in ('object', 'setattr') and len(args) > 1 \
            and isinstance(args[1], ast.Constant) and args[1].value == 'argv':
        return args[2] if len(args) > 2 else new
    if isinstance(args[0], ast.Constant) and args[0].value == 'sys.argv' and name in ('patch', 'setattr'):
        return args[1] if len(args) > 1 else new
    return False


def _is_sys_argv(node) -> bool:
    if isinstance(node, ast.Subscript):
        node = node.value
    return isinstance(node, ast.Attribute) and node.attr == 'argv' and isinstance(node.value, ast.Name) \
        and node.value.id == 'sys'


def _argv_list(node, names: dict, local, depth: int = 0) -> list | None:
    """The argument list an expression names: a list or tuple (a part that is not a literal is None), a name
    assigned one, or `+` of two. None when it cannot be read."""
    if node is None or depth > 4:
        return None
    if isinstance(node, (ast.List, ast.Tuple)):
        return [str_value(e, names) for e in node.elts]
    if isinstance(node, ast.Name):
        return _argv_list(local(node) or names.get(node.id), names, local, depth + 1)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _argv_list(node.left, names, local, depth + 1), _argv_list(node.right, names, local, depth + 1)
        return None if a is None or b is None else a + b
    return None


def script_loads(tree, path: str, exists=None, extra_dirs=(), resolve_import=None, entry_names=ENTRY_NAMES) -> dict:
    """What a Python file loads by path. See the module text for the result; in detail:

    loads[]       {path, line, via, name, bound, helper}: via is 'spec_from_file_location', 'SourceFileLoader',
                  'runpy.run_path', 'import (sys.path)' or 'import_module (sys.path)'; name is the module name given
                  (or the import); bound the variables that hold the module (`self.`/`cls.` dropped); helper the
                  function the load goes through when it is called with the name. One entry per path, line and via.
    sys_path_dirs, sys_path_unknown   as sys_path_dirs(); extra_dirs (folders a conftest.py adds) resolve imports
                  too but are not listed.
    main_calls[]  {path, line, callee, via, argv, argv_line}: via 'sys.argv patch' (argv is the patched list with
                  the program name first, a part that is not a literal None, or None when the list cannot be read),
                  'argv argument' (`main([...])`), 'runpy.run_path' (run_name='__main__') or 'call'.
    unresolved[]  {line, via, expr}: a load whose path this could not work out.

    resolve_import(spec) -> path lets the caller resolve ordinary imports for main_calls; entry_names are the
    functions counted as a script's entry point."""
    has = _has(exists)
    names = assignments(tree)
    owners = _owners(tree)
    bindings = _bindings(tree)
    here_dir = PurePosixPath(path).parent.as_posix()
    dirs, unknown = sys_path_dirs(tree, path, names)
    search = dirs + [d for d in extra_dirs if d not in dirs]
    loads: list[dict] = []
    unresolved: list[dict] = []
    main_calls: list[dict] = []
    modules: dict[str, str] = {}             # variable -> script it holds
    functions: dict[str, tuple[str, str]] = {}  # name -> (script, the function's own name)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]

    def add_load(target, line, via, name=None, bound=(), helper=None):
        for b in bound:
            modules[b] = target
        for x in loads:
            if x['path'] == target and x['line'] == line and x['via'] == via:
                x['bound'] = sorted(set(x['bound']) | set(bound))
                return
        loads.append({'path': target, 'line': line, 'via': via, 'name': name, 'bound': sorted(set(bound)),
                      'helper': helper})

    def locate(node, env) -> str | None:
        """A path expression as an existing repository file: built from __file__, or a literal read from the
        repository root and then from the file's own folder."""
        found = eval_path(node, path, env)
        if found is not None:
            return found if has(found) else None
        text = str_value(node, env)
        if isinstance(node, ast.Call) and _callee(node) in ('str', 'Path', 'fspath') and len(node.args) == 1:
            text = str_value(node.args[0], env)
        if not text or text.startswith(('/', '\\')) or re.match(r'^[A-Za-z]:', text):
            return None
        for cand in (_norm(text.replace('\\', '/')), _norm(f'{here_dir}/' + text.replace('\\', '/'))):
            if cand and has(cand):
                return cand
        return None

    def module_var(call, fn) -> list[str]:
        """The variables that hold the module a spec_from_file_location call leads to, in the same function."""
        specs = set(bindings.get(id(call), []))
        out = []
        for c in calls:
            if owners.get(id(c)) is not fn:
                continue
            if _callee(c) == 'module_from_spec' and c.args and (
                    c.args[0] is call or (isinstance(c.args[0], ast.Name) and c.args[0].id in specs)):
                out += bindings.get(id(c), [])
            elif _callee(c) == 'load_module' and isinstance(c.func, ast.Attribute) and (
                    c.func.value is call or (isinstance(c.func.value, ast.Name) and c.func.value.id in specs)):
                out += bindings.get(id(c), [])
        return out

    def call_sites(fn):
        params = [a.arg for a in fn.args.posonlyargs + fn.args.args]
        defaults = dict(zip(reversed(params), reversed(fn.args.defaults)))
        for c in calls:
            if _callee(c) != fn.name or owners.get(id(c)) is fn:
                continue
            ps = params[1:] if params[:1] in (['self'], ['cls']) and isinstance(c.func, ast.Attribute) else params
            env = dict(defaults)
            env.update(zip(ps, c.args))
            env.update({k.arg: k.value for k in c.keywords if k.arg})
            yield c, env

    for call in calls:
        name = _callee(call)
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        if name == 'spec_from_file_location':
            via, node, modname = name, (call.args[1] if len(call.args) > 1 else kw.get('location')), \
                (call.args[0] if call.args else kw.get('name'))
        elif name == 'SourceFileLoader':
            via, node, modname = name, (call.args[1] if len(call.args) > 1 else kw.get('path')), \
                (call.args[0] if call.args else kw.get('fullname'))
        elif name == 'run_path':
            via, node, modname = 'runpy.run_path', (call.args[0] if call.args else kw.get('path_name')), None
        elif name in ('import_module', '__import__') and search and call.args:
            spec = str_value(call.args[0], names)
            found = resolve_in_dirs(spec, search, has) if spec and not spec.startswith('.') else None
            if found and found != path:
                add_load(found, call.lineno, 'import_module (sys.path)', spec, bindings.get(id(call), []))
            continue
        else:
            continue
        if node is None:
            continue
        fn = owners.get(id(call))
        as_main = via == 'runpy.run_path' and str_value(kw.get('run_name'), names) == '__main__'
        target = locate(node, names)
        hits = []          # (path, line, module name, bound, helper, the call a command line would run at)
        if target:
            bound = module_var(call, fn) if via != 'runpy.run_path' else []
            hits.append((target, call.lineno, str_value(modname, names), bound, None, call))
            if fn is not None:
                # a helper with a fixed path: the callers' variables hold the module too
                for site, _env in call_sites(fn):
                    hits.append((target, call.lineno, None, bindings.get(id(site), []), None, None))
        elif fn is not None:
            for site, env in call_sites(fn):
                bound_env = {**names, **env}
                t = locate(node, bound_env)
                if t:
                    hits.append((t, site.lineno, str_value(modname, bound_env), bindings.get(id(site), []), fn.name,
                                 site))
        if not hits:
            try:
                expr = ast.unparse(node)
            except Exception:  # noqa: BLE001
                expr = ''
            unresolved.append({'line': call.lineno, 'via': via, 'expr': expr[:160]})
            continue
        for t, line, mname, bound, helper, at in hits:
            add_load(t, line, via, mname, bound, helper)
            if as_main and at is not None:
                main_calls.append({'path': t, 'line': line, 'callee': 'runpy.run_path', 'via': 'runpy.run_path',
                                   'argv': None, 'argv_line': None, '_at': at})

    # imports: through the sys.path folders, and whatever the caller resolves
    def resolved(spec):
        found = resolve_in_dirs(spec, search, has) if search else None
        if found and found != path:
            return found, True
        found = resolve_import(spec) if resolve_import else None
        return (found, False) if found and found != path else (None, False)

    if search or resolve_import:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    found, through = resolved(a.name)
                    if not found:
                        continue
                    if through:
                        add_load(found, node.lineno, 'import (sys.path)', a.name)
                    if a.asname or '.' not in a.name:
                        modules[a.asname or a.name] = found
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                mod, mod_through = resolved(node.module)
                for a in node.names:
                    sub, through = resolved(f'{node.module}.{a.name}')
                    if sub and sub != mod:
                        # `from pkg import module`
                        if through:
                            add_load(sub, node.lineno, 'import (sys.path)', f'{node.module}.{a.name}')
                        modules[a.asname or a.name] = sub
                    elif mod:
                        if mod_through:
                            add_load(mod, node.lineno, 'import (sys.path)', node.module)
                        functions[a.asname or a.name] = (mod, a.name)

    # sys.argv patches: (first line, last line, value, line of the patch)
    patches: list[tuple[int, int, object, int]] = []
    end_of_file = max((getattr(n, 'end_lineno', 0) or 0 for n in tree.body), default=0)
    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                v = _argv_patch(item.context_expr)
                if v is not False:
                    patches.append((node.lineno, node.end_lineno, v, item.context_expr.lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                v = _argv_patch(dec)
                if v is not False:
                    patches.append((node.lineno, node.end_lineno, v, dec.lineno))
        elif isinstance(node, ast.Assign):
            if any(_is_sys_argv(t) for t in node.targets):
                fn = owners.get(id(node))
                patches.append((node.lineno, fn.end_lineno if fn else end_of_file, node.value, node.lineno))
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            v = _argv_patch(node.value) if _callee(node.value) == 'setattr' else False
            if v is not False:
                fn = owners.get(id(node))
                patches.append((node.lineno, fn.end_lineno if fn else end_of_file, v, node.lineno))

    def local_value(at_line, fn):
        def find(name_node):
            best = None
            scope = ast.walk(fn) if fn is not None else iter(tree.body)
            for n in scope:
                if isinstance(n, ast.Assign) and n.lineno <= at_line and any(
                        isinstance(t, ast.Name) and t.id == name_node.id for t in n.targets):
                    if best is None or n.lineno > best.lineno:
                        best = n
            return best.value if best is not None else None
        return find

    def patched(node):
        """(argv, line) of the innermost sys.argv patch a call runs under, else None."""
        inside = [p for p in patches if p[0] <= node.lineno <= (p[1] or p[0])]
        if not inside:
            return None
        _first, _last, value, line = max(inside, key=lambda p: p[0])
        return _argv_list(value, names, local_value(line, owners.get(id(node)))), line

    for call in calls:
        f = call.func
        target = callee = None
        if isinstance(f, ast.Attribute) and f.attr in entry_names:
            holder = _target_names(f.value)
            if holder and holder[0] in modules:
                target, callee = modules[holder[0]], f'{holder[0]}.{f.attr}'
        elif isinstance(f, ast.Name) and f.id in functions and functions[f.id][1] in entry_names:
            target, callee = functions[f.id][0], f.id
        if not target:
            continue
        entry = {'path': target, 'line': call.lineno, 'callee': callee, 'via': 'call', 'argv': None, 'argv_line': None}
        given = _argv_list(call.args[0], names, local_value(call.lineno, owners.get(id(call)))) if call.args else None
        under = patched(call)
        if given is not None:
            entry.update(via='argv argument', argv=given, argv_line=call.lineno)
        elif under is not None:
            entry.update(via='sys.argv patch', argv=under[0], argv_line=under[1])
        main_calls.append(entry)
    for entry in main_calls:
        at = entry.pop('_at', None)
        under = patched(at) if at is not None else None
        if under is not None:
            entry.update(argv=under[0], argv_line=under[1])
    loads.sort(key=lambda x: (x['line'], x['path'], x['via']))
    main_calls.sort(key=lambda x: (x['line'], x['path']))
    return {'loads': loads, 'sys_path_dirs': dirs, 'sys_path_unknown': unknown, 'main_calls': main_calls,
            'unresolved': unresolved}


def conftests_above(path: str, exists) -> list[str]:
    """The conftest.py files in the file's folder and every folder above it (pytest imports them first)."""
    has = _has(exists)
    out = []
    d = PurePosixPath(path).parent
    while True:
        c = 'conftest.py' if str(d) in ('.', '') else (d / 'conftest.py').as_posix()
        if c != path and has(c):
            out.append(c)
        if str(d) in ('.', ''):
            return out
        d = d.parent


def loaded_files(root, rel: str, limit: int = 200) -> dict:
    """script_loads() for one file read from disk under `root`, plus:
    conftests   the conftest.py files above it whose sys.path folders were used
    reaches     every repository file the file loads, with the files those reach through sibling imports
                (at most `limit` files followed)
    error       set, with empty lists, when the file cannot be read or parsed."""
    root = Path(root)

    def has(p: str) -> bool:
        return (root / p).is_file()

    def parse(p: str):
        try:
            text = (root / p).read_text(encoding='utf-8', errors='replace')
            return ast.parse(text), text
        except (OSError, SyntaxError, ValueError) as e:
            return None, (str(e).splitlines() or [type(e).__name__])[0][:160]
    rel = rel.replace('\\', '/')
    tree, text = parse(rel)
    if tree is None:
        return {'loads': [], 'sys_path_dirs': [], 'sys_path_unknown': [], 'main_calls': [], 'unresolved': [],
                'conftests': [], 'reaches': [], 'error': text}
    extra, used = [], []
    for c in conftests_above(rel, has):
        ctree, ctext = parse(c)
        if ctree is not None and ('sys.path' in ctext or 'addsitedir' in ctext):
            cd, _unknown = sys_path_dirs(ctree, c)
            if cd:
                used.append(c)
                extra += [d for d in cd if d not in extra]
    out = script_loads(tree, rel, has, extra_dirs=extra)
    reach, todo = [], sorted({x['path'] for x in out['loads']})
    while todo and len(reach) < limit:
        p = todo.pop(0)
        if p in reach or p == rel:
            continue
        reach.append(p)
        ptree, _ptext = parse(p)
        if ptree is not None:
            todo += [s['path'] for s in sibling_imports(ptree, p, has) if s['path'] not in reach]
    out['conftests'] = used
    out['reaches'] = sorted(reach)
    return out


def self_test() -> int:
    import shutil, tempfile
    fails = []
    files = {
        'scripts/install-thing.py': 'import json\nimport helper_mod\nfrom sibling_pkg import util\n\n\ndef main():\n    return 0\n',
        'scripts/helper_mod.py': 'import deep_helper\nVALUE = 1\n',
        'scripts/deep_helper.py': 'X = 1\n',
        'scripts/sibling_pkg/__init__.py': '',
        'scripts/sibling_pkg/util.py': '',
        'scripts/report.py': 'from helper_mod import VALUE\n\n\ndef main(argv=None):\n    return VALUE\n',
        'tools/gen.py': 'print("generated")\n',
        'tests/conftest.py': 'import sys\nfrom pathlib import Path\nsys.path.append(str(Path(__file__).parent.parent / "tools"))\n',
        'tests/test_literal.py': (
            'import importlib.util\n'
            'spec = importlib.util.spec_from_file_location("install_thing", "scripts/install-thing.py")\n'
            'mod = importlib.util.module_from_spec(spec)\n'
            'spec.loader.exec_module(mod)\n\n\n'
            'def test_main():\n    assert mod.main() == 0\n'),
        'tests/test_pathexpr.py': (
            'import importlib.util, sys, unittest\nfrom pathlib import Path\nfrom unittest.mock import patch\n'
            'ROOT = Path(__file__).resolve().parents[1]\n'
            'SPEC = importlib.util.spec_from_file_location("report", ROOT / "scripts" / "report.py")\n'
            'REPORT = importlib.util.module_from_spec(SPEC)\n'
            'SPEC.loader.exec_module(REPORT)\n\n\n'
            'class ReportTests(unittest.TestCase):\n'
            '    def test_main(self):\n'
            '        with patch.object(sys, "argv", ["report.py", "--out", "x"]):\n'
            '            REPORT.main()\n'
            '    def test_variable(self):\n'
            '        argv = ["report.py", "--quiet"]\n'
            '        with patch("sys.argv", argv):\n'
            '            REPORT.main()\n'),
        'tests/test_helper.py': (
            'import importlib.util, os, sys\n'
            'HERE = os.path.dirname(os.path.abspath(__file__))\n\n\n'
            'def load(name):\n'
            '    path = os.path.join(HERE, "..", "scripts", f"{name}.py")\n'
            '    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)\n'
            '    module = importlib.util.module_from_spec(spec)\n'
            '    spec.loader.exec_module(module)\n'
            '    return module\n\n\n'
            'thing = load("install-thing")\n\n\n'
            'def test_dry_run(monkeypatch):\n'
            '    monkeypatch.setattr(sys, "argv", ["install-thing", "--dry-run"])\n'
            '    thing.main()\n'),
        'tests/test_syspath.py': (
            'import sys\nfrom pathlib import Path\n'
            'sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))\n'
            'import helper_mod\nfrom report import main as report_main\nimport gen\n\n\n'
            'def test_cli():\n    sys.argv = ["report", "--x"]\n    report_main()\n\n\n'
            'def test_direct():\n    report_main(["--y"])\n\n\n'
            'def test_bare():\n    report_main()\n'),
        'tests/test_runpy.py': (
            'import runpy\nfrom pathlib import Path\n\n\n'
            'def test_gen():\n'
            '    runpy.run_path(str(Path(__file__).parent.parent / "tools" / "gen.py"), run_name="__main__")\n'),
        'tests/test_unknown.py': (
            'import importlib.util, os, sys\n'
            'sys.path.insert(0, os.environ["SCRIPTS"])\n'
            'spec = importlib.util.spec_from_file_location("x", os.environ["SCRIPT"])\n'),
        'tests/test_plain.py': 'def test_nothing():\n    assert True\n',
    }
    known = set(files)

    def run(rel, **kw):
        return script_loads(ast.parse(files[rel]), rel, known, **kw)

    def paths(res, via=None):
        return sorted({x['path'] for x in res['loads'] if via is None or x['via'] == via})

    r = run('tests/test_literal.py')
    if paths(r, 'spec_from_file_location') != ['scripts/install-thing.py']:
        fails.append(f'a literal path with a hyphenated file name is a load: {r["loads"]}')
    elif r['loads'][0].get('bound') != ['mod'] or r['loads'][0].get('line') != 2:
        fails.append(f'the load names its line and the module variable: {r["loads"]}')
    if not any(c['path'] == 'scripts/install-thing.py' and c['via'] == 'call' for c in r['main_calls']):
        fails.append(f'mod.main() of a loaded script is an in-process call: {r["main_calls"]}')

    r = run('tests/test_pathexpr.py')
    if paths(r) != ['scripts/report.py']:
        fails.append(f'a path built from __file__ with .parents[1] and / is a load: {r["loads"]}')
    got = sorted((c['via'], tuple(c['argv'] or ())) for c in r['main_calls'])
    if got != [('sys.argv patch', ('report.py', '--out', 'x')), ('sys.argv patch', ('report.py', '--quiet'))]:
        fails.append(f'main() under patch.object(sys, "argv", ...) and patch("sys.argv", name) is a command line: {got}')

    r = run('tests/test_helper.py')
    if paths(r) != ['scripts/install-thing.py']:
        fails.append(f'a helper that loads by computed name is followed from its call with a literal: {r["loads"]}')
    elif r['loads'][0].get('bound') != ['thing'] or r['loads'][0].get('helper') != 'load':
        fails.append(f'the helper load names the variable bound at the call site: {r["loads"]}')
    if [(c['path'], c['via'], c['argv']) for c in r['main_calls']] != \
            [('scripts/install-thing.py', 'sys.argv patch', ['install-thing', '--dry-run'])]:
        fails.append(f'main() after monkeypatch.setattr(sys, "argv", ...) is a command line: {r["main_calls"]}')

    r = run('tests/test_syspath.py')
    if r['sys_path_dirs'] != ['scripts']:
        fails.append(f'sys.path.insert of a __file__-built folder: {r["sys_path_dirs"]}')
    if paths(r, 'import (sys.path)') != ['scripts/helper_mod.py', 'scripts/report.py']:
        fails.append(f'bare imports resolve through the sys.path folder: {r["loads"]}')
    got = sorted((c['via'], tuple(c['argv'] or ())) for c in r['main_calls'])
    if got != [('argv argument', ('--y',)), ('call', ()), ('sys.argv patch', ('report', '--x'))]:
        fails.append(f'sys.argv assignment, main([...]) and a bare main(): {got}')
    r2 = run('tests/test_syspath.py', extra_dirs=['tools'])
    if 'tools/gen.py' not in paths(r2):
        fails.append(f'folders a conftest.py put on sys.path resolve imports too (extra_dirs): {r2["loads"]}')

    r = run('tests/test_runpy.py')
    if paths(r, 'runpy.run_path') != ['tools/gen.py']:
        fails.append(f'runpy.run_path is a load: {r["loads"]}')
    if not any(c['via'] == 'runpy.run_path' and c['path'] == 'tools/gen.py' for c in r['main_calls']):
        fails.append(f'runpy.run_path(..., run_name="__main__") runs the script as a command: {r["main_calls"]}')

    r = run('tests/test_unknown.py')
    if r['loads'] or [u['via'] for u in r['unresolved']] != ['spec_from_file_location'] or r['sys_path_unknown'] != [2]:
        fails.append(f'a path from the environment is unresolved, never dropped or guessed: {r}')

    r = run('tests/test_plain.py')
    if any(r[k] for k in ('loads', 'sys_path_dirs', 'main_calls', 'unresolved')) or may_load(files['tests/test_plain.py']):
        fails.append(f'a file that loads nothing: {r}')

    sib = sibling_imports(ast.parse(files['scripts/install-thing.py']), 'scripts/install-thing.py', known)
    if sorted(x['path'] for x in sib) != ['scripts/helper_mod.py', 'scripts/sibling_pkg/__init__.py',
                                             'scripts/sibling_pkg/util.py']:
        fails.append(f'a script imports its siblings by bare name (never the standard library): {sib}')

    # eval_path keeps the behaviour of the impact selector's resolver
    def ev(src):
        tree = ast.parse(src)
        return eval_path(tree.body[-1].value, 'pkg/tests/unit/test_a.py', assignments(tree))
    for src, want in (
            ('Path(__file__).parent.parent / "src"', 'pkg/tests/src'),
            ('str(Path(__file__).resolve().parents[2] / "scripts")', 'pkg/scripts'),
            ('os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib")', 'pkg/tests/lib'),
            ('HERE = Path(__file__).parent\nHERE.parent', 'pkg/tests'),
            ('Path(__file__).parents[9]', None),
            ('os.environ["X"]', None),
            ('Path(__file__).resolve().parents[2].joinpath("scripts", "x-y.py")', 'pkg/scripts/x-y.py')):
        if ev(src) != want:
            fails.append(f'eval_path({src!r}) = {ev(src)!r}, want {want!r}')

    tmp = Path(tempfile.mkdtemp(prefix='pyloads-'))
    try:
        for rel, text in files.items():
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp / rel).write_text(text, encoding='utf-8')
        lf = loaded_files(tmp, 'tests/test_helper.py')
        if lf.get('reaches') != ['scripts/deep_helper.py', 'scripts/helper_mod.py', 'scripts/install-thing.py',
                                 'scripts/sibling_pkg/__init__.py', 'scripts/sibling_pkg/util.py']:
            fails.append(f'loaded_files follows sibling imports of the loaded script: {lf.get("reaches")}')
        lf = loaded_files(tmp, 'tests/test_syspath.py')
        if 'tools/gen.py' not in lf.get('reaches', []) or lf.get('conftests') != ['tests/conftest.py']:
            fails.append(f'loaded_files reads the conftest.py above the test for sys.path folders: {lf}')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print('py_loads self-test FAILED:\n  - ' + '\n  - '.join(fails))
        return 1
    print('py_loads self-test: ok')
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if '--self-test' in argv:
        return self_test()
    if len(argv) < 2 or argv[0].startswith('-'):
        print(__doc__)
        return 0 if argv[:1] in (['-h'], ['--help']) else 2
    root = Path(argv[0])
    out = {}
    for rel in argv[1:]:
        rel = rel.replace('\\', '/')
        out[rel] = loaded_files(root, rel)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
