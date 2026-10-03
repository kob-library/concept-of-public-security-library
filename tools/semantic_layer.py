#!/usr/bin/env python3
"""Semantic publishing layer: Topic / Concept / CitationTarget /
LanguageVariant — evidence-gated records with ONE deterministic
indexability policy shared by the HTML site (site_builder) and the
GitHub release tree (export_unified).

Editorial records live in editorial/*.jsonl in the repository and are
validated against live corpus evidence at build time: a concept whose
evidence can no longer be verified against real sections is dropped
from publication rather than published on faith. No source text is
ever modified — these are derived, secondary navigation records.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SCOPES = ('corpus_term', 'editorial_navigation', 'established_term')
ID_RX = {
    'topic': re.compile(r'^topic-[a-z0-9][a-z0-9-]*$'),
    'concept': re.compile(r'^cp-[a-z0-9][a-z0-9-]*$'),
    'citation': re.compile(r'^ct-[a-z0-9][a-z0-9-]*$'),
    # variant ids embed the corpus work_id, which uses the project's
    # Cyrillic-Latin slug alphabet — so the pattern must too.
    'variant': re.compile(r'^lv-[a-z0-9а-яё][a-z0-9а-яё-]*$'),
}
PUB_STATES = ('draft', 'published', 'withdrawn')
REVIEW_STATES = ('unreviewed', 'reviewed')
TRANSLATION_STATUSES = ('canonical', 'provisional', 'confirmed', 'absent')


def norm_ru(text: str) -> str:
    """Evidence matching normalization: case/ё/punctuation tolerant."""
    text = (text or '').lower().replace('ё', 'е').replace('­', '')
    return re.sub(r'[^\w]+', ' ', text)


def read_jsonl(path: Path) -> list:
    if not path or not Path(path).is_file():
        return []
    return [json.loads(l) for l in Path(path)
            .read_text(encoding='utf-8').splitlines() if l.strip()]


def write_jsonl(path: Path, rows: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n'
                            for r in rows), encoding='utf-8')


def load_editorial(ed_dir: Path | None) -> dict:
    """Load editorial seed records. Missing dir/files -> empty layer."""
    ed = {'topics': [], 'concepts': [], 'citations': [], 'variants': []}
    if not ed_dir or not Path(ed_dir).is_dir():
        return ed
    d = Path(ed_dir)
    for key, name in (('topics', 'topics.jsonl'), ('concepts', 'concepts.jsonl'),
                      ('citations', 'citations.jsonl'),
                      ('variants', 'language_variants.jsonl')):
        ed[key] = read_jsonl(d / name)
    return ed


# ------------------------------------------------------------------ schema

def _need(rec: dict, fields: tuple, idkey: str, errors: list):
    for f in fields:
        if rec.get(f) in (None, ''):
            errors.append(f'{rec.get(idkey, "?")}: missing field {f}')


def validate_records(ed: dict) -> list:
    """Schema + referential checks. Returns a list of error strings."""
    errors = []
    topic_ids = {t.get('topic_id') for t in ed['topics']}
    concept_ids = {c.get('concept_id') for c in ed['concepts']}
    for t in ed['topics']:
        if not ID_RX['topic'].match(t.get('topic_id') or ''):
            errors.append(f"bad topic_id {t.get('topic_id')}")
        _need(t, ('label', 'membership_rule', 'membership_rule_version',
                  'publication_state'), 'topic_id', errors)
        if (t.get('membership_rule') or {}).get('type') != 'title_terms':
            errors.append(f"{t['topic_id']}: unsupported membership_rule")
        if not (t.get('membership_rule') or {}).get('terms'):
            errors.append(f"{t['topic_id']}: empty membership terms")
        if t.get('publication_state') not in PUB_STATES:
            errors.append(f"{t['topic_id']}: bad publication_state")
    for c in ed['concepts']:
        if not ID_RX['concept'].match(c.get('concept_id') or ''):
            errors.append(f"bad concept_id {c.get('concept_id')}")
        _need(c, ('canonical_term', 'definition_scope', 'evidence',
                  'review_state', 'review_role', 'reviewed_at',
                  'publication_state'), 'concept_id', errors)
        if c.get('definition_scope') not in SCOPES:
            errors.append(f"{c['concept_id']}: bad definition_scope")
        if c.get('review_state') not in REVIEW_STATES:
            errors.append(f"{c['concept_id']}: bad review_state")
        if c.get('publication_state') not in PUB_STATES:
            errors.append(f"{c['concept_id']}: bad publication_state")
        for tid in c.get('related_topic_ids') or []:
            if tid not in topic_ids:
                errors.append(f"{c['concept_id']}: unknown topic {tid}")
        for cid in c.get('related_concept_ids') or []:
            if cid not in concept_ids:
                errors.append(f"{c['concept_id']}: unknown concept {cid}")
        if not isinstance(c.get('evidence'), list) or not c['evidence']:
            errors.append(f"{c['concept_id']}: evidence must be a non-empty list")
        else:
            for ev in c['evidence']:
                if not ev.get('section_id') or ev.get('kind') not in ('title', 'text'):
                    errors.append(f"{c['concept_id']}: malformed evidence {ev}")
    for ct in ed['citations']:
        if not ID_RX['citation'].match(ct.get('citation_target_id') or ''):
            errors.append(f"bad citation_target_id {ct.get('citation_target_id')}")
        _need(ct, ('work_id', 'section_id', 'anchor', 'language'),
              'citation_target_id', errors)
    errors += variant_chain_errors(ed['variants'])
    return errors


def variant_chain_errors(variants: list) -> list:
    """LanguageVariant rules: exactly one canonical variant per entity;
    variant-of-variant chains are forbidden — every non-canonical
    variant must point at the canonical one, never at another variant."""
    errors = []
    by_id = {v.get('variant_id'): v for v in variants}
    if len(by_id) != len(variants):
        errors.append('duplicate variant_id')
    by_entity = {}
    for v in variants:
        if not ID_RX['variant'].match(v.get('variant_id') or ''):
            errors.append(f"bad variant_id {v.get('variant_id')}")
        _need(v, ('entity_id', 'language', 'canonical_name',
                  'translation_status', 'review_status'),
              'variant_id', errors)
        if v.get('translation_status') not in TRANSLATION_STATUSES:
            errors.append(f"{v.get('variant_id')}: bad translation_status")
        by_entity.setdefault(v.get('entity_id'), []).append(v)
    for entity, vs in by_entity.items():
        canonical = [v for v in vs if v.get('variant_id')
                     == v.get('canonical_variant_id')]
        if len(canonical) != 1:
            errors.append(f'{entity}: must have exactly one canonical variant')
            continue
        canon = canonical[0]
        for v in vs:
            if v is canon:
                continue
            if v.get('canonical_variant_id') != canon['variant_id']:
                errors.append(
                    f"{v.get('variant_id')}: points at non-canonical variant "
                    f"{v.get('canonical_variant_id')} (variant-of-variant "
                    'chains are forbidden)')
            src = v.get('source_variant_id')
            if src is not None and src != canon['variant_id']:
                errors.append(
                    f"{v.get('variant_id')}: source_variant_id {src} is not "
                    'the canonical variant')
    for v in variants:
        src = v.get('source_variant_id')
        if src is not None and src not in by_id:
            errors.append(f"{v.get('variant_id')}: unknown source_variant_id {src}")
    return errors


# ---------------------------------------------------------------- evidence

def verify_evidence(concept: dict, get_title, get_text) -> list:
    """Confirm each evidence entry against real corpus content.
    `get_title(section_id)` -> title or None; `get_text(section_id)` ->
    body text or None. Returns the confirmed entries only — unverifiable
    evidence is dropped, never silently kept."""
    ok = []
    terms = [concept['canonical_term']] + list(concept.get('aliases') or [])
    for ev in concept.get('evidence') or []:
        sid = ev['section_id']
        term = ev.get('term') or concept['canonical_term']
        hay = get_title(sid) if ev['kind'] == 'title' else get_text(sid)
        if hay is None:
            continue
        nhay, nterm = norm_ru(hay), norm_ru(term)
        if nterm in nhay or any(norm_ru(t) in nhay for t in terms):
            ok.append(ev)
    return ok


def topic_members(topic: dict, sections: list) -> list:
    """First-match membership: a section joins at most one topic
    (callers pass topics in priority order and exclude used ids)."""
    terms = [norm_ru(t) for t in
             (topic.get('membership_rule') or {}).get('terms') or []]
    return [s for s in sections
            if s.get('kind') != 'frontmatter'
            and any(t in norm_ru(s.get('title') or '') for t in terms)]


# -------------------------------------------------------------- indexable

def indexable(kind: str, rec: dict | None = None, *,
              members=None, evidence=None) -> bool:
    """The ONE indexability policy. Page generation, sitemap, robots
    meta and language handling must all consult this function so an
    entity can never be classified differently across surfaces.
    Fails closed on unknown kinds."""
    if kind in ('chunk', 'search', 'alias', 'taxonomy_empty'):
        return False
    rec = rec or {}
    if kind == 'topic':
        return rec.get('publication_state') == 'published' and bool(members)
    if kind == 'concept':
        return (rec.get('publication_state') == 'published'
                and rec.get('review_state') == 'reviewed'
                and bool(evidence))
    if kind == 'language_variant':
        return (rec.get('translation_status') == 'confirmed'
                and rec.get('review_status') == 'reviewed')
    if kind == 'citation_target':
        return False  # addressing layer, never an indexable page class
    if kind in ('work', 'section', 'volume_index', 'landing', 'topics_index',
                'concepts_index', 'figure'):
        return True  # existing/publication-default behavior
    return False


# ---------------------------------------------------------------- JSON-LD

def ld_json(script_objs: list) -> str:
    """Serialize a JSON-LD graph for embedding."""
    graph = script_objs[0] if len(script_objs) == 1 else {'@graph': script_objs}
    if isinstance(graph, dict):
        graph.setdefault('@context', 'https://schema.org')
    return '<script type="application/ld+json">' + \
        json.dumps(graph, ensure_ascii=False) + '</script>'


def ld_breadcrumb(items: list) -> dict:
    """items: [(name, url_or_None)] — visible order."""
    return {'@type': 'BreadcrumbList',
            'itemListElement': [
                {'@type': 'ListItem', 'position': i + 1, 'name': n,
                 **({'item': u} if u else {})}
                for i, (n, u) in enumerate(items)]}


def ld_datacatalog(name: str, url: str, datasets: list, description='') -> dict:
    return {'@type': 'DataCatalog', 'name': name, 'url': url,
            **({'description': description} if description else {}),
            'dataset': datasets}


def ld_dataset(name: str, url: str, description='', downloads=None) -> dict:
    d = {'@type': 'Dataset', 'name': name, 'url': url,
         **({'description': description} if description else {}),
         'isAccessibleForFree': True}
    if downloads:
        d['distribution'] = [
            {'@type': 'DataDownload', 'contentUrl': u,
             'encodingFormat': fmt} for u, fmt in downloads]
    return d


def ld_book(name: str, url: str, *, part_of=None, language='ru') -> dict:
    return {'@type': 'Book', 'name': name, 'url': url,
            'inLanguage': language,
            **({'isPartOf': part_of} if part_of else {})}


def ld_definedterm(concept: dict, url: str, termset_url: str) -> dict:
    scope_label = {'corpus_term': 'Термин, специфичный для корпуса источников',
                   'editorial_navigation': 'Редакционный навигационный термин',
                   'established_term': 'Общеупотребительный термин'
                   }[concept['definition_scope']]
    return {'@type': 'DefinedTerm', 'name': concept['canonical_term'],
            'alternateName': list(concept.get('aliases') or []),
            'description': concept.get('editorial_description') or scope_label,
            'url': url, 'inDefinedTermSet': termset_url,
            'inLanguage': 'ru'}


def ld_definedtermset(name: str, url: str, terms: list) -> dict:
    return {'@type': 'DefinedTermSet', 'name': name, 'url': url,
            'hasDefinedTerm': terms}


def ld_collection(name: str, url: str, items: list, description='') -> dict:
    return {'@type': 'CollectionPage', 'name': name, 'url': url,
            **({'description': description} if description else {}),
            'mainEntity': {'@type': 'ItemList', 'itemListElement': items}}


# ------------------------------------------------------------ semantic ids

def section_public_path(volume: int, vol_path: str) -> str:
    """tom section: site/repo path from the volume-local record."""
    return f'tom-{volume}/' + str(Path(vol_path).with_suffix('.html')
                                  ).as_posix()


def section_repo_path(volume: int, vol_path: str) -> str:
    return f'tom-{volume}/' + str(Path(vol_path).with_suffix('.md')
                                  ).as_posix()
