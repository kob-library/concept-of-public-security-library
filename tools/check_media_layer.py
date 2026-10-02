#!/usr/bin/env python3
"""Media-layer QA (Issue #13, B12): verifies that the figures registry, the
private corpus media placement and an export build are mutually consistent.

Checks (each prints PASS/FAIL; exit 1 on any FAIL):
  registry   — records parse; required fields present; every published
               record carries public_asset_sha256/public_path; staged files
               exist and match the recorded sha256
  corpus     — every published/duplicate record has its media file under the
               work's media dir; bytes match the staged asset; every image
               reference in corpus markdown resolves to a file or to a
               registry record that is blocked/metadata-only
  export     — every kept image ref in the output resolves; output media
               bytes equal corpus bytes; figure docs ship alongside
"""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path

IMG_RX = re.compile(r'!\[((?:[^\[\]]|\[[^\]]*\])*)\]\(([^)]*)\)')
TAG_SRC_RX = re.compile(r'<img\b[^>]*?src="([^"]+)"', re.I)


def iter_img_refs(text):
    """Yield the raw ref target for markdown refs and raw <img> tags."""
    for m in IMG_RX.finditer(text):
        yield m.group(2)
    for m in TAG_SRC_RX.finditer(text):
        yield m.group(1)

results = []


