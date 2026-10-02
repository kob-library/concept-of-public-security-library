#!/usr/bin/env python3
"""Structural QA for tom-1, which uses the pilot v0.3 schema (audit F4).

Comparable guarantees to validate_corpus.py (v0.4 volumes 2-6):
- manifest SHA-256 pins the exact source DOC/PDF bytes;
- each published Markdown body must equal the indexed section text plus its
  footnote definitions (image markup form normalized);
- retrieval chunks must reassemble each section without gaps;
- footnote indexes, PDF page bounds and preserved media stay consistent.

NOT visual verification: media/OLE review status is a release-gate concern.
"""
import argparse, hashlib, json, re, sys
from collections import defaultdict
from pathlib import Path
import yaml

RE_FM = re.compile(r'\A---\n(.*?)\n---\n', re.S)
RE_FM_BODY = re.compile(r'\A---\n.*?\n---\n\n', re.S)
RE_MD_IMG = re.compile(r'!\[[^\]]*\]\(([^)\s]+)[^)]*\)')
RE_HTML_IMG = re.compile(r'<img\s+[^>]*src="([^"]+)"[^>]*>')


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(2**20), b''):
            h.update(chunk)
    return h.hexdigest()


def loadjsonl(f):
    return [json.loads(x) for x in f.read_text(encoding='utf-8').splitlines() if x.strip()]


def norm_media_markup(t):
    t = RE_MD_IMG.sub(lambda m: 'IMG[' + m.group(1).split('/')[-1].split('#')[0].split('?')[0] + ']', t)
    t = RE_HTML_IMG.sub(lambda m: 'IMG[' + m.group(1).split('/')[-1].split('?')[0] + ']', t)
    return t


