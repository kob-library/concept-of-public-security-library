#!/usr/bin/env python3
"""Tests for the github-first export: a Markdown-only public repository layout
readable natively on GitHub (Issue #7)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))
sys.path.insert(0, str(REPO / 'tests'))
import export_github_first  # noqa: E402
from test_public_output import fixture_site_corpus  # noqa: E402
from test_release_gate import corpus_pins, write_review  # noqa: E402

TEXT_FIRST = {'name': 'text-first', 'volumes': [1, 2, 3, 4, 5, 6],
              'media': 'none', 'pdfs': 'reference',
              'canonical_pdf_url': 'https://example.invalid/archive'}


def run_export(corpus, review, tmp, mode='candidate', scope=TEXT_FIRST):
    approval = write_review(Path(review), corpus, approved=False,
                            pins=corpus_pins(corpus), scope=scope)
    out = Path(tmp) / 'export'
    report = export_github_first.export(corpus, out, approval, mode,
                                        review_dir=Path(review))
    return out, report


class GithubFirstExportTest(unittest.TestCase):
    def test_layout_and_all_sections_present(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            out, report = run_export(corpus, Path(t) / 'review', t)
            self.assertEqual(report['verdict'], 'CLEAN')
            self.assertEqual(report['missing_md_links'], [])
            self.assertEqual(report['sections'], 12)  # 2 fixture sections x 6 vols
            for name in ('README.md', 'TOPICS.md', 'NOTICE.md', 'LICENSE',
                         'AGENTS.md', 'EXPORT_MANIFEST.json'):
                self.assertTrue((out / name).is_file(), name)
            for v in range(1, 7):
                toc = (out / f'tom-{v}/README.md').read_text(encoding='utf-8')
                self.assertIn('sections/fx-01.md', toc)
                self.assertTrue((out / f'tom-{v}/sections/fx-01.md').is_file())

    def test_private_frontmatter_rewritten(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            f = corpus / 'tom-2/sections/fx-01.md'
            f.write_text(
                '---\ntitle: x\nsource_pdf_drive_url: '
                'https://drive' + '.google.com/file/d/SECRETID/view\n'
                'pilot_release_status: internal_only\n---\n\n'
                '# Раздел\n\nТекст.\n', encoding='utf-8')
            out, _ = run_export(corpus, Path(t) / 'review', t)
            exp = (out / 'tom-2/sections/fx-01.md').read_text(encoding='utf-8')
            self.assertNotIn('drive' + '.google', exp)
            self.assertNotIn('internal_only', exp)
            self.assertNotIn('SECRETID', exp)
            self.assertIn('Текст.', exp)
            self.assertIn('Первичный текст', exp)

    def test_media_becomes_omission_marker(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            f = corpus / 'tom-1/sections/fx-01.md'
            f.write_text(f.read_text(encoding='utf-8') +
                         '\n![схема](../assets/media/image9.png)\n',
                         encoding='utf-8')
            out, report = run_export(corpus, Path(t) / 'review', t)
            exp = (out / 'tom-1/sections/fx-01.md').read_text(encoding='utf-8')
            self.assertIn('не включена в данный выпуск', exp)
            self.assertNotIn('![', exp)
            self.assertNotIn('](../assets/media/image9.png)', exp)
            self.assertIn('Файл источника: assets/media/image9.png', exp)
            self.assertEqual(report['media_omitted_inline'],
                             ['tom-1:fx-os-1-s01:image9.png'])
            self.assertFalse((out / 'tom-1/assets').exists())

    def test_broken_relative_link_neutralized(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            f = corpus / 'tom-3/sections/fx-01.md'
            f.write_text(f.read_text(encoding='utf-8') +
                         '\nСм. [“Деловой Петербург”](../../Деловой) и [PDF](../source/x.pdf).\n',
                         encoding='utf-8')
            out, report = run_export(corpus, Path(t) / 'review', t)
            exp = (out / 'tom-3/sections/fx-01.md').read_text(encoding='utf-8')
            self.assertIn('“Деловой Петербург”', exp)
            self.assertNotIn('../../Деловой', exp)
            self.assertNotIn('../source/x.pdf', exp)
            self.assertEqual(len(report['links_neutralized']), 2)
            self.assertEqual(report['missing_md_links'], [])

    def test_working_relative_link_kept(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            f = corpus / 'tom-1/sections/fx-02.md'
            f.write_text(f.read_text(encoding='utf-8') +
                         '\nСм. [первый раздел](fx-01.md).\n', encoding='utf-8')
            out, report = run_export(corpus, Path(t) / 'review', t)
            exp = (out / 'tom-1/sections/fx-02.md').read_text(encoding='utf-8')
            self.assertIn('[первый раздел](fx-01.md)', exp)
            self.assertEqual(report['missing_md_links'], [])

    def test_prev_next_navigation(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            out, _ = run_export(corpus, Path(t) / 'review', t)
            exp = (out / 'tom-4/sections/fx-01.md').read_text(encoding='utf-8')
            self.assertIn('[Оглавление тома 4](../README.md)', exp)
            self.assertIn('[Вперёд →](fx-02.md)', exp)
            exp2 = (out / 'tom-4/sections/fx-02.md').read_text(encoding='utf-8')
            self.assertIn('[← Назад](fx-01.md)', exp2)

    def test_public_mode_fails_closed(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            approval = write_review(Path(t) / 'review', corpus, approved=False,
                                    pins=corpus_pins(corpus), scope=TEXT_FIRST)
            with self.assertRaises(SystemExit):
                export_github_first.export(corpus, Path(t) / 'out', approval,
                                           'public', review_dir=Path(t) / 'review')
            self.assertFalse((Path(t) / 'out').exists())

    def test_topics_links_resolve_to_real_sections(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            out, _ = run_export(corpus, Path(t) / 'review', t)
            topics = out / 'TOPICS.md'
            if topics.is_file():
                for m in __import__('re').finditer(r'\]\((tom-\d/[^)]+)\)',
                                                   topics.read_text(encoding='utf-8')):
                    self.assertTrue((out / m.group(1)).is_file(), m.group(1))

    def test_no_binary_or_private_paths(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            out, report = run_export(corpus, Path(t) / 'review', t)
            exts = {p.suffix.lower() for p in out.rglob('*') if p.is_file()}
            self.assertFalse(exts & {'.pdf', '.png', '.jpeg', '.jpg', '.doc',
                                     '.docx', '.wmf', '.emf'})
            self.assertFalse((out / 'review').exists())
            self.assertFalse((out / 'source_corpus').exists())

    def test_manifest_covers_every_file_except_itself(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            out, report = run_export(corpus, Path(t) / 'review', t)
            man = json.loads((out / 'EXPORT_MANIFEST.json').read_text(encoding='utf-8'))
            listed = {f['path'] for f in man['files']}
            on_disk = {p.relative_to(out).as_posix()
                       for p in out.rglob('*') if p.is_file()}
            self.assertEqual(listed, on_disk - {'EXPORT_MANIFEST.json'})
            self.assertEqual(report['files_hashed'], len(listed))
            self.assertEqual(report['files_on_disk'], len(on_disk))

    def test_publish_workflow_not_exported(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            out, _ = run_export(corpus, Path(t) / 'review', t)
            self.assertTrue((out / '.github/workflows/ci.yml').is_file())
            self.assertFalse((out / '.github/workflows/publish.yml').exists())

    def test_notice_has_full_sha256(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            works = [json.loads(l) for l in
                     (corpus / 'data/works.jsonl').read_text(encoding='utf-8').splitlines()]
            fake = 'a' * 64
            works[0]['doc_sha256'] = fake
            (corpus / 'data/works.jsonl').write_text(
                ''.join(json.dumps(w) + '\n' for w in works), encoding='utf-8')
            out, _ = run_export(corpus, Path(t) / 'review', t)
            notice = (out / 'NOTICE.md').read_text(encoding='utf-8')
            self.assertIn(fake, notice)
            self.assertNotIn('…', notice.split('## Источники')[1].split('##')[0])

    def test_output_path_guard(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            approval = write_review(Path(t) / 'review', corpus, approved=False,
                                    pins=corpus_pins(corpus), scope=TEXT_FIRST)
            for bad in (corpus, corpus.parent, corpus / 'tom-1',
                        Path(t) / 'review'):
                with self.assertRaises(SystemExit, msg=str(bad)):
                    export_github_first.export(corpus, bad, approval, 'candidate',
                                               review_dir=Path(t) / 'review')
            self.assertTrue((corpus / 'tom-1/data/sections.jsonl').is_file())


if __name__ == '__main__':
    unittest.main()
