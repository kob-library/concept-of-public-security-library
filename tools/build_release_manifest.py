#!/usr/bin/env python3
"""Build a release manifest candidate for the public library.

Enumerates exactly which files would ship (per volume: source DOC/PDF hashes,
index hashes, media inclusion status), what is excluded and why, the review
status per item, and known limitations. The manifest is a *candidate*: it does
not grant approval and is not proof of human review.
"""
import argparse, hashlib, json
from pathlib import Path

VOLUMES = range(1, 7)
# Files/dirs that must never reach the public artifact.
EXCLUDED_SOURCE_PDFS = {
    'mertvaia-voda-red-2015.pdf': 'separate scope: edition referenced by a 2004 court ruling; not part of the six-volume release',
    'osnovy-sociologii-tom-3.pdf': 'duplicate evidence copy inside tom-1/source; canonical copy ships in tom-3',
    'ot-chelovekoobraziya-2001.pdf': 'reference edition for comparison, not in the declared release scope',
}

AUTHOR_NOTICE = (
    'Авторское ©-уведомление в исходных документах предоставляет каждому желающему '
    'полное право копировать и тиражировать материалы целиком или фрагментарно, '
    'в том числе в коммерческих целях; ответственность за искажение смысла при '
    'фрагментарном цитировании авторы возлагают на использующего материалы. '
    'Уведомление сохраняется как исторический факт, не подменяется лицензиями '
    'MIT/CC и не распространяется на произведения третьих лиц (обложки, репродукции).'
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(2 ** 20), b''):
            h.update(chunk)
    return h.hexdigest()


def jsonl(path: Path):
    return [json.loads(x) for x in path.read_text(encoding='utf-8').splitlines() if x.strip()]


def build_manifest(corpus: Path, review_dir: Path) -> dict:
    approval_file = review_dir / 'release_approval.json'
    approval = json.loads(approval_file.read_text(encoding='utf-8')) if approval_file.is_file() else {}
    pins = approval.get('source_pins') or {}
    media_rows = jsonl(review_dir / 'media_review_queue.jsonl') \
        if (review_dir / 'media_review_queue.jsonl').is_file() else []
    ole_rows = jsonl(review_dir / 'ole_review_queue.jsonl') \
        if (review_dir / 'ole_review_queue.jsonl').is_file() else []
    volumes = []
    for v in VOLUMES:
        book = corpus / f'tom-{v}'
        vol = {'volume': v, 'present': book.is_dir(), 'source_files': {},
               'index_files': {}, 'media': {}, 'excluded_files': []}
        src = book / 'source'
        if src.is_dir():
            canonical = f'osnovy-sociologii-tom-{v}.pdf'
            for f in sorted(src.iterdir()):
                if f.is_file():
                    if f.suffix.lower() == '.pdf' and f.name != canonical:
                        vol['excluded_files'].append(
                            {'file': f'source/{f.name}',
                             'reason': EXCLUDED_SOURCE_PDFS.get(
                                 f.name, 'not in the declared release scope'),
                             'sha256': sha256(f)})
                    else:
                        vol['source_files'][f.name] = {
                            'sha256': sha256(f), 'bytes': f.stat().st_size,
                            'pinned_sha256': (pins.get(f'tom-{v}') or {}).get(
                                'doc_sha256' if f.suffix == '.doc' else 'pdf_sha256')}
        for name in ('sections.jsonl', 'chunks.jsonl', 'manifest.json', 'qa.json'):
            f = book / 'data' / name
            if f.is_file():
                vol['index_files'][name] = {'sha256': sha256(f), 'bytes': f.stat().st_size}
        vol['media']['queue_entries'] = sum(1 for r in media_rows if r.get('volume') == v)
        vol['media']['verified'] = sum(
            1 for r in media_rows
            if r.get('volume') == v and r.get('human_review_status') == 'verified')
        vol['ole_queue_entries'] = sum(1 for r in ole_rows if r.get('volume') == v)
        vol['ole_verified'] = sum(
            1 for r in ole_rows
            if r.get('volume') == v and r.get('human_review_status') == 'verified')
        volumes.append(vol)
    return {
        'manifest_kind': 'release_candidate',
        'scope': 'osnovy-sociologii volumes 1-6',
        'author_notice': AUTHOR_NOTICE,
        'third_party_art': {
            'status': 'HOLD',
            'note': 'Cover art / reproductions by third parties (e.g. G. Williams, '
                    'F. Reshetnikov, G. Korzhev) are NOT covered by the authors '
                    'notice; publish text without disputed third-party images '
                    'or resolve rights first.'},
        'volumes': volumes,
        'queue_summary': {
            'media_pending': sum(1 for r in media_rows if r.get('human_review_status') != 'verified'),
            'media_verified': sum(1 for r in media_rows if r.get('human_review_status') == 'verified'),
            'ole_pending': sum(1 for r in ole_rows if r.get('human_review_status') != 'verified'),
            'ole_verified': sum(1 for r in ole_rows if r.get('human_review_status') == 'verified')},
        'known_limitations': [
            'PDF page references are automatic candidates until human-confirmed',
            'embedded OLE contents not yet visually reviewed',
            'author assertions are not independently verified facts'],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--corpus', type=Path, required=True)
    ap.add_argument('--review', type=Path, default=Path('review'))
    ap.add_argument('--out', type=Path, default=None)
    a = ap.parse_args()
    man = build_manifest(a.corpus, a.review)
    text = json.dumps(man, ensure_ascii=False, indent=2)
    if a.out:
        a.out.write_text(text + '\n', encoding='utf-8')
    else:
        print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
