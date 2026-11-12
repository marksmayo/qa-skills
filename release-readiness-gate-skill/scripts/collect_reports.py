#!/usr/bin/env python3
"""Inventory a release candidate's reports: hash, detect, validate, check freshness, extract facts.

    python collect_reports.py --reports rc/reports.json --candidate rc/candidate.json --policy rc/release_policy.json \
        [--repo <git checkout>] [--skills-root <dir with the other skills>] --out collection.json

--reports is a directory or a manifest. A directory: every file under it is listed, recognised reports
with their type, JUnit XML as `ci-junit`, anything else as `unrecognised` (never ignored);
`<report>.provenance.json` sidecars are read, not listed. A manifest (JSON, {"reports": [{"path": ...,
"provenance": ...}]}, paths relative to the manifest) lists reports where they were produced, so each
skill's validator finds the evidence beside its report.

Validation: each report is checked with its own skill's validator, found at
<skills-root>/<skill>-skill/scripts/<validator> (default skills root: the directory this skill is in),
with the arguments the policy gives in validator_args[<type>]. The command and exit code are recorded;
a missing validator is `not_run`. JUnit is valid when it parses.

Freshness: a report is fresh when its provenance (the report's own `subject` block, else the sidecar)
names the candidate's commit, and its build id and target URL (when both sides give one) match.
Otherwise it is stale (commit_mismatch, build_mismatch, target_mismatch, generated_before_commit),
or unverified when there is no provenance. With --repo, a stale report's commit is checked against the
candidate: commits between them and the files they changed are listed. Two reports of the same type:
the freshest counts and the others are `superseded`.

Conditional requirements (`if` in the policy) are evaluated from the files changed since the
candidate's previous_tag (needs --repo); with no tag or no repo they are treated as true.
"""
from __future__ import annotations
import argparse, fnmatch, hashlib, json, os, shlex, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from adapters import TYPES, detect, facts_for  # noqa: E402


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes().replace(b'\r\n', b'\n')).hexdigest()  # line endings normalised


def posix(p: Path) -> str:
    try:
        return Path(os.path.relpath(p)).as_posix()
    except ValueError:  # another drive on Windows
        return p.as_posix()


def repo_label(repo: Path | None) -> str | None:
    """The repository as recorded: relative when it is under the working directory, else only its HEAD (no local path)."""
    if not repo:
        return None
    rel = posix(repo)
    if not rel.startswith('..') and not Path(rel).is_absolute():
        return rel
    head = git(repo, 'rev-parse', 'HEAD') if (repo / '.git').exists() else None
    return f'(outside the working directory; HEAD {head})' if head else '(outside the working directory)'


def inventory(reports: Path) -> list[tuple[Path, Path | None]]:
    """(report, explicit provenance or None) for a directory or a manifest."""
    if reports.is_dir():
        return [(x, None) for x in sorted(reports.rglob('*')) if x.is_file() and not x.name.endswith('.provenance.json')]
    m = json.loads(reports.read_text(encoding='utf-8'))
    base = reports.parent
    return [(Path(os.path.normpath(base / r['path'])), Path(os.path.normpath(base / r['provenance'])) if r.get('provenance') else None) for r in m['reports']]


def git(repo: Path, *args) -> str | None:
    r = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def parse_time(s) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def provenance(path: Path, facts: dict | None, explicit: Path | None = None) -> dict:
    subj = (facts or {}).get('subject')
    if isinstance(subj, dict) and subj.get('commit'):
        return dict(subj, source='report')
    side = explicit or path.with_name(path.name + '.provenance.json')
    if side.exists():
        return dict(json.loads(side.read_text(encoding='utf-8')), source=posix(side), source_sha256=sha256(side))
    return {'source': None}


def run_validator(path: Path, name: str, skills_root: Path, policy: dict) -> dict:
    if name == 'ci-junit':
        return {'command': 'parse_results.parse_junit', 'exit_code': None, 'status': 'valid', 'errors': []}
    t = TYPES['types'][name]
    script = skills_root / f'{t.get("skill_dir", name.split(":")[0])}-skill' / 'scripts' / t['validator']
    args = shlex.split(policy.get('validator_args', {}).get(name, ''))
    cmd = ['python', posix(script), posix(path), *args]
    if not script.exists():
        return {'command': ' '.join(cmd), 'exit_code': None, 'status': 'not_run', 'errors': [f'validator not found at {posix(script)}']}
    r = subprocess.run([sys.executable, str(script), str(path), *args], capture_output=True, text=True, encoding='utf-8', errors='replace')
    lines = [l.strip() for l in (r.stdout + r.stderr).splitlines() if l.strip()]
    errs = [l.lstrip('- ').strip() for l in lines if l.startswith('-')][:5] or lines[:3]
    return {'command': ' '.join(cmd), 'exit_code': r.returncode, 'status': 'valid' if r.returncode == 0 else 'invalid', 'errors': [] if r.returncode == 0 else errs}


