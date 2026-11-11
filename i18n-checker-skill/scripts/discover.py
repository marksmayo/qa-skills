#!/usr/bin/env python3
"""Find a repository's translation resources, work out each file's locale, and parse them all.

    python discover.py --repo examples/bookclub --out run/resources.json [--source-locale en] [--locales en,de,fr,ar]

Locale from the path: locales/de.json, de/messages.json, messages.de.json, values-de/strings.xml (values/ is
the source), de.lproj/, app_de.arb, de.po (or the PO "Language:" header). Several resource sets (a web set
and an Android set) are kept apart by `set`, so keys are compared within a set only.

The source locale is --source-locale, else `en` when present, else the locale with the most keys. The i18n
library is detected from package.json dependencies and imports (i18next, react-intl/FormatJS, vue-i18n,
angular, gettext), Android or iOS resources.

Output: {source_locale, library, sets{name: {format, source_locale, locales{code: {files, keys}}}},
entries{set: {locale: [entry]}}, duplicates[], not_parsed[]}.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import parse_resources as pr  # noqa: E402

SKIP = {'node_modules', '.git', 'dist', 'build', 'vendor', '__pycache__', 'Pods'}
LOC = r'([a-z]{2,3}(?:[-_][A-Za-z]{2,4})?)'
LIBS = [('i18next', r'i18next|react-i18next'), ('formatjs', r'react-intl|@formatjs'), ('vue-i18n', r'vue-i18n'),
        ('angular', r'@angular/localize|ngx-translate'), ('lingui', r'@lingui')]


def classify(p: Path, repo: Path) -> tuple[str, str, str] | None:
    """(format, locale, set) or None."""
    rel = p.relative_to(repo).as_posix()
    parts = rel.split('/')
    if p.name == 'strings.xml' and len(parts) >= 2 and parts[-2].startswith('values'):
        q = parts[-2][len('values'):].lstrip('-')
        loc = q.replace('-r', '-') if q else 'default'
        return 'android', loc, '/'.join(parts[:-2]) or '.'
    if p.suffix in ('.strings', '.stringsdict'):
        m = re.search(r'([A-Za-z-]+)\.lproj', rel)
        if m:
            return p.suffix.lstrip('.'), m.group(1).replace('Base', 'base'), re.sub(r'[A-Za-z-]+\.lproj/.*', '', rel) or '.'
    if p.suffix == '.xcstrings':
        return 'xcstrings', '*', rel
    if p.suffix in ('.po', '.pot'):
        if p.suffix == '.pot':
            return 'po', 'template', str(p.parent.relative_to(repo).as_posix())
        m = re.search(r'Language:\s*([\w-]+)', p.read_text(encoding='utf-8', errors='replace')[:2000])
        return 'po', (m.group(1) if m else p.stem).replace('_', '-'), str(p.parent.parent.relative_to(repo).as_posix())
    if p.suffix in ('.json', '.arb') and any(x in rel.lower() for x in ('locale', 'i18n', 'lang', 'translation', 'messages', 'l10n')) or p.suffix == '.arb':
        m = re.fullmatch(LOC, p.stem.split('.')[-1].replace('app_', '')) or re.fullmatch(LOC, p.stem.replace('app_', ''))
        if m:
            return ('arb' if p.suffix == '.arb' else 'json'), m.group(1).replace('_', '-'), p.parent.relative_to(repo).as_posix()
        m = re.fullmatch(LOC, p.parent.name)
        if m:
            return 'json', m.group(1).replace('_', '-'), p.parent.parent.relative_to(repo).as_posix() + ':' + p.stem
    if p.suffix in ('.yml', '.yaml') and 'locales' in rel:
        return 'yaml', p.stem, p.parent.relative_to(repo).as_posix()
    return None


def library(repo: Path, formats: set[str]) -> str:
    pkg = repo / 'package.json'
    deps = ''
    if pkg.is_file():
        d = json.loads(pkg.read_text(encoding='utf-8'))
        deps = ' '.join(list(d.get('dependencies', {})) + list(d.get('devDependencies', {})))
    for name, pat in LIBS:
        if re.search(pat, deps):
            return name
    if 'android' in formats:
        return 'android-resources' + (' + web (custom)' if 'json' in formats else '')
    if formats & {'strings', 'stringsdict', 'xcstrings'}:
        return 'apple-localization'
    if 'po' in formats:
        return 'gettext'
    return 'custom' if formats else 'none'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--source-locale')
    ap.add_argument('--locales')
    a = ap.parse_args()
    sets: dict[str, dict] = {}
    entries: dict[str, dict[str, list]] = {}
    dups, not_parsed = [], []
    for p in sorted(a.repo.rglob('*')):
        if not p.is_file() or set(p.relative_to(a.repo).parts) & SKIP:
            continue
        c = classify(p, a.repo)
        if not c:
            continue
        fmt, loc, set_name = c
        rel = p.relative_to(a.repo).as_posix()
        if fmt == 'yaml':
            not_parsed.append({'file': rel, 'reason': 'YAML resources are not parsed in this version'})
            continue
        if fmt == 'xcstrings':
            for l, es in pr.parse_xcstrings(p).items():
                entries.setdefault(set_name, {}).setdefault(l, []).extend(es)
                sets.setdefault(set_name, {'format': fmt, 'locales': {}})['locales'].setdefault(l, {'files': [], 'keys': 0})['files'].append(rel)
            continue
        es, ds = pr.PARSERS[fmt](p)
        for e in es:
            e['file'] = rel
        for d in ds:
            d['file'] = rel
        dups += ds
        entries.setdefault(set_name, {}).setdefault(loc, []).extend(es)
        s = sets.setdefault(set_name, {'format': fmt, 'locales': {}})
        s['locales'].setdefault(loc, {'files': [], 'keys': 0})['files'].append(rel)
    wanted = set(a.locales.split(',')) if a.locales else None
    overall_source = a.source_locale
    for name, s in sets.items():
        for loc in list(s['locales']):
            if wanted and loc not in wanted and loc not in ('default', 'template'):
                del s['locales'][loc]
                entries[name].pop(loc, None)
                continue
            s['locales'][loc]['keys'] = len(entries[name].get(loc, []))
        locs = s['locales']
        src = 'default' if 'default' in locs else 'template' if 'template' in locs else \
            (a.source_locale if a.source_locale in locs else 'en' if 'en' in locs else max(locs, key=lambda l: locs[l]['keys']))
        s['source_locale'] = src
        if src not in ('default', 'template') and not overall_source:
            overall_source = src
    overall_source = overall_source or 'en'
    for s in sets.values():
        if s['source_locale'] in ('default', 'template'):
            s['source_is'] = overall_source
    out = {'repo': a.repo.as_posix(), 'source_locale': overall_source, 'library': library(a.repo, {s['format'] for s in sets.values()}),
           'sets': sets, 'entries': entries, 'duplicates': dups, 'not_parsed': not_parsed}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'{sum(len(s["locales"]) for s in sets.values())} locale file set(s) in {len(sets)} set(s); source {overall_source}; '
          f'library {out["library"]} -> {a.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
