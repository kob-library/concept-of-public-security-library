#!/usr/bin/env python3
"""Build a minimal synthetic corpus so CI can smoke-test the site pipeline
without the real (unreleased) source texts. Contains no authored content.
"""
import argparse, json
from pathlib import Path

VOLUMES = range(1, 7)


def jline(obj):
    return json.dumps(obj, ensure_ascii=False) + '\n'


def make_volume(root: Path, v: int):
    book = root / f'tom-{v}'
    for d in ('data', 'source', 'sections', 'assets/media'):
        (book / d).mkdir(parents=True, exist_ok=True)
    sections = []
    for i in (1, 2):
        sec_id = f'fx-os-{v}-s{i:02}'
        path = f'sections/fx-{i:02}.md'
        (book / path).write_text(
            f'---\ntitle: Фикстурный раздел {i} тома {v}\n---\n\n'
            f'# Фикстурный раздел {i}\n\n'
            f'Проверочный текст тома {v}, раздел {i}. Не является авторским содержимым.\n',
            encoding='utf-8')
        sections.append({
            'id': sec_id, 'kind': 'section', 'volume': v,
            'title': f'Фикстурный раздел {i} тома {v}', 'path': path,
            'pdf_page_candidate': 5 if i == 1 else None,
            'page_mapping_status': 'fixture',
            'previous_path': None, 'next_path': None})
    sections[0]['next_path'] = sections[1]['path']
    sections[1]['previous_path'] = sections[0]['path']
    (book / 'data/sections.jsonl').write_text(
        ''.join(jline(s) for s in sections), encoding='utf-8')
    chunks = [{'id': s['id'] + '-chunk-0000', 'section_id': s['id'],
               'volume': v, 'source_path': s['path'], 'start_char': 0,
               'end_char': 40, 'text': f'Проверочный текст тома {v}.'}
              for s in sections]
    (book / 'data/chunks.jsonl').write_text(
        ''.join(jline(c) for c in chunks), encoding='utf-8')
    (book / 'source' / f'osnovy-sociologii-tom-{v}.pdf').write_bytes(
        b'%PDF-1.4\n% fixture placeholder, not a real edition\n')
    return sections, chunks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, required=True)
    out = ap.parse_args().output
    out.mkdir(parents=True, exist_ok=True)
    (out / 'data').mkdir(exist_ok=True)
    all_sections, all_chunks = [], []
    works = []
    for v in VOLUMES:
        secs, chunks = make_volume(out, v)
        all_sections += secs
        all_chunks += chunks
        works.append({'volume': v, 'root_path': f'tom-{v}', 'schema': 'fixture'})
    # Root index links this editorial topic page; fixture must provide it.
    tdir = out / 'tom-1/topics'
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / 'social-time-technological-change.md').write_text(
        '# Фикстурная тематическая страница\n\nЗаглушка для смоук-теста.\n',
        encoding='utf-8')
    (out / 'tom-1/source/extra-evidence.pdf').write_bytes(b'%PDF-1.4\n% fixture\n')
    (out / 'data/works.jsonl').write_text(''.join(jline(w) for w in works), encoding='utf-8')
    (out / 'data/sections.jsonl').write_text(''.join(jline(s) for s in all_sections), encoding='utf-8')
    (out / 'data/chunks.jsonl').write_text(''.join(jline(c) for c in all_chunks), encoding='utf-8')
    print(json.dumps({'fixture': str(out), 'volumes': 6,
                      'sections': len(all_sections), 'chunks': len(all_chunks)},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
