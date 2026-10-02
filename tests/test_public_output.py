#!/usr/bin/env python3
"""Tests for the public-output whitelist, forbidden-content scanner, release
manifest generator and release dry-run."""
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))
sys.path.insert(0, str(REPO / 'tests'))
from scan_public_output import scan_tree  # noqa: E402
import site_builder  # noqa: E402

from test_release_gate import corpus_pins, write_review  # noqa: E402
# media_row/ole_row unused here: queues stay empty in these fixtures

MISTUNE = importlib.util.find_spec('mistune') is not None


def fixture_site_corpus(tmp: Path) -> Path:
    """Site-buildable synthetic corpus (no authored text)."""
    corpus = tmp / 'corpus'
    r = subprocess.run(
        [sys.executable, str(REPO / 'tools/make_fixture_corpus.py'),
         '--output', str(corpus)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    for v in range(1, 7):
        d = corpus / f'tom-{v}'
        (d / 'assets/media_original').mkdir(parents=True, exist_ok=True)
        (d / 'source' / f'osnovy-sociologii-tom-{v}.doc').write_bytes(b'DOC fx')
        if v == 1:
            (d / 'data/manifest.json').write_text(json.dumps(
                {'volume': 1, 'docx_media_without_body_use': []}), encoding='utf-8')
            # internal-only extras that must never reach public output
            (d / 'source/mertvaia-voda-red-2015.pdf').write_bytes(b'%PDF mw')
            (d / 'data/sources.jsonl').write_text(
                '{"u":"https://internal-storage.invalid/file/x"}\n', encoding='utf-8')
        else:
            (d / 'data/qa.json').write_text(json.dumps({
                'volume': v, 'source_text_split_lossless': True,
                'omitted_inline_media_unique': 0, 'docx_media_not_placed': [],
                'OLE_objects': 0}), encoding='utf-8')
    return corpus


def review_dir(tmp: Path, corpus: Path, approved=False, scope=None):
    return write_review(tmp / 'review', corpus, approved=approved, media=[], ole=[],
                        pins=corpus_pins(corpus), scope=scope)


@unittest.skipUnless(MISTUNE, 'mistune not installed')
class PublicWhitelistTest(unittest.TestCase):
    def test_candidate_build_excludes_internal_files(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            out = Path(t) / 'site'
            review = review_dir(Path(t), corpus)
            site_builder.build(corpus, out, 'candidate', release=review)
            self.assertFalse((out / 'tom-1/data').exists())
            self.assertFalse((out / 'tom-1/source/mertvaia-voda-red-2015.pdf').exists())
            self.assertFalse((out / 'tom-1/source/extra-evidence.pdf').exists())
            self.assertFalse((out / 'tom-1/topics').exists())
            self.assertTrue((out / 'tom-1/source/osnovy-sociologii-tom-1.pdf').exists())
            idx = (out / 'index.html').read_text(encoding='utf-8')
            self.assertNotIn('topics/social-time', idx)
            self.assertIn('content="noindex,nofollow"', idx)
            self.assertIn('Disallow: /', (out / 'robots.txt').read_text())

    def test_unreviewed_image_absent_from_candidate_artifact(self):
        """Audit attack artifact side: approved preview is shipped, an
        unreviewed file sitting next to it is not."""
        import hashlib
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            corpus = fixture_site_corpus(tmp)
            mdir = corpus / 'tom-2/assets/media'
            (mdir / 'image9.png').write_bytes(b'approved preview')
            (mdir / 'rogue.png').write_bytes(b'unreviewed payload')
            review = review_dir(tmp, corpus, approved=False)
            allow = {'volume': 2, 'path': 'assets/media/image9.png',
                     'sha256': hashlib.sha256(b'approved preview').hexdigest(),
                     'source_image': 'image9.png', 'reviewer': 'R',
                     'decision': 'approved', 'scope': 'x'}
            (review.parent / 'public_media_allowlist.jsonl').write_text(
                json.dumps(allow) + '\n', encoding='utf-8')
            out = tmp / 'site'
            site_builder.build(corpus, out, 'candidate', release=review)
            self.assertTrue((out / 'tom-2/assets/media/image9.png').is_file())
            self.assertFalse((out / 'tom-2/assets/media/rogue.png').exists())

    def test_candidate_output_scans_clean(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            out = Path(t) / 'site'
            site_builder.build(corpus, out, 'candidate')
            problems, _ = scan_tree(out)
            self.assertEqual(problems, [])

    def test_section_page_has_citation_button(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            out = Path(t) / 'site'
            site_builder.build(corpus, out, 'candidate')
            pages = [p.read_text(encoding='utf-8') for p in out.rglob('sections/*.html')]
            self.assertTrue(pages)
            for html in pages:
                self.assertIn('class="cite"', html)
                self.assertIn('reader.js', html)
            self.assertTrue(any('Кандидат страницы PDF' in html for html in pages))

    def test_search_results_point_at_full_sections(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            out = Path(t) / 'site'
            site_builder.build(corpus, out, 'candidate')
            idx = json.loads((out / 'data/search_index.json').read_text(encoding='utf-8'))
            self.assertTrue(idx)
            for item in idx:
                self.assertTrue((out / item['url']).is_file(), item['url'])


    def test_text_first_scope_omits_media_and_bundled_pdf(self):
        """media:'none' + pdfs:'reference' -> no media files, no source/*.pdf,
        visible omission notes instead of broken images, external canonical
        edition link instead of a bundled PDF."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            corpus = fixture_site_corpus(tmp)
            mdir = corpus / 'tom-2/assets/media'
            (mdir / 'image9.png').write_bytes(b'preview')
            # a section that references the image inline
            secs = corpus / 'tom-2/data/sections.jsonl'
            rows = [json.loads(l) for l in secs.read_text(encoding='utf-8').splitlines() if l.strip()]
            sec_path = corpus / 'tom-2' / rows[0]['path']
            sec_path.write_text(sec_path.read_text(encoding='utf-8')
                                + '\n\n![Схема](assets/media/image9.png)\n', encoding='utf-8')
            scope = {'name': 'text-first', 'volumes': [1, 2, 3, 4, 5, 6],
                     'media': 'none', 'pdfs': 'reference',
                     'canonical_pdf_url': 'https://archive.example/editions'}
            review = review_dir(tmp, corpus, scope=scope)
            out = tmp / 'site'
            site_builder.build(corpus, out, 'candidate', release=review)
            # no media shipped, anywhere
            self.assertFalse(list(out.rglob('assets/media/*')))
            self.assertFalse((out / 'tom-2/source/osnovy-sociologii-tom-2.pdf').exists())
            # visible omission note, not a broken image
            pages = [p for p in out.rglob('*.html') if 'media-omitted' in p.read_text(encoding='utf-8')]
            self.assertTrue(pages, 'omission note missing')
            self.assertFalse(list(out.rglob('assets/media/image9.png')))
            # citation leads to the external canonical archive
            page = next(out.rglob('sections/*.html')).read_text(encoding='utf-8')
            self.assertIn('archive.example/editions', page)
            self.assertIn('кандидат', page.lower())
            # index honestly discloses the composition
            idx = (out / 'index.html').read_text(encoding='utf-8')
            self.assertIn('не включены', idx)

    def test_relative_media_path_also_replaced(self):
        """PR#4 review: '../assets/media/x.png' (the actual corpus form) must
        be replaced too — no surviving <img> and no broken internal refs."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            corpus = fixture_site_corpus(tmp)
            mdir = corpus / 'tom-2/assets/media'
            (mdir / 'deep.png').write_bytes(b'px')
            secs = corpus / 'tom-2/data/sections.jsonl'
            rows = [json.loads(l) for l in secs.read_text(encoding='utf-8').splitlines() if l.strip()]
            sec_path = corpus / 'tom-2' / rows[0]['path']
            sec_path.write_text(sec_path.read_text(encoding='utf-8')
                                + '\n\n![Схема](../assets/media/deep.png)\n', encoding='utf-8')
            scope = {'name': 'tf', 'volumes': [1, 2, 3, 4, 5, 6],
                     'media': 'none', 'pdfs': 'none'}
            review = review_dir(tmp, corpus, scope=scope)
            out = tmp / 'site'
            site_builder.build(corpus, out, 'candidate', release=review)
            all_html = [p.read_text(encoding='utf-8') for p in out.rglob('*.html')]
            self.assertFalse(any('<img' in h for h in all_html))
            self.assertFalse(list(out.rglob('assets/media/*')))
            self.assertTrue(any('media-omitted' in h for h in all_html))
            # no internal link resolves to a missing file
            self.assertEqual(site_builder.audit_internal_links(out), [])

    def test_internal_build_keeps_full_media(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            (corpus / 'tom-2/assets/media/image9.png').write_bytes(b'preview')
            out = Path(t) / 'site'
            site_builder.build(corpus, out, 'internal')
            self.assertTrue((out / 'tom-2/assets/media/image9.png').is_file())
            self.assertTrue((out / 'tom-1/source/osnovy-sociologii-tom-1.pdf').is_file())


    def test_base_url_trailing_slash_normalized(self):
        """--base-url 'https://h/r/' must not produce '//' in canonical."""
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            out = Path(t) / 'site'
            site_builder.build(corpus, out, 'candidate',
                               base_url='https://owner.github.io/repo/')
            for p in out.rglob('*.html'):
                h = p.read_text(encoding='utf-8')
                canon = re.search(r'<link rel="canonical" href="([^"]+)"', h)
                self.assertTrue(canon, p)
                self.assertNotIn('repo//', canon.group(1), p)

    def test_pages_have_unique_meta_and_h1(self):
        """Issue #5: unique title/description/canonical + one meaningful H1
        per index/volume/section page; canonical only when base-url given."""
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            out = Path(t) / 'site'
            site_builder.build(corpus, out, 'candidate',
                               base_url='https://owner.github.io/repo')
            titles, descs, canons = set(), set(), set()
            for p in out.rglob('*.html'):
                h = p.read_text(encoding='utf-8')
                title = re.search(r'<title>([^<]+)</title>', h)
                desc = re.search(r'<meta name="description" content="([^"]+)"', h)
                canon = re.search(r'<link rel="canonical" href="([^"]+)"', h)
                self.assertTrue(title, p)
                self.assertTrue(desc, p)
                self.assertTrue(canon, p)
                titles.add(title.group(1)); descs.add(desc.group(1)); canons.add(canon.group(1))
                self.assertTrue(canon.group(1).startswith('https://owner.github.io/repo/'))
            # every page's metadata is unique
            self.assertEqual(len(titles), len(list(out.rglob('*.html'))))
            self.assertEqual(len(descs), len(list(out.rglob('*.html'))))
            self.assertEqual(len(canons), len(list(out.rglob('*.html'))))
            # exactly one meaningful H1 per page
            for p in out.rglob('*.html'):
                h = p.read_text(encoding='utf-8')
                self.assertEqual(h.count('<h1'), 1, p)

    def test_public_sitemap_and_robots(self):
        """Issue #5: public mode emits sitemap.xml with absolute URLs inside
        the Pages subpath + robots.txt pointing at it; candidate has neither."""
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            review = review_dir(Path(t), corpus)
            # approved public build with base-url
            import test_release_gate as trg
            approval = Path(t) / 'review' / 'release_approval.json'
            a = json.loads(approval.read_text(encoding='utf-8'))
            for k in list(a):
                if k not in ('approved_by', 'approval_date', 'release_scope', 'source_pins'):
                    a[k] = True
            approval.write_text(json.dumps(a), encoding='utf-8')
            out = Path(t) / 'pub'
            site_builder.build(corpus, out, 'public', release=review,
                               base_url='https://owner.github.io/repo')
            sm = (out / 'sitemap.xml').read_text(encoding='utf-8')
            self.assertIn('https://owner.github.io/repo/index.html', sm)
            self.assertIn('tom-1/index.html', sm)
            self.assertNotIn('\\', sm)  # URLs must use forward slashes on any OS
            robots = (out / 'robots.txt').read_text(encoding='utf-8')
            self.assertIn('Sitemap: https://owner.github.io/repo/sitemap.xml', robots)
            idx = (out / 'index.html').read_text(encoding='utf-8')
            self.assertIn('index,follow', idx)
            # candidate/internals keep noindex and ship no sitemap
            out2 = Path(t) / 'cand'
            site_builder.build(corpus, out2, 'candidate', release=review)
            self.assertFalse((out2 / 'sitemap.xml').exists())
            self.assertIn('Disallow: /', (out2 / 'robots.txt').read_text())

    def test_topics_page_grounded_and_editorial(self):
        """Rubric page: only rubrics confirmed by real section titles ship,
        every link resolves to a full section, editorial notice present."""
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            out = Path(t) / 'site'
            site_builder.build(corpus, out, 'candidate')
            topics = out / 'topics.html'
            if topics.exists():  # only when fixture titles confirm a rubric
                h = topics.read_text(encoding='utf-8')
                self.assertIn('не авторский текст', h.lower())
                for href in re.findall(r'href="([^"]+\.html)"', h):
                    self.assertNotIn('\\', href, href)  # URLs are posix on every OS
                    self.assertTrue((out / href).is_file(), href)
                # search index urls must be posix too
                idx = json.loads((out / 'data/search_index.json').read_text(encoding='utf-8'))
                self.assertFalse(any('\\' in i['url'] for i in idx))


class ScanPublicOutputTest(unittest.TestCase):
    def test_detects_forbidden_content(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            drive_url = 'https://' + 'drive' + '.google' + '.invalid/x'
            (d / 'leak.html').write_text(f'<a href="{drive_url}">x</a>', encoding='utf-8')
            (d / 'path.html').write_text('<img src="/mnt/data/x.png">', encoding='utf-8')
            mail = 'contact me at user@' + 'example.com'
            (d / 'mail.txt').write_text(mail, encoding='utf-8')
            (d / 'ole.bin').write_bytes(b'\xd0\xcf\x11\xe0')
            (d / 'extra.pdf').write_bytes(b'%PDF rogue')
            (d / 'raw_extracted_main.md').write_text('dump', encoding='utf-8')
            # binary noise that resembles an e-mail inside images must not flag
            (d / 'img.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'x' * 99 + b'7@' + b'm.l,\x0c')
            problems, _ = scan_tree(d)
            self.assertEqual(len(problems), 6, problems)

    def test_binary_metadata_email_detected(self):
        # Personal data embedded in binary metadata (PDF Info / EXIF-style
        # printable strings) must be caught, while compressed-data noise
        # must not.
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / 'img.png').write_bytes(
                b'\x89PNG\r\n\x1a\n' + b'\x00' * 40 + b'Author: me@' + b'example.com' + b'\x00' * 40)
            problems, _ = scan_tree(d)
            self.assertTrue(any('e-mail' in p for p in problems), problems)

    def _png_with_ztxt(self, text: bytes) -> bytes:
        import zlib as _z, struct as _s
        def chunk(typ, data):
            c = _s.pack('>I', len(data)) + typ + data
            return c + _s.pack('>I', _z.crc32(typ + data) & 0xffffffff)
        ztxt = b'Comment\x00\x00' + _z.compress(text)
        return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', _s.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
                + chunk(b'zTXt', ztxt) + chunk(b'IDAT', _z.compress(b'\x00\x00\x00\x00'))
                + chunk(b'IEND', b''))

    def _pdf_with_flate(self, text: bytes) -> bytes:
        import zlib as _z
        stream = _z.compress(text)
        return (b'%PDF-1.5\n1 0 obj<</Type/Metadata/Subtype/XML/Filter/FlateDecode/Length '
                + str(len(stream)).encode() + b'>>stream\n' + stream + b'\nendstream endobj\n%%EOF')

    def test_compressed_and_encoded_identities_detected(self):
        """Audit probes: identity hidden in compressed/encoded carriers must flag."""
        import base64
        # b64 detection threshold is 40 chars — use a realistically long address
        secret_parts = ('operator.personal.mail', 'example.com')
        secret = (secret_parts[0] + '@' + secret_parts[1]).encode()
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            # 1. mixed-case e-mail inside compressed PNG zTXt metadata
            (d / 'meta.png').write_bytes(self._png_with_ztxt(b'Contact: ' + secret.upper()))
            # 2. XMP inside a FlateDecode PDF stream
            (d / 'osnovy-sociologii-tom-1.pdf').write_bytes(
                self._pdf_with_flate(b'<dc:creator><rdf:li>' + secret + b'</rdf:li></dc:creator>'))
            # 3. base64 blob inside a text file
            (d / 'blob.js').write_text('x="' + base64.b64encode(secret).decode() + '"', encoding='utf-8')
            # 4. UTF-16 encoded address
            (d / 'u16.txt').write_bytes('mail: ' .encode() + secret.decode().encode('utf-16-le'))
            problems, _ = scan_tree(d)
            joined = ' | '.join(problems)
            self.assertIn('meta.png', joined)
            self.assertIn('osnovy-sociologii-tom-1.pdf', joined)
            self.assertIn('blob.js', joined)
            self.assertIn('u16.txt', joined)

    def test_operator_login_forbid_param(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / 'page.html').write_text('<p>built by some-user-42</p>', encoding='utf-8')
            problems, _ = scan_tree(d, forbid=['some-user-42'])
            self.assertTrue(any('forbidden string' in p for p in problems), problems)
            problems_ok, _ = scan_tree(d)  # unknown logins are not hardcoded
            self.assertEqual(problems_ok, [])

    def test_bare_external_file_id_flagged(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / 'l.html').write_text('<a href="u?id=A1b2C3d4E5f6G7h8I9j0K1l2M3n4">x</a>',
                                      encoding='utf-8')
            problems, _ = scan_tree(d)
            self.assertTrue(any('opaque external file id' in p for p in problems), problems)

    def test_clean_tree_passes(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / 'index.html').write_text('<p>ok</p>', encoding='utf-8')
            (d / 'tom-1/source').mkdir(parents=True)
            (d / 'tom-1/source/osnovy-sociologii-tom-1.pdf').write_bytes(b'%PDF ok')
            problems, _ = scan_tree(d)
            self.assertEqual(problems, [])


class ReleaseManifestTest(unittest.TestCase):
    def test_manifest_lists_scope_and_exclusions(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            corpus = fixture_site_corpus(tmp)
            review = review_dir(tmp, corpus)
            r = subprocess.run(
                [sys.executable, str(REPO / 'tools/build_release_manifest.py'),
                 '--corpus', str(corpus), '--review', str(review.parent)],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            man = json.loads(r.stdout)
            self.assertEqual(man['manifest_kind'], 'release_candidate')
            t1 = man['volumes'][0]
            excluded = {x['file'] for x in t1['excluded_files']}
            self.assertIn('source/mertvaia-voda-red-2015.pdf', excluded)
            self.assertIn('source/extra-evidence.pdf', excluded)
            self.assertIn('sha256', t1['source_files']['osnovy-sociologii-tom-1.pdf'])


@unittest.skipUnless(MISTUNE, 'mistune not installed')
class ReleaseDryRunTest(unittest.TestCase):
    def test_dry_run_reports_gate_block_and_no_deploy(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            corpus = fixture_site_corpus(tmp)
            approval = review_dir(tmp, corpus, approved=False)
            out = tmp / 'candidate'
            r = subprocess.run(
                [sys.executable, str(REPO / 'tools/release_dry_run.py'),
                 '--corpus', str(corpus), '--approval', str(approval),
                 '--output', str(out)],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            rep = json.loads(r.stdout)
            self.assertEqual(rep['verdict'], 'BLOCKED-BY-GATE')
            self.assertFalse(rep['gate_approved'])
            self.assertTrue(any('Not approved' in x for x in rep['gate_blocking_reasons']))
            self.assertGreater(rep['file_count'], 0)
            idx = (out / 'index.html').read_text(encoding='utf-8')
            self.assertIn('не для публикации', idx.lower())
            self.assertEqual(rep['forbidden_content'], [])


class ExportRepoTest(unittest.TestCase):
    def test_export_is_whitelisted_and_clean(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / 'export'
            r = subprocess.run(
                [sys.executable, str(REPO / 'tools/export_public_repo.py'),
                 '--output', str(out)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((out / 'README.md').is_file())
            self.assertTrue((out / 'LICENSE').is_file())
            self.assertTrue((out / 'EXPORT_INDEX.json').is_file())
            all_rel = {p.relative_to(out).as_posix() for p in out.rglob('*') if p.is_file()}
            for bad in all_rel:
                self.assertFalse(bad.startswith(('review/', 'media_qa/', 'source_corpus/')),
                                 bad)
                self.assertNotIn('media_review_queue', bad)
                self.assertNotIn('ole_review_queue', bad)

    def test_export_contains_no_operator_identity(self):
        # The operator account name (from the private remote URL) must never
        # appear in exported files — neither as path nor as content.
        url = subprocess.run(['git', '-C', str(REPO), 'remote', 'get-url', 'origin'],
                             capture_output=True, text=True).stdout.strip()
        m = re.search(r'github\.com[:/]([^/]+)/', url)
        if not m:
            self.skipTest('origin remote is not a GitHub URL')
        owner = m.group(1).encode()
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / 'export'
            subprocess.run([sys.executable, str(REPO / 'tools/export_public_repo.py'),
                            '--output', str(out)], capture_output=True, text=True)
            for p in out.rglob('*'):
                if p.is_file():
                    self.assertNotIn(owner, p.read_bytes(),
                                     f'operator identity in {p.name}')


@unittest.skipUnless(MISTUNE, 'mistune not installed')
class ReaderHtmlContractTest(unittest.TestCase):
    """HTML contract check on a candidate build: search-index entries resolve
    to full section pages carrying context, citation button and PDF link.

    This is NOT a browser E2E: interactive selection/clipboard behaviour is
    tracked as HOLD in export/PUBLIC_STATUS.md."""

    def test_search_result_opens_full_context(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = fixture_site_corpus(Path(t))
            out = Path(t) / 'site'
            site_builder.build(corpus, out, 'candidate')
            idx = json.loads((out / 'data/search_index.json').read_text(encoding='utf-8'))
            hits = [x for x in idx if 'проверочный' in x['text'].lower()]
            self.assertTrue(hits)
            item = hits[0]
            page = (out / item['url']).read_text(encoding='utf-8')
            # result leads to the full section, not an isolated chunk
            self.assertIn('pager', page)          # prev/next navigation present
            self.assertIn('cite', page)           # copy-with-source button
            self.assertIn('osnovy-sociologii', page)  # source PDF link
            self.assertIn('<article>', page)          # full rendered section


if __name__ == '__main__':
    unittest.main()
