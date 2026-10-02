#!/usr/bin/env python3
"""Adversarial tests for corpus integrity validators.

Ported from the independent audit reproduction scripts: the validator must
detect mutations of the actual PUBLISHED Markdown bodies, not only of raw
text/chunk indexes and manifests. Uses synthetic corpora only.
"""
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VALIDATE_V4 = REPO / 'converter/validate_corpus.py'
VALIDATE_T1 = REPO / 'converter/validate_tom1.py'


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def build_volume_v4(root: Path):
    """Synthetic tom-2-style (v0.4 schema) volume that must pass validation."""
    t = root / 'tom-2'
    (t / 'data').mkdir(parents=True)
    (t / 'sections').mkdir()
    (t / 'source').mkdir()
    (t / 'assets/media_original').mkdir(parents=True)
    (t / 'assets/media').mkdir()
    doc, pdf = b'DOC BYTES', b'%PDF-1.4 fake'
    text = ('Первый абзац кириллицей.\n\nВторой абзац со сноской[^1].\n\n'
            '![Иллюстрация из исходного DOC: image9.png](../assets/media/image9.png)\n\n'
            'Третий абзац.')
    foots = {'1': '[^1]: Текст сноски кириллицей.'}
    body = text.rstrip() + '\n\n' + foots['1'] + '\n'
    doc_sha, pdf_sha = sha(doc), sha(pdf)
    meta = ('id: os-t2-ch1\nkind: section\nvolume: 2\ntitle: Глава 1\n'
            f'source_doc_sha256: {doc_sha}\nsource_pdf_sha256: {pdf_sha}\n'
            'pdf_page_1_based_candidate: 5\nfootnote_numbers: [1]\n')
    (t / 'sections/ch-1.md').write_text('---\n' + meta + '---\n\n' + body, encoding='utf-8')
    (t / 'source/osnovy-sociologii-tom-2.doc').write_bytes(doc)
    (t / 'source/osnovy-sociologii-tom-2.pdf').write_bytes(pdf)
    (t / 'data/raw_extracted_main.md').write_text(text, encoding='utf-8')
    (t / 'assets/media_original/image9.png').write_bytes(b'PNGDATA')
    (t / 'assets/media/image9.png').write_bytes(b'PNGDATA preview')
    (t / 'data/sections.jsonl').write_text(json.dumps({
        'id': 'os-t2-ch1', 'path': 'sections/ch-1.md', 'text': text,
        'footnote_numbers': [1], 'footnotes': foots, 'images_original': [],
        'pdf_page_candidate': 5, 'kind': 'section', 'volume': 2,
    }, ensure_ascii=False) + '\n', encoding='utf-8')
    (t / 'data/chunks.jsonl').write_text(json.dumps({
        'id': 'os-t2-ch1-chunk-0000', 'section_id': 'os-t2-ch1',
        'start_char': 0, 'end_char': len(text), 'pdf_page_candidate': 5,
        'text': text, 'volume': 2, 'source_path': 'sections/ch-1.md',
    }, ensure_ascii=False) + '\n', encoding='utf-8')
    (t / 'data/footnotes.jsonl').write_text(json.dumps({
        'id': 1, 'owner': 'os-t2-ch1', 'definition_raw': foots['1'],
    }, ensure_ascii=False) + '\n', encoding='utf-8')
    (t / 'data/figures.jsonl').write_text('', encoding='utf-8')
    (t / 'data/media_anchors.jsonl').write_text('', encoding='utf-8')
    (t / 'data/manifest.json').write_text(json.dumps({
        'converter_version': '0.4', 'volume': 2,
        'doc_sha256': doc_sha, 'pdf_sha256': pdf_sha, 'pdf_pages': 100,
        'unmodified_extracted_main_sha256': sha(text.encode()),
        'docx_media_count': 1, 'ole_embedded_objects_count': 0,
    }), encoding='utf-8')
    (t / 'data/qa.json').write_text(json.dumps({
        'volume': 2, 'omitted_inline_media_unique': 0, 'OLE_objects': 0,
        'omitted_inline_media_locatable_to_unit_unique': 0,
        'page_mapping_statuses_v2': {},
    }), encoding='utf-8')
    return t


