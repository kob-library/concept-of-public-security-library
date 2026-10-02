#!/usr/bin/env python3
"""Tests for the Issue #9 unified-library export: six volumes plus
books/analytics/collections in one GitHub-first repository."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))
sys.path.insert(0, str(REPO / 'tests'))
import export_github_first  # noqa: E402
import export_unified  # noqa: E402
from test_public_output import fixture_site_corpus  # noqa: E402
from test_release_gate import corpus_pins, write_review  # noqa: E402

TEXT_FIRST = {'name': 'text-first', 'volumes': [1, 2, 3, 4, 5, 6],
              'media': 'none', 'pdfs': 'reference',
              'canonical_pdf_url': 'https://example.invalid/archive'}


def fixture_extra(root: Path) -> Path:
    """Minimal unified-corpus fixture: one book + one analytics item."""
    extra = root / 'extra'
    book = extra / 'books/sample-book'
    ana = extra / 'analytics/20000101-sample-note'
    book.mkdir(parents=True)
    ana.mkdir(parents=True)
    (book / '01.md').write_text(
        '# Глава 1\n\nТекст книги со ссылкой[^9].\n\n![схема](img/pic.png)\n',
        encoding='utf-8')
    (book / '02.md').write_text(
        'Продолжение без заголовка.\n\n[^9]: Сноска из последнего раздела.\n',
        encoding='utf-8')
    (ana / '01.md').write_text(
        '---\ntitle: x\ninternal_only: yes\n---\n\n# Записка\n\n'
        'См. [Деловой](file:///C:/Users/op/Documents/secret) и '
        '[внешнее](https://dotu.ru/x/).\n', encoding='utf-8')
    (extra / 'data_works.jsonl').write_text('\n'.join(json.dumps(w, ensure_ascii=False) for w in [
        {'work_id': 'sample-book', 'collection': 'books',
         'title': 'Образцовая книга', 'dir': 'books/sample-book',
         'sections': 2, 'sha256_source': 'b' * 64,
         'source_name': 'sample.doc', 'text_status': 'automatic_transcription',
         'year': '2001', 'dotu_url': 'https://dotu.ru/sample/'},
        {'work_id': 'sample-note', 'collection': 'analytics',
         'title': 'Образцовая записка', 'dir': 'analytics/20000101-sample-note',
         'sections': 1, 'sha256_source': 'c' * 64,
         'source_name': 'note.doc', 'text_status': 'automatic_transcription',
         'date': '2000-01-01'},
        {'work_id': 'third-party-ref', 'collection': 'collections',
         'title': 'Сторонний реферат', 'dir': 'collections/third-party-ref',
         'sections': 1, 'sha256_source': 'd' * 64,
         'source_name': 'ref.doc', 'text_status': 'automatic_transcription'},
    ]) + '\n', encoding='utf-8')
    (extra / 'data_sections.jsonl').write_text('\n'.join(
        json.dumps(s, ensure_ascii=False) for s in [
            {'work_id': 'sample-book', 'section_id': 'sample-book/s01',
             'path': 'books/sample-book/01.md', 'title': 'Глава 1',
             'reading_order': 1},
            {'work_id': 'sample-book', 'section_id': 'sample-book/s02',
             'path': 'books/sample-book/02.md', 'title': 'Глава 2',
             'reading_order': 2},
            {'work_id': 'sample-note', 'section_id': 'sample-note/s01',
             'path': 'analytics/20000101-sample-note/01.md',
             'title': 'Записка', 'reading_order': 1},
            {'work_id': 'third-party-ref', 'section_id': 'ref/s01',
             'path': 'collections/third-party-ref/01.md',
             'title': 'Реферат', 'reading_order': 1},
        ]) + '\n', encoding='utf-8')
    (extra / 'excluded_works.json').write_text(json.dumps([
        {'work_id': 'third-party-ref', 'status': 'third_party_work',
         'reason': 'test fixture'}]), encoding='utf-8')
    return extra


def run_unified(corpus, extra, review, tmp):
    approval = write_review(Path(review), corpus, approved=False,
                            pins=corpus_pins(corpus), scope=TEXT_FIRST)
    out = Path(tmp) / 'export'
    report = export_github_first.export(corpus, out, approval, 'candidate',
                                        review_dir=Path(review))
    report = export_unified.extend(out, Path(extra), report)
    return out, report


class UnifiedExportTest(unittest.TestCase):
    def test_books_and_analytics_exported(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            extra = fixture_extra(Path(t))
            out, report = run_unified(corpus, extra, Path(t) / 'review', t)
            self.assertEqual(report['verdict'], 'CLEAN')
            self.assertEqual(report['works_added'], 2)
            self.assertEqual(report['work_sections_added'], 3)
            for p in ('books/sample-book/README.md',
                      'books/sample-book/01.md',
                      'books/sample-book/02.md',
                      'analytics/20000101-sample-note/01.md',
                      'CATALOG.md', 'CHRONOLOGY.md'):
                self.assertTrue((out / p).is_file(), p)

    def test_excluded_third_party_not_exported(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            extra = fixture_extra(Path(t))
            out, report = run_unified(corpus, extra, Path(t) / 'review', t)
            self.assertFalse((out / 'collections').exists())
            self.assertIn('third-party-ref', report['excluded_works'])
            works = [json.loads(l) for l in
                     (out / 'data/works.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertNotIn('third-party-ref', {w['work_id'] for w in works})

    def test_local_file_link_neutralized(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            extra = fixture_extra(Path(t))
            out, report = run_unified(corpus, extra, Path(t) / 'review', t)
            exp = (out / 'analytics/20000101-sample-note/01.md').read_text(
                encoding='utf-8')
            self.assertIn('Деловой', exp)
            self.assertNotIn('file://', exp)
            self.assertNotIn('C:/Users', exp)
            self.assertNotIn('internal_only', exp)
            self.assertTrue(report['work_links_neutralized'] >= 1)

    def test_work_media_omission_marker(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            extra = fixture_extra(Path(t))
            out, report = run_unified(corpus, extra, Path(t) / 'review', t)
            exp = (out / 'books/sample-book/01.md').read_text(encoding='utf-8')
            self.assertIn('не включена в данный выпуск', exp)
            self.assertNotIn('![', exp)
            self.assertEqual(report['work_media_omitted'], 1)

    def test_footnote_defs_replicated_into_referencing_section(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            extra = fixture_extra(Path(t))
            out, report = run_unified(corpus, extra, Path(t) / 'review', t)
            s1 = (out / 'books/sample-book/01.md').read_text(encoding='utf-8')
            s2 = (out / 'books/sample-book/02.md').read_text(encoding='utf-8')
            self.assertIn('[^9]', s1)
            self.assertIn('[^9]: Сноска из последнего раздела.', s1)
            self.assertIn('[^9]: Сноска из последнего раздела.', s2)
            self.assertEqual(report['footnote_defs_replicated'], 1)

    def test_heading_injected_when_missing(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            extra = fixture_extra(Path(t))
            out, _ = run_unified(corpus, extra, Path(t) / 'review', t)
            exp = (out / 'books/sample-book/02.md').read_text(encoding='utf-8')
            self.assertIn('# Глава 2', exp)

    def test_index_schema_unified(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            extra = fixture_extra(Path(t))
            out, _ = run_unified(corpus, extra, Path(t) / 'review', t)
            works = [json.loads(l) for l in
                     (out / 'data/works.jsonl').read_text(encoding='utf-8').splitlines()]
            secs = [json.loads(l) for l in
                    (out / 'data/sections.jsonl').read_text(encoding='utf-8').splitlines()]
            for w in works:
                for k in ('work_id', 'title', 'dir', 'sections'):
                    self.assertIn(k, w, f'{k} missing in {w.get("work_id")}')
            vol_ids = {w['work_id'] for w in works if w.get('volume')}
            self.assertEqual(vol_ids,
                             {f'osnovy-sociologii-tom-{v}' for v in range(1, 7)})
            for s in secs:
                self.assertIn('work_id', s)
                self.assertIn('reading_order', s)
            self.assertTrue((out / 'NOTICE.md').is_file())

    def test_catalog_and_chronology_cover_volumes(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            extra = fixture_extra(Path(t))
            out, _ = run_unified(corpus, extra, Path(t) / 'review', t)
            cat = (out / 'CATALOG.md').read_text(encoding='utf-8')
            chron = (out / 'CHRONOLOGY.md').read_text(encoding='utf-8')
            self.assertIn('tom-6/README.md', cat)
            self.assertIn('books/sample-book/README.md', cat)
            self.assertIn('tom-1/README.md', chron)
            self.assertIn('analytics/20000101-sample-note/README.md', chron)

    def test_no_local_paths_or_binaries(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            extra = fixture_extra(Path(t))
            out, report = run_unified(corpus, extra, Path(t) / 'review', t)
            self.assertEqual(report['missing_md_links'], [])
            import re as _re
            for p in out.rglob('*'):
                if p.is_file() and p.suffix.lower() in ('.md', '.json', '.jsonl', '.txt'):
                    txt = p.read_text(encoding='utf-8')
                    # local machine links (drive-letter or file:// to non-tmp)
                    self.assertIsNone(
                        _re.search(r'file:///(?!tmp)', txt, _re.I), str(p))
                    self.assertIsNone(
                        _re.search(r'[A-Za-z]:[\\/][Uu]sers', txt), str(p))
                    self.assertNotIn('\u00ad', txt, str(p))


if __name__ == '__main__':
    unittest.main()