def validate_one(root):
    errors, warnings = [], []
    mp = root / 'data/manifest.json'
    if not mp.exists():
        return {'volume': 1, 'structural_test': 'FAIL', 'error_count': 1,
                'errors': ['pilot manifest missing'], 'warnings': []}
    man = json.loads(mp.read_text(encoding='utf-8'))
    prefix = 'osnovy-sociologii-tom-1'
    for ext, key in (('doc', 'source_original_sha256'), ('pdf', 'pdf_source_sha256')):
        f = root / 'source' / f'{prefix}.{ext}'
        if not f.is_file():
            errors.append(f'missing source {prefix}.{ext}')
        elif digest(f) != man.get(key):
            errors.append(f'{ext.upper()} SHA mismatch vs pilot manifest')
    sections = loadjsonl(root / 'data/sections.jsonl')
    chunks = loadjsonl(root / 'data/chunks.jsonl')
    if man.get('units_total') is not None and len(sections) != man['units_total']:
        errors.append(f"units_total {man['units_total']} != {len(sections)} section rows")
    if len({s['id'] for s in sections}) != len(sections):
        errors.append('duplicate section ids')
    grouped = defaultdict(list)
    for c in chunks:
        grouped[c['unit_id']].append(c)
    pages = man.get('pdf_page_count')
    for s in sections:
        p = root / s['path']
        if not p.exists():
            errors.append('missing section ' + s['path'])
            continue
        content = p.read_text(encoding='utf-8')
        ma = RE_FM.match(content)
        if not ma:
            errors.append('missing yaml ' + s['path'])
            continue
        meta = yaml.safe_load(ma[1])
        if meta.get('source_file_sha256') != man.get('source_original_sha256'):
            errors.append('source doc metadata mismatch ' + s['path'])
        if meta.get('source_pdf_sha256') != man.get('pdf_source_sha256'):
            errors.append('source pdf metadata mismatch ' + s['path'])
        if meta.get('pdf_page_start') != s.get('pdf_page_start'):
            errors.append('page metadata mismatch ' + s['path'])
        if pages:
            for k in ('pdf_page_start', 'pdf_page_end_inclusive'):
                pg = s.get(k)
                if pg is not None and not (1 <= pg <= pages):
                    errors.append(f'out-of-bound PDF page ({k}={pg}) ' + s['path'])
        # The location/order of Markdown footnote *definitions* is not
        # authored prose. Compare by number, but require identical bytes of
        # each definition, and identical main text. Do not silently correct.
        body = RE_FM_BODY.sub('', content, count=1)
        fnos = s.get('footnote_numbers') or []
        fdefs = s.get('footnotes') or {}
        markers = list(re.finditer(r'^\[\^(\d+)\]:', body, re.M))
        main = body[:markers[0].start()].rstrip('\n') if markers else body.rstrip('\n')
        if norm_media_markup(main) != norm_media_markup(s['text'].rstrip('\n')):
            errors.append('published Markdown main text diverges from indexed text ' + s['path'])
        md_defs = {}
        for i, mark in enumerate(markers):
            number = mark.group(1)
            if number in md_defs:
                errors.append('duplicate Markdown footnote definition ' + number + ' ' + s['path'])
            finish = markers[i+1].start() if i + 1 < len(markers) else len(body)
            md_defs[number] = body[mark.start():finish].rstrip('\n')
        if set(md_defs) != set(fdefs) or {str(n) for n in fnos} != set(fdefs):
            errors.append('footnote ID set diverges from indexed definitions ' + s['path'])
        for number in set(md_defs) & set(fdefs):
            if norm_media_markup(md_defs[number]) != norm_media_markup(fdefs[number].rstrip('\n')):
                errors.append('footnote definition mismatch ' + number + ' ' + s['path'])
        for lnk in re.findall(r'!\[[^\]]*\]\(([^)]+)\)', content):
            if re.match(r'^(?:https?://|data:)', lnk):
                continue
            if not (p.parent / lnk.split('#')[0]).exists():
                errors.append('missing local image ' + s['path'] + ' -> ' + lnk)
        cs = grouped.get(s['id'], [])
        if ''.join(c['text'] for c in cs) != s['text']:
            errors.append('loss of retrieval chunks ' + s['path'])
        at = 0
        for c in cs:
            if c['start_char_in_unit'] != at or c['end_char_in_unit'] != at + len(c['text']):
                errors.append('chunk discontinuity ' + s['path'])
            at = c['end_char_in_unit']
        if str(s.get('media_original')) != str(meta.get('original_media_items', s.get('media_original'))):
            errors.append('media metadata mismatch ' + s['path'])
    if {c['unit_id'] for c in chunks} - {s['id'] for s in sections}:
        errors.append('chunks reference unknown sections')
    media = {f.name for f in (root / 'assets/media_original').iterdir() if f.is_file()} \
        if (root / 'assets/media_original').is_dir() else set()
    if man.get('original_docx_media_items') is not None \
            and len(media) != man['original_docx_media_items']:
        errors.append(f"media_original count {len(media)} != manifest "
                      f"{man['original_docx_media_items']}")
    for s in sections:
        for m in (s.get('media_original') or []):
            if m not in media:
                errors.append(f"indexed media missing on disk: {m} ({s['path']})")
    not_inline = man.get('docx_media_without_body_use') or []
    if not_inline:
        warnings.append(f'{len(not_inline)} media not inline in Markdown; '
                        'reviewed via media queue in release gate')
    return {'volume': 1, 'structural_test': 'PASS' if not errors else 'FAIL',
            'error_count': len(errors), 'errors': errors[:30], 'warnings': warnings,
            'sections': len(sections), 'chunks': len(chunks),
            'visual_complete': False, 'publication_ready': False}


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--volumes', nargs='+', type=int, default=[1])
    # Publish preflight: warnings must block the release, not just inform.
    p.add_argument('--publish-strict', action='store_true',
                   help='treat warnings as release-blocking failures')
    a = p.parse_args()
    results = [validate_one(a.root / f'tom-{v}') for v in a.volumes]
    if a.publish_strict:
        for r in results:
            if r['warnings']:
                r['errors'] = r['errors'] + ['publish-strict: ' + w for w in r['warnings']]
                r['structural_test'] = 'FAIL'
    for r in results:
        print(json.dumps(r, ensure_ascii=False), flush=True)
    if any(x['structural_test'] == 'FAIL' for x in results):
        sys.exit(1)
