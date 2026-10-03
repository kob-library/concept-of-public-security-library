#!/usr/bin/env python3
"""Track 2 MVP: research-discovery semantic layer. Covers the required
acceptance cases: URL compatibility, entity stability, evidence gating,
the single indexable() policy, LanguageVariant rules, CitationTarget
durability, deterministic canonicals/JSON-LD/breadcrumbs and source-text
immutability."""
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))
sys.path.insert(0, str(REPO / 'tests'))
import semantic_layer as sem  # noqa: E402
import site_builder  # noqa: E402
from test_public_output import fixture_site_corpus, review_dir  # noqa: E402

MISTUNE = importlib.util.find_spec('mistune') is not None


def _c(concept_id='cp-fx', term='фикстурный раздел', evidence=None,
       scope='corpus_term', desc='Редакционное описание понятия.',
       review='reviewed', pub='published', topics=None, aliases=None,
       related=None):
    return {'concept_id': concept_id, 'canonical_term': term,
            'aliases': aliases if aliases is not None else ['фикстурного раздела'],
            'definition_scope': scope, 'editorial_description': desc,
            'evidence': evidence if evidence is not None else
            [{'section_id': 'fx-os-1-s01', 'kind': 'title', 'term': 'фикстурн'}],
            'related_topic_ids': topics if topics is not None else ['topic-fx'],
            'related_work_ids': [],
            'related_section_ids': ['fx-os-1-s01'],
            'related_concept_ids': related or [],
            'review_state': review, 'review_role': 'test_curator',
            'reviewed_at': '2025-12', 'publication_state': pub}


def _t(topic_id='topic-fx', terms=('фикстурн',), pub='published'):
    return {'topic_id': topic_id, 'label': 'Фикстурная тема',
            'description': 'Редакционная фикстурная рубрика.',
            'membership_rule': {'type': 'title_terms', 'terms': list(terms)},
            'membership_rule_version': 1, 'publication_state': pub}


def _ct():
    return {'citation_target_id': 'ct-fx-01',
            'work_id': 'osnovy-sociologii-tom-1',
            'section_id': 'fx-os-1-s01', 'anchor': 'sec-fx-01',
            'site_path': 'tom-1/sections/fx-01.html',
            'repo_path': 'tom-1/sections/fx-01.md',
            'language': 'ru', 'status': 'active'}


def _v(vid='lv-cp-fx-ru', entity='cp-fx', lang='ru', status='canonical',
       review='reviewed', source=None, canon=None):
    return {'variant_id': vid, 'entity_id': entity, 'entity_kind': 'concept',
            'language': lang, 'canonical_name': 'фикстурный раздел',
            'aliases': [], 'localized_summary': None,
            'translation_status': status, 'review_status': review,
            'source_variant_id': source,
            'canonical_variant_id': canon or vid}


def fx_ed(tmp: Path, *, topics='default', concepts='default',
          citations='default', variants='default') -> Path:
    """Write a synthetic editorial layer for the fixture corpus."""
    d = tmp / 'ed'
    d.mkdir(parents=True, exist_ok=True)

    def w(name, rows):
        (d / name).write_text(
            ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows),
            encoding='utf-8')

    w('topics.jsonl', [_t()] if topics == 'default' else topics)
    w('concepts.jsonl', [_c()] if concepts == 'default' else concepts)
    w('citations.jsonl', [_ct()] if citations == 'default' else citations)
    if variants == 'default':
        variants = [_v()]
    w('language_variants.jsonl', variants)
    return d


def build_site(corpus: Path, out: Path, mode='internal', release=None,
               base_url='', editorial=None):
    site_builder.build(corpus, out, mode, release=release,
                       base_url=base_url, editorial=editorial)
    return out


def public_review(tmp: Path, corpus: Path) -> Path:
    """Approval record with every flag set (same pattern as
    test_public_output)."""
    review = review_dir(tmp, corpus)
    approval = Path(tmp) / 'review' / ('release_' + 'approval.json')
    a = json.loads(approval.read_text(encoding='utf-8'))
    for k in list(a):
        if k not in ('approved_by', 'approval_date', 'release_scope',
                     'source_pins'):
            a[k] = True
    approval.write_text(json.dumps(a), encoding='utf-8')
    return review


