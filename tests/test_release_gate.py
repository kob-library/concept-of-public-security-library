#!/usr/bin/env python3
"""Unit tests for the fail-closed release gate and the public-build block check.

Covers the review-mandated semantics: source metrics in ``qa.json`` (embedded
OLE count, media omitted from inline Markdown) are historical facts that stay
non-zero after review; findings are closed via verified queue entries.
Also covers the audit findings: queue-coverage must be per distinct object,
and the release decision must be pinned to reviewed source bytes.
"""
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))
import release_gate  # noqa: E402


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def corpus_pins(corpus: Path):
    pins = {}
    for v in range(1, 7):
        src = corpus / f'tom-{v}/source'
        pin = {'doc_sha256': sha(src / f'osnovy-sociologii-tom-{v}.doc'),
               'pdf_sha256': sha(src / f'osnovy-sociologii-tom-{v}.pdf')}
        extras = {f.name: sha(f) for f in src.glob('*')
                  if f.name not in {f'osnovy-sociologii-tom-{v}.doc',
                                    f'osnovy-sociologii-tom-{v}.pdf'}}
        if extras:
            pin['extra_pdfs'] = extras
        pins[f'tom-{v}'] = pin
    return pins


def build_corpus(root: Path, qa_over=None, t1_manifest=None):
    qa_over = qa_over or {}
    for v in range(1, 7):
        d = root / f'tom-{v}'
        (d / 'data').mkdir(parents=True)
        (d / 'source').mkdir(parents=True)
        (d / 'assets/media_original').mkdir(parents=True)
        (d / 'data/sections.jsonl').write_text('{"id":"x"}\n', encoding='utf-8')
        (d / 'source' / f'osnovy-sociologii-tom-{v}.doc').write_bytes(b'DOC fixture v%d' % v)
        (d / 'source' / f'osnovy-sociologii-tom-{v}.pdf').write_bytes(
            b'%PDF-1.4 fixture v' + str(v).encode())
        if v == 1:
            man = {'volume': 1, 'source_original_sha256': sha(d / 'source/osnovy-sociologii-tom-1.doc'),
                   'pdf_source_sha256': sha(d / 'source/osnovy-sociologii-tom-1.pdf'),
                   'docx_media_without_body_use': []}
            man.update(t1_manifest or {})
            (d / 'data/manifest.json').write_text(json.dumps(man), encoding='utf-8')
        else:
            qa = {'source_text_split_lossless': True,
                  'omitted_inline_media_unique': 0,
                  'docx_media_not_placed': [],
                  'OLE_objects': 0}
            qa.update(qa_over.get(v, {}))
            (d / 'data/qa.json').write_text(json.dumps(qa), encoding='utf-8')
    return root


def media_row(corpus: Path, volume: int, image: str, status='verified', **kw):
    f = corpus / f'tom-{volume}/assets/media_original' / image
    f.parent.mkdir(parents=True, exist_ok=True)
    if not f.exists():
        f.write_bytes(b'fixture image bytes for ' + image.encode())
    row = {'id': f'os-t{volume}-{image}', 'volume': volume, 'image': image,
           'source_original_path': f'assets/media_original/{image}',
           'source_file_sha256': sha(f), 'human_review_status': status,
           'manual_decision': 'ok'}
    row.update(kw)
    return row


def ole_row(volume: int, saved_as: str, status='verified', **kw):
    row = {'saved_as': saved_as, 'volume': volume, 'sha256': '1' * 64,
           'human_review_status': status}
    row.update(kw)
    return row


