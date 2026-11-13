#!/usr/bin/env python3
"""Compare fixture records with the models they stand for (stdlib only).

    python schema_drift.py <repo> --inventory inventory.json [--out drift.json]

Models come from the repo's Python sources: dataclasses, pydantic-style `BaseModel` classes,
SQLAlchemy declarative classes (`Column(...)` or `Mapped[...]`), TypedDicts, and Enum classes
for enum-typed fields; JSON Schema files (`*.schema.json`) and OpenAPI JSON `components.schemas`
add more. A fixture record is matched to a model by the key or table it sits under, or else by
its file name (`appointments.yaml` -> `Appointment`). Three rules:

- extra_field: the fixture has a field the model no longer has;
- missing_required: the model requires a field (no default) the fixture lacks;
- invalid_enum: an enum-typed field holds a value that isn't one of the enum's values.

Models are scoped: a fixture is compared only with models from its own package or service (the
nearest directory with a pyproject.toml, package.json or similar), unless the repo has one root.
A record matches a model only when at least half of its keys are the model's fields, so a chart
or an extract that happens to share a file name with a model is not compared with it. Records no
model matches are counted per file as unmatched, not guessed.
"""
from __future__ import annotations
import argparse, ast, json, re, sys
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import datafiles, detect_conventions, inventory as inv  # noqa: E402

MODEL_BASES = {'BaseModel', 'Model', 'Base', 'DeclarativeBase', 'TypedDict', 'Schema', 'SQLModel'}


def singular(word: str) -> str:
    w = word.lower().replace('-', '_')
    if w.endswith('ies'):
        return w[:-3] + 'y'
    if w.endswith('sses') or w.endswith('xes') or w.endswith('ches') or w.endswith('shes'):
        return w[:-2]
    if w.endswith('s') and not w.endswith('ss'):
        return w[:-1]
    return w


def norm(name: str) -> str:
    return re.sub(r'[^a-z0-9]', '', name.lower())


def python_models(root: Path, files: list[str]) -> tuple[dict, dict]:
    models, enums = {}, {}
    for rel in files:
        if not rel.endswith('.py') or detect_conventions.is_test_file(rel) or inv.in_test_area(rel):
            continue
        try:
            tree = ast.parse((root / rel).read_text(encoding='utf-8', errors='replace'))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else '' for b in node.bases}
            if bases & {'Enum', 'IntEnum', 'StrEnum', 'Flag'}:
                vals = []
                for st in node.body:
                    if isinstance(st, ast.Assign) and isinstance(st.value, ast.Constant):
                        vals.append(st.value.value)
                enums[node.name] = vals
                continue
            is_dc = any('dataclass' in ast.dump(d) for d in node.decorator_list)
            if not (is_dc or bases & MODEL_BASES):
                continue
            fields = {}
            for st in node.body:
                if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                    ann = ast.unparse(st.annotation) if hasattr(ast, 'unparse') else ''
                    required = st.value is None and not re.search(r'Optional|None|NotRequired', ann)
                    if st.value is not None and isinstance(st.value, ast.Call) and getattr(st.value.func, 'id', getattr(st.value.func, 'attr', '')) in ('field', 'Field', 'mapped_column', 'Column'):
                        kws = {k.arg for k in st.value.keywords}
                        required = not (kws & {'default', 'default_factory', 'server_default', 'nullable'})
                    fields[st.target.id] = {'annotation': ann, 'required': required}
                elif isinstance(st, ast.Assign) and isinstance(st.value, ast.Call) and \
                        getattr(st.value.func, 'id', getattr(st.value.func, 'attr', '')) in ('Column', 'mapped_column'):
                    for t in st.targets:
                        if isinstance(t, ast.Name):
                            kws = {k.arg: k.value for k in st.value.keywords}
                            required = isinstance(kws.get('nullable'), ast.Constant) and kws['nullable'].value is False \
                                and 'default' not in kws and 'primary_key' not in kws
                            fields[t.id] = {'annotation': ast.unparse(st.value) if hasattr(ast, 'unparse') else '', 'required': required}
            if node.name == 'Meta' or not fields:
                continue
            # two services may each define a model of the same name: keep both, keyed apart
            key = node.name if node.name not in models else f'{node.name}@{rel}:{node.lineno}'
            models[key] = {'name': node.name, 'source': f'{rel}:{node.lineno}', 'fields': fields}
    for m in models.values():
        for f in m['fields'].values():
            f['enum'] = next((e for e in enums if re.search(rf'\b{e}\b', f['annotation'])), None)
    return models, enums