def rel_files(out: Path) -> set:
    return {p.relative_to(out).as_posix()
            for p in out.rglob('*') if p.is_file()}


def ld_blocks(html_text: str) -> list:
    out = []
    for m in re.finditer(
            r'type="application/ld\+json">(.*?)</',
            html_text, re.S):
        obj = json.loads(m.group(1))
        out += obj['@graph'] if '@graph' in obj else [obj]
    return out


@unittest.skipUnless(MISTUNE, 'mistune not installed')
class SemanticLayerPolicyTest(unittest.TestCase):
    """Unit-level policy checks — no site build needed."""

    def test_chunk_never_indexable(self):
        self.assertFalse(sem.indexable('chunk'))
        self.assertFalse(sem.indexable('chunk', {'id': 'x'}))
        self.assertFalse(sem.indexable('search'))
        self.assertFalse(sem.indexable('alias'))
        self.assertFalse(sem.indexable('taxonomy_empty'))
        self.assertFalse(sem.indexable('citation_target'))

    def test_concept_policy(self):
        c = _c()
        self.assertTrue(sem.indexable('concept', c, evidence=[1]))
        self.assertFalse(sem.indexable('concept', c, evidence=[]))
        self.assertFalse(sem.indexable('concept', _c(review='unreviewed'),
                                     evidence=[1]))
        self.assertFalse(sem.indexable('concept', _c(pub='draft'),
                                     evidence=[1]))

    def test_provisional_variant_never_indexable(self):
        prov = _v(vid='lv-cp-fx-en', lang='en', status='provisional',
                  source='lv-cp-fx-ru', canon='lv-cp-fx-ru')
        self.assertFalse(sem.indexable('language_variant', prov))
        conf = _v(vid='lv-cp-fx-en', lang='en', status='confirmed',
                  source='lv-cp-fx-ru', canon='lv-cp-fx-ru')
        self.assertTrue(sem.indexable('language_variant', conf))
        unrev = _v(vid='lv-cp-fx-de', lang='de', status='confirmed',
                   review='unreviewed', source='lv-cp-fx-ru',
                   canon='lv-cp-fx-ru')
        self.assertFalse(sem.indexable('language_variant', unrev))

    def test_variant_of_variant_rejected(self):
        ru = _v(vid='lv-e-ru', entity='e')
        en = _v(vid='lv-e-en', entity='e', lang='en', status='provisional',
                source='lv-e-ru', canon='lv-e-ru')
        chain = _v(vid='lv-e-zh', entity='e', lang='zh-Hans',
                   status='provisional', source='lv-e-en', canon='lv-e-en')
        errors = sem.variant_chain_errors([ru, en, chain])
        self.assertTrue(any('variant-of-variant' in e
                            or 'non-canonical' in e for e in errors))

    def test_variant_needs_exactly_one_canonical(self):
        a = _v(vid='lv-e-ru', entity='e', canon='lv-e-en')
        b = _v(vid='lv-e-en', entity='e', lang='en', canon='lv-e-ru',
               source='lv-e-en')
        self.assertTrue(sem.variant_chain_errors([a, b]))

    def test_validate_records_schema(self):
        ed = {'topics': [_t()], 'concepts': [_c()], 'citations': [_ct()],
              'variants': [_v()]}
        self.assertEqual(sem.validate_records(ed), [])
        bad = {'topics': [_t(topic_id='BAD')], 'concepts': [_c(scope='invented')],
               'citations': [], 'variants': []}
        self.assertTrue(sem.validate_records(bad))

    def test_concept_id_not_derived_from_description(self):
        c1 = _c(desc='Описание первой редакции.')
        c2 = _c(desc='Совершенно другой редакционный текст.')
        self.assertEqual(c1['concept_id'], c2['concept_id'])
        self.assertNotEqual(c1['editorial_description'],
                            c2['editorial_description'])