def build_volume_v3(root: Path):
    """Synthetic tom-1-style (pilot v0.3 schema) volume that must pass."""
    t = root / 'tom-1'
    (t / 'data').mkdir(parents=True)
    (t / 'sections').mkdir()
    (t / 'source').mkdir()
    (t / 'assets/media_original').mkdir(parents=True)
    (t / 'assets/media').mkdir()
    text = 'Абзац первый кириллицей.\n\nАбзац второй со сносками[^3][^4].'
    foots = {'3': '[^3]: Определение сноски.', '4': '[^4]: Второе определение.'}
    body = text + '\n\n' + foots['3'] + '\n\n' + foots['4'] + '\n'
    doc_sha, pdf_sha = sha(b'DOC1'), sha(b'%PDF-1.4 t1')
    meta = ('id: os-t1-s1\nunit_id: os-t1-s1\nunit_title: Раздел 1\n'
            f'source_file_sha256: {doc_sha}\nsource_pdf_sha256: {pdf_sha}\n'
            'pdf_page_start: 3\noriginal_footnote_numbers: [3, 4]\n'
            'original_media_items: []\n')
    (t / 'sections/s1.md').write_text('---\n' + meta + '---\n\n' + body, encoding='utf-8')
    (t / 'source/osnovy-sociologii-tom-1.doc').write_bytes(b'DOC1')
    (t / 'source/osnovy-sociologii-tom-1.pdf').write_bytes(b'%PDF-1.4 t1')
    (t / 'assets/media_original/image15.png').write_bytes(b'PNG1')
    (t / 'data/sections.jsonl').write_text(json.dumps({
        'id': 'os-t1-s1', 'path': 'sections/s1.md', 'text': text,
        'source_text_hash': 'x' * 64, 'source_sha256': doc_sha,
        'source_pdf_sha256': pdf_sha, 'reading_order': 1,
        'pdf_page_start': 3, 'pdf_page_end_inclusive': 3,
        'pdf_locator_level': 'auto_page',
        'footnote_numbers': ['3', '4'], 'footnotes': foots, 'media_original': [],
    }, ensure_ascii=False) + '\n', encoding='utf-8')
    (t / 'data/chunks.jsonl').write_text(json.dumps({
        'chunk_id': 'os-t1-s1-c1', 'unit_id': 'os-t1-s1',
        'start_char_in_unit': 0, 'end_char_in_unit': len(text),
        'source_section_path': 'sections/s1.md',
        'text': text,
    }, ensure_ascii=False) + '\n', encoding='utf-8')
    (t / 'data/manifest.json').write_text(json.dumps({
        'version': '0.3', 'volume': 1, 'units_total': 1,
        'source_original_sha256': doc_sha,
        'pdf_source_sha256': pdf_sha,
        'source_text_body_sha256': sha(text.encode()),
        'pdf_page_count': 50,
        'original_docx_media_items': 1,
        'docx_media_without_body_use': [],
    }), encoding='utf-8')
    return t