def write_review(rdir: Path, corpus: Path, approved=True, media=None, ole=None,
                 pins=None, allowlist=None, scope=None):
    rdir.mkdir(parents=True, exist_ok=True)
    all_flags = release_gate.BASE_REQUIRED + [
        'illustrations_and_ole_review', 'rights_and_third_party_art_review']
    approval = {k: approved for k in all_flags}
    approval.update({'approved_by': 'Reviewer Name',
                     'approval_date': '2026-10-01',
                     'release_scope': scope or {'name': 'six volumes',
                                                'volumes': [1, 2, 3, 4, 5, 6],
                                                'media': 'allowlist',
                                                'pdfs': 'ship'}})
    if pins is not None:
        approval['source_pins'] = pins
    (rdir / 'release_approval.json').write_text(json.dumps(approval), encoding='utf-8')
    (rdir / 'media_review_queue.jsonl').write_text(
        ''.join(json.dumps(m, ensure_ascii=False) + '\n' for m in (media or [])),
        encoding='utf-8')
    (rdir / 'ole_review_queue.jsonl').write_text(
        ''.join(json.dumps(o, ensure_ascii=False) + '\n' for o in (ole or [])),
        encoding='utf-8')
    if allowlist is not None:
        (rdir / 'public_media_allowlist.jsonl').write_text(
            ''.join(json.dumps(a, ensure_ascii=False) + '\n' for a in allowlist),
            encoding='utf-8')
    return rdir / 'release_approval.json'


def allowlist_entry(corpus: Path, volume: int, name: str, scope='six volumes',
                    decision='approved', source_image=None, **kw):
    preview = corpus / f'tom-{volume}/assets/media' / name
    entry = {'volume': volume, 'path': f'assets/media/{name}',
             'sha256': sha(preview), 'source_image': source_image or name,
             'reviewer': 'Reviewer Name', 'decision': decision, 'scope': scope}
    entry.update(kw)
    return entry