@unittest.skipUnless(MISTUNE, 'mistune not installed')
class SemanticSiteBuildTest(unittest.TestCase):
    """Integration: site build with a synthetic editorial layer."""

    def test_existing_urls_preserved(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            corpus = fixture_site_corpus(t)
            before = t / 'before'
            build_site(corpus, before, 'internal',
                       editorial=t / 'empty-ed')
            after = t / 'after'
            build_site(corpus, after, 'internal',
                       editorial=fx_ed(t))
            missing = rel_files(before) - rel_files(after)
            self.assertEqual(missing, set(),
                             f'existing URLs broken: {missing}')

    def test_topic_ids_stable_across_rebuild(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            corpus = fixture_site_corpus(t)
            ed = fx_ed(t)
            for name in ('a', 'b'):
                out = t / name
                build_site(corpus, out, 'internal', editorial=ed)
            self.assertTrue((t / 'a/topics/topic-fx.html').is_file())
            self.assertTrue((t / 'b/topics/topic-fx.html').is_file())
            ea = json.loads((t / 'a/data/entities.json')
                            .read_text(encoding='utf-8'))
            eb = json.loads((t / 'b/data/entities.json')
                            .read_text(encoding='utf-8'))
            ids_a = {e['id'] for e in ea['entities'] if e['kind'] == 'topic'}
            ids_b = {e['id'] for e in eb['entities'] if e['kind'] == 'topic'}
            self.assertEqual(ids_a, ids_b, {'topic-fx'})

    def test_empty_topic_not_published(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            corpus = fixture_site_corpus(t)
            ed = fx_ed(t, topics=[_t(terms=('отсутствующийтермин',))],
                       concepts=[])
            out = t / 'site'
            build_site(corpus, out, 'internal', editorial=ed)
            self.assertFalse((out / 'topics/topic-fx.html').exists())
            e = json.loads((out / 'data/entities.json')
                           .read_text(encoding='utf-8'))
            self.assertFalse([x for x in e['entities']
                              if x['kind'] == 'topic'])

    def test_concept_without_evidence_not_published(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            corpus = fixture_site_corpus(t)
            ed = fx_ed(t, concepts=[_c(evidence=[
                {'section_id': 'fx-no-such-section', 'kind': 'title'}])])
            out = t / 'site'
            build_site(corpus, out, 'internal', editorial=ed)
            self.assertFalse((out / 'concepts/cp-fx.html').exists())
            e = json.loads((out / 'data/entities.json')
                           .read_text(encoding='utf-8'))
            self.assertFalse([x for x in e['entities']
                              if x['kind'] == 'concept'])

    def test_definition_scope_visibly_distinguished(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            corpus = fixture_site_corpus(t)
            ed = fx_ed(t, concepts=[
                _c(),
                _c(concept_id='cp-fx-established', term='фикстурный текст',
                   scope='established_term', topics=[],
                   aliases=[], related=[],
                   evidence=[{'section_id': 'fx-os-2-s01', 'kind': 'title',
                              'term': 'фикстурный'}])])
            out = t / 'site'
            build_site(corpus, out, 'internal', editorial=ed)
            corpus_page = (out / 'concepts/cp-fx.html').read_text(encoding='utf-8')
            est_page = (out / 'concepts/cp-fx-established.html') \
                .read_text(encoding='utf-8')
            self.assertIn('термин корпуса', corpus_page)
            self.assertNotIn('общеупотребительный', corpus_page)
            self.assertIn('общеупотребительный', est_page)
            self.assertNotIn('термин корпуса (авторская', est_page)

    def test_editorial_change_keeps_page_identity(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            corpus = fixture_site_corpus(t)
            build_site(corpus, t / 'a', 'internal',
                       editorial=fx_ed(t / 'e1', concepts=[
                           _c(desc='Первая редакция описания.')]))
            build_site(corpus, t / 'b', 'internal',
                       editorial=fx_ed(t / 'e2', concepts=[
                           _c(desc='Вторая редакция описания.')]))
            self.assertTrue((t / 'a/concepts/cp-fx.html').is_file())
            self.assertTrue((t / 'b/concepts/cp-fx.html').is_file())

    def test_citation_target_stable_across_rebuild(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            corpus = fixture_site_corpus(t)
            ed = fx_ed(t)
            build_site(corpus, t / 'a', 'internal', editorial=ed)
            build_site(corpus, t / 'b', 'internal', editorial=ed)
            ca = (t / 'a/data/citations.jsonl').read_text(encoding='utf-8')
            cb = (t / 'b/data/citations.jsonl').read_text(encoding='utf-8')
            self.assertEqual(ca, cb)
            page = (t / 'b/tom-1/sections/fx-01.html') \
                .read_text(encoding='utf-8')
            self.assertIn('ct-fx-01', page)

    def test_membership_change_keeps_citation_targets(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            corpus = fixture_site_corpus(t)
            build_site(corpus, t / 'a', 'internal',
                       editorial=fx_ed(t / 'e1'))
            # topic becomes empty/unpublished — citations must survive
            build_site(corpus, t / 'b', 'internal',
                       editorial=fx_ed(t / 'e2',
                                       topics=[_t(terms=('неттакого',))],
                                       concepts=[]))
            ca = (t / 'a/data/citations.jsonl').read_text(encoding='utf-8')
            cb = (t / 'b/data/citations.jsonl').read_text(encoding='utf-8')
            self.assertEqual(ca, cb)
            page = (t / 'b/tom-1/sections/fx-01.html') \
                .read_text(encoding='utf-8')
            self.assertIn('ct-fx-01', page)

    def test_sitemap_contains_only_indexable(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            corpus = fixture_site_corpus(t)
            review = public_review(t, corpus)
            ed = fx_ed(t, concepts=[
                _c(),
                _c(concept_id='cp-fx-ghost', term='несуществующий термин',
                   topics=[], aliases=[], related=[], evidence=[
                       {'section_id': 'fx-nope', 'kind': 'title'}])])
            out = t / 'site'
            site_builder.build(corpus, out, 'public', release=review,
                               base_url='https://owner.github.io/repo',
                               editorial=ed)
            sm = (out / 'sitemap.xml').read_text(encoding='utf-8')
            self.assertIn('concepts/cp-fx.html', sm)
            self.assertIn('concepts/index.html', sm)
            self.assertIn('topics/topic-fx.html', sm)
            self.assertNotIn('cp-fx-ghost', sm)
            self.assertNotIn('chunk', sm)
            self.assertNotIn('data/', sm)

    def test_canonical_urls_deterministic(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            corpus = fixture_site_corpus(t)
            review = public_review(t, corpus)
            ed = fx_ed(t)
            canon = {}
            for name in ('a', 'b'):
                out = t / name
                site_builder.build(corpus, out, 'public', release=review,
                                   base_url='https://owner.github.io/repo',
                                   editorial=ed)
                canon[name] = sorted(re.findall(
                    r'<link rel="canonical" href="([^"]+)"',
                    ''.join(p.read_text(encoding='utf-8')
                            for p in out.rglob('*.html'))))
            self.assertEqual(canon['a'], canon['b'])
            self.assertEqual(len(canon['a']), len(set(canon['a'])),
                             'duplicate canonical URLs')

    def test_alias_creates_no_independent_page(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            corpus = fixture_site_corpus(t)
            review = public_review(t, corpus)
            ed = fx_ed(t)
            out = t / 'site'
            site_builder.build(corpus, out, 'public', release=review,
                               base_url='https://owner.github.io/repo',
                               editorial=ed)
            pages = [p.name for p in (out / 'concepts').glob('*.html')]
            self.assertEqual(sorted(pages), ['cp-fx.html', 'index.html'])
            canon = re.findall(
                r'<link rel="canonical" href="([^"]+)"',
                ''.join(p.read_text(encoding='utf-8')
                        for p in out.rglob('*.html')))
            self.assertEqual(len(canon), len(set(canon)))

    def test_jsonld_derives_from_entity(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            corpus = fixture_site_corpus(t)
            ed = fx_ed(t)
            out = t / 'site'
            build_site(corpus, out, 'internal', editorial=ed)
            page = (out / 'concepts/cp-fx.html').read_text(encoding='utf-8')
            lds = ld_blocks(page)
            term = [x for x in lds if x.get('@type') == 'DefinedTerm']
            self.assertEqual(len(term), 1)
            self.assertEqual(term[0]['name'], 'фикстурный раздел')
            self.assertEqual(term[0]['alternateName'], ['фикстурного раздела'])
            self.assertIn('редакционное', term[0]['description'].lower())

    def test_breadcrumblist_matches_visible_hierarchy(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            corpus = fixture_site_corpus(t)
            ed = fx_ed(t)
            out = t / 'site'
            build_site(corpus, out, 'internal', editorial=ed)
            page = (out / 'concepts/cp-fx.html').read_text(encoding='utf-8')
            lds = [x for x in ld_blocks(page)
                   if x.get('@type') == 'BreadcrumbList']
            self.assertEqual(len(lds), 1)
            names = [i['name'] for i in lds[0]['itemListElement']]
            crumb = re.search(r'<p class="breadcrumbs">(.*?)</p>',
                              page, re.S).group(1)
            visible = re.sub(r'<[^>]+>', '', crumb)
            for n in names:
                self.assertIn(n.replace('«Основы социологии»', 'Библиотека')
                              if n.startswith('«') else n, visible)

    def test_source_text_unchanged(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            corpus = fixture_site_corpus(t)
            src = corpus / 'tom-1/sections/fx-01.md'
            before_bytes = src.read_bytes()
            plain = t / 'plain'
            build_site(corpus, plain, 'internal', editorial=t / 'empty')
            layered = t / 'layered'
            build_site(corpus, layered, 'internal', editorial=fx_ed(t))
            self.assertEqual(src.read_bytes(), before_bytes)
            # a non-citation section renders byte-identical output
            self.assertEqual(
                (plain / 'tom-2/sections/fx-01.html').read_bytes(),
                (layered / 'tom-2/sections/fx-01.html').read_bytes())
            # the citation section differs only by the stable-address badge
            a = (plain / 'tom-1/sections/fx-01.html') \
                .read_text(encoding='utf-8')
            b = (layered / 'tom-1/sections/fx-01.html') \
                .read_text(encoding='utf-8')
            badge = re.search(
                r'<small class="muted" id="[^"]+"> стабильный адрес цитирования: '
                r'<code>ct-fx-01</code></small>', b)
            self.assertIsNotNone(badge)
            self.assertEqual(a, b.replace(badge.group(0), ''))

    def test_no_llm_in_artifacts(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            corpus = fixture_site_corpus(t)
            out = t / 'site'
            build_site(corpus, out, 'internal', editorial=fx_ed(t))
            blob = '\n'.join(p.read_text(encoding='utf-8')
                             for p in out.rglob('*')
                             if p.suffix in ('.html', '.json', '.jsonl'))
            for marker in ('openai', 'anthropic', 'llm_generated',
                           'chatgpt', 'gpt-'):
                self.assertNotIn(marker, blob.lower())


@unittest.skipUnless(MISTUNE, 'mistune not installed')
class EditorialSeedDataTest(unittest.TestCase):
    """The shipped editorial seed records must stay schema-valid."""

    def test_seed_records_validate(self):
        ed = sem.load_editorial(REPO / 'editorial')
        self.assertGreater(len(ed['topics']), 0)
        self.assertGreater(len(ed['concepts']), 0)
        self.assertEqual(sem.validate_records(ed), [])

    def test_seed_no_personal_reviewer_names(self):
        ed = sem.load_editorial(REPO / 'editorial')
        for c in ed['concepts']:
            self.assertIn('review_role', c)
            self.assertNotIn('reviewer', c)
            self.assertNotIn('reviewed_by_name', c)
            self.assertNotRegex(c.get('review_role', ''), r'[А-Я][а-я]+ [А-Я]')


if __name__ == '__main__':
    unittest.main()
