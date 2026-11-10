#!/usr/bin/env python3
"""Read and write SARIF 2.1.0 (stdlib only).

Reading normalises results from any SARIF-emitting tool (Semgrep, detekt, SwiftLint, Android Lint,
Bandit, gosec, ...) into flat records an agent can triage:

    {tool, version, rule_id, level, message, file, line, column, end_line, snippet, fingerprint,
     tags, cwe}

`tags` are the rule's and result's property tags; `cwe` lists the CWE ids found in them, in the
rule's `cwe` property, or in CWE taxonomy relationships (as gosec writes them).

Writing builds a minimal valid SARIF log from findings, for upload to code-scanning dashboards.

    python sarif.py read results.sarif [more.sarif ...] [--root .] [--out results.json]

Paths are made relative to --root when they fall under it, and file:// URIs are decoded.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
from urllib.parse import unquote, urlparse

LEVELS = ('error', 'warning', 'note', 'none')
CWE_RE = re.compile(r'\bCWE[-_ :]?(\d{1,4})\b', re.I)

def _cwes(*sources) -> list[str]:
    found: list[str] = []
    for src in sources:
        for text in (src if isinstance(src, list) else [src]):
            for n in CWE_RE.findall(str(text or '')):
                cwe = f'CWE-{int(n)}'
                if cwe not in found:
                    found.append(cwe)
    return found

def _relationship_cwes(rule: dict) -> list[str]:
    out = []
    for rel in rule.get('relationships') or []:
        target = rel.get('target') or {}
        comp = ((target.get('toolComponent') or {}).get('name') or '').upper()
        if comp == 'CWE' and str(target.get('id', '')).isdigit():
            out.append(f"CWE-{int(target['id'])}")
    return out

def _uri_to_path(uri: str) -> str:
    if uri.startswith('file:'):
        p = unquote(urlparse(uri).path)
        if len(p) > 2 and p[0] == '/' and p[2] == ':':  # /C:/x on Windows
            p = p[1:]
        return p
    return unquote(uri)

def _relative(path: str, root: Path | None) -> str:
    if not root or not path:
        return path.replace('\\', '/')
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return path.replace('\\', '/')

def _message(msg: dict, rule: dict) -> str:
    text = (msg or {}).get('text') or (msg or {}).get('markdown') or ''
    if not text and (msg or {}).get('id'):
        templ = ((rule.get('messageStrings') or {}).get(msg['id']) or {}).get('text', '')
        args = (msg or {}).get('arguments') or []
        text = templ
        for i, a in enumerate(args):
            text = text.replace('{%d}' % i, str(a))
    return ' '.join(text.split())

def read(path: Path, root: Path | None = None) -> list[dict]:
    """Normalise every result in one SARIF file."""
    root = Path(root) if root else None
    log = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    out: list[dict] = []
    for run in log.get('runs') or []:
        driver = ((run.get('tool') or {}).get('driver') or {})
        tool, version = driver.get('name', 'unknown'), driver.get('semanticVersion') or driver.get('version')
        rules = driver.get('rules') or []
        rule_by_id = {r.get('id'): r for r in rules if r.get('id')}
        bases = {k: v.get('uri', '') for k, v in (run.get('originalUriBaseIds') or {}).items()}
        for res in run.get('results') or []:
            rid = res.get('ruleId')
            if rid is None and isinstance(res.get('ruleIndex'), int) and res['ruleIndex'] < len(rules):
                rid = rules[res['ruleIndex']].get('id')
            rule = rule_by_id.get(rid, {})
            level = res.get('level') or ((rule.get('defaultConfiguration') or {}).get('level')) or 'warning'
            locs = res.get('locations') or [{}]
            for loc in locs:
                phys = loc.get('physicalLocation') or {}
                art = phys.get('artifactLocation') or {}
                uri = art.get('uri', '')
                base = bases.get(art.get('uriBaseId', ''), '')
                file = _uri_to_path(base + uri if base and not uri.startswith('file:') else uri)
                region = phys.get('region') or {}
                fp = res.get('fingerprints') or res.get('partialFingerprints') or {}
                rprops, props = rule.get('properties') or {}, res.get('properties') or {}
                tags = [str(x) for x in (rprops.get('tags') or []) + (props.get('tags') or [])]
                cwe = _cwes(tags, rprops.get('cwe') or [], props.get('cwe') or [], _relationship_cwes(rule))
                out.append({
                    'tool': tool, 'version': version, 'rule_id': rid, 'level': level if level in LEVELS else 'warning',
                    'message': _message(res.get('message') or {}, rule),
                    'file': _relative(file, root), 'line': region.get('startLine'), 'column': region.get('startColumn'),
                    'end_line': region.get('endLine'), 'snippet': ((region.get('snippet') or {}).get('text') or '').strip()[:300],
                    'fingerprint': next(iter(fp.values()), None), 'tags': tags, 'cwe': cwe,
                })
    return out

def build(tool: str, version: str | None, results: list[dict], info_uri: str | None = None) -> dict:
    """Build a SARIF log. Each result: {rule_id, level, message, file, line, column?, rule_name?, help?,
    properties?, rule_properties?}. rule_properties (e.g. tags, security-severity for GitHub code
    scanning) are taken from the first result of each rule."""
    rules: dict[str, dict] = {}
    for r in results:
        rid = r['rule_id']
        if rid not in rules:
            rule = {'id': rid, 'shortDescription': {'text': r.get('rule_name') or rid}}
            if r.get('help'):
                rule['help'] = {'text': r['help']}
            if r.get('rule_properties'):
                rule['properties'] = r['rule_properties']
            rules[rid] = rule
    index = {rid: i for i, rid in enumerate(rules)}
    driver = {'name': tool, 'rules': list(rules.values())}
    if version:
        driver['semanticVersion'] = version
    if info_uri:
        driver['informationUri'] = info_uri
    sarif_results = []
    for r in results:
        region = {'startLine': int(r.get('line') or 1)}
        if r.get('column'):
            region['startColumn'] = int(r['column'])
        item = {'ruleId': r['rule_id'], 'ruleIndex': index[r['rule_id']],
                'level': r.get('level') if r.get('level') in LEVELS else 'warning',
                'message': {'text': r.get('message') or r['rule_id']},
                'locations': [{'physicalLocation': {'artifactLocation': {'uri': str(r.get('file') or '').replace('\\', '/')},
                                                    'region': region}}]}
        if r.get('properties'):
            item['properties'] = r['properties']
        sarif_results.append(item)
    return {'$schema': 'https://json.schemastore.org/sarif-2.1.0.json', 'version': '2.1.0',
            'runs': [{'tool': {'driver': driver}, 'results': sarif_results}]}

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('read', help='normalise SARIF results to JSON')
    r.add_argument('files', nargs='+', type=Path)
    r.add_argument('--root', type=Path, help='make paths relative to this directory')
    r.add_argument('--out', type=Path)
    args = ap.parse_args()
    results: list[dict] = []
    for f in args.files:
        try:
            results.extend(read(f, args.root))
        except (OSError, ValueError) as exc:
            print(f'Unable to read {f}: {exc}', file=sys.stderr)
            return 2
    text = json.dumps(results, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + '\n', encoding='utf-8')
        print(f'{len(results)} result(s) -> {args.out}')
    else:
        print(text)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