def schema_models(root: Path, files: list[str]) -> dict:
    out = {}
    for rel in files:
        if not rel.endswith('.json'):
            continue
        name = PurePosixPath(rel).name
        try:
            data = json.loads((root / rel).read_text(encoding='utf-8'))
        except (ValueError, UnicodeDecodeError, OSError):
            continue
        schemas = {}
        if name.endswith('.schema.json') and isinstance(data, dict) and 'properties' in data:
            schemas[data.get('title') or name[:-12]] = data
        if isinstance(data, dict) and 'openapi' in data:
            schemas.update((data.get('components') or {}).get('schemas') or {})
        for title, sch in schemas.items():
            if not isinstance(sch, dict) or 'properties' not in sch:
                continue
            req = set(sch.get('required', []))
            out[title if title not in out else f'{title}@{rel}'] = {'name': title, 'source': rel, 'fields': {k: {'annotation': json.dumps(v.get('type')), 'required': k in req,
                                                        'enum': None, 'enum_values': v.get('enum')}
                                                    for k, v in sch['properties'].items()},
                          'closed': sch.get('additionalProperties') is False}
    return out


def model_root(source: str, roots: set[str]) -> str:
    return inv.root_of(source.split(':', 1)[0], roots)


def match_model(record: dict, path: str, models: dict, roots: set[str] | None = None) -> str | None:
    """The model a record stands for: named by its key or table (or the file name), in the record's own package root,
    and sharing at least half of the record's keys."""
    here = inv.root_of(path, roots or set())
    scoped = {k: m for k, m in models.items() if not roots or model_root(m['source'], roots) == here}
    keys = set(record['fields'])
    if not keys:
        return None
    for cand in (record.get('hint'), PurePosixPath(path).stem):
        if not cand:
            continue
        for form in (cand, singular(cand)):
            named = [k for k, m in scoped.items() if norm(m.get('name', k)) == norm(form)]
            best = max(named, key=lambda k: len(keys & set(scoped[k]['fields'])), default=None)
            if best and len(keys & set(scoped[best]['fields'])) / len(keys) >= 0.5:
                return best
    return None


def drift(root: Path, inventory: dict) -> dict:
    files = inv.walk(root)
    inv.set_product_dirs(root, files)          # a product package named e2e or tests holds models, not test data
    models, enums = python_models(root, files)
    models.update(schema_models(root, files))
    roots = inv.package_roots(files)
    roots = roots if len(roots | {'.'}) > 1 else set()      # one root: every model is in scope
    findings: dict[tuple, dict] = {}
    unmatched: dict[str, int] = {}
    for item in inventory['inventory']:
        if item['kind'] not in ('fixture', 'seed', 'recording') or item['status'] != 'scanned':
            continue
        try:
            records = datafiles.load_records(root / item['path'], item['path'])
        except datafiles.NotAssessed:
            continue
        for r in records:
            model = match_model(r, item['path'], models, roots)
            if not model:
                unmatched[item['path']] = unmatched.get(item['path'], 0) + 1
                continue
            spec = models[model]
            mname = spec.get('name', model)
            mf = spec['fields']
            for fname, fv in r['fields'].items():
                if fname not in mf:
                    key = (item['path'], 'extra_field', fname, model)
                    findings.setdefault(key, {'rule': 'extra_field', 'file': item['path'], 'field': fname, 'model': mname,
                                              'model_source': spec['source'], 'lines': []})['lines'].append(fv['line'])
                    continue
                allowed = mf[fname].get('enum_values') or (enums.get(mf[fname]['enum']) if mf[fname].get('enum') else None)
                if allowed is not None and fv['value'] not in allowed:
                    key = (item['path'], 'invalid_enum', fname, model)
                    findings.setdefault(key, {'rule': 'invalid_enum', 'file': item['path'], 'field': fname, 'model': mname,
                                              'model_source': spec['source'], 'lines': [], 'allowed': allowed,
                                              'values': []})
                    findings[key]['lines'].append(fv['line'])
                    findings[key]['values'].append(fv['value'])
            for fname, meta in mf.items():
                if meta['required'] and fname not in r['fields'] and r['kind'] != 'row':
                    key = (item['path'], 'missing_required', fname, model)
                    findings.setdefault(key, {'rule': 'missing_required', 'file': item['path'], 'field': fname,
                                              'model': mname, 'model_source': spec['source'], 'lines': []})['lines'].append(r['line'])
    return {'models': {k: {'source': v['source'], 'fields': sorted(v['fields'])} for k, v in sorted(models.items())},
            'findings': list(findings.values()), 'unmatched_records': dict(sorted(unmatched.items())),
            'unmatched_total': sum(unmatched.values())}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('repo', type=Path)
    ap.add_argument('--inventory', type=Path, required=True)
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    res = drift(args.repo.resolve(), json.loads(args.inventory.read_text(encoding='utf-8')))
    text = json.dumps(res, indent=2) + '\n'
    if args.out:
        args.out.write_text(text, encoding='utf-8')
        print(f'{len(res["models"])} models, {len(res["findings"])} drift findings, {res["unmatched_total"]} unmatched records in {len(res["unmatched_records"])} files -> {args.out}')
    else:
        sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