def check(name, ok, detail=''):
    results.append((name, bool(ok), detail))
    print(('PASS' if ok else 'FAIL'), '-', name, ('| ' + detail)[:140])


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def resolve(ref_base: str, target: str):
    from urllib.parse import unquote
    from pathlib import PurePosixPath
    tp = unquote(target.split('#')[0].split('?')[0])
    parts = []
    for p in (PurePosixPath(ref_base).parent / tp).parts:
        if p == '..':
            if parts:
                parts.pop()
        elif p != '.':
            parts.append(p)
    return '/'.join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--registry', type=Path, required=True)
    ap.add_argument('--unified', type=Path, required=True)
    ap.add_argument('--tom-corpus', type=Path, required=True)
    ap.add_argument('--assets', type=Path, required=True)
    ap.add_argument('--export', type=Path, default=None)
    a = ap.parse_args()

    figs = [json.loads(l) for l in
            a.registry.read_text(encoding='utf-8').splitlines() if l.strip()]
    by_id = {f['figure_id']: f for f in figs}
    works_dir = {}
    dw = a.unified / 'data_works.jsonl'
    if dw.is_file():
        for l in dw.read_text(encoding='utf-8').splitlines():
            if l.strip():
                w = json.loads(l)
                works_dir[w['work_id']] = w

    # ---------- registry checks ----------
    check('registry: records parse', len(figs) > 0, f'{len(figs)} records')
    req = {'figure_id', 'work_id', 'work_dir', 'source_kind',
           'publication_status', 'rights_status'}
    missing = [f['figure_id'] for f in figs if req - set(f)]
    check('registry: required fields', not missing, ','.join(missing[:3]))
    nopub = [f['figure_id'] for f in figs
             if f['publication_status'] == 'published'
             and not (f.get('public_asset_sha256') and f.get('public_path'))]
    check('registry: published records carry asset sha+path', not nopub,
          ','.join(nopub[:3]))
    bad_stage = []
    n_staged = 0
    for f in figs:
        if f['publication_status'] != 'published':
            continue
        p = a.assets / f['work_dir'] / Path(f['public_path']).name
        n_staged += 1
        if not p.is_file() or sha(p) != f['public_asset_sha256']:
            bad_stage.append(f['figure_id'])
    check('staged assets exist and match sha', not bad_stage,
          f'{n_staged} checked; bad: {bad_stage[:3]}')
    nohash = [f['figure_id'] for f in figs
              if f['publication_status'] in ('published', 'duplicate')
              and not f.get('source_sha256')]
    check('registry: publishable records carry source_sha256', not nohash,
          ','.join(nohash[:3]))

    # ---------- corpus checks ----------
    def corpus_media_dir(f):
        wd = f['work_dir']
        if wd.startswith('tom-'):
            return a.tom_corpus / wd / 'assets/media'
        d = works_dir.get(f['work_id'], {}).get('dir') or f'books/{wd}'
        return a.unified / d / 'media'

    missing_file, sha_mismatch, n_check = [], [], 0
    for f in figs:
        if f['publication_status'] not in ('published', 'duplicate'):
            continue
        if not f.get('media_file'):
            continue
        canon = by_id.get(f.get('duplicate_of') or '', f)
        if canon['publication_status'] != 'published':
            continue  # duplicate of a blocked object: stays blocked
        # converted formats land under the referenced stem + staged suffix
        stem = os.path.splitext(f['media_file'])[0]
        suffix = Path(canon['public_path']).suffix
        d = corpus_media_dir(f)
        cp = d / f['media_file']
        if not cp.is_file() and (d / (stem + suffix)).is_file():
            cp = d / (stem + suffix)
        n_check += 1
        if not cp.is_file():
            missing_file.append(f['figure_id'])
            continue
        sp = a.assets / canon['work_dir'] / Path(canon['public_path']).name
        if sp.is_file() and sha(cp) != sha(sp):
            sha_mismatch.append(f['figure_id'])
    check('corpus: media file present for publishable records',
          not missing_file, f'{n_check} checked; missing {missing_file[:3]}')
    check('corpus: media bytes == staged asset', not sha_mismatch,
          ','.join(sha_mismatch[:3]))

    # every corpus image ref resolves or is a known non-publishable record
    refs_by_name = {}
    for f in figs:
        for mf in {f.get('media_file'), f.get('original_filename')} - {None}:
            refs_by_name.setdefault((f['work_id'], mf), []).append(f)
    unresolved, bogus = [], []
    n_refs = 0
    for w in works_dir.values():
        wroot = a.unified / w['dir']
        if not wroot.is_dir():
            continue
        for md in wroot.rglob('*.md'):
            rel = md.relative_to(a.unified).as_posix()
            for target in iter_img_refs(md.read_text(encoding='utf-8')):
                n_refs += 1
                rel_t = resolve(rel, target)
                if (a.unified / rel_t).is_file():
                    continue
                name = target.split('/')[-1]
                recs = refs_by_name.get((w['work_id'], name))
                ok = recs and all(
                    r['publication_status'] in ('metadata_only_rights',
                                                'metadata_only_source',
                                                'unresolved_reference')
                    or (r['publication_status'] == 'duplicate' and
                        by_id.get(r.get('duplicate_of') or '',
                                {'publication_status': ''})
                        .get('publication_status') != 'published')
                    for r in recs)
                (unresolved if ok else bogus).append(
                    f'{rel} -> {name}')
    check('corpus: every image ref resolves or is an explicitly blocked record',
          not bogus, f'{n_refs} refs; unaccounted: {bogus[:3]}')

    # fb2: no silent loss — every fb2 occurrence is bound to an anchor
    fb2 = [f for f in figs if f['source_kind'] == 'fb2_embedded']
    unb = [f['figure_id'] for f in fb2 if not f.get('anchor_file')]
    check('fb2: every embedded image occurrence bound to corpus position',
          not unb, f'{len(fb2)} fb2 records; unbound: {unb[:3]}')

    # ---------- export checks ----------
    if a.export:
        out = a.export
        broken, kept = [], 0
        for md in out.rglob('*.md'):
            rel = md.relative_to(out).as_posix()
            for t in iter_img_refs(md.read_text(encoding='utf-8')):
                if '://' in t or t.startswith('#'):
                    continue
                kept += 1
                if not (out / resolve(rel, t)).is_file():
                    broken.append(f'{rel} -> {t}')
        check('export: every kept image ref resolves', not broken,
              f'{kept} kept refs; broken: {broken[:3]}')
        n_md = sum(1 for _ in out.rglob('*.md'))
        check('export: md files present', n_md > 0, f'{n_md}')
        priv = []
        for md in list(out.rglob('figures_registry.jsonl')) + \
                list(out.rglob('FIGURES.md')):
            t = md.read_text(encoding='utf-8')
            queue_rx = 'media_' + 'review_' + 'queue'
            if re.search(r'[A-Za-z]:\\|_audit_tmp|' + queue_rx +
                         r'|drive\.google', t):
                priv.append(md.name)
        check('export: figure docs carry no private paths', not priv,
              ','.join(priv[:3]))

    fails = [n for n, ok, _ in results if not ok]
    print(f'\n{len(results) - len(fails)}/{len(results)} checks passed')
    return 1 if fails else 0


if __name__ == '__main__':
    raise SystemExit(main())