class ReleaseGateTest(unittest.TestCase):
    def run_gate(self, qa_overrides=None, approved=True, media=None, ole=None,
                 t1_manifest=None, pins='auto', tmp=None):
        tmp = tmp or Path(tempfile.mkdtemp())
        corpus = tmp / 'corpus'
        build_corpus(corpus, qa_overrides, t1_manifest)
        if media is None:
            media = [media_row(corpus, 2, 'image9.png')]
        if ole is None:
            ole = [ole_row(3, 'ole/tom-3-oleObject1.bin')]
        if pins == 'auto':
            pins = corpus_pins(corpus)
        approval = write_review(tmp / 'review', corpus, approved=approved,
                                media=media, ole=ole, pins=pins)
        return release_gate.check_release(corpus, approval), corpus

    def test_fully_approved_passes(self):
        (ok, reasons, notes), _ = self.run_gate()
        self.assertTrue(ok, reasons)

    def test_missing_approval_blocks(self):
        (ok, reasons, _), _ = self.run_gate(approved=False)
        self.assertFalse(ok)
        self.assertTrue(any(r.startswith('Not approved:') for r in reasons))

    def test_missing_source_pins_blocks(self):
        (ok, reasons, _), _ = self.run_gate(pins=None)
        self.assertFalse(ok)
        self.assertTrue(any('pin' in r.lower() for r in reasons))

    def test_source_pdf_swap_after_approval_blocks(self):
        with tempfile.TemporaryDirectory() as t:
            (ok0, _, _), corpus = self.run_gate(tmp=Path(t))
            self.assertTrue(ok0)
            (corpus / 'tom-2/source/osnovy-sociologii-tom-2.pdf').write_bytes(b'%PDF OTHER EDITION')
            approval = Path(t) / 'review/release_approval.json'
            ok, reasons, _ = release_gate.check_release(corpus, approval)
            self.assertFalse(ok)
            self.assertTrue(any('sha256' in r and 'tom-2' in r.lower() or 'Volume 2' in r
                                for r in reasons), reasons)

    def test_source_doc_swap_after_approval_blocks(self):
        with tempfile.TemporaryDirectory() as t:
            (ok0, _, _), corpus = self.run_gate(tmp=Path(t))
            self.assertTrue(ok0)
            (corpus / 'tom-4/source/osnovy-sociologii-tom-4.doc').write_bytes(b'DOC altered')
            approval = Path(t) / 'review/release_approval.json'
            ok, reasons, _ = release_gate.check_release(corpus, approval)
            self.assertFalse(ok)

    def test_verified_media_file_missing_blocks(self):
        with tempfile.TemporaryDirectory() as t:
            (ok0, _, _), corpus = self.run_gate(tmp=Path(t))
            self.assertTrue(ok0)
            (corpus / 'tom-2/assets/media_original/image9.png').unlink()
            approval = Path(t) / 'review/release_approval.json'
            ok, reasons, _ = release_gate.check_release(corpus, approval)
            self.assertFalse(ok)

    def test_verified_media_bytes_swapped_blocks(self):
        with tempfile.TemporaryDirectory() as t:
            (ok0, _, _), corpus = self.run_gate(tmp=Path(t))
            self.assertTrue(ok0)
            (corpus / 'tom-2/assets/media_original/image9.png').write_bytes(b'forged')
            approval = Path(t) / 'review/release_approval.json'
            ok, reasons, _ = release_gate.check_release(corpus, approval)
            self.assertFalse(ok)

    def test_ole_pending_blocks_without_requiring_removal(self):
        (ok, reasons, notes), _ = self.run_gate(
            qa_overrides={3: {'OLE_objects': 1}},
            ole=[ole_row(3, 'ole/tom-3-oleObject1.bin', status='pending')])
        self.assertFalse(ok)
        self.assertTrue(any('embedded OLE objects not human-verified' in r for r in reasons))

    def test_verified_ole_closes_finding_and_keeps_source_metric(self):
        qa_over = {3: {'OLE_objects': 1}}
        with tempfile.TemporaryDirectory() as t:
            (ok, reasons, notes), corpus = self.run_gate(
                qa_over, ole=[ole_row(3, 'ole/tom-3-oleObject1.bin')], tmp=Path(t))
            self.assertTrue(ok, reasons)
            qa = json.loads((corpus / 'tom-3/data/qa.json').read_text(encoding='utf-8'))
            self.assertEqual(qa['OLE_objects'], 1)
            self.assertTrue(any('1 verified' in n for n in notes))

    def test_ole_duplicate_queue_row_does_not_cover_other_object(self):
        # Two distinct OLE objects in the source, but the SAME saved_as verified twice.
        (ok, reasons, _), _ = self.run_gate(
            qa_overrides={3: {'OLE_objects': 2}},
            ole=[ole_row(3, 'ole/tom-3-oleObject1.bin'),
                 ole_row(3, 'ole/tom-3-oleObject1.bin')])
        self.assertFalse(ok)
        self.assertTrue(any('embedded OLE' in r for r in reasons), reasons)

    def test_ole_distinct_saved_as_required_per_object(self):
        # Same binary content at two insertion points stays two identities.
        rows = [ole_row(3, 'ole/tom-3-oleObject1.bin', sha256='a' * 64),
                ole_row(3, 'ole/tom-3-oleObject2.bin', sha256='a' * 64)]
        (ok, reasons, _), _ = self.run_gate(
            qa_overrides={3: {'OLE_objects': 2}}, ole=rows)
        self.assertTrue(ok, reasons)

    def test_omitted_media_pending_blocks(self):
        corpus_qa = {2: {'omitted_inline_media_unique': 1,
                         'docx_media_not_placed': ['image9.png']}}
        (ok, reasons, _), _ = self.run_gate(
            qa_overrides=corpus_qa,
            media=[media_row(Path(tempfile.mkdtemp()) / 'unused', 2, 'image9.png',
                             status='pending')])
        self.assertFalse(ok)
        self.assertTrue(any('omitted media lack a verified review entry' in r for r in reasons))

    def test_omitted_media_verified_closes_finding(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = build_corpus(Path(t) / 'corpus',
                                  {2: {'omitted_inline_media_unique': 1,
                                       'docx_media_not_placed': ['image9.png']}})
            row = media_row(corpus, 2, 'image9.png')
            approval = write_review(Path(t) / 'review', corpus, media=[row],
                                    ole=[ole_row(3, 'ole/tom-3-oleObject1.bin')],
                                    pins=corpus_pins(corpus))
            ok, reasons, _ = release_gate.check_release(corpus, approval)
            self.assertTrue(ok, reasons)

    def test_omitted_media_count_without_list_blocks(self):
        (ok, reasons, _), _ = self.run_gate(
            qa_overrides={2: {'omitted_inline_media_unique': 2,
                              'docx_media_not_placed': []}},
            media=[media_row(Path(tempfile.mkdtemp()) / 'x', 2, 'unrelated.png')])
        self.assertFalse(ok)
        self.assertTrue(any('cannot match against review queue' in r for r in reasons))

    def test_tom1_non_inline_media_require_review(self):
        (ok, reasons, _), _ = self.run_gate(
            t1_manifest={'docx_media_without_body_use': ['image15.png']})
        self.assertFalse(ok)
        self.assertTrue(any('Volume 1' in r for r in reasons), reasons)

    def test_tom1_non_inline_media_verified_passes(self):
        with tempfile.TemporaryDirectory() as t:
            corpus = build_corpus(Path(t) / 'corpus',
                                  t1_manifest={'docx_media_without_body_use': ['image15.png']})
            media = [media_row(corpus, 2, 'image9.png'), media_row(corpus, 1, 'image15.png')]
            approval = write_review(Path(t) / 'review', corpus, media=media,
                                    ole=[ole_row(3, 'ole/tom-3-oleObject1.bin')],
                                    pins=corpus_pins(corpus))
            ok, reasons, _ = release_gate.check_release(corpus, approval)
            self.assertTrue(ok, reasons)


@unittest.skipUnless(importlib.util.find_spec('mistune'),
                     'site_builder dependency mistune not installed')
class PublicBuildBlockTest(unittest.TestCase):
    def test_public_build_refused_with_reasons_and_no_output(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            corpus = build_corpus(t / 'corpus')
            approval = write_review(t / 'review', corpus, approved=False,
                                    media=[media_row(corpus, 2, 'image9.png')],
                                    ole=[ole_row(3, 'ole/tom-3-oleObject1.bin')],
                                    pins=corpus_pins(corpus))
            out = t / 'public_site'
            r = subprocess.run(
                [sys.executable, str(REPO / 'tools/assert_public_blocked.py'),
                 '--corpus', str(corpus), '--output', str(out),
                 '--release-approval', str(approval),
                 '--expect', 'Not approved: editorial_text_review'],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse(out.exists())

    def test_checker_fails_when_expected_reason_absent(self):
        # The negative check must fail, not pass on a generic non-zero exit,
        # when the gate's refusal lacks an expected reason.
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            corpus = build_corpus(t / 'corpus')
            approval = write_review(t / 'review', corpus, approved=False,
                                    media=[media_row(corpus, 2, 'image9.png')],
                                    ole=[ole_row(3, 'ole/tom-3-oleObject1.bin')],
                                    pins=corpus_pins(corpus))
            r = subprocess.run(
                [sys.executable, str(REPO / 'tools/assert_public_blocked.py'),
                 '--corpus', str(corpus), '--output', str(t / 'pub2'),
                 '--release-approval', str(approval),
                 '--expect', 'no-such-reason-ever'],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)


class ReleaseScopeTest(unittest.TestCase):
    """Owner decision (issue #2): a limited *approved scope* must work without
    fabricating reviews — pending media/OLE queues stay HOLD, not blockers,
    when the declared scope ships no graphics at all."""

    TEXT_FIRST = {'name': 'text-first', 'volumes': [1, 2, 3, 4, 5, 6],
                  'media': 'none', 'pdfs': 'reference',
                  'canonical_pdf_url': 'https://www.vodaspb.ru/archive'}

    def write(self, tmp, corpus, scope, approved=True, **kw):
        return write_review(tmp / 'review', corpus, approved=approved,
                            pins=corpus_pins(corpus), scope=scope, **kw)

    def test_text_first_passes_with_pending_media_and_ole(self):
        # Only base flags approved; media/art flags left False; queues pending.
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            corpus = build_corpus(tmp / 'corpus')
            app = write_review(
                tmp / 'review', corpus, approved=False, pins=corpus_pins(corpus),
                scope=self.TEXT_FIRST,
                media=[media_row(corpus, 2, 'image9.png', status='pending')],
                ole=[ole_row(3, 'ole/tom-3-oleObject1.bin', status='pending')])
            approval = json.loads(app.read_text(encoding='utf-8'))
            for f in release_gate.BASE_REQUIRED:
                approval[f] = True
            app.write_text(json.dumps(approval), encoding='utf-8')
            ok, reasons, notes = release_gate.check_release(corpus, app)
            self.assertTrue(ok, reasons)
            self.assertTrue(any('not shipped in this scope' in n for n in notes))

    def test_text_first_blocked_without_base_flags(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            corpus = build_corpus(tmp / 'corpus')
            app = self.write(tmp, corpus, self.TEXT_FIRST, approved=False,
                             media=[media_row(corpus, 2, 'image9.png', status='pending')])
            ok, reasons, _ = release_gate.check_release(corpus, app)
            self.assertFalse(ok)
            self.assertTrue(any('Not approved' in r for r in reasons), reasons)

    def test_media_allowlist_not_required_when_media_none(self):
        # Preview files on disk must NOT demand an allowlist when the declared
        # scope ships no graphics — they simply do not enter the artifact.
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            corpus = build_corpus(tmp / 'corpus')
            (corpus / 'tom-2/assets/media/image9.png').parent.mkdir(
                parents=True, exist_ok=True)
            (corpus / 'tom-2/assets/media/image9.png').write_bytes(b'px')
            app = self.write(tmp, corpus, self.TEXT_FIRST, media=[])
            ok, reasons, _ = release_gate.check_release(corpus, app)
            self.assertTrue(ok, reasons)

    def test_ship_pdfs_requires_third_party_art_review(self):
        # PDFs carry covers/embedded third-party art: shipping them without
        # the art-rights flag must block even when everything else is approved.
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            corpus = build_corpus(tmp / 'corpus')
            scope = dict(self.TEXT_FIRST, pdfs='ship')
            app = write_review(tmp / 'review', corpus, approved=True,
                               pins=corpus_pins(corpus), scope=scope,
                               media=[media_row(corpus, 2, 'image9.png')],
                               ole=[ole_row(3, 'ole/tom-3-oleObject1.bin')])
            approval = json.loads(app.read_text(encoding='utf-8'))
            approval['rights_and_third_party_art_review'] = False
            app.write_text(json.dumps(approval), encoding='utf-8')
            ok, reasons, _ = release_gate.check_release(corpus, app)
            self.assertFalse(ok)
            self.assertTrue(any('rights_and_third_party_art_review' in r
                                for r in reasons), reasons)

    def test_malformed_scope_blocks(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            corpus = build_corpus(tmp / 'corpus')
            app = self.write(tmp, corpus, scope='everything', media=[])
            ok, reasons, _ = release_gate.check_release(corpus, app)
            self.assertFalse(ok)
            self.assertTrue(any('release_scope' in r for r in reasons), reasons)


class MediaAllowlistTest(unittest.TestCase):
    """P0 (PR#3 review): publishable preview media are allowlist-driven.

    The building blocks reproduce the audit attack: on a fully approved
    corpus, adding an unreviewed image with consistent Markdown/indexes must
    block the release; byte substitution of an approved preview must block;
    only a matching approved entry (chained to a verified queue original)
    passes.
    """

    def build_with_preview(self, tmp: Path):
        corpus = build_corpus(tmp / 'corpus')
        preview = corpus / 'tom-2/assets/media/image9.png'
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b'approved preview bytes')
        media = [media_row(corpus, 2, 'image9.png')]
        return corpus, media

    def write(self, tmp, corpus, media, allowlist_args=None):
        al = None if allowlist_args is None else [allowlist_entry(corpus, 2, 'image9.png', **allowlist_args)]
        return write_review(tmp / 'review', corpus, approved=True, media=media,
                            ole=[ole_row(3, 'ole/tom-3-oleObject1.bin')],
                            pins=corpus_pins(corpus), allowlist=al)

    def test_preview_without_allowlist_blocks(self):
        # no allowlist file at all while preview files exist
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            corpus, media = self.build_with_preview(tmp)
            app = self.write(tmp, corpus, media)
            ok, reasons, _ = release_gate.check_release(corpus, app)
            self.assertFalse(ok)
            self.assertTrue(any('allowlist missing' in r for r in reasons), reasons)

    def test_audit_attack_new_media_blocks_and_is_not_shipped(self):
        """Approved corpus -> attacker adds consistent md/indexes + new image
        -> gate must FAIL and the file must not enter the public artifact."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            corpus, media = self.build_with_preview(tmp)
            app = self.write(tmp, corpus, media, allowlist_args={})
            ok0, r0, _ = release_gate.check_release(corpus, app)
            self.assertTrue(ok0, r0)  # positive control
            # attack: new unreviewed preview + media_original bytes
            rogue = corpus / 'tom-2/assets/media/attack_new_image.png'
            rogue.write_bytes(b'unreviewed third-party content')
            (corpus / 'tom-2/assets/media_original/attack_new_image.png').write_bytes(b'rogue orig')
            ok, reasons, _ = release_gate.check_release(corpus, app)
            self.assertFalse(ok)
            self.assertTrue(any('not allowlisted' in r for r in reasons), reasons)
            # artifact-side check lives in tests/test_public_output.py
            # (test_unreviewed_image_absent_from_candidate_artifact)

    def test_preview_bytes_changed_after_approval_blocks(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            corpus, media = self.build_with_preview(tmp)
            app = self.write(tmp, corpus, media, allowlist_args={})
            ok0, r0, _ = release_gate.check_release(corpus, app)
            self.assertTrue(ok0, r0)
            # original untouched, preview bytes silently replaced
            (corpus / 'tom-2/assets/media/image9.png').write_bytes(b'tampered preview')
            ok, reasons, _ = release_gate.check_release(corpus, app)
            self.assertFalse(ok)
            self.assertTrue(any('bytes differ' in r for r in reasons), reasons)

    def test_allowlist_entry_requires_verified_original(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            corpus, media = self.build_with_preview(tmp)
            # allowlist entry points at an original that is NOT verified
            app = self.write(tmp, corpus, media, allowlist_args={'source_image': 'other.png'})
            ok, reasons, _ = release_gate.check_release(corpus, app)
            self.assertFalse(ok)
            self.assertTrue(any('verified queue original' in r for r in reasons), reasons)

    def test_duplicate_allowlist_entry_blocks(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            corpus, media = self.build_with_preview(tmp)
            e = allowlist_entry(corpus, 2, 'image9.png')
            app = write_review(tmp / 'review', corpus, approved=True, media=media,
                               ole=[ole_row(3, 'ole/tom-3-oleObject1.bin')],
                               pins=corpus_pins(corpus), allowlist=[e, e])
            ok, reasons, _ = release_gate.check_release(corpus, app)
            self.assertFalse(ok)
            self.assertTrue(any('duplicate' in r for r in reasons), reasons)


if __name__ == '__main__':
    unittest.main()