def freshness(prov: dict, cand: dict, cand_time: datetime | None, repo: Path | None) -> dict:
    if not prov.get('commit'):
        return {'freshness': 'unverified', 'reasons': ['no provenance: the report has no subject block and no sidecar provenance.json']}
    reasons = []
    if prov['commit'] != cand['commit'] and not (len(prov['commit']) >= 7 and cand['commit'].startswith(prov['commit'])):
        reasons.append('commit_mismatch')
    if prov.get('build_id') and cand.get('build_id') and prov['build_id'] != cand['build_id']:
        reasons.append('build_mismatch')
    if prov.get('target_url') and cand.get('target_url') and prov['target_url'].rstrip('/') != cand['target_url'].rstrip('/'):
        reasons.append('target_mismatch')
    gen = parse_time(prov.get('generated_at'))
    if gen and cand_time and gen < cand_time and not reasons:
        reasons.append('generated_before_commit')
    out = {'freshness': 'stale' if reasons else 'fresh', 'reasons': reasons}
    if 'commit_mismatch' in reasons and repo:
        if git(repo, 'merge-base', '--is-ancestor', prov['commit'], cand['commit']) is not None:
            out['commits_behind'] = int(git(repo, 'rev-list', '--count', f'{prov["commit"]}..{cand["commit"]}') or 0)
            out['changed_since'] = (git(repo, 'diff', '--name-only', prov['commit'], cand['commit']) or '').splitlines()
        else:
            out['note'] = 'the report commit is not an ancestor of the candidate'
    return out


def conditions(policy: dict, cand: dict, repo: Path | None) -> dict:
    out = {}
    tag = cand.get('previous_tag')
    files = None
    if repo and tag:
        diff = git(repo, 'diff', '--name-only', tag, cand['commit'])
        files = diff.splitlines() if diff is not None else None
    for name, c in policy.get('conditions', {}).items():
        if files is None:
            out[name] = {'value': True, 'basis': 'no previous_tag or repository: treated as true', 'files': []}
            continue
        hit = sorted({f for f in files for pat in c['paths'] if fnmatch.fnmatch(f, pat)})
        out[name] = {'value': bool(hit), 'basis': f'files changed since {tag}', 'files': hit}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--reports', type=Path, required=True)
    ap.add_argument('--candidate', type=Path, required=True)
    ap.add_argument('--policy', type=Path, required=True)
    ap.add_argument('--waivers', type=Path)
    ap.add_argument('--repo', type=Path)
    ap.add_argument('--skills-root', type=Path, default=HERE.parent.parent)
    ap.add_argument('--no-validate', action='store_true', help='record every validator as not_run (for a dry inventory)')
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    cand = json.loads(a.candidate.read_text(encoding='utf-8'))
    policy = json.loads(a.policy.read_text(encoding='utf-8'))
    repo = a.repo if a.repo and (a.repo / '.git').exists() else None
    cand_time = parse_time(git(repo, 'show', '-s', '--format=%cI', cand['commit'])) if repo else parse_time(cand.get('commit_time'))
    skip = {p.resolve() for p in (a.candidate, a.policy, a.waivers, a.out) if p}
    sources = []
    for p, explicit in inventory(a.reports):
        if p.resolve() in skip:
            continue
        if not p.exists():
            sources.append({'path': posix(p), 'sha256': None, 'type': 'missing_file', 'skill': 'unrecognised', 'skill_version': None, 'provenance': {'source': None},
                            'validator': {'command': None, 'exit_code': None, 'status': 'not_run', 'errors': ['listed in the manifest but not found']},
                            'freshness': 'unverified', 'freshness_reasons': ['file not found'], 'facts': None, 'superseded': False})
            continue
        name, _ = detect(p)
        facts = facts_for(p) if name else None
        prov = provenance(p, facts, explicit)
        src = {'path': posix(p), 'sha256': sha256(p), 'type': name or 'unrecognised', 'skill': (facts or {}).get('skill') or 'unrecognised',
               'skill_version': (facts or {}).get('skill_version'), 'provenance': prov}
        if not name:
            src.update(validator={'command': None, 'exit_code': None, 'status': 'not_run', 'errors': ['not a report type the gate recognises']},
                       freshness='unverified', freshness_reasons=['unrecognised file'], facts=None, superseded=False)
            sources.append(src)
            continue
        src['validator'] = ({'command': None, 'exit_code': None, 'status': 'not_run', 'errors': ['--no-validate']} if a.no_validate
                            else run_validator(p, name, a.skills_root, policy))
        fr = freshness(prov, cand, cand_time, repo)
        src.update(freshness=fr['freshness'], freshness_reasons=fr['reasons'], **{k: fr[k] for k in ('commits_behind', 'changed_since', 'note') if k in fr})
        src['generated_at'] = prov.get('generated_at') or facts.get('generated_at')
        src['facts'] = facts
        src['superseded'] = False
        sources.append(src)
        print(f'{src["type"]:28} {src["validator"]["status"]:8} {src["freshness"]:10} {posix(p)}', flush=True)
    rank = {'fresh': 0, 'stale': 1, 'unverified': 2}
    by_type: dict = {}
    for s in sources:
        if s['type'] not in ('unrecognised', 'missing_file'):
            by_type.setdefault(s['type'], []).append(s)
    for group in by_type.values():
        group.sort(key=lambda s: (s['validator']['status'] != 'valid', rank[s['freshness']], -(parse_time(s.get('generated_at')) or datetime.min.replace(tzinfo=timezone.utc)).timestamp()))
        for s in group[1:]:
            s['superseded'] = True
    out = {'collected_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'candidate': cand,
           'candidate_commit_time': cand_time.isoformat() if cand_time else None,
           'reports': posix(a.reports), 'reports_kind': 'directory' if a.reports.is_dir() else 'manifest', 'repo': repo_label(a.repo), 'repo_used': bool(repo),
           'policy': {'path': posix(a.policy), 'sha256': sha256(a.policy), 'starter': bool(policy.get('starter'))},
           'waivers': {'path': posix(a.waivers), 'sha256': sha256(a.waivers)} if a.waivers and a.waivers.exists() else None,
           'conditions': conditions(policy, cand, repo), 'sources': sources}
    a.out.write_text(json.dumps(out, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(f'{len(sources)} file(s) -> {a.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
