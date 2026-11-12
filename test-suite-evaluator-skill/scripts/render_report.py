#!/usr/bin/env python3
"""Render a Test Suite Evaluator evaluation.json as a terminal report (optional ANSI colour) or Markdown.

    python render_report.py evaluation.json [--color auto|always|never]
    python render_report.py evaluation.json --markdown report.md     (or --markdown - for stdout)

Sections follow SKILL.md Step 10: title and mode, score card, critical findings, dimension scorecard (with each
dimension's evidence, risks and recommendations), behavioural coverage and interface coverage, execution results,
recommendations by Step 9 group with acceptance criteria, evidence limitations, and an appendix of commands and
inspected artefacts.
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; MAGENTA = "\033[35m"; CYAN = "\033[36m"

REC_GROUPS = [('critical_risk', 'Critical confidence risks'), ('top_improvement', 'Highest-leverage improvements'),
              ('quick_win', 'Quick wins'), ('structural', 'Structural improvements'),
              ('evidence_needed', 'Evidence needed for a stronger reassessment')]
CELLS = [('happy_path', 'Happy'), ('negative', 'Negative'), ('boundary', 'Boundary'), ('permissions', 'Permissions'),
         ('concurrency', 'Concurrency'), ('dependency_failure', 'Dep. failure'), ('recovery', 'Recovery')]
CELL_MARK = {'covered': '✓ covered', 'partial': '◐ partial', 'not_covered': '✗ none', 'not_applicable': '– n/a',
             'unknown': '? unknown'}
DECIMALS_NOTE = 'Weighted scores use one decimal; differences of a few tenths are not materially significant.'


def supports_color(mode: str) -> bool:
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty() and os.environ.get('TERM', '') != 'dumb' and 'NO_COLOR' not in os.environ


def score_style(score):
    if score is None:
        return DIM
    if score >= 8:
        return GREEN
    if score >= 6.5:
        return CYAN
    if score >= 5:
        return YELLOW
    if score >= 3:
        return MAGENTA
    return RED


def bar(score, width=20):
    if score is None:
        return '·' * width
    filled = max(0, min(width, round(score / 10 * width)))
    return '█' * filled + '░' * (width - filled)


def fmt(v):
    return 'not assessed' if v is None else f'{v:.1f}/10'


def ev_text(e):
    if isinstance(e, dict):
        tag = f"[{e['id']}] " if e.get('id') else ''
        return f"{tag}{e['text']} ({e['level'].replace('_', ' ')})"
    return str(e)


def details(item):
    d = (item.get('details') or '').strip()
    return f"{item['status'].replace('_', ' ')}: {d}" if d else item['status'].replace('_', ' ')


class Out:
    """Collects lines for either format so each section is written once."""

    def __init__(self, md: bool, color: bool):
        self.md, self.color, self.lines = md, color and not md, []

    def paint(self, text, code):
        return f'{code}{text}{RESET}' if self.color else text

    def h1(self, text):
        if self.md:
            self.lines += [f'# {text}', '']
        else:
            self.lines += [self.paint(text, BOLD), '=' * len(text)]

    def h2(self, text):
        if self.md:
            self.lines += ['', f'## {text}', '']
        else:
            self.lines += ['', self.paint(text.upper(), BOLD)]

    def h3(self, text, code=''):
        if self.md:
            self.lines += ['', f'### {text}', '']
        else:
            self.lines.append(self.paint(text, code or BOLD))

    def gap(self):
        if self.md and self.lines and self.lines[-1] != '':
            self.lines.append('')

    def p(self, text, code=''):
        if self.md:
            self.gap()
            self.lines += [text, ''] if text else []
        else:
            self.lines.append(self.paint(text, code) if code else text)

    def li(self, text, depth=0):
        self.lines.append(('  ' * depth if self.md else '  ' * (depth + 1)) + '- ' + text)

    def table(self, head, rows):
        if self.md:
            self.gap()
            esc = lambda s: str(s).replace('|', '\\|').replace('\n', ' ')  # noqa: E731
            self.lines.append('| ' + ' | '.join(head) + ' |')
            self.lines.append('|' + '|'.join('---' for _ in head) + '|')
            for r in rows:
                self.lines.append('| ' + ' | '.join(esc(c) for c in r) + ' |')
            self.lines.append('')
        else:
            widths = [max(len(str(x)) for x in [h] + [r[i] for r in rows]) for i, h in enumerate(head)]
            self.lines.append('  ' + '  '.join(str(h).ljust(w) for h, w in zip(head, widths)))
            for r in rows:
                self.lines.append('  ' + '  '.join(str(c).ljust(w) for c, w in zip(r, widths)))

    def text(self):
        out = []
        for line in self.lines:
            if line == '' and (not out or out[-1] == ''):
                continue
            out.append(line)
        return '\n'.join(out).rstrip() + '\n'


def render(data: dict, md: bool, color: bool) -> str:
    o = Out(md, color)
    meta, head = data['metadata'], data['headline']
    o.h1(f"Test suite evaluation — {meta['repository']}")
    execution = meta.get('execution', 'unknown').replace('_', ' ')
    o.p(f"Mode: {meta['assessment_mode'].replace('_', ' ')}  |  Execution: {execution}  |  "
        f"Confidence: {head['overall_confidence']}  |  Evidence completeness: {head['evidence_completeness']:.1f}%")
    o.p(f"Skill version {meta.get('skill_version', 'not recorded')}, generated {meta.get('generated_at', '')}")
    if meta.get('provisional'):
        o.p('PROVISIONAL: no execution evidence supports these scores.', MAGENTA)

    # score card
    o.h2('Score card')
    overall, raw = head['overall_score'], head['raw_weighted_score']
    o.p(f"Overall: {overall:.1f}/10  {bar(overall)}  {head['rating_band']}", score_style(overall))
    capped = abs(raw - overall) > 1e-9
    o.p(f"Raw weighted score: {raw:.1f}/10" + (f" (capped to {overall:.1f})" if capped else ' (no cap lowers it)'))
    rows = [(name, fmt(head.get(key))) for key, name in (
        ('testing_effectiveness', 'Testing effectiveness'), ('suite_trustworthiness', 'Suite trustworthiness'),
        ('engineering_quality', 'Engineering quality'), ('operational_health', 'Operational health'))]
    if md:
        o.lines.append('')
        o.table(['Group', 'Score'], rows)
    else:
        for name, shown in rows:
            v = head.get({'Testing effectiveness': 'testing_effectiveness', 'Suite trustworthiness': 'suite_trustworthiness',
                          'Engineering quality': 'engineering_quality', 'Operational health': 'operational_health'}[name])
            o.p(f"  {name:22} {shown}", score_style(v))
    caps = head.get('applied_caps', [])
    if caps:
        for cap in caps:
            line = f"cap {cap['rule']} (max {cap['limit']:g}): {cap['explanation']}"
            if md:
                o.li(line)
            else:
                o.p('! ' + line, MAGENTA)
    else:
        o.p('No caps applied: no cap condition holds.')
    o.p(DECIMALS_NOTE, DIM)

    # findings
    findings = data.get('critical_findings', [])
    crit = [f for f in findings if f['severity'] == 'critical']
    other = [f for f in findings if f['severity'] != 'critical']
    for title, group in (('Critical findings', crit), ('Other serious findings', other)):
        if not group:
            if title == 'Critical findings':
                o.h2(title)
                o.p('None recorded at critical severity.')
            continue
        o.h2(title)
        for f in group:
            sev = f['severity'].upper()
            cat = f.get('category', 'other')
            tag = '' if cat == 'other' else f" ({cat.replace('_', ' ')})"
            o.h3(f"[{sev}] {f['title']}{tag}", RED if sev in ('CRITICAL', 'HIGH') else YELLOW)
            if f.get('impact'):
                o.p(('' if md else '  ') + f['impact'])
            for e in f.get('evidence', []):
                o.li(e)
            if f.get('remediated'):
                o.li('remediated')

    # dimensions
    o.h2('Dimension scorecard')
    if md:
        o.table(['Dimension', 'Weight', 'Score', 'Confidence', 'Evidence level'],
                [(d['name'], f"{d['weight']:g}", 'not assessed' if d['score'] is None else f"{d['score']}/10",
                  d['confidence'], d['evidence_level'].replace('_', ' ')) for d in data['dimensions']])
    width = max(len(d['name']) for d in data['dimensions'])
    for d in data['dimensions']:
        score = d.get('score')
        shown = 'N/A' if score is None else f'{score}/10'
        if md:
            o.h3(f"{d['name']} — {'not assessed' if score is None else f'{score}/10'}")
            o.p(f"Weight {d['weight']:g}, confidence {d['confidence']}, evidence {d['evidence_level'].replace('_', ' ')}.")
            o.p('')
            o.p(d['finding'])
        else:
            o.p(f"{d['name']:{width}} {shown:>5} {bar(score, 12)}  {d['confidence']}/{d['evidence_level']}",
                score_style(score))
            o.p('  ' + d['finding'])
        if d.get('applicable') is False:
            o.li('not applicable: ' + d.get('not_applicable_reason', ''))
        for key, label in (('evidence', 'Evidence'), ('risks', 'Risk'), ('recommendations', 'Recommendation')):
            for e in d.get(key, []):
                o.li(f"{label}: {ev_text(e)}")

    # behavioural coverage
    bc = data.get('behavioural_coverage', [])
    o.h2('Behavioural coverage (test-derived unless noted)')
    if bc:
        o.table(['Feature'] + [c for _, c in CELLS] + ['Notes'],
                [[b['feature']] + [CELL_MARK.get(b.get(k), '? unknown') for k, _ in CELLS] + [b.get('notes', '')]
                 for b in bc])
    else:
        o.p('No behavioural coverage matrix recorded.')
    ic = data.get('interface_coverage', [])
    if ic:
        o.h2('Interface coverage (not code coverage)')
        o.table(['Interface', 'Kind', 'Tested', 'Evidence', 'Method'],
                [(b['interface'], b['kind'], f"{b['tested']}/{b['total']}", b['evidence_level'].replace('_', ' '),
                  b['method']) for b in ic])
        for b in ic:
            if b.get('untested'):
                # a group entry ({"group": "...", "count": n}) stands for n interfaces
                n = sum(u.get('count', 1) if isinstance(u, dict) else 1 for u in b['untested'])
                o.p(f"{b['interface']}: {n} untested")
                for u in b['untested']:
                    o.li(f"{u['group']} ({u['count']})" if isinstance(u, dict) else u)
            if b.get('notes'):
                o.p(b['notes'])

    # execution
    er = meta.get('execution_result')
    if er:
        o.h2('Execution results')
        o.table(['Layer', 'Framework', 'Status', 'Reason'],
                [(l['name'], l.get('framework', ''), l['status'].replace('_', ' '), l.get('reason', ''))
                 for l in er['layers']])
        if er['runs']:
            if not md:
                o.p('')
            o.table(['Run', 'Origin', 'Layers', 'Passed', 'Failed', 'Skipped', 'Errored', 'Duration'],
                    [(r['id'], r['origin'].replace('_', ' '), ', '.join(r['layers']), r['passed'], r['failed'],
                      r['skipped'], r['errored'],
                      '' if r.get('duration_seconds') is None else f"{r['duration_seconds']:g} s"
                      + (' (coverage)' if r.get('coverage') else '')) for r in er['runs']])
        fails = er.get('failures', [])
        if fails:
            o.p('Failures by class:')
            for cls in ('product', 'test', 'environment', 'unknown'):
                group = [f for f in fails if f['class'] == cls]
                if not group:
                    continue
                o.li(f"{cls}: {sum(f.get('count', 1) for f in group)}")
                n_runs = len(er['runs'])
                for f in group:
                    seen = f.get('runs_failed')
                    flaky = f" — failed in {len(seen)} of {n_runs} runs" if seen else ''
                    count = f" (x{f['count']})" if f.get('count', 1) > 1 else ''
                    o.li(f"{f['test']}{count}{flaky}: {f['evidence']}", 1)

    # recommendations
    recs = sorted(data.get('recommendations', []), key=lambda x: x.get('priority', 999))
    o.h2('Recommendations')
    for cat, label in REC_GROUPS:
        group = [r for r in recs if r.get('category') == cat]
        if not group:
            continue
        o.h3(label)
        for r in group:
            dims = ', '.join(r.get('dimensions_affected', []))
            kinds = ', '.join(r.get('evidence_kinds', []))
            extra = f"; dimensions: {dims}" if dims else ''
            extra += f"; evidence: {kinds}" if kinds else ''
            o.p(f"{r['priority']}. {o.paint(r['title'], BOLD)} [{r['impact']} impact / {r['effort']} effort{extra}]")
            o.p(('   ' if not md else '   ') + r['action'])
            for a in r.get('acceptance_criteria', []):
                o.li('accept when: ' + a, 1)
    uncategorised = [r for r in recs if r.get('category') not in dict(REC_GROUPS)]
    for r in uncategorised:
        o.p(f"{r['priority']}. {r['title']}: {r['action']}")

    # limitations
    lacking = [x for x in data.get('evidence_inventory', []) if x['status'] in ('missing', 'partial')]
    o.h2('Evidence limitations')
    if lacking:
        for item in lacking:
            o.li(f"{item['name']}: {details(item)}")
    else:
        o.p('No inventory item is missing or partial.')
    if meta.get('notes'):
        o.p('Notes:')
        for n in meta['notes']:
            o.li(n)

    # appendix
    o.h2('Appendix: commands and inspected artefacts')
    cmds = data.get('commands', [])
    if cmds:
        o.table(['Command', 'Exit', 'Duration', 'Summary'],
                [((f"`{c.get('command', '')}`" if md else c.get('command', '')),
                  '' if c.get('exit_code') is None else c['exit_code'],
                  '' if c.get('duration_seconds') is None else f"{c['duration_seconds']:g} s",
                  c.get('summary', '') + (f" [{c['environment']}]" if c.get('environment') else ''))
                 for c in cmds])
    else:
        o.p('No commands recorded.')
    o.p('')
    o.table(['Evidence', 'Kind', 'Status'],
            [(x['name'], x['kind'], details(x)) for x in data.get('evidence_inventory', [])])
    return o.text()


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # UTF-8 whatever the locale; never crash on a character
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('json_file', type=Path)
    ap.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    ap.add_argument('--markdown', metavar='FILE', help='write a Markdown report to FILE (- for stdout)')
    args = ap.parse_args(argv)
    try:
        data = json.loads(args.json_file.read_text(encoding='utf-8-sig'))
    except Exception as exc:
        print(f'Unable to read evaluation: {exc}', file=sys.stderr)
        return 2
    if args.markdown:
        text = render(data, md=True, color=False)
        if args.markdown == '-':
            sys.stdout.write(text)
        else:
            Path(args.markdown).write_text(text, encoding='utf-8')
            print(f'wrote {args.markdown}')
        return 0
    sys.stdout.write(render(data, md=False, color=supports_color(args.color)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