def run_validator(script: Path, corpus: Path, volumes=None):
    if not script.exists():
        return 2, f'validator not found: {script}'
    cmd = [sys.executable, str(script), '--root', str(corpus)]
    if volumes:
        cmd += ['--volumes'] + [str(v) for v in volumes]
    r = subprocess.run(cmd,
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace')
    return r.returncode, (r.stdout or '') + (r.stderr or '')


def mutated(build, mutator):
    d = Path(tempfile.mkdtemp())
    try:
        target = build(d / 'corpus')
        mutator(target)
        return target
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise


class V4MarkdownDriftTest(unittest.TestCase):
    """The validator must catch mutations of the published Markdown body."""

    def check(self, mutator, want_fail, note=''):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        build_volume_v4(d / 'corpus')
        t = d / 'corpus/tom-2'
        if mutator:
            mutator(t)
        rc, out = run_validator(VALIDATE_V4, d / 'corpus', volumes=[2])
        if want_fail:
            self.assertNotEqual(rc, 0, f'{note}: expected FAIL, got PASS\n{out}')
        else:
            self.assertEqual(rc, 0, f'{note}: expected PASS\n{out}')
        return out

    def test_baseline_passes(self):
        self.check(None, False, 'baseline')

    def test_markdown_paragraph_deletion_detected(self):
        def m(t):
            f = t / 'sections/ch-1.md'
            f.write_text(f.read_text(encoding='utf-8').replace('Третий абзац.', ''), encoding='utf-8')
        self.check(m, True, 'deleted paragraph')

    def test_markdown_image_reference_removal_detected(self):
        def m(t):
            f = t / 'sections/ch-1.md'
            f.write_text(f.read_text(encoding='utf-8')
                         .replace('![Иллюстрация из исходного DOC: image9.png](../assets/media/image9.png)\n\n', ''),
                         encoding='utf-8')
        self.check(m, True, 'image ref removed')

    def test_markdown_footnote_removal_detected(self):
        def m(t):
            f = t / 'sections/ch-1.md'
            f.write_text(f.read_text(encoding='utf-8').replace('[^1]: Текст сноски кириллицей.', ''),
                         encoding='utf-8')
        self.check(m, True, 'footnote definition removed')

    def test_markdown_cyrillic_corruption_detected(self):
        def m(t):
            f = t / 'sections/ch-1.md'
            f.write_text(f.read_text(encoding='utf-8').replace('Первый', 'РџРµСЂРІС‹Р№'),
                         encoding='utf-8')
        self.check(m, True, 'mojibake')

    def test_media_markup_form_tolerated(self):
        # Rewrite markup form must equal raw <img> index form after normalization.
        def m(t):
            sec = json.loads((t / 'data/sections.jsonl').read_text(encoding='utf-8').splitlines()[0])
            sec['text'] = sec['text'].replace(
                '![Иллюстрация из исходного DOC: image9.png](../assets/media/image9.png)',
                '<img src="/legacy-workspace/media/image9.png" style="width:1in" />')
            (t / 'data/sections.jsonl').write_text(json.dumps(sec, ensure_ascii=False) + '\n',
                                                   encoding='utf-8')
            f = t / 'data/raw_extracted_main.md'
            f.write_text(sec['text'], encoding='utf-8')
            # keep raw hash consistent
            man = json.loads((t / 'data/manifest.json').read_text(encoding='utf-8'))
            man['unmodified_extracted_main_sha256'] = sha(sec['text'].encode())
            (t / 'data/manifest.json').write_text(json.dumps(man), encoding='utf-8')
            ch = json.loads((t / 'data/chunks.jsonl').read_text(encoding='utf-8').splitlines()[0])
            ch['text'] = sec['text']
            ch['end_char'] = len(sec['text'])
            (t / 'data/chunks.jsonl').write_text(json.dumps(ch, ensure_ascii=False) + '\n',
                                                 encoding='utf-8')
        self.check(m, False, 'img markup equivalence')

    def test_raw_text_still_detected(self):
        def m(t):
            f = t / 'data/raw_extracted_main.md'
            f.write_text(f.read_text(encoding='utf-8').replace('Первый', 'ПервыйX'), encoding='utf-8')
        self.check(m, True, 'raw text mutation')

    def test_chunk_drop_detected(self):
        def m(t):
            (t / 'data/chunks.jsonl').write_text('', encoding='utf-8')
        self.check(m, True, 'chunk dropped')

    def test_pdf_page_out_of_range_detected(self):
        def m(t):
            f = t / 'data/sections.jsonl'
            sec = json.loads(f.read_text(encoding='utf-8').splitlines()[0])
            sec['pdf_page_candidate'] = 999
            f.write_text(json.dumps(sec, ensure_ascii=False) + '\n', encoding='utf-8')
        self.check(m, True, 'page out of range')


class V3PilotSchemaTest(unittest.TestCase):
    """tom-1 pilot schema must get comparable validation guarantees."""

    def check(self, mutator, want_fail, note=''):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        build_volume_v3(d / 'corpus')
        t = d / 'corpus/tom-1'
        if mutator:
            mutator(t)
        rc, out = run_validator(VALIDATE_T1, d / 'corpus')
        if want_fail:
            self.assertNotEqual(rc, 0, f'{note}: expected FAIL, got PASS\n{out}')
        else:
            self.assertEqual(rc, 0, f'{note}: expected PASS\n{out}')
        return out

    def test_baseline_passes(self):
        self.check(None, False, 'tom-1 baseline')

    def test_tom1_markdown_body_drift_detected(self):
        def m(t):
            f = t / 'sections/s1.md'
            f.write_text(f.read_text(encoding='utf-8').replace('Абзац второй', ''), encoding='utf-8')
        self.check(m, True, 'tom-1 paragraph loss')

    def test_tom1_source_doc_swap_detected(self):
        def m(t):
            (t / 'source/osnovy-sociologii-tom-1.doc').write_bytes(b'OTHER DOC')
        self.check(m, True, 'tom-1 doc swap')

    def test_tom1_source_pdf_swap_detected(self):
        def m(t):
            (t / 'source/osnovy-sociologii-tom-1.pdf').write_bytes(b'%PDF OTHER')
        self.check(m, True, 'tom-1 pdf swap')

    def test_tom1_footnote_index_inconsistency_detected(self):
        def m(t):
            f = t / 'data/sections.jsonl'
            sec = json.loads(f.read_text(encoding='utf-8').splitlines()[0])
            sec['footnote_numbers'] = ['3', '4', '5']
            f.write_text(json.dumps(sec, ensure_ascii=False) + '\n', encoding='utf-8')
        self.check(m, True, 'footnote index drift')

    def test_tom1_footnote_definition_reorder_allowed(self):
        # Definition ORDER is layout, not authored content: same defs by number
        # in a different order must pass.
        def m(t):
            f = t / 'sections/s1.md'
            c = f.read_text(encoding='utf-8')
            c = c.replace('[^3]: Определение сноски.\n\n[^4]: Второе определение.',
                          '[^4]: Второе определение.\n\n[^3]: Определение сноски.')
            f.write_text(c, encoding='utf-8')
        self.check(m, False, 'footnote def reorder')

    def test_tom1_footnote_definition_removal_detected(self):
        def m(t):
            f = t / 'sections/s1.md'
            f.write_text(f.read_text(encoding='utf-8').replace('\n\n[^4]: Второе определение.', ''),
                         encoding='utf-8')
        self.check(m, True, 'footnote def removed')

    def test_tom1_footnote_definition_alteration_detected(self):
        def m(t):
            f = t / 'sections/s1.md'
            f.write_text(f.read_text(encoding='utf-8').replace('Второе определение.', 'Подменённое определение.'),
                         encoding='utf-8')
        self.check(m, True, 'footnote def altered')

    def test_tom1_footnote_definition_duplicate_detected(self):
        def m(t):
            f = t / 'sections/s1.md'
            f.write_text(f.read_text(encoding='utf-8') + '\n[^3]: Дублированное определение.\n',
                         encoding='utf-8')
        self.check(m, True, 'footnote def duplicated')


if __name__ == '__main__':
    unittest.main()
